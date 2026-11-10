"""Translate LEA nodes into Blender's native Freestyle line renderer.

No Pencil+ binary, Render App, or copied implementation is used for rendering.
"""
import time
import math
import uuid
import threading
import bpy
from bpy.app.handlers import persistent
from . import routing, material_scope, freestyle_material, brush_detail, curves, object_uv

PREFIX = "LEA::"
_syncing = False
_render_started = {}
_last_sync_writes = 0


class LineSetContext:
    """Read-only per-output settings; shared Line Sets never get mutated."""
    def __init__(self, source, output):
        self.source = source
        self.line_output = output

    def __getattr__(self, name):
        if name=='random_seed':return self.line_output.random_seed
        return getattr(self.source, name)

    def __getitem__(self, name):
        return self.source[name]

    def __setitem__(self, name, value):
        # Backend-owned source collection IDs retain the existing source scope.
        self.source[name] = value


def _set(owner, name, value):
    """Avoid redundant RNA writes that invalidate Blender render caches."""
    global _last_sync_writes
    current = getattr(owner, name)
    if isinstance(value, (tuple, list)):
        same = len(current) == len(value) and all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6) for a, b in zip(current, value))
    elif isinstance(value, float):
        same = math.isclose(current, value, rel_tol=1e-6, abs_tol=1e-6)
    else:
        same = current == value
    if not same:
        setattr(owner, name, value)
        _last_sync_writes += 1
    return not same


def last_sync_writes():
    return _last_sync_writes


def ensure_default_tree(scene, create_detail=False):
    settings = scene.lea_line
    if settings.tree and settings.tree.bl_idname == "LEALineTree":
        from .ui import upgrade_tree
        upgrade_tree(settings.tree)
        curves.upgrade_tree(settings.tree)
        return settings.tree
    tree = bpy.data.node_groups.new("rea pencil for blender", "LEALineTree")
    output = tree.nodes.new("LEAOutputNode")
    output.location = (420, 80)
    line_set = tree.nodes.new("LEALineSetNode")
    line_set.location = (0, 80)
    brush = tree.nodes.new("LEABrushNode")
    brush.location = (-300, 80)
    if create_detail:
        from .brush_detail import initialize_from_brush
        detail = tree.nodes.new('LEABrushDetailNode')
        detail.location = (-560, 80)
        initialize_from_brush(detail, brush)
        tree.links.new(detail.outputs[0], brush.inputs['Brush Detail'])
    tree.links.new(brush.outputs[0], line_set.inputs["Brush"])
    tree.links.new(line_set.outputs[0], output.inputs[0])
    settings.tree = tree
    return tree


def _specifications(tree, *, with_output=False):
    """Only output-connected sets render; disconnected drafts remain drafts."""
    outputs = sorted((n for n in tree.nodes if n.bl_idname == "LEAOutputNode" and n.enabled),
                     key=lambda n: n.render_priority)
    if not outputs:
        return []
    result = []
    for output in outputs:
        group = []
        seen = set()
        for socket in output.inputs:
            for link in socket.links:
                node = link.from_node
                if node.bl_idname != "LEALineSetNode":
                    raise ValueError("Line Output accepts Line Set nodes only")
                if node.name in seen or not node.enabled:
                    continue
                seen.add(node.name)
                if (getattr(node, 'target_mode', 'ALL') == 'LISTED' and not node.source_collection
                        and not any(item.object for item in node.objects)
                        and not any(item.material for item in node.materials)):
                    continue
                brush_socket = node.inputs.get("Brush")
                if not brush_socket or not brush_socket.is_linked:
                    raise ValueError(f"{node.name}: connect a Brush node")
                brush = routing.connected_brush(node, 'Brush')
                brushes = [brush]
                for socket in node.inputs:
                    if socket.is_linked and not socket.name.endswith(' Reduction'):
                        brushes.append(routing.connected_brush(node, socket.name))
                for candidate in brushes:
                    for visibility, enabled in (('VISIBLE', node.visible), ('HIDDEN', node.hidden)):
                        if not enabled: continue
                        effective = routing.line_brush(node, candidate, visibility)
                        if (effective.distance_fade or effective.distance_alpha) and effective.distance_end <= effective.distance_start:
                            raise ValueError('Distance End must be greater than Distance Start')
                from . import object_uv
                object_uv.validate_visibility(node,brush)
                if with_output or output.width_mode != 'INHERIT' or output.random_seed:
                    node = LineSetContext(node, output)
                group.append((node, brush))
        result.extend(sorted(group, key=lambda pair: getattr(pair[0], 'render_priority', 0)))
    return result


