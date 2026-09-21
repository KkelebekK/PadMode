"""The daemon's end of the keyboard channel: spawn it, talk to it, bury it.

Stdlib only. This module is the *only* thing the daemon imports from the ``osk``
package besides :mod:`~padmode.osk.protocol`; the renderer and its GTK
dependency live behind ``python -m padmode.osk`` and are never imported here
(CLAUDE.md §2).

The keyboard is an optional feature, probed like ``playerctl``: if
gtk4-layer-shell is missing, :meth:`OskClient.start` says so once and returns
False. That disables the keyboard. It never stops the daemon starting, and it
never leaves the daemon in ``Mode.OSK`` -- the caller checks the return.
"""

from __future__ import annotations

import logging
import os
import select
import subprocess
import sys
from pathlib import Path

from . import preload_library, protocol

log = logging.getLogger(__name__)

#: How long to wait for the renderer to go away before killing it.
STOP_TIMEOUT = 1.5


class OskClient:
    """A renderer process, or the absence of one."""

    def __init__(self, config, spawn=subprocess.Popen) -> None:
        self._config = config
        self._spawn = spawn
        self._proc = None
        self._buffer = b""
        self._warned = False
        #: True once the renderer says it is up and layer-shell worked.
        self.ready = False

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- lifecycle -----------------------------------------------------
    def start(self) -> bool:
        """Spawn the renderer, unmapped. Returns False if it cannot run.

        Called on entering a pad-driven mode rather than when the keyboard is
        first asked for: measured, the first show costs ~105 ms of GTK renderer
        setup and every show after it 0.6 ms, so paying that once while the
        user is already moving the pointer is the difference between a keyboard
        that appears and one that lands.
        """
        if self.alive:
            return True
        if not self._config.enabled:
            return False

        preload = self._config.preload or preload_library()
        if preload is None:
            if not self._warned:
                self._warned = True
                log.info(
                    "on-screen keyboard disabled: gtk4-layer-shell not found. "
                    "Install it (Arch: gtk4-layer-shell) or set osk.preload."
                )
            return False

        env = dict(os.environ)
        # gtk4-layer-shell cannot hook GDK when PyGObject dlopens it late, so
        # the renderer only gets a layer surface if it is preloaded. Measured
        # on Plasma 6.7 / GTK 4.22; see padmode.osk.preload_library.
        env["LD_PRELOAD"] = preload
        # Make `python -m padmode.osk` resolve whether we were started from a
        # checkout or an installed package.
        root = str(Path(__file__).resolve().parent.parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (root, env.get("PYTHONPATH")) if part
        )

        argv = [sys.executable, "-m", "padmode.osk", f"--height={self._config.height:g}"]
        if self._config.position == "top":
            argv.append("--top")
        if self._config.auto_open:
            # Only then does the renderer connect as an accessibility client,
            # so a user who does not want the feature does not pay for it.
            argv.append("--watch-focus")

        try:
            # bufsize=0: we read the raw fd ourselves. A buffered reader would
            # pull several messages into Python's buffer at once, and then
            # select() reports the fd as not ready while unread messages are
            # already sitting there -- which silently loses keystrokes.
            self._proc = self._spawn(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                env=env, bufsize=0,
            )
        except OSError as exc:
            log.warning("cannot start the on-screen keyboard: %s", exc)
            self._proc = None
            return False

        self._buffer = b""
        self.ready = False
        return True

    def stop(self) -> None:
        """Terminate the renderer. Safe to call when there isn't one."""
        proc, self._proc = self._proc, None
        self.ready = False
        self._buffer = b""
        if proc is None:
            return
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()

    # -- sending -------------------------------------------------------
    def show(self, hints: dict[str, str]) -> None:
        self._send(protocol.encode(protocol.SHOW, hints=hints))

    def hide(self) -> None:
        self._send(protocol.encode(protocol.HIDE))

    def ask_focus(self) -> None:
        """Ask what has focus now, for the case no focus *change* happened."""
        self._send(protocol.encode(protocol.ASK_FOCUS))

    def step(self, direction: str) -> None:
        self._send(protocol.encode(protocol.STEP, dir=direction))

    def move(self, dx: float, dy: float) -> None:
        """Slide the panel. Sent on the tick, so only when it is really moving.

        Rounded to a tenth of a pixel: the surface accumulates the remainder
        anyway, and it keeps the tick's worth of JSON short.
        """
        self._send(protocol.encode(protocol.MOVE, dx=round(dx, 1), dy=round(dy, 1)))

    def act(self, name: str) -> None:
        self._send(protocol.encode(protocol.ACT, name=name))

    def _send(self, payload: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(payload)
        except (BrokenPipeError, OSError, ValueError):
            # It died between our last poll and now. Reaping it here keeps the
            # daemon's view honest; the caller finds out via `alive`.
            self.stop()

    # -- receiving -----------------------------------------------------
    def poll(self) -> list[tuple[str, dict]]:
        """Whatever the renderer has said since last time. Never blocks."""
        if self._proc is None or self._proc.stdout is None:
            return []
        fd = self._proc.stdout.fileno()
        messages: list[tuple[str, dict]] = []
        while True:
            try:
                readable = select.select([fd], [], [], 0)[0]
            except (OSError, ValueError):
                self.stop()
                break
            if not readable:
                break
            try:
                data = os.read(fd, 65536)
            except (OSError, ValueError):
                data = b""
            if not data:
                # EOF: the renderer is gone. This is the whole liveness
                # mechanism -- no watchdog, no timeout (docs/on-screen-keyboard
                # risk 4).
                self.stop()
                break
            self._buffer += data
            while b"\n" in self._buffer:
                line, _, self._buffer = self._buffer.partition(b"\n")
                parsed = protocol.decode(line)
                if parsed is None:
                    continue
                if parsed[0] == protocol.READY:
                    self.ready = True
                messages.append(parsed)
        return messages
