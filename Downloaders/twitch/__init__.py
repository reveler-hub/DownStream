"""Twitch downloader: URL validation, video info lookup, and download.

Exposes `matches(url)` and `main(argv)` — the contract the core dispatcher
in `downstream/registry.py` looks for on every site module under
`Downloaders/`.
"""

from .cli import main
from .urls import LinkType, classify_url, is_valid_twitch_url as matches
from .info import Chapter, ChannelOfflineError, VideoInfo, get_video_info, InfoError
from .download import DownloadCancelledError, DownloadResult, download_video

__all__ = [
    "matches",
    "main",
    "LinkType",
    "classify_url",
    "Chapter",
    "VideoInfo",
    "get_video_info",
    "InfoError",
    "ChannelOfflineError",
    "DownloadResult",
    "download_video",
    "DownloadCancelledError",
]
