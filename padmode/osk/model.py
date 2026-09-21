"""Grid state: where the highlight is, what shift is doing, and where it sits.

Everything the keyboard knows that is not a pixel. Kept apart from
:mod:`padmode.osk.view` so it can be driven from a test without a compositor,
which is where nearly all of the interesting logic can be covered
(CLAUDE.md §5), and apart from :mod:`padmode.osk.keys` because that file is the
design and this one is the behaviour.

Stdlib only. Imported by the renderer process, never by the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import keys, protocol

#: Shift states. One-shot rather than a plain toggle because typing a single
#: capital is the common case, and a toggle makes it cost two extra presses.
#: Pressing shift twice locks it, the way a phone keyboard does; the Caps key
#: goes straight to the locked state.
SHIFT_OFF = "off"
SHIFT_ONCE = "once"
SHIFT_LOCK = "lock"


@dataclass(frozen=True)
class Emit:
    """What the renderer should send back to the daemon, if anything."""

    kind: str
    text: str | None = None
    keys: tuple[str, ...] = ()
    #: For :data:`~padmode.osk.protocol.HOLD`: whether the modifier goes down
    #: or comes back up.
    down: bool = False


@dataclass
class Keyboard:
    """Which layer, where the highlight is, and shift."""

    layer: str = keys.LETTERS
    row: int = 1
    #: Column 1, not 0: column 0 of the second row is Tab, and starting on a
    #: letter is friendlier than starting on a modifier.
    col: int = 1
    shift: str = SHIFT_OFF
    #: Latched modifier actions ("ctrl", "alt", "super"), each genuinely held
    #: down on the virtual keyboard until pressed again. Deliberately *not*
    #: one-shot like shift: holding Alt while stepping the window switcher, or
    #: Ctrl while clicking, both need the modifier to outlive one keystroke.
    mods: set[str] = field(default_factory=set)
    #: Set when the last action changed something the view must redraw beyond
    #: the highlight (the layer, or the shift indicator).
    dirty: bool = field(default=False, compare=False)
    #: Remembered horizontal position, in key units. The same idea as a text
    #: editor's desired column: moving up and down preserves it, so travelling
    #: past a narrow row and back returns you to where you started rather than
    #: dragging the highlight sideways.
    x: float = field(default=0.0, compare=False)

    def __post_init__(self) -> None:
        self._sync_x()

    # -- reading -------------------------------------------------------
    @property
    def rows(self) -> tuple[tuple[keys.Key, ...], ...]:
        return keys.LAYERS[self.layer]

    @property
    def current(self) -> keys.Key:
        return self.rows[self.row][self.col]

    def label_of(self, key: keys.Key) -> str:
        """What to draw on a key, honouring shift and the current layer.

        Shift only affects letters. Digits keep their labels: the symbols that
        share their keys on a US layout live on the symbol layer instead, which
        is both how phones do it and the only version that stays true when the
        user's layout is not US (:func:`padmode.buttons.keys_for_char`).
        """
        if key.action == "layer":
            return "abc" if self.layer == keys.SYMBOLS else "?#"
        if key.char and self.shift != SHIFT_OFF and key.char.isalpha():
            return key.char.upper()
        return key.label

    # -- moving --------------------------------------------------------
    def _sync_x(self) -> None:
        low, high = keys.SPANS[self.layer][self.row][self.col]
        self.x = (low + high) / 2

    def _under_x(self, row: int) -> int:
        """The key in ``row`` that lies under the remembered position."""
        spans = keys.SPANS[self.layer][row]
        for index, (low, high) in enumerate(spans):
            if low <= self.x < high:
                return index
        return len(spans) - 1  # only reachable at the extreme right edge

    def step(self, direction: str) -> None:
        """Move the highlight one key, wrapping at every edge.

        Vertical movement goes by *position*, not by column number: the keys
        are not all one unit wide, so the key above the middle of the space bar
        is the one visually above it, not the fourth key of the row.

        Wrapping in both axes rather than stopping: the grid is small, and
        wrapping puts the bottom row one press *up* from the top row as well as
        one press down from the bottom, which saves a traverse for the keys
        pressed most often.
        """
        rows = self.rows
        if direction in ("left", "right"):
            self.col = (
                self.col + (1 if direction == "right" else -1)
            ) % len(rows[self.row])
            self._sync_x()
            return
        if direction in ("up", "down"):
            self.row = (self.row + (1 if direction == "down" else -1)) % len(rows)
            # x is deliberately left alone -- that is what makes it remembered.
            self.col = self._under_x(self.row)

    # -- pressing ------------------------------------------------------
    def press(self) -> Emit | None:
        """Activate the highlighted key."""
        key = self.current
        if key.action is not None:
            return self.act(key.action)
        return self._type(key)

    def act(self, name: str) -> Emit | None:
        """Perform a named action, whether from a grid key or a pad button."""
        if name == "shift":
            self.shift = {
                SHIFT_OFF: SHIFT_ONCE,
                SHIFT_ONCE: SHIFT_LOCK,
                SHIFT_LOCK: SHIFT_OFF,
            }[self.shift]
            self.dirty = True
            return None
        if name == "capslock":
            # Locks our own shift rather than sending KEY_CAPSLOCK: the grid
            # then draws in capitals, so what you see is what you get, and
            # closing the keyboard cannot leave the desktop stuck in caps.
            self.shift = SHIFT_OFF if self.shift == SHIFT_LOCK else SHIFT_LOCK
            self.dirty = True
            return None
        if name == "layer":
            self.layer = keys.SYMBOLS if self.layer == keys.LETTERS else keys.LETTERS
            # The frame is identical, so the highlight stays where it looks
            # like it is; only a character key can land on a different index.
            self.col = self._under_x(self.row)
            self.dirty = True
            return None
        if name in protocol.MODIFIERS:
            down = name not in self.mods
            if down:
                self.mods.add(name)
            else:
                self.mods.discard(name)
            self.dirty = True
            return Emit(
                protocol.HOLD, keys=(protocol.MODIFIERS[name],), down=down
            )
        if name == "close":
            return Emit(protocol.CLOSED)
        if name == "space":
            return Emit(protocol.TYPE, text=" ")
        if name in keys.ACTION_KEYS:
            return Emit(protocol.KEY, keys=keys.ACTION_KEYS[name])
        if name == "commit":
            return self.press()
        return None

    def release_mods(self) -> tuple[Emit, ...]:
        """Let every latched modifier up.

        Called whenever the keyboard leaves the screen. A latch that outlived
        the keyboard would strand the desktop with Ctrl or Super held down,
        which is unusable and gives no clue where it came from.
        """
        emits = tuple(
            Emit(protocol.HOLD, keys=(protocol.MODIFIERS[name],), down=False)
            for name in sorted(self.mods)
        )
        self.mods.clear()
        self.dirty = bool(emits)
        return emits

    def _type(self, key: keys.Key) -> Emit | None:
        if key.char is None:
            return None
        text = self.label_of(key)
        if self.shift == SHIFT_ONCE:
            self.shift = SHIFT_OFF
            self.dirty = True
        return Emit(protocol.TYPE, text=text)


# -- where the keyboard sits -------------------------------------------
# It floats rather than being welded to one edge, so it can be driven out of
# the way of whatever you are typing into. What comes out of here is a
# **top-left corner inside the screen's usable area**, which the surface hands
# to the compositor as a pair of layer-shell margins -- the keyboard is its own
# surface, so moving it moves the surface.
#
# The arithmetic lives here, with the rest of the stdlib-only logic, because
# the surface cannot be tested without a compositor and this part does not need
# one (CLAUDE.md §5).
#
# ``output`` means the **usable** area, not the monitor's geometry. They differ
# by whatever the panels reserve, and margins are measured from the usable
# area: sizing against a 1440-tall monitor with a 34px panel puts the keyboard
# 34px below the bottom of the screen. Observed, on hardware.


def _clamp(value: float, span: float, limit: float) -> float:
    """Keep a span of ``span`` wholly inside ``0..limit``.

    A panel larger than the output goes to 0 rather than to a negative
    coordinate: better to lose the far edge off the bottom-right than to have
    the keys you navigate from start off-screen.
    """
    return min(max(value, 0.0), max(0.0, limit - span))


def clamp_position(
    x: float, y: float, panel: tuple[float, float], output: tuple[float, float]
) -> tuple[float, float]:
    """Nearest position to ``x, y`` that leaves the whole panel on screen."""
    return _clamp(x, panel[0], output[0]), _clamp(y, panel[1], output[1])


def home_position(
    panel: tuple[float, float], output: tuple[float, float], edge: str
) -> tuple[float, float]:
    """Where the keyboard sits until it is moved, and what ``recenter`` undoes.

    Centred against the configured edge: it is the position that assumes least
    about where the text field is, which is the same reason the keyboard never
    reserves space.
    """
    return clamp_position(
        (output[0] - panel[0]) / 2,
        0.0 if edge == "top" else output[1] - panel[1],
        panel,
        output,
    )