def source_collection(scene, node):
    """Resolve explicit objects through a private collection, without editing meshes."""
    if material_scope.active(node):
        # Object targets and material faces are ORed by the per-edge predicate.
        return node.source_collection
    targets = getattr(node, 'objects', ())
    if not len(targets) and (getattr(node, 'target_mode', 'ALL') == 'ALL' or node.source_collection):
        return node.source_collection
    scene_id = scene.get('rea_targets_owner')
    if not scene_id or any(s != scene and s.get('rea_targets_owner') == scene_id for s in bpy.data.scenes):
        scene_id = uuid.uuid4().hex
        scene['rea_targets_owner'] = scene_id
    node_id = node.get('rea_targets_node')
    if not node_id:
        node_id = uuid.uuid4().hex
        node['rea_targets_node'] = node_id
    key = scene_id + ':' + node_id
    collection = next((c for c in bpy.data.collections if c.get('rea_targets_key') == key), None)
    if collection is None:
        collection = bpy.data.collections.new('rea pencil targets ' + node_id[:8])
        collection['rea_targets_key'] = key
        collection['rea_targets_owner'] = scene_id
    selected = {target.object for target in targets if target.object and scene.objects.get(target.object.name) == target.object}
    if node.source_collection:
        selected.intersection_update(node.source_collection.all_objects)
    existing = set(collection.objects)
    for obj in existing - selected:
        collection.objects.unlink(obj)
    for obj in selected - existing:
        collection.objects.link(obj)
    return collection


def cleanup_target_collections(scene, active=None):
    owner = scene.get('rea_targets_owner')
    if owner is None:
        return
    for collection in list(bpy.data.collections):
        if collection.get('rea_targets_owner') == owner and (active is None or collection not in active):
            # A collection still used by a native preview/line set stays alive.
            if collection.users == 0:
                bpy.data.collections.remove(collection)


