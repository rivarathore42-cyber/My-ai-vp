-- galaxy_memory.db schema v6 (Phase 3): native execution log and analytics cache. Additive: no v1-v5 table changes.
CREATE TABLE IF NOT EXISTS native_runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bridge         TEXT NOT NULL CHECK (length(bridge) <= 60),
    op             TEXT NOT NULL CHECK (length(op) <= 40),
    status         TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout', 'crashed', 'invalid', 'unavailable',
                                                   'circuit_open', 'fallback')),
    n_inputs       INTEGER NOT NULL CHECK (n_inputs >= 0),
    duration_ms    REAL NOT NULL CHECK (duration_ms >= 0),
    stderr_excerpt TEXT NOT NULL CHECK (length(stderr_excerpt) <= 300),
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_native_runs_status ON native_runs (status, run_id);

CREATE TABLE IF NOT EXISTS analytics_cache (
    cache_key      TEXT PRIMARY KEY CHECK (length(cache_key) = 64),
    dataset_sha256 TEXT NOT NULL CHECK (length(dataset_sha256) = 64),
    result_json    TEXT NOT NULL CHECK (length(result_json) <= 200000),
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analytics_expiry ON analytics_cache (expires_at);
