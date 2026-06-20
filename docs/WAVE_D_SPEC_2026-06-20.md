# Wave D — Wanted Page — Implementation Spec (R6-R12)

Read-only deep-dive (frontend-developer) refining the plan's Wave D + the locked
ux-researcher decisions (flat list, series chip→existing modal, lazy detect_series,
sort≠follow, origin tags, auto_request_series OFF default). Mostly reuse; **one**
migration. file:line verified.

## Reality flags
1. The missing row carries NO series fields. `ContentRequest` has none;
   `CatalogWork` has series only in `extra` JSON; only `Work` has real columns. → the
   chip is driven by the catalog row's `extra["series"]`/`["series_position"]`, which
   the API must surface; the API must also add `catalog_work_id` (needed to open the modal).
2. A roster endpoint ALREADY exists — `GET /catalog/{id}/series` + `POST
   /catalog/{id}/series/acquire` (`index.py:1065-1085`, → detect_series/acquire_series).
   No new endpoint. Reuse `api.catalogSeries`/`api.acquireSeries`.
3. `SeriesModal` is private in `CatalogCard.tsx:390` — **export it** (takes
   `{catalogId, seriesName, onClose}`, does lazy fetch + confirm-gated "Grab all" +
   shelf pick). Do NOT use `SeriesLibraryModal` (wrong semantics: keyed off a library Work).
4. goodreads virtual rows have no catalog_work_id → no chip (correct).

## 1. Rename (R6) — UI only, route stays /api/missing
`App.tsx:130` nav "Missing"→"Wanted"; `Missing.tsx:256` `<h1>`→"Wanted"; empty-state
copy optional. Keep filename/component/query-key/`api.listMissing` (renaming = 12-file churn).

## 2. Sort (R7-R9)
- BE `missing.py` list_missing: add `sort` param (newest|author|series|title, validate
  like status/reason). order_by map: newest=`id.desc()` (default, unchanged);
  author=`lower(author) nulls_last, title`; title=`lower(title)`; series=`outerjoin
  (CatalogWork)` + `json_extract(extra,'$.series') nulls_last` (Ungrouped bucket) then
  `'$.series_position'` then title. Goodreads union stays appended last (comment it). Cap 500.
- FE `Missing.tsx`: a Sort `Select` **un-gated from admin** (R7-9 are user features) beside
  the filters; thread `sort` into `api.listMissing` params + `qk.missing` key; pass for all users.

## 3. Series chip → existing modal (R10/R11)
- API: add `catalog_work_id`, `series`, `series_position` to `MissingRequestOut`
  (`schemas.py:1046`) + TS (`system.ts:133`), populated from the joined CatalogWork.extra
  (reuse the series-sort join; NO detect_series at list time). Populate in `_row_out` +
  the recheck response.
- FE chip in the Row metadata line: when `catalog_work_id!=null && series`, a compact
  clickable chip `Series · {name}{ · #pos}` (Badge-style). **No "3/9" count on load** (that
  needs detect_series). Click → `<SeriesModal catalogId seriesName onClose>` (lazy roster +
  owned/total counts shown INSIDE the modal). Import the now-exported SeriesModal.
- Manual "request whole series" (R11): already wired — SeriesModal "Grab all" → acquireSeries
  (SERIES_ACQUIRE_CAP=30, skips owned). Zero new wiring. Caveat: lands in the caller's library.

## 4. Auto-series (R12)
- Hook in **`catalog.acquire_catalog`** (`catalog.py:1144-1180`) ONLY — NOT `acquire.acquire`
  / `note_request` (recursion + fires on system/stock rechecks). After the ebook acquire:
  `if variant in (ebook,both) and config_store.effective("auto_request_series"):
  await _maybe_auto_series(db, cw, user=user, shelf_id=shelf_id)`. New best-effort guarded
  helper calls `series.detect_series` (lazy, only when toggle on) then `series.acquire_series
  (want_all=True)`. Runaway guards are ALL reused: cap 30, skip owned (hooked_work_id),
  idempotent note_request + is_gated (a sibling already known-missing returns "gated", not
  re-searched); acquire_series→acquire (not acquire_catalog) so no recursion.
- Toggle: `auto_request_series: bool = False` in `config.py` Settings + `config_store.py`
  EDITABLE (mirror `auto_backup_enabled`/`missing_recheck_days`; one runtime_config row, no
  migration, no endpoint). FE: a `Toggle` in Settings `MissingRecheckCard` (`Settings.tsx:653`).

## 5. Origin tags (R12 legibility) — the one migration
- Add `origin: str|None` + `origin_detail: str|None` to `ContentRequest` (`models.py:797`) +
  **migration 0036** (mirror 0034/0035 idempotent style). Default null = "request".
- Stamp `origin="series"`, `origin_detail=<series name>` on NEW sibling rows when
  acquire_series runs from the auto hook (pass a flag; don't overwrite a row the user
  requested directly).
- Surface `origin` (`row.origin or "request"`) + `origin_detail` in `_row_out` +
  `MissingRequestOut` + TS (`origin?: "request"|"goodreads"|"series"`). FE: mirror the
  goodreads pattern — `isSeries` → a "from series" badge (keep the status badge) +
  `from series "{detail}"` in the metadata line + add to `ORIGIN_OPTIONS` filter.

## Diff summary
Rename (2 lines) · sort (missing.py + Missing.tsx + keys) · chip+modal (API fields + export
SeriesModal + Row chip) · auto-series (acquire_catalog hook + config toggle + Settings toggle)
· origin (ContentRequest 2 cols + migration 0036 + stamp + surface). **No new endpoints, no new
modal, no inline rosters; detect_series only lazy.** Existing missing tests: sort defaults to
newest (current behavior), API only ADDS fields — should stay green; add sort + auto-series +
origin tests.
</content>
