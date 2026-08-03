"""Tests for the OpenAI-compatible embedder (batching + retry) via MockTransport."""

from __future__ import annotations

import httpx
import pytest

from pterodactyl_rag.embeddings import EmbeddingError, OpenAIEmbedder


def _make_embedder(handler, **kwargs) -> OpenAIEmbedder:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return OpenAIEmbedder(api_key="sk-test", model="m", dim=3, client=client, **kwargs)


async def test_embed_returns_vectors_in_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        inputs = body["input"]
        # Return in REVERSE order to prove the client re-sorts by index.
        data = [{"index": len(inputs) - 1 - i, "embedding": [float(len(inputs) - 1 - i)] * 3} for i in range(len(inputs))]
        return httpx.Response(200, json={"data": data})

    emb = _make_embedder(handler)
    vecs = await emb.embed(["a", "b", "c"])
    assert vecs == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]
    await emb.aclose()


async def test_empty_input() -> None:
    emb = _make_embedder(lambda r: httpx.Response(200, json={"data": []}))
    assert await emb.embed([]) == []
    await emb.aclose()


async def test_batching_multiple_requests() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls["n"] += 1
        inputs = json.loads(request.content)["input"]
        data = [{"index": i, "embedding": [1.0, 1.0, 1.0]} for i in range(len(inputs))]
        return httpx.Response(200, json={"data": data})

    emb = _make_embedder(handler, batch_size=2)
    vecs = await emb.embed(["a", "b", "c", "d", "e"])
    assert len(vecs) == 5
    assert calls["n"] == 3  # ceil(5/2)
    await emb.aclose()


async def test_retry_on_429_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # Avoid real backoff sleeps.
    import pterodactyl_rag.embeddings as mod

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 1.0, 1.0]}]})

    emb = _make_embedder(handler)
    vecs = await emb.embed(["a"])
    assert vecs == [[1.0, 1.0, 1.0]]
    assert state["n"] == 2
    await emb.aclose()


async def test_non_retryable_raises() -> None:
    emb = _make_embedder(lambda r: httpx.Response(400, text="bad request"))
    with pytest.raises(EmbeddingError, match="400"):
        await emb.embed(["a"])
    await emb.aclose()


async def test_exhausted_retries_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import pterodactyl_rag.embeddings as mod

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    emb = _make_embedder(lambda r: httpx.Response(503, text="down"), max_retries=3)
    with pytest.raises(EmbeddingError, match="after 3 attempts"):
        await emb.embed(["a"])
    await emb.aclose()


@pytest.mark.parametrize(
    "base_url,expected_local",
    [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:8080/v1", True),
        ("http://[::1]:11434/v1", True),
        ("https://api.openai.com/v1", False),
        ("https://embed.internal.example.com/v1", False),
    ],
)
def test_is_local_endpoint(base_url: str, expected_local: bool) -> None:
    emb = OpenAIEmbedder(api_key="sk", model="m", dim=3, base_url=base_url)
    assert emb._is_local_endpoint() is expected_local


async def test_local_endpoint_client_ignores_env_proxy() -> None:
    # A local endpoint must build a client that ignores ambient proxy env
    # (trust_env=False), so a socks:// ALL_PROXY can't break transport setup.
    emb = OpenAIEmbedder(api_key="sk", model="m", dim=3, base_url="http://localhost:11434/v1")
    client = await emb._get_client()
    assert client.trust_env is False
    await emb.aclose()


async def test_remote_endpoint_client_trusts_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear ambient proxy env so client construction is deterministic regardless
    # of the developer/CI shell (which may export a socks:// ALL_PROXY).
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    emb = OpenAIEmbedder(api_key="sk", model="m", dim=3, base_url="https://api.openai.com/v1")
    client = await emb._get_client()
    assert client.trust_env is True
    await emb.aclose()
