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

> **Status: v0.5.** Media and volume control, a full desktop mode, app overlays
> that return you to the game, a bindable frontend to land on when the game is
> gone, a metadata generator for Heroic libraries, and a console-style
> on-screen keyboard you drive with the pad. Everything except overlays and the
> keyboard still works on every Linux desktop with no compositor involvement at
> all.

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

| Desktop | Media + desktop mode | Overlays | On-screen keyboard |
|---|---|---|---|
| KDE Plasma (Wayland) | Yes | Yes — needs `kdotool` | Yes — needs `gtk4-layer-shell` |
| Any X11 desktop | Yes | Yes — needs `xdotool` | **No** — not in v0.5 |
| Hyprland / sway | Yes | Community backend | Should work, untested |
| GNOME (Wayland) | Yes | **No** — see below | **No** — see below |
| GNOME (X11) | Yes | Yes | **No** — not in v0.5 |

The keyboard is narrower than overlays, and for a different reason. It draws
itself with `zwlr_layer_shell_v1`, the Wayland protocol for surfaces that sit
above everything without ever taking keyboard focus — which is what stops a
keyboard typing into itself. X11 needs a different mechanism entirely, and
GNOME declines to implement layer-shell at all. Both are separate limitations
from the overlay one, even though GNOME loses on both counts.

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
- Optional, for the on-screen keyboard only: `gtk4-layer-shell` and PyGObject
  (`gtk4-layer-shell python-gobject` on Arch). Wayland sessions only
- Optional, for the keyboard opening itself: `at-spi2-core`, plus desktop
  accessibility switched on (see below)

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
| **Y** | Show the on-screen keyboard |
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

### On-screen keyboard

Press **Y** in desktop mode and a console-style keyboard appears along the
bottom of the screen. A desktop you can drive but not type into still sends you
looking for a keyboard, which rather defeats the point.

It is laid out like a 60% keyboard, so Tab, Caps, Shift, Enter and the
modifiers are where your hands already expect them. Four departures: the
symbol-layer toggle takes Escape's place, the number row carries no shifted
legends (those live on the symbol layer), there is no `/`, and the cursor keys
sit beside the space bar.

| Input | Does |
|---|---|
| D-pad | Move around the grid |
| A | Type the highlighted key |
| B | Put the keyboard away |
| X | Backspace |
| Y | Space |
| LB / RB | Move the text cursor left / right |
| LT | Shift |
| RT | Symbols / letters |
| L3 | Caps lock |
| Start | Enter |
| **Left stick** | Move around the grid — the pointer stays put |
| Right stick | Still scrolls, and still Tabs |

**Every shortcut is printed on the key it belongs to**, the way a console
keyboard does it — a yellow Y on the space bar, a blue X on Backspace, `RT` on
the layer toggle. Only `A` and `B` get a line of their own at the bottom, since
neither is a key on the grid. Rebind anything in `[osk.bindings]` and its badge
moves with it.

The d-pad moves by *position*, not by column: the key above the middle of the
space bar is the one directly above it, not the fourth key of the row.

**Ctrl, Alt and Super latch** — press once to hold, press again to let go — so
`Super`+`a` opens your launcher, `Ctrl` stays down while you click something
with the stick, and `Alt` can be held across several keys. They release
themselves whenever the keyboard goes away, so you can't strand your desktop
with a modifier stuck down.

(Latching Super and unlatching it without pressing anything opens KDE's
launcher. That's a real key press and release, and a hardware keyboard does the
same thing.)

It is a *layer*, not a mode: any button the keyboard does not use keeps its
desktop job, the right stick still scrolls, and the exit button still works.
The **left stick moves around the grid** while it is up, and the pointer stays
put — the keyboard's own shortcuts take all three mouse buttons, so a cursor
you could move but not click with was worth less than a second d-pad. Shift
applies to one letter, or locks if you press it twice. The keyboard never takes
keyboard focus, so it types into whatever you were pointing at — including
password fields, since the keystrokes are ordinary virtual-hardware input and
no application can tell them apart from a real keyboard. It does not take mouse
input either: clicks pass straight through to whatever is underneath, so it
never costs you part of the screen.

Set `height` to how much of the screen you want it to take. It sizes itself to
whichever monitor it appears on, and follows you if you move to another one.

**It opens by itself when you click into a text field**, and closes again when
that field goes away — but only if it opened itself. One you opened with `Y`
stays until you dismiss it. This needs desktop accessibility switched on, which
on a stock KDE install it is not:

```bash
gsettings set org.gnome.desktop.interface toolkit-accessibility true
```

Without it nothing reports its text fields and nothing happens; padmode says so
once at startup rather than leaving you guessing. Coverage is per-application:
Qt and GTK apps work, and Electron apps expose nothing unless launched with
`--force-renderer-accessibility`. Turn the whole thing off with
`auto_open = false`.

`A` commits rather than left-clicking while the keyboard is up, because
committing a character is what you are mostly doing. Press `B` first if you
want to click something.

Everything is overridable in `[osk.bindings]`, except the d-pad, which the grid
needs.

