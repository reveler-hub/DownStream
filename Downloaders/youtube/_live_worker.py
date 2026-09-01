"""Standalone worker for a `--live-from-start` YouTube channel capture --
run as a real child process rather than an in-process yt-dlp call, so it
can be killed outright rather than merely asked nicely.

Why this exists: a continuous, long-running live channel's
`--live-from-start` walk-back can block for a long, unbounded time
*inside yt-dlp's own extraction call*, before a single `progress_hook`
fires — confirmed directly against a real 24/7 stream (NASA's ISS feed),
where over 50 seconds passed with zero hook calls and `extract_info`
never returning. `cancel_event` has nothing to check against during that
phase, since it's only ever consulted from inside a progress hook. A real
OS process, unlike an in-process call, can be interrupted regardless of
what it's blocked doing internally.

Talks to its parent over stdout: one `KIND {json}` line per event, so the
parent can relay progress and learn the outcome without needing anything
more than a pipe.
"""

from __future__ import annotations

import json
import pickle
import sys

import yt_dlp


def _emit(kind: str, payload: dict) -> None:
    print(f"{kind} {json.dumps(payload)}", flush=True)


def main() -> int:
    with open(sys.argv[1], "rb") as f:
        job = pickle.load(f)
    url = job["url"]
    ydl_opts = job["ydl_opts"]

    def hook(d: dict) -> None:
        # Only relay JSON-safe scalars -- yt-dlp's progress dict can carry
        # things like a live `_TimeIt`/ctx object that json.dumps chokes on.
        safe = {k: v for k, v in d.items() if isinstance(v, (str, int, float, bool, type(None)))}
        _emit("PROGRESS", safe)

    ydl_opts["progress_hooks"] = [hook]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(url, download=True)
            if data and "entries" in data:
                entries = list(data.get("entries") or [])
                data = entries[0] if entries else None
            if data is None:
                _emit("ERROR", {"message": f"No info returned for {url!r}"})
                return 1
            final_path = ydl.prepare_filename(data)
        _emit("RESULT", {"path": final_path})
        return 0
    except KeyboardInterrupt:
        # SIGINT from the parent (its cancellation signal) lands here --
        # yt-dlp itself may or may not have left a clean partial file
        # depending on exactly where this landed; nothing more to do than
        # report it and let the parent decide the job is cancelled.
        _emit("CANCELLED", {})
        return 130
    except Exception as exc:
        _emit("ERROR", {"message": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
