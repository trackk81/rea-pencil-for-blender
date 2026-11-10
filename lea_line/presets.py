"""Data-only presets stored with a line graph in the .blend document."""
import json
import math
import uuid
from pathlib import Path
import bpy
from bpy.props import EnumProperty, StringProperty
from .brush_detail import LEGACY_FIELDS, EXTRA_FIELDS
from .stamp_brush import FIELDS as STAMP_FIELDS, PREVIEW_PATTERN

CURVES_KEY = '_curves'
CURVE_FLAGS = {'size_curve_enabled', 'alpha_curve_enabled'}
LOOP_FIELDS = {'loop_start', 'loop_orientation', 'dash_phase'}
DIRECTION_FIELDS = {'line_direction_enabled', 'line_direction'}

STORE_KEY = '_rea_detail_presets_v1'
PENCIL_ASSET = '_rea_pencil_tip'
PENCIL_FIELDS = tuple(name for name in STAMP_FIELDS if not name.startswith('distortion_'))
PENCIL_TIPS = {
    'graphite_grain': ('Graphite · Grain', {'brush_type': 'NORMAL', 'brush_map_opacity': .92}),
    'dry_charcoal': ('Charcoal · Dry', {'brush_type': 'MULTIPLE', 'stamp_size': 32,
        'stamp_size_random': 40, 'horizontal_space': .16, 'vertical_space': .13,
        'horizontal_space_random': 45, 'vertical_space_random': 55}),
    'soft_graphite': ('Graphite · Soft', {'brush_type': 'NORMAL', 'brush_map_opacity': .86,
        'stretch': -.16}),
}
PENCIL_PATTERN = {'graphite_grain': (.35, .10), 'dry_charcoal': (.37, .13), 'soft_graphite': (.28, .045)}
GROUPS = {'BRUSH': ('caps',),
          'STROKE': tuple(field for field in LEGACY_FIELDS if field != 'caps') + EXTRA_FIELDS}
BUILTINS = {
    'BRUSH': {'round': ('Round', {'caps': 'ROUND'}),
              'flat': ('Flat', {'caps': 'BUTT'}),
              'square': ('Square', {'caps': 'SQUARE'}),
              **PENCIL_TIPS},
    'STROKE': {'solid': ('Solid', {}),
               'dashed': ('Dashed', {'dashed': True, 'dash': 10, 'gap': 5}),
               'long_dashes': ('Long Dashes', {'dashed': True, 'dash': 24, 'gap': 8}),
               'wave': ('Wave', {'distortion_enabled': True, 'distortion_amplitude': 5,
                                 'distortion_wavelength': 100}),
               'taper': ('End Taper', {'taper': True, 'taper_start': .05, 'taper_end': .05})},
}


def _tip_id(group, preset_id):
    if group == 'BRUSH' and preset_id.startswith('builtin:') and preset_id[8:] in PENCIL_TIPS:
        return preset_id[8:]
    return None


def _linked_tip(detail):
    socket = detail.inputs.get('Brush Map')
    if not socket or not socket.is_linked:
        return None
    node = socket.links[0].from_node
    if node.bl_idname == 'LEATextureMapNode' and node.image and node.get(PENCIL_ASSET) == node.image.get(PENCIL_ASSET):
        return node.get(PENCIL_ASSET)
    return None


def _connect_tip(detail, tip):
    socket = detail.inputs.get('Brush Map')
    if socket is None:
        raise ValueError('Brush Detail is missing its Brush Map input')
    tree = detail.id_data
    node = next((n for n in tree.nodes if n.bl_idname == 'LEATextureMapNode' and n.get(PENCIL_ASSET) == tip
                 and n.image and n.image.get(PENCIL_ASSET) == tip), None)
    if node is not None:
        if not socket.is_linked or socket.links[0].from_node != node:
            tree.links.new(node.outputs[0], socket)
        return
    image = next((image for image in bpy.data.images if image.get(PENCIL_ASSET) == tip), None)
    created_image = image is None
    node = None
    try:
        if created_image:
            path = Path(__file__).with_name('brush_tips') / (tip + '.png')
            image = bpy.data.images.load(str(path), check_existing=False)
            image.name = 'REA ' + PENCIL_TIPS[tip][0]
            image.colorspace_settings.name = 'Non-Color'
            image.pack()
            image[PENCIL_ASSET] = tip
            image[PREVIEW_PATTERN] = PENCIL_PATTERN[tip]
        node = tree.nodes.new('LEATextureMapNode')
        node[PENCIL_ASSET] = tip
        node.name = PENCIL_TIPS[tip][0]
        node.location = (detail.location.x-290, detail.location.y)
        node.image = image
        node.wrap_mode_u = node.wrap_mode_v = 'CLIP'
        tree.links.new(node.outputs[0], socket)
    except Exception:
        if node is not None:
            tree.nodes.remove(node)
        if created_image and image is not None and image.users == 0:
            bpy.data.images.remove(image)
        raise


