"""Which way an animal went through the pet door, from the motion detector's changed blocks: "out",
"in", or None (a visit, or can't tell). For the Google Home cute sentence's facts (cute_context.py).

The pet door camera sits low on the porch looking along the door, the flap on the left. Going out, the
first motion of the event is at the flap (it swings open). Coming in, the animal ends up left of where
it started, near the flap, and once it has gone the porch looks as it did before the event (it isn't
sitting there). Anything else, e.g. sitting by the door and walking off, is a visit and gives no fact.
Tuned 2026-11-20 on 13 hand-checked pet door clips: 12 right; the one miss said "visit", not the
wrong way.

door: the flap's box in the frame, x0,y0,x1,y1 as fractions (google_home.cute_door), e.g.
0.28,0.25,0.47,0.80 for the pet door camera."""

import numpy as np

OUT_SHARE = 0.7          # going out: at least this share of the first moving frame's motion is at the flap
IN_GONE = 0.035          # coming in: at most this share of the porch differs from before the event
IN_MAX_X = 0.65          # ... the last motion is left of this (the flap is at 0.28-0.47)
IN_MOVE = 0.05           # ... and at least this far left of where it started


def parse_box(text):
    """'0.28,0.25,0.47,0.80' -> (0.28, 0.25, 0.47, 0.8); '' -> None."""
    parts = [p for p in (text or "").replace(" ", "").split(",") if p]
    if not parts:
        return None
    if len(parts) != 4:
        raise ValueError("a box is x0,y0,x1,y1")
    return tuple(float(p) for p in parts)


class Track:
    """Per event: where the motion was in each moving frame, and the porch before it began."""

    def __init__(self, door, before):
        self.door = door
        self.before = None if before is None else before.copy()   # block-averaged gray, before the event
        self.moves = []                 # (share of the changed blocks at the flap, mean x 0-1)
        self.after = None               # block-averaged gray at the end of the event

    def add(self, changed):
        n = int(changed.sum())
        if n < 20:
            return
        h, w = changed.shape
        x0, y0, x1, y1 = self.door
        at_door = int(changed[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)].sum())
        xs = np.nonzero(changed)[1]
        self.moves.append((at_door / n, float(xs.mean()) / w))

    def direction(self):
        """'out', 'in' or None. Before the event has ended (self.after unset) only 'out' can be told."""
        if not self.moves:
            return None
        if self.moves[0][0] >= OUT_SHARE:
            return "out"
        if self.after is None or self.before is None:
            return None
        start = np.mean([x for _, x in self.moves[:3]])
        end = np.mean([x for _, x in self.moves[-3:]])
        gone = float((np.abs(self.after - self.before) > 14).mean())
        if gone <= IN_GONE and end < IN_MAX_X and end < start - IN_MOVE:
            return "in"
        return None
