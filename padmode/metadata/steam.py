"""Steam -> Pegasus metadata.

**Partly built.** What is here reads the library: where Steam lives, which
library folders it has, and which apps are installed in them. Art, filtering
out runtimes and tools, and writing the Pegasus file are specified in
``docs/metadata-generators.md`` and not built yet, so this is not reachable
from ``padmode metadata``.

Steam describes each installed app in an ``appmanifest_<appid>.acf`` file, in
Valve's KeyValues text format (:mod:`.vdf`). Nothing here talks to Steam or
the network: the manifests are the whole source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import vdf

log = logging.getLogger(__name__)

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


def _text(block: dict, key: str) -> str:
    """A string value, or "" -- including where a block sits instead."""
    value = block.get(key)
    return value if isinstance(value, str) else ""


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0
