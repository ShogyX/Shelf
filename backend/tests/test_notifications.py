"""Notification subsystem: Apprise URL building, dispatch (recipient resolution + per-event gating +
multi-channel fan-out + dedup), the in-app feed, and the API surface (channels, prefs, broadcast)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app import notifications as N
from app.db import SessionLocal, init_db
from app.main import app
from app.models import (
    Notification,
    NotificationChannel,
    User,
    UserSession,
    UserSettings,
)


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for m in (Notification, NotificationChannel, UserSettings, UserSession, User):
        db.execute(delete(m))
    db.commit()
    db.close()
    N._cooldowns.clear()
    yield


@pytest.fixture
def capture(monkeypatch):
    """Capture push + email deliveries instead of sending them."""
    pushes: list[tuple] = []
    emails: list[tuple] = []
    monkeypatch.setattr(N, "notify", lambda url, t, b: pushes.append((url, t, b)) or True)
    monkeypatch.setattr(N, "send_message", lambda cfg, to, subj, body: emails.append((to, subj, body)))
    monkeypatch.setattr(N, "smtp_configured", lambda cfg: True)
    return pushes, emails


def _user(db, name, role="user"):
    u = User(username=name, password_hash="x", role=role, is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    return u


def test_delete_user_removes_notifications_and_channels():
    """F15: deleting a user must also remove their notifications + notification channels, or an
    orphaned enabled channel keeps attempting delivery for a now-deleted user_id."""
    from app.routers.auth import delete_user
    db = SessionLocal()
    admin = _user(db, "admin", role="admin")
    victim = _user(db, "victim")
    db.add(Notification(user_id=victim.id, event_key="download.completed", title="t", body="b"))
    db.add(NotificationChannel(user_id=victim.id, kind="ntfy", apprise_url="ntfy://nt/a", enabled=True))
    db.commit()
    delete_user(victim.id, admin=admin, db=db)
    assert db.scalar(select(func.count(Notification.id)).where(Notification.user_id == victim.id)) == 0
    assert db.scalar(select(func.count(NotificationChannel.id))
                     .where(NotificationChannel.user_id == victim.id)) == 0
    db.close()


# ----------------------------------------------------------------- build_apprise_url
def test_build_apprise_url():
    assert N.build_apprise_url("ntfy", {"topic": "t"}) == "ntfy://ntfy.sh/t"
    assert N.build_apprise_url("ntfy", {"server": "https://n.example/", "topic": "t", "token": "tk"}) \
        == "ntfy://tk@n.example/t"
    assert N.build_apprise_url("pushover", {"user_key": "u", "token": "k"}) == "pover://u@k"
    assert N.build_apprise_url("telegram", {"bot_token": "1:a", "chat_id": "9"}) == "tgram://1:a/9"
    assert N.build_apprise_url("discord", {"webhook": "https://discord.com/api/webhooks/11/AbC_d"}) \
        == "discord://11/AbC_d"
    assert N.build_apprise_url("slack", {"webhook": "https://hooks.slack.com/services/T1/B2/xyz"}) \
        == "slack://T1/B2/xyz"
    assert N.build_apprise_url("apprise", {"url": "json://x"}) == "json://x"
    assert N.build_apprise_url("email", {}) is None          # email has no URL
    assert N.build_apprise_url("ntfy", {}) is None            # missing required field
    assert N.build_apprise_url("pushover", {"user_key": "u"}) is None


def test_config_redaction_and_merge():
    assert N.public_config("discord", {"webhook": "secret"}) == {"webhook_set": True}
    assert N.public_config("ntfy", {"topic": "t", "token": ""}) == {"topic": "t", "token_set": False}
    # a blank secret keeps the stored value; a provided one overwrites
    assert N.merge_config("discord", {"webhook": "OLD"}, {"webhook": ""}) == {"webhook": "OLD"}
    assert N.merge_config("discord", {"webhook": "OLD"}, {"webhook": "NEW"}) == {"webhook": "NEW"}


# ----------------------------------------------------------------- dispatch engine
def test_dispatch_to_user_writes_inapp_and_fans_out(capture):
    pushes, _ = capture
    db = SessionLocal()
    u = _user(db, "alice")
    db.add(UserSettings(user_id=u.id))
    db.add(NotificationChannel(user_id=u.id, kind="ntfy", apprise_url="ntfy://nt/a", enabled=True))
    db.commit()
    N.dispatch_event(db, "library.added", user_id=u.id, title="Added", body="Dune")
    assert pushes == [("ntfy://nt/a", "Added", "Dune")]
    assert db.scalar(select(func.count(Notification.id)).where(Notification.user_id == u.id)) == 1
    db.close()


def test_per_event_optout_suppresses(capture):
    pushes, _ = capture
    db = SessionLocal()
    u = _user(db, "alice")
    db.add(UserSettings(user_id=u.id, notify_prefs={"library.added": False}))
    db.add(NotificationChannel(user_id=u.id, kind="ntfy", apprise_url="ntfy://nt/a", enabled=True))
    db.commit()
    N.dispatch_event(db, "library.added", user_id=u.id, title="Added", body="Dune")
    assert pushes == []
    assert db.scalar(select(func.count(Notification.id))) == 0   # not even an in-app row
    db.close()


def test_admin_fanout_excludes_inactive_and_respects_admin_prefs(capture):
    pushes, _ = capture
    db = SessionLocal()
    a1 = _user(db, "admin1", role="admin")
    a2 = _user(db, "admin2", role="admin")
    inactive = _user(db, "admin3", role="admin"); inactive.is_active = False
    _user(db, "regular")  # non-admin shouldn't receive
    for a in (a1, a2, inactive):
        db.add(NotificationChannel(user_id=a.id, kind="ntfy", apprise_url=f"ntfy://nt/{a.id}", enabled=True))
    db.commit()
    N.dispatch_event(db, "ops.job_failed", audience="admin", title="Job failed", body="x", level="error")
    assert {p[0] for p in pushes} == {f"ntfy://nt/{a1.id}", f"ntfy://nt/{a2.id}"}

    # Operator-wide opt-out suppresses for all admins.
    pushes.clear()
    N.set_admin_prefs(db, {"ops.job_failed": False})
    N.dispatch_event(db, "ops.job_failed", audience="admin", title="Job failed", body="x")
    assert pushes == []
    db.close()


def test_multi_channel_and_disabled_and_email(capture):
    pushes, emails = capture
    db = SessionLocal()
    u = _user(db, "alice")
    db.add(UserSettings(user_id=u.id, delivery_config={"email_to": "me@x.com"}))
    db.add(NotificationChannel(user_id=u.id, kind="ntfy", apprise_url="ntfy://nt/a", enabled=True))
    db.add(NotificationChannel(user_id=u.id, kind="email", enabled=True))
    db.add(NotificationChannel(user_id=u.id, kind="apprise", apprise_url="json://x", enabled=False))
    db.commit()
    N.dispatch_event(db, "kindle.sent", user_id=u.id, title="Sent", body="b")
    # default_on for kindle.sent is False → must explicitly opt in
    assert pushes == [] and emails == []
    s = db.scalar(select(UserSettings).where(UserSettings.user_id == u.id))
    s.notify_prefs = {"kindle.sent": True}; db.commit()
    N.dispatch_event(db, "kindle.sent", user_id=u.id, title="Sent", body="b")
    assert [p[0] for p in pushes] == ["ntfy://nt/a"]      # enabled push only (disabled apprise skipped)
    assert emails == [("me@x.com", "Sent", "b")]          # email channel delivered
    db.close()


def test_one_failing_channel_doesnt_break_others(monkeypatch):
    delivered: list[str] = []

    def flaky(url, t, b):
        if "boom" in url:
            raise RuntimeError("channel down")
        delivered.append(url)
        return True
    monkeypatch.setattr(N, "notify", flaky)
    db = SessionLocal()
    u = _user(db, "alice")
    db.add(UserSettings(user_id=u.id))
    db.add(NotificationChannel(user_id=u.id, kind="apprise", apprise_url="boom://x", enabled=True))
    db.add(NotificationChannel(user_id=u.id, kind="ntfy", apprise_url="ntfy://ok", enabled=True))
    db.commit()
    N.dispatch_event(db, "library.added", user_id=u.id, title="t", body="b")  # must not raise
    assert delivered == ["ntfy://ok"]
    db.close()


def test_dedup_cooldown(capture):
    pushes, _ = capture
    db = SessionLocal()
    a = _user(db, "admin", role="admin")
    db.add(NotificationChannel(user_id=a.id, kind="ntfy", apprise_url="ntfy://nt/a", enabled=True))
    db.commit()
    for _ in range(3):
        N.dispatch_event(db, "ops.health_degraded", audience="admin", title="degraded", body="db",
                         dedup_key="health", cooldown=999)
    assert len(pushes) == 1  # repeated within cooldown → only the first fires
    db.close()


# ----------------------------------------------------------------- API
@pytest.fixture
def admin_client():
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "hunter2pw"})
        yield c


def test_channels_crud_and_test_and_prefs(admin_client, capture):
    pushes, _ = capture
    # create a guided ntfy channel — server builds the apprise URL, secrets redacted on read
    r = admin_client.post("/api/notifications/channels",
                          json={"kind": "ntfy", "config": {"topic": "mine", "token": "sek"}})
    assert r.status_code == 200
    ch = r.json()
    assert ch["kind"] == "ntfy" and ch["config"] == {"topic": "mine", "token_set": True}

    # test it → routes through the (captured) push
    t = admin_client.post(f"/api/notifications/channels/{ch['id']}/test").json()
    assert t["ok"] is True and pushes and pushes[-1][0] == "ntfy://sek@ntfy.sh/mine"

    # a bad channel is rejected at create time
    assert admin_client.post("/api/notifications/channels",
                             json={"kind": "telegram", "config": {"bot_token": "x"}}).status_code == 400

    # event prefs: default_on respected, then flip one off
    prefs = admin_client.get("/api/notifications/prefs").json()
    assert any(e["key"] == "library.added" and e["enabled"] for e in prefs)
    admin_client.put("/api/notifications/prefs", json={"selected": {"library.added": False}})
    prefs = {e["key"]: e["enabled"] for e in admin_client.get("/api/notifications/prefs").json()}
    assert prefs["library.added"] is False

    admin_client.delete(f"/api/notifications/channels/{ch['id']}")
    assert admin_client.get("/api/notifications/channels").json() == []


def test_inapp_feed_and_broadcast(admin_client, capture):
    pushes, _ = capture
    admin_client.post("/api/notifications/channels", json={"kind": "ntfy", "config": {"topic": "t"}})
    # broadcast a planned-downtime notice to all users (just the admin here)
    r = admin_client.post("/api/notifications/admin/broadcast",
                          json={"kind": "downtime", "title": "Maintenance", "body": "back at 3pm"})
    assert r.json()["recipients"] == 1
    assert pushes and pushes[-1][1] == "Maintenance"

    assert admin_client.get("/api/notifications/unread-count").json()["count"] == 1
    items = admin_client.get("/api/notifications").json()
    assert len(items) == 1 and items[0]["event_key"] == "admin.downtime"
    admin_client.post(f"/api/notifications/{items[0]['id']}/read")
    assert admin_client.get("/api/notifications/unread-count").json()["count"] == 0


def test_admin_prefs_and_global_channel_admin_only(admin_client):
    # admin prefs surface admin-audience events
    pa = admin_client.get("/api/notifications/admin/prefs").json()
    assert any(e["key"] == "ops.health_degraded" for e in pa)
    # global fallback channel
    g = admin_client.put("/api/notifications/admin/global-channel",
                         json={"kind": "ntfy", "config": {"topic": "ops"}}).json()
    assert g["kind"] == "ntfy"
    assert admin_client.get("/api/notifications/admin/global-channel").json()["id"] == g["id"]
