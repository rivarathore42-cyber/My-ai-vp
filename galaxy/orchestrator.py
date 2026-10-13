"""Master Orchestrator (#1), Galaxy AI v7.0.

Request flow:
  1. Input checks and per-user conversation key ('user:conversation').
  2. Security Sentinel (#2) scans the whole prompt. A blocked prompt is never stored.
  3. QueryDecomposer splits on AND / aur / then / ; (at most 8 sections). Each section gets a complexity level.
  4. Memory sections (#8) run first, in order (they read and write the user's own state).
  5. Other sections run CONCURRENTLY (asyncio.gather). Complex code, math and exam-logic sections, when the local
     model is ready, go to the Proposer -> Critic -> Synthesizer pipeline (#3). LLM work queues on the model gate.
  6. Knowledge and math sections check the fast path (#3) first, unless the user has saved notes (then the answer
     must come from those notes, so the global cache is skipped).
  7. Every section gets min(its own limit, remaining request budget).
  8. Reasoning trails are returned in a separate 'reasoning' field and stored in reasoning_traces.
  9. UI/UX (#16) joins the sections into one Markdown answer, which is saved to SQLite.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace as _replace
import logging
import re
import sqlite3
import time
import uuid
from typing import Any, Optional

from .agents_system import (CppNativeBridge, DebuggerSentinelAgent, RustSafetyBridge, SelfEvolutionAgent,
                            VisionMetadataAgent, ZigKernelBridge)
from .agents_text import (CodeInterpreterAgent, CreativeWriterAgent, DataAnalyticsAgent, MathLogicAgent,
                          SecuritySentinelAgent, TranslatorAgent, UiUxAgent)
from .agents_web import (ApiGatewayAgent, GeneralKnowledgeAgent, MemoryVaultAgent, SearchAgent, WebSearchAgent,
                         WikipediaKnowledgeAgent)
from .core import MAX_INPUT_CHARS, PARAMS, Agent, AgentResult, ApiGateway, clean_text, fail, ok, safe_id, system_metrics
from .fastpath import FastPathCache
from .llm import LocalLLMProvider
from .memory import MemoryStore
from .reasoning import ReasoningParser, classify_complexity
from .reflection import SpeculativeSynthesizer
from .semantic import SemanticIndex
from .breaker import CircuitBreaker
from .core import owner_id
from . import native_ref as NR
from .agents_system import parse_command
from .telemetry import BUDGETS, budget_for, current_rss_mb
from .security_scan import remove_invisible
from . import voice_router as VR
from .voice_router import strip_wake
from . import android_intents as AI
from . import friend_mode as FR
from .identity import bind as bind_identity
from .security import RequestSigner, new_signing_key
from . import media_controller as MC
from . import platforms as PF

log = logging.getLogger("galaxy.v7.orchestrator")
# The complete 20-agent execution flow (Phase 4). Numbers follow the agent taxonomy used in DESIGN.md.
AGENT_REGISTRY = (
    (1, "Master Orchestrator & Decomposer", "orchestrator"), (2, "Security Sentinel", "security"),
    (3, "Speculative Fast-Path & Reflection Synthesizer", "speculative"), (4, "Math Logic", "math"),
    (5, "Web Search (DDGS)", "web"), (6, "Wikipedia Knowledge", "wikipedia"), (7, "Code Interpreter", "code"),
    (8, "Memory Vault", "memory"), (9, "General Knowledge", "knowledge"), (10, "Creative Writer", "creative"),
    (11, "Translator & Code-Switcher", "translate"), (12, "Data Analytics", "data"),
    (13, "C++ Native Bridge", "cpp"), (14, "Rust Safety Bridge", "rust"), (15, "Zig Kernel Bridge", "zig"),
    (16, "UI/UX & Voice SSML", "ui"), (17, "API Gateway", "gateway"), (18, "Self-Evolution", "evolution"),
    (19, "Debugger Sentinel", "debugger"), (20, "Vision & Document Metadata", "vision"),
)
CACHE_NAMESPACES = {"knowledge": "knowledge", "math": "math"}
MAX_SECTIONS = 8
_CODE_REQUEST = re.compile(r"\b(write|generate|create|implement|make)\b.{0,40}\b(function|program|script|class|code)\b", re.I)
_LOGIC_CUE = re.compile(r"\b(exam|logic|options?|assum\w*|which\s+of\s+the\s+following|statements?|puzzle|deduc\w*)\b", re.I)

_RULES = [
    ("security", re.compile(r"\b(?:ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions|system\s+prompt|jailbreak|dan\s+mode)\b", re.I)),
    ("memory", re.compile(r"^\s*(?:note|document|remember this)\s*:|\bsearch my notes\b|\bfind in my notes\b|\bmy notes\b|"
                          r"\bforget (?:note|doc|document)\b|\bwhat did i note\b", re.I)),
    ("math", re.compile(r"\b(?:calculate|compute|solve|sqrt|factorial|integral|derivative|equation)\b|[\d)]\s*[-+*/^×÷]\s*[\d(]|\d\s*%\s*of\s*\d", re.I)),
    ("code", re.compile(r"```|\b(?:python|javascript|js|java|c\+\+|rust|script|function|code|program|debug|syntax|compile|traceback)\b", re.I)),
    ("data", re.compile(r"\b(?:csv|json|dataset|data\s+analysis|statistics|mean\s+of|average\s+of|table)\b", re.I)),
    ("search", re.compile(r"\b(?:search|latest|news|today|current|recent|weather|price|look\s*up|google|find\s+out)\b", re.I)),
    ("translate", re.compile(r"\b(?:translate|translation|in\s+hindi|in\s+english|hindi\s+me|english\s+me)\b", re.I)),
    ("creative", re.compile(r"\b(?:haiku|5-7-5|poem|poetry|story|essay|kavita|shayari|lyrics|limerick)\b", re.I)),
    ("speech", re.compile(r"\b(?:speak|read\s+aloud|text\s+to\s+speech|tts|voice|ssml)\b", re.I)),
    ("vision", re.compile(r"\b(?:image|picture|photo|screenshot|ocr)\b|\.(?:png|jpe?g|gif|webp)\b", re.I)),
    ("memory", re.compile(r"\b(?:remember|recall|my\s+name\s+is|what(?:'s|\s+is)\s+my\s+name|mera\s+naam|i\s+(?:like|love|prefer)|"
                          r"my\s+history|chat\s+history|recent\s+messages)\b", re.I)),
    ("native", re.compile(r"\b(?:sum|mean|stddev|sort|trace|primes?|matrix_trace)\b\s*:?\s*\[?[-\d]", re.I)),
    ("wiki", re.compile(r"^\s*wiki\b|\bwikipedia\b", re.I)),
    ("knowledge", re.compile(r"\b(?:what\s+is|who\s+is|who\s+was|when\s+did|why\s+does|how\s+does|explain|history|capital\s+of|define)\b|\?\s*$", re.I)),
]
_SPLIT = re.compile(
    r"\s*,?\s+and\s+(?=(?:also\s+|then\s+|please\s+)?(?:write|search|find|calculate|compute|solve|explain|translate|"
    r"summari[sz]e|create|make|run|check|fix|debug|tell|what|who|when|where|why|how|list|generate|read|open|show|give|"
    r"scan|analy[sz]e|plot|convert|save|remember|recall|speak|say|likho|batao|dhundo|bolo|samjhao|kya|kitna|kaise)\b)"
    r"|\s+aur\s+(?=[\w\d(])|\s*;\s*|\s+(?:then|also|plus)\s+(?=[a-z])|\s+और\s+|\s+फिर\s+",
    re.I)


from .clauses import QueryDecomposer  # Agent 1: clause-aware splitting and intent routing (Phase 1 wiring)


def _friendly_reason(error: str) -> str:
    """Turns an internal failure message into a reason a person can act on."""
    text = (error or "").lower()
    if "no offline knowledge" in text:
        return "offline knowledge base mein ye topic nahi mila"
    if "web lookup" in text or "web search" in text:
        return "web search se bhi jawab nahi mila (internet ya search library check karein)"
    if "local model" in text or "model abhi" in text or "llm" in text:
        return "local model abhi ready nahi hai"
    if "too short" in text:
        return "sawaal bahut chhota hai, thoda aur likhein"
    if "timed out" in text or "time budget" in text:
        return "jawab dene me der ho gayi"
    if "paused after" in text:
        return "ye step thodi der ke liye rok diya gaya hai"
    return "ek internal step fail hua"


def _contract(payload: Any) -> dict:
    """Every chat reply carries a non-empty text, an ok flag and a status. Nothing reaches the page without them."""
    out = dict(payload) if isinstance(payload, dict) else {}
    text = out.get("text")
    if not isinstance(text, str) or not text.strip():
        out["text"] = "Jawab tayyar nahi ho paya. Dobara try karein."
        out["ok"] = False
        out["status"] = "degraded"
    out.setdefault("ok", True)
    out.setdefault("status", "ok" if out["ok"] else "degraded")
    return out


class _Refusal(Exception):
    """A voice request that cannot become an Android action; the message is the reply."""


class Orchestrator(Agent):
    name = "Master Orchestrator"

    def __init__(self, store: MemoryStore, llm: Optional[LocalLLMProvider] = None) -> None:
        self.store = store
        self.decomposer = QueryDecomposer()
        self.llm = llm or LocalLLMProvider()
        self.gateway = ApiGateway()
        self.breakers = {"web": CircuitBreaker("web"), "wikipedia": CircuitBreaker("wikipedia")}
        self.fastpath = FastPathCache(store)
        self.index = SemanticIndex(store)
        self.debugger = DebuggerSentinelAgent(store)
        self.evolution = SelfEvolutionAgent(store)
        self.security = SecuritySentinelAgent()
        self.speculative = SpeculativeSynthesizer(self.fastpath, self.llm, store)      # #3
        self.math = MathLogicAgent()                                                    # #4
        self.search = SearchAgent(self.gateway, store)                                  # helper for #5 and #6
        self.web_search = WebSearchAgent(self.search)                                   # #5
        self.wikipedia = WikipediaKnowledgeAgent(self.search)                           # #6
        self.code = CodeInterpreterAgent()                                              # #7
        self.memory = MemoryVaultAgent(store, self.index)                               # #8
        self.knowledge = GeneralKnowledgeAgent(self.search, self.llm, self.index)       # #9
        self.creative = CreativeWriterAgent(self.llm)                                   # #10
        self.translator = TranslatorAgent(self.llm)                                     # #11
        self.data = DataAnalyticsAgent(store)                                                # #12
        self.cpp = CppNativeBridge()                                                    # #13
        self.rust = RustSafetyBridge()                                                  # #14
        self.zig = ZigKernelBridge()                                                    # #15
        self.ui = UiUxAgent()                                                           # #16
        self.gateway_agent = ApiGatewayAgent(self.gateway)                              # #17
        self.vision = VisionMetadataAgent()                                             # #20
        # #18 SelfEvolution and #19 DebuggerSentinel are self.evolution and self.debugger; #1 is this object.
        self.agents: dict[str, Agent] = {
            "security": self.security, "speculative": self.speculative, "math": self.math, "web": self.web_search,
            "wikipedia": self.wikipedia, "code": self.code, "memory": self.memory, "knowledge": self.knowledge,
            "creative": self.creative, "translate": self.translator, "data": self.data, "cpp": self.cpp,
            "rust": self.rust, "zig": self.zig, "ui": self.ui, "gateway": self.gateway_agent,
            "evolution": self.evolution, "debugger": self.debugger, "vision": self.vision,
        }
        self.routes = {"security": "security", "math": "math", "code": "code", "data": "data", "search": "web",
                       "translate": "translate", "creative": "creative", "speech": "ui", "vision": "vision",
                       "memory": "memory", "native": "cpp", "wiki": "wikipedia", "knowledge": "knowledge"}
        for bridge in (self.cpp, self.rust, self.zig):
            bridge.store = store
            self.debugger.register_healer(bridge.name, bridge.reset_after_repair)

    # --- internals -----------------------------------------------------------
    def _telemetry(self, agent: str, ok_flag: bool, elapsed_ms: float, queue_ms: float = 0.0,
                   memory_mb: Optional[float] = None) -> None:
        try:
            self.store.log_call(agent, ok_flag, elapsed_ms, queue_ms, memory_mb)
        except sqlite3.Error:
            log.exception("could not write call telemetry")

    @staticmethod
    def _service_failure(result: AgentResult) -> bool:
        if result.ok:
            return False
        error = (result.error or "").lower()
        return any(marker in error for marker in ("unreachable", "timed out", "failed safely", "web search failed"))

    def _reference_bypass(self, key: str, text: str, failure: AgentResult) -> AgentResult:
        """A native engine that tripped its breaker is bypassed for ops the deterministic Python reference implements."""
        agent = self.agents[key]
        try:
            parsed = parse_command(text, agent.op_aliases, agent.supported_ops)
        except ValueError:
            return failure
        if parsed is None or parsed[0] not in NR.OPS:
            return failure
        op, nums = parsed
        try:
            value = NR.run(op, nums)
        except ValueError:
            return failure
        agent._log(op, "fallback", len(nums), time.perf_counter())
        return ok(agent.name, f"`{op}` = `{value}`\n\n*(bypassed: {agent.engine_label} paused after repeated failures; "
                              "computed by the deterministic Python reference)*", engine="python-reference", bypassed=True)

    def _flush_breaker(self, key: str) -> None:
        breaker = self.breakers.get(key)
        if breaker is None:
            return
        for state in breaker.drain_transitions():
            try:
                self.store.log_breaker_event(key, state, breaker.failures)
            except sqlite3.Error:
                log.exception("could not write the breaker event")

    async def _invoke(self, key: str, text: str, ctx: dict) -> AgentResult:
        if key.startswith("reason:"):
            name, limit = self.speculative.name, 45.0

            def factory():
                return self.speculative.reason(key.split(":", 1)[1], text, ctx)
        else:
            agent = self.agents[key]
            name = agent.name
            limit = float(getattr(agent, "timeout_s", None) or PARAMS["agent_timeout"])

            def factory():
                return agent.process(text, ctx)
        limit = min(limit, budget_for(name, PARAMS["agent_timeout"]))
        deadline = ctx.get("deadline")
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                return fail(name, "skipped: request time budget used up")
            limit = min(limit, remaining)
        try:
            result = await asyncio.wait_for(factory(), limit)
        except asyncio.TimeoutError:
            return fail(name, f"timed out after {limit:.0f} s")
        except Exception as exc:
            seen = self.debugger.record(name, exc)
            return fail(name, f"failed safely ({type(exc).__name__}, seen {seen} times)")
        if not isinstance(result, AgentResult):
            seen = self.debugger.record(name, TypeError(f"{name} returned {type(result).__name__}"))
            return fail(name, f"failed safely (TypeError, seen {seen} times)")
        return result

    async def _run(self, key: str, text: str, ctx: dict, dispatched: Optional[float] = None,
                   bypass_ok: bool = True) -> AgentResult:
        started = time.perf_counter()
        queue_ms = max(0.0, (started - dispatched) * 1000) if dispatched is not None else 0.0
        breaker = self.breakers.get(key)
        if breaker is not None:
            allowed = breaker.allow()
            self._flush_breaker(key)
            if not allowed:
                name = self.agents[key].name
                if bypass_ok:
                    fallback = "wikipedia" if key == "web" else "web"
                    result = await self._run(fallback, text, ctx, dispatched, bypass_ok=False)
                    if result.ok:
                        result.text = (f"*(bypassed: {name} paused after repeated failures; answered from "
                                       f"{self.agents[fallback].name})*\n\n" + (result.text or ""))
                        return result
                return fail(name, f"{name} paused after repeated failures; retry shortly")
        try:
            result = await self._invoke(key, text, ctx)
        except asyncio.CancelledError:
            # RC-04: a cancelled request must free its half-open probe, or the service stays bypassed for good.
            if breaker is not None:
                breaker.release()
                self._flush_breaker(key)
            raise
        if key in ("cpp", "rust", "zig") and not result.ok and "paused" in (result.error or ""):
            result = self._reference_bypass(key, text, result)
        if breaker is not None:
            if self._service_failure(result):
                breaker.record_failure()
            else:
                breaker.record_success()
            self._flush_breaker(key)
        elapsed = (time.perf_counter() - started) * 1000
        self._telemetry(result.agent, result.ok, elapsed, queue_ms, current_rss_mb())
        return result

    def registry_status(self) -> list[dict]:
        rows = []
        for number, role, key in AGENT_REGISTRY:
            present = key == "orchestrator" or key in self.agents
            rows.append({"n": number, "role": role, "key": key, "present": present})
        return rows

    def _route_plan(self, text: str) -> AgentResult:
        """Agent 1 answers with the routing it applies to a request: each part, its intent, and its complexity."""
        if not (text or "").strip():
            return fail("Master Orchestrator & Decomposer", "empty request, nothing to route")
        parts = self.decomposer.split(text) or [text]
        lines = []
        for i, sub in enumerate(parts, 1):
            lines.append(f"{i}. {clean_text(sub, 80)} -> {self.decomposer.classify(sub)} ({classify_complexity(sub)})")
        return ok("Master Orchestrator & Decomposer", "Routing plan:\n" + "\n".join(lines), source="decomposer")

    async def _run_section(self, key: str, text: str, ctx: dict, complexity: str = "standard") -> AgentResult:
        """Runs one agent. A successful answer is never empty: the reply says the step had nothing more to add."""
        result = await self._run_section_raw(key, text, ctx, complexity)
        if result.ok and not (result.text or "").strip():
            result = _replace(result, text="Done: is step ke liye aur kuch nahi chahiye.")
        return result

    async def _run_section_raw(self, key: str, text: str, ctx: dict, complexity: str = "standard") -> AgentResult:
        if key == "orchestrator":
            return self._route_plan(text)
        dispatched = time.perf_counter()
        user = ctx.get("user_id")
        namespace = CACHE_NAMESPACES.get(key)
        has_notes = bool(user) and self.index.count(user) > 0
        if namespace and not (namespace == "knowledge" and has_notes):
            try:
                hit = self.speculative.lookup(namespace, text)
            except sqlite3.Error:
                log.exception("fast-path lookup failed; running the agent instead")
                hit = None
            if hit is not None:
                body = f"{hit.text}\n\n*(fast path: exact match, served from cache, hit #{hit.hits})*"
                return ok(self.agents[key].name, body, source=hit.source, mode=hit.mode, fastpath=True)
        result = await self._run(key, text, ctx, dispatched)
        if namespace and result.ok and result.meta.get("cacheable") and result.text:
            try:
                self.speculative.remember(namespace, text, {"text": result.text, "source": result.source,
                                                            "mode": result.meta.get("mode", "")})
            except sqlite3.Error:
                log.exception("fast-path store failed")
        reasoning = result.meta.get("reasoning") if isinstance(result.meta, dict) else None
        if reasoning and user:
            try:
                self.store.log_trace(user, result.agent, complexity, reasoning,
                                     bool(result.meta.get("complete", True)))
            except sqlite3.Error:
                log.exception("could not store the reasoning trace")
        return result

    def _history(self, conv_key: str) -> list[tuple[str, str]]:
        try:
            return self.store.recent_messages(conv_key, 12)
        except sqlite3.Error:
            log.exception("could not read conversation history")
            return []

    def _persist(self, conv_key: str, user_id: str, prompt: str, answer: str) -> None:
        try:
            self.store.ensure_conversation(conv_key, user_id)
            self.store.add_message(conv_key, "user", prompt)
            self.store.add_message(conv_key, "assistant", answer)
            self.store.prune_messages(conv_key, keep=500)
        except sqlite3.Error:
            log.exception("could not persist conversation turn")

    @staticmethod
    def _render(index: int, title: str, result: AgentResult, heading: bool) -> str:
        if result.ok:
            body = result.text or "✅ Done."
            if result.source:
                body += f"\n\nSource: {result.source}"
        else:
            body = f"⚠️ {result.error}"
        prefix = f"### {index}. {clean_text(title, 120)}\n\n" if heading else ""
        return prefix + body

    @staticmethod
    def _key_for(intent: str, sub: str) -> str:
        if intent == "native":
            if re.search(r"\bzig\b", sub, re.I):
                return "zig"
            return "rust" if re.search(r"\brust\b", sub, re.I) else "cpp"
        return {"security": "security", "math": "math", "code": "code", "data": "data", "search": "web",
                "translate": "translate", "creative": "creative", "speech": "ui", "vision": "vision",
                "memory": "memory", "wiki": "wikipedia", "knowledge": "knowledge"}.get(intent, "knowledge")

    def _reasoning_key(self, intent: str, sub: str) -> Optional[str]:
        if intent == "code" and _CODE_REQUEST.search(sub) and "```" not in sub:
            return "reason:code"
        if intent == "math" and self.math.extract(sub) is None:
            return "reason:math"
        if intent == "knowledge" and _LOGIC_CUE.search(sub):
            return "reason:logic"
        return None

    def _record_security(self, user_id: str, result: AgentResult, prompt: str) -> None:
        flags = result.meta.get("flags") or []
        if not flags:
            return
        action = "sanitized" if result.ok else "blocked"
        severity = result.meta.get("severity", "medium")
        excerpt = remove_invisible(prompt)[0].replace("\n", " ")[:200]
        try:
            self.store.log_security_event(user_id, severity, action, ",".join(flags),
                                          hashlib.sha256(prompt.encode("utf-8")).hexdigest(), excerpt)
        except sqlite3.Error:
            log.exception("could not write the security event")

    _ANDROID_TTL_S = 120
    _REVOKABLE = tuple(sorted(set(PF.always_keys()) | {"whatsapp", "notify", "camera", "settings"}))

    def _bind(self, body: Any, identity: Any) -> Any:
        """Binds a request to its identity (Phase 7). Over HTTP the identity comes from the Bearer token and always wins.
        In-process callers (tests, the local CLI) pass no identity, and their body user_id is used."""
        if identity is not None:
            return bind_identity(body, identity)
        if not isinstance(body, dict):
            return body
        out = dict(body)
        owner = owner_id(out.get("user_id"))
        out.pop("user_id", None)
        out.pop("_session", None)  # a body can never carry a session
        if owner is not None:
            out["user_id"] = owner
        return out

    def _owner(self, body: Any) -> Optional[str]:
        return owner_id(body.get("user_id")) if isinstance(body, dict) else None

    def _voice_turn(self, text: str, owner: Optional[str], session: Optional[str] = None) -> Optional[dict]:
        """Routes a voice turn. When Friend Mode is on, the reply uses the user's saved nickname."""
        reply = self._voice_turn_core(text, owner, session)
        if reply is not None and owner is not None:
            reply["text"] = FR.address(self.store.profile_get(owner), reply["text"])
        return reply

    def _voice_turn_core(self, text: str, owner: Optional[str], session: Optional[str] = None) -> Optional[dict]:
        """Voice intents. Nothing runs here: an Android action becomes a request that waits for the user's decision."""
        route = VR.classify(text)
        if route.kind == "reasoning":
            return None
        none = {"type": "none"}
        if owner is None:
            return {"text": "Android actions ke liye user_id chahiye. Anonymous session me ye kaam nahi hota.", "action": none}
        if route.kind == "save_contact":
            if not route.phone:
                return {"text": "Number sahi nahi hai. Country code ke saath likhein, jaise +91 98765 43210.", "action": none}
            self.store.contact_save(owner, route.name, route.phone)
            return {"text": f"{route.name} ka number save ho gaya.", "action": none}
        try:
            spec = self._intent_for(route, owner)
        except _Refusal as exc:
            return {"text": str(exc), "action": none}
        except ValueError as exc:
            return {"text": f"Ye kaam abhi nahi ho sakta: {exc}", "action": none}
        return self._android_request(owner, spec, session)

    def _intent_for(self, route: Any, owner: str) -> AI.IntentSpec:
        """Maps a route to a validated intent. Media commands come from media_controller; system routes from here."""
        kind = route.kind
        if kind == "platform_needed":
            raise _Refusal("Kis app par? Spotify, YouTube Music, JioSaavn, Apple Music, Wynk, YouTube, Netflix, "
                           "Prime Video ya Hotstar batayein.")
        if kind == "needs_query":
            raise _Refusal("Kya dhoondhun? Naam batayein.")
        if kind == "needs_direction":
            raise _Refusal("Aage ya peeche? Batayein, jaise '45 second aage'.")
        if kind == "app_open":
            spec = AI.platform_open(route.name)
        elif kind == "camera":
            spec = AI.camera()
        elif kind == "settings":
            spec = AI.settings(route.query)
        elif kind == "notify":
            spec = AI.notify(route.query)
        elif kind == "whatsapp_open":
            spec = AI.whatsapp_open()
        elif kind in ("whatsapp_chat", "whatsapp_message", "call"):
            contact = self.store.contact_find(owner, route.name)
            if contact is None:
                raise _Refusal(f"'{route.name}' contact nahi mila. 'save contact {route.name} <number>' bolein.")
            if kind == "call":
                spec = AI.call(contact["phone"], contact["display_name"])
            elif kind == "whatsapp_chat":
                spec = AI.whatsapp_chat(contact["phone"], contact["display_name"])
            else:
                spec = AI.whatsapp_message(contact["phone"], contact["display_name"], route.query)
        elif route.name in MC.MEDIA_KEYS and kind.startswith(route.name + "_"):
            spec = MC.build(route.name, kind[len(route.name) + 1:], route.query)
        else:
            raise ValueError("this command has no Android intent")
        if not FR.allows(self.store.profile_get(owner), AI.CATEGORY[spec.kind]):
            raise _Refusal("Friend Mode mein ye category allow nahi ki gayi hai. Settings me badal sakte hain.")
        return spec

    def _signer(self) -> RequestSigner:
        signer = getattr(self, "_request_signer", None)
        if signer is None:
            signer = RequestSigner(new_signing_key())   # lives in this process only: pending requests die with a restart
            self._request_signer = signer
        return signer

    def _android_request(self, owner: str, spec: AI.IntentSpec, session: Optional[str] = None) -> dict:
        """Stores the executable action sealed and bound to the asking session. The action is NOT returned here."""
        now = time.time()
        rid = uuid.uuid4().hex
        can_always = bool(spec.standing_ok and spec.risk == "low" and FR.always_allowed(self.store.profile_get(owner)))
        standing = can_always and self.store.grant_active(owner, spec.kind)
        action_json = json.dumps(spec.action, ensure_ascii=False)
        signature = self._signer().sign(rid, owner, session or "", spec.kind, action_json, spec.risk, int(can_always))
        self.store.request_create(owner, {
            "request_id": rid, "kind": spec.kind, "title": spec.title, "why": spec.why, "preview": spec.preview,
            "risk": spec.risk, "can_always": int(can_always), "action_json": action_json, "created_at": now,
            "expires_at": now + self._ANDROID_TTL_S, "token_id": session, "signature": signature})
        badge = PF.PLATFORMS[spec.kind].badge if spec.kind in PF.PLATFORMS else "G"
        return {"text": spec.reply or f"{spec.title}: aapki permission chahiye. Neeche faisla kijiye.",
                "action": {"type": "permission_request", "request_id": rid, "kind": spec.kind, "title": spec.title,
                           "why": spec.why, "preview": spec.preview, "destination": spec.destination,
                           "platform": spec.platform, "verb": spec.verb, "badge": badge, "risk": spec.risk,
                           "can_always": can_always, "mode": "streamlined" if standing else "full",
                           "expires_in_s": self._ANDROID_TTL_S}}

    def handle_decision(self, body: Any, identity: Any = None) -> dict:
        """The only path to an executable Android action. Owner-bound, session-bound, sealed, single use, expiring, atomic."""
        body = self._bind(body, identity)
        owner = self._owner(body)
        if owner is None:
            return {"status": "refused", "reason": "user_id required"}
        decision = body.get("decision")
        rid = body.get("request_id")
        if decision not in ("once", "always", "deny"):
            return {"status": "refused", "reason": "unknown decision"}
        if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{32}", rid):
            return {"status": "refused", "reason": "unknown request"}
        row = self.store.request_get(owner, rid)   # another user's id looks exactly like an unknown id
        if row is None:
            return {"status": "refused", "reason": "unknown request"}
        if identity is not None and row.get("token_id") != identity.token_id:
            return {"status": "refused", "reason": "this request belongs to another session"}
        if row["status"] != "pending":
            return {"status": "refused", "reason": f"already {row['status']}"}
        now = time.time()
        if now > row["expires_at"]:
            self.store.request_expire(owner, rid, now)
            return {"status": "expired"}
        sealed = (rid, owner, row.get("token_id") or "", row["kind"], row["action_json"], row["risk"], int(row["can_always"]))
        if not self._signer().verify(row.get("signature"), *sealed):
            self.store.request_claim(owner, rid, "denied", "integrity", now)
            return {"status": "refused", "reason": "the request failed its integrity check and was cancelled"}
        action = json.loads(row["action_json"])
        if action.get("type") in ("open_url", "dial", "settings") and not AI.safe_url(action.get("url", "")):
            self.store.request_claim(owner, rid, "denied", "allowlist", now)
            return {"status": "refused", "reason": "the action is not on the allowlist"}
        if decision == "deny":
            self.store.request_claim(owner, rid, "denied", "deny", now)
            return {"status": "denied"}
        if decision == "always" and not (row["can_always"] and FR.always_allowed(self.store.profile_get(owner))):
            return {"status": "refused",
                    "reason": "Always Allow needs an explicit profile-level grant and is not offered for this action"}
        if not self.store.request_claim(owner, rid, "executed", decision, now):
            return {"status": "refused", "reason": "already decided"}
        if decision == "always":
            self.store.grant_set(owner, row["kind"])
        return {"status": "approved", "decision": decision, "action": action}

    def handle_revoke(self, body: Any, identity: Any = None) -> dict:
        body = self._bind(body, identity)
        owner = self._owner(body)
        kind = body.get("kind") if isinstance(body, dict) else None
        if owner is None:
            return {"status": "refused", "reason": "user_id required"}
        if kind not in self._REVOKABLE:
            return {"status": "refused", "reason": "unknown kind"}
        return {"status": "revoked" if self.store.grant_revoke(owner, kind) else "none"}

    def handle_trace(self, body: Any, identity: Any = None) -> dict:
        body = self._bind(body, identity)
        owner = self._owner(body)
        if owner is None:
            return {"status": "refused", "reason": "user_id required"}
        try:
            limit = int(body.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        return {"status": "ok", "requests": self.store.request_list(owner, limit)}

    def handle_profile(self, body: Any, identity: Any = None) -> dict:
        body = self._bind(body, identity)
        owner = self._owner(body)
        if owner is None:
            return {"status": "refused", "reason": "user_id required"}
        op = body.get("op")
        if op == "get":
            return {"status": "ok", "profile": self.store.profile_get(owner)}
        if op == "question":
            return {"status": "ok", "question": FR.next_question(body.get("answers") or {})}
        if op == "onboard":
            try:
                profile = FR.build_profile(body.get("answers") or {})
            except (ValueError, TypeError) as exc:
                return {"status": "refused", "reason": str(exc)}
            self.store.profile_save(owner, profile)
            return {"status": "ok", "profile": profile}
        if op == "toggle":
            profile = self.store.profile_get(owner)
            if profile is None:
                return {"status": "refused", "reason": "complete onboarding first"}
            profile["enabled"] = bool(body.get("enabled"))
            self.store.profile_save(owner, profile)
            return {"status": "ok", "profile": profile}
        if op == "always":
            profile = self.store.profile_get(owner)
            if profile is None:
                return {"status": "refused", "reason": "complete onboarding first"}
            boundaries = set(profile["boundaries"])
            if body.get("enabled"):
                boundaries.add("always_allow")
            else:
                boundaries.discard("always_allow")
            profile["boundaries"] = sorted(boundaries)
            self.store.profile_save(owner, profile)
            return {"status": "ok", "profile": profile}
        return {"status": "refused", "reason": "unknown op"}

    def persona_for(self, owner: Optional[str]) -> Optional[str]:
        """Prepared for the prompt builder: the Friend Mode persona, or None when the mode is off."""
        return FR.persona_instruction(self.store.profile_get(owner)) if owner else None

    async def _handle(self, body: Any) -> dict:
        if not isinstance(body, dict):
            return {"text": "JSON body bhejein jisme 'message' field ho."}
        prompt = body.get("message")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"text": "Kuch likhein - sawaal ya command."}
        if len(prompt) > MAX_INPUT_CHARS:
            return {"text": f"Message bahut lamba hai (limit {MAX_INPUT_CHARS:,} characters)."}
        owner = owner_id(body.get("user_id"))  # RC-03: personal memory only for a validated, explicit id
        user_id = owner or "anonymous"  # used for logs and labels; never as a memory key
        conv_key = f"{user_id}:{safe_id(body.get('conversation_id'), 'default')}"
        image = body.get("image_base64") if isinstance(body.get("image_base64"), str) else None
        ctx: dict[str, Any] = {"user_id": owner, "conversation_id": conv_key, "image_base64": image,
                               "history": [], "deadline": time.monotonic() + PARAMS["total_timeout"] - 3.0}
        security = await self._run("security", prompt, ctx)
        self._record_security(user_id, security, prompt)
        if not security.ok:
            flags = ", ".join(security.meta.get("flags", [])) or "policy"
            return {"text": f"**Security Sentinel** ne ye request rok di ({flags}). Agar galti se hua hai to "
                            "seedha shabdon me dobara likhein."}
        clean_prompt = security.meta.get("clean", prompt)
        if body.get("source") == "voice":  # Hey Galaxy turn: app intents are answered here, the rest is reasoning
            voice = self._voice_turn(clean_prompt, owner, body.get('_session') if isinstance(body, dict) else None)
            if voice is not None:
                return voice
            clean_prompt = strip_wake(clean_prompt)
        ctx["history"] = self._history(conv_key) if owner else []  # anonymous sessions read no stored history
        tasks = self.decomposer.split(clean_prompt)
        plan = []
        for i, sub in enumerate(tasks, 1):
            intent = self.decomposer.classify(sub)
            complexity = classify_complexity(sub)
            key = self._key_for(intent, sub)
            if complexity == "complex" and self.llm.available():
                key = self._reasoning_key(intent, sub) or key
            plan.append((i, sub, key, complexity))

        results: dict[int, AgentResult] = {}
        for i, sub, key, complexity in plan:
            if key == "memory":
                results[i] = await self._run_section(key, sub, ctx, complexity)
        concurrent = [(i, sub, key, complexity) for i, sub, key, complexity in plan if key != "memory"]
        outcomes = await asyncio.gather(*(self._run_section(key, sub, ctx, complexity)
                                          for _, sub, key, complexity in concurrent))
        for (i, _, _, _), outcome in zip(concurrent, outcomes):
            results[i] = outcome

        sections = [self._render(i, sub, results[i], len(tasks) > 1) for i, sub, _, _ in plan]
        if image:
            vision = await self._run("vision", clean_prompt, ctx)
            sections.append(self._render(len(sections) + 1, "image", vision, True))
        final = self.ui.synthesize(sections)
        if plan and all(not results[i].ok for i, _, _, _ in plan):
            final = self._degraded_reply([(sub, results[i]) for i, sub, _, _ in plan])
        if not final.strip():
            final = self._degraded_reply([])
        if owner:  # anonymous turns are not stored, so no other caller can read them
            self._persist(conv_key, owner, clean_prompt, final)
        payload: dict[str, Any] = {"text": final}
        trails = []
        for i, sub, _, _ in plan:
            reasoning = results[i].meta.get("reasoning") if isinstance(results[i].meta, dict) else None
            if reasoning:
                trails.append(f"### {i}. {clean_text(sub, 120)}\n\n{ReasoningParser.render_trail(reasoning)}")
        if trails:
            payload["reasoning"] = "\n\n".join(trails)
        return payload

    @staticmethod
    def _degraded_reply(items) -> str:
        """Used when no section produced an answer: says what failed, and what the person can do next."""
        reasons: list[str] = []
        for _, result in items:
            reason = _friendly_reason(getattr(result, "error", "") or "")
            if reason not in reasons:
                reasons.append(reason)
        lines = ["Abhi is sawaal ka poora jawab nahi de paya."]
        if reasons:
            lines.append("Kaaran: " + "; ".join(reasons) + ".")
        lines.append("Kya karein: local model ke load hone ka wait karein, ya internet check karke dobara puchhein. "
                     "Math, notes aur voice commands bina model ke bhi chalte hain.")
        return "\n\n".join(lines)

    # --- public API ----------------------------------------------------------
    async def handle(self, body: Any, identity: Any = None) -> dict:
        body = self._bind(body, identity)
        try:
            return _contract(await asyncio.wait_for(self._handle(body), PARAMS["total_timeout"]))
        except asyncio.TimeoutError:
            return _contract({"text": "Request ko time limit ke andar complete nahi kar paaya, isliye roka gaya. "
                                      "Chhota sawaal try karein.", "ok": False, "status": "timeout"})
        except Exception as exc:
            self.debugger.record(self.name, exc)
            return _contract({"text": "Internal error - kuch execute nahi hua. Dobara try karein.",
                              "ok": False, "status": "error"})

    async def process(self, text: str, ctx: dict) -> AgentResult:
        payload = await self.handle({"message": text, **ctx})
        return AgentResult(agent=self.name, ok=True, text=payload["text"])

    def health(self) -> dict:
        integrity = self.store.integrity_ok()
        return {"status": "ok" if integrity else "degraded", "version": "7.0.0", "system": system_metrics(),
                "database": {"path": str(self.store.path), "schema_version": self.store.schema_version(),
                             "integrity_ok": integrity, "tables": self.store.table_counts()},
                "gateway": self.gateway.status(), "llm": self.llm.status(), "fastpath": self.fastpath.stats(),
                "bridges": {b.name: b.status() for b in (self.cpp, self.rust, self.zig)},
                "evolution": self.evolution.metrics(), "top_errors": self.store.top_errors(5),
                "agents": len(AGENT_REGISTRY), "registry": self.registry_status(),
                "breakers": {k: v.snapshot() for k, v in self.breakers.items()}, "budgets": dict(BUDGETS)}

    async def shutdown(self) -> None:
        for bridge in (self.cpp, self.rust, self.zig):
            await bridge.stop()
