"""Kick CLI: check a Kick link and print its info, or download it.

Not reachable from a terminal in this project -- DownStream is GUI-only
there (see `downstream/cli.py`); `server.py`'s job dispatch calls
`info.py`/`download.py` directly, never this module's `main()`. Kept
importable for scripting/manual use outside the app proper:

    python -c "from Downloaders.kick.cli import main; main(['info', '<url>'])"
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from .download import download_video
from .info import ChannelOfflineError, InfoError, get_video_info
from .urls import LinkType, classify_url, extract_id


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a (live)"
    return str(timedelta(seconds=int(seconds)))


def _cmd_info(args: argparse.Namespace) -> int:
    link_type = classify_url(args.url)
    if link_type is LinkType.UNKNOWN:
        print(f"Invalid Kick link: {args.url}", file=sys.stderr)
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
    print(f"Views:      {info.view_count}")
    print(f"Upload date:{info.upload_date}")
    print(f"Live:       {info.is_live}")
    print(f"Quality:    {', '.join(info.qualities) or 'n/a'}")
    print(f"URL:        {info.webpage_url}")
    return 0


def _download(url: str, args: argparse.Namespace):
    return download_video(
        url,
        output_dir=args.output,
        quality=args.quality,
        concurrent_fragments=args.concurrent_fragments,
        cookies=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
    )


def _cmd_download(args: argparse.Namespace) -> int:
    link_type = classify_url(args.url)
    if link_type is LinkType.UNKNOWN:
        print(f"Invalid Kick link: {args.url}", file=sys.stderr)
        return 1

    print(f"Downloading {link_type.value}: {args.url}")

    try:
        result = _download(args.url, args)
    except ChannelOfflineError:
        # Unlike Twitch/YouTube, there's no "offer their latest VOD
        # instead" fallback here -- yt-dlp has no extractor for listing a
        # Kick channel's VODs, so there's nothing to offer. See
        # ChannelOfflineError's docstring in info.py for why.
        channel = extract_id(args.url)
        print(f"{channel or args.url} is offline.", file=sys.stderr)
        return 1
    except InfoError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Saved to: {result.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate, inspect, or download a Kick link.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cookies_help = "Path to a Netscape-format cookies.txt file for a logged-in Kick session (needed for sub-only VODs)"
    cookies_from_browser_help = (
        "Read cookies directly from an installed browser instead of a file, "
        "e.g. 'chrome', 'firefox:default-release'. Same BROWSER[+KEYRING][:PROFILE][::CONTAINER] "
        "syntax as yt-dlp's own --cookies-from-browser"
    )

    info_parser = subparsers.add_parser("info", help="Show info for a Kick link")
    info_parser.add_argument("url", help="A kick.com VOD, clip, or channel URL")
    info_parser.add_argument("-c", "--cookies", default=None, help=cookies_help)
    info_parser.add_argument("-b", "--cookies-from-browser", default=None, help=cookies_from_browser_help)
    info_parser.set_defaults(func=_cmd_info)

    download_parser = subparsers.add_parser("download", help="Download a Kick VOD, clip, or live stream")
    download_parser.add_argument("url", help="A kick.com VOD, clip, or channel URL")
    download_parser.add_argument(
        "-o", "--output", default=".", help="Directory to save the download in (default: current directory)"
    )
    download_parser.add_argument(
        "-q", "--quality", default="best", help="Quality label from info's Quality: line, or best/worst (default: best)"
    )
    download_parser.add_argument(
        "-N",
        "--concurrent-fragments",
        type=int,
        default=4,
        help="Number of video fragments to download concurrently (default: 4)",
    )
    download_parser.add_argument("-c", "--cookies", default=None, help=cookies_help)
    download_parser.add_argument("-b", "--cookies-from-browser", default=None, help=cookies_from_browser_help)
    download_parser.set_defaults(func=_cmd_download)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
