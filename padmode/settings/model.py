"""What the settings window edits, and how an edit becomes text in the file.

No GTK here: everything the window can do to the config goes through
:class:`Session`, so all of it is testable without a display -- the same
split as the keyboard's :mod:`padmode.osk.model`.

The rules that matter:

* **The file is edited, not regenerated** (:mod:`padmode.tomledit`). A setting
  put back to its default is *removed* from the file rather than written out,
  so the file only ever says what the user changed.
* **Nothing reaches the disk before Apply**, and Apply goes through
  :func:`padmode.config.write`, which refuses what the daemon would refuse.
  Between edits the session may be briefly invalid -- an overlay deleted while
  a button still opens it -- and :attr:`Session.problem` says so, in the
  daemon's own words, so the window can hold Apply back until it is fixed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import buttons, config
from .. import unlock as unlock_module
from ..actions.builtin import ACTIONS
from ..tomledit import Document, EditError


# -- the settings that are numbers, switches and choices --------------------
@dataclass(frozen=True)
class Number:
    table: tuple[str, ...]
    key: str
    label: str
    help: str
    minimum: float
    maximum: float
    step: float
    digits: int = 2
    integer: bool = False


@dataclass(frozen=True)
class Toggle:
    table: tuple[str, ...]
    key: str
    label: str
    help: str


@dataclass(frozen=True)
class Choice:
    table: tuple[str, ...]
    key: str
    label: str
    help: str
    options: tuple[tuple[str, str], ...]   # (value, label)


Field = Number | Toggle | Choice

DESKTOP = ("desktop",)
OSK = ("osk",)
GENERAL = ("general",)
WINDOWS = ("windows",)
UNLOCK = ("unlock",)
APPEARANCE = ("appearance",)

#: The Feel page, group by group. Ranges are the config parser's own, so the
#: window can never offer a value the daemon would refuse.
FEEL: tuple[tuple[str, tuple[Field, ...]], ...] = (
    ("Pointer", (
        Number(DESKTOP, "pointer_speed", "Pointer speed",
               "Pixels per second at full stick", 1, 20000, 50, 0),
        Number(DESKTOP, "curve", "Acceleration curve",
               "1 is linear; higher gives finer slow control", 1, 5, 0.1, 1),
        Number(DESKTOP, "scroll_speed", "Scroll speed",
               "Wheel clicks per second at full stick", 0.1, 500, 1, 1),
        Number(DESKTOP, "dead_zone", "Dead zone",
               "Ignore the stick this close to centre", 0, 0.9, 0.01),
    )),
    ("Sticks and buttons", (
        Number(DESKTOP, "stick_threshold", "Stick press point",
               "How far a stick goes to count as a direction", 0.2, 0.95, 0.05),
        Number(DESKTOP, "stick_settle", "Stick settle time",
               "Seconds before a flicked stick can fire again", 0, 1, 0.01),
        Number(DESKTOP, "stick_dominance", "Stick dominance",
               "How much one axis must beat the other", 1, 10, 0.1, 1),
        Number(DESKTOP, "button_debounce", "Button debounce",
               "Ignore releases shorter than this, in seconds", 0, 0.5, 0.01),
        Number(GENERAL, "trigger_threshold", "Trigger press point",
               "How far LT and RT are squeezed to count", 0.05, 0.95, 0.05),
    )),
    ("Sound", (
        Number(GENERAL, "volume_step", "Volume step",
               "Percent per press", 1, 100, 1, 0, integer=True),
    )),
    ("On-screen keyboard", (
        Number(OSK, "height", "Size", "Percent of the screen's height",
               8, 70, 1, 0),
        Choice(OSK, "position", "Starts at", "The edge it opens against",
               (("bottom", "Bottom"), ("top", "Top"))),
        Toggle(OSK, "auto_open", "Open by itself",
               "When you click into a text field"),
        Number(OSK, "repeat_delay", "Repeat delay",
               "Seconds a direction is held before it repeats", 0.05, 2, 0.05),
        Number(OSK, "repeat_interval", "Repeat speed",
               "Seconds between repeats; 0 turns repeat off", 0, 1, 0.01),
        Number(OSK, "move_speed", "Move speed",
               "How fast the right stick slides it", 50, 20000, 50, 0),
    )),
    ("Unlock with the pad", (
        Number(UNLOCK, "stick_threshold", "Dial press point",
               "How far the stick goes to aim at a digit", 0.2, 0.95, 0.05),
    )),
)

#: Settings that live on other pages but are fields all the same.
OVERLAY_TIMING: tuple[Field, ...] = (
    Number(WINDOWS, "settle", "Return delay",
           "Seconds between letting go of the pad and refocusing the game",
           0, 5, 0.1, 1),
    Number(WINDOWS, "launch_timeout", "Launch timeout",
           "How long to wait for an app's window", 0, 120, 1, 0),
    Toggle(WINDOWS, "park_pointer", "Park the pointer",
           "Move the cursor onto the overlay (focus-follows-mouse)"),
)

UNLOCK_SWITCH = Toggle(UNLOCK, "enabled", "Unlock with the pad",
                       "Needs a password made only of digits")

THEME = Choice(APPEARANCE, "theme", "Theme", "How this window looks", (
    ("system", "Follow the system"),
    ("dracula", "Dracula"),
    ("night", "Night blue"),
    ("green", "Console green"),
    ("amber", "Warm amber"),
))

GLYPHS = Choice(APPEARANCE, "glyphs", "Button labels",
                "Drawn the way your controller prints them", (
    ("auto", "Automatic"),
    ("xbox", "Xbox"),
    ("playstation", "PlayStation"),
    ("nintendo", "Nintendo"),
))


def default_of(field: Field) -> Any:
    return _attr(config.Config(), field)


def _attr(cfg: config.Config, field: Field) -> Any:
    holder: Any = {
        GENERAL: cfg, DESKTOP: cfg.desktop, OSK: cfg.osk,
        WINDOWS: cfg.windows, UNLOCK: cfg.unlock, APPEARANCE: cfg.appearance,
    }[field.table]
    return getattr(holder, field.key)


# -- bindings ---------------------------------------------------------------
#: The three tables of buttons -> actions, and their defaults.
GUIDE = "bindings"
DESKTOP_MODE = "desktop.bindings"
UNLOCK_MODE = "unlock.bindings"

_TABLES = {
    GUIDE: ("bindings",),
    DESKTOP_MODE: ("desktop", "bindings"),
    UNLOCK_MODE: ("unlock", "bindings"),
}
_DEFAULTS = {
    GUIDE: config.DEFAULT_BINDINGS,
    DESKTOP_MODE: config.DEFAULT_DESKTOP_BINDINGS,
    UNLOCK_MODE: unlock_module.DEFAULT_BINDINGS,
}

#: Display order for buttons, so a list reads like the pad does.
BUTTON_ORDER = (
    "A", "B", "X", "Y", "LB", "RB", "LT", "RT", "START", "SELECT", "L3", "R3",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
    *unlock_module.SECTORS,
)

_ARROWS = {"UP": "↑", "DOWN": "↓", "LEFT": "←", "RIGHT": "→",
           "UP_RIGHT": "↗", "DOWN_RIGHT": "↘", "DOWN_LEFT": "↙", "UP_LEFT": "↖"}


def button_label(name: str) -> str:
    """``LS_UP_RIGHT`` -> ``Left stick ↗``; ``DPAD_UP`` -> ``D-pad ↑``."""
    if name.startswith("DPAD_"):
        return f"D-pad {_ARROWS[name[5:]]}"
    for prefix, stick in (("LS_", "Left stick"), ("RS_", "Right stick")):
        if name.startswith(prefix):
            return f"{stick} {_ARROWS[name[3:]]}"
    return {"START": "Start", "SELECT": "Select", "GUIDE": "Guide"}.get(name, name)


#: Short names for the built-in actions. ``padmode actions`` prints the long
#: help; a row has room for a few words.
ACTION_NAMES = {
    "media.next": "Next track",
    "media.previous": "Previous track",
    "media.play_pause": "Play / pause",
    "media.stop": "Stop",
    "audio.volume_up": "Volume up",
    "audio.volume_down": "Volume down",
    "audio.toggle_mute": "Mute sound",
    "mic.toggle_mute": "Mute microphone",
}


def action_label(kind: str, value: Any) -> str:
    """What a binding does, in words."""
    if kind == UNLOCK_MODE:
        return {"enter": "Try the password", "backspace": "Delete one",
                "clear": "Delete everything"}.get(value, f"Digit {value}")
    if isinstance(value, list):
        return "Run " + " ".join(value)
    if kind == DESKTOP_MODE:
        if value == "osk":
            return "On-screen keyboard"
        if value.startswith("click."):
            return f"{value[6:].capitalize()} click"
        if value.startswith("key."):
            return "Key " + "+".join(k.capitalize() for k in value[4:].split("+"))
        return value
    if value in ACTION_NAMES:
        return ACTION_NAMES[value]
    if value in ACTIONS:
        return ACTIONS[value].help
    overlay = config.overlay_name(value)
    if overlay is not None:
        return f"Open {overlay}"
    return {config.FRONTEND_BINDING: "Go home to the frontend",
            config.OSK_BINDING: "Desktop mode with the keyboard up",
            config.SETTINGS_BINDING: "This settings window"}.get(value, value)


# -- icon tiles ---------------------------------------------------------------
#: Tile colour slots, as numbered in themes.TILE_KINDS.
MEDIA, SOUND, APPS, POINTER, KEYBOARD, SYSTEM, LOCK, HOME = range(1, 9)

_ACTION_ICONS = {
    "media.next": ("media-skip-forward-symbolic", MEDIA),
    "media.previous": ("media-skip-backward-symbolic", MEDIA),
    "media.play_pause": ("media-playback-start-symbolic", MEDIA),
    "media.stop": ("media-playback-stop-symbolic", MEDIA),
    "audio.volume_up": ("audio-volume-high-symbolic", SOUND),
    "audio.volume_down": ("audio-volume-low-symbolic", SOUND),
    "audio.toggle_mute": ("audio-volume-muted-symbolic", SOUND),
    "mic.toggle_mute": ("microphone-sensitivity-muted-symbolic", SOUND),
    config.FRONTEND_BINDING: ("go-home-symbolic", HOME),
    config.OSK_BINDING: ("input-keyboard-symbolic", KEYBOARD),
    config.SETTINGS_BINDING: ("emblem-system-symbolic", SYSTEM),
}


def icon_for_binding(kind: str, value: Any) -> tuple[str, int]:
    """``(icon name, tile colour)`` for what a binding does."""
    if kind == UNLOCK_MODE:
        if value == "enter":
            return "go-jump-symbolic", LOCK
        if value in ("backspace", "clear"):
            return "edit-clear-symbolic", LOCK
        return "system-lock-screen-symbolic", LOCK
    if isinstance(value, list):
        return "utilities-terminal-symbolic", SYSTEM
    if kind == DESKTOP_MODE:
        if value == "osk":
            return "input-keyboard-symbolic", KEYBOARD
        if value.startswith("click."):
            return "input-mouse-symbolic", POINTER
        return "input-keyboard-symbolic", KEYBOARD
    if config.overlay_name(value) is not None:
        return "view-app-grid-symbolic", APPS
    return _ACTION_ICONS.get(value, ("input-gaming-symbolic", SYSTEM))


def icon_for_field(field: "Field") -> tuple[str, int]:
    """``(icon name, tile colour)`` for a number, switch or choice."""
    if field.table == APPEARANCE:
        return (("preferences-desktop-appearance-symbolic", SYSTEM)
                if field.key == "theme" else ("input-gaming-symbolic", KEYBOARD))
    if field.table == OSK:
        return "input-keyboard-symbolic", KEYBOARD
    if field.table == UNLOCK:
        return "system-lock-screen-symbolic", LOCK
    if field.table == WINDOWS:
        return "preferences-system-time-symbolic", APPS
    if field.table == GENERAL and field.key == "volume_step":
        return "audio-volume-high-symbolic", SOUND
    if field.key in ("pointer_speed", "curve", "scroll_speed", "dead_zone"):
        return "input-mouse-symbolic", POINTER
    return "input-gaming-symbolic", POINTER


def sort_buttons(names) -> list[str]:
    order = {name: i for i, name in enumerate(BUTTON_ORDER)}
    return sorted(names, key=lambda n: (order.get(n, len(order)), n))


#: The two unlock layouts the page offers outright (docs/usage.md).
XBOX_UNLOCK = {
    "DPAD_UP": "1", "DPAD_LEFT": "2", "DPAD_DOWN": "3", "DPAD_RIGHT": "4",
    "LT": "5", "RT": "6", "LB": "7", "RB": "8", "Y": "9", "X": "0",
    **{f"LS_{s}": False for s in ("UP", "UP_RIGHT", "RIGHT", "DOWN_RIGHT",
                                   "DOWN", "DOWN_LEFT", "LEFT", "UP_LEFT")},
}


# -- the session ------------------------------------------------------------
class Session:
    """The config file as the window is editing it."""

    def __init__(self, path: Path | None = None, service=None) -> None:
        self.path = path or config.default_path()
        #: The systemd service (:class:`padmode.settings.service.Service`), or
        #: None to leave it alone -- the tests, and padmode run by hand.
        self.service = service
        #: Start at login: what it is, and what Apply will make it. None when
        #: there is no service to switch.
        self.autostart_saved = service.autostart() if service is not None else None
        self.autostart = self.autostart_saved
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        #: Raises :class:`EditError` for a file written in a shape the editor
        #: will not touch; the window says so instead of opening.
        self.doc = Document(text)
        self.saved = text
        self.config = config.Config()
        #: Why Apply would be refused right now, or None. A file that is
        #: already broken on disk opens anyway, with this set: the window is
        #: the easiest place to fix it.
        self.problem: str | None = None
        self._settle()

    @property
    def dirty(self) -> bool:
        return self.doc.text != self.saved or self.autostart != self.autostart_saved

    def set_autostart(self, on: bool) -> None:
        """Pending, like every other change: Apply makes it so."""
        if self.autostart_saved is not None:
            self.autostart = on

    # -- plain fields --------------------------------------------------
    def value(self, field: Field) -> Any:
        return _attr(self.config, field)

    def set(self, field: Field, value: Any) -> None:
        if isinstance(field, Number):
            value = int(round(value)) if field.integer else round(float(value), 6)
            value = min(max(value, field.minimum), field.maximum)
        if value == default_of(field):
            self.doc.remove(field.table, field.key)
        else:
            self.doc.set(field.table, field.key, value)
        self._settle()

    def reset(self, field: Field) -> None:
        self.doc.remove(field.table, field.key)
        self._settle()

    # -- bindings ------------------------------------------------------
    def bindings(self, kind: str) -> dict[str, Any]:
        """Button -> what it does, defaults included, as the daemon sees it."""
        if kind == GUIDE:
            return dict(self.config.bindings)
        if kind == DESKTOP_MODE:
            return dict(self.config.desktop.bindings)
        return dict(self.config.unlock.bindings)

    def bind(self, kind: str, button: str, value: Any) -> None:
        self._bind(kind, button, value)
        self._settle()

    def unbind(self, kind: str, button: str) -> None:
        self._unbind(kind, button)
        self._settle()

    def move(self, kind: str, old: str, new: str) -> tuple[str, Any] | None:
        """Put what ``old`` does onto ``new``. Returns what ``new`` lost, if
        anything, so the window can say so -- nothing is dropped silently."""
        if old == new:
            return None
        current = self.bindings(kind)
        value = current[old]
        lost = current.get(new)
        self._bind(kind, new, value)
        self._unbind(kind, old)
        self._settle()
        return (new, lost) if lost is not None and lost is not False else None

    def unlock_layout(self, layout: str) -> None:
        """``dial`` (the defaults) or ``xbox``: replace the whole table."""
        table = _TABLES[UNLOCK_MODE]
        self.doc.remove_table(table)
        if layout == "xbox":
            for button, value in XBOX_UNLOCK.items():
                self.doc.set(table, button, value)
        self._settle()

    def reset_bindings(self, kind: str) -> None:
        self.doc.remove_table(_TABLES[kind])
        self._settle()

    def _bind(self, kind: str, button: str, value: Any) -> None:
        table = _TABLES[kind]
        if _DEFAULTS[kind].get(button) == value:
            self.doc.remove(table, button)
        else:
            self.doc.set(table, button, value)

    def _unbind(self, kind: str, button: str) -> None:
        table = _TABLES[kind]
        if button in _DEFAULTS[kind]:
            self.doc.set(table, button, False)
        else:
            self.doc.remove(table, button)

    # -- overlays and the frontend --------------------------------------
    def set_overlay(self, name: str, window_class: str, command: list[str] | None) -> None:
        table = ("overlays", name)
        self.doc.set(table, "class", window_class)
        if command:
            self.doc.set(table, "command", command)
        else:
            self.doc.remove(table, "command")
        self._settle()

    def remove_overlay(self, name: str) -> None:
        """The overlay, and every button that opened it -- otherwise the file
        would name an overlay that no longer exists, which the daemon refuses."""
        for button, value in self.bindings(GUIDE).items():
            if config.overlay_name(value) == name:
                self._unbind(GUIDE, button)
        self.doc.remove_table(("overlays", name))
        self._settle()

    def set_frontend(self, window_class: str, command: list[str] | None) -> None:
        self.doc.set(("frontend",), "class", window_class)
        if command:
            self.doc.set(("frontend",), "command", command)
        else:
            self.doc.remove(("frontend",), "command")
        self._settle()

    def remove_frontend(self) -> None:
        for button, value in self.bindings(GUIDE).items():
            if config.is_frontend(value):
                self._unbind(GUIDE, button)
        self.doc.remove_table(("frontend",))
        self._settle()

    # -- the Steam ignore list -----------------------------------------
    def ignore(self) -> dict[int, bool]:
        data = self.doc.data
        table = data.get("metadata", {}).get("steam", {}).get("ignore", {})
        return {int(k): bool(v) for k, v in table.items()}

    def set_hidden(self, appid: int, hidden: bool | None) -> None:
        """Hide, show, or (None) go back to what padmode would decide."""
        table = ("metadata", "steam", "ignore")
        if hidden is None:
            self.doc.remove(table, str(appid))
        else:
            self.doc.set(table, str(appid), hidden)
        self._settle()

    # -- Apply ---------------------------------------------------------
    def apply(self) -> config.Config:
        """Write the file, and switch start-at-login if that changed.

        Raises :class:`padmode.config.ConfigError` if the daemon would refuse
        the file -- nothing is written or switched then -- or if systemctl
        refuses, after the file is safely written.
        """
        if self.doc.text != self.saved:
            self.config = config.write(self.doc.text, self.path)
            self.saved = self.doc.text
        if self.autostart != self.autostart_saved and self.service is not None:
            error = self.service.set_autostart(bool(self.autostart))
            if error:
                raise config.ConfigError(f"could not change start at login: {error}")
            self.autostart_saved = self.autostart
        return self.config

    def discard(self) -> None:
        self.doc = Document(self.saved)
        self.autostart = self.autostart_saved
        self._settle()

    def _settle(self) -> None:
        """Re-read what the edits add up to. Invalid is allowed, and named."""
        try:
            self.config = config.check_text(self.doc.text)
            self.problem = None
        except config.ConfigError as exc:
            self.problem = str(exc)


def steam_apps() -> list[tuple[Any, str | None]]:
    """Installed Steam apps and why padmode would hide each, ignoring the
    user's list -- so the page can show what a "default" choice means.
    Empty when there is no Steam."""
    from ..metadata import steam  # never on the daemon's import path (§2)

    steam_dir = steam.find_steam_dir()
    if steam_dir is None:
        return []
    apps = [a for a in steam.installed_apps(steam_dir) if a.installed]
    return [(app, steam.skip_reason(app, {})) for app in
            sorted(apps, key=lambda a: a.name.lower())]


__all__ = [
    "Choice", "EditError", "FEEL", "Field", "Number", "Session", "Toggle",
    "action_label", "button_label", "default_of", "sort_buttons",
    "steam_apps",
]
