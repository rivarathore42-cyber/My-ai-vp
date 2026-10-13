"""Traceable adversarial pipeline (agent #3): Proposer -> Critic -> Synthesizer.

Deterministic critics (math, code) cost no LLM call. Every 'expression = value' step is re-evaluated with the AST
evaluator, and code is parsed and checked for dangerous calls. Code is never executed.
The LLM critic (logic and general knowledge) is one self-reflection call. It runs only when more than 15 s of the
request budget remain. The synthesizer runs only after flaws were found, under the same 15 s rule.
At most three LLM calls per answer. Every run writes one row to verification_log.
"""
from __future__ import annotations

import ast
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from .agents_text import MathLogicAgent
from .llm import LLMUnavailable, LocalLLMProvider
from .prompts import critic_prompt, synthesis_prompt
from .reasoning import ReasoningParser

log = logging.getLogger("galaxy.v7.critic")
STAGE_MIN_SECONDS = 15.0
PROPOSER_MIN_SECONDS = 5.0
MAX_FLAWS = 8
_EQ_LINE = re.compile(r"^\s*([^=\n]{1,120}?)\s*=\s*(-?\d[\d,]*(?:\.\d+)?(?:e[-+]?\d+)?)\s*$", re.I | re.M)
_DANGEROUS_NAMES = {"eval", "exec", "compile", "__import__"}
_DANGEROUS_ATTRS = {"system", "popen"}
_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S | re.I)
_math = MathLogicAgent()


def check_math(text: str) -> list[str]:
    flaws = []
    for match in _EQ_LINE.finditer(text):
        expr = match.group(1).strip(" `*-")
        claimed_text = match.group(2).replace(",", "")
        if not re.search(r"[-+*/^]", expr):
            continue
        result = _math.evaluate(expr)
        if not result["ok"]:
            continue  # not evaluable (words, unknown symbols): cannot be checked, so it is not reported as a flaw
        claimed, actual = float(claimed_text), float(result["value"])
        if abs(actual - claimed) > 1e-6 * max(1.0, abs(claimed)):
            flaws.append(f"'{expr} = {claimed_text}' is wrong: it equals {result['value']}")
    return flaws[:MAX_FLAWS]


def check_code(text: str) -> list[str]:
    match = _FENCE.search(text)
    if not match:
        return ["no fenced python code block in the answer"]
    try:
        tree = ast.parse(match.group(1))
    except SyntaxError as exc:
        return [f"syntax error on line {exc.lineno}: {exc.msg}"]
    flaws = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _DANGEROUS_NAMES:
                flaws.append(f"calls {node.func.id}() on line {node.lineno}")
            if isinstance(node.func, ast.Attribute) and node.func.attr in _DANGEROUS_ATTRS:
                flaws.append(f"calls .{node.func.attr}() on line {node.lineno}")
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    flaws.append(f"shell=True on line {node.lineno}")
    return flaws[:MAX_FLAWS]


def parse_flaws(text: str) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned or re.fullmatch(r"NONE\.?", cleaned, re.I):
        return []
    bullets = [ln.strip()[1:].strip() for ln in cleaned.splitlines() if ln.strip().startswith("-")]
    bullets = [b for b in bullets if b]
    return bullets[:MAX_FLAWS] if bullets else [cleaned[:300]]


@dataclass
class PipelineResult:
    text: str
    reasoning: str
    verdict: str          # pass | revised | flagged | unreviewed | skipped
    flaws: list
    critic: str           # deterministic | llm | skipped | none
    llm_calls: int
    note: str = ""


