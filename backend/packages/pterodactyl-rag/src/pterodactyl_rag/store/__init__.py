"""Vector store implementations for the plugin-docs RAG index."""

from __future__ import annotations

from .base import FacetValue, IndexStats, SearchResult, VectorStore
from .memory import InMemoryVectorStore, cosine_similarity

__all__ = [
    "VectorStore",
    "SearchResult",
    "FacetValue",
    "IndexStats",
    "InMemoryVectorStore",
    "cosine_similarity",
]
