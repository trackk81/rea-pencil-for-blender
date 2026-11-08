"""GPU edge classification, projection and mesh centerline depth testing.

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


def _tag_redraw(scene):
    # A node-editor toggle need not invalidate the separate View3D region.
    for window in bpy.context.window_manager.windows:
        if window.scene==scene:
            for area in window.screen.areas:
                if area.type=='VIEW_3D':area.tag_redraw()


def stats(scene):
    entry = _states.get(scene.as_pointer(), {})
    return {key: entry.get(key) for key in ('edges', 'build_ms', 'draw_ms', 'draws', 'uploads', 'dirty', 'error')}


def _jobs(scene):
    from . import backend, routing, material_scope, stroke_variation,gpu_styles
    if len(scene.view_layers)!=1:
        raise ValueError('GPU 시험 경로는 단일 뷰 레이어만 지원합니다')
    jobs = []
    for node, common in backend._specifications(scene.lea_line.tree):
        if material_scope.active(node) or node.use_material_functions:
            raise ValueError('GPU: 재질별 얼굴 선택·Line Functions는 Native를 사용하세요')
        # Resolve identities without creating/linking helper collections inside
        # a render callback. Render depsgraph objects are evaluated copies.
        available = {o.original for o in scene.objects}
        listed = getattr(node, 'objects', ())
        if len(listed):
            targets = {item.object.original for item in listed if item.object} & available
        elif getattr(node, 'target_mode', 'ALL') == 'ALL' or node.source_collection:
            targets = available
        else:
            targets = set()
        if node.source_collection:
            targets.intersection_update(o.original for o in node.source_collection.all_objects)
        for visibility,enabled in (('VISIBLE',node.visible),('HIDDEN',node.hidden)):
            if not enabled:continue
            if any(routing.feature_enabled(node,k,visibility) for k in ('intersection','ridge_valley','suggestive_contour')):
                raise ValueError('GPU: 교차선·곡면 특수선은 Native를 사용하세요')
            brush,deferred=routing.live_brush(node,common,visibility)
            if deferred or stroke_variation.snapshot(brush) is not None:raise ValueError('GPU: 랜덤·혼합 브러시는 Native를 사용하세요')
            gpu_styles.validate(brush)
            if getattr(brush,'brush_type','SIMPLE')=='MULTIPLE' or getattr(brush,'brush_map_on',False):raise ValueError('GPU: 다중 스탬프·브러시 맵은 Native를 사용하세요')
            if any(getattr(brush,k,False) for k in ('distortion_enabled','distortion_map_on')):raise ValueError('GPU: 왜곡은 Native를 사용하세요')
            if any(getattr(brush,k,0) for k in ('min_line_length','extend','offset_x','offset_y','normal_offset')):raise ValueError('GPU: 길이 제한·선 오프셋은 Native를 사용하세요')
            if getattr(brush,'caps','ROUND')!='ROUND' or getattr(brush,'hidden_mask_objects',()):raise ValueError('GPU: Round 캡만 지원하며 숨은 선 마스크는 Native를 사용하세요')
            if (abs(getattr(brush,'stretch',0.))>1e-6 or getattr(brush,'groove',0.) or abs(getattr(brush,'stamp_reduction_start',1.)-1.)>1e-6 or abs(getattr(brush,'stamp_reduction_end',1.)-1.)>1e-6):raise ValueError('GPU: 스탬프 변형은 Native를 사용하세요')
            proxy=routing.ResolvedBrush(node,{k:routing.feature_enabled(node,k,visibility) for k in ('outline','border','crease','edge_mark','material_boundary')})
            proxy.overrides['_gpu_hidden']=visibility=='HIDDEN'
            jobs.append((proxy,brush,targets))
    return jobs


def _records(mesh, matrix, node, payload=None):
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
    if payload is not None:
        payload.update(world_vertices=world,endpoints=endpoints,chosen_mask=chosen,loop_edges=loop_edges,face_counts=counts[chosen])
    return result, np.asarray(extra,dtype=np.float32).reshape(-1,4)


def _channel(brush, kind):
    channels = getattr(brush, 'distance_channels', {})
    if kind in channels:
        value = channels[kind]
        return (value['near'], value['far'], value['factor'], float(value['enabled']))
    enabled = getattr(brush, 'distance_fade' if kind == 'SIZE' else 'distance_alpha', False)
    return (brush.distance_start, brush.distance_end,
            brush.far_thickness if kind == 'SIZE' else brush.far_alpha, float(enabled))


def snapshot(scene, dg):
    """Copy evaluated geometry without installing viewport handlers or timers.

    A render engine passes its RENDER depsgraph; viewport sync passes VIEWPORT.
    All Blender data is copied synchronously in the calling Blender callback.
    """
    from . import backend, gpu_styles, gpu_depth
    start = time.perf_counter()
    jobs = _jobs(scene)
    groups = []
    source_ids = set()
    # The viewport depth buffer can reject the expanded stroke quad against
    # its own sloping surface. Classify the centerline against our unjittered
    # mesh depth in every shading mode, then draw the whole stroke as overlay.
    needs_depth=True
    for node, brush, targets in jobs:
        pieces = []; extra_pieces=[];extra_offset=0
        map_pieces=[];chain_vertices=[];chain_ends=[];vertex_offset=0
        style=gpu_styles.snapshot(brush,backend.width_scale(scene,node),getattr(node,'_gpu_hidden',False),needs_depth)
        for instance in dg.object_instances:
            obj = instance.object
            parent=instance.parent.original if instance.is_instance and instance.parent else None
            if obj.type != 'MESH' or (obj.original not in targets and parent not in targets) or not instance.show_self:
                continue
            if dg.mode != 'RENDER' and not instance.is_instance and not obj.original.visible_get(view_layer=dg.view_layer):
                continue
            mesh = obj.to_mesh()
            try:
                payload={};records,extra=_records(mesh, instance.matrix_world, node,payload)
                if len(records):
                    mapped=gpu_styles.mesh_maps(mesh,payload,style['maps'])
                    if mapped is not None:map_pieces.append(mapped)
                    if style['needs_chain']:
                        used,inverse=np.unique(payload['endpoints'],return_inverse=True)
                        chain_vertices.append(payload['world_vertices'][used]);chain_ends.append(inverse.reshape(-1,2)+vertex_offset);vertex_offset+=len(used)
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
        map_coordinates=np.concatenate(map_pieces) if map_pieces else np.zeros((1,4),np.float32)
        if 'size' in style['maps'] and style['maps']['size'].source_type=='OBJECTCOLOR' and map_pieces:
            maximum=float(np.max(map_coordinates[:,4:8,:3]@np.array((.2126,.7152,.0722))))
            style['params'][3,2]*=max(1.,1-brush.size_map_amount+brush.size_map_amount*maximum)
        if style['params'][3,2]*brush.thickness*backend.width_scale(scene,node)>512:raise ValueError('GPU: 효과 적용 굵기가 512px를 넘으면 Native를 사용하세요')
        groups.append({'records': records, 'extra_normals':extra, 'texture': None, 'brush': brush,
                       'style':style,'map_coordinates':map_coordinates,
                       'chain_vertices':np.concatenate(chain_vertices) if chain_vertices else np.zeros((0,3),np.float32),
                       'chain_endpoints':np.concatenate(chain_ends) if chain_ends else np.zeros((0,2),np.int32),
                       'width': brush.thickness*backend.width_scale(scene, node),
                       'size_reduction': _channel(brush, 'SIZE'), 'alpha_reduction': _channel(brush, 'ALPHA')})
    for group in groups:
        for obj in group['style']['references']:source_ids.add(obj.as_pointer())
        for n in group['style']['maps'].values():
            if getattr(n,'image',None):source_ids.add(n.image.as_pointer())
    depth_data=gpu_depth.snapshot(dg) if needs_depth else None
    return {'groups': groups, 'source_ids': source_ids,'depth':depth_data,
        'edges': sum(len(g['records']) for g in groups), 'build_ms': (time.perf_counter()-start)*1000,
        'draw_ms': 0., 'draws': 0, 'uploads': 0, 'dirty': False, 'error': ''}


def sync(scene):
    global _handle, _building
    if bpy.app.background:
        raise ValueError('GPU 미리보기는 Blender 창에서 사용하세요')
    from . import backend,gpu_styles,gpu_depth
    from . import free_view
    followed=free_view._entries.get(scene.as_pointer())
    if followed:free_view._restore_navigation(followed)
    _building = True
    try:
        window=next((w for w in bpy.context.window_manager.windows if w.scene==scene),None)
        if window is None:raise ValueError('GPU 미리보기 장면을 Blender 창에서 여세요')
        with bpy.context.temp_override(window=window):dg = bpy.context.evaluated_depsgraph_get()
        _states[scene.as_pointer()] = snapshot(scene, dg)
        if _handle is None:_handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_VIEW')
        if not bpy.app.timers.is_registered(_watch):bpy.app.timers.register(_watch, first_interval=.2)
        from . import curves,preview_runtime
        curves.mark_synced(scene);preview_runtime.mark_synced(scene)
    finally:_building = False
    _tag_redraw(scene)
    return ['GPU 시험 미리보기: Solid/재질/Rendered · 곡선/점선/맵/숨은 선. 분기점은 선을 나누며 루프 시작·위상·연결부·AA는 Native와 다릅니다']


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
    edgeIndex=float(edge);faceChoice=(f1>=0.0&&f0<0.0)?1.0:0.0;clipInterval=vec2(0,1);screenEnds=vec4(0);chainSpan=vec3(0);
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
    if (pa<=0.0) { float t=pa/(pa-qa); p=mix(p,q,t); wa=mix(wa,wb,t);clipInterval.x=t; }
    else if (qa<=0.0) { float t=qa/(qa-pa); q=mix(q,p,t); wb=mix(wb,wa,t);clipInterval.y=1.0-t; }
    vec2 sa=p.xy/p.w*viewportSize*0.5, sb=q.xy/q.w*viewportSize*0.5;
    vec2 delta=sb-sa; float lengthValue=length(delta);
    vec2 tangent=delta/max(lengthValue,0.00001), normal=vec2(-tangent.y,tangent.x);
    vec3 span=wb-wa;vec4 profiles=texelFetch(styleData,ivec2(14,0),0);
    vec3 reference=profiles.z>.5?texelFetch(styleData,ivec2(15,0),0).xyz:eyePosition;
    float closest=clamp(dot(reference-wa,span)/max(dot(span,span),1e-20),0.0,1.0);
    float distanceValue=length(reference-mix(wa,wb,closest));
    float boundReduction=profiles.x>.5?1.0:reduction(sizeReduction,distanceValue);
    float halfWidth=max(0.0,lineWidth*boundReduction*texelFetch(styleData,ivec2(3,0),0).z*0.5);
    float extent=halfWidth+1.0;
    vec2 offset=tangent*mix(-extent,extent,corner.x)+normal*corner.y*extent;
    vec4 pos=mix(p,q,corner.x); pos.xy+=offset*2.0/viewportSize*pos.w;
    pos.z-=0.000002*pos.w;
    gl_Position=pos;
    lineCoord=vec2(mix(-extent,lengthValue+extent,corner.x),corner.y*extent);
    segmentLength=lengthValue;worldA=wa;worldB=wb;inverseW=vec2(1.0/p.w,1.0/q.w);
    screenEnds=vec4(sa+viewportSize*.5,sb+viewportSize*.5);
    if(texelFetch(styleData,ivec2(3,0),0).w>.5){int w=textureSize(chainData,0).x;chainSpan=texelFetch(chainData,ivec2(edge%w,edge/w),0).xyz;}
}
'''
FRAGMENT = r'''
float reduction(vec4 spec,float distanceValue) {
    return mix(1.0,mix(1.0,spec.z,clamp((distanceValue-spec.x)/max(0.000001,spec.y-spec.x),0.0,1.0)),spec.w);
}
float centerDepth(vec2 center) {
    // Depth texels are located at pixel centers. Interpolate at the actual
    // projected centerline, rather than shifting it to the next half pixel.
    // Nearest sampling can classify an edge as behind its own sloping face.
    vec2 p=center-vec2(.5), f=fract(p);ivec2 a=ivec2(floor(p));
    ivec2 last=textureSize(sceneDepth,0)-1;
    float d00=texelFetch(sceneDepth,clamp(a,ivec2(0),last),0).r;
    float d10=texelFetch(sceneDepth,clamp(a+ivec2(1,0),ivec2(0),last),0).r;
    float d01=texelFetch(sceneDepth,clamp(a+ivec2(0,1),ivec2(0),last),0).r;
    float d11=texelFetch(sceneDepth,clamp(a+ivec2(1,1),ivec2(0),last),0).r;
    return mix(mix(d00,d10,f.x),mix(d01,d11,f.x),f.y);
}
void main() {
    float t=clamp(lineCoord.x/max(segmentLength,0.00001),0.0,1.0);
    float u=t*inverseW.y/max(mix(inverseW.x,inverseW.y,t),1e-20);
    vec3 world=mix(worldA,worldB,u);float cameraDistance=length(eyePosition-world);
    vec4 profiles=styleValue(14);
    float sizeDistance=profiles.z>.5?length(styleValue(15).xyz-world):cameraDistance;
    float alphaDistance=profiles.w>.5?length(styleValue(16).xyz-world):cameraDistance;
    float distanceSize=profiles.x>.5&&sizeReduction.w>.5?curveValue(clamp((sizeDistance-sizeReduction.x)/max(.000001,sizeReduction.y-sizeReduction.x),0.0,1.0),2):reduction(sizeReduction,sizeDistance);
    float distanceAlpha=profiles.y>.5&&alphaReduction.w>.5?curveValue(clamp((alphaDistance-alphaReduction.x)/max(.000001,alphaReduction.y-alphaReduction.x),0.0,1.0),3):reduction(alphaReduction,alphaDistance);
    vec4 curveOptions=styleValue(3),dashOptions=styleValue(4),modes=styleValue(5);
    float along=curveOptions.w>.5?mix(chainSpan.x,chainSpan.y,t):clamp(lineCoord.x,0.0,segmentLength);
    float parameter=curveOptions.w>.5?along/max(chainSpan.z,.00001):t;
    float sizeCurve=curveOptions.x>.5?curveValue(parameter,0):1.0;
    float alphaCurve=curveOptions.y>.5?curveValue(parameter,1):1.0;
    float originalU=mix(clipInterval.x,clipInterval.y,u);
    vec4 colorMap=mapSample(colorImage,8,0,originalU),sizeMap=mapSample(sizeImage,11,4,originalU);
    float mapSize=styleValue(11).x>.5?mix(1.0,dot(sizeMap.rgb,vec3(.2126,.7152,.0722))*sizeMap.a,modes.w):1.0;
    float radius=max(0.0,lineWidth*distanceSize*sizeCurve*mapSize*0.5);
    float opacity=inkColor.a*distanceAlpha*alphaCurve;
    vec3 color=styleValue(8).x>.5?mix(inkColor.rgb,colorMap.rgb,modes.z*colorMap.a):inkColor.rgb;
    if(modes.x>.5){
        vec2 center=mix(screenEnds.xy,screenEnds.zw,t);
        float scene=centerDepth(center);
        vec4 clip=viewProjection*vec4(mix(worldA,worldB,u),1);float depth=clip.z/clip.w*.5+.5;
        bool hidden=depth>scene+.000002;
        if((modes.x>1.5)!=hidden)discard;
    }
    float outside=max(max(-lineCoord.x,lineCoord.x-segmentLength),0.0);
    if(dashOptions.x>.5&&dashOptions.z>0.0){
        float period=max(.00001,dashOptions.y+dashOptions.z),phase=mod(along+dashOptions.w,period);
        if(phase>dashOptions.y)outside=max(outside,min(phase-dashOptions.y,period-phase));
    }
    float distanceValue=length(vec2(outside,lineCoord.y));
    float coverage=clamp(radius+0.5-distanceValue,0.0,1.0);
    if (coverage<=0.0 || radius<=0.0 || opacity<=0.0) discard;
    fragColor=vec4(color,opacity*coverage);
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
    interface.flat('FLOAT','edgeIndex');interface.flat('FLOAT','faceChoice');interface.flat('VEC2','clipInterval')
    interface.flat('VEC4','screenEnds');interface.flat('VEC3','chainSpan')
    info.vertex_out(interface)
    info.sampler(0, 'FLOAT_2D', 'edgeData')
    info.sampler(1, 'FLOAT_2D', 'extraNormals')
    info.sampler(2, 'FLOAT_2D', 'styleData')
    for slot,name in enumerate(('curveData','chainData','colorImage','sizeImage','coordinateData','sceneDepth'),3):info.sampler(slot,'FLOAT_2D',name)
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
    for kind,name in (('MAT4','viewProjection'),('VEC4','viewportInfo'),('VEC4','eyeInfo'),('VEC4','directionInfo'),('VEC4','viewExtra')):
        info.push_constant(kind,name)
    info.fragment_out(0, 'VEC4', 'fragColor')
    from . import gpu_styles
    info.vertex_source(VERTEX); info.fragment_source(gpu_styles.GLSL+FRAGMENT)
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
    from . import gpu_styles
    gpu_styles.upload(group)


def _draw():
    context = bpy.context; scene = context.scene
    if scene is None or not getattr(scene, 'lea_line', None) or not scene.lea_line.preview_enabled:return
    entry = _states.get(scene.as_pointer())
    if not entry or entry['error'] or bpy.app.is_job_running('RENDER'):return
    shading=context.space_data.shading
    if shading.type not in {'SOLID','MATERIAL','RENDERED'}:return
    if shading.type=='RENDERED' and scene.render.engine not in {'BLENDER_EEVEE_NEXT','CYCLES'}:return
    if shading.type=='SOLID' and shading.show_xray:return
    # Always use the current region's matrices, including during navigation,
    # after leaving camera view, and in additional View3D areas. No helper is
    # needed: the scene camera remains solely a final-render setting.
    import gpu
    start=time.perf_counter()
    depth, mask, blend = gpu.state.depth_test_get(), gpu.state.depth_mask_get(), gpu.state.blend_get()
    try:
        _resources()
        view=context.region_data; inverse=view.view_matrix.inverted()
        from . import gpu_depth,gpu_chain
        depth_texture=gpu_depth.draw(entry['depth'],view,(context.region.width,context.region.height),context.area.as_pointer()) if entry.get('depth') else None
        if depth_texture is None:
            if 'dummy_depth' not in entry:entry['dummy_depth']=gpu.types.GPUTexture((1,1),format='R32F',data=gpu.types.Buffer('FLOAT',1,[1.]))
            depth_texture=entry['dummy_depth']
        gpu.state.depth_test_set('NONE' if entry.get('depth') else 'LESS_EQUAL'); gpu.state.depth_mask_set(False); gpu.state.blend_set('ALPHA')
        _shader.bind()
        direction=inverse.to_3x3() @ __import__('mathutils').Vector((0,0,1))
        _shader.uniform_float('viewProjection',view.perspective_matrix)
        _shader.uniform_float('eyeInfo',(*inverse.translation,0.))
        _shader.uniform_float('directionInfo',(*direction,0.))
        _shader.uniform_float('viewExtra',(*gpu.state.viewport_get()[:2],0.,0.))
        _shader.uniform_sampler('sceneDepth',depth_texture)
        for group in entry['groups']:
            if not len(group['records']):continue
            if group['texture'] is None:_texture(group);entry['uploads']+=1
            if group['style']['needs_chain']:
                gpu_chain.update(group,view.perspective_matrix,(context.region.width,context.region.height,group['width'],int(view.is_perspective)),(*inverse.translation,0.),(*direction,0.),context.area.as_pointer())
                _shader.bind()
            brush=group['brush']
            _shader.uniform_sampler('edgeData',group['texture'])
            _shader.uniform_sampler('extraNormals',group['normal_texture'])
            _shader.uniform_sampler('styleData',group['style_texture'])
            for name,key in (('curveData','curve_texture'),('chainData','chain_texture'),('colorImage','color_texture'),('sizeImage','size_texture'),('coordinateData','coordinate_texture')):_shader.uniform_sampler(name,group[key])
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
        from . import gpu_chain
        gpu_chain.reset()
    _tag_redraw(scene)


@persistent
def _changed(scene, depsgraph):
    if _building:return
    entry=_states.get(scene.as_pointer())
    if entry and any(isinstance(update.id,(bpy.types.Object,bpy.types.Mesh,bpy.types.Image)) and
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
    from . import gpu_chain
    gpu_chain.reset()


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
