"""The state machine. This is the app.

:func:`decide` and :func:`pointer_delta` are pure: same inputs, same output, no
I/O, and no evdev import. They are the entire testing strategy for input
handling, because CI has no gamepad (CLAUDE.md §4, §5). Everything that touches
the world lives in :class:`Daemon` below them.

Four modes as of v0.3. The on-screen keyboard (v0.5) deliberately did not make
a fifth: it is a *layer* over a pad-driven mode, because the pointer has to
keep working while you type. When a fifth mode does arrive and this file stops
being easy to read in one sitting, split it into ``modes/`` -- but not before.
"""

from __future__ import annotations

import enum
import logging
import math
import time
from dataclasses import dataclass, field

from . import buttons
from .config import (
    Binding,
    Config,
    Frontend,
    Overlay,
    is_frontend,
    is_osk,
    overlay_name,
)
from .osk import protocol as osk_protocol

log = logging.getLogger(__name__)

#: Pointer updates per second while desktop mode is active.
TICK_HZ = 60.0
TICK_INTERVAL = 1.0 / TICK_HZ

#: Tick used when the only outstanding work is a deferred return-to-game. The
#: pointer is not moving, so 20 Hz is plenty to notice the settle elapsing.
SETTLE_TICK = 0.05

#: How far below ``trigger_threshold`` an analogue trigger must fall before it
#: counts as released. Without the gap, a finger resting near the threshold
#: chatters out presses and releases without moving.
TRIGGER_HYSTERESIS = 0.15

#: How soon after a click of ours a focus change still counts as caused by it.
#: The narrow trigger: an application focusing its own search box at startup,
#: or a dialog appearing, must not open the keyboard -- doing so would cost the
#: user the pointer, since the keyboard parks it.
AUTO_OPEN_AFTER_CLICK = 1.5

#: How long after a click to ask what has focus. Long enough for the
#: application to have processed the click, short enough not to feel laggy.
#: The ask is needed because AT-SPI only reports focus *changes*, so clicking
#: the field you are already in raises no event at all.
FOCUS_CHECK_DELAY = 0.15

#: How often to check that the field we opened for is still there, while the
#: keyboard is up because of it. A poll rather than an event because there is
#: no event: measured, AT-SPI never once reported focus *leaving* a text field
#: -- clicking a blank area does not move focus, and a window vanishing sends
#: nothing at all.
FIELD_CHECK_INTERVAL = 0.5

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
    #: The on-screen keyboard is up and owns the whole pad. Entered from
    #: DESKTOP or OVERLAY and returns to whichever it came from
    #: (:attr:`State.osk_return`); never entered from IDLE without passing
    #: through DESKTOP first, because the keyboard needs the virtual devices.
    OSK = "osk"


#: Modes in which we hold the pad exclusively and the tick runs.
PAD_MODES = (Mode.DESKTOP, Mode.OVERLAY, Mode.OSK)

#: Of those, the ones where the sticks drive the virtual pointer. The keyboard
#: takes both sticks -- the left one navigates the grid, the right one slides
#: the panel -- so the pointer is parked for as long as it is up.
POINTER_MODES = (Mode.DESKTOP, Mode.OVERLAY)


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


@dataclass(frozen=True)
class OskOp:
    """Something to do to the on-screen keyboard.

    ``open`` and ``close`` are ours to act on -- each is a mode change, and
    only the daemon may make one, because only it knows whether the renderer
    came up. ``step`` and ``act`` are forwarded to the renderer, which owns the
    grid and decides what they mean.
    """

    kind: str
    value: str = ""


@dataclass
class OskNav:
    """A navigation direction being held, and when it next repeats.

    The clock is the daemon's rather than the renderer's because a stick held
    at full deflection and a d-pad held down both sit there emitting nothing:
    there is no second event to drive a repeat off, so it has to come from the
    tick (docs/on-screen-keyboard.md §Where the repeat clock had to live).
    """

    direction: str
    #: Monotonic time of the next step.
    due: float
    #: The stick axis or d-pad hat code this came from, so the release of one
    #: does not cancel a hold on the other.
    source: int
    #: Which way the axis was pushed, or 0 for the d-pad -- which reports its
    #: own release and so needs no deflection watching.
    sign: int = 0


