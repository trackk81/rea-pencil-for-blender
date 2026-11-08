"""rea pencil for blender node editor and scene controls for Blender 4.4.

This module contains no Pencil+ code or binary dependencies.
"""

import math
from contextlib import contextmanager

import bpy
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty,
    IntProperty, PointerProperty, StringProperty,
)
from .brush_detail import LEABrushDetailNode, initialize_from_brush
from . import independent_reduction


_sync_busy = False
OVERRIDE_FEATURES = (
    "outline", "border", "crease", "material_boundary", "edge_mark",
    "ridge_valley", "suggestive_contour",
)
OVERRIDE_SOCKET_NAMES = tuple(
    f"{prefix}{feature} Brush"
    for prefix in ("", "Hidden ") for feature in OVERRIDE_FEATURES
)
EDGE_FEATURES = OVERRIDE_FEATURES + ("intersection",)


@contextmanager
def suspend_sync():
    """Group graph edits without applying intermediate or incomplete graphs."""
    global _sync_busy
    previous = _sync_busy
    _sync_busy = True
    try:
        yield
    finally:
        _sync_busy = previous


def _refresh_override_visibility(node, context=None):
    with suspend_sync():
        for socket in node.inputs:
            if socket.name in OVERRIDE_SOCKET_NAMES:
                desired = not (node.show_overrides or socket.is_linked)
                if socket.hide != desired:
                    socket.hide = desired


def _hidden_selection_changed(node, context=None):
    # First opt-in starts from the old shared selection, so merely enabling the
    # control does not change the image. Later toggles retain the authored values.
    if node.independent_hidden_edges and not node.get('rea_hidden_edges_initialized', False):
        with suspend_sync():
            for feature in EDGE_FEATURES:
                name = 'hidden_' + feature
                if not node.is_property_set(name):
                    setattr(node, name, getattr(node, feature))
            node['rea_hidden_edges_initialized'] = True
    _sync_if_enabled(node, context)


def upgrade_tree(tree):
    """Add newly introduced sockets while preserving old node IDs and links.

    This intentionally does not apply scene settings. Callers can migrate a
    complete graph before synchronizing it. Returns the number of new sockets.
    """
    if tree is None or tree.bl_idname != "LEALineTree":
        return 0
    added = 0
    with suspend_sync():
        for node in tree.nodes:
            if node.bl_idname == 'LEAOutputNode':
                from .node_workflow import ensure_trailing
                ensure_trailing(node)
                continue
            if node.bl_idname == 'LEABrushDetailNode':
                for name in ('Brush Map','Distortion Map'):
                    if node.inputs.get(name) is None:node.inputs.new('LEALineSocket',name).link_limit=1;added+=1
                continue
            if node.bl_idname == "LEABrushNode":
                for name in ('Color Map','Size Map'):
                    if node.inputs.get(name) is None:
                        node.inputs.new('LEALineSocket',name).link_limit=1
                        added+=1
                if node.inputs.get("Brush Detail") is None:
                    node.inputs.new("LEALineSocket", "Brush Detail").link_limit = 1
                    added += 1
                if node.inputs.get("Distance Reduction") is None:
                    node.inputs.new("LEALineSocket", "Distance Reduction").link_limit = 1
                    added += 1
                continue
            if node.bl_idname != "LEALineSetNode":
                continue
            for name in ("Brush", "Hidden Brush") + OVERRIDE_SOCKET_NAMES + independent_reduction.SOCKETS:
                if node.inputs.get(name) is None:
                    node.inputs.new("LEALineSocket", name).link_limit = 1
                    added += 1
            _refresh_override_visibility(node)
    return added


def _sync_if_enabled(owner, context=None):
    # RNA rebinding may notify existing nodes during restricted addon enable.
    # Deferred bootstrap/load handlers will synchronize after data is available.
    if not hasattr(bpy.data, 'scenes'):
        return
    from . import preview_material
    if preview_material.is_suspended():
        return
    """Keep the configured native line sets in step with the editable graph."""
    global _sync_busy
    if _sync_busy:
        return
    tree = owner if isinstance(owner, bpy.types.NodeTree) else getattr(owner, "id_data", None)
    if isinstance(tree, bpy.types.Scene):
        scenes = [tree]
    else:
        scenes = [s for s in bpy.data.scenes if hasattr(s, "lea_line") and s.lea_line.tree == tree]
    _sync_busy = True
    try:
        from . import backend
        for scene in scenes:
            settings = scene.lea_line
            if (settings.auto_sync or settings.preview_enabled) and settings.tree is not None:
                try:
                    warnings = backend.sync_scene(scene, settings.tree) if settings.auto_sync else []
                    if settings.preview_enabled:
                        from . import viewport
                        warnings = list(warnings) + viewport.sync_preview(scene)
                    settings.last_message = "\n".join(warnings) if warnings else "rea pencil for blender settings synchronized"
                except Exception as exc:
                    # Node graph editing may temporarily leave an incomplete graph.
                    settings.last_message = str(exc)
            elif settings.preview_enabled and settings.tree is None:
                from . import viewport
                viewport.clear_preview(scene)
            from . import object_uv
            object_uv.request_prepare(scene)
    finally:
        _sync_busy = False


def _tree_poll(_self, tree):
    return tree.bl_idname == "LEALineTree"


def _preview_changed(settings, context):
    if _sync_busy:
        return
    scene = settings.id_data
    if not isinstance(scene, bpy.types.Scene):
        return
    try:
        from . import viewport
        with suspend_sync():
            if settings.preview_enabled:
                warnings = viewport.sync_preview(scene)
                settings.last_message = "\n".join(warnings) if warnings else "Camera-based live preview synchronized"
            else:
                viewport.clear_preview(scene)
                settings.last_message = "Live preview disabled"
        from . import preview_runtime
        preview_runtime.mark_synced(scene)
    except Exception as exc:
        settings.last_message = str(exc)


class LEALineSettings(bpy.types.PropertyGroup):
    fast_line_art: BoolProperty(name="Line Art Render (Experimental)", description="Alternative rendering for intersections and potential speed gains; different strokes, limited brush support; F12 stays normal", default=False)
    stroke_width_profile: EnumProperty(name="Stroke Coverage", description="Line Art final render only; choose geometric width or empirical coverage matched to the original Simple brush", items=[("PHYSICAL", "Geometric Width", "Keep geometric pixel width; existing behavior"), ("PENCIL_SIMPLE", "Pencil-like Simple", "Empirical wider coverage measured from the original Simple brush; not full brush parity")], default="PHYSICAL")
    line_art_aa: EnumProperty(name="Edge Smoothing", description="Line Art final render only; crisp edges can look stair-stepped", items=[('SCENE', 'Scene SMAA', 'Keep the scene Grease Pencil antialiasing setting'), ('CRISP', 'Crisp Edges', 'Disable SMAA filtering; sharper coverage but more jagged edges')], default='SCENE')
    reuse_line_pass: BoolProperty(name="Reuse Unchanged Lines (Experimental)", description="Eevee, one view layer, simple compositor only. Uses a separate line pass with potentially different edge antialiasing. F12 stays a normal render", default=False)
    tree: PointerProperty(name="Line Graph", type=bpy.types.NodeTree, poll=_tree_poll, update=_sync_if_enabled)
    auto_sync: BoolProperty(name="Auto Sync", description="Update native Freestyle settings as this graph changes", default=True, update=_sync_if_enabled)
    backend: EnumProperty(name="Backend", items=[("FREESTYLE", "Blender Freestyle", "Native Blender Freestyle line renderer")], default="FREESTYLE")
    width_mode: EnumProperty(name="Width Mode", description="Use fixed pixel widths or scale widths with render height", items=[("ABSOLUTE", "Absolute Pixels", "Keep line widths fixed in render pixels"), ("RELATIVE", "Relative to Height", "Scale pixel widths by render height divided by reference height")], default="ABSOLUTE", update=_sync_if_enabled)
    reference_height: IntProperty(name="Reference Height", description="Render height at which relative line widths equal their brush thickness", min=1, default=480, update=_sync_if_enabled)
    preview_enabled: BoolProperty(name="Enable Live Preview", description="Show camera-based Line Art with live stroke curves and pixel effects; final Freestyle lines may differ", default=False, update=_preview_changed)
    preview_compute: EnumProperty(name="뷰포트 계산", description="Solid 뷰에서 곡선·점선·맵·숨은 선을 GPU로 계산합니다. 메시 준비는 메인 스레드에서 수행하며 Native와 선 연결·AA 차이가 있습니다", items=[('NATIVE','Native Line Art','기존 Line Art 계산'),('GPU','GPU 미리보기 (시험)','정적 메시 캐시와 GPU 셰이더 사용; 미지원 설정은 Native로 처리')], default='GPU', update=_preview_changed)
    follow_refresh_mode: EnumProperty(name="시점 이동 중 갱신", description="큰 장면은 멈춘 뒤 갱신을 권장합니다. 이동 중 선을 잠시 숨기고 마지막 시점에서 계산합니다", items=[('SETTLED','멈춘 뒤 갱신','이동 중 선 계산을 미루고 시점이 0.2초 멈추면 갱신'),('LIVE','계속 갱신','시점이 바뀔 때마다 선 재계산; 큰 장면에서는 느릴 수 있습니다')], default='SETTLED')
    preview_simple_coverage: BoolProperty(name="Simple Coverage (Live)", description="Empirical coverage for uniform solids and directed open size curves (enable Line Direction). Closed variable chains and dashed/distance/alpha-curve styles retain geometric width; does not reproduce the original AA kernel or change final rendering", default=False, update=_preview_changed)
    preview_endpoint_coverage: BoolProperty(name="Endpoint Coverage (Experimental)", description="Conservative endpoint coverage for directed open SIZE curves with Round caps. Skips closed chains, other effects and existing Simple Coverage. Live preview only; original AA and final rendering may differ", default=False, update=_preview_changed)
    preview_feature_brushes: BoolProperty(name="Mixed Brushes (Experimental)", description="Separate visible marked-edge, crease and material-boundary solid brushes, directed size curves and round/flat/square caps. Unsupported selections keep the common brush", default=False, update=_preview_changed)
    benchmark_json: StringProperty(name="Benchmark Results", default="")
    last_render_seconds: FloatProperty(name="Last Render", default=0.0, min=0.0, precision=3)
    last_message: StringProperty(name="Status", default="")


