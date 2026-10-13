"""Agent 11 helpers (Phase 3): Hinglish and Devanagari handling, technical-term protection, word-level gloss.

- roman_to_deva(word): Roman to Devanagari. Common Hinglish words come from a curated lexicon (exact). Other words use a
  rule-based transliterator (consonant plus matra, halant for clusters, a word-final consonant with no inherent vowel,
  a word-final 'i' as ी). The rule path is approximate: schwa deletion and aspiration are guessed. It reports 'rule'.
- segment(text): tags tokens as deva, hi (Hinglish from the lexicon), tech (protected term), en, num or punct.
- gloss(text, transliterate): a word-level English gloss. It is not a translation of meaning.
- lost_terms(source, output): protected terms from the source that are missing from the output.
"""
from __future__ import annotations

import re

LEXICON = {
    "naam": "नाम", "kya": "क्या", "hai": "है", "hain": "हैं", "mera": "मेरा", "meri": "मेरी", "mere": "मेरे",
    "aur": "और", "kitna": "कितना", "kitni": "कितनी", "nahi": "नहीं", "karo": "करो", "karna": "करना",
    "batao": "बताओ", "kaise": "कैसे", "kahan": "कहाँ", "kab": "कब", "kyun": "क्यों", "ghar": "घर",
    "paani": "पानी", "bhi": "भी", "accha": "अच्छा", "achha": "अच्छा", "theek": "ठीक", "samjhao": "समझाओ",
    "likho": "लिखो", "bolo": "बोलो", "dekho": "देखो", "chahiye": "चाहिए", "ko": "को", "ka": "का", "ki": "की",
    "ke": "के", "se": "से", "me": "में", "par": "पर", "main": "मैं", "mujhe": "मुझे", "tum": "तुम", "aap": "आप",
    "yeh": "यह", "woh": "वह", "kaun": "कौन", "bahut": "बहुत", "sab": "सब", "kuch": "कुछ", "ek": "एक", "do": "दो",
    "teen": "तीन", "abhi": "अभी",
}
GLOSS = {
    "naam": "name", "kya": "what", "hai": "is", "hain": "are", "mera": "my", "meri": "my", "mere": "my", "aur": "and",
    "kitna": "how much", "kitni": "how many", "nahi": "not", "karo": "do", "karna": "to do", "batao": "tell",
    "kaise": "how", "kahan": "where", "kab": "when", "kyun": "why", "ghar": "home", "paani": "water", "bhi": "also",
    "accha": "good", "achha": "good", "theek": "fine", "samjhao": "explain", "likho": "write", "bolo": "say",
    "dekho": "see", "chahiye": "need", "ko": "to", "ka": "of", "ki": "of", "ke": "of", "se": "from", "me": "in",
    "par": "on", "main": "I", "mujhe": "me", "tum": "you", "aap": "you", "yeh": "this", "woh": "that", "kaun": "who",
    "bahut": "very", "sab": "all", "kuch": "some", "ek": "one", "do": "two", "teen": "three", "abhi": "now",
}
TECH_TERMS = frozenset({
    "api", "apis", "database", "db", "function", "variable", "python", "javascript", "typescript", "json", "sql",
    "http", "https", "server", "client", "class", "object", "array", "string", "integer", "float", "boolean", "loop",
    "query", "endpoint", "token", "model", "cache", "compiler", "bug", "debug", "deploy", "module", "library",
    "framework", "package", "error", "exception", "file", "folder", "git", "docker", "linux", "windows", "browser",
    "rust", "c++", "cpp", "java", "html", "css", "csv", "regex", "thread", "process", "memory", "cpu", "gpu",
    "kubernetes", "terminal", "algorithm", "parameter", "return", "import",
})
_TOKEN = re.compile(r"[\u0900-\u097F]+|[A-Za-z][A-Za-z+#]*|\d+(?:\.\d+)?|[^\sA-Za-z\u0900-\u097F\d]")
_DEVA = re.compile(r"[\u0900-\u097F]")
_DEVA_DIGITS = re.compile(r"[\u0966-\u096F]+")  # Devanagari digits (RC-02)
# Devanagari spellings of protected technical terms, kept in Roman script in every output (RC-02).
DEVA_TECH = {"एपीआई": "API", "डेटाबेस": "database", "फंक्शन": "function", "वेरिएबल": "variable",
             "पायथन": "Python", "जेसन": "JSON", "एसक्यूएल": "SQL", "सर्वर": "server", "क्लास": "class",
             "लूप": "loop", "कैश": "cache", "टोकन": "token", "एरे": "array", "स्ट्रिंग": "string",
             "मॉडल": "model", "फाइल": "file", "एरर": "error", "बग": "bug"}
