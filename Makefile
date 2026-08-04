# DeerFlow - Unified Development Environment

.PHONY: help config config-upgrade check install setup doctor support-bundle detect-thread-boundaries detect-blocking-io dev dev-daemon start start-daemon nginx stop up down clean docker-init docker-start docker-stop docker-restart docker-logs docker-logs-frontend docker-logs-gateway docker-logs-redis rag-stack-up rag-stack-up-dev rag-stack-down rag-stack-restart rag-stack-logs rag-ingest-docker pg-up pg-down pg-restart pg-destroy pg-logs stack-up stack-up-dev stack-down stack-restart rag-ingest rag-serve rag-stats rag-reset langfuse-up langfuse-down langfuse-restart langfuse-logs

BASH ?= bash
BACKEND_UV_RUN = cd backend && uv run

# Standalone pgvector Postgres (docker compose) + plugin-docs RAG vector store.
DOCKER_COMPOSE ?= docker compose
RAG_COMPOSE_FILE = docker/docker-compose.rag.yaml
# Dev overlay: layer on top of RAG_COMPOSE_FILE to hot-reload rag-mcp (build the
# `dev` stage + bind-mount source). Prod (rag-stack-up) omits it and builds the
# `runtime` stage with code baked in — mirroring DeerFlow's own dev/prod split.
RAG_DEV_COMPOSE_FILE = docker/docker-compose.rag.dev.yaml
RAG_DIR = backend/packages/pterodactyl-rag
# Langfuse tracing backend (self-hosted, docker compose). Reads LANGFUSE_* from
# root .env via the compose env_file.
LANGFUSE_COMPOSE_FILE = docker/docker-compose.langfuse.yaml
# Root .env holds the RAG secrets/paths (PTERO_RAG_*). Unlike docker compose,
# neither make nor `uv run` auto-loads it, so the rag-* targets source it
# themselves at runtime. Values stay in the gitignored .env; nothing is inlined.
ENV_FILE ?= .env
LOAD_DOTENV = set -a; [ -f $(ENV_FILE) ] && . ./$(ENV_FILE); set +a;

# Detect OS for Windows compatibility
ifeq ($(OS),Windows_NT)
    SHELL := cmd.exe
    PYTHON ?= python
    # Run repo shell scripts through Git Bash when Make is launched from cmd.exe / PowerShell.
    RUN_WITH_GIT_BASH = call scripts\run-with-git-bash.cmd
else
    PYTHON ?= python3
    RUN_WITH_GIT_BASH =
endif

FRONTEND_PNPM = $(PYTHON) ../scripts/pnpm.py

