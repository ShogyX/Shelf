"""Library-manager integrations (Readarr for books/novels, Kapowarr for comics)."""
from .base import ExternalWork, IntegrationError, RootFolder, client_for

__all__ = ["ExternalWork", "IntegrationError", "RootFolder", "client_for"]
