"""The state machine. This is the app.

:func:`decide` and :func:`pointer_delta` are pure: same inputs, same output, no
I/O, and no evdev import. They are the entire testing strategy for input
handling, because CI has no gamepad (CLAUDE.md §4, §5). Everything that touches
the world lives in :class:`Daemon` below them.

Three modes as of v0.2. When a fourth arrives and this file stops being easy to
read in one sitting, split it into ``modes/`` -- but not before.
"""

from __future__ import annotations

import enum
import logging
import math
import time
from dataclasses import dataclass, field

from . import buttons
from .config import Binding, Config

log = logging.getLogger(__name__)

#: Pointer updates per second while desktop mode is active.
TICK_HZ = 60.0
TICK_INTERVAL = 1.0 / TICK_HZ


class Mode(enum.Enum):
    """What the gamepad currently belongs to."""

    #: The pad belongs to whatever is running. We read without grabbing.
    IDLE = "idle"
    #: Modifier held: we hold an exclusive grab and buttons are commands.
    GUIDE_HELD = "guide_held"
    #: Pad drives the pointer and keyboard. Grabbed for the duration.
    DESKTOP = "desktop"


class Grab(enum.Enum):
    """A requested change to our exclusive hold on the device."""

    ACQUIRE = "acquire"
    RELEASE = "release"


@dataclass(frozen=True)
class VirtualOp:
    """Something to do on the virtual mouse or keyboard."""

    #: ``click`` and ``key`` follow the button (press and release); ``tap``
    #: fires a whole combination once, on press only.
    kind: str
    codes: tuple[int, ...]
    pressed: bool = True


@dataclass
class State:
    """Mutable daemon state. Small on purpose."""

    mode: Mode = Mode.IDLE
    #: Axis code -> deflection, normalised to -1.0..1.0.
    sticks: dict[int, float] = field(default_factory=dict)
    #: Axis ranges from the open device, for normalising stick readings.
    axis_ranges: dict[int, tuple[int, int]] = field(default_factory=dict)
    #: D-pad axis code -> key code currently held, so it can be released.
    hats: dict[int, int] = field(default_factory=dict)
    #: Axes that have fired and are waiting to be re-armed by returning to
    #: centre. Tracked per axis, not per direction: the rebound crosses the
    #: *opposite* threshold, so disarming only the direction that fired would
    #: leave the other one live for exactly the event we need to ignore.
    axis_disarmed: set[int] = field(default_factory=set)
    #: Axis code -> time before which it cannot re-arm.
    axis_settle: dict[int, float] = field(default_factory=dict)
    #: Button name -> (release to emit, time to emit it). Holds a release back
    #: briefly so contact bounce can cancel it.
    pending_release: dict[str, tuple[VirtualOp, float]] = field(default_factory=dict)

    def reset_inputs(self) -> None:
        """Forget transient input state when leaving desktop mode."""
        self.sticks.clear()
        self.hats.clear()
        self.axis_disarmed.clear()
        self.axis_settle.clear()
        self.pending_release.clear()


@dataclass(frozen=True)
class Decision:
    """What :func:`decide` wants done. Nothing here has happened yet."""

    grab: Grab | None = None
    binding: Binding | None = None
    #: Zero or more virtual-input operations, applied in order. A stick that
    #: snaps from one extreme to the other releases one direction and engages
    #: the opposite one from a single event, so this cannot be a single value.
    virtual: tuple[VirtualOp, ...] = ()
    #: Set when the mode changed, so the daemon can react to the transition.
    mode: Mode | None = None

    def __bool__(self) -> bool:
        return bool(self.grab or self.binding or self.virtual or self.mode)


NOTHING = Decision()