class LEALineTree(bpy.types.NodeTree):
    bl_idname = "LEALineTree"
    bl_label = "REA Pencil Line"
    bl_icon = "GREASEPENCIL"
    ui_layout: EnumProperty(name='Layout', description='Original panel arrangement or compact controls', items=[('ORIGINAL', 'Original Layout', 'Original panel arrangement; unsupported controls remain disabled'), ('COMPACT', 'Compact', 'Compact rea controls with adaptive target lists')], default='ORIGINAL')
    line_index: IntProperty(name='Selected Line', min=0, get=lambda tree: _line_index_get(tree), set=lambda tree, value: _line_index_set(tree, value))
    panel_section: EnumProperty(name='Section', items=[('LINE_LIST', 'Line List', 'Line Sets and targets'), ('BRUSH', 'Brush Settings', 'Connected Brush settings'), ('DETAIL', 'Brush Detail', 'Connected stroke settings')], default='LINE_LIST')
    panel_anchor: StringProperty(name='Panel Selection')
    panel_line_set: StringProperty(name='Panel Line Set')
    panel_brush: StringProperty(name='Panel Brush')

    @classmethod
    def get_from_context(cls, context):
        scene = context.scene
        if scene and hasattr(scene, "lea_line"):
            return scene.lea_line.tree, scene, scene
        return None, None, None

    def update(self):
        # Blender may notify an existing tree while removing its custom RNA
        # type during add-on disable/re-enable.
        if (not hasattr(self, 'nodes') or not hasattr(bpy.data, 'scenes')
                or not hasattr(bpy.types.Scene, 'lea_line')):
            return
        if _sync_busy:
            return
        for node in self.nodes:
            if node.bl_idname == "LEALineSetNode" and hasattr(node, 'show_overrides'):
                _refresh_override_visibility(node)
        _sync_if_enabled(self)


class LEALineSocket(bpy.types.NodeSocket):
    bl_idname = "LEALineSocket"
    bl_label = "rea pencil for blender Connection"

    def draw(self, context, layout, node, text):
        from .node_workflow import draw_socket
        draw_socket(self, context, layout, node, text)

    def draw_color(self, context, node):
        if getattr(node.id_data, 'ui_layout', 'COMPACT') == 'ORIGINAL':
            from .node_workflow import socket_color
            return socket_color(self, node)
        return (0.28, 0.65, 0.85, 1.0)


class _LEANode:
    @classmethod
    def poll(cls, node_tree):
        return node_tree.bl_idname == "LEALineTree"


class LEAOutputNode(_LEANode, bpy.types.Node):
    random_seed: IntProperty(name='Random Seed',min=0,max=65535,default=0,update=_sync_if_enabled)
    bl_idname = "LEAOutputNode"
    bl_label = "Line"
    bl_icon = "GREASEPENCIL"

    enabled: BoolProperty(name="Enabled", default=True, update=_sync_if_enabled)
    render_priority: IntProperty(name='Render Priority', default=0, update=_sync_if_enabled)
    width_mode: EnumProperty(name='Line Size', items=[('INHERIT', 'Scene Default', 'Preserve legacy scene width settings'),
        ('ABSOLUTE', 'Absolute', 'Fixed pixel widths for this Line'),
        ('RELATIVE', 'Relative (640*480)', 'Scale this Line with render height / 480')],
        default='INHERIT', update=_sync_if_enabled)
    line_size_type: EnumProperty(name='Line Size', items=[('ABSOLUTE', 'Absolute', '', 0),
        ('RELATIVE', 'Relative (640*480)', '', 1)],
        get=lambda node: int((node.width_mode if node.width_mode != 'INHERIT' else
            getattr(getattr(bpy.context.scene, 'lea_line', None), 'width_mode', 'ABSOLUTE')) == 'RELATIVE'),
        set=lambda node, value: setattr(node, 'width_mode', 'RELATIVE' if value else 'ABSOLUTE'))
    line_set_index: IntProperty(name='Selected Line Set', min=0, get=lambda node: _line_set_index_get(node), set=lambda node, value: _line_set_index_set(node, value))

    def init(self, context):
        self.width = 140 if self.id_data.ui_layout == 'ORIGINAL' else 205
        from .node_workflow import ensure_trailing
        ensure_trailing(self)

    def update(self):
        from .node_workflow import ensure_trailing
        ensure_trailing(self)

    def draw_buttons(self, context, layout):
        if self.id_data.ui_layout == 'ORIGINAL':
            row = layout.row()
            row.prop(self, 'enabled', text='Active')
            if any(not s.is_linked for s in list(self.inputs)[:-1]):
                from .node_workflow import _button
                _button(row, self, 0, 'COMPACT', 'UNLINKED')
            return
        layout.prop(self, "enabled")
        layout.prop(self, 'render_priority')


class LEAObjectTarget(bpy.types.PropertyGroup):
    object: PointerProperty(name="Object", type=bpy.types.Object, update=_sync_if_enabled)


class LEAMaterialTarget(bpy.types.PropertyGroup):
    material: PointerProperty(name="Material", type=bpy.types.Material, update=_sync_if_enabled)


