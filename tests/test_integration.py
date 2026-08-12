"""Integration tests against a synthetic gamepad.

CLAUDE.md §5: unit tests cover the decision logic, and integration tests create
a *real* uinput device, replay event sequences through the actual evdev read
loop, and assert on what the daemon did. This is the only automated coverage of
device discovery, grabbing, and the disconnect path.

Requires write access to /dev/uinput (the 'input' group plus the shipped udev
rule). Skipped automatically when that isn't available, so the suite still
passes on a machine or CI runner without it.
"""

from __future__ import annotations

import threading
import time
import unittest

try:
    from evdev import AbsInfo, UInput, ecodes
    HAVE_EVDEV = True
except ImportError:  # pragma: no cover
    HAVE_EVDEV = False

from padmode.config import Config
from padmode.daemon import Daemon

# Deliberately not prefixed with "padmode": the reader skips devices with that
# prefix so it never grabs its own virtual pad (v0.2). Naming this one
# "padmode-test" would make it invisible and these tests would hang.
DEVICE_NAME = "selftest gamepad"

DEADLINE = 5.0


def _uinput_available() -> bool:
    if not HAVE_EVDEV:
        return False
    try:
        with open("/dev/uinput", "wb"):
            return True
    except OSError:
        return False


def _gamepad_capabilities() -> dict:
    axis = AbsInfo(value=0, min=-32768, max=32767, fuzz=0, flat=0, resolution=0)
    hat = AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)
    return {
        ecodes.EV_KEY: [
            ecodes.BTN_SOUTH, ecodes.BTN_EAST, ecodes.BTN_NORTH, ecodes.BTN_WEST,
            ecodes.BTN_TL, ecodes.BTN_TR, ecodes.BTN_SELECT, ecodes.BTN_START,
            ecodes.BTN_MODE, ecodes.BTN_THUMBL, ecodes.BTN_THUMBR,
        ],
        ecodes.EV_ABS: [
            (ecodes.ABS_X, axis),
            (ecodes.ABS_Y, axis),
            (ecodes.ABS_HAT0X, hat),
            (ecodes.ABS_HAT0Y, hat),
        ],
    }


def _wait_for(predicate, deadline: float = DEADLINE) -> bool:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class RecordingRunner:
    """Stands in for ActionRunner, capturing bindings instead of spawning."""

    def __init__(self) -> None:
        self.ran: list = []
        self.preflighted = False

    def preflight(self, _bindings) -> None:
        self.preflighted = True

    def run(self, binding) -> bool:
        self.ran.append(binding)
        return True


