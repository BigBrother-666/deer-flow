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


@pytest.mark.anyio
async def test_read_file_lines_windows_output(mock_request):
    mock_request(lambda *_: "\n".join(f"line{i}" for i in range(10)))
    result = await tools.read_file_lines_tool.ainvoke({"server_id": "s", "file_path": "/f", "offset": 2, "limit": 3})
    assert "[lines 2-5 of 10]" in result
    assert "line2" in result and "line4" in result
    assert "line5" not in result.split("\n", 1)[1]


@pytest.mark.anyio
async def test_search_file_returns_only_matches(mock_request):
    body = "ok\nERROR: boom\nok\nerror: again\n"
    mock_request(lambda *_: body)
    result = await tools.search_file_tool.ainvoke({"server_id": "s", "file_path": "/log", "pattern": "error"})
    assert "2: ERROR: boom" in result
    assert "4: error: again" in result
    assert "ok" not in result


@pytest.mark.anyio
async def test_search_file_no_matches(mock_request):
    mock_request(lambda *_: "nothing here")
    result = await tools.search_file_tool.ainvoke({"server_id": "s", "file_path": "/log", "pattern": "zzz"})
    assert "no matches" in result


@pytest.mark.anyio
async def test_search_file_invalid_regex(mock_request):
    mock_request(lambda *_: "x")
    result = await tools.search_file_tool.ainvoke({"server_id": "s", "file_path": "/log", "pattern": "("})
    assert result.startswith("Error: invalid search pattern")


@pytest.mark.anyio
async def test_read_console_joins_lines(monkeypatch):
    async def _fake(server_id, lines, *, origin, **kwargs):
        return ["a", "b", "c"][-lines:]

    monkeypatch.setattr(tools, "fetch_recent_console", _fake)
    monkeypatch.setattr(tools, "load_config", lambda: type("C", (), {"panel_url": "https://p"})())
    result = await tools.read_console_tool.ainvoke({"server_id": "s", "lines": 2})
    assert result == "b\nc"


@pytest.mark.anyio
async def test_read_console_empty(monkeypatch):
    async def _fake(server_id, lines, *, origin, **kwargs):
        return []

    monkeypatch.setattr(tools, "fetch_recent_console", _fake)
    monkeypatch.setattr(tools, "load_config", lambda: type("C", (), {"panel_url": "https://p"})())
    result = await tools.read_console_tool.ainvoke({"server_id": "s"})
    assert "no console output" in result
