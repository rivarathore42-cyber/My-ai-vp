"""Shared prompt parameter type. Kept separate so that creative_forms can import it without importing prompts."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GenParams:
    max_tokens: int
    temperature: float
