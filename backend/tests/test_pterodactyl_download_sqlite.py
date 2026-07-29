"""Unit tests for pterodactyl download + sqlite tools (client/workspace mocked)."""

import json
import sqlite3

import pytest

from deerflow.community.pterodactyl import tools


@pytest.fixture
def no_client_init(monkeypatch):
    monkeypatch.setattr(tools.PterodactylClient, "__init__", lambda self, config=None: None)


@pytest.mark.anyio
async def test_download_file_writes_workspace_and_returns_meta(monkeypatch, tmp_path, no_client_init):
    monkeypatch.setattr(tools, "_resolve_workspace_dir", lambda runtime: tmp_path)

    async def _download(self, path, dest, *, params=None):
        dest.write(b"hello-bytes")
        return 11

    monkeypatch.setattr(tools.PterodactylClient, "download", _download)
    raw = await tools.download_file_tool.ainvoke({"server_id": "s", "file_path": "/data/world.db"})
    meta = json.loads(raw)
    assert meta["path"] == "/mnt/user-data/workspace/world.db"
    assert meta["bytes"] == 11
    assert len(meta["sha256"]) == 64
    assert (tmp_path / "world.db").read_bytes() == b"hello-bytes"


@pytest.mark.anyio
async def test_query_sqlite_returns_rows(monkeypatch, tmp_path, no_client_init):
    # Build a real sqlite db to serve as the "downloaded" file.
    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE players (name TEXT, score INT)")
    conn.executemany("INSERT INTO players VALUES (?, ?)", [("steve", 10), ("alex", 20)])
    conn.commit()
    conn.close()

    async def _download(self, path, dest, *, params=None):
        dest.write(src.read_bytes())
        return src.stat().st_size

    monkeypatch.setattr(tools.PterodactylClient, "download", _download)
    raw = await tools.query_sqlite_tool.ainvoke({"server_id": "s", "file_path": "/data.db", "query": "SELECT name, score FROM players ORDER BY score"})
    result = json.loads(raw)
    assert result["columns"] == ["name", "score"]
    assert result["rows"][0] == {"name": "steve", "score": 10}
    assert result["truncated"] is False


@pytest.mark.anyio
async def test_query_sqlite_rejects_writes(no_client_init):
    result = await tools.query_sqlite_tool.ainvoke({"server_id": "s", "file_path": "/data.db", "query": "DELETE FROM players"})
    assert result.startswith("Error: only read-only")


@pytest.mark.anyio
async def test_query_sqlite_rejects_multiple_statements(no_client_init):
    result = await tools.query_sqlite_tool.ainvoke({"server_id": "s", "file_path": "/data.db", "query": "SELECT 1; DROP TABLE players"})
    assert result.startswith("Error: only a single")
