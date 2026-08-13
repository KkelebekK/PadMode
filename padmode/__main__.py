"""Command line entry point.

Logging goes to stderr and journald picks it up. No log files (CLAUDE.md §4).
"""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import sys
from pathlib import Path

from . import __version__
from .actions import ACTIONS, ActionContext, ActionRunner
from .config import Config, ConfigError, default_path, load
from .daemon import Daemon

log = logging.getLogger("padmode")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _on_sigterm(_signum: int, _frame) -> None:
    # Raising here unwinds the read loop through its `finally`, which is what
    # releases the grab. Exiting any other way risks leaving the pad captured.
    raise SystemExit(0)


def cmd_run(args: argparse.Namespace, config: Config) -> int:
    from .input import GamepadReader, VirtualInput
    from .windows import detect

    signal.signal(signal.SIGTERM, _on_sigterm)
    reader = GamepadReader(name=config.device_name, path=config.device_path)
    runner = ActionRunner(ActionContext(volume_step=config.volume_step))
    detection = detect(config.windows.backend)
    # Worth a line only if something here uses windows; otherwise it is noise
    # for the majority who run v0.1 and v0.2 features on any desktop.
    log.log(
        logging.INFO if config.overlays else logging.DEBUG,
        "window backend: %s", detection.reason,
    )
    # Created here but not opened: the uinput devices appear on first entry to
    # desktop mode, so someone who never uses it gets no phantom hardware.
    # Overlays need them too, even with desktop mode switched off.
    virtual = VirtualInput() if config.desktop.enabled or config.overlays else None
    try:
        Daemon(config, reader, runner, virtual, detection.backend).run()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_windows(args: argparse.Namespace, config: Config) -> int:
    """Show the window backend and check each overlay can find its app."""
    from .windows import detect

    detection = detect(config.windows.backend)
    backend = detection.backend
    print(f"backend: {backend.name if backend else 'none'}")
    print(f"  {detection.reason}")
    print()
    for tool in ("kdotool", "xdotool"):
        print(f"  {tool:9} {shutil.which(tool) or '(not installed)'}")

    if backend is None:
        print()
        print("Overlays need a window backend. Everything else works without one.")
        return 1 if config.overlays else 0

    active = backend.get_active_window()
    print(f"\nactive window: {active or '(none)'}")
    if active is not None:
        # The answer to "what do I write in class = ?". Focus the app you want
        # to overlay, run this from a terminal on another screen or a delay.
        print(f"  class: {backend.window_class(active) or '(unknown)'}")

    if not config.overlays:
        print("\nNo overlays configured. To add one:\n")
        print("  [overlays.discord]")
        print('  class = "discord"')
        print('  command = ["discord"]\n')
        print("  [bindings]")
        print('  B = "overlay.discord"')
        return 0

    print("\noverlays:")
    stuck = 0
    for name, overlay in sorted(config.overlays.items()):
        window = backend.find_window(overlay.window_class)
        if window is not None:
            state = f"found {window}"
        elif overlay.command:
            state = (
                f"not running; would start {' '.join(overlay.command)} "
                f"and wait for class {overlay.window_class!r}"
            )
        else:
            # No window and no way to make one: this button cannot work now.
            stuck += 1
            state = f"no window matching class {overlay.window_class!r}, and no command"
        print(f"  {name:12} {state}")
    return 1 if stuck else 0


def cmd_devices(args: argparse.Namespace, config: Config) -> int:
    """List input devices and show which one we would pick, and why."""
    from evdev import InputDevice, list_devices

    from .input import find_gamepad, looks_like_gamepad

    chosen = find_gamepad(config.device_name, config.device_path)
    chosen_path = chosen.path if chosen else None

    found = False
    for path in sorted(list_devices()):
        try:
            device = InputDevice(path)
        except OSError as exc:
            print(f"    {path:20} <unreadable: {exc}>")
            continue
        if not looks_like_gamepad(device):
            continue
        found = True
        marker = "==>" if path == chosen_path else "   "
        print(f"{marker} {path:20} {device.name!r}")

    if not found:
        print("No gamepads detected.")
        print()
        print("If one is plugged in, you may not have permission to read it.")
        print("Check that you are in the 'input' group:  id -nG")
        return 1
    if chosen is None:
        print()
        print(f"None matched device.name = {config.device_name!r}.")
        return 1
    print()
    print("'==>' is the device padmode will use.")
    print("To pin it, put this in", default_path())
    print(f'\n[device]\nname = "{chosen.name}"')
    return 0


