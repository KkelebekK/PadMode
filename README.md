# padmode

Console mode for the Linux desktop you already run.

Hold your gamepad's **Guide** button and press another button to skip tracks,
change volume, or mute your mic — without alt-tabbing, without pausing, and
without leaving the game. The pad still belongs to the game the rest of the
time.

> **Status: v0.1.** This is the portable core — media, volume and mic control.
> It has no compositor dependency and works on every Linux desktop. Desktop
> mode, in-game overlays and gamepad unlock are on the roadmap below.

`padmode` is a provisional name and may change before 1.0.

---

## Why this exists

Bazzite, ChimeraOS and `gamescope-session` all do "boot into a console
experience" well — but they ask you to adopt an entire operating system.
padmode *adds* console behaviour to the distro you already run.

It is not a distro, not a game frontend, and not a general-purpose input
remapper. It does a few specific things and stays out of the way.

## What works where

v0.1 needs no window management at all, so it runs anywhere. Later versions
need to activate windows, which Wayland deliberately does not expose to
ordinary clients:

| Desktop | v0.1 (this release) | v0.3+ (overlays) |
|---|---|---|
| KDE Plasma (Wayland) | Yes | Yes |
| Any X11 desktop | Yes | Yes |
| Hyprland / sway | Yes | Community backend |
| GNOME (Wayland) | Yes | **No** — see below |
| GNOME (X11) | Yes | Yes |

GNOME on Wayland refuses window activation requests from clients, by design.
Overlay and return-to-game cannot be implemented there without a GNOME Shell
extension, which is out of scope. Everything in v0.1 still works fine.

## Requirements

- Python 3.11 or newer
- `python-evdev` (`python-evdev` on Arch, `python3-evdev` on Debian/Fedora)
- Membership of the `input` group
- Optional: `playerctl` for media keys, `wpctl` (PipeWire) for volume and mic

Missing optional tools disable only the bindings that need them. No
`playerctl`? Volume control still works, and padmode says so once at startup
instead of failing silently.

## Install

```bash
git clone https://github.com/kelebek/padmode
cd padmode
./install.sh
```

The installer explains each step and asks before changing anything. If you'd
rather run the commands yourself:

```bash
./install.sh --print-steps
```

It never runs as root. Two steps use `sudo` — installing a udev rule and adding
you to the `input` group — and both are asked for separately, so you can
decline either. To remove everything: `./install.sh --uninstall`.

You must **log out and back in** after joining the `input` group.

## Use

Hold **Guide**, press a button:

| Combo | Does |
|---|---|
| Guide + RB | Next track |
| Guide + LB | Previous track |
| Guide + A | Play / pause |
| Guide + X | Mute / unmute output |
| Guide + Y | Mute / unmute microphone |
| Guide + D-pad Up / Down | Volume up / down |

Useful commands:

```bash
padmode devices   # which gamepads were found, and which one is used
padmode config    # effective configuration after defaults and overrides
padmode actions   # everything you can bind, and whether its tool is installed
```

Logs go to the journal: `journalctl --user -u padmode -f`

## Configuration

Everything above is a default, and every default can be changed. Create
`~/.config/padmode/config.toml`:

```toml
[device]
# Autodetected by capability. Set this only if you have several pads.
# Substring match, case-insensitive.
# name = "X-Box"

[general]
modifier = "GUIDE"   # the button you hold; SELECT and START also work well
volume_step = 5      # percent per press

[bindings]
# Bindings merge over the defaults, so you only write what you change.
B = "media.stop"           # add one
RB = "audio.volume_up"     # change one
Y = false                  # remove one

# A binding can also be any command you like:
START = ["notify-send", "padmode", "hello from the couch"]
```

Button names accept the obvious spellings: `RB` or `R1`, `GUIDE` or `MODE` or
`HOME`, `SELECT` or `BACK`, `DPAD_UP` or `UP`, in any case.

Bad configuration stops the daemon with an explanation rather than starting up
and quietly ignoring half your bindings.

## Known limitations

- **The game also sees the Guide press.** padmode reads without grabbing until
  the modifier goes down, so the press that *starts* the grab has already been
  delivered. If your game binds Guide (Steam's overlay does), it will react.
  Set `modifier` to `SELECT` or `START` to avoid it.
- **Media control needs an MPRIS-capable player.** `playerctl` talks to players
  over MPRIS. Most expose it by default, but some need it switched on — the
  YouTube Music desktop app, for one, only shows up once its MPRIS / API-server
  plugin is enabled. If the media bindings do nothing, check that `playerctl -l`
  lists your player.
- **Actions fire on press, not release.** Holding a bound button does not
  repeat.
- **One gamepad.** Multi-pad support isn't implemented; the first matching
  device wins.

## Roadmap

| Version | Adds | Needs a compositor? |
|---|---|---|
| **v0.1** | Media, volume, mic while holding Guide | No |
| v0.2 | Virtual mouse/keyboard, desktop mode | No |
| v0.3 | Window backends, in-game overlays, return-to-game | KDE + X11 |
| v0.4 | Frontend integration, library metadata generators | KDE + X11 |
| v0.5 | Gamepad unlock (opt-in, off by default) | Screensaver D-Bus |

v0.1 and v0.2 are deliberately useful on their own, on any desktop.

## Development

```bash
python -m unittest discover -s tests -t .
```

Tests use stdlib `unittest`, so contributors need nothing installed beyond what
the app itself requires. The suite has two layers: pure unit tests for the
decision logic, which need no hardware, and integration tests that create a
synthetic uinput gamepad and replay real event sequences through the real read
loop. The integration tests skip themselves automatically without `/dev/uinput`
access.

See [CLAUDE.md](CLAUDE.md) for the design contract and the reasoning behind the
locked decisions.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
