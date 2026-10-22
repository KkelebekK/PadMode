# Development

## Tests

```bash
python -m unittest discover -s tests -t .
```

Tests use stdlib `unittest`, so contributors need nothing installed beyond what
the app itself requires. The suite has two layers: pure unit tests for the
decision logic, which need no hardware, and integration tests that create a
synthetic uinput gamepad and replay real event sequences through the real read
loop. The integration tests skip themselves automatically without `/dev/uinput`
access.

A handful of tests drive the real desktop -- the pointer moves, windows
alt-tab -- and are skipped unless you ask for them. Run them before a release,
on a session you do not mind being typed into:

```bash
PADMODE_INTEGRATION=1 python -m unittest discover -s tests -t .
```

The settings window is checked end to end with no pad and no screen: a
synthetic uinput pad, the real daemon, and the window on a broadway display,
rendered in memory. Stop the service first, or it reads the synthetic pad too:

```bash
gtk4-broadwayd :5 &
systemctl --user stop padmode.service
python3 tools/settings_e2e.py assign   # also: feel, close
systemctl --user start padmode.service
```

`python -m padmode.settings --screenshot out.png --page feel` renders one page
to a PNG the same way, for checking a layout change.

See [CLAUDE.md](../CLAUDE.md) for the design contract and the reasoning behind the
locked decisions.

## Roadmap

| Version | Adds | Needs a compositor? |
|---|---|---|
| ✅ v0.1 | Media, volume, mic while holding Guide | No |
| ✅ v0.2 | Virtual mouse/keyboard, desktop mode | No |
| ✅ v0.3 | Window backends, in-game overlays, return-to-game | KDE + X11 |
| ✅ **v0.4** | Frontend button and fallback, Heroic metadata generator | KDE + X11 |
| ✅ **v0.5** | On-screen keyboard you drive with the pad, like a console | KDE Wayland only |
| ✅ v0.6 | Steam metadata generator | No |
| ✅ **v0.7** | Unlocking the screen with the pad (opt-in, off by default) | Screensaver D-Bus |
| ✅ **v0.8** | Settings window — change the config from the pad | No |

v0.1 and v0.2 are deliberately useful on their own, on any desktop. Overlays
are the first feature that has to activate a window, which is where Wayland
starts having opinions — so they are additive, and their absence costs you
nothing else.
