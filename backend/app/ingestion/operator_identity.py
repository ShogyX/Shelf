"""Live-editable crawl identity (Settings → Crawl identity).

The PoliteFetcher identifies itself honestly to every site it touches: a ``User-Agent`` (which
carries the project name + a contact URL) and a ``From`` header (a contact email), per the
sourcing principle. These used to be env-only — read once at startup from the cached Settings —
so an operator couldn't correct the contact details (e.g. point a site's admin at their own
address) without editing config and restarting.

Here they live in the ``app_settings`` key/value table and are pushed onto the running fetcher,
so a change takes effect on the next request of every running AND future crawl — no restart.
Clearing a field falls back to the env/config default rather than sending an empty header.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..config import get_settings

log = logging.getLogger("shelf.operator_identity")

_KEY = "operator_identity"
_MAX_LEN = 512
_FIELDS = ("user_agent", "contact_email")


def defaults() -> dict[str, str]:
    """The env/config-seeded identity (``SHELF_USER_AGENT`` / ``SHELF_CONTACT_EMAIL``)."""
    s = get_settings()
    return {"user_agent": s.user_agent, "contact_email": s.contact_email}


def _clean(value, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    v = " ".join(value.split())[:_MAX_LEN]  # collapse newlines/tabs — these become HTTP headers
    return v or fallback


def get_identity(db: Session) -> dict[str, str]:
    """Current crawl identity (operator overrides merged over the env/config defaults)."""
    from ..models import AppSetting

    out = defaults()
    row = db.get(AppSetting, _KEY)
    if row and isinstance(row.value, dict):
        for k in _FIELDS:
            if k in row.value:
                out[k] = _clean(row.value[k], out[k])
    return out


def set_identity(db: Session, updates: dict[str, str]) -> dict[str, str]:
    """Persist overrides, then push them onto the live PoliteFetcher (no restart)."""
    from ..models import AppSetting

    current = get_identity(db)
    for k in _FIELDS:
        if k in (updates or {}) and updates[k] is not None:
            current[k] = _clean(updates[k], current[k])

    row = db.get(AppSetting, _KEY)
    if row is None:
        row = AppSetting(key=_KEY, value=dict(current))
        db.add(row)
    else:
        row.value = dict(current)
    db.commit()

    apply_runtime(current)
    return current


def apply_runtime(identity: dict[str, str]) -> None:
    """Push the identity onto the running fetcher so the next HTTP request (and any new browser
    context) carries it. Safe to call before the fetcher has been created."""
    try:
        from .engine import get_fetcher
        get_fetcher().set_identity(identity["user_agent"], identity["contact_email"])
    except Exception:  # noqa: BLE001
        log.exception("failed to apply crawl identity to the live fetcher")


def apply_saved(db: Session) -> None:
    """At startup, seed the live fetcher from any persisted overrides."""
    apply_runtime(get_identity(db))
