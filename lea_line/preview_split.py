"""Native projected angle splitting for the generated single-layer GP preview.

Call before pixel resampling: positions are projected pixels, radii are pixels,
and the caller's depth attributes are still attached. Each selected corner is
duplicated into the two adjacent chains. No source data or Python frame handler
is changed. The disabled socket selects the original geometry directly.

This builder targets the addon's generated polyline ink, not arbitrary authored
Grease Pencil layers. It preserves per-point attributes and material/cap data;
the GP conversion creates a default layer (its name is not retained). Inputs
with any layer count other than one pass through without reconstruction.
"""

PREFIX = '.rea_split_'


def _node(graph, kind, label):
    node = graph.nodes.new(kind)
    node.name = node.label = 'Split ' + label
    return node


def _wire(graph, value, socket):
    if isinstance(value, (int, float, bool)):
        socket.default_value = value
    else:
        graph.links.new(value, socket)


def _math(graph, operation, *values, integer=False):
    node = _node(graph, 'FunctionNodeIntegerMath' if integer else 'ShaderNodeMath', operation)
    node.operation = operation
    for value, socket in zip(values, node.inputs):
        _wire(graph, value, socket)
    return node.outputs[0]


def _bool(graph, operation, *values):
    node = _node(graph, 'FunctionNodeBooleanMath', operation)
    node.operation = operation
    for value, socket in zip(values, node.inputs):
        _wire(graph, value, socket)
    return node.outputs[0]


def _switch(graph, kind, condition, first, second):
    node = _node(graph, 'GeometryNodeSwitch', 'Choose ' + kind)
    node.input_type = kind
    for name, value in (('Switch', condition), ('False', first), ('True', second)):
        _wire(graph, value, node.inputs[name])
    return node.outputs[0]


def _vector(graph, operation, first, second=None):
    node = _node(graph, 'ShaderNodeVectorMath', operation)
    node.operation = operation
    _wire(graph, first, node.inputs[0])
    if second is not None:
        _wire(graph, second, node.inputs[1])
    return node.outputs['Value'] if operation in {'DOT_PRODUCT', 'LENGTH'} else node.outputs['Vector']


def _read(graph, name, data_type):
    node = _node(graph, 'GeometryNodeInputNamedAttribute', 'Read ' + name)
    node.data_type = data_type
    node.inputs['Name'].default_value = name
    return node.outputs['Attribute']


def _store(graph, geometry, name, data_type, value, domain='POINT'):
    node = _node(graph, 'GeometryNodeStoreNamedAttribute', 'Store ' + name)
    node.data_type, node.domain = data_type, domain
    node.inputs['Name'].default_value = name
    _wire(graph, geometry, node.inputs['Geometry'])
    _wire(graph, value, node.inputs['Value'])
    return node.outputs[0]


def _at(graph, value, index, data_type):
    node = _node(graph, 'GeometryNodeFieldAtIndex', 'Endpoint ' + data_type)
    node.domain, node.data_type = 'POINT', data_type
    _wire(graph, value, node.inputs['Value'])
    _wire(graph, index, node.inputs['Index'])
    return node.outputs[0]