class AdversarialPipeline:
    def __init__(self, llm: Optional[LocalLLMProvider], store=None) -> None:
        self.llm = llm
        self.store = store

    def _ready(self) -> bool:
        return self.llm is not None and self.llm.available()

    async def _call(self, system: str, user: str, params, deadline: float, counter: dict, min_seconds: float):
        remaining = deadline - time.monotonic()
        if not self._ready() or remaining <= min_seconds:
            return None
        counter["n"] += 1
        try:
            return await self.llm.agenerate(system, user, timeout=min(40.0, remaining),
                                            max_tokens=params.max_tokens, temperature=params.temperature)
        except LLMUnavailable:
            return None

    @staticmethod
    def _scope(kind: str, reasoning: str, answer: str) -> str:
        return f"{reasoning}\n{answer}" if kind == "math" else answer

    @staticmethod
    def _deterministic(kind: str, text: str) -> list[str]:
        if kind == "math":
            return check_math(text)
        if kind == "code":
            return check_code(text)
        return []

    def _log(self, kind: str, verdict: str, critic: str, flaws: list) -> None:
        if self.store is None:
            return
        try:
            self.store.log_verification(kind, verdict, critic, flaws)
        except sqlite3.Error:
            log.exception("could not write verification_log")

    async def run(self, kind: str, question: str, proposal: tuple, deadline: float) -> PipelineResult:
        counter = {"n": 0}
        raw = await self._call(*proposal, deadline, counter, PROPOSER_MIN_SECONDS)
        if raw is None:
            flaws = ["the model is not ready or there is not enough time to propose an answer"]
            self._log(kind, "skipped", "skipped", flaws)
            return PipelineResult("", "", "skipped", flaws, "skipped", counter["n"])
        proposed = ReasoningParser.parse(raw)
        if proposed.truncated:
            flaws = ["the reasoning ran out of its token budget before a final answer was written"]
            self._log(kind, "flagged", "none", flaws)
            return PipelineResult("", proposed.reasoning, "flagged", flaws, "none", counter["n"])
        answer = proposed.answer
        critic = "deterministic" if kind in ("math", "code") else "none"
        flaws = self._deterministic(kind, self._scope(kind, proposed.reasoning, answer))
        if kind not in ("math", "code"):
            critic_raw = await self._call(*critic_prompt(kind, question, answer), deadline, counter, STAGE_MIN_SECONDS)
            if critic_raw is None:
                note = "The critic did not run (under 15 s of budget left), so this answer has not been self-checked."
                self._log(kind, "unreviewed", "skipped", [])
                return PipelineResult(answer, proposed.reasoning, "unreviewed", [], "skipped", counter["n"], note)
            critic = "llm"
            flaws = parse_flaws(critic_raw)
        if not flaws:
            self._log(kind, "pass", critic, [])
            return PipelineResult(answer, proposed.reasoning, "pass", [], critic, counter["n"])
        fix_raw = await self._call(*synthesis_prompt(kind, question, answer, flaws), deadline, counter, STAGE_MIN_SECONDS)
        if fix_raw is None:
            self._log(kind, "flagged", critic, flaws)
            return PipelineResult(answer, proposed.reasoning, "flagged", flaws, critic, counter["n"],
                                  "The repair did not run (under 15 s of budget left).")
        repaired = ReasoningParser.parse(fix_raw)
        if repaired.truncated or not repaired.answer:
            self._log(kind, "flagged", critic, flaws)
            return PipelineResult(answer, proposed.reasoning, "flagged",
                                  flaws + ["the repair ran out of its token budget"], critic, counter["n"])
        trail = "\n\n".join(part for part in (proposed.reasoning, repaired.reasoning) if part)
        remaining = self._deterministic(kind, self._scope(kind, repaired.reasoning, repaired.answer))
        if remaining:
            self._log(kind, "flagged", critic, remaining)
            return PipelineResult(repaired.answer, trail, "flagged", remaining, critic, counter["n"],
                                  "Some flaws survived the repair.")
        note = "The repair was not re-reviewed by the LLM critic (time budget)." if critic == "llm" else ""
        self._log(kind, "revised", critic, flaws)
        return PipelineResult(repaired.answer, trail, "revised", flaws, critic, counter["n"], note)