def cmd_actions(args: argparse.Namespace, config: Config) -> int:
    """List built-in actions and whether their external command is installed."""
    runner = ActionRunner(ActionContext(volume_step=config.volume_step))
    width = max(len(name) for name in ACTIONS)
    for name, spec in sorted(ACTIONS.items()):
        ok = "" if runner.available(spec.binary) else f"  [{spec.binary} NOT INSTALLED]"
        print(f"  {name:<{width}}  {spec.help}{ok}")
    return 0


def cmd_config(args: argparse.Namespace, config: Config) -> int:
    """Show the effective configuration after defaults and overrides merge."""
    path = args.config or default_path()
    print(f"# config file: {path}{'' if path.exists() else '  (not present, using defaults)'}")
    print("\n[device]")
    print(f'name = {config.device_name!r}' if config.device_name else "# name = <autodetect>")
    print(f'path = {config.device_path!r}' if config.device_path else "# path = <autodetect>")
    print("\n[general]")
    print(f'modifier = "{config.modifier}"')
    print(f"volume_step = {config.volume_step}")
    print("\n[bindings]")
    for name, binding in sorted(config.bindings.items()):
        rendered = f'"{binding}"' if isinstance(binding, str) else repr(binding)
        print(f"{name} = {rendered}")

    desktop = config.desktop
    print("\n[desktop]")
    print(f"enabled = {str(desktop.enabled).lower()}")
    print(f'enter = "{desktop.enter}"        # hold {config.modifier} and press this')
    print(f'exit = "{desktop.exit}"')
    print(f"pointer_speed = {desktop.pointer_speed:g}")
    print(f"scroll_speed = {desktop.scroll_speed:g}")
    print(f"dead_zone = {desktop.dead_zone:g}")
    print(f"curve = {desktop.curve:g}")
    print("\n[desktop.bindings]")
    for name, spec in sorted(desktop.bindings.items()):
        print(f'{name} = "{spec}"')

    windows = config.windows
    print("\n[windows]")
    print(f'backend = "{windows.backend}"')
    print(f"settle = {windows.settle:g}")
    print(f"launch_timeout = {windows.launch_timeout:g}")
    print(f"minimize_on_return = {str(windows.minimize_on_return).lower()}")
    print(f"park_pointer = {str(windows.park_pointer).lower()}")
    for name, overlay in sorted(config.overlays.items()):
        print(f"\n[overlays.{name}]")
        print(f'class = "{overlay.window_class}"')
        if overlay.command:
            print(f"command = {overlay.command!r}")
    if not config.overlays:
        print("\n# no overlays configured -- see 'padmode windows'")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="padmode",
        description="Console mode for the Linux desktop you already run.",
    )
    parser.add_argument("--version", action="version", version=f"padmode {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "-c", "--config", type=Path, metavar="PATH",
        help=f"config file (default: {default_path()})",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the daemon (default)").set_defaults(func=cmd_run)
    sub.add_parser("devices", help="list gamepads and show which is used").set_defaults(
        func=cmd_devices
    )
    sub.add_parser("actions", help="list built-in actions").set_defaults(func=cmd_actions)
    sub.add_parser(
        "windows", help="show the window backend and check overlays"
    ).set_defaults(func=cmd_windows)
    sub.add_parser("config", help="show effective configuration").set_defaults(
        func=cmd_config
    )
    parser.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        config = load(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 1
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())
