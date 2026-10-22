<h1 align="center">PadMode</h1>

<p align="center">
  Console mode for the Linux desktop you already run.<br/>
  Control music and volume mid-game, drive the whole desktop from the couch,
  and pop apps up over your game — all with just a gamepad.
</p>

<p align="center">
  <a href="docs/usage.md">Usage</a> ·
  <a href="docs/configuration.md">Configuration</a> ·
  <a href="#installation">Installation</a> ·
  <a href="docs/usage.md#known-limitations">Known limitations</a>
</p>

<p align="center">
  <a href="https://github.com/KkelebekK/PadMode/releases/latest"><img src="https://img.shields.io/github/v/release/KkelebekK/PadMode" alt="Latest release"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/KkelebekK/PadMode" alt="License"/></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"/>
</p>

## Features

- **Media keys on the pad** — hold Guide to skip tracks, change volume or mute
  your mic without leaving the game.
- **Desktop mode** — the pad becomes a mouse and keyboard, alt-tab included.
- **On-screen keyboard** — console style, opens by itself when you click into a
  text field.
- **Overlays** — bring Discord, a browser or anything else up over your game,
  and get back with one press.
- **Home button** — jump to your game frontend (Pegasus, or any other).
- **Library metadata** — generate Pegasus collections from Heroic and Steam,
  cover art included.
- **Unlock with the pad** — type your password at the lock screen *(opt-in)*.
- **Settings window** — rebind buttons, tune the feel and manage your apps,
  all from the pad: Guide + R3.

Not a distro and not a game frontend: padmode adds console behaviour to the
system you already have, and stays out of the way the rest of the time.

## Controls

Hold **Guide** and press:

| Button | Does |
|---|---|
| RB / LB | Next / previous track |
| A | Play / pause |
| X / Y | Mute output / mute microphone |
| D-pad up / down | Volume |
| L3 | Desktop mode |
| R3 | Settings window |

In desktop mode the left stick moves the pointer, the right stick scrolls,
A / B / X click, **Y** opens the keyboard and **Guide** hands the pad back.
Everything is rebindable — see [usage](docs/usage.md) and
[configuration](docs/configuration.md).

## Installation

Install the dependency, then run the installer:

```bash
sudo pacman -S python-evdev        # Arch
sudo apt install python3-evdev     # Debian / Ubuntu
sudo dnf install python3-evdev     # Fedora
```

```bash
git clone https://github.com/KkelebekK/PadMode
cd PadMode
./install.sh
```

The installer asks before every step and never runs as root. Log out and back
in afterwards so your account joins the `input` group. Prefer to do it by
hand? `./install.sh --print-steps`. To remove: `./install.sh --uninstall`.

Optional extras, each enabling one feature: `playerctl` and `wpctl` for media
and volume, `kdotool` (KDE) or `xdotool` (X11) for overlays,
`gtk4-layer-shell` for the on-screen keyboard, `libadwaita` for the settings
window.

## Compatibility

| Desktop | Media + desktop mode | Overlays | On-screen keyboard | Unlock |
|---|---|---|---|---|
| KDE Plasma (Wayland) | ✅ | ✅ | ✅ | ✅ |
| Any X11 desktop | ✅ | ✅ | ❌ | untested |
| Hyprland / sway | ✅ | ❌ | untested | untested |
| GNOME (Wayland) | ✅ | ❌ | ❌ | untested |

Media keys and desktop mode work everywhere, because the pad simply acts like
a real mouse and keyboard. The rest needs help from the desktop itself:

- **Overlays on GNOME (Wayland)** — GNOME does not let one app bring another
  to the front. It would take a GNOME Shell extension; not planned.
- **Overlays on Hyprland / sway** — each of these controls windows its own
  way, so each needs its own small piece of support. Possible, not written
  yet — a good first contribution.
- **On-screen keyboard on X11** — X11 needs a different way of drawing a
  keyboard that floats above everything. Possible later, not scheduled.
- **On-screen keyboard on GNOME** — GNOME does not offer the kind of
  always-on-top panel the keyboard uses. Not planned.
- **Unlock outside KDE** — it listens for the screen locking, and other
  desktops announce that differently or not at all. Only KDE has been tried.

`padmode windows` tells you what works on yours.

## Configuration

The easiest way is the settings window: **Guide + R3**, or *padmode settings*
in your application menu. It edits `~/.config/padmode/config.toml` for you and
keeps whatever you wrote in it by hand, comments included. Prefer the file?
You only write what you change:

```toml
[bindings]
B = "media.stop"                                 # a built-in action
START = ["notify-send", "hello from the couch"]  # or any command
SELECT = "overlay.chat"                          # or an app over your game

[overlays.chat]
class = "discord"
command = ["discord"]
```

`padmode config` prints the full effective configuration. All options:
[docs/configuration.md](docs/configuration.md).

## Unlock with the pad

Off by default. Turn it on with `[unlock] enabled = true` and the left stick
becomes a dial of eight digits at the lock screen, with X and Y for 9 and 0 —
or [use the Xbox layout](docs/usage.md#unlocking-with-the-pad).

> ⚠️ This needs a login password made only of digits, which is easier to guess
> and is usually your `sudo` password too. Use a long one.

## Development

`python -m unittest discover -s tests -t .` — see
[docs/development.md](docs/development.md) for the test layers and the roadmap.

## License

[GPL-3.0](LICENSE)
