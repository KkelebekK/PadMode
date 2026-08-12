"""End-to-end desktop mode: synthetic pad in, real virtual devices out.

This is the only automated proof that the uinput output path works. It drives
the daemon with a synthetic gamepad, then opens padmode's *own* virtual pointer
and keyboard as ordinary input devices and asserts on what came out of them.

Skipped without /dev/uinput access, like the other integration tests.
"""

from __future__ import annotations

import threading
import time
import unittest

try:
    from evdev import InputDevice, UInput, ecodes
    HAVE_EVDEV = True
except ImportError:  # pragma: no cover
    HAVE_EVDEV = False

from padmode import buttons
from padmode.config import Config
from padmode.daemon import Daemon

from .test_integration import (
    DEADLINE,
    DEVICE_NAME,
    RecordingRunner,
    _gamepad_capabilities,
    _uinput_available,
    _wait_for,
)


class Sink:
    """Reads a virtual device in the background and records what arrives."""

    def __init__(self, device: InputDevice) -> None:
        self.device = device
        self.events: list[tuple[int, int, int]] = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for event in self.device.read_loop():
                self.events.append((event.type, event.code, event.value))
                if self._stop:
                    return
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        self._stop = True
        try:
            self.device.close()
        except (OSError, ValueError):
            pass

    def rel_total(self, code: int) -> int:
        return sum(v for t, c, v in self.events if t == ecodes.EV_REL and c == code)

    def key_events(self, code: int) -> list[int]:
        return [v for t, c, v in self.events if t == ecodes.EV_KEY and c == code]


