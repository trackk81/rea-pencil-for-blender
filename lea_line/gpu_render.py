"""Experimental independent mesh line renderer; never invokes Freestyle.

The render engine receives Blender's RENDER depsgraph, not a viewport snapshot.
It shares edge preparation and shaders with the live preview. This is a line-only
engine: mesh-opaque occlusion, round caps and the supported GPU brush subset.
It is not a Pencil+ rasterizer or a beauty/material renderer.
"""
import time
from types import SimpleNamespace

import bpy
import numpy as np
from mathutils import Vector

LAST_RESULT = {}
_grease_pencil_scopes = {}


def _exclude_grease_pencils(scene):
    objects = [(obj, obj.hide_render) for obj in scene.objects
               if obj.type == 'GREASEPENCIL' and not obj.hide_render]
    try:
        for obj, _ in objects:
            obj.hide_render = True
    except Exception:
        _restore_grease_pencils(objects)
        raise
    return objects


def _restore_grease_pencils(objects):
    for obj, value in objects:
        try:
            if obj.hide_render != value:
                obj.hide_render = value
        except ReferenceError:
            pass  # A deleted scene/object no longer has a setting to restore.


@bpy.app.handlers.persistent
def _render_init(scene, *args):
    # Cover direct engine/F12 invocation as well as the REA line-only button.
    if scene.render.engine == 'REA_PENCIL_GPU':
        key = scene.as_pointer()
        if key not in _grease_pencil_scopes:
            _grease_pencil_scopes[key] = _exclude_grease_pencils(scene)


@bpy.app.handlers.persistent
def _render_end(scene, *args):
    _restore_grease_pencils(_grease_pencil_scopes.pop(scene.as_pointer(), ()))


@bpy.app.handlers.persistent
def _restore_all(*args):
    for objects in _grease_pencil_scopes.values():
        _restore_grease_pencils(objects)
    _grease_pencil_scopes.clear()


def render_lines(scene, *, write_still=False):
    """Run the independent engine and restore the caller's render settings."""
    global LAST_RESULT
    from . import ui
    flags = {name: getattr(scene.render, name) for name in
             ('engine', 'use_freestyle', 'use_compositing', 'use_sequencer')}
    auto_sync = scene.lea_line.auto_sync
    # Blender 4.4 adds its native Grease Pencil pass after an external engine's
    # Combined result, even with compositor/sequencer/Freestyle disabled. A
    # REA-only output must exclude that extra pass for this invocation only.
    grease_pencils = []
    started = time.perf_counter()
    LAST_RESULT = {}
    try:
        with ui.suspend_sync():
            scene.lea_line.auto_sync = False
        scene.render.engine = REA_GPU_LINE_ENGINE.bl_idname
        scene.render.use_freestyle = False
        scene.render.use_compositing = False
        scene.render.use_sequencer = False
        grease_pencils = _exclude_grease_pencils(scene)
        operation = bpy.ops.render.render(write_still=write_still, scene=scene.name)
        if 'CANCELLED' in operation or LAST_RESULT.get('status') == 'cancelled':
            return {'status': 'cancelled', 'seconds': time.perf_counter()-started}
        if LAST_RESULT.get('status') != 'ok':
            raise RuntimeError(LAST_RESULT.get('message', 'GPU line render did not complete'))
        return dict(LAST_RESULT, seconds=time.perf_counter()-started, warnings=[
            '독립 GPU 선 렌더 · 메시 불투명 가림 판정 · 원본과 선 연결부/AA가 다릅니다'])
    finally:
        _restore_grease_pencils(grease_pencils)
        for name, value in flags.items():
            setattr(scene.render, name, value)
        with ui.suspend_sync():
            scene.lea_line.auto_sync = auto_sync


def publish(render_layer, rgba):
    # RNA's collection bulk setter accepts a contiguous NumPy float buffer.
    # Per-pixel NumPy row conversion costs ~1s at 2332x1652 on the test scene.
    render_layer.passes.foreach_set('rect', rgba.ravel())


