"""Notification subsystem — event catalog + multi-channel dispatch.

Builds on the existing primitives: :func:`app.notify.notify` (Apprise push), :func:`app.kindle`
(SMTP email), and the ``AppSetting`` key/value store. A typed event registry (:data:`REGISTRY`)
describes every notification the app can emit; users/admins opt in/out per event. Each recipient may
have several delivery channels (:class:`app.models.NotificationChannel`) — guided per-service configs
are translated to Apprise URLs by :func:`build_apprise_url`; email is delivered via the shared SMTP.

The single entrypoint is :func:`dispatch_event` — fully DEFENSIVE (never raises) and BLOCKING (does
network I/O), so async callers must invoke it via ``asyncio.to_thread`` (use :func:`dispatch_threadsafe`
which opens its own DB session — never share a request/loop session across threads).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .kindle import app_smtp, send_message, smtp_configured
from .models import (
    AppSetting,
    Notification,
    NotificationChannel,
    User,
    UserSettings,
)
from .notify import notify

log = logging.getLogger("shelf.notifications")


# --------------------------------------------------------------------- event registry
@dataclass(frozen=True)
class EventDef:
    key: str
    label: str
    description: str
    audience: str   # 'user' | 'admin'
    category: str
    default_on: bool


REGISTRY: dict[str, EventDef] = {e.key: e for e in [
    # ---- user · Library ----
    EventDef("library.added", "Added to library",
             "A new title was added to your library.", "user", "Library", True),
    EventDef("download.completed", "Download completed",
             "A download / acquisition you requested finished.", "user", "Library", True),
    EventDef("download.failed", "Download failed",
             "A download / acquisition could not be completed.", "user", "Library", True),
    # ---- user · Delivery ----
    EventDef("kindle.sent", "Sent to Kindle",
             "A title was delivered to your Kindle / email.", "user", "Delivery", False),
    EventDef("kindle.failed", "Kindle / email failed",
             "A Kindle or email delivery failed.", "user", "Delivery", True),
    # ---- user · Announcements ----
    EventDef("admin.downtime", "Planned maintenance",
             "Scheduled downtime / maintenance announcements from the administrator.",
             "user", "Announcements", True),
    EventDef("admin.announcement", "Announcements",
             "General announcements from the administrator.", "user", "Announcements", True),
    # ---- admin · Ops health ----
    EventDef("ops.health_degraded", "Health degraded",
             "The instance reported a degraded health probe.", "admin", "Ops health", True),
    EventDef("ops.job_failed", "Scheduler job failed",
             "A scheduled background job raised an error.", "admin", "Ops health", True),
    EventDef("ops.backup", "Backup result",
             "A scheduled backup succeeded or failed.", "admin", "Ops health", True),
    EventDef("ops.app_started", "App started",
             "The application started up.", "admin", "Ops health", False),
    # ---- admin · Integrations & crawl ----
    EventDef("ops.integration_sync_failed", "Integration sync failed",
             "An integration (Readarr / Kapowarr / SAB / metadata) sync errored.",
             "admin", "Integrations & crawl", True),
    EventDef("ops.crawl_blocked", "Crawl site blocked",
             "A crawl source is blocking or repeatedly erroring (anti-bot).",
             "admin", "Integrations & crawl", True),
    EventDef("ops.high_error_rate", "High error rate",
             "The outbound request error / anti-bot rate is elevated.",
             "admin", "Integrations & crawl", True),
    EventDef("ops.download_failed", "Download failure (ops)",
             "A download failed (operations view, across all users).",
             "admin", "Integrations & crawl", False),
    EventDef("security.malware", "Malware blocked",
             "A torrent-grabbed file was flagged by VirusTotal and deleted before import.",
             "admin", "Integrations & crawl", True),
]}


def events_for(audience: str) -> list[EventDef]:
    """Registry entries for an audience ('user' | 'admin'), in declaration order."""
    return [e for e in REGISTRY.values() if e.audience == audience]


# --------------------------------------------------------------------- channel config / apprise URL
# Per-kind config fields that are secrets — redacted on read, kept-when-blank on update (mirrors the
# global-SMTP password handling in kindle.set_global_smtp).
_SECRET_FIELDS: dict[str, set[str]] = {
    "ntfy": {"token", "password"},
    "pushover": {"token", "user_key"},
    "telegram": {"bot_token"},
    "discord": {"webhook"},
    "slack": {"webhook"},
    "apprise": {"url"},
    "email": set(),
}

CHANNEL_KINDS = tuple(_SECRET_FIELDS.keys())


def public_config(kind: str, cfg: dict | None) -> dict:
    """A config safe to return from the API: non-secret fields verbatim; each secret field replaced
    by a ``<field>_set`` boolean so the UI can show 'configured' without leaking the value."""
    secret = _SECRET_FIELDS.get(kind, set())
    out: dict = {}
    for k, v in (cfg or {}).items():
        if k in secret:
            out[f"{k}_set"] = bool(v)
        else:
            out[k] = v
    return out


def merge_config(kind: str, stored: dict | None, incoming: dict | None) -> dict:
    """Merge a config update: blank secret fields keep the stored value (so re-saving without
    re-typing a token preserves it); ``<field>_set`` echo keys from a read are ignored."""
    secret = _SECRET_FIELDS.get(kind, set())
    cfg = dict(stored or {})
    for k, v in (incoming or {}).items():
        if k.endswith("_set"):
            continue
        if k in secret and not (str(v or "").strip()):
            continue
        cfg[k] = v.strip() if isinstance(v, str) else v
    return cfg


_DISCORD_RE = re.compile(r"/webhooks/(\d+)/([A-Za-z0-9_-]+)")
_SLACK_RE = re.compile(r"/services/(T[A-Za-z0-9]+)/(B[A-Za-z0-9]+)/([A-Za-z0-9]+)")


def _host(server: str | None) -> str:
    """Strip a scheme/path off a server input → bare host (default ntfy.sh)."""
    s = (server or "").strip()
    if not s:
        return "ntfy.sh"
    s = re.sub(r"^https?://", "", s)
    return s.split("/")[0]


def build_apprise_url(kind: str, cfg: dict | None) -> str | None:
    """Translate a structured channel config into an Apprise URL. Returns None when required fields
    are missing (caller skips the channel) or the kind has no URL (email). Never raises."""
    cfg = cfg or {}
    try:
        if kind == "apprise":
            return (cfg.get("url") or "").strip() or None
        if kind == "email":
            return None
        if kind == "ntfy":
            topic = (cfg.get("topic") or "").strip()
            if not topic:
                return None
            host = _host(cfg.get("server"))
            tok = (cfg.get("token") or "").strip()
            user, pw = (cfg.get("user") or "").strip(), (cfg.get("password") or "").strip()
            auth = f"{tok}@" if tok else (f"{user}:{pw}@" if user and pw else "")
            return f"ntfy://{auth}{host}/{topic}"
        if kind == "pushover":
            user_key = (cfg.get("user_key") or "").strip()
            token = (cfg.get("token") or "").strip()
            return f"pover://{user_key}@{token}" if user_key and token else None
        if kind == "telegram":
            bot = (cfg.get("bot_token") or "").strip()
            chat = (cfg.get("chat_id") or "").strip()
            return f"tgram://{bot}/{chat}" if bot and chat else None
        if kind == "discord":
            m = _DISCORD_RE.search(cfg.get("webhook") or "")
            return f"discord://{m.group(1)}/{m.group(2)}" if m else None
        if kind == "slack":
            m = _SLACK_RE.search(cfg.get("webhook") or "")
            return f"slack://{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else None
    except Exception:  # noqa: BLE001 — never let URL building break a save/dispatch
        log.exception("apprise URL build failed for kind=%s", kind)
    return None


# --------------------------------------------------------------------- admin (operator-wide) prefs
_ADMIN_PREFS_KEY = "notify_admin_prefs"


def get_admin_prefs(db: Session) -> dict:
    row = db.get(AppSetting, _ADMIN_PREFS_KEY)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def set_admin_prefs(db: Session, selected: dict) -> dict:
    row = db.get(AppSetting, _ADMIN_PREFS_KEY)
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    for k, v in (selected or {}).items():
        if k in REGISTRY:
            cfg[k] = bool(v)
    if row is None:
        db.add(AppSetting(key=_ADMIN_PREFS_KEY, value=cfg))
    else:
        row.value = cfg
    db.commit()
    return cfg


# --------------------------------------------------------------------- delivery
def deliver_to_channel(db: Session, channel: NotificationChannel, title: str, body: str,
                       *, recipient_email: str | None = None) -> tuple[bool, str | None]:
    """Send one notification through one channel. Returns (ok, error). Never raises."""
    try:
        if channel.kind == "email":
            if not recipient_email:
                return False, "no email address set (Settings → Send to Kindle)"
            cfg = app_smtp(db)
            if not smtp_configured(cfg):
                return False, "the shared mail server isn't configured"
            send_message(cfg, recipient_email, title, body)
            return True, None
        url = channel.apprise_url
        if not url:
            return False, "channel has no delivery URL"
        ok = notify(url, title, body)
        return ok, None if ok else "push delivery failed (check the channel config)"
    except Exception as exc:  # noqa: BLE001
        log.exception("channel delivery failed (kind=%s)", channel.kind)
        return False, str(exc)


def _user_email(settings: UserSettings | None) -> str | None:
    if settings is None:
        return None
    return (settings.delivery_config or {}).get("email_to") or settings.kindle_email or None


# --------------------------------------------------------------------- storm control
_cooldowns: dict[str, float] = {}
_cooldown_lock = threading.Lock()
_DEFAULT_COOLDOWN = 3600.0  # seconds — ops alerts for a persistent condition won't re-fire sooner


def _passes_cooldown(dedup_key: str | None, cooldown: float) -> bool:
    """True if this dedup_key hasn't fired within `cooldown` seconds (records the time when True)."""
    if not dedup_key:
        return True
    now = time.monotonic()
    with _cooldown_lock:
        last = _cooldowns.get(dedup_key)
        if last is not None and now - last < cooldown:
            return False
        _cooldowns[dedup_key] = now
    return True


