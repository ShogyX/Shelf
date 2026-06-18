# Autonomous Run — Per-Stage Verification Log

Independent sub-agent verdicts (V2) for each batch, plus V1 (100×3 torrent accuracy) and the final
security/bug/regression review. Each entry: batch, verifier verdict, evidence, deploy status.

| Batch | Verdict | pytest | tsc+build | live smoke | regression | deploy | notes |
|---|---|---|---|---|---|---|---|
| A | PASS | 790 passed, 4 skipped | tsc 0 / vite ok | SPA 200; dist has new strings | only 4 src files + client.ts | ✅ | Independent verifier (karen) first FAILED on R12 (jnovel/memory adapters still enabled+surfaced, royalroad disabled); fixed by restoring jnovel/memory hints + keeping royalroad; removed orphaned `reapJobs` client method. Re-verified delta: tsc 0, dist hints present, reapJobs gone. |
| C | PASS | 790 passed, 4 skipped | tsc 0 / vite ok | dist ships HIDDEN_SOURCE_KEYS + HIDDEN_ADAPTERS; Batch A strings intact | only Sources.tsx + AddWork.tsx | ✅ | Sources tab hides memory/jnovel/local_folder/local_import/lf; Add grid also hides memory+jnovel; royalroad kept gated/visible. R12 invariant re-confirmed (grid set {comix,generic_feed,gutenberg,standardebooks} all have hints). |
| B | PASS | 790 passed, 4 skipped | tsc 0 / vite ok | old "System configuration"/"Save system settings" gone; 6 group titles distributed; A+C intact | SystemSettings.tsx, Settings.tsx, Users.tsx | ✅ | System tab dissolved into SystemConfigCard({groups}) (keys-only partial PUT, no clobber); login→Users, crawl+comix→Indexing, imgcache→Storage, flaresolverr→Integrations(admin), log_level→Backups; Auto-backups group dropped (dup); SMTP→Notifications(admin); Goodreads tab removed→Integrations un-gated (card visible to non-admins, rest admin); Blocklist→Acquisition(admin). Automation tab kept (removed in E). All 6 group titles resolve to real GROUPS. |
| E | PASS | 791 passed, 4 skipped (+1 union test) | tsc 0 / vite ok | union via TestClient; tabs have no automation/system/goodreads | missing.py, schemas.py, test_ledger.py, Missing.tsx, client.ts, Settings.tsx, -QueuedHooksCard.tsx | ✅ | Read-time union: pending goodreads QueuedHook rows surface in /missing tagged origin="goodreads" (status "open"), per-user scoped, excluded by reason/non-open status filters. Frontend badge + Source filter; recheck suppressed for goodreads rows (avoids wrong-id recheck); origin-prefixed React keys. Automation tab + QueuedHooksCard removed. No schema change. |
| D | PASS | 792 passed, 4 skipped (+annas tests) | tsc 0 / vite ok | in-process import + 5-case merge edge tests by verifier | libgen.py, integrations.py, provider_catalog.py, test_libgen.py, test_matching_improvements.py, IntegrationsManager.tsx | ✅ | AA-only search: providers=["annas"], _FALLBACK empty, _PROVIDERS={annas}; dropped zlib/ocean/liber3 search + zlib creds; KEEP libgen mirror download route (md5→ads/get→annas fast-download), kind="libgen". CRITICAL: annas_key now redacted on read + preserved on update (merge keeps stored key when UI omits it; strips _set flag) — verifier confirmed no data-loss across 5 edge cases. Frontend: AA secret-key field with set/not-set indicator (blank=keep); provider picker + zlib fields removed. provider_catalog relabeled "Anna's Archive", auth=key. |
| F+G | PASS | 803 passed, 4 skipped (+11 torrent/VT tests) | tsc 0 / vite ok | qBit client + VT API live-validated; 3 self-checks; verifier confirmed all safety invariants | qbittorrent.py, virustotal.py, torrents.py, torrent_scan.py, downloads.py, acquire.py, release_matcher.py, scheduler.py, notifications.py, integrations.py, schemas.py, provider_catalog.py, IntegrationsManager.tsx, Settings.tsx, client.ts, scripts/torrent_match_verify.py | ✅ | **Pre-flight (operator-requested): qBittorrent client live-tested vs v5.1.4 (found+fixed None-param bug); VirusTotal key live-tested (malicious/clean/unknown all correct).** F: qBit client + torrent grab (release_matcher protocols=torrent, R22) + torrent_poll_tick reusing client-agnostic _import_completed; SAB poll_tick excludes grab_kind torrent at both sites; torrent route FIRST + configurable, no-op without qBit. G: VirusTotal gate runs between completion and import — malicious→delete+notify(security.malware)+ledger, clean→allow, unknown→policy, no VT→no-op, API error→fail-open; DB-lookup only (no upload). R21 cap: per-min via rpm=4, quota-exhaust fails open+logs; explicit per-day toggle DEFERRED. V1 100×3 harness written (deferred run — needs Prowlarr torrent indexers wired into Shelf + the live torrent E2E). Verifier: no FAIL condition met. |

## Post-review live commissioning + tuning (2026-06-17/18)

Operator added Prowlarr + SABnzbd + (later) metadata integrations; gave the VirusTotal key.
Agent added qBittorrent + VirusTotal integrations and commissioned the torrent route LIVE.

**Path resolution.** Shared NAS-Pool is mounted `/media/NAS-Pool` (qBit/SAB hosts) vs `/mnt/NAS-Pool`
(Shelf). qBit `save_path=/media/NAS-Pool/media/Downloads/shelf` + path-mapping {/media/NAS-Pool→
/mnt/NAS-Pool}. Verified: qBit write → instantly visible on Shelf.

**Live bugs found + fixed (all committed/deployed):**
- explicit qBit save_path (category save-path ignored for manual adds) — `fd49b0f`
- qBittorrent v5 API: resume→/torrents/start, paused→stopped, pausedUP→stoppedUP — `d4c68fa`
- candidate cascade: try next ranked release when a .torrent URL is dead (Prowlarr returns .torrent
  proxy URLs, not magnets; top candidate sometimes unfetchable) — `2489197`
- final security/correctness review (code-reviewer agent): H1 malware-gate bypass for .fb2/.djvu
  (gate now scans verify's full ext set), M1 grab serialization, M3 error/stall/age failsafe
  (4h→45m fall-through), M4 import+hash off the event loop, M2 orphan reaper — `1f578b3`
- sync_all VirusTotal skip (NotImplementedError) — `869e3d1`
- seeder-aware ranking + stall guard (from the search probe) — `68869c1`

**E2E PROVEN:** Huckleberry Finn → torrent match → qBit download (shared pool) → VirusTotal (clean) →
verify → imported to library, in ~10s.

**V1 accuracy (15 popular titles):** CORRECT precision 100%, **0 wrong imports** (the R23 bar). Low
*yield* (most titles NO-RESULT) is torrent AVAILABILITY — Prowlarr reports stale seeder counts but the
swarms are dead (stalledDL@0%); the matcher is correct. Mirrors the libgen finding: availability, not
matching. The catalog skews to public-domain titles (poorly seeded on trackers; they flow via Anna's
Archive). Full 100×3 not run (resource-heavy + availability-bound); the harness `scripts/
torrent_match_verify.py` is ready (non-polluting, purges imports). R21 explicit per-day VT cap toggle
still deferred (per-min via rpm=4; quota-exhaust fails open).
