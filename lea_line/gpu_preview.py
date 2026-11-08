"""Experimental Solid-view GPU edge classification, projection and depth testing.

Blender mesh/depsgraph access stays on the main thread. View-independent edge
adjacency is uploaded once; camera movement changes shader uniforms only.
Not the Pencil+ rasterizer: joins/stamp coverage and AA remain different.
"""
import math
import time
import bpy
import numpy as np
from bpy.app.handlers import persistent

_states = {}
_handle = None
_shader = None
_batch = None
_building = False


def active(scene):
    return scene.as_pointer() in _states


def stats(scene):
    entry = _states.get(scene.as_pointer(), {})
    return {key: entry.get(key) for key in ('edges', 'build_ms', 'draw_ms', 'draws', 'uploads', 'dirty', 'error')}


def _jobs(scene):
    from . import backend, routing, material_scope, texture_maps, stroke_variation
    if len(scene.view_layers)!=1:
        raise ValueError('GPU 시험 경로는 단일 뷰 레이어만 지원합니다')
    jobs = []
    for node, common in backend._specifications(scene.lea_line.tree):
        if node.hidden or node.intersection or node.ridge_valley or node.suggestive_contour:
            raise ValueError('GPU: 기본 보이는 선만 지원합니다. 숨은 선·교차선·곡면 특수선은 Native를 사용하세요')
        if material_scope.active(node) or node.use_material_functions:
            raise ValueError('GPU: 재질별 얼굴 선택·Line Functions는 Native를 사용하세요')
        if not node.visible:
            continue
        brush, deferred = routing.live_brush(node, common, 'VISIBLE')
        if deferred or texture_maps.maps(brush) or stroke_variation.snapshot(brush) is not None:
            raise ValueError('GPU: 맵·랜덤·혼합 브러시는 Native를 사용하세요')
        if getattr(brush, 'brush_type', 'SIMPLE') == 'MULTIPLE' or getattr(brush, 'brush_map_on', False):
            raise ValueError('GPU: 다중 스탬프·브러시 맵은 Native를 사용하세요')
        if any(getattr(brush, key, False) for key in ('dashed', 'taper', 'size_curve_enabled',
                'alpha_curve_enabled', 'distortion_enabled', 'distortion_map_on')):
            raise ValueError('GPU: 점선·굵기/알파 곡선·왜곡은 Native를 사용하세요')
        if any(getattr(brush, key, 0) for key in ('min_line_length', 'extend', 'offset_x', 'offset_y', 'normal_offset')):
            raise ValueError('GPU: 길이 제한·선 오프셋은 Native를 사용하세요')
        if getattr(brush, 'caps', 'ROUND') != 'ROUND' or getattr(brush, 'hidden_mask_objects', ()):
            raise ValueError('GPU: Round 캡 기본 실선만 지원합니다')
        if getattr(brush, 'distance_reference_enabled', False) or getattr(brush, 'distance_curve_table', None):
            raise ValueError('GPU: 거리 참조·거리 곡선은 Native를 사용하세요')
        channels = getattr(brush, 'distance_channels', {})
        if any(c.get('reference') or c.get('profile') is not None for c in channels.values()):
            raise ValueError('GPU: 독립 거리 참조·곡선은 Native를 사용하세요')
        # Non-default Normal stamp shaping cannot be represented by a solid edge.
        if (abs(getattr(brush, 'stretch', 0.)) > 1e-6 or getattr(brush, 'groove', 0.)
                or abs(getattr(brush, 'stamp_reduction_start', 1.)-1.) > 1e-6
                or abs(getattr(brush, 'stamp_reduction_end', 1.)-1.) > 1e-6):
            raise ValueError('GPU: 스탬프 변형은 Native를 사용하세요')
        collection = backend.source_collection(scene, node)
        targets = set(collection.all_objects) if collection else set(scene.objects)
        jobs.append((node, brush, targets))
    return jobs


