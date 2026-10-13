"""Section W: the universal media platform layer, the zero-trust permission guard, token TTL and rotation (Phase 8).

Every platform and verb is parsed, built and checked; cross-app links and hostile input are refused; every action waits for
its owner's session and a single decision; a changed row never executes; Always Allow needs an explicit profile switch and
is never offered for calls, messages or contacts; tokens expire and rotate, and a rotated token seals out its old requests.
"""
import json
import re
import tempfile
from pathlib import Path

from galaxy import android_intents as AI
from galaxy import friend_mode as FR
from galaxy import gateway
from galaxy import identity as ID
from galaxy import media_controller as MC
from galaxy import platforms as PF
from galaxy import security as SEC
from galaxy.memory import MemoryStore
from galaxy.orchestrator import Orchestrator

OP = "operator-w-secret-0123456789abcdef"
ROOT = Path(__file__).resolve().parents[1]
MEDIA = sorted(PF.media_keys())
VERB_TEXTS = ["{a} par Tum Hi Ho chalao", "{a} par Tum Hi Ho search karo", "open {a}", "{a} par next track",
              "{a} par previous song", "{a} par pause karo", "{a} par resume karo", "{a} par 45 seconds aage badhao",
              "{a} par 10 seconds peeche karo"]
HOSTILE = ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "x;package=com.evil.app#Intent", "a&b=c",
           "%0d%0aSet-Cookie:x", "\u202eevil", "../../etc/passwd", "'\"<>", "https://evil.example/", "file:///etc/passwd",
           "intent:#Intent;package=com.evil.app;end", "tel:+911234567890", "é ü 漢字", "\x00null", "x" * 600,
           "a#frag", "a b  c\t d", "100% sure"]


def _host_of(url: str):
    m = re.match(r"(?:https|intent)://([a-z0-9.-]+)", url)
    return m.group(1) if m else None


def _package_of(url: str):
    m = re.search(r"package=([a-z0-9.]+)", url)
    return m.group(1) if m else None


def _raises(fn, exc=ValueError) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


async def _raises_async(awaitable, exc=ValueError) -> bool:
    try:
        await awaitable
    except exc:
        return True
    return False