def _fields(group):
    if group not in GROUPS:
        raise ValueError('Unknown preset group')
    return GROUPS[group]


def _selection_key(group):
    _fields(group)
    return '_rea_preset_' + group.lower()


def _defaults(detail, group):
    values = {name: detail.bl_rna.properties[name].default for name in _fields(group)}
    if group == 'STROKE':
        from . import curves
        values[CURVES_KEY] = curves.default_payload()
    return values


def snapshot(detail, group):
    values = {name: getattr(detail, name) for name in _fields(group)}
    if group == 'STROKE':
        from . import curves
        values[CURVES_KEY] = curves.export_detail(detail)
    return values


def _read(tree):
    raw = tree.get(STORE_KEY, '[]')
    if not isinstance(raw, str) or len(raw) > 1024 * 1024:
        raise ValueError('Invalid preset library')
    items = json.loads(raw)
    if not isinstance(items, list) or len(items) > 256:
        raise ValueError('Preset library must contain at most 256 entries')
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != {'id', 'name', 'group', 'values'}
                or not isinstance(item['id'], str) or not item['id'].startswith('user:')
                or item['id'] in seen or not isinstance(item['name'], str)
                or not item['name'].strip() or len(item['name']) > 80
                or item['group'] not in GROUPS or not isinstance(item['values'], dict)):
            raise ValueError('Invalid preset entry')
        seen.add(item['id'])
    return items


def _require_detail(tree_name, node_name):
    tree = bpy.data.node_groups.get(tree_name)
    node = tree.nodes.get(node_name) if tree and tree.bl_idname == 'LEALineTree' else None
    if node is None or node.bl_idname != 'LEABrushDetailNode':
        raise ValueError('Brush Detail is no longer available')
    if not tree.is_editable:
        raise ValueError('This linked graph is read-only')
    return node


def _validated(detail, group, values, *, tip=None):
    if not isinstance(values, dict):
        raise ValueError('Preset fields do not match the supported group')
    scalar = {name: value for name, value in values.items() if name != CURVES_KEY}
    expected = set(_fields(group))
    if tip:
        expected.update(PENCIL_FIELDS)
    # Accept the exact scalar generations shipped in 0.8, 0.9-0.12 and 0.13.
    # Arbitrary missing-field combinations must not silently become presets.
    historical = (expected - DIRECTION_FIELDS,
                  expected - DIRECTION_FIELDS - LOOP_FIELDS,
                  expected - DIRECTION_FIELDS - LOOP_FIELDS - CURVE_FLAGS)
    if group == 'STROKE' and set(scalar) in historical:
        scalar.update({name: detail.bl_rna.properties[name].default
                       for name in expected - set(scalar)})
    if set(scalar) != expected or (CURVES_KEY in values and group != 'STROKE'):
        raise ValueError('Preset fields do not match the supported group')
    for name, value in scalar.items():
        prop = detail.bl_rna.properties[name]
        if prop.type == 'BOOLEAN':
            valid = type(value) is bool
        elif prop.type == 'ENUM':
            valid = isinstance(value, str) and value in prop.enum_items.keys()
        elif prop.type in {'INT', 'FLOAT'}:
            valid = (type(value) in ({int} if prop.type == 'INT' else {int, float})
                     and math.isfinite(value) and prop.hard_min <= value <= prop.hard_max)
        else:
            valid = False
        if not valid:
            raise ValueError('Invalid preset value: ' + name)
    if CURVES_KEY in values:
        from . import curves
        scalar[CURVES_KEY] = curves.validate_payload(values[CURVES_KEY])
    return scalar


def preset_values(detail, group, preset_id):
    _fields(group)
    if not isinstance(preset_id, str):
        raise ValueError('Invalid preset identifier')
    if preset_id == 'default':
        return _defaults(detail, group)
    if preset_id.startswith('builtin:'):
        entry = BUILTINS[group].get(preset_id[8:])
        if not entry:
            raise ValueError('Preset is no longer available')
        defaults = _defaults(detail, group)
        if _tip_id(group, preset_id):
            defaults.update({name: detail.bl_rna.properties[name].default for name in PENCIL_FIELDS})
            defaults['brush_map_on'] = True
        return {**defaults, **entry[1]}
    item = next((item for item in _read(detail.id_data)
                 if item['id'] == preset_id and item['group'] == group), None)
    if item is None:
        raise ValueError('Preset is no longer available')
    return _validated(detail, group, item['values'])


