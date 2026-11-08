"""Run GUI Blender with --factory-startup --enable-event-simulate --python this_file."""
import bpy,sys,json,time,traceback
from pathlib import Path
ROOT=Path(__import__('os').environ.get('REA_GPU_TEST_SOURCE',str(Path(__file__).resolve().parents[1])));OUT=Path(__import__('os').environ.get('REA_GPU_TEST_OUT',str(ROOT/'artifacts/f12_event')));OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT))
import lea_line;lea_line.register()
from lea_line import backend,ui,f12_render
bpy.context.preferences.use_preferences_save=False;bpy.context.preferences.view.show_splash=False;bpy.context.preferences.view.render_display_type='NONE'
(OUT/'temporary').mkdir(exist_ok=True);bpy.context.preferences.filepaths.temporary_directory=str(OUT/'temporary')
state={}
def finish(error=None):
 report={'status':'failed' if error else 'ok','error':error,'invocation':f12_render.LAST_INVOCATION,'events':state}
 (OUT/'event_probe.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print('F12_EVENT_PROBE',report['status'],error,flush=True)
 def quit():bpy.ops.wm.quit_blender();return None
 bpy.app.timers.register(quit,first_interval=.1)
def setup():
 try:
  s=bpy.context.scene;s.lea_line.auto_sync=False;backend.ensure_default_tree(s,create_detail=True)
  s.render.resolution_x=s.render.resolution_y=128;s.render.resolution_percentage=100;s.eevee.taa_render_samples=8
  w=bpy.context.window_manager.windows[0];area=next(a for a in w.screen.areas if a.type=='VIEW_3D');reg=next(r for r in area.regions if r.type=='WINDOW')
  state['keymaps']=[(config.name,km.name,k.idname,k.active) for config in (w.id_data.keyconfigs.default,w.id_data.keyconfigs.addon,w.id_data.keyconfigs.user) for km in config.keymaps for k in km.keymap_items if k.type=='F12' and not k.ctrl and not k.shift and not k.alt]
  w.event_simulate(type='MOUSEMOVE',value='NOTHING',x=reg.x+reg.width//2,y=reg.y+reg.height//2)
  w.event_simulate(type='F12',value='PRESS',x=reg.x+reg.width//2,y=reg.y+reg.height//2);w.event_simulate(type='F12',value='RELEASE',x=reg.x+reg.width//2,y=reg.y+reg.height//2)
  state['deadline']=time.monotonic()+30;bpy.app.timers.register(poll,first_interval=.5)
 except Exception:finish(traceback.format_exc())
 return None
def poll():
 try:
  if f12_render.LAST_INVOCATION:
   assert f12_render.LAST_INVOCATION['route']=='GPU',f12_render.LAST_INVOCATION
   assert f12_render.LAST_INVOCATION['status']=='dispatched',f12_render.LAST_INVOCATION
   finish();return None
  if time.monotonic()>state['deadline']:raise AssertionError('F12 key event did not reach the REA operator')
 except Exception:finish(traceback.format_exc());return None
 return .5
bpy.app.timers.register(setup,first_interval=1.)
