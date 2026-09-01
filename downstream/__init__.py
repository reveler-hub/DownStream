"""DownStream: multi-site downloader core. Dispatches URLs to the right
site-specific downloader under `Downloaders/`."""

from .cli import main
from .registry import find_site, site_names

__all__ = ["main", "find_site", "site_names"]
