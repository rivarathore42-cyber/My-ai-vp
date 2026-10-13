"""Reasoning-trail handling (v7): <think> parsing, complexity routing and per-depth token budgets.

Reasoning models (for example DeepSeek-R1 distills) and instruction models prompted to think both produce the same
shape: reasoning inside <think>...</think>, then the answer. Some chat templates already emit the opening tag, so the
output contains only '</think>'. A reasoning block that never closes means the token budget ran out: the trail is
kept, and the answer is reported as missing. It is never invented.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.S | re.I)
THINK_OPEN = re.compile(r"<think>", re.I)
THINK_CLOSE = re.compile(r"</think>", re.I)
THINK_ANY = re.compile(r"</?think>", re.I)
THINK_WORDS = {"simple": 0, "standard": 180, "complex": 380}
MAX_TRAIL_CHARS = 6000
_COMPLEX_WORDS = re.compile(r"\b(prove|derive|step[- ]by[- ]step|why|compare|analy[sz]e|debug|optimi[sz]e|design|"
                            r"algorithm|logic|reason|exam|word\s+problem|plan|trade-?offs?)\b", re.I)
_CODE_REQUEST = re.compile(r"\b(write|generate|create|implement|make)\b.{0,40}\b(function|program|script|class|code)\b", re.I)


@dataclass(frozen=True)
class Split:
    reasoning: str
    answer: str
    complete: bool
    truncated: bool


def split_reasoning(raw: str) -> Split:
    text = str(raw or "")
    blocks = THINK_BLOCK.findall(text)
    if blocks:
        answer = THINK_BLOCK.sub("", text).strip()
        trail = "\n\n".join(b.strip() for b in blocks)[:MAX_TRAIL_CHARS]
        return Split(reasoning=trail, answer=answer, complete=True, truncated=False)
    close = THINK_CLOSE.search(text)
    if close:  # the chat template already emitted the opening tag
        trail = THINK_OPEN.sub("", text[:close.start()]).strip()[:MAX_TRAIL_CHARS]
        return Split(reasoning=trail, answer=text[close.end():].strip(), complete=True, truncated=False)
    opened = THINK_OPEN.search(text)
    if opened:  # reasoning started and never closed: the budget ran out
        return Split(reasoning=text[opened.end():].strip()[:MAX_TRAIL_CHARS], answer="", complete=False, truncated=True)
    return Split(reasoning="", answer=text.strip(), complete=True, truncated=False)


def classify_complexity(text: str) -> str:
    score = 0
    words = len(text.split())
    if words > 25:
        score += 1
    if words > 60:
        score += 1
    score += min(3, len(_COMPLEX_WORDS.findall(text)))
    if re.search(r"\d\s*[-+*/^×÷%]\s*\d", text) and re.search(r"\b(solve|calculate|compute|find)\b", text, re.I):
        score += 1
    if _CODE_REQUEST.search(text) or "```" in text:
        score += 1
    if score >= 3:
        return "complex"
    if score >= 1:
        return "standard"
    return "simple"


class ReasoningParser:
    """Facade used by the agents. parse() splits a raw model output into the reasoning trail and the answer."""

    @staticmethod
    def parse(raw: str) -> Split:
        parsed = split_reasoning(raw)
        return Split(reasoning=parsed.reasoning, answer=THINK_ANY.sub("", parsed.answer).strip(),
                     complete=parsed.complete, truncated=parsed.truncated)

    @staticmethod
    def render_trail(trail: str, limit: int = 1200) -> str:
        """Plain text for the UI's 'Thinking' panel, cut at a line boundary when it is too long."""
        text = (trail or "").strip()
        if len(text) <= limit:
            return text
        head = text[:limit].rsplit("\n", 1)[0] or text[:limit]
        return head + "\n... (trail shortened)"
