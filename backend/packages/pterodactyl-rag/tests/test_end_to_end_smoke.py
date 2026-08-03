"""T17 end-to-end smoke: real ingest pipeline → in-memory store → MCP tools.

Drives the whole RAG path a live deployment would run — load a sample plugin
doc from disk, split/tag/embed it, upsert into the store, then answer through
the same tool bodies the FastMCP server registers — but against the in-memory
store and the deterministic fake embedder, so it needs no Postgres or embedding
API key. A live-DB variant is out of scope here (documented in the README); this
locks the wiring between every module so a break anywhere surfaces as a failed
citation rather than a runtime 500 in the agent.
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeEmbedder

from pterodactyl_rag.pipeline import ingest
from pterodactyl_rag.retriever import Retriever
from pterodactyl_rag.server import RagService, create_mcp_server
from pterodactyl_rag.store import InMemoryVectorStore

# A realistic plugin doc with YAML frontmatter tags and a citable config key.
SAMPLE_DOC = """---
plugin: EssentialsX
category: config
---

# Kits configuration

Kits are configured in `config.yml` under the `kits:` section. Each kit lists
the items granted. The `kit-auto-equip-armor` option, when set to `true`,
automatically equips armor pieces included in a kit when the player claims it.
"""


async def _seed_from_disk(tmp_path: Path) -> RagService:
    docs_dir = tmp_path / "docs" / "EssentialsX"
    docs_dir.mkdir(parents=True)
    (docs_dir / "kits.md").write_text(SAMPLE_DOC, encoding="utf-8")

    store = InMemoryVectorStore()
    embedder = FakeEmbedder()
    report = await ingest(
        str(tmp_path / "docs"),
        store,
        embedder,
        max_tokens=800,
        overlap=120,
        embed_model="fake",
        prune=True,
    )
    assert report.indexed, "ingest should index the sample doc"
    assert report.chunks_written >= 1

    retriever = Retriever(store, FakeEmbedder())
    return RagService(store, retriever)


async def test_end_to_end_ingest_search_and_cite(tmp_path: Path) -> None:
    svc = await _seed_from_disk(tmp_path)

    # Discovery: the ingested plugin tag is really present in the index.
    facets = (await svc.list_facets())["facets"]
    plugins = {fv["value"] for fv in facets["plugin"]}
    assert "essentialsx" in plugins

    # Search with the discovered plugin filter returns a hit from that source.
    result = await svc.search("how do kits configure auto equip armor", plugin="essentialsx")
    assert result["filter"]["applied"] == ["plugin:essentialsx"]
    assert result["hits"], "expected at least one hit for the ingested doc"
    hit = result["hits"][0]
    assert hit["plugin"] == "essentialsx"

    # Read the fuller document text and cite the concrete config key.
    doc = await svc.get_document(hit["source_path"])
    assert doc["text"] is not None
    assert "kit-auto-equip-armor" in doc["text"]


async def test_end_to_end_stats_and_five_tools(tmp_path: Path) -> None:
    svc = await _seed_from_disk(tmp_path)

    stats = await svc.stats()
    assert stats["documents"] == 1
    assert stats["chunks"] >= 1
    assert stats["embed_model"] == "fake"

    mcp = create_mcp_server(svc)
    names = {t.name for t in await mcp.list_tools()}
    assert names == {"rag_search", "rag_get_document", "rag_list_sources", "rag_list_facets", "rag_stats"}
