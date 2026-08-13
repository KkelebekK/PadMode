"""The on-screen keyboard (v0.5).

Two halves that must not be confused:

* :mod:`padmode.osk.protocol` and :mod:`padmode.osk.layout` are stdlib-only.
  The daemon imports the protocol, so nothing reachable from here may pull in a
  GUI toolkit -- the daemon's dependencies are stdlib + ``python-evdev``
  (CLAUDE.md §2), and a missing toolkit must disable one feature rather than
  stop the daemon starting.
* :mod:`padmode.osk.renderer` draws, needs GTK4 and ``gtk4-layer-shell``, and
  runs as its own process (``python -m padmode.osk``). The daemon spawns it and
  never imports it. ``tests/test_osk.py`` fails if that stops being true.
"""

from __future__ import annotations

import ctypes.util
import os

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
