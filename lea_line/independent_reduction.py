"""Line Set reduction channels. Blender IDs are resolved only during preparation."""
import bpy
from bpy.props import StringProperty, EnumProperty

SOCKETS = ('V Size Reduction', 'V Alpha Reduction', 'H Size Reduction', 'H Alpha Reduction')
_choice_items = []  # RNA dynamic enum strings must remain alive while its dialog is open.


def connected_channels(node):
    return {link.to_socket.name for socket in node.outputs for link in socket.links
            if link.to_node.bl_idname == 'LEALineSetNode' and link.to_socket.name in SOCKETS}


def draw_channel_settings(layout, node):
    names = connected_channels(node)
    if not names: return False
    layout.label(text='Channel follows the Line Set connection', icon='INFO')
    if not node.curve_enabled:
        if any('Size' in name for name in names): layout.prop(node, 'far_thickness', text='Far Size')
        if any('Alpha' in name for name in names): layout.prop(node, 'far_alpha', text='Far Alpha')
    return True


def property_name(socket):
    return socket.lower().replace(' ', '_') + '_on'


def linked(node, name):
    socket = node.inputs.get(name)
    if not socket or not socket.is_linked:
        return None
    result = socket.links[0].from_node
    if result.bl_idname != 'LEADistanceReductionNode':
        raise ValueError(f'{node.name}: {name} requires a Reduction Settings node')
    return result


def configured(node, visibility):
    prefix = 'H ' if visibility == 'HIDDEN' else 'V '
    return any(getattr(node, property_name(prefix + kind + ' Reduction'), False)
               or (node.inputs.get(prefix + kind + ' Reduction') and node.inputs[prefix + kind + ' Reduction'].is_linked)
               for kind in ('Size', 'Alpha'))


def apply(node, brush, visibility):
    if not configured(node, visibility):
        return brush
    from . import routing, distance_curves
    channels = {}
    for kind, flag, far in (('SIZE', 'distance_fade', 'far_thickness'), ('ALPHA', 'distance_alpha', 'far_alpha')):
        name = ('H ' if visibility == 'HIDDEN' else 'V ') + kind.title() + ' Reduction'
        source = linked(node, name)
        on = getattr(node, property_name(name), False)
        explicit = source is not None or on
        enabled = bool(on and source and source.enabled) if explicit else bool(getattr(brush, flag, False))
        spec = {'enabled': enabled, 'near': float(source.distance_start if explicit and source else brush.distance_start),
                'far': float(source.distance_end if explicit and source else brush.distance_end),
                'factor': float(getattr(source, far) if explicit and source else getattr(brush, far)),
                'reference': bool(source.refer_object_on if explicit and source else getattr(brush, 'distance_reference_enabled', False)),
                'object': source.object_reference if explicit and source else getattr(brush, 'distance_reference_object', None),
                'data': None, 'profile': None}
        if enabled:
            if spec['far'] <= spec['near']:
                raise ValueError(f'{node.name}: {name} End must be greater than Start')
            if explicit and source.curve_enabled:
                spec['data'], spec['profile'] = distance_curves.resolve(source)
            elif not explicit:
                spec['data'] = getattr(brush, 'distance_curve_data', None)
                spec['profile'] = getattr(brush, 'distance_curve_table', None)
        channels[kind] = spec
    return routing.ResolvedBrush(brush, {
        'distance_independent': True, 'distance_channels': channels,
        'distance_fade': channels['SIZE']['enabled'], 'distance_alpha': channels['ALPHA']['enabled'],
        'distance_start': 0., 'distance_end': 1., 'far_thickness': 1., 'far_alpha': 1.,
        'distance_reference_enabled': False, 'distance_curve_data': None, 'distance_curve_table': None})


