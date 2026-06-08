"""Granular per-user permissions.

A capability model layered on top of the admin/user role. **Admins implicitly hold every
permission** and are never restricted. A normal user holds a configurable SUBSET of capability
flags: their own ``User.permissions`` (a JSON list) when set, otherwise the admin-controlled
global default (``AppSetting['default_user_permissions']``), otherwise the built-in baseline.

Scope (per the product decision): permissions govern the USER-FACING layer — which pages a user
may see and the actions hook / acquire / add / send. Infrastructure MANAGEMENT (editing sources,
managing jobs/crawl, integrations, backups, user management) stays admin-only and is NOT grantable
here; those endpoints keep their ``require_admin`` gate.

Mirrors the ``allowed_categories`` design: a per-user JSON override column, a global default in
AppSetting, a single ``effective_permissions`` resolver, and the resolved set echoed in ``MeOut``.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

# Capability flag → human description (shown in the admin Users UI). Canonical order.
PERMISSIONS: dict[str, str] = {
    "index.view": "Browse the Index / discovery catalog",
    "index.hook": "Hook discovered titles into their library",
    "index.acquire": "Acquire / download titles (integrations + usenet pipeline)",
    "add.use": "Use the Add page (add by URL / import a file)",
    "send.kindle": "Send to Kindle + set their own target / private email",
    "jobs.view": "View the Jobs page (read-only — managing jobs stays admin)",
    "sources.view": "View the Sources page (read-only — editing stays admin)",
}
ALL_PERMISSIONS: tuple[str, ...] = tuple(PERMISSIONS)

# What a brand-new normal user can do when no global default has been configured.
DEFAULT_PERMISSIONS: tuple[str, ...] = (
    "index.view", "index.hook", "index.acquire", "add.use", "send.kindle",
)

_DEFAULT_PERMISSIONS_KEY = "default_user_permissions"  # AppSetting key for the normal-user default


def clean_permissions(perms) -> list[str]:
    """Keep only known permission flags, in canonical order, deduped."""
    s = {p for p in (perms or []) if p in PERMISSIONS}
    return [p for p in ALL_PERMISSIONS if p in s]


def get_default_permissions(db: Session) -> list[str] | None:
    """The admin-set default permission set for normal users. ``None`` = use the built-in baseline."""
    from .models import AppSetting
    row = db.get(AppSetting, _DEFAULT_PERMISSIONS_KEY)
    val = row.value if row else None
    return clean_permissions(val) if isinstance(val, list) else None


def set_default_permissions(db: Session, perms: list[str] | None) -> list[str] | None:
    """Set (or clear, with ``None``) the normal-user default permission set. Returns the new value."""
    from .models import AppSetting
    row = db.get(AppSetting, _DEFAULT_PERMISSIONS_KEY)
    if perms is None:
        if row is not None:
            db.delete(row)
        db.commit()
        return None
    clean = clean_permissions(perms)
    if row is None:
        db.add(AppSetting(key=_DEFAULT_PERMISSIONS_KEY, value=clean))
    else:
        row.value = clean
    db.commit()
    return clean


def effective_permissions(db: Session, user) -> list[str]:
    """The capabilities ``user`` actually has. Admins → ALL; a normal user uses their own
    ``permissions`` if set, else the global default, else the built-in baseline."""
    if user is None:
        return []
    if getattr(user, "role", None) == "admin":
        return list(ALL_PERMISSIONS)
    perms = user.permissions
    if perms is None:
        perms = get_default_permissions(db)
    if perms is None:
        perms = list(DEFAULT_PERMISSIONS)
    return clean_permissions(perms)


def has_permission(db: Session, user, permission: str) -> bool:
    return permission in effective_permissions(db, user)
