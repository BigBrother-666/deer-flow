# Pterodactyl integration

Native DeerFlow tools wrapping the Pterodactyl **Client API** so the agent can
operate existing game servers (built for Minecraft maintenance: diagnose, fix,
manage plugins). All state-changing operations require human confirmation.

## Layout

| File | Purpose |
|------|---------|
| `config.py` | Resolves `panel_url` / `api_key` / `timeout` from the `pterodactyl` tool group. |
| `client.py` | Async httpx client: auth, timeout, retry on 429/5xx, error normalization. |
| `errors.py` | Typed domain errors → recoverable tool-error strings. |
| `console.py` | Wings websocket access: recent-console replay and send-command-and-capture-output (both bounded by idle/overall timeouts). |
| `tools.py` | 11 read-only + 6 mutating `@tool`s. |
| `mutations.py` | Single source of truth for which tools mutate + confirmation-token logic. |
| `guard.py` | `PterodactylGuardMiddleware`: HITL hard gate for mutating tools. |

## Configuration

1. Add the tool group and tools in `config.yaml` (see the commented
   `pterodactyl` blocks in `config.example.yaml`). Provide credentials via env:

   ```yaml
   tool_groups:
     - name: pterodactyl
       panel_url: $PTERODACTYL_PANEL_URL   # https://panel.example.com
       api_key: $PTERODACTYL_API_KEY       # Client API key (ptlc_...)
       timeout: 30
   ```

2. Enable the guard middleware in `extensions_config.json`:

   ```json
   { "middlewares": ["deerflow.community.pterodactyl.guard:PterodactylGuardMiddleware"] }
   ```

Never inline the API key — always use `$VAR`. Use a permission-scoped Client key.

## Reading logs, big files, and databases

These tools keep large/binary content out of the model context:

- `pterodactyl_read_console` — last N console lines via the Wings websocket
  (no REST endpoint exists for console history; the tool connects, replays the
  buffered history, then closes on an idle timeout).
- `pterodactyl_send_command` (mutating, gated) — with `capture_output=True`
  (default) it runs one atomic connect → auth → send command → capture the
  output the command prints → close, and returns that output. Only output
  produced after the command is captured (history is not replayed), so the
  result reflects this command's response. `capture_output=False` falls back to
  fire-and-forget REST.
- `pterodactyl_read_file_lines` — read a file by `offset`/`limit` line range to
  paginate large logs/configs instead of dumping the whole file.
- `pterodactyl_search_file` — keyword/regex search that returns only matching
  lines (with line numbers).
- `pterodactyl_download_file` — streams any file (including binary/DB) into
  `/mnt/user-data/workspace/` and returns only its path/size/sha256; the bytes
  never enter the context. Inspect it afterward with the sandbox's own tools.
- `pterodactyl_query_sqlite` — downloads a `.db`/`.sqlite` to a temp file and
  runs one read-only query (SELECT/PRAGMA/EXPLAIN/WITH, opened `mode=ro`,
  capped at 200 rows). The database itself is never inlined.

## HITL model

A mutating tool call is BLOCKED until the message history contains an
affirmative `ask_clarification` reply whose question embedded that operation's
`[PTERO-CONFIRM:<token>]` marker, and no mutating tool has executed since
(single-consume). The token is a hash of `(tool_name, args)`, so a confirmation
for one operation cannot authorize a different one. See the
`minecraft-server-ops` skill for the agent-facing workflow.

## Tests

```
uv run pytest tests/test_pterodactyl_*.py -q   # from backend/
```
