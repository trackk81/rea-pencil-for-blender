"""Native closed-loop point ordering for projected, generated GP preview ink.

This is ordering machinery, not an asserted Pencil+ start-point policy. KEEP
defaults are exact bypasses. A caller may opt into screen-space lexicographic
start rules and winding, or supply a per-curve start index/reversal field.
"""
from . import preview_split as split

PREFIX = '.rea_loop_'
START_RULES = {
    'MIN_X_MIN_Y': ('X', 1, 'Y', 1),
    'MIN_X_MAX_Y': ('X', 1, 'Y', -1),
    'MAX_X_MIN_Y': ('X', -1, 'Y', 1),
    'MAX_X_MAX_Y': ('X', -1, 'Y', -1),
    'MIN_Y_MIN_X': ('Y', 1, 'X', 1),
    'MIN_Y_MAX_X': ('Y', 1, 'X', -1),
    'MAX_Y_MIN_X': ('Y', -1, 'X', 1),
    'MAX_Y_MAX_X': ('Y', -1, 'X', -1),
}


def _node(graph, kind, name):
    node = graph.nodes.new(kind)
    node.name = node.label = 'Loop ' + name
    return node


def _read(graph, suffix, kind='INT'):
    return split._read(graph, PREFIX + suffix, kind)


def _accumulate(graph, value, group, kind='INT'):
    node = _node(graph, 'GeometryNodeAccumulateField', 'Grouped ' + kind)
    node.data_type, node.domain = kind, 'POINT'
    split._wire(graph, value, node.inputs['Value'])
    split._wire(graph, group, node.inputs['Group ID'])
    return node


def _sort(graph, geometry, group, weight):
    node = _node(graph, 'GeometryNodeSortElements', 'Select Canonical Start')
    node.domain = 'POINT'
    split._wire(graph, geometry, node.inputs['Geometry'])
    split._wire(graph, group, node.inputs['Group ID'])
    split._wire(graph, weight, node.inputs['Sort Weight'])
    return node.outputs[0]


