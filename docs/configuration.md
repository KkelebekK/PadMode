# Configuration

Every default in [usage.md](usage.md) can be changed. Create
`~/.config/padmode/config.toml`:

```toml
[device]
# Autodetected by capability. Set this only if you have several pads.
# Substring match, case-insensitive.
# name = "X-Box"

[general]
modifier = "GUIDE"   # the button you hold. Keep it on Guide: any button your
                     # games read gets left latched down -- see usage.md, Known limitations
volume_step = 5      # percent per press
trigger_threshold = 0.5   # how far LT/RT must be squeezed, on pads whose
                          # triggers are analogue axes. Releases a little below
                          # this, so a resting finger does not chatter

[bindings]
# Bindings merge over the defaults, so you only write what you change.
# Most of this is easier in the settings window (Guide + R3).
B = "media.stop"           # add one
RB = "audio.volume_up"     # change one
Y = false                  # remove one

# A binding can also be any command you like:
START = ["notify-send", "padmode", "hello from the couch"]

[desktop]
enabled = true
enter = "L3"           # held with the modifier to enter desktop mode
exit = "GUIDE"         # pressed on its own to leave
pointer_speed = 900    # pixels per second at full stick deflection
scroll_speed = 10      # wheel clicks per second
dead_zone = 0.15       # radial: ignore the stick within this of centre
curve = 2.0            # 1.0 is linear; higher gives finer slow control
stick_threshold = 0.6  # how far a stick must go to count as a direction press
stick_settle = 0.15    # after a flick, the shortest time before the axis may
                       # re-arm. It also has to return to centre first.
button_debounce = 0.08 # ignore a button release this brief. Stick clicks
                       # (L3/R3) bounce when you move the stick.
stick_dominance = 2.0  # how much one axis must beat the other before it acts,
                       # so a slightly-off flick still steps instead of scrolling

[desktop.bindings]
Y = "key.f5"           # any key, held while the button is held
LB = "key.ctrl+w"      # a combination, fired once per press
R3 = "key.super"       # hold a modifier for as long as you hold the button

# Stick directions can be bound like buttons. Binding one takes that axis away
# from its analogue job, and only the dominant axis of a stick acts at a time
# -- so flicking sideways steps without also scrolling, and pushing up/down
# scrolls without stepping.
RS_RIGHT = "key.tab"
RS_LEFT = "key.shift+tab"

[windows]
backend = "auto"       # auto | kwin | x11 | none. Autodetected from the session
settle = 0.7           # pause between handing the pad back and refocusing the
                       # game, so a button you are still holding can't leak in
launch_timeout = 10    # how long to wait for a launched app's window
park_pointer = false   # move the cursor onto the overlay when it opens, and
                       # back where it was when you return. Worth enabling
                       # under focus-follows-mouse, where a cursor left over
                       # your game can pull focus off the overlay

[overlays.chat]
class = "discord"          # regex, case-insensitive, matched on window class
command = ["discord"]      # optional: without it, only raises a running app

[overlays.browser]
class = "firefox"
command = ["firefox"]

[frontend]
class = "pegasus"       # where a button bound to "frontend" takes you, and
command = ["pegasus-fe"]   # where you land if the game exits while an overlay
                           # is up. The command is used only by the button --
                           # the automatic fallback never starts anything.

[osk]
enabled = true         # the on-screen keyboard
position = "bottom"    # or "top". It never reserves space, so it covers
                       # rather than pushes windows around
height = 30            # percent of screen height; the width scales to match,
                       # so it looks the same on a laptop panel and a TV
auto_open = true       # open when you click into a text field, and close when
                       # that field goes away. Needs desktop accessibility on
# preload = "/usr/lib/libgtk4-layer-shell.so"   # only if autodetection fails

[osk.bindings]         # what the pad does while the keyboard is up.
A = "commit"           # commit, close, back, space, enter, tab, shift,
B = "close"            # capslock, layer, left, right, ctrl, alt, super.
X = "back"             # The d-pad navigates the grid and cannot be rebound.
Y = "space"            # Whatever you bind, the badge on the key follows.
LT = "shift"
RT = "layer"
L3 = "capslock"

[unlock]
enabled = false        # read usage.md, "Unlocking with the pad", first
stick_threshold = 0.6  # how far a stick goes to aim at a digit; it types when
                       # it comes back to half of this

[unlock.bindings]      # see usage.md, "Unlocking with the pad"
LS_UP = "1"
X = "9"
```

Button names accept the obvious spellings: `RB` or `R1`, `GUIDE` or `MODE` or
`HOME`, `SELECT` or `BACK`, `DPAD_UP` or `UP`, `RS_RIGHT` or
`RIGHT_STICK_RIGHT`, in any case.

**The triggers are bindable too.** Most pads — every Xbox controller among
them — report `LT` and `RT` as analogue axes rather than buttons, so padmode
translates a squeeze past `trigger_threshold` into a press and a release below
it back into a release. Bind them like any other button, including as the
modifier or as `desktop.enter`. Pads that already report their triggers as
buttons are left alone, so nothing fires twice. `padmode devices` says which
kind yours is.

Bad configuration stops the daemon with an explanation rather than starting up
and quietly ignoring half your bindings.
