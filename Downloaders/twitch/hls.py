"""Concurrent HLS segment fetching for fast, frame-accurate chapter downloads.

yt-dlp's own section-download support (used for `--chapter` before this
module existed) forces a slow single-threaded ffmpeg read straight off the
network for HLS sources — see the git history of `download.py`. This module
instead fetches only the segments overlapping the requested time range
ourselves, concurrently, then re-encodes just that (now-local) slice to the
exact boundary — the same frame-accurate result, without the network being
the bottleneck for the whole chapter.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_SEGMENT_RETRIES = 3
_MAP_URI_RE = re.compile(r'#EXT-X-MAP:.*URI="([^"]+)"')


class Cancelled(Exception):
    """Raised when `cancel_event` is set while a chapter fetch is in progress."""


@dataclass
class Segment:
    start: float
    end: float
    uri: str
    init_uri: str | None = None


def parse_segments(playlist_text: str, base_url: str) -> list[Segment]:
    """Parse an HLS media playlist into its segments with absolute timestamps.

    Twitch VODs are packaged either as legacy MPEG-TS (segments are plain,
    independently-concatenable transport-stream packets) or as fragmented
    MP4/CMAF (each segment is a moof/mdat fragment that only makes sense
    appended after a shared `#EXT-X-MAP` init segment containing the moov
    box). `init_uri` carries that init segment's URL for fMP4 playlists —
    None for plain TS ones, where there's nothing to prepend.
    """
    segments: list[Segment] = []
    current_time = 0.0
    pending_duration = 0.0
    current_init_uri: str | None = None

    for line in playlist_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            m = _MAP_URI_RE.match(line)
            if m:
                uri = m.group(1)
                current_init_uri = uri if uri.startswith("http://") or uri.startswith("https://") else base_url + uri
        elif line.startswith("#EXTINF:"):
            pending_duration = float(line.removeprefix("#EXTINF:").split(",")[0])
        elif line and not line.startswith("#"):
            uri = line if line.startswith("http://") or line.startswith("https://") else base_url + line
            segments.append(
                Segment(start=current_time, end=current_time + pending_duration, uri=uri, init_uri=current_init_uri)
            )
            current_time += pending_duration

    return segments


def _download_segment(uri: str, dest: Path) -> None:
    """Downloads `uri` to `dest`, skipping entirely if `dest` already
    exists. Writes to a `.part` sibling first and only `os.replace()`s it
    into place on success — an atomic rename, so a segment file existing
    at `dest` always means it's fully downloaded, never a half-written
    leftover from an interrupted attempt. That's what makes a resumed
    fetch_chapter() call (via `cache_dir`) able to trust `dest.exists()`
    as "already have this one, skip it"."""
    if dest.exists():
        return
    part = dest.with_suffix(dest.suffix + ".part")
    last_error: Exception | None = None
    for _ in range(_SEGMENT_RETRIES):
        try:
            with urllib.request.urlopen(uri, timeout=30) as response, open(part, "wb") as f:
                shutil.copyfileobj(response, f)
            os.replace(part, dest)
            return
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Failed to download segment {uri!r} after {_SEGMENT_RETRIES} attempts") from last_error


def fetch_chapter(
    playlist_url: str,
    start: float,
    end: float,
    output_path: Path,
    max_workers: int = 4,
    progress_hook: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
    cache_dir: Path | None = None,
) -> None:
    """Download the HLS segments covering [start, end) and write the exact range to `output_path`.

    Fetches the overlapping segments concurrently (the actual speed win),
    concatenates them (prepending the playlist's `#EXT-X-MAP` init segment
    first if it has one — required for fMP4/CMAF-packaged VODs, a no-op for
    legacy MPEG-TS ones), then re-encodes just that slice with ffmpeg to
    trim precisely to `start`/`end` — matching the frame accuracy of
    yt-dlp's own slower section-download path. `progress_hook`, if given, is called with
    `{"status": "fetching_segments", "downloaded": N, "total": M}` as
    segments complete, then once with `{"status": "encoding"}` when the
    final ffmpeg trim starts. If `cancel_event` is given and gets set while
    this is running, raises `Cancelled` — queued segment fetches are
    dropped and in-flight ones are let finish (they're already most of the
    way done), rather than left to complete only to be thrown away.

    `cache_dir`, if given, makes the fetch resumable: segments are staged
    there (instead of an auto-cleaned temp directory) and, thanks to
    `_download_segment`'s atomic writes, a retry pointed at the same
    `cache_dir` skips every segment already on disk instead of re-fetching
    it. The directory is only removed after a fully successful encode — an
    interrupted or cancelled fetch leaves it in place on purpose, for the
    next attempt to pick up from. Without `cache_dir`, behavior is
    unchanged: segments live in a throwaway temp directory, nothing to
    resume from.
    """

    def _check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled("Cancelled by user")

    with urllib.request.urlopen(playlist_url, timeout=15) as response:
        playlist_text = response.read().decode("utf-8", errors="replace")

    base_url = playlist_url.rsplit("/", 1)[0] + "/"
    all_segments = parse_segments(playlist_text, base_url)
    overlapping = [s for s in all_segments if s.end > start and s.start < end]

    if not overlapping:
        raise ValueError(f"No segments found covering {start}-{end} in {playlist_url!r}")

    _check_cancelled()

    range_start = overlapping[0].start
    total = len(overlapping)
    completed = 0
    completed_lock = threading.Lock()
    init_uri = next((s.init_uri for s in overlapping if s.init_uri), None)

    def _download_and_report(uri: str, dest: Path) -> None:
        nonlocal completed
        _check_cancelled()
        _download_segment(uri, dest)
        if progress_hook is not None:
            with completed_lock:
                completed += 1
                n = completed
            progress_hook({"status": "fetching_segments", "downloaded": n, "total": total})

    def _run(tmp: Path) -> None:
        segment_paths = [tmp / f"{i:06d}.ts" for i in range(len(overlapping))]

        # fMP4 segments only decode correctly appended after their shared
        # init segment (the moov box lives there, not in any fragment) —
        # fetch it once, up front. Plain-TS playlists have no init_uri, so
        # this is a no-op for them. Goes through the same atomic
        # _download_segment as everything else, so it's cache_dir-resumable
        # too — a resumed attempt won't re-fetch it if it's already there.
        init_path: Path | None = None
        if init_uri is not None:
            init_path = tmp / "init.mp4"
            _download_segment(init_uri, init_path)

        pool = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = [
                pool.submit(_download_and_report, seg.uri, path) for seg, path in zip(overlapping, segment_paths)
            ]
            try:
                for future in futures:
                    future.result()  # re-raises on failure, including Cancelled
            except Exception:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
        finally:
            pool.shutdown(wait=True)

        _check_cancelled()

        if progress_hook is not None:
            progress_hook({"status": "encoding"})

        concat_path = tmp / "concat.ts"
        with open(concat_path, "wb") as out:
            if init_path is not None:
                with open(init_path, "rb") as part:
                    shutil.copyfileobj(part, out)
            for path in segment_paths:
                with open(path, "rb") as part:
                    shutil.copyfileobj(part, out)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(concat_path),
                "-ss",
                str(start - range_start),
                "-t",
                str(end - start),
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            check=True,
        )

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _run(cache_dir)
        shutil.rmtree(cache_dir, ignore_errors=True)  # only reached after a full success
    else:
        with tempfile.TemporaryDirectory(prefix="downstream-chapter-") as tmp_str:
            _run(Path(tmp_str))
