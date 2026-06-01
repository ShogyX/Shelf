# Shelf — a quiet, self-hosted reader for long-form fiction

Shelf is a single-user web app for reading serialized / long-form fiction with a
first-class native reader (full typography control, light / dark / sepia themes),
library management, reading-progress tracking, and a **pluggable, rights-respecting
ingestion engine** that slowly and politely pulls works from sources you are
permitted to ingest.

> Working title. Rename freely.

---

## Quick install (fire-and-forget)

From the project directory (or after extracting a packaged tarball):

```bash
./install.sh
```

**It provisions its own toolchain**, so it works regardless of what's on the host:
if the system Python is older than 3.11 (or missing) it installs a private CPython
via [`uv`], and if Node.js is older than 18 (or missing) it downloads a private
Node runtime — both kept under `.toolchain/`, nothing system-wide is changed. It
then builds the web UI, creates the virtualenv, installs the app as a `systemd`
service, and starts it on a free port (never colliding with something already
listening). Re-run any time to update and restart. Overrides:

[`uv`]: https://github.com/astral-sh/uv

```bash
SHELF_PORT=9000 ./install.sh     # preferred starting port (auto-bumps if busy)
SHELF_USER=alice ./install.sh    # run the service as this user
```

Build a distributable archive with `./scripts/package.sh` (excludes the venv,
`node_modules`, builds, and the database). Service management afterwards:
`systemctl status shelf.service` · `journalctl -u shelf.service -f`.

> **JS rendering** (Playwright + Chromium, for `render_js` sources) is set up
> **by default** by `install.sh` — the browser and its OS libraries are installed
> into `.toolchain/` and wired into the service. Skip it with `SHELF_NO_RENDER=1
> ./install.sh`. For a manual (pip) install instead of `install.sh`:
> `pip install -e .[render] && playwright install chromium`.

### Read in the terminal

The installer also adds **`shelfcli`** — a terminal reader that shares the same
database (and reading progress) as the web app:

```bash
shelfcli      # browse the library, open a title, pick up where you left off
```

Keys: `↑/↓` move · `Enter` open · `/` search · `Space` page · `←/→` (or `p`/`n`)
prev/next chapter · `t` contents · `q` back. Stop reading in the terminal and the
browser resumes at the same spot — and vice-versa.

---

## Sourcing policy (read first)

Shelf does **not** ship a scraper for arbitrary sites. Ingestion is a **pluggable
adapter system**. Every adapter carries a `ComplianceDeclaration` describing its
legal basis (license, ToS-permitted flag, robots.txt respected). **The engine
refuses — as a hard error, not a warning — to ingest any source whose
`tos_permitted` flag is off.** That flag is an operator toggle on the Sources page:
turn a source on only for content you have the right to read.

Adapters shipped:

| Adapter | Basis | Default |
|---|---|---|
| `gutenberg` | Project Gutenberg, public domain | enabled |
| `standardebooks` | Standard Ebooks, public domain / CC0 | enabled |
| `local_import` | EPUB / TXT / Markdown you legally own | enabled |
| `generic_feed` | User-supplied RSS/Atom/OPDS **or** a chapter-index page; adaptive web extraction | **disabled** — requires you to enable it *and* attest you are permitted |
| `royalroad` | Unverified ToS | **disabled & stubbed** — do not enable without a documented basis |

The `PoliteFetcher` always reads `robots.txt` first, identifies itself honestly with
a contact address, rate-limits per source (min interval + daily cap), and backs off
on 429/5xx (honouring `Retry-After`). The slow-crawl scheduler exists to be a *good
citizen* to permitted sources — not to evade detection on sources you shouldn't touch.

---

## Architecture

```
React + TS SPA  ──REST──▶  FastAPI
  Library                    routers: works, chapters, reading, sources, jobs, settings
  Reader (typography)        services: sanitize, reader-content, progress
  Sources / Jobs / Settings  ingestion engine:
                               AdapterRegistry → SourceAdapter (compliance-gated)
                               PoliteFetcher (robots, rate-limit, backoff)
                               CrawlScheduler (APScheduler; slow, resumable backfill)
                                     │
                               SQLite (SQLAlchemy 2.x)
```

## Tech stack

- **Backend:** FastAPI · SQLAlchemy 2.x · SQLite (Postgres-ready) · httpx · BeautifulSoup ·
  feedparser · ebooklib · APScheduler · Alembic · Pydantic v2.
- **Frontend:** React 18 · TypeScript · Vite · Tailwind (CSS-variable theme tokens) ·
  TanStack Query · Zustand.

