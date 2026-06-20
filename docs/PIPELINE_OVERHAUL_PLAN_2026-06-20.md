# Shelf Pipeline Overhaul — Plan (LazyLibrarian-inspired)

Status: **DRAFT / PLANNING**. Created 2026-06-20.
This document is built in stages: Stage 0 (raw spec from the query) → grounding
against the real app → refined, reviewed, staged implementation plan.

---

## Stage 0 — Requirements decomposed from the query (verbatim intent, unfiltered)

Each item is traced back to the user's request so nothing is dropped. Items get
CONFIRMED / ADJUSTED / DROPPED in the grounding pass.

### Theme 1 — LazyLibrarian adoption (philosophy & source)
- **R1** Study LazyLibrarian's (https://gitlab.com/LazyLibrarian/LazyLibrarian)
  fetch/download/acquire pipeline; adopt its more solid/reliable matching +
  fetching approach where it improves Shelf. Long-lived repo → likely more robust.
- **R2** LazyLibrarian supports **usenet** + **qbittorrent**; review both deeply,
  compare to Shelf, adopt what likely improves Shelf.
- **R3** **Do NOT touch** Web hook / index fetching — native Shelf solution, does
  matching *post*-fetch (enrichment), not pre-fetch; out of scope.
- **R4** **AA (Anna's Archive):** if AA presents a similar *matching surface* to
  usenet/torrent → fold it into the *same* matching/fetch logic; if not → keep a
  tailored approach.
- **R5** **Success metric:** baseline = current Missing page (current matcher). New
  approach must surface **>15% more** of the currently-missing titles to count as
  an improvement.

### Theme 2 — Wanted page (renamed from Missing)
- **R6** Rename **Missing → Wanted**.
- **R7** Sort **and follow** by **author**.
- **R8** Sort **and follow** by **series**.
- **R9** Sort by **title**.
- **R10** Wanted shows only what was *requested*, BUT if e.g. book 5 of a 9-book
  series is requested, **surface the entire series**.
- **R11** Allow **manual** "request the whole series".
- **R12** **Auto**-request the whole series when a Settings toggle is ON
  (**OFF by default**).

### Theme 3 — Catalog + metadata
- **R13** More comprehensive **categorization** + **metadata matching** in catalog.
- **R14** Click a title → **select the author** → request **all** titles by them.
- **R15** **Follow author** → auto-fetch any new title they publish.
- **R16** **Follow series** → Shelf periodically looks for new titles in the series
  and auto-fetches + adds them.

### Theme 4 — Source ordering & queueing (KEY FOCUS)
- **R17** Many sources; order is **dynamic / UI-editable**. Each work is searched
  **exhaustively at each source in order, NEVER in parallel** — finish one source
  before the next.
- **R18** Search returns no matching title ("fails") → move to next source, search.
- **R19** Source returns **non-200** OR **daily usage limit reached** → note it,
  **schedule** the title for that source for when it's available again, move on to
  the next source.
- **R20** If a **downstream** source matches the title → **remove** it from the
  unavailable source's queue.
- **R21** If **no** downstream source matches → title **stays queued** for the
  unavailable source until it's available, then search there.
- **R22** A title must **never** move to a source it has **already searched**,
  regardless of that search's result.

### Theme 5 — VirusTotal gate (torrents)
- **R23** Torrent files must pass the **VT gate** before entering stock/library.
- **R24** VT has per-minute/hour/day limits → a **queue** naturally forms to honor
  them; every file passes the gate before entering the library.
- **R25** Surface **VT queue stats** in the Statistics tab (Settings).

### Theme 6 — Stats
- **R26** Pipeline stats already exist in the Statistics page; provide an
  **improved overview** once the pipeline is overhauled.

---

## Stage 1 — Grounding against the real app

Each Stage-0 requirement reconciled against the actual code (4 sub-agent maps:
pipeline, Wanted/catalog, VT/stats, LazyLibrarian). Verdict: **CONFIRMED** (build
as written), **ADJUSTED** (changed by reality), **ALREADY DONE**, **DIVERGE**
(deliberately not like LL), **AT-RISK** (metric/assumption tension).

### What already exists (don't rebuild)
- **Ordered source cascade**: `acquire.py` `ROUTES = (torrent, pipeline, libgen,
  web_index, readarr, kapowarr)`; order user-editable, persisted in `AppSetting`
  key `fetch_source_priority` (global) / `…:user:{id}` (per-user). `acquire()`
  iterates `for r in order` **sequentially, returns on first route success**
  (`acquire.py:194-285`). → **R17 is already satisfied at the route level.** (The
  parallelism inside a route — Prowlarr fans out query *variants* via
  `asyncio.gather`, `release_matcher.py:882` — is *within* one source and stays.)
- **Series enumeration**: `series.py` `detect_series()` resolves a book's series
  and enumerates the full ordered roster (Hardcover → OpenLibrary → Google Books),
  cached in mem + `CatalogWork.extra["series_members"]`. `acquire_series()` already
  requests "all/selected" members (cap 30). → **R10/R11 are mostly built.**
- **VT is already a true gate** for torrents: `torrents._finish()` →
  `torrent_scan.scan_gate()` runs **before** `import_completed()`
  (`torrents.py:221`). → R23 is partly satisfied (gate position correct).
- **Verify-after-download** (`verify.match_titles`) already mirrors LL's
  post-process re-verify. → keep.
- **Periodic release check**: `metadata_sync.check_releases()` (6 h) +
  `QueuedHook` auto-hook (reasons related/goodreads) — the pattern to extend for
  follow. → reuse, don't reinvent.

### Reconciliation table
| Req | Verdict | Notes / change |
|---|---|---|
| R1 | CONFIRMED | Adopt LL's **matching** (unified normalize → `token_set_ratio` author+title avg → threshold → re-verify) + **failedsearch backoff**. Not a wholesale rewrite. |
| R2 | ADJUSTED | Shelf already has usenet (`downloads.py`→SAB) + torrent (`torrents.py`→qBittorrent) via Prowlarr. Matching is shared for these two but **not** with AA. Main LL lesson = unify, plus the `failedsearch` backoff table. |
| R3 | CONFIRMED | `web_index` route untouched. |
| R4 | CONFIRMED→FOLD | AA/`libgen.py` is a **separate** path today (direct search+download, minimal title-only match). Its search *does* return candidates → it **can** join the unified scorer. Fold AA into the same match surface (Stage A). |
| R5 | **AT-RISK** | Prior backtest proved matching changes recovered ≈0 because **~88% of stuck titles return zero releases anywhere** — the 1:10 yield is **availability**, not matching ([[shelf-matching-fix-2026-06-16]]). A matching rewrite alone is unlikely to hit +15%. Realistic levers for "+15% found": (a) AA fold-in catches releases the usenet-only path missed, (b) the **per-source retry-when-available queue** (R19-R21) recovers titles blocked by transient 503/quota, (c) ISBN/alt-title/series enumeration surfaces alternate editions. **Keep R5 as the decision gate, measure with `scripts/backtest_matching.py`, and report honestly if availability-bound.** |
| R6 | CONFIRMED | Rename Missing→Wanted (`routers/missing.py`, `Missing.tsx`). |
| R7-R9 | CONFIRMED | Add sort by author/series/title. Author is a **string** (no Author table) and series lives on the work — sort is feasible from existing fields. "Follow" parts → Stage E. |
| R10 | ALREADY (wire-up) | `detect_series()` gives the roster; Wanted page just needs to render owned/missing/wanted state per member. |
| R11 | ALREADY | `acquire_series()` exists; expose the button on Wanted. |
| R12 | CONFIRMED | New Settings toggle (off by default) → auto-enqueue siblings on member request. |
| R13 | ADJUSTED | Catalog already has rich taxonomy (`catalog_groups`/`catalog_tags`: genres/themes/demographics/format, popularity-normalised). "More comprehensive" = wire **author + series as first-class facets**, not a taxonomy rebuild. |
| R14 | CONFIRMED | "Request all by author": enumerate via the same providers `series.py` uses (`inauthor:` Google Books / OpenLibrary; Hardcover if keyed). No Author table needed for v1 — query on demand, cap N. |
| R15-R16 | CONFIRMED (new) | No follow/subscription exists. New `Subscription` table (kind=author\|series) + periodic enumerate-and-diff tick extending `check_releases`. |
| R17 | ALREADY | Sequential ordered cascade exists (see above). |
| R18 | ADJUSTED | Today on no-match the route just returns nothing and the cascade advances — but there is **no record** it was tried. Add the per-source memo so "advanced past, don't repeat" is durable (feeds R22). |
| R19-R21 | **CONFIRMED (core new work)** | Today: a rate-limited route backs off the *job*; an HTTP error marks the **whole title** unavailable (all routes) until `next_check_at`. There is **no per-source "schedule for when this one source is back, move to next now, drop from its queue if a later source wins, else keep queued"**. This is the central build: a **per-(work, source) search-state machine** (Stage B). |
| R22 | CONFIRMED (core) | No per-(title, source) memo exists today; `ContentRequest` gates all routes together. The new state table makes "never re-search an already-searched source" durable. |
| R23 | ADJUSTED | Gate position already correct, but it **fails OPEN** on VT errors/rate-limit and on "unknown/still-analysing" (404) it imports by default. Make it a **hard** gate: defer instead of fail-open (Stage C). |
| R24 | CONFIRMED (new) | Today VT is a 15 s throttle (4 rpm), **no queue**, no defer-on-pending. Build a real **pending-VT queue** honoring per-min/day caps; hold the file until a clean verdict. |
| R25 | CONFIRMED | New VT-queue stats (depth/pending/cleared/blocked/waiting-quota) → Statistics tab (extend `StatisticsPanel.tsx` + a stats route). |
| R26 | CONFIRMED | Improve `/stats/pipeline` overview to reflect per-source queue states + VT queue once B/C land. |

### Deliberate divergence from LazyLibrarian (decision needed — see Open Questions)
LL **queries every enabled provider, then ranks all results together** with
provider priority only as a *tiebreak* — it does **not** stop at the first source.
The user's brief is the **opposite**: strict priority order, exhaust one source,
stop at first match, never parallel across sources. **We adopt LL's matching,
backoff, and Wanted/Snatched state model, but keep Shelf's ordered cascade.** This
is intentional; flag it because it means we are *not* copying LL's search loop.

---

## Stage 2 — Staged implementation plan

Six waves. Each independently shippable, each with a verify step. Order chosen so
the matching foundation and the per-source state machine (the risky core) land and
prove out before the UX that depends on them. Ponytail throughout: extend existing
tables/ticks, no new services, shortest diff.

### Wave A — Unified matcher + AA fold-in + the route-outcome contract  (R1, R2, R4, R5)  ✅ DONE 2026-06-20 (committed, NOT deployed)
> Implemented per `WAVE_A_SPEC_2026-06-20.md`: `verify.score_candidate` shared core,
> `libgen` delegates to it + content gates (companion/boxset-for-single/language),
> new `outcome.py` (`Outcome`/`RouteResult`), `acquire()` refactored (public dict +
> CODE-H1 byte-identical). Reviewed (backend-architect spec + code-reviewer; P1
> request-aware boxset adopted). Invariant held: `release_matcher.py` + 7 protected
> test files unchanged; suite 897 pass. **R5 NOT measurable in this env** (Prowlarr
> partially unreachable + AA returned no hits = availability-bound, exactly as the
> plan predicted); backtest AA arm is wired for when upstreams are healthy.

- Extract release scoring into one entry point used by Prowlarr (usenet+torrent)
  **and** AA/libgen: candidates → normalize (adopt LL `dictrepl`/`cleanName`
  improvements into `fuzzy.py` only where they beat current) → `author` +
  `title` token-set similarity → threshold → ranked list.
- `libgen.py` search results flow through that scorer instead of title-only match.
- Keep `verify.match_titles` post-download as the second gate.
- **NEW (review-driven, blocks Wave B): define a structured route-outcome type
  here.** Today every route block in `acquire.py:201-269` swallows failures into a
  `last_err` string and returns job-or-`None` — a 503, a quota stop, and a genuine
  no-match are **indistinguishable**. Wave B cannot exist without telling them
  apart. Each route must return `matched | no_match | exhausted(all_broken) |
  unavailable(retry_at, reason) | error`. Defining it in A means the six route
  blocks are refactored **once**, and B just consumes the outcome.
- **Verify:** extend `scripts/backtest_matching.py` to A/B old vs new over the
  current Wanted list; report % of currently-missing titles newly found.
  **Gate = R5 (>15%); if availability-bound, report that explicitly** rather than
  claiming a win.

### Wave B — Per-source search-state machine  (R18-R22) ← core  ✅ BACKEND DONE 2026-06-20 (committed, NOT deployed; frontend info-icon pending)
> Implemented per `WAVE_B_SPEC_2026-06-20.md`: `work_source_searches` + `source_attempts`
> tables (migration 0034), `source_state.py` (CAS lease + state machine), B-min search-
> failure signal in `release_matcher` (contextvar; ranking byte-identical), `acquire()`
> lease/record around the loop (matched dicts + CODE-H1 byte-identical), worker
> exhausted/unavailable hooks, R20 drop-on-real-import, `source_retry_tick` replacing
> `missing_recheck_tick`, recheck = reset+reacquire, per-source state in the missing API.
> Reviewed (code-reviewer): fixed **P0** (transient SAB outage mid-cascade wrongly
> terminal → now `unavailable`/retry) + **P1** (matched backstop keyed to wrong cluster
> member). Invariant held: `test_acquire.py` + 3 matcher test files unchanged; suite 910.

- New table **`work_source_search`, a CHILD of `content_requests`** (FK to
  `content_request.id`, unique on `(content_request_id, source)` for free
  idempotent upsert like `ledger.py:97`). **Not** a JSON column on
  `content_requests` — the scheduler must *index-filter* "due rows where
  `next_retry_at ≤ now`" and must hold a *row lease* (a JSON blob re-introduces the
  two-writer race). Columns: `source`, `status ∈ {pending, searching, no_match,
  exhausted, unavailable, matched, skipped}`, `last_attempt_at`, `next_retry_at`,
  `last_http_status`, `reason`, `lease_token`, `leased_at`.
  - **`exhausted` distinct from `no_match`** (review): "found releases, all broke"
    (`downloads.py:639` `all_broken`) is still terminal-for-this-source per R22 but
    must not be lost as a plain no-match.