def _animated_paths(animation, paths):
    if not animation:
        return False
    if any(curve.data_path in paths for curve in animation.drivers):
        return True
    def action_hits(action):
        if not action:
            return False
        if any(curve.data_path in paths for curve in action.fcurves):
            return True
        return any(curve.data_path in paths for layer in action.layers for strip in layer.strips
                   for bag in getattr(strip, 'channelbags', ()) for curve in bag.fcurves)
    def strip_hits(strip):
        return action_hits(getattr(strip, 'action', None)) or any(strip_hits(child) for child in getattr(strip, 'strips', ()))
    return action_hits(animation.action) or any(strip_hits(strip) for track in animation.nla_tracks for strip in track.strips)


def apply_preset(detail, group, preset_id):
    from . import ui
    raw = preset_values(detail, group, preset_id)
    tip = _tip_id(group, preset_id)
    values = _validated(detail, group, raw, tip=tip)
    scalar = {name: value for name, value in values.items() if name != CURVES_KEY}
    if group == 'BRUSH' and preset_id == 'default' and _linked_tip(detail):
        scalar.update(brush_type='SIMPLE', brush_map_on=False)
    # A preset must not appear applied while a driver/animation overrides it.
    if any(detail.is_property_readonly(name) for name in scalar):
        raise ValueError('Preset contains read-only properties')
    animation = detail.id_data.animation_data
    paths = {detail.path_from_id(name) for name in scalar}
    if _animated_paths(animation, paths):
        raise ValueError('Apply presets on an unanimated Detail; animation is preserved')
    if CURVES_KEY in values:
        from . import curves
        curves.validate_import_target(detail)
    before = {name: getattr(detail, name) for name in scalar}
    with ui.suspend_sync():
        try:
            for name, value in scalar.items():
                setattr(detail, name, value)
            if CURVES_KEY in values:
                from . import curves
                curves.import_detail(detail, values[CURVES_KEY])
            if tip:
                _connect_tip(detail, tip)
        except Exception:
            for name, value in before.items():
                setattr(detail, name, value)
            raise
        detail[_selection_key(group)] = preset_id
    ui._sync_if_enabled(detail)


def add_preset(detail, group, name):
    from . import ui
    tree = detail.id_data
    items = _read(tree)
    if len(items) >= 256:
        raise ValueError('This graph already has 256 saved presets')
    name = name.strip()[:80]
    if not name:
        raise ValueError('Enter a preset name')
    names = {item['name'] for item in items if item['group'] == group}
    base, count = name, 1
    while name in names:
        name = base[:74] + '.' + str(count).zfill(3)
        count += 1
    item = {'id': 'user:' + uuid.uuid4().hex, 'name': name, 'group': group,
            'values': _validated(detail, group, snapshot(detail, group))}
    items.append(item)
    with ui.suspend_sync():
        tree[STORE_KEY] = json.dumps(items, allow_nan=False, separators=(',', ':'))
        tree.use_fake_user = True
        detail[_selection_key(group)] = item['id']
    return item['id']


def remove_preset(detail, group, preset_id):
    from . import ui
    tree = detail.id_data
    items = _read(tree)
    remaining = [item for item in items if not (item['id'] == preset_id and item['group'] == group)]
    if len(remaining) == len(items):
        raise ValueError('Only a saved user preset can be removed')
    with ui.suspend_sync():
        tree[STORE_KEY] = json.dumps(remaining, allow_nan=False, separators=(',', ':'))
        for node in tree.nodes:
            if node.bl_idname == 'LEABrushDetailNode' and node.get(_selection_key(group)) == preset_id:
                del node[_selection_key(group)]


def _selected_label(detail, group):
    selected = detail.get(_selection_key(group), 'default')
    if not isinstance(selected, str):
        selected = 'custom'
    if selected == 'default':
        label = 'Default'
    elif selected.startswith('builtin:'):
        label = BUILTINS[group].get(selected[8:], ('Custom', {}))[0]
    else:
        label = next((item['name'] for item in _read(detail.id_data)
                      if item['id'] == selected and item['group'] == group), 'Custom')
    try:
        values = preset_values(detail, group, selected)
        current = snapshot(detail, group)
        tip = _tip_id(group, selected)
        if tip:
            current.update({name: getattr(detail, name) for name in PENCIL_FIELDS})
        def equivalent(left, right):
            if isinstance(right, dict):
                return (isinstance(left, dict) and left.keys() == right.keys()
                        and all(equivalent(left[key], value) for key, value in right.items()))
            if isinstance(right, (list, tuple)):
                return (isinstance(left, (list, tuple)) and len(left) == len(right)
                        and all(equivalent(a, b) for a, b in zip(left, right)))
            return (math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-6)
                    if type(right) in {int, float} and type(left) in {int, float} else left == right)
        equal = (not tip or tip == _linked_tip(detail)) and all(
            equivalent(current[name], value) for name, value in values.items())
    except ValueError:
        equal = False
    return label if equal else label + ' *'


