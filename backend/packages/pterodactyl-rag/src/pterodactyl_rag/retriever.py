"""Query-side retrieval (design §4.5, §4.5.1).

Turns a natural-language query plus loose facet hints into a ranked hit list.
Responsibilities that keep the burden off the agent:

- **Fuzzy tag normalization** — the agent may pass ``plugin="EssentialsX"``; the
  retriever normalizes it to the canonical ``plugin:essentialsx`` and, when it
  does not exist verbatim, fuzzy-matches against the known plugin facet values.
- **Soft filtering** — delegated to the store: a filter that matches nothing is
  dropped and the result is flagged ``relaxed`` (never a hard empty failure).
- **Bounded snippets** — long chunk text is truncated so results never flood the
  model context.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from .embeddings import Embedder
from .store.base import VectorStore
from .tags import normalize_tag, normalize_value

DEFAULT_SNIPPET_CHARS = 500


@dataclass(slots=True)
class RetrievedHit:
    plugin: str | None
    title: str | None
    heading_path: str | None
    source_path: str
    chunk_index: int
    score: float
    snippet: str
    tags: list[str]


@dataclass(slots=True)
class RetrievalResponse:
    hits: list[RetrievedHit]
    filter_applied: list[str]
    filter_relaxed: bool
    note: str | None = None

    def as_dict(self) -> dict:
        return {
            "hits": [
                {
                    "plugin": h.plugin,
                    "title": h.title,
                    "heading_path": h.heading_path,
                    "source_path": h.source_path,
                    "chunk_index": h.chunk_index,
                    "score": h.score,
                    "snippet": h.snippet,
                    "tags": h.tags,
                }
                for h in self.hits
            ],
            "filter": {
                "applied": self.filter_applied,
                "relaxed": self.filter_relaxed,
                "note": self.note,
            },
        }


class Retriever:
    """Embeds queries and searches the store with fuzzy, soft tag filtering."""

    def __init__(self, store: VectorStore, embedder: Embedder, *, snippet_chars: int = DEFAULT_SNIPPET_CHARS) -> None:
        self._store = store
        self._embedder = embedder
        self._snippet_chars = snippet_chars

    async def _resolve_plugin_tag(self, plugin: str) -> str:
        """Normalize + fuzzy-match a plugin hint to a known ``plugin:`` tag.

        If the normalized value already exists as a facet, use it. Otherwise pick
        the closest known plugin (difflib) above a similarity cutoff; if nothing
        is close, keep the normalized guess (the store's soft fallback then
        relaxes it rather than failing).
        """
        value = normalize_value(plugin)
        if not value:
            return ""
        facets = await self._store.list_facets(namespace="plugin")
        known = [fv.value for fv in facets.get("plugin", [])]
        if value in known:
            return f"plugin:{value}"
        close = difflib.get_close_matches(value, known, n=1, cutoff=0.8)
        if close:
            return f"plugin:{close[0]}"
        return f"plugin:{value}"

    async def _build_tags(
        self,
        *,
        plugin: str | None,
        category: str | None,
        platform: str | None,
        lang: str | None,
        tags: list[str] | None,
    ) -> list[str]:
        resolved: list[str] = []
        if plugin:
            pt = await self._resolve_plugin_tag(plugin)
            if pt:
                resolved.append(pt)
        for ns, val in (("category", category), ("platform", platform), ("lang", lang)):
            if val:
                norm = normalize_tag(f"{ns}:{val}")
                if norm:
                    resolved.append(norm)
        for raw in tags or []:
            norm = normalize_tag(raw)
            if norm and norm not in resolved:
                resolved.append(norm)
        return resolved

    def _snippet(self, text: str) -> str:
        text = text.strip()
        if len(text) <= self._snippet_chars:
            return text
        return text[: self._snippet_chars].rstrip() + " …"

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        plugin: str | None = None,
        category: str | None = None,
        platform: str | None = None,
        lang: str | None = None,
        tags: list[str] | None = None,
    ) -> RetrievalResponse:
        query = (query or "").strip()
        if not query:
            return RetrievalResponse(hits=[], filter_applied=[], filter_relaxed=False, note="empty query")

        filter_tags = await self._build_tags(plugin=plugin, category=category, platform=platform, lang=lang, tags=tags)

        embedding = (await self._embedder.embed([query]))[0]
        result = await self._store.search(embedding, top_k=top_k, tags=filter_tags or None)

        note: str | None = None
        if result.relaxed:
            note = f"No documents matched the tag filter {filter_tags}; returned unfiltered results instead."
        elif not result.hits:
            note = "No documents in the index matched. Run `pterodactyl-rag ingest` if the index is empty."

        hits = [
            RetrievedHit(
                plugin=h.plugin,
                title=h.title,
                heading_path=h.heading_path,
                source_path=h.source_path,
                chunk_index=h.chunk_index,
                score=h.score,
                snippet=self._snippet(h.snippet),
                tags=h.tags,
            )
            for h in result.hits
        ]
        return RetrievalResponse(hits=hits, filter_applied=result.applied_tags, filter_relaxed=result.relaxed, note=note)