def _normalize_closed(graph, geometry):
    """Turn Line Art's repeated-endpoint open chains into actual cyclic curves.

    Only coincident projected positions with matching depth AND inverse depth
    qualify. A mere screen-space crossing never joins different 3D endpoints.
    Missing camera-depth attributes conservatively leave the chain unchanged.
    """
    index = _node(graph, 'GeometryNodeInputIndex', 'Closure Index').outputs[0]
    curve = _node(graph, 'GeometryNodeCurveOfPoint', 'Closure Curve')
    _wire(graph, index, curve.inputs['Point Index'])
    first = _math(graph, 'SUBTRACT', index, curve.outputs['Index in Curve'], integer=True)
    count = _node(graph, 'GeometryNodeAccumulateField', 'Closure Point Count')
    count.data_type, count.domain = 'INT', 'POINT'
    count.inputs['Value'].default_value = 1
    _wire(graph, curve.outputs['Curve Index'], count.inputs['Group ID'])
    last = _math(graph, 'SUBTRACT',
                 _math(graph, 'ADD', first, count.outputs['Total'], integer=True), 1, integer=True)
    position = _node(graph, 'GeometryNodeInputPosition', 'Closure Position').outputs[0]
    delta = _vector(graph, 'SUBTRACT', _at(graph, position, first, 'FLOAT_VECTOR'),
                     _at(graph, position, last, 'FLOAT_VECTOR'))
    closed = _bool(graph, 'AND', _math(graph, 'GREATER_THAN', count.outputs['Total'], 2),
                   _math(graph, 'LESS_THAN', _vector(graph, 'LENGTH', delta), 1e-5))
    for name, tolerance in (('.rea_depth', 1e-6), ('.rea_inverse_depth', 1e-9)):
        attr = _node(graph, 'GeometryNodeInputNamedAttribute', 'Closure ' + name)
        attr.data_type = 'FLOAT'
        attr.inputs['Name'].default_value = name
        matches = _math(graph, 'COMPARE', _at(graph, attr.outputs['Attribute'], first, 'FLOAT'),
                         _at(graph, attr.outputs['Attribute'], last, 'FLOAT'), tolerance)
        closed = _bool(graph, 'AND', closed, _bool(graph, 'AND', attr.outputs['Exists'], matches))
    cyclic = _node(graph, 'GeometryNodeInputSplineCyclic', 'Closure Existing Cyclic').outputs[0]
    closed = _bool(graph, 'AND', closed, _bool(graph, 'NOT', cyclic))
    name = PREFIX + 'close_endpoint'
    geometry = _store(graph, geometry, name, 'BOOLEAN', closed)
    remove = _node(graph, 'GeometryNodeDeleteGeometry', 'Remove Repeated Closing Point')
    remove.domain = 'POINT'
    _wire(graph, geometry, remove.inputs['Geometry'])
    _wire(graph, _bool(graph, 'AND', _read(graph, name, 'BOOLEAN'),
                       _math(graph, 'COMPARE', index, last, .1)), remove.inputs['Selection'])
    set_cyclic = _node(graph, 'GeometryNodeSetSplineCyclic', 'Normalize Closed Chains')
    _wire(graph, remove.outputs[0], set_cyclic.inputs['Geometry'])
    _wire(graph, _read(graph, name, 'BOOLEAN'), set_cyclic.inputs['Selection'])
    set_cyclic.inputs['Cyclic'].default_value = True
    cleanup = _node(graph, 'GeometryNodeRemoveAttribute', 'Remove Closure Flag')
    cleanup.inputs['Name'].default_value = name
    _wire(graph, set_cyclic.outputs[0], cleanup.inputs['Geometry'])
    return cleanup.outputs[0]


