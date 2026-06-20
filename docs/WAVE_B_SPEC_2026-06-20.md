# Wave B — Implementation Spec (per-(work, source) search-state machine, R18-R22)

Derived from a read-only deep-dive (backend-architect) refining
`PIPELINE_OVERHAUL_PLAN_2026-06-20.md` Wave B. Wave B **changes live gating
semantics** (subsumes `missing_recheck_tick`, per-source instead of title-level).
All paths under `/root/Shelf/backend`.

## Five conflicts the design surfaced (resolutions)
- **#1 (BLOCKING, technical):** `release_matcher.find_releases` (`:872-880`) swallows
  EVERY Prowlarr search exception (503/429/quota) into `[]` → bubbles up as
  `job=None` → recorded `NO_MATCH` → terminal (R22 lockout). A Prowlarr outage is
  byte-identical to a real no-match at acquire time. **R19 is impossible without
  surfacing this.** Resolution **B-min**: `find_releases` also returns a
  failure signal (`None | "blocked" | "rate_limited"`); ranking/scoring stays
  bit-identical. This DOES add to `release_matcher.py` (Wave A kept it pristine;
  that invariant was Wave-A-only). **Required to land with Wave B.**
- **#2:** EXHAUSTED is a WORKER-time verdict, not acquire-time. `acquire()` only
  knows `matched|no_match|unavailable` (a job enqueued = `matched`; the
  no_match-vs-all_broken split happens later in `downloads._grab_next:639` /
  `libgen:1196`). So `exhausted` is written from the download hooks, not the loop.
  Plan §196 overstated acquire-time knowledge.
- **#3 (simplification):** on download FAILURE the correct transition is
  `matched→exhausted` (terminal), **NOT** the plan's `matched→pending` reopen — the
  source already exhausted its candidate list in one job, and broken releases are now
  in `BrokenRelease` (filtered at `release_matcher:891`), so re-searching re-finds the
  same junk. R20's "don't lose the upstream queue on failure" is satisfied by
  **never dropping upstream rows until real import** (§3.1) — no reopen logic needed.
  Fewer states, less code.
- **#4 (USER-FACING) — RESOLVED:** admin "Recheck now" **RESETS the durable rows**
  (status `no_match`/`exhausted`/`unavailable`→`pending`, clear leases) then re-acquires
  full-cascade — the human "try everything fresh" override. PLUS surface the per-source
  state in the Wanted/Missing UI: an **info icon per title** whose popover shows, per
  source, the **last result** (status + reason), the **date** (`last_attempt_at`), and
  the **sources searched**. Backend: expose the work's `work_source_searches` rows via
  the missing/wanted API (extend `MissingRequestOut` or a sibling endpoint). Frontend:
  an info icon + popover in `Missing.tsx`.
- **#5:** No existing per-source day-cap for download sources (`UsenetGrab` is
  per-release; `ratelimit.py` in-memory; `Source.max_daily_requests` is the crawler).
  New tiny `source_attempt` append-only ledger (modeled on `UsenetGrab` +
  `_grab_blocked_until`). Day cap is **opt-in** via `Integration.config.max_daily_requests`,
  default uncapped (backoff-only). Plain non-200 = fixed 6h backoff; quota = computed
  `next_source_free_at`.

## Schema
- **`work_source_searches`** (child of `content_requests`, FK + unique
  `(content_request_id, source)`): `source`, `status ∈ {pending, searching, no_match,
  exhausted, unavailable, matched, skipped}`, `last_http_status`, `reason`,
  `last_attempt_at`, `next_retry_at` (indexed), `lease_token`/`leased_at` (CAS lease,
  mirrors `CrawlJob`), `attempts`. **Rows only for the 3 durable download sources**
  (torrent/pipeline/libgen); web_index/readarr/kapowarr stay row-existence checks.
- **`source_attempts`** (append-only): `source`, `ok`, `created_at` (indexed) — powers
  "is source S available now / next free at T".
