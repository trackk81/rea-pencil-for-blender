"""Original-order Brush Detail controls with explicit capability boundaries.

Layout facts come from the installed BrushDetailNodePanel and retained REA
evidence. Rendering properties belong to our independent implementation. The
reference RNA is displayed only in disabled rows and never enters a renderer.
"""
from . import presets, reference_controls as ref
from .brush_detail import draw_section


SECTIONS = ('Brush Editor', 'Stroke', 'Distortion',
            'Stroke Size Reduction Settings', 'Stroke Alpha Reduction Settings',
            'Color Range', 'rea Extensions')
SECTION_SPECS = tuple(zip(('brush_editor', 'stroke', 'distortion', 'size', 'alpha', 'color_range', 'extensions'),
                          SECTIONS, (None, None, 'distortion_enabled', 'size_curve_enabled',
                                     'alpha_curve_enabled', None, None)))


def _pair(context, layout, detail, original, label, random, random_label='Random', actual=None):
    """Reserve one 40% caption column, then two unsplit value widgets."""
    owner, name = (detail, actual) if actual else (ref.data(context, 'detail'), original)
    if not actual:
        layout = layout.row(align=True)
        layout.enabled = False
    ref.pair(context, layout, label, owner, name, 'detail', random, random_label)


def _enum(layout, owner, name, title, items):
    split = layout.split(factor=.4)
    split.use_property_split = False
    label = split.row(align=True)
    label.alignment = 'RIGHT'
    label.label(text=title)
    controls = split.split(factor=1.0).row(align=True)
    for value, text in items:
        controls.prop_enum(owner, name, value, text=text)


def _half(context, layout, name, label, text='', right=False):
    split = layout.split(factor=.4)
    split.use_property_split = False
    heading = split.row()
    heading.alignment = 'RIGHT'
    heading.label(text=label)
    controls = split.split(factor=1.0).split(factor=.5)
    left, right_side = controls.row(align=True), controls.row(align=True)
    field, blank = (right_side, left) if right else (left, right_side)
    ref.prop(context, field, 'detail', name, text=text)
    blank.label(text='')


def _map(context, layout, prefix, title, value, value_label):
    split = layout.row().split(factor=.4)
    split.use_property_split = False
    heading = split.row(align=True)
    heading.alignment = 'RIGHT'
    ref.prop(context, heading, 'detail', prefix + '_on_gui', text='')
    heading.label(text=title)
    heading.label(text='', icon='TEXTURE')
    controls = split.split(factor=1.0).row(align=True)
    ref.unavailable(controls, text='None')
    ref.prop(context, controls, 'detail', value, text=value_label)


def _preset_row(layout, detail, group):
    box = layout.box()
    box.use_property_split = False
    column = box.column(align=True)
    column.label(text='Brush Presets' if group == 'BRUSH' else 'Stroke Presets')
    split = column.split(factor=.7)
    row = split.row(align=True)
    row.context_pointer_set('lea_preset_detail', detail)
    try:
        label = presets._selected_label(detail, group)
        entries = presets._read(detail.id_data)
    except (TypeError, ValueError):
        column.label(text='Preset data needs repair', icon='ERROR')
        return
    row.menu('LEA_MT_' + group.lower() + '_presets', text=label)
    presets._target(row.operator('lea_line.preset_add', text='', icon='ADD'), detail, group)
    selected = detail.get(presets._selection_key(group), '')
    remove = row.row(align=True)
    remove.enabled = any(item['id'] == selected and item['group'] == group for item in entries)
    presets._target(remove.operator('lea_line.preset_remove', text='', icon='REMOVE'), detail, group).preset_id = selected
    defaults = split.row(align=True)
    presets._target(defaults.operator('lea_line.preset_apply', text='Default', icon='LOOP_BACK'), detail, group).preset_id = 'default'
    layout.separator(factor=2.0)


def _brush_editor(context, body, detail):
    from . import stamp_brush
    stamp_brush.draw_editor(context,body,detail)
    return
    _preset_row(body, detail, 'BRUSH')
    ref.enum(context, body, 'detail', 'brush_type', 'Brush Type')
    column = body.column(align=True)
    column.enabled = False
    _map(context, column, 'brush_map', 'Brush Map', 'brush_map_opacity', 'Opacity')
    for original, label, random in (('stretch', 'Stretch', 'stretch_random'),
                                    ('angle', 'Angle', 'angle_random')):
        _pair(context, column, detail, original, label, random)
    _pair(context, column, detail, 'groove', 'Groove', 'groove_number', 'Number')
    _pair(context, column, detail, 'size', 'Size', 'size_random')
    _half(context, column, 'antialiasing', 'Antialiasing')
    _pair(context, column, detail, 'horizontal_space', 'Horizontal Space', 'horizontal_space_random')
    _pair(context, column, detail, 'vertical_space', 'Vertical Space', 'vertical_space_random')
    split = body.row().split(factor=.4)
    split.use_property_split = False
    split.enabled = False
    label = split.row(align=True)
    label.alignment = 'RIGHT'
    label.label(text='Reduction')
    row = split.split(factor=1.0).row(align=True)
    ref.prop(context, row, 'detail', 'reduction_start', text='Start')
    ref.prop(context, row, 'detail', 'reduction_end', text='End')


