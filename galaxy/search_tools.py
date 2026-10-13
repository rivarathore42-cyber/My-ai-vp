"""Agent 5 - DuckDuckGo search helpers (Phase 1): query distillation, verbatim extractive summaries, resilience limits.

Nothing here generates text. Every sentence in a summary is a verbatim substring of one search snippet, and its source
URL is kept with it. Tests check that property on every output.
"""
from __future__ import annotations

import math
import re
from collections import Counter

SEARCH_TIMEOUT_SECONDS = 10.0
RETRY_DELAY_SECONDS = 0.5
OFFLINE_MAX_AGE_SECONDS = 2 * 86400
MAX_TERMS = 8
COMMAND_PHRASES = re.compile(
    r"\b(?:please|pls|kindly|can you|could you|would you|tell me|search (?:for|about|on)|look (?:up|for)|google|"
    r"find (?:out|me)|give me|show me|what (?:is|are)|who (?:is|was)|when (?:is|was|did)|where (?:is|was)|"
    r"how (?:is|are|do|does)|latest|news (?:about|on|of)|update (?:on|about)|info(?:rmation)? (?:about|on)|"
    r"details (?:about|of))\b", re.I)
FILLER_WORDS = {
    "the", "a", "an", "of", "for", "about", "on", "in", "to", "me", "my", "is", "are", "was", "were", "and", "or",
    "with", "from", "it", "this", "that", "some", "any", "do", "does", "did", "be", "been", "has", "have", "had",
    "there", "their", "its", "you", "i", "we", "kya", "ke", "ki", "ka", "ko", "hai", "batao", "bata", "mujhe", "bare",
    "baare", "mein", "me",
}
_SENT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z0-9]+")


def distill_query(text: str) -> str:
    raw = (text or "").strip()
    quoted = [q.strip() for q in re.findall(r'"([^"]{2,80})"', raw)]
    rest = COMMAND_PHRASES.sub(" ", re.sub(r'"[^"]*"', " ", raw))
    kept: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[\w][\w.+#%:/-]*", rest):
        clean = token.strip(".:/-")
        low = clean.lower()
        if not clean or low in FILLER_WORDS or low in seen:
            continue
        seen.add(low)
        kept.append(clean)
        if len(kept) >= MAX_TERMS:
            break
    query = " ".join([f'"{q}"' for q in quoted] + kept)[:200]
    return query if len(query) >= 3 else ""


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) >= 2 and w not in FILLER_WORDS}


def extract_sentences(query: str, results: list[dict], max_sentences: int = 2) -> list[dict]:
    """results: [{'title', 'href', 'body'}]. Returns up to max_sentences sentences, verbatim, with their source."""
    qterms = _terms(query)
    if not qterms:
        return []
    candidates = []
    for item in results:
        href = str(item.get("href") or item.get("url") or "")
        body = str(item.get("body") or "")
        if not re.match(r"https?://", href):
            continue
        for piece in _SENT.split(body):
            sentence = piece.strip()
            if 20 <= len(sentence) <= 400:
                candidates.append({"sentence": sentence, "url": href, "title": str(item.get("title") or "")[:120]})
    if not candidates:
        return []
    doc_freq: Counter = Counter()
    sentence_terms = []
    for cand in candidates:
        terms = _terms(cand["sentence"])
        sentence_terms.append(terms)
        doc_freq.update(terms & qterms)
    n = len(candidates)
    scored = []
    for index, (cand, terms) in enumerate(zip(candidates, sentence_terms)):
        overlap = terms & qterms
        if not overlap:
            continue
        score = sum(1.0 + math.log((n + 1) / (doc_freq[w] + 1)) for w in overlap)
        scored.append((score, -index, cand))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    chosen, seen = [], set()
    for score, _, cand in scored:
        key = cand["sentence"].lower()
        if key in seen:
            continue
        seen.add(key)
        chosen.append({**cand, "score": round(score, 3)})
        if len(chosen) >= max_sentences:
            break
    return chosen