class LEA_UL_object_targets(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.prop(item, "object", text="", emboss=False, icon="OBJECT_DATA")


class LEA_UL_material_targets(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.prop(item, "material", text="", emboss=False, icon="MATERIAL")


class LEA_UL_line_sets(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        node = item.links[0].from_node if item.is_linked else None
        if node and node.bl_idname == 'LEALineSetNode':
            row = layout.row(align=True)
            row.prop(node, 'enabled', text='')
            row.prop(node, 'name', text='', emboss=False)

    def filter_items(self, context, data, property_name):
        sockets = getattr(data, property_name)
        nodes = [socket.links[0].from_node if socket.is_linked else None for socket in sockets]
        flags = [self.bitflag_filter_item if node and node.bl_idname == 'LEALineSetNode' else 0 for node in nodes]
        sorted_indices = sorted(range(len(sockets)), key=lambda index: (nodes[index].render_priority if flags[index] else 65536, index))
        order = [0] * len(sockets)
        for position, index in enumerate(sorted_indices):
            order[index] = position
        return flags, order


def _line_output(tree):
    outputs = _line_outputs(tree)
    index = tree.line_index
    if 0 <= index < len(tree.nodes) and tree.nodes[index] in outputs:
        return tree.nodes[index]
    return outputs[0] if outputs else None


def _line_outputs(tree):
    return sorted((node for node in tree.nodes if node.bl_idname == 'LEAOutputNode'),
                  key=lambda node: node.render_priority)


def _line_index_get(tree):
    active = tree.nodes.active
    if active and active.bl_idname == 'LEAOutputNode':
        return list(tree.nodes).index(active)
    index = tree.get('_rea_ui_output_index', -1)
    parents = []
    pending = [active] if active else []
    visited = set()
    while pending:
        node = pending.pop()
        if node in visited:
            continue
        visited.add(node)
        if node.bl_idname == 'LEAOutputNode':
            parents.append(node)
        else:
            pending.extend(link.to_node for link in tree.links if link.from_node == node)
    if parents:
        if 0 <= index < len(tree.nodes) and tree.nodes[index] in parents:
            return index
        return list(tree.nodes).index(min(parents, key=lambda node: node.render_priority))
    if 0 <= index < len(tree.nodes) and tree.nodes[index].bl_idname == 'LEAOutputNode':
        return index
    return next((i for i, node in enumerate(tree.nodes) if node.bl_idname == 'LEAOutputNode'), 0)


def _line_index_set(tree, value):
    if not (0 <= value < len(tree.nodes)) or tree.nodes[value].bl_idname != 'LEAOutputNode':
        return
    with suspend_sync():
        tree['_rea_ui_output_index'] = value
        output = tree.nodes[value]
        _activate_node(tree, output)
        connected = _connected_line_sets(output)
        if tree.nodes.get(tree.panel_line_set) not in connected:
            tree.panel_line_set = connected[0].name if connected else ''
        tree.panel_section = 'LINE_LIST'


def _connected_line_sets(output):
    """Include disabled sets in UI order while preserving socket order on ties."""
    result = []
    seen = set()
    for socket in output.inputs if output else ():
        for link in socket.links:
            node = link.from_node
            if node.bl_idname == "LEALineSetNode" and node.name not in seen:
                result.append(node)
                seen.add(node.name)
    return sorted(result, key=lambda node: node.render_priority)


def _activate_node(tree, node):
    for candidate in tree.nodes:
        candidate.select = candidate == node
    tree.nodes.active = node
    tree.panel_anchor = node.name
    if node.bl_idname == 'LEALineSetNode':
        tree.panel_line_set = node.name
        tree.panel_section = 'LINE_LIST'
        brush = _brush_for_input(node, 'Hidden Brush' if node.line_visibility == 'HIDDEN' else 'Brush')
        tree.panel_brush = brush.name if brush else ''
    elif node.bl_idname == 'LEABrushNode':
        tree.panel_brush = node.name
        tree.panel_section = 'BRUSH'
    elif node.bl_idname == 'LEABrushDetailNode':
        tree.panel_section = 'DETAIL'
    elif node.bl_idname == 'LEADistanceReductionNode':
        # This auxiliary node is reached directly, outside the three main tabs.
        tree.panel_anchor = ''


def _linked_node(node, socket_name, expected=None):
    socket = node.inputs.get(socket_name) if node else None
    source = socket.links[0].from_node if socket and socket.is_linked else None
    return source if source and (expected is None or source.bl_idname == expected) else None


def _brush_for_input(node, socket_name):
    brush = _linked_node(node, socket_name, 'LEABrushNode')
    if brush is None and socket_name != 'Brush':
        fallback = 'Hidden Brush' if socket_name.startswith('Hidden ') else 'Brush'
        brush = _linked_node(node, fallback, 'LEABrushNode')
        if brush is None:
            brush = _linked_node(node, 'Brush', 'LEABrushNode')
    return brush


def _panel_context(tree):
    """Resolve authored graph relationships without changing selection or data."""
    active = tree.nodes.active
    line_set = active if active and active.bl_idname == 'LEALineSetNode' else tree.nodes.get(tree.panel_line_set)
    if line_set and line_set.bl_idname != 'LEALineSetNode':
        line_set = None
    if active and active.bl_idname == 'LEAOutputNode':
        connected = _connected_line_sets(active)
        if line_set not in connected:
            line_set = connected[0] if connected else None
    brush = active if active and active.bl_idname == 'LEABrushNode' else None
    detail = active if active and active.bl_idname == 'LEABrushDetailNode' else None
    auxiliary = detail or (active if active and active.bl_idname == 'LEADistanceReductionNode' else None)
    if auxiliary:
        input_name = 'Brush Detail' if detail else 'Distance Reduction'
        candidates = [link.to_node for link in tree.links if link.from_node == auxiliary and link.to_node.bl_idname == 'LEABrushNode' and link.to_socket.name == input_name]
        brush = next((node for node in candidates if node.name == tree.panel_brush), candidates[0] if candidates else None)
    if brush:
        parents = [link.to_node for link in tree.links if link.from_node == brush and link.to_node.bl_idname == 'LEALineSetNode']
        if line_set not in parents:
            line_set = parents[0] if parents else line_set
    if line_set is None:
        try:
            connected = _connected_line_sets(_line_output(tree))
            line_set = connected[0] if connected else None
        except ValueError:
            pass
    if brush is None and line_set:
        brush = _brush_for_input(line_set, 'Hidden Brush' if line_set.line_visibility == 'HIDDEN' else 'Brush')
    if detail is None:
        detail = _linked_node(brush, 'Brush Detail', 'LEABrushDetailNode')
    section = tree.panel_section
    if active and active.name != tree.panel_anchor:
        section = {'LEALineSetNode': 'LINE_LIST', 'LEAOutputNode': 'LINE_LIST',
                   'LEABrushNode': 'BRUSH', 'LEABrushDetailNode': 'DETAIL'}.get(active.bl_idname, section)
    return line_set, brush, detail, section


def _line_set_index_get(output):
    line_set = _panel_context(output.id_data)[0]
    for index, socket in enumerate(output.inputs):
        if socket.is_linked and socket.links[0].from_node == line_set:
            return index
    return min(output.get('_rea_ui_lineset_index', 0), max(0, len(output.inputs) - 1))


def _line_set_index_set(output, value):
    output['_rea_ui_lineset_index'] = value
    if 0 <= value < len(output.inputs):
        socket = output.inputs[value]
        if socket.is_linked and socket.links[0].from_node.bl_idname == 'LEALineSetNode':
            _activate_node(output.id_data, socket.links[0].from_node)


def _panel_width(context):
    region = getattr(context, 'region', None)
    width = getattr(region, 'width', 300)
    scale = getattr(getattr(getattr(context, 'preferences', None), 'system', None), 'ui_scale', 1.0)
    return width / max(scale, .5)


def _target_operator(layout, operator_id, node, text, icon):
    op = layout.operator(operator_id, text=text, icon=icon)
    op.tree_name = node.id_data.name
    op.node_name = node.name
    return op


def _section_header(layout, node, open_property, title, enabled_property=None):
    box = layout.box()
    header = box.row(align=True)
    header.prop(node, open_property, text="", emboss=False,
                icon="TRIA_DOWN" if getattr(node, open_property) else "TRIA_RIGHT")
    if enabled_property:
        header.prop(node, enabled_property, text=title)
    else:
        header.label(text=title)
    return box if getattr(node, open_property) else None


def _brush_action(layout, node, socket_name, action, text='', icon='NONE'):
    op = _target_operator(layout, 'lea_line.brush_connection', node, text, icon)
    op.socket_name, op.action = socket_name, action
    return op


def _draw_target_list(layout, node, material=False, rows=4):
    layout.label(text='Materials' if material else 'Objects')
    layout.template_list('LEA_UL_material_targets' if material else 'LEA_UL_object_targets',
                         node.name + ('mat' if material else 'obj'), node, 'materials' if material else 'objects',
                         node, 'material_index' if material else 'object_index', rows=rows)
    row = layout.row(align=True)
    _target_operator(row, 'lea_line.add_selected_materials' if material else 'lea_line.add_selected_objects', node, 'Add', 'ADD')
    remove = row.row(align=True)
    remove.enabled = bool(node.materials if material else node.objects)
    _target_operator(remove, 'lea_line.remove_material_target' if material else 'lea_line.remove_object_target', node, '', 'REMOVE')


def _draw_targets(layout, node, context):
    layout.prop(node, 'source_collection', text='Collection')
    if _panel_width(context) >= 440:
        row = layout.row()
        _draw_target_list(row.column(), node)
        _draw_target_list(row.column(), node, material=True)
    else:
        layout.row(align=True).prop(node, 'target_tab', expand=True)
        _draw_target_list(layout, node, material=node.target_tab == 'MATERIALS', rows=3)
    if node.materials:
        layout.label(text='Objects OR material faces', icon='MATERIAL')
        layout.label(text='Material targets: final render only', icon='INFO')
    elif not node.objects:
        layout.label(text='Empty targets: collection or scene', icon='INFO')


def _draw_brush_link(layout, node, socket_name, compact=False):
    direct = _linked_node(node, socket_name, 'LEABrushNode')
    brush = _brush_for_input(node, socket_name)
    row = layout.row(align=True)
    if direct:
        _brush_action(row, node, socket_name, 'SELECT', direct.name, 'BRUSH_DATA')
        _brush_action(row, node, socket_name, 'UNLINK', '', 'UNLINKED')
    else:
        row.label(text='Shared Brush' if brush else 'No Brush', icon='LINKED' if brush else 'BRUSH_DATA')
        _brush_action(row, node, socket_name, 'CREATE', 'New' if not compact else '', 'ADD')
        if brush:
            _brush_action(row, node, socket_name, 'SELECT', '', 'RESTRICT_SELECT_OFF')
    return brush


def _draw_brush_values(layout, brush):
    layout.use_property_split = True
    layout.use_property_decorate = False
    color = layout.row(align=True)
    color.use_property_split = False
    color.prop(brush, 'color', text='Color')
    color.prop(brush, 'alpha', text='Opacity')
    layout.prop(brush, 'thickness', text='Size')


def _sidebar_section(layout, identifier, title, owner=None, enabled_property=None, default_closed=False, native=True):
    """Use region-local native folds only from a full-width sidebar layout.

    Node extended controls and CPU layout doubles use an open box fallback.
    No authored node setting is changed by expanding or collapsing a section.
    """
    panel = getattr(layout, 'panel', None)
    if native and callable(panel):
        header, body = panel('rea_pencil_' + identifier, default_closed=default_closed)
    else:
        box = layout.box()
        header, body = box.row(align=True), box.column()
    if owner is not None and enabled_property:
        header.prop(owner, enabled_property, text=title)
    else:
        header.label(text=title)
    return header, body


def _draw_output_settings(context, layout, tree, runtime=False):
    """Expose existing output options without changing a different scene graph."""
    scene = getattr(context, 'scene', None)
    if scene is None or scene.lea_line.tree != tree:
        layout.label(text='This graph is not the scene output', icon='INFO')
        layout.label(text='Scene render settings are unchanged')
        return
    settings = scene.lea_line
    layout.use_property_split = True
    layout.use_property_decorate = False
    if not runtime:
        row = layout.row(align=True)
        row.use_property_split = False
        row.label(text='Line Size')
        row.prop_enum(settings, 'width_mode', 'ABSOLUTE', text='Absolute')
        row.prop_enum(settings, 'width_mode', 'RELATIVE', text='Relative')
        if settings.width_mode == 'RELATIVE':
            layout.prop(settings, 'reference_height')
        return
    layout.prop(settings, 'auto_sync')
    layout.prop(settings, 'fast_line_art', text='Line Art Render')
    if settings.fast_line_art:
        layout.prop(settings, 'stroke_width_profile', text='Coverage')
        layout.prop(settings, 'line_art_aa', text='Antialiasing')
    else:
        layout.prop(settings, 'reuse_line_pass', text='Reuse Line Pass')
    preview = layout.row(align=True)
    preview.use_property_split = False
    preview.prop(settings, 'preview_enabled', text='Live Preview')
    if settings.preview_enabled:
        preview.operator('lea_line.refresh_preview', text='', icon='FILE_REFRESH')
        layout.prop(settings, 'preview_simple_coverage')
        layout.prop(settings, 'preview_endpoint_coverage')
        layout.prop(settings, 'preview_feature_brushes')
    row = layout.row(align=True)
    row.operator('lea_line.apply', text='Apply', icon='FILE_REFRESH')
    row.operator('lea_line.render', text='Render', icon='RENDER_STILL')
    if settings.fast_line_art:
        layout.label(text='F12 uses Freestyle', icon='INFO')


def _draw_runtime_panel(context, layout, tree):
    scene = getattr(context, 'scene', None)
    if scene is None or scene.lea_line.tree != tree:
        return
    _, body = _sidebar_section(layout, 'render_preview', 'Render & Preview', default_closed=True)
    if body is not None:
        _draw_output_settings(context, body, tree, runtime=True)


def _draw_reduction_controls(context, layout, brush):
    if brush is None:
        layout.label(text='Connect a Brush to edit reduction', icon='INFO')
        return
    reduction = _linked_node(brush, 'Distance Reduction', 'LEADistanceReductionNode')
    socket = brush.inputs.get('Distance Reduction')
    row = layout.row(align=True)
    if reduction:
        _target_operator(row, 'lea_line.reduction_connection', brush, reduction.name, 'DRIVER_DISTANCE').action = 'SELECT'
        _target_operator(row, 'lea_line.reduction_connection', brush, '', 'UNLINKED').action = 'UNLINK'
        layout.prop(reduction, 'enabled', text='Use Reduction Node')
        controls = layout.column(align=True)
        controls.enabled = reduction.enabled
        controls.prop(reduction, 'reduce_thickness', text='Size Reduction')
        controls.prop(reduction, 'reduce_opacity', text='Alpha Reduction')
        if reduction.reduce_thickness or reduction.reduce_opacity:
            controls.prop(reduction, 'distance_start', text='Near')
            controls.prop(reduction, 'distance_end', text='Far')
        if reduction.reduce_thickness:
            row=controls.row();row.enabled=not reduction.curve_enabled
            row.prop(reduction, 'far_thickness', text='Far Size')
        if reduction.reduce_opacity:
            row=controls.row();row.enabled=not reduction.curve_enabled
            row.prop(reduction, 'far_alpha', text='Far Alpha')
        from . import reduction_reference
        reduction_reference.draw(controls,reduction)
        from . import distance_curves
        distance_curves.draw(controls,reduction)
    elif socket and socket.is_linked:
        row.label(text='Invalid Reduction connection', icon='ERROR')
        _target_operator(row, 'lea_line.reduction_connection', brush, '', 'UNLINKED').action = 'UNLINK'
    else:
        _target_operator(row, 'lea_line.reduction_connection', brush, 'Create Reduction Node', 'ADD').action = 'CREATE'
        layout.prop(brush, 'distance_fade', text='Size Reduction')
        layout.prop(brush, 'distance_alpha', text='Alpha Reduction')
        if brush.distance_fade or brush.distance_alpha:
            layout.prop(brush, 'distance_start', text='Near')
            layout.prop(brush, 'distance_end', text='Far')
        if brush.distance_fade:
            layout.prop(brush, 'far_thickness', text='Far Size')
        if brush.distance_alpha:
            layout.prop(brush, 'far_alpha', text='Far Alpha')


def _draw_line_visibility(layout, node, include_priority=True):
    if include_priority:
        layout.prop(node, 'render_priority', text='Render Order')
    layout.row(align=True).prop(node, 'line_visibility', expand=True)
    hidden = node.line_visibility == 'HIDDEN'
    layout.prop(node, 'hidden' if hidden else 'visible', text='Enable Hidden Lines' if hidden else 'Enable Visible Lines')
    return hidden


def _draw_line_brush(context, layout, node):
    hidden = node.line_visibility == 'HIDDEN'
    layout.enabled = node.enabled and (node.hidden if hidden else node.visible)
    brush = _draw_brush_link(layout, node, 'Hidden Brush' if hidden else 'Brush')
    if brush:
        _draw_brush_values(layout.column(), brush)
    return brush


def _draw_edge_controls(context, layout, node):
    hidden = node.line_visibility == 'HIDDEN'
    layout.enabled = node.enabled and (node.hidden if hidden else node.visible)
    if hidden:
        layout.prop(node, 'independent_hidden_edges', text='Independent Edge Types')
        if not node.independent_hidden_edges:
            layout.label(text='Uses visible edge types', icon='LINKED')
    prefix = 'hidden_' if hidden and node.independent_hidden_edges else ''
    labels = (('outline', 'Outline'), ('border', 'Open Border'), ('intersection', 'Intersection'),
              ('material_boundary', 'Material ID Boundary'), ('edge_mark', 'Selected Edges'),
              ('crease', 'Normal Angle / Crease'), ('ridge_valley', 'Ridge & Valley'),
              ('suggestive_contour', 'Suggestive Contour'))
    for feature, label in labels:
        row = layout.row()
        row.enabled = not hidden or node.independent_hidden_edges
        row.prop(node, prefix + feature, text=label)
        if feature == 'intersection':
            if getattr(node, prefix + feature):
                layout.label(text='Line Art only', icon='INFO')
            continue
        subgroup = layout.box()
        subgroup.enabled = bool(getattr(node, prefix + feature))
        if feature == 'crease':
            subgroup.prop(node, 'crease_angle', text='Crease Angle')
        socket_name = ('Hidden ' if hidden else '') + feature + ' Brush'
        row = subgroup.row(align=True)
        direct = _linked_node(node, socket_name, 'LEABrushNode')
        _brush_action(row, node, socket_name, 'UNLINK' if direct else 'CREATE', 'Specific Brush',
                      'CHECKBOX_HLT' if direct else 'CHECKBOX_DEHLT')
        if direct:
            _brush_action(row, node, socket_name, 'SELECT', direct.name, 'BRUSH_DATA')
    layout.label(text='Crease angle shared across sets', icon='INFO')


def _draw_detail_button(header, brush):
    if brush:
        detail = _linked_node(brush, 'Brush Detail', 'LEABrushDetailNode')
        _target_operator(header, 'lea_line.detail_connection', brush, 'Detail', 'NODE' if detail else 'ADD').action = 'SELECT' if detail else 'CREATE'


def _draw_line_set_settings(context, layout, node, include_targets=True, sidebar=False):
    if sidebar:
        header, body = _sidebar_section(layout, 'line_set', 'Line Set', node, 'enabled')
        header.prop(node, 'render_priority', text='Order')
        if body is None:
            return
        body.prop(node, 'name', text='Name')
    else:
        body = layout.column()
        body.prop(node, 'enabled', text=node.name)
    body.enabled = node.enabled
    if include_targets:
        targets = _section_header(body, node, 'show_targets', 'Targets')
        if targets:
            _draw_targets(targets, node, context)
    hidden = _draw_line_visibility(body, node, include_priority=not sidebar)
    brush = _brush_for_input(node, 'Hidden Brush' if hidden else 'Brush')
    header, brush_body = _sidebar_section(layout, 'line_brush', 'Brush') if sidebar else _sidebar_section(body, 'line_brush', 'Brush', native=False)
    _draw_detail_button(header, brush)
    if brush_body is not None:
        _draw_line_brush(context, brush_body, node)
    _, edge_body = _sidebar_section(layout, 'line_edge', 'Edge') if sidebar else _sidebar_section(body, 'line_edge', 'Edge', native=False)
    if edge_body is not None:
        _draw_edge_controls(context, edge_body, node)
    _, reduction_body = _sidebar_section(layout, 'line_reduction', 'Reduction', default_closed=True) if sidebar else _sidebar_section(body, 'line_reduction', 'Reduction', native=False)
    if reduction_body is not None:
        reduction_body.enabled = node.enabled and (node.hidden if hidden else node.visible)
        from . import independent_reduction
        independent_reduction.draw(reduction_body, node)
        _draw_reduction_controls(context, reduction_body, brush)


class LEALineSetNode(_LEANode, bpy.types.Node):
    is_mask_hidden_lines: BoolProperty(name='Mask Hidden Lines of Other Line Sets',default=False,update=_sync_if_enabled)
    use_material_functions: BoolProperty(name='Material Line Functions',default=False,update=_sync_if_enabled)
    bl_idname = "LEALineSetNode"
    bl_label = "Line Set"
    target_mode: EnumProperty(name='Target Objects', items=[('ALL', 'All if empty', 'Legacy workflow: an empty object list includes all meshes'),
        ('LISTED', 'Listed targets only', 'No object or material targets means no lines; a source collection may define the object scope')], default='ALL', update=_sync_if_enabled)
    bl_icon = "GREASEPENCIL"

    enabled: BoolProperty(name="Enabled", default=True, update=_sync_if_enabled)
    render_priority: IntProperty(name="Render Priority", description="Lower values are applied first; equal priorities preserve line-output socket order", min=0, max=65535, default=0, update=_sync_if_enabled)
    v_size_reduction_on: BoolProperty(name='Size Reduction', default=False, update=_sync_if_enabled)
    v_alpha_reduction_on: BoolProperty(name='Alpha Reduction', default=False, update=_sync_if_enabled)
    h_size_reduction_on: BoolProperty(name='Size Reduction', default=False, update=_sync_if_enabled)
    h_alpha_reduction_on: BoolProperty(name='Alpha Reduction', default=False, update=_sync_if_enabled)
    visible: BoolProperty(name="Visible Lines", default=True, update=_sync_if_enabled)
    hidden: BoolProperty(name="Hidden Lines", default=False, update=_sync_if_enabled)
    independent_hidden_edges: BoolProperty(name="Independent Hidden Edges", description="Choose hidden edge types separately; disabled preserves the original shared selection", default=False, update=_hidden_selection_changed)
    outline: BoolProperty(name="Outline", default=True, update=_sync_if_enabled)
    crease: BoolProperty(name="Crease", default=True, update=_sync_if_enabled)
    material_boundary: BoolProperty(name="Material Boundary", default=False, update=_sync_if_enabled)
    edge_mark: BoolProperty(name="Marked Edges", default=False, update=_sync_if_enabled)
    border: BoolProperty(name="Open Border", default=True, update=_sync_if_enabled)
    ridge_valley: BoolProperty(name="Ridge & Valley", default=False, update=_sync_if_enabled)
    suggestive_contour: BoolProperty(name="Suggestive Contour", default=False, update=_sync_if_enabled)
    intersection: BoolProperty(name="Intersection (Line Art)", description="Generate intersections in Line Art preview and experimental Line Art renders; unsupported by Freestyle", default=False, update=_sync_if_enabled)
    hidden_outline: BoolProperty(name="Outline", default=True, update=_sync_if_enabled)
    hidden_border: BoolProperty(name="Open Border", default=True, update=_sync_if_enabled)
    hidden_crease: BoolProperty(name="Crease", default=True, update=_sync_if_enabled)
    hidden_material_boundary: BoolProperty(name="Material Boundary", default=False, update=_sync_if_enabled)
    hidden_edge_mark: BoolProperty(name="Marked Edges", default=False, update=_sync_if_enabled)
    hidden_ridge_valley: BoolProperty(name="Ridge & Valley", default=False, update=_sync_if_enabled)
    hidden_suggestive_contour: BoolProperty(name="Suggestive Contour", default=False, update=_sync_if_enabled)
    hidden_intersection: BoolProperty(name="Intersection (Preview Only)", description="Hidden intersections in Line Art preview only; unsupported by Freestyle", default=False, update=_sync_if_enabled)
    crease_angle: FloatProperty(name="Crease Angle", description="Freestyle has one shared crease angle per view layer; different line-set angles cannot be rendered independently", subtype="ANGLE", min=0.0, max=math.pi, default=math.radians(134), update=_sync_if_enabled)
    normal_angle_min: FloatProperty(name='Min', description='Minimum normal angle; Max stays180degrees. Final Freestyle shares one angle per view layer',
        subtype='ANGLE', min=0., max=math.pi, get=lambda node: max(0., math.pi-node.crease_angle),
        set=lambda node,value: setattr(node,'crease_angle',math.pi-value))
    source_collection: PointerProperty(name="Collection", description="Limit object and material targets to this collection; leave empty for the scene", type=bpy.types.Collection, update=_sync_if_enabled)
    objects: CollectionProperty(name="Objects", description="Explicit object targets; an empty list uses the chosen collection or scene", type=LEAObjectTarget)
    object_index: IntProperty(name="Active Object Target", min=0, default=0)
    materials: CollectionProperty(name="Materials", description="Select faces using these materials, combined with explicit objects within the collection", type=LEAMaterialTarget)
    material_index: IntProperty(name="Active Material Target", min=0, default=0)
    target_tab: EnumProperty(name="Target Type", items=[('OBJECTS', 'Objects', 'Whole objects'), ('MATERIALS', 'Materials', 'Faces using selected materials')], default='OBJECTS')
    line_visibility: EnumProperty(name='Line Visibility', items=[('VISIBLE', 'Visible Lines', 'Edit visible lines'), ('HIDDEN', 'Hidden Lines', 'Edit hidden lines')], default='VISIBLE')
    show_targets: BoolProperty(name="Targets", default=False)
    show_visible_settings: BoolProperty(name="Visible Lines Section", default=True)
    show_hidden_settings: BoolProperty(name="Hidden Lines Section", default=False)
    show_overrides: BoolProperty(name="Show Feature Brushes", description="Show separate visible and hidden brush inputs for each edge type; connected inputs always remain visible", default=False, update=_refresh_override_visibility)

    def init(self, context):
        self.width = 140 if self.id_data.ui_layout == 'ORIGINAL' else 235
        with suspend_sync():
            for name in ("Brush", "Hidden Brush") + OVERRIDE_SOCKET_NAMES + independent_reduction.SOCKETS:
                self.inputs.new("LEALineSocket", name).link_limit = 1
            self.outputs.new("LEALineSocket", "Line Set")
            _refresh_override_visibility(self)

    def draw_buttons(self, context, layout):
        if self.id_data.ui_layout == 'ORIGINAL':
            layout.prop(self, 'enabled', text='On')
            return
        layout.prop(self, 'enabled')
        scope = (f'{len(self.objects)} objects / {len(self.materials)} materials' if self.materials
                 else f'{len(self.objects)} objects' if self.objects
                 else self.source_collection.name if self.source_collection else 'Scene')
        layout.label(text=scope, icon='OBJECT_DATA')
        layout.prop(self, 'show_overrides')
        layout.label(text='N > REA Pencil Line: settings')

    def draw_buttons_ext(self, context, layout):
        self.draw_controls(context, layout)

    def draw_controls(self, context, layout):
        _draw_line_set_settings(context, layout, self)


class LEABrushNode(_LEANode, bpy.types.Node):
    bl_idname = "LEABrushNode"
    bl_label = "Brush Settings"
    bl_icon = "GREASEPENCIL"

    color: FloatVectorProperty(name="Color", subtype="COLOR", size=3, min=0.0, max=1.0, default=(0.015, 0.015, 0.015), update=_sync_if_enabled)
    alpha: FloatProperty(name="Opacity", min=0.0, max=1.0, default=1.0, update=_sync_if_enabled)
    linear_alpha: BoolProperty(name='Linear Alpha',description='Correct Freestyle alpha decoding; opt in to preserve legacy file appearance',default=False,update=_sync_if_enabled)
    thickness: FloatProperty(name="Thickness", description="Stroke width in render pixels", min=0.1, max=1000.0, default=2.0, precision=2, update=_sync_if_enabled)
    from .texture_maps import enabled_changed as _map_changed
    color_map_on: BoolProperty(name='Color Map',default=False,update=_map_changed)
    color_map_opacity: FloatProperty(name='Opacity',min=0,max=1,default=1.,update=_sync_if_enabled)
    size_map_on: BoolProperty(name='Size Map',default=False,update=_map_changed)
    size_map_amount: FloatProperty(name='Amount',min=0,max=1,default=1.,update=_sync_if_enabled)
    caps: EnumProperty(name="Caps", items=[("ROUND", "Round", "Rounded ends"), ("BUTT", "Butt", "Flat ends"), ("SQUARE", "Square", "Extended square ends")], default="ROUND", update=_sync_if_enabled)
    dashed: BoolProperty(name="Dashed", default=False, update=_sync_if_enabled)
    dash: IntProperty(name="Dash", min=1, max=10000, default=10, update=_sync_if_enabled)
    gap: IntProperty(name="Gap", min=1, max=10000, default=5, update=_sync_if_enabled)
    taper: BoolProperty(name="Taper", description="Vary stroke thickness along its length", default=False, update=_sync_if_enabled)
    taper_start: FloatProperty(name="Start Thickness", description="Thickness fraction at the start of each stroke", min=0.0, max=1.0, default=0.2, update=_sync_if_enabled)
    taper_end: FloatProperty(name="End Thickness", description="Thickness fraction at the end of each stroke", min=0.0, max=1.0, default=0.2, update=_sync_if_enabled)
    distance_fade: BoolProperty(name="Distance Reduction", description="Reduce stroke thickness with distance from the camera", default=False, update=_sync_if_enabled)
    distance_start: FloatProperty(name="Near Distance", description="Camera distance where thickness reduction begins", min=0.0, default=0.0, update=_sync_if_enabled)
    distance_end: FloatProperty(name="Far Distance", description="Camera distance where the far thickness is reached", min=0.0, default=50.0, update=_sync_if_enabled)
    far_thickness: FloatProperty(name="Far Thickness", description="Thickness fraction at the far distance", min=0.0, max=1.0, default=0.3, update=_sync_if_enabled)
    distance_alpha: BoolProperty(name="Distance Opacity", description="Reduce stroke opacity using the same near and far distances as thickness reduction", default=False, update=_sync_if_enabled)
    far_alpha: FloatProperty(name="Far Opacity", description="Opacity fraction at the far distance", min=0.0, max=1.0, default=0.1, update=_sync_if_enabled)
    refer_object_on: BoolProperty(name='Refer Object',description='Use distance from Reference Object; an empty reference uses the world origin',default=False,update=_sync_if_enabled)
    object_reference: PointerProperty(name='Reference Object',type=bpy.types.Object,update=_sync_if_enabled)
    def init(self, context):
        self.width = 140 if self.id_data.ui_layout == 'ORIGINAL' else 205
        self.inputs.new("LEALineSocket", "Brush Detail").link_limit = 1
        self.inputs.new('LEALineSocket','Color Map').link_limit=1
        self.inputs.new('LEALineSocket','Size Map').link_limit=1
        self.inputs.new("LEALineSocket", "Distance Reduction").link_limit = 1
        self.outputs.new("LEALineSocket", "Brush")

    def draw_buttons(self, context, layout):
        if self.id_data.ui_layout == 'ORIGINAL':
            return
        layout.prop(self, 'color', text='')
        layout.prop(self, 'thickness')
        layout.label(text='N > REA Pencil Line: settings')

    def draw_buttons_ext(self, context, layout):
        self.draw_controls(context, layout)

    def draw_controls(self, context, layout, include_reduction=True):
        _draw_brush_values(layout, self)
        from . import texture_maps
        for kind in ('color','size'):texture_maps.draw_brush_map(context,layout,self,kind)
        detail = _linked_node(self, 'Brush Detail', 'LEABrushDetailNode')
        row = layout.row(align=True)
        op = _target_operator(row, 'lea_line.detail_connection', self, detail.name if detail else 'Create Brush Detail', 'NODE' if detail else 'ADD')
        op.action = 'SELECT' if detail else 'CREATE'
        if detail:
            _target_operator(row, 'lea_line.detail_connection', self, '', 'UNLINKED').action = 'UNLINK'
            row = layout.row()
            row.prop(detail, 'enabled', text='Use Brush Detail')
        if detail and detail.enabled:
            layout.label(text='Stroke settings controlled by Brush Detail', icon='LINKED')
        else:
            stroke = layout.box()
            stroke.label(text='Inline Stroke Settings')
            stroke.prop(self, "caps")
            stroke.prop(self, "dashed")
            if self.dashed:
                row = stroke.row(align=True)
                row.prop(self, "dash")
                row.prop(self, "gap")
            stroke.prop(self, "taper")
            if self.taper:
                stroke.prop(self, "taper_start")
                stroke.prop(self, "taper_end")
        if include_reduction:
            box = layout.box()
            box.label(text='Reduction')
            _draw_reduction_controls(context, box, self)


def _distance_curve_changed(owner,context):
    from . import curves
    if owner.curve_enabled:curves.ensure_detail(owner)
    _sync_if_enabled(owner,context)


class LEADistanceReductionNode(_LEANode, bpy.types.Node):
    bl_idname = "LEADistanceReductionNode"
    bl_label = "Distance Reduction"
    bl_icon = "DRIVER_DISTANCE"

    enabled: BoolProperty(name="Enabled", description="Enable this linked reduction; disabled overrides inline reduction with no reduction", default=True, update=_sync_if_enabled)
    reduce_thickness: BoolProperty(name="Reduce Thickness", default=True, update=_sync_if_enabled)
    reduce_opacity: BoolProperty(name="Reduce Opacity", default=False, update=_sync_if_enabled)
    distance_start: FloatProperty(name="Near Distance", subtype='DISTANCE', min=0.0, default=1.0, update=_sync_if_enabled)
    distance_end: FloatProperty(name="Far Distance", subtype='DISTANCE', min=0.0, default=10.0, update=_sync_if_enabled)
    far_thickness: FloatProperty(name="Far Thickness", description="Thickness fraction at the far distance", min=0.0, max=1.0, default=0.3, update=_sync_if_enabled)
    far_alpha: FloatProperty(name="Far Opacity", description="Opacity fraction at the far distance", min=0.0, max=1.0, default=0.1, update=_sync_if_enabled)
    refer_object_on: BoolProperty(name='Refer Object',description='Use distance from Reference Object; an empty reference uses the world origin',default=False,update=_sync_if_enabled)
    object_reference: PointerProperty(name='Reference Object',type=bpy.types.Object,update=_sync_if_enabled)
    curve_enabled: BoolProperty(name='Use Reduction Curve',default=False,update=_distance_curve_changed)
    curve_tree: PointerProperty(type=bpy.types.NodeTree,poll=lambda self,tree:tree.bl_idname=='ShaderNodeTree',options={'HIDDEN'})

    def init(self, context):
        self.width = 205
        self.outputs.new("LEALineSocket", "Reduction")
        from . import curves
        curves.ensure_detail(self)

    def copy(self,source):
        from . import curves
        curves.copy_detail(self,source)

    def free(self):
        from . import curves
        curves.free_detail(self)

    def draw_buttons(self, context, layout):
        layout.prop(self, 'enabled')
        layout.label(text='Object distance' if self.refer_object_on else 'Camera distance', icon='DRIVER_DISTANCE')
        layout.label(text='N > REA Pencil Line: settings')

    def draw_buttons_ext(self, context, layout):
        self.draw_controls(context, layout)

    def draw_controls(self, context, layout):
        layout.prop(self, 'enabled')
        controls = layout.column()
        controls.enabled = self.enabled
        controls.prop(self, 'distance_start')
        controls.prop(self, 'distance_end')
        if independent_reduction.connected_channels(self):
            from . import reduction_reference, distance_curves
            distance_curves.draw(controls,self)
            reduction_reference.draw(controls,self)
            independent_reduction.draw_channel_settings(controls,self)
            return
        controls.prop(self, 'reduce_thickness')
        if self.reduce_thickness:
            row=controls.row();row.enabled=not self.curve_enabled;row.prop(self, 'far_thickness')
        controls.prop(self, 'reduce_opacity')
        if self.reduce_opacity:
            row=controls.row();row.enabled=not self.curve_enabled;row.prop(self, 'far_alpha')
        layout.label(text='Connect Reduction to a Brush input')
        from . import reduction_reference
        reduction_reference.draw(controls,self)
        from . import distance_curves
        distance_curves.draw(controls,self)


class _LEATargetOperator:
    tree_name: StringProperty(name="Node Tree")
    node_name: StringProperty(name="Line Set")

    def target_node(self, context):
        tree = bpy.data.node_groups.get(self.tree_name) if self.tree_name else context.scene.lea_line.tree
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None or node.bl_idname != "LEALineSetNode":
            self.report({"ERROR"}, "The target Line Set no longer exists")
            return None
        return node


class LEA_OT_add_selected_objects(_LEATargetOperator, bpy.types.Operator):
    bl_idname = "lea_line.add_selected_objects"
    bl_label = "Add Selected Objects"
    bl_description = "Add selected scene objects to this Line Set without changing source objects"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        node = self.target_node(context)
        if node is None:
            return {"CANCELLED"}
        selected = sorted(context.selected_objects, key=lambda item: item.name)
        if not selected:
            self.report({"INFO"}, "Select scene objects to add")
            return {"CANCELLED"}
        added = 0
        with suspend_sync():
            existing = {item.object for item in node.objects if item.object}
            for obj in selected:
                if obj not in existing:
                    node.objects.add().object = obj
                    existing.add(obj)
                    added += 1
            if node.objects:
                node.object_index = len(node.objects) - 1
        _sync_if_enabled(node, context)
        self.report({"INFO"}, f"Added {added} object target(s)")
        return {"FINISHED"}


class LEA_OT_remove_object_target(_LEATargetOperator, bpy.types.Operator):
    bl_idname = "lea_line.remove_object_target"
    bl_label = "Remove Object Target"
    bl_description = "Remove the active object target from this Line Set"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        node = self.target_node(context)
        if node is None or not node.objects:
            return {"CANCELLED"}
        with suspend_sync():
            node.objects.remove(min(node.object_index, len(node.objects) - 1))
            node.object_index = max(0, min(node.object_index, len(node.objects) - 1))
        _sync_if_enabled(node, context)
        return {"FINISHED"}


class LEA_OT_add_selected_materials(_LEATargetOperator, bpy.types.Operator):
    bl_idname = "lea_line.add_selected_materials"
    bl_label = "Add Selected Materials"
    bl_description = "Add materials from every slot of selected objects, skipping empty slots and duplicates"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        node = self.target_node(context)
        if node is None:
            return {'CANCELLED'}
        selected = {slot.material for obj in context.selected_objects for slot in obj.material_slots if slot.material}
        if not selected:
            self.report({'INFO'}, 'Selected objects have no materials')
            return {'CANCELLED'}
        added = 0
        with suspend_sync():
            existing = {item.material for item in node.materials if item.material}
            for material in sorted(selected - existing, key=lambda item: item.name):
                node.materials.add().material = material
                added += 1
            node.material_index = max(0, len(node.materials) - 1)
            node.target_tab = 'MATERIALS'
        _sync_if_enabled(node, context)
        self.report({'INFO'}, f'Added {added} material target(s)')
        return {'FINISHED'}


class LEA_OT_remove_material_target(_LEATargetOperator, bpy.types.Operator):
    bl_idname = 'lea_line.remove_material_target'
    bl_label = 'Remove Material Target'
    bl_description = 'Remove the active material target without changing any object material slots'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        node = self.target_node(context)
        if node is None or not node.materials:
            return {'CANCELLED'}
        with suspend_sync():
            node.materials.remove(min(node.material_index, len(node.materials) - 1))
            node.material_index = max(0, min(node.material_index, len(node.materials) - 1))
        _sync_if_enabled(node, context)
        return {'FINISHED'}


class LEA_OT_select_line_set(_LEATargetOperator, bpy.types.Operator):
    bl_idname = 'lea_line.select_line_set'
    bl_label = 'Select Line Set'
    bl_description = 'Make this Line Set the active node in the graph and settings sidebar'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        node = self.target_node(context)
        if node is None:
            return {'CANCELLED'}
        _activate_node(node.id_data, node)
        return {'FINISHED'}


class LEA_OT_select_brush(_LEATargetOperator, bpy.types.Operator):
    bl_idname = 'lea_line.select_brush'
    bl_label = 'Select Connected Brush'
    bl_description = 'Show the connected Brush settings; hidden lines use the visible brush when no hidden brush is connected'
    bl_options = {'REGISTER', 'UNDO'}
    socket_name: StringProperty(name='Brush Input', default='Brush')

    def execute(self, context):
        node = self.target_node(context)
        if node is None:
            return {'CANCELLED'}
        socket = node.inputs.get(self.socket_name)
        if self.socket_name == 'Hidden Brush' and socket and not socket.is_linked:
            socket = node.inputs.get('Brush')
        if not socket or not socket.is_linked:
            self.report({'INFO'}, 'Connect a Brush to this input first')
            return {'CANCELLED'}
        brush = socket.links[0].from_node
        if brush.bl_idname != 'LEABrushNode':
            self.report({'ERROR'}, 'This input is not connected to a Brush node')
            return {'CANCELLED'}
        _activate_node(node.id_data, brush)
        return {'FINISHED'}


class LEA_OT_panel_section(bpy.types.Operator):
    bl_idname = 'lea_line.panel_section'
    bl_label = 'Show Settings Section'
    bl_description = 'Navigate the selected Line Set and connected Brush; does not change line settings'
    tree_name: StringProperty()
    section: EnumProperty(items=[('LINE_LIST', 'Line List', ''), ('BRUSH', 'Brush Settings', ''), ('DETAIL', 'Brush Detail', '')])

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.tree_name)
        if not tree or tree.bl_idname != 'LEALineTree':
            return {'CANCELLED'}
        line_set, brush, detail, _ = _panel_context(tree)
        tree.panel_line_set = line_set.name if line_set else ''
        tree.panel_brush = brush.name if brush else ''
        target = {'LINE_LIST': line_set, 'BRUSH': brush, 'DETAIL': detail}.get(self.section)
        if target:
            _activate_node(tree, target)
        tree.panel_section = self.section
        tree.panel_anchor = tree.nodes.active.name if tree.nodes.active else ''
        return {'FINISHED'}


class LEA_OT_brush_connection(_LEATargetOperator, bpy.types.Operator):
    bl_idname = 'lea_line.brush_connection'
    bl_label = 'Brush Connection'
    bl_description = 'Create a brush from the effective settings, select it, or disconnect while preserving its node'
    bl_options = {'REGISTER', 'UNDO'}
    socket_name: StringProperty(default='Brush')
    action: EnumProperty(items=[('CREATE', 'Create Brush', ''), ('SELECT', 'Select Brush', ''), ('UNLINK', 'Disconnect Brush', '')])

    def execute(self, context):
        node = self.target_node(context)
        if node is None or self.socket_name not in ('Brush', 'Hidden Brush') + OVERRIDE_SOCKET_NAMES:
            return {'CANCELLED'}
        socket = node.inputs.get(self.socket_name)
        if socket is None:
            self.report({'ERROR'}, 'Apply settings once to migrate this graph')
            return {'CANCELLED'}
        tree = node.id_data
        tree.panel_line_set = node.name
        if self.action == 'SELECT':
            brush = _brush_for_input(node, self.socket_name)
            if not brush:
                return {'CANCELLED'}
            _activate_node(tree, brush)
            return {'FINISHED'}
        if self.action == 'UNLINK':
            if not socket.is_linked:
                return {'CANCELLED'}
            with suspend_sync():
                for link in list(socket.links):
                    tree.links.remove(link)
                _refresh_override_visibility(node)
                _activate_node(tree, node)
            _sync_if_enabled(node, context)
            return {'FINISHED'}
        if socket.is_linked:
            self.report({'INFO'}, 'This input already has a connection')
            return {'CANCELLED'}
        base = _brush_for_input(node, self.socket_name)
        with suspend_sync():
            brush = tree.nodes.new('LEABrushNode')
            brush.location = (node.location.x - 280, node.location.y - 160)
            if base:
                for name in LEABrushNode.__annotations__:
                    setattr(brush, name, getattr(base, name))
                for source in base.inputs:
                    for link in source.links:
                        tree.links.new(link.from_socket, brush.inputs[source.name])
            else:
                detail = tree.nodes.new('LEABrushDetailNode')
                initialize_from_brush(detail, brush)
                detail.location = (brush.location.x - 260, brush.location.y)
                tree.links.new(detail.outputs[0], brush.inputs['Brush Detail'])
            tree.links.new(brush.outputs[0], socket)
            _refresh_override_visibility(node)
            _activate_node(tree, brush)
        _sync_if_enabled(node, context)
        return {'FINISHED'}


class LEA_OT_detail_connection(bpy.types.Operator):
    bl_idname = 'lea_line.detail_connection'
    bl_label = 'Brush Detail Connection'
    bl_description = 'Edit or create shared stroke settings; disconnecting preserves the authored Detail node'
    bl_options = {'REGISTER', 'UNDO'}
    tree_name: StringProperty()
    node_name: StringProperty()
    action: EnumProperty(items=[('CREATE', 'Create Detail', ''), ('SELECT', 'Select Detail', ''), ('UNLINK', 'Disconnect Detail', '')])

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.tree_name)
        brush = tree.nodes.get(self.node_name) if tree and tree.bl_idname == 'LEALineTree' else None
        if brush is None or brush.bl_idname != 'LEABrushNode':
            return {'CANCELLED'}
        socket = brush.inputs.get('Brush Detail')
        if socket is None:
            self.report({'ERROR'}, 'Apply settings once to migrate this graph')
            return {'CANCELLED'}
        detail = _linked_node(brush, 'Brush Detail', 'LEABrushDetailNode')
        tree.panel_brush = brush.name
        if self.action == 'SELECT':
            if detail:
                _activate_node(tree, detail)
            else:
                _activate_node(tree, brush)
                tree.panel_section = 'DETAIL'
            return {'FINISHED'}
        if self.action == 'UNLINK':
            if not socket.is_linked:
                return {'CANCELLED'}
            with suspend_sync():
                for link in list(socket.links):
                    tree.links.remove(link)
                _activate_node(tree, brush)
            _sync_if_enabled(brush, context)
            return {'FINISHED'}
        if socket.is_linked:
            self.report({'INFO'}, 'This Brush already has Detail connected')
            return {'CANCELLED'}
        with suspend_sync():
            detail = tree.nodes.new('LEABrushDetailNode')
            initialize_from_brush(detail, brush)
            detail.location = (brush.location.x - 260, brush.location.y)
            tree.links.new(detail.outputs[0], socket)
            _activate_node(tree, detail)
        _sync_if_enabled(brush, context)
        return {'FINISHED'}


class LEA_OT_reduction_connection(bpy.types.Operator):
    bl_idname = 'lea_line.reduction_connection'
    bl_label = 'Distance Reduction Connection'
    bl_description = 'Create from inline distance settings, select, or disconnect while preserving the Reduction node'
    bl_options = {'REGISTER', 'UNDO'}
    tree_name: StringProperty()
    node_name: StringProperty()
    action: EnumProperty(items=[('CREATE', 'Create Reduction', ''), ('SELECT', 'Select Reduction', ''), ('UNLINK', 'Disconnect Reduction', '')])

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.tree_name)
        brush = tree.nodes.get(self.node_name) if tree and tree.bl_idname == 'LEALineTree' else None
        if brush is None or brush.bl_idname != 'LEABrushNode':
            return {'CANCELLED'}
        socket = brush.inputs.get('Distance Reduction')
        if socket is None:
            self.report({'ERROR'}, 'Apply settings once to migrate this graph')
            return {'CANCELLED'}
        reduction = _linked_node(brush, 'Distance Reduction', 'LEADistanceReductionNode')
        if self.action == 'SELECT':
            if reduction is None:
                return {'CANCELLED'}
            tree.panel_brush = brush.name
            _activate_node(tree, reduction)
            return {'FINISHED'}
        if self.action == 'UNLINK':
            if not socket.is_linked:
                return {'CANCELLED'}
            with suspend_sync():
                for link in list(socket.links):
                    tree.links.remove(link)
                _activate_node(tree, brush)
            _sync_if_enabled(brush, context)
            return {'FINISHED'}
        if socket.is_linked:
            self.report({'INFO'}, 'This Brush already has a Reduction connection')
            return {'CANCELLED'}
        with suspend_sync():
            reduction = tree.nodes.new('LEADistanceReductionNode')
            reduction.reduce_thickness = brush.distance_fade
            reduction.reduce_opacity = brush.distance_alpha
            for name in ('distance_start', 'distance_end', 'far_thickness', 'far_alpha'):
                setattr(reduction, name, getattr(brush, name))
            reduction.location = (brush.location.x - 260, brush.location.y - 240)
            tree.links.new(reduction.outputs[0], socket)
            tree.panel_brush = brush.name
            _activate_node(tree, reduction)
        _sync_if_enabled(brush, context)
        return {'FINISHED'}


