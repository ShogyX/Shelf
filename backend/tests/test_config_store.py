"""Runtime config overrides (Settings → System): override wins, blank reverts, consumers honor it."""
from __future__ import annotations

import logging

from app import config_store as cs
from app.config import get_settings
from app.db import SessionLocal, init_db


def _reset(db):
    cs.update(db, {f: ("" if t is str else getattr(get_settings(), f)) for f, t in cs.EDITABLE.items()})


def test_override_and_revert_string():
    init_db()
    db = SessionLocal()
    try:
        cs.update(db, {"flaresolverr_url": "http://solver.local:8191"})
        assert cs.effective("flaresolverr_url") == "http://solver.local:8191"
        assert "flaresolverr_url" in cs.overridden()
        # consumer honors it
        from app.ingestion import flaresolverr as fs
        assert fs._endpoint() == "http://solver.local:8191/v1"
        cs.update(db, {"flaresolverr_url": ""})           # blank reverts to env/default
        assert "flaresolverr_url" not in cs.overridden()
        assert cs.effective("flaresolverr_url") == get_settings().flaresolverr_url
    finally:
        cs.update(db, {"flaresolverr_url": ""}); db.close()


def test_typed_coercion_and_unknown_keys_ignored():
    init_db()
    db = SessionLocal()
    try:
        cs.update(db, {"imgcache_max_mb": "16384", "comix_browser_enabled": "false",
                       "bogus_field": "x"})
        assert cs.effective("imgcache_max_mb") == 16384            # str → int
        assert cs.effective("comix_browser_enabled") is False       # "false" → bool
        assert "bogus_field" not in cs.all_effective()              # unknown key ignored
    finally:
        cs.update(db, {"imgcache_max_mb": get_settings().imgcache_max_mb,
                       "comix_browser_enabled": get_settings().comix_browser_enabled})
        db.close()


def test_log_level_side_effect():
    init_db()
    db = SessionLocal()
    try:
        cs.update(db, {"log_level": "WARNING"})
        assert logging.getLogger().level == logging.WARNING
    finally:
        cs.update(db, {"log_level": "INFO"}); db.close()
