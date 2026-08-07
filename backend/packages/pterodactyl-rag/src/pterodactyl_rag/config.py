"""Environment-driven settings for the RAG server and ingest CLI.

The server runs as its own process, so it takes configuration from the
environment rather than DeerFlow's ``config.yaml`` (design §4.4). Secrets
(``DATABASE_URL``, embedding API key) are passed by the DeerFlow MCP launcher
after it resolves ``$VAR`` references in ``extensions_config.json``, so this
module does not re-implement ``$VAR`` expansion; it reads already-resolved
values and validates them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

PG_SCHEMA = "pterodactyl_rag"

DEFAULT_EMBED_MODEL = "text-embedding-3-small"
DEFAULT_EMBED_DIM = 1536
DEFAULT_CHUNK_TOKENS = 800
DEFAULT_CHUNK_OVERLAP = 120
DEFAULT_TOP_K = 5
DEFAULT_TRANSPORT = "stdio"
# HTTP transport bind address. Defaults bind all interfaces so the server is
# reachable from other containers on a shared Docker network; FastMCP's own
# default (127.0.0.1) is loopback-only and unreachable across containers. These
# are only used when transport == "http".
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8000

_VALID_TRANSPORTS = ("stdio", "http")


class ConfigError(RuntimeError):
    """Raised when required settings are missing or invalid."""


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be positive, got {parsed}")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved, validated configuration for both the server and the CLI."""

    database_url: str
    docs_dir: str | None
    embed_url: str | None
    embed_api_key: str | None
    embed_model: str
    embed_dim: int
    chunk_tokens: int
    chunk_overlap: int
    top_k: int
    transport: str
    http_host: str
    http_port: int

    @property
    def pg_schema(self) -> str:
        return PG_SCHEMA

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``PTERO_RAG_*`` environment variables.

        Validates only what every entry point needs (the DSN). ``docs_dir`` and
        the embedding credentials are validated lazily by the operations that
        require them (see :meth:`require_docs_dir` / :meth:`require_embeddings`)
        so ``serve`` can start without ``docs_dir`` and ``stats`` without an
        embedding key.
        """
        database_url = _get("PTERO_RAG_DATABASE_URL")
        if not database_url:
            raise ConfigError("PTERO_RAG_DATABASE_URL is required (the Postgres DSN; reuse $DATABASE_URL).")

        transport = (_get("PTERO_RAG_TRANSPORT", DEFAULT_TRANSPORT) or DEFAULT_TRANSPORT).lower()
        if transport not in _VALID_TRANSPORTS:
            raise ConfigError(f"PTERO_RAG_TRANSPORT must be one of {_VALID_TRANSPORTS}, got {transport!r}")

        chunk_tokens = _get_int("PTERO_RAG_CHUNK_TOKENS", DEFAULT_CHUNK_TOKENS)
        chunk_overlap = _get_int("PTERO_RAG_CHUNK_OVERLAP", DEFAULT_CHUNK_OVERLAP)
        if chunk_overlap >= chunk_tokens:
            raise ConfigError(f"PTERO_RAG_CHUNK_OVERLAP ({chunk_overlap}) must be smaller than PTERO_RAG_CHUNK_TOKENS ({chunk_tokens}).")

        return cls(
            database_url=database_url,
            docs_dir=_get("PTERO_RAG_DOCS_DIR"),
            embed_url=_get("PTERO_RAG_EMBED_URL"),
            embed_api_key=_get("PTERO_RAG_EMBED_API_KEY"),
            embed_model=_get("PTERO_RAG_EMBED_MODEL", DEFAULT_EMBED_MODEL) or DEFAULT_EMBED_MODEL,
            embed_dim=_get_int("PTERO_RAG_EMBED_DIM", DEFAULT_EMBED_DIM),
            chunk_tokens=chunk_tokens,
            chunk_overlap=chunk_overlap,
            top_k=_get_int("PTERO_RAG_TOP_K", DEFAULT_TOP_K),
            transport=transport,
            http_host=_get("PTERO_RAG_HTTP_HOST", DEFAULT_HTTP_HOST) or DEFAULT_HTTP_HOST,
            http_port=_get_int("PTERO_RAG_HTTP_PORT", DEFAULT_HTTP_PORT),
        )

    def require_docs_dir(self) -> str:
        """Return ``docs_dir`` or raise — used by the ingest path."""
        if not self.docs_dir:
            raise ConfigError("PTERO_RAG_DOCS_DIR is required for ingestion.")
        return self.docs_dir

    def require_embeddings(self) -> None:
        """Validate that an embedding API key is present (embed/ingest/query)."""
        if not self.embed_api_key:
            raise ConfigError("PTERO_RAG_EMBED_API_KEY is required to embed or query (provide via $VAR, never inline).")
