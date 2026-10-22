#!/usr/bin/env python3
"""Write the settings window's sounds: padmode/settings/sounds/*.wav.

The "console tones" set the maintainer chose by ear from browser mock-ups.
These are the same tones -- frequencies, wave shapes, envelopes, offsets --
rendered offline with the standard library, so the files can be rebuilt
from this script instead of being opaque binaries:

    python3 tools/make_sounds.py
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 44100
OUT = Path(__file__).resolve().parent.parent / "padmode" / "settings" / "sounds"


def wave_at(kind: str, phase: float) -> float:
    if kind == "sine":
        return math.sin(2 * math.pi * phase)
    if kind == "triangle":
        return 4 * abs(phase - math.floor(phase + 0.5)) - 1
    raise ValueError(kind)


def tone(buf, freq, dur, kind, vol, slide=None, delay=0.0):
    """One note: 5 ms exponential attack, exponential decay to silence over
    ``dur`` -- what the mock-ups' WebAudio gain ramps did."""
    start = int(delay * RATE)
    n = int(dur * RATE)
    phase = 0.0
    for i in range(n):
        t = i / RATE
        f = freq * (slide / freq) ** (t / dur) if slide else freq
        phase += f / RATE
        attack = min(1.0, t / 0.005)
        env = vol * attack * (0.0001 / vol) ** (t / dur) if vol else 0
        j = start + i
        while len(buf) <= j:
            buf.append(0.0)
        buf[j] += env * wave_at(kind, phase)


SOUNDS = {
    "nav": [(1320, 0.05, "sine", 0.06)],
    "select": [(880, 0.08, "sine", 0.09), (1320, 0.10, "sine", 0.06, None, 0.04)],
    "back": [(990, 0.08, "sine", 0.08, 660)],
    "apply": [(660, 0.09, "triangle", 0.09), (880, 0.09, "triangle", 0.08, None, 0.08),
              (1320, 0.16, "triangle", 0.07, None, 0.16)],
}

#: The mock-ups played at WebAudio's full scale; the files are louder so the
#: window's volume setting has room to turn them down.
GAIN = 6.5


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, notes in SOUNDS.items():
        buf: list[float] = []
        for note in notes:
            tone(buf, *note)
        buf += [0.0] * int(0.02 * RATE)
        peak = max(abs(x) for x in buf) * GAIN
        scale = GAIN if peak <= 0.95 else 0.95 / max(abs(x) for x in buf)
        with wave.open(str(OUT / f"{name}.wav"), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(b"".join(struct.pack("<h", int(x * scale * 32767))
                                     for x in buf))
        print(f"{name}.wav  {len(buf) / RATE * 1000:.0f} ms")


if __name__ == "__main__":
    main()
