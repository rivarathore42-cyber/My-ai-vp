"""Agent 16 (Phase 4): voice-ready SSML and well-formed markdown.

- markdown_to_speech(md): code blocks become a short note, tables become a summary sentence, headings become strong
  emphasis, bold becomes moderate emphasis, links keep their text, bullets become sentences, HTML tags are removed.
  Markdown and HTML symbols never reach the speech text.
- to_ssml(md, rate): W3C SSML with <speak>, <p>, <s>, <prosody>, <emphasis> and <break>. Text is escaped.
- validate_ssml(ssml): parses the XML and checks the element set, attribute values and the absence of symbols.
- repair_fences(md): closes a code fence a section left open, so one broken section cannot break the whole answer.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET

RATES = ("x-slow", "slow", "medium", "fast", "x-fast")
STRENGTHS = ("none", "x-weak", "weak", "medium", "strong", "x-strong")
EMPHASIS_LEVELS = ("strong", "moderate", "reduced", "none")
ALLOWED_TAGS = frozenset({"speak", "p", "s", "prosody", "break", "emphasis"})
MAX_SPEECH_CHARS = 3000
_MARK_ON, _MARK_OFF = "\ue001", "\ue002"  # private-use: never control characters, never in normal text
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FENCE = re.compile(r"```.*?```", re.S)
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?(\s*\|\s*:?-{3,}:?)*\s*\|?\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_QUOTE = re.compile(r"^\s*>\s?")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_HTML = re.compile(r'</?(?:a|abbr|b|big|blockquote|br|code|del|div|em|font|h[1-6]|hr|i|img|ins|li|mark|ol|p|pre|s|small|span|strike|strong|sub|sup|table|td|th|tr|u|ul)(?:\s+[-\w:]+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s"\'<>]+))*\s*/?>', re.I)  # A16-01: "a<b and c>d" is not a tag
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_SYMBOL_STRIP = re.compile(r"[*#`|<>\[\]{}\\^~_=]")
_SYMBOL_CHECK = re.compile(r"[*#`|<>\[\]{}]")
_TIME = re.compile(r"^\d{1,4}ms$")


def repair_fences(markdown: str) -> str:
    """Closes a code fence a section left open. Counts fence lines, so an even count is left unchanged."""
    fences = sum(1 for line in markdown.splitlines() if line.lstrip().startswith("```"))
    return markdown + "\n```" if fences % 2 else markdown


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


_MATH_SPEECH = [(re.compile(r"(?<=\d)\s*\+\s*(?=\d)"), " plus "), (re.compile(r"(?<=\d)\s*\*\s*(?=\d)"), " times "),
                (re.compile(r"(?<=\d)\s*/\s*(?=\d)"), " divided by "), (re.compile(r"(?<=\d)\s*=\s*(?=-?\d)"), " equals "),
                (re.compile(r"(?<=\d)\s+-\s+(?=\d)"), " minus ")]


def _speak_math(text: str) -> str:
    for pattern, spoken in _MATH_SPEECH:  # A16-02: operators are read, not dropped
        text = pattern.sub(spoken, text)
    return text


def _sanitize(text: str) -> str:
    text = _CONTROL.sub("", text)
    text = _SYMBOL_STRIP.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _segments(line: str) -> str:
    """One line with bold spans wrapped in markers; links, code and HTML reduced to plain text."""
    line = _HTML.sub("", line)
    line = _LINK.sub(lambda m: m.group(1), line)
    line = _INLINE_CODE.sub(lambda m: m.group(1), line)
    return _BOLD.sub(lambda m: _MARK_ON + (m.group(1) or m.group(2)) + _MARK_OFF, line)


def markdown_to_speech(markdown: str) -> dict:
    text = _CONTROL.sub("", (markdown or "").replace("\r", "")).replace(_MARK_ON, "").replace(_MARK_OFF, "")
    omitted = len(_FENCE.findall(text))
    text = _FENCE.sub("\n\n(code block omitted)\n\n", text)
    lines, table_rows, out = text.split("\n"), [], []

    def flush_table() -> None:
        if table_rows:
            header = [c.strip() for c in table_rows[0].strip().strip("|").split("|")]
            data_rows = sum(1 for r in table_rows[1:] if not _TABLE_RULE.match(r))
            cols = ", ".join(c for c in header[:-1] if c) + (" and " + header[-1] if len(header) > 1 else header[0])
            out.append(f"A table with columns {cols} and {data_rows} row(s).")
            table_rows.clear()

    for raw in lines:
        if _TABLE_LINE.match(raw):
            table_rows.append(raw)
            continue
        flush_table()
        if not raw.strip() or _RULE.match(raw):
            out.append("")
            continue
        heading = _HEADING.match(raw)
        if heading:
            out.append(_MARK_ON + _sanitize(_segments(heading.group(1))).rstrip(".") + "." + _MARK_OFF + "\n\n")
            continue
        line = _QUOTE.sub("", _BULLET.sub("", raw))
        out.append(_segments(line))
    flush_table()
    paragraphs = []
    for block in "\n".join(out).split("\n\n"):
        if not block.strip():
            continue
        sentences = []
        for sentence in _sentences(block.replace("\n", " ")):
            on, off = sentence.count(_MARK_ON), sentence.count(_MARK_OFF)
            marked = _sanitize(_speak_math(sentence))
            if marked.strip(" ."):
                sentences.append({"text": marked, "balanced": on == off})
        if sentences:
            paragraphs.append(sentences)
    plain = " ".join(s["text"].replace(_MARK_ON, "").replace(_MARK_OFF, "") for p in paragraphs for s in p)
    return {"paragraphs": paragraphs, "plain": plain, "code_blocks_omitted": omitted}


def strip_markdown(text: str) -> str:
    return markdown_to_speech(text)["plain"]


def _xml_sentence(marked: str) -> str:
    parts = marked.split(_MARK_ON)
    xml_parts = [html.escape(parts[0], quote=False)]
    for segment in parts[1:]:
        emphasized, separator, rest = segment.partition(_MARK_OFF)
        if separator == "":
            xml_parts.append(html.escape(segment, quote=False))
            continue
        xml_parts.append(f'<emphasis level="moderate">{html.escape(emphasized, quote=False)}</emphasis>')
        xml_parts.append(html.escape(rest, quote=False))
    return "".join(xml_parts)


def to_ssml(markdown: str, rate: str = "medium", max_chars: int = MAX_SPEECH_CHARS) -> str:
    if rate not in RATES:
        raise ValueError(f"unsupported rate '{rate}'")
    speech = markdown_to_speech(markdown)
    budget, paragraphs = max_chars, []
    for paragraph in speech["paragraphs"]:
        kept = []
        for sentence in paragraph:
            if budget <= 0:
                break
            text = sentence["text"][:budget]
            budget -= len(text)
            kept.append(f"<s>{_xml_sentence(text)}</s>")
        if kept:
            paragraphs.append("<p>" + " ".join(kept) + "</p>")
        if budget <= 0:
            break
    body = '<break strength="medium"/>'.join(paragraphs)
    return f'<speak><prosody rate="{rate}">{body}</prosody></speak>'


def validate_ssml(ssml: str) -> tuple[bool, str]:
    try:
        root = ET.fromstring(ssml)
    except ET.ParseError as exc:
        return False, f"not well-formed XML: {exc}"
    if root.tag != "speak":
        return False, "root element must be <speak>"
    total_text = 0
    for element in root.iter():
        if element.tag not in ALLOWED_TAGS:
            return False, f"element <{element.tag}> is not in the supported SSML subset"
        attrs = dict(element.attrib)
        if element.tag == "prosody" and attrs.get("rate") not in RATES:
            return False, "prosody rate must be one of " + ", ".join(RATES)
        if element.tag == "break" and not (_TIME.match(attrs.get("time", "")) or attrs.get("strength") in STRENGTHS):
            return False, "break needs a time such as 300ms or a valid strength"
        if element.tag == "emphasis" and attrs.get("level", "moderate") not in EMPHASIS_LEVELS:
            return False, "emphasis level is not valid"
        if element.tag not in ("prosody", "break", "emphasis") and attrs:
            return False, f"<{element.tag}> takes no attributes here"
        for piece in (element.text, element.tail):
            if piece:
                total_text += len(piece)
                if _SYMBOL_CHECK.search(piece):
                    return False, "a markdown or HTML symbol is left in the speech text"
    if total_text > MAX_SPEECH_CHARS * 1.2:
        return False, "speech text is longer than the limit"
    return True, "valid SSML subset (speak, p, s, prosody, emphasis, break)"
