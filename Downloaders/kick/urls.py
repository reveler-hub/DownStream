"""Recognize and classify Kick links (VOD, clip, channel/live)."""

from __future__ import annotations

import re
from enum import Enum


class LinkType(Enum):
    VOD = "vod"
    CLIP = "clip"
    CHANNEL = "channel"
    UNKNOWN = "unknown"


_VOD_RE = re.compile(
    r"^https?://(?:www\.)?kick\.com/(?P<channel>[\w-]+)/videos/"
    r"(?P<id>[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12})/?(?:\?.*)?$",
    re.IGNORECASE,
)
_CLIP_PATH_RE = re.compile(
    r"^https?://(?:www\.)?kick\.com/(?P<channel>[\w-]+)/clips/(?P<slug>clip_[\w-]+)/?(?:\?.*)?$", re.IGNORECASE
)
_CLIP_QUERY_RE = re.compile(
    r"^https?://(?:www\.)?kick\.com/(?P<channel>[\w-]+)/?\?(?:[^#]*&)?clip=(?P<slug>clip_[\w-]+)", re.IGNORECASE
)
_CHANNEL_RE = re.compile(r"^https?://(?:www\.)?kick\.com/(?P<channel>[\w-]+)/?(?:\?.*)?$", re.IGNORECASE)

# Reserved path segments that are never channel names, mirroring yt-dlp's own
# kick:live extractor's negative lookahead for the same URL shape.
_RESERVED_FIRST_SEGMENT = {"video", "videos", "categories", "search", "auth", "clips"}


def classify_url(url: str) -> LinkType:
    """Return the kind of Kick link `url` is, or LinkType.UNKNOWN if unrecognized."""
    url = url.strip()

    if _VOD_RE.match(url):
        return LinkType.VOD
    if _CLIP_PATH_RE.match(url) or _CLIP_QUERY_RE.match(url):
        return LinkType.CLIP

    match = _CHANNEL_RE.match(url)
    if match and match.group("channel").lower() not in _RESERVED_FIRST_SEGMENT:
        return LinkType.CHANNEL

    return LinkType.UNKNOWN


def is_valid_kick_url(url: str) -> bool:
    """True if `url` looks like a VOD, clip, or channel link this tool understands."""
    return classify_url(url) is not LinkType.UNKNOWN


def extract_id(url: str) -> str | None:
    """Pull the VOD id, clip slug, or channel name out of a recognized Kick URL."""
    url = url.strip()

    match = _VOD_RE.match(url)
    if match:
        return match.group("id")

    match = _CLIP_PATH_RE.match(url) or _CLIP_QUERY_RE.match(url)
    if match:
        return match.group("slug")

    match = _CHANNEL_RE.match(url)
    if match and match.group("channel").lower() not in _RESERVED_FIRST_SEGMENT:
        return match.group("channel")

    return None