- **Global priority only** for the durable machine (review): `ContentRequest` is
  user-agnostic (`models.py` cluster, no `user_id`); per-user ordering stays a
  first-attempt UI nicety, the durable per-source progression uses
  `acquire.global_priority` (`acquire.py:51`). Avoids a user_id on the table that
  would break the cross-user download dedup (`downloads.py:460-480`).
- Rewrite the cascade driver around the Wave-A outcome type:
  - iterate sources in global priority order;
  - **skip any source already terminal** for this work (`no_match` / `exhausted` /
    `matched` / `skipped`) → R22;
  - **lease the row** (`UPDATE … WHERE status IN (pending,unavailable) AND
    lease_token IS NULL`, CAS like the crawl-job lease `scheduler.py:538-559`)
    before searching → kills the retry-tick-vs-live-`acquire` double-search race;
  - search; on **no_match/exhausted** → record terminal, advance → R18;
  - on **match** → record `matched` **provisionally** (a download job was only
    *enqueued*, `acquire.py:231` returns `"downloading"`); **do NOT drop upstream
    `unavailable` rows yet** → R20 deferred to resolve, see below;
  - on **non-200 / over daily quota** → record `unavailable` + `next_retry_at`,
    advance to next source NOW → R19.
- **R20 wired off real import, not enqueue** (review, top correctness risk): the
  "drop this work's upstream `unavailable` rows" fires from the **`mark_resolved`**
  hook (`ledger.py:155`, fired at true import `import_completed`), not from the
  provisional match. If the enqueued download later fails verification
  (`downloads.py:_grab_next`, `torrents.py:238`) the `matched` row reverts and the
  upstream queue is **not** lost.
