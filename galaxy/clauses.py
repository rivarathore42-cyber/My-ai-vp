"""Agent 1 - Master Orchestrator (Phase 1): clause-aware decomposition and high-precision intent routing.

Splitting. A separator (and, aur, and then, then, also, plus, or a semicolon) splits a prompt only when the clause on
its right is an INDEPENDENT command. Explanatory or modifying clauses stay attached to the sentence before them:
  "solve step by step and explain why ..."   -> 1 section   ('explain why' modifies the request)
  "compare Python and Rust for web servers"  -> 1 section   ('Rust ...' is not a clause)
  "write a poem and make it rhyme"           -> 1 section   ('make it' modifies the request)
  "who was X and what is 15% of 200"         -> 2 sections  ('what is ...' is an independent question)
  "explain photosynthesis; calculate 2^8"    -> 2 sections  (a semicolon always separates)
An independent clause starts with a command verb, or with a question word and at least three words, or with a number
followed by a command verb within the first four words.

Routing. Ordered rules, most specific first; the first match wins and the fallback is 'knowledge'. The order encodes
the precedence: 'write a function that computes ...' is code, while 'calculate 144/12' is math.
Scope: this is a rule-based router. Its precision is measured on the corpus in tests/test_v6.py (section M), not claimed
for all inputs.
"""
from __future__ import annotations

import re

COMMAND_VERBS = frozenset({
    "write", "search", "find", "calculate", "compute", "solve", "translate", "summarize", "summarise", "create", "make",
    "run", "check", "fix", "debug", "list", "generate", "read", "open", "scan", "convert", "save", "remember", "recall",
    "tell", "give", "show", "sum", "sort", "mean", "stddev", "trace", "primes", "speak", "say", "likho", "batao",
    "dhundo", "bolo", "samjhao", "wiki", "note", "forget", "evaluate", "simplify",
})
WH_WORDS = frozenset({"what", "who", "when", "where", "which", "whose", "how",
                     # Hinglish question and self-reference starters (Phase 4 fix: "and mera naam kya hai")
                     "kya", "kitna", "kitni", "kaise", "kab", "kahan", "kaun", "kyun", "mera", "meri", "mujhe"})
MODIFIER_RE = re.compile(
    r"^(?:(?:explain|describe|justify|elaborate|clarify|discuss)\s+(?:why|how)\b|show\s+(?:why|how)\b|why\b|"
    r"how\b(?!\s+(?:many|much))|make\s+it\b|keep\s+it\b|use\s+it\b|in\s+(?:short|brief|detail)\b|briefly\b|"
    r"step\s+by\s+step\b|with\s+(?:reasons?|examples?)\b|is\b|are\b|was\b|were\b|do\b|does\b|did\b|can\b|should\b|"
    r"would\b|will\b|so\b|because\b|and\b|or\b)",
    re.I)
_SEP = re.compile(r"\s*;\s*|\s*,?\s+(?:and\s+then|and\s+also|and|aur|then|also|plus)\s+|\s+(?:और|फिर)\s+", re.I)


def _independent(clause: str) -> bool:
    text = clause.strip()
    words = re.findall(r"[\w'/.+%-]+", text.lower())
    if len(words) < 2 or MODIFIER_RE.match(text):
        return False
    first = words[0]
    if first in COMMAND_VERBS:
        return True
    if first in WH_WORDS and len(words) >= 3:
        return True
    return bool(re.match(r"[\d(]", text)) and any(w in COMMAND_VERBS for w in words[:4])


def split_clauses(text: str, max_sections: int = 8) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    clauses: list[str] = []
    start = 0
    for match in _SEP.finditer(cleaned):
        left = cleaned[start:match.start()]
        if not left.strip(" ,."):
            continue
        rest = cleaned[match.end():]
        following = _SEP.search(rest)
        right = rest[:following.start()] if following else rest
        if match.group(0).strip() == ";" or _independent(right):
            clauses.append(left.strip(" ,."))
            start = match.end()
    tail = cleaned[start:].strip(" ,.")
    if tail:
        clauses.append(tail)
    clauses = [c for c in clauses if c]
    if len(clauses) > max_sections:
        clauses = clauses[:max_sections - 1] + [" ".join(clauses[max_sections - 1:])]
    return clauses or [cleaned]


