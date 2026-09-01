"""Registry of available site downloaders, and URL-based dispatch between them.

Adding a new site means: build it as its own subpackage under `Downloaders/`
exposing `matches(url) -> bool`, `get_video_info(url, **options)`, and
`download_video(url, **options)` (see `Downloaders/twitch/__init__.py` for
the reference shape), then add its module path to `_SITE_MODULES` below.
`find_site()` is only ever used by `server.py`'s job dispatch -- DownStream
is GUI-only from a terminal (see `cli.py`), so a site's own `main(argv)`
(its `info`/`download` subcommands) isn't part of this contract; each site
keeps one anyway, reachable by importing its `cli.py` directly, but nothing
in this project calls it.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

_SITE_MODULES = [
    "Downloaders.twitch",
    "Downloaders.youtube",
    "Downloaders.kick",
    "Downloaders.instagram",
]


def site_names() -> list[str]:
    """Short names of every registered site, for error messages/help text."""
    return [name.rsplit(".", 1)[-1] for name in _SITE_MODULES]


def find_site(url: str) -> ModuleType | None:
    """Return the site module that handles `url`, or None if none do."""
    for module_name in _SITE_MODULES:
        site = import_module(module_name)
        if site.matches(url):
            return site
    return None
