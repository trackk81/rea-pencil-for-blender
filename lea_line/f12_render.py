"""F12 dispatch through the same composite path as the REA Render button.

The existing prepared-render add-on keymap dispatches here; no duplicate binding
is added. Native/user keymaps are never edited.
Scenes without a REA graph, or with F12 integration disabled, use Blender render.
"""
import time
import bpy

LAST_INVOCATION = {}


def _native_render(scene):
    if getattr(scene, 'lea_line', None) and scene.lea_line.tree:
        from . import object_uv
        object_uv.prepare_scene(scene, apply_styles=True)
    return bpy.ops.render.render('INVOKE_DEFAULT', use_viewport=True)


class LEA_OT_render_f12(bpy.types.Operator):
    bl_idname = 'lea_line.render_f12'
    bl_label = 'Render Image with REA Lines'
    bl_description = 'REA 그래프가 있으면 장면과 REA 선을 합성하고, 없으면 Blender 기본 렌더를 실행합니다'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        from .gpu_render import LINE_ENGINES
        global LAST_INVOCATION
        scene = context.scene
        settings = getattr(scene, 'lea_line', None)
        use_rea = bool(settings and settings.tree and settings.f12_gpu_lines)
        engine = scene.render.engine
        line_only = engine in LINE_ENGINES
        route = 'BLENDER'
        if use_rea:
            route = engine.removeprefix('REA_PENCIL_') if line_only else settings.final_line_backend
        LAST_INVOCATION = {'route': route,
                           'scene': scene.name, 'status': 'running', 'start': time.perf_counter()}
        if bpy.app.is_job_running('RENDER'):
            LAST_INVOCATION['status'] = 'busy'
            self.report({'WARNING'}, '이미 렌더가 진행 중입니다')
            return {'CANCELLED'}
        try:
            if use_rea:
                LAST_INVOCATION['output'] = 'LINES' if line_only else 'COMPOSITE'
                result = (bpy.ops.lea_line.render_lines('EXEC_DEFAULT') if line_only
                          else bpy.ops.lea_line.render('EXEC_DEFAULT'))
            else:
                result = _native_render(scene)
            LAST_INVOCATION.update(status='cancelled' if 'CANCELLED' in result else 'dispatched',
                                   seconds=time.perf_counter()-LAST_INVOCATION['start'])
            return {'CANCELLED'} if 'CANCELLED' in result else {'FINISHED'}
        except Exception as error:
            LAST_INVOCATION.update(status='error', message=str(error))
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


def register():
    bpy.utils.register_class(LEA_OT_render_f12)


def unregister():
    bpy.utils.unregister_class(LEA_OT_render_f12)
