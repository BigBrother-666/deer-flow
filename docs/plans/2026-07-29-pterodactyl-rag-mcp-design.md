# Pterodactyl Plugin-Docs RAG — Design & Task List

- **Date:** 2026-07-29
- **Status:** Design (pending implementation)
- **Owner:** Pterodactyl integration
- **Related:** `backend/packages/harness/deerflow/community/pterodactyl/`, `skills/public/minecraft-server-ops/`, [[pterodactyl-skill-sync]]

## 1. Goal

Give the Pterodactyl agent a way to answer plugin-configuration questions from
authoritative plugin documentation instead of guessing or scraping the web
mid-task. A self-contained RAG module ingests plugin docs (split → embed →
index) and serves **retrieval tools over MCP** so the agent can look up how a
plugin behaves, what a config key does, or which command/permission node it
exposes.

## 2. Decisions (locked)

These three choices are settled and shape everything below.

| Axis | Decision | Why |
|------|----------|-----|
| **Delivery** | **Standalone MCP server** (separate process, stdio + http transport) | Keeps the vector/embedding dependency footprint out of the Gateway process; RAG reachable by any MCP client; consistent with DeerFlow's existing MCP extension surface (`extensions_config.json` → `mcpServers`). |
| **Vector store** | **pgvector, reusing the existing Postgres backend** (`database.postgres_url`) | Production already runs Postgres for checkpointer + app repos; one datastore, one backup story. The panel has backups disabled, but the RAG index is rebuildable from source docs, so this is acceptable. |
| **Ingestion source** | **Local files/folder** | Operator drops plugin docs (`.md`/`.txt`/`.html`/`.pdf`) into a directory; an offline `ingest` command splits, embeds, and upserts them. No live crawling in v1. |

Non-goals for v1: agent-driven live ingestion, URL crawling, multi-tenant
index isolation, re-ranking models, hybrid BM25+vector fusion. Called out in
§8 as future work.

## 3. Why MCP (and the honest tradeoff)

The sibling pterodactyl tools are **native community `@tool`s** loaded in-process.
A RAG retriever could have been the same. We deliberately chose MCP instead:

- **Pro:** embedding client + `pgvector`/`psycopg` + document parsers
  (`pypdf`, `beautifulsoup4`, chunkers) stay in a separate venv/process and
  never bloat the Gateway import graph or its blocking-IO surface.
- **Pro:** the index and its tools are reusable outside DeerFlow.
- **Con:** it sits behind the deferred-tool / `tool_search` promotion layer
  (see backend middleware chain items 24–25), not as first-class native tools.
  Mitigated by giving the server clear routing keywords so
  `McpRoutingMiddleware` auto-promotes it for plugin-doc questions.
- **Con:** one more process to run/deploy. Mitigated by stdio transport for
  local/dev (Gateway spawns it) and http for shared/prod deployments.

## 4. Architecture

```
                        ┌─────────────────────────────────────────────┐
   operator drops docs  │  deerflow-pterodactyl-rag  (separate package) │
   into docs_dir/  ───► │                                               │
                        │  ingest pipeline        query path (MCP srv)  │
   $ pterodactyl-rag    │  ┌──────────────┐       ┌──────────────────┐  │
     ingest  ─────────► │  │ loader        │      │ MCP server       │  │
                        │  │  → splitter   │      │ (FastMCP/stdio+  │  │
                        │  │  → embedder ──┼───┐  │  http)           │  │
                        │  │  → upsert     │   │  │  tools:          │  │
                        │  └──────┬────────┘   │  │  - rag_search    │  │
                        │         │            │  │  - rag_get_doc   │  │
                        │         ▼            │  │  - rag_list_srcs │  │
                        │   ┌───────────────┐  │  │  - rag_stats     │  │
                        │   │ VectorStore   │◄─┘  └────────┬─────────┘  │
                        │   │  (pgvector)   │◄────────────┘            │
                        │   └──────┬────────┘                          │
                        └──────────┼───────────────────────────────────┘
                                   ▼
                        Postgres (existing database.postgres_url)
                        schema: pterodactyl_rag.*   (isolated schema)

   Gateway agent ──(MCP stdio/http)──► rag_search ──► pgvector similarity ──► chunks
```

