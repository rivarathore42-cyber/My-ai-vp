"""Android intents for Galaxy (Phase 8): the URL rules and the intent builders.

Builders return IntentSpec values and launch nothing. The orchestrator keeps the executable action with the request and
returns it only after the user's decision in the permission modal.

Rules (each has a test in tests/platform_security.py):
- A link reaches a platform only as an allowlisted pair: its host with its own Android package, or its own package as an
  app launch. A Spotify host with a Netflix package is refused: that is cross-app execution.
- An https fallback must name the same host as the app link.
- User text is cleaned of control characters, length-limited and percent-encoded before it reaches a URL.
- Calls and message sending are high risk and can never be remembered by Always Allow.
- Playback controls (next, previous, pause, resume, seek) are not reachable from a web link. They open the platform with a
  clear hint, and the reply says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional
from urllib.parse import quote, unquote, urlsplit

from . import platforms as P
from .platforms import PLATFORMS, SETTINGS_ACTIONS as SETTINGS

MAX_TEXT = 500
MAX_QUERY = 120
SAFE_HOSTS = P.hosts()
SAFE_PACKAGES = P.packages()
SAFE_PAIRS = P.pairs()
CATEGORY = {key: p.boundary for key, p in PLATFORMS.items()}
CATEGORY.update({"call": "calls", "notify": "notifications"})
ACTION_TYPES = frozenset({"open_url", "dial", "camera", "notify", "settings"})
RISK_LEVELS = ("low", "high")
CONTROL_HINTS = {"next": "Spotify me 'Next' dabayein.", "previous": "'Previous' dabayein.",
                 "pause": "Play/Pause dabayein.", "resume": "Play dabayein."}
CONTROL_LABELS = {"next": "Next track", "previous": "Previous track", "pause": "Pause", "resume": "Play"}

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_HTTPS = re.compile(r"https://([a-z0-9.-]+)(/[^\s]*)?")
_INTENT = re.compile(r"intent://([a-z0-9.-]+)(/[^#\s;]*)#Intent;scheme=https;package=([a-z0-9.]+);"
                     r"S\.browser_fallback_url=([^;\s]+);end")
_APP = re.compile(r"intent:#Intent;package=([a-z0-9.]+);end")
_SETTINGS_URL = re.compile(r"intent:#Intent;action=(android\.settings\.[A-Z_]+);end")
_TEL = re.compile(r"tel:\+?\d{8,15}")


@dataclass(frozen=True)
class IntentSpec:
    kind: str                 # a platform key, or "call", "camera", "settings", "notify", "whatsapp"
    title: str
    why: str
    risk: str                 # "low" or "high"
    destination: str          # shown to the user: the host, or the device feature
    action: dict              # the executable action; returned only after a decision
    preview: str = ""
    standing_ok: bool = False
    reply: str = ""           # what Galaxy says when this intent is requested
    platform: str = ""        # the platform's display name, for the modal badge
    verb: str = ""            # the exact action, for the modal: Open, Search, Play, Seek 45 s forward...


def clean_text(value, limit: int) -> str:
    text = " ".join(_CONTROL_CHARS.sub(" ", str(value or "")).split())
    return text[:limit].rstrip()


def _enc(value: str) -> str:
    return quote(value, safe="")


def _query(value, limit: int = MAX_QUERY) -> str:
    text = clean_text(value, limit)
    if not text:
        raise ValueError("empty text")
    return text


def _digits(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not 8 <= len(digits) <= 15:
        raise ValueError("phone number is not valid")
    return digits


def _phone(value) -> str:
    raw = str(value or "").strip()
    if not re.fullmatch(r"\+?\d{8,15}", raw):
        raise ValueError("phone number is not valid")
    return raw


def safe_url(url) -> bool:
    """True only for the link shapes Galaxy may produce: a platform host over https, tel: digits, a platform's package
    intent (host and package as a pair, with a fallback on the same host), an app launch by package, or a fixed settings screen."""
    if not isinstance(url, str) or len(url) > 2000:
        return False
    if _TEL.fullmatch(url):
        return True
    match = _HTTPS.fullmatch(url)
    if match:
        return match.group(1) in SAFE_HOSTS
    match = _INTENT.fullmatch(url)
    if match:
        host, package = match.group(1), match.group(3)
        fallback = _HTTPS.fullmatch(unquote(match.group(4)))
        return ((host, package) in SAFE_PAIRS and fallback is not None and fallback.group(1) == host)
    match = _APP.fullmatch(url)
    if match:
        return match.group(1) in SAFE_PACKAGES
    match = _SETTINGS_URL.fullmatch(url)
    return bool(match) and match.group(1) in SETTINGS.values()


def package_intent(host: str, path: str, package: str, fallback: str) -> str:
    """Chrome intent URL for a platform. The path must already be encoded; the fallback is encoded here."""
    return f"intent://{host}{path}#Intent;scheme=https;package={package};S.browser_fallback_url={_enc(fallback)};end"


def validate(spec: IntentSpec) -> IntentSpec:
    action = spec.action or {}
    if spec.kind not in CATEGORY:
        raise ValueError(f"unknown kind {spec.kind!r}")
    if spec.risk not in RISK_LEVELS:
        raise ValueError("risk must be low or high")
    if spec.risk == "high" and spec.standing_ok:
        raise ValueError("high-risk actions cannot be remembered")
    if not (0 < len(spec.title) <= 160) or not (0 < len(spec.why) <= 400):
        raise ValueError("title or reason out of range")
    if len(spec.preview) > MAX_TEXT or len(spec.reply) > 400:
        raise ValueError("preview or reply too long")
    kind = action.get("type")
    if kind not in ACTION_TYPES:
        raise ValueError(f"unknown action type {kind!r}")
    if kind in ("open_url", "dial", "settings"):
        url = action.get("url", "")
        if not safe_url(url):
            raise ValueError("URL is not on the allowlist")
        if kind == "dial" and not url.startswith("tel:"):
            raise ValueError("dial needs a tel: URL")
    return spec


def _spec(**fields) -> IntentSpec:
    return validate(IntentSpec(**fields))


def _platform(key: str):
    if key not in PLATFORMS:
        raise ValueError(f"unknown platform {key!r}")
    return PLATFORMS[key]


def _destination(p) -> str:
    return p.host or f"{p.label} on this device"


def _launch(p) -> str:
    """The URL that opens the platform's start page: through its own package when it has a host, else an app launch."""
    if p.host and p.package and p.root:
        parts = urlsplit(p.root)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return package_intent(p.host, path, p.package, p.root)
    if p.package:
        return f"intent:#Intent;package={p.package};end"
    raise ValueError(f"{p.label} has no launch route")


