"""Token-aware, heading-aware chunking (design §4.3 step 2).

Markdown is split along ATX headings so each chunk keeps a ``heading_path`` like
``"Config > Permissions"`` for citation; sections larger than the token budget
are further split with overlap. Non-markdown text uses a recursive character/
paragraph splitter with the same token budget and overlap.

Token counting uses ``tiktoken`` when available and falls back to a whitespace
word count otherwise, so the splitter works in environments without the encoder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Chunk

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(slots=True)
class _Section:
    heading_path: str | None
    text: str


class _TokenCounter:
    """Lazy tiktoken-backed counter with a whitespace fallback."""

    def __init__(self) -> None:
        self._encoder = None
        try:
            import tiktoken

            self._encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:  # pragma: no cover - only when tiktoken is absent
            self._encoder = None

    def count(self, text: str) -> int:
        if self._encoder is not None:
            return len(self._encoder.encode(text))
        return len(text.split())

    def split_by_tokens(self, text: str, max_tokens: int, overlap: int) -> list[str]:
        """Split ``text`` into windows of at most ``max_tokens`` with overlap."""
        if self._encoder is not None:
            ids = self._encoder.encode(text)
            if len(ids) <= max_tokens:
                return [text]
            step = max(max_tokens - overlap, 1)
            out: list[str] = []
            for start in range(0, len(ids), step):
                window = ids[start : start + max_tokens]
                if not window:
                    break
                out.append(self._encoder.decode(window).strip())
                if start + max_tokens >= len(ids):
                    break
            return [w for w in out if w]
        # Whitespace fallback: window over words.
        words = text.split()
        if len(words) <= max_tokens:
            return [text]
        step = max(max_tokens - overlap, 1)
        out = []
        for start in range(0, len(words), step):
            window = words[start : start + max_tokens]
            if not window:
                break
            out.append(" ".join(window))
            if start + max_tokens >= len(words):
                break
        return [w for w in out if w]


def _split_markdown_sections(text: str) -> list[_Section]:
    """Split markdown into sections keyed by their heading path.

    A running stack of headings by level builds the ``heading_path`` breadcrumb.
    Text before the first heading becomes a section with ``heading_path=None``.
    """
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    buf: list[str] = []
    current_path: str | None = None

    def flush() -> None:
        nonlocal buf
        body = "\n".join(buf).strip()
        if body:
            sections.append(_Section(heading_path=current_path, text=body))
        buf = []

    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_path = " > ".join(t for _, t in stack)
        else:
            buf.append(line)
    flush()
    return sections


def _split_plain_sections(text: str) -> list[_Section]:
    """Treat blank-line-separated paragraphs as one flat, heading-less section."""
    body = text.strip()
    return [_Section(heading_path=None, text=body)] if body else []


def split_document(
    text: str,
    *,
    is_markdown: bool,
    max_tokens: int,
    overlap: int,
    tags: list[str] | None = None,
) -> list[Chunk]:
    """Split raw document text into ordered :class:`Chunk` objects.

    Args:
        text: Raw document text.
        is_markdown: Use the heading-aware splitter (else plain paragraphs).
        max_tokens: Target maximum tokens per chunk.
        overlap: Token overlap between sub-chunks of an oversized section.
        tags: Tags copied onto every produced chunk (design §4.2).
    """
    counter = _TokenCounter()
    sections = _split_markdown_sections(text) if is_markdown else _split_plain_sections(text)

    chunks: list[Chunk] = []
    index = 0
    for section in sections:
        for piece in counter.split_by_tokens(section.text, max_tokens, overlap):
            piece = piece.strip()
            if not piece:
                continue
            chunks.append(
                Chunk(
                    chunk_index=index,
                    content=piece,
                    heading_path=section.heading_path,
                    token_count=counter.count(piece),
                    tags=list(tags or []),
                )
            )
            index += 1
    return chunks
