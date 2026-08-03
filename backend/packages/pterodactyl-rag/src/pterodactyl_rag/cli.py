"""Command-line entry point for the plugin-docs RAG index.

Subcommands (design §9, T12):

- ``ingest``  — walk ``PTERO_RAG_DOCS_DIR``, split/embed/upsert, prune deleted.
- ``serve``   — run the MCP server (stdio by default, http if configured).
- ``stats``   — print index health (doc/chunk counts, embed model/dim).
- ``reset``   — drop the index schema (destructive; requires ``--yes``).

Configuration comes entirely from ``PTERO_RAG_*`` environment variables
(:class:`Settings`); secrets are never accepted as flags. Errors are reported as
concise messages with a non-zero exit code rather than tracebacks.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import ConfigError, Settings

logger = logging.getLogger("pterodactyl_rag.cli")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pterodactyl-rag", description="Retrieval index over Pterodactyl/Minecraft plugin documentation.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Ingest docs from PTERO_RAG_DOCS_DIR into the index.")
    p_ingest.add_argument("--no-prune", action="store_true", help="Do not remove index entries whose source file has disappeared.")

    sub.add_parser("serve", help="Run the MCP server (stdio unless PTERO_RAG_TRANSPORT=http).")
    sub.add_parser("stats", help="Print index health and exit.")

    p_reset = sub.add_parser("reset", help="Drop the index schema (destructive).")
    p_reset.add_argument("--yes", action="store_true", help="Confirm the destructive reset without prompting.")

    return parser


async def _run_ingest(settings: Settings, *, prune: bool) -> int:
    from .embeddings import OpenAIEmbedder
    from .pipeline import ingest
    from .server import build_store

    docs_dir = settings.require_docs_dir()
    settings.require_embeddings()
    store = build_store(settings)
    embedder = OpenAIEmbedder(
        api_key=settings.embed_api_key or "",
        model=settings.embed_model,
        dim=settings.embed_dim,
        base_url=settings.embed_base_url,
    )
    report = await ingest(
        docs_dir,
        store,
        embedder,
        max_tokens=settings.chunk_tokens,
        overlap=settings.chunk_overlap,
        embed_model=settings.embed_model,
        prune=prune,
    )
    summary = report.as_dict()
    print(f"Ingest complete: {summary['documents_indexed']} indexed, {summary['documents_skipped']} skipped, {summary['documents_pruned']} pruned, {summary['chunks_written']} chunks written.")
    return 0


async def _run_stats(settings: Settings) -> int:
    from .server import build_store

    store = build_store(settings)
    await store.initialize(embed_model=settings.embed_model, embed_dim=settings.embed_dim)
    s = await store.stats()
    print(f"Index stats:\n  documents:   {s.documents}\n  chunks:      {s.chunks}\n  embed_model: {s.embed_model}\n  embed_dim:   {s.embed_dim}\n  last_ingest: {s.last_ingest}")
    return 0


async def _run_reset(settings: Settings, *, confirmed: bool) -> int:
    if not confirmed:
        print("Refusing to reset without --yes (this drops the entire index schema).", file=sys.stderr)
        return 2
    from .server import build_store

    store = build_store(settings)
    reset = getattr(store, "reset", None)
    if reset is None:
        print("The configured store does not support reset.", file=sys.stderr)
        return 1
    await reset()
    print(f"Dropped schema {settings.pg_schema!r}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "serve":
            # serve() owns the event loop (FastMCP.run is synchronous).
            from .server import serve

            serve(settings)
            return 0
        if args.command == "ingest":
            return asyncio.run(_run_ingest(settings, prune=not args.no_prune))
        if args.command == "stats":
            return asyncio.run(_run_stats(settings))
        if args.command == "reset":
            return asyncio.run(_run_reset(settings, confirmed=args.yes))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — CLI boundary: report concisely, no traceback.
        logger.debug("command failed", exc_info=True)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 2  # unreachable: subparser is required


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
