"""Stage 3 tests: sanitization strips dangerous markup, keeps semantic structure."""
from __future__ import annotations

from app.sanitize import count_words, sanitize_html, text_to_html


def test_strips_scripts_and_handlers():
    raw = '<p onclick="evil()">Hi</p><script>steal()</script><style>x{}</style>'
    out = sanitize_html(raw)
    assert "script" not in out.lower()
    assert "onclick" not in out.lower()
    assert "<style" not in out.lower()
    assert "Hi" in out


def test_blocks_javascript_uri():
    out = sanitize_html('<a href="javascript:alert(1)">x</a>')
    assert "javascript:" not in out.lower()


def test_keeps_semantic_tags_and_unwraps_unknown():
    raw = "<article><p>A <em>b</em> <strong>c</strong></p><marquee>d</marquee></article>"
    out = sanitize_html(raw)
    assert "<em>b</em>" in out
    assert "<strong>c</strong>" in out
    assert "marquee" not in out.lower()
    assert "d" in out  # content preserved even though tag unwrapped


def test_text_to_html_paragraphs():
    out = text_to_html("Line one\n\nLine two")
    assert out.count("<p>") == 2


def test_word_count():
    assert count_words("<p>one two three</p>") == 3
