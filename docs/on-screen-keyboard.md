# On-screen keyboard (v0.5)

Type without a physical keyboard: a grid of keys drawn on screen, navigated
with the pad, one button to commit a character. The thing that makes a couch
session self-sufficient — a desktop you can drive but not type into still
sends you looking for a keyboard.

Modelled on the Xbox and PlayStation keyboards. **Not built.** This is the
specification.

---

## Decisions

| Question | Decision |
|---|---|
| What draws it | A **separate process in this repo**, launched by the daemon |
| Dependencies | The GUI toolkit is **optional and probed at runtime**, exactly like `playerctl` and `kdotool`. The daemon's own dependencies stay stdlib + `python-evdev` |
| Grid and selection | Owned by the **renderer** |
| Typing | The **daemon** types on the virtual keyboard it already creates (v0.2) |
| Scope for v1 | Xbox-style: letters, digits, a symbol layer, shift, space, backspace, enter |

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
discrete steps a d-pad produces. Do not re-derive dead zones in the renderer.

## Typing path

Renderer sends `type: q`; the daemon types it on the virtual keyboard from
v0.2. No new device, no new permissions, and the renderer never touches
`/dev/uinput` — which also keeps `reader.VIRTUAL_PREFIX` (§5) doing its one
job of stopping us reading back our own output.

Typing through uinput rather than the Wayland input-method protocol means the
keystrokes are indistinguishable from real hardware, so this works in every
application, in password fields, and on X11 and Wayland alike. It is also
already built and already tested.

## The focus problem

A keyboard that takes focus types into itself. This is the central technical
problem, and the reason this feature is a compositor question at all.

**Measured on this machine** (Plasma 6.7, `wayland-info`):

```
interface: 'zwlr_layer_shell_v1',   version: 5
interface: 'zwp_input_method_v1',   version: 1
```

`zwlr_layer_shell_v1` is the mechanism: a surface on the overlay layer with
`keyboard_interactivity` set to `none` is drawn above everything and never
takes keyboard focus. That is how wvkbd and squeekboard do it.

Portability, and it is not the same story as overlays:

| Session | Approach | Status |
|---|---|---|
| KDE Wayland | `zwlr_layer_shell_v1` | Protocol confirmed present; untested |
| X11 | override-redirect window, or `_NET_WM_STATE_ABOVE` plus no focus | Untested |
| GNOME Wayland | — | **Not possible.** GNOME refuses to implement layer-shell |
| wlroots (sway, Hyprland) | `zwlr_layer_shell_v1` | Should work; community territory |

GNOME Wayland loses this for the same reason it loses overlays — the
compositor declines to expose the capability — but it is a *different*
protocol, so the two limitations should be described separately rather than
lumped together as "GNOME is unsupported".

**Toolkit.** PyQt6, PySide6 and PyGObject are all present here. The deciding
factor is likely to be layer-shell bindings rather than preference:
`gtk-layer-shell` has GObject-introspection bindings usable from Python,
whereas Qt's equivalent, LayerShellQt, is C++ with no Python bindings. That
points at GTK even though KDE is the primary target. Confirm before
committing — this is exactly the kind of assumption §4 says to measure.

## Risks to measure before building

1. **Does our own surface show up as a window?** Today's return-verification
   work measured that `kdotool search --class .` returns plasmashell's shell
   surfaces alongside real windows, and that activating one fails
   permanently — exit 0, no effect, forever. If our keyboard appears in
   `find_window` results the same way, it could be matched by an overlay's
   class regex or, worse, recorded as an overlay's return target, which would
   produce a return that can never succeed and a fallback that fires every
   time. **Check this first**; it is cheap to check and expensive to discover
   later.
2. **Over a fullscreen game.** Gotcha §6.2 measured that KWin stops direct
   scanout when something must be composited on top, which is why overlays
   work. A layer-shell surface should force the same thing, but "should" is
   how §6.2 got written wrong the first time.
3. **Round-trip latency per keypress**, renderer to daemon. Trace it with
   `watch_virtual.py`; if it is perceptible, that is the cost of the ownership
   split and should be recorded honestly rather than tuned in the dark.
4. **What happens when the renderer dies** while the pad is grabbed. The
   daemon must release the grab and leave OSK mode on its own — a stuck grab
   is the worst failure this app has (§4).

## Open questions

- **How is it opened?** A binding (`osk`, alongside `frontend`) covers opening
  it from idle. But the case that matters most is *inside desktop mode*: you
  have just pointed the virtual mouse at a text field and need to type. That
  needs a desktop-mode binding, and `[desktop.bindings]` currently only
  accepts `click.*` and `key.*`.
- **Is `osk` a mode?** It behaves like one — the pad is grabbed and buttons
  mean something specific — but it can also be *concurrent* with desktop
  mode, since the pointer stays useful while the keyboard is up. Concurrency
  would be the first time two modes are active at once, which the state
  machine has never had to express.
- **Where does it sit on screen**, and does it need to avoid covering the text
  field it is typing into? Consoles do not solve this; they own the whole
  screen. We do not.
- **Does it replace part of the unlock module (v0.7)?** Unlock types digits
  blind because you cannot draw over a lock screen. If a layer-shell surface
  *can* appear over the KDE lock screen, unlock becomes "the OSK, at the lock
  screen" rather than its own thing. Worth checking before building either.

## Out of scope for v1

Word prediction and suggestion strips (needs a dictionary and a text model —
a bigger project than the keyboard). Accent and international layers.
Dictation. Text-cursor movement with the shoulder buttons. All are
PlayStation-parity features worth having later; none are needed for a couch
session to stop needing a keyboard.
