"""Render-based regression checks for the independent engine and UI path."""
import bpy, sys, math, json
from pathlib import Path
import numpy as np
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view

ROOT=Path(__import__('os').environ.get('REA_GPU_TEST_SOURCE', str(Path(__file__).resolve().parents[1])));OUT=Path(__import__('os').environ.get('REA_GPU_TEST_OUT', str(ROOT/'artifacts/independent_gpu_regression')));OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT))
import lea_line;lea_line.register()
from lea_line import backend,ui,gpu_render,gpu_preview
bpy.context.preferences.use_preferences_save=False
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
s=bpy.context.scene;s.render.resolution_x=192;s.render.resolution_y=128;s.render.resolution_percentage=100;s.render.image_settings.file_format='PNG';s.render.image_settings.color_mode='RGBA'
bpy.ops.mesh.primitive_cube_add();cube=bpy.context.object
bpy.ops.object.grease_pencil_add(type='STROKE',location=(0,0,2));gp=bpy.context.object
gp_hidden=gp.copy();gp_hidden.data=gp.data.copy();s.collection.objects.link(gp_hidden);gp_hidden.hide_render=True
bpy.ops.object.camera_add(location=(0,0,6));s.camera=bpy.context.object;s.camera.data.type='ORTHO';s.camera.data.ortho_scale=6
with ui.suspend_sync():s.lea_line.auto_sync=False
tree=backend.ensure_default_tree(s,create_detail=True);n,b=backend._specifications(tree)[0];brush=getattr(b,'source',b);detail=ui._linked_node(b,'Brush Detail')
with ui.suspend_sync():
 n.target_mode='LISTED';n.objects.clear();n.objects.add().object=cube
 n.outline=n.border=n.crease=n.visible=True;n.hidden=n.material_boundary=n.edge_mark=False
 n.crease_angle=math.radians(134);brush.thickness=2;brush.color=(.5,.2,.1);brush.alpha=1;detail.brush_type='SIMPLE';detail.dashed=False
checks={};index=0
def fail_freestyle(*args,**kwargs):raise AssertionError('Freestyle scene sync was invoked')
backend.sync_scene=fail_freestyle
with ui.suspend_sync():s.lea_line.auto_sync=True
s.render.use_freestyle=True;s.render.use_compositing=True;s.render.use_sequencer=True
def render():
 global index
 s.render.filepath=str(OUT/f'case_{index}.png');index+=1
 flags=(s.render.engine,s.render.use_freestyle,s.render.use_compositing,s.render.use_sequencer,s.lea_line.auto_sync)
 report=gpu_render.render_lines(s,write_still=True)
 assert report['status']=='ok',report
 assert flags==(s.render.engine,s.render.use_freestyle,s.render.use_compositing,s.render.use_sequencer,s.lea_line.auto_sync)
 assert gp.hide_render is False and gp_hidden.hide_render is True
 assert not gpu_render._grease_pencil_scopes
 image=bpy.data.images.load(s.render.filepath,check_existing=False);rgba=np.array(image.pixels[:]).reshape(image.size[1],image.size[0],4);bpy.data.images.remove(image)
 return rgba,report
