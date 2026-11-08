"""Single-viewport helper camera. The scene's render camera is never changed."""
import functools
import time
import bpy
from bpy.props import BoolProperty
from bpy.app.handlers import persistent
TAG='rea_free_view_camera'
_entries={}
_active=None
SETTLE_SECONDS=.2


def _view_signature(area,region):
    view=area.spaces.active.region_3d
    return (tuple(tuple(row) for row in view.view_matrix),
            tuple(tuple(row) for row in view.window_matrix),region.width,region.height,
            area.spaces.active.clip_start,area.spaces.active.clip_end)


def _hide_during_navigation(scene,entry):
    """Hide only owned generated strokes, preserving per-view-layer visibility."""
    from . import viewport
    hidden=entry.setdefault('navigation_hidden',[])
    known=set();alive=[]
    for obj,layer in hidden:
        try:known.add((obj.as_pointer(),layer.as_pointer()))
        except ReferenceError:continue
        alive.append((obj,layer))
    entry['navigation_hidden']=hidden=alive
    owner=scene.get(viewport.OWNER_SCENE_KEY)
    if not owner:return
    for obj in scene.objects:
        if not viewport._owned(obj,owner):continue
        for layer in scene.view_layers:
            if (obj.as_pointer(),layer.as_pointer()) not in known and not obj.hide_get(view_layer=layer):
                obj.hide_set(True,view_layer=layer);hidden.append((obj,layer))


def _restore_navigation(entry):
    for obj,layer in entry.pop('navigation_hidden',[]):
        try:obj.hide_set(False,view_layer=layer)
        except (ReferenceError,RuntimeError):pass
    entry.pop('pending_signature',None)
    entry.pop('pending_since',None)

def camera(scene):
    entry=_entries.get(scene.as_pointer())
    return entry['camera'] if _active==scene.as_pointer() and entry else scene.camera

def dimensions(scene):
    entry=_entries.get(scene.as_pointer())
    return entry['dimensions'] if _active==scene.as_pointer() and entry else None

def context(function):
    @functools.wraps(function)
    def run(scene,*args,**kwargs):
        global _active
        previous=_active
        material_render=kwargs.get('material_render',args[0] if args else False)
        if not material_render and scene.as_pointer() in _entries:_active=scene.as_pointer()
        try:return function(scene,*args,**kwargs)
        finally:_active=previous
    return run

def stop(scene):
    entry=_entries.pop(scene.as_pointer(),None)
    if entry:
        _restore_navigation(entry)
        cam=entry['camera'];data=cam.data
        if cam.get(TAG):bpy.data.objects.remove(cam,do_unlink=True)
        if data.users==0 and data.get(TAG):bpy.data.cameras.remove(data)

def update(scene,area,region):
    entry=_entries[scene.as_pointer()];space=area.spaces.active;view=space.region_3d
    if view.view_perspective=='CAMERA':stop(scene);return True
    matrix=view.view_matrix.inverted();projection=view.window_matrix
    flat=_view_signature(area,region)
    if entry.get('signature')==flat:return False
    cam=entry['camera'];cam.matrix_world=matrix
    cam.data.type='ORTHO' if view.view_perspective=='ORTHO' else 'PERSP'
    cam.data.sensor_fit='VERTICAL';cam.data.sensor_height=36
    cam.data.lens=max(1.,min(5000.,18*projection[1][1]))
    cam.data.ortho_scale=2/max(1e-10,projection[1][1])
    cam.data.clip_start=space.clip_start;cam.data.clip_end=space.clip_end
    entry['dimensions']=(max(1,region.width),max(1,region.height));entry['signature']=flat
    return True

