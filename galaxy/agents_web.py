"""Web, Wikipedia and knowledge agents (#5, #6, #9), memory vault (#8) and API gateway (#17).
All outbound HTTP goes through ApiGateway: HTTPS, allow-listed hosts, no credentials, no redirects, size cap, rate limit."""
from __future__ import annotations

import asyncio
import os
import re
from typing import Optional
from urllib.parse import quote

from .core import Agent, AgentResult, ApiGateway, GatewayError, clean_text, fail, ok
from .llm import LLMUnavailable, LocalLLMProvider
from .memory import MemoryStore
from .search_tools import (OFFLINE_MAX_AGE_SECONDS, RETRY_DELAY_SECONDS, SEARCH_TIMEOUT_SECONDS, distill_query,
                           extract_sentences)
from .wiki_parse import build_facts, facts_to_text
from .semantic import PreferenceGraph, coverage as query_coverage, score_texts

WIKI_MAX_BYTES = 1_500_000  # wikitext of a long article can exceed the default 300 KB gateway cap
from .prompts import knowledge_grounded_prompt, knowledge_open_prompt
from .reasoning import ReasoningParser, classify_complexity
from .reflection import reflect

WIKI_DEFAULT = "https://en.wikipedia.org"
NEWS_RE = re.compile(r"\b(news|latest|breaking|today|aaj|abhi|current|recent|price|weather|score|result)\b", re.I)
_CMD_WORDS = re.compile(r"\b(?:please|pls|kindly|can you|could you|tell me|search|google|look\s*up|find|latest|news)\b", re.I)
_FILLER = {"for", "the", "on", "of", "in", "about", "a", "an", "to", "me", "some", "any", "is", "are", "what",
           "whats", "what's", "tell", "give", "show", "info", "information", "details", "kya", "ke", "ki", "ka", "ko"}
_STOP_TERMS = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "has", "have", "its",
               "into", "about", "what", "who", "when", "where", "why", "how", "which", "can", "does", "did",
               "not", "you", "your", "our", "their", "they", "them", "but", "also", "than", "then", "there", "here"}


def clean_query(text: str) -> str:
    return distill_query(text)


def _terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP_TERMS}


def best_sentence(query: str, bodies: list[str]) -> str:
    """Extractive: the sentence sharing the most query terms. Returns '' when nothing overlaps."""
    qt = _terms(query)
    best, best_score = "", 0
    for body in bodies:
        for sent in re.split(r"(?<=[.!?])\s+", body):
            if len(sent) < 20:
                continue
            score = len(qt & _terms(sent))
            if score > best_score:
                best, best_score = sent, score
    return best[:400] if best_score else ""


class SearchUnavailable(RuntimeError):
    """The web search backend cannot be used: library missing, network down, or upstream error."""


