"""Where the keyboard sits: the layer surface, its size, and its margins.

The panel is its own surface, no wider than the keys it draws, placed by a
pair of layer-shell margins. Moving it is moving the surface -- which is what
lets the right stick push it off whatever you are typing into, and why it is
rounded on every side: every side is now an edge you can see.

What it looks like is :mod:`padmode.osk.view`; which corners a panel is
*allowed* to sit in is arithmetic in :mod:`padmode.osk.model`, kept there
because it can be checked without a compositor (CLAUDE.md §5). What is left
here is only what genuinely needs a live compositor: mapping, measuring, and
the margins.

Needs GTK4 and gtk4-layer-shell. Never imported by the daemon (CLAUDE.md §2).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LS  # noqa: E402

from . import log, model  # noqa: E402

#: What the compositor calls us. Window backends can enumerate layer surfaces,
#: so the overlay and window-switching code needs a name to skip.
NAMESPACE = "padmode-osk"

#: Frames to wait for the compositor's configure after anchoring all four
#: edges, before giving up and sizing against whatever we have. Bounded on
#: purpose: measuring forever means an invisible keyboard, which is a far
#: worse failure than a badly sized one.
MEASURE_FRAMES = 10


class Surface:
    """The layer surface the keyboard is drawn on, and where it is placed."""

    def __init__(
        self, view, edge: str = "bottom", height_pct: float = 30.0
    ) -> None:
        self.view = view
        self.edge = edge
        self.height_pct = height_pct
        #: Usable area of the output, learned by measuring. None until then.
        self._usable: tuple[float, float] | None = None
        #: What the view last measured itself at, in pixels.
        self._panel: tuple[float, float] = (0.0, 0.0)
        #: Top-left corner of the panel inside the usable area. Kept as floats
        #: so a stick that moves the panel a third of a pixel per tick still
        #: moves it: the remainder accumulates here rather than being thrown
        #: away by the integer margins (``client.move`` rounds to a tenth on
        #: the wire for the same reason).
        self._pos: tuple[float, float] | None = None
        self._measuring = False
        self._show_after_measure = False
        self._frames = 0
        #: Surface size when the current measuring pass began, so a frame that
        #: still reports it can be recognised as "the configure has not
        #: landed".
        self._pre_measure: tuple[int, int] = (0, 0)
        #: The frame clock we have a handler on. One per surface, and the
        #: surface dies on unmap, so comparing identity avoids both duplicate
        #: handlers and none at all.
        self._clock = None

        self.window = Gtk.Window()
        self.window.add_css_class("osk")
        # Deliberately no set_default_size(): the panel's own measurement is
        # the size, and a default fights it.
        self.window.set_child(view.root)

        LS.init_for_window(self.window)
        LS.set_namespace(self.window, NAMESPACE)
        # OVERLAY so it is above fullscreen windows; NONE so it can never take
        # keyboard focus, which is the whole reason this is a layer surface and
        # not an ordinary window.
        LS.set_layer(self.window, LS.Layer.OVERLAY)
        LS.set_keyboard_mode(self.window, LS.KeyboardMode.NONE)
        # Never reserve space: reshuffling the user's windows to make room is
        # not something a keyboard that appears for ten seconds should do.
        LS.set_exclusive_zone(self.window, 0)
        self._anchor_corner()

        self.window.connect("map", self._on_map)

    @property
    def visible(self) -> bool:
        """On screen *and* meant to be seen -- a measuring pass is neither."""
        return self.window.get_visible() and not self._measuring

    # -- coming and going ----------------------------------------------
    def prewarm(self) -> None:
        """Map once, invisibly, to warm GTK and learn the output size.

        Measured on the old full-width surface: the first show cost ~105 ms of
        renderer setup and every show after it 0.6 ms. Paying that while the
        user is still moving the pointer is the difference between a keyboard
        that appears and one that lands.
        """
        self._begin_measure()

    def show(self) -> None:
        """Put the keyboard on screen, measuring the output first.

        The measuring pass runs on every show rather than once at startup: a
        panel the size of the keyboard cannot tell which output it is on from
        its own width, which is how the previous full-width surface noticed.
        Re-measuring is the honest answer to that, and it costs one unmapped
        map -- the same pass the pre-warm already runs.
        """
        if self.window.get_visible():
            return
        self._show_after_measure = True
        self._begin_measure()

    def hide(self) -> None:
        self._show_after_measure = False
        self.window.set_visible(False)

    # -- measuring -----------------------------------------------------
    def _begin_measure(self) -> None:
        """Anchor all four edges and map, so the surface reports the output.

        A layer surface reports width 0 until the compositor's configure
        arrives, and ``Gdk.Display.get_monitor_at_surface`` returns None for
        one, so there is no way to ask in advance. Anchored to all four edges
        it is given the usable area of the output instead -- which is the
        number the margins are measured from, and the one the keyboard has to
        be sized against.
        """
        if self._measuring:
            return
        self._measuring = True
        self._frames = 0
        self._pre_measure = (self.window.get_width(), self.window.get_height())
        self.view.set_opacity(0.0)
        for edge in (LS.Edge.TOP, LS.Edge.BOTTOM, LS.Edge.LEFT, LS.Edge.RIGHT):
            LS.set_anchor(self.window, edge, True)
            LS.set_margin(self.window, edge, 0)
        self.window.set_visible(True)
        log(f"measure begin was={self._pre_measure[0]}x{self._pre_measure[1]}")

    def _on_frame(self, _clock) -> None:
        if not self._measuring:
            return
        width, height = self.window.get_width(), self.window.get_height()
        if width <= 0 or height <= 0:
            return  # configure has not arrived; try again on the next frame
        self._frames += 1
        if (width, height) == self._pre_measure and self._frames < MEASURE_FRAMES:
            # Anchoring the remaining edges of an already-mapped surface does
            # not resize it until the compositor's configure arrives, which is
            # not the same frame. Reading now would measure the keyboard's own
            # size and scale everything from it.
            return
        self._finish_measure(width, height)

    def _finish_measure(self, width: int, height: int) -> None:
        self._measuring = False
        self._usable = (float(width), float(height))
        log(f"measure done usable={width}x{height} after {self._frames} frames")
        # Unmap before resizing. A mapped layer surface only propagates a size
        # change reliably when it *grows*, and going from the whole output back
        # down to keyboard-sized is the shrinking case every time. A fresh map
        # is the path known to size correctly, and it costs 0.6 ms.
        self.window.set_visible(False)
        self.view.rescale(self._usable, self.height_pct)
        self.view.set_opacity(1.0)
        self._anchor_corner()
        self.reflow(home=self._pos is None)
        if self._show_after_measure:
            self._show_after_measure = False
            self.window.set_visible(True)

    def _anchor_corner(self) -> None:
        """Anchor top-left only, so the margins are a position."""
        for edge in (LS.Edge.TOP, LS.Edge.LEFT):
            LS.set_anchor(self.window, edge, True)
        for edge in (LS.Edge.BOTTOM, LS.Edge.RIGHT):
            LS.set_anchor(self.window, edge, False)

    # -- placing -------------------------------------------------------
    def reflow(self, home: bool = False) -> None:
        """Re-read the panel's size and put it back inside the screen.

        Called after anything that can change how big the panel is -- a rescale
        or a rebuilt grid -- because the corner it was clamped against no
        longer describes it.
        """
        if self._usable is None:
            return
        width, height = self.view.measure()
        self._panel = (float(width), float(height))
        if home or self._pos is None:
            self._pos = model.home_position(self._panel, self._usable, self.edge)
        else:
            self._pos = model.clamp_position(*self._pos, self._panel, self._usable)
        log(f"place panel={width}x{height} at={self._pos[0]:.0f},{self._pos[1]:.0f} "
            f"edge={self.edge} home={home}")
        self._apply()

    def recenter(self) -> None:
        """Put the panel back where it started."""
        self.reflow(home=True)

    def move(self, dx: float, dy: float) -> None:
        """Slide the panel, keeping all of it on screen."""
        if self._pos is None or self._usable is None:
            return
        moved = model.clamp_position(
            self._pos[0] + dx, self._pos[1] + dy, self._panel, self._usable
        )
        was = (int(self._pos[0]), int(self._pos[1]))
        self._pos = moved
        # Sub-pixel movement still accumulates in _pos; only whole pixels are
        # worth a round trip to the compositor.
        if (int(moved[0]), int(moved[1])) != was:
            self._apply()

    def _apply(self) -> None:
        if self._pos is None:
            return
        LS.set_margin(self.window, LS.Edge.LEFT, int(self._pos[0]))
        LS.set_margin(self.window, LS.Edge.TOP, int(self._pos[1]))

    # -- input ---------------------------------------------------------
    def _on_map(self, *_args) -> None:
        self._clear_input_region()
        clock = self.window.get_frame_clock()
        # One handler for the lifetime of the surface, rather than connecting
        # and disconnecting around each pass. The clock belongs to the surface
        # and dies with it on unmap, so comparing identity is enough.
        if clock is not None and clock is not self._clock:
            self._clock = clock
            clock.connect("after-paint", self._on_frame)

    def _clear_input_region(self) -> None:
        """Let clicks through: the keyboard is driven by the pad, not the mouse.

        It matters most during a measuring pass, when the surface covers the
        whole output -- without this, every click on the screen would land on
        an invisible keyboard for as long as the pass takes.
        """
        surface = self.window.get_surface()
        if surface is None:
            return
        try:
            import cairo

            surface.set_input_region(cairo.Region())
        except (ImportError, AttributeError, TypeError):
            pass  # clicks land on the keyboard instead; not worth failing over
