"""Twitch CLI: check a Twitch link and print its info, or download it.

Not reachable from a terminal in this project -- DownStream is GUI-only
there (see `downstream/cli.py`); `server.py`'s job dispatch calls
`info.py`/`download.py` directly, never this module's `main()`. Kept
importable for scripting/manual use outside the app proper:

    python -c "from Downloaders.twitch.cli import main; main(['info', '<url>'])"
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from .download import download_video
from .info import ChannelOfflineError, InfoError, get_latest_vod_url, get_video_info
from .urls import LinkType, classify_url, extract_id


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a (live)"
    return str(timedelta(seconds=int(seconds)))


def _format_timestamp(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def _print_muted_warning(muted_ranges: list[tuple[float, float]]) -> None:
    if not muted_ranges:
        return
    total = sum(end - start for start, end in muted_ranges)
    print(
        f"Warning: {len(muted_ranges)} segment(s) totaling {_format_timestamp(total)} are muted "
        f"(Twitch's copyright compliance system, not recoverable):"
    )
    for start, end in muted_ranges:
        print(f"  {_format_timestamp(start)}-{_format_timestamp(end)}")


def _print_chapters(chapters: list) -> None:
    if not chapters:
        return
    print("Chapters:")
    for i, chapter in enumerate(chapters, start=1):
        print(f"  {i}) {_format_timestamp(chapter.start)}-{_format_timestamp(chapter.end)}  {chapter.title}")


def _cmd_info(args: argparse.Namespace) -> int:
    link_type = classify_url(args.url)
    if link_type is LinkType.UNKNOWN:
        print(f"Invalid Twitch link: {args.url}", file=sys.stderr)
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
    _print_chapters(info.chapters)
    _print_muted_warning(info.muted_ranges)
    return 0


def _download(url: str, args: argparse.Namespace):
    return download_video(
        url,
        output_dir=args.output,
        quality=args.quality,
        concurrent_fragments=args.concurrent_fragments,
        cookies=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        chapter=args.chapter,
    )


def _offer_latest_vod(channel_url: str, args: argparse.Namespace):
    channel_login = extract_id(channel_url)
    vod_url = get_latest_vod_url(channel_login) if channel_login else None
    if not vod_url:
        print(f"{channel_login or channel_url} is offline and has no VODs available.", file=sys.stderr)
        return None

    try:
        answer = input(f"{channel_login} is offline, do you want to download their latest VOD instead? [y/N] ")
    except EOFError:
        answer = "n"

    if answer.strip().lower() not in ("y", "yes"):
        print("Skipping.")
        return None

    print(f"Downloading vod: {vod_url}")
    try:
        return _download(vod_url, args)
    except InfoError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None


def _cmd_download(args: argparse.Namespace) -> int:
    link_type = classify_url(args.url)
    if link_type is LinkType.UNKNOWN:
        print(f"Invalid Twitch link: {args.url}", file=sys.stderr)
        return 1

    print(f"Downloading {link_type.value}: {args.url}")

    try:
        result = _download(args.url, args)
    except ChannelOfflineError:
        result = _offer_latest_vod(args.url, args)
        if result is None:
            return 1
    except InfoError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Saved to: {result.path}")
    _print_muted_warning(result.muted_ranges)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate, inspect, or download a Twitch link.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cookies_help = (
        "Path to a Netscape-format cookies.txt file for a logged-in Twitch "
        "session (needed for sub-only VODs)"
    )
    cookies_from_browser_help = (
        "Read cookies directly from an installed browser instead of a file, "
        "e.g. 'chrome', 'firefox:default-release'. Same BROWSER[+KEYRING][:PROFILE][::CONTAINER] "
        "syntax as yt-dlp's own --cookies-from-browser"
    )

    info_parser = subparsers.add_parser("info", help="Show info for a Twitch link")
    info_parser.add_argument("url", help="A twitch.tv VOD, clip, or channel URL")
    info_parser.add_argument("-c", "--cookies", default=None, help=cookies_help)
    info_parser.add_argument("-b", "--cookies-from-browser", default=None, help=cookies_from_browser_help)
    info_parser.set_defaults(func=_cmd_info)

    download_parser = subparsers.add_parser("download", help="Download a Twitch VOD, clip, or live stream")
    download_parser.add_argument("url", help="A twitch.tv VOD, clip, or channel URL")
    download_parser.add_argument(
        "-o", "--output", default=".", help="Directory to save the download in (default: current directory)"
    )
    download_parser.add_argument(
        "-q", "--quality", default="best", help="Format/quality selector passed to yt-dlp (default: best)"
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
    download_parser.add_argument(
        "--chapter",
        type=int,
        default=None,
        metavar="N",
        help="Download only chapter N (1-based, see info's Chapters: list) instead of the whole VOD",
    )
    download_parser.set_defaults(func=_cmd_download)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