def draw(layout, node):
    prefix = 'H ' if node.line_visibility == 'HIDDEN' else 'V '
    for kind in ('Size', 'Alpha'):
        name = prefix + kind + ' Reduction'
        row = layout.row(align=True)
        row.prop(node, property_name(name), text=kind + ' Reduction')
        controls = row.row(align=True); controls.enabled = getattr(node, property_name(name))
        target = linked(node, name)
        def button(action, text, icon):
            op = controls.operator('lea_line.channel_reduction', text=text, icon=icon)
            op.tree_name = node.id_data.name; op.node_name = node.name; op.socket_name = name; op.action = action
        button('SELECT' if target else 'CREATE', target.name if target else 'None', 'TRACKING' if target else 'ADD')
        button('PICK', '', 'DOWNARROW_HLT')
        if target: button('UNLINK', '', 'X')


def choices(self, context):
    global _choice_items
    tree = bpy.data.node_groups.get(self.tree_name)
    _choice_items = [(n.name, n.name, 'Connect existing Reduction Settings') for n in tree.nodes
                     if n.bl_idname == 'LEADistanceReductionNode'] if tree else []
    return _choice_items


class LEA_OT_channel_reduction(bpy.types.Operator):
    bl_idname = 'lea_line.channel_reduction'; bl_label = 'Reduction Settings'; bl_options = {'REGISTER', 'UNDO'}
    tree_name: StringProperty(); node_name: StringProperty(); socket_name: StringProperty()
    action: EnumProperty(items=[(x, x.title(), '') for x in ('CREATE', 'SELECT', 'UNLINK', 'PICK')])
    target: EnumProperty(items=choices)

    def invoke(self, context, event):
        if self.action == 'PICK': return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def draw(self, context): self.layout.prop(self, 'target', text='Reduction Settings')

    def execute(self, context):
        from . import ui
        tree = bpy.data.node_groups.get(self.tree_name)
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'LEALineSetNode' or self.socket_name not in SOCKETS:
            return {'CANCELLED'}
        with ui.suspend_sync():
            socket = node.inputs.get(self.socket_name)
            current = linked(node, self.socket_name)
            if self.action == 'UNLINK':
                for link in list(socket.links): tree.links.remove(link)
                setattr(node, property_name(self.socket_name), False)
            elif self.action == 'SELECT':
                if not current: return {'CANCELLED'}
                ui._activate_node(tree, current)
            else:
                current = tree.nodes.get(self.target) if self.action == 'PICK' else tree.nodes.new('LEADistanceReductionNode')
                if not current or current.bl_idname != 'LEADistanceReductionNode': return {'CANCELLED'}
                if self.action == 'CREATE':
                    current.name = self.socket_name[2:] + ' Settings'; current.curve_enabled = True
                    current.location = (node.location.x - 300, node.location.y - (150 if 'Alpha' in self.socket_name else 0))
                tree.links.new(current.outputs[0], socket)
                setattr(node, property_name(self.socket_name), True)
                ui._activate_node(tree, current)
        ui._sync_if_enabled(node, context)
        return {'FINISHED'}


