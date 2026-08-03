"""pgvector-backed VectorStore (design §4.2).

Uses an isolated ``pterodactyl_rag`` schema in the existing Postgres database.
``psycopg`` and ``pgvector`` are imported lazily inside methods so the module can
be imported in environments (e.g. the Gateway process) that do not install them.

Schema:
  documents(id, source_path UNIQUE, title, content_hash, tags TEXT[], mtime, ingested_at)
  chunks(id, document_id FK, chunk_index, heading_path, content, token_count,
         tags TEXT[], embedding vector(dim))  -- tags denormalized for single-scan filter
  meta(embed_model, embed_dim)                -- pins the index's embedding space
"""

from __future__ import annotations

import logging

from ..models import Chunk, Document
from ..tags import plugin_of
from .base import FacetValue, IndexStats, SearchResult
from .memory import cosine_similarity  # noqa: F401  (re-export for parity tests)

logger = logging.getLogger(__name__)

SCHEMA = "pterodactyl_rag"


class PgVectorStore:
    """Postgres + pgvector implementation of the VectorStore protocol."""

    def __init__(self, dsn: str, *, schema: str = SCHEMA) -> None:
        self._dsn = dsn
        self._schema = schema
        self._pool = None
        self._embed_dim: int | None = None

    async def _get_pool(self):
        if self._pool is None:
            from psycopg_pool import AsyncConnectionPool

            self._pool = AsyncConnectionPool(self._dsn, open=False, min_size=1, max_size=4)
            await self._pool.open()
        return self._pool

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _register_vector(self, conn) -> None:
        from pgvector.psycopg import register_vector_async

        await register_vector_async(conn)

    async def initialize(self, *, embed_model: str, embed_dim: int) -> None:
        pool = await self._get_pool()
        async with pool.connection() as conn:
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self._schema}")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await self._register_vector(conn)
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._schema}.documents (
                    id           BIGSERIAL PRIMARY KEY,
                    source_path  TEXT NOT NULL UNIQUE,
                    title        TEXT,
                    content_hash TEXT NOT NULL,
                    tags         TEXT[] NOT NULL DEFAULT '{{}}',
                    mtime        DOUBLE PRECISION,
                    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._schema}.chunks (
                    id           BIGSERIAL PRIMARY KEY,
                    document_id  BIGINT NOT NULL REFERENCES {self._schema}.documents(id) ON DELETE CASCADE,
                    chunk_index  INT NOT NULL,
                    heading_path TEXT,
                    content      TEXT NOT NULL,
                    token_count  INT,
                    tags         TEXT[] NOT NULL DEFAULT '{{}}',
                    embedding    vector({embed_dim}) NOT NULL,
                    UNIQUE (document_id, chunk_index)
                )
                """
            )
            await conn.execute(f"CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON {self._schema}.chunks USING hnsw (embedding vector_cosine_ops)")
            await conn.execute(f"CREATE INDEX IF NOT EXISTS chunks_tags_gin ON {self._schema}.chunks USING gin (tags)")
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._schema}.meta (
                    id          INT PRIMARY KEY DEFAULT 1,
                    embed_model TEXT NOT NULL,
                    embed_dim   INT NOT NULL,
                    CONSTRAINT meta_singleton CHECK (id = 1)
                )
                """
            )
            row = await (await conn.execute(f"SELECT embed_model, embed_dim FROM {self._schema}.meta WHERE id = 1")).fetchone()
            if row is None:
                await conn.execute(f"INSERT INTO {self._schema}.meta (id, embed_model, embed_dim) VALUES (1, %s, %s)", (embed_model, embed_dim))
            else:
                stored_model, stored_dim = row
                if stored_dim != embed_dim:
                    raise ValueError(f"Index dimension mismatch: stored {stored_dim}, configured {embed_dim}. Run `pterodactyl-rag reset` and re-ingest.")
                if stored_model != embed_model:
                    raise ValueError(f"Index model mismatch: stored {stored_model!r}, configured {embed_model!r}. Run `pterodactyl-rag reset` and re-ingest.")
            self._embed_dim = embed_dim

    async def get_document_hash(self, source_path: str) -> str | None:
        pool = await self._get_pool()
        async with pool.connection() as conn:
            row = await (await conn.execute(f"SELECT content_hash FROM {self._schema}.documents WHERE source_path = %s", (source_path,))).fetchone()
            return row[0] if row else None

    async def upsert_document(self, document: Document, chunks: list[Chunk]) -> None:
        from pgvector import Vector

        pool = await self._get_pool()
        async with pool.connection() as conn:
            await self._register_vector(conn)
            async with conn.transaction():
                row = await (
                    await conn.execute(
                        f"""
                        INSERT INTO {self._schema}.documents (source_path, title, content_hash, tags, mtime)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (source_path) DO UPDATE
                          SET title = EXCLUDED.title,
                              content_hash = EXCLUDED.content_hash,
                              tags = EXCLUDED.tags,
                              mtime = EXCLUDED.mtime,
                              ingested_at = now()
                        RETURNING id
                        """,
                        (document.source_path, document.title, document.content_hash, document.tags, document.mtime),
                    )
                ).fetchone()
                document_id = row[0]
                # Replace all chunks for this document.
                await conn.execute(f"DELETE FROM {self._schema}.chunks WHERE document_id = %s", (document_id,))
                for chunk in chunks:
                    if chunk.embedding is None:
                        raise ValueError("Chunk embedding is required for upsert")
                    await conn.execute(
                        f"""
                        INSERT INTO {self._schema}.chunks
                          (document_id, chunk_index, heading_path, content, token_count, tags, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (document_id, chunk.chunk_index, chunk.heading_path, chunk.content, chunk.token_count, chunk.tags, Vector(chunk.embedding)),
                    )

    async def delete_documents(self, source_paths: list[str]) -> int:
        if not source_paths:
            return 0
        pool = await self._get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(f"DELETE FROM {self._schema}.documents WHERE source_path = ANY(%s)", (source_paths,))
            return cur.rowcount

    async def list_source_paths(self) -> list[str]:
        pool = await self._get_pool()
        async with pool.connection() as conn:
            rows = await (await conn.execute(f"SELECT source_path FROM {self._schema}.documents ORDER BY source_path")).fetchall()
            return [r[0] for r in rows]

    async def _run_search(self, conn, query_embedding, top_k: int, tags: list[str] | None):
        from pgvector import Vector

        params: list = [Vector(query_embedding)]
        where = ""
        if tags:
            where = "WHERE c.tags @> %s"
            params.append(tags)
        params_tail = [Vector(query_embedding), top_k]
        sql = f"""
            SELECT d.source_path, d.title, c.chunk_index, c.heading_path, c.content, c.tags,
                   1 - (c.embedding <=> %s) AS score
            FROM {self._schema}.chunks c
            JOIN {self._schema}.documents d ON d.id = c.document_id
            {where}
            ORDER BY c.embedding <=> %s
            LIMIT %s
        """
        rows = await (await conn.execute(sql, [*params, *params_tail])).fetchall()
        return rows

    async def search(self, query_embedding: list[float], *, top_k: int, tags: list[str] | None = None) -> SearchResult:
        from .base import SearchHit  # local import to avoid cycle at module load

        pool = await self._get_pool()
        async with pool.connection() as conn:
            await self._register_vector(conn)
            applied = list(tags or [])
            rows = await self._run_search(conn, query_embedding, top_k, applied or None)
            relaxed = False
            if applied and not rows:
                rows = await self._run_search(conn, query_embedding, top_k, None)
                relaxed = True

        hits = [
            SearchHit(
                plugin=plugin_of(list(r[5])),
                title=r[1],
                heading_path=r[3],
                source_path=r[0],
                chunk_index=r[2],
                score=round(max(float(r[6]), 0.0), 6),
                snippet=r[4],
                tags=list(r[5]),
            )
            for r in rows
        ]
        return SearchResult(hits=hits, applied_tags=applied, relaxed=relaxed)

    async def get_document_text(self, source_path: str, *, max_chars: int) -> str | None:
        pool = await self._get_pool()
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    f"""
                    SELECT c.content FROM {self._schema}.chunks c
                    JOIN {self._schema}.documents d ON d.id = c.document_id
                    WHERE d.source_path = %s ORDER BY c.chunk_index
                    """,
                    (source_path,),
                )
            ).fetchall()
        if not rows:
            return None
        return "\n\n".join(r[0] for r in rows)[:max_chars]

    async def list_sources(self, *, plugin: str | None = None) -> list[dict]:
        pool = await self._get_pool()
        async with pool.connection() as conn:
            where = ""
            params: list = []
            if plugin is not None:
                where = "WHERE d.tags @> %s"
                params.append([f"plugin:{plugin}"])
            rows = await (
                await conn.execute(
                    f"""
                    SELECT d.source_path, d.title, d.tags, count(c.id) AS chunks
                    FROM {self._schema}.documents d
                    LEFT JOIN {self._schema}.chunks c ON c.document_id = d.id
                    {where}
                    GROUP BY d.id, d.source_path, d.title, d.tags
                    ORDER BY d.source_path
                    """,
                    params,
                )
            ).fetchall()
        return [{"source_path": r[0], "title": r[1], "plugin": plugin_of(list(r[2])), "tags": list(r[2]), "chunks": r[3]} for r in rows]

    async def list_facets(self, *, namespace: str | None = None) -> dict[str, list[FacetValue]]:
        pool = await self._get_pool()
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    f"""
                    SELECT tag, count(*) AS docs FROM (
                        SELECT DISTINCT id, unnest(tags) AS tag FROM {self._schema}.documents
                    ) t
                    GROUP BY tag
                    """
                )
            ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for tag, docs in rows:
            ns, _, val = str(tag).partition(":")
            if not val:
                ns, val = "_", str(tag)
            if namespace is not None and ns != namespace:
                continue
            result.setdefault(ns, {})[val] = docs
        return {ns: [FacetValue(value=v, docs=n) for v, n in sorted(vals.items(), key=lambda kv: (-kv[1], kv[0]))] for ns, vals in result.items()}

    async def stats(self) -> IndexStats:
        pool = await self._get_pool()
        async with pool.connection() as conn:
            docs = (await (await conn.execute(f"SELECT count(*) FROM {self._schema}.documents")).fetchone())[0]
            chunks = (await (await conn.execute(f"SELECT count(*) FROM {self._schema}.chunks")).fetchone())[0]
            meta = await (await conn.execute(f"SELECT embed_model, embed_dim FROM {self._schema}.meta WHERE id = 1")).fetchone()
            last = await (await conn.execute(f"SELECT max(ingested_at)::text FROM {self._schema}.documents")).fetchone()
        return IndexStats(
            documents=docs,
            chunks=chunks,
            embed_model=meta[0] if meta else None,
            embed_dim=meta[1] if meta else None,
            last_ingest=last[0] if last else None,
        )

    async def reset(self) -> None:
        """Drop the entire schema (used by ``pterodactyl-rag reset``)."""
        pool = await self._get_pool()
        async with pool.connection() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {self._schema} CASCADE")
        self._embed_dim = None