> **Assumes a US keyboard layout.** padmode types by sending key *codes*, and
> the character that arrives is whatever your layout maps that key position to.
> On a non-US layout the letters are fine but the punctuation will not match
> what the keyboard draws. Fixing that properly means reading your active
> layout, which padmode does not do yet.

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

**If no button does anything, run `padmode devices` first.** With Steam
running, every controller you connect also gets a virtual twin published by
Steam, named like a real Xbox controller, that never sends an event. padmode always prefers real hardware
over a virtual device, and `padmode devices` prints the reason next to every
pad it found — including why the one it passed over lost. If it reports that
the pad in use is virtual, it exits non-zero and tells you what to pin.

### Game libraries

If you use Heroic or Steam and a frontend that reads Pegasus metadata, padmode
can generate it:

```bash
padmode metadata heroic --dry-run   # see what it would write
padmode metadata heroic             # write it, and fetch cover art
padmode metadata steam              # the same for Steam
```

It reads Heroic's own library cache, downloads cover and background art once,
and writes `~/Games/pegasus-heroic/metadata.pegasus.txt` (`--out` to change
that). Re-running is cheap: art already on disk is never fetched twice. Add
that directory in your frontend's settings — padmode writes the files but
cannot register the directory for you.

`install.sh` offers to keep it current automatically whenever you install or
remove a game. This is a batch tool, entirely separate from the daemon.

The Steam generator reads Steam's own manifests and copies art out of Steam's
cache, so nothing is downloaded. It leaves out Proton, the Steam Linux
Runtimes and the like, and prints each app it skipped and why. To hide
something else, or show something it hid:

```toml
[metadata.steam.ignore]
1234567 = true    # hide this appid
228980 = false    # show it even though padmode would not
```

Pegasus has a Steam data source of its own that lists those tools as games;
turn it off, or every game appears twice. There is no automatic re-run for
Steam yet — run it after installing or removing a game.

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
modifier = "GUIDE"   # the button you hold. Keep it on Guide: any button your
                     # games read gets left latched down -- see Known limitations
volume_step = 5      # percent per press
trigger_threshold = 0.5   # how far LT/RT must be squeezed, on pads whose
                          # triggers are analogue axes. Releases a little below
                          # this, so a resting finger does not chatter

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

[osk]
enabled = true         # the on-screen keyboard
position = "bottom"    # or "top". It never reserves space, so it covers
                       # rather than pushes windows around
height = 30            # percent of screen height; the width scales to match,
                       # so it looks the same on a laptop panel and a TV
auto_open = true       # open when you click into a text field, and close when
                       # that field goes away. Needs desktop accessibility on
# preload = "/usr/lib/libgtk4-layer-shell.so"   # only if autodetection fails

[osk.bindings]         # what the pad does while the keyboard is up.
A = "commit"           # commit, close, back, space, enter, tab, shift,
B = "close"            # capslock, layer, left, right, ctrl, alt, super.
X = "back"             # The d-pad navigates the grid and cannot be rebound.
Y = "space"            # Whatever you bind, the badge on the key follows.
LT = "shift"
RT = "layer"
L3 = "capslock"
```

Button names accept the obvious spellings: `RB` or `R1`, `GUIDE` or `MODE` or
`HOME`, `SELECT` or `BACK`, `DPAD_UP` or `UP`, `RS_RIGHT` or
`RIGHT_STICK_RIGHT`, in any case.

**The triggers are bindable too.** Most pads — every Xbox controller among
them — report `LT` and `RT` as analogue axes rather than buttons, so padmode
translates a squeeze past `trigger_threshold` into a press and a release below
it back into a release. Bind them like any other button, including as the
modifier or as `desktop.enter`. Pads that already report their triggers as
buttons are left alone, so nothing fires twice. `padmode devices` says which
kind yours is.

Bad configuration stops the daemon with an explanation rather than starting up
and quietly ignoring half your bindings.

## Known limitations

- **The game sees the modifier press, and never the release.** padmode reads
  without grabbing until the modifier goes down, so the press that *starts* the
  grab has already reached the game. The release then arrives while padmode
  holds the grab — so only padmode sees it, and the game is left believing the
  button is still held down.

  Measured over Sunshine with `modifier = "SELECT"`, watching the pad from a
  second reader the way a game does: **42 presses got through, and one
  release.** The single release escaped only because it followed the press that
  *left* desktop mode, which drops the grab first — so the button un-latches
  once per exit and then sticks again.

  This is the real reason the default modifier is **Guide**: it is the one
  button games ignore. Do not move `modifier` onto a button your games use.
  `SELECT` or `START` will leave that button latched mid-game, which is much
  worse than Guide being seen. If your setup genuinely cannot send Guide — some
  streaming clients' on-screen pads can't — the fix is on the client, not here.

  Solving it properly means padmode holding the pad permanently and re-emitting
  a virtual gamepad for games to read, the way Steam Input does. That is a
  deliberate architecture change, recorded in CLAUDE.md §10 and not scheduled.
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
| ✅ **v0.5** | On-screen keyboard you drive with the pad, like a console | KDE Wayland only |
| v0.6 | Steam metadata generator *(built, not yet released)* | No |
| v0.7 | Gamepad unlock (opt-in, off by default) | Screensaver D-Bus |
| v0.8 | Settings GUI — change the config from a window, with the pad | No |

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
