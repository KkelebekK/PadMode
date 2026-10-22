#!/usr/bin/env python3
"""Drive the settings window end to end, without a pad and without a screen.

    gtk4-broadwayd :5 &
    systemctl --user stop padmode.service     # it would read the synthetic pad too
    python3 tools/settings_e2e.py assign      # or: feel, close
    systemctl --user start padmode.service

A synthetic uinput pad, the real daemon from this checkout, and the window on a
broadway display -- rendered in memory, never on the desktop you are using.
Each scenario presses buttons on the synthetic pad and then checks what landed
in a throwaway config file; a screenshot of the window's last state is written
next to it. This is how v0.8 was checked (docs/development.md): the unit tests
cover the model and the daemon, this covers the wiring between them.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from evdev import AbsInfo, UInput, ecodes as e

REPO = Path(__file__).resolve().parent.parent
PAD = "Synthetic Test Pad"

SCENARIOS = {
    # Third row of "Hold Guide and press" (Y, mute mic) onto LB, then Apply.
    "assign": ('LB = "mic.toggle_mute"', "Y = false"),
    # Feel page: pointer speed up three steps; curve up one and reset.
    "feel": ("pointer_speed = 1050.0",),
    # Change a value, press B, take "Apply and close".
    "close": ("pointer_speed = 850.0",),
}


def main() -> int:
    scenario = sys.argv[1] if len(sys.argv) > 1 else "assign"
    if scenario not in SCENARIOS:
        print(f"scenarios: {', '.join(SCENARIOS)}")
        return 2
    work = Path(tempfile.mkdtemp(prefix="padmode-e2e-"))
    cfg = work / "config.toml"
    cfg.write_text(f'[device]\nname = "{PAD}"\n')
    axis = AbsInfo(0, -32768, 32767, 0, 0, 0)
    hat_info = AbsInfo(0, -1, 1, 0, 0, 0)
    pad = UInput({
        e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST, e.BTN_TL,
                   e.BTN_TR, e.BTN_SELECT, e.BTN_START, e.BTN_MODE,
                   e.BTN_THUMBL, e.BTN_THUMBR],
        e.EV_ABS: [(e.ABS_X, axis), (e.ABS_Y, axis), (e.ABS_RX, axis),
                   (e.ABS_RY, axis), (e.ABS_HAT0X, hat_info),
                   (e.ABS_HAT0Y, hat_info)],
    }, name=PAD, version=1)
    procs = []
    try:
        time.sleep(0.5)
        procs.append(subprocess.Popen(
            [sys.executable, "-u", "-m", "padmode", "-v", "-c", str(cfg), "run"],
            cwd=REPO, stdout=open(work / "daemon.log", "w"),
            stderr=subprocess.STDOUT))
        time.sleep(2.5)
        env = dict(os.environ, GDK_BACKEND="broadway",
                   BROADWAY_DISPLAY=os.environ.get("BROADWAY_DISPLAY", ":5"))
        delay = "30" if scenario == "close" else "9"
        window = subprocess.Popen(
            [sys.executable, "-m", "padmode.settings", "-v", "-c", str(cfg),
             "--page", "buttons", "--screenshot", str(work / "last.png"),
             "--screenshot-delay", delay],
            cwd=REPO, env=env, stdout=open(work / "window.log", "w"),
            stderr=subprocess.STDOUT)
        procs.append(window)
        time.sleep(3.5)

        def key(code, value):
            pad.write(e.EV_KEY, code, value)
            pad.syn()
            time.sleep(0.15)

        def press(code):
            key(code, 1)
            key(code, 0)

        def flick(axis_code, value, rest=0.25):
            pad.write(e.EV_ABS, axis_code, value)
            pad.syn()
            time.sleep(0.15)
            pad.write(e.EV_ABS, axis_code, 0)
            pad.syn()
            time.sleep(rest)

        if scenario == "assign":
            flick(e.ABS_HAT0Y, 1)
            flick(e.ABS_HAT0Y, 1)
            press(e.BTN_SOUTH)
            time.sleep(0.4)
            press(e.BTN_TL)
            time.sleep(0.6)
            press(e.BTN_START)
        elif scenario == "feel":
            press(e.BTN_TR)
            time.sleep(0.5)
            for _ in range(3):
                flick(e.ABS_HAT0X, 1)
            flick(e.ABS_Y, 32767, rest=0.3)
            flick(e.ABS_HAT0X, 1)
            press(e.BTN_WEST)
            time.sleep(0.3)
            press(e.BTN_START)
        else:
            press(e.BTN_TR)
            time.sleep(0.5)
            flick(e.ABS_HAT0X, -1)
            press(e.BTN_EAST)
            time.sleep(1.5)
            # The dialog's buttons are stacked, "Apply and close" on top
            # and focused: wander down one and back up before taking it.
            flick(e.ABS_HAT0Y, 1)
            flick(e.ABS_HAT0Y, -1)
            press(e.BTN_SOUTH)
            time.sleep(0.8)
        window.wait(timeout=40)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        pad.close()

    text = cfg.read_text()
    missing = [line for line in SCENARIOS[scenario] if line not in text]
    print(text)
    print(f"logs and screenshot: {work}")
    if missing:
        print(f"FAIL: missing {missing}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
