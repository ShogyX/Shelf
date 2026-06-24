# Shelf — Frontend Redesign Fixes Plan (2026-06-24)

Grounded by 5 parallel read-only investigations (search/data-loading, library/shelves,
sources/imports/add, descriptions-bug root-cause, visual/layout survey). Every item below cites the
real current behaviour + exact files. Execution is wave-by-wave; **each wave gets a specialized
sub-agent review** (ui-designer / frontend-developer / code-reviewer / backend-architect) before
build → demo-verify → commit (branch → `--no-ff` local main), matching the established redesign flow.

Legend: 🔴 user-requested · 🟡 other issue found · files are `frontend/src/...` unless noted.

---

## Wave A — Unified search (nav-driven) 🔴
**Problem.** Three inconsistent searches: the **nav search bar is a dead button** that just routes to
`/discover` with no query (`App.tsx:228-235`); **Library** search is local state feeding `qk.works(q)`
(`Library.tsx:434-462`, in-page input `:550-563`); **Discover** search is the good pattern — URL
`?q=` via `useSearchParams` (`Index.tsx:54-104`).

**Goal.** The nav search is the single input. It drives Library AND Discover via the URL `?q=`. The
in-page search inputs are removed (per the user: "searchbar should be removed since it will be mapped
elsewhere").

**Changes.**
- `App.tsx` nav search: button → controlled `<input>`. On type, set `?q=` on the **current** route
  (Library or Discover); on other routes, navigate to the context's search target. Debounced, `replace`
  history (mirror `Index.tsx:87-104`). Show the input value from the active route's `?q=`.
- `Library.tsx`: replace local `[query,setQuery]` with `useSearchParams` `?q=`; **delete the in-page
  search input** (`:550-563`). Keep `qk.works(q, activeShelf)`.
- `Index.tsx`: keep `?q=` logic; **delete the in-page search input** (`:231`) + the `PageHeader` search
  area (handled visually in Wave C).
- 🟡 Decide nav-search scope: searching from Library filters Library; from anywhere else routes to
  `/discover?q=`. A small affordance ("press Enter to search everywhere") optional.

**Risks.** Library currently also supports shelf-filtered search; ensure `?q=` composes with
`activeShelf`. Browser back/forward must not thrash (use `replace`).

**Verify.** Type in nav on Library → Library filters; on Discover → catalog searches; URL `?q=`
shareable; refresh restores. Review: **frontend-developer** (state/URL correctness).

---

## Wave B — Cached-first data + media 🔴 ("items/media resolve before queries to api")
**Problem.** `main.tsx:8-12` sets a flat `staleTime:2000`, no persistence, no prefetch. Library has no
`placeholderData` (blanks to a skeleton on every return); Discover has `keepPreviousData` (good).
Covers have no skeleton — generative fallback flashes before the real cover (`Cover.tsx:72-94`).

**Goal.** Render from cache first; never show an empty flash when we already had data; warm the cache
before navigation.

**Changes.** (review delta: **localStorage persistence CUT** — it's outside the 17 requests and the
user-visible "resolve before queries" win comes entirely from the in-memory items below.)
- `main.tsx`: raise per-query `staleTime` (works 30s, catalog 5s, work-detail 60s) via options on each
  `useQuery`/`useInfiniteQuery`. No new deps, no persistence.
- `Library.tsx` works query: add `placeholderData: keepPreviousData` so it holds the old grid (the same
  pattern Discover already uses) — this is the core "no empty flash on return" fix.
- Nav pills (`App.tsx pill()` ~`:188-205`): `onMouseEnter` → `qc.prefetchQuery` the destination's
  primary list (works / catalog). Cheap, instant-feel.
- `Cover.tsx`: show a neutral shimmer/skeleton until the `<img>` `onLoad`, then cross-fade (kills the
  generative-then-real flash). Generative stays the final fallback only when there's no URL.

**Risks.** `keepPreviousData` can briefly show a deleted work until the background refetch lands —
bounded by `staleTime` + the existing invalidations on delete/repair/check (`Library.tsx:475-522`),
acceptable. (Reload-flash persistence is deliberately deferred; revisit only if observed.)

**Verify.** Navigate Library↔Discover with no blank flash; reload shows last lists instantly then
revalidates; covers fade in without a text-gradient flash. Review: **frontend-developer**.

---

## Wave C — Discover layout conformance 🔴 (cramped / not full-bleed / off-design)
**Problem (measured).** Discover wraps the WHOLE page (header+search+billboard+rails) in
`max-w-7xl` (1280px) — **wider than the app's `max-w-6xl` (1152)**, so it overhangs the nav by ~64px
each side (`Index.tsx:28`). The billboard is a **bordered, rounded, inset card** (`Index.tsx:294`
`mt-4 h-[320px] rounded-2xl border`) preceded by a `PageHeader` ("BROWSE / Discover" + paragraph) and
the search bar — so the hero is "boxed in the middle" instead of the spec's full-bleed billboard
(`discover-dark.png`). Library does it right: `LibraryHome` full-bleed band rendered OUTSIDE the
`max-w-6xl` main (`Library.tsx:845`, `LibraryHome.tsx:45`).

**Goal.** Match `discover-dark.png`: full-bleed billboard hero spanning to the page ends with the
ambient bleeding behind the glass nav, then genre chips + discovery rails within `max-w-6xl`.

**Changes.**
- `Index.tsx`: change `max-w-7xl` → `max-w-6xl`; **drop the `PageHeader`**; render the featured
  billboard as a **full-bleed band** (mirror `LibraryHome` hero — `absolute inset-0` cover, layered
  scrims, inner text capped `max-w-6xl mx-auto`, no border/rounding). Search input already removed in
  Wave A. Genre-chip row + `CatalogRows` rails go in the `max-w-6xl` main below the hero.
- Reuse `LibraryHome`'s hero scrim/markup so Library + Discover heroes are identical chrome.

**Risks.** Discover billboard is data-conditional (needs `catalog_groups`); keep an empty/parsed state.
Mobile hero height (use the same `h-[440px] sm:h-[480px]` + responsive text as Library).

**Verify.** Side-by-side vs `discover-dark.png` desktop+mobile; hero edge-to-edge; columns align with
nav. Review: **ui-designer**.

---

## Wave D — Library restructure 🔴 (shelves→rails, merge old shelf UI, multi-select, shelf-mgmt→Settings)
**Problem.** The recent "conformance" change hid the legacy management grid (search + Select/Check-
updates/Add toolbar + `ShelfBar` bookshelves + poster grid) inside a **collapsed `Disclosure`**
(`Library.tsx` home branch) — the user explicitly wants this **merged into the new design, not hidden
in a dropdown**. Bookshelves render as **horizontal pill tabs** (`ShelfBar` `:262-422`); the user wants
**category rails**. Multi-select (`selecting`/`selected` `:437-458`, card checkboxes `:690-697`) lives
only in the grid; the user wants it on **"Browse all / See all"**. Shelf **create/manage** (`ShelfDialog`
`:126-260`, `ShelfBar` settings `:366-419`, "+ New shelf") is in Library; the user wants it **moved to
Settings** and the add-shelf icon **removed** from Library.

**Goal.** Library home = cinematic hero + rails, INCLUDING **one rail per bookshelf** (covers scrolling
sideways). "See all" on any rail opens a **full grid view with multi-select**. Shelf management lives
in Settings.

**Changes.**
- **Remove the `Disclosure`.** Home renders: `LibraryHome` hero → Continue/Audiobooks/Watchlist/New
  rails → **per-shelf rails**. Build shelf rails by reusing `Rail` + `CoverCard`
  (`Rail.tsx`, `CoverCard.tsx`): for each `Bookshelf`, a `<Rail title={shelf.name} moreLabel="See all"
  moreTo={browse url}>` of its top ~12 works (`api.listWorks("",{shelfId})`). Clicking a cover opens
  `WorkDetailModal`.
- **Full browse view + multi-select.** New **route** `/library/browse?shelf=<id|all>` (decided: route,
  not modal — deep-linkable, back-button, better mobile multi-select) rendering the existing poster grid
  + `buildGridItems` series logic + the multi-select toolbar (reuse `selecting`/`selected`/
  `downloadSelected` + card checkboxes). Wire every rail's `See all` (incl. the currently-dead "New in
  your library" `moreLabel` with no destination 🟡) to this view. **Note:** shelf rails reuse
  `api.listWorks("",{shelfId})` per shelf (one query each) — accepted cost, capped at ~6 rails + lazy-
  load; NO new batch endpoint (revisit only if 6 small parallel queries lag in demo).
- **Empty states (review delta):** a shelf with 0 works renders no rail (not a blank one); the browse
  view shows a loading skeleton + empty state. Same for Wave E author rails.
- **Move shelf mgmt to Settings.** New Settings "Bookshelves" card (a tab under Personal): list shelves
  with create (`ShelfDialog` logic) + per-shelf edit/delete/automation (lift `ShelfBar` settings card).
  Remove "+ New shelf" + the settings gear from Library.
- 🟡 `ShelfBar` pill tabs: keep a slim filter affordance or retire entirely (rails replace browsing;
  the browse view handles "All"). Default: retire from home; the browse view has a shelf selector.

**Risks.** N shelves → N list queries; cap rails (e.g. first 6 shelves + "more in Settings") and lazy-
load. Series-collapsing (`SeriesLibraryCard`) must survive in the browse grid. Multi-select bulk
actions (check-updates/download) must stay wired.

**Verify.** Home shows shelf rails (no dropdown); "See all" → grid with working multi-select; creating/
editing/deleting a shelf in Settings reflects on home. Review: **ui-designer** + **frontend-developer**.

---

## Wave E — Watchlist conformance 🔴 (match design, per-author sideways scroll)
**Problem.** Watchlist is capped at **`max-w-3xl` (768px)** — the most cramped page, ~336px dead margin
(`Watchlist.tsx:817`). It has a **3-dropdown control bar** (Sort/Status/Reason + toggles + Expand/
Collapse `:82-83,733`) absent from the spec. Author grouping = the `Sort` dropdown switching to a
**vertical accordion** of `GroupBlock`s (`:241-300,860-864`). Spec (`watchlist-dark.png`) is full-width:
stat strip → "Needs attention" horizontal title cards, no dropdowns.

**Goal.** Match `watchlist-{dark,light}.png`: full-width; per-author **horizontal scroll rails**
(covers scrolling sideways) replacing the dropdown+accordion.

**Changes.**
- `Watchlist.tsx`: `max-w-3xl` → `max-w-6xl`. Remove the Sort/group dropdown bar; keep essential
  filters as quiet chips if needed (or move to a `?` menu). Convert each author `GroupBlock` body from
  a `divide-y` vertical list into a horizontal **cover rail** (reuse `Rail`/`CoverCard` or
  `CatalogRows.tsx:169` `flex gap-3 overflow-x-auto`). Keep the 5 stat tiles + Rescan-all.
- 🟡 Replace StatTile emoji (`🔍`,`🕘` `:709-710`) per Wave G.

**Risks.** Watchlist groups can be large; rails must scroll-snap + lazy cover-load. Preserve the
`"\0ungrouped"` NUL sentinel (use Read, never sed). Preserve per-source/release-gate logic.

**Verify.** vs `watchlist-dark.png`; author rails scroll sideways; full width. Review: **ui-designer**.

---

## Wave F — Sources consolidation 🔴 (prune done jobs, merge imports, "+"→popups)
**Problem.**
1. **Backfill jobs linger.** `SourcesHub` calls `api.listJobs` which returns **all** jobs incl
   `done`/`failed` (`routers/jobs.py:27-37`, no status filter; cap 200). The pruner
   `_prune_superseded_jobs()` exists (`ingestion/scheduler.py:592-621`) but is **never scheduled/
   called**. Done backfills clutter the Active-jobs list (`SourcesHub.tsx:100`).
2. **List imports is a separate `/imports` page** (`ListImports.tsx`, route `App.tsx:394`); SourcesHub
   only shows preview cards that navigate away (`:135-152`).
3. **"+" Add menu redirects** to full pages (`App.tsx:131-178`): every item `navigate(to)` →
   `/discover`,`/add`,`/imports`. User wants **context-driven popups/modals, no redirects**.

**Goal.** Sources page shows only active/failed jobs (done pruned); List-imports lives inside Sources;
the "+" opens popups/modals.

**Changes.**
- **Prune (backend + client) — CONSERVATIVE (review delta).** (a) Client: `SourcesHub` Active list
  excludes `done` (and `failed` move to a History view, not vanish) (`SourcesHub.tsx:100`). (b) Backend:
  schedule a `@scheduled_task()` tick (~5min) calling `_prune_superseded_jobs()` (defined, unscheduled)
  **modified to keep the newest `failed` per work even when a newer `done` exists** (or gate failed-
  pruning behind an age threshold) — silently deleting failed-job records breaks the stuck-job forensics
  this app relies on. (c) A **mandatory** "History (done/failed)" `Disclosure` on Sources so terminal
  jobs stay inspectable. (d) optional `?status=` filter on `/jobs` (`routers/jobs.py`), backward-compat.
- **Merge List-imports into Sources.** Extract `ListImports`' manage list (`ImportRow` + `AddListModal`
  `:186-548` which is ALREADY a modal) into a reusable component; render it as a **Sources section/tab**.
  Keep `/imports` as a redirect to `/sources` for bookmarks. Remove it from the mobile "More" sheet/nav.
