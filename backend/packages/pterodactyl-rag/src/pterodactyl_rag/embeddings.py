"""OpenAI-compatible batch embedding client (design §4.3 step 3).

A thin async wrapper over the ``POST /embeddings`` endpoint with batching and
retry/backoff on 429/5xx. Kept dependency-light (``httpx`` only) so it works
against any OpenAI-compatible embeddings server (OpenAI, Azure, local vLLM, etc.).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class Embedder(Protocol):
    """Minimal embedding interface the pipeline and retriever depend on.

    Kept as a Protocol so tests can substitute a deterministic fake without a
    network call or API key.
    """

    @property
    def dim(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingError(RuntimeError):
    """Raised when the embedding endpoint fails after retries."""


class OpenAIEmbedder:
    """Async batch embedder for an OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dim: int,
        base_url: str | None = None,
        batch_size: int = 64,
        max_retries: int = 5,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dim = dim
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._batch_size = max(batch_size, 1)
        self._max_retries = max(max_retries, 1)
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def dim(self) -> int:
        return self._dim

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # A local embed endpoint (Ollama / TEI on localhost) must never be
            # routed through an ambient proxy (HTTP(S)_PROXY / ALL_PROXY). Beyond
            # being pointless, a socks:// ALL_PROXY makes httpx fail to build its
            # transport unless socksio is installed. Disable trust_env for local
            # hosts so proxy env is ignored; remote endpoints keep proxy support.
            self._client = httpx.AsyncClient(timeout=self._timeout, trust_env=not self._is_local_endpoint())
        return self._client

    def _is_local_endpoint(self) -> bool:
        host = urlparse(self._base_url).hostname or ""
        return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> OpenAIEmbedder:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        client = await self._get_client()
        url = f"{self._base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload = {"model": self._model, "input": batch}

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    data = resp.json()["data"]
                    # Preserve request order regardless of server ordering.
                    ordered = sorted(data, key=lambda d: d["index"])
                    return [item["embedding"] for item in ordered]
                if resp.status_code not in _RETRYABLE_STATUS:
                    raise EmbeddingError(f"Embedding request failed [{resp.status_code}]: {resp.text[:200]}")
                last_exc = EmbeddingError(f"retryable status {resp.status_code}")

            backoff = min(2.0**attempt, 30.0)
            logger.warning("Embedding batch attempt %d/%d failed (%s); retrying in %.1fs", attempt + 1, self._max_retries, last_exc, backoff)
            await asyncio.sleep(backoff)

        raise EmbeddingError(f"Embedding endpoint failed after {self._max_retries} attempts: {last_exc}")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` in order, batching requests. Empty input -> ``[]``."""
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors = await self._embed_batch(batch)
            if len(vectors) != len(batch):
                raise EmbeddingError(f"Embedding count mismatch: sent {len(batch)}, got {len(vectors)}")
            out.extend(vectors)
        return out
