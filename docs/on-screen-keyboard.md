# On-screen keyboard (v0.5)

Type without a physical keyboard: a grid of keys drawn on screen, navigated
with the pad, one button to commit a character. The thing that makes a couch
session self-sufficient — a desktop you can drive but not type into still
sends you looking for a keyboard.

Modelled on the Xbox and PlayStation keyboards.

**Status: done, and the manual checklist is complete.** Driven from a real
gamepad on Plasma 6.7: the keyboard opens, types into an application (letters,
shift, the symbol layer and latching Ctrl/Alt/Super), spans the output it lands
on, follows the active monitor across a two-output session with mixed scaling,
draws over a fullscreen game, and survives having its renderer killed without
stranding the pad. Round-trip latency is about a millisecond plus our own tick.
61 tests cover the grid, protocol, config and routing.

What is *not* established is breadth: one game, one compositor, one pad. The
open items are listed in §Still unmeasured.

---

## Decisions

| Question | Decision |
|---|---|
| What draws it | A **separate process in this repo**, launched by the daemon |
| Dependencies | The GUI toolkit is **optional and probed at runtime**, exactly like `playerctl` and `kdotool`. The daemon's own dependencies stay stdlib + `python-evdev` |
| Grid and selection | Owned by the **renderer** |
| Typing | The **daemon** types on the virtual keyboard it already creates (v0.2) |
| Layout | A **60% keyboard**: staggered widths, Tab/Caps/Shift/Enter where hands expect them, modifiers along the bottom |
| Scope | Letters, digits, a symbol layer, shift, caps, space, backspace, enter, tab, cursor keys — **plus latching Ctrl/Alt/Super**, which consoles have no use for and a desktop cannot do without |
| Toolkit | **GTK4 + `gtk4-layer-shell`** via PyGObject |
| Session support | **Wayland only.** X11 disables the keyboard with one line |
| Is it a mode? | **No** — a layer over a pad-driven mode |
| How it opens | The value `osk` in `[desktop.bindings]` (default `Y`) or `[bindings]` |
| Renderer lifecycle | **Pre-warmed** on entering a pad mode, unmapped until needed |
| Pointer while up | **Parked.** The left stick drives the grid instead — the shortcut map had already taken every mouse button |
| Auto-open | **On by default**, needing desktop accessibility switched on. Opens only just after a click of ours; closes only if it opened itself |

The first two follow the metadata generators (CLAUDE.md §2): same repo,
separate process, never imported by the daemon. Nothing that draws pixels
belongs in the process that owns the gamepad, and a missing toolkit must
disable one feature rather than stop the daemon starting.

## Who owns what, and the price of it

The renderer owns the grid, the layout and the highlight. The daemon forwards
input and does the typing.

This was chosen over the daemon owning selection, with the trade-offs
understood rather than discovered later:

- **The layout lives in exactly one place**, so there is no way for the
  highlight and the typed character to disagree — the worst bug this feature
  could have.
- **The highlight responds locally**, so movement and press feedback are not
  hostage to another process's scheduling.
- **Cost: every keypress is a round trip.** The renderer knows which character
  was chosen, so it must send it back for the daemon to type. If the renderer
  stalls, typing stalls. Acceptable because the renderer does almost nothing
  per event, but it is a real coupling and should be one of the first things
  traced when the feature misbehaves.
- **Cost: input state spans two processes**, which CLAUDE.md §4 warns produces
  "confusing, self-consistent nonsense" when it goes wrong. Mitigated below,
  and the mitigation is not optional.

### What keeps that safe: the renderer never sees an analogue value

The renderer is only ever sent **discrete steps**. That was true when the d-pad
was the only thing that navigated, and it stayed true when the left stick was
added.

This matters more than it looks. Every expensive lesson in this project's
input handling — spring-back overshoot crossing the opposite threshold,
`stick_dominance`, `stick_settle`, `button_debounce` — is about turning noisy
analogue deflection into discrete intent. A d-pad emits discrete hat events
already, so it needed none of it; a stick needs all of it.

So when the stick was added, the digestion went in the **daemon**, which
already had that logic and its tests. The extraction was mechanical: the
"which direction just fired" decision came out of `_stick_direction_ops` into a
helper that both callers share, with a predicate for "does this direction mean
anything here" so an axis still only disarms itself for a direction that
actually fires. The renderer received the same `step` messages it already
understood and needed no changes at all — and the keyboard inherited the
overshoot handling for free rather than growing its own.