### 4.1 Module location & packaging

New standalone package under the backend, sibling to the harness package so it
has its own dependency set and does **not** violate the harness→app import
firewall (it imports neither):

```
backend/packages/pterodactyl-rag/
├── pyproject.toml                 # name: deerflow-pterodactyl-rag; own deps
├── README.md
├── src/pterodactyl_rag/
│   ├── __init__.py
│   ├── config.py                  # env-driven settings (DSN, embedding, dirs)
│   ├── models.py                  # Document, Chunk, SearchHit dataclasses
│   ├── loaders.py                 # local file discovery + parse (md/txt/html/pdf)
│   ├── splitter.py                # chunking (token-aware, heading-aware for md)
│   ├── embeddings.py              # OpenAI-compatible embedding client
│   ├── store/
│   │   ├── __init__.py
│   │   ├── base.py                # VectorStore protocol
│   │   └── pgvector_store.py      # schema DDL, upsert, similarity search
│   ├── pipeline.py                # ingest orchestration (load→split→embed→upsert)
│   ├── retriever.py               # query embed → search → assemble hits
│   ├── server.py                  # MCP server: tool defs + transport wiring
│   └── cli.py                     # `pterodactyl-rag ingest|serve|stats|reset`
└── tests/
    ├── test_splitter.py
    ├── test_pipeline.py           # fake embedder + in-memory/sqlite store
    ├── test_retriever.py
    └── test_server_tools.py       # MCP tool contract tests
```

Rationale for a separate package rather than `deerflow/community/pterodactyl_rag/`:
the deps (`psycopg[binary]`, `pgvector`, `pypdf`, `beautifulsoup4`, `mcp`)
should not enter `deerflow-harness`. The Gateway only ever talks to it over the
MCP wire, so there is no code import between them.

### 4.2 Data model (Postgres, isolated schema `pterodactyl_rag`)

```sql
CREATE SCHEMA IF NOT EXISTS pterodactyl_rag;
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector

-- One row per source document (a file on disk).
CREATE TABLE pterodactyl_rag.documents (
    id            BIGSERIAL PRIMARY KEY,
    source_path   TEXT NOT NULL UNIQUE,     -- relative path under docs_dir
    title         TEXT,
    content_hash  TEXT NOT NULL,            -- sha256 of raw file; skip re-embed if unchanged
    tags          TEXT[] NOT NULL DEFAULT '{}',  -- namespaced tags; see §4.2.1
    mtime         DOUBLE PRECISION,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per chunk; embedding dim is fixed at ingest time by the model.
CREATE TABLE pterodactyl_rag.chunks (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES pterodactyl_rag.documents(id) ON DELETE CASCADE,
    chunk_index   INT NOT NULL,
    heading_path  TEXT,                     -- e.g. "Config > permissions" for citation
    content       TEXT NOT NULL,
    token_count   INT,
    tags          TEXT[] NOT NULL DEFAULT '{}',  -- copied from the parent document (denormalized, see below)
    embedding     vector(1536) NOT NULL,    -- dim from config; see §4.4
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX ON pterodactyl_rag.chunks
    USING hnsw (embedding vector_cosine_ops);
-- GIN index over the tag array so a single index covers every tag namespace.
CREATE INDEX ON pterodactyl_rag.chunks USING gin (tags);
```

**Why `tags` is denormalized onto `chunks`:** the vector lives on `chunks`, so
the tag filter must live there too. A combined
`WHERE tags @> ARRAY['plugin:essentialsx'] ORDER BY embedding <=> $1` then runs
as one scan with no join back to `documents`. Tags are the parent document's
tags, copied at ingest time.

Idempotent re-ingest: compare `content_hash`; unchanged docs are skipped, changed
docs have their chunks (and copied tags) replaced (delete-by-`document_id` then
re-insert), removed files are pruned. Dimension is pinned per index — changing
the embedding model requires a `reset` + full re-ingest (validated at startup,
§4.4).

