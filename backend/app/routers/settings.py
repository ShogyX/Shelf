from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..models import User, UserSettings
from ..schemas import SettingsIn, SettingsOut

router = APIRouter()

DEFAULT_READER_PREFS = {
    "fontFamily": "serif",
    "fontSize": 19,
    "lineHeight": 1.7,
    "letterSpacing": 0,
    "paragraphSpacing": 1.0,
    "measure": 38,
    "justify": False,
    "mode": "scroll",
    "textColor": "",
    "bgColor": "",
    "textLightness": None,
    "bgLightness": None,
    "fabX": None,
    "fabY": None,
    "fabSide": "right",      # left | right | top | bottom
    "fabPos": 0.5,           # fractional position along that edge (0..1)
    "textPosition": 50,      # 0=left … 50=center … 100=right
}

# Delivery keys returned to the client (password is never returned).
_DELIVERY_PUBLIC = ("smtp_host", "smtp_port", "smtp_username", "smtp_from", "smtp_security",
                    "email_to")


def _delivery_view(cfg: dict) -> dict:
    cfg = cfg or {}
    out = {k: cfg.get(k) for k in _DELIVERY_PUBLIC}
    out["smtp_password_set"] = bool(cfg.get("smtp_password"))
    return out


def _get_or_create(db: Session, user_id: int) -> UserSettings:
    s = db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if s is None:
        s = UserSettings(user_id=user_id, theme="system", reader_prefs=dict(DEFAULT_READER_PREFS))
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def _out(s) -> SettingsOut:
    from ..config import get_settings as _gs
    from ..kindle import resolve_smtp, smtp_configured

    prefs = {**DEFAULT_READER_PREFS, **(s.reader_prefs or {})}
    cfg = resolve_smtp(_gs(), s.delivery_config or {})
    return SettingsOut(
        theme=s.theme,
        reader_prefs=prefs,
        kindle_email=s.kindle_email,
        smtp_configured=smtp_configured(cfg),
        delivery=_delivery_view(s.delivery_config or {}),
    )


@router.get("/settings", response_model=SettingsOut)
def get_settings_ep(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> SettingsOut:
    return _out(_get_or_create(db, user.id))


@router.put("/settings", response_model=SettingsOut)
def update_settings_ep(
    payload: SettingsIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> SettingsOut:
    s = _get_or_create(db, user.id)
    if payload.theme is not None:
        s.theme = payload.theme
    if payload.reader_prefs is not None:
        s.reader_prefs = {**(s.reader_prefs or {}), **payload.reader_prefs}
    if payload.kindle_email is not None:
        s.kindle_email = payload.kindle_email.strip() or None
    if payload.delivery is not None:
        cfg = dict(s.delivery_config or {})
        incoming = payload.delivery
        for k in ("smtp_host", "smtp_username", "smtp_from", "smtp_security", "email_to"):
            if k in incoming:
                cfg[k] = (incoming[k] or "").strip()
        if "smtp_port" in incoming and incoming["smtp_port"]:
            cfg["smtp_port"] = int(incoming["smtp_port"])
        # Password: only overwrite when a non-empty value is supplied.
        if incoming.get("smtp_password"):
            cfg["smtp_password"] = incoming["smtp_password"]
        s.delivery_config = cfg
    db.commit()
    db.refresh(s)
    return _out(s)