def build(graph, geometry, angle_socket, enabled_socket, *, normalize_closed=True):
    """Return GP geometry split where projected tangent turn > angle (radians).

    Coincident neighbor points have no meaningful angle and do not split.
    A zero threshold selects every valid interior point, including straight
    points, matching the observed original endpoint behavior at zero.
    Unsplit cyclic curves remain cyclic; split cyclic curves are rotated into
    open chains whose endpoints retain the exact duplicated corner data.
    A caller that already ran preview_loop may skip repeated normalization.
    """
    original = geometry
    if normalize_closed:
        geometry = _normalize_closed(graph, geometry)
    index = _node(graph, 'GeometryNodeInputIndex', 'Original Index').outputs[0]
    curve = _node(graph, 'GeometryNodeCurveOfPoint', 'Original Curve')
    _wire(graph, index, curve.inputs['Point Index'])
    curve_id = curve.outputs['Curve Index']
    in_curve = curve.outputs['Index in Curve']
    base = _math(graph, 'SUBTRACT', index, in_curve, integer=True)
    position = _node(graph, 'GeometryNodeInputPosition', 'Pixel Position').outputs[0]
    neighbors, valid = [], []
    for step in (-1, 1):
        offset = _node(graph, 'GeometryNodeOffsetPointInCurve', 'Neighbor ' + str(step))
        _wire(graph, index, offset.inputs['Point Index'])
        offset.inputs['Offset'].default_value = step
        sample = _node(graph, 'GeometryNodeFieldAtIndex', 'Neighbor Position ' + str(step))
        sample.domain, sample.data_type = 'POINT', 'FLOAT_VECTOR'
        _wire(graph, position, sample.inputs['Value'])
        _wire(graph, offset.outputs['Point Index'], sample.inputs['Index'])
        vector = _vector(graph, 'SUBTRACT', sample.outputs[0], position)
        neighbors.append(_vector(graph, 'NORMALIZE', vector))
        valid.append(_bool(graph, 'AND', offset.outputs['Is Valid Offset'],
                           _math(graph, 'GREATER_THAN', _vector(graph, 'LENGTH', vector), 1e-6)))
    dot = _vector(graph, 'DOT_PRODUCT', *neighbors)
    dot = _math(graph, 'MINIMUM', _math(graph, 'MAXIMUM', dot, -1), 1)
    angle = _math(graph, 'ARCCOSINE', _math(graph, 'MULTIPLY', dot, -1))
    zero_threshold = _bool(graph, 'NOT', _math(graph, 'GREATER_THAN', angle_socket, 1e-9))
    corner = _bool(graph, 'AND', _bool(graph, 'AND', *valid),
                   _bool(graph, 'OR', zero_threshold, _math(graph, 'GREATER_THAN', angle, angle_socket)))
    count = _node(graph, 'GeometryNodeAccumulateField', 'Corners Per Curve')
    count.data_type, count.domain = 'INT', 'POINT'
    _wire(graph, corner, count.inputs['Value'])
    _wire(graph, curve_id, count.inputs['Group ID'])
    point_count = _node(graph, 'GeometryNodeAccumulateField', 'Points Per Curve')
    point_count.data_type, point_count.domain = 'INT', 'POINT'
    point_count.inputs['Value'].default_value = 1
    _wire(graph, curve_id, point_count.inputs['Group ID'])
    cyclic = _node(graph, 'GeometryNodeInputSplineCyclic', 'Original Cyclic').outputs[0]
    # Base uses the original first point index, so segment IDs cannot collide
    # between curves and do not need a fixed multiplier or float arithmetic.
    fields = {'base': ('INT', base), 'order': ('INT', in_curve),
              'point_count': ('INT', point_count.outputs['Total']),
              'corner': ('BOOLEAN', corner), 'segment': ('INT', count.outputs['Trailing']),
              'total': ('INT', count.outputs['Total']), 'cyclic': ('BOOLEAN', cyclic)}
    for name in ('material_index', 'start_cap', 'end_cap'):
        fields[name] = ('INT', _read(graph, name, 'INT'))
    for name, (kind, value) in fields.items():
        geometry = _store(graph, geometry, PREFIX + name, kind, value)
    # Curve to Points supports GP drawings directly and emits one instance for
    # each layer; subsequent nodes operate natively on those point clouds.
    points = _node(graph, 'GeometryNodeCurveToPoints', 'Evaluated Polyline Points')
    points.mode = 'EVALUATED'
    _wire(graph, geometry, points.inputs['Curve'])
    duplicate = _node(graph, 'GeometryNodeDuplicateElements', 'Duplicate Corners')
    duplicate.domain = 'POINT'
    _wire(graph, points.outputs['Points'], duplicate.inputs['Geometry'])
    _wire(graph, _math(graph, 'ADD', _read(graph, PREFIX + 'corner', 'BOOLEAN'), 1, integer=True),
          duplicate.inputs['Amount'])
    segment = _math(graph, 'ADD', _read(graph, PREFIX + 'segment', 'INT'),
                    duplicate.outputs['Duplicate Index'], integer=True)
    total = _read(graph, PREFIX + 'total', 'INT')
    was_cyclic = _read(graph, PREFIX + 'cyclic', 'BOOLEAN')
    split_cycle = _bool(graph, 'AND', was_cyclic, _math(graph, 'GREATER_THAN', total, 0))
    segment = _switch(graph, 'INT', split_cycle, segment,
                      _math(graph, 'MODULO', segment, _math(graph, 'MAXIMUM', total, 1, integer=True), integer=True))
    group = _math(graph, 'ADD', _read(graph, PREFIX + 'base', 'INT'), segment, integer=True)
    # For the wrapped cyclic chain, initial points sort after the tail. Negating
    # the tail's original indices is insufficient; use an offset above every
    # original point index while retaining all original relative ordering.
    wrap_head = _bool(graph, 'AND', split_cycle,
                      _bool(graph, 'AND', _math(graph, 'LESS_THAN', _read(graph, PREFIX + 'segment', 'INT'), 1),
                            _math(graph, 'LESS_THAN', duplicate.outputs['Duplicate Index'], 1)))
    order = _read(graph, PREFIX + 'order', 'INT')
    # Non-head points are negative; head points are nonnegative. Local point
    # indices keep sorting precise for realistically sized preview strokes.
    order = _switch(graph, 'FLOAT', wrap_head,
                    _math(graph, 'SUBTRACT', order, _read(graph, PREFIX + 'point_count', 'INT'), integer=True), order)
    rebuilt = _node(graph, 'GeometryNodePointsToCurves', 'Rebuild Chains')
    _wire(graph, duplicate.outputs['Geometry'], rebuilt.inputs['Points'])
    _wire(graph, group, rebuilt.inputs['Curve Group ID'])
    _wire(graph, order, rebuilt.inputs['Weight'])
    geometry = rebuilt.outputs[0]
    set_cyclic = _node(graph, 'GeometryNodeSetSplineCyclic', 'Restore Unsplit Loops')
    _wire(graph, geometry, set_cyclic.inputs['Geometry'])
    _wire(graph, _bool(graph, 'AND', was_cyclic, _math(graph, 'LESS_THAN', total, 1)), set_cyclic.inputs['Cyclic'])
    geometry = set_cyclic.outputs[0]
    for name in ('material_index', 'start_cap', 'end_cap'):
        geometry = _store(graph, geometry, name, 'INT', _read(graph, PREFIX + name, 'INT'), 'CURVE')
    to_gp = _node(graph, 'GeometryNodeCurvesToGreasePencil', 'Restore Grease Pencil')
    to_gp.inputs['Instances as Layers'].default_value = True
    _wire(graph, geometry, to_gp.inputs['Curves'])
    # Point clouds have no material slots. Retain the original GP palette via
    # an empty drawing, then remove its empty layer after native Join Geometry.
    # This also preserves a material replaced by the owner's undo repair without
    # storing another Material pointer in this graph or adding a sync callback.
    empty = _node(graph, 'GeometryNodeDeleteGeometry', 'Retain Original Palette')
    empty.domain = 'CURVE'
    empty.inputs['Selection'].default_value = True
    _wire(graph, original, empty.inputs['Geometry'])
    join = _node(graph, 'GeometryNodeJoinGeometry', 'Restore Material Palette')
    # A link inserted into a multi-input socket precedes its existing links in
    # Blender 4.4. Insert the empty source last so it occupies layer index zero.
    _wire(graph, to_gp.outputs[0], join.inputs['Geometry'])
    _wire(graph, empty.outputs[0], join.inputs['Geometry'])
    remove_layer = _node(graph, 'GeometryNodeDeleteGeometry', 'Remove Empty Source Layer')
    remove_layer.domain = 'LAYER'
    _wire(graph, join.outputs[0], remove_layer.inputs['Geometry'])
    _wire(graph, _math(graph, 'LESS_THAN', index, 1), remove_layer.inputs['Selection'])
    geometry = remove_layer.outputs[0]
    # Join remaps material indices using each input's palette. The reconstructed
    # input had no slots, so restore original indices after the palettes join.
    geometry = _store(graph, geometry, 'material_index', 'INT',
                      _read(graph, PREFIX + 'material_index', 'INT'), 'CURVE')
    for name in fields:
        remove = _node(graph, 'GeometryNodeRemoveAttribute', 'Remove ' + name)
        remove.inputs['Name'].default_value = PREFIX + name
        _wire(graph, geometry, remove.inputs['Geometry'])
        geometry = remove.outputs[0]
    layers = _node(graph, 'GeometryNodeAttributeDomainSize', 'Source Layer Count')
    layers.component = 'GREASEPENCIL'
    _wire(graph, original, layers.inputs['Geometry'])
    single_layer = _math(graph, 'COMPARE', layers.outputs['Layer Count'], 1, .01)
    supported = _bool(graph, 'AND', enabled_socket, single_layer)
    return _switch(graph, 'GEOMETRY', supported, original, geometry)
