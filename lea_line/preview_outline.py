"""Camera-aware exterior filtering for a single-mesh, Outline-only preview.

Line Art emits internal front/back transitions as contours. Keep its visibility
calculation, then reject points backed by the target on both screen sides.
Other categories and multiple-object sets retain their existing evaluation.
"""
import bpy
from . import preview_camera, preview_stroke as f, preview_split as split

TAG = 'rea_exterior_outline'


def cleanup(obj):
    for modifier in list(obj.modifiers):
        if modifier.type == 'NODES' and modifier.node_group and modifier.node_group.get(TAG):
            graph = modifier.node_group
            obj.modifiers.remove(modifier)
            if graph.users == 0:
                bpy.data.node_groups.remove(graph)


def sync(obj, scene, target, *, orient_wave=False):
    if target is None:
        cleanup(obj)
        return
    modifier = next((m for m in obj.modifiers if m.type == 'NODES' and m.node_group
                     and m.node_group.get(TAG)), None)
    perspective = preview_camera.projection(scene)[1]
    if modifier and (modifier.node_group.get('perspective') != perspective or 'Orient open wave' not in modifier.node_group.nodes):
        cleanup(obj)
        modifier = None
    if modifier is None:
        g = bpy.data.node_groups.new('.rea exterior outline', 'GeometryNodeTree')
        g[TAG] = 1
        g['perspective'] = perspective
        g.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
        g.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
        source = g.nodes.new('NodeGroupInput')
        output = g.nodes.new('NodeGroupOutput')
        fields = preview_camera.add_camera_fields(g, scene)
        target_node = g.nodes.new('GeometryNodeObjectInfo')
        target_node.name = 'Outline target'
        target_node.transform_space = 'RELATIVE'
        tangent = g.nodes.new('GeometryNodeInputTangent').outputs[0]
        tx = f._vector(g, 'DOT_PRODUCT', 'Tangent X', tangent, fields['right'])
        ty = f._vector(g, 'DOT_PRODUCT', 'Tangent Y', tangent, fields['up'])
        if perspective:
            delta = f._vector(g, 'SUBTRACT', 'Position from eye', fields['position'], fields['camera_location'])
            td = f._vector(g, 'DOT_PRODUCT', 'Tangent depth', tangent, fields['forward'])
            for axis, basis in (('X', fields['right']), ('Y', fields['up'])):
                coord = f._vector(g, 'DOT_PRODUCT', 'Position '+axis, delta, basis)
                slope = f._math(g, 'DIVIDE', 'Projected '+axis, coord, fields['depth'])
                correction = f._math(g, 'MULTIPLY', 'Perspective '+axis, slope, td)
                if axis == 'X':
                    tx = f._math(g, 'SUBTRACT', 'Projected tangent X', tx, correction)
                else:
                    ty = f._math(g, 'SUBTRACT', 'Projected tangent Y', ty, correction)
        negative_y = f._math(g, 'MULTIPLY', 'Side X', ty, -1.)
        normal = f._vector(g, 'ADD', 'Screen side',
                           f._vector(g, 'SCALE', 'Side right', fields['right'], negative_y),
                           f._vector(g, 'SCALE', 'Side up', fields['up'], tx))
        normal = f._vector(g, 'NORMALIZE', 'Unit side', normal)
        hits = []
        for sign in (-1., 1.):
            distance = f._math(g, 'MULTIPLY', 'Side pixels', fields['pixel_size'], sign*1.5)
            offset = f._vector(g, 'SCALE', 'Side offset', normal, distance)
            sample = f._vector(g, 'ADD', 'Side sample', fields['position'], offset)
            delta = f._vector(g, 'SUBTRACT', 'View ray', sample, fields['camera_location'])
            ray = g.nodes.new('GeometryNodeRaycast')
            ray.data_type = 'FLOAT'
            g.links.new(target_node.outputs['Geometry'], ray.inputs['Target Geometry'])
            g.links.new(fields['camera_location'], ray.inputs['Source Position'])
            g.links.new(delta, ray.inputs['Ray Direction'])
            ray.inputs['Ray Length'].default_value = 1e8
            if not perspective:
                depth = f._vector(g, 'SCALE', 'Parallel depth', fields['forward'], fields['raw_depth'])
                origin = f._vector(g, 'SUBTRACT', 'Parallel origin', sample, depth)
                g.links.new(origin, ray.inputs['Source Position'])
                g.links.new(fields['forward'], ray.inputs['Ray Direction'])
            hits.append(ray.outputs['Is Hit'])
        both = g.nodes.new('FunctionNodeBooleanMath')
        both.operation = 'AND'
        for i, hit in enumerate(hits):
            g.links.new(hit, both.inputs[i])
        delete = g.nodes.new('GeometryNodeDeleteGeometry')
        delete.domain = 'POINT'
        g.links.new(source.outputs['Geometry'], delete.inputs['Geometry'])
        g.links.new(both.outputs[0], delete.inputs['Selection'])
        # Normalize only open, displaced exterior contours. A left-side hit
        # with no right-side hit means its left normal points inward.
        # Capture on POINT before CURVE adaptation: raycasting the average
        # position instead would test the curve center and lose the side.
        inward = f._math(g, 'SUBTRACT', 'Backing side vote', hits[1], hits[0])
        average = g.nodes.new('GeometryNodeFieldOnDomain')
        average.data_type = 'FLOAT'
        average.domain = 'CURVE'
        captured=f._store(g, source.outputs['Geometry'], '.rea_wave_side_vote', 'FLOAT', 'POINT', inward)
        g.links.new(captured, delete.inputs['Geometry'])
        g.links.new(f._attribute(g, '.rea_wave_side_vote'), average.inputs['Value'])
        backwards = f._math(g, 'GREATER_THAN', 'Inward majority', average.outputs[0], .0001)
        cyclic = g.nodes.new('GeometryNodeInputSplineCyclic').outputs[0]
        # Line Art also represents closed ink with coincident endpoints and
        # use_cyclic=False; do not change those loops' existing seam contract.
        index=g.nodes.new('GeometryNodeInputIndex').outputs[0]
        first=g.nodes.new('GeometryNodePointsOfCurve')
        last=g.nodes.new('GeometryNodePointsOfCurve')
        for endpoint in (first,last):
            g.links.new(index,endpoint.inputs['Curve Index'])
            g.links.new(index,endpoint.inputs['Weights'])
        g.links.new(f._math(g,'SUBTRACT','Last index',first.outputs['Total'],1),last.inputs['Sort Index'])
        position=g.nodes.new('GeometryNodeInputPosition').outputs[0]
        delta=split._vector(g,'SUBTRACT',split._at(g,position,last.outputs['Point Index'],'FLOAT_VECTOR'),
                            split._at(g,position,first.outputs['Point Index'],'FLOAT_VECTOR'))
        gap=split._vector(g,'LENGTH',delta)
        open_gap=f._math(g,'GREATER_THAN','Separated endpoints',gap,
                         f._math(g,'MULTIPLY','Closed endpoint tolerance',fields['pixel_size'],4.))
        enabled = f._math(g,'MULTIPLY','Open endpoints enabled',f._value(g, 'Orient open wave'),open_gap)
        selected = f._math(g, 'MULTIPLY', 'Open wave winding', backwards,
                          f._math(g, 'MULTIPLY', 'Open wave enabled', enabled,
                                  f._math(g, 'SUBTRACT', 'Open curve', 1., cyclic)))
        reverse = g.nodes.new('GeometryNodeReverseCurve')
        g.links.new(delete.outputs[0], reverse.inputs['Curve'])
        g.links.new(selected, reverse.inputs['Selection'])
        remove=g.nodes.new('GeometryNodeRemoveAttribute')
        remove.inputs['Name'].default_value='.rea_wave_side_vote'
        g.links.new(reverse.outputs[0], remove.inputs['Geometry'])
        g.links.new(remove.outputs[0], output.inputs['Geometry'])
        modifier = obj.modifiers.new('.rea exterior outline', 'NODES')
        modifier.node_group = g
    g = modifier.node_group
    f._set(g.nodes['Orient open wave'].outputs[0], 'default_value', float(orient_wave))
    if g.nodes['Outline target'].inputs['Object'].default_value != target:
        g.nodes['Outline target'].inputs['Object'].default_value = target
    preview_camera.sync_fields(g, scene)
    if modifier.show_render:
        modifier.show_render = False
    # Evaluate before width, texture and displacement effects.
    index = list(obj.modifiers).index(modifier)
    line_index = next(i for i, m in enumerate(obj.modifiers) if m.type == 'LINEART')
    if index != line_index+1:
        obj.modifiers.move(index, line_index+1)
