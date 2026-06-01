"""shelfcli — a terminal reader for Shelf.

A small curses TUI that talks straight to the same SQLite database the web app
uses, so browsing, reading, and (crucially) reading progress are shared: stop in
the terminal, pick up in the browser, and vice-versa. Progress is written through
the very same code path as the web reader (`reading.save_progress`).

Run:  shelfcli            (installed by install.sh; or `python -m app.cli`)
"""
from __future__ import annotations

import argparse
import curses
import os
import sys
import textwrap

from bs4 import BeautifulSoup
from sqlalchemy import func, select

from .db import SessionLocal
from .models import Chapter, ReadingState, User, Work
from .routers.reading import save_progress_for as _save_progress_for
from .schemas import ProgressIn

_BLOCK_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "figure"]
# How often (ms) the UI wakes to re-query the DB so background gathering (the slow
# web crawler fetching new chapters) shows up live without needing a keypress.
REFRESH_MS = 1500


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


def _resolve_user(db, username: str | None = None):
    """Which account shelfcli reads/writes progress as → (user_id, username, note).

    Priority: --user / SHELF_CLI_USER, else the first active admin, else any user.
    Returns (None, None, note) when no user matches so the caller can explain why.
    """
    from .config import get_settings

    name = (username or get_settings().cli_user or "").strip()
    if name:
        u = db.scalar(select(User).where(User.username == name))
        if u is not None:
            return u.id, u.username, None
        return None, None, f"no account named {name!r}"
    u = db.scalar(
        select(User).where(User.role == "admin", User.is_active.is_(True)).order_by(User.id)
    ) or db.scalar(select(User).order_by(User.id))
    if u is not None:
        return u.id, u.username, None
    return None, None, "no accounts yet — create one in the web app (Setup/Users)"


def _work_rows(db, user_id, q: str | None = None):
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
        state = db.scalar(select(ReadingState).where(
            ReadingState.work_id == w.id, ReadingState.user_id == user_id))
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


def _resume_target(db, user_id, work_id: int):
    """(chapter_id, paragraph_index) to open at — the last spot, else first chapter."""
    state = db.scalar(select(ReadingState).where(
        ReadingState.work_id == work_id, ReadingState.user_id == user_id))
    chapters = _fetched_chapters(db, work_id)
    if not chapters:
        return None
    valid_ids = {c[0] for c in chapters}
    if state and state.last_chapter_id in valid_ids:
        return state.last_chapter_id, state.paragraph_index
    return chapters[0][0], 0


def _load_chapter(db, work_id: int, chapter_id: int) -> dict | None:
    """Everything the reader needs for one chapter, in a single session."""
    chapters = _fetched_chapters(db, work_id)
    ids = [c[0] for c in chapters]
    if chapter_id not in ids:
        if not ids:
            return None
        chapter_id = ids[0]
    idx = ids.index(chapter_id)
    ch = db.get(Chapter, chapter_id)
    return {
        "chapter_id": chapter_id,
        "title": ch.title if ch else "",
        "index": ch.index if ch else (idx + 1),
        "blocks": _blocks(_chapter_body(db, chapter_id)),
        "prev_id": ids[idx - 1] if idx > 0 else None,
        "next_id": ids[idx + 1] if idx < len(ids) - 1 else None,
        "chapters": chapters,
    }


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


# ---------------------------------------------------------------- "inconspicuous" modes
# Disguise the reader so a passer-by sees man-page documentation or streaming logs
# instead of a novel. Cycle with `d`; start with `--disguise`.
DISGUISES = ("off", "docs", "logs")
_LOG_LEVELS = ["INFO", "INFO", "DEBUG", "INFO", "WARN", "INFO", "DEBUG", "INFO", "TRACE", "INFO"]
_LOG_MODS = ["http.server", "db.session", "auth.token", "cache.layer", "worker.queue",
             "scheduler", "io.fs", "net.client", "render.pipeline", "config.loader"]


def _slug(s: str) -> str:
    out = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (s or "").lower())
    return out.strip("-")[:40] or "service"


def _man_name(title: str) -> str:
    out = "".join(c for c in (title or "") if c.isalnum() or c in " -_").strip().upper()
    return (out.split(" ")[0] or "MANUAL")[:18]