- **Source availability = a persisted append-only attempt ledger, NOT
  `ratelimit.py`** (review, foundational): `ratelimit.py` is an in-memory monotonic
  *spacer* with no day-counter and no persistence (`ratelimit.py:19,42`) — it
  cannot yield a durable `next_retry_at`. Clone the proven `UsenetGrab` per-listing
  daily-cap pattern (`models.py:859`, `downloads.py:91-107`) into a small
  `source_attempt(source, at)` ledger → restart-safe "calls used today / next free
  at T", exactly like `_grab_blocked_until` (`downloads.py:107`). For a plain
  non-200 (not quota) use a backoff from `last_attempt_at` (reuse the 6 h transient
  recheck, `ledger.py:40-41`). **Do not use `Source.max_daily_requests`** — that is
  the web-crawler `Source` table; download sources are `Integration` rows
  (Prowlarr/SAB/qB, `acquire.py:114-118`).
- **Subsumes, not sits beside, the title-level gate** (review, sequencing): Wave B
  **replaces** `missing_recheck_tick` (`scheduler.py:1325`) and changes
  `force=True` (`acquire.py:162`) from "ignore gate, re-search everything" to
  "search only non-terminal sources". Otherwise the old recheck tick re-searches
  `no_match` sources and violates R22.
- **Scheduler**: one tick drains leased `unavailable` rows whose `next_retry_at ≤
  now` and whose source is available, searching **only that source** → R21+R22.