def sync_scene(scene, tree=None):
    global _syncing, _last_sync_writes
    if _syncing:
        return []
    tree = tree or scene.lea_line.tree
    if tree is None:
        return []
    from .ui import upgrade_tree
    upgrade_tree(tree)
    curves.upgrade_tree(tree)
    specifications = _specifications(tree)  # Validate before touching render settings.
    freestyle_material.validate(scene, specifications)
    effective_brushes = []
    warnings=[]
    from . import texture_maps
    for node, brush in specifications:
        warnings.extend(texture_maps.validate(brush,scene,node))
        effective_brushes.append(brush)
        brush_detail.validate_scaled_brush(brush, width_scale(scene, node))
        for socket in node.inputs:
            if socket.is_linked and not socket.name.endswith(' Reduction'):
                linked_brush = routing.connected_brush(node, socket.name)
                effective_brushes.append(linked_brush)
                warnings.extend(texture_maps.validate(linked_brush,scene,node))
                brush_detail.validate_scaled_brush(linked_brush, width_scale(scene, node))
    if any(getattr(brush,'loop_start','KEEP')!='KEEP' or getattr(brush,'loop_orientation','KEEP')!='KEEP' or getattr(brush,'dash_phase',0)!=0 for _,brush in specifications):
        warnings.append('Loop and dash-phase controls apply to live preview; Freestyle final chaining is independent.')
    if any(getattr(brush, 'line_direction_enabled', False) for brush in effective_brushes):
        warnings.append('Open-chain direction applies to live preview only; Freestyle final chaining is independent.')
    angles = {round(node.crease_angle, 6) for node, _ in specifications if routing.any_enabled(node, 'crease')}
    if len(angles) > 1:
        raise ValueError("Freestyle shares one crease angle per view layer; use the same angle in all Line Sets")
    if scene.render.engine not in {"BLENDER_EEVEE_NEXT", "CYCLES"}:
        raise ValueError("rea pencil for blender requires Eevee or Cycles; select a supported render engine")
    resolved_collections = {node.name: source_collection(scene, node) for node, _ in specifications}
    _syncing = True
    _last_sync_writes = 0
    try:
        _set(scene.render, 'use_freestyle', True)
        _set(scene.render, 'line_thickness', 1.0)
        _set(scene.render, 'line_thickness_mode', "ABSOLUTE")
        for layer in scene.view_layers:
            fs = layer.freestyle_settings
            _set(fs, 'mode', "EDITOR")
            _set(fs, 'use_culling', False)
            _set(fs, 'use_material_boundaries', any(material_scope.active(node) or routing.any_enabled(node, 'material_boundary') for node, _ in specifications))
            _set(fs, 'use_ridges_and_valleys', any(routing.any_enabled(node, 'ridge_valley') for node, _ in specifications))
            _set(fs, 'use_suggestive_contours', any(routing.any_enabled(node, 'suggestive_contour') for node, _ in specifications))
            _set(fs, 'use_smoothness', True)
            _set(fs, 'use_view_map_cache', True)
            if angles:
                _set(fs, 'crease_angle', next(iter(angles)))
            desired = set()
            for index, (node, brush) in enumerate(specifications):
                if routing.any_enabled(node, 'intersection'):
                    warnings.append(f"{node.name}: intersection lines are not supported by this backend")
                if routing.any_enabled(node, 'wireframe'):
                    warnings.append(f"{node.name}: Wireframe uses GPU preview or GPU render.")
                if routing.any_enabled(node, 'smoothing_boundary'):
                    warnings.append(f"{node.name}: Smoothing Boundary uses GPU preview or GPU render.")
                if routing.any_enabled(node, 'object_edge'):
                    warnings.append(f"{node.name}: Object contours use GPU preview or GPU render.")
                for visibility, enabled in (("VISIBLE", node.visible), ("HIDDEN", node.hidden)):
                    if not enabled:
                        continue
                    for job in routing.jobs(node, brush, visibility):
                        name = f"{PREFIX}{index}:{visibility}" + job['suffix']
                        desired.add(name)
                        ls = fs.linesets.get(name) or fs.linesets.new(name)
                        _sync_line_set(scene, ls, node, visibility, job, resolved_collections[node.name])
            # Preserve user-owned line sets, but turn off Blender's untouched default
            # once on initial activation. Never delete unrelated user configuration.
            if not layer.get("lea_initialized", False):
                default = fs.linesets.get("LineSet")
                if default:
                    default.show_render = False
                layer["lea_initialized"] = True
            for ls in list(fs.linesets):
                if ls.name.startswith(PREFIX) and ls.name not in desired:
                    style = ls.linestyle
                    fs.linesets.remove(ls)
                    if style and style.users == 0:
                        bpy.data.linestyles.remove(style)
        cleanup_target_collections(scene, set(resolved_collections.values()))
        curves.mark_synced(scene)
        object_uv.request_prepare(scene)
        return list(dict.fromkeys(warnings))
    finally:
        _syncing = False


def width_scale(scene, node=None):
    settings = scene.lea_line
    output = getattr(node, 'line_output', None)
    if output and output.width_mode != 'INHERIT':
        return (scene.render.resolution_y * scene.render.resolution_percentage / 100 / 480
                if output.width_mode == 'RELATIVE' else 1.)
    if settings.width_mode == 'RELATIVE':
        height = scene.render.resolution_y * scene.render.resolution_percentage / 100
        return height / settings.reference_height
    return 1.0


