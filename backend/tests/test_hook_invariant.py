"""The audio-hooking invariant is enforced at the DATA layer: hooked_work_id must never point at a
media_kind='audio' Work. Per-write-site guards demonstrably failed (142 violating rows reached
production via a 'provably safe' path), so db.enforce_invariant_triggers makes the DB refuse the
write — and db._purge_audio_hooks cleans legacy state at boot."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal, _purge_audio_hooks, init_db
from app.models import CatalogGroup, CatalogWork, Work


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    s.execute(delete(CatalogGroup))
    s.execute(delete(CatalogWork))
    s.execute(delete(Work))
    s.commit()
    yield s
    s.rollback()
    s.close()


def _audio(db) -> Work:
    w = Work(title="Dune (audio)", media_kind="audio", local_path="/a/dune.m4b")
    db.add(w); db.commit(); db.refresh(w)
    return w


def test_db_refuses_audio_hooks(db):
    w = _audio(db)
    db.add(CatalogWork(domain="d", work_url="u1", title="Dune", norm_key="dune",
                       hooked_work_id=w.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(CatalogGroup(norm_key="dune", media_bucket="text", title="Dune",
                        hooked_work_id=w.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # UPDATE path: hook a text work (fine), then try to re-point at the audio one.
    t = Work(title="Dune", media_kind="text", local_path="/b/dune.epub")
    db.add(t); db.commit(); db.refresh(t)
    cw = CatalogWork(domain="d", work_url="u2", title="Dune", norm_key="dune",
                     hooked_work_id=t.id)
    db.add(cw); db.commit(); db.refresh(cw)
    cw.hooked_work_id = w.id
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # Un-hooking always passes (NULL is never a violation).
    cw2 = db.get(CatalogWork, cw.id)
    cw2.hooked_work_id = None
    db.commit()


def test_purge_audio_hooks_cleans_legacy_state(db):
    # Create the forbidden state via the trigger-invisible route (media_kind flipped AFTER hooking),
    # exactly how legacy rows arose — then the boot purge must clean it.
    w = Work(title="Artemis", media_kind="text", local_path="/a/artemis.m4b")
    db.add(w); db.commit(); db.refresh(w)
    local = CatalogWork(provider="local", domain="local", work_url=f"local:{w.id}",
                        title="Artemis", norm_key="artemis", hooked_work_id=w.id)
    real = CatalogWork(provider="hardcover", domain="hardcover.app", work_url="hc:1",
                       title="Artemis", norm_key="artemis", hooked_work_id=w.id)
    db.add_all([local, real]); db.commit()
    w.media_kind = "audio"; db.commit()
    lid, rid = local.id, real.id
    db.close()

    _purge_audio_hooks()

    s = SessionLocal()
    assert s.get(CatalogWork, lid) is None                      # local mirror row deleted outright
    kept = s.get(CatalogWork, rid)
    assert kept is not None and kept.hooked_work_id is None     # real row kept, just un-hooked
    s.close()