_CONSONANTS = sorted([
    ("chh", "छ"), ("kh", "ख"), ("gh", "घ"), ("ch", "च"), ("jh", "झ"), ("th", "थ"), ("dh", "ध"), ("ph", "फ"),
    ("bh", "भ"), ("sh", "श"), ("k", "क"), ("g", "ग"), ("j", "ज"), ("t", "त"), ("d", "द"), ("n", "न"), ("p", "प"),
    ("b", "ब"), ("m", "म"), ("y", "य"), ("r", "र"), ("l", "ल"), ("v", "व"), ("w", "व"), ("s", "स"), ("h", "ह"),
    ("f", "फ"), ("z", "ज"),
], key=lambda kv: -len(kv[0]))
_MATRAS = [("aa", "ा"), ("ee", "ी"), ("ii", "ी"), ("oo", "ू"), ("uu", "ू"), ("ai", "ै"), ("au", "ौ"), ("a", ""),
           ("i", "ि"), ("u", "ु"), ("e", "े"), ("o", "ो")]
_INDEPENDENT = [("aa", "आ"), ("ee", "ई"), ("ii", "ई"), ("oo", "ऊ"), ("uu", "ऊ"), ("ai", "ऐ"), ("au", "औ"),
                ("a", "अ"), ("i", "इ"), ("u", "उ"), ("e", "ए"), ("o", "ओ")]


def _vowel_at(text: str, index: int, table: list) -> tuple[str, str] | None:
    for key, mark in table:
        if text.startswith(key, index):
            return key, mark
    return None


def roman_to_deva(word: str) -> tuple[str, str]:
    low = word.lower()
    if low in TECH_TERMS:
        return word, "protected"  # RC-02: a technical term is never transliterated
    if low in LEXICON:
        return LEXICON[low], "lexicon"
    if not re.fullmatch(r"[a-z]+", low):
        return word, "unchanged"
    out: list[str] = []
    i, n = 0, len(low)
    while i < n:
        consumed = False
        for roman, deva in _CONSONANTS:
            if low.startswith(roman, i):
                i += len(roman)
                vowel = _vowel_at(low, i, _MATRAS)
                if vowel is not None:
                    key, mark = vowel
                    if key == "i" and i + len(key) == n:
                        mark = "ी"
                    out.append(deva + mark)
                    i += len(key)
                elif i >= n:
                    out.append(deva)
                else:
                    out.append(deva + "्")
                consumed = True
                break
        if consumed:
            continue
        vowel = _vowel_at(low, i, _INDEPENDENT)
        if vowel is not None:
            out.append(vowel[1])
            i += len(vowel[0])
            continue
        out.append(low[i])
        i += 1
    return "".join(out), "rule"


AMBIGUOUS = frozenset({"me", "do"})  # Hinglish words that are also English; decided by sentence context


def segment(text: str) -> list[tuple[str, str]]:
    """Tags every token. A protected technical term is always 'tech' and stays in Roman script. An ambiguous word
    (me, do) is Hinglish only if the sentence contains a clearly Hinglish word (RC-02: 'send me the file' stays English)."""
    words = [match.group(0) for match in _TOKEN.finditer(text or "")]
    clearly_hinglish = any(w.lower() in LEXICON and w.lower() not in AMBIGUOUS and w.lower() not in TECH_TERMS
                           for w in words)
    tokens = []
    for tok in words:
        low = tok.lower()
        if tok in DEVA_TECH:
            tokens.append((DEVA_TECH[tok], "tech"))  # RC-02: Devanagari term -> Roman term
            continue
        if _DEVA_DIGITS.fullmatch(tok):
            tokens.append((tok, "num"))  # RC-02: Devanagari digits are numbers
            continue
        if _DEVA.search(tok):
            tag = "deva"
        elif tok[0].isdigit():
            tag = "num"
        elif not tok[0].isalpha():
            tag = "punct"
        elif low in TECH_TERMS:
            tag = "tech"
        elif low in LEXICON:
            tag = "hi" if (low not in AMBIGUOUS or clearly_hinglish) else "en"
        else:
            tag = "en"
        tokens.append((tok, tag))
    return tokens


def gloss(text: str, transliterate) -> tuple[str, list[str]]:
    parts: list[str] = []
    unknown: list[str] = []
    for tok, tag in segment(text):
        if tag == "deva":
            parts.append(transliterate(tok))
        elif tag == "hi":
            meaning = GLOSS.get(tok.lower())
            if meaning is None:
                unknown.append(tok)
                parts.append(tok + "?")
            else:
                parts.append(meaning)
        else:
            parts.append(tok)
    out = re.sub(r"\s+([,.!?;:])", r"\1", " ".join(parts))
    return out, unknown


def technical_terms(text: str) -> list[str]:
    seen: dict[str, str] = {}
    for tok, tag in segment(text):
        if tag == "tech":
            seen.setdefault(tok.lower(), tok)
    return list(seen.values())  # first-appearance order: the warning lists terms as the user wrote them (RC-02)


def lost_terms(source: str, output: str) -> list[str]:
    lowered = (output or "").lower()
    return [t for t in technical_terms(source) if t.lower() not in lowered]
