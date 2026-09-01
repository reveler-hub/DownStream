"""Fetch metadata for a Twitch link via yt-dlp, without downloading anything."""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Any

import yt_dlp
from yt_dlp.cookies import SUPPORTED_BROWSERS, SUPPORTED_KEYRINGS

from .urls import LinkType, classify_url

# yt-dlp's Twitch live-channel extractor uses a persisted GraphQL query
# (StreamMetadata) that doesn't select a viewer-count field at all, so
# `view_count` always comes back None for live channel links. Twitch's
# public web client hits the same GQL endpoint unauthenticated with this
# client ID for its own UI, so we do the same for a small supplemental
# query to fill in `viewersCount`. This is a public, widely-used ID (also
# relied on by yt-dlp itself, streamlink, chatterino, etc.), not a secret.
_TWITCH_PUBLIC_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_TWITCH_GQL_URL = "https://gql.twitch.tv/gql"


class InfoError(Exception):
    """Raised when a link is unrecognized or yt-dlp can't extract info for it."""


class ChannelOfflineError(InfoError):
    """Raised for a channel link whose streamer isn't currently live."""


@dataclass
class Chapter:
    start: float
    end: float
    title: str


@dataclass
class VideoInfo:
    id: str
    title: str
    uploader: str | None
    duration_seconds: float | None
    view_count: int | None
    upload_date: str | None  # YYYYMMDD, as yt-dlp reports it
    is_live: bool
    thumbnail: str | None
    webpage_url: str
    link_type: LinkType
    qualities: list[str]
    muted_ranges: list[tuple[float, float]]
    chapters: list[Chapter]
    raw: dict[str, Any]

    @classmethod
    def from_ydl_dict(cls, data: dict[str, Any], link_type: LinkType) -> "VideoInfo":
        # For a channel link specifically, yt-dlp's own `title` field is
        # just a synthesized "<channel> (live) <timestamp>" placeholder,
        # not the streamer's actual broadcast title -- that's in
        # `description` instead (confirmed directly: a real live channel's
        # title was "dota2ti (live) 2026-08-23 01:38" while description
        # held "[EN] BoomBoys vs. Team Spirit - The International 2026 -
        # Lower Bracket Semifinal", the same title Twitch's own page
        # shows). VODs/clips already have a real title. Same fix as
        # download.py's identical title-selection logic.
        title = data.get("title") or ""
        if link_type is LinkType.CHANNEL and data.get("description"):
            title = data["description"]
        return cls(
            id=str(data.get("id")),
            title=title,
            uploader=data.get("uploader"),
            duration_seconds=data.get("duration"),
            view_count=data.get("view_count"),
            upload_date=data.get("upload_date"),
            is_live=bool(data.get("is_live")),
            thumbnail=data.get("thumbnail"),
            webpage_url=data.get("webpage_url") or "",
            link_type=link_type,
            qualities=_extract_qualities(data.get("formats")),
            # DMCA-style muting and chapters only apply to stored VODs, not
            # clips or live streams, so skip both for those.
            muted_ranges=get_muted_ranges(data.get("formats")) if link_type is LinkType.VOD else [],
            chapters=get_chapters(data) if link_type is LinkType.VOD else [],
            raw=data,
        )


def clean_quality_label(format_id: str) -> str:
    """Human-friendly quality label, e.g. "1080p60__source_" -> "1080p", "audio_only" -> "Audio Only".

    Frame rate isn't a meaningful choice for viewers picking a download
    quality, and Twitch's "__source_" suffix on live streams is an
    implementation detail, so both are stripped for display.
    """
    if format_id.lower() == "audio_only":
        return "Audio Only"
    label = re.sub(r"__source_?$", "", format_id)
    label = re.sub(r"^(\d+p)\d+$", r"\1", label)
    return label


def _extract_qualities(formats: list[dict[str, Any]] | None) -> list[str]:
    """Cleaned, deduplicated quality labels, lowest to highest as yt-dlp orders them.

    Storyboard entries (thumbnail-preview sprites, not playable video) are
    filtered out.
    """
    labels: list[str] = []
    for f in formats or []:
        format_id = f.get("format_id")
        if not format_id or f.get("ext") == "mhtml":
            continue
        label = clean_quality_label(format_id)
        if label not in labels:
            labels.append(label)
    return labels


def get_chapters(data: dict[str, Any]) -> list[Chapter]:
    """Convert yt-dlp's chapter dicts (game/category changes during the VOD) to `Chapter`s.

    The still-in-progress final chapter of a VOD recorded while its channel
    was still live can come back with end_time == start_time (0 duration);
    treat that as running to the end of the video instead.
    """
    duration = data.get("duration")
    chapters = []
    for c in data.get("chapters") or []:
        start = c.get("start_time")
        end = c.get("end_time")
        if start is None or end is None:
            continue
        if end <= start and duration is not None:
            end = duration
        chapters.append(Chapter(start=start, end=end, title=c.get("title") or ""))
    return chapters


