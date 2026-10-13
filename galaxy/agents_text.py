"""Text agents: security, maths, code, data, creative meter-checker, language script tools, UI/UX, voice.
Every class here is deterministic: regex, AST, a fixed transliteration table, or a local subprocess."""
from __future__ import annotations

import ast
import asyncio
import contextlib
import html
import json
import math
import operator
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .core import Agent, AgentResult, MAX_INPUT_CHARS, clean_text, fail, ok
from .llm import LLMUnavailable, LocalLLMProvider
from .prompts import creative_prompt, detect_form, translation_prompt
from . import hinglish
from . import voice as VOICE
from .tabular import ANALYTICS_VERSION, TableError, fmt, parse_table, preview_table, render_report, summarise
import hashlib
import statistics
import sqlite3
import time
from .creative_forms import MAX_CONTINUATIONS, continuation_prompt, validate
from .code_analysis import analyze
from .simulate import estimate_growth, simulate
from .reasoning import ReasoningParser, classify_complexity


# ---- Security Sentinel: implemented in security_scan.py ----
from .security_scan import SecuritySentinelAgent  # noqa: F401


# ---- Math Logic: implemented in mathlogic.py ----
from .mathlogic import MathLogicAgent, fmt_num  # noqa: F401


# ----------------------------------------------------------------- Code Interpreter
class CodeInterpreterAgent(Agent):
    """Phase 2 static analysis. Python: ast lint, plus on request a restricted dry-run simulator (step budget) and an
    empirical growth estimate. JavaScript: node --check when Node is installed. C++: g++ -fsyntax-only when installed.
    Rust: rustc --emit=metadata when installed. HTML and CSS: structure and pattern checks. Code is never executed."""

    name = "Code Interpreter"
    _FENCE = re.compile(r"```([A-Za-z+#]*)[ \t]*\n(.*?)```", re.S)

    @staticmethod
    def python_metrics(code: str) -> Optional[dict]:
        report = analyze("python", code)
        return report["metrics"] if report.get("ok") and report.get("syntax_ok") else None

    @staticmethod
    def _sim_args(text: str) -> list:
        n = re.search(r"\bn\s*=\s*(\d{1,6})\b", text)
        if n:
            return [int(n.group(1))]
        literal = re.search(r"\[[-\d,\s.]{1,2000}\]", text)
        if literal:
            try:
                value = ast.literal_eval(literal.group(0))
                if isinstance(value, list):
                    return [value]
            except (ValueError, SyntaxError):
                return []
        return []

    async def process(self, text: str, ctx: dict) -> AgentResult:
        match = self._FENCE.search(text)
        if not match:
            return fail(self.name, "paste the code inside a fenced block, e.g. ```python, ```javascript, ```html, "
                                   "```css, ```cpp or ```rust")
        code = match.group(2)
        report = await asyncio.to_thread(analyze, match.group(1) or "python", code)
        if not report.get("ok"):
            return fail(self.name, report["error"])
        language, syntax = report["language"], report["syntax_ok"]
        lines = [f"Syntax check **{'passed' if syntax else 'failed'}** ({language}, tool: {report['tool']})."
                 if syntax is not None else f"Syntax not verified ({language}, tool: {report['tool']})."]
        lines.append("The code was NOT executed. This is static analysis only.")
        if report["lexical_only"]:
            lines.append("No compiler or parser for this language is installed here, so the check is lexical "
                         "(brackets, strings, pattern rules).")
        findings = report["findings"]
        if findings:
            rows = "\n".join(f"| {f['severity']} | {f['rule']} | {f['line'] or '-'} | {f['message']} |" for f in findings[:30])
            lines.append("| severity | rule | line | message |\n|---|---|---|---|\n" + rows)
        else:
            lines.append("No findings.")
        if language == "python" and report["metrics"]:
            lines.append("| metric | value |\n|---|---|\n" +
                         "\n".join(f"| {k.replace('_', ' ')} | {v} |" for k, v in report["metrics"].items()))
        args = self._sim_args(text)
        if language == "python" and syntax and re.search(r"\b(simulate|dry[ -]?run|trace)\b", text, re.I):
            sim = await asyncio.to_thread(simulate, code, None, args)
            if sim["ok"]:
                lines.append(f"**Dry run** (restricted simulator, no real execution): result {sim['result']!r}, "
                             f"{sim['steps']} steps.")
            else:
                lines.append(f"**Dry run stopped:** {sim['error']} (after {sim['steps']} steps).")
            if sim.get("output"):
                lines.append("Output:\n```\n" + "\n".join(sim["output"][:50]) + "\n```")
        if language == "python" and syntax and re.search(r"\bcomplexity\b|\bbig[ -]?o\b", text, re.I):
            kind = "int" if args and isinstance(args[0], int) else "list"
            est = await asyncio.to_thread(estimate_growth, code, None, (8, 16, 32, 64, 128), kind)
            if est.get("ok"):
                lines.append(f"**Empirical growth estimate:** {est['estimate']} (exponent {est['exponent']}). {est['note']}")
            else:
                lines.append(f"Complexity estimate not available: {est.get('error', 'unknown')}")
        return ok(self.name, "\n\n".join(lines), language=language, syntax_ok=syntax, findings=findings,
                  metrics=report["metrics"])


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")[:120]


