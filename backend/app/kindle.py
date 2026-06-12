"""Email delivery (Send-to-Kindle and send-to-personal-email) over SMTP.

SMTP can be configured either via env (SHELF_SMTP_*) or, preferably, by the operator
through the UI (stored in UserSettings.delivery_config). For Send-to-Kindle the From
address must be on the Amazon account's approved personal-document sender list.
"""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from .config import Settings


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    sender: str = ""
    starttls: bool = True
    ssl: bool = False


def resolve_smtp(env: Settings, delivery: dict | None) -> SmtpConfig:
    """Build the effective SMTP config — UI/DB settings take precedence over env."""
    d = delivery or {}
    if d.get("smtp_host"):
        sec = (d.get("smtp_security") or "starttls").lower()
        return SmtpConfig(
            host=d.get("smtp_host", ""),
            port=int(d.get("smtp_port") or 587),
            username=d.get("smtp_username", "") or "",
            password=d.get("smtp_password", "") or "",
            sender=d.get("smtp_from", "") or "",
            starttls=sec == "starttls",
            ssl=sec == "ssl",
        )
    return SmtpConfig(
        host=env.smtp_host,
        port=env.smtp_port,
        username=env.smtp_user,
        password=env.smtp_password,
        sender=env.smtp_from,
        starttls=env.smtp_starttls,
        ssl=env.smtp_ssl,
    )


def smtp_configured(cfg: SmtpConfig) -> bool:
    return bool(cfg.host and cfg.sender)


# --- Global (admin-configured) SMTP ------------------------------------------------------------
# The SMTP SERVER is configured once by the admin and shared by every user; a user only sets WHO
# the mail goes to (their Kindle / private address). Stored in AppSetting 'global_smtp'.
_GLOBAL_SMTP_KEY = "global_smtp"


def get_global_smtp(db) -> dict:
    """The admin-set global SMTP server settings (may be empty → env fallback applies)."""
    from .models import AppSetting
    row = db.get(AppSetting, _GLOBAL_SMTP_KEY)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def set_global_smtp(db, incoming: dict) -> dict:
    """Merge admin SMTP settings into the global config. Password is only overwritten when a
    non-empty value is supplied (so saving without re-typing it keeps the stored one)."""
    from .models import AppSetting
    row = db.get(AppSetting, _GLOBAL_SMTP_KEY)
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    for k in ("smtp_host", "smtp_username", "smtp_from", "smtp_security"):
        if k in incoming:
            cfg[k] = (incoming.get(k) or "").strip()
    if incoming.get("smtp_port"):
        cfg["smtp_port"] = int(incoming["smtp_port"])
    if incoming.get("smtp_password"):  # only when re-entered
        cfg["smtp_password"] = incoming["smtp_password"]
    if row is None:
        db.add(AppSetting(key=_GLOBAL_SMTP_KEY, value=cfg))
    else:
        row.value = cfg
    db.commit()
    return cfg


def app_smtp(db) -> SmtpConfig:
    """The effective SMTP server for sending: the admin global config (precedence) else env."""
    from .config import get_settings
    return resolve_smtp(get_settings(), get_global_smtp(db))


def send_document(
    cfg: SmtpConfig,
    to_email: str,
    subject: str,
    body: str,
    attachment: bytes,
    filename: str,
    mime: tuple[str, str] = ("application", "epub+zip"),
) -> None:
    """Email a document attachment. Raises on misconfiguration or SMTP failure."""
    if not smtp_configured(cfg):
        raise RuntimeError(
            "SMTP is not configured. Set it in Settings → Send to Kindle, or via "
            "SHELF_SMTP_HOST / SHELF_SMTP_FROM."
        )

    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(attachment, maintype=mime[0], subtype=mime[1], filename=filename)

    if cfg.ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=60)
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=60)
    try:
        server.ehlo()
        if cfg.starttls and not cfg.ssl:
            server.starttls()
            server.ehlo()
        if cfg.username:
            # Never AUTH over cleartext: with neither SSL nor STARTTLS the credentials would
            # cross the wire in the clear. Refuse loudly so the operator fixes the config
            # instead of silently leaking the mailbox password on every send.
            if not (cfg.ssl or cfg.starttls):
                raise RuntimeError(
                    "SMTP credentials are configured but neither SSL nor STARTTLS is enabled — "
                    "refusing to send the password over an unencrypted connection. Enable "
                    "SSL/STARTTLS in Settings → Send to Kindle (or remove the username for an "
                    "open relay)."
                )
            server.login(cfg.username, cfg.password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass
