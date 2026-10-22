"""The daemon's control socket: how the settings window talks to it (v0.8).

A Unix socket in ``$XDG_RUNTIME_DIR``, one JSON object per line each way --
the keyboard's pipe protocol, over a socket because the window is not the
daemon's child: it can be started from the application menu. Stdlib only,
like everything on the daemon's import path.

Requests, window to daemon:

``{"op": "reload"}``
    Re-read the config file (Apply). Answered with ``{"reply": "reload",
    "ok": ..., "error": ..., "restart": [...]}``: what failed, or which changed
    settings only take effect after a restart.
``{"op": "hold"}`` / ``{"op": "release"}``
    Take the pad for the window (``Mode.SETTINGS``), or hand it back.
    Answered with ``{"event": "held"}``, ``{"event": "refused", "reason":
    ...}`` or ``{"event": "released"}``. Closing the connection releases too.
``{"op": "capture"}`` / ``{"op": "capture", "cancel": true}``
    The next input pressed is reported, not acted on: ``{"event":
    "captured", "input": "RB"}``. Guide cancels: ``{"event": "cancelled"}``.
``{"op": "keyboard"}``
    Open the on-screen keyboard over the window, for a text field.

While the window holds the pad it is sent what the pad does, already digested
the way the keyboard's grid is (``docs/on-screen-keyboard.md``):
``{"event": "nav", "dir": "up"}``, ``{"event": "act", "action": "select"}``,
``{"event": "scroll", "amount": -1}``.

**Only the same user gets in.** The socket lives in a directory only that user
can enter, is created mode 0600, and every connection's peer is checked with
``SO_PEERCRED`` besides. Anyone who can reach it can take the pad, so the belt
and the braces are both worth having.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import struct
from pathlib import Path

log = logging.getLogger(__name__)

SOCKET_NAME = "padmode.sock"

#: A line longer than this is not a request we sent; drop the connection.
MAX_LINE = 64 * 1024

#: What the pad does in the settings window, button by button (CLAUDE.md §3.5).
SETTINGS_BUTTONS: dict[str, str] = {
    "A": "select",
    "B": "back",
    "LB": "page_prev",
    "RB": "page_next",
    "START": "apply",
    "Y": "reset",
}

#: Sent in place of a request when a connection goes away.
CLOSED = {"op": "_closed"}


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / SOCKET_NAME
    return Path(f"/tmp/padmode-{os.getuid()}") / SOCKET_NAME


def encode(message: dict) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode()


class ControlServer:
    """The listening socket and its clients, behind one descriptor.

    Everything is registered in one epoll set, and the epoll descriptor is what
    the reader selects on beside the pad -- it reads as ready whenever anything
    inside it is. So the daemon watches a single descriptor however many
    windows connect.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or socket_path()
        self._listener: socket.socket | None = None
        self._epoll: select.epoll | None = None
        self._clients: dict[int, socket.socket] = {}
        self._buffers: dict[int, bytes] = {}

    @property
    def path(self) -> Path:
        return self._path

    def fileno(self) -> int | None:
        return self._epoll.fileno() if self._epoll is not None else None

    def start(self) -> bool:
        """Listen. False, having said why, if another daemon already is."""
        if self._listener is not None:
            return True
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._path.exists():
                if _answers(self._path):
                    log.warning("control: another padmode is listening on %s; "
                                "the settings window will talk to that one",
                                self._path)
                    return False
                self._path.unlink()  # left behind by a daemon that died
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old_umask = os.umask(0o177)
            try:
                listener.bind(str(self._path))
            finally:
                os.umask(old_umask)
            listener.listen(4)
            listener.setblocking(False)
        except OSError as exc:
            log.warning("control: cannot listen on %s (%s); the settings "
                        "window cannot reach this daemon", self._path, exc)
            return False
        self._listener = listener
        self._epoll = select.epoll()
        self._epoll.register(listener.fileno(), select.EPOLLIN)
        return True

    def poll(self) -> list[tuple[int, dict]]:
        """``(client, request)`` pairs since last time, oldest first.

        Never blocks. A connection that closed yields :data:`CLOSED` once.
        """
        if self._epoll is None or self._listener is None:
            return []
        out: list[tuple[int, dict]] = []
        for fd, _ in self._epoll.poll(0):
            if fd == self._listener.fileno():
                self._accept()
                continue
            out.extend(self._read(fd))
        return out

    def send(self, client: int, message: dict) -> None:
        sock = self._clients.get(client)
        if sock is None:
            return
        try:
            sock.sendall(encode(message))
        except OSError:
            self._drop(client)

    def stop(self) -> None:
        for client in list(self._clients):
            self._drop(client)
        if self._epoll is not None:
            self._epoll.close()
            self._epoll = None
        if self._listener is not None:
            self._listener.close()
            self._listener = None
            try:
                self._path.unlink()
            except OSError:
                pass

    # -- internals -----------------------------------------------------
    def _accept(self) -> None:
        assert self._listener is not None and self._epoll is not None
        while True:
            try:
                sock, _ = self._listener.accept()
            except BlockingIOError:
                return
            except OSError as exc:
                log.warning("control: accept failed: %s", exc)
                return
            if _peer_uid(sock) != os.getuid():
                log.warning("control: refused a connection from another user")
                sock.close()
                continue
            sock.setblocking(False)
            fd = sock.fileno()
            self._clients[fd] = sock
            self._buffers[fd] = b""
            self._epoll.register(fd, select.EPOLLIN | select.EPOLLRDHUP)

    def _read(self, fd: int) -> list[tuple[int, dict]]:
        sock = self._clients.get(fd)
        if sock is None:
            return []
        out: list[tuple[int, dict]] = []
        while True:
            try:
                data = sock.recv(65536)
            except BlockingIOError:
                break
            except OSError:
                data = b""
            if not data:
                self._drop(fd)
                out.append((fd, dict(CLOSED)))
                return out
            self._buffers[fd] += data
            if len(self._buffers[fd]) > MAX_LINE and b"\n" not in self._buffers[fd]:
                log.warning("control: dropped a client sending nonsense")
                self._drop(fd)
                out.append((fd, dict(CLOSED)))
                return out
        while b"\n" in self._buffers[fd]:
            line, _, self._buffers[fd] = self._buffers[fd].partition(b"\n")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and isinstance(message.get("op"), str):
                out.append((fd, message))
        return out

    def _drop(self, fd: int) -> None:
        sock = self._clients.pop(fd, None)
        self._buffers.pop(fd, None)
        if sock is None:
            return
        if self._epoll is not None:
            try:
                self._epoll.unregister(fd)
            except OSError:
                pass
        sock.close()


def _peer_uid(sock: socket.socket) -> int | None:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def _answers(path: Path) -> bool:
    """Whether something is listening on ``path`` right now."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def request(message: dict, path: Path | None = None, timeout: float = 2.0) -> dict | None:
    """Send one request and wait for its answer. For scripts and tests.

    The window keeps a connection open instead, because the events that matter
    to it arrive unasked.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path or socket_path()))
        sock.sendall(encode(message))
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buffer += chunk
        return json.loads(buffer.partition(b"\n")[0])
    except (OSError, ValueError):
        return None
    finally:
        sock.close()
