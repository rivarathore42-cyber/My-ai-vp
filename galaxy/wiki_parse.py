"""Agent 6 helpers (Phase 2): Wikipedia wikitext -> structured facts, with boilerplate removed.

Scope: a heuristic subset of MediaWiki markup, not a full parser. It reads the first infobox (nested templates are kept
inside one field; date templates render as YYYY-MM-DD), the lead paragraph, and the sections whose titles are in
TARGET_SECTIONS. Tables, galleries, references, categories, file links and HTML comments are removed, not rendered.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Optional

TARGET_SECTIONS = ("history", "background", "biography", "early life", "career", "discovery", "overview", "definition",
                   "description", "science", "physics", "chemistry", "biology", "mathematics", "applications", "legacy")
BOILERPLATE_SECTIONS = ("see also", "references", "external links", "further reading", "notes", "bibliography",
                        "sources", "gallery", "footnotes", "citations", "publications", "works")
MAX_FIELD_CHARS = 200
MAX_SECTION_CHARS = 450
MAX_SECTIONS = 4
MAX_LEAD_SENTENCES = 3
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_REF = re.compile(r"<ref\b[^>]*/>|<ref\b[^>]*>.*?</ref>", re.S | re.I)
_GALLERY = re.compile(r"<gallery\b[^>]*>.*?</gallery>", re.S | re.I)
_FILE_LINK = re.compile(r"\[\[(?:File|Image|Category|Special|Wikipedia|Help):[^\[\]]*(?:\[\[[^\[\]]*\]\][^\[\]]*)*\]\]", re.I)
_DATE = re.compile(r"\{\{\s*(?:birth|death|start|end)\s+(?:and\s+age\s+)?date[^{}|]*\|\s*(\d{1,4})\s*\|\s*(\d{1,2})\s*\|\s*(\d{1,2})[^{}]*\}\}", re.I)
_YEAR = re.compile(r"\{\{\s*(?:birth|death)\s+year[^{}|]*\|\s*(\d{1,4})[^{}]*\}\}", re.I)
_PIPED_LINK = re.compile(r"\[\[(?:[^\[\]|]*\|)?([^\[\]]*)\]\]")
_EXT_LINK = re.compile(r"\[https?://\S+\s+([^\]]+)\]")
_EXT_BARE = re.compile(r"\[https?://[^\]]*\]")
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_HEADING = re.compile(r"^(={2,4})\s*(.+?)\s*\1\s*$", re.M)
_DISAMBIG = re.compile(r"\{\{\s*(?:disambiguation|hndis|dab)\b", re.I)
_INFOBOX = re.compile(r"\{\{\s*Infobox\b", re.I)


def strip_balanced(text: str, opener: str, closer: str) -> str:
    """Removes balanced blocks such as {{...}} or {|...|}. An unclosed block is dropped to the end of the text."""
    pattern = re.compile(re.escape(opener) + "|" + re.escape(closer))
    out, depth, last = [], 0, 0
    for match in pattern.finditer(text):
        if match.group(0) == opener:
            if depth == 0:
                out.append(text[last:match.start()])
            depth += 1
        elif depth > 0:
            depth -= 1
            if depth == 0:
                last = match.end()
    if depth == 0:
        out.append(text[last:])
    return "".join(out)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def _clean_inline(text: str) -> str:
    text = _YEAR.sub(lambda m: m.group(1), text)
    text = _COMMENT.sub("", text)
    text = _REF.sub("", text)
    text = _GALLERY.sub("", text)
    for _ in range(3):
        text = _FILE_LINK.sub("", text)
    text = _DATE.sub(lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", text)
    text = strip_balanced(text, "{{", "}}")
    text = strip_balanced(text, "{|", "|}")
    text = _PIPED_LINK.sub(lambda m: m.group(1), text)
    text = _EXT_LINK.sub(lambda m: m.group(1), text)
    text = _EXT_BARE.sub("", text)
    text = _BR.sub("; ", text)
    text = _TAG.sub(" ", text)
    return text.replace("'''", "").replace("''", "")


def _clean_value(value: str) -> str:
    cleaned = _normalize(_clean_inline(value)).strip("; ")
    return cleaned[:MAX_FIELD_CHARS]


def _find_block(text: str, start: int) -> Optional[str]:
    depth = 0
    for match in re.finditer(r"\{\{|\}\}", text[start:]):
        depth += 1 if match.group(0) == "{{" else -1
        if depth == 0:
            return text[start:start + match.end()]
    return None


def _split_top_level(body: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for match in re.finditer(r"\{\{|\}\}|\[\[|\]\]|\|", body):
        token = match.group(0)
        if token in ("{{", "[["):
            depth += 1
        elif token in ("}}", "]]"):
            depth = max(0, depth - 1)
        elif depth == 0:
            parts.append(body[start:match.start()])
            start = match.end()
    parts.append(body[start:])
    return parts


def extract_infobox(wikitext: str) -> OrderedDict:
    match = _INFOBOX.search(wikitext)
    if not match:
        return OrderedDict()
    block = _find_block(wikitext, match.start())
    if not block:
        return OrderedDict()
    fields: OrderedDict = OrderedDict()
    for part in _split_top_level(block[2:-2])[1:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = _normalize(key.replace("_", " ")).lower()
        if not key or len(key) > 60:
            continue
        cleaned = _clean_value(value)
        if cleaned and key not in fields:
            fields[key] = cleaned
    return fields


def _lead_and_sections(stripped: str) -> tuple[str, list[tuple[str, str]]]:
    headings = list(_HEADING.finditer(stripped))
    lead_src = stripped[:headings[0].start()] if headings else stripped
    lead = " ".join(_sentences(_normalize(lead_src))[:MAX_LEAD_SENTENCES])
    sections = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(stripped)
        sections.append((heading.group(2).strip().lower(), _normalize(stripped[heading.end():end])))
    return lead, sections


def select_sections(sections: list[tuple[str, str]]) -> OrderedDict:
    chosen: OrderedDict = OrderedDict()
    for title, body in sections:
        if not body or any(b in title for b in BOILERPLATE_SECTIONS):
            continue
        if not any(t in title for t in TARGET_SECTIONS):
            continue
        cut = body[:MAX_SECTION_CHARS]
        if len(body) > MAX_SECTION_CHARS:
            cut = cut.rsplit(" ", 1)[0]
        chosen[title] = cut
        if len(chosen) >= MAX_SECTIONS:
            break
    return chosen


def build_facts(title: str, url: str, wikitext: str) -> dict:
    if _DISAMBIG.search(wikitext):
        raise ValueError("disambiguation page")
    infobox = extract_infobox(wikitext)
    lead, sections = _lead_and_sections(_clean_inline(wikitext))
    return {"title": title, "url": url, "lead": lead, "infobox": dict(infobox),
            "sections": dict(select_sections(sections))}


def facts_to_text(facts: dict, limit: int = 900) -> str:
    parts = []
    if facts.get("lead"):
        parts.append(facts["lead"])
    if facts.get("infobox"):
        parts.append("Facts: " + "; ".join(f"{k}: {v}" for k, v in facts["infobox"].items())[:300] + ".")
    for title, body in facts.get("sections", {}).items():
        parts.append(f"{title.capitalize()}: {body[:200]}")
    text = " ".join(parts)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0]
    return text


def claim_list(facts: dict) -> list[dict]:
    """Structured claims: one key/value per infobox field, one per lead sentence. Values are verbatim source text."""
    claims = [{"key": k, "value": v, "source": "infobox"} for k, v in facts.get("infobox", {}).items()]
    claims += [{"key": "lead", "value": s, "source": "lead"} for s in _sentences(facts.get("lead", ""))]
    return claims


def unsupported_by_claims(answer: str, facts: dict) -> list[str]:
    """Verification against the structured claims: every number and multi-word name in the answer must occur in a
    claim value. Uses the same lexical rule as the reflection step, but against claims instead of raw text."""
    from .reflection import unsupported_claims
    corpus = " ".join(c["value"] for c in claim_list(facts))
    return unsupported_claims(answer, corpus)
