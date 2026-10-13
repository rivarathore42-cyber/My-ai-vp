"""Section Y: open access by default (Phase 12).

There is no pairing code and no password. A request without credentials acts for the owner. A request with a credential
is checked: the operator token is administrator, a valid user token is that user, anything else is refused. Memory and
administration are operator-only, and the HTTP layer has no pairing route and no cookie.
"""
from pathlib import Path

from galaxy import auth
from galaxy import gateway
from galaxy import identity as ID

ROOT = Path(__file__).resolve().parents[1]


async def run(check) -> None:
    check(auth.owner_from_env(None) == "owner" and auth.owner_from_env("") == "owner",
          "Y-owner: with no owner id, the owner is 'owner'")
    check(auth.owner_from_env("anonymous") == "owner" and auth.owner_from_env("operator") == "owner",
          "Y-owner: a reserved label can never be the owner id")

    session = auth.open_session("owner")
    check(session.kind == "user" and session.user_id == "owner" and session.token_id == "open" and not ID.is_admin(session),
          "Y-session: the default session is the owner's, and is never an administrator")

    operator = "operator-test-token-" + "x" * 24
    nobody = lambda credential: None
    check(ID.identity_from_bearer("Bearer " + operator, operator, nobody).kind == "operator",
          "Y-bearer: the operator token gives administrator rights")
    check(ID.identity_from_bearer("Bearer not-the-token", operator, nobody) is None,
          "Y-bearer: a wrong credential is refused; it does not fall back to the owner")

    policy = gateway.GuardPolicy(operator, open_access=True, max_body=8 * 1024 * 1024, accept=lambda c: None)
    check(policy.check("GET", "/api/me", None, "203.0.113.5", 0).allowed,
          "Y-open: a request without credentials passes the gate and becomes the owner's session")
    check(policy.check("POST", "/api/upload", None, "203.0.113.5", 9_000_000).status == 413,
          "Y-open: the body limit still applies to open access")
    check(not gateway.GuardPolicy(operator).check("GET", "/api/me", None, "203.0.113.5", 0).allowed,
          "Y-gate: a gate without open access still refuses a request without credentials")

    api_source = (ROOT / "galaxy" / "api.py").read_text(encoding="utf-8")
    check("/api/pair" not in api_source and "/api/unpair" not in api_source and "/api/admin/pairing" not in api_source,
          "Y-removed: the HTTP layer has no pairing route")
    package = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "galaxy").glob("*.py"))
    check("PairingBook" not in package and "session_cookie" not in package and "GALAXY_AUTH" not in package,
          "Y-removed: no pairing book, session cookie or access-mode setting remains in the package")
    check("from __future__" not in api_source,
          "Y-fastapi: the HTTP layer has no postponed annotations, so FastAPI can resolve Request inside build_app")
    check(api_source.count('"operator_only"') == 7,
          "Y-admin: seven administrator routes (memory, tuning, tune, tokens x3, health) each refuse anyone but the operator")
    check('token_id == "open"' in api_source,
          "Y-rotate: the owner's open session cannot rotate a token; a real token is needed")

    page = (ROOT / "galaxy" / "web" / "app.html").read_text(encoding="utf-8")
    script = (ROOT / "galaxy" / "web" / "app.js").read_text(encoding="utf-8")
    check("pair" not in page.lower() and "/api/pair" not in script,
          "Y-page: no pairing screen, field or call on the page")
    check("window.galaxyUser = function () { return ''; };" in script,
          "Y-page: the page never sends an identity; the server decides it")