- **Verify:** unit test the state machine with a fake multi-source harness —
  no_match advances; unavailable requeues; **downstream import (not enqueue) clears
  upstream queue, and a failed download re-opens it**; an already-searched source
  never repeats; two concurrent drivers can't double-search a leased row.

### Wave C — VT hard gate + pending queue  (R23-R25)  [revised per backend review]
- **Reuse `DownloadJob.status="vt_pending"` + `not_before`, NOT a new table**
  (review): the deferred-job machinery already exists (`downloads.py:_resume_deferred`,
  poll-tick drain). The only real work is that the **torrent** poll tick
  (`torrents.py:254`) currently has **no** deferred/resume path — add one.
- Replace fail-open with **defer** for the two cases that *can* resolve by waiting:
  VT **rate-limit** and VT **outage**. A worker drains `vt_pending` jobs when a slot
  frees, re-checks the hash, imports on clean verdict, blocks+deletes on malicious.
- **Durable day cap** (review): the 4/min spacer can stay in `ratelimit.py` (volatile
  is fine for spacing), but **500/day must persist** — clone `UsenetGrab` as a
  `vt_submission(at)` ledger and compute "slots free today / next slot at" via the
  `_grab_blocked_until` pattern (`downloads.py:91-107`).
- **Parked-torrent safety** (review): a parked file is fully downloaded and **still
  seeding an unscanned, possibly-malicious torrent** (removal only post-verdict,
  `torrents.py:223`). So on park: **pause the torrent**, enforce a **max-park-age**
  that fails the job rather than holding forever, keep parked files out of the
  staging GC (`downloads.py:190`), and surface **parked bytes** in stats.
