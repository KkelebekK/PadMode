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
import os
import re
import select
import selectors
import time
from collections.abc import Iterator

from evdev import InputDevice, ecodes, list_devices

from .. import buttons

log = logging.getLogger(__name__)


#: Yielded when the tick deadline passes, in place of an event.
#:
#: Desktop mode needs a steady heartbeat because an analogue stick reports a
#: *position*: holding it at full deflection produces no further events, so
#: without a tick the pointer would move only while the stick was moving.
#:
#: It is ``None`` rather than a sentinel class so that the pure state machine
#: in ``daemon.py`` can recognise a tick without importing anything from this
#: module, which would drag evdev into the one part of the app that must stay
#: testable without it. Events are always 3-tuples, so there is no ambiguity.
TICK = None

#: Yielded when a watched file descriptor (:meth:`GamepadReader.watch`) has
#: something to read. An empty tuple for the same reason ``TICK`` is ``None``:
#: recognisable without importing this module, and never mistaken for an
#: event, which is always three integers.
WAKE = ()

#: Devices we create ourselves (v0.2+) are named with this prefix so that we
#: never grab our own virtual pad and feed our output back into our input.
VIRTUAL_PREFIX = "padmode"

#: Where the kernel publishes every input node, and its parentage with it.
#:
#: A node created through uinput has no parent device -- the kernel files it
#: under ``/sys/devices/virtual/input`` and ``<node>/device/device`` does not
#: exist. Hardware always has a parent: the USB interface, the i2c client, or
#: the uhid device a Bluetooth pad arrives on. Parentage, not the path: a
#: Bluetooth pad lives under ``/sys/devices/virtual`` too (see ``is_virtual``).
SYSFS_INPUT = "/sys/class/input"

Event = tuple[int, int, int]


def is_virtual(device: InputDevice) -> bool:
    """True if this node came from uinput rather than from hardware.

    Steam publishes a virtual twin of every controller you connect, named
    ``Microsoft X-Box 360 pad <n>``, and on a machine where Steam running is
    the normal case -- ours, by design -- that twin is the one we kept
    choosing. It never emits an event, so every binding silently did nothing
    and no error appeared anywhere.

    Measured, rather than assumed, about the twin: it appears within a second
    of a pad being connected (creating a synthetic pad reliably produced a
    second one, ``pad 1``), and it disappears again when that pad is
    unplugged. So it shadows real hardware rather than standing in for it.

    Measured 2026-09-21, KDE Wayland, Steam idle in the tray, both nodes read
    at once for 25 seconds with nothing holding a grab: ``event28`` (the real
    pad, ``phys='usb-0000:05:00.3-1/input0'``) delivered **36558 events**, and
    ``event256`` (Steam's, ``phys=''``) delivered **zero**.

    The test is the kernel's own statement of parentage, and deliberately not
    anything the creating process chose for itself. Three weaker signals were
    checked on the same machine and rejected:

    * **The name.** Steam's pad is named like real hardware. There is nothing
      to match on, and CLAUDE.md §5 says so already for our own devices.
    * **An empty ``phys``.** True of Steam's pad, but it is not the rule: our
      own virtual devices report ``py-evdev-uinput``, which is python-evdev's
      default, so any uinput pad built with the very library we use would pass
      the test. An empty ``phys`` is a symptom of Steam's choices, and Steam
      can change it without telling us.
    * **A sysfs path under ``/sys/devices/virtual``.** The obvious reading of
      the rule above, and wrong. Measured by creating a Bluetooth-bus HID pad
      through ``/dev/uhid``: it is published at
      ``/sys/devices/virtual/misc/uhid/0005:1234:5678.0021/input/input321`` --
      *under* ``/devices/virtual``, because uhid is itself a virtual misc
      device. Matching on the path would reject every Bluetooth controller.
      It has a parent (the uhid device), so parentage classifies it correctly.

    The inverse error exists but does not reach us: a few real devices also
    have no parent -- this machine's ``HP WMI hotkeys`` is one -- and none of
    them are gamepads. If one ever is, ``device.path`` overrides outright.

    Fails open. If sysfs is not mounted we cannot tell, and a device we cannot
    classify is better treated as real than quietly demoted.
    """
    return _is_virtual_node(device.path)


def _is_virtual_node(path: str) -> bool:
    """:func:`is_virtual`, answered from the path alone.

    Kept separate so that ranking never has to open anything. Measured on this
    machine: reading parentage for all 33 nodes costs **0.6 ms**, while opening
    each one and asking for its capabilities costs **118 ms** -- and discovery
    runs again on every reconnect.
    """
    node = os.path.basename(os.path.realpath(path))
    published = os.path.join(SYSFS_INPUT, node)
    if not os.path.isdir(published):
        return False
    return not os.path.exists(os.path.join(published, "device", "device"))


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


