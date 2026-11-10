"""Bound retained REA preview buffers, independently of Blender's viewport."""
from collections import deque

MIB = 1024 * 1024

_STROKE_TEXTURES = ('texture', 'normal_texture', 'style_texture', 'curve_texture',
                    'coordinate_texture', 'color_texture', 'size_texture', 'chain_texture')
_CHAIN_TEXTURES = ('chain_vertex_texture', 'chain_edge_texture', 'chain_adjacency_texture',
                   'chain_ping', 'chain_views', 'chain_result_size', 'joint_texture', 'direction_heads')


def _rgba32f(count):
    count = max(1, count)
    width = min(4096, count)
    return width * ((count + width - 1) // width) * 16


def stroke_bytes(group):
    from .gpu_styles import curve_texture_data
    style = group['style']
    n = len(group['records'])
    size = (_rgba32f(4*n) + _rgba32f(len(group['extra_normals'])) + style['params'].nbytes
            + curve_texture_data(style).nbytes + _rgba32f(group['map_coordinates'].size//4) + 16)
    size += sum(style['images'][name].nbytes if name in style['images'] else 16
                for name in ('color', 'size'))
    if style['needs_chain']:
        from . import gpu_chain
        vertices = len(group['chain_vertices'])
        # Topology textures, ping-pong work buffers and one region's outputs.
        size += _rgba32f(vertices) + _rgba32f(n) + _rgba32f(vertices+2*n) + 2*_rgba32f(2*n)
        size += _rgba32f(n)
        params = style['params']
        if gpu_chain.tracks_joints(params):
            size += _rgba32f(n)
        if (params[6,2] == 0 and params[7,2]) or params[18,0] or params[17,3]:
            size += _rgba32f(n)
        if params[17,3]:
            size += _rgba32f(2*n)
        if n>=8 and params[18,0]:
            size += gpu_chain.MAX_WAVE_MASK_BYTES
    return size


def release_stroke(group):
    for key in _STROKE_TEXTURES + _CHAIN_TEXTURES:
        group.pop(key, None)
    group['texture'] = None


class BufferCache:
    def __init__(self):
        self.buffers = {}
        self.bytes = self.limit = self.evictions = self.streamed = 0
        self.offscreen = deque()

    def begin(self, visible, limit):
        self.limit = limit
        self.streamed = 0
        self.offscreen = deque(key for key in self.buffers if key not in visible)
        while self.bytes > limit:
            self.evict(self.offscreen.popleft() if self.offscreen else next(iter(self.buffers)))

    def evict(self, key):
        size, release, resource = self.buffers.pop(key)
        release(resource)
        self.bytes -= size
        self.evictions += 1

    def retain(self, key, size, release, resource):
        if key in self.buffers:
            return True
        if size <= self.limit:
            while self.bytes+size > self.limit and self.offscreen:
                self.evict(self.offscreen.popleft())
            if self.bytes+size <= self.limit:
                self.buffers[key] = size, release, resource
                self.bytes += size
                return True
        # Keep already visible cached entries. Evicting them on every draw
        # would re-upload the whole scene whenever the working set is larger.
        self.streamed += size
        return False


def release_depth(data):
    data.pop('batch', None)
