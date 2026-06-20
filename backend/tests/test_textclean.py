"""Reader text cleanup: de-censoring + reflow of badly-scraped chapter HTML."""
from __future__ import annotations

from app.ingestion.textclean import clean_chapter_html, is_garbled

# A miniature of the real novellunar shape: tiny spans, '\n' spacer spans, letter-by-letter
# censorship ('s.h.i.+ro' = Shiro, 'was.h.i.+ng' = washing), a dup <h1>, a date stamp, a promo link.
DIRTY = (
    "<div><h1>Vol 1 Chapter 2</h1><div><span>Mar 18, 2026</span></div>"
    '<a href="https://play.google.com/store/apps/x"></a>'
    "<span>Chapter 2</span><span>\n</span><span>Part 1</span><span>\n</span>"
    "<span>The naked s.</span><span>h.</span><span>i.</span><span>+ro asked while being</span>"
    "<span>\n</span><span>was.</span><span>h.</span><span>i.</span><span>+ng.</span><span>\n</span>"
    '<span>"Nii.......I hope you can explain."</span>'
    "</div>"
)


def test_is_garbled():
    assert is_garbled(DIRTY)
    assert not is_garbled("<p>Already clean prose, nothing to do here.</p>")


def test_clean_removes_censorship_and_junk_and_reflows():
    out = clean_chapter_html(DIRTY)
    assert "shiro" in out and "washing" in out          # censorship de-obfuscated
    assert ".+" not in out                               # no obfuscation markers remain
    assert "play.google" not in out and "Mar 18, 2026" not in out  # promo + date dropped
    assert "Vol 1 Chapter 2" not in out                  # duplicate title (reader shows it) dropped
    assert "<h3>Chapter 2</h3>" in out and "<h3>Part 1</h3>" in out  # headings kept as headings
    # Dialogue is its own paragraph; the censored sentence reflows above it.
    assert "the naked shiro asked while being washing." in out.lower()
    assert out.count("<p>") >= 2


def test_idempotent_on_clean_content():
    out1 = clean_chapter_html(DIRTY)
    assert clean_chapter_html(out1) == out1              # re-cleaning is a no-op


def test_never_blanks_content():
    # A plain paragraph isn't garbled → returned verbatim, never emptied.
    plain = "<p>The quick brown fox.</p>"
    assert clean_chapter_html(plain) == plain