def _records(mesh, matrix, node):
    """Vectorized topology preparation; no view-dependent edge selection here."""
    nv, ne, npoly, nl = len(mesh.vertices), len(mesh.edges), len(mesh.polygons), len(mesh.loops)
    if not nv or not ne or not npoly:
        return np.empty((0, 4, 4), np.float32), np.empty((0,4),np.float32)
    vertices = np.empty(nv*3, np.float32); mesh.vertices.foreach_get('co', vertices)
    edge_vertices = np.empty(ne*2, np.int32); mesh.edges.foreach_get('vertices', edge_vertices)
    loop_edges = np.empty(nl, np.int32); mesh.loops.foreach_get('edge_index', loop_edges)
    lengths = np.empty(npoly, np.int32); mesh.polygons.foreach_get('loop_total', lengths)
    faces = np.repeat(np.arange(npoly, dtype=np.int32), lengths)
    first = np.full(ne, npoly, np.int32); last = np.full(ne, -1, np.int32)
    np.minimum.at(first, loop_edges, faces); np.maximum.at(last, loop_edges, faces)
    counts = np.bincount(loop_edges, minlength=ne)
    if np.any(counts > 64):
        raise ValueError('GPU: 한 모서리에 64개를 넘는 면이 연결된 경우 Native를 사용하세요')
    valid = counts > 0
    first = np.minimum(first, npoly-1); last = np.maximum(last, 0)
    normals = np.empty(npoly*3, np.float32); mesh.polygons.foreach_get('normal', normals)
    transform = np.asarray(matrix, dtype=np.float32)
    normal_matrix = np.linalg.inv(transform[:3, :3]).T
    normals = normals.reshape(-1, 3) @ normal_matrix.T
    normals /= np.maximum(1e-20, np.linalg.norm(normals, axis=1)[:, None])
    n0, n1 = normals[first], normals[last]
    dot = np.clip((n0*n1).sum(axis=1), -1., 1.)
    border = counts == 1
    fixed = border & bool(node.border or node.outline)
    if node.crease:
        fixed |= (counts == 2) & (dot < math.cos(math.pi-node.crease_angle))
    materials = np.empty(npoly, np.int32); mesh.polygons.foreach_get('material_index', materials)
    if node.material_boundary:
        fixed |= (counts == 2) & (materials[first] != materials[last])
    if node.edge_mark:
        marks = np.empty(ne, bool); mesh.edges.foreach_get('use_freestyle_mark', marks)
        fixed |= marks
    silhouette = (counts == 2) & bool(node.outline) & (dot < 1.-1e-7)
    # Store ALL incident normals for non-manifold edges. A single GPU instance
    # ORs their facing tests, avoiding duplicated strokes/alpha accumulation.
    extra=[];extra_start=np.zeros(ne,np.float32);extra_count=np.zeros(ne,np.float32)
    nonmanifold=np.flatnonzero(counts>2)
    if len(nonmanifold):
        order=np.argsort(loop_edges,kind='stable'); offsets=np.concatenate(([0],np.cumsum(counts)))
        for edge in nonmanifold:
            incident=np.unique(faces[order[offsets[edge]:offsets[edge+1]]])
            ns=normals[incident]
            if node.crease and np.min(ns@ns.T)<math.cos(math.pi-node.crease_angle):fixed[edge]=True
            if node.material_boundary and np.any(materials[incident]!=materials[incident[0]]):fixed[edge]=True
            silhouette[edge]=bool(node.outline) and np.min(ns@ns.T)<1.-1e-7
            extra_start[edge]=len(extra);extra_count[edge]=len(ns)
            extra.extend((*normal,0.) for normal in ns)
    chosen = valid & (fixed | silhouette)
    endpoints = edge_vertices.reshape(-1, 2)[chosen]
    world = vertices.reshape(-1, 3) @ transform[:3, :3].T + transform[:3, 3]
    result = np.zeros((len(endpoints), 4, 4), np.float32)
    result[:, 0, :3] = world[endpoints[:, 0]]; result[:, 0, 3] = fixed[chosen]
    result[:, 1, :3] = world[endpoints[:, 1]]; result[:, 1, 3] = silhouette[chosen]
    result[:, 2, :3] = n0[chosen]; result[:, 3, :3] = n1[chosen]
    result[:, 2, 3]=extra_start[chosen];result[:, 3, 3]=extra_count[chosen]
    return result, np.asarray(extra,dtype=np.float32).reshape(-1,4)


