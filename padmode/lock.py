"""Whether the screen is locked, from the screensaver's D-Bus signal.

The first half of the unlock module (v0.7, ``docs/virtual-input.md``): it only
*knows* whether the screen is locked. Typing digits at the lock screen is built
on top of this and lives elsewhere.

Stdlib only, like everything on the daemon's import path. There is no D-Bus
binding in the standard library and §2 forbids adding one for this, so the bus
is reached the same way ``playerctl`` and ``kdotool`` are: an external command.
``gdbus`` ships with GLib, which every desktop this runs on already has.
``gdbus monitor`` is one long-lived process whose stdout is a line per signal;
the daemon's event loop selects on that pipe next to the gamepad, so nothing
here ever polls.

Measured on Plasma 6.7 (KWin owns the name):

* ``ActiveChanged`` is on **one interface at two object paths** --
  ``/ScreenSaver`` and ``/org/freedesktop/ScreenSaver`` -- so subscribing to
  everything the name emits hears every lock twice. ``org.kde.screensaver``
  carries ``AboutToLock`` and nothing else. We watch ``/ScreenSaver`` only, and
  still act on *changes* rather than signals, so a desktop that does emit twice
  is harmless.
* ``gdbus monitor --dest`` filters on the sender, which means another client
  on the session bus cannot fake a lock by emitting the signal itself. That
  matters more here than anywhere else in padmode: "locked" is what will turn
  the digit keyboard on.

Two rules from the spec:

* **Ask at startup.** A daemon started after the screen locked -- exactly what
  happens on a boot that autologins and then locks -- would otherwise never
  learn it. We ask when ``gdbus`` reports that the name has an owner, which it
  does only after its signal subscription is in place, so no lock can fall
  between the question and the first signal.
* **Degrade gracefully.** No ``gdbus``, no screensaver service, or the monitor
  dying all mean the module is off and says so once. Whenever the answer is
  unknown, the answer is "unlocked": the safe direction, since unlocked is the
  state in which nothing extra is live.
"""

from __future__ import annotations

import logging
import os
import select
import shutil
import subprocess

log = logging.getLogger(__name__)

TOOL = "gdbus"
SERVICE = "org.freedesktop.ScreenSaver"
#: The one of the two object paths we listen on (see the module docstring).
OBJECT_PATH = "/ScreenSaver"
INTERFACE = "org.freedesktop.ScreenSaver"

#: How long to wait for ``GetActive``. It runs inside the event loop, so a
#: hung compositor must not be able to stall the pad for long; on a warm system
#: the call takes a few milliseconds.
QUERY_TIMEOUT = 1.0

_SIGNAL = f"{INTERFACE}.ActiveChanged"


def parse_signal(line: str) -> bool | None:
    """The lock state carried by one ``gdbus monitor`` line, or None.

    A signal looks like ``/ScreenSaver: org.freedesktop.ScreenSaver.ActiveChanged
    (true,)``. Everything else ``gdbus`` prints -- the banner, owner changes,
    other signals on the same object -- is None here.
    """
    path, sep, rest = line.strip().partition(": ")
    if not sep or path != OBJECT_PATH:
        return None
    name, _, args = rest.partition(" ")
    if name != _SIGNAL:
        return None
    return parse_bool(args)


def parse_bool(text: str) -> bool | None:
    """``(true,)`` -> True, ``(false,)`` -> False, anything else -> None.

    The GVariant text form of a one-boolean tuple, which is what both
    ``gdbus call`` and ``gdbus monitor`` print.
    """
    value = text.strip()
    if value == "(true,)":
        return True
    if value == "(false,)":
        return False
    return None


def owner_change(line: str) -> bool | None:
    """True if the name just gained an owner, False if it lost one, else None.

    ``gdbus monitor`` prints one of these when it starts, and again whenever
    the service comes or goes -- a compositor restart, or a daemon that started
    before the session finished coming up.
    """
    text = line.strip()
    if not text.startswith(f"The name {SERVICE} "):
        return None
    if " is owned by " in text:
        return True
    if text.endswith("does not have an owner"):
        return False
    return None


