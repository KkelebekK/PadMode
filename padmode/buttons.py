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
ABS_X = 0x00      # 0  -- left stick horizontal
ABS_Y = 0x01      # 1  -- left stick vertical
ABS_RX = 0x03     # 3  -- right stick horizontal
ABS_RY = 0x04     # 4  -- right stick vertical
ABS_HAT0X = 0x10  # 16 -- d-pad horizontal, -1 left / +1 right
ABS_HAT0Y = 0x11  # 17 -- d-pad vertical,   -1 up   / +1 down

#: Analogue stick axis name -> absolute axis code. Unlike the d-pad, these are
#: continuous: they report a *position*, which desktop mode integrates into
#: pointer velocity rather than treating as discrete presses.
STICKS: dict[str, int] = {
    "LEFT_X": ABS_X,
    "LEFT_Y": ABS_Y,
    "RIGHT_X": ABS_RX,
    "RIGHT_Y": ABS_RY,
}

#: Stick direction name -> (axis code, sign). These let a stick push act as a
#: discrete press, the way the d-pad does, so it can be bound to a key. Binding
#: a direction takes that axis away from its analogue job (pointer or scroll).
STICK_DIRS: dict[str, tuple[int, int]] = {
    "LS_LEFT": (ABS_X, -1),
    "LS_RIGHT": (ABS_X, 1),
    "LS_UP": (ABS_Y, -1),
    "LS_DOWN": (ABS_Y, 1),
    "RS_LEFT": (ABS_RX, -1),
    "RS_RIGHT": (ABS_RX, 1),
    "RS_UP": (ABS_RY, -1),
    "RS_DOWN": (ABS_RY, 1),
}

_STICK_BY_CODE: dict[int, str] = {code: name for name, code in STICKS.items()}


#: The other axis of the same physical stick.
_SIBLING: dict[int, int] = {
    ABS_X: ABS_Y, ABS_Y: ABS_X,
    ABS_RX: ABS_RY, ABS_RY: ABS_RX,
}


def sibling_axis(ev_code: int) -> int | None:
    """The other axis of the same stick, or None if it isn't a stick axis."""
    return _SIBLING.get(ev_code)


def directions_on_axis(ev_code: int) -> tuple[str, str] | None:
    """The (negative, positive) direction names for an axis, if it is a stick."""
    negative = positive = None
    for name, (code, sign) in STICK_DIRS.items():
        if code == ev_code:
            if sign < 0:
                negative = name
            else:
                positive = name
    if negative is None or positive is None:
        return None
    return negative, positive

#: Fallback range when a device does not advertise one. Nearly every pad uses
#: a signed 16-bit range for its sticks.
DEFAULT_AXIS_RANGE = (-32768, 32767)


def stick_axis(ev_code: int) -> str | None:
    """Stick axis name for an absolute axis code, or None if it isn't a stick."""
    return _STICK_BY_CODE.get(ev_code)


def normalize_axis(value: int, lo: int, hi: int) -> float:
    """Scale a raw axis reading to -1.0..1.0, centred on the axis midpoint.

    Ranges are per-device and not always symmetric, so the midpoint is computed
    rather than assumed to be zero.
    """
    if hi <= lo:
        return 0.0
    centre = (lo + hi) / 2
    half = (hi - lo) / 2
    return max(-1.0, min(1.0, (value - centre) / half))

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

#: Mouse button name -> EV_KEY code, for desktop mode's virtual pointer.
CLICKS: dict[str, int] = {
    "LEFT": 0x110,    # 272  BTN_LEFT
    "RIGHT": 0x111,   # 273  BTN_RIGHT
    "MIDDLE": 0x112,  # 274  BTN_MIDDLE
}

# Keyboard keys, for desktop mode. Hardcoded for the same reason as BUTTONS:
# frozen kernel ABI, and it keeps this module importable without evdev. The
# letter block is QWERTY-ordered in the kernel, not alphabetical, so it is
# spelled out rather than computed.
def _row(start: int, letters: str) -> dict[str, int]:
    return {ch: start + i for i, ch in enumerate(letters)}


