# padmode

Console mode for the Linux desktop you already run.

Hold your gamepad's **Guide** button and press another button to skip tracks,
change volume, or mute your mic — without alt-tabbing, without pausing, and
without leaving the game. The pad still belongs to the game the rest of the
time.

Then press Guide + L3 and the pad becomes a mouse and keyboard, so you can
drive the whole desktop from the couch without reaching for either. Or bind a
button to an app and have it come up *over* your game, with one press to send
you straight back — and another button to go home to your game frontend.

> **Status: v0.4.** Media and volume control, a full desktop mode, app
> overlays that return you to the game, a bindable frontend to land on when
> the game is gone, and a metadata generator for Heroic libraries. Everything
> except overlays still works on every Linux desktop with no compositor
> involvement at all.

`padmode` is a provisional name and may change before 1.0.

---

## Why this exists

Bazzite, ChimeraOS and `gamescope-session` all do "boot into a console
experience" well — but they ask you to adopt an entire operating system.
padmode *adds* console behaviour to the distro you already run.

It is not a distro, not a game frontend, and not a general-purpose input
remapper. It does a few specific things and stays out of the way.

## What works where

Media keys and desktop mode need no window management at all, so they run
anywhere. Overlays have to activate windows, which Wayland deliberately does
not expose to ordinary clients:

| Desktop | Media + desktop mode | Overlays |
|---|---|---|
| KDE Plasma (Wayland) | Yes | Yes — needs `kdotool` |
| Any X11 desktop | Yes | Yes — needs `xdotool` |
| Hyprland / sway | Yes | Community backend |
| GNOME (Wayland) | Yes | **No** — see below |
| GNOME (X11) | Yes | Yes |

GNOME on Wayland refuses window activation requests from clients, by design.
Overlays and return-to-game cannot be implemented there without a GNOME Shell
extension, which is out of scope. Everything else still works fine — the
virtual mouse and keyboard are ordinary input devices, indistinguishable from
real hardware, so no compositor gets a say in them. padmode says so once at
startup and disables only the overlay buttons.

`padmode windows` tells you what it found on your system.

## Requirements

- Python 3.11 or newer
- `python-evdev` (`python-evdev` on Arch, `python3-evdev` on Debian/Fedora)
- Membership of the `input` group
- Write access to `/dev/uinput` for desktop mode — the installer's udev rule
  handles this
- Optional: `playerctl` for media keys, `wpctl` (PipeWire) for volume and mic
- Optional, for overlays only: `kdotool` on KDE Wayland, `xdotool` on X11

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
| Guide + L3 | Enter desktop mode |

### Desktop mode

Hold Guide and click the left stick (L3) and the pad takes over your desktop:

| Input | Does |
|---|---|
| Left stick | Move the pointer |
| Right stick (up/down) | Scroll |
| A / B / X | Left / right / middle click |
| D-pad | Arrow keys |
| Start / Select | Enter / Escape |
| **Hold R3** | Hold Alt — opens the window switcher |
| Right stick (left/right) | Tab / Shift+Tab — step through it |
| **Guide** | Leave desktop mode |

Alt-tab is worth explaining, because the obvious binding is the wrong one.
Binding a button to "press alt+tab" taps the whole combination and releases it
instantly, so it can only ever flip between your two most recent windows. Here
R3 *holds* Alt, keeping the switcher open, and flicking the right stick steps
through it — release R3 to commit. Any button can hold a modifier this way:
a single key follows the button, while a combination like `key.ctrl+w` fires
once per press.

The pointer accelerates smoothly rather than moving at one fixed speed, so
small stick movements are precise and large ones cover the screen quickly.
Adjust with `pointer_speed`, `dead_zone` and `curve` in config.

While desktop mode is active the pad is held exclusively, so nothing leaks
through to whatever is running underneath. Leaving hands it straight back.

### Overlays

An overlay is an app on a button. Press it and the app comes up over whatever
you were doing, with the pad driving it; press the exit button and you are back
in the game, pad and all.

```toml
[overlays.chat]
class = "discord"          # matched against the window class
command = ["discord"]      # started if it isn't running yet

[bindings]
B = "overlay.chat"         # hold Guide, press B
```

That is the whole feature. If the app is already running it is raised; if it
isn't, padmode starts it and waits for its window. Leaving minimizes the
overlay and puts focus back on the window you came from — and hands the pad
back *first*, so a button you are still holding cannot leak through into your
game. The app keeps running, so the next press raises it instantly.

Don't know what to put in `class`? Focus the app and run `padmode windows`.

