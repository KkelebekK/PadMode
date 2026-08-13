"""The window backend interface, and the subprocess plumbing under it.

Deliberately tiny (CLAUDE.md §3.3). Three operations carry overlay mode:

    find_window(window_class) -> WindowId | None
    activate_window(window_id) -> None
    get_active_window() -> WindowId | None

Three more are optional and default to "cannot": two for ``park_pointer``, one
so ``padmode windows`` can tell a user what to write in their config. A
backend that leaves them alone simply disables those extras. Resist adding
anything else: every *required* operation has to be implemented by every future
community backend, and the three above are the ones overlay mode cannot be
built without.

Backends are external commands (``kdotool``, ``xdotool``), not libraries, so
the whole layer is argv in and text out. That keeps it testable without a
compositor: pass a fake runner and assert on the argv.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Opaque window handle. KWin gives UUIDs in braces, X11 gives decimal ids, and
#: nothing above this layer is allowed to care which.
WindowId = str

#: Runs argv, returns stdout, or None if the command failed. Injected in tests.
Runner = Callable[[list[str]], "str | None"]

#: Window tools answer in milliseconds. A timeout this short exists only so a
#: wedged tool cannot take the daemon's event loop down with it.
TIMEOUT = 2.0


@dataclass(frozen=True)
class Rect:
    """Window geometry in screen coordinates."""

    x: int
    y: int
    width: int
    height: int

    @property
    def centre(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def contains(self, x: int, y: int) -> bool:
        """Is this point inside the window?"""
        return (
            self.x <= x < self.x + self.width
            and self.y <= y < self.y + self.height
        )


def run_tool(argv: list[str], timeout: float = TIMEOUT) -> str | None:
    """Run a window tool and return its stdout, or None if it failed.

    Failure is normal, not exceptional: ``getactivewindow`` exits non-zero when
    nothing is focused, and a search for a window that isn't running succeeds
    with no output. Callers distinguish "no answer" from "no window" only where
    it matters, so both arrive as None or an empty string.
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        log.warning("%s timed out after %.1fs", argv[0], timeout)
        return None
    except OSError as exc:
        log.warning("could not run %s: %s", argv[0], exc)
        return None
    if proc.returncode != 0:
        log.debug("%s failed (%d): %s", " ".join(argv), proc.returncode,
                  proc.stderr.strip())
        return None
    return proc.stdout


class WindowBackend:
    """What overlay mode needs from a compositor.

    The base class implements the two optional operations as "cannot", so a
    backend only overrides them if the underlying tool can actually do it.
    """

    #: Short id used in config (``[windows] backend``) and in logs.
    name = "none"

    def find_window(self, window_class: str) -> WindowId | None:
        """First window whose class matches, or None if there is no such window."""
        raise NotImplementedError

    def activate_window(self, window_id: WindowId) -> None:
        """Focus and raise a window."""
        raise NotImplementedError

    def get_active_window(self) -> WindowId | None:
        """The focused window, or None if nothing is focused."""
        raise NotImplementedError

    # -- optional: only park_pointer uses these ------------------------
    def window_geometry(self, window_id: WindowId) -> Rect | None:
        """Where a window is, or None if this backend cannot say."""
        return None

    def pointer_position(self) -> tuple[int, int] | None:
        """Where the cursor is, or None if this backend cannot say.

        Reading the cursor is what makes parking it possible at all: our
        virtual pointer is a *relative* device and no compositor here exposes a
        warp, so the only way to land on a target is to move, look, and correct.
        """
        return None

    # -- optional: diagnostics only ------------------------------------
    def window_class(self, window_id: WindowId) -> str | None:
        """Class of a window, or None if this backend cannot say.

        Used by ``padmode windows`` and nothing else. "Which string do I put in
        ``class``?" is the first question anyone configuring an overlay has,
        and answering it inside the tool beats telling them to go and learn a
        window tool's command line.
        """
        return None

    @property
    def can_park(self) -> bool:
        """True if this backend implements both optional operations."""
        cls = type(self)
        return (
            cls.window_geometry is not WindowBackend.window_geometry
            and cls.pointer_position is not WindowBackend.pointer_position
        )


def first_line(out: str | None) -> str | None:
    """First non-blank line of a tool's output, or None."""
    if not out:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def parse_shell(out: str | None) -> dict[str, str]:
    """Parse the ``KEY=value`` form that xdotool's ``--shell`` flag emits."""
    values: dict[str, str] = {}
    for line in (out or "").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key:
            values[key.strip()] = value.strip().strip('"')
    return values


def as_coord(value: str | None) -> int | None:
    """A screen coordinate as a whole number of pixels, or None.

    Parses through ``float`` on purpose. KWin reports geometry in *logical*
    coordinates, which on a scaled display are not integers -- a real window
    here measured ``1520x813.5999999999999``. Parsing that with ``int()``
    raises, and the failure is invisible: geometry comes back None and cursor
    parking quietly does nothing on exactly the windows that need it.
    """
    try:
        return round(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
