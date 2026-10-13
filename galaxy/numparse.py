"""Single source of truth for reading numbers from text (RC-01).

Accepted: 12, -3.5, +7, 1e5, 2.5E-3 and comma thousands groups (1,000 and 1,000,000).
Refused with ValueError: a number that overflows to infinity (1e400). A comma run that is not a thousands group
(1,2,3) is three numbers, never 123. Nothing is silently dropped or split.
"""
from __future__ import annotations

import math
import re

MAX_NUMBERS = 5000
_NUM = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?"
_TOKEN = re.compile(rf"(?<![\w.]){_NUM}(?![\w.])")
_STRICT = re.compile(rf"^{_NUM}$")
_THOUSANDS = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?![\w.])")


def parse_number(token: str) -> float:
    text = token.strip()
    if not _STRICT.match(text):
        raise ValueError(f"not a number: {token!r}")
    value = float(text.replace(",", ""))
    if not math.isfinite(value):
        raise ValueError(f"number out of range: {token!r}")
    return value


def extract_numbers(text: str) -> list[float]:
    return [parse_number(match.group(0)) for match in _TOKEN.finditer(text or "")]


def thousands_to_plain(expression: str) -> str:
    """Removes comma thousands separators from a math expression. Skipped when the expression has parentheses,
    because there a comma is a function argument separator (max(1,200) must stay two arguments)."""
    if "(" in expression:
        return expression
    return _THOUSANDS.sub(lambda match: match.group(0).replace(",", ""), expression)
