"""Quantitative GPU line checks: pixel width, color, opacity and mesh occlusion."""
import bpy,sys,os,json,math
from pathlib import Path
import numpy as np
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view
ROOT=Path(os.environ.get('REA_GPU_TEST_SOURCE',str(Path(__file__).resolve().parents[1])))
OUT=Path(os.environ.get('REA_GPU_TEST_OUT',str(ROOT/'artifacts/gpu_style_metrics')));OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT));import lea_line;lea_line.register()
from lea_line import backend,ui,gpu_render
bpy.context.preferences.use_preferences_save=False
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
s=bpy.context.scene;s.render.resolution_x=800;s.render.resolution_y=400;s.render.resolution_percentage=100
bpy.ops.object.camera_add(location=(0,0,10));s.camera=bpy.context.object;s.camera.data.type='ORTHO';s.camera.data.ortho_scale=8
s.camera.data.clip_start=.1;s.camera.data.clip_end=100
with ui.suspend_sync():s.lea_line.auto_sync=False
tree=backend.ensure_default_tree(s,create_detail=True);n,b=backend._specifications(tree)[0];brush=getattr(b,'source',b)
def quad(name,x,z,width=.5):
 mesh=bpy.data.meshes.new(name);mesh.from_pydata([(x-width/2,-1.5,z),(x+width/2,-1.5,z),(x+width/2,1.5,z),(x-width/2,1.5,z)],[],[(0,1,2,3)])
 obj=bpy.data.objects.new(name,mesh);s.collection.objects.link(obj);return obj
planes=[quad('Near',-2,7),quad('Middle',0,4),quad('Far',2,1)]
with ui.suspend_sync():
 n.target_mode='LISTED';n.objects.clear()
 for o in planes:n.objects.add().object=o
 n.outline=n.crease=n.hidden=False;n.border=n.visible=True
 brush.thickness=8;brush.color=(.2,.5,.8);brush.alpha=1
 brush.distance_fade=True;brush.distance_alpha=True;brush.distance_start=3;brush.distance_end=9;brush.far_thickness=.25;brush.far_alpha=.2
checks={};rows=[]
def capture():
 result=gpu_render.render_lines(s,capture=True);assert result['status']=='ok';return result.pop('pixels')
def measure(rgba,o):
 point=Vector((o.location.x,0,0))
 # These meshes store their world positions in the vertex coordinates.
 point=o.matrix_world@o.data.vertices[0].co;point.y=0
 uv=world_to_camera_view(s,s.camera,point);x=int(uv.x*800);y=int(uv.y*400)
 band=rgba[y-1:y+2,x-10:x+11];alpha=band[:,:,3];peak=float(alpha.max())
 width=float(alpha.sum(axis=1).mean()/peak)
 distance=(s.camera.matrix_world.translation-point).length;t=min(1,max(0,(distance-3)/6))
 expected_width=8*(1-.75*t);expected_alpha=1-.8*t
 color=band[:,:,:3][alpha>.01]/alpha[alpha>.01,None]
 row={'object':o.name,'distance':distance,'width_px':width,'expected_width_px':expected_width,'alpha':peak,'expected_alpha':expected_alpha,'color':color.mean(axis=0).tolist()}
 assert abs(width-expected_width)<.12,row
 assert abs(peak-expected_alpha)<.01,row
 assert np.max(np.abs(color-np.array(brush.color)))<.001,row
 return row
rgba=capture()
for o in planes:rows.append(measure(rgba,o))
checks['distance_pixel_width_and_opacity_against_analytic_values']=True
checks['premultiplied_color_matches_requested_linear_RGB']=True
# Independent Size/Alpha Reduction connections must match the same linear rules.
with ui.suspend_sync():
 brush.distance_fade=brush.distance_alpha=False
 for kind in ('Size','Alpha'):
  reduction=tree.nodes.new('LEADistanceReductionNode');reduction.distance_start=3;reduction.distance_end=9;reduction.far_thickness=.25;reduction.far_alpha=.2
  tree.links.new(reduction.outputs[0],n.inputs['V '+kind+' Reduction']);setattr(n,'v_'+kind.lower()+'_reduction_on',True)
