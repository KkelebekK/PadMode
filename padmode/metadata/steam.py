"""Steam -> Pegasus metadata.

Reads the library -- where Steam lives, which library folders it has, which
apps are installed in them -- tells the games from the tools Steam installs
beside them, copies each game's art out of Steam's cache, and writes a Pegasus
collection. ``padmode metadata steam``; see ``docs/metadata-generators.md``.

Steam describes each installed app in an ``appmanifest_<appid>.acf`` file, in
Valve's KeyValues text format (:mod:`.vdf`). Nothing here talks to Steam or
the network: the manifests and the art cache are the whole source of truth.
Re-run it any time: it is idempotent, and art that has not changed is not
copied again.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import stat
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import pegasus, vdf
from .images import looks_like_image

log = logging.getLogger(__name__)

DEFAULT_OUT = "~/Games/pegasus-steam"
DEFAULT_COLLECTION = "Steam"

#: Steam registers a handler for ``steam://`` URLs (``steam.desktop``, here),
#: so games launch through it rather than through an executable -- the same
#: shape as Heroic's, and indifferent to which library the game is in.
LAUNCH_TEMPLATE = 'xdg-open "steam://rungameid/{appid}"'

#: Where Steam lives. ``~/.steam/root`` first: Steam keeps that link pointing
#: at wherever it is really installed, which is the question being asked. The
#: usual native location and the Flatpak follow, in case the link is missing.
STEAM_CANDIDATES = (
    "~/.steam/root",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
)

#: The ``StateFlags`` bit that means the app's files are all on disk. Other
#: bits track updates and downloads, so a manifest existing is not the same as
#: the app being installed. Test the bit, never compare: an installed app with
#: an update waiting reads 6, not 4 (measured: Proton Experimental, here).
FULLY_INSTALLED = 4

#: Steam's art cache, under its own directory. A library on another drive has
#: no cache of its own: every app's art is here, wherever the app is installed.
ART_CACHE = "appcache/librarycache"

#: The file names Steam gives each piece of art, by role. Measured over the
#: 572 app directories in the cache here:
#:
#: - the portrait cover is ``library_600x900.jpg`` (199 apps) or the newer
#:   ``library_capsule.jpg`` (73), never both for one app. Both files are
#:   300x450 -- the size in the first name is not the size of the file;
#: - the background is ``library_hero.jpg``, 1920x620;
#: - the logo is ``logo.png``, 640x360;
#: - the header is ``library_header.jpg`` or ``header.jpg``, 460x215.
#:
#: The ``<sha1>.jpg`` that sits beside them is a 32x32 icon, not art.
ART_NAMES = {
    "cover": ("library_600x900.jpg", "library_capsule.jpg"),
    "background": ("library_hero.jpg",),
    "logo": ("logo.png",),
    "header": ("library_header.jpg", "header.jpg"),
}

#: The file a compatibility tool keeps in its install directory, telling Steam
#: how to run games *through* it. Measured here: every Proton build and every
#: Steam Linux Runtime has one (8 of 8), and no game does (0 of 7). It catches
#: the next Proton without anyone adding its name or appid to a list.
TOOL_MANIFEST = "toolmanifest.vdf"

#: Apps that are not games and carry no structural sign of it, by appid, with
#: the reason reported when one is skipped. Defaults, merged with the user's
#: ``[metadata.steam.ignore]`` -- see :func:`skip_reason`.
DEFAULT_IGNORE: dict[int, str] = {
    228980: "Steamworks redistributables",
}

#: Valve names a game's server "<game> Dedicated Server". It installs like any
#: app and has no tool manifest; one was installed on this machine (its
#: directory is still in ``steamapps/common``).
SERVER_SUFFIX = " dedicated server"


@dataclass(frozen=True)
class App:
    """One app with a manifest, as the manifest describes it."""

    appid: int
    name: str
    #: Its directory under ``steamapps/common``. Empty if the manifest lacks it.
    installdir: str
    #: The library folder holding it -- the directory that contains
    #: ``steamapps/``, which is the main Steam directory or a second drive.
    library: Path
    state_flags: int

    @property
    def installed(self) -> bool:
        return bool(self.state_flags & FULLY_INSTALLED)


def find_steam_dir(explicit: Path | None = None) -> Path | None:
    """Steam's own directory, symlinks resolved, or None if it isn't here."""
    candidates = (
        [explicit] if explicit else [Path(c).expanduser() for c in STEAM_CANDIDATES]
    )
    for candidate in candidates:
        if (candidate / "steamapps").is_dir():
            return candidate.resolve()
    return None


def library_dirs(steam_dir: Path) -> list[Path]:
    """Every library folder Steam knows about, its own directory first.

    Games installed to another drive are not under ``steam_dir`` at all; the
    only record of where they went is ``steamapps/libraryfolders.vdf``. Each
    numbered entry is a block holding ``path``. An older layout maps the number
    straight to the path and mixes in bookkeeping keys that are not numbers --
    not seen on this machine, but cheap to accept, so it is.

    A missing or unreadable file costs the other libraries, not Steam's own:
    that one is always returned. A library whose directory is gone (an
    unmounted drive) is skipped with a warning, because its games could not
    launch anyway.
    """
    found = [steam_dir]
    path = steam_dir / "steamapps" / "libraryfolders.vdf"
    try:
        data = vdf.load(path)
    except FileNotFoundError:
        return found
    except (OSError, vdf.VDFError) as exc:
        log.warning("skipping %s, reading only %s: %s", path, steam_dir, exc)
        return found

    seen = {steam_dir.resolve()}
    folders = data.get("libraryfolders")
    if not isinstance(folders, dict):
        log.warning("%s has no libraryfolders block", path)
        return found
    for key, entry in folders.items():
        if not key.isdigit():
            continue
        raw = _text(entry, "path") if isinstance(entry, dict) else entry
        if not raw:
            continue
        library = Path(raw)
        if not (library / "steamapps").is_dir():
            log.warning("library folder %s is not there, skipping it", library)
            continue
        real = library.resolve()
        if real not in seen:
            seen.add(real)
            found.append(library)
    return found


def read_manifest(path: Path, library: Path) -> App | None:
    """One ``appmanifest_*.acf``, or None (with a warning) if it is unusable."""
    try:
        data = vdf.load(path)
    except (OSError, vdf.VDFError) as exc:
        log.warning("skipping %s: %s", path.name, exc)
        return None
    state = data.get("appstate")
    if not isinstance(state, dict):
        state = {}
    appid = _text(state, "appid")
    if not appid.isdigit():
        log.warning("skipping %s: no numeric appid", path.name)
        return None
    installdir = _text(state, "installdir")
    return App(
        appid=int(appid),
        # A manifest can be written before Steam knows the name. The directory
        # is a better label than nothing, and the appid is better than that.
        name=_text(state, "name") or installdir or f"Steam app {appid}",
        installdir=installdir,
        library=library,
        state_flags=_int(_text(state, "stateflags")),
    )


def installed_apps(steam_dir: Path) -> list[App]:
    """Every fully installed app across every library folder.

    Nothing is filtered yet beyond "installed": Proton, the Linux runtimes and
    the redistributables are all still here, because to Steam they are apps
    like any other.

    An app with manifests in two libraries -- a stale copy left behind by a
    move -- is listed once, from the first library that has it.
    """
    apps: dict[int, App] = {}
    for library in library_dirs(steam_dir):
        for path in sorted((library / "steamapps").glob("appmanifest_*.acf")):
            app = read_manifest(path, library)
            if app is None:
                continue
            if not app.installed:
                log.info("not fully installed, skipping: %s", app.name)
                continue
            apps.setdefault(app.appid, app)
    return list(apps.values())


@dataclass(frozen=True)
class Art:
    """Where each piece of an app's art is. None where Steam has none cached."""

    cover: Path | None = None
    background: Path | None = None
    logo: Path | None = None
    header: Path | None = None