def sync_live(graph, brush, setter):
    """Native per-point fields; no Python geometry access on redraw."""
    from . import curves
    channels = getattr(brush, 'distance_channels', None)
    count = changed = 0
    for kind, target, fallback in (('SIZE', 'Reduced Radius', 'Distance Size Factor'),
                                   ('ALPHA', 'Reduced Opacity', 'Distance Alpha Factor')):
        prefix = '.rea channel ' + kind + ' '
        switch = graph.nodes.get(prefix + 'switch')
        if channels is None and switch is None: continue
        if switch is None:
            def new(type_id, name):
                nonlocal count
                n = graph.nodes.new(type_id); n.name = prefix + name; count += 1; return n
            def math(op, name, a, b):
                n = new('ShaderNodeMath', name); n.operation = op
                for i, value in enumerate((a, b)):
                    if isinstance(value, (float, int)): n.inputs[i].default_value = value
                    else: graph.links.new(value, n.inputs[i])
                return n.outputs[0]
            old = graph.nodes[target].inputs[1].links[0].from_socket
            switch = new('GeometryNodeSwitch', 'switch'); switch.input_type = 'FLOAT'
            graph.links.new(old, switch.inputs['False']); graph.links.new(switch.outputs[0], graph.nodes[target].inputs[1])
            info = new('GeometryNodeObjectInfo', 'reference'); info.transform_space = 'RELATIVE'
            origin = new('GeometryNodeSwitch', 'origin'); origin.input_type = 'VECTOR'
            # The shared distance input can have a disabled reference switch.
            camera = graph.nodes['Camera Distance'].inputs[1].links[0].from_socket
            shared = graph.nodes.get('.rea reduction origin')
            if shared: camera = shared.inputs['False'].links[0].from_socket
            graph.links.new(camera, origin.inputs['False']); graph.links.new(info.outputs['Location'], origin.inputs['True'])
            distance = new('ShaderNodeVectorMath', 'distance'); distance.operation = 'DISTANCE'
            graph.links.new(graph.nodes['Camera Distance'].inputs[0].links[0].from_socket, distance.inputs[0])
            graph.links.new(origin.outputs[0], distance.inputs[1])
            near = new('ShaderNodeValue', 'near'); span = new('ShaderNodeValue', 'span')
            t = math('SUBTRACT', 'subtract', distance.outputs['Value'], near.outputs[0])
            t = math('DIVIDE', 'divide', t, span.outputs[0]); t = math('MAXIMUM', 'min', t, 0.); t = math('MINIMUM', 'max', t, 1.)
            curve = new('ShaderNodeFloatCurve', 'curve'); graph.links.new(t, curve.inputs['Value'])
            profile = math('MAXIMUM', 'curve min', curve.outputs[0], 0.); profile = math('MINIMUM', 'curve max', profile, 1.)
            delta = new('ShaderNodeValue', 'delta')
            linear = math('MULTIPLY', 'linear delta', t, delta.outputs[0]); linear = math('ADD', 'linear factor', linear, 1.)
            choose = new('GeometryNodeSwitch', 'profile'); choose.input_type = 'FLOAT'
            graph.links.new(linear, choose.inputs['False']); graph.links.new(profile, choose.inputs['True'])
            enabled = new('GeometryNodeSwitch', 'enabled'); enabled.input_type = 'FLOAT'; enabled.inputs['False'].default_value = 1.
            graph.links.new(choose.outputs[0], enabled.inputs['True']); graph.links.new(enabled.outputs[0], switch.inputs['True'])
        from . import distance_curves
        shared = graph.nodes.get(distance_curves.SIZE if kind == 'SIZE' else distance_curves.ALPHA)
        source = shared.outputs[0] if shared else graph.nodes[fallback].outputs[0]
        if not switch.inputs['False'].is_linked or switch.inputs['False'].links[0].from_socket != source:
            graph.links.new(source, switch.inputs['False']); count += 1
        if not graph.nodes[target].inputs[1].is_linked or graph.nodes[target].inputs[1].links[0].from_socket != switch.outputs[0]:
            graph.links.new(switch.outputs[0], graph.nodes[target].inputs[1]); count += 1
        setter(switch.inputs['Switch'], 'default_value', channels is not None)
        if channels is None: continue
        spec = channels[kind]; on = spec['enabled']
        setter(graph.nodes[prefix+'enabled'].inputs['Switch'], 'default_value', on)
        setter(graph.nodes[prefix+'reference'].inputs['Object'], 'default_value', spec['object'] if on and spec['reference'] else None)
        setter(graph.nodes[prefix+'origin'].inputs['Switch'], 'default_value', on and spec['reference'])
        setter(graph.nodes[prefix+'near'].outputs[0], 'default_value', spec['near'])
        setter(graph.nodes[prefix+'span'].outputs[0], 'default_value', max(1e-20, spec['far']-spec['near']))
        setter(graph.nodes[prefix+'delta'].outputs[0], 'default_value', spec['factor']-1.)
        setter(graph.nodes[prefix+'profile'].inputs['Switch'], 'default_value', spec['data'] is not None)
        if spec['data'] is not None: changed += curves.apply_mapping(graph.nodes[prefix+'curve'].mapping, spec['data'])
    if changed: graph.update_tag()
    return count, changed
