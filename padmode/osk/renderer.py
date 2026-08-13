"""The process that draws the keyboard.

Runs as ``python -m padmode.osk``, spawned by the daemon, and is the only part
of padmode that needs a GUI toolkit. Nothing here is ever imported by the
daemon (CLAUDE.md §2); it talks over stdin/stdout using
:mod:`padmode.osk.protocol`.

**Must be started with ``LD_PRELOAD`` pointing at ``libgtk4-layer-shell.so``.**
Measured on Plasma 6.7 / GTK 4.22: without it ``is_supported()`` is false even
though KWin advertises ``zwlr_layer_shell_v1`` v5, because the library cannot
hook GDK when PyGObject ``dlopen``s it after the fact. The dedicated
``liblayer-shell-preload.so`` shipped alongside does *not* work; the main
library does. :func:`padmode.osk.preload_library` finds it.

Draws with GSK's cairo renderer, which for a static grid measured 7 ms to first
frame and 85 MB resident against 104 ms and 224 MB for the Vulkan default.
Recorded in docs/on-screen-keyboard.md along with the caveat that upstream may
one day remove it.
"""

from __future__ import annotations

import os
import sys

from . import layout, protocol

# Before GTK is imported: for a keyboard that never animates, the software
# renderer is faster to first frame and far lighter. Overridable, so a user on
# a GTK that has dropped it can still get a keyboard.
os.environ.setdefault("GSK_RENDERER", "cairo")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LS  # noqa: E402

NAMESPACE = "padmode-osk"

# The keyboard is sized as a share of the screen's *height*; the width follows
# from the key aspect ratio. Height is the dimension worth controlling: it is
# how much of your screen the keyboard eats, and it is the same fraction on a
# laptop panel and a television.
KEY_ASPECT = 0.9          # key height as a fraction of key width
KEY_MIN = 26              # below this it is unreadable from a couch
KEY_MAX = 400
#: Fraction of the output width the grid may occupy before the height-derived
#: size has to give way. Stops a large percentage on a narrow screen from
#: running the rows off both edges.
WIDTH_BUDGET = 0.96
#: Vertical chrome that is not keys: the panel's padding, per-row margins and
#: the hint line. Approximate by design -- the percentage is a request, not a
#: contract, and measuring it exactly would cost a second layout pass.
VCHROME = 32
ROW_MARGIN = 6
#: Frames to wait for the compositor's configure after anchoring all four
#: edges, before giving up and sizing against whatever we have. Bounded on
#: purpose: measuring forever means an invisible keyboard, which is a far worse
#: failure than a badly sized one.
MEASURE_FRAMES = 10

CSS = b"""
window.osk { background: transparent; }
.keyboard {
    background: alpha(#12141a, 0.94);
    border-top: 1px solid alpha(#ffffff, 0.10);
    padding: 14px 18px 12px 18px;
}
.key {
    background: alpha(#ffffff, 0.07);
    border-radius: 8px;
    margin: 3px;
    padding: 10px 0;
    color: #e8eaf0;
    font-size: 17px;
    font-weight: 500;
}
.key.wide { font-size: 15px; }
.key.highlight {
    background: #4a7dff;
    color: #ffffff;
    font-weight: 700;
}
.key.engaged { background: alpha(#4a7dff, 0.35); }
.hints {
    color: alpha(#e8eaf0, 0.45);
    font-size: 13px;
    padding: 6px 4px 0 4px;
}
"""


