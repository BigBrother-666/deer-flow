"""VectorStore protocol + shared result types (design §4.2, §4.5.1).

The store owns persistence and similarity search. Two implementations exist: an
in-memory store (tests) and a pgvector-backed store (production). The soft-filter
fallback (tag filter that yields 0 hits retries without the filter) is the
store's responsibility so every backend behaves identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..models import Chunk, Document, SearchHit


@dataclass(slots=True)
class SearchResult:
    """A ranked hit list plus provenance about the filter that produced it."""

    hits: list[SearchHit]
    applied_tags: list[str] = field(default_factory=list)
    """Tags that were actually applied as a filter (post-normalization)."""

    relaxed: bool = False
    """True when a tag filter matched nothing and was dropped for a full search."""


@dataclass(slots=True)
class FacetValue:
    value: str
    docs: int


@dataclass(slots=True)
class IndexStats:
    documents: int
    chunks: int
    embed_model: str | None
    embed_dim: int | None
    last_ingest: str | None


@runtime_checkable
class VectorStore(Protocol):
    """Persistence + retrieval interface shared by all backends."""

    async def initialize(self, *, embed_model: str, embed_dim: int) -> None:
        """Create schema/tables if needed and validate the embedding dimension.

        Must raise if a previously-initialized index used a different dimension
        or model (design §4.4) — mixing embedding spaces silently is a bug.
        """
        ...

    async def get_document_hash(self, source_path: str) -> str | None:
        """Return the stored content hash for a document, or ``None``."""
        ...

    async def upsert_document(self, document: Document, chunks: list[Chunk]) -> None:
        """Insert/replace a document and its chunks atomically.

        Chunk embeddings must be populated. Tags on chunks are the document's
        tags (the caller copies them; the store persists them as given).
        """
        ...

    async def delete_documents(self, source_paths: list[str]) -> int:
        """Delete documents (and cascade their chunks). Returns count removed."""
        ...

    async def list_source_paths(self) -> list[str]:
        """Return every indexed document's source_path."""
        ...

    async def search(
        self,
        query_embedding: list[float],
        *,
        top_k: int,
        tags: list[str] | None = None,
    ) -> SearchResult:
        """Cosine-similarity search with optional soft tag pre-filter.

        If ``tags`` is given, results are restricted to chunks whose tag array
        contains all of them; if that yields nothing, the store retries without
        the filter and marks ``relaxed=True`` (design §4.5.1).
        """
        ...

    async def get_document_text(self, source_path: str, *, max_chars: int) -> str | None:
        """Reassemble a document's chunk texts in order (bounded), or ``None``."""
        ...

    async def list_sources(self, *, plugin: str | None = None) -> list[dict]:
        """List indexed documents with plugin/title/chunk counts."""
        ...

    async def list_facets(self, *, namespace: str | None = None) -> dict[str, list[FacetValue]]:
        """Return tag values present in the index grouped by namespace."""
        ...

    async def stats(self) -> IndexStats:
        """Return index health counters."""
        ...
