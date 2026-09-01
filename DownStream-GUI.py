#!/usr/bin/env bash
""":"
exec python3 "$0" "$@"
":"""

"""Double-click entry point: opens DownStream's GUI in your browser.

On Linux/Mac, the block above is a self-relaunching shell/Python polyglot
(the same pattern Chaturdown uses) — it makes this file directly runnable
(`./DownStream-GUI.py`, or double-clicked in a file manager that respects
the execute bit) without the caller needing to know to type `python3`
first. It works because `exec python3 "$0" "$@"` re-invokes this same file
under python3, which then sees those same four lines as nothing more than
an inert triple-quoted string and skips straight past them.

On Windows this block does nothing at all — double-clicking a `.py` file
there goes through the standard `.py` -> python.exe file association set
up by the official python.org installer, not through any shebang.

Either way, once *some* Python is running, the SELF-RELAUNCH block below
makes sure it's specifically DownStream_Venv's Python (same two-layer
pattern as Chaturdown/DownTube: the polyglot above only gets Unix into
*a* python3; this second, plain-Python step is what actually lands in the
venv, and it's the only mechanism that does anything at all on Windows).
Only once that's settled does this call into `downstream --gui`: starts
the local control-panel server and opens it in your default browser.

A double-click launch has no terminal attached, so without help, any
startup failure (missing dependency, port already in use, etc.) would
just silently fail -- nothing would open and there'd be nothing to go on.
Any exception after the venv relaunch is instead written to
DownStream-GUI-error.log next to this script, so a failed launch still
leaves something to debug.
"""

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")

_SCRIPT_DIR = Path(__file__).resolve().parent
_ERROR_LOG = _SCRIPT_DIR / "DownStream-GUI-error.log"
_VENV_DIR = _SCRIPT_DIR / "DownStream_Venv"

# ============================================================
# SELF-RELAUNCH -- always run under DownStream_Venv's own Python, no
# matter how this script was started (double-click, system Python, a bare
# `python DownStream-GUI.py`, etc). Must happen before any non-stdlib
# import (yt-dlp, curl_cffi, etc. only exist inside the venv).
# ============================================================
if IS_WINDOWS:
    _target_python = _VENV_DIR / "Scripts" / "python.exe"
    _already_there = Path(sys.executable).resolve() == _target_python.resolve()
else:
    _target_python = _VENV_DIR / "bin" / "python3"
    _already_there = Path(sys.prefix).resolve() == _VENV_DIR.resolve()

if not _already_there:
    if not _target_python.exists():
        print(f"DownStream_Venv not found at {_target_python}")
        print(
            "Create it with:\n"
            "  python3 -m venv DownStream_Venv\n"
            "  DownStream_Venv/bin/pip install -r requirements.txt   "
            "(Windows: DownStream_Venv\\Scripts\\pip install -r requirements.txt)"
        )
        if IS_WINDOWS:
            input("\nPress Enter to close this window...")
        sys.exit(1)
    os.execv(str(_target_python), [str(_target_python), str(Path(__file__).resolve()), *sys.argv[1:]])

sys.path.insert(0, str(_SCRIPT_DIR))


def _log_startup_error() -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] Failed to start:\n{traceback.format_exc()}\n")
    except OSError:
        pass  # can't even write the log -- nothing more we can do here


if __name__ == "__main__":
    try:
        from downstream.cli import main

        raise SystemExit(main(["--gui", *sys.argv[1:]]))
    except SystemExit:
        raise
    except BaseException:
        _log_startup_error()
        raise