KEYS: dict[str, int] = {
    "ESC": 1, "BACKSPACE": 14, "TAB": 15, "ENTER": 28, "SPACE": 57,
    "MINUS": 12, "EQUAL": 13, "LEFTBRACE": 26, "RIGHTBRACE": 27,
    "SEMICOLON": 39, "APOSTROPHE": 40, "GRAVE": 41, "BACKSLASH": 43,
    "COMMA": 51, "DOT": 52, "SLASH": 53, "CAPSLOCK": 58,
    # modifiers
    "CTRL": 29, "LEFTCTRL": 29, "RIGHTCTRL": 97,
    "SHIFT": 42, "LEFTSHIFT": 42, "RIGHTSHIFT": 54,
    "ALT": 56, "LEFTALT": 56, "RIGHTALT": 100,
    "SUPER": 125, "META": 125, "LEFTMETA": 125, "RIGHTMETA": 126,
    "COMPOSE": 127, "MENU": 127,
    # navigation
    "HOME": 102, "UP": 103, "PAGEUP": 104, "LEFT": 105, "RIGHT": 106,
    "END": 107, "DOWN": 108, "PAGEDOWN": 109, "INSERT": 110, "DELETE": 111,
    # digits: KEY_1..KEY_9 are 2..10, KEY_0 is 11
    **{str(n): 1 + n for n in range(1, 10)}, "0": 11,
    # letters, in kernel (QWERTY) order
    **_row(16, "QWERTYUIOP"),
    **_row(30, "ASDFGHJKL"),
    **_row(44, "ZXCVBNM"),
    # function keys: F1..F10 are 59..68, then F11/F12 jump
    **{f"F{n}": 58 + n for n in range(1, 11)}, "F11": 87, "F12": 88,
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
    # stick directions, spelled out
    "LEFT_STICK_LEFT": "LS_LEFT",
    "LEFT_STICK_RIGHT": "LS_RIGHT",
    "LEFT_STICK_UP": "LS_UP",
    "LEFT_STICK_DOWN": "LS_DOWN",
    "RIGHT_STICK_LEFT": "RS_LEFT",
    "RIGHT_STICK_RIGHT": "RS_RIGHT",
    "RIGHT_STICK_UP": "RS_UP",
    "RIGHT_STICK_DOWN": "RS_DOWN",
    "LSTICK_LEFT": "LS_LEFT",
    "LSTICK_RIGHT": "LS_RIGHT",
    "LSTICK_UP": "LS_UP",
    "LSTICK_DOWN": "LS_DOWN",
    "RSTICK_LEFT": "RS_LEFT",
    "RSTICK_RIGHT": "RS_RIGHT",
    "RSTICK_UP": "RS_UP",
    "RSTICK_DOWN": "RS_DOWN",
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
    if key in BUTTONS or key in HATS or key in STICK_DIRS:
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


def parse_keys(spec: str) -> list[int] | None:
    """Parse a key spec like ``"alt+tab"`` into key codes, or None if invalid.

    All but the last code are treated as modifiers: they are pressed first,
    released last, so ``alt+tab`` behaves the way a person expects.
    """
    parts = [part.strip().upper() for part in spec.split("+")]
    if not all(parts):  # rejects "", "a+", "+b"
        return None
    codes = []
    for part in parts:
        code = KEYS.get(part)
        if code is None:
            return None
        codes.append(code)
    return codes


def parse_click(spec: str) -> int | None:
    """Parse a mouse button name like ``"left"`` into its EV_KEY code."""
    return CLICKS.get(spec.strip().upper())


# Characters the on-screen keyboard can type, as the keys you would press on a
# US layout. Punctuation is spelled out because there is no rule connecting a
# character to its key -- the kernel names keys by position, not by meaning.
_UNSHIFTED: dict[str, str] = {
    " ": "SPACE", "`": "GRAVE", "-": "MINUS", "=": "EQUAL",
    "[": "LEFTBRACE", "]": "RIGHTBRACE", ";": "SEMICOLON",
    "'": "APOSTROPHE", "\\": "BACKSLASH", ",": "COMMA", ".": "DOT",
    "/": "SLASH",
}

_SHIFTED: dict[str, str] = {
    "~": "GRAVE", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0", "_": "MINUS",
    "+": "EQUAL", "{": "LEFTBRACE", "}": "RIGHTBRACE", ":": "SEMICOLON",
    '"': "APOSTROPHE", "|": "BACKSLASH", "<": "COMMA", ">": "DOT",
    "?": "SLASH",
}


def keys_for_char(ch: str) -> list[int] | None:
    """Key codes that type ``ch``, modifiers first, or None if we cannot.

    Shaped like :func:`parse_keys` so the result feeds straight into
    ``VirtualInput.tap_keys``.

    **This assumes a US layout, and cannot do otherwise.** We type by sending
    kernel key codes, which name keys by *position*; the character that comes
    out is whatever the compositor's active xkb layout maps that position to.
    On any other layout the key the keyboard draws and the character the
    application receives will disagree for at least the punctuation. That is
    the price of typing through uinput rather than an input method, and it is
    the same price that buys us password fields and every toolkit
    (docs/on-screen-keyboard.md).
    """
    if len(ch) != 1:
        return None
    if ch in _UNSHIFTED:
        return [KEYS[_UNSHIFTED[ch]]]
    if ch in _SHIFTED:
        return [KEYS["SHIFT"], KEYS[_SHIFTED[ch]]]
    if ch.isdigit():
        return [KEYS[ch]]
    if "a" <= ch <= "z":
        return [KEYS[ch.upper()]]
    if "A" <= ch <= "Z":
        return [KEYS["SHIFT"], KEYS[ch]]
    return None
