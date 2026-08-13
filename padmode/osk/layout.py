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
#: Pressing shift twice locks it, the way a phone keyboard does.
SHIFT_OFF = "off"
SHIFT_ONCE = "once"
SHIFT_LOCK = "lock"


@dataclass(frozen=True)
class Key:
    """One cell. Either types a character or performs an action, never both."""

    label: str
    char: str | None = None
    action: str | None = None
    #: Columns spanned when drawn. Navigation ignores it -- a wide key is still
    #: one cell to move onto, which keeps the movement rules trivial.
    width: int = 1


@dataclass(frozen=True)
class Emit:
    """What the renderer should send back to the daemon, if anything."""

    kind: str
    text: str | None = None
    keys: tuple[str, ...] = ()


def _chars(row: str) -> tuple[Key, ...]:
    return tuple(Key(label=ch, char=ch) for ch in row)


#: Letters and digits. Deliberately the same shape as a console keyboard: the
#: point of this feature is that it is familiar, not that it is clever.
LETTER_ROWS: tuple[str, ...] = (
    "1234567890",
    "qwertyuiop",
    "asdfghjkl'",
    "zxcvbnm,.?",
)

#: Every remaining ASCII symbol, in four rows of eight. The two layers have
#: different column counts on purpose -- 32 symbols do not fit a 10-wide grid,
#: and a ragged final row navigates worse than a narrower rectangle.
SYMBOL_ROWS: tuple[str, ...] = (
    "!@#$%^&*",
    "()-_=+[]",
    "{}\\|;:'\"",
    ",.<>/?`~",
)

#: Shared bottom row. These have dedicated pad buttons too, but they are on the
#: grid as well: nothing should require memorising a shortcut.
ACTION_ROW: tuple[Key, ...] = (
    Key(label="⇧", action="shift"),
    Key(label="?#", action="layer"),
    Key(label="space", action="space", width=4),
    Key(label="⌫", action="back"),
    Key(label="↵", action="enter", width=2),
)


def _build(rows: tuple[str, ...]) -> tuple[tuple[Key, ...], ...]:
    return tuple(_chars(row) for row in rows) + (ACTION_ROW,)


LAYERS: dict[str, tuple[tuple[Key, ...], ...]] = {
    LETTERS: _build(LETTER_ROWS),
    SYMBOLS: _build(SYMBOL_ROWS),
}

#: Named keys the grid can send that are not characters.
_ACTION_KEYS: dict[str, tuple[str, ...]] = {
    "back": ("BACKSPACE",),
    "enter": ("ENTER",),
}


@dataclass
class Keyboard:
    """Grid state: which layer, where the highlight is, and shift."""

    layer: str = LETTERS
    row: int = 1
    col: int = 0
    shift: str = SHIFT_OFF
    #: Set when the last action changed something the renderer must redraw
    #: beyond the highlight (the layer, or the shift indicator).
    dirty: bool = field(default=False, compare=False)

    # -- reading -------------------------------------------------------
    @property
    def rows(self) -> tuple[tuple[Key, ...], ...]:
        return LAYERS[self.layer]

    @property
    def current(self) -> Key:
        return self.rows[self.row][self.col]

    def label_of(self, key: Key) -> str:
        """What to draw on a key, honouring shift.

        Shift only affects letters. Digits keep their labels: the symbols that
        share their keys on a US layout live on the symbol layer instead, which
        is both how phones do it and the only version that stays true when the
        user's layout is not US (:func:`padmode.buttons.keys_for_char`).
        """
        if key.char and self.shift != SHIFT_OFF and key.char.isalpha():
            return key.char.upper()
        return key.label

    # -- moving --------------------------------------------------------
    def step(self, direction: str) -> None:
        """Move the highlight one cell, wrapping at every edge.

        Wrapping in both axes rather than stopping: the grid is small, and
        wrapping puts the action row one press *up* from the top row as well as
        one press down from the bottom, which saves a traverse for the keys
        pressed most often.
        """
        rows = self.rows
        if direction in ("left", "right"):
            width = len(rows[self.row])
            self.col = (self.col + (1 if direction == "right" else -1)) % width
            return
        if direction in ("up", "down"):
            self.row = (self.row + (1 if direction == "down" else -1)) % len(rows)
            # Rows differ in length -- the action row is short, and the two
            # layers are different widths. Keep the column where the user put
            # it and clamp only as far as this row allows.
            self.col = min(self.col, len(rows[self.row]) - 1)

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
        if name == "layer":
            self.layer = SYMBOLS if self.layer == LETTERS else LETTERS
            # The new layer is a different shape; put the highlight somewhere
            # that certainly exists rather than clamping into a corner.
            self.row = min(self.row, len(self.rows) - 1)
            self.col = min(self.col, len(self.rows[self.row]) - 1)
            self.dirty = True
            return None
        if name == "close":
            return Emit(protocol.CLOSED)
        if name == "space":
            return Emit(protocol.TYPE, text=" ")
        if name in _ACTION_KEYS:
            return Emit(protocol.KEY, keys=_ACTION_KEYS[name])
        if name == "commit":
            return self.press()
        return None

    def _type(self, key: Key) -> Emit | None:
        if key.char is None:
            return None
        text = self.label_of(key)
        if self.shift == SHIFT_ONCE:
            self.shift = SHIFT_OFF
            self.dirty = True
        return Emit(protocol.TYPE, text=text)
