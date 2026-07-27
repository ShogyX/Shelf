"""One-off full-pool wrong-match scan (read-only).

Walks every Work with a local file (books + audiobooks + comics) through
app.ingestion.match_audit.audit_work and writes the suspects to a JSONL file for adjudication.
Progress lines go to stdout so a long run is observable.

Usage (from /root/Shelf/backend):
    python scripts/match_audit_scan.py [--out /tmp/match-audit.jsonl] [--kind audio|text|comic|all]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

DB = os.path.abspath("shelf.db")
assert os.path.basename(os.getcwd()) == "backend" and os.path.exists(DB), (
    f"run from /root/Shelf/backend; shelf.db not found at {DB}")

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.ingestion.match_audit import audit_work  # noqa: E402
from app.models import Work  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/match-audit.jsonl")
    ap.add_argument("--kind", choices=["audio", "text", "comic", "all"], default="all")
    args = ap.parse_args()
    kinds = ["audio", "text", "comic"] if args.kind == "all" else [args.kind]

    db = SessionLocal()
    ids = list(db.scalars(select(Work.id).where(
        Work.media_kind.in_(kinds), Work.local_path.is_not(None), Work.local_path != "")
        .order_by(Work.id)).all())
    print(f"scanning {len(ids)} works → {args.out}", flush=True)
    suspects = scanned = 0
    with open(args.out, "w") as out:
        for i, wid in enumerate(ids):
            if i and i % 500 == 0:
                print(f"progress {i}/{len(ids)} suspects={suspects}", flush=True)
                db.close(); db = SessionLocal()   # keep the session/read-txn short-lived
            w = db.get(Work, wid)
            if w is None:
                continue
            scanned += 1
            try:
                rec = audit_work(db, w)
            except Exception as exc:  # noqa: BLE001 — one unreadable file must not stop the sweep
                print(f"error work={wid}: {exc}", file=sys.stderr, flush=True)
                continue
            if rec:
                suspects += 1
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"DONE scanned={scanned} suspects={suspects} out={args.out}", flush=True)


if __name__ == "__main__":
    main()
