# Shelf — Matching / Fetching Fix Plan (2026-06-16)

Status: **PLAN — awaiting approval. No engine code changed.**
Author: investigation from live `shelf.service` + `shelf.db` (read-only) + full code trace.

## 0. Problem, measured

Live stock batch (4h old, running): **108 stocked / 941 attempted = 11.5%** ("1:10").
It is **two independent problems**:

| Problem | Evidence | Nature |
|---|---|---|
| **A. Matcher discards correct books** | usenet found a release but verify rejected ~53% (124 failed vs 108 imported); logs: `verify FAILED 'Taming the fire': title 1.00 · author miss (conf 0.50)`, `cascade early-abort … < 0.65 conf` | **matching bug — fixable** |
| **B. No source available** | 708/941 have no usenet release; libgen fallback **100% down** (66/68 jobs `blocked/unreachable (transient)`, 0 stocked / 473 retries today) | **availability — infra, not matching** |

**No rewrite warranted.** The engine already ports Radarr/Prowlarr concepts and is well-structured; the false-negatives are concentrated in one band (author-miss on perfect titles) and are directly targetable. A full-batch ≥90% is impossible while 75% of titles have no source — so the **success metric is: ≥90% of titles that have a release on a reachable source produce a correct download**, proven by an offline backtest before deploy.

---

## Phase 0 — Backtest harness (do FIRST; proves the 90% claim)

Build a read-only replay script (`scripts/backtest_matching.py`, not wired into the app) that:
- pulls the 124 `download_jobs grab_kind='stock' status='failed'` + 115 `content_requests` "all_broken" items,
- re-runs `score_release` / `verify.score_match` (old vs new) over their recorded candidate release names + (where the file still exists) embedded metadata,
- reports: how many flip from reject→accept, and a manual-spot-check sample to estimate false-positive risk.

Gate: only ship Phase A/B if backtest shows recovery **without** a meaningful false-positive rise.

---

## Phase A — Stop discarding correct books (matcher precision)

Root cause: **authors are matched by exact token-set intersection only** — no fuzzy / initials / transliteration (titles get fuzzy via `fuzzy.py`; authors get none). Release names routinely omit the author, so "absent" is wrongly treated as "wrong."

### A1. Fuzzy author helper — `ingestion/fuzzy.py` (new fn)
Add `author_similarity(a, b) -> float` (0..1): accent-fold + lowercase (reuse `extract.norm`), handle `Last, First` ↔ `First Last`, expand/normalize initials (`J.R.R.` → `jrr`), then `token_sort_ratio`. Returns max over name forms. Single source of truth for both call sites below.

### A2. `release_matcher.py:260-261` — pre-download author penalty
Current: `elif author_hit is False: score *= 0.6` (perfect title → **0.60**, lands in the [0.60,0.65) dead band).
Change: compute author via A1; treat **author-absent-from-release-name as neutral**, penalize only a positive *conflict* (author present in the name but dissimilar). Soften the absence factor `0.6 → 0.75` so a perfect-title candidate is **tried** (≥ `CASCADE_ABORT_FLOOR` 0.65) but stays **speculative** (< `AUTO_GRAB_DEFAULT` 0.8 — no blind auto-grab; precision preserved, the real check moves to verify).
`_author_tokens` (`:154-165`) is unchanged; A1 augments the `author_hit` decision.

### A3. `downloads.py:285` / `_remaining_all_doomed` (`:313-330`) — dead band
With A2, perfect-title candidates rise to 0.75 and the [0.60,0.65) trap closes for them. Belt-and-suspenders: align `CASCADE_ABORT_FLOOR` with `MATCH_FLOOR` (0.65 → 0.60) so **nothing the matcher *accepted* is pre-emptively abandoned** — only sub-floor tails. (The code comment at `:283-284` already claims this invariant; this makes it true.)

### A4. `verify.py:227-247` (`score_match`) — the decisive post-download killer
Current: `elif ahit is False: score *= 0.5` → perfect title 1.0 → **0.50** → below `_VERIFY_MIN` 0.6 → good download deleted + release blacklisted.
Change:
- author comparison via A1 (kills transliteration/initials false-misses);
- ISBN short-circuit (Phase B2): exact ISBN → `score = 1.0`;
- keep one-side-empty = neutral (current); for a genuine present-and-disagreeing author soften `0.5 → 0.7` so a **perfect title still clears the 0.6 floor** (a disagreeing embedded author is often a translator/uploader/bad catalog value, not proof of wrong book).

---

## Phase B — Use metadata already fetched (free precision wins)

