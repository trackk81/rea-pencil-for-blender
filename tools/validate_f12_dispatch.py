"""Render-based F12 dispatcher checks, including selected line-only engine."""
import bpy,sys,os,json
from pathlib import Path
ROOT=Path(os.environ.get('REA_GPU_TEST_SOURCE',str(Path(__file__).resolve().parents[1])))
OUT=Path(os.environ.get('REA_GPU_TEST_OUT',str(ROOT/'artifacts/f12_dispatch')));OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT));import lea_line;lea_line.register()
from lea_line import ui,backend,f12_render,gpu_render
s=bpy.context.scene;s.render.resolution_x=s.render.resolution_y=96;s.render.resolution_percentage=100;s.eevee.taa_render_samples=4
with ui.suspend_sync():s.lea_line.auto_sync=False
tree=backend.ensure_default_tree(s,create_detail=True)
backend.sync_scene=lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('legacy sync was called'))
with ui.suspend_sync():s.lea_line.auto_sync=True;s.lea_line.f12_gpu_lines=True
checks={}
assert 'FINISHED' in bpy.ops.lea_line.prepared_render('EXEC_DEFAULT')
assert f12_render.LAST_INVOCATION['route']=='GPU' and f12_render.LAST_INVOCATION['output']=='COMPOSITE'
checks['existing_F12_preparation_operator_dispatches_GPU_composite']=True
s.render.engine='CYCLES';s.cycles.samples=1;s.cycles.device='GPU'
s.render.compositor_device='GPU';s.lea_line.cycles_cpu_render=True
cpu_passes=[]
def watch_cpu_passes(scene,*args):
 cpu_passes.append((scene.render.engine,scene.cycles.device,scene.render.compositor_device))
bpy.app.handlers.render_pre.append(watch_cpu_passes)
try:
 assert 'FINISHED' in bpy.ops.lea_line.prepared_render('EXEC_DEFAULT')
finally:
 bpy.app.handlers.render_pre.remove(watch_cpu_passes)
assert cpu_passes==[('REA_PENCIL_GPU','GPU','GPU'),('CYCLES','CPU','CPU')],cpu_passes
assert s.render.engine=='CYCLES' and s.cycles.device=='GPU' and s.render.compositor_device=='GPU'
checks['F12_Cycles_CPU_beauty_and_compositor_restore_devices']=True
cpu_passes.clear()
s.lea_line.final_line_backend='CPU'
bpy.app.handlers.render_pre.append(watch_cpu_passes)
try:
 assert 'FINISHED' in bpy.ops.lea_line.prepared_render('EXEC_DEFAULT')
finally:
 bpy.app.handlers.render_pre.remove(watch_cpu_passes)
assert cpu_passes==[('REA_PENCIL_CPU','GPU','GPU'),('CYCLES','CPU','CPU')],cpu_passes
assert f12_render.LAST_INVOCATION['route']=='CPU' and gpu_render.LAST_RESULT['alpha_mass']>0
assert s.cycles.device=='GPU' and s.render.compositor_device=='GPU'
checks['F12_CPU_lines_and_Cycles_CPU_beauty_restore_devices']=True
s.lea_line.final_line_backend='GPU'
s.render.engine='REA_PENCIL_GPU'
assert 'FINISHED' in bpy.ops.lea_line.prepared_render('EXEC_DEFAULT')
assert f12_render.LAST_INVOCATION['output']=='LINES' and gpu_render.LAST_RESULT['status']=='ok'
checks['selected_GPU_line_engine_keeps_line_only_F12_semantics']=True
native=f12_render._native_render;calls=[];f12_render._native_render=lambda scene:calls.append(scene.name) or {'FINISHED'}
try:
 with ui.suspend_sync():s.lea_line.f12_gpu_lines=False
 assert 'FINISHED' in bpy.ops.lea_line.prepared_render('EXEC_DEFAULT');assert calls and f12_render.LAST_INVOCATION['route']=='BLENDER'
 checks['opt_out_uses_native_adapter']=True
 with ui.suspend_sync():s.lea_line.f12_gpu_lines=True;s.lea_line.tree=None
 assert 'FINISHED' in bpy.ops.lea_line.prepared_render('EXEC_DEFAULT');assert len(calls)==2
 checks['no_REA_graph_uses_native_adapter']=True
finally:f12_render._native_render=native
lea_line.unregister();lea_line.register();lea_line.unregister()
checks['registration_roundtrip']=True
(OUT/'report.json').write_text(json.dumps({'status':'ok','checks':checks},indent=2),encoding='utf-8')
print('F12_DISPATCH_TEST_OK',json.dumps(checks),flush=True)
