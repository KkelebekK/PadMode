# Virtual input device (v0.2, v0.7)

Reference for `padmode/input/virtual.py` and the modes built on it.

**Desktop mode (v0.2) is implemented.** The section below documents what
shipped. The unlock module (v0.7) has its first piece: the daemon knows when the
screen is locked; typing at the lock screen is still a specification, written
down because it was learned by building a working version once.

We create our own uinput device with `python-evdev` rather than depending on
input-remapper (CLAUDE.md §3.2). Doing so removes a heavy dependency *and*
deletes "now hand-build two presets in a GUI" from the install instructions.

---

## The fact everything else rests on

**A uinput virtual keyboard is indistinguishable from a real USB keyboard** to
everything above the kernel — compositors, toolkits, and lock screens included.
There is no "synthetic input" flag to detect. This is why unlock (v0.7) is
possible at all, and it is also why the same device can drive an ordinary
desktop session.

Practical consequence: create **separate** virtual devices for the pointer and
the keyboard rather than one combined device. A single device advertising both
relative axes and a full keymap is unusual enough that some applications
misclassify it.

## Desktop mode (v0.2)

Gamepad becomes a mouse and keyboard. No compositor involvement whatsoever —
this is why v0.2 is still universally portable.

Mapping that worked well, and is the default to ship:

| Input | Output |
|---|---|
| Left stick | Pointer motion (`REL_X` / `REL_Y`) |
| Right stick | Scroll (`REL_WHEEL`, and `REL_HWHEEL` for horizontal) |
| A | Left click (`BTN_LEFT`) |
| B | Right click (`BTN_RIGHT`) |
| D-pad | Arrow keys |
| Return button | Leave desktop mode |

**Sticks report position; a mouse consumes velocity.** Do not emit one `REL_X`
per incoming stick event — that produces a pointer that moves only while the
stick is *changing* and stops dead when it is held at full deflection. Instead,
keep the latest stick position as state and emit motion from a repeating timer
(a steady tick, 60 Hz), scaling the deflection into pixels per tick. Apply a
dead zone, and a non-linear response curve so slow precise movement and fast
travel are both comfortable.

Everything above is a default and is overridable, same as bindings.

### What building it added

- **The tick must be scheduled, not just a read timeout.** The obvious
  implementation — `select()` with a timeout, treat the timeout as the tick —
  starves exactly when it matters: an actively moving stick produces a constant
  event stream, `select` never times out, and the pointer stops updating. Track
  the deadline explicitly and emit a tick whenever it passes, regardless of how
  much input arrived. See `GamepadReader._pump`.
- **Sub-pixel motion needs an accumulator.** At 60 Hz a gentle deflection is a
  fraction of a pixel per tick, every one of those truncates to zero, and the
  pointer never moves however long you hold it. Carry the remainder between
  ticks. The wheel needs this even more, being far coarser than the pointer.
- **Tick only while desktop mode is active.** A permanent 60 Hz wakeup keeps a
  laptop from ever idling, for a feature most users are not currently using.
  `GamepadReader.set_tick` turns it on and off with the mode.
- **Release everything on the way out.** Leaving desktop mode while holding the
  button bound to left-click would otherwise leave the virtual mouse button
  down forever — the session becomes unusable and the cause is invisible.
- **Combinations fire once, on press.** Holding `alt+tab` as a press/release
  pair leaves alt down while the window switcher is open. Single keys still
  follow the button, so holding a d-pad direction repeats normally.
- **A tapped `alt+tab` is the wrong binding for a gamepad**, which is not
  obvious until you use it. Tapping the pair releases alt immediately, so the
  switcher closes at once and you can only ever flip between the two most
  recent windows. What works is holding alt on a button (a single key follows
  the button) and stepping with something else. Hence:
- **Stick directions are bindable**, engaging past `stick_threshold`. Binding a
  direction takes that axis away from its analogue role, per axis rather than
  per stick — so `RS_LEFT`/`RS_RIGHT` can drive the window switcher while
  `RS_UP`/`RS_DOWN` still scroll.
