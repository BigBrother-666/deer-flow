"""Single source of truth for which Pterodactyl tools mutate server state.

``PterodactylGuardMiddleware`` reads ``MUTATING_TOOLS`` to decide which calls
require a human confirmation. Every new write tool MUST be registered here, or
it will bypass the human-in-the-loop gate. A meta-test asserts that every
non-read-only pterodactyl tool is listed.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

# Tool names that create / delete / modify server state and therefore require
# an explicit human confirmation before execution.
MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "pterodactyl_power_action",
        "pterodactyl_send_command",
        "pterodactyl_write_file",
        "pterodactyl_rename_file",
        "pterodactyl_delete_file",
        "pterodactyl_create_backup",
        "pterodactyl_restore_backup",
        "pterodactyl_delete_backup",
        "pterodactyl_update_startup_variable",
    }
)

# High-risk operations that always require a fresh confirmation and are never
# eligible for any relaxation of the single-consume rule.
HIGH_RISK_TOOLS: frozenset[str] = frozenset(
    {
        "pterodactyl_power_action",  # stop/kill can drop players
        "pterodactyl_delete_file",
        "pterodactyl_restore_backup",
        "pterodactyl_delete_backup",
    }
)

CONFIRM_MARKER_PREFIX = "PTERO-CONFIRM:"


def is_mutating(tool_name: str | None) -> bool:
    return tool_name in MUTATING_TOOLS


def confirmation_token(tool_name: str, args: dict[str, Any] | None) -> str:
    """Deterministic token binding a confirmation to one exact operation.

    Hashes the tool name together with its canonicalized arguments so a
    confirmation for "restart server X" cannot authorize "delete file Y", and
    changing any argument after confirmation invalidates the approval.
    """
    canonical = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    digest = sha256(f"{tool_name}|{canonical}".encode()).hexdigest()
    return digest[:12]


def confirm_marker(token: str) -> str:
    """The marker string the model must embed in its confirmation question."""
    return f"[{CONFIRM_MARKER_PREFIX}{token}]"
