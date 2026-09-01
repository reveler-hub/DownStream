"""`--remote` mode: control DownStream over HTTP+JSON from another device.

A small stdlib-only HTTP server (no new dependency, so it runs anywhere
yt-dlp does — including a NAS or SBC) exposing job-based control of any
registered site's `get_video_info`/`download_video`. Flow:

    POST /jobs   {"url": ..., "action": "info"|"download", "options": {...}}
                 -> {"job_id": "..."}
    GET  /jobs/<job_id>
                 -> {"status": "running"|"done"|"error"|"cancelled", "progress": {...}, "result": {...}}
    GET  /jobs   -> {"jobs": [...]} -- every "download"-action job this
                 server process has ever seen (JobStore never expires
                 entries), so the GUI can re-adopt still-running ones after
                 a page reload instead of losing track of them (a plain
                 browser refresh empties its own in-memory `jobs` array, but
                 doesn't touch anything server-side -- the download itself
                 was never tied to that page staying open). "info"-action
                 jobs (the New Job form's live URL-preview probe) are
                 deliberately excluded -- they're never added to the GUI's
                 own job list either, so leaving them in here just meant a
                 page reload would dump one onto the Jobs page as a
                 surprise, never-submitted "info — done" card.
    POST /jobs/<job_id>/cancel
                 -> the job, unchanged if it wasn't still running

`options` is passed straight through as keyword arguments to the matched
site's `get_video_info`/`download_video` — this module doesn't know or care
what any site's specific options are, same principle as `cli.py`'s argv
passthrough. One deliberate exception: `options.cookies_content`, if
present, is this server's own concept, not a site option — it's the raw
text of a cookies.txt file, uploaded directly with the job so a browser
GUI can supply cookies to a headless machine (a NAS running `--remote`)
that has no browser of its own for `-b/--cookies-from-browser` to read.
`_run_job` materializes it into a real temp file and passes that as the
normal `cookies=<path>` argument, then removes it once the job finishes.
Every request needs `Authorization: Bearer <token>`; a random token is
generated at startup and printed unless one is passed in.

    GET  /browsers -> {"detected": [{"id", "label", "value"}, ...]}

Best-effort list of browsers with real, used cookie databases on whichever
machine actually receives this request -- *this* one for "This Computer",
or the box "Remote" points at, since a --remote instance answers the same
way about itself. Includes Firefox-based forks (Zen, LibreWolf, Floorp)
yt-dlp doesn't know by name, whose `value` points `-b/--cookies-from-browser`
at the fork's actual profile directory instead (`"firefox:<path>"`, the
same syntax the flag already accepts for any custom profile). Sorted
most-recently-used first -- the GUI has no picker for this, it just
automatically sends entry 0's `value` as `cookies_from_browser` with every
job unless a cookies file is uploaded instead, on the theory that the
browser you touched most recently is the one you're actually logged into.

`GET /` (no auth required) serves `gui.html`, DownStream's browser-based
control panel, with this instance's own base URL/token pre-filled — that's
what `--gui` opens automatically. `--remote` serves the exact same page and
API; the only differences are the default bind address (127.0.0.1 vs
0.0.0.0) and whether a browser tab gets opened. Every JSON response carries
permissive CORS headers so that page's "Remote" tab can reach a *different*
DownStream instance across origins (e.g. this browser tab, opened by a
local `--gui`, controlling a NAS running `--remote`).

Auto-watch (add a channel once, get a job automatically whenever it's
live):

    POST /channels   {"url": ..., "auto_download": true, "options": {...}}
                      -> the new channel
    GET  /channels    -> {"channels": [...]}
    POST /channels/<channel_id>   {"auto_download": ...} and/or {"options": ...}
                      -> the updated channel
    DELETE /channels/<channel_id> -> stops watching it (an in-progress
                      download it triggered is left to finish, not cut off)

A background thread (`_watch_loop`, started once in `run_server`) polls
every channel with `auto_download` on, using the same site.get_video_info
every other lookup uses -- no cookies passed for that check, since a
channel's live/offline status has been open to check anonymously on every
site tested against so far. The moment one flips to live, it creates a
job through the exact same `JobStore`/`_run_job` path `POST /jobs` uses,
so an auto-triggered download shows up in the normal jobs list like any
other -- no separate download pipeline to maintain. Channel *config*
(url, auto_download, options) is persisted to `_CHANNELS_FILE` next to
wherever the server runs, surviving restarts; live runtime state (status,
last_checked, the active job id) is not persisted and starts fresh -- from
"checking" -- every time the server (re)starts. If a channel's `options`
includes `cookies_content`, that raw cookies.txt text IS persisted to that
file in plaintext, unlike the one-shot `/jobs` case where it only ever
touches a temp file deleted immediately after -- worth knowing before
using that combination on a shared machine.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .registry import find_site

_GUI_HTML_PATH = Path(__file__).parent / "gui.html"
_ERROR_LOG_NAME = "DownStream-server-error.log"
_CHANNELS_FILE = Path("downstream-channels.json")
_DEFAULT_WATCH_INTERVAL = 90.0  # seconds between full poll cycles

# tkinter dialogs aren't safe to have two of open at once from different
# threads (observed hangs on Windows when a second askdirectory() call
# raced an already-open one) -- this server otherwise spawns one thread per
# request, so without a lock two near-simultaneous "Browse..." clicks could
# do exactly that.
_folder_picker_lock = threading.Lock()


def _pick_folder_native(initial_dir: str | None) -> str | None:
    """Opens a native OS folder-picker dialog *on this machine* and returns
    the chosen absolute path, or None if the user cancelled.

    Only meaningful when the server and the browser are on the same
    machine -- the GUI's "This Computer" case. Pointed at a `--remote` box
    instead, this pops the dialog on whatever display *that* machine has
    (headless NAS/SBC: none, so it raises outright) rather than the
    browser's -- a real limitation, not a bug, since there's no way for a
    server process to reach back into a browser's own filesystem sandbox
    for an absolute path.
    """
    import tkinter
    from tkinter import filedialog

    with _folder_picker_lock:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)  # otherwise it can open behind the browser window
        try:
            chosen = filedialog.askdirectory(
                initialdir=initial_dir if initial_dir and os.path.isdir(initial_dir) else None,
                mustexist=True,
            )
        finally:
            root.destroy()
        return chosen or None


def _cleanup_orphaned_live_workers() -> None:
    """A crash or restart while a YouTube live capture (see
    `Downloaders/youtube/_live_worker.py`) was in progress leaves its child
    process orphaned -- killing the parent doesn't kill an already-spawned
    subprocess, and the new server incarnation's JobStore has no memory of
    it either way (job tracking is in-memory, reset on every start), so
    there's no way to find or cancel it through the app any more. Confirmed
    directly: a restart during testing left one running untracked for over
    ten minutes. Best-effort sweep on startup so these don't just
    accumulate silently across restarts or crashes.
    """
    try:
        if sys.platform.startswith("win"):
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    '(Get-CimInstance Win32_Process | Where-Object '
                    '{ $_.CommandLine -like "*_live_worker.py*" }).ProcessId',
                ],
                capture_output=True, text=True, timeout=5,
            )
        else:
            result = subprocess.run(["pgrep", "-f", "_live_worker.py"], capture_output=True, text=True, timeout=5)
    except Exception:
        return

    for pid_str in result.stdout.split():
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
            else:
                os.kill(pid, signal.SIGINT)
        except (ProcessLookupError, OSError):
            continue


def _detect_installed_browsers() -> list[tuple[str, float]]:
    """Best-effort `[(browser, cookie_db_mtime), ...]` for the
    (`-b/--cookies-from-browser`-supported) browsers that look installed on
    *this* machine -- the mtime lets `_detect_cookie_sources` rank several
    detected browsers by which was most recently actually signed into
    (touches the cookie database), so the GUI can pick one automatically
    without asking.

    Deliberately cheap: walks each browser's profile directory looking for
    an actual "Cookies"/"cookies.sqlite" file -- the same directory-walk
    yt-dlp's own extractor does before it ever opens or decrypts one, reused
    here via yt-dlp's own (private, so this degrades gracefully rather than
    breaking outright if a future yt-dlp release changes them) helpers,
    rather than a shallower "does the top-level profile folder exist" check
    -- several browsers on a real machine can have that folder present but
    empty (e.g. a package manager pre-creating it) despite never having
    actually been signed into, which would otherwise offer a browser whose
    cookie lookup was always going to fail at download time. A browser
    missing from the result doesn't mean it can't work, just that no cookie
    database was found somewhere yt-dlp already knows to look.
    """
    found: list[tuple[str, float]] = []

    try:
        from yt_dlp.cookies import CHROMIUM_BASED_BROWSERS, YDLLogger, _find_files, _get_chromium_based_browser_settings, _newest
    except ImportError:
        CHROMIUM_BASED_BROWSERS, _get_chromium_based_browser_settings, _newest = (), None, None

    if _get_chromium_based_browser_settings is not None:
        logger = YDLLogger()
        for browser in sorted(CHROMIUM_BASED_BROWSERS):
            try:
                browser_dir = _get_chromium_based_browser_settings(browser)["browser_dir"]
                if not os.path.isdir(browser_dir):
                    continue
                newest_path = _newest(_find_files(browser_dir, "Cookies", logger))
                if newest_path is not None:
                    found.append((browser, os.lstat(newest_path).st_mtime))
            except Exception:
                continue  # this browser's directory layout isn't what was expected -- skip it, not fatal

    try:
        from yt_dlp.cookies import _firefox_browser_dirs, _firefox_cookie_dbs
        from yt_dlp.cookies import _newest as _newest_ff
        newest_path = _newest_ff(_firefox_cookie_dbs(_firefox_browser_dirs()))
        if newest_path is not None:
            found.append(("firefox", os.lstat(newest_path).st_mtime))
    except Exception:
        pass

    if sys.platform == "darwin":
        safari_paths = (
            "~/Library/Cookies/Cookies.binarycookies",
            "~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
        )
        for p in safari_paths:
            full = os.path.expanduser(p)
            if os.path.isfile(full):
                found.append(("safari", os.lstat(full).st_mtime))
                break

    return found


# Firefox-based forks yt-dlp doesn't recognize by name -- it only knows
# "firefox" itself, but its Firefox extractor accepts a literal directory as
# the "profile" half of BROWSER:PROFILE (see `_is_path` in yt-dlp's own
# cookies.py), so a fork's cookies are reachable as "firefox:<profile dir>"
# once that directory is found. Base folders below are confirmed against
# real installs on Linux + Windows; Mac entries are inferred from the same
# per-browser naming convention (unverified on real hardware -- detection
# just fails gracefully, same as everything else in this file, if wrong).
_FIREFOX_FORK_BASE_DIRS: dict[str, dict[str, Any]] = {
    "Floorp": {
        "linux": lambda: [os.path.expanduser("~/.floorp")],
        "win32": lambda: [os.path.join(os.environ.get("APPDATA", ""), "Floorp")],
        "darwin": lambda: [os.path.expanduser("~/Library/Application Support/Floorp")],
    },
    "Zen": {
        "linux": lambda: [os.path.expanduser("~/.config/zen")],
        "win32": lambda: [os.path.join(os.environ.get("APPDATA", ""), "zen")],
        "darwin": lambda: [os.path.expanduser("~/Library/Application Support/zen")],
    },
    "LibreWolf": {
        # Confirmed on Arch's official pacman package: nested one level
        # deeper (~/.config/librewolf/librewolf) than Zen's flat layout --
        # try that first, then fall back to the flat layout other install
        # methods (official tarball, other distros) may use instead.
        "linux": lambda: [
            os.path.expanduser("~/.config/librewolf/librewolf"),
            os.path.expanduser("~/.config/librewolf"),
        ],
        "win32": lambda: [os.path.join(os.environ.get("APPDATA", ""), "librewolf")],
        "darwin": lambda: [os.path.expanduser("~/Library/Application Support/librewolf")],
    },
}


def _read_ini_sections(path: str) -> dict[str, dict[str, str]]:
    """Minimal `[Section]\\nkey=value` parser -- stdlib's `configparser` balks
    at profiles.ini's duplicate-looking section names across old Firefox
    versions, so Firefox-based browsers all ship (and parse) their own."""
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                current = m.group(1)
                sections[current] = {}
            elif current and "=" in line:
                key, _, value = line.partition("=")
                sections[current][key.strip()] = value.strip()
    return sections


def _resolve_default_profile(base_dir: str) -> str:
    """Parse profiles.ini (+ installs.ini) the way Firefox-based browsers
    do, preferring an install-specific default over the plain Default=1
    flag -- necessary because real Zen/LibreWolf installs have been seen to
    disagree between the two. Returns "" on anything unresolvable."""
    ini_path = os.path.join(base_dir, "profiles.ini")
    if not os.path.isfile(ini_path):
        return ""
    try:
        sections = _read_ini_sections(ini_path)
        installs_path = os.path.join(base_dir, "installs.ini")
        if os.path.isfile(installs_path):
            for key, values in _read_ini_sections(installs_path).items():
                sections[f"Install_{key}"] = values
    except OSError:
        return ""

    target = None
    for key, values in sections.items():
        if key.startswith("Install"):
            target = values.get("Default") or target
    if not target:
        for key, values in sections.items():
            if key.startswith("Profile") and values.get("Default") == "1":
                target = values.get("Path")
    if not target:
        return ""
    full = os.path.join(base_dir, target)
    return full if os.path.isdir(full) else ""


def _detect_firefox_forks() -> list[tuple[str, str, float]]:
    """[(display_name, resolved_profile_dir, cookie_db_mtime), ...] for each
    fork in `_FIREFOX_FORK_BASE_DIRS` whose base folder exists here, whose
    default profile resolves, AND which actually has a cookies.sqlite in
    it -- a resolved profile with no cookie database yet (installed but
    never signed into anywhere) is skipped rather than offered, same
    reasoning as `_detect_installed_browsers`."""
    platform_key = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    found = []
    for fork_name, platform_dirs in _FIREFOX_FORK_BASE_DIRS.items():
        getter = platform_dirs.get(platform_key)
        if not getter:
            continue
        for base_dir in getter():
            if base_dir and os.path.isdir(base_dir):
                resolved = _resolve_default_profile(base_dir)
                if not resolved:
                    continue
                cookie_db = os.path.join(resolved, "cookies.sqlite")
                if os.path.isfile(cookie_db):
                    found.append((fork_name, resolved, os.lstat(cookie_db).st_mtime))
                    break
    return found


def _detect_cookie_sources() -> list[dict[str, str]]:
    """Detected browsers AND Firefox-based forks, unified into
    `{"id", "label", "value"}` ready for the GUI to use directly -- `value`
    is exactly what `-b/--cookies-from-browser` expects (a fork's is
    `"firefox:<resolved profile dir>"`). Sorted most-recently-used first:
    the GUI has no picker for this any more, it just automatically uses
    entry 0 (touching a browser's cookie database, i.e. actually being
    logged into something with it, is a reasonable proxy for "the one
    you'd want cookies from")."""
    entries = [
        {"label": b.title(), "id": b, "value": b, "_mtime": mtime}
        for b, mtime in _detect_installed_browsers()
    ]
    entries += [
        {"label": fork_name, "id": fork_name.lower(), "value": f"firefox:{profile_path}", "_mtime": mtime}
        for fork_name, profile_path, mtime in _detect_firefox_forks()
    ]
    entries.sort(key=lambda e: e["_mtime"], reverse=True)
    for e in entries:
        del e["_mtime"]
    return entries


def _log_startup_error() -> None:
    """`--remote`/`--gui` is often run headless (nohup'd over SSH on a
    NAS/SBC, or double-clicked with no attached terminal) where a printed
    traceback can be missed or lost once the session ends -- write it to a
    log file in the current directory too, in addition to letting it print
    normally. Never raises itself, so a failure here can't mask the real
    error."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_ERROR_LOG_NAME, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] Failed to start:\n{traceback.format_exc()}\n")
        print(f"(also logged to {_ERROR_LOG_NAME})")
    except OSError:
        pass  # can't even write the log -- the terminal traceback is all there is


def _json_safe(value: Any) -> Any:
    """Recursively convert dataclasses/Path/Enum values into JSON-serializable ones."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@dataclass
class Job:
    id: str
    url: str
    action: str  # "info" | "download"
    status: str = "running"  # "running" | "done" | "error" | "cancelled"
    progress: dict = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    # Server-side, not client-side, so a page reload doesn't reset the
    # clock -- `elapsed` in to_json() is always computed fresh from these,
    # never tracked separately in the GUI's own job objects.
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "action": self.action,
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "elapsed": (self.finished_at or time.time()) - self.created_at,
        }


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, url: str, action: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], url=url, action=action)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def update_progress(self, job_id: str, progress: dict) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                # Merged, not replaced -- a live channel capture's own
                # progress_hook calls only ever carry a couple of keys
                # (status/downloaded_bytes, see each site's _watch_progress),
                # not a full yt-dlp info_dict. Replacing outright would wipe
                # out the title a job's very first hook call sets (see
                # download_video() in each site's download.py) the moment
                # any later, narrower update arrived.
                job.progress = {**job.progress, **progress}

    def finish(self, job_id: str, result: Any = None, error: str | None = None, cancelled: bool = False) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.finished_at = time.time()
            if cancelled:
                job.status = "cancelled"
                job.error = error or "Cancelled by user"
                if result is not None:
                    # A cancelled live download can still have salvaged a
                    # real, playable file (see the site modules'
                    # _salvage_partial_file) -- worth keeping even though
                    # the job as a whole didn't finish the way it was asked
                    # to.
                    job.result = _json_safe(result)
            elif error is not None:
                job.status = "error"
                job.error = error
            else:
                job.status = "done"
                job.result = _json_safe(result)

    def cancel(self, job_id: str) -> Job | None:
        """Signal a running job to stop. No-op if the job is already finished."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job.status == "running":
                job.cancel_event.set()
            return job


@dataclass
class Channel:
    id: str
    url: str
    auto_download: bool = True
    options: dict = field(default_factory=dict)
    # Runtime state -- not persisted, starts fresh on every server start.
    status: str = "checking"  # "checking" | "offline" | "live" | "downloading" | "error"
    last_checked: float | None = None
    last_error: str | None = None
    active_job_id: str | None = None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "auto_download": self.auto_download,
            "options": self.options,
            "status": self.status,
            "last_checked": self.last_checked,
            "last_error": self.last_error,
            "active_job_id": self.active_job_id,
        }

    def to_persisted_dict(self) -> dict:
        """Config only -- excludes the runtime fields above."""
        return {"id": self.id, "url": self.url, "auto_download": self.auto_download, "options": self.options}


class ChannelStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._channels: dict[str, Channel] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            entries = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for entry in entries:
            try:
                channel = Channel(
                    id=entry["id"],
                    url=entry["url"],
                    auto_download=bool(entry.get("auto_download", True)),
                    options=entry.get("options") or {},
                )
            except KeyError:
                continue  # malformed entry -- skip rather than fail the whole load
            self._channels[channel.id] = channel

    def _save(self) -> None:
        try:
            data = [c.to_persisted_dict() for c in self._channels.values()]
            self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def add(self, url: str, auto_download: bool, options: dict) -> Channel:
        channel = Channel(id=uuid.uuid4().hex[:12], url=url, auto_download=auto_download, options=options)
        with self._lock:
            self._channels[channel.id] = channel
            self._save()
        return channel

    def remove(self, channel_id: str) -> bool:
        with self._lock:
            existed = self._channels.pop(channel_id, None) is not None
            if existed:
                self._save()
            return existed

    def get(self, channel_id: str) -> Channel | None:
        with self._lock:
            return self._channels.get(channel_id)

    def list(self) -> list[Channel]:
        with self._lock:
            return list(self._channels.values())

    def update_config(self, channel_id: str, **kwargs: Any) -> Channel | None:
        """User-facing changes (auto_download, options) -- persisted."""
        with self._lock:
            channel = self._channels.get(channel_id)
            if channel is None:
                return None
            for key, value in kwargs.items():
                setattr(channel, key, value)
            self._save()
            return channel

    def update_status(self, channel_id: str, **kwargs: Any) -> None:
        """Runtime state from the watch loop (status, last_checked, ...) -- not persisted."""
        with self._lock:
            channel = self._channels.get(channel_id)
            if channel is not None:
                for key, value in kwargs.items():
                    setattr(channel, key, value)


def _check_channel(channel: Channel, channel_store: ChannelStore, job_store: JobStore) -> None:
    """One poll of one channel: skip if a triggered download is still running,
    otherwise check live status and start a download job the moment it's
    seen live. Never raises -- a single channel's failure (bad URL, site
    down, whatever) must not stop the rest from being checked."""
    if channel.active_job_id is not None:
        job = job_store.get(channel.active_job_id)
        if job is not None and job.status == "running":
            return  # already downloading -- nothing to do this cycle
        channel_store.update_status(channel.id, active_job_id=None)

    site = find_site(channel.url)
    if site is None:
        channel_store.update_status(
            channel.id, status="error", last_error="No downloader available for this URL", last_checked=time.time()
        )
        return

    offline_exc = getattr(site, "ChannelOfflineError", ())  # empty tuple: isinstance() never matches
    try:
        info = site.get_video_info(channel.url)
    except Exception as exc:
        if isinstance(exc, offline_exc):
            channel_store.update_status(channel.id, status="offline", last_checked=time.time(), last_error=None)
        else:
            channel_store.update_status(channel.id, status="error", last_checked=time.time(), last_error=str(exc))
        return

    # `getattr(..., False)`, not `info.is_live` -- not every site's VideoInfo
    # has a live/offline concept at all (Instagram's doesn't: a post/story
    # either exists or doesn't, there's no continuous stream to watch for).
    # This function is documented (see its own docstring) as never raising,
    # called from _watch_loop with no per-channel try/except of its own --
    # confirmed directly that a plain `info.is_live` here throws
    # AttributeError for such a site, which would kill the whole watch
    # thread (every channel, every site) on its very next poll rather than
    # just this one channel.
    if not getattr(info, "is_live", False):
        channel_store.update_status(channel.id, status="offline", last_checked=time.time(), last_error=None)
        return

    channel_store.update_status(channel.id, status="live", last_checked=time.time(), last_error=None)
    job = job_store.create(channel.url, "download")
    channel_store.update_status(channel.id, status="downloading", active_job_id=job.id)
    threading.Thread(target=_run_job, args=(job_store, job, dict(channel.options)), daemon=True).start()


def _watch_loop(
    channel_store: ChannelStore, job_store: JobStore, poll_interval: float, stop_event: threading.Event
) -> None:
    """Runs a full check immediately on start (so a fresh launch doesn't sit
    idle for a whole interval before its first look), then every
    `poll_interval` seconds after. A channel added mid-cycle just waits for
    the next one -- this is a periodic watcher, not a realtime push."""
    while True:
        for channel in channel_store.list():
            if not channel.auto_download:
                continue
            _check_channel(channel, channel_store, job_store)
        if stop_event.wait(poll_interval):
            return


def _supported_options(func, options: dict) -> dict:
    """Drops any `options` key `func` doesn't accept as a keyword argument.

    The GUI sends one option set regardless of which site the URL belongs
    to (e.g. `concurrent_fragments` always has a value, whether or not the
    target site's `download_video` even has that parameter) -- this module
    deliberately doesn't know or care what any site's specific options are
    (see this module's own docstring), so unlike `cli.py`'s argv passthrough
    (where each site's own argparse rejects an option it doesn't define),
    here a site missing a parameter the GUI always sends would otherwise
    surface as a raw TypeError instead of just being ignored. Confirmed
    directly: Instagram's `download_video` has no `concurrent_fragments`
    parameter, and the GUI's fragments field always has a value (defaults
    to 6), so a real submitted job crashed with exactly this error before
    this filter existed.
    """
    accepted = inspect.signature(func).parameters
    return {k: v for k, v in options.items() if k in accepted}


def _run_job(store: JobStore, job: Job, options: dict) -> None:
    site = find_site(job.url)
    if site is None:
        store.finish(job.id, error=f"No downloader available for: {job.url}")
        return

    def hook(d: dict) -> None:
        store.update_progress(job.id, _json_safe(d))

    # `cookies_content` isn't a real site option -- it's this server's own
    # convenience layer for the case a browser-driven GUI actually needs:
    # controlling a headless machine (a NAS running --remote) that has no
    # browser of its own for `-b/--cookies-from-browser` to read, so the
    # GUI instead uploads an already-exported cookies.txt's raw content
    # with the job. Materialize it into a real file here -- the one place
    # that needs to know this trick exists -- so every site module still
    # only ever sees the plain `cookies=<path>` it already understands.
    # Written with mkstemp's default 0600 permissions (this is session
    # credential material) and removed once the job is done either way.
    options = dict(options)
    cookies_content = options.pop("cookies_content", None)
    temp_cookie_path: str | None = None
    if cookies_content:
        fd, temp_cookie_path = tempfile.mkstemp(prefix="downstream-cookies-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(cookies_content)
        options["cookies"] = temp_cookie_path

    try:
        try:
            if job.action == "info":
                result = site.get_video_info(job.url, **_supported_options(site.get_video_info, options))
            elif job.action == "download":
                result = site.download_video(
                    job.url,
                    progress_hook=hook,
                    cancel_event=job.cancel_event,
                    **_supported_options(site.download_video, options),
                )
            else:
                store.finish(job.id, error=f"Unknown action: {job.action!r}")
                return
        except Exception as exc:
            # A cancelled live download can still have salvaged a real file
            # on disk (see e.g. Downloaders/twitch/download.py's
            # _salvage_partial_file) -- surfaced here as `.path` on the
            # exception rather than a full DownloadResult, since there's no
            # such object for a call that didn't actually complete.
            salvaged_path = getattr(exc, "path", None)
            result = {"path": salvaged_path} if salvaged_path else None
            store.finish(job.id, result=result, error=str(exc), cancelled=job.cancel_event.is_set())
            return

        store.finish(job.id, result=result)
    finally:
        if temp_cookie_path is not None:
            try:
                os.remove(temp_cookie_path)
            except OSError:
                pass


class _Handler(BaseHTTPRequestHandler):
    store: JobStore
    channels: ChannelStore
    token: str

    def _check_auth(self) -> bool:
        expected = f"Bearer {self.token}"
        return secrets.compare_digest(self.headers.get("Authorization", ""), expected)

    def _cors_headers(self) -> None:
        # Permissive by design: this API is protected by the bearer token,
        # not by origin, so the GUI page (served from one DownStream
        # instance's own origin) can reach a *different* instance's
        # --remote across origins for its "Remote" tab.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def _serve_gui(self) -> None:
        html = _GUI_HTML_PATH.read_text(encoding="utf-8")
        html = html.replace("__LOCAL_TOKEN__", self.token)
        self._send_html(200, html.encode("utf-8"))

    def _read_json_body(self) -> tuple[dict | None, tuple[int, dict] | None]:
        """Returns (body, None) on success, or (None, (status, error_payload)) on failure."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return None, (400, {"error": "invalid JSON body"})
        if not isinstance(body, dict):
            return None, (400, {"error": "body must be a JSON object"})
        return body, None

    def _handle_create_job(self) -> None:
        body, err = self._read_json_body()
        if err is not None:
            return self._send_json(*err)

        url = body.get("url")
        action = body.get("action", "info")
        options = body.get("options", {})
        if not url:
            return self._send_json(400, {"error": "missing 'url'"})
        if action not in ("info", "download"):
            return self._send_json(400, {"error": f"invalid action: {action!r}"})
        if not isinstance(options, dict):
            return self._send_json(400, {"error": "'options' must be an object"})

        job = self.store.create(url, action)
        threading.Thread(target=_run_job, args=(self.store, job, options), daemon=True).start()
        self._send_json(202, {"job_id": job.id})

    def _handle_pick_folder(self) -> None:
        body, err = self._read_json_body()
        if err is not None:
            return self._send_json(*err)

        initial_dir = body.get("initial_dir")
        try:
            path = _pick_folder_native(initial_dir if isinstance(initial_dir, str) else None)
        except Exception as exc:
            # Most likely: tkinter isn't installed (some slim Linux Python
            # builds skip it), or there's no display to show a dialog on
            # (a headless --remote box). Either way, this machine just
            # can't show a native picker -- typing the path is the fallback,
            # not a dead end.
            return self._send_json(501, {"error": f"No native folder picker available here ({exc}) -- type the path instead."})
        return self._send_json(200, {"path": path})  # path is None if the user cancelled

    def _handle_create_channel(self) -> None:
        body, err = self._read_json_body()
        if err is not None:
            return self._send_json(*err)

        url = body.get("url")
        options = body.get("options", {})
        if not url:
            return self._send_json(400, {"error": "missing 'url'"})
        if not isinstance(options, dict):
            return self._send_json(400, {"error": "'options' must be an object"})

        channel = self.channels.add(url, bool(body.get("auto_download", True)), options)
        self._send_json(201, channel.to_json())

    def _handle_update_channel(self, channel_id: str) -> None:
        body, err = self._read_json_body()
        if err is not None:
            return self._send_json(*err)

        updates: dict[str, Any] = {}
        if "auto_download" in body:
            updates["auto_download"] = bool(body["auto_download"])
        if "options" in body:
            if not isinstance(body["options"], dict):
                return self._send_json(400, {"error": "'options' must be an object"})
            updates["options"] = body["options"]
        if not updates:
            return self._send_json(400, {"error": "nothing to update -- send 'auto_download' and/or 'options'"})

        channel = self.channels.update_config(channel_id, **updates)
        if channel is None:
            return self._send_json(404, {"error": "channel not found"})
        self._send_json(200, channel.to_json())

    def do_POST(self) -> None:
        if not self._check_auth():
            return self._send_json(401, {"error": "unauthorized"})

        path = urlparse(self.path).path
        parts = path.strip("/").split("/")

        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
            job = self.store.cancel(parts[1])
            if job is None:
                return self._send_json(404, {"error": "job not found"})
            return self._send_json(202, job.to_json())

        if path == "/jobs":
            return self._handle_create_job()

        if path == "/pick-folder":
            return self._handle_pick_folder()

        if path == "/channels":
            return self._handle_create_channel()

        if len(parts) == 2 and parts[0] == "channels":
            return self._handle_update_channel(parts[1])

        return self._send_json(404, {"error": "not found"})

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/":
            return self._serve_gui()

        if not self._check_auth():
            return self._send_json(401, {"error": "unauthorized"})

        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 1 and parts[0] == "jobs":
            # "info" jobs are the New Job form's live URL-preview probe
            # (see gui.html's probeOnce -- fires on every debounced
            # keystroke pause, never something a user consciously submitted).
            # The GUI never calls addJob() for them, so leaving them in this
            # listing did nothing but wait for a page reload to dump them
            # onto the Jobs page as a surprise, unexplained "info — done"
            # card -- confirmed directly by reproducing it live.
            visible = [j for j in self.store.list() if j.action != "info"]
            return self._send_json(200, {"jobs": [j.to_json() for j in visible]})

        if len(parts) == 2 and parts[0] == "jobs":
            job = self.store.get(parts[1])
            if job is None:
                return self._send_json(404, {"error": "job not found"})
            return self._send_json(200, job.to_json())

        if len(parts) == 1 and parts[0] == "channels":
            return self._send_json(200, {"channels": [c.to_json() for c in self.channels.list()]})

        if len(parts) == 1 and parts[0] == "browsers":
            return self._send_json(200, {"detected": _detect_cookie_sources()})

        return self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if not self._check_auth():
            return self._send_json(401, {"error": "unauthorized"})

        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "channels":
            if not self.channels.remove(parts[1]):
                return self._send_json(404, {"error": "channel not found"})
            return self._send_json(200, {"removed": True})

        return self._send_json(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        pass  # BaseHTTPRequestHandler logs every request to stderr by default; too noisy


def _detect_existing_gui(gui_url: str) -> bool:
    """True if `gui_url` already serves a DownStream instance.

    Lets a second `--gui` launch (e.g. a double-click while an earlier one
    is still quietly running in the background, with no window to show for
    it) open the browser to the existing instance instead of dying on a
    port conflict. Only used for `--gui` (see `open_browser` below) —
    `--remote` deliberately still fails loudly on a real port conflict,
    since silently no-op'ing a headless server start would hide a genuine
    problem (wrong port already used by something else entirely).
    """
    try:
        with urllib.request.urlopen(gui_url, timeout=2) as response:
            body = response.read(8192).decode("utf-8", errors="replace")
    except Exception:
        return False
    return "<title>DownStream</title>" in body


def run_server(
    host: str = "0.0.0.0",
    port: int = 8420,
    token: str | None = None,
    open_browser: bool = False,
    watch_interval: float = _DEFAULT_WATCH_INTERVAL,
) -> None:
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    gui_url = f"http://{browse_host}:{port}/"

    if open_browser and _detect_existing_gui(gui_url):
        print(f"DownStream is already running at {gui_url} — opening it instead of starting a new instance.")
        try:
            webbrowser.open(gui_url)
        except Exception:
            pass
        return

    _cleanup_orphaned_live_workers()

    token = token or secrets.token_urlsafe(24)
    store = JobStore()
    channels = ChannelStore(_CHANNELS_FILE)

    class Handler(_Handler):
        pass

    Handler.store = store
    Handler.channels = channels
    Handler.token = token

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except Exception:
        _log_startup_error()
        raise

    watch_stop = threading.Event()
    threading.Thread(target=_watch_loop, args=(channels, store, watch_interval, watch_stop), daemon=True).start()

    print(f"DownStream server listening on {host}:{port}")
    print(f"Token: {token}")
    print(f"GUI:   {gui_url}")
    print("API requests need: Authorization: Bearer <token>")
    if channels.list():
        print(f"Watching {len(channels.list())} channel(s) from {_CHANNELS_FILE} (checking every {watch_interval:.0f}s)")
    if open_browser:
        try:
            webbrowser.open(gui_url)
        except Exception:
            pass  # no browser available (e.g. a headless box) — the printed URL still works
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    except Exception:
        _log_startup_error()
        raise
    finally:
        watch_stop.set()
