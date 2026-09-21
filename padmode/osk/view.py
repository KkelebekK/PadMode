"""What the keyboard looks like: the widget tree, the CSS, and key sizes.

Everything here is appearance. Where the panel sits and how it gets on screen
belong to :mod:`padmode.osk.surface`; what the keys *do* belongs to
:mod:`padmode.osk.model`. This file owns the one question neither of those can
answer: given a screen this big, how big is a key?

Needs GTK4. Never imported by the daemon (CLAUDE.md §2).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from . import keys as grid  # noqa: E402
from . import log, model  # noqa: E402

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

CSS = b"""
window.osk { background: transparent; }
/* A floating panel, not a bar welded to the screen edge: the surface is the
   keyboard and layer-shell margins place it, so the right stick can push it
   out of the way of whatever you are typing into. Rounded on every side
   because every side is now an edge you can see. */
.keyboard {
    background: alpha(#12141a, 0.94);
    border: 1px solid alpha(#ffffff, 0.10);
    border-radius: 14px;
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
/* The pad button that does this key's job, in its corner. Xbox colours,
   because padmode names buttons the Xbox way everywhere else too. */
.badge {
    background: alpha(#000000, 0.5);
    color: #e8eaf0;
    border-radius: 999px;
    margin: 2px;
    padding: 0 3px;
    font-weight: 700;
}
.badge.b-a { background: #3fa34a; color: #ffffff; }
.badge.b-b { background: #cf4032; color: #ffffff; }
.badge.b-x { background: #2f6fd0; color: #ffffff; }
.badge.b-y { background: #d9a521; color: #14161c; }
.hints {
    color: alpha(#e8eaf0, 0.45);
    font-size: 13px;
    padding: 6px 4px 0 4px;
}
"""

#: Buttons whose badge reads better as a glyph than as its name. Everything
#: else -- A, B, X, Y, LB, RB, LT, RT, L3 -- is short enough already.
BADGE_TEXT = {"START": "≡"}


def install_css(display) -> None:
    """Load the base stylesheet. Called once, before any widget is built."""
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


class View:
    """The panel: rows of keys, their badges, and the legend line.

    Holds the :class:`~padmode.osk.model.Keyboard` it is drawing, because the
    label on a key depends on shift and the layer, and one owner is what stops
    the highlight and the typed character from ever disagreeing.
    """

    def __init__(self, keyboard: model.Keyboard) -> None:
        self.keyboard = keyboard
        self.hints: dict[str, str] = {}
        self._cells: list[list[Gtk.Widget]] = []
        self._unit = KEY_MIN
        # Font size has to track key size, and CSS is the only way to set it
        # per-class. Its own provider so it can be replaced without rebuilding
        # the base stylesheet.
        self._sizing = Gtk.CssProvider()

        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.root.add_css_class("keyboard")
        self._hint_label = Gtk.Label()
        self._hint_label.add_css_class("hints")
        self._hint_label.set_xalign(0.5)  # centred within the panel, like the rows

    def attach_sizing(self, display) -> None:
        """Install the per-key-size stylesheet, above the base one."""
        Gtk.StyleContext.add_provider_for_display(
            display, self._sizing, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1
        )

    def set_opacity(self, value: float) -> None:
        self.root.set_opacity(value)

    # -- how big a key is ----------------------------------------------
    def rescale(self, usable: tuple[int, int], height_pct: float) -> None:
        """Recompute key size from the configured share of screen height.

        ``usable`` is the usable area, which is what the user actually has to
        give up: a share of the monitor's *geometry* would count the panel
        strut as screen the keyboard could use, and come out a few percent too
        big.
        """
        out_w, out_h = usable
        rows = len(self.keyboard.rows)
        target = out_h * height_pct / 100.0
        # Solve for key height, treating the legend line as a fraction of a key.
        key_h = (target - VCHROME - rows * ROW_MARGIN) / (rows + 0.30)
        unit = int(key_h / KEY_ASPECT)
        # The height-derived size still has to fit across the screen. Every row
        # is the same width in units, so one number covers both layers.
        unit = min(unit, int(out_w * WIDTH_BUDGET / grid.ROW_UNITS))
        unit = max(KEY_MIN, min(unit, KEY_MAX))
        log(f"rescale output={out_w}x{out_h} pct={height_pct:g} "
            f"target={target:.0f} unit={unit} (was {self._unit})")
        if unit == self._unit:
            return
        self._unit = unit
        self._sizing.load_from_data(
            f".key {{ font-size: {max(11, int(unit * 0.36))}px; }}"
            f".key.wide {{ font-size: {max(10, int(unit * 0.30))}px; }}"
            f".badge {{ font-size: {max(9, int(unit * 0.22))}px; "
            f"min-width: {max(12, int(unit * 0.30))}px; }}"
            f".hints {{ font-size: {max(10, int(unit * 0.22))}px; }}".encode()
        )
        self._apply_key_sizes()

    def _key_size(self, key: grid.Key) -> tuple[int, int]:
        gap = max(4, self._unit // 8)
        # Widths are fractional (1.5U, 2.25U, 6.25U), and a wide key swallows
        # the gaps it spans, so it lines up with the keys above and below it.
        return (
            round(self._unit * key.width + gap * (key.width - 1)),
            int(self._unit * KEY_ASPECT),
        )

    def _apply_key_sizes(self) -> None:
        for r, widgets in enumerate(self._cells):
            for c, widget in enumerate(widgets):
                key = self.keyboard.rows[r][c]
                parent = widget.get_parent()  # the Overlay carrying the badge
                (parent or widget).set_size_request(*self._key_size(key))

    def measure(self) -> tuple[int, int]:
        """What the keyboard asks to be, in pixels.

        ``measure`` rather than the allocated size: the allocation is only
        right after a layout pass, and the first placement happens before the
        panel has ever been on screen.
        """
        _, width, _, _ = self.root.measure(Gtk.Orientation.HORIZONTAL, -1)
        _, height, _, _ = self.root.measure(Gtk.Orientation.VERTICAL, width)
        return width, height

    # -- drawing -------------------------------------------------------
    def rebuild(self) -> None:
        """Build the widgets for the current layer, discarding the old ones.

        From scratch rather than reconciling: on a grid this small rebuilding
        costs nothing measurable, and the two layers differ in the middle.

        The caller must re-place the panel afterwards -- the one it was placed
        against no longer exists. Same size in practice, since every row is 13U
        on both layers, but re-clamping is cheaper than relying on that staying
        true.
        """
        child = self.root.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.root.remove(child)
            child = nxt
        self._cells = []

        for row in self.keyboard.rows:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            box.set_halign(Gtk.Align.CENTER)
            widgets: list[Gtk.Widget] = []
            for key in row:
                label = Gtk.Label(label=self.keyboard.label_of(key))
                label.add_css_class("key")
                if key.width > 1 or len(key.label) > 2:
                    # Word labels (ctrl, alt, super, space) need the smaller
                    # font to fit a key that is one unit wide.
                    label.add_css_class("wide")
                cell = Gtk.Overlay()
                cell.set_child(label)
                cell.set_size_request(*self._key_size(key))
                badge = self._badge_for(key)
                if badge is not None:
                    cell.add_overlay(badge)
                box.append(cell)
                widgets.append(label)
            self.root.append(box)
            self._cells.append(widgets)
        self.root.append(self._hint_label)
        self.refresh()

    def _badge_for(self, key: grid.Key) -> Gtk.Widget | None:
        """The pad button that does this key's job, drawn in its corner.

        Taken from the daemon's hints rather than baked into the grid, so a
        rebound button moves its badge with it -- bindings are meant to be
        fully overridable, and a badge that lied would be worse than none.
        """
        button = self.hints.get(key.action or "")
        if button is None:
            return None
        badge = Gtk.Label(label=BADGE_TEXT.get(button, button))
        badge.add_css_class("badge")
        badge.add_css_class(f"b-{button.lower()}")
        badge.set_halign(Gtk.Align.END)
        badge.set_valign(Gtk.Align.START)
        badge.set_can_target(False)  # decoration, never a hit target
        return badge

    def refresh(self) -> None:
        """Move the highlight and repaint shift-sensitive labels."""
        for r, widgets in enumerate(self._cells):
            for c, widget in enumerate(widgets):
                key = self.keyboard.rows[r][c]
                widget.set_label(self.keyboard.label_of(key))
                highlighted = r == self.keyboard.row and c == self.keyboard.col
                engaged = (
                    (key.action == "shift"
                     and self.keyboard.shift != model.SHIFT_OFF)
                    or (key.action == "layer"
                        and self.keyboard.layer == grid.SYMBOLS)
                    or (key.action in self.keyboard.mods)
                )
                _css(widget, "highlight", highlighted)
                _css(widget, "engaged", engaged and not highlighted)
        self._hint_label.set_label(self._hint_text())

    def _hint_text(self) -> str:
        """The actions that have no key of their own, and so no badge.

        Everything else is labelled where it lives. The list is
        :data:`~padmode.osk.keys.OFF_GRID_HINTS` rather than a literal here so
        a test can hold it against the bindings without needing GTK.
        """
        parts = [
            f"{self.hints[action]} {word}"
            for action, word in grid.OFF_GRID_HINTS
            if action in self.hints
        ]
        return "   ".join(parts)


def _css(widget: Gtk.Widget, name: str, present: bool) -> None:
    if present:
        widget.add_css_class(name)
    else:
        widget.remove_css_class(name)
