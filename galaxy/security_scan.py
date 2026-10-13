"""Agent 2 - Security Sentinel (RC-05 hardened).

Pipeline for every prompt, in this order:
 1. Strip invisible and format characters everywhere: zero-width space and joiner, word joiner, soft hyphen, bidi
    controls and isolates, variation selectors, Hangul fillers, Mongolian separators, combining grapheme joiner,
    Unicode tag characters, control codes. ZWJ and ZWNJ survive ONLY between two Devanagari letters (legitimate
    conjuncts); anywhere else they are an evasion and are removed.
 2. NFKC normalisation (fullwidth forms become ASCII), then a confusables map (Cyrillic, Greek, roman numerals).
 3. Scan variants: the plain text, a leetspeak fold (1->i 0->o 3->e 4->a 5->s 7->t @->a $->s !->i), and decoded
    payloads (HTML entities, \\uXXXX and \\xNN escapes, percent encoding, base64 of 16+ characters).
 4. Severity-tagged rules. BLOCK on any high rule, or on two medium rules. Downstream agents receive the clean text
    (invisibles removed, confusables mapped), never the raw prompt.
Limit: heuristic. It removes the known evasion classes and logs everything; it does not understand intent.
"""
from __future__ import annotations

import base64
import binascii
import html
import re
import unicodedata
from typing import Any
from urllib.parse import unquote

from .core import MAX_INPUT_CHARS, Agent, AgentResult, fail, ok

# Invisible and format characters, removed everywhere (ZWJ/ZWNJ are handled by _strip_joiners).
_INVISIBLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u180b-\u180f\u200b\u200e\u200f\u2060-\u206f\u202a-\u202e"
    "\ufe00-\ufe0f\ufeff\u3164\uffa0\U000e0000-\U000e007f\U000e0100-\U000e01ef]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_JOINERS = "\u200c\u200d"
_DEVANAGARI = ("\u0900", "\u097f")

CONFUSABLES = {
    # Cyrillic
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "һ": "h", "ԝ": "w", "ӏ": "l", "ї": "i", "ү": "y", "ѵ": "v",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "Х": "X", "І": "I", "Ѕ": "S", "Ү": "Y",
    # Greek
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O",
    "Ρ": "P", "Τ": "T", "Χ": "X", "Υ": "Y", "ο": "o", "ν": "v", "α": "a", "ι": "i", "ρ": "p", "υ": "u",
    "ϲ": "c", "ε": "e",
    # other lookalikes
    "ɡ": "g", "ɑ": "a", "ɩ": "i", "ǀ": "l", "ⅰ": "i", "ⅼ": "l",
}
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
_BASE64 = re.compile(r"(?<![A-Za-z0-9+/])(?:[A-Za-z0-9+/]{4}){4,}(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?(?![A-Za-z0-9+/=])")
_HEX = re.compile(r"(?:\\x[0-9a-fA-F]{2}){1,}")
_UNI = re.compile(r"(?:\\u[0-9a-fA-F]{4}){1,}")
_PCT = re.compile(r"(?:%[0-9a-fA-F]{2}){1,}")

# RC-05: runs of single letters split by spaces or punctuation ("i g n o r e", "i.g.n.o.r.e") are folded for scanning.
_SPACED = re.compile(r"(?<![A-Za-z])(?:[A-Za-z][ \t._\-*|/]){2,}[A-Za-z](?![A-Za-z])")


def _collapse_spaced(text: str) -> str:
    return _SPACED.sub(lambda m: re.sub(r"[ \t._\-*|/]", "", m.group(0)), text)


