"""Local folder adapter (Stage: local path mapping).

A watched directory mapped as a reading-media source. Like local_import it does
no network I/O and is not crawl-driven — works are created/updated by the folder
sync + watchdog observer (see app/ingestion/local_folder.py). This class exists so
the source shows up in the registry with the right compliance posture.
"""
from __future__ import annotations

from ..base import ComplianceDeclaration, SourceAdapter, registry


@registry.register
class LocalFolderAdapter(SourceAdapter):
    key = "local_folder"
    display_name = "Local folder (watched)"
    description = (
        "Map a local directory of EPUB / TXT / Markdown / PDF / CBZ / CBR files. "
        "Shelf imports each file and watches the folder for additions and changes."
    )
    base_url = None
    enabled = True
    compliance = ComplianceDeclaration(
        license_basis="user-owned",
        tos_permitted_default=True,
        robots_respected=False,
        needs_attestation=False,
        min_request_interval_s=0.0,
        max_daily_requests=1000000,
    )
