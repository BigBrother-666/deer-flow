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
| `tools.py` | 7 read-only + 9 mutating `@tool`s. |
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