def build(graph, geometry, enabled_socket, *, start='KEEP', orientation='KEEP',
          start_index_socket=None, reverse_socket=None):
    """Reorder closed chains while retaining every point's attributes and ink.

    ``start`` is KEEP or a START_RULES key. Rules use projected X right/Y up.
    ``orientation`` is KEEP, CLOCKWISE or COUNTERCLOCKWISE. Explicit socket
    arguments override the corresponding policy; start indices are local to
    each normalized closed curve. Open chains retain their existing order.
    Multiple layers and a disabled input pass through exactly.
    """
    if start not in {'KEEP', *START_RULES}:
        raise ValueError('Unknown loop start rule: ' + str(start))
    if orientation not in {'KEEP', 'CLOCKWISE', 'COUNTERCLOCKWISE'}:
        raise ValueError('Unknown loop orientation: ' + str(orientation))
    if start == orientation == 'KEEP' and start_index_socket is None and reverse_socket is None:
        return geometry
    original = geometry
    geometry = split._normalize_closed(graph, geometry)
    index = _node(graph, 'GeometryNodeInputIndex', 'Original Index').outputs[0]
    curve = _node(graph, 'GeometryNodeCurveOfPoint', 'Original Curve')
    split._wire(graph, index, curve.inputs['Point Index'])
    group = curve.outputs['Curve Index']
    local = curve.outputs['Index in Curve']
    count = _accumulate(graph, 1, group).outputs['Total']
    cyclic = _node(graph, 'GeometryNodeInputSplineCyclic', 'Closed Chain').outputs[0]
    position = _node(graph, 'GeometryNodeInputPosition', 'Projected Position').outputs[0]
    xy = _node(graph, 'ShaderNodeSeparateXYZ', 'Projected Coordinates')
    split._wire(graph, position, xy.inputs[0])
    if reverse_socket is not None:
        reverse = reverse_socket
    elif orientation == 'KEEP':
        reverse = False
    else:
        next_point = _node(graph, 'GeometryNodeOffsetPointInCurve', 'Next Point')
        split._wire(graph, index, next_point.inputs['Point Index'])
        next_point.inputs['Offset'].default_value = 1
        following = _node(graph, 'ShaderNodeSeparateXYZ', 'Next Coordinates')
        split._wire(graph, split._at(graph, position, next_point.outputs['Point Index'], 'FLOAT_VECTOR'), following.inputs[0])
        cross = split._math(graph, 'SUBTRACT',
                            split._math(graph, 'MULTIPLY', xy.outputs['X'], following.outputs['Y']),
                            split._math(graph, 'MULTIPLY', xy.outputs['Y'], following.outputs['X']))
        area = _accumulate(graph, cross, group, 'FLOAT').outputs['Total']
        reverse = split._math(graph, 'GREATER_THAN' if orientation == 'CLOCKWISE' else 'LESS_THAN', area, 0)
    fields = {'group': ('INT', group), 'order': ('INT', local), 'count': ('INT', count),
              'cyclic': ('BOOLEAN', cyclic), 'reverse': ('BOOLEAN', reverse)}
    if start_index_socket is not None:
        fields['start'] = ('INT', start_index_socket)
    for name in ('material_index', 'start_cap', 'end_cap'):
        fields[name] = ('INT', split._read(graph, name, 'INT'))
    for name, (kind, value) in fields.items():
        geometry = split._store(graph, geometry, PREFIX + name, kind, value)
    points = _node(graph, 'GeometryNodeCurveToPoints', 'Polyline Points')
    points.mode = 'EVALUATED'
    split._wire(graph, geometry, points.inputs['Curve'])
    geometry = points.outputs['Points']
    group, local, count = (_read(graph, name) for name in ('group', 'order', 'count'))
    if start_index_socket is not None:
        start_index = _read(graph, 'start')
    elif start == 'KEEP':
        start_index = 0
    else:
        primary, primary_sign, secondary, secondary_sign = START_RULES[start]
        # Blender 4.4 Sort Elements breaks equal weights by their current index,
        # so secondary then primary gives an exact stable lexicographic order.
        # Source: node_geo_sort_elements.cc grouped_sort(), v4.4.0.
        geometry = _sort(graph, geometry, group, split._math(graph, 'MULTIPLY', xy.outputs[secondary], secondary_sign))
        geometry = _sort(graph, geometry, group, split._math(graph, 'MULTIPLY', xy.outputs[primary], primary_sign))
        rank = _accumulate(graph, 1, group).outputs['Trailing']
        selected = split._switch(graph, 'INT', split._math(graph, 'LESS_THAN', rank, 1), 0, local)
        start_index = _accumulate(graph, selected, group).outputs['Total']
    relative = split._math(graph, 'SUBTRACT', local, start_index, integer=True)
    directed = split._switch(graph, 'INT', _read(graph, 'reverse', 'BOOLEAN'), relative,
                              split._math(graph, 'NEGATE', relative, integer=True))
    rotated = split._math(graph, 'FLOORED_MODULO', directed,
                          split._math(graph, 'MAXIMUM', count, 1, integer=True), integer=True)
    weight = split._switch(graph, 'INT', _read(graph, 'cyclic', 'BOOLEAN'), local, rotated)
    rebuild = _node(graph, 'GeometryNodePointsToCurves', 'Ordered Chains')
    split._wire(graph, geometry, rebuild.inputs['Points'])
    split._wire(graph, group, rebuild.inputs['Curve Group ID'])
    split._wire(graph, weight, rebuild.inputs['Weight'])
    close = _node(graph, 'GeometryNodeSetSplineCyclic', 'Restore Closed Chains')
    split._wire(graph, rebuild.outputs[0], close.inputs['Geometry'])
    split._wire(graph, _read(graph, 'cyclic', 'BOOLEAN'), close.inputs['Cyclic'])
    geometry = close.outputs[0]
    for name in ('start_cap', 'end_cap'):
        geometry = split._store(graph, geometry, name, 'INT', _read(graph, name), 'CURVE')
    gp = _node(graph, 'GeometryNodeCurvesToGreasePencil', 'Restore Grease Pencil')
    gp.inputs['Instances as Layers'].default_value = True
    split._wire(graph, geometry, gp.inputs['Curves'])
    empty = _node(graph, 'GeometryNodeDeleteGeometry', 'Retain Original Material Palette')
    empty.domain = 'CURVE'
    empty.inputs['Selection'].default_value = True
    split._wire(graph, original, empty.inputs['Geometry'])
    join = _node(graph, 'GeometryNodeJoinGeometry', 'Restore Original Material Palette')
    # Multi-input links insert before existing links in Blender 4.4.
    split._wire(graph, gp.outputs[0], join.inputs['Geometry'])
    split._wire(graph, empty.outputs[0], join.inputs['Geometry'])
    remove = _node(graph, 'GeometryNodeDeleteGeometry', 'Remove Empty Source Layer')
    remove.domain = 'LAYER'
    split._wire(graph, join.outputs[0], remove.inputs['Geometry'])
    split._wire(graph, split._math(graph, 'LESS_THAN', index, 1), remove.inputs['Selection'])
    geometry = split._store(graph, remove.outputs[0], 'material_index', 'INT', _read(graph, 'material_index'), 'CURVE')
    for name in fields:
        remove = _node(graph, 'GeometryNodeRemoveAttribute', 'Remove ' + name)
        remove.inputs['Name'].default_value = PREFIX + name
        split._wire(graph, geometry, remove.inputs['Geometry'])
        geometry = remove.outputs[0]
    layers = _node(graph, 'GeometryNodeAttributeDomainSize', 'Source Layer Count')
    layers.component = 'GREASEPENCIL'
    split._wire(graph, original, layers.inputs['Geometry'])
    enabled = split._bool(graph, 'AND', enabled_socket,
                          split._math(graph, 'COMPARE', layers.outputs['Layer Count'], 1, .01))
    return split._switch(graph, 'GEOMETRY', enabled, original, geometry)