def _channel(brush, kind):
    channels = getattr(brush, 'distance_channels', {})
    if kind in channels:
        value = channels[kind]
        return (value['near'], value['far'], value['factor'], float(value['enabled']))
    enabled = getattr(brush, 'distance_fade' if kind == 'SIZE' else 'distance_alpha', False)
    return (brush.distance_start, brush.distance_end,
            brush.far_thickness if kind == 'SIZE' else brush.far_alpha, float(enabled))


def sync(scene):
    global _handle, _building
    if bpy.app.background:
        raise ValueError('GPU 미리보기는 Blender 창에서 사용하세요')
    from . import backend
    from . import free_view
    followed=free_view._entries.get(scene.as_pointer())
    if followed:free_view._restore_navigation(followed)
    jobs = _jobs(scene)
    start = time.perf_counter(); _building = True
    try:
        window=next((w for w in bpy.context.window_manager.windows if w.scene==scene),None)
        if window is None:raise ValueError('GPU 미리보기 장면을 Blender 창에서 여세요')
        with bpy.context.temp_override(window=window):dg = bpy.context.evaluated_depsgraph_get()
        groups = []
        source_ids = set()
        for node, brush, targets in jobs:
            pieces = []; extra_pieces=[];extra_offset=0
            for instance in dg.object_instances:
                obj = instance.object
                parent=instance.parent.original if instance.is_instance and instance.parent else None
                if obj.type != 'MESH' or (obj.original not in targets and parent not in targets) or not instance.show_self:
                    continue
                if not instance.is_instance and not obj.original.visible_get(view_layer=dg.view_layer):
                    continue
                mesh = obj.to_mesh()
                try:
                    records,extra=_records(mesh, instance.matrix_world, node)
                    records[records[:,3,3]>2,2,3]+=extra_offset
                    pieces.append(records);extra_pieces.append(extra);extra_offset+=len(extra)
                    source_ids.add(obj.original.as_pointer())
                    if parent:source_ids.add(parent.as_pointer())
                    if obj.original.data:source_ids.add(obj.original.data.as_pointer())
                finally:obj.to_mesh_clear()
            records = np.concatenate(pieces) if pieces else np.empty((0,4,4), np.float32)
            if len(records) > 2_000_000:
                raise ValueError('GPU 시험 경로의 모서리 메모리 한도를 넘었습니다')
            extra=np.concatenate(extra_pieces) if extra_pieces else np.empty((0,4),np.float32)
            groups.append({'records': records, 'extra_normals':extra, 'texture': None, 'brush': brush,
                           'width': brush.thickness*backend.width_scale(scene, node),
                           'size_reduction': _channel(brush, 'SIZE'), 'alpha_reduction': _channel(brush, 'ALPHA')})
        _states[scene.as_pointer()] = {'groups': groups, 'source_ids': source_ids,
            'edges': sum(len(g['records']) for g in groups), 'build_ms': (time.perf_counter()-start)*1000,
            'draw_ms': 0., 'draws': 0, 'uploads': 0, 'dirty': False, 'error': ''}
        if _handle is None:_handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_VIEW')
        if not bpy.app.timers.is_registered(_watch):bpy.app.timers.register(_watch, first_interval=.2)
    finally:_building = False
    return ['GPU 시험 미리보기: Solid · 기본 실선/선형 거리 감소. 스탬프·연결부·AA는 Native와 다릅니다']


