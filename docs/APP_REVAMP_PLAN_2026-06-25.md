# Shelf App Revamp Plan — 2026-06-25

Persistent plan (survives sessions) for the 9-point revamp request. Grounded by 4 read-only sub-agent
investigations. **Process rules (from the user): UI work uses the design skill; code work is delegated
to sub-agents; the final review uses MULTIPLE sub-agents.** Each wave: implement → verify (build/tests
+ read-only browser on localhost:8000) → commit. Prod-safety: no destructive prod-DB ops without an
explicit gate + backup; local-only (no external calls) during dev/review.

Status legend: ☐ todo · ◐ in progress · ☑ done

---

## Wave 1 — Background chrome / ambient (UI · design skill) ☐
**Problem:** ambient background is `fixed inset-0 -z-10` (App.tsx:459-461) → only covers the viewport,
so on any page taller than the viewport a **hard border/seam** appears when scrolling past 100vh; also
it's static (not animated). All main pages affected.
**Fix:**
- Make the ambient cover the FULL scroll height seamlessly (e.g. `position:fixed` layer that truly
  pins to viewport + an opaque themed base so there's no transparent seam; or `background-attachment`
  on a root layer). Verify no seam on a long Library/Discover/Watchlist/Settings page.
- Add a SUBTLE animation (slow drift/pulse of the accent radials via `@keyframes`, `prefers-reduced-motion`
  respected). Token-driven (index.css `--ambient`), tracks all 14 palettes.
**Files:** `src/App.tsx` (459-461), `src/index.css` (`--ambient` light 51-55 / dark 77-81).
**Verify:** scroll each main page in browser (light + dark) — no seam, smooth ambient, animated.

## Wave 2 — Discover hero: rotate + high-res + books-mainly (UI + small backend) ☐
**Problem:** `src/pages/Index.tsx:66-68` picks `rows.data[0].items[0]` — deterministic (never rotates),
any media kind, whatever cover the source gave (often low-res).
**Fix:**
- Rotate the featured title (randomize / cycle on load; ideally auto-rotate every N s).
- Prefer BOOKS (media_kind text/book) for the hero; fall back gracefully.
- Use a proper HIGH-RES cover. Backend: add/extend an endpoint (e.g. `/catalog/featured`) that returns
  a small rotated set of book groups with the best available cover; or filter client-side from rows +
  request a larger cover variant. Avoid blurry upscales.
**Files:** `src/pages/Index.tsx` (66-68 selection, ~170-219 hero render), maybe `app/routers/index.py`
(catalog_rows / a new featured endpoint), `src/components/Cover.tsx` (high-res variant).
**Verify:** Discover hero shows a book with a crisp cover; reloads show different titles.

## Wave 3 — Library audiobook category row (UI) ☐
**Problem:** `src/components/LibraryHome.tsx:132-138` only has "Audiobooks **in progress**" (continue-
listening). No general Audiobooks category row.
**Fix:** add an "Audiobooks" rail (all audiobook works in the library / available), reusing Rail +
CoverCard (`kind="audio"`). Backend: a works query filtered to audio media_kind if not already exposed.
**Files:** `src/components/LibraryHome.tsx`, `src/api/client/*` (works-by-kind), maybe `app/routers/works.py`.
**Verify:** Library home shows an Audiobooks row when audio content exists.

## Wave 4 — Legacy "browse all" pages → current design (UI · design skill) ☐
**Problem:** "Browse all"/"See all" + genre "Browse →" lead to `BrowseLibrary` (`/library/browse`) and
`BrowseCatalog` (`/browse/:dimension/:value`) which look like the OLD dense UI, not the premium redesign.
Redirects exist (/jobs→/sources, /imports→/sources, /missing,/following→/watchlist, /index→/discover).
**Fix:**
- Restyle BrowseLibrary + BrowseCatalog to match the current premium design (full-bleed header, rails/
  poster grid consistent with home, tinted chrome). Keep multi-select where it's genuinely used.