def decide(
    ev_type: int,
    ev_code: int,
    ev_value: int,
    state: State,
    config: Config,
    now: float = 0.0,
) -> Decision:
    """Map one input event to a decision, mutating only ``state``.

    ``now`` is a monotonic timestamp in seconds. It is an explicit argument
    rather than a call to the clock so this stays pure and the time-dependent
    behaviour (stick settling) is testable without sleeping.

    * Pressing the modifier grabs the pad, so buttons become commands rather
      than leaking into the running game.
    * While it is held, a bound button yields its binding, and the configured
      desktop key enters desktop mode.
    * In desktop mode the pad drives the pointer until the exit button.
    """
    if state.mode is Mode.DESKTOP:
        return _decide_desktop(ev_type, ev_code, ev_value, state, config, now)

    modifier_code = buttons.code_of(config.modifier)
    if ev_type == buttons.EV_KEY and ev_code == modifier_code:
        if ev_value == 1:
            # Idempotent: a repeated press without a release (which happens if
            # the daemon starts while the button is already down) must not
            # stack grabs.
            if state.mode is Mode.GUIDE_HELD:
                return NOTHING
            state.mode = Mode.GUIDE_HELD
            return Decision(grab=Grab.ACQUIRE, mode=Mode.GUIDE_HELD)
        if ev_value == 0:
            if state.mode is not Mode.GUIDE_HELD:
                return NOTHING
            state.mode = Mode.IDLE
            return Decision(grab=Grab.RELEASE, mode=Mode.IDLE)
        return NOTHING  # autorepeat

    if state.mode is not Mode.GUIDE_HELD:
        return NOTHING

    name = buttons.resolve(ev_type, ev_code, ev_value)
    if name is None:
        return NOTHING

    desktop = config.desktop
    if desktop.enabled and name == desktop.enter:
        # Already grabbed from the modifier press, so the grab is unchanged.
        state.mode = Mode.DESKTOP
        state.reset_inputs()
        return Decision(mode=Mode.DESKTOP)

    binding = config.action_for(name)
    return Decision(binding=binding) if binding is not None else NOTHING


def _decide_desktop(
    ev_type: int,
    ev_code: int,
    ev_value: int,
    state: State,
    config: Config,
    now: float,
) -> Decision:
    desktop = config.desktop

    # Sticks first: they are by far the most frequent events, and they only
    # update state -- the pointer itself moves on the tick.
    if ev_type == buttons.EV_ABS:
        axis = buttons.stick_axis(ev_code)
        if axis is not None:
            lo, hi = state.axis_ranges.get(ev_code, buttons.DEFAULT_AXIS_RANGE)
            value = buttons.normalize_axis(ev_value, lo, hi)
            state.sticks[ev_code] = value
            ops = _stick_direction_ops(ev_code, value, state, desktop, now)
            return Decision(virtual=ops) if ops else NOTHING

    if ev_type == buttons.EV_KEY and ev_value == 1:
        if ev_code == buttons.code_of(desktop.exit):
            state.mode = Mode.IDLE
            state.reset_inputs()
            return Decision(grab=Grab.RELEASE, mode=Mode.IDLE)

    # D-pad: press on leaving centre, release on returning, so holding a
    # direction behaves like holding the key.
    if ev_type == buttons.EV_ABS and ev_code in (buttons.ABS_HAT0X, buttons.ABS_HAT0Y):
        if ev_value == 0:
            code = state.hats.pop(ev_code, None)
            if code is None:
                return NOTHING
            return Decision(virtual=(VirtualOp("key", (code,), pressed=False),))
        name = buttons.resolve(ev_type, ev_code, ev_value)
        op = _op_for(desktop.bindings.get(name or ""), pressed=True)
        if op is not None and op.kind == "key":
            state.hats[ev_code] = op.codes[0]
        return Decision(virtual=(op,)) if op else NOTHING

    if ev_type == buttons.EV_KEY and ev_value in (0, 1):
        name = _button_name(ev_code)
        op = _op_for(desktop.bindings.get(name or ""), pressed=ev_value == 1)
        if op is None or name is None:
            return NOTHING
        if op.kind == "tap" or desktop.button_debounce <= 0:
            return Decision(virtual=(op,))
        if ev_value == 0:
            # Hold the release back: if the button comes straight back down it
            # was contact bounce, not the user letting go.
            state.pending_release[name] = (
                VirtualOp(op.kind, op.codes, pressed=False),
                now + desktop.button_debounce,
            )
            return NOTHING
        if state.pending_release.pop(name, None) is not None:
            # Bounce cancelled. The key never went up, so nothing to press.
            return NOTHING
        return Decision(virtual=(op,))

    return NOTHING


