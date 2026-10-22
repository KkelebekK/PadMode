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

from . import __version__, buttons
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
    from .control import ControlServer
    from .input import DigitKeypad, GamepadReader, VirtualInput
    from .lock import LockWatcher
    from .unlock import KEYPAD_KEYS
    from .osk.client import OskClient
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
    # Same shape: constructed, not started. The renderer process appears when
    # a pad-driven mode does, and a session without gtk4-layer-shell simply
    # never gets one -- the daemon does not care either way.
    osk = OskClient(config.osk) if config.osk.enabled else None
    # Not even constructed unless asked for: unlock is opt-in, and an off
    # feature should not leave a gdbus process behind it.
    lock = LockWatcher() if config.unlock.enabled else None
    keypad = DigitKeypad(KEYPAD_KEYS) if config.unlock.enabled else None
    # Always: Apply in the settings window needs it whether or not the window
    # ever takes the pad, and it costs one socket.
    control = ControlServer()
    try:
        Daemon(
            config, reader, runner, virtual, detection.backend, osk, lock, keypad,
            control=control, config_path=args.config,
        ).run()
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

    frontend = config.frontend
    if not config.overlays and frontend is None:
        print("\nNo overlays configured. To add one:\n")
        print("  [overlays.discord]")
        print('  class = "discord"')
        print('  command = ["discord"]\n')
        print("  [bindings]")
        print('  B = "overlay.discord"')
        return 0

    stuck = 0
    if config.overlays:
        print("\noverlays:")
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

    if frontend is not None:
        window = backend.find_window(frontend.window_class)
        print("\nfrontend:")
        if window is not None:
            print(f"  found {window}")
        elif frontend.command:
            print(f"  not running; the frontend button would start "
                  f"{' '.join(frontend.command)}")
            # Worth spelling out: it is the one asymmetry in this feature.
            print("  the return-target fallback would find nothing, and never "
                  "starts it")
        else:
            print(f"  no window matching class {frontend.window_class!r}, and "
                  "no command to start one")
    elif config.overlays:
        print("\nNo frontend configured. If a game exits while an overlay is")
        print("up, there is nowhere to return to. To add one:\n")
        print("  [frontend]")
        print('  class = "pegasus"')
    return 1 if stuck else 0


def cmd_metadata(args: argparse.Namespace, config: Config) -> int:
    """Generate frontend metadata from a game library.

    The import is inside the function on purpose. The generators do network and
    filesystem work that has no business near the input loop, and CLAUDE.md §2
    keeps them out of the daemon entirely -- so nothing above this line may
    import them, and a test asserts it.
    """
    if args.generator is None:
        args.metadata_parser.print_help()
        return 2
    if args.generator == "steam":
        from .metadata import steam

        return steam.main(args)
    from .metadata import heroic

    return heroic.main(args)


def _device_verdict(device, chosen_path: str | None, needle: str | None) -> str:
    """One line saying why this device was taken, or why it was passed over.

    Every device gets a reason, including the one we picked. The bug this
    exists for -- binding Steam's inert virtual pad and appearing dead with no
    error anywhere -- was invisible precisely because the listing said which
    device won and never said why the others lost.
    """
    from .input.reader import VIRTUAL_PREFIX, is_virtual

    if device.name.startswith(VIRTUAL_PREFIX):
        return "skipped: one of padmode's own virtual devices"
    if needle and needle not in device.name.lower():
        return f"skipped: name does not contain {needle!r}"
    if is_virtual(device):
        if device.path == chosen_path:
            return ("virtual: published by another program, no hardware "
                    "behind it -- and nothing else matched")
        return ("skipped: virtual, published by another program -- a real pad "
                "outranks it")
    return f"physical: {device.phys or 'no phys reported'}"


