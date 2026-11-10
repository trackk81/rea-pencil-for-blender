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
from mathutils import Matrix, Vector

LAST_RESULT = {}
_capture_requested = False
_captured_pixels = None
_line_render_scopes = {}
LINE_ENGINES = {'REA_PENCIL_GPU', 'REA_PENCIL_CPU'}


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


def _restore_line_scope(scene, objects, compositing, sequencer):
    _restore_grease_pencils(objects)
    try:
        scene.render.use_compositing = compositing
        scene.render.use_sequencer = sequencer
    except ReferenceError:
        pass


@bpy.app.handlers.persistent
def _render_init(scene, *args):
    # Cover direct engine/F12 invocation as well as the REA line-only button.
    if scene.render.engine in LINE_ENGINES:
        key = scene.as_pointer()
        if key not in _line_render_scopes:
            render = scene.render
            _line_render_scopes[key] = (scene, _exclude_grease_pencils(scene),
                                        render.use_compositing, render.use_sequencer)
            # Use Blender's standard result layout without running the
            # authored compositor or sequence editor for a line-only render.
            render.use_compositing = False
            render.use_sequencer = False


@bpy.app.handlers.persistent
def _render_end(scene, *args):
    scope = _line_render_scopes.pop(scene.as_pointer(), None)
    if scope is not None:
        _restore_line_scope(*scope)


@bpy.app.handlers.persistent
def _restore_all(*args):
    for scope in _line_render_scopes.values():
        _restore_line_scope(*scope)
    _line_render_scopes.clear()


def render_lines(scene, *, write_still=False, capture=False):
    """Run the independent engine and restore the caller's render settings."""
    global LAST_RESULT, _capture_requested, _captured_pixels
    from . import ui
    flags = {name: getattr(scene.render, name) for name in
             ('engine', 'use_freestyle', 'use_compositing', 'use_sequencer')}
    auto_sync = scene.lea_line.auto_sync
    # Blender 4.4 adds its native Grease Pencil pass after an external engine's
    # Combined result, even with compositor/sequencer/Freestyle disabled. A
    # REA-only output must exclude that extra pass for this invocation only.
    grease_pencils = []
    cpu = flags['engine'] == 'REA_PENCIL_CPU' or (
        flags['engine'] not in LINE_ENGINES and scene.lea_line.final_line_backend == 'CPU')
    started = time.perf_counter()
    LAST_RESULT = {}
    previous_capture = _capture_requested
    _capture_requested = capture
    _captured_pixels = None
    try:
        with ui.suspend_sync():
            scene.lea_line.auto_sync = False
        scene.render.engine = REA_CPU_LINE_ENGINE.bl_idname if cpu else REA_GPU_LINE_ENGINE.bl_idname
        scene.render.use_freestyle = False
        scene.render.use_compositing = False
        scene.render.use_sequencer = False
        grease_pencils = _exclude_grease_pencils(scene)
        operation = bpy.ops.render.render(write_still=write_still, scene=scene.name)
        if 'CANCELLED' in operation or LAST_RESULT.get('status') == 'cancelled':
            return {'status': 'cancelled', 'seconds': time.perf_counter()-started}
        if LAST_RESULT.get('status') != 'ok':
            raise RuntimeError(LAST_RESULT.get('message', 'REA line render did not complete'))
        result = dict(LAST_RESULT, seconds=time.perf_counter()-started, warnings=[
            f'독립 {"CPU" if cpu else "GPU"} 선 렌더 · 메시 불투명 가림 판정 · 원본과 선 연결부/AA가 다릅니다'])
        if capture:
            if _captured_pixels is None:
                raise RuntimeError('REA line image was not captured')
            result['views' if isinstance(_captured_pixels, dict) else 'pixels'] = _captured_pixels
        return result
    finally:
        _capture_requested = previous_capture
        _captured_pixels = None
        _restore_grease_pencils(grease_pencils)
        for name, value in flags.items():
            setattr(scene.render, name, value)
        with ui.suspend_sync():
            scene.lea_line.auto_sync = auto_sync


def publish(render_layer, rgba):
    # RNA's collection bulk setter accepts a contiguous NumPy float buffer.
    # Per-pixel NumPy row conversion costs ~1s at 2332x1652 on the test scene.
    render_layer.passes.foreach_set('rect', rgba.ravel())


# Keep GPU color/depth allocation bounded independently of output dimensions.
_TILE_SIDE = 4096
_RENDER_AREA = -168


