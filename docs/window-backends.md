# Window backends and overlay mode (v0.3)

Reference for `padmode/windows/` and the overlay half of `padmode/daemon.py`.

v0.3 is the first version that needs a compositor, so it is the first version
that does not work everywhere. That is the whole cost of the feature, and it is
paid in exactly one place: activating a window.

---

## The interface

Three operations are required (CLAUDE.md §3.3):

```python
find_window(window_class) -> WindowId | None
activate_window(window_id) -> None
get_active_window() -> WindowId | None
```

Three more are optional and default to returning `None`, so a backend that
ignores them simply disables the features built on them:

```python
window_geometry(window_id) -> Rect | None   # park_pointer
pointer_position() -> tuple[int, int] | None # park_pointer
window_class(window_id) -> str | None        # `padmode windows` only
```

`WindowId` is opaque. KWin hands out brace-wrapped UUIDs
(`{da465e33-2ea3-49a4-bc0d-c561aa268d4b}`), X11 hands out decimal numbers
(`2097152`), and nothing above the backend is allowed to care.

Both shipped backends are external commands, not libraries, so the entire layer
is argv in and text out — which is why it can be tested without a compositor by
injecting a fake runner.

## Detection

| Session | Desktop | Backend | Tool |
|---|---|---|---|
| Wayland | KDE / Plasma | `kwin` | `kdotool` |
| Wayland | GNOME | **none** | — |
| Wayland | anything else | **none** | — |
| X11 | any, including KDE | `x11` | `xdotool` |

Overridable with `[windows] backend = "kwin" | "x11" | "none"`, which skips
session sniffing entirely — somebody running a compositor we have never heard
of may well know that `xdotool` works there.

Two details worth keeping:

- **`WAYLAND_DISPLAY` is checked before `DISPLAY`.** A Wayland session almost
  always has `DISPLAY` set as well, for XWayland. Testing `DISPLAY` first would
  classify every KDE Wayland session as X11.
- **X11 sessions get `xdotool` even on KDE.** `kdotool` works there too, but
  `xdotool` is the better-trodden path and does not inject a script into the
  compositor to answer a question.

### GNOME Wayland

Not supported, and not a bug to be fixed later. GNOME's compositor refuses
window activation requests from clients by design; there is no call for a
backend to make. It would need a GNOME Shell extension, which is out of scope.
Everything in v0.1 and v0.2 still works there — virtual input devices are
ordinary hardware as far as any compositor is concerned.

## The tools

### kdotool (KWin)

KWin exposes no window control to ordinary Wayland clients. `kdotool` reaches
the compositor by loading a KWin script over D-Bus, which is the sanctioned
route. Each call is a short-lived process; measured at roughly 3 ms on a warm
Plasma 6.7 session, which is why overlay mode can afford to shell out per
operation instead of holding a connection open.

Output formats, captured from kdotool 0.2.3, and pinned in `tests/test_windows.py`:

```
$ kdotool getactivewindow
{da465e33-2ea3-49a4-bc0d-c561aa268d4b}

$ kdotool getwindowgeometry {da465e33-...}
Window {da465e33-2ea3-49a4-bc0d-c561aa268d4b}
  Position: 1544,42
  Geometry: 2544x1390

$ kdotool getmouselocation
x:2684 y:1238 screen:1 window:{da465e33-...}
```

**Geometry is not always whole pixels.** KWin reports *logical* coordinates,
so on a scaled screen the same command prints `Geometry: 1520x813.5999999999999`.
Parsing that with `int()` raises, `window_geometry` returns None, and cursor
parking silently stops working on exactly the windows it was asked about. The
first version of this backend had that bug; it survived a full unit-test suite
because the sample it was written against happened to be integral, and it was
found the moment the parser met a real window. Both forms are pinned in
`tests/test_windows.py` now.

`search --class` takes a **regular expression** and is case-insensitive unless
`-C` is passed, so `class = "discord"` matches a window whose class is
`Discord`. A search that matches nothing exits 0 and prints nothing.

There is **no `mousemove`**: kdotool can read the cursor but not place it. See
parking, below.

### xdotool (X11)

Covers all six operations on its own, so `wmctrl` is not used — it can
enumerate and activate windows but cannot report the cursor, which would leave
parking half-implemented for no gain.

`--onlyvisible` on `search` matters more than it looks: X11 applications keep
unmapped utility and icon windows of the same class around, and activating one
of those focuses nothing the user can see.

`getwindowclassname` prints the literal `(null)` and still exits 0 for a window
with no `WM_CLASS`; the backend filters that out.

