"""Ingestion orchestration: load -> split -> embed -> upsert (design §4.3).

Idempotent: a document whose content hash is unchanged is skipped; a changed
document has its chunks replaced; a document whose source file no longer exists
is pruned. Tags resolved by the loader are propagated onto every chunk before
upsert so the store can filter and rank in one scan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .embeddings import Embedder
from .loaders import iter_documents
from .splitter import split_document
from .store.base import VectorStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestReport:
    """Summary of an ingest run."""

    indexed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    chunks_written: int = 0

    def as_dict(self) -> dict:
        return {
            "indexed": self.indexed,
            "skipped": self.skipped,
            "pruned": self.pruned,
            "documents_indexed": len(self.indexed),
            "documents_skipped": len(self.skipped),
            "documents_pruned": len(self.pruned),
            "chunks_written": self.chunks_written,
        }


async def ingest(
    docs_dir: str,
    store: VectorStore,
    embedder: Embedder,
    *,
    max_tokens: int,
    overlap: int,
    embed_model: str,
    prune: bool = True,
) -> IngestReport:
    """Ingest every supported document under ``docs_dir`` into ``store``.

    Args:
        docs_dir: Directory of plugin docs to walk.
        store: Target vector store (already constructed, not yet initialized).
        embedder: Embedding client; its ``dim`` pins the index dimension.
        max_tokens / overlap: Chunking parameters.
        embed_model: Model name recorded in the index meta row.
        prune: Remove indexed documents whose source file has disappeared.
    """
    await store.initialize(embed_model=embed_model, embed_dim=embedder.dim)

    report = IngestReport()
    seen: set[str] = set()

    for loaded in iter_documents(docs_dir):
        doc = loaded.document
        seen.add(doc.source_path)

        existing = await store.get_document_hash(doc.source_path)
        if existing == doc.content_hash:
            report.skipped.append(doc.source_path)
            continue

        chunks = split_document(
            loaded.text,
            is_markdown=loaded.is_markdown,
            max_tokens=max_tokens,
            overlap=overlap,
            tags=doc.tags,
        )
        if not chunks:
            logger.info("No chunks produced for %s; skipping", doc.source_path)
            report.skipped.append(doc.source_path)
            continue

        vectors = await embedder.embed([c.content for c in chunks])
        for chunk, vector in zip(chunks, vectors):
            chunk.embedding = vector

        await store.upsert_document(doc, chunks)
        report.indexed.append(doc.source_path)
        report.chunks_written += len(chunks)

    if prune:
        indexed_paths = set(await store.list_source_paths())
        stale = sorted(indexed_paths - seen)
        if stale:
            await store.delete_documents(stale)
            report.pruned.extend(stale)

    logger.info("Ingest complete: %s", report.as_dict())
    return report