def _render_window(render):
    width = render.resolution_x * render.resolution_percentage // 100
    height = render.resolution_y * render.resolution_percentage // 100
    if render.use_border:
        # Blender multiplies the normalized border in float32 and truncates
        # each endpoint independently when creating its internal result.
        left = int(np.float32(render.border_min_x) * np.float32(width))
        bottom = int(np.float32(render.border_min_y) * np.float32(height))
        right = int(np.float32(render.border_max_x) * np.float32(width))
        top = int(np.float32(render.border_max_y) * np.float32(height))
    else:
        left, bottom, right, top = 0, 0, width, height
    if right <= left or top <= bottom:
        raise ValueError('GPU 렌더 영역이 1픽셀보다 작습니다')
    return (width, height), (left, bottom, right, top)


def _tile_projection(projection, width, height, x, y, tile_width, tile_height):
    """Map a full-frame pixel rectangle to clip space without changing depth."""
    sx, sy = width / tile_width, height / tile_height
    crop = Matrix(((sx, 0, 0, sx - 1 - 2*x/tile_width),
                   (0, sy, 0, sy - 1 - 2*y/tile_height),
                   (0, 0, 1, 0), (0, 0, 0, 1)))
    return crop @ projection


def _camera_projection(scene, depsgraph, camera, frame, shift_x):
    width, height = frame
    scale_x, scale_y = scene.render.pixel_aspect_x, scene.render.pixel_aspect_y
    projection = camera.calc_matrix_camera(depsgraph, x=width, y=height, scale_x=scale_x, scale_y=scale_y)
    if shift_x is not None:
        fit = camera.data.sensor_fit
        vertical = fit == 'VERTICAL' or (fit == 'AUTO' and height*scale_y > width*scale_x)
        view_factor = height*scale_y/(width*scale_x) if vertical else 1.0
        # Apply Blender's per-view lens shift in clip space for either camera type.
        projection[0] -= 2*(shift_x-camera.data.shift_x)*view_factor*projection[3]
    return projection


