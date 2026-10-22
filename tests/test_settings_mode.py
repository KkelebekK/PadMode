"""v0.8 step 2: the control socket, and the pad driving the settings window.

Three layers, as everywhere else here: the socket against a real Unix socket
in a temporary directory; the pure decision function with time passed by hand;
and the daemon wired to fakes, asserting on what was grabbed, sent and
released, in order.
"""

from __future__ import annotations

import os
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from padmode import buttons, control
from padmode.config import parse
from padmode.control import ControlServer, encode, request
from padmode.daemon import (
    WAKE,
    Daemon,
    Grab,
    Mode,
    SettingsOp,
    State,
    decide,
    settings_scroll_steps,
)

KEY, ABS, SYN = buttons.EV_KEY, buttons.EV_ABS, buttons.EV_SYN
FULL = 32767
GUIDE = buttons.code_of("GUIDE")


def code(name: str) -> int:
    return buttons.code_of(name)


# -- the socket -------------------------------------------------------------
class _SocketBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "run" / "padmode.sock"
        self.server = ControlServer(self.path)
        self.assertTrue(self.server.start())

    def tearDown(self):
        self.server.stop()
        self.dir.cleanup()

    def connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.path))
        sock.settimeout(1.0)
        return sock

    def poll(self, want=1):
        out = []
        for _ in range(50):
            out += self.server.poll()
            if len(out) >= want:
                break
            import select
            select.select([self.server.fileno()], [], [], 0.02)
        return out



class SocketTest(_SocketBase):
    def test_only_the_owner_can_reach_it(self):
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_requests_in_answers_out(self):
        sock = self.connect()
        sock.sendall(encode({"op": "reload"}) + encode({"op": "hold"}))
        got = self.poll(2)
        self.assertEqual([m for _, m in got], [{"op": "reload"}, {"op": "hold"}])
        client = got[0][0]
        self.server.send(client, {"event": "held"})
        self.assertEqual(sock.recv(100), b'{"event":"held"}\n')
        sock.close()
        self.assertEqual(self.poll(1), [(client, control.CLOSED)])

    def test_a_line_in_pieces_and_junk_between(self):
        sock = self.connect()
        sock.sendall(b'not json\n{"op": "rel')
        self.assertEqual(self.poll(1), [])
        sock.sendall(b'oad"}\n[1, 2]\n')
        self.assertEqual([m for _, m in self.poll(1)], [{"op": "reload"}])
        sock.close()

    def test_a_stale_socket_is_replaced_a_live_one_is_not(self):
        second = ControlServer(self.path)
        with self.assertLogs("padmode.control", "WARNING"):
            self.assertFalse(second.start())
        self.server.stop()
        self.path.touch()  # what a daemon that died leaves behind
        third = ControlServer(self.path)
        self.assertTrue(third.start())
        third.stop()
        self.server = ControlServer(self.path)
        self.assertTrue(self.server.start())

    def test_request_helper(self):
        def answer():
            for client, message in self.poll(1):
                self.server.send(client, {"reply": message["op"], "ok": True})

        thread = threading.Thread(target=answer)
        thread.start()
        self.assertEqual(request({"op": "reload"}, self.path),
                         {"reply": "reload", "ok": True})
        thread.join()

    def test_nobody_listening(self):
        self.server.stop()
        self.assertIsNone(request({"op": "reload"}, self.path, timeout=0.2))


class LinkTest(_SocketBase):
    """The window's end, against the real server."""

    def test_events_both_ways_and_the_daemon_going_away(self):
        from padmode.settings.link import DaemonLink

        link = DaemonLink(self.path)
        self.assertTrue(link.connect())
        self.assertTrue(link.send({"op": "hold"}))
        client, message = self.poll(1)[0]
        self.assertEqual(message, {"op": "hold"})
        self.server.send(client, {"event": "held"})
        self.server.send(client, {"event": "nav", "dir": "up"})
        import select
        select.select([link.fileno()], [], [], 1)
        events = []
        for _ in range(20):
            events += link.read() or []
            if len(events) == 2:
                break
            select.select([link.fileno()], [], [], 0.05)
        self.assertEqual(events, [{"event": "held"}, {"event": "nav", "dir": "up"}])
        self.server.stop()
        select.select([link.fileno()], [], [], 1)
        self.assertIsNone(link.read())
        self.assertFalse(link.connected)
        self.server = ControlServer(self.path)
        self.server.start()

    def test_no_daemon(self):
        from padmode.settings.link import DaemonLink

        self.server.stop()
        link = DaemonLink(self.path)
        self.assertFalse(link.connect())
        self.assertIsNone(link.reload())
        self.server = ControlServer(self.path)
        self.server.start()


