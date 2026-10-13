"""Identity binding (Phase 7, hardened in Phase 8): one identity per request, decided by its Bearer token.

Trust model
- The operator token (GALAXY_API_TOKEN) is an administrator credential, not a user. It may use the API anonymously and may
  issue or revoke user tokens. It cannot act as a user.
- A user token is issued by the operator for one user id, expires, and can be rotated by its owner. The store keeps only
  its SHA-256 hash. A request with a live user token acts for that user and for no other.
- A request body never identifies the user. Any user_id in the body is refused (even a matching one), and any "_session"
  key is discarded. The token alone decides who is asking and which session asked.
- In-process callers (tests, a local CLI) pass no identity; their body user_id is used. That path is not reachable over
  HTTP, because api.py always passes an identity or refuses the request.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .core import owner_id

TOKEN_PREFIX = "gx_"
TOKEN_BYTES = 32


class IdentityMismatch(Exception):
    """The request tries to name a user, or an identity its token does not carry."""


@dataclass(frozen=True)
class Identity:
    kind: str                       # "user" or "operator"
    user_id: Optional[str] = None   # set only for "user"
    token_id: str = ""              # short, non-secret id for audit, rotation and the request's session


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_id(token: str) -> str:
    return token_hash(token)[:12]


def new_user_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def credential_of(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, credential = authorization.strip().partition(" ")
    credential = credential.strip()
    if scheme.lower() != "bearer" or not credential:
        return None
    return credential


def identity_from_bearer(authorization: Optional[str], operator_token: str,
                         lookup: Callable[[str], Optional[str]]) -> Optional[Identity]:
    """The identity behind a Bearer header, or None when the request is unauthenticated."""
    credential = credential_of(authorization)
    if credential is None:
        return None
    if operator_token and hmac.compare_digest(credential.encode("utf-8"), operator_token.encode("utf-8")):
        return Identity("operator", None, "operator")
    user_id = lookup(credential)
    if user_id:
        return Identity("user", user_id, token_id(credential))
    return None


def is_admin(identity: Optional[Identity]) -> bool:
    return identity is not None and identity.kind == "operator"


def is_user(identity: Optional[Identity]) -> bool:
    return identity is not None and identity.kind == "user"


def bind(body: Any, identity: Identity) -> Any:
    """A copy of the body that carries the identity's user and session. Anything the body says about the user is refused."""
    if not isinstance(body, dict):
        return body
    out = {key: value for key, value in body.items() if key != "_session"}
    if out.get("user_id") not in (None, ""):
        raise IdentityMismatch("do not send user_id; your token identifies you")
    out.pop("user_id", None)
    if identity.kind == "operator":
        return out
    out["user_id"] = identity.user_id
    out["_session"] = identity.token_id
    return out
