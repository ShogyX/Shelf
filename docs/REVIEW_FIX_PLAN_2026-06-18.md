# Shelf — Full Review & Fix Plan (2026-06-18)

## Context

A 5-lens review of the Shelf app (security, architecture, backend implementation, frontend
implementation, UI/UX) run by independent sub-agents, each reality-checked against the actual code
(file:line, reproductions where feasible). This document consolidates every finding, assigns a
**minimal (ponytail) fix**, and — critically — sequences the fixes into **waves** that avoid
collisions (multiple fixes rewriting the same file) and respect dependencies (fixes that rely on a
primitive another fix introduces). The §"Clash & dependency analysis" section is the part to read
before implementing.

**Implementation rule:** apply the **ponytail skill** to every fix — smallest change that holds,
reuse existing primitives, no speculative abstractions. Re-verify against reality during
implementation (run it, don't assume). Large refactors are planned but marked *deferred* — do them
only when the file is already open for another reason, unless the operator asks otherwise.

Overall health: **no Critical issues found.** The codebase is well-hardened (SSRF pinning, zip-slip,
SQLi, command-injection, IDOR, auth/session all verified solid). Findings are High↓, dominated by
consistency/robustness debt and a few real correctness bugs.

Baseline before changes: backend `813 passed / 4 skipped`; frontend `tsc` clean, `build` green.
Branch `acquire-fixes-2026-06-16` (UI rounds 1–3 uncommitted; acquire-priority fix deployed live).

---

## ⚠ INCIDENT-1 — Production DB clobbered by ad-hoc code (root cause + fix) — TOP PRIORITY

**What happened (2026-06-18 ~20:12–20:25):** a long-running external write transaction against the
**production** `backend/shelf.db` bulk-deleted Works (1714→~590), CatalogGroups (181268→17), CatalogWorks,
**all 9 Integrations**, and the non-admin `test` user — leaving chapters/content/library_items **orphaned**
(raw multi-table delete bypassing ORM cascade). It held the SQLite write lock long enough to exhaust the
app's SQLAlchemy pool (`QueuePool ... TimeoutError` at 20:13–20:14 in the journal). The wiped table-set
matches the test/reset-fixture set; the timing matches the multi-agent review reality-checking against the
**live** instance. **shogy's 15 library works were restored** (only the parent Work rows were gone; chapters
+ content + library_items survived as orphans and reconnected). Integrations / catalog / `test` user were
left wiped per the operator's "only restore the user's library works" instruction.

**Root-cause enabler:** `config.py:16 database_url = "sqlite:///./shelf.db"` (**relative** path) + `SessionLocal()`
defaulting to it + **no production write-guard**. So any in-repo script/repro/test-helper run from `backend/`
hits production. The test suite is safe ONLY because `tests/conftest.py` overrides `SHELF_DATABASE_URL` to a
tmp DB — that protection does **not** extend to ad-hoc `python -c`, maintenance scripts, or a test module
imported outside pytest.

**Fix (do FIRST, before any other wave — ponytail-minimal):**
1. **Destructive-op guard.** Add `app/safety.py:require_destructive_ok(db)` that raises unless
   `SHELF_ALLOW_DESTRUCTIVE=1` **or** the DB path is a recognized tmp/test DB. Call it from every bulk-reset /
   cleanup utility and the test reset helpers. `conftest.py` sets the env (it already uses a tmp DB).
2. **Absolute, explicit DB path.** Resolve `database_url` to an absolute path at startup so cwd can never
   silently select a different file; log the resolved prod path on boot.
3. **Operational rule (highest value, non-code):** reviews / bug-reproductions / scripts MUST run against the
   isolated pytest DB or a **copy** of `shelf.db` — NEVER `SessionLocal()` against prod. Bake this into the
   plan's verification philosophy and any future review-agent instructions.
4. **Orphan-prevention (follow-up):** a Work delete left ~20k orphan chapters — wire `ON DELETE CASCADE` (or
   app-level cascade) so partial wipes stay consistent; or ensure deletes go through the ORM. Lower priority
   than #1–#3 (the real fix is preventing the unauthorized delete).

