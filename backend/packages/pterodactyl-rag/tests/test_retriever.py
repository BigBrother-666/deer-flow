"""Tests for the retriever: fuzzy tags, soft fallback, snippet bounding, echo."""

from __future__ import annotations

from conftest import FakeEmbedder

from pterodactyl_rag.models import Chunk, Document
from pterodactyl_rag.retriever import Retriever
from pterodactyl_rag.store import InMemoryVectorStore


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


async def test_empty_query_returns_empty() -> None:
    r = Retriever(await _seed(), FakeEmbedder())
    resp = await r.search("   ")
    assert resp.hits == []
    assert resp.note == "empty query"


async def test_search_returns_hits_with_filter_echo() -> None:
    store = await _seed()
    r = Retriever(store, FakeEmbedder())
    resp = await r.search("how to configure kits", plugin="essentialsx")
    assert resp.filter_applied == ["plugin:essentialsx"]
    assert resp.filter_relaxed is False
    assert resp.hits and resp.hits[0].plugin == "essentialsx"


async def test_fuzzy_plugin_normalization() -> None:
    store = await _seed()
    r = Retriever(store, FakeEmbedder())
    # Mixed case + spacing should resolve to canonical known facet value.
    resp = await r.search("kits", plugin="EssentialsX")
    assert resp.filter_applied == ["plugin:essentialsx"]
    assert not resp.filter_relaxed


async def test_fuzzy_plugin_typo_matches_known() -> None:
    store = await _seed()
    r = Retriever(store, FakeEmbedder())
    resp = await r.search("kits", plugin="essentialx")  # typo, close to essentialsx
    assert resp.filter_applied == ["plugin:essentialsx"]


async def test_unknown_plugin_soft_relaxes() -> None:
    store = await _seed()
    r = Retriever(store, FakeEmbedder())
    resp = await r.search("anything", plugin="totally-unrelated-plugin")
    assert resp.filter_relaxed is True
    assert resp.hits  # fell back to unfiltered
    assert "returned unfiltered" in (resp.note or "")


async def test_closed_facets_normalized() -> None:
    store = await _seed()
    r = Retriever(store, FakeEmbedder())
    resp = await r.search("nodes", category="permissions")
    assert "category:permissions" in resp.filter_applied
    assert resp.hits and resp.hits[0].plugin == "worldguard"


async def test_snippet_is_bounded() -> None:
    store = InMemoryVectorStore()
    await store.initialize(embed_model="fake", embed_dim=8)
    emb = FakeEmbedder()
    long_text = "x " * 1000
    vec = (await emb.embed([long_text]))[0]
    await store.upsert_document(
        Document(source_path="A/config/big.md", content_hash="h", title="Big", tags=["plugin:a"]),
        [Chunk(chunk_index=0, content=long_text, tags=["plugin:a"], embedding=vec)],
    )
    r = Retriever(store, emb, snippet_chars=100)
    resp = await r.search("x")
    assert resp.hits
    assert len(resp.hits[0].snippet) <= 102 + 2  # bound + ellipsis


async def test_as_dict_shape() -> None:
    store = await _seed()
    r = Retriever(store, FakeEmbedder())
    d = (await r.search("kits", plugin="essentialsx")).as_dict()
    assert set(d) == {"hits", "filter"}
    assert set(d["filter"]) == {"applied", "relaxed", "note"}
    assert d["hits"] and set(d["hits"][0]) >= {"plugin", "source_path", "score", "snippet", "tags"}