---

## Running it

### Backend

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# schema: either let the app create tables on boot, or use migrations:
alembic upgrade head

python -m app.seed          # optional: seed a demo work for offline UI dev

python -m app               # binds 0.0.0.0:8000 (SHELF_HOST / SHELF_PORT)
# or, for autoreload during dev:
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API is served under `/api` (health at `GET /api/health`) and listens on all
interfaces (`0.0.0.0`) by default. Config is via `SHELF_*` env vars (see
`app/config.py`) — e.g. `SHELF_HOST`, `SHELF_PORT`, `SHELF_DATABASE_URL`,
`SHELF_CONTACT_EMAIL`, `SHELF_SCHEDULER_TICK_SECONDS`, `SHELF_CHAPTERS_PER_TICK`.

### Frontend

```bash
cd frontend
npm install
npm run dev      # listens on 0.0.0.0:5173, proxies /api → :8000
```

### Tests

```bash
cd backend && . .venv/bin/activate && pytest          # politeness + sanitizer units
cd frontend && npx playwright test                    # read-and-resume e2e (see e2e/)
```

### Production / always-on (systemd)

In production a **single service** serves both the API and the built SPA on port 8000
(FastAPI mounts `frontend/dist`), so there's nothing to proxy. Build the frontend, then
install the unit:

```bash
cd frontend && npm install && npm run build           # produces frontend/dist
sudo install -m 644 deploy/shelf.service /etc/systemd/system/shelf.service
sudo systemctl daemon-reload
sudo systemctl enable --now shelf.service             # starts now + on every boot
systemctl status shelf.service                        # check it's active
journalctl -u shelf.service -f                        # follow logs (incl. crawl ticks)
```

The unit (`deploy/shelf.service`) runs `python -m app` from the venv with
`Restart=always`, binds `0.0.0.0:8000`, and points Playwright at the browser cache for
`render_js`. After changing code or rebuilding the frontend: `sudo systemctl restart
shelf.service`. Override host/port/DB via `Environment=SHELF_*` lines in the unit (or a
drop-in: `systemctl edit shelf.service`). Open `http://<host>:8000/`.

---

## Using it

