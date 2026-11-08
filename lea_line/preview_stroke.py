"""Native post-Line-Art stroke styling for Blender 4.4 live previews.

Only addon-owned preview data is changed. Native Geometry Nodes multiply the
generated radius and opacity by editable curves; no Python per-frame geometry
walk or redraw rasterizer is installed. Line Art chaining still differs from
Freestyle and the original renderer.
"""
import copy
import math
import uuid
import bpy
from . import curves
from . import preview_camera, preview_split, preview_loop, preview_direction, preview_coverage
from . import preview_endpoint
from .width_profile import live_variable_coverage_supported, live_variable_coverage_factor

TAG = 'rea_preview_stroke_graph'
OWNER = 'rea_preview_stroke_owner'
SCHEMA = 1
MODIFIER_NAME = 'rea pencil live Brush Detail'
SIZE_NODE = 'Stroke Size Curve'
ALPHA_NODE = 'Stroke Alpha Curve'
BUILD = 13
DASH_UID = 'rea_preview_stroke_dash_uid'


def _same(current, desired):
    if isinstance(desired, (list, tuple)):
        return len(current) == len(desired) and all(_same(a, b) for a, b in zip(current, desired))
    if isinstance(desired, float):
        return math.isclose(current, desired, rel_tol=1e-6, abs_tol=1e-7)
    return current == desired


def _set(owner, name, value):
    if _same(getattr(owner, name), value):
        return False
    setattr(owner, name, value)
    return True


def _owned(graph, obj):
    token = obj.get(OWNER)
    return bool(graph and token and graph.get(TAG) is True and graph.get(OWNER) == token
                and graph.get('schema') == SCHEMA)


def _modifiers(obj):
    return [modifier for modifier in obj.modifiers
            if modifier.type == 'NODES' and _owned(modifier.node_group, obj)]


def cleanup(obj):
    """Remove only this object's tagged styling modifiers and unused graphs."""
    changed = 0
    for modifier in _modifiers(obj):
        graph = modifier.node_group
        obj.modifiers.remove(modifier)
        changed += 1
        if graph.users == 0 and graph.library is None:
            bpy.data.node_groups.remove(graph)
    dash = _dash_modifier(obj)
    if dash:
        obj.modifiers.remove(dash)
        changed += 1
    if DASH_UID in obj:
        del obj[DASH_UID]
    return changed


def _dash_modifier(obj):
    token = obj.get(DASH_UID)
    return next((modifier for modifier in obj.modifiers if modifier.type == 'GREASE_PENCIL_DASH'
                 and modifier.persistent_uid == token), None) if token is not None else None


def set_enabled(obj, enabled, setter=None):
    """Stage live effects without deleting IDs; final renders never use them."""
    set_value = setter or _set
    modifiers = _modifiers(obj)
    dash = _dash_modifier(obj)
    if dash:
        modifiers.append(dash)
    for modifier in modifiers:
        set_value(modifier, 'show_viewport', bool(enabled))
        set_value(modifier, 'show_render', False)
    return {'structural_writes': 0, 'curve_writes': 0, 'warnings': []}


def _node(graph, kind, name):
    node = graph.nodes.new(kind)
    node.name = node.label = name
    return node


def _multiply(graph, name, first, second):
    node = _node(graph, 'ShaderNodeMath', name)
    node.operation = 'MULTIPLY'
    graph.links.new(first, node.inputs[0])
    graph.links.new(second, node.inputs[1])
    return node.outputs[0]


def _math(graph, operation, name, *values):
    node = _node(graph, 'ShaderNodeMath', name)
    node.operation = operation
    for index, value in enumerate(values):
        if isinstance(value, (int, float)):
            node.inputs[index].default_value = value
        else:
            graph.links.new(value, node.inputs[index])
    return node.outputs[0]


