"""The window's end of the control socket (:mod:`padmode.control`).

One long-lived connection for the pad -- holding it, and hearing what it does
-- because those events arrive unasked. Apply's reload goes over a separate,
one-shot request instead (:func:`padmode.control.request`), so its answer can
never be confused with a pad event arriving at the same moment.

Stdlib only; the window plugs :meth:`DaemonLink.fileno` into GLib.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from .. import control


class DaemonLink:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or control.socket_path()
        self._sock: socket.socket | None = None
        self._buffer = b""

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> bool:
        if self._sock is not None:
            return True
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self._path))
        except OSError:
            sock.close()
            return False
        sock.setblocking(False)
        self._sock = sock
        self._buffer = b""
        return True

    def fileno(self) -> int | None:
        return self._sock.fileno() if self._sock is not None else None

    def send(self, message: dict) -> bool:
        if self._sock is None:
            return False
        try:
            self._sock.sendall(control.encode(message))
            return True
        except OSError:
            self.close()
            return False

    def read(self) -> list[dict] | None:
        """Events received since last time; None once the daemon has gone."""
        if self._sock is None:
            return None
        while True:
            try:
                data = self._sock.recv(65536)
            except BlockingIOError:
                break
            except OSError:
                data = b""
            if not data:
                self.close()
                return None
            self._buffer += data
        events = []
        while b"\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition(b"\n")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                events.append(message)
        return events

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def reload(self) -> dict | None:
        """Ask the daemon to re-read the config. None if it is not running."""
        return control.request({"op": "reload"}, self._path)