@unittest.skipUnless(_uinput_available(), "needs write access to /dev/uinput")
class TestDesktopModeEndToEnd(unittest.TestCase):
    def setUp(self):
        from padmode.input import GamepadReader, VirtualInput

        self.ui = UInput(_gamepad_capabilities(), name=DEVICE_NAME, version=1)
        self.addCleanup(self._close_device)

        self.virtual = VirtualInput()
        self.reader = GamepadReader(name=DEVICE_NAME, retry_delay=0.05)
        self.daemon = Daemon(Config(), self.reader, RecordingRunner(), self.virtual)
        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

        if not _wait_for(lambda: self.reader.device is not None):
            self.fail("reader never found the synthetic device")
        self.sinks: list[Sink] = []

    def _close_device(self):
        try:
            self.ui.close()
        except (OSError, ValueError):
            pass

    def _shutdown(self):
        for sink in getattr(self, "sinks", []):
            sink.close()
        self.reader.stop()
        # Wake the blocked read so stop() takes effect. It must be an axis the
        # device actually advertises, or nothing is emitted and every teardown
        # waits out the join timeout. 4242 sits inside the dead zone, so it
        # cannot move the pointer on the way out.
        try:
            self.ui.write(ecodes.EV_ABS, ecodes.ABS_X, 4242)
            self.ui.syn()
        except (OSError, ValueError):
            pass
        self.thread.join(timeout=DEADLINE)

    # -- helpers -------------------------------------------------------
    def key(self, code, value):
        self.ui.write(ecodes.EV_KEY, code, value)
        self.ui.syn()

    def axis(self, code, value):
        self.ui.write(ecodes.EV_ABS, code, value)
        self.ui.syn()

    def enter_desktop(self):
        self.key(ecodes.BTN_MODE, 1)
        self.assertTrue(_wait_for(lambda: self.reader.grabbed))
        self.key(ecodes.BTN_THUMBL, 1)  # L3
        self.key(ecodes.BTN_THUMBL, 0)
        self.assertTrue(_wait_for(lambda: self.virtual.active),
                        "virtual devices were never created")
        self.key(ecodes.BTN_MODE, 0)

    def sink_for(self, path):
        """Attach to one of *our* virtual devices, by path.

        Never by name: a padmode daemon running on the same machine has
        devices with identical names, and attaching to its pointer instead of
        ours would record nothing and fail for a baffling reason.
        """
        self.assertIsNotNone(path, "virtual device was never created")
        sink = Sink(InputDevice(path))
        self.sinks.append(sink)
        return sink

    @property
    def pointer(self):
        return self.sink_for(self.virtual.pointer_path)

    @property
    def keyboard(self):
        return self.sink_for(self.virtual.keyboard_path)

    # -- tests ---------------------------------------------------------
    def test_virtual_devices_appear_only_after_entering(self):
        # Lazy creation: someone who never uses desktop mode should not get
        # two phantom input devices in their session.
        self.assertFalse(self.virtual.active,
                         "virtual devices existed before desktop mode was used")
        self.enter_desktop()
        self.assertTrue(self.virtual.active)
        self.assertIsNotNone(self.virtual.pointer_path)

    def test_left_stick_moves_the_pointer_while_held(self):
        self.enter_desktop()
        pointer = self.pointer
        # Push and hold. A single event, then silence -- if motion depended on
        # incoming events rather than the tick, nothing would move.
        self.axis(ecodes.ABS_X, 32767)
        self.assertTrue(_wait_for(lambda: pointer.rel_total(ecodes.REL_X) > 50))
        self.axis(ecodes.ABS_X, 0)

        moved = pointer.rel_total(ecodes.REL_X)
        self.assertGreater(moved, 0, "pointer never moved right")
        time.sleep(0.2)
        self.assertAlmostEqual(pointer.rel_total(ecodes.REL_X), moved, delta=2,
                               msg="pointer kept moving after the stick recentred")

    def test_stick_direction_maps_to_pointer_direction(self):
        self.enter_desktop()
        pointer = self.pointer
        self.axis(ecodes.ABS_Y, -32768)  # stick up
        self.assertTrue(_wait_for(lambda: pointer.rel_total(ecodes.REL_Y) < -50))
        self.axis(ecodes.ABS_Y, 0)
        self.assertLess(pointer.rel_total(ecodes.REL_Y), 0, "up should be negative")

    def test_dead_zone_means_a_resting_stick_does_not_drift(self):
        self.enter_desktop()
        pointer = self.pointer
        self.axis(ecodes.ABS_X, 1000)  # inside the default dead zone
        time.sleep(0.3)
        self.assertEqual(pointer.rel_total(ecodes.REL_X), 0)

    def test_scroll_starts_without_waiting_for_the_accumulator(self):
        """A gentle push must scroll promptly, not after a visible pause.

        At 10 clicks per second a third-deflection push takes over three
        seconds to fill the accumulator, which reads as scrolling being broken.
        The first click of a gesture is emitted immediately instead.
        """
        self.enter_desktop()
        pointer = self.pointer
        self.axis(ecodes.ABS_RY, -9830)  # ~30% deflection, upward
        self.assertTrue(
            _wait_for(lambda: pointer.rel_total(ecodes.REL_WHEEL) > 0, deadline=0.5),
            "no wheel event within half a second of pushing the stick",
        )

    def test_each_new_scroll_gesture_starts_promptly(self):
        self.enter_desktop()
        pointer = self.pointer
        self.axis(ecodes.ABS_RY, -9830)
        self.assertTrue(_wait_for(
            lambda: pointer.rel_total(ecodes.REL_WHEEL) > 0, deadline=0.5))
        self.axis(ecodes.ABS_RY, 0)  # back to rest
        time.sleep(0.1)
        before = pointer.rel_total(ecodes.REL_WHEEL)
        self.axis(ecodes.ABS_RY, -9830)  # push again
        self.assertTrue(
            _wait_for(lambda: pointer.rel_total(ecodes.REL_WHEEL) > before,
                      deadline=0.5),
            "the second gesture had to refill the accumulator",
        )

    def test_a_resting_stick_does_not_scroll(self):
        self.enter_desktop()
        pointer = self.pointer
        self.axis(ecodes.ABS_RY, 1000)  # inside the dead zone
        time.sleep(0.3)
        self.assertEqual(pointer.rel_total(ecodes.REL_WHEEL), 0)

    def test_a_button_clicks(self):
        self.enter_desktop()
        pointer = self.pointer
        self.key(ecodes.BTN_SOUTH, 1)
        self.key(ecodes.BTN_SOUTH, 0)
        self.assertTrue(_wait_for(
            lambda: pointer.key_events(ecodes.BTN_LEFT) == [1, 0]))

    def test_exiting_releases_a_button_still_held(self):
        self.enter_desktop()
        pointer = self.pointer
        self.key(ecodes.BTN_SOUTH, 1)  # hold left click
        self.assertTrue(_wait_for(
            lambda: pointer.key_events(ecodes.BTN_LEFT) == [1]))
        self.key(ecodes.BTN_MODE, 1)   # leave while still holding it
        self.assertTrue(
            _wait_for(lambda: pointer.key_events(ecodes.BTN_LEFT) == [1, 0]),
            "left click stayed down after leaving desktop mode",
        )

    def test_exiting_releases_the_gamepad(self):
        self.enter_desktop()
        self.key(ecodes.BTN_MODE, 1)
        self.assertTrue(_wait_for(lambda: not self.reader.grabbed))

    def test_alt_tab_holds_alt_while_the_stick_steps_through(self):
        """The whole point of the alt-tab rework, end to end."""
        self.enter_desktop()
        keyboard = self.keyboard
        config = Config()
        alt, tab = buttons.KEYS["ALT"], buttons.KEYS["TAB"]

        self.key(ecodes.BTN_THUMBR, 1)  # hold R3
        self.assertTrue(_wait_for(lambda: keyboard.key_events(alt) == [1]),
                        "alt was never pressed")

        for _ in range(2):  # two flicks of the right stick
            self.axis(ecodes.ABS_RX, 32767)
            self.axis(ecodes.ABS_RX, 0)
            # Past the settle window. Firing them back to back would be
            # correctly ignored as spring-back, which is not how a person
            # flicks a stick twice.
            time.sleep(config.desktop.stick_settle + 0.05)
        self.assertTrue(_wait_for(lambda: keyboard.key_events(tab) == [1, 0, 1, 0]),
                        "stick flicks did not tap tab twice")

        # Alt must still be down: releasing it between taps is exactly the bug
        # that made the old alt+tab binding only toggle two windows.
        self.assertEqual(keyboard.key_events(alt), [1])

        self.key(ecodes.BTN_THUMBR, 0)  # release R3 to commit the choice
        self.assertTrue(_wait_for(lambda: keyboard.key_events(alt) == [1, 0]),
                        "alt was never released")

    def test_leaving_desktop_mode_releases_a_held_modifier(self):
        self.enter_desktop()
        keyboard = self.keyboard
        alt = buttons.KEYS["ALT"]
        self.key(ecodes.BTN_THUMBR, 1)
        self.assertTrue(_wait_for(lambda: keyboard.key_events(alt) == [1]))
        self.key(ecodes.BTN_MODE, 1)  # leave while alt is still held
        self.assertTrue(_wait_for(lambda: keyboard.key_events(alt) == [1, 0]),
                        "alt stayed down after leaving desktop mode")

    def test_padmode_does_not_read_its_own_virtual_devices(self):
        from padmode.input import find_gamepad
        from padmode.input.reader import VIRTUAL_PREFIX

        self.enter_desktop()
        # The virtual pointer advertises BTN_LEFT and would otherwise be a
        # candidate; the name prefix is what keeps us from feeding our own
        # output back into our input.
        #
        # Asserted as "not ours" rather than "is the synthetic pad", because a
        # real controller plugged into the developer's machine is also a valid
        # match and would otherwise fail this test for the wrong reason.
        chosen = find_gamepad()
        self.assertIsNotNone(chosen)
        self.assertFalse(chosen.name.startswith(VIRTUAL_PREFIX), chosen.name)
        self.assertEqual(find_gamepad(DEVICE_NAME).name, DEVICE_NAME)