def _vector(graph, operation, name, first, second=None):
    node = _node(graph, 'ShaderNodeVectorMath', name)
    node.operation = operation
    graph.links.new(first, node.inputs[0])
    if second is not None:
        graph.links.new(second, node.inputs['Scale'] if operation == 'SCALE' else node.inputs[1])
    return node.outputs['Value'] if operation == 'DOT_PRODUCT' else node.outputs['Vector']


def _value(graph, name, value=0):
    node = _node(graph, 'ShaderNodeValue', name)
    node.outputs[0].default_value = value
    return node.outputs[0]


def _attribute(graph, name, data_type='FLOAT'):
    node = _node(graph, 'GeometryNodeInputNamedAttribute', 'Read ' + name)
    node.data_type = data_type
    node.inputs['Name'].default_value = name
    return node.outputs['Attribute']


def _combine(graph, name, x, y, z=0):
    node = _node(graph, 'ShaderNodeCombineXYZ', name)
    for index, value in enumerate((x, y, z)):
        if isinstance(value, (int, float)):
            node.inputs[index].default_value = value
        else:
            graph.links.new(value, node.inputs[index])
    return node.outputs[0]


def _position(graph, name, geometry, position=None, offset=None):
    node = _node(graph, 'GeometryNodeSetPosition', name)
    graph.links.new(geometry, node.inputs['Geometry'])
    if position is not None:
        graph.links.new(position, node.inputs['Position'])
    if offset is not None:
        graph.links.new(offset, node.inputs['Offset'])
    return node.outputs[0]


def _resample(graph, geometry, name):
    length = _node(graph, 'GeometryNodeSplineLength', name + ' Length').outputs['Length']
    count = _math(graph, 'ADD', name + ' Count', _math(graph, 'CEIL', name + ' Round', length), 1)
    count = _math(graph, 'MINIMUM', name + ' Budget', count, 16384)
    resample = _node(graph, 'GeometryNodeResampleCurve', name)
    resample.mode = 'COUNT'
    graph.links.new(geometry, resample.inputs['Curve'])
    graph.links.new(count, resample.inputs['Count'])
    return resample.outputs['Curve']


def _project(graph, geometry, scene):
    fields = preview_camera.add_camera_fields(graph, scene)
    depth = fields['depth']
    geometry = _store(graph, geometry, '.rea_depth', 'FLOAT', 'POINT', depth)
    inverse = _math(graph, 'DIVIDE', 'Inverse Camera Depth', 1, depth)
    geometry = _store(graph, geometry, '.rea_inverse_depth', 'FLOAT', 'POINT', inverse)
    radius = _node(graph, 'GeometryNodeInputRadius', 'World Radius').outputs[0]
    radius = _math(graph, 'DIVIDE', 'Radius In Pixels', radius, fields['pixel_size'])
    set_radius = _node(graph, 'GeometryNodeSetCurveRadius', 'Project Radius')
    graph.links.new(geometry, set_radius.inputs['Curve'])
    graph.links.new(radius, set_radius.inputs['Radius'])
    relative = _vector(graph, 'SUBTRACT', 'Camera Relative Position', fields['position'], fields['camera_location'])
    x = _vector(graph, 'DOT_PRODUCT', 'Projected Horizontal', relative, fields['right'])
    y = _vector(graph, 'DOT_PRODUCT', 'Projected Vertical', relative, fields['up'])
    horizontal_size = _math(graph, 'MULTIPLY', 'Horizontal Pixel Size', fields['pixel_size'], _value(graph, 'Projection Pixel Aspect', 1))
    x = _math(graph, 'DIVIDE', 'Horizontal Pixels', x, horizontal_size)
    y = _math(graph, 'DIVIDE', 'Vertical Pixels', y, fields['pixel_size'])
    position = _combine(graph, 'Pixel Position', x, y)
    geometry = _position(graph, 'Project Camera Pixels', set_radius.outputs['Curve'], position=position)
    return geometry, fields


