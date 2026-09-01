"""Instagram downloader: URL validation, video info lookup, and download.

Video only -- posts/reels/IGTV and stories, not photos. yt-dlp's Instagram
extractor silently drops photo-only items rather than raising anything
distinguishable (see PHOTO_SUPPORT.md, next to this file, for what it would
take to add them later).

Exposes `matches(url)` and `main(argv)` — the contract the core dispatcher
in `downstream/registry.py` looks for on every site module under
`Downloaders/`.
"""

from .cli import main
from .download import DownloadCancelledError, DownloadResult, download_video
from .info import InfoError, VideoInfo, get_video_info
from .urls import LinkType, classify_url, is_valid_instagram_url as matches

__all__ = [
    "matches",
    "main",
    "LinkType",
    "classify_url",
    "VideoInfo",
    "get_video_info",
    "InfoError",
    "DownloadResult",
    "download_video",
    "DownloadCancelledError",
]
