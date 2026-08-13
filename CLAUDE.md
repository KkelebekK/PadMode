# padmode

Turn any Linux desktop into a couch/console experience driven entirely by a
gamepad: media and volume control without leaving your game, a gamepad-as-mouse
desktop mode, app overlays over a running game, and (opt-in) unlocking the
screen with the pad.

`padmode` is a provisional name — final name TBD. The repo directory is
`WeHaveSteamMachineAtHome` (a joke); do not use "Steam Machine" or other Valve
product names in the shipped tool, package, or docs.

**Status: v0.3.2** (Guide-hold media/volume/mic; uinput virtual
mouse/keyboard; desktop mode with bindable stick directions; window backends
and overlay mode with return-to-game; 292 tests). This document is the
contract. If something here is wrong, fix this file in the same change as the
code.

---

## 1. What this is (and is not)

This grew out of a working personal setup on CachyOS/KDE, which has since been
decommissioned — this app replaces it outright. Everything from it worth
keeping was folded into §6 (gotchas) and `docs/metadata-generators.md`. There
is no longer a second implementation to consult or stay compatible with.

**Non-goals:**
- Not a distro, session, or greeter. Bazzite/ChimeraOS/`gamescope-session`
  already own "boot into a bespoke console OS." Our niche is *adding* console
  mode to the distro you already run.
- Not a game frontend. We integrate with frontends; we don't ship one.
- Not a general-purpose input remapper. We do specific modes, not arbitrary
  device remapping. That's input-remapper's and AntiMicroX's job.

---

## 2. Locked decisions

| Area | Decision |
|---|---|
| Language | **Python 3.11+** (stdlib `tomllib`; everything is subprocess glue, perf is irrelevant) |
| Architecture | **One daemon, state machine.** A single process owns the gamepad |
| First release | **Portable core first** — ship what works on every desktop, add compositor code later |
| Frontend | **Agnostic.** Core knows nothing about Pegasus; frontends are config |
| Bindings | **Good defaults, fully overridable** via config |
| Unlock feature | **In-repo, opt-in, off by default**, with a blunt security warning |
| Metadata generators | **Same repo, separate command.** Never imported by the daemon |
| Packaging | **AUR + `install.sh`** |
| Installer scope | **Both**: prompted, explained DE changes *and* a printed manual-steps mode for people who won't run them |
| License | **GPL-3.0** (ecosystem fit — input-remapper, KDE, evdev tooling are GPL) |
| Window backends | **KDE Wayland + X11.** Others community-contributed against the interface |

### Decisions made in service of the above (change freely if they stop serving it)

- **Config:** TOML at `$XDG_CONFIG_HOME/padmode/config.toml`.
- **Dependencies:** core is stdlib + `python-evdev` only. `playerctl`, `wpctl`,
  and window tools are *external commands*, probed at runtime, degrade
  gracefully when absent. Never add a Python dependency without a strong reason.
- **Privileges:** user-level systemd service. Needs read on `/dev/input/event*`
  (the `input` group) and write on `/dev/uinput` (a shipped udev rule). **Never
  runs as root.**
- **No input-remapper.** We create our own uinput virtual device (see §3.2).

---

## 3. Architecture

### 3.1 Single daemon, explicit modes

One process opens the gamepad and never lets go of ownership. Modes:

- `idle` — pad belongs to whatever is running (game, frontend). We read
  **without grabbing**.
- `guide_held` — momentarily grabbed; buttons are commands, not game input.
- `desktop` — virtual mouse/keyboard active; pad drives the desktop.
- `overlay` — like `desktop`, but we remember a window to return to.
- `unlock` — virtual keyboard types digits at the lock screen (opt-in).

Mode transitions are the whole app. Keep them in one readable place.

### 3.2 Two things the old setup did that we delete on day one

These are the reason this is a rewrite and not a port:

**The four-hop return signal.** The old setup routed "user pressed the return
button" through: input-remapper → synthetic `Ctrl+Alt+P` → KDE global shortcut
daemon → `touch /tmp/pegasus-desktop.flag` → polled by bash every 0.4s. The
daemon already reads the gamepad over evdev — it can see the button itself.
**No flag file, no synthetic keystroke, no DE global-shortcut dependency, no
polling.** This single change removes the least-portable piece of the design.

**The input-remapper dependency.** `python-evdev` (already required) can create
a uinput virtual mouse/keyboard directly. This removes a heavy dependency and,
more importantly, deletes *"now open the input-remapper GUI and hand-build two
presets"* from the install instructions. That manual step is the difference
between a tool people install and one they bounce off.

### 3.3 Window backends

Only **three** operations are required. Keep it this small — every one of them
has to be implemented by every future community backend:

```python
find_window(window_class) -> WindowId | None
activate_window(window_id) -> None
get_active_window() -> WindowId | None
```

Four more are **optional**, default to "cannot", and disable the feature built
on them rather than the backend: `minimize_window` (used by the default return
behaviour), `window_geometry` and `pointer_position` (for `park_pointer`), and
`window_class` (for `padmode windows` and nothing else). A backend that
implements only the required three still gives working overlays.

Backends: `kwin` (via `kdotool`), `x11` (via `xdotool`; `wmctrl` turned out to
be unnecessary), autodetected from the session. GNOME Wayland **cannot** be
supported — it refuses window activation from clients. Say so plainly in the
README rather than accumulating bug reports.

No backend for this session means overlays are disabled with one startup line
saying why; it is never a startup failure, because a config shared between a
KDE box and a GNOME laptop should still run on both.

Detail, including the tools' output formats and the manual checklist, is in
`docs/window-backends.md`.

### 3.4 Roadmap

| Version | Contents | Compositor dependency |
|---|---|---|
| ~~**v0.1**~~ ✅ | evdev reader, Guide-hold media/volume/mic, config, systemd unit, udev rule, `install.sh` | **None** — works on every Linux desktop |
| ~~**v0.2**~~ ✅ | uinput virtual device, desktop mode (pad as mouse), return button | **None** — confirmed: no window code was needed |
| ~~**v0.3**~~ ✅ | Window backends, overlay mode, return-to-game | KDE + X11 |
| **v0.4** | Frontend integration (return target), metadata generators | KDE + X11 |
| **v0.5** | Unlock module (opt-in, off by default) | Screensaver D-Bus |

Do not pull work earlier in this list. The point of the ordering is that v0.1
and v0.2 are *universally useful on their own*.

---

## 4. Code conventions

- **Pure decision functions.** The existing `gamepad-guide-daemon.py` got this
  right: a decision function returning an action, with no I/O. Ours is
  `decide(ev_type, ev_code, ev_value, state, config, now) -> Decision` in
  `daemon.py`. Config *and the clock* are explicit arguments rather than being
  smuggled in via `state` or read from `time.monotonic()` inside, so the output
  depends only on the inputs and time-dependent behaviour is testable without
  sleeping. Keep that shape — it is the entire testing strategy for input
  handling, since CI has no gamepad.
- **Never hardcode a device.** The old code pinned `"Generic X-Box pad"` and
  `/dev/input/event3`. Enumerate and match by capability, with a config
  override.
- **Never hardcode an app path.** `/usr/bin/discord` and friends come from
  config.
- **External commands are optional.** Probe with `shutil.which`, disable the
  feature and log once if missing. A user without `playerctl` should still get
  volume control.
- **Always release the grab in a `finally`.** A crash that leaves the pad
  grabbed makes the machine unusable by controller — the worst failure mode
  this app has. Also handle `SIGTERM`.