def _unproject(graph, geometry, fields):
    inverse = _attribute(graph, '.rea_inverse_depth')
    perspective_depth = _math(graph, 'DIVIDE', 'Interpolated Perspective Depth', 1, inverse)
    choose = _node(graph, 'GeometryNodeSwitch', 'Restore Depth')
    choose.input_type = 'FLOAT'
    graph.links.new(_attribute(graph, '.rea_depth'), choose.inputs['False'])
    graph.links.new(perspective_depth, choose.inputs['True'])
    depth = choose.outputs[0]
    factor = _value(graph, 'Projection Pixel Unit')
    perspective_size = _math(graph, 'MULTIPLY', 'Restored Perspective Pixel Size', factor, depth)
    size = _node(graph, 'GeometryNodeSwitch', 'Restore Pixel Size')
    size.input_type = 'FLOAT'
    graph.links.new(factor, size.inputs['False'])
    graph.links.new(perspective_size, size.inputs['True'])
    pixel_size = size.outputs[0]
    position = _node(graph, 'GeometryNodeInputPosition', 'Styled Pixel Position').outputs[0]
    split = _node(graph, 'ShaderNodeSeparateXYZ', 'Styled Pixel Coordinates')
    graph.links.new(position, split.inputs[0])
    x = _math(graph, 'MULTIPLY', 'Restore Horizontal Units', split.outputs['X'], pixel_size)
    x = _math(graph, 'MULTIPLY', 'Restore Horizontal Aspect', x, graph.nodes['Projection Pixel Aspect'].outputs[0])
    y = _math(graph, 'MULTIPLY', 'Restore Vertical Units', split.outputs['Y'], pixel_size)
    right = _vector(graph, 'SCALE', 'Restore Horizontal Vector', fields['right'], x)
    up = _vector(graph, 'SCALE', 'Restore Vertical Vector', fields['up'], y)
    forward = _vector(graph, 'SCALE', 'Restore Depth Vector', fields['forward'], depth)
    horizontal_vertical = _vector(graph, 'ADD', 'Restore Camera Plane', right, up)
    local = _vector(graph, 'ADD', 'Restore Camera Space', horizontal_vertical, forward)
    world = _vector(graph, 'ADD', 'Restore World Position', local, fields['camera_location'])
    radius = _node(graph, 'GeometryNodeInputRadius', 'Styled Pixel Radius').outputs[0]
    set_radius = _node(graph, 'GeometryNodeSetCurveRadius', 'Restore World Radius')
    graph.links.new(geometry, set_radius.inputs['Curve'])
    graph.links.new(_math(graph, 'MULTIPLY', 'Restored World Radius', radius, pixel_size), set_radius.inputs['Radius'])
    geometry = _position(graph, 'Unproject Camera Pixels', set_radius.outputs['Curve'], position=world)
    for name in ('.rea_depth', '.rea_inverse_depth'):
        remove = _node(graph, 'GeometryNodeRemoveAttribute', 'Remove ' + name)
        remove.inputs['Name'].default_value = name
        graph.links.new(geometry, remove.inputs['Geometry'])
        geometry = remove.outputs[0]
    return geometry


def _store(graph, geometry, name, data_type, domain, value=None):
    node = _node(graph, 'GeometryNodeStoreNamedAttribute', 'Set ' + name)
    node.data_type, node.domain = data_type, domain
    node.inputs['Name'].default_value = name
    graph.links.new(geometry, node.inputs['Geometry'])
    if value is not None:
        graph.links.new(value, node.inputs['Value'])
    return node.outputs['Geometry']