Overlays are the one part of padmode that needs cooperation from your desktop.
See [what works where](#what-works-where). Fullscreen games are fine — a
fullscreen Steam game at 2560x1440 was overlaid and returned to on KWin
Wayland. A game that keeps the compositor out of the loop entirely (its own
gamescope session, say) may still refuse, so treat it as per-game: press the
button and look.

### Going home

Tell padmode about your frontend and it gains a "home" button, and somewhere
to put you when the game you were playing is gone:

```toml
[frontend]
class = "pegasus"
command = ["pegasus-fe"]   # optional

[bindings]
SELECT = "frontend"        # hold Guide, press Select
```

`class` is a case-insensitive regex, so `"pegasus"` matches Pegasus's actual
window class, `org.pegasus-frontend.pegasus-fe`. Run `padmode windows` with
your frontend focused if you use a different one.

Pressing it raises the frontend and hands the pad *back* — the frontend reads
the gamepad itself, so padmode gets out of the way rather than driving it with
a virtual mouse.

The other half is automatic. If your game exits while an overlay is up, the
window padmode was going to return you to no longer exists, and asking the
compositor to focus it succeeds while doing nothing at all — leaving you on the
desktop holding a controller. With a frontend configured, padmode notices that
focus did not land where it aimed and sends you home instead.

One deliberate asymmetry: the **button** may start your frontend if it is not
running, but the automatic fallback never will. A game crashing should not
launch processes behind your back.

Useful commands:

```bash
padmode devices   # which gamepads were found, and which one is used
padmode config    # effective configuration after defaults and overrides
padmode actions   # everything you can bind, and whether its tool is installed
padmode windows   # the window backend, and whether each overlay can be found
```

### Game libraries

If you use Heroic and a frontend that reads Pegasus metadata, padmode can
generate it:

```bash
padmode metadata heroic --dry-run   # see what it would write
padmode metadata heroic             # write it, and fetch cover art
```

It reads Heroic's own library cache, downloads cover and background art once,
and writes `~/Games/pegasus-heroic/metadata.pegasus.txt` (`--out` to change
that). Re-running is cheap: art already on disk is never fetched twice. Add
that directory in your frontend's settings — padmode writes the files but
cannot register the directory for you.

`install.sh` offers to keep it current automatically whenever you install or
remove a game. This is a batch tool, entirely separate from the daemon.

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

[desktop]
enabled = true
enter = "L3"           # held with the modifier to enter desktop mode
exit = "GUIDE"         # pressed on its own to leave
pointer_speed = 900    # pixels per second at full stick deflection
scroll_speed = 10      # wheel clicks per second
dead_zone = 0.15       # radial: ignore the stick within this of centre
curve = 2.0            # 1.0 is linear; higher gives finer slow control
stick_threshold = 0.6  # how far a stick must go to count as a direction press
stick_settle = 0.15    # after a flick, the shortest time before the axis may
                       # re-arm. It also has to return to centre first.
button_debounce = 0.08 # ignore a button release this brief. Stick clicks
                       # (L3/R3) bounce when you move the stick.
stick_dominance = 2.0  # how much one axis must beat the other before it acts,
                       # so a slightly-off flick still steps instead of scrolling

[desktop.bindings]
Y = "key.f5"           # any key, held while the button is held
LB = "key.ctrl+w"      # a combination, fired once per press
R3 = "key.super"       # hold a modifier for as long as you hold the button

# Stick directions can be bound like buttons. Binding one takes that axis away
# from its analogue job, and only the dominant axis of a stick acts at a time
# -- so flicking sideways steps without also scrolling, and pushing up/down
# scrolls without stepping.
RS_RIGHT = "key.tab"
RS_LEFT = "key.shift+tab"

[windows]
backend = "auto"       # auto | kwin | x11 | none. Autodetected from the session
settle = 0.7           # pause between handing the pad back and refocusing the
                       # game, so a button you are still holding can't leak in
launch_timeout = 10    # how long to wait for a launched app's window
park_pointer = false   # move the cursor onto the overlay when it opens, and
                       # back where it was when you return. Worth enabling
                       # under focus-follows-mouse, where a cursor left over
                       # your game can pull focus off the overlay

[overlays.chat]
class = "discord"          # regex, case-insensitive, matched on window class
command = ["discord"]      # optional: without it, only raises a running app

[overlays.browser]
class = "firefox"
command = ["firefox"]

[frontend]
class = "pegasus"       # where a button bound to "frontend" takes you, and
command = ["pegasus-fe"]   # where you land if the game exits while an overlay
                           # is up. The command is used only by the button --
                           # the automatic fallback never starts anything.
```

Button names accept the obvious spellings: `RB` or `R1`, `GUIDE` or `MODE` or
`HOME`, `SELECT` or `BACK`, `DPAD_UP` or `UP`, `RS_RIGHT` or
`RIGHT_STICK_RIGHT`, in any case.

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
- **A stick with bound directions ignores true diagonals.** Only the clearly
  dominant axis acts, so flicking right steps and pushing up scrolls, but a
  push at roughly 45° does nothing rather than guessing. Lower
  `stick_dominance` toward 1.0 to widen each cone, at the cost of sloppy flicks
  scrolling when you meant to step.
- **Actions fire on press, not release.** Holding a bound button does not
  repeat.
- **One gamepad.** Multi-pad support isn't implemented; the first matching
  device wins.
- **Some games may refuse to be overlaid.** Fullscreen on its own is not the
  problem — a fullscreen Steam game overlays fine on KDE Wayland, because the
  compositor stops bypassing itself as soon as something must be drawn on top.
  A game running under its own gamescope session is a different matter. Press
  the button and look; it takes ten seconds to find out.
- **Overlays need a window backend.** No `kdotool`/`xdotool`, or GNOME on
  Wayland, means the overlay buttons do nothing and say so once at startup.
  Nothing else is affected.
- **`park_pointer` is unproven.** No compositor here can place the cursor, so
  padmode drives its own pointer and corrects until it arrives. It is off by
  default and gives up rather than fighting; if it lands short, that is why.

## Roadmap

| Version | Adds | Needs a compositor? |
|---|---|---|
| ✅ v0.1 | Media, volume, mic while holding Guide | No |
| ✅ v0.2 | Virtual mouse/keyboard, desktop mode | No |
| ✅ v0.3 | Window backends, in-game overlays, return-to-game | KDE + X11 |
| ✅ **v0.4** | Frontend button and fallback, Heroic metadata generator | KDE + X11 |
| v0.4.1 | Steam metadata generator | No |
| v0.5 | Gamepad unlock (opt-in, off by default) | Screensaver D-Bus |

v0.1 and v0.2 are deliberately useful on their own, on any desktop. Overlays
are the first feature that has to activate a window, which is where Wayland
starts having opinions — so they are additive, and their absence costs you
nothing else.

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
