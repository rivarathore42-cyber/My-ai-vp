"""Section T of the suite: the offline voice path, end to end, without HTTP.

Covers the Node tests for the Vosk wrapper, the static allowlist, the page composition (the PAGE literal is read from
api.py with ast, so FastAPI is not needed), the offline rule for everything in galaxy/web (no speech-service controller,
no other origin), and the voice path of Orchestrator._handle, which is the function /api/chat calls.
"""
import ast
import json
import re
import subprocess
import tempfile
from pathlib import Path

from galaxy import platforms as _platforms
from galaxy import web_assets
from galaxy.memory import MemoryStore
from galaxy.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[1]
# The only destinations the voice and permission UI may name (launch targets, never page resources).
ALLOWED_DESTINATIONS = set(_platforms.hosts())
WEB = ROOT / "galaxy" / "web"


def _api_page() -> str:
    tree = ast.parse((ROOT / "galaxy" / "api.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "PAGE" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("PAGE literal not found in api.py")


def _store() -> MemoryStore:
    return MemoryStore(Path(tempfile.mkdtemp(prefix="voice_t_")) / "t.db")


async def run(check) -> None:
    for script in ("tests/js/voice_vosk.test.js", "tests/js/voice_wakeup.test.js"):
        result = subprocess.run(["node", str(ROOT / script)], capture_output=True, text=True)
        check(result.returncode == 0 and "checks passed" in result.stdout, f"T-browser: {script} passes")

    for f in sorted(WEB.iterdir()):
        if f.suffix in (".js", ".css", ".html"):
            text = f.read_text(encoding="utf-8")
            check("SpeechRecognition" not in text, f"T-offline: {f.name} has no speech-service controller")
            loads = re.compile(r"""(?:src|href)\s*=\s*["']?https?://|fetch\(\s*["'`]https?://|import\(\s*["'`]https?://|importScripts\(""")
            check(not loads.search(text), f"T-offline: {f.name} loads nothing from another origin")
            hosts = set(re.findall(r"https://([a-z0-9.-]+)", text))
            check(hosts <= ALLOWED_DESTINATIONS, f"T-offline: {f.name} names only allowlisted destinations")
            check(not re.search(r"http://", text), f"T-offline: {f.name} uses no plain http")

    page = (WEB / "app.html").read_text(encoding="utf-8")
    for needle in ("/static/voice/voice_wakeup.css", "/static/voice/permission_modal.css", "/static/voice/app.css",
                   "/static/voice/voice_wakeup.js", "/static/voice/permission_modal.js", "/static/voice/voice_ui.js",
                   "/static/voice/app.js", 'id="gvWake"', 'id="gvFab"', 'id="gvPermModal"'):
        check(needle in page, f"T-page: the page loads or contains {needle[:44]}")
    check(not re.search(r'(?:src|href)="https?://', page), "T-page: the page loads nothing from another origin")
    check(page.count("<script") == page.count("</script>"), "T-page: script tags are balanced")
    check("(no answer)" not in (WEB / "app.js").read_text(encoding="utf-8"), "T-page: the page never prints '(no answer)'")
    base = Path(tempfile.mkdtemp(prefix="assets_"))
    (base / "models").mkdir()
    (base / "vendor").mkdir()
    outside = Path(tempfile.mkdtemp(prefix="outside_")) / "secret.txt"
    outside.write_text("not for the web", encoding="utf-8")
    (base / "vendor" / "vosk.js").symlink_to(outside)
    (base / "models" / "vosk-model-small-en-in-0.4.tar.gz").write_bytes(b"x")
    check(web_assets.resolve_asset("voice_wakeup.js") is not None, "T-static: an allowlisted script is served")
    check(web_assets.resolve_asset("../api.py") is None, "T-static: parent-directory traversal is refused")
    check(web_assets.resolve_asset("/etc/passwd") is None, "T-static: absolute paths are refused")
    check(web_assets.resolve_asset("models/../../api.py") is None, "T-static: traversal inside a model path is refused")
    check(web_assets.resolve_asset("api.py") is None, "T-static: a file type outside the allowlist is refused")
    check(web_assets.resolve_asset("vendor/vosk.js", base) is None, "T-static: a symlink that leaves the web root is refused")
    check(web_assets.resolve_asset("models/vosk-model-small-en-in-0.4.tar.gz", base) is not None,
          "T-static: a placed model archive is served")
    check(web_assets.resolve_asset("models/vosk-model-small-hi-0.22.tar.gz", base) is None,
          "T-static: a model that is not placed yet gives a 404, not a crash")
    check(web_assets.content_type(Path("x.js")) == "application/javascript"
          and web_assets.content_type(Path("m.tar.gz")) == "application/gzip", "T-static: content types")

    store = _store()
    orch = Orchestrator(store)
    r = await orch._handle({"message": "open youtube and search Free Fire gameplay", "user_id": "user_a", "source": "voice"})
    check(r.get("action", {}).get("type") == "permission_request" and "search_query" not in json.dumps(r),
          "T-route: a voice YouTube search asks for permission first, with no URL yet")
    r = await orch._handle({"message": "save contact Hansraj +91 98765 43210", "user_id": "user_a", "source": "voice"})
    check("save ho gaya" in r["text"], "T-route: a voice contact is saved for its owner")
    r = await orch._handle({"message": "Hey Galaxy, call Hansraj", "user_id": "user_a", "source": "voice"})
    check(r["action"]["type"] == "permission_request" and "tel:" not in json.dumps(r),
          "T-route: a voice call asks for permission first, with no dialer URL")
    r = await orch._handle({"message": "Hey Galaxy, call Hansraj", "user_id": "user_b", "source": "voice"})
    check(r["action"]["type"] == "none", "T-route: another owner gets no contact")
    r = await orch._handle({"message": "call Hansraj", "source": "voice"})
    check(r["action"]["type"] == "none" and "user_id" in r["text"], "T-route: an anonymous voice turn gets no contacts")
    r = await orch._handle({"message": "Hey Galaxy, what is 2 plus 2", "user_id": "user_a", "source": "voice"})
    check("4" in r["text"] and "action" not in r, "T-route: a voice question reaches the agents with the wake word removed")
    r = await orch._handle({"message": "what is 2 plus 2", "user_id": "user_a"})
    check("4" in r["text"] and "action" not in r, "T-route: a typed question is not routed as a voice command")
