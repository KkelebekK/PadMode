"""The on-screen keyboard.

Two halves that must not be confused:

* :mod:`padmode.osk.protocol`, :mod:`padmode.osk.client`,
  :mod:`padmode.osk.keys` and :mod:`padmode.osk.model` are stdlib-only. The
  daemon imports the first two, so nothing reachable from them may pull in a
  GUI toolkit -- the daemon's dependencies are stdlib + ``python-evdev``
  (CLAUDE.md §2), and a missing toolkit must disable one feature rather than
  stop the daemon starting.
* :mod:`padmode.osk.surface`, :mod:`padmode.osk.view`, :mod:`padmode.osk.app`
  and :mod:`padmode.osk.focus` draw and watch. They need GTK4 and
  ``gtk4-layer-shell``, and run as their own process
  (``python -m padmode.osk``). The daemon spawns it and never imports them.
  ``tests/test_osk.py`` fails if that stops being true.

The keys and the grid state are stdlib rather than GTK on purpose: the layout
lives in exactly one place, and that place is testable without a compositor,
which is where nearly all of the interesting logic can be covered (CLAUDE.md
§5).
"""

from __future__ import annotations

import ctypes.util
import os
import sys
import time


def log(message: str) -> None:
    """One line to the renderer's stderr, which journald collects.

    Sizing depends on compositor timing we cannot reproduce in a test, so the
    decisions are traceable in the log rather than only inferrable from what
    ends up on screen (CLAUDE.md §4).

    Timestamped, because *when* turned out to matter as much as *what*: a
    keyboard that opens at the previous screen's size and one that corrects
    itself a moment later produce identical lines in a different order, and
    without a clock there is no way to tell them apart.

    Here rather than in one of the drawing modules so that ``surface``,
    ``view`` and ``app`` can all reach it without importing each other. The
    daemon imports this package for :func:`preload_library` and never calls
    this.
    """
    print(f"osk: [{time.monotonic():9.3f}] {message}", file=sys.stderr, flush=True)


#: Checked in order when the loader cannot name the library for us. Distros
#: that split it elsewhere fall back to ``osk.preload`` in config.
_PRELOAD_FALLBACKS = (
    "/usr/lib/libgtk4-layer-shell.so",
    "/usr/lib64/libgtk4-layer-shell.so",
    "/usr/local/lib/libgtk4-layer-shell.so",
)


def preload_library() -> str | None:
    """Path or soname to put in ``LD_PRELOAD`` for the renderer, if we can.

    gtk4-layer-shell cannot hook GDK when PyGObject ``dlopen``s it after GTK
    has already started, so the renderer only gets a layer surface if the
    library is preloaded. Measured on Plasma 6.7 / GTK 4.22: without this,
    ``is_supported()`` is false even though the compositor advertises
    ``zwlr_layer_shell_v1`` v5. Note it must be the main library --
    ``liblayer-shell-preload.so``, shipped for exactly this purpose, does not
    work here.

    Returns None when the library is not installed, which is the signal to
    disable the keyboard with one log line rather than spawn a renderer that
    will draw an ordinary window over the middle of the screen.
    """
    found = ctypes.util.find_library("gtk4-layer-shell")
    if found:
        return found
    for path in _PRELOAD_FALLBACKS:
        if os.path.exists(path):
            return path
    return None
