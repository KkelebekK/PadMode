"""Configuration: good defaults, fully overridable.

The defaults below reproduce a known-working setup, so a fresh install does
something useful with no config file at all. Any of it can be overridden in
``$XDG_CONFIG_HOME/padmode/config.toml``.

Bad config is a startup failure, not a warning. A daemon that silently ignores
half your bindings is worse than one that refuses to start and tells you why.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import buttons
from .actions.builtin import ACTIONS
from .osk import protocol as osk_protocol
from .windows import backend_names


class ConfigError(Exception):
    """Raised for any malformed or unusable configuration."""


#: Default bindings, applied while the modifier button is held.
DEFAULT_BINDINGS: dict[str, str] = {
    "RB": "media.next",
    "LB": "media.previous",
    "A": "media.play_pause",
    "Y": "mic.toggle_mute",
    "X": "audio.toggle_mute",
    "DPAD_UP": "audio.volume_up",
    "DPAD_DOWN": "audio.volume_down",
}

DEFAULT_MODIFIER = "GUIDE"
DEFAULT_VOLUME_STEP = 5

#: A binding value is either a built-in action id, an argv list to exec,
#: ``overlay.<name>`` naming a declared overlay, or ``frontend``.
Binding = str | list[str]

#: Marks a binding as "open this overlay" rather than "run this action".
OVERLAY_PREFIX = "overlay."

#: Marks a binding as "go back to the frontend". Not in :data:`ACTIONS`
#: because it is not a command to run: it is a window to hand the pad back to.
FRONTEND_BINDING = "frontend"


def overlay_name(binding: Binding) -> str | None:
    """The overlay a binding opens, or None if it is an ordinary action."""
    if isinstance(binding, str) and binding.startswith(OVERLAY_PREFIX):
        return binding[len(OVERLAY_PREFIX):]
    return None


def is_frontend(binding: Binding) -> bool:
    """True if this binding means "go home to the frontend"."""
    return binding == FRONTEND_BINDING


#: Marks a binding as "put the on-screen keyboard up". Like ``frontend`` it is
#: not an action to run, and like ``overlay.<name>`` it changes mode -- but it
#: is a *request* rather than a mode name, because only the daemon knows
#: whether the renderer actually came up (docs/on-screen-keyboard.md).
OSK_BINDING = "osk"


def is_osk(binding: Binding) -> bool:
    """True if this binding means "show the on-screen keyboard"."""
    return binding == OSK_BINDING

#: Desktop-mode defaults: gamepad drives the pointer and keyboard.
DEFAULT_DESKTOP_BINDINGS: dict[str, str] = {
    "A": "click.left",
    "B": "click.right",
    "X": "click.middle",
    "DPAD_UP": "key.up",
    "DPAD_DOWN": "key.down",
    "DPAD_LEFT": "key.left",
    "DPAD_RIGHT": "key.right",
    "START": "key.enter",
    "SELECT": "key.esc",
    # Hold R3 to hold Alt, then flick the right stick to step through the
    # window switcher. A single `key.alt+tab` tap can only ever toggle between
    # the last two windows, because alt is released before you can choose.
    "R3": "key.alt",
    "RS_RIGHT": "key.tab",
    "RS_LEFT": "key.shift+tab",
    # The one button with no desktop job of its own, so the keyboard costs
    # nothing to reach and takes nothing away.
    "Y": "osk",
}

#: What the pad does *while the keyboard is up*. This is the whole map, not an
#: overlay on the desktop one: the keyboard is its own mode, so a button
#: missing from here does nothing until you put the keyboard away. Both sticks
#: are taken too -- the left one navigates the grid and the right one slides
#: the panel around the screen, so it can be pushed off whatever you are
#: typing into.
#:
#: ``A`` commits even though it is left-click in desktop mode: committing a
#: character is by far the most frequent thing you do while typing, so it gets
#: the most natural button, and ``B`` puts the keyboard away in one press when
#: you want to click something. The d-pad is not listed because it navigates
#: the grid and is not bindable in v1 -- the grid needs it.
#:
#: The shoulders move the text cursor and the triggers change what the keys
#: mean -- one trigger for shift, one for the symbol layer. The triggers are
#: only usable here because analogue ones are translated into buttons (§2);
#: on most pads they are axes and could not be bound at all before that.
DEFAULT_OSK_BINDINGS: dict[str, str] = {
    "A": "commit",
    "B": "close",
    "X": "back",
    "Y": "space",
    "START": "enter",
    "LB": "left",
    "RB": "right",
    "LT": "shift",
    "RT": "layer",
    "L3": "capslock",
    # The stick that moves the panel puts it back, which is where a thumb
    # already is when it has been pushed somewhere silly. It costs R3's desktop
    # job (holding Alt) for as long as the keyboard is up; alt-tabbing while
    # typing is not a thing anyone does, and Alt latches on the grid anyway.
    "R3": "recenter",
}


@dataclass(frozen=True)
class OskConfig:
    """The on-screen keyboard: a separate process the daemon spawns."""

    enabled: bool = True
    #: Which screen edge it starts against, and what ``recenter`` puts it back
    #: to. It never reserves space, so it covers rather than displaces -- there
    #: is no way to know where the text field you are typing into is, which is
    #: also why the right stick can move it out of the way.
    position: str = "bottom"
    #: Share of the screen's height the keyboard should take, in percent. The
    #: width follows from it, since the keys keep their proportions. Height is
    #: the dimension worth naming: it is how much of the screen you lose, and
    #: it means the same thing on a laptop panel and a television. Approximate
    #: rather than exact -- getting it to the pixel would cost a second layout
    #: pass for no benefit.
    height: float = 30.0
    #: Open the keyboard by itself when you click into a text field, and close
    #: it again when focus leaves one -- but only when it opened itself. A
    #: keyboard you asked for stays until you dismiss it.
    #:
    #: Needs desktop accessibility switched on (``gsettings set
    #: org.gnome.desktop.interface toolkit-accessibility true``); without it no
    #: application reports its text fields and nothing happens. padmode says so
    #: once at startup rather than leaving you guessing.
    auto_open: bool = True
    #: How long a navigation direction must be held before the highlight starts
    #: travelling on its own, in seconds. Held rather than flicked is the
    #: normal way to cross a keyboard: the grid is 13 keys wide, and flicking
    #: thirteen times to reach Backspace is what made the first version feel
    #: stiff.
    repeat_delay: float = 0.35
    #: Seconds between steps once it is travelling. 0 turns hold-to-repeat off
    #: and goes back to one step per push.
    repeat_interval: float = 0.07
    #: How fast the right stick slides the whole keyboard, in pixels per
    #: second at full deflection. The panel is clamped to the screen, so the
    #: worst a wild push can do is park it in a corner -- and ``recenter`` puts
    #: it back.
    move_speed: float = 900.0
    #: How far one axis of the left stick must exceed the other before it steps
    #: the highlight. 1.0 -- a plain "whichever is bigger" -- rather than
    #: ``desktop.stick_dominance``'s 2.0, and the difference is the whole
    #: reason this knob exists.
    #:
    #: A margin buys something on the desktop, where the same stick chooses
    #: between two *different* jobs: stepping and scrolling. Doing nothing in
    #: the ambiguous wedge beats confidently doing the wrong one. On the
    #: keyboard the stick has one job, so the wedge is bought and never used --
    #: at 2.0 each direction gets a 53 degree cone and 147 degrees of the
    #: circle step nowhere, which is felt immediately as directions that do not
    #: answer. At 1.0 the four cones meet at the diagonals and cover it.
    #:
    #: Raise it if the diagonals feel indecisive on your pad.
    stick_dominance: float = 1.0
    #: Path or soname to preload so gtk4-layer-shell can hook GDK. Empty means
    #: autodetect (:func:`padmode.osk.preload_library`); set it only if the
    #: library lives somewhere we do not look.
    preload: str | None = None
    bindings: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_OSK_BINDINGS)
    )


@dataclass(frozen=True)
class DesktopConfig:
    """Desktop mode: the pad becomes a mouse and keyboard."""

    enabled: bool = True
    #: Pressed with the modifier held to enter desktop mode.
    enter: str = "L3"
    #: Pressed on its own to leave. This is the "return button" that used to
    #: be routed through a synthetic keystroke and a flag file (CLAUDE.md §3.2).
    exit: str = "GUIDE"
    #: Pointer travel in pixels per second at full stick deflection.
    pointer_speed: float = 900.0
    #: Wheel clicks per second at full deflection of the right stick.
    scroll_speed: float = 10.0
    #: Deflection below this is treated as centred, to absorb stick drift.
    dead_zone: float = 0.15
    #: Response exponent. 1.0 is linear; higher gives finer slow control.
    curve: float = 2.0
    #: Deflection at which a bound stick *direction* counts as pressed.
    stick_threshold: float = 0.6
    #: Seconds to ignore an axis after one of its directions fires. A stick
    #: released from full deflection springs back past centre hard enough to
    #: cross the opposite threshold, which would step the window switcher back
    #: the other way. No threshold can filter that out -- the overshoot is
    #: bigger than the trigger -- so it is filtered by time instead.
    stick_settle: float = 0.15
    #: How far one axis of a stick must exceed the other before it acts, when
    #: that stick has a bound direction. At 1.0 the split is exactly 45
    #: degrees, so a flick that strays a hair past the diagonal flips from
    #: stepping to scrolling. A margin gives each function a comfortable cone
    #: and leaves a small ambiguous wedge in between where nothing happens,
    #: which is much better than confidently doing the wrong thing.
    stick_dominance: float = 2.0
    #: Seconds a button release is held back before it is believed. A stick
    #: click (L3/R3) is a switch underneath the stick, and flicking the stick
    #: sideways can momentarily unseat it -- so a button you are still holding
    #: reports a release and an immediate re-press. For a held modifier that
    #: closes and reopens the window switcher mid-flick. Set to 0 to disable.
    button_debounce: float = 0.08
    bindings: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_DESKTOP_BINDINGS)
    )


@dataclass(frozen=True)
class Overlay:
    """An app that can be raised over whatever is running, and returned from."""

    #: The key under ``[overlays]``; what a binding refers to.
    name: str
    #: Matched against the window class, case-insensitively, as a regex.
    window_class: str
    #: Argv to start the app if no matching window exists. Without one, the
    #: overlay can only raise an app that is already running.
    command: list[str] | None = None


@dataclass(frozen=True)
class Frontend:
    """The game frontend: where the pad goes when there is nowhere else.

    Two jobs, and they deliberately do not behave the same way:

    * a button bound to ``frontend`` raises it, and **may start it** if it is
      not running;
    * a return from an overlay whose window has died lands here, and **never
      starts anything** -- a game crashing is no reason for the daemon to spawn
      a process behind the user's back.
    """

    #: Matched against the window class, case-insensitively, as a regex.
    window_class: str
    #: Argv to start it. Used only by the binding, never by the fallback.
    command: list[str] | None = None


@dataclass(frozen=True)
class WindowsConfig:
    """Window backend selection, and the timing overlay mode depends on."""

    #: ``auto``, a backend name, or ``none``. See :mod:`padmode.windows`.
    backend: str = "auto"
    #: Seconds between releasing the pad and re-activating the window we came
    #: from. Gotcha §6.1: release the grab *before* the previous window regains
    #: focus, or a still-held return button leaks a raw gamepad event into it.
    #: The setup this replaces used 0.7s and it crashed a frontend without one.
    settle: float = 0.7
    #: How long to wait for a launched app's window to appear before giving up.
    #: Cold-starting an Electron app takes seconds, so this is generous.
    launch_timeout: float = 10.0
    #: Move the cursor onto the overlay window (gotcha §6.3). Off by default:
    #: it only matters under focus-follows-mouse, which is not the default on
    #: any desktop we support, and it is the one thing here that moves the
    #: pointer without the user asking.
    park_pointer: bool = False


@dataclass(frozen=True)
class UnlockConfig:
    """Typing the login password at the lock screen with the pad (v0.7).

    Off by default and staying that way: it only works with a digits-only
    password, which is a real security downgrade (docs/virtual-input.md).
    """

    enabled: bool = False


@dataclass(frozen=True)
class Config:
    """Fully resolved configuration. Immutable once built."""

    #: Substring of the gamepad's name, case-insensitive. None = autodetect.
    device_name: str | None = None
    #: Exact device path. Set this only to override autodetection entirely.
    device_path: str | None = None
    #: Canonical name of the button that must be held for bindings to fire.
    modifier: str = DEFAULT_MODIFIER
    #: Volume change per press, in percent.
    volume_step: int = DEFAULT_VOLUME_STEP
    #: How far an analogue trigger must be squeezed before ``LT``/``RT`` count
    #: as pressed, from 0.0 to 1.0. Only relevant on pads that report their
    #: triggers as axes, which is most of them; a pad with digital triggers
    #: ignores this entirely. Releasing happens a little lower, so a finger
    #: resting on the threshold does not chatter.
    trigger_threshold: float = 0.5
    #: Canonical button name -> action id or argv list.
    bindings: dict[str, Binding] = field(default_factory=lambda: dict(DEFAULT_BINDINGS))
    desktop: DesktopConfig = field(default_factory=DesktopConfig)
    windows: WindowsConfig = field(default_factory=WindowsConfig)
    osk: OskConfig = field(default_factory=OskConfig)
    unlock: UnlockConfig = field(default_factory=UnlockConfig)
    #: Overlay name -> definition. Empty by default: an overlay needs an app,
    #: and we do not know which apps anyone has.
    overlays: dict[str, Overlay] = field(default_factory=dict)
    #: The game frontend, if one is configured. None by default for the same
    #: reason as overlays: we do not know which frontend anyone runs, and the
    #: core is frontend-agnostic (CLAUDE.md §2).
    frontend: Frontend | None = None

    def action_for(self, name: str) -> Binding | None:
        """Binding attached to a canonical button name, if any."""
        return self.bindings.get(name)

    def overlay_for(self, name: str) -> Overlay | None:
        """The overlay a button opens, if it is bound to one."""
        binding = self.bindings.get(name)
        overlay = overlay_name(binding) if binding is not None else None
        return self.overlays.get(overlay) if overlay is not None else None


def default_path() -> Path:
    """Where the config lives, honouring XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "padmode" / "config.toml"