def pending_release_ops(state: State, now: float) -> tuple[VirtualOp, ...]:
    """Releases whose debounce has elapsed, meaning the button really is up."""
    due = [name for name, (_, at) in state.pending_release.items() if now >= at]
    return tuple(state.pending_release.pop(name)[0] for name in due)


def _stick_direction_ops(
    ev_code: int, value: float, state: State, desktop, now: float
) -> tuple[VirtualOp, ...]:
    """Turn a stick crossing its threshold into one discrete step.

    A stick is not a button: it has no detent, and it springs back hard enough
    to overshoot. Three things follow, and all three were found by using it.

    **Each crossing fires once, as a tap.** Holding a key down while the stick
    is held would hand the repeat rate to the compositor's keyboard autorepeat,
    which steps the window switcher again after its repeat delay -- so a flick
    held a moment too long skips an extra window.

    **Re-arming needs the stick back near centre**, not merely below the engage
    threshold, or a dip-and-rebound counts as a second flick.

    **And re-arming requires the stick back at centre**, not merely the passage
    of time. Released from full deflection a stick does not glide home, it
    rings: it swings *past* centre and crosses the opposite threshold, so a
    flick left fires left and then right. A pure time window cannot bound that,
    because the ring lasts longer the harder you push -- so a fixed window
    expires mid-ring on a big flick and fires again immediately, which is
    exactly the "long push skips more often than a short one" symptom.

    So the axis disarms on firing and only re-arms once the stick is observed
    near centre. The ``stick_settle`` window stacks on top, holding off re-arm
    while the first violent part of the ring passes through the centre.
    """
    pair = buttons.directions_on_axis(ev_code)
    if pair is None:
        return ()
    engage = desktop.stick_threshold
    rearm = max(desktop.dead_zone, engage * 0.35)

    if ev_code in state.axis_disarmed:
        if now >= state.axis_settle.get(ev_code, 0.0) and abs(value) < rearm:
            state.axis_disarmed.discard(ev_code)
        return ()

    # Only the dominant axis of a stick acts, so a mostly-vertical push cannot
    # also trip a horizontal step. This is the mirror of the suppression in
    # pointer_delta and keeps the two from ever firing off one movement.
    sibling = buttons.sibling_axis(ev_code)
    if sibling is not None and not _dominates(
        state, ev_code, sibling, desktop.stick_dominance
    ):
        return ()

    for name, sign in ((pair[0], -1), (pair[1], 1)):
        if value * sign < engage:
            continue
        op = _op_for(desktop.bindings.get(name), pressed=True)
        if op is None:
            continue
        state.axis_disarmed.add(ev_code)
        state.axis_settle[ev_code] = now + desktop.stick_settle
        if op.kind == "tap":
            return (op,)  # a combination is already a complete tap
        return (op, VirtualOp(op.kind, op.codes, pressed=False))
    return ()


def stick_tick_ops(state: State, desktop, now: float) -> tuple[VirtualOp, ...]:
    """Re-evaluate bound stick axes on the tick, using the last known values.

    Needed because re-arming is driven by the stick being *near centre*, and a
    stick sitting at rest emits no events. Without a periodic look, an axis
    that returned to centre unobserved would stay disarmed for good and the
    direction would simply stop working.
    """
    ops: list[VirtualOp] = []
    for axis in claimed_axes(desktop):
        ops += _stick_direction_ops(
            axis, state.sticks.get(axis, 0.0), state, desktop, now
        )
    return tuple(ops)


