"""Agent 18 helpers (Phase 4): process memory reading and bounded per-agent timeout budgets.

The budget rules are pure functions so the bounds can be tested directly:
  - a single change moves a budget by at most 20% and at most 5 s;
  - every budget stays between 5 s and 45 s;
  - a rising p95 trend (latest window at least 1.2 times the previous one) raises the budget by at least 10%;
  - the request invariant: the memory stage (run first, one after another) plus the slowest concurrent agent must fit
    in 55 s, which leaves 5 s of the 60 s request limit as reserve.
"""
from __future__ import annotations

from typing import Optional

FLOOR_SECONDS = 5.0
CEILING_SECONDS = 45.0
REQUEST_LIMIT_SECONDS = 60.0
RESERVE_SECONDS = 5.0
MAX_STEP_RATIO = 0.2
MAX_STEP_SECONDS = 5.0
MIN_SAMPLES = 20
TREND_RATIO = 1.2
STAGE_AGENTS = frozenset({"Memory Vault"})  # display names: budgets and telemetry use them
BUDGETS: dict[str, float] = {}


def current_rss_mb() -> Optional[float]:
    """Resident memory of this process in MB, read from /proc on Linux. None elsewhere (reported, not guessed)."""
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 2)
    except (OSError, ValueError, IndexError):
        return None
    return None


def budget_for(agent: str, default: float) -> float:
    return BUDGETS.get(agent, default)


def propose_budget(current: float, p95_ms: float, previous_p95_ms: Optional[float]) -> float:
    target = min(CEILING_SECONDS, max(FLOOR_SECONDS, p95_ms / 1000 * 3))
    if previous_p95_ms and p95_ms / previous_p95_ms >= TREND_RATIO:
        target = max(target, current * 1.1)
    step = min(current * MAX_STEP_RATIO, MAX_STEP_SECONDS)
    low, high = max(FLOOR_SECONDS, current - step), min(CEILING_SECONDS, current + step)
    return round(min(high, max(low, target)), 2)


def request_fits(budgets: dict[str, float]) -> bool:
    stage = sum(v for k, v in budgets.items() if k in STAGE_AGENTS)
    concurrent = max((v for k, v in budgets.items() if k not in STAGE_AGENTS), default=0.0)
    return stage + concurrent <= REQUEST_LIMIT_SECONDS - RESERVE_SECONDS