# -- the decisions ----------------------------------------------------------
class DecideTest(unittest.TestCase):
    def setUp(self):
        self.cfg = parse({})
        self.state = State(mode=Mode.SETTINGS)
        self.now = 10.0

    def feed(self, *events):
        ops = []
        last = None
        for ev in events:
            last = decide(*ev, self.state, self.cfg, self.now)
            ops += list(last.settings)
        return ops, last

    def test_face_buttons_are_actions(self):
        ops, _ = self.feed(*[(KEY, code(n), 1) for n in
                             ("A", "B", "LB", "RB", "START", "Y", "X")])
        self.assertEqual([o.value for o in ops],
                         ["select", "back", "page_prev", "page_next", "apply",
                          "reset"])

    def test_the_dpad_steps(self):
        ops, _ = self.feed((ABS, buttons.ABS_HAT0Y, 1), (ABS, buttons.ABS_HAT0Y, 0),
                           (ABS, buttons.ABS_HAT0X, -1))
        self.assertEqual(ops, [SettingsOp("nav", "down"), SettingsOp("nav", "left")])

    def test_the_left_stick_steps_once_per_push(self):
        ops, _ = self.feed((ABS, buttons.ABS_Y, -FULL), (ABS, buttons.ABS_Y, -FULL + 5),
                           (ABS, buttons.ABS_Y, 0))
        self.assertEqual(ops, [SettingsOp("nav", "up")])

    def test_guide_lets_go(self):
        _, d = self.feed((KEY, GUIDE, 1))
        self.assertEqual((d.mode, d.grab), (Mode.IDLE, Grab.RELEASE))
        self.assertIs(self.state.mode, Mode.IDLE)

    def test_capture_a_button_a_trigger_and_the_dpad(self):
        self.state.trigger_axes = {buttons.ABS_RZ: code("RT")}
        for events, name in (([(KEY, code("B"), 1)], "B"),
                             ([(ABS, buttons.ABS_RZ, 255)], "RT"),
                             ([(ABS, buttons.ABS_HAT0X, 1)], "DPAD_RIGHT")):
            self.state.capture = True
            ops, _ = self.feed(*events)
            self.assertEqual(ops, [SettingsOp("captured", name)])
            self.assertFalse(self.state.capture)
            self.state.triggers.clear()

    def test_capture_a_stick_direction_on_the_way_home(self):
        self.state.capture = True
        ops, _ = self.feed((ABS, buttons.ABS_RX, -FULL), (ABS, buttons.ABS_RY, FULL),
                           (SYN, 0, 0))
        self.assertEqual(ops, [])  # still out there
        ops, _ = self.feed((ABS, buttons.ABS_RX, 0), (ABS, buttons.ABS_RY, 0),
                           (SYN, 0, 0))
        self.assertEqual(ops, [SettingsOp("captured", "RS_DOWN_LEFT")])

    def test_capture_does_nothing_else(self):
        self.state.capture = True
        ops, _ = self.feed((ABS, buttons.ABS_Y, -FULL))
        self.assertEqual(ops, [])
        self.assertIsNone(self.state.osk_nav)

    def test_guide_cancels_a_capture_and_stays(self):
        self.state.capture = True
        ops, d = self.feed((KEY, GUIDE, 1))
        self.assertEqual(ops, [SettingsOp("cancelled")])
        self.assertIs(self.state.mode, Mode.SETTINGS)
        self.assertIsNone(d.grab)

    def test_the_right_stick_scrolls_at_desktop_speed(self):
        self.feed((ABS, buttons.ABS_RY, FULL))
        steps = sum(settings_scroll_steps(self.state, self.cfg, 1 / 60)
                    for _ in range(60))
        # Ten a second; the last may still be in the accumulator.
        self.assertIn(steps, (9, 10))
        self.feed((ABS, buttons.ABS_RY, 0))
        self.assertEqual(settings_scroll_steps(self.state, self.cfg, 1 / 60), 0)


class OpenSettingsTest(unittest.TestCase):
    def test_guide_r3_asks_for_the_window(self):
        cfg, state = parse({}), State()
        decide(KEY, GUIDE, 1, state, cfg)
        d = decide(KEY, code("R3"), 1, state, cfg)
        self.assertTrue(d.open_settings)
        self.assertIs(state.mode, Mode.GUIDE_HELD)  # the window takes the pad

    def test_rebindable(self):
        cfg = parse({"bindings": {"R3": False, "START": "settings"}})
        state = State()
        decide(KEY, GUIDE, 1, state, cfg)
        self.assertFalse(decide(KEY, code("R3"), 1, state, cfg).open_settings)
        self.assertTrue(decide(KEY, code("START"), 1, state, cfg).open_settings)


# -- the daemon, against fakes ----------------------------------------------
class FakeControl:
    """Requests handed out one WAKE at a time; what was sent, recorded."""

    def __init__(self, log, script) -> None:
        self.log = log
        self.script = list(script)
        self.sent: list[tuple[int, dict]] = []

    def start(self) -> bool:
        return True

    def fileno(self):
        return 99

    def poll(self):
        return [self.script.pop(0)] if self.script else []

    def send(self, client, message) -> None:
        self.sent.append((client, message))
        self.log.append(("send", message.get("event") or message.get("reply")))

    def stop(self) -> None:
        pass


