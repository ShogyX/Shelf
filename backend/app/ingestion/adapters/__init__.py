"""Importing this package registers every built-in adapter with the global registry."""
from . import (  # noqa: F401
    generic_feed,
    gutenberg,
    local_folder,
    local_import,
    mangadex,
    memory,
    royalroad,
    standardebooks,
    web_index,
)