@unittest.skipUnless(_uinput_available(), "needs write access to /dev/uinput")
class TestSyntheticGamepad(unittest.TestCase):
    """Drives the real daemon with a real (virtual) device."""

    def setUp(self):
        from padmode.input import GamepadReader

        self.ui = UInput(_gamepad_capabilities(), name=DEVICE_NAME, version=1)
        self.addCleanup(self._close_device)

        self.runner = RecordingRunner()
        self.reader = GamepadReader(name=DEVICE_NAME, retry_delay=0.05)
        self.daemon = Daemon(Config(), self.reader, self.runner)

        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

        if not _wait_for(lambda: self.reader.device is not None):
            self.fail(f"reader never found the synthetic device {DEVICE_NAME!r}")

    def _close_device(self):
        # Closing twice is normal here: one test closes the device as the thing
        # under test, and cleanup then closes it again.
        try:
            self.ui.close()
        except (OSError, ValueError):
            pass

    def _shutdown(self):
        self.reader.stop()
        # stop() only takes effect when the blocking read returns, so nudge the
        # loop with a harmless event. It has to be a real *state change* or the
        # input core silently drops it -- a key release for a button that was
        # never pressed emits nothing at all. An analogue stick move always
        # changes state and is never a binding (see the test below).
        # Without this the thread survives the test, and the next test's device
        # -- same name -- gets grabbed by the stale daemon and loses its events.
        try:
            self.axis(ecodes.ABS_X, 4242)
        except (OSError, ValueError):
            pass  # device already closed; the loop is unblocked anyway
        self.thread.join(timeout=DEADLINE)
        self.assertFalse(self.thread.is_alive(), "daemon thread outlived the test")

    # -- helpers -------------------------------------------------------
    def key(self, code: int, value: int) -> None:
        self.ui.write(ecodes.EV_KEY, code, value)
        self.ui.syn()

    def axis(self, code: int, value: int) -> None:
        self.ui.write(ecodes.EV_ABS, code, value)
        self.ui.syn()

    def expect_ran(self, count: int) -> bool:
        return _wait_for(lambda: len(self.runner.ran) >= count)

    # -- tests ---------------------------------------------------------
    def test_discovery_picked_our_device(self):
        self.assertEqual(self.reader.device.name, DEVICE_NAME)
        self.assertTrue(self.runner.preflighted)

    def test_guide_plus_rb_runs_media_next(self):
        self.key(ecodes.BTN_MODE, 1)
        self.assertTrue(_wait_for(lambda: self.reader.grabbed),
                        "holding the modifier should grab the device")
        self.key(ecodes.BTN_TR, 1)
        self.key(ecodes.BTN_TR, 0)
        self.assertTrue(self.expect_ran(1), "binding never fired")
        self.key(ecodes.BTN_MODE, 0)
        self.assertTrue(_wait_for(lambda: not self.reader.grabbed),
                        "releasing the modifier should release the grab")
        self.assertEqual(self.runner.ran, ["media.next"])

    def test_buttons_are_ignored_without_the_modifier(self):
        self.key(ecodes.BTN_TR, 1)
        self.key(ecodes.BTN_TR, 0)
        self.key(ecodes.BTN_SOUTH, 1)
        self.key(ecodes.BTN_SOUTH, 0)
        # Give the loop a chance to do the wrong thing before asserting.
        time.sleep(0.25)
        self.assertEqual(self.runner.ran, [])
        self.assertFalse(self.reader.grabbed)

    def test_dpad_controls_volume_while_held(self):
        self.key(ecodes.BTN_MODE, 1)
        self.assertTrue(_wait_for(lambda: self.reader.grabbed))
        self.axis(ecodes.ABS_HAT0Y, -1)
        self.axis(ecodes.ABS_HAT0Y, 0)
        self.axis(ecodes.ABS_HAT0Y, 1)
        self.axis(ecodes.ABS_HAT0Y, 0)
        self.assertTrue(self.expect_ran(2))
        self.key(ecodes.BTN_MODE, 0)
        self.assertEqual(self.runner.ran, ["audio.volume_up", "audio.volume_down"])

    def test_analogue_stick_movement_is_not_a_binding(self):
        self.key(ecodes.BTN_MODE, 1)
        self.assertTrue(_wait_for(lambda: self.reader.grabbed))
        for value in (-20000, 0, 20000, 0):
            self.axis(ecodes.ABS_X, value)
        self.key(ecodes.BTN_MODE, 0)
        time.sleep(0.25)
        self.assertEqual(self.runner.ran, [])

    def test_grab_is_released_when_the_device_disappears(self):
        self.key(ecodes.BTN_MODE, 1)
        self.assertTrue(_wait_for(lambda: self.reader.grabbed))
        # Unplug while grabbed -- the worst case, and the one that would
        # otherwise leave a real pad captured and the machine uncontrollable.
        self._close_device()
        self.assertTrue(_wait_for(lambda: not self.reader.grabbed),
                        "grab must not survive the device vanishing")


@unittest.skipUnless(_uinput_available(), "needs write access to /dev/uinput")
class TestDeviceSelection(unittest.TestCase):
    def test_capability_matching_rejects_a_keyboard(self):
        from padmode.input import find_gamepad, looks_like_gamepad

        keyboard = UInput({ecodes.EV_KEY: [ecodes.KEY_A, ecodes.KEY_B]},
                          name="selftest keyboard", version=1)
        self.addCleanup(keyboard.close)
        pad = UInput(_gamepad_capabilities(), name=DEVICE_NAME, version=1)
        self.addCleanup(pad.close)

        self.assertTrue(_wait_for(lambda: find_gamepad(DEVICE_NAME) is not None))
        found = find_gamepad(DEVICE_NAME)
        self.assertEqual(found.name, DEVICE_NAME)
        self.assertTrue(looks_like_gamepad(found))

        # A keyboard must never be selectable as a gamepad, however it is named.
        self.assertIsNone(find_gamepad("selftest keyboard"))
