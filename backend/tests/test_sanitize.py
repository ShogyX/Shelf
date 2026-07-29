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


def test_http_anchor_gets_rel_noopener():
    # SEC-1: a kept http(s) link carries rel=noopener noreferrer nofollow (defense-in-depth).
    out = sanitize_html('<a href="http://example.com/x">link</a>')
    assert 'href="http://example.com/x"' in out
    assert "noopener" in out and "noreferrer" in out and "nofollow" in out


def test_sanitize_filename_is_the_one_rule_for_names_on_disk():
    """This replaced five copy-pasted variants (librivox / import_core / companion / delivery), two
    of which lived in the SAME module and named the same title differently depending on which
    endpoint produced the file."""
    from app.sanitize import sanitize_filename

    assert sanitize_filename("Harry Potter & the Chamber/Secrets") == "Harry Potter the Chamber Secrets"
    assert sanitize_filename("A Book: Part 2") == "A Book Part 2"
    # Punctuation the class deliberately keeps — dropping it would mangle ordinary titles.
    assert sanitize_filename("Bod, O'Brien (Unabridged) - Vol.1") == "Bod, O'Brien (Unabridged) - Vol.1"
    # Path separators can never survive, or a "filename" could escape its directory.
    assert "/" not in sanitize_filename("../../etc/passwd")
    assert "\\" not in sanitize_filename(r"..\..\windows")
    # Replacement char, truncation and fallback are the three things the old copies disagreed on.
    # repl substitutes the DISALLOWED characters; spaces are inside the allowed class and stay,
    # which is what the librivox track-naming path relied on.
    assert sanitize_filename("a/b", repl="_") == "a_b"
    assert sanitize_filename("a b", repl="_") == "a b"
    assert sanitize_filename("abcdef", limit=3) == "abc"
    assert sanitize_filename("", fallback="track.mp3") == "track.mp3"
    assert sanitize_filename("日本語", fallback="book") == "日本語"      # \w keeps CJK
    assert sanitize_filename("💥", fallback="book") == "book"          # emoji sanitizes to nothing
    assert sanitize_filename(None, fallback="book") == "book"


def test_log_safe_stops_a_forged_audit_line():
    """Values that came from a request must not be able to write extra lines into the log. The
    service-token admin API logs the username it was asked to create, and that log IS the audit
    trail an operator reads after an incident — so a newline in a username could fabricate entries
    in it. CodeQL flagged 7 of these (py/log-injection) on the audit logging itself."""
    from app.sanitize import log_safe

    forged = "bob\nINFO  service-admin[abc]: HARD-deleted user id=1"
    out = log_safe(forged)
    assert "\n" not in out and "\r" not in out
    assert out.startswith("bob ")           # the newline became a space, content is preserved
    assert log_safe("a\r\nb") == "a  b"
    # Non-strings are accepted (the update path logs a list of field names).
    assert log_safe(["email", "is_active"]) == "['email', 'is_active']"
    assert log_safe(None) == "None"
    # One field can't push the rest of the line out of view.
    assert len(log_safe("x" * 5000)) == 200
    assert len(log_safe("x" * 5000, limit=10)) == 10
