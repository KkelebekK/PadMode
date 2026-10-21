"""The digit dial: what one push, one press, one rebound types.

Pure, like the rest of the state machine: events in, key codes out, time
passed in by hand. The daemon half -- the keypad appearing on lock and going
on unlock, the pad grabbed in between -- is at the bottom, against fakes.
"""

from __future__ import annotations

import os
import unittest

from padmode import buttons, unlock
from padmode.config import ConfigError, parse
from padmode.daemon import WAKE, Daemon, Grab, Mode, State, decide, lock_changed
from padmode.unlock import (
    KEY_BACKSPACE,
    KEY_ENTER,
    KEYPAD_KEYS,
    UnlockState,
    sector_of,
    step,
)

ABS = buttons.EV_ABS
KEY = buttons.EV_KEY
FULL = 32767
DIGIT = {d: buttons.KEYS[d] for d in "0123456789"}
SETTLE = 0.15


SYN = (buttons.EV_SYN, 0, 0)


def press(name: str) -> list[tuple[int, int, int]]:
    code = buttons.code_of(name)
    return [(KEY, code, 1), SYN, (KEY, code, 0), SYN]


def push(x: float, y: float, axes=(buttons.ABS_X, buttons.ABS_Y)):
    """Out to (x, y) and straight back home, as two frames."""
    ax, ay = axes
    return [(ABS, ax, int(x * FULL)), (ABS, ay, int(y * FULL)), SYN,
            (ABS, ax, 0), (ABS, ay, 0), SYN]


def frame(*events):
    return [*events, SYN]


class Dial:
    """Feeds events to :func:`step` with the default bindings."""

    def __init__(self, bindings=None, threshold=0.6) -> None:
        self.state = UnlockState()
        self.bindings = dict(unlock.DEFAULT_BINDINGS if bindings is None
                             else bindings)
        self.threshold = threshold
        self.now = 100.0

    def feed(self, events, dt: float = 0.0) -> list[int]:
        out: list[int] = []
        for ev in events:
            out.extend(step(*ev, self.state, self.bindings, self.threshold,
                            SETTLE, {}, self.now))
            self.now += dt
        return out


class SectorTest(unittest.TestCase):
    def test_eight_ways_from_the_top_clockwise(self):
        d = 0.7071
        cases = [((0, -1), 0), ((d, -d), 1), ((1, 0), 2), ((d, d), 3),
                 ((0, 1), 4), ((-d, d), 5), ((-1, 0), 6), ((-d, -d), 7)]
        for (x, y), sector in cases:
            self.assertEqual(sector_of(x, y), sector, (x, y))

    def test_each_sector_is_45_degrees_around_its_direction(self):
        import math
        for degrees, sector in ((22.0, 0), (23.0, 1), (337.6, 0), (337.0, 7)):
            rad = math.radians(degrees)
            self.assertEqual(sector_of(math.sin(rad), -math.cos(rad)), sector,
                             degrees)


