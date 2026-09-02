# DownStream

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

A multi-site video downloader with a browser GUI, for Twitch, YouTube,
Kick, and Instagram (posts/reels/IGTV and stories — video only, not
photos). Paste a link, pick a quality and a folder, download — or watch a
Twitch/YouTube/Kick channel and have it auto-download the moment it goes
live (Instagram has no channel/live concept, so Auto Watch doesn't apply
there). Built on
`yt-dlp` under the hood, but the point of DownStream is the GUI (and
`--remote`, for running it headless on a NAS/SBC controlled from a GUI
elsewhere) — if all you want is a bare command-line downloader, `yt-dlp`
itself already does that.

<img width="1920" height="1034" alt="New_job" src="https://github.com/user-attachments/assets/9dd2d90f-be02-493d-b6f5-5c6fdd31ee3f" />
<img width="1919" height="936" alt="DownStream-GUI" src="https://github.com/user-attachments/assets/733b7a7f-fd63-4bce-b3a9-20d45c91f8b2" />





**[Full documentation is on the wiki](https://github.com/reveler-hub/DownStream/wiki)**
— the GUI in full, the `--remote` HTTP API, per-site details and
limitations, cookies, and auto-watch. This README is just setup and a
quick start.

## Setup

```
git clone https://github.com/reveler-hub/DownStream.git
cd DownStream
python3 -m venv DownStream_Venv
DownStream_Venv/bin/pip install -r requirements.txt   # Windows: DownStream_Venv\Scripts\pip install -r requirements.txt
```

That's the whole setup — once `DownStream_Venv/` exists next to this
README, every entry point (`python -m downstream ...`, `DownStream-GUI.py`)
auto-relaunches itself into it, no matter which Python actually started it
or whether that Python has any of this installed. Just double-click
`DownStream-GUI.py` and it works even from a totally bare system Python,
and there's nothing to activate or remember. If
`DownStream_Venv/` doesn't exist yet, the relaunch fails with the exact
two commands above instead of a confusing traceback.

YouTube additionally needs [`deno`](https://deno.com) installed and on
`PATH` — current YouTube extraction requires running some of the site's own
JS to solve signature/PO-token challenges, and `deno` is the JS runtime
`yt-dlp-ejs` (installed into the venv above) executes it through. Kick's
API sits behind Cloudflare, and yt-dlp's Kick extractor requests
browser-TLS impersonation on every call — `curl_cffi` (also installed into
the venv above) is the backend that provides it. Twitch needs `ffmpeg`
(also used for YouTube's video+audio merge and every site's live-download
handling). Instagram needs cookies for almost everything — not just extra
content the way Twitch/Kick's cookies unlock sub-only VODs, but plain
public posts too once you hit Instagram's login wall, and always for
stories. See the **[Cookies From Browser](https://github.com/reveler-hub/DownStream/wiki/Cookies-From-Browser)**
wiki page.

## Usage

### GUI

Double-click **`DownStream-GUI.py`** at the repo root — no terminal
needed. It starts a local server and opens DownStream's control panel in
your default browser automatically. See the
**[GUI Guide](https://github.com/reveler-hub/DownStream/wiki/GUI-Guide)**
for the New Job form, the Jobs page, Auto Watch, the folder picker, and
what happens when you cancel a download mid-capture.

If you'd rather start it from a terminal (same thing, just explicit about
host/port/token):

```
python3 -m downstream --gui [--host HOST] [--port PORT] [--token TOKEN]
```

### Remote control (`--remote`)

The other real command-line use: running DownStream headless on a NAS/SBC,
controlled from a GUI (or `curl`, or anything else) on another machine.

```
python3 -m downstream --remote [--host HOST] [--port PORT] [--token TOKEN]
```

See the **[Remote API](https://github.com/reveler-hub/DownStream/wiki/Remote-API)**
page for the full HTTP+JSON API.

## Layout

See **[Architecture](https://github.com/reveler-hub/DownStream/wiki/Architecture)**
on the wiki for the module-by-module breakdown.