**This backend has never run against a live X11 session.** It is covered by
unit tests at the argv-and-parsing level only. The checklist below is what
somebody with an X11 session should walk through.

## Overlay mode

Entering (`Guide` + a button bound to `overlay.<name>`):

1. Refuse early if there is no backend, no virtual input, or nothing to show —
   a transition that cannot work leaves the session exactly as it found it.
2. Read the active window **first**, before anything can steal focus. This is
   the thing we will return to.
3. `find_window(class)`. If it exists, activate it. If it does not and a
   `command` is configured, spawn it and poll every 250 ms until its window
   appears or `launch_timeout` expires.
4. The pad stays grabbed and drives the pointer, exactly as in desktop mode.

Leaving (the same exit button as desktop mode):

1. Release everything held on the virtual devices.
2. **Release the grab.**
3. Wait `settle` (default 0.7 s).
4. Minimize the overlay, unless `minimize_on_return = false`.
5. Put the cursor back, if it was parked.
6. Re-activate the remembered window.

Step 2 before step 6 is gotcha §6.1, and it is not optional: a still-held
return button leaking a raw gamepad event into the newly focused window is what
used to crash the frontend. The wait is *deferred onto the tick* rather than
slept through, so the read loop stays alive and SIGTERM still gets the pad
released. A daemon shut down mid-settle performs the return on the way out.

Things that follow from this design and are easy to get wrong later:

- **A new overlay cancels a return that is still pending.** Otherwise the old
  return fires mid-overlay and yanks focus away from the app just opened.
- **Launch polling is rate-limited**, not once per tick. Each look spawns a
  process; sixty a second would be absurd.
- **Failing to launch does not throw the user out of overlay mode.** The pad
  still drives the desktop, and the exit button still works.
- **Desktop mode is untouched.** It remembers nothing and returns nowhere, the
  same as in v0.2.

### Exclusive fullscreen

Cannot be overlaid, per gotcha §6.2. Borderless windowed is a requirement, not
a suggestion — do not try to work around it.

## Minimizing on return, and why it is the default

Traced on a real fullscreen game with `watch_focus.py`:

```
t=+ 13.81  active=steam_app_3156330  cursor=(3472,722)  over=steam_app_3156330
t=+ 14.27  active=zen                cursor=(3472,722)  over=zen
```

The return worked — the game had focus. Then 460 ms later the compositor gave
it back to the browser, with the cursor never moving a pixel. That is
focus-follows-mouse acting on a pointer that is still over the overlay, and no
amount of care on our side of the activation call can prevent it while the
overlay is visible underneath the cursor.

So the overlay is minimized on the way out. It cannot take focus from off the
screen, the game is guaranteed unobscured, and — checked before committing to
it — **KWin still returns a minimized window from `search --class`**, so the
next press raises that window instead of launching a second copy of the app.
`windowactivate` restores and focuses it in one call.

`minimize_on_return = false` restores the old behaviour for anyone who would
rather the overlay stayed on screen.

An earlier draft of this section warned that apps set to *minimize to the system
tray* would vanish instead. That was written from plausibility, not
observation — and the Discord test above had already shown the opposite on this
machine. It has been removed. If some app ever does behave that way, trace it
and write down what actually happened; do not restore a guess.

## Parking the cursor (`park_pointer`)

Off by default, and largely superseded by minimizing — it matters when
`minimize_on_return = false`, where the overlay stays visible and the cursor
has to be kept off it (gotcha §6.3).

The awkward part: **nothing here can warp the cursor.** KWin's scripting API
exposes no cursor placement, `xdotool mousemove` does not move the real cursor
on a Wayland session, and our own virtual pointer is a *relative* device. So
parking is done by moving our pointer and looking at where it ended up:

```
gain = 1.0
repeat up to 6 times:
    here = pointer_position()
    if within 8px of the target: done
    ask for (target - here) / gain
    gain = (how far it actually moved) / (how far we asked)
```

The feedback loop exists because pointer acceleration is not ours to predict: a
relative delta does not travel as far as its arithmetic says, and how far off
it is depends on a curve we do not get to see. Measuring the gain each round
converges whether the session accelerates or decelerates us, without assuming
anything about the curve.

> **Not yet measured on real hardware.** The loop is bounded and the feature is
> off by default, so the worst case is a cursor that stops short of the target.
> Before tuning any constant in it, trace the real thing (CLAUDE.md §4):
> `watch_virtual.py` shows what our pointer emitted, and `kdotool
> getmouselocation` shows where the cursor actually went. Do not adjust
> `WARP_ATTEMPTS`, `WARP_TOLERANCE` or `WARP_LATENCY` on the strength of a
> theory about acceleration.

