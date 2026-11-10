"""Brush Detail swatch drawn beside the active detail in the node editor."""
from collections import OrderedDict
import math

import bpy
from bpy.app.handlers import persistent
import gpu
from gpu_extras.presets import draw_texture_2d
from mathutils import Matrix
import numpy as np

from . import curves, routing, stamp_brush, texture_maps

TIP, STROKE = 64, 192
WIDTH, HEIGHT = TIP + STROKE + 3, TIP + 2
_cache = OrderedDict()
_handler = None


def _map(brush, kind):
    try:
        return stamp_brush.detail_map(brush, kind)
    except ValueError:
        return None


def _map_key(node):
    if node is None:
        return None
    image = node.image
    return (image.as_pointer(), tuple(image.size), stamp_brush._revisions.get(image.as_pointer(), 0),
            node.wrap_mode_u, node.wrap_mode_v, node.filter_mode, tuple(node.tiling),
            tuple(node.offset), node.rotation)


def _curve(brush, kind, parameter):
    detail = getattr(brush, 'detail_source', None)
    if detail is not None and getattr(brush, kind.lower() + '_curve_enabled', False):
        node = curves.get_mapping_node(detail, kind)
        if node is not None:
            mapping = node.mapping
            curve = mapping.curves[0]
            return np.clip([mapping.evaluate(curve, x) for x in parameter], 0., 1. if kind == 'ALPHA' else 4.)
    if kind == 'SIZE' and brush.taper:
        return np.interp(parameter, (0., .5, 1.), (brush.taper_start, 1., brush.taper_end))
    return np.ones_like(parameter)


def _stroke(brush, width, stamp, distortion, pattern):
    spacing, jitter = pattern
    x = np.linspace(18., 164., math.ceil(146/spacing)+1)
    t = (x - x[0]) / (x[-1] - x[0])
    y = 31.5 + 12 * np.sin(2 * math.pi * (x - 18.5) / 128)
    distance = np.r_[0., np.cumsum(np.hypot(np.diff(x), np.diff(y)))]
    if brush.distortion_enabled:
        y += brush.distortion_amplitude * np.sin(
            2 * math.pi * distance / brush.distortion_wavelength + brush.distortion_phase)
        if distortion is not None:
            uv = np.column_stack((x / STROKE, y / TIP))
            y += brush.distortion_map_amount * texture_maps.sample(distortion, uv)[:, :3].mean(axis=1)
    y += brush.normal_offset - brush.offset_y
    x += brush.offset_x
    if jitter:
        offsets = np.random.default_rng(1219).uniform(-jitter, jitter, (2, len(x)))
        x += offsets[0]
        y += offsets[1]
    # The original swatch measured about 1 px at size 1 and size + 1
    # from size 2 through 16. This offset is only for the editor sample.
    size = (width + min(1., width-1.)) * _curve(brush, 'SIZE', t)
    opacity = _curve(brush, 'ALPHA', t)
    if stamp is not None:
        size *= np.interp(t, (0., 1.), (brush.stamp_reduction_start, brush.stamp_reduction_end))
    if brush.dashed:
        visible = (distance + brush.dash_phase) % (brush.dash + brush.gap) < brush.dash
    else:
        visible = np.ones_like(t, dtype=bool)
    ink = np.zeros((TIP, STROKE), dtype=np.float32)
    for cx, cy, diameter, alpha in zip(x[visible], y[visible], size[visible], opacity[visible]):
        radius = diameter * .5
        left, right = max(0, math.floor(cx-radius)), min(STROKE, math.ceil(cx+radius+1))
        bottom, top = max(0, math.floor(cy-radius)), min(TIP, math.ceil(cy+radius+1))
        if left >= right or bottom >= top or radius < .01:
            continue
        yy, xx = np.ogrid[bottom:top, left:right]
        dx, dy = xx-cx, yy-cy
        if stamp is None:
            coverage = np.clip(radius + .5 - np.hypot(dx, dy), 0., 1.)
        else:
            last = stamp.shape[0]-1
            u = np.rint((dx / diameter + .5) * last).astype(int)
            v = np.rint((dy / diameter + .5) * last).astype(int)
            coverage = stamp[v.clip(0, last), u.clip(0, last)] * ((abs(dx) <= radius) & (abs(dy) <= radius))
        patch = ink[bottom:top, left:right]
        np.maximum(patch, coverage * alpha, out=patch)
    return ink


