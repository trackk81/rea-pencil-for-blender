"""Node-level Line Set editing with original-style presentation.

Internal socket identifiers and existing links remain compatible with old files.
All actions use Blender operators; the original package is never imported.
"""
import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty

_growing = set()


def original(node):
    return getattr(node.id_data, 'ui_layout', 'COMPACT') == 'ORIGINAL'


def append_socket(node):
    socket = node.inputs.new('LEALineSocket', 'Line Set')
    socket.link_limit = 1
    return socket


def ensure_trailing(node):
    """Append only; old files retain their socket IDs, links and blank slots."""
    if node.bl_idname != 'LEAOutputNode' or not hasattr(bpy.data, 'scenes'):
        return
    from . import ui, preview_material
    if preview_material.is_suspended():
        return
    key = node.as_pointer()
    if key in _growing:
        return
    _growing.add(key)
    try:
        with ui.suspend_sync():
            if not node.inputs or node.inputs[-1].is_linked:
                append_socket(node)
    finally:
        _growing.discard(key)


def available_socket(node):
    return next((s for s in node.inputs if not s.is_linked), None) or append_socket(node)


def socket_color(socket, node):
    if socket.is_output:
        kind = node.bl_idname
    elif node.bl_idname == 'LEAOutputNode':
        kind = 'LEALineSetNode'
    elif node.bl_idname == 'LEALineSetNode':
        kind = 'LEADistanceReductionNode' if socket.name.endswith(' Reduction') else 'LEABrushNode'
    elif socket.name == 'Brush Detail':
        kind = 'LEABrushDetailNode'
    else:
        kind = 'LEADistanceReductionNode'
    return {'LEALineSetNode': (1., .4, .216, .5),
            'LEABrushNode': (.4, 1., .216, .5),
            'LEABrushDetailNode': (0., .9, .9, .5)}.get(kind, (0., .4, .9, .5))


def _button(layout, node, index, action, icon):
    op = layout.operator('lea_line.line_socket', text='', icon=icon, emboss=action != 'CREATE')
    op.tree_name = node.id_data.name
    op.node_name = node.name
    op.socket_index = index
    op.action = action


def draw_socket(socket, context, layout, node, text):
    if not original(node):
        layout.label(text=text.replace('_', ' ').title())
        return
    if socket.is_output:
        layout.label(text='Output')
        return
    if node.bl_idname == 'LEAOutputNode':
        layout.enabled = node.enabled and node.id_data.is_editable
        index = list(node.inputs).index(socket)
        left = layout.row()
        if socket.is_linked:
            left.label(text=socket.links[0].from_node.name)
        else:
            _button(left, node, index, 'CREATE', 'NODE')
        right = layout.row(align=True)
        right.alignment = 'RIGHT'
        _button(right, node, index, 'INSERT', 'ADD')
        _button(right, node, index, 'REMOVE', 'REMOVE')
        return
    if node.bl_idname == 'LEALineSetNode':
        layout.enabled = node.enabled
        names = {'Brush': 'V Brush Settings', 'Hidden Brush': 'H Brush Settings'}
        label = socket.name if socket.name.endswith(' Reduction') else names.get(socket.name)
        if label is None:
            hidden = socket.name.startswith('Hidden ')
            name = socket.name.removeprefix('Hidden ').removesuffix(' Brush')
            name = {'outline': 'Outline', 'border': 'Open Border', 'crease': 'Normal Angle',
                    'material_boundary': 'Material ID', 'edge_mark': 'Selected Edges',
                    'ridge_valley': 'Ridge / Valley', 'suggestive_contour': 'Suggestive Contour'}.get(name, name)
            label = ('H ' if hidden else 'V ') + name
        layout.label(text=label)
        return
    if node.bl_idname=='LEABrushNode' and socket.name in ('Color Map','Size Map'):
        layout.prop(node,'color_map_on' if socket.name=='Color Map' else 'size_map_on',text='')
    if node.bl_idname=='LEABrushDetailNode' and socket.name in ('Brush Map','Distortion Map'):
        layout.prop(node,'brush_map_on' if socket.name=='Brush Map' else 'distortion_map_on',text='')
    layout.label(text=text)


def create_set(context, tree, output, socket):
    from . import ui
    from .brush_detail import initialize_from_brush
    with ui.suspend_sync():
        index = list(output.inputs).index(socket)
        node = tree.nodes.new('LEALineSetNode')
        node.name = 'Line Set'
        node.target_mode = 'LISTED'
        brush = tree.nodes.new('LEABrushNode')
        brush.name = 'Brush Settings'
        detail = tree.nodes.new('LEABrushDetailNode')
        detail.name = 'Brush Detail'
        initialize_from_brush(detail, brush)
        node.location = (output.location.x - 360, output.location.y - index * 480)
        brush.location = (node.location.x - 340, node.location.y)
        detail.location = (brush.location.x - 320, brush.location.y)
        tree.links.new(detail.outputs[0], brush.inputs['Brush Detail'])
        tree.links.new(brush.outputs[0], node.inputs['Brush'])
        tree.links.new(node.outputs[0], socket)
        ensure_trailing(output)
        tree.line_index = list(tree.nodes).index(output)
        ui._activate_node(tree, node)
        if context.scene.lea_line.tree is None:
            context.scene.lea_line.tree = tree
    return node


class LEA_OT_line_socket(bpy.types.Operator):
    bl_idname = 'lea_line.line_socket'
    bl_label = 'Edit Line Set Input'
    bl_description = 'Create a Line Set, insert an input, remove it, or compact empty inputs; existing nodes are preserved'
    bl_options = {'REGISTER', 'UNDO'}
    tree_name: StringProperty()
    node_name: StringProperty()
    socket_index: IntProperty(min=0)
    action: EnumProperty(items=[('CREATE', 'New Line Set', ''), ('INSERT', 'Insert Input', ''),
                                ('REMOVE', 'Remove Input', ''), ('COMPACT', 'Remove Empty Inputs', '')])

    def execute(self, context):
        from . import ui
        tree = bpy.data.node_groups.get(self.tree_name)
        node = tree.nodes.get(self.node_name) if tree else None
        if (node is None or node.bl_idname != 'LEAOutputNode' or not tree.is_editable
                or (self.action != 'COMPACT' and self.socket_index >= len(node.inputs))):
            return {'CANCELLED'}
        with ui.suspend_sync():
            if self.action == 'CREATE':
                socket = node.inputs[self.socket_index]
                if socket.is_linked:
                    return {'CANCELLED'}
                create_set(context, tree, node, socket)
            elif self.action == 'INSERT':
                append_socket(node)
                node.inputs.move(len(node.inputs)-1, self.socket_index)
            elif self.action == 'REMOVE':
                node.inputs.remove(node.inputs[self.socket_index])
                ensure_trailing(node)
            else:
                # Keep linked inputs in their current order, then one blank.
                for socket in list(node.inputs):
                    if not socket.is_linked:
                        node.inputs.remove(socket)
                ensure_trailing(node)
            tree.line_index = list(tree.nodes).index(node)
        ui._sync_if_enabled(tree, context)
        return {'FINISHED'}


def register():
    bpy.utils.register_class(LEA_OT_line_socket)


def unregister():
    if LEA_OT_line_socket.is_registered:
        bpy.utils.unregister_class(LEA_OT_line_socket)
