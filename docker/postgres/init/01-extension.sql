-- Enable pgvector on first boot. The pgvector/pgvector image ships the
-- extension binaries; this just registers it in the default database so both
-- DeerFlow's own persistence and the pterodactyl-rag vector store can use it.
--
-- Runs only when the data volume is empty (docker-entrypoint-initdb.d contract).
-- To re-run on an existing volume: `make pg-down` removes the volume, then
-- `make pg-up`. Or apply manually: CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vector;