def tick():
    if bpy.app.is_job_running('RENDER'):return .1
    existing={s.as_pointer():s for s in bpy.data.scenes}
    for key,entry in list(_entries.items()):
        scene=existing.get(key)
        if scene is None:
            _entries.pop(key,None)
            _restore_navigation(entry)
            try:
                cam=entry['camera'];data=cam.data
                if cam.get(TAG):bpy.data.objects.remove(cam,do_unlink=True)
                if data.users==0 and data.get(TAG):bpy.data.cameras.remove(data)
            except ReferenceError:pass
            continue
        area=next((a for w in bpy.context.window_manager.windows for a in w.screen.areas
                   if a.as_pointer()==entry['area'] and a.type=='VIEW_3D'),None)
        if area is None or not scene.lea_line.preview_enabled:
            stop(scene);continue
        region=next((r for r in area.regions if r.type=='WINDOW'),None)
        if region and area.spaces.active.region_3d.view_perspective=='CAMERA':
            stop(scene)
            from . import viewport
            viewport.sync_preview(scene)
            continue
        if not region:continue
        signature=_view_signature(area,region)
        if getattr(scene.lea_line,'follow_refresh_mode','SETTLED')=='SETTLED':
            if signature!=entry.get('signature'):
                now=time.monotonic()
                if signature!=entry.get('pending_signature'):
                    entry['pending_signature']=signature;entry['pending_since']=now
                _hide_during_navigation(scene,entry)
                if now-entry['pending_since']<SETTLE_SECONDS:continue
            elif 'pending_signature' in entry:
                _restore_navigation(entry)
        if update(scene,area,region):
            from . import viewport
            try:viewport.sync_preview(scene)
            except Exception as exc:scene.lea_line.last_message='Free View: '+str(exc);stop(scene)
            finally:_restore_navigation(entry)
        elif 'pending_signature' in entry:
            _restore_navigation(entry)
    return 1/30 if _entries else None

class LEA_OT_follow_view(bpy.types.Operator):
    bl_idname='lea_line.follow_view';bl_label='Follow Viewport';bl_options={'REGISTER'}
    enabled:BoolProperty(default=True)
    def execute(self,context):
        scene=context.scene
        if not self.enabled:
            stop(scene)
            from . import viewport
            viewport.sync_preview(scene);return {'FINISHED'}
        if bpy.app.is_job_running('RENDER') or not scene.lea_line.tree:return {'CANCELLED'}
        areas=[context.area] if context.area and context.area.type=='VIEW_3D' else [a for a in context.screen.areas if a.type=='VIEW_3D']
        if len(areas)!=1:
            self.report({'ERROR'},'Choose one View3D to follow');return {'CANCELLED'}
        area=areas[0];region=next(r for r in area.regions if r.type=='WINDOW')
        if area.spaces.active.region_3d.view_perspective=='CAMERA':
            self.report({'ERROR'},'Leave camera view with Numpad 0, then follow the viewport');return {'CANCELLED'}
        from . import backend,texture_maps,material_scope,routing
        for node,common in backend._specifications(scene.lea_line.tree):
            if material_scope.active(node) or node.use_material_functions:
                self.report({'ERROR'},'Free View material-face evaluation is not yet verified');return {'CANCELLED'}
            for visibility in ('VISIBLE','HIDDEN'):
                for job in routing.jobs(node,common,visibility):
                    if texture_maps.maps(job['brush']):
                        self.report({'ERROR'},'Free View texture mapping is not yet verified');return {'CANCELLED'}
        stop(scene)
        data=bpy.data.cameras.new('.rea free view camera');data[TAG]=True
        cam=bpy.data.objects.new('.rea free view camera',data);cam[TAG]=True;cam['rea_pencil_preview']=True
        scene.collection.objects.link(cam);cam.hide_render=True;cam.hide_viewport=True
        _entries[scene.as_pointer()]={'camera':cam,'area':area.as_pointer(),'dimensions':(region.width,region.height)}
        scene.lea_line.preview_enabled=True;update(scene,area,region)
        from . import viewport
        viewport.sync_preview(scene)
        if not bpy.app.timers.is_registered(tick):bpy.app.timers.register(tick,first_interval=1/30)
        return {'FINISHED'}

@persistent
def reset(*_):
    global _active
    for entry in list(_entries.values()):_restore_navigation(entry)
    _entries.clear();_active=None
    if not hasattr(bpy.data,'objects'):return
    for obj in list(bpy.data.objects):
        if obj.get(TAG):
            data=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
            if data.users==0 and data.get(TAG):bpy.data.cameras.remove(data)

def register():
    bpy.utils.register_class(LEA_OT_follow_view)
    for handlers in (bpy.app.handlers.load_post,bpy.app.handlers.undo_post,bpy.app.handlers.redo_post):
        if reset not in handlers:handlers.append(reset)

def unregister():
    if bpy.app.timers.is_registered(tick):bpy.app.timers.unregister(tick)
    reset()
    for handlers in (bpy.app.handlers.load_post,bpy.app.handlers.undo_post,bpy.app.handlers.redo_post):
        if reset in handlers:handlers.remove(reset)
    if LEA_OT_follow_view.is_registered:bpy.utils.unregister_class(LEA_OT_follow_view)
