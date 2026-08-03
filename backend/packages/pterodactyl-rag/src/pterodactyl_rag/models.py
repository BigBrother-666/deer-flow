"""Core domain types for the plugin-docs RAG pipeline.

These are transport-agnostic dataclasses shared by the ingestion pipeline, the
vector store, and the retriever. Tags are always the normalized, namespaced form
(e.g. ``plugin:essentialsx``); see :mod:`pterodactyl_rag.tags` and design §4.2.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Document:
    """A single source document (one file under the docs directory)."""

    source_path: str
    """Path relative to the configured docs directory (the natural key)."""

    content_hash: str
    """sha256 of the raw file bytes; unchanged hash => skip re-embedding."""

    title: str | None = None
    tags: list[str] = field(default_factory=list)
    """Normalized namespaced tags, e.g. ``["plugin:essentialsx", "category:config"]``."""

    mtime: float | None = None


@dataclass(slots=True)
class Chunk:
    """A contiguous slice of a document's text, the unit that gets embedded.

    ``tags`` are copied from the parent :class:`Document` so the vector store can
    filter and rank in a single scan without joining back to the document row
    (design §4.2).
    """

    chunk_index: int
    content: str
    heading_path: str | None = None
    token_count: int | None = None
    tags: list[str] = field(default_factory=list)
    embedding: list[float] | None = None
    """Populated by the embedding step; ``None`` before embedding."""


@dataclass(slots=True)
class SearchHit:
    """One retrieval result returned to the agent."""

    plugin: str | None
    title: str | None
    heading_path: str | None
    source_path: str
    chunk_index: int
    score: float
    """Cosine similarity in ``[0, 1]`` (1 == identical direction)."""

    snippet: str
    tags: list[str] = field(default_factory=list)
