"""Opt-in native Line Art final render, with camera-scaled stroke radii.

This is an alternative line algorithm, not a faster equivalent of Freestyle.
Only the add-on Render button opts in; standard F12 and animation stay unchanged.
"""
import time
import bpy
from mathutils import Matrix, Vector
from . import backend, routing, ui, viewport, stroke_bake, material_scope, brush_detail
from .width_profile import coverage_width


def unsupported(scene, specifications):
    if getattr(scene.lea_line,'preview_feature_brushes',False):
        return 'Mixed Brushes preview requires normal final rendering; disable it for experimental Line Art final rendering'
    if bpy.app.version[:2] != (4, 4) or scene.render.engine != 'BLENDER_EEVEE_NEXT':
        return 'Fast Line Art currently requires Blender 4.4 and Eevee'
    if scene != bpy.context.scene or len(scene.view_layers) != 1 or scene.render.use_multiview:
        return 'Fast Line Art requires the active scene, one view layer and one view'
    if not scene.camera or scene.camera.data.type not in {'ORTHO', 'PERSP'}:
        return 'Fast Line Art requires an orthographic or perspective camera'
    if scene.render.use_motion_blur or scene.camera.data.dof.use_dof:
        return 'Motion blur and depth of field require normal rendering'
    if scene.render.pixel_aspect_x != scene.render.pixel_aspect_y:
        return 'Non-square pixels require normal rendering'
    if scene.use_nodes and scene.node_tree and any(
            link.from_node.bl_idname == 'CompositorNodeRLayers' and link.from_socket.name == 'Freestyle'
            for link in scene.node_tree.links):
        return 'Compositing a separate Freestyle pass requires normal rendering'
    if any(ls.show_render and not ls.name.startswith(backend.PREFIX)
           for ls in scene.view_layers[0].freestyle_settings.linesets):
        return 'User-owned Freestyle line sets require normal rendering'
    for node, brush in specifications:
        from . import stamp_brush
        if stamp_brush.active(brush):return 'Stamp brushes require normal rendering'
        from . import stroke_variation
        if stroke_variation.snapshot(brush) or node.use_material_functions:return 'Stroke variation / Material Line Functions requires normal rendering'
        from . import texture_maps
        if texture_maps.maps(brush):return 'Texture Maps require normal final rendering'
        if getattr(brush, 'line_direction_enabled', False):
            return 'Open-chain direction currently applies to live preview only'
        if getattr(brush,'loop_start','KEEP')!='KEEP' or getattr(brush,'loop_orientation','KEEP')!='KEEP':
            return 'Explicit loop controls currently apply to live preview only'
        if material_scope.active(node):
            reason = material_scope.unsupported_reason(scene) or stroke_bake.ineligible_reason(scene)
            if reason:
                return reason
        if node.hidden:
            return 'Hidden-line compositing requires normal rendering'
        if node.visible and node.outline != node.border:
            return 'Separate outline and open-border selection requires normal rendering'
        if routing.any_enabled(node, 'ridge_valley') or routing.any_enabled(node, 'suggestive_contour'):
            return 'Ridge/valley and suggestive contours require normal rendering'
        if any(socket.is_linked for socket in node.inputs if socket.name not in {'Brush', 'Hidden Brush'}):
            return 'Per-feature brush overrides require normal rendering'
        if getattr(brush, 'size_curve_enabled', False) or getattr(brush, 'alpha_curve_enabled', False):
            return 'Stroke size and alpha curves require normal rendering'
        if any(getattr(brush, name, False) for name in ('dashed', 'taper', 'distance_fade', 'distance_alpha')) or brush.caps != 'ROUND':
            return 'Dash, taper, distance modifiers and non-round caps require normal rendering'
        if brush_detail.stroke_geometry_enabled(brush):
            return 'Brush Detail stroke filtering, offsets, extension and distortion require normal rendering'
    return None


def _camera_parameters(scene):
    # Evaluate the camera before creating Line Art objects. Asking for a fresh
    # viewport depsgraph afterwards would calculate every stroke once here and
    # then again in Blender's separate render depsgraph.
    camera = scene.camera.evaluated_get(bpy.context.evaluated_depsgraph_get())
    frame = camera.data.view_frame(scene=scene)
    return {'type': camera.data.type, 'vertical': max(p.y for p in frame) - min(p.y for p in frame),
            'frame_z': abs(frame[0].z), 'matrix': camera.matrix_world.copy(), 'clip_start': camera.data.clip_start}