Do not re-derive dead zones in the renderer.

**No debounce on the keyboard's buttons**, deliberately. The releases held back
in desktop mode exist because a stick *click* bounces when the stick is moved;
these are face buttons, and no such bounce has been traced on them. Inventing a
filter for it would be the exact mistake CLAUDE.md §4 was written about. If a
character ever types twice, trace it before changing anything.

## The layout

A 60% keyboard, with four deliberate departures: the symbol-layer toggle takes
Escape's place, the number row carries no shifted legends, there is no `/`, and
the cursor keys fill the space to the right of the space bar.

| Row | Letters | Symbols |
|---|---|---|
| 4 | `?#` `1`…`0` `⌫` | `?#` `!@#$%^&*()` `⌫` |
| 3 | `tab` `q`…`p` `\` | `tab` `-_=+[]{};:` `\|` |
| 2 | `caps` `a`…`l` `↵` | `caps` `' " ` ~ < >` `↵` |
| 1 | `⇧` `z`…`m` `,` `.` `⇧` | `⇧` `, . / ?` `⇧` |
| 0 | `ctrl` `win` `alt` `space` `←` `→` | *identical* |

**Every row is exactly 13U on both layers**, and that is load bearing twice
over. The rows line up, and toggling layers cannot change the surface height —
which matters because a mapped layer surface only resizes reliably when it
*grows* (§Sizing, item 5). A test asserts it rather than trusting it.

