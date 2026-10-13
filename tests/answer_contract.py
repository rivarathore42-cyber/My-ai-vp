"""Section X: the reply contract and the page's selectors (Phase 9).

Every chat reply carries text, ok and status. An ordinary question never comes back empty or as a raw internal message, even
with no model loaded; when nothing can be answered, the reply says why and what to do next. The page's scripts only use
elements that exist in its markup, so no selector can point at nothing.
"""
import json
import re
import tempfile
from pathlib import Path

from galaxy.memory import MemoryStore
from galaxy.orchestrator import Orchestrator, _contract, _friendly_reason

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "galaxy" / "web"
PROMPTS = ["calculate 144/12", "what is photosynthesis", "who is the prime minister of India", "latest news about AI",
           "write a haiku about rain", "translate hello to hindi", "analyze this python: def f(): return 1",
           "remember my favourite colour is blue", "hi", "", "   ", "@#$%^", "tell me a joke", "explain recursion"]


async def run(check) -> None:
    check(_contract(None)["ok"] is False and _contract(None)["text"].strip() != "",
          "X-contract: a reply that is not a dict still gets text, ok and status")
    check(_contract({"text": "  "})["status"] == "degraded", "X-contract: a blank text is never shown as an answer")
    check(_contract({"text": "fine"})["ok"] is True and _contract({"text": "fine"})["status"] == "ok",
          "X-contract: a real text keeps its meaning and gains ok and status")
    check(_friendly_reason("no offline knowledge source here").startswith("offline knowledge"),
          "X-reason: a missing offline topic is explained in plain words")
    check(_friendly_reason("query too short after cleaning").startswith("sawaal bahut chhota"),
          "X-reason: a short question is explained in plain words")
    check(_friendly_reason("something unexpected") == "ek internal step fail hua",
          "X-reason: an unknown failure still gets a plain sentence")

    store = MemoryStore(Path(tempfile.mkdtemp(prefix="contract_x_")) / "x.db")
    orch = Orchestrator(store)
    bad = []
    for prompt in PROMPTS:
        reply = await orch.handle({"message": prompt})
        text = reply.get("text") if isinstance(reply, dict) else None
        if (not (isinstance(text, str) and text.strip()) or "(no answer)" in text or "no offline knowledge source" in text
                or not isinstance(reply.get("ok"), bool) or not isinstance(reply.get("status"), str)):
            bad.append(prompt)
        json.dumps(reply)
    check(not bad, f"X-replies: every ordinary question gets a real, structured reply with no model loaded "
                   f"({len(PROMPTS)} prompts; problems: {bad[:3]})")

    page = (WEB / "app.html").read_text(encoding="utf-8")
    ids_in_page = set(re.findall(r'\bid="([^"]+)"', page))
    referenced = set()
    for name in ("app.js", "voice_ui.js", "permission_modal.js"):
        source = (WEB / name).read_text(encoding="utf-8")
        referenced |= set(re.findall(r"\$\(\s*'([^']+)'\s*\)", source))
        referenced |= set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]", source))
    missing = sorted(referenced - ids_in_page)
    check(not missing, f"X-selectors: every element the scripts use exists in the page ({len(referenced)} ids; missing: {missing})")
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    check("window.galaxyOnAction =" not in app_js and "window.galaxyOnReply =" not in app_js,
          "X-hooks: app.js does not overwrite the reply and action hooks the voice layer owns")
