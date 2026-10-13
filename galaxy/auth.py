"""Access (Phase 12): open by default.

There is no pairing code and no password. A request that carries no credential acts for the owner, so the page opens
straight into the assistant. A request that carries an Authorization header is still checked: a valid operator token gets
administrator rights, a valid user token gets that user's rights, and anything else is refused.

Be aware: whoever can reach the URL can chat as the owner, and the answers draw on the owner's notes.
"""
from __future__ import annotations

from .core import owner_id
from .identity import Identity


def owner_from_env(value) -> str:
    return owner_id(value or "owner") or "owner"


def open_session(owner: str) -> Identity:
    """The identity of a request that carries no credential."""
    return Identity("user", owner, "open")
