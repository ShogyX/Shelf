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

# The catalog grids (Discover rows / Browse / facets / stats) are ~180–630ms to recompute but a ~3ms
# cache hit. They're invalidated by clear_catalog() on ANY catalog write — and a continuous crawl
# writes constantly, so without throttling the cache is almost always cold and the warm tick +
# visits keep re-paying the recompute. Coalesce those invalidations over a WIDE window: these are
# slow-moving aggregates (title/source counts, genre facets), so a couple of minutes of staleness is
# invisible, and the wider window collapses a whole crawl's write burst into ONE recompute rather
# than one every 20s. (Per-user library/stock membership is applied live, after the cache; a user
# ACTION — hook/stock/settings — uses clear_catalog(force=True), which bypasses this window.)
_CATALOG_CLEAR_COOLDOWN = 180.0
_last_catalog_clear = 0.0


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


# ARCH-H3: named entry points for the cache namespaces, so a write path invalidates by calling a
# discoverable function instead of repeating a bare string prefix that a typo could silently break
# (a wrong prefix = stale reads until the short TTL lapses). Add one here when a new namespace appears.
def clear_catalog(*, force: bool = False) -> None:
    """Invalidate cached catalog reads — call after ANY write touching catalog works/groups/hooked
    flags (the Index/Browse grids, catalog-stats/facets all key off the ``catalog`` namespace).

    THROTTLED (see _CATALOG_CLEAR_COOLDOWN): a burst of writes during a crawl coalesces into one
    invalidation, so Discover stays a warm ~3ms hit instead of paying the ~480ms recompute on every
    visit. Pass force=True to bypass (e.g. an explicit user action that must reflect immediately)."""
    global _last_catalog_clear
    if not force:
        now = time.monotonic()
        with _lock:
            if now - _last_catalog_clear < _CATALOG_CLEAR_COOLDOWN:
                return                       # coalesced into a recent clear
            _last_catalog_clear = now
    clear("catalog")                          # clear() takes _lock itself — call outside our hold


def clear_index() -> None:
    """Invalidate cached index reads (index-sites + stats). Note this also covers the ``index-sites``
    keys, since they share the ``index`` prefix."""
    clear("index")


def clear_index_sites() -> None:
    """Invalidate only the cached index-sites listings (narrower than :func:`clear_index`)."""
    clear("index-sites")
