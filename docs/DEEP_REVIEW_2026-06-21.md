# Shelf deep review — 2026-06-21 (multi-role, verified)

# Shelf — Consolidated Deep Review (Lead Reviewer)

*All findings below survived adversarial verification (verdict.isReal !== false). Refuted/speculative items dropped. Fixes are specified ponytail-minimal: the smallest change that closes the defect, no refactors.*

---

## 1. Executive Summary

**Top risks.** Two unbounded/unrecoverable state bugs (`SourceAttempt` grows forever; a NULL-date `planned` row is gated forever) and an admin "Recheck" that silently skips stuck sources are the highest-impact correctness defects. Security is all P2: a non-admin blind-SSRF primitive via Apprise URLs, a forgot-password lockout oracle, a login existence/validity oracle, and two minor cache/hardening gaps. Reader formatting has four P1 text-mangling bugs (word-fusion, CJK non-splitting, false de-censoring, false headings).

**The duplicate story (the operator's #1 concern).** Duplicates leak in two stages. In **search/browse grouping**, the exact-key union merges different works that share a title because it skips the author check the contract requires (DUP-1), while subtitle/"Book N" variants of the *same* work split into separate cards because `norm_title` and the fuzzy branch both bail on single-token titles (DUP-2); meanwhile book-provider rows carry ISBNs but never set `identity_key`, so the one deterministic merge key is unused for the entire book side (MERGE-2). In **series fetching**, the series cache keys on the *book* title not the *series* identity so each member re-enumerates and mints divergently-named rows (S-DUP-2); owned-volume detection misses when the acquired Work's norm_key drifted, so already-owned books are re-fetched forever (S-DUP-3); a `ContextVar` read across `asyncio.gather` silently caches partial rosters that later resurface as "new" (S-DUP-4); and `follow_tick` retires keys that were never actually fetched (S-DUP-1). The through-line: **Shelf merges on normalized *titles* where every mature analogue merges on *identifiers* first** — and has no canonical series id, so a free-text `extra.series` string collides.

---

## 2. ★ PRIORITY: Metadata Merging, Series Indexing & Duplicate Avoidance ★

This is the deepest section by request. Findings are grouped **search/browse dedup** then **series-fetch dedup**, each woven with the specific cross-app method to adopt.

### 2A. Search & browse grouping (the card you see)

#### DUP-1 (P1) — Exact-key union ignores authors → different works merge into one card
**`backend/app/ingestion/catalog.py:398-408`**
`_union_find_groups` buckets rows by `(norm_key, media)` and unions with **no author check** (lines 404-406), contradicting the documented contract `titles_match()` (`extract.py:1007-1008`) which gates exact-title equality on `authors_compatible()`. Reproduced: *Twilight*/Stephenie Meyer and *Twilight*/William Gay (both `norm_key='twilight'`) merge into one group, yet `titles_match(...)` returns `False`. Affects both the live search path (`group_rows`, `catalog.py:755`) and persisted browse (`regroup` → `catalog_groups.py:78`). The fuzzy branch at line 448 *already* gates on author compatibility — the exact branch just doesn't.
**Ponytail fix:** mirror line 448. Before `union(i, by_key[bk])`, compute `ai, aj = atoks[i], atoks[by_key[bk]]` (already precomputed at line 378) and skip the union when `ai and aj and not (ai & aj)`. One condition.

#### DUP-2 (P1) — Subtitle / "Book N" variants split one work into duplicate cards
**`backend/app/ingestion/extract.py:871-918` (`norm_title`) + `catalog.py:416-456` (fuzzy block)**
`norm_title` strips volume markers only before a *digit* and never strips trailing subtitles, so `'Dune'→'dune'` but `'Dune: Book One'→'dune book one'` ("one" is a word, not a digit). Because `'dune'` is single-token, the fuzzy branch never considers it — `catalog.py:420` requires ≥2 tokens to enter `postings`, line 425 skips <2-token rows from `rare_buckets`, and `titles_match` bails at `extract.py:1016` on <2-token titles. Same work → two cards in search *and* browse. (`metadata_sync._confidence:87` already handles the strict-subset case for *provider* matching — the catalog union-find has no equivalent.)
**Ponytail fix:** in the fuzzy block, when one row's token-set is a strict subset of another of the **same author and bucket**, union them (mirror metadata_sync's `ta < tb or tb < ta` subset rule). Single-token rows must be added to `postings`/`rare_buckets` (today excluded at 420/425). Keep author-gated to avoid over-merge.