def _build(obj, scene, loop_start='KEEP', loop_orientation='KEEP'):
    graph = bpy.data.node_groups.new('.rea pencil live stroke style', 'GeometryNodeTree')
    graph[TAG], graph[OWNER], graph['schema'] = True, obj[OWNER], SCHEMA
    graph['build'] = BUILD
    graph['loop_config'] = loop_start+'|'+loop_orientation
    try:
        graph.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
        graph.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
        source = _node(graph, 'NodeGroupInput', 'Input')
        output = _node(graph, 'NodeGroupOutput', 'Output')
        geometry, fields = _project(graph, source.outputs['Geometry'], scene)
        geometry = preview_loop.build(graph,geometry,True,start=loop_start,orientation=loop_orientation)
        geometry = preview_split.build(graph, geometry,
            _value(graph, 'Split Turn Threshold', math.pi/2),
            _value(graph, 'Angle Split Enabled'),
            normalize_closed=loop_start == loop_orientation == 'KEEP')
        geometry = preview_direction.build(graph, geometry,
            _value(graph, 'Line Direction Radians', -math.pi/6),
            _value(graph, 'Line Direction Enabled'))
        minimum = _value(graph, 'Minimum Pixels')
        length = _node(graph, 'GeometryNodeSplineLength', 'Projected Stroke Length').outputs['Length']
        selection = _math(graph, 'LESS_THAN', 'Short Stroke Filter', length, minimum)
        remove = _node(graph, 'GeometryNodeDeleteGeometry', 'Remove Short Strokes')
        remove.domain = 'CURVE'
        graph.links.new(geometry, remove.inputs['Geometry'])
        graph.links.new(selection, remove.inputs['Selection'])
        coverage_enabled = _value(graph, 'Open Coverage Enabled')
        endpoint_enabled = _value(graph,preview_endpoint.ENABLED)
        geometry, endpoint_open = preview_endpoint.capture(graph,remove.outputs[0],endpoint_enabled)
        geometry, open_field = preview_coverage.store_open(graph,geometry,coverage_enabled)
        geometry = _resample(graph, geometry, 'Pixel Sampling')
        parameter = _node(graph, 'GeometryNodeSplineParameter', 'Stroke Parameter')
        size = _node(graph, 'ShaderNodeFloatCurve', SIZE_NODE)
        alpha = _node(graph, 'ShaderNodeFloatCurve', ALPHA_NODE)
        for node in (size, alpha):
            graph.links.new(parameter.outputs['Factor'], node.inputs['Value'])
        size_factor = _math(graph, 'MAXIMUM', 'Size Minimum Clamp', size.outputs['Value'], 0)
        size_factor = _math(graph, 'MINIMUM', 'Size Maximum Clamp', size_factor, 1)
        coverage_factor = preview_coverage.factor(graph,coverage_enabled,_value(graph,'Open Coverage Factor',1),open_field)
        size_factor = _multiply(graph,'Open Coverage Size',size_factor,coverage_factor)
        alpha_factor = _math(graph, 'MAXIMUM', 'Alpha Minimum Clamp', alpha.outputs['Value'], 0)
        alpha_factor = _math(graph, 'MINIMUM', 'Alpha Maximum Clamp', alpha_factor, 1)
        tangent = _node(graph, 'GeometryNodeInputTangent', 'Projected Tangent').outputs[0]
        tangent_xyz = _node(graph, 'ShaderNodeSeparateXYZ', 'Tangent Coordinates')
        graph.links.new(tangent, tangent_xyz.inputs[0])
        normal = _combine(graph, 'Projected Normal', _math(graph, 'MULTIPLY', 'Normal X', tangent_xyz.outputs['Y'], -1), tangent_xyz.outputs['X'])
        normal_amount = _value(graph, 'Normal Offset Pixels')
        wave = _math(graph, 'DIVIDE', 'Wave Cycles', parameter.outputs['Length'], _value(graph, 'Wave Length Pixels', 100))
        wave = _math(graph, 'MULTIPLY', 'Wave Radians', wave, 2 * math.pi)
        wave = _math(graph, 'ADD', 'Wave Phase', wave, _value(graph, 'Wave Phase Radians'))
        wave = _math(graph, 'SINE', 'Wave Sine', wave)
        wave = _math(graph, 'MULTIPLY', 'Wave Displacement', wave, _value(graph, 'Wave Amount Pixels'))
        displacement = _math(graph, 'ADD', 'Normal And Wave Displacement', normal_amount, wave)
        normal_offset = _vector(graph, 'SCALE', 'Projected Normal Displacement', normal, displacement)
        xy = _combine(graph, 'XY Pixel Offset', _value(graph, 'X Offset Pixels'), _value(graph, 'Y Offset Pixels'))
        geometry_offset = _vector(graph, 'ADD', 'Pixel Geometry Offset', xy, normal_offset)
        starts = _math(graph, 'LESS_THAN', 'Start Endpoint', parameter.outputs['Factor'], .000001)
        ends = _math(graph, 'GREATER_THAN', 'End Endpoint', parameter.outputs['Factor'], .999999)
        ends = _math(graph, 'SUBTRACT', 'Endpoint Direction', ends, starts)
        cyclic = _node(graph, 'GeometryNodeInputSplineCyclic', 'Cyclic Strokes').outputs[0]
        open_curve = _math(graph, 'SUBTRACT', 'Open Stroke', 1, cyclic)
        ends = _math(graph, 'MULTIPLY', 'Open Endpoint Direction', ends, open_curve)
        original_radius = _node(graph, 'GeometryNodeInputRadius', 'Endpoint Radius').outputs[0]
        square = _math(graph, 'MULTIPLY', 'Square Cap Radius', original_radius, size_factor)
        square = _math(graph, 'MULTIPLY', 'Square Cap Extension', square, _value(graph, 'Square Caps'))
        extension = _math(graph, 'ADD', 'Total Endpoint Extension', square, _value(graph, 'Extend Pixels'))
        extension = _math(graph, 'MULTIPLY', 'Signed Endpoint Extension', extension, ends)
        extension = _vector(graph, 'SCALE', 'Endpoint Extension Vector', tangent, extension)
        geometry_offset = _vector(graph, 'ADD', 'All Pixel Geometry Offsets', geometry_offset, extension)
        geometry = _position(graph, 'Apply Pixel Geometry', geometry, offset=geometry_offset)
        # Re-sampling after distortion/extension keeps dash intervals near one
        # screen pixel; the bounded count prevents runaway offscreen allocations.
        resampled = _resample(graph, geometry, 'Dash Pixel Sampling')
        choose = _node(graph, 'GeometryNodeSwitch', 'Dash Sampling Enabled')
        choose.input_type = 'GEOMETRY'
        graph.links.new(geometry, choose.inputs['False'])
        graph.links.new(resampled, choose.inputs['True'])
        geometry = choose.outputs[0]
        radius = _node(graph, 'GeometryNodeInputRadius', 'Original Radius')
        thickness = _node(graph, 'GeometryNodeSetCurveRadius', 'Apply Stroke Size')
        graph.links.new(geometry, thickness.inputs['Curve'])
        scaled_radius = _multiply(graph, 'Multiply Stroke Size', radius.outputs['Radius'], size_factor)
        scaled_radius = preview_endpoint.radius(graph,scaled_radius,endpoint_open,ends,endpoint_enabled)
        graph.links.new(scaled_radius, thickness.inputs['Radius'])
        opacity = _node(graph, 'GeometryNodeInputNamedAttribute', 'Original Opacity')
        opacity.data_type = 'FLOAT'
        opacity.inputs['Name'].default_value = 'opacity'
        scaled_opacity = _multiply(graph, 'Multiply Stroke Alpha', opacity.outputs['Attribute'], alpha_factor)
        geometry = _store(graph, thickness.outputs['Curve'], 'opacity', 'FLOAT', 'POINT', scaled_opacity)
        geometry = _store(graph, geometry, 'start_cap', 'INT', 'CURVE')
        geometry = _store(graph, geometry, 'end_cap', 'INT', 'CURVE')
        geometry = _unproject(graph, geometry, fields)
        unreduced = geometry
        distance = _node(graph, 'ShaderNodeVectorMath', 'Camera Distance')
        distance.operation = 'DISTANCE'
        graph.links.new(fields['position'], distance.inputs[0])
        graph.links.new(fields['camera_location'], distance.inputs[1])
        near = _value(graph, 'Reduction Near')
        span = _value(graph, 'Reduction Span', 1)
        fraction = _math(graph, 'SUBTRACT', 'Distance From Near', distance.outputs['Value'], near)
        fraction = _math(graph, 'DIVIDE', 'Distance Fraction', fraction, span)
        fraction = _math(graph, 'MAXIMUM', 'Distance Near Clamp', fraction, 0)
        fraction = _math(graph, 'MINIMUM', 'Distance Far Clamp', fraction, 1)
        size_reduction = _math(graph, 'MULTIPLY', 'Distance Size Delta', fraction, _value(graph, 'Distance Size Delta'))
        size_reduction = _math(graph, 'ADD', 'Distance Size Factor', size_reduction, 1)
        alpha_reduction = _math(graph, 'MULTIPLY', 'Distance Alpha Delta', fraction, _value(graph, 'Distance Alpha Delta'))
        alpha_reduction = _math(graph, 'ADD', 'Distance Alpha Factor', alpha_reduction, 1)
        radius = _node(graph, 'GeometryNodeInputRadius', 'Distance Input Radius').outputs[0]
        reduced = _node(graph, 'GeometryNodeSetCurveRadius', 'Apply Distance Size')
        graph.links.new(geometry, reduced.inputs['Curve'])
        graph.links.new(_multiply(graph, 'Reduced Radius', radius, size_reduction), reduced.inputs['Radius'])
        opacity = _attribute(graph, 'opacity')
        geometry = _store(graph, reduced.outputs[0], 'opacity', 'FLOAT', 'POINT',
                          _multiply(graph, 'Reduced Opacity', opacity, alpha_reduction))
        enabled = _node(graph, 'GeometryNodeSwitch', 'Distance Enabled')
        enabled.input_type = 'GEOMETRY'
        graph.links.new(unreduced, enabled.inputs['False'])
        graph.links.new(geometry, enabled.inputs['True'])
        geometry = enabled.outputs[0]
        geometry = preview_coverage.clear(graph,geometry)
        graph.links.new(geometry, output.inputs['Geometry'])
        return graph
    except Exception:
        if graph.users == 0:
            bpy.data.node_groups.remove(graph)
        raise