- **Log to stdout/stderr, let journald handle it.** No log files.
- **Debug input problems by tracing the device, never by reasoning about it.**
  This is not a style preference; it is the most expensive lesson in the
  project so far. One misbehaviour ("a stick flick steps two windows") produced
  four consecutive fixes built on plausible theories, each of which addressed a
  mechanism that measurement later showed does not happen on the hardware. The
  actual causes — diagonal contamination into the scroll wheel, and a stick
  click bouncing when the stick is moved — were both obvious within one trace,
  and neither had been guessed. Symptoms that *sound* like input-handling bugs
  routinely originate in the hardware or in a second process.

  So, before changing any input logic in response to a report:

  1. Trace the real device. `decide()` takes an explicit `now`, so a captured
     event stream can be replayed offline and the decision logic checked in
     isolation.
  2. Watch what the *virtual* devices actually emit. "We sent one keystroke and
     the compositor acted twice" and "we sent two" look identical from the
     outside and need completely different fixes.
  3. Confirm exactly one padmode is running first. Two daemons produce
     confusing, self-consistent nonsense — and any tool written for this must
     *refuse* to run when it finds duplicates rather than picking one.
  4. Only then form a theory, and say plainly which observation rules out the
     alternatives.

  **This covers the docs as well as the code.** A caveat nobody has observed is
  a guess with a confident voice, and it outlives the conversation that made it
  up. v0.3.2 shipped a warning about apps minimizing to the system tray while
  the test output on screen showed the opposite; it was removed one commit
  later. Write down what happened, not what might.

---

## 5. Testing

CI has no gamepad, no compositor, and no audio. So:

- **Unit:** pure decision functions, config parsing, mode transitions. This is
  where most coverage lives, and it can cover nearly all the interesting logic.
- **Integration:** create a *synthetic* uinput device, replay event sequences,
  assert on the resulting state machine. Runs locally and in privileged CI;
  skips itself when `/dev/uinput` isn't writable.
- **Manual:** anything touching a real compositor or the lock screen. Keep a
  written checklist in `docs/`; do not pretend these are automated.

Run with `python -m unittest discover -s tests -t .`. **Stdlib `unittest`, not
pytest** — the promise that this project needs no dependencies should hold for
contributors too, and most distros ship a PEP 668 Python where installing test
deps is its own chore.

Things the integration tests learned the hard way, all worth keeping:

- A uinput write is dropped unless it is a real **state change** *on an axis or
  key the device actually advertises*. A key release for a button that was
  never pressed emits nothing; so does a write to an undeclared axis. Both fail
  silently, and both show up as tests that mysteriously wait out their timeout.
- Writing to a closed `UInput` raises `ValueError` (fd is -1), not `OSError`.
- The daemon's own virtual pointer advertises `BTN_LEFT` and would otherwise
  match the gamepad capability check. The `padmode` name prefix in
  `reader.VIRTUAL_PREFIX` is what stops us reading back our own output — do not
  rename the virtual devices without updating it.
- **Never identify a virtual device by name, and never by diffing
  `/dev/input`.** A padmode running on the same machine — the maintainer's own,
  during development — has devices with identical names, so tests silently
  attach to the wrong one and record nothing. Diffing the device list around
  creation looks like the fix but is not: event numbers are recycled, so a node
  still being torn down can hold the number the new device is about to get.
  `virtual._node_of` uses the `UI_GET_SYSNAME` ioctl, which asks the fd itself.
- Tests must assume **other input devices exist**, including real controllers
  and another padmode instance. Assertions of the form "the only gamepad is the
  synthetic one" fail on any developer machine with a pad plugged in.

---

## 6. Gotchas inherited from the working setup

Learned the hard way; re-learning them is expensive.

1. **Release the grab *before* the previous window regains focus.** Otherwise a
   still-held return button leaks a raw gamepad event into the newly-focused
   window. This crashed Pegasus. The old scripts used a 0.7s `SETTLE` delay
   between release and re-activation — keep an equivalent, and keep it
   configurable. *Live since v0.3:* `windows.settle`, deferred onto the tick
   rather than slept through, in `Daemon._schedule_return`. The ordering has a
   test of its own; do not "simplify" it into a sleep.
