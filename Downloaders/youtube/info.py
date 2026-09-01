"""Wraps yt-dlp to fetch metadata for a validated YouTube link, without downloading media."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yt_dlp
from yt_dlp.cookies import SUPPORTED_BROWSERS, SUPPORTED_KEYRINGS

from .urls import LinkType, classify_url

# YouTube's extraction now depends on running some of the site's own JS to
# solve signature/PO-token challenges — yt-dlp-ejs supplies that, executed
# through a real JS runtime (deno; must be installed and on PATH, unlike
# ffmpeg-style bundled binaries). Needed for every request, not just
# downloads — even metadata-only lookups fail without it on current
# YouTube.
JS_RUNTIME_OPTS: dict[str, Any] = {
    "js_runtimes": {"deno": {}},
    "remote_components": ["ejs:github"],
}


class InfoError(Exception):
    """Raised when a YouTube link can't be looked up."""


class ChannelOfflineError(InfoError):
    """Raised when a channel link is requested but the channel isn't currently live."""


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
        # Like Twitch, a live video's "view_count" is total historical page
        # views, not how many people are watching right now — that's a
        # separate field.
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
    """YouTube's format_ids are opaque itags (e.g. "137"), not descriptive
    like Twitch's — there's nothing to clean, so the label comes from the
    format's own height/vcodec instead."""
    if fmt.get("vcodec") in (None, "none"):
        return "Audio Only"
    height = fmt.get("height")
    return f"{height}p" if height else (fmt.get("format_id") or "unknown")


def _extract_qualities(formats: list[dict] | None) -> list[str]:
    heights: list[int] = []
    has_audio_only = False
    for f in formats or []:
        if f.get("vcodec") in (None, "none"):
            has_audio_only = True
            continue
        height = f.get("height")
        if height and height not in heights:
            heights.append(height)
    labels = [f"{h}p" for h in sorted(heights)]
    if has_audio_only:
        labels.append("Audio Only")
    return labels


def _is_user_not_live(exc: Exception) -> bool:
    exc_info = getattr(exc, "exc_info", None)
    return bool(exc_info) and isinstance(exc_info[1], yt_dlp.utils.UserNotLive)


def get_latest_video_url(channel_url: str) -> str | None:
    """The most recent upload on a channel's Videos tab, or None if it has
    none (or the lookup fails) — used to offer a fallback when a channel
    link is requested but the channel isn't currently live."""
    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlist_items": "1"}
    base = channel_url.rstrip("/")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(f"{base}/videos", download=False)
    except Exception:
        return None
    entries = list((data or {}).get("entries") or [])
    if not entries:
        return None
    return entries[0].get("url")


def parse_cookies_from_browser(spec: str) -> tuple[str, str | None, str | None, str | None]:
    """Parse yt-dlp's own `BROWSER[+KEYRING][:PROFILE][::CONTAINER]` syntax
    into the tuple its `cookiesfrombrowser` option expects -- same syntax
    and behavior as yt-dlp's own `--cookies-from-browser` CLI flag (this is
    yt-dlp's own parsing regex, copied rather than reimplemented from
    scratch, since it's not exposed as a reusable function), so a value
    that works there works here too. Particularly relevant for YouTube:
    lets a real logged-in browser supply cookies directly, with nothing to
    export -- and cookies here are close to required, not an edge case
    (see `download.download_video`'s docstring).
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


# yt-dlp's own default User-Agent always claims to be Chrome, regardless of
# where the cookies it's sending actually came from. Sent alongside a
# Firefox-family session's cookies, that mismatch is itself a signal
# YouTube's bot-check can act on -- a real logged-in session suddenly
# presenting as a different browser. Chromium-based sources need no
# override; yt-dlp's default already matches that family. Ported from
# DownTube's own production fix for the same problem (see its
# `USER_AGENT` constant) -- may need bumping if it starts looking stale.
_UA_BY_BROWSER_FAMILY: dict[str, str] = {
    "firefox": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) Gecko/20100101 Firefox/152.0",
    "safari": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
}


def user_agent_for_cookie_browser(browser_name: str) -> str | None:
    """A User-Agent whose declared browser family matches `browser_name`
    (yt-dlp's own `--cookies-from-browser` browser name), or None when no
    override is needed."""
    return _UA_BY_BROWSER_FAMILY.get(browser_name)


def _cookie_opts(cookies: str | None, cookies_from_browser: str | None) -> dict[str, Any]:
    opts: dict[str, Any] = {}
    if cookies:
        opts["cookiefile"] = cookies
    if cookies_from_browser:
        browser = parse_cookies_from_browser(cookies_from_browser)
        opts["cookiesfrombrowser"] = browser
        ua = user_agent_for_cookie_browser(browser[0])
        if ua:
            opts["http_headers"] = {"User-Agent": ua}
    return opts


def probe(
    url: str, cookies: str | None = None, cookies_from_browser: str | None = None
) -> tuple[dict, LinkType]:
    """Validate `url`, extract its metadata, and return (info_dict, link_type).

    For a channel link, this checks the channel's `/live` URL — YouTube's
    own always-current redirect to whatever's live there, if anything.
    Shared by `get_video_info` and `download_video` so both extract exactly
    once and see identical data.
    """
    link_type = classify_url(url)
    if link_type is LinkType.UNKNOWN:
        raise InfoError(f"Not a recognized YouTube URL: {url!r}")

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        **JS_RUNTIME_OPTS,
        **_cookie_opts(cookies, cookies_from_browser),
    }

    target = url
    if link_type is LinkType.CHANNEL:
        target = url.rstrip("/")
        # A channel URL the user pasted may already point at its own
        # /live redirect -- YouTube's own canonical "whatever's live on
        # this channel right now" link, and a very natural thing to end up
        # with here given DownStream's whole live-capture feature is built
        # around exactly that use case. Appending unconditionally would
        # double it up into ".../live/live", which doesn't resolve the way
        # a plain ".../live" does.
        if not re.search(r"/live(?:\?.*)?$", target, re.IGNORECASE):
            target += "/live"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(target, download=False)
    except yt_dlp.utils.DownloadError as exc:
        if link_type is LinkType.CHANNEL and _is_user_not_live(exc):
            raise ChannelOfflineError(f"Channel is not currently live: {url!r}") from exc
        raise InfoError(f"Could not look up {url!r}: {exc}") from exc

    if data is None:
        raise InfoError(f"No info returned for {url!r}")
    if "entries" in data:
        entries = list(data.get("entries") or [])
        if not entries:
            raise InfoError(f"No video found at {url!r}")
        data = entries[0]

    return data, link_type


def get_video_info(url: str, cookies: str | None = None, cookies_from_browser: str | None = None) -> VideoInfo:
    data, link_type = probe(url, cookies=cookies, cookies_from_browser=cookies_from_browser)
    return VideoInfo.from_ydl_dict(data, link_type)
