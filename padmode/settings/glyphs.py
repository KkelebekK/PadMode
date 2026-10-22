"""What a pad's buttons are called and drawn as, by the pad in your hands.

padmode names buttons the Xbox way everywhere (``A``, ``RB``, ``START``),
because the kernel does and the config has to say something. The window
shows them the way *your* controller prints them: ✕ ○ △ □ and L1/R1 on a
PlayStation pad, the Nintendo letters and ZL/ZR on a Switch pad. Chosen by
the maintainer, together with the coloured tiles.

The face buttons are matched by **position**, through the kernel's codes:
padmode's ``A`` is the south button, ``B`` east, ``X`` north, ``Y`` west
(``padmode/buttons.py``). On a PlayStation pad that is ✕, ○, △ and □. On a
Nintendo pad it is B, A, X and Y -- the letters sit in other places there,
which is exactly why the label has to follow the pad.

**Untried on real Sony and Nintendo hardware**: the maintainer's pad is an
Xbox-layout one. The family can be forced in ``[appearance] glyphs``.
"""

from __future__ import annotations

SONY = 0x054C
NINTENDO = 0x057E
MICROSOFT = 0x045E

FAMILIES = ("xbox", "playstation", "nintendo")


def family_of(name: str | None, vendor: int | None) -> str:
    """``xbox``, ``playstation`` or ``nintendo``. Unknown pads look like Xbox
    ones, since that is what padmode's own names already are."""
    text = (name or "").lower()
    if vendor == SONY or any(w in text for w in ("dualsense", "dualshock",
                                                 "playstation", "wireless controller")):
        return "playstation"
    if vendor == NINTENDO or any(w in text for w in ("nintendo", "switch", "joy-con")):
        return "nintendo"
    return "xbox"


#: (label, colour) per face button. Colour is an index into the badge set:
#: a, b, x, y in the theme -- or the family's own when it has one.
_FACE = {
    "xbox": {"A": ("A", "a"), "B": ("B", "b"), "X": ("X", "x"), "Y": ("Y", "y")},
    # PlayStation's own colours: cross blue, circle red, triangle green,
    # square pink. Reused through the a/b/x/y slots in that order.
    "playstation": {"A": ("✕", "ps-cross"), "B": ("○", "ps-circle"),
                    "X": ("△", "ps-triangle"), "Y": ("□", "ps-square")},
    "nintendo": {"A": ("B", "chip"), "B": ("A", "chip"), "X": ("X", "chip"),
                 "Y": ("Y", "chip")},
}

_OTHERS = {
    "xbox": {"LB": "LB", "RB": "RB", "LT": "LT", "RT": "RT", "START": "Menu",
             "SELECT": "View", "GUIDE": "Xbox", "L3": "LS", "R3": "RS"},
    "playstation": {"LB": "L1", "RB": "R1", "LT": "L2", "RT": "R2",
                    "START": "Options", "SELECT": "Create", "GUIDE": "PS",
                    "L3": "L3", "R3": "R3"},
    "nintendo": {"LB": "L", "RB": "R", "LT": "ZL", "RT": "ZR", "START": "+",
                 "SELECT": "−", "GUIDE": "Home", "L3": "LS", "R3": "RS"},
}

_ARROWS = {"UP": "↑", "DOWN": "↓", "LEFT": "←", "RIGHT": "→", "UP_RIGHT": "↗",
           "DOWN_RIGHT": "↘", "DOWN_LEFT": "↙", "UP_LEFT": "↖"}


def glyph(button: str, family: str) -> tuple[str, str]:
    """``(text, style)`` for one of padmode's button names on this family.

    ``style`` is ``a``/``b``/``x``/``y`` (the theme's face colours), one of
    the ``ps-*`` colours, or ``chip`` for a plain grey key.
    """
    family = family if family in FAMILIES else "xbox"
    if button in _FACE[family]:
        return _FACE[family][button]
    if button in _OTHERS[family]:
        return _OTHERS[family][button], "chip"
    if button.startswith("DPAD_"):
        return "D" + _ARROWS[button[5:]], "chip"
    if button.startswith(("LS_", "RS_")):
        stick = _OTHERS[family]["L3" if button[0] == "L" else "R3"]
        return stick + _ARROWS[button[3:]], "chip"
    return button, "chip"