def _stroke(context, body, detail):
    _preset_row(body, detail, 'STROKE')
    ref.enum(context, body, 'detail', 'stroke_type', 'Stroke Type')
    _enum(body, detail, 'line_type', 'Line Type', (('FULL', 'Full'), ('DASHED', 'Dashed')))
    pattern = body.column(align=True)
    pattern.enabled = detail.dashed
    _pair(context, pattern, detail, 'length', 'Length', 'length_random', actual='dash')
    _pair(context, pattern, detail, 'space', 'Space', 'space_random', actual='gap')
    column = body.column(align=True)
    column.prop(detail,'stroke_size_random',text='Size Random')
    _pair(context, column, detail, 'extend', 'Extend', 'extend_random', actual='extend')
    _pair(context, column, detail, 'line_copy', 'Line Copy', 'line_copy_random')
    _pair(context, column, detail, 'normal_offset', 'Normal Offset', 'normal_offset_random', actual='normal_offset')
    _pair(context, column, detail, 'x_offset', 'X Offset', 'x_offset_random', actual='offset_x')
    _pair(context, column, detail, 'y_offset', 'Y Offset', 'y_offset_random', actual='offset_y')
    column = body.column(align=True)
    angle = column.row(align=True)
    angle.use_property_split = False
    angle.prop(detail, 'split_angle_enabled', text='')
    value = angle.row()
    value.enabled = detail.split_angle_enabled
    value.prop(detail, 'original_split_angle', text='Line Split Angle')
    column.prop(detail, 'min_line_length', text='Min Line Length')
    ref.prop(context, column, 'detail', 'line_link_length', text='Line Link Length')
    direction = column.row(align=True)
    direction.use_property_split = False
    direction.prop(detail, 'line_direction_enabled', text='')
    value = direction.row()
    value.enabled = detail.line_direction_enabled
    value.prop(detail, 'line_direction', text='Line Direction (Angle)')
    ref.enum(context, column, 'detail', 'loop_direction_type', 'Loop Direction', reverse=True)


def _distortion(context, body, detail):
    from . import stamp_brush
    stamp_brush.draw_map(body,detail,'distortion')
    column = body.column(align=True)
    _pair(context, column, detail, 'distortion_amount', 'Amount', 'distortion_random', actual='distortion_amplitude')
    _pair(context, column, detail, 'distortion_cycles', 'Cycles', 'distortion_cycles_random', actual='distortion_wavelength')
    _pair(context, column, detail, 'distortion_phase', 'Phase', 'distortion_phase_random', actual='distortion_phase')


def _curve(body, detail, kind):
    from . import curves
    node = curves.get_mapping_node(detail, kind)
    if node is not None:
        body.template_curve_mapping(node, 'mapping')
    else:
        body.label(text='Enable to create curve', icon='INFO')


def _color_range(context, body, detail):
    column = body.column(align=True)
    row = column.row()
    row.use_property_split = False
    # Blender 4.4 suppresses expanded enum captions when text='' is supplied.
    # Explicit items preserve the original RGB/HSV labels in narrow sidebars.
    owner = detail
    for value in ('RGB', 'HSV'):
        row.prop_enum(owner, 'color_space_type', value, text=value)
    row = column.row(align=True)
    row.use_property_split = False
    space = detail.color_space_type
    channels = (('red', 'R'), ('green', 'G'), ('blue', 'B')) if space == 'RGB' else (
                ('hue', 'H'), ('saturation', 'S'), ('value', 'V'))
    for name, label in channels:
        row.prop(detail,'color_space_' + name,text=label)


def _extensions(body, detail):
    body.use_property_split = True
    body.use_property_decorate = False
    body.prop(detail, 'enabled', text='Use Brush Detail')
    column = body.column()
    column.enabled = detail.enabled
    column.prop(detail, 'caps')
    column.prop(detail, 'loop_start')
    column.prop(detail, 'loop_orientation')
    column.prop(detail, 'dash_phase')
    column.prop(detail, 'split_angle_enabled', text='Split Stroke Chains')
    angle = column.row()
    angle.enabled = detail.split_angle_enabled
    angle.prop(detail, 'split_angle', text='Projected Split Angle')
    column.prop(detail, 'taper', text='Legacy Stroke Taper')
    ends = column.row(align=True)
    ends.enabled = detail.taper and not detail.size_curve_enabled
    ends.prop(detail, 'taper_start', text='Start')
    ends.prop(detail, 'taper_end', text='End')


def draw_body(context, layout, detail, key):
    """Section body for a genuine Blender Panel class; creates no nested panel."""
    spec = next((spec for spec in SECTION_SPECS if spec[0] == key), None)
    if spec is None:
        raise ValueError('Unknown Brush Detail section: ' + key)
    enabled_property = spec[2]
    layout.use_property_split = key not in {'size', 'alpha', 'color_range'}
    layout.use_property_decorate = False
    layout.enabled = detail.id_data.is_editable and (key == 'extensions' or (
        detail.enabled and (not enabled_property or getattr(detail, enabled_property))))
    if key == 'brush_editor':
        _brush_editor(context, layout, detail)
    elif key == 'stroke':
        _stroke(context, layout, detail)
    elif key == 'distortion':
        _distortion(context, layout, detail)
    elif key in {'size', 'alpha'}:
        _curve(layout, detail, key.upper())
    elif key == 'color_range':
        _color_range(context, layout, detail)
    else:
        _extensions(layout, detail)


def draw(context, layout, detail):
    """Draw only on a full-width native sidebar layout, never inside a split."""
    for key, title, enabled_property in SECTION_SPECS:
        if key == 'extensions':
            header, body = layout.panel('rea_reference_detail_extensions', default_closed=True)
            header.label(text=title)
        else:
            body = draw_section(layout, detail, 'rea_reference_detail_' + key, title,
                                enabled_property=enabled_property, sidebar=True)
        if body is not None:
            draw_body(context, body, detail, key)