- Migration `0034` mirrors `0031` (idempotent inspect-before-create). No backfill:
  legacy ContentRequests lazily get `pending` children on next acquire; a thin legacy
  sweep in the retry tick covers unattended legacy `unavailable` rows.
- New module **`source_state.py`**: `ensure_rows`, `terminal_sources`, `lease` (CAS),
  `record`, `drop_upstream_unavailable`, `due_unavailable`, `source_available_now`,
  `next_source_free_at`.

## Cascade driver (acquire.py)
- Before loop: `ensure_rows(req, usable_durable_sources)`; compute `terminal` skip-set.
- In loop, for a durable source: `if r in terminal: continue` (R22, applies even under
  `force`); else `lease()` (CAS `UPDATE … WHERE status IN (pending,unavailable) AND
  (lease free OR stale)`) — kills the tick-vs-live double-search race.
- Map RouteResult: job→`matched`+return SAME dict; `None`(real empty)→`no_match`;
  raised/search-failed→`unavailable`+`next_retry_at`. Bottom title-level
  `mark_unavailable` KEPT as the coarse gate.
- One `SourceAttempt` written per durable search.

## Provisional match (the top correctness fix)
- R20 "drop upstream unavailable rows" fires from `ledger.mark_resolved` (REAL import,
  `import_core:329`/`libgen:986,1142`) — add an optional `source` param to
  `mark_resolved` → calls `drop_upstream_unavailable` (sets other `unavailable` rows
  `skipped`). NOT from the acquire-time match.
- Download FAILURE hooks (`downloads:603,640`, `torrents:238,299`, `libgen:1185,1196`)
  set the source row `exhausted` (or `unavailable` for libgen `blocked`), guarded
  `if req is not None`; audiobook jobs skipped (ebook-only v1).

## Retry tick (replaces missing_recheck_tick)
- `source_retry_tick` (same 30-min APScheduler slot): selects `due_unavailable` rows
  (`status=unavailable`, `next_retry_at<=now`, lease free/stale, parent not resolved),
  re-checks `source_available_now`, then `acquire(route=row.source, force=True)` —
  searches ONLY that source (R21). Plus: reap stale `searching` leases→pending; a
  legacy sweep for `unavailable` ContentRequests with zero children; a backstop for
  `matched` rows whose job died. Old `missing_recheck_tick` removed (net-zero jobs).
- `force=True` redefined: bypasses the TITLE gate but still honors per-source terminals.
- Missing/Wanted **page** (GET `/missing`) reads only ContentRequest → untouched.

## Availability / next_retry_at
- `source_available_now(source)`: `count(SourceAttempt in last 24h) < daily_cap` (cap
  from `Integration.config.max_daily_requests`, None=uncapped→always available).
- `next_source_free_at`: `_grab_blocked_until` math — `times[len-cap] + 24h`.
- non-200 → `next_retry_at = now + 6h` (`ledger._TRANSIENT_RECHECK`); quota →
  `next_source_free_at`.

## Tests
- **`test_acquire.py` must pass UNEDITED** (Wave B invariant): matched dicts, cascade
  order, CODE-H1 unchanged; a fresh title's rows all start `pending` so the lease never
  skips a source.
- Rewrite `test_ledger.py:206,242` (the recheck-tick tests) for `source_retry_tick`;
  add a reset-sources test for the recheck endpoint (decision #4a).
- `test_downloads/torrents/libgen`: additive assertions for the `record(exhausted)`
  writes; existing `mark_unavailable` assertions stay valid.

## Top risks
1. Indexer-down→permanent no_match (#1) — **blocking**, B-min required.
2. Provisional match never resolves (stuck SAB job) → backstop in the tick.
3. Lease leak on crash → `leased_at < stale` reap.
4. acquire-vs-tick parent race → unchanged (ledger already IntegrityError-safe); per-
   source rows are CAS-leased.
5. `exhausted` written from worker hooks lacking parent context → `req = ledger._get`,
   guard `if req is not None`.
</content>