def load(path: Path | None = None) -> Config:
    """Load config from disk. A missing file is fine and yields the defaults."""
    path = path or default_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Config()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    return parse(data)


def parse(data: dict[str, Any]) -> Config:
    """Build a Config from already-parsed TOML data.

    Split out from :func:`load` so the validation rules can be tested without
    touching the filesystem.
    """
    cfg = Config()

    device = _table(data, "device")
    cfg = replace(
        cfg,
        device_name=_opt_str(device, "name", "device"),
        device_path=_opt_str(device, "path", "device"),
    )

    general = _table(data, "general")
    if "modifier" in general:
        raw_mod = general["modifier"]
        if not isinstance(raw_mod, str):
            raise ConfigError("general.modifier must be a string, e.g. \"GUIDE\"")
        modifier = buttons.canonical(raw_mod)
        if modifier is None:
            raise ConfigError(f"general.modifier: unknown button {raw_mod!r}")
        if modifier in buttons.HATS:
            raise ConfigError(
                f"general.modifier: {raw_mod!r} is a d-pad direction; "
                "the modifier must be a real button you can hold"
            )
        cfg = replace(cfg, modifier=modifier)

    if "trigger_threshold" in general:
        value = general["trigger_threshold"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError("general.trigger_threshold must be a number")
        if not 0.05 <= value <= 0.95:
            raise ConfigError(
                "general.trigger_threshold must be between 0.05 and 0.95 "
                "(a fraction of full squeeze)"
            )
        cfg = replace(cfg, trigger_threshold=float(value))

    if "volume_step" in general:
        step = general["volume_step"]
        if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= 100:
            raise ConfigError("general.volume_step must be an integer from 1 to 100")
        cfg = replace(cfg, volume_step=step)

    # Overlays, the frontend and the keyboard first: a binding may name any of
    # them, so all three have to exist before the bindings that refer to them
    # can be checked.
    cfg = replace(
        cfg,
        windows=_parse_windows(_table(data, "windows")),
        overlays=_parse_overlays(_table(data, "overlays")),
        frontend=_parse_frontend(_table(data, "frontend"), "frontend" in data),
        osk=_parse_osk(_table(data, "osk")),
    )
    cfg = replace(
        cfg,
        bindings=_parse_bindings(
            _table(data, "bindings"), cfg.modifier, cfg.overlays, cfg.frontend,
            cfg.osk,
        ),
    )
    cfg = replace(cfg, desktop=_parse_desktop(_table(data, "desktop"), cfg))
    cfg = replace(cfg, unlock=_parse_unlock(_table(data, "unlock")))
    return cfg


def _parse_unlock(table: dict[str, Any]) -> UnlockConfig:
    """Parse ``[unlock]``. Absent means off, which is the default for a reason."""
    _reject_unknown(table, {"enabled"}, "unlock")
    unlock = UnlockConfig()
    if "enabled" in table:
        if not isinstance(table["enabled"], bool):
            raise ConfigError("unlock.enabled must be true or false")
        unlock = replace(unlock, enabled=table["enabled"])
    return unlock


def _parse_windows(table: dict[str, Any]) -> WindowsConfig:
    windows = WindowsConfig()
    _reject_unknown(table, {"backend", "settle", "launch_timeout", "park_pointer"},
                    "windows")

    if "backend" in table:
        backend = table["backend"]
        if not isinstance(backend, str) or backend not in backend_names():
            raise ConfigError(
                f"windows.backend must be one of: {', '.join(backend_names())}"
            )
        windows = replace(windows, backend=backend)

    if "park_pointer" in table:
        if not isinstance(table["park_pointer"], bool):
            raise ConfigError("windows.park_pointer must be true or false")
        windows = replace(windows, park_pointer=table["park_pointer"])

    for key, lo, hi in (("settle", 0.0, 5.0), ("launch_timeout", 0.0, 120.0)):
        if key not in table:
            continue
        value = table[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"windows.{key} must be a number of seconds")
        if not lo <= value <= hi:
            raise ConfigError(f"windows.{key} must be between {lo} and {hi}")
        windows = replace(windows, **{key: float(value)})
    return windows


def _parse_overlays(table: dict[str, Any]) -> dict[str, Overlay]:
    """Parse ``[overlays.<name>]`` tables into overlay definitions."""
    overlays: dict[str, Overlay] = {}
    for name, spec in table.items():
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ConfigError(
                f"overlays: {name!r} is not a usable name. Use letters, digits, "
                "'-' or '_', so a binding can refer to it as "
                f'"{OVERLAY_PREFIX}{name}"'
            )
        if not isinstance(spec, dict):
            raise ConfigError(
                f"[overlays.{name}] must be a table, e.g.\n"
                f'  [overlays.{name}]\n  class = "discord"\n'
                f'  command = ["discord"]'
            )
        _reject_unknown(spec, {"class", "command"}, f"overlays.{name}")

        window_class = spec.get("class")
        if not isinstance(window_class, str) or not window_class.strip():
            raise ConfigError(
                f"overlays.{name}.class must be a non-empty string matching the "
                'app\'s window class, e.g. class = "discord". '
                "`padmode windows` prints the class of the focused window."
            )

        command = spec.get("command")
        if command is not None and (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) for part in command)
        ):
            raise ConfigError(
                f"overlays.{name}.command must be a non-empty list of strings, "
                'e.g. command = ["discord"]. Omit it entirely to only raise an '
                "app that is already running."
            )
        overlays[name] = Overlay(
            name=name,
            window_class=window_class,
            command=list(command) if command else None,
        )
    return overlays


