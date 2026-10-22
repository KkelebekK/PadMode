"""The tab highlight's squash and stretch, as plain arithmetic.

Kept out of the GTK code so it can be tested, and tuned, without a display.
:func:`pill` answers one question: at time ``t`` (0..1) of a slide from one
tab to another, where are the highlight's edges and how tall is it?

The feel comes from the two edges not moving together:

* the **leading** edge sets off at once and overshoots a little before it
  settles (an ease-out-back curve) -- the pill *stretches* towards the new tab;
* the **trailing** edge waits, then follows with a smaller overshoot of its
  own -- it arrives after the leading edge has already settled, swings past
  its mark, and for a moment the pill *squeezes* before it rests;
* its **height** answers its width, the way a squashed ball's does: thinner
  while stretched, a touch thicker while squeezed. Volume, roughly, is kept.
"""

from __future__ import annotations

#: When the trailing edge sets off, as a share of the slide.
TRAIL_DELAY = 0.1
#: How far each edge overshoots, in ease-out-back's own units.
OVERSHOOT = 1.6
TRAIL_OVERSHOOT = 1.2
#: How much the height answers the width. 0 keeps it rigid.
SQUASH = 0.45
#: Height never goes below or above these shares of its resting height.
MIN_HEIGHT, MAX_HEIGHT = 0.7, 1.15


def ease_out_back(t: float, s: float = OVERSHOOT) -> float:
    t -= 1.0
    return t * t * ((s + 1.0) * t + s) + 1.0


def ease_in_out_cubic(t: float) -> float:
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def _clamp(t: float) -> float:
    return 0.0 if t < 0 else 1.0 if t > 1 else t


def pill(t: float, start: tuple[float, float], end: tuple[float, float],
         height: float) -> tuple[float, float, float]:
    """``(left, right, height)`` at time ``t``.

    ``start`` and ``end`` are the ``(left, right)`` edges of the old and new
    tab. The height is centred by the caller.
    """
    t = _clamp(t)
    if t <= 0:
        return start[0], start[1], height
    forward = end[0] >= start[0]
    lead = ease_out_back(_clamp(t / (1 - TRAIL_DELAY)))
    trail = ease_out_back(_clamp((t - TRAIL_DELAY) / (1 - TRAIL_DELAY)),
                          TRAIL_OVERSHOOT)
    if forward:   # moving right: the right edge leads
        right = start[1] + (end[1] - start[1]) * lead
        left = start[0] + (end[0] - start[0]) * trail
    else:         # moving left: the left edge leads
        left = start[0] + (end[0] - start[0]) * lead
        right = start[1] + (end[1] - start[1]) * trail
    if right < left + 1:   # never inside out, however hard the spring
        right = left + 1
    rest = start[1] - start[0] + (end[1] - start[1] - (end[0] - start[0])) * t
    stretch = (right - left) / rest if rest > 0 else 1.0
    h = height * (1 - SQUASH * (stretch - 1) / max(stretch, 1e-6))
    h = min(max(h, height * MIN_HEIGHT), height * MAX_HEIGHT)
    if t >= 1:
        return end[0], end[1], height
    return left, right, h
