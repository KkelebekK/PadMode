"""The built-in action registry.

Every action is a *description* of a command to run -- no side effects live
here, so the whole registry can be unit-tested by asserting on generated argv.
:mod:`padmode.actions.runner` is the only thing that actually spawns anything.

This module must not import :mod:`padmode.config`; config imports *this* to
validate action ids, and the dependency has to point one way. That is why
builders take an :class:`ActionContext` rather than a ``Config``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ActionContext:
    """The slice of configuration that built-in actions actually need."""

    volume_step: int = 5


@dataclass(frozen=True)
class ActionSpec:
    """A built-in action: which external command it needs, and how to call it."""

    #: External binary that must be present for this action to work at all.
    binary: str
    #: Builds the full argv for the action.
    build: Callable[[ActionContext], list[str]]
    #: One-line description, used by ``padmode actions``.
    help: str


def _playerctl(*args: str) -> Callable[[ActionContext], list[str]]:
    return lambda _ctx: ["playerctl", *args]


def _sink_volume(direction: str) -> Callable[[ActionContext], list[str]]:
    # `-l 1.0` caps the sink at 100%. PipeWire will happily amplify past that
    # and it sounds terrible, so raising volume is clamped; lowering needs no
    # limit and passing one there would be a no-op anyway.
    def build(ctx: ActionContext) -> list[str]:
        step = f"{ctx.volume_step}%{direction}"
        limit = ["-l", "1.0"] if direction == "+" else []
        return ["wpctl", "set-volume", *limit, "@DEFAULT_SINK@", step]

    return build


#: Action id -> spec. Ids are the strings users write in their config.
ACTIONS: dict[str, ActionSpec] = {
    "media.next": ActionSpec(
        binary="playerctl",
        build=_playerctl("next"),
        help="Skip to the next track",
    ),
    "media.previous": ActionSpec(
        binary="playerctl",
        build=_playerctl("previous"),
        help="Go back to the previous track",
    ),
    "media.play_pause": ActionSpec(
        binary="playerctl",
        build=_playerctl("play-pause"),
        help="Toggle play/pause",
    ),
    "media.stop": ActionSpec(
        binary="playerctl",
        build=_playerctl("stop"),
        help="Stop playback",
    ),
    "audio.volume_up": ActionSpec(
        binary="wpctl",
        build=_sink_volume("+"),
        help="Raise output volume by general.volume_step percent",
    ),
    "audio.volume_down": ActionSpec(
        binary="wpctl",
        build=_sink_volume("-"),
        help="Lower output volume by general.volume_step percent",
    ),
    "audio.toggle_mute": ActionSpec(
        binary="wpctl",
        build=lambda _ctx: ["wpctl", "set-mute", "@DEFAULT_SINK@", "toggle"],
        help="Mute or unmute audio output",
    ),
    "mic.toggle_mute": ActionSpec(
        binary="wpctl",
        build=lambda _ctx: ["wpctl", "set-mute", "@DEFAULT_SOURCE@", "toggle"],
        help="Mute or unmute the microphone",
    ),
}
