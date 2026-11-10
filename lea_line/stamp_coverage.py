"""Prepare a periodic ink mask shared by the GPU and CPU line samplers."""
import math

import numpy as np

from . import stamp_brush


def enabled(brush):
    return stamp_brush.active(brush) and (
        brush.brush_type == 'MULTIPLE' or brush.brush_map_on)


def prepare(brush, width, node):
    """Bake the union of jittered tips. Alpha is applied once per stroke."""
    spacing, jitter = stamp_brush.preview_pattern(node, width)
    stamps = 32
    period = stamps * spacing
    columns = min(2048, max(128, math.ceil(period * 2)))
    rows = 64
    coverage = np.zeros((rows, columns), np.float32)
    if width <= 0.:
        return coverage, period
    tip = stamp_brush.mask_pixels(brush, width, node)
    tip_height, tip_width = tip.shape
    step = period / columns
    y = (np.arange(rows) + .5) / rows * width - width / 2
    rng = np.random.default_rng(1219)
    for index in range(stamps):
        dx, dy = rng.uniform(-jitter, jitter, 2) if jitter else (0., 0.)
        center = index * spacing + dx
        first = math.floor((center - width / 2) / step)
        last = math.ceil((center + width / 2) / step)
        # A period is at least 32 px. Huge, heavily overlapping tips do
        # not need more than one period of coverage per repeated stamp.
        if last - first > columns:
            first = math.floor(center / step) - columns // 2
            last = first + columns
        x = np.arange(first, last)
        u = ((x + .5)*step-center) / width + .5
        v = (y-dy) / width + .5
        px = np.clip(u * tip_width - .5, 0, tip_width - 1)
        py = np.clip(v * tip_height - .5, 0, tip_height - 1)
        ix, iy = px.astype(np.intp), py.astype(np.intp)
        fx, fy = px-ix, (py-iy)[:, None]
        nx, ny = np.minimum(ix+1, tip_width-1), np.minimum(iy+1, tip_height-1)
        low = tip[np.ix_(iy, ix)]*(1-fx) + tip[np.ix_(iy, nx)]*fx
        high = tip[np.ix_(ny, ix)]*(1-fx) + tip[np.ix_(ny, nx)]*fx
        ink = (low*(1-fy)+high*fy) * ((v >= 0.) & (v <= 1.))[:, None] * ((u >= 0.) & (u <= 1.))
        columns_wrapped = x % columns
        previous = coverage[:, columns_wrapped]
        coverage[:, columns_wrapped] = previous + (1-previous)*ink
    return coverage, period
