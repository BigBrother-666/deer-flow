"""Pterodactyl Client API tools.

Read-only tools execute directly. Mutating tools (power/command/file/startup) are
listed in ``mutations.py`` and hard-gated by ``PterodactylGuardMiddleware`` — the
model must obtain an explicit human confirmation before they run.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from langchain.tools import tool

from .client import PterodactylClient
from .config import load_config
from .console import fetch_recent_console, run_command_capture
from .errors import PterodactylError

# Read-only query verbs allowed against downloaded SQLite databases.
_SQLITE_READONLY_PREFIXES = ("select", "pragma", "explain", "with")
_SQLITE_MAX_ROWS = 200


def _err(exc: PterodactylError) -> str:
    """Normalize a domain error into a recoverable, model-readable string."""
    detail = getattr(exc, "detail", None)
    suffix = f" ({detail})" if detail else ""
    return f"Error: {exc}{suffix}"


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


async def _get(path: str, *, params: dict[str, Any] | None = None, expect_json: bool = True) -> Any:
    return await PterodactylClient().request("GET", path, params=params, expect_json=expect_json)


async def _send(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    content: str | None = None,
    expect_json: bool = True,
) -> Any:
    return await PterodactylClient().request(method, path, params=params, json=json, content=content, expect_json=expect_json)


@tool("pterodactyl_list_servers", parse_docstring=True)
async def list_servers_tool(show_all: bool = False) -> str:
    """List Pterodactyl servers the configured API key can access.

    Returns a JSON array of servers with their identifier, name, node, and
    current state. Use the ``server_id`` field for other tools.

    Args:
        show_all: When True, list every server on the panel (the panel's
            "Show others' servers" toggle, ``type=admin-all``). Requires the API
            key to belong to a panel admin; non-admins get an empty list. Use
            this when the default listing is empty because the account does not
            directly own the servers.
    """
    params = {"type": "admin-all"} if show_all else None
    try:
        data = await _get("", params=params)
    except PterodactylError as exc:
        return _err(exc)
    servers = [
        {
            "server_id": item["attributes"]["identifier"],
            "name": item["attributes"]["name"],
            "node": item["attributes"].get("node"),
            "description": item["attributes"].get("description"),
        }
        for item in (data or {}).get("data", [])
    ]
    return _dump(servers)


@tool("pterodactyl_get_server", parse_docstring=True)
async def get_server_tool(server_id: str) -> str:
    """Get details and current status for a single server.

    Args:
        server_id: The server identifier (from pterodactyl_list_servers).
    """
    try:
        data = await _get(f"/servers/{server_id}")
    except PterodactylError as exc:
        return _err(exc)
    return _dump((data or {}).get("attributes", {}))


@tool("pterodactyl_get_resources", parse_docstring=True)
async def get_resources_tool(server_id: str) -> str:
    """Get live resource usage (CPU, memory, disk, network, uptime, state).

    Args:
        server_id: The server identifier.
    """
    try:
        data = await _get(f"/servers/{server_id}/resources")
    except PterodactylError as exc:
        return _err(exc)
    return _dump((data or {}).get("attributes", {}))


@tool("pterodactyl_list_files", parse_docstring=True)
async def list_files_tool(server_id: str, directory: str = "/") -> str:
    """List files and folders in a server directory.

    Args:
        server_id: The server identifier.
        directory: Absolute path within the server volume (default: "/").
    """
    try:
        data = await _get(f"/servers/{server_id}/files/list", params={"directory": directory})
    except PterodactylError as exc:
        return _err(exc)
    files = [
        {
            "name": item["attributes"]["name"],
            "is_file": item["attributes"]["is_file"],
            "size": item["attributes"].get("size"),
            "modified_at": item["attributes"].get("modified_at"),
        }
        for item in (data or {}).get("data", [])
    ]
    return _dump({"directory": directory, "entries": files})


@tool("pterodactyl_read_file", parse_docstring=True)
async def read_file_tool(server_id: str, file_path: str, max_chars: int = 20000) -> str:
    """Read the contents of a config or log file on the server.

    Args:
        server_id: The server identifier.
        file_path: Absolute path to the file within the server volume.
        max_chars: Truncate output to this many characters (default: 20000).
    """
    try:
        content = await _get(
            f"/servers/{server_id}/files/contents",
            params={"file": file_path},
            expect_json=False,
        )
    except PterodactylError as exc:
        return _err(exc)
    text = content if isinstance(content, str) else _dump(content)
    if len(text) > max_chars:
        return text[:max_chars] + f"\n... [truncated at {max_chars} chars]"
    return text


@tool("pterodactyl_get_startup", parse_docstring=True)
async def get_startup_tool(server_id: str) -> str:
    """Read the server's startup command and configurable startup variables.

    Args:
        server_id: The server identifier.
    """
    try:
        data = await _get(f"/servers/{server_id}/startup")
    except PterodactylError as exc:
        return _err(exc)
    variables = [
        {
            "name": item["attributes"].get("name"),
            "env_variable": item["attributes"].get("env_variable"),
            "server_value": item["attributes"].get("server_value"),
            "is_editable": item["attributes"].get("is_editable"),
        }
        for item in (data or {}).get("data", [])
    ]
    meta = (data or {}).get("meta", {})
    return _dump({"startup_command": meta.get("startup_command"), "variables": variables})


@tool("pterodactyl_read_console", parse_docstring=True)
async def read_console_tool(server_id: str, lines: int = 100) -> str:
    """Read the most recent console/log output lines from a running server.

    Connects to the panel's live console (websocket), replays the buffered
    history, and returns the last ``lines`` output lines. Useful for diagnosing
    crashes, errors, and startup issues without reading log files directly.

    Args:
        server_id: The server identifier.
        lines: How many of the most recent console lines to return (default: 100).
    """
    origin = load_config().panel_url.rstrip("/")
    try:
        collected = await fetch_recent_console(server_id, lines, origin=origin)
    except PterodactylError as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001 - websocket failures are recoverable
        return f"Error: could not read console for server {server_id}: {exc}"
    if not collected:
        return "(no console output received; the server may be offline)"
    return "\n".join(collected)


@tool("pterodactyl_read_file_lines", parse_docstring=True)
async def read_file_lines_tool(server_id: str, file_path: str, offset: int = 0, limit: int = 200) -> str:
    """Read a slice of a text file by line range (paginate large files).

    Fetches the file server-side but returns only lines ``offset`` .. ``offset+limit``
    so a large log/config never floods the context. Line numbers are 0-based.

    Args:
        server_id: The server identifier.
        file_path: Absolute path to the file within the server volume.
        offset: 0-based line index to start from (default: 0).
        limit: Maximum number of lines to return (default: 200).
    """
    try:
        content = await _get(
            f"/servers/{server_id}/files/contents",
            params={"file": file_path},
            expect_json=False,
        )
    except PterodactylError as exc:
        return _err(exc)
    text = content if isinstance(content, str) else _dump(content)
    all_lines = text.splitlines()
    total = len(all_lines)
    if offset < 0:
        offset = 0
    window = all_lines[offset : offset + max(limit, 0)]
    header = f"[lines {offset}-{offset + len(window)} of {total}]"
    return header + "\n" + "\n".join(window)


@tool("pterodactyl_search_file", parse_docstring=True)
async def search_file_tool(server_id: str, file_path: str, pattern: str, max_matches: int = 50, ignore_case: bool = True) -> str:
    """Search a text file for a keyword/regex and return only matching lines.

    Streams the file server-side and returns matching lines with 1-based line
    numbers — the full file is never dumped into the context. Use this to locate
    errors/keywords in large logs before reading a specific range.

    Args:
        server_id: The server identifier.
        file_path: Absolute path to the file within the server volume.
        pattern: Substring or regular expression to search for.
        max_matches: Maximum number of matching lines to return (default: 50).
        ignore_case: Case-insensitive matching (default: True).
    """
    try:
        content = await _get(
            f"/servers/{server_id}/files/contents",
            params={"file": file_path},
            expect_json=False,
        )
    except PterodactylError as exc:
        return _err(exc)
    text = content if isinstance(content, str) else _dump(content)
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return f"Error: invalid search pattern: {exc}"
    matches = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if regex.search(line):
            matches.append(f"{lineno}: {line}")
            if len(matches) >= max(max_matches, 1):
                break
    if not matches:
        return f"(no matches for {pattern!r} in {file_path})"
    return "\n".join(matches)


# ---------------------------------------------------------------------------
# Mutating tools — hard-gated by PterodactylGuardMiddleware. These require an
# explicit human confirmation (via ask_clarification) before they run.
# ---------------------------------------------------------------------------


@tool("pterodactyl_power_action", parse_docstring=True)
async def power_action_tool(server_id: str, signal: str) -> str:
    """Send a power signal to a server. REQUIRES HUMAN CONFIRMATION.

    Args:
        server_id: The server identifier.
        signal: One of "start", "stop", "restart", "kill". "kill" is a hard
            stop that can corrupt an unsaved world; prefer "stop"/"restart".
    """
    signal = signal.strip().lower()
    if signal not in {"start", "stop", "restart", "kill"}:
        return f"Error: invalid signal '{signal}' (expected start/stop/restart/kill)"
    try:
        await _send("POST", f"/servers/{server_id}/power", json={"signal": signal})
    except PterodactylError as exc:
        return _err(exc)
    return f"OK: sent '{signal}' to server {server_id}."


@tool("pterodactyl_send_command", parse_docstring=True)
async def send_command_tool(server_id: str, command: str, capture_output: bool = True, wait_seconds: float = 5.0) -> str:
    """Send a console command to a running server and capture its output. REQUIRES HUMAN CONFIRMATION.

    When ``capture_output`` is True (default) this opens the live console
    websocket, sends the command, captures the output it prints (bounded by a
    short idle/overall timeout), then closes the socket and returns that output —
    a single atomic connect → send → read → close. Set it to False for
    fire-and-forget (POST the command over REST without waiting for output).

    Args:
        server_id: The server identifier.
        command: The console command to execute (e.g. "list", "say hello", "op steve").
        capture_output: Capture and return the command's console output (default: True).
        wait_seconds: Max seconds to wait for output before returning (default: 5.0).
    """
    if not capture_output:
        try:
            await _send("POST", f"/servers/{server_id}/command", json={"command": command})
        except PterodactylError as exc:
            return _err(exc)
        return f"OK: command sent to server {server_id}: {command}"

    origin = load_config().panel_url.rstrip("/")
    try:
        lines = await run_command_capture(server_id, command, origin=origin, overall_timeout=wait_seconds)
    except PterodactylError as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001 - websocket failures are recoverable
        return f"Error: could not capture output for '{command}' on {server_id}: {exc}"
    if not lines:
        return f"OK: command sent to server {server_id}: {command}\n(no output captured within {wait_seconds:.0f}s)"
    return "\n".join(lines)


@tool("pterodactyl_write_file", parse_docstring=True)
async def write_file_tool(server_id: str, file_path: str, content: str) -> str:
    """Write (create or overwrite) a file on the server. REQUIRES HUMAN CONFIRMATION.

    Args:
        server_id: The server identifier.
        file_path: Absolute path to write within the server volume.
        content: Full file contents (overwrites any existing file).
    """
    try:
        await _send(
            "POST",
            f"/servers/{server_id}/files/write",
            params={"file": file_path},
            content=content,
            expect_json=False,
        )
    except PterodactylError as exc:
        return _err(exc)
    return f"OK: wrote {len(content)} chars to {file_path} on server {server_id}."


@tool("pterodactyl_rename_file", parse_docstring=True)
async def rename_file_tool(server_id: str, from_path: str, to_path: str, root: str = "/") -> str:
    """Rename or move a file/folder on the server. REQUIRES HUMAN CONFIRMATION.

    Args:
        server_id: The server identifier.
        from_path: Existing path (relative to root).
        to_path: New path (relative to root).
        root: Base directory both paths are relative to (default: "/").
    """
    payload = {"root": root, "files": [{"from": from_path, "to": to_path}]}
    try:
        await _send("PUT", f"/servers/{server_id}/files/rename", json=payload)
    except PterodactylError as exc:
        return _err(exc)
    return f"OK: renamed {from_path} -> {to_path} on server {server_id}."


@tool("pterodactyl_delete_file", parse_docstring=True)
async def delete_file_tool(server_id: str, file_path: str, root: str = "/") -> str:
    """Delete a file or folder on the server. REQUIRES HUMAN CONFIRMATION.

    Args:
        server_id: The server identifier.
        file_path: Path to delete (relative to root).
        root: Base directory the path is relative to (default: "/").
    """
    payload = {"root": root, "files": [file_path]}
    try:
        await _send("POST", f"/servers/{server_id}/files/delete", json=payload)
    except PterodactylError as exc:
        return _err(exc)
    return f"OK: deleted {file_path} on server {server_id}."


@tool("pterodactyl_update_startup_variable", parse_docstring=True)
async def update_startup_variable_tool(server_id: str, env_variable: str, value: str) -> str:
    """Update an editable startup (environment) variable. REQUIRES HUMAN CONFIRMATION.

    Args:
        server_id: The server identifier.
        env_variable: The env variable key (e.g. "SERVER_JARFILE", "MAX_PLAYERS").
        value: New value for the variable.
    """
    payload = {"key": env_variable, "value": value}
    try:
        data = await _send("PUT", f"/servers/{server_id}/startup/variable", json=payload)
    except PterodactylError as exc:
        return _err(exc)
    current = (data or {}).get("attributes", {}).get("server_value", value)
    return f"OK: set {env_variable}={current} on server {server_id}."


# ---------------------------------------------------------------------------
# Read-only download / SQLite tools — these never inline full file contents.
# ---------------------------------------------------------------------------


def _resolve_workspace_dir(runtime: Any) -> Path:
    """Resolve the current thread's sandbox workspace host dir for downloads."""
    from deerflow.config.paths import get_paths
    from deerflow.runtime.user_context import get_effective_user_id, resolve_runtime_user_id

    thread_id = None
    if runtime is not None:
        ctx = getattr(runtime, "context", None) or {}
        thread_id = ctx.get("thread_id")
        if not thread_id:
            cfg = getattr(runtime, "config", None) or {}
            thread_id = (cfg.get("configurable") or {}).get("thread_id")
    if not thread_id:
        raise PterodactylError("No active thread; cannot resolve a download directory.")
    user_id = (resolve_runtime_user_id(runtime) if runtime is not None else None) or get_effective_user_id()
    workspace = get_paths().sandbox_workspace_dir(thread_id, user_id=user_id)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


