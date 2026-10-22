"""Desktop mode: transitions, virtual output, and the pointer maths."""

import math
import unittest

from padmode import buttons
from padmode.config import Config, ConfigError, DesktopConfig, parse
from padmode.daemon import (
    Grab,
    Mode,
    State,
    decide,
    pending_release_ops,
    pointer_delta,
)

GUIDE = buttons.code_of("GUIDE")
L3 = buttons.code_of("L3")
A = buttons.code_of("A")
B = buttons.code_of("B")
R3 = buttons.code_of("R3")
RB = buttons.code_of("RB")
KEY = buttons.EV_KEY
ABS = buttons.EV_ABS


def enter_desktop(state, config):
    """Hold the modifier and press the desktop key."""
    decide(KEY, GUIDE, 1, state, config)
    return decide(KEY, L3, 1, state, config)


def one(decision):
    """The single virtual op a decision carries."""
    assert len(decision.virtual) == 1, f"expected 1 op, got {decision.virtual}"
    return decision.virtual[0]


def one_of(ops):
    """The single op in a tuple of ops."""
    assert len(ops) == 1, f"expected 1 op, got {ops}"
    return ops[0]


class TestEnteringAndLeaving(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.state = State()

    def test_modifier_plus_enter_key_starts_desktop_mode(self):
        decision = enter_desktop(self.state, self.config)
        self.assertIs(decision.mode, Mode.DESKTOP)
        self.assertIs(self.state.mode, Mode.DESKTOP)

    def test_entering_does_not_change_the_grab(self):
        # We are already grabbed from the modifier press; re-grabbing would be
        # a no-op at best and a dropped grab at worst.
        self.assertIsNone(enter_desktop(self.state, self.config).grab)

    def test_exit_button_leaves_and_releases_the_pad(self):
        enter_desktop(self.state, self.config)
        # The modifier is still physically held; its release must not exit.
        self.assertFalse(decide(KEY, GUIDE, 0, self.state, self.config))
        self.assertIs(self.state.mode, Mode.DESKTOP)
        # A fresh press of the exit button does.
        decision = decide(KEY, GUIDE, 1, self.state, self.config)
        self.assertIs(decision.mode, Mode.IDLE)
        self.assertIs(decision.grab, Grab.RELEASE)
        self.assertIs(self.state.mode, Mode.IDLE)

    def test_leaving_forgets_stick_state(self):
        enter_desktop(self.state, self.config)
        decide(ABS, buttons.ABS_X, 32767, self.state, self.config)
        self.assertTrue(self.state.sticks)
        decide(KEY, GUIDE, 1, self.state, self.config)
        self.assertEqual(self.state.sticks, {})

    def test_desktop_mode_can_be_disabled(self):
        config = Config(desktop=DesktopConfig(enabled=False))
        state = State()
        decide(KEY, GUIDE, 1, state, config)
        self.assertFalse(decide(KEY, L3, 1, state, config))
        self.assertIs(state.mode, Mode.GUIDE_HELD)

    def test_tier_one_bindings_still_work_before_entering(self):
        decide(KEY, GUIDE, 1, self.state, self.config)
        self.assertEqual(decide(KEY, RB, 1, self.state, self.config).binding,
                         "media.next")


class TestVirtualOutput(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.state = State()
        enter_desktop(self.state, self.config)

    def test_a_is_left_click_and_follows_press_and_release(self):
        pressed = one(decide(KEY, A, 1, self.state, self.config, 1.0))
        self.assertEqual((pressed.kind, pressed.codes, pressed.pressed),
                         ("click", (buttons.CLICKS["LEFT"],), True))
        # The release is held back briefly in case it is contact bounce.
        self.assertFalse(decide(KEY, A, 0, self.state, self.config, 1.0))
        released = one_of(pending_release_ops(self.state, 2.0))
        self.assertEqual((released.codes, released.pressed),
                         ((buttons.CLICKS["LEFT"],), False))

    def test_right_click(self):
        op = one(decide(KEY, B, 1, self.state, self.config))
        self.assertEqual(op.codes, (buttons.CLICKS["RIGHT"],))

    def test_r3_holds_alt_rather_than_tapping_alt_tab(self):
        # Tapping the whole combination can only toggle between the last two
        # windows; holding alt is what keeps the switcher open to choose from.
        pressed = one(decide(KEY, R3, 1, self.state, self.config, 1.0))
        self.assertEqual((pressed.kind, pressed.codes, pressed.pressed),
                         ("key", (buttons.KEYS["ALT"],), True))
        self.assertFalse(decide(KEY, R3, 0, self.state, self.config, 1.0))
        released = one_of(pending_release_ops(self.state, 2.0))
        self.assertEqual((released.codes, released.pressed),
                         ((buttons.KEYS["ALT"],), False))

    def test_a_combination_binding_taps_once_on_press_only(self):
        config = Config(desktop=DesktopConfig(bindings={"Y": "key.ctrl+w"}))
        state = State()
        enter_desktop(state, config)
        pressed = one(decide(KEY, buttons.code_of("Y"), 1, state, config))
        self.assertEqual(pressed.kind, "tap")
        self.assertEqual(pressed.codes, (buttons.KEYS["CTRL"], buttons.KEYS["W"]))
        self.assertFalse(decide(KEY, buttons.code_of("Y"), 0, state, config))

    def test_dpad_holds_the_key_until_released(self):
        down = one(decide(ABS, buttons.ABS_HAT0Y, -1, self.state, self.config))
        self.assertEqual((down.kind, down.codes, down.pressed),
                         ("key", (buttons.KEYS["UP"],), True))
        up = one(decide(ABS, buttons.ABS_HAT0Y, 0, self.state, self.config))
        self.assertEqual((up.codes, up.pressed), ((buttons.KEYS["UP"],), False))

    def test_dpad_release_without_press_is_ignored(self):
        self.assertFalse(decide(ABS, buttons.ABS_HAT0Y, 0, self.state, self.config))

    def test_unbound_button_produces_nothing(self):
        config = Config(desktop=DesktopConfig(bindings={}))
        state = State()
        enter_desktop(state, config)
        self.assertFalse(decide(KEY, A, 1, state, config))

    def test_sticks_only_update_state(self):
        decision = decide(ABS, buttons.ABS_X, 32767, self.state, self.config)
        self.assertFalse(decision)
        self.assertAlmostEqual(self.state.sticks[buttons.ABS_X], 1.0, places=2)


class TestStickDirections(unittest.TestCase):
    """Alt-tab: hold R3 for alt, flick the right stick to step through."""

    def setUp(self):
        self.config = Config()
        self.state = State()
        self.now = 100.0
        enter_desktop(self.state, self.config)

    def flick(self, fraction, after=1.0):
        """Move the right stick horizontally, as a fraction of full travel.

        ``after`` advances the fake clock first. It defaults to a full second,
        well past the settle window, so ordinary flicks are independent; pass
        something small to simulate spring-back happening milliseconds later.
        """
        self.now += after
        raw = int(fraction * 32767)
        return decide(ABS, buttons.ABS_RX, raw, self.state, self.config, self.now)

    def test_pushing_right_taps_tab_once(self):
        # Press and release together: the key is never left held, so the
        # compositor's autorepeat can't step the switcher a second time.
        ops = self.flick(1.0).virtual
        self.assertEqual(len(ops), 2)
        self.assertEqual((ops[0].codes, ops[0].pressed), ((buttons.KEYS["TAB"],), True))
        self.assertEqual((ops[1].codes, ops[1].pressed), ((buttons.KEYS["TAB"],), False))

    def test_holding_the_stick_does_not_step_again(self):
        self.flick(1.0)
        self.assertFalse(self.flick(0.9))
        self.assertFalse(self.flick(0.8))
        self.assertFalse(self.flick(1.0))

    def test_returning_to_centre_emits_nothing_but_rearms(self):
        self.flick(1.0)
        self.assertFalse(self.flick(0.0), "recentring should not emit anything")
        self.assertTrue(self.flick(1.0), "a second flick should step again")

    def test_below_threshold_does_nothing(self):
        self.assertFalse(self.flick(0.4))  # default threshold is 0.6

    def test_spring_back_wobble_does_not_step_twice(self):
        # One flick, two windows: the stick dips under the engage point and
        # rebounds while still ringing down.
        self.flick(1.0)
        wobble = []
        for value in (0.55, 0.30, 0.48, 0.25, 0.40):
            wobble += list(self.flick(value, after=0.02).virtual)
        self.assertEqual(wobble, [], "spring-back wobble fired a second step")

    def test_overshoot_past_centre_does_not_fire_the_opposite_direction(self):
        # The reported bug: flick left, and it steps left then back right.
        # Released from full deflection the stick swings *past* centre and
        # crosses the opposite threshold, which no threshold value can filter
        # out because the overshoot is bigger than the trigger.
        opening = self.flick(-1.0)
        self.assertTrue(opening, "the deliberate flick should step")
        rebound = []
        for value in (-0.4, 0.3, 0.85, 0.5, 0.1):  # swings well past +0.6
            rebound += list(self.flick(value, after=0.02).virtual)
        self.assertEqual(rebound, [], "spring-back fired the opposite direction")

    def test_a_second_flick_needs_a_trip_through_centre(self):
        self.flick(-1.0)
        self.assertFalse(self.flick(1.0, after=0.02),   # still ringing
                         "rebound fired the opposite direction")
        self.flick(0.0, after=0.2)                      # settled at centre
        self.assertTrue(self.flick(1.0, after=0.05),
                        "a deliberate second flick must work")

    def test_settle_can_be_disabled(self):
        config = Config(desktop=DesktopConfig(stick_settle=0.0))
        state = State()
        enter_desktop(state, config)
        decide(ABS, buttons.ABS_RX, 32767, state, config, 1.0)
        decide(ABS, buttons.ABS_RX, 0, state, config, 1.01)
        self.assertTrue(decide(ABS, buttons.ABS_RX, 32767, state, config, 1.02))

    def test_rearming_requires_coming_back_near_centre(self):
        self.flick(1.0)
        self.assertFalse(self.flick(0.30))  # above the re-arm point: still held
        self.assertFalse(self.flick(0.05))  # now disarmed, but emits nothing
        self.assertTrue(self.flick(1.0))    # and a real flick works again

    def test_pushing_left_is_shift_tab_and_taps_once(self):
        op = one(self.flick(-1.0))
        self.assertEqual(op.kind, "tap")
        self.assertEqual(op.codes, (buttons.KEYS["SHIFT"], buttons.KEYS["TAB"]))
        self.assertFalse(self.flick(0.0))

    def test_snapping_extreme_to_extreme_without_centring_does_not_fire(self):
        # Deliberately conservative: a jump from full right to full left with
        # no reading near centre is indistinguishable from a violent rebound,
        # so it is ignored. In the hand this costs nothing -- a stick swept
        # across reports plenty of intermediate positions on the way.
        self.flick(1.0)
        self.assertFalse(self.flick(-1.0))

    def test_sweeping_across_through_centre_does_fire(self):
        self.flick(1.0)
        self.flick(0.0)                       # the sweep passes through centre
        op = one(self.flick(-1.0))
        self.assertEqual(op.kind, "tap")
        self.assertEqual(op.codes, (buttons.KEYS["SHIFT"], buttons.KEYS["TAB"]))

    def test_a_bound_axis_stops_scrolling(self):
        # RS_LEFT/RIGHT are bound by default, so horizontal scroll is off...
        self.state.sticks[buttons.ABS_RX] = 1.0
        _, _, scroll_x, _ = pointer_delta(self.state, self.config, 1.0)
        self.assertEqual(scroll_x, 0.0)
        # ...while a clear vertical push still scrolls.
        self.state.sticks[buttons.ABS_RX] = 0.0
        self.state.sticks[buttons.ABS_RY] = -1.0
        *_, scroll_y = pointer_delta(self.state, self.config, 1.0)
        self.assertGreater(scroll_y, 0)

    def test_unbound_directions_leave_the_axis_analogue(self):
        config = Config(desktop=DesktopConfig(bindings={}))
        state = State()
        state.sticks[buttons.ABS_RX] = 1.0
        _, _, scroll_x, _ = pointer_delta(state, config, 1.0)
        self.assertGreater(scroll_x, 0)

    def test_leaving_desktop_mode_forgets_disarmed_axes(self):
        self.flick(1.0)
        self.assertTrue(self.state.axis_disarmed)
        decide(KEY, GUIDE, 1, self.state, self.config)
        self.assertEqual(self.state.axis_disarmed, set())

    def test_a_long_flick_that_rings_past_the_settle_window_fires_once(self):
        """The reported symptom: long pushes skip more often than short ones.

        A harder push rings longer, so a fixed settle window expires while the
        stick is still well outside the threshold. Re-arming has to depend on
        the stick actually reaching centre, not on the clock.
        """
        self.assertTrue(self.flick(1.0), "the deliberate flick should step")
        ring = []
        # Still swinging hard 300ms later, long after a 150ms window closes.
        for value in (0.2, -0.9, 0.75, -0.6, 0.65, -0.3, 0.25):
            ring += list(self.flick(value, after=0.05).virtual)
        self.assertEqual(ring, [], "a long ring-down fired extra steps")

    def test_rearms_once_the_stick_actually_reaches_centre(self):
        self.flick(1.0)
        for value in (-0.9, 0.75, -0.6):
            self.assertFalse(self.flick(value, after=0.05))
        self.flick(0.0, after=0.05)                # settled at last
        self.assertTrue(self.flick(1.0, after=0.3), "never re-armed")

    def test_a_held_stick_never_repeats(self):
        self.assertTrue(self.flick(1.0))
        for _ in range(5):
            self.assertFalse(self.flick(1.0, after=0.5))

    def test_tick_rearms_a_stick_resting_at_centre(self):
        # A stick at rest emits no events, so without the tick the axis would
        # stay disarmed for ever and the direction would stop working.
        from padmode.daemon import stick_tick_ops

        self.flick(1.0)
        self.state.sticks[buttons.ABS_RX] = 0.0
        self.assertEqual(
            stick_tick_ops(self.state, self.config.desktop, self.now + 1.0), ())
        self.assertNotIn(buttons.ABS_RX, self.state.axis_disarmed)
        self.assertTrue(self.flick(1.0, after=0.5))

    def test_stick_directions_are_rejected_in_tier_one_bindings(self):
        with self.assertRaises(ConfigError) as ctx:
            parse({"bindings": {"RS_RIGHT": "media.next"}})
        self.assertIn("desktop.bindings", str(ctx.exception))

    def test_direction_names_accept_spelled_out_forms(self):
        self.assertEqual(buttons.canonical("right_stick_right"), "RS_RIGHT")
        self.assertEqual(buttons.canonical("rstick_up"), "RS_UP")


class TestDiagonalContamination(unittest.TestCase):
    """A flick sideways must not also scroll.

    Measured on real hardware: one horizontal flick of the right stick emitted
    a Tab *and* two wheel clicks, because no thumb moves a stick in a straight
    line and the vertical axis was still driving the scroll wheel. In a window
    switcher the wheel steps the selection, so the flick appeared to skip.
    """

    def setUp(self):
        self.config = Config()
        self.state = State()
        self.state.mode = Mode.DESKTOP

    def place(self, x, y):
        self.state.sticks[buttons.ABS_RX] = x
        self.state.sticks[buttons.ABS_RY] = y

    def test_horizontal_flick_does_not_scroll(self):
        # Hard right with the amount of vertical wobble a real thumb produces.
        self.place(0.95, 0.30)
        _, _, _, scroll_y = pointer_delta(self.state, self.config, 1.0)
        self.assertEqual(scroll_y, 0.0, "a sideways flick scrolled")

    def test_vertical_push_still_scrolls(self):
        # The whole stick is not sacrificed: pushing mostly up still scrolls.
        self.place(0.20, -0.90)
        _, _, _, scroll_y = pointer_delta(self.state, self.config, 1.0)
        self.assertGreater(scroll_y, 0)

    def test_vertical_push_does_not_step(self):
        self.state.sticks[buttons.ABS_RY] = -0.95
        ops = decide(ABS, buttons.ABS_RX, int(0.65 * 32767), self.state, self.config, 1.0)
        self.assertFalse(ops, "a mostly-vertical push fired a horizontal step")

    def test_horizontal_flick_still_steps(self):
        self.state.sticks[buttons.ABS_RY] = 0.30
        ops = decide(ABS, buttons.ABS_RX, int(0.95 * 32767), self.state, self.config, 1.0)
        self.assertTrue(ops, "the flick did not step")

    def test_a_sloppy_flick_still_steps(self):
        # 20 degrees off horizontal: well within what a thumb produces.
        self.state.sticks[buttons.ABS_RY] = 0.35
        self.assertTrue(
            decide(ABS, buttons.ABS_RX, int(0.95 * 32767), self.state, self.config, 1.0),
            "a slightly-off flick stopped working",
        )

    def test_an_ambiguous_diagonal_does_nothing(self):
        # Neither function is clearly intended, so neither fires. Doing nothing
        # beats confidently scrolling when the user meant to step.
        self.place(0.70, 0.70)
        _, _, scroll_x, scroll_y = pointer_delta(self.state, self.config, 1.0)
        self.assertEqual((scroll_x, scroll_y), (0.0, -0.0))

    def test_an_unclaimed_stick_is_unaffected(self):
        # Nothing is bound on the left stick, so both axes stay analogue.
        self.state.sticks[buttons.ABS_X] = 0.95
        self.state.sticks[buttons.ABS_Y] = 0.30
        dx, dy, _, _ = pointer_delta(self.state, self.config, 1.0)
        self.assertGreater(dx, 0)
        self.assertGreater(dy, 0, "diagonal pointer movement was suppressed")


class TestButtonBounce(unittest.TestCase):
    """A stick click bounces when the stick is flicked.

    Measured: holding R3 while flicking produced a release and a re-press 42ms
    apart. For a held modifier that closes and reopens the window switcher.
    """

    def setUp(self):
        self.config = Config()
        self.state = State()
        enter_desktop(self.state, self.config)
        decide(KEY, R3, 1, self.state, self.config, 1.0)  # hold R3 -> alt down

    def test_a_bounce_is_absorbed(self):
        self.assertFalse(decide(KEY, R3, 0, self.state, self.config, 1.00))
        self.assertFalse(decide(KEY, R3, 1, self.state, self.config, 1.04))
        # Nothing was emitted either way: alt stayed down throughout.
        self.assertEqual(pending_release_ops(self.state, 2.0), ())

    def test_a_real_release_still_happens(self):
        self.assertFalse(decide(KEY, R3, 0, self.state, self.config, 1.0))
        self.assertEqual(pending_release_ops(self.state, 1.05), (),
                         "released before the debounce elapsed")
        released = one_of(pending_release_ops(self.state, 1.20))
        self.assertEqual((released.codes, released.pressed),
                         ((buttons.KEYS["ALT"],), False))

    def test_release_is_only_emitted_once(self):
        decide(KEY, R3, 0, self.state, self.config, 1.0)
        self.assertTrue(pending_release_ops(self.state, 2.0))
        self.assertEqual(pending_release_ops(self.state, 3.0), ())

    def test_debounce_can_be_disabled(self):
        config = Config(desktop=DesktopConfig(button_debounce=0.0))
        state = State()
        enter_desktop(state, config)
        decide(KEY, R3, 1, state, config, 1.0)
        released = one(decide(KEY, R3, 0, state, config, 1.0))
        self.assertFalse(released.pressed)

    def test_leaving_desktop_mode_drops_pending_releases(self):
        decide(KEY, R3, 0, self.state, self.config, 1.0)
        self.assertTrue(self.state.pending_release)
        decide(KEY, GUIDE, 1, self.state, self.config, 1.0)
        self.assertEqual(self.state.pending_release, {})


class TestPointerMaths(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.state = State()
        self.state.mode = Mode.DESKTOP

    def push(self, axis, value):
        self.state.sticks[axis] = value

    def test_centred_sticks_produce_no_movement(self):
        self.assertEqual(pointer_delta(self.state, self.config, 1.0), (0, 0, 0, 0))

    def test_dead_zone_absorbs_drift(self):
        self.push(buttons.ABS_X, 0.1)  # default dead zone is 0.15
        dx, *_ = pointer_delta(self.state, self.config, 1.0)
        self.assertEqual(dx, 0.0)

    def test_full_deflection_moves_at_the_configured_speed(self):
        self.push(buttons.ABS_X, 1.0)
        dx, *_ = pointer_delta(self.state, self.config, 1.0)
        self.assertAlmostEqual(dx, self.config.desktop.pointer_speed)

    def test_movement_scales_with_elapsed_time(self):
        self.push(buttons.ABS_X, 1.0)
        full = pointer_delta(self.state, self.config, 1.0)[0]
        half = pointer_delta(self.state, self.config, 0.5)[0]
        self.assertAlmostEqual(half, full / 2)

    def test_direction_is_preserved(self):
        self.push(buttons.ABS_X, -1.0)
        self.push(buttons.ABS_Y, -1.0)
        dx, dy, _, _ = pointer_delta(self.state, self.config, 1.0)
        self.assertLess(dx, 0)
        self.assertLess(dy, 0)  # stick up is negative, and so is pointer up

    def test_response_starts_from_zero_outside_the_dead_zone(self):
        # Just past the threshold the pointer should crawl, not jump.
        self.push(buttons.ABS_X, self.config.desktop.dead_zone + 0.001)
        dx, *_ = pointer_delta(self.state, self.config, 1.0)
        self.assertLess(abs(dx), 1.0)

    def test_curve_makes_slow_movement_finer_than_linear(self):
        self.push(buttons.ABS_X, 0.5)
        curved = pointer_delta(self.state, self.config, 1.0)[0]
        linear_cfg = Config(desktop=DesktopConfig(curve=1.0))
        linear = pointer_delta(self.state, linear_cfg, 1.0)[0]
        self.assertLess(curved, linear)

    def test_right_stick_scrolls_and_up_is_positive(self):
        self.push(buttons.ABS_RY, -1.0)  # stick pushed up
        *_, scroll_y = pointer_delta(self.state, self.config, 1.0)
        self.assertGreater(scroll_y, 0)  # positive REL_WHEEL scrolls up

    def test_scroll_uses_its_own_speed(self):
        self.push(buttons.ABS_RY, -1.0)
        *_, scroll_y = pointer_delta(self.state, self.config, 1.0)
        self.assertAlmostEqual(scroll_y, self.config.desktop.scroll_speed)


class TestRadialResponse(unittest.TestCase):
    """The dead zone and curve apply to the stick vector, not to each axis.

    A per-axis dead zone carves a plus-shaped hole out of the stick's range and
    snaps anything near a cardinal direction onto it, which feels
    eight-directional in the hand. These are the tests that pin that down.
    """

    def setUp(self):
        self.config = Config()
        self.state = State()
        self.state.mode = Mode.DESKTOP

    def move(self, x, y):
        self.state.sticks[buttons.ABS_X] = x
        self.state.sticks[buttons.ABS_Y] = y
        dx, dy, _, _ = pointer_delta(self.state, self.config, 1.0)
        return dx, dy

    def speed(self, x, y):
        dx, dy = self.move(x, y)
        return math.hypot(dx, dy)

    def test_diagonal_is_as_fast_as_cardinal(self):
        # With a per-axis curve the diagonal came out ~40% slower.
        cardinal = self.speed(1.0, 0.0)
        diagonal = self.speed(0.7071, 0.7071)
        self.assertAlmostEqual(diagonal, cardinal, delta=cardinal * 0.02)

    def test_speed_is_uniform_all_the_way_round(self):
        speeds = [
            self.speed(math.cos(math.radians(a)), math.sin(math.radians(a)))
            for a in range(0, 360, 15)
        ]
        self.assertAlmostEqual(min(speeds), max(speeds), delta=max(speeds) * 0.02)

    def test_a_small_component_is_not_snapped_away(self):
        # Nearly-horizontal: the slight vertical component must survive, or
        # the pointer locks to the axis.
        dx, dy = self.move(0.95, 0.10)
        self.assertGreater(dx, 0)
        self.assertGreater(dy, 0, "small vertical component was zeroed")

    def test_direction_is_preserved_exactly(self):
        dx, dy = self.move(0.6, 0.3)
        self.assertAlmostEqual(dy / dx, 0.5, places=6)

    def test_dead_zone_is_radial(self):
        # Each axis alone sits inside the dead zone, but the vector does not.
        dx, dy = self.move(0.12, 0.12)
        self.assertGreater(math.hypot(dx, dy), 0.0)

    def test_inside_the_radius_nothing_moves(self):
        self.assertEqual(self.move(0.10, 0.05), (0.0, 0.0))

    def test_square_gated_corner_is_clamped_but_keeps_direction(self):
        # Some pads report past 1.0 diagonally; speed clamps, angle does not.
        dx, dy = self.move(1.0, 1.0)
        self.assertAlmostEqual(math.hypot(dx, dy),
                               self.config.desktop.pointer_speed,
                               delta=self.config.desktop.pointer_speed * 0.02)
        self.assertAlmostEqual(dx, dy, places=6)


class TestAxisNormalisation(unittest.TestCase):
    def test_signed_16_bit_range(self):
        self.assertAlmostEqual(buttons.normalize_axis(32767, -32768, 32767), 1.0, places=3)
        self.assertAlmostEqual(buttons.normalize_axis(-32768, -32768, 32767), -1.0, places=3)
        self.assertAlmostEqual(buttons.normalize_axis(0, -32768, 32767), 0.0, places=3)

    def test_asymmetric_unsigned_range_centres_correctly(self):
        # Some pads report 0..255 with 128 at rest.
        self.assertAlmostEqual(buttons.normalize_axis(128, 0, 255), 0.0, places=1)
        self.assertAlmostEqual(buttons.normalize_axis(255, 0, 255), 1.0, places=2)

    def test_values_are_clamped(self):
        self.assertEqual(buttons.normalize_axis(99999, -32768, 32767), 1.0)

    def test_degenerate_range_is_safe(self):
        self.assertEqual(buttons.normalize_axis(5, 10, 10), 0.0)

    def test_uses_device_ranges_from_state(self):
        config, state = Config(), State()
        enter_desktop(state, config)
        state.axis_ranges = {buttons.ABS_X: (0, 255)}
        decide(ABS, buttons.ABS_X, 255, state, config)
        self.assertAlmostEqual(state.sticks[buttons.ABS_X], 1.0, places=2)


class TestKeySpecParsing(unittest.TestCase):
    def test_single_key(self):
        self.assertEqual(buttons.parse_keys("up"), [buttons.KEYS["UP"]])

    def test_combination_order_is_modifiers_first(self):
        self.assertEqual(buttons.parse_keys("alt+tab"),
                         [buttons.KEYS["ALT"], buttons.KEYS["TAB"]])

    def test_case_and_spacing_are_forgiving(self):
        self.assertEqual(buttons.parse_keys(" Ctrl + W "),
                         [buttons.KEYS["CTRL"], buttons.KEYS["W"]])

    def test_letters_use_kernel_qwerty_order(self):
        self.assertEqual(buttons.KEYS["Q"], 16)
        self.assertEqual(buttons.KEYS["A"], 30)
        self.assertEqual(buttons.KEYS["Z"], 44)

    def test_digits(self):
        self.assertEqual(buttons.KEYS["1"], 2)
        self.assertEqual(buttons.KEYS["0"], 11)

    def test_rejects_nonsense(self):
        for bad in ("", "banana", "alt+", "+tab", "ctrl++a"):
            self.assertIsNone(buttons.parse_keys(bad), bad)

    def test_clicks(self):
        self.assertEqual(buttons.parse_click("left"), buttons.CLICKS["LEFT"])
        self.assertIsNone(buttons.parse_click("sideways"))


class TestDesktopConfig(unittest.TestCase):
    def test_defaults(self):
        desktop = parse({}).desktop
        self.assertTrue(desktop.enabled)
        self.assertEqual(desktop.enter, "L3")
        self.assertEqual(desktop.exit, "GUIDE")

    def test_values_are_read_and_normalised(self):
        desktop = parse({"desktop": {"enter": "r3", "pointer_speed": 1200}}).desktop
        self.assertEqual(desktop.enter, "R3")
        self.assertEqual(desktop.pointer_speed, 1200.0)

    def test_bindings_merge_and_unbind(self):
        desktop = parse({"desktop": {"bindings": {"Y": "key.f5", "A": False}}}).desktop
        self.assertEqual(desktop.bindings["Y"], "key.f5")
        self.assertNotIn("A", desktop.bindings)
        self.assertEqual(desktop.bindings["B"], "click.right")

    def test_numeric_bounds(self):
        for key, bad in (("pointer_speed", 0), ("dead_zone", 0.95), ("curve", 9)):
            with self.assertRaises(ConfigError):
                parse({"desktop": {key: bad}})

    def test_unknown_button_rejected(self):
        with self.assertRaises(ConfigError):
            parse({"desktop": {"enter": "BANANA"}})

    def test_bad_action_rejected(self):
        for bad in ("click.sideways", "key.banana", "wiggle.left", "key."):
            with self.assertRaises(ConfigError, msg=bad):
                parse({"desktop": {"bindings": {"Y": bad}}})

    def test_enter_colliding_with_a_tier_one_binding_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            parse({"desktop": {"enter": "RB"}})  # RB is media.next by default
        self.assertIn("also bound", str(ctx.exception))

    def test_enter_on_r3_still_works_after_the_settings_default(self):
        cfg = parse({"desktop": {"enter": "R3"}})
        self.assertEqual(cfg.desktop.enter, "R3")
        self.assertNotIn("R3", cfg.bindings)
        with self.assertRaises(ConfigError):  # written by hand, it collides
            parse({"desktop": {"enter": "R3"}, "bindings": {"R3": "settings"}})

    def test_enter_cannot_be_the_modifier(self):
        with self.assertRaises(ConfigError):
            parse({"desktop": {"enter": "GUIDE"}})

    def test_exit_colliding_with_a_desktop_binding_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            parse({"desktop": {"exit": "A"}})  # A is click.left by default
        self.assertIn("also bound", str(ctx.exception))

    def test_collision_check_respects_unbinding(self):
        # Freeing RB first makes it a legal enter key.
        cfg = parse({"bindings": {"RB": False}, "desktop": {"enter": "RB"}})
        self.assertEqual(cfg.desktop.enter, "RB")
