"""Virtual mouse and keyboard, created with uinput.

This is what replaces input-remapper (CLAUDE.md §3.2). `python-evdev` is
already a dependency and can create uinput devices directly, which removes both
a heavy dependency and the "hand-build two presets in a GUI" install step.

Two devices, not one (see `docs/virtual-input.md`): a device advertising both
relative axes and a full keymap is unusual enough that some applications
misclassify it. Their names start with ``padmode`` so
:func:`padmode.input.reader.find_gamepad` skips them and we can never read back
our own output.
"""

from __future__ import annotations

import fcntl
import glob
import logging
import os
import time

from evdev import UInput, ecodes

from .. import buttons

log = logging.getLogger(__name__)

POINTER_NAME = "padmode virtual pointer"
KEYBOARD_NAME = "padmode virtual keyboard"

_POINTER_CAPS = {
    ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL, ecodes.REL_HWHEEL],
    ecodes.EV_KEY: sorted(buttons.CLICKS.values()),
}

# Advertise the whole keyboard range rather than only the keys we bind by
# default, so a user can bind any key in config without us shipping a new
# device definition.
_KEYBOARD_CAPS = {ecodes.EV_KEY: sorted(set(buttons.KEYS.values()))}


class VirtualInput:
    """Owns the virtual devices and everything currently pressed on them.

    Devices are created on first use rather than at startup: a user who never
    enters desktop mode should not have two phantom input devices sitting in
    their session. Once created they are kept, because creating a uinput device
    takes long enough that doing it per activation loses the first events while
    the compositor notices the new hardware.
    """

    def __init__(self) -> None:
        self._pointer: UInput | None = None
        self._keyboard: UInput | None = None
        self._pointer_path: str | None = None
        self._keyboard_path: str | None = None
        self._held: set[tuple[str, int]] = set()
        self._acc_x = 0.0
        self._acc_y = 0.0
        self._acc_scroll_x = 0.0
        self._acc_scroll_y = 0.0
        self._scrolling = False

    # -- lifecycle -----------------------------------------------------
    @property
    def active(self) -> bool:
        return self._pointer is not None

    @property
    def pointer_path(self) -> str | None:
        """Device node of our virtual pointer, or None if not open.

        Resolved by watching which node appears as we create the device, not
        by looking the name up afterwards. ``UInput.device`` does the latter,
        which is ambiguous the moment a second padmode is running -- the names
        are identical -- and races with udev besides, returning None if the
        node has not been published yet.
        """
        return self._pointer_path

    @property
    def keyboard_path(self) -> str | None:
        """Device node of our virtual keyboard, or None if not open."""
        return self._keyboard_path

    def open(self) -> bool:
        """Create the devices. Returns False if uinput isn't available."""
        if self._pointer is not None:
            return True
        # Built locally and published in one step at the end. Assigning as we
        # go would make `active` true while the keyboard is still being
        # created, and anything acting on that -- the tests do -- would see a
        # half-open device with no keyboard path.
        pointer = keyboard = None
        try:
            pointer = UInput(_POINTER_CAPS, name=POINTER_NAME, version=1)
            pointer_path = _node_of(pointer)
            keyboard = UInput(_KEYBOARD_CAPS, name=KEYBOARD_NAME, version=1)
            keyboard_path = _node_of(keyboard)
        except (OSError, ValueError) as exc:
            log.error(
                "cannot create virtual input devices (%s). Desktop mode needs "
                "write access to /dev/uinput -- install the udev rule and make "
                "sure you are in the 'input' group.", exc,
            )
            for partial in (pointer, keyboard):
                if partial is not None:
                    try:
                        partial.close()
                    except (OSError, ValueError):
                        pass
            return False

        self._pointer, self._pointer_path = pointer, pointer_path
        self._keyboard, self._keyboard_path = keyboard, keyboard_path
        log.info("virtual input ready")
        return True

    def close(self) -> None:
        """Release everything held, then destroy the devices."""
        self.release_all()
        for attr in ("_pointer", "_keyboard"):
            device = getattr(self, attr)
            if device is not None:
                try:
                    device.close()
                except (OSError, ValueError):
                    pass
                setattr(self, attr, None)
        self._pointer_path = self._keyboard_path = None

    # -- output --------------------------------------------------------
    def move(self, dx: float, dy: float) -> None:
        """Move the pointer, carrying sub-pixel remainder between calls.

        Without the accumulator, a slow stick deflection produces a delta below
        1px every tick, every one of those rounds to zero, and the pointer
        never moves at all no matter how long you hold it.
        """
        self._acc_x += dx
        self._acc_y += dy
        step_x, self._acc_x = _split(self._acc_x)
        step_y, self._acc_y = _split(self._acc_y)
        if step_x == 0 and step_y == 0:
            return
        if self._pointer is None:
            return
        if step_x:
            self._pointer.write(ecodes.EV_REL, ecodes.REL_X, step_x)
        if step_y:
            self._pointer.write(ecodes.EV_REL, ecodes.REL_Y, step_y)
        self._syn(self._pointer)

    def scroll(self, dx: float, dy: float) -> None:
        """Scroll, accumulating fractional wheel clicks like :meth:`move`.

        The wheel is far coarser than the pointer -- ten clicks a second
        against hundreds of pixels -- so without accumulation a gentle stick
        push would round to zero and never scroll at all.

        The first click of a gesture is emitted immediately instead of waiting
        for the accumulator to fill. At ten clicks per second that wait is a
        sixth of a second at full deflection and over half a second at half,
        which reads as the scroll being broken rather than gradual.
        """
        if dx == 0.0 and dy == 0.0:
            self._scrolling = False
            self._acc_scroll_x = self._acc_scroll_y = 0.0
            return

        if not self._scrolling:
            # Starting from rest: fire one click now, in whichever directions
            # are actually moving, and accumulate from empty afterwards.
            self._scrolling = True
            self._acc_scroll_x = self._acc_scroll_y = 0.0
            step_x = _sign(dx)
            step_y = _sign(dy)
        else:
            self._acc_scroll_x += dx
            self._acc_scroll_y += dy
            step_x, self._acc_scroll_x = _split(self._acc_scroll_x)
            step_y, self._acc_scroll_y = _split(self._acc_scroll_y)

        if (step_x == 0 and step_y == 0) or self._pointer is None:
            return
        if step_y:
            self._pointer.write(ecodes.EV_REL, ecodes.REL_WHEEL, step_y)
        if step_x:
            self._pointer.write(ecodes.EV_REL, ecodes.REL_HWHEEL, step_x)
        self._syn(self._pointer)

    def click(self, code: int, pressed: bool) -> None:
        self._emit(self._pointer, "click", code, pressed)

    def key(self, code: int, pressed: bool) -> None:
        self._emit(self._keyboard, "key", code, pressed)

    def tap_keys(self, codes: list[int]) -> None:
        """Press a key combination and release it, modifiers outermost.

        ``[ALT, TAB]`` presses alt, presses tab, releases tab, releases alt --
        which is what makes alt+tab actually switch windows rather than emit
        two unrelated keystrokes.
        """
        for code in codes:
            self.key(code, True)
        for code in reversed(codes):
            self.key(code, False)

    def release_all(self) -> None:
        """Release every button and key we are still holding.

        Load-bearing on the way out of desktop mode: leaving while holding the
        button bound to left-click would otherwise leave the virtual mouse
        button down forever, and the session unusable.
        """
        for kind, code in sorted(self._held):
            device = self._pointer if kind == "click" else self._keyboard
            if device is None:
                continue
            try:
                device.write(ecodes.EV_KEY, code, 0)
                self._syn(device)
            except (OSError, ValueError):
                pass
        self._held.clear()
        self._acc_x = self._acc_y = 0.0
        self._acc_scroll_x = self._acc_scroll_y = 0.0
        self._scrolling = False

    # -- internals -----------------------------------------------------
    def _emit(self, device: UInput | None, kind: str, code: int, pressed: bool) -> None:
        if device is None:
            return
        entry = (kind, code)
        # Mirror the kernel: only a real state change is worth emitting.
        if pressed and entry in self._held:
            return
        if not pressed and entry not in self._held:
            return
        try:
            device.write(ecodes.EV_KEY, code, 1 if pressed else 0)
            self._syn(device)
        except (OSError, ValueError) as exc:
            log.warning("virtual write failed: %s", exc)
            return
        self._held.add(entry) if pressed else self._held.discard(entry)

    @staticmethod
    def _syn(device: UInput) -> None:
        try:
            device.syn()
        except (OSError, ValueError):
            pass