class LEA_OT_duplicate_line_set(_LEATargetOperator, bpy.types.Operator):
    bl_idname = 'lea_line.duplicate_line_set'
    bl_label = 'Duplicate Line Set'
    bl_description = 'Duplicate a connected Line Set and its targets, sharing all linked Brushes; the source is unchanged'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        source = self.target_node(context)
        if source is None:
            return {'CANCELLED'}
        tree = source.id_data
        try:
            output = _line_output(tree)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        if source not in _connected_line_sets(output):
            self.report({'INFO'}, 'Connect this Line Set to the active Line Output first')
            return {'CANCELLED'}
        with suspend_sync():
            from .node_workflow import available_socket
            free_socket = available_socket(output)
            node = tree.nodes.new('LEALineSetNode')
            node.name = source.name + ' Copy'
            node.label = source.label
            node.location = (source.location.x, source.location.y - 240)
            # Copy authored RNA fields, not backend ownership IDs/custom data.
            # Target collections need separate entries; object/material IDs stay shared.
            for name in LEALineSetNode.__annotations__:
                if name not in {'objects', 'materials', 'normal_angle_min'}:
                    setattr(node, name, getattr(source, name))
            for target in source.objects:
                node.objects.add().object = target.object
            for target in source.materials:
                node.materials.add().material = target.material
            if source.get('rea_hidden_edges_initialized'):
                node['rea_hidden_edges_initialized'] = True
            for socket in source.inputs:
                for link in socket.links:
                    tree.links.new(link.from_socket, node.inputs[socket.name])
            tree.links.new(node.outputs[0], free_socket)
            _refresh_override_visibility(node)
            _activate_node(tree, node)
        _sync_if_enabled(node, context)
        return {'FINISHED'}