def _build_pixel_radius_group(graph, scene, width, camera):
    """Radius = half the pixel width times world units per camera pixel.

    Generated Line Art objects have identity transforms. Perspective radii use
    each point's camera-space depth, so near/far strokes retain the same width.
    """
    graph.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    graph.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    nodes, links = graph.nodes, graph.links
    source, output = nodes.new('NodeGroupInput'), nodes.new('NodeGroupOutput')
    radius = nodes.new('GeometryNodeSetCurveRadius')
    links.new(source.outputs['Geometry'], radius.inputs['Curve'])
    links.new(radius.outputs['Curve'], output.inputs['Geometry'])
    height = max(1, int(scene.render.resolution_y * scene.render.resolution_percentage / 100))
    factor = width * .5 * camera['vertical'] / height
    if camera['type'] == 'ORTHO':
        radius.inputs['Radius'].default_value = factor
    else:
        factor /= camera['frame_z']
        position = nodes.new('GeometryNodeInputPosition')
        depth = nodes.new('ShaderNodeVectorMath')
        depth.operation = 'DOT_PRODUCT'
        forward = (camera['matrix'].to_quaternion() @ Vector((0, 0, -1))).normalized()
        depth.inputs[1].default_value = forward
        links.new(position.outputs['Position'], depth.inputs[0])
        offset = nodes.new('ShaderNodeMath')
        offset.operation = 'ADD'
        offset.inputs[1].default_value = -camera['matrix'].translation.dot(forward)
        links.new(depth.outputs['Value'], offset.inputs[0])
        positive = nodes.new('ShaderNodeMath')
        positive.operation = 'MAXIMUM'
        positive.inputs[1].default_value = camera['clip_start']
        links.new(offset.outputs[0], positive.inputs[0])
        scale = nodes.new('ShaderNodeMath')
        scale.operation = 'MULTIPLY'
        scale.inputs[1].default_value = factor
        links.new(positive.outputs[0], scale.inputs[0])
        links.new(scale.outputs[0], radius.inputs['Radius'])
    return graph


def _pixel_radius_group(scene, width, camera=None):
    graph = bpy.data.node_groups.new('.rea pencil render pixel width', 'GeometryNodeTree')
    graph['rea_pixel_width_owned'] = True
    try:
        return _build_pixel_radius_group(graph, scene, width, camera or _camera_parameters(scene))
    except Exception:
        bpy.data.node_groups.remove(graph)
        raise


def _invoke_render(scene, write_still):
    return bpy.ops.render.render(write_still=write_still, scene=scene.name)


def render(scene, write_still=False):
    started = time.perf_counter()
    warnings = backend.sync_scene(scene)
    specifications = backend._specifications(scene.lea_line.tree)
    reason = unsupported(scene, specifications)
    if reason:
        _invoke_render(scene, write_still)
        return {'mode': 'FREESTYLE_FALLBACK', 'seconds': time.perf_counter() - started,
                'warnings': [reason] + warnings}
    old_auto = scene.lea_line.auto_sync
    old_freestyle = scene.render.use_freestyle
    old_antialias = scene.grease_pencil_settings.antialias_threshold
    keep_preview = scene.lea_line.preview_enabled
    modifiers, groups, objects, material_nodes = [], [], [], {}
    try:
        with ui.suspend_sync():
            scene.lea_line.auto_sync = False
        if getattr(scene.lea_line, 'line_art_aa', 'SCENE') == 'CRISP':
            scene.grease_pencil_settings.antialias_threshold = 0
        camera = _camera_parameters(scene)
        viewport.sync_preview(scene, material_render=True)
        owner = scene.get(viewport.OWNER_SCENE_KEY)
        for obj in scene.objects:
            if not viewport._owned(obj, owner):
                continue
            key = obj.get(viewport.KEY)
            index, visibility = key.split(':')
            if visibility != 'VISIBLE':
                continue
            node, brush = specifications[int(index)]
            if material_scope.active(node):
                material_nodes[obj.name] = node
            objects.append((obj, obj.hide_render, obj.matrix_world.copy()))
            obj.matrix_world = Matrix.Identity(4)
            obj.hide_render = False
            width = coverage_width(brush.thickness * backend.width_scale(scene, node),
                                   getattr(scene.lea_line, 'stroke_width_profile', 'PHYSICAL'))
            group = _pixel_radius_group(scene, width, camera)
            groups.append(group)
            modifier = obj.modifiers.new('.rea pencil render pixel width', 'NODES')
            modifier.node_group = group
            modifiers.append((obj, modifier))
        scene.render.use_freestyle = False
        with stroke_bake.prepared(scene, [obj for obj, _, _ in objects], material_nodes) as preparation:
            if material_nodes and not preparation['used']:
                raise RuntimeError('Material stroke preparation unavailable: ' + str(preparation['reason']))
            _invoke_render(scene, write_still)
        return {'mode': 'LINE_ART', 'seconds': time.perf_counter() - started, 'stroke_preparation': preparation,
                'warnings': ['Fast Line Art uses different edge classification and antialiasing from Freestyle and Pencil+.']}
    finally:
        for obj, modifier in modifiers:
            obj.modifiers.remove(modifier)
        for group in groups:
            if group.users == 0:
                bpy.data.node_groups.remove(group)
        for obj, hide_render, matrix in objects:
            obj.hide_render = hide_render
            obj.matrix_world = matrix
        scene.render.use_freestyle = old_freestyle
        scene.grease_pencil_settings.antialias_threshold = old_antialias
        with ui.suspend_sync():
            scene.lea_line.auto_sync = old_auto
        if not keep_preview:
            viewport.clear_preview(scene)
        else:
            viewport.sync_preview(scene)
