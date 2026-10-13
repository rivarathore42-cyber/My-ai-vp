"""Universal platform registry (Phase 8): the one list of apps and sites Galaxy may open, and how each is reached.

Everything that builds or checks a link reads this list: android_intents (URL rules), media_controller (voice names), and
the permission modal (its allowlist block is generated from here; a test checks the block in the JavaScript file).

A platform is reached only through its own pair: its host with its own Android package. A link that names one platform's
host with another platform's package is refused. That is the cross-app rule.

VERIFY BEFORE RELEASE: hosts, search paths and package names were chosen from each platform's public site and Android
package. They change with app versions, countries and devices. PLATFORM_VERIFY lists what to check on a real device.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    category: str                  # "audio", "video", "messaging" or "system"
    boundary: str                  # Friend Mode category: media, messaging, calls, camera, settings, notifications
    host: Optional[str]            # the https host the platform is reached through, or None (app-only)
    package: str                   # the Android package, or "" when the device has no package (camera, settings)
    search: Optional[str]          # URL template with {q}, or None when search is not offered
    root: Optional[str]            # https start page, or None
    always_ok: bool                # may be remembered by Always Allow (low risk; calls and messages never)
    extra_hosts: tuple = ()
    badge: str = ""


PLATFORMS = {p.key: p for p in (
    Platform("spotify", "Spotify", "audio", "media", "open.spotify.com", "com.spotify.music",
             "https://open.spotify.com/search/{q}", "https://open.spotify.com/", True, badge="♪"),
    Platform("youtube_music", "YouTube Music", "audio", "media", "music.youtube.com", "com.google.android.apps.youtube.music",
             "https://music.youtube.com/search?q={q}", "https://music.youtube.com/", True, badge="♫"),
    Platform("jiosaavn", "JioSaavn", "audio", "media", "www.jiosaavn.com", "com.jio.media.jiobeats",
             "https://www.jiosaavn.com/search/{q}", "https://www.jiosaavn.com/", True, badge="J"),
    Platform("apple_music", "Apple Music", "audio", "media", "music.apple.com", "com.apple.android.music",
             "https://music.apple.com/search?term={q}", "https://music.apple.com/", True, badge="A"),
    Platform("wynk", "Wynk Music", "audio", "media", "wynk.in", "com.bsbportal.music",
             "https://wynk.in/music/search?q={q}", "https://wynk.in/", True, badge="Ω"),
    Platform("youtube", "YouTube", "video", "media", "www.youtube.com", "com.google.android.youtube",
             "https://www.youtube.com/results?search_query={q}", "https://www.youtube.com/", True, badge="▶"),
    Platform("netflix", "Netflix", "video", "media", "www.netflix.com", "com.netflix.mediaclient",
             "https://www.netflix.com/search?q={q}", "https://www.netflix.com/", True, badge="N"),
    Platform("prime_video", "Prime Video", "video", "media", "www.primevideo.com", "com.amazon.avod.thirdpartyclient",
             "https://www.primevideo.com/search/?phrase={q}", "https://www.primevideo.com/", True, badge="P"),
    Platform("hotstar", "Hotstar", "video", "media", "www.hotstar.com", "in.startv.hotstar",
             "https://www.hotstar.com/in/search?q={q}", "https://www.hotstar.com/in", True, badge="H"),
    Platform("whatsapp", "WhatsApp", "messaging", "messaging", "web.whatsapp.com", "com.whatsapp",
             None, "https://web.whatsapp.com/", True, extra_hosts=("wa.me",), badge="✉"),
    Platform("contacts", "Contacts", "system", "calls", None, "com.google.android.contacts", None, None, False, badge="☏"),
    Platform("dialer", "Phone", "system", "calls", None, "com.google.android.dialer", None, None, False, badge="✆"),
    Platform("camera", "Camera", "system", "camera", None, "", None, None, True, badge="◉"),
    Platform("settings", "Settings", "system", "settings", None, "", None, None, True, badge="⚙"),
)}

SETTINGS_ACTIONS = {"wifi": "android.settings.WIFI_SETTINGS", "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
                    "sound": "android.settings.SOUND_SETTINGS", "display": "android.settings.DISPLAY_SETTINGS"}

PLATFORM_VERIFY = (
    "Each host opens the platform's own search or start page on Chrome for Android, with the app installed and not.",
    "Each package name matches the installed app (Play Store listing) on the target country and device.",
    "Wynk and JioSaavn search paths; Hotstar and Wynk domains have changed over time.",
    "intent:// fallbacks resolve to the same host as the app link (the rule enforced here).",
)


def media_keys() -> frozenset:
    return frozenset(k for k, p in PLATFORMS.items() if p.category in ("audio", "video"))


def hosts() -> frozenset:
    out = set()
    for p in PLATFORMS.values():
        if p.host:
            out.add(p.host)
        out.update(p.extra_hosts)
    return frozenset(out)


def pairs() -> frozenset:
    return frozenset((p.host, p.package) for p in PLATFORMS.values() if p.host and p.package)


def packages() -> frozenset:
    return frozenset(p.package for p in PLATFORMS.values() if p.package)


def always_keys() -> frozenset:
    return frozenset(k for k, p in PLATFORMS.items() if p.always_ok)


def search_url(key: str, query: str) -> str:
    template = PLATFORMS[key].search
    if not template:
        raise ValueError(f"{PLATFORMS[key].label} search is not offered")
    return template.format(q=quote(query, safe=""))


def js_allowlist() -> str:
    """The generated block that permission_modal.js must contain verbatim (a test checks it)."""
    def arr(items) -> str:
        return "[" + ", ".join("'" + item + "'" for item in sorted(items)) + "]"
    return ("  // BEGIN GENERATED ALLOWLIST (galaxy/platforms.py). Do not edit by hand; a test checks this block.\n"
            f"  var HOSTS = {arr(hosts())};\n"
            f"  var PACKAGES = {arr(packages())};\n"
            f"  var PAIRS = {arr(f'{h}|{p}' for h, p in pairs())};\n"
            f"  var SETTINGS = {arr(SETTINGS_ACTIONS.values())};\n"
            "  // END GENERATED ALLOWLIST\n")
