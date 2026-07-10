# Shelf — a quiet, self-hosted reader for long-form fiction & comics

[![CI](https://github.com/ShogyX/Shelf/actions/workflows/ci.yml/badge.svg)](https://github.com/ShogyX/Shelf/actions/workflows/ci.yml)
[![CodeQL](https://github.com/ShogyX/Shelf/actions/workflows/codeql.yml/badge.svg)](https://github.com/ShogyX/Shelf/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Shelf is a self-hosted media library for **books, comics/manga, web serials, and
audiobooks**. It pairs a first-class reader (full typography control, many color themes,
a dedicated comic/webtoon viewer) and a gapless audiobook player with a shared library,
per-user reading/listening progress, a **polite, rights-respecting ingestion engine**,
and an **acquisition pipeline** (Prowlarr→SABnzbd usenet, qBittorrent, Anna's Archive)
that fetches requested titles — in every configured language (EN/NO) and both formats
(read + listen) — into a shared, operator-curated **stock pool**. There's also a terminal
reader (`shelfcli`) and an **Audiobookshelf-compatible API** so mobile ABS clients (e.g.
*Still*) can log in, browse, read, and listen natively.

The library is shared across accounts; each user keeps their own progress, reader
settings, bookshelves, and Kindle/email delivery. The UI is localized (English + Norsk).

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
  automation: **auto-Kindle** (email new chapters as they arrive) and **notify** (a push
  notification when a title is added). New chapters are gathered automatically for every
  actively-releasing title in your library — there's no per-shelf toggle for it.
- **Send to Kindle / export** — every work has a 📤 action: download a generated EPUB
  (or CBZ for comics), or email it to your Kindle over SMTP.
- **Resume everywhere** — progress auto-saves the exact chapter + paragraph and syncs
  between the web app and `shelfcli`. A "Continue reading" rail puts you back in one click.
- **Listen** — audiobooks live in a shared pool every account can play: a persistent
  mini-player with chapters, sleep timer, playback rate, resume-everywhere positions, and
  automatic AAC transcode for formats a browser can't decode. Titles show **variant
  badges** (📖/🎧 × EN/NO) so you can see at a glance which languages and formats are
  in stock.
- **Acquire & stock** — request any catalogued title ("Acquire") and Shelf fetches it
  through the configured route order: torrents (qBittorrent + VirusTotal gate), the
  usenet pipeline (Prowlarr→SABnzbd, shared-downloader-aware), or Anna's Archive —
  with ranked candidate cascades, content verification against embedded metadata, and
  automatic retry ledgers per title/format. Every acquisition expands to **all
  configured content languages × both formats**. Admins pre-fetch whole genres into a
  shared **stock pool** so popular titles open instantly.
- **Track & import lists** — follow external lists (Goodreads, AniList, Hardcover,
  Open Library, MAL, Amazon wishlists): new entries are auto-acquired to your library
  or **straight into stock** (admin), with optional whole-series fetch + series follow.
  A **Wanted** dashboard tracks every open request and what's blocking it.
- **Self-maintaining library** — background integrity scans (missing/corrupt files with
  automatic stock re-fetch), language-aware dedup (one EN + one NO per format), junk
  filtering (study guides / summaries / self-labelled fan-fiction spin-offs), cover
  localization, series healing, and scheduled backups with selective restore.
- **Audiobookshelf-compatible API** — point an ABS mobile client at your Shelf URL and
  log in: libraries, browse, search, collections (=bookshelves), streaming with
  transcode fallback, offline progress sync, and the ereader position all round-trip.
- **Multi-account & access-gated** — every route requires a login; per-user category
  permissions and an 18+ opt-in gate what each account can browse; an admin role
  manages users, sources, acquisition, and crawl settings.

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
5. **Organize** works onto **bookshelves** and turn on auto-Kindle / notify (new chapters are
   gathered automatically — no per-shelf toggle needed).
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

The `PoliteFetcher` reads `robots.txt`, identifies itself honestly with a User-Agent + contact
address (editable by an admin under **Settings → Crawl identity**, or via `SHELF_USER_AGENT` /
`SHELF_CONTACT_EMAIL`), rate-limits per source (min interval + daily cap), and backs off on 429/5xx. JS-heavy / anti-bot sources
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
from **localhost** (or an SSH tunnel) before the address is public — over a public connection,
first-admin setup is **refused (403) unless `SHELF_SETUP_TOKEN` is set**, so a stranger can't claim
admin on a freshly-exposed instance. The app itself enforces PBKDF2
hashing, login brute-force lockout, Secure/httpOnly/SameSite cookies, security headers
(CSP/HSTS/`X-Frame-Options`), a `Host` allow-list, and disabled API docs. Full step-by-step:
**[`deploy/cloudflare-tunnel.md`](deploy/cloudflare-tunnel.md)**.

## Architecture & tech stack

```
React + TS SPA ──REST──▶ FastAPI ─┬─ ingestion engine ──── SQLite (SQLAlchemy 2.x)
  Discover / Browse / Library      │   AdapterRegistry (compliance-gated crawl)
  Reader (prose + comic)           │   PoliteFetcher (robots, rate-limit, CF-solver tiers)
  Audio player / Wanted / Stock    │   CrawlScheduler + ~40 maintenance ticks (APScheduler)
  Settings / Users / Jobs          ├─ acquisition: Prowlarr→SABnzbd · qBittorrent(+VirusTotal) ·
ABS clients (Still) ──ABS API──▶   │      Anna's Archive · release matcher + verify + ledgers
shelfcli (terminal) ──shared DB──▶ └─ integrations: Readarr · Kapowarr · Audiobookshelf ·
                                        Storyteller · Hardcover/OpenLibrary/GoogleBooks/AniList
```

- **Backend:** FastAPI · SQLAlchemy 2.x · SQLite (WAL, invariant triggers) · httpx ·
  BeautifulSoup · ebooklib · Pillow · APScheduler · Alembic · Pydantic v2 · ffmpeg/ffprobe
  (audio probe + transcode) · Playwright/zendriver (optional, for `render_js` sources).
- **Frontend:** React 18 · TypeScript · Vite · Tailwind · TanStack Query · Zustand ·
  react-i18next (EN + NO).

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

## Configuration

Settings live in two places, and there's no need to restart for most of them:

- **`backend/.env` or `SHELF_*` environment variables** — boot/built-in defaults.
  Copy [`backend/.env.example`](backend/.env.example) to `backend/.env` and edit. It's a
  full, commented reference of every option (network, storage paths, auth/security,
  crawl politeness, backups, SMTP). The example contains **no secrets** — never commit
  your real `.env` (it's gitignored).
- **Settings → System / Storage (in the web UI, admin)** — most behavioral knobs are
  editable at runtime and honored immediately, defaulting to the env/built-in value
  until you change them. Boot- and security-critical vars (host/port, database, CSP,
  trusted proxy, cookies, setup token) stay in the environment by design.

## Contributing

PRs welcome — see **[`CONTRIBUTING.md`](CONTRIBUTING.md)** for local setup and the
test/lint commands CI runs (`pytest` + `npm run build`). Please keep changes focused,
keep secrets out of commits, and respect the sourcing/compliance policy above.

## Security

Found a vulnerability? **Don't open a public issue** — report it privately via GitHub
Security Advisories. See **[`SECURITY.md`](SECURITY.md)** for the policy and the
self-hosting hardening checklist. Deployment-side hardening is summarized under
"Exposing it on the internet" above; repo-side settings (branch protection, code/secret
scanning) are in **[`docs/repo-configuration.md`](docs/repo-configuration.md)**.

## License

[MIT](LICENSE) © ShogyX. Note that the license covers Shelf's **code**; it does not grant
any rights to content you ingest — that remains governed by each source's terms and the
operator-controlled sourcing policy described above.