#### MERGE-2 (P1) — Book rows carry ISBNs but never set `identity_key` → only fuzzy-title dedup
**`backend/app/ingestion/book_catalog.py:430-498` (`upsert_hit`) + `catalog_enrichment.py:431-454` (`_enrich_openlibrary`)**
`identity_key` is the deterministic cross-source merge key (its own docstring at `models.py:433-438` lists `isbn:9780…` as canonical) but it's written in exactly **one** place — `_persist_identity` (`catalog_enrichment.py:268`), reached only from the AniList/RanobeDB path. `upsert_hit` stores the ISBN into `extra['isbn']` (`book_catalog.py:475-476`) but never sets `identity_key`; `_enrich_openlibrary` never calls `_persist_identity`. So the same physical book from Google Books + OpenLibrary + Hardcover (identical ISBN) has `identity_key=NULL` on all three and can only merge by fuzzy title — which DUP-2 shows fails. The identity bucket (`catalog.py:385-394`) merges only rows that *have* a key.
**Ponytail fix:** in `upsert_hit`, when `hit.isbn` is present and `identity_key` is unset: `entry.identity_key = f'isbn:{normalized_isbn13}'[:64]`. ~2 lines; first-id-wins guard prevents churn.

#### MERGE-1 (P1) — Hooked-work enrichment is last-writer-wins → broad provider clobbers specialist
**`backend/app/integrations/metadata_sync.py:190-201` + `430-455`**
`enrich_work` unconditionally **overwrites** `work.author`/`description`/`cover_url` whenever a provider returns non-empty (lines 194-201), and `enrich_work_all_providers` iterates `select(Integration).where(enabled)` with **no `.order_by`** (line 438) — so the last provider by Integration scan-id wins every display field. Google Books (in `_AUTHOR_REQUIRED_PROVIDERS` at `:111` yet still frequently matching) can overwrite RanobeDB/AniList specialist data for a light novel. The catalog-discovery path `_enrich_provider` does this **correctly** (concurrent gather, longest-synopsis wins — `catalog_enrichment.py:363`); the two paths are inconsistent.
**Ponytail fix:** sort the integration loop by a small priority map (ranobedb/anilist before googlebooks/hardcover) so specialists run last and last-writer favors them; and/or make `enrich_work` upgrade-not-clobber for synopsis (take the longer), fill author/cover only when empty — reusing the precedence the catalog path already encodes.

#### MERGE-3 (P2) — Provider-prefixed identity keys never reconcile
**`backend/app/ingestion/catalog_enrichment.py:259-273` + `catalog.py:385-394`**
`identity_key = f'{provider_kind}:{ref}'` (first-id-wins) and the union-find buckets on the full prefixed string. So one work matched `anilist:123` and another row of the *same* work matched `ranobedb:456` are distinct buckets and never merge by identity — defeating identity_key's purpose whenever two rows were enriched by different providers (common when one provider is transient at `:311`). Falls back to title matching, which *usually* still merges → P2.
**Ponytail fix:** record a single canonical identity per work across all hits in a pass (`_enrich_provider` already gathers all hits at `:312` — pick deterministically, e.g. AniList id if present else first), or prefer a provider-agnostic id (ISBN) consistently.

#### DUP-3 (P2) — Search and browse use two divergent grouping engines; series-collapse on only one
**`backend/app/routers/index.py:601-634` (live) vs `731-825` (persisted) ; `catalog.py:915-947` (`collapse_series_cards`)**
The `/catalog` (search) path re-runs live union-find and applies `collapse_series_cards` (`index.py:634`) **only when there is no query**. The persisted-group serializer `_serialize_groups` — used by categories/browse/rows (`index.py:902, 925, 1011`) — never calls it, and `regroup`'s `_build_groups` (`catalog_groups.py:69`) has no series collapse. So one series renders as **one card** on live browse but **N per-volume cards** on the persisted endpoints. The live path also groups only within `find_rows`' candidate window (2000 browse / search limit, `index.py:607-608`) vs the full-catalog persisted regroup, so rep/source selection can differ.
**Ponytail fix:** pick one authority. Cheapest: have the live no-query browse read the same persisted `CatalogGroup` rows the other endpoints use, leaving `group_rows` only for the query path. No model change.

