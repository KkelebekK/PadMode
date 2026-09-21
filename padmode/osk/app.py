"""The renderer process: the daemon's messages, the grid, and the screen.

``python -m padmode.osk`` lands here. Nothing else does: this is the only
module that owns all four pieces at once -- :mod:`~padmode.osk.model` (what the
keyboard *is*), :mod:`~padmode.osk.view` (what it looks like),
:mod:`~padmode.osk.surface` (where it sits) and the line-JSON channel to the
daemon -- and keeping that wiring in one file is what lets the other three stay
testable, or drawable, on their own.

Never imported by the daemon (CLAUDE.md §2); it talks over stdin and stdout
using :mod:`padmode.osk.protocol`, and its stderr is inherited so GTK's
warnings land in journald with everything else.

**Must be started with ``LD_PRELOAD`` pointing at ``libgtk4-layer-shell.so``.**
Measured on Plasma 6.7 / GTK 4.22: without it ``is_supported()`` is false even
though KWin advertises ``zwlr_layer_shell_v1`` v5, because the library cannot
hook GDK when PyGObject ``dlopen``s it after the fact. The dedicated
``liblayer-shell-preload.so`` shipped alongside does *not* work; the main
library does. :func:`padmode.osk.preload_library` finds it.
"""

from __future__ import annotations

import os
import sys

# Before GTK is imported by anything below: for a keyboard that never animates
# the software renderer is faster to first frame and far lighter -- measured at
# 7 ms and 85 MB against 104 ms and 224 MB for the Vulkan default. Overridable,
# so a user on a GTK that has dropped it can still get a keyboard.
os.environ.setdefault("GSK_RENDERER", "cairo")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LS  # noqa: E402

from . import log, model, protocol  # noqa: E402
from . import view as view_module  # noqa: E402
from .surface import Surface  # noqa: E402

#: Default share of the screen height, when started without ``--height``.
#: The daemon always passes one; this is for running the renderer by hand.
DEFAULT_HEIGHT = 30.0


class App:
    """The keyboard, the panel it is drawn on, and the daemon's channel."""

    def __init__(self, keyboard: model.Keyboard, view, surface: Surface) -> None:
        self.keyboard = keyboard
        self.view = view
        self.surface = surface
        #: Focus watcher, when auto-open is on. Set by :func:`main`.
        self.watcher = None
        self._buffer = b""

    # -- messages in ---------------------------------------------------
    def feed(self, data: bytes) -> None:
        """Consume whatever arrived on stdin, handling partial lines."""
        self._buffer += data
        while b"\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition(b"\n")
            parsed = protocol.decode(line)
            if parsed is not None:
                self.on_message(*parsed)

    def on_message(self, kind: str, fields: dict) -> None:
        if kind == protocol.SHOW:
            self._on_show(fields.get("hints"))
        elif kind == protocol.HIDE:
            self._put_away()
        elif kind == protocol.ASK_FOCUS:
            self._answer_focus()
        elif kind == protocol.STEP:
            direction = fields.get("dir")
            if isinstance(direction, str):
                self.keyboard.step(direction)
                self.view.refresh()
        elif kind == protocol.MOVE:
            dx, dy = fields.get("dx"), fields.get("dy")
            if isinstance(dx, (int, float)) and isinstance(dy, (int, float)):
                self.surface.move(float(dx), float(dy))
        elif kind == protocol.ACT:
            name = fields.get("name")
            if isinstance(name, str) and name in protocol.ACTIONS:
                self._act(name)

    def _on_show(self, hints) -> None:
        if isinstance(hints, dict):
            fresh = {str(k): str(v) for k, v in hints.items() if isinstance(v, str)}
            if fresh != self.view.hints:
                # Badges are built with the widgets, and the hints arrive with
                # the first show -- so the grid has to be rebuilt once they
                # land, or the very first keyboard has no badges.
                self.view.hints = fresh
                self._rebuild()
        # Shift is per-visit; where the highlight sits is not, so reopening the
        # keyboard does not throw away where you were.
        self.keyboard.shift = model.SHIFT_OFF
        self.view.refresh()
        self.surface.show()

    def _act(self, name: str) -> None:
        if name == "recenter":
            # The only action that moves the panel rather than the highlight,
            # so it is the surface's and never reaches the grid.
            self.surface.recenter()
            return
        was_layer = self.keyboard.layer
        emit = self.keyboard.act(name)
        if self.keyboard.layer != was_layer:
            self._rebuild()
        else:
            self.view.refresh()
        self.keyboard.dirty = False
        if emit is not None:
            self.send(emit)

    def _rebuild(self) -> None:
        """Redraw the grid, then put the panel back inside the screen.

        Always paired: a rebuilt grid is a new measurement, and the corner the
        panel was clamped against described the old one.
        """
        self.view.rebuild()
        self.surface.reflow()

    def _answer_focus(self) -> None:
        if self.watcher is None:
            return
        # Two different questions, and which one to answer depends on whether
        # we are already up. Closed: "is there something to type into?" Open:
        # "is the field we opened for still there?" The second cannot be asked
        # as the first -- the panel and the desktop have editable fields of
        # their own, so "anything typeable anywhere" is answered yes forever.
        editable = (
            self.watcher.field_still_focused()
            if self.surface.visible
            else self.watcher.current_editable()
        )
        write(protocol.encode(protocol.FOCUS, editable=editable))

    # -- messages out --------------------------------------------------
    def send(self, emit: model.Emit) -> None:
        if emit.kind == protocol.TYPE:
            write(protocol.encode(protocol.TYPE, text=emit.text))
        elif emit.kind == protocol.KEY:
            write(protocol.encode(protocol.KEY, keys=list(emit.keys)))
        elif emit.kind == protocol.HOLD:
            write(protocol.encode(
                protocol.HOLD, keys=list(emit.keys), down=emit.down
            ))
        elif emit.kind == protocol.CLOSED:
            self._put_away()
            write(protocol.encode(protocol.CLOSED))

    def _put_away(self) -> None:
        """Leave the screen, letting go of anything we were holding."""
        for emit in self.keyboard.release_mods():
            self.send(emit)
        self.view.refresh()
        self.surface.hide()


