-- galaxy_memory.db schema v5 (Phase 2): optional expiry for semantic documents (TTL). Additive: existing rows keep NULL
-- (never expires).
ALTER TABLE semantic_documents ADD COLUMN expires_at REAL;
CREATE INDEX IF NOT EXISTS idx_semdocs_expiry ON semantic_documents (expires_at);
