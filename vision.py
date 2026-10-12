"""Motion scoring and frame picking for cmo_watch.py (classification is cmo_ollama's, see cmo_watch.py).

Runs on the pet door Pi too: Python 3.7, numpy 1.16, Pillow 5.4.
"""
import io
import logging

import numpy as np
from PIL import Image

log = logging.getLogger("cmo")

# ---- motion ---------------------------------------------------------------
class MotionScorer:
    """Score each frame by the fraction of (unmasked) pixels that differ from a running background.

    Works on a block-averaged gray image (160x120 for 640x480 with block 4), which also smooths out
    sensor noise."""

    def __init__(self, width, height, masks=(), pixel_threshold=14, alpha=0.08, global_change=0.45, block=4):
        self.block = block
        self.h, self.w = height // block, width // block
        self.pixel_threshold = pixel_threshold
        self.alpha = alpha
        self.global_change = global_change
        self.mask = np.ones((self.h, self.w), dtype=bool)
        for x0, y0, x1, y1 in masks:
            self.mask[int(y0 * self.h):int(np.ceil(y1 * self.h)), int(x0 * self.w):int(np.ceil(x1 * self.w))] = False
        self.bg = None

    def score(self, rgb):
        b = self.block
        g = rgb[:self.h * b, :self.w * b].astype(np.float32).dot(np.array([0.299, 0.587, 0.114], np.float32))
        g = g.reshape(self.h, b, self.w, b).mean(axis=(1, 3))
        if self.bg is None:
            self.bg = g
            return 0.0
        changed = np.abs(g - self.bg) > self.pixel_threshold
        s = float(changed[self.mask].mean())
        if s > self.global_change:   # a light switched on or off, or the camera re-exposed: start over
            self.bg = g
            return 0.0
        self.bg += self.alpha * (g - self.bg)
        return s


def sharpness(rgb):
    """Variance of the Laplacian of the green channel. Higher means sharper and better lit, so
    motion-blurred and near-black frames sink to the bottom."""
    g = rgb[:, :, 1].astype(np.float32)
    lap = 4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


def frame_rank(motion, rgb, motion_cap=0.05):
    """How good a frame is to show the LLM. More motion helps up to motion_cap (about 5% of the frame
    changing). Past the cap, sharpness decides, because the frames with the most motion are usually
    a blurry subject right in front of the lens."""
    return sharpness(rgb) * min(motion / motion_cap, 1.0)


class Candidate:
    __slots__ = ("score", "t", "rgb")

    def __init__(self, score, t, rgb):
        self.score, self.t, self.rgb = score, t, rgb


class FramePicker:
    """Keep the k highest-scoring frames that are at least min_gap seconds apart (a greedy
    non-maximum suppression), using O(k) memory however long the event runs."""

    def __init__(self, k=4, min_gap=1.0):
        self.k, self.min_gap = k, min_gap
        self.items = []

    def add(self, score, t, rgb):
        near = [c for c in self.items if abs(c.t - t) < self.min_gap]
        if near:
            if score <= max(c.score for c in near):
                return
            for c in near:
                self.items.remove(c)
        elif len(self.items) >= self.k:
            worst = min(self.items, key=lambda c: c.score)
            if score <= worst.score:
                return
            self.items.remove(worst)
        self.items.append(Candidate(score, t, rgb.copy()))

    def best_first(self):
        return sorted(self.items, key=lambda c: c.score, reverse=True)


def to_jpeg(rgb, quality=85):
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def make_grid(frames, cols=None):
    """Tile same-sized RGB frames left-to-right, top-to-bottom: 1x2 for 2, 2x2 for 3-4, 3x2 for 5-6."""
    cols = min(cols or (2 if len(frames) <= 4 else 3), len(frames))
    rows = -(-len(frames) // cols)
    h, w, _ = frames[0].shape
    grid = np.zeros((rows * h, cols * w, 3), np.uint8)
    for i, f in enumerate(frames):
        r, c = divmod(i, cols)
        grid[r * h:(r + 1) * h, c * w:(c + 1) * w] = f
    grid[:, [c * w for c in range(1, cols)]] = 255     # thin separators so the model sees separate frames
    grid[[r * h for r in range(1, rows)], :] = 255
    return grid
