"""Unit tests for pterodactyl read-only tools (client mocked)."""

import json

import pytest

from deerflow.community.pterodactyl import tools
from deerflow.community.pterodactyl.errors import PterodactylNotFoundError


@pytest.fixture
def mock_request(monkeypatch):
    """Patch PterodactylClient.request; test supplies a handler(method, path, kwargs)."""

    def _install(handler):
        # Skip real config loading in the client constructor.
        monkeypatch.setattr(tools.PterodactylClient, "__init__", lambda self, config=None: None)

        async def _request(self, method, path, *, params=None, json=None, expect_json=True):
            return handler(method, path, {"params": params, "json": json, "expect_json": expect_json})

        monkeypatch.setattr(tools.PterodactylClient, "request", _request)

    return _install


@pytest.mark.anyio
async def test_list_servers(mock_request):
    payload = {"data": [{"attributes": {"identifier": "abc123", "name": "MC", "node": "n1", "description": "d"}}]}
    seen = {}

    def handler(method, path, kwargs):
        seen.update(kwargs)
        return payload

    mock_request(handler)
    result = json.loads(await tools.list_servers_tool.ainvoke({}))
    assert result == [{"server_id": "abc123", "name": "MC", "node": "n1", "description": "d"}]
    # Default listing sends no type filter (owner/accessible servers only).
    assert seen["params"] is None


@pytest.mark.anyio
async def test_list_servers_show_all_sends_admin_filter(mock_request):
    seen = {}

    def handler(method, path, kwargs):
        seen.update(kwargs)
        return {"data": []}

    mock_request(handler)
    await tools.list_servers_tool.ainvoke({"show_all": True})
    assert seen["params"] == {"type": "admin-all"}


@pytest.mark.anyio
async def test_get_resources(mock_request):
    payload = {"attributes": {"current_state": "running", "resources": {"memory_bytes": 100}}}
    mock_request(lambda *_: payload)
    result = json.loads(await tools.get_resources_tool.ainvoke({"server_id": "abc123"}))
    assert result["current_state"] == "running"


@pytest.mark.anyio
async def test_read_file_truncates(mock_request):
    mock_request(lambda *_: "x" * 100)
    result = await tools.read_file_tool.ainvoke({"server_id": "s", "file_path": "/f", "max_chars": 10})
    assert result.startswith("x" * 10)
    assert "truncated" in result


@pytest.mark.anyio
async def test_read_file_passes_path_param(mock_request):
    seen = {}

    def handler(method, path, kwargs):
        seen.update(kwargs["params"])
        return "content"

    mock_request(handler)
    await tools.read_file_tool.ainvoke({"server_id": "s", "file_path": "/server.properties"})
    assert seen["file"] == "/server.properties"


@pytest.mark.anyio
async def test_error_is_normalized(mock_request):
    def handler(*_):
        raise PterodactylNotFoundError("Resource not found", status_code=404)

    mock_request(handler)
    result = await tools.get_server_tool.ainvoke({"server_id": "bad"})
    assert result.startswith("Error:")
