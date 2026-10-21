"""The lock watcher: what it reads, what it ignores, and how it fails.

No bus here. ``gdbus`` is replaced by a real pipe the test writes into, so
the reading -- select, partial lines, EOF -- is the code that runs in the
daemon, not a stand-in for it. The lines are the ones ``gdbus monitor``
printed on Plasma 6.7.
"""

from __future__ import annotations

import os
import subprocess
import unittest
import unittest.mock

from padmode import lock
from padmode.config import ConfigError, parse
from padmode.daemon import WAKE, Daemon
from padmode.lock import LockWatcher, owner_change, parse_bool, parse_signal

BANNER = ("Monitoring signals on object /ScreenSaver owned by "
          "org.freedesktop.ScreenSaver\n")
OWNED = "The name org.freedesktop.ScreenSaver is owned by :1.10\n"
UNOWNED = "The name org.freedesktop.ScreenSaver does not have an owner\n"
LOCKED = "/ScreenSaver: org.freedesktop.ScreenSaver.ActiveChanged (true,)\n"
UNLOCKED = "/ScreenSaver: org.freedesktop.ScreenSaver.ActiveChanged (false,)\n"


class ParseTest(unittest.TestCase):
    def test_signal(self):
        self.assertIs(parse_signal(LOCKED), True)
        self.assertIs(parse_signal(UNLOCKED), False)

    def test_the_second_object_path_is_not_ours(self):
        # KDE emits the same signal here too; listening to both is what would
        # hear every lock twice.
        self.assertIsNone(parse_signal(
            "/org/freedesktop/ScreenSaver: "
            "org.freedesktop.ScreenSaver.ActiveChanged (true,)"
        ))

    def test_other_lines(self):
        for line in (BANNER, OWNED, UNOWNED, "",
                     "/ScreenSaver: org.kde.screensaver.AboutToLock ()",
                     "/ScreenSaver: org.freedesktop.ScreenSaver.ActiveChanged"):
            self.assertIsNone(parse_signal(line), line)

    def test_bool(self):
        self.assertIs(parse_bool("(true,)\n"), True)
        self.assertIs(parse_bool("(false,)"), False)
        self.assertIsNone(parse_bool("(uint32 3,)"))

    def test_owner(self):
        self.assertIs(owner_change(OWNED), True)
        self.assertIs(owner_change(UNOWNED), False)
        self.assertIsNone(owner_change(BANNER))
        self.assertIsNone(owner_change(
            "The name org.example.Other is owned by :1.2"))


class FakeMonitor:
    """A ``gdbus monitor`` process whose stdout is a pipe we write into."""

    def __init__(self) -> None:
        read_fd, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.returncode = None
        self.terminated = False

    def say(self, *lines: str) -> None:
        os.write(self.write_fd, "".join(lines).encode())

    def exit(self) -> None:
        os.close(self.write_fd)
        self.returncode = 0

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.exit()

    def wait(self, timeout=None):
        return self.returncode

    def kill(self) -> None:
        self.terminate()


class FakeCall:
    """``subprocess.run`` for ``gdbus call``: answers GetActive."""

    def __init__(self, answer: str = "(false,)\n", returncode: int = 0) -> None:
        self.answer = answer
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return subprocess.CompletedProcess(argv, self.returncode,
                                           self.answer, "boom")


def watcher(answer="(false,)\n", returncode=0, tool="/usr/bin/gdbus"):
    monitor = FakeMonitor()
    call = FakeCall(answer, returncode)
    spawned: list[list[str]] = []

    def spawn(argv, **kwargs):
        spawned.append(argv)
        return monitor

    w = LockWatcher(spawn=spawn, run=call, which=lambda name: tool)
    return w, monitor, call, spawned


