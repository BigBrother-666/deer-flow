# pterodactyl-rag MCP server — standalone image (dev + prod stages)
#
# Self-contained: build context is this package directory (backend/packages/
# pterodactyl-rag). It deliberately does NOT touch backend/Dockerfile — the heavy
# embedding / vector / document-parsing deps stay out of the Gateway image, and
# DeerFlow only ever reaches this server over the MCP wire.
#
# Two build targets mirror DeerFlow's own convention (backend/Dockerfile):
#   - dev     (make docker-start): source is bind-mounted at runtime and the
#             process is wrapped in `watchfiles` for hot-reload. The dev group
#             (incl. watchfiles) is installed. Startup runs `uv sync` to
#             self-heal the .venv named volume, mirroring docker/dev-entrypoint.sh.
#   - runtime (make up): code is COPIED into the image and frozen with
#             `uv sync --frozen --no-dev`; no source mount, no watcher.
#
# Build (from the package directory, or via `context:` in docker-compose.rag.yaml):
#   docker build --target runtime -t deerflow-pterodactyl-rag .
#   docker build --target dev     -t deerflow-pterodactyl-rag:dev .
# Restricted networks:
#   docker build --build-arg APT_MIRROR=mirrors.aliyun.com \
#     --build-arg UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple ...

# UV source image (override for restricted networks that cannot reach ghcr.io).
# Pinned to the same uv version as backend/Dockerfile for consistency.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.7.20
FROM ${UV_IMAGE} AS uv-source

# ── Base: python + uv + locale, shared by both dev and runtime ──────────────
FROM python:3.12-slim-bookworm AS base

# Optionally override apt mirror / PyPI index for restricted networks
# (mirrors the backend/Dockerfile convention).
ARG APT_MIRROR
ARG UV_INDEX_URL

RUN if [ -n "${APT_MIRROR}" ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null || true; \
    fi

# Install uv (source image overridable via UV_IMAGE build arg).
COPY --from=uv-source /uv /uvx /usr/local/bin/

# UTF-8 locale prevents UnicodeEncodeError on Chinese/emoji plugin docs in
# minimal containers where locale configuration may be missing.
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONIOENCODING=utf-8

WORKDIR /app

# streamable-http transport binds 0.0.0.0:8000 (see config.py DEFAULT_HTTP_*).
EXPOSE 8000

# ── dev: hot-reload, source bind-mounted at runtime (make docker-start) ─────
FROM base AS dev

# Copy dependency manifests only, then sync WITH the dev group so watchfiles is
# present. The package src is bind-mounted at runtime (and lives on a named
# .venv volume), so the container re-syncs at startup to self-heal. Copying
# lockfiles first keeps this layer cached across source edits.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_INDEX_URL=${UV_INDEX_URL:-https://pypi.org/simple} uv sync --frozen

# `watchfiles` restarts the server on any change under /app/src. `--no-sync`
# because startup already synced (below); watchfiles owns the restart loop that
# FastMCP's own `mcp.run()` does not expose.
CMD ["sh", "-c", "uv sync --frozen && exec uv run --no-sync watchfiles 'uv run --no-sync pterodactyl-rag serve' /app/src"]

# ── runtime: code baked in, frozen, no watcher (make up) ────────────────────
FROM base AS runtime

# Build context is the package directory, so copy it in and sync from uv.lock.
COPY . .

# `--no-dev`: the prod image must not ship the dev-only watcher / test tooling.
# psycopg[binary] ships prebuilt wheels, so no compiler toolchain is needed.
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_INDEX_URL=${UV_INDEX_URL:-https://pypi.org/simple} uv sync --frozen --no-dev

# `--no-sync`: the venv is already built above; don't re-resolve at start.
CMD ["uv", "run", "--no-sync", "pterodactyl-rag", "serve"]
