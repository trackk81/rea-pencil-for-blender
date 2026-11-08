"""Opt-in final line-image reuse for unchanged mesh geometry and line settings.

The cache never skips base shading. Unsupported compositor/view-layer layouts
fall back to a normal render. A new evaluated-geometry fingerprint is computed
on every request; no transform/brush/camera invalidation is inferred from timers.
"""
import hashlib
import json
import tempfile
import time
from pathlib import Path
import bpy
import numpy as np
from . import backend, ui

_entry = None


def clear():
    global _entry
    if _entry:
        image = _entry.get('image')
        try:
            if image and image.name in bpy.data.images and image.users == 0:
                bpy.data.images.remove(image)
        except ReferenceError:
            pass
    _entry = None


def _unsupported(scene):
    if scene != bpy.context.scene or bpy.context.view_layer not in tuple(scene.view_layers):
        return 'Line cache requires the active scene and view layer'
    if scene.camera is None or scene.camera.type != 'CAMERA':
        return 'Line cache requires a scene camera'
    if not scene.lea_line.tree:
        return 'Line cache requires a line graph'
    from . import texture_maps
    if any(texture_maps.maps(n) for n in scene.lea_line.tree.nodes if n.bl_idname=='LEABrushNode'):
        return 'Texture Map image edits require normal rendering; static line cache bypassed'
    from . import object_uv
    if object_uv.uses(scene):return 'Surface/reference snapshots require normal rendering; static line cache bypassed'
    if scene.render.engine != 'BLENDER_EEVEE_NEXT':
        return 'Line cache has been validated for Eevee only'
    if scene.render.use_motion_blur:
        return 'Motion-blurred scenes require normal rendering'
    if len(scene.view_layers) != 1 or scene.render.use_multiview:
        return 'Line cache requires one view layer and one render view'
    if not scene.view_layers[0].use:
        return 'Line cache requires an enabled render view layer'
    if not scene.render.use_compositing:
        return 'Line cache needs compositor output enabled'
    if scene.render.use_sequencer and scene.sequence_editor and len(scene.sequence_editor.strips):
        return 'Sequencer output requires normal rendering'
    if scene.render.use_simplify:
        return 'Render simplification can differ from evaluated viewport geometry'
    if any(obj.type not in {'MESH', 'LIGHT', 'CAMERA', 'EMPTY', 'GREASEPENCIL'} for obj in scene.objects if not obj.hide_render):
        return 'Line cache currently supports mesh scenes only'
    if any(obj.type == 'GREASEPENCIL' and not obj.hide_render for obj in scene.objects):
        return 'Rendered Grease Pencil objects require a normal render'
    # This fingerprint uses the viewport depsgraph. Do not assume it contains
    # objects hidden only in the viewport or the render variant of modifiers.
    def hidden_collection(collection):
        if collection.hide_viewport and not collection.hide_render:
            return True
        return any(hidden_collection(child) for child in collection.children)
    if hidden_collection(scene.collection):
        return 'Viewport-hidden render collections require normal rendering'
    def hidden_layer_collection(collection):
        if collection.exclude:
            return False
        return collection.hide_viewport or any(hidden_layer_collection(child) for child in collection.children)
    if hidden_layer_collection(scene.view_layers[0].layer_collection):
        return 'Viewport-hidden layer collections require normal rendering'
    def inspect_object(obj):
        if obj.hide_render:
            return None
        if obj.type not in {'MESH', 'LIGHT', 'CAMERA', 'EMPTY'}:
            return 'Non-mesh instances require normal rendering'
        if obj.hide_viewport:
            return 'Viewport-hidden render objects require normal rendering'
        original = obj.original
        if scene.objects.get(original.name) == original and original.hide_get(view_layer=scene.view_layers[0]):
            return 'Temporarily hidden render objects require normal rendering'
        if getattr(obj, 'particle_systems', None):
            return 'Particle and hair geometry requires normal rendering'
        for modifier in obj.modifiers:
            if modifier.show_render != modifier.show_viewport:
                return 'Different viewport/render modifier visibility requires normal rendering'
            if not modifier.show_render:
                continue
            if modifier.type == 'NODES':
                return 'Geometry Nodes may contain unobserved render-only components'
            if modifier.type in {'SUBSURF', 'MULTIRES'} and modifier.levels != modifier.render_levels:
                return 'Different viewport/render subdivision requires normal rendering'
        return None
    checked_collections = set()
    def inspect_collection(collection):
        if collection in checked_collections:
            return None
        checked_collections.add(collection)
        if collection.hide_viewport and not collection.hide_render:
            return 'Viewport-hidden instance collections require normal rendering'
        for obj in collection.objects:
            reason = inspect_object(obj)
            if reason:
                return reason
            if obj.instance_type == 'COLLECTION' and obj.instance_collection:
                reason = inspect_collection(obj.instance_collection)
                if reason:
                    return reason
        for child in collection.children:
            reason = inspect_collection(child)
            if reason:
                return reason
        return None
    reason = inspect_collection(scene.collection)
    if reason:
        return reason
    bpy.context.view_layer.update()
    for instance in bpy.context.evaluated_depsgraph_get().object_instances:
        reason = inspect_object(instance.object)
        if reason:
            return reason
    if scene.node_tree and any(node.bl_idname not in {'CompositorNodeRLayers', 'CompositorNodeComposite'} for node in scene.node_tree.nodes):
        return 'Custom compositor detected; using normal rendering'
    if scene.node_tree:
        layers = [n for n in scene.node_tree.nodes if n.bl_idname == 'CompositorNodeRLayers']
        if len(layers) > 1 or any(n.scene is not None and n.scene != scene for n in layers):
            return 'Cross-scene compositor requires normal rendering'
        if scene.use_nodes:
            composites = [n for n in scene.node_tree.nodes if n.bl_idname == 'CompositorNodeComposite']
            if len(layers) != 1 or len(composites) != 1:
                return 'Line cache requires one direct Render Layers to Composite path'
            image = composites[0].inputs['Image']
            if (not image.is_linked or image.links[0].from_node != layers[0]
                    or image.links[0].from_socket.name != 'Image'
                    or layers[0].layer not in {'', scene.view_layers[0].name}):
                return 'Custom compositor routing requires normal rendering'
    for line_set in scene.view_layers[0].freestyle_settings.linesets:
        if not line_set.show_render:
            continue
        if not line_set.name.startswith(backend.PREFIX):
            return 'User-owned Freestyle line sets require normal rendering'
        style = line_set.linestyle
        if style.use_nodes or (style.use_texture and any(slot and slot.texture for slot in style.texture_slots)) or len(style.color_modifiers):
            return 'Custom native line shaders require normal rendering'
        if any(not modifier.name.startswith(backend.PREFIX) for modifier in list(style.thickness_modifiers) + list(style.alpha_modifiers)):
            return 'Custom native thickness or alpha modifiers require normal rendering'
        if any(modifier.type not in {'ALONG_STROKE', 'DISTANCE_FROM_CAMERA'} for modifier in style.thickness_modifiers):
            return 'Unsupported native thickness modifier requires normal rendering'
        if any(modifier.type != 'DISTANCE_FROM_CAMERA' and not (
                modifier.type == 'ALONG_STROKE' and modifier.name == backend.PREFIX + 'Stroke Alpha')
                for modifier in style.alpha_modifiers):
            return 'Unsupported native alpha modifier requires normal rendering'
        detail_geometry = {'SAMPLING', 'BACKBONE_STRETCHER', '2D_OFFSET', 'SINUS_DISPLACEMENT'}
        if any(modifier.type != 'SAMPLING' and not (
                modifier.name.startswith(backend.PREFIX + 'Detail ') and modifier.type in detail_geometry)
                for modifier in style.geometry_modifiers):
            return 'Custom native stroke geometry requires normal rendering'
    return None