def _parse_muted_ranges(playlist_text: str) -> list[tuple[float, float]]:
    """Find (start_seconds, end_seconds) ranges of muted segments in an HLS media playlist.

    Twitch marks DMCA-muted VOD segments with a "-muted" suffix on the
    segment filename (e.g. "12-muted.ts" instead of "12.ts"). Consecutive
    muted segments are merged into a single range.
    """
    ranges: list[tuple[float, float]] = []
    current_time = 0.0
    pending_duration = 0.0
    run_start: float | None = None

    for line in playlist_text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            pending_duration = float(line.removeprefix("#EXTINF:").split(",")[0])
        elif line and not line.startswith("#"):
            if "-muted" in line:
                if run_start is None:
                    run_start = current_time
            elif run_start is not None:
                ranges.append((run_start, current_time))
                run_start = None
            current_time += pending_duration

    if run_start is not None:
        ranges.append((run_start, current_time))

    return ranges


def get_muted_ranges(formats: list[dict[str, Any]] | None) -> list[tuple[float, float]]:
    """Best-effort check of a VOD's highest-quality HLS playlist for muted segment ranges.

    Muting is applied uniformly across every quality rendition of the same
    recording, so checking one is enough. Returns an empty list on any
    failure (network error, unexpected playlist shape) rather than raising,
    since this is a supplemental check, not core to fetching video info.
    """
    video_formats = [
        f for f in (formats or []) if f.get("format_id") and f.get("ext") != "mhtml" and f.get("vcodec") not in (None, "none")
    ]
    if not video_formats:
        return []

    playlist_url = video_formats[-1].get("url")
    if not playlist_url:
        return []

    try:
        with urllib.request.urlopen(playlist_url, timeout=10) as response:
            playlist_text = response.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    return _parse_muted_ranges(playlist_text)


def _fetch_live_viewer_count(channel_login: str) -> int | None:
    """Best-effort lookup of a channel's current concurrent viewer count.

    Returns None on any failure (network error, channel offline, unexpected
    response shape) rather than raising, since this is a supplemental
    enrichment and shouldn't block the rest of the info lookup.
    """
    # json.dumps gives a properly quoted/escaped GraphQL string literal.
    query = f"query {{ user(login: {json.dumps(channel_login)}) {{ stream {{ viewersCount }} }} }}"
    body = json.dumps({"query": query}).encode("utf-8")
    request = urllib.request.Request(
        _TWITCH_GQL_URL,
        data=body,
        headers={
            "Client-ID": _TWITCH_PUBLIC_CLIENT_ID,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
        stream = payload["data"]["user"]["stream"]
        return int(stream["viewersCount"])
    except Exception:
        return None


def _is_user_not_live(exc: yt_dlp.utils.DownloadError) -> bool:
    """True if `exc` was caused by yt-dlp's Twitch extractor hitting an offline channel.

    yt-dlp wraps the original extractor exception in `exc.exc_info`; checking
    its type is more robust than matching on yt-dlp's error message text,
    which is an internal detail that could change between versions.
    """
    exc_info = getattr(exc, "exc_info", None)
    return bool(exc_info) and isinstance(exc_info[1], yt_dlp.utils.UserNotLive)


def get_latest_vod_url(channel_login: str) -> str | None:
    """Return the URL of `channel_login`'s most recent VOD, or None if they have none."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "playlistend": 1,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(f"https://www.twitch.tv/{channel_login}/videos", download=False)
    except yt_dlp.utils.DownloadError:
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
    that works there works here too. Lets a real logged-in browser supply
    cookies directly with nothing to export -- particularly relevant for
    YouTube's module, where cookies are close to required rather than an
    edge case; here for Twitch it still only matters for sub-only VODs.
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

    `cookies` is a path to a Netscape-format cookies.txt file (the kind
    exported by browser extensions) for a logged-in Twitch session.
    `cookies_from_browser` reads cookies directly from an installed
    browser instead (see `parse_cookies_from_browser`) -- if both are
    given, both are passed to yt-dlp, matching its own permissive
    behavior. Needed for sub-only VODs, which yt-dlp can't fetch a working
    access token for otherwise. Raises InfoError if the link isn't
    recognized or yt-dlp can't extract info for it. Shared by
    `get_video_info` and `download.download_video`, since both need the
    same validated, unwrapped info dict.
    """
    link_type = classify_url(url)
    if link_type is LinkType.UNKNOWN:
        raise InfoError(f"Not a recognized Twitch URL: {url!r}")

    ydl_opts = {
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

    # A channel link with no active stream, or a clip, may resolve to a
    # playlist/entries wrapper instead of a single video dict.
    if "entries" in data:
        entries = list(data.get("entries") or [])
        if not entries:
            raise InfoError(f"No video found at {url!r} (channel may be offline)")
        data = entries[0]

    return data, link_type


def get_video_info(url: str, cookies: str | None = None, cookies_from_browser: str | None = None) -> VideoInfo:
    """Validate `url` and return its metadata. Raises InfoError if that's not possible."""
    data, link_type = probe(url, cookies=cookies, cookies_from_browser=cookies_from_browser)
    info = VideoInfo.from_ydl_dict(data, link_type)

    if link_type is LinkType.CHANNEL and info.is_live and info.view_count is None:
        channel_login = data.get("uploader_id")
        if channel_login:
            info.view_count = _fetch_live_viewer_count(channel_login)

    return info
