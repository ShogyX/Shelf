"""Variant expansion (usenet/annas/torrent paths): a title acquired via a download route also
tracks every configured content language × format combination (EN/NO × ebook/audiobook), guarded
by edition_exists, with language pinned via the same-cluster member row in that language."""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion import acquire as acq
from app.models import CatalogWork, Work


@pytest.fixture
def db(monkeypatch):
    init_db()
    s = SessionLocal()
    for m in (CatalogWork, Work):
        s.execute(delete(m))
    s.commit()
    monkeypatch.setattr("app.config_store.content_languages", lambda: ["en", "no"])
    yield s
    s.close()


def _cw(db, title, lang, norm_key=None):
    cw = CatalogWork(domain="d", work_url=f"u/{title}/{lang}", title=title,
                     norm_key=norm_key or title.lower(), media_kind="text", language=lang)
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


async def test_expands_missing_language_and_format(db, monkeypatch):
    rep = _cw(db, "Dune", "en")
    no_row = _cw(db, "Dune", "no")
    calls = []

    async def fake_acquire(db_, member, *, user_id, priority, shelf_id=None,
                           context=None, variant="ebook", _expand=True, **kw):
        calls.append((member.id, member.language, variant, _expand))
        return {"route": "pipeline", "status": "downloading"}

    monkeypatch.setattr(acq, "acquire", fake_acquire)
    await acq.expand_variants(db, rep, user_id=1, priority=["pipeline"], done="ebook")
    combos = {(lang, v) for _id, lang, v, _e in calls}
    # EN ebook already handled (the trigger) — the other three combos fire, recursion-guarded.
    assert combos == {("en", "audiobook"), ("no", "ebook"), ("no", "audiobook")}
    assert all(e is False for *_x, e in calls)
    # The Norwegian combos are pinned via the NO member row (its language drives the whole chain).
    assert {c[0] for c in calls if c[1] == "no"} == {no_row.id}


async def test_expansion_skips_owned_editions_and_unknown_languages(db, monkeypatch):
    rep = _cw(db, "Mistborn", "en")     # cluster has NO norwegian row → 'no' combos skipped
    # The EN audiobook edition already exists in the library → skipped by edition_exists.
    db.add(Work(title="Mistborn", author=None, media_kind="audio", language="en",
                local_path="/a/mistborn.m4b"))
    db.commit()
    calls = []

    async def fake_acquire(db_, member, **kw):
        calls.append((member.language, kw.get("variant")))
        return {"route": "pipeline", "status": "downloading"}

    monkeypatch.setattr(acq, "acquire", fake_acquire)
    await acq.expand_variants(db, rep, user_id=1, priority=["pipeline"], done="ebook")
    assert calls == []                   # nothing to do: EN ebook done, EN audio owned, no NO row


async def test_comics_expand_language_only(db, monkeypatch):
    rep = _cw(db, "Berserk", "en")
    rep.media_kind = "comic"; db.commit()
    _no = _cw(db, "Berserk", "no")
    _no.media_kind = "comic"; db.commit()
    calls = []

    async def fake_acquire(db_, member, **kw):
        calls.append((member.language, kw.get("variant")))
        return {"route": "pipeline", "status": "downloading"}

    monkeypatch.setattr(acq, "acquire", fake_acquire)
    await acq.expand_variants(db, rep, user_id=1, priority=["pipeline"], done="ebook")
    assert calls == [("no", "ebook")]    # no comic audiobooks
