# padmode

Turn any Linux desktop into a couch/console experience driven entirely by a
gamepad: media and volume control without leaving your game, a gamepad-as-mouse
desktop mode, app overlays over a running game, and (opt-in) unlocking the
screen with the pad.

`padmode` is a provisional name — final name TBD. Do not use "Steam Machine"
or other Valve product names in the shipped tool, package, or docs.

**Status: v0.7.0** (Guide-hold media/volume/mic; uinput virtual
mouse/keyboard; desktop mode with bindable stick directions; window backends
and overlay mode with return-to-game; frontend integration and the Heroic and
Steam metadata generators; on-screen keyboard with auto-open; opt-in unlock
with the pad; 669 tests). This document is the
contract. If something here is wrong, fix this file in the same change as the
code.

The keyboard is finished and its manual checklist is complete: it types, spans
and follows the active output, draws over a fullscreen game, survives its
renderer being killed, and opens itself when you click into a text field. The
round trip the ownership split costs is ~1 ms. What is not established is
breadth — one game, one compositor, one pad, and auto-open tried only against
Qt applications — which is what `docs/on-screen-keyboard.md` §Still unmeasured
now lists.

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
- Not an accessibility tool. We *read* AT-SPI to notice when a text field has
  focus, so the keyboard can open itself; we never expose padmode's own
  interface to assistive technology, and we read roles and states only, never
  content.

**One deliberate widening:** auto-open is the first feature that needs to know
something about *application content* rather than input and windows. It is
opt-outable, it degrades to nothing when accessibility is switched off, and it
buys the single behaviour that makes the keyboard feel finished. Anything
further in that direction should be argued for on its own, not waved through as
precedent.

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
| Metadata generators | **Same repo, own subcommand** (`padmode metadata …`), imported lazily by the CLI and **never by the daemon** — enforced by a test, not by good intentions |
| Anything that draws | **A separate process.** The on-screen keyboard (v0.5) is the first: GTK4 + `gtk4-layer-shell`, spawned as `python -m padmode.osk`, talking line-JSON over a pipe. Its toolkit is an *optional runtime dependency* probed like `playerctl` — missing means that one feature is disabled, never that the daemon fails to start, and a test fails if anything on the daemon's import path reaches `gi` |
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
- **Analogue triggers are translated into buttons** before `decide()` sees
  them, so `LT`/`RT` work anywhere a button works — modifier, `desktop.enter`,
  any bindings table — with no downstream branch knowing they came from an
  axis. Most pads report the triggers only as `ABS_Z`/`ABS_RZ` and never emit
  `BTN_TL2`/`BTN_TR2`, which made them silently unbindable until v0.5. A pad
  that reports *both* is left alone, or every press would fire twice; the
  reader decides which from the device's capabilities. Confirmed on a real
  analogue-trigger pad, not only against a synthetic one.

---

## 3. Architecture

### 3.1 Single daemon, explicit modes

One process opens the gamepad and never lets go of ownership. Modes:

- `idle` — pad belongs to whatever is running (game, frontend). We read
  **without grabbing**.
- `guide_held` — momentarily grabbed; buttons are commands, not game input.
- `desktop` — virtual mouse/keyboard active; pad drives the desktop.
- `overlay` — like `desktop`, but we remember a window to return to.
- `unlock` — a digits-only keypad types the password at the lock screen
  (opt-in). Entered and left by the lock watcher, never by a button.

Mode transitions are the whole app. Keep them in one readable place.

**The on-screen keyboard is not a mode.** It was listed here as one until v0.5,
on the grounds that it would need to be *concurrent* with another mode — the
pointer stays useful while typing — which the state machine had never had to
express. Building it showed the concurrency never needed expressing: `osk` is a
**flag on the state**, valid only while the mode is in `PAD_MODES`. While it is
set the d-pad and the buttons in `[osk.bindings]` belong to the keyboard, and
every other button keeps its desktop binding. One boolean and one routing rule,
instead of a fifth mode that would have been 90% a copy of desktop. See
`docs/on-screen-keyboard.md`.