- **A stick flick is armed and disarmed per axis, and re-arming requires the
  stick to return to centre.** A held stick must not repeat, and a threshold
  alone cannot express that, so the axis disarms on firing and re-arms only
  once the stick is observed near centre. `stick_settle` sits on top as a
  minimum time before re-arming.

  > **A caution, recorded because it cost four rounds of wrong fixes.** Most of
  > this machinery was originally built for a theory that measurement later
  > disproved: that a released stick *rings* past centre and crosses the
  > opposite threshold. On real hardware the overshoot was **−0.012** — about
  > fifty times smaller than the 0.6 threshold it supposedly crossed. The
  > arm/disarm model is still correct and worth keeping, but `stick_settle`
  > guards a mechanism that has never actually been observed. Do not add more
  > logic here on the strength of reasoning alone; trace the device first
  > (`decide()` takes an explicit `now`, so a trace can be replayed offline).

- **A stick flick contaminates the other axis, and that axis must be
  suppressed.** This was the real cause of "one flick, two windows". No thumb
  moves a stick in a straight line, so a horizontal flick also deflects the
  vertical axis — which was still driving the scroll wheel. A window switcher
  steps on scroll, so each flick emitted a Tab *and* wheel clicks, and the
  harder the flick the worse it got. Only the **dominant** axis of a stick acts:
  the smaller one is suppressed for both stepping and analogue use. The stick
  keeps both jobs (flick sideways to step, push up/down to scroll) without them
  interfering.

- **Stick clicks (L3/R3) bounce when the stick is moved.** The switch sits
  under the stick, and flicking sideways can momentarily unseat it: a measured
  trace showed a release and re-press **42 ms apart** while the button was
  being held down continuously. For a button bound to a held modifier that
  closes and reopens the window switcher mid-gesture. Releases are therefore
  held back for `button_debounce` and cancelled if the button returns.

- **The disarmed state must be re-evaluated on the tick.** Re-arming depends on
  observing the stick near centre, and a stick at rest emits no events — so an
  axis that quietly returned to centre would stay disarmed for ever and the
  direction would appear dead. `stick_tick_ops` re-checks each bound axis every
  tick using the last known value.
- **Disarm per axis, not per direction.** The rebound crosses the *opposite*
  threshold, so disarming only the direction that fired leaves the other one
  live for precisely the event that needs ignoring.
- **One event can produce several virtual actions.** A stick flicked from one
  extreme straight to the other releases one direction and engages the opposite
  in the same event, and the release must be emitted first or the old key stays
  held. `Decision.virtual` is a tuple for this reason.
- **The dead zone and curve must be radial, applied to the stick vector — not
  per axis.** A per-axis dead zone carves a plus-shaped hole out of the stick's
  range: push nearly-but-not-quite straight right and the small vertical
  component falls inside the dead zone, gets zeroed, and the pointer snaps to
  exactly horizontal. Every direction near an axis collapses onto it and the
  result feels eight-directional. A per-axis curve compounds it, and also makes
  diagonals about 40% slower than cardinals. Take the magnitude once, scale a
  unit direction vector by it, and all 360 degrees work at a uniform speed.
- **Emit the first wheel click of a gesture immediately.** The accumulator that
  makes fine scrolling possible also delays the *start* of every scroll: at ten
  clicks per second a third-deflection push spends over three seconds filling
  it. Users read that as scrolling being broken, not gradual. Fire one click on
  the transition from rest, then accumulate normally. The pointer needs no
  equivalent because hundreds of pixels per second clears one whole pixel per
  tick immediately.

## Unlock module (v0.7)

**Opt-in, off by default, with a blunt security warning.** It requires a
digits-only login password, which is a real security downgrade — the README
must say so plainly rather than burying it.

The design is: watch for the screen locking, and only while locked, let the pad
type digits.

**Lock-state signal — built** (`padmode/lock.py`). The daemon listens for
`ActiveChanged` on `org.freedesktop.ScreenSaver`. There is no D-Bus binding in
the standard library and §2 rules out adding one, so it is reached the way
every other desktop service is: an external command. `gdbus monitor` is one
long-lived process, and its stdout is a pipe the event loop selects on beside
the pad (`GamepadReader.watch`, which yields `WAKE`). Nothing polls.

What the spec guessed, and what Plasma 6.7 actually does:

- **The duplicate is two object paths, not two interfaces.** `ActiveChanged`
  lives on `org.freedesktop.ScreenSaver` at both `/ScreenSaver` and
  `/org/freedesktop/ScreenSaver`; `org.kde.screensaver` carries only
  `AboutToLock`. We listen on `/ScreenSaver` alone, and still act only when
  the state *changes*, so a desktop that does emit twice costs nothing.
