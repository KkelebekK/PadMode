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
    return cfg


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
