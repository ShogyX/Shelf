# Shelf — a quiet, self-hosted reader for long-form fiction & comics

Shelf is a self-hosted web app for reading serialized fiction, light novels, and
comics/manga. It pairs a first-class reader (full typography control, many color
themes, a dedicated comic/webtoon viewer) with a shared library, per-user reading
progress, and a **polite, rights-respecting ingestion engine** that slowly pulls
works from sources you're permitted to read. There's also a terminal reader
(`shelfcli`) that shares the same library and progress.

The library is shared across accounts; each user keeps their own progress, reader
settings, bookshelves, and Kindle/email delivery.

## What it offers

- **Read anything long-form** — light novels, web serials, public-domain books, and
  comics/manga. Prose gets a distraction-free reader: many eye-friendly color themes,
  font/size/spacing/width controls, scroll **or** paginated mode, focus mode, and a
  draggable floating control. Comics get a dedicated viewer with webtoon (continuous)
  or single-page modes, fit/zoom, and position memory.
- **Discover & browse** — a Netflix-style Index with Most-Popular and genre/theme lanes
  plus a full **Browse** catalog (search, filter by media/genre, sort by popularity),
  built from cross-source metadata enrichment (covers, synopsis, genres, chapter counts,
  popularity).
- **Per-user library & bookshelves** — organize works onto shelves with optional
  automation: **auto-update** (keep gathering new chapters), **auto-Kindle** (email new
  chapters as they arrive), and **notify** (a push notification when a title is added).
- **Send to Kindle / export** — every work has a 📤 action: download a generated EPUB
  (or CBZ for comics), or email it to your Kindle over SMTP.
- **Resume everywhere** — progress auto-saves the exact chapter + paragraph and syncs
  between the web app and `shelfcli`. A "Continue reading" rail puts you back in one click.
- **Multi-account & access-gated** — every route requires a login; an admin role manages
  users, sources, and crawl settings.

## Quick install (fire-and-forget)

From the project directory:

```bash
./install.sh
```

It installs everything the app needs regardless of what's on the host: it finds (or
installs/provisions) Python ≥ 3.11 and Node ≥ 18, sets up the virtualenv and a headless
Chromium (Playwright, for JS-heavy / anti-bot sources), builds the
web UI, and runs the app as a `systemd` service on a free port. Fallback toolchains live
under `.toolchain/`. **Re-run any time** to update and restart; it backs up your database
first and is fully idempotent. Overrides:

```bash
SHELF_PORT=9000 ./install.sh     # preferred starting port (auto-bumps if busy)
SHELF_USER=alice ./install.sh    # run the service as this user
SHELF_NO_RENDER=1 ./install.sh   # skip Chromium (JS/anti-bot sources won't crawl)
```

Manage it afterwards with `systemctl status shelf.service` and
`journalctl -u shelf.service -f`. Build a distributable archive with
`./scripts/package.sh` (excludes the venv, `node_modules`, builds, and the database).

### Read in the terminal

The installer also adds **`shelfcli`**, a terminal reader sharing the same database
and progress as the web app:

```bash
shelfcli                 # browse your library, open a title, resume where you left off
shelfcli --user alice    # act as a specific account (default: the first admin)
```

Keys: `↑/↓` move · `Enter` open · `/` search · `Space` page · `←/→` chapter · `t` contents
· `q` back. Press `d` for **inconspicuous mode** — re-skin the reader as man-page docs or
streaming logs (reading position is preserved).

## Using it

1. **Add a work** — pick a source, enter a reference (a book id, an ebook URL/slug,
   a feed/index URL, a title…) and **Hook** it. Or **import** an EPUB/TXT/MD you own.
2. **Browse** the Index for popular/genre lanes, or open **Browse** to search and filter the
   whole catalog.
3. A **slow backfill** drains in the background within each source's rate budget (watch it on
   **Jobs**), resuming after restarts. The library card shows live "gathered / total" progress.
4. **Read** in the web reader or `shelfcli`; progress syncs both ways.
5. **Organize** works onto **bookshelves** and turn on auto-update / auto-Kindle / notify.
6. **Send to Kindle / export** with the 📤 action.

### Accounts

First boot shows a **Setup** screen to create the initial admin. More accounts are added on
the **Users** page (admin-only). Sessions are HTTP-only cookies lasting 30 days
(`SHELF_SESSION_DAYS`).

