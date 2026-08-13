"""Heroic Games Launcher (Epic, GOG, Amazon) -> Pegasus metadata.

Ported from a generator that ran against a real library for months; the
properties it earned the hard way are kept and marked below. Re-run it any
time: it is idempotent, so art already on disk is never fetched twice.

Heroic keeps a cache of each store's library as JSON, which is the whole
source of truth here -- no network calls to the stores themselves, only to
their art CDNs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import pegasus

log = logging.getLogger(__name__)

#: Where Heroic keeps its config, native package first, then Flatpak.
CONFIG_CANDIDATES = (
    "~/.config/heroic",
    "~/.var/app/com.heroicgameslauncher.hgl/config/heroic",
)

DEFAULT_OUT = "~/Games/pegasus-heroic"
DEFAULT_COLLECTION = "Heroic"

#: Heroic registers a URL handler, so entries launch through it rather than
#: through an executable path -- which means this works for Epic, GOG and
#: Amazon titles alike, and keeps working when Heroic moves the install.
LAUNCH_TEMPLATE = 'xdg-open "heroic://launch?appName={app_name}&runner={runner}"'

#: Art downloads fail transiently. Retrying an entry beats aborting the run.
RETRIES = 3
TIMEOUT = 30

#: Some CDNs answer a bare urllib request with a 403.
USER_AGENT = "Mozilla/5.0"

#: Bytes that start a real image. Launcher CDNs return HTML error pages with a
#: 200 status, and an HTML file saved as .jpg is a broken cover in the
#: frontend forever, because the next run sees the file and skips it.
SIGNATURES = (
    (0, b"\xff\xd8\xff"),  # JPEG
    (0, b"\x89PNG\r\n\x1a\n"),  # PNG
    (0, b"GIF8"),  # GIF
    (8, b"WEBP"),  # WebP, after a RIFF header
)

#: Fetches a URL and returns its bytes. Injected in tests, which must never
#: reach the network.
Fetch = Callable[[str], bytes]


@dataclass(frozen=True)
class Options:
    """Everything the generator needs to know. Nothing is read from globals."""

    config_dir: Path | None = None
    out_dir: Path = Path(DEFAULT_OUT).expanduser()
    collection: str = DEFAULT_COLLECTION
    dry_run: bool = False

    @classmethod
    def resolve(
        cls,
        config_dir: Path | None = None,
        out_dir: Path | None = None,
        collection: str | None = None,
        dry_run: bool = False,
    ) -> Options:
        """Flags first, then the environment, then the defaults.

        Resolution lives here rather than in the argument parser because the
        parser is built on every ``padmode`` invocation, and importing this
        module to describe its own flags would drag the generator into the
        daemon's startup -- the exact thing CLAUDE.md §2 forbids.
        """
        return cls(
            config_dir=config_dir or _env_path("HEROIC_CONFIG_DIR"),
            out_dir=(
                out_dir
                or _env_path("PEGASUS_HEROIC_OUT")
                or Path(DEFAULT_OUT).expanduser()
            ),
            collection=collection or DEFAULT_COLLECTION,
            dry_run=dry_run,
        )


def find_config_dir(explicit: Path | None = None) -> Path | None:
    """Heroic's config directory, or None if it isn't installed here."""
    candidates = (
        [explicit] if explicit else [Path(c).expanduser() for c in CONFIG_CANDIDATES]
    )
    for candidate in candidates:
        if (candidate / "store_cache").is_dir():
            return candidate
    return None


def installed_games(config_dir: Path) -> list[dict]:
    """Every installed game across the three store caches.

    One file per store (``legendary`` for Epic, ``gog``, ``nile`` for Amazon).
    A store you don't use is present but empty, and a store cache that is
    unreadable is skipped with a warning rather than failing the run -- one
    broken file should not cost you the other two libraries.
    """
    games: list[dict] = []
    for path in sorted((config_dir / "store_cache").glob("*_library.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("skipping %s: %s", path.name, exc)
            continue
        # Heroic has written both shapes: a list, and a dict wrapping one.
        if isinstance(data, dict):
            library = data.get("library", [])
        elif isinstance(data, list):
            library = data
        else:
            library = []
        games += [
            game
            for game in library
            if isinstance(game, dict) and game.get("is_installed")
        ]
    return games


def sanitize(name: object) -> str:
    """A filename-safe key. App ids are opaque strings, not always tidy ones."""
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(name))


def looks_like_image(data: bytes) -> bool:
    """True if these bytes start like an image we can trust."""
    return any(
        data[offset:offset + len(sig)] == sig for offset, sig in SIGNATURES
    )


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        return response.read()


def download(
    url: str | None,
    dest: Path,
    *,
    fetch: Fetch = _fetch,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = RETRIES,
) -> bool:
    """Fetch art unless it is already there. True if the file exists after.

    Validated before it is saved, because a CDN error page arrives with a 200
    status and would otherwise be cached on disk as a permanently broken cover.
    """
    if not url:
        return False
    if dest.exists() and dest.stat().st_size > 0:
        return True
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            data = fetch(url)
            if not looks_like_image(data):
                raise ValueError(f"not an image ({len(data)} bytes)")
            dest.write_bytes(data)
            return True
        except Exception as exc:  # noqa: BLE001 -- any failure is retryable
            last = exc
            if attempt < retries:
                sleep(1.5 * attempt)
    log.warning("art failed: %s (%s)", dest.name, last)
    return False


def collect(
    games: list[dict], options: Options, *, fetch: Fetch = _fetch
) -> tuple[list[pegasus.Game], list[str]]:
    """Turn raw library entries into metadata, fetching art as it goes.

    Returns the entries and a list of art that could not be fetched, so the
    caller can say so and the user knows re-running will retry it.
    """
    art_dir = options.out_dir / "art"
    games_dir = options.out_dir / "games"
    entries: list[pegasus.Game] = []
    failures: list[str] = []

    for game in sorted(games, key=lambda g: _title(g).lower()):
        app_name = game.get("app_name")
        title = _title(game)
        if not app_name:
            log.warning("skipping %r: no app_name", title)
            continue
        key = sanitize(app_name)
        cover = art_dir / f"{key}_cover.jpg"
        background = art_dir / f"{key}_bg.jpg"

        if options.dry_run:
            have_cover = bool(game.get("art_square"))
            have_background = bool(game.get("art_cover"))
        else:
            (games_dir / f"{key}.heroic").touch(exist_ok=True)
            # art_square is the portrait box front; art_cover is the landscape
            # background. The names are the launcher's, and they read backwards.
            have_cover = download(game.get("art_square"), cover, fetch=fetch)
            have_background = download(game.get("art_cover"), background, fetch=fetch)
            for wanted, got, what in (
                (game.get("art_square"), have_cover, "cover"),
                (game.get("art_cover"), have_background, "background"),
            ):
                if wanted and not got:
                    failures.append(f"{title}: {what}")

        description = (game.get("extra") or {}).get("about", {}).get("description")
        entries.append(pegasus.Game(
            title=title,
            file=f"games/{key}.heroic",
            launch=LAUNCH_TEMPLATE.format(
                app_name=app_name, runner=game.get("runner") or "legendary"
            ),
            developer=game.get("developer") or None,
            # Some stores repeat the title as the description. Saying it twice
            # on screen looks like a bug in the theme.
            description=description if description != title else None,
            box_front=f"art/{cover.name}" if have_cover else None,
            background=f"art/{background.name}" if have_background else None,
        ))
        print(f"  + {title} ({game.get('runner') or 'legendary'})")
    return entries, failures


def _title(game: dict) -> str:
    return pegasus.escape(game.get("title") or game.get("app_name") or "")


def generate(options: Options, *, fetch: Fetch = _fetch) -> int:
    """Run the generator. Returns a process exit status."""
    config_dir = find_config_dir(options.config_dir)
    if config_dir is None:
        looked = options.config_dir or " or ".join(CONFIG_CANDIDATES)
        log.error(
            "no Heroic config found (looked in %s). Use --heroic-dir if it "
            "lives somewhere else.", looked,
        )
        return 1
    print(f"Heroic config  : {config_dir}")

    games = installed_games(config_dir)
    print(f"installed games: {len(games)}")
    if not games:
        log.error("nothing installed, so there is nothing to write")
        return 1

    if not options.dry_run:
        for directory in (options.out_dir / "art", options.out_dir / "games"):
            directory.mkdir(parents=True, exist_ok=True)

    entries, failures = collect(games, options, fetch=fetch)
    if options.dry_run:
        print(pegasus.render(entries, options.collection))
        return 0

    path = pegasus.write(entries, options.out_dir, options.collection)
    print(f"\nwrote {path}")
    if failures:
        log.warning("art that could not be fetched (re-run to retry):")
        for failure in failures:
            log.warning("  - %s", failure)
    # The generator writes files but cannot register the directory: that lives
    # in Pegasus's own settings, and only the user can put it there.
    print(f"\nIn Pegasus: Settings -> Set game directories -> add:\n  "
          f"{options.out_dir}")
    return 0


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def main(args: argparse.Namespace) -> int:
    return generate(Options.resolve(
        config_dir=args.heroic_dir,
        out_dir=args.out,
        collection=args.collection,
        dry_run=args.dry_run,
    ))
