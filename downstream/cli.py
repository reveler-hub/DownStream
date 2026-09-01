"""Core CLI: start the server -- the only thing this project runs directly from a terminal.

    python -m downstream --remote [--host HOST] [--port PORT] [--token TOKEN]
    python -m downstream --gui [--host HOST] [--port PORT] [--token TOKEN]

DownStream is GUI-first, deliberately: there's no `info`/`download`
terminal command for a URL, on any site -- `--remote` and `--gui` both just
start the same HTTP server (see `server.py`) that the browser GUI actually
talks to. `--remote` is meant for a headless box (NAS/SBC) being driven
from elsewhere, so it binds every interface and doesn't open a browser;
`--gui` is meant for sitting down at this machine directly, so it binds
only to localhost and opens DownStream's control panel in the default
browser automatically. Each site's own `info`/`download` logic still lives
in its `info.py`/`download.py` -- reached through the server, from
`server.py`'s `_run_job`, never from a terminal.
"""

from __future__ import annotations

import argparse
import sys


def _run_server_mode(argv: list[str], flag: str, default_host: str, open_browser: bool) -> int:
    from .server import _DEFAULT_WATCH_INTERVAL, run_server

    parser = argparse.ArgumentParser(prog=f"downstream {flag}")
    parser.add_argument(flag, action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--host", default=default_host, help=f"Interface to listen on (default: {default_host})")
    parser.add_argument("--port", type=int, default=8420, help="Port to listen on (default: 8420)")
    parser.add_argument(
        "--token", default=None, help="Auth token clients must send (default: a random one is generated and printed)"
    )
    parser.add_argument(
        "--watch-interval",
        type=float,
        default=_DEFAULT_WATCH_INTERVAL,
        metavar="SECONDS",
        help=f"How often to check auto-watched channels for a live stream (default: {_DEFAULT_WATCH_INTERVAL:.0f}s)",
    )
    args = parser.parse_args(argv)

    run_server(
        host=args.host, port=args.port, token=args.token, open_browser=open_browser, watch_interval=args.watch_interval
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    if "--remote" in argv:
        return _run_server_mode(argv, "--remote", default_host="0.0.0.0", open_browser=False)
    if "--gui" in argv:
        return _run_server_mode(argv, "--gui", default_host="127.0.0.1", open_browser=True)

    print("DownStream is GUI-based -- there's no terminal info/download command.", file=sys.stderr)
    print("Usage: python -m downstream --gui [--host HOST] [--port PORT] [--token TOKEN]", file=sys.stderr)
    print("       python -m downstream --remote [--host HOST] [--port PORT] [--token TOKEN]", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