linked=capture()
assert np.max(np.abs(linked-rgba))<.00001
checks['independent_size_alpha_connections_match_legacy_distance_channels']=True
# A second color must change RGB, without changing coverage or opacity.
with ui.suspend_sync():brush.color=(.8,.1,.3)
colored=capture();assert np.max(np.abs(colored[:,:,3]-rgba[:,:,3]))<.00001
mask=colored[:,:,3]>.01
assert np.max(np.abs(colored[:,:,:3][mask]/colored[:,:,3][mask,None]-np.array(brush.color)))<.001
checks['line_color_changes_without_changing_width_or_opacity']=True
with ui.suspend_sync():
 for socket in ('V Size Reduction','V Alpha Reduction'):
  for link in list(n.inputs[socket].links):tree.links.remove(link)
 n.v_size_reduction_on=n.v_alpha_reduction_on=False
 brush.distance_fade=brush.distance_alpha=False;brush.thickness=4;brush.alpha=.6;brush.color=(.1,.2,.9)
 n.objects.clear()
 for o in planes:o.hide_render=True
 back=quad('Occluded target',0,0,width=1);n.objects.add().object=back
 front=quad('Opaque occluder',1,1,width=2)
visible=capture()
def stripe(rgba,x):
 uv=world_to_camera_view(s,s.camera,Vector((x,0,0)));px=int(uv.x*800);py=int(uv.y*400)
 return float(rgba[py-25:py+25,px-4:px+5,3].sum())
left,right=stripe(visible,-.5),stripe(visible,.5)
assert left>50 and right==0,(left,right)
checks['opaque_mesh_hides_back_edge_but_keeps_front_edge']=True
with ui.suspend_sync():n.visible=False;n.hidden=True
hidden=capture();hleft,hright=stripe(hidden,-.5),stripe(hidden,.5)
assert hleft==0 and hright>50,(hleft,hright)
checks['hidden_line_selection_is_inverse_of_visible_selection']=True
# Distinct Line Sets keep their own width, color and alpha in a single draw.
from lea_line import node_workflow
with ui.suspend_sync():
 back.hide_render=front.hide_render=True
 left_obj=quad('Red set',-1.5,0);right_obj=quad('Blue set',1.5,0)
 n.visible=True;n.hidden=False;n.objects.clear();n.objects.add().object=left_obj;brush.color=(.8,.1,.2);brush.alpha=.6;brush.thickness=4
 other=tree.nodes.new('LEALineSetNode');other_brush=tree.nodes.new('LEABrushNode')
 tree.links.new(other_brush.outputs[0],other.inputs['Brush']);tree.links.new(other.outputs[0],node_workflow.available_socket(ui._line_output(tree)))
 other.target_mode='LISTED';other.objects.clear();other.objects.add().object=right_obj;other.outline=other.crease=other.hidden=False;other.border=other.visible=True
 other_brush.color=(.1,.2,.8);other_brush.alpha=.5;other_brush.thickness=6
multi=capture()
for x,color,width,opacity in [(-1.75,(.8,.1,.2),4,.6),(1.25,(.1,.2,.8),6,.5)]:
 uv=world_to_camera_view(s,s.camera,Vector((x,0,0)));px=int(uv.x*800);py=int(uv.y*400)
 band=multi[py-1:py+2,px-8:px+9];a=band[:,:,3];mask=a>.01;rgb=band[:,:,:3][mask]/a[mask,None]
 assert abs(float(a.max())-opacity)<.001
 assert abs(float(a.sum(axis=1).mean()/a.max())-width)<.01
 assert np.max(np.abs(rgb-np.array(color)))<.001
checks['multiple_LineSets_keep_separate_width_color_alpha']=True
report={'status':'ok','checks':checks,'distance_metrics':rows,'occlusion':{'visible_left':left,'visible_right':right,'hidden_left':hleft,'hidden_right':hright},'scope':'Orthographic, opaque mesh occluder, 800x400, round solid brush. Does not establish transparent-material or Pencil+ AA parity.'}
(OUT/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print('GPU_STYLE_METRICS_OK',json.dumps(report),flush=True)
