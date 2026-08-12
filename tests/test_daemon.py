import unittest

from padmode import buttons
from padmode.config import Config
from padmode.daemon import Grab, Mode, State, decide

GUIDE = buttons.code_of("GUIDE")
RB = buttons.code_of("RB")
B = buttons.code_of("B")
KEY = buttons.EV_KEY
ABS = buttons.EV_ABS


def press(code, state, config, value=1, ev_type=KEY):
    return decide(ev_type, code, value, state, config)


class TestModifierGrab(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.state = State()

    def test_holding_modifier_grabs_the_pad(self):
        decision = press(GUIDE, self.state, self.config)
        self.assertIs(decision.grab, Grab.ACQUIRE)
        self.assertIs(self.state.mode, Mode.GUIDE_HELD)

    def test_releasing_modifier_hands_the_pad_back(self):
        press(GUIDE, self.state, self.config)
        decision = press(GUIDE, self.state, self.config, value=0)
        self.assertIs(decision.grab, Grab.RELEASE)
        self.assertIs(self.state.mode, Mode.IDLE)

    def test_repeated_press_does_not_stack_grabs(self):
        press(GUIDE, self.state, self.config)
        again = press(GUIDE, self.state, self.config)
        self.assertIsNone(again.grab)
        self.assertIs(self.state.mode, Mode.GUIDE_HELD)

    def test_release_without_press_is_ignored(self):
        # Happens when the daemon starts while the button is already down.
        decision = press(GUIDE, self.state, self.config, value=0)
        self.assertFalse(decision)
        self.assertIs(self.state.mode, Mode.IDLE)

    def test_autorepeat_is_ignored(self):
        press(GUIDE, self.state, self.config)
        decision = press(GUIDE, self.state, self.config, value=2)
        self.assertFalse(decision)
        self.assertIs(self.state.mode, Mode.GUIDE_HELD)


class TestBindings(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.state = State()

    def hold(self):
        press(GUIDE, self.state, self.config)

    def test_bound_button_fires_while_modifier_held(self):
        self.hold()
        self.assertEqual(press(RB, self.state, self.config).binding, "media.next")

    def test_bound_button_does_nothing_when_modifier_is_not_held(self):
        # The whole point: without the modifier the pad belongs to the game.
        self.assertFalse(press(RB, self.state, self.config))

    def test_button_release_does_not_fire(self):
        self.hold()
        self.assertFalse(press(RB, self.state, self.config, value=0))

    def test_unbound_button_does_nothing(self):
        self.hold()
        self.assertFalse(press(B, self.state, self.config))

    def test_dpad_up_and_down_control_volume(self):
        self.hold()
        up = press(buttons.ABS_HAT0Y, self.state, self.config, value=-1, ev_type=ABS)
        down = press(buttons.ABS_HAT0Y, self.state, self.config, value=1, ev_type=ABS)
        self.assertEqual(up.binding, "audio.volume_up")
        self.assertEqual(down.binding, "audio.volume_down")

    def test_dpad_centre_does_nothing(self):
        self.hold()
        centre = press(buttons.ABS_HAT0Y, self.state, self.config, value=0, ev_type=ABS)
        self.assertFalse(centre)

    def test_binding_can_be_a_raw_command(self):
        config = Config(bindings={"RB": ["notify-send", "hi"]})
        state = State()
        press(GUIDE, state, config)
        self.assertEqual(press(RB, state, config).binding, ["notify-send", "hi"])

    def test_modifier_is_configurable(self):
        config = Config(modifier="START", bindings={"RB": "media.next"})
        state = State()
        # GUIDE is now just another button, and it is unbound.
        self.assertFalse(press(GUIDE, state, config))
        self.assertIs(press(buttons.code_of("START"), state, config).grab, Grab.ACQUIRE)
        self.assertEqual(press(RB, state, config).binding, "media.next")


class TestSequences(unittest.TestCase):
    def test_full_hold_press_release_cycle(self):
        config, state = Config(), State()
        events = [
            (KEY, GUIDE, 1),
            (KEY, RB, 1),
            (KEY, RB, 0),
            (KEY, GUIDE, 0),
            (KEY, RB, 1),  # after release: belongs to the game again
        ]
        results = [decide(t, c, v, state, config) for t, c, v in events]
        self.assertIs(results[0].grab, Grab.ACQUIRE)
        self.assertEqual(results[1].binding, "media.next")
        self.assertFalse(results[2])
        self.assertIs(results[3].grab, Grab.RELEASE)
        self.assertFalse(results[4])
        self.assertIs(state.mode, Mode.IDLE)

    def test_decide_never_returns_both_a_grab_and_a_binding(self):
        # The daemon applies grab before binding; if one event ever produced
        # both, the ordering would become load-bearing and subtle.
        config, state = Config(), State()
        for ev_type, code, value in [
            (KEY, GUIDE, 1), (KEY, RB, 1), (ABS, buttons.ABS_HAT0Y, -1), (KEY, GUIDE, 0)
        ]:
            decision = decide(ev_type, code, value, state, config)
            self.assertFalse(decision.grab is not None and decision.binding is not None)
