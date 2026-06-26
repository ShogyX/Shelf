"""Runtime-editable configuration overrides (Settings → System).

Behavioral config that used to be env-only is now overridable at runtime AND honored WITHOUT a restart
(unless noted). Overrides persist in one ``AppSetting`` row, mirrored into a process cache (so the hot
path does no DB read), and fall back to the env/``Settings`` default when unset.

Deliberately NOT here: boot/infra/security vars that bind at startup or gate exposure — host, port,
database_url, static_dir, the CSP/security headers, trust_proxy/forwarded_allow_ips/allowed_hosts,
cookie_* , setup_token, scheduler_enabled. Those stay env-only.
"""
from __future__ import annotations

import logging
import threading

from .config import get_settings
from .models import AppSetting

_KEY = "runtime_config"

# field name (must match a Settings attribute) -> coercion type. Drives the API + the editor UI.
EDITABLE: dict[str, type] = {
    "log_level": str,
    "imgcache_max_mb": int,
    "auto_backup_enabled": bool, "auto_backup_level": str,
    "auto_backup_interval_hours": int, "auto_backup_keep": int,
    "index_max_pages": int, "index_max_depth": int,
    "index_stop_after_idle_pages": int, "index_max_pending_frontier": int,
    "flaresolverr_url": str, "flaresolverr_timeout_s": int, "flaresolverr_clearance_ttl_s": int,
    "comix_browser_enabled": bool, "comix_browser_pages_per_tick": int, "solver_chrome_path": str,
    "login_max_attempts": int, "login_window_seconds": int, "min_password_length": int,
    "registration_mode": str,
    "missing_recheck_days": int, "missing_recheck_batch": int,
    "auto_request_series": bool,
    # How often the monitored external reading-list imports are re-polled for new titles.
    "list_sync_interval_hours": int,
    # Daily caps on operator stock searches/downloads (0 = unlimited).
    "stock_searches_per_day": int, "stock_downloads_per_day": int,
}

_lock = threading.Lock()
_overrides: dict[str, object] = {}


def _coerce(field: str, value):
    t = EDITABLE[field]
    if t is bool:
        return value.strip().lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value)
    if t is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return str(value).strip()


def load(db) -> None:
    """Warm the in-process override cache from the persisted row (call once at startup)."""
    row = db.get(AppSetting, _KEY)
    vals = row.value if (row and isinstance(row.value, dict)) else {}
    with _lock:
        _overrides.clear()
        for f, v in vals.items():
            if f in EDITABLE:
                c = _coerce(f, v)
                if c is not None:
                    _overrides[f] = c
    _apply_side_effects()


def effective(field: str):
    """The override for ``field`` if set, else the env/Settings default."""
    with _lock:
        if field in _overrides:
            return _overrides[field]
    return getattr(get_settings(), field)


def all_effective() -> dict:
    return {f: effective(f) for f in EDITABLE}


def overridden() -> set[str]:
    with _lock:
        return set(_overrides)


def update(db, patch: dict) -> dict:
    """Apply overrides from ``patch`` (only keys in EDITABLE). A blank string clears a string override
    (revert to default). Persists, refreshes the cache, and applies side effects (e.g. log level)."""
    row = db.get(AppSetting, _KEY)
    cur = dict(row.value) if (row and isinstance(row.value, dict)) else {}
    for f in EDITABLE:
        if f not in patch:
            continue
        v = patch[f]
        if EDITABLE[f] is str and isinstance(v, str) and not v.strip():
            cur.pop(f, None)          # blank → revert to the default
            continue
        c = _coerce(f, v)
        if c is not None:
            cur[f] = c
    if row is None:
        db.add(AppSetting(key=_KEY, value=cur))
    else:
        row.value = cur
    db.commit()
    load(db)
    return all_effective()


def _apply_side_effects() -> None:
    """Settings that need a live nudge beyond just being read fresh: the root log level."""
    lvl = getattr(logging, str(effective("log_level")).upper(), None)
    if isinstance(lvl, int):
        logging.getLogger().setLevel(lvl)