Verification: a test asserting `require_destructive_ok` raises against a non-tmp DB without the env flag;
boot logs the absolute prod path. **This supersedes Wave 0 ordering — implement INCIDENT-1 first.**

---

## Findings (with fixes)

IDs: SEC=security, CODE=implementation, ARCH=architecture, FE=frontend-impl, UI=ui/ux.

### Confirmed correctness/security bugs (fix now)

| ID | Sev | Location | Issue | Minimal fix |
|----|-----|----------|-------|-------------|
| **CODE-H1** | High | `app/ingestion/acquire.py:172` (`order = [route] if route else priority`), `:248` (unconditional `mark_unavailable`) | A **forced** single route (`route=…`) that finds nothing falls into the unconditional `mark_unavailable`, gating the title across **all** routes for ~14d. Reproduced. (Re-verify lines against current `acquire.py` — it grew after the torrent-priority fix.) | Only `mark_unavailable` when `route is None` (full-chain exhaustion); a forced route returns `status:"none"` without gating. |
| **SEC-M1/S3** | Med | `app/routers/imgproxy.py:57` (`/cover`) vs `:101` (`/img`) | `GET /api/cover` fetches **any** public `http(s)` URL for any authed user with no host allowlist (`/img` has the `referer_for→403` gate). SSRF to private IPs IS blocked (`imagecache._fetch_image:196` `assert_public_url` + IP-pin) — so the real exposure is **authed cache-fill / same-origin amplification of arbitrary public images**, not an open internal proxy. Confirmed. | Add a cover-CDN host allowlist to the remote branch (mirror `referer_for`/`/img`), or restrict remote fetch to known cover hosts; local `/media,/covers,/api` redirects unchanged. |
| **SEC-M2** | Med | `app/covers.py:43,60`; `app/imagecache.py:32,121,284` | `image/svg+xml` accepted, stored, served same-origin → stored-XSS one CSP-change away. **Three** sites incl. `_CTYPE_BY_EXT:284` (legacy-migrated covers) — miss it and a `.svg` can still be re-served. | Drop SVG from all three accepted-MIME/ext maps (covers are raster). |
| **CODE-M2 + ARCH-H2** | Med | `app/routers/auth.py:404,447` create/update; `_purge_user` (`auth.py:580`) | Admin email set/edit skips `_EMAIL_RE` + `.lower()` the public path enforces; concurrent dup → unhandled `IntegrityError` 500. **And** `_purge_user` (deletes 9 tables, `:597-609`) never deletes `fetch_source_priority:user:{id}` → leaks on user delete (confirmed). | Validate+lower email in both admin paths; wrap commit → 409 on IntegrityError. In `_purge_user`, also delete the `app_settings` row `fetch_source_priority:user:{id}` (key from `acquire._user_key`). |
| **CODE-M1** | Med | `ingestion/libgen.py:876,963` | Two users requesting the same not-yet-cached title → two libgen jobs → duplicate file + duplicate `Work` (per-user dedup only, no piggyback, no pre-download hooked check). | In `_advance_job`/`_import_file`, re-read `cw.hooked_work_id` before download; if set, `add_to_library` + finish job as imported. |
| **CODE-M3** | Med | `pages/Users.tsx` UserDrawer sync effect | Typing a new email/display-name then clicking a sibling action (`Make admin`/`Disable`) triggers refetch → effect re-seeds → **unsaved edits silently revert**. | Key the re-seed effect on `u.id` only (re-seed on user switch), or guard with a dirty flag. |
| **SEC-L2** | Low | `sanitize.py:88` | `javascript:`/`data:` scheme denylist bypassable by intra-scheme whitespace (`java\tscript:`). Mitigated by CSP today. | Strip whitespace/controls before the scheme test, or allowlist `http/https/relative`. |
| **SEC-L1** | Low | `backend/.env` (mode 644) | World-readable secrets (SMTP pw, setup token). Not git-tracked (good). | `chmod 600`; have `install.sh` set perms on creation. |