class FakeReader:
    def __init__(self, log, script) -> None:
        self.log, self.script = log, script
        self.axis_ranges, self.trigger_axes = {}, {}
        self.tick = None

    def watch(self, fd):
        pass

    def unwatch(self, fd):
        pass

    def grab(self):
        self.log.append(("grab",))

    def ungrab(self):
        self.log.append(("ungrab",))

    def set_tick(self, interval):
        self.tick = interval

    def events(self):
        yield from self.script
        self.log.append(("end",))


class FakeRunner:
    def __init__(self):
        self.contexts = []

    def preflight(self, bindings):
        pass

    def available(self, binary):
        return True

    def run(self, binding):
        return True

    def set_context(self, ctx):
        self.contexts.append(ctx)


def run(script, requests, config_text="", path=None):
    log: list = []
    reader = FakeReader(log, script)
    ctl = FakeControl(log, requests)
    runner = FakeRunner()
    daemon = Daemon(parse({}), reader, runner, control=ctl, config_path=path)
    with unittest.TestCase().assertLogs("padmode", "INFO"):
        daemon.run()
    return daemon, log[:log.index(("end",))], ctl, runner


class DaemonTest(unittest.TestCase):
    def test_hold_drive_and_let_go(self):
        daemon, log, ctl, _ = run(
            [WAKE, (KEY, code("RB"), 1), (KEY, GUIDE, 1), (KEY, code("RB"), 1)],
            [(1, {"op": "hold"})],
        )
        self.assertEqual(log, [("grab",), ("send", "held"), ("send", "act"),
                               ("send", "released"), ("ungrab",)])
        self.assertEqual(ctl.sent[1], (1, {"event": "act", "action": "page_next"}))
        self.assertIs(daemon.state.mode, Mode.IDLE)

    def test_the_window_closing_lets_go(self):
        _, log, _, _ = run([WAKE, WAKE], [(1, {"op": "hold"}), (1, control.CLOSED)])
        self.assertEqual(log, [("grab",), ("send", "held"), ("send", "released"),
                               ("ungrab",)])

    def test_another_connection_closing_does_not(self):
        daemon, log, _, _ = run([WAKE, WAKE],
                                [(1, {"op": "hold"}), (2, control.CLOSED)])
        self.assertIs(daemon.state.mode, Mode.SETTINGS)

    def test_press_to_assign(self):
        _, _, ctl, _ = run(
            [WAKE, WAKE, (KEY, code("LB"), 1), (KEY, code("LB"), 1)],
            [(1, {"op": "hold"}), (1, {"op": "capture"})],
        )
        self.assertEqual([m for _, m in ctl.sent],
                         [{"event": "held"},
                          {"event": "captured", "input": "LB"},
                          {"event": "act", "action": "page_prev"}])

    def test_capture_only_for_the_window_holding_the_pad(self):
        daemon, _, _, _ = run([WAKE, WAKE], [(1, {"op": "hold"}),
                                             (2, {"op": "capture"})])
        self.assertFalse(daemon.state.capture)

    def test_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[general]\nvolume_step = 9\n[device]\nname = \"X\"\n")
            daemon, _, ctl, runner = run([WAKE], [(1, {"op": "reload"})], path=path)
            self.assertEqual(ctl.sent[0][1], {"reply": "reload", "ok": True,
                                              "error": None,
                                              "restart": ["device"]})
            self.assertEqual(daemon._config.volume_step, 9)
            self.assertEqual(runner.contexts[0].volume_step, 9)

            path.write_text('[bindings]\nRB = "no.such.thing"\n')
            daemon2, _, ctl2, _ = run([WAKE], [(1, {"op": "reload"})], path=path)
            reply = ctl2.sent[0][1]
            self.assertFalse(reply["ok"])
            self.assertIn("no.such.thing", reply["error"])
            self.assertEqual(daemon2._config, parse({}))


class LockDuringSettingsTest(unittest.TestCase):
    def test_locking_takes_the_pad_from_the_window(self):
        from tests.test_unlock import FakeKeypad, FakeWatcher

        log: list = []
        reader = FakeReader(log, [WAKE, WAKE, (KEY, code("X"), 1)])
        ctl = FakeControl(log, [(1, {"op": "hold"})])
        watcher = FakeWatcher([False, True])
        daemon = Daemon(parse({"unlock": {"enabled": True}}), reader, FakeRunner(),
                        lock=watcher, keypad=FakeKeypad(log), control=ctl)
        with self.assertLogs("padmode", "INFO"):
            daemon.run()
        log = log[:log.index(("end",))]
        self.assertEqual(log, [
            ("grab",), ("send", "held"),             # the window has the pad
            ("send", "released"),                    # the lock takes it
            ("keypad", "open"), ("grab",),
            ("tap", buttons.KEYS["9"]),              # X types 9, not "reset"
        ])
        self.assertIs(daemon.state.mode, Mode.UNLOCK)
        # Asking again while locked is refused.
        ctl.script = [(1, {"op": "hold"})]
        daemon._pump_control()
        self.assertEqual(ctl.sent[-1][1]["event"], "refused")


if __name__ == "__main__":
    unittest.main()
