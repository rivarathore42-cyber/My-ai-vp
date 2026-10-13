"""Section V: token-to-identity binding (Phase 7, hardened in Phase 8), and media control with Friend Mode context."""
import tempfile
from pathlib import Path

from galaxy import android_intents as AI
from galaxy import friend_mode as FR
from galaxy import gateway
from galaxy import identity as ID
from galaxy import media_controller as MC
from galaxy import voice_router as VR
from galaxy.memory import MemoryStore
from galaxy.orchestrator import Orchestrator

OP = "operator-test-secret-0123456789abcdef"


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


def _subject(route) -> str:
    return route.query if route.name in MC.MEDIA_KEYS else (route.name or route.query)


async def run(check) -> None:
    path = Path(tempfile.mkdtemp(prefix="identity_v_")) / "v.db"
    store = MemoryStore(path)
    orch = Orchestrator(store)
    tok_a, tok_b, tok_c, tok_d = (store.token_issue(u) for u in ("user_a", "user_b", "user_c", "user_d"))
    ident_a = ID.identity_from_bearer(f"Bearer {tok_a}", OP, store.token_resolve)
    ident_b = ID.identity_from_bearer(f"Bearer {tok_b}", OP, store.token_resolve)
    ident_c = ID.identity_from_bearer(f"Bearer {tok_c}", OP, store.token_resolve)
    ident_d = ID.identity_from_bearer(f"Bearer {tok_d}", OP, store.token_resolve)
    op = ID.identity_from_bearer(f"Bearer {OP}", OP, store.token_resolve)

    # --- identity comes from the token
    check(ident_a is not None and ident_a.kind == "user" and ident_a.user_id == "user_a",
          "V-identity: a user token resolves to its own user")
    check(ID.identity_from_bearer(f"bearer {tok_b}", OP, store.token_resolve).user_id == "user_b",
          "V-identity: the scheme is case-insensitive and the identity is still the token's user")
    check(op is not None and op.kind == "operator" and op.user_id is None,
          "V-identity: the operator token is an administrator with no user")
    check(ID.identity_from_bearer("Bearer not-a-token", OP, store.token_resolve) is None,
          "V-identity: an unknown token resolves to nothing")
    check(ID.identity_from_bearer(None, OP, store.token_resolve) is None
          and ID.identity_from_bearer("Basic abc", OP, store.token_resolve) is None,
          "V-identity: a missing or malformed credential resolves to nothing")
    check(ID.identity_from_bearer(f"Bearer {tok_a}", "", store.token_resolve).user_id == "user_a",
          "V-identity: user tokens do not depend on an operator token")
    check(ID.identity_from_bearer(f"Bearer {tok_a}", OP, lambda _t: None) is None,
          "V-identity: a token the store does not resolve is refused")

    # --- storage keeps no plaintext tokens
    check(tok_a.encode() not in path.read_bytes() and tok_b.encode() not in path.read_bytes(),
          "V-storage: the database holds no plaintext token")
    check(all("token_hash" not in row and "user_id" in row for row in store.token_list()),
          "V-storage: the token listing shows ids, never hashes or tokens")
    check(_raises(lambda: store.token_issue("anonymous")), "V-storage: a token cannot be issued to no user")

    # --- the body can never say who is asking (Phase 8: any user id in a body is refused)
    check(ID.bind({"message": "hi"}, ident_a)["user_id"] == "user_a",
          "V-bind: the token's user is written into the bound body")
    check(_raises(lambda: ID.bind({"message": "hi", "user_id": "user_a"}, ident_a), ID.IdentityMismatch),
          "V-bind: a body naming a user is refused, even the same user")
    check(_raises(lambda: ID.bind({"message": "hi", "user_id": "anonymous"}, ident_a), ID.IdentityMismatch),
          "V-bind: a body with any user id is refused")
    check(_raises(lambda: ID.bind({"user_id": "user_b"}, ident_a), ID.IdentityMismatch),
          "V-bind: a body naming another user is refused")
    check(_raises(lambda: ID.bind({"user_id": "user_a"}, op), ID.IdentityMismatch),
          "V-bind: the operator token cannot act for a user")
    check("user_id" not in ID.bind({"message": "hi", "user_id": ""}, op),
          "V-bind: the operator acts anonymously")
    check(ID.bind({"message": "hi", "_session": "forged"}, ident_a)["_session"] == ident_a.token_id,
          "V-bind: a forged session in the body is replaced by the token's session")
    check(ID.is_admin(op) and not ID.is_admin(ident_a) and not ID.is_admin(None),
          "V-admin: only the operator token is an administrator")

    # --- the Android decision is claimed only by the owner's token
    await orch.handle({"message": "save contact Hansraj +91 98765 43210", "source": "voice"}, identity=ident_a)
    call = await orch.handle({"message": "Hey Galaxy, call Hansraj", "source": "voice"}, identity=ident_a)
    rid = call["action"]["request_id"]
    check(call["action"]["type"] == "permission_request", "V-decision: the voice call still asks first")
    check(orch.handle_decision({"request_id": rid, "decision": "once"}, identity=ident_b)["status"] == "refused",
          "V-decision: another user's token cannot claim the request")
    check(orch.handle_decision({"request_id": rid, "decision": "once"}, identity=op)["status"] == "refused",
          "V-decision: the operator token cannot claim a user's request")
    check(_raises(lambda: orch.handle_decision({"user_id": "user_a", "request_id": rid, "decision": "once"},
                                               identity=ident_b), ID.IdentityMismatch),
          "V-decision: a body naming user A, sent with B's token, is refused")
    check(orch.handle_decision({"request_id": "a" * 32, "decision": "once"}, identity=ident_a)["status"] == "refused",
          "V-decision: a forged request id is refused")
    done = orch.handle_decision({"request_id": rid, "decision": "once"}, identity=ident_a)
    check(done["status"] == "approved" and done["action"]["type"] == "dial",
          "V-decision: the owner's token claims the request")
    check(orch.handle_decision({"request_id": rid, "decision": "once"}, identity=ident_a)["status"] == "refused",
          "V-decision: the owner cannot claim it twice")

    # --- chat binding
    check(await _raises_async(orch.handle({"message": "what is 2 plus 2", "user_id": "user_b"}, identity=ident_a),
                              ID.IdentityMismatch),
          "V-chat: a chat body naming another user is refused")
    chat = await orch.handle({"message": "what is 2 plus 2"}, identity=ident_a)
    check("4" in chat["text"], "V-chat: the owner's own chat still works")
    anon = await orch.handle({"message": "what is 2 plus 2"}, identity=op)
    check("4" in anon["text"], "V-chat: the operator can chat anonymously")

    # --- Friend Mode profiles are per token
    store.profile_save("user_a", FR.build_profile({"nickname": "सुनील भाई", "tone": "warm",
                                                   "boundaries": ["allow:media", "allow:calls"], "confirm": True}))
    store.profile_save("user_b", FR.build_profile({"nickname": "भैया", "tone": "casual",
                                                   "boundaries": ["allow:media"], "confirm": True}))
    store.profile_save("user_d", FR.build_profile({"nickname": "सुनील भाई", "tone": "formal",
                                                   "boundaries": ["allow:media"], "confirm": True}))
    pa = orch.handle_profile({"op": "get"}, identity=ident_a)
    pb = orch.handle_profile({"op": "get"}, identity=ident_b)
    check(pa["profile"]["nickname"] == "सुनील भाई" and pb["profile"]["nickname"] == "भैया",
          "V-friend: each token reads only its own profile")
    check(_raises(lambda: orch.handle_profile({"op": "get", "user_id": "user_a"}, identity=ident_b), ID.IdentityMismatch),
          "V-friend: a profile request naming another user is refused")
    check(orch.handle_profile({"op": "get"}, identity=op).get("profile") is None,
          "V-friend: the operator has no personal profile")

    # --- the gateway: every route is known, and user tokens are admitted (the route then decides)
    policy = gateway.GuardPolicy(OP, accept=lambda credential: store.token_resolve(credential) is not None)
    check(policy.check("POST", "/api/chat", f"Bearer {tok_a}", "c1", now=0.0).allowed,
          "V-guard: a user token passes the gateway")
    check(policy.check("POST", "/api/chat", "Bearer not-a-token", "c2", now=0.0).status == 401,
          "V-guard: an unknown token is refused")
    check(policy.check("POST", "/api/android/decision", f"Bearer {OP}", "c3", now=0.0).allowed,
          "V-guard: the Android routes are known to the gateway")
    check(policy.check("GET", "/api/me", f"Bearer {tok_a}", "c4", now=0.0).allowed,
          "V-guard: the identity route is known to the gateway")
    check(policy.check("POST", "/api/android/decision", None, "c5", now=0.0).status == 401,
          "V-guard: an unauthenticated decision never reaches a route")

    # --- media commands: the app is named, so the command is never guessed
    cases = [
        ("Spotify par change track", "spotify_next", ""), ("Spotify par next song", "spotify_next", ""),
        ("Spotify par अगला गाना", "spotify_next", ""), ("YouTube Music par previous song", "youtube_music_previous", ""),
        ("Spotify par pichla gaana", "spotify_previous", ""), ("Netflix par pause karo", "netflix_pause", ""),
        ("Spotify par gaana roko", "spotify_pause", ""), ("Hotstar par play karo", "hotstar_resume", ""),
        ("Spotify par play music", "spotify_resume", ""),
        ("Spotify par seek 45 seconds forward", "spotify_seek", "45:forward"),
        ("Spotify par seek back 10 seconds", "spotify_seek", "10:backward"),
        ("Spotify par 45 second aage karo", "spotify_seek", "45:forward"),
        ("Spotify par seek forward 2 minutes", "spotify_seek", "120:forward"),
        ("Spotify par seek 30 seconds back", "spotify_seek", "30:backward"),
        ("Spotify par Tum Hi Ho gaana chalao", "spotify_play", "Tum Hi Ho"),
        ("play song Tum Hi Ho", "platform_needed", ""), ("Tum Hi Ho gaana chalao", "platform_needed", ""),
        ("change track", "platform_needed", ""), ("seek 45 seconds forward", "platform_needed", ""),
        ("45 second aage seek karo", "platform_needed", ""),
        ("Open my Spotify library and play the first song", "spotify_open", "library"),
        ("Spotify par lofi mix search karo", "spotify_search", "lofi mix"),
        ("what is the next song in the list", "reasoning", ""), ("the next step in math", "reasoning", ""),
    ]
    for text, kind, want in cases:
        route = VR.classify(text)
        check(route.kind == kind and (not want or _subject(route) == want), f"V-media: {text!r} -> {kind}")
    specs = [AI.media_next(), AI.media_previous(), AI.media_pause(), AI.media_resume(), AI.media_seek(45, "forward"),
             AI.spotify_play_song("Tum Hi Ho")]
    check(all(AI.safe_url(s.action["url"]) and s.reply and s.standing_ok for s in specs),
          "V-media: every media control opens its platform safely and explains itself")
    check(_raises(lambda: AI.media_seek(0, "forward")) and _raises(lambda: AI.media_seek(99999, "forward"))
          and _raises(lambda: AI.media_seek(10, "sideways")),
          "V-media: seek bounds and direction are enforced")

    seek = await orch.handle({"message": "Spotify par seek 45 seconds forward", "source": "voice"}, identity=ident_a)
    check(seek["action"]["type"] == "permission_request" and seek["action"]["kind"] == "spotify",
          "V-media: a seek asks for permission and targets Spotify")
    check("45" in seek["text"] and "Spotify" in seek["text"],
          "V-media: the reply names the limit and the way to finish the action")
    ok = orch.handle_decision({"request_id": seek["action"]["request_id"], "decision": "once"}, identity=ident_a)
    check(ok["status"] == "approved" and ok["action"]["url"].startswith("intent://open.spotify.com/")
          and AI.safe_url(ok["action"]["url"]),
          "V-media: the approved fallback opens Spotify without error")
    unnamed = await orch.handle({"message": "45 seconds aage badhao", "source": "voice"}, identity=ident_a)
    check(unnamed["action"]["type"] == "none" and "Kis app par" in unnamed["text"],
          "V-media: a seek with no app asks which app, and opens nothing")

    # --- Friend Mode context: the reply uses each user's own nickname, in their own tone
    nxt = await orch.handle({"message": "Spotify par change track", "source": "voice"}, identity=ident_a)
    check(nxt["text"].startswith("सुनील भाई, Ye control"), "V-friend: the reply uses the user's saved nickname")
    other = await orch.handle({"message": "Spotify par change track", "source": "voice"}, identity=ident_b)
    check(other["text"].startswith("भैया, Ye control") and "सुनील" not in other["text"],
          "V-friend: another user's reply uses their own nickname only")
    formal = await orch.handle({"message": "Spotify par change track", "source": "voice"}, identity=ident_d)
    check(formal["text"].startswith("सुनील भाई जी, Ye control"), "V-friend: a formal tone uses the respectful form")
    nameless = await orch.handle({"message": "Spotify par change track", "source": "voice"}, identity=ident_c)
    check(nameless["text"].startswith("Ye control"), "V-friend: without a profile no name is invented")

    # --- revocation takes effect at the identity layer
    check(store.token_revoke(ID.token_id(tok_b))
          and ID.identity_from_bearer(f"Bearer {tok_b}", OP, store.token_resolve) is None,
          "V-revoke: a revoked token resolves to nothing")
