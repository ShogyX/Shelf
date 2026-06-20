"""Reader text cleanup for badly-scraped chapter HTML.

Some web-novel sources serve prose as thousands of tiny ``<span>`` fragments (with newline "spacer"
spans) and censor certain substrings letter-by-letter — e.g. *Shiro* → ``s.h.i.+ro``, *washing* →
``was.h.i.+ng``, *relationship* → ``relations.h.i.+p``. Rendered, that collapses into a wall of text
strewn with dotted garble. :func:`clean_chapter_html` de-garbles the censorship and reflows the
fragments into readable ``<p>`` paragraphs. It is conservative: if it can't produce paragraphs it
returns the input unchanged (never blanks out a chapter).
"""
from __future__ import annotations

import re
from html import escape

from bs4 import BeautifulSoup

# Promo/junk the source injects (anchors + stray header lines).
_JUNK_SUBSTR = ("play.google.com", "apps.apple.com", "novelaistudio", "novellunar.com")
_DATE_RE = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}$")  # e.g. "Mar 18, 2026"

# Letter-by-letter censorship marker: a token carrying a ``.+`` joiner, e.g. ``s.h.i.+ro``. The
# ``.+`` pair never occurs glued inside real prose, so keying on it can't corrupt normal text.
_CENSOR_RE = re.compile(r"[A-Za-z]+(?:\.[A-Za-z]+)*\.\+[A-Za-z]+")

# Sentence boundary: end punctuation, then whitespace, then an opener (capital / quote / em-dash /
# footnote bracket). Used to re-paragraph reflowed narration.
_SENT_SPLIT = re.compile(r'(?<=[.!?…])\s+(?=["“A-Z—\[])')

# Leading heading lines (the source repeats the title / volume / part above the body): kept as <h3>.
_HEADING_RE = re.compile(
    r"^(?:Vol(?:ume)?\s+\d+\s+)?(?:Chapter|Part|Prologue|Epilogue|Interlude|Afterword|Side\s+Story)\b",
    re.I)


def _deobfuscate(text: str) -> str:
    return _CENSOR_RE.sub(lambda m: m.group().replace(".", "").replace("+", ""), text)


def is_garbled(html: str) -> bool:
    """Heuristic worth-cleaning check: heavy span fragmentation (the wall-of-text shape), OR the
    letter-by-letter censorship marker once spans are glued (in the raw HTML the ``.+`` is split
    across span boundaries, so it must be checked on the concatenated text)."""
    if not html:
        return False
    if html.count("<span") > 40:
        return True
    return ".+" in BeautifulSoup(html, "lxml").get_text("")


def clean_chapter_html(html: str) -> str:
    """Return a readable version of ``html``: censorship removed + reflowed into ``<p>`` paragraphs.
    Idempotent-ish (re-running on cleaned output is a no-op of substance) and never returns blank."""
    if not html or not html.strip() or not is_garbled(html):
        return html  # only touch the fragmented/censored shape; already-clean content is returned as-is
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        tag.decompose()  # the reader renders the chapter title itself; drop the duplicate
    for a in soup.find_all("a", href=True):
        if any(s in a["href"] for s in _JUNK_SUBSTR):
            a.decompose()
    # Drop a publish-date stamp node (often glued to the next span, so a line-level filter misses it).
    for tag in soup.find_all(["span", "time", "div", "p"]):
        if _DATE_RE.match(tag.get_text(strip=True) or ""):
            tag.decompose()
    # '' separator: censorship stays glued (the spacer spans contribute their own '\n'); the newlines
    # at wrap/structure points are kept for now so leading heading lines can be peeled off.
    raw = _deobfuscate(soup.get_text(""))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in raw.split("\n")]
    lines = [ln for ln in lines if ln and not _DATE_RE.match(ln)
             and not any(s in ln for s in _JUNK_SUBSTR)]

    out: list[str] = []
    # Peel the LEADING run of heading/label lines (Chapter / Part / a short subtitle) into <h3>; stop
    # at the first real prose line so mid-body short lines are never mistaken for headings.
    i = 0
    while i < len(lines) and _is_heading(lines[i]):
        out.append(f"<h3>{escape(lines[i])}</h3>")
        i += 1
    body = " ".join(lines[i:])
    out.extend(f"<p>{escape(p)}</p>" for p in _paragraphize(body))
    if not any(t.startswith("<p>") for t in out):
        return html  # safety: never blank out a chapter's prose
    return "".join(out)


def _is_heading(line: str) -> bool:
    words = line.split()
    if _HEADING_RE.match(line) and len(words) <= 6:
        return True
    # A short, capitalized label with no sentence punctuation (e.g. a part/scene subtitle).
    return len(words) <= 3 and line[:1].isupper() and not re.search(r"[.!?,;:]", line)


def _paragraphize(text: str) -> list[str]:
    """Split reflowed prose into paragraphs: each quoted line of dialogue is its own paragraph;
    narration is split at sentence boundaries (web-novel prose is largely one sentence per line)."""
    out: list[str] = []
    for chunk in re.split(r'("[^"]*")', text):  # alternating narration / "dialogue"
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.startswith('"') and chunk.endswith('"'):
            out.append(chunk)
            continue
        for sent in _SENT_SPLIT.split(chunk):
            sent = sent.strip()
            if sent:
                out.append(sent)
    return out


if __name__ == "__main__":  # ponytail: runnable self-check
    dirty = (
        '<div><a href="https://play.google.com/store/apps/x"></a>'
        "<span>The naked s.</span><span>h.</span><span>i.</span><span>+ro was.</span>"
        "<span>h.</span><span>i.</span><span>+ng.</span><span>\n</span>"
        '<span>"Explain?</span><span>\n</span><span>I hope you can explain."</span>'
        "<span>\n</span><span>Mar 18, 2026</span></div>"
    )
    out = clean_chapter_html(dirty)
    assert "shiro" in out and "washing" in out, out          # censorship removed
    assert ".+" not in out and "play.google" not in out, out  # markers + promo gone
    assert "Mar 18, 2026" not in out, out                     # date stamp dropped
    assert out.count("<p>") >= 2, out                         # reflowed into paragraphs
    assert "Explain? I hope you can explain." in out.replace("&quot;", '"'), out  # dialogue = one paragraph
    print("textclean self-check OK\n", out)