class DialTest(unittest.TestCase):
    def test_the_default_dial(self):
        d = 0.7071
        pushes = [(0, -1), (d, -d), (1, 0), (d, d), (0, 1), (-d, d), (-1, 0),
                  (-d, -d)]
        for n, (x, y) in enumerate(pushes, start=1):
            dial = Dial()
            self.assertEqual(dial.feed(push(x, y)), [DIGIT[str(n)]], (x, y))

    def test_x_is_nine_and_y_is_zero(self):
        dial = Dial()
        self.assertEqual(dial.feed(press("X") + press("Y")),
                         [DIGIT["9"], DIGIT["0"]])

    def test_typed_on_the_way_home_not_on_the_way_out(self):
        dial = Dial()
        self.assertEqual(dial.feed(frame((ABS, buttons.ABS_Y, -FULL))), [])
        # Still out there, however long it is held.
        dial.now += 5
        self.assertEqual(dial.feed(frame((ABS, buttons.ABS_Y, -FULL + 10))), [])
        self.assertEqual(dial.feed(frame((ABS, buttons.ABS_Y, 0))), [DIGIT["1"]])

    def test_the_aim_can_be_corrected_before_letting_go(self):
        dial = Dial()
        events = [*frame((ABS, buttons.ABS_Y, -FULL)),          # up: 1
                  *frame((ABS, buttons.ABS_X, FULL)),           # up-right: 2
                  *frame((ABS, buttons.ABS_Y, 0)),              # right: 3
                  *frame((ABS, buttons.ABS_X, 0))]
        self.assertEqual(dial.feed(events), [DIGIT["3"]])

    def test_resting_near_the_edge_types_nothing(self):
        dial = Dial()
        events = [*frame((ABS, buttons.ABS_Y, int(-0.55 * FULL))),
                  *frame((ABS, buttons.ABS_Y, int(-0.45 * FULL))),
                  *frame((ABS, buttons.ABS_Y, 0))]
        self.assertEqual(dial.feed(events), [])

    def test_halfway_back_is_not_home(self):
        dial = Dial()
        dial.feed(frame((ABS, buttons.ABS_Y, -FULL)))
        self.assertEqual(dial.feed(frame((ABS, buttons.ABS_Y, int(-0.4 * FULL)))), [])
        self.assertEqual(dial.feed(frame((ABS, buttons.ABS_Y, int(-0.25 * FULL)))),
                         [DIGIT["1"]])

    def test_the_spring_back_does_not_type_the_opposite_digit(self):
        dial = Dial()
        events = [*frame((ABS, buttons.ABS_Y, -FULL)), *frame((ABS, buttons.ABS_Y, 0)),
                  *frame((ABS, buttons.ABS_Y, int(0.7 * FULL))),  # overshoot, down
                  *frame((ABS, buttons.ABS_Y, 0))]
        self.assertEqual(dial.feed(events, dt=0.01), [DIGIT["1"]])

    def test_the_same_digit_twice_is_never_slowed(self):
        dial = Dial()
        self.assertEqual(dial.feed(push(0, -1) + push(0, -1), dt=0.01),
                         [DIGIT["1"], DIGIT["1"]])

    def test_a_neighbouring_digit_right_away_is_fine(self):
        dial = Dial()
        self.assertEqual(dial.feed(push(0, -1) + push(0.7071, -0.7071), dt=0.01),
                         [DIGIT["1"], DIGIT["2"]])

    def test_the_opposite_digit_once_settled(self):
        dial = Dial()
        typed = dial.feed(push(0, -1), dt=0.01)
        dial.now += SETTLE
        typed += dial.feed(push(0, 1), dt=0.01)
        self.assertEqual(typed, [DIGIT["1"], DIGIT["5"]])

    def test_enter_backspace_and_clear(self):
        dial = Dial()
        typed = dial.feed(push(0, -1) + push(1, 0) + press("X"), dt=0.2)
        self.assertEqual(len(typed), 3)
        self.assertEqual(dial.feed(press("SELECT")), [KEY_BACKSPACE])
        # Clear deletes what the pad typed and no more.
        self.assertEqual(dial.feed(press("B")), [KEY_BACKSPACE] * 2)
        self.assertEqual(dial.feed(press("B")), [])
        dial.feed(press("Y"))
        self.assertEqual(dial.feed(press("START")), [KEY_ENTER])
        self.assertEqual(dial.feed(press("B")), [])

    def test_unbound_inputs_type_nothing(self):
        dial = Dial()
        self.assertEqual(dial.feed(press("A") + press("LB")), [])
        self.assertEqual(dial.feed(push(0, -1, (buttons.ABS_RX, buttons.ABS_RY))),
                         [])

    def test_anything_can_be_bound(self):
        dial = Dial({"DPAD_UP": "4", "RS_DOWN_LEFT": "7", "A": "enter"})
        events = ([(ABS, buttons.ABS_HAT0Y, -1), (ABS, buttons.ABS_HAT0Y, 0)]
                  + push(-0.7071, 0.7071, (buttons.ABS_RX, buttons.ABS_RY))
                  + push(0, -1)
                  + press("A"))
        self.assertEqual(dial.feed(events, dt=0.2),
                         [DIGIT["4"], DIGIT["7"], KEY_ENTER])

    def test_the_keypad_can_type_nothing_else(self):
        self.assertEqual(set(KEYPAD_KEYS),
                         {*DIGIT.values(), KEY_ENTER, KEY_BACKSPACE})


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = parse({})
        self.assertFalse(cfg.unlock.enabled)
        self.assertEqual(cfg.unlock.bindings["LS_UP"], "1")
        self.assertEqual(cfg.unlock.bindings["LS_UP_LEFT"], "8")
        self.assertEqual(cfg.unlock.bindings["X"], "9")
        self.assertEqual(cfg.unlock.bindings["Y"], "0")

    def test_overrides_merge_and_unbind(self):
        cfg = parse({"unlock": {"bindings": {
            "left_stick_up": 0, "dpad_up": "enter", "B": False,
            "rs-down-left": "3",
        }}})
        b = cfg.unlock.bindings
        self.assertEqual(b["LS_UP"], "0")
        self.assertEqual(b["DPAD_UP"], "enter")
        self.assertEqual(b["RS_DOWN_LEFT"], "3")
        self.assertNotIn("B", b)
        self.assertEqual(b["LS_RIGHT"], "3")  # untouched default

    def test_rejects(self):
        for bindings in ({"LS_SIDEWAYS": "1"}, {"A": "10"}, {"A": "ctrl"},
                         {"A": 12}, {"GUIDE": "1"}):
            with self.assertRaises(ConfigError, msg=bindings):
                parse({"unlock": {"bindings": bindings}})
        with self.assertRaises(ConfigError):
            parse({"unlock": {"stick_threshold": 1.5}})


class LockChangedTest(unittest.TestCase):
    def test_locking_hands_the_pad_to_the_dial(self):
        cfg = parse({"unlock": {"enabled": True}})
        state = State(mode=Mode.DESKTOP)
        state.unlock.typed = 4
        decision = lock_changed(True, state, cfg)
        self.assertEqual((decision.mode, decision.grab),
                         (Mode.UNLOCK, Grab.ACQUIRE))
        self.assertEqual(state.unlock.typed, 0)
        self.assertFalse(lock_changed(True, state, cfg))  # already there
        decision = lock_changed(False, state, cfg)
        self.assertEqual((decision.mode, decision.grab),
                         (Mode.IDLE, Grab.RELEASE))
        self.assertFalse(lock_changed(False, state, cfg))

    def test_off_means_only_knowing(self):
        state = State()
        self.assertFalse(lock_changed(True, state, parse({})))
        self.assertTrue(state.locked)
        self.assertIs(state.mode, Mode.IDLE)

    def test_guide_does_nothing_at_the_lock_screen(self):
        cfg = parse({"unlock": {"enabled": True}})
        state = State()
        lock_changed(True, state, cfg)
        guide = buttons.code_of("GUIDE")
        self.assertFalse(decide(KEY, guide, 1, state, cfg))
        self.assertIs(state.mode, Mode.UNLOCK)
        self.assertEqual(
            decide(KEY, buttons.code_of("X"), 1, state, cfg).keypad,
            (DIGIT["9"],),
        )