Only the character keys differ between layers. Every action key is shared by
identity, so nothing a thumb is resting on moves when you toggle: Backspace
does not wander off mid-word. `|` sits where `\` does — the same physical key,
its shifted meaning — and `,` and `.` are repeated on the symbol layer because
going back a layer for a decimal point would be absurd. Between them the two
layers cover all 32 ASCII punctuation characters, and a test checks that every
character on either layer is one the virtual keyboard can actually send.

Right Shift is 1.75U rather than a real 60%'s 2.75U: keeping `.` costs a unit,
and the shift reached for least is the cheapest place to find it.

### Navigating a grid that is not a lattice

Keys range from 1U to 6.25U, so moving by column index stops meaning anything
— the fourth key of the bottom row is the space bar, and the fourth key of the
row above is nowhere near it. Vertical movement instead goes by **position**:
each row's key spans are precomputed in units, and the keyboard remembers a
horizontal coordinate the way a text editor remembers a desired column. Left
and right reset it to the new key's centre; up and down keep it and select
whichever key covers it.

The visible result is that a run of `up` presses travels in a straight line —
from the middle of the space bar you get `b`, `h`, `y` — and that going down
past a row of wide keys and back returns you where you started instead of
dragging the highlight sideways.

### The stick is a sector here, not a flick

Everywhere else in padmode a stick *direction* is a bound button: it taps once
when it crosses the threshold, then disarms until the stick is seen back near
centre. That rule is not fussiness — a stick released from full deflection does
not glide home, it rings, crossing the far threshold hard enough to tap once
the wrong way — and the keyboard inherited it whole, on the reasoning that the
grid should get exactly the digestion a bound direction gets.

Rolling the stick in a slow circle is what showed it was the wrong model. Your
thumb never passes through centre, so the axis that just fired stays disarmed
through the next quarter turn. Measured against the old code, driving a perfect
circle at 60 Hz:

| Roll | Old (flick) | New (sector) |
|---|---|---|
| 0.5 turns/s | 11 steps per turn | 11 steps per turn |
| 1 turn/s | 4 steps per turn | 4 steps per turn |
| 2 turns/s | **a direction dropped every turn** | 4 steps per turn |

So the grid asks a different question, every tick and on every stick event:
*which way is the stick pointing*, the way a d-pad is asked which way it is
held. A changed answer steps the highlight; an unchanged one repeats on the
hold clock. The spring-back block survives, measured from the moment the stick
comes home rather than from the moment it fired, plus one rule that says a
thumb cannot cross centre without passing through it — a direction reversing in
a single sample is the ring, not a push.

The eleven steps per turn at half a turn a second are not a bug: a slow roll
sits in each sector for 500 ms, and hold-to-travel starts at 350 ms. Holding a
direction is supposed to travel.

`osk.stick_dominance` is 1.0 where `desktop.stick_dominance` is 2.0, and the
difference is the same lesson from the other end. A margin is worth paying for
when the stick chooses between two *different jobs* — stepping or scrolling —
because doing nothing in the ambiguous wedge beats confidently doing the wrong
one. On the grid the stick has one job, so at 2.0 each direction gets a 53°
cone and 147° of the circle steps nowhere. That was the first thing reported,
before the skipping: directions that do not answer.

### Badges

Each key carries the pad button that operates it, the way a console keyboard
does: Xbox colours for A/B/X/Y, plain grey for the shoulders and stick clicks.
They are read from the daemon's action → button hints rather than baked into
the layout, so rebinding a button moves its badge; a badge that lied would be
worse than none.

`commit` and `close` are the only two shortcuts with no key of their own —
commit acts on whatever is highlighted, close makes the whole thing vanish —
so they get a two-item line along the bottom. That line replaced a six-item
legend that duplicated what the badges now say.

## A layer, not a mode

`osk` is a flag on the state (`State.osk`), valid only while `mode` is in
`PAD_MODES`. It is not a fifth `Mode`, and the concurrency the original spec
worried about never had to be expressed in the state machine.

While the keyboard is up:

- the **d-pad** steps the highlight instead of emitting arrow keys;
- the **left stick does too**, digested by the daemon into the same discrete
  steps, and no longer drives the pointer;
- the buttons in `[osk.bindings]` do their keyboard job;
- **every other button keeps its desktop binding** — `SELECT` is still Esc;
- the **right stick still scrolls**, and its bound directions still Tab;
- the **exit button still leaves the mode**, unconditionally. It is the one
  escape that must never need thinking about.

`A` commits even though it is left-click in desktop mode. Committing a
character is by far the most frequent thing you do while typing, so it gets the
most natural button, and `B` puts the keyboard away in one press when you want
to click something.

**The pointer is parked while the keyboard is up, and that is a correction
rather than a loss.** This document used to claim the live pointer was "the
whole point" of the layer design. That stopped being true when the v0.5
shortcut map took `A`, `B` and `X` — every mouse button — leaving a cursor you
could move but not click with. So the left stick was given to the grid instead,
where it is useful. What still makes this a layer and not a mode: every button
the keyboard does not claim keeps its desktop job, the right stick still
scrolls, and the exit button still works.

Opening `osk` from the modifier tier (`[bindings]`) **enters desktop mode with
the keyboard already up** rather than inventing a keyboard-only state. One code
path owns the grab, and you get a pointer you were going to want anyway.

## Typing path

Renderer sends `{"t":"type","text":"q"}`; the daemon maps it with
`buttons.keys_for_char` and taps it on the virtual keyboard from v0.2. No new
device, no new permissions, and the renderer never touches `/dev/uinput` —
which also keeps `reader.VIRTUAL_PREFIX` doing its one job of stopping us
reading back our own output.

Typing through uinput rather than the Wayland input-method protocol means the
keystrokes are indistinguishable from real hardware, so this works in every
application, in password fields, and on X11 and Wayland alike. It is also
already built and already tested.

**It also assumes a US layout, and cannot do otherwise.** Kernel key codes name
keys by *position*; the character that arrives is whatever the compositor's
active xkb layout maps that position to. On any other layout the key the
keyboard draws and the character the application receives will disagree, at
least for punctuation. This is the price of typing through uinput rather than
an input method — the same price that buys password fields and every toolkit.
It is not a bug to be fixed by adjusting the grid; fixing it properly means
reading the active layout, which nothing here does.

### Latching modifiers

Ctrl, Alt and Super sit along the bottom row and **latch**: press once to hold,
press again to release. This is the one deliberate departure from console
parity — an Xbox keyboard has no Ctrl because there is nothing to press it
against, and a desktop is nothing but things to press it against.

They are **genuinely held down on the virtual keyboard**, not remembered and
prepended to the next keystroke. That distinction is the whole feature:

- a global shortcut sees a real modifier, so `Super+A` opens a launcher;
- `Ctrl` stays down across a *click*, not just a keystroke, because the
  pointer is still live while the keyboard is up;
- `Alt` can be held across several keystrokes, which is what stepping a window
  switcher needs.

None of that works if the modifier is a note-to-self applied to one commit.

Shift is deliberately *not* one of these: it stays one-shot-then-lock, because
capitalising a single letter is the common case and a latch would cost two
extra presses every time.

**The release paths matter more than the presses.** A latch that outlived the
keyboard would leave the desktop with Super held and no clue why. So it is
released when the keyboard hides, when it closes, when the mode ends, and — as
a backstop in the daemon rather than the renderer — if the renderer dies while
holding one. The daemon tracks exactly which codes *it* latched, so releasing
them cannot disturb a key some other binding is holding.

Verified twice: first without a gamepad, by feeding synthetic pad events into
a real daemon and watching the virtual keyboard device (`LEFTMETA down, A down,
A up, LEFTMETA up`, and a Vicinae window appearing where there was none), then
from a real pad on Plasma 6.7 — including that the latch is genuinely cleared
afterwards, which is the half that would otherwise turn every later keystroke
into a shortcut.

One consequence worth knowing rather than fixing: latching Super and unlatching
it without pressing anything in between opens KDE's application launcher. We
send a genuine press and release, and a hardware keyboard does exactly the same.

### The protocol

Line-delimited JSON over the child's stdin and stdout, defined in
`padmode/osk/protocol.py`. A socket was the alternative; pipes won because
**liveness comes free** — the renderer dies, the daemon's read returns EOF, and
OSK mode ends without a watchdog. Given a stuck grab is the worst failure this
app has, the failure path wanting the least machinery wins.

The renderer's stderr is inherited, so GTK's warnings land in journald with
everything else and stdout stays a clean channel.

> **Read the pipe raw.** The daemon must not use a buffered reader plus
> `select`. A buffered `readline` pulls several messages into Python's buffer
> at once, after which `select` reports the fd as not ready while a complete
> message is already sitting there — silently losing keystrokes. This bit the
> driver script during development and is pinned by a test.

## The focus problem

A keyboard that takes focus types into itself. This is the central technical
problem, and the reason this feature is a compositor question at all.

`zwlr_layer_shell_v1` is the mechanism: a surface on the overlay layer with
`keyboard_interactivity` set to `none` is drawn above everything and never
takes keyboard focus. **Measured on Plasma 6.7: it does not move focus.** The
active window was identical before, during and after the surface was up, on
both probe runs.

| Session | Approach | Status |
|---|---|---|
| KDE Wayland | `zwlr_layer_shell_v1` | **Working.** Compositor advertises v5; gtk4-layer-shell binds v4 |
| X11 | — | **Not supported in v0.5.** Different mechanism (override-redirect), not written |
| GNOME Wayland | — | **Not possible.** GNOME refuses to implement layer-shell |
| wlroots (sway, Hyprland) | `zwlr_layer_shell_v1` | Should work; community territory, untested |

GNOME Wayland loses this for the same reason it loses overlays — the
compositor declines to expose the capability — but it is a *different*
protocol, so the two limitations should be described separately rather than
lumped together as "GNOME is unsupported". Note also that the keyboard is
narrower than overlays: overlays work on X11 and the keyboard does not.

### Toolkit: measured, and it inverted the original guess

The spec assumed GTK because `gtk-layer-shell` has GObject-introspection
bindings and Qt's `LayerShellQt` is C++ with none. On this machine the
availability argument runs the other way — `layer-shell-qt` is installed as a
Plasma dependency, including the `QT_WAYLAND_SHELL_INTEGRATION=layer-shell`
plugin, while `gtk4-layer-shell` had to be installed.

The conclusion survives anyway, for a sharper reason. `LayerShellQt::Window`
exposes `layer`, `anchors` and `keyboardInteractivity` as properties reachable
only through `LayerShellQt::Window::get(QWindow*)` — C++, no bindings. The
env-var plugin yields a layer surface with **whatever the defaults are and no
Python-reachable way to change them**, and `keyboardInteractivity` is precisely
the setting the feature hinges on. So: GTK4.

**The cost the spec did not anticipate: `gtk4-layer-shell` is a package every
user must install.** That is within the "optional runtime dependency probed
like playerctl" model, but it is a real install step and the README says so.

### `LD_PRELOAD` is mandatory

Measured, and it would have been an ugly surprise at integration time:

```
LD_PRELOAD=<none>                            is_supported() = False
LD_PRELOAD=/usr/lib/liblayer-shell-preload.so  is_supported() = False
LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so     is_supported() = True
```

gtk4-layer-shell cannot hook GDK when PyGObject `dlopen`s it after the fact, so
the library has to be preloaded. Note that the *dedicated* preload shim shipped
for this purpose does **not** work here — the main library does. The daemon
sets this when it spawns the renderer; `padmode.osk.preload_library` finds the
path via `ctypes.util.find_library`, and `osk.preload` overrides it.

Without it there is no error, just a window that is not a layer surface. Since
that would draw an ordinary focus-stealing window across the screen, the
renderer checks `is_supported()` and reports `failed` rather than drawing.

## Risks, as measured

**1. Does our own surface show up as a window? Yes — and it does not matter.**

While the surface is up, `kdotool search --class .` returns one more window
than before, and `getwindowclassname` reports it as **`python3.14`** — the
interpreter binary. `PR_SET_NAME` does not change that, and kdotool 0.2.3 has
no way to filter by pid, so **we cannot rename or exclude our own surface.**

What makes it safe is that it is **never the active window**. The dangerous
path in CLAUDE.md §6.6 — a dead window id recorded as an overlay's return
target, producing a return that can never succeed — goes through
`get_active_window`, which can never return our surface. Teardown is clean too:
the id disappears when the surface is hidden, with none left behind.

The residual exposure is `find_window(regex)` matching `python3.14`, which
needs a user whose overlay or frontend class regex matches the interpreter
name. The daemon closes even that by never leaving the surface mapped across a
window operation: `_leave` stops the renderer before `_schedule_return`, and
overlay entry pre-warms only after its window work is done. Costs 0.6 ms to put
the keyboard back.

**2. Over a fullscreen game. Measured: it works.** Tried against *The Farmer
Was Replaced* running fullscreen — the keyboard drew over it and typing reached
the game. This is gotcha §6.2 again: KWin drops direct scanout as soon as
something has to be composited on top, and a layer surface on the overlay layer
forces that just as an overlay window does.

One game is one game, not a rule. §6.2 was written wrong the first time by
generalising from a single setup, so treat this as "works on the game it was
tried on" and record others as they are tried — particularly anything running
under its own gamescope session, which bypasses the compositor entirely.

**3. Round-trip latency per keypress. Measured: ~1 ms, plus our own tick.**
128 samples from a real typing session: min **1.0 ms**, median 10.2 ms, p90
16.1 ms, max 17.3 ms.

The spread is not the renderer. Replies are collected on the daemon's 60 Hz
tick, so a reply lands uniformly somewhere in a 16.7 ms window — which is
exactly what a 1.0 ms floor and a 17.3 ms ceiling describe. **The ownership
split costs about a millisecond**; the rest is padmode polling the pipe on the
tick rather than in the read loop's selector.

Imperceptible in use, so it is not worth changing today. But if the keyboard
ever needs to feel snappier, the fix is to select on the renderer's pipe
alongside the gamepad rather than waiting for the tick — the renderer is not
the slow part and tuning it would be aiming at the wrong thing.

**4. What happens when the renderer dies.** Handled and tested: EOF ends OSK
mode, the daemon logs once and carries on. The grab belongs to the *mode*, not
to the keyboard, so desktop mode continues with a working pointer and the pad
is never stranded.

## Sizing, and four things that only a compositor could teach us

The keyboard is sized as a share of the screen's **height** (`osk.height`,
percent); the width follows from the key proportions. Height is the dimension
worth naming — it is how much screen you lose, and it means the same thing on a
laptop panel and a television. It is approximate: getting it exact would cost a
second layout pass for no benefit.

Everything below was found by measuring on real hardware, and none of it was
guessable from the documentation:

1. **Never call `set_default_size` on a layer-shell window.** Anchoring to both
   side edges is what makes the surface span the output — but *any* call to
   `set_default_size`, including `set_default_size(-1, -1)`, pins it to the
   content's natural width instead. Measured: 460x176 with the call, 1536x176
   without it, everything else identical. This is why the keyboard first
   appeared as a small box in the bottom-left corner.

2. **The output size cannot be known before mapping.** A layer surface reports
   width 0 until the compositor's configure arrives, and
   `Gdk.Display.get_monitor_at_surface` returns **None** for one. So the size
   is learned by mapping once during pre-warm with all four edges anchored —
   which makes the surface report the output's usable area — with the content
   at zero opacity and no input region, then unmapping. The user's first show
   is then already correct, and the GTK renderer is fully warm as a bonus.

3. **`set_visible(True)` on an already-visible window emits no `map`.** The
   re-measure triggered by moving output relied on a fresh `map` to reconnect
   its frame handler, and never got one — leaving a full-screen surface with
   its content at zero opacity. A keyboard you could type on but not see.
   Measured as 1536x830, exactly the usable area of that output. The frame
   handler now lives for the surface's lifetime instead.

4. **Anchoring the fourth edge on a mapped surface does not resize it that
   frame.** Reading the size immediately measured the keyboard's own height and
   scaled everything from it, which is how it came out far too small after
   moving screens. The pass now waits for the size to actually change, with a
   bounded frame budget — measuring forever means an invisible keyboard, which
   is much worse than a badly sized one.

5. **Resizing a mapped layer surface only propagates when it grows.** Coming
   down from a larger output left the panel at the old height with dead space
   under the keys; going up looked fine. So the surface is always unmapped,
   rescaled, and mapped again — the same path the pre-warm pass uses, and it
   costs 0.6 ms.

**Since the rewrite, the surface *is* the keyboard.** It is no longer stretched
across the output: it is anchored to the top-left corner at exactly the size the
keys measure, and a pair of layer-shell margins says where that corner is. That
is what lets the right stick slide the panel out from under whatever you are
typing into — the corner arithmetic lives in `osk/model.py`, where it can be
tested without a compositor. Anchoring all four edges is now only how the
*measuring* pass asks the compositor how big the output is.

It costs one thing and buys another. A panel-sized surface cannot tell which
output it is on from its own width — which is how the full-width version
noticed, in point 4 above. So the measuring pass runs before every show rather
than once at startup: measured on Plasma 6.7, 5 ms and a single frame, and it
turns "moved to another screen mid-session" from a case handled by comparing
widths into a case that cannot arise.

The renderer logs one line per measure, rescale and placement to stderr, which
the daemon inherits and journald collects. Sizing depends on compositor timing
that no test can reproduce, so the decisions are traceable rather than merely
inferrable from what ends up on screen.

**The keyboard does not take mouse input.** Its input region is empty, so
clicks pass through to whatever is underneath. Without that it would swallow
pointer events across everything it covers, costing you the bottom third of the
screen — the opposite of the point of it being a layer rather than a mode.

## Performance, and why the renderer is pre-warmed

All measured on this machine, GTK 4.22, a 44-key grid:

| | cost |
|---|---|
| `import gi` + GTK4 | ~81 ms (162 ms cold cache) |
| `Gtk.init_check()` | 0.0 ms |
| Build the grid | ~10 ms |
| RSS, window built but never mapped | ~68 MB |
| **First show → first frame** | **~105 ms** |
| **Every show after that** | **~0.6 ms** |
| RSS after 20 show/hide cycles | flat, no leak |

`Gtk.init_check()` costing nothing is the informative one: GTK4 does no real
work until a surface is mapped, so all the cost lands on the first show.

Renderer comparison, same grid:

| `GSK_RENDERER` | first frame | steady RSS |
|---|---|---|
| vulkan (default) | 104 ms | 224 MB |
| ngl | 57 ms | 263 MB |
| gl | 142 ms | 296 MB |
| **cairo** (ours) | **7 ms** | **85 MB** |

For a grid that never animates, the software renderer is 14× faster to first
frame and 2.6× lighter, costing ~2 ms per re-show that nobody can perceive.
**Caveat with an expiry date:** GTK's cairo renderer is a fallback path
upstream has discussed removing. If it goes, we fall back to `ngl` and pay the
memory. `GSK_RENDERER` is set with `setdefault`, so it can be overridden.

The renderer is therefore **pre-warmed on entering a pad-driven mode** and left
unmapped: 68 MB idle, one ~7 ms first show per session, instant after that. The
structural reason matters more than the milliseconds — auto-open (below) needs
a process running while the keyboard is *closed*, and it cannot live in the
daemon without dragging `gi` in against CLAUDE.md §2.

## Auto-open, as built

Click into a text field and the keyboard appears; when that field goes away it
closes again — but only if it opened itself. One you asked for stays until you
dismiss it, because a keyboard vanishing under your hands is worse than a
button press.

`osk.auto_open`, on by default. One knob: closing is part of opening, not a
separate switch.

### It needs accessibility switched on, and says so

**Measured, and this is the first thing to check when nothing happens.** On a
stock KDE install `org.a11y.Status IsEnabled` is **false**, and with it false
AT-SPI lists six system helpers, none of which have windows, and **no focus
event ever arrives no matter what you click**. Turning it on:

```
gsettings set org.gnome.desktop.interface toolkit-accessibility true
```

took the same desktop from 6 applications to 17, and Qt/QML apps immediately
exposed their text fields — vicinae's search box, plasmashell's, and the lock
screen's password field. Long-running Qt processes picked it up without
restarting.

Because the failure mode is *silence*, `focus.unavailable_reason()` separates
"the Atspi typelib is missing" from "accessibility is switched off" and prints
the command. An hour went into diagnosing this by hand; nobody should have to
do it twice.

Coverage is per-application, and Electron is the known gap: Chromium exposes
nothing without `--force-renderer-accessibility`. The Claude desktop app is a
live example — it takes `--enable-wayland-ime --wayland-text-input-version=3`
and would work under the input-method route we rejected, and never under this
one.

### Opening: the click is what makes it safe

The trigger is deliberately narrow — an editable field taking focus **within
1.5 s of a click padmode itself synthesised**. Not any focus change: an
application focusing its own search box at startup would otherwise open the
keyboard, and since the keyboard parks the pointer, a wrong opening costs the
user their cursor until they press `B`.

Two mechanisms, because one is not enough:

- **A subscription** to `object:state-changed:focused`, which is exact — the
  event names the object that gained focus.
- **A poll**, 150 ms after each click, because AT-SPI reports focus *changes*
  and clicking a field that already has focus changes nothing. That is the
  commonest case of all: click a field, close the keyboard, click it again.

The poll must not stop at the first node claiming focus. **Measured on Kate:
twenty nodes report `FOCUSED` simultaneously** — status-bar buttons, page tabs,
list items inside closed dropdowns — and a depth-first search returning the
first hit landed on a filter widget's list item and concluded you could not
type, while the editor sat two levels up plainly marked `text`/editable. So
every focused node is considered, and `SHOWING` weeds out the ones parked in
hidden popups.

### Closing: "focus left" is not a signal that exists

The first attempt waited to be told that focus had left a text field. It never
was. **Five focus reports in a real session, every one `editable=True`** — not
one negative ever arrived, because clicking a blank area does not move focus
at all, and a window vanishing sends nothing.

Asking "is anything typeable focused right now" does not work either: the panel
and the desktop have editable fields of their own, so the answer is yes
forever.

So the watcher **remembers the specific object** that caused the opening and
checks twice a second whether it is still focused, still `SHOWING`, and not
`DEFUNCT`. A dead widget is a definite signal, unlike the absence of an event,
which is indistinguishable from nothing happening. That is what makes the
common case work: type in a launcher, hit Enter, the launcher exits — and the
keyboard goes with it.

It deliberately does **not** close when you click a blank area of the same
window. Focus really is still in the field there; typing would still land in
it, so closing would be wrong.

### The bug this combination created

Auto-open lands *between* the press and the release of the very click that
triggered it. The keyboard claims `A`, so the release arrived as a keyboard
button and was swallowed — leaving `BTN_LEFT` held down across the entire
desktop, unclearable even with a physical mouse, until desktop mode ended.

Opening now releases whatever the pad is holding first, exactly as `_leave`
already did on the way out, and a test pins it. Worth remembering as a shape
rather than an incident: **anything that changes which layer owns a button must
account for buttons that are held at that instant.**

### Privacy

The watcher reads roles and states, never content. It can see that a password
field has focus — the lock screen proved that — and must never try to see more.
That the lock screen is visible to it at all suggested the unlock module
(v0.7) might be "the keyboard, at the lock screen". It is not; see below.

## At the lock screen

*Tried* (Plasma 6.7 Wayland, 2026-10-21, pad in hand): it **types but is not
drawn**. Desktop mode works at the KDE lock screen -- the pointer moves -- and
pressing `Y` opens the keyboard as far as padmode can tell: the renderer
measured and placed the panel, and round trips came back in 4-17 ms. But
nothing appeared, while `A` still committed the invisible highlighted key into
the password field. KWin hides ordinary clients' layer surfaces while locked.

So the unlock module (v0.7) is **not** "the OSK, at the lock screen": a
keyboard you cannot see is no keyboard. What the test did establish is the
half the module needs -- uinput reaches the greeter, pointer and keys alike.

## Still unmeasured

Everything here is honest about not having happened yet:

- **Any game other than the one tried** (risk 2), especially a gamescope
  session.
- **Any compositor other than KWin.**
- **Auto-open beyond Qt apps.** Verified on Kate and Vicinae. GTK4 has
  AT-SPI built in and Firefox activates its own, but neither has been
  tried. Electron is a known negative: no accessibility tree without
  `--force-renderer-accessibility`.

## Manual checklist

CI has no compositor, so these are done by hand and the results written down
here. CLAUDE.md §5: do not pretend these are automated.

- [x] **It appears at all.** `padmode run` from the repo, Guide + L3, press Y.
      A keyboard along the bottom of the screen. *Verified.*
- [x] **It does not take focus.** *Verified* — the active window id was
      identical before, during and after, and typing lands in the app that had
      focus.
- [x] **It types.** *Verified* into Kate: letters in order, once each.
- [x] **Shift.** *Verified* — capitals arrived (`QazxcXZZASD`).
- [x] **The symbol layer** types what it draws on a US layout. *Verified* —
      `!@#$%^^++_-)({}\\<>` all arrived correctly.