2. ~~**Exclusive-fullscreen games cannot be overlaid.**~~ **Measured false on
   KWin Wayland.** A Steam game reporting `fullscreen: true` at 2560x1440 was
   overlaid with a browser and driven by the pad, then returned to. The claim
   came from the X11 setup this replaces, where a game could mode-set and grab
   the display outright; KWin drops out of direct scanout when something has to
   be drawn on top. Do not tell users borderless windowed is a requirement.
   Individual games may still resist (gamescope sessions, anything bypassing
   the compositor), so treat it as per-game rather than a rule — the check is
   in `docs/window-backends.md`.
3. **Focus-follows-mouse steals focus back from the window you return to**, if
   the overlay is still on screen under the cursor. Traced on a real game: the
   game got focus, and 460ms later the overlay took it back with the cursor
   never moving. *Since v0.3.2 the fix is `windows.minimize_on_return`, on by
   default* — an overlay that is off the screen cannot take focus, and KWin
   still finds a minimized window by class, so the next press raises it rather
   than launching a second copy (verified).

   `windows.park_pointer` remains, off by default, for anyone who turns
   minimizing off; it then lands the cursor inside the returned-to window and
   clear of the overlay. Two failed versions of that rule are recorded in
   `docs/window-backends.md` — do not re-derive it. And note that **no
   compositor here can warp the cursor**, so parking drives our own relative
   pointer in a measure-and-correct loop that has still never been checked
   against real pointer acceleration.
4. **KWin "Keep above" silently breaks Alt+Tab.** Use focus protection instead.
   Relevant to the printed manual instructions, not to our code.
5. **Gamepads re-enumerate.** The read loop must survive the device vanishing
   and reappearing; reconnect rather than exit.

---

## 7. Planned layout

Present tense = exists today; *italic* = planned, do not create empty stubs.

```
padmode/
  __main__.py               # CLI: run, devices, actions, config
  daemon.py                 # decide() + Daemon; the state machine
  config.py                 # TOML, defaults, validation
  buttons.py                # friendly names <-> evdev codes
  input/reader.py           # device discovery, read loop, grab/ungrab, ticks
  input/virtual.py          # uinput virtual mouse + keyboard
  actions/builtin.py        # action registry (pure argv builders)
  actions/runner.py         # probing + execution
  windows/backend.py        # the interface + subprocess plumbing
  windows/kwin.py           # kdotool
  windows/x11.py            # xdotool
  windows/__init__.py       # session detection
  modes/                    # *later* split out of daemon.py when it earns it
contrib/metadata/           # *v0.4* heroic2pegasus, steam2pegasus
packaging/                  # udev rule, systemd unit template; *PKGBUILD*
tests/                      # unit + synthetic-uinput integration
docs/
install.sh
```

`modes/` does not exist yet on purpose: overlay mode shares nearly all of its
behaviour with desktop mode and differs only at the edges, so the two read
better as one file than as five. Split when a mode arrives that is genuinely
its own thing — unlock (v0.5) probably is.

---

## 8. Working on this machine

Developed on the maintainer's daily driver. The older setup this replaces has
been removed, so nothing else competes for the gamepad any more.

- **Install deliberately, never as a side effect of development.** Run from the
  repo (`python -m padmode`) while working on it. `install.sh` is a separate,
  conscious act.
- **A crash that leaves the pad grabbed makes the machine uncontrollable by
  controller.** There is a keyboard fallback, but treat the `finally` that
  releases the grab (§4) as load-bearing, and never test grab changes without a
  way to kill the daemon from the keyboard.
- Auto-lock at login is still configured (`lock-at-login.sh`) and is unrelated
  to this project. Leave it alone.

## 9. Reference for unbuilt work

Detail for later versions lives in `docs/`, so this file stays a contract
rather than a spec dump. Both were distilled from a working implementation;
prefer them over re-deriving:

- `docs/virtual-input.md` — v0.2 desktop mode and the v0.5 unlock module.
- `docs/window-backends.md` — v0.3 backends, overlay sequencing, cursor
  parking, and the manual checklist §5 requires.
- `docs/metadata-generators.md` — v0.4 Heroic and Steam library scraping.

## 10. Open items

- **Final name.** `padmode` is provisional.