# --------------------------------------------------------------------- dispatch
def dispatch_event(
    db: Session,
    event_key: str,
    *,
    user_id: int | None = None,
    audience: str | None = None,
    title: str,
    body: str = "",
    level: str = "info",
    context: dict | None = None,
    dedup_key: str | None = None,
    cooldown: float = _DEFAULT_COOLDOWN,
) -> None:
    """Resolve recipients, record an in-app Notification, and fan out to each recipient's enabled
    channels. SYNCHRONOUS + BLOCKING (network I/O) and fully defensive — any error is logged and
    swallowed (a notification must never break the caller). Pass ``dedup_key`` for repeated ops
    alerts so a persistent condition doesn't notify every tick."""
    try:
        evt = REGISTRY.get(event_key)
        if evt is None:
            log.warning("dispatch for unknown event %r", event_key)
            return
        if not _passes_cooldown(dedup_key, cooldown):
            return

        if audience == "admin":
            if not get_admin_prefs(db).get(event_key, evt.default_on):
                return
            recipients = list(db.scalars(
                select(User).where(User.role == "admin", User.is_active.is_(True))))
        elif user_id is not None:
            u = db.get(User, user_id)
            recipients = [u] if (u and u.is_active) else []
        else:
            return

        for user in recipients:
            try:
                # admin-audience events are gated operator-wide above, so skip the per-user gate;
                # user-audience events honour each user's own per-event opt-out.
                _deliver_to_user(db, user, evt, title, body, level, force=audience == "admin")
            except Exception:  # noqa: BLE001 — one recipient must not abort the rest
                log.exception("notification delivery to user %s failed", getattr(user, "id", "?"))
    except Exception:  # noqa: BLE001 — dispatch must never raise into the caller
        log.exception("dispatch_event failed for %r", event_key)


