---
name: minecraft-server-ops
description: Use this skill when maintaining, diagnosing, or fixing a Minecraft (or other game) server hosted on a Pterodactyl panel. Trigger on requests like "the server is lagging / crashed / won't start", "check server status", "restart the server", "read the crash log", "run a console command", "edit server.properties", "manage plugins", or any troubleshooting of a Pterodactyl-managed game server. Uses the pterodactyl_* tools; all state-changing actions require human confirmation.
---

# Minecraft Server Ops (Pterodactyl)

## Overview

This skill drives the `pterodactyl_*` tools to operate a game server on a
Pterodactyl panel: inspect state, read logs and configs, and — after explicit
human confirmation — perform power actions, run console commands, edit files,
and adjust startup variables.

Core principle: **diagnose from read-only evidence first, change the smallest
thing, verify, and only escalate to destructive actions with confirmation.**

## Human-in-the-loop (non-negotiable)

Mutating tools (`pterodactyl_power_action`, `pterodactyl_send_command`,
`pterodactyl_write_file`, `pterodactyl_rename_file`, `pterodactyl_delete_file`,
`pterodactyl_update_startup_variable`) are hard-gated. A call is BLOCKED until
the user confirms.

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
3. **Read the evidence.** `pterodactyl_list_files` on `/logs`, then read
   `logs/latest.log` (and `crash-reports/` if the server died). Read before
   theorizing. Prefer targeted reads over dumping whole files:
   - `pterodactyl_read_console` — the live console's most recent lines (fastest
     for "what is it printing right now / why did it just crash").
   - `pterodactyl_search_file` — grep a large log for a keyword/stack-trace
     before reading around it.
   - `pterodactyl_read_file_lines` — read a specific line range of a big file.
   - `pterodactyl_read_file` — small configs read whole (truncates if huge).
4. **Inspect data/state when needed.**
   - `pterodactyl_send_command` (confirmed) — run a console command and read its
     output in one step (e.g. `list`, `tps`, `plugins`). This opens the console
     socket, sends the command, captures what it prints, and closes.
   - `pterodactyl_download_file` — pull a file (including binary/DB) into the
     workspace without loading it into context.
   - `pterodactyl_query_sqlite` — run a read-only `SELECT` against a downloaded
     `.db`/`.sqlite` (e.g. a plugin's data store) instead of reading raw bytes.
5. **Form one hypothesis**, state it, then act on the smallest fix.
6. **Verify** after any change: re-read the log / resources and confirm the
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
- This panel has backups disabled, so there is no automatic rollback. Before
  overwriting or deleting anything, preserve the current state yourself:
  `pterodactyl_download_file` the file(s) into the workspace (or copy them aside
  with `pterodactyl_rename_file`, confirmed) so you can restore by hand if the
  change goes wrong. Make the smallest reversible change possible.

## Safety notes
- Prefer `restart` over `kill`; `kill` can corrupt an unsaved world.
- No backups are available on this panel — never rely on a restore to undo a
  mistake. Save a copy of any file before overwriting or deleting it.
- Read a file before overwriting it; never blind-write configs.
