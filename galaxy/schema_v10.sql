-- galaxy_memory.db schema v10 (Phase 7): API identity tokens. Only the SHA-256 hash of a token is stored. Additive.
CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash  TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
    token_id    TEXT NOT NULL UNIQUE CHECK (length(token_id) = 12),
    user_id     TEXT NOT NULL CHECK (length(user_id) BETWEEN 1 AND 64),
    created_at  REAL NOT NULL,
    revoked_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);
