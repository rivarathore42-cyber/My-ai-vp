-- galaxy_memory.db schema, version 1. Applied idempotently by galaxy/memory.py on every start.
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY CHECK (length(user_id) BETWEEN 1 AND 64),
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_attributes (
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    key         TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 64),
    value       TEXT NOT NULL CHECK (length(value) <= 2000),
    updated_at  REAL NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS conversations (
    conv_id     TEXT PRIMARY KEY CHECK (length(conv_id) BETWEEN 1 AND 130),
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    msg_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id     TEXT NOT NULL REFERENCES conversations(conv_id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL CHECK (length(content) <= 8000),
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conv_id, msg_id);

CREATE TABLE IF NOT EXISTS agent_calls (
    call_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
    elapsed_ms  REAL NOT NULL CHECK (elapsed_ms >= 0),
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_agent ON agent_calls (agent, call_id);

CREATE TABLE IF NOT EXISTS error_log (
    signature       TEXT PRIMARY KEY,
    agent           TEXT NOT NULL,
    exc_type        TEXT NOT NULL,
    sample_message  TEXT NOT NULL,
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    count           INTEGER NOT NULL DEFAULT 1 CHECK (count >= 1),
    healed          INTEGER NOT NULL DEFAULT 0 CHECK (healed IN (0, 1))
);

CREATE TABLE IF NOT EXISTS tuning_log (
    tune_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    param       TEXT NOT NULL,
    old_value   REAL NOT NULL,
    new_value   REAL NOT NULL,
    reason      TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS search_cache (
    query_key   TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);
