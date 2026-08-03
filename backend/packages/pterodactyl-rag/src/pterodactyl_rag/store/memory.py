"""In-memory VectorStore for tests and lightweight local use.

Implements the exact same contract as the pgvector store — including soft-filter
fallback and facet grouping — with plain Python so the test suite needs no
database. Not intended for production (no persistence, linear scan).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..models import Chunk, Document, SearchHit
from ..tags import plugin_of
from .base import FacetValue, IndexStats, SearchResult


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity in ``[-1, 1]``; 0 for a zero vector."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass(slots=True)
class _StoredChunk:
    source_path: str
    chunk_index: int
    content: str
    heading_path: str | None
    tags: list[str]
    embedding: list[float]


@dataclass(slots=True)
class InMemoryVectorStore:
    """Dict-backed store keyed by ``source_path``."""

    _docs: dict[str, Document] = field(default_factory=dict)
    _chunks: dict[str, list[_StoredChunk]] = field(default_factory=dict)
    _embed_model: str | None = None
    _embed_dim: int | None = None
    _last_ingest: str | None = None

    async def initialize(self, *, embed_model: str, embed_dim: int) -> None:
        if self._embed_dim is not None and self._embed_dim != embed_dim:
            raise ValueError(f"Index dimension mismatch: stored {self._embed_dim}, configured {embed_dim}. Reset and re-ingest.")
        if self._embed_model is not None and self._embed_model != embed_model:
            raise ValueError(f"Index model mismatch: stored {self._embed_model!r}, configured {embed_model!r}. Reset and re-ingest.")
        self._embed_model = embed_model
        self._embed_dim = embed_dim

    async def get_document_hash(self, source_path: str) -> str | None:
        doc = self._docs.get(source_path)
        return doc.content_hash if doc else None

    async def upsert_document(self, document: Document, chunks: list[Chunk]) -> None:
        self._docs[document.source_path] = document
        stored: list[_StoredChunk] = []
        for chunk in chunks:
            if chunk.embedding is None:
                raise ValueError("Chunk embedding is required for upsert")
            stored.append(
                _StoredChunk(
                    source_path=document.source_path,
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                    heading_path=chunk.heading_path,
                    tags=list(chunk.tags),
                    embedding=list(chunk.embedding),
                )
            )
        self._chunks[document.source_path] = stored
        self._last_ingest = "now"

    async def delete_documents(self, source_paths: list[str]) -> int:
        removed = 0
        for path in source_paths:
            if path in self._docs:
                del self._docs[path]
                self._chunks.pop(path, None)
                removed += 1
        return removed

    async def list_source_paths(self) -> list[str]:
        return sorted(self._docs)

    def _all_chunks(self) -> list[_StoredChunk]:
        out: list[_StoredChunk] = []
        for chunks in self._chunks.values():
            out.extend(chunks)
        return out

    def _rank(self, query_embedding: list[float], candidates: list[_StoredChunk], top_k: int) -> list[SearchHit]:
        scored = [(cosine_similarity(query_embedding, c.embedding), c) for c in candidates]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        hits: list[SearchHit] = []
        for score, c in scored[:top_k]:
            doc = self._docs.get(c.source_path)
            hits.append(
                SearchHit(
                    plugin=plugin_of(c.tags),
                    title=doc.title if doc else None,
                    heading_path=c.heading_path,
                    source_path=c.source_path,
                    chunk_index=c.chunk_index,
                    score=round(max(score, 0.0), 6),
                    snippet=c.content,
                    tags=list(c.tags),
                )
            )
        return hits

    async def search(self, query_embedding: list[float], *, top_k: int, tags: list[str] | None = None) -> SearchResult:
        everything = self._all_chunks()
        applied = list(tags or [])
        if applied:
            tagset = set(applied)
            filtered = [c for c in everything if tagset.issubset(set(c.tags))]
            if filtered:
                return SearchResult(hits=self._rank(query_embedding, filtered, top_k), applied_tags=applied, relaxed=False)
            # Soft fallback: no matches -> drop the filter (design §4.5.1).
            return SearchResult(hits=self._rank(query_embedding, everything, top_k), applied_tags=applied, relaxed=True)
        return SearchResult(hits=self._rank(query_embedding, everything, top_k), applied_tags=[], relaxed=False)

    async def get_document_text(self, source_path: str, *, max_chars: int) -> str | None:
        chunks = self._chunks.get(source_path)
        if not chunks:
            return None
        ordered = sorted(chunks, key=lambda c: c.chunk_index)
        text = "\n\n".join(c.content for c in ordered)
        return text[:max_chars]

    async def list_sources(self, *, plugin: str | None = None) -> list[dict]:
        out: list[dict] = []
        for path, doc in sorted(self._docs.items()):
            doc_plugin = plugin_of(doc.tags)
            if plugin is not None and doc_plugin != plugin:
                continue
            out.append(
                {
                    "source_path": path,
                    "title": doc.title,
                    "plugin": doc_plugin,
                    "tags": list(doc.tags),
                    "chunks": len(self._chunks.get(path, [])),
                }
            )
        return out

    async def list_facets(self, *, namespace: str | None = None) -> dict[str, list[FacetValue]]:
        counts: dict[str, dict[str, int]] = {}
        for doc in self._docs.values():
            seen_ns_val: set[tuple[str, str]] = set()
            for tag in doc.tags:
                ns, _, val = tag.partition(":")
                if not val:
                    ns, val = "_", ns
                if namespace is not None and ns != namespace:
                    continue
                if (ns, val) in seen_ns_val:
                    continue
                seen_ns_val.add((ns, val))
                counts.setdefault(ns, {}).setdefault(val, 0)
                counts[ns][val] += 1
        result: dict[str, list[FacetValue]] = {}
        for ns, vals in counts.items():
            result[ns] = [FacetValue(value=v, docs=n) for v, n in sorted(vals.items(), key=lambda kv: (-kv[1], kv[0]))]
        return result

    async def stats(self) -> IndexStats:
        return IndexStats(
            documents=len(self._docs),
            chunks=sum(len(c) for c in self._chunks.values()),
            embed_model=self._embed_model,
            embed_dim=self._embed_dim,
            last_ingest=self._last_ingest,
        )
