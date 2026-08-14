# On-screen keyboard (v0.5)

Type without a physical keyboard: a grid of keys drawn on screen, navigated
with the pad, one button to commit a character. The thing that makes a couch
session self-sufficient — a desktop you can drive but not type into still
sends you looking for a keyboard.

Modelled on the Xbox and PlayStation keyboards.

**Status: working on hardware for the core path.** Driven from a real gamepad
on Plasma 6.7: the keyboard opens, types into an application (letters, shift
and the whole symbol layer), spans the output it lands on, and follows the
active monitor across a two-output session with mixed scaling. 52 tests cover
the grid, protocol, config and routing.

Several checklist items remain untested — the pointer staying live while typing,
behaviour over a fullscreen game, round-trip latency, and what a dying renderer
looks like from the user's seat. They are listed in §Manual checklist, still
unticked.

---

## Decisions

| Question | Decision |
|---|---|
| What draws it | A **separate process in this repo**, launched by the daemon |
| Dependencies | The GUI toolkit is **optional and probed at runtime**, exactly like `playerctl` and `kdotool`. The daemon's own dependencies stay stdlib + `python-evdev` |
| Grid and selection | Owned by the **renderer** |
| Typing | The **daemon** types on the virtual keyboard it already creates (v0.2) |
| Scope for v1 | Xbox-style: letters, digits, a symbol layer, shift, space, backspace, enter — **plus latching Ctrl/Alt/Super**, which consoles have no use for and a desktop cannot do without |
| Toolkit | **GTK4 + `gtk4-layer-shell`** via PyGObject |
| Session support | **Wayland only.** X11 disables the keyboard with one line |
| Is it a mode? | **No** — a layer over a pad-driven mode |
| How it opens | The value `osk` in `[desktop.bindings]` (default `Y`) or `[bindings]` |
| Renderer lifecycle | **Pre-warmed** on entering a pad mode, unmapped until needed |

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

### What keeps that safe in v1: navigate with the d-pad

**The sticks do not drive the OSK in v1.** The d-pad and the face buttons do,
which is how both consoles work anyway.

This matters more than it looks. Every expensive lesson in this project's
input handling — spring-back overshoot crossing the opposite threshold,
`stick_dominance`, `stick_settle`, `button_debounce` — is about turning noisy
analogue deflection into discrete intent. A d-pad emits discrete hat events
already. Keeping the sticks out of v1 means the renderer never has to
re-implement any of that, and the "two processes in the input path" risk drops
to forwarding button presses.

If stick navigation is added later, the digestion belongs in the **daemon** —
it already has that logic, tested — and the renderer should receive the same
discrete `step` messages a d-pad produces. Do not re-derive dead zones in the
renderer.

**No debounce on the keyboard's buttons**, deliberately. The releases held back
in desktop mode exist because a stick *click* bounces when the stick is moved;
these are face buttons, and no such bounce has been traced on them. Inventing a
filter for it would be the exact mistake CLAUDE.md §4 was written about. If a
character ever types twice, trace it before changing anything.

## A layer, not a mode

`osk` is a flag on the state (`State.osk`), valid only while `mode` is in
`PAD_MODES`. It is not a fifth `Mode`, and the concurrency the original spec
worried about never had to be expressed in the state machine.

While the keyboard is up:

- the **d-pad** steps the highlight instead of emitting arrow keys;
- the buttons in `[osk.bindings]` do their keyboard job;
- **every other button keeps its desktop binding** — `SELECT` is still Esc;
- the **sticks still drive the pointer**, which is the whole point;
- the **exit button still leaves the mode**, unconditionally. It is the one
  escape that must never need thinking about.

`A` commits even though it is left-click in desktop mode. Committing a
character is by far the most frequent thing you do while typing, so it gets the
most natural button, and `B` puts the keyboard away in one press when you want
to click something. That trade is the one deliberate loss in "the pointer stays
useful": you can move the cursor while typing, but clicking costs a `B` first.

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

Ctrl, Alt and Super are on the action row and **latch**: press once to hold,
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

Verified end to end without a gamepad, by feeding synthetic pad events into a
real daemon and watching the virtual keyboard device: `LEFTMETA down, A down,
A up, LEFTMETA up`, and a Vicinae window appearing where there was none.

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

