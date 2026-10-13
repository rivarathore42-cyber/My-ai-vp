"""Agent 17 (Phase 4): inbound gateway policy for /api routes.

Pure logic, so it can be tested without a web framework; api.py adapts it to FastAPI.
  - Fail closed: /api requires 'Authorization: Bearer <token>'. The token is compared in constant time.
  - Random token on boot when none is configured (secrets.token_urlsafe, 32 bytes).
  - Token-bucket rate limits: per client for authorised requests, and a stricter bucket for failed authentication.
  - Route guard: unknown /api routes give 404 and wrong methods give 405, both only after authentication.
  - Body limit: a declared body over 1 MB is refused with 413.
  - Buckets are bounded (LRU, 4096 keys), so an attacker cannot exhaust memory by rotating client keys.
"""
from __future__ import annotations

import hmac
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

TOKEN_BYTES = 32
MAX_RETRY_AFTER_S = 3600.0
MAX_BODY_BYTES = 1_000_000
MAX_KEYS = 4096
ROUTES = {("POST", "/api/chat"), ("GET", "/api/health"), ("GET", "/api/me"), ("GET", "/api/tuning"),
          ("POST", "/api/evolution/tune"), ("POST", "/api/android/decision"), ("POST", "/api/android/revoke"),
          ("POST", "/api/android/trace"), ("POST", "/api/friend/profile"), ("GET", "/api/admin/tokens"),
          ("POST", "/api/admin/tokens"), ("POST", "/api/admin/tokens/revoke"), ("POST", "/api/me/rotate")}  # every route the API serves (was missing the Android and profile routes)
PARAM_ROUTES = {("GET", "/api/memory/")}


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def resolve_token(configured: Optional[str]) -> tuple[str, bool]:
    """(token, generated). A configured token is used as-is; otherwise a random one is generated."""
    token = (configured or "").strip()  # A17-01: a whitespace-only value is unset
    if token:
        return token, False
    return new_token(), True


class RateLimiter:
    def __init__(self, capacity: float, refill_per_s: float, max_keys: int = MAX_KEYS,
                 clock=time.monotonic) -> None:
        self.capacity = float(capacity)
        self.refill = float(refill_per_s)
        self.max_keys = max_keys
        self.clock = clock
        self._buckets: "OrderedDict[str, tuple[float, float]]" = OrderedDict()

    def _level(self, key: str, now: float) -> float:
        tokens, stamp = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - stamp) * self.refill)
        self._buckets[key] = (tokens, now)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)
        return tokens

    def take(self, key: str, now: Optional[float] = None) -> tuple[bool, float]:
        now = self.clock() if now is None else now
        tokens = self._level(key, now)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            return True, 0.0
        return False, min(MAX_RETRY_AFTER_S, round((1 - tokens) / self.refill, 2)) if self.refill > 0 else MAX_RETRY_AFTER_S

    def has_token(self, key: str, now: Optional[float] = None) -> bool:
        return self._level(key, self.clock() if now is None else now) >= 1

    def tracked_keys(self) -> int:
        return len(self._buckets)


@dataclass
class Decision:
    allowed: bool
    status: int = 200
    body: Optional[dict] = None
    headers: dict = field(default_factory=dict)


class GuardPolicy:
    def __init__(self, token: str, api_capacity: float = 30, api_refill_per_s: float = 2.0,
                 auth_capacity: float = 10, auth_refill_per_s: float = 1 / 60,
                 max_body: int = MAX_BODY_BYTES, clock=time.monotonic, accept=None, open_access: bool = False) -> None:
        if not token:
            raise ValueError("a non-empty token is required (fail closed)")
        self._token = token.encode("utf-8")
        self._accept = accept  # Phase 7: a user token is valid when the store resolves it
        self._open = open_access  # local mode (Phase 9): the gate admits every request
        self.api_rate = RateLimiter(api_capacity, api_refill_per_s, clock=clock)
        self.auth_rate = RateLimiter(auth_capacity, auth_refill_per_s, clock=clock)
        self.max_body = max_body
        self.clock = clock

    def _valid(self, authorization: Optional[str]) -> bool:
        if self._open:
            return True
        if not authorization:
            return False
        scheme, _, credential = authorization.strip().partition(" ")
        if scheme.lower() != "bearer" or not credential:  # A17-02: the scheme is case-insensitive (RFC 7235)
            return False
        if hmac.compare_digest(credential.strip().encode("utf-8"), self._token):
            return True
        return bool(self._accept and self._accept(credential.strip()))

    def _route(self, method: str, path: str) -> str:
        if (method, path) in ROUTES:
            return "ok"
        if any(path == p for _, p in ROUTES):
            return "method"
        if path.startswith("/api/memory/"):
            rest = path[len("/api/memory/"):]
            if rest and "/" not in rest:
                return "ok" if method == "GET" else "method"
        return "unknown"

    @staticmethod
    def _deny(status: int, message: str, retry_after: Optional[float] = None) -> Decision:
        headers = {"Retry-After": str(max(1, int(round(min(MAX_RETRY_AFTER_S, retry_after or 1)))))} if status == 429 else {}
        return Decision(False, status, {"error": message}, headers)

    def check(self, method: str, path: str, authorization: Optional[str], client_key: str,
              content_length: int = 0, now: Optional[float] = None) -> Decision:
        now = self.clock() if now is None else now
        if not (path == "/api" or path.startswith("/api/")):  # A17-03
            return Decision(True, 200)
        if not self.auth_rate.has_token(client_key, now):
            _, wait = self.auth_rate.take(client_key, now)
            return self._deny(429, "too many failed authentication attempts", wait)
        if not self._valid(authorization):
            self.auth_rate.take(client_key, now)
            return self._deny(401, "unauthorized: send 'Authorization: Bearer <token>'")
        if content_length > self.max_body:
            return self._deny(413, f"request body larger than {self.max_body} bytes")
        route = self._route(method.upper(), path)
        if route == "unknown":
            return self._deny(404, "no such /api route")
        if route == "method":
            return self._deny(405, "method not allowed for this route")
        allowed, wait = self.api_rate.take(client_key, now)
        if not allowed:
            return self._deny(429, "rate limit exceeded", wait)
        return Decision(True, 200)