def _axis_ranges(device: InputDevice) -> dict[int, tuple[int, int]]:
    """Read every absolute axis range the device advertises."""
    try:
        caps = device.capabilities().get(ecodes.EV_ABS, ())
    except OSError:
        return {}
    return {code: (info.min, info.max) for code, info in caps}


def _trigger_axes(device: InputDevice) -> dict[int, int]:
    """Trigger axes that should stand in for buttons, as axis code -> key code.

    Most pads report the triggers only as ``ABS_Z``/``ABS_RZ`` and never emit
    ``BTN_TL2``/``BTN_TR2``, so without this ``LT`` and ``RT`` are unbindable.
    A few report both, and translating those would deliver every trigger press
    twice -- so a trigger is translated only when its button is absent.
    """
    try:
        caps = device.capabilities()
    except OSError:
        return {}
    keys = set(caps.get(ecodes.EV_KEY, ()))
    axes = {code for code, _ in caps.get(ecodes.EV_ABS, ())}
    translated = {}
    for axis, name in buttons.TRIGGERS.items():
        code = buttons.BUTTONS[name]
        if axis in axes and code not in keys:
            translated[axis] = code
    return translated


def _node_key(path: str) -> tuple[int, str]:
    """Sort key ordering event9 before event28, and event28 before event256.

    ``sorted()`` over the raw paths is a *string* sort, so "event256" lands
    ahead of "event28" because '2' < '8'. Device order was therefore arbitrary
    above event9, which is how Steam's pad came to be tried first -- though
    only by accident, since Steam's node could just as easily have been the
    lower number. The ordering is worth fixing on its own; it is not the fix.
    """
    match = re.search(r"(\d+)$", path)
    return (int(match.group(1)) if match else -1, path)


def _ranked_paths() -> list[str]:
    """Every node path, real hardware first, each group in node order.

    The whole preference lives here, decided from sysfs before a single device
    is opened -- so the first candidate that matches is already the best one
    and the search can stop there, exactly as it used to.
    """
    physical: list[str] = []
    virtual: list[str] = []
    for path in sorted(list_devices(), key=_node_key):
        (virtual if _is_virtual_node(path) else physical).append(path)
    return physical + virtual


