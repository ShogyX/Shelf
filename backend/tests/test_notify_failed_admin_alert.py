"""Stock-job failures (user_id=None) must still emit the admin ops.download_failed alert — only the
user-facing download.failed notice is gated on there being a requesting user."""
from __future__ import annotations

from types import SimpleNamespace

from app.ingestion import downloads


def _capture(monkeypatch):
    calls: list[dict] = []

    def _dispatch(db, event_key, **kw):
        calls.append({"event_key": event_key, **kw})

    import app.notifications as notif
    monkeypatch.setattr(notif, "dispatch_soon", _dispatch)
    return calls


def test_stock_job_failure_still_alerts_admin(monkeypatch):
    calls = _capture(monkeypatch)
    job = SimpleNamespace(user_id=None, catalog_work_id=None, error="boom")
    downloads._notify_failed(object(), job)

    events = [c["event_key"] for c in calls]
    assert "ops.download_failed" in events           # admin alert fires for stock jobs
    assert "download.failed" not in events           # no user notice without a user
    admin = next(c for c in calls if c["event_key"] == "ops.download_failed")
    assert admin["audience"] == "admin"


def test_user_job_failure_alerts_both(monkeypatch):
    calls = _capture(monkeypatch)
    job = SimpleNamespace(user_id=7, catalog_work_id=None, error="boom")
    downloads._notify_failed(object(), job)

    events = [c["event_key"] for c in calls]
    assert "download.failed" in events
    assert "ops.download_failed" in events