def _target(operator, detail, group):
    operator.tree_name = detail.id_data.name
    operator.node_name = detail.name
    operator.group = group
    return operator


def _redraw(context):
    # Saving/removing ID-property data has no render update callback. Refresh
    # labels and remove-button state without triggering scene synchronization.
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'NODE_EDITOR':
                area.tag_redraw()


def draw_presets(layout, detail, group='STROKE'):
    box = layout.box()
    box.use_property_split = False
    box.label(text='Brush Presets' if group == 'BRUSH' else 'Stroke Presets')
    row = box.row(align=True)
    row.context_pointer_set('lea_preset_detail', detail)
    try:
        label = _selected_label(detail, group)
        items = _read(detail.id_data)
    except (ValueError, TypeError):
        box.label(text='Preset data needs repair', icon='ERROR')
        return
    row.menu('LEA_MT_' + group.lower() + '_presets', text=label)
    _target(row.operator('lea_line.preset_add', text='', icon='ADD'), detail, group)
    remove = row.row(align=True)
    selected = detail.get(_selection_key(group), '')
    remove.enabled = any(item['id'] == selected and item['group'] == group for item in items)
    _target(remove.operator('lea_line.preset_remove', text='', icon='REMOVE'), detail, group).preset_id = selected
    _target(row.operator('lea_line.preset_apply', text='Default', icon='LOOP_BACK'), detail, group).preset_id = 'default'


class _Target:
    tree_name: StringProperty()
    node_name: StringProperty()
    group: EnumProperty(items=[('BRUSH', 'Brush', ''), ('STROKE', 'Stroke', '')])

    def detail(self):
        return _require_detail(self.tree_name, self.node_name)


class LEA_OT_preset_add(_Target, bpy.types.Operator):
    bl_idname = 'lea_line.preset_add'
    bl_label = 'Save Preset in this Graph'
    bl_description = 'Save the current supported settings in this line graph; included when the .blend is saved'
    bl_options = {'REGISTER', 'UNDO'}
    preset_name: StringProperty(name='Name', default='My Preset', maxlen=80)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        self.layout.prop(self, 'preset_name')
        self.layout.label(text='Saved with this graph in the .blend')

    def execute(self, context):
        try:
            add_preset(self.detail(), self.group, self.preset_name)
        except (ValueError, TypeError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        _redraw(context)
        return {'FINISHED'}


class LEA_OT_preset_apply(_Target, bpy.types.Operator):
    bl_idname = 'lea_line.preset_apply'
    bl_label = 'Apply Preset'
    bl_description = 'Apply this group to the shared Brush Detail; other settings and links are preserved'
    bl_options = {'REGISTER', 'UNDO'}
    preset_id: StringProperty()

    def execute(self, context):
        try:
            apply_preset(self.detail(), self.group, self.preset_id)
        except (ValueError, TypeError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        _redraw(context)
        return {'FINISHED'}


class LEA_OT_preset_remove(_Target, bpy.types.Operator):
    bl_idname = 'lea_line.preset_remove'
    bl_label = 'Remove Saved Preset'
    bl_description = 'Remove only this saved entry from the graph; current brush settings are kept'
    bl_options = {'REGISTER', 'UNDO'}
    preset_id: StringProperty()

    def execute(self, context):
        try:
            remove_preset(self.detail(), self.group, self.preset_id)
        except (ValueError, TypeError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        _redraw(context)
        return {'FINISHED'}


def _draw_menu(layout, context, group):
    detail = getattr(context, 'lea_preset_detail', None)
    if detail is None or detail.bl_idname != 'LEABrushDetailNode':
        layout.label(text='Select a Brush Detail')
        return
    for key, (label, _values) in BUILTINS[group].items():
        _target(layout.operator('lea_line.preset_apply', text=label), detail, group).preset_id = 'builtin:' + key
    try:
        entries = [item for item in _read(detail.id_data) if item['group'] == group]
    except (ValueError, TypeError):
        layout.label(text='Invalid saved preset data', icon='ERROR')
        return
    if entries:
        layout.separator()
        layout.label(text='Saved in this Graph')
    for item in entries:
        _target(layout.operator('lea_line.preset_apply', text=item['name']), detail, group).preset_id = item['id']


class LEA_MT_brush_presets(bpy.types.Menu):
    bl_label = 'Brush Presets'
    def draw(self, context):
        _draw_menu(self.layout, context, 'BRUSH')


class LEA_MT_stroke_presets(bpy.types.Menu):
    bl_label = 'Stroke Presets'
    def draw(self, context):
        _draw_menu(self.layout, context, 'STROKE')


CLASSES = (LEA_OT_preset_add, LEA_OT_preset_apply, LEA_OT_preset_remove,
           LEA_MT_brush_presets, LEA_MT_stroke_presets)


def register():
    registered = []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
    except Exception:
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
