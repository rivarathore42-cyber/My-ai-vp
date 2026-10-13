-- galaxy_memory.db schema v9 (Phase 6): Friend Mode profile, Always-Allow grants, and Android permission requests.
-- Additive. Every row is keyed by the owner; the storage layer refuses anonymous access.
CREATE TABLE IF NOT EXISTS friend_profiles (
    user_id     TEXT PRIMARY KEY CHECK (length(user_id) BETWEEN 1 AND 64),
    nickname    TEXT NOT NULL DEFAULT '' CHECK (length(nickname) <= 24),
    tone        TEXT NOT NULL DEFAULT 'warm' CHECK (tone IN ('warm', 'casual', 'formal')),
    boundaries  TEXT NOT NULL DEFAULT '[]' CHECK (length(boundaries) <= 1024),
    enabled     INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS permission_grants (
    user_id     TEXT NOT NULL CHECK (length(user_id) BETWEEN 1 AND 64),
    kind        TEXT NOT NULL CHECK (length(kind) <= 32),
    granted_at  REAL NOT NULL,
    revoked_at  REAL,
    PRIMARY KEY (user_id, kind)
);
CREATE TABLE IF NOT EXISTS android_requests (
    request_id   TEXT PRIMARY KEY CHECK (length(request_id) = 32),
    user_id      TEXT NOT NULL CHECK (length(user_id) BETWEEN 1 AND 64),
    kind         TEXT NOT NULL CHECK (length(kind) <= 32),
    title        TEXT NOT NULL CHECK (length(title) <= 160),
    why          TEXT NOT NULL CHECK (length(why) <= 400),
    preview      TEXT NOT NULL DEFAULT '' CHECK (length(preview) <= 500),
    risk         TEXT NOT NULL CHECK (risk IN ('low', 'high')),
    can_always   INTEGER NOT NULL DEFAULT 0 CHECK (can_always IN (0, 1)),
    action_json  TEXT NOT NULL CHECK (length(action_json) <= 4096),
    status       TEXT NOT NULL CHECK (status IN ('pending', 'denied', 'executed', 'expired')),
    decision     TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    decided_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_android_requests_user ON android_requests(user_id, created_at);
