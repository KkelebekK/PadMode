"""Analogue triggers standing in for LT and RT.

Most pads report their triggers as ``ABS_Z``/``ABS_RZ`` and never emit
``BTN_TL2``/``BTN_TR2``, which made ``LT`` and ``RT`` silently unbindable on
nearly every real controller. They are translated into button events before
anything else looks at them, so the interesting cases are the boundaries: when
a squeeze becomes a press, when it stops being one, and when a pad should be
left alone because its triggers are already buttons.
"""

from __future__ import annotations

import unittest

from padmode import buttons
from padmode.config import Config, ConfigError, parse
from padmode.daemon import Grab, Mode, State, decide
from padmode.input.reader import _trigger_axes

ABS = buttons.EV_ABS
KEY = buttons.EV_KEY
LT = buttons.BUTTONS["LT"]
RT = buttons.BUTTONS["RT"]
FULL = 255


def armed(config=None) -> State:
    """A state whose device reports both triggers as axes, 0..255."""
    state = State()
    state.trigger_axes = {buttons.ABS_Z: LT, buttons.ABS_RZ: RT}
    state.axis_ranges = {buttons.ABS_Z: (0, FULL), buttons.ABS_RZ: (0, FULL)}
    return state


class FakeDevice:
    def __init__(self, keys, axes):
        self._caps = {
            buttons.EV_KEY: list(keys),
            buttons.EV_ABS: [(code, None) for code in axes],
        }

    def capabilities(self):
        return self._caps


class TestNormalising(unittest.TestCase):
    def test_a_trigger_rests_at_zero_not_at_minus_one(self):
        # The whole reason this is not normalize_axis: a trigger rests at its
        # minimum, and centring it would read "fully pressed the other way".
        self.assertEqual(buttons.normalize_trigger(0, 0, FULL), 0.0)
        self.assertEqual(buttons.normalize_trigger(FULL, 0, FULL), 1.0)
        self.assertAlmostEqual(buttons.normalize_trigger(128, 0, FULL), 0.502, places=2)

    def test_out_of_range_readings_are_clamped(self):
        self.assertEqual(buttons.normalize_trigger(-40, 0, FULL), 0.0)
        self.assertEqual(buttons.normalize_trigger(9999, 0, FULL), 1.0)

    def test_a_degenerate_range_does_not_divide_by_zero(self):
        self.assertEqual(buttons.normalize_trigger(5, 7, 7), 0.0)

    def test_a_range_that_does_not_start_at_zero(self):
        self.assertEqual(buttons.normalize_trigger(-32768, -32768, 32767), 0.0)
        self.assertEqual(buttons.normalize_trigger(32767, -32768, 32767), 1.0)


class TestWhichPadsNeedTranslating(unittest.TestCase):
    def test_axes_only_pad_gets_both_triggers_translated(self):
        device = FakeDevice(keys=[buttons.BUTTONS["A"]],
                            axes=[buttons.ABS_Z, buttons.ABS_RZ])
        self.assertEqual(_trigger_axes(device), {buttons.ABS_Z: LT, buttons.ABS_RZ: RT})

    def test_a_pad_with_digital_triggers_is_left_alone(self):
        # Translating one that already reports buttons would fire twice.
        device = FakeDevice(keys=[LT, RT], axes=[buttons.ABS_Z, buttons.ABS_RZ])
        self.assertEqual(_trigger_axes(device), {})

    def test_each_trigger_is_decided_separately(self):
        device = FakeDevice(keys=[LT], axes=[buttons.ABS_Z, buttons.ABS_RZ])
        self.assertEqual(_trigger_axes(device), {buttons.ABS_RZ: RT})

    def test_a_pad_without_trigger_axes_gets_nothing(self):
        device = FakeDevice(keys=[buttons.BUTTONS["A"]], axes=[buttons.ABS_X])
        self.assertEqual(_trigger_axes(device), {})