- [x] **It spans the output** it lands on, at the configured share of screen
      height. *Verified* on both a 1536x830 and a 2560x1406 output.
- [x] **The left stick drives the grid** while the keyboard is up, and the
      pointer stays put. *Verified*, including that a hard flick steps exactly
      one key — the spring-back overshoot rule is inherited from desktop mode
      rather than reimplemented.
- [x] **`SELECT` is still Escape** while the keyboard is up, and `A` is not a
      left-click until you close it with `B`. *Verified* — only the d-pad and
      the buttons in `[osk.bindings]` are taken.
- [x] **The exit button leaves desktop mode** with the keyboard up, in one
      press, and the keyboard goes with it. *Verified*.
- [x] **Nothing is left on screen** after leaving. *Verified* — no surface in
      `kdotool search --class .`, and no renderer process.
- [x] **Move between outputs mid-session**, in both directions, repeatedly.
      *Verified.* This one item found three separate bugs — all of §Sizing
      items 3, 4 and 5 — and none of them had a CI test available, because the
      renderer needs a compositor. Re-run it after any change to the sizing
      code, in **both** directions: growing and shrinking took different paths
      and only one of them worked.
- [x] **Over a fullscreen game** (risk 2). *Verified* against *The Farmer Was
      Replaced*, fullscreen: the keyboard drew over it and typing reached the
      game. One game is not a rule — record others as they are tried, and
      expect a gamescope session to behave differently.
