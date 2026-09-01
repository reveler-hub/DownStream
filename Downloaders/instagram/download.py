"""Download an Instagram post/reel/IGTV or story via yt-dlp."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yt_dlp
from yt_dlp.utils import sanitize_filename

from ..download_helpers import make_progress_hook
from .info import InfoError, parse_cookies_from_browser, probe
from .urls import LinkType, extract_id

# No %(title)s here, unlike every other site's DEFAULT_OUTPUT_TEMPLATE --
# Instagram synthesizes a "Video by <username>"/"Post by <username>" title
# whenever a post/story item has no real one of its own (confirmed by
# reading yt-dlp's own InstagramBaseIE._extract_product), and most reels
# and story items get exactly that, not a real title. _finalize_display_name
# below picks the actual display name after the fact -- the real title when
# there is one, the uploader's name otherwise -- so it never has to fight a
# boilerplate title already baked into the downloaded filename.
DEFAULT_OUTPUT_TEMPLATE = "%(uploader)s/%(uploader)s [%(id)s].%(ext)s"


def _is_boilerplate_title(title: str | None, channel: str | None) -> bool:
    """True for yt-dlp's own synthesized Instagram title, or no title at all.

    `channel` (not `uploader`) is the right field to compare against here --
    the boilerplate is built from the account's @username (`user.username`,
    yt-dlp's `channel`), not its display name (`user.full_name`, yt-dlp's
    `uploader`); confirmed directly against a real reel: `channel` was
    "samcotton", `uploader` was "Sam Cotton", and `title` was exactly "Video
    by samcotton" -- matches `channel`, not `uploader`.
    """
    if not title:
        return True
    return bool(channel) and title in (f"Video by {channel}", f"Post by {channel}")


def _finalize_display_name(path: str, entry: dict) -> str:
    """Renames yt-dlp's own `<uploader> [<id>].<ext>` output (see
    DEFAULT_OUTPUT_TEMPLATE) to a clean, human name: the item's real title
    when it has one, or just the uploader's name when it doesn't (see
    `_is_boilerplate_title`).

    Unlike download_helpers.finalize_filename's id-suffix fallback on a
    clean-name collision, this uses a plain, readable " (2)", " (3)", ...
    counter instead -- appropriate here specifically because Instagram
    routinely gives an uploader's whole run of reels/stories the exact same
    computed clean name (most have no real title at all), so relying on the
    id fallback would mean nearly every file after the first keeps an
    unreadable numeric id forever instead of a short, sensible number.
    """
    p = Path(path)
    channel = entry.get("channel")
    title = entry.get("title")
    stem = title if not _is_boilerplate_title(title, channel) else (entry.get("uploader") or channel)
    if not stem:
        return path
    stem = sanitize_filename(stem)

    candidate = p.with_name(f"{stem}{p.suffix}")
    n = 2
    while candidate.exists() and candidate.resolve() != p.resolve():
        candidate = p.with_name(f"{stem} ({n}){p.suffix}")
        n += 1
    if candidate.resolve() == p.resolve():
        return path
    try:
        return str(p.replace(candidate))
    except OSError:
        return path


class DownloadCancelledError(InfoError):
    """Raised when `cancel_event` is set before or during a download."""


@dataclass
class DownloadResult:
    paths: list[Path]
    link_type: LinkType


def download_video(
    url: str,
    output_dir: str = ".",
    output_template: str | None = None,
    cookies: str | None = None,
    cookies_from_browser: str | None = None,
    progress_hook: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> DownloadResult:
    """Validate `url` and download it, returning the path(s) to the resulting file(s).

    `cookies`/`cookies_from_browser` behave the same as Twitch's/Kick's
    `download_video` -- except here they're not optional even for a plain
    public post; see info.probe's own docstring. `progress_hook` and
    `cancel_event` behave the same as every other site's `download_video`.

    A story link with no specific item id (the tray link Instagram gives
    you when there's no per-item "copy link" option) downloads every
    *currently active* item at once -- yt-dlp's own story extractor already
    resolves that shape as a playlist, so this is one `extract_info` call,
    not a manual loop. A specific post/reel/IGTV or single story item
    downloads just that one file. Either way, only video items come back --
    yt-dlp's Instagram extractor silently drops photo-only items before
    they ever reach us (confirmed directly: a photo story item just
    vanishes from the result instead of raising anything distinguishable),
    so a photo-only post or an all-photo story tray raises InfoError here
    rather than producing a file.

    Raises InfoError if the link isn't recognized, has no video content, or
    yt-dlp can't download it; DownloadCancelledError if `cancel_event`
    stops it first.
    """
    _, link_type = probe(url, cookies=cookies, cookies_from_browser=cookies_from_browser)
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelledError("Cancelled by user")

    is_story_tray = link_type is LinkType.STORY and extract_id(url) is None

    if output_template is None:
        output_template = DEFAULT_OUTPUT_TEMPLATE

    ydl_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        # A story tray link has to stay a playlist so every currently
        # active item downloads, not just the first -- everything else
        # (a single post/reel/IGTV, or a story link with a specific item
        # id) is exactly one item, same as Twitch's/Kick's VOD/clip path.
        "noplaylist": not is_story_tray,
        "outtmpl": str(Path(output_dir) / output_template),
    }
    if cookies:
        ydl_opts["cookiefile"] = cookies
    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = parse_cookies_from_browser(cookies_from_browser)
    if progress_hook is not None or cancel_event is not None:
        ydl_opts["progress_hooks"] = [make_progress_hook(cancel_event, progress_hook)]

    # Unlike Twitch's/Kick's live-channel path, nothing here hands off to an
    # external ffmpeg process -- every Instagram download (single item or a
    # story tray's several) goes through yt-dlp's own fragment downloader,
    # so there's no salvage story to tell: a cancelled item's leftover
    # `.part` file is left as-is rather than renamed, same as Twitch's/
    # Kick's own VOD/clip path (only their CHANNEL/live case salvages).
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(url, download=True)

            if data is None:
                raise InfoError(
                    f"No video content available at {url!r} -- this may be a photo "
                    "(unsupported) or, for a story link, no longer active"
                )

            entries = data["entries"] if "entries" in data else [data]
            entries = [e for e in entries if e]
            if not entries:
                raise InfoError(f"No video content available at {url!r} (all items may be photos)")

            final_paths = [ydl.prepare_filename(e) for e in entries]
    except yt_dlp.utils.DownloadCancelled as exc:
        raise DownloadCancelledError(str(exc)) from exc
    except yt_dlp.utils.DownloadError as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError(str(exc)) from exc
        raise InfoError(f"Could not download {url!r}: {exc}") from exc

    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelledError("Cancelled by user")

    final_paths = [_finalize_display_name(p, e) for p, e in zip(final_paths, entries)]
    return DownloadResult(paths=[Path(p) for p in final_paths], link_type=link_type)
