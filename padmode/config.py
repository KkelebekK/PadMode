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

#: A binding value is either a built-in action id, or an argv list to exec.
Binding = str | list[str]

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
}


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
    #: Canonical button name -> action id or argv list.
    bindings: dict[str, Binding] = field(default_factory=lambda: dict(DEFAULT_BINDINGS))
    desktop: DesktopConfig = field(default_factory=DesktopConfig)

    def action_for(self, name: str) -> Binding | None:
        """Binding attached to a canonical button name, if any."""
        return self.bindings.get(name)


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

    if "volume_step" in general:
        step = general["volume_step"]
        if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= 100:
            raise ConfigError("general.volume_step must be an integer from 1 to 100")
        cfg = replace(cfg, volume_step=step)

    cfg = replace(cfg, bindings=_parse_bindings(_table(data, "bindings"), cfg.modifier))
    cfg = replace(cfg, desktop=_parse_desktop(_table(data, "desktop"), cfg))
    return cfg


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
        desktop, bindings=_parse_desktop_bindings(_table(table, "bindings"))
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


def _parse_desktop_bindings(table: dict[str, Any]) -> dict[str, str]:
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
                f"desktop.bindings.{name} must be a string like \"click.left\" "
                'or "key.alt+tab", or false to unbind'
            )
        _validate_desktop_action(name, value)
        merged[name] = value
    return merged


def _validate_desktop_action(name: str, value: str) -> None:
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
        f"desktop.bindings.{name}: {value!r} must start with 'click.' or 'key.'"
    )


def _parse_bindings(table: dict[str, Any], modifier: str) -> dict[str, Binding]:
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
        merged[name] = _validate_binding(name, value)

    if modifier in merged:
        raise ConfigError(
            f"bindings: {modifier} is the modifier button (general.modifier), "
            "so it cannot also be bound to an action"
        )
    return merged


def _validate_binding(name: str, value: Any) -> Binding:
    """A binding is a known action id, or a non-empty list of argv strings."""
    if isinstance(value, str):
        if value not in ACTIONS:
            raise ConfigError(
                f"bindings.{name}: unknown action {value!r}. "
                f"Known: {', '.join(sorted(ACTIONS))}"
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
