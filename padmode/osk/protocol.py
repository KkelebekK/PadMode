"""Line-delimited JSON between the daemon and the keyboard renderer.

One JSON object per line, in both directions, over the child's stdin and
stdout. Chosen over a socket because the renderer's liveness then comes for
free: it dies, the daemon's read returns EOF, and OSK mode ends without a
watchdog. A stuck grab is the worst failure this app has (CLAUDE.md §4), so
the failure path wanting the least machinery wins.

The renderer's stderr is inherited, so GTK's warnings land in journald with
everything else and stdout stays a clean channel.

Stdlib only, deliberately: the daemon imports this module.
"""

from __future__ import annotations

import json
from typing import Any

# -- daemon -> renderer ------------------------------------------------
#: Map the surface. Carries ``hints``: action name -> pad button, so the
#: renderer can label its keys with the buttons *this* config uses.
SHOW = "show"
#: Unmap the surface, keeping the process alive and warm.
HIDE = "hide"
#: One discrete step of the highlight: ``dir`` is up/down/left/right. Discrete
#: because v1 navigates with the d-pad; if sticks are ever added, the daemon
#: digests them into these same steps and the renderer never learns about dead
#: zones (docs/on-screen-keyboard.md).
STEP = "step"
#: A semantic action: ``name`` is one of :data:`ACTIONS`. The daemon resolves
#: buttons to actions so the mapping stays in config, where it is bindable.
ACT = "act"

# -- renderer -> daemon ------------------------------------------------
#: The renderer is up and layer-shell worked. Sent once, before anything is
#: drawn, so a pre-warmed renderer that cannot work is known about early.
READY = "ready"
#: Type ``text``, a single character, on the virtual keyboard.
TYPE = "type"
#: Press a named key or combination, e.g. ``["BACKSPACE"]``.
KEY = "key"
#: Hold a modifier down, or let it up: ``keys`` and ``down``. Genuinely held on
#: the virtual keyboard rather than remembered and prepended, so the compositor
#: sees a real modifier and Super+A, Ctrl+click and held Alt all behave the way
#: they would from hardware. The daemon owns releasing them again -- a latch
#: that outlived the keyboard would strand the desktop with Super down.
HOLD = "hold"
#: The user dismissed the keyboard from within.
CLOSED = "closed"
#: The renderer cannot run. ``reason`` says why, once, in plain words.
FAILED = "failed"

#: Actions a button can be forwarded as. The renderer decides what each does
#: to its own grid; the daemon only knows the names.
ACTIONS = frozenset({
    "commit",    # press the highlighted key
    "back",      # backspace
    "space",
    "enter",
    "tab",
    "shift",
    "capslock",  # lock shift on, rather than a real KEY_CAPSLOCK
    "layer",     # letters <-> symbols
    "close",
    # Move the text cursor. Worth pad buttons of their own: correcting a typo
    # otherwise means closing the keyboard to use the pointer.
    "left",
    "right",
    # Latching modifiers: press once to hold, again to release.
    "ctrl",
    "alt",
    "super",
})

#: Actions that latch a modifier, and the key each holds down.
MODIFIERS: dict[str, str] = {
    "ctrl": "LEFTCTRL",
    "alt": "LEFTALT",
    "super": "LEFTMETA",
}


def encode(kind: str, **fields: Any) -> bytes:
    """One message, newline-terminated, ready to write to a pipe."""
    return (json.dumps({"t": kind, **fields}, separators=(",", ":")) + "\n").encode()


def decode(line: str | bytes) -> tuple[str, dict[str, Any]] | None:
    """Parse one line into ``(kind, fields)``, or None if it is not a message.

    Never raises. A renderer that prints something unexpected to stdout -- a
    stray toolkit message, a half-written line at exit -- must not be able to
    take the daemon down with it.
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    kind = msg.get("t")
    if not isinstance(kind, str):
        return None
    return kind, {key: value for key, value in msg.items() if key != "t"}
