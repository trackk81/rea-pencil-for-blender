"""Independent sidebar reconstruction from observed panel order and labels.

Only equivalent implemented operations are editable. Unsupported reference RNA
is owned by reference_controls and is always drawn disabled. No target package
is imported or read at runtime.
"""
import bpy

from . import ui
from . import reference_controls as ref


def tree_from(context):
    space = getattr(context, 'space_data', None)
    return getattr(space, 'edit_tree', None) if getattr(space, 'tree_type', None) == 'LEALineTree' else None


def original(context, section=None):
    tree = tree_from(context)
    return bool(tree and tree.ui_layout == 'ORIGINAL' and
                not (tree.nodes.active and tree.nodes.active.bl_idname in {'LEATextureMapNode','LEADistanceReductionNode'}) and
                (section is None or ui._panel_context(tree)[3] == section))


def output_from(tree):
    index = tree.line_index
    if 0 <= index < len(tree.nodes) and tree.nodes[index].bl_idname == 'LEAOutputNode':
        return tree.nodes[index]
    return None


def selected_set(context):
    tree = tree_from(context)
    return ui._panel_context(tree)[0] if tree else None


def brush_from(context):
    tree = tree_from(context)
    return ui._panel_context(tree)[1] if tree else None


def detail_from(context):
    tree = tree_from(context)
    return ui._panel_context(tree)[2] if tree else None


def settings_layout(layout):
    layout.use_property_split = True
    layout.use_property_decorate = False
    return layout


def branch_enabled(node):
    return node.enabled and (node.hidden if node.line_visibility == 'HIDDEN' else node.visible)


class LEA_UL_reference_lines(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, 'enabled', text='')
        row.prop(item, 'name', text='', emboss=False)

    def filter_items(self, context, data, property_name):
        nodes = list(getattr(data, property_name))
        lines = ui._line_outputs(data)
        hidden = [n for n in nodes if n not in lines]
        order = lines + hidden
        return [self.bitflag_filter_item if n in lines else 0 for n in nodes], [order.index(n) for n in nodes]


