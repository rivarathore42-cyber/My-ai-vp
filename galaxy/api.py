"""HTTP layer (Phase 12). Open by default: a request without credentials acts for the owner (see auth.py).

Every /api answer is JSON. Chat replies always carry text, ok and status. Every refusal has the same shape. Inputs are
validated before they reach the engine (validation.py). The page itself is web/app.html.
"""

import asyncio
import base64
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

from . import auth
from . import memory_watch
from . import validation
from . import web_assets
from .agents_system import VisionMetadataAgent
from .core import DEFAULT_DB, ROOT, owner_id
from .gateway import GuardPolicy, resolve_token
from .identity import Identity, IdentityMismatch, credential_of, identity_from_bearer, is_admin, is_user
from .identity import token_id as _token_id
from .memory import MemoryStore
from .orchestrator import Orchestrator
from .security import DAY_S, clamp_ttl

log = logging.getLogger("galaxy.v6.api")
OWNER = auth.owner_from_env(os.environ.get("GALAXY_OWNER_ID"))
MAX_REQUEST_BYTES = 8 * 1024 * 1024   # a 5 MB upload, base64-encoded, plus its JSON wrapper
FRIENDLY = {
    401: "Session nahi mila. Page dobara load karein.",
    403: "Is session ko ye kaam karne ki ijazat nahi hai.",
    404: "Ye route maujood nahi hai.",
    405: "Is route par ye method allowed nahi hai.",
    413: "Data bahut bada hai.",
    429: "Bahut zyada requests aa gayi hain. Thodi der baad try karein.",
}


