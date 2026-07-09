"""One-off repair: the 36GB backup was restored as a MERGE, but integer-PK collisions meant the
user's actual library works were dropped (ON CONFLICT DO NOTHING) while the library_items/reading
rows that reference them were inserted — so the library points at the wrong (stocked) works.

This re-inserts the user's works + their chapters + chapter_contents from the backup with FRESH,
non-colliding ids, then re-points library_items / reading_states / bookshelf_items / metadata_links
(by their own backup row id) to the new work/chapter ids. Idempotent-ish: run with --apply to commit.

Usage:  python relink_library.py [--apply]
"""
import json
import sqlite3
import sys
import zipfile

DB = "/root/Shelf/backend/shelf.db"
ZIP = "/root/Shelf/backend/backups/upload-shelf-backup-full-2026-06-10-20260611-171917.zip"
APPLY = "--apply" in sys.argv


def load(z, name):
    rows = []
    with z.open(f"data/{name}.jsonl") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def cols(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def insert_rows(db, table, rows, current_cols):
    """Insert rows keeping only columns the current schema has (version-tolerant)."""
    if not rows:
        return 0
    keys = [c for c in rows[0] if c in current_cols]
    ph = ",".join("?" for _ in keys)
    quoted = ",".join(f'"{k}"' for k in keys)   # some cols ("index") are SQL keywords
    sql = f"INSERT INTO {table} ({quoted}) VALUES ({ph})"
    db.executemany(sql, [[r.get(k) for k in keys] for r in rows])
    return len(rows)


def main():
    z = zipfile.ZipFile(ZIP)
    bworks = load(z, "works")
    bchapters = load(z, "chapters")
    wids = {w["id"] for w in bworks}
    bchapters = [c for c in bchapters if c["work_id"] in wids]
    chap_ids = {c["id"] for c in bchapters}
    bcontents = [c for c in load(z, "chapter_contents") if c["chapter_id"] in chap_ids]
    bli = load(z, "library_items")
    brs = load(z, "reading_states")
    bbi = load(z, "bookshelf_items")
    bml = load(z, "metadata_links")

    db = sqlite3.connect(DB)
    db.execute("PRAGMA foreign_keys=OFF")
    w_cols = cols(db, "works"); c_cols = cols(db, "chapters"); cc_cols = cols(db, "chapter_contents")

    # --- allocate fresh, non-colliding id ranges
    def maxid(t):
        return db.execute(f"SELECT COALESCE(MAX(id),0) FROM {t}").fetchone()[0]
    nw = maxid("works"); nc = maxid("chapters"); ncc = maxid("chapter_contents")
    W, C, CC = {}, {}, {}
    for w in bworks:
        nw += 1; W[w["id"]] = nw
    for c in bchapters:
        nc += 1; C[c["id"]] = nc
    for cc in bcontents:
        ncc += 1; CC[cc["id"]] = ncc

    # --- remap rows (work_id / chapter_id / content_id / last_chapter_id) onto the new ids
    new_works = []
    for w in bworks:
        r = dict(w); r["id"] = W[w["id"]]
        # re-point catalog hook later; mark hooked so it shows as owned
        new_works.append(r)
    new_chaps = []
    for c in bchapters:
        r = dict(c); r["id"] = C[c["id"]]; r["work_id"] = W[c["work_id"]]
        r["content_id"] = CC.get(c.get("content_id")) if c.get("content_id") else None
        new_chaps.append(r)
    new_cc = []
    for cc in bcontents:
        r = dict(cc); r["id"] = CC[cc["id"]]; r["chapter_id"] = C[cc["chapter_id"]]
        new_cc.append(r)

    print(f"plan: insert works={len(new_works)} chapters={len(new_chaps)} chapter_contents={len(new_cc)}")
    print(f"   work id range {min(W.values())}..{max(W.values())}")
    print("   re-point: library_items=%d reading_states=%d bookshelf_items=%d metadata_links=%d"
          % (len(bli), len(brs), len(bbi), len(bml)))
    print("   title fixes (sample):")
    cur = {r[0]: r[1] for r in db.execute("SELECT id,title FROM works").fetchall()}
    for li in bli[:6]:
        old = li["work_id"]; new = W.get(old)
        want = next((w["title"] for w in bworks if w["id"] == old), "?")
        print(f"     li#{li['id']}: was->{cur.get(old,'?')[:30]!r}  now->{want[:30]!r} (work {old}->{new})")

    if not APPLY:
        print("\nDRY RUN — re-run with --apply to commit.")
        return

    try:
        db.execute("BEGIN")
        insert_rows(db, "works", new_works, w_cols)
        insert_rows(db, "chapter_contents", new_cc, cc_cols)   # contents first (chapters fk them via content_id)
        insert_rows(db, "chapters", new_chaps, c_cols)
        # mark the restored works hooked/owned
        db.executemany("UPDATE works SET hooked=1 WHERE id=?", [(i,) for i in W.values()])
        # re-point the link rows BY THEIR OWN backup row id
        for li in bli:
            db.execute("UPDATE library_items SET work_id=? WHERE id=?", (W[li["work_id"]], li["id"]))
        for rs in brs:
            lc = C.get(rs.get("last_chapter_id")) if rs.get("last_chapter_id") else None
            db.execute("UPDATE reading_states SET work_id=?, last_chapter_id=? WHERE id=?",
                       (W[rs["work_id"]], lc, rs["id"]))
        for bi in bbi:
            db.execute("UPDATE bookshelf_items SET work_id=? WHERE id=?", (W[bi["work_id"]], bi["id"]))
        for ml in bml:
            if ml["work_id"] in W:
                db.execute("UPDATE metadata_links SET work_id=? WHERE id=?", (W[ml["work_id"]], ml["id"]))
        db.execute("COMMIT")
        print("\nAPPLIED.")
    except Exception as e:
        db.execute("ROLLBACK")
        print("ROLLED BACK:", e)
        raise


if __name__ == "__main__":
    main()