def _scalars(block):
    """Serialize RNA values, including native fields not owned by our UI."""
    result = {}
    for prop in block.bl_rna.properties:
        if prop.is_readonly or prop.type not in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
            continue
        value = getattr(block, prop.identifier)
        if getattr(prop, 'is_array', False):
            value = list(value)
        elif isinstance(value, set):
            value = sorted(value)
        result[prop.identifier] = value
    return result


def _modifier_settings(modifier):
    result = _scalars(modifier)
    curve = getattr(modifier, 'curve', None)
    if curve is not None:
        result['curve'] = [_scalars(curve), [
            [_scalars(point) for point in channel.points] for channel in curve.curves]]
    return result


def fingerprint(scene):
    if scene != bpy.context.scene or bpy.context.view_layer not in tuple(scene.view_layers):
        raise ValueError('Cannot fingerprint a different scene with the active depsgraph')
    if scene.camera is None:
        raise ValueError('Cannot fingerprint lines without a scene camera')
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    digest = hashlib.sha256()
    def add(value):
        data = json.dumps(value, sort_keys=True, default=str).encode()
        digest.update(len(data).to_bytes(8, 'little'))
        digest.update(data)
    add(['line-cache-v04', scene.as_pointer(), bpy.context.view_layer.as_pointer(),
         scene.frame_current, scene.frame_subframe])
    render = scene.render
    add({name: getattr(render, name) for name in ('resolution_x', 'resolution_y', 'resolution_percentage',
        'pixel_aspect_x', 'pixel_aspect_y', 'use_border', 'use_crop_to_border',
        'border_min_x', 'border_max_x', 'border_min_y', 'border_max_y',
        'filter_size', 'film_transparent', 'use_compositing', 'use_sequencer')})
    # The engine may change line-pass antialiasing even with identical geometry.
    add(_scalars(scene.eevee))
    camera = scene.camera.evaluated_get(depsgraph)
    add([list(row) for row in camera.matrix_world])
    add({name: getattr(camera.data, name) for name in ('type', 'lens', 'ortho_scale', 'shift_x', 'shift_y', 'clip_start', 'clip_end', 'sensor_fit', 'sensor_width', 'sensor_height')})
    add([scene.lea_line.width_mode, scene.lea_line.reference_height])
    fs = scene.view_layers[0].freestyle_settings
    add({prop.identifier: getattr(fs, prop.identifier) for prop in fs.bl_rna.properties
         if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'ENUM'} and not prop.is_readonly})
    for line_set in fs.linesets:
        if line_set.show_render:
            style = line_set.linestyle
            add(_scalars(line_set))
            add(_scalars(style))
            add([[_modifier_settings(modifier) for modifier in modifiers] for modifiers in
                 (style.geometry_modifiers, style.thickness_modifiers, style.alpha_modifiers)])
    tree = scene.lea_line.tree
    for node in tree.nodes:
        values = {'type': node.bl_idname, 'name': node.name}
        for prop in node.bl_rna.properties:
            name = prop.identifier
            if prop.is_readonly or name in {
                    'rna_type', 'location', 'dimensions', 'parent', 'color',
                    'select', 'hide', 'width', 'height', 'label', 'use_custom_color',
                    'show_options', 'show_preview', 'show_texture',
                    'line_visibility', 'target_tab', 'object_index', 'material_index', 'line_set_index',
                    'show_targets', 'show_visible_settings', 'show_hidden_settings', 'show_overrides'}:
                continue
            if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
                value = getattr(node, name)
                values[name] = list(value) if getattr(prop, 'is_array', False) else value
        if node.bl_idname == 'LEABrushNode':
            values['brush_color'] = list(node.color)
        if node.bl_idname == 'LEABrushDetailNode':
            from . import curves
            payload = curves.export_detail(node)
            values['stroke_curves'] = {kind: data for kind, data in payload.items()
                                      if node.enabled and getattr(node, kind.lower() + '_curve_enabled')}
        if node.bl_idname == 'LEALineSetNode':
            scope = backend.source_collection(scene, node)
            values['objects'] = sorted(obj.name_full for obj in scope.all_objects) if scope else None
            values['explicit_objects'] = [(item.object.name_full, item.object.as_pointer()) if item.object else None for item in node.objects]
            values['material_targets'] = [(item.material.name_full, item.material.as_pointer()) if item.material else None for item in node.materials]
        add(values)
    add(sorted((link.from_node.name, link.from_socket.name, link.to_node.name, link.to_socket.name) for link in tree.links))
    layer = scene.view_layers[0]
    add(layer.material_override.name_full if layer.material_override else None)
    def collections(collection):
        return [collection.name, collection.exclude, collection.holdout, collection.indirect_only,
                [collections(child) for child in collection.children]]
    add(collections(layer.layer_collection))
    for instance in depsgraph.object_instances:
        obj = instance.object
        if obj.type != 'MESH' or obj.hide_render:
            continue
        add([obj.original.name_full, obj.original.as_pointer(), [list(row) for row in instance.matrix_world],
             bool(instance.show_self), obj.visible_camera, obj.is_holdout, obj.is_shadow_catcher])
        mesh = obj.to_mesh()
        try:
            for elements, attr, count, dtype in ((mesh.vertices, 'co', 3, np.float32),
                    (mesh.edges, 'vertices', 2, np.int32),
                    (mesh.loops, 'vertex_index', 1, np.int32), (mesh.loops, 'edge_index', 1, np.int32),
                    (mesh.polygons, 'loop_start', 1, np.int32), (mesh.polygons, 'loop_total', 1, np.int32),
                    (mesh.polygons, 'material_index', 1, np.int32), (mesh.polygons, 'use_smooth', 1, np.bool_),
                    (mesh.polygons, 'use_freestyle_mark', 1, np.bool_),
                    (mesh.edges, 'use_freestyle_mark', 1, np.bool_)):
                data = np.empty(len(elements) * count, dtype=dtype)
                elements.foreach_get(attr, data)
                add([attr, len(elements), count, np.dtype(dtype).str])
                digest.update(data.tobytes())
            add(mesh.has_custom_normals)
            add([(slot.material.name_full, slot.material.as_pointer()) if slot.material else None for slot in obj.material_slots])
            if mesh.has_custom_normals:
                normals = np.empty(len(mesh.corner_normals) * 3, dtype=np.float32)
                mesh.corner_normals.foreach_get('vector', normals)
                digest.update(normals.tobytes())
            add([(material.name_full, material.as_pointer()) if material else None for material in mesh.materials])
        finally:
            obj.to_mesh_clear()
    return digest.hexdigest()