class SearchAgent:
    """Shared helper behind agents #5 (web) and #6 (Wikipedia). Not registered as an agent itself."""

    CACHE_SECONDS = 300

    def __init__(self, gateway: ApiGateway, store: MemoryStore) -> None:
        self.gateway = gateway
        self.store = store

    @staticmethod
    def wiki_base() -> str:
        return os.environ.get("GALAXY_WIKI_BASE", WIKI_DEFAULT).rstrip("/")

    @staticmethod
    def route(text: str, prefer: str = "auto") -> str:
        if prefer in ("fact", "news"):
            return prefer
        return "news" if NEWS_RE.search(text) else "fact"

    def _ddg_sync(self, query: str, n: int) -> list[dict]:
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
            except ImportError:
                raise SearchUnavailable("DuckDuckGo library not installed (pip install ddgs)") from None
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=n))

    async def _web(self, query: str) -> Optional[tuple[str, str]]:
        last_error, raw = "no response", None
        for attempt in range(2):
            try:
                raw = await asyncio.wait_for(asyncio.to_thread(self._ddg_sync, query, 5), SEARCH_TIMEOUT_SECONDS)
                break
            except SearchUnavailable:
                raise
            except asyncio.TimeoutError:
                last_error = "timeout"
            except Exception as exc:
                last_error = type(exc).__name__
            if attempt == 0:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        if raw is None:
            raise SearchUnavailable(f"web search failed after 2 attempts ({last_error})")
        rows = []
        for item in raw[:5]:
            href = str(item.get("href") or item.get("url") or "")
            if re.match(r"https?://", href):
                rows.append((clean_text(item.get("title"), 120) or href, href, clean_text(item.get("body"), 400)))
        if not rows:
            return None
        found = extract_sentences(query, [{"title": t, "href": h, "body": b} for t, h, b in rows])
        lead = " ".join(f["sentence"] for f in found)
        body_md = (lead + "\n\n" if lead else "") + "\n".join(f"- [{t}]({h})" for t, h, _ in rows[:3])
        return body_md, (found[0]["url"] if found else rows[0][1])

    async def _wiki(self, query: str) -> Optional[tuple[str, str, dict]]:
        base = self.wiki_base()
        search_url = f"{base}/w/api.php?action=opensearch&limit=1&namespace=0&format=json&search={quote(query)}"
        data = await asyncio.to_thread(self.gateway.get_json, search_url)
        titles = data[1] if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list) else []
        if not titles:
            return None
        title = str(titles[0])[:120]
        slug = quote(title.replace(" ", "_"), safe="")
        page = f"https://en.wikipedia.org/wiki/{slug}"
        try:
            parsed = await asyncio.to_thread(self.gateway.get_json,
                                             f"{base}/w/api.php?action=parse&page={quote(title)}&prop=wikitext"
                                             f"&format=json&formatversion=2&redirects=1", WIKI_MAX_BYTES)
            wikitext = ((parsed.get("parse") or {}).get("wikitext") or "") if isinstance(parsed, dict) else ""
        except GatewayError:
            wikitext = ""
        if wikitext:
            try:
                facts = build_facts(title, page, wikitext)
            except ValueError:
                return None
            if facts["lead"] or facts["infobox"]:
                return facts_to_text(facts), page, facts
        summary = await asyncio.to_thread(self.gateway.get_json, f"{base}/api/rest_v1/page/summary/{slug}")
        if not isinstance(summary, dict) or summary.get("type") == "disambiguation":
            return None
        extract = clean_text(summary.get("extract"), 2000)
        if not extract:
            return None
        url = (((summary.get("content_urls") or {}).get("desktop")) or {}).get("page", "")
        if isinstance(url, str) and url.startswith("https://en.wikipedia.org/"):
            page = url
        lead = " ".join(re.split(r"(?<=[.!?])\s+", extract)[:3])[:700]
        return lead, page, {"title": title, "url": page, "lead": lead, "infobox": {}, "sections": {}}

    async def wiki_only(self, text: str, agent_name: str = "Wikipedia Knowledge") -> AgentResult:
        query = clean_query(text)
        if len(query) < 3:
            return fail(agent_name, "query too short after cleaning")
        key = f"wiki:{query.lower()}"
        cached = self.store.cache_get(key, self.CACHE_SECONDS)
        if cached:
            return ok(agent_name, cached[1]["text"], source=cached[1]["source"], backend="wiki", cacheable=True,
                      facts=cached[1].get("facts", {}))
        try:
            hit = await self._wiki(query)
        except GatewayError as exc:
            return fail(agent_name, f"Wikipedia unreachable: {exc}")
        if not hit:
            return fail(agent_name, "no Wikipedia article matched this query")
        body, source, facts = hit
        self.store.cache_put(key, source, {"text": body, "source": source, "facts": facts})
        return ok(agent_name, body, source=source, backend="wiki", cacheable=True, facts=facts)

    async def web_only(self, text: str, agent_name: str = "Web Search (DDGS)") -> AgentResult:
        query = clean_query(text)
        if len(query) < 3:
            return fail(agent_name, "query too short after cleaning")
        key = f"web:{query.lower()}"
        cached = self.store.cache_get(key, self.CACHE_SECONDS)
        if cached:
            return ok(agent_name, cached[1]["text"], source=cached[1]["source"], backend="web", cacheable=False)
        try:
            hit = await self._web(query)
        except SearchUnavailable as exc:
            stale = self.store.cache_get(key, OFFLINE_MAX_AGE_SECONDS)
            if stale:
                return ok(agent_name, "(offline: showing an earlier result that may be out of date)\n\n"
                                      + stale[1]["text"], source=stale[1]["source"], backend="web-stale",
                          cacheable=False, stale=True)
            return fail(agent_name, f"{exc}; no cached result for this query")
        if not hit:
            return fail(agent_name, "web search returned no usable results")
        body, source = hit
        self.store.cache_put(key, source, {"text": body, "source": source})
        return ok(agent_name, body, source=source, backend="web", cacheable=False)

    async def answer(self, text: str, prefer: str = "auto") -> AgentResult:
        if self.route(text, prefer) == "news":
            return await self.web_only(text, agent_name="Search")
        wiki = await self.wiki_only(text, agent_name="Search")
        if wiki.ok:
            return wiki
        web = await self.web_only(text, agent_name="Search")
        if web.ok:
            return web
        return fail("Search", f"wiki: {wiki.error}; web: {web.error}")


