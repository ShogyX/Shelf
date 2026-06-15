"""Notifications API — delivery channels, per-event preferences, the in-app feed, and (admin) the
global default channel + broadcast. The dispatch engine lives in :mod:`app.notifications`."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import notifications as N
from ..auth import current_user, require_admin
from ..db import get_db
from ..models import Notification, NotificationChannel, User, UserSettings
from ..schemas import (
    BroadcastIn,
    ChannelIn,
    ChannelOut,
    EventDefOut,
    NotificationOut,
    PrefsIn,
)

router = APIRouter()


# ----------------------------------------------------------------- channels
def _channel_out(ch: NotificationChannel) -> ChannelOut:
    return ChannelOut(id=ch.id, kind=ch.kind, label=ch.label,
                      config=N.public_config(ch.kind, ch.config), enabled=bool(ch.enabled))


def _build_or_400(kind: str, config: dict) -> str | None:
    """Build the Apprise URL (None for email). Reject a non-email channel whose config can't yield
    a valid URL so the user fixes it now rather than silently never receiving anything."""
    if kind not in N.CHANNEL_KINDS:
        raise HTTPException(400, f"unknown channel kind {kind!r}")
    url = N.build_apprise_url(kind, config)
    if kind != "email" and not url:
        raise HTTPException(400, "couldn't build a valid notification target from those details")
    return url


@router.get("/notifications/channels", response_model=list[ChannelOut])
def list_channels(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[ChannelOut]:
    rows = db.scalars(select(NotificationChannel).where(NotificationChannel.user_id == user.id)
                      .order_by(NotificationChannel.id)).all()
    return [_channel_out(c) for c in rows]


@router.post("/notifications/channels", response_model=ChannelOut)
def create_channel(payload: ChannelIn, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> ChannelOut:
    cfg = N.merge_config(payload.kind, {}, payload.config)
    url = _build_or_400(payload.kind, cfg)
    ch = NotificationChannel(user_id=user.id, kind=payload.kind, label=(payload.label or None),
                             config=cfg, apprise_url=url,
                             enabled=True if payload.enabled is None else payload.enabled)
    db.add(ch); db.commit(); db.refresh(ch)
    return _channel_out(ch)


def _owned_channel(db: Session, channel_id: int, user_id: int | None) -> NotificationChannel:
    ch = db.get(NotificationChannel, channel_id)
    if ch is None or ch.user_id != user_id:
        raise HTTPException(404, "channel not found")
    return ch


@router.put("/notifications/channels/{channel_id}", response_model=ChannelOut)
def update_channel(channel_id: int, payload: ChannelIn, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> ChannelOut:
    ch = _owned_channel(db, channel_id, user.id)
    if payload.label is not None:
        ch.label = payload.label or None
    if payload.enabled is not None:
        ch.enabled = payload.enabled
    if payload.config:
        ch.config = N.merge_config(ch.kind, ch.config, payload.config)
        ch.apprise_url = _build_or_400(ch.kind, ch.config)
    db.commit(); db.refresh(ch)
    return _channel_out(ch)


@router.delete("/notifications/channels/{channel_id}")
def delete_channel(channel_id: int, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> dict:
    ch = _owned_channel(db, channel_id, user.id)
    db.delete(ch); db.commit()
    return {"deleted": True}


@router.post("/notifications/channels/{channel_id}/test")
def test_channel(channel_id: int, user: User = Depends(current_user),
                 db: Session = Depends(get_db)) -> dict:
    ch = _owned_channel(db, channel_id, user.id)
    settings = db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    ok, err = N.deliver_to_channel(
        db, ch, "Shelf test notification",
        "If you can read this, this channel is working. 🎉",
        recipient_email=N._user_email(settings))
    return {"ok": ok, "error": err}


# ----------------------------------------------------------------- event preferences
def _prefs_view(events: list, selected: dict) -> list[EventDefOut]:
    return [EventDefOut(key=e.key, label=e.label, description=e.description, audience=e.audience,
                        category=e.category, default_on=e.default_on,
                        enabled=bool(selected.get(e.key, e.default_on)))
            for e in events]


@router.get("/notifications/prefs", response_model=list[EventDefOut])
def get_prefs(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[EventDefOut]:
    s = db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    return _prefs_view(N.events_for("user"), (s.notify_prefs if s else None) or {})


@router.put("/notifications/prefs", response_model=list[EventDefOut])
def set_prefs(payload: PrefsIn, user: User = Depends(current_user),
              db: Session = Depends(get_db)) -> list[EventDefOut]:
    s = db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if s is None:
        s = UserSettings(user_id=user.id)
        db.add(s)
    prefs = dict(s.notify_prefs or {})
    for k, v in payload.selected.items():
        if k in N.REGISTRY and N.REGISTRY[k].audience == "user":
            prefs[k] = bool(v)
    s.notify_prefs = prefs
    db.commit()
    return _prefs_view(N.events_for("user"), prefs)


@router.get("/notifications/admin/prefs", response_model=list[EventDefOut],
            dependencies=[Depends(require_admin)])
def get_admin_prefs(db: Session = Depends(get_db)) -> list[EventDefOut]:
    return _prefs_view(N.events_for("admin"), N.get_admin_prefs(db))


@router.put("/notifications/admin/prefs", response_model=list[EventDefOut],
            dependencies=[Depends(require_admin)])
def set_admin_prefs(payload: PrefsIn, db: Session = Depends(get_db)) -> list[EventDefOut]:
    cfg = N.set_admin_prefs(db, payload.selected)
    return _prefs_view(N.events_for("admin"), cfg)


# ----------------------------------------------------------------- in-app feed
@router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = Query(False),
    limit: int = Query(30, ge=1, le=100),
    before_id: int | None = Query(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[Notification]:
    sel = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        sel = sel.where(Notification.read_at.is_(None))
    if before_id:
        sel = sel.where(Notification.id < before_id)
    return list(db.scalars(sel.order_by(Notification.id.desc()).limit(limit)))


@router.get("/notifications/unread-count")
def unread_count(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    n = db.scalar(select(func.count(Notification.id)).where(
        Notification.user_id == user.id, Notification.read_at.is_(None))) or 0
    return {"count": int(n)}


@router.post("/notifications/read-all")
def read_all(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    from sqlalchemy import update
    db.execute(update(Notification).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)
    ).values(read_at=func.now()))
    db.commit()
    return {"count": 0}


@router.post("/notifications/{notification_id}/read")
def mark_read(notification_id: int, user: User = Depends(current_user),
              db: Session = Depends(get_db)) -> dict:
    n = db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(404, "notification not found")
    if n.read_at is None:
        n.read_at = func.now()
        db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- admin: global channel + broadcast
@router.get("/notifications/admin/global-channel", response_model=ChannelOut | None,
            dependencies=[Depends(require_admin)])
def get_global_channel(db: Session = Depends(get_db)) -> ChannelOut | None:
    ch = db.scalar(select(NotificationChannel).where(NotificationChannel.user_id.is_(None))
                   .order_by(NotificationChannel.id))
    return _channel_out(ch) if ch else None


@router.put("/notifications/admin/global-channel", response_model=ChannelOut,
            dependencies=[Depends(require_admin)])
def set_global_channel(payload: ChannelIn, db: Session = Depends(get_db)) -> ChannelOut:
    ch = db.scalar(select(NotificationChannel).where(NotificationChannel.user_id.is_(None))
                   .order_by(NotificationChannel.id))
    merged = N.merge_config(payload.kind, ch.config if ch else {}, payload.config)
    url = _build_or_400(payload.kind, merged)
    if ch is None:
        ch = NotificationChannel(user_id=None, kind=payload.kind, label=payload.label or "Global",
                                 config=merged, apprise_url=url, enabled=True)
        db.add(ch)
    else:
        ch.kind = payload.kind
        ch.label = payload.label or ch.label
        ch.config = merged
        ch.apprise_url = url
        if payload.enabled is not None:
            ch.enabled = payload.enabled
    db.commit(); db.refresh(ch)
    return _channel_out(ch)


@router.post("/notifications/admin/broadcast", dependencies=[Depends(require_admin)])
def broadcast(payload: BroadcastIn, db: Session = Depends(get_db)) -> dict:
    event_key = "admin.downtime" if payload.kind == "downtime" else "admin.announcement"
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    for u in users:
        N.dispatch_event(db, event_key, user_id=u.id, title=payload.title, body=payload.body,
                         level="warn" if payload.kind == "downtime" else "info")
    return {"recipients": len(users)}