def sample_pixels(brush, width, brush_map=None, distortion=None):
    """Draw two grayscale samples into an RGBA buffer; no Blender IDs are made."""
    yy, xx = np.ogrid[:TIP, :TIP]
    stamp = stamp_brush.mask_pixels(brush, 58., brush_map) if stamp_brush.active(brush) else None
    if stamp is None:
        tip = np.clip(29.5 - np.hypot(xx-31.5, yy-31.5), 0., 1.)
    else:
        samples = np.clip(np.rint((np.arange(TIP)-2.5) * (stamp.shape[0]-1) / 58).astype(int), 0, stamp.shape[0]-1)
        tip = stamp[np.ix_(samples, samples)]
    ink = _stroke(brush, width, stamp, distortion, stamp_brush.preview_pattern(brush_map, width))
    pixels = np.full((HEIGHT, WIDTH, 4), .95, dtype=np.float32)
    pixels[1:-1, 1:TIP+1, :3] = (.8 - .7*tip)[:, :, None]
    pixels[1:-1, TIP+2:-1, :3] = (.8 - .7*ink)[:, :, None]
    y, x = np.ogrid[:HEIGHT, :WIDTH]
    corners = np.hypot(np.maximum(abs(x-(WIDTH-1)*.5)-(WIDTH*.5-10), 0.),
                       np.maximum(abs(y-(HEIGHT-1)*.5)-(HEIGHT*.5-10), 0.))
    pixels[:, :, 3] = np.clip(10 - corners, 0., 1.)
    return pixels


def _texture(tree, detail, brush, width):
    brush_map = _map(brush, 'brush') if stamp_brush.active(brush) else None
    distortion = _map(brush, 'distortion') if brush.distortion_enabled else None
    fields = ('brush_type', 'dashed', 'dash', 'gap', 'dash_phase', 'taper', 'taper_start',
              'taper_end', 'distortion_enabled', 'distortion_amplitude', 'distortion_wavelength',
              'distortion_phase', 'distortion_map_amount', 'normal_offset', 'offset_x', 'offset_y',
              'size_curve_enabled', 'alpha_curve_enabled')
    curve_data = curves.export_detail(detail) if brush.size_curve_enabled or brush.alpha_curve_enabled else None
    key = (detail.name, width, tuple(getattr(brush, field) for field in fields),
           stamp_brush.spec(brush), _map_key(brush_map), _map_key(distortion),
           curve_data)
    pointer = tree.as_pointer()
    previous = _cache.get(pointer)
    if previous is not None and previous[0] == key:
        _cache.move_to_end(pointer)
        return previous[1]
    pixels = sample_pixels(brush, width, brush_map, distortion)
    buffer = gpu.types.Buffer('FLOAT', pixels.size, pixels.ravel())
    texture = gpu.types.GPUTexture((WIDTH, HEIGHT), format='RGBA8', data=buffer)
    _cache[pointer] = (key, texture)
    _cache.move_to_end(pointer)
    if len(_cache) > 8:
        _cache.popitem(last=False)
    return texture


def _draw():
    from . import ui
    context = bpy.context
    tree = context.space_data.edit_tree
    if tree is None or tree.bl_idname != 'LEALineTree':
        return
    try:
        _, brush, detail, _ = ui._panel_context(tree)
        if detail is None or detail.dimensions.y <= 0:
            return
        scale = context.preferences.system.ui_scale
        location = detail.location_absolute
        region = context.region
        width, height = WIDTH*scale, HEIGHT*scale
        x, y = region.view2d.view_to_region(location.x*scale, location.y*scale - detail.dimensions.y*.5, clip=False)
        x -= width + 16*scale
        y -= height*.5
        if x + width <= 0 or x >= region.width or y + height <= 0 or y >= region.height:
            return
        size = min(20., max(1., brush.thickness if brush else 1.))
        effective = routing.ResolvedBrush(detail, {'detail_source': detail})
        texture = _texture(tree, detail, effective, size)
    except (ReferenceError, RuntimeError, ValueError):
        # Blender can draw an incomplete graph while sockets are being linked.
        return
    blend = gpu.state.blend_get()
    try:
        gpu.state.blend_set('ALPHA')
        with gpu.matrix.push_pop(), gpu.matrix.push_pop_projection():
            gpu.matrix.load_identity()
            gpu.matrix.load_projection_matrix(
                Matrix.Translation((-1., -1., 0.)) @ Matrix.Diagonal((2./region.width, 2./region.height, 1., 1.)))
            draw_texture_2d(texture, (x, y), width, height)
    finally:
        gpu.state.blend_set(blend)


@persistent
def _clear(*_args):
    _cache.clear()


def register():
    global _handler
    if bpy.app.background or _handler is not None:
        return
    _handler = bpy.types.SpaceNodeEditor.draw_handler_add(_draw, (), 'WINDOW', 'BACKDROP')
    bpy.app.handlers.load_pre.append(_clear)
    bpy.app.handlers.undo_post.append(_clear)
    bpy.app.handlers.redo_post.append(_clear)


def unregister():
    global _handler
    if _handler is not None:
        bpy.types.SpaceNodeEditor.draw_handler_remove(_handler, 'WINDOW')
        _handler = None
    for handlers in (bpy.app.handlers.load_pre, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _clear in handlers:
            handlers.remove(_clear)
    _clear()