### 4.2.1 Tag design (namespaced array)

Instead of one column per facet, tags are a single `TEXT[]` of **namespaced**
values (`namespace:value`), covered by one GIN index. Adding a new facet never
changes the schema — it is just a new prefix. Filtering is
`tags @> ARRAY['plugin:essentialsx']` (AND) or an overlap test for OR.

Recommended namespaces:

| Namespace | Meaning | Source | Cardinality | Example |
|-----------|---------|--------|-------------|---------|
| `plugin:` | Plugin name — the primary filter | top-level folder name (auto) | open / growing | `plugin:essentialsx` |
| `category:` | Doc type | sub-folder / frontmatter | **closed set** | `category:config`, `category:permissions`, `category:commands`, `category:faq` |
| `platform:` | Server platform | frontmatter / sidecar | **closed set** | `platform:paper`, `platform:spigot`, `platform:fabric` |
| `mc:` | Minecraft version | frontmatter / sidecar | small | `mc:1.21` |
| `lang:` | Document language | auto-detect / frontmatter | **closed set** | `lang:en`, `lang:zh` |
| *(free)* | Arbitrary keywords | frontmatter | open | `economy`, `permission-node` |

**Why this matters most for precision, not speed.** With plugin-doc volumes
(thousands–tens of thousands of chunks) pure vector search stays fast for a long
time. The main win from tags is *recall correctness* — a question about
EssentialsX must not surface WorldGuard chunks. Note that HNSW + a hard `WHERE`
can drop recall when the filter is very selective (pgvector 0.8 iterative scans
help); true scale-out is per-plugin partitioning, deferred to §8.

**Tag sources — automatic first, manual override second (no agent involvement at
ingest):**

1. **Path inference (zero-config).** `docs_dir/EssentialsX/config/x.md` →
   `plugin:essentialsx` + `category:config`. First path segment = plugin,
   second (if it matches a known category) = category.
2. **Frontmatter / sidecar override.** A markdown YAML frontmatter block, or a
   `.rag.yaml` in a folder, supplies or corrects tags:
   ```yaml
   ---
   tags: [platform:paper, mc:1.21]
   plugin: EssentialsX      # explicit override of the inferred plugin
   ---
   ```

**Normalization (applied at ingest).** All tags are lowercased, trimmed,
hyphen-normalized, de-duplicated, and capped (count per doc + length per tag),
so `EssentialsX`, `essentials x`, and `Essentials-X` all collapse to
`plugin:essentialsx`. This is what lets the *query side* be forgiving (§4.5.1).

### 4.3 Ingestion pipeline

1. **Load** — walk `docs_dir` recursively. Parse by extension:
   - `.md`/`.markdown`/`.txt` → text as-is (markdown keeps heading structure).
   - `.html`/`.htm` → `beautifulsoup4` text extraction (strip nav/script).
   - `.pdf` → `pypdf` page text.
   - **Tags resolved here** (§4.2.1): path inference + frontmatter/sidecar
     override, then normalization. The agent is never involved at ingest.
2. **Split** — token-aware chunking with overlap (defaults: ~800 tokens,
   120 overlap). Markdown splitter is heading-aware so `heading_path` is
   preserved for citations; other formats fall back to a recursive character
   splitter.
3. **Embed** — batch chunk texts through an OpenAI-compatible embeddings
   endpoint. Batching + retry/backoff on 429/5xx.
4. **Upsert** — write `documents` + `chunks` transactionally per document;
   copy the document's normalized `tags` onto every chunk row (§4.2).

Runs via CLI (`pterodactyl-rag ingest --docs-dir ...`) — an offline operator
step, not on the request path.

### 4.4 Embeddings & config

Env-driven (the server runs as its own process; no `config.yaml` coupling):