def platform_open(key: str) -> IntentSpec:
    p = _platform(key)
    return _spec(kind=key, title=f"{p.label} kholna", why=f"Galaxy AI needs access to {p.label} to open it for you.",
                 risk="low", destination=_destination(p), action={"type": "open_url", "url": _launch(p), "label": p.label},
                 platform=p.label, verb="Open", standing_ok=p.always_ok,
                 reply=f"{p.label} khol raha hoon. Aapki permission ke baad hi khulega.")


def platform_search(key: str, query: str, verb: str = "Search") -> IntentSpec:
    """Opens the platform's own search for the query. Playing a result is the user's tap; Galaxy cannot pick one."""
    p = _platform(key)
    q = _query(query)
    url = P.search_url(key, q)
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if verb == "Play":
        title = f"{p.label} par '{q}' play karna"
        why = f"Galaxy AI can open {p.label}'s search for '{q}'. {p.label} starts the item itself; Galaxy cannot press play in the app."
        reply = f"{p.label} par '{q}' khol raha hoon. Pehla result aap tap karein, wahi chalega. Aapki permission ke baad hi khulega."
    else:
        title = f"{p.label} par '{q}' search"
        why = f"Galaxy AI needs access to {p.label} to show results for '{q}'."
        reply = f"{p.label} par '{q}' ki search khol raha hoon. Aapki permission ke baad hi khulega."
    return _spec(kind=key, title=title, why=why, risk="low", destination=_destination(p),
                 action={"type": "open_url", "url": package_intent(p.host, path, p.package, url), "label": f"{p.label}: {q}"},
                 preview=q, platform=p.label, verb=verb, standing_ok=p.always_ok, reply=reply)