**2. Over a fullscreen game. Not measured.** Gotcha §6.2 established that KWin
drops direct scanout when something must be composited on top, which is why
overlays work; a layer surface on the overlay layer should force the same
thing. "Should" is how §6.2 got written wrong the first time, so this stays
unmeasured until someone runs it — it is on the manual checklist.

**3. Round-trip latency per keypress. Not measured.** Needs the real path.

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

The renderer logs one line per measure and rescale to stderr, which the daemon
inherits and journald collects. Sizing depends on compositor timing that no
test can reproduce, so the decisions are traceable rather than merely
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

## Auto-open: specified, not built

Opening the keyboard when you click into a text field. **Not in this change**;
the binding-driven keyboard had to work first.

The useful asset is that **we own the click** — it comes from our own virtual
pointer — so the question is narrowly "did a text field just take focus?".

**AT-SPI is the mechanism.** Subscribe to `object:state-changed:focused`, read
the role and states of whatever took focus (`ROLE_TEXT`, `ROLE_ENTRY`,
`ROLE_PASSWORD_TEXT`, `STATE_EDITABLE`). Already present on this machine —
`at-spi2-core` running, `Atspi-2.0.typelib` installed, no new package. It asks
about the *focused* thing, which is exactly where our uinput keystrokes land,
so the signal and the effect cannot disagree.

Two costs to measure before building: browsers and Electron apps switch into
accessibility mode when an AT client connects, which has a real performance
cost on the whole desktop; and coverage is per-app rather than universal.

The Wayland alternative, `zwp_input_method`, is the "correct" mechanism and
wrong for us: it means *being* an input method, which fights typing through
uinput, and KWin accepts only one — so we would displace fcitx5 or ibus for
anyone running one. (For the record, this session advertises
`zwp_text_input_manager_v3` v2 and `zwp_input_method_v1` v1.)

**The failure modes are lopsided, and that shapes the design.** A false
negative costs one button press. A false positive covers the screen *and* takes
the d-pad and face buttons away from the pointer, so a wrongly-opened keyboard
makes the desktop harder to drive than having no keyboard at all. Auto-open
must be conservative and opt-in.

Auto-*close* on focus leaving a text field is wanted, and will be tried, with
`osk.auto_close` as the retreat if it flaps in practice. The detector should
emit `focus_in`/`focus_out` with the role attached either way, so the retreat
is a default change rather than a redesign.

**This is the first feature in padmode that needs to know something about
application content** rather than input and windows. CLAUDE.md §1's non-goals
do not forbid it, but it is a category shift and belongs in the contract
deliberately.

## Still unmeasured

Everything here is honest about not having happened yet:

- **Over a fullscreen game** (risk 2).
- **Round-trip latency per keypress** (risk 3). Trace with `watch_virtual.py`.
- **Whether it appears over the KDE lock screen**, which would make the unlock
  module (v0.7) "the OSK, at the lock screen" rather than its own thing.
- **Any compositor other than KWin.**

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
- [ ] **The pointer still works** while the keyboard is up — this is the whole
      premise of it being a layer.
- [ ] **`SELECT` is still Escape** while the keyboard is up, and `A` is not a
      left-click until you close it with `B`.
- [ ] **The exit button leaves desktop mode** with the keyboard up, in one
      press, and the keyboard goes with it.
- [ ] **Nothing is left on screen** after leaving. `kdotool search --class .`
      returns the same count as before.
- [x] **Move between outputs mid-session**, in both directions, repeatedly.
      *Verified.* This one item found three separate bugs — all of §Sizing
      items 3, 4 and 5 — and none of them had a CI test available, because the
      renderer needs a compositor. Re-run it after any change to the sizing
      code, in **both** directions: growing and shrinking took different paths
      and only one of them worked.
- [ ] **Over a fullscreen game** (risk 2, unmeasured). Start a game, overlay
      it, open the keyboard, type into something. Record what happened —
      including if it does not work, and on which game.
- [ ] **Kill the renderer** while it is up (`pkill -f padmode.osk`). Desktop
      mode carries on, the pointer still moves, the pad is not stranded, and
      the log says so once.
- [ ] **Round-trip latency** (risk 3). `watch_virtual.py` while typing; is the
      delay between the button and the keystroke perceptible?
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
