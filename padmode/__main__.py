"""Command line entry point.

Logging goes to stderr and journald picks it up. No log files (CLAUDE.md §4).
"""

from __future__ import annotations

import argparse
import logging
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

    signal.signal(signal.SIGTERM, _on_sigterm)
    reader = GamepadReader(name=config.device_name, path=config.device_path)
    runner = ActionRunner(ActionContext(volume_step=config.volume_step))
    # Created here but not opened: the uinput devices appear on first entry to
    # desktop mode, so someone who never uses it gets no phantom hardware.
    virtual = VirtualInput() if config.desktop.enabled else None
    try:
        Daemon(config, reader, runner, virtual).run()
    except KeyboardInterrupt:
        pass
    return 0


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