class WatcherTest(unittest.TestCase):
    def test_listens_to_one_path_of_one_sender(self):
        w, monitor, _, spawned = watcher()
        self.assertTrue(w.start())
        argv = spawned[0]
        self.assertEqual(argv[argv.index("--dest") + 1], lock.SERVICE)
        self.assertEqual(argv[argv.index("--object-path") + 1], "/ScreenSaver")
        w.stop()

    def test_no_gdbus_means_off(self):
        w, monitor, _, spawned = watcher(tool=None)
        with self.assertLogs("padmode.lock", "WARNING"):
            self.assertFalse(w.start())
        self.assertEqual(spawned, [])
        self.assertIsNone(w.fileno())
        self.assertEqual(w.poll(), [])
        monitor.exit()
        monitor.stdout.close()

    def test_asks_once_the_name_has_an_owner(self):
        w, monitor, call, _ = watcher(answer="(true,)\n")
        w.start()
        monitor.say(BANNER)
        self.assertEqual(w.poll(), [])
        self.assertEqual(call.calls, [])
        monitor.say(OWNED)
        # Already locked when we started: learnt from the question, not from
        # a signal that is never going to come.
        self.assertEqual(w.poll(), [True])
        self.assertTrue(w.locked)
        self.assertIn("org.freedesktop.ScreenSaver.GetActive", call.calls[0])
        w.stop()

    def test_reports_changes_not_signals(self):
        w, monitor, _, _ = watcher()
        w.start()
        monitor.say(BANNER, OWNED)
        self.assertEqual(w.poll(), [])  # unlocked, as we assumed
        monitor.say(LOCKED, LOCKED)
        self.assertEqual(w.poll(), [True])
        monitor.say(UNLOCKED)
        self.assertEqual(w.poll(), [False])
        self.assertEqual(w.poll(), [])
        w.stop()

    def test_a_line_split_across_reads(self):
        w, monitor, _, _ = watcher()
        w.start()
        monitor.say(OWNED, LOCKED[:20])
        self.assertEqual(w.poll(), [])
        monitor.say(LOCKED[20:])
        self.assertEqual(w.poll(), [True])
        w.stop()

    def test_an_unanswered_question_changes_nothing(self):
        for answer, code, says in (
            (subprocess.TimeoutExpired("gdbus", 1.0), 0, True),
            ("", 1, True),
            ("(uint32 0,)", 0, False),
        ):
            w, monitor, _, _ = watcher(answer=answer, returncode=code)
            w.start()
            monitor.say(OWNED)
            with (self.assertLogs("padmode.lock", "WARNING") if says
                  else self.assertNoLogs("padmode.lock", "WARNING")):
                self.assertEqual(w.poll(), [])
            self.assertFalse(w.locked)
            w.stop()

    def test_losing_the_service_counts_as_unlocked(self):
        w, monitor, _, _ = watcher()
        w.start()
        monitor.say(OWNED, LOCKED)
        self.assertEqual(w.poll(), [True])
        monitor.say(UNOWNED)
        with self.assertLogs("padmode.lock", "INFO"):
            self.assertEqual(w.poll(), [False])
        w.stop()

    def test_dying_while_locked_reports_unlocked(self):
        w, monitor, _, _ = watcher()
        w.start()
        monitor.say(OWNED, LOCKED)
        self.assertEqual(w.poll(), [True])
        monitor.exit()
        with self.assertLogs("padmode.lock", "WARNING"):
            self.assertEqual(w.poll(), [False])
        self.assertIsNone(w.fileno())
        self.assertFalse(w.locked)


class ReaderWatchTest(unittest.TestCase):
    """The real reader's loop, with a pipe standing in for the pad."""

    def test_a_watched_pipe_wakes_the_loop(self):
        from padmode.input import reader as reader_module

        pad_r, pad_w = os.pipe()
        other_r, other_w = os.pipe()
        device = unittest.mock.Mock()
        device.fileno.return_value = pad_r
        device.read.return_value = []
        reader = reader_module.GamepadReader()
        reader.watch(other_r)
        loop = reader._pump(device)
        try:
            os.write(other_w, b"x")
            self.assertEqual(next(loop), reader_module.WAKE)
            device.read.assert_not_called()  # the pad had nothing to say
            # Dropped mid-loop: the pipe is still readable, so if it stayed
            # registered the next select would wake for it again.
            reader.unwatch(other_r)
            reader.set_tick(0.001)
            self.assertIs(next(loop), reader_module.TICK)
        finally:
            loop.close()
            for fd in (pad_r, pad_w, other_r, other_w):
                os.close(fd)


class FakeReader:
    def __init__(self, script) -> None:
        self.script = script
        self.watched: list[tuple[str, int]] = []
        self.axis_ranges = {}
        self.trigger_axes = {}

    def watch(self, fd) -> None:
        self.watched.append(("watch", fd))

    def unwatch(self, fd) -> None:
        self.watched.append(("unwatch", fd))

    def ungrab(self) -> None:
        pass

    def set_tick(self, interval) -> None:
        pass

    def events(self):
        for item in self.script:
            if callable(item):
                item()
                continue
            yield item


class FakeRunner:
    def preflight(self, bindings) -> None:
        pass

    def available(self, binary) -> bool:
        return True

    def run(self, binding) -> bool:
        return True


class DaemonTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(parse({}).unlock.enabled)
        self.assertTrue(parse({"unlock": {"enabled": True}}).unlock.enabled)
        with self.assertRaises(ConfigError):
            parse({"unlock": {"enabled": "yes"}})
        with self.assertRaises(ConfigError):
            parse({"unlock": {"pin": "1234"}})

    def test_follows_the_lock_screen_and_lets_go_when_the_monitor_dies(self):
        w, monitor, _, _ = watcher()
        seen: list[bool] = []
        reader = FakeReader([
            lambda: monitor.say(OWNED, LOCKED), WAKE,
            lambda: seen.append(daemon.state.locked),
            lambda: monitor.say(UNLOCKED), WAKE,
            lambda: seen.append(daemon.state.locked),
            lambda: monitor.say(LOCKED), WAKE,
            monitor.exit, WAKE,
            lambda: seen.append(daemon.state.locked),
        ])
        daemon = Daemon(parse({"unlock": {"enabled": True}}), reader,
                        FakeRunner(), lock=w)
        with self.assertLogs("padmode", "INFO") as logs:
            daemon.run()
        self.assertEqual(seen, [True, False, False])
        fd = reader.watched[0][1]
        self.assertEqual(reader.watched, [("watch", fd), ("unwatch", fd)])
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(
            [m for m in messages if m.startswith("screen ")],
            ["screen locked", "screen unlocked", "screen locked",
             "screen unlocked"],
        )

    def test_without_a_watcher_nothing_is_watched(self):
        reader = FakeReader([WAKE])
        daemon = Daemon(parse({}), reader, FakeRunner())
        with self.assertLogs("padmode", "INFO"):
            daemon.run()
        self.assertEqual(reader.watched, [])
        self.assertFalse(daemon.state.locked)


if __name__ == "__main__":
    unittest.main()
