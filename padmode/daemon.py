"""The state machine. This is the app.

:func:`decide` is pure: same inputs, same output, no I/O, no evdev import.
It is the entire testing strategy for input handling, because CI has no
gamepad (CLAUDE.md §4, §5). Everything that actually touches the world lives
in :class:`Daemon` below it.

v0.1 has two modes. The rest (desktop, overlay, unlock) arrive in later
versions and must land *here*, not scattered across the codebase.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass

from . import buttons
from .config import Binding, Config

log = logging.getLogger(__name__)


class Mode(enum.Enum):
    """What the gamepad currently belongs to."""

    #: The pad belongs to whatever is running. We read without grabbing.
    IDLE = "idle"
    #: Modifier held: we hold an exclusive grab and buttons are commands.
    GUIDE_HELD = "guide_held"


class Grab(enum.Enum):
    """A requested change to our exclusive hold on the device."""

    ACQUIRE = "acquire"
    RELEASE = "release"


@dataclass
class State:
    """Mutable daemon state. Small on purpose."""

    mode: Mode = Mode.IDLE


@dataclass(frozen=True)
class Decision:
    """What :func:`decide` wants done. Nothing here has happened yet."""

    #: Change to our device grab, if any.
    grab: Grab | None = None
    #: Binding to execute, if any.
    binding: Binding | None = None

    def __bool__(self) -> bool:
        return self.grab is not None or self.binding is not None


NOTHING = Decision()


def decide(
    ev_type: int, ev_code: int, ev_value: int, state: State, config: Config
) -> Decision:
    """Map one input event to a decision, mutating only ``state``.

    The rules, in full:

    * Pressing the modifier grabs the pad, so subsequent buttons are commands
      rather than leaking into the running game.
    * Releasing the modifier hands the pad straight back.
    * While the modifier is held, a bound button yields its binding.
    * Everything else is ignored.
    """
    modifier_code = buttons.code_of(config.modifier)

    if ev_type == buttons.EV_KEY and ev_code == modifier_code:
        if ev_value == 1:
            # Idempotent: a repeated press without a release (which happens if
            # the daemon starts while the button is already down) must not
            # stack grabs.
            if state.mode is Mode.GUIDE_HELD:
                return NOTHING
            state.mode = Mode.GUIDE_HELD
            return Decision(grab=Grab.ACQUIRE)
        if ev_value == 0:
            if state.mode is not Mode.GUIDE_HELD:
                return NOTHING
            state.mode = Mode.IDLE
            return Decision(grab=Grab.RELEASE)
        return NOTHING  # autorepeat

    if state.mode is not Mode.GUIDE_HELD:
        return NOTHING

    name = buttons.resolve(ev_type, ev_code, ev_value)
    if name is None:
        return NOTHING

    binding = config.action_for(name)
    if binding is None:
        return NOTHING
    return Decision(binding=binding)


class Daemon:
    """Wires the pure state machine to a real device and real subprocesses."""

    def __init__(self, config: Config, reader, runner) -> None:
        self._config = config
        self._reader = reader
        self._runner = runner
        self._state = State()

    @property
    def state(self) -> State:
        return self._state

    def run(self) -> None:
        """Read events until interrupted, acting on each decision.

        The ``finally`` is load-bearing: a crash that leaves the pad grabbed
        makes the machine unusable by controller, which is the worst failure
        mode this app has (CLAUDE.md §4).
        """
        self._runner.preflight(self._config.bindings)
        log.info(
            "ready -- hold %s and press a bound button (%d bindings)",
            self._config.modifier,
            len(self._config.bindings),
        )
        try:
            for ev_type, ev_code, ev_value in self._reader.events():
                self._handle(ev_type, ev_code, ev_value)
        finally:
            self._reader.ungrab()
            log.info("stopped")

    def _handle(self, ev_type: int, ev_code: int, ev_value: int) -> None:
        decision = decide(ev_type, ev_code, ev_value, self._state, self._config)
        if not decision:
            return
        if decision.grab is Grab.ACQUIRE:
            self._reader.grab()
        elif decision.grab is Grab.RELEASE:
            self._reader.ungrab()
        if decision.binding is not None:
            self._runner.run(decision.binding)