class LEA_OT_disconnect_line_set(_LEATargetOperator, bpy.types.Operator):
    bl_idname = 'lea_line.disconnect_line_set'
    bl_label = 'Remove From Output'
    bl_description = 'Disconnect this Line Set from Line Outputs, preserving it and its Brushes as an editable draft'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        node = self.target_node(context)
        if node is None:
            return {'CANCELLED'}
        tree = node.id_data
        links = [link for link in tree.links if link.from_node == node and link.to_node.bl_idname == 'LEAOutputNode']
        if not links:
            return {'CANCELLED'}
        with suspend_sync():
            for link in links:
                tree.links.remove(link)
            _activate_node(tree, node)
        _sync_if_enabled(node, context)
        return {'FINISHED'}


class LEA_OT_move_line_set(_LEATargetOperator, bpy.types.Operator):
    bl_idname = 'lea_line.move_line_set'
    bl_label = 'Move Line Set'
    bl_description = 'Move a connected Line Set in render order; renumber connected priorities without changing drafts'
    bl_options = {'REGISTER', 'UNDO'}
    direction: EnumProperty(items=[('UP', 'Up', 'Render earlier'), ('DOWN', 'Down', 'Render later')], default='UP')

    def execute(self, context):
        node = self.target_node(context)
        if node is None:
            return {'CANCELLED'}
        try:
            nodes = _connected_line_sets(_line_output(node.id_data))
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        if node not in nodes:
            return {'CANCELLED'}
        index = nodes.index(node)
        other = index + (-1 if self.direction == 'UP' else 1)
        if not 0 <= other < len(nodes):
            return {'CANCELLED'}
        nodes[index], nodes[other] = nodes[other], nodes[index]
        with suspend_sync():
            for priority, candidate in enumerate(nodes):
                candidate.render_priority = priority
            _activate_node(node.id_data, node)
        _sync_if_enabled(node, context)
        return {'FINISHED'}