class DataAnalyticsAgent(Agent):
    """Phase 3 zero-dependency analytics. CSV, JSON records and markdown tables are parsed in pure Python and loaded into
    an in-memory SQLite table with generated column names, so header text never reaches SQL. Results are cached by the
    hash of the dataset, so an identical table is not recomputed."""

    name = "Data Analytics"
    CACHE_SECONDS = 7 * 86400

    def __init__(self, store=None) -> None:
        self.store = store

    def stats_table(self, nums: list[float]) -> str:
        rows = [("count", str(len(nums))), ("mean", fmt(statistics.fmean(nums))),
                ("median", fmt(statistics.median(nums))), ("mode", fmt(statistics.multimode(nums)[0])),
                ("min", fmt(min(nums))), ("max", fmt(max(nums)))]
        if len(nums) >= 2:
            rows += [("variance (sample)", fmt(statistics.variance(nums))),
                     ("std dev (sample)", fmt(statistics.stdev(nums)))]
        return "| stat | value |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)

    def csv_table(self, text: str) -> str:
        parsed = parse_table(text)
        return preview_table(parsed["headers"], parsed["rows"])

    def records_table(self, obj) -> str:
        if not (isinstance(obj, list) and obj and all(isinstance(r, dict) for r in obj[:50])):
            return ""
        parsed = parse_table(json.dumps(obj))
        return preview_table(parsed["headers"], parsed["rows"])

    @staticmethod
    def _body(text: str) -> str:
        match = re.search(r"```[A-Za-z]*[ \t]*\n(.*?)```", text, re.S)
        return (match.group(1) if match else text).strip()

    @staticmethod
    def _numbers_only(stripped: str) -> Optional[list[float]]:
        if stripped.startswith("["):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                return None
            if isinstance(obj, list) and obj and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in obj):
                return [float(v) for v in obj][:5000]
            return None
        first = stripped.splitlines()[0] if stripped else ""
        if stripped.startswith("{") or first.lstrip().startswith("|") or ("," in stripped and "\n" in stripped):
            return None
        return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", stripped)][:5000]

    async def process(self, text: str, ctx: dict) -> AgentResult:
        stripped = self._body(text)
        if not stripped:
            return fail(self.name, "no data found")
        numbers = self._numbers_only(stripped)
        if numbers is not None:
            if len(numbers) < 2:
                return fail(self.name, "give JSON, a multi-line CSV, a markdown table, or at least two numbers")
            return ok(self.name, self.stats_table(numbers), count=len(numbers), cached=False)
        key = hashlib.sha256(f"{ANALYTICS_VERSION}|{stripped}".encode("utf-8")).hexdigest()
        now = time.time()
        if self.store is not None:
            hit = self.store.analytics_get(key, now)
            if hit is not None:
                return ok(self.name, json.loads(hit)["body"], cached=True)
        try:
            parsed = parse_table(stripped)
        except TableError as exc:
            return fail(self.name, str(exc))
        body = render_report(parsed, summarise(parsed["headers"], parsed["rows"]))[:100_000]
        if self.store is not None:
            with contextlib.suppress(sqlite3.Error):  # the cache is optional: a failed write never fails the analysis
                self.store.analytics_put(key, hashlib.sha256(stripped.encode("utf-8")).hexdigest(),
                                         json.dumps({"body": body}, ensure_ascii=False), now, self.CACHE_SECONDS)
        return ok(self.name, body, cached=False, rows=len(parsed["rows"]), columns=len(parsed["headers"]),
                  kind=parsed["kind"])


