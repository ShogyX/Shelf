from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin
from ..db import get_db
from ..models import User, UserSettings
from ..schemas import GlobalSmtpIn, GlobalSmtpOut, SettingsIn, SettingsOut

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
# The SMTP server is now global (admin-configured); a user's delivery config holds only their own
# recipient ('email_to' private inbox; 'kindle_email' is a separate column).
def _delivery_view(cfg: dict) -> dict:
    return {"email_to": (cfg or {}).get("email_to")}


def _get_or_create(db: Session, user_id: int) -> UserSettings:
    s = db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if s is None:
        s = UserSettings(user_id=user_id, theme="system", reader_prefs=dict(DEFAULT_READER_PREFS))
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def _out(s, db: Session) -> SettingsOut:
    from ..kindle import app_smtp, smtp_configured

    prefs = {**DEFAULT_READER_PREFS, **(s.reader_prefs or {})}
    cfg = app_smtp(db)  # the global (admin) SMTP server
    return SettingsOut(
        theme=s.theme,
        reader_prefs=prefs,
        kindle_email=s.kindle_email,
        smtp_configured=smtp_configured(cfg),
        # The shared sending address — the user can see who their mail comes from (read-only).
        smtp_from=cfg.sender or None,
        delivery=_delivery_view(s.delivery_config or {}),
        apprise_url=s.apprise_url,
    )


@router.get("/settings", response_model=SettingsOut)
def get_settings_ep(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> SettingsOut:
    return _out(_get_or_create(db, user.id), db)


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
    if payload.apprise_url is not None:
        s.apprise_url = payload.apprise_url.strip() or None
    if payload.delivery is not None:
        # Only the user's own recipient is per-user now; the SMTP server is global/admin.
        cfg = dict(s.delivery_config or {})
        if "email_to" in payload.delivery:
            cfg["email_to"] = (payload.delivery["email_to"] or "").strip()
        s.delivery_config = cfg
    db.commit()
    db.refresh(s)
    return _out(s, db)


def _global_smtp_out(db: Session) -> GlobalSmtpOut:
    from ..kindle import app_smtp, get_global_smtp, smtp_configured
    g = get_global_smtp(db)
    cfg = app_smtp(db)
    return GlobalSmtpOut(
        smtp_host=g.get("smtp_host") or cfg.host or None,
        smtp_port=int(g.get("smtp_port") or cfg.port or 587),
        smtp_username=g.get("smtp_username") or cfg.username or None,
        smtp_from=g.get("smtp_from") or cfg.sender or None,
        smtp_security=g.get("smtp_security") or ("ssl" if cfg.ssl else "starttls"),
        smtp_password_set=bool(g.get("smtp_password") or cfg.password),
        configured=smtp_configured(cfg),
    )


@router.get("/settings/smtp", response_model=GlobalSmtpOut,
            dependencies=[Depends(require_admin)])
def get_global_smtp_ep(db: Session = Depends(get_db)) -> GlobalSmtpOut:
    """The shared, admin-configured SMTP server every user sends through (password never returned)."""
    return _global_smtp_out(db)


@router.put("/settings/smtp", response_model=GlobalSmtpOut,
            dependencies=[Depends(require_admin)])
def set_global_smtp_ep(payload: GlobalSmtpIn, db: Session = Depends(get_db)) -> GlobalSmtpOut:
    """Configure the shared SMTP server (admin only). Password is only updated when re-entered."""
    from ..kindle import set_global_smtp
    set_global_smtp(db, payload.model_dump(exclude_none=True))
    return _global_smtp_out(db)