_RULE_SOURCES = [
    ("override_instructions", "high",
     r"\b(?:ignore|disregard|forget|skip|override|bypass)\s+(?:all\s+|any\s+|every\s+|your\s+|the\s+|my\s+)?"
     r"(?:previous|prior|above|earlier|original|system|initial|preceding)\s+(?:instructions?|prompts?|rules?|directions?|guidelines?|messages?|context)\b"
     r"|\b(?:ignore|disregard|forget)\s+(?:all|everything)\s+(?:above|before|you\s+were\s+told)\b"),
    ("reveal_system_prompt", "high",
     r"\b(?:reveal|print|show|repeat|output|display|leak|dump)\b.{0,30}\b(?:system\s+prompt|hidden\s+prompt|developer\s+(?:message|prompt)|your\s+instructions|initial\s+prompt)\b"),
    ("role_override", "high",
     r"\byou\s+are\s+now\b|\bpretend\s+(?:to\s+be|you\s+are)\b|\bfrom\s+now\s+on\s+you\s+(?:will|are|must)\b|"
     r"\bact\s+as\s+(?:dan|an?\s+unfiltered|an?\s+evil|an?\s+unrestricted)\b"),
    ("jailbreak_persona", "high",
     r"\b(?:jailbreak|dan\s+mode|developer\s+mode\s+(?:enabled|on)|do\s+anything\s+now|aim\s+mode|stan\s+mode|evil\s+confidant)\b"
     r"|\b(?:no|without\s+any)\s+(?:restrictions|filters|rules|guardrails|limits)\b.{0,40}\b(?:answer|respond|reply|you)\b"),
    ("disable_safety", "high",
     r"\b(?:disable|bypass|turn\s+off|remove|circumvent)\b.{0,30}\b(?:safety|guardrails?|filters?|restrictions?|moderation|content\s+policy)\b"),
    ("system_override", "high",
     r"\[\s*system\s*\]|<\s*/?\s*system\s*>|\bsystem\s+override\b|\badmin(?:istrator)?\s+mode\b|\broot\s+access\b"),
    ("chat_template_injection", "high",
     r"<\|im_(?:start|end)\|>|<\|(?:system|user|assistant|endoftext)\|>|\[/?INST\]|<</?SYS>>|^\s*###\s*(?:system|instruction)\s*:"),
    ("shell_injection", "high",
     r"(?:;|&&|\|\||`|\$\()\s*(?:rm\s+-rf|curl\s+\S+\s*\|\s*(?:ba)?sh|wget\s+\S+\s*\|\s*(?:ba)?sh|chmod\s+777|mkfs|dd\s+if=|nc\s+-e|bash\s+-i)"),
    ("secret_exfil", "high",
     r"\b(?:cat|type|print|show|read|dump|exfiltrate)\b.{0,20}(?:/etc/passwd|/etc/shadow|\.env\b|id_rsa|api[_ ]?key|secret\s+key|private\s+key)"),
    ("indirect_injection", "medium",
     r"\b(?:the\s+(?:following|above|attached|embedded)\s+(?:document|text|email|webpage|page|file|message)\s+(?:says|tells|instructs|asks)\s+"
     r"(?:you|the\s+assistant|the\s+ai)|when\s+(?:summari[sz]ing|reading|translating)\s+this\b.{0,60}\b(?:instead|also|ignore|must))"),
    ("url_exfil", "medium",
     r"\b(?:send|post|upload|forward|email)\b.{0,40}\b(?:password|token|secret|api\s*key|credentials?|conversation)\b.{0,40}https?://"),
    ("persona_roleplay_bypass", "medium",
     r"\bhypothetically\b.{0,60}\b(?:no\s+rules|no\s+restrictions|without\s+(?:any\s+)?(?:limits|filters))\b"),
]
RULES = [(name, sev, re.compile(src, re.I | re.M)) for name, sev, src in _RULE_SOURCES]


def _is_devanagari(ch: str) -> bool:
    return _DEVANAGARI[0] <= ch <= _DEVANAGARI[1]


def _strip_joiners(text: str) -> tuple[str, int]:
    """ZWJ and ZWNJ are kept only between two Devanagari letters; elsewhere they are removed as evasion."""
    out, removed = [], 0
    for index, ch in enumerate(text):
        if ch in _JOINERS:
            before = text[index - 1] if index > 0 else ""
            after = text[index + 1] if index + 1 < len(text) else ""
            if before and after and _is_devanagari(before) and _is_devanagari(after):
                out.append(ch)
            else:
                removed += 1
        else:
            out.append(ch)
    return "".join(out), removed


def remove_invisible(text: str) -> tuple[str, int]:
    text, joiners = _strip_joiners(text)
    text, invisible = _INVISIBLE.subn("", text)
    text, control = _CONTROL.subn("", text)
    return text, joiners + invisible + control