a,base=render();assert a[:,:,3].sum()>100
checks['render_graph_and_settings_restored']=True
# Native GP is present, but cannot contribute to REA-only output.
gp.hide_render=True
s.render.filepath=str(OUT/'GP_manually_excluded.png');gpu_render.render_lines(s,write_still=True)
im=bpy.data.images.load(s.render.filepath,check_existing=False);no_gp=np.array(im.pixels[:]).reshape(im.size[1],im.size[0],4);bpy.data.images.remove(im);gp.hide_render=False
assert np.array_equal(a,no_gp)
checks['native_GP_excluded_and_all_object_flags_restored']=True
old_engine=s.render.engine;s.render.engine='REA_PENCIL_GPU';s.render.filepath=str(OUT/'direct.png')
bpy.ops.render.render(write_still=True)
assert gp.hide_render is False and gp_hidden.hide_render is True and not gpu_render._grease_pencil_scopes
im=bpy.data.images.load(s.render.filepath,check_existing=False);direct=np.array(im.pixels[:]).reshape(im.size[1],im.size[0],4);bpy.data.images.remove(im)
assert np.array_equal(a,direct)
s.render.engine=old_engine
checks['direct_engine_excludes_GP_without_Freestyle_sync']=True
bulk=gpu_render.publish
gpu_render.publish=lambda layer,rgba:setattr(layer.passes['Combined'],'rect',rgba.reshape(-1,4))
reference,_=render();gpu_render.publish=bulk
assert np.array_equal(a,reference);checks['bulk_transfer_matches_reference_exactly']=True
cube.hide_set(True);bpy.context.view_layer.update()
view=gpu_preview.snapshot(s,bpy.context.evaluated_depsgraph_get());assert view['edges']==0
hidden_view,_=render();assert np.array_equal(a,hidden_view);checks['render_visibility_independent_of_viewport_hide']=True
cube.hide_set(False);cube.hide_render=True
hidden_render,_=render();assert hidden_render[:,:,3].sum()==0;checks['hide_render_respected']=True
cube.hide_render=False
modifier=cube.modifiers.new('Render-only subdivision','SUBSURF');modifier.levels=0;modifier.render_levels=2
subdiv,sub=render();assert sub['depth_triangles']>base['depth_triangles'];checks['render_subdivision_respected']=True
cube.modifiers.remove(modifier)
with ui.suspend_sync():brush.alpha=.25
alpha,_=render();assert 0<alpha[:,:,3].sum()<a[:,:,3].sum()*.5;checks['fractional_alpha']=True
with ui.suspend_sync():brush.alpha=1
for w,h in ((128,256),(512,128)):
 s.render.resolution_x=w;s.render.resolution_y=h
 rgba,_=render();assert rgba.shape==(h,w,4)
 y,x=np.where(rgba[:,:,3]>.05)
 corners=[world_to_camera_view(s,s.camera,Vector((vx,vy,1))) for vx in (-1,1) for vy in (-1,1)]
 expected=(max(0,min(p.x for p in corners)*w),min(w,max(p.x for p in corners)*w),max(0,min(p.y for p in corners)*h),min(h,max(p.y for p in corners)*h))
 actual=(x.min(),x.max()+1,y.min(),y.max()+1)
 assert max(abs(v-e) for v,e in zip(actual,expected))<3,(actual,expected)
checks['portrait_landscape_projection_alignment']=True
with ui.suspend_sync():detail.caps='BUTT'
try:render();raise AssertionError('Unsupported cap accepted')
except RuntimeError:pass
assert s.render.engine=='BLENDER_EEVEE_NEXT' and s.lea_line.auto_sync
assert gp.hide_render is False and not gpu_render._grease_pencil_scopes
checks['GP_restored_after_render_failure']=True
with ui.suspend_sync():detail.caps='ROUND'
old_engine=s.render.engine;s.render.engine='REA_PENCIL_GPU'
gpu_render._render_init(s);assert gp.hide_render is True
for callback in bpy.app.handlers.render_cancel:
 if callback==gpu_render._render_end:callback(s)
assert gp.hide_render is False and gp_hidden.hide_render is True and not gpu_render._grease_pencil_scopes
s.render.engine=old_engine
checks['GP_restored_by_registered_cancel_handler']=True
checks['unsupported_style_errors_without_freestyle_and_restores_settings']=True
with ui.suspend_sync():detail.caps='ROUND'
# Verify the actual line-only UI operator uses the new engine.
assert 'FINISHED' in bpy.ops.lea_line.render_lines()
assert gpu_render.LAST_RESULT['status']=='ok'
checks['UI_operator_uses_independent_engine']=True
s.render.engine='REA_PENCIL_GPU';gpu_render._render_init(s);assert gp.hide_render is True
lea_line.unregister();assert gp.hide_render is False
checks['GP_restored_on_unregistration']=True
# A rejected RNA write must not leave an earlier object's render flag changed.
class FakeGP:
 type='GREASEPENCIL'
 def __init__(self,reject=False):self.value=False;self.reject=reject
 @property
 def hide_render(self):return self.value
 @hide_render.setter
 def hide_render(self,value):
  if value and self.reject:raise RuntimeError('simulated read-only linked object')
  self.value=value
one,two=FakeGP(),FakeGP(True)
try:gpu_render._exclude_grease_pencils(type('Scene',(),{'objects':[one,two]})());raise AssertionError('rejected write accepted')
except RuntimeError:pass
assert one.hide_render is False and two.hide_render is False
checks['partial_exclusion_failure_restores_prior_flags']=True

lea_line.register();lea_line.unregister()
checks['registration_roundtrip']=True
(OUT/'report.json').write_text(json.dumps({'status':'ok','checks':checks,'renders':index},indent=2),encoding='utf-8')
print('INDEPENDENT_GPU_REGRESSION_OK',json.dumps(checks),flush=True)