def draw(scene, depsgraph, entry, frame, bounds, *, world=None, shift_x=None):
    import gpu
    from . import gpu_preview as preview, gpu_depth, gpu_chain
    camera = scene.camera.evaluated_get(depsgraph)
    if camera.data.type not in {'PERSP', 'ORTHO'}:
        raise ValueError('GPU Line Render supports perspective and orthographic cameras')
    width, height = frame
    x0, y0, x1, y1 = bounds
    region_width, region_height = x1-x0, y1-y0
    cropped_frame = bounds != (0,0,width,height)

    texture_limit = gpu.capabilities.max_texture_size_get()
    tile_limit = min(_TILE_SIDE, texture_limit)
    groups = [group for group in entry['groups'] if len(group['records'])]
    large_render = entry['edges'] > preview.MAX_GROUP_EDGES
    streamed_groups = {id(group) for group in groups if large_render
                       and not group['style']['needs_chain'] and not group['style']['maps']}
    stroke_samplers = (('edgeData', 'texture'), ('extraNormals', 'normal_texture'),
                       ('styleData', 'style_texture'), ('curveData', 'curve_texture'),
                       ('chainData', 'chain_texture'), ('colorImage', 'color_texture'),
                       ('sizeImage', 'size_texture'), ('coordinateData', 'coordinate_texture'))
    # Keep unshifted centerline depth samples inside the tile after an offset,
    # extension, or round cap moves the fragment away from its source.
    reach = max((group['width'] * group['style']['params'][3, 2] / 2
                 + group['style']['params'][7, 3]
                 + max(abs(group['style']['params'][17,0]),abs(group['style']['params'][17,1]))
                 + 4*abs(group['style']['params'][17,2])
                 + abs(group['style']['params'][18,0])
                 + max(0.,group['style']['params'][19,1]-.5)
                 for group in groups), default=0)
    margin = int(np.ceil(reach)) + 2
    if any((group['style']['params'][7,3]>0 or group['style']['params'][17,2]!=0)
           and group['style']['params'][6,2]==0 for group in groups):
        # A joint can also sample just inside the neighboring edge.
        margin += 2
    visibility = {id(group):gpu_chain.visibility_minimum(group) for group in groups}
    if cropped_frame or width>tile_limit or height>tile_limit:
        # Length queries can leave the readback rectangle by one threshold.
        # Keep a bounded tile overlap, preserving the original chain fallback
        # when an authored threshold cannot fit that overlap.
        available = max(0, tile_limit//4-margin-1)
        visibility = {key:value if value<=available else 0 for key,value in visibility.items()}
        maximum = max(visibility.values(),default=0)
        if maximum:margin+=int(np.ceil(maximum))+1
    padded_width = min(width,x1+margin)-max(0,x0-margin)
    padded_height = min(height,y1+margin)-max(0,y0-margin)
    step_x = region_width if padded_width<=tile_limit else max(1,tile_limit-2*margin)
    step_y = region_height if padded_height<=tile_limit else max(1,tile_limit-2*margin)
    tiled = step_x < region_width or step_y < region_height
    windowed = cropped_frame or tiled
    mixed_groups = [group for group in groups if group['style']['params'][17,3]]
    wave_groups = {id(group) for group in groups if group['style']['params'][18,0]
                   and max(width,height)<=texture_limit}
    if windowed and mixed_groups and max(width,height)>texture_limit:
        raise ValueError(f'GPU: 면 방향이 섞인 마크 선의 Normal Offset은 전체 화면 깊이가 필요합니다. 한 변이 GPU 한도 {texture_limit}px를 넘으면 Native를 사용하세요.')
    if max(padded_width,padded_height)>tile_limit and tile_limit <= 2*margin:
        raise ValueError('GPU 타일에서 이 선을 그릴 수 없습니다. 브러시 굵기, 연장 길이 또는 오프셋을 줄이세요.')
    entry['gpu_texture_limit'] = texture_limit
    entry['render_tiles'] = 0
    test_break = entry.get('test_break')

    if world is None:
        world = camera.matrix_world.normalized()
    view = world.inverted()
    projection = _camera_projection(scene, depsgraph, camera, frame, shift_x)
    pv = projection @ view
    eye = (*world.translation, 0.)
    direction = (*(world.to_3x3() @ Vector((0, 0, 1))), 0.)
    perspective = int(camera.data.type == 'PERSP')
    depth, mask, blend = gpu.state.depth_test_get(), gpu.state.depth_mask_get(), gpu.state.blend_get()
    viewport, scissor = gpu.state.viewport_get(), gpu.state.scissor_get()
    offscreen = None
    tile_size = None
    try:
        preview._resources()
        shader = preview._shader
        for group in groups:
            if id(group) in streamed_groups:
                continue
            preview._texture(group)
            if group['style']['needs_chain']:
                info = (width, height, group['width'], perspective)
                gpu_chain.update(group, pv, info, eye, direction, _RENDER_AREA)
        normal_directions, wave_runs = {}, {}
        full_depth = None
        if windowed and (mixed_groups or wave_groups):
            if test_break is not None and test_break():
                return None
            # Chain visibility can cross tile boundaries. Keep full depth
            # only when the wave shader needs to locate an interior cut.
            full_view = SimpleNamespace(view_matrix=view, window_matrix=projection)
            full_depth = gpu_depth.draw(entry['depth'], full_view, (width,height), _RENDER_AREA)
            try:
                for group in groups:
                    mixed = bool(group['style']['params'][17,3])
                    wave = id(group) in wave_groups
                    if not (mixed or wave):
                        continue
                    if test_break is not None and test_break():
                        return None
                    info = (width, height, group['width'], perspective)
                    if wave:
                        wave_runs[id(group)] = gpu_chain.update_visibility(
                            group,pv,info,eye,direction,full_depth,_RENDER_AREA)
                    else:
                        normal_directions[id(group)] = gpu_chain.update_normal_directions(
                            group,pv,info,eye,direction,full_depth,_RENDER_AREA)
            finally:
                entry['depth']['areas'].pop(_RENDER_AREA, None)
                if not wave_groups:
                    full_depth = None
        rgba = np.empty((region_height, region_width, 4), np.float32) if tiled else None
        for y in range(y0, y1, step_y):
            for x in range(x0, x1, step_x):
                if test_break is not None and test_break():
                    return None
                right, top = min(x1, x + step_x), min(y1, y + step_y)
                left, bottom = max(0, x - margin), max(0, y - margin)
                size = (min(width, right + margin) - left,
                        min(height, top + margin) - bottom)
                if size != tile_size:
                    if offscreen is not None:
                        offscreen.free()
                        offscreen = None
                    offscreen = gpu.types.GPUOffScreen(*size, format='RGBA32F')
                    tile_size = size
                cropped = _tile_projection(projection, width, height, left, bottom, *size) if windowed else projection
                with offscreen.bind():
                    gpu.state.viewport_set(0, 0, *size)
                    gpu.state.scissor_set(0, 0, *size)
                    framebuffer = gpu.state.active_framebuffer_get()
                    framebuffer.clear(color=(0, 0, 0, 0), depth=1.)
                    tile_view = SimpleNamespace(view_matrix=view, window_matrix=cropped)
                    texture = gpu_depth.draw(entry['depth'], tile_view, size, _RENDER_AREA)
                    gpu.state.depth_test_set('NONE')
                    gpu.state.depth_mask_set(False)
                    gpu.state.blend_set('ALPHA')
                    shader.bind()
                    tile = (left, bottom, *size) if windowed else (0, 0, 0, 0)
                    for name, value in (('viewProjection', pv), ('eyeInfo', eye), ('viewExtra', tile)):
                        shader.uniform_float(name, value)
                    shader.uniform_sampler('sceneDepth', texture)
                    shader.uniform_sampler('objectDepth', texture)
                    last_object_depth = None
                    for group in groups:
                        streaming = id(group) in streamed_groups
                        if streaming:
                            if test_break is not None and test_break():
                                return None
                            preview._texture(group)
                        if group.get('object_depth') is not None and group['object_depth'] is not last_object_depth:
                            object_depth = preview._object_depth(group, tile_view, size, _RENDER_AREA)
                            shader.bind()
                            shader.uniform_sampler('objectDepth', object_depth)
                            last_object_depth = group['object_depth']
                        info = (width, height, group['width'], perspective)
                        visibility_data = group['chain_texture']
                        wave = id(group) in wave_groups
                        group_depth = full_depth if wave and windowed else texture
                        use_visibility = wave or bool(visibility[id(group)])
                        if wave and windowed:
                            visibility_data = wave_runs[id(group)]
                        elif use_visibility:
                            visibility_data = gpu_chain.update_visibility(group,pv,info,eye,direction,texture,_RENDER_AREA,(left,bottom))
                            shader.bind()
                        elif group['style']['params'][17,3]:
                            if windowed:
                                visibility_data = normal_directions[id(group)]
                            else:
                                visibility_data = gpu_chain.update_normal_directions(group,pv,info,eye,direction,texture,_RENDER_AREA)
                                shader.bind()
                        shader.uniform_sampler('sceneDepth', group_depth)
                        for name, key in stroke_samplers:
                            shader.uniform_sampler(name, group[key])
                        shader.uniform_sampler('visibilityData', visibility_data)
                        shader.uniform_sampler('jointData', group.get('joint_texture',group['chain_texture']))
                        wave_mask = group.get('chain_views',{}).get(_RENDER_AREA,{}).get('wave_mask')
                        shader.uniform_sampler('waveMask', wave_mask if wave_mask is not None else group['chain_texture'])
                        shader.uniform_float('directionInfo', (*direction[:3],2. if wave_mask is not None else float(use_visibility)))
                        shader.uniform_float('viewportInfo', info)
                        preview._batch.draw_instanced(shader, instance_count=len(group['records'])*(2 if group['style']['params'][17,3] else 1))
                        if streaming:
                            # The next draw binds its own textures. Keep only
                            # the current group's uploads resident for this tile.
                            for _, key in stroke_samplers:
                                del group[key]
                    w, h = right-x, top-y
                    buffer = framebuffer.read_color(x-left, y-bottom, w, h, 4, 0, 'FLOAT')
                    buffer.dimensions = w * h * 4
                    pixels = np.array(buffer).reshape(h, w, 4)
                    entry['render_tiles'] += 1
                    if not tiled:
                        return pixels
                    rgba[y-y0:top-y0, x-x0:right-x0] = pixels
        return rgba
    finally:
        if offscreen is not None:
            offscreen.free()
        entry['depth']['areas'].pop(_RENDER_AREA, None)
        entry.get('object_areas', {}).pop(_RENDER_AREA, None)
        gpu.state.depth_test_set(depth)
        gpu.state.depth_mask_set(mask)
        gpu.state.blend_set(blend)
        gpu.state.viewport_set(*viewport)
        gpu.state.scissor_set(*scissor)


class _LineEngine:
    def render(self, depsgraph):
        global LAST_RESULT, _captured_pixels
        from . import gpu_preview
        cpu = self.bl_idname == 'REA_PENCIL_CPU'
        if cpu:
            from . import cpu_render
        snapshot = cpu_render.snapshot if cpu else gpu_preview.snapshot
        raster = cpu_render.draw if cpu else draw
        start = time.perf_counter()
        scene = depsgraph.scene_eval
        LAST_RESULT = {'status': 'running', 'depsgraph_mode': depsgraph.mode}
        result = None
        active_view = self.active_view_get()
        try:
            if depsgraph.mode != 'RENDER':
                raise ValueError('GPU Line Render requires the render dependency graph')
            if not scene.camera:
                raise ValueError('GPU Line Render requires a scene camera')
            # Ink uses the current evaluated frame and pinhole projection.
            # The separate beauty render retains Blender's shutter and lens settings.
            stereo = scene.render.use_multiview
            if stereo and scene.render.views_format != 'STEREO_3D':
                raise ValueError('GPU Line Render: use the Stereo 3D views format for left and right eyes')
            views = ([view.name for view in scene.render.views if view.use and view.name in {'left','right'}]
                     if stereo else [''])
            if not views:
                raise ValueError('GPU Line Render: enable at least one stereo eye')
            if not scene.lea_line.tree:
                raise ValueError('Create a REA Pencil Line tree first')
            frame, bounds = _render_window(scene.render)
            self.update_stats('REA Pencil', 'Preparing render geometry')
            entry = snapshot(scene, depsgraph)
            prepared = time.perf_counter()
            if self.test_break():
                LAST_RESULT = {'status': 'cancelled'}
                return
            entry['test_break'] = self.test_break
            width, height = frame
            left, bottom, right, top = bounds
            output_size = (top-bottom,right-left) if scene.render.use_crop_to_border else (height,width)
            camera = scene.camera.evaluated_get(depsgraph)
            captures = {}
            tiles = 0
            alpha_mass = 0.
            draw_seconds = 0.
            cpu_prepare_seconds = cpu_raster_seconds = 0.
            for view in views:
                world, shift_x = None, None
                if stereo:
                    self.active_view_set(view)
                    world = self.camera_model_matrix(camera)
                    shift_x = self.camera_shift_x(camera)
                draw_start = time.perf_counter()
                rgba = raster(scene, depsgraph, entry, frame, bounds, world=world, shift_x=shift_x)
                draw_seconds += time.perf_counter()-draw_start
                if rgba is None or self.test_break():
                    LAST_RESULT = {'status': 'cancelled'}
                    return
                result = self.begin_result(0, 0, rgba.shape[1], rgba.shape[0], view=view)
                publish(result.layers[0], rgba)
                self.end_result(result)
                result = None
                tiles += entry['render_tiles']
                if cpu:
                    cpu_prepare_seconds += entry['cpu_prepare_seconds']
                    cpu_raster_seconds += entry['cpu_raster_seconds']
                alpha_mass += float(rgba[:,:,3].sum())
                if _capture_requested:
                    if rgba.shape[:2] != output_size:
                        padded = np.zeros((*output_size,4), dtype=np.float32)
                        padded[bottom:top,left:right] = rgba
                        captures[view] = padded
                    else:
                        captures[view] = rgba
            if _capture_requested:
                _captured_pixels = captures if stereo else captures['']
            LAST_RESULT = {'status': 'ok', 'depsgraph_mode': depsgraph.mode, 'line_backend': 'CPU' if cpu else 'GPU',
                'prepare_seconds': prepared-start + cpu_prepare_seconds,
                'engine_seconds': time.perf_counter()-start, 'edges': entry['edges'],
                'depth_triangles': entry['depth_triangles'] if cpu else len(entry['depth']['triangles']),
                'alpha_mass': alpha_mass, 'size': list(output_size),
                'render_window': list(bounds),
                'render_tiles': tiles, 'gpu_texture_limit': entry['gpu_texture_limit']}
            LAST_RESULT['CPU_raster_seconds' if cpu else 'GPU_draw_readback_seconds'] = (
                cpu_raster_seconds if cpu else draw_seconds)
            if stereo:
                LAST_RESULT['view_names'] = views
        except Exception as error:
            if result is not None:
                self.end_result(result, cancel=True)
            LAST_RESULT = {'status': 'error', 'message': str(error)}
            self.report({'ERROR'}, str(error))
        finally:
            self.active_view_set(active_view)


class REA_GPU_LINE_ENGINE(_LineEngine, bpy.types.RenderEngine):
    bl_idname = 'REA_PENCIL_GPU'
    bl_label = 'REA Pencil GPU Lines (Experimental)'
    bl_use_gpu_context = True
    bl_use_preview = False
    bl_use_postprocess = True
    bl_use_custom_freestyle = True


class REA_CPU_LINE_ENGINE(_LineEngine, bpy.types.RenderEngine):
    bl_idname = 'REA_PENCIL_CPU'
    bl_label = 'REA Pencil CPU Lines (Experimental)'
    bl_use_gpu_context = False
    bl_use_preview = False
    bl_use_postprocess = True
    bl_use_custom_freestyle = True


def register():
    bpy.utils.register_class(REA_GPU_LINE_ENGINE)
    bpy.utils.register_class(REA_CPU_LINE_ENGINE)
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
    bpy.utils.unregister_class(REA_CPU_LINE_ENGINE)
