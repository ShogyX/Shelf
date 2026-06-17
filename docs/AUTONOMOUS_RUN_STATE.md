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
- [x] **D** — Open Libraries integration → Anna's-Archive-only (secret-key field; keep kind="libgen").
      (V2 PASS; deployed. AA-only search, libgen mirror download kept; annas_key redacted + merge-preserved
      on update — no data loss; provider picker + zlib creds removed.)
- [x] **F** (V2 PASS — deployed) — qBittorrent integration + torrent route (Prowlarr torrent-indexer → qBit) +
      torrent-first acquisition order (configurable) + auto-import worker. R22 matching rigor.
      **IN PROGRESS (operator decision 2026-06-17: build F+G code now, defer live V1 — no Prowlarr
      torrent indexers / VT key configured yet).**
      - [x] **F1 client** (committed WIP): `app/integrations/qbittorrent.py` QBittorrentClient
        (Web API v2 cookie login, SID cache + re-login on 403, add_torrent/torrents_info/torrent_files/
        set_file_priority/resume/delete, magnet_hash, is_complete) + wired into base.py PIPELINE_KINDS +
        client_for, schemas IntegrationIn.kind regex (+qbittorrent), provider_catalog entry. username in
        config, password in api_key column. Self-check (`python -m app.integrations.qbittorrent`) passes;
        pytest 792 green. NOTE annas_key redaction (Batch D) covers the api_key column already; qBit
        password is in api_key (never returned) — no extra redaction needed.
      - [x] **F2 worker** (V2 PASS) — torrent grab (top-ranked torrent release → qBit add paused → filePrio book
        file(s) → resume; DownloadJob grab_kind="torrent", hash→nzo_id, content_path→storage_path) +
        `torrent_poll_tick` in scheduler.py reusing downloads._import_completed(db, job, qbit_integ)
        (verify→promote→import→link→notify→ledger). poll_tick already excludes grab_kind=="libgen" → also
        exclude "torrent". Seed/keep-after-import policy from config, optional qBit delete.
        REUSE TARGETS: downloads.grab_release/_enqueue (456-552), _import_completed(db,job,sab) (651),
        map_path/_job_dir/_target_dir, get_sabnzbd→add get_qbittorrent. release_matcher already filters
        protocols (search_prefs line ~352 `("usenet",)` default; ProwlarrClient.search takes protocols).
      - [x] **F3 route+order** (V2 PASS) — acquire.py ROUTES/DEFAULT_PRIORITY add "torrent" FIRST
        (`["torrent","pipeline","libgen","web_index","readarr","kapowarr"]`), available_routes gate
        (qbittorrent+prowlarr-torrent enabled), dispatch torrent branch. Frontend FetchPriorityCard
        ROUTE_LABELS (+torrent), IntegrationsManager qBit config form (base_url, username, password,
        category default "shelf", save path, path mappings, seed/keep policy). R22: torrent grabs flow
        through release_matcher score_release + verify (no shortcut); is_boxset/pack reject + seeders bonus.
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

## F+G + V1 status (2026-06-17)
- F (qBittorrent client + torrent route) and G (VirusTotal gate) are CODE-COMPLETE, V2-verified,
  deployed. qBit client + VT key live-validated (qBit None-param bug fixed).
- V1 100x3 run DEFERRED: add qBittorrent + Prowlarr (torrent indexers) + VirusTotal as Shelf
  integrations, then run scripts/torrent_match_verify.py for the live torrent E2E + accuracy gate.
- R21 explicit per-day VT cap toggle deferred (per-min enforced via rpm=4; quota-exhaust fails open).
