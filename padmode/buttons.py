"""Friendly button names <-> Linux input event codes.

Users write ``RB`` in their config, not ``311``. This module owns that
translation and nothing else.

The integer codes are hardcoded rather than imported from ``evdev.ecodes`` on
purpose: they are frozen kernel userspace ABI (``input-event-codes.h``) and can
never change, and keeping this module dependency-free means the decision logic
that imports it stays testable on a machine with no evdev and no gamepad.

A note on X and Y, because it trips up everyone exactly once:
the kernel defines ``BTN_X == BTN_NORTH == 307`` and ``BTN_Y == BTN_WEST ==
308``. On a physical Xbox-layout pad, X sits *west* and Y sits *north*, so the
kernel's compass aliases are geometrically wrong. The numbers are still right:
a pad's X button emits 307 and its Y button emits 308. Trust the letters here,
not the compass points.
"""

from __future__ import annotations

# --- event types (linux/input-event-codes.h) ---
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03

# --- absolute axes ---
ABS_HAT0X = 0x10  # 16 -- d-pad horizontal, -1 left / +1 right
ABS_HAT0Y = 0x11  # 17 -- d-pad vertical,   -1 up   / +1 down

#: Canonical button name -> EV_KEY code.
BUTTONS: dict[str, int] = {
    "A": 0x130,       # 304  BTN_SOUTH
    "B": 0x131,       # 305  BTN_EAST
    "X": 0x133,       # 307  BTN_NORTH -- see module docstring
    "Y": 0x134,       # 308  BTN_WEST  -- see module docstring
    "LB": 0x136,      # 310  BTN_TL
    "RB": 0x137,      # 311  BTN_TR
    "LT": 0x138,      # 312  BTN_TL2   (digital trigger; many pads use ABS_Z)
    "RT": 0x139,      # 313  BTN_TR2   (digital trigger; many pads use ABS_RZ)
    "SELECT": 0x13A,  # 314  BTN_SELECT
    "START": 0x13B,   # 315  BTN_START
    "GUIDE": 0x13C,   # 316  BTN_MODE
    "L3": 0x13D,      # 317  BTN_THUMBL
    "R3": 0x13E,      # 318  BTN_THUMBR
}

#: D-pad direction name -> (absolute axis code, sign of the reported value).
HATS: dict[str, tuple[int, int]] = {
    "DPAD_UP": (ABS_HAT0Y, -1),
    "DPAD_DOWN": (ABS_HAT0Y, 1),
    "DPAD_LEFT": (ABS_HAT0X, -1),
    "DPAD_RIGHT": (ABS_HAT0X, 1),
}

#: Alternative spellings accepted in config, mapped to canonical names.
ALIASES: dict[str, str] = {
    "MODE": "GUIDE",
    "HOME": "GUIDE",
    "XBOX": "GUIDE",
    "PS": "GUIDE",
    "BACK": "SELECT",
    "VIEW": "SELECT",
    "MENU": "START",
    "OPTIONS": "START",
    "L1": "LB",
    "R1": "RB",
    "L2": "LT",
    "R2": "RT",
    "LS": "L3",
    "RS": "R3",
    "UP": "DPAD_UP",
    "DOWN": "DPAD_DOWN",
    "LEFT": "DPAD_LEFT",
    "RIGHT": "DPAD_RIGHT",
}

# Reverse lookups, built once.
_CODE_TO_BUTTON: dict[int, str] = {code: name for name, code in BUTTONS.items()}
_HAT_TO_NAME: dict[tuple[int, int], str] = {sig: name for name, sig in HATS.items()}


def canonical(name: str) -> str | None:
    """Normalise a user-supplied button name, or return None if unknown.

    Accepts any case and the aliases above, so ``"r1"``, ``"RB"`` and ``"rb"``
    all resolve to ``"RB"``.
    """
    key = name.strip().upper().replace("-", "_").replace(" ", "_")
    key = ALIASES.get(key, key)
    if key in BUTTONS or key in HATS:
        return key
    return None


def is_known(name: str) -> bool:
    """True if ``name`` is a button or d-pad direction we can bind."""
    return canonical(name) is not None


def resolve(ev_type: int, ev_code: int, ev_value: int) -> str | None:
    """Resolve a raw input event to a canonical name, or None.

    Only *activating* events resolve to a name: a key press (value 1, not a
    release or an autorepeat) or a d-pad push away from centre. Key releases
    and d-pad returns-to-centre deliberately resolve to None -- v0.1 fires
    actions on press and has no use for the release edge.
    """
    if ev_type == EV_KEY:
        if ev_value != 1:  # 0 = release, 2 = autorepeat
            return None
        return _CODE_TO_BUTTON.get(ev_code)
    if ev_type == EV_ABS:
        if ev_value == 0:  # returned to centre
            return None
        sign = 1 if ev_value > 0 else -1
        return _HAT_TO_NAME.get((ev_code, sign))
    return None


def code_of(name: str) -> int | None:
    """EV_KEY code for a canonical button name, or None for d-pad/unknown."""
    key = canonical(name)
    return BUTTONS.get(key) if key else None
