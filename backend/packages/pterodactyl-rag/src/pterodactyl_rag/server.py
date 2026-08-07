"""MCP server exposing read-only retrieval tools over the plugin-docs index.

Design §4.5. Five tools are registered on a FastMCP server:

- ``rag_search`` — vector search with soft tag filtering; ``category``/``lang``
  are schema **enums** so the model sees legal values, while ``plugin`` and free
  ``tags`` are strings the server fuzzy-normalizes (§4.5.1).
- ``rag_get_document`` — bounded full text of one indexed source.
- ``rag_list_sources`` — indexed documents with plugin/title/chunk counts.
- ``rag_list_facets`` — the **discovery** tool: real tag values per namespace.
- ``rag_stats`` — index health (doc/chunk counts, embed model/dim, last ingest).

The tools are read-only (ingestion is CLI-side), so no human-in-the-loop gate is
needed. Every tool catches its own errors and returns a **recoverable string**
payload instead of raising, so a transient store/embedding failure degrades to a
readable note the model can act on rather than aborting the run.

Testability: the tool bodies live on :class:`RagService`, which takes an already
constructed store + optional retriever, so tests drive them against the
in-memory store and a fake embedder without a live database or MCP transport
(``test_server_tools.py``). ``server.py`` only wires those methods onto FastMCP.
"""

from __future__ import annotations

import logging
from typing import Literal

from .config import Settings
from .embeddings import Embedder, OpenAIEmbedder
from .retriever import Retriever
from .store.base import VectorStore
from .tags import KNOWN_CATEGORIES

logger = logging.getLogger(__name__)

# Closed-set facets are enums so the model picks a legal value without a
# discovery round-trip (§4.5.1 point 2). Open facets (plugin, free tags) stay
# strings that the retriever fuzzy-normalizes server-side.
CategoryLiteral = Literal["config", "permissions", "commands", "faq", "api", "install"]
LangLiteral = Literal["en", "zh"]

# Guard the enum against silent drift from the canonical tag category set.
assert set(KNOWN_CATEGORIES) == set(CategoryLiteral.__args__), "rag_search category enum must match tags.KNOWN_CATEGORIES"

DEFAULT_GET_DOCUMENT_CHARS = 8000


class RagService:
    """Backing implementation of the five MCP tools.

    Holds an initialized :class:`VectorStore` and an optional
    :class:`Retriever` (present only when embedding credentials are configured,
    since search needs to embed the query). ``list_facets`` / ``list_sources`` /
    ``stats`` / ``get_document`` work without embeddings; ``search`` returns a
    recoverable note when no retriever is available.
    """

    def __init__(self, store: VectorStore, retriever: Retriever | None) -> None:
        self._store = store
        self._retriever = retriever

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        plugin: str | None = None,
        category: CategoryLiteral | None = None,
        platform: str | None = None,
        lang: LangLiteral | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        if self._retriever is None:
            return {
                "hits": [],
                "filter": {"applied": [], "relaxed": False, "note": "Search is unavailable: no embedding API key is configured for the RAG server."},
            }
        try:
            response = await self._retriever.search(
                query,
                top_k=top_k,
                plugin=plugin,
                category=category,
                platform=platform,
                lang=lang,
                tags=tags,
            )
        except Exception as exc:  # noqa: BLE001 — surface as recoverable note, never abort the run.
            logger.warning("rag_search failed: %s", exc)
            return {"hits": [], "filter": {"applied": [], "relaxed": False, "note": f"Search failed: {exc}"}}
        return response.as_dict()

    async def get_document(self, source_path: str, *, max_chars: int = DEFAULT_GET_DOCUMENT_CHARS) -> dict:
        bound = max_chars if max_chars > 0 else DEFAULT_GET_DOCUMENT_CHARS
        try:
            text = await self._store.get_document_text(source_path, max_chars=bound)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rag_get_document failed: %s", exc)
            return {"source_path": source_path, "text": None, "note": f"Failed to read document: {exc}"}
        if text is None:
            return {
                "source_path": source_path,
                "text": None,
                "note": "No indexed document at that source_path. Use rag_list_sources to see indexed paths.",
            }
        return {"source_path": source_path, "text": text, "truncated": len(text) >= bound}

    async def list_sources(self, plugin: str | None = None) -> dict:
        try:
            sources = await self._store.list_sources(plugin=plugin)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rag_list_sources failed: %s", exc)
            return {"sources": [], "note": f"Failed to list sources: {exc}"}
        return {"sources": sources, "count": len(sources)}

    async def list_facets(self, namespace: str | None = None) -> dict:
        try:
            facets = await self._store.list_facets(namespace=namespace)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rag_list_facets failed: %s", exc)
            return {"facets": {}, "note": f"Failed to list facets: {exc}"}
        return {"facets": {ns: [{"value": fv.value, "docs": fv.docs} for fv in values] for ns, values in facets.items()}}

    async def stats(self) -> dict:
        try:
            s = await self._store.stats()
        except Exception as exc:  # noqa: BLE001
            logger.warning("rag_stats failed: %s", exc)
            return {"note": f"Failed to read index stats: {exc}"}
        return {
            "documents": s.documents,
            "chunks": s.chunks,
            "embed_model": s.embed_model,
            "embed_dim": s.embed_dim,
            "last_ingest": s.last_ingest,
        }


