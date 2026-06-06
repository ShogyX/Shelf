"""Library-manager integrations (Readarr for books/novels, Kapowarr for comics) plus the
acquisition pipeline (Prowlarr search + SABnzbd downloader)."""
from .base import (
    ExternalWork,
    IntegrationError,
    RootFolder,
    client_for,
    is_pipeline_kind,
)

__all__ = [
    "ExternalWork",
    "IntegrationError",
    "RootFolder",
    "client_for",
    "is_pipeline_kind",
]