def cmd_devices(args: argparse.Namespace, config: Config) -> int:
    """List input devices and show which one we would pick, and why."""
    from evdev import InputDevice, list_devices

    from .input import find_gamepad, is_virtual, looks_like_gamepad
    from .input.reader import _node_key

    chosen = find_gamepad(config.device_name, config.device_path)
    chosen_path = chosen.path if chosen else None
    needle = config.device_name.lower() if config.device_name else None

    found = False
    for path in sorted(list_devices(), key=_node_key):
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
        print(f"    {_device_verdict(device, chosen_path, needle)}")
        if path == chosen_path:
            # Worth saying out loud: "LT does nothing" is otherwise a silent
            # mystery, and which form a pad uses is invisible from outside.
            from .input.reader import _trigger_axes

            translated = _trigger_axes(device)
            if translated:
                names = ", ".join(sorted(
                    buttons.TRIGGERS[axis] for axis in translated
                ))
                print(f"    {names} are analogue axes on this pad, and are "
                      "translated into button presses")
            else:
                print("    triggers report as buttons on this pad")

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
    print("Real hardware always outranks a virtual device, whatever the event")
    print("numbers are. Steam publishes a pad named like real hardware for as")
    print("long as it is running, and that pad never sends an event here.")

    if is_virtual(chosen):
        print()
        print("WARNING: the selected device is virtual, so buttons will most")
        print("likely do nothing. Plug in a real pad, or pin one below.")
        print("To pin it, put this in", default_path())
        print(f'\n[device]\npath = "{chosen.path}"')
        return 1

    print()
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
    # Not actions, but the first place someone looks for "what can I bind?".
    print("\nA binding can also be:")
    print(f"  {'overlay.<name>':<{width}}  raise an app declared in [overlays]")
    print(f"  {'frontend':<{width}}  raise the app declared in [frontend]")
    print(f"  {'[\"cmd\", \"arg\"]':<{width}}  any command you like")
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
    print(f"trigger_threshold = {config.trigger_threshold:g}"
          "   # how far LT/RT must be squeezed")
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
    print(f"park_pointer = {str(windows.park_pointer).lower()}")
    for name, overlay in sorted(config.overlays.items()):
        print(f"\n[overlays.{name}]")
        print(f'class = "{overlay.window_class}"')
        if overlay.command:
            print(f"command = {overlay.command!r}")
    if not config.overlays:
        print("\n# no overlays configured -- see 'padmode windows'")

    if config.frontend is not None:
        print("\n[frontend]")
        print(f'class = "{config.frontend.window_class}"')
        if config.frontend.command:
            print(f"command = {config.frontend.command!r}")
            print("# used by a button bound to \"frontend\"; the return-target")
            print("# fallback never starts it")
    else:
        print("\n# no frontend configured -- overlay mode has nowhere to fall")
        print("# back to if the game exits while the overlay is up")

    osk = config.osk
    print("\n[osk]")
    print(f"enabled = {str(osk.enabled).lower()}")
    print(f'position = "{osk.position}"')
    print(f"height = {osk.height:g}       # percent of screen height")
    if osk.preload:
        print(f'preload = "{osk.preload}"')
    else:
        # Resolved here rather than guessed: this is the difference between a
        # keyboard and a focus-stealing window, so it is worth showing.
        from .osk import preload_library

        found = preload_library()
        print(f"# preload = {found!r}" if found else
              "# gtk4-layer-shell not found -- the keyboard is unavailable")
    print("\n[osk.bindings]        # the d-pad navigates and cannot be rebound")
    for name, action in sorted(osk.bindings.items()):
        print(f'{name} = "{action}"')

    print("\n[unlock]")
    print(f"enabled = {str(config.unlock.enabled).lower()}"
          "       # needs a digits-only password: a real downgrade")
    print(f"stick_threshold = {config.unlock.stick_threshold:g}")
    print("\n[unlock.bindings]     # a stick sector types on the way home")
    for name, action in sorted(config.unlock.bindings.items()):
        print(f'{name} = "{action}"')
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
    _add_metadata_parser(sub)
    parser.set_defaults(func=cmd_run)
    return parser


def _add_metadata_parser(sub) -> None:
    """``padmode metadata <generator>``: write frontend metadata for a library.

    Every flag is declared here rather than by the generator itself. The parser
    is built on every invocation, so asking the generator to describe its own
    options would import it -- and its network and filesystem machinery -- into
    ``padmode run`` (CLAUDE.md §2). What the flags *mean* still belongs to the
    generator: it resolves them against the environment and its own defaults.
    """
    metadata = sub.add_parser(
        "metadata",
        help="generate frontend metadata from a game library",
        description="Read a launcher's library and write metadata for a "
                    "frontend. Not part of the daemon; run it whenever your "
                    "library changes.",
    )
    generators = metadata.add_subparsers(dest="generator")
    heroic = generators.add_parser(
        "heroic", help="Heroic (Epic, GOG, Amazon) -> Pegasus",
    )
    heroic.add_argument(
        "--heroic-dir", type=Path, metavar="PATH",
        help="Heroic's config directory "
             "(default: autodetect, or $HEROIC_CONFIG_DIR)",
    )
    heroic.add_argument(
        "--out", type=Path, metavar="PATH",
        help="where to write metadata and art "
             "(default: $PEGASUS_HEROIC_OUT, else ~/Games/pegasus-heroic)",
    )
    heroic.add_argument(
        "--collection", metavar="NAME",
        help="collection name in the frontend (default: Heroic)",
    )
    heroic.add_argument(
        "--dry-run", action="store_true",
        help="print the metadata instead of writing anything or fetching art",
    )
    steam = generators.add_parser(
        "steam", help="Steam -> Pegasus",
    )
    steam.add_argument(
        "--steam-dir", type=Path, metavar="PATH",
        help="Steam's directory, the one holding steamapps/ "
             "(default: autodetect, or $PADMODE_STEAM_DIR)",
    )
    steam.add_argument(
        "--out", type=Path, metavar="PATH",
        help="where to write metadata and art "
             "(default: $PEGASUS_STEAM_OUT, else ~/Games/pegasus-steam)",
    )
    steam.add_argument(
        "--collection", metavar="NAME",
        help="collection name in the frontend (default: Steam)",
    )
    steam.add_argument(
        "--dry-run", action="store_true",
        help="print the metadata instead of writing anything or copying art",
    )
    metadata.set_defaults(func=cmd_metadata, metadata_parser=metadata)


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
