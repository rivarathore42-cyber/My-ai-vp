"""Agent #3 - Speculative Fast-Path & Reflection Synthesizer (v7.0).

Three mechanisms live here:
  1. reflect(): lexical draft verification for source-grounded answers (every number and multi-word name in the
     draft must appear in the source). At most one LLM revision.
  2. The fast-path cache (see fastpath.py): exact, validated answers for knowledge and math.
  3. reason(): the Proposer -> Critic -> Synthesizer pipeline (critic.py) for complex code, math and exam logic.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

from .core import Agent, AgentResult, fail, ok
from .critic import AdversarialPipeline, PipelineResult
from .fastpath import FastPathCache
from .llm import LLMUnavailable, LocalLLMProvider
from .prompts import code_proposal_prompt, knowledge_revision_prompt, logic_proposal_prompt, math_proposal_prompt
from .reasoning import ReasoningParser

NOTE = "\n\n*Some details could not be matched to the source and were removed or flagged: {items}.*"
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?!\w)")
_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")


def claims(text: str) -> set[str]:
    numbers = {m.group(0).replace(",", "") for m in _NUM.finditer(text)}
    names = {m.group(0) for m in _NAME.finditer(text)}
    return numbers | names


_NUM_TOKEN = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!\w|\.\d)")  # a full stop after a number ends the sentence


def unsupported_claims(answer: str, source: str) -> list[str]:
    """RC-06: a number must equal a whole number in the source (643 is not 1643); a name must occur on word boundaries."""
    source_numbers = {m.group(0) for m in _NUM_TOKEN.finditer(source.replace(",", ""))}
    source_lower = source.lower()
    bad = []
    for claim in sorted(claims(answer)):
        if claim[:1].isdigit():
            if claim not in source_numbers:
                bad.append(claim)
        elif not re.search(r"(?<!\w)" + re.escape(claim.lower()) + r"(?!\w)", source_lower):
            bad.append(claim)
    return bad


@dataclass
class ReflectionResult:
    text: str
    unsupported: list
    passes: int


def split_sentences(text: str) -> list[str]:
    return [piece.strip() for piece in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if piece.strip()]


def strip_unsupported(answer: str, source: str) -> tuple[str, list[str], list[str]]:
    """Keep only the sentences whose claims all occur in the source. Returns (kept_text, removed_sentences,
    removed_claims). Removal is deterministic: a sentence with one unsupported number or name is dropped whole."""
    kept, removed, removed_claims = [], [], []
    for sentence in split_sentences(answer):
        bad = unsupported_claims(sentence, source)
        if bad:
            removed.append(sentence)
            removed_claims.extend(bad)
        else:
            kept.append(sentence)
    return " ".join(kept), removed, removed_claims


async def reflect(question: str, source: str, draft: str, llm: Optional[LocalLLMProvider]) -> ReflectionResult:
    """Draft -> verify -> at most one revision -> deterministic strip. The final text never contains a claim the
    source does not support: unsupported sentences are removed and reported, never silently kept."""
    bad = unsupported_claims(draft, source)
    if not bad:
        return ReflectionResult(draft, [], 1)
    text, passes = draft, 1
    if llm is not None and llm.available():
        system, user, params = knowledge_revision_prompt(question, draft, bad, source)
        try:
            raw = await llm.agenerate(system, user, timeout=40.0, max_tokens=params.max_tokens,
                                      temperature=params.temperature)
        except LLMUnavailable:
            raw = ""
        revised = ReasoningParser.parse(raw).answer if raw else ""
        if revised:
            text, passes = revised, 2
    kept, removed, removed_claims = strip_unsupported(text, source)
    final = kept if kept else "The source does not say."
    if removed:
        final += NOTE.format(items=", ".join(sorted(set(removed_claims))) + f" ({len(removed)} sentence(s) removed)")
    return ReflectionResult(final, sorted(set(removed_claims)), passes)


def _verdict_text(kind: str, res: PipelineResult) -> str:
    if res.verdict == "pass":
        head = {"math": "Verified: every `expr = value` step was re-evaluated with the AST evaluator and matches.",
                "code": "Verified: the code parses and passed the AST safety checks. It was NOT executed."}.get(
            kind, "The LLM critic found no flaws. This is self-review by the same model, not an independent check.")
    elif res.verdict == "revised":
        head = "Revised once after the critic found: " + "; ".join(res.flaws[:4])
    elif res.verdict == "flagged":
        head = "Unresolved flaws: " + "; ".join(res.flaws[:4])
    else:
        head = res.note or "Not fully reviewed."
    if res.note and res.verdict != "unreviewed":
        head += " " + res.note
    return f"{res.text}\n\n*({head})*" if res.text else f"*({head})*"


class SpeculativeSynthesizer(Agent):
    """Agent #3. Owns the fast-path cache and the adversarial pipeline. 'cache stats' reports the cache."""

    name = "Speculative Fast-Path & Reflection Synthesizer"
    PROPOSERS = {"code": code_proposal_prompt, "math": math_proposal_prompt, "logic": logic_proposal_prompt}

    def __init__(self, cache: FastPathCache, llm: Optional[LocalLLMProvider] = None, store=None) -> None:
        self.cache = cache
        self.pipeline = AdversarialPipeline(llm, store)

    def lookup(self, namespace: str, text: str):
        return self.cache.lookup(namespace, text)

    def remember(self, namespace: str, text: str, payload: dict) -> bool:
        return self.cache.store_answer(namespace, text, payload)

    async def reason(self, kind: str, question: str, ctx: dict) -> AgentResult:
        if kind not in self.PROPOSERS:
            return fail(self.name, f"unknown reasoning kind '{kind}'")
        deadline = ctx.get("deadline") or (time.monotonic() + 30.0)
        res = await self.pipeline.run(kind, question, self.PROPOSERS[kind](question), deadline)
        meta = {"verdict": res.verdict, "critic": res.critic, "llm_calls": res.llm_calls, "flaws": res.flaws,
                "reasoning": res.reasoning, "complete": res.verdict != "flagged" or bool(res.text),
                "cacheable": False, "kind": kind}
        if res.verdict == "skipped":
            return fail(self.name, res.flaws[0], **meta)
        return ok(self.name, _verdict_text(kind, res), **meta)

    async def process(self, text: str, ctx: dict) -> AgentResult:
        stats = self.cache.stats()
        return ok(self.name, f"**Fast-path cache:** {stats['live_entries']} live entries "
                             f"({stats['entries']} stored), {stats['total_hits']} hits so far.")
