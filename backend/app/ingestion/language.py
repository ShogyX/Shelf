"""Language detection + canonicalization for releases and downloaded files.

Two jobs, both dependency-free:
  * detect_languages(release_name) — parse the language(s) a usenet release declares, ported from
    Radarr/Sonarr's LanguageParser (full-name substrings + case-insensitive token regex + a
    case-sensitive 2-letter-code pass with SUB/codec guards). Multi-language is first-class.
  * canonicalize(raw) — fold any 2-letter / 3-letter (incl. ISO-639-2 B/T doublets like ger→de,
    fre→fr) / English-name token to a canonical ISO-639-1 code (Readarr's CanonicalizeLanguage).
  * detect_text_language(text) — a stop-word-frequency fallback to verify a downloaded file's
    actual language when its embedded metadata doesn't declare one.
"""
from __future__ import annotations

import re

# Canonical 2-letter codes we recognize.
_VALID2 = {
    "en", "de", "fr", "es", "it", "pt", "nl", "ru", "ja", "zh", "ko", "pl", "sv", "no", "da",
    "fi", "cs", "sk", "hu", "ro", "tr", "ar", "he", "hi", "el", "uk", "vi", "th", "id", "ca",
}

# ISO-639-2 (3-letter), including bibliographic↔terminological doublets (ger/deu, fre/fra, …).
_THREE_TO_TWO = {
    "eng": "en", "ger": "de", "deu": "de", "fre": "fr", "fra": "fr", "spa": "es", "ita": "it",
    "por": "pt", "dut": "nl", "nld": "nl", "rus": "ru", "jpn": "ja", "chi": "zh", "zho": "zh",
    "kor": "ko", "pol": "pl", "swe": "sv", "nor": "no", "dan": "da", "fin": "fi", "cze": "cs",
    "ces": "cs", "slo": "sk", "slk": "sk", "hun": "hu", "rum": "ro", "ron": "ro", "tur": "tr",
    "ara": "ar", "heb": "he", "hin": "hi", "gre": "el", "ell": "el", "ukr": "uk", "vie": "vi",
    "tha": "th", "ind": "id", "cat": "ca",
}

_NAME_TO_TWO = {
    "english": "en", "german": "de", "deutsch": "de", "french": "fr", "francais": "fr",
    "français": "fr", "spanish": "es", "espanol": "es", "español": "es", "castellano": "es",
    "latino": "es", "italian": "it", "italiano": "it", "portuguese": "pt", "portugues": "pt",
    "português": "pt", "brazilian": "pt", "dutch": "nl", "nederlands": "nl", "russian": "ru",
    "japanese": "ja", "chinese": "zh", "mandarin": "zh", "korean": "ko", "polish": "pl",
    "swedish": "sv", "norwegian": "no", "danish": "da", "finnish": "fi", "czech": "cs",
    "slovak": "sk", "hungarian": "hu", "romanian": "ro", "turkish": "tr", "arabic": "ar",
    "hebrew": "he", "hindi": "hi", "greek": "el", "ukrainian": "uk", "vietnamese": "vi",
    "thai": "th", "indonesian": "id", "catalan": "ca",
}


def canonicalize(raw: str | None) -> str | None:
    """Fold any language token to a canonical ISO-639-1 code, or None."""
    if not raw:
        return None
    s = str(raw).strip().lower().replace("_", "-").split("-")[0].strip()
    if not s:
        return None
    if len(s) == 2:
        return s if s in _VALID2 else None
    if len(s) == 3:
        return _THREE_TO_TWO.get(s)
    return _NAME_TO_TWO.get(s)


# Pass B — case-insensitive token regex, one named group per language. Ported/condensed from
# Radarr's LanguageRegex; the alternatives include 3-letter codes and English names.
_TOKEN_RE = re.compile(
    r"\b(?P<en>eng|english)\b"
    r"|\b(?P<de>ger|deu|german|deutsch)\b"
    r"|\b(?P<fr>fre|fra|french|francais|truefrench|vff|vostfr)\b"
    r"|\b(?P<es>spa|spanish|espanol|castellano|latino)\b"
    r"|\b(?P<it>ita|italian|italiano)\b"
    r"|\b(?P<pt>por|portuguese|portugues|dublado|brazilian)\b"
    r"|\b(?P<nl>dut|nld|dutch|nederlands)\b"
    r"|\b(?P<ru>rus|russian)\b"
    r"|\b(?P<ja>jpn|jap|japanese)\b"
    r"|\b(?P<zh>zho|chinese|mandarin)\b"
    r"|\b(?P<ko>kor|korean)\b"
    r"|\b(?P<pl>pol|polish)\b"
    r"|\b(?P<sv>swe|swedish)\b"
    r"|\b(?P<no>nor|norwegian)\b"
    r"|\b(?P<da>dan|danish)\b"
    r"|\b(?P<fi>fin|finnish)\b"
    r"|\b(?P<cs>cze|ces|czech)\b"
    r"|\b(?P<hu>hun|hungarian)\b"
    r"|\b(?P<tr>tur|turkish)\b"
    r"|\b(?P<ar>ara|arabic)\b"
    r"|\b(?P<el>gre|ell|greek)\b"
    r"|\b(?P<uk>ukr|ukrainian)\b",
    re.IGNORECASE,
)

