"""Memory watch (Phase 12): how much memory this process holds, read from the operating system.

Linux (Render, Hugging Face, Docker) reports it. Other systems return None rather than a guess.
"""
from __future__ import annotations

from typing import Optional


def rss_mb() -> Optional[float]:
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None