The **left stick drives the grid** while it is up, and the pointer is parked.
This reverses "the sticks are untouched", written when the keyboard first
landed: the shortcut map had by then taken `A`, `B` and `X` — every mouse
button — so the pointer could be moved but not clicked with, and giving the
stick to the grid was a correction rather than a loss. The right stick still
scrolls and still tabs. The digestion lives in the daemon, so the renderer only
ever receives the discrete steps a d-pad produces.

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
on them rather than the backend: `minimize_window` (used on every return),
`window_geometry` and `pointer_position` (for `park_pointer`), and
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

### 3.4 The frontend

Configured in `[frontend]` (a window class, and optionally a command), and used
for two things:

- **A binding.** `SELECT = "frontend"` raises it and hands the pad *back* —
  it is not a mode, because the frontend reads the gamepad itself. It **may
  start** the frontend if it is not running.
- **The return-target fallback.** A window id outlives its window (§6.6), so
  when the game exits while an overlay is up, the return activates a dead id
  and strands the user on the desktop holding a controller. After every return
  we check where focus actually landed and, if it is not where we sent it, go
  to the frontend instead. This path **never starts anything**: a game crashing
  is not a reason to spawn a process on the user's behalf.

Checking the outcome rather than testing the id for liveness is deliberate: it
needs only `get_active_window`, which every backend must implement anyway, and
it measures the thing we care about. Both timing and the activation itself are
deferred exactly as an overlay return is, so §6.1's ordering still holds.

Core stays frontend-agnostic: nothing here knows what Pegasus is.

### 3.5 Roadmap

| Version | Contents | Compositor dependency |
|---|---|---|
| ~~**v0.1**~~ ✅ | evdev reader, Guide-hold media/volume/mic, config, systemd unit, udev rule, `install.sh` | **None** — works on every Linux desktop |
| ~~**v0.2**~~ ✅ | uinput virtual device, desktop mode (pad as mouse), return button | **None** — confirmed: no window code was needed |
| ~~**v0.3**~~ ✅ | Window backends, overlay mode, return-to-game | KDE + X11 |
| ~~**v0.4**~~ ✅ | Frontend integration (return target), Heroic metadata generator | KDE + X11 |
| ~~**v0.5**~~ ✅ | On-screen keyboard, console style (`docs/on-screen-keyboard.md`) | **KDE Wayland only** — layer-shell, not window activation. X11 was dropped from this version: it needs override-redirect, which is different code, not a flag. The keyboard is therefore *narrower* than overlays, and the README says so |
| ~~**v0.6**~~ ✅ | Steam metadata generator (`docs/metadata-generators.md`) | None |
| ~~**v0.7**~~ ✅ | Unlock module, opt-in, off by default (`padmode/lock.py`, `padmode/unlock.py`, `docs/virtual-input.md`) | Screensaver D-Bus — **verified on KDE only**. The OSK is *not* the unlock UI: KWin hides it at the lock screen |
| **v0.8** | Settings GUI: edit the config from a window, drivable with the pad — *built, not released* (`padmode/settings/`) | None — an ordinary window |

Do not pull work earlier in this list. The point of the ordering is that v0.1
and v0.2 are *universally useful on their own*.

**v0.8, the settings GUI** (added 2026-09-23, specified and built 2026-10-22). Today every
setting — bindings, overlays, the frontend, the Steam ignore list — means
editing `config.toml` by hand. What is already decided elsewhere, so it is not
re-derived:

- It **draws, so it is a separate process** (§2), and its toolkit is an
  optional runtime dependency, probed like the keyboard's: GTK4 missing means
  no GUI, never a daemon that fails to start.
- It **saves only what the loaders accept**: `config.parse` for the daemon's
  sections, `steam.parse_ignore` for `[metadata.steam.ignore]`. Bad config is
  a startup failure for the daemon (the rule at the top of `config.py`), so a
  GUI that can write a file the daemon refuses would turn a typo into a pad
  that does nothing.