- Audit redirected legacy routes — keep redirects for old bookmarks, remove any dead old-page code.
- Remove/hide redundant settings + info surfaced on these pages.
**Files:** `src/pages/BrowseLibrary.tsx`, `src/pages/BrowseCatalog.tsx`, `src/components/LibraryGrid.tsx`,
`src/App.tsx` (routes), `src/components/catalog/CatalogRows.tsx` (Browse → links).
**Verify:** the browse pages visually match home; no old-UI look; redirects still resolve.

## Wave 5 — List-import pagination for ALL providers (backend · sub-agent) ☐
**Problem:** Goodreads import of 76k titles resolved only ~100 — `_goodreads` (`list_import.py:145-165`)
does a single RSS GET with no pagination loop. AniList (single GraphQL) + Hardcover (`limit:1000`) also
lack true pagination. (OpenLibrary, MAL, Amazon already paginate.)
**Fix:** add pagination to EVERY provider:
- Goodreads RSS supports `&page=N` (returns ~100/page) — loop pages until an empty/short page (verify live).
- AniList: page through `Page(page:N, perPage:50)`. Hardcover: offset/limit loop. Cap with a sane max
  + the daily/total caps from Wave 7.
**Files:** `app/ingestion/list_import.py` (each provider fetcher).
**Verify:** unit test each provider's pagination loop (mock multi-page); a large Goodreads shelf returns >100.

## Wave 6 — List caching + bandwidth-light change-scan (backend · sub-agent) ☐
**Problem:** list items are NOT stored; `sync_list` re-fetches + re-resolves the whole external list every
tick (`list_import.py:416-489`); covers re-resolved on `GET /list-imports/{id}/items`. Slow + wasteful.
**Fix:**
- Persist imported list items (titles/refs/cover_url) on import (new table or JSON column on
  ListSubscription) so the UI serves them from cache (fast loads, no re-resolve).
- Periodic `list_sync_tick`: fetch ONLY the lightweight list (titles/refs), DIFF vs cached keys, and act
  on additions/removals — do NOT resolve images during the scan (save bandwidth). Image/work resolution
  happens lazily (on add to library / stock), not on every scan.
**Files:** `app/ingestion/list_import.py`, `app/routers/list_imports.py`, `app/models.py` (storage),
`app/ingestion/scheduler.py` (list_sync_tick), migration.
**Verify:** import → items load instantly from cache; sync scan makes no image fetches (assert via logs/test).

## Wave 7 — Stocking → Sources tab (backend + UI · sub-agent + design skill) ☐
**Problem:** standalone `/stock` page (`src/pages/Stock.tsx`); user wants it folded into Sources, compact,
with new capabilities.
**Fix:**
- Remove the `/stock` route/page; fold a compact Stocking section into `SourcesHub.tsx` (config + rate
  limits + queue + batches + list-feeds), reusing extracted Stock components.