VERTEX = r'''
vec4 record(int edge, int field) {
    int index=edge*4+field; int width=textureSize(edgeData,0).x;
    return texelFetch(edgeData,ivec2(index%width,index/width),0);
}
float reduction(vec4 spec,float distanceValue) {
    return mix(1.0,mix(1.0,spec.z,clamp((distanceValue-spec.x)/max(0.000001,spec.y-spec.x),0.0,1.0)),spec.w);
}
void main() {
    int edge=gl_InstanceID;
    vec4 a=record(edge,0), b=record(edge,1);
    vec3 toEye=perspectiveView!=0 ? eyePosition-a.xyz : viewDirection;
    float f0=dot(record(edge,2).xyz,toEye), f1=dot(record(edge,3).xyz,toEye);
    bool outline=(f0>=0.0)!=(f1>=0.0);
    int extraCount=int(record(edge,3).w);
    if (a.w<0.5 && b.w>0.5 && extraCount>2) {
        bool front=false, back=false; int start=int(record(edge,2).w);
        int width=textureSize(extraNormals,0).x;
        for (int i=0;i<extraCount;i++) {
            int index=start+i;
            float facing=dot(texelFetch(extraNormals,ivec2(index%width,index/width),0).xyz,toEye);
            front=front || facing>=0.0; back=back || facing<0.0;
        }
        outline=front && back;
    }
    bool chosen=a.w>0.5 || (b.w>0.5 && outline);
    vec4 p=viewProjection*vec4(a.xyz,1.0), q=viewProjection*vec4(b.xyz,1.0);
    // Clip the segment against the near plane before dividing by w.
    float pa=p.z+p.w, qa=q.z+q.w;
    if (!chosen || (pa<=0.0 && qa<=0.0)) {
        gl_Position=vec4(2.0,2.0,2.0,1.0); lineCoord=vec2(0.0); segmentLength=0.0;
        worldA=vec3(0.0);worldB=vec3(0.0);inverseW=vec2(1.0);return;
    }
    vec3 wa=a.xyz, wb=b.xyz;
    if (pa<=0.0) { float t=pa/(pa-qa); p=mix(p,q,t); wa=mix(wa,wb,t); }
    else if (qa<=0.0) { float t=qa/(qa-pa); q=mix(q,p,t); wb=mix(wb,wa,t); }
    vec2 sa=p.xy/p.w*viewportSize*0.5, sb=q.xy/q.w*viewportSize*0.5;
    vec2 delta=sb-sa; float lengthValue=length(delta);
    vec2 tangent=delta/max(lengthValue,0.00001), normal=vec2(-tangent.y,tangent.x);
    vec3 span=wb-wa;
    float closest=clamp(dot(eyePosition-wa,span)/max(dot(span,span),1e-20),0.0,1.0);
    float distanceValue=length(eyePosition-mix(wa,wb,closest));
    float halfWidth=max(0.0,lineWidth*reduction(sizeReduction,distanceValue)*0.5);
    float extent=halfWidth+1.0;
    vec2 offset=tangent*mix(-extent,extent,corner.x)+normal*corner.y*extent;
    vec4 pos=mix(p,q,corner.x); pos.xy+=offset*2.0/viewportSize*pos.w;
    pos.z-=0.000002*pos.w;
    gl_Position=pos;
    lineCoord=vec2(mix(-extent,lengthValue+extent,corner.x),corner.y*extent);
    segmentLength=lengthValue;worldA=wa;worldB=wb;inverseW=vec2(1.0/p.w,1.0/q.w);
}
'''
FRAGMENT = r'''
float reduction(vec4 spec,float distanceValue) {
    return mix(1.0,mix(1.0,spec.z,clamp((distanceValue-spec.x)/max(0.000001,spec.y-spec.x),0.0,1.0)),spec.w);
}
void main() {
    float t=clamp(lineCoord.x/max(segmentLength,0.00001),0.0,1.0);
    float u=t*inverseW.y/max(mix(inverseW.x,inverseW.y,t),1e-20);
    float cameraDistance=length(eyePosition-mix(worldA,worldB,u));
    float radius=max(0.0,lineWidth*reduction(sizeReduction,cameraDistance)*0.5);
    float opacity=inkColor.a*reduction(alphaReduction,cameraDistance);
    float outside=max(max(-lineCoord.x,lineCoord.x-segmentLength),0.0);
    float distanceValue=length(vec2(outside,lineCoord.y));
    float coverage=clamp(radius+0.5-distanceValue,0.0,1.0);
    if (coverage<=0.0 || radius<=0.0 || opacity<=0.0) discard;
    fragColor=vec4(inkColor.rgb,opacity*coverage);
}
'''