The cursor is restored **before** focus returns to the game, not after: a
relative motion delivered to a focused game reads as a camera turn.

**Where it is restored to is not always where it started.** Under
focus-follows-mouse the cursor decides who has focus, so leaving it anywhere
the overlay still covers hands focus straight back to the overlay — the
pointer is sitting on it — and it bounces to the front the instant you return.

The test is *not* "is the origin inside the window we are going back to". That
version shipped in v0.3.1 and was measured useless within the hour: a
fullscreen game at 1536,0 2560x1440 with a browser overlay at 1966,293
1626x954 has the browser **wholly inside** the game, so the origin is inside
the game essentially always and under the browser at the same time. What the
cursor needs is a spot inside the window being returned to **and** outside the
overlay. `Daemon._homecoming` tries the origin first, then the centre, then
points a sixth of the way in from each side.

Corners are deliberately not candidates: on a fullscreen window they are
*screen* corners, and landing on one fires whatever hot corner the user has
configured. If no spot is clear — an overlay covering the whole window — the
cursor stays where it started, because moving it would not help.

## Exclusive fullscreen, and how to tell

KWin answers the window-state half directly, with no extra tool. Run this and
click into the game within five seconds:

```bash
sleep 5 && qdbus6 org.kde.KWin /KWin org.kde.KWin.getWindowInfo "$(kdotool getactivewindow)"
```

Among the keys returned are `fullscreen`, `keepAbove`, `noBorder`, `type`,
`resourceClass` and the geometry.

That flag does **not** settle whether the game can be overlaid, though. On
Wayland, borderless-fullscreen and exclusive-fullscreen both report
`fullscreen: true`; what actually blocks an overlay is the compositor handing
the game's buffer straight to the display. The definitive test is to press the
overlay button and look.

**And the answer, measured, is that fullscreen is not a blocker here.** A Steam
game (`resourceClass: steam_app_3156330`, `fullscreen: true`, 2560x1440) was
overlaid with a browser, driven by the pad, and returned to. KWin evidently
stops scanning out directly the moment another surface must be composited on
top. Gotcha §6.2 came from the X11-era setup this replaces, where a game could
mode-set and grab the display; it has been corrected rather than repeated.

That is one game, not a proof about all of them. A title running under its own
gamescope session, or one that keeps the compositor out of the loop some other
way, may still refuse. Treat it as per-game, and use the recipe above plus the
overlay button to find out in ten seconds.

---

## Manual test checklist

Nothing below is automated, and none of it can be (CLAUDE.md §5). Run it on a
real session after changing anything in `windows/` or the overlay paths.

Before starting, make sure exactly one padmode is running, or nothing observed
is trustworthy:

```bash
systemctl --user stop padmode.service; pkill -f 'python -m padmode'
```

### KDE Wayland

- [ ] `padmode windows` names the `kwin` backend and finds `kdotool`.
- [ ] With an app focused, `padmode windows` prints that app's class, and the
      class it prints works as `class = ` in config.
- [ ] `padmode windows` lists each configured overlay as found or not running.
- [ ] **Raise an open app.** With the overlay app already running, hold Guide
      and press its button: the app comes to the front and the pad moves its
      pointer.
- [ ] **Return.** Press the exit button: focus goes back to the window that was
      active before, and the pad belongs to it again.
- [ ] **Launch a closed app.** Quit the app, press the combo: it starts, and is
      raised once its window appears.
- [ ] **Launch failure.** Point an overlay at a command that exits immediately;
      after `launch_timeout` the log says so and the pad still works.
- [ ] **Over a game.** With a borderless-windowed game running, overlay and
      return. The game keeps running and receives no stray input.
- [ ] **Hold the exit button down** for a second or two while returning. The
      game must not receive a gamepad event from it (gotcha §6.1).
- [ ] **A fullscreen game.** Confirmed working once; check it again after any
      change to the overlay path, since it is the case the feature exists for.
- [ ] **Second monitor.** Overlay an app that lives on another screen.
- [ ] **Kill the daemon mid-settle** (exit the overlay, then Ctrl-C within
      0.7 s): focus still returns.
- [ ] **No backend.** Set `[windows] backend = "none"`, start the daemon: one
      warning at startup, overlay buttons do nothing, everything else works.
- [ ] With `park_pointer = true`: the cursor lands on the overlay window and
      comes back to where it started. Measure it rather than eyeballing it.

### X11 (never yet run)

Everything above, plus:

- [ ] `padmode windows` picks the `x11` backend on an X11 session, even on KDE.
- [ ] `find_window` does not match an invisible window of the right class
      (start the app, hide its window however that app allows, press the combo).