class LockWatcher:
    """A ``gdbus monitor`` process, or the absence of one.

    Same convention as :class:`~padmode.osk.client.OskClient`: :meth:`start`
    returns False when the feature cannot run, and nothing else ever fails
    because of it.
    """

    def __init__(
        self,
        spawn=subprocess.Popen,
        run=subprocess.run,
        which=shutil.which,
    ) -> None:
        self._spawn = spawn
        self._run = run
        self._which = which
        self._proc = None
        self._buffer = b""
        self._warned_owner = False
        #: Whether the screen is locked, as far as we know. False whenever we
        #: do not know (see the module docstring).
        self.locked = False

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def fileno(self) -> int | None:
        """The pipe to select on, or None when there is nothing to watch."""
        if self._proc is None or self._proc.stdout is None:
            return None
        return self._proc.stdout.fileno()

    def start(self) -> bool:
        """Spawn the monitor. Returns False, having said why, if it cannot run."""
        if self.alive:
            return True
        if self._which(TOOL) is None:
            log.warning(
                "unlock: %s not found, so the lock screen cannot be noticed; "
                "unlock is off (it ships with GLib)", TOOL,
            )
            return False
        try:
            self._proc = self._spawn(
                [TOOL, "monitor", "--session", "--dest", SERVICE,
                 "--object-path", OBJECT_PATH],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("unlock: could not start %s monitor (%s); unlock is off",
                        TOOL, exc)
            self._proc = None
            return False
        return True

    def poll(self) -> list[bool]:
        """Lock states entered since last time, oldest first. Never blocks.

        Only *changes* are reported: a signal repeating the state we already
        hold is dropped here, so the caller can treat every entry as an edge.
        """
        fd = self.fileno()
        if fd is None:
            return []
        changes: list[bool] = []
        while True:
            try:
                readable = select.select([fd], [], [], 0)[0]
            except (OSError, ValueError):
                self._died("its pipe broke")
                break
            if not readable:
                break
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                self._died("it exited")
                break
            self._buffer += data
            while b"\n" in self._buffer:
                raw, _, self._buffer = self._buffer.partition(b"\n")
                state = self._interpret(raw.decode("utf-8", "replace"))
                if state is not None and state != self.locked:
                    self.locked = state
                    changes.append(state)
        # Dying while locked is a change too: the safe answer is "unlocked",
        # and the caller has to hear about it to tear down what it built.
        if self._proc is None and self.locked:
            self.locked = False
            changes.append(False)
        return changes

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        self._buffer = b""
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()

    # -- internals -----------------------------------------------------
    def _interpret(self, line: str) -> bool | None:
        state = parse_signal(line)
        if state is not None:
            return state
        owned = owner_change(line)
        if owned is True:
            self._warned_owner = False
            return self._query()
        if owned is False:
            if not self._warned_owner:
                log.info(
                    "unlock: nothing provides %s right now; treating the "
                    "screen as unlocked until something does", SERVICE,
                )
                self._warned_owner = True
            return False
        return None

    def _query(self) -> bool | None:
        """``GetActive``, or None if the answer did not come back."""
        try:
            result = self._run(
                [TOOL, "call", "--session", "--dest", SERVICE,
                 "--object-path", OBJECT_PATH,
                 "--method", f"{INTERFACE}.GetActive"],
                capture_output=True, text=True, timeout=QUERY_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("unlock: could not ask whether the screen is locked (%s)",
                        exc)
            return None
        if result.returncode != 0:
            log.warning("unlock: GetActive failed: %s",
                        (result.stderr or "").strip() or result.returncode)
            return None
        return parse_bool(result.stdout)

    def _died(self, why: str) -> None:
        log.warning("unlock: the %s monitor stopped (%s); unlock is off until "
                    "padmode restarts", TOOL, why)
        self.stop()