def find_gamepad(name: str | None = None, path: str | None = None) -> InputDevice | None:
    """Locate the gamepad to use.

    ``path`` wins outright if set (an explicit override is an explicit
    override). Otherwise every capability-matching device is ranked -- **real
    hardware first, virtual devices only as a fallback**, each group in node
    order -- and the best one is returned, filtered by a case-insensitive
    substring of ``name`` when one is configured.

    Ranked rather than filtered, for two reasons. The synthetic pad the
    integration tests create is itself a uinput device (CLAUDE.md §5), so
    refusing virtual devices outright would take the project's entire
    device-discovery test strategy with it. And a virtual pad we probably
    cannot use is still a better answer than no pad at all -- provided we say
    so, which is why landing on one is a warning rather than silence.
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
    for path in _ranked_paths():
        try:
            device = InputDevice(path)
        except OSError:
            continue  # raced with an unplug, or we lack permission
        if (device.name.startswith(VIRTUAL_PREFIX)
                or not looks_like_gamepad(device)
                or (needle and needle not in device.name.lower())):
            # Closed rather than abandoned: discovery runs on every reconnect,
            # and a leaked descriptor per rejected device adds up for good.
            device.close()
            continue
        if _is_virtual_node(path):
            # Reached only when no real pad matched, since they are ranked
            # first -- so this is the fallback, and it has to be audible.
            log.warning(
                "%s (%r) is a virtual device -- there is no hardware behind "
                "it, only another program. If no button does anything, that "
                "is why; run 'padmode devices'.", device.path, device.name)
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
        self._tick_interval: float | None = None
        self._axis_ranges: dict[int, tuple[int, int]] = {}
        self._trigger_axes: dict[int, int] = {}
        #: Other descriptors the event loop waits on beside the pad's own.
        self._watched: set[int] = set()
        #: The selector of the loop now running, so a descriptor dropped
        #: mid-loop stops being selected at once -- a closed pipe left
        #: registered would read as forever readable.
        self._selector: selectors.BaseSelector | None = None

    @property
    def axis_ranges(self) -> dict[int, tuple[int, int]]:
        """Per-axis ``(min, max)`` for the open device.

        Stick ranges are a property of the hardware, so they are read from the
        device rather than assumed. Empty until a device is open.
        """
        return self._axis_ranges

    @property
    def trigger_axes(self) -> dict[int, int]:
        """Trigger axes to translate into button presses, axis -> key code.

        Empty for a pad whose triggers are already buttons, and empty until
        a device is open.
        """
        return self._trigger_axes

    def set_tick(self, interval: float | None) -> None:
        """Start or stop the heartbeat, in seconds; None blocks indefinitely.

        Dynamic rather than fixed so we only wake up 60 times a second while
        desktop mode is actually active. Ticking constantly would keep a laptop
        from ever settling, for a feature most users are not using right now.
        """
        self._tick_interval = interval

    def watch(self, fd: int) -> None:
        """Also wake on ``fd``: :meth:`events` yields ``WAKE`` when it is readable.

        For things the daemon listens to besides the pad -- the lock-screen
        monitor is the first. Nothing is read from ``fd`` here; the owner
        drains it. Watched descriptors are heard while no pad is connected
        too: the settings window's Apply must not wait for a controller.
        """
        if fd in self._watched:
            return
        self._watched.add(fd)
        if self._selector is not None:
            self._selector.register(fd, selectors.EVENT_READ)

    def unwatch(self, fd: int) -> None:
        if fd not in self._watched:
            return
        self._watched.discard(fd)
        if self._selector is not None:
            try:
                self._selector.unregister(fd)
            except (KeyError, ValueError):
                pass

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

    def events(self) -> Iterator[Event | None]:
        """Infinite stream of ``(type, code, value)`` and ticks, across reconnects.

        Never returns normally. Exits by exception -- ``SystemExit`` from the
        signal handler, or ``GeneratorExit`` if the consumer stops early -- and
        releases the grab on the way out either way.
        """
        try:
            while not self._stop:
                device = self._open()
                if device is None:
                    yield from self._wait(self._retry_delay)
                    continue
                log.info("listening on %s (%s)", device.path, device.name)
                try:
                    yield from self._pump(device)
                except OSError as exc:
                    log.info("%s went away (%s); waiting for it to come back",
                             device.path, exc)
                finally:
                    self._close()
                if not self._stop:
                    yield from self._wait(self._retry_delay)
        finally:
            self._close()

    def _wait(self, seconds: float) -> Iterator[Event | None]:
        """Sit out ``seconds`` with no pad, still answering watched descriptors.

        Waiting by sleeping was fine while the pad was all there was. The
        settings window's Apply must work with the controller switched off --
        it is a window, and a mouse can click it -- so the control socket has
        to be heard here too. Measured: with ``time.sleep`` a reload sent while
        no pad was connected waited forever.
        """
        deadline = time.monotonic() + seconds
        while not self._stop:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            if not self._watched:
                time.sleep(left)
                return
            ready, _, _ = select.select(list(self._watched), [], [], left)
            if ready:
                yield WAKE

    def _pump(self, device: InputDevice) -> Iterator[Event | None]:
        """Yield events, interleaving ticks on a fixed schedule when enabled.

        The deadline is tracked explicitly instead of relying on ``select``
        timing out. Otherwise a stick being actively waggled produces a steady
        stream of events, ``select`` never times out, and ticks stop arriving
        precisely when pointer motion is most needed.
        """
        selector = selectors.DefaultSelector()
        pad = device.fileno()
        selector.register(pad, selectors.EVENT_READ)
        for fd in self._watched:
            selector.register(fd, selectors.EVENT_READ)
        self._selector = selector
        deadline: float | None = None
        try:
            while not self._stop:
                interval = self._tick_interval
                if interval is None:
                    deadline, timeout = None, None
                else:
                    now = time.monotonic()
                    if deadline is None:
                        deadline = now + interval
                    timeout = max(0.0, deadline - now)

                ready = {key.fd for key, _ in selector.select(timeout)}
                if pad in ready:
                    try:
                        for event in device.read():
                            yield (event.type, event.code, event.value)
                            if self._stop:
                                return
                    except BlockingIOError:
                        pass  # raced with another reader; nothing buffered
                if ready - {pad}:
                    yield WAKE

                if deadline is not None and time.monotonic() >= deadline:
                    yield TICK
                    # Re-base rather than accumulate, so a slow tick never
                    # queues a burst of catch-up ticks.
                    deadline = max(time.monotonic(), deadline + interval)
        finally:
            self._selector = None
            selector.close()

    def _open(self) -> InputDevice | None:
        device = find_gamepad(self._name, self._path)
        if device is None:
            log.debug("no gamepad found; retrying in %.1fs", self._retry_delay)
        self._device = device
        self._axis_ranges = _axis_ranges(device) if device else {}
        self._trigger_axes = _trigger_axes(device) if device else {}
        return device

    def _close(self) -> None:
        self.ungrab()
        if self._device is not None:
            try:
                self._device.close()
            except OSError:
                pass
            self._device = None