def configure_logging() -> None:
    logdir = ROOT / "logs"
    logdir.mkdir(exist_ok=True)
    root = logging.getLogger("galaxy")
    root.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        handler = RotatingFileHandler(logdir / "galaxy.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)


def create_service(db_path: Optional[str] = None) -> Orchestrator:
    store = MemoryStore(db_path or DEFAULT_DB)
    store.prune_calls(keep=20_000)
    service = Orchestrator(store)
    service.llm.warm_in_background()   # the model loads in the background; requests never wait for it
    return service


def build_app(service: Optional[Orchestrator] = None):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    configure_logging()
    service = service or create_service()
    operator_token, generated = resolve_token(os.environ.get("GALAXY_API_TOKEN"))
    if generated:
        os.environ["GALAXY_API_TOKEN"] = operator_token
        print("operator token generated for this boot (admin routes only): " + operator_token, file=sys.stderr, flush=True)
    policy = GuardPolicy(operator_token, open_access=True, max_body=MAX_REQUEST_BYTES,
                         accept=lambda credential: service.store.token_resolve(credential) is not None)
    app = FastAPI(title="Galaxy AI Engine v7.0", version="7.0.0")
    upload_lock = asyncio.Lock()   # one file at a time: the memory peak of an upload does not stack up
    rss = memory_watch.rss_mb()
    local_model = "off" if os.environ.get("GALAXY_SKIP_LLM") == "1" else "on"
    print(f"galaxy startup: process memory {rss if rss is not None else 'unknown'} MB, local model {local_model}",
          file=sys.stderr, flush=True)

    def error_response(status: int, text: str, code: Optional[str] = None):
        return JSONResponse({"ok": False, "status": status, "error": code or "refused", "text": text}, status_code=status)

    def who(request: Request) -> Optional[Identity]:
        """The identity for this request. A header, when present, must be valid; no header means the owner's session."""
        header = (request.headers.get("authorization") or "").strip()
        if header:
            return identity_from_bearer(header, operator_token, service.store.token_resolve)
        return auth.open_session(OWNER)

    async def body_of(request: Request):
        try:
            return await request.json()
        except Exception:
            return None

    def run_bound(fn, ident: Identity, body):
        try:
            return JSONResponse(fn(body, identity=ident))
        except IdentityMismatch as exc:
            return error_response(403, str(exc), "identity_mismatch")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        client = request.client.host if request.client else "unknown"
        decision = policy.check(request.method, path, request.headers.get("authorization"), client, length)
        if not decision.allowed:
            return error_response(decision.status, FRIENDLY.get(decision.status, "Request refused."))
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse((web_assets.WEB_DIR / "app.html").read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health_check():
        """Liveness for the platform (Render's healthCheckPath). Public and cheap; it reveals nothing about the system."""
        return JSONResponse({"ok": True, "status": "alive", "version": "7.0.0"})

    @app.get("/static/voice/{name:path}")
    async def static_asset(name: str):
        path = web_assets.resolve_asset(name)
        if path is None:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path, media_type=web_assets.content_type(path), headers={"Cache-Control": "no-cache"})

    @app.get("/api/me")
    async def api_me(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        return JSONResponse({"ok": True, "status": 200, "mode": "open", "kind": ident.kind,
                             "user_id": ident.user_id, "text": ""})

    @app.get("/api/health")
    async def api_health(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403], "operator_only")
        payload = service.health()
        payload["process_mb"] = memory_watch.rss_mb()
        return JSONResponse(payload)

    @app.post("/api/chat")
    async def api_chat(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        cleaned, error = validation.check_chat_body(await body_of(request))
        if error:
            return error_response(400, error, "bad_request")
        try:
            payload = await service.handle(cleaned, identity=ident)
        except IdentityMismatch as exc:
            return error_response(403, str(exc), "identity_mismatch")
        except Exception:
            log.exception("chat request failed")
            return error_response(500, "Internal error - kuch execute nahi hua. Dobara try karein.", "internal")
        return JSONResponse(payload)

    @app.post("/api/upload")
    async def api_upload(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        body = await body_of(request)
        if not isinstance(body, dict):
            return error_response(400, "upload body must be a JSON object", "bad_request")
        upload, error = validation.check_upload(body.get("filename"), body.get("mime"), body.get("data"))
        if error:
            return error_response(400, error, "bad_upload")
        if upload_lock.locked():
            return error_response(429, "Ek file abhi check ho rahi hai. Thodi der baad bhejein.", "busy")
        async with upload_lock:
            encoded = base64.b64encode(upload["bytes"]).decode("ascii")
            try:
                result = await VisionMetadataAgent().process(upload["filename"], {"image_base64": encoded})
            except Exception:
                log.exception("upload inspection failed")
                return error_response(500, "File check nahi ho paya. Dobara try karein.", "internal")
        text = result.text if result.ok and result.text else (result.error or "File check nahi ho paya.")
        return JSONResponse({"ok": bool(result.ok), "status": 200, "filename": upload["filename"],
                             "mime": upload["mime"], "text": text})

    @app.get("/api/memory/{user_id}")
    async def api_memory(user_id: str, request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403], "operator_only")
        uid = owner_id(user_id)
        if uid is None:
            return error_response(404, "Ye user maujood nahi hai.", "no_such_user")
        return JSONResponse({"ok": True, "status": 200, "user_id": uid,
                             "attributes": service.store.all_attrs(uid), "text": ""})

    @app.get("/api/tuning")
    async def api_tuning(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403], "operator_only")
        return JSONResponse({"ok": True, "status": 200, "history": service.store.tuning_history(20), "text": ""})

    @app.post("/api/evolution/tune")
    async def api_tune(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403], "operator_only")
        return JSONResponse(service.evolution.tune())

    @app.get("/api/admin/tokens")
    async def api_list_tokens(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403], "operator_only")
        return JSONResponse({"ok": True, "status": 200, "tokens": service.store.token_list(), "text": ""})

    @app.post("/api/admin/tokens")
    async def api_issue_token(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403], "operator_only")
        body = await body_of(request)
        uid = owner_id(body.get("user_id")) if isinstance(body, dict) else None
        if uid is None:
            return error_response(400, "user_id required", "bad_request")
        ttl = None
        if isinstance(body, dict) and body.get("ttl_days") is not None:
            try:
                ttl = clamp_ttl(int(body["ttl_days"]) * DAY_S)
            except (TypeError, ValueError):
                return error_response(400, "ttl_days must be a number", "bad_request")
        issued = service.store.token_issue(uid, ttl_s=ttl)
        return JSONResponse({"ok": True, "status": 200, "user_id": uid, "token": issued, "token_id": _token_id(issued),
                             "text": "Token sirf ek baar dikhaya gaya hai. Abhi save karein."})

    @app.post("/api/admin/tokens/revoke")
    async def api_revoke_token(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403], "operator_only")
        body = await body_of(request)
        key = body.get("token_id") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key:
            return error_response(400, "token_id required", "bad_request")
        return JSONResponse({"ok": True, "status": 200, "revoked": service.store.token_revoke(key), "text": ""})

    @app.post("/api/me/rotate")
    async def api_rotate_own_token(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        header = (request.headers.get("authorization") or "").strip()
        if not header or not is_user(ident) or ident.token_id == "open":
            return error_response(403, "Rotation ke liye apna token bhejein.", "needs_token")
        fresh = service.store.token_rotate(credential_of(header))
        if fresh is None:
            return error_response(401, FRIENDLY[401])
        return JSONResponse({"ok": True, "status": 200, "token": fresh, "token_id": _token_id(fresh),
                             "text": "Token rotate ho gaya. Purana token band ho gaya hai. Ye sirf ek baar dikhaya gaya hai."})

    @app.post("/api/android/decision")
    async def api_android_decision(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        return run_bound(service.handle_decision, ident, await body_of(request))

    @app.post("/api/android/revoke")
    async def api_android_revoke(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        return run_bound(service.handle_revoke, ident, await body_of(request))

    @app.post("/api/android/trace")
    async def api_android_trace(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        return run_bound(service.handle_trace, ident, await body_of(request))

    @app.post("/api/friend/profile")
    async def api_friend_profile(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        return run_bound(service.handle_profile, ident, await body_of(request))

    return app