def _button_name(ev_code: int) -> str | None:
    for name, code in buttons.BUTTONS.items():
        if code == ev_code:
            return name
    return None


def _op_for(spec: str | None, pressed: bool) -> VirtualOp | None:  # noqa: D401
    """Turn a desktop binding string into an operation, or None."""
    if not spec:
        return None
    kind, _, rest = spec.partition(".")
    if kind == "click":
        code = buttons.parse_click(rest)
        return VirtualOp("click", (code,), pressed) if code is not None else None
    if kind == "key":
        codes = buttons.parse_keys(rest)
        if not codes:
            return None
        if len(codes) > 1:
            # A combination fires once, on press. Holding alt+tab down would
            # otherwise leave alt stuck while the window switcher is open.
            return VirtualOp("tap", tuple(codes)) if pressed else None
        return VirtualOp("key", tuple(codes), pressed)
    return None


def pointer_delta(
    state: State, config: Config, dt: float
) -> tuple[float, float, float, float]:
    """Pointer and scroll movement for one tick of ``dt`` seconds.

    Returns ``(dx, dy, scroll_x, scroll_y)`` as floats; the caller accumulates
    the fractional parts. Left stick drives the pointer, right stick the wheel.

    Stick axes are negative upwards, and so are ``REL_Y`` and an upward scroll,
    so no axis needs inverting.
    """
    desktop = config.desktop
    claimed = claimed_axes(desktop)

    def axis(code: int) -> float:
        # An axis bound to a key direction has given up its analogue job.
        if code in claimed:
            return 0.0
        # If its *sibling* is bound, this axis only counts while it clearly
        # dominates. No thumb pushes a stick in a perfectly straight line, so
        # without this a sideways flick to step the window switcher also
        # scrolls it -- which looks exactly like the flick skipping an extra
        # window, and gets worse the harder you flick.
        sibling = buttons.sibling_axis(code)
        if sibling is not None and sibling in claimed:
            if not _dominates(state, code, sibling, desktop.stick_dominance):
                return 0.0
        return state.sticks.get(code, 0.0)

    lx, ly = _vector_response(axis(buttons.ABS_X), axis(buttons.ABS_Y), desktop)
    rx, ry = _vector_response(axis(buttons.ABS_RX), axis(buttons.ABS_RY), desktop)
    return (
        lx * desktop.pointer_speed * dt,
        ly * desktop.pointer_speed * dt,
        rx * desktop.scroll_speed * dt,
        -ry * desktop.scroll_speed * dt,
    )


def _dominates(state: State, axis: int, sibling: int, ratio: float) -> bool:
    """True if ``axis`` is far enough ahead of its sibling to act alone.

    The margin matters. With a plain ``>`` comparison the boundary sits at
    exactly 45 degrees, so a flick that strays a degree past it stops stepping
    and starts scrolling instead -- the same movement doing two different
    things depending on wobble. Requiring a ratio gives each function a cone of
    its own with a small ambiguous wedge between them where nothing fires.
    """
    return abs(state.sticks.get(axis, 0.0)) >= ratio * abs(state.sticks.get(sibling, 0.0))


def claimed_axes(desktop) -> set[int]:
    """Axes that a bound stick direction has taken over from analogue use."""
    return {
        buttons.STICK_DIRS[name][0]
        for name in desktop.bindings
        if name in buttons.STICK_DIRS
    }