- **VT is hash-lookup-ONLY today** (review, `virustotal.py:3` "we never upload"):
  a 404 = permanently *unknown*, so "wait & re-submit until analysed" is
  **incoherent** without adding a `POST /files` upload + analysis-poll path — which
  reverses a deliberate privacy/quota decision. → **Open Question 5.** Default in
  this plan: keep lookup-only; `vt_block_unknown` stays a hard block-or-allow with
  **no** "wait for analysis" state. Parking applies to rate-limit/outage **only**.
- Stats: queue depth, waiting-on-quota, parked bytes, cleared/blocked → Statistics.
- **Verify:** a rate-limited VT response defers (not imports); a clean verdict
  releases the file; the day cap throttles the worker; a parked torrent is paused
  and a max-age park fails the job.

### Wave D — Wanted page  (R6-R12)  [revised per UX review — reuse, don't rebuild]
- Rename Missing→Wanted end to end (route stays `/missing` internally or alias;
  page + nav relabel).
- Sort by author / series / title as a **single `Select` control** beside the
  existing admin filters (`Missing.tsx:177-181`); "Ungrouped" bucket for rows with
  no detected series (R7-R9, sort half). Sort ≠ Follow — no follow buttons here.
- **List stays FLAT, one row per requested title** (review P0: inline rosters flood
  the page — 9 rows for one book). Series surfaced as a **compact chip** in the
  existing metadata line: `Series · {name} · 3/9 owned · 1 wanted`, driven by the
  cheap persisted `series`/`series_position` (`series.py:553`). Clicking the chip
  opens the **existing `SeriesModal`/`SeriesLibraryModal`** (CatalogCard.tsx:390,
  Library.tsx:899) which already render the roster + owned/wanted badges + a
  confirm-gated "fetch missing". **`detect_series()` runs lazily on expand, never
  per-row on load** (review: it's a ~5-call cross-API lookup, `series.py:357`) →
  R10/R11 via reuse.
- Settings toggle `auto_request_series` (off default); when on, requesting a member
  enqueues siblings → R12. **Every auto-added row carries an origin tag** (extend
  the existing goodreads-origin pattern, `Missing.tsx:115-119`): `from series "X"`
  so the user sees *why* it appeared.
- **Verify:** request book 5/9 → row shows a `3/9` chip, modal lists all 9 with
  correct badges; manual series fetch enqueues the missing ones; toggle on
  auto-enqueues and the new rows show `from series`.

### Wave E — Follow author / series  (R14-R16, follow half of R7/R8)  [revised per UX review]
- New `subscription` table: `(kind ∈ {author, series}, key, user_id, active,
  auto_request bool, last_checked_at)` — **per-subscription `auto_request`**, no
  blanket global auto-follow (review: flood risk; rescope).
- Catalog title detail: make the **author a menu affordance** (`CatalogCard.tsx:282`)
  → "Request all by {author}" + "Follow {author}"; series view → "Follow series".
- **"Request all by author" = enumerate-first, then counted confirm** (review P1):
  query the `series.py` provider helpers, then open the existing `useConfirm`
  (`CatalogCard.tsx:439`) showing the real count + destination shelf, routed through
  the `SeriesModal` checkbox list so it's reviewable. **Backend-enforced hard cap**
  mirroring `SERIES_ACQUIRE_CAP=30` (`series.py:31`) — never silently truncate.
- **Dedicated "Following" view** (tab beside Wanted): lists each subscription
  `name · following since · X auto-added · [Unfollow]` — the auditable home + off
  switch the auto behavior needs (review P0).
- Periodic tick (extend `check_releases`): for each active subscription, enumerate
  current bibliography / series roster, diff vs known, and for `auto_request`
  subs create Wanted rows tagged `following {author}`; non-auto subs surface new
  volumes as one-click suggestions in the Following view. Toast on auto-fire.
- **Verify:** follow an author (auto on) → seed a new title in the provider stub →
  tick creates a `following {author}`-tagged Wanted row + toast; unfollow stops it;
  the Following view shows the count and unfollow works.

### Wave F — Stats overview refresh  (R26)
- Extend `/stats/pipeline` + `StatisticsPanel.tsx` to show per-source queue depth
  & next-retry, VT-queue stats, and follow/subscription counts.
- **Verify:** numbers reconcile with DB state after a seeded run.

---

## Open questions for the operator (blockers before build)
1. **Cascade vs LL "query-all-then-rank":** confirm we keep Shelf's strict ordered,
   stop-at-first-match cascade (your brief) and do **not** adopt LL's query-all
   model. (Plan assumes yes.) — *open, low-risk; matches the brief.*
2. **R5 reality:** ✅ **RESOLVED 2026-06-20 — measured decision gate.** Build the
   matcher + AA fold-in + retry-when-available queue, backtest, and report the real
   number honestly; "availability-bound, no matching win" is an acceptable outcome.
3. **Author following without an Author table:** v1 enumerates an author's
   bibliography on demand via Google Books/OpenLibrary/Hardcover (no new Author
   entity). Acceptable, or do you want a durable Author table? — *open; default no.*
4. **Auto-request scope:** auto-series (R12) is global-off. Auto-follow is rescoped
   to **per-subscription** `auto_request` (no blanket global) with the
   `SERIES_ACQUIRE_CAP`-style hard cap. Confirm this is the desired model. — *open;
   default as stated.*
5. **VT upload:** ✅ **RESOLVED 2026-06-20 — keep lookup-only.** Wave C defers/parks
   **only** on VT rate-limit or outage; "unknown" hash stays a hard block-or-allow
   per `vt_block_unknown`, no wait-for-analysis. No `POST /files` upload path.

## Sub-agent reviews adopted

**backend-architect (Waves A/B/C, sequencing)** — all major findings folded in:
- Wave A now owns the **structured route-outcome type** (was the unsurfaced real
  cost of B); A refactors the six route blocks once, B consumes the outcome.
- `work_source_search` is a **child of `content_requests`** with a **row lease**
  (CAS) — not a JSON column (kills the two-writer race + keeps the drain indexable).
- **Global priority** for the durable machine (no user_id → preserves download
  dedup); added the `exhausted`/`all_broken` terminal state.
- **R20 fires on `mark_resolved` (real import), not on enqueue** — top correctness
  fix; a failed download re-opens the upstream queue.
- Availability/`next_retry_at` from a **persisted `source_attempt` ledger** (clone
  of `UsenetGrab`), **not** in-memory `ratelimit.py`; **not** `Source.max_daily_requests`
  (wrong table — that's the crawler).
- Wave B **subsumes `missing_recheck_tick`** and redefines `force=True`.
- Wave C **reuses `vt_pending`/`not_before`** (no new table) but must add a resume
  path to the **torrent** poll tick; **persisted `vt_submission` ledger** for the
  500/day cap; **pause-on-park + max-park-age + parked-bytes stat** for the seeding/
  disk risk; the **VT-upload incoherence** → Open Question 5.

**ux-researcher (Waves D/E)** — all major findings folded in:
- **Reuse** `SeriesModal`/`SeriesLibraryModal`/`useConfirm`/`pickShelf` — Waves D/E
  are mostly wiring existing pieces.
- Wanted list **stays flat**; series behind a **collapsed chip → existing modal**;
  **no inline rosters** (P0 flood fix); `detect_series()` **lazy on expand**.
- **Sort control ≠ Follow action**; Follow gets a dedicated **"Following" view**.
- "Request all by author" = **enumerate-then-counted-confirm + hard cap** (P1).
- **Origin tags** on auto-added Wanted rows (P0 legibility); **drop blanket
  auto-follow** for per-subscription `auto_request`.

**Deferred / not adopted (with reason):**
- Durable **Author table** — not built for v1 (on-demand enumeration suffices;
  both reviews agree). Revisit only if author-following proves hot.
- VT **upload/analysis lifecycle** — deferred to an explicit operator decision
  (OQ5); plan ships lookup-only.
- LL's **query-all-then-rank** search loop — deliberately not adopted (keeps
  Shelf's ordered cascade per the brief; see Stage 1 divergence note).
</content>
</invoke>
