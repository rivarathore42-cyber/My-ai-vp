"""Shared infrastructure for Galaxy AI v6.

Nothing here talks to an AI provider or needs an API key. Outbound HTTP is restricted to the
ApiGateway allow-list. Every number that changes behaviour is either a constant or a value
measured on this machine and recorded in galaxy_memory.db.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent
DEFAULT_DB = Path(os.environ.get("GALAXY_DB", ROOT / "galaxy_memory.db"))
MAX_INPUT_CHARS = 20_000

# Runtime parameters. SelfEvolutionAgent may move these, only inside TUNE_BOUNDS,
# and every change is written to the tuning_log table.
PARAMS: dict[str, float] = {"agent_timeout": 15.0, "total_timeout": 60.0}
TUNE_BOUNDS: dict[str, tuple[float, float]] = {"agent_timeout": (10.0, 15.0)}

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def clean_text(value: Any, limit: int) -> str:
    text = _CTRL.sub("", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


_OWNER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# Labels that name a session or an operator, never a person (Phase 7). A user id may not be one of these in any case.
RESERVED_OWNER_IDS = frozenset({"anon", "anonymous", "operator", "system", "admin", "root"})


def owner_id(value: Any) -> Optional[str]:
    """The only key under which personal memory is stored (RC-03). A missing, malformed or reserved id is anonymous (None)."""
    if isinstance(value, str) and _OWNER_ID.match(value) and value.lower() not in RESERVED_OWNER_IDS:
        return value
    return None


def safe_id(value: Any, default: str) -> str:
    return value if isinstance(value, str) and _SAFE_ID.match(value) else default


@dataclass
class AgentResult:
    agent: str
    ok: bool
    text: str = ""
    error: str = ""
    source: str = ""
    meta: dict = field(default_factory=dict)


def ok(agent: str, text: str, source: str = "", **meta: Any) -> AgentResult:
    return AgentResult(agent=agent, ok=True, text=text, source=source, meta=meta)


def fail(agent: str, error: str, **meta: Any) -> AgentResult:
    return AgentResult(agent=agent, ok=False, error=error, meta=meta)


class Agent:
    """Base class. Every concrete agent overrides process(); the test-suite verifies this."""

    name = "Agent"

    async def process(self, text: str, ctx: dict) -> AgentResult:
        raise NotImplementedError(f"{type(self).__name__} must override process()")


class GatewayError(RuntimeError):
    """Raised when an outbound request is refused or fails. The message is safe to show to users."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a redirect is an error: it could point at a host that is not allow-listed


class ApiGateway:
    """The only code path that talks to the internet. HTTPS + allow-listed hosts, no credentials,
    no redirects, per-host rate limit, response size cap, hard timeout."""

    ALLOWED_HOSTS = frozenset({"en.wikipedia.org"})
    MAX_BYTES = 300_000
    RATE_PER_MIN = 20
    TIMEOUT = 8.0
    LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}
        self._opener = urllib.request.build_opener(_NoRedirect)
        self.calls = 0
        self.denied = 0

    def _local_allowed(self, host: str) -> bool:
        # Test-only override, read at call time. Never set in production.
        return os.environ.get("GALAXY_GATEWAY_ALLOW_LOCAL") == "1" and host in self.LOCAL_HOSTS

    def check(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.username or parts.password:
            self.denied += 1
            raise GatewayError("credentials in URL refused")
        if not self._local_allowed(host) and (parts.scheme != "https" or host not in self.ALLOWED_HOSTS):
            self.denied += 1
            raise GatewayError(f"host not allow-listed: {host or '?'}")
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(host, []) if now - t < 60]
            if len(hits) >= self.RATE_PER_MIN:
                self.denied += 1
                raise GatewayError(f"rate limit reached for {host}")
            hits.append(now)
            self._hits[host] = hits
        return host

    def get_json(self, url: str, max_bytes: int = 0) -> Any:
        self.check(url)
        limit = max_bytes or self.MAX_BYTES
        req = urllib.request.Request(url, headers={"User-Agent": "GalaxyAI/6.0", "Accept": "application/json"})
        try:
            with self._opener.open(req, timeout=self.TIMEOUT) as resp:
                body = resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            raise GatewayError(f"upstream HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GatewayError(f"network error: {type(exc).__name__}") from None
        if len(body) > limit:
            raise GatewayError("upstream response too large")
        self.calls += 1
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise GatewayError("upstream sent invalid JSON") from None

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            recent = {h: len([t for t in v if now - t < 60]) for h, v in self._hits.items()}
        return {"allowed_hosts": sorted(self.ALLOWED_HOSTS), "upstream_calls": self.calls,
                "denied": self.denied, "requests_last_minute": recent}


def system_metrics() -> dict[str, Any]:
    """Real machine metrics. Linux-only fields are None on other platforms (reported, not guessed)."""
    metrics: dict[str, Any] = {"cpu_count": os.cpu_count() or 1, "platform": sys.platform}
    try:
        with open("/proc/loadavg", encoding="utf-8") as fh:
            metrics["load_1m"] = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        metrics["load_1m"] = None
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0])
        metrics["mem_total_mb"] = round(info["MemTotal"] / 1024, 1)
        metrics["mem_available_mb"] = round(info["MemAvailable"] / 1024, 1)
    except (OSError, ValueError, IndexError, KeyError):
        metrics["mem_total_mb"] = metrics["mem_available_mb"] = None
    try:
        import resource

        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        metrics["peak_rss_mb"] = round(raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024, 1)
    except (ImportError, OSError, ValueError):
        metrics["peak_rss_mb"] = None
    return metrics
