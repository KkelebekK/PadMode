"""Device I/O: reading the real gamepad (and, from v0.2, writing a virtual one)."""

from .reader import (
    TICK,
    WAKE,
    GamepadReader,
    find_gamepad,
    is_virtual,
    looks_like_gamepad,
)
from .virtual import DigitKeypad, VirtualInput

__all__ = [
    "DigitKeypad",
    "TICK",
    "WAKE",
    "GamepadReader",
    "VirtualInput",
    "find_gamepad",
    "is_virtual",
    "looks_like_gamepad",
]
