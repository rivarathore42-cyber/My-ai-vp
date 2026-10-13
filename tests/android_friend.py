"""Section U of the suite: Android intents, the permission guard, and Galaxy Friend Mode (Phase 6).

Covers the intent builders and URL allowlist (including injection attempts), the schema, the voice routes, the permission
guard (no executable URL before a decision, owner-scoped and single-use decisions, expiry, Always Allow rules, revoke),
Friend Mode (dynamic nickname, no hard-coded names, onboarding validation, boundaries that block categories), and the
permission modal's pure logic (run through Node) and its place in the composed page.
"""
import ast
import json
import subprocess
import tempfile
from pathlib import Path

from galaxy import android_intents as AI
from galaxy import friend_mode as FR
from galaxy import media_controller as MC
from galaxy import voice_router as VR
from galaxy import web_assets
from galaxy.memory import MemoryStore
from galaxy.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[1]


def _store() -> MemoryStore:
    return MemoryStore(Path(tempfile.mkdtemp(prefix="android_u_")) / "u.db")


def _api_page() -> str:
    tree = ast.parse((ROOT / "galaxy" / "api.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "PAGE" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("PAGE literal not found in api.py")


def _raises(fn, exc=ValueError) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


async def run(check) -> None:
    # --- intent builders and the URL allowlist -------------------------------------------------
    built = {
        "youtube": AI.youtube_search("Free Fire gameplay"), "spotify-open": AI.spotify_open(library=True),
        "spotify-search": AI.spotify_search("lofi mix"), "whatsapp-open": AI.whatsapp_open(),
        "whatsapp-chat": AI.whatsapp_chat("919876543210", "Mom"),
        "whatsapp-message": AI.whatsapp_message("919876543210", "Mom", "I am late"),
        "call": AI.call("+919876543210", "Hansraj"), "camera": AI.camera(),
        "settings": AI.settings("wifi"), "notify": AI.notify("Dinner ready"),
    }
    for label, spec in built.items():
        url = spec.action.get("url")
        check(spec.action["type"] in AI.ACTION_TYPES and (url is None or AI.safe_url(url)),
              f"U-intent: {label} is on the allowlist")
    check(AI.spotify_search("lofi mix").action["url"].startswith(
        "intent://open.spotify.com/search/lofi%20mix#Intent;scheme=https;package=com.spotify.music;"),
        "U-intent: Spotify search uses the Spotify package with an https fallback")
    hostile = AI.spotify_search("x;package=com.evil.app#Intent").action["url"]
    check(hostile.count("package=") == 1 and "package=com.spotify.music" in hostile and hostile.count("#Intent") == 1
          and hostile.count(";") == 4, "U-inject: a query cannot add a package, a fragment or a parameter")
    message = AI.whatsapp_message("919876543210", "Mom", "hi&text=x#frag").action["url"]
    check("&text=x" not in message and "%26text%3Dx%23frag" in message,
          "U-inject: message text is percent-encoded and cannot add parameters")
    check(AI.notify("a\x00b\x07c").action["body"] == "a b c", "U-inject: control characters are removed")
    for bad in ["javascript:alert(1)", "https://evil.example/", "https://open.spotify.com.evil.example/",
                "https://evil.example@open.spotify.com/", "tel:abc", "tel:+91",
                "intent:#Intent;action=android.settings.DEVELOPMENT_SETTINGS;end"]:
        check(not AI.safe_url(bad), f"U-url: refused {bad[:44]!r}")
    check(not AI.safe_url(AI.package_intent("open.spotify.com", "/", "com.evil.app", "https://open.spotify.com/")),
          "U-url: an unlisted package is refused")
    check(_raises(lambda: AI.settings("camera")), "U-validate: an unlisted settings screen is refused")
    check(_raises(lambda: AI.spotify_search("   ")), "U-validate: an empty search is refused")
    check(_raises(lambda: AI.call("abc", "x")), "U-validate: an invalid phone number is refused")
    check(_raises(lambda: AI.validate(AI.IntentSpec(kind="call", title="x", why="y", risk="high", destination="d",
                                                    action={"type": "dial", "url": "tel:+919876543210"}, standing_ok=True))),
          "U-validate: a high-risk action can never be remembered")
    check(all(built[k].standing_ok is False for k in ("call", "whatsapp-chat", "whatsapp-message")),
          "U-policy: calls and messages are never Always Allow eligible")
    check(all(s.kind in AI.CATEGORY for s in built.values()), "U-schema: every kind is in the schema")

    # --- voice routes -----------------------------------------------------------------------------
    cases = [
        ("Open my Spotify library and play the first song", "spotify_open", "library"),
        ("Spotify par lofi mix search karo", "spotify_search", "lofi mix"),
        ("open camera", "camera", ""), ("camera kholo", "camera", ""),
        ("what is a camera sensor", "reasoning", ""),
        ("open wifi settings", "settings", "wifi"), ("bluetooth settings kholo", "settings", "bluetooth"),
        ("send a WhatsApp message to Mom saying I am late", "whatsapp_message", "Mom"),
        ("व्हाट्सएप पर मम्मी को मैसेज करो कि मैं लेट हूँ", "whatsapp_message", "मम्मी"),
        ("send a notification saying dinner is ready", "notify", "dinner is ready"),
        ("call Hansraj", "call", "Hansraj"),
        ("what is 2 plus 2", "reasoning", ""),
    ]
    for text, kind, want in cases:
        route = VR.classify(text)
        got = route.query if route.name in MC.MEDIA_KEYS else (route.name or route.query)
        check(route.kind == kind and (not want or got == want), f"U-route: {text!r} -> {kind}")
    check(VR.classify("व्हाट्सएप पर मम्मी को मैसेज करो कि मैं लेट हूँ").query == "मैं लेट हूँ",
          "U-route: the Hindi message body is kept")

    # --- the permission guard -----------------------------------------------------------------
    store = _store()
    orch = Orchestrator(store)
    orch._voice_turn("save contact Hansraj +91 98765 43210", "user_a")
    call = orch._voice_turn("call Hansraj", "user_a")
    rid = call["action"]["request_id"]
    check(call["action"]["type"] == "permission_request" and "tel:" not in json.dumps(call),
          "U-guard: a call carries no dialer URL until a decision")
    check(call["action"]["mode"] == "full" and call["action"]["can_always"] is False and call["action"]["risk"] == "high",
          "U-guard: a call is high risk, full prompt, no Always Allow")
    check(orch.handle_decision({"user_id": "user_a", "request_id": rid, "decision": "always"})["status"] == "refused",
          "U-guard: calls cannot be Always Allow, even if the client asks")
    check(orch.handle_decision({"user_id": "user_b", "request_id": rid, "decision": "once"})["status"] == "refused",
          "U-guard: another user cannot decide this request")
    check(orch.handle_decision({"request_id": rid, "decision": "once"})["status"] == "refused",
          "U-guard: an anonymous decision is refused")
    check(orch.handle_decision({"user_id": "user_a", "request_id": "a" * 32, "decision": "once"})["status"] == "refused",
          "U-guard: a forged request id is refused")
    check(orch.handle_decision({"user_id": "user_a", "request_id": rid, "decision": "yes please"})["status"] == "refused",
          "U-guard: an unknown decision is refused")
    done = orch.handle_decision({"user_id": "user_a", "request_id": rid, "decision": "once"})
    check(done["status"] == "approved" and done["action"] == {"type": "dial", "url": "tel:+919876543210", "label": "Call: Hansraj"},
          "U-guard: Allow Once returns the dialer action")
    check(orch.handle_decision({"user_id": "user_a", "request_id": rid, "decision": "once"})["status"] == "refused",
          "U-guard: a decision is single use")

    deny = orch._voice_turn("call Hansraj", "user_a")
    denied = orch.handle_decision({"user_id": "user_a", "request_id": deny["action"]["request_id"], "decision": "deny"})
    check(denied["status"] == "denied" and "action" not in denied, "U-guard: Deny returns no action")
    check(orch.handle_decision({"user_id": "user_a", "request_id": deny["action"]["request_id"],
                                "decision": "once"})["status"] == "refused",
          "U-guard: a denied request cannot be approved later")

    old = orch._voice_turn("call Hansraj", "user_a")
    store._conn.execute("UPDATE android_requests SET expires_at=0 WHERE request_id=?", (old["action"]["request_id"],))
    check(orch.handle_decision({"user_id": "user_a", "request_id": old["action"]["request_id"],
                                "decision": "once"})["status"] == "expired",
          "U-guard: an expired request is never executed")

    store.profile_save("user_a", FR.build_profile({"nickname": "", "tone": "warm",
                                                    "boundaries": ["allow:media", "always_allow"], "confirm": True}))
    s1 = orch._voice_turn("open spotify", "user_a")
    check(s1["action"]["mode"] == "full" and s1["action"]["can_always"] is True,
          "U-always: a low-risk request offers Always Allow")
    remembered = orch.handle_decision({"user_id": "user_a", "request_id": s1["action"]["request_id"], "decision": "always"})
    check(remembered["status"] == "approved" and store.grant_active("user_a", "spotify"),
          "U-always: Always Allow is remembered for this kind")
    s2 = orch._voice_turn("open spotify", "user_a")
    check(s2["action"]["mode"] == "streamlined" and "url" not in s2["action"],
          "U-always: the next request is streamlined and still needs a tap, with no URL before the decision")
    check(orch.handle_revoke({"user_id": "user_a", "kind": "spotify"})["status"] == "revoked",
          "U-always: revoke removes the remembered decision")
    check(orch._voice_turn("open spotify", "user_a")["action"]["mode"] == "full",
          "U-always: after revoke the full prompt returns")
    check(orch.handle_revoke({"user_id": "user_b", "kind": "spotify"})["status"] == "none",
          "U-always: another user cannot revoke it")
    check(orch.handle_revoke({"user_id": "user_a", "kind": "call"})["status"] == "refused",
          "U-always: calls have nothing to revoke")

    trace = orch.handle_trace({"user_id": "user_a", "limit": 50})
    check(any(r["status"] == "executed" for r in trace["requests"])
          and all("action_json" not in r and "url" not in json.dumps(r) for r in trace["requests"]),
          "U-trace: the execution trace lists outcomes without URLs")

    # --- Friend Mode -------------------------------------------------------------------------------
    nick = FR.sanitize_nickname("<b>सुनील</b> भाई!!")
    check("<" not in nick and ">" not in nick and "सुनील" in nick and len(nick) <= 24,
          "U-friend: a nickname is cleaned of markup and kept short")
    check(len(FR.sanitize_nickname("x" * 200)) <= 24, "U-friend: a long nickname is cut")
    check(FR.persona_instruction(None) is None
          and FR.persona_instruction({"enabled": False, "nickname": "भैया"}) is None,
          "U-friend: no persona when Friend Mode is off")
    persona = FR.persona_instruction({"enabled": True, "nickname": "भैया", "tone": "warm",
                                      "boundaries": ["no_money_decisions"]})
    check("'भैया'" in persona and "Sunil" not in persona and "no_money_decisions" in persona,
          "U-friend: the persona uses the user's own nickname and boundary")
    neutral = FR.persona_instruction({"enabled": True, "nickname": "", "tone": "casual", "boundaries": []})
    check("neutral" in neutral and "never invent" in neutral, "U-friend: without a nickname no name is invented")
    check(FR.greeting({"nickname": "सुनील भाई", "tone": "warm"}) == "और सुनील भाई, क्या हाल-चाल? क्या करना है आज?",
          "U-friend: the warm greeting uses the stored nickname")
    check(FR.greeting({"nickname": "", "tone": "warm"}).startswith("और, क्या हाल-चाल"),
          "U-friend: without a nickname the greeting stays neutral")
    check(FR.next_question({})["step"] == "nickname", "U-onboard: the first question asks for a nickname")
    check(FR.next_question({"nickname": "", "tone": "warm", "boundaries": []})["step"] == "confirm",
          "U-onboard: the last question is the confirmation")
    check(_raises(lambda: FR.validate_step("tone", "angry")), "U-onboard: an unknown tone is refused")
    check(_raises(lambda: FR.validate_step("boundaries", ["allow:hacking"])), "U-onboard: an unknown boundary is refused")
    check(_raises(lambda: FR.validate_step("confirm", False)), "U-onboard: Friend Mode needs a confirmation")
    check(_raises(lambda: FR.build_profile({"nickname": "x", "tone": "warm", "boundaries": []})),
          "U-onboard: an incomplete onboarding is refused")
    check(_raises(lambda: FR.build_profile({"nickname": "x", "tone": "warm", "boundaries": [], "confirm": True, "x": 1})),
          "U-onboard: an unknown onboarding field is refused")
    profile = FR.build_profile({"nickname": "skip", "tone": "formal", "boundaries": ["allow:calls", "allow:media"],
                                "confirm": True})
    check(profile == {"nickname": "", "tone": "formal", "boundaries": ["allow:calls", "allow:media"], "enabled": True},
          "U-onboard: 'skip' keeps no nickname and the profile is enabled")
    check(FR.allows(None, "calls") and not FR.allows({"enabled": True, "boundaries": []}, "calls")
          and FR.allows({"enabled": True, "boundaries": ["allow:calls"]}, "calls"),
          "U-friend: boundaries decide the categories when Friend Mode is on, and all are open when it is off")

    store.profile_save("user_c", FR.build_profile({"nickname": "भैया", "tone": "warm",
                                                   "boundaries": ["allow:media"], "confirm": True}))
    orch._voice_turn("save contact Mom +91 98765 43211", "user_c")
    blocked = orch._voice_turn("call Mom", "user_c")
    check(blocked["action"]["type"] == "none" and "Friend Mode" in blocked["text"],
          "U-friend: a category outside the boundaries never reaches the permission modal")
    check(orch._voice_turn("open spotify", "user_c")["action"]["type"] == "permission_request",
          "U-friend: an allowed category still asks first")
    check(orch.persona_for("user_c") is not None and orch.persona_for("nobody_here") is None,
          "U-friend: the persona is prepared per owner")

    # --- no user name is hard-coded anywhere in the source --------------------------------------
    for f in sorted(p for p in (ROOT / "galaxy").rglob("*") if p.is_file() and p.suffix in (".py", ".js", ".html", ".css", ".sql")):
        text = f.read_text(encoding="utf-8")
        check("Sunil" not in text and "सुनील" not in text, f"U-names: {f.name} hard-codes no user name")

    # --- the permission modal in the page
    page = (ROOT / "galaxy" / "web" / "app.html").read_text(encoding="utf-8")
    for needle in ("/static/voice/permission_modal.js", "/static/voice/permission_modal.css", 'id="gvPermModal"',
                   'id="gvPermActions"'):
        check(needle in page, f"U-page: the page contains {needle}")
    app_js = (ROOT / "galaxy" / "web" / "app.js").read_text(encoding="utf-8")
    check("window.galaxyHeaders" in app_js and "window.galaxyUser" in app_js and "window.galaxyVoiceSend" in app_js,
          "U-page: the page provides the hooks the permission and voice scripts call")
    check(not any(x in page for x in ('src="http', 'href="http')), "U-page: the permission modal loads nothing external")

    # --- the modal's pure logic (Node) --------------------------------------------------------------
    node = subprocess.run(["node", str(ROOT / "tests" / "js" / "permission_modal.test.js")], capture_output=True, text=True)
    check(node.returncode == 0 and "permission modal checks passed" in node.stdout,
          "U-browser: the permission modal's logic passes its Node tests")