# ----------------------------------------------------------------- Creative Writer (meter checker)
_VOWEL_GROUPS = re.compile(r"[aeiouy]+")


def syllables(word: str) -> int:
    """Vowel-group heuristic with silent-e handling. Approximate: it can be off by one on unusual words."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    count = len(_VOWEL_GROUPS.findall(w))
    if w.endswith("e") and not w.endswith(("le", "ee")) and count > 1:
        count -= 1
    return max(1, count)


def line_syllables(line: str) -> int:
    return sum(syllables(w) for w in re.findall(r"[A-Za-z']+", line))


class CreativeWriterAgent(Agent):
    """Haiku meter checks are rule-based. Free-form creative writing uses the local GGUF model when it is ready,
    and says plainly when it is not."""

    name = "Creative Writer"
    timeout_s = 45.0

    def __init__(self, llm: Optional[LocalLLMProvider] = None) -> None:
        self.llm = llm

    @staticmethod
    def _meter_lines(text: str) -> Optional[list[str]]:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) >= 3 and re.search(r"haiku|5-7-5|meter", lines[0], re.I) and not re.search(
                r"\b(write|compose|create|make|generate|likho|likhiye|bana|banao)\b", lines[0], re.I):  # A10-01
            return lines[1:]
        return None

    @staticmethod
    def meter_report(lines: list[str]) -> AgentResult:
        if len(lines) == 3:
            target = [5, 7, 5]
            rows, all_ok = [], True
            for i, (line, want) in enumerate(zip(lines, target), 1):
                got = line_syllables(line)
                verdict = "ok" if got == want else ("too many" if got > want else "too few")
                all_ok = all_ok and got == want
                rows.append(f"| {i} | {line} | {got} | {want} | {verdict} |")
            head = "| line | text | syllables (est.) | target | verdict |\n|---|---|---|---|---|\n"
            summary = "Haiku meter **matches** 5-7-5." if all_ok else "Haiku meter does **not** match 5-7-5."
            return ok("Creative Writer", f"{summary}\n\n{head}" + "\n".join(rows) +
                                        "\n\nSyllable counts are a heuristic estimate.", meter_ok=all_ok)
        counts = [line_syllables(line) for line in lines[:20]]
        table = "\n".join(f"| {i} | {line} | {c} |" for i, (line, c) in enumerate(zip(lines, counts), 1))
        return ok("Creative Writer", "| line | text | syllables (est.) |\n|---|---|---|\n" + table +
                                     "\n\nSyllable counts are a heuristic estimate.", counts=counts)

    async def process(self, text: str, ctx: dict) -> AgentResult:
        lines = self._meter_lines(text)
        if lines is not None:
            return self.meter_report(lines)
        if self.llm is None:
            return fail(self.name, "creative model is not configured")
        depth = classify_complexity(text)
        form = detect_form(text)
        system, user, params = creative_prompt(text, depth)
        try:
            raw, finish = await self.llm.agenerate_full(system, user, timeout=40.0, max_tokens=params.max_tokens,
                                                        temperature=params.temperature)
        except LLMUnavailable as exc:
            return fail(self.name, f"creative writing ke liye local model abhi ready nahi hai ({exc}). "
                                   "Haiku meter check ke liye 'haiku' likh kar 3 lines bhejein.")
        parsed = ReasoningParser.parse(raw)
        if parsed.truncated or not parsed.answer:
            return fail(self.name, "the reasoning ran out of its token budget before the piece was written")
        body, rounds = parsed.answer, 0
        while finish == "length" and rounds < MAX_CONTINUATIONS:
            c_system, c_user, c_params = continuation_prompt(form, body)
            try:
                more, finish = await self.llm.agenerate_full(c_system, c_user, timeout=25.0,
                                                             max_tokens=c_params.max_tokens,
                                                             temperature=c_params.temperature)
            except LLMUnavailable:
                break
            extra = ReasoningParser.parse(more).answer
            if not extra:
                break
            body = body.rstrip() + "\n" + extra.lstrip()
            rounds += 1
        structure = validate(form, body)
        truncated = finish == "length"
        if not structure["ok"]:
            body += f"\n\n*(structure check: {'; '.join(structure['problems'])})*"
        if truncated:
            body += "\n\n*(the piece reached the token limit and could not be finished automatically)*"
        return ok(self.name, body, engine="local-gguf", reasoning=parsed.reasoning, complete=parsed.complete,
                  depth=depth, form=form, structure=structure, continuations=rounds, truncated=truncated)


# ----------------------------------------------------------------- Translator / language sync
_DEVA_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ट": "t",
              "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
              "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v",
              "श": "sh", "ष": "sh", "स": "s", "ह": "h"}
_DEVA_VOWEL = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au"}
_DEVA_MATRA = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "े": "e", "ै": "ai", "ो": "o", "ौ": "au"}
_DEVA_SIGN = {"ं": "n", "ँ": "n", "ः": "h"}
_HALANT = "्"
_HI_ROMAN = {"hai", "hain", "kya", "aur", "mera", "meri", "mere", "karo", "karna", "batao", "likho", "nahi",
             "kitna", "kitni", "kaise", "kab", "ko", "ka", "ki", "ke", "se", "me", "tum", "aap", "main", "mujhe",
             "naam", "kar", "do", "do", "bhi", "ji", "accha", "theek", "samjhao", "bolo"}


def transliterate_devanagari(word: str) -> str:
    """Deterministic Devanagari -> Latin transliteration. Inherent 'a' is dropped at the end of a word."""
    out: list[str] = []
    chars = list(word)
    i = 0
    while i < len(chars):
        ch = chars[i]
        if ch in _DEVA_VOWEL:
            out.append(_DEVA_VOWEL[ch])
        elif ch in _DEVA_CONS:
            out.append(_DEVA_CONS[ch])
            nxt = chars[i + 1] if i + 1 < len(chars) else ""
            if nxt == _HALANT:
                i += 1
            elif nxt in _DEVA_MATRA:
                out.append(_DEVA_MATRA[nxt])
                i += 1
            elif nxt and (nxt in _DEVA_CONS or nxt in _DEVA_VOWEL or nxt in _DEVA_SIGN):
                out.append("a")
        elif ch in _DEVA_SIGN:
            out.append(_DEVA_SIGN[ch])
        elif ch in _DEVA_MATRA:
            out.append(_DEVA_MATRA[ch])
        elif ch not in (_HALANT, "़"):
            out.append(ch)
        i += 1
    return "".join(out)


class TranslatorAgent(Agent):
    """Phase 3 Hinglish and Devanagari handling. Rule-based output (always): script detection, tokens tagged deva, hi,
    tech, en and num, Roman-to-Devanagari for Hinglish words, technical terms kept in English, and a word-level gloss.
    When the local model is ready, its meaning translation is added and checked: every technical term from the input
    must appear in the output, or the answer is flagged as term-lost."""

    name = "Translator"
    timeout_s = 45.0

    def __init__(self, llm: Optional[LocalLLMProvider] = None) -> None:
        self.llm = llm

    @staticmethod
    def rule_based(text: str) -> Optional[tuple[str, dict]]:
        tokens = hinglish.segment(text)
        if not any(tag in ("deva", "hi", "tech", "en") for _, tag in tokens):
            return None
        deva = [t for t, tag in tokens if tag == "deva"]
        latin = [t for t, tag in tokens if tag in ("hi", "tech", "en")]
        if deva and latin:
            script = "mixed Devanagari and Latin"
        elif deva:
            script = "Devanagari (Hindi)"
        else:
            script = "Latin (Hinglish or English)"
        tagged = []
        for tok, tag in tokens:
            if tag == "deva":
                tagged.append(f"{tok} (hi -> {transliterate_devanagari(tok)})")
            elif tag == "hi":
                tagged.append(f"{tok} (hi) = {hinglish.roman_to_deva(tok)[0]}")
            elif tag == "tech":
                tagged.append(f"{tok} (tech, kept)")
            elif tag == "num":
                tagged.append(f"{tok} (num)")
            elif tag == "en":
                tagged.append(f"{tok} (en)")
        hi_count = sum(1 for _, tag in tokens if tag == "hi")
        en_count = sum(1 for _, tag in tokens if tag == "en")
        code_switched = hi_count > 0 and en_count > 0
        technical = hinglish.technical_terms(text)
        gloss, unknown = hinglish.gloss(text, transliterate_devanagari)
        lines = [f"**Script:** {script}", f"**Code-switched:** {'yes' if code_switched else 'no'}",
                 "**Tokens:** " + ", ".join(tagged)]
        if technical:
            lines.append("**Technical terms kept in English:** " + ", ".join(technical))
        lines.append("**Gloss (word-level, rule-based; not a meaning translation):** " + gloss)
        if unknown:
            lines.append("**Unknown Hinglish words:** " + ", ".join(unknown))
        return "\n\n".join(lines), {"script": script, "code_switched": code_switched, "technical_terms": technical}

    async def process(self, text: str, ctx: dict) -> AgentResult:
        base = self.rule_based(text)
        if base is None:
            return fail(self.name, "no Hindi or English words found in the text")
        body, meta = base
        if self.llm is None or not self.llm.available():
            return ok(self.name, body + "\n\n*Meaning translation: local model abhi ready nahi hai; upar transliteration, "
                                        "gloss aur token tags hain.*", **meta)
        terms = meta["technical_terms"]
        try:
            system, user, params = translation_prompt(text)
            if terms:
                user = f"{user}\nKEEP THESE ENGLISH TERMS UNCHANGED: {', '.join(terms)}"
            answer = await self.llm.agenerate(system, user, timeout=40.0, max_tokens=params.max_tokens,
                                              temperature=params.temperature)
        except LLMUnavailable as exc:
            return ok(self.name, body + f"\n\n*Meaning translation skipped ({exc}); upar transliteration, gloss aur "
                                        "token tags hain.*", **meta)
        lost = hinglish.lost_terms(text, answer)
        if lost:
            return ok(self.name, body + f"\n\n*The local model's translation dropped technical terms ({', '.join(lost)}), "
                                        "so it is not shown. The rule-based output above keeps every term.*",
                      term_lost=lost, **meta)
        return ok(self.name, body + f"\n\n**Meaning (local model):**\n{answer}", engine="local-gguf",
                  term_lost=[], **meta)


# ----------------------------------------------------------------- UI / UX synthesizer
class UiUxAgent(Agent):
    name = "UI/UX Engine"

    @staticmethod
    def clean(markdown: str) -> str:
        markdown = unicodedata.normalize("NFKC", markdown)
        markdown = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", markdown)
        markdown = re.sub(r"\n{4,}", "\n\n\n", markdown)
        return markdown.strip()

    @classmethod
    def synthesize(cls, sections: list[str]) -> str:
        return cls.clean("\n\n---\n\n".join(VOICE.repair_fences(s) for s in sections if s.strip()))

    @staticmethod
    def html_fragment(markdown: str) -> str:
        return '<div class="answer">' + html.escape(markdown).replace("\n", "<br>") + "</div>"

    async def process(self, text: str, ctx: dict) -> AgentResult:
        if re.search(r"\b(?:ssml|speech|read\s+aloud|text\s+to\s+speech)\b", text, re.I):
            payload = re.sub(r"(?is)^.*?\b(?:ssml|speech|read\s+aloud|text\s+to\s+speech)\b\s*[:\-]?\s*", "", text, count=1)
            ssml = VoiceSynthesizerAgent.to_ssml(VoiceSynthesizerAgent(), payload)
            _, message = VoiceSynthesizerAgent.validate(ssml)
            return ok(self.name, f"SSML ({message}):\n\n```xml\n{ssml}\n```", ssml=ssml)
        return ok(self.name, self.clean(text))


# ----------------------------------------------------------------- Voice synthesizer (SSML)
class VoiceSynthesizerAgent(Agent):
    """Builds and validates SSML for any external text-to-speech engine (W3C SSML subset). It does not produce audio.
    Markdown symbols and HTML tags never reach the speech text; code blocks are announced, not read."""

    name = "Voice Synthesizer"
    MAX_CHARS = VOICE.MAX_SPEECH_CHARS

    @staticmethod
    def strip_markdown(text: str) -> str:
        return VOICE.strip_markdown(text)

    def to_ssml(self, text: str, rate: str = "medium") -> str:
        return VOICE.to_ssml(text, rate, self.MAX_CHARS)

    @staticmethod
    def validate(ssml: str) -> tuple[bool, str]:
        return VOICE.validate_ssml(ssml)

    async def process(self, text: str, ctx: dict) -> AgentResult:
        ssml = self.to_ssml(text)
        valid, message = self.validate(ssml)
        if not valid:
            return fail(self.name, message)
        return ok(self.name, f"SSML ({message}):\n\n```xml\n{ssml}\n```", ssml=ssml)