@tool("pterodactyl_download_file", parse_docstring=True)
async def download_file_tool(server_id: str, file_path: str, runtime: Any = None) -> str:
    """Download a server file into the workspace WITHOUT reading it into context.

    Streams the file (including binary/DB files) to
    ``/mnt/user-data/workspace/`` and returns its local path, size, and sha256.
    Use this for databases and large files: download here, then inspect with
    ``pterodactyl_query_sqlite`` or the sandbox's own file/search tools instead
    of loading the whole file into the model context.

    Args:
        server_id: The server identifier.
        file_path: Absolute path to the file within the server volume.
    """
    from hashlib import sha256

    try:
        workspace = _resolve_workspace_dir(runtime)
    except PterodactylError as exc:
        return _err(exc)
    dest = workspace / Path(file_path).name
    hasher = sha256()

    class _Sink:
        def __init__(self, fh):
            self._fh = fh

        def write(self, chunk: bytes) -> None:
            hasher.update(chunk)
            self._fh.write(chunk)

    try:
        with dest.open("wb") as fh:
            written = await PterodactylClient().download(
                f"/servers/{server_id}/files/download",
                _Sink(fh),
                params={"file": file_path},
            )
    except PterodactylError as exc:
        return _err(exc)
    virtual = f"/mnt/user-data/workspace/{dest.name}"
    return _dump({"path": virtual, "bytes": written, "sha256": hasher.hexdigest()})