| Env var | Meaning | Default |
|---------|---------|---------|
| `PTERO_RAG_DATABASE_URL` | Postgres DSN (reuse `$DATABASE_URL`) | required |
| `PTERO_RAG_DOCS_DIR` | Local docs folder to ingest | required for `ingest` |
| `PTERO_RAG_EMBED_BASE_URL` | OpenAI-compatible embeddings base URL | — |
| `PTERO_RAG_EMBED_API_KEY` | API key (`$VAR`, never inline) | — |
| `PTERO_RAG_EMBED_MODEL` | Embedding model name | `text-embedding-3-small` |
| `PTERO_RAG_EMBED_DIM` | Vector dimension (must match model) | `1536` |
| `PTERO_RAG_CHUNK_TOKENS` / `_OVERLAP` | Chunk sizing | `800` / `120` |
| `PTERO_RAG_TOP_K` | Default hits returned | `5` |
| `PTERO_RAG_TRANSPORT` | `stdio` or `http` | `stdio` |

Startup validation: the server reads the stored index dim from a tiny
`pterodactyl_rag.meta` row and refuses to serve if it disagrees with
`PTERO_RAG_EMBED_DIM`/model — prevents silently mixing embedding spaces.

### 4.5 MCP server & tools

Built with the `mcp` Python SDK (FastMCP). Read-only tools only (ingest is
CLI-side), so no HITL gate is needed. Tools:

- **`rag_search`** `(query: str, top_k: int = 5, plugin: str | None = None,
  category: Literal["config","permissions","commands","faq"] | None = None,
  platform: str | None = None, lang: str | None = None,
  tags: list[str] | None = None)`
  → JSON list of hits `{plugin, tags, title, heading_path, source_path,
  chunk_index, score, snippet}`, **plus a `filter` echo block**
  `{applied: [...], relaxed: bool, note}` so the model sees which tags actually
  filtered and whether the server fell back (§4.5.1). The closed-set facets
  (`category`, `lang`) are **enums in the schema** so the model sees legal
  values without guessing; open facets (`plugin`, free `tags`) are strings the
  server fuzzy-normalizes. Snippets are bounded.
- **`rag_get_document`** `(source_path: str, max_chars: int = 8000)`
  → full (bounded) reconstructed text of one source doc for deeper reading
  after a search hit, mirroring the pterodactyl `read_file` truncation pattern.
- **`rag_list_sources`** `(plugin: str | None = None)`
  → indexed documents with plugin/title/chunk counts, so the agent can see
  what coverage exists before asking.
- **`rag_list_facets`** `(namespace: str | None = None)` → the **discovery
  tool**: the actual tag values present in the index, grouped by namespace, with
  document counts (e.g. `{"plugin": [{"value": "essentialsx", "docs": 12}, ...],
  "category": [...]}`). This is how the agent learns the real plugin names to
  filter by instead of inventing them (§4.5.1).
- **`rag_stats`** `()` → index health: doc count, chunk count, embedding
  model/dim, last ingest time.

### 4.5.1 How the agent uses tags (the real problem)

Tags are cheap to store; the hard part is the agent picking the *right* filter
value at query time. A wrong hard filter is worse than no filter — it returns
nothing. The design follows what production RAG systems converge on:

1. **Discover, don't guess (mirrors the existing pterodactyl pattern).** Just as
   the agent calls `list_servers` to get a real `server_id` before acting, it
   calls **`rag_list_facets`** to get real plugin names before filtering. It
   selects `plugin:essentialsx` from the tool's returned values rather than
   recalling whether it's `essentialsx` or `essential-x`.
2. **Closed sets are enums in the schema.** `category`/`lang` (and optionally
   `platform`) are fixed value sets, so they're declared as enums on
   `rag_search`. The model sees the legal values in the tool definition and
   never needs a discovery round-trip or a guess for them.
3. **Soft filtering — the server never hard-fails on a bad tag.** If a tag
   filter yields 0 hits, the server **automatically retries without the filter**
   and returns the vector-only results flagged `filter.relaxed = true` with a
   note. A wrong guess degrades to "a bit noisier," not "empty." This is
   metadata-*aware* retrieval, not metadata-*mandatory*.
