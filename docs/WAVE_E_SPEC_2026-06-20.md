# Wave E — Follow Author / Series — Implementation Spec (R14-R16)

Read-only deep-dive (backend-architect). LOCKED: per-subscription `auto_request` (NO
blanket global); "request all by author" = enumerate-then-counted-confirm + hard cap
(SERIES_ACQUIRE_CAP=30); dedicated Following view; follow on the catalog detail, not the
Wanted page; auto-added rows REUSE Wave D `origin`/`origin_detail` (origin="following").
`auto_request` default = **True** (R15 "Follow → auto-fetch"; off-switch in Following view).

## Conflicts/flags (decided)
- No reusable author-enumeration exists — `inauthor:` is buried in `_gb_series` and
  series-filtered. Add `enumerate_author` lifting the GB fetch body out of `_gb_series`
  WITHOUT changing series behavior (test_series must stay green). The one net-new helper.
- The tick is a NEW `follow_tick` slot (6h), NOT an extension of `check_releases`
  (that's per-MetadataLink). Mirror `source_retry_tick`/`queued_hook_tick`.
- Author key = `extract._author_norm(name)` (order-sensitive token-bag; same author in two
  name orders → two keys — documented v1 limitation). Series key = `norm_title(series_name)`.
- Background tick can't toast → write a `Notification` row (bell) on auto-fire.

## 1. Model `Subscription` (after VtSubmission, models.py:943) + migration 0037
Cols: id, user_id(FK,index), kind(author|series), key(512,index), display_name,
active(bool=T), auto_request(bool=T), known_keys(JSON — diff baseline, single writer ok),
auto_added(int=0), last_checked_at, created_at. UNIQUE(user_id, kind, key). Migration 0037
mirrors 0034-0036 idempotent inspect-before-create, down_revision 0036.

## 2. Enumeration (series.py)
- `enumerate_author(db, author_name: str) -> list[dict]`: assemble from GB `inauthor:"<name>"`
  (lift `_gb_series` GB body → `_gb_author_volumes`) + OL `_ol_query` author search; dedup by
  norm_title, drop `_BUNDLE_RE`, gate every candidate through `authors_compatible`. Returns the
  detect_series-shaped dicts. Wrap in `telemetry.instrument("metadata")` + reset `_series_transient`.
  Hardcover author query = optional v2 (GB+OL covers v1).
- Owned/requested skip: run output through `_annotate(db, None, books)` → `hooked_work_id`
  (owned, any user) skipped; already-requested handled by `acquire`→note_request/is_gated reuse.

## 3. Request-all-by-author (R14)
- `series.acquire_author` = `acquire_series` with ONE swap (enumerate_author instead of
  detect_series); reuse cap-30 + `_resolve_book_row` + owned-skip + the `acq.acquire(ctx)` loop;
  ctx={"author_full", "origin"?, "origin_detail"?}.
- `catalog.acquire_author` (near catalog.py:1208) mirrors `acquire_series`.
- Routes (index.py ~1075): `GET /catalog/{id}/author` → {author, books, count} (plain auth);
  `POST /catalog/{id}/author/acquire` (gated `_INDEX_ACQUIRE`) {refs?, all, shelf_id?}. Enumerate
  returns FULL count so the UI confirm is honest ("Queue 30 of 142?") — cap enforced server-side + logged.
- FE: author in CatalogCard.tsx:282/816 → menu: "Request all by {author}" opens `AuthorModal`
  (clone SeriesModal:390, swap catalogSeries→catalogAuthor / acquireSeries→acquireAuthor /
  qk.series→qk.author; reuse useConfirm+pickShelf+checkbox roster) + "Follow {author}" → subscribe.
  SeriesModal footer gets a "Follow series" button.

## 4. follow_tick (R15/R16) — scheduler.py new slot (6h), logic in follow.py or series.py
For each active sub: resolve rep (author=display_name string; series=a CatalogWork with
extra["series"]==display_name) → enumerate (author=enumerate_author; series=detect_series.books) →
if `_series_transient` set, SKIP this round (don't poison baseline) → diff current vs known_keys →
for auto_request subs: each NEW + not-owned book → `_resolve_book_row` then
`acquire(user_id=sub.user_id, context={author_full, origin:"following", origin_detail:name})`,
bump auto_added, cap new per tick at SERIES_ACQUIRE_CAP; non-auto subs: no acquire (lazy "Check now"
in Following view re-enumerates) → set known_keys=current, last_checked_at=now, commit. A
`Notification` row when auto_added increased. Guards (owned/gated/requested) all reused from acquire.
SEED known_keys at SUBSCRIBE time so day-1 backlog isn't auto-fired — only future titles.

## 5. Following view + endpoints
- `routers/subscriptions.py` (new, per-user gated, register main.py:168): GET /subscriptions (own);
  POST {kind, catalog_id?|series_name?} (resolve key+display_name, idempotent upsert on unique,
  seed known_keys best-effort); PATCH /{id} {auto_request?, active?} (403 non-owner); DELETE /{id}.
  `SubscriptionOut`/`SubscriptionCreateIn` in schemas.py.
- FE: `pages/Following.tsx` (mirror Missing.tsx list) — row: name · kind badge · since · "{n}
  auto-added" · auto on/off toggle (PATCH) · Unfollow (DELETE+confirm) · "Check now" for non-auto.
  `App.tsx` nav+route after Wanted; `client/subscriptions.ts`; `qk.subscriptions`.

## Tests + risks
Tests: subscribe/list/unfollow/toggle (+403 cross-user); request-all-by-author (full count, cap,
skip-owned); follow_tick (auto on→new title opens "following" row + auto_added++; auto off→no-op;
owned/gated NOT re-requested; transient→baseline unchanged). Risks: prolific author flood (cap +
seed-at-subscribe + off switch); provider hammer (6h + sleep(0.5) + bounded queries); cross-user
(acquire user_id=sub.user_id, owned-skip global); author-key spelling drift (documented); transient
poisoning baseline (`_series_transient` skip). Reuse acquire/acquire_series/note_request/origin cols/
scheduler slot — smallest diff. Do NOT extend check_releases; do NOT change _gb_series behavior.
</content>
