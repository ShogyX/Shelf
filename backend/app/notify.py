"""Per-user push notifications via Apprise.

Each user can store an Apprise URL (``UserSettings.apprise_url``) pointing at their own
ntfy / Pushover / Telegram / Discord / … target. ``notify()`` is a thin, defensive wrapper:
it never raises (a failed or missing push must not break the auto-hook pipeline) and does
blocking network I/O, so callers on the event loop should dispatch it via ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging

log = logging.getLogger("shelf.notify")


def notify(apprise_url: str | None, title: str, body: str) -> bool:
    """Send a push to ``apprise_url``. Returns True on success, False otherwise (no-op on a
    blank URL or when apprise isn't installed). Never raises."""
    url = (apprise_url or "").strip()
    if not url:
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