def platform_control(key: str, control: str, seconds: Optional[int] = None, direction: Optional[str] = None) -> IntentSpec:
    """A playback control. Web links cannot press it, so Galaxy opens the platform with a clear hint instead of failing."""
    p = _platform(key)
    if p.category not in ("audio", "video"):
        raise ValueError(f"{p.label} has no playback controls")
    if control == "seek":
        if not isinstance(seconds, int) or not 1 <= seconds <= 3600:
            raise ValueError("seek must be between 1 and 3600 seconds")
        if direction not in ("forward", "backward"):
            raise ValueError("seek direction must be forward or backward")
        word = "aage" if direction == "forward" else "peeche"
        label = f"Seek {seconds} s {direction}"
        hint = f"{p.label} me seek bar par {seconds} second {word} karein."
    elif control in CONTROL_HINTS:
        label = CONTROL_LABELS[control]
        hint = CONTROL_HINTS[control].replace("Spotify", p.label)
    else:
        raise ValueError(f"unknown control {control!r}")
    return _spec(kind=key, title=f"{p.label} kholna ({label})",
                 why=("Galaxy AI cannot press the platform's own controls from the web. That needs the platform's app SDK "
                      "or a native Android app. It can open the app so you can do it there."),
                 risk="low", destination=_destination(p), action={"type": "open_url", "url": _launch(p), "label": p.label},
                 preview=hint, platform=p.label, verb=label, standing_ok=p.always_ok,
                 reply=f"Ye control web se {p.label} ke andar nahi hota, isliye main {p.label} khol raha hoon. "
                       f"{hint} Aapki permission ke baad hi khulega.")


def youtube_open() -> IntentSpec:
    return platform_open("youtube")


def youtube_search(query: str) -> IntentSpec:
    return platform_search("youtube", query)


def spotify_open(library: bool = False) -> IntentSpec:
    spec = platform_open("spotify")
    if library:
        spec = replace(spec, preview="Galaxy AI cannot pick a track from your Spotify library; playback stays inside Spotify.")
    return spec


def spotify_search(query: str) -> IntentSpec:
    return platform_search("spotify", query)


def spotify_play_song(query: str) -> IntentSpec:
    return platform_search("spotify", query, verb="Play")


def media_next() -> IntentSpec:
    return platform_control("spotify", "next")


def media_previous() -> IntentSpec:
    return platform_control("spotify", "previous")


def media_pause() -> IntentSpec:
    return platform_control("spotify", "pause")


def media_resume() -> IntentSpec:
    return platform_control("spotify", "resume")


def media_seek(seconds: int, direction: str) -> IntentSpec:
    return platform_control("spotify", "seek", seconds, direction)


def whatsapp_open() -> IntentSpec:
    return platform_open("whatsapp")


def whatsapp_chat(phone: str, name: str) -> IntentSpec:
    digits = _digits(phone)
    who = clean_text(name, 64) or "contact"
    return _spec(kind="whatsapp", title=f"{who} ka WhatsApp chat kholna",
                 why=f"Galaxy AI needs WhatsApp to open the chat with {who}. You decide what to send there.",
                 risk="high", destination="WhatsApp (wa.me)", action={"type": "open_url", "url": f"https://wa.me/{digits}",
                                                                      "label": f"WhatsApp: {who}"},
                 platform="WhatsApp", verb="Open chat",
                 reply=f"{who} ka WhatsApp chat khol raha hoon. Aapki permission ke baad hi khulega.")


