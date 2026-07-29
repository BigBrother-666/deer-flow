"""Unit tests for the Pterodactyl console websocket fetch (socket mocked)."""

import json

import pytest

from deerflow.community.pterodactyl import console
from deerflow.community.pterodactyl.errors import PterodactylAPIError


class _FakeWS:
    """Scripted websocket: yields queued frames, then blocks (idle) forever."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        # No more scripted frames -> simulate idle so wait_for times out.
        import asyncio

        await asyncio.sleep(3600)


def _connect_factory(ws):
    class _Ctx:
        async def __aenter__(self_inner):
            return ws

        async def __aexit__(self_inner, *a):
            return False

    def _factory(socket_url, origin):
        return _Ctx()

    return _factory


@pytest.fixture
def patch_socket_details(monkeypatch):
    async def _details(server_id):
        return ("wss://wings.example.com/ws", "tok-123")

    monkeypatch.setattr(console, "_fetch_socket_details", _details)


@pytest.mark.anyio
async def test_fetch_replays_history_and_returns_last_n(patch_socket_details):
    frames = [
        json.dumps({"event": "auth success"}),
        json.dumps({"event": "console output", "args": ["line1\nline2"]}),
        json.dumps({"event": "console output", "args": ["line3"]}),
    ]
    ws = _FakeWS(frames)
    result = await console.fetch_recent_console("srv", 2, origin="https://panel.example.com", connect_fn=_connect_factory(ws), idle_timeout=0.01)
    assert result == ["line2", "line3"]
    # Auth + "send logs" were both issued.
    assert ws.sent[0] == {"event": "auth", "args": ["tok-123"]}
    assert {"event": "send logs", "args": [None]} in ws.sent


@pytest.mark.anyio
async def test_fetch_stops_on_token_expiry(patch_socket_details):
    frames = [
        json.dumps({"event": "auth success"}),
        json.dumps({"event": "console output", "args": ["only-line"]}),
        json.dumps({"event": "token expired"}),
    ]
    ws = _FakeWS(frames)
    result = await console.fetch_recent_console("srv", 100, origin="https://panel.example.com", connect_fn=_connect_factory(ws), idle_timeout=0.01)
    assert result == ["only-line"]


@pytest.mark.anyio
async def test_missing_socket_details_raises(monkeypatch):
    async def _request(self, method, path, **kwargs):
        return {"data": {}}

    monkeypatch.setattr("deerflow.community.pterodactyl.client.PterodactylClient.__init__", lambda self, config=None: None)
    monkeypatch.setattr("deerflow.community.pterodactyl.client.PterodactylClient.request", _request)
    with pytest.raises(PterodactylAPIError):
        await console._fetch_socket_details("srv")


@pytest.mark.anyio
async def test_run_command_capture_sends_command_and_reads(patch_socket_details):
    frames = [
        json.dumps({"event": "auth success"}),
        json.dumps({"event": "console output", "args": ["There are 2 players online:"]}),
        json.dumps({"event": "console output", "args": ["steve, alex"]}),
    ]
    ws = _FakeWS(frames)
    result = await console.run_command_capture("srv", "list", origin="https://panel.example.com", connect_fn=_connect_factory(ws), idle_timeout=0.01)
    assert result == ["There are 2 players online:", "steve, alex"]
    # Auth first, then the command is sent over the socket (no history request).
    assert ws.sent[0] == {"event": "auth", "args": ["tok-123"]}
    assert {"event": "send command", "args": ["list"]} in ws.sent
    assert not any(s.get("event") == "send logs" for s in ws.sent)


@pytest.mark.anyio
async def test_run_command_capture_returns_empty_when_no_output(patch_socket_details):
    frames = [json.dumps({"event": "auth success"})]
    ws = _FakeWS(frames)
    result = await console.run_command_capture("srv", "quietcmd", origin="https://panel.example.com", connect_fn=_connect_factory(ws), idle_timeout=0.01, overall_timeout=0.05)
    assert result == []
