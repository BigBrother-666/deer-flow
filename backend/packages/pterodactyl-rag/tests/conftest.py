"""Shared test fixtures/helpers for the RAG package."""

from __future__ import annotations

import hashlib

from pterodactyl_rag.embeddings import Embedder


class FakeEmbedder(Embedder):
    """Deterministic, network-free embedder for tests.

    Produces a small fixed-dim vector from a hash of the text so identical text
    yields identical vectors and similar text is not required to be close — tests
    that need ordering seed the store with vectors directly instead.
    """

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vec = [((digest[i % len(digest)]) / 255.0) for i in range(self._dim)]
            out.append(vec)
        return out
