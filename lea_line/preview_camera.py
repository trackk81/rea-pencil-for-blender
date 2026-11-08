"""Native camera fields and pixel-sized radii for live Line Art preview.

Object Info keeps transforms in Blender's dependency graph. Only projection
settings (lens, camera type, resolution) need scalar synchronization.
"""
import math
import bpy
from . import free_view

TAG = 'rea_preview_camera_width'
PREFIX = '.rea camera '


def projection(scene):
    camera = free_view.camera(scene)
    if not camera or camera.data.type not in {'PERSP', 'ORTHO'}:
        raise ValueError('Live pixel effects require a perspective or orthographic scene camera')
    frame = camera.data.view_frame(scene=scene)
    vertical = max(p.y for p in frame) - min(p.y for p in frame)
    height = free_view.dimensions(scene)[1] if free_view.dimensions(scene) else max(1, int(scene.render.resolution_y * scene.render.resolution_percentage / 100))
    factor = vertical / height
    perspective = camera.data.type == 'PERSP'
    if perspective:
        factor /= abs(frame[0].z)
    return camera, perspective, factor, camera.data.clip_start


def signature(scene):
    if not free_view.camera(scene):
        return None
    camera, perspective, factor, clip = projection(scene)
    return (camera.as_pointer(), perspective, factor, clip,
            scene.render.pixel_aspect_x / scene.render.pixel_aspect_y,
            scene.render.resolution_y, scene.render.resolution_percentage)


def _node(graph, kind, name):
    node = graph.nodes.new(kind)
    node.name = PREFIX + name
    return node


def add_camera_fields(graph, scene):
    """Return native world-space fields; generated preview objects use identity."""
    links = graph.links
    camera = _node(graph, 'GeometryNodeObjectInfo', 'object')
    camera.transform_space = 'RELATIVE'
    position = _node(graph, 'GeometryNodeInputPosition', 'position').outputs[0]
    basis = {}
    for name, value in (('right', (1, 0, 0)), ('up', (0, 1, 0)), ('forward', (0, 0, -1))):
        rotate = _node(graph, 'FunctionNodeRotateVector', name)
        rotate.inputs['Vector'].default_value = value
        links.new(camera.outputs['Rotation'], rotate.inputs['Rotation'])
        basis[name] = rotate.outputs[0]
    relative = _node(graph, 'ShaderNodeVectorMath', 'relative')
    relative.operation = 'SUBTRACT'
    links.new(position, relative.inputs[0])
    links.new(camera.outputs['Location'], relative.inputs[1])
    depth = _node(graph, 'ShaderNodeVectorMath', 'depth')
    depth.operation = 'DOT_PRODUCT'
    links.new(relative.outputs[0], depth.inputs[0])
    links.new(basis['forward'], depth.inputs[1])
    positive = _node(graph, 'ShaderNodeMath', 'positive depth')
    positive.operation = 'MAXIMUM'
    links.new(depth.outputs['Value'], positive.inputs[0])
    scale = _node(graph, 'ShaderNodeMath', 'pixel scale')
    scale.operation = 'MULTIPLY'
    links.new(positive.outputs[0], scale.inputs[0])
    choose = _node(graph, 'GeometryNodeSwitch', 'projection')
    choose.input_type = 'FLOAT'
    links.new(scale.outputs[0], choose.inputs['True'])
    sync_fields(graph, scene)
    return {**basis, 'pixel_size': choose.outputs[0], 'position': position,
            'depth': positive.outputs[0], 'raw_depth': depth.outputs['Value'],
            'camera_location': camera.outputs['Location']}


def _set(owner, name, value):
    current = getattr(owner, name)
    same = math.isclose(current, value, rel_tol=1e-7, abs_tol=1e-9) if isinstance(value, float) else current == value
    if same:
        return False
    setattr(owner, name, value)
    return True


def sync_fields(graph, scene, setter=None):
    setter = setter or _set
    nodes = graph.nodes
    camera, perspective, factor, clip = projection(scene)
    writes = 0
    for owner, name, value in (
        (nodes[PREFIX+'object'].inputs['Object'], 'default_value', camera),
        (nodes[PREFIX+'positive depth'].inputs[1], 'default_value', clip),
        (nodes[PREFIX+'pixel scale'].inputs[1], 'default_value', factor),
        (nodes[PREFIX+'projection'].inputs['Switch'], 'default_value', perspective),
        (nodes[PREFIX+'projection'].inputs['False'], 'default_value', factor),
    ):
        writes += bool(setter(owner, name, value))
    return writes


def _owned(modifier):
    return (modifier.type == 'NODES' and modifier.node_group and modifier.node_group.get(TAG) == 1)


def sync(obj, scene, width, setter=None):
    """Set absolute radius before shape effects; preserve group IDs on edits."""
    setter = setter or _set
    modifier = next((m for m in obj.modifiers if _owned(m)), None)
    created = modifier is None
    if created:
        graph = bpy.data.node_groups.new('.rea preview pixel width', 'GeometryNodeTree')
        graph[TAG] = 1
        graph.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
        graph.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
        source = graph.nodes.new('NodeGroupInput')
        output = graph.nodes.new('NodeGroupOutput')
        fields = add_camera_fields(graph, scene)
        radius = _node(graph, 'ShaderNodeMath', 'radius')
        radius.operation = 'MULTIPLY'
        graph.links.new(fields['pixel_size'], radius.inputs[0])
        set_radius = graph.nodes.new('GeometryNodeSetCurveRadius')
        graph.links.new(source.outputs['Geometry'], set_radius.inputs['Curve'])
        graph.links.new(radius.outputs[0], set_radius.inputs['Radius'])
        graph.links.new(set_radius.outputs[0], output.inputs['Geometry'])
        modifier = obj.modifiers.new('.rea preview pixel width', 'NODES')
        modifier.node_group = graph
    graph = modifier.node_group
    sync_fields(graph, scene, setter)
    setter(graph.nodes[PREFIX+'radius'].inputs[1], 'default_value', width * .5)
    setter(modifier, 'show_render', False)
    setter(modifier, 'show_viewport', True)
    return int(created)


def cleanup(obj):
    for modifier in list(obj.modifiers):
        if _owned(modifier):
            graph = modifier.node_group
            obj.modifiers.remove(modifier)
            if graph.users == 0:
                bpy.data.node_groups.remove(graph)


def set_enabled(obj, enabled, setter=None):
    setter = setter or _set
    for modifier in obj.modifiers:
        if _owned(modifier):
            setter(modifier, 'show_viewport', enabled)
            setter(modifier, 'show_render', False)
