"""Namespaced tag resolution and normalization (design §4.2.1).

Tags are the ``namespace:value`` form stored as a Postgres ``TEXT[]`` and covered
by one GIN index. Two responsibilities live here:

- **Resolution** — derive tags for a document from its path (zero-config) plus an
  optional frontmatter/sidecar override.
- **Normalization** — canonicalize any tag (lowercase, trim, hyphenate, dedupe,
  cap) so ``EssentialsX`` / ``essentials x`` / ``Essentials-X`` all collapse to
  ``plugin:essentialsx``. This is what lets the query side stay forgiving.

The agent is never involved at ingest time; these functions run in the pipeline.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# Closed-set namespace whose value must be one of a fixed vocabulary. Path
# inference only assigns ``category`` when the folder name matches one of these.
KNOWN_CATEGORIES = ("config", "permissions", "commands", "faq", "api", "install")

PLUGIN_NS = "plugin"
CATEGORY_NS = "category"

# Guardrails against dirty/oversized tags leaking into the index.
MAX_TAGS_PER_DOC = 32
MAX_TAG_LENGTH = 80

_WHITESPACE_RE = re.compile(r"\s+")
_INVALID_VALUE_RE = re.compile(r"[^a-z0-9._/-]+")
_MULTI_HYPHEN_RE = re.compile(r"-{2,}")


def normalize_value(value: str) -> str:
    """Normalize a bare tag value (no namespace).

    Lowercases, collapses whitespace to single hyphens, drops characters outside
    ``[a-z0-9._/-]``, and trims stray hyphens. Returns ``""`` if nothing remains.
    """
    text = value.strip().lower()
    if not text:
        return ""
    text = _WHITESPACE_RE.sub("-", text)
    text = _INVALID_VALUE_RE.sub("-", text)
    text = _MULTI_HYPHEN_RE.sub("-", text)
    return text.strip("-._")


def normalize_tag(tag: str) -> str:
    """Normalize a full tag.

    A tag with a ``namespace:value`` shape has each side normalized
    independently; a bare tag (no colon) is normalized as a free value. Returns
    ``""`` when the result is empty or malformed (e.g. empty namespace/value),
    so callers can drop it.
    """
    raw = tag.strip()
    if not raw:
        return ""
    if ":" in raw:
        namespace, _, value = raw.partition(":")
        ns = normalize_value(namespace)
        val = normalize_value(value)
        if not ns or not val:
            return ""
        return f"{ns}:{val}"[:MAX_TAG_LENGTH]
    return normalize_value(raw)[:MAX_TAG_LENGTH]


def normalize_tags(tags: list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize a collection of tags: drop empties, dedupe (stable), cap count."""
    if not tags:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        norm = normalize_tag(tag)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
        if len(out) >= MAX_TAGS_PER_DOC:
            break
    return out


def infer_tags_from_path(source_path: str) -> list[str]:
    """Infer ``plugin:`` and (when recognized) ``category:`` tags from the path.

    ``EssentialsX/config/perms.md`` -> ``["plugin:essentialsx", "category:config"]``.
    The first path segment is the plugin; the second, if it matches
    :data:`KNOWN_CATEGORIES`, is the category. A bare filename yields no tags.
    """
    parts = [p for p in PurePosixPath(source_path).parts if p not in ("", "/", ".")]
    # Drop the filename (last part) — only directory segments carry taxonomy.
    dirs = parts[:-1] if len(parts) >= 1 else []
    tags: list[str] = []
    if dirs:
        plugin = normalize_value(dirs[0])
        if plugin:
            tags.append(f"{PLUGIN_NS}:{plugin}")
    if len(dirs) >= 2:
        cat = normalize_value(dirs[1])
        if cat in KNOWN_CATEGORIES:
            tags.append(f"{CATEGORY_NS}:{cat}")
    return tags


def resolve_tags(source_path: str, frontmatter: dict | None = None) -> list[str]:
    """Resolve the final tag set for a document.

    Combines path inference with frontmatter/sidecar overrides:

    - ``frontmatter["tags"]`` — a list of full or bare tags, appended.
    - ``frontmatter["plugin"]`` — an explicit ``plugin:`` value that **replaces**
      any path-inferred plugin (an operator override of a mis-named folder).

    The result is normalized (deduped, capped). Frontmatter-supplied values win
    on collisions because they are appended after inference and dedupe is stable
    on first occurrence — except ``plugin`` which is handled explicitly so the
    override truly replaces rather than co-exists.
    """
    frontmatter = frontmatter or {}
    inferred = infer_tags_from_path(source_path)

    explicit_plugin = frontmatter.get("plugin")
    if explicit_plugin:
        plugin_val = normalize_value(str(explicit_plugin))
        if plugin_val:
            inferred = [t for t in inferred if not t.startswith(f"{PLUGIN_NS}:")]
            inferred.insert(0, f"{PLUGIN_NS}:{plugin_val}")

    extra = frontmatter.get("tags")
    fm_tags: list[str] = []
    if isinstance(extra, str):
        fm_tags = [extra]
    elif isinstance(extra, (list, tuple)):
        fm_tags = [str(t) for t in extra]

    return normalize_tags([*inferred, *fm_tags])


def plugin_of(tags: list[str]) -> str | None:
    """Return the plugin value from a tag list, or ``None``."""
    prefix = f"{PLUGIN_NS}:"
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None
