"""Per-user push notifications via Apprise.

Each user can store an Apprise URL (``UserSettings.apprise_url``) pointing at their own
ntfy / Pushover / Telegram / Discord / … target. ``notify()`` is a thin, defensive wrapper:
it never raises (a failed or missing push must not break the auto-hook pipeline) and does
blocking network I/O, so callers on the event loop should dispatch it via ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

log = logging.getLogger("shelf.notify")

# Self-hosted push schemes whose host is user-supplied — allowed only if the host is public.
_HOST_VALIDATED_SCHEMES = {
    "ntfy", "ntfys", "matrix", "matrixs", "matrixc", "mqtt", "mqtts",
    "rsyslog", "xmpp", "smtp", "smtps",
}
# Fixed-vendor schemes that connect to the PROVIDER's own host (no user-supplied target host) and are
# safe. Any scheme not in this set and not host-validated is denied (SSRF-1: default-deny, not allow).
_FIXED_VENDOR_SCHEMES = {
    "discord", "telegram", "tgram", "pover", "pushover", "slack", "pbul", "pushbullet",
    "gotify", "gotifys", "twilio", "mailgun", "sendgrid", "ses", "msteams", "wxteams",
    "signal", "signals", "rocket", "rockets", "mailto", "mailtos",
}


def _target_allowed(url: str) -> bool:
    """Reject Apprise targets usable for SSRF. Default-DENY: only fixed-vendor schemes (which connect
    to the provider's own host) and self-hosted schemes whose user-supplied host resolves to a public
    address are allowed. Any unknown scheme is refused."""
    try:
        pr = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    scheme = (pr.scheme or "").lower()
    if scheme in _HOST_VALIDATED_SCHEMES:
        host = pr.hostname
        if not host:
            log.warning("notify: refusing host-validated apprise scheme %r with no host", scheme)
            return False
        from .ingestion.netguard import is_public_url
        netloc = host if not pr.port else f"{host}:{pr.port}"
        if not is_public_url(f"http://{netloc}"):
            log.warning("notify: refusing apprise target with non-public host %r", host)
            return False
        return True
    if scheme in _FIXED_VENDOR_SCHEMES:
        return True
    log.warning("notify: refusing unknown/SSRF-capable apprise scheme %r", scheme)
    return False


def notify(apprise_url: str | None, title: str, body: str) -> bool:
    """Send a push to ``apprise_url``. Returns True on success, False otherwise (no-op on a
    blank URL or when apprise isn't installed). Never raises."""
    url = (apprise_url or "").strip()
    if not url:
        return False
    if not _target_allowed(url):
        return False
    try:
        import apprise  # imported lazily so the app runs without the optional push backend
    except Exception:  # noqa: BLE001
        log.warning("apprise is not installed; skipping push notification")
        return False
    try:
        ap = apprise.Apprise()
        if not ap.add(url):
            log.warning("apprise rejected the configured URL (malformed?)")
            return False
        return bool(ap.notify(title=title, body=body))
    except Exception:  # noqa: BLE001 — a push failure must never break the caller
        log.exception("apprise notification failed")
        return False
