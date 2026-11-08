"""Native Blender entry point and working Line List actions; no render app."""
import bpy
from bpy.props import EnumProperty, StringProperty
from . import ui, backend


def _camera_view(context):
    """Prefer the followed view, then the current/adjacent View3D."""
    from . import free_view
    followed = free_view._entries.get(context.scene.as_pointer(), {}).get('area')
    views = [(window, area) for window in context.window_manager.windows
             if window.scene == context.scene for area in window.screen.areas if area.type == 'VIEW_3D']
    return next((item for item in views if item[1].as_pointer() == followed), None) or next(
        (item for item in views if item[1] == context.area), None) or next(
        (item for item in views if item[0] == context.window), None) or (views[0] if views else None)


class LEA_OT_create_camera(bpy.types.Operator):
    bl_idname = 'lea_line.create_camera'
    bl_label = '카메라 자동 생성'
    bl_description = '현재 3D 뷰 시점으로 장면 카메라를 만듭니다. 3D 뷰가 없으면 장면의 메시를 바라봅니다'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.scene and context.scene.is_editable and not bpy.app.is_job_running('RENDER'))

    def execute(self, context):
        scene = context.scene
        if scene.camera is not None:
            self.report({'INFO'}, '기존 장면 카메라를 유지합니다')
            return {'FINISHED'}
        from mathutils import Vector
        view = _camera_view(context)
        data = bpy.data.cameras.new('REA Camera')
        camera = None
        try:
            camera = bpy.data.objects.new('REA Camera', data)
            scene.collection.objects.link(camera)
            if view:
                space = view[1].spaces.active
                region_view = space.region_3d
                projection = region_view.window_matrix
                camera.matrix_world = region_view.view_matrix.inverted()
                data.type = 'PERSP' if region_view.is_perspective else 'ORTHO'
                data.sensor_fit = 'VERTICAL'
                data.sensor_height = 36
                data.lens = max(1., min(5000., 18 * projection[1][1]))
                data.ortho_scale = 2 / max(1e-10, projection[1][1])
                data.clip_start, data.clip_end = space.clip_start, space.clip_end
            else:
                points = [obj.matrix_world @ Vector(corner) for obj in scene.objects
                          if obj.type == 'MESH' and not obj.hide_render and not obj.get('rea_pencil_preview')
                          for corner in obj.bound_box]
                low = Vector(tuple(min(p[i] for p in points) for i in range(3))) if points else Vector((-1,-1,-1))
                high = Vector(tuple(max(p[i] for p in points) for i in range(3))) if points else Vector((1,1,1))
                center = (low + high) * .5
                radius = max(1., (high - low).length * .5)
                camera.location = center + Vector((1., -1., .75)).normalized() * max(8., radius * 4)
                camera.rotation_euler = (center - camera.location).to_track_quat('-Z', 'Y').to_euler()
                data.clip_end = max(1000., radius * 20)
            scene.camera = camera
        except Exception as exc:
            if scene.camera == camera:
                scene.camera = None
            if camera is not None:
                bpy.data.objects.remove(camera, do_unlink=True)
            if data.users == 0:
                bpy.data.cameras.remove(data)
            self.report({'ERROR'}, '카메라 생성 실패: ' + str(exc))
            return {'CANCELLED'}
        # Object selection, viewport orientation and render resolution are preserved.
        ui._sync_if_enabled(scene.lea_line, context)
        self.report({'INFO'}, '미리보기·렌더용 장면 카메라를 만들었습니다')
        return {'FINISHED'}


def tree_from(context, name=''):
    if name:
        tree = bpy.data.node_groups.get(name)
    else:
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if tree is None or tree.bl_idname != 'LEALineTree':
            tree = context.scene.lea_line.tree
    return tree if tree and tree.bl_idname == 'LEALineTree' else None


def _sync(context, tree):
    if context.scene.lea_line.tree == tree:
        backend.sync_scene(context.scene, tree)


class LEA_OT_line_list_action(bpy.types.Operator):
    bl_idname = 'lea_line.line_list_action'
    bl_label = 'Edit Line List'
    bl_description = 'Add, remove or reorder the selected Line; removing a Line keeps its Line Sets as drafts'
    bl_options = {'REGISTER', 'UNDO'}
    tree_name: StringProperty()
    action: EnumProperty(items=[('ADD', 'Add', ''), ('REMOVE', 'Remove', ''),
                                ('UP', 'Up', ''), ('DOWN', 'Down', '')])

    def execute(self, context):
        tree = tree_from(context, self.tree_name)
        if tree is None or not tree.is_editable:
            return {'CANCELLED'}
        lines = ui._line_outputs(tree)
        selected = ui._line_output(tree)
        with ui.suspend_sync():
            if self.action == 'ADD':
                node = tree.nodes.new('LEAOutputNode')
                node.name = 'Line'
                node.render_priority = len(lines)
                node.location = (420, 80 - len(lines) * 500)
                tree.line_index = list(tree.nodes).index(node)
            elif self.action == 'REMOVE':
                if selected is None:
                    return {'CANCELLED'}
                index = lines.index(selected)
                lines.remove(selected)
                tree.nodes.remove(selected)
                if lines:
                    tree.line_index = list(tree.nodes).index(lines[min(index, len(lines) - 1)])
                else:
                    tree.nodes.active = None
                    tree.panel_line_set = tree.panel_brush = tree.panel_anchor = ''
                    tree.panel_section = 'LINE_LIST'
            else:
                if selected not in lines:
                    return {'CANCELLED'}
                index = lines.index(selected)
                other = index + (-1 if self.action == 'UP' else 1)
                if not 0 <= other < len(lines):
                    return {'CANCELLED'}
                lines[index], lines[other] = lines[other], lines[index]
                for priority, line in enumerate(lines):
                    line.render_priority = priority
                tree.line_index = list(tree.nodes).index(selected)
        _sync(context, tree)
        return {'FINISHED'}


