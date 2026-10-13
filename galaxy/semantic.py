"""Memory Vault (Phase 2): pure-Python TF-IDF retrieval over a user's notes, facts and conversation turns.

Per document: whole words after Hinglish canonicalisation, character trigrams of words of four or more letters (spelling
variants share terms), and word bigrams (multi-word phrases). Queries are expanded with a curated synonym list at half
weight. Documents may carry a TTL (expires_at); expired rows are removed before every add and search. Preference tags are
stored per user under 'profile:' and are readable and writable only by their owner. No embedding model is used.
Stated limits: the synonym list and the canonical forms are small and hand-written. A synonym that is not on the list is
not matched. Retrieval is lexical, with subword overlap.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from typing import Optional

from .memory import MemoryStore

MAX_DOCS_PER_USER = 2000
MAX_BODY_CHARS = 4000
MAX_CANDIDATES = 500
DEFAULT_CONTEXT_TTL = 7 * 86400
PROFILE_TAGS = frozenset({"goal", "tech_stack", "language", "timezone"})
CANON = {"nam": "naam", "kia": "kya", "mere": "mera", "meri": "mera", "padai": "padhai", "padhna": "padhai",
         "bata": "batao", "bataiye": "batao"}
SYNONYM_GROUPS = [
    ("car", "automobile", "vehicle", "auto"), ("laptop", "notebook", "computer", "pc"),
    ("phone", "mobile", "cellphone", "handset"), ("job", "work", "career", "employment", "naukri"),
    ("study", "padhai", "learn", "education"), ("name", "naam"), ("price", "cost", "rate", "fee"),
    ("goal", "aim", "target", "objective"), ("stack", "technology", "framework", "tools"),
    ("doctor", "physician"), ("house", "home", "flat", "apartment", "ghar"), ("meeting", "call", "standup", "sync"),
    ("water", "paani"), ("food", "khana", "meal"), ("money", "paisa", "paise", "cash"),
]
STOP = {"the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "for", "and", "or", "what", "when",
        "who", "where", "why", "how", "which", "do", "does", "did", "i", "me", "my", "it", "this", "that", "with",
        "by", "at", "be", "kya", "hai", "ka", "ki", "ke", "ko", "se", "aur", "mera", "meri", "tha", "thi", "note"}


def canon_word(word: str) -> str:
    return CANON.get(word, word)


def tokens(text: str) -> dict:
    words = [canon_word(w) for w in re.findall(r"\w+", text.lower()) if 1 < len(w) <= 40 and w not in STOP]
    counts: Counter = Counter()
    for word in words:
        counts[word] += 1.0
        if len(word) >= 4:
            padded = f"^{word}$"
            for i in range(len(padded) - 2):
                counts["~" + padded[i:i + 3]] += 1.0
    for first, second in zip(words, words[1:]):
        bigram = f"{first}_{second}"
        if len(bigram) <= 40:
            counts[bigram] += 1.0
    return dict(counts)


def expand_query(query: dict) -> dict:
    expanded = dict(query)
    for term in list(query):
        if term.startswith("~") or "_" in term:
            continue
        for group in SYNONYM_GROUPS:
            if term in group:
                for synonym in group:
                    if synonym != term and synonym not in expanded:
                        expanded[synonym] = 0.5
    return expanded


def score_texts(query: str, texts: list[str], k: int = 3, min_score: float = 0.15) -> list[tuple[int, float]]:
    """Ephemeral TF-IDF over an in-memory list (used for this conversation's turns). Nothing is stored."""
    docs = [tokens(t) for t in texts]
    q = expand_query(tokens(query))
    if not docs or not q:
        return []
    n = len(docs)
    df: Counter = Counter()
    for doc in docs:
        df.update(set(doc))

    def idf(term: str) -> float:
        return math.log((n + 1) / (df.get(term, 0) + 1)) + 1.0
    qvec = {t: (1 + math.log(w)) * idf(t) for t, w in q.items() if df.get(t, 0) > 0}
    qnorm = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
    scored = []
    for index, doc in enumerate(docs):
        dvec = {t: (1 + math.log(w)) * idf(t) for t, w in doc.items()}
        dnorm = math.sqrt(sum(v * v for v in dvec.values())) or 1.0
        dot = sum(qvec[t] * dvec[t] for t in qvec if t in dvec)
        if dot > 0:
            score = dot / (qnorm * dnorm)
            if score >= min_score:
                scored.append((score, index))
    scored.sort(reverse=True)
    return [(index, round(score, 3)) for score, index in scored[:k]]


def _check_owner(user_id: object) -> None:
    from .core import owner_id
    if owner_id(user_id) is None:
        raise ValueError("personal memory needs a valid user id (anonymous sessions keep no memory)")


class PreferenceGraph:
    """Explicit, owner-only profile tags (goal, tech_stack, language, timezone), stored as user attributes."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def set(self, user_id: str, tag: str, value: str) -> None:
        _check_owner(user_id)
        if tag not in PROFILE_TAGS:
            raise ValueError(f"unknown profile tag '{tag}'; allowed: {', '.join(sorted(PROFILE_TAGS))}")
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip()[:200]
        if not cleaned:
            raise ValueError("empty profile value")
        self.store.set_attr(user_id, f"profile:{tag}", cleaned)

    def get(self, user_id: str, tag: str) -> Optional[str]:
        _check_owner(user_id)
        return self.store.get_attr(user_id, f"profile:{tag}")

    def all(self, user_id: str) -> dict:
        _check_owner(user_id)
        return {k[len("profile:"):]: v for k, v in self.store.all_attrs(user_id).items() if k.startswith("profile:")}


class SemanticIndex:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def count(self, user_id: str) -> int:
        _check_owner(user_id)
        return self.store.doc_count(user_id)

    def add(self, user_id: str, kind: str, body: str, source: str = "", ttl_seconds: Optional[float] = None) -> int:
        _check_owner(user_id)
        if kind not in ("note", "document", "fact"):
            raise ValueError("kind must be note, document or fact")
        text = (body or "").strip()[:MAX_BODY_CHARS]
        if not text:
            raise ValueError("empty note")
        terms = tokens(text)
        if not terms:
            raise ValueError("no searchable words in this note")
        now = time.time()
        self.store.doc_prune_expired(now)
        if self.store.doc_count(user_id) >= MAX_DOCS_PER_USER:
            raise ValueError(f"memory is full ({MAX_DOCS_PER_USER} notes); forget some first")
        expires = now + ttl_seconds if ttl_seconds else None
        return self.store.doc_add(user_id, kind, source[:200], text, len(terms), terms, expires)

    def add_context(self, user_id: str, text: str, ttl_seconds: float = DEFAULT_CONTEXT_TTL) -> int:
        _check_owner(user_id)
        return self.add(user_id, "fact", text, source="conversation", ttl_seconds=ttl_seconds)

    def remove(self, user_id: str, doc_id: int) -> bool:
        _check_owner(user_id)
        return self.store.doc_delete(user_id, doc_id)

    def listing(self, user_id: str, limit: int = 20) -> list[dict]:
        _check_owner(user_id)
        return self.store.doc_list(user_id, limit)

    def search(self, user_id: str, query: str, k: int = 3, min_score: float = 0.3) -> list[dict]:
        _check_owner(user_id)
        self.store.doc_prune_expired(time.time())
        q = expand_query(tokens(query))
        n = self.store.doc_count(user_id)
        if not q or n == 0:
            return []
        candidates = self.store.doc_candidates(user_id, list(q), MAX_CANDIDATES)
        if not candidates:
            return []
        docs: dict[int, dict[str, float]] = {}
        for doc_id, term, tf in self.store.doc_terms_for(user_id, candidates):
            docs.setdefault(doc_id, {})[term] = tf
        vocab = set(q)
        for tfs in docs.values():
            vocab |= set(tfs)
        df = self.store.doc_df(user_id, list(vocab))
        idf = {t: math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0 for t in vocab}
        qvec = {t: (1 + math.log(q[t])) * idf[t] for t in q if df.get(t, 0) > 0}
        qnorm = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
        scored = []
        for doc_id, tfs in docs.items():
            dvec = {t: (1 + math.log(tf)) * idf[t] for t, tf in tfs.items()}
            dnorm = math.sqrt(sum(v * v for v in dvec.values())) or 1.0
            dot = sum(qvec[t] * dvec[t] for t in qvec if t in dvec)
            score = dot / (qnorm * dnorm)
            if score >= min_score:
                scored.append((score, doc_id))
        scored.sort(reverse=True)
        top = scored[:k]
        bodies = {r["doc_id"]: r for r in self.store.doc_bodies(user_id, [d for _, d in top])}
        return [{"doc_id": d, "score": round(s, 3), "kind": bodies[d]["kind"], "source": bodies[d]["source"],
                 "body": bodies[d]["body"]} for s, d in top if d in bodies]


def coverage(query: str, body: str) -> float:
    """Share of the query's content words that the note contains. One shared word is not enough (RC-08)."""
    terms = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3 and w not in STOP}
    if not terms:
        return 0.0
    words = {canon_word(w) for w in re.findall(r"[a-z0-9]+", body.lower())}
    return sum(1 for t in terms if t in words) / len(terms)
