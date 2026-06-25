# Shelf Deep Review — 2026-06-25 (local-only, multi-agent)

Two workflow passes. Round 1 produced the **`+` Add popup** diagnosis (now FIXED, commit `fe59e40`).
Round 2 produced the 6 remaining dimensions below. Every finding carries concrete evidence
(file:line, read-only DB query, or screenshot). Operator-gated items (prod DB mutations) are flagged
and must NOT be run as ad-hoc scripts against prod `./shelf.db` (two prior wipe incidents).

## Executive summary
App is fundamentally healthy: consistent chrome/rails/responsive, zero JS page errors, sound matching
logic, well-tuned WAL/pragmas/scheduler, all 4 crawl sites produce plausible correctly-typed rows.
Issues cluster into: (A) metadata/catalog **data integrity** — the only P1s, both fallout from the
earlier works-table wipe/rebuild + a crawl classifier gap; (B) crawl parsing/classification quality;
(C) cover delivery efficiency; (D) frontend tidiness/doc drift.

## P1 — must-fix (data integrity)
- **P1-1. Stale `metadata_links` → recycled work ids.** 39/54 `auto` links now mismatch (link 14 →
  work 1442 "First Lie Wins" but ref is "One Piece"; link 10 → "Klara and the Sun" matched "Tom
  Sawyer" conf 1.0). Cause: works wipe/rebuild reused ids 1392–1650, FK is `NO ACTION` +
  `foreign_keys=0`, and `check_releases` (`metadata_sync.py:400-440`) re-validates by `ref` only,
  never re-scoring vs the work's CURRENT title. Fix: (1) re-score in `check_releases` and skip/expire
  links below `MATCH_THRESHOLD` [code, safe]; (2) one-time cleanup of stale `auto` links [**operator
  DB mutation**]; (3) enable FK + `ON DELETE CASCADE` on `metadata_links.work_id` [**migration**].
- **P1-2. Gutenberg "Readers also downloaded" → 1,533 fake works.** `catalog_groups` 135402,
  member_count=1533, members are `web_index` rows ending `/ebooks/<id>/also`. `/also` matches no
  listing/junk/subpage regex and the title isn't in `_GENERIC_TITLES`, so each becomes a CatalogWork
  unioned on one empty-author norm_key. Fix: add `also`/`downloads?` to `_LISTING_PATH_RE`/
  `_WORK_SUBPAGE_RE` (`extract.py:754`) + `"readers also downloaded"` to `_GENERIC_TITLES` [code];
  purge group 135402 via regroup [**operator**].

## P2 — should-fix
- **P2-1.** Boilerplate titles ("Test"=88, "CHAPTER###"=42, "The Mentor"=55) merge unrelated works →
  extend `_GENERIC_TITLES` (`extract.py`) with test/chapter/untitled/prologue/epilogue/contents.
- **P2-2.** Prose flipped to `comic` via title keyword (gutenberg 74 "Comic Latin Grammar" etc.;
  contagion to "War and Peace" via group merge). `detect_media_kind` blob includes `title`
  (`extract.py:745`) → drop title; rely on og_type/site_name/URL/domain; gate upgrade at `catalog.py:153`.
- **P2-3.** Hardcover manga ingested as `text` (group 5110 "Berserk"; 19,956 rows, `meta_label` NULL)
  → map Hardcover comic/manga category to `media_kind='comic'` at ingest (`metadata.py`/`provider_catalog.py`),
  mirroring AniList `_FORMAT_LABEL`.
- **P2-4.** Dead `Jobs` page default export (`pages/Jobs.tsx:19`, unrouted; only `JobRow` is used) → delete body, keep `JobRow`.
- **P2-5.** Stale "Jobs tab" copy (`CatalogCard.tsx` lines 127/158/175/195/479/507/651/685-686/960/978; check Stock/Watchlist) → "Sources".
- **P2-6.** Duplicated acquire-mutation logic (`CatalogCard.tsx:116-199` re-defined at `CatalogDetail` 909-990) → extract `useCatalogAcquire(group)` hook (mirrors `useAddTitle`). Resolves half of P2-5.
- **P2-7.** Header mixes SVG icons with emoji (`App.tsx:49` 🎨, `NotificationBell.tsx:54` 🔔) → Lucide-style inline SVG (Palette/Bell, currentColor).
- **P2-8.** `db.py:18-24,62` pool comments contradict code (`pool_size=20,max_overflow=40`; worst case 24MB×60≈1.44GB on 8GB box) → reconcile COMMENTS (don't change `max_overflow` without observed pressure).
- **P2-9.** Covers ship full-res into 166px slots (1 viewport = 5.8MB/23 imgs; 14/25 >1.6× needed, up to 2164×3264). `covers.save_cover` (`covers.py:48-53`) no downscale → Pillow downscale to ~600×900 JPEG q82/WebP. ~5-8× cut.
- **P2-10.** No `srcset`/`sizes` on cover `<img>` (`Cover.tsx:79-90`) → add after P2-9.
- **P2-11.** Local covers cached only 1h though content-addressed/immutable (`/covers/<hash>.jpg`
  `private,max-age=3600`; `/api/cover` already `immutable`). → give `/covers` (+ `/media/imgcache`) the
  `max-age=31536000, immutable` header. **Corrects the too-conservative value set earlier this session.** Cheapest cover win.

## P3 — polish
- **P3-1.** `imgcache_sweep_tick` (2h) full-scans 432k `catalog_works` on `cover_url LIKE '%/imgcache/%'`
  (no matching partial index) → add partial index in `db._ensure_indexes` mirroring the `_remote` ones.
- **P3-2.** `Sources.tsx`/`ListImports.tsx` mis-located as "pages" (no default export/route) → move to `components/`.
- **P3-3.** comix.to never extracts author (`adapters/comix.py:107` hardcodes None) → read authors/artists from JSON. Low value.
- **P3-4.** Webtoon "authors" are synopsis fragments (221/24,752) → suppress byline-from-description for comic sites or validate name-shape.
- **P3-5.** Non-cover/wrong-aspect images leak into 2:3 slots → reject `w/h>1.3` in `imagecache._fetch_image`. Rare/cosmetic.

## Recommended fix order
- **Wave 1 (safe code-only quick-wins):** P2-11 cover immutable cache · P2-7 emoji→SVG · P2-4+P2-5 dead Jobs + copy · P2-8 comment reconcile · P3-1 partial indexes.
- **Wave 2 (classifier/ingest, code-only; new rows self-correct, historical purge separate/operator):** P1-2 + P2-1 + P2-2 (`extract.py`/`catalog.py`) · P2-3 Hardcover mapping · P1-1 `check_releases` re-score.
- **Wave 3 (refactor):** P2-6 shared hook · P2-9→P2-10 cover thumbnailing then srcset · P3 polish.
- **Operator-gated (DB mutations / migration — explicit go-ahead, run via app/migration, never ad-hoc on prod):** P1-1 stale-link cleanup + FK CASCADE migration · P1-2/P2-1 historical catalog-group purge.

---

## Execution status (2026-06-25)

**DONE + committed (code-only, verified):**
- P1-2, P2-1, P2-2 — catalog classifier fixes (`extract.py`) — `3023c3b`
- P1-1 (code part) — stale-link re-score guard in `check_releases` (`metadata_sync.py`) — `3023c3b`
- P2-11 — `/covers` immutable cache header — `3023c3b` (verified live: `private, max-age=31536000, immutable`)
- P2-8 — db.py pool/cache comment reconcile — `3023c3b`
- P2-7 — emoji→SVG header icons — `77fba7c` (verified in browser)
- P2-4 — deleted dead `Jobs` default export — `77fba7c`
- P2-5 — "Jobs tab"→"Sources page" copy — `77fba7c`
- (`fe59e40`) the `+` Add popup P1 — Modal portal + mobile popover (round-1 finding)

**DEFERRED (with reason — not done):**
- **P1-1 data cleanup + FK CASCADE migration**, **P1-2 / P2-1 historical catalog-group purge** — prod DB mutations / migration; **operator-gated** (two prior wipe incidents). The classifier/re-score code fixes mean NEW rows self-correct + stale links stop being acted on; existing bogus rows still need the gated purge. Awaiting go-ahead.
- **P2-3 Hardcover comic-mapping** — Hardcover search returns an opaque doc blob; needs a real API response (operator token + external call) to find the genre/format field. Can't verify locally.
- **P2-6 `useCatalogAcquire` dedup** — behavior-preserving refactor of the live acquire flow; can't be verified on prod without triggering acquisitions (forbidden). Needs the demo instance / operator testing.
- **P2-9 / P2-10 cover thumbnailing + srcset** — proper fix is a thumbnail-variant pipeline + srcset + backfill (a mini-project); a naive downscale-on-save would soften the hero and only help new covers. P2-11's immutable cache already removed the repeat-fetch cost.
- **P3-1 imgcache index** — skipped: the two sweep queries use different LIKE shapes; a partial index wouldn't reliably be used. Bounded (2h cadence) anyway.
- **P3-2/3/4/5** — low-value churn (file moves; comic-site author extraction; rare aspect-ratio reject). Skipped/deferred.