### B1. Thread alt-titles into verify — `downloads.py:690` + `verify.py`
Currently verify scores the file against the **single English `want_title`** → a book correctly grabbed under its romaji/native title fails on `dc:title`. Fix: at import, `matchmeta.get_work_meta(db, cw, allow_fetch=False)` (already persisted) → pass its `titles` list to `verify.verify_download`; `score_match`/`_title_score` take the **max** over candidate titles. (`find_releases` already uses this list pre-download — verify just isn't given it.)

### B2. ISBN as match confirmation — `verify.py` + `downloads.py`
- `verify._epub_meta`: also extract `dc:identifier` ISBN (PDF: doc info). 
- Thread `want_isbn = cw.extra.get("isbn")` (already stored, `book_catalog.py:476`) into verify; **exact ISBN equality → confidence 1.0**, the single strongest signal available, rescuing author-miss/`NO_AUTHOR_MIN_CONF` cases.

### B3. (optional, larger) Propagate rich provider metadata to the catalog row
`MetadataLink.payload` holds provider ISBN + RanobeDB/AniList aliases on the **Work**, but the matcher reads the thinner `CatalogWork.extra`. In `metadata_sync`/`catalog_enrichment`, copy provider `isbn` + `aliases`/synonyms → `CatalogWork.extra["isbn"]`/`["alt_titles"]` so A/B feed on the best available data and the duplicate AniList round-trip in `matchmeta` can be retired.

---

## Phase C — Availability (the bigger lever for 1:10) — DIAGNOSE first

libgen fallback is **100% down**; this is infra, not matching. Diagnose before changing:
1. Host-level reachability: `curl -I` the libgen mirrors — distinguish **network/DNS/ISP block** from **Cloudflare challenge**.
2. Solver health: is FlareSolverr/zendriver (`flaresolverr.py`, `zendriver_solver.py`, `cf_browser.py`) actually running and passing challenges? The fetcher returns `throttled`/`blocked` (`libgen.py:307-347`) when the browser can't render/solve.
3. Mirror list freshness (`libgen.py`): are `libgen.la/.li` still valid endpoints?
Output → targeted fix (mirror update, solver repair, or proxy). Independent subsystem — can run in parallel with A/B.

Secondary (after C works): the stock libgen path is a **decoupled retry loop**, not an inline cascade like `acquire.py:160-216`. Once libgen is healthy, confirm the `stock_libgen_tick` recovery actually drains issue items; consider scoping auto-selection away from the long-tail titles with zero usenet coverage (708/941).

---

## Verification per change

- **Unit (pytest, suite currently 720 green):** add cases — perfect-title+author-absent-from-name → tried not aborted; `author_similarity` hits on `J. Smith≈John Smith`, `Dostoevsky≈Dostoyevsky`; `score_match` perfect-title+ISBN-match → 1.0; verify passes English-want vs romaji-file via alt-title; cascade keeps ≥floor candidates.
- **Backtest (Phase 0):** % of the 124+115 failures recovered, with FP spot-check.
- **Live:** after deploy, re-measure `grab_kind='stock'` imported-vs-failed (now 108/124) and the `verify FAILED … author miss` log rate over a window.

## Ordering & risk

Phase 0 → A → B → re-backtest → (deploy A/B); **C in parallel** (separate subsystem).
Primary risk of A/B is **false positives** (wrong book grabbed). Mitigations: auto-grab bar (`AUTO_GRAB_DEFAULT` 0.8) stays strict — changes only widen the *speculative→verify* path; ISBN + fuzzy add precision; backtest gates the merge; `auto_grab_min_confidence` / `verify_min` are already live-tunable config, so thresholds can be adjusted without redeploy.

---

## IMPLEMENTED (2026-06-16) + backtest result

Done on `main` working tree (uncommitted), full suite green **788 passed / 4 skipped** (+12 new tests):
- **A1** `fuzzy.author_similarity` (initials/order/transliteration); **A2** release_matcher author-absence
  now neutral-ish (×0.75, + fuzzy hit) instead of ×0.6; **A3** `CASCADE_ABORT_FLOOR` 0.65→0.60 (kills
  the dead band); verify author-miss kept strict ×0.5 (reverted a 0.7 try after two existing
  precision tests correctly failed — the recall win is fuzzy+ISBN+alt, NOT weakening this gate).
- **B1** alt-titles threaded into `verify.verify_download`/`verify_file`/`score_match` (+ libgen import
  path); **B2** ISBN extracted from EPUB `dc:identifier`, ISBN-10/13 normalized, exact match → conf
  1.0 short-circuit; **B3** RanobeDB provider aliases → `CatalogWork.extra.alt_titles` (Google-Books
  ISBN isn't in the catalog-enrichment fan-out, and ISBN already reaches extra via book_catalog/OL).
- **Phase 0** `scripts/backtest_matching.py` (read-only live A/B).

**Backtest finding (decisive):** of 40 currently-stuck titles, **only 5 returned ANY usenet release**
(7 releases total) — **88% have zero availability** — and all 5 are tried by both old and new
formulas. → The stuck cohort is an **availability** failure, not a matcher failure. **No matcher
change — or rewrite — can lift the full-batch yield above ~90%, because the bottleneck is upstream
availability** (long-tail titles absent from usenet + the libgen CDN being down), not scoring logic.
The A/B matcher wins apply to the subset that HAS releases (where verify was rejecting ~53%);
they're correct and unit-tested, but they are not what gates the 1:10.

## Phase C result (diagnosed, not a code bug)
libgen mirrors (libgen.la/.gl/.bz/.vg/.li) are all healthy (200/307); their shared download CDN
`cdn2/cdn3.booksdl.lc` returns **503 from the nginx origin** (`cdn1` is NXDOMAIN) — a LibGen-side
outage, NOT Cloudflare, NOT the VPN, NOT the solver (FlareSolverr healthy). Shelf correctly classifies
it transient. Only real mitigations: wait for the CDN to recover, or add an alternate download source
(Anna's Archive / IPFS CIDs) — a feature, not a bug fix.

## Open questions for operator
1. OK to add a read-only backtest script under `scripts/` (no app wiring)?
2. Phase C: is the libgen block expected (ISP/region) — should I budget for a proxy, or is the solver simply broken?
3. Include the larger B3 (provider→catalog metadata propagation) now, or defer?