- **"+" → popups (#13) + context-driven menu (#15).** `App.tsx AddMenu`: each item opens a modal instead
  of navigating — `ImportListModal` (reuse `AddListModal`), `AddByURLModal` + `UploadFilesModal`. To
  avoid page/modal drift, extract a **shared logic hook** (`useAddTitle`/`useImportFiles`) consumed by
  BOTH the existing `/add` tabs and the new modals (don't copy-paste JSX). On success: toast + invalidate
  `qk.works()`, NO reader redirect. **#15 (user-confirmed scope): SAME options everywhere, each opens
  its own popup/modal — no route- or selection-awareness.** Keep the existing permission-gating
  (`App.tsx:139`). "Search & request" stays a route to `/discover` (or a quick-search popup). Keep
  `/add` for deep-links.
- 🟡 `Stock.tsx` (admin) can become a modal later (P3, not required now).

**Risks.** Add-tab→modal extraction must preserve **attestation + crawl-policy + grab/index logic** — the
shared-hook approach + a test asserting attestation is still required before grab mitigates drift. The
prune tick must never delete a `failed` job a user may be diagnosing (the conservative rule above). `/jobs`
`?status=` stays backward-compatible. Confirm with the user that losing **in-place** home-grid multi-
select (now only in the browse view, #6) is acceptable.

**Verify.** Done backfills disappear from Sources (and stay gone after the tick); List-imports manageable
inside Sources; "+" opens modals with no navigation; add-by-URL/upload work from the modal. Review:
**backend-architect** (prune tick + endpoint) + **frontend-developer** (modals).

---

## Wave G — Tinting/chroming + de-emoji + mobile polish 🔴
**Problem.** `--ambient` is two radial gradients anchored at the **top** of the viewport
(`index.css:76`), painted once as `fixed inset-0 -z-10` (`App.tsx:373`); `body` is flat `--bg`
(`index.css:112`). Pages that open with a billboard (Library/Discover) get a cinematic halo; **non-
billboard pages (Watchlist/Sources/Settings) read flat**, especially below the fold and on mobile. The
`--hair` token is under-applied (cards fall back to heavier `--border`). Settings rail uses **emoji
icons** (`🎚🔔⤓🔌🌐💾🛡📊` `Settings.tsx:1214-1244,1307`); the bottom tab bar (`App.tsx:323-337`) and
StatTiles also use emoji. Mobile: Settings rail is a horizontal emoji strip that **word-clips** ("Acq…",
`Settings.tsx:1289`).

**Goal.** Even, premium tint/chrome on every route; **remove the Settings-tab icons** (user-requested),
sweep other emoji for consistency; fix mobile clipping.

**Changes.** (review delta: pin ONE ambient approach + NO new icon dep.)
- **Ambient anchor — PINNED:** soften the `--ambient` gradient's fade so the whole viewport gets a
  subtle field (not only the top band), and add ONE lower-anchored radial — done in `index.css:76`
  (token only), so every route inherits depth with zero per-page work; the global
  `fixed inset-0 -z-10` layer (`App.tsx:373`) already paints it. Then standardize card borders to
  `--hair-strong` + `--pop-shadow` + the colored top-rule the spec stat cards use.
- **De-emoji — no dep.** Remove `icon` from `Settings.tsx` `TAB_DEFS` + the rendering span (`:1307`)
  (explicit request). Reuse the nav's existing **inline-SVG** pattern (`App.tsx:214,233`) if a glyph is
  wanted, else plain text — **no icon library**. 🟡 Sweep bottom tab bar (`App.tsx:323-337`) + Watchlist
  StatTiles (`:709-710`) the same way. Accepted temporary inconsistency if the tab-bar sweep trails the
  Settings rail.
- **Mobile.** Settings rail: right edge-fade or wrap to a 2-row grid so tabs don't word-clip; re-verify
  no horizontal overflow across all routes (survey found none beyond this). Keep `aria-label`s where a
  removed emoji was the only label.

**Risks.** Background changes interact with reading skins / 14 themes — test light + dark + a couple
mids. Don't regress the glass nav.

**Verify.** Walk every route in Daylight+Charcoal desktop+mobile; non-billboard pages no longer read
flat; no emoji in Settings rail; no mobile clip. Review: **ui-designer**.

---

## Wave H — Book-description markup bug (backend) 🔴
**Root cause (confirmed).** Descriptions are stored **raw** — source adapters + page-metadata extraction
copy `description`/`synopsis` with **no HTML/markdown sanitize**, and the frontend renders them as plain
text (`whitespace-pre-line`, `WorkDetailModal.tsx:245-248`; hero blurb `LibraryHome.tsx:76-79`), so tags
show literally (e.g. *Library of Heaven's Path*). Only **metadata providers** strip HTML (Goodreads
`metadata.py:297`, AniList `:576`); **adapters** (`jnovel.py:140`, comix, standardebooks, generic_feed),
`extract.page_metadata()` (`extract.py:348-359,1071`), and **Ranobedb** (`:240`) do not. Direct
assignments with no clean: `engine.py:113`, `tracker.py:67`, `catalog.py:1101`. Helpers exist but
aren't applied: `strip_html` (`integrations/base.py:48-54`), `_clean_ol_description`
(`book_catalog.py:794-809`).

**Goal.** No raw HTML/markdown in any stored description; fix the cause (ingest), and backfill existing
rows.

**Changes.**
- Add a shared `clean_synopsis(text)` (strip `<…>` tags, unescape entities, drop stray
  `**`/`_`/`` ` ``/`~`, collapse whitespace) — extend the existing `strip_html`/`_clean_ol_description`
  rather than reinvent. Apply at the **single chokepoints**: the `meta.description`→`work.description`
  assignment (`engine.py:113`, `tracker.py:67`), `catalog.py` `entry.synopsis`→`work.description`
  (`:1101`), `extract.page_metadata()` description return (`extract.py`), and the un-stripped providers
  (Ranobedb). Prefer cleaning where text ENTERS `Work.description`/`CatalogWork.synopsis` so every
  adapter is covered by one call.
- **Backfill** existing rows: a one-time migration/tick cleaning `Work.description` +
  `CatalogWork.synopsis` where they contain `<`, `**`, or `_`. Re-clean *Library of Heaven's Path*
  specifically to confirm.
- 🟡 Verify the specific source for *Library of Heaven's Path* (a web-crawl adapter via
  `page_metadata`) is covered.
- **Render-time guard (review delta — defense-in-depth):** also strip tags at display in
  `WorkDetailModal`/`LibraryHome` so any *future* un-cleaned adapter can't re-introduce the bug. Cheap,
  and complements (does not replace) the ingest fix.

**Risks.** Over-stripping could mangle legitimate `*` (e.g. ratings) — scope the markdown strip to
paired markers / leading-`>`. Don't strip inside the reader content (this is descriptions only).

**Verify.** `pytest` a sample with `<p>…</p>`, `**bold**`, entities → clean; backfill clears existing
rows; the named title renders clean. Review: **backend-architect** + a `pytest` unit. Restart
`shelf.service`.

---

## Sequencing & dependencies (review delta: A→D kept adjacent)
1. **H** (descriptions) — independent backend; do first/parallel (high value, low risk).
2. **A** (unified search) — unblocks removing in-page search in **C/D**. **Exit test:** `?q=` composes
   correctly with `activeShelf` on Library (a unit/interaction test) — do NOT defer this to D.
3. **D** (Library) — **immediately after A** (keep adjacent; don't slot B/E/F between them, since the
   `?q=`+`activeShelf`+restructure interaction only fully exercises once D lands). Largest wave.
4. **C** (Discover) — also depends on A; after D or parallel.
5. **B** (cached-first) — independent; pairs with A/D.
6. **E** (Watchlist) — independent UI (doesn't use search); any time.
7. **F** (Sources/imports/add) — backend prune + modals; independent of A–E.
8. **G** (tint/chrome/de-emoji/mobile) — **last**, cross-cutting polish over the restructured pages.

Each wave: implement → **specialized sub-agent review** → fix → `npm run build`+`tsc` (+`pytest` for
H/F-backend; tests required for A's `?q=`+`activeShelf` and F's attestation-before-grab) → demo
screenshot-verify on an **isolated, seeded non-prod DB** (`.ui-review/setup-demo.sh` → `:8011`,
Charcoal+Daylight, desktop+mobile, vs handoff) → commit branch→`--no-ff` main → restart `shelf.service`
if backend changed. **NEVER verify against prod** (two prior prod-DB wipe incidents — relative
`./shelf.db`). Not pushed to origin.

## Other issues found (folded into the waves above) 🟡
- Nav search is a **non-functional dead button** today (Wave A).
- "New in your library" rail's **"See all" has no destination** (Wave D).
- Covers **flash** generative→real with no skeleton (Wave B).
- `_prune_superseded_jobs()` **exists but is never scheduled** (Wave F).
- Emoji used in **3 places** (Settings rail, bottom tab bar, StatTiles) — consistency sweep (Wave G).
- Ranobedb provider stores **raw description** unlike its siblings (Wave H).

## Decisions (resolved after plan review)
1. **Cache persistence** (Wave B): **CUT** — outside the 17 requests; `placeholderData`+`staleTime`+
   prefetch already deliver "resolve before queries." No new dep, no localStorage persistence in v1.
2. **Emoji** (Wave G): **remove now, no icon dep** — strip Settings-rail emoji using the existing
   inline-SVG/text pattern; sweep tab bar + StatTiles the same way (accept brief inconsistency).
3. **Browse-all** (Wave D): **route** `/library/browse` (deep-linkable, back-button, hosts multi-select
   better on mobile than a modal).
4. **ShelfBar pill tabs** (Wave D): **retire from home** — per-shelf rails + the browse view's shelf
   selector replace them (removes the nested/cramped chrome behind #3/#8).

## User confirmations (all resolved 2026-06-24)
- **#6:** ✅ losing in-place home-grid multi-select (moves to `/library/browse`) is acceptable.
- **#15:** ✅ "+" menu = SAME options everywhere, each opens its own popup (no route/selection-awareness).
- **#11:** ✅ failed jobs kept inspectable in a Sources "History" view, not auto-deleted.

## Plan-review applied (frontend-developer)
Cut localStorage persistence (over-engineered/out-of-scope); made failed-job pruning conservative +
History view mandatory (riskiest behavioural change); pinned ONE ambient-tint approach; sharpened #15
into a testable definition; kept Waves A→D adjacent with a `?q=`+`activeShelf` exit-test; required an
isolated seeded DB for verification (prior prod-wipe incidents); added empty-states for new rails/browse
+ a render-time description guard; shared-hook for Add modals to prevent page/modal drift; resolved all
4 open decisions. All 17 user requests confirmed covered.
