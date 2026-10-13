"""Section AC: memory on a small host (Phase 12).

The caches are bounded, and the local model runs only when it is switched on. Uploads run one at a time, so their memory
peak does not stack up. The process reports its own memory at startup and on the operator health route.
"""
from pathlib import Path

from galaxy import memory_watch

ROOT = Path(__file__).resolve().parents[1]


async def run(check) -> None:
    reading = memory_watch.rss_mb()
    if Path("/proc/self/status").exists():
        check(isinstance(reading, float) and 1 <= reading <= 4096,
              "AC-rss: on Linux the process reports its memory in MB")
    else:
        check(reading is None, "AC-rss: on other systems it says unknown rather than guessing")

    api = (ROOT / "galaxy" / "api.py").read_text(encoding="utf-8")
    check("upload_lock.locked()" in api and "async with upload_lock" in api and '"busy"' in api,
          "AC-upload: one file runs at a time; a second one gets a clear busy answer")
    check("process memory" in api and "local model" in api,
          "AC-startup: the startup log states the process memory and whether the local model is on")
    health = api[api.index('@app.get("/api/health")'):api.index('@app.post("/api/chat")')]
    check("is_admin(ident)" in health and 'payload["process_mb"]' in health,
          "AC-health: the health report is operator-only and includes the process memory")
    cache = (ROOT / "galaxy" / "fastpath.py").read_text(encoding="utf-8")
    check("HOT_LIMIT" in cache and "popitem(last=False)" in cache,
          "AC-cache: the hot answer cache is bounded and drops its oldest entry")
