"""Kick downloader: URL validation, video info lookup, and download.

Exposes `matches(url)` and `main(argv)` — the contract the core dispatcher
in `downstream/registry.py` looks for on every site module under
`Downloaders/`.
"""

from .cli import main
from .download import DownloadCancelledError, DownloadResult, download_video
from .info import ChannelOfflineError, InfoError, VideoInfo, get_video_info
from .urls import LinkType, classify_url, is_valid_kick_url as matches

__all__ = [
    "matches",
    "main",
    "LinkType",
    "classify_url",
    "VideoInfo",
    "get_video_info",
    "InfoError",
    "ChannelOfflineError",
    "DownloadResult",
    "download_video",
    "DownloadCancelledError",
]