def _resources():
    global _shader, _batch
    if _shader is not None:return
    import gpu
    from gpu_extras.batch import batch_for_shader
    info = gpu.types.GPUShaderCreateInfo()
    info.vertex_in(0, 'VEC2', 'corner')
    interface = gpu.types.GPUStageInterfaceInfo('rea_gpu_edge_interface')
    interface.no_perspective('VEC2', 'lineCoord')
    interface.flat('FLOAT', 'segmentLength')
    interface.flat('VEC3','worldA');interface.flat('VEC3','worldB');interface.flat('VEC2','inverseW')
    info.vertex_out(interface)
    info.sampler(0, 'FLOAT_2D', 'edgeData')
    info.sampler(1, 'FLOAT_2D', 'extraNormals')
    info.sampler(2, 'FLOAT_2D', 'styleData')
    info.typedef_source('''
#define viewportSize viewportInfo.xy
#define lineWidth viewportInfo.z
#define perspectiveView int(viewportInfo.w)
#define eyePosition eyeInfo.xyz
#define viewDirection directionInfo.xyz
#define inkColor texelFetch(styleData,ivec2(0,0),0)
#define sizeReduction texelFetch(styleData,ivec2(1,0),0)
#define alphaReduction texelFetch(styleData,ivec2(2,0),0)
''')
    # Keep per-view push constants below Blender's portable128-byte minimum.
    for kind,name in (('MAT4','viewProjection'),('VEC4','viewportInfo'),('VEC4','eyeInfo'),('VEC4','directionInfo')):
        info.push_constant(kind,name)
    info.fragment_out(0, 'VEC4', 'fragColor')
    info.vertex_source(VERTEX); info.fragment_source(FRAGMENT)
    _shader = gpu.shader.create_from_info(info)
    _batch = batch_for_shader(_shader, 'TRIS', {'corner':[(0,-1),(0,1),(1,-1),(1,1)]}, indices=[(0,2,1),(1,2,3)])


def _texture(group):
    import gpu
    count = len(group['records'])*4
    if count == 0:return
    width = min(4096, count); height = (count+width-1)//width
    data = np.zeros((height*width,4), np.float32)
    data[:count] = group['records'].reshape(-1,4)
    group['texture'] = gpu.types.GPUTexture((width,height), format='RGBA32F',
        data=gpu.types.Buffer('FLOAT', data.size, data.ravel()))
    extra=group['extra_normals']
    count=max(1,len(extra));width=min(4096,count);height=(count+width-1)//width
    data=np.zeros((height*width,4),np.float32);data[:len(extra)]=extra
    group['normal_texture']=gpu.types.GPUTexture((width,height),format='RGBA32F',
        data=gpu.types.Buffer('FLOAT',data.size,data.ravel()))
    brush=group['brush']
    values=np.asarray((*brush.color,brush.alpha,*group['size_reduction'],*group['alpha_reduction']),np.float32)
    group['style_texture']=gpu.types.GPUTexture((3,1),format='RGBA32F',data=gpu.types.Buffer('FLOAT',12,values))