### Frontend correctness / a11y (fix now, small)

| ID | Sev | Location | Issue | Minimal fix |
|----|-----|----------|-------|-------------|
| **FE-H1** | High | `components/ui.tsx:44` `useDialogFocus` | Escape is handled in **capture** phase + `stopPropagation` → a stacked dialog (ShelfPrompt over CatalogDetail; confirm over SeriesModal) closes the **underlying** dialog, not the top one. | Move Escape to **bubble** phase (or a tiny module-level dialog stack; only the top entry acts). Keep Tab-trap behavior. |
| **FE-H2** | High | `components/ui.tsx:36,44` | Two stacked dialogs' window-level Tab-traps fight → erratic Tab. | Same fix as FE-H1 (dialog stack / topmost-only handler). |
| **FE-M4** | Med | `components/ui.tsx:80` | `Modal` is `aria-modal` but has no accessible name (no `aria-labelledby`/`aria-label`). | Add `id` to the `<h3>` + `aria-labelledby` (fallback `aria-label` when title isn't a string). |
| **FE-M5** | Med | `ui.tsx` Modal + hand-rolled dialogs | Backdrops don't lock body scroll. | `useEffect` in `useDialogFocus` sets `document.body` `overflow:hidden` while mounted. |
| **CODE-L5** | Low | `settings/NotificationCards.tsx` | Testing one channel disables **every** row's Test button (`disabled={test.isPending}`). | `disabled={test.isPending && test.variables === c.id}`. |
| **CODE-L6** | Low | `IntegrationsManager.tsx` CloudflareSolverBox | Form never re-syncs after first load; dead `setForm({...form!, ...{}})`. | Reseed form when entering edit/on save; delete the no-op spread. |
| **FE-L1** | Low | `index.css:29` / `themes.ts` | `[data-theme="sepia"]` block can never match (group resolves to light/dark); CSS hex are stale dupes of `themes.ts`. | Delete the dead `sepia` block; keep only `:root`+dark boot fallback. |
| **FE-L4** | Low | `Index.tsx:207` etc. | `g.id || g.norm_key || g.title` key chain drops a legit `id:0`; title-collisions. | Use `??` not `||`. |

### UI/UX consistency (fix now, mechanical → medium)

| ID | Sev | Location | Issue | Minimal fix |
|----|-----|----------|-------|-------------|
| **UI-H1** | High | `pages/Settings.tsx` (~15 headers) | 15 hand-rolled `<h2>…<InfoHint/></h2>` instead of the `CardHeader` primitive built to replace them (Users already migrated). | Mechanical swap each to `<CardHeader title hint desc badge />`. |
| **UI-H2/H3** | High | ~9 files (`Stock`, `Index`, `IntegrationsManager`, `SystemSettings`, `StorageSettings`, `AddWork`, `BrowseCatalog`, `Settings`) | Each re-inlines the input class string (drifted: `bg-bg`/`bg-surface`, with/without `focus:border-accent`) instead of the exported `inputCls`; inconsistent focus rings. | Import `inputCls`, delete local `input`/`field`/`selCls`; compose widths via `` `${inputCls} w-24` ``. |
| **UI-M1** | Med | `Index.tsx:200,226,256`, `BrowseCatalog.tsx:93` | Empty/zero-result states are bare `<p>` instead of `EmptyState` (Jobs/Stock use it). | Wrap genuine empty results in `<EmptyState>`; keep "Type to search…" as a plain idle hint. |
| **UI-M2** | Med | `Stock.tsx:266`, `CatalogCard.tsx:415,782`, `IndexShared.tsx:419` | 3 near-identical hand-rolled full-screen-sheet dialog shells (drifted padding/close); Stock dups Escape handling + `useDialogFocus`. | Add `Modal variant="fullscreen-sheet"` (full-screen mobile, centered `sm:`) to `ui.tsx`; route the three through it. |
| **UI-M3** | Med | `Stock.tsx:131` | "Queue stocking" dense single-row form overflows on tablet/phone. | `grid gap-3 sm:grid-cols-2 lg:grid-cols-3`, full-width fields via `Select`/`inputCls`, submit on its own row; Sort+Cap behind a `Disclosure`. |
| **UI-M4** | Med | `Jobs.tsx:46,55,65` | Bespoke section headers (not `SectionHeader`); 4-line reaper-internals intro bloat. | `<SectionHeader>`; move reaper text into an `InfoHint` by the H1. Same for Stock's intro paragraphs. |
| **UI-M5** | Med | `AddWork.tsx:198` | Hand-rolled crawl-policy collapsible (third collapsible style) vs `Disclosure`. | `<Disclosure title="Crawl speed & schedule" subtitle="Optional…">`. |
| **UI-M6** | Med | `AddWork.tsx:232,241` | Lowercase primary CTAs ("grab title", "crawl & index") clash with Title-case everywhere. | "Grab title" / "Crawl & index". |
| **UI-L2** | Low | `Reader.tsx:433`, Jobs/Stock `✕` | Icon-only `size="sm"` buttons ~28px < 44px touch target. | Add `Button size="icon"` (`h-9 w-9 p-0`); use for icon buttons. |
| **UI-L3** | Low | `App.tsx:124` | Nav uses hidden-scrollbar overflow; rightmost tabs can sit off-screen on phone. | `flex-wrap` on small screens (as the `Tabs` primitive already does) or an edge fade. |
| **UI-L4** | Low | `CatalogCard.tsx:302` | Card's per-source button wall competes with the primary Acquire CTA (card disagrees with the modal's "one primary action"). | Collapse to "View N sources →" that opens the detail modal. |
| **UI-L6** | Low | `Missing.tsx:8` vs `Stock.tsx:13` | `searching` tone grey on Missing, violet on Stock. | Align `searching → violet` (app's in-progress tone). |
| **CODE-L4 + FE-M1** | Low/Med | `CatalogCard.tsx` mutations | `["catalog"]` invalidation is over-broad (refetches whole infinite grid on one tile action); stock/acquire don't invalidate `["stock-summary"]`. | Prefer `qc.setQueryData` patch of the single group; add `["stock-summary"]` invalidation. |
| **FE-M2** | Med | `CatalogCard.tsx:160` **and `:725`** | `useQuery(["stock-summary"])` mounts **per card** (60×) — deduped network but 60 subscriptions + re-render fan-out; admin-only yet on every card. Two copies (compact card + detail). | Lift `stock-summary`/`allowStock` to the page/section; pass `allowStock` down as a prop. Cover BOTH sites. |

### Frontend performance (fix now where cheap)

| ID | Sev | Location | Issue | Minimal fix |
|----|-----|----------|-------|-------------|
| **FE-H3** | High | `App.tsx:11` | Single 602 kB bundle; every page static-imported, incl. admin-only Settings/Users/Jobs/Stock shipped to all. | `React.lazy` the route components + `<Suspense>` fallback. |
| **FE-M3** | Med | `Index.tsx:205`, `BrowseCatalog.tsx:99` | No list virtualization; hundreds of heavy cards stay mounted after scrolling. | **Deferred** unless catalogs reach thousands; partial relief from FE-M2. `@tanstack/react-virtual` when needed. |

### Architecture (high-value minimal fixes + deferred refactors)

| ID | Sev | Location | Issue | Fix / disposition |
|----|-----|----------|-------|-------------------|
| **ARCH-H1** | High | `db.py:480–608` `_ADDITIVE_COLUMNS` + Alembic | Three overlapping migration mechanisms; Alembic can't build from empty (creates only 7 of ~20 tables); a new `mapped_column` silently missing on existing SQLite DBs unless hand-added to `_ADDITIVE_COLUMNS`. | **Fix now (ponytail-minimal):** add a boot/test **drift assertion** diffing `Base.metadata` columns vs `inspect(engine)`, failing loudly on any mapped column missing from the live DB; optionally a generic additive `ALTER` to replace the manual dict. *Defer* the full "pick one authority / squash Alembic baseline" decision. |
| **ARCH-H3** | High | `cache.py` + 20+ `cache.clear("catalog")` sites | Stale-data risk: every new catalog write path must remember the right prefix. | **Fix now (minimal):** a monotonic **generation counter** folded into cache keys (bump on any catalog write → all stale keys dead), replacing scattered prefix clears; or named-prefix helpers as a smaller step. |
| **ARCH-M3** | Med | `scheduler.py:1491` | 27 ticks, hardcoded intervals, no registration jitter → SQLite-writer phase-alignment. | **Fix now (small):** drive intervals from one dict/config; add small registration jitter. |
| **ARCH-M1** | Med | `torrents.py`/`libgen.py` import `downloads._*` | 3 pipelines share a core via private `_`-members; `downloads` can't refactor them safely; verdict protocol undocumented. | **Deferred:** extract `ingestion/import_core.py` (promote `_import_completed`/`_promote`/`_notify_import`, document the verdict enum) *before adding a 4th route*. |
| **ARCH-M2** | Med | `routers/index.py` (46 endpoints) | Fat router: domain logic + cache invalidation + serialization. | **Deferred:** thin on next substantial change; move orchestration into `ingestion.catalog`/`acquire` (pairs with ARCH-H3). |
| **ARCH-M4/M5, L1–L4** | Low | `Work` wide table; `client.ts` monolith + `Settings.tsx` 1325 LOC + ad-hoc RQ keys; override-pattern dup; un-FK'd `ChapterContent.chapter_id`; dead columns; cache sizing | Tech-debt seams. | **Deferred/monitor.** Plan: `queryKeys` factory + split `client.ts` by domain when it next grows; `WorkCrawlState` split only if crawl state grows; drop dead columns in a future schema pass. |
| **FE-L2** | Low | `client.ts:435`, `IntegrationsManager.tsx:149` | `IntegrationConfig = Record<string,any>` + `buildBody(...) as any` defeats typing on the heaviest form. | **Deferred:** discriminated union per `kind`. |

### Deployment / ops notes (not code; document)

- **SEC-S1:** per-IP login lockout weakens if `trust_proxy=True` and the proxy doesn't strip client XFF. Per-username cap still applies. → Ops: ensure the fronting proxy overwrites XFF.
- **SEC-S2:** in-process brute-force state is per-worker → multiplies with `--workers>1`. → Add a hard guard refusing `>1` worker until lockout state moves to the DB (small), or document the constraint.
- **CODE-L7:** "Last seen" is newest session **start**, not last activity. Accurate enough; rename label to "Last sign-in" if precision matters (trivial).

---

## Clash & dependency analysis (read before implementing)

**Shared-file hotspots** — the same file is touched by several findings; coordinate into one pass so
fixes don't overwrite each other:

- **`components/ui.tsx`** ← FE-H1, FE-H2, FE-M4, FE-M5, UI-M2(add variant), UI-L2. → **One coordinated
  primitive pass (Wave 2)**, because downstream consumers depend on the fixed `useDialogFocus`/`Modal`.
- **`components/catalog/CatalogCard.tsx`** ← UI-M2(route dialogs through Modal), FE-M2(lift stock-summary),
  UI-L4(collapse source buttons), CODE-L4+FE-M1(invalidation). → **One coordinated CatalogCard pass
  (Wave 3)**. Do NOT touch CatalogCard in earlier waves — it would be rewritten 4×.
- **`pages/Settings.tsx`** ← UI-H1(CardHeader ×15), UI-H2(inputCls). → one mechanical pass (Wave 4).
- **`routers/auth.py`** ← CODE-M2(email), ARCH-H2(`_purge_user` cascade delete). → one pass (Wave 0).
- **`covers.py`+`imagecache.py`** ← SEC-M2(SVG, **3 sites**). → one pass (Wave 0).
- **`App.tsx`** ← FE-H3(lazy routes), UI-L3(nav wrap). → one pass (Wave 4).
- **`pages/Index.tsx`** ← FE-L4(`??` key) **+** UI-M1(EmptyState) **+** FE-M3(deferred). → one pass (Wave 4); FE-L4 rides along.
- **`pages/AddWork.tsx`** ← UI-M6(CTA casing) **+** UI-M5(Disclosure) **+** UI-H2(inputCls). → one pass (Wave 4); UI-M6 rides along.
- **`components/IntegrationsManager.tsx`** ← CODE-L6(CloudflareSolverBox) **+** UI-H2(inputCls). → one pass (Wave 4); CODE-L6 rides along.
- **`pages/Stock.tsx`** ← UI-M2(modal→`Modal`) **+** UI-M3(form grid). → one Wave-4 pass; **also** delete Stock's own bubble-phase Escape handler (`Stock.tsx:240-241`) when routing through `Modal` — after FE-H1 migrates `useDialogFocus` to bubble phase, the duplicate would double-close.

> **Correction (karen review):** FE-L4, UI-M6 and CODE-L6 were originally in Wave 1 but each shares a
> file with a Wave-4 fix — they are **moved into the Wave-4 per-file passes** below so each file opens once.

**Dependency edges** (B needs A first):
- UI-M2 part B (route CatalogDetail/SeriesModal/Stock onto `Modal`; `IndexShared.tsx` lives at
  `components/IndexShared.tsx`) **depends on** Wave 2 adding `variant="fullscreen-sheet"` + the
  FE-H1/H2/M5 `useDialogFocus` fixes. → primitives first.
- `Button size="icon"` (UI-L2) genuinely does not exist yet (`ui.tsx` has only `sm`/`md`) → must land in
  Wave 2 before any consumer uses it.
- **NOT a dependency (clarification):** `CardHeader` and `inputCls` **already exist** in `ui.tsx` (added
  in earlier rounds). Wave 4's UI-H1/UI-H2 are pure *adoption* passes — Wave 2 does NOT need to create them.
- UI-M3 (Stock form) and UI-M2 (Stock modal) both touch `Stock.tsx`; do Stock's modal+form together.
- ARCH-H3 (cache generation counter) should land **before** ARCH-M2 router-thinning (invalidation
  moves with logic) — but ARCH-M2 is deferred, so no live conflict.
- CODE-L4/FE-M1 invalidation tweaks live in CatalogCard → fold into Wave 3 (don't do standalone).

**No-clash but same-area (safe in parallel):** SEC-M1 (imgproxy) vs SEC-M2 (covers/imagecache) — adjacent
but different files. CODE-H1 (acquire.py) is isolated (note: acquire.py was just changed for the
torrent-priority fix — re-verify the gate edit against current code). CODE-M1 (libgen.py) isolated.

**Clashes with recent uncommitted work:** CODE-M3/L6/L4/L5 and FE-M2 refine code written in the UI
rounds 1–3 (Users drawer, CloudflareSolverBox, stock-at-acquire). They're refinements, not conflicts —
but implement them on top of the current working tree, not the last committed state.

---

## Implementation roadmap (waves)

Each wave ends with a verification gate. Ponytail throughout.

**Wave 0 — Security + backend correctness (independent files, high value):**
CODE-H1, SEC-M1/S3, SEC-M2, CODE-M2+ARCH-H2, CODE-M1, SEC-L2, SEC-L1.
Gate: `pytest` (add/extend tests: forced-route-no-gate, email validation+409+cascade-delete, libgen
hooked-short-circuit, /cover allowlist 403, SVG rejected). Restart backend; smoke `/api/fetch-priority`,
`/api/cover` allowlist.

**Wave 1 — Frontend quick wins (isolated files only):**
CODE-L5 (NotificationCards), CODE-M3 (Users.tsx), FE-L1 (index.css), UI-L6 (Missing.tsx).
*(Moved to Wave 4 to avoid double-opening a file: FE-L4→Index.tsx pass, UI-M6→AddWork pass, CODE-L6→
IntegrationsManager pass.)*
Gate: `tsc` + `build`.

**Wave 2 — Design-system primitive hardening (`ui.tsx`) — foundation:**
FE-H1, FE-H2, FE-M5, FE-M4, UI-M2(add `variant="fullscreen-sheet"`), UI-L2(`size="icon"`).
Gate: `tsc` + `build`; browser-check existing modals still trap/restore focus + Escape closes the
**top** dialog when stacked (open a catalog detail → Acquire prompt → Esc closes only the prompt).

**Wave 3 — Catalog component consolidation (depends on Wave 2):**
UI-M2(route CatalogDetail/SeriesModal onto Modal), FE-M2(lift stock-summary + prop), UI-L4(View N
sources), CODE-L4+FE-M1(scoped invalidation). One CatalogCard pass.
Gate: `tsc`+`build`; browser-verify popup/series still work (rounds-1 screenshots as reference), stock
prompt still gated for admins, no per-card stock-summary query in network panel.

**Wave 4 — UI consistency adoption (mechanical, broad); each file opened ONCE:**
- `Settings.tsx`: UI-H1 (CardHeader ×15) + UI-H2 (inputCls).
- `IntegrationsManager.tsx`: UI-H2 (inputCls) + **CODE-L6** (CloudflareSolverBox reseed / dead-spread).
- `Stock.tsx`: UI-M3 (form grid) + UI-M2 (modal→`Modal`, **delete its `:240-241` Escape duplicate**) + UI-H2.
- `Index.tsx`: UI-M1 (EmptyState) + **FE-L4** (`??` key) + UI-H2 (`selCls`).
- `AddWork.tsx`: UI-M5 (Disclosure) + **UI-M6** (CTA casing) + UI-H2.
- `BrowseCatalog.tsx` / `SystemSettings.tsx` / `StorageSettings.tsx`: UI-H2 (inputCls).
- `Jobs.tsx`: UI-M4 (SectionHeader + InfoHint).  `IndexShared.tsx`: UI-M2 (modal→`Modal`).
- `App.tsx`: UI-L3 (nav wrap) + **FE-H3** (lazy routes + Suspense).
Gate: `tsc`+`build` (confirm route chunks split out); browser-pass Settings/Stock/Jobs/AddWork, light+dark.

**Wave 5 — Architecture (minimal):**
ARCH-H1 (schema-drift assertion — the one to do) + ARCH-M3 (**registration jitter only**; intervals
already flow from `_initial_tuning()`, skip the "drive from a dict" half).
Gate: full `pytest`; boot the app (drift assertion passes on the live DB); a test with a deliberately
model-only column is caught by the assertion.

**Deferred (planned; do when the file is next touched or on operator request):**
- **ARCH-H3** (cache generation counter) — **demoted per ponytail review:** TTL ~4s, clears concentrated
  (≈18 calls, 14 in `index.py`), a missed clear costs ≤4s of read staleness — threading a generation
  counter through every key + write path costs more than the bug. If touched, take only the smaller
  "named-prefix helpers" step.
- ARCH-M1 (import_core), ARCH-M2 (router thinning), ARCH-M4 (Work split), ARCH-M5/FE-L2 (client.ts split +
  queryKeys factory + typed integration body), FE-M3 (virtualization), SEC-S2 (worker guard), CODE-L7
  (label rename). Each sized/rationale'd above. **SEC-L2** is genuinely Low (defense-in-depth behind CSP);
  fix the one-liner but it does not gate Wave 0.

---

## Verification philosophy

- Every backend fix gets/extends a `pytest` (the repo convention; `tests/` already covers acquire,
  auth, torrents, stock). Reproduce the bug in a failing test first, then fix (CODE-H1, CODE-M1,
  CODE-M2, SEC-M1/M2).
- Every frontend wave: `tsc --noEmit` + `npm run build`, then a browser pass on the touched screens
  (light+dark) using a minted-then-deleted admin session (as in the prior rounds), mutating nothing.
- Cross-cutting reality-check (karen-style) after Wave 3 and Wave 4: confirm the called-out issues are
  actually resolved in the running app, not just in code.
- Nothing is committed until the operator approves; UI rounds 1–3 remain uncommitted on the branch.
