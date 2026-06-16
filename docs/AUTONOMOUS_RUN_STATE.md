# Shelf Autonomous Implementation — RESUME CONTRACT / STATE

**This file is the single source of truth for the autonomous run. On any (re)invocation, READ THIS
FIRST, then continue from the first unchecked batch.** Plan of record:
`docs/UI_UNIFICATION_REQUIREMENTS_2026-06-16.md` (requirements R1–R24, Batches A–G + V1/V2).

## How to resume (for a fresh agent / scheduled tick)
1. Read this file + the plan doc. Check `HALT` and `LEASE` below.
2. If `HALT=yes` → do nothing but report why; a human must clear it.
3. If `LEASE` holds a timestamp < 30 min old → another run is active; stop.
4. Else take the lease (set LEASE to now + your id), do the **next unchecked batch**:
   implement → `pytest` + `tsc&&vite build` green → **independent verifier sub-agent (V2)** →
   on PASS: commit + deploy (backup DB, restart shelf.service, health-check) → tick the box + append
   to `VERIFICATION_LOG_2026-06-16.md` → release lease. On FAIL: set HALT=yes, record the reason, stop.
5. Branch: `acquire-fixes-2026-06-16` (already deployed: matcher/retry/UI + AA fast-download).

## Control flags
- `HALT`: no
- `LEASE`: (none)
- `LAST_UPDATED`: 2026-06-16T22:?? (init)

## Batch checklist (tick only after the V2 verifier PASS + deploy)
- [x] **A** — quick frontend: R7 Catalog-Layout rename · R8 remove reader top-right dropdown ·
      R9 remove Revive button · R13 grab-title/crawl&index renames · R14 remove broken-cleanup ·
      R12 verify import-format hints. (V2 PASS after R12 fix; deployed.)
- [x] **C** — Sources: keep Gutenberg/StandardEbooks/Comix/GenericFeed/RoyalRoad; remove
      memory-demo/local-folder/local-import/J-Novel. (V2 PASS; deployed. Also hid memory+jnovel
      from the Add-a-title grid for R11; royalroad stays gated/visible in Sources tab.)
- [x] **B** — Settings reorg: dissolve System (login→Users, crawl+comix→Indexing, imgcache→Storage,
      flaresolverr→Integrations, log_level→Backups); SMTP→Notifications; Goodreads→Integrations
      (un-gated); Blocked-content→Acquisition. (V2 PASS; deployed. Automation tab kept for E.)
- [x] **E** — Goodreads "waiting on hook" → Missing tab (read-time union, tag `goodreads`); then
      remove the Automation tab. (V2 PASS; deployed. +1 backend union test; QueuedHooksCard deleted.)
- [ ] **D** — Open Libraries integration → Anna's-Archive-only (secret-key field; keep kind="libgen").
- [ ] **F** — qBittorrent integration + torrent route (Prowlarr torrent-indexer → qBit) +
      torrent-first acquisition order (configurable) + auto-import worker. R22 matching rigor.
- [ ] **G** — VirusTotal integration: hash torrent files, quarantine+notify+log on non-clean,
      optional VT-rate cap. Then **V1** (100×3 torrent accuracy ≥90%) + final security/bug/regression
      review (specialized sub-agents) before declaring done.

## Constraints (honest)
- No quota-introspection tool exists; cannot self-meter session/weekly usage. Mitigation: durable
  per-stage commits + this state file → lossless resume on next invocation. Cron ticks drive the loop
  only while a Claude process is alive/idle; a hard limit that ends the session pauses the run until
  next invocation (resumes here automatically, no re-explaining needed).
- Each batch: DB backup before deploy, git branch (not main), independent verifier gates the deploy,
  final security review at the end. Verifier FAIL → HALT (no deploy).

## Log
- 2026-06-16: run state initialised. Batches A–G pending. (Matcher/retry/UI + AA fast-download already
  live from commits 74a6913, ce5a857.)