def whatsapp_message(phone: str, name: str, text: str) -> IntentSpec:
    digits = _digits(phone)
    who = clean_text(name, 64) or "contact"
    body = _query(text, MAX_TEXT)
    return _spec(kind="whatsapp", title=f"{who} ko WhatsApp message tayyar karna",
                 why=(f"Galaxy AI needs WhatsApp to prepare a message to {who}. It is only pre-filled: "
                      "nothing is sent until you press Send in WhatsApp."),
                 risk="high", destination="WhatsApp (wa.me)",
                 action={"type": "open_url", "url": f"https://wa.me/{digits}?text={_enc(body)}", "label": f"WhatsApp: {who}"},
                 preview=body, platform="WhatsApp", verb="Message",
                 reply=f"{who} ke liye WhatsApp message tayyar kar raha hoon. Aapki permission ke baad hi khulega, aur bhejna aapke haath me hai.")


def call(phone: str, name: str) -> IntentSpec:
    number = _phone(phone)
    who = clean_text(name, 64) or "contact"
    return _spec(kind="call", title=f"{who} ko call karna",
                 why=f"Galaxy AI needs the phone dialer to call {who}. The call starts only when you press the call button.",
                 risk="high", destination="Phone dialer", action={"type": "dial", "url": f"tel:{number}", "label": f"Call: {who}"},
                 platform="Phone", verb="Call", reply=f"{who} ko call ke liye dialer khol raha hoon. Aapki permission ke baad hi khulega.")


def camera() -> IntentSpec:
    return _spec(kind="camera", title="Camera kholna", why="Galaxy AI needs the camera to take a photo when you ask.",
                 risk="low", destination="Camera on this device", action={"type": "camera", "label": "Camera"},
                 platform="Camera", verb="Open camera", standing_ok=True,
                 reply="Camera khol raha hoon. Aapki permission ke baad hi khulega.")


def settings(section: str) -> IntentSpec:
    if section not in SETTINGS:
        raise ValueError(f"settings screen {section!r} is not on the list")
    return _spec(kind="settings", title=f"{section} settings kholna",
                 why="Galaxy AI needs to open this Android settings screen for you.", risk="low",
                 destination=f"Android settings: {section}",
                 action={"type": "settings", "url": f"intent:#Intent;action={SETTINGS[section]};end", "label": f"{section} settings"},
                 platform="Settings", verb="Open", standing_ok=True,
                 reply=f"{section} settings khol raha hoon. Aapki permission ke baad hi khulega.")


def notify(text: str) -> IntentSpec:
    body = clean_text(text, 140)
    if not body:
        raise ValueError("empty notification")
    return _spec(kind="notify", title="Notification bhejna",
                 why="Galaxy AI needs permission to show a notification. Your browser may also ask once.", risk="low",
                 destination="Notifications on this device", action={"type": "notify", "title": "Galaxy AI", "body": body},
                 preview=body, platform="Notifications", verb="Notify", standing_ok=True,
                 reply="Notification bhejne ki permission chahiye. Aapki permission ke baad hi bhejunga.")


ANDROID_INTENT_SCHEMA = {
    "version": 2,
    "kinds": {kind: {"category": category} for kind, category in CATEGORY.items()},
    "action_types": sorted(ACTION_TYPES),
    "risk_levels": list(RISK_LEVELS),
    "required_fields": ["kind", "title", "why", "risk", "destination", "action"],
    "url_rules": ("https to a platform host; intent:// only for a platform's own (host, package) pair with a fallback on the "
                  "same host; app launch by a registered package; tel: with digits only; settings from a fixed list"),
}
