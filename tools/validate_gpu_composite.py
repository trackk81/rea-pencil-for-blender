"""Blender render regression: beauty + GPU lines, with authored graph restoration."""
import bpy,sys,os,json
from pathlib import Path
import numpy as np
ROOT=Path(os.environ.get('REA_GPU_TEST_SOURCE',str(Path(__file__).resolve().parents[1])))
OUT=Path(os.environ.get('REA_GPU_TEST_OUT',str(ROOT/'artifacts/composite_regression')));OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT));import lea_line;lea_line.register()
from lea_line import gpu_composite,gpu_render,backend,ui
bpy.context.preferences.use_preferences_save=False
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
s=bpy.context.scene;s.render.resolution_x=256;s.render.resolution_y=192;s.render.resolution_percentage=100;s.eevee.taa_render_samples=8
s.render.image_settings.file_format='PNG';s.render.image_settings.color_mode='RGBA';s.render.film_transparent=True
s.view_settings.view_transform='Standard';s.render.dither_intensity=0
bpy.ops.mesh.primitive_plane_add(size=2);cube=bpy.context.object
mat=bpy.data.materials.new('Emission surface');mat.diffuse_color=(.1,.3,.7,1);cube.data.materials.append(mat)
bpy.ops.object.grease_pencil_add(type='STROKE');helper=bpy.context.object;helper.modifiers.new('Generated Line Art','LINEART')
bpy.ops.object.grease_pencil_add(type='STROKE',location=(10,0,0));drawing=bpy.context.object
preview=cube.copy();preview.data=cube.data.copy();preview['rea_pencil_preview']=True;s.collection.objects.link(preview);preview.location.z=2
bpy.ops.object.camera_add(location=(0,0,6));s.camera=bpy.context.object;s.camera.data.type='ORTHO';s.camera.data.ortho_scale=6
with ui.suspend_sync():s.lea_line.auto_sync=False
tree=backend.ensure_default_tree(s,create_detail=True);n,b=backend._specifications(tree)[0];brush=getattr(b,'source',b)
with ui.suspend_sync():
 n.target_mode='LISTED';n.objects.clear();n.objects.add().object=cube
 brush.color=(1,0,0);brush.thickness=6.;brush.alpha=.5
 s.lea_line.auto_sync=True
s.use_nodes=True;g=s.node_tree;g.nodes.clear();rl=g.nodes.new('CompositorNodeRLayers');rl.layer=s.view_layers[0].name
grade=g.nodes.new('CompositorNodeBrightContrast');grade.inputs['Bright'].default_value=2
comp=g.nodes.new('CompositorNodeComposite');g.links.new(rl.outputs['Image'],grade.inputs['Image']);g.links.new(grade.outputs['Image'],comp.inputs['Image'])
file=g.nodes.new('CompositorNodeOutputFile');file.base_path=str(OUT/'file_output');file.mute=True;g.links.new(rl.outputs['Image'],file.inputs[0]);g.nodes.active=grade
backend.sync_scene=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Freestyle sync invoked'))
s.render.use_freestyle=True;s.render.use_sequencer=True
checks={};observed=[]
def watch(scene,*args):
 if scene.render.engine in {'BLENDER_EEVEE_NEXT','CYCLES'}:
  assert helper.hide_render and preview.hide_render and not drawing.hide_render
  assert not scene.render.use_freestyle and not scene.lea_line.auto_sync
  observed.append(True)
bpy.app.handlers.render_pre.append(watch)
def fingerprint():
 return {'nodes':[(x.name,x.bl_idname,x.mute,getattr(x,'use_alpha',None)) for x in g.nodes],
 'links':sorted((x.from_node.name,x.from_socket.identifier,x.to_node.name,x.to_socket.identifier) for x in g.links),
 'active':g.nodes.active.name,'objects':[(o.name,o.hide_render) for o in s.objects],
 'flags':(s.render.engine,s.render.use_freestyle,s.render.use_compositing,s.render.use_sequencer,s.use_nodes,s.lea_line.auto_sync)}
