"""Central, admin-configurable storage paths.

Historically these directories were env-only (read once into ``config.Settings`` at boot): the image
cache, cover store, and backups dir. This module lets an admin override them at runtime from
Settings → Storage, while DEFAULTING to whatever is already in place (env or the built-in default) so
nothing changes until they edit it.

Overrides are persisted in a single ``AppSetting`` row and mirrored into a process-level cache so the
hot path (``media_dir()`` is called per cover/image request) never does a DB read. ``load()`` warms
the cache at startup; ``update()`` writes both the row and the cache.

Note on the data model: the STOCK directory is the central on-disk pool; a user's library is just a
list of pointers (``LibraryItem`` → ``Work``), and watched folders are secondary pools the library
also only points into — so re-pointing a path changes where NEW files are written/read, it does not
move what's already on disk (migrate that yourself).
"""
from __future__ import annotations

import threading

from .models import AppSetting

_KEY = "storage_paths"
# App-level directories this module owns (each maps to a config.Settings field of the same name as
# its built-in default). The stock dir + integration/watched-folder paths live in their own stores
# and are surfaced/edited via their own APIs.
KEYS = ("media_dir", "covers_dir", "backup_dir")

_lock = threading.Lock()
_overrides: dict[str, str] = {}


def load(db) -> None:
    """Warm the in-process override cache from the persisted row (call once at startup)."""
    row = db.get(AppSetting, _KEY)
    vals = row.value if (row and isinstance(row.value, dict)) else {}
    with _lock:
        _overrides.clear()
        _overrides.update({k: str(v).strip() for k, v in vals.items()
                           if k in KEYS and str(v or "").strip()})


def get(key: str) -> str:
    """The admin override for ``key`` (empty string when unset → caller uses its built-in default)."""
    with _lock:
        return _overrides.get(key, "")


def all_overrides() -> dict[str, str]:
    with _lock:
        return dict(_overrides)


# The audiobook library is stored on its OWN path (separate from ebooks/comics — universal best
# practice: different file sizes + folder-per-book layout). Kept as a plain AppSetting (read only at
# fetch/import time, not a hot path) rather than in the cached app-dir map above.
_AUDIOBOOK_KEY = "audiobook_library_path"


def audiobook_path(db) -> str:
    """The admin-set audiobook library path ('' when unset → caller derives a default)."""
    row = db.get(AppSetting, _AUDIOBOOK_KEY)
    return (row.value or "").strip() if (row and isinstance(row.value, str)) else ""


def set_audiobook_path(db, path: str | None) -> str:
    """Set (or clear, with blank) the audiobook library path. Returns the stored value."""
    val = (path or "").strip()
    row = db.get(AppSetting, _AUDIOBOOK_KEY)
    if val:
        if row is None:
            db.add(AppSetting(key=_AUDIOBOOK_KEY, value=val))
        else:
            row.value = val
    elif row is not None:
        db.delete(row)
    db.commit()
    return val


def update(db, patch: dict[str, str | None]) -> dict[str, str]:
    """Set/clear path overrides. A blank/None value clears the override (reverts to the default).
    Persists the row and refreshes the cache. Returns the new override map."""
    row = db.get(AppSetting, _KEY)
    cur = dict(row.value) if (row and isinstance(row.value, dict)) else {}
    for k in KEYS:
        if k in patch:
            v = (patch[k] or "").strip()
            if v:
                cur[k] = v
            else:
                cur.pop(k, None)
    if row is None:
        db.add(AppSetting(key=_KEY, value=cur))
    else:
        row.value = cur
    db.commit()
    with _lock:
        _overrides.clear()
        _overrides.update({k: v for k, v in cur.items() if k in KEYS and v})
    return dict(_overrides)