def _run_sqlite_query(db_path: str, query: str, params: list[Any] | None) -> dict[str, Any]:
    """Execute a read-only query against a local SQLite file (blocking)."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query, params or [])
        rows = cur.fetchmany(_SQLITE_MAX_ROWS + 1)
        columns = [d[0] for d in cur.description] if cur.description else []
        truncated = len(rows) > _SQLITE_MAX_ROWS
        data = [dict(row) for row in rows[:_SQLITE_MAX_ROWS]]
        return {"columns": columns, "rows": data, "truncated": truncated}
    finally:
        conn.close()


@tool("pterodactyl_query_sqlite", parse_docstring=True)
async def query_sqlite_tool(server_id: str, file_path: str, query: str) -> str:
    """Run a read-only SQL query against a SQLite database file on the server.

    Downloads the ``.db``/``.sqlite`` file to a temporary location and executes a
    single read-only query (SELECT/PRAGMA/EXPLAIN/WITH) against it, returning up
    to 200 rows as JSON. The database itself never enters the context. Writes
    are rejected and the DB is opened read-only.

    Args:
        server_id: The server identifier.
        file_path: Absolute path to the SQLite database within the server volume.
        query: A single read-only SQL statement (SELECT/PRAGMA/EXPLAIN/WITH).
    """
    stripped = query.strip().rstrip(";").strip()
    if ";" in stripped:
        return "Error: only a single SQL statement is allowed."
    if not stripped.lower().startswith(_SQLITE_READONLY_PREFIXES):
        return "Error: only read-only queries (SELECT/PRAGMA/EXPLAIN/WITH) are allowed."

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=True) as tmp:
        try:
            await PterodactylClient().download(
                f"/servers/{server_id}/files/download",
                tmp,
                params={"file": file_path},
            )
            tmp.flush()
        except PterodactylError as exc:
            return _err(exc)
        try:
            result = await asyncio.to_thread(_run_sqlite_query, tmp.name, stripped, None)
        except sqlite3.Error as exc:
            return f"Error: SQLite query failed: {exc}"
    return _dump(result)
