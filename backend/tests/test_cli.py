"""Terminal-reader (shelfcli) pure-logic tests — no TTY required."""
from __future__ import annotations

import pathlib

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


def _cli_db_fixture():
    """A small library for one user: 3 works, 4 chapters each (2 readable in one)."""
    from app.db import SessionLocal, init_db
    from app.models import Chapter, ChapterContent, LibraryItem, ReadingState, User, Work
    from sqlalchemy import delete

    init_db()
    db = SessionLocal()
    for m in (ReadingState, LibraryItem, ChapterContent, Chapter, Work, User):
        db.execute(delete(m))
    db.commit()
    user = User(username="cli", password_hash="x", role="admin")
    db.add(user)
    db.commit()
    works = []
    for i in range(3):
        w = Work(title=f"Work {i}", author=f"Author {i}", language="en", hooked=False)
        db.add(w)
        db.flush()
        db.add(LibraryItem(user_id=user.id, work_id=w.id))
        for j in range(1, 5):
            ch = Chapter(work_id=w.id, source_chapter_ref=f"c{i}-{j}", index=j,
                         title=f"Ch {j}", fetch_status="fetched")
            db.add(ch)
            db.flush()
            # Work 1 has only its first two chapters fetched → readable < total.
            if i == 1 and j > 2:
                continue
            cc = ChapterContent(chapter_id=ch.id, format="html", body="<p>x</p>",
                                word_count=1, checksum=f"{i}-{j}")
            db.add(cc)
            db.flush()
            ch.content_id = cc.id
        works.append(w)
    db.commit()
    return db, user, works


def test_work_rows_is_scoped_to_the_user_and_counts_correctly():
    """The library is per-user, and 'readable' counts only chapters whose content is stored."""
    from app.cli import _work_rows
    from app.models import LibraryItem, User
    from sqlalchemy import select

    db, user, works = _cli_db_fixture()
    try:
        rows = _work_rows(db, user.id)
        assert {r["title"] for r in rows} == {"Work 0", "Work 1", "Work 2"}
        by_title = {r["title"]: r for r in rows}
        assert by_title["Work 0"]["total"] == 4 and by_title["Work 0"]["readable"] == 4
        assert by_title["Work 1"]["total"] == 4 and by_title["Work 1"]["readable"] == 2
        # Another account sees nothing — works are shared rows, libraries are not.
        other = User(username="other", password_hash="x", role="user")
        db.add(other)
        db.commit()
        assert _work_rows(db, other.id) == []
        assert _work_rows(db, None) == []
        # ...until it's in their library.
        db.add(LibraryItem(user_id=other.id, work_id=works[0].id))
        db.commit()
        assert [r["title"] for r in _work_rows(db, other.id)] == ["Work 0"]
        assert db.scalar(select(User.id).where(User.username == "cli")) == user.id
    finally:
        db.close()


def test_work_rows_resume_percentage_and_ordering():
    """Progress % comes from the resume chapter's index + scroll, and read titles sort first."""
    from app.cli import _work_rows
    from app.models import Chapter, ReadingState
    from sqlalchemy import select

    db, user, works = _cli_db_fixture()
    try:
        ch2 = db.scalar(select(Chapter).where(Chapter.work_id == works[2].id, Chapter.index == 3))
        db.add(ReadingState(user_id=user.id, work_id=works[2].id,
                            last_chapter_id=ch2.id, scroll_fraction=0.5))
        db.commit()
        rows = _work_rows(db, user.id)
        assert rows[0]["title"] == "Work 2"          # recently-read first
        assert rows[0]["has_state"] is True
        assert rows[0]["pct"] == 62.5                # (3-1 + 0.5) / 4
        assert all(r["pct"] == 0.0 for r in rows[1:])
    finally:
        db.close()


def test_work_rows_does_not_scale_queries_with_library_size():
    """The library screen re-runs this every REFRESH_MS, so it must not be O(works) in STATEMENTS —
    a per-work version measured 4321 statements / 780ms for a 1440-title library, i.e. thousands of
    queries a second against the server's DB while merely sitting on the shelf."""
    from sqlalchemy import event
    from app.cli import _work_rows
    from app.models import Chapter, ChapterContent, LibraryItem, Work

    db, user, _works = _cli_db_fixture()
    try:
        def count_statements():
            n = [0]
            bind = db.get_bind()

            def _tick(*_a, **_k):
                n[0] += 1

            event.listen(bind, "before_cursor_execute", _tick)
            try:
                _work_rows(db, user.id)
            finally:
                event.remove(bind, "before_cursor_execute", _tick)
            return n[0]

        small = count_statements()
        for i in range(20):                      # grow the library ~7x
            w = Work(title=f"Extra {i}", author="A", language="en", hooked=False)
            db.add(w)
            db.flush()
            db.add(LibraryItem(user_id=user.id, work_id=w.id))
            ch = Chapter(work_id=w.id, source_chapter_ref=f"x{i}", index=1, title="Ch",
                         fetch_status="fetched")
            db.add(ch)
            db.flush()
            cc = ChapterContent(chapter_id=ch.id, format="html", body="<p>x</p>",
                                word_count=1, checksum=f"x{i}")
            db.add(cc)
            db.flush()
            ch.content_id = cc.id
        db.commit()
        assert len(_work_rows(db, user.id)) == 23
        assert count_statements() == small, "query count grew with the library (N+1 regression)"
    finally:
        db.close()

def test_a_mistyped_db_path_refuses_instead_of_inventing_a_library(tmp_path):
    """A typo in --db used to CREATE an empty SQLite file at the typo'd path and then show an empty
    library — the same "invented a database" failure the no-args guard exists to prevent, plus a
    stray ~900KB file wherever the typo pointed.

    Run as a subprocess because the guard reads cached settings that are fixed at import.
    """
    import os
    import subprocess
    import sys

    missing = tmp_path / "typo.db"
    env = {k: v for k, v in os.environ.items() if k != "SHELF_DATABASE_URL"}
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "--db", str(missing), "--list-users"],
        capture_output=True, text=True, timeout=120,
        cwd=str(pathlib.Path(__file__).resolve().parent.parent), env=env,
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "no database at" in proc.stderr
    assert not missing.exists(), "refused, but still created the database file"
