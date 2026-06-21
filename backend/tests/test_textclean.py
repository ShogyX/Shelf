"""Reader text cleanup: de-censoring + reflow of badly-scraped chapter HTML."""
from __future__ import annotations

from app.ingestion.textclean import _fix_top_structure, clean_chapter_html, is_garbled

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


# Sources switch quote style mid-book (Library of Heaven's Path goes straight→smart at ch.1128). A
# multi-sentence run of SMART-quoted dialogue must stay one paragraph, not shatter at every sentence.
SMART = (
    "<div>" + "<span>x</span>" * 41 +  # force the garbled (>40 span) path
    "<span>The puppet spoke.</span><span>\n</span>"
    "<span>“The technique you chose is the Grand Constellation Finger.</span><span>\n</span>"
    "<span>If you withstand my attack, you clear the trial.</span><span>\n</span>"
    "<span>Otherwise, you’ll have to try again,” the puppet said.</span></div>"
)


def test_smart_quote_dialogue_stays_one_paragraph():
    out = clean_chapter_html(SMART)
    # the whole quoted run (3 sentences) is a single <p>, with the narration tag split off after it
    assert "<p>“The technique you chose is the Grand Constellation Finger. " \
           "If you withstand my attack, you clear the trial. " \
           "Otherwise, you’ll have to try again,”</p>" in out, out
    assert "<p>the puppet said.</p>" in out, out


def test_missing_closing_quote_does_not_merge_chapter():
    # An unbalanced opening quote must NOT swallow the rest of the body into one giant block.
    bad = "<div>" + "<span>y</span>" * 41 + \
        "<span>He said “oops with no close. Then narration. And more narration here.</span></div>"
    out = clean_chapter_html(bad)
    assert out.count("<p>") >= 2, out
    assert max(len(p) for p in out.split("<p>")) < 200, out


def test_cjk_chapter_splits_into_paragraphs():
    # TXT-2: a CJK chapter (sentences ending 。！？, no inter-sentence space) must reflow into multiple
    # <p>, not collapse into one wall. >40 spans forces the garbled path.
    cjk = ("<div>" + "<span>x</span>" * 41 +
           "<span>第一句话结束了。</span><span>第二句话也结束了！</span>"
           "<span>第三句话是问句吗？</span><span>第四句话结束。</span></div>")
    out = clean_chapter_html(cjk)
    assert out.count("<p>") >= 2, out


def test_literal_dot_plus_in_prose_not_garbled_or_mangled():
    # TXT-3: prose containing a literal ".+" (e.g. a regex or "file.+ext") must NOT be treated as
    # garbled nor have characters stripped.
    prose = "<p>The regex .+ matches everything and file.+ext stays</p>"
    assert not is_garbled(prose)
    out = clean_chapter_html(prose)
    assert "file.+ext" in out and ".+ matches" in out, out


def test_no_spacer_spans_are_not_fused():
    # TXT-1: words served as adjacent spans with NO inter-span whitespace and no '\n' spacer must not
    # fuse into one run. The DIRTY censor sample (covered above) must still de-censor — verified here too.
    h = ("<div>" + "".join(f"<span>{w}</span>" for w in
                          ["The", "cat", "sat", "on", "the", "mat.", "It", "was", "warm."] * 6) + "</div>")
    out = clean_chapter_html(h)
    assert "The cat sat" in out and "Thecatsat" not in out, out
    # DIRTY still de-censors with the ' ' separator in play.
    censored = clean_chapter_html(DIRTY)
    assert "shiro" in censored and "washing" in censored, censored


def test_prose_scene_opener_not_promoted_to_heading():
    # TXT-4: a short Title-Case scene-opener as the LEADING line must stay prose; only the explicit
    # Chapter/Part shape becomes <h3>. A censorship marker (not span-count) triggers the garbled path so
    # the heading-peel still sees the real first line.
    h = ("<div><span>The End</span><span>\n</span>"
         "<span>He walked s.</span><span>h.</span><span>i.</span><span>+ro away now.</span></div>")
    out = clean_chapter_html(h)
    assert "<h3>The End</h3>" not in out and "<p>The End" in out, out
    h2 = ("<div><span>Chapter 5</span><span>\n</span>"
          "<span>The story s.</span><span>h.</span><span>i.</span><span>+ro continues.</span></div>")
    assert "<h3>Chapter 5</h3>" in clean_chapter_html(h2)


def test_fix_top_structure_degludes_title_and_credit():
    # ch.1125 shape: title + credit + first sentence all fused into one <p>.
    glued = ("<p>Chapter 1125: Teacher, Thank You "
             "Translator: StarveCleric Editor: Millman97 Shi Hao spent years.</p><p>Next para.</p>")
    assert _fix_top_structure(glued) == (
        "<h3>Chapter 1125: Teacher, Thank You</h3>"
        "<p>Translator: StarveCleric Editor: Millman97</p>"
        "<p>Shi Hao spent years.</p><p>Next para.</p>")
    # ch.1121 shape: title already its own <h3>, but credit fused to the first sentence.
    g2 = "<h3>Chapter 1121: Elder Qi</h3><p>Translator: StarveCleric Editor: Millman97 Zhuo did send it.</p>"
    assert _fix_top_structure(g2) == (
        "<h3>Chapter 1121: Elder Qi</h3>"
        "<p>Translator: StarveCleric Editor: Millman97</p><p>Zhuo did send it.</p>")
    # already isolated → idempotent no-op.
    ok = "<h3>Chapter 1: X</h3><p>Translator: A Editor: B</p><p>Body.</p>"
    assert _fix_top_structure(ok) == ok