@dataclass
class State:
    """Mutable daemon state. Small on purpose."""

    mode: Mode = Mode.IDLE
    #: Axis code -> deflection, normalised to -1.0..1.0.
    sticks: dict[int, float] = field(default_factory=dict)
    #: Axis ranges from the open device, for normalising stick readings.
    axis_ranges: dict[int, tuple[int, int]] = field(default_factory=dict)
    #: Trigger axis -> the key code it stands in for, from the open device.
    #: Empty when the pad reports its triggers as buttons already.
    trigger_axes: dict[int, int] = field(default_factory=dict)
    #: Trigger axis -> whether it currently counts as held. Deliberately *not*
    #: cleared by :meth:`reset_inputs`: it mirrors where the user's finger
    #: actually is, which a mode change does not alter. Clearing it mid-hold
    #: would swallow the release and leave everything downstream believing the
    #: trigger was still down.
    triggers: dict[int, bool] = field(default_factory=dict)
    #: D-pad axis code -> key code currently held, so it can be released.
    hats: dict[int, int] = field(default_factory=dict)
    #: Axes that have fired and are waiting to be re-armed by returning to
    #: centre. Tracked per axis, not per direction: the rebound crosses the
    #: *opposite* threshold, so disarming only the direction that fired would
    #: leave the other one live for exactly the event we need to ignore.
    axis_disarmed: set[int] = field(default_factory=set)
    #: Axis code -> time before which it cannot re-arm.
    axis_settle: dict[int, float] = field(default_factory=dict)
    #: Axis code -> (direction sign to ignore, until when). A stick released
    #: from a long hold springs back past centre hard enough to cross the
    #: opposite threshold, and :attr:`axis_settle` cannot cover it: a hold
    #: outlasts that window by definition. Only the *opposite* sign is blocked,
    #: so pushing the same way again is never delayed by having let go.
    axis_rebound: dict[int, tuple[int, float]] = field(default_factory=dict)
    #: Button name -> (release to emit, time to emit it). Holds a release back
    #: briefly so contact bounce can cancel it.
    pending_release: dict[str, tuple[VirtualOp, float]] = field(default_factory=dict)
    #: Where :data:`Mode.OSK` goes back to when the keyboard closes: the mode it
    #: was opened from. Deliberately *not* cleared by :meth:`reset_inputs` --
    #: it is not transient input state, and ``_leave`` reads it to decide
    #: whether an overlay return is owed.
    osk_return: Mode = Mode.DESKTOP
    #: The navigation direction currently held, if any.
    osk_nav: OskNav | None = None

    def reset_inputs(self) -> None:
        """Forget transient input state when entering or leaving a mode."""
        self.sticks.clear()
        self.hats.clear()
        self.axis_disarmed.clear()
        self.axis_settle.clear()
        self.axis_rebound.clear()
        self.pending_release.clear()
        self.osk_nav = None


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
    #: Zero or more things to do to the on-screen keyboard.
    osk: tuple[OskOp, ...] = ()

    def __bool__(self) -> bool:
        return bool(
            self.grab
            or self.binding
            or self.virtual
            or self.mode
            or self.overlay
            or self.frontend
            or self.osk
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

    An analogue trigger is turned into a press or release of its button before
    anything else looks at the event, so ``LT`` and ``RT`` behave like every
    other button everywhere they can appear -- as the modifier, as
    ``desktop.enter``, in any bindings table -- without a single downstream
    branch knowing they came from an axis.

    * Pressing the modifier grabs the pad, so buttons become commands rather
      than leaking into the running game.
    * While it is held, a bound button yields its binding, the configured
      desktop key enters desktop mode, and a button bound to ``overlay.<name>``
      enters overlay mode.
    * In desktop and overlay mode the pad drives the pointer until the exit
      button. The two behave identically here; they differ only in what the
      daemon does on the way in and out.
    """
    if ev_type == buttons.EV_ABS and ev_code in state.trigger_axes:
        translated = _trigger_event(ev_code, ev_value, state, config)
        if translated is None:
            return NOTHING  # a trigger moving, but not across the threshold
        ev_type, ev_code, ev_value = translated

    if state.mode is Mode.OSK:
        return _decide_osk(ev_type, ev_code, ev_value, state, config, now)
    if state.mode in POINTER_MODES:
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
    if is_osk(binding):
        if not desktop.enabled:
            return NOTHING
        # Enter desktop mode, and ask for the keyboard on top of it. Not
        # ``mode=Mode.OSK``: the keyboard needs the virtual devices desktop
        # mode opens, and if the renderer will not come up this is where we
        # want to be left. The daemon makes the second transition, once it
        # knows the renderer is running.
        state.mode = Mode.DESKTOP
        state.reset_inputs()
        return Decision(mode=Mode.DESKTOP, osk=(OskOp("open"),))
    if is_frontend(binding):
        # Deliberately no mode change: the frontend drives itself from the pad,
        # so going home means giving the device back rather than taking it
        # over. We stay in GUIDE_HELD until the modifier is released, and the
        # daemon defers the activation past that release (gotcha §6.1).
        return Decision(frontend=True)
    return Decision(binding=binding)


def _trigger_event(
    ev_code: int, ev_value: int, state: State, config: Config
) -> tuple[int, int, int] | None:
    """Turn a trigger reading into a button press or release, or None.

    None means "nothing to report": the trigger moved, but not across a
    boundary. Most trigger events are exactly that, since a squeeze produces a
    stream of readings on the way down and back up.

    Releasing lower than it presses is not decoration. A trigger resting near
    the threshold, or one with a worn spring, would otherwise chatter out a
    stream of presses and releases from a finger that is not moving.
    """
    lo, hi = state.axis_ranges.get(ev_code, buttons.DEFAULT_TRIGGER_RANGE)
    level = buttons.normalize_trigger(ev_value, lo, hi)
    code = state.trigger_axes[ev_code]
    held = state.triggers.get(ev_code, False)
    if not held and level >= config.trigger_threshold:
        state.triggers[ev_code] = True
        return (buttons.EV_KEY, code, 1)
    if held and level <= max(0.0, config.trigger_threshold - TRIGGER_HYSTERESIS):
        state.triggers[ev_code] = False
        return (buttons.EV_KEY, code, 0)
    return None


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
        spec = desktop.bindings.get(name or "")
        if is_osk(spec):
            # Press only: the keyboard is not a key you hold. Closing it is
            # [osk.bindings]'s job -- while it is up this button has a keyboard
            # meaning of its own, the way it does on a console.
            return Decision(osk=(OskOp("open"),)) if ev_value == 1 else NOTHING
        op = _op_for(spec, pressed=ev_value == 1)
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


#: D-pad button name -> the direction the grid understands.
_OSK_STEPS = {
    "DPAD_UP": "up",
    "DPAD_DOWN": "down",
    "DPAD_LEFT": "left",
    "DPAD_RIGHT": "right",
}

#: Left-stick directions move the highlight too. The pointer has nothing to
#: click with while the keyboard is up -- its own shortcuts take all three
#: mouse buttons -- so the stick is better spent on the grid.
#:
#: Which axis each direction lies on, and which way along it. The stick is read
#: as a *sector* here rather than as a flick, so a direction has to be able to
#: name its own axis (see :func:`osk_stick_direction`).
_OSK_STEP_AXIS: dict[str, tuple[int, int]] = {
    "left": (buttons.ABS_X, -1),
    "right": (buttons.ABS_X, 1),
    "up": (buttons.ABS_Y, -1),
    "down": (buttons.ABS_Y, 1),
}

_OSK_OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up"}

#: The axes that navigate the grid.
OSK_STICK_AXES = frozenset({buttons.ABS_X, buttons.ABS_Y})

#: The axes that slide the whole panel around the screen. This is the only way
#: to get the keyboard off the field you are typing into, and it costs
#: scrolling and Tab for as long as it is up.
OSK_MOVE_AXES = frozenset({buttons.ABS_RX, buttons.ABS_RY})

#: How much of ``stick_threshold`` a held direction must keep to stay held.
#: Below the firing threshold, because a thumb holding a direction drifts, and
#: re-arming at exactly the threshold makes a long hold stutter.
NAV_HOLD_RATIO = 0.75


def _decide_osk(
    ev_type: int,
    ev_code: int,
    ev_value: int,
    state: State,
    config: Config,
    now: float,
) -> Decision:
    """Route one event while the keyboard is up. It owns the whole pad.

    Every button is either bound in ``[osk.bindings]`` or does nothing, which
    is what makes this a mode rather than a layer over desktop mode. The one
    exception is the exit button, checked first: it is the escape that must
    never need thinking about, and a keyboard that could swallow it would be
    able to strand a user holding a controller.

    No debounce here, deliberately. The releases held back in desktop mode
    exist because a stick *click* bounces when the stick is moved (CLAUDE.md
    §4); these are face buttons, and inventing a filter for a bounce nobody has
    traced is exactly the mistake that section was written about. If a key ever
    types twice, trace it first.
    """
    if ev_type == buttons.EV_KEY and ev_value == 1:
        if ev_code == buttons.code_of(config.desktop.exit):
            state.mode = Mode.IDLE
            state.reset_inputs()
            return Decision(grab=Grab.RELEASE, mode=Mode.IDLE)

    if ev_type == buttons.EV_ABS:
        if ev_code in (buttons.ABS_HAT0X, buttons.ABS_HAT0Y):
            if ev_value == 0:
                # Only cancel a hold this hat started: the stick may be holding
                # a direction of its own, and one letting go is not the other.
                if state.osk_nav is not None and state.osk_nav.source == ev_code:
                    state.osk_nav = None
                return NOTHING
            name = buttons.resolve(ev_type, ev_code, ev_value)
            direction = _OSK_STEPS.get(name or "")
            if direction is None:
                return NOTHING
            # sign=0: a d-pad reports its own release, so there is no deflection
            # to watch for it coming home.
            state.osk_nav = OskNav(
                direction, now + config.osk.repeat_delay, ev_code
            )
            return Decision(osk=(OskOp("step", direction),))

        axis = buttons.stick_axis(ev_code)
        if axis is None:
            return NOTHING
        lo, hi = state.axis_ranges.get(ev_code, buttons.DEFAULT_AXIS_RANGE)
        # Recorded before any routing decision: the tick reads deflection
        # directly for the right stick, and for noticing a held one let go.
        state.sticks[ev_code] = buttons.normalize_axis(ev_value, lo, hi)
        if ev_code in OSK_STICK_AXES:
            # On the event rather than the tick, and that is not a nicety: a
            # stick let go from full deflection is back past centre in under a
            # frame, so a tick can miss the whole release.
            ops = osk_nav_ops(state, config, now)
            return Decision(osk=ops) if ops else NOTHING
        # The right stick slides the panel, and does so on the tick: a stick
        # held at deflection stops emitting entirely, and a panel that only
        # moved while the stick was still moving would be unusable.
        return NOTHING

    if ev_type == buttons.EV_KEY and ev_value == 1:
        name = _button_name(ev_code)
        action = config.osk.bindings.get(name or "")
        if action is None:
            # Bound to nothing here, so it does nothing. It does *not* fall
            # back to its desktop job -- that fallthrough is precisely what
            # this stopped being.
            return NOTHING
        if action == "close":
            return Decision(osk=(OskOp("close"),))
        return Decision(osk=(OskOp("act", action),))

    return NOTHING


def osk_stick_direction(state: State, config: Config) -> str | None:
    """Which of the four ways the left stick points now, or None near centre.

    A **sector**, not a flick, and that is the whole difference. Elsewhere a
    stick direction is a bound button: it taps once, then disarms until the
    stick is seen back at centre, because a stick released from deflection
    rings past centre and would otherwise tap again the other way.

    On the grid that model is wrong, and rolling the stick in a circle is what
    exposes it -- the thumb never passes through centre, so the axis that just
    fired stays disarmed through the next quarter turn. Measured against the
    old code at 60 Hz: a slow roll produced eleven steps per turn instead of
    four, and a fast one silently dropped a direction per turn. Both feel like
    the keyboard ignoring you, which is exactly how it was reported.

    So the stick is asked what it is *pointing at*, every tick, the way a d-pad
    is asked which way it is held -- and the answer changing is what steps the
    highlight. Holding a direction still repeats; that clock is
    :func:`osk_nav_ops`'.

    Two pieces of hysteresis keep a thumb resting on a boundary from
    chattering: a held direction stays held down to ``NAV_HOLD_RATIO`` of the
    engage threshold, and it keeps the sector until another direction beats it
    by a tenth. Neither is enough to make the circle skip, because both are far
    smaller than a quarter turn.
    """
    x = state.sticks.get(buttons.ABS_X, 0.0)
    y = state.sticks.get(buttons.ABS_Y, 0.0)
    engage = config.desktop.stick_threshold
    hold = engage * NAV_HOLD_RATIO
    dominance = config.osk.stick_dominance
    nav = state.osk_nav
    # Only a stick-held direction is sticky; a d-pad reports its own release.
    held = nav.direction if nav is not None and nav.sign else None

    best: tuple[str, float] | None = None
    for direction, (axis, sign) in _OSK_STEP_AXIS.items():
        along = (x if axis == buttons.ABS_X else y) * sign
        across = abs(y if axis == buttons.ABS_X else x)
        if direction == held:
            if along < hold or along < dominance * across * 0.9:
                continue
        elif along < engage or along < dominance * across:
            continue
        if best is None or along > best[1]:
            best = (direction, along)
    return best[0] if best else None


def osk_nav_ops(state: State, config: Config, now: float) -> tuple[OskOp, ...]:
    """Steps the grid is owed right now: a new direction, or a repeat.

    Called from the tick and from every stick event, and the two cannot
    disagree: the answer is derived from where the stick is, so asking twice in
    one frame produces one step. The event path exists because a stick let go
    from full deflection is back past centre in under a frame, and a tick can
    miss the whole release.

    This is the only place padmode repeats a held direction itself. A bound
    stick direction elsewhere taps a real key and hands the repeat rate to the
    compositor's autorepeat; here the steps go to our own renderer, so the rate
    is ours to choose and ours to stop.
    """
    if state.mode is not Mode.OSK:
        state.osk_nav = None
        return ()

    direction = osk_stick_direction(state, config)
    nav = state.osk_nav

    if direction is None:
        if nav is not None and nav.sign:
            # The stick came home. Block the way back for a moment: released
            # from full deflection it does not glide to centre, it rings --
            # swinging past and across the far threshold, which would step once
            # the wrong way. A time window alone cannot bound that (the ring
            # lasts longer the harder you push), but a window measured from the
            # release can.
            axis, sign = _OSK_STEP_AXIS[nav.direction]
            state.axis_rebound[axis] = (-sign, now + config.desktop.stick_settle)
            state.osk_nav = None
    else:
        axis, sign = _OSK_STEP_AXIS[direction]
        if nav is not None and nav.sign and direction == _OSK_OPPOSITE[nav.direction]:
            # A thumb cannot cross centre without passing through it, so this
            # is the ring being sampled past the far threshold rather than a
            # push. Same block, taken earlier.
            state.axis_rebound[axis] = (sign, now + config.desktop.stick_settle)
            state.osk_nav = None
            return ()
        blocked, until = state.axis_rebound.get(axis, (0, 0.0))
        if sign == blocked and now < until:
            return ()
        if nav is None or nav.direction != direction or not nav.sign:
            state.osk_nav = OskNav(
                direction, now + config.osk.repeat_delay, axis, sign
            )
            return (OskOp("step", direction),)

    nav = state.osk_nav
    if nav is None or config.osk.repeat_interval <= 0 or now < nav.due:
        return ()
    nav.due = now + config.osk.repeat_interval
    return (OskOp("step", nav.direction),)


def osk_move_delta(
    state: State, config: Config, dt: float
) -> tuple[float, float]:
    """How far to slide the panel this tick, in pixels.

    Deliberately the pointer's dead zone and response curve: a keyboard that
    accelerated differently from the cursor would feel like a different device
    in the same hand.
    """
    if state.mode is not Mode.OSK:
        return (0.0, 0.0)
    x, y = _vector_response(
        state.sticks.get(buttons.ABS_RX, 0.0),
        state.sticks.get(buttons.ABS_RY, 0.0),
        config.desktop,
    )
    speed = config.osk.move_speed * dt
    return (x * speed, y * speed)


def pending_release_ops(state: State, now: float) -> tuple[VirtualOp, ...]:
    """Releases whose debounce has elapsed, meaning the button really is up."""
    due = [name for name, (_, at) in state.pending_release.items() if now >= at]
    return tuple(state.pending_release.pop(name)[0] for name in due)


def _stick_step(
    ev_code: int,
    value: float,
    state: State,
    desktop,
    now: float,
    wanted,
) -> str | None:
    """The stick direction that just fired, or None. All the hard-won bits.

    ``wanted`` says whether a direction means anything to the caller. It gates
    the *side effects* as well as the answer: an axis must not disarm itself
    for a direction that was going to be ignored, or an unbound push would
    swallow the next real one.

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
        return None
    engage = desktop.stick_threshold
    rearm = max(desktop.dead_zone, engage * 0.35)

    if ev_code in state.axis_disarmed:
        if now >= state.axis_settle.get(ev_code, 0.0) and abs(value) < rearm:
            state.axis_disarmed.discard(ev_code)
        return None

    # Only the dominant axis of a stick acts, so a mostly-vertical push cannot
    # also trip a horizontal step. This is the mirror of the suppression in
    # pointer_delta and keeps the two from ever firing off one movement.
    sibling = buttons.sibling_axis(ev_code)
    if sibling is not None and not _dominates(
        state, ev_code, sibling, desktop.stick_dominance
    ):
        return None

    blocked, until = state.axis_rebound.get(ev_code, (0, 0.0))
    for name, sign in ((pair[0], -1), (pair[1], 1)):
        if value * sign < engage:
            continue
        if sign == blocked and now < until:
            # The spring-back from a direction that was being held, not a push.
            # Only the opposite sign is ever blocked, so pushing the same
            # way again still fires immediately.
            return None
        if not wanted(name):
            continue
        state.axis_disarmed.add(ev_code)
        state.axis_settle[ev_code] = now + desktop.stick_settle
        return name
    return None


def _stick_direction_ops(
    ev_code: int, value: float, state: State, desktop, now: float
) -> tuple[VirtualOp, ...]:
    """A bound stick direction, as a tap on the virtual keyboard."""
    def bound(name: str) -> bool:
        return _op_for(desktop.bindings.get(name), pressed=True) is not None

    name = _stick_step(ev_code, value, state, desktop, now, bound)
    if name is None:
        return ()
    op = _op_for(desktop.bindings.get(name), pressed=True)
    if op is None:  # unreachable: `bound` already checked
        return ()
    if op.kind == "tap":
        return (op,)  # a combination is already a complete tap
    return (op, VirtualOp(op.kind, op.codes, pressed=False))


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

    Only called in :data:`POINTER_MODES`. The keyboard is its own mode and owns
    both sticks, so there is no branch here for it -- the tick simply does not
    ask.
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

    def __init__(
        self, config: Config, reader, runner, virtual=None, windows=None, osk=None
    ) -> None:
        self._config = config
        self._reader = reader
        self._runner = runner
        self._virtual = virtual
        #: Window backend, or None when this session has none (see
        #: :mod:`padmode.windows`). Overlay mode is the only thing that uses it.
        self._windows = windows
        #: Keyboard renderer, or None when this session has no keyboard --
        #: same convention as ``virtual`` and ``windows``: absent means the one
        #: feature is off, never that anything fails.
        self._osk = osk
        #: Whether the keyboard now on screen opened itself. Only an automatic
        #: opening closes automatically: one you asked for stays until you
        #: dismiss it, because having it vanish under you would be worse than
        #: having to press a button.
        self._osk_auto = False
        #: When we last synthesised a click. Auto-open needs it: "we own the
        #: click" is what separates *you* entering a field from an application
        #: focusing one on its own.
        self._clicked_at = 0.0
        #: When to ask the renderer what has focus, after a click. None when
        #: nothing is outstanding.
        self._focus_check_at: float | None = None
        #: When we last asked the keyboard to do something, so the round trip
        #: can be reported rather than guessed at. The renderer owning
        #: selection costs a trip per keypress (docs/on-screen-keyboard.md);
        #: this is how we find out what that actually costs.
        self._osk_sent_at: float | None = None
        #: Key codes the keyboard has latched down (ctrl/alt/super). Tracked
        #: here rather than trusted to the renderer: a modifier left held is a
        #: desktop nobody can use, and the renderer is the thing most likely to
        #: not be around to tidy up after itself.
        self._osk_held: set[int] = set()
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
            # Before the deferred return below, for the same reason _leave does
            # it: nothing of ours on screen while windows are being activated.
            self._stop_osk()
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
        self._state.trigger_axes = self._reader.trigger_axes
        # Resolved through the return mode, so the keyboard is invisible to the
        # entering/leaving comparison below: desktop -> OSK -> desktop is not a
        # mode entry, and leaving OSK that was opened over an overlay still
        # owes the overlay its return.
        was = self._state.mode
        if was is Mode.OSK:
            was = self._state.osk_return
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
        for osk_op in decision.osk:
            self._apply_osk(osk_op)

    def _tick(self) -> None:
        now = time.monotonic()
        # Window work first, and outside the mode check: a return-to-game is
        # deliberately outstanding *after* we have left overlay mode.
        self._pump_windows(now)
        if self._state.mode not in PAD_MODES or self._virtual is None:
            return
        # In every pad mode, including OSK: while the keyboard is *down*, this
        # is what polls for the text field that would open it.
        self._pump_osk()
        if self._state.mode is Mode.OSK:
            # The keyboard's own two continuous jobs. Nothing else runs: it
            # owns both sticks, so there is no pointer to move and no bound
            # stick direction that could still be live.
            for op in osk_nav_ops(self._state, self._config, now):
                self._apply_osk(op)
            mx, my = osk_move_delta(self._state, self._config, TICK_INTERVAL)
            if (mx or my) and self._osk is not None:
                self._osk.move(mx, my)
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
            if op.pressed:
                # Remembered for auto-open. This is the "we own the click"
                # asset: the only way to tell you entering a field apart from
                # an application focusing one by itself.
                self._clicked_at = time.monotonic()
                if (
                    self._config.osk.auto_open
                    and self._state.mode is not Mode.OSK
                ):
                    self._focus_check_at = self._clicked_at + FOCUS_CHECK_DELAY
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
        # Before any window work: our layer surface *is* enumerable by the
        # window backends -- measured, and it reports the interpreter's name,
        # which we cannot change. It is never the *active* window, so it can
        # never become a return target, but taking it off screen before we go
        # looking for windows makes a class collision impossible rather than
        # merely unlikely. Costs 0.6 ms to put back.
        self._stop_osk()
        if was is Mode.OVERLAY:
            self._schedule_return()

    def _enter_desktop(self) -> bool:
        """Start desktop mode, or back out cleanly if uinput is unavailable."""
        if self._virtual is None or not self._virtual.open():
            log.warning("desktop mode unavailable; staying put")
            return self._abort()
        self._reader.set_tick(TICK_INTERVAL)
        self._prewarm_osk()
        log.info("desktop mode -- press %s to leave", self._config.desktop.exit)
        return True

    # -- the on-screen keyboard ----------------------------------------
    def _prewarm_osk(self) -> None:
        """Start the renderer with nothing drawn, on entering a pad mode.

        The first show costs ~105 ms of GTK renderer setup and every one after
        it 0.6 ms (measured; docs/on-screen-keyboard.md). Paying it here means
        the user is moving the pointer while it happens instead of waiting for
        a keyboard. Failure is silent by design: the keyboard being unavailable
        must never interfere with desktop mode, which is the feature that
        works everywhere.
        """
        if self._osk is not None:
            self._osk.start()

    def _stop_osk(self) -> None:
        """Take the keyboard off screen and kill the renderer.

        For leaving a pad mode altogether, so it does not put the mode back the
        way :meth:`_leave_osk` does -- the caller is on its way to IDLE.
        """
        self._leave_osk()
        self._osk_auto = False
        self._focus_check_at = None
        self._release_osk_mods()
        if self._osk is not None:
            self._osk.stop()

    def _leave_osk(self) -> None:
        """Go back to the mode the keyboard was opened from.

        The one place the OSK mode ends, so every way out of it -- the close
        button, the renderer dying, the renderer saying it cannot run -- lands
        in the same state. Resetting the inputs is what stops a direction still
        held on the way out from immediately arrowing through the desktop.
        """
        if self._state.mode is Mode.OSK:
            self._state.mode = self._state.osk_return
            self._state.reset_inputs()

    def _release_osk_mods(self) -> None:
        """Let go of every modifier the keyboard latched.

        The renderer releases them itself when it goes away, which covers the
        ordinary path. This is the backstop for the ones where it never gets
        to: it died, or the mode ended out from under it.
        """
        if self._virtual is not None:
            for code in sorted(self._osk_held):
                self._virtual.key(code, False)
        self._osk_held.clear()

    def _apply_osk(self, op: OskOp) -> None:
        if op.kind == "open":
            self._open_osk()
            return
        if op.kind == "close":
            self._close_osk()
            return
        if self._state.mode is not Mode.OSK or self._osk is None:
            return
        if op.kind == "step":
            self._osk.step(op.value)
        elif op.kind == "act":
            self._osk_sent_at = time.monotonic()
            self._osk.act(op.value)

    def _open_osk(self, auto: bool = False) -> None:
        """Put the keyboard up, and enter its mode if it really came up.

        The only writer of ``Mode.OSK``. :func:`decide` asks for the keyboard
        but does not take the mode itself, because only here do we know whether
        the renderer is running -- and unlike every other refused transition
        the right landing is *not* IDLE (``_abort``): a keyboard that will not
        draw should leave you in desktop mode with a working pointer, not on
        your own holding a controller.
        """
        if self._state.mode is Mode.OSK:
            return
        if self._state.mode not in POINTER_MODES:
            return
        if self._osk is None or not self._osk.start():
            log.info("no on-screen keyboard in this session")
            return
        self._osk.show(self._osk_hints())
        self._state.osk_return = self._state.mode
        self._state.mode = Mode.OSK
        # A direction held while it opens must not immediately travel across
        # the grid, and a stick already deflected must not fling the panel into
        # a corner before the user has seen it.
        self._state.reset_inputs()
        self._osk_auto = auto
        # Start watching the field that caused this, so we notice it going
        # away. Only for an automatic opening: one you asked for stays.
        self._focus_check_at = (
            time.monotonic() + FIELD_CHECK_INTERVAL if auto else None
        )
        # Let go of whatever the pad is holding, before the keyboard takes the
        # buttons over. Opening lands *between* the press and the release of
        # the very click that triggered it, and the keyboard claims A -- so the
        # release arrives as a keyboard button and is swallowed, leaving
        # BTN_LEFT down across the entire desktop. Nothing short of leaving the
        # mode clears that, not even a physical mouse. The same reasoning as
        # the release in `_leave`, for the same reason. (The debounced releases
        # that would have flushed it went with `reset_inputs` above.)
        if self._virtual is not None:
            self._virtual.release_all()

    def _close_osk(self) -> None:
        self._leave_osk()
        self._osk_auto = False
        self._focus_check_at = None
        if self._osk is not None:
            self._osk.hide()
        # After the hide: the renderer sends its own releases in response, and
        # this catches whatever it did not.
        self._release_osk_mods()

    def _focus_changed(self, editable: bool) -> None:
        """Open or close the keyboard as focus moves between text fields.

        Opening is deliberately hard and closing deliberately easy, because the
        two failures are not symmetrical: a keyboard that fails to appear costs
        one button press, while one that appears uninvited covers the screen
        *and* parks the pointer, leaving the user worse off than with no
        keyboard at all.
        """
        up = self._state.mode is Mode.OSK
        log.debug(
            "focus: editable=%s osk=%s auto=%s since_click=%.2fs",
            editable, up, self._osk_auto,
            time.monotonic() - self._clicked_at,
        )
        if not self._config.osk.auto_open or self._state.mode not in PAD_MODES:
            return
        if editable:
            if up:
                return
            if time.monotonic() - self._clicked_at > AUTO_OPEN_AFTER_CLICK:
                # Focus moved for some other reason: an application starting,
                # a dialog opening, a window closing. Not our business.
                return
            self._open_osk(auto=True)
        elif up and self._osk_auto:
            self._close_osk()

    def _osk_hints(self) -> dict[str, str]:
        """Action -> the button bound to it, so the keyboard can label itself.

        Built here rather than in the renderer because this is where the
        config lives; the renderer draws whatever it is told.
        """
        hints: dict[str, str] = {}
        for button, action in self._config.osk.bindings.items():
            hints.setdefault(action, button)
        return hints

    def _pump_osk(self) -> None:
        """Act on whatever the renderer said, and notice if it died."""
        if self._osk is None:
            return
        if self._focus_check_at is not None and (
            time.monotonic() >= self._focus_check_at
        ):
            self._osk.ask_focus()
            # Keep asking while the keyboard is up because of a field: that is
            # the only way to notice the field going away. Once it is closed,
            # or was opened by hand, there is nothing to watch.
            self._focus_check_at = (
                time.monotonic() + FIELD_CHECK_INTERVAL
                if self._state.mode is Mode.OSK and self._osk_auto
                else None
            )
        for kind, fields in self._osk.poll():
            self._handle_osk(kind, fields)
        if self._state.mode is Mode.OSK and not self._osk.alive:
            # Risk 4: the renderer is gone while the pad is grabbed. Going back
            # to the mode underneath is all that is needed -- the grab belongs
            # to the mode, not to the keyboard, so desktop mode carries on with
            # a working pointer.
            log.warning("the on-screen keyboard exited; carrying on without it")
            self._leave_osk()
            self._release_osk_mods()

    def _handle_osk(self, kind: str, fields: dict) -> None:
        if kind in (osk_protocol.TYPE, osk_protocol.KEY) and self._osk_sent_at:
            # Includes our own tick: we collect the reply on the next one, so
            # up to 16ms of this is the daemon's polling, not the renderer.
            log.debug("osk round trip %.1f ms",
                      (time.monotonic() - self._osk_sent_at) * 1000)
            self._osk_sent_at = None
        if kind == osk_protocol.TYPE:
            text = fields.get("text")
            codes = buttons.keys_for_char(text) if isinstance(text, str) else None
            if codes is None:
                log.debug("cannot type %r on this layout", text)
                return
            self._apply(VirtualOp("tap", tuple(codes)))
        elif kind == osk_protocol.KEY:
            names = fields.get("keys")
            if not isinstance(names, list):
                return
            codes = [buttons.KEYS.get(str(part).upper()) for part in names]
            if not codes or any(code is None for code in codes):
                return
            self._apply(VirtualOp("tap", tuple(code for code in codes if code)))
        elif kind == osk_protocol.HOLD:
            names = fields.get("keys")
            if not isinstance(names, list) or self._virtual is None:
                return
            down = bool(fields.get("down"))
            codes = [buttons.KEYS.get(str(part).upper()) for part in names]
            if not codes or any(code is None for code in codes):
                return
            for code in codes:
                # Genuinely held, not remembered: the compositor sees a real
                # modifier, so Super+A reaches a global shortcut and Ctrl+click
                # works, exactly as they would from a hardware keyboard.
                self._virtual.key(code, down)
                if down:
                    self._osk_held.add(code)
                else:
                    self._osk_held.discard(code)
        elif kind == osk_protocol.FOCUS:
            self._focus_changed(bool(fields.get("editable")))
        elif kind == osk_protocol.CLOSED:
            self._leave_osk()
            self._osk_auto = False
            self._release_osk_mods()
        elif kind == osk_protocol.FAILED:
            log.warning("on-screen keyboard: %s", fields.get("reason", "unavailable"))
            self._stop_osk()

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
        # After the window work, not before: the keyboard's surface is
        # enumerable, so nothing of ours is on screen while we are searching
        # for and activating windows.
        self._prewarm_osk()
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