def find_art(steam_dir: Path, appid: int) -> Art:
    """Locate an app's art in Steam's cache. Nothing is copied or downloaded.

    Three layouts, and one app can mix the first two (measured: a cover in a
    hashed subdirectory, its background loose beside it):

    - ``<appid>/<name>``, loose in the app's directory;
    - ``<appid>/<hash>/<name>``, one subdirectory per file, which is how
      Steam stores anything it has fetched recently -- every
      ``library_capsule.jpg`` here is stored this way;
    - ``<appid>_<name>``, flat in the cache. What most guides still describe;
      not one file on this machine is stored like that, but it costs a stat.

    An empty file is passed over: it is what Steam leaves while it is still
    writing. Where one role has several files -- not seen here, where no app
    has two -- the newest wins, since the older is the one left behind.
    """
    cache = steam_dir / ART_CACHE
    app_dir = cache / str(appid)
    found = {}
    for role, names in ART_NAMES.items():
        candidates: list[Path] = []
        for name in names:
            candidates.append(app_dir / name)
            candidates.extend(sorted(app_dir.glob(f"*/{name}")))
            candidates.append(cache / f"{appid}_{name}")
        found[role] = _newest(candidates)
    return Art(**found)


def _newest(paths: list[Path]) -> Path | None:
    """The most recently written non-empty file among these, if any exist."""
    best: tuple[float, Path] | None = None
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            continue
        # Strictly newer, so a tie keeps the earlier layout in the list.
        if best is None or info.st_mtime > best[0]:
            best = (info.st_mtime, path)
    return best[1] if best else None