def draw(scene, depsgraph, entry):
    import gpu
    from . import gpu_preview as preview, gpu_depth, gpu_chain
    camera = scene.camera.evaluated_get(depsgraph)
    if camera.data.type not in {'PERSP', 'ORTHO'}:
        raise ValueError('GPU Line Render supports perspective and orthographic cameras')
    width = scene.render.resolution_x * scene.render.resolution_percentage // 100
    height = scene.render.resolution_y * scene.render.resolution_percentage // 100
    if width < 1 or height < 1 or width * height > 16_777_216:
        raise ValueError('GPU Line Render requires 1..16 megapixels')
    world = camera.matrix_world.normalized()
    view = world.inverted()
    projection = camera.calc_matrix_camera(depsgraph, x=width, y=height,
        scale_x=scene.render.pixel_aspect_x, scale_y=scene.render.pixel_aspect_y)
    pv = projection @ view
    eye = (*world.translation, 0.)
    direction = (*(world.to_3x3() @ Vector((0, 0, 1))), 0.)
    depth, mask, blend = gpu.state.depth_test_get(), gpu.state.depth_mask_get(), gpu.state.blend_get()
    viewport, scissor = gpu.state.viewport_get(), gpu.state.scissor_get()
    offscreen = gpu.types.GPUOffScreen(width, height, format='RGBA32F')
    try:
        with offscreen.bind():
            gpu.state.viewport_set(0, 0, width, height)
            gpu.state.scissor_set(0, 0, width, height)
            framebuffer = gpu.state.active_framebuffer_get()
            framebuffer.clear(color=(0, 0, 0, 0), depth=1.)
            texture = gpu_depth.draw(entry['depth'],
                SimpleNamespace(view_matrix=view, window_matrix=projection), (width, height), -168)
            preview._resources()
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            gpu.state.blend_set('ALPHA')
            shader = preview._shader
            shader.bind()
            for name, value in (('viewProjection', pv), ('eyeInfo', eye),
                                ('directionInfo', direction), ('viewExtra', (0, 0, 0, 0))):
                shader.uniform_float(name, value)
            shader.uniform_sampler('sceneDepth', texture)
            for group in entry['groups']:
                if not len(group['records']):
                    continue
                preview._texture(group)
                info = (width, height, group['width'], int(camera.data.type == 'PERSP'))
                if group['style']['needs_chain']:
                    gpu_chain.update(group, pv, info, eye, direction, -168)
                    shader.bind()
                for name, key in (('edgeData', 'texture'), ('extraNormals', 'normal_texture'),
                    ('styleData', 'style_texture'), ('curveData', 'curve_texture'),
                    ('chainData', 'chain_texture'), ('colorImage', 'color_texture'),
                    ('sizeImage', 'size_texture'), ('coordinateData', 'coordinate_texture')):
                    shader.uniform_sampler(name, group[key])
                shader.uniform_float('viewportInfo', info)
                preview._batch.draw_instanced(shader, instance_count=len(group['records']))
            buffer = framebuffer.read_color(0, 0, width, height, 4, 0, 'FLOAT')
            buffer.dimensions = width * height * 4
            return np.array(buffer).reshape(height, width, 4)
    finally:
        offscreen.free()
        gpu.state.depth_test_set(depth)
        gpu.state.depth_mask_set(mask)
        gpu.state.blend_set(blend)
        gpu.state.viewport_set(*viewport)
        gpu.state.scissor_set(*scissor)


class REA_GPU_LINE_ENGINE(bpy.types.RenderEngine):
    bl_idname = 'REA_PENCIL_GPU'
    bl_label = 'REA Pencil GPU Lines (Experimental)'
    bl_use_gpu_context = True
    bl_use_preview = False
    bl_use_postprocess = False
    bl_use_custom_freestyle = True

    def render(self, depsgraph):
        global LAST_RESULT
        from . import gpu_preview
        start = time.perf_counter()
        scene = depsgraph.scene_eval
        LAST_RESULT = {'status': 'running', 'depsgraph_mode': depsgraph.mode}
        result = None
        try:
            if depsgraph.mode != 'RENDER':
                raise ValueError('GPU Line Render requires the render dependency graph')
            if not scene.camera:
                raise ValueError('GPU Line Render requires a scene camera')
            partial_border = scene.render.use_border and (
                scene.render.border_min_x != 0 or scene.render.border_min_y != 0 or
                scene.render.border_max_x != 1 or scene.render.border_max_y != 1)
            if partial_border or scene.render.use_multiview or scene.render.use_motion_blur:
                raise ValueError('GPU Line Render: border, stereo and motion blur are not supported yet')
            if scene.camera.data.dof.use_dof:
                raise ValueError('GPU Line Render: depth of field is not supported yet')
            if not scene.lea_line.tree:
                raise ValueError('Create a REA Pencil Line tree first')
            self.update_stats('REA Pencil', 'Preparing render geometry')
            entry = gpu_preview.snapshot(scene, depsgraph)
            prepared = time.perf_counter()
            if self.test_break():
                LAST_RESULT = {'status': 'cancelled'}
                return
            rgba = draw(scene, depsgraph, entry)
            drawn = time.perf_counter()
            if self.test_break():
                LAST_RESULT = {'status': 'cancelled'}
                return
            result = self.begin_result(0, 0, rgba.shape[1], rgba.shape[0])
            publish(result.layers[0], rgba)
            self.end_result(result)
            result = None
            LAST_RESULT = {'status': 'ok', 'depsgraph_mode': depsgraph.mode,
                'prepare_seconds': prepared-start, 'GPU_draw_readback_seconds': drawn-prepared,
                'engine_seconds': time.perf_counter()-start, 'edges': entry['edges'],
                'depth_triangles': len(entry['depth']['triangles']),
                'alpha_mass': float(rgba[:, :, 3].sum()), 'size': list(rgba.shape[:2])}
        except Exception as error:
            if result is not None:
                self.end_result(result, cancel=True)
            LAST_RESULT = {'status': 'error', 'message': str(error)}
            self.report({'ERROR'}, str(error))


def register():
    bpy.utils.register_class(REA_GPU_LINE_ENGINE)
    for handlers, callback in ((bpy.app.handlers.render_init, _render_init),
                              (bpy.app.handlers.render_complete, _render_end),
                              (bpy.app.handlers.render_cancel, _render_end),
                              (bpy.app.handlers.load_pre, _restore_all)):
        if callback not in handlers:
            handlers.append(callback)


def unregister():
    _restore_all()
    for handlers, callback in ((bpy.app.handlers.render_init, _render_init),
                              (bpy.app.handlers.render_complete, _render_end),
                              (bpy.app.handlers.render_cancel, _render_end),
                              (bpy.app.handlers.load_pre, _restore_all)):
        if callback in handlers:
            handlers.remove(callback)
    bpy.utils.unregister_class(REA_GPU_LINE_ENGINE)
