"""shelfcli — a terminal reader for Shelf.

A small curses TUI that talks straight to the same SQLite database the web app
uses, so browsing, reading, and (crucially) reading progress are shared: stop in
the terminal, pick up in the browser, and vice-versa. Progress is written through
the very same code path as the web reader (`reading.save_progress`).

Run:  shelfcli            (installed by install.sh; or `python -m app.cli`)
"""
from __future__ import annotations

import curses
import sys
import textwrap

from bs4 import BeautifulSoup
from sqlalchemy import event, func, select

from .db import SessionLocal, engine
from .models import Chapter, ReadingState, Work
from .routers.reading import get_progress as _get_progress
from .routers.reading import save_progress as _save_progress
from .schemas import ProgressIn

_BLOCK_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "figure"]


# --------------------------------------------------------------------------- data
def _fetched_chapters(db, work_id: int):
    """Readable chapters (content stored), in order: list of (id, index, title)."""
    rows = db.execute(
        select(Chapter.id, Chapter.index, Chapter.title)
        .where(Chapter.work_id == work_id, Chapter.content_id.is_not(None))
        .order_by(Chapter.index)
    ).all()
    return [(r[0], r[1], r[2]) for r in rows]


def _chapter_body(db, chapter_id: int) -> str:
    ch = db.get(Chapter, chapter_id)
    if ch is None or ch.content is None:
        return ""
    return ch.content.body or ""


def _blocks(html: str):
    """Parse chapter HTML into ordered (kind, text) blocks for the terminal."""
    soup = BeautifulSoup(html or "", "lxml")
    tags = soup.find_all(_BLOCK_TAGS)
    out: list[tuple[str, str]] = []
    if not tags:
        text = soup.get_text("\n", strip=True)
        return [("p", line) for line in text.split("\n") if line.strip()] or [("p", "(empty)")]
    for el in tags:
        if el.find_parent(_BLOCK_TAGS):
            continue  # only top-level blocks (keeps index aligned with the web reader)
        if el.find("img") and not el.get_text(strip=True):
            out.append(("img", "[ image — open in the web reader to view ]"))
            continue
        txt = el.get_text(" ", strip=True)
        if not txt:
            continue
        if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            out.append(("h", txt))
        elif el.name == "li":
            out.append(("li", "• " + txt))
        elif el.name == "blockquote":
            out.append(("q", "“" + txt + "”"))
        else:
            out.append(("p", txt))
    return out or [("p", "(empty)")]


def _work_rows(db, q: str | None = None):
    """Library rows with resume info, recently-read first."""
    stmt = select(Work).order_by(Work.created_at.desc())
    works = list(db.scalars(stmt).all())
    if q:
        ql = q.lower()
        works = [
            w for w in works
            if ql in (w.title or "").lower() or ql in (w.author or "").lower()
        ]
    rows = []
    for w in works:
        state = db.scalar(select(ReadingState).where(ReadingState.work_id == w.id))
        total = db.scalar(select(func.count(Chapter.id)).where(Chapter.work_id == w.id)) or 0
        readable = db.scalar(
            select(func.count(Chapter.id)).where(
                Chapter.work_id == w.id, Chapter.content_id.is_not(None)
            )
        ) or 0
        pct = 0.0
        updated = None
        if state and state.last_chapter_id:
            ch = db.get(Chapter, state.last_chapter_id)
            if ch and total:
                through = (ch.index - 1) + min(1.0, max(0.0, state.scroll_fraction))
                pct = min(100.0, round(100 * through / total, 1))
            updated = state.updated_at
        rows.append({
            "id": w.id, "title": w.title, "author": w.author,
            "readable": readable, "total": total, "pct": pct, "updated": updated,
            "has_state": bool(state and state.last_chapter_id),
        })
    # Recently-read first, then the rest by recency of addition.
    rows.sort(key=lambda r: (r["updated"] is not None, r["updated"] or 0), reverse=True)
    return rows


def _resume_target(db, work_id: int):
    """(chapter_id, paragraph_index) to open at — the last spot, else first chapter."""
    state = db.scalar(select(ReadingState).where(ReadingState.work_id == work_id))
    chapters = _fetched_chapters(db, work_id)
    if not chapters:
        return None
    valid_ids = {c[0] for c in chapters}
    if state and state.last_chapter_id in valid_ids:
        return state.last_chapter_id, state.paragraph_index
    return chapters[0][0], 0


# ------------------------------------------------------------------------ rendering
def _layout(blocks, width: int):
    """Flatten blocks into display lines: list of (text, attr, block_index)."""
    lines: list[tuple[str, int, int]] = []
    for bi, (kind, txt) in enumerate(blocks):
        if kind == "h" and lines:
            lines.append(("", 0, bi))
        attr = curses.A_BOLD if kind == "h" else 0
        indent = "    " if kind in ("li", "q") else ""
        wrapped = textwrap.wrap(txt, max(8, width - len(indent))) or [""]
        for j, w in enumerate(wrapped):
            lines.append(((indent if j == 0 else "  ") + w if indent else w, attr, bi))
        lines.append(("", 0, bi))  # blank line between blocks
    while lines and lines[-1][0] == "":
        lines.pop()
    return lines