def _vector_response(x: float, y: float, desktop) -> tuple[float, float]:
    """Apply the dead zone and response curve to a stick as a *vector*.

    This is deliberately radial rather than per-axis, and getting it wrong is
    very obvious in the hand. An axial dead zone carves a plus-shaped hole out
    of the stick's range: push nearly-but-not-quite straight right and the
    small vertical component lands inside the dead zone, gets zeroed, and the
    pointer snaps to exactly horizontal. The result feels eight-directional.
    Testing the magnitude once, then scaling a unit direction vector, keeps all
    360 degrees available and gives the same top speed in every direction.

    Deflection is rescaled so movement starts from zero just outside the dead
    zone; without that, the pointer jumps to a noticeable speed the instant the
    stick crosses the threshold.
    """
    magnitude = math.hypot(x, y)
    if magnitude <= desktop.dead_zone:
        return 0.0, 0.0
    # Corners of a square-gated stick read past 1.0; clamp the speed but keep
    # the true direction, so diagonals point where the stick points.
    clamped = min(magnitude, 1.0)
    scaled = ((clamped - desktop.dead_zone) / (1.0 - desktop.dead_zone)) ** desktop.curve
    return x / magnitude * scaled, y / magnitude * scaled


class Daemon:
    """Wires the pure state machine to a real device and real subprocesses."""

    def __init__(self, config: Config, reader, runner, virtual=None) -> None:
        self._config = config
        self._reader = reader
        self._runner = runner
        self._virtual = virtual
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
            for item in self._reader.events():
                if item is None:
                    self._tick()
                else:
                    self._handle(*item)
        finally:
            self._reader.ungrab()
            if self._virtual is not None:
                self._virtual.close()
            log.info("stopped")

    def _handle(self, ev_type: int, ev_code: int, ev_value: int) -> None:
        self._state.axis_ranges = self._reader.axis_ranges
        was = self._state.mode
        decision = decide(
            ev_type, ev_code, ev_value, self._state, self._config, time.monotonic()
        )
        if not decision:
            return

        # Only act on a genuine desktop-mode transition. Comparing against the
        # previous mode keeps ordinary IDLE/GUIDE_HELD changes from needlessly
        # tearing down the tick and the virtual devices.
        entering = decision.mode is Mode.DESKTOP and was is not Mode.DESKTOP
        leaving = was is Mode.DESKTOP and decision.mode not in (None, Mode.DESKTOP)
        if entering and not self._enter_desktop():
            return
        if leaving:
            self._leave_desktop()

        if decision.grab is Grab.ACQUIRE:
            self._reader.grab()
        elif decision.grab is Grab.RELEASE:
            self._reader.ungrab()

        if decision.binding is not None:
            self._runner.run(decision.binding)
        for op in decision.virtual:
            self._apply(op)

    def _tick(self) -> None:
        if self._state.mode is not Mode.DESKTOP or self._virtual is None:
            return
        dx, dy, sx, sy = pointer_delta(self._state, self._config, TICK_INTERVAL)
        self._virtual.move(dx, dy)
        self._virtual.scroll(sx, sy)
        now = time.monotonic()
        # A stick resting at centre emits nothing, so re-arming has to be
        # noticed here rather than waiting for an event that never comes. The
        # same goes for a deferred release: the button is already up, so no
        # further event is coming to flush it.
        for op in stick_tick_ops(self._state, self._config.desktop, now):
            self._apply(op)
        for op in pending_release_ops(self._state, now):
            self._apply(op)

    def _apply(self, op: VirtualOp) -> None:
        if self._virtual is None:
            return
        if op.kind == "click":
            self._virtual.click(op.codes[0], op.pressed)
        elif op.kind == "key":
            self._virtual.key(op.codes[0], op.pressed)
        elif op.kind == "tap":
            self._virtual.tap_keys(list(op.codes))

    def _enter_desktop(self) -> bool:
        """Start desktop mode, or back out cleanly if uinput is unavailable."""
        if self._virtual is None or not self._virtual.open():
            log.warning("desktop mode unavailable; staying put")
            self._state.mode = Mode.IDLE
            self._state.reset_inputs()
            self._reader.ungrab()
            return False
        self._reader.set_tick(TICK_INTERVAL)
        log.info("desktop mode -- press %s to leave", self._config.desktop.exit)
        return True

    def _leave_desktop(self) -> None:
        self._reader.set_tick(None)
        if self._virtual is not None:
            # Releasing is what stops a button held on the way out from
            # leaving the virtual mouse button down forever.
            self._virtual.release_all()
