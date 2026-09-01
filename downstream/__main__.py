"""Entry point for `python -m downstream ...` / `python -m downstream --remote|--gui`.

============================================================
SELF-RELAUNCH -- always run under this project's own venv Python, no
matter which Python the caller started with. Must happen before any
non-stdlib import (yt-dlp, curl_cffi, etc. only exist inside the venv).
Same pattern as Chaturdown/DownTube's self-relaunch block, adapted for a
`-m package` entry point instead of a standalone script file.
============================================================
"""

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")

_repo_root = Path(__file__).resolve().parent.parent
_venv_dir = _repo_root / "DownStream_Venv"
if IS_WINDOWS:
    _target_python = _venv_dir / "Scripts" / "python.exe"
    _already_there = Path(sys.executable).resolve() == _target_python.resolve()
else:
    _target_python = _venv_dir / "bin" / "python3"
    _already_there = Path(sys.prefix).resolve() == _venv_dir.resolve()

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
    os.execv(str(_target_python), [str(_target_python), "-m", "downstream", *sys.argv[1:]])

from .cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
