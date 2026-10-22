# Using padmode

Everything the pad does, mode by mode. Every setting is in
[configuration.md](configuration.md).

## Media and volume

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

## Desktop mode

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

## On-screen keyboard

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

## Unlocking with the pad

> **⚠️ This needs a login password made of digits only, and that is a real
> security downgrade.** On most systems the login password is also your `sudo`
> password, and often what unlocks your keyring, and a password of digits is
> far easier to guess than one of words — by anyone who sits down at the
> machine, with a pad or without. If you turn this on, use a long one: eight
> digits or more, not a four-digit PIN. padmode cannot make a numeric password
> strong; it can only make it convenient.

It is **off by default** and stays off unless you write:

```toml
[unlock]
enabled = true
```

While the screen is locked, the pad types digits into the password field. The
lock screen shows nothing padmode draws, so there is nothing on screen to
read: the layout is meant to be learnt by feel, and the dots in the field are
your only feedback.

| Input | Types |
|---|---|
| Left stick ↑ ↗ → ↘ ↓ ↙ ← ↖ | 1 2 3 4 5 6 7 8 |
| X / Y | 9 / 0 |
| Start | Enter — try the password |
| Select | Delete one |
| B | Delete everything the pad typed |

A stick direction types **when you let go**, not when you push: push, feel
where you are, correct if you need to, release. The stick springing back past
centre does not type the opposite digit.

What protects you while it is on:

- **It only exists while the screen is locked.** The keypad padmode types with
  is a separate virtual device, created when the lock screen comes up and
  destroyed when it goes away. During normal use there is no device that could
  type a digit into whatever has focus.
- **That keypad can type 0–9, Enter and Backspace, and nothing else** — not a
  letter, not a shortcut, whatever goes wrong upstream.
- **Only the real lock screen counts.** padmode listens to the screensaver
  service itself; another program on your session bus cannot fake a lock.
- **While locked, the pad is padmode's.** A game still running behind the lock
  screen does not see your password being dialled. Guide does nothing there.
- **When in doubt, it is off.** No screensaver service, or the watcher dying,
  means unlock is disabled and the log says so once.

Every input is rebindable, including the eight directions of either stick:

```toml
[unlock.bindings]
LS_UP = "0"          # any digit, or "enter", "backspace", "clear"
DPAD_UP = "1"        # buttons and the d-pad work too
RS_DOWN_LEFT = "5"   # sectors: LS_/RS_ + UP, UP_RIGHT, RIGHT, DOWN_RIGHT,
                     # DOWN, DOWN_LEFT, LEFT, UP_LEFT
B = false            # unbind
```

**Prefer the Xbox lock screen's layout?** It puts the digits on buttons rather
than on a stick — 1–4 on the d-pad, then the triggers, the bumpers, Y and X.
This gives you exactly that, with the stick dial switched off so there is only
one way to type each digit:

```toml
[unlock.bindings]
DPAD_UP = "1"
DPAD_LEFT = "2"
DPAD_DOWN = "3"
DPAD_RIGHT = "4"
LT = "5"
RT = "6"
LB = "7"
RB = "8"
Y = "9"
X = "0"
LS_UP = false
LS_UP_RIGHT = false
LS_RIGHT = false
LS_DOWN_RIGHT = false
LS_DOWN = false
LS_DOWN_LEFT = false
LS_LEFT = false
LS_UP_LEFT = false
```

Start, Select and B keep their meaning in both layouts.

## Overlays

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

## Going home

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

## Game libraries

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

## What works where

Media keys and desktop mode need no window management at all, so they run
anywhere. Overlays have to activate windows, which Wayland deliberately does
not expose to ordinary clients:

| Desktop | Media + desktop mode | Overlays | On-screen keyboard |
|---|---|---|---|
| KDE Plasma (Wayland) | Yes | Yes — needs `kdotool` | Yes — needs `gtk4-layer-shell` |
| Any X11 desktop | Yes | Yes — needs `xdotool` | **No** — not yet |
| Hyprland / sway | Yes | **No** — not written yet | Should work, untested |
| GNOME (Wayland) | Yes | **No** — see below | **No** — see below |
| GNOME (X11) | Yes | Yes | **No** — not yet |

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
  accessibility switched on (see [On-screen keyboard](#on-screen-keyboard))
- Optional, for unlock only: `gdbus` (part of GLib, so almost certainly already
  there) and a desktop that provides `org.freedesktop.ScreenSaver` on the
  session bus. Tested on KDE Plasma only

Missing optional tools disable only the bindings that need them. No
`playerctl`? Volume control still works, and padmode says so once at startup
instead of failing silently.

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
- **Unlock is tested on KDE Plasma only.** Other desktops name their
  screensaver service differently or not at all; where padmode finds none, it
  says so and unlock stays off. Also untried: starting padmode while the screen
  is already locked, and the pad reconnecting at the lock screen — that loses
  the exclusive hold, so a game behind the lock would see the digits again.
- **`park_pointer` is unproven.** No compositor here can place the cursor, so
  padmode drives its own pointer and corrects until it arrives. It is off by
  default and gives up rather than fighting; if it lands short, that is why.