def _draw():
    context = bpy.context; scene = context.scene
    if scene is None or not getattr(scene, 'lea_line', None) or not scene.lea_line.preview_enabled:return
    entry = _states.get(scene.as_pointer())
    if not entry or entry['error'] or bpy.app.is_job_running('RENDER'):return
    if context.space_data.shading.type != 'SOLID' or context.space_data.shading.show_xray:return
    # Always use the current region's matrices, including during navigation,
    # after leaving camera view, and in additional View3D areas. No helper is
    # needed: the scene camera remains solely a final-render setting.
    import gpu
    start=time.perf_counter()
    depth, mask, blend = gpu.state.depth_test_get(), gpu.state.depth_mask_get(), gpu.state.blend_get()
    try:
        _resources()
        gpu.state.depth_test_set('LESS_EQUAL'); gpu.state.depth_mask_set(False); gpu.state.blend_set('ALPHA')
        view=context.region_data; inverse=view.view_matrix.inverted()
        _shader.bind()
        direction=inverse.to_3x3() @ __import__('mathutils').Vector((0,0,1))
        _shader.uniform_float('viewProjection',view.perspective_matrix)
        _shader.uniform_float('eyeInfo',(*inverse.translation,0.))
        _shader.uniform_float('directionInfo',(*direction,0.))
        for group in entry['groups']:
            if not len(group['records']):continue
            if group['texture'] is None:_texture(group);entry['uploads']+=1
            brush=group['brush']
            _shader.uniform_sampler('edgeData',group['texture'])
            _shader.uniform_sampler('extraNormals',group['normal_texture'])
            _shader.uniform_sampler('styleData',group['style_texture'])
            _shader.uniform_float('viewportInfo',(context.region.width,context.region.height,group['width'],int(view.is_perspective)))
            _batch.draw_instanced(_shader, instance_count=len(group['records']))
        entry['draws']+=1;entry['draw_ms']=(time.perf_counter()-start)*1000
    except Exception as exc:
        entry['error']=str(exc);scene.lea_line.last_message='GPU 미리보기 실패: '+str(exc)
        print('REA GPU preview:',exc)
    finally:
        gpu.state.depth_test_set(depth);gpu.state.depth_mask_set(mask);gpu.state.blend_set(blend)


def stop(scene):
    global _handle, _shader, _batch
    _states.pop(scene.as_pointer(), None)
    if not _states:
        if _handle is not None:bpy.types.SpaceView3D.draw_handler_remove(_handle,'WINDOW');_handle=None
        _shader=None;_batch=None


@persistent
def _changed(scene, depsgraph):
    if _building:return
    entry=_states.get(scene.as_pointer())
    if entry and any(isinstance(update.id,(bpy.types.Object,bpy.types.Mesh)) and
                     (update.id.original.as_pointer() in entry['source_ids'] or isinstance(update.id,bpy.types.Mesh)
                      or getattr(update.id,'type',None)=='MESH' or getattr(update.id,'instance_type',None)=='COLLECTION')
                     for update in depsgraph.updates):
        entry['dirty']=True


def _watch():
    if bpy.app.is_job_running('RENDER'):return .2
    scenes={s.as_pointer():s for s in bpy.data.scenes}
    for key,entry in list(_states.items()):
        scene=scenes.get(key)
        if scene is None:_states.pop(key,None);continue
        if not scene.lea_line.preview_enabled or scene.lea_line.preview_compute!='GPU':stop(scene);continue
        if entry['dirty'] or entry['error']:
            # Retry on the main thread; viewport falls back to Native on failure.
            from . import viewport
            if entry['error']:
                scene.lea_line.preview_compute='NATIVE'
            else:
                try:viewport.sync_preview(scene)
                except Exception as exc:scene.lea_line.last_message='GPU 갱신 실패: '+str(exc);stop(scene)
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type=='VIEW_3D':area.tag_redraw()
    return .2 if _states else None


@persistent
def reset(*_args):
    global _handle,_shader,_batch
    _states.clear()
    if _handle is not None:bpy.types.SpaceView3D.draw_handler_remove(_handle,'WINDOW');_handle=None
    _shader=None;_batch=None


def register():
    if _changed not in bpy.app.handlers.depsgraph_update_post:bpy.app.handlers.depsgraph_update_post.append(_changed)
    for handlers in (bpy.app.handlers.load_pre,bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre):
        if reset not in handlers:handlers.append(reset)


def unregister():
    reset()
    if bpy.app.timers.is_registered(_watch):bpy.app.timers.unregister(_watch)
    if _changed in bpy.app.handlers.depsgraph_update_post:bpy.app.handlers.depsgraph_update_post.remove(_changed)
    for handlers in (bpy.app.handlers.load_pre,bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre):
        if reset in handlers:handlers.remove(reset)
