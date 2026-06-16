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

# Apprise schemes that issue an HTTP(S) request to an ARBITRARY, URL-specified host: a logged-in
# (non-admin) user could point one at internal services / cloud metadata (169.254.169.254) — an SSRF
# primitive. Always refused.
_DENY_SCHEMES = {"json", "jsons", "xml", "xmls", "form", "forms", "file", "files"}
# Self-hosted push schemes whose host is user-supplied — allowed only if the host is public.
_HOST_VALIDATED_SCHEMES = {"ntfy", "ntfys", "matrix", "matrixs", "matrixc", "mqtt", "mqtts"}


def _target_allowed(url: str) -> bool:
    """Reject Apprise targets usable for SSRF: generic-HTTP-request schemes (json/xml/form/file)
    outright, and self-hosted schemes (ntfy/matrix/mqtt) whose host resolves to a private/internal/
    metadata address. Fixed-vendor schemes (discord, telegram, pushover, slack, …) connect to the
    provider's own host and are allowed."""
    try:
        pr = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    scheme = (pr.scheme or "").lower()
    if scheme in _DENY_SCHEMES:
        log.warning("notify: refusing SSRF-capable apprise scheme %r", scheme)
        return False
    if scheme in _HOST_VALIDATED_SCHEMES and pr.hostname:
        netloc = pr.hostname if not pr.port else f"{pr.hostname}:{pr.port}"
        from .ingestion.netguard import is_public_url
        if not is_public_url(f"http://{netloc}"):
            log.warning("notify: refusing apprise target with non-public host %r", pr.hostname)
            return False
    return True


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
