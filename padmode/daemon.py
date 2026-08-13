"""The state machine. This is the app.

:func:`decide` and :func:`pointer_delta` are pure: same inputs, same output, no
I/O, and no evdev import. They are the entire testing strategy for input
handling, because CI has no gamepad (CLAUDE.md §4, §5). Everything that touches
the world lives in :class:`Daemon` below them.

Four modes as of v0.3. When a fifth arrives and this file stops being easy to
read in one sitting, split it into ``modes/`` -- but not before.
"""

from __future__ import annotations

import enum
import logging
import math
import time
from dataclasses import dataclass, field

from . import buttons
from .config import Binding, Config, Frontend, Overlay, is_frontend, overlay_name

log = logging.getLogger(__name__)

#: Pointer updates per second while desktop mode is active.
TICK_HZ = 60.0
TICK_INTERVAL = 1.0 / TICK_HZ

#: Tick used when the only outstanding work is a deferred return-to-game. The
#: pointer is not moving, so 20 Hz is plenty to notice the settle elapsing.
SETTLE_TICK = 0.05

#: How often we look for the window of an app we just launched. Each look is
#: one short-lived process, so this trades a little latency for staying well
#: clear of spawning one 60 times a second.
LAUNCH_POLL = 0.25

#: Checking that focus actually landed where we sent it: how long to wait
#: before the first look, and how many looks before concluding it did not.
#: Only used when a frontend is configured.
#:
#: Measured on KWin Wayland (Plasma 6.7): 15 paced activations of real
#: application windows all took focus on the first ask, 3.0-6.2 ms after
#: `windowactivate` returned, subprocess round trip included. So the first look
#: is deliberately ~25x the worst observed case. The retries are not for that
#: latency -- they are for the one activation in ~20 that was dropped when
#: switching windows back-to-back as fast as subprocesses allow, which is not
#: how the daemon behaves but is cheap to be wrong about. See
#: docs/window-backends.md.
RETURN_VERIFY_DELAY = 0.15
RETURN_VERIFY_ATTEMPTS = 3

#: Closed-loop cursor parking: how many move-and-look rounds to try, how close
#: counts as arrived, and how long to let the compositor digest a move before
#: reading the cursor back. The gain bounds stop one odd reading from turning
#: into an absurd next request. See :meth:`Daemon._warp`.
WARP_ATTEMPTS = 6
WARP_TOLERANCE = 8
WARP_LATENCY = 0.02
WARP_GAIN_MIN = 0.05
WARP_GAIN_MAX = 20.0


class Mode(enum.Enum):
    """What the gamepad currently belongs to."""

    #: The pad belongs to whatever is running. We read without grabbing.
    IDLE = "idle"
    #: Modifier held: we hold an exclusive grab and buttons are commands.
    GUIDE_HELD = "guide_held"
    #: Pad drives the pointer and keyboard. Grabbed for the duration.
    DESKTOP = "desktop"
    #: Like DESKTOP, but an app was raised over what was running and we
    #: remember the window to hand focus back to on the way out.
    OVERLAY = "overlay"