def map_confusables(text: str) -> tuple[str, int]:
    out, count = [], 0
    for ch in text:
        if ch in CONFUSABLES:
            out.append(CONFUSABLES[ch])
            count += 1
        else:
            out.append(ch)
    return "".join(out), count


def _readable(decoded: str) -> bool:
    return len(decoded) >= 8 and sum(ch.isprintable() for ch in decoded) >= 0.9 * len(decoded)


def _decode_hex(text: str) -> str:
    return _HEX.sub(lambda m: bytes.fromhex(m.group(0).replace("\\x", "")).decode("utf-8", "replace"), text)


def _decode_unicode(text: str) -> str:
    return _UNI.sub(lambda m: "".join(chr(int(c[2:], 16)) for c in re.findall(r"\\u[0-9a-fA-F]{4}", m.group(0))), text)


def decoded_variants(text: str) -> list[str]:
    variants: list[str] = []
    if "&" in text:
        unescaped = html.unescape(text)
        if unescaped != text:
            variants.append(unescaped)
    for decoder in (_decode_hex, _decode_unicode, lambda t: _PCT.sub(lambda m: unquote(m.group(0)), t)):
        changed = decoder(text)
        if changed != text:
            variants.append(changed)
    for match in _BASE64.finditer(text):
        candidate = match.group(0)
        try:
            decoded = base64.b64decode(candidate + "=" * (-len(candidate) % 4), validate=True).decode("utf-8", "strict")
        except (binascii.Error, ValueError):
            continue
        if _readable(decoded):
            variants.append(decoded)
        if len(variants) >= 8:
            break
    return variants[:8]


def scan(text: Any) -> dict:
    if not isinstance(text, str):
        return {"blocked": True, "flags": ["non_text_input"], "severity": "high", "clean": "", "invisible": 0,
                "confusables": 0}
    if len(text) > MAX_INPUT_CHARS:
        return {"blocked": True, "flags": ["too_long"], "severity": "high", "clean": "", "invisible": 0,
                "confusables": 0}
    stripped, invisible = remove_invisible(text)
    mapped, confusables = map_confusables(unicodedata.normalize("NFKC", stripped))

    def hits(variant: str) -> set[str]:
        return {name for name, _, rx in RULES if rx.search(variant)}

    severity_of = {name: sev for name, sev, _ in RULES}
    plain = hits(mapped)
    found: dict[str, str] = {name: severity_of[name] for name in plain}
    # An evasion is counted only when a rule fires on the folded or decoded form and not on the plain text.
    leet_new = hits(mapped.translate(_LEET)) - plain
    if leet_new:
        found.setdefault("leetspeak_evasion", "medium")
        for name in leet_new:
            found.setdefault(name, severity_of[name])
    spaced_new = hits(_collapse_spaced(mapped)) - plain
    if spaced_new:
        found.setdefault("spaced_letters_evasion", "medium")
        for name in spaced_new:
            found.setdefault(name, severity_of[name])
    for variant in decoded_variants(mapped):
        decoded_new = hits(variant) - plain
        if decoded_new:
            found.setdefault("hidden_payload", "high")
            for name in decoded_new:
                found.setdefault(name, severity_of[name])
    if invisible:
        found.setdefault("zero_width_evasion", "medium")
    if confusables:
        found.setdefault("homoglyph_substitution", "medium")
    severities = list(found.values())
    blocked = "high" in severities or severities.count("medium") >= 2
    level = "high" if "high" in severities else ("medium" if severities else "none")
    return {"blocked": blocked, "flags": sorted(found), "severity": level, "clean": mapped,
            "invisible": invisible, "confusables": confusables}


class SecuritySentinelAgent(Agent):
    name = "Security Sentinel"

    def scan(self, text: Any) -> dict:
        return scan(text)

    async def process(self, text: str, ctx: dict) -> AgentResult:
        s = scan(text)
        meta = {"flags": s["flags"], "severity": s["severity"]}
        if s["blocked"]:
            return fail(self.name, "blocked: " + ", ".join(s["flags"]), **meta)
        action = "sanitized" if s["flags"] else "allowed"
        return ok(self.name, "", clean=s["clean"], action=action, **meta)
