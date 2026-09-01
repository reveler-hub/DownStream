"""Download a YouTube video or live stream via yt-dlp."""

from __future__ import annotations

import json
import os
import pickle
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yt_dlp

from ..download_helpers import finalize_filename, make_progress_hook
from .info import InfoError, JS_RUNTIME_OPTS, _cookie_opts, probe
from .urls import LinkType

DEFAULT_OUTPUT_TEMPLATE = "%(uploader)s/%(title)s [%(id)s].%(ext)s"

_IS_WINDOWS = sys.platform.startswith("win")
_LIVE_WORKER_PATH = Path(__file__).parent / "_live_worker.py"


class DownloadCancelledError(InfoError):
    """Raised when `cancel_event` is set before or during a download."""


def _find_partial_downloads(output_dir: str, video_id: str) -> list[str]:
    """Every per-format `.part` temp file yt-dlp may have left behind
    under `output_dir` for this video -- matched by the video's id
    (always present in DownStream's own output template, `[%(id)s]`)
    rather than trying to predict yt-dlp's exact temp filename ourselves,
    since the final extension it actually uses isn't always what was
    requested (confirmed directly: a real video ended up '.mp4' despite
    `merge_output_format: 'mkv'`, per yt-dlp's own container-compatibility
    override) -- the id is the one thing guaranteed present and unique
    without needing to reproduce that logic.
    """
    marker = f"[{video_id}]"
    matches = []
    for root, _dirs, files in os.walk(output_dir):
        for name in files:
            if marker in name and name.endswith(".part"):
                matches.append(os.path.join(root, name))
    return sorted(matches)


def _watch_live_progress(
    done_event: threading.Event, output_dir: str, video_id: str, progress_hook: Callable[[dict], None]
) -> None:
    """Runs alongside a live capture's worker subprocess. The worker only
    ever relays yt-dlp's own progress hooks, which can go quiet for a long
    time during --live-from-start's walk-back phase (confirmed directly
    against a real 24/7 stream: 50+ seconds with zero hook calls) -- long
    enough that a job showing nothing but "Starting..." the whole time
    looks stuck even once real fragment data has actually started landing
    on disk. Polls for that directly instead of waiting on the worker's
    relay, summing whatever real per-format `.part` files currently exist
    for this video (the same discovery _salvage_split_download uses at
    cancel time) rather than any single one, since video and audio
    download as separate files.
    """
    while not done_event.wait(2):
        parts = _find_partial_downloads(output_dir, video_id)
        total = sum(os.path.getsize(p) for p in parts if os.path.exists(p))
        if total:
            progress_hook({"status": "downloading", "downloaded_bytes": total})


def _split_temp_name(part_path: str) -> tuple[str, str] | None:
    """Splits a yt-dlp per-format temp file `<base>.f<format_id>.<ext>.part`
    into `(base, ext)` -- e.g. ".../Title [id].f401.mp4.part" ->
    (".../Title [id]", "mp4"). None if it doesn't match that shape
    (defensive -- only ever act on files we're confident about)."""
    if not part_path.endswith(".part"):
        return None
    stem = part_path[: -len(".part")]
    base, dot_ext = os.path.splitext(stem)
    if not dot_ext:
        return None
    base2, dot_fid = os.path.splitext(base)
    if not (dot_fid.startswith(".f") and len(dot_fid) > 2):
        return None
    return base2, dot_ext.lstrip(".")


