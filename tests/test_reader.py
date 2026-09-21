"""Device selection: which pad we pick when several are on offer.

These are unit tests over a faked ``/sys/class/input`` and a faked device
list, because the case that matters cannot be reproduced in CI: it needs Steam
running, publishing a virtual pad named exactly like real hardware. What was
measured on the hardware is recorded in ``reader.is_virtual``; what is
asserted here is that the rule derived from it holds.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from evdev import ecodes

from padmode.input import reader


def _sysfs(root: str, node: str, *, virtual: bool, parent: str = "1-1:1.0") -> None:
    """Publish a node the way the kernel would, with or without a parent.

    Copied from the real layouts, all three measured on the machine this was
    written on:

    * real USB pad -- ``/sys/devices/pci0000:00/.../usb1/1-1/1-1:1.0/input/
      input239``, parent ``1-1:1.0``
    * Bluetooth pad over uhid -- ``/sys/devices/virtual/misc/uhid/
      0005:1234:5678.0021/input/input321``, parent the uhid device
    * uinput pad (Steam's, and our own) -- ``/sys/devices/virtual/input/
      input243``, no parent at all
    """
    published = os.path.join(root, node, "device")
    os.makedirs(published, exist_ok=True)
    if not virtual:
        holder = os.path.join(root, "..", "devices", parent)
        os.makedirs(holder, exist_ok=True)
        os.symlink(holder, os.path.join(published, "device"))


class FakeDevice:
    """Enough of an InputDevice for selection: it never reads events."""

    def __init__(self, path, name, phys="", gamepad=True):
        self.path = path
        self.name = name
        self.phys = phys
        self._gamepad = gamepad
        self.closed = False

    def capabilities(self):
        if not self._gamepad:
            return {ecodes.EV_KEY: [ecodes.KEY_A]}
        return {
            ecodes.EV_KEY: [ecodes.BTN_SOUTH, ecodes.BTN_MODE],
            ecodes.EV_ABS: [(ecodes.ABS_X, mock.Mock(min=-32768, max=32767))],
        }

    def close(self):
        self.closed = True


class DeviceSelectionTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, True))
        patch = mock.patch.object(reader, "SYSFS_INPUT", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.devices: list[FakeDevice] = []

    def publish(self, node, name, *, virtual, phys="", gamepad=True,
                parent="1-1:1.0"):
        _sysfs(self.root, node, virtual=virtual, parent=parent)
        device = FakeDevice(f"/dev/input/{node}", name, phys, gamepad)
        self.devices.append(device)
        return device

    def find(self, name=None):
        """Run discovery, recording which nodes were actually opened."""
        by_path = {d.path: d for d in self.devices}
        self.opened: list[str] = []

        def open_device(path):
            self.opened.append(path)
            return by_path[path]

        with mock.patch.object(reader, "list_devices",
                               return_value=list(by_path)), \
             mock.patch.object(reader, "InputDevice", open_device):
            return reader.find_gamepad(name)

    # -- the measured case ---------------------------------------------
    def test_the_real_pad_wins_over_steams_virtual_one(self):
        """The reported bug, reproduced from the hardware trace.

        Steam's node is the *higher* number and sorts first as a string, so
        the old code picked it; it emits nothing, and padmode went silent.
        """
        steam = self.publish("event256", "Microsoft X-Box 360 pad 0",
                             virtual=True, phys="")
        real = self.publish("event28", "Generic X-Box pad", virtual=False,
                            phys="usb-0000:05:00.3-1/input0")

        self.assertIs(self.find(), real)
        # Stronger than "it lost": ranking is decided from sysfs, so the
        # inert pad is never even opened.
        self.assertNotIn(steam.path, self.opened)

    def test_a_real_pad_wins_from_any_node_number(self):
        """Steam's pad could as easily have taken the lower number.

        The numeric sort alone would pick the wrong device here, which is why
        the ordering fix is not the fix.
        """
        self.publish("event9", "Microsoft X-Box 360 pad 0", virtual=True)
        real = self.publish("event260", "Generic X-Box pad", virtual=False,
                            phys="usb-0000:05:00.3-1/input0")
        self.assertIs(self.find(), real)

    def test_a_virtual_pad_is_used_when_it_is_all_there_is(self):
        """Better than nothing, but it must be said out loud."""
        steam = self.publish("event256", "Microsoft X-Box 360 pad 0",
                             virtual=True)
        with self.assertLogs(reader.log, "WARNING") as caught:
            self.assertIs(self.find(), steam)
        self.assertIn("virtual device", "\n".join(caught.output))

    def test_picking_a_real_pad_warns_about_nothing(self):
        self.publish("event28", "Generic X-Box pad", virtual=False,
                     phys="usb-0000:05:00.3-1/input0")
        with mock.patch.object(reader.log, "warning") as warned:
            self.assertIsNotNone(self.find())
        warned.assert_not_called()

    # -- the signals we rejected ---------------------------------------
    def test_our_own_uinput_devices_are_not_identified_by_an_empty_phys(self):
        """python-evdev sets phys='py-evdev-uinput' by default.

        Measured on the hardware: our own virtual pointer and keyboard report
        that string, so "empty phys means virtual" would wave through every
        uinput pad built with the library we ourselves use. The parentage test
        catches it; the phys test would not.
        """
        ours = self.publish("event257", "padmode virtual pad", virtual=True,
                            phys="py-evdev-uinput")
        self.assertTrue(reader.is_virtual(ours))

    def test_a_bluetooth_pad_arriving_over_uhid_is_physical(self):
        """The case a path-prefix test would have broken.

        Measured by creating a Bluetooth-bus HID pad through ``/dev/uhid``:
        it is published under ``/sys/devices/virtual/misc/uhid/...`` -- inside
        ``/devices/virtual``, because uhid is a virtual misc device -- with
        ``phys`` set to a MAC. Only its parentage tells it apart from Steam's
        pad, so only parentage may be the test.
        """
        pad = self.publish("event259", "selftest bluetooth pad", virtual=False,
                           phys="00:1a:7d:da:71:13",
                           parent="virtual/misc/uhid/0005:1234:5678.0021")
        self.assertFalse(reader.is_virtual(pad))

    def test_a_bluetooth_pad_outranks_steams_pad(self):
        """The same ranking, for the transport we cannot plug in here."""
        self.publish("event256", "Microsoft X-Box 360 pad 0", virtual=True)
        pad = self.publish("event259", "selftest bluetooth pad", virtual=False,
                           phys="00:1a:7d:da:71:13",
                           parent="virtual/misc/uhid/0005:1234:5678.0021")
        self.assertIs(self.find(), pad)

    def test_classification_fails_open_when_sysfs_is_missing(self):
        """No sysfs, no verdict -- and a device we cannot classify is real.

        Refusing to rank anything would be worse than ranking it optimistically:
        the fallback still works, it just loses the tie-break.
        """
        device = FakeDevice("/dev/input/event0", "Generic X-Box pad")
        with mock.patch.object(reader, "SYSFS_INPUT", "/nonexistent"):
            self.assertFalse(reader.is_virtual(device))

    # -- the things that already had to keep working -------------------
    def test_our_own_devices_are_still_skipped_by_name(self):
        """The VIRTUAL_PREFIX rule is not made redundant by ranking.

        Without it, a machine whose only gamepad-shaped device is our own
        virtual pad would read back its own output.
        """
        self.publish("event257", f"{reader.VIRTUAL_PREFIX} virtual pad",
                     virtual=True, phys="py-evdev-uinput")
        self.assertIsNone(self.find())

    def test_a_keyboard_is_never_a_gamepad(self):
        self.publish("event3", "selftest keyboard", virtual=False,
                     gamepad=False)
        self.assertIsNone(self.find())

    def test_a_device_we_open_and_reject_is_closed_again(self):
        """Discovery reruns on every reconnect; leaks here are forever."""
        keyboard = self.publish("event3", "selftest keyboard", virtual=False,
                                gamepad=False)
        pad = self.publish("event28", "Generic X-Box pad", virtual=False)
        self.assertIs(self.find(), pad)
        self.assertIn(keyboard.path, self.opened)
        self.assertTrue(keyboard.closed, "a rejected device must be closed")
        self.assertFalse(pad.closed, "the chosen device must stay open")

    def test_the_name_filter_still_applies_to_a_real_pad(self):
        self.publish("event28", "Generic X-Box pad", virtual=False)
        wanted = self.publish("event29", "8BitDo Pro 2", virtual=False)
        self.assertIs(self.find("8bitdo"), wanted)
        self.assertIsNone(self.find("no such pad"))

    def test_a_named_virtual_pad_is_still_reachable(self):
        """Config asking for it by name is an explicit choice, and it wins."""
        steam = self.publish("event256", "Microsoft X-Box 360 pad 0",
                             virtual=True)
        self.publish("event28", "Generic X-Box pad", virtual=False)
        self.assertIs(self.find("microsoft"), steam)


class NodeOrderTest(unittest.TestCase):
    def test_nodes_sort_numerically_not_as_strings(self):
        """'event256' < 'event28' as a string, which is how this started."""
        paths = ["/dev/input/event256", "/dev/input/event28",
                 "/dev/input/event9", "/dev/input/event30"]
        self.assertEqual(
            sorted(paths, key=reader._node_key),
            ["/dev/input/event9", "/dev/input/event28",
             "/dev/input/event30", "/dev/input/event256"],
        )

    def test_a_path_without_a_number_still_sorts(self):
        self.assertEqual(reader._node_key("/dev/input/by-id/pad")[0], -1)


if __name__ == "__main__":
    unittest.main()