class TestSqueezing(unittest.TestCase):
    """A trigger crossing the threshold has to behave like a button press."""

    def setUp(self):
        self.config = parse({"general": {"modifier": "LT"}})

    def test_a_full_squeeze_presses_the_button(self):
        state = armed()
        decision = decide(ABS, buttons.ABS_Z, FULL, state, self.config)
        self.assertIs(decision.grab, Grab.ACQUIRE)
        self.assertIs(state.mode, Mode.GUIDE_HELD)

    def test_letting_go_releases_it(self):
        state = armed()
        decide(ABS, buttons.ABS_Z, FULL, state, self.config)
        decision = decide(ABS, buttons.ABS_Z, 0, state, self.config)
        self.assertIs(decision.grab, Grab.RELEASE)
        self.assertIs(state.mode, Mode.IDLE)

    def test_a_light_touch_does_nothing(self):
        state = armed()
        self.assertFalse(decide(ABS, buttons.ABS_Z, 40, state, self.config))
        self.assertIs(state.mode, Mode.IDLE)

    def test_the_readings_on_the_way_down_do_not_repeat_the_press(self):
        # A squeeze produces a stream of values; only the crossing counts.
        state = armed()
        self.assertTrue(decide(ABS, buttons.ABS_Z, 200, state, self.config))
        for value in (210, 230, 255, 240):
            with self.subTest(value=value):
                self.assertFalse(decide(ABS, buttons.ABS_Z, value, state, self.config))

    def test_hovering_at_the_threshold_does_not_chatter(self):
        # Releasing lower than it presses is the point: a finger resting on the
        # boundary would otherwise emit an endless press/release stream.
        state = armed()
        threshold = int(FULL * self.config.trigger_threshold) + 2
        self.assertTrue(decide(ABS, buttons.ABS_Z, threshold, state, self.config))
        for value in (threshold - 1, threshold + 1, threshold - 2):
            with self.subTest(value=value):
                self.assertFalse(decide(ABS, buttons.ABS_Z, value, state, self.config))

    def test_it_releases_once_it_falls_clearly_below(self):
        state = armed()
        decide(ABS, buttons.ABS_Z, FULL, state, self.config)
        decision = decide(ABS, buttons.ABS_Z, int(FULL * 0.3), state, self.config)
        self.assertIs(decision.grab, Grab.RELEASE)

    def test_the_two_triggers_are_independent(self):
        config = parse({"general": {"modifier": "GUIDE"},
                        "bindings": {"RT": "audio.toggle_mute"}})
        state = armed()
        decide(KEY, buttons.code_of("GUIDE"), 1, state, config)
        decision = decide(ABS, buttons.ABS_RZ, FULL, state, config)
        self.assertEqual(decision.binding, "audio.toggle_mute")

    def test_a_pad_with_digital_triggers_ignores_the_axis(self):
        state = State()  # no trigger_axes: nothing to translate
        state.axis_ranges = {buttons.ABS_Z: (0, FULL)}
        self.assertFalse(decide(ABS, buttons.ABS_Z, FULL, state, self.config))

    def test_a_held_trigger_survives_a_mode_change(self):
        # reset_inputs deliberately leaves this alone: it mirrors where the
        # finger is, and clearing it mid-hold would swallow the release.
        state = armed()
        decide(ABS, buttons.ABS_Z, FULL, state, self.config)
        state.reset_inputs()
        self.assertTrue(state.triggers[buttons.ABS_Z])


class TestTriggerAsDesktopEnter(unittest.TestCase):
    def test_the_original_request_works(self):
        # "modifier = SELECT, enter = L2" -- the combination that started this,
        # and which silently did nothing before triggers were translated.
        config = parse({
            "general": {"modifier": "SELECT"},
            "desktop": {"enter": "LT", "exit": "SELECT",
                        "bindings": {"SELECT": False}},
            "bindings": {"SELECT": False},
        })
        state = armed()
        decide(KEY, buttons.code_of("SELECT"), 1, state, config)
        decision = decide(ABS, buttons.ABS_Z, FULL, state, config)
        self.assertIs(decision.mode, Mode.DESKTOP)


class TestConfig(unittest.TestCase):
    def test_the_default(self):
        self.assertEqual(Config().trigger_threshold, 0.5)

    def test_setting_it(self):
        self.assertEqual(
            parse({"general": {"trigger_threshold": 0.25}}).trigger_threshold, 0.25
        )

    def test_a_threshold_that_could_never_release_is_refused(self):
        for value in (0.0, 0.99, 2, -1):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse({"general": {"trigger_threshold": value}})

    def test_it_must_be_a_number(self):
        for value in ("half", True, []):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse({"general": {"trigger_threshold": value}})


if __name__ == "__main__":
    unittest.main()
