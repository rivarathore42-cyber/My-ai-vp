-- galaxy_memory.db schema v2: speculative fast-path cache. Applied in place to v1 databases by galaxy/memory.py.
CREATE TABLE IF NOT EXISTS fastpath_cache (
    query_hash       TEXT PRIMARY KEY,                       -- sha256(namespace | exact_query)
    namespace        TEXT NOT NULL CHECK (namespace IN ('knowledge', 'math')),
    exact_query      TEXT NOT NULL CHECK (length(exact_query) BETWEEN 3 AND 400),
    cached_response  TEXT NOT NULL CHECK (length(cached_response) <= 10000),   -- JSON {text, source, mode}
    hit_count        INTEGER NOT NULL DEFAULT 0 CHECK (hit_count >= 0),
    created_at       REAL NOT NULL,
    ttl              REAL NOT NULL,                          -- absolute expiry, epoch seconds
    last_hit_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_fastpath_ttl ON fastpath_cache (ttl);
