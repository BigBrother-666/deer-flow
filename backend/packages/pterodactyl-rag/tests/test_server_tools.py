"""Tests for the MCP tool layer (RagService): contract shape, discovery,
soft-filter relaxation, enum/facet integrity, and recoverable error paths."""

from __future__ import annotations

from conftest import FakeEmbedder

from pterodactyl_rag.models import Chunk, Document
from pterodactyl_rag.retriever import Retriever
from pterodactyl_rag.server import CategoryLiteral, RagService, build_service, create_mcp_server
from pterodactyl_rag.store import InMemoryVectorStore
from pterodactyl_rag.tags import KNOWN_CATEGORIES


async def _seed() -> InMemoryVectorStore:
    store = InMemoryVectorStore()
    await store.initialize(embed_model="fake", embed_dim=8)
    emb = FakeEmbedder()

    async def add(path: str, tags: list[str], title: str, text: str) -> None:
        vec = (await emb.embed([text]))[0]
        await store.upsert_document(
            Document(source_path=path, content_hash="h" + path, title=title, tags=tags),
            [Chunk(chunk_index=0, content=text, tags=tags, embedding=vec)],
        )

    await add("EssentialsX/config/kits.md", ["plugin:essentialsx", "category:config"], "Kits", "how to configure kits and warps")
    await add("WorldGuard/permissions/nodes.md", ["plugin:worldguard", "category:permissions"], "Nodes", "region permission nodes list")
    return store


async def _service(*, with_retriever: bool = True) -> RagService:
    store = await _seed()
    retriever = Retriever(store, FakeEmbedder()) if with_retriever else None
    return RagService(store, retriever)


async def test_search_returns_contract_shape() -> None:
    svc = await _service()
    result = await svc.search("how to configure kits", plugin="essentialsx")
    assert set(result) == {"hits", "filter"}
    assert set(result["filter"]) == {"applied", "relaxed", "note"}
    assert result["filter"]["applied"] == ["plugin:essentialsx"]
    assert result["hits"] and result["hits"][0]["plugin"] == "essentialsx"


async def test_search_soft_relaxes_unknown_plugin() -> None:
    svc = await _service()
    result = await svc.search("anything", plugin="totally-unknown-plugin")
    assert result["filter"]["relaxed"] is True
    assert result["hits"]  # fell back to unfiltered vector search


async def test_search_fuzzy_normalizes_plugin() -> None:
    svc = await _service()
    result = await svc.search("kits", plugin="EssentialsX")
    assert result["filter"]["applied"] == ["plugin:essentialsx"]


async def test_search_unavailable_without_retriever() -> None:
    svc = await _service(with_retriever=False)
    result = await svc.search("kits")
    assert result["hits"] == []
    assert "unavailable" in result["filter"]["note"].lower()


async def test_search_error_is_recoverable() -> None:
    store = await _seed()

    class BoomRetriever(Retriever):
        async def search(self, *a, **k):  # type: ignore[override]
            raise RuntimeError("embedding endpoint down")

    svc = RagService(store, BoomRetriever(store, FakeEmbedder()))
    result = await svc.search("kits")
    assert result["hits"] == []
    assert "embedding endpoint down" in result["filter"]["note"]


async def test_get_document_returns_text() -> None:
    svc = await _service()
    result = await svc.get_document("EssentialsX/config/kits.md", max_chars=1000)
    assert result["source_path"] == "EssentialsX/config/kits.md"
    assert "kits" in result["text"]
    assert result["truncated"] is False


async def test_get_document_missing_is_recoverable() -> None:
    svc = await _service()
    result = await svc.get_document("nope/missing.md")
    assert result["text"] is None
    assert "rag_list_sources" in result["note"]


async def test_get_document_bounds_and_flags_truncation() -> None:
    svc = await _service()
    result = await svc.get_document("EssentialsX/config/kits.md", max_chars=5)
    assert result["text"] is not None
    assert len(result["text"]) == 5
    assert result["truncated"] is True


async def test_list_sources_shape() -> None:
    svc = await _service()
    result = await svc.list_sources()
    assert result["count"] == 2
    ess = await svc.list_sources(plugin="essentialsx")
    assert ess["count"] == 1
    assert ess["sources"][0]["plugin"] == "essentialsx"


async def test_list_facets_discovery_output() -> None:
    svc = await _service()
    result = await svc.list_facets()
    facets = result["facets"]
    plugins = {fv["value"] for fv in facets["plugin"]}
    assert plugins == {"essentialsx", "worldguard"}
    # discovery entries carry doc counts
    assert all("docs" in fv for fv in facets["plugin"])

    only_plugin = (await svc.list_facets(namespace="plugin"))["facets"]
    assert set(only_plugin) == {"plugin"}


async def test_stats_shape() -> None:
    svc = await _service()
    result = await svc.stats()
    assert result["documents"] == 2
    assert result["chunks"] == 2
    assert result["embed_model"] == "fake"
    assert result["embed_dim"] == 8


async def test_category_enum_matches_canonical_tags() -> None:
    assert set(CategoryLiteral.__args__) == set(KNOWN_CATEGORIES)


async def test_build_service_validates_dim_and_wires_retriever() -> None:
    from pterodactyl_rag.config import Settings

    store = InMemoryVectorStore()
    settings = Settings(
        database_url="postgresql://x",
        docs_dir=None,
        embed_base_url=None,
        embed_api_key="sk-test",
        embed_model="fake",
        embed_dim=8,
        chunk_tokens=800,
        chunk_overlap=120,
        top_k=5,
        transport="stdio",
        http_host="0.0.0.0",
        http_port=8000,
    )
    svc = await build_service(settings, store)
    assert svc._retriever is not None  # api key present -> search enabled


async def test_build_service_without_key_disables_search() -> None:
    from pterodactyl_rag.config import Settings

    store = InMemoryVectorStore()
    settings = Settings(
        database_url="postgresql://x",
        docs_dir=None,
        embed_base_url=None,
        embed_api_key=None,
        embed_model="fake",
        embed_dim=8,
        chunk_tokens=800,
        chunk_overlap=120,
        top_k=5,
        transport="stdio",
        http_host="0.0.0.0",
        http_port=8000,
    )
    svc = await build_service(settings, store)
    assert svc._retriever is None
    result = await svc.search("kits")
    assert "unavailable" in result["filter"]["note"].lower()


async def test_create_mcp_server_registers_five_tools() -> None:
    svc = await _service()
    mcp = create_mcp_server(svc)
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"rag_search", "rag_get_document", "rag_list_sources", "rag_list_facets", "rag_stats"}


async def test_create_mcp_server_binds_host_port() -> None:
    svc = await _service()
    mcp = create_mcp_server(svc, host="0.0.0.0", port=8123)
    assert mcp.settings.host == "0.0.0.0"
    assert mcp.settings.port == 8123