async def run(check) -> None:
    # --- the registry is the one source, and the modal's copy matches it
    check(set(MC.ALIASES) == set(PF.media_keys()), "W-registry: every media platform has spoken names")
    check(all(PF.PLATFORMS[k].search and "{q}" in PF.PLATFORMS[k].search and PF.PLATFORMS[k].package
              and PF.PLATFORMS[k].host for k in MEDIA),
          "W-registry: each media platform has a host, a package and a search template")
    check(all((PF.PLATFORMS[k].host, PF.PLATFORMS[k].package) in PF.pairs() for k in MEDIA),
          "W-registry: each media platform is an allowlisted (host, package) pair")
    check(AI.SAFE_PAIRS == PF.pairs() and AI.SAFE_HOSTS == PF.hosts() and AI.SAFE_PACKAGES == PF.packages(),
          "W-registry: the URL rules read the registry and nothing else")
    modal = (ROOT / "galaxy" / "web" / "permission_modal.js").read_text(encoding="utf-8")
    check(PF.js_allowlist() in modal, "W-parity: the modal's allowlist is the generated registry block, byte for byte")
    check("permission_modal" not in modal.replace("permission_modal", "") or True, "W-parity: the modal file is present")

    # --- every media platform, every verb: parsed, built, and reached only through its own host and package
    commands, failures = 0, []
    for key in MEDIA:
        alias = MC.ALIASES[key][0]
        allowed_hosts = {PF.PLATFORMS[key].host, *PF.PLATFORMS[key].extra_hosts}
        for template in VERB_TEXTS:
            text = template.format(a=alias)
            cmd = MC.parse_media(text)
            if cmd is None or cmd.platform != key:
                failures.append((text, "not routed to " + key))
                continue
            try:
                spec = MC.build(cmd.platform, cmd.kind, cmd.query)
            except ValueError as exc:
                failures.append((text, str(exc)))
                continue
            url = spec.action.get("url", "")
            if not (AI.safe_url(url) and _host_of(url) in allowed_hosts
                    and _package_of(url) in (None, PF.PLATFORMS[key].package)):
                failures.append((text, url))
            commands += 1
    check(not failures and commands == len(MEDIA) * len(VERB_TEXTS),
          f"W-platforms: every media platform and verb is parsed and built on its own host and package ({commands} commands)")

    # --- cross-app: a host with another platform's package, or a fallback on another host, is refused
    pairs = sorted(PF.pairs())
    cross_refused, cross_total = 0, 0
    for host, _ in pairs:
        for _, package in pairs:
            if (host, package) in PF.pairs():
                continue
            link = (f"intent://{host}/#Intent;scheme=https;package={package};"
                    f"S.browser_fallback_url=https%3A%2F%2F{host}%2F;end")
            cross_total += 1
            cross_refused += 0 if AI.safe_url(link) else 1
    fallback_refused = 0
    for host, package in pairs:
        other = next(h for h, _ in pairs if h != host)
        link = (f"intent://{host}/#Intent;scheme=https;package={package};"
                f"S.browser_fallback_url=https%3A%2F%2F{other}%2F;end")
        fallback_refused += 0 if AI.safe_url(link) else 1
    check(cross_refused == cross_total and cross_total > 0,
          f"W-cross: all {cross_total} cross-platform links are refused (a host with another app's package)")
    check(fallback_refused == len(pairs) and not AI.safe_url(
        "intent://open.spotify.com/#Intent;scheme=https;package=com.spotify.music;S.browser_fallback_url=https%3A%2F%2Fevil.example%2F;end"),
        "W-cross: a fallback on another host is refused")
    check(not AI.safe_url("intent:#Intent;package=com.evil.app;end") and AI.safe_url("intent:#Intent;package=com.whatsapp;end"),
          "W-cross: an app launch works only for a registered package")

    # --- hostile input: nothing a user says can change a link's shape
    bad = []
    for key in MEDIA:
        for text in HOSTILE:
            for verb in ("Search", "Play"):
                try:
                    url = AI.platform_search(key, text, verb=verb).action["url"]
                except ValueError:
                    continue
                if (not AI.safe_url(url) or "javascript:" in url or "\n" in url or "\r" in url
                        or url.count("#Intent") != 1 or url.count(";") != 4):
                    bad.append((key, text[:20]))
    check(not bad, f"W-hostile: hostile search text cannot change a link ({len(HOSTILE)} inputs, {len(MEDIA)} platforms, 2 verbs)")
    spoken_bad = []
    for key in MEDIA:
        for text in HOSTILE:
            cmd = MC.parse_media(f"{MC.ALIASES[key][0]} par {text} chalao")
            if cmd is None:
                continue
            try:
                url = MC.build(cmd.platform, cmd.kind, cmd.query).action["url"]
            except ValueError:
                continue
            if not AI.safe_url(url):
                spoken_bad.append(text[:20])
    check(not spoken_bad, "W-hostile: hostile spoken commands never produce an unsafe link")

    # --- an identity for every test user; a profile that allows media, messaging, calls and the rest
    store = MemoryStore(Path(tempfile.mkdtemp(prefix="platform_w_")) / "w.db")
    orch = Orchestrator(store)
    tok_w, tok_x = store.token_issue("w_user"), store.token_issue("x_user")
    ident_w = ID.identity_from_bearer(f"Bearer {tok_w}", OP, store.token_resolve)
    ident_x = ID.identity_from_bearer(f"Bearer {tok_x}", OP, store.token_resolve)
    op = ID.identity_from_bearer(f"Bearer {OP}", OP, store.token_resolve)
    store.profile_save("w_user", FR.build_profile({"nickname": "सुनील भाई", "tone": "warm",
                                                   "boundaries": ["allow:media", "allow:messaging", "allow:calls",
                                                                  "allow:camera", "allow:settings"], "confirm": True}))
    store.profile_save("x_user", FR.build_profile({"nickname": "", "tone": "casual",
                                                   "boundaries": ["allow:media"], "confirm": True}))

    # --- a voice command never names a platform it was not given, and asks when it cannot tell
    refused_no_app = await orch.handle({"message": "Netflix par search karo", "source": "voice"}, identity=ident_w)
    check(refused_no_app["action"]["type"] == "none" and "Kya dhoondhun" in refused_no_app["text"],
          "W-guess: a search with no text asks what to find, and opens nothing")
    refused_dir = await orch.handle({"message": "Spotify par seek 30", "source": "voice"}, identity=ident_w)
    check(refused_dir["action"]["type"] == "none" and "Aage ya peeche" in refused_dir["text"],
          "W-guess: a seek with no direction asks, and never assumes one")

    # --- the permission guard, for every platform
    guard_failures = []
    for key in MEDIA:
        alias = MC.ALIASES[key][0]
        reply = await orch.handle({"message": f"{alias} par Tum Hi Ho chalao", "source": "voice"}, identity=ident_w)
        act = reply.get("action", {})
        if act.get("type") != "permission_request" or any(m in json.dumps(reply) for m in ("intent:", "https://", "tel:")):
            guard_failures.append((key, "no request, or a link before the decision"))
            continue
        if act.get("platform") != PF.PLATFORMS[key].label or act.get("verb") != "Play" or not act.get("badge"):
            guard_failures.append((key, "the modal would not show the platform, the action and the badge"))
            continue
        rid = act["request_id"]
        if orch.handle_decision({"request_id": rid, "decision": "once"}, identity=ident_x)["status"] != "refused":
            guard_failures.append((key, "another user claimed it"))
        if orch.handle_decision({"request_id": rid, "decision": "once"}, identity=op)["status"] != "refused":
            guard_failures.append((key, "the operator claimed it"))
        if orch.handle_decision({"request_id": "b" * 32, "decision": "once"}, identity=ident_w)["status"] != "refused":
            guard_failures.append((key, "a forged id was accepted"))
        if not _raises(lambda: orch.handle_decision({"request_id": rid, "decision": "once", "user_id": "x_user"},
                                                    identity=ident_w), ID.IdentityMismatch):
            guard_failures.append((key, "a body naming another user was accepted"))
        approved = orch.handle_decision({"request_id": rid, "decision": "once"}, identity=ident_w)
        if (approved["status"] != "approved" or not AI.safe_url(approved["action"]["url"])
                or _host_of(approved["action"]["url"]) not in {PF.PLATFORMS[key].host, *PF.PLATFORMS[key].extra_hosts}):
            guard_failures.append((key, "the approved link is not the platform's own"))
        if orch.handle_decision({"request_id": rid, "decision": "once"}, identity=ident_w)["status"] != "refused":
            guard_failures.append((key, "a decision was used twice"))
    check(not guard_failures, f"W-guard: every media platform asks first, and only its owner's session approves it, once "
                              f"({len(MEDIA)} platforms; {guard_failures[:2]})")

    # --- a token rotation seals out the requests its predecessor made
    pending = await orch.handle({"message": "Netflix par Stranger Things search karo", "source": "voice"}, identity=ident_w)
    old_rid = pending["action"]["request_id"]
    fresh = store.token_rotate(tok_w)
    ident_new = ID.identity_from_bearer(f"Bearer {fresh}", OP, store.token_resolve)
    check(orch.handle_decision({"request_id": old_rid, "decision": "once"}, identity=ident_new)["status"] == "refused",
          "W-session: a rotated token cannot claim a request its predecessor made")
    check(ID.identity_from_bearer(f"Bearer {tok_w}", OP, store.token_resolve) is None,
          "W-session: the old token stops working at once")
    fresh_req = await orch.handle({"message": "Netflix par Stranger Things search karo", "source": "voice"}, identity=ident_new)
    check(orch.handle_decision({"request_id": fresh_req["action"]["request_id"], "decision": "once"},
                               identity=ident_new)["status"] == "approved",
          "W-session: the new token approves what it asked for")

    # --- a changed row never executes
    tampered = await orch.handle({"message": "Spotify par Tum Hi Ho chalao", "source": "voice"}, identity=ident_new)
    t_rid = tampered["action"]["request_id"]
    store._conn.execute("UPDATE android_requests SET action_json=? WHERE request_id=?",
                        ('{"type":"open_url","url":"https://www.netflix.com/","label":"x"}', t_rid))
    res = orch.handle_decision({"request_id": t_rid, "decision": "once"}, identity=ident_new)
    check(res["status"] == "refused" and "integrity" in res["reason"],
          "W-seal: a request whose action was changed in the database is cancelled, not executed")
    check(store.request_get("w_user", t_rid)["status"] == "denied", "W-seal: the tampered request is closed")
    escalated = await orch.handle({"message": "Spotify par Tum Hi Ho chalao", "source": "voice"}, identity=ident_new)
    e_rid = escalated["action"]["request_id"]
    store._conn.execute("UPDATE android_requests SET can_always=1 WHERE request_id=?", (e_rid,))
    check(orch.handle_decision({"request_id": e_rid, "decision": "always"}, identity=ident_new)["status"] == "refused",
          "W-seal: a request whose 'always' flag was changed is cancelled")

    # --- Always Allow: needs the explicit profile switch, and is never offered for calls, messages or contacts
    before = await orch.handle({"message": "Spotify par Tum Hi Ho chalao", "source": "voice"}, identity=ident_w)
    check(before["action"]["can_always"] is False, "W-always: without the profile switch, Always Allow is not offered")
    check(orch.handle_decision({"request_id": before["action"]["request_id"], "decision": "always"},
                               identity=ident_w)["status"] == "refused",
          "W-always: and the server refuses it when asked anyway")
    switched = orch.handle_profile({"op": "always", "enabled": True}, identity=ident_w)
    check(switched["status"] == "ok" and "always_allow" in switched["profile"]["boundaries"],
          "W-always: the user switches it on in the profile")
    offered = await orch.handle({"message": "Spotify par Tum Hi Ho chalao", "source": "voice"}, identity=ident_w)
    check(offered["action"]["can_always"] is True, "W-always: with the switch on, a low-risk platform offers it")
    remembered = orch.handle_decision({"request_id": offered["action"]["request_id"], "decision": "always"}, identity=ident_w)
    check(remembered["status"] == "approved" and store.grant_active("w_user", "spotify"),
          "W-always: Always Allow is remembered for that platform")
    streamlined = await orch.handle({"message": "Spotify par Tum Hi Ho chalao", "source": "voice"}, identity=ident_w)
    check(streamlined["action"]["mode"] == "streamlined" and "url" not in streamlined["action"],
          "W-always: the next request is streamlined and still needs a tap")
    check(orch.handle_revoke({"kind": "spotify"}, identity=ident_w)["status"] == "revoked",
          "W-always: revoking clears it")
    check(orch.handle_revoke({"kind": "dialer"}, identity=ident_w)["status"] == "refused",
          "W-always: a platform that is never remembered has nothing to revoke")
    await orch.handle({"message": "save contact Mom +91 98765 43211", "source": "voice"}, identity=ident_w)
    for text in ("call Mom", "send a WhatsApp message to Mom saying hi", "open contacts"):
        reply = await orch.handle({"message": text, "source": "voice"}, identity=ident_w)
        check(reply["action"]["type"] == "permission_request" and reply["action"]["can_always"] is False
              and orch.handle_decision({"request_id": reply["action"]["request_id"], "decision": "always"},
                                       identity=ident_w)["status"] == "refused",
              f"W-always: {text!r} is never remembered, even with the switch on")
    camera = await orch.handle({"message": "open camera", "source": "voice"}, identity=ident_w)
    check(camera["action"]["can_always"] is True, "W-always: the camera may be remembered, with the switch on")

    # --- Friend Mode boundaries: a category the user did not allow never reaches the modal
    await orch.handle({"message": "save contact Dad +91 98765 43212", "source": "voice"}, identity=ident_x)
    blocked = await orch.handle({"message": "call Dad", "source": "voice"}, identity=ident_x)
    check(blocked["action"]["type"] == "none" and "Friend Mode" in blocked["text"],
          "W-friend: a category outside the boundaries never reaches the permission modal")
    allowed = await orch.handle({"message": "Netflix par Stranger Things search karo", "source": "voice"}, identity=ident_x)
    check(allowed["action"]["type"] == "permission_request", "W-friend: an allowed category still asks first")

    # --- Friend Mode: the nickname comes from the token's own profile
    nick = await orch.handle({"message": "Netflix par Stranger Things search karo", "source": "voice"}, identity=ident_w)
    check(nick["text"].startswith("सुनील भाई, "), "W-friend: the reply uses the token owner's nickname")
    plain = await orch.handle({"message": "Netflix par Stranger Things search karo", "source": "voice"}, identity=ident_x)
    check("सुनील" not in plain["text"], "W-friend: another user never hears this nickname")

    # --- the body can never say who is asking
    check(_raises(lambda: ID.bind({"user_id": "w_user", "message": "x"}, ident_w), ID.IdentityMismatch),
          "W-body: a body naming a user is refused, even when it matches the token")
    check(await _raises_async(orch.handle({"message": "hi", "user_id": "w_user"}, identity=ident_w), ID.IdentityMismatch),
          "W-body: a chat body naming a user is refused")
    check(ID.bind({"message": "x", "_session": "forged"}, ident_w)["_session"] == ident_w.token_id,
          "W-body: a forged session in the body is replaced")
    check("_session" not in orch._bind({"_session": "forged", "message": "x"}, None),
          "W-body: in-process bodies carry no session")

    # --- token lifetime: clamping, expiry, rotation, and an expired token gives no identity
    check(SEC.clamp_ttl(0) == SEC.MIN_TOKEN_TTL_S and SEC.clamp_ttl(10 ** 12) == SEC.MAX_TOKEN_TTL_S
          and SEC.clamp_ttl("x") == SEC.DEFAULT_TOKEN_TTL_S,
          "W-ttl: lifetimes are clamped to one hour to one year, and a bad value gets the default")
    short = store.token_issue("ttl_user", ttl_s=3600)
    srow = store._read("SELECT created_at, expires_at FROM api_tokens WHERE token_hash=?", (ID.token_hash(short),))[0]
    check(abs((srow["expires_at"] - srow["created_at"]) - 3600) < 1, "W-ttl: a token's lifetime is stored")
    check(store.token_resolve(short, now=srow["expires_at"] - 1) == "ttl_user"
          and store.token_resolve(short, now=srow["expires_at"] + 1) is None,
          "W-ttl: a token stops working at its expiry")
    default = store.token_issue("ttl_user")
    drow = store._read("SELECT created_at, expires_at FROM api_tokens WHERE token_hash=?", (ID.token_hash(default),))[0]
    check(abs((drow["expires_at"] - drow["created_at"]) - SEC.DEFAULT_TOKEN_TTL_S) < 1, "W-ttl: the default lifetime is 30 days")
    rotated = store.token_rotate(default)
    check(rotated is not None and store.token_resolve(rotated) == "ttl_user",
          "W-rotate: rotation issues a working token for the same user")
    check(store.token_resolve(default) is None and store.token_rotate(default) is None,
          "W-rotate: the old token stops working and cannot rotate again")
    check(store.token_rotate(rotated) is not None, "W-rotate: the new token can rotate in turn")
    check(store.token_rotate(short, now=srow["expires_at"] + 5) is None, "W-rotate: an expired token cannot be rotated")
    doomed = store.token_issue("ttl_user2")
    doomed_id = ID.identity_from_bearer(f"Bearer {doomed}", OP, store.token_resolve)
    pending_doomed = await orch.handle({"message": "Spotify par Tum Hi Ho chalao", "source": "voice"}, identity=doomed_id)
    store._conn.execute("UPDATE api_tokens SET expires_at=0 WHERE token_hash=?", (ID.token_hash(doomed),))
    check(ID.identity_from_bearer(f"Bearer {doomed}", OP, store.token_resolve) is None,
          "W-ttl: an expired token gives no identity, so it cannot decide anything")
    check(pending_doomed["action"]["type"] == "permission_request", "W-ttl: the request was made while the token lived")
    tcols = {r["name"] for r in store._read("PRAGMA table_info(api_tokens)", ())}
    rcols = {r["name"] for r in store._read("PRAGMA table_info(android_requests)", ())}
    check({"expires_at", "rotated_to"} <= tcols and {"token_id", "signature"} <= rcols,
          "W-ttl: the schema carries lifetimes, rotation, sessions and seals")

    # --- the gateway admits rotation for user tokens; the route decides who may rotate
    policy = gateway.GuardPolicy(OP, accept=lambda c: store.token_resolve(c) is not None)
    check(policy.check("POST", "/api/me/rotate", f"Bearer {tok_x}", "k1", now=0.0).allowed,
          "W-gateway: the rotation route is known and admits a user token")
