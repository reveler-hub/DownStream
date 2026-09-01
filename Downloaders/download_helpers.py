"""Helpers shared by every site's `download.py`.

Factored out after the same fix had to be written twice -- a retry-on-cancel
race was fixed for Twitch, then found (by inspection, not report) to affect
Kick too, which also turned out to be quietly missing an *earlier* fix
(stable live filenames + forced overwrite) Twitch already had. Both gaps had
the same root cause: nothing forced the two copies to change together, since
they were never one piece of code to begin with. Every function here is
genuinely site-agnostic -- none of it encodes anything about a specific
platform -- so a future fix made here reaches every caller at once instead
of relying on someone remembering to port it.

`get_child_pids`/`find_ffmpeg_pid_for_output`/`stop_ffmpeg` locate and stop
the ffmpeg subprocess yt-dlp spawns for a live HLS capture (Twitch, Kick --
YouTube's live path is architecturally different; see its own
`_live_worker.py`/`_stop_live_worker`). `salvage_partial_file`,
`watch_for_cancel`, and `watch_progress` build on those for the cancel/
progress gap an external-ffmpeg live capture creates: yt-dlp's own
`progress_hooks` -- the only thing that ever notices `cancel_event`, or
reports progress -- simply never fire while ffmpeg is doing the actual
downloading. `finalize_filename` and `make_progress_hook` apply to every
download, live or not, on any site (YouTube included).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

import yt_dlp

_IS_WINDOWS = sys.platform.startswith("win")


def get_child_pids(pid: int) -> list[int]:
    """Direct child PIDs of `pid`, right now."""
    try:
        if _IS_WINDOWS:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f'(Get-CimInstance Win32_Process -Filter "ParentProcessId={pid}").ProcessId'],
                capture_output=True, text=True, timeout=5,
            )
        else:
            result = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5)
        return [int(p) for p in result.stdout.split()]
    except Exception:
        return []


def find_ffmpeg_pid_for_output(parent_pid: int, output_path: str) -> int | None:
    """Among `parent_pid`'s direct children, the one whose command line
    mentions `output_path` -- i.e. the specific ffmpeg process yt-dlp
    spawned to write *this* download, not some other job's, since a
    long-running --remote/--gui server can have several live downloads
    (each with their own ffmpeg child of this same Python process) going
    at once."""
    for child_pid in get_child_pids(parent_pid):
        try:
            if _IS_WINDOWS:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", f'(Get-CimInstance Win32_Process -Filter "ProcessId={child_pid}").CommandLine'],
                    capture_output=True, text=True, timeout=5,
                )
            else:
                result = subprocess.run(["ps", "-p", str(child_pid), "-o", "args="], capture_output=True, text=True, timeout=5)
            cmdline = result.stdout
        except Exception:
            continue
        if output_path in cmdline:
            return child_pid
    return None


def stop_ffmpeg(pid: int) -> None:
    """SIGINT first on Unix, so ffmpeg finalizes the output file instead of
    leaving it truncated mid-write; escalates to SIGKILL if it's still
    alive after a short wait. Windows has no equivalent graceful stop
    available here (CTRL_BREAK_EVENT only works on a process we ourselves
    spawned with CREATE_NEW_PROCESS_GROUP, which yt-dlp's internal ffmpeg
    subprocess isn't), so it's a hard `taskkill /F` there -- the output
    file is left as whatever ffmpeg had written at that instant."""
    try:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
            return
        os.kill(pid, signal.SIGINT)
    except (ProcessLookupError, OSError):
        return

    for _ in range(15):
        time.sleep(1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def salvage_partial_file(expected_path: str) -> str | None:
    """A cancelled live download's actual video data survives on disk as
    `<expected_path>.part` -- yt-dlp's own temp-file convention, only
    renamed to the real filename on a *successful* completion, which a
    cancellation by definition never reaches. `stop_ffmpeg`'s SIGINT
    already makes ffmpeg finalize a valid, playable file there (confirmed
    directly: cancelled a real live download, the resulting `.part` file
    opened fine end-to-end in `ffprobe` as complete, correctly-encoded
    h264 video) -- this just gives it back its real name instead of
    leaving something that looks broken/hidden behind an unfamiliar
    extension. Returns the final path if a rename happened, None if there
    was nothing to salvage (or the rename itself failed).
    """
    part_path = expected_path + ".part"
    if not os.path.exists(part_path):
        return None
    try:
        os.replace(part_path, expected_path)
        return expected_path
    except OSError:
        return None


def finalize_filename(path: str, video_id: str | None) -> str:
    """Renames a real, finished output file down from yt-dlp's own
    `<title> [<id>].<ext>` naming to just `<title>.<ext>`. The id is only
    ever needed while a download is still in flight -- keeping a live
    channel's filename stable across retries (see each site's own
    DEFAULT_LIVE_OUTPUT_TEMPLATE) and disambiguating leftover temp files on
    cancel (salvage_partial_file, or YouTube's own _salvage_split_download)
    -- not in what actually lands in the user's downloads folder.

    Falls back to keeping the id if a different file already occupies the
    clean name, rather than silently overwriting it -- the case this
    guards against is real, not theoretical: two different recordings can
    share the exact same title (a live channel's title staying stable
    broadcast to broadcast, or two videos that just happen to be titled the
    same), so the first finished recording gets the clean name and a
    second, later one correctly falls back to its own id-suffixed name
    instead of clobbering the first. Best-effort: returns `path` unchanged
    if there's no id to strip or the rename itself fails.
    """
    if not video_id:
        return path
    marker = f" [{video_id}]"
    if marker not in path:
        return path
    clean_path = path.replace(marker, "", 1)
    if os.path.exists(clean_path) and os.path.abspath(clean_path) != os.path.abspath(path):
        return path
    try:
        os.replace(path, clean_path)
        return clean_path
    except OSError:
        return path


def watch_for_cancel(cancel_event: threading.Event, done_event: threading.Event, output_path: str) -> None:
    """Runs for the duration of a live download only (started right before
    it, stopped right after via `done_event`). yt-dlp hands a live HLS
    capture off to `ffmpeg` as an external process -- completely outside
    yt-dlp's own Python-level progress_hooks, which is the only thing that
    ever checks `cancel_event` otherwise, so a cancelled live download
    would just keep running forever without this. Polls rather than
    blocking indefinitely on cancel_event.wait() so it also notices
    `done_event` (the download finishing on its own, e.g. the stream
    ending) and exits instead of leaking a thread.

    Cancelling keeps retrying the PID lookup rather than trying once and
    giving up -- ffmpeg may not have spawned yet (still resolving the
    stream) at the exact moment cancel_event is set. Confirmed directly: a
    cancel requested in that window, against the single-attempt version of
    this function, left the real capture running to completion, completely
    untouched, despite the UI reporting the job cancelled.
    """
    my_pid = os.getpid()
    while not done_event.is_set():
        if cancel_event.wait(0.5):
            while not done_event.is_set():
                pid = find_ffmpeg_pid_for_output(my_pid, output_path)
                if pid is not None:
                    stop_ffmpeg(pid)
                    return
                if done_event.wait(0.5):
                    return
            return


def watch_progress(done_event: threading.Event, output_path: str, progress_hook: Callable[[dict], None]) -> None:
    """Runs alongside watch_for_cancel, for the same reason: yt-dlp's own
    progress_hooks never fire for a live channel capture, since ffmpeg is
    downloading it entirely on its own, outside yt-dlp's Python-level
    downloader. Without this, a job's progress never updates past its
    initial empty state, so a genuinely-downloading live capture just
    shows "Starting..." in the GUI forever, no matter how much real data
    has actually been written -- confirmed directly against a real
    capture that was already tens of megabytes in. Polls the growing
    `.part` file's size directly instead of trying to parse ffmpeg's own
    progress output, which yt-dlp manages internally and doesn't expose.
    """
    part_path = output_path + ".part"
    while not done_event.wait(2):
        try:
            size = os.path.getsize(part_path)
        except OSError:
            continue
        progress_hook({"status": "downloading", "downloaded_bytes": size})


def make_progress_hook(
    cancel_event: threading.Event | None, user_hook: Callable[[dict], None] | None
) -> Callable[[dict], None]:
    """Wraps `user_hook` (if any) so every progress update also checks
    `cancel_event` first -- yt-dlp calls a progress hook often enough
    during a normal, non-live-ffmpeg download for this to be the actual
    mechanism that stops one once cancelled: raising yt-dlp's own
    DownloadCancelled from inside the hook is what unwinds out of
    `extract_info()`."""
    def hook(d: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("Cancelled by user")
        if user_hook is not None:
            user_hook(d)

    return hook