- [x] **Kill the renderer** while it is up (`pkill -f padmode.osk`). *Verified*
      — the keyboard vanished, the pointer kept working, the face buttons went
      back to clicking, Guide still left the mode, and the log carried exactly
      one warning. The pad was readable by other processes afterwards, so the
      grab was not stranded.
- [x] **Round-trip latency** (risk 3). *Verified* — 128 samples, 1.0 ms floor
      and a 17.3 ms ceiling set by our own 60 Hz tick. Not perceptible while
      typing. The daemon logs each trip at debug level, so `-v` re-measures it
      any time.
- [x] **The 60% layout, badges and the new shortcut map.** *Verified* on a real
      pad: badges land on the right keys with A/B/X/Y in Xbox colours, RT
      toggles layers and the toggle reads `abc` on symbols, L3 locks caps and
      the grid redraws in capitals, LB/RB move the text cursor, and repeated
      layer toggles do not resize the keyboard.
- [x] **Auto-open and auto-close.** *Verified* on Kate and Vicinae: clicking a
      field opens the keyboard, clicking the same field again reopens it, and
      a launcher exiting takes the keyboard with it. A keyboard opened with `Y`
      stays. The left mouse button is not left held afterwards — that one was a
      real bug, found by testing, and now has a regression test.
- [x] **Latching Ctrl/Alt/Super.** Latch `super`, commit a letter, and a global
      shortcut fires. *Verified* — Vicinae opened from the pad. Then close and
      reopen the keyboard and type: it must be a plain letter, because a latch
      that survived would turn everything after it into a shortcut.
- [x] **Without `gtk4-layer-shell`**, or on X11: *verified* by running the
      renderer with no `LD_PRELOAD`. It reports
      `{"t":"failed","reason":"layer-shell unavailable ..."}`, exits 1 and
      draws nothing; the daemon logs it once and carries on.

## Out of scope for v1

Word prediction and suggestion strips (needs a dictionary and a text model —
a bigger project than the keyboard). Accent and international layers.
Dictation. Text-cursor movement with the shoulder buttons. Stick navigation.
X11. All are worth having later; none are needed for a couch session to stop
needing a keyboard.