def run(label):
 before=fingerprint();s.render.filepath=str(OUT/(label+'.png'));result=gpu_composite.render(s,write_still=True);assert fingerprint()==before
 assert gpu_render._captured_pixels is None and not gpu_render._capture_requested
 assert not [im for im in bpy.data.images if im.name.startswith('.REA GPU line composite')]
 return result
result=run('combined');assert result['status']=='ok' and observed
im=bpy.data.images.load(s.render.filepath,check_existing=False);a=np.array(im.pixels[:]).reshape(im.size[1],im.size[0],4);bpy.data.images.remove(im)
assert ((a[:,:,0]>a[:,:,2]+.1)&(a[:,:,3]>.1)).sum()>100
assert (a[:,:,3]==0).sum()>100
checks['beauty_GPU_color_alpha_and_transparent_background']=True
# Check the actual beauty/compositor result, not just the raw line buffer.
# A single open quad avoids coincident front/back cube edges.
mat.use_nodes=True;mat.node_tree.nodes.clear();emit=mat.node_tree.nodes.new('ShaderNodeEmission');emit.inputs['Color'].default_value=(.1,.2,.3,1);emit.inputs['Strength'].default_value=1
output=mat.node_tree.nodes.new('ShaderNodeOutputMaterial');mat.node_tree.links.new(emit.outputs[0],output.inputs['Surface'])
grade.inputs['Bright'].default_value=0;s.view_settings.look='None';s.view_settings.exposure=0;s.view_settings.gamma=1
run('numeric_color_alpha')
im=bpy.data.images.load(s.render.filepath,check_existing=False);pixel=np.array(im.pixels[:]).reshape(im.size[1],im.size[0],4)[96,87,:3];bpy.data.images.remove(im)
linear=np.where(pixel<=.04045,pixel/12.92,((pixel+.055)/1.055)**2.4)
expected=np.array((.55,.1,.15))
assert np.max(np.abs(linear-expected))<.008,(pixel,linear,expected)
checks['composited_half_alpha_RGB_matches_analytic_linear_blend']=True
# Preserve a non-identity authored post-process in the following checks.
grade.inputs['Bright'].default_value=2
checks['generated_lines_excluded_authored_GP_kept']=True
checks['original_graph_settings_and_image_ownership_restored']=True
s.use_nodes=False;comp.mute=True;comp.use_alpha=False;file.mute=False
run('disabled_graph');assert file.mute is False and comp.mute is True and comp.use_alpha is False and s.use_nodes is False
checks['disabled_authored_compositor_restored']=True
s.use_nodes=True;comp.mute=False;comp.use_alpha=True;file.mute=True
s.render.engine='CYCLES';s.cycles.device='CPU';s.cycles.samples=2
run('cycles');s.render.engine='BLENDER_EEVEE_NEXT'
checks['Cycles_beauty_with_GPU_lines']=True
invoke=gpu_composite._render_beauty
def failure(*args):raise RuntimeError('simulated beauty failure')
gpu_composite._render_beauty=failure
before=fingerprint()
try:run('failure');raise AssertionError('failure not raised')
except RuntimeError as error:assert 'simulated beauty' in str(error)
assert fingerprint()==before and not gpu_render._capture_requested
gpu_composite._render_beauty=lambda *args:{'CANCELLED'}
assert run('cancel')['status']=='cancelled'
gpu_composite._render_beauty=invoke
checks['beauty_failure_and_cancel_restore_everything']=True
assert 'FINISHED' in bpy.ops.lea_line.render()
checks['normal_REA_Render_operator_uses_GPU_composite']=True
bpy.app.handlers.render_pre.remove(watch)
(OUT/'report.json').write_text(json.dumps({'status':'ok','checks':checks,'first_render':result,'observed_beauty_callbacks':len(observed)},indent=2),encoding='utf-8')
print('GPU_COMPOSITE_REGRESSION_OK',json.dumps(checks),flush=True)