class LEA_UL_reference_line_sets(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        node = item.links[0].from_node if item.is_linked else None
        if node and node.bl_idname == 'LEALineSetNode':
            layout.prop(node, 'name', text='', emboss=False)

    def filter_items(self, context, data, property_name):
        return ui.LEA_UL_line_sets.filter_items(self, context, data, property_name)


class LEA_UL_reference_objects(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.object.name_full if item.object else '')


class LEA_UL_reference_materials(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.material.name_full if item.material else '')


class _Panel:
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'REA Pencil Line'


class LEA_PT_reference_switch(_Panel, bpy.types.Panel):
    bl_idname = 'LEA_PT_reference_switch'
    bl_label = ''
    bl_options = {'HIDE_HEADER'}
    bl_order = 0

    @classmethod
    def poll(cls, context):
        return tree_from(context) is not None

    def draw(self, context):
        tree = tree_from(context)
        layout = self.layout
        if tree.ui_layout != 'ORIGINAL':
            return
        _, brush, detail, section = ui._panel_context(tree)
        row = layout.row(align=True)
        row.alignment = 'LEFT'
        for value, label in (('LINE_LIST', 'Line List'), ('BRUSH', 'Brush Settings'), ('DETAIL', 'Brush Detail')):
            if value != 'LINE_LIST' and brush is None and detail is None:
                continue
            op = row.operator('lea_line.panel_section', text=label, icon='PRESET' if value == 'LINE_LIST' else 'NONE', depress=value == section)
            op.tree_name, op.section = tree.name, value


class LEA_PT_reference_line_list(_Panel, bpy.types.Panel):
    bl_idname = 'LEA_PT_reference_line_list'
    bl_label = 'Line List'
    bl_order = 1

    @classmethod
    def poll(cls, context):
        return original(context, 'LINE_LIST')

    def draw(self, context):
        tree = tree_from(context)
        outer = self.layout.split(factor=.1)
        outer.column()
        content = outer.split(factor=1.0).split(factor=.95)
        split = content.row().split(factor=.92)
        column = split.column()
        column.template_list('LEA_UL_reference_lines', '', tree, 'nodes', tree, 'line_index', rows=3, maxrows=3)
        buttons = column.row(align=True)
        buttons.enabled = tree.is_editable
        add = buttons.operator('lea_line.line_list_action', text='Add')
        add.tree_name, add.action = tree.name, 'ADD'
        remove = buttons.row(align=True)
        remove.enabled = output_from(tree) is not None
        op = remove.operator('lea_line.line_list_action', text='Remove')
        op.tree_name, op.action = tree.name, 'REMOVE'
        arrows = split.split(factor=1.0).column(align=True)
        arrows.separator(factor=4.0)
        lines = ui._line_outputs(tree)
        selected = output_from(tree)
        index = lines.index(selected) if selected in lines else -1
        for action, icon in (('UP', 'TRIA_UP'), ('DOWN', 'TRIA_DOWN')):
            row = arrows.row(align=True)
            row.enabled = tree.is_editable and (index > 0 if action == 'UP' else 0 <= index < len(lines) - 1)
            op = row.operator('lea_line.line_list_action', text='', icon=icon)
            op.tree_name, op.action = tree.name, action
        content.split(factor=1.0).column()
        self.layout.separator(factor=.25)


def draw_target_list(layout, node, material=False):
    layout.label(text='Materials' if material else 'Objects')
    if node:
        layout.template_list('LEA_UL_reference_materials' if material else 'LEA_UL_reference_objects',
            'reference_' + node.name + ('_mat' if material else '_obj'), node,
            'materials' if material else 'objects', node, 'material_index' if material else 'object_index', rows=5)
        row = layout.row(align=True)
        op = ui._target_operator(row, 'lea_line.target_dialog', node, 'Add', 'NONE')
        op.kind, op.action = ('MATERIAL' if material else 'OBJECT'), 'ADD'
        remove = row.row(align=True)
        remove.enabled = bool(node.materials if material else node.objects)
        op = ui._target_operator(remove, 'lea_line.target_dialog', node, 'Remove', 'NONE')
        op.kind, op.action = ('MATERIAL' if material else 'OBJECT'), 'REMOVE'
    else:
        layout.box().label(text='')
        row = layout.row(align=True)
        ref.unavailable(row, 'Add')
        ref.unavailable(row, 'Remove')


def draw_line_lists(context, layout, output, node):
    tree = tree_from(context)
    # Fixed original proportions apply even in a narrow sidebar.
    split = layout.row().split(factor=.32)
    column = split.column()
    column.label(text='Line Sets')
    column.template_list('LEA_UL_reference_line_sets', '', output, 'inputs', output, 'line_set_index', rows=5, sort_lock=True)
    buttons = column.row(align=True)
    add = buttons.row(align=True)
    add.enabled = tree.is_editable
    add.operator('lea_line.add_line_set', text='Add').tree_name = tree.name
    remove = buttons.row(align=True)
    connected = ui._connected_line_sets(output)
    remove.enabled = node in connected
    if node:
        ui._target_operator(remove, 'lea_line.disconnect_line_set', node, 'Remove', 'NONE')
    else:
        ref.unavailable(remove, 'Remove')
    rest = split.split(factor=.1)
    arrows = rest.column(align=True)
    arrows.separator(factor=12.0)
    index = connected.index(node) if node in connected else -1
    for direction, icon in (('UP', 'TRIA_UP'), ('DOWN', 'TRIA_DOWN')):
        row = arrows.row(align=True)
        row.enabled = index > 0 if direction == 'UP' else 0 <= index < len(connected) - 1
        if node:
            ui._target_operator(row, 'lea_line.move_line_set', node, '', icon).direction = direction
        else:
            ref.unavailable(row, '', icon)
    targets = rest.split(factor=1.0).row()
    draw_target_list(targets.column(), node)
    draw_target_list(targets.column(), node, True)


class LEA_PT_reference_line(_Panel, bpy.types.Panel):
    bl_idname = 'LEA_PT_reference_line'
    bl_label = ''
    bl_order = 100
    bl_options = {'HEADER_LAYOUT_EXPAND'}

    @classmethod
    def poll(cls, context):
        return original(context, 'LINE_LIST') and output_from(tree_from(context)) is not None

    def draw_header(self, context):
        node = output_from(tree_from(context))
        split = self.layout.split(factor=.5)
        split.prop(node, 'enabled', text=node.name)
        right = split.row()
        right.alignment = 'RIGHT'
        right.prop(node, 'render_priority', text='Render Priority')

    def draw(self, context):
        tree = tree_from(context)
        output = output_from(tree)
        col = settings_layout(self.layout.column(align=True))
        col.enabled = output.enabled
        draw_line_lists(context, col, output, selected_set(context))
        col.separator(factor=2.0)
        settings = context.scene.lea_line
        if settings.tree == tree:
            split = col.split(factor=.4)
            label = split.row()
            label.alignment = 'RIGHT'
            label.label(text='Line Size')
            row = split.row(align=True)
            row.use_property_split = False
            row.prop_enum(output, 'line_size_type', 'ABSOLUTE', text='Absolute')
            row.prop_enum(output, 'line_size_type', 'RELATIVE', text='Relative (640*480)')
        else:
            ref.enum(context, col, 'line', 'line_size_type', 'Line Size')
        col.separator()
        col.prop(output,'random_seed',text='Random Seed')
        col.label(text='Blender Render · F12', icon='RENDER_STILL')


class LEA_PT_reference_line_set(_Panel, bpy.types.Panel):
    bl_idname = 'LEA_PT_reference_line_set'
    bl_label = ''
    bl_order = 101
    bl_options = {'HEADER_LAYOUT_EXPAND'}

    @classmethod
    def poll(cls, context):
        return original(context, 'LINE_LIST') and selected_set(context) is not None

    def draw_header(self, context):
        node = selected_set(context)
        split = self.layout.split(factor=.5)
        split.prop(node, 'enabled', text=node.name)
        right = split.row()
        right.alignment = 'RIGHT'
        ref.prop(context, right, 'line_set', 'lineset_id', text='Line Set ID')

    def draw(self, context):
        node = selected_set(context)
        row = self.layout.row(align=True)
        row.alignment = 'LEFT'
        row.prop_enum(node, 'line_visibility', 'VISIBLE', text='Visible Lines')
        row.prop_enum(node, 'line_visibility', 'HIDDEN', text='Hidden Lines')
        hidden = node.line_visibility == 'HIDDEN'
        self.layout.prop(node, 'hidden' if hidden else 'visible',
                         text='Enable Hidden Lines' if hidden else 'Enable Visible Lines')
        if hidden:
            self.layout.prop(node, 'independent_hidden_edges', text='Independent Edge Types')
        rule = self.layout.row()
        rule.scale_y = .1
        rule.enabled = False
        rule.label(text='.' * 320)


def draw_brush(context, layout):
    brush = brush_from(context)
    if brush is None:
        return
    settings_layout(layout)
    column = layout.column(align=True)
    row = column.row(align=True)
    row.prop(brush, 'color', text='Color')
    row.prop(brush, 'alpha', text='Blend Amount', slider=True)
    draw_brush_map(context, column, 'color_map', 'Color Map', 'color_map_opacity', 'Opacity')
    column = layout.column(align=True)
    column.prop(brush, 'thickness', text='Size')
    draw_brush_map(context, column, 'size_map', 'Size Map', 'size_map_amount', 'Amount')
    if detail_from(context):
        column = layout.column(align=True)
        ref.prop(context, column, 'detail', 'stretch', text='Stretch')
        ref.prop(context, column, 'detail', 'angle', text='Angle')


def draw_brush_map(context, layout, prefix, title, value, value_label):
    brush=brush_from(context)
    if brush:
        from . import texture_maps
        texture_maps.draw_brush_map(context,layout,brush,'color' if prefix=='color_map' else 'size')
        return
    split = layout.row().split(factor=.4)
    split.use_property_split = False
    label = split.row(align=True)
    label.alignment = 'RIGHT'
    label.enabled = False
    label.prop(ref.data(context, 'brush'), prefix + '_on_gui', text='')
    label.label(text=title)
    label.label(text='', icon='TEXTURE')
    controls = split.split(factor=1.0).row(align=True)
    controls.enabled = False
    controls.operator('lea_line.reference_unavailable', text='None')
    controls.prop(ref.data(context, 'brush'), value, text=value_label)


class LEA_PT_reference_set_brush(_Panel, bpy.types.Panel):
    bl_idname = 'LEA_PT_reference_set_brush'
    bl_parent_id = 'LEA_PT_reference_line_set'
    bl_label = ''
    bl_order = 1
    bl_options = {'HEADER_LAYOUT_EXPAND'}

    @classmethod
    def poll(cls, context):
        return LEA_PT_reference_line_set.poll(context)

    def draw_header(self, context):
        self.layout.label(text='Brush')
        brush = brush_from(context)
        if brush:
            right = self.layout.row()
            right.alignment = 'RIGHT'
            detail = ui._linked_node(brush, 'Brush Detail', 'LEABrushDetailNode')
            ui._target_operator(right, 'lea_line.detail_connection', brush, 'Brush Detail', 'TRACKING').action = 'SELECT' if detail else 'CREATE'

    def draw(self, context):
        self.layout.enabled = branch_enabled(selected_set(context))
        draw_brush(context, self.layout)


EDGE_ROWS = (
    ('outline', 'Outline', 'outline', (('outline_open', 'Open Edge'), ('outline_merge_groups', 'Merge Groups'))),
    ('object', 'Object', None, (('object_open', 'Open Edge'),)),
    ('intersection', 'Intersection', 'intersection', (('intersection_self', 'Self-Intersection'),)),
    ('smooth', 'Smoothing Boundary', None, ()),
    ('material', 'Material ID Boundary', 'material_boundary', ()),
    ('selected', 'Selected Edges', 'edge_mark', ()),
    ('normal_angle', 'Normal Angle', 'crease', (('normal_angle_min', 'Min'), ('normal_angle_max', 'Max'))),
    ('wireframe', 'Wireframe', None, ()),
)


class LEA_PT_reference_edge(_Panel, bpy.types.Panel):
    bl_idname = 'LEA_PT_reference_edge'
    bl_parent_id = 'LEA_PT_reference_line_set'
    bl_label = 'Edge'
    bl_order = 2

    @classmethod
    def poll(cls, context):
        return LEA_PT_reference_line_set.poll(context)

    def draw(self, context):
        node = selected_set(context)
        hidden = node.line_visibility == 'HIDDEN'
        prefix = 'h_' if hidden else 'v_'
        column = self.layout.column()
        column.enabled = branch_enabled(node)
        # Hidden categories intentionally remain shared until the explicit
        # independent-hidden selection switch in Extensions is enabled.
        for source, label, supported, children in EDGE_ROWS:
            prop = ('hidden_' if hidden and node.independent_hidden_edges else '') + supported if supported else None
            if prop:
                toggle = column.row()
                toggle.enabled = not hidden or node.independent_hidden_edges
                toggle.prop(node, prop, text=label)
            else:
                ref.prop(context, column, 'line_set', prefix + source + '_on', text=label)
            indent = column.split(factor=.02)
            indent.column()
            box = indent.split(factor=1.0).box().column(align=True)
            box.use_property_split = False
            box.enabled = bool(getattr(node, prop)) if prop else False
            for child, caption in children:
                if child == 'normal_angle_min':
                    control = box.row()
                    control.enabled = not hidden or node.independent_hidden_edges
                    control.prop(node, 'normal_angle_min', text=caption)
                else:
                    ref.prop(context, box, 'line_set', prefix + child, text=caption)
            if supported in ui.OVERRIDE_FEATURES:
                socket = ('Hidden ' if hidden else '') + supported + ' Brush'
                direct = ui._linked_node(node, socket, 'LEABrushNode')
                row = box.row(align=True)
                ui._brush_action(row, node, socket, 'UNLINK' if direct else 'CREATE', 'Specific Brush Settings',
                                 'CHECKBOX_HLT' if direct else 'CHECKBOX_DEHLT')
                if direct:
                    ui._brush_action(row, node, socket, 'SELECT', direct.name, 'TRACKING')
                else:
                    ref.unavailable(row)
            else:
                ref.node_link(context, box, 'line_set', prefix + source + '_specific_on_gui', 'Specific Brush Settings')
        column.separator(factor=3.0)
        ref.prop(context, column, 'line_set', 'is_weld_edges', text='Weld Edges Between Objects')
        column.prop(node,'is_mask_hidden_lines',text='Mask Hidden Lines of Other Line Sets')


class LEA_PT_reference_reduction(_Panel, bpy.types.Panel):
    bl_idname = 'LEA_PT_reference_reduction'
    bl_parent_id = 'LEA_PT_reference_line_set'
    bl_label = 'Reduction'
    bl_order = 3

    @classmethod
    def poll(cls, context):
        return LEA_PT_reference_line_set.poll(context)

    def draw(self, context):
        node = selected_set(context)
        from . import independent_reduction
        column = self.layout.column()
        column.enabled = node.enabled and (node.hidden if node.line_visibility == 'HIDDEN' else node.visible)
        independent_reduction.draw(column, node)


class LEA_PT_reference_brush(_Panel, bpy.types.Panel):
    bl_idname = 'LEA_PT_reference_brush'
    bl_label = 'Brush'
    bl_order = 100

    @classmethod
    def poll(cls, context):
        return original(context, 'BRUSH') and brush_from(context) is not None

    def draw(self, context):
        draw_brush(context, self.layout)


class LEA_PT_reference_detail_empty(_Panel, bpy.types.Panel):
    bl_idname = 'LEA_PT_reference_detail_empty'
    bl_label = ''
    bl_options = {'HIDE_HEADER'}
    bl_order = 100

    @classmethod
    def poll(cls, context):
        return original(context, 'DETAIL') and detail_from(context) is None

    def draw(self, context):
        brush = brush_from(context)
        if brush:
            ui._target_operator(self.layout, 'lea_line.detail_connection', brush, 'Create Brush Detail', 'ADD').action = 'CREATE'


class LEA_PT_reference_extensions(_Panel, bpy.types.Panel):
    bl_idname = 'LEA_PT_reference_extensions'
    bl_label = 'rea Extensions'
    bl_order = 900
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return original(context)

    def draw(self, context):
        tree = tree_from(context)
        node, brush, detail, section = ui._panel_context(tree)
        layout = settings_layout(self.layout)
        if section == 'DETAIL' and detail:
            from . import reference_detail
            reference_detail.draw_body(context, layout, detail, 'extensions')
        if node:
            column = layout.column(align=True)
            column.prop(node, 'render_priority', text='Line Set Render Order')
            column.prop(node,'use_material_functions')
            column.prop(node, 'source_collection', text='Collection')
            row = column.row(align=True)
            row.prop(node, 'visible', text='Visible Lines')
            row.prop(node, 'hidden', text='Hidden Lines')
            column.prop(node, 'independent_hidden_edges', text='Independent Hidden Edges')
            column.label(text='Native Edge Categories')
            hidden = node.line_visibility == 'HIDDEN' and node.independent_hidden_edges
            for prop, label in (('border', 'Open Border'), ('crease', 'Crease'), ('ridge_valley', 'Ridge / Valley'), ('suggestive_contour', 'Suggestive Contour')):
                toggle = column.row()
                toggle.enabled = node.line_visibility != 'HIDDEN' or node.independent_hidden_edges
                toggle.prop(node, ('hidden_' if hidden else '') + prop, text=label)
            column.prop(node, 'crease_angle', text='Native Crease Angle')
            ui._draw_brush_link(column, node, 'Hidden Brush' if node.line_visibility == 'HIDDEN' else 'Brush')
            row = column.row(align=True)
            ui._target_operator(row, 'lea_line.duplicate_line_set', node, 'Duplicate Line Set', 'DUPLICATE')
            ui._target_operator(row, 'lea_line.disconnect_line_set', node, 'Disconnect', 'UNLINKED')
        if brush:
            box = layout.box()
            box.prop(brush,'linear_alpha',text='Linear Alpha (Final)')
            box.label(text='Distance Endpoints')
            ui._draw_reduction_controls(context, box, brush)
        if context.scene.lea_line.tree == tree:
            if context.scene.lea_line.width_mode == 'RELATIVE':
                layout.prop(context.scene.lea_line, 'reference_height')
            ui._draw_output_settings(context, layout, tree, runtime=True)
        drafts = [n for n in tree.nodes if n.bl_idname == 'LEALineSetNode' and n not in ui._connected_line_sets(output_from(tree))]
        if drafts:
            layout.label(text='Disconnected Drafts')
            for draft in drafts:
                ui._target_operator(layout, 'lea_line.select_line_set', draft, draft.name, 'NODE')


CLASSES = [LEA_UL_reference_lines, LEA_UL_reference_line_sets, LEA_UL_reference_objects, LEA_UL_reference_materials, LEA_PT_reference_switch,
    LEA_PT_reference_line_list, LEA_PT_reference_line, LEA_PT_reference_line_set,
    LEA_PT_reference_set_brush, LEA_PT_reference_edge, LEA_PT_reference_reduction,
    LEA_PT_reference_brush, LEA_PT_reference_detail_empty, LEA_PT_reference_extensions]
DETAIL_CLASSES = []


def _detail_panel_functions(key, enabled_property):
    from . import reference_detail
    def poll(cls, context):
        return original(context, 'DETAIL') and detail_from(context) is not None
    def draw_header(self, context):
        if enabled_property:
            self.layout.prop(detail_from(context), enabled_property, text='')
    def draw(self, context):
        detail = detail_from(context)
        self.layout.enabled = detail.enabled and (getattr(detail, enabled_property) if enabled_property else True)
        reference_detail.draw_body(context, self.layout, detail, key)
    return poll, draw_header, draw


def register():
    from . import reference_detail
    for index, (key, title, enabled_property) in enumerate(reference_detail.SECTION_SPECS):
        if key == 'extensions':
            continue
        poll, draw_header, draw = _detail_panel_functions(key, enabled_property)
        cls = type('LEA_PT_reference_detail_' + key, (_Panel, bpy.types.Panel), {
            'bl_idname': 'LEA_PT_reference_detail_' + key, 'bl_label': title, 'bl_order': 100 + index,
            'poll': classmethod(poll), 'draw_header': draw_header, 'draw': draw,
        })
        DETAIL_CLASSES.append(cls)
    for cls in (*CLASSES, *DETAIL_CLASSES):
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed((*CLASSES, *DETAIL_CLASSES)):
        bpy.utils.unregister_class(cls)
    DETAIL_CLASSES.clear()