4. **Server-side fuzzy normalization.** The agent may pass `plugin="EssentialsX"`
   or `"essentials x"`; the server normalizes and fuzzy-matches to the canonical
   `plugin:essentialsx`. The burden of remembering exact tags lives on the
   server, not the LLM.

This is deliberately lighter than LangChain's `SelfQueryRetriever` (which uses an
extra LLM step to translate a natural-language query into a metadata filter from
a field-schema description). For this single, well-scoped domain, discovery tool
+ schema enums + soft filtering is cheaper and more predictable. Self-query is
noted as future work (§8) if free-form multi-facet filtering is ever needed.

The **`minecraft-server-ops` skill** must encode this workflow explicitly so the
agent follows it: *"To look up plugin docs, first call `rag_list_facets` to
confirm the plugin name, then `rag_search` with that `plugin=`; do not hand-craft
tag strings."* (Task T16; keeps the [[pterodactyl-skill-sync]] invariant.)

### 4.6 Wiring into DeerFlow

Registered in `extensions_config.json` under `mcpServers` — no harness code
change required. Example (stdio; Gateway spawns the process):

```json
{
  "mcpServers": {
    "pterodactyl_rag": {
      "enabled": true,
      "type": "stdio",
      "command": "pterodactyl-rag",
      "args": ["serve"],
      "env": {
        "PTERO_RAG_DATABASE_URL": "$DATABASE_URL",
        "PTERO_RAG_EMBED_API_KEY": "$OPENAI_API_KEY"
      },
      "tool_call_timeout": 60,
      "description": "Retrieval over Pterodactyl/Minecraft plugin documentation",
      "routing": {
        "mode": "prefer",
        "priority": 60,
        "keywords": ["plugin", "插件", "config", "permission", "EssentialsX",
                     "documentation", "how to configure", "command node"]
      }
    }
  }
}
```

The `routing.keywords` let `McpRoutingMiddleware` auto-promote `rag_search` when
a request looks like a plugin-doc question, so the agent reaches for docs before
guessing. An http transport variant is documented for shared deployments.

## 5. Failure modes & safety

- **DB unreachable / pgvector missing** → tools return a recoverable
  `Error: ...` string (mirrors pterodactyl tool convention) rather than raising.
- **Empty index** → `rag_search` returns `[]` with a note to run `ingest`.
- **Embedding API failure** → retry/backoff; surface a recoverable error.
- **Secrets** → DSN and API key only via `$VAR`; never logged. Reference by
  key name, not value.
- **Dim mismatch** → hard fail at startup (§4.4), never serve wrong-space hits.
- **Untrusted doc content** → retrieved snippets are data, not instructions;
  the Gateway's `ToolResultSanitizationMiddleware` scope is name-based, so add
  `rag_search`/`rag_get_document` to that allowlist (see task 8) so fetched
  doc text cannot forge framework tags.

## 6. Testing

Backend TDD is mandatory (per `backend/AGENTS.md`). All tests use a **fake
embedder** (deterministic vectors) and either a transaction-rolled-back Postgres
fixture or a SQLite/in-memory `VectorStore` implementation of the `base.py`
protocol, so no live API or DB is required in CI.

- `test_splitter.py` — chunk sizing, overlap, markdown heading paths.
- `test_pipeline.py` — idempotent re-ingest (hash skip), change replacement,
  deleted-file prune.
- `test_retriever.py` — top_k ordering, plugin filter, snippet bounding.
- `test_server_tools.py` — MCP tool JSON contracts + recoverable-error strings.

## 7. Documentation updates (required, same change set)

- `backend/packages/pterodactyl-rag/README.md` — setup, ingest, serve, env vars.
- `AGENTS.md` (root) — repo map entry for the new package.
- `backend/AGENTS.md` — note the RAG MCP server + its `ToolResultSanitization`
  allowlist entry.
- `config.example.yaml` / `extensions_config.example.json` — commented
  `pterodactyl_rag` MCP server block.
