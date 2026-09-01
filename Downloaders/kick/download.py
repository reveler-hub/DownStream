"""Download a Kick VOD, clip, or live stream via yt-dlp."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yt_dlp

from ..download_helpers import finalize_filename, make_progress_hook, salvage_partial_file, watch_for_cancel, watch_progress
from .info import InfoError, clean_quality_label, parse_cookies_from_browser, probe
from .urls import LinkType

DEFAULT_OUTPUT_TEMPLATE = "%(uploader)s/%(title)s [%(id)s].%(ext)s"

# yt-dlp appends the current wall-clock minute to a live entry's title on
# every extraction whenever live_from_start isn't set (YoutubeDL.py's own
# core behavior, not a Kick-specific quirk -- confirmed by reading it
# directly) -- Kick's channel/live downloads don't pass live_from_start (no
# "from the start" concept here the way YouTube has), so with
# DEFAULT_OUTPUT_TEMPLATE, retrying a channel download after an
# interruption gets a brand new filename instead of resuming the old one --
# orphaning the first attempt instead of continuing it. Omitting %(title)s
# for live channel downloads keeps the filename stable across retries so
# yt-dlp's own resume logic can find and continue the existing file. Same
# fix as Twitch's identical DEFAULT_LIVE_OUTPUT_TEMPLATE, ported here since
# it was missed the first time around -- Kick channel captures go through
# the exact same yt-dlp-hands-off-to-ffmpeg live path.
DEFAULT_LIVE_OUTPUT_TEMPLATE = "%(uploader)s/%(uploader)s live [%(id)s].%(ext)s"

# Quality selectors that mean something to yt-dlp itself rather than
# naming one of the stream's own formats.
_PASSTHROUGH_QUALITIES = {"best", "worst"}


class DownloadCancelledError(InfoError):
    """Raised when `cancel_event` is set before or during a download."""


@dataclass
class DownloadResult:
    path: Path
    link_type: LinkType


def _resolve_quality(quality: str, formats: list[dict] | None) -> str:
    """Map a quality argument to the format_id yt-dlp actually needs.

    Kick's format_ids are just a sequential index (0, 1, 2...) with no
    quality info encoded in them, unlike Twitch's -- so matching happens
    against each format's clean (height-based) label instead, same idea as
    Twitch's `_resolve_quality` but keyed off the label rather than the raw
    id. Falls back to passing `quality` through unchanged if nothing
    matches, which covers "best"/"worst".
    """
    if quality in _PASSTHROUGH_QUALITIES:
        return quality

    for f in formats or []:
        format_id = f.get("format_id")
        if not format_id:
            continue
        if format_id == quality or clean_quality_label(f) == quality:
            return format_id

    return quality


def download_video(
    url: str,
    output_dir: str = ".",
    quality: str = "best",
    concurrent_fragments: int = 4,
    output_template: str | None = None,
    cookies: str | None = None,
    cookies_from_browser: str | None = None,
    progress_hook: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> DownloadResult:
    """Validate `url` and download it, returning the path to the resulting file.

    `cookies` is a path to a Netscape-format cookies.txt file for a
    logged-in Kick session, needed for sub-only VODs. `cookies_from_browser`
    reads cookies directly from an installed browser instead of a file --
    see `info.parse_cookies_from_browser` for the
    `BROWSER[+KEYRING][:PROFILE][::CONTAINER]` syntax it accepts.
    `progress_hook` and `cancel_event` behave the same as
    Twitch's/YouTube's `download_video`. Raises InfoError if the link
    isn't recognized or yt-dlp can't download it (offline channel, deleted
    VOD/clip, network error, etc); DownloadCancelledError if `cancel_event`
    stops it first.
    """
    probe_data, link_type = probe(url, cookies=cookies, cookies_from_browser=cookies_from_browser)
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelledError("Cancelled by user")

    if progress_hook is not None:
        # A live channel capture's own progress updates (download_helpers.watch_progress)
        # never carry a title -- ffmpeg has no concept of one -- so this is
        # the only chance the GUI gets to know what it's actually watching
        # rather than just the URL it was given. Merged into job.progress
        # by JobStore.update_progress rather than replaced, so it survives
        # every later, narrower update.
        progress_hook({"title": probe_data.get("title")})

    resolved_quality = _resolve_quality(quality, probe_data.get("formats"))

    if output_template is None:
        output_template = DEFAULT_LIVE_OUTPUT_TEMPLATE if link_type is LinkType.CHANNEL else DEFAULT_OUTPUT_TEMPLATE

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": resolved_quality,
        "concurrent_fragment_downloads": concurrent_fragments,
        "outtmpl": str(Path(output_dir) / output_template),
    }
    if link_type is LinkType.CHANNEL:
        # DEFAULT_LIVE_OUTPUT_TEMPLATE deliberately gives every capture of
        # the same channel the same filename (see its own docstring) --
        # without this, yt-dlp's default "skip if the target already
        # exists" behavior means a *second* download of a channel that's
        # gone live again just silently no-ops against whatever finished
        # file the first capture already left there, reporting success
        # instantly without ever actually connecting to the stream. Same
        # bug as Twitch's (see its identical comment); ported here since it
        # was missed the first time around. Each live channel download is
        # its own distinct capture and should always overwrite, unlike a
        # VOD/clip (left at yt-dlp's default there -- identical content, so
        # skipping an unwanted re-download is the sensible default).
        ydl_opts["overwrites"] = True
    if cookies:
        ydl_opts["cookiefile"] = cookies
    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = parse_cookies_from_browser(cookies_from_browser)
    if progress_hook is not None or cancel_event is not None:
        ydl_opts["progress_hooks"] = [make_progress_hook(cancel_event, progress_hook)]

    # A live channel download is the one case yt-dlp hands off to ffmpeg as
    # an external process instead of using its own fragment downloader --
    # invisible to progress_hooks (and so to the cancel check inside
    # download_helpers.make_progress_hook), which would otherwise leave a
    # cancelled live download running forever. Watch for that specifically,
    # scoped to just this call via done_event.
    done_event = threading.Event()
    expected_path: str | None = None
    if link_type is LinkType.CHANNEL:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            expected_path = ydl.prepare_filename(probe_data)
        if cancel_event is not None:
            threading.Thread(
                target=watch_for_cancel, args=(cancel_event, done_event, expected_path), daemon=True
            ).start()
        if progress_hook is not None:
            threading.Thread(
                target=watch_progress, args=(done_event, expected_path, progress_hook), daemon=True
            ).start()

    def _cancelled(message: str, from_exc: BaseException | None = None) -> DownloadCancelledError:
        # Attaches the salvaged file's path (see download_helpers.salvage_partial_file),
        # if there was anything to salvage, so callers -- server.py's job
        # handling in particular -- can still surface it even though this
        # is the cancellation path, not a successful return.
        err = DownloadCancelledError(message)
        salvaged = salvage_partial_file(expected_path) if expected_path else None
        err.path = finalize_filename(salvaged, probe_data.get("id")) if salvaged else None
        if from_exc is not None:
            raise err from from_exc
        return err

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(url, download=True)

            if data is None:
                raise InfoError(f"No info returned for {url!r}")

            if "entries" in data:
                entries = list(data.get("entries") or [])
                if not entries:
                    raise InfoError(f"No video found at {url!r} (channel may be offline)")
                data = entries[0]

            final_path = ydl.prepare_filename(data)
    except yt_dlp.utils.DownloadCancelled as exc:
        _cancelled(str(exc), exc)
    except yt_dlp.utils.DownloadError as exc:
        if cancel_event is not None and cancel_event.is_set():
            _cancelled(str(exc), exc)
        raise InfoError(f"Could not download {url!r}: {exc}") from exc
    finally:
        done_event.set()

    if cancel_event is not None and cancel_event.is_set():
        raise _cancelled("Cancelled by user")

    final_path = finalize_filename(final_path, data.get("id") or probe_data.get("id"))
    return DownloadResult(path=Path(final_path), link_type=link_type)
