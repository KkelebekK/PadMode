"""Finds the gamepad, reads it, and survives it going away.

Two rules from CLAUDE.md drive this whole module:

* **Never hardcode a device.** Match by capability, with a config override.
  The old setup pinned ``/dev/input/event3``; on this very machine that path is
  now the ``Video Bus``. Enumeration is not optional.
* **Gamepads re-enumerate.** Wireless pads sleep, USB pads get replugged, and
  the kernel hands out a different event number each time. Losing the device is
  a normal Tuesday, not an error -- reconnect instead of exiting.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

from evdev import InputDevice, ecodes, list_devices

log = logging.getLogger(__name__)

#: Devices we create ourselves (v0.2+) are named with this prefix so that we
#: never grab our own virtual pad and feed our output back into our input.
VIRTUAL_PREFIX = "padmode"

Event = tuple[int, int, int]


def looks_like_gamepad(device: InputDevice) -> bool:
    """True if a device has the capabilities of a gamepad.

    The test is "reports a south face button and at least one absolute axis".
    That accepts every pad worth supporting while rejecting keyboards, mice and
    the pile of pseudo-devices every laptop exposes.
    """
    try:
        caps = device.capabilities()
    except OSError:
        return False
    keys = set(caps.get(ecodes.EV_KEY, ()))
    axes = {code for code, _ in caps.get(ecodes.EV_ABS, ())}
    return ecodes.BTN_SOUTH in keys and bool(axes)


def _candidates() -> Iterator[InputDevice]:
    """Every readable input device, in stable order."""
    for path in sorted(list_devices()):
        try:
            yield InputDevice(path)
        except OSError:
            continue  # raced with an unplug, or we lack permission


def find_gamepad(name: str | None = None, path: str | None = None) -> InputDevice | None:
    """Locate the gamepad to use.

    ``path`` wins outright if set (an explicit override is an explicit
    override). Otherwise the first capability-matching device is used, filtered
    by a case-insensitive substring of ``name`` when one is configured.
    """
    if path:
        try:
            device = InputDevice(path)
        except OSError as exc:
            log.warning("device.path %s is not usable: %s", path, exc)
            return None
        if not looks_like_gamepad(device):
            log.warning("device.path %s (%r) is not a gamepad", path, device.name)
        return device

    needle = name.lower() if name else None
    for device in _candidates():
        if device.name.startswith(VIRTUAL_PREFIX):
            continue
        if not looks_like_gamepad(device):
            continue
        if needle and needle not in device.name.lower():
            continue
        return device
    return None


class GamepadReader:
    """Yields raw events from the gamepad, reconnecting as needed.

    Grab state is tracked here rather than by the caller, so a double grab or a
    double release is harmless. That matters because the release path runs from
    a ``finally`` that may fire when we were never grabbed at all.
    """

    def __init__(
        self,
        name: str | None = None,
        path: str | None = None,
        retry_delay: float = 2.0,
    ) -> None:
        self._name = name
        self._path = path
        self._retry_delay = retry_delay
        self._device: InputDevice | None = None
        self._grabbed = False
        self._stop = False

    @property
    def grabbed(self) -> bool:
        return self._grabbed

    def stop(self) -> None:
        """Ask the event loop to finish.

        Takes effect once the current blocking read returns -- so within
        ``retry_delay`` if no pad is connected, or as soon as the next event
        arrives if one is. Shutdown normally happens by signal instead; this
        exists for callers that own the loop, such as the integration tests.
        """
        self._stop = True

    @property
    def device(self) -> InputDevice | None:
        return self._device

    def grab(self) -> None:
        """Take exclusive control, so events stop reaching the running game."""
        if self._grabbed or self._device is None:
            return
        try:
            self._device.grab()
            self._grabbed = True
        except OSError as exc:
            # Another process holds the grab. Not fatal: we simply stay in
            # read-only mode, and the user's presses also reach the game.
            log.warning("could not grab %s: %s", self._device.path, exc)

    def ungrab(self) -> None:
        """Hand the pad back. Safe to call at any time, including twice."""
        if not self._grabbed:
            return
        self._grabbed = False
        if self._device is None:
            return
        try:
            self._device.ungrab()
        except OSError:
            # The device vanished while we held it; the grab died with it.
            pass

    def events(self) -> Iterator[Event]:
        """Infinite stream of ``(type, code, value)``, across reconnects.

        Never returns normally. Exits by exception -- ``SystemExit`` from the
        signal handler, or ``GeneratorExit`` if the consumer stops early -- and
        releases the grab on the way out either way.
        """
        try:
            while not self._stop:
                device = self._open()
                if device is None:
                    time.sleep(self._retry_delay)
                    continue
                log.info("listening on %s (%s)", device.path, device.name)
                try:
                    for event in device.read_loop():
                        yield (event.type, event.code, event.value)
                        if self._stop:
                            return
                except OSError as exc:
                    log.info("%s went away (%s); waiting for it to come back",
                             device.path, exc)
                finally:
                    self._close()
                if not self._stop:
                    time.sleep(self._retry_delay)
        finally:
            self._close()

    def _open(self) -> InputDevice | None:
        device = find_gamepad(self._name, self._path)
        if device is None:
            log.debug("no gamepad found; retrying in %.1fs", self._retry_delay)
        self._device = device
        return device

    def _close(self) -> None:
        self.ungrab()
        if self._device is not None:
            try:
                self._device.close()
            except OSError:
                pass
            self._device = None