class LEA_OT_new_graph(bpy.types.Operator):
    bl_idname = "lea_line.new_graph"
    bl_label = "Create Line Graph"
    bl_description = "Create a line output, line set and brush connected together"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from . import backend
        created = context.scene.lea_line.tree is None
        tree = backend.ensure_default_tree(context.scene, create_detail=True)
        context.scene.lea_line.tree = tree
        if created:
            for node in tree.nodes:
                if node.bl_idname == 'LEALineSetNode':node.target_mode = 'LISTED'
        output = _line_output(tree)
        if output:
            tree.line_index = list(tree.nodes).index(output)
        warnings = backend.sync_scene(context.scene, tree)
        if context.area and context.area.type == "NODE_EDITOR":
            context.space_data.tree_type = "LEALineTree"
            context.space_data.pin = False
        if warnings:
            self.report({"WARNING"}, "; ".join(warnings))
        return {"FINISHED"}


class LEA_OT_apply(bpy.types.Operator):
    bl_idname = "lea_line.apply"
    bl_label = "Apply Line Settings"
    bl_description = "Synchronize this line graph with native Blender Freestyle"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from . import backend
        try:
            warnings = backend.sync_scene(context.scene)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        context.scene.lea_line.last_message = "\n".join(warnings) if warnings else "rea pencil for blender settings synchronized"
        self.report({"WARNING"} if warnings else {"INFO"}, "; ".join(warnings) if warnings else "rea pencil for blender settings synchronized")
        return {"FINISHED"}


