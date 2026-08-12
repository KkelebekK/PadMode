"""Overlay mode: config, transitions, and the order things happen in.

The interesting part of v0.3 is sequencing, not arithmetic. Gotcha §6.1 says
the pad must be released *before* the window we came from regains focus, and
"it works on my machine" cannot tell the difference between that and the
reverse -- so the fakes here share one log and the tests assert on the order of
entries in it.

Time is faked too. The settle delay and the launch timeout are the whole
feature; testing them by sleeping would make the suite slow and flaky at once.
"""

from __future__ import annotations

import unittest
from unittest import mock

from padmode import buttons, daemon as daemon_module
from padmode.config import Config, ConfigError, parse
from padmode.daemon import Daemon, Grab, Mode, State, decide
from padmode.windows.backend import Rect, WindowBackend

GUIDE = buttons.code_of("GUIDE")
B = buttons.code_of("B")
A = buttons.code_of("A")
L3 = buttons.code_of("L3")
KEY = buttons.EV_KEY

GAME = "{game}"
CHAT = "{chat}"


def overlay_config(**sections) -> Config:
    """A config with one overlay bound to B, plus any extra sections."""
    data = {
        "overlays": {"chat": {"class": "discord", "command": ["discord"]}},
        "bindings": {"B": "overlay.chat"},
    }
    data.update(sections)
    return parse(data)


