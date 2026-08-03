"""Tests for env-driven Settings resolution and validation (design §4.4)."""

from __future__ import annotations

import pytest

from pterodactyl_rag.config import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_EMBED_DIM,
    DEFAULT_EMBED_MODEL,
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PORT,
    DEFAULT_TOP_K,
    PG_SCHEMA,
    ConfigError,
    Settings,
)

_ALL_VARS = [
    "PTERO_RAG_DATABASE_URL",
    "PTERO_RAG_DOCS_DIR",
    "PTERO_RAG_EMBED_BASE_URL",
    "PTERO_RAG_EMBED_API_KEY",
    "PTERO_RAG_EMBED_MODEL",
    "PTERO_RAG_EMBED_DIM",
    "PTERO_RAG_CHUNK_TOKENS",
    "PTERO_RAG_CHUNK_OVERLAP",
    "PTERO_RAG_TOP_K",
    "PTERO_RAG_TRANSPORT",
    "PTERO_RAG_HTTP_HOST",
    "PTERO_RAG_HTTP_PORT",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ALL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_missing_dsn_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="PTERO_RAG_DATABASE_URL"):
        Settings.from_env()


def test_defaults_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x/y")
    s = Settings.from_env()
    assert s.database_url == "postgresql://x/y"
    assert s.embed_model == DEFAULT_EMBED_MODEL
    assert s.embed_dim == DEFAULT_EMBED_DIM
    assert s.chunk_tokens == DEFAULT_CHUNK_TOKENS
    assert s.top_k == DEFAULT_TOP_K
    assert s.transport == "stdio"
    assert s.http_host == DEFAULT_HTTP_HOST
    assert s.http_port == DEFAULT_HTTP_PORT
    assert s.pg_schema == PG_SCHEMA
    assert s.docs_dir is None


def test_http_host_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("PTERO_RAG_HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("PTERO_RAG_HTTP_PORT", "9001")
    s = Settings.from_env()
    assert s.http_host == "127.0.0.1"
    assert s.http_port == 9001


def test_http_port_must_be_positive_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("PTERO_RAG_HTTP_PORT", "0")
    with pytest.raises(ConfigError, match="PTERO_RAG_HTTP_PORT"):
        Settings.from_env()


def test_transport_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("PTERO_RAG_TRANSPORT", "carrier-pigeon")
    with pytest.raises(ConfigError, match="PTERO_RAG_TRANSPORT"):
        Settings.from_env()


def test_transport_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("PTERO_RAG_TRANSPORT", "HTTP")
    assert Settings.from_env().transport == "http"


def test_int_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("PTERO_RAG_TOP_K", "0")
    with pytest.raises(ConfigError, match="PTERO_RAG_TOP_K"):
        Settings.from_env()


def test_int_must_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("PTERO_RAG_EMBED_DIM", "not-a-number")
    with pytest.raises(ConfigError, match="PTERO_RAG_EMBED_DIM"):
        Settings.from_env()


def test_overlap_must_be_smaller_than_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("PTERO_RAG_CHUNK_TOKENS", "500")
    monkeypatch.setenv("PTERO_RAG_CHUNK_OVERLAP", "500")
    with pytest.raises(ConfigError, match="OVERLAP"):
        Settings.from_env()


def test_require_docs_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x/y")
    s = Settings.from_env()
    with pytest.raises(ConfigError, match="PTERO_RAG_DOCS_DIR"):
        s.require_docs_dir()

    monkeypatch.setenv("PTERO_RAG_DOCS_DIR", "/tmp/docs")
    assert Settings.from_env().require_docs_dir() == "/tmp/docs"


def test_require_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x/y")
    s = Settings.from_env()
    with pytest.raises(ConfigError, match="PTERO_RAG_EMBED_API_KEY"):
        s.require_embeddings()

    monkeypatch.setenv("PTERO_RAG_EMBED_API_KEY", "sk-test")
    Settings.from_env().require_embeddings()  # no raise
