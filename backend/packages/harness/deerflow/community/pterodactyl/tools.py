"""Pterodactyl Client API tools.

Read-only tools execute directly. Mutating tools (power/file/backup/startup) are
listed in ``mutations.py`` and hard-gated by ``PterodactylGuardMiddleware`` — the
model must obtain an explicit human confirmation before they run.
"""

from __future__ import annotations

import json
from typing import Any

from langchain.tools import tool

from .client import PterodactylClient
from .errors import PterodactylError


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


@tool("pterodactyl_list_backups", parse_docstring=True)
async def list_backups_tool(server_id: str) -> str:
    """List backups for a server.

    Args:
        server_id: The server identifier.
    """
    try:
        data = await _get(f"/servers/{server_id}/backups")
    except PterodactylError as exc:
        return _err(exc)
    backups = [
        {
            "uuid": item["attributes"]["uuid"],
            "name": item["attributes"]["name"],
            "is_successful": item["attributes"].get("is_successful"),
            "bytes": item["attributes"].get("bytes"),
            "created_at": item["attributes"].get("created_at"),
        }
        for item in (data or {}).get("data", [])
    ]
    return _dump(backups)


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
async def send_command_tool(server_id: str, command: str) -> str:
    """Send a console command to a running server. REQUIRES HUMAN CONFIRMATION.

    Args:
        server_id: The server identifier.
        command: The console command to execute (e.g. "say hello", "op steve").
    """
    try:
        await _send("POST", f"/servers/{server_id}/command", json={"command": command})
    except PterodactylError as exc:
        return _err(exc)
    return f"OK: command sent to server {server_id}: {command}"


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


@tool("pterodactyl_create_backup", parse_docstring=True)
async def create_backup_tool(server_id: str, name: str | None = None) -> str:
    """Create a new backup of the server. REQUIRES HUMAN CONFIRMATION.

    Args:
        server_id: The server identifier.
        name: Optional backup name.
    """
    payload: dict[str, Any] = {}
    if name:
        payload["name"] = name
    try:
        data = await _send("POST", f"/servers/{server_id}/backups", json=payload)
    except PterodactylError as exc:
        return _err(exc)
    uuid = (data or {}).get("attributes", {}).get("uuid")
    return f"OK: backup created for server {server_id} (uuid={uuid})."


@tool("pterodactyl_restore_backup", parse_docstring=True)
async def restore_backup_tool(server_id: str, backup_uuid: str) -> str:
    """Restore a server from a backup. REQUIRES HUMAN CONFIRMATION.

    This overwrites current server files with the backup's contents.

    Args:
        server_id: The server identifier.
        backup_uuid: UUID of the backup to restore (from pterodactyl_list_backups).
    """
    try:
        await _send("POST", f"/servers/{server_id}/backups/{backup_uuid}/restore", json={})
    except PterodactylError as exc:
        return _err(exc)
    return f"OK: restore started for server {server_id} from backup {backup_uuid}."


@tool("pterodactyl_delete_backup", parse_docstring=True)
async def delete_backup_tool(server_id: str, backup_uuid: str) -> str:
    """Delete a backup. REQUIRES HUMAN CONFIRMATION.

    Args:
        server_id: The server identifier.
        backup_uuid: UUID of the backup to delete.
    """
    try:
        await _send("DELETE", f"/servers/{server_id}/backups/{backup_uuid}")
    except PterodactylError as exc:
        return _err(exc)
    return f"OK: deleted backup {backup_uuid} on server {server_id}."


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