class FakeClock:
    """Stands in for the ``time`` module inside daemon.py."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeReader:
    def __init__(self, log: list) -> None:
        self.log = log
        self.grabbed = False
        self.tick: float | None = None
        self.axis_ranges: dict[int, tuple[int, int]] = {}

    def grab(self) -> None:
        self.grabbed = True
        self.log.append(("grab",))

    def ungrab(self) -> None:
        # Recorded even when we were not grabbed: the release path runs from a
        # finally that may fire either way, and the order still matters.
        self.grabbed = False
        self.log.append(("ungrab",))

    def set_tick(self, interval: float | None) -> None:
        self.tick = interval
        self.log.append(("tick", interval))


class ScriptedReader(FakeReader):
    """A reader that replays a fixed script, then ends the loop."""

    def __init__(self, log: list, script: list) -> None:
        super().__init__(log)
        self.script = script

    def events(self):
        for item in self.script:
            if callable(item):  # a clock advance, or any other side effect
                item()
                continue
            yield item


class FakeRunner:
    def __init__(self, log: list, ok: bool = True) -> None:
        self.log = log
        self.ok = ok

    def preflight(self, bindings) -> None:
        self.log.append(("preflight", tuple(sorted(bindings))))

    def available(self, binary: str) -> bool:
        return self.ok

    def run(self, binding) -> bool:
        self.log.append(("run", tuple(binding)))
        return self.ok


class FakeBackend(WindowBackend):
    """A compositor that does as it is told and remembers being told."""

    name = "fake"

    def __init__(
        self,
        log: list,
        *,
        active: str | None = GAME,
        windows: dict[str, str] | None = None,
        geometry: Rect | None = None,
        cursor: tuple[int, int] = (0, 0),
    ) -> None:
        self.log = log
        self.active = active
        self.windows = dict(windows or {})
        self.geometry = geometry
        self.cursor = cursor
        #: Set False to model a backend that implements only the required ops.
        self.can_report = True

    def find_window(self, window_class: str) -> str | None:
        self.log.append(("find", window_class))
        return self.windows.get(window_class)

    def activate_window(self, window_id: str) -> None:
        self.log.append(("activate", window_id))
        self.active = window_id

    def get_active_window(self) -> str | None:
        return self.active

    def window_geometry(self, window_id: str) -> Rect | None:
        return self.geometry if self.can_report else None

    def pointer_position(self) -> tuple[int, int] | None:
        return self.cursor if self.can_report else None


class FakeVirtual:
    """Records virtual output, and moves the fake backend's cursor.

    ``gain`` models pointer acceleration: what the compositor does with a
    relative delta is not what we asked for, which is the whole reason parking
    is a feedback loop rather than one subtraction.
    """

    def __init__(self, log: list, backend: FakeBackend | None = None,
                 gain: float = 1.0, opens: bool = True) -> None:
        self.log = log
        self.backend = backend
        self.gain = gain
        self.opens = opens
        self.moves: list[tuple[float, float]] = []

    def open(self) -> bool:
        return self.opens

    def close(self) -> None:
        pass

    def release_all(self) -> None:
        self.log.append(("release_all",))

    def move(self, dx: float, dy: float) -> None:
        self.moves.append((dx, dy))
        if self.backend is not None:
            x, y = self.backend.cursor
            self.backend.cursor = (
                round(x + dx * self.gain), round(y + dy * self.gain)
            )

    def scroll(self, dx: float, dy: float) -> None:
        pass

    def click(self, code: int, pressed: bool) -> None:
        self.log.append(("click", code, pressed))

    def key(self, code: int, pressed: bool) -> None:
        self.log.append(("key", code, pressed))

    def tap_keys(self, codes) -> None:
        self.log.append(("tap", tuple(codes)))


class DaemonFixture(unittest.TestCase):
    """Builds a daemon whose every collaborator is fake, on a fake clock."""

    def build(self, config=None, *, backend="default", virtual_gain=1.0,
              runner_ok=True, script=None, **backend_kwargs):
        self.log: list = []
        self.clock = FakeClock()
        patcher = mock.patch.object(daemon_module, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.config = config or overlay_config()
        if backend == "default":
            backend = FakeBackend(self.log, **backend_kwargs)
        self.backend = backend
        self.reader = (
            FakeReader(self.log) if script is None
            else ScriptedReader(self.log, script)
        )
        self.runner = FakeRunner(self.log, ok=runner_ok)
        self.virtual = FakeVirtual(self.log, backend, gain=virtual_gain)
        self.daemon = Daemon(
            self.config, self.reader, self.runner, self.virtual, backend
        )
        return self.daemon

    def enter_overlay(self) -> None:
        self.daemon._handle(KEY, GUIDE, 1)
        self.daemon._handle(KEY, B, 1)

    def leave_overlay(self) -> None:
        """Release the modifier we came in with, then press exit.

        The log is cleared in between: entering activates a window too, and an
        assertion about ordering wants to see only what leaving did.
        """
        self.daemon._handle(KEY, GUIDE, 0)
        self.log.clear()
        self.daemon._handle(KEY, GUIDE, 1)

    def index_of(self, kind: str) -> int:
        for position, entry in enumerate(self.log):
            if entry[0] == kind:
                return position
        raise AssertionError(f"{kind!r} never happened: {self.log}")

    def kinds(self) -> list[str]:
        return [entry[0] for entry in self.log]


class TestOverlayConfig(unittest.TestCase):
    def test_an_overlay_can_be_declared_and_bound(self):
        config = overlay_config()
        overlay = config.overlays["chat"]
        self.assertEqual(overlay.window_class, "discord")
        self.assertEqual(overlay.command, ["discord"])
        self.assertEqual(config.bindings["B"], "overlay.chat")

    def test_overlay_for_resolves_a_button(self):
        config = overlay_config()
        self.assertIs(config.overlay_for("B"), config.overlays["chat"])

    def test_overlay_for_is_none_for_an_ordinary_action(self):
        self.assertIsNone(overlay_config().overlay_for("RB"))

    def test_binding_an_undeclared_overlay_is_an_error(self):
        with self.assertRaises(ConfigError) as caught:
            parse({"bindings": {"B": "overlay.discord"}})
        self.assertIn("no overlay named 'discord'", str(caught.exception))

    def test_the_error_lists_the_overlays_that_do_exist(self):
        with self.assertRaises(ConfigError) as caught:
            parse({
                "overlays": {"chat": {"class": "discord"}},
                "bindings": {"B": "overlay.chatt"},
            })
        self.assertIn("chat", str(caught.exception))

    def test_command_is_optional(self):
        config = parse({
            "overlays": {"chat": {"class": "discord"}},
            "bindings": {"B": "overlay.chat"},
        })
        self.assertIsNone(config.overlays["chat"].command)

    def test_class_is_required(self):
        with self.assertRaises(ConfigError) as caught:
            parse({"overlays": {"chat": {"command": ["discord"]}}})
        self.assertIn("class", str(caught.exception))

    def test_class_must_not_be_blank(self):
        with self.assertRaises(ConfigError):
            parse({"overlays": {"chat": {"class": "   "}}})

    def test_command_must_be_a_list_of_strings(self):
        with self.assertRaises(ConfigError):
            parse({"overlays": {"chat": {"class": "d", "command": "discord"}}})

    def test_command_must_not_be_empty(self):
        with self.assertRaises(ConfigError):
            parse({"overlays": {"chat": {"class": "d", "command": []}}})

    def test_a_misspelled_key_is_an_error_not_a_shrug(self):
        # `comand` would silently turn a launching overlay into one that only
        # works when the app is already open.
        with self.assertRaises(ConfigError) as caught:
            parse({"overlays": {"chat": {"class": "d", "comand": ["discord"]}}})
        self.assertIn("comand", str(caught.exception))

    def test_overlay_must_be_a_table(self):
        with self.assertRaises(ConfigError):
            parse({"overlays": {"chat": "discord"}})

    def test_overlay_name_must_be_referenceable(self):
        with self.assertRaises(ConfigError) as caught:
            parse({"overlays": {"my chat": {"class": "d"}}})
        self.assertIn("usable name", str(caught.exception))

    def test_unknown_action_error_mentions_overlays(self):
        with self.assertRaises(ConfigError) as caught:
            parse({
                "overlays": {"chat": {"class": "d"}},
                "bindings": {"B": "media.rewind"},
            })
        self.assertIn("overlay.chat", str(caught.exception))


class TestWindowsConfig(unittest.TestCase):
    def test_defaults(self):
        windows = Config().windows
        self.assertEqual(windows.backend, "auto")
        self.assertEqual(windows.settle, 0.7)
        self.assertFalse(windows.park_pointer)

    def test_backend_must_be_known(self):
        with self.assertRaises(ConfigError) as caught:
            parse({"windows": {"backend": "mutter"}})
        self.assertIn("auto", str(caught.exception))

    def test_backend_none_is_accepted(self):
        self.assertEqual(parse({"windows": {"backend": "none"}}).windows.backend,
                         "none")

    def test_settle_is_range_checked(self):
        with self.assertRaises(ConfigError):
            parse({"windows": {"settle": 30}})

    def test_settle_accepts_an_integer(self):
        self.assertEqual(parse({"windows": {"settle": 1}}).windows.settle, 1.0)

    def test_park_pointer_must_be_a_bool(self):
        with self.assertRaises(ConfigError):
            parse({"windows": {"park_pointer": "yes"}})

    def test_unknown_key_is_an_error(self):
        with self.assertRaises(ConfigError) as caught:
            parse({"windows": {"setle": 0.5}})
        self.assertIn("setle", str(caught.exception))


class TestOverlayTransitions(unittest.TestCase):
    """The pure half: what decide() makes of the events."""

    def setUp(self):
        self.config = overlay_config()
        self.state = State()

    def enter(self):
        decide(KEY, GUIDE, 1, self.state, self.config)
        return decide(KEY, B, 1, self.state, self.config)

    def test_modifier_plus_bound_button_enters_overlay_mode(self):
        decision = self.enter()
        self.assertIs(decision.mode, Mode.OVERLAY)
        self.assertEqual(decision.overlay, "chat")
        self.assertIs(self.state.mode, Mode.OVERLAY)

    def test_entering_does_not_change_the_grab(self):
        # We are already grabbed from the modifier press.
        self.assertIsNone(self.enter().grab)

    def test_entering_runs_no_action(self):
        self.assertIsNone(self.enter().binding)

    def test_overlay_mode_drives_the_pointer_like_desktop_mode(self):
        self.enter()
        decision = decide(KEY, A, 1, self.state, self.config, 1.0)
        self.assertEqual(decision.virtual[0].codes, (buttons.CLICKS["LEFT"],))

    def test_the_modifier_release_does_not_leave(self):
        # The modifier is still physically held when the overlay opens.
        self.enter()
        self.assertFalse(decide(KEY, GUIDE, 0, self.state, self.config))
        self.assertIs(self.state.mode, Mode.OVERLAY)

    def test_exit_button_leaves_and_releases_the_pad(self):
        self.enter()
        decision = decide(KEY, GUIDE, 1, self.state, self.config)
        self.assertIs(decision.mode, Mode.IDLE)
        self.assertIs(decision.grab, Grab.RELEASE)

    def test_other_bindings_still_work_while_the_modifier_is_held(self):
        decide(KEY, GUIDE, 1, self.state, self.config)
        self.assertEqual(
            decide(KEY, buttons.code_of("RB"), 1, self.state, self.config).binding,
            "media.next",
        )


class TestEnteringAnOverlay(DaemonFixture):
    def test_raises_a_window_that_is_already_open(self):
        self.build(windows={"discord": CHAT})
        self.enter_overlay()
        self.assertIn(("activate", CHAT), self.log)
        self.assertIs(self.daemon.state.mode, Mode.OVERLAY)

    def test_remembers_the_window_it_came_from(self):
        self.build(windows={"discord": CHAT}, active=GAME)
        self.enter_overlay()
        self.assertEqual(self.daemon._return_to, GAME)

    def test_starts_the_pointer_tick(self):
        self.build(windows={"discord": CHAT})
        self.enter_overlay()
        self.assertEqual(self.reader.tick, daemon_module.TICK_INTERVAL)

    def test_launches_the_app_when_no_window_matches(self):
        self.build(windows={})
        self.enter_overlay()
        self.assertIn(("run", ("discord",)), self.log)
        # Nothing to activate yet -- the app has not drawn a window.
        self.assertNotIn("activate", self.kinds())
        self.assertIs(self.daemon.state.mode, Mode.OVERLAY)

    def test_activates_the_window_once_it_appears(self):
        self.build(windows={})
        self.enter_overlay()
        self.clock.sleep(daemon_module.LAUNCH_POLL)
        self.daemon._tick()  # still starting up
        self.assertNotIn("activate", self.kinds())

        self.backend.windows["discord"] = CHAT
        self.clock.sleep(daemon_module.LAUNCH_POLL)
        self.daemon._tick()
        self.assertIn(("activate", CHAT), self.log)

    def test_polling_is_not_once_per_tick(self):
        # Each look spawns a process; sixty a second would be absurd.
        self.build(windows={})
        self.enter_overlay()
        before = self.kinds().count("find")
        for _ in range(10):
            self.clock.sleep(daemon_module.TICK_INTERVAL)
            self.daemon._tick()
        self.assertEqual(self.kinds().count("find"), before)

    def test_gives_up_after_the_launch_timeout(self):
        self.build(windows={})
        self.enter_overlay()
        self.clock.sleep(self.config.windows.launch_timeout + 1)
        self.daemon._tick()
        self.assertIsNone(self.daemon._pending_launch)
        # Giving up on the launch does not throw the user out of the mode.
        self.assertIs(self.daemon.state.mode, Mode.OVERLAY)

    def test_a_window_that_appears_late_still_counts(self):
        self.build(windows={})
        self.enter_overlay()
        self.clock.sleep(self.config.windows.launch_timeout - 0.1)
        self.backend.windows["discord"] = CHAT
        self.daemon._tick()
        self.assertIn(("activate", CHAT), self.log)


class TestRefusingAnOverlay(DaemonFixture):
    """A transition that cannot work must leave the session as it found it."""

    def assert_backed_out(self):
        self.assertIs(self.daemon.state.mode, Mode.IDLE)
        self.assertIn(("ungrab",), self.log)
        self.assertIsNone(self.daemon._return_to)
        self.assertNotIn("activate", self.kinds())

    def test_without_a_window_backend(self):
        self.build(backend=None)
        self.enter_overlay()
        self.assert_backed_out()

    def test_when_nothing_matches_and_there_is_no_command(self):
        config = parse({
            "overlays": {"chat": {"class": "discord"}},
            "bindings": {"B": "overlay.chat"},
        })
        self.build(config, windows={})
        self.enter_overlay()
        self.assert_backed_out()

    def test_when_the_command_cannot_be_run(self):
        self.build(windows={}, runner_ok=False)
        self.enter_overlay()
        self.assert_backed_out()

    def test_when_virtual_input_is_unavailable(self):
        self.build(windows={"discord": CHAT})
        self.virtual.opens = False
        self.enter_overlay()
        self.assert_backed_out()

    def test_the_tick_is_not_left_running(self):
        self.build(backend=None)
        self.enter_overlay()
        self.assertIsNone(self.reader.tick)


class TestReturningFromAnOverlay(DaemonFixture):
    def test_the_pad_is_released_before_the_old_window_takes_focus(self):
        # Gotcha §6.1. The reverse order leaks a still-held return button into
        # the newly focused window, which is what crashed the old frontend.
        self.build(windows={"discord": CHAT})
        self.enter_overlay()
        self.leave_overlay()
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertLess(self.index_of("ungrab"), self.index_of("activate"))

    def test_nothing_happens_until_the_settle_has_passed(self):
        self.build(windows={"discord": CHAT})
        self.enter_overlay()
        self.leave_overlay()
        self.clock.sleep(self.config.windows.settle - 0.01)
        self.daemon._tick()
        self.assertNotIn("activate", self.kinds())

    def test_the_window_we_came_from_is_activated(self):
        self.build(windows={"discord": CHAT}, active=GAME)
        self.enter_overlay()
        self.leave_overlay()
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertIn(("activate", GAME), self.log)

    def test_the_settle_is_configurable(self):
        config = overlay_config(windows={"settle": 2.0})
        self.build(config, windows={"discord": CHAT})
        self.enter_overlay()
        self.leave_overlay()
        self.clock.sleep(1.0)
        self.daemon._tick()
        self.assertNotIn(("activate", GAME), self.log)
        self.clock.sleep(1.0)
        self.daemon._tick()
        self.assertIn(("activate", GAME), self.log)

    def test_the_tick_stops_once_the_return_is_done(self):
        self.build(windows={"discord": CHAT})
        self.enter_overlay()
        self.leave_overlay()
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertIsNone(self.reader.tick)

    def test_the_pad_is_handed_back_immediately_not_after_the_settle(self):
        self.build(windows={"discord": CHAT})
        self.enter_overlay()
        self.leave_overlay()
        self.assertFalse(self.reader.grabbed)

    def test_re_entering_cancels_a_return_that_is_still_pending(self):
        # Otherwise the old return fires mid-overlay and yanks focus away.
        self.build(windows={"discord": CHAT})
        self.enter_overlay()
        self.leave_overlay()
        self.enter_overlay()
        self.assertIsNone(self.daemon._pending_return)
        self.clock.sleep(self.config.windows.settle * 2)
        self.daemon._tick()
        self.assertIs(self.daemon.state.mode, Mode.OVERLAY)
        self.assertNotIn(("activate", GAME), self.log)

    def test_nothing_is_scheduled_when_there_was_no_window_to_return_to(self):
        self.build(windows={"discord": CHAT}, active=None)
        self.enter_overlay()
        self.leave_overlay()
        self.assertIsNone(self.daemon._pending_return)
        self.assertIsNone(self.reader.tick)


class TestDesktopModeIsUnaffected(DaemonFixture):
    """v0.3 adds a mode; it does not change the one that shipped in v0.2."""

    def test_desktop_mode_remembers_nothing_and_returns_nowhere(self):
        self.build(windows={"discord": CHAT})
        self.daemon._handle(KEY, GUIDE, 1)
        self.daemon._handle(KEY, L3, 1)
        self.assertIs(self.daemon.state.mode, Mode.DESKTOP)
        self.assertIsNone(self.daemon._return_to)

        self.daemon._handle(KEY, GUIDE, 0)
        self.daemon._handle(KEY, GUIDE, 1)
        self.clock.sleep(5.0)
        self.daemon._tick()
        self.assertNotIn("activate", self.kinds())
        self.assertIsNone(self.reader.tick)


class TestParkingTheCursor(DaemonFixture):
    """The closed loop that stands in for a warp nobody exposes."""

    def parking_config(self, **windows):
        return overlay_config(windows={"park_pointer": True, **windows})

    def test_off_by_default(self):
        self.build(windows={"discord": CHAT},
                   geometry=Rect(1000, 0, 400, 300), cursor=(10, 10))
        self.enter_overlay()
        self.assertEqual(self.virtual.moves, [])

    def test_lands_on_the_centre_of_the_overlay(self):
        self.build(self.parking_config(), windows={"discord": CHAT},
                   geometry=Rect(1000, 0, 400, 300), cursor=(0, 0))
        self.enter_overlay()
        self.assertEqual(self.backend.cursor, (1200, 150))

    def test_converges_when_the_session_moves_us_less_than_we_asked(self):
        # Pointer acceleration is not ours to predict, so the loop measures its
        # own gain instead of assuming one.
        self.build(self.parking_config(), windows={"discord": CHAT},
                   virtual_gain=0.5, geometry=Rect(1000, 0, 400, 300))
        self.enter_overlay()
        self.assertEqual(self.backend.cursor, (1200, 150))

    def test_converges_when_the_session_moves_us_further_than_we_asked(self):
        self.build(self.parking_config(), windows={"discord": CHAT},
                   virtual_gain=3.0, geometry=Rect(1000, 0, 400, 300))
        self.enter_overlay()
        self.assertEqual(self.backend.cursor, (1200, 150))

    def test_the_loop_is_bounded(self):
        # A session that ignores us entirely must not spin forever.
        self.build(self.parking_config(), windows={"discord": CHAT},
                   virtual_gain=0.0, geometry=Rect(1000, 0, 400, 300))
        self.enter_overlay()
        self.assertLessEqual(len(self.virtual.moves), daemon_module.WARP_ATTEMPTS)

    def test_the_cursor_goes_back_before_focus_does(self):
        # Restoring after the game has focus would feed it a relative motion,
        # which a game reads as a camera turn.
        self.build(self.parking_config(), windows={"discord": CHAT},
                   geometry=Rect(1000, 0, 400, 300), cursor=(40, 60))
        self.enter_overlay()
        moves_on_entry = len(self.virtual.moves)
        self.leave_overlay()
        self.clock.sleep(self.config.windows.settle)
        # The cursor is back where it started before focus follows it home.
        cursor_when_focus_returned = []
        self.backend.activate_window = lambda window: (
            cursor_when_focus_returned.append(self.backend.cursor),
            self.log.append(("activate", window)),
        )
        self.daemon._tick()
        self.assertGreater(len(self.virtual.moves), moves_on_entry)
        self.assertEqual(self.backend.cursor, (40, 60))
        self.assertEqual(cursor_when_focus_returned, [(40, 60)])

    def test_re_entering_keeps_the_original_cursor_origin(self):
        # The second park must not record the middle of the first overlay as
        # "where the cursor started".
        self.build(self.parking_config(), windows={"discord": CHAT},
                   geometry=Rect(1000, 0, 400, 300), cursor=(40, 60))
        self.enter_overlay()
        self.leave_overlay()
        self.enter_overlay()
        self.leave_overlay()
        self.clock.sleep(self.config.windows.settle)
        self.daemon._tick()
        self.assertEqual(self.backend.cursor, (40, 60))

    def test_a_backend_that_cannot_report_just_skips_parking(self):
        self.build(self.parking_config(), windows={"discord": CHAT})
        self.backend.can_report = False
        self.enter_overlay()
        self.assertEqual(self.virtual.moves, [])
        self.assertIs(self.daemon.state.mode, Mode.OVERLAY)


class TestThroughTheRealLoop(DaemonFixture):
    """One pass through Daemon.run(), to prove the wiring rather than the parts."""

    def test_open_and_return(self):
        script = [
            (KEY, GUIDE, 1),
            (KEY, B, 1),
            (KEY, GUIDE, 0),
            (KEY, GUIDE, 1),
            # The clock only moves when the script says so, so the settle
            # elapses exactly here and nowhere else.
            lambda: self.clock.sleep(1.0),
            None,  # a tick: where the deferred return happens
        ]
        self.build(script=script, windows={"discord": CHAT})

        self.daemon.run()

        self.assertIn("preflight", self.kinds())
        self.assertIn(("activate", CHAT), self.log)
        self.assertIn(("activate", GAME), self.log)
        # The overlay opened, then the pad went back, then the game did.
        self.assertLess(self.log.index(("activate", CHAT)),
                        self.index_of("ungrab"))
        self.assertLess(self.index_of("ungrab"),
                        self.log.index(("activate", GAME)))
        self.assertFalse(self.reader.grabbed)

    def test_a_return_still_settling_at_shutdown_happens_anyway(self):
        # Otherwise SIGTERM during the settle leaves the user looking at the
        # overlay with no daemon left to put them back.
        script = [(KEY, GUIDE, 1), (KEY, B, 1), (KEY, GUIDE, 0), (KEY, GUIDE, 1)]
        self.build(script=script, windows={"discord": CHAT})
        self.daemon.run()
        self.assertIn(("activate", GAME), self.log)
        self.assertLess(self.index_of("ungrab"),
                        self.log.index(("activate", GAME)))

    def test_overlay_bindings_are_kept_out_of_the_action_preflight(self):
        # "overlay.chat" is not an external command, and the action runner
        # would report it as a missing one.
        self.build(script=[])
        self.daemon.run()
        preflight = next(entry for entry in self.log if entry[0] == "preflight")
        self.assertNotIn("B", preflight[1])


if __name__ == "__main__":
    unittest.main()