def _mux_partials(video_part: str, audio_part: str, final_path: str) -> bool:
    """Best-effort remux of a cancelled split download's separate video-
    only and audio-only partials into one playable file, mirroring what
    yt-dlp's own FFmpegMergerPP would have produced on a successful
    completion. `-c copy` -- no re-encoding, just repackaging whatever
    was already downloaded. Returns True if it actually produced a real
    output file."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_part, "-i", audio_part, "-c", "copy", final_path],
            capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return os.path.exists(final_path) and os.path.getsize(final_path) > 0


def _cleanup_stray(part_path: str) -> None:
    """Removes a leftover `.part` file and its `.ytdl` resume-metadata
    sidecar (yt-dlp writes one alongside every `.part` file) -- best
    effort, since these are just temp files, not anything a failure here
    should be allowed to escalate over."""
    candidates = [part_path]
    if part_path.endswith(".part"):
        candidates.append(part_path[: -len(".part")] + ".ytdl")
    for path in candidates:
        try:
            os.remove(path)
        except OSError:
            pass


def _salvage_split_download(output_dir: str, video_id: str | None) -> str | None:
    """Best-effort recovery for a cancelled split-format (separate video
    and audio, which is how YouTube serves nearly everything, live or
    not) download. Each requested format downloads to its own
    `<name>.f<format_id>.<ext>.part` temp file (yt-dlp's own convention --
    see YoutubeDL.py's process_info(), the `prepend_extension(...,
    'f{format_id}', ...)` call) -- these only get merged into the final
    single file once *every* requested format finishes, a point a
    cancellation by definition never reaches, so they'd otherwise just
    sit there under an unfamiliar extension.

    Recovers by muxing a real video partial + a real audio partial
    together via ffmpeg if both exist; otherwise keeps the single
    largest real partial alone (a silent video, or audio with no
    picture) rather than nothing at all -- its format id stays in the
    name (only ".part" is stripped) so it's clearly a lone stream, not a
    complete download. Returns None (nothing salvageable, all temp files
    cleaned up) if every partial was empty -- cancelled before any real
    data ever arrived, the common case for an early cancel.
    """
    if not video_id:
        return None
    parts = _find_partial_downloads(output_dir, video_id)
    real_parts = [p for p in parts if os.path.getsize(p) > 0]
    if not real_parts:
        for p in parts:
            _cleanup_stray(p)
        return None

    salvaged: str | None = None
    if len(real_parts) >= 2:
        # Heuristic: video is almost always the larger of the two (far
        # higher bitrate than audio-only) -- good enough for a
        # best-effort recovery, not the primary download path.
        by_size = sorted(real_parts, key=os.path.getsize, reverse=True)
        video_part, audio_part = by_size[0], by_size[1]
        split = _split_temp_name(video_part)
        if split is not None:
            base, ext = split
            final_path = f"{base}.{ext}"
            if _mux_partials(video_part, audio_part, final_path):
                salvaged = final_path

    if salvaged is None:
        best = max(real_parts, key=os.path.getsize)
        candidate = best[: -len(".part")]
        try:
            os.replace(best, candidate)
            salvaged = candidate
        except OSError:
            pass

    for p in parts:
        if os.path.exists(p) and p != salvaged:
            _cleanup_stray(p)
    return salvaged


@dataclass
class DownloadResult:
    path: Path
    link_type: LinkType


def _resolve_quality(quality: str) -> str:
    """Map a quality argument to a yt-dlp format selector string.

    Unlike Twitch's pre-muxed HLS variants, YouTube formats are usually
    split video-only/audio-only (DASH) — there's no single format_id to
    pick for "1080p", so this builds a height-capped
    bestvideo+bestaudio selector instead. Accepts a clean label from
    `info`'s `Quality:` line (e.g. "1080p", "Audio Only"). "best"/"worst"
    get the same video+audio-merging treatment rather than being passed to
    yt-dlp bare: a literal "best"/"worst" format selector requires a single
    pre-muxed format to exist, which plenty of real streams (confirmed
    against a real live channel that only offered split DASH formats) don't
    have, and yt-dlp then refuses with "Requested format is not available"
    instead of falling back to merging streams the way its own CLI default
    (`bestvideo*+bestaudio/best`) does.
    """
    if quality == "best":
        return "bestvideo+bestaudio/best"
    if quality == "worst":
        return "worstvideo+worstaudio/worst"
    if quality.strip().lower() in ("audio only", "audio", "audio_only", "audio-only"):
        return "bestaudio/best"
    height = quality[:-1] if quality.endswith("p") else quality
    if height.isdigit():
        return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
    return quality  # let yt-dlp's own error surface for a genuinely bad value


def _stop_live_worker(proc: subprocess.Popen) -> None:
    """SIGINT first (Windows: terminate) so the worker's own KeyboardInterrupt
    handler gets a chance to report back cleanly; escalates to a hard kill
    if it doesn't exit soon. Same graceful-then-forceful shape as Twitch's/
    Kick's `download_helpers.stop_ffmpeg`, just against the worker process
    directly instead of having to go hunting for a child process by
    command line."""
    try:
        if _IS_WINDOWS:
            proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _run_live_capture(
    url: str,
    ydl_opts: dict[str, Any],
    progress_hook: Callable[[dict], None] | None,
    cancel_event: threading.Event | None,
) -> str:
    """Runs a live channel's `--live-from-start` capture as a real child
    process (see `_live_worker.py` for why) instead of yt-dlp's in-process
    API, so `cancel_event` can actually stop it even during the
    unboundedly-long extraction phase a continuous stream's walk-back can
    hit. Returns the final file path; raises DownloadCancelledError if
    cancelled, InfoError on any other failure.
    """
    fd, job_path = tempfile.mkstemp(prefix="downstream-yt-live-", suffix=".pickle")
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump({"url": url, "ydl_opts": ydl_opts}, f)

        proc = subprocess.Popen(
            [sys.executable, str(_LIVE_WORKER_PATH), job_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        cancelled = False

        def watch_cancel() -> None:
            nonlocal cancelled
            if cancel_event is None:
                return
            while proc.poll() is None:
                if cancel_event.wait(0.5):
                    cancelled = True
                    _stop_live_worker(proc)
                    return

        watcher = threading.Thread(target=watch_cancel, daemon=True)
        watcher.start()

        result: dict[str, Any] = {}
        assert proc.stdout is not None
        for line in proc.stdout:
            kind, _, rest = line.strip().partition(" ")
            try:
                payload = json.loads(rest) if rest else {}
            except json.JSONDecodeError:
                continue
            if kind == "PROGRESS":
                if progress_hook is not None:
                    progress_hook(payload)
            elif kind == "RESULT":
                result = payload
            elif kind == "ERROR":
                result = {"error": payload.get("message", "unknown error")}
            elif kind == "CANCELLED":
                cancelled = True

        proc.wait()
        stderr_output = proc.stderr.read().strip() if proc.stderr else ""

        if cancelled or (cancel_event is not None and cancel_event.is_set()):
            raise DownloadCancelledError("Cancelled by user")
        if "error" in result:
            raise InfoError(f"Could not download {url!r}: {result['error']}")
        if "path" not in result:
            raise InfoError(f"Could not download {url!r}: {stderr_output or 'live capture process exited unexpectedly'}")
        return result["path"]
    finally:
        try:
            os.remove(job_path)
        except OSError:
            pass


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
    logged-in YouTube session — unlike Twitch, this is close to required
    rather than an edge case: current YouTube 403s on the actual media
    fetch without a real session, even though metadata-only lookups work
    fine either way. `cookies_from_browser` reads cookies directly from an
    installed browser instead of a file -- see
    `info.parse_cookies_from_browser` for the
    `BROWSER[+KEYRING][:PROFILE][::CONTAINER]` syntax it accepts; verified
    directly (a real download succeeded using nothing but
    `cookiesfrombrowser=('chrome', None, None, None)` against a real
    installed Chrome profile, no manual export at all). `progress_hook`
    and `cancel_event` behave the same as Twitch's `download_video`.
    Raises InfoError if the link isn't recognized or yt-dlp can't download
    it; DownloadCancelledError if `cancel_event` stops it first.
    """
    probe_data, link_type = probe(url, cookies=cookies, cookies_from_browser=cookies_from_browser)
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelledError("Cancelled by user")

    if progress_hook is not None:
        # A live channel capture's own progress updates never carry a
        # title (the live worker subprocess only ever relays yt-dlp's raw
        # fragment-progress hooks, and a continuous stream's walk-back can
        # run for a long time before the first of those even fires) -- this
        # is the earliest the GUI can know what it's actually watching
        # rather than just the URL it was given. Merged into job.progress
        # by JobStore.update_progress rather than replaced, so it survives
        # every later, narrower update.
        progress_hook({"title": probe_data.get("title")})

    resolved_quality = _resolve_quality(quality)
    is_live = bool(probe_data.get("is_live"))

    if output_template is None:
        output_template = DEFAULT_OUTPUT_TEMPLATE

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": resolved_quality,
        "concurrent_fragment_downloads": concurrent_fragments,
        # `concurrent_fragment_downloads` alone only parallelizes fragmented
        # protocols (DASH/HLS) -- confirmed directly, a 4K AV1 format served
        # over plain progressive HTTPS was single-connection and unaffected
        # by it despite "Concurrent fragments: 6" being set. http_chunk_size
        # makes yt-dlp split even a plain HTTPS download into internal
        # chunks, which concurrent_fragment_downloads then actually
        # parallelizes -- YouTube's CDN throttles per-connection, so this is
        # the difference between one throttled connection and several.
        "http_chunk_size": 10 * 1024 * 1024,
        "outtmpl": str(Path(output_dir) / output_template),
        "merge_output_format": "mkv",
        **JS_RUNTIME_OPTS,
        **_cookie_opts(cookies, cookies_from_browser),
    }
    if is_live:
        # Capture from the actual start of the broadcast instead of
        # joining mid-stream — YouTube (unlike Twitch) supports this
        # directly. wait_for_video gives a just-scheduled/starting stream
        # some runway instead of failing outright; fragment_retries matters
        # for a recording that might run for hours unattended — both
        # settings carried over from DownTube's own production use.
        ydl_opts["live_from_start"] = True
        ydl_opts["wait_for_video"] = (30, None)
        ydl_opts["fragment_retries"] = "infinite"

    download_url = probe_data.get("webpage_url") or url

    if is_live:
        # Run as a real child process, not yt-dlp's in-process API: a
        # continuous live channel's --live-from-start walk-back can block
        # for a long, unbounded time *inside yt-dlp's own extraction call*
        # (confirmed directly — over 50s with zero progress_hook calls and
        # extract_info never returning, against a real 24/7 stream), before
        # progress_hooks -- the only thing that ever checks cancel_event --
        # get a chance to fire even once. A real process can be killed
        # regardless of what it's blocked on internally; an in-process call
        # can't be interrupted from cancel_event alone. See _live_worker.py.
        progress_done_event = threading.Event()
        if progress_hook is not None and probe_data.get("id"):
            threading.Thread(
                target=_watch_live_progress,
                args=(progress_done_event, output_dir, probe_data["id"], progress_hook),
                daemon=True,
            ).start()
        try:
            final_path = _run_live_capture(download_url, ydl_opts, progress_hook, cancel_event)
        except DownloadCancelledError as exc:
            video_id = probe_data.get("id")
            salvaged = _salvage_split_download(output_dir, video_id)
            exc.path = finalize_filename(salvaged, video_id) if salvaged else None
            raise
        finally:
            progress_done_event.set()
        final_path = finalize_filename(final_path, probe_data.get("id"))
        return DownloadResult(path=Path(final_path), link_type=link_type)

    if progress_hook is not None or cancel_event is not None:
        ydl_opts["progress_hooks"] = [make_progress_hook(cancel_event, progress_hook)]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(download_url, download=True)

            if data is None:
                raise InfoError(f"No info returned for {url!r}")

            if "entries" in data:
                entries = list(data.get("entries") or [])
                if not entries:
                    raise InfoError(f"No video found at {url!r}")
                data = entries[0]

            final_path = ydl.prepare_filename(data)
    except yt_dlp.utils.DownloadCancelled as exc:
        err = DownloadCancelledError(str(exc))
        video_id = probe_data.get("id")
        salvaged = _salvage_split_download(output_dir, video_id)
        err.path = finalize_filename(salvaged, video_id) if salvaged else None
        raise err from exc
    except yt_dlp.utils.DownloadError as exc:
        raise InfoError(f"Could not download {url!r}: {exc}") from exc

    final_path = finalize_filename(final_path, data.get("id") or probe_data.get("id"))
    return DownloadResult(path=Path(final_path), link_type=link_type)
