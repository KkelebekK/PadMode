"""Device I/O: reading the real gamepad (and, from v0.2, writing a virtual one)."""

from .reader import (
    TICK,
    GamepadReader,
    find_gamepad,
    is_virtual,
    looks_like_gamepad,
)
from .virtual import VirtualInput

__all__ = [
    "TICK",
    "GamepadReader",
    "VirtualInput",
    "find_gamepad",
    "is_virtual",
    "looks_like_gamepad",
]
