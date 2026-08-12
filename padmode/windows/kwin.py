"""KWin backend, via ``kdotool``.

KWin exposes no window control to ordinary Wayland clients, so there is no
library call to make here: ``kdotool`` reaches the compositor by loading a KWin
script over D-Bus, which is the only sanctioned route. Every operation is one
short-lived process (a few milliseconds on a warm system).

This backend is used on **KDE Wayland**. On an X11 session the ``x11`` backend
is preferred instead -- it is the better-trodden path there and does not need
to inject anything into the compositor.
"""

from __future__ import annotations

import logging

from .backend import (
    Rect,
    Runner,
    WindowBackend,
    WindowId,
    as_coord,
    first_line,
    run_tool,
)

log = logging.getLogger(__name__)

TOOL = "kdotool"


class KwinBackend(WindowBackend):
    """Window operations against a running KWin."""

    name = "kwin"

    def __init__(self, runner: Runner | None = None, tool: str = TOOL) -> None:
        self._run = runner or run_tool
        self._tool = tool

    def find_window(self, window_class: str) -> WindowId | None:
        """First window whose class matches ``window_class`` as a regex.

        ``kdotool search`` matches case-insensitively unless told otherwise, so
        a config saying ``class = "discord"`` finds a window whose class is
        ``Discord``. That is the forgiving behaviour a config file wants.
        """
        ids = _ids(self._run([self._tool, "search", "--class", window_class]))
        if len(ids) > 1:
            log.debug("%d windows match class %r; using the first",
                      len(ids), window_class)
        return ids[0] if ids else None

    def get_active_window(self) -> WindowId | None:
        return _window_id(first_line(self._run([self._tool, "getactivewindow"])))

    def activate_window(self, window_id: WindowId) -> None:
        self._run([self._tool, "windowactivate", window_id])

    def window_class(self, window_id: WindowId) -> str | None:
        return first_line(
            self._run([self._tool, "getwindowclassname", window_id])
        )

    def window_geometry(self, window_id: WindowId) -> Rect | None:
        """Parse the human-readable geometry report.

        ``kdotool`` has no ``--shell`` form for this, so the three-line prose
        output is all there is:

            Window {8f3c...}
              Position: 1544,42
              Geometry: 2544x1390

        On a scaled screen the numbers are *logical* coordinates and come back
        fractional -- ``1520x813.5999999999999`` from a real window here --
        which is why they are parsed with :func:`as_coord` rather than ``int``.
        """
        out = self._run([self._tool, "getwindowgeometry", window_id])
        position = size = None
        for line in (out or "").splitlines():
            key, _, value = line.strip().partition(":")
            if key == "Position":
                position = _pair(value, ",")
            elif key == "Geometry":
                size = _pair(value, "x")
        if position is None or size is None:
            return None
        return Rect(position[0], position[1], size[0], size[1])

    def pointer_position(self) -> tuple[int, int] | None:
        """Read the cursor from ``x:2684 y:1238 screen:1 window:{...}``."""
        line = first_line(self._run([self._tool, "getmouselocation"]))
        fields: dict[str, str] = {}
        for field in (line or "").split():
            key, sep, value = field.partition(":")
            if sep:
                fields[key] = value
        x, y = as_coord(fields.get("x")), as_coord(fields.get("y"))
        return None if x is None or y is None else (x, y)


def _ids(out: str | None) -> list[WindowId]:
    found = (_window_id(line.strip()) for line in (out or "").splitlines())
    return [window for window in found if window is not None]


def _window_id(line: str | None) -> WindowId | None:
    """KWin ids are brace-wrapped UUIDs; anything else is a stray message."""
    if line and line.startswith("{") and line.endswith("}"):
        return line
    return None


def _pair(value: str, separator: str) -> tuple[int, int] | None:
    left, sep, right = value.strip().partition(separator)
    if not sep:
        return None
    first, second = as_coord(left), as_coord(right)
    return None if first is None or second is None else (first, second)
