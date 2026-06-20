# Wave C — Implementation Spec (VT hard gate + pending queue, R23-R25)

Read-only deep-dive (backend-architect). **Decisions locked:** VT stays
HASH-LOOKUP-ONLY (no upload, no analysis-poll); parking applies ONLY to VT
rate-limit/outage; an "unknown" (404) stays a hard block-or-allow per
`vt_block_unknown` (NO wait-for-analysis). Reuse `DownloadJob.status` + `not_before`;
the only new table is `vt_submissions` (clone of `UsenetGrab`).

## Today (the fail-open to fix)
`torrent_scan.scan_gate()` (`torrent_scan.py:79-116`) returns bool; **fails OPEN** on
any `IntegrationError` (`:101-104`) — incl. rate-limit/outage. Called in
`torrents._finish` (`:221-224`) BEFORE `import_completed` (gate position correct).
`virustotal.lookup` returns None on 404, re-raises a string-only `IntegrationError`
otherwise (no typed rate-limit exception). 4/min spacer in `ratelimit.py` is in-memory
(can't back 500/day). The torrent tick (`torrent_poll_tick:256`) has NO deferred/resume
path and NO `_poll_lock`.

## Changes (file-by-file)
- **`virustotal.py`**: add `VTUnavailable(IntegrationError)`; in `lookup`, map
  429/503/connection/timeout → `VTUnavailable`; 404 → None; 401/other → plain
  `IntegrationError` (hard error, never park).
- **`qbittorrent.py`**: add `pause()` (symmetric to `resume:173`; qBit 5.0 `/torrents/stop`
  with `/torrents/pause` 404-fallback).
- **`models.py`**: `VtSubmission` (id, created_at indexed) near `UsenetGrab`; migration
  0035 to create `vt_submissions` (mirror 0034 idempotent style).
- **`torrent_scan.py`**: `scan_gate` returns **`"block"|"allow"|"park"`**; pre-flight
  `vt_blocked_until(db)` → `"park"` (before hashing); record ONE `VtSubmission` after each
  successful `lookup` (not on raise); catch `VTUnavailable` → `"park"` (replaces fail-open).
  Add `vt_blocked_until(db)` (clone `_grab_blocked_until:91-107`) enforcing BOTH per-min
  (4) and per-day (500) caps, returning the later block time; caps overridable via
  `vt.config`.
- **`torrents.py`**: add module `_poll_lock` (MANDATORY — resume makes overlap harmful);
  `_park_for_vt` (pause torrent, set status=`vt_pending`+not_before, enforce max-park-age)
  + `_resume_vt_pending` (re-check age, re-run scan_gate, block/re-park/allow→import);
  `VT_MAX_PARK=24h` (fail+delete+notify on expiry); 3-state mapping in `_finish:221-224`;
  drain `vt_pending` (status==vt_pending, grab_kind, not_before<=now) at TOP of
  `torrent_poll_tick`; `parked` in the return dict; prune old `VtSubmission`.
  **INVARIANT: `vt_pending` must NOT be added to `import_core.ACTIVE_STATUSES`** (it's
  drained only by the dedicated not_before query; adding it breaks `_reap_orphans`).
- **`routers/integrations.py`**: extend `virustotal_usage:103` with a `queue` block
  (depth, parked_bytes, waiting_on_quota, next_slot_at, blocked).
- **`frontend integrations.ts` + `StatisticsPanel.tsx`**: add `queue?` to `VirusTotalUsage`;
  render a queue sub-section in `VirusTotalStatsCard` (depth amber>0, parked bytes,
  blocked red>0, "waiting on VT quota · next slot" badge).

## Tests (test_torrents.py)
- Update the 6 gate tests bool→3-state (`== "block"`/`"allow"`); **replace**
  `test_vt_gate_fails_open_on_api_error` with `..._parks_on_rate_limit` (`== "park"`);
  `_FakeQB` gains `pause`. Add: rate-limited→parks-not-imports; clean→releases;
  day-cap-throttles; max-park-age→fails+deletes; pause-on-park asserted;
  API-429-safety-net→parks; `VTUnavailable` mapping (401 hard, 429/503 park); ledger
  records one per successful lookup, none on raise, two for a 2-file torrent.

## Top risks
1. `vt_pending ∉ ACTIVE_STATUSES` is load-bearing (comment it).
2. Local ledger vs VT's real counter drift → the `VTUnavailable` 429-catch reconciles;
   max-park-age is the backstop. Both must be present.
3. Unscanned seeding window between completion and pause; a pause FAILURE leaves an
   unscanned file seeding → still set `vt_pending` (age clock runs) + log loudly.
4. Tick concurrency → `_poll_lock` mandatory.
5. Multi-file torrent partially over quota → pre-flight `vt_blocked_until` parks the whole
   job before hashing (all-or-nothing per tick); resume re-hashes all files (idempotent).

## Conflicts with the plan (flagged)
- Plan's "keep parked files out of the staging GC (downloads.py:190)" is misaimed —
  that GC is SAB-staging-only; torrent files live under qBit's save_path, never in scope.
  Durability comes from pause-not-delete. Just verify `storage_path` is set on park.
- The usenet `deferred` machinery is NOT shared — Wave C writes a genuinely new ~40-line
  torrent resume path (only the `not_before`+due-query PATTERN is reused).
- `scan_gate` bool→3-state ripples to 6 existing tests (update, don't fight).
- No typed rate-limit exception exists → `VTUnavailable` is required scaffolding.
</content>
