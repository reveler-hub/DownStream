"""Fetch metadata for a Kick link via yt-dlp, without downloading anything."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yt_dlp
from yt_dlp.cookies import SUPPORTED_BROWSERS, SUPPORTED_KEYRINGS

from .urls import LinkType, classify_url


class InfoError(Exception):
    """Raised when a link is unrecognized or yt-dlp can't extract info for it."""


class ChannelOfflineError(InfoError):
    """Raised for a channel link whose streamer isn't currently live.

    Unlike Twitch/YouTube, there's no matching "offer their latest VOD
    instead" fallback here: yt-dlp has no extractor for a Kick channel's
    VOD listing (only kick:live, kick:vod, kick:clips exist) --
    `kick.com/<channel>/videos` isn't recognized as a listing page and
    silently falls through to the live-channel extractor instead (its
    regex has no end anchor), which just fails as "not live" again rather
    than raising anything distinguishable. Reimplementing that listing
    against Kick's own undocumented API was deliberately skipped rather
    than maintaining a scraper yt-dlp itself doesn't support.
    """


@dataclass
class VideoInfo:
    id: str
    title: str
    uploader: str | None
    duration_seconds: float | None
    view_count: int | None
    upload_date: str | None
    is_live: bool
    thumbnail: str | None
    webpage_url: str
    link_type: LinkType
    qualities: list[str]
    raw: dict[str, Any]

    @classmethod
    def from_ydl_dict(cls, data: dict[str, Any], link_type: LinkType) -> "VideoInfo":
        # A live channel's viewer count comes back as concurrent_view_count
        # directly from Kick's API (unlike Twitch, no supplemental query
        # needed) -- view_count otherwise, for VODs/clips.
        view_count = data.get("concurrent_view_count") if data.get("is_live") else data.get("view_count")
        return cls(
            id=str(data.get("id")),
            title=data.get("title") or "",
            uploader=data.get("uploader"),
            duration_seconds=data.get("duration"),
            view_count=view_count,
            upload_date=data.get("upload_date"),
            is_live=bool(data.get("is_live")),
            thumbnail=data.get("thumbnail"),
            webpage_url=data.get("webpage_url") or "",
            link_type=link_type,
            qualities=_extract_qualities(data.get("formats")),
            raw=data,
        )


def clean_quality_label(fmt: dict) -> str:
    """Kick's format_ids are just a sequential index (0, 1, 2...), not
    descriptive like Twitch's -- there's nothing to clean, so the label
    comes from the format's own height instead, same approach as YouTube's
    module."""
    if fmt.get("vcodec") in (None, "none"):
        return "Audio Only"
    height = fmt.get("height")
    return f"{height}p" if height else (fmt.get("format_id") or "unknown")


def _extract_qualities(formats: list[dict[str, Any]] | None) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for f in formats or []:
        if not f.get("format_id"):
            continue
        label = clean_quality_label(f)
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def _is_user_not_live(exc: yt_dlp.utils.DownloadError) -> bool:
    exc_info = getattr(exc, "exc_info", None)
    return bool(exc_info) and isinstance(exc_info[1], yt_dlp.utils.UserNotLive)


def parse_cookies_from_browser(spec: str) -> tuple[str, str | None, str | None, str | None]:
    """Parse yt-dlp's own `BROWSER[+KEYRING][:PROFILE][::CONTAINER]` syntax
    into the tuple its `cookiesfrombrowser` option expects -- same syntax
    and behavior as yt-dlp's own `--cookies-from-browser` CLI flag (this is
    yt-dlp's own parsing regex, copied rather than reimplemented from
    scratch, since it's not exposed as a reusable function), so a value
    that works there works here too.
    """
    mobj = re.fullmatch(
        r"""(?x)
        (?P<name>[^+:]+)
        (?:\s*\+\s*(?P<keyring>[^:]+))?
        (?:\s*:\s*(?!:)(?P<profile>.+?))?
        (?:\s*::\s*(?P<container>.+))?
        """,
        spec,
    )
    if mobj is None:
        raise InfoError(f"Invalid --cookies-from-browser value: {spec!r}")

    name, keyring, profile, container = mobj.group("name", "keyring", "profile", "container")
    name = name.lower()
    if name not in SUPPORTED_BROWSERS:
        raise InfoError(
            f"Unsupported browser {name!r} for --cookies-from-browser. Supported: {', '.join(sorted(SUPPORTED_BROWSERS))}"
        )
    if keyring is not None:
        keyring = keyring.upper()
        if keyring not in SUPPORTED_KEYRINGS:
            raise InfoError(
                f"Unsupported keyring {keyring!r} for --cookies-from-browser. "
                f"Supported: {', '.join(sorted(SUPPORTED_KEYRINGS))}"
            )
    return (name, profile, keyring, container)


def _cookie_opts(cookies: str | None, cookies_from_browser: str | None) -> dict[str, Any]:
    opts: dict[str, Any] = {}
    if cookies:
        opts["cookiefile"] = cookies
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = parse_cookies_from_browser(cookies_from_browser)
    return opts


def probe(
    url: str, cookies: str | None = None, cookies_from_browser: str | None = None
) -> tuple[dict[str, Any], LinkType]:
    """Validate `url` and return yt-dlp's raw info dict for it, without downloading.

    `cookies` is a path to a Netscape-format cookies.txt file for a
    logged-in Kick session -- needed for subscriber-only VODs, the same way
    Twitch's `-c/--cookies` unlocks sub-only content: yt-dlp's Kick
    extractor reads a `session_token` cookie and sends it as a bearer
    token on every API call. `cookies_from_browser` reads cookies directly
    from an installed browser instead of a file (see
    `parse_cookies_from_browser`). Raises InfoError if the link isn't
    recognized or yt-dlp can't extract info for it. Shared by
    `get_video_info` and `download.download_video`.
    """
    link_type = classify_url(url)
    if link_type is LinkType.UNKNOWN:
        raise InfoError(f"Not a recognized Kick URL: {url!r}")

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        **_cookie_opts(cookies, cookies_from_browser),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        if _is_user_not_live(exc):
            raise ChannelOfflineError(f"{url!r} is not currently live") from exc
        raise InfoError(f"Could not fetch info for {url!r}: {exc}") from exc

    if data is None:
        raise InfoError(f"No info returned for {url!r}")
    if "entries" in data:
        entries = list(data.get("entries") or [])
        if not entries:
            raise InfoError(f"No video found at {url!r} (channel may be offline)")
        data = entries[0]

    return data, link_type


def get_video_info(url: str, cookies: str | None = None, cookies_from_browser: str | None = None) -> VideoInfo:
    """Validate `url` and return its metadata. Raises InfoError if that's not possible."""
    data, link_type = probe(url, cookies=cookies, cookies_from_browser=cookies_from_browser)
    return VideoInfo.from_ydl_dict(data, link_type)