def build_store(settings: Settings) -> VectorStore:
    """Construct the pgvector store from settings (lazy import of heavy deps)."""
    from .store.pgvector_store import PgVectorStore

    return PgVectorStore(settings.database_url, schema=settings.pg_schema)


def build_embedder(settings: Settings) -> Embedder:
    """Construct the OpenAI-compatible embedder; requires an API key."""
    settings.require_embeddings()
    return OpenAIEmbedder(
        api_key=settings.embed_api_key or "",
        model=settings.embed_model,
        dim=settings.embed_dim,
        url=settings.embed_url,
    )


async def build_service(settings: Settings, store: VectorStore) -> RagService:
    """Initialize the store (dim-validating against settings) and build the service.

    Startup dim validation: ``store.initialize`` records the embedding model/dim
    on a fresh index and **refuses** to open an existing index whose stored dim
    or model differs from the configured one (raising ``ValueError``), so we
    never serve queries whose vectors are incompatible with the stored ones.
    """
    await store.initialize(embed_model=settings.embed_model, embed_dim=settings.embed_dim)
    retriever: Retriever | None = None
    if settings.embed_api_key:
        retriever = Retriever(store, build_embedder(settings), snippet_chars=500)
    else:
        logger.warning("No PTERO_RAG_EMBED_API_KEY set; rag_search will be unavailable (discovery/stats tools still work).")
    return RagService(store, retriever)


def create_mcp_server(service: RagService, *, name: str = "pterodactyl-rag", host: str | None = None, port: int | None = None) -> FastMCP:  # noqa: F821 (quoted forward ref)
    """Register the five read-only tools on a FastMCP server.

    ``host`` / ``port`` are only meaningful for the HTTP transport. They are
    passed to the ``FastMCP`` constructor because FastMCP resolves its bind
    address from constructor kwargs (or a ``.env`` file), NOT from process
    environment variables — so ``PTERO_RAG_HTTP_HOST`` / ``FASTMCP_HOST`` alone
    would not move the server off FastMCP's loopback-only default.
    """
    from mcp.server.fastmcp import FastMCP

    kwargs: dict[str, object] = {}
    if host is not None:
        kwargs["host"] = host
    if port is not None:
        kwargs["port"] = port
    mcp = FastMCP(name, **kwargs)

    _search_desc = (
        "Search Pterodactyl/Minecraft plugin documentation by semantic query. "
        "Optionally filter by plugin name, category, platform, lang, or raw tags; "
        "unknown filters soft-relax rather than failing. "
        "Returns ranked snippets plus a filter echo block."
    )

    @mcp.tool(description=_search_desc)
    async def rag_search(  # type: ignore[no-untyped-def]
        query: str,
        top_k: int = 5,
        plugin: str | None = None,
        category: CategoryLiteral | None = None,
        platform: str | None = None,
        lang: LangLiteral | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        return await service.search(query, top_k=top_k, plugin=plugin, category=category, platform=platform, lang=lang, tags=tags)

    @mcp.tool(description="Read the full (bounded) reconstructed text of one indexed source document, addressed by its source_path from a rag_search hit.")
    async def rag_get_document(source_path: str, max_chars: int = DEFAULT_GET_DOCUMENT_CHARS) -> dict:  # type: ignore[no-untyped-def]
        return await service.get_document(source_path, max_chars=max_chars)

    @mcp.tool(description="List indexed documents with plugin/title/chunk counts. Optionally scope to one plugin. Use this to see what documentation coverage exists.")
    async def rag_list_sources(plugin: str | None = None) -> dict:  # type: ignore[no-untyped-def]
        return await service.list_sources(plugin=plugin)

    _list_facets_desc = (
        "Discovery tool: list the real tag values present in the index grouped by "
        "namespace (e.g. the actual plugin names), with document counts. Call this to "
        "confirm a plugin name BEFORE filtering rag_search, instead of guessing tag strings."
    )

    @mcp.tool(description=_list_facets_desc)
    async def rag_list_facets(namespace: str | None = None) -> dict:  # type: ignore[no-untyped-def]
        return await service.list_facets(namespace=namespace)

    @mcp.tool(description="Report index health: document count, chunk count, embedding model and dimension, and last ingest time.")
    async def rag_stats() -> dict:  # type: ignore[no-untyped-def]
        return await service.stats()

    return mcp


def serve(settings: Settings | None = None) -> None:
    """Entry point for ``pterodactyl-rag serve``: build, dim-validate, run.

    Runs synchronously via FastMCP's own event loop. ``build_service`` is driven
    on a fresh loop first so the startup dim validation fails fast (before the
    transport binds) with an actionable error, rather than surfacing on the
    first tool call.
    """
    import anyio

    settings = settings or Settings.from_env()
    store = build_store(settings)
    service = anyio.run(build_service, settings, store)
    if settings.transport == "http":
        mcp = create_mcp_server(service, host=settings.http_host, port=settings.http_port)
        transport = "streamable-http"
        logger.info("Starting pterodactyl-rag MCP server on http transport (%s:%s).", settings.http_host, settings.http_port)
    else:
        mcp = create_mcp_server(service)
        transport = "stdio"
        logger.info("Starting pterodactyl-rag MCP server on stdio transport.")
    mcp.run(transport=transport)
