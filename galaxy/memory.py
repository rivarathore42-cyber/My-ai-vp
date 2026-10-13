"""galaxy_memory.db - SQLite persistence in WAL mode. Parameterised SQL only, one lock around the connection."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time

from .identity import new_user_token, token_hash, token_id
from .security import DEFAULT_TOKEN_TTL_S, clamp_ttl, is_live
from pathlib import Path
from typing import Any, Optional

from .core import PACKAGE_DIR, owner_id

SCHEMA_PATH = PACKAGE_DIR / "schema.sql"
MIGRATIONS = [(2, PACKAGE_DIR / "schema_v2.sql"), (3, PACKAGE_DIR / "schema_v3.sql"), (4, PACKAGE_DIR / "schema_v4.sql"),
              (5, PACKAGE_DIR / "schema_v5.sql"), (6, PACKAGE_DIR / "schema_v6.sql"), (7, PACKAGE_DIR / "schema_v7.sql"),
              (8, PACKAGE_DIR / "schema_v8.sql"), (9, PACKAGE_DIR / "schema_v9.sql"), (10, PACKAGE_DIR / "schema_v10.sql"), (11, PACKAGE_DIR / "schema_v11.sql")]
SCHEMA_VERSION = 11
_TABLES = ("users", "user_attributes", "conversations", "messages", "agent_calls", "error_log", "tuning_log",
           "search_cache", "fastpath_cache", "semantic_documents", "memory_trigrams", "verification_log",
           "reasoning_traces", "security_events", "native_runs", "analytics_cache", "breaker_events", "contacts",
           "friend_profiles", "permission_grants", "android_requests",
           "api_tokens")


def _percentile(sorted_values: list[float], pct: float) -> Optional[float]:
    if not sorted_values:
        return None
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return round(sorted_values[rank - 1], 3)


def _check_user(user_id) -> str:
    """RC-03: every semantic-memory access must name a validated owner. Anonymous, absent or malformed ids are refused
    here, in the storage layer, so no caller can bypass the orchestrator check."""
    if owner_id(user_id) is None:
        raise ValueError("unauthenticated memory access refused: a valid user id is required")
    return user_id


class MemoryStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL",
                       "PRAGMA foreign_keys=ON", "PRAGMA busy_timeout=5000"):
            self._conn.execute(pragma)
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))  # v1 base tables, idempotent
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            for _, path in MIGRATIONS:
                self._conn.executescript(path.read_text(encoding="utf-8"))
            self._conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        else:
            current = row["version"]
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} has schema v{current}, code expects v{SCHEMA_VERSION}")
            if current < SCHEMA_VERSION:  # in-place upgrade: existing rows are untouched
                for version, path in MIGRATIONS:
                    if version > current:
                        self._conn.executescript(path.read_text(encoding="utf-8"))
                self._conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    # --- users and attributes -------------------------------------------------
    def touch_user(self, user_id: str) -> None:
        now = time.time()
        self._write("INSERT INTO users(user_id, created_at, last_seen) VALUES (?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen", (user_id, now, now))

    def set_attr(self, user_id: str, key: str, value: str) -> None:
        self.touch_user(user_id)
        self._write("INSERT INTO user_attributes(user_id,key,value,updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (user_id, key, value[:2000], time.time()))

    def get_attr(self, user_id: str, key: str) -> Optional[str]:
        rows = self._read("SELECT value FROM user_attributes WHERE user_id=? AND key=?", (user_id, key))
        return rows[0]["value"] if rows else None

    def all_attrs(self, user_id: str) -> dict[str, str]:
        rows = self._read("SELECT key, value FROM user_attributes WHERE user_id=?", (user_id,))
        return {r["key"]: r["value"] for r in rows}

    # --- conversations and short-term context --------------------------------
    def ensure_conversation(self, conv_id: str, user_id: str) -> None:
        self.touch_user(user_id)
        self._write("INSERT OR IGNORE INTO conversations(conv_id,user_id,created_at) VALUES (?,?,?)",
                    (conv_id, user_id, time.time()))

    def add_message(self, conv_id: str, role: str, content: str) -> None:
        self._write("INSERT INTO messages(conv_id,role,content,created_at) VALUES (?,?,?,?)",
                    (conv_id, role, content[:8000], time.time()))

    def recent_messages(self, conv_id: str, limit: int = 12) -> list[tuple[str, str]]:
        rows = self._read("SELECT role, content FROM messages WHERE conv_id=? ORDER BY msg_id DESC LIMIT ?",
                          (conv_id, limit))
        return [(r["role"], r["content"]) for r in reversed(rows)]

    def prune_messages(self, conv_id: str, keep: int = 500) -> None:
        self._write("DELETE FROM messages WHERE conv_id=? AND msg_id NOT IN "
                    "(SELECT msg_id FROM messages WHERE conv_id=? ORDER BY msg_id DESC LIMIT ?)",
                    (conv_id, conv_id, keep))

    # --- telemetry -----------------------------------------------------------
    def log_call(self, agent: str, ok: bool, elapsed_ms: float, queue_ms: float = 0.0,
                 memory_mb: Optional[float] = None) -> None:
        self._write("INSERT INTO agent_calls(agent,ok,elapsed_ms,created_at,queue_ms,memory_mb) VALUES (?,?,?,?,?,?)",
                    (agent[:80], 1 if ok else 0, float(elapsed_ms), time.time(), max(0.0, float(queue_ms)),
                     None if memory_mb is None else float(memory_mb)))

    def agent_summary(self, window: int = 200) -> dict[str, dict[str, Any]]:
        rows: list = []  # A18-02: each agent keeps its own last `window` calls, whatever the other agents did
        for name_row in self._read("SELECT DISTINCT agent FROM agent_calls", ()):
            rows.extend(self._read("SELECT agent, ok, elapsed_ms, queue_ms, memory_mb FROM agent_calls "
                                   "WHERE agent=? ORDER BY call_id DESC LIMIT ?", (name_row["agent"], window)))
        grouped: dict[str, list] = {}
        for row in rows:
            bucket = grouped.setdefault(row["agent"], [])
            if len(bucket) < window:
                bucket.append(row)
        summary: dict[str, dict[str, Any]] = {}
        for name, items in grouped.items():
            latencies = sorted(r["elapsed_ms"] for r in items if r["ok"])
            queues = sorted(r["queue_ms"] for r in items)
            memory = [r["memory_mb"] for r in items if r["memory_mb"] is not None]
            summary[name] = {"n": len(items), "success_rate": round(sum(r["ok"] for r in items) / len(items), 4),
                             "p50_ms": _percentile(latencies, 50), "p95_ms": _percentile(latencies, 95),
                             "queue_p95_ms": _percentile(queues, 95), "memory_mb_last": memory[0] if memory else None}
        return summary

    def latency_window(self, agent: str, offset: int, limit: int) -> list[float]:
        rows = self._read("SELECT elapsed_ms FROM agent_calls WHERE agent=? AND ok=1 ORDER BY call_id DESC LIMIT ? OFFSET ?",
                          (agent, limit, offset))
        return [float(r["elapsed_ms"]) for r in rows]

    def call_stats(self, agent: Optional[str] = None, window: int = 200) -> dict[str, Any]:
        if agent is None:
            rows = self._read("SELECT ok, elapsed_ms FROM agent_calls ORDER BY call_id DESC LIMIT ?", (window,))
        else:
            rows = self._read("SELECT ok, elapsed_ms FROM agent_calls WHERE agent=? ORDER BY call_id DESC LIMIT ?",
                              (agent, window))
        n = len(rows)
        if n == 0:
            return {"n": 0, "success_rate": None, "p50_ms": None, "p95_ms": None}
        latencies = sorted(r["elapsed_ms"] for r in rows if r["ok"])
        return {"n": n, "success_rate": round(sum(r["ok"] for r in rows) / n, 4),
                "p50_ms": _percentile(latencies, 50), "p95_ms": _percentile(latencies, 95)}

    def prune_calls(self, keep: int = 20000) -> None:
        self._write("DELETE FROM agent_calls WHERE call_id NOT IN "
                    "(SELECT call_id FROM agent_calls ORDER BY call_id DESC LIMIT ?)", (keep,))

    # --- error tracking ------------------------------------------------------
    def log_error(self, signature: str, agent: str, exc_type: str, message: str, stack_hash: str = "") -> int:
        now = time.time()
        self._write("INSERT INTO error_log(signature,agent,exc_type,sample_message,first_seen,last_seen,count,stack_hash) "
                    "VALUES (?,?,?,?,?,?,1,?) ON CONFLICT(signature) DO UPDATE SET "
                    "count=count+1, last_seen=excluded.last_seen",
                    (signature, agent, exc_type, message[:300], now, now, stack_hash[:32]))
        return self._read("SELECT count FROM error_log WHERE signature=?", (signature,))[0]["count"]

    def mark_healed(self, signature: str) -> None:
        self._write("UPDATE error_log SET healed=1 WHERE signature=?", (signature,))

    def top_errors(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._read("SELECT signature, agent, exc_type, sample_message, count, healed, last_seen "
                          "FROM error_log ORDER BY count DESC, last_seen DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # --- parameter tuning history --------------------------------------------
    def log_tuning(self, param: str, old: float, new: float, reason: str) -> None:
        self._write("INSERT INTO tuning_log(param,old_value,new_value,reason,created_at) VALUES (?,?,?,?,?)",
                    (param, float(old), float(new), reason[:300], time.time()))

    def last_tuning_time(self) -> Optional[float]:
        return self._read("SELECT MAX(created_at) AS t FROM tuning_log")[0]["t"]

    def tuning_history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._read("SELECT param, old_value, new_value, reason, created_at FROM tuning_log "
                          "ORDER BY tune_id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # --- search cache --------------------------------------------------------
    def cache_get(self, key: str, max_age: float) -> Optional[tuple[str, Any]]:
        rows = self._read("SELECT source, payload, fetched_at FROM search_cache WHERE query_key=?", (key,))
        if not rows or time.time() - rows[0]["fetched_at"] > max_age:
            return None
        return rows[0]["source"], json.loads(rows[0]["payload"])

    def cache_put(self, key: str, source: str, payload: Any) -> None:
        self._write("INSERT INTO search_cache(query_key,source,payload,fetched_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(query_key) DO UPDATE SET source=excluded.source, "
                    "payload=excluded.payload, fetched_at=excluded.fetched_at",
                    (key, source, json.dumps(payload, ensure_ascii=False), time.time()))

    # --- speculative fast-path cache (schema v2) ------------------------------
    def fastpath_get(self, query_hash: str, now: float) -> Optional[dict[str, Any]]:
        rows = self._read("SELECT query_hash, namespace, exact_query, cached_response, hit_count, ttl FROM fastpath_cache "
                          "WHERE query_hash=? AND ttl>?", (query_hash, now))
        return dict(rows[0]) if rows else None

    def fastpath_touch(self, query_hash: str, now: float) -> int:
        self._write("UPDATE fastpath_cache SET hit_count=hit_count+1, last_hit_at=? WHERE query_hash=?",
                    (now, query_hash))
        rows = self._read("SELECT hit_count FROM fastpath_cache WHERE query_hash=?", (query_hash,))
        return rows[0]["hit_count"] if rows else 0

    def fastpath_put(self, query_hash: str, namespace: str, exact_query: str, cached_response: str,
                     now: float, ttl: float) -> None:
        self._write("INSERT INTO fastpath_cache(query_hash,namespace,exact_query,cached_response,hit_count,"
                    "created_at,ttl,last_hit_at) VALUES (?,?,?,?,0,?,?,NULL) ON CONFLICT(query_hash) DO UPDATE SET "
                    "cached_response=excluded.cached_response, exact_query=excluded.exact_query, "
                    "created_at=excluded.created_at, ttl=excluded.ttl",
                    (query_hash, namespace, exact_query, cached_response, now, ttl))

    def fastpath_prune(self, now: float) -> None:
        self._write("DELETE FROM fastpath_cache WHERE ttl<=?", (now,))

    def fastpath_stats(self, now: float) -> dict[str, int]:
        row = self._read("SELECT COUNT(*) AS entries, COALESCE(SUM(hit_count),0) AS hits, "
                         "SUM(CASE WHEN ttl>? THEN 1 ELSE 0 END) AS live FROM fastpath_cache", (now,))[0]
        return {"entries": row["entries"], "live_entries": row["live"] or 0, "total_hits": row["hits"]}

    def schema_version(self) -> int:
        return self._read("SELECT version FROM schema_version")[0]["version"]

    # --- semantic memory, verification log, reasoning traces (schema v3) -----
    @staticmethod
    def _chunks(items, size: int = 400):
        items = list(items)
        for start in range(0, len(items), size):
            yield items[start:start + size]

    def doc_count(self, user_id: str) -> int:
        _check_user(user_id)
        return self._read("SELECT COUNT(*) AS c FROM semantic_documents WHERE user_id=?", (user_id,))[0]["c"]

    def doc_add(self, user_id: str, kind: str, source: str, body: str, term_count: int, terms: dict,
                expires_at: Optional[float] = None) -> int:
        _check_user(user_id)
        self.touch_user(user_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "INSERT INTO semantic_documents(user_id,kind,source,body,term_count,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                    (user_id, kind, source, body, term_count, time.time(), expires_at))
                doc_id = cur.lastrowid
                self._conn.executemany(
                    "INSERT INTO memory_trigrams(user_id,term,doc_id,tf) VALUES (?,?,?,?)",
                    [(user_id, term, doc_id, float(tf)) for term, tf in terms.items() if tf > 0])
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return doc_id

    def doc_prune_expired(self, now: float) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM semantic_documents WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
            return cur.rowcount

    def doc_delete(self, user_id: str, doc_id: int) -> bool:
        _check_user(user_id)
        with self._lock:
            cur = self._conn.execute("DELETE FROM semantic_documents WHERE user_id=? AND doc_id=?", (user_id, doc_id))
            return cur.rowcount > 0

    def doc_list(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        _check_user(user_id)
        rows = self._read("SELECT doc_id, kind, source, substr(body,1,120) AS preview, created_at FROM semantic_documents "
                          "WHERE user_id=? ORDER BY doc_id DESC LIMIT ?", (user_id, limit))
        return [dict(r) for r in rows]

    def doc_candidates(self, user_id: str, terms: list, limit: int) -> list[int]:
        _check_user(user_id)
        found: set[int] = set()
        for chunk in self._chunks(terms):
            marks = ",".join("?" * len(chunk))
            for row in self._read(f"SELECT DISTINCT doc_id FROM memory_trigrams WHERE user_id=? AND term IN ({marks})",
                                  (user_id, *chunk)):
                found.add(row["doc_id"])
                if len(found) >= limit:
                    return sorted(found)
        return sorted(found)

    def doc_terms_for(self, user_id: str, doc_ids: list) -> list[tuple]:
        _check_user(user_id)
        out: list[tuple] = []
        for chunk in self._chunks(doc_ids):
            marks = ",".join("?" * len(chunk))
            rows = self._read(f"SELECT doc_id, term, tf FROM memory_trigrams WHERE user_id=? AND doc_id IN ({marks})",
                              (user_id, *chunk))
            out.extend((r["doc_id"], r["term"], r["tf"]) for r in rows)
        return out

    def doc_df(self, user_id: str, terms: list) -> dict[str, int]:
        _check_user(user_id)
        df: dict[str, int] = {}
        for chunk in self._chunks(terms):
            marks = ",".join("?" * len(chunk))
            for row in self._read(f"SELECT term, COUNT(*) AS df FROM memory_trigrams WHERE user_id=? AND term IN ({marks}) "
                                  "GROUP BY term", (user_id, *chunk)):
                df[row["term"]] = row["df"]
        return df

    def doc_bodies(self, user_id: str, doc_ids: list) -> list[dict[str, Any]]:
        _check_user(user_id)
        out: list[dict[str, Any]] = []
        for chunk in self._chunks(doc_ids):
            marks = ",".join("?" * len(chunk))
            rows = self._read(f"SELECT doc_id, kind, source, body FROM semantic_documents WHERE user_id=? AND doc_id IN ({marks})",
                              (user_id, *chunk))
            out.extend(dict(r) for r in rows)
        return out

    def log_verification(self, task_kind: str, verdict: str, critic_kind: str, flaws: list) -> None:
        self._write("INSERT INTO verification_log(task_kind,verdict,critic_kind,flaws,created_at) VALUES (?,?,?,?,?)",
                    (task_kind, verdict, critic_kind, "\n".join(str(f) for f in flaws)[:4000], time.time()))

    def verification_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self._read("SELECT task_kind, verdict, critic_kind, flaws, created_at FROM verification_log "
                                            "ORDER BY check_id DESC LIMIT ?", (limit,))]

    def log_trace(self, user_id: str, agent: str, complexity: str, reasoning: str, complete: bool) -> None:
        self._write("INSERT INTO reasoning_traces(user_id,agent,complexity,reasoning,complete,created_at) VALUES (?,?,?,?,?,?)",
                    (user_id, agent[:80], complexity, reasoning[:8000], 1 if complete else 0, time.time()))
        self._write("DELETE FROM reasoning_traces WHERE trace_id NOT IN "
                    "(SELECT trace_id FROM reasoning_traces ORDER BY trace_id DESC LIMIT 2000)")

    # --- fast-path hit counter (write-through) and honeypot log (schema v4) --
    def fastpath_bump(self, query_hash: str, now: float) -> None:
        self._write("UPDATE fastpath_cache SET hit_count=hit_count+1, last_hit_at=? WHERE query_hash=?", (now, query_hash))

    def log_security_event(self, user_id: str, severity: str, action: str, flags: str, prompt_sha256: str,
                           excerpt: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO security_events(user_id,severity,action,flags,prompt_sha256,excerpt,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (user_id, severity, action, flags[:500], prompt_sha256, excerpt[:200], time.time()))
            return cur.lastrowid

    def security_recent(self, user_id: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        if user_id is None:
            rows = self._read("SELECT event_id, user_id, severity, action, flags, created_at FROM security_events "
                              "ORDER BY event_id DESC LIMIT ?", (limit,))
        else:
            rows = self._read("SELECT event_id, user_id, severity, action, flags, created_at FROM security_events "
                              "WHERE user_id=? ORDER BY event_id DESC LIMIT ?", (user_id, limit))
        return [dict(r) for r in rows]

    def security_count(self) -> int:
        return self._read("SELECT COUNT(*) AS c FROM security_events")[0]["c"]

    # --- native execution log and analytics cache (schema v6) -----------------
    def log_native_run(self, bridge: str, op: str, status: str, n_inputs: int, duration_ms: float,
                       stderr_excerpt: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO native_runs(bridge,op,status,n_inputs,duration_ms,stderr_excerpt,created_at) VALUES (?,?,?,?,?,?,?)",
                (bridge[:60], op[:40], status, max(0, int(n_inputs)), max(0.0, float(duration_ms)),
                 stderr_excerpt[:300], time.time()))
            return cur.lastrowid

    def native_runs_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self._read(
            "SELECT run_id, bridge, op, status, n_inputs, duration_ms, stderr_excerpt, created_at FROM native_runs "
            "ORDER BY run_id DESC LIMIT ?", (limit,))]

    def native_status_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._read("SELECT status, COUNT(*) AS n FROM native_runs GROUP BY status", ())}

    def analytics_get(self, key: str, now: float) -> Optional[str]:
        rows = self._read("SELECT result_json FROM analytics_cache WHERE cache_key=? AND expires_at>?", (key, now))
        return rows[0]["result_json"] if rows else None

    def analytics_put(self, key: str, dataset_sha256: str, result_json: str, now: float, ttl_seconds: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO analytics_cache(cache_key,dataset_sha256,result_json,created_at,expires_at) VALUES (?,?,?,?,?)",
                (key, dataset_sha256, result_json, now, now + ttl_seconds))

    def analytics_prune(self, now: float) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM analytics_cache WHERE expires_at <= ?", (now,)).rowcount

    # --- circuit breaker events (schema v7) ----------------------------------
    def log_breaker_event(self, service: str, state: str, failures: int) -> None:
        self._write("INSERT INTO breaker_events(service,state,failures,created_at) VALUES (?,?,?,?)",
                    (service[:40], state, max(0, int(failures)), time.time()))

    def breaker_events_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self._read("SELECT service, state, failures, created_at FROM breaker_events "
                                            "ORDER BY event_id DESC LIMIT ?", (limit,))]

    # --- contacts for voice commands (schema v8): owner-only -----------------
    @staticmethod
    def _alias(display_name: str) -> str:
        key = " ".join((display_name or "").lower().split())[:64]
        if not key:
            raise ValueError("empty contact name")
        return key

    def contact_save(self, user_id: str, display_name: str, phone: str) -> None:
        _check_user(user_id)
        key = self._alias(display_name)
        with self._lock:
            self._conn.execute(
                "INSERT INTO contacts(user_id,alias_key,display_name,phone,created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(user_id,alias_key) DO UPDATE SET display_name=excluded.display_name, phone=excluded.phone",
                (user_id, key, display_name.strip()[:64], phone, time.time()))

    def contact_find(self, user_id: str, display_name: str) -> Optional[dict[str, Any]]:
        _check_user(user_id)
        rows = self._read("SELECT display_name, phone FROM contacts WHERE user_id=? AND alias_key=?",
                          (user_id, self._alias(display_name)))
        return dict(rows[0]) if rows else None

    # --- Friend Mode, permission grants and Android requests (schema v9): owner-only ----
    def profile_get(self, user_id: str) -> Optional[dict[str, Any]]:
        _check_user(user_id)
        rows = self._read("SELECT nickname, tone, boundaries, enabled FROM friend_profiles WHERE user_id=?", (user_id,))
        if not rows:
            return None
        row = rows[0]
        return {"nickname": row["nickname"], "tone": row["tone"], "boundaries": json.loads(row["boundaries"] or "[]"),
                "enabled": bool(row["enabled"])}

    def profile_save(self, user_id: str, profile: dict[str, Any]) -> None:
        _check_user(user_id)
        with self._lock:
            self._conn.execute(
                "INSERT INTO friend_profiles(user_id,nickname,tone,boundaries,enabled,updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET nickname=excluded.nickname, tone=excluded.tone, "
                "boundaries=excluded.boundaries, enabled=excluded.enabled, updated_at=excluded.updated_at",
                (user_id, profile["nickname"], profile["tone"],
                 json.dumps(list(profile["boundaries"]), ensure_ascii=False), 1 if profile["enabled"] else 0, time.time()))

    def grant_active(self, user_id: str, kind: str) -> bool:
        _check_user(user_id)
        return bool(self._read("SELECT 1 FROM permission_grants WHERE user_id=? AND kind=? AND revoked_at IS NULL",
                               (user_id, kind)))

    def grant_set(self, user_id: str, kind: str) -> None:
        _check_user(user_id)
        with self._lock:
            self._conn.execute(
                "INSERT INTO permission_grants(user_id,kind,granted_at,revoked_at) VALUES (?,?,?,NULL) "
                "ON CONFLICT(user_id,kind) DO UPDATE SET granted_at=excluded.granted_at, revoked_at=NULL",
                (user_id, kind, time.time()))

    def grant_revoke(self, user_id: str, kind: str) -> bool:
        _check_user(user_id)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE permission_grants SET revoked_at=? WHERE user_id=? AND kind=? AND revoked_at IS NULL",
                (time.time(), user_id, kind))
        return cur.rowcount > 0

    def request_create(self, user_id: str, row: dict[str, Any]) -> None:
        _check_user(user_id)
        with self._lock:
            self._conn.execute(
                "INSERT INTO android_requests(request_id,user_id,kind,title,why,preview,risk,can_always,action_json,"
                "status,decision,created_at,expires_at,token_id,signature) VALUES (?,?,?,?,?,?,?,?,?,'pending','',?,?,?,?)",
                (row["request_id"], user_id, row["kind"], row["title"], row["why"], row["preview"], row["risk"],
                 row["can_always"], row["action_json"], row["created_at"], row["expires_at"], row.get("token_id"),
                 row["signature"]))

    def request_get(self, user_id: str, request_id: str) -> Optional[dict[str, Any]]:
        _check_user(user_id)
        rows = self._read("SELECT * FROM android_requests WHERE user_id=? AND request_id=?", (user_id, request_id))
        return dict(rows[0]) if rows else None

    def request_claim(self, user_id: str, request_id: str, new_status: str, decision: str, now: float) -> bool:
        """Atomic single-use claim: only a pending, unexpired request moves on. True exactly once."""
        _check_user(user_id)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE android_requests SET status=?, decision=?, decided_at=? "
                "WHERE user_id=? AND request_id=? AND status='pending' AND expires_at >= ?",
                (new_status, decision, now, user_id, request_id, now))
        return cur.rowcount == 1

    def request_expire(self, user_id: str, request_id: str, now: float) -> None:
        _check_user(user_id)
        with self._lock:
            self._conn.execute("UPDATE android_requests SET status='expired', decided_at=? "
                               "WHERE user_id=? AND request_id=? AND status='pending'", (now, user_id, request_id))

    def request_list(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        _check_user(user_id)
        rows = self._read("SELECT kind, title, status, decision, created_at, decided_at FROM android_requests "
                          "WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, max(1, min(int(limit), 100))))
        return [dict(r) for r in rows]

    # --- API identity tokens (schema v10/v11): only hashes are stored, every token expires, tokens rotate -------------
    def token_issue(self, user_id: str, ttl_s: Optional[int] = None) -> str:
        _check_user(user_id)
        now = time.time()
        ttl = clamp_ttl(DEFAULT_TOKEN_TTL_S if ttl_s is None else ttl_s)
        token = new_user_token()
        with self._lock:
            self._conn.execute("INSERT INTO api_tokens(token_hash,token_id,user_id,created_at,expires_at) VALUES (?,?,?,?,?)",
                               (token_hash(token), token_id(token), owner_id(user_id), now, now + ttl))
        return token

    def token_resolve(self, token: str, now: Optional[float] = None) -> Optional[str]:
        if not token:
            return None
        now = time.time() if now is None else now
        rows = self._read("SELECT user_id, expires_at, revoked_at FROM api_tokens WHERE token_hash=?", (token_hash(token),))
        if not rows or not is_live(rows[0]["expires_at"], rows[0]["revoked_at"], now):
            return None
        return rows[0]["user_id"]

    def token_rotate(self, old_token: str, ttl_s: Optional[int] = None, now: Optional[float] = None) -> Optional[str]:
        """Replaces a live token with a new one for the same user. The old token stops working in the same step."""
        now = time.time() if now is None else now
        rows = self._read("SELECT user_id, token_id, expires_at, revoked_at FROM api_tokens WHERE token_hash=?",
                          (token_hash(old_token or ""),))
        if not rows or not is_live(rows[0]["expires_at"], rows[0]["revoked_at"], now):
            return None
        user, old_key = rows[0]["user_id"], rows[0]["token_id"]
        ttl = clamp_ttl(DEFAULT_TOKEN_TTL_S if ttl_s is None else ttl_s)
        fresh = new_user_token()
        fresh_key = token_id(fresh)
        with self._lock:
            self._conn.execute("INSERT INTO api_tokens(token_hash,token_id,user_id,created_at,expires_at) VALUES (?,?,?,?,?)",
                               (token_hash(fresh), fresh_key, user, now, now + ttl))
            try:
                cur = self._conn.execute("UPDATE api_tokens SET revoked_at=?, rotated_to=? WHERE token_id=? AND revoked_at IS NULL",
                                         (now, fresh_key, old_key))
                if cur.rowcount != 1:
                    raise RuntimeError("token changed while rotating")
            except Exception:
                self._conn.execute("DELETE FROM api_tokens WHERE token_id=?", (fresh_key,))
                raise
        return fresh

    def token_list(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._read("SELECT token_id, user_id, created_at, expires_at, revoked_at, rotated_to FROM api_tokens "
                          "ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit), 500)),))
        return [dict(r) for r in rows]

    def token_revoke(self, token_key: str) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE api_tokens SET revoked_at=? WHERE token_id=? AND revoked_at IS NULL",
                                     (time.time(), token_key))
        return cur.rowcount > 0

    # --- health --------------------------------------------------------------
    def integrity_ok(self) -> bool:
        return self._read("PRAGMA integrity_check")[0][0] == "ok"

    def table_counts(self) -> dict[str, int]:
        return {name: self._read(f"SELECT COUNT(*) AS c FROM {name}")[0]["c"] for name in _TABLES}

    def close(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