- **Lists as input:** surface ListSubscriptions with `to_stock=True` (already wired in `sync_list`).
- **Daily caps:** new AppSettings `stock_searches_per_day` / `stock_downloads_per_day`; enforce in
  `stock.py:stock_tick` (count today's stock searches/grabs, gate the batch); show usage in UI.
- **Entire-catalog input:** option to stock the whole catalog via `_select_groups(media=None,...)` with a cap.
- **Exclude web_index:** option to skip crawled rows — filter `_select_groups` to groups with ≥1 member
  whose `provider != 'web_index'`. Thread through `queue_stock` → `queue_selection` → `_select_groups`.
**Files:** backend `app/ingestion/stock.py`, `app/routers/stock.py`, `app/schemas.py`, `app/config.py`,
`app/ingestion/scheduler.py`; frontend `src/pages/SourcesHub.tsx`, extract from `src/pages/Stock.tsx`,
`src/App.tsx` (remove route), `src/api/client/stock.ts`.
**Verify:** Sources tab has compact stocking; per-day caps enforced (test); entire-catalog + exclude-web
options work; /stock route gone (redirect to /sources for old links).

## Wave 8 — Fix automated backup (backend · sub-agent) ☐
**Problem:** `auto_backup_last_at` advances (Jun 25) but the newest backup FILE on disk is Jun 20, and the
UI lists none new. The tick marks last-run WITHOUT producing a file (or writes to a dir the UI doesn't
scan / `start_build()` fails async silently).
**Fix:** trace `scheduled_backup_tick` (scheduler.py:1293-1341) → `backups_store.start_build()`: confirm
it (a) actually writes a file, (b) to the SAME dir+pattern the UI's `GET /admin/backups` scans, (c)
doesn't update last_at before a successful build, (d) surfaces errors. Fix the root cause; ensure a new
backup appears in the UI after a tick. (Prior "auto-backup stall fixed" may have regressed.)
**Files:** `app/backup.py`, `app/ingestion/scheduler.py` (scheduled_backup_tick), `app/routers/backup.py`,
`src/pages/Settings.tsx` BackupPanel.
**Verify:** force a due backup → a new .bak appears on disk AND in the Settings Backup UI; last_at only
advances on success.

## Wave 9 — Remove unused legacy endpoints (backend · sub-agent) ☐
**Problem:** ~216 routes; some old-UI endpoints likely dead. Candidates (verify each is truly uncalled by
the frontend AND not an external/webhook): DELETE /catalog/{id}, DELETE /index/blocks/{id}, DELETE
/queued-hooks/{id}, POST /jobs/reap, POST /downloads/clear, etc.
**Fix:** for each candidate, grep the frontend API client + confirm no caller, confirm not external →
remove route + now-dead handler/helpers. Conservative: when in doubt, keep.
**Files:** `app/routers/*.py`; cross-ref `src/api/client/*.ts`.
**Verify:** tests pass; frontend build + browser smoke shows no broken calls.

## Wave 10 — Final multi-agent review (MULTIPLE sub-agents) ☐
Run several sub-agents in parallel over the whole changed surface: (1) code-reviewer on the full diff,
(2) karen for end-to-end stability/functionality (read-only browser), (3) ui-designer/frontend review of
the visual changes, (4) a backend-architect pass on the list/stock/backup changes. Fix every confirmed
finding. Local-only, no prod mutations.

---

### Sequencing
UI waves (1,2,3,4) can interleave with backend waves (5,6,8,9); Wave 7 spans both. Recommended order:
**8 (backup, isolated) → 5 (pagination) → 6 (caching) → 1 (ambient) → 2 (hero) → 3 (audiobook row) →
4 (browse redesign) → 7 (stocking move) → 9 (dead endpoints) → 10 (review).** Commit per wave.

---

## ✅ COMPLETE (2026-06-26) — all waves shipped to local main

| Wave | Commit | Status |
|---|---|---|
| 8 backup fix | e2b237d | ☑ force_zip64 + last_at-on-success; new 2 GB backup verified |
| 5 pagination | 9bdc5fb | ☑ Goodreads/AniList/Hardcover paginate + caps (53 tests) |
| 6 list cache + change-scan | e798e67 | ☑ list_subscription_items + migration 0043; scan does zero image resolution |
| 1-4 UI | 56ee99f | ☑ ambient seam-fix+animation, rotating book hero, audiobook rail, premium browse |
| 7a stock backend | 74e2d76 | ☑ daily caps + entire-catalog + exclude-web-index + feeding-lists |
| 7b stock UI | 6a88303 | ☑ folded into Sources; /stock→/sources; standalone page removed |
| 9 dead endpoints | ce9037d | ☑ removed /jobs/reap + /downloads/clear (conservative) |
| 10 review + polish | e4cb9bc | ☑ code-reviewer + karen + ui-designer; fixed de-emoji mobile nav/popovers, hero CTA, dup badge |

**Final review (3 sub-agents):** code-reviewer = "ship it" (no critical/should-fix); karen = stable, all
9 fixes verified end-to-end (backup file produced+listed, hero rotates books, seam gone, stocking in
Sources, browse premium, list cache serving); ui-designer findings all fixed in e4cb9bc. 1035 backend
tests pass throughout.

**Follow-ups for the operator (external/can't verify locally):**
- AniList query-shape change → sanity-check against a real AniList user on the live 76k-style import.
- Goodreads `&page=` pagination → confirm on the real 76k shelf (loop is safe regardless).
- Optional: clear `auto_backup_last_at` to make the next auto-backup fire before its 24h window.