- It is **not a game launcher.** "Should we ship a frontend of our own?"
  (§10) is a different question and stays open.

The known hard part: **the stdlib reads TOML but cannot write it.** Writing
the file back without losing the user's comments and layout needs either a
small writer for our own schema, or edits made in place. Decide that first.

**Decided with the maintainer, 2026-10-22:**

- **Edits are made in place.** A changed value is rewritten on its own line; a
  missing key is appended to its table, a missing table to the end of the
  file. Comments, order and blank lines the user wrote survive. Our schema is
  numbers, strings, booleans and string lists, which is what makes a writer
  this narrow honest. Not a separate `gui.toml` (one setting in two places),
  not a regenerated file (the user's comments lost).
- **An ordinary window that the pad drives natively** — not through desktop
  mode's pointer. *Revised the same day:* the first answer was "drive it in
  desktop mode, for free", on the grounds that native pad support needs the
  daemon to hand the pad to the window. The maintainer asked why not native;
  press-to-assign needs exactly that channel anyway, and the keyboard already
  proved the pattern (the daemon owns the pad, digests sticks into steps, the
  renderer only hears steps). So: a `SETTINGS` pad mode, the OSK's twin.
  The window stays an ordinary window, not full screen; the mouse and the
  keyboard still work, because pad, keys and mouse all move the same focus.

  | Pad | In the settings window |
  |---|---|
  | D-pad / left stick | Move focus (repeats while held, the keyboard's clock) |
  | A | Select, toggle, or open the on-screen keyboard on a text field |
  | B | Back; at the top, close (asking if anything is unapplied) |
  | LB / RB | Previous / next page |
  | Start | Apply |
  | Y | Reset the focused setting to its default |
  | Right stick | Scroll a long list |
  | Guide | Let go of the pad; the window stays open for the mouse |

  Text fields hand over to the on-screen keyboard and come back to `SETTINGS`
  when it closes — `osk_return` already does that for desktop mode.
- **First version covers everything users edit by hand today:** bindings
  (Guide combos, desktop mode, unlock — the Xbox layout one choice away),
  feel (pointer and scroll speed, dead zone, curve, keyboard size and edge),
  overlays and the frontend, and the Steam ignore list.
- **Nothing is saved until Apply.** Apply writes the file and the daemon picks
  it up at once. Closing with unapplied changes asks: apply, discard and close,
  or go back.
- **Rebinding is press-to-assign**, the console way: the window asks for a
  button, you press it on the pad, that is the button — stick directions
  included. Picking from a list stays as the fallback. The daemon holds the
  pad in desktop mode, so it is the daemon that reports the press.
- **Guide + R3 opens it**, as a default binding (`R3 = "settings"`) that can be
  rebound like any other, and the pad is the window's at once. Opened from the
  application menu or `padmode settings` instead, the window asks the daemon
  for the pad itself.

**Look, decided with the maintainer (2026-10-22):** the *console* layout —
tabs down the left rail with a highlight that slides with squash and stretch
(`settings/motion.py`), large rows, the pad's face buttons as coloured badges
in their own colours, a hint bar along the bottom (A Select, B Back, LB/RB
Page, Start Apply, Y Default, Guide Let go). Five themes, chosen in the window
(`[appearance] theme`): `system` (libadwaita follows the desktop), `dracula`
(the maintainer's usual), `night`, `green`, `amber`. A theme is a palette *and*
a shape: corner radius and how the focused row shows — fill, ring or bar
(`settings/themes.py`). Colour was one aesthetic decision of several;
the rest are decided one at a time. **Typography: Nunito** (rounded, friendly,
Switch-like; heavy headings) — shipped with the window under the OFL rather
than expected on the system. **Density: couch** — read from across the room: base
type 1.3×, 76 px rows, 32 px badges, the window opening at 1280×900. Depth,
iconography, motion and sound are still to be decided.

How it is built, in order — each step usable and tested before the next:

1. ✅ `padmode/tomledit.py`: the in-place writer. Pure stdlib; every edit is
   re-read with `tomllib` and compared with the document it was meant to
   produce, and refused (text untouched) if anything else moved. Inline
   tables, dotted keys and arrays of tables are refused, not guessed at.
   `config.write` is the only door to the disk: `check_text` (the daemon's
   parse and the Steam ignore list), then an atomic rename keeping the file's
   mode.
2. ✅ A control socket on the daemon (`padmode/control.py`,
   `$XDG_RUNTIME_DIR/padmode.sock`, line JSON like the keyboard's pipe):
   `reload` for Apply, `capture` for press-to-assign, `hold`/`release` for
   `Mode.SETTINGS`, `keyboard` for a text field. One epoll descriptor for the
   listener and every client, watched by the reader beside the pad. Mode 0600,
   and `SO_PEERCRED` checked besides. A reload that fails keeps the old config
   and says why; settings read only at startup (the pad, the window backend,
   unlock on/off, whether desktop mode's devices exist) come back as
   `restart`. Locking the screen takes the pad from the window like from any
   mode. Measured live: reload 0.6 ms; the first hold ~200 ms (it creates the
   virtual devices the keyboard will type with), later ones immediate. **The
   live test found that the reader slept while no pad was connected, so a
   reload with the controller off waited forever** — it now waits on watched
   descriptors instead of sleeping.
3. ✅ The window (`python -m padmode.settings`, GTK 4 + libadwaita, its own
   process): `model.py` holds every edit (stdlib, tested), `link.py` the
   socket, `app.py` only draws and routes input through per-row handlers.
   **Pad navigation is ours, not GTK's:** up/down go row to row, left/right
   adjust or walk the row's own buttons. GTK's directional focus stepped into
   a row's trash button on "down", and libadwaita's dialogs left focus on an
   internal widget that directional focus could not leave — both measured.
   B leaves a text field only if A entered it; a number's box holds text
   focus too, and B on it closed nothing until that was told apart.
4. ✅ The four pages: bindings (press-to-assign; the button's old job is
   named, never dropped silently), feel, apps (overlays, frontend, timing),
   Steam ignore list with a write-it-now button.
5. ✅ The `settings` binding (R3 by default; gives way to `desktop.enter =
   "R3"` so older configs still start), `padmode settings` (opens even on a
   config the daemon refuses), the menu entry via `install.sh`, docs.
   Checked end to end by `tools/settings_e2e.py`: a synthetic pad, the real
   daemon, the window on broadway — assign, a number, reset, close-and-apply.
   **Not yet tried:** typing into a text field through the OSK; a real pad;
   KDE's focus-stealing rules when Guide + R3 starts the window. The release
   waits for the maintainer.

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

**The tests that drive the real desktop are opt-in.** Creating a uinput
*gamepad* is harmless — it only gives padmode something to read — but the
handful of tests that also build padmode's virtual mouse and keyboard emit
real input: the pointer moves, the screen scrolls, windows alt-tab on whatever
session is running. On a machine someone is sitting at, that is disruptive
enough to have been asked about, so they are behind `PADMODE_INTEGRATION=1`
and skip by default (`tests.drives_the_desktop`, a class-level decorator).
Before a release, buy the coverage back with:

```bash
PADMODE_INTEGRATION=1 python -m unittest discover -s tests -t .
```

The default run still covers discovery, grabbing and the disconnect path via
`test_integration.py`, which creates a pad but no virtual output. Anything new
that builds a `VirtualInput` must carry the decorator.

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
- **The synthetic pad is itself a uinput device, so device selection must
  never exclude those outright.** `find_gamepad` *ranks* real hardware above
  virtual devices (§6.8) rather than filtering virtual ones out — which is what
  keeps the synthetic pad findable while Steam's inert pad loses to a real one.
  A filter would have taken the whole device-discovery test strategy with it.
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
   never moving. *Since v0.3.2 the overlay is always minimized on the way out*
   — an overlay that is off the screen cannot take focus, and KWin still finds
   a minimized window by class, so the next press raises it rather than
   launching a second copy (verified). **Not configurable, deliberately**: no
   setting makes "the overlay steals focus back" a good outcome, and every
   switch is another combination that has to work.

   `windows.park_pointer` remains, off by default, and now does only the
   symmetrical thing: cursor onto the overlay when it opens, back where it
   started when you leave. Two attempts at a cleverer return rule were both
   killed by measurement within hours and are recorded in
   `docs/window-backends.md` — do not re-derive them. Note also that **no
   compositor here can warp the cursor**, so parking drives our own relative
   pointer in a measure-and-correct loop that has still never been checked
   against real pointer acceleration.
4. **KWin "Keep above" silently breaks Alt+Tab.** Use focus protection instead.
   Relevant to the printed manual instructions, not to our code.
5. **Gamepads re-enumerate.** The read loop must survive the device vanishing
   and reappearing; reconnect rather than exit.
6. **A window id outlives its window, silently.** Measured on Plasma 6.7 with
   two invented UUIDs: `kdotool windowactivate <dead id>` exits **0**, prints
   nothing and does nothing — and `getwindowclassname <dead id>` does exactly
   the same. So every "did it work?" signal a subprocess backend has says yes,
   *and* the obvious pre-check ("ask whether the id still resolves") is just as
   blind as the operation it was meant to guard. This is why overlay mode
   cannot trust its remembered return target: if the game exits while the
   overlay is up, the return "succeeds" and leaves the user on the desktop
   holding a controller. The only reliable signal is where focus actually ended
   up afterwards, which is what §3.4 checks. Assume the same of any backend
   operation that takes an id.

7. **The grab eats the modifier's release, and the game keeps the button.**
   We read without grabbing until the modifier goes down, so its *press* has
   already been delivered to everything else reading the device. We then grab,
   and the matching *release* arrives while we hold it — so nobody else ever
   sees the button come up, and a game is left with the modifier latched down.

   Measured with a second, non-grabbing reader on a Sunshine virtual pad with
   `modifier = "SELECT"`: **42 presses reached it, and one release.** That one
   escaped only because it followed the press that *left* desktop mode, which
   drops the grab first — which is precisely why the reported symptom was "it
   works once per exit, then stops".

   This is the real reason `GUIDE` is the default modifier, and a stronger one
   than "games might bind Guide": Guide is the button games reliably ignore, so
   it is the only one whose latching costs nothing. **Never suggest moving
   `modifier` onto SELECT or START.** The README recommended exactly that until
   this was measured — advice that breaks games, written from the same
   plausible reasoning §4 warns about.

   There is no small fix. The release cannot be un-eaten, and delaying the grab
   until a bound button fires only moves the leak onto that button. The real
   fix is the passthrough question in §10.

   *Numbered 7 rather than inserted in order because §6.1, §6.2, §6.3 and §6.6
   are cited by name from other documents and from the code.*

8. **Steam publishes an inert twin of every pad you connect.**
   With Steam running — the normal case on a gaming desktop — connecting a controller also produces a uinput pad called
   `Microsoft X-Box 360 pad <n>`, with a real 360 pad's capabilities and a USB
   bus type. It never emits anything. Measured: the twin appears within a
   second of the pad (creating a synthetic pad reliably produced a second
   twin), and disappears when the pad is unplugged — it shadows hardware
   rather than standing in for it.
   Measured 2026-09-21 on KDE Wayland with Steam idle in the tray, both nodes
   read at once for 25 seconds and no grab held: the real pad (`event28`)
   delivered **36558 events**, Steam's (`event256`) delivered **zero**.

   Two separate faults let us bind it. `_candidates()` sorted paths as
   *strings*, so `event256` came before `event28` and device order was
   effectively arbitrary above `event9`. And nothing distinguished a live pad
   from an inert one: the name is identical to real hardware, so no name test
   can work, and `VIRTUAL_PREFIX` only ever skipped our own devices. The
   result was total silence — every binding did nothing, with no error
   anywhere, which is why it went unnoticed.

   The rule is now **parentage, read from sysfs**: a uinput node has no
   `/sys/class/input/<node>/device/device`, and hardware always has one. It is
   deliberately *not* either signal that looks sufficient:

   - **an empty `phys`** — true of Steam's pad, but our own virtual devices
     report `py-evdev-uinput`, python-evdev's default, so every uinput pad
     built with the library we ourselves use would pass;
   - **a sysfs path under `/devices/virtual`** — a Bluetooth pad arrives over
     uhid and is published at
     `/sys/devices/virtual/misc/uhid/0005:.../input/inputN`, *inside*
     `/devices/virtual`, so that test rejects every Bluetooth controller.
     Measured by creating one through `/dev/uhid` rather than assumed.

   Real hardware **outranks** a virtual pad rather than excluding it: the
   integration tests' synthetic pad is uinput too (§5), and a virtual pad is a
   better answer than none provided we say so. Landing on one logs a warning
   and makes `padmode devices` exit 1. A host where the pad in use is
   legitimately virtual — a Sunshine pad, as in §6.7 — should pin
   `device.path`, which still wins outright.

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
  metadata/heroic.py        # Heroic library -> metadata; never imported above
  metadata/pegasus.py       # the Pegasus writer
  metadata/steam.py         # Steam library -> metadata (*v0.6*)
  metadata/images.py        # the image check both generators share
  metadata/vdf.py           # Valve's KeyValues text format. stdlib
  osk/protocol.py           # line-JSON messages; stdlib, imported by both
  osk/client.py             # the daemon's end: spawn, talk, bury. stdlib
  osk/keys.py               # the grid: what is on it, how wide. stdlib
  osk/model.py              # navigation, shift, and where the panel may sit. stdlib
  osk/view.py               # the widgets, the CSS, and how big a key is. GTK4
  osk/surface.py            # the layer surface: mapping, measuring, margins
  osk/focus.py              # AT-SPI watcher, for auto-open
  osk/app.py                # wires the four together. Never imported by the daemon
  osk/__main__.py           # `python -m padmode.osk`
  modes/                    # *later* split out of daemon.py when it earns it
packaging/                  # udev rule, systemd units; *PKGBUILD*
tests/                      # unit + synthetic-uinput integration
docs/
install.sh
```

`metadata/` sits inside the package because it is reached as `padmode metadata
heroic`, but it is not part of the daemon in any other sense: nothing outside
`__main__.cmd_metadata` may import it, and `tests/test_metadata.py` fails if
anything does. That includes building the argument parser, which happens on
every `padmode run` — so the CLI declares the generator's flags itself and the
generator resolves them.

`modes/` does not exist yet on purpose: overlay mode shares nearly all of its
behaviour with desktop mode and differs only at the edges, so the two read
better as one file than as five. Split when a mode arrives that is genuinely
its own thing — unlock (v0.7) probably is.

---

## 8. Developing on your own desktop

padmode is usually developed on the same machine it runs on, with a real pad
and a real session.

- **Install deliberately, never as a side effect of development.** Run from the
  repo (`python -m padmode`) while working on it. `install.sh` is a separate,
  conscious act.
- **A crash that leaves the pad grabbed makes the machine uncontrollable by
  controller.** There is a keyboard fallback, but treat the `finally` that
  releases the grab (§4) as load-bearing, and never test grab changes without a
  way to kill the daemon from the keyboard.

## 9. Reference

Detail lives in `docs/`, so this file stays a contract rather than a spec dump.

**The README is a front page, not a manual** (maintainer, 2026-10-22): what it
is, a short feature list, the default controls, install, a compatibility
table, one config example, license — the shape of input-remapper's or
sc-controller's. How things work and every option go in `docs/usage.md` and
`docs/configuration.md`; tests and the roadmap in `docs/development.md`. Keep
it under ~150 lines: someone deciding whether to install does not read the
reasoning.
These were distilled from a working implementation; prefer them over
re-deriving:

- `docs/virtual-input.md` — v0.2 desktop mode and the unlock module (v0.7).
- `docs/on-screen-keyboard.md` — v0.5 as built, with the measurements. Our own
  surface **does** show up in `find_window`, reports the interpreter's name and
  cannot be renamed — but it is never the *active* window, so return targets
  are safe. Also: `LD_PRELOAD` is mandatory, the cairo renderer is 14× faster
  to first frame, and typing through uinput assumes a US layout.
- `docs/window-backends.md` — backends, overlay sequencing, the return
  fallback, cursor parking, and the manual checklist §5 requires.
- `docs/metadata-generators.md` — the Heroic generator as built, and the Steam
  one as specified.

## 10. Open items

- **Final name.** `padmode` is provisional.
- **Should we ship a frontend of our own?** *Idea, not a decision, not
  scheduled.* Today the frontend features are only useful once you have
  installed something else — v0.4 could not be tested without building
  `pegasus-frontend-git` from the AUR first, which is exactly the kind of step
  that makes people bounce off a tool (§3.2). A frontend in the box would make
  the repo work on its own, with Pegasus or anything else still selectable by
  config.

  **This reverses a non-goal.** §1 says "Not a game frontend. We integrate
  with frontends; we don't ship one," and §2 locks the core as frontend-
  agnostic. Both would have to be edited on purpose, in the same change, with
  the reason written down — not left to contradict the code.

  Two things worth knowing before deciding, neither of which argues for or
  against:

  - **The pieces arrive anyway.** v0.5 puts a GUI process with a toolkit in
    this repo, and v0.6 adds a second library scraper. A frontend is
    approximately "draw a grid of what the scrapers found and launch it", so
    the marginal cost after v0.6 is far lower than it looks today. That is an
    argument for deciding *after* v0.6 rather than now.
  - **There is a middle option**: a minimal launcher — a pad-navigable grid of
    installed games, no theming, no scraper ecosystem, no competing with
    Pegasus or ES-DE — which is a much smaller thing than "a frontend" and
    might be all that "works out of the box" actually requires.

  If we do ship one, the metadata generators change meaning: they exist to
  *export* to someone else's frontend, and a frontend of our own would read
  libraries directly instead.

- **Should padmode own the pad and re-emit a virtual one?** *Question raised by
  a measurement, not a plan.* Gotcha §6.7: because we only grab once the
  modifier is already down, the game receives the press and never the release,
  and is left with that button latched. It is invisible with `GUIDE` — games
  ignore it — and unusable with any button a game actually reads.

  The fix used by Steam Input and gamescope is **permanent ownership**: grab
  the physical pad at startup, create a virtual gamepad, and forward events to
  it. Games read the virtual device, so we decide exactly what they see —
  including replaying a modifier tap once we know it did not begin a chord.

  **This reverses a locked decision.** §3.1 says `idle` means "the pad belongs
  to whatever is running. We read **without grabbing**", and that is what makes
  padmode additive rather than something that sits between you and your games.
  Ownership would have to be edited there deliberately, with the reason
  written down.

  What it would cost, so nobody re-derives it:

  - A virtual gamepad mirroring every button and axis of the real device,
    including force feedback, which we do not forward today and which games do
    use.
  - Every application that resolves gamepads by name or by `/dev/input` order
    would see a different device. Steam's own controller configuration would
    need to be repointed.
  - The `finally` that releases the grab (§4) stops being a courtesy and
    becomes the difference between "padmode crashed" and "the pad vanished
    mid-game".

  Not scheduled. Revisit if a pad without a usable Guide button turns up, or if
  someone wants the modifier on a button their games read. Until then the
  honest answer is the README's: keep `modifier = "GUIDE"`.
