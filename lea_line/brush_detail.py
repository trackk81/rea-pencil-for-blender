"""Shared stroke construction settings for the independent Freestyle backend.

The controls follow the original workflow, but native Freestyle geometry and
sampling are an independent implementation, not Pencil+ brush equivalence.
"""
import math
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty


LEGACY_FIELDS = ('caps', 'dashed', 'dash', 'gap', 'taper', 'taper_start', 'taper_end')
EXTRA_FIELDS = ('min_line_length', 'split_angle_enabled', 'split_angle', 'extend',
                'normal_offset', 'offset_x', 'offset_y', 'distortion_enabled',
                'distortion_amplitude', 'distortion_wavelength', 'distortion_phase',
                'size_curve_enabled', 'alpha_curve_enabled', 'loop_start', 'loop_orientation', 'dash_phase',
                'line_direction_enabled', 'line_direction')
from .stroke_variation import FIELDS as VARIATION_FIELDS
EXTRA_FIELDS += VARIATION_FIELDS
from .stamp_brush import FIELDS as STAMP_FIELDS
EXTRA_FIELDS += STAMP_FIELDS


def _changed(owner, context):
    from .ui import _sync_if_enabled
    _sync_if_enabled(owner, context)


def _wave_changed(owner, context):
    from .ui import _sync_if_enabled
    _sync_if_enabled(owner, context, wave_detail=owner)


def _curve_enabled_changed(owner, context):
    from . import curves
    if owner.size_curve_enabled or owner.alpha_curve_enabled:
        curves.ensure_detail(owner)
    _changed(owner, context)


def _curve_tree_poll(_self, tree):
    return tree.bl_idname == 'ShaderNodeTree'


def initialize_from_brush(detail, brush):
    """Opt into a shared Detail without changing an existing Brush's appearance."""
    from .ui import suspend_sync
    with suspend_sync():
        for name in LEGACY_FIELDS:
            setattr(detail, name, getattr(brush, name))


def stroke_geometry_enabled(brush):
    """Used by preview/render capability checks on the effective Brush values."""
    return (getattr(brush, 'min_line_length', 0) > 0
            or getattr(brush, 'split_angle_enabled', False)
            or any(abs(getattr(brush, name, 0)) > 0 for name in ('extend', 'normal_offset', 'offset_x', 'offset_y'))
            or getattr(brush, 'distortion_enabled', False))


def validate_scaled_brush(brush, scale):
    """Reject unsupported relative ranges before native RNA silently clamps."""
    if getattr(brush, 'min_line_length', 0) * scale > 10000:
        raise ValueError('Min Line Length exceeds the Freestyle 10,000 pixel limit after relative width scaling')
    if getattr(brush, 'distortion_enabled', False) and brush.distortion_wavelength * scale < 0.0001:
        raise ValueError('Distortion wavelength is too small at the current relative width scale')


def _line_type_get(node):
    return int(node.dashed)


def _line_type_set(node, value):
    node.dashed = bool(value)


def draw_section(layout, node, identifier, title, enabled_property=None, sidebar=False):
    """Draw a full-width native sidebar section or a safe node-layout fallback.

    Native layout panels cannot be nested in boxes or split columns. Callers
    opt in only from the sidebar's root layout; node properties/popovers remain
    ordinary boxes. Native fold state belongs to the region, never the graph.
    """
    if sidebar and hasattr(layout, 'panel'):
        header, body = layout.panel(identifier, default_closed=False)
    else:
        section = layout.box()
        header, body = section.row(), section.column()
    header.use_property_split = False
    header.use_property_decorate = False
    if enabled_property:
        control = header.row()
        control.enabled = node.enabled
        control.prop(node, enabled_property, text=title)
    else:
        header.label(text=title)
    if body is not None:
        body.use_property_split = True
        body.use_property_decorate = False
        body.enabled = node.enabled and (not enabled_property or getattr(node, enabled_property))
    return body