#: Modes in which the pad drives the virtual pointer and keyboard. They differ
#: only in what happens at the edges, so everything in between is shared.
PAD_MODES = (Mode.DESKTOP, Mode.OVERLAY)


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
    #: Name of the overlay to open, when entering overlay mode.
    overlay: str | None = None
    #: Go back to the configured frontend. Not a mode change: the pad is handed
    #: *back*, so this only says where focus should end up once it is free.
    frontend: bool = False

    def __bool__(self) -> bool:
        return bool(
            self.grab
            or self.binding
            or self.virtual
            or self.mode
            or self.overlay
            or self.frontend
        )


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
    * While it is held, a bound button yields its binding, the configured
      desktop key enters desktop mode, and a button bound to ``overlay.<name>``
      enters overlay mode.
    * In desktop and overlay mode the pad drives the pointer until the exit
      button. The two behave identically here; they differ only in what the
      daemon does on the way in and out.
    """
    if state.mode in PAD_MODES:
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
    if binding is None:
        return NOTHING

    overlay = overlay_name(binding)
    if overlay is not None:
        # Already grabbed from the modifier press, so the grab is unchanged.
        # The daemon may still refuse the transition -- there may be no window
        # backend, or nothing to show -- and put the mode back.
        state.mode = Mode.OVERLAY
        state.reset_inputs()
        return Decision(mode=Mode.OVERLAY, overlay=overlay)
    if is_frontend(binding):
        # Deliberately no mode change: the frontend drives itself from the pad,
        # so going home means giving the device back rather than taking it
        # over. We stay in GUIDE_HELD until the modifier is released, and the
        # daemon defers the activation past that release (gotcha §6.1).
        return Decision(frontend=True)
    return Decision(binding=binding)


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


def _label(spec: Overlay | Frontend) -> str:
    """How a window spec is named in a log line."""
    return "frontend" if isinstance(spec, Frontend) else f"overlay {spec.name}"


class Daemon:
    """Wires the pure state machine to a real device and real subprocesses."""

    def __init__(self, config: Config, reader, runner, virtual=None, windows=None) -> None:
        self._config = config
        self._reader = reader
        self._runner = runner
        self._virtual = virtual
        #: Window backend, or None when this session has none (see
        #: :mod:`padmode.windows`). Overlay mode is the only thing that uses it.
        self._windows = windows
        self._state = State()
        #: Window to hand focus back to when overlay mode ends.
        self._return_to = None
        #: The window the overlay raised, so the cursor can be kept off it.
        self._overlay_window = None
        #: Where the cursor was before we parked it, so it can be put back.
        self._parked_from: tuple[int, int] | None = None
        #: ``(spec, deadline, next poll)`` while waiting for a launched app.
        #: The spec is an :class:`Overlay` or the :class:`Frontend`; both carry
        #: a window class and a command, which is all the polling needs.
        self._pending_launch: tuple[Overlay | Frontend, float, float] | None = None
        #: When the deferred return-to-game is due.
        self._pending_return: float | None = None
        #: Whether that return is worth checking up on afterwards. Only a
        #: return *to a remembered window* can find that window gone.
        self._verify_return = False
        #: ``(window we asked for, when to look, looks left)`` after activating.
        self._pending_verify: tuple[str, float, int] | None = None
        #: Where "go home" is heading. Deliberately *not* ``_return_to``: an
        #: overlay opened in between overwrites that with the window it must go
        #: back to, which would quietly redirect the frontend button.
        self._home_to = None
        #: The frontend button was pressed while the pad is still grabbed, so
        #: the activation is waiting for the release.
        self._home_pending = False
        self._warned_park = False

    @property
    def state(self) -> State:
        return self._state

    def run(self) -> None:
        """Read events until interrupted, acting on each decision.

        The ``finally`` is load-bearing: a crash that leaves the pad grabbed
        makes the machine unusable by controller, which is the worst failure
        mode this app has (CLAUDE.md §4).
        """
        # Overlay and frontend bindings name a window, not an external command,
        # so they are checked separately rather than confusing the action
        # runner -- which would otherwise report them as a missing binary.
        self._runner.preflight({
            name: binding
            for name, binding in self._config.bindings.items()
            if overlay_name(binding) is None and not is_frontend(binding)
        })
        self._preflight_overlays()
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
            # A return that was still settling when we were asked to stop
            # happens now, while the virtual devices are still open. Skipping
            # it would leave the user staring at the overlay with the daemon
            # gone -- and the grab is already released, so the order §6.1 cares
            # about still holds.
            if self._pending_return is not None:
                self._finish_return()
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

        # Only act on a genuine transition into or out of a pad-driven mode.
        # Comparing against the previous mode keeps ordinary IDLE/GUIDE_HELD
        # changes from needlessly tearing down the tick and virtual devices.
        entering = decision.mode in PAD_MODES and was not in PAD_MODES
        leaving = was in PAD_MODES and decision.mode not in (None, *PAD_MODES)
        if entering:
            if not self._enter(decision):
                return
            # Opening a pad-driven mode supersedes a "go home" that has not
            # fired yet: the user has since asked for something else.
            self._home_pending = False
        if leaving:
            self._leave(was)

        if decision.grab is Grab.ACQUIRE:
            self._reader.grab()
        elif decision.grab is Grab.RELEASE:
            self._reader.ungrab()
            if self._home_pending:
                # The pad is free as of this instant, which is exactly what the
                # frontend activation was waiting for.
                self._home_pending = False
                self._defer_return(verify=False)

        if decision.binding is not None:
            self._runner.run(decision.binding)
        if decision.frontend:
            self._go_home()
        for op in decision.virtual:
            self._apply(op)

    def _tick(self) -> None:
        now = time.monotonic()
        # Window work first, and outside the mode check: a return-to-game is
        # deliberately outstanding *after* we have left overlay mode.
        self._pump_windows(now)
        if self._state.mode not in PAD_MODES or self._virtual is None:
            return
        dx, dy, sx, sy = pointer_delta(self._state, self._config, TICK_INTERVAL)
        self._virtual.move(dx, dy)
        self._virtual.scroll(sx, sy)
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

    # -- entering and leaving a pad-driven mode ------------------------
    def _enter(self, decision: Decision) -> bool:
        if decision.mode is Mode.OVERLAY:
            return self._enter_overlay(decision.overlay)
        return self._enter_desktop()

    def _leave(self, was: Mode) -> None:
        self._reader.set_tick(None)
        if self._virtual is not None:
            # Releasing is what stops a button held on the way out from
            # leaving the virtual mouse button down forever.
            self._virtual.release_all()
        if was is Mode.OVERLAY:
            self._schedule_return()

    def _enter_desktop(self) -> bool:
        """Start desktop mode, or back out cleanly if uinput is unavailable."""
        if self._virtual is None or not self._virtual.open():
            log.warning("desktop mode unavailable; staying put")
            return self._abort()
        self._reader.set_tick(TICK_INTERVAL)
        log.info("desktop mode -- press %s to leave", self._config.desktop.exit)
        return True

    def _enter_overlay(self, name: str | None) -> bool:
        """Raise an app over what is running, remembering where to go back to.

        Everything that can refuse the transition is checked before anything
        visible happens, so a button that cannot work leaves the session
        exactly as it found it rather than half-entering a mode.
        """
        overlay = self._config.overlays.get(name or "")
        if overlay is None:  # unreachable via validated config
            log.warning("no overlay named %r", name)
            return self._abort()
        if self._windows is None:
            log.warning(
                "overlay %s needs a window backend and this session has none",
                overlay.name,
            )
            return self._abort()
        if self._virtual is None or not self._virtual.open():
            log.warning("overlay %s needs virtual input; staying put", overlay.name)
            return self._abort()

        # Read this before anything can steal focus -- it is the whole point.
        # Unless a return is still pending: focus has not gone back yet, so
        # what is active right now is the overlay we just left. Asking again
        # would make the overlay its own return target and strand the user on
        # it. The window still waiting is the one we actually want.
        if self._pending_return is None:
            self._return_to = self._windows.get_active_window()
        target = self._windows.find_window(overlay.window_class)
        if target is None:
            if not overlay.command:
                log.info(
                    "overlay %s: nothing matches class %r and there is no "
                    "command to start it",
                    overlay.name, overlay.window_class,
                )
                return self._abort()
            if not self._runner.run(overlay.command):
                return self._abort()  # the runner has already said why
            now = time.monotonic()
            self._pending_launch = (
                overlay, now + self._config.windows.launch_timeout, now + LAUNCH_POLL
            )

        # A new overlay supersedes a return that is still pending from the last
        # one, and anything the frontend button set in motion; otherwise either
        # would fire mid-overlay and yank focus away. Done here, at the point
        # the entry is committed, so an overlay that *refused* leaves a pending
        # "go home" alone -- nothing opened, so the earlier request stands. The
        # isinstance check is what keeps this from eating the launch this
        # overlay may have just started a few lines above.
        self._pending_return = None
        self._home_to = None
        self._home_pending = False
        if self._pending_launch is not None and isinstance(
            self._pending_launch[0], Frontend
        ):
            self._pending_launch = None
        self._reader.set_tick(TICK_INTERVAL)
        if target is not None:
            self._show(overlay, target)
        log.info("overlay %s -- press %s to go back",
                 overlay.name, self._config.desktop.exit)
        return True

    def _abort(self) -> bool:
        """Undo a refused transition into a pad-driven mode.

        Always returns False, so callers can ``return self._abort()``.
        """
        self._state.mode = Mode.IDLE
        self._state.reset_inputs()
        # This releases the pad below, and a "go home" waiting for exactly that
        # release is now due. A refused overlay opened nothing, so the earlier
        # request still stands.
        if self._home_pending:
            self._home_pending = False
            self._defer_return(verify=False)
        # A return already in flight survives a refused entry: failing to open
        # a second overlay is no reason to strand the user on the first, nor to
        # forget which window that return still has to put away.
        if self._pending_return is None:
            self._return_to = None
            self._overlay_window = None
        self._pending_launch = None
        self._reader.ungrab()
        return False

    # -- overlay window work, all driven from the tick -----------------
    def _pump_windows(self, now: float) -> None:
        """Advance whatever the overlay is waiting on. Never blocks the loop."""
        if self._pending_launch is not None:
            self._poll_launch(now)
        if self._pending_return is not None and now >= self._pending_return:
            self._finish_return()
        if self._pending_verify is not None:
            self._check_return(now)

    def _poll_launch(self, now: float) -> None:
        """Watch for the window of an app we started, until it or time runs out.

        Serves both the overlay and the frontend button, which want the same
        thing (wait for a class to appear) and different things once it does:
        an overlay is raised now, the frontend only once the pad is free.
        """
        spec, deadline, next_poll = self._pending_launch
        if now < next_poll:
            return
        target = self._windows.find_window(spec.window_class) if self._windows else None
        if target is not None:
            self._pending_launch = None
            if isinstance(spec, Frontend):
                self._arm_home(target)
            else:
                self._show(spec, target)
            return
        if now >= deadline:
            self._pending_launch = None
            log.warning(
                "%s: %s opened no window matching class %r within %.0fs",
                _label(spec), spec.command[0] if spec.command else "?",
                spec.window_class, self._config.windows.launch_timeout,
            )
            # Giving up is the end of the work, and outside a pad-driven mode
            # nothing else is keeping the heartbeat alive. Without this the
            # frontend button leaves the daemon ticking 20 times a second for
            # the rest of the session.
            self._idle_tick()
            return
        self._pending_launch = (spec, deadline, now + LAUNCH_POLL)

    def _show(self, overlay: Overlay, window_id) -> None:
        if self._windows is None:
            return
        log.info("overlay %s: activating %s", overlay.name, window_id)
        self._overlay_window = window_id
        self._windows.activate_window(window_id)
        if self._config.windows.park_pointer:
            self._park(window_id)

    def _schedule_return(self) -> None:
        """Hand focus back, but not yet.

        Gotcha §6.1: the grab has to be released *before* the old window
        regains focus, or a still-held return button leaks a raw gamepad event
        into it -- which used to crash the frontend. The release happens in
        this same event; the re-activation is deferred past it by
        ``windows.settle``.

        Deferring rather than sleeping also keeps the read loop alive through
        the wait, so SIGTERM still gets us out with the pad released.
        """
        if self._windows is None or (
            self._return_to is None and self._parked_from is None
        ):
            self._return_to = self._parked_from = None
            return
        self._defer_return(verify=True)

    def _defer_return(self, verify: bool) -> None:
        """Activate ``self._return_to`` once ``settle`` has passed.

        Shared by the two things that hand focus somewhere the pad is about to
        belong to: leaving an overlay, and the frontend button. They differ
        only in ``verify`` -- a return to a *remembered* window can find that
        window gone, whereas the frontend was looked up a moment ago.
        """
        self._verify_return = verify
        self._pending_return = time.monotonic() + self._config.windows.settle
        self._reader.set_tick(SETTLE_TICK)

    def _finish_return(self) -> None:
        self._pending_return = None
        overlay, self._overlay_window = self._overlay_window, None
        # When both are pending, "go home" wins: it is the later request, and
        # it is the one the user made on purpose.
        target = self._return_to if self._home_to is None else self._home_to
        self._home_to = None
        going_back = self._windows is not None and target is not None

        # Get the overlay off the screen first, and always. Under
        # focus-follows-mouse a visible overlay under the cursor takes focus
        # straight back -- measured on a real game, 460ms after we handed focus
        # over, with the cursor never moving. A minimized window cannot do
        # that. It also guarantees the game is unobscured, and KWin still finds
        # a minimized window by class, so the next press raises this one rather
        # than starting a second copy.
        if going_back and overlay is not None:
            if not self._windows.minimize_window(overlay):
                log.debug("this backend cannot minimize; leaving %s on screen",
                          overlay)

        # Put the cursor back *before* the old window takes focus: the motion
        # then lands on the overlay, which does not care, rather than on a game
        # that would read it as a camera turn.
        if self._parked_from is not None:
            origin, self._parked_from = self._parked_from, None
            self._warp(*origin)
        if going_back:
            log.info("returning to %s", target)
            self._windows.activate_window(target)
            if self._verify_return and self._config.frontend is not None:
                # Costs one extra call, and only for people who configured a
                # frontend to fall back to. Everyone else pays nothing.
                self._pending_verify = (
                    target,
                    time.monotonic() + RETURN_VERIFY_DELAY,
                    RETURN_VERIFY_ATTEMPTS,
                )
        self._return_to = None
        self._verify_return = False
        self._idle_tick()

    def _idle_tick(self) -> None:
        """Stop the heartbeat once nothing is waiting on it.

        One place rather than several: the tick serves the pointer, launch
        polling, the settle and the verification at once, and clearing it while
        any of them is outstanding silently strands that one.
        """
        if self._state.mode in PAD_MODES:
            return
        if (
            self._pending_launch is not None
            or self._pending_return is not None
            or self._pending_verify is not None
        ):
            return
        self._reader.set_tick(None)

    def _check_return(self, now: float) -> None:
        """Did focus actually land where we sent it? If not, go home.

        A window id outlives its window. When a game exits while the overlay is
        up, ``kdotool windowactivate`` on the remembered id exits 0 and does
        nothing at all, which leaves the user looking at the desktop holding a
        gamepad -- the one failure this mode cannot recover from on its own.

        Asking "does this id still exist" would need a new backend operation
        and a different answer from every window tool. Asking *where focus
        ended up* needs only ``get_active_window``, which every backend already
        has to implement (CLAUDE.md §3.3), and measures the thing we actually
        care about rather than a proxy for it.
        """
        target, due, attempts = self._pending_verify
        if now < due:
            return
        active = self._windows.get_active_window() if self._windows else None
        if active != target and attempts > 1:
            self._pending_verify = (target, now + RETURN_VERIFY_DELAY, attempts - 1)
            return
        self._pending_verify = None
        if active != target:
            self._fall_back_home(target, active)
        self._idle_tick()

    def _fall_back_home(self, target, active) -> None:
        frontend = self._config.frontend
        if frontend is None or self._windows is None:  # unreachable
            return
        log.info(
            "%s did not take focus (%s has it) -- treating it as gone and "
            "falling back to the frontend", target, active or "nothing",
        )
        window = self._windows.find_window(frontend.window_class)
        if window is None:
            # Deliberately does not start it. A game exiting is not a reason to
            # spawn a process on the user's behalf; the frontend button is.
            log.warning(
                "nothing matches the frontend class %r either, so there is "
                "nowhere to go back to", frontend.window_class,
            )
            return
        self._windows.activate_window(window)

    # -- the frontend: going home (v0.4) --------------------------------
    def _go_home(self) -> None:
        """Hand the pad back to the frontend.

        Not a mode: the frontend reads the gamepad itself, so the useful thing
        is to give the device back rather than drive it with a virtual mouse.
        That makes the timing identical to an overlay return -- the pad must be
        released before the frontend takes focus (gotcha §6.1) -- so this only
        works out *where* to go, and :meth:`_arm_home` handles *when*.
        """
        frontend = self._config.frontend
        if frontend is None:  # unreachable via validated config
            return
        if self._windows is None:
            log.warning(
                "the frontend button needs a window backend and this session "
                "has none"
            )
            return
        window = self._windows.find_window(frontend.window_class)
        if window is not None:
            self._arm_home(window)
            return
        if not frontend.command:
            log.info(
                "frontend: nothing matches class %r and there is no command "
                "to start it", frontend.window_class,
            )
            return
        if not self._runner.run(frontend.command):
            return  # the runner has already said why
        now = time.monotonic()
        self._pending_launch = (
            frontend, now + self._config.windows.launch_timeout, now + LAUNCH_POLL
        )
        self._reader.set_tick(SETTLE_TICK)

    def _arm_home(self, window) -> None:
        """Send focus to ``window``, as soon as the pad is not ours."""
        self._home_to = window
        if self._state.mode is Mode.GUIDE_HELD:
            # Still grabbed: the modifier is held down. Waiting for its release
            # is what keeps the button's own release out of the frontend, which
            # is the event that used to crash it.
            self._home_pending = True
            return
        self._defer_return(verify=False)

    # -- cursor parking (gotcha §6.3) ----------------------------------
    def _park(self, window_id) -> None:
        rect = self._windows.window_geometry(window_id)
        here = self._windows.pointer_position()
        if rect is None or here is None:
            if not self._warned_park:
                self._warned_park = True
                log.warning(
                    "park_pointer: the %s backend cannot report the window "
                    "geometry or the cursor, so parking is disabled",
                    getattr(self._windows, "name", "window"),
                )
            return
        # Only the *first* park records an origin. Opening a second overlay
        # before the return has run would otherwise record the middle of the
        # first one as "where the cursor started", and put it there on the way
        # out instead of back where the user left it.
        if self._parked_from is None:
            self._parked_from = here
        self._warp(*rect.centre)

    def _warp(self, x: int, y: int) -> None:
        """Drive the cursor to a screen position by moving and looking.

        There is no warp to call: our pointer is a *relative* device, and
        neither kdotool nor xdotool can place the cursor on a Wayland session.
        Nor can the delta be computed once and emitted -- pointer acceleration
        means a relative move does not travel as far as its arithmetic says,
        and how far off it is depends on a curve we do not get to see.

        So each round measures itself: ask for a move, read where the cursor
        actually ended up, and use the ratio to scale the next request. Two
        rounds are usually enough whether the session accelerates or decelerates
        us, and no round trusts a number it has not just observed.

        **Not yet measured on real hardware** -- park_pointer is off by default
        and this loop is bounded, so the worst case is a cursor that stops
        somewhere short. See docs/window-backends.md before tuning it, and
        trace it rather than reasoning about it (CLAUDE.md §4).
        """
        if self._virtual is None or self._windows is None:
            return
        gain = 1.0
        for _ in range(WARP_ATTEMPTS):
            here = self._windows.pointer_position()
            if here is None:
                return
            dx, dy = x - here[0], y - here[1]
            if abs(dx) <= WARP_TOLERANCE and abs(dy) <= WARP_TOLERANCE:
                return
            request = (dx / gain, dy / gain)
            self._virtual.move(*request)
            time.sleep(WARP_LATENCY)
            after = self._windows.pointer_position()
            if after is None:
                return
            asked = math.hypot(*request)
            moved = math.hypot(after[0] - here[0], after[1] - here[1])
            if asked > 0 and moved > 0:
                gain = min(max(moved / asked, WARP_GAIN_MIN), WARP_GAIN_MAX)
        log.debug("park_pointer: did not settle on (%d, %d)", x, y)

    def _preflight_overlays(self) -> None:
        """Say at startup what will not work, rather than on the first press."""
        overlays = self._config.overlays
        frontend = self._config.frontend
        if not overlays and frontend is None:
            return
        if self._windows is None:
            log.warning(
                "%d overlay(s)%s configured but this session has no window "
                "backend, so they are disabled",
                len(overlays), " and a frontend" if frontend is not None else "",
            )
            return
        for name, overlay in sorted(overlays.items()):
            if overlay.command and not self._runner.available(overlay.command[0]):
                log.warning(
                    "overlay %s: %s is not installed, so it can only raise an "
                    "already-running window", name, overlay.command[0],
                )
        if (
            frontend is not None
            and frontend.command
            and not self._runner.available(frontend.command[0])
        ):
            log.warning(
                "frontend: %s is not installed, so the frontend button can "
                "only raise an already-running window", frontend.command[0],
            )
