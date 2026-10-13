"""Static web assets: the allowlist for /static/voice/ and the content types. The page itself is web/app.html."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

WEB_DIR = Path(__file__).resolve().parent / "web"
_ALLOWED = re.compile(r"[A-Za-z0-9_-]+\.(?:js|css)|vendor/[A-Za-z0-9_.-]+\.js|models/[A-Za-z0-9_.-]+\.tar\.gz")
_TYPES = {".js": "application/javascript", ".css": "text/css", ".gz": "application/gzip"}


def resolve_asset(relpath: str, base: Path = WEB_DIR) -> Optional[Path]:
    """Path of an allowlisted static file under `base`, or None. Traversal and unlisted names are refused."""
    if not isinstance(relpath, str) or ".." in relpath or not _ALLOWED.fullmatch(relpath):
        return None
    root = base.resolve()
    candidate = (root / relpath).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def content_type(path: Path) -> str:
    return _TYPES.get(path.suffix.lower(), "application/octet-stream")
