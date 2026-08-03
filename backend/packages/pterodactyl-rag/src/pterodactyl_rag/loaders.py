"""Local document discovery and parsing (design §4.3 step 1).

Walks the docs directory, parses each supported file into plain text, extracts
YAML frontmatter (markdown), and resolves the document's tags via
:mod:`pterodactyl_rag.tags`. PDF/HTML parsers are imported lazily so the module
loads in environments without those optional dependencies.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import Document
from .tags import resolve_tags

logger = logging.getLogger(__name__)

MARKDOWN_EXTS = {".md", ".markdown"}
TEXT_EXTS = {".txt"}
HTML_EXTS = {".html", ".htm"}
PDF_EXTS = {".pdf"}
SUPPORTED_EXTS = MARKDOWN_EXTS | TEXT_EXTS | HTML_EXTS | PDF_EXTS

_FRONTMATTER_FENCE = "---"


@dataclass(slots=True)
class LoadedDocument:
    """A parsed document plus the metadata needed to index it."""

    document: Document
    text: str
    is_markdown: bool


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a leading ``---`` YAML frontmatter block from markdown body.

    Returns ``(frontmatter_dict, body)``. When no valid frontmatter is present
    (or it fails to parse / is not a mapping) returns ``({}, text)`` unchanged.
    """
    if not text.startswith(_FRONTMATTER_FENCE):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_FENCE:
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError:
                logger.warning("Invalid YAML frontmatter; ignoring")
                return {}, text
            return (data if isinstance(data, dict) else {}), body.lstrip("\n")
    return {}, text


def _read_sidecar(path: Path) -> dict:
    """Read an optional ``.rag.yaml`` in the file's directory for folder tags."""
    sidecar = path.parent / ".rag.yaml"
    if not sidecar.is_file():
        return {}
    try:
        data = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_title(frontmatter: dict, body: str, is_markdown: bool) -> str | None:
    title = frontmatter.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    if is_markdown:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
            if stripped:
                break
    return None


def _parse_html(data: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _parse_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def load_file(path: Path, docs_dir: Path) -> LoadedDocument | None:
    """Parse one file into a :class:`LoadedDocument`, or ``None`` if unsupported.

    ``source_path`` is stored relative to ``docs_dir`` (the natural key). Tags
    merge sidecar + frontmatter overrides; frontmatter wins on the same key.
    """
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        return None

    raw = path.read_bytes()
    content_hash = _sha256_bytes(raw)
    source_path = path.relative_to(docs_dir).as_posix()
    is_markdown = ext in MARKDOWN_EXTS

    frontmatter: dict = {}
    if is_markdown or ext in TEXT_EXTS:
        text = raw.decode("utf-8", errors="replace")
        if is_markdown:
            frontmatter, text = split_frontmatter(text)
    elif ext in HTML_EXTS:
        text = _parse_html(raw)
    else:  # PDF
        text = _parse_pdf(path)

    sidecar = _read_sidecar(path)
    merged_fm = {**sidecar, **frontmatter}
    tags = resolve_tags(source_path, merged_fm)
    title = _extract_title(merged_fm, text, is_markdown)

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None

    document = Document(
        source_path=source_path,
        content_hash=content_hash,
        title=title,
        tags=tags,
        mtime=mtime,
    )
    return LoadedDocument(document=document, text=text, is_markdown=is_markdown)


def iter_documents(docs_dir: str | Path):
    """Yield :class:`LoadedDocument` for every supported file under ``docs_dir``.

    Hidden files/directories (``.``-prefixed) and unsupported extensions are
    skipped. Files that fail to parse are logged and skipped rather than
    aborting the whole ingest.
    """
    root = Path(docs_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"docs_dir is not a directory: {docs_dir}")
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() not in SUPPORTED_EXTS:
            continue
        try:
            loaded = load_file(path, root)
        except Exception:  # noqa: BLE001 - one bad file must not abort ingest
            logger.exception("Failed to parse %s; skipping", path)
            continue
        if loaded is not None:
            yield loaded