- **`GetActive` is asked when the name gains an owner**, not blindly at
  startup. `gdbus monitor` reports the owner only after its subscription is
  in place, so no lock can slip between the question and the first signal —
  and a daemon that starts before the compositor does still learns the state
  the moment it appears. The call takes ~3 ms.
- **`--dest` filters on the sender.** Another client on the session bus cannot
  fake a lock by emitting the signal itself — which matters, because "locked"
  is what will turn the digit keyboard on.
- **Unknown means unlocked.** No `gdbus`, no owner, an unanswered question, or
  the monitor dying: each is said once in the log, and the state is the one in
  which nothing extra is live. The watcher dying while locked reports an
  unlock, so whatever was built for the lock screen gets torn down.

The watcher is not even constructed unless `[unlock] enabled = true`, so an
install that never opts in never has a `gdbus` process.

*Verified live* (Plasma 6.7 Wayland, 2026-10-21): Meta+L, then unlocking,
logged exactly one `screen locked` and one `screen unlocked`, 3.8 s apart —
the duplicate on the second path never reached us. Not yet tried: a daemon
started while already locked, and any desktop other than KDE.

**Not the on-screen keyboard.** Tried at the KDE lock screen: it types there
but is never drawn (`docs/on-screen-keyboard.md` §At the lock screen), so the
module stays its own thing, as specified. The same test showed uinput input
reaching the greeter, which is what this design rests on.

**Only bind digits while locked.** The virtual keyboard must be created on lock
and destroyed on unlock, never left live during normal use, so a stray press
can never type into a focused application.

**Digit input — built** (`padmode/unlock.py`, `Mode.UNLOCK`). Decided with the
maintainer on 2026-10-21, after the lock-screen test showed nothing we draw is
visible there: the input is laid out to be learnt by feel, because the only
feedback is the count of dots.

| Input | Default |
|---|---|
| Left stick, 8 sectors of 45°, from the top clockwise | `1` `2` `3` `4` `5` `6` `7` `8` (↑ ↗ → ↘ ↓ ↙ ← ↖) |
| X / Y | `9` / `0` |
| START | Enter |
| SELECT | delete one |
| B | delete everything the pad typed |

Every entry is a default in `[unlock.bindings]`: any button, d-pad direction
or sector of either stick (`LS_UP_RIGHT`, `RS_DOWN` …) can take any digit or
action, and `false` unbinds. The maintainer's reference point was the Switch,
digits on the stick and the rest on face buttons.

How it behaves, and why:

- **A sector types on the way home, not on the way out.** Push, feel where
  you are, correct, let go. The digit is the last sector the stick was in
  above `stick_threshold` (0.6), typed when it falls to half of that.
- **The stick is read per frame (`SYN_REPORT`), never per axis.** The two axes
  of one movement are two events, and between them the stick is somewhere it
  never was: coming home from ↗, X lands first and for one event it reads as
  straight up. Per-axis reading typed 1 for 2; the tests caught it.
- **The spring-back is ignored.** For `desktop.stick_settle` after a digit, a
  push towards the three sectors facing it is taken for the rebound. The same
  sector is never blocked, so `11` is as quick as `12`.
- **Clear deletes what the pad typed**, counted, and no more: what someone
  typed on a real keyboard is invisible to us.
- **Entered and left only by the lock watcher** (`lock_changed`), never by a
  button. Locking takes the pad from whatever had it — desktop mode is left
  the way its exit button leaves it, the keyboard closed — grabs it so a game
  behind the lock never sees the password dialled, and opens the keypad.
  Unlocking closes the keypad and lands in IDLE, not back where it was.
  Guide does nothing here: there is nothing behind a lock screen for it to
  control.
- **The keypad is its own uinput device** advertising `KEY_0`–`KEY_9`,
  Enter and Backspace and nothing else — so not even a bug upstream can make
  it type a shortcut. Top-row digits, not the keypad's: those depend on Num
  Lock. Creating it costs ~100 ms, paid at the moment of locking.
- If the keypad cannot be created, the pad is handed straight back.

*Verified live* (Plasma 6.7 Wayland, 2026-10-21, the maintainer with the pad):
every default typed what it should — three stick sectors, X and Y, one delete,
clear — Guide did nothing, and on a second lock the pad alone unlocked the
machine. Not yet tried: a daemon started while already locked, the pad
reconnecting mid-lock (the grab is lost on reconnect; digits still type), any
desktop other than KDE.
