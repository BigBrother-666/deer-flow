"""Tests for the CLI: argument parsing, config-error exit codes, reset guard."""

from __future__ import annotations

import pytest

from pterodactyl_rag import cli


def test_parser_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args([])


def test_parser_accepts_known_commands() -> None:
    parser = cli._build_parser()
    for cmd in ("ingest", "serve", "stats", "reset"):
        args = parser.parse_args([cmd])
        assert args.command == cmd


def test_ingest_no_prune_flag() -> None:
    args = cli._build_parser().parse_args(["ingest", "--no-prune"])
    assert args.no_prune is True


def test_missing_dsn_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PTERO_RAG_DATABASE_URL", raising=False)
    assert cli.main(["stats"]) == 2


def test_reset_without_yes_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x")
    # reset dispatch happens before any DB call; the --yes guard returns 2.
    assert cli.main(["reset"]) == 2


def test_dispatch_routes_to_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x")
    called: dict[str, object] = {}

    async def fake_stats(settings) -> int:  # type: ignore[no-untyped-def]
        called["settings"] = settings
        return 0

    monkeypatch.setattr(cli, "_run_stats", fake_stats)
    assert cli.main(["stats"]) == 0
    assert called["settings"].database_url == "postgresql://x"


def test_dispatch_routes_to_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTERO_RAG_DATABASE_URL", "postgresql://x")
    seen: dict[str, object] = {}

    def fake_serve(settings) -> None:  # type: ignore[no-untyped-def]
        seen["settings"] = settings

    monkeypatch.setattr("pterodactyl_rag.server.serve", fake_serve)
    assert cli.main(["serve"]) == 0
    assert seen["settings"].transport == "stdio"
