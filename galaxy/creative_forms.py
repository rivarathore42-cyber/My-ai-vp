"""Agent 10 (Phase 2): structured templates, structural validators and continuation for creative writing.

Validators are deterministic: they count syllables, lines, stanzas and paragraphs, and compare line-end rhymes by a
suffix rule. They judge structure, not literary quality. Syllable counts are the agents_text heuristic (approximate).
"""
from __future__ import annotations

import re

from .prompts_base import GenParams

MAX_CONTINUATIONS = 2

FORM_RULES = {
    "haiku": {"instruction": "Write exactly three lines with 5, 7 and 5 syllables. No title, no explanation."},
    "limerick": {"instruction": "Write exactly five lines with the rhyme scheme AABBA. Lines 1, 2 and 5 are long; lines 3 and 4 are short. No title."},
    "sonnet": {"instruction": "Write fourteen lines of rhymed iambic pentameter: three quatrains and a final couplet. No title."},
    "poem": {"instruction": "Write two to four stanzas of four lines each, with a blank line between stanzas. No title line."},
    "song lyrics": {"instruction": "Write two verses and a chorus. Label them [Verse 1], [Chorus], [Verse 2] on their own lines. No explanation."},
    "essay": {"instruction": "Write a title on the first line, then three to five paragraphs: introduction, body, conclusion. Blank line between paragraphs."},
    "story": {"instruction": "Write a short story of three to five paragraphs with a clear beginning, middle and end. Blank line between paragraphs. No title line."},
}


def _syllables(line: str) -> int:
    from .agents_text import line_syllables  # imported here to keep the module graph acyclic
    return line_syllables(line)


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def rhyme_tail(line: str) -> str:
    words = re.findall(r"[A-Za-z']+", line.lower())
    if not words:
        return ""
    match = re.search(r"[aeiouy]+[^aeiouy]*$", words[-1])
    return match.group(0) if match else words[-1][-2:]


def check_haiku(text: str) -> dict:
    lines = _lines(text)
    counts = [_syllables(ln) for ln in lines]
    problems = []
    if len(lines) != 3:
        problems.append(f"haiku needs 3 lines, found {len(lines)}")
    elif counts != [5, 7, 5]:
        problems.append(f"syllables per line are {counts}, target [5, 7, 5]")
    return {"ok": not problems, "problems": problems, "counts": counts}


def check_limerick(text: str) -> dict:
    lines = _lines(text)
    problems = []
    if len(lines) != 5:
        problems.append(f"limerick needs 5 lines, found {len(lines)}")
    else:
        tails = [rhyme_tail(ln) for ln in lines]
        if not (tails[0] == tails[1] == tails[4] and tails[2] == tails[3] and tails[0] != tails[2]):
            problems.append("rhyme scheme is not AABBA")
    return {"ok": not problems, "problems": problems}


def check_sonnet(text: str) -> dict:
    lines = _lines(text)
    problems = [] if len(lines) == 14 else [f"sonnet needs 14 lines, found {len(lines)}"]
    return {"ok": not problems, "problems": problems}


def check_poem(text: str) -> dict:
    stanzas = _paragraphs(text)
    problems = []
    if not stanzas:
        problems.append("no stanzas found")
    elif any(len(_lines(s)) < 2 for s in stanzas):
        problems.append("a stanza has fewer than two lines")
    return {"ok": not problems, "problems": problems}


def check_lyrics(text: str) -> dict:
    blocks = _paragraphs(text)
    labelled = bool(re.search(r"\[(?:Verse|Chorus)", text))
    problems = [] if labelled or len(blocks) >= 2 else ["lyrics need labelled sections or at least two stanzas"]
    return {"ok": not problems, "problems": problems}


def check_prose(text: str, minimum: int) -> dict:
    paragraphs = _paragraphs(text)
    problems = [] if len(paragraphs) >= minimum else [f"needs at least {minimum} paragraphs, found {len(paragraphs)}"]
    return {"ok": not problems, "problems": problems}


_CHECKS = {"haiku": check_haiku, "limerick": check_limerick, "sonnet": check_sonnet, "poem": check_poem,
           "song lyrics": check_lyrics, "essay": lambda t: check_prose(t, 3), "story": lambda t: check_prose(t, 3)}


def validate(form: str, text: str) -> dict:
    checker = _CHECKS.get(form)
    if checker is None:
        return {"form": form, "ok": True, "problems": [], "checked": False}
    result = checker(text)
    return {"form": form, "checked": True, **result}


def continuation_prompt(form: str, so_far: str) -> tuple[str, str, GenParams]:
    system = (
        "ROLE: You continue a piece of writing exactly where it stopped.\n"
        f"TASK: Continue the {form} below from its last word. Do not repeat any of it. Keep the same form, language, "
        "voice and rhyme scheme, and bring the piece to a proper ending.\n"
        "LENGTH: At most 300 words.\n"
        "REASONING: Answer directly. Do not write <think> tags.\n"
        "FORMAT: Plain text only, no preface."
    )
    return system, so_far[-1500:], GenParams(max_tokens=512, temperature=0.7)