def render(scene, write_still=False):
    """Render synchronously; return cache hit/miss plus exact measured scope."""
    global _entry
    started = time.perf_counter()
    backend.sync_scene(scene)
    reason = _unsupported(scene)
    if reason:
        bpy.ops.render.render(write_still=write_still, scene=scene.name)
        return {'cache': 'unsupported', 'reason': reason, 'seconds': time.perf_counter() - started}
    key = fingerprint(scene)
    try:
        hit = bool(_entry and _entry['key'] == key and _entry['image'].name in bpy.data.images)
    except ReferenceError:
        clear()
        hit = False
    old_auto = scene.lea_line.auto_sync
    old_nodes = scene.use_nodes
    old_freestyle = scene.render.use_freestyle
    fs = scene.view_layers[0].freestyle_settings
    old_pass = fs.as_render_pass
    created = []
    old_link = None
    composite = None
    try:
        with ui.suspend_sync():
            scene.lea_line.auto_sync = False
        scene.use_nodes = True
        graph = scene.node_tree
        composite = next((n for n in graph.nodes if n.bl_idname == 'CompositorNodeComposite'), None)
        if composite is None:
            composite = graph.nodes.new('CompositorNodeComposite')
            created.append(composite)
        if composite.inputs['Image'].is_linked:
            link = composite.inputs['Image'].links[0]
            old_link = (link.from_socket, link.to_socket)
        layers = graph.nodes.new('CompositorNodeRLayers')
        created.append(layers)
        layers.layer = scene.view_layers[0].name
        alpha = graph.nodes.new('CompositorNodeAlphaOver')
        created.append(alpha)
        graph.links.new(layers.outputs['Image'], alpha.inputs[1])
        graph.links.new(alpha.outputs[0], composite.inputs['Image'])
        if hit:
            scene.render.use_freestyle = False
            image_node = graph.nodes.new('CompositorNodeImage')
            created.append(image_node)
            image_node.image = _entry['image']
            graph.links.new(image_node.outputs['Image'], alpha.inputs[2])
            bpy.ops.render.render(write_still=write_still)
        else:
            scene.render.use_freestyle = True
            fs.as_render_pass = True
            graph.links.new(layers.outputs['Freestyle'], alpha.inputs[2])
            with tempfile.TemporaryDirectory(prefix='rea_pencil_lines_') as directory:
                output = graph.nodes.new('CompositorNodeOutputFile')
                created.append(output)
                output.base_path = directory
                output.format.file_format = 'OPEN_EXR'
                output.format.color_mode = 'RGBA'
                output.format.color_depth = '32'
                output.file_slots[0].path = 'lines_'
                graph.links.new(layers.outputs['Freestyle'], output.inputs[0])
                bpy.ops.render.render(write_still=write_still)
                files = list(Path(directory).glob('*.exr'))
                if len(files) != 1:
                    raise RuntimeError('Expected one completed Freestyle pass image')
                image = bpy.data.images.load(str(files[0]), check_existing=False)
                image.name = '.rea pencil cached line pass'
                image.pack()
                clear()
                _entry = {'key': key, 'image': image}
        return {'cache': 'hit' if hit else 'miss', 'key': key, 'seconds': time.perf_counter() - started}
    finally:
        for node in reversed(created):
            if node.name in scene.node_tree.nodes:
                scene.node_tree.nodes.remove(node)
        if old_link and composite and composite.name in scene.node_tree.nodes:
            scene.node_tree.links.new(*old_link)
        fs.as_render_pass = old_pass
        scene.render.use_freestyle = old_freestyle
        scene.use_nodes = old_nodes
        with ui.suspend_sync():
            scene.lea_line.auto_sync = old_auto
