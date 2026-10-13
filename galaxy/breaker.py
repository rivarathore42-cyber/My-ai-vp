"""Self-healing circuit breaker (RC-04 hardened).

States: closed (calls pass), open (calls are bypassed until the cooldown ends), half-open (one probe at a time).
Freeze and race safety:
  - every state change happens under a lock, so one breaker can be shared between threads;
  - a half-open probe holds a lease. If the probe is cancelled (release()) or never reports, the lease expires
    after probe_lease_s, so the breaker can never stay half-open forever;
  - transitions are queued and drained by the caller, so every change can be written to breaker_events.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 3, cooldown_s: float = 10.0,
                 clock: Callable[[], float] = time.monotonic, probe_lease_s: Optional[float] = None) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.probe_lease_s = cooldown_s if probe_lease_s is None else probe_lease_s
        self.clock = clock
        self.lock = threading.Lock()
        self.state = CLOSED
        self.failures = 0
        self.trips = 0
        self.opened_at = 0.0
        self.probe_started: Optional[float] = None
        self.transitions: list[str] = []

    def _move(self, new: str) -> None:
        if new != self.state:
            self.state = new
            self.transitions.append(new)

    def allow(self) -> bool:
        with self.lock:
            now = self.clock()
            if self.state == OPEN:
                if now - self.opened_at < self.cooldown_s:
                    return False
                self._move(HALF_OPEN)
                self.probe_started = None
            if self.state == HALF_OPEN:
                if self.probe_started is not None and now - self.probe_started < self.probe_lease_s:
                    return False
                self.probe_started = now
            return True

    def release(self) -> None:
        """An allowed call ended without a verdict (cancelled). Frees the probe slot at once."""
        with self.lock:
            if self.state == HALF_OPEN:
                self.probe_started = None

    def record_success(self) -> Optional[str]:
        with self.lock:
            previous = self.state
            self.failures = 0
            self.probe_started = None
            self._move(CLOSED)
            return CLOSED if previous != CLOSED else None

    def record_failure(self) -> Optional[str]:
        with self.lock:
            self.failures += 1
            self.probe_started = None
            if self.state == HALF_OPEN or self.failures >= self.failure_threshold:
                if self.state != OPEN:
                    self.trips += 1
                self.opened_at = self.clock()
                self._move(OPEN)
                return OPEN
            return None

    def is_open_now(self) -> bool:
        with self.lock:
            return self.state == OPEN and self.clock() - self.opened_at < self.cooldown_s

    def open_until(self) -> float:
        with self.lock:
            return self.opened_at + self.cooldown_s if self.state == OPEN else 0.0

    def force_open(self, until: float) -> None:
        with self.lock:
            self.opened_at = until - self.cooldown_s
            self.probe_started = None
            self._move(OPEN)

    def reset(self) -> None:
        with self.lock:
            self.failures = 0
            self.probe_started = None
            self.opened_at = 0.0
            self._move(CLOSED)

    def drain_transitions(self) -> list[str]:
        with self.lock:
            out = list(self.transitions)
            self.transitions.clear()
            return out

    def snapshot(self) -> dict:
        with self.lock:
            return {"service": self.name, "state": self.state, "failures": self.failures, "trips": self.trips}
