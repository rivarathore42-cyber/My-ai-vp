"""Agent 3 fast path (Phase 1): exact-match answer cache, a hot in-memory layer over SQLite, validated hashes.

Key:     query_hash = sha256(namespace | exact_query)
exact_query, per namespace:
  knowledge: lower-case, punctuation removed, filler words removed.
  math:      lower-case, whitespace collapsed, every symbol kept (144/12 and 144-12 are different entries).
Layers:
  1. hot: an OrderedDict of recent entries (LRU, 512 entries). A hot hit costs one dict lookup plus one UPDATE of the
     hit counter. It does not touch the entry body.
  2. SQLite: one primary-key read on a hot miss.
Validation: an entry is served only when its namespace and exact_query equal the request (a hash collision cannot
return another question's answer) and its ttl has not passed. Expired hot entries are dropped and re-checked in SQLite.
hit_count is written through on every hit, so the database and the hot layer never disagree about the count.
Only answers an agent marks cacheable are stored.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from .memory import MemoryStore

FILLER = {"please", "pls", "kindly", "tell", "me", "the", "a", "an", "is", "are", "was", "were", "of", "to", "for",
          "about", "kya", "batao", "ke", "ki", "ka", "ko", "hai", "what", "whats", "give", "show"}
TTL_SECONDS = {"knowledge": 7 * 86400, "math": 30 * 86400}
MAX_QUERY_CHARS = 400
MAX_ENTRY_CHARS = 8000


def normalize(text: str, namespace: str = "knowledge") -> str:
    if namespace == "math":
        return re.sub(r"\s+", " ", text.lower()).strip()[:MAX_QUERY_CHARS]
    words = [w for w in re.findall(r"\w+", text.lower()) if w not in FILLER]
    return " ".join(words)[:MAX_QUERY_CHARS]


def query_key(namespace: str, exact_query: str) -> str:
    return hashlib.sha256(f"{namespace}|{exact_query}".encode("utf-8")).hexdigest()


@dataclass
class FastHit:
    text: str
    source: str
    mode: str
    hits: int


class FastPathCache:
    HOT_LIMIT = 512

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self._hot: "OrderedDict[str, dict]" = OrderedDict()

    def _remember(self, key: str, entry: dict) -> None:
        self._hot[key] = entry
        self._hot.move_to_end(key)
        while len(self._hot) > self.HOT_LIMIT:
            self._hot.popitem(last=False)

    def hot_size(self) -> int:
        return len(self._hot)

    def lookup(self, namespace: str, text: str):
        if namespace not in TTL_SECONDS:
            return None
        exact = normalize(text, namespace)
        if len(exact) < 3:
            return None
        now = time.time()
        key = query_key(namespace, exact)
        entry = self._hot.get(key)
        if entry is not None and entry["expires"] > now and entry["namespace"] == namespace and entry["exact"] == exact:
            self._hot.move_to_end(key)
        else:
            self._hot.pop(key, None)
            row = self.store.fastpath_get(key, now)
            if row is None or row["namespace"] != namespace or row["exact_query"] != exact:
                return None
            entry = {"namespace": namespace, "exact": exact, "expires": float(row["ttl"]),
                     "hits": int(row["hit_count"]), "payload": json.loads(row["cached_response"])}
            self._remember(key, entry)
        entry["hits"] += 1
        self.store.fastpath_bump(key, now)
        payload = entry["payload"]
        return FastHit(text=payload.get("text", ""), source=payload.get("source", ""),
                       mode=payload.get("mode", ""), hits=entry["hits"])

    def store_answer(self, namespace: str, text: str, payload: dict) -> bool:
        if namespace not in TTL_SECONDS or not payload.get("text"):
            return False
        exact = normalize(text, namespace)
        if len(exact) < 3:
            return False
        now = time.time()
        key = query_key(namespace, exact)
        body = json.dumps({"text": str(payload["text"])[:MAX_ENTRY_CHARS],
                           "source": str(payload.get("source", ""))[:300],
                           "mode": str(payload.get("mode", ""))[:40]}, ensure_ascii=False)
        self.store.fastpath_put(key, namespace, exact, body, now, now + TTL_SECONDS[namespace])
        self.store.fastpath_prune(now)
        self._hot.pop(key, None)
        return True

    def stats(self) -> dict:
        return self.store.fastpath_stats(time.time())