def _flat():
    return dict(copy.deepcopy(curves.DEFAULT), points=[[0, 1, 'VECTOR'], [1, 1, 'VECTOR']])


def _size_data(brush):
    if getattr(brush, 'size_curve_enabled', False):
        return brush.size_curve_data
    if getattr(brush, 'taper', False):
        return dict(copy.deepcopy(curves.DEFAULT), points=[
            [0, brush.taper_start, 'AUTO'], [.25, 1, 'AUTO'],
            [.75, 1, 'AUTO'], [1, brush.taper_end, 'AUTO']])
    return _flat()


def sync(obj, brush, scene, width_scale=1.0, setter=None):
    """Sync stable native styling IDs; ordinary writes use the supplied setter.

    Returned structural_writes/curve_writes are extra changes not already counted
    by setter. It returns warnings about supported approximations, never claims
    missing effects were applied.
    """
    result = {'structural_writes': 0, 'curve_writes': 0, 'warnings': []}
    loop_start = getattr(brush,'loop_start','KEEP')
    loop_orientation = getattr(brush,'loop_orientation','KEEP')
    loop_config = loop_start+'|'+loop_orientation
    distance_size = getattr(brush, 'distance_fade', False)
    distance_alpha = getattr(brush, 'distance_alpha', False)
    near, far = getattr(brush, 'distance_start', 0), getattr(brush, 'distance_end', 50)
    if (distance_size or distance_alpha) and far <= near:
        raise ValueError('Distance End must be greater than Distance Start')
    active = (getattr(brush, 'size_curve_enabled', False) or getattr(brush, 'alpha_curve_enabled', False)
              or distance_size or distance_alpha or getattr(brush, 'split_angle_enabled', False)
              or getattr(brush, 'line_direction_enabled', False)
              or loop_start != 'KEEP' or loop_orientation != 'KEEP'
              or getattr(brush, 'taper', False) or getattr(brush, 'caps', 'ROUND') != 'ROUND'
              or getattr(brush, 'dashed', False) or (getattr(brush, 'distortion_enabled', False) and getattr(brush,'distortion_amplitude',0)!=0)
              or any(getattr(brush, name, 0) for name in ('min_line_length', 'extend', 'normal_offset', 'offset_x', 'offset_y')))
    if not active:
        result['structural_writes'] = cleanup(obj)
        return result
    set_value = setter or _set
    owned = _modifiers(obj)
    if not owned:
        if not obj.get(OWNER):
            obj[OWNER] = uuid.uuid4().hex
        graph = _build(obj, scene, loop_start, loop_orientation)
        modifier = obj.modifiers.new(MODIFIER_NAME, 'NODES')
        modifier.node_group = graph
        result['structural_writes'] += 1
    else:
        modifier = owned[0]
        graph = modifier.node_group
        if graph.get('build') != BUILD or graph.get('loop_config') != loop_config:
            previous = graph
            graph = _build(obj, scene, loop_start, loop_orientation)
            modifier.node_group = graph
            if previous.users == 0 and previous.library is None:
                bpy.data.node_groups.remove(previous)
            result['structural_writes'] += 1
    preview_camera.sync_fields(graph, scene, set_value)
    _, perspective, factor, _ = preview_camera.projection(scene)
    set_value(graph.nodes['Projection Pixel Unit'].outputs[0], 'default_value', factor)
    set_value(graph.nodes['Projection Pixel Aspect'].outputs[0], 'default_value', scene.render.pixel_aspect_x / scene.render.pixel_aspect_y)
    for name in ('Restore Depth', 'Restore Pixel Size'):
        set_value(graph.nodes[name].inputs['Switch'], 'default_value', perspective)
    set_value(modifier, 'show_viewport', True)
    set_value(modifier, 'show_render', False)
    set_value(graph.nodes['Distance Enabled'].inputs['Switch'], 'default_value', distance_size or distance_alpha)
    from . import reduction_reference
    result['structural_writes'] += reduction_reference.sync_live(graph,brush,set_value)
    from . import distance_curves
    structural,mapping=distance_curves.sync_live(graph,brush,set_value)
    from . import independent_reduction
    extra_structure,extra_mapping=independent_reduction.sync_live(graph,brush,set_value)
    structural+=extra_structure;mapping+=extra_mapping
    result['structural_writes']+=structural;result['curve_writes']+=mapping
    mapping_changed = False
    for node_name, payload in ((SIZE_NODE, _size_data(brush)),
                               (ALPHA_NODE, brush.alpha_curve_data if getattr(brush, 'alpha_curve_enabled', False) else _flat())):
        counted = 0
        def curve_set(owner, name, value):
            nonlocal counted
            changed = set_value(owner, name, value)
            counted += bool(changed)
            return changed
        changed = curves.apply_mapping(graph.nodes[node_name].mapping, payload, setter=curve_set)
        mapping_changed = bool(changed) or mapping_changed
        result['curve_writes'] += max(0, changed - counted)
        prefix = 'Size' if node_name == SIZE_NODE else 'Alpha'
        set_value(graph.nodes[prefix + ' Minimum Clamp'].inputs[1], 'default_value', payload['clip_min_y'])
        set_value(graph.nodes[prefix + ' Maximum Clamp'].inputs[1], 'default_value', payload['clip_max_y'])
    if mapping_changed:
        # CurveMapping.update rebuilds its lookup table but does not invalidate
        # an already evaluated Geometry Nodes modifier in Blender 4.4. Tag only
        # changed mappings; no-op and color-only synchronization stay untouched.
        graph.update_tag()
    cap = 1 if getattr(brush, 'caps', 'ROUND') in {'BUTT', 'SQUARE'} else 0
    variable_coverage = bool(getattr(scene.lea_line,'preview_simple_coverage',False) and live_variable_coverage_supported(brush))
    nominal_width = getattr(brush,'thickness',1) * width_scale
    for name in ('start_cap', 'end_cap'):
        set_value(graph.nodes['Set ' + name].inputs['Value'], 'default_value', cap)
    values = {
        preview_endpoint.ENABLED:float(preview_endpoint.enabled(brush,scene)),
        'Open Coverage Enabled':float(variable_coverage),
        'Open Coverage Factor':live_variable_coverage_factor(nominal_width) if variable_coverage else 1,
        'Split Turn Threshold': max(0.0, min(math.pi, math.pi-getattr(brush, 'split_angle', math.pi/2))),
        'Angle Split Enabled': float(getattr(brush, 'split_angle_enabled', False)),
        'Line Direction Radians': getattr(brush, 'line_direction', -math.pi/6),
        'Line Direction Enabled': float(getattr(brush, 'line_direction_enabled', False)),
        'Reduction Near': near,
        'Reduction Span': max(.000001, far-near),
        'Distance Size Delta': brush.far_thickness-1 if distance_size else 0,
        'Distance Alpha Delta': brush.far_alpha-1 if distance_alpha else 0,
        'Minimum Pixels': getattr(brush, 'min_line_length', 0) * width_scale,
        'Extend Pixels': getattr(brush, 'extend', 0) * width_scale,
        'X Offset Pixels': getattr(brush, 'offset_x', 0) * width_scale,
        'Y Offset Pixels': -getattr(brush, 'offset_y', 0) * width_scale,
        'Normal Offset Pixels': getattr(brush, 'normal_offset', 0) * width_scale,
        'Square Caps': float(getattr(brush, 'caps', 'ROUND') == 'SQUARE'),
        'Wave Amount Pixels': getattr(brush, 'distortion_amplitude', 0) * width_scale if getattr(brush, 'distortion_enabled', False) else 0,
        'Wave Length Pixels': max(.0001, getattr(brush, 'distortion_wavelength', 100) * width_scale),
        'Wave Phase Radians': getattr(brush, 'distortion_phase', 0),
    }
    for name, value in values.items():
        set_value(graph.nodes[name].outputs[0], 'default_value', float(value))
    if distance_size or distance_alpha:
        result['warnings'].append('Live distance reduction uses per-point camera distance; original reduction curves/reference objects and final stroke sampling may differ.')
    dashed = getattr(brush, 'dashed', False)
    set_value(graph.nodes['Dash Sampling Enabled'].inputs['Switch'], 'default_value', dashed)
    dash = _dash_modifier(obj)
    if dashed:
        if dash is None:
            dash = obj.modifiers.new('rea pencil live Dash', 'GREASE_PENCIL_DASH')
            obj[DASH_UID] = dash.persistent_uid
            result['structural_writes'] += 1
        segment = dash.segments[0]
        set_value(segment, 'dash', max(2, min(32767, round(brush.dash * width_scale) + 1)))
        set_value(segment, 'gap', max(0, min(32767, round(brush.gap * width_scale) - 1)))
        set_value(segment, 'radius', 1.0)
        set_value(segment, 'opacity', 1.0)
        set_value(segment, 'material_index', -1)
        set_value(segment, 'use_cyclic', False)
        phase = round(getattr(brush,'dash_phase',0)*width_scale)
        bounded_phase = max(-2147483648,min(2147483647,phase))
        set_value(dash, 'dash_offset', bounded_phase)
        if phase != bounded_phase:
            result['warnings'].append('Live dash phase was limited to the native signed32-bit range after width scaling.')
        set_value(dash, 'show_viewport', True)
        set_value(dash, 'show_render', False)
        result['warnings'].append('Live dash lengths use pixel samples and may differ by one sample at stroke ends.')
    elif dash:
        obj.modifiers.remove(dash)
        del obj[DASH_UID]
        result['structural_writes'] += 1
    return result