class Renderer:
    """Owns the surface, the grid widgets and the highlight."""

    def __init__(self, position: str = "bottom", height_pct: float = 30.0) -> None:
        self.keyboard = layout.Keyboard()
        self.hints: dict[str, str] = {}
        self._cells: list[list[Gtk.Widget]] = []
        self._buffer = b""
        self._unit = KEY_MIN
        self._height_pct = height_pct
        #: Usable size of the output we were last mapped on, or None until the
        #: measuring pass has run.
        self._output: tuple[int, int] | None = None
        self._measuring = False
        self._show_pending = False
        #: Whether the surface was already on screen when measuring began, so
        #: a re-measure triggered by moving output does not hide a keyboard the
        #: user is looking at.
        self._keep_visible = False
        #: Surface size when the current measuring pass began, so a frame that
        #: still reports it can be recognised as "the configure has not landed".
        self._pre_measure: tuple[int, int] = (0, 0)
        self._measure_frames = 0
        #: The frame clock we have a handler on. One per surface, and the
        #: surface is destroyed on unmap, so this is how we avoid both
        #: duplicate handlers and none at all.
        self._clock = None
        # Font size has to track key size, and CSS is the only way to set it
        # per-class. Its own provider so it can be replaced without rebuilding
        # the base stylesheet.
        self._sizing = Gtk.CssProvider()

        self.window = Gtk.Window()
        self.window.add_css_class("osk")
        # Deliberately no set_default_size(). Anchoring to both side edges is
        # what makes the surface span the output -- but *any* call to
        # set_default_size, including set_default_size(-1, -1), pins it to the
        # content's natural width instead. Measured: 460x176 with the call,
        # 1536x176 without it, everything else identical.

        self._grid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._grid_box.add_css_class("keyboard")
        self._hint_label = Gtk.Label()
        self._hint_label.add_css_class("hints")
        # Centred, like the rows: the surface spans the whole output, so
        # left-aligned hints end up marooned in a corner.
        self._hint_label.set_xalign(0.5)
        self._grid_box.append(self._hint_label)
        self.window.set_child(self._grid_box)

        LS.init_for_window(self.window)
        LS.set_namespace(self.window, NAMESPACE)
        # OVERLAY so it is above fullscreen windows; NONE so it can never take
        # keyboard focus, which is the whole reason this is a layer surface and
        # not an ordinary window. Measured: focus does not move when it maps.
        LS.set_layer(self.window, LS.Layer.OVERLAY)
        LS.set_keyboard_mode(self.window, LS.KeyboardMode.NONE)
        edge = LS.Edge.TOP if position == "top" else LS.Edge.BOTTOM
        for anchored in (edge, LS.Edge.LEFT, LS.Edge.RIGHT):
            LS.set_anchor(self.window, anchored, True)
        # Never reserve space: reshuffling the user's windows to make room is
        # not something a keyboard that appears for ten seconds should do.
        LS.set_exclusive_zone(self.window, 0)

        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self._sizing,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
        )
        self.window.connect("map", self._on_map)
        self._rebuild()
        self._begin_measure()

    # -- sizing --------------------------------------------------------
    def _begin_measure(self) -> None:
        """Learn the output's usable size without the user seeing anything.

        The size cannot be known before mapping: measured, a layer surface
        reports width 0 until the compositor's configure arrives, and
        `Gdk.Display.get_monitor_at_surface` returns None for one, so there is
        no way to ask in advance. Sizing after the first paint of the *real*
        show works, but it is visibly a pop -- confirmed by eye, not assumed.

        So we map once during pre-warm, anchored to all four edges (which makes
        the surface report the usable area of the output) with the content
        transparent and no input region, read the size, and unmap. The user's
        first show is then already the right size. It also fully warms the GTK
        renderer, which the earlier pre-warm did not.
        """
        if self._measuring:
            return
        self._measuring = True
        # Only map if we are not already on screen. Calling set_visible(True)
        # on a visible window emits no `map`, which is what stranded this pass
        # the first time: the restart waited for a signal that never came, and
        # left a full-screen surface with its content at zero opacity -- a
        # keyboard you could type on but not see. Measured on a two-output
        # session: 1536x830, the whole usable area of the output.
        self._keep_visible = self.window.get_visible()
        self._pre_measure = (self.window.get_width(), self.window.get_height())
        self._measure_frames = 0
        self._grid_box.set_opacity(0.0)
        LS.set_anchor(self.window, LS.Edge.TOP, True)
        if not self._keep_visible:
            self.window.set_visible(True)
        _log(f"measure begin visible={self._keep_visible} was={self._pre_measure}")

    def _finish_measure(self) -> None:
        width, height = self.window.get_width(), self.window.get_height()
        if width <= 0 or height <= 0:
            return  # configure has not arrived; try again on the next frame
        self._measure_frames += 1
        if (width, height) == self._pre_measure and self._measure_frames < MEASURE_FRAMES:
            # Anchoring the fourth edge on an *already mapped* surface does not
            # resize it until the compositor's configure arrives, which is not
            # the same frame. Reading now would measure the keyboard's own
            # height and scale everything from it -- which is exactly how the
            # keyboard came out far too small after moving output.
            return
        self._output = (width, height)
        self._measuring = False
        _log(f"measure done output={self._output} after {self._measure_frames} frames")
        LS.set_anchor(self.window, LS.Edge.TOP, False)
        # Always unmap, rescale, then map again if it belongs on screen.
        # Resizing a mapped layer surface only propagates when it grows:
        # coming down from a larger output left the panel at the old height
        # with dead space under the keys, while going up looked fine. A fresh
        # map is the path that is known to size correctly -- it is what the
        # pre-warm pass does -- and it costs 0.6 ms.
        was_visible = self._keep_visible
        self.window.set_visible(False)
        self._rescale()
        self._grid_box.set_opacity(1.0)
        if was_visible:
            # Straight back up, deliberately not through _show(): moving
            # between monitors should not reset shift or where the highlight
            # sits.
            self.window.set_visible(True)
        elif self._show_pending:
            self._show_pending = False
            self._show()

    def _on_map(self, *_args) -> None:
        self._clear_input_region()
        clock = self.window.get_frame_clock()
        # One handler for the lifetime of the surface, rather than connecting
        # and disconnecting around each pass. The clock belongs to the surface
        # and dies with it on unmap, so comparing identity is enough to avoid
        # both duplicate handlers and none at all.
        if clock is not None and clock is not self._clock:
            self._clock = clock
            clock.connect("after-paint", self._on_frame)

    def _on_frame(self, _clock) -> None:
        if self._measuring:
            self._finish_measure()
            return
        width = self.window.get_width()
        if width > 0 and self._output is not None and width != self._output[0]:
            # The compositor put us on a different output -- it follows focus,
            # so this happens whenever you move screens mid-session. Width is
            # the only signal available: our height is our own content, not the
            # output's, so two outputs of equal width but different height are
            # indistinguishable here and keep the first one's sizing.
            self._begin_measure()

    def _clear_input_region(self) -> None:
        """Let clicks through: the keyboard is driven by the pad, not the mouse.

        Without this the surface swallows pointer events across everything it
        covers, which would make the still-live pointer useless for the bottom
        third of the screen -- the opposite of the point of it being a layer.
        """
        surface = self.window.get_surface()
        if surface is None:
            return
        try:
            import cairo

            surface.set_input_region(cairo.Region())
        except (ImportError, AttributeError, TypeError):
            pass  # clicks land on the keyboard instead; not worth failing over

    def _rescale(self) -> None:
        """Recompute key size from the configured share of screen height."""
        if self._output is None:
            return
        out_w, out_h = self._output
        rows = len(self.keyboard.rows)
        target = out_h * self._height_pct / 100.0
        # Solve for key height, treating the hint line as a fraction of a key.
        key_h = (target - VCHROME - rows * ROW_MARGIN) / (rows + 0.30)
        unit = int(key_h / KEY_ASPECT)
        # The height-derived size still has to fit across the screen.
        widest = max(len(row) for row in self.keyboard.rows)
        unit = min(unit, int(out_w * WIDTH_BUDGET / widest))
        unit = max(KEY_MIN, min(unit, KEY_MAX))
        _log(f"rescale output={out_w}x{out_h} pct={self._height_pct:g} "
             f"target={target:.0f} unit={unit} (was {self._unit})")
        if unit == self._unit:
            return
        self._unit = unit
        self._sizing.load_from_data(
            f".key {{ font-size: {max(11, int(unit * 0.36))}px; }}"
            f".key.wide {{ font-size: {max(10, int(unit * 0.30))}px; }}"
            f".hints {{ font-size: {max(10, int(unit * 0.22))}px; }}".encode()
        )
        self._apply_key_sizes()

    def _apply_key_sizes(self) -> None:
        for r, widgets in enumerate(self._cells):
            for c, widget in enumerate(widgets):
                widget.set_size_request(*self._key_size(self.keyboard.rows[r][c]))

    def _key_size(self, key: layout.Key) -> tuple[int, int]:
        gap = max(4, self._unit // 8)
        return (
            self._unit * key.width + gap * (key.width - 1),
            int(self._unit * KEY_ASPECT),
        )

    # -- drawing -------------------------------------------------------
    def _rebuild(self) -> None:
        """Build the widgets for the current layer, discarding the old ones.

        From scratch rather than reconciling: the two layers are different
        widths, and on a grid this small rebuilding costs nothing measurable.
        """
        child = self._grid_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._grid_box.remove(child)
            child = nxt
        self._cells = []

        for row in self.keyboard.rows:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            box.set_halign(Gtk.Align.CENTER)
            widgets: list[Gtk.Widget] = []
            for key in row:
                label = Gtk.Label(label=self.keyboard.label_of(key))
                label.add_css_class("key")
                if key.width > 1:
                    label.add_css_class("wide")
                label.set_size_request(*self._key_size(key))
                box.append(label)
                widgets.append(label)
            self._grid_box.append(box)
            self._cells.append(widgets)
        self._grid_box.append(self._hint_label)
        self._refresh()

    def _refresh(self) -> None:
        """Move the highlight and repaint shift-sensitive labels."""
        for r, widgets in enumerate(self._cells):
            for c, widget in enumerate(widgets):
                key = self.keyboard.rows[r][c]
                widget.set_label(self.keyboard.label_of(key))
                highlighted = r == self.keyboard.row and c == self.keyboard.col
                engaged = (
                    key.action == "shift"
                    and self.keyboard.shift != layout.SHIFT_OFF
                ) or (key.action == "layer" and self.keyboard.layer == layout.SYMBOLS)
                _css(widget, "highlight", highlighted)
                _css(widget, "engaged", engaged and not highlighted)
        self._hint_label.set_label(self._hint_text())

    def _hint_text(self) -> str:
        if not self.hints:
            return ""
        order = (("commit", "select"), ("close", "close"), ("back", "⌫"),
                 ("space", "space"), ("shift", "shift"), ("layer", "?#"))
        parts = [
            f"{self.hints[action]} {word}"
            for action, word in order
            if action in self.hints
        ]
        return "   ".join(parts)

    # -- messages ------------------------------------------------------
    def on_message(self, kind: str, fields: dict) -> None:
        if kind == protocol.SHOW:
            hints = fields.get("hints")
            if isinstance(hints, dict):
                self.hints = {
                    str(k): str(v) for k, v in hints.items() if isinstance(v, str)
                }
            self._show()
            return
        if kind == protocol.HIDE:
            self.window.set_visible(False)
            return
        if kind == protocol.STEP:
            direction = fields.get("dir")
            if isinstance(direction, str):
                self.keyboard.step(direction)
                self._refresh()
            return
        if kind == protocol.ACT:
            name = fields.get("name")
            if not isinstance(name, str) or name not in protocol.ACTIONS:
                return
            was_layer = self.keyboard.layer
            emit = self.keyboard.act(name)
            if self.keyboard.layer != was_layer:
                self._rebuild()
            else:
                self._refresh()
            if emit is not None:
                self.send(emit)
            return

    def _show(self) -> None:
        if self._measuring:
            # Asked for before the measuring pass finished. Showing now would
            # be the wrong size and then jump, which is the thing the pass
            # exists to avoid.
            self._show_pending = True
            return
        # Shift is per-visit; where the highlight sits is not, so reopening the
        # keyboard does not throw away where you were.
        self.keyboard.shift = layout.SHIFT_OFF
        self._refresh()
        self.window.set_visible(True)

    def send(self, emit: layout.Emit) -> None:
        if emit.kind == protocol.TYPE:
            write(protocol.encode(protocol.TYPE, text=emit.text))
        elif emit.kind == protocol.KEY:
            write(protocol.encode(protocol.KEY, keys=list(emit.keys)))
        elif emit.kind == protocol.CLOSED:
            self.window.set_visible(False)
            write(protocol.encode(protocol.CLOSED))

    def feed(self, data: bytes) -> None:
        """Consume whatever arrived on stdin, handling partial lines."""
        self._buffer += data
        while b"\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition(b"\n")
            parsed = protocol.decode(line)
            if parsed is not None:
                self.on_message(*parsed)


def _log(message: str) -> None:
    """One line to stderr, which the daemon inherits and journald collects.

    Sizing depends on compositor timing we cannot reproduce in a test, so the
    decisions are traceable in the log rather than only inferrable from what
    ends up on screen (CLAUDE.md §4).
    """
    print(f"osk: {message}", file=sys.stderr, flush=True)


def _css(widget: Gtk.Widget, name: str, present: bool) -> None:
    if present:
        widget.add_css_class(name)
    else:
        widget.remove_css_class(name)


def write(payload: bytes) -> None:
    """Write one message to the daemon, flushing so nothing waits in a buffer."""
    try:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    except (BrokenPipeError, ValueError):
        # The daemon is gone. Nothing to report it to.
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    position = "top" if "--top" in argv else "bottom"
    height_pct = 30.0
    for arg in argv:
        if arg.startswith("--height="):
            try:
                height_pct = float(arg.split("=", 1)[1])
            except ValueError:
                pass

    Gtk.init()
    if not LS.is_supported():
        write(protocol.encode(
            protocol.FAILED,
            reason="layer-shell unavailable -- start this with "
                   "LD_PRELOAD=libgtk4-layer-shell.so on a Wayland session "
                   "whose compositor supports zwlr_layer_shell_v1",
        ))
        return 1

    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    renderer = Renderer(position=position, height_pct=height_pct)
    loop = GLib.MainLoop()

    def on_stdin(fd: int, _condition: int) -> bool:
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if not data:
            loop.quit()  # daemon gone: EOF is the whole liveness mechanism
            return False
        renderer.feed(data)
        return True

    GLib.unix_fd_add_full(
        GLib.PRIORITY_DEFAULT, sys.stdin.fileno(), GLib.IOCondition.IN, on_stdin
    )
    write(protocol.encode(protocol.READY))
    loop.run()
    return 0
