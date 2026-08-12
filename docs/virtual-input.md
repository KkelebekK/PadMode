# Virtual input device (v0.2, v0.5)

Reference for `padmode/input/virtual.py` and the modes built on it.

**Desktop mode (v0.2) is implemented.** The section below documents what
shipped. The unlock module (v0.5) is still a specification, written down
because it was learned by building a working version once.

We create our own uinput device with `python-evdev` rather than depending on
input-remapper (CLAUDE.md §3.2). Doing so removes a heavy dependency *and*
deletes "now hand-build two presets in a GUI" from the install instructions.

---

## The fact everything else rests on

**A uinput virtual keyboard is indistinguishable from a real USB keyboard** to
everything above the kernel — compositors, toolkits, and lock screens included.
There is no "synthetic input" flag to detect. This is why unlock (v0.5) is
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

## Unlock module (v0.5)

**Opt-in, off by default, with a blunt security warning.** It requires a
digits-only login password, which is a real security downgrade — the README
must say so plainly rather than burying it.

The design is: watch for the screen locking, and only while locked, let the pad
type digits.

**Lock-state signal.** Subscribe to the `ActiveChanged` D-Bus signal on
`org.freedesktop.ScreenSaver`. Three things this has to get right:

- **KDE emits the signal on two interfaces** (`org.freedesktop.ScreenSaver` and
  `org.kde.screensaver`), so the same lock event arrives twice. De-duplicate by
  acting only when the state actually *changes*, not on every signal.
- **Query `GetActive` at startup.** Otherwise a daemon that starts while the
  screen is already locked — which is exactly what happens on a boot that
  autologins and then locks — never learns it is locked and the pad does
  nothing.
- The screensaver interface is the least portable piece in the project. Other
  desktops expose different names, and some expose none. Degrade gracefully:
  if the signal is unavailable, disable the module and log once.

**Only bind digits while locked.** The virtual keyboard must be created on lock
and destroyed on unlock, never left live during normal use, so a stray press
can never type into a focused application.
