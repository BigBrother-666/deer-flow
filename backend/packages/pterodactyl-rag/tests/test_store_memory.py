"""Tests for the in-memory VectorStore contract (design §4.2, §4.5.1).

These lock the behavior the pgvector store must match: dim validation, idempotent
upsert, soft-filter fallback, document reassembly, sources, and facets.
"""

from __future__ import annotations

import pytest

from pterodactyl_rag.models import Chunk, Document
from pterodactyl_rag.store import InMemoryVectorStore, VectorStore


def _doc(path: str, tags: list[str], title: str | None = None) -> Document:
    return Document(source_path=path, content_hash="h-" + path, title=title, tags=tags)


def _chunk(i: int, content: str, tags: list[str], emb: list[float]) -> Chunk:
    return Chunk(chunk_index=i, content=content, tags=tags, embedding=emb)


async def _seed() -> InMemoryVectorStore:
    store = InMemoryVectorStore()
    await store.initialize(embed_model="m", embed_dim=3)
    ess_tags = ["plugin:essentialsx", "category:config"]
    await store.upsert_document(
        _doc("EssentialsX/config/kits.md", ess_tags, "Kits"),
        [_chunk(0, "kits config", ess_tags, [1.0, 0.0, 0.0]), _chunk(1, "more kits", ess_tags, [0.9, 0.1, 0.0])],
    )
    wg_tags = ["plugin:worldguard", "category:permissions"]
    await store.upsert_document(
        _doc("WorldGuard/permissions/nodes.md", wg_tags, "Nodes"),
        [_chunk(0, "region perms", wg_tags, [0.0, 1.0, 0.0])],
    )
    return store


def test_inmemory_satisfies_protocol() -> None:
    assert isinstance(InMemoryVectorStore(), VectorStore)


async def test_dim_mismatch_raises() -> None:
    store = InMemoryVectorStore()
    await store.initialize(embed_model="m", embed_dim=3)
    with pytest.raises(ValueError, match="dimension mismatch"):
        await store.initialize(embed_model="m", embed_dim=8)


async def test_search_ranks_by_cosine() -> None:
    store = await _seed()
    res = await store.search([1.0, 0.0, 0.0], top_k=3)
    assert not res.relaxed
    assert res.hits[0].source_path == "EssentialsX/config/kits.md"
    assert res.hits[0].plugin == "essentialsx"
    assert res.hits[0].score >= res.hits[1].score


async def test_tag_filter_restricts() -> None:
    store = await _seed()
    res = await store.search([0.0, 1.0, 0.0], top_k=5, tags=["plugin:worldguard"])
    assert res.applied_tags == ["plugin:worldguard"]
    assert not res.relaxed
    assert {h.plugin for h in res.hits} == {"worldguard"}


async def test_soft_fallback_when_filter_empty() -> None:
    store = await _seed()
    res = await store.search([1.0, 0.0, 0.0], top_k=5, tags=["plugin:doesnotexist"])
    assert res.relaxed is True
    assert res.applied_tags == ["plugin:doesnotexist"]
    assert res.hits  # fell back to full search


async def test_idempotent_upsert_replaces_chunks() -> None:
    store = await _seed()
    tags = ["plugin:essentialsx", "category:config"]
    # Re-upsert same doc with a single chunk -> old chunks replaced.
    await store.upsert_document(_doc("EssentialsX/config/kits.md", tags, "Kits"), [_chunk(0, "replaced", tags, [1.0, 0.0, 0.0])])
    stats = await store.stats()
    assert stats.documents == 2
    assert stats.chunks == 2  # 1 (essx replaced) + 1 (worldguard)


async def test_get_document_text_bounded() -> None:
    store = await _seed()
    text = await store.get_document_text("EssentialsX/config/kits.md", max_chars=8)
    assert text is not None
    assert len(text) == 8
    assert await store.get_document_text("missing", max_chars=100) is None


async def test_delete_documents() -> None:
    store = await _seed()
    removed = await store.delete_documents(["WorldGuard/permissions/nodes.md", "nope"])
    assert removed == 1
    assert await store.list_source_paths() == ["EssentialsX/config/kits.md"]


async def test_list_sources_and_plugin_filter() -> None:
    store = await _seed()
    all_sources = await store.list_sources()
    assert len(all_sources) == 2
    ess = await store.list_sources(plugin="essentialsx")
    assert len(ess) == 1
    assert ess[0]["chunks"] == 2


async def test_list_facets_grouped_by_namespace() -> None:
    store = await _seed()
    facets = await store.list_facets()
    plugins = {fv.value for fv in facets["plugin"]}
    assert plugins == {"essentialsx", "worldguard"}
    cats = {fv.value for fv in facets["category"]}
    assert cats == {"config", "permissions"}

    only_plugin = await store.list_facets(namespace="plugin")
    assert set(only_plugin) == {"plugin"}


async def test_stats_reports_model_and_dim() -> None:
    store = await _seed()
    stats = await store.stats()
    assert stats.embed_model == "m"
    assert stats.embed_dim == 3
    assert stats.documents == 2
    assert stats.chunks == 3
