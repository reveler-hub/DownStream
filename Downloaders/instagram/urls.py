"""Recognize and classify Instagram links (post/reel/IGTV, story)."""

from __future__ import annotations

import re
from enum import Enum


class LinkType(Enum):
    POST = "post"
    STORY = "story"
    UNKNOWN = "unknown"


# Matches yt-dlp's own InstagramIE._VALID_URL shape: /p/, /tv/, /reel(s)/ all
# resolve to the same single-media extractor, so there's no need to tell
# them apart -- a post is a post regardless of which of the three paths
# Instagram's own UI happened to use for it.
_POST_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:p|tv|reels?)/(?P<id>[^/?#&]+)/?(?:\?.*)?$",
    re.IGNORECASE,
)

# A story link with no numeric id is a user's whole story tray (all of
# their currently-active stories); one with an id is a single story item.
# Both need an authenticated session -- yt-dlp's InstagramStoryIE reads the
# viewing user's cookies the same way its base extractor does for any
# private content, there's no separate "public" story path the way a public
# post has.
_STORY_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/stories/(?P<user>[^/?#]+)(?:/(?P<id>\d+))?/?(?:\?.*)?$",
    re.IGNORECASE,
)


def classify_url(url: str) -> LinkType:
    """Return the kind of Instagram link `url` is, or LinkType.UNKNOWN if unrecognized."""
    url = url.strip()

    if _POST_RE.match(url):
        return LinkType.POST
    if _STORY_RE.match(url):
        return LinkType.STORY

    return LinkType.UNKNOWN


def is_valid_instagram_url(url: str) -> bool:
    """True if `url` looks like a post/reel/IGTV or story link this tool understands."""
    return classify_url(url) is not LinkType.UNKNOWN


def extract_id(url: str) -> str | None:
    """Pull the post shortcode, or the story user (and item id, if present), out of a recognized link."""
    url = url.strip()

    match = _POST_RE.match(url)
    if match:
        return match.group("id")

    match = _STORY_RE.match(url)
    if match:
        return match.group("id") or match.group("user")

    return None