- `skills/public/minecraft-server-ops/SKILL.md` — teach the agent to consult
  `rag_search` for plugin config/permission questions (per [[pterodactyl-skill-sync]]).

## 8. Future work (out of v1 scope)

- Agent-driven ingest tool (add docs encountered mid-task).
- URL/doc-site crawling (reuse fetch/scrape community tools).
- Hybrid BM25 + vector retrieval and a re-ranking stage.
- Per-plugin/version index **partitioning** — the real scale-out lever once tag
  pre-filtering is no longer enough (§4.2.1).
- `SelfQueryRetriever`-style LLM query→filter translation if free-form
  multi-facet filtering is ever needed beyond the enum + discovery approach
  (§4.5.1).

## 9. Task list

### Phase 1 — Package skeleton & config
- [ ] **T1** Create `backend/packages/pterodactyl-rag/` package (`pyproject.toml`,
  deps: `mcp`, `psycopg[binary]`, `pgvector`, `pypdf`, `beautifulsoup4`,
  `httpx`, `tiktoken`; `requires-python >=3.12`). Console script `pterodactyl-rag`.
- [ ] **T2** `config.py` — env-driven settings + validation (§4.4).
- [ ] **T3** `models.py` — `Document`, `Chunk`, `SearchHit` dataclasses
  (each carries `tags: list[str]`).

### Phase 2 — Ingestion (TDD)
- [ ] **T4** `splitter.py` + `test_splitter.py` — token/heading-aware chunking.
- [ ] **T4b** `tags.py` + `test_tags.py` — namespaced-tag resolution
  (path inference + frontmatter/sidecar override) and normalization
  (lowercase/trim/hyphen/dedupe/cap) per §4.2.1.
- [ ] **T5** `loaders.py` — md/txt/html/pdf parsing + frontmatter extraction;
  wires in T4b tag resolution.
- [ ] **T6** `embeddings.py` — OpenAI-compatible batch embed client w/ retry.
- [ ] **T7** `store/base.py` + `store/pgvector_store.py` — schema DDL (incl.
  `tags TEXT[]` + GIN index), meta row, idempotent upsert copying tags onto
  chunks, **soft-filtered** cosine search (tag pre-filter with automatic
  no-filter fallback, §4.5.1), and a `list_facets` query. In-memory store impl
  for tests.
- [ ] **T8** `pipeline.py` + `test_pipeline.py` — load→split→embed→upsert;
  hash-skip, change-replace, prune, tag propagation. Fake embedder in tests.

### Phase 3 — Retrieval & MCP server (TDD)
- [ ] **T9** `retriever.py` + `test_retriever.py` — query embed → tag-filtered
  search with soft-fallback → bounded snippet assembly; server-side fuzzy tag
  normalization; `filter` echo block; top_k. Assert relaxed-fallback behavior.
- [ ] **T10** `server.py` — FastMCP server, **five tools** (§4.5) incl.
  `rag_list_facets` and enum facets on `rag_search`; stdio+http transport,
  recoverable-error strings, startup dim validation.
- [ ] **T11** `test_server_tools.py` — MCP tool contract + error-path tests;
  cover enum validation, discovery output, and soft-filter relaxation.
- [ ] **T12** `cli.py` — `ingest | serve | stats | reset` subcommands.

### Phase 4 — Integration & docs
- [ ] **T13** Add `rag_search`/`rag_get_document` to the Gateway
  `ToolResultSanitizationMiddleware` remote-content allowlist (§5).
- [ ] **T14** Add commented `pterodactyl_rag` block to
  `extensions_config.example.json` (+ http variant note).
- [ ] **T15** Package `README.md`; update root `AGENTS.md`, `backend/AGENTS.md`,
  `config.example.yaml`.
- [ ] **T16** Update `skills/public/minecraft-server-ops/SKILL.md` to use
  `rag_search`; keep [[pterodactyl-skill-sync]] invariant satisfied.
- [ ] **T17** End-to-end smoke: ingest a sample plugin doc set, run the server
  over stdio, verify the Gateway agent can `rag_search` and cite a config key.
```
