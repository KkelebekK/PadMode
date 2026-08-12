# Virtual input device (v0.2, v0.5)

Reference for `padmode/input/virtual.py` and the modes built on it. Not yet
implemented; written down because it was learned by building a working version
once.

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
(a steady tick, e.g. 60 Hz), scaling the deflection into pixels per tick. Apply
a dead zone, and a non-linear response curve so slow precise movement and fast
travel are both comfortable.

Everything above is a default and must be overridable, same as bindings.

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