def _sync_line_set(scene, ls, node, visibility, job, collection):
    _set(ls, 'show_render', bool(job['include']))
    _set(ls, 'select_by_visibility', True)
    _set(ls, 'visibility', visibility)
    _set(ls, 'select_by_edge_types', True)
    # AND with negative previous categories prevents two brush jobs painting
    # the same feature edge. Category precedence is explicit in routing.FEATURES.
    _set(ls, 'edge_type_combination', 'AND' if job['suffix'] else 'OR')
    _set(ls, 'edge_type_negation', 'INCLUSIVE')
    _set(ls, 'select_by_face_marks', False)
    _set(ls, 'select_by_image_border', False)
    _set(ls, 'select_by_collection', collection is not None)
    _set(ls, 'collection', collection)
    _set(ls, 'collection_negation', 'INCLUSIVE')
    for feature in ('silhouette', 'border', 'crease', 'ridge_valley', 'suggestive_contour', 'material_boundary', 'contour', 'external_contour', 'edge_mark'):
        _set(ls, 'select_' + feature, feature in job['include'] or feature in job['exclude'])
        _set(ls, 'exclude_' + feature, feature in job['exclude'])
    brush = job['brush']
    style = ls.linestyle
    _set(style, 'color', tuple(brush.color))
    _set(style, 'alpha', brush.alpha)
    scale = width_scale(scene, node)
    _set(style, 'thickness', brush.thickness * scale)
    from . import stamp_brush
    _set(style, 'caps', 'BUTT' if stamp_brush.active(brush) else brush.caps)
    _set(style, 'use_dashed_line', brush.dashed)
    _set(style, 'dash1', max(1, round(brush.dash * scale)))
    _set(style, 'gap1', max(1, round(brush.gap * scale)))
    
    for name in ('dash2', 'dash3', 'gap2', 'gap3'):
        _set(style, name, 0)
    _sync_thickness_modifiers(style, brush)
    _sync_alpha_modifier(style, brush)
    _sync_stroke_alpha_modifier(style, brush)
    _sync_stroke_detail(scene, style, brush, scale)
    from . import final_alpha
    from . import stamp_brush
    final_alpha.sync(style,bool(getattr(brush,'distance_alpha',False) or getattr(brush,'linear_alpha',False) or stamp_brush.active(brush)),_set)
    stamp_brush.sync_style(style,brush,brush.thickness*scale,_set)


def _sync_stroke_detail(scene, style, brush, scale=None):
    """Map supported detail controls to native projected-chain operations."""
    scale = width_scale(scene) if scale is None else scale
    minimum = getattr(brush, 'min_line_length', 0)
    _set(style, 'use_length_min', minimum > 0)
    _set(style, 'length_min', minimum * scale)
    split = getattr(brush, 'split_angle_enabled', False)
    _set(style, 'use_angle_min', split)
    if split:
        _set(style, 'angle_min', brush.split_angle)
    jobs = []
    distortion = getattr(brush, 'distortion_enabled', False) and getattr(brush,'distortion_amplitude',0)!=0
    if distortion:
        jobs.append(('Sampling', 'SAMPLING', {'sampling': 1.0}))
    extend = getattr(brush, 'extend', 0)
    if extend:
        jobs.append(('Extend', 'BACKBONE_STRETCHER', {'backbone_length': extend * scale}))
    normal, x, y = (getattr(brush, name, 0) for name in ('normal_offset', 'offset_x', 'offset_y'))
    if normal or x or y:
        jobs.append(('Offset', '2D_OFFSET', {'start': normal * scale, 'end': normal * scale,
                                           'x': x * scale, 'y': -y * scale}))
    if distortion:
        wave = brush.distortion_wavelength * scale
        # Native SinusDisplacementShader takes a pixel phase and converts it to
        # radians internally; our UI stores the phase as an angle.
        jobs.append(('Distortion', 'SINUS_DISPLACEMENT', {
            'wavelength': wave, 'amplitude': brush.distortion_amplitude * scale,
            'phase': brush.distortion_phase / (2 * math.pi) * wave}))
    prefix = PREFIX + 'Detail '
    existing = [mod for mod in style.geometry_modifiers if mod.name.startswith(prefix)]
    desired = [(prefix + name, kind) for name, kind, _ in jobs]
    if [(mod.name, mod.type) for mod in existing] != desired:
        for mod in existing:
            style.geometry_modifiers.remove(mod)
        for name, kind, _ in jobs:
            style.geometry_modifiers.new(prefix + name, kind)
    for name, _, values in jobs:
        modifier = style.geometry_modifiers[prefix + name]
        _set(modifier, 'use', True)
        for key, value in values.items():
            _set(modifier, key, value)


