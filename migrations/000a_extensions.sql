-- 000a_extensions.sql — Postgres extensions for the shared viabe-team database.
--
-- pgvector: embeddings storage for the knowledge layers (L1-L4).
CREATE EXTENSION IF NOT EXISTS vector;

-- Apache AGE (graph extension) is NOT enabled here. It is deferred to VT-7
-- (Apache AGE setup): neither Supabase-hosted Postgres nor the standard CI
-- Postgres images bundle the AGE binary, so `CREATE EXTENSION age` requires a
-- server-level install handled by that task. Enabling it here would fail the
-- migration runner on every environment that lacks the binary.
