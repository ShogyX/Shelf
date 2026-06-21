"""Series detection + selective acquisition.

Many books belong to a series/trilogy. Given a catalog book, detect its series and enumerate the
sibling volumes (ordered), so the UI can offer "fetch the whole series" or a custom selection. The
chosen volumes are acquired through the normal route priority (web hook / manager / usenet pipeline).

Series data comes from Open Library's ``series`` field (keyless, broad). When a book has no series we
return nothing — callers show a graceful "no series found".
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time

import httpx
from .. import telemetry
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CatalogWork
from .book_catalog import OPENLIBRARY, _UA, _hc_token, _ol_cover
from .extract import authors_compatible, norm_title

log = logging.getLogger("shelf.series")

_OL_FIELDS = "key,title,author_name,first_publish_year,cover_i,series,readinglog_count"
_TIMEOUT = 20.0
SERIES_ACQUIRE_CAP = 30   # max volumes acquired in one request (bounds latency + grabs)
# Cache the cross-API series enumeration per title (DB status is re-annotated fresh each call), so
# repeat "View Series" clicks are instant instead of re-hitting Hardcover/OL/GB.
_SERIES_CACHE: dict[str, tuple[float, str | None, str | None, list]] = {}  # ckey → (ts, name, series_id, books)
_SERIES_TTL = 3600.0
_SERIES_CACHE_MAX = 2048   # bound: one entry per distinct queried title would grow unbounded
# The in-memory cache is lost on restart, re-running the ~5-call enumeration for every title. Series
# membership is STABLE, so the resolved enumeration is also persisted onto the catalog row's
# extra["series_members"] (wall-clock stamped) and reused after restart for a long TTL (14B).
_SERIES_PERSIST_TTL = 14 * 24 * 3600.0  # 14 days — re-enumerate only occasionally for new volumes

# Per-call "did a provider lookup hit a TRANSIENT failure?" flag. The series providers swallow
# network/5xx/timeout errors and return empty, which is indistinguishable from a genuine "no series"
# — and a genuine negative gets cached durably for 14 days. Without this, one Hardcover/OL blip would
# hide a real series for two weeks (even after the API recovers). The name-resolution helpers run
# directly awaited in detect_series's task (no gather between), so a ContextVar set inside them is
# visible here; detect_series resets it per call and refuses to durably cache a negative when set.
_series_transient: contextvars.ContextVar[bool] = contextvars.ContextVar("series_transient",
                                                                         default=False)


def _mark_transient() -> None:
    _series_transient.set(True)


def _is_transient_status(code: int) -> bool:
    """A 5xx or 429 is a retry-worthy blip; a 200/404/4xx is a definitive answer."""
    return code >= 500 or code == 429


def _series_cache_put(key: str, value: tuple[float, str | None, str | None, list]) -> None:
    """Store with a size bound — the TTL gates freshness but never evicts, so a long-running
    process viewing many series would grow the dict forever. Sweep expired entries at the cap,
    then drop the oldest if still over."""
    _SERIES_CACHE[key] = value
    if len(_SERIES_CACHE) > _SERIES_CACHE_MAX:
        now = time.monotonic()
        for k in [k for k, (ts, _n, _s, _b) in _SERIES_CACHE.items() if now - ts > _SERIES_TTL]:
            _SERIES_CACHE.pop(k, None)
        while len(_SERIES_CACHE) > _SERIES_CACHE_MAX:
            _SERIES_CACHE.pop(next(iter(_SERIES_CACHE)), None)


# A trailing volume number on a series label: "Mistborn (1)" / "Discworld #8" / "Wheel of Time, 4".
_SERIES_NUM_RE = re.compile(r"[\s,#:(\[]+(\d{1,3})\s*[)\]]?\s*$")


def parse_series_label(raw: str | None) -> tuple[str | None, int | None]:
    """Split a raw OL series label into (name, position)."""
    if not raw:
        return (None, None)
    s = str(raw).strip()
    m = _SERIES_NUM_RE.search(s)
    pos = int(m.group(1)) if m else None
    name = (_SERIES_NUM_RE.sub("", s).strip(" -–—,:#([") if m else s).strip()
    return (name or None, pos)


async def _ol_query(client: httpx.AsyncClient, q: str, *, limit: int) -> list[dict]:
    # Host is the fixed OPENLIBRARY constant; the user query rides as a structured param (httpx encodes
    # it) so it can NEVER influence the host/path — closes py/partial-ssrf, which can't see that quoting.
    url = f"{OPENLIBRARY}/search.json"
    try:
        r = await client.get(url, params={"q": q, "fields": _OL_FIELDS, "limit": limit},
                             headers={"Accept": "application/json", "User-Agent": _UA})
    except httpx.HTTPError as exc:
        log.info("series OL query failed: %s", exc)
        _mark_transient()
        return []
    if r.status_code != 200:
        if _is_transient_status(r.status_code):
            _mark_transient()
        return []
    return (r.json() or {}).get("docs", []) or []


def _series_from_title(title: str) -> str | None:
    """Best-effort series name from a title's shape: 'Series: Subtitle' or 'Title (Series, #N)'.
    (Open Library's series field is usually null, so this catches the common formatting.)"""
    t = (title or "").strip()
    m = re.search(r"\(([^)]+?)(?:[\s,#]+\d{1,3})?\)\s*$", t)  # 'Title (Series #2)'
    if m:
        cand = m.group(1).strip(" ,#")
        if len(cand) >= 3 and not cand.isdigit():
            return cand
    if ":" in t:  # 'Series: Subtitle'
        head = t.split(":", 1)[0].strip()
        if len(head.split()) <= 5 and len(head) >= 3:
            return head
    return None


def _valid_series_name(name: str | None) -> str | None:
    """Reject boxset/omnibus 'names' (a comma-list of every volume) and other junk."""
    if not name:
        return None
    n = name.strip()
    if len(n) > 45 or "," in n or "&" in n or "/" in n:
        return None
    return n or None


async def _confirms_series(client: httpx.AsyncClient, name: str) -> bool:
    """True if Open Library knows `name` as a real multi-volume series (≥2 members)."""
    probe = await _ol_query(client, f'series:"{name}"', limit=4)
    return len([p for p in probe if p.get("title")]) >= 2


async def _gb_author_volumes(client: httpx.AsyncClient, q: str, key: str) -> list[dict]:
    """Fetch + parse a Google Books ``/volumes`` query into OL-doc-shaped dicts (with ``subtitle`` +
    ``position``). The shared fetch/parse body, factored out of ``_gb_series`` so author-enumeration
    can reuse it with an ``inauthor:`` query (the series path keeps its exact prior query). Best-effort:
    [] on any error; transient failures flag ``_series_transient``."""
    from ..integrations.metadata import _gb_year
    from .book_catalog import GOOGLE_BOOKS_API

    params = {"q": q, "maxResults": 40, "printType": "books"}
    if key:
        params["key"] = key
    try:
        r = await client.get(f"{GOOGLE_BOOKS_API}/volumes", params=params,
                             headers={"Accept": "application/json", "User-Agent": _UA})
    except httpx.HTTPError as exc:
        log.info("series GB query failed: %s", exc)
        _mark_transient()
        return []
    if r.status_code != 200:
        if _is_transient_status(r.status_code):
            _mark_transient()
        return []
    out: list[dict] = []
    for it in (r.json() or {}).get("items", []) or []:
        vi = it.get("volumeInfo") or {}
        title = (vi.get("title") or "").strip()
        if not title:
            continue
        si = vi.get("seriesInfo") or {}
        bdn = si.get("bookDisplayNumber")
        pos = int(bdn) if (bdn and str(bdn).isdigit()) else None
        out.append({
            "title": title,
            "subtitle": (vi.get("subtitle") or "").strip(),
            "author_name": vi.get("authors") or [],
            "first_publish_year": _gb_year(vi.get("publishedDate")),
            "cover_i": None, "series": None, "position": pos,
            "key": "gb:" + (it.get("id") or ""),
        })
    return out


async def _gb_series(client: httpx.AsyncClient, name: str, author: str | None,
                     key: str) -> list[dict]:
    """Enumerate a series from Google Books — which often tags a volume's SUBTITLE with the series
    ('Warmage: Book Two of the Spellmonger Series') even when the title doesn't contain it, catching
    disjoint-title volumes Open Library's series filter misses. Returns OL-doc-shaped dicts (with
    ``subtitle`` + ``position``). Best-effort: [] on any error. Author-gated by the caller."""
    q = f'inauthor:"{author}"' if author else f'intitle:"{name}"'
    return await _gb_author_volumes(client, q, key)


async def _series_name_for(client: httpx.AsyncClient, cw: CatalogWork) -> str | None:
    """The book's series name. Gather candidates (stored OL series field, title shape, the title
    itself, OL probe), then return the first one OL CONFIRMS is a real multi-volume series — so a
    first-volume title like 'Spellmonger'/'Dune' is recognized while garbage candidates are dropped."""
    candidates: list[str] = []
    for cand in (
        parse_series_label((cw.extra or {}).get("series"))[0],
        _series_from_title(cw.title),
        cw.title,
    ):
        v = _valid_series_name(cand)
        if v:
            candidates.append(v)
    # OL probe: a near-title doc that carries a series label.
    docs = await _ol_query(client, f"{cw.title} {cw.author or ''}".strip(), limit=5)
    tq = set(norm_title(cw.title).split())
    for d in docs:
        if not d.get("series"):
            continue
        dt = set(norm_title(d.get("title") or "").split())
        if tq and dt and len(tq & dt) / len(tq | dt) >= 0.6:
            v = _valid_series_name(parse_series_label((d.get("series") or [None])[0])[0])
            if v:
                candidates.append(v)

    seen: set[str] = set()
    for c in candidates:
        cl = c.lower()
        if cl in seen:
            continue
        seen.add(cl)
        if await _confirms_series(client, c):
            return c
    return None


# Phrase-based so we drop real bundles/omnibus without nuking legit volumes whose title merely
# contains a word like "Game" (e.g. "A Game of Thrones") or "Complete".
_BUNDLE_RE = re.compile(
    r"\b(saga|omnibus|box ?set|boxed set|sampler|companion|anthology|tetralogy|coffret|"
    r"complete (series|collection|saga)|\d+[\s-]*book(s)?|book\s*\d+\s*[-–]\s*\d+)\b"
    r"|\bcollection\b|\bcollected\b|\btrilogy\b|\[\d+\s*/\s*\d+\]", re.I,
)


# Hardcover exposes authoritative series membership (book_series with positions) — the best source
# for ENUMERATING a series completely, including disjoint-title volumes OL/GB miss.
_HC_SERIES_SEARCH = (
    'query($q:String!,$n:Int!){ search(query:$q, query_type:"Series", per_page:$n, page:1)'
    "{ results } }"
)
_HC_SERIES_BOOKS = (
    "query($id:Int!){ series(where:{id:{_eq:$id}}){ id name "
    "book_series(order_by:[{position:asc}], where:{book:{canonical_id:{_is_null:true}, "
    "is_partial_book:{_eq:false}}, compilation:{_eq:false}}){ position "
    "book{ id title release_year contributions{ author{ name } } } } } }"
)


async def _hc_graphql(client: httpx.AsyncClient, token: str, query: str, variables: dict) -> dict:
    from ..integrations.metadata import HARDCOVER_API, _hc_norm_token
    try:
        r = await client.post(
            HARDCOVER_API, json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {_hc_norm_token(token)}",
                     "Accept": "application/json", "User-Agent": _UA},
        )
    except httpx.HTTPError as exc:
        log.info("hardcover series query failed: %s", exc)
        _mark_transient()
        return {}
    if r.status_code != 200:
        if _is_transient_status(r.status_code):
            _mark_transient()
        return {}
    data = r.json() or {}
    if data.get("errors"):
        log.info("hardcover series gql error: %s", (data["errors"] or [{}])[0].get("message"))
        return {}
    return data.get("data") or {}


async def _hc_series_lookup(client: httpx.AsyncClient, token: str, name: str,
                            author: str | None) -> tuple[str | None, str | None, list[dict]]:
    """Resolve a series on Hardcover by name (author-gated) and enumerate its member books with
    positions. Returns (canonical_series_name, canonical_series_id "hc:<id>", [member dicts]) or
    (None, None, [])."""
    if not token or not name:
        return (None, None, [])
    from ..integrations.metadata import _hc_hits
    data = await _hc_graphql(client, token, _HC_SERIES_SEARCH, {"q": name, "n": 5})
    want = norm_title(name)
    wset = set(want.split())
    # Several series can share a name (e.g. an empty audiobook-narrator "Spellmonger" vs the real
    # 31-book one). Rank by name match, then by how many books the series actually has.
    best, best_key = None, (0.0, -1)
    for h in _hc_hits(data):
        hn = h.get("name") or h.get("title") or ""
        hid = h.get("id")
        if not hn or hid is None:
            continue
        anames = h.get("author_names") or []
        compat = authors_compatible(author, ", ".join(a for a in anames if a))
        if author and anames and not compat:
            continue
        hset = set(norm_title(hn).split())
        score = (len(wset & hset) / len(wset | hset)) if (wset | hset) else 0.0
        subset = bool(wset) and (wset <= hset or hset <= wset)
        if subset:
            score = max(score, 0.9)
        elif not (author and anames and compat):
            # A merely-overlapping series name (neither exact nor subset) is only trustworthy when
            # the AUTHOR corroborates it — otherwise a shared word would wrongly match (e.g. the
            # web-novel 'Against the Gods' onto a 'God Against the Gods' series).
            continue
        bc = int(h.get("primary_books_count") or h.get("books_count") or 0)
        key = (round(score, 3), bc)
        if score >= 0.5 and key > best_key:
            best, best_key = h, key
    if best is None:
        return (None, None, [])
    try:
        sid = int(best.get("id"))
    except (TypeError, ValueError):
        return (None, None, [])
    data2 = await _hc_graphql(client, token, _HC_SERIES_BOOKS, {"id": sid})
    rows = data2.get("series") or []
    if not rows:
        return (None, None, [])
    s = rows[0]
    docs: list[dict] = []
    for bs in s.get("book_series") or []:
        b = bs.get("book") or {}
        title = (b.get("title") or "").strip()
        if not title:
            continue
        authors = [c["author"]["name"] for c in (b.get("contributions") or [])
                   if isinstance(c, dict) and (c.get("author") or {}).get("name")]
        docs.append({
            "title": title, "author_name": authors, "first_publish_year": b.get("release_year"),
            "position": bs.get("position"), "series": None, "cover_i": None,
            "key": f"hc:{b.get('id')}",
        })
    return (s.get("name") or name, f"hc:{sid}", docs)


async def detect_series(db: Session, cw: CatalogWork) -> dict:
    """Detect `cw`'s series and enumerate its volumes (ordered). Returns {series, books:[...]}.
    Each book: title, author, year, position, cover_url, ref (OL key), catalog_id, hooked_work_id.

    Membership is established from several sources, merged + deduped:
      * Hardcover ``book_series`` — AUTHORITATIVE positional membership (the most complete source,
        incl. disjoint-title volumes), when a Hardcover token is configured;
      * the OL ``series:"<name>"`` filter (authoritative);
      * OL author+title-contains and Google Books series-subtitle (for series whose volume titles
        share the name, e.g. Dune / Harry Potter).
    Bundles/omnibus and wrong-author hits are dropped. Hardcover can also CONFIRM a series that OL
    doesn't index, so this works as a standard for all works when a token is present."""
    from . import book_catalog
    _series_transient.set(False)   # reset the per-call transient-failure flag
    hc_token = _hc_token(db)
    stored = (cw.extra or {}).get("series") if isinstance(cw.extra, dict) else None
    ckey = norm_title(stored or cw.title or "")

    # Cache the (slow) cross-API enumeration per title; DB status (catalog_id/hooked) is re-annotated
    # fresh each call so it stays current.
    cached = _SERIES_CACHE.get(ckey)
    if cached and (time.monotonic() - cached[0] < _SERIES_TTL):
        return _annotate(db, cached[1], [dict(b) for b in cached[3]], cached[2])
    # Process cache missed (or restarted) → try the PERSISTED enumeration on the row before paying
    # for the ~5-call cross-API lookup again (14B). Re-warm the in-memory cache on a hit.
    persisted = _persisted_series_members(cw)
    if persisted is not None:
        name_p, sid_p, books_p = persisted
        _series_cache_put(ckey, (time.monotonic(), name_p, sid_p, [dict(b) for b in books_p]))
        return _annotate(db, name_p, [dict(b) for b in books_p], sid_p)

    async with telemetry.instrument("metadata", timeout=_TIMEOUT, follow_redirects=True) as client:
        # Hardcover first — fast + authoritative membership (incl. disjoint titles). Prefer the
        # stored series name, else the title.
        hc_name, hc_series_id, hc_docs = await _hc_series_lookup(
            client, hc_token, stored or cw.title, cw.author)
        name = hc_name
        # Only fall back to the (slower) OL series confirmation when Hardcover found nothing.
        if not name:
            name = await _series_name_for(client, cw)
        if not name:
            # Only cache a GENUINE negative. If a provider hit a transient failure (network/5xx/
            # timeout) while resolving the name, "no series" is unreliable — don't cache it at all
            # (especially not the 14-day durable record), so the next call re-resolves once the API
            # recovers instead of hiding a real series for two weeks.
            if not _series_transient.get():
                _series_cache_put(ckey, (time.monotonic(), None, None, []))
                _persist_series_members(db, cw, None, None, [])   # durable negative survives restart (14B)
            else:
                log.info("series lookup for cw=%s inconclusive (transient provider failure) — "
                         "not caching the negative", getattr(cw, "id", "?"))
            return {"series": None, "series_id": None, "books": []}
        want = norm_title(name)
        wset = set(want.split())
        # Stable canonical series id: Hardcover's when it resolved the series, else a deterministic
        # name slug. Lets dedup/ownership key on identity rather than the free-text name (S-DUP-2/3).
        series_id = hc_series_id or (f"name:{want}" if want else None)
        # OL / GB supplements — concurrent + time-bounded so they can't stall the response (Hardcover
        # already supplied the bulk). Any that error or time out are simply skipped.
        # S-DUP-4: each supplement runs in its OWN gather task, so the _series_transient ContextVar it
        # sets isn't visible to this parent context. Read the flag INSIDE the task (where the set IS
        # visible) and return it alongside the docs, so a transient supplement failure — which may have
        # dropped volumes — can gate the durable cache below.
        async def _olf():
            return await _ol_query(client, f'series:"{name}"', limit=40), _series_transient.get()

        async def _ola():
            return await _ol_query(client, f"{cw.author or ''} {name}".strip(), limit=40), _series_transient.get()

        async def _gb():
            return await _gb_series(client, name, cw.author, book_catalog._gb_key(db)), _series_transient.get()

        try:
            results = await asyncio.wait_for(
                asyncio.gather(_olf(), _ola(), _gb(), return_exceptions=True), timeout=4.0)
            parsed = [r if isinstance(r, tuple) else ([], True) for r in results]  # errored task = incomplete
        except asyncio.TimeoutError:
            parsed = [([], True), ([], True), ([], True)]   # timed-out supplements may have dropped volumes
        by_filter, by_author, gb_docs = (p[0] if isinstance(p[0], list) else [] for p in parsed)
        # OR the supplement flags with the parent flag (Hardcover name+docs resolution ran directly
        # awaited, so its transient mark IS in this context). Any True → roster may be incomplete.
        sup_transient = _series_transient.get() or any(p[1] for p in parsed)

    found: dict[str, dict] = {}

    def _consider(d: dict, trusted: bool) -> None:
        title = (d.get("title") or "").strip()
        if not title or _BUNDLE_RE.search(title):
            return
        nk = norm_title(title)
        if not nk or nk in found:
            return
        sname, pos = parse_series_label((d.get("series") or [None])[0])
        if pos is None and d.get("position"):
            pos = d["position"]
        # Match the series name against title AND subtitle — GB tags disjoint-title volumes there.
        ntoks = set(norm_title(f"{title} {d.get('subtitle') or ''}").split())
        is_member = trusted or (sname and norm_title(sname) == want) or (wset and wset <= ntoks)
        if not is_member:
            return
        authors = ", ".join(d.get("author_name") or []) or None
        if cw.author and not authors_compatible(cw.author, authors):
            return
        found[nk] = {
            "title": title, "author": authors, "year": d.get("first_publish_year"),
            "position": pos, "cover_url": _ol_cover(d.get("cover_i")),
            "ref": d.get("key"), "norm_key": nk,
        }

    for d in hc_docs:        # Hardcover book_series: authoritative membership (incl. disjoint titles)
        _consider(d, True)
    for d in by_filter:      # OL series: filter asserts membership
        _consider(d, True)
    for d in by_author:      # require series-match or title-contains
        _consider(d, False)
    for d in gb_docs:        # GB: series in title/subtitle (catches disjoint-title volumes), author-gated
        _consider(d, False)

    books_raw = sorted(
        found.values(),
        # Unknown-position volumes sort LAST: use +inf, not 999 — a long series (manga/webnovel with
        # >999 entries, which this app explicitly targets) has legitimate positions >=999 that would
        # otherwise interleave with the "unknown" sentinel and scramble the order.
        key=lambda b: (b["position"] if b["position"] is not None else float("inf"),
                       b["year"] or 9999, b["title"]),
    )
    # S-DUP-4: only cache/persist a roster we believe is COMPLETE. A transient supplement (or
    # Hardcover) failure may have dropped volumes; caching that partial set durably (14 days) makes the
    # missing volumes resurface as "new" later → duplicate fetches. On transient, return the best-effort
    # roster for display but don't cache it — the next call re-resolves once the API recovers (mirrors
    # the negative path above). ponytail: conservative — skips caching even if Hardcover alone was
    # complete; the cost is a re-resolution, never wrong data.
    if not sup_transient:
        _series_cache_put(ckey, (time.monotonic(), name, series_id, [dict(b) for b in books_raw]))
        # Populate the DB with the whole series (tag rows + owned works with name + position + id) so
        # the series is durably recorded, not just computed on the fly. Self-isolating (savepoint).
        _persist_series(db, name, series_id, books_raw)
        _persist_series_members(db, cw, name, series_id, books_raw)   # survive-restart cache (14B)
    return _annotate(db, name, books_raw, series_id)


def _persisted_series_members(cw: CatalogWork) -> tuple[str | None, str | None, list[dict]] | None:
    """Return ``(name, series_id, books)`` from a FRESH persisted enumeration on the row, or None when
    absent or stale. Lets a restart skip the ~5-call cross-API lookup since series membership is stable."""
    rec = (cw.extra or {}).get("series_members") if isinstance(cw.extra, dict) else None
    if not isinstance(rec, dict):
        return None
    ts = rec.get("ts")
    if not isinstance(ts, (int, float)) or (time.time() - ts) >= _SERIES_PERSIST_TTL:
        return None
    books = rec.get("books")
    if not isinstance(books, list):
        return None
    return rec.get("name"), rec.get("series_id"), [dict(b) for b in books if isinstance(b, dict)]


def _persist_series_members(db: Session, cw: CatalogWork, name: str | None,
                            series_id: str | None, books: list[dict]) -> None:
    """Stamp the resolved enumeration onto the row's extra so a repeat detect_series after a restart
    re-annotates from the DB without the cross-API calls (14B). Wall-clock stamped (survives restart,
    unlike the monotonic in-memory cache). Best-effort, isolated in a savepoint."""
    try:
        extra = dict(cw.extra or {})
        extra["series_members"] = {"ts": time.time(), "name": name, "series_id": series_id,
                                   "books": books}
        with db.begin_nested():
            cw.extra = extra
        db.commit()
    except Exception:  # noqa: BLE001 — cache persistence must never fail the lookup
        log.exception("persisting series_members for cw=%s failed", getattr(cw, "id", "?"))


def _best_row_for(db: Session, nk: str):
    """The catalog row for a norm_key, PREFERRING one already hooked to a library work — so a duplicate
    unhooked listing row can't mask a volume the user actually owns (the old ``.limit(1)`` bug)."""
    return db.scalar(
        select(CatalogWork).where(CatalogWork.norm_key == nk)
        .order_by(CatalogWork.hooked_work_id.is_(None))  # hooked rows (False=0) first
        .limit(1)
    )


def _persist_series(db: Session, name: str | None, series_id: str | None, books: list[dict]) -> None:
    """Durably record the enumerated series. For each volume: tag its catalog row(s) with
    ``extra.series`` + ``extra.series_position`` (creating a lightweight listing row for a volume we
    don't have yet, so the whole series is represented), and stamp ``series`` + ``series_position``
    onto any OWNED work hooked to that volume — so the library can group + order the books and show
    what's missing. Idempotent: commits only when something actually changed.

    The writes run in a SAVEPOINT so a failure rolls back only the series mutations — it can never
    disturb pending work in the (sometimes shared) request session. This function is fully
    synchronous (no awaits), so on a single event loop it runs atomically with no interleaving."""
    if not name:
        return
    try:
        with db.begin_nested():
            changed = _apply_series_rows(db, name, series_id, books)
    except Exception:  # noqa: BLE001 — persistence is best-effort; never fail the lookup
        log.exception("persisting series %r failed", name)
        return
    if changed:
        db.commit()


def _apply_series_rows(db: Session, name: str, series_id: str | None, books: list[dict]) -> bool:
    """Apply the series tags/rows (inside the caller's savepoint). Returns whether anything changed."""
    from ..models import Work
    changed = False
    for b in books:
        nk = b.get("norm_key")
        if not nk:
            continue
        pos = b.get("position")
        rows = db.scalars(
            select(CatalogWork).where(CatalogWork.norm_key == nk)
            .order_by(CatalogWork.hooked_work_id.is_(None))
        ).all()
        if not rows:
            ref = b.get("ref") or ""
            # Deterministic work_url so a repeat enumeration finds the same row instead of minting
            # another. For a refless volume, SCOPE the synthetic URL by the series name + position +
            # title-key: keying on nk alone would collide two genuinely-different works that normalize
            # to the same nk (the over-merge this catalog otherwise fights) onto one constant URL.
            if ref:
                url = f"https://hardcover.app/{ref}"
            else:
                skey = norm_title(name) or "series"
                vkey = f"{nk}-{pos}" if pos is not None else nk
                url = f"https://hardcover.app/series/{skey}/{vkey}"
            ext = {"series": name, "series_position": pos, "listing_only": True}
            if series_id:
                ext["series_id"] = series_id
            row = CatalogWork(
                provider="hardcover", provider_ref=(ref or None), domain="hardcover.app",
                work_url=url, norm_key=nk, title=b.get("title") or nk, author=b.get("author"),
                cover_url=b.get("cover_url"), extra=ext,
            )
            db.add(row)
            rows = [row]
            changed = True
        for row in rows:
            ex = dict(row.extra or {})
            if (ex.get("series") != name or (pos is not None and ex.get("series_position") != pos)
                    or (series_id and ex.get("series_id") != series_id)):
                ex["series"] = name
                if pos is not None:
                    ex["series_position"] = pos
                if series_id:
                    ex["series_id"] = series_id
                row.extra = ex
                changed = True
            if row.hooked_work_id:  # tag the owned work so the library groups + orders it
                w = db.get(Work, row.hooked_work_id)
                if w is not None and (w.series != name
                                      or (pos is not None and w.series_position != pos)
                                      or (series_id and w.series_id != series_id)):
                    w.series = name
                    if pos is not None:
                        w.series_position = pos
                    if series_id:
                        w.series_id = series_id
                    changed = True
    return changed


def _annotate(db: Session, name: str | None, books: list[dict], series_id: str | None = None) -> dict:
    """Add fresh DB status (catalog_id / hooked_work_id) to enumerated series books; drop norm_key."""
    from ..models import Work
    out: list[dict] = []
    for raw in books:
        b = dict(raw)
        nk = b.pop("norm_key", None)
        row = _best_row_for(db, nk) if nk else None
        b["catalog_id"] = row.id if row else None
        hooked = row.hooked_work_id if row else None
        # Fallback: an owned work tagged with this series + position (covers a work whose hooked catalog
        # row's norm_key drifted from the canonical volume title — S-DUP-3). Prefer the stable series_id
        # so a renamed/same-named series can't mis-match; fall back to the name when there's no id.
        if hooked is None and b.get("position") is not None:
            probe = (Work.series_id == series_id) if series_id else (Work.series == name if name else None)
            if probe is not None:
                hooked = db.scalar(
                    select(Work.id).where(probe, Work.series_position == b["position"]).limit(1)
                )
        b["hooked_work_id"] = hooked
        out.append(b)
    return {"series": name, "series_id": series_id, "books": out}


async def enumerate_author(db: Session, author_name: str) -> list[dict]:
    """Enumerate an author's books (Wave E follow-author / request-all-by-author). Assembles candidates
    from Google Books ``inauthor:"<name>"`` + Open Library author search, dedups by ``norm_title``, drops
    bundles/omnibus (``_BUNDLE_RE``), and gates every candidate through ``authors_compatible`` so a
    same-title wrong-author edition can't slip in. Returns ``_annotate``-d ``detect_series``-shaped dicts
    (each with catalog_id / hooked_work_id) so owned/requested titles can be skipped by the caller.

    Best-effort + time-bounded like ``detect_series``; resets the per-call ``_series_transient`` flag so
    the caller (follow_tick) can tell a transient provider blip apart from a genuine empty result."""
    from . import book_catalog
    name = (author_name or "").strip()
    if not name:
        return []
    _series_transient.set(False)
    async with telemetry.instrument("metadata", timeout=_TIMEOUT, follow_redirects=True) as client:
        async def _gb():
            return await _gb_author_volumes(client, f'inauthor:"{name}"', book_catalog._gb_key(db))

        async def _ol():
            return await _ol_query(client, f'author:"{name}"', limit=40)

        try:
            results = await asyncio.wait_for(
                asyncio.gather(_gb(), _ol(), return_exceptions=True), timeout=4.0)
        except asyncio.TimeoutError:
            results = ([], [])
        gb_docs, ol_docs = (r if isinstance(r, list) else [] for r in results)

    found: dict[str, dict] = {}
    for d in [*gb_docs, *ol_docs]:
        title = (d.get("title") or "").strip()
        if not title or _BUNDLE_RE.search(title):
            continue
        nk = norm_title(title)
        if not nk or nk in found:
            continue
        authors = ", ".join(d.get("author_name") or []) or None
        if not authors_compatible(name, authors):
            continue
        found[nk] = {
            "title": title, "author": authors, "year": d.get("first_publish_year"),
            "position": d.get("position"), "cover_url": _ol_cover(d.get("cover_i")),
            "ref": d.get("key"), "norm_key": nk,
        }
    books_raw = sorted(found.values(), key=lambda b: (b["year"] or 9999, b["title"]))
    # No series name (this is an author roster), so _annotate's series-position fallback is inert.
    return _annotate(db, None, books_raw)["books"]


def _pick_by_author(db: Session, nk: str, author: str | None) -> CatalogWork | None:
    """An unhooked catalog row for `nk` whose author matches — so a same-title wrong-author edition
    (e.g. a study guide) can't be grabbed as the series volume."""
    rows = db.scalars(
        select(CatalogWork).where(CatalogWork.norm_key == nk, CatalogWork.hooked_work_id.is_(None))
    ).all()
    if not rows:
        return None
    if author:
        for r in rows:
            if authors_compatible(author, r.author):
                return r
        return None  # rows exist but none match the author → resolve fresh
    return rows[0]


async def _resolve_book_row(db: Session, title: str, author: str | None) -> CatalogWork | None:
    """Find (or live-resolve) a not-yet-hooked, author-matching catalog row for a series volume."""
    from . import book_catalog
    nk = norm_title(title)
    row = _pick_by_author(db, nk, author)
    if row is not None:
        return row
    try:
        await book_catalog.resolve_live(db, f"{title} {author or ''}".strip())
    except Exception:  # noqa: BLE001
        return None
    return _pick_by_author(db, nk, author)


async def acquire_series(db: Session, cw: CatalogWork, *, refs: list[str] | None, want_all: bool,
                         user_id: int, shelf_id: int | None = None,
                         origin: str | None = None) -> list[dict]:
    """Acquire selected series volumes (by OL ref, or all) via the user's route priority.

    ``origin`` (e.g. "series", set by the auto-series hook) tags the ledger rows opened for sibling
    volumes so the Wanted page can show them as 'from series …'; left None for a manual grab."""
    from . import acquire as acq
    detected = await detect_series(db, cw)
    chosen = [b for b in detected["books"] if want_all or (refs and b["ref"] in refs)]
    # Bound the synchronous work so a huge series can't time out the request or flood the grabber.
    capped = chosen[:SERIES_ACQUIRE_CAP]
    priority = acq.user_priority(db, _user(db, user_id))
    results: list[dict] = []
    if len(chosen) > len(capped):
        log.info("series acquire capped at %s of %s volumes", len(capped), len(chosen))
    chosen = capped
    for b in chosen:
        if b.get("hooked_work_id"):
            results.append({"title": b["title"], "ref": b["ref"], "status": "in_library"})
            continue
        row = await _resolve_book_row(db, b["title"], b["author"])
        if row is None:
            results.append({"title": b["title"], "ref": b["ref"], "status": "unresolved"})
            continue
        # Tell the matcher this is a known series volume so the release name's series + position +
        # full author aren't treated as "unexplained" (which would block the auto-grab). ``volume``
        # lets the matcher reject a release that declares a DIFFERENT volume number (a substring-title
        # volume like "Spellmonger" #1 must not match "Spellmonger 06 - Journeymage").
        ctx = {"series": detected["series"], "author_full": b["author"], "allow_volume": True,
               "volume": b.get("position")}
        if origin:  # tag the ledger row this sibling opens (auto-series hook → "from series …")
            ctx["origin"] = origin
            ctx["origin_detail"] = detected["series"]
        try:
            res = await acq.acquire(db, row, user_id=user_id, priority=priority, shelf_id=shelf_id,
                                    context=ctx)
        except Exception:  # noqa: BLE001 — log the detail; don't surface str(exc) to the client
            log.exception("series volume acquire failed: %r", b.get("title"))
            results.append({"title": b["title"], "ref": b["ref"], "status": "error",
                            "detail": "acquisition failed"})
            continue
        results.append({"title": b["title"], "ref": b["ref"], **res})
    return results


async def acquire_author(db: Session, author_name: str, *, refs: list[str] | None, want_all: bool,
                         user_id: int, shelf_id: int | None = None, origin: str | None = None,
                         origin_detail: str | None = None) -> list[dict]:
    """Acquire an author's books (Wave E request-all-by-author / follow-author auto-fetch). The same
    shape as ``acquire_series`` with ONE swap: ``enumerate_author`` instead of ``detect_series``. Reuses
    the cap, ``_resolve_book_row``, the owned-skip, and the ``acq.acquire`` loop. ``origin`` (e.g.
    "following") tags the ledger rows; ``origin_detail`` defaults to the author name."""
    from . import acquire as acq
    books = await enumerate_author(db, author_name)
    chosen = [b for b in books if want_all or (refs and b["ref"] in refs)]
    # Bound the synchronous work so a prolific author can't time out the request or flood the grabber.
    capped = chosen[:SERIES_ACQUIRE_CAP]
    priority = acq.user_priority(db, _user(db, user_id))
    results: list[dict] = []
    if len(chosen) > len(capped):
        log.info("author acquire capped at %s of %s books", len(capped), len(chosen))
    chosen = capped
    for b in chosen:
        if b.get("hooked_work_id"):
            results.append({"title": b["title"], "ref": b["ref"], "status": "in_library"})
            continue
        row = await _resolve_book_row(db, b["title"], b["author"])
        if row is None:
            results.append({"title": b["title"], "ref": b["ref"], "status": "unresolved"})
            continue
        ctx = {"author_full": b["author"]}
        if origin:  # tag the ledger row this opens (follow-author → "following")
            ctx["origin"] = origin
            ctx["origin_detail"] = origin_detail or author_name
        try:
            res = await acq.acquire(db, row, user_id=user_id, priority=priority, shelf_id=shelf_id,
                                    context=ctx)
        except Exception:  # noqa: BLE001 — log the detail; don't surface str(exc) to the client
            log.exception("author book acquire failed: %r", b.get("title"))
            results.append({"title": b["title"], "ref": b["ref"], "status": "error",
                            "detail": "acquisition failed"})
            continue
        results.append({"title": b["title"], "ref": b["ref"], **res})
    return results


def _user(db: Session, user_id: int):
    from ..models import User
    return db.get(User, user_id)
