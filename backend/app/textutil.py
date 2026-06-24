"""Plain-text normalization for book DESCRIPTIONS / synopses (not reader chapter content).

Sources hand us descriptions as HTML (``<p>``/``<br>``/``<i>``), entity-escaped text, or light
markdown (``**bold**``, ``[text](url)``, ``> quote``). The UI renders descriptions as PLAIN TEXT
(``whitespace-pre-line``), so any markup shows up literally (e.g. "Library of Heaven's Path" showing
raw ``<p>`` tags). ``clean_synopsis`` converts markup to readable plain text while KEEPING paragraph
breaks and WITHOUT mangling a lone ``*``/``_`` (e.g. a ``4*`` rating, a ``file_name``).

Pure stdlib + no app imports, so ``models.py`` can use it in a ``@validates`` hook without a cycle.
"""
from __future__ import annotations

import html
import re

_BR_RE = re.compile(r"(?i)<\s*br\s*/?\s*>")
_BLOCK_END_RE = re.compile(r"(?i)</\s*(?:p|div|li|h[1-6]|blockquote|ul|ol|tr)\s*>")
_TAG_RE = re.compile(r"<[^>]+>")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?:)?[^)\s]*\)")          # [text](url) -> text
_MD_CODE_RE = re.compile(r"`([^`]+)`")                                    # `code` -> code
# **bold**/__bold__ -> bold. The body `(?:(?!\1).)+?` forbids the delimiter inside, so the match is
# linear (no catastrophic backtracking on unbalanced "**a **a …" — a third-party DoS vector).
_MD_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)((?:(?!\1).)+?)(?<=\S)\1", re.S)
# italic: a single * or _ hugging non-space text and NOT adjacent to a word char, so "4*"/"a_b"/"5*6"
# survive untouched. Same non-backtracking body.
_MD_ITAL_RE = re.compile(r"(?<![\w*_])([*_])(?=\S)((?:(?!\1).)+?)(?<=\S)\1(?![\w*_])", re.S)
_MAX_LEN = 8000  # cap before regex work (matches the IndexedPage.description bound) — DoS belt
_HR_RE = re.compile(r"(?m)^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$")         # markdown --- / *** rule


def clean_synopsis(text: str | None) -> str | None:
    """Normalize a description/synopsis to clean plain text. Returns None when empty after cleaning."""
    if not text:
        return None
    s = str(text)[:_MAX_LEN]
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_END_RE.sub("\n", s)          # block-level closes -> newline (keep paragraphs)
    s = _TAG_RE.sub("", s)                   # remaining tags
    s = html.unescape(s)                     # &amp; &#39; &quot; &lt; …
    s = _MD_LINK_RE.sub(r"\1", s)
    s = _MD_CODE_RE.sub(r"\1", s)
    for _ in range(2):                       # twice for nested **_x_**
        s = _MD_BOLD_RE.sub(r"\2", s)
        s = _MD_ITAL_RE.sub(r"\2", s)
    s = _HR_RE.sub("", s)
    s = re.sub(r"(?m)^[ \t]{0,3}>[ \t]?", "", s)   # leading blockquote markers
    s = re.sub(r"[ \t]+", " ", s)                  # collapse runs of spaces/tabs
    s = re.sub(r"[ \t]*\n", "\n", s)               # trim trailing space before newlines
    s = re.sub(r"\n{3,}", "\n\n", s)               # cap blank-line runs
    return s.strip() or None


def demo() -> None:
    """Runnable self-check (`python -m app.textutil`)."""
    assert clean_synopsis("<p>A boy <i>finds</i> a library.</p><p>Then chaos.</p>") \
        == "A boy finds a library.\nThen chaos."
    assert clean_synopsis("Line one<br>Line two") == "Line one\nLine two"
    assert clean_synopsis("Tom &amp; Jerry &#39;quoted&#39;") == "Tom & Jerry 'quoted'"
    assert clean_synopsis("A **bold** and _italic_ tale") == "A bold and italic tale"
    assert clean_synopsis("See [the wiki](https://x.io/a) for more") == "See the wiki for more"
    # NEGATIVE cases — a lone * / _ must SURVIVE (ratings, filenames, math):
    assert clean_synopsis("Rated 4* and 5* stars") == "Rated 4* and 5* stars"
    assert clean_synopsis("the file_name_here is fine") == "the file_name_here is fine"
    assert clean_synopsis("2 * 3 = 6") == "2 * 3 = 6"
    assert clean_synopsis("") is None
    assert clean_synopsis(None) is None
    assert clean_synopsis("<p></p>") is None
    print("textutil.clean_synopsis OK")


if __name__ == "__main__":
    demo()
