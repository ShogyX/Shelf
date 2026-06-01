"""Terminal-reader (shelfcli) pure-logic tests — no TTY required."""
from __future__ import annotations

from app.cli import _blocks, _disguise_layout, _layout


def test_disguise_layout_docs_and_logs():
    blocks = [("h", "The Heading"), ("p", "Some prose that is long enough to wrap nicely here.")]
    # off → identical to the normal layout
    assert _disguise_layout(blocks, 50, "off") == _layout(blocks, 50)
    # docs → man-page style: uppercased section heading, no log prefixes
    docs = "\n".join(t for t, _a, _b in _disguise_layout(blocks, 50, "docs"))
    assert "THE HEADING" in docs
    # logs → every line carries a timestamp + level + module prefix
    log_lines = [t for t, _a, _b in _disguise_layout(blocks, 80, "logs") if t]
    assert log_lines and all(ln.startswith("2026-") for ln in log_lines)
    assert any(" INFO  " in ln or " DEBUG " in ln or " WARN  " in ln for ln in log_lines)
    # block indices are preserved so reading progress still maps correctly
    assert {b for _t, _a, b in _disguise_layout(blocks, 80, "logs")} == {0, 1}


def test_blocks_parses_structure():
    html = (
        "<h2>Chapter 5</h2><p>First paragraph.</p>"
        "<ul><li>one</li><li>two</li></ul>"
        "<blockquote>quoted</blockquote>"
        '<figure><img src="/media/comics/x/0001.png"/></figure>'
    )
    blocks = _blocks(html)
    kinds = [k for k, _ in blocks]
    texts = [t for _, t in blocks]
    assert kinds[0] == "h" and texts[0] == "Chapter 5"
    assert ("p", "First paragraph.") in blocks
    assert any(k == "li" and t.startswith("• one") for k, t in blocks)
    assert any(k == "li" and t.startswith("• two") for k, t in blocks)
    assert any(k == "q" for k, _ in blocks)
    assert any(k == "img" for k, _ in blocks)  # comic image -> placeholder block


def test_blocks_plain_text_fallback():
    blocks = _blocks("just some text with no tags")
    assert blocks and blocks[0][0] == "p"
    assert "just some text" in blocks[0][1]


def test_tui_q_is_crash_proof_on_db_errors():
    # A transient DB error must NOT propagate (it would crash the curses UI) — q()
    # rolls back, closes, and returns the default so the TUI keeps running.
    from app.cli import TUI

    tui = TUI.__new__(TUI)  # bypass curses-dependent __init__
    assert tui.q(lambda db: 1 / 0, default="ok") == "ok"
    assert tui.q(lambda db: 7) == 7  # real session still works for good ops


def test_layout_wraps_and_maps_block_indices():
    blocks = [("h", "Heading"), ("p", "word " * 60)]  # long paragraph wraps
    lines = _layout(blocks, width=40)
    # Every display line carries its source block index for progress tracking.
    assert all(len(item) == 3 for item in lines)
    block_indices = {bi for _t, _a, bi in lines}
    assert block_indices == {0, 1}
    # The wrapped paragraph spans multiple lines, none exceeding the width.
    para_lines = [t for t, _a, bi in lines if bi == 1 and t]
    assert len(para_lines) > 1
    assert all(len(t) <= 40 for t in para_lines)