def _sync_alpha_modifier(style, brush):
    name = PREFIX + 'Distance Alpha'
    modifier = style.alpha_modifiers.get(name)
    if getattr(brush, 'distance_independent', False) or not getattr(brush, 'distance_alpha', False) or getattr(brush,'distance_reference_enabled',False) or getattr(brush,'distance_curve_data',None) is not None:
        if modifier:
            style.alpha_modifiers.remove(modifier)
        return
    modifier = modifier or style.alpha_modifiers.new(name, 'DISTANCE_FROM_CAMERA')
    _set(modifier, 'blend', 'MULTIPLY')
    _set(modifier, 'influence', 1.0)
    _set(modifier, 'mapping', 'CURVE')
    _set(modifier, 'range_min', brush.distance_start)
    _set(modifier, 'range_max', brush.distance_end)
    curve = modifier.curve.curves[0]
    _sync_curve(modifier.curve, [(0, 1), (1, brush.far_alpha)])


def _sync_stroke_alpha_modifier(style, brush):
    name = PREFIX + 'Stroke Alpha'
    modifier = style.alpha_modifiers.get(name)
    if not getattr(brush, 'alpha_curve_enabled', False):
        if modifier:
            style.alpha_modifiers.remove(modifier)
        return
    modifier = modifier or style.alpha_modifiers.new(name, 'ALONG_STROKE')
    _set(modifier, 'use', True)
    _set(modifier, 'blend', 'MULTIPLY')
    _set(modifier, 'influence', 1.0)
    _set(modifier, 'mapping', 'CURVE')
    _set(modifier, 'invert', False)
    _sync_custom_curve(modifier.curve, brush.alpha_curve_data)


def _sync_custom_curve(mapping, data):
    global _last_sync_writes
    # Count topology changes in addition to the RNA values counted by _set.
    before = _last_sync_writes
    changes = curves.apply_mapping(mapping, data, setter=_set)
    _last_sync_writes += max(0, changes - (_last_sync_writes - before))


def _sync_curve(mapping, coordinates):
    global _last_sync_writes
    changed = False
    # Returning from a custom size curve must restore legacy interpolation,
    # clipping and level defaults as well as the endpoint coordinates.
    for name in curves.MAPPING_FIELDS:
        changed = _set(mapping, name, curves.DEFAULT[name]) or changed
    curve = mapping.curves[0]
    if len(curve.points) != len(coordinates):
        while len(curve.points) > 2:
            curve.points.remove(curve.points[1])
        for coordinate in coordinates[1:-1]:
            curve.points.new(*coordinate)
        _last_sync_writes += 1
        changed = True
    for point, coordinate in zip(curve.points, coordinates):
        changed = _set(point, 'location', coordinate) or changed
        changed = _set(point, 'handle_type', 'AUTO') or changed
    if changed:
        mapping.update()