def is_tool(app: App) -> bool:
    """True if Steam runs games through this app (Proton, the runtimes)."""
    if not app.installdir:
        return False
    install = app.library / "steamapps" / "common" / app.installdir
    return (install / TOOL_MANIFEST).is_file()


def skip_reason(app: App, ignore: Mapping[int, bool]) -> str | None:
    """Why this app is not a game, or None if it is one.

    ``ignore`` is the user's ``[metadata.steam.ignore]``, and it is asked
    first, because it overrides everything below it in both directions:
    ``true`` hides any app, ``false`` shows one that a default or a check
    would have hidden. That is how bindings work too -- good defaults, and
    ``false`` to take one back.
    """
    if app.appid in ignore:
        return "ignored in config" if ignore[app.appid] else None
    if app.appid in DEFAULT_IGNORE:
        return DEFAULT_IGNORE[app.appid]
    if is_tool(app):
        return "compatibility tool"
    if app.name.lower().endswith(SERVER_SUFFIX):
        return "dedicated server"
    return None


def split_games(
    apps: Iterable[App], ignore: Mapping[int, bool]
) -> tuple[list[App], list[tuple[App, str]]]:
    """The games, and everything else with the reason it was left out.

    The skipped list is returned rather than logged so the caller can show it:
    an app hidden by mistake should be one line of output away from being
    noticed, not missing without a trace.
    """
    games: list[App] = []
    skipped: list[tuple[App, str]] = []
    for app in apps:
        reason = skip_reason(app, ignore)
        if reason is None:
            games.append(app)
        else:
            skipped.append((app, reason))
    return games, skipped