### 2B. Series fetching (the auto-fetch loop)

#### S-DUP-2 (P1) — `detect_series` caches by TITLE-norm, not series identity → re-enumerates and mints divergently-named rows
**`backend/app/ingestion/series.py:350-356, 448-453, 519-552`**
The in-memory cache and the 14-day persisted enumeration key on `ckey = norm_title(stored or cw.title)` (the **book's** title, `:350`). Vol-1 and vol-2 of one series → two ckeys → two cross-API enumerations → two `_persist_series` runs. Worse, the resolved series `name` is provider-dependent: `name = hc_name` (`:368`) when a Hardcover token exists vs `_series_name_for` (`:372`) otherwise (e.g. "Spellmonger" vs "The Spellmonger Series"). `_apply_series_rows` scopes a refless volume's synthetic URL by `skey = norm_title(name)` (`:541-543`) → two names → two distinct listing rows for the same volume, each with a different `extra['series']`. `_series_rep_for` (`scheduler.py:1466-1472`) then flips roster representation across ticks.
**Ponytail fix:** once `name` is resolved, re-key the cache/persist on the **series** identity: `ckey = norm_title(name)` (keep the title-key only for the negative-cache case). Vol-1/vol-2 then share one cache entry and one enumeration.

#### S-DUP-3 (P1) — owned-skip misses when the acquired Work's norm_key drifted → already-owned volume re-fetched every tick
**`backend/app/ingestion/series.py:572-591` (`_annotate`), `690-692`; `scheduler.py:1545`**
`_annotate` resolves ownership via `_best_row_for(nk)` → `row.hooked_work_id` (`:579-582`, keyed on the *provider* title's norm_key) plus a fallback that fires only `if hooked is None and name and b.get('position') is not None` (`:584`). That fallback is **inert** when position is None (GB sets pos only from numeric `bookDisplayNumber`, `:166-167`) and for author subs entirely (`enumerate_author` calls `_annotate(db, None, books_raw)` → name=None, `:640`). When the owned Work's catalog row norm_key drifted from the canonical title (the disjoint-title case this codebase explicitly fights), both probes miss → `hooked_work_id` stays None → re-acquired. The acquire-time grab/libgen dedup is keyed on the *same drifted cluster* (`downloads.py:481-483`, `libgen.py:1028-1030`) so it also fails to collapse.
**Ponytail fix:** add a third ownership probe in `_annotate` — match an owned Work by `norm_title(title)` of the volume against `Work.norm_key`/the hooked cluster, and for author subs a direct `Work` lookup by `norm_title(title)`.

#### S-DUP-4 (P2) — `_series_transient` ContextVar read across `asyncio.gather` → partial roster cached 14 days → recovered volumes resurface as "new"
**`backend/app/ingestion/series.py:398-403, 48-49, 347`**
The supplemental enumeration runs inside `asyncio.gather(_olf(), _ola(), _gb(), …)` (`:399-400`). `gather` wraps each coroutine in a Task that runs in a **copy** of the context, so a `_mark_transient()` (set True) inside `_ol_query` (`:96`) or `_gb_author_volumes` (`:153`) mutates the child context and is invisible to the parent's `_series_transient.get()` after the gather (**empirically reproduced**). The code comment at `:45-47` even notes the guarantee only holds with "no gather between." Result: a transient 5xx that truncates the roster isn't detected → partial list cached in-memory (`:448`) and persisted 14 days (`:453`); recovered volumes later diff as **new** and auto-fetch as duplicates.
**Ponytail fix:** have the three supplemental coroutines **return** their transient status instead of relying on the ContextVar; OR those into a local `transient` before deciding to cache.

#### S-DUP-1 (P1) — `follow_tick` retires non-fetched keys and inflates `auto_added`
**`backend/app/ingestion/scheduler.py:1543-1563`**
The per-key loop adds `k` to `processed_new` (`:1543`), calls `await acquire(...)` then `added += 1` (`:1553-1554`) with **no check of acquire's status**, and advances the baseline to `current - overflow` (`:1563`) — so every *processed* key is retired from `known_keys` whether or not a fetch started. `acquire` genuinely returns non-fetch statuses: `gated` (`acquire.py:219`), `planned` (`:215`), `none` (`:409`). A gated/no-match key is permanently retired and never retried; `auto_added` is inflated by non-fetches. *(The elaborate cross-sub narrative in the original title is partly speculative and dropped — the substantiated bug is retire-on-non-fetch.)*
**Ponytail fix:** capture `res = await acquire(...)` and only `processed_new.add(k)` / `added += 1` when `res.get('status') in ('downloading','grabbed','hooked')`; leave gated/none keys out so they stay "new" and retry next tick.

#### S-DUP-5 (P2) — `acquire_series` has no in-flight gate; cluster-drift escapes the per-user download dedup
**`backend/app/ingestion/series.py:690-714, 659-670`**
`acquire_series`'s only duplicate defense is the per-`(norm_key cluster, user_id)` check in `grab_release` (`downloads.py:481-501`) / `_active_libgen_job` (`libgen.py:1028-1040`). `_resolve_book_row` (`:659-670`) calls `book_catalog.resolve_live` which can mint/upsert a row in a **different** norm_key cluster (subtitle/edition drift between runs), so a second run or a race with `follow_tick` lands on a cluster the in-flight job isn't in → `downloads.py:482` misses → duplicate grab. `ledger.is_gated` returns False for any status other than unavailable/planned (`:253-264`), so an in-flight `downloading` title is *not* gated. (Conditional on actual cluster drift; `_recently_resolved` at `book_catalog.py:545` dampens it → medium confidence, P2.)
**Ponytail fix:** add a cross-user in-flight gate to `acquire`/`note_request`: if a `DownloadJob` for this cluster is in an active status for **any** user, return `in_flight` and skip.

### 2C. Methods to adopt (woven to the bugs above)

These are the cross-app recommendations that directly retire the duplicate findings — listed in leverage order.

1. **Identifier-first union-find (highest leverage — fixes MERGE-2/MERGE-3, makes DUP-1 safe).** Every mature analogue dedups on identifiers before titles: Calibre disables title/author matching entirely when an ISBN matches; Kavita CBL Tier-1 is ComicVine/Metron id before any name match; Audiobookshelf does ASIN direct lookup; OpenLibrary resolves ISBN→edition→**work key**. Shelf already has the primitive (`identity_key` drives the first title-independent bucket) but populates it only from AniList/comix. **Promote ISBN-13/ASIN/OL-work-id into a *set* of identity keys and union any two rows sharing any one.** GB already returns `industryIdentifiers`; ranobedb returns anilist/mal ids; OL exposes work OLID. This merges cross-language/cross-edition rows `norm_title` provably can't, and makes same-title-different-work safe (different ISBNs never merge).

2. **Canonical series id with positioned members (fixes S-DUP-2, and DUP-3's collapse).** Replace free-text `extra.series` + `extra.series_position` with the **Hardcover series id (or OL series key) Shelf already fetches** in `_hc_series_lookup`, with membership referencing that id + position. LazyLibrarian (series+member tables), Hardcover (`book_series` positions), Kavita (external series ids), and the Audiobookshelf upstream "series need an ASIN/id" request all converge here. Two real series sharing a name then stop colliding, and `collapse_series_cards`/`View Series`/`auto_request_series` dedup on a stable id.

3. **Explicit provider precedence + field-level merge (fixes MERGE-1).** Adopt komf/ABS model: integer provider priority, base from the first positive match, each lower provider fills **only** fields the higher left empty, with per-provider per-field toggles ("AniList may set status+cover but not title"). Replace `enrich_work`'s ad-hoc field rules with a small declarative precedence table so one source can never silently mask another (the ABS CONTENTGROUP bug).

4. **Tiered match ladder with confidence cutoffs (reduces wrong series fetches behind S-DUP-3).** Restructure `_series_name_for`/`_consider` into ordered rungs (identifier → exact norm name+author → subset+author → fuzzy+author) recording which rung fired, mirroring Kavita's 7-tier CBL ladder and Calibre's 4×5 grid, so a low-confidence rung is gated behind a configurable cutoff.

5. **Persistent "merge" + "not-a-duplicate" override table (safety valve over the regroup DELETE+INSERT).** Add a small override table consulted by `_union_find_groups` as the first and a final pass: force-merge (shared identity_key) and force-split/exempt (negative pair), surviving regroup. Mirrors Calibre's `partition_using_exemptions` and Kavita Tier-0 remap. This is the operator override the brief asks for and a guard against the catalog's known over-merge/flicker history.

6. **Harden `norm_title` with Calibre "Similar" normalization, reused for release-name matching.** Add an optional aggressive tier (drop trailing subtitle / anything after and/or/aka/colon) used only as a low-confidence rung, and run the *same* normalizer over usenet/libgen release names before matching (LazyLibrarian applies `cleanName` to both sides). Directly supports the DUP-2 subset rule.

---

## 3. Security Findings (ranked)

All P2 — no active RCE/XSS. Ranked by exploitability.

#### SSRF-1 (P2) — Per-user Apprise URL is a blind-SSRF primitive
**`backend/app/notify.py:18-42`; `routers/settings.py:132-133`; `notifications.py:161-162`**
**Attack:** any logged-in non-admin stores a raw Apprise URL (`settings.py:133` saves it verbatim; `notifications.py:161-162` returns `cfg['url']` unchanged). `_target_allowed` is an incomplete denylist — `_DENY_SCHEMES` blocks json/xml/form/file, `_HOST_VALIDATED_SCHEMES` host-checks only ntfy/matrix/mqtt, **every other scheme falls through to `True`**. Even validated schemes have a TOCTOU/DNS-rebinding gap: `is_public_url` validates the *name*, then `ap.add(url)/ap.notify` re-resolves and connects with **no IP pinning** (unlike `netguard.safe_get`). Drives blind outbound HTTP(S)/MQTT/Matrix to RFC-1918 panels or `169.254.169.254`.
**Ponytail fix:** in `_target_allowed`, resolve+pin like `netguard.assert_public_url` for any user-supplied host and reject non-public; treat unknown schemes as **deny**; add remaining host-bearing schemes (rsyslog/xmpp/smtp(s)) to `_HOST_VALIDATED_SCHEMES`.

#### DOS-1 (P2) — Unauthenticated forgot-password lets an attacker lock out a victim's reset flow
**`backend/app/routers/auth.py:303-313`**
**Attack:** `forgot_password` calls `record_login_failure("forgot:ip", "forgot:<attacker-supplied identifier>")` unconditionally before any matching (`:312-313`). N POSTs with a victim's email fill that bucket to `login_max_attempts`, 429-ing the victim's own reset requests for `login_window_seconds` (default 900s). Separate namespace means login itself is unaffected; impact scoped to the reset flow.
**Ponytail fix:** drop the per-identifier `record_login_failure` (keep the per-IP one) so a third party can't poison a victim's bucket.

#### AUTHZ-1 (P2) — Login reveals account existence + password validity for not-yet-approved accounts
**`backend/app/routers/auth.py:177-189`**
**Attack:** the 403 "pending approval" is raised **only after** a successful `verify_password`, distinguishing "valid creds, pending" (403) from "invalid creds" (401) — a credential-validity oracle for gated self-registered accounts. Requires a correct password, so it's a low-value confirmation oracle.
**Ponytail fix:** return the same generic 401 for pending accounts; surface "pending" only via a separate authenticated/self-service path.

#### AUTHZ-2 (P2) — Revoked session serves `/media` and `/covers` for up to 15s
**`backend/app/static_auth.py:25-50`**
**Attack:** `SessionStaticFiles` caches positive token validity for `_TTL_S=15.0` and short-circuits `return True` before any `session_user()` DB check, so a force-logged-out/deactivated/password-reset session can still fetch per-user imagery for ≤15s. Negative results aren't cached. Documented accepted tradeoff; only image bytes the holder already had open → low severity.
**Ponytail fix (optional):** on logout/logout-all/password-change/deactivate, also `static_auth.invalidate(token)` (evict from `_cache`); or lower `_TTL_S`.

#### SEC-1 (P2) — Chapter `<a>` links lack `rel=noopener` (defense-in-depth only)
**`backend/app/sanitize.py:27-30, 84-95`**
No active XSS (dangerous schemes + `target=_blank` are stripped — verified). `<a href>` is kept with no `rel`. Reverse-tabnabbing isn't reachable today since `target` is dropped → pure hardening.
**Ponytail fix:** in `_clean_attrs`, when keeping an http(s) `<a>`, force `rel='noopener noreferrer nofollow'`. One line, low priority.

---

## 4. Bugs / Correctness (ranked)

#### RES-1 (P1) — `SourceAttempt` grows unbounded, never pruned
**`backend/app/ingestion/source_state.py:201-203`; `models.py:919`**
**Trigger:** `record_attempt()` inserts a row per durable search (`acquire._record_source` at `acquire.py:268/272/283`); readers (`source_available_now:225`, `next_source_free_at:237`) only query the last-24h window. **No `delete(SourceAttempt)` exists anywhere.** On a busy instance with a non-empty missing ledger the 30-min retry tick + 60s rescan drain append forever. (`UsenetGrab`/`VtSubmission`, modeled identically, *are* pruned — `downloads.py:842`, `torrents.py:387`.)
**Ponytail fix:** in an existing tick, `db.execute(delete(SourceAttempt).where(SourceAttempt.created_at < _utcnow() - 2*source_state._DAY)); db.commit()`.

#### STATE-1 (P1) — `reset_sources()` skips `matched`/`searching` → admin Recheck silently can't re-search a dead source
**`backend/app/ingestion/source_state.py:183-198`; `routers/missing.py:208`; `rescan.py:74`**
**Trigger:** a torrent grab registers, its job later dies, the row sits `matched`. `reset_sources` only resets `{no_match, exhausted, unavailable, skipped}` (`:193`) — `matched`/`searching` absent. `lease()` only claims `_LEASABLE={pending,unavailable}` (`:97`), `ensure_rows` is a no-op for an existing row, so an admin "Recheck now" (`missing.py:208`, `force=True`) **silently skips** that source; recovery waits on the 30-min reaper.
**Ponytail fix:** add `'matched'` and `'searching'` to the `reset_sources` status filter.

#### STATE-3 (P1) — `is_gated()` returns `(True, None)` for a `planned` row with NULL `release_date` → gated forever, never re-checked
**`backend/app/ingestion/ledger.py:253-258`**
**Trigger:** a `planned` ContentRequest with `release_date=NULL` (migration / partial write / non-standard `mark_planned` caller). `is_gated` returns `(True, None)` (no next-check) and the only un-planning sweep requires `release_date.is_not(None)` (`scheduler.py:1366-1371`), so the row is excluded forever. Normal `mark_planned` always sets a date, so this is a latent unrecoverable gate.
**Ponytail fix:** return `(False, None)` when `rd is None` (treat a dateless planned row as released).

#### STATE-2 (P2) — A resolved import doesn't cancel a sibling in-flight grab → duplicate Work window
**`backend/app/ingestion/source_state.py:128-144`; `ledger.py:241-242`**
**Trigger:** torrent + pipeline both leased/searching the same req; pipeline imports first (`mark_resolved`); `drop_upstream_unavailable` flips only **`unavailable`** rows to skipped (`:135-143`), leaving a sibling `searching`/`matched` grab running, which completes and imports a second copy. Grab-time dedup only fires at grab *start* (`torrents.py:111`). (Window requires two sources concurrently downloading the same cluster → medium confidence, P2.)
**Ponytail fix:** in `drop_upstream_unavailable`/`mark_resolved`, also flip other sources' `searching`/`matched`/`pending` rows to `skipped` and cancel any active non-keep_source `DownloadJob` for the cluster.

#### ERR-1 (P2) — `rescan_drain_tick` calls `db.refresh(row)` after `db.rollback()` outside the try → tick aborts, queue wedges
**`backend/app/ingestion/rescan.py:132-140`**
**Trigger:** an `acquire()` failure that deletes/expunges the row. The try wraps only the `acquire` call; lines 138-140 (`db.refresh(row); row.rescan_queued_at=None; db.commit()`) are **outside** it, so after the rollback `db.refresh` on a gone row raises `ObjectDeletedError`/`InvalidRequestError`, escaping the tick and leaving `rescan_queued_at` set on the rest of the batch (progress strip never completes).
**Ponytail fix:** `row = db.get(ContentRequest, row.id); if row: row.rescan_queued_at = None; db.commit()`.

#### CONC-1 (P2) — Reaper revive is read-then-write, not CAS → a just-renewed healthy backfill gets yanked
**`backend/app/ingestion/scheduler.py:656-670` vs `548-559`**
**Trigger:** a backfill whose lease lands within a reaper tick boundary. The reaper checks `if not _lease_expired(job, now): continue` (`:663`) using the **in-memory** `lease_expires_at` from the tick's initial load (`:643-645`), then overwrites `lease_token` (`:666`) and sets `status='scheduled'` (`:668`) as a plain assignment — not a conditional UPDATE. A concurrent `_renew_lease` (separate session, `:548-559`) isn't detected, so a healthy long run is killed. No corruption (the live runner abandons on its next renew), but the lease's purpose is defeated. (Narrow window → medium confidence.)
**Ponytail fix:** make revival a CAS — `update(CrawlJob).where(id==job.id, lease_expires_at==observed_exp).values(lease_token=new, status='scheduled')`, count revived only if `rowcount==1`.

---

## 5. Reader Content Formatting (ranked)

#### TXT-1 (P1) — Whole-word spans with no whitespace fuse into a run-together blob
**`backend/app/ingestion/textclean.py:110` (`soup.get_text("")`) + `:96`**
**Bad input:** a chapter of `<span>The</span><span>cat</span><span>sat.</span>…` (>40 spans, no inter-span whitespace) → `get_text("")` fuses to `Thecatsat.` and `_paragraphize` emits `<p>Thecatsat.Thedogran…</p>`, bypassing the safety net because a `<p>` *was* produced. (Note: the original 9-span repro string doesn't trip the >40-span `is_garbled` gate at `:53`; the defect is real once the cleaner actually runs.)
**Ponytail fix:** use `soup.get_text(" ")` at line 110; the existing `re.sub(r'[ \t]+',' ',ln)` at `:111` collapses the resulting double-spaces.

#### TXT-2 (P1) — CJK / non-Latin chapters collapse into one giant paragraph
**`backend/app/ingestion/textclean.py:27` (`_SENT_SPLIT`) + `:157`**
**Bad input:** a 45-span chapter ending sentences with `。！？` → `_SENT_SPLIT` (ASCII `[.!?…]` + whitespace + Latin opener only) never fires, so `_paragraphize` emits one enormous `<p>`. Compounded by TXT-1's empty separator. Critical for a manga/CJK-targeted app.
**Ponytail fix:** append `。！？` to the lookbehind class and make the trailing `\s+` optional (CJK has no inter-sentence space): `re.compile(r'(?<=[.!?…。！？])\s*(?=["“A-Z—\[一-鿿ぁ-ヿ])')`.

#### TXT-3 (P1) — De-censor regex mangles real text containing literal `.+`
**`backend/app/ingestion/textclean.py:23` (`_CENSOR_RE`) + `:43`, gate at `:55`**
**Bad input:** `'See e.g. file.+ext and a.b.c.+d'` → `_deobfuscate` returns `'See e.g. fileext and abcd'` (dots/plus stripped); `is_garbled('<p>The regex .+ matches everything.</p>')` returns `True`, force-running pure prose through the pipeline. The line-22 comment claim that `.+` never occurs in real prose is false (regex/glob/path/version text). Narrow real-world prevalence but a genuine corruption.
**Ponytail fix:** tighten `_CENSOR_RE` to the single-letter censorship shape: `re.compile(r'[A-Za-z](?:\.[A-Za-z])+\.\+[A-Za-z]+')` so `file.+ext` no longer matches while `s.h.i.+ro` still does; gate `is_garbled` on `_CENSOR_RE.search`, not a bare `'.+'` substring.

#### TXT-4 (P2) — Title-Case prose scene-openers wrongly promoted to `<h3>`
**`backend/app/ingestion/textclean.py:129` (`_is_heading`, the `len(words)<=3` branch) + `:119`**
**Bad input:** a garbled chapter opening with `'He Was Gone'` / `'The End'` / `'Three Days Later'` → `_is_heading` returns `True` (Title-Case, ≤3 words, no terminal punctuation) and the line is peeled to `<h3>`. Cosmetic; affects only leading lines (peel loop stops at first non-heading).
**Ponytail fix:** drop the loose second `return` at line 134 and rely on the explicit `_HEADING_RE` (Chapter/Part/Prologue) only — or require ALL-CAPS rather than Title-Case.

#### IMG-1 (P2) — Protocol-relative hotlinked image srcs aren't proxied → broken comic images
**`backend/app/routers/imgproxy.py:40` vs `:53`**
**Bad input:** `src="//x.pstatic.net/page.jpg"` → `referer_for` accepts `//host` (`:53`) but `repl` only proxies `http://`/`https://` (`:40`), so the browser fetches it directly without the required Referer and the hotlink-protected CDN returns a broken image. Rare (most pages localized to `/media` at ingest).
**Ponytail fix:** in `repl`, if `url.startswith('//')` normalize to `'https:'+url` before the `referer_for` check. One branch.

---

## 6. Prioritized Fix Backlog

*All fixes to be implemented ponytail-minimal (smallest change that closes the defect; no refactors, no speculative flexibility).*

| Pri | ID | One-line fix | File:line |
|-----|-----|--------------|-----------|
| **P0** | — | *(none — no P0 found; highest-severity confirmed items are P1)* | — |
| **P1** | DUP-1 | Author-gate the exact-key union (mirror fuzzy branch) | `catalog.py:404-406` |
| **P1** | DUP-2 | Subset-union same-author single-token vs subtitle rows; index single-token rows | `extract.py:894`, `catalog.py:420,425` |
| **P1** | MERGE-2 | Set `identity_key='isbn:…'` in `upsert_hit` when ISBN present | `book_catalog.py:475-476` |
| **P1** | MERGE-1 | Order providers by specificity / upgrade-not-clobber synopsis | `metadata_sync.py:194-201,438` |
| **P1** | S-DUP-2 | Re-key series cache/persist on `norm_title(name)` once resolved | `series.py:350` |
| **P1** | S-DUP-3 | Add title-token ownership probe in `_annotate` (incl. author subs) | `series.py:584-588` |
| **P1** | S-DUP-1 | Gate `processed_new`/`added` on `acquire` status | `scheduler.py:1543-1563` |
| **P1** | RES-1 | Prune `SourceAttempt` (>2 days) in an existing tick | `source_state.py:203` |
| **P1** | STATE-1 | Add `matched`/`searching` to `reset_sources` filter | `source_state.py:193` |
| **P1** | STATE-3 | Return `(False, None)` for planned + NULL `release_date` | `ledger.py:256-258` |
| **P1** | TXT-1 | `soup.get_text(" ")` instead of `""` | `textclean.py:110` |
| **P1** | TXT-2 | Add `。！？` to `_SENT_SPLIT`, make trailing `\s` optional | `textclean.py:27` |
| **P1** | TXT-3 | Tighten `_CENSOR_RE` to single-letter shape; gate `is_garbled` on it | `textclean.py:23,55` |
| **P2** | MERGE-3 | Record one canonical identity per work per enrich pass | `catalog_enrichment.py:268` |
| **P2** | DUP-3 | Live no-query browse reads persisted `CatalogGroup` rows | `index.py:601-634` |
| **P2** | S-DUP-4 | Return transient status from gathered coroutines (not ContextVar) | `series.py:399-403` |
| **P2** | S-DUP-5 | Cross-user in-flight cluster gate in `acquire` | `series.py:690-714` |
| **P2** | SSRF-1 | Resolve+pin host, deny unknown schemes in `_target_allowed` | `notify.py:18-42` |
| **P2** | DOS-1 | Drop per-identifier `record_login_failure` (keep per-IP) | `auth.py:312-313` |
| **P2** | AUTHZ-1 | Generic 401 for pending accounts | `auth.py:177-189` |
| **P2** | AUTHZ-2 | Evict token from `static_auth._cache` on revoke (optional) | `static_auth.py:25-50` |
| **P2** | SEC-1 | Force `rel='noopener noreferrer nofollow'` on http(s) `<a>` | `sanitize.py:84-95` |
| **P2** | STATE-2 | Cancel sibling in-flight grabs on `mark_resolved` | `source_state.py:135-143` |
| **P2** | ERR-1 | `db.get(ContentRequest, id)` + guard instead of `refresh` | `rescan.py:138-140` |
| **P2** | CONC-1 | CAS revival guarded on observed `lease_expires_at` | `scheduler.py:663-668` |
| **P2** | TXT-4 | Drop loose ≤3-word heading branch; rely on `_HEADING_RE` | `textclean.py:134` |
| **P2** | IMG-1 | Normalize `//` srcs to `https:` before proxy | `imgproxy.py:40` |

**Strategic (adopt-from-other-apps, beyond one-line fixes — sequence after the P1 quick wins):** (1) identifier-set union-find keyed on ISBN/ASIN/OL-work-id — *the single highest-leverage duplicate fix*; (2) canonical series id + positioned members replacing `extra.series`; (3) declarative provider-precedence table (komf/ABS); (4) tiered series match ladder with confidence cutoffs (Kavita CBL); (5) persistent merge / not-a-duplicate override table surviving regroup (Calibre exemptions); (6) Calibre "Similar" normalization in `norm_title`, reused for release-name matching.