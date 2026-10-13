"""Voice task router for 'Hey Galaxy' (Phases 5 to 8).

A transcribed command is classified into one route. Android routes become permission requests in the orchestrator: the
server never opens a program, runs a shell command or dials. English and Hindi (Devanagari) spellings are recognised; the
lists are short and stated, not exhaustive. Word boundaries use whitespace or string ends, because Devanagari vowel signs
are not word characters for \\b.

Media and platform commands (audio and video apps) are parsed by media_controller; this module adds the system routes.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from . import media_controller as MC

WAKE_RE = re.compile(r"^\s*(?:hey|ok|okay|हे|ओके)\s+(?:galaxy|galaxie|galaxi|गैलेक्सी|गैलक्सी|गेलेक्सी)(?=\s|[,.!?:-]|$)[\s,.!?:-]*",
                     re.I)
WHATSAPP_RE = re.compile(r"(?:whatsapp|whats app|व्हाट्सएप|व्हाट्सऐप|वाट्सएप|वाट्सऐप)", re.I)
CAMERA_RE = re.compile(r"^\s*(?:(?:open|khol\w*|start)\s+(?:the\s+|my\s+)?(?:camera|कैमरा)"
                       r"|(?:camera|कैमरा)\s+(?:open|khol\w*|खोल[\u0900-\u097F]*)"
                       r"|take\s+a\s+photo|photo\s+(?:lo|kheencho)|फोटो\s+(?:लो|खींचो))(?=\s|$|[.!?])", re.I)
SETTINGS_RE = re.compile(r"(?<![A-Za-z])(wifi|wi-fi|bluetooth|sound|display)(?![A-Za-z])\s*(?:settings?|सेटिंग[\u0900-\u097F]*)",
                         re.I)
OPEN_RE = re.compile(r"open|khol|खोल", re.I)
APP_OPEN_RE = re.compile(r"^\s*(?:open|khol\w*|खोल\S*)\s+(?:my\s+|mera\s+|meri\s+)?"
                         r"(?P<app>contacts?|कॉन्टैक्ट्स?|dialer|dialpad|phone\s+app)\s*[.!?]?\s*$", re.I)
TECH_RE = re.compile(r"\b(?:function|functions|api|code|python|script|method|class|variable|database|sql|json|bug|error|algorithm)\b",
                     re.I)
_NOTIFY = re.compile(r"^\s*(?:send\s+(?:me\s+)?(?:a\s+)?notification|notify\s+me|show\s+(?:a\s+)?notification"
                     r"|notification\s+(?:bhejo|dikhao))\s*(?:saying|that|about|:)?\s*(?P<text>.+)$", re.I)
_MSG_EN = re.compile(r"^\s*(?:send\s+)?(?:a\s+)?(?:message|msg)\s+(?:to\s+)?(?P<name>.+?)\s+(?:saying|that|:)\s*(?P<text>.+)$",
                     re.I)
_MSG_HI = re.compile(r"^(?:(?:पर|par|pe)\s+)?(?P<name>.+?)\s*(?:को|ko)\s+(?:message|msg|मैसेज|मेसेज)\s*(?:karo|करो|bhejo|भेजो)?"
                     r"\s*(?:कि|ki|that)\s+(?P<text>.+)$", re.I)
_CHAT = r"(?:chat|message|msg|मैसेज|मेसेज|चैट|बात)"
_VERB = r"(?:(?:karo|करो|bhejo|भेजो|kar|करें)(?=\s|$))"
_CHAT_AFTER = re.compile(rf"(?<![A-Za-z]){_CHAT}(?![A-Za-z])\s*{_VERB}?\s*(?:with|ke\s+saath|से|se|को|ko)?\s*(.+)$", re.I)
_CHAT_BEFORE = re.compile(rf"^(.+?)\s*(?:को|ko)\s+{_CHAT}(?![A-Za-z])\s*{_VERB}?\s*$", re.I)
_CALL = r"(?:call|कॉल|फ़ोन|फोन|phone)"
_CALL_LEAD = re.compile(rf"^\s*{_CALL}(?![A-Za-z])(?:\s+(?:karo|करो|kar)(?=\s))?\s+(.+)$", re.I)
_CALL_TRAIL = re.compile(rf"^(.+?)\s*(?:को|ko)\s+{_CALL}(?![A-Za-z])(?:\s+(?:karo|करो|kar)(?=\s|$))?\s*$", re.I)
_SAVE = re.compile(r"^\s*(?:save|add|सेव|जोड़ो|जोडो)(?![A-Za-z])\s*(?:करो|karo|kar)?\s*(?:contact|कॉन्टैक्ट|कांटेक्ट)\s+"
                   r"(.+?)\s+(\+?\d[\d\s-]{6,})\s*$", re.I)
_LEAD = {"open", "khol", "kholo", "kholiye", "and", "और", "please", "पर", "par", "pe", "on", "ko", "को", "for", "kar",
         "karo", "करो"}
_TRAIL = {"पर", "par", "pe", "on", "karo", "करो", "please", "दो", "do", "ko", "को", "youtube", "यूट्यूब", "whatsapp"}


@dataclass
class VoiceRoute:
    kind: str
    query: str = ""
    name: str = ""
    phone: str = ""


def strip_wake(text: str) -> str:
    return WAKE_RE.sub("", text or "", count=1).strip()


def clean(fragment: str) -> str:
    tokens = fragment.strip(" ,.!?:;-").split()
    while tokens and tokens[0].lower() in _LEAD:
        tokens.pop(0)
    while tokens and tokens[-1].lower() in _TRAIL:
        tokens.pop()
    return " ".join(tokens).strip(" ,.!?:;-")


def normalize_phone(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    if not re.fullmatch(r"\+?[\d\s-]+", text):
        return None
    digits = re.sub(r"\D", "", text)
    if not 8 <= len(digits) <= 15:
        return None
    if text.startswith("+"):
        return "+" + digits
    local = os.environ.get("GALAXY_DEFAULT_COUNTRY_CODE", "91")
    return f"+{local}{digits}" if len(digits) == 10 else "+" + digits


def youtube_url(query: str) -> str:
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)


def _whatsapp(text: str) -> VoiceRoute:
    core = WHATSAPP_RE.sub(" ", text).strip()
    match = _MSG_EN.match(core) or _MSG_HI.match(core)
    if match:
        name, body = clean(match.group("name")), clean(match.group("text"))
        if name and body:
            return VoiceRoute("whatsapp_message", name=name, query=body)
    name = ""
    match = _CHAT_BEFORE.match(core)
    if match:
        name = clean(match.group(1))
    if not name:
        match = _CHAT_AFTER.search(core)
        if match:
            name = clean(match.group(1))
    if name:
        return VoiceRoute("whatsapp_chat", name=name)
    return VoiceRoute("whatsapp_open")


def _call(text: str) -> Optional[VoiceRoute]:
    match = _CALL_LEAD.match(text) or _CALL_TRAIL.match(text)
    if not match:
        return None
    name = clean(match.group(1))
    if not name or len(name) > 40 or TECH_RE.search(name):
        return None
    return VoiceRoute("call", name=name)


def _notify(text: str) -> Optional[VoiceRoute]:
    match = _NOTIFY.match(text)
    if match:
        body = clean(match.group("text"))
        if body:
            return VoiceRoute("notify", query=body)
    return None


def _camera(text: str) -> Optional[VoiceRoute]:
    return VoiceRoute("camera") if CAMERA_RE.search(text) else None


def _settings(text: str) -> Optional[VoiceRoute]:
    match = SETTINGS_RE.search(text)
    if match and OPEN_RE.search(text):
        return VoiceRoute("settings", query=match.group(1).lower().replace("wi-fi", "wifi"))
    return None


def _app_open(text: str) -> Optional[VoiceRoute]:
    match = APP_OPEN_RE.match(text)
    if not match:
        return None
    word = match.group("app").lower()
    name = "contacts" if ("contact" in word or "कॉन्टैक्ट" in word) else "dialer"
    return VoiceRoute("app_open", name=name)


def _to_route(command: MC.MediaCommand) -> VoiceRoute:
    if command.kind == "platform_needed":
        return VoiceRoute("platform_needed", query=command.query)
    if command.kind in ("needs_query", "needs_direction"):
        return VoiceRoute(command.kind, name=command.platform)
    return VoiceRoute(f"{command.platform}_{command.kind}", name=command.platform, query=command.query)


def classify(text: str) -> VoiceRoute:
    t = strip_wake(text)
    save = _SAVE.match(t)
    if save:
        return VoiceRoute("save_contact", name=clean(save.group(1)), phone=normalize_phone(save.group(2)) or "")
    for route in (_notify(t), _camera(t), _settings(t), _app_open(t)):
        if route is not None:
            return route
    command = MC.parse_media(t)
    if command is not None:
        return _to_route(command)
    if WHATSAPP_RE.search(t):
        return _whatsapp(t)
    call = _call(t)
    if call is not None:
        return call
    return VoiceRoute("reasoning")
