"""Tests for the ingest pipeline: hash-skip, change-replace, prune, tags."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeEmbedder

from pterodactyl_rag.pipeline import ingest
from pterodactyl_rag.store import InMemoryVectorStore

INGEST_KW = {"max_tokens": 1000, "overlap": 50, "embed_model": "fake"}


def _write(docs: Path, rel: str, body: str) -> Path:
    f = docs / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    return f


async def test_ingest_indexes_and_propagates_tags(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write(docs, "EssentialsX/config/kits.md", "# Kits\nHow to configure kits.")
    store = InMemoryVectorStore()

    report = await ingest(str(docs), store, FakeEmbedder(), **INGEST_KW)
    assert report.indexed == ["EssentialsX/config/kits.md"]
    assert report.chunks_written >= 1

    # tags propagated to chunks -> filterable
    res = await store.search([0.1] * 8, top_k=5, tags=["plugin:essentialsx"])
    assert not res.relaxed
    assert res.hits and res.hits[0].plugin == "essentialsx"


async def test_reingest_skips_unchanged(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write(docs, "A/config/x.md", "# X\nbody")
    store = InMemoryVectorStore()

    first = await ingest(str(docs), store, FakeEmbedder(), **INGEST_KW)
    assert len(first.indexed) == 1

    second = await ingest(str(docs), store, FakeEmbedder(), **INGEST_KW)
    assert second.indexed == []
    assert second.skipped == ["A/config/x.md"]


async def test_reingest_replaces_changed(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    f = _write(docs, "A/config/x.md", "# X\noriginal")
    store = InMemoryVectorStore()
    await ingest(str(docs), store, FakeEmbedder(), **INGEST_KW)

    f.write_text("# X\ncompletely different content now", encoding="utf-8")
    report = await ingest(str(docs), store, FakeEmbedder(), **INGEST_KW)
    assert report.indexed == ["A/config/x.md"]

    text = await store.get_document_text("A/config/x.md", max_chars=1000)
    assert "different content" in text


async def test_prune_removes_deleted_files(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write(docs, "A/config/keep.md", "# K\nkeep")
    gone = _write(docs, "A/config/gone.md", "# G\ngone")
    store = InMemoryVectorStore()
    await ingest(str(docs), store, FakeEmbedder(), **INGEST_KW)
    assert set(await store.list_source_paths()) == {"A/config/keep.md", "A/config/gone.md"}

    gone.unlink()
    report = await ingest(str(docs), store, FakeEmbedder(), **INGEST_KW)
    assert report.pruned == ["A/config/gone.md"]
    assert await store.list_source_paths() == ["A/config/keep.md"]


async def test_prune_disabled(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    gone = _write(docs, "A/config/gone.md", "# G\ngone")
    store = InMemoryVectorStore()
    await ingest(str(docs), store, FakeEmbedder(), **INGEST_KW)
    gone.unlink()
    report = await ingest(str(docs), store, FakeEmbedder(), prune=False, **INGEST_KW)
    assert report.pruned == []
    assert "A/config/gone.md" in await store.list_source_paths()


async def test_ingest_missing_dir_raises(tmp_path: Path) -> None:
    store = InMemoryVectorStore()
    with pytest.raises(NotADirectoryError):
        await ingest(str(tmp_path / "nope"), store, FakeEmbedder(), **INGEST_KW)