def _parse_frontend(table: dict[str, Any], declared: bool) -> Frontend | None:
    """Parse ``[frontend]``. Absent is fine; present and empty is a mistake."""
    if not declared:
        return None
    _reject_unknown(table, {"class", "command"}, "frontend")

    window_class = table.get("class")
    if not isinstance(window_class, str) or not window_class.strip():
        raise ConfigError(
            "frontend.class must be a non-empty string matching your frontend's "
            'window class, e.g. class = "pegasus". '
            "`padmode windows` prints the class of the focused window."
        )

    command = table.get("command")
    if command is not None and (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) for part in command)
    ):
        raise ConfigError(
            "frontend.command must be a non-empty list of strings, e.g. "
            'command = ["pegasus-fe"]. It is used only by a button bound to '
            '"frontend"; the return-target fallback never starts it.'
        )
    return Frontend(
        window_class=window_class, command=list(command) if command else None
    )


def _reject_unknown(table: dict[str, Any], known: set[str], section: str) -> None:
    """Fail on a misspelled key rather than silently ignoring it.

    A typo in ``command`` turns an overlay that launches its app into one that
    quietly does nothing when the app is closed -- a bug the user would spend
    real time on. Cheap to catch here.
    """
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(
            f"[{section}]: unknown key{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(key) for key in unknown)}. "
            f"Known: {', '.join(sorted(known))}"
        )


