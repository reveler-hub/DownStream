"""YouTube URL classification: video vs channel vs unknown."""

from __future__ import annotations

import re
from enum import Enum


class LinkType(Enum):
    VIDEO = "video"
    CHANNEL = "channel"
    UNKNOWN = "unknown"


_HOST_RE = re.compile(r"^https?://([\w-]+\.)?(youtube\.com|youtu\.be)(/|$)", re.IGNORECASE)

_VIDEO_ID = r"[\w-]{11}"
_WATCH_RE = re.compile(rf"[?&]v=({_VIDEO_ID})")
_SHORT_RE = re.compile(rf"youtu\.be/({_VIDEO_ID})", re.IGNORECASE)
_SHORTS_RE = re.compile(rf"youtube\.com/shorts/({_VIDEO_ID})", re.IGNORECASE)
_LIVE_VIDEO_RE = re.compile(rf"youtube\.com/live/({_VIDEO_ID})", re.IGNORECASE)

_HANDLE_RE = re.compile(r"youtube\.com/@([\w.-]+)", re.IGNORECASE)
_CHANNEL_ID_RE = re.compile(r"youtube\.com/channel/([\w-]+)", re.IGNORECASE)
_CUSTOM_RE = re.compile(r"youtube\.com/c/([\w-]+)", re.IGNORECASE)
_USER_RE = re.compile(r"youtube\.com/user/([\w-]+)", re.IGNORECASE)

_VIDEO_PATTERNS = (_WATCH_RE, _SHORT_RE, _SHORTS_RE, _LIVE_VIDEO_RE)
_CHANNEL_PATTERNS = (_HANDLE_RE, _CHANNEL_ID_RE, _CUSTOM_RE, _USER_RE)


def classify_url(url: str) -> LinkType:
    url = url.strip()
    if not _HOST_RE.match(url):
        return LinkType.UNKNOWN
    if any(rx.search(url) for rx in _VIDEO_PATTERNS):
        return LinkType.VIDEO
    if any(rx.search(url) for rx in _CHANNEL_PATTERNS):
        return LinkType.CHANNEL
    return LinkType.UNKNOWN


def is_valid_youtube_url(url: str) -> bool:
    return classify_url(url) is not LinkType.UNKNOWN


def extract_id(url: str) -> str | None:
    """Return the video ID for a video link, or the channel handle/ID for a
    channel link (with a leading '@' for handles, to match how they're
    written) — None for anything unrecognized."""
    for rx in _VIDEO_PATTERNS:
        m = rx.search(url)
        if m:
            return m.group(1)
    m = _HANDLE_RE.search(url)
    if m:
        return "@" + m.group(1)
    for rx in (_CHANNEL_ID_RE, _CUSTOM_RE, _USER_RE):
        m = rx.search(url)
        if m:
            return m.group(1)
    return None