def load_ignore(path: Path) -> dict[int, bool]:
    """``[metadata.steam.ignore]`` from padmode's config file.

    Read here rather than by the daemon's config loader, which ignores the
    ``[metadata]`` table entirely. A typo in the ignore list should stop this
    generator with a message, not stop the daemon -- the pad has nothing to do
    with it. A missing file is an empty list.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except ValueError as exc:  # TOMLDecodeError and UnicodeDecodeError both
        raise ValueError(f"{path} is not valid TOML: {exc}") from exc
    try:
        return parse_ignore(data)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def parse_ignore(data: Mapping) -> dict[int, bool]:
    """Validate the ignore list in already-parsed TOML. Strict, like the config.

    ::

        [metadata.steam.ignore]
        228980 = false    # show the redistributables after all
        1234567 = true    # hide this one
    """
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("[metadata] must be a table")
    section = metadata.get("steam", {})
    if not isinstance(section, dict):
        raise ValueError("[metadata.steam] must be a table")
    unknown = sorted(set(section) - {"ignore"})
    if unknown:
        raise ValueError(
            f"[metadata.steam]: unknown key {', '.join(map(repr, unknown))}. "
            "Known: ignore"
        )
    table = section.get("ignore", {})
    if not isinstance(table, dict):
        raise ValueError(
            "metadata.steam.ignore must be a table of appid = true/false, e.g.\n"
            "  [metadata.steam.ignore]\n  228980 = false"
        )
    ignore: dict[int, bool] = {}
    for key, value in table.items():
        if not key.isdigit():
            raise ValueError(
                f"metadata.steam.ignore: {key!r} is not an appid. Use the "
                "number, which is in the app's store URL and its manifest name"
            )
        if not isinstance(value, bool):
            raise ValueError(
                f"metadata.steam.ignore.{key} must be true (hide it) or "
                "false (show it even if padmode would hide it)"
            )
        ignore[int(key)] = value
    return ignore


def _text(block: dict, key: str) -> str:
    """A string value, or "" -- including where a block sits instead."""
    value = block.get(key)
    return value if isinstance(value, str) else ""


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


@dataclass(frozen=True)
class Options:
    """Everything the generator needs to know. Nothing is read from globals."""

    steam_dir: Path | None = None
    out_dir: Path = Path(DEFAULT_OUT).expanduser()
    collection: str = DEFAULT_COLLECTION
    dry_run: bool = False
    #: padmode's config file, read for ``[metadata.steam.ignore]`` only.
    #: None means no ignore list at all, which is what the tests want.
    config_path: Path | None = None

    @classmethod
    def resolve(
        cls,
        steam_dir: Path | None = None,
        out_dir: Path | None = None,
        collection: str | None = None,
        dry_run: bool = False,
        config_path: Path | None = None,
    ) -> Options:
        """Flags first, then the environment, then the defaults.

        Here rather than in the argument parser for the reason given in
        :meth:`heroic.Options.resolve`: the parser is built on every
        ``padmode`` invocation, and the daemon must not import this module.
        """
        return cls(
            steam_dir=steam_dir or _env_path("PADMODE_STEAM_DIR"),
            out_dir=(
                out_dir
                or _env_path("PEGASUS_STEAM_OUT")
                or Path(DEFAULT_OUT).expanduser()
            ),
            collection=collection or DEFAULT_COLLECTION,
            dry_run=dry_run,
            config_path=config_path,
        )


def copy_art(source: Path | None, dest: Path) -> bool:
    """Copy one piece of art out of Steam's cache. True if ``dest`` is usable.

    Copied rather than pointed at: Steam's cache is not permanent, and a
    hashed directory changes name when the art is updated, so a reference
    into it can go stale between runs. A copy beside the metadata cannot, and
    keeps the generated library movable, as Heroic's is.

    Copying has its own failure, the one :mod:`.images` describes: a file
    caught mid-write. So the copy lands beside ``dest`` first and replaces it
    only once it looks like an image -- a broken new version never replaces a
    good old one. Art whose size and mtime match the copy (``copy2`` keeps
    the mtime) is left alone, which is what makes re-running cheap.
    """
    if source is None:
        return False
    partial = dest.with_name(dest.name + ".part")
    try:
        src = source.stat()
        if dest.is_file():
            have = dest.stat()
            if (have.st_size, have.st_mtime_ns) == (src.st_size, src.st_mtime_ns):
                return True
        shutil.copy2(source, partial)
        with partial.open("rb") as copied:
            if not looks_like_image(copied.read(16)):
                raise ValueError("not an image")
        os.replace(partial, dest)
        return True
    except (OSError, ValueError) as exc:
        log.warning("art failed: %s (%s)", dest.name, exc)
        return dest.is_file()
    finally:
        partial.unlink(missing_ok=True)


def collect(
    games: list[App], options: Options, steam_dir: Path
) -> tuple[list[pegasus.Game], list[str]]:
    """Turn games into metadata, copying art as it goes.

    Returns the entries and a note for every game left without a cover or a
    background, so the caller can say which, rather than the frontend showing
    a blank tile with no explanation.
    """
    art_dir = options.out_dir / "art"
    games_dir = options.out_dir / "games"
    entries: list[pegasus.Game] = []
    notes: list[str] = []

    for app in sorted(games, key=lambda a: a.name.lower()):
        art = find_art(steam_dir, app.appid)
        found = {"cover": art.cover, "background": art.background}
        dests = {
            role: art_dir / f"{app.appid}_{role}{source.suffix}"
            for role, source in found.items()
            if source is not None
        }
        if options.dry_run:
            have = {role: role in dests for role in found}
        else:
            (games_dir / f"{app.appid}.steam").touch(exist_ok=True)
            have = {
                role: role in dests and copy_art(found[role], dests[role])
                for role in found
            }
        for role, source in found.items():
            if source is None:
                notes.append(f"{app.name}: no {role} in Steam's cache")
            elif not have[role]:
                notes.append(f"{app.name}: {role} could not be copied")

        entries.append(pegasus.Game(
            title=app.name,
            file=f"games/{app.appid}.steam",
            launch=LAUNCH_TEMPLATE.format(appid=app.appid),
            box_front=f"art/{dests['cover'].name}" if have["cover"] else None,
            background=(
                f"art/{dests['background'].name}" if have["background"] else None
            ),
        ))
        print(f"  + {app.name} ({app.appid})")
    return entries, notes


def generate(options: Options) -> int:
    """Run the generator. Returns a process exit status."""
    steam_dir = find_steam_dir(options.steam_dir)
    if steam_dir is None:
        looked = options.steam_dir or " or ".join(STEAM_CANDIDATES)
        log.error(
            "no Steam found (looked in %s). Use --steam-dir if it lives "
            "somewhere else.", looked,
        )
        return 1
    print(f"Steam          : {steam_dir}")

    try:
        ignore = load_ignore(options.config_path) if options.config_path else {}
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    apps = installed_apps(steam_dir)
    games, skipped = split_games(apps, ignore)
    print(f"installed apps : {len(apps)}, games: {len(games)}")
    # Every skip is shown, with its reason: a game hidden by mistake should be
    # one line away from being noticed, and one ignore entry from coming back.
    for app, reason in sorted(skipped, key=lambda pair: pair[0].name.lower()):
        print(f"  - {app.name} ({app.appid}): {reason}")
    if not games:
        # Overwriting good metadata with an empty collection because Steam was
        # mid-update is worse than doing nothing -- the Heroic rule.
        log.error("no games installed, so there is nothing to write")
        return 1

    if not options.dry_run:
        for directory in (options.out_dir / "art", options.out_dir / "games"):
            directory.mkdir(parents=True, exist_ok=True)

    entries, notes = collect(games, options, steam_dir)
    if options.dry_run:
        print(pegasus.render(entries, options.collection))
        return 0

    path = pegasus.write(entries, options.out_dir, options.collection)
    print(f"\nwrote {path}")
    for note in notes:
        log.warning("%s", note)
    print(f"\nIn Pegasus: Settings -> Set game directories -> add:\n  "
          f"{options.out_dir}")
    # Measured: Pegasus ships a Steam data source of its own, on by default
    # here, and it knows nothing about the filter. With both on, every game
    # is listed twice. Its settings are the user's, so we say it, not do it.
    print("and turn off Pegasus's own Steam data source, or every game is "
          "listed twice\n  (providers.steam.enabled: false in its settings.txt)")
    return 0


def main(args: argparse.Namespace) -> int:
    from ..config import default_path

    return generate(Options.resolve(
        steam_dir=args.steam_dir,
        out_dir=args.out,
        collection=args.collection,
        dry_run=args.dry_run,
        config_path=args.config or default_path(),
    ))


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None
