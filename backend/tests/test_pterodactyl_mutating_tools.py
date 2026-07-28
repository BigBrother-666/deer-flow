"""Unit tests for pterodactyl mutating tools (client mocked, guard not involved).

These verify the HTTP request shape each tool produces. The human-in-the-loop
gate is tested separately in test_pterodactyl_guard.py.
"""

import pytest

from deerflow.community.pterodactyl import tools
from deerflow.community.pterodactyl.errors import PterodactylAuthError


@pytest.fixture
def capture(monkeypatch):
    """Capture the (method, path, kwargs) of the single request each tool makes."""
    calls = []

    monkeypatch.setattr(tools.PterodactylClient, "__init__", lambda self, config=None: None)

    def _install(response=None):
        async def _request(self, method, path, *, params=None, json=None, content=None, expect_json=True):
            calls.append({"method": method, "path": path, "params": params, "json": json, "content": content})
            return response

        monkeypatch.setattr(tools.PterodactylClient, "request", _request)
        return calls

    return _install


@pytest.mark.anyio
async def test_power_action_posts_signal(capture):
    calls = capture()
    result = await tools.power_action_tool.ainvoke({"server_id": "s", "signal": "restart"})
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/servers/s/power"
    assert calls[0]["json"] == {"signal": "restart"}
    assert result.startswith("OK")


@pytest.mark.anyio
async def test_power_action_rejects_bad_signal(capture):
    calls = capture()
    result = await tools.power_action_tool.ainvoke({"server_id": "s", "signal": "explode"})
    assert result.startswith("Error")
    assert calls == []  # never hits the API


@pytest.mark.anyio
async def test_send_command_posts(capture):
    calls = capture()
    await tools.send_command_tool.ainvoke({"server_id": "s", "command": "say hi"})
    assert calls[0]["path"] == "/servers/s/command"
    assert calls[0]["json"] == {"command": "say hi"}


@pytest.mark.anyio
async def test_write_file_sends_raw_content(capture):
    calls = capture()
    await tools.write_file_tool.ainvoke({"server_id": "s", "file_path": "/server.properties", "content": "a=b"})
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/servers/s/files/write"
    assert calls[0]["params"] == {"file": "/server.properties"}
    assert calls[0]["content"] == "a=b"


@pytest.mark.anyio
async def test_rename_file_payload(capture):
    calls = capture()
    await tools.rename_file_tool.ainvoke({"server_id": "s", "from_path": "a.txt", "to_path": "b.txt"})
    assert calls[0]["method"] == "PUT"
    assert calls[0]["path"] == "/servers/s/files/rename"
    assert calls[0]["json"] == {"root": "/", "files": [{"from": "a.txt", "to": "b.txt"}]}


@pytest.mark.anyio
async def test_delete_file_payload(capture):
    calls = capture()
    await tools.delete_file_tool.ainvoke({"server_id": "s", "file_path": "world/session.lock"})
    assert calls[0]["path"] == "/servers/s/files/delete"
    assert calls[0]["json"] == {"root": "/", "files": ["world/session.lock"]}


@pytest.mark.anyio
async def test_create_backup_returns_uuid(capture):
    capture({"attributes": {"uuid": "u-1"}})
    result = await tools.create_backup_tool.ainvoke({"server_id": "s", "name": "pre-update"})
    assert "u-1" in result


@pytest.mark.anyio
async def test_restore_backup_path(capture):
    calls = capture()
    await tools.restore_backup_tool.ainvoke({"server_id": "s", "backup_uuid": "u-1"})
    assert calls[0]["path"] == "/servers/s/backups/u-1/restore"


@pytest.mark.anyio
async def test_delete_backup_uses_delete_method(capture):
    calls = capture()
    await tools.delete_backup_tool.ainvoke({"server_id": "s", "backup_uuid": "u-1"})
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["path"] == "/servers/s/backups/u-1"


@pytest.mark.anyio
async def test_update_startup_variable_payload(capture):
    capture({"attributes": {"server_value": "20"}})
    result = await tools.update_startup_variable_tool.ainvoke({"server_id": "s", "env_variable": "MAX_PLAYERS", "value": "20"})
    assert "MAX_PLAYERS=20" in result


@pytest.mark.anyio
async def test_mutating_tool_error_is_normalized(monkeypatch):
    monkeypatch.setattr(tools.PterodactylClient, "__init__", lambda self, config=None: None)

    async def _request(self, *a, **k):
        raise PterodactylAuthError("Authentication failed", status_code=403)

    monkeypatch.setattr(tools.PterodactylClient, "request", _request)
    result = await tools.power_action_tool.ainvoke({"server_id": "s", "signal": "stop"})
    assert result.startswith("Error")
