# Structural dedup plan — identifier-set merge + canonical series id (2026-06-21)

Follow-up to the deep review (`DEEP_REVIEW_2026-06-21.md`). The P1/P2 *one-line* fixes
shipped (commit on main). The findings below are NOT safe one-liners — they need schema
+ careful regroup verification — so they're grouped into two structural mini-projects to
build and review separately. Each fold-in lists the review IDs it retires.

## Project 1 — Identifier-set union-find (highest leverage)
**Problem:** Shelf merges catalog rows on normalized TITLES; every mature analogue
(Calibre, Kavita, Audiobookshelf, OpenLibrary) merges on IDENTIFIERS first. `identity_key`
exists but is a single provider-prefixed string, so cross-provider/cross-edition rows of
the same book don't reconcile.
**Build:**
- Promote identity to a SET of canonical keys per row: ISBN-13, ASIN, OpenLibrary work
  OLID, AniList/MAL id — extracted from the providers Shelf already calls (GB
  industryIdentifiers, OL work key, ranobedb anilist/mal ids). Store as
  `CatalogWork.extra["identity_keys"]` (list) or a small join table; migration.
- In `_union_find_groups` (`catalog.py`), union any two rows sharing ANY identity key
  (before the title pass). This merges cross-language/edition rows `norm_title` can't,
  and makes same-title-different-work SAFE (different ISBNs never merge).
**Folds in / retires:** DUP-2 (subtitle/“Book N” variants of one work reconcile by ISBN
instead of risky title-stripping), MERGE-3 (one canonical identity), and hardens DUP-1.
**Verify:** regroup over a copy; assert no catalog-group count regression + the known
over-merge cases stay split; the existing "editions group / spinoffs separate" tests pass.

## Project 2 — Canonical series id + positioned members
**Problem:** series identity is a free-text `extra["series"]` string + `extra["series_position"]`.
Two real series sharing a name collide; the same series resolves to divergent names across
providers/ticks; the series cache keys on the BOOK title; and there's no stable Work
identity to detect an already-owned volume whose catalog norm_key drifted.
**Build:**
- A canonical series id (Hardcover series id Shelf already fetches in `_hc_series_lookup`,
  else an OL series key) + a `series_members` model referencing (series_id, position,
  work/catalog ref). Migration.
- Re-key `detect_series`'s cache + the persisted enumeration on the SERIES id (not the book
  title) — S-DUP-2.
- Give an owned Work a stable identity (a `Work.norm_key` column or an identity link) so
  `_annotate`'s ownership probe can match a volume whose catalog norm_key drifted — S-DUP-3.
- `collapse_series_cards` / View-Series / `auto_request_series` dedup on the series id.
**Folds in / retires:** S-DUP-2, S-DUP-3, DUP-2 (volume vs series-landing), DUP-3 (one
grouping authority for series collapse across live + persisted endpoints).
**Verify:** two same-named series stay separate; a series re-enumerated across ticks keeps
ONE name + member set; an owned volume isn't re-fetched.

## Standalone deferred items (not structural — quick follow-ups, your call)
These are isolated but were deferred from the one-line pass for care, not size:
- **S-DUP-4** (P2) — `_series_transient` ContextVar isn't visible across `asyncio.gather`;
  partial rosters get cached 14 days → recovered volumes resurface as "new". Fix: the
  gathered wrappers RETURN their transient status (read the ContextVar inside the same task)
  and OR them before persisting. ~10 lines, self-contained.
- **S-DUP-5** (P2) — no cross-user in-flight gate: cluster drift escapes the per-user
  download dedup → a duplicate grab. Fix: a cross-user "active DownloadJob for this cluster"
  gate in `acquire`/`note_request`. Touches the hot acquire path → wants its own review.
- **CONC-1** (P2) — crawl reaper revival is read-then-write, not CAS; a just-renewed healthy
  backfill can be yanked (no corruption — the live runner abandons on its next renew). Fix:
  a guarded `UPDATE … WHERE lease_expires_at == observed` CAS; mind the reaper's batched commit.
- **AUTHZ-1** (P2, FLAGGED) — login returns a distinct 403 for valid-but-pending accounts
  (a credential-validity oracle), but the registration UX (`AuthGate.tsx`) detects pending BY
  that 403 message. Closing the oracle needs a separate non-credential `registration-status`
  path (itself an enumeration surface) → a product decision before changing.