def _parse_osk(table: dict[str, Any]) -> OskConfig:
    """Parse ``[osk]``. Absent is fine; the keyboard is on by default."""
    _reject_unknown(
        table,
        {
            "enabled", "position", "height", "auto_open", "preload", "bindings",
            "repeat_delay", "repeat_interval", "move_speed",
            "stick_dominance",
        },
        "osk",
    )
    osk = OskConfig()

    for key in ("enabled", "auto_open"):
        if key not in table:
            continue
        if not isinstance(table[key], bool):
            raise ConfigError(f"osk.{key} must be true or false")
        osk = replace(osk, **{key: table[key]})

    if "position" in table:
        position = table["position"]
        if position not in ("bottom", "top"):
            raise ConfigError('osk.position must be "bottom" or "top"')
        osk = replace(osk, position=position)

    if "height" in table:
        height = table["height"]
        if isinstance(height, bool) or not isinstance(height, (int, float)):
            raise ConfigError("osk.height must be a number of percent, e.g. 30")
        if not 8 <= height <= 70:
            raise ConfigError(
                "osk.height must be between 8 and 70 percent of screen height"
            )
        osk = replace(osk, height=float(height))

    for key, lo, hi in (
        ("repeat_delay", 0.05, 2.0),
        # Zero is meaningful: hold-to-repeat off, one step per push.
        ("repeat_interval", 0.0, 1.0),
        ("move_speed", 50.0, 20000.0),
        ("stick_dominance", 1.0, 10.0),
    ):
        if key not in table:
            continue
        value = table[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"osk.{key} must be a number")
        if not lo <= value <= hi:
            raise ConfigError(f"osk.{key} must be between {lo} and {hi}")
        osk = replace(osk, **{key: float(value)})

    if "preload" in table:
        preload = table["preload"]
        if not isinstance(preload, str) or not preload:
            raise ConfigError(
                "osk.preload must be a path or soname for gtk4-layer-shell, "
                'e.g. "/usr/lib/libgtk4-layer-shell.so". Leave it out to '
                "autodetect."
            )
        osk = replace(osk, preload=preload)

    merged = dict(DEFAULT_OSK_BINDINGS)
    for raw_name, value in _table(table, "bindings").items():
        name = buttons.canonical(raw_name)
        if name is None:
            raise ConfigError(f"osk.bindings: unknown button {raw_name!r}")
        if name in buttons.HATS:
            raise ConfigError(
                f"osk.bindings.{name}: the d-pad navigates the keyboard and "
                "cannot be rebound"
            )
        if value is False:
            merged.pop(name, None)
            continue
        if not isinstance(value, str) or value not in osk_protocol.ACTIONS:
            raise ConfigError(
                f"osk.bindings.{name} must be one of "
                f"{', '.join(sorted(osk_protocol.ACTIONS))}, or false to unbind"
            )
        merged[name] = value
    return replace(osk, bindings=merged)


