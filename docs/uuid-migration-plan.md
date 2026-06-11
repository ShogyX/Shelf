# Migration plan: universally-unique identifiers for cross-instance safety

## Why
Primary keys are per-instance auto-increment integers, so a backup's `id = N` means a *different*
row on the target. A merge restore that preserved those ids therefore mis-linked the library
(see the 2026-06-11 incident: every `library_item` pointed at the wrong work after a merge).

That specific corruption is **already fixed** — `app/backup.py` now remaps integer PKs and rewrites
foreign keys on restore (`_FK_COLUMNS`, `_NATURAL_KEY`, `_load_table_mapped`), with a regression test
(`test_merge_into_populated_db_remaps_ids_no_mislink`). The remap relies on per-table *natural keys*;
tables without one (notably `works`) insert fresh, which is correct but can duplicate if the same
backup is merged twice.

A stable, universally-unique id per row removes that last gap: it gives every entity an identity that
survives across instances, so a merge dedupes on it (no duplicates) and a sync/federation feature
becomes possible later.

## Recommended approach: additive `uid`, NOT a PK swap
Do **not** replace the integer PKs. Converting every PK + FK + query to UUID is a very large, risky
change for little extra benefit over an additive column, and it bloats every index (UUIDs are 4× the
width of a 32-bit int and kill locality on SQLite).

Instead add a `uid TEXT` column (a UUIDv4 / ULID string) to the entity tables, unique, generated once
and **never reused**. Integer PKs stay as the internal join key; `uid` is the cross-instance identity.

### Tables that need a `uid`
The entities that cross instances and whose mislink causes damage:
`users, works, chapters, chapter_contents, bookshelves, library_items, reading_states,
bookshelf_items, metadata_links, catalog_groups, catalog_works`.
(Operational/transient tables — download_jobs, crawl_jobs, stock_*, usenet_grabs, queued_hooks,
sessions — do not need one; they're not migrated or are already keyed by a natural value.)

## Steps
1. **Schema (Alembic migration).**
   - Add nullable `uid` to each table above; create a unique index on it.
   - Backfill every existing row with a fresh UUID in the migration (batched `UPDATE`).
   - A follow-up migration makes it `NOT NULL` once backfilled.
2. **Models.** Add `uid: Mapped[str] = mapped_column(String(36), unique=True, index=True,
   default=lambda: str(uuid.uuid4()))` to each model so new rows self-assign. Add a SQLAlchemy
   `before_insert` safety hook that fills `uid` if missing.
3. **Backup/restore (the payoff).** In `_NATURAL_KEY`, key these tables on `("uid",)`. The existing
   `_load_table_mapped` remap logic then dedupes on `uid` (same uid → reuse the target row's int id;
   new uid → fresh int id), and the FK remap already rewrites children through `idmap`. This removes
   the "insert fresh → possible duplicate" caveat for `works`/`catalog_groups`.
4. **Backward compatibility.** A backup from *before* this change has no `uid` column. `_load_table`
   is already version-tolerant (missing column → default), and `_NATURAL_KEY` must fall back to the
   prior natural key when a row carries no `uid` — so old backups still restore. Keep the current
   natural keys as the fallback in `_NATURAL_KEY`.
5. **API/UI (optional, later).** Nothing must change immediately — `uid` is internal. If a public
   stable identifier is ever wanted (share links, federation), expose `uid` instead of the int id.

## Effort & risk
- ~1 Alembic migration + backfill (the slow part: one UPDATE per ~600k rows — batch it).
- Model edits are mechanical (one column each).
- The restore change is ~5 lines (swap the natural key) because the remap machinery already exists.
- Risk is low and contained: the int PKs and all existing joins are untouched; `uid` is additive.

## Acceptance test
Extend `test_merge_into_populated_db_remaps_ids_no_mislink`: merge the SAME backup **twice** into a
populated instance and assert the library has no duplicate works/library_items the second time
(dedupe on `uid`), in addition to the existing "links to the right work, not the decoy" assertion.
