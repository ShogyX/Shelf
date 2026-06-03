"""Web-index adapter (Stage: URL index).

Backs the "Index" feature. Not crawl-driven through the chapter pipeline; the
index crawler (app/ingestion/indexer.py) uses this Source purely for the polite
fetcher's per-source budget + robots posture. The operator can disable it or tune
its rate limits on the Sources page like any other source.
"""
from __future__ import annotations

from ..base import ComplianceDeclaration, SourceAdapter, registry


@registry.register
class WebIndexAdapter(SourceAdapter):
    key = "web_index"
    display_name = "Web index"
    description = (
        "Index web pages you choose into a searchable, in-app library. "
        "Obeys robots.txt and rate limits; auto-crawls within your page/depth bounds."
    )
    base_url = None
    enabled = True
    compliance = ComplianceDeclaration(
        license_basis="user-directed",
        tos_permitted_default=True,
        robots_respected=True,
        needs_attestation=False,
        # User-directed indexing of a site the operator chose: politeness comes entirely from
        # the per-request interval + adaptive backoff (it throttles down hard when a site pushes
        # back). The daily budget is therefore UNLIMITED (0) — the per-source interval is the
        # only "local budget". Operator-editable on the Sources page (set a positive cap to
        # re-impose a daily ceiling for any source).
        min_request_interval_s=2.0,
        max_daily_requests=0,
    )