# Pass C — case-SENSITIVE 2-letter codes (only meaningful uppercase). Guard against a subtitle tag
# (ES.SUB / SUB.ES) and the audio codec DTS-ES being read as a spoken language.
_CODE_RE = re.compile(
    r"(?<!SUB[\W_])(?<!SUB)"
    r"(?:(?P<en>\bEN\b)|(?P<de>\bDE\b)|(?P<fr>\bFR\b)|(?P<it>\bIT\b)|(?P<ru>\bRU\b)"
    r"|(?P<pl>\bPL\b)|(?P<nl>\bNL\b)|(?P<pt>\bPT\b)|(?P<sv>\bSE\b)|(?P<cs>\bCZ\b)"
    r"|(?P<es>(?<!DTS[._ -])\bES\b))"
    r"(?![\W_]SUB)"
)

_MULTI_RE = re.compile(r"[\b._\- ]multi[\b._\- ]", re.IGNORECASE)


def _matches(name: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for m in _TOKEN_RE.finditer(name):
        code = m.lastgroup
        if code:
            out.append((m.start(), code))
    for m in _CODE_RE.finditer(name):
        code = m.lastgroup
        if code:
            out.append((m.start(), code))
    return out


def detect_languages(name: str | None) -> set[str]:
    """All languages a release name declares (ISO-639-1 codes). Empty when none are stated."""
    return {c for _p, c in _matches(name or "")}


def primary_language(name: str | None) -> str | None:
    """The single best language for a release — the LAST-occurring tag, since language/format
    metadata trails the title (so a title word like 'The German Wife' doesn't override a real
    trailing '…German' tag). None when nothing is declared."""
    ms = _matches(name or "")
    if not ms:
        return None
    return max(ms, key=lambda x: x[0])[1]


def is_multi_language(name: str | None) -> bool:
    return bool(_MULTI_RE.search(name or "")) or len(detect_languages(name)) > 1


# --------------------------------------------------------------- content-based fallback
# Tiny high-frequency stop-word sets — enough to tell major languages apart when a file declares
# no language. Deliberately disjoint, common words.
_STOPWORDS: dict[str, set[str]] = {
    "en": {"the", "and", "of", "to", "in", "that", "was", "his", "with", "had", "for", "she"},
    "de": {"der", "die", "und", "den", "das", "ist", "nicht", "ein", "eine", "mit", "sich", "auch"},
    "fr": {"le", "la", "les", "des", "une", "que", "qui", "dans", "pas", "pour", "avec", "est"},
    "es": {"que", "los", "las", "una", "por", "con", "para", "como", "más", "pero", "del", "está"},
    "it": {"che", "non", "una", "per", "con", "gli", "del", "come", "sono", "questo", "alla", "più"},
    "pt": {"que", "não", "uma", "para", "com", "dos", "como", "mais", "por", "está", "ele", "seu"},
    "nl": {"het", "een", "van", "dat", "niet", "zijn", "met", "voor", "maar", "aan", "ook", "naar"},
    "ru": {"что", "как", "это", "она", "был", "его", "они", "так", "все", "там", "уже", "нет"},
}


def detect_text_language(text: str | None, *, min_tokens: int = 40) -> str | None:
    """Best-guess language of a body of text by stop-word frequency. Returns a code only when one
    language clearly dominates; None when the sample is too small or ambiguous (so an uncertain
    guess never causes a false rejection)."""
    if not text:
        return None
    toks = re.findall(r"[^\W\d_]+", text.lower(), re.UNICODE)
    if len(toks) < min_tokens:
        return None
    counts = {lang: sum(1 for t in toks if t in sw) for lang, sw in _STOPWORDS.items()}
    best = max(counts, key=counts.get)
    top = counts[best]
    if top < 3:
        return None
    runner = max((v for k, v in counts.items() if k != best), default=0)
    return best if top >= runner * 1.5 else None