class LEA_OT_add_line_set(bpy.types.Operator):
    bl_idname = "lea_line.add_line_set"
    bl_label = "Add Line Set and Brush"
    bl_options = {"REGISTER", "UNDO"}
    tree_name: StringProperty(name='Line Graph')

    def execute(self, context):
        from . import backend
        tree = bpy.data.node_groups.get(self.tree_name) if self.tree_name else context.scene.lea_line.tree
        tree = tree or backend.ensure_default_tree(context.scene)
        if tree.bl_idname != 'LEALineTree':
            return {'CANCELLED'}
        output = _line_output(tree)
        if output is None:
            output = tree.nodes.new("LEAOutputNode")
        with suspend_sync():
            from .node_workflow import available_socket, create_set
            line_set = create_set(context, tree, output, available_socket(output))
        if context.scene.lea_line.tree == tree:
            backend.sync_scene(context.scene, tree)
        else:
            _sync_if_enabled(tree, context)
        return {"FINISHED"}


class LEA_OT_render(bpy.types.Operator):
    bl_idname = "lea_line.render"
    bl_label = "Render with Lines"
    bl_description = "Apply line settings and render the current scene"

    def execute(self, context):
        from . import backend
        if context.scene.camera is None:
            self.report({"ERROR"}, "Set a scene camera before rendering")
            return {"CANCELLED"}
        if context.scene.lea_line.tree is None:
            self.report({"ERROR"}, "Create a Line Graph before rendering")
            return {"CANCELLED"}
        try:
            warnings = backend.sync_scene(context.scene)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if warnings:
            self.report({"WARNING"}, "; ".join(warnings))
        if context.scene.lea_line.fast_line_art:
            from . import fast_render
            try:
                result = fast_render.render(context.scene, write_still=False)
            except Exception as exc:
                self.report({'ERROR'}, str(exc))
                return {'CANCELLED'}
            label = "Freestyle fallback" if result.get('mode') == 'FREESTYLE_FALLBACK' else "Fast Line Art"
            message = f"{label}: {result['seconds']:.3f} s"
            if result.get('warnings'):
                message += '; ' + '; '.join(result['warnings'])
            context.scene.lea_line.last_render_seconds = result['seconds']
            context.scene.lea_line.last_message = message
            self.report({'INFO'}, message)
        elif context.scene.lea_line.reuse_line_pass:
            from . import render_cache
            try:
                result = render_cache.render(context.scene)
            except Exception as exc:
                self.report({'ERROR'}, str(exc))
                return {'CANCELLED'}
            message = f"Line cache: {result['cache']}; {result['seconds']:.3f} s"
            if result.get('reason'):
                message += '; ' + result['reason']
            context.scene.lea_line.last_message = message
            self.report({'INFO'}, message)
        else:
            bpy.ops.render.render("INVOKE_DEFAULT")
        return {"FINISHED"}


class LEA_OT_refresh_preview(bpy.types.Operator):
    bl_idname = "lea_line.refresh_preview"
    bl_label = "Refresh Live Preview"
    bl_description = "Refresh camera-based Line Art preview without changing the preview enable setting"

    def execute(self, context):
        settings = context.scene.lea_line
        if not settings.preview_enabled:
            self.report({"INFO"}, "Enable Live Preview before refreshing")
            return {"CANCELLED"}
        try:
            from . import viewport
            from . import texture_maps
            if settings.tree:
                images={n.image for brush in settings.tree.nodes if brush.bl_idname=='LEABrushNode'
                        for n in texture_maps.maps(brush).values() if n.source_type=='IMAGE' and n.image}
                for image in images:image.update_tag()
            with suspend_sync():
                warnings = viewport.sync_preview(context.scene)
            from . import object_uv
            object_uv.request_prepare(context.scene)
            settings.last_message = "\n".join(warnings) if warnings else "Camera-based live preview synchronized"
        except Exception as exc:
            settings.last_message = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"WARNING"} if warnings else {"INFO"}, "; ".join(warnings) if warnings else "Camera-based live preview synchronized")
        return {"FINISHED"}


class LEA_PT_scene(bpy.types.Panel):
    bl_label = "rea pencil for blender"
    bl_idname = "LEA_PT_scene"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "REA Pencil Line"

    def draw(self, context):
        from .workflow_ui import draw_start
        draw_start(self.layout, context)


class LEA_MT_nodes(bpy.types.Menu):
    bl_idname = "LEA_MT_nodes"
    bl_label = "rea pencil for blender Nodes"

    def draw(self, context):
        for node_type, label in (("LEABrushNode", "Brush Settings"), ("LEABrushDetailNode", "Brush Detail"), ('LEATextureMapNode','Texture Map'), ("LEADistanceReductionNode", "Distance Reduction"), ("LEALineSetNode", "Line Set"), ("LEAOutputNode", "Line")):
            operator = self.layout.operator("node.add_node", text=label)
            operator.type = node_type
            operator.use_transform = True