def _disguise_layout(blocks, width, mode):
    """Re-flow blocks to look like documentation (man page) or terminal logs."""
    lines: list[tuple[str, int, int]] = []
    if mode == "docs":
        for bi, (kind, txt) in enumerate(blocks):
            if kind == "h":
                if lines:
                    lines.append(("", 0, bi))
                lines.append(("   " + txt.upper()[: max(1, width - 3)], curses.A_BOLD, bi))
                lines.append(("", 0, bi))
            else:
                indent = "       "  # man-page body indent
                for seg in (textwrap.wrap(txt, max(10, width - len(indent))) or [""]):
                    lines.append((indent + seg, 0, bi))
                lines.append(("", 0, bi))
        while lines and lines[-1][0] == "":
            lines.pop()
        return lines
    if mode == "logs":
        sec = 0
        for bi, (_kind, txt) in enumerate(blocks):
            lvl = _LOG_LEVELS[bi % len(_LOG_LEVELS)]
            mod = _LOG_MODS[bi % len(_LOG_MODS)]
            prefix = f"2026-06-01T09:00:00.000Z {lvl:<5} {mod}: "
            avail = max(20, width - len(prefix))
            for j, seg in enumerate(textwrap.wrap(txt, avail) or [""]):
                mm, ss, ms = (sec // 60) % 60, sec % 60, (bi * 137 + j * 31) % 1000
                sec += 1
                line = f"2026-06-01T09:{mm:02d}:{ss:02d}.{ms:03d}Z {lvl:<5} {mod}: {seg}"
                attr = curses.A_DIM if lvl in ("DEBUG", "TRACE") else 0
                lines.append((line[:width], attr, bi))
        return lines
    return _layout(blocks, width)


def _safe_add(win, y, x, s, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w:
        try:
            win.addnstr(y, x, s, max(0, w - x - 1), attr)
        except curses.error:
            pass


# --------------------------------------------------------------------------- screens
class TUI:
    def __init__(self, stdscr, username: str | None = None, disguise: str = "off"):
        from .config import get_settings

        self.scr = stdscr
        self.db_url = get_settings().database_url
        self.disguise = disguise if disguise in DISGUISES else "off"
        self.user_id, self.user_name, self.user_note = self.q(
            lambda db: _resolve_user(db, username), default=(None, None, "database unavailable")
        )
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(REFRESH_MS)  # getch() returns -1 after this, driving live refresh

    def _cycle_disguise(self):
        self.disguise = DISGUISES[(DISGUISES.index(self.disguise) + 1) % len(DISGUISES)]

    def work_rows(self, query):
        """Library rows + an error string (so a DB problem shows as a message, not an
        empty shelf). Unlike q(), this surfaces the failure instead of swallowing it."""
        db = SessionLocal()
        try:
            return _work_rows(db, self.user_id, query or None), None
        except Exception as exc:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:
                pass
            return [], str(exc)
        finally:
            db.close()

    def q(self, func, *args, default=None):
        """Run a DB operation in a FRESH short-lived session.

        Per-operation sessions mean (a) we always see the latest data even while the
        web service/scheduler write concurrently, and (b) a transient lock or failed
        write can never poison a long-lived session and crash a later action — on any
        error we roll back, close, and return `default` so the UI keeps running.
        """
        db = SessionLocal()
        try:
            return func(db, *args)
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            return default
        finally:
            db.close()

    # ---- library ----
    def library(self):
        sel = 0
        top = 0
        query = ""
        searching = False
        while True:
            rows, err = self.work_rows(query)
            h, w = self.scr.getmaxyx()
            body_h = h - 4
            sel = max(0, min(sel, len(rows) - 1)) if rows else 0
            if sel < top:
                top = sel
            elif sel >= top + body_h:
                top = sel - body_h + 1

            self.scr.erase()
            if self.disguise == "docs":
                _safe_add(self.scr, 0, 2, "INDEX(7)", curses.A_BOLD)
                _safe_add(self.scr, 0, max(2, (w - 12) // 2), "Manual Pages", curses.A_DIM)
                _safe_add(self.scr, 0, max(2, w - 10), "INDEX(7)", curses.A_BOLD)
            elif self.disguise == "logs":
                _safe_add(self.scr, 0, 2, "$ ls -la ~/notes", curses.A_BOLD)
            else:
                who = f"  ·  {self.user_name}" if self.user_name else ""
                _safe_add(self.scr, 0, 2, f"Shelf — terminal reader{who}", curses.A_BOLD)
                hint = "↑/↓ move · Enter read · / search · q quit"
                _safe_add(self.scr, 0, max(30, w - len(hint) - 2), hint, curses.A_DIM)
            _safe_add(self.scr, 1, 2, "─" * (w - 4), curses.A_DIM)

            if not rows:
                if err:
                    _safe_add(self.scr, 3, 2, "Couldn't read the library:", curses.A_BOLD)
                    _safe_add(self.scr, 4, 2, err[: w - 4], curses.A_DIM)
                    _safe_add(self.scr, 6, 2, f"database: {self.db_url}"[: w - 4], curses.A_DIM)
                elif query:
                    _safe_add(self.scr, 3, 2, "No titles match your search.", curses.A_DIM)
                else:
                    _safe_add(self.scr, 3, 2, "This library has no works.", curses.A_DIM)
                    _safe_add(self.scr, 4, 2, f"database: {self.db_url}"[: w - 4], curses.A_DIM)
                    if self.user_note:
                        _safe_add(self.scr, 5, 2,
                                  f"account: {self.user_note}"[: w - 4], curses.A_DIM)
                    _safe_add(self.scr, 7, 2,
                              "If this looks wrong, the server may use a different database file.",
                              curses.A_DIM)
                    _safe_add(self.scr, 8, 2,
                              "Run: shelfcli --db /path/to/shelf.db   (or shelfcli --list-users)",
                              curses.A_DIM)
            for i in range(top, min(len(rows), top + body_h)):
                r = rows[i]
                y = 2 + (i - top)
                attr = curses.A_REVERSE if i == sel else 0
                if self.disguise == "docs":
                    text = f"  {i + 1:>3}.  {r['title']}"
                    _safe_add(self.scr, y, 2, text.ljust(w - 4)[: w - 4], attr)
                elif self.disguise == "logs":
                    size = f"{(r['readable'] * 7 + 13) % 97 + 2}K"
                    text = (f"-rw-r--r--  1 user  user  {size:>4}  Jun  1 09:{i % 60:02d}  "
                            f"{_slug(r['title'])}.md")
                    _safe_add(self.scr, y, 2, text.ljust(w - 4)[: w - 4], attr)
                else:
                    marker = "▸" if r["has_state"] else " "
                    pct = f"{r['pct']:>3.0f}%" if r["has_state"] else "   ·"
                    total, readable = r["total"], r["readable"]
                    gathering = bool(total) and readable < total
                    chap = f"{readable}/{total}" if total else "0"
                    gmark = "⟳" if gathering else " "
                    title = r["title"][: max(10, w - 36)]
                    author = (r["author"] or "Unknown")[:14]
                    _safe_add(self.scr, y, 2, f"{marker} {title}".ljust(w - 34), attr)
                    meta = f"{author:<14} {chap:>9}{gmark} {pct}"
                    _safe_add(self.scr, y, max(2, w - 32), meta, attr | curses.A_DIM)

            if searching:
                footer = f"Search: {query}_"
            elif self.disguise == "docs":
                footer = f"Manual Pages — {len(rows)} entries"
            elif self.disguise == "logs":
                footer = f"total {len(rows)}"
            else:
                footer = f"{len(rows)} title(s)" + (f' · filter: "{query}"' if query else "")
            _safe_add(self.scr, h - 2, 2, "─" * (w - 4), curses.A_DIM)
            _safe_add(self.scr, h - 1, 2, footer, curses.A_DIM)
            self.scr.refresh()

            c = self.scr.getch()
            if c == -1:
                continue  # idle tick: re-query the library (live gathering updates)
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
            elif c == ord("d"):
                self._cycle_disguise()  # off → docs → logs (inconspicuous mode)
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
        target = self.q(_resume_target, self.user_id, work_id)
        if target is None:
            self._flash("No readable chapters yet — the crawler may still be working.")
            return
        chapter_id, paragraph = target
        while chapter_id is not None:
            res = self.read_chapter(work_id, work_title, chapter_id, paragraph)
            paragraph = 0
            if res is None:
                return  # user quit back to library
            chapter_id = res  # next/prev chapter id to open

    def read_chapter(self, work_id, work_title, chapter_id, paragraph):
        data = self.q(_load_chapter, work_id, chapter_id)
        if data is None:
            self._flash("Couldn't load this chapter (it may still be downloading).")
            return None
        chapter_id = data["chapter_id"]
        ch_title = data["title"]
        ch_index = data["index"]
        blocks = data["blocks"]
        prev_id = data["prev_id"]
        next_id = data["next_id"]

        h, w = self.scr.getmaxyx()

        def metrics():
            # Disguised modes read full-width + left-aligned (like a man page / terminal);
            # normal reading uses a centered measure for comfortable line length.
            if self.disguise == "off":
                wd = min(w - 6, 96)
                return wd, max(2, (w - wd) // 2)
            return max(20, w - 4), 2

        width, margin = metrics()
        lines = _disguise_layout(blocks, width, self.disguise)
        body_h = h - 3

        # Jump to the saved paragraph (top line whose block index >= saved).
        top = 0
        for li, (_t, _a, bi) in enumerate(lines):
            if bi >= paragraph:
                top = li
                break

        def relayout(preserve_block=None):
            """Recompute wrapped lines (after a resize or disguise toggle), keeping place."""
            nonlocal lines, top, width, margin, body_h, h, w
            h, w = self.scr.getmaxyx()
            width, margin = metrics()
            if preserve_block is None:
                preserve_block = lines[min(top, len(lines) - 1)][2] if lines else 0
            lines = _disguise_layout(blocks, width, self.disguise)
            body_h = h - 3
            top = next((li for li, (_t, _a, bi) in enumerate(lines)
                        if bi >= preserve_block), max(0, len(lines) - 1))

        def save():
            top_block = lines[min(top, len(lines) - 1)][2] if lines else 0
            frac = top_block / max(1, len(blocks))
            self.q(
                lambda db: _save_progress_for(
                    db, self.user_id, work_id,
                    ProgressIn(last_chapter_id=chapter_id,
                               scroll_fraction=min(1.0, frac), paragraph_index=top_block),
                ),
            )

        def fresh_nav():
            """Re-read sibling chapters so newly-gathered next/prev become available."""
            nonlocal next_id, prev_id
            ids = self.q(lambda db: [x[0] for x in _fetched_chapters(db, work_id)], default=None)
            if ids and chapter_id in ids:
                i = ids.index(chapter_id)
                prev_id = ids[i - 1] if i > 0 else None
                next_id = ids[i + 1] if i < len(ids) - 1 else None

        while True:
            max_top = max(0, len(lines) - body_h)
            top = max(0, min(top, max_top))
            self.scr.erase()
            cur_block = lines[top][2] if lines else 0
            cpct = round(100 * cur_block / max(1, len(blocks)))
            if self.disguise == "docs":
                name = _man_name(work_title)
                _safe_add(self.scr, 0, 2, f"{name}(1)", curses.A_BOLD)
                _safe_add(self.scr, 0, max(2, (w - 23) // 2),
                          "General Commands Manual", curses.A_DIM)
                _safe_add(self.scr, 0, max(2, w - 2 - len(name) - 3), f"{name}(1)", curses.A_BOLD)
                _safe_add(self.scr, 1, 0, "─" * w, curses.A_DIM)
            elif self.disguise == "logs":
                cmd = f"$ journalctl -u {_slug(work_title)}.service -f --no-pager"
                _safe_add(self.scr, 0, 2, cmd[: w - 4], curses.A_BOLD)
            else:
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
            if self.disguise == "docs":
                status = f" Manual page {_man_name(work_title)}(1) line {top + 1} "
                _safe_add(self.scr, h - 1, 0, status.ljust(w - 1), curses.A_REVERSE)
            elif self.disguise == "logs":
                pass  # raw scrolling logs — no chrome
            else:
                hint = " ↑/↓ scroll · Space page · ←/→ p/n chapter · t contents · q library "
                _safe_add(self.scr, h - 1, 0, hint[: w - 1].center(w - 1), curses.A_REVERSE)
            self.scr.refresh()

            c = self.scr.getch()
            if c == -1:
                fresh_nav()  # idle tick: pick up chapters the crawler just gathered
                continue
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
                if next_id is None:
                    fresh_nav()  # maybe the next chapter was just gathered
                if next_id is not None:
                    save()
                    return next_id
                else:
                    self._flash("No next chapter gathered yet — it may still be downloading.")
            elif c in (curses.KEY_LEFT, ord("p")):
                if prev_id is not None:
                    save()
                    return prev_id
                else:
                    self._flash("You're at the first chapter.")
            elif c == ord("t"):
                picked = self.toc(work_id, chapter_id)
                if picked is not None and picked != chapter_id:
                    save()
                    return picked
            elif c == ord("d"):
                self._cycle_disguise()  # off → docs → logs (inconspicuous mode)
                relayout()
            elif c in (ord("q"), 27):
                save()
                return None
            elif c == curses.KEY_RESIZE:
                relayout()

    # ---- table of contents ----
    def toc(self, work_id, current_id):
        sel = -1
        top = 0
        while True:
            # Re-query each tick so chapters the crawler gathers appear live.
            chapters = self.q(lambda db: _fetched_chapters(db, work_id), default=[]) or []
            if sel < 0:
                sel = next((i for i, c in enumerate(chapters) if c[0] == current_id), 0)
            sel = max(0, min(sel, len(chapters) - 1)) if chapters else 0
            h, w = self.scr.getmaxyx()
            body_h = h - 4
            if sel < top:
                top = sel
            elif sel >= top + body_h:
                top = sel - body_h + 1
            self.scr.erase()
            _safe_add(self.scr, 0, 2, f"Contents  ({len(chapters)} ready)", curses.A_BOLD)
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
            if c == -1:
                continue  # idle tick: re-query (new chapters appear)
            if c in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif c in (curses.KEY_DOWN, ord("j")):
                sel = min(len(chapters) - 1, sel + 1)
            elif c in (curses.KEY_NPAGE,):
                sel = min(len(chapters) - 1, sel + body_h)
            elif c in (curses.KEY_PPAGE,):
                sel = max(0, sel - body_h)
            elif c in (curses.KEY_ENTER, 10, 13):
                if chapters:
                    return chapters[sel][0]
            elif c in (ord("q"), 27, ord("t")):
                return None

    def _flash(self, msg: str):
        h, w = self.scr.getmaxyx()
        _safe_add(self.scr, h - 1, 0, (" " + msg).ljust(w - 1), curses.A_REVERSE)
        self.scr.refresh()
        curses.napms(1100)


# --------------------------------------------------------------------------- entry
def _db_url(path: str) -> str:
    return f"sqlite:///{os.path.abspath(os.path.expanduser(path))}"


def _print_users() -> None:
    from .db import SessionLocal

    db = SessionLocal()
    try:
        users = list(db.scalars(select(User).order_by(User.id)).all())
    finally:
        db.close()
    if not users:
        print("No accounts yet. Create one in the web app (first-run Setup), then re-run.")
        return
    print("Accounts (use: shelfcli --user NAME):")
    for u in users:
        flags = []
        if u.role == "admin":
            flags.append("admin")
        if not u.is_active:
            flags.append("disabled")
        print(f"  {u.username}" + (f"  [{', '.join(flags)}]" if flags else ""))


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="shelfcli",
        description="Shelf terminal reader — browse and read your library; progress syncs "
                    "with the web app.",
    )
    ap.add_argument("-u", "--user", metavar="NAME",
                    help="act as this account (default: the first admin)")
    ap.add_argument("--db", metavar="PATH",
                    help="path to the Shelf database file (default: the server's)")
    ap.add_argument("--list-users", action="store_true", help="list accounts and exit")
    ap.add_argument("-d", "--disguise", choices=("docs", "logs"), default="off",
                    help="start in an inconspicuous mode that looks like documentation "
                         "(docs) or terminal logs (logs); toggle in-app with 'd'")
    args = ap.parse_args()

    # --db must take effect before the DB engine is created at import, so re-exec once
    # with the env set (the engine in app.db is built from the environment).
    if args.db and os.environ.get("SHELF_DATABASE_URL") != _db_url(args.db):
        os.environ["SHELF_DATABASE_URL"] = _db_url(args.db)
        os.execv(sys.executable, [sys.executable, "-m", "app.cli", *sys.argv[1:]])

    from .config import get_settings
    from .db import init_db

    # Refuse to silently create an empty DB. The default database_url is the
    # *relative* "sqlite:///./shelf.db" — when neither --db nor SHELF_DATABASE_URL
    # is set, init_db() would happily create an empty shelf.db in the current
    # directory, and the CLI would then show an empty library while the real
    # server DB lives elsewhere. Catch that case before init_db() does any work.
    db_url = get_settings().database_url
    if not args.db and not os.environ.get("SHELF_DATABASE_URL") \
            and db_url.startswith("sqlite:///"):
        db_path = db_url[len("sqlite:///"):]
        if db_path and not os.path.exists(db_path):
            abs_path = os.path.abspath(db_path)
            print(
                f"shelfcli: no database at {abs_path}.\n"
                "  You're not pointing at the server's library. Either run the\n"
                "  installer's wrapper (/usr/local/bin/shelfcli), pass --db explicitly\n"
                "  (shelfcli --db /path/to/shelf.db), or set SHELF_DATABASE_URL.",
                file=sys.stderr,
            )
            sys.exit(2)

    try:
        init_db()  # ensure the database we point at is present + migrated (multi-user schema)
    except Exception as exc:  # noqa: BLE001
        print(f"shelfcli: cannot open the database at {get_settings().database_url}\n  {exc}",
              file=sys.stderr)
        sys.exit(1)

    if args.list_users:
        _print_users()
        return

    try:
        curses.wrapper(lambda s: TUI(s, username=args.user, disguise=args.disguise).library())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