def _frame_later(window_id, area_id):
    def frame():
        if not hasattr(bpy.types.Scene, 'lea_line'):
            return None
        for window in bpy.context.window_manager.windows:
            if window.as_pointer() != window_id:
                continue
            for area in window.screen.areas:
                if area.as_pointer() != area_id or area.type != 'NODE_EDITOR':
                    continue
                region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                if region:
                    with bpy.context.temp_override(window=window, area=area, region=region):
                        bpy.ops.node.view_all()
                        sidebar = next((r for r in area.regions if r.type == 'UI'), None)
                        if sidebar and sidebar.width > 0:
                            sidebar.active_panel_category = 'REA Pencil Line'
                            bpy.ops.view2d.pan(deltax=-sidebar.width // 2, deltay=0)
                return None
        return None
    bpy.app.timers.register(frame, first_interval=.15)


class LEA_OT_open_line_editor(bpy.types.Operator):
    bl_idname = 'lea_line.open_line_editor'
    bl_label = 'Open Line Editor'
    bl_description = 'Open the Line List beside the viewport; create connected Line, Line Set and Brush settings on first use'

    @classmethod
    def poll(cls, context):
        return bool(context.window and context.area and context.area.type in {'VIEW_3D', 'NODE_EDITOR'})

    def execute(self, context):
        created = context.scene.lea_line.tree is None
        tree = backend.ensure_default_tree(context.scene, create_detail=True)
        if created:
            with ui.suspend_sync():
                for node in tree.nodes:
                    if node.bl_idname == 'LEALineSetNode':node.target_mode = 'LISTED'
        tree.ui_layout = 'ORIGINAL'
        output = ui._line_output(tree)
        if output:
            tree.line_index = list(tree.nodes).index(output)
        warnings = backend.sync_scene(context.scene, tree)
        editor = next((a for a in context.screen.areas if a.type == 'NODE_EDITOR'
                       and a.spaces.active.tree_type == 'LEALineTree'), None)
        if editor is None:
            if context.area.type == 'VIEW_3D':
                before = {a.as_pointer() for a in context.screen.areas}
                result = bpy.ops.screen.area_split(direction='VERTICAL', factor=.55)
                if result != {'FINISHED'}:
                    self.report({'ERROR'}, 'Could not open editor; choose REA Pencil Line from an editor type menu')
                    return {'CANCELLED'}
                editor = next((a for a in context.screen.areas if a.as_pointer() not in before), None)
                if editor is None:
                    return {'CANCELLED'}
            else:
                editor = context.area
            editor.type = 'NODE_EDITOR'
            editor.ui_type = 'LEALineTree'
        editor.spaces.active.tree_type = 'LEALineTree'
        editor.spaces.active.pin = False
        editor.spaces.active.show_region_ui = True
        editor.tag_redraw()
        _frame_later(context.window.as_pointer(), editor.as_pointer())
        if warnings:
            self.report({'WARNING'}, '; '.join(warnings))
        return {'FINISHED'}


def draw_actions(layout, context, tree=None):
    settings = context.scene.lea_line
    from . import gpu_preview
    gpu_live=gpu_preview.active(context.scene)
    row = layout.row(align=True)
    preview=row.row(align=True)
    preview.enabled=bool(settings.tree and (context.scene.camera or settings.preview_compute=='GPU'))
    preview.prop(settings, 'preview_enabled', text='Viewport Preview', toggle=True, icon='HIDE_OFF')
    render=row.row(align=True);render.enabled=bool(settings.tree and context.scene.camera)
    render.operator('lea_line.render', text='Render', icon='RENDER_STILL')
    views=layout.row(align=True)
    if gpu_live or (settings.preview_compute=='GPU' and not settings.preview_enabled):
        views.label(text='GPU · 현재 시점을 자동 추적',icon='VIEW3D')
    else:
        views.operator('lea_line.follow_view',text='Follow Viewport').enabled=True
        views.operator('lea_line.follow_view',text='Scene Camera').enabled=False
    layout.prop(settings,'preview_compute',text='뷰포트 계산')
    if settings.preview_enabled:
        from . import free_view
        entry=free_view._entries.get(context.scene.as_pointer())
        if gpu_live:
            layout.label(text='이동 중 선 유지 · Solid / 재질 미리보기',icon='INFO')
            layout.label(text='곡선 · 점선 · 맵 · 숨은 선 (GPU)',icon='INFO')
            layout.label(text='Native와 시작점·연결부·AA 차이 있음',icon='INFO')
        else:
            layout.label(text='자유 시점 추적 중' if entry else '카메라 기준 미리보기 · Numpad 0', icon='CAMERA_DATA')
        if not gpu_live and settings.preview_compute=='GPU':
            layout.label(text='GPU 미지원 설정 · Native 경로 사용',icon='ERROR')
        if entry and not gpu_live:
            layout.prop(settings,'follow_refresh_mode',text='갱신 방식')
            if settings.follow_refresh_mode=='SETTLED':
                layout.label(text='이동 중 선 숨김 · 멈추면 자동 갱신',icon='INFO')
    if context.scene.camera is None:
        layout.operator('lea_line.create_camera', text='카메라 자동 생성', icon='OUTLINER_OB_CAMERA')
        layout.label(text='렌더용 카메라를 만듭니다' if settings.preview_compute=='GPU' else '미리보기·렌더용 카메라를 만듭니다', icon='INFO')


def draw_start(layout, context):
    from . import updater
    updater.draw(layout, context)
    layout.operator('lea_line.open_line_editor', text='라인 편집기 열기', icon='NODETREE')
    layout.separator()
    layout.label(text='1  Line List에서 Line / Line Set 선택')
    layout.label(text='2  Objects의 Add 창에서 대상 메시 선택')
    layout.label(text='3  Brush의 Color / Size 설정 → F12')
    if context.scene.lea_line.tree:
        layout.separator()
        draw_actions(layout, context)
        if context.area.type == 'VIEW_3D' and context.scene.camera:
            layout.operator('view3d.view_camera', text='카메라 보기 · Numpad 0', icon='CAMERA_DATA')


class LEA_PT_workflow_tools(bpy.types.Panel):
    bl_idname = 'LEA_PT_workflow_tools'
    bl_label = 'Preview & Render'
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'REA Pencil Line'
    bl_order = -10

    @classmethod
    def poll(cls, context):
        return context.space_data.tree_type == 'LEALineTree'

    def draw(self, context):
        tree = tree_from(context)
        if tree is None:
            self.layout.label(text='새 라인을 만들어 시작하세요', icon='INFO')
            self.layout.operator('lea_line.new_graph', text='New · 라인 만들기', icon='ADD')
            return
        draw_actions(self.layout, context, tree)
        self.layout.label(text='Objects에서 대상 선택 · Brush에서 Color / Size')


class LEA_PT_workflow_options(bpy.types.Panel):
    bl_idname = 'LEA_PT_workflow_options'
    bl_label = 'REA Options & Help'
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'REA Pencil Line'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1000

    @classmethod
    def poll(cls, context):
        return context.space_data.tree_type == 'LEALineTree' and tree_from(context) is not None

    def draw(self, context):
        tree = tree_from(context);settings = context.scene.lea_line;layout = self.layout
        layout.prop(tree, 'ui_layout')
        layout.prop(settings, 'auto_sync')
        if not settings.auto_sync:
            layout.operator('lea_line.apply', icon='FILE_REFRESH')
        node = ui._panel_context(tree)[0]
        if node:layout.prop(node, 'target_mode')
        layout.label(text='Add 창에서 대상을 체크하고 OK로 확정합니다')
        layout.label(text='사이드바가 좁으면 왼쪽 경계를 드래그해 넓히세요')
        layout.label(text='회색 항목은 상위 체크를 확인하세요')
        layout.label(text='Color / Size Map 등 일부 항목은 아직 미지원입니다')
        layout.label(text='렌더 앱은 필요하지 않습니다 · Blender F12 사용')
        layout.prop(settings, 'preview_simple_coverage')
        layout.prop(settings, 'preview_endpoint_coverage')
        layout.prop(settings, 'preview_feature_brushes')
        if settings.preview_enabled:
            layout.operator('lea_line.refresh_preview', icon='FILE_REFRESH')
        if settings.last_message:
            for line in settings.last_message.splitlines()[:4]:
                layout.label(text=line, icon='INFO')


CLASSES = (LEA_OT_create_camera, LEA_OT_line_list_action, LEA_OT_open_line_editor, LEA_PT_workflow_tools, LEA_PT_workflow_options)


def register():
    done = []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls);done.append(cls)
    except Exception:
        for cls in reversed(done):bpy.utils.unregister_class(cls)
        raise


def unregister():
    for cls in reversed(CLASSES):
        if cls.is_registered:bpy.utils.unregister_class(cls)
