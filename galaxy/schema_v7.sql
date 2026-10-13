-- galaxy_memory.db schema v7 (Phase 4): call-level queue and memory telemetry, error stack hashes, breaker events.
-- Additive: existing rows keep the defaults (queue 0, memory unknown, stack hash empty).
ALTER TABLE agent_calls ADD COLUMN queue_ms REAL NOT NULL DEFAULT 0 CHECK (queue_ms >= 0);
ALTER TABLE agent_calls ADD COLUMN memory_mb REAL;
ALTER TABLE error_log ADD COLUMN stack_hash TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS breaker_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    service     TEXT NOT NULL CHECK (length(service) <= 40),
    state       TEXT NOT NULL CHECK (state IN ('open', 'half_open', 'closed')),
    failures    INTEGER NOT NULL CHECK (failures >= 0),
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_breaker_service ON breaker_events (service, event_id);
