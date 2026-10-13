"""Token lifetime and request integrity (Phase 8).

- TTL: every user token expires. The default is 30 days. An operator can choose from one hour to 365 days; anything outside
  that range is clamped, and a value that is not a number gets the default.
- Rotation: a user can replace their own token. The old token stops working at once (see Store.token_rotate).
- Seals: every pending action carries an HMAC over its fields, made with a key that lives only in this process. A row
  changed in the database (a different URL, a different owner, a different session, or "always") fails the check, and
  the request is cancelled instead of executed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

DAY_S = 86400
DEFAULT_TOKEN_TTL_S = 30 * DAY_S
MIN_TOKEN_TTL_S = 3600
MAX_TOKEN_TTL_S = 365 * DAY_S


def clamp_ttl(ttl_s: Any) -> int:
    try:
        value = int(ttl_s)
    except (TypeError, ValueError):
        return DEFAULT_TOKEN_TTL_S
    return max(MIN_TOKEN_TTL_S, min(MAX_TOKEN_TTL_S, value))


def is_live(expires_at: Any, revoked_at: Any, now: float) -> bool:
    return revoked_at is None and expires_at is not None and float(expires_at) > float(now)


def new_signing_key() -> bytes:
    return secrets.token_bytes(32)


class RequestSigner:
    """HMAC-SHA256 over the canonical JSON of the fields. Compared in constant time."""

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) < 32:
            raise ValueError("a signing key of at least 32 bytes is required")
        self._key = bytes(key)

    def sign(self, *fields: Any) -> str:
        message = json.dumps(["" if f is None else str(f) for f in fields], ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def verify(self, signature: Any, *fields: Any) -> bool:
        return isinstance(signature, str) and hmac.compare_digest(signature, self.sign(*fields))
