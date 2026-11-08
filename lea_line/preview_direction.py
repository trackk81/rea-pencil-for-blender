"""Native projected open-chain direction, applied after angle splitting.

The angle uses X right / Y down, matching the observed original control.
Closed curves keep their seam and winding. A nearly perpendicular or zero
endpoint displacement keeps its input order; original tie handling is unknown.
No geometry conversion, named attributes, or Python per-frame traversal.
"""
from . import preview_split as fields


def build(graph, geometry, angle_socket, enabled_socket):
    def node(kind, name):
        result = graph.nodes.new(kind)
        result.name = result.label = 'Direction ' + name
        return result

    index = node('GeometryNodeInputIndex', 'Index').outputs[0]
    first = node('GeometryNodePointsOfCurve', 'First Point')
    last = node('GeometryNodePointsOfCurve', 'Last Point')
    for endpoint in (first, last):
        fields._wire(graph, index, endpoint.inputs['Curve Index'])
        fields._wire(graph, index, endpoint.inputs['Weights'])
    fields._wire(graph, fields._math(graph, 'SUBTRACT', first.outputs['Total'], 1, integer=True), last.inputs['Sort Index'])
    position = node('GeometryNodeInputPosition', 'Projected Position').outputs[0]
    delta = fields._vector(graph, 'SUBTRACT',
                           fields._at(graph, position, last.outputs['Point Index'], 'FLOAT_VECTOR'),
                           fields._at(graph, position, first.outputs['Point Index'], 'FLOAT_VECTOR'))
    direction = node('ShaderNodeCombineXYZ', 'Preferred Vector')
    fields._wire(graph, fields._math(graph, 'COSINE', angle_socket), direction.inputs['X'])
    fields._wire(graph, fields._math(graph, 'MULTIPLY', fields._math(graph, 'SINE', angle_socket), -1), direction.inputs['Y'])
    alignment = fields._vector(graph, 'DOT_PRODUCT',
                               fields._vector(graph, 'NORMALIZE', delta), direction.outputs[0])
    # Suppress float sine/cosine noise at cardinal angles and retain ties.
    backwards = fields._math(graph, 'LESS_THAN', alignment, -1e-6)
    cyclic = node('GeometryNodeInputSplineCyclic', 'Closed Chain').outputs[0]
    selected = fields._bool(graph, 'AND', backwards, fields._bool(graph, 'NOT', cyclic))
    reverse = node('GeometryNodeReverseCurve', 'Orient Open Chains')
    fields._wire(graph, geometry, reverse.inputs['Curve'])
    fields._wire(graph, selected, reverse.inputs['Selection'])
    return fields._switch(graph, 'GEOMETRY', enabled_socket, geometry, reverse.outputs[0])
