# Shelf — UI Unification + Libgen→AA: Requirements (2026-06-16)

Captured verbatim from the operator's request. **Review + surgical plan follow this doc; nothing is
implemented yet.** Each requirement has a stable ID for the plan to reference.

## 1. Libgen → Anna's Archive conversion
- **R1** — Convert the "Open Libraries" (libgen) integration to be **solely Anna's Archive**. Drop the
  other providers/mirrors (libgen mirror family, z-library, OceanOfPDF, liber3) and the libgen host list.
- **R2** — Surface AA **login credentials**: *member* login and *apijson* access (the secret key),
  as described in the AA FAQ → API section (https://annas-archive.gl/faq#api).

## 2. Settings reorganization (unify scattered toggles/knobs)
- **R3** — **Remove the "System" tab**; move its settings into the relevant categories:
  - **R3a** login settings → **Users** tab
  - **R3b** crawl → **Indexing**
  - **R3c** backups → **Backups** tab
  - **R3d** image cache → **Storage**
  - **R3e** comix crawler → **Indexing**
  - **R3f** Cloudflare solverr → **Integrations**
- **R4** — **Remove the "Automation" tab.** Works waiting on a hook (from Goodreads) → move to the
  **Missing** tab, shown with their own tag.
- **R5** — **Remove the "Goodreads" tab**; the Goodreads integration → **Integrations** page.
- **R6** — Move **Email / SMTP** from Integrations → **Notifications** tab.
- **R7** — Rename **"Appearance & layout" → "Catalog Layout"**.

## 3. Reader
- **R8** — When opening a title to read, a dropdown menu appears in the **top-right corner** that
  should not be there — **remove it**.

## 4. Jobs page
- **R9** — Remove the **"Revive stalled jobs"** button.

## 5. "Add" — Sources / Import
- **R10** — In the **Sources** tab (under Add), remove all **non-working** sources: *j-novel*, *x*,
  *in-memory demo*, *local folder* (surfaced/used elsewhere — doesn't belong here), *local import*
  (same as local folder).
- **R11** — Surface **only currently-working sources**; the default settings should be the ones that
  actually work right now.
- **R12** — **"Import title"** should specify the **title format** needed for specific titles.
- **R13** — Rename **"hook & backfill" → "grab title"**; rename **"index" → "crawl & index"**.

## 6. Indexing page
- **R14** — Remove **"clean up broken titles"** from the Indexing page.
- **R15** — Move **"blocked content"** → **Acquisition** (out of the Indexing tab).

## 7. qBittorrent + torrent support for Anna's Archive
- **R16** — Add a **qBittorrent download-client integration**. Live instance: `http://10.10.102.28:8090`
  (qBittorrent **v5.1.4**, Web API v2, cookie login). Config: base URL, username, password (secret),
  category, save path, path mappings, seed/keep-after-import policy.
- **R17** — Integrate qBittorrent **with Anna's Archive** so a book can be pulled via **three routes**:
  (1) **API** = `fast_download.json` (membership key, already implemented); (2) **regular download** =
  the existing mirror `ads.php`→`get.php` / direct URL route; (3) **torrent** = via qBittorrent, per the
  AA torrents page (https://annas-archive.gl/torrents).
- **R18** — **Automatic import from qBittorrent**: completed torrents in the Shelf category are auto-
  verified and imported into the library (reusing the existing verify→promote→import pipeline).

## 8. Acquisition order + malware scanning
- **R19** — Title searches must try **torrents FIRST (exhaustively)** → then **usenet** → then **Anna's
  Archive**, and this order must be **configurable on the Acquisition page**. (AA-torrent is scrapped —
  AA torrents don't support individual books.)
- **R20** — Add a **VirusTotal integration** (API). **Hash every file grabbed from torrent** (SHA-256)
  and check it against VirusTotal's database. If a file is **not clean**, it is **removed**, and a
  **notification + log event** are surfaced.
- **R21** — The admin can **optionally cap torrent fetches to VirusTotal's API rate limit** (so each
  grabbed file stays within the VT quota) — a toggle, off by default.

## 9. Matching correctness for torrents + verification protocol
- **R22** — **Torrent grabs MUST use the same correct matching logic as usenet/AA** (the full
  `release_matcher` confidence/gate stack + post-download `verify`). Torrent names are the messiest
  source (scene groups, packs, bundles), so matching precision here is critical and must be proven.
- **R23** — Verify torrent matching **extensively**: a test that **fetches 100 random titles, 3 times**,
  and records **how many matched torrent grabs were CORRECT vs NOT correct** (precision over the
  repeated runs). This is an acceptance gate for the torrent batches.
- **R24** — **During implementation, run verification between EACH stage** to confirm the stage is
  *truly* complete and introduced **no regressions** — **performed by independent sub-agents** (not
  self-attested).

---

# Surgical Plan (review complete — awaiting approval; nothing implemented)

Persistence fact that de-risks the whole Settings reorg: there is **no monolithic settings store**.
System knobs are a **partial-merge** `PUT /settings/system` (`config_store.EDITABLE`), so a card can PUT
only the keys it owns. **Moving knobs between tabs is pure frontend** — the keys stay valid wherever
edited. Three cards already do this (`MissingRecheckCard`, `AutoBackupSection`, `RegistrationModeCard`).

## Batch A — Quick frontend removals/renames (low risk, no backend)
- **R7** Rename tab label `Settings.tsx:1259` "Appearance & layout" → **"Catalog Layout"**.
- **R8** Reader dropdown: remove `DefaultShelfSelect` usage `Reader.tsx:445-447` + the component `602-628`.
  (`api.setWorkDefaultShelf` becomes UI-unused; leave the API.)
- **R9** Jobs: remove the `reap` mutation `Jobs.tsx:45-58`, the button `64-72`, the now-orphaned
  `const qc` `:20`, and trim the prose `:74-78`. (Backend reaper still runs on its timer — no API change.)
- **R13** `AddWork.tsx`: rename button "Hook & backfill" `:230` → **"grab title"**; "Index" `:239` →
  **"crawl & index"**; align the `InfoHint` copy `242-255`.
- **R14** Remove `BrokenCleanupCard` from the Indexing tab render (`Settings.tsx:1294`) + its def `199-246`.
- **R12** Import-title format hints already exist (`AddWork.tsx:13-20 REF_HINTS`, shown at `:165/:168`).
  Plan: ensure every surfaced source has a hint and the hint shows per selected source (verify/extend,
  not new infra).

## Batch B — Settings reorg (frontend moves; system-config partial-PUT)
- **R3 Remove the System tab** (`Settings.tsx:1299`) and redistribute `SystemSettings.tsx GROUPS`:
  - **R3a login** (`login_max_attempts`,`login_window_seconds`,`min_password_length`) → new card on
    **Users** page (`Users.tsx`, mirror `RegistrationModeCard`'s `putSystemConfig` pattern).
  - **R3b crawl defaults** (`index_max_pages/_max_depth/_stop_after_idle_pages/_max_pending_frontier`)
    → **Indexing** tab card.
  - **R3d image-cache cap** (`imgcache_max_mb`) → **Storage** (new small card in `StorageSettings.tsx`
    calling `putSystemConfig` — note Storage's own form uses `putStorage`; this card stays separate).
  - **R3e comix crawler** (`comix_browser_enabled/_pages_per_tick`,`solver_chrome_path`) → **Indexing**.
  - **R3f Cloudflare solver** (`flaresolverr_url/_timeout_s/_clearance_ttl_s`) → **Integrations**.
  - **R3c backups**: the System "Automatic backups" group is a **duplicate** of `AutoBackupSection`
    already in the Backup tab → just drop it from System (no move needed).
  - **Loose ends:** `log_level` and `registration_mode` (already in Users). Proposed home for
    `log_level`: a tiny "Logging" card on the **Users** (admin) page or Backups. **(Decision Q below.)**
- **R6** Move `GlobalSmtpCard` (`Settings.tsx:308-392`) from the Integrations tab render → into
  `NotificationsPanel` (`Settings.tsx:860-869`, admin-gated). Same `/settings/smtp` API — pure move.
  (Backend `kindle.app_smtp` stays the shared SMTP resolver for Kindle delivery — unchanged.)
- **R5** Remove the **Goodreads tab** (`Settings.tsx:1266`); render `GoodreadsCard` on the
  **Integrations** tab. Wrinkle: the Integrations tab is `admin:true` but Goodreads is **per-user** →
  need to keep Goodreads reachable by non-admins (un-gate just that card, or a small per-user
  sub-section). **(Decision Q below.)**
- **R15** Move `BlocklistCard` ("Blocked content", `Settings.tsx:162-197`) out of Indexing →
  **Acquisition** tab render (`:1267-1272`). Pure move (same `/index/blocks` API).
- **R4 Remove the Automation tab** (`Settings.tsx:1300`). Its `QueuedHooksCard` data (works waiting on
  a hook) is relocated per Batch E (surface in Missing). Until E lands, do not remove the tab.

## Batch C — Sources cleanup (R10/R11)
- The **"Sources" management tab** (`Sources.tsx` via `/sources`) lists every adapter. Filter it to the
  **working network sources only**: keep **Project Gutenberg**, **Standard Ebooks**; remove from this
  view: **In-memory demo** (`memory`), **Local folder** (`local_folder` — already its own "Watched
  folders" tab), **Local import** (`local_import` — already the "Import files" tab), **J-Novel**
  (`jnovel`, paid/gated). The Add-a-title grid already hides `local_*`/`web_index` (`AddWork.tsx:24`).
- Make working sources the defaults (Gutenberg + Standard Ebooks are already `enabled` +
  `tos_permitted_default=True`). **"x" is unidentified — see Decision Q.** Comix.to / Generic-feed /
  Royal Road disposition also needs confirming (not in the user's remove-list).

## Batch D — Backend: libgen integration → Anna's Archive only (R1, R2)
- Keep integration **`kind="libgen"`** (renaming is a data migration — avoid). Change behavior + labels.
- `libgen.py`: `DEFAULT_PROVIDERS`/`ALL_PROVIDERS` → `["annas"]`; `_PROVIDERS` → `{"annas": _annas_search}`;
  `_FALLBACK_PROVIDERS` → `set()`. Remove `_zlibrary_search`/`_oceanofpdf_search`/`_liber3_search` +
  `zlib_user`/`zlib_pass` from Config/load_config. **KEEP** `_libgen_query`,`_parse_size`,
  `_libgen_get_url`,`DEFAULT_LIBGEN_HOSTS`,`libgen_hosts` — Anna's MD5s still download via the libgen
  mirror ads→get route (free path) before the AA fast-download fallback.
- `integrations.py`: drop `zlib_pass` from `_SECRET_CFG_KEYS`; add the AA secret/password keys.
  `provider_catalog.py:131-142`: relabel the entry to Anna's-Archive-only.
- **R2 AA credentials UI:** in the Open Libraries config form (`IntegrationsManager.tsx`) surface
  the **membership secret key** (`annas_key`, already added — the *apijson* credential for
  `fast_download.json`) prominently. Per the AA FAQ the **only API auth is this secret key**; there is
  no separate member-login API. Optional: an account email/password to enable the **free slow_download**
  path (raises rate limits) — but that needs a DDoS-Guard solver to be useful. **(Decision Q below.)**
- Update `test_libgen.py` (`_cfg` drops zlib_*; remove the zlib/ocean fallback test).

## Batch E — Goodreads "waiting on hook" → Missing tab with a tag (R4 data)
- Chosen approach: **read-time union (no schema change).** `list_missing` (`missing.py:43`) also surfaces
  `QueuedHook` rows with `reason="goodreads"`, `status="pending"` (`metadata.py:168` is the existing
  endpoint) as virtual Missing entries tagged `goodreads`. Add a `tag`/`origin` field to
  `MissingRequestOut` (synthesized, not stored) and a frontend badge + filter on the Missing page.
- Then remove the Automation tab (Batch B/R4) since its content now lives in Missing.

## Batch F — qBittorrent integration + torrent route for AA (R16, R17, R18)

**Feasibility finding (must read first).** `https://annas-archive.gl/torrents` confirms AA torrents are
**bulk preservation torrents**, "*not meant for downloading individual books*" — terabyte-scale dataset
collections (zlib = 480 torrents / 83.7 TB), in the AAC container format, listed at `/dyn/torrents.json`.
There is **no per-MD5 torrent locator**. So a per-book AA-torrent download means: locate the md5's
containing torrent + file from AA bulk metadata, add that (huge) torrent to qBittorrent, and use
BitTorrent **selective file download** (qBit `filePrio`) to fetch only the one file. That is heavy,
slow, often packs many records per AAC file, and AA discourages it. **The reliable per-book torrent
path is a torrent *indexer* (Prowlarr → magnet/.torrent) → qBittorrent**, not AA's bulk torrents. See
the decision at the end of this batch.

### F1 — qBittorrent client + integration (mirror SABnzbd) — **R16**
New `app/integrations/qbittorrent.py`, `QBittorrentClient(BaseClient)`, modeled on `sabnzbd.py`:
- **Auth:** `POST /api/v2/auth/login` (form `username`,`password`) → `SID` cookie kept on the client
  (verified live: returns `200` + `Set-Cookie: SID=…`). Store **username in `config`**, **password in
  the `api_key` column** (the existing never-returned secret slot — no new redaction needed).
- **Add:** `POST /api/v2/torrents/add` (`urls`=magnet or `.torrent` URL, `category`, `savepath`,
  `paused=true` first for selective download); qBit doesn't return the hash, so compute it from the
  magnet/torrent or read it back from `/api/v2/torrents/info?category=…` right after.
- **Selective download:** `GET /torrents/files?hash=` → choose the target file index, then
  `POST /torrents/filePrio` (set all to 0 except the target), then `/torrents/resume`.
- **Status:** `GET /api/v2/torrents/info?category=shelf` → `state`,`progress`,`content_path`,`save_path`.
- **Delete/cleanup:** `POST /api/v2/torrents/delete?deleteFiles=` (mirror SAB `delete_history`).
- **test_connection:** login + `GET /api/v2/app/version` (live: `v5.1.4`).
- **Wiring touch-points (exactly like SAB):** add `qbittorrent` to the `IntegrationIn.kind` regex
  (`schemas.py:526`); `PIPELINE_KINDS` + `client_for` (`base.py:135,142`); a `provider_catalog.py`
  entry (category `pipeline`, auth `key`+username note); add/test/sync branches in
  `routers/integrations.py`. Config defaults seed `base_url=http://10.10.102.28:8090`, `category=shelf`.

### F2 — Torrent grab + auto-import worker (mirror SAB reconcile) — **R18**
- New `grab_kind="torrent"` (fits the 8-char column); the qBittorrent **torrent hash → `DownloadJob.nzo_id`**,
  qBit `content_path` → `storage_path`. **No schema change** (the model already tracks SAB + libgen jobs).
- New scheduler tick `torrent_poll_tick` (parallel to `download_poll_tick`/`libgen_tick`,
  `scheduler.py:1561/1576`): poll `torrents/info?category=shelf`, and for each completed torrent map
  `content_path`→local (`downloads.map_path`/`_job_dir`, fully reusable) and run the **existing**
  verify→promote→import→link→notify→ledger flow (`downloads._import_completed` logic, parametrized off
  the SAB client; or libgen's `_import_file`). Apply the seed/keep-after-import policy, then optionally
  `torrents/delete`. `poll_tick` already excludes `grab_kind=="libgen"`; likewise exclude `"torrent"`.

### F3 — Torrent acquisition via Prowlarr torrent indexers → qBittorrent — **R17 (RESOLVED: scope b)**
**Operator decision: Prowlarr torrent-indexer only.** AA's bulk torrents are NOT used for on-demand
grabs (the F4 AA-torrent locator is dropped). Instead the qBittorrent client (F1) is wired as the
**torrent download backend of the acquisition pipeline**, the standard *arr-stack pattern:
- Extend the pipeline so Prowlarr can search **torrent** indexers, not just usenet: today
  `release_matcher`/`search_prefs` force `protocols=["usenet"]` — add `torrent` (config-gated).
- When a grabbed release is a torrent (magnet/`.torrent`), route it to **qBittorrent** instead of
  SABnzbd: a torrent `DownloadJob` (`grab_kind="torrent"`, hash→`nzo_id`) enqueued via the qBit client,
  finished by `torrent_poll_tick` (F2) — verify→import reused. SAB still handles usenet releases.
- **AA itself keeps two routes** — API `fast_download` (key) + regular mirror/direct (both implemented);
  AA's bulk torrents are not an on-demand source. So "three routes" = API + regular for AA, **plus a
  torrent route for the pipeline at large** (per-book torrents from indexers), which is the practical,
  reliable torrent capability.
- Plumbing: `acquire.py` `pipeline` route already dispatches Prowlarr→SAB; add a torrent branch (or a
  sibling `torrent` route) that hands torrent releases to qBittorrent. `available_routes` gains a
  `torrent`/qBit gate. Stock (`stock.py`) can use the torrent backend the same way it uses usenet.

### F3b — Acquisition ORDER: torrent → usenet → AA, configurable — **R19**
- Add a **`torrent` route** to `acquire.py` `ROUTES`/`DEFAULT_PRIORITY` (`acquire.py:20,23`) and make it
  the **first** entry; `pipeline` (usenet) second; `libgen` (AA) third:
  `DEFAULT_PRIORITY = ["torrent","pipeline","libgen","web_index","readarr","kapowarr"]`. The cascade
  already exhausts a route's candidates before the next, satisfying "torrents first **exhaustively**."
- **Configurable on the Acquisition page:** the existing `FetchPriorityCard` (`Settings.tsx:649-727`)
  reorders these routes from `ROUTE_LABELS` (`Settings.tsx:641`). Add labels:
  `torrent:"Torrent (Prowlarr → qBittorrent)"`, relabel `pipeline:"Usenet (Prowlarr → SABnzbd)"`,
  `libgen:"Anna's Archive"`. Per-user + global-default save already exist — no new API.

*(F4 — AA bulk-torrent per-file locator — dropped per the operator decision: heavy/slow/AA-discouraged,
and AA torrents don't support individual books.)*

### F-MATCH — Torrent matching rigor (CRITICAL) — **R22**
Torrent release names are the **messiest** of the three sources (scene groups `-RARBG`, packs/bundles,
season ranges, mislabeled formats), so the torrent route must run the **exact same matching stack** as
usenet/AA — no shortcuts:
- **Pre-grab:** score every torrent release through `release_matcher.score_release` / `rank_releases`
  (`title_author_confidence` with fuzzy author + ISBN + alt-titles; the junk/boxset/companion/volume/
  format/language gates; `auto_grab_min_confidence` floor). Torrent-specific care: `is_boxset`/pack
  rejection for single-title requests (torrent "packs" are common), and the seeders/retail rank bonuses
  to prefer healthy, clean releases. **Do not** let a `.torrent`/magnet name alone authorize an import.
- **Post-download (the real gate):** `verify.verify_download` on the actual file's **embedded metadata
  + ISBN** (not the torrent name), then **VirusTotal** (Batch G) — only a file that passes BOTH enters
  the library. A multi-file torrent (pack) uses `verify.match_titles` to map files→requested titles.
- This reuse is automatic if the torrent route flows through `release_matcher` + `downloads`/`verify`
  (it does, in the F1–F3 design) — F-MATCH is the explicit requirement that it **stays** that way and is
  proven by R23's accuracy test (see Verification & quality gates).

## Batch G — VirusTotal malware scanning of torrent files (R20, R21)

Torrents are untrusted, so every file pulled via the **torrent route** is hash-checked against
VirusTotal before it can enter the library.

### G1 — VirusTotal integration (API) — **R20**
- New integration **kind `virustotal`** (category `security`), `api_key` = VT API key (secret). Client
  `VirusTotalClient(BaseClient)`: lookup `GET https://www.virustotal.com/api/v3/files/{sha256}` with
  header `x-apikey`; read `data.attributes.last_analysis_stats` → `{malicious, suspicious, harmless,
  undetected}`. **404 = unknown (not in DB).** `test_connection` = a cheap quota/whoami call
  (`GET /api/v3/users/{key}` or a known-hash lookup) returning the daily/min quota.
- Wiring (same touch-points as other integrations): `IntegrationIn.kind` regex (`schemas.py:526`),
  `client_for` (`base.py:142`), a `provider_catalog.py` entry, add/test/sync branches in
  `routers/integrations.py`. Surfaces as a card on the **Integrations** page.

### G2 — Hash-and-gate the torrent import path — **R20**
- In the torrent auto-import flow (Batch F2, in `torrent_poll_tick`/the torrent `_import_*`), **after the
  download completes and before promote/import**: SHA-256 each candidate book file, look it up on VT.
  - **Not clean** (`malicious > 0`, or `suspicious >` threshold): **delete the file** + the torrent's
    data (qBit `torrents/delete?deleteFiles=true`), set job `status="failed"`/`error="VirusTotal: N
    engines flagged …"`, mark the release broken, **emit a notification** (new `malware`/`security`
    notification event via the existing notifications system) **and a log event** (`shelf.security`
    logger; optionally a surfaced row).
  - **Clean** (`malicious==0`, suspicious within threshold): proceed to verify→promote→import.
  - **Unknown** (404 — not in VT DB): configurable policy `vt_block_unknown` (default **off** = allow;
    on = hold/skip). We do **not** upload files to VT (privacy + quota) — DB lookup only, per the
    requirement ("check it against its database").
- Scope: **torrent files only** by default (`vt_scan_scope="torrent"`); a config option can widen to all
  downloads. Usenet/AA files are not torrent-sourced, so they're not gated unless the admin opts in.

### G3 — Optional VT rate-limit cap on torrent fetches — **R21**
- Config `vt_cap_torrent_to_limit` (bool, **default off**) + `vt_per_min` (default **4**) / `vt_per_day`
  (default **500**) (VT free-tier limits; editable for paid keys). When **on**, the **torrent grab/scan
  path is throttled** to the VT quota (reuse the `ratelimit`/daily-cap pattern already used for SAB
  grabs and libgen) so every grabbed file can be scanned without exceeding VT. When **off**, torrents
  fetch unthrottled and only as many files as quota allows are scanned that day (rest pass with a
  logged "VT quota exhausted" note, or are held — tied to `vt_block_unknown`).
- Surface the cap toggle + limits on the **Acquisition** page (it governs torrent acquisition
  throughput), reading the VT integration's config. VT credentials stay on the Integrations card.

## Verification & quality gates (R23, R24)

### V1 — Torrent matching accuracy test: 100 random titles × 3 — **R23** (acceptance gate for F/G)
A harness `scripts/torrent_match_verify.py` that, per run, picks **100 random catalog titles** and for
each runs the **full torrent acquisition end-to-end** — match (`release_matcher` over torrent-protocol
Prowlarr results) → grab → qBittorrent download → VirusTotal scan → `verify.verify_download` — then
classifies the outcome:
- **CORRECT** — verify confirms the imported file is the requested book (title/author/ISBN match);
- **INCORRECT** — a file was grabbed/imported but is the wrong book (verify mismatch slipped through, or
  a manual spot-check disagrees);
- **NO-RESULT** — no torrent candidate cleared the matcher (not a precision failure).
Run the whole thing **3 times** and report, per run and aggregated: counts + **precision =
CORRECT / (CORRECT + INCORRECT)**, the INCORRECT cases (title, release name, why), and **cross-run
stability** (same titles → same verdicts?). **Acceptance bar: precision ≥ 90%, zero INCORRECT imports
that VirusTotal+verify should have caught.** Practicalities: scope to a dedicated qBit category, cap
file sizes, **delete downloads + torrents after each title** (no library pollution), honor the VT cap;
the run is resource-heavy (≈300 grabs) so it's an explicit acceptance step, not CI. INCORRECT cases feed
matcher tuning before sign-off.

### V2 — Per-stage independent verification via sub-agents — **R24**
After **every** batch (A…G) is coded + its own tests/build pass, a **fresh, independent sub-agent**
(reality-check / `karen`-style — NOT the implementing agent, no shared context) verifies the stage
before the next begins. Each verifier:
1. Re-reads that batch's requirement IDs from this doc and checks **each is actually met** (reads the
   diff, runs the app, exercises the changed surface — not self-attestation).
2. Independently runs the **full backend `pytest`** + **`tsc + vite build`**, and a **live smoke test**
   of the changed pages/endpoints on `:8000`.
3. **Regression check:** confirms previously-completed batches still work (suite green, prior surfaces
   intact, no contradicted decisions).
4. Returns a structured **PASS/FAIL with evidence**; **FAIL blocks the next batch** until fixed and
   re-verified. The verifier for **F/G** additionally runs/reviews V1 (the 100x3 accuracy test).
Record each verdict in a running `docs/VERIFICATION_LOG_2026-06-16.md`.

## Suggested order
A (independent) → C → B (do E’s surfacing before removing the Automation tab) → E → D → **F** → **G**.
**Each stage is gated by an independent sub-agent verification (V2); F/G additionally gated by the
100×3 torrent-accuracy test (V1).** Each batch ends `tsc+vite` + `pytest` green, then a deploy, then
the verifier sub-agent signs off before the next batch starts.

## Resolved decisions (operator, 2026-06-16)
1. **AA credentials (R2):** **Secret key only** — surface the membership `annas_key` (apijson) in the
   integration form; no account email/password, no free slow-download path. Make the field prominent
   and the integration Anna's-Archive-only.
2. **Sources (R10/R11):** **Keep** Gutenberg, Standard Ebooks, **Comix.to**, **Generic feed/web**,
   **Royal Road** in the Sources tab. **Remove** In-memory demo (`memory`), Local folder, Local import,
   **J-Novel** (`jnovel`). Royal Road is `enabled=False` today → surface it as a gated/attestable
   source. (This also resolves "x" — it's in the removed set.)
3. **Goodreads (R5):** **Un-gate the Goodreads card** on the Integrations tab so all users can connect
   their shelf; the rest of Integrations stays admin-only.
4. **`log_level` (R3):** goes to the **Backups** tab (admin).

> Implementation has NOT started. Awaiting the operator's go-ahead to build, batch-by-batch
> (A → C → B → E → D), each batch tested (tsc+vite, pytest) and deployed before the next.