def write(payload: bytes) -> None:
    """Write one message to the daemon, flushing so nothing waits in a buffer."""
    try:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    except (BrokenPipeError, ValueError):
        # The daemon is gone. Nothing to report it to.
        pass


def _height_from(argv: list[str]) -> float:
    for arg in argv:
        if arg.startswith("--height="):
            try:
                return float(arg.split("=", 1)[1])
            except ValueError:
                return DEFAULT_HEIGHT
    return DEFAULT_HEIGHT


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    edge = "top" if "--top" in argv else "bottom"

    Gtk.init()
    if not LS.is_supported():
        write(protocol.encode(
            protocol.FAILED,
            reason="layer-shell unavailable -- start this with "
                   "LD_PRELOAD=libgtk4-layer-shell.so on a Wayland session "
                   "whose compositor supports zwlr_layer_shell_v1",
        ))
        return 1

    display = Gdk.Display.get_default()
    view_module.install_css(display)
    keyboard = model.Keyboard()
    view = view_module.View(keyboard)
    view.attach_sizing(display)
    view.rebuild()
    surface = Surface(view, edge=edge, height_pct=_height_from(argv))
    app = App(keyboard, view, surface)
    # Map once, unseen: it warms the GTK renderer and learns the output size,
    # so the user's first keyboard is already the right size in the right
    # place instead of arriving and then correcting itself.
    surface.prewarm()

    loop = GLib.MainLoop()

    def on_stdin(fd: int, _condition: int) -> bool:
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if not data:
            loop.quit()  # daemon gone: EOF is the whole liveness mechanism
            return False
        app.feed(data)
        return True

    GLib.unix_fd_add_full(
        GLib.PRIORITY_DEFAULT, sys.stdin.fileno(), GLib.IOCondition.IN, on_stdin
    )

    # Imported only when asked for, so a session without the Atspi typelib is
    # not even slightly affected by the feature existing.
    watcher = None
    if "--watch-focus" in argv:
        from .focus import FocusWatcher

        watcher = FocusWatcher(
            lambda editable: write(
                protocol.encode(protocol.FOCUS, editable=editable)
            )
        )
        reason = watcher.start()
        if reason is not None:
            # Say it rather than fail: the keyboard still works by hand, and
            # the whole failure mode here is silence, which is maddening to
            # diagnose from the outside.
            log(f"auto-open unavailable: {reason}")
            watcher = None
        app.watcher = watcher

    write(protocol.encode(protocol.READY))
    loop.run()
    if watcher is not None:
        watcher.stop()
    return 0
