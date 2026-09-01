"""Download a Twitch VOD, clip, or live stream via yt-dlp."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yt_dlp
from yt_dlp.utils import sanitize_filename

from ..download_helpers import finalize_filename, make_progress_hook, salvage_partial_file, watch_for_cancel, watch_progress
from . import hls
from .info import InfoError, clean_quality_label, get_chapters, get_muted_ranges, parse_cookies_from_browser, probe
from .urls import LinkType


class DownloadCancelledError(InfoError):
    """Raised when `cancel_event` is set before or during a download."""


DEFAULT_OUTPUT_TEMPLATE = "%(uploader)s/%(title)s [%(id)s].%(ext)s"

# yt-dlp appends the current wall-clock minute to a live stream's title on
# every extraction (its own core behavior, YoutubeDL.py, not Twitch-specific)
# so with DEFAULT_OUTPUT_TEMPLATE, retrying a channel download after an
# interruption gets a brand new filename instead of resuming the old one —
# orphaning the first attempt instead of continuing it. Omitting %(title)s
# for live channel downloads keeps the filename stable across retries so
# yt-dlp's own resume logic can find and continue the existing file.
DEFAULT_LIVE_OUTPUT_TEMPLATE = "%(uploader)s/%(uploader)s live [%(id)s].%(ext)s"

# Quality selectors that mean something to yt-dlp itself rather than
# naming one of the stream's own formats, so they're passed through as-is.
_PASSTHROUGH_QUALITIES = {"best", "worst"}


@dataclass
class DownloadResult:
    path: Path
    link_type: LinkType
    muted_ranges: list[tuple[float, float]]


def _resolve_quality(quality: str, formats: list[dict]) -> str:
    """Map a quality argument to the format_id yt-dlp actually needs.

    Accepts either a raw format_id (e.g. "1080p60__source_") or the
    cleaned label `info`'s `Quality:` line prints (e.g. "1080p") — the
    latter so a value copy-pasted from `info`'s output just works. Falls
    back to passing `quality` through unchanged if nothing matches, which
    covers "best"/"worst" and lets yt-dlp's own error message surface for
    a genuinely bad value.
    """
    if quality in _PASSTHROUGH_QUALITIES:
        return quality

    for f in formats or []:
        format_id = f.get("format_id")
        if not format_id or f.get("ext") == "mhtml":
            continue
        if format_id == quality or clean_quality_label(format_id) == quality:
            return format_id

    return quality


def _select_format(quality: str, formats: list[dict]) -> dict:
    """Like `_resolve_quality`, but returns the format dict itself (for its playlist URL)."""
    video_formats = [
        f for f in formats or [] if f.get("format_id") and f.get("ext") != "mhtml" and f.get("vcodec") not in (None, "none")
    ]
    if not video_formats:
        raise InfoError("No downloadable video formats found")

    if quality == "best":
        return video_formats[-1]
    if quality == "worst":
        return video_formats[0]

    for f in video_formats:
        format_id = f["format_id"]
        if format_id == quality or clean_quality_label(format_id) == quality:
            return f

    raise InfoError(f"Quality {quality!r} not found")


def download_video(
    url: str,
    output_dir: str = ".",
    quality: str = "best",
    concurrent_fragments: int = 4,
    output_template: str | None = None,
    cookies: str | None = None,
    cookies_from_browser: str | None = None,
    chapter: int | None = None,
    progress_hook: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> DownloadResult:
    """Validate `url` and download it, returning the path to the resulting file.

    `cookies` is a path to a Netscape-format cookies.txt file for a
    logged-in Twitch session, needed for sub-only VODs. `cookies_from_browser`
    reads cookies directly from an installed browser instead of a file --
    see `info.parse_cookies_from_browser` for the `BROWSER[+KEYRING][:PROFILE][::CONTAINER]`
    syntax it accepts. `chapter` is a
    1-based index into the VOD's chapter list (as shown by `info`'s
    `Chapters:` section) to download just that chapter instead of the whole
    VOD; only valid for VOD links. `output_template` defaults to
    DEFAULT_OUTPUT_TEMPLATE, or DEFAULT_LIVE_OUTPUT_TEMPLATE for a channel
    link, unless explicitly overridden. `progress_hook`, if given, is called
    with a dict on every progress update — yt-dlp's own hook shape
    (`status`, `downloaded_bytes`, `total_bytes`, etc.) for the normal path,
    or a coarser `{"status": "fetching_segments"|"encoding"}` for
    `--chapter`, which has no native yt-dlp progress reporting of its own.
    `cancel_event`, if given, is checked between fragments/segments (and
    right before the download starts); setting it stops the download and
    raises DownloadCancelledError instead of returning a result. Raises
    InfoError if the link isn't recognized, the chapter index is out of
    range, or yt-dlp can't download it (offline channel, deleted VOD/clip,
    network error, etc).
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
        #
        # For a channel link specifically, yt-dlp's own `title` field is
        # just a synthesized "<channel> (live) <timestamp>" placeholder,
        # not the streamer's actual broadcast title -- confirmed directly:
        # a real live channel's `title` was "dota2ti (live) 2026-08-23
        # 01:38" while `description` held the real title, "[EN] BoomBoys
        # vs. Team Spirit - The International 2026 - Lower Bracket
        # Semifinal". VODs/clips already have a real `title`.
        display_title = probe_data.get("title")
        if link_type is LinkType.CHANNEL and probe_data.get("description"):
            display_title = probe_data["description"]
        progress_hook({"title": display_title})

    resolved_quality = _resolve_quality(quality, probe_data.get("formats"))
    muted_ranges = get_muted_ranges(probe_data.get("formats")) if link_type is LinkType.VOD else []

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
        # instantly without ever actually connecting to the stream.
        # Confirmed directly: a real second attempt against an already-
        # downloaded channel returned "done" in ~6s with zero bytes
        # transferred, while a clean target genuinely captured live data.
        # Each live channel download is its own distinct capture and
        # should always overwrite, unlike a VOD/clip (left at yt-dlp's
        # default there -- identical content, so skipping an unwanted
        # re-download is the sensible default).
        ydl_opts["overwrites"] = True
    if cookies:
        ydl_opts["cookiefile"] = cookies
    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = parse_cookies_from_browser(cookies_from_browser)
    if progress_hook is not None or cancel_event is not None:
        ydl_opts["progress_hooks"] = [make_progress_hook(cancel_event, progress_hook)]

    if chapter is not None:
        if link_type is not LinkType.VOD:
            raise InfoError("--chapter only applies to VOD links")
        chapters = get_chapters(probe_data)
        if not 1 <= chapter <= len(chapters):
            raise InfoError(f"Chapter {chapter} does not exist; this VOD has {len(chapters)} chapter(s)")
        selected = chapters[chapter - 1]

        fmt = _select_format(resolved_quality, probe_data.get("formats"))
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            final_path = Path(ydl.prepare_filename(probe_data))

        # prepare_filename() only knows about the whole VOD's metadata, so
        # two different --chapter downloads of the same VOD would otherwise
        # compute the identical final_path and silently overwrite one
        # another — fold the chapter into the filename to keep them apart.
        # This also gives the resumable cache dir below a name that's
        # unique per chapter, not just per VOD. The video id (also from
        # prepare_filename()) is dropped here rather than left in -- the
        # chapter number + title already disambiguate on their own, so
        # there's nothing for it to add.
        video_id = probe_data.get("id")
        clean_stem = final_path.stem.replace(f" [{video_id}]", "") if video_id else final_path.stem
        chapter_suffix = sanitize_filename(selected.title or f"chapter {chapter}")
        final_path = final_path.with_name(f"{clean_stem} - {chapter_suffix} [ch{chapter}]{final_path.suffix}")
        cache_dir = final_path.parent / f".{final_path.stem}.segments"

        try:
            hls.fetch_chapter(
                fmt["url"],
                selected.start,
                selected.end,
                final_path,
                max_workers=concurrent_fragments,
                progress_hook=progress_hook,
                cancel_event=cancel_event,
                cache_dir=cache_dir,
            )
        except hls.Cancelled as exc:
            raise DownloadCancelledError(str(exc)) from exc
        except Exception as exc:
            raise InfoError(f"Could not download chapter {chapter} of {url!r}: {exc}") from exc

        return DownloadResult(path=final_path, link_type=link_type, muted_ranges=muted_ranges)

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
    return DownloadResult(path=Path(final_path), link_type=link_type, muted_ranges=muted_ranges)
