"""A tiny in-process TTL cache for expensive read endpoints.

The catalog grouping (O(n²) cross-source clustering) and the crawl-stats aggregates are
recomputed on every poll — and the frontend polls the Index/Jobs pages every few seconds
while crawling. Under load (many hooked + indexing titles) that repeated work is what
makes the site feel slow. Caching each result for a few seconds collapses a burst of
identical polls into one computation, and pairing it with off-loop execution keeps the
async event loop responsive. Entries are namespaced so writes can drop just their slice.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

_lock = threading.Lock()
# OrderedDict so we can evict oldest entries — the catalog cache key includes the search
# query + filters + pagination, an effectively unbounded keyspace that would otherwise leak.
_store: "OrderedDict[str, tuple[float, object]]" = OrderedDict()

# Default time-to-live for cached read results (seconds). Short enough that staleness is
# never user-visible for long; long enough to absorb the frontend's poll cadence.
DEFAULT_TTL = 4.0
# Hard cap on live entries (LRU-evicted) so varied searches can't grow memory without bound.
MAX_ENTRIES = 512


def get(key: str):
    """Return the cached value for ``key`` if still fresh, else None."""
    now = time.monotonic()
    with _lock:
        entry = _store.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires <= now:
            _store.pop(key, None)
            return None
        _store.move_to_end(key)  # mark as recently used
        return value


def put(key: str, value, ttl: float = DEFAULT_TTL) -> None:
    now = time.monotonic()
    with _lock:
        _store[key] = (now + ttl, value)
        _store.move_to_end(key)
        # Drop already-expired entries opportunistically, then LRU-evict any excess.
        if len(_store) > MAX_ENTRIES:
            for k in [k for k, (exp, _) in _store.items() if exp <= now]:
                _store.pop(k, None)
            while len(_store) > MAX_ENTRIES:
                _store.popitem(last=False)


def clear(prefix: str = "") -> None:
    """Drop all entries (prefix='') or just those whose key starts with ``prefix``."""
    with _lock:
        if not prefix:
            _store.clear()
            return
        for k in [k for k in _store if k.startswith(prefix)]:
            _store.pop(k, None)
