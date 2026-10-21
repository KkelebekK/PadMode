"""Typing the login password with the pad, while the screen is locked (v0.7).

The second half of the unlock module; :mod:`padmode.lock` is the first, and
knows *when* this applies. Nothing here touches a device: :func:`step` turns
one input event into key codes to tap, and the daemon taps them on a keypad
that exists only while the screen is locked (``docs/virtual-input.md``).

Nothing is drawn, so nothing can be read off the screen -- the lock screen
hides every layer surface but its own, measured with the on-screen keyboard.
The only feedback is the count of dots in the password field. That is why the
input is laid out to be learnt by feel rather than looked up:

* **The left stick is a dial of eight digits**, 1 at the top and counting
  clockwise -- 45 degrees a digit. Push towards one and let go: the digit is
  typed as the stick comes home. Committing on the release, rather than on the
  push, is what makes aiming forgiving -- you can push, feel where you are,
  correct, and only then let go.
* **X is 9 and Y is 0**, START sends, SELECT deletes one, B deletes all.

Every one of those is a default in ``[unlock.bindings]``; any button, d-pad
direction or sector of either stick can be given any digit or action.

The stick needs one guard the buttons do not. A stick let go of springs back
*past* centre, and a hard flick can carry it over the threshold on the far
side -- which here would aim, and on the next return type, the opposite digit.
For ``desktop.stick_settle`` after a commit, a deflection towards the three
sectors facing the one just typed is taken for that rebound and ignored. The
same sector is never blocked, so "11" is as fast as "12".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import buttons

#: The eight sectors of a stick, from straight up and clockwise. The index is
#: the sector number :func:`sector_of` returns.
_SECTOR_SUFFIXES = (
    "UP", "UP_RIGHT", "RIGHT", "DOWN_RIGHT",
    "DOWN", "DOWN_LEFT", "LEFT", "UP_LEFT",
)

#: Stick prefix -> its (x, y) axes.
STICK_AXES: dict[str, tuple[int, int]] = {
    "LS": (buttons.ABS_X, buttons.ABS_Y),
    "RS": (buttons.ABS_RX, buttons.ABS_RY),
}

#: Every bindable sector name: ``LS_UP``, ``LS_UP_RIGHT`` ... ``RS_UP_LEFT``.
SECTORS: tuple[str, ...] = tuple(
    f"{stick}_{suffix}" for stick in STICK_AXES for suffix in _SECTOR_SUFFIXES
)

_AXIS_TO_STICK: dict[int, str] = {
    axis: stick for stick, pair in STICK_AXES.items() for axis in pair
}

#: Actions a binding can name besides a digit.
ENTER = "enter"
BACKSPACE = "backspace"
CLEAR = "clear"
ACTIONS = frozenset({*"0123456789", ENTER, BACKSPACE, CLEAR})

DEFAULT_BINDINGS: dict[str, str] = {
    **{f"LS_{suffix}": str(n + 1) for n, suffix in enumerate(_SECTOR_SUFFIXES)},
    "X": "9",
    "Y": "0",
    "START": ENTER,
    "SELECT": BACKSPACE,
    "B": CLEAR,
}

#: The keys an action taps. Top-row digits rather than the keypad's: those need
#: Num Lock, which a lock screen will not tell us the state of. The top row
#: types digits on every Latin layout we know of, Turkish Q included.
KEY_ENTER = buttons.KEYS["ENTER"]
KEY_BACKSPACE = buttons.KEYS["BACKSPACE"]
_DIGIT_KEYS: dict[str, int] = {d: buttons.KEYS[d] for d in "0123456789"}

#: Every key the keypad ever taps -- and therefore every key it advertises.
#: A device that can only type these cannot type into anything that matters,
#: whatever goes wrong.
KEYPAD_KEYS: tuple[int, ...] = tuple(
    sorted({*_DIGIT_KEYS.values(), KEY_ENTER, KEY_BACKSPACE})
)

#: How far below the threshold the stick must come back before the digit is
#: typed, as a share of it. Lower than the threshold so a thumb resting near the
#: edge does not type a stream of digits.
RELEASE_RATIO = 0.5


def canonical_sector(name: str) -> str | None:
    """``"ls_up_right"`` -> ``"LS_UP_RIGHT"``; None if it is not a sector."""
    key = name.strip().upper().replace("-", "_").replace(" ", "_")
    return key if key in SECTORS else None


def sector_of(x: float, y: float) -> int:
    """Which of eight sectors ``(x, y)`` points into: 0 is up, then clockwise.

    ``y`` is the axis as the kernel reports it, negative upwards.
    """
    angle = math.degrees(math.atan2(x, -y)) % 360.0
    return int((angle + 22.5) // 45.0) % 8


@dataclass
class StickAim:
    """One stick, as far as typing digits is concerned."""

    x: float = 0.0
    y: float = 0.0
    #: An axis moved since the last frame ended, so it is due a look.
    dirty: bool = False
    #: The sector the stick points into while pushed past the threshold.
    #: None while it is home.
    aimed: int | None = None
    #: Sector just typed, and until when its rebound is ignored.
    typed: int | None = None
    settle_until: float = 0.0


@dataclass
class UnlockState:
    """What :func:`step` remembers between events. Reset on every lock."""

    sticks: dict[str, StickAim] = field(default_factory=dict)
    #: How many characters the pad has put in the field since it was last
    #: emptied. ``clear`` deletes exactly these -- not what someone typed on a
    #: real keyboard, which we cannot see.
    typed: int = 0


def step(
    ev_type: int,
    ev_code: int,
    ev_value: int,
    state: UnlockState,
    bindings: dict[str, str],
    threshold: float,
    settle: float,
    axis_ranges: dict[int, tuple[int, int]],
    now: float,
) -> tuple[int, ...]:
    """Key codes to tap for one input event, in order. Mutates ``state`` only.

    A stick is only *looked at* when a frame ends (``SYN_REPORT``), never per
    axis. The two axes of one movement arrive as two events, and between them
    the stick is somewhere it never was: coming home from up-right, X lands
    first and for one event the stick reads as straight up -- which typed 1
    for 2 until this waited for the frame.
    """
    if ev_type == buttons.EV_ABS and ev_code in _AXIS_TO_STICK:
        stick = _AXIS_TO_STICK[ev_code]
        if not any(name.startswith(stick + "_") for name in bindings):
            return ()
        lo, hi = axis_ranges.get(ev_code, buttons.DEFAULT_AXIS_RANGE)
        value = buttons.normalize_axis(ev_value, lo, hi)
        aim = state.sticks.setdefault(stick, StickAim())
        if ev_code == STICK_AXES[stick][0]:
            aim.x = value
        else:
            aim.y = value
        aim.dirty = True
        return ()

    if ev_type == buttons.EV_SYN:
        keys: tuple[int, ...] = ()
        for stick, aim in state.sticks.items():
            if not aim.dirty:
                continue
            aim.dirty = False
            sector = _track(aim, threshold, settle, now)
            if sector is not None:
                keys += _act(
                    bindings.get(f"{stick}_{_SECTOR_SUFFIXES[sector]}"), state
                )
        return keys

    # Buttons and the d-pad act on the press. A release, an autorepeat and a
    # d-pad coming home all resolve to None.
    name = buttons.resolve(ev_type, ev_code, ev_value)
    if name is None:
        return ()
    return _act(bindings.get(name), state)


def _track(aim: StickAim, threshold: float, settle: float, now: float) -> int | None:
    """Follow one stick; the sector to type when it comes home, else None."""
    magnitude = math.hypot(aim.x, aim.y)
    if magnitude >= threshold:
        sector = sector_of(aim.x, aim.y)
        if aim.aimed is None and _is_rebound(aim, sector, now):
            return None
        # The latest sector wins, so a push can be corrected before letting go.
        aim.aimed = sector
        return None
    if aim.aimed is not None and magnitude <= threshold * RELEASE_RATIO:
        sector, aim.aimed = aim.aimed, None
        aim.typed, aim.settle_until = sector, now + settle
        return sector
    return None


def _is_rebound(aim: StickAim, sector: int, now: float) -> bool:
    """True for a push that is really the spring-back from the last digit."""
    if aim.typed is None or now >= aim.settle_until:
        return False
    # 0 = the same sector, 4 = straight across.
    apart = (sector - aim.typed) % 8
    return min(apart, 8 - apart) >= 3


def _act(action: str | None, state: UnlockState) -> tuple[int, ...]:
    if action is None:
        return ()
    if action in _DIGIT_KEYS:
        state.typed += 1
        return (_DIGIT_KEYS[action],)
    if action == BACKSPACE:
        state.typed = max(0, state.typed - 1)
        return (KEY_BACKSPACE,)
    if action == CLEAR:
        count, state.typed = state.typed, 0
        return (KEY_BACKSPACE,) * count
    if action == ENTER:
        # A wrong password empties the field, a right one ends the lock; either
        # way nothing we typed is left in it.
        state.typed = 0
        return (KEY_ENTER,)
    return ()