class WebSearchAgent(Agent):
    """Agent #5. Web search through DuckDuckGo (the ddgs library). Needs network and the library."""

    name = "Web Search (DDGS)"

    def __init__(self, search: SearchAgent) -> None:
        self.search = search

    async def process(self, text: str, ctx: dict) -> AgentResult:
        return await self.search.web_only(text, agent_name=self.name)


class WikipediaKnowledgeAgent(Agent):
    """Agent #6. Wikipedia summaries through the allow-listed gateway. Answers are cacheable."""

    name = "Wikipedia Knowledge"

    def __init__(self, search: SearchAgent) -> None:
        self.search = search

    async def process(self, text: str, ctx: dict) -> AgentResult:
        return await self.search.wiki_only(text, agent_name=self.name)


class GeneralKnowledgeAgent(Agent):
    """Agent #9 (Phase 2). Note-first grounded verification: the user's own notes (semantic TF-IDF), then Wikipedia
    (with structured facts), then web snippets, then the local model's own knowledge. Every answer starts with a
    grounding label: [Grounded: User Notes], [Grounded: Wikipedia], [Grounded: Web snippets] or [Open Reasoning].
    Answers built from the user's notes are never cached: they belong to that user alone."""

    name = "General Knowledge"
    timeout_s = 45.0
    LABELS = {"notes": "[Grounded: User Notes]", "wiki": "[Grounded: Wikipedia]", "web": "[Grounded: Web snippets]",
              "web-stale": "[Grounded: Web snippets, offline cache]"}
    OPEN_LABEL = "[Open Reasoning]"

    def __init__(self, search: SearchAgent, llm: Optional[LocalLLMProvider] = None, index=None) -> None:
        self.search = search
        self.llm = llm
        self.index = index

    def _ready(self) -> bool:
        return self.llm is not None and self.llm.available()

    async def process(self, text: str, ctx: dict) -> AgentResult:
        user = ctx.get("user_id")  # RC-03: notes are read only for a validated owner
        if self.index is not None and user:
            hits = [h for h in self.index.search(user, text, k=1, min_score=0.35)
                    if query_coverage(text, h["body"]) >= 0.5]  # RC-08: most content words must match
            if hits:
                note = hits[0]
                source = ok(self.name, note["body"][:900], source=f"your notes #{note['doc_id']} ({note['kind']})",
                            cacheable=False, backend="notes")
                return await self._from_source(text, source)
        source = await self.search.wiki_only(text, agent_name=self.name)
        if not source.ok:
            source = await self.search.web_only(text, agent_name=self.name)
        if source.ok:
            return await self._from_source(text, source)
        return await self._open(text, source)

    async def _from_source(self, question: str, source: AgentResult) -> AgentResult:
        cacheable = bool(source.meta.get("cacheable"))
        backend = str(source.meta.get("backend", "web"))
        label = self.LABELS.get(backend, "[Grounded]")
        if self._ready():
            depth = classify_complexity(question)
            system, user, params = knowledge_grounded_prompt(question, source.text, depth)
            try:
                raw = await self.llm.agenerate(system, user, timeout=40.0, max_tokens=params.max_tokens,
                                               temperature=params.temperature)
            except LLMUnavailable:
                raw = ""
            parsed = ReasoningParser.parse(raw) if raw else None
            if parsed is not None and parsed.answer and not parsed.truncated:
                verified = await reflect(question, source.text, parsed.answer, self.llm)
                suffix = f"; reflection passes: {verified.passes}" if verified.passes > 1 else ""
                body = (f"{label} grounded in {source.source}{suffix} (model-written, checked against the source):\n\n"
                        f"{verified.text}")
                return ok(self.name, body, source=source.source, mode="grounded", label=label, cacheable=cacheable,
                          passes=verified.passes, unsupported=verified.unsupported, reasoning=parsed.reasoning,
                          complete=parsed.complete, depth=depth, backend=backend,
                          trail_missing=(depth != "simple" and not parsed.reasoning))
        return ok(self.name, f"{label} grounded in {source.source} (quoted, not model-written):\n\n{source.text}",
                  source=source.source, mode="extractive", label=label, cacheable=cacheable, backend=backend)

    async def _open(self, question: str, failure: AgentResult) -> AgentResult:
        if self._ready():
            depth = classify_complexity(question)
            system, user, params = knowledge_open_prompt(question, depth)
            try:
                raw = await self.llm.agenerate(system, user, timeout=40.0, max_tokens=params.max_tokens,
                                               temperature=params.temperature)
            except LLMUnavailable:
                raw = ""
            parsed = ReasoningParser.parse(raw) if raw else None
            if parsed is not None and parsed.answer and not parsed.truncated:
                return ok(self.name, f"{self.OPEN_LABEL} from the model's own knowledge, not checked against a source "
                                     f"- please verify:\n\n{parsed.answer}", mode="open", label=self.OPEN_LABEL,
                          reasoning=parsed.reasoning, complete=parsed.complete, backend="open")
        return fail(self.name, "no offline knowledge source here, and web lookup gave nothing: " + failure.error)


