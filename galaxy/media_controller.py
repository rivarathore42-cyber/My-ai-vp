"""Universal media and app router (Phase 8).

parse_media(text) reads a natural voice command and returns a MediaCommand: which platform, which verb, and the query or
the seek amount. It builds no URL and runs nothing. build(platform, verb, query) turns a command into a validated IntentSpec.

Audio platforms: Spotify, YouTube Music, JioSaavn, Apple Music, Wynk. Video platforms: YouTube, Netflix, Prime Video, Hotstar.
System apps (WhatsApp, Phone, Contacts, Camera, Settings) are routed by voice_router.

Rules
- A control (next, previous, pause, resume, seek) never guesses the app. Without a named platform the router asks which one.
- The longest name wins: "YouTube Music" is not "YouTube".
- The platform name is taken out before the verb and query are read, so "Netflix par X search karo" and "search X on
  Netflix" give the same command.
- Seek needs an amount and a direction. Without a direction the router asks; it does not assume one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional

from . import android_intents as AI
from .platforms import PLATFORMS, media_keys

ALIASES = {
    "spotify": ["spotify", "स्पॉटिफ़ाई", "स्पॉटिफाई", "स्पोटिफाई"],
    "youtube_music": ["youtube music", "yt music", "यूट्यूब म्यूजिक", "यूट्यूब म्यूज़िक"],
    "jiosaavn": ["jiosaavn", "jio saavn", "saavn", "जियोसावन", "जियो सावन"],
    "apple_music": ["apple music", "एप्पल म्यूजिक", "ऐपल म्यूजिक", "एप्पल म्यूज़िक"],
    "wynk": ["wynk music", "wynk", "विंक"],
    "youtube": ["youtube", "you tube", "यूट्यूब", "यू ट्यूब"],
    "netflix": ["netflix", "नेटफ्लिक्स"],
    "prime_video": ["prime video", "amazon prime video", "amazon prime", "प्राइम वीडियो"],
    "hotstar": ["disney+ hotstar", "hotstar", "hot star", "हॉटस्टार", "हॉट स्टार"],
}
MEDIA_KEYS = media_keys()
if set(ALIASES) != set(MEDIA_KEYS):
    raise RuntimeError("media aliases and the platform registry disagree")

_LOOKUP = {alias.lower(): key for key, aliases in ALIASES.items() for alias in aliases}
_PLATFORM_RE = re.compile(r"(?<!\S)(" + "|".join(re.escape(a) for a in sorted(_LOOKUP, key=len, reverse=True))
                          + r")(?=[\s.,!?]|$)", re.I)
_FORWARD = frozenset({"forward", "ahead", "aage", "आगे", "badhao", "badhaao", "badhaiye", "badhaye", "बढ़ाओ", "बढाओ",
                      "बढ़ाइए"})
_BACKWARD = frozenset({"backward", "back", "peeche", "पीछे", "wapas", "ghataon", "घटाओ", "piche"})
_MEDIA_WORD = r"(?:track|tracks|song|songs|gaana|gaane|gana|गाना|गाने|गीत|ट्रैक|music|संगीत|video|movie|show|episode)"
_NUM = re.compile(r"(?<!\S)(\d{1,4})\s*(seconds?|secs?|sec|minutes?|mins?|min|सेकंड|सेकेंड|मिनट)?(?=\s|$|[.!?])", re.I)
_NEXT = re.compile(r"(?<!\S)(?:next|skip|agla|अगला)(?=\s|$|[.!?])", re.I)
_CHANGE = re.compile(r"(?<!\S)(?:change|badlo|बदलो)(?=\s|$)", re.I)
_PREVIOUS = re.compile(r"(?<!\S)(?:previous|pichla|पिछला|pichhla)(?=\s|$|[.!?])", re.I)
_PAUSE = re.compile(r"(?<!\S)(?:pause|roko|रोको|band\s+karo|बंद\s+करो|rok\s+do|रोक\s+दो)(?=\s|$|[.!?])", re.I)
_RESUME = re.compile(r"(?<!\S)(?:resume|jaari\s+rakho|जारी\s+रखो)(?=\s|$|[.!?])", re.I)
_SEEK_WORD = re.compile(r"(?<!\S)seek(?=\s|$|[.!?])", re.I)
_SEARCH = re.compile(r"(?<!\S)(?:search|khojo|khoj|खोजो|dhoondho|dhundo|ढूंढो|ढूँढो|सर्च)(?=\s|$|[.!?])", re.I)
_PLAY = re.compile(r"(?<!\S)(?:play|chalao|चलाओ|bajao|बजाओ|lagao|लगाओ|sunao|सुनाओ)(?=\s|$|[.!?])", re.I)
_OPEN = re.compile(r"(?<!\S)(?:open|khol\w*|खोल\S*)(?=\s|$|[.!?])", re.I)
_LIBRARY = re.compile(r"(?<!\S)(?:library|लाइब्रेरी)(?=\s|$|[.!?])", re.I)
_START_CONTROL = re.compile(r"^\s*(?:(?:please|plz|ok|oye)\s+)?(?:next|skip|agla|अगला|previous|pichla|पिछला|pause|roko|रोको"
                            r"|resume)(?=\s|$|[.!?])", re.I)
_START_CHANGE = re.compile(r"^\s*(?:(?:please|plz|ok|oye)\s+)?(?:change|badlo|बदलो)\s+(?:the\s+)?"
                           r"(?:track|song|gaana|गाना|गीत|ट्रैक)(?=\s|$|[.!?])", re.I)
_PLAY_SONG = re.compile(r"^\s*(?:play|chalao|bajao|sunao)\s+(?:song|gaana|music|गाना|गीत|संगीत)(?=\s|$)"
                        r"|(?<!\S)(?:song|gaana|music|गाना|गीत)\s+(?:chalao|bajao|lagao|sunao|play|चलाओ|बजाओ|लगाओ|सुनाओ)"
                        r"(?=\s|$|[.!?])", re.I)
_START_NUMBER = re.compile(r"^\s*(?:(?:please|plz)\s+)?\d", re.I)
_FILLERS = frozenset({"par", "pe", "on", "पर", "se", "से", "ko", "को", "karo", "करो", "kar", "do", "दो", "please", "and",
                      "और", "open", "khol", "kholo", "search", "khojo", "khoj", "play", "chalao", "bajao", "lagao", "sunao",
                      "gaana", "gaane", "gana", "song", "songs", "track", "tracks", "music", "video", "show", "movie",
                      "serial", "series", "गाना", "गाने", "गीत", "संगीत", "वीडियो", "शो", "फिल्म", "my", "mera", "meri",
                      "mere", "chalaiye", "lagaiye"})


@dataclass(frozen=True)
class MediaCommand:
    kind: str              # search, play, open, next, previous, pause, resume, seek, needs_query, needs_direction, platform_needed
    platform: str = ""     # a platform key, or "" when no platform was named
    query: str = ""        # search text, or "<seconds>:<direction>" for seek


def find_platform(text: str):
    """(platform key, the text without the platform name), or None."""
    match = _PLATFORM_RE.search(text)
    if not match:
        return None
    key = _LOOKUP[match.group(1).lower()]
    return key, (text[:match.start()] + " " + text[match.end():]).strip()


def _clean(fragment: str) -> str:
    tokens = [t for t in fragment.replace(",", " ").split() if t]
    while tokens and tokens[0].lower().strip(".!?") in _FILLERS:
        tokens.pop(0)
    while tokens and tokens[-1].lower().strip(".!?") in _FILLERS:
        tokens.pop()
    return " ".join(tokens).strip(" .!?:;-")


def _around(core: str, pattern: re.Pattern) -> str:
    """The query can follow the verb ('search X') or come before it ('X khojo')."""
    match = pattern.search(core)
    return _clean(core[match.end():]) or _clean(core[:match.start()])


def _seek_amount(text: str) -> Optional[tuple]:
    """(seconds, direction) when the text has an amount and a direction word; otherwise None."""
    match = _NUM.search(text)
    if not match:
        return None
    direction = None
    for word in re.split(r"[\s,.!?]+", text):
        lowered = word.lower()
        if lowered in _FORWARD:
            direction = "forward"
            break
        if lowered in _BACKWARD:
            direction = "backward"
            break
    if direction is None:
        return None
    unit = (match.group(2) or "").lower()
    scale = 60 if unit.startswith(("min", "मिनट")) else 1
    return int(match.group(1)) * scale, direction


def _control(core: str) -> Optional[str]:
    if _NEXT.search(core) or (_CHANGE.search(core) and re.search(_MEDIA_WORD, core, re.I)):
        return "next"
    if _PREVIOUS.search(core):
        return "previous"
    if _PAUSE.search(core):
        return "pause"
    if _RESUME.search(core):
        return "resume"
    return None


def _without_platform(text: str) -> Optional[MediaCommand]:
    """A playback command with no app named is never assumed: the router asks which app."""
    if _START_CONTROL.search(text) or _START_CHANGE.search(text) or _PLAY_SONG.search(text):
        return MediaCommand("platform_needed")
    seek = _seek_amount(text)
    if seek is not None and (_SEEK_WORD.search(text) or _START_NUMBER.search(text)):
        return MediaCommand("platform_needed", query=f"{seek[0]}:{seek[1]}")
    return None


def parse_media(text: str) -> Optional[MediaCommand]:
    text = " ".join((text or "").split())
    if not text:
        return None
    found = find_platform(text)
    if found is None:
        return _without_platform(text)
    key, core = found
    if _LIBRARY.search(core):
        return MediaCommand("open", key, "library")
    seek = _seek_amount(core)
    if seek is not None:
        return MediaCommand("seek", key, f"{seek[0]}:{seek[1]}")
    if _SEEK_WORD.search(core) and _NUM.search(core):
        return MediaCommand("needs_direction", key)
    control = _control(core)
    if control:
        return MediaCommand(control, key)
    if _SEARCH.search(core):
        query = _around(core, _SEARCH)
        return MediaCommand("search", key, query) if query else MediaCommand("needs_query", key)
    if _PLAY.search(core):
        query = _around(core, _PLAY)
        return MediaCommand("play", key, query) if query else MediaCommand("resume", key)
    if _OPEN.search(core) or not _clean(core):
        return MediaCommand("open", key)
    return None


def build(platform: str, verb: str, query: str = "") -> AI.IntentSpec:
    """A validated intent for one media command. Raises ValueError for anything outside the rules."""
    if platform not in MEDIA_KEYS:
        raise ValueError("not a media platform")
    if verb == "open":
        spec = AI.platform_open(platform)
        if query == "library":
            spec = replace(spec, preview=f"Galaxy AI cannot pick a track from your {spec.platform} library; "
                                         "playback stays inside the app.")
        return spec
    if verb == "search":
        return AI.platform_search(platform, query)
    if verb == "play":
        return AI.platform_search(platform, query, verb="Play")
    if verb in ("next", "previous", "pause", "resume"):
        return AI.platform_control(platform, verb)
    if verb == "seek":
        seconds, _, direction = query.partition(":")
        return AI.platform_control(platform, "seek", int(seconds), direction)
    raise ValueError(f"unknown media verb {verb!r}")