1. **Add → pick a source → enter a reference → Hook.** For Gutenberg, the reference is
   the numeric book id (e.g. `1342` for *Pride and Prejudice*). For Standard Ebooks,
   an ebook URL or `author/title` slug. For a feed/web source, the feed or index URL
   (you must first enable it on **Sources** and attest you're permitted).
2. The work appears in the **Library**; a **slow backfill** job drains in the background
   (watch it on **Jobs**) within the source's rate budget, resuming after restarts.
3. **Read.** A distraction-free reader with:
   - **14 eye-friendly color modes** (Daylight, Paper, Sepia, Solarized, Mist, Sage,
     Charcoal, Midnight, Nord, Gruvbox, Slate, Forest, E-ink…) — all gently toned, none
     pure black/white. Pick from the reader's "Aa" panel, Settings, or the nav chip;
     "Match system" follows your OS. Switches apply instantly to reader + chrome.
   - **Typography controls:** 6 font choices, size/line-height/letter- & paragraph-spacing
     steppers, width presets (Narrow→Full), ragged/justified.
   - **Text & background lightness sliders** — independently tune how light the text and
     the page are (keeps the theme's hue/tint, adjusts only lightness; "Auto" = theme default).
   - **Scroll *or* Paginated** reading (column pager with page indicator + tap zones).
   - A **dockable floating control** (Contents · Focus · Settings) — drag it toward any
     edge to dock left / right / top / bottom (it reorients), and the settings panel
     **falls out right next to it**. Position is remembered.
   - **Text position** slider — place the reading column anywhere from left to right.
   - **Focus mode** — a true full-screen, distraction-free view that shows *only the text*
     and lets you scroll; enter from the floating ⛶ button, the settings panel, or `f`,
     exit with the corner ✕ or `Esc` (uses the Fullscreen API).
   - Top progress bar, per-chapter reading-time, keyboard nav (←/→ pages or chapters,
     space, j/k scroll, t = contents, h = hide bars, f = focus), and a TOC drawer.

The whole app is **mobile-friendly** — responsive nav (horizontally scrollable), 2-column
library, touch-draggable floating reader controls, tap zones for page turns, safe-area
insets, and a reading layout that never overflows narrow screens.
   Progress auto-saves the **chapter + exact paragraph** (robust across font/width
   changes) and restores you to that paragraph on reopen.
4. **Continue reading.** The dashboard shows a "Continue reading" rail of recently-read
   works — cover, current chapter, % through the book — one click resumes exactly where
   you left off.
5. **Import** EPUB/TXT/MD you own directly — no crawl needed.
6. **Send to Kindle / export EPUB.** Every work has a **📤 Send** action: download a
   generated EPUB (with cover + chapters, optional chapter range), or email it straight to
   your Kindle. Set your `@kindle.com` address in **Settings**; the server emails the EPUB
   as an attachment via SMTP (Amazon's official Send-to-Kindle path).
7. **Export** your library + progress as JSON from **Settings**.

### Send to Kindle setup

EPUB **download** always works. To enable **email delivery** (to your Kindle *or* your own
inbox), enter your email provider's SMTP login in **Settings → Send to Kindle / email**
(host, port, username, password, From address, security) — no restart needed. The password
is stored locally and never returned by the API. For Kindle, add the From address to your
Amazon *Approved Personal Document E-mail List*.

Alternatively (or as a fallback) configure SMTP via env on the server:

```
SHELF_SMTP_HOST=smtp.example.com
SHELF_SMTP_PORT=587
SHELF_SMTP_USER=you@example.com
SHELF_SMTP_PASSWORD=app-password
SHELF_SMTP_FROM=you@example.com         # must be Amazon-approved
SHELF_SMTP_STARTTLS=true                # or SHELF_SMTP_SSL=true for port 465
```

Then set your device address (e.g. `name@kindle.com`) in Settings and use **📤 Send** on any work.

---

## JS-heavy & dynamically-paginated sites (`generic_feed`)

Some web-serial sites render content/navigation with JavaScript and don't expose a
fully-enumerable table of contents (the chapter list is loaded via an endpoint their
own robots.txt disallows). Two per-source toggles on the **Sources** page handle this:

- **Headless browser (`render_js`)** — fetch pages with a real Chromium (via Playwright)
  instead of plain HTTP. This renders JS and waits out *passive* anti-bot JS challenges.
  Slower/heavier; enable only for permitted sources that need it.
- **robots.txt (`robots_respected`)** — on by default; can be turned off **for
  dev/troubleshooting on sources you are permitted to read**.

When a source can't be enumerated from its TOC, `generic_feed` switches to **sequential
mode**: it seeds the first chapter and crawls forward, following each page's
"next chapter" link — or, when navigation is JS-routed with no link, by **incrementing
the numeric chapter URL** (e.g. `…/chapter/1` → `…/chapter/2`). It stitches multi-page
chapters and stops when a page yields too little text (end of serial). All of this still
runs through the PoliteFetcher's rate-limit/robots/backoff and the slow scheduler.

Even though chapters can't be enumerated upfront, the novel page usually **advertises a total**
(e.g. a "Chapters (2271)" label). Shelf reads that and shows live **"gathered / total"** progress —
on the Library card (with a "gathering" bar) and in Jobs (e.g. `36 / 2271 chapters gathered · 2%`).
The total self-corrects if crawling passes it and is finalized to the real count when the backfill ends.

> The headless browser handles JS rendering and *passive* challenges. It does **not**
> defeat interactive/managed anti-bot challenges (e.g. Cloudflare Turnstile "Just a
> moment…"); doing so would be detection evasion, which is out of scope. Sites behind a
> managed challenge are not supported.

Setup notes: `playwright install chromium` is required for `render_js` (run once after
`pip install`). The bundled headless Chromium needs no display.

## Cover art

Covers are gathered automatically and shown in the library + Continue Reading rail:
- **Gutenberg** — the book's published cover (`og:image`, with a cache-path fallback).
- **Standard Ebooks** — the edition's `downloads/cover.jpg`.
- **generic_feed / web** — the page's `og:image` (e.g. a web-novel's poster).
- **EPUB import** — the cover image is extracted from the file and served from `/covers/`.

When a work has no artwork (plain-text import, demo data), a tasteful **designed cover**
is generated deterministically from the title — gradient, spine, hairline frame, the
title in serif, and the author — so the shelf always looks intentional. Remote covers
that fail to load fall back to the same generated design.

## Known limitations

- Gutenberg/EPUB chapter splitting is heuristic (heading-based); for a few editions
  front-matter headings can produce extra short "chapters". Content is always intact.
- `generic_feed` adaptive web extraction is best-effort readability, not site-specific.
- Sites behind an interactive/managed anti-bot challenge (e.g. Cloudflare Turnstile) are
  not ingestable — see above.
- Single-user; no auth by default (self-host behind your own trust boundary).
