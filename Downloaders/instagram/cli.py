"""Instagram CLI: check an Instagram link and print its info, or download it.

Not reachable from a terminal in this project -- DownStream is GUI-only
there (see `downstream/cli.py`); `server.py`'s job dispatch calls
`info.py`/`download.py` directly, never this module's `main()`. Kept
importable for scripting/manual use outside the app proper:

    python -c "from Downloaders.instagram.cli import main; main(['info', '<url>'])"
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from .download import DownloadCancelledError, download_video
from .info import InfoError, get_video_info
from .urls import LinkType, classify_url


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    return str(timedelta(seconds=int(seconds)))


def _cmd_info(args: argparse.Namespace) -> int:
    link_type = classify_url(args.url)
    if link_type is LinkType.UNKNOWN:
        print(f"Invalid Instagram link: {args.url}", file=sys.stderr)
        return 1

    print(f"Link type: {link_type.value}")

    try:
        info = get_video_info(args.url, cookies=args.cookies, cookies_from_browser=args.cookies_from_browser)
    except InfoError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Title:      {info.title}")
    print(f"Uploader:   {info.uploader}")
    print(f"Duration:   {_format_duration(info.duration_seconds)}")
    print(f"Upload date:{info.upload_date}")
    print(f"URL:        {info.webpage_url}")
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    link_type = classify_url(args.url)
    if link_type is LinkType.UNKNOWN:
        print(f"Invalid Instagram link: {args.url}", file=sys.stderr)
        return 1

    print(f"Downloading {link_type.value}: {args.url}")

    try:
        result = download_video(
            args.url,
            output_dir=args.output,
            cookies=args.cookies,
            cookies_from_browser=args.cookies_from_browser,
        )
    except DownloadCancelledError as exc:
        print(f"Cancelled: {exc}", file=sys.stderr)
        return 1
    except InfoError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for path in result.paths:
        print(f"Saved to: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate, inspect, or download an Instagram link.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cookies_help = (
        "Path to a Netscape-format cookies.txt file for a logged-in Instagram session "
        "(needed for almost everything here, including public posts once you hit Instagram's login wall, and always for stories)"
    )
    cookies_from_browser_help = (
        "Read cookies directly from an installed browser instead of a file, "
        "e.g. 'chrome', 'firefox:default-release'. Same BROWSER[+KEYRING][:PROFILE][::CONTAINER] "
        "syntax as yt-dlp's own --cookies-from-browser"
    )

    info_parser = subparsers.add_parser("info", help="Show info for an Instagram link")
    info_parser.add_argument("url", help="An instagram.com post/reel/tv or stories URL")
    info_parser.add_argument("-c", "--cookies", default=None, help=cookies_help)
    info_parser.add_argument("-b", "--cookies-from-browser", default=None, help=cookies_from_browser_help)
    info_parser.set_defaults(func=_cmd_info)

    download_parser = subparsers.add_parser(
        "download", help="Download an Instagram post/reel/IGTV, or a story (single item or the whole active tray)"
    )
    download_parser.add_argument("url", help="An instagram.com post/reel/tv or stories URL")
    download_parser.add_argument(
        "-o", "--output", default=".", help="Directory to save the download in (default: current directory)"
    )
    download_parser.add_argument("-c", "--cookies", default=None, help=cookies_help)
    download_parser.add_argument("-b", "--cookies-from-browser", default=None, help=cookies_from_browser_help)
    download_parser.set_defaults(func=_cmd_download)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
