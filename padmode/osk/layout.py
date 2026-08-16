"""The key grid, the highlight, and what pressing a key means.

The renderer owns all of this (docs/on-screen-keyboard.md): the layout lives in
exactly one place, so the highlight and the typed character cannot disagree --
the worst bug this feature could have. Kept free of GTK so the whole thing is
testable without a compositor, which is where nearly all of the interesting
logic can be covered (CLAUDE.md §5).

Stdlib only. Imported by the renderer process, never by the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import protocol

#: Layer names.
LETTERS = "letters"
SYMBOLS = "symbols"

#: Shift states. One-shot rather than a plain toggle because typing a single
#: capital is the common case, and a toggle makes it cost two extra presses.
#: Pressing shift twice locks it, the way a phone keyboard does; the Caps key
#: goes straight to the locked state.
SHIFT_OFF = "off"
SHIFT_ONCE = "once"
SHIFT_LOCK = "lock"

#: Width of every row, in key units. Both layers match, and that is load
#: bearing twice over: the rows line up visually, and toggling layers cannot
#: change the surface height -- which matters because a mapped layer surface
#: only resizes reliably when it *grows* (docs/on-screen-keyboard.md §Sizing).
ROW_UNITS = 13.0


@dataclass(frozen=True)
class Key:
    """One cell. Either types a character or performs an action, never both."""

    label: str
    char: str | None = None
    action: str | None = None
    #: Width in key units, as a mechanical keyboard measures them: 1U is a
    #: letter, 6.25U is the space bar.
    width: float = 1.0


@dataclass(frozen=True)
class Emit:
    """What the renderer should send back to the daemon, if anything."""

    kind: str
    text: str | None = None
    keys: tuple[str, ...] = ()
    #: For :data:`~padmode.osk.protocol.HOLD`: whether the modifier goes down
    #: or comes back up.
    down: bool = False


def _chars(row: str, width: float = 1.0) -> tuple[Key, ...]:
    return tuple(Key(label=ch, char=ch, width=width) for ch in row)


# The frame: every key that is not a character. These are shared between the
# layers by identity, so toggling changes only the middle of the keyboard and
# nothing a thumb is resting on moves.
_LAYER = Key(label="?#", action="layer")
_BACK = Key(label="⌫", action="back", width=2.0)
_TAB = Key(label="tab", action="tab", width=1.5)
_CAPS = Key(label="caps", action="capslock", width=1.75)
_ENTER = Key(label="↵", action="enter", width=2.25)
_LSHIFT = Key(label="⇧", action="shift", width=2.25)
#: Narrower than the 2.75U a real 60% gives it: keeping `.` on the bottom row
#: costs a unit, and the shift you reach for least is the cheapest place to
#: find it.
_RSHIFT = Key(label="⇧", action="shift", width=1.75)
_CTRL = Key(label="ctrl", action="ctrl", width=1.25)
_WIN = Key(label="win", action="super", width=1.25)
_ALT = Key(label="alt", action="alt", width=1.25)
_SPACE = Key(label="space", action="space", width=6.25)
_LEFT = Key(label="←", action="left", width=1.5)
_RIGHT = Key(label="→", action="right", width=1.5)

#: Identical on both layers, so the modifiers and the space bar never move.
_BOTTOM: tuple[Key, ...] = (_CTRL, _WIN, _ALT, _SPACE, _LEFT, _RIGHT)

#: A 60% keyboard, with four deliberate departures: the layer toggle takes
#: Escape's place, the number row carries no shifted legends, `/` is gone, and
#: the cursor keys fill the space to the right of the space bar. Familiar
#: beats clever -- the point of the feature is that you already know where
#: things are.
LETTER_LAYER: tuple[tuple[Key, ...], ...] = (
    (_LAYER, *_chars("1234567890"), _BACK),
    (_TAB, *_chars("qwertyuiop"), Key(label="\\", char="\\", width=1.5)),
    (_CAPS, *_chars("asdfghjkl"), _ENTER),
    (_LSHIFT, *_chars("zxcvbnm,."), _RSHIFT),
    _BOTTOM,
)

#: The same frame with the characters swapped. `|` sits where `\` does -- the
#: same physical key, its shifted meaning. `,` and `.` are repeated here on
#: purpose: they are wanted constantly while typing symbols, and going back a
#: layer for a decimal point would be absurd.
SYMBOL_LAYER: tuple[tuple[Key, ...], ...] = (
    (_LAYER, *_chars("!@#$%^&*()"), _BACK),
    (_TAB, *_chars("-_=+[]{};:"), Key(label="|", char="|", width=1.5)),
    (_CAPS, *_chars("'\"`~<>", width=1.5), _ENTER),
    (_LSHIFT, *_chars(",./?", width=2.25), _RSHIFT),
    _BOTTOM,
)

LAYERS: dict[str, tuple[tuple[Key, ...], ...]] = {
    LETTERS: LETTER_LAYER,
    SYMBOLS: SYMBOL_LAYER,
}


def _spans(row: tuple[Key, ...]) -> tuple[tuple[float, float], ...]:
    """Where each key of a row starts and ends, in key units."""
    spans, x = [], 0.0
    for key in row:
        spans.append((x, x + key.width))
        x += key.width
    return tuple(spans)


#: Precomputed once: navigation asks "which key is under this point" on every
#: vertical move, and the layout never changes at runtime.
SPANS: dict[str, tuple[tuple[tuple[float, float], ...], ...]] = {
    name: tuple(_spans(row) for row in rows) for name, rows in LAYERS.items()
}

#: Named keys the grid can send that are not characters.
_ACTION_KEYS: dict[str, tuple[str, ...]] = {
    "back": ("BACKSPACE",),
    "enter": ("ENTER",),
    "tab": ("TAB",),
    "left": ("LEFT",),
    "right": ("RIGHT",),
}


@dataclass
class Keyboard:
    """Grid state: which layer, where the highlight is, and shift."""

    layer: str = LETTERS
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
    #: Set when the last action changed something the renderer must redraw
    #: beyond the highlight (the layer, or the shift indicator).
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
    def rows(self) -> tuple[tuple[Key, ...], ...]:
        return LAYERS[self.layer]

    @property
    def current(self) -> Key:
        return self.rows[self.row][self.col]

    def label_of(self, key: Key) -> str:
        """What to draw on a key, honouring shift and the current layer.

        Shift only affects letters. Digits keep their labels: the symbols that
        share their keys on a US layout live on the symbol layer instead, which
        is both how phones do it and the only version that stays true when the
        user's layout is not US (:func:`padmode.buttons.keys_for_char`).
        """
        if key.action == "layer":
            return "abc" if self.layer == SYMBOLS else "?#"
        if key.char and self.shift != SHIFT_OFF and key.char.isalpha():
            return key.char.upper()
        return key.label

    # -- moving --------------------------------------------------------
    def _sync_x(self) -> None:
        low, high = SPANS[self.layer][self.row][self.col]
        self.x = (low + high) / 2

    def _under_x(self, row: int) -> int:
        """The key in ``row`` that lies under the remembered position."""
        spans = SPANS[self.layer][row]
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
            self.layer = SYMBOLS if self.layer == LETTERS else LETTERS
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
        if name in _ACTION_KEYS:
            return Emit(protocol.KEY, keys=_ACTION_KEYS[name])
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

    def _type(self, key: Key) -> Emit | None:
        if key.char is None:
            return None
        text = self.label_of(key)
        if self.shift == SHIFT_ONCE:
            self.shift = SHIFT_OFF
            self.dirty = True
        return Emit(protocol.TYPE, text=text)
