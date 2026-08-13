"""X11 backend, via ``xdotool``.

Used on any X11 session, including KDE's -- ``xdotool`` talks to the X server
directly and does not have to inject a script into the compositor.

``xdotool`` alone covers all five operations, so ``wmctrl`` is not used. It can
activate and enumerate windows but cannot report the cursor, which would leave
``park_pointer`` half-implemented for no gain.

**Untested against a live X11 session.** The maintainer's machine runs KDE
Wayland, so this backend is covered by unit tests at the argv-and-parsing level
only; ``docs/window-backends.md`` carries the manual checklist for anyone who
can run it for real.
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
    parse_shell,
    run_tool,
)

log = logging.getLogger(__name__)

TOOL = "xdotool"


class X11Backend(WindowBackend):
    """Window operations against an X server."""

    name = "x11"

    def __init__(self, runner: Runner | None = None, tool: str = TOOL) -> None:
        self._run = runner or run_tool
        self._tool = tool

    def find_window(self, window_class: str) -> WindowId | None:
        """First mapped window whose ``WM_CLASS`` matches, as a regex.

        ``--onlyvisible`` matters more here than it looks: X11 applications
        keep unmapped utility and icon windows around that share the class, and
        activating one of those focuses nothing the user can see.
        """
        ids = _ids(
            self._run(
                [self._tool, "search", "--onlyvisible", "--class", window_class]
            )
        )
        if len(ids) > 1:
            log.debug("%d windows match class %r; using the first",
                      len(ids), window_class)
        return ids[0] if ids else None

    def get_active_window(self) -> WindowId | None:
        return _window_id(first_line(self._run([self._tool, "getactivewindow"])))

    def activate_window(self, window_id: WindowId) -> None:
        self._run([self._tool, "windowactivate", window_id])

    def minimize_window(self, window_id: WindowId) -> bool:
        return self._run([self._tool, "windowminimize", window_id]) is not None

    def window_class(self, window_id: WindowId) -> str | None:
        # xdotool prints the literal "(null)" and still exits 0 when a window
        # has no WM_CLASS, which is not a class name anybody wants echoed back.
        name = first_line(self._run([self._tool, "getwindowclassname", window_id]))
        return None if name in (None, "(null)") else name

    def window_geometry(self, window_id: WindowId) -> Rect | None:
        values = parse_shell(
            self._run([self._tool, "getwindowgeometry", "--shell", window_id])
        )
        fields = [as_coord(values.get(key)) for key in ("X", "Y", "WIDTH", "HEIGHT")]
        if any(field is None for field in fields):
            return None
        x, y, width, height = fields
        return Rect(x, y, width, height)  # type: ignore[arg-type]

    def pointer_position(self) -> tuple[int, int] | None:
        values = parse_shell(self._run([self._tool, "getmouselocation", "--shell"]))
        x, y = as_coord(values.get("X")), as_coord(values.get("Y"))
        return None if x is None or y is None else (x, y)


def _ids(out: str | None) -> list[WindowId]:
    found = (_window_id(line.strip()) for line in (out or "").splitlines())
    return [window for window in found if window is not None]


def _window_id(line: str | None) -> WindowId | None:
    """X11 ids are decimal. Anything else is a diagnostic on stdout."""
    return line if line and line.isdigit() else None
