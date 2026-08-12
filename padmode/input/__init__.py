"""Device I/O: reading the real gamepad (and, from v0.2, writing a virtual one)."""

from .reader import GamepadReader, find_gamepad, looks_like_gamepad

__all__ = ["GamepadReader", "find_gamepad", "looks_like_gamepad"]