RULES: list[tuple[str, re.Pattern]] = [
    ("security", re.compile(r"\b(?:ignore|disregard|forget|skip|override|bypass)\s+(?:all\s+|any\s+|every\s+|your\s+|the\s+|my\s+)?"
                            r"(?:previous|prior|above|earlier|original|system|initial|preceding)\s+"
                            r"(?:instructions?|prompts?|rules?|directions?|guidelines?|messages?|context)\b|\bjailbreak\b|\bdan\s+mode\b", re.I)),
    ("memory", re.compile(r"^\s*(?:note|document|remember this)\s*:|\bmy\s+notes\b|\bsearch\s+my\s+notes\b|\bfind\s+in\s+my\s+notes\b|"
                          r"\bforget\s+(?:note|doc|document)\b|\bwhat\s+did\s+i\s+note\b|\bmy\s+name\s+is\b|"
                          r"\bwhat(?:'s|\s+is)\s+my\s+name\b|\bmera\s+naam\b|\bi\s+(?:like|love|prefer)\b|\bmy\s+history\b|"
                          r"\bchat\s+history\b|\brecent\s+messages\b|\bremember\s+(?:that|this)\b|\bmy\s+(?:goal|tech\s+stack|language|timezone)\s+is\b|\bshow\s+my\s+profile\b|\bsearch\s+my\s+chats\b", re.I)),
    ("data", re.compile(r"```\s*(?:csv|json|tsv|markdown|table)\b", re.I)),
    ("code", re.compile(r"```|\b(?:write|create|implement|generate|make|build|debug|fix|refactor|review|explain)\b[^.?!\n]{0,40}"
                        r"\b(?:function|program|script|class|method|algorithm|snippet|module|code)\b|"
                        r"\b(?:python|javascript|js|rust|c\+\+|java|golang)\b[^.?!\n]{0,20}"
                        r"\b(?:code|script|program|function|example|snippet)\b|\bsyntax\s+error\b|\btraceback\b", re.I)),
    ("native", re.compile(r"^\s*(?:sum|mean|stddev|sort|trace|matrix_trace)\b\s*:?\s*\[?[-\d]|^\s*primes?\b(?:\s+(?:upto|up\s+to|until|below|under))?\s*:?\s*\d", re.I)),
    ("math", re.compile(r"\b(?:calculate|compute|solve|evaluate|simplify|factorial|sqrt|cbrt|log|logarithm|sin|cos|tan|"
                        r"permutations?|combinations?|choose|percent|percentage|gcd|lcm)\b|\d\s*[*/^×÷+]\s*\d|\d\s+-\s+\d|"
                        r"\d\s*%\s*of\s*\d|\d!|\b\d+\s*[CP]\s*\d+\b|\d+(?:\.\d+)?\s+(?:times|multiplied\s+by|divided\s+by|plus|minus|over|mod|modulo|to\s+the\s+power\s+of|raised\s+to)\s+\d+", re.I)),
    ("data", re.compile(r"\b(?:csv|json|dataset|data\s+analysis|statistics|statistical|average\s+of|mean\s+of|median|"
                        r"std\s*dev|table)\b", re.I)),
    ("translate", re.compile(r"\b(?:translate|translation|translated)\b|\bin\s+(?:hindi|english)\b|\b(?:hindi|english)\s+me\b", re.I)),
    ("creative", re.compile(r"\b(?:haiku|5-7-5|poem|poetry|sonnet|limerick|story|essay|lyrics?|kavita|shayari|kahani|nibandh|geet)\b", re.I)),
    ("search", re.compile(r"\b(?:search|latest|news|today|current|recent|weather|price|look\s*up|google|find\s+out|score\s+of|stock)\b", re.I)),
    ("speech", re.compile(r"\b(?:speak|read\s+aloud|text\s+to\s+speech|tts|voice|ssml)\b", re.I)),
    ("vision", re.compile(r"\b(?:image|picture|photo|screenshot|ocr)\b|\.(?:png|jpe?g|gif|webp)\b", re.I)),
    ("wiki", re.compile(r"^\s*wiki\b|\bwikipedia\b", re.I)),
]


def classify_intent(task: str) -> str:
    for intent, pattern in RULES:
        if pattern.search(task):
            return intent
    return "knowledge"


class QueryDecomposer:
    def __init__(self, max_sections: int = 8) -> None:
        self.max_sections = max_sections

    def split(self, text: str) -> list[str]:
        return split_clauses(text, self.max_sections)

    def classify(self, task: str) -> str:
        return classify_intent(task)