def _sync_thickness_modifiers(style, brush):
    # Keep modifier IDs stable during interactive edits; no repeated accumulation.
    modifiers = style.thickness_modifiers
    for name, enabled, kind in (
        (PREFIX + 'Taper', getattr(brush, 'taper', False), 'ALONG_STROKE'),
        (PREFIX + 'Distance', getattr(brush, 'distance_fade', False) and not getattr(brush,'distance_independent',False) and not getattr(brush,'distance_reference_enabled',False) and getattr(brush,'distance_curve_data',None) is None, 'DISTANCE_FROM_CAMERA'),
    ):
        modifier = modifiers.get(name)
        if not enabled:
            if modifier:
                modifiers.remove(modifier)
            continue
        modifier = modifier or modifiers.new(name, kind)
        _set(modifier, 'use', True)
        _set(modifier, 'blend', 'MULTIPLY')
        _set(modifier, 'influence', 1.0)
        if kind == 'DISTANCE_FROM_CAMERA':
            if brush.distance_end <= brush.distance_start:
                raise ValueError('Distance End must be greater than Distance Start')
            _set(modifier, 'mapping', 'LINEAR')
            _set(modifier, 'range_min', brush.distance_start)
            _set(modifier, 'range_max', brush.distance_end)
            # Freestyle BoundedProperty uses abs(max-min), so descending
            # value bounds increase thickness. Keep bounds ascending and
            # invert the normalized distance instead.
            _set(modifier, 'value_min', brush.far_thickness)
            _set(modifier, 'value_max', 1.0)
            _set(modifier, 'invert', True)
        else:
            _set(modifier, 'mapping', 'CURVE')
            _set(modifier, 'value_min', 0.0)
            _set(modifier, 'value_max', 1.0)
            if getattr(brush, 'size_curve_enabled', False):
                _sync_custom_curve(modifier.curve, brush.size_curve_data)
            else:
                _sync_curve(modifier.curve, [(0, brush.taper_start), (.25, 1), (.75, 1), (1, brush.taper_end)])


@persistent
def _render_pre(scene, *args):
    from .gpu_render import LINE_ENGINES
    if scene.render.engine in LINE_ENGINES:
        _render_started[scene.as_pointer()] = time.perf_counter()
        return
    if hasattr(scene, "lea_line") and scene.lea_line.tree:
        if scene.lea_line.auto_sync and not (threading.current_thread() is not threading.main_thread()
                and object_uv.prepared(scene.name_full,scene.frame_current)):
            warnings = sync_scene(scene)
            for warning in warnings:
                print("rea pencil for blender:", warning)
        _render_started[scene.as_pointer()] = time.perf_counter()


@persistent
def _render_post(scene, *args):
    started = _render_started.pop(scene.as_pointer(), None)
    if started is not None:
        scene.lea_line.last_render_seconds = time.perf_counter() - started


@persistent
def _load_post(*args):
    from .ui import upgrade_tree
    from . import render_cache
    render_cache.clear()
    for tree in list(bpy.data.node_groups):
        upgrade_tree(tree)
        curves.upgrade_tree(tree)
    curves.reset_watch()
    from . import preview_runtime
    preview_runtime.reset()


def register():
    freestyle_material.register()
    curves.register()
    from . import preview_runtime
    preview_runtime.register()
    for handlers, callback in ((bpy.app.handlers.render_pre, _render_pre), (bpy.app.handlers.render_post, _render_post), (bpy.app.handlers.render_cancel, _render_post), (bpy.app.handlers.load_post, _load_post)):
        if callback not in handlers:
            handlers.append(callback)


def unregister():
    from . import preview_runtime
    preview_runtime.unregister()
    curves.unregister()
    freestyle_material.unregister()
    for handlers, callback in ((bpy.app.handlers.render_pre, _render_pre), (bpy.app.handlers.render_post, _render_post), (bpy.app.handlers.render_cancel, _render_post), (bpy.app.handlers.load_post, _load_post)):
        if callback in handlers:
            handlers.remove(callback)
    _render_started.clear()
