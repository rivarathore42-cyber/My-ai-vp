-- galaxy_memory.db schema v4 (Phase 1): honeypot log for the Security Sentinel. Additive: no v1-v3 table is changed.
CREATE TABLE IF NOT EXISTS security_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    action        TEXT NOT NULL CHECK (action IN ('blocked', 'sanitized')),
    flags         TEXT NOT NULL CHECK (length(flags) <= 500),
    prompt_sha256 TEXT NOT NULL CHECK (length(prompt_sha256) = 64),
    excerpt       TEXT NOT NULL CHECK (length(excerpt) <= 200),
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_secevents_user ON security_events (user_id, event_id);
