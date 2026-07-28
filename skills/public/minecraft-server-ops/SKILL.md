---
name: minecraft-server-ops
description: Use this skill when maintaining, diagnosing, or fixing a Minecraft (or other game) server hosted on a Pterodactyl panel. Trigger on requests like "the server is lagging / crashed / won't start", "check server status", "restart the server", "read the crash log", "edit server.properties", "manage plugins", "back up the world", or any troubleshooting of a Pterodactyl-managed game server. Uses the pterodactyl_* tools; all state-changing actions require human confirmation.
---

# Minecraft Server Ops (Pterodactyl)

## Overview

This skill drives the `pterodactyl_*` tools to operate a game server on a
Pterodactyl panel: inspect state, read logs and configs, and — after explicit
human confirmation — perform power actions, edit files, manage backups, and
adjust startup variables.

Core principle: **diagnose from read-only evidence first, change the smallest
thing, verify, and only escalate to destructive actions with confirmation.**

## Human-in-the-loop (non-negotiable)

Mutating tools (`pterodactyl_power_action`, `pterodactyl_send_command`,
`pterodactyl_write_file`, `pterodactyl_rename_file`, `pterodactyl_delete_file`,
`pterodactyl_create_backup`, `pterodactyl_restore_backup`,
`pterodactyl_delete_backup`, `pterodactyl_update_startup_variable`) are
hard-gated. A call is BLOCKED until the user confirms.

When a mutating tool returns a `BLOCKED:` message containing a
`[PTERO-CONFIRM:<token>]` marker:
1. Call `ask_clarification` with `clarification_type="risk_confirmation"`.
2. State the exact operation (server, action, target file/args) and its impact.
3. Include the `[PTERO-CONFIRM:<token>]` marker **verbatim** in your question.
4. After the user confirms, re-issue the SAME tool with the SAME arguments.
5. If the user declines, do not retry — propose an alternative.

Never batch several destructive actions behind one confirmation; each mutating
call needs its own confirmed marker.

## Standard diagnostic workflow

1. **Locate the server.** `pterodactyl_list_servers` → pick the `server_id`.
   If the list is empty, the API key's account may not directly own the
   servers — retry with `pterodactyl_list_servers(show_all=True)` (the panel's
   "Show others' servers" view; needs an admin key).
2. **Check state & resources.** `pterodactyl_get_server` (status) and
   `pterodactyl_get_resources` (CPU / memory / disk / uptime). High memory near
   the limit or CPU pinned at 100% points at load or a leak.
3. **Read the evidence.** `pterodactyl_list_files` on `/logs`, then
   `pterodactyl_read_file` on `logs/latest.log` (and `crash-reports/` if the
   server died). Read before theorizing.
4. **Form one hypothesis**, state it, then act on the smallest fix.
5. **Verify** after any change: re-read the log / resources and confirm the
   symptom is gone.

## Common scenarios

### Server won't start
- Read `logs/latest.log` for the fatal line. Typical causes:
  - EULA not accepted → `eula.txt` must contain `eula=true`
    (`pterodactyl_write_file`, confirmed).
  - Port already in use / wrong `server-port` in `server.properties`.
  - Bad/incompatible plugin or mod → check the stack trace for the plugin name.
  - Wrong Java / jar → inspect `pterodactyl_get_startup` (SERVER_JARFILE).

### Crash or freeze under load
- `pterodactyl_get_resources`: if memory is at the cap, raise the heap via the
  startup variable (e.g. `MAX_MEMORY`) or reduce view-distance/entities in
  `server.properties`. Confirm the change, restart, verify.
- Look for `Can't keep up!` (TPS drop) or `OutOfMemoryError` in the log.

### Plugin conflict
- `pterodactyl_list_files` on `/plugins`. Correlate errors in the log to a
  plugin jar. To disable one, rename it to `*.jar.disabled`
  (`pterodactyl_rename_file`, confirmed), restart, and re-check.

### Config change (server.properties, ops, whitelist)
- Read the file, show the user the exact diff you intend, then
  `pterodactyl_write_file` the full new contents (confirmed). Restart if the
  setting is only read at boot.

### Before any risky change
- Take a backup first: `pterodactyl_create_backup` (confirmed) so a
  `pterodactyl_restore_backup` is available if the change goes wrong.

## Safety notes
- Prefer `restart` over `kill`; `kill` can corrupt an unsaved world.
- `restore_backup` overwrites current files — confirm the target backup UUID
  with the user and take a fresh backup first when feasible.
- Read a file before overwriting it; never blind-write configs.
