"""Section S of the suite: Hey Galaxy voice routing (Phase 5).

Covers the router (English and Hindi phrases), owner-only contacts at the storage layer, the voice actions the browser
receives (every app action needs a tap; nothing runs on the server), and the browser's pure wake-word logic, which runs
through Node.
"""
import subprocess
import tempfile
from pathlib import Path

from galaxy import media_controller as MC
from galaxy import voice_router as VR
from galaxy.memory import MemoryStore
from galaxy.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[1]

CASES = [
    ("Hey Galaxy, open YouTube and search Free Fire gameplay", "youtube_search", "Free Fire gameplay"),
    ("YouTube par Free Fire gameplay khojo", "youtube_search", "Free Fire gameplay"),
    ("यूट्यूब पर Free Fire gameplay खोजो", "youtube_search", "Free Fire gameplay"),
    ("search Free Fire on youtube", "youtube_search", "Free Fire"),
    ("open youtube", "youtube_open", ""),
    ("open WhatsApp and chat with Mom", "whatsapp_chat", "Mom"),
    ("WhatsApp par Mom ko message karo", "whatsapp_chat", "Mom"),
    ("व्हाट्सएप पर मम्मी को मैसेज करो", "whatsapp_chat", "मम्मी"),
    ("open WhatsApp", "whatsapp_open", ""),
    ("call Hansraj", "call", "Hansraj"),
    ("call karo Hansraj", "call", "Hansraj"),
    ("हैन्सराज को कॉल करो", "call", "हैन्सराज"),
    ("call a python function", "reasoning", ""),
    ("call kar", "reasoning", ""),
    ("save contact Hansraj +91 98765 43210", "save_contact", "Hansraj"),
    ("hey galaxy", "reasoning", ""),
    ("what is 2 plus 2", "reasoning", ""),
    ("explain what a message queue is", "reasoning", ""),
]


def _store() -> MemoryStore:
    return MemoryStore(Path(tempfile.mkdtemp(prefix="voice_")) / "v.db")


def _orchestrator_for(store: MemoryStore) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)  # only the store is needed for the voice turn itself
    orch.store = store
    return orch


async def run(check) -> None:
    for text, kind, want in CASES:
        route = VR.classify(text)
        got = route.query if route.name in MC.MEDIA_KEYS else (route.name or route.query)
        check(route.kind == kind and (not want or got == want), f"S-router: {text!r} -> {kind}")

    check(VR.normalize_phone("+91 98765 43210") == "+919876543210", "S-phone: an international number is kept")
    check(VR.normalize_phone("9876543210") == "+919876543210", "S-phone: 10 digits get the default country code")
    check(VR.normalize_phone("12ab") is None and VR.normalize_phone("123") is None, "S-phone: junk and short numbers refused")
    check("a%26b+c" in VR.youtube_url("a&b c"), "S-url: the query is percent-encoded")

    store = _store()
    orch = _orchestrator_for(store)
    search = orch._voice_turn("open youtube and search javascript:alert(1)&x", "user_a")
    check(search["action"]["type"] == "permission_request" and "tel:" not in str(search),
          "S-action: a YouTube search waits for a permission decision and carries no URL yet")
    searched = orch.handle_decision({"user_id": "user_a", "request_id": search["action"]["request_id"], "decision": "once"})
    check("javascript%3Aalert%281%29%26x" in searched["action"]["url"] and "javascript:" not in searched["action"]["url"],
          "S-security: script-like text is only encoded inside the query")
    anon = orch._voice_turn("call Hansraj", None)
    check(anon["action"]["type"] == "none" and "user_id" in anon["text"], "S-privacy: anonymous sessions get no contacts")
    saved = orch._voice_turn("save contact Hansraj +91 98765 43210", "user_a")
    check("save ho gaya" in saved["text"], "S-contacts: a contact is saved for its owner")
    call = orch._voice_turn("call Hansraj", "user_a")
    check(call["action"]["type"] == "permission_request" and call["action"]["risk"] == "high"
          and call["action"]["can_always"] is False, "S-call: a call is high risk and asks first")
    dialled = orch.handle_decision({"user_id": "user_a", "request_id": call["action"]["request_id"], "decision": "once"})
    check(dialled["action"] == {"type": "dial", "url": "tel:+919876543210", "label": "Call: Hansraj"},
          "S-call: after a decision, a dial action with the saved number")
    chat = orch._voice_turn("open WhatsApp and chat with Hansraj", "user_a")
    chatted = orch.handle_decision({"user_id": "user_a", "request_id": chat["action"]["request_id"], "decision": "once"})
    check(chatted["action"]["url"] == "https://wa.me/919876543210", "S-whatsapp: the wa.me link after a decision")
    other = orch._voice_turn("call Hansraj", "user_b")
    check(other["action"]["type"] == "none", "S-isolation: another owner cannot reach the contact")
    bad = orch._voice_turn("save contact Mom +91 1234567890123456", "user_a")
    check("sahi nahi" in bad["text"] and store.contact_find("user_a", "Mom") is None,
          "S-validation: an invalid number is refused and not stored")
    check(orch._voice_turn("what is 2 plus 2", "user_a") is None,
          "S-route: a reasoning turn returns None, so the 20-agent pipeline answers")

    store.contact_save("user_a", "Hansraj", "+919000000000")
    check(store.contact_find("user_a", "Hansraj")["phone"] == "+919000000000", "S-storage: saving again updates the number")
    check(store.contact_find("user_b", "Hansraj") is None, "S-storage: lookups are scoped to the owner")
    try:
        store.contact_find(None, "Hansraj")
        check(False, "S-storage: anonymous access must be refused")
    except ValueError:
        check(True, "S-storage: anonymous access is refused at the storage layer")

    node = subprocess.run(["node", str(ROOT / "tests" / "js" / "voice_wakeup.test.js")], capture_output=True, text=True)
    check(node.returncode == 0 and "16 voice wake-up checks passed" in node.stdout,
          "S-browser: the wake-word logic passes its Node tests")
