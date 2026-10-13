-- galaxy_memory.db schema v3: local semantic memory (pure-Python TF-IDF index), verification log, reasoning traces.
CREATE TABLE IF NOT EXISTS semantic_documents (
    doc_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('note', 'document', 'fact')),
    source      TEXT NOT NULL DEFAULT '' CHECK (length(source) <= 200),
    body        TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
    term_count  INTEGER NOT NULL CHECK (term_count >= 0),
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_semdocs_user ON semantic_documents (user_id, doc_id);

-- Inverted index: one row per (user, term, document). Terms are whole words and character trigrams; tf is the count.
CREATE TABLE IF NOT EXISTS memory_trigrams (
    user_id  TEXT NOT NULL,
    term     TEXT NOT NULL CHECK (length(term) BETWEEN 1 AND 40),
    doc_id   INTEGER NOT NULL REFERENCES semantic_documents(doc_id) ON DELETE CASCADE,
    tf       REAL NOT NULL CHECK (tf > 0),
    PRIMARY KEY (user_id, term, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_memtri_doc ON memory_trigrams (doc_id);

CREATE TABLE IF NOT EXISTS verification_log (
    check_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    task_kind    TEXT NOT NULL CHECK (task_kind IN ('code', 'math', 'logic', 'knowledge', 'creative')),
    verdict      TEXT NOT NULL CHECK (verdict IN ('pass', 'revised', 'flagged', 'unreviewed', 'skipped')),
    critic_kind  TEXT NOT NULL CHECK (critic_kind IN ('deterministic', 'llm', 'none', 'skipped')),
    flaws        TEXT NOT NULL CHECK (length(flaws) <= 4000),
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reasoning_traces (
    trace_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    agent       TEXT NOT NULL CHECK (length(agent) <= 80),
    complexity  TEXT NOT NULL CHECK (complexity IN ('simple', 'standard', 'complex')),
    reasoning   TEXT NOT NULL CHECK (length(reasoning) <= 8000),
    complete    INTEGER NOT NULL CHECK (complete IN (0, 1)),
    created_at  REAL NOT NULL
);
