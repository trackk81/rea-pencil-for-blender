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

def marked_strip(name, y, segment_count):
    columns = segment_count + 1
    vertices = [(column*.12, y+offset, 0)
                for offset in (-.1, 0, .1) for column in range(columns)]
    faces = [(row*columns+i, row*columns+i+1, (row+1)*columns+i+1, (row+1)*columns+i)
             for row in (0, 1) for i in range(segment_count)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    for edge in mesh.edges:
        edge.use_freestyle_mark = all(columns <= i < 2*columns for i in edge.vertices)
    obj = bpy.data.objects.new(name, mesh)
    s.collection.objects.link(obj)
    return obj


with ui.suspend_sync():
    other.visible = False
    n.objects.clear()
    n.border = False
    n.edge_mark = True
    for y, count in ((-.5, 1), (.5, 2)):
        n.objects.add().object = marked_strip(f'{count} marked segments', y, count)
    detail = next(node for node in tree.nodes if node.bl_idname == 'LEABrushDetailNode')

for minimum, expected in ((0, (True, True)), (13, (False, True)),
                          (24, (False, True)), (25, (False, False))):
    with ui.suspend_sync():
        detail.min_line_length = minimum
    result = capture()
    present = []
    for y in (-.5, .5):
        uv = world_to_camera_view(s, s.camera, Vector((.06, y, 0)))
        x, py = int(uv.x*800), int(uv.y*400)
        present.append(bool(np.max(result[py-1:py+2, x-1:x+2, 3]) > .01))
    assert tuple(present) == expected, (minimum, present)
checks['minimum_projected_length_keeps_complete_chains_at_the_threshold']=True

with ui.suspend_sync():
    detail.min_line_length = 0
unextended = capture()
with ui.suspend_sync():
    detail.extend = 6
extended = capture()
for y in (-.5, .5):
    uv = world_to_camera_view(s, s.camera, Vector((0, y, 0)))
    py = int(uv.y*400)
    before = np.flatnonzero(unextended[py, :, 3] > .01)
    after = np.flatnonzero(extended[py, :, 3] > .01)
    assert (before[0] - after[0], after[-1] - before[-1]) == (6, 6)
checks['extension_adds_the_requested_pixels_to_both_chain_ends']=True

# Put a tile boundary between the 12 px stroke endpoint and its extended cap.
tile_side = gpu_render._TILE_SIDE
try:
    gpu_render._TILE_SIDE = 231
    tiled = gpu_render.render_lines(s, capture=True)
    assert tiled['status'] == 'ok' and tiled['render_tiles'] > 1, tiled['status']
    assert np.max(np.abs(tiled['pixels'] - extended)) < .002
finally:
    gpu_render._TILE_SIDE = tile_side
checks['extension_crosses_tile_boundary_without_losing_endpoint_depth']=True

# Occluders split the 24 px chain into 5 px and 9 px visible pieces. Its
# hidden run crosses the joint between both marked 12 px segments.
with ui.suspend_sync():
    detail.extend = 0
    cover = quad('Middle of marked chain', .10, 1, width=.10)
    cover.location.y = .5
    cover.scale.y = .1


def marked_ink(pixels, x, y):
    uv = world_to_camera_view(s, s.camera, Vector((x, y, 0)))
    px, py = int(uv.x*800), int(uv.y*400)
    return bool(np.max(pixels[py-1:py+2, px-1:px+2, 3]) > .01)


visible = capture()
assert all(marked_ink(visible, x, y) for x,y in ((.03,.5), (.21,.5), (.06,-.5)))
with ui.suspend_sync():
    detail.min_line_length = 10
filtered = capture()
assert marked_ink(filtered, .06, -.5)
assert not any(marked_ink(filtered, x, .5) for x in (.03,.21))
checks['visible_minimum_is_applied_to_each_exposed_piece']=True

with ui.suspend_sync():
    n.visible, n.hidden = False, True
    detail.min_line_length = 9
hidden = capture()
assert marked_ink(hidden, .10, .5)
try:
    gpu_render._TILE_SIDE = 434
    tiled = gpu_render.render_lines(s, capture=True)
    assert tiled['status'] == 'ok' and tiled['render_tiles'] > 1
    assert np.max(np.abs(tiled['pixels'] - hidden)) < .002
finally:
    gpu_render._TILE_SIDE = tile_side
with ui.suspend_sync():
    detail.min_line_length = 11
assert not marked_ink(capture(), .10, .5)
checks['hidden_minimum_follows_a_piece_across_the_chain_joint_and_tile_boundary']=True

with ui.suspend_sync():
    inner_cover = quad('Middle of one edge', .06, 1, width=.04)
    inner_cover.location.y = -.5
    inner_cover.scale.y = .1
    detail.min_line_length = 3
assert marked_ink(capture(), .06, -.5)
with ui.suspend_sync():
    detail.min_line_length = 5
assert not marked_ink(capture(), .06, -.5)
checks['minimum_measures_a_hidden_span_inside_one_edge']=True

with ui.suspend_sync():
    n.visible, n.hidden = True, False
    detail.min_line_length = 0
assert not marked_ink(capture(), .10, .5)
with ui.suspend_sync():
    detail.extend = 5
visible_extended = capture()
assert marked_ink(visible_extended, .10, .5)
# The extended cap crosses the core tile boundary and samples the same cut.
try:
    gpu_render._TILE_SIDE = 430
    tiled = gpu_render.render_lines(s, capture=True)
    assert tiled['status'] == 'ok' and tiled['render_tiles'] > 1
    assert np.max(np.abs(tiled['pixels'] - visible_extended)) < .002
finally:
    gpu_render._TILE_SIDE = tile_side
checks['extension_fills_a_depth_cut_across_the_tile_boundary']=True

with ui.suspend_sync():
    detail.min_line_length = 10
assert not marked_ink(capture(), .10, .5)
checks['extension_respects_the_length_filter_at_depth_cuts']=True

with ui.suspend_sync():
    n.visible, n.hidden = False, True
    detail.min_line_length = 0
assert marked_ink(capture(), .03, .5)
checks['hidden_span_extends_into_the_adjacent_visible_area']=True

with ui.suspend_sync():
    n.objects.clear()
    mesh = bpy.data.meshes.new('Bent marked stroke')
    points = ((-.30,0), (.02,0), (.18,.28))
    vertices = [(x, y+offset, 0) for offset in (-.12,0,.12) for x,y in points]
    faces = [(row*3+i,row*3+i+1,(row+1)*3+i+1,(row+1)*3+i)
             for row in (0,1) for i in (0,1)]
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    for edge in mesh.edges:
        edge.use_freestyle_mark = all(3<=i<6 for i in edge.vertices)
    bent = bpy.data.objects.new(mesh.name, mesh)
    s.collection.objects.link(bent)
    n.objects.add().object = bent
    joint_cover = quad('Cover starts at the joint', 1.02, 1, width=2)
    detail.extend = 8
    n.visible, n.hidden = True, False

visible_joint = capture()
assert marked_ink(visible_joint, .08, 0)
assert not marked_ink(visible_joint, .06, .07)
try:
    gpu_render._TILE_SIDE = 430
    tiled = gpu_render.render_lines(s, capture=True)
    assert tiled['status'] == 'ok' and tiled['render_tiles'] > 1
    assert np.max(np.abs(tiled['pixels'] - visible_joint)) < .002
finally:
    gpu_render._TILE_SIDE = tile_side
checks['visible_joint_extends_the_incoming_tangent_across_a_tile_boundary']=True

with ui.suspend_sync():
    n.visible, n.hidden = False, True
hidden_joint = capture()
assert marked_ink(hidden_joint, -.02, -.07)
assert not marked_ink(hidden_joint, -.04, 0)
checks['hidden_joint_extends_its_own_tangent_without_a_horizontal_spur']=True

with ui.suspend_sync():
    n.visible, n.hidden = True, False
    detail.extend = 0
unshifted = capture()
with ui.suspend_sync():
    detail.offset_x, detail.offset_y = 11, 7
shifted = capture()
expected = np.zeros_like(unshifted)
expected[:-7, 11:] = unshifted[7:, :-11]
assert np.max(np.abs(shifted - expected)) < .002
checks['xy_offset_translates_ink_after_depth_classification']=True

# The stroke moves across the tile boundary while its source remains left.
try:
    gpu_render._TILE_SIDE = 436
    tiled = gpu_render.render_lines(s, capture=True)
    assert tiled['status'] == 'ok' and tiled['render_tiles'] > 1
    assert np.max(np.abs(tiled['pixels'] - shifted)) < .002
finally:
    gpu_render._TILE_SIDE = tile_side
checks['xy_offset_preserves_unshifted_depth_samples_across_tiles']=True

with ui.suspend_sync():
    detail.offset_x = detail.offset_y = 0
    detail.normal_offset = 6
offset_visible = capture()
assert marked_ink(offset_visible, -.1, .06)
assert not marked_ink(offset_visible, -.1, 0)
assert not marked_ink(offset_visible, .08, .06)
checks['normal_offset_moves_the_visible_mark_without_moving_its_occluder']=True

with ui.suspend_sync():
    joint_cover.hide_render = True
offset_bent = capture()
assert marked_ink(offset_bent, .02, .12)
try:
    gpu_render._TILE_SIDE = 462
    tiled = gpu_render.render_lines(s, capture=True)
    assert tiled['status'] == 'ok' and tiled['render_tiles'] > 1
    assert np.max(np.abs(tiled['pixels'] - offset_bent)) < .002
finally:
    gpu_render._TILE_SIDE = tile_side
checks['normal_offset_keeps_the_bend_joined_across_tiles']=True

with ui.suspend_sync():
    mixed = bpy.data.meshes.new('Opposite incident face directions')
    mixed.from_pydata(vertices, [], [faces[i] for i in (0,3,2,1)])
    mixed.update()
    for edge in mixed.edges:
        edge.use_freestyle_mark = all(3<=i<6 for i in edge.vertices)
    bent.data = mixed
longest_right = capture()
assert marked_ink(longest_right, -.10, -.06)
assert not marked_ink(longest_right, -.10, .06)
with ui.suspend_sync():
    joint_cover.hide_render = False
longest_visible_left = capture()
assert marked_ink(longest_visible_left, -.10, .06)
assert not marked_ink(longest_visible_left, -.10, -.06)
with ui.suspend_sync():
    n.visible, n.hidden = False, True
assert marked_ink(capture(), .13, .0725)
checks['mixed_normal_offset_follows_the_longest_eligible_span_after_occlusion']=True

with ui.suspend_sync():
    cover.hide_render = inner_cover.hide_render = joint_cover.hide_render = True
    n.visible, n.hidden = True, False
    polygon = ((-.72,-.20),(-.43,-.58),(.25,-.57),(.75,-.24),(.70,.25),(.12,.59),(-.54,.51))
    count = len(polygon)
    vertices = [(x*scale,y*scale,0) for scale in (.8,1,1.2) for x,y in polygon]
    pairs = [(i,(i+1)%count) for i in range(count)]
    inner = [(i,count+i,count+j,j) for i,j in pairs]
    outer = [(count+i,2*count+i,2*count+j,count+j) for i,j in pairs]
    # The longest edge votes negative, but one complete loop votes positive.
    negative = (False,True,False,False,True,False,True)
    faces = [a if side else b for a,b,side in zip(inner,outer,negative)]
    faces += [b if side else a for a,b,side in zip(inner,outer,negative)]
    loop = bpy.data.meshes.new('Length vote around a marked loop')
    loop.from_pydata(vertices, [], faces)
    loop.update()
    for edge in loop.edges:
        edge.use_freestyle_mark = all(count<=i<2*count for i in edge.vertices)
    bent.data = loop
weighted_loop = capture()
assert marked_ink(weighted_loop, -.09, -.515)
assert not marked_ink(weighted_loop, -.09, -.635)
assert marked_ink(weighted_loop, -.572, .14)
checks['closed_mixed_normal_offset_sums_face_directions_once_per_loop']=True

try:
    for covered, hidden in ((False,False),(True,False),(True,True)):
        with ui.suspend_sync():
            joint_cover.hide_render = not covered
            n.visible, n.hidden = not hidden, hidden
        gpu_render._TILE_SIDE = tile_side
        whole = capture()
        assert np.max(whole[:,:,3]) > .1
        gpu_render._TILE_SIDE = 462
        tiled = gpu_render.render_lines(s, capture=True)
        assert tiled['status'] == 'ok' and tiled['render_tiles'] > 1
        assert np.max(np.abs(tiled['pixels'] - whole)) < .002
finally:
    gpu_render._TILE_SIDE = tile_side
    with ui.suspend_sync():
        joint_cover.hide_render = True
        n.visible, n.hidden = True, False
checks['mixed_normal_offset_keeps_full_frame_directions_in_tiled_visible_and_hidden_passes']=True

with ui.suspend_sync():
    hinge = bpy.data.meshes.new('Marked edge between faces with different inclinations')
    hinge.from_pydata([(-.5,-.15,.45),(.5,-.15,.45),(-.5,0,0),(.5,0,0),
                       (-.5,.15,0),(.5,.15,0)], [], [(0,1,3,2),(2,3,5,4)])
    hinge.update()
    for edge in hinge.edges:
        edge.use_freestyle_mark = set(edge.vertices) == {2,3}
    bent.data = hinge
hinged = capture()
assert marked_ink(hinged, -.15, -.06)
assert not marked_ink(hinged, -.15, .06)
checks['normal_offset_prefers_the_face_facing_the_camera_and_keeps_its_own_edge_visible']=True

with ui.suspend_sync():
    joint_cover.hide_render = False
covered_hinge = capture()
assert marked_ink(covered_hinge, -.15, -.06)
assert not marked_ink(covered_hinge, .15, -.06)
checks['normal_offset_depth_correction_preserves_an_external_occluder']=True

with ui.suspend_sync():
    joint_cover.hide_render = True
    bent.hide_render = True
    n.objects.clear()
    whole_mark = marked_strip('Single wave edge', -.5, 1)
    whole_mark.scale.x = 6
    n.objects.add().object = whole_mark
    n.objects.add().object = marked_strip('Six connected wave edges', .5, 6)
    brush.thickness, brush.alpha = 3, 1
    detail.normal_offset = 0
    detail.distortion_enabled = True
    detail.distortion_amplitude, detail.distortion_wavelength = 6, 24
    detail.distortion_phase = math.pi/2
wave = capture()
assert marked_ink(wave, .24, .56) and not marked_ink(wave, .24, .44)
assert marked_ink(wave, .36, .44) and not marked_ink(wave, .36, .56)
wave_diff = np.abs(wave[135:165] - wave[235:265])
assert np.max(wave_diff) < .004, np.max(wave_diff)
checks['distortion_phase_is_continuous_across_straight_joins_without_alpha_buildup']=True

try:
    gpu_render._TILE_SIDE = 450
    tiled = gpu_render.render_lines(s, capture=True)
    assert tiled['status'] == 'ok' and tiled['render_tiles'] > 1
    assert np.max(np.abs(tiled['pixels'] - wave)) < .002
finally:
    gpu_render._TILE_SIDE = tile_side
checks['distortion_keeps_coverage_and_phase_across_gpu_render_tiles']=True

with ui.suspend_sync():
    wave_cover = quad('Finite distortion occluder', .30, 1, width=.28)
visible_wave = capture()
assert not marked_ink(visible_wave, .22, .5)
assert marked_ink(visible_wave, .56, .44) and not marked_ink(visible_wave, .56, .56)
with ui.suspend_sync():
    n.visible, n.hidden = False, True
hidden_wave = capture()
for y in (-.5,.5):
    assert marked_ink(hidden_wave, .28, y-.06) and not marked_ink(hidden_wave, .28, y+.06)
    assert marked_ink(hidden_wave, .40, y+.06) and not marked_ink(hidden_wave, .40, y-.06)
    assert not marked_ink(hidden_wave, .56, y-.06)
checks['distortion_restarts_at_hidden_entry_and_visible_reappearance']=True

try:
    gpu_render._TILE_SIDE = 450
    tiled = gpu_render.render_lines(s, capture=True)
    assert tiled['status'] == 'ok' and tiled['render_tiles'] > 1
    assert np.max(np.abs(tiled['pixels'] - hidden_wave)) < .004
finally:
    gpu_render._TILE_SIDE = tile_side
checks['distortion_hidden_phase_is_identical_across_render_tiles']=True

visible_cap = visible_wave[251:262, 442:444, 3]
hidden_cap = hidden_wave[251:262, 414:416, 3]
for cap in (visible_cap, hidden_cap):
    assert cap.max() > .5 and np.any((cap > .01) & (cap < .99))
assert not visible_wave[251:262, 439, 3].any()
assert not hidden_wave[251:262, 411, 3].any()
checks['distortion_round_cut_caps_keep_a_partial_alpha_fringe_across_the_cut']=True

with ui.suspend_sync():
    wave_cover.location.x = .08
hidden_at_join = capture()
with ui.suspend_sync():
    n.visible, n.hidden = True, False
visible_at_join = capture()
for pixels, columns in ((hidden_at_join, slice(422,424)), (visible_at_join, slice(424,426))):
    bottom = pixels[137:164, columns, 3]
    top = pixels[237:264, columns, 3]
    assert bottom.max() > .5 and top.max() > .5
    assert np.max(np.abs(bottom-top)) < .015
checks['distortion_cut_at_a_straight_join_preserves_visible_and_hidden_caps']=True

with ui.suspend_sync():
    wave_cover.hide_render = True
    n.visible, n.hidden = True, False
    whole_mark.location.x = -4.63
    whole_mark.scale.x = 45
offscreen_wave = capture()
assert marked_ink(offscreen_wave, .05, -.56) and not marked_ink(offscreen_wave, .05, -.44)
checks['distortion_keeps_phase_from_a_stroke_start_outside_the_frame']=True

with ui.suspend_sync():
    n.objects.clear()
    n.objects.add().object = whole_mark
    whole_mark.location.x = 0
    whole_mark.scale.x = 6
    detail.distortion_enabled = False
    s.lea_line.stroke_width_profile = 'PENCIL_SIMPLE'
for width, expected in ((.5,.25),(1.5,2),(3,4)):
    with ui.suspend_sync():
        brush.thickness = width
    for y in (0,.0025,.005):
        whole_mark.location.y = y
        alpha = capture()[140:160, 410:420, 3]
        assert np.max(np.abs(alpha.sum(axis=0)-expected)) < .003
checks['pencil_simple_gpu_coverage_matches_the_subpixel_width_model_at_three_phases']=True

with ui.suspend_sync():
    s.lea_line.stroke_width_profile = 'PHYSICAL'
physical_alpha = capture()[140:160, 410:420, 3]
assert np.max(np.abs(physical_alpha.sum(axis=0)-3)) < .003
checks['geometric_gpu_width_is_preserved_when_the_profile_is_disabled']=True

output = ui._line_output(tree)
with ui.suspend_sync():
    whole_mark.location.y = 0
    output.antialiasing = 0
hard = capture()[:,:,3]
assert np.any(hard == 1) and np.all((hard == 0) | (hard == 1))
with ui.suspend_sync():
    output.antialiasing = 1
default_filter = capture()[:,:,3]
with ui.suspend_sync():
    output.over_sampling = 2
sampled = capture()[:,:,3]
assert np.max(np.abs(sampled - default_filter)) > .05
for alpha in (default_filter, sampled):
    assert np.max(np.abs(alpha[140:160,410:420].sum(axis=0)-3)) < .003
with ui.suspend_sync():
    output.antialiasing = 2
    output.over_sampling = 4
soft = capture()[:,:,3]
assert np.count_nonzero((soft>.01)&(soft<.99)) > np.count_nonzero((sampled>.01)&(sampled<.99))
assert np.max(np.abs(soft[140:160,410:420].sum(axis=0)-3)) < .003
checks['line_aa_and_oversampling_change_gpu_coverage_without_changing_straight_ink_mass']=True

with ui.suspend_sync():
    n.visible, n.hidden = False, True
    wave_cover.hide_render = False
    detail.distortion_enabled = True
soft_wave = capture()
try:
    gpu_render._TILE_SIDE = 450
    tiled = gpu_render.render_lines(s, capture=True)
    assert tiled['status'] == 'ok' and tiled['render_tiles'] > 1
    assert soft_wave[:,:,3].max() > .5
    assert np.max(np.abs(tiled['pixels'] - soft_wave)) < .004
finally:
    gpu_render._TILE_SIDE = tile_side
checks['line_aa_filter_keeps_wave_cap_coverage_across_tiles']=True

try:
    s.render.use_border = s.render.use_crop_to_border = True
    s.render.border_min_x,s.render.border_min_y = .515,.27
    s.render.border_max_x,s.render.border_max_y = .58,.65
    region = gpu_render.render_lines(s, capture=True)
    x0,y0,x1,y1 = region['render_window']
    expected = soft_wave[y0:y1,x0:x1]
    assert region['pixels'].shape == expected.shape and expected[:,:,3].max() > .5
    assert np.max(np.abs(region['pixels']-expected)) < .004
finally:
    s.render.use_border = s.render.use_crop_to_border = False
checks['render_region_preserves_wave_phase_and_aa_at_the_border']=True

report={'status':'ok','checks':checks,'distance_metrics':rows,'occlusion':{'visible_left':left,'visible_right':right,'hidden_left':hleft,'hidden_right':hright},'scope':'Orthographic, opaque mesh occluder, 800x400, round solid brush. Does not establish transparent-material or Pencil+ AA parity.'}
(OUT/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print('GPU_STYLE_METRICS_OK',json.dumps(report),flush=True)
