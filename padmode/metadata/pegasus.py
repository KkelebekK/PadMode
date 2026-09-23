"""Writing Pegasus metadata files.

Split from the scrapers because the launcher and the frontend are independent
choices: the Steam generator writes this same format from a completely
different source, and a second frontend would want a different writer from
the same scrape.

Pegasus accepts several filenames for this file. We write
``metadata.pegasus.txt`` and only that one -- the format documentation lists
alternatives, and a codebase that disagrees with its own docs about which one
it writes costs somebody an afternoon.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: The one filename we write. See the module docstring before changing it.
METADATA_FILE = "metadata.pegasus.txt"


@dataclass(frozen=True)
class Game:
    """One entry in a Pegasus collection.

    Paths are stored relative to the metadata file's own directory, which is
    what makes a generated library movable.
    """

    title: str
    #: Shell command Pegasus runs. Everything here launches through the
    #: launcher's own URL handler rather than an executable path.
    launch: str
    #: A real file that has to exist: Pegasus hides entries whose file is
    #: missing when ``general.verify-files`` is on, which is the default.
    file: str
    developer: str | None = None
    description: str | None = None
    box_front: str | None = None
    background: str | None = None

    def render(self) -> str:
        lines = [
            f"game: {escape(self.title)}",
            f"file: {self.file}",
            f"launch: {self.launch}",
        ]
        for key, value in (
            ("developer", self.developer),
            ("description", self.description),
            ("assets.box_front", self.box_front),
            ("assets.background", self.background),
        ):
            if value:
                lines.append(f"{key}: {escape(value)}")
        return "\n".join(lines)


def escape(value: object) -> str:
    """Collapse a value onto one line.

    Metadata values are single-line, and store descriptions routinely contain
    newlines. A description that wraps silently turns the rest of itself into
    unparseable keys.
    """
    return " ".join(str(value).split())


def render(games: list[Game], collection: str) -> str:
    """The whole metadata file, as text."""
    body = "\n\n".join(game.render() for game in games)
    return f"collection: {collection}\n\n\n{body}\n"


def write(games: list[Game], out_dir: Path, collection: str) -> Path:
    """Write the metadata file and return where it went."""
    path = out_dir / METADATA_FILE
    path.write_text(render(games, collection), encoding="utf-8")
    return path