def _parse_desktop(table: dict[str, Any], cfg: Config) -> DesktopConfig:
    desktop = DesktopConfig()

    if "enabled" in table:
        if not isinstance(table["enabled"], bool):
            raise ConfigError("desktop.enabled must be true or false")
        desktop = replace(desktop, enabled=table["enabled"])

    for key in ("enter", "exit"):
        if key not in table:
            continue
        raw = table[key]
        if not isinstance(raw, str):
            raise ConfigError(f"desktop.{key} must be a string, e.g. \"L3\"")
        name = buttons.canonical(raw)
        if name is None:
            raise ConfigError(f"desktop.{key}: unknown button {raw!r}")
        desktop = replace(desktop, **{key: name})

    for key, lo, hi in (
        ("pointer_speed", 1.0, 20000.0),
        ("scroll_speed", 0.1, 500.0),
        ("dead_zone", 0.0, 0.9),
        ("curve", 1.0, 5.0),
        ("stick_threshold", 0.2, 0.95),
        ("stick_settle", 0.0, 1.0),
        ("button_debounce", 0.0, 0.5),
        ("stick_dominance", 1.0, 10.0),
    ):
        if key not in table:
            continue
        value = table[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"desktop.{key} must be a number")
        if not lo <= value <= hi:
            raise ConfigError(f"desktop.{key} must be between {lo} and {hi}")
        desktop = replace(desktop, **{key: float(value)})

    desktop = replace(
        desktop, bindings=_parse_desktop_bindings(_table(table, "bindings"), cfg.osk)
    )

    # The enter button is pressed with the modifier held, exactly like a tier-1
    # binding, so the two share a namespace and must not collide.
    if desktop.enter in cfg.bindings:
        raise ConfigError(
            f"desktop.enter ({desktop.enter}) is also bound in [bindings]; "
            "holding the modifier and pressing it would do both"
        )
    if desktop.enter == cfg.modifier:
        raise ConfigError("desktop.enter cannot be the modifier button itself")
    if desktop.exit in desktop.bindings:
        raise ConfigError(
            f"desktop.exit ({desktop.exit}) is also bound in [desktop.bindings]; "
            "it would fire that action on the way out"
        )
    return desktop