def _split(value: float) -> tuple[int, float]:
    """Split into a whole part to emit and a remainder to carry forward."""
    whole = int(value)
    return whole, value - whole


#: UI_GET_SYSNAME(64) from linux/uinput.h, expanded from the _IOC macros:
#: direction _IOC_READ (2), size 64, type 'U', number 44.
_UI_GET_SYSNAME_LEN = 64
_UI_GET_SYSNAME = (2 << 30) | (_UI_GET_SYSNAME_LEN << 16) | (ord("U") << 8) | 44


def _node_of(device: UInput, timeout: float = 1.0) -> str | None:
    """Ask the kernel which input node this uinput device created.

    Two more obvious approaches are both unreliable. ``UInput.device`` looks
    the device up by *name*, which is ambiguous as soon as a second padmode is
    running. Diffing /dev/input around creation looks sound but is not: event
    numbers are recycled, so a node still being torn down can occupy the very
    number the new device is about to get, and the diff comes back empty.

    UI_GET_SYSNAME asks the fd itself, so neither race applies.
    """
    buf = bytearray(_UI_GET_SYSNAME_LEN)
    try:
        fcntl.ioctl(device.fd, _UI_GET_SYSNAME, buf)
    except OSError:
        return None
    sysname = buf.split(b"\x00")[0].decode(errors="replace")
    if not sysname:
        return None

    # The sysfs entry can trail the ioctl by a moment.
    deadline = time.monotonic() + timeout
    while True:
        for entry in glob.glob(f"/sys/class/input/{sysname}/event*"):
            return f"/dev/input/{os.path.basename(entry)}"
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def _sign(value: float) -> int:
    """-1, 0 or 1 -- one wheel click in the direction of travel."""
    if value > 0:
        return 1
    return -1 if value < 0 else 0
