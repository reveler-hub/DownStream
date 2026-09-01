"""Fetch metadata for an Instagram link via yt-dlp, without downloading anything."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yt_dlp
from yt_dlp.cookies import SUPPORTED_BROWSERS, SUPPORTED_KEYRINGS

from .urls import LinkType, classify_url


class InfoError(Exception):
    """Raised when a link is unrecognized or yt-dlp can't extract info for it."""


@dataclass
class VideoInfo:
    id: str
    title: str
    uploader: str | None
    duration_seconds: float | None
    upload_date: str | None
    thumbnail: str | None
    webpage_url: str
    link_type: LinkType
    raw: dict[str, Any]

    @classmethod
    def from_ydl_dict(cls, data: dict[str, Any], link_type: LinkType) -> "VideoInfo":
        return cls(
            id=str(data.get("id")),
            title=data.get("title") or data.get("description") or "",
            uploader=data.get("uploader") or data.get("channel"),
            duration_seconds=data.get("duration"),
            upload_date=data.get("upload_date"),
            thumbnail=data.get("thumbnail"),
            webpage_url=data.get("webpage_url") or "",
            link_type=link_type,
            raw=data,
        )


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
    logged-in Instagram session. Unlike Twitch/Kick, this isn't just for
    unlocking extra content -- a story link (LinkType.STORY) has no public
    path at all, and even a public post can hit Instagram's login wall
    without a session. `cookies_from_browser` reads cookies directly from
    an installed browser instead of a file (see `parse_cookies_from_browser`).
    Raises InfoError if the link isn't recognized or yt-dlp can't extract
    info for it.
    """
    link_type = classify_url(url)
    if link_type is LinkType.UNKNOWN:
        raise InfoError(f"Not a recognized Instagram URL: {url!r}")

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
        raise InfoError(f"Could not fetch info for {url!r}: {exc}") from exc

    if data is None:
        raise InfoError(f"No info returned for {url!r}")
    if "entries" in data:
        entries = list(data.get("entries") or [])
        if not entries:
            raise InfoError(f"No media found at {url!r}")
        data = entries[0]

    return data, link_type


def get_video_info(url: str, cookies: str | None = None, cookies_from_browser: str | None = None) -> VideoInfo:
    """Validate `url` and return its metadata. Raises InfoError if that's not possible."""
    data, link_type = probe(url, cookies=cookies, cookies_from_browser=cookies_from_browser)
    return VideoInfo.from_ydl_dict(data, link_type)