def _parse_desktop_bindings(
    table: dict[str, Any], osk: OskConfig
) -> dict[str, str]:
    merged: dict[str, str] = dict(DEFAULT_DESKTOP_BINDINGS)
    for raw_name, value in table.items():
        name = buttons.canonical(raw_name)
        if name is None:
            raise ConfigError(f"desktop.bindings: unknown button {raw_name!r}")
        if value is False:
            merged.pop(name, None)
            continue
        if not isinstance(value, str):
            raise ConfigError(
                f"desktop.bindings.{name} must be a string like \"click.left\", "
                '"key.alt+tab" or "osk", or false to unbind'
            )
        _validate_desktop_action(name, value, osk)
        merged[name] = value
    return merged


def _validate_desktop_action(name: str, value: str, osk: OskConfig) -> None:
    if is_osk(value):
        if not osk.enabled:
            raise ConfigError(
                f"desktop.bindings.{name}: the on-screen keyboard is turned off "
                "([osk] enabled = false), so nothing would happen"
            )
        return
    kind, _, spec = value.partition(".")
    if kind == "click":
        if buttons.parse_click(spec) is None:
            raise ConfigError(
                f"desktop.bindings.{name}: unknown mouse button {spec!r}. "
                f"Known: {', '.join(sorted(buttons.CLICKS))}"
            )
        return
    if kind == "key":
        if buttons.parse_keys(spec) is None:
            raise ConfigError(
                f"desktop.bindings.{name}: cannot parse key {spec!r}. "
                'Use names like "up", "enter" or "alt+tab"'
            )
        return
    raise ConfigError(
        f"desktop.bindings.{name}: {value!r} must start with 'click.' or 'key.', "
        "or be \"osk\""
    )