def _draw_connected_sets(layout, tree, output, nodes, selected):
    layout.label(text='Line Sets')
    row = layout.row()
    if output:
        row.template_list('LEA_UL_line_sets', tree.name, output, 'inputs', output, 'line_set_index', rows=4)
    else:
        row.label(text='No Line Output')
    arrows = row.column(align=True)
    for direction, icon in (('UP', 'TRIA_UP'), ('DOWN', 'TRIA_DOWN')):
        sub = arrows.column(align=True)
        index = nodes.index(selected) if selected in nodes else -1
        sub.enabled = index > 0 if direction == 'UP' else 0 <= index < len(nodes) - 1
        if selected:
            _target_operator(sub, 'lea_line.move_line_set', selected, '', icon).direction = direction
    buttons = layout.row(align=True)
    add = buttons.row(align=True)
    add.enabled = tree.is_editable
    add.operator('lea_line.add_line_set', text='Add', icon='ADD').tree_name = tree.name
    if selected:
        copy = buttons.row(align=True)
        copy.enabled = selected in nodes and bool(output) and tree.is_editable
        _target_operator(copy, 'lea_line.duplicate_line_set', selected, '', 'DUPLICATE')
        unlink = buttons.row(align=True)
        unlink.enabled = selected in nodes
        _target_operator(unlink, 'lea_line.disconnect_line_set', selected, '', 'UNLINKED')


class LEA_PT_node_settings(bpy.types.Panel):
    bl_label = "rea pencil for blender"
    bl_idname = "LEA_PT_node_settings"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "REA Pencil Line"

    @classmethod
    def poll(cls, context):
        tree = context.space_data.edit_tree
        return context.space_data.tree_type == "LEALineTree" and (tree is None or tree.ui_layout == 'COMPACT')

    def draw(self, context):
        layout = self.layout
        tree = context.space_data.edit_tree
        if tree is None:
            layout.operator("lea_line.new_graph", icon="ADD")
            return
        line_set, brush, detail, section = _panel_context(tree)
        tabs = layout.row(align=True)
        for value, label in (('LINE_LIST', 'Line List'), ('BRUSH', 'Brush Settings'), ('DETAIL', 'Brush Detail')):
            tab = tabs.row(align=True)
            tab.enabled = value == 'LINE_LIST' or brush is not None or detail is not None
            op = tab.operator('lea_line.panel_section', text=label, depress=section == value)
            op.tree_name, op.section = tree.name, value
        active = tree.nodes.active
        if active and active.bl_idname == 'LEADistanceReductionNode' and active.name != tree.panel_anchor:
            _, body = _sidebar_section(layout, 'selected_reduction', 'Reduction')
            if body is not None:
                body.prop(active, 'name', text='Name')
                active.draw_controls(context, body)
            _draw_runtime_panel(context, layout, tree)
            return
        if section == 'BRUSH':
            if brush:
                _, body = _sidebar_section(layout, 'brush_settings', 'Brush')
                if body is not None:
                    body.prop(brush, 'name', text='Name')
                    brush.draw_controls(context, body, include_reduction=False)
                _, reduction_body = _sidebar_section(layout, 'brush_reduction', 'Reduction', default_closed=True)
                if reduction_body is not None:
                    _draw_reduction_controls(context, reduction_body, brush)
            else:
                layout.label(text='Select or connect a Brush', icon='INFO')
            _draw_runtime_panel(context, layout, tree)
            return
        if section == 'DETAIL':
            if detail:
                layout.prop(detail, 'name', text='Detail')
                detail.draw_controls(context, layout, sidebar=True)
            elif brush:
                layout.label(text='Inline stroke settings are active', icon='INFO')
                _target_operator(layout, 'lea_line.detail_connection', brush, 'Create Brush Detail', 'ADD').action = 'CREATE'
            else:
                layout.label(text='Select a Brush Detail node', icon='INFO')
            _draw_runtime_panel(context, layout, tree)
            return
        try:
            output = _line_output(tree)
            line_sets = _connected_line_sets(output)
        except ValueError as exc:
            layout.label(text=str(exc), icon='ERROR')
            return
        _, line_list_body = _sidebar_section(layout, 'line_list', 'Line List')
        if line_list_body is not None:
            for candidate in (node for node in tree.nodes if node.bl_idname == 'LEAOutputNode'):
                row = line_list_body.row(align=True)
                row.prop(candidate, 'enabled', text='')
                row.prop(candidate, 'name', text='', icon='NODE')
            if output is None:
                line_list_body.label(text='Add a Line Set to create an output')
        _, line_body = _sidebar_section(layout, 'line', 'Line', output, 'enabled' if output else None)
        if line_body is not None:
            wide = _panel_width(context) >= 540 and line_set is not None
            lists = line_body.row() if wide else line_body.column()
            line_column = lists.column() if wide else lists
            _draw_connected_sets(line_column, tree, output, line_sets, line_set)
            if wide:
                _draw_target_list(lists.column(), line_set)
                _draw_target_list(lists.column(), line_set, material=True)
                line_body.prop(line_set, 'source_collection', text='Collection')
                if line_set.materials:
                    line_body.label(text='Objects OR material faces', icon='MATERIAL')
                    line_body.label(text='Material targets: final render only', icon='INFO')
            elif line_set:
                _draw_targets(line_body, line_set, context)
            line_body.separator()
            _draw_output_settings(context, line_body, tree)
        if line_set:
            _draw_line_set_settings(context, layout, line_set, include_targets=False, sidebar=True)
        else:
            layout.label(text='Add a Line Set to edit line settings', icon='INFO')
        drafts = [node for node in tree.nodes if node.bl_idname == 'LEALineSetNode' and node not in line_sets]
        if drafts:
            _, draft_body = _sidebar_section(layout, 'drafts', 'Disconnected Drafts', default_closed=True)
            if draft_body is not None:
                for node in drafts:
                    _target_operator(draft_body, 'lea_line.select_line_set', node, node.name, 'NODE')
        _draw_runtime_panel(context, layout, tree)


class LEA_PT_feature_coverage(bpy.types.Panel):
    """Summarize implemented groups and current native renderer limitations.

    Missing original features are information, never inert property controls.
    """
    bl_label = 'Feature Coverage'
    bl_idname = 'LEA_PT_feature_coverage'
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'REA Pencil Line'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1000

    @classmethod
    def poll(cls, context):
        return context.space_data.tree_type == 'LEALineTree'

    def draw(self, context):
        layout = self.layout
        layout.label(text='Available', icon='CHECKMARK')
        layout.label(text='Graph, targets, priority, shared brushes')
        layout.label(text='Color, width, opacity, visible/hidden edges')
        layout.label(text='Editable size / alpha curves')
        layout.separator()
        layout.label(text='Partial / Native Renderer Differences', icon='INFO')
        layout.label(text='Dashes, taper, distance endpoints, edge AA')
        layout.label(text='Stroke offsets, extension, sine distortion')
        layout.label(text='Intersections: Line Art only')
        layout.label(text='One crease angle per view layer')
        layout.label(text='Material face targets: final render only')
        layout.separator()
        layout.label(text='Not Yet Implemented', icon='DOT')
        layout.label(text='Texture maps and stamp brushes')
        layout.label(text='Wireframe and Line Functions')
        layout.label(text='Reference objects, weld / merge groups')
        layout.label(text='Original renderer parity is not complete')


def _draw_node_add(self, context):
    if context.space_data.tree_type == "LEALineTree":
        self.layout.menu("LEA_MT_nodes", icon="GREASEPENCIL")


def _draw_node_header(self, context):
    if context.space_data.tree_type == "LEALineTree" and context.scene:
        if not context.scene.lea_line.auto_sync:
            self.layout.operator("lea_line.apply", text="", icon="FILE_REFRESH")


CLASSES = (
    LEALineSettings, LEALineTree, LEALineSocket,
    LEAObjectTarget, LEAMaterialTarget, LEA_UL_object_targets, LEA_UL_material_targets, LEA_UL_line_sets,
    LEAOutputNode, LEALineSetNode, LEABrushNode, LEABrushDetailNode, LEADistanceReductionNode,
    LEA_OT_add_selected_objects, LEA_OT_remove_object_target,
    LEA_OT_add_selected_materials, LEA_OT_remove_material_target,
    LEA_OT_select_line_set, LEA_OT_select_brush, LEA_OT_duplicate_line_set,
    LEA_OT_panel_section, LEA_OT_brush_connection, LEA_OT_detail_connection, LEA_OT_reduction_connection,
    independent_reduction.LEA_OT_channel_reduction,
    LEA_OT_disconnect_line_set, LEA_OT_move_line_set,
    LEA_OT_new_graph, LEA_OT_apply, LEA_OT_add_line_set, LEA_OT_render,
    LEA_OT_refresh_preview,
    LEA_PT_scene, LEA_MT_nodes, LEA_PT_node_settings, LEA_PT_feature_coverage,
)


def register():
    from . import reference_panels, workflow_ui, target_dialog, node_workflow, texture_maps
    registered = []
    menu = header = False
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
        bpy.types.Scene.lea_line = PointerProperty(type=LEALineSettings)
        bpy.types.NODE_MT_add.append(_draw_node_add)
        menu = True
        bpy.types.NODE_HT_header.append(_draw_node_header)
        header = True
        reference_panels.register()
        target_dialog.register()
        workflow_ui.register()
        node_workflow.register()
        texture_maps.register()
    except Exception:
        texture_maps.unregister()
        node_workflow.unregister()
        workflow_ui.unregister()
        target_dialog.unregister()
        for cls in reversed((*reference_panels.CLASSES, *reference_panels.DETAIL_CLASSES)):
            if cls.is_registered:
                bpy.utils.unregister_class(cls)
        reference_panels.DETAIL_CLASSES.clear()
        if header:
            bpy.types.NODE_HT_header.remove(_draw_node_header)
        if menu:
            bpy.types.NODE_MT_add.remove(_draw_node_add)
        if registered and hasattr(bpy.types.Scene, 'lea_line'):
            del bpy.types.Scene.lea_line
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise


def unregister():
    from . import reference_panels, workflow_ui, target_dialog, node_workflow, texture_maps
    texture_maps.unregister()
    node_workflow.unregister()
    workflow_ui.unregister()
    target_dialog.unregister()
    reference_panels.unregister()
    bpy.types.NODE_HT_header.remove(_draw_node_header)
    bpy.types.NODE_MT_add.remove(_draw_node_add)
    del bpy.types.Scene.lea_line
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
