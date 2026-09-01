"""Recognize and classify Twitch links (VOD, clip, channel/live)."""

from __future__ import annotations

import re
from enum import Enum


class LinkType(Enum):
    VOD = "vod"
    CLIP = "clip"
    CHANNEL = "channel"
    UNKNOWN = "unknown"


_VOD_RE = re.compile(
    r"^https?://(?:www\.)?twitch\.tv/(?:videos|[\w]+/video)/(?P<id>\d+)/?(?:\?.*)?$"
)
_CLIP_CHANNEL_RE = re.compile(
    r"^https?://(?:www\.)?twitch\.tv/[\w]+/clip/(?P<slug>[\w-]+)/?(?:\?.*)?$"
)
_CLIP_SHORT_RE = re.compile(
    r"^https?://clips\.twitch\.tv/(?P<slug>[\w-]+)/?(?:\?.*)?$"
)
_CHANNEL_RE = re.compile(
    # Twitch usernames are normally 4-25 chars, but a handful of very early
    # accounts (e.g. "xqc") are grandfathered in at 3 chars.
    r"^https?://(?:www\.)?twitch\.tv/(?P<channel>[a-zA-Z0-9_]{3,25})/?(?:\?.*)?$"
)

# Reserved path segments that are never channel names, so a URL like
# twitch.tv/videos (no id) or twitch.tv/directory doesn't get misread as a channel.
_RESERVED_FIRST_SEGMENT = {
    "videos",
    "directory",
    "downloads",
    "settings",
    "subscriptions",
    "wallet",
    "friends",
    "drops",
    "inventory",
    "p",
}


def classify_url(url: str) -> LinkType:
    """Return the kind of Twitch link `url` is, or LinkType.UNKNOWN if unrecognized."""
    url = url.strip()

    if _VOD_RE.match(url):
        return LinkType.VOD
    if _CLIP_CHANNEL_RE.match(url) or _CLIP_SHORT_RE.match(url):
        return LinkType.CLIP

    match = _CHANNEL_RE.match(url)
    if match and match.group("channel").lower() not in _RESERVED_FIRST_SEGMENT:
        return LinkType.CHANNEL

    return LinkType.UNKNOWN


def is_valid_twitch_url(url: str) -> bool:
    """True if `url` looks like a VOD, clip, or channel link this tool understands."""
    return classify_url(url) is not LinkType.UNKNOWN


def extract_id(url: str) -> str | None:
    """Pull the VOD id, clip slug, or channel name out of a recognized Twitch URL."""
    url = url.strip()

    match = _VOD_RE.match(url)
    if match:
        return match.group("id")

    match = _CLIP_CHANNEL_RE.match(url) or _CLIP_SHORT_RE.match(url)
    if match:
        return match.group("slug")

    match = _CHANNEL_RE.match(url)
    if match and match.group("channel").lower() not in _RESERVED_FIRST_SEGMENT:
        return match.group("channel")

    return None
