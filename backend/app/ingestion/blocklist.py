"""Operator blocklist: URLs / domains barred from the index.

When the operator removes broken content, the offending URL (and optionally its whole domain)
is recorded here so the crawler won't re-discover/re-catalog it and it can't be hooked again.
Matching is cheap: exact normalized-URL match, or a domain match covering every URL on it.
"""
from __future__ import annotations

from urllib.parse import urldefrag, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import IndexBlock


def normalize_url(url: str) -> str:
    return urldefrag((url or "").strip())[0].rstrip("/")


def domain_of(url: str) -> str:
    netloc = (urlparse(url or "").netloc or "").lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def is_blocked(db: Session, url: str) -> bool:
    """True if this URL (or its domain) is on the operator blocklist."""
    if not url:
        return False
    nurl = normalize_url(url)
    dom = domain_of(url)
    row = db.scalar(
        select(IndexBlock.id).where(
            ((IndexBlock.scope == "url") & (IndexBlock.value == nurl))
            | ((IndexBlock.scope == "domain") & (IndexBlock.value == dom))
        ).limit(1)
    )
    return row is not None


def blocked_sets(db: Session) -> tuple[set[str], set[str]]:
    """Load the whole blocklist once as (url_set, domain_set) for cheap in-memory checks in
    hot loops (e.g. the crawl frontier), avoiding a query per candidate URL."""
    urls: set[str] = set()
    domains: set[str] = set()
    for scope, value in db.execute(select(IndexBlock.scope, IndexBlock.value)).all():
        (domains if scope == "domain" else urls).add(value)
    return urls, domains


def is_blocked_in(url: str, urls: set[str], domains: set[str]) -> bool:
    """In-memory variant of :func:`is_blocked` using pre-loaded sets from :func:`blocked_sets`."""
    if not url:
        return False
    return normalize_url(url) in urls or domain_of(url) in domains


def add_block(db: Session, *, scope: str, value: str, reason: str | None = None,
              title: str | None = None) -> IndexBlock:
    """Add (or fetch existing) a block. scope is 'url' or 'domain'; value is normalized."""
    scope = "domain" if scope == "domain" else "url"
    norm = domain_of(value) if scope == "domain" else normalize_url(value)
    existing = db.scalar(
        select(IndexBlock).where(IndexBlock.scope == scope, IndexBlock.value == norm)
    )
    if existing is not None:
        return existing
    block = IndexBlock(scope=scope, value=norm, reason=(reason or None), title=(title or None))
    db.add(block)
    db.commit()
    db.refresh(block)
    return block
