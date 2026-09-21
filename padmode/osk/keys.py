"""The key grid: what is on the keyboard, and how wide each key is.

Data, and the span arithmetic derived from it. Nothing here has state and
nothing here decides anything -- :mod:`padmode.osk.model` does that, and
:mod:`padmode.osk.view` draws it. The split exists because this file is the
*design*: the layout, the widths and the legends are choices made once and not
to be re-derived, while everything around them is machinery.

The layout lives in exactly one place so the highlight and the typed character
cannot disagree -- the worst bug this feature could have.

Stdlib only, and free of GTK, so the whole grid is testable without a
compositor (CLAUDE.md §5).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Layer names.
LETTERS = "letters"
SYMBOLS = "symbols"

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

#: The actions with no key of their own, and the word the legend line gives
#: each. Commit acts on whatever is highlighted, close makes the whole thing go
#: away, and recenter moves the panel rather than typing anything -- so none of
#: them can carry a badge, and without this line there would be nothing on
#: screen saying which button does them.
#:
#: Here rather than in the view so a test can check it against the bindings
#: (``tests/test_osk.py``): an action bound to a button, absent from both
#: layers and missing from this list is a shortcut with nowhere to advertise
#: itself, which is precisely the sort of thing nobody notices for a release.
OFF_GRID_HINTS: tuple[tuple[str, str], ...] = (
    ("commit", "select"),
    ("close", "close"),
    ("recenter", "recenter"),
)

#: Named keys the grid can send that are not characters.
ACTION_KEYS: dict[str, tuple[str, ...]] = {
    "back": ("BACKSPACE",),
    "enter": ("ENTER",),
    "tab": ("TAB",),
    "left": ("LEFT",),
    "right": ("RIGHT",),
}