def _safe_add(win, y, x, s, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w:
        try:
            win.addnstr(y, x, s, max(0, w - x - 1), attr)
        except curses.error:
            pass


# --------------------------------------------------------------------------- screens
class TUI:
    def __init__(self, stdscr, db):
        self.scr = stdscr
        self.db = db
        curses.curs_set(0)
        stdscr.keypad(True)

    # ---- library ----
    def library(self):
        sel = 0
        top = 0
        query = ""
        searching = False
        while True:
            rows = _work_rows(self.db, query or None)
            h, w = self.scr.getmaxyx()
            body_h = h - 4
            sel = max(0, min(sel, len(rows) - 1)) if rows else 0
            if sel < top:
                top = sel
            elif sel >= top + body_h:
                top = sel - body_h + 1

            self.scr.erase()
            _safe_add(self.scr, 0, 2, "Shelf — terminal reader", curses.A_BOLD)
            hint = "  ↑/↓ move · Enter read · / search · r refresh · q quit"
            _safe_add(self.scr, 0, max(26, w - len(hint) - 2), hint.strip(), curses.A_DIM)
            _safe_add(self.scr, 1, 2, "─" * (w - 4), curses.A_DIM)

            if not rows:
                msg = "No titles match your search." if query else "Your library is empty."
                _safe_add(self.scr, 3, 2, msg, curses.A_DIM)
            for i in range(top, min(len(rows), top + body_h)):
                r = rows[i]
                y = 2 + (i - top)
                marker = "▸" if r["has_state"] else " "
                pct = f"{r['pct']:>4.0f}%" if r["has_state"] else "   ·"
                title = r["title"][: max(10, w - 34)]
                author = (r["author"] or "Unknown")[:20]
                line = f"{marker} {title}"
                attr = curses.A_REVERSE if i == sel else 0
                _safe_add(self.scr, y, 2, line.ljust(w - 30), attr)
                meta = f"{author:<20} {pct}"
                _safe_add(self.scr, y, max(2, w - 28), meta, attr | curses.A_DIM)

            footer = f"Search: {query}_" if searching else \
                f"{len(rows)} title(s)" + (f' · filter: "{query}"' if query else "")
            _safe_add(self.scr, h - 2, 2, "─" * (w - 4), curses.A_DIM)
            _safe_add(self.scr, h - 1, 2, footer, curses.A_DIM)
            self.scr.refresh()

            c = self.scr.getch()
            if searching:
                if c in (curses.KEY_ENTER, 10, 13):
                    searching = False
                elif c == 27:  # Esc
                    searching = False
                    query = ""
                elif c in (curses.KEY_BACKSPACE, 127, 8):
                    query = query[:-1]
                elif 32 <= c < 127:
                    query += chr(c)
                sel = 0
                top = 0
                continue
            if c in (ord("q"), 27):
                return
            elif c == ord("/"):
                searching = True
            elif c in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif c in (curses.KEY_DOWN, ord("j")):
                sel = min(len(rows) - 1, sel + 1) if rows else 0
            elif c in (curses.KEY_NPAGE,):
                sel = min(len(rows) - 1, sel + body_h)
            elif c in (curses.KEY_PPAGE,):
                sel = max(0, sel - body_h)
            elif c == ord("r"):
                pass  # rows re-queried each loop
            elif c in (curses.KEY_ENTER, 10, 13) and rows:
                self.open_work(rows[sel]["id"], rows[sel]["title"])

    # ---- reader ----
    def open_work(self, work_id: int, work_title: str):
        target = _resume_target(self.db, work_id)
        if target is None:
            self._flash("This title has no readable chapters yet (the crawler may still be working).")
            return
        chapter_id, paragraph = target
        while chapter_id is not None:
            res = self.read_chapter(work_id, work_title, chapter_id, paragraph)
            paragraph = 0
            if res is None:
                return  # user quit back to library
            chapter_id = res  # next/prev chapter id to open

    def read_chapter(self, work_id, work_title, chapter_id, paragraph):
        chapters = _fetched_chapters(self.db, work_id)
        ids = [c[0] for c in chapters]
        idx = ids.index(chapter_id) if chapter_id in ids else 0
        ch = self.db.get(Chapter, chapter_id)
        ch_title = ch.title if ch else ""
        ch_index = ch.index if ch else (idx + 1)
        blocks = _blocks(_chapter_body(self.db, chapter_id))
        prev_id = ids[idx - 1] if idx > 0 else None
        next_id = ids[idx + 1] if idx < len(ids) - 1 else None

        h, w = self.scr.getmaxyx()
        width = min(w - 6, 96)
        margin = max(2, (w - width) // 2)
        lines = _layout(blocks, width)
        body_h = h - 3

        # Jump to the saved paragraph (top line whose block index >= saved).
        top = 0
        for li, (_t, _a, bi) in enumerate(lines):
            if bi >= paragraph:
                top = li
                break

        def save():
            top_block = lines[min(top, len(lines) - 1)][2] if lines else 0
            frac = top_block / max(1, len(blocks))
            try:
                _save_progress(
                    work_id,
                    ProgressIn(last_chapter_id=chapter_id,
                               scroll_fraction=min(1.0, frac), paragraph_index=top_block),
                    self.db,
                )
            except Exception:
                pass

        while True:
            max_top = max(0, len(lines) - body_h)
            top = max(0, min(top, max_top))
            self.scr.erase()
            cur_block = lines[top][2] if lines else 0
            cpct = round(100 * cur_block / max(1, len(blocks)))
            head = f"{work_title} · {ch_title}".strip(" ·")[: w - 18]
            _safe_add(self.scr, 0, 2, head, curses.A_BOLD)
            _safe_add(self.scr, 0, max(2, w - 16), f"Ch {ch_index} · {cpct:>3}%", curses.A_DIM)
            _safe_add(self.scr, 1, 0, "─" * w, curses.A_DIM)
            for row in range(body_h):
                li = top + row
                if li >= len(lines):
                    break
                text, attr, _bi = lines[li]
                if text:
                    _safe_add(self.scr, 2 + row, margin, text, attr)
            hint = " ↑/↓ scroll · Space page · ←/→ p/n chapter · t contents · q library "
            _safe_add(self.scr, h - 1, 0, hint[: w - 1].center(w - 1), curses.A_REVERSE)
            self.scr.refresh()

            c = self.scr.getch()
            if c in (curses.KEY_UP, ord("k")):
                top -= 1
            elif c in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif c in (ord(" "), curses.KEY_NPAGE):
                top += body_h - 2
            elif c in (curses.KEY_PPAGE, ord("b")):
                top -= body_h - 2
            elif c in (ord("g"), curses.KEY_HOME):
                top = 0
            elif c in (ord("G"), curses.KEY_END):
                top = max_top
            elif c in (curses.KEY_RIGHT, ord("n")):
                if next_id is not None:
                    save()
                    return next_id
                else:
                    self._flash("You're at the last chapter.")
            elif c in (curses.KEY_LEFT, ord("p")):
                if prev_id is not None:
                    save()
                    return prev_id
                else:
                    self._flash("You're at the first chapter.")
            elif c == ord("t"):
                picked = self.toc(chapters, chapter_id)
                if picked is not None and picked != chapter_id:
                    save()
                    return picked
            elif c in (ord("q"), 27):
                save()
                return None
            elif c == curses.KEY_RESIZE:
                h, w = self.scr.getmaxyx()
                width = min(w - 6, 96)
                margin = max(2, (w - width) // 2)
                lines = _layout(blocks, width)
                body_h = h - 3

    # ---- table of contents ----
    def toc(self, chapters, current_id):
        sel = next((i for i, c in enumerate(chapters) if c[0] == current_id), 0)
        top = 0
        while True:
            h, w = self.scr.getmaxyx()
            body_h = h - 4
            if sel < top:
                top = sel
            elif sel >= top + body_h:
                top = sel - body_h + 1
            self.scr.erase()
            _safe_add(self.scr, 0, 2, "Contents", curses.A_BOLD)
            _safe_add(self.scr, 0, max(12, w - 34), "Enter open · q/Esc back", curses.A_DIM)
            _safe_add(self.scr, 1, 2, "─" * (w - 4), curses.A_DIM)
            for i in range(top, min(len(chapters), top + body_h)):
                cid, cidx, ctitle = chapters[i]
                y = 2 + (i - top)
                mark = "►" if cid == current_id else " "
                label = f"{mark} {cidx:>4}  {ctitle}"[: w - 4]
                _safe_add(self.scr, y, 2, label, curses.A_REVERSE if i == sel else 0)
            self.scr.refresh()
            c = self.scr.getch()
            if c in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif c in (curses.KEY_DOWN, ord("j")):
                sel = min(len(chapters) - 1, sel + 1)
            elif c in (curses.KEY_NPAGE,):
                sel = min(len(chapters) - 1, sel + body_h)
            elif c in (curses.KEY_PPAGE,):
                sel = max(0, sel - body_h)
            elif c in (curses.KEY_ENTER, 10, 13):
                return chapters[sel][0]
            elif c in (ord("q"), 27, ord("t")):
                return None

    def _flash(self, msg: str):
        h, w = self.scr.getmaxyx()
        _safe_add(self.scr, h - 1, 0, (" " + msg).ljust(w - 1), curses.A_REVERSE)
        self.scr.refresh()
        curses.napms(1100)


# --------------------------------------------------------------------------- entry
def _run(stdscr):
    db = SessionLocal()
    try:
        TUI(stdscr, db).library()
    finally:
        db.close()


def main() -> None:
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print("shelfcli — terminal reader for Shelf\n\n"
              "  shelfcli            browse + read your library in the terminal\n\n"
              "Keys: ↑/↓ move · Enter open · / search · t contents · Space page · q back\n"
              "Reading progress is shared with the web app — pick up where you left off.")
        return
    # Be patient with the DB lock if the web service writes progress at the same time.
    @event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_con, _rec):  # noqa: ANN001
        try:
            dbapi_con.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass

    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