def _parse_bindings(
    table: dict[str, Any],
    modifier: str,
    overlays: dict[str, Overlay],
    frontend: Frontend | None = None,
    osk: OskConfig | None = None,
) -> dict[str, Binding]:
    """Merge user bindings over the defaults.

    Merging (rather than replacing) means adding one binding doesn't require
    restating all of them. To remove a default, set it to ``false``.
    """
    merged: dict[str, Binding] = dict(DEFAULT_BINDINGS)

    for raw_name, value in table.items():
        name = buttons.canonical(raw_name)
        if name is None:
            raise ConfigError(
                f"bindings: unknown button {raw_name!r}. "
                f"Known: {', '.join(sorted(buttons.BUTTONS) + sorted(buttons.HATS))}"
            )
        if name in buttons.STICK_DIRS:
            raise ConfigError(
                f"bindings.{raw_name}: stick directions only work in "
                "[desktop.bindings]; the sticks belong to the game while the "
                "modifier is held"
            )
        if value is False:
            merged.pop(name, None)
            continue
        merged[name] = _validate_binding(name, value, overlays, frontend, osk)

    if modifier in merged:
        raise ConfigError(
            f"bindings: {modifier} is the modifier button (general.modifier), "
            "so it cannot also be bound to an action"
        )
    return merged


