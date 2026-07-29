"""Console access over the Pterodactyl (Wings) websocket.

The Client API has no REST endpoint for console history or command output; both
are only available over the Wings websocket. This module obtains a short-lived
socket token via the Client API, opens the socket, authenticates, performs one
action (replay history, or send a command and capture what it prints), then
closes. Every path is bounded by idle + overall timeouts so a tool call always
terminates and never leaks an open socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from .client import PterodactylClient
from .errors import PterodactylAPIError

logger = logging.getLogger(__name__)

# Hard caps so a chatty server can never make the tool run unbounded.
DEFAULT_IDLE_TIMEOUT = 2.0
DEFAULT_OVERALL_TIMEOUT = 20.0
MAX_BUFFERED_LINES = 2000

# Factory type: given (socket_url, origin) return an async-context websocket.
ConnectFn = Callable[[str, str], AbstractAsyncContextManager[Any]]
# Called once after "auth success"; use it to send logs/command requests.
AfterAuth = Callable[[Any], Awaitable[None]]


async def _fetch_socket_details(server_id: str) -> tuple[str, str]:
    """Return ``(socket_url, token)`` for a server's Wings console websocket."""
    data = await PterodactylClient().request("GET", f"/servers/{server_id}/websocket")
    attrs = (data or {}).get("data") or (data or {}).get("attributes") or {}
    socket_url = attrs.get("socket")
    token = attrs.get("token")
    if not socket_url or not token:
        raise PterodactylAPIError("Panel did not return a websocket socket/token")
    return socket_url, token


def _default_connect(socket_url: str, origin: str) -> AbstractAsyncContextManager[Any]:
    """Default websocket connection factory (imported lazily so tests can patch)."""
    from websockets.asyncio.client import connect

    return connect(socket_url, origin=origin, open_timeout=DEFAULT_OVERALL_TIMEOUT)


async def _collect(
    ws: Any,
    token: str,
    *,
    after_auth: AfterAuth,
    idle_timeout: float,
    overall_timeout: float,
    wait_for_first: bool,
) -> list[str]:
    """Auth, run ``after_auth``, then gather console output lines until done.

    Stops on: overall deadline, buffer cap, token expiry, or an idle gap. When
    ``wait_for_first`` is True the idle gap only ends collection *after* at least
    one line arrived, so a command whose output is briefly delayed is not missed.
    """
    await ws.send(json.dumps({"event": "auth", "args": [token]}))

    lines: list[str] = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + overall_timeout

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0 or len(lines) >= MAX_BUFFERED_LINES:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(idle_timeout, remaining))
        except TimeoutError:
            if wait_for_first and not lines:
                continue  # nothing captured yet; keep waiting until overall deadline
            break  # idle: output has drained

        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue

        event = msg.get("event")
        if event == "auth success":
            await after_auth(ws)
        elif event in ("console output", "install output"):
            for arg in msg.get("args") or []:
                if isinstance(arg, str):
                    lines.extend(arg.splitlines() or [arg])
        elif event in ("token expiring", "token expired", "jwt error"):
            break
    return lines


async def _run(
    server_id: str,
    *,
    origin: str,
    after_auth: AfterAuth,
    connect_fn: ConnectFn | None,
    idle_timeout: float,
    overall_timeout: float,
    wait_for_first: bool,
) -> list[str]:
    """Open the socket, collect output for one action, and close."""
    socket_url, token = await _fetch_socket_details(server_id)
    connect = connect_fn or _default_connect
    async with connect(socket_url, origin) as ws:
        return await _collect(
            ws,
            token,
            after_auth=after_auth,
            idle_timeout=idle_timeout,
            overall_timeout=overall_timeout,
            wait_for_first=wait_for_first,
        )


async def fetch_recent_console(
    server_id: str,
    lines: int = 100,
    *,
    origin: str,
    connect_fn: ConnectFn | None = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    overall_timeout: float = DEFAULT_OVERALL_TIMEOUT,
) -> list[str]:
    """Return up to the last ``lines`` console output lines for a server."""

    async def _request_history(ws: Any) -> None:
        await ws.send(json.dumps({"event": "send logs", "args": [None]}))

    collected = await _run(
        server_id,
        origin=origin,
        after_auth=_request_history,
        connect_fn=connect_fn,
        idle_timeout=idle_timeout,
        overall_timeout=overall_timeout,
        wait_for_first=False,
    )
    return collected[-lines:] if lines > 0 else collected


async def run_command_capture(
    server_id: str,
    command: str,
    *,
    origin: str,
    connect_fn: ConnectFn | None = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    overall_timeout: float = DEFAULT_OVERALL_TIMEOUT,
    max_lines: int = 200,
) -> list[str]:
    """Send ``command`` over the socket and capture the output it prints.

    Connect → auth → send the command → collect output until idle/overall
    timeout → close. Only output produced after auth is captured (history is not
    replayed), so the result reflects this command's response.
    """

    async def _send_command(ws: Any) -> None:
        await ws.send(json.dumps({"event": "send command", "args": [command]}))

    collected = await _run(
        server_id,
        origin=origin,
        after_auth=_send_command,
        connect_fn=connect_fn,
        idle_timeout=idle_timeout,
        overall_timeout=overall_timeout,
        wait_for_first=True,
    )
    return collected[-max_lines:] if max_lines > 0 else collected