class MemoryVaultAgent(Agent):
    """Agent #8 (v7). Per-user state in galaxy_memory.db: name and preferences, recent history, and semantic notes
    (notes:, document:, search my notes, list my notes, forget note N). Notes are visible only to their owner."""

    name = "Memory Vault"

    def __init__(self, store: MemoryStore, index=None) -> None:
        self.store = store
        self.index = index
        self.profile = PreferenceGraph(store)

    async def process(self, text: str, ctx: dict) -> AgentResult:
        uid = ctx.get("user_id")
        if uid is None:  # RC-03: anonymous sessions have no personal memory
            return fail(self.name, "personal memory needs a user id; anonymous sessions keep no memory")
        goal = re.match(r"^\s*my\s+(goal|tech\s+stack|language|timezone)\s+is\s+(.+)$", text, re.I | re.S)
        if goal:
            tag = goal.group(1).lower().replace(" ", "_")
            try:
                self.profile.set(uid, tag, goal.group(2))
            except ValueError as exc:
                return fail(self.name, str(exc))
            return ok(self.name, f"Saved your {tag.replace('_', ' ')}: {goal.group(2).strip()[:200]}", stored="profile", tag=tag)
        if re.search(r"\bshow\s+my\s+profile\b", text, re.I):
            data = self.profile.all(uid)
            if not data:
                return fail(self.name, "no profile saved yet")
            return ok(self.name, "**Your profile:**\n" + "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in sorted(data.items())))
        chats = re.search(r"\bsearch\s+my\s+chats\b\s*(.*)$", text, re.I | re.S)
        if chats:
            query = chats.group(1).strip(" ?.") or text
            user_turns = [content for role, content in ctx.get("history", []) if role == "user"]
            hits = score_texts(query, user_turns, k=3, min_score=0.15)
            if not hits:
                return fail(self.name, "nothing earlier in this conversation matches that")
            return ok(self.name, "**Matches from this conversation** (ephemeral TF-IDF, not stored):\n" +
                      "\n".join(f"- {user_turns[i][:160]} (score {score})" for i, score in hits))
        if self.index is not None:
            note = re.match(r"^\s*(note|document|remember this)\s*:\s*(.+)$", text, re.I | re.S)
            if note:
                kind = "document" if note.group(1).lower() == "document" else "note"
                try:
                    doc_id = self.index.add(uid, kind, note.group(2), source="chat")
                except ValueError as exc:
                    return fail(self.name, str(exc))
                return ok(self.name, f"Saved as {kind} #{doc_id}. Later, ask 'search my notes <topic>' to find it." + (" The text was truncated to 4000 characters (cut at the storage limit)." if len(note.group(2).strip()) > 4000 else ""),
                          stored=kind, doc_id=doc_id)
            if re.search(r"\b(?:search my notes|find in my notes|what did i note about|my notes about)\b", text, re.I):
                query = re.sub(r"(?is)^.*?(?:search my notes|find in my notes|what did i note about|my notes about)\s*",
                               "", text, count=1).strip(" ?.") or text
                hits = self.index.search(uid, query, k=3, min_score=0.15)
                if not hits:
                    return fail(self.name, "no saved note matches that")
                lines = [f"- #{h['doc_id']} (score {h['score']}): {h['body'][:160]}" for h in hits]
                return ok(self.name, "**Matches from your notes** (lexical TF-IDF match):\n" + "\n".join(lines))
            if re.search(r"\b(?:list|show) my notes\b", text, re.I):
                rows = self.index.listing(uid, 10)
                if not rows:
                    return fail(self.name, "you have no saved notes yet")
                return ok(self.name, "**Your notes:**\n" + "\n".join(f"- #{r['doc_id']} [{r['kind']}] {r['preview']}"
                                                                   for r in rows))
            forget = re.search(r"\bforget\s+(?:note|doc|document)\s*#?\s*(\d+)\b", text, re.I)
            if forget:
                if self.index.remove(uid, int(forget.group(1))):
                    return ok(self.name, f"Note #{forget.group(1)} deleted.")
                return fail(self.name, f"no note #{forget.group(1)} belongs to you")
        match = re.search(r"\bmy\s+name\s+is\s+([A-Za-z][A-Za-z .'-]{0,50})", text, re.I)
        if match:
            name = match.group(1).strip(" .")
            self.store.set_attr(uid, "name", name)
            return ok(self.name, f"Theek hai, yaad rakh liya: aapka naam **{name}** hai (database me save ho gaya).",
                      stored="name")
        if re.search(r"\b(?:what(?:'s| is) my name|who am i|mera naam kya)\b", text, re.I):
            name = self.store.get_attr(uid, "name")
            if name:
                return ok(self.name, f"Aapka naam **{name}** hai.")
            return fail(self.name, "naam abhi tak nahi bataya - 'my name is ...' likhein")
        match = re.search(r"\bi\s+(?:like|love|prefer)\s+(.{2,80})", text, re.I)
        if match:
            item = match.group(1).strip(" .")
            items = [x for x in (self.store.get_attr(uid, "preferences") or "").split("; ") if x]
            if item not in items:
                items.append(item)
            self.store.set_attr(uid, "preferences", "; ".join(items[-20:]))
            return ok(self.name, f"Note kar liya: aapko **{item}** pasand hai.", stored="preference")
        if re.search(r"what (?:do|did) i (?:like|prefer)|meri pasand", text, re.I):
            prefs = self.store.get_attr(uid, "preferences")
            if prefs:
                return ok(self.name, "Aapki pasand: " + prefs.replace("; ", ", "))
            return fail(self.name, "abhi tak koi pasand save nahi hui - 'I like ...' likhein")
        if re.search(r"\b(?:history|recent messages?|previous messages?)\b", text, re.I):
            recent = [c for r, c in ctx.get("history", []) if r == "user"][-5:]
            if recent:
                return ok(self.name, "Recent messages:\n" + "\n".join(f"- {m[:200]}" for m in recent))
            return fail(self.name, "is conversation me abhi tak koi history nahi hai")
        return fail(self.name, "memory: 'my name is ...', 'I like ...', 'what do I like' ya 'history' use karein")


class ApiGatewayAgent(Agent):
    """Agent #17. Inbound: bearer-token auth state. Outbound: the allow-list policy and live counters."""

    name = "API Gateway"

    def __init__(self, gateway: ApiGateway) -> None:
        self.gateway = gateway

    async def process(self, text: str, ctx: dict) -> AgentResult:
        s = self.gateway.status()
        recent = ", ".join(f"{h}={n}" for h, n in s["requests_last_minute"].items()) or "none"
        auth = "required (GALAXY_API_TOKEN set)" if os.environ.get("GALAXY_API_TOKEN") else "NOT set (local use only)"
        return ok(self.name, "**API gateway status**\n"
                             f"- inbound auth: bearer token {auth}\n"
                             f"- allowed outbound hosts: {', '.join(s['allowed_hosts'])}\n"
                             f"- upstream calls so far: {s['upstream_calls']}\n"
                             f"- denied requests: {s['denied']}\n"
                             f"- requests in the last minute: {recent}\n"
                             "- outbound policy: HTTPS only, no credentials in URL, no redirects, 300 KB cap, "
                             "8 s timeout, 20 requests per minute per host\n"
                             "- user-supplied URLs are never fetched")
