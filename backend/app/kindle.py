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
            server.login(cfg.username, cfg.password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass
