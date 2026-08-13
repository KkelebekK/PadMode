"""The frontend: going home, and landing there when the game has gone.

Two features share one mechanism (v0.4). Both end with "activate a window once
the pad is no longer ours", which is the sequencing gotcha §6.1 exists for, so
the tests here assert on the *order* of entries in a shared log exactly as the
overlay tests do -- and reuse their fakes, so the two cannot drift apart.

The fallback is the interesting one. A window id outlives its window: when the
game exits while an overlay is up, activating the remembered id succeeds and
does nothing. :class:`DeadWindowBackend` is that compositor.
"""

from __future__ import annotations

import unittest

from padmode import buttons
from padmode.config import ConfigError, Frontend, parse
from padmode.daemon import (
    LAUNCH_POLL,
    RETURN_VERIFY_ATTEMPTS,
    RETURN_VERIFY_DELAY,
    Mode,
)

from .test_overlay import CHAT, GAME, GUIDE, KEY, B, DaemonFixture, FakeBackend

SELECT = buttons.code_of("SELECT")
L3 = buttons.code_of("L3")
HOME = "{pegasus}"


def frontend_config(**sections):
    """A frontend bound to SELECT, plus any extra or replacement sections."""
    data = {
        "frontend": {"class": "pegasus"},
        "bindings": {"SELECT": "frontend"},
    }
    data.update(sections)
    return parse(data)


def overlay_and_frontend(**sections):
    """An overlay on B and a frontend on SELECT: the fallback's setting."""
    data = {
        "overlays": {"chat": {"class": "discord", "command": ["discord"]}},
        "frontend": {"class": "pegasus"},
        "bindings": {"B": "overlay.chat", "SELECT": "frontend"},
    }
    data.update(sections)
    return parse(data)


class DeadWindowBackend(FakeBackend):
    """A compositor where activating a dead window id quietly does nothing.

    This is what `kdotool windowactivate` does with the id of a game that has
    since exited: exit status 0, no error, no effect. Reproducing it is the
    only way to test the fallback, because every other fake would report the
    activation as having worked.
    """

    def __init__(self, log, *, dead=None, **kwargs):
        super().__init__(log, **kwargs)
        self.dead = dead

    def activate_window(self, window_id):
        self.log.append(("activate", window_id))
        if window_id != self.dead:
            self.active = window_id


class SlowBackend(FakeBackend):
    """A compositor that honours an activation, but not immediately.

    Guards against the verification being trigger-happy: a return that is
    merely slow must not throw the user at the frontend.
    """

    def __init__(self, log, *, looks=1, **kwargs):
        super().__init__(log, **kwargs)
        self.looks = looks
        self._requested = None

    def activate_window(self, window_id):
        self.log.append(("activate", window_id))
        self._requested = window_id

    def get_active_window(self):
        if self._requested is not None:
            if self.looks <= 0:
                self.active, self._requested = self._requested, None
            else:
                self.looks -= 1
        return self.active


class TestFrontendConfig(unittest.TestCase):
    def test_a_frontend_can_be_declared_and_bound(self):
        config = frontend_config()
        self.assertEqual(config.frontend, Frontend(window_class="pegasus"))
        self.assertEqual(config.bindings["SELECT"], "frontend")

    def test_there_is_no_frontend_by_default(self):
        self.assertIsNone(parse({}).frontend)

    def test_the_command_is_optional(self):
        config = frontend_config(frontend={"class": "p", "command": ["pegasus-fe"]})
        self.assertEqual(config.frontend.command, ["pegasus-fe"])

    def test_class_is_required(self):
        with self.assertRaises(ConfigError) as caught:
            parse({"frontend": {"command": ["pegasus-fe"]}})
        self.assertIn("frontend.class", str(caught.exception))

    def test_an_empty_section_is_an_error_rather_than_no_frontend(self):
        # Writing [frontend] and nothing else is a mistake, not a way to
        # switch it off. Saying so beats a button that silently does nothing.
        with self.assertRaises(ConfigError):
            parse({"frontend": {}})

    def test_class_must_not_be_blank(self):
        with self.assertRaises(ConfigError):
            parse({"frontend": {"class": "  "}})

    def test_command_must_be_a_list_of_strings(self):
        with self.assertRaises(ConfigError):
            parse({"frontend": {"class": "p", "command": "pegasus-fe"}})

    def test_an_unknown_key_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse({"frontend": {"class": "p", "clas": "typo"}})
        self.assertIn("clas", str(caught.exception))

    def test_binding_the_frontend_without_declaring_one_is_an_error(self):
        with self.assertRaises(ConfigError) as caught:
            parse({"bindings": {"SELECT": "frontend"}})
        self.assertIn("[frontend]", str(caught.exception))

    def test_an_unknown_action_lists_the_frontend_when_one_exists(self):
        with self.assertRaises(ConfigError) as caught:
            parse({
                "frontend": {"class": "p"},
                "bindings": {"SELECT": "frontendd"},
            })
        self.assertIn("frontend", str(caught.exception))


class TestGoingHome(DaemonFixture):
    """The bound button: raise the frontend and give it the pad back."""

    def press_home(self, release=True):
        self.daemon._handle(KEY, GUIDE, 1)
        self.daemon._handle(KEY, SELECT, 1)
        if release:
            self.daemon._handle(KEY, GUIDE, 0)

    def test_the_frontend_is_activated(self):
        self.build(frontend_config(), windows={"pegasus": HOME})
        self.press_home()
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertIn(("activate", HOME), self.log)

    def test_the_pad_is_released_before_the_frontend_takes_focus(self):
        # Gotcha §6.1, and the reason this is deferred at all: the frontend is
        # the very application a leaked gamepad event used to crash.
        self.build(frontend_config(), windows={"pegasus": HOME})
        self.press_home()
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertLess(self.index_of("ungrab"), self.index_of("activate"))

    def test_nothing_happens_while_the_modifier_is_still_held(self):
        # Waiting on the release, not on the clock: no amount of time passing
        # is allowed to hand focus over while we still hold the pad.
        self.build(frontend_config(), windows={"pegasus": HOME})
        self.press_home(release=False)
        self.clock.sleep(60.0)
        self.daemon._tick()
        self.assertNotIn("activate", self.kinds())

    def test_it_does_not_take_the_pad_over(self):
        # Unlike an overlay. The frontend reads the gamepad itself, so the
        # useful thing is to give the device back, not drive it with a mouse.
        self.build(frontend_config(), windows={"pegasus": HOME})
        self.press_home()
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertIs(self.daemon.state.mode, Mode.IDLE)
        self.assertFalse(self.reader.grabbed)
        self.assertIsNone(self.reader.tick)

    def test_it_starts_the_frontend_when_it_is_not_running(self):
        config = frontend_config(
            frontend={"class": "pegasus", "command": ["pegasus-fe"]}
        )
        self.build(config, windows={})
        self.press_home()
        self.assertIn(("run", ("pegasus-fe",)), self.log)

        self.backend.windows["pegasus"] = HOME
        self.clock.sleep(LAUNCH_POLL)
        self.daemon._tick()
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertIn(("activate", HOME), self.log)

    def test_a_window_that_never_appears_gives_up_quietly(self):
        config = frontend_config(
            frontend={"class": "pegasus", "command": ["pegasus-fe"]}
        )
        self.build(config, windows={})
        self.press_home()
        self.clock.sleep(self.config.windows.launch_timeout + LAUNCH_POLL)
        self.daemon._tick()
        self.assertNotIn("activate", self.kinds())
        self.assertIsNone(self.reader.tick)

    def test_without_a_command_it_only_says_so(self):
        self.build(frontend_config(), windows={})
        self.press_home()
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertNotIn("activate", self.kinds())
        self.assertNotIn("run", self.kinds())

    def test_without_a_window_backend_nothing_happens(self):
        self.build(frontend_config(), backend=None)
        self.press_home()
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertNotIn("activate", self.kinds())

    def test_opening_a_pad_driven_mode_cancels_it(self):
        # Pressing the frontend button and then asking for desktop mode means
        # the user changed their mind; landing on the frontend when they
        # eventually leave desktop mode would be a surprise minutes later.
        self.build(frontend_config(), windows={"pegasus": HOME})
        self.daemon._handle(KEY, GUIDE, 1)
        self.daemon._handle(KEY, SELECT, 1)
        self.daemon._handle(KEY, L3, 1)
        self.daemon._handle(KEY, GUIDE, 0)
        self.assertFalse(self.daemon._home_pending)
        self.daemon._handle(KEY, GUIDE, 1)  # leave desktop mode
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertNotIn(("activate", HOME), self.log)

    def test_an_overlay_that_cannot_open_still_goes_home(self):
        # The refusal path releases the pad itself rather than going through a
        # decision, so the deferred activation has to be armed there too.
        config = overlay_and_frontend(
            overlays={"chat": {"class": "discord"}},  # no command, not running
        )
        self.build(config, windows={"pegasus": HOME})
        self.daemon._handle(KEY, GUIDE, 1)
        self.daemon._handle(KEY, SELECT, 1)
        self.daemon._handle(KEY, B, 1)
        self.assertIs(self.daemon.state.mode, Mode.IDLE)
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertIn(("activate", HOME), self.log)

    def test_opening_an_overlay_cancels_a_frontend_still_launching(self):
        # Otherwise the frontend appears seconds later and takes the focus the
        # overlay was just given.
        config = overlay_and_frontend(
            frontend={"class": "pegasus", "command": ["pegasus-fe"]}
        )
        self.build(config, windows={"discord": CHAT})
        self.daemon._handle(KEY, GUIDE, 1)
        self.daemon._handle(KEY, SELECT, 1)
        self.daemon._handle(KEY, B, 1)
        self.assertIsNone(self.daemon._pending_launch)

        self.backend.windows["pegasus"] = HOME
        self.clock.sleep(LAUNCH_POLL + self.config.windows.settle)
        self.daemon._tick()
        self.assertNotIn(("activate", HOME), self.log)
        self.assertIs(self.daemon.state.mode, Mode.OVERLAY)


class TestFallingBackToTheFrontend(DaemonFixture):
    """The game exited while the overlay was up; the remembered id is dead."""

    def verify(self):
        """Advance past the settle, then through every verification look."""
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        for _ in range(RETURN_VERIFY_ATTEMPTS):
            self.clock.sleep(RETURN_VERIFY_DELAY)
            self.daemon._tick()

    def build_dead(self, **kwargs):
        return self.build(
            overlay_and_frontend(),
            backend_cls=DeadWindowBackend,
            dead=GAME,
            windows={"discord": CHAT, "pegasus": HOME},
            active=GAME,
            **kwargs,
        )

    def test_a_dead_return_target_lands_on_the_frontend(self):
        self.build_dead()
        self.enter_overlay()
        self.leave_overlay()
        self.verify()
        self.assertIn(("activate", GAME), self.log)  # tried, and did nothing
        self.assertIn(("activate", HOME), self.log)

    def test_the_frontend_is_never_started_by_the_fallback(self):
        # A game crashing is not a reason to spawn a process on the user's
        # behalf. The button may launch it; this must not.
        config = overlay_and_frontend(
            frontend={"class": "pegasus", "command": ["pegasus-fe"]}
        )
        self.build(
            config, backend_cls=DeadWindowBackend, dead=GAME,
            windows={"discord": CHAT}, active=GAME,
        )
        self.enter_overlay()
        self.leave_overlay()
        self.verify()
        self.assertNotIn(("run", ("pegasus-fe",)), self.log)
        self.assertNotIn(("activate", HOME), self.log)

    def test_a_return_that_worked_is_left_alone(self):
        self.build(
            overlay_and_frontend(),
            windows={"discord": CHAT, "pegasus": HOME},
            active=GAME,
        )
        self.enter_overlay()
        self.leave_overlay()
        self.verify()
        self.assertIn(("activate", GAME), self.log)
        self.assertNotIn(("activate", HOME), self.log)

    def test_a_slow_return_is_not_mistaken_for_a_dead_one(self):
        self.build(
            overlay_and_frontend(),
            backend_cls=SlowBackend,
            looks=RETURN_VERIFY_ATTEMPTS - 1,
            windows={"discord": CHAT, "pegasus": HOME},
            active=GAME,
        )
        self.enter_overlay()
        self.leave_overlay()
        self.verify()
        self.assertNotIn(("activate", HOME), self.log)

    def test_nothing_is_verified_when_no_frontend_is_configured(self):
        # The extra look costs a subprocess. Nobody pays for a fallback they
        # have not configured.
        from .test_overlay import overlay_config

        self.build(overlay_config(), windows={"discord": CHAT}, active=GAME)
        self.enter_overlay()
        self.leave_overlay()
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertIsNone(self.daemon._pending_verify)
        self.assertIsNone(self.reader.tick)

    def test_going_home_is_not_itself_verified(self):
        # Otherwise a frontend that is slow to focus would fall back to the
        # frontend, which is at best pointless and at worst a loop.
        self.build(frontend_config(), windows={"pegasus": HOME})
        self.daemon._handle(KEY, GUIDE, 1)
        self.daemon._handle(KEY, SELECT, 1)
        self.daemon._handle(KEY, GUIDE, 0)
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertIsNone(self.daemon._pending_verify)

    def test_the_tick_stops_once_the_check_is_done(self):
        self.build_dead()
        self.enter_overlay()
        self.leave_overlay()
        self.verify()
        self.assertIsNone(self.reader.tick)

    def test_a_missing_frontend_window_is_reported_not_crashed(self):
        self.build(
            overlay_and_frontend(),
            backend_cls=DeadWindowBackend, dead=GAME,
            windows={"discord": CHAT},  # the frontend is gone too
            active=GAME,
        )
        self.enter_overlay()
        self.leave_overlay()
        self.verify()
        self.assertIn(("find", "pegasus"), self.log)
        self.assertIsNone(self.daemon._pending_verify)


if __name__ == "__main__":
    unittest.main()