help:
	@echo "DeerFlow Development Commands:"
	@echo "  make setup           - Interactive setup wizard (recommended for new users)"
	@echo "  make doctor          - Check configuration and system requirements"
	@echo "  make support-bundle  - Create a redacted issue summary, AI draft, and evidence bundle"
	@echo "  make config          - Generate local config files (aborts if config already exists)"
	@echo "  make config-upgrade  - Merge new fields from config.example.yaml into config.yaml"
	@echo "  make check           - Check if all required tools are installed"
	@echo "  make detect-thread-boundaries - Inventory backend executor/thread/event-loop boundaries"
	@echo "  make detect-blocking-io        - Inventory blocking IO that may block the backend event loop"
	@echo "  make install         - Install all dependencies (frontend + backend + pre-commit hooks)"
	@echo "  make setup-sandbox   - Pre-pull sandbox container image (recommended)"
	@echo "  make dev             - Start all services in development mode (with hot-reloading)"
	@echo "  make dev-daemon      - Start dev services in background (daemon mode)"
	@echo "  make start           - Start all services in production mode (optimized, no hot-reloading)"
	@echo "  make start-daemon    - Start prod services in background (daemon mode)"
	@echo "  make nginx           - Start nginx alone in the foreground (local dev config)"
	@echo "  make stop            - Stop all running services"
	@echo "  make clean           - Clean up processes and temporary files"
	@echo ""
	@echo "Docker Production Commands:"
	@echo "  make up              - Build and start production Docker services (localhost:2026)"
	@echo "  make down            - Stop and remove production Docker containers"
	@echo ""
	@echo "Docker Development Commands:"
	@echo "  make docker-init     - Pull the sandbox image"
	@echo "  make docker-start    - Start Docker services (mode-aware from config.yaml, localhost:2026)"
	@echo "  make docker-stop     - Stop Docker development services"
	@echo "  make docker-restart  - Restart Docker development services"
	@echo "  make docker-logs     - View Docker development logs"
	@echo "  make docker-logs-frontend - View Docker frontend logs"
	@echo "  make docker-logs-gateway - View Docker gateway logs"
	@echo "  make docker-logs-redis - View Docker Redis logs"
	@echo ""
	@echo "RAG Extension Stack (pgvector + rag-mcp) Commands:"
	@echo "  make rag-stack-up      - Build + start the RAG stack, prod mode (code baked in); creates shared network"
	@echo "  make rag-stack-up-dev  - Build + start the RAG stack, dev mode (source bind-mounted, watchfiles hot-reload)"
	@echo "  make rag-stack-down    - Stop and remove the RAG stack"
	@echo "  make rag-stack-restart - Restart the RAG stack"
	@echo "  make rag-stack-logs    - Tail RAG stack logs (postgres + rag-mcp)"
	@echo "  make rag-ingest-docker - One-shot docs ingest inside the stack (mounts PTERO_RAG_DOCS_DIR)"
	@echo ""
	@echo "Postgres (pgvector) Commands:"
	@echo "  make pg-up           - Start only the pgvector Postgres service of the RAG stack"
	@echo "  make pg-down         - Stop and remove the RAG stack containers"
	@echo "  make pg-restart      - Restart the Postgres service"
	@echo "  make pg-destroy      - Stop the stack and DELETE its data volume (destructive)"
	@echo "  make pg-logs         - Tail Postgres logs"
	@echo ""
	@echo "Full Stack (Docker dev services + RAG extension stack) Commands:"
	@echo "  make stack-up        - Start the RAG stack (prod), then the Docker dev stack (localhost:2026)"
	@echo "  make stack-up-dev    - Start the RAG stack (dev hot-reload), then the Docker dev stack"
	@echo "  make stack-down      - Stop the Docker dev stack, then the RAG stack"
	@echo "  make stack-restart   - Restart the RAG stack and the Docker dev stack"
	@echo ""
	@echo "Plugin-Docs RAG (host process, local debug) Commands:"
	@echo "  make rag-ingest      - Ingest PTERO_RAG_DOCS_DIR into the vector store (needs embed key)"
	@echo "  make rag-serve       - Run the RAG MCP server in the foreground"
	@echo "  make rag-stats       - Print vector-store index health"
	@echo "  make rag-reset       - Drop the RAG schema (destructive; asks for env RAG_RESET_YES=1)"
	@echo ""
	@echo "Langfuse Tracing Backend (docker compose) Commands:"
	@echo "  make langfuse-up      - Start the self-hosted Langfuse stack (UI at LANGFUSE_BASE_URL)"
	@echo "  make langfuse-down    - Stop and remove the Langfuse stack (keeps data volume)"
	@echo "  make langfuse-restart - Restart the Langfuse stack"
	@echo "  make langfuse-logs    - Tail Langfuse logs"

## Setup & Diagnosis
setup:
	@$(BACKEND_UV_RUN) python ../scripts/setup_wizard.py

doctor:
	@$(BACKEND_UV_RUN) python ../scripts/doctor.py

support-bundle:
	@$(BACKEND_UV_RUN) python ../scripts/support_bundle.py --include-doctor

detect-thread-boundaries:
	@$(BACKEND_UV_RUN) python ../scripts/detect_thread_boundaries.py --json-output ../.deer-flow/thread-boundary-inventory.json

detect-blocking-io:
	@$(MAKE) -C backend detect-blocking-io

config:
	@$(PYTHON) ./scripts/configure.py

config-upgrade:
	@$(RUN_WITH_GIT_BASH) ./scripts/config-upgrade.sh

# Check required tools
check:
	@$(PYTHON) ./scripts/check.py

# Install all dependencies
install:
	@echo "Installing backend dependencies..."
	@cd backend && uv sync
	@echo "Installing frontend dependencies..."
	@cd frontend && $(FRONTEND_PNPM) install
	@echo "Installing pre-commit hooks..."
	@uv tool install pre-commit
	@pre-commit install --overwrite
	@echo "✓ All dependencies installed"
	@echo ""
	@echo "=========================================="
	@echo "  Optional: Pre-pull Sandbox Image"
	@echo "=========================================="
	@echo ""
	@echo "If you plan to use Docker/Container-based sandbox, you can pre-pull the image:"
	@echo "  make setup-sandbox"
	@echo ""

# Pre-pull sandbox Docker image (optional but recommended)
setup-sandbox:
	@$(RUN_WITH_GIT_BASH) ./scripts/setup-sandbox.sh

# Start all services in development mode (with hot-reloading)
dev:
	@$(PYTHON) ./scripts/check.py
	@$(RUN_WITH_GIT_BASH) ./scripts/serve.sh --dev

# Start all services in production mode (with optimizations)
start:
	@$(PYTHON) ./scripts/check.py
	@$(RUN_WITH_GIT_BASH) ./scripts/serve.sh --prod

