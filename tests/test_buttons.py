import unittest

from padmode import buttons


class TestCanonical(unittest.TestCase):
    def test_accepts_canonical_names(self):
        self.assertEqual(buttons.canonical("RB"), "RB")
        self.assertEqual(buttons.canonical("DPAD_UP"), "DPAD_UP")

    def test_is_case_and_separator_insensitive(self):
        for spelling in ("rb", "Rb", "  rb  "):
            self.assertEqual(buttons.canonical(spelling), "RB")
        self.assertEqual(buttons.canonical("dpad-up"), "DPAD_UP")
        self.assertEqual(buttons.canonical("dpad up"), "DPAD_UP")

    def test_resolves_aliases(self):
        self.assertEqual(buttons.canonical("MODE"), "GUIDE")
        self.assertEqual(buttons.canonical("home"), "GUIDE")
        self.assertEqual(buttons.canonical("R1"), "RB")
        self.assertEqual(buttons.canonical("back"), "SELECT")
        self.assertEqual(buttons.canonical("up"), "DPAD_UP")

    def test_rejects_nonsense(self):
        self.assertIsNone(buttons.canonical("BANANA"))
        self.assertIsNone(buttons.canonical(""))
        self.assertFalse(buttons.is_known("BANANA"))


class TestResolve(unittest.TestCase):
    def test_key_press_resolves(self):
        self.assertEqual(buttons.resolve(buttons.EV_KEY, 311, 1), "RB")

    def test_key_release_and_autorepeat_do_not_resolve(self):
        self.assertIsNone(buttons.resolve(buttons.EV_KEY, 311, 0))
        self.assertIsNone(buttons.resolve(buttons.EV_KEY, 311, 2))

    def test_dpad_directions(self):
        up = buttons.resolve(buttons.EV_ABS, buttons.ABS_HAT0Y, -1)
        down = buttons.resolve(buttons.EV_ABS, buttons.ABS_HAT0Y, 1)
        left = buttons.resolve(buttons.EV_ABS, buttons.ABS_HAT0X, -1)
        right = buttons.resolve(buttons.EV_ABS, buttons.ABS_HAT0X, 1)
        self.assertEqual((up, down, left, right),
                         ("DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT"))

    def test_dpad_return_to_centre_does_not_resolve(self):
        self.assertIsNone(buttons.resolve(buttons.EV_ABS, buttons.ABS_HAT0Y, 0))

    def test_analogue_stick_is_not_a_binding(self):
        # ABS_X is a stick axis, not a d-pad hat; it must never map to a button.
        self.assertIsNone(buttons.resolve(buttons.EV_ABS, 0x00, 120))

    def test_unknown_event_type_is_ignored(self):
        self.assertIsNone(buttons.resolve(0x04, 4, 1))  # EV_MSC


class TestCodes(unittest.TestCase):
    def test_xbox_face_buttons_match_the_kernel_abi(self):
        # Regression guard for the BTN_NORTH/BTN_WEST confusion described in
        # the module docstring: a pad's X emits 307 and its Y emits 308.
        self.assertEqual(buttons.code_of("A"), 304)
        self.assertEqual(buttons.code_of("B"), 305)
        self.assertEqual(buttons.code_of("X"), 307)
        self.assertEqual(buttons.code_of("Y"), 308)
        self.assertEqual(buttons.code_of("GUIDE"), 316)

    def test_dpad_has_no_key_code(self):
        self.assertIsNone(buttons.code_of("DPAD_UP"))
