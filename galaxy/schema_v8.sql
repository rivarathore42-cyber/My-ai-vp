-- galaxy_memory.db schema v8 (Phase 5, voice): per-owner contacts for 'call' and 'chat' commands. Additive.
-- Phone numbers are personal data: every row is keyed by the owner, and the storage layer refuses anonymous access.
CREATE TABLE IF NOT EXISTS contacts (
    user_id       TEXT NOT NULL CHECK (length(user_id) BETWEEN 1 AND 64),
    alias_key     TEXT NOT NULL CHECK (length(alias_key) BETWEEN 1 AND 64),
    display_name  TEXT NOT NULL CHECK (length(display_name) <= 64),
    phone         TEXT NOT NULL CHECK (length(phone) BETWEEN 8 AND 17),
    created_at    REAL NOT NULL,
    PRIMARY KEY (user_id, alias_key)
);