### Send to Kindle setup

EPUB/CBZ **download** always works. For **email delivery**, enter your SMTP login in
**Settings → Send to Kindle / email** (no restart needed; the password is stored locally and
never returned by the API), or configure it on the server via `SHELF_SMTP_*` env vars. To
deliver to a Kindle, allow-list the From address in your device's approved-sender settings,
then set your Kindle delivery address in Settings.

## Sourcing policy (read first)

Shelf does **not** ship a scraper for arbitrary sites. Ingestion is a **pluggable adapter
system**; each adapter carries a `ComplianceDeclaration` (license basis, ToS-permitted flag,
robots.txt respected). **The engine refuses — as a hard error — to ingest any source whose
`tos_permitted` flag is off.** That flag is an operator toggle on the **Sources** page: enable
a source only for content you have the right to read.

| Adapter | Ingests | Default |
|---|---|---|
| Public-domain libraries | Public-domain / CC0 books | enabled |
| Local files | EPUB / TXT / MD / PDF / CBZ / CBR you own | enabled |
| Web index | A chapter-index page on a site you may read | enabled |
| Generic feed | RSS/Atom/OPDS or an adaptive web crawl | enabled, **attest first** |
| Manga / light-novel sites | Image- or text-based serials from accounts you hold | enabled, **attest first** |
| Unverified-ToS sources | — | **disabled & stubbed** |

The `PoliteFetcher` reads `robots.txt`, identifies itself with a contact address, rate-limits
per source (min interval + daily cap), and backs off on 429/5xx. JS-heavy / anti-bot sources
use a per-source **headless browser** (`render_js`) for *passive* challenges only — it does
**not** defeat interactive/managed challenges (e.g. CAPTCHA-style "just a moment" interstitials);
those sites aren't supported.

## Exposing it on the internet

Before going public, harden the install:

```bash
SHELF_TUNNEL=1 ./install.sh   # bind 127.0.0.1, Secure cookies, trust proxy
```

Then front it with a **reverse-proxy tunnel** (no inbound ports) and ideally an
**access-control layer** so only people you allow reach the login page. Create the first admin
**before** the tunnel is public (or set `SHELF_SETUP_TOKEN`). The app itself enforces PBKDF2
hashing, login brute-force lockout, Secure/httpOnly/SameSite cookies, security headers
(CSP/HSTS/`X-Frame-Options`), a `Host` allow-list, and disabled API docs. Full step-by-step:
**[`deploy/cloudflare-tunnel.md`](deploy/cloudflare-tunnel.md)**.

## Architecture & tech stack

```
React + TS SPA  ──REST──▶  FastAPI ── ingestion engine ── SQLite (SQLAlchemy 2.x)
  Library / Browse           routers: works, chapters, reading,   AdapterRegistry (compliance-gated)
  Reader (prose + comic)              catalog, sources, jobs,      PoliteFetcher (robots, rate-limit)
  Bookshelves / Settings              delivery, users, settings    CrawlScheduler (APScheduler)
```

- **Backend:** FastAPI · SQLAlchemy 2.x · SQLite · httpx · BeautifulSoup · ebooklib · Pillow ·
  APScheduler · Alembic · Pydantic v2 · Playwright (optional, for `render_js`).
- **Frontend:** React 18 · TypeScript · Vite · Tailwind · TanStack Query · Zustand.

## Developing

```bash
# Backend
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,render,rar]"   # render = Playwright; rar = CBR support
alembic upgrade head                 # or let the app create tables on boot
python -m app                        # serves API + built UI on 0.0.0.0:8000
pytest                               # politeness + sanitizer + ingestion units

# Frontend (dev only — production serves the built dist from FastAPI)
cd frontend && npm install && npm run dev   # :5173, proxies /api → :8000
```

Config is via `SHELF_*` env vars (see `app/config.py`) — e.g. `SHELF_HOST`, `SHELF_PORT`,
`SHELF_DATABASE_URL`, `SHELF_CONTACT_EMAIL`, `SHELF_SCHEDULER_TICK_SECONDS`, `SHELF_SMTP_*`.
After `pip install -e .[render]`, run `playwright install chromium` once for `render_js`.
