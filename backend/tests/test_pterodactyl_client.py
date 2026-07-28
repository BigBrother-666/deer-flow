"""Unit tests for the Pterodactyl async client (HTTP mocked)."""

import httpx
import pytest

from deerflow.community.pterodactyl.client import PterodactylClient
from deerflow.community.pterodactyl.config import PterodactylConfig
from deerflow.community.pterodactyl.errors import (
    PterodactylAPIError,
    PterodactylAuthError,
    PterodactylNotFoundError,
    PterodactylTimeoutError,
)


def _client() -> PterodactylClient:
    return PterodactylClient(PterodactylConfig(panel_url="https://p.example.com", api_key="k", timeout=5))


def _patch_request(monkeypatch, handler):
    """Replace httpx.AsyncClient wholesale so no real client (or env proxy) is built."""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            return handler(method, url, kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


@pytest.mark.anyio
async def test_request_success_json(monkeypatch):
    _patch_request(monkeypatch, lambda *_: httpx.Response(200, json={"data": []}))
    result = await _client().request("GET", "/servers")
    assert result == {"data": []}


@pytest.mark.anyio
async def test_request_sends_auth_header(monkeypatch):
    seen = {}

    def handler(method, url, kwargs):
        seen["auth"] = kwargs["headers"]["Authorization"]
        return httpx.Response(200, json={})

    _patch_request(monkeypatch, handler)
    await _client().request("GET", "/servers")
    assert seen["auth"] == "Bearer k"


@pytest.mark.anyio
async def test_request_raw_text(monkeypatch):
    _patch_request(monkeypatch, lambda *_: httpx.Response(200, text="server.properties"))
    result = await _client().request("GET", "/files/contents", expect_json=False)
    assert result == "server.properties"


@pytest.mark.anyio
async def test_auth_error_on_401(monkeypatch):
    _patch_request(monkeypatch, lambda *_: httpx.Response(401, json={}))
    with pytest.raises(PterodactylAuthError):
        await _client().request("GET", "/servers")


@pytest.mark.anyio
async def test_not_found_on_404(monkeypatch):
    _patch_request(monkeypatch, lambda *_: httpx.Response(404, json={}))
    with pytest.raises(PterodactylNotFoundError):
        await _client().request("GET", "/servers/bad")


@pytest.mark.anyio
async def test_api_error_extracts_detail(monkeypatch):
    body = {"errors": [{"detail": "Validation failed", "code": "ValidationException"}]}
    _patch_request(monkeypatch, lambda *_: httpx.Response(422, json=body))
    with pytest.raises(PterodactylAPIError) as exc:
        await _client().request("POST", "/servers/x/files/write")
    assert exc.value.detail == "Validation failed"
    assert exc.value.status_code == 422


@pytest.mark.anyio
async def test_retries_then_succeeds_on_transient(monkeypatch):
    calls = {"n": 0}

    def handler(method, url, kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"ok": True})

    _patch_request(monkeypatch, handler)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    result = await _client().request("GET", "/servers")
    assert result == {"ok": True}
    assert calls["n"] == 2


@pytest.mark.anyio
async def test_timeout_raises_after_retries(monkeypatch):
    def handler(method, url, kwargs):
        raise httpx.TimeoutException("slow")

    _patch_request(monkeypatch, handler)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    with pytest.raises(PterodactylTimeoutError):
        await _client().request("GET", "/servers")


async def _no_sleep(_seconds):
    return None