class LEABrushDetailNode(bpy.types.Node):
    bl_idname = 'LEABrushDetailNode'
    bl_label = 'Brush Detail'
    bl_icon = 'GREASEPENCIL'
    color_space_type: EnumProperty(items=[('RGB','RGB',''),('HSV','HSV','')],default='RGB',update=_changed)
    color_space_red: FloatProperty(name='R',min=0,max=1,default=0,update=_changed)
    color_space_green: FloatProperty(name='G',min=0,max=1,default=0,update=_changed)
    color_space_blue: FloatProperty(name='B',min=0,max=1,default=0,update=_changed)
    color_space_hue: FloatProperty(name='H',min=0,max=1,default=0,update=_changed)
    color_space_saturation: FloatProperty(name='S',min=0,max=1,default=0,update=_changed)
    color_space_value: FloatProperty(name='V',min=0,max=1,default=0,update=_changed)
    stroke_size_random: FloatProperty(name='Size Random',min=0,max=100,default=0,subtype='PERCENTAGE',update=_changed)

    brush_type: EnumProperty(name='Brush Type',items=[('NORMAL','Normal','Fixed screen stamp'),('MULTIPLE','Multiple','Packed stamp mask'),('SIMPLE','Simple','Legacy solid stroke')],default='SIMPLE',update=_changed)
    from .stamp_brush import map_enabled as _map_changed
    brush_map_on: BoolProperty(name='Brush Map',default=False,update=_map_changed)
    brush_map_opacity: FloatProperty(name='Opacity',min=0,max=1,default=1,update=_changed)
    stretch: FloatProperty(name='Stretch',min=-.98,max=.98,default=0,update=_changed)
    angle: FloatProperty(name='Angle',subtype='ANGLE',default=0,update=_changed)
    groove: FloatProperty(name='Groove',min=0,max=1,default=0,update=_changed)
    groove_number: IntProperty(name='Number',min=3,max=20,default=5,update=_changed)
    stamp_size: FloatProperty(name='Size',min=1,max=100,default=16,update=_changed)
    stamp_size_random: FloatProperty(name='Random',min=0,max=100,default=80,update=_changed)
    stamp_antialiasing: FloatProperty(name='Antialiasing',min=0,max=10,default=.5,update=_changed)
    horizontal_space: FloatProperty(name='Horizontal Space',min=0,max=1,default=.1,update=_changed)
    horizontal_space_random: FloatProperty(name='Random',min=0,max=100,default=100,update=_changed)
    vertical_space: FloatProperty(name='Vertical Space',min=0,max=1,default=.1,update=_changed)
    vertical_space_random: FloatProperty(name='Random',min=0,max=100,default=100,update=_changed)
    stamp_reduction_start: FloatProperty(name='Start',min=0,max=1,default=1,update=_changed)
    stamp_reduction_end: FloatProperty(name='End',min=0,max=1,default=1,update=_changed)
    distortion_map_on: BoolProperty(name='Distortion Map',default=False,update=_map_changed)
    distortion_map_amount: FloatProperty(name='Amount',min=0,max=1000,default=5,update=_changed)
    enabled: BoolProperty(name='Use Brush Detail', description='Use these shared stroke settings; disabled uses the Brush inline settings', default=True, update=_changed)
    caps: EnumProperty(name='Caps', items=[('ROUND', 'Round', 'Rounded ends'), ('BUTT', 'Butt', 'Flat ends'), ('SQUARE', 'Square', 'Extended square ends')], default='ROUND', update=_changed)
    dashed: BoolProperty(name='Dashed', default=False, update=_changed)
    line_type: EnumProperty(name='Line Type', items=[('FULL', 'Full', 'Continuous stroke', 0), ('DASHED', 'Dashed', 'Repeated dash and gap', 1)], get=_line_type_get, set=_line_type_set)
    dash: IntProperty(name='Length', description='Dash length in render pixels', min=1, max=10000, default=10, update=_changed)
    gap: IntProperty(name='Space', description='Gap between dashes in render pixels', min=1, max=10000, default=5, update=_changed)
    dash_phase: IntProperty(name='Dash Phase (Live)', description='Pixel-sampled dash offset in live preview; native phase can differ by one sample',min=-10000,max=10000,default=0,update=_changed)
    taper: BoolProperty(name='Endpoint Taper', description='Legacy endpoint thickness reduction; the editable size curve takes precedence when enabled', default=False, update=_changed)
    taper_start: FloatProperty(name='Start', description='Thickness fraction at stroke start', min=0, max=1, default=.2, update=_changed)
    taper_end: FloatProperty(name='End', description='Thickness fraction at stroke end', min=0, max=1, default=.2, update=_changed)
    min_line_length: FloatProperty(name='Min Line Length', description='Exclude strokes shorter than this projected pixel length. GPU uses depth-split lengths for standard edges and full chains for object-depth tests or thresholds that exceed tile overlap. Zero disables filtering', min=0, max=10000, default=0, subtype='PIXEL', update=_changed)
    split_angle_enabled: BoolProperty(name='Split by Angle', description='Split Freestyle chains at a projected interior angle below the threshold', default=False, update=_changed)
    split_angle: FloatProperty(name='Line Split Angle', description='Projected interior angle; native Freestyle splitting may differ from Pencil+', min=0, max=math.pi, default=math.pi/2, subtype='ANGLE', update=_changed)
    original_split_angle: FloatProperty(name='Line Split Angle', description='Projected turn angle: sharper turns above this value split; complementary to the legacy interior-angle setting',
        min=0, max=math.pi, subtype='ANGLE',
        get=lambda self: max(0.0,min(math.pi,math.pi-self.split_angle)),
        set=lambda self,value: setattr(self,'split_angle',math.pi-value))
    extend: FloatProperty(name='Extend', description='Screen-space extension at each stroke end, in pixels. GPU extends solid round caps at chain ends and at depth cuts on standard edges', min=0, max=10000, default=0, subtype='PIXEL', update=_changed)
    line_direction_enabled: BoolProperty(name='Line Direction (Live)', description='Enable live open-chain direction in projected camera space; ties preserve order', default=False, update=_changed)
    line_direction: FloatProperty(name='Line Direction (Angle)', description='Live open-chain direction in projected camera space; ties preserve order', min=-math.pi, max=math.pi, default=-math.pi/6, subtype='ANGLE', update=_changed)
    loop_start: EnumProperty(name='Loop Start (Live)', description='Projected seam selection; original automatic edge selection remains different',
        items=[('KEEP','Keep','Preserve incoming start'),('MIN_X_MIN_Y','Leftmost','Leftmost point, then lowest'),
               ('MAX_X_MIN_Y','Rightmost','Rightmost point, then lowest'),('MIN_Y_MIN_X','Lowest','Lowest point, then leftmost'),
               ('MAX_Y_MIN_X','Highest','Highest point, then leftmost')],default='KEEP',update=_changed)
    loop_orientation: EnumProperty(name='Loop Direction', description='Projected winding in Native preview and in GPU/CPU Wave with Line Direction enabled',
        items=[('KEEP','Keep','Preserve incoming winding'),('CLOCKWISE','Clockwise','Clockwise in projected camera space'),
               ('COUNTERCLOCKWISE','Counterclockwise','Counterclockwise in projected camera space')],default='KEEP',update=_changed)
    normal_offset: FloatProperty(name='Normal Offset', description='Offset along the projected stroke normal, in pixels; chain direction determines the sign', min=-1000, max=1000, default=0, subtype='PIXEL', update=_changed)
    offset_x: FloatProperty(name='X Offset', description='Horizontal image-space displacement in pixels', min=-1000, max=1000, default=0, subtype='PIXEL', update=_changed)
    offset_y: FloatProperty(name='Y Offset', description='Vertical image-space displacement in pixels; positive moves down', min=-1000, max=1000, default=0, subtype='PIXEL', update=_changed)
    distortion_enabled: BoolProperty(name='Distortion', description='Native sinusoidal stroke displacement; maps and random variation are not implemented', default=False, update=_changed)
    distortion_amplitude: FloatProperty(name='Amount', description='Sinusoidal displacement amplitude in pixels', min=0, max=1000, default=5, subtype='PIXEL', update=_wave_changed)
    distortion_wavelength: FloatProperty(name='Cycles', description='Distance per sinusoidal cycle in pixels', min=1, max=10000, default=100, subtype='PIXEL', update=_wave_changed)
    distortion_phase: FloatProperty(name='Phase', description='Sinusoidal phase angle', min=-30*math.pi, max=30*math.pi, default=0, subtype='ANGLE', update=_wave_changed)
    size_curve_enabled: BoolProperty(name='Stroke Size Reduction Settings', description='Use the editable stroke-size curve instead of the legacy endpoint taper', default=False, update=_curve_enabled_changed)
    alpha_curve_enabled: BoolProperty(name='Stroke Alpha Reduction Settings', description='Multiply opacity by the editable curve along every stroke', default=False, update=_curve_enabled_changed)
    curve_tree: PointerProperty(type=bpy.types.NodeTree, poll=_curve_tree_poll, options={'HIDDEN'})

    @classmethod
    def poll(cls, tree):
        return tree.bl_idname == 'LEALineTree'

    def init(self, context):
        self.width = 140 if self.id_data.ui_layout == 'ORIGINAL' else 205
        self.outputs.new('LEALineSocket', 'Brush Detail')
        for name in ('Brush Map','Distortion Map'):self.inputs.new('LEALineSocket',name).link_limit=1
        from . import curves
        curves.ensure_detail(self)

    def copy(self, source):
        from . import curves
        curves.copy_detail(self, source)

    def free(self):
        from . import curves
        curves.free_detail(self)

    def draw_buttons(self, context, layout):
        if self.id_data.ui_layout == 'ORIGINAL':
            return
        layout.prop(self, 'enabled', text='Enabled')
        layout.prop(self, 'line_type', text='')
        layout.label(text='N > REA Pencil Line: Brush Detail')

    def draw_buttons_ext(self, context, layout):
        self.draw_controls(context, layout)

    def draw_controls(self, context, layout, sidebar=False):
        from .presets import draw_presets
        layout.prop(self, 'enabled')
        if not self.enabled:
            layout.label(text='Using Brush inline stroke settings', icon='INFO')
        editor = draw_section(layout, self, 'rea_detail_brush_editor', 'Brush Editor', sidebar=sidebar)
        if editor is not None:
            draw_presets(editor, self, group='BRUSH')
            editor.prop(self,'brush_type')
            from . import stamp_brush
            stamp_brush.draw_map(editor,self,'brush')
            editor.prop(self, 'caps')
        stroke = draw_section(layout, self, 'rea_detail_stroke', 'Stroke', sidebar=sidebar)
        if stroke is not None:
            draw_presets(stroke, self, group='STROKE')
            row = stroke.row(align=True)
            row.use_property_split = False
            row.prop_enum(self, 'line_type', 'FULL', text='Full')
            row.prop_enum(self, 'line_type', 'DASHED', text='Dashed')
            pattern = stroke.column(align=True)
            pattern.enabled = self.dashed
            pattern.prop(self, 'dash')
            pattern.prop(self, 'gap')
            stroke.prop(self,'stroke_size_random')
            pattern.prop(self, 'dash_phase')
            stroke.prop(self, 'extend')
            stroke.prop(self, 'normal_offset')
            stroke.prop(self, 'offset_x')
            stroke.prop(self, 'offset_y')
            row = stroke.row()
            row.use_property_split = False
            row.prop(self, 'split_angle_enabled')
            row = stroke.row()
            row.enabled = self.split_angle_enabled
            row.prop(self, 'split_angle')
            stroke.prop(self, 'min_line_length')
            direction = stroke.row(align=True)
            direction.use_property_split = False
            direction.prop(self, 'line_direction_enabled', text='')
            angle = direction.row()
            angle.enabled = self.line_direction_enabled
            angle.prop(self, 'line_direction')
            stroke.prop(self, 'loop_start')
            stroke.prop(self, 'loop_orientation')
        distortion = draw_section(layout, self, 'rea_detail_distortion', 'Distortion',
                                  enabled_property='distortion_enabled', sidebar=sidebar)
        if distortion is not None:
            from . import stamp_brush
            stamp_brush.draw_map(distortion,self,'distortion')
            distortion.prop(self, 'distortion_amplitude')
            distortion.prop(self, 'distortion_wavelength')
            distortion.prop(self, 'distortion_phase')
        reduction = draw_section(layout, self, 'rea_detail_size_reduction', 'Stroke Size Reduction',
                                 enabled_property='taper', sidebar=sidebar)
        if reduction is not None:
            row = reduction.row(align=True)
            row.use_property_split = False
            row.prop(self, 'taper_start', text='Start')
            row.prop(self, 'taper_end', text='End')
        layout.label(text='Live preview / Freestyle stroke effects', icon='INFO')
        color=draw_section(layout,self,'rea_detail_color_range','Color Range',sidebar=sidebar)
        if color is not None:
            from .reference_detail import _color_range
            _color_range(context,color,self)
        layout.label(text='Stamp and distortion maps pending')
        layout.label(text='Editable curves: Original Detail layout')