def _deliver_to_user(db: Session, user: User, evt: EventDef, title: str, body: str, level: str,
                     *, force: bool) -> None:
    """Per-user gate (skipped for admin audience, already gated operator-wide), in-app row, fan-out."""
    settings = db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if not force:  # user-audience events honour the user's own per-event opt-out
        prefs = (settings.notify_prefs if settings else None) or {}
        if not prefs.get(evt.key, evt.default_on):
            return

    db.add(Notification(user_id=user.id, event_key=evt.key, title=title[:255],
                        body=body or "", level=level))
    db.commit()

    channels = list(db.scalars(select(NotificationChannel).where(
        NotificationChannel.user_id == user.id, NotificationChannel.enabled.is_(True))))
    # Admins with no personal channel fall back to the admin-configured global default channel.
    if not channels and evt.audience == "admin":
        channels = list(db.scalars(select(NotificationChannel).where(
            NotificationChannel.user_id.is_(None), NotificationChannel.enabled.is_(True))))
    email = _user_email(settings)
    for ch in channels:
        deliver_to_channel(db, ch, title, body, recipient_email=email)


def dispatch_threadsafe(event_key: str, **kw) -> None:
    """Thread/async-safe dispatch: opens its OWN session so it can run inside ``asyncio.to_thread``
    without sharing the caller's request/loop session. Use from async contexts:
    ``await asyncio.to_thread(dispatch_threadsafe, "library.added", user_id=u, title=..., body=...)``."""
    from .db import SessionLocal
    db = SessionLocal()
    try:
        dispatch_event(db, event_key, **kw)
    finally:
        db.close()


def dispatch_soon(db: Session, event_key: str, **kw) -> None:
    """Context-aware, non-blocking dispatch usable from ANY call site. If invoked while an asyncio
    event loop is running (an async request / tick), it offloads to a worker thread with its own DB
    session so the loop is never blocked by push/email I/O (fire-and-forget — dispatch is defensive).
    Otherwise (a sync worker thread / scheduler job) it runs inline on the caller's session."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.run_in_executor(None, lambda: dispatch_threadsafe(event_key, **kw))
    else:
        dispatch_event(db, event_key, **kw)