class XboxLayoutTest(unittest.TestCase):
    """The README's Xbox-style layout parses, and types what it promises."""

    def test_the_readme_block(self):
        import pathlib
        import re
        import tomllib

        readme = pathlib.Path(__file__).resolve().parent.parent / "README.md"
        blocks = re.findall(r"```toml\n(.*?)```", readme.read_text(), re.S)
        block = next(b for b in blocks if 'DPAD_LEFT = "2"' in b)
        data = tomllib.loads(block)
        data["unlock"]["enabled"] = True
        cfg = parse(data)
        state = State(trigger_axes={buttons.ABS_Z: buttons.code_of("LT"),
                                    buttons.ABS_RZ: buttons.code_of("RT")})
        lock_changed(True, state, cfg)
        hy, hx = buttons.ABS_HAT0Y, buttons.ABS_HAT0X
        events = [(ABS, hy, -1), (ABS, hy, 0), (ABS, hx, -1), (ABS, hx, 0),
                  (ABS, hy, 1), (ABS, hy, 0), (ABS, hx, 1), (ABS, hx, 0),
                  (ABS, buttons.ABS_Z, 255), (ABS, buttons.ABS_Z, 0),
                  (ABS, buttons.ABS_RZ, 255), (ABS, buttons.ABS_RZ, 0)]
        for name in ("LB", "RB", "Y", "X"):
            events += press(name)
        events += push(0, -1)  # the dial is off in this layout
        typed = [code for ev in events
                 for code in decide(*ev, state, cfg, 0.0).keypad]
        self.assertEqual(typed, [DIGIT[d] for d in "1234567890"])


# --- the daemon, against fakes ---------------------------------------------
class FakeWatcher:
    """Lock states handed out one WAKE at a time."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.stopped = False
        self._r, self._w = os.pipe()

    def start(self) -> bool:
        return True

    def fileno(self):
        return self._r

    def poll(self):
        return [self.script.pop(0)] if self.script else []

    def stop(self) -> None:
        self.stopped = True
        for fd in (self._r, self._w):
            try:
                os.close(fd)
            except OSError:
                pass


class FakeKeypad:
    def __init__(self, log, ok=True) -> None:
        self.log = log
        self.ok = ok

    def open(self) -> bool:
        self.log.append(("keypad", "open"))
        return self.ok

    def tap(self, code) -> None:
        self.log.append(("tap", code))

    def close(self) -> None:
        self.log.append(("keypad", "close"))


class FakeReader:
    def __init__(self, log, script) -> None:
        self.log = log
        self.script = script
        self.axis_ranges = {}
        self.trigger_axes = {}

    def watch(self, fd) -> None:
        pass

    def unwatch(self, fd) -> None:
        pass

    def grab(self) -> None:
        self.log.append(("grab",))

    def ungrab(self) -> None:
        self.log.append(("ungrab",))

    def set_tick(self, interval) -> None:
        pass

    def events(self):
        yield from self.script
        self.log.append(("end",))


class FakeRunner:
    def preflight(self, bindings) -> None:
        pass

    def available(self, binary) -> bool:
        return True

    def run(self, binding) -> bool:
        return True


def run_daemon(script, locks, keypad_ok=True):
    log: list = []
    reader = FakeReader(log, script)
    daemon = Daemon(parse({"unlock": {"enabled": True}}), reader, FakeRunner(),
                    lock=FakeWatcher(locks), keypad=FakeKeypad(log, keypad_ok))
    with unittest.TestCase().assertLogs("padmode", "INFO"):
        daemon.run()
    # The shutdown tail is the same every time; the interesting part is above.
    return daemon, log[:log.index(("end",))]


class DaemonTest(unittest.TestCase):
    def test_the_keypad_lives_exactly_as_long_as_the_lock(self):
        x = buttons.code_of("X")
        _, log = run_daemon(
            [(KEY, x, 1), WAKE, (KEY, x, 1), (KEY, x, 0), WAKE, (KEY, x, 1)],
            [True, False],
        )
        self.assertEqual(log, [
            ("keypad", "open"), ("grab",),
            ("tap", DIGIT["9"]),
            ("keypad", "close"), ("ungrab",),
        ])

    def test_no_keypad_means_the_pad_is_handed_back(self):
        x = buttons.code_of("X")
        daemon, log = run_daemon([WAKE, (KEY, x, 1)], [True], keypad_ok=False)
        self.assertEqual(log, [("keypad", "open"), ("ungrab",)])
        self.assertIs(daemon.state.mode, Mode.IDLE)
        self.assertTrue(daemon.state.locked)


if __name__ == "__main__":
    unittest.main()