def _validate_binding(
    name: str,
    value: Any,
    overlays: dict[str, Overlay],
    frontend: Frontend | None = None,
    osk: OskConfig | None = None,
) -> Binding:
    """A binding is an action id, an overlay, the frontend, the keyboard, or
    an argv list."""
    if isinstance(value, str):
        if is_osk(value):
            if osk is not None and not osk.enabled:
                raise ConfigError(
                    f"bindings.{name}: the on-screen keyboard is turned off "
                    "([osk] enabled = false), so nothing would happen"
                )
            return value
        overlay = overlay_name(value)
        if overlay is not None:
            if overlay not in overlays:
                declared = ", ".join(sorted(overlays)) or "none are declared"
                raise ConfigError(
                    f"bindings.{name}: no overlay named {overlay!r}. "
                    f"Declare it with [overlays.{overlay}] -- {declared}"
                )
            return value
        if is_frontend(value):
            if frontend is None:
                raise ConfigError(
                    f"bindings.{name}: no frontend is configured. Declare one "
                    'with:\n  [frontend]\n  class = "pegasus"'
                )
            return value
        if value not in ACTIONS:
            known = sorted(ACTIONS) + [f"{OVERLAY_PREFIX}{o}" for o in sorted(overlays)]
            if frontend is not None:
                known.append(FRONTEND_BINDING)
            if osk is None or osk.enabled:
                known.append(OSK_BINDING)
            raise ConfigError(
                f"bindings.{name}: unknown action {value!r}. "
                f"Known: {', '.join(known)}"
            )
        return value
    if isinstance(value, list):
        if not value or not all(isinstance(part, str) for part in value):
            raise ConfigError(
                f"bindings.{name}: a command must be a non-empty list of strings, "
                'e.g. ["notify-send", "hello"]'
            )
        return list(value)
    raise ConfigError(
        f"bindings.{name} must be an action id, a command list, or false to unbind"
    )


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _opt_str(table: dict[str, Any], key: str, section: str) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{section}.{key} must be a non-empty string")
    return value