# Start all services in daemon mode (background)
dev-daemon:
	@$(PYTHON) ./scripts/check.py
	@$(RUN_WITH_GIT_BASH) ./scripts/serve.sh --dev --daemon

# Start prod services in daemon mode (background)
start-daemon:
	@$(PYTHON) ./scripts/check.py
	@$(RUN_WITH_GIT_BASH) ./scripts/serve.sh --prod --daemon

# Start nginx alone in the foreground with the local dev config
nginx:
	@$(RUN_WITH_GIT_BASH) ./scripts/nginx.sh

# Stop all services
stop:
	@$(RUN_WITH_GIT_BASH) ./scripts/serve.sh --stop

# Clean up
clean: stop
	@echo "Cleaning up..."
	@-rm -rf backend/.deer-flow 2>/dev/null || true
	@-rm -rf logs/*.log 2>/dev/null || true
	@echo "✓ Cleanup complete"

# ==========================================
# Docker Development Commands
# ==========================================

# Initialize Docker containers and install dependencies
docker-init:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh init

# Start Docker development environment
docker-start:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh start

# Stop Docker development environment
docker-stop:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh stop

# Restart Docker development environment
docker-restart:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh restart

# View Docker development logs
docker-logs:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh logs

# View Docker development logs
docker-logs-frontend:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh logs --frontend
docker-logs-gateway:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh logs --gateway
docker-logs-redis:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh logs --redis

# ==========================================
# Production Docker Commands
# ==========================================

# Build and start production services
up:
	@$(RUN_WITH_GIT_BASH) ./scripts/deploy.sh

# Stop and remove production containers
down:
	@$(RUN_WITH_GIT_BASH) ./scripts/deploy.sh down

# ==========================================
# RAG Extension Stack (pgvector + rag-mcp) Commands
# ==========================================
# The RAG extension stack (docker/docker-compose.rag.yaml) bundles two services:
#   - postgres:  pgvector (backs DeerFlow persistence + the RAG vector store)
#   - rag-mcp:   the containerized pterodactyl-rag MCP server (http :8000)
# on the shared external network `deer-flow-shared`, which the main dev stack
# also joins so the gateway reaches the server at http://rag-mcp:8000/mcp.

# Start the whole RAG extension stack, PROD mode (rag-mcp code baked into the
# image, no watcher; builds the `runtime` stage; creates the shared network)
rag-stack-up:
	@$(DOCKER_COMPOSE) -f $(RAG_COMPOSE_FILE) up -d --build
	@echo "✓ RAG stack up (prod): deer-flow-postgres + deer-flow-rag-mcp (http://rag-mcp:8000/mcp)"

# Start the RAG extension stack, DEV mode (rag-mcp source bind-mounted, watchfiles
# hot-reload; builds the `dev` stage via the dev overlay compose)
rag-stack-up-dev:
	@$(DOCKER_COMPOSE) -f $(RAG_COMPOSE_FILE) -f $(RAG_DEV_COMPOSE_FILE) up -d --build
	@echo "✓ RAG stack up (dev, hot-reload): deer-flow-postgres + deer-flow-rag-mcp (http://rag-mcp:8000/mcp)"

# Stop and remove the RAG extension stack (keeps the data volume)
rag-stack-down:
	@$(DOCKER_COMPOSE) -f $(RAG_COMPOSE_FILE) down

rag-stack-restart:
	@$(DOCKER_COMPOSE) -f $(RAG_COMPOSE_FILE) restart

# Tail RAG extension stack logs (postgres + rag-mcp)
rag-stack-logs:
	@$(DOCKER_COMPOSE) -f $(RAG_COMPOSE_FILE) logs -f

# One-shot docs ingest inside the stack (mounts PTERO_RAG_DOCS_DIR at /docs).
# Reuses the rag-mcp image + in-network DSN; embeds via host Ollama.
rag-ingest-docker:
	@$(LOAD_DOTENV) test -n "$$PTERO_RAG_DOCS_DIR" || { echo "PTERO_RAG_DOCS_DIR is required (set it in .env)"; exit 1; }
	@$(LOAD_DOTENV) $(DOCKER_COMPOSE) -f $(RAG_COMPOSE_FILE) run --rm \
		-v "$$PTERO_RAG_DOCS_DIR:/docs:ro" -e PTERO_RAG_DOCS_DIR=/docs \
		rag-mcp uv run --no-sync pterodactyl-rag ingest

# --- Postgres-only helpers (single service of the RAG stack) ---

# Start only the pgvector Postgres service
pg-up:
	@$(DOCKER_COMPOSE) -f $(RAG_COMPOSE_FILE) up -d postgres
	@echo "✓ Postgres up on $(PTERO_RAG_DATABASE_URL)"

# Stop and remove the RAG stack containers
pg-down:
	@$(DOCKER_COMPOSE) -f $(RAG_COMPOSE_FILE) down

# Restart only the Postgres service (keeps the data volume)
pg-restart:
	@$(DOCKER_COMPOSE) -f $(RAG_COMPOSE_FILE) restart postgres

# Stop the stack AND delete the data volume (destructive; wipes all data)
pg-destroy:
	@$(DOCKER_COMPOSE) -f $(RAG_COMPOSE_FILE) down -v

# Tail Postgres logs
pg-logs:
	@$(DOCKER_COMPOSE) -f $(RAG_COMPOSE_FILE) logs -f postgres

# ==========================================
# Full Stack (dev services + RAG extension stack) Commands
# ==========================================

# Start the RAG extension stack first (creates deer-flow-shared), then the dev stack.
# RAG runs in prod mode (code baked in); use stack-up-dev for RAG hot-reload.
stack-up: rag-stack-up up langfuse-up

# Full dev stack with RAG hot-reload: RAG dev overlay first, then the dev stack.
stack-up-dev: rag-stack-up-dev docker-start langfuse-up

# Stop the Docker dev stack first, then the RAG extension stack
stack-down: langfuse-down docker-stop rag-stack-down

# Restart the RAG extension stack and the Docker dev stack
stack-restart: rag-stack-restart langfuse-restart docker-restart

# ==========================================
# Plugin-Docs RAG Vector Store Commands
# ==========================================
# These wrap `pterodactyl-rag` (standalone uv project). They read secrets/paths
# from your environment — set them before running, never inline them here:
#   export PTERO_RAG_EMBED_API_KEY="$$OPENAI_API_KEY"   # or "local" for Ollama/TEI
#   export PTERO_RAG_DOCS_DIR="/abs/path/to/docs"       # required by rag-ingest
# PTERO_RAG_DATABASE_URL defaults to the docker-compose.rag.yaml DSN.

# Ingest docs (chunk -> embed -> upsert) and prune deleted files
rag-ingest:
	@$(LOAD_DOTENV) cd $(RAG_DIR) && uv run pterodactyl-rag ingest

# Run the RAG MCP server in the foreground (stdio, or http via PTERO_RAG_TRANSPORT)
rag-serve:
	@$(LOAD_DOTENV) cd $(RAG_DIR) && uv run pterodactyl-rag serve

# Print index health (documents / chunks / embed model / dim / last ingest)
rag-stats:
	@$(LOAD_DOTENV) cd $(RAG_DIR) && uv run pterodactyl-rag stats

# Drop the RAG schema (destructive). Guarded: set RAG_RESET_YES=1 to proceed.
rag-reset:
ifeq ($(RAG_RESET_YES),1)
	@$(LOAD_DOTENV) cd $(RAG_DIR) && uv run pterodactyl-rag reset --yes
else
	@echo "Refusing to reset: this drops the pterodactyl_rag schema."
	@echo "Re-run with: RAG_RESET_YES=1 make rag-reset"
	@exit 1
endif

# ==========================================
# Langfuse Tracing Backend Commands
# ==========================================
# Self-hosted Langfuse v4 stack (docker/docker-compose.langfuse.yaml). Set the
# LANGFUSE_* keys in the root .env; unlike docker compose's own auto-load, that
# root .env is sourced here via LOAD_DOTENV so compose can interpolate it (the
# compose file is under docker/, not the repo root). Point DeerFlow at this
# instance with LANGFUSE_BASE_URL / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY;
# the compose LANGFUSE_INIT_* block seeds a project with those same keys on
# first boot, so tracing works with no manual UI setup.

# Start the Langfuse stack
langfuse-up:
	@$(LOAD_DOTENV) $(DOCKER_COMPOSE) -f $(LANGFUSE_COMPOSE_FILE) up -d
	@echo "✓ Langfuse up on $${LANGFUSE_BASE_URL:-http://localhost:3000}"

# Stop and remove the Langfuse stack (keeps the data volumes)
langfuse-down:
	@$(LOAD_DOTENV) $(DOCKER_COMPOSE) -f $(LANGFUSE_COMPOSE_FILE) down

# Restart the Langfuse stack
langfuse-restart:
	@$(LOAD_DOTENV) $(DOCKER_COMPOSE) -f $(LANGFUSE_COMPOSE_FILE) restart

# Tail Langfuse logs
langfuse-logs:
	@$(LOAD_DOTENV) $(DOCKER_COMPOSE) -f $(LANGFUSE_COMPOSE_FILE) logs -f
