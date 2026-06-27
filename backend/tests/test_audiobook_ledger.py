"""Audiobooks get the same missing-content ledger as ebooks: a SEPARATE per-format row, gated and
re-checked independently — so finding one format never stops the other from being retried. This is the
"if both are requested and one is found, the other is still retried periodically" guarantee."""
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import ledger
from app.models import CatalogWork, ContentRequest, ContentRequestRequester


def _fresh_db():
    init_db()
    db = SessionLocal()
    db.execute(delete(ContentRequestRequester))
    db.execute(delete(ContentRequest))
    db.execute(delete(CatalogWork))
    db.commit()
    cw = CatalogWork(provider="x", provider_ref="r", domain="d", work_url="w", title="Dune",
                     norm_key="dune", media_kind="text")
    db.add(cw); db.commit(); db.refresh(cw)
    return db, cw


def _rows(db):
    return {r.variant: r for r in db.scalars(
        select(ContentRequest).where(ContentRequest.norm_key == "dune")).all()}


def test_audiobook_row_is_separate_and_gated_independently():
    db, cw = _fresh_db()
    # An audiobook miss opens an AUDIOBOOK row only — the ebook is untouched.
    ledger.mark_unavailable(db, cw, reason="no_match", variant="audiobook")
    rows = _rows(db)
    assert set(rows) == {"audiobook"} and rows["audiobook"].status == "unavailable"
    assert ledger.is_gated(db, cw, variant="audiobook")[0] is True
    assert ledger.is_gated(db, cw, variant="ebook")[0] is False     # no ebook row → not gated
    db.close()


def test_resolving_one_format_leaves_the_other_being_chased():
    db, cw = _fresh_db()
    # Both formats requested; both come back unavailable → two independent rows.
    ledger.note_request(db, cw, None, variant="ebook")
    ledger.note_request(db, cw, None, variant="audiobook")
    ledger.mark_unavailable(db, cw, reason="no_match", variant="ebook")
    ledger.mark_unavailable(db, cw, reason="no_match", variant="audiobook")
    assert set(_rows(db)) == {"ebook", "audiobook"}

    # The EBOOK is found → only its gate clears; the AUDIOBOOK row stays unavailable (still retried).
    ledger.mark_resolved(db, cw, variant="ebook")
    rows = _rows(db)
    assert rows["ebook"].status == "resolved"
    assert rows["audiobook"].status == "unavailable"
    assert rows["audiobook"].next_check_at is not None          # the periodic re-check is still scheduled
    assert ledger.is_gated(db, cw, variant="audiobook")[0] is True
    db.close()


if __name__ == "__main__":
    test_audiobook_row_is_separate_and_gated_independently()
    test_resolving_one_format_leaves_the_other_being_chased()
    print("ok")
