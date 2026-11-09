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
from .gpu_chain import EDGE_SELECTION
from .gpu_depth import CENTER_DEPTH, MARKED_DEPTH, SPAN_LENGTH

_states = {}
_handle = None
_shader = None
_batch = None
_building = False
MAX_STROKE_WIDTH = 512


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
    for node, common in backend._specifications(scene.lea_line.tree, with_output=True):
        if material_scope.active(node) or node.use_material_functions:
            raise ValueError('GPU: 재질별 얼굴 선택·Line Functions는 Native를 사용하세요')
        # Resolve identities without creating/linking helper collections inside
        # a render callback. Render depsgraph objects are evaluated copies.
        scope = ({obj.original for obj in node.source_collection.all_objects}
                 if node.source_collection else None)
        listed = getattr(node, 'objects', ())
        if len(listed):
            # An instance source may be absent from scene.objects. The actual
            # evaluated occurrences are filtered when building the snapshot.
            targets = {item.object.original for item in listed if item.object}
            if scope is not None:
                targets.intersection_update(scope)
        elif getattr(node, 'target_mode', 'ALL') == 'ALL' or node.source_collection:
            targets = scope
        else:
            targets = set()
        for visibility,enabled in (('VISIBLE',node.visible),('HIDDEN',node.hidden)):
            if not enabled:continue
            if any(routing.feature_enabled(node,k,visibility) for k in ('intersection','ridge_valley','suggestive_contour')):
                raise ValueError('GPU: 교차선·곡면 특수선은 Native를 사용하세요')
            features = ('outline', 'border', 'crease', 'edge_mark', 'material_boundary') + routing.GPU_ONLY_EDGE_FEATURES
            object_enabled = routing.feature_enabled(node, 'object_edge', visibility)
            outline_enabled = routing.feature_enabled(node, 'outline', visibility)
            merge_groups = routing.feature_enabled(node, 'outline_merge_groups', visibility)
            object_open = routing.feature_enabled(node, 'object_open', visibility)
            outline_open = routing.feature_enabled(node, 'outline_open', visibility)
            outline_owns_border = outline_open and outline_enabled
            brush, deferred = routing.live_brush(node, common, visibility, gpu=True)
            if deferred or object_enabled or outline_enabled:
                routes = routing.gpu_edge_brushes(node, brush, visibility, features,
                                                 allow_border=object_enabled or outline_enabled or merge_groups)
                if not routes:
                    raise ValueError('GPU: 전용 브러시가 연결된 Line Set은 해당 선 종류만 켜세요')
            else:
                routes = [(None, brush, ())]
            for feature, brush, exclude in routes:
                # The Outline brush owns exterior contours even without an
                # Object brush or a merged collection.
                object_mode = -1 if feature == 'outline' else 0
                if feature == 'crease' and outline_enabled:
                    # Resolve a rear crease that projects onto its own front
                    # contour without changing other Normal Angle edges.
                    object_mode = 2
                if feature == 'border' and outline_owns_border and not object_enabled:
                    continue
                if object_enabled:
                    if feature == 'object_edge':
                        object_mode = 1
                    elif feature == 'border':
                        if outline_owns_border and object_open:
                            continue
                        object_mode = 1 if outline_owns_border else -1 if object_open else 0
                    if feature in {'object_edge', 'border'}:
                        exclude = tuple(key for key in exclude if key != 'outline')
                if stroke_variation.snapshot(brush) is not None:
                    raise ValueError('GPU: 랜덤 브러시는 Native를 사용하세요')
                gpu_styles.validate(brush)
                if getattr(brush,'brush_type','SIMPLE')=='MULTIPLE' or getattr(brush,'brush_map_on',False):raise ValueError('GPU: 다중 스탬프·브러시 맵은 Native를 사용하세요')
                if getattr(brush,'distortion_map_on',False):raise ValueError('GPU: 왜곡 맵은 Native를 사용하세요')
                if getattr(brush,'caps','ROUND')!='ROUND' or getattr(brush,'hidden_mask_objects',()):raise ValueError('GPU: Round 캡만 지원하며 숨은 선 마스크는 Native를 사용하세요')
                if (abs(getattr(brush,'stretch',0.))>1e-6 or getattr(brush,'groove',0.) or abs(getattr(brush,'stamp_reduction_start',1.)-1.)>1e-6 or abs(getattr(brush,'stamp_reduction_end',1.)-1.)>1e-6):raise ValueError('GPU: 스탬프 변형은 Native를 사용하세요')
                proxy = routing.ResolvedBrush(node, {
                    key: key == feature if feature else routing.feature_enabled(node, key, visibility)
                    for key in features})
                if not (outline_enabled or object_enabled) and (
                    proxy.crease and not any(getattr(proxy, key) for key in features if key != 'crease')
                ):
                    # Normal Angle alone still reserves the front silhouette
                    # and needs to classify the coincident rear crease.
                    object_mode = 3
                proxy.overrides['_gpu_exclude'] = exclude
                normal_offset = bool(getattr(brush,'normal_offset',0))
                if normal_offset and (object_mode or not proxy.edge_mark or
                                      any(getattr(proxy,k) for k in features if k!='edge_mark')):
                    raise ValueError('GPU: Normal Offset은 단독 Marked Edge를 지원합니다')
                proxy.overrides['_gpu_normal_offset'] = normal_offset
                distortion = bool(gpu_styles.wave_amount(brush))
                if distortion and (object_mode or not proxy.edge_mark or
                                   any(getattr(proxy,k) for k in features if k!='edge_mark')):
                    raise ValueError('GPU: 파형 왜곡은 단독 Marked Edge를 지원합니다')
                proxy.overrides['_gpu_distortion'] = distortion
                proxy.overrides['_gpu_hidden'] = visibility == 'HIDDEN'
                proxy.overrides['outline_open'] = outline_open
                proxy.overrides['object_open'] = object_open
                proxy.overrides['_gpu_object_mode'] = object_mode
                # Group coverage affects Outline ownership. A standalone
                # Normal Angle stroke still tests its own surface.
                proxy.overrides['_gpu_merge_mode'] = object_mode if merge_groups and object_mode != 3 else 0
                if visibility == 'HIDDEN' and node.independent_hidden_edges:
                    proxy.overrides['normal_angle_min'] = node.hidden_normal_angle_min
                    proxy.overrides['normal_angle_max'] = node.hidden_normal_angle_max
                jobs.append((proxy, brush, targets))
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
    outline_open = getattr(node, 'outline_open', True)
    object_edge = getattr(node, 'object_edge', False)
    object_mode = getattr(node, '_gpu_object_mode', 0)
    fixed = border & bool(node.border or (node.outline and outline_open) or
                          (object_edge and node.object_open))
    wireframe = getattr(node, 'wireframe', False)
    smoothing_boundary = getattr(node, 'smoothing_boundary', False)
    if wireframe:
        wire_edges = counts >= 2
        attribute = mesh.attributes.get('pencil_wire_edge')
        if attribute is not None and attribute.domain == 'EDGE' and attribute.data_type == 'BOOLEAN':
            selected = np.empty(ne, dtype=bool)
            attribute.data.foreach_get('value', selected)
            wire_edges &= selected
        fixed |= wire_edges
    exclude = getattr(node, '_gpu_exclude', ())
    if smoothing_boundary or 'smoothing_boundary' in exclude:
        smooth = np.empty(npoly, dtype=bool)
        mesh.polygons.foreach_get('use_smooth', smooth)
        sharp = np.empty(ne, dtype=bool)
        mesh.edges.foreach_get('use_edge_sharp', sharp)
        np.logical_or.at(sharp, loop_edges, ~smooth[faces])
        smoothing = (counts >= 2) & sharp
        if smoothing_boundary:
            fixed |= smoothing
    test_crease = node.crease or 'crease' in exclude
    if test_crease:
        min_angle, max_angle = node.normal_angle_min, node.normal_angle_max
        # RNA angles and transformed normals are float32. Allow their rounding
        # error when a face pair lies exactly on either authored endpoint.
        cosine_min = math.cos(max_angle) - 2e-7
        cosine_max = math.cos(min_angle) + 2e-7
        crease_range = min_angle <= max_angle + 2e-7
        crease = (counts == 2) & (dot >= cosine_min) & (dot <= cosine_max) & crease_range
    test_material = node.material_boundary or 'material_boundary' in exclude
    if test_material:
        materials = np.empty(npoly, np.int32)
        mesh.polygons.foreach_get('material_index', materials)
        material_boundary = (counts == 2) & (materials[first] != materials[last])
    if node.edge_mark or 'edge_mark' in exclude:
        marks = np.empty(ne, bool); mesh.edges.foreach_get('use_freestyle_mark', marks)
        marks &= ~border
        if node.edge_mark:
            fixed |= marks
    outline_has_priority = 'outline' in exclude
    # Pencil+ reserves shared silhouette edges for Outline, including when
    # Outline is disabled.
    internal_edges = wireframe or smoothing_boundary or node.edge_mark or node.crease or node.material_boundary
    suppress_outline = outline_has_priority or (not node.outline and internal_edges)
    test_outline = bool(node.outline or object_edge or suppress_outline)
    silhouette = (counts == 2) & test_outline & (dot < 1.-1e-7)
    # Store ALL incident normals for non-manifold edges. A single GPU instance
    # ORs their facing tests, avoiding duplicated strokes/alpha accumulation.
    extra=[];extra_start=np.zeros(ne,np.float32);extra_count=np.zeros(ne,np.float32)
    nonmanifold=np.flatnonzero(counts>2)
    if len(nonmanifold):
        order=np.argsort(loop_edges,kind='stable'); offsets=np.concatenate(([0],np.cumsum(counts)))
        for edge in nonmanifold:
            incident=np.unique(faces[order[offsets[edge]:offsets[edge+1]]])
            ns=normals[incident]
            if test_crease and crease_range:
                left, right = np.triu_indices(len(ns), k=1)
                pairs = (ns[left] * ns[right]).sum(axis=1)
                crease[edge] = np.any((pairs >= cosine_min) & (pairs <= cosine_max))
            if test_material:
                material_boundary[edge] = np.any(materials[incident] != materials[incident[0]])
            silhouette[edge]=test_outline and not object_edge and np.min(ns@ns.T)<1.-1e-7
            extra_start[edge]=len(extra);extra_count[edge]=len(ns)
            extra.extend((*normal,0.) for normal in ns)
    if node.crease:
        fixed |= crease
    if node.material_boundary:
        fixed |= material_boundary
    if 'smoothing_boundary' in exclude:
        fixed &= ~smoothing
    if 'material_boundary' in exclude:
        fixed &= ~material_boundary
    if 'edge_mark' in exclude:
        fixed &= ~marks
    if 'crease' in exclude:
        fixed &= ~crease
    if outline_has_priority and outline_open:
        fixed &= ~border
    if suppress_outline:
        silhouette &= fixed
    chosen = valid & (fixed | silhouette)
    endpoints = edge_vertices.reshape(-1, 2)[chosen]
    world = vertices.reshape(-1, 3) @ transform[:3, :3].T + transform[:3, 3]
    result = np.zeros((len(endpoints), 4, 4), np.float32)
    result[:, 0, :3] = world[endpoints[:, 0]]; result[:, 0, 3] = fixed[chosen]
    result[:, 1, :3] = world[endpoints[:, 1]]
    result[:, 1, 3] = np.where(silhouette[chosen], -1. if suppress_outline else 1., 0.)
    result[:, 2, :3] = n0[chosen]; result[:, 3, :3] = n1[chosen]
    result[:, 2, 3]=extra_start[chosen];result[:, 3, 3]=extra_count[chosen]
    normal_offset = getattr(node, '_gpu_normal_offset', False)
    distortion = getattr(node, '_gpu_distortion', False)
    if object_mode or normal_offset or distortion:
        # For manifold edges the otherwise unused metadata stores the polygon
        # winding relative to the directed edge, one sign per incident face.
        loop_vertices = np.empty(nl, np.int32)
        mesh.loops.foreach_get('vertex_index', loop_vertices)
        loop_ids = np.arange(nl, dtype=np.int32)
        first_loop = np.full(ne, nl, np.int32)
        last_loop = np.full(ne, -1, np.int32)
        np.minimum.at(first_loop, loop_edges, loop_ids)
        np.maximum.at(last_loop, loop_edges, loop_ids)
        manifold = counts[chosen] <= 2
        edge_mask = chosen & (counts <= 2)
        endpoint = endpoints[manifold, 0]
        handedness = np.sign(np.linalg.det(transform[:3, :3]))
        result[manifold, 2, 3] = np.where(loop_vertices[first_loop[edge_mask]] == endpoint, handedness, -handedness)
        result[manifold, 3, 3] = np.where(loop_vertices[last_loop[edge_mask]] == endpoint, handedness, -handedness)
        if distortion:
            if not np.all(counts[chosen] == 2) or np.any(np.linalg.norm(n1[chosen]-n0[chosen], axis=1) > 1e-6):
                raise ValueError('GPU: 파형 왜곡은 같은 평면의 두 면이 만나는 Marked Edge를 지원합니다')
            degree = np.bincount(endpoints.ravel(), minlength=nv)
            rays = world[endpoints[:, 1]] - world[endpoints[:, 0]]
            rays /= np.maximum(np.linalg.norm(rays, axis=1, keepdims=True), 1e-12)
            ray_sum = np.zeros_like(world)
            winding_sum = np.zeros(nv, np.float32)
            np.add.at(ray_sum, endpoints[:, 0], rays)
            np.add.at(ray_sum, endpoints[:, 1], -rays)
            np.add.at(winding_sum, endpoints[:, 0], result[:, 2, 3])
            np.add.at(winding_sum, endpoints[:, 1], -result[:, 2, 3])
            joined = degree == 2
            if (np.any(degree > 2) or np.any(winding_sum[joined] != 0) or
                    np.any(np.linalg.norm(ray_sum[joined], axis=1) > 1e-5)):
                raise ValueError('GPU: 꺾임·분기·면 방향 변화가 있는 파형 왜곡은 Native를 사용하세요')
        if normal_offset:
            if not np.all(manifold):
                raise ValueError('GPU: 비매니폴드 모서리의 Normal Offset은 Native를 사용하세요')
            degree = np.bincount(endpoints.ravel(), minlength=nv)
            balance = np.zeros(nv, dtype=np.float32)
            np.add.at(balance, endpoints[:, 0], result[:, 2, 3])
            np.add.at(balance, endpoints[:, 1], -result[:, 2, 3])
            linked = np.any(degree[endpoints] == 2, axis=1)
            different_normals = np.linalg.norm(n1[chosen]-n0[chosen], axis=1) > 1e-6
            # Even consistent first-face winding can change when the view
            # favors the second face on part of the chain.
            payload['mixed_normal_directions'] = bool(np.any((degree == 2) & (balance != 0)) or
                                                       np.any(linked & different_normals))
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


def _stroke_group(brush, width, style, parts, object_depth=None):
    records, normals, maps, vertices, endpoints = parts
    if len(records) > 2_000_000:
        raise ValueError('GPU 시험 경로의 모서리 메모리 한도를 넘었습니다')
    if 'size' in style['maps'] and style['maps']['size'].source_type == 'OBJECTCOLOR' and len(records):
        maximum = float(np.max(maps[:, 4:8, :3] @ np.array((.2126, .7152, .0722))))
        style = dict(style, params=style['params'].copy())
        style['params'][3, 2] *= max(1., 1-brush.size_map_amount+brush.size_map_amount*maximum)
    if style['params'][3, 2] * width > MAX_STROKE_WIDTH:
        raise ValueError(f'GPU: 효과 적용 굵기가 {MAX_STROKE_WIDTH}px를 넘으면 Native를 사용하세요')
    return {'records': records, 'extra_normals': normals, 'texture': None, 'brush': brush,
            'style': style, 'map_coordinates': maps, 'chain_vertices': vertices, 'chain_endpoints': endpoints,
            'object_depth': object_depth, 'width': width,
            'size_reduction': _channel(brush, 'SIZE'), 'alpha_reduction': _channel(brush, 'ALPHA')}


def snapshot(scene, dg):
    """Copy evaluated geometry without installing viewport handlers or timers.

    A render engine passes its RENDER depsgraph; viewport sync passes VIEWPORT.
    All Blender data is copied synchronously in the calling Blender callback.
    """
    from . import backend, gpu_styles, gpu_depth, routing, width_profile
    start = time.perf_counter()
    jobs = _jobs(scene)
    groups = []
    source_ids = set()
    object_depths = {}
    object_areas = {}
    members = (gpu_depth.merge_members(scene.collection)
               if any(node._gpu_merge_mode for node, _, _ in jobs) else {})
    # The viewport depth buffer can reject the expanded stroke quad against
    # its own sloping surface. Classify the centerline against our unjittered
    # mesh depth in every shading mode, then draw the whole stroke as overlay.
    needs_depth=True
    depth_data = gpu_depth.snapshot(dg, members)
    merge_depths = depth_data.pop('merge_depths')
    for merged in merge_depths.values():
        merged['areas'] = object_areas
    for node, brush, targets in jobs:
        pieces = []; extra_pieces=[];extra_offset=0
        map_pieces=[];chain_vertices=[];chain_ends=[];vertex_offset=0
        scale = backend.width_scale(scene, node)
        width = brush.thickness * scale
        object_mode = getattr(node, '_gpu_object_mode', 0)
        merge_mode = node._gpu_merge_mode
        object_records = 0
        style=gpu_styles.snapshot(brush,scale,getattr(node,'_gpu_hidden',False),needs_depth,
                                  output=getattr(node,'line_output',None))
        profile = scene.lea_line.stroke_width_profile
        if (profile == 'PENCIL_SIMPLE' and getattr(brush, 'brush_type', 'SIMPLE') == 'SIMPLE'
                and width_profile.live_coverage_supported(brush) and 'size' not in style['maps']
                and not any(spec['enabled'] for spec in getattr(brush, 'distance_channels', {}).values())):
            width = width_profile.coverage_width(width, profile)
            style['params'][19, 0] = 1
        style['params'][6, 2] = object_mode
        merged_style = style
        merged_node = node
        if merge_mode:
            merged_style = dict(style, params=style['params'].copy())
            merged_style['params'][6, 2] = merge_mode
            merged_style['params'][6, 3] = 1
            if merge_mode != object_mode:
                merged_node = routing.ResolvedBrush(node, {'_gpu_object_mode': merge_mode})
        for instance in dg.object_instances:
            obj = instance.object
            parent=instance.parent.original if instance.is_instance and instance.parent else None
            if obj.type != 'MESH' or not instance.show_self:
                continue
            if targets is not None and obj.original not in targets and parent not in targets:
                continue
            if dg.mode != 'RENDER' and not instance.is_instance and not obj.original.visible_get(view_layer=dg.view_layer):
                continue
            mesh = obj.to_mesh()
            try:
                merged = merge_depths.get(members.get(obj.original)) if merge_mode else None
                payload={};records,extra=_records(mesh, instance.matrix_world, merged_node if merged is not None else node,payload)
                if payload.get('mixed_normal_directions'):
                    style['params'][17,3] = 1
                if len(records):
                    mapped=gpu_styles.mesh_maps(mesh,payload,style['maps'])
                    vertices = np.zeros((0, 3), np.float32)
                    ends = np.zeros((0, 2), np.int32)
                    if style['needs_chain']:
                        used,inverse=np.unique(payload['endpoints'],return_inverse=True)
                        vertices = payload['world_vertices'][used]
                        ends = inverse.reshape(-1,2)
                    if object_mode or merged is not None:
                        object_records += len(records)
                        if object_records > 2_000_000:
                            raise ValueError('GPU 시험 경로의 모서리 메모리 한도를 넘었습니다')
                        if merged is None:
                            # Blender can reuse the evaluated instance wrapper
                            # while iterating. Cache by stable source identity.
                            key = (obj.original.as_pointer(), tuple(tuple(row) for row in instance.matrix_world))
                            if key not in object_depths:
                                object_depths[key] = {'vertices': payload['world_vertices'],
                                                      'triangles': gpu_depth.mesh_triangles(mesh),
                                                      'areas': object_areas}
                            depth = object_depths[key]
                        else:
                            depth = merged
                        parts = (records, extra, mapped if mapped is not None else np.zeros((1,4),np.float32),
                                 vertices, ends)
                        groups.append(_stroke_group(brush, width, merged_style if merged is not None else style, parts, depth))
                    else:
                        if mapped is not None:map_pieces.append(mapped)
                        if style['needs_chain']:
                            chain_vertices.append(vertices);chain_ends.append(ends+vertex_offset);vertex_offset+=len(vertices)
                        records[records[:,3,3]>2,2,3]+=extra_offset
                        pieces.append(records);extra_pieces.append(extra);extra_offset+=len(extra)
                source_ids.add(obj.original.as_pointer())
                if parent:source_ids.add(parent.as_pointer())
                if obj.original.data:source_ids.add(obj.original.data.as_pointer())
            finally:obj.to_mesh_clear()
        if object_mode:
            continue
        records = np.concatenate(pieces) if pieces else np.empty((0,4,4), np.float32)
        extra=np.concatenate(extra_pieces) if extra_pieces else np.empty((0,4),np.float32)
        map_coordinates=np.concatenate(map_pieces) if map_pieces else np.zeros((1,4),np.float32)
        parts = (records, extra, map_coordinates,
                 np.concatenate(chain_vertices) if chain_vertices else np.zeros((0,3),np.float32),
                 np.concatenate(chain_ends) if chain_ends else np.zeros((0,2),np.int32))
        groups.append(_stroke_group(brush, width, style, parts))
    for group in groups:
        for obj in group['style']['references']:source_ids.add(obj.as_pointer())
        for n in group['style']['maps'].values():
            if getattr(n,'image',None):source_ids.add(n.image.as_pointer())
    return {'groups': groups, 'source_ids': source_ids,'depth':depth_data,'object_areas':object_areas,
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


JOINT_DEPTH = CENTER_DEPTH + MARKED_DEPTH + SPAN_LENGTH + r'''
float sampleDepth(sampler2D depthImage, vec2 pixel) {
    ivec2 coordinate=ivec2(floor(pixel));
    if (any(lessThan(coordinate,ivec2(0))) || any(greaterThanEqual(coordinate,textureSize(depthImage,0)))) return 1.0;
    return texelFetch(depthImage,coordinate,0).r;
}
bool jointAxis(vec2 center,vec4 points) {
    vec2 x=vec2(2,0), y=vec2(0,2);
    float dx=abs(sampleDepth(sceneDepth,center+x)-sampleDepth(sceneDepth,center-x));
    float dy=abs(sampleDepth(sceneDepth,center+y)-sampleDepth(sceneDepth,center-y));
    vec2 span=abs(points.zw-points.xy);
    return abs(dx-dy)>.000002?dx>dy:span.x>=span.y;
}
float jointProbe(vec4 points,bool atStart,bool xAxis) {
    vec2 delta=points.zw-points.xy;
    float axis=xAxis?delta.x:delta.y;
    float inward=atStart?axis:-axis;
    // Half a pixel across the depth boundary separates a shared cut from a
    // real run on the next edge. At a texel tie use the lower pixel on both
    // sides so the two probes stay one pixel apart.
    float inset=min(.5,min((.5-sign(inward)*.00025)/max(abs(axis),.00001),
                          1.5/max(length(delta),.00001)));
    return atStart?inset:1.0-inset;
}
bool jointSample(vec4 points,vec2 depths,float t,bool xAxis,bool hidden,vec4 planes) {
    vec2 center=mix(points.xy,points.zw,t);
    if (any(lessThan(center,vec2(0))) || any(greaterThanEqual(center,vec2(textureSize(sceneDepth,0))))) return false;
    vec2 delta=points.zw-points.xy;
    float axis=xAxis?center.x:center.y, stepValue=xAxis?delta.x:delta.y;
    if (abs(stepValue)<.00001) return spanSample(points,depths,t,hidden,planes);
    float sampleT=t+(floor(axis)+.5-axis)/stepValue;
    if (abs(sampleT-t)*length(delta)>1.5) return spanSample(points,depths,t,hidden,planes);
    float depth=mix(depths.x,depths.y,sampleT);
    return (depth>markedCenterDepth(mix(points.xy,points.zw,sampleT),delta,depth,planes)+.000002)==hidden;
}
bool jointSample(vec4 points,vec2 depths,float t,bool xAxis,bool hidden) {
    return jointSample(points,depths,t,xAxis,hidden,vec4(0));
}
'''


VERTEX = r'''
vec4 record(int edge, int field) {
    int index=edge*4+field; int width=textureSize(edgeData,0).x;
    return texelFetch(edgeData,ivec2(index%width,index/width),0);
}
''' + EDGE_SELECTION + JOINT_DEPTH + r'''
float reduction(vec4 spec,float distanceValue) {
    return mix(1.0,mix(1.0,spec.z,clamp((distanceValue-spec.x)/max(0.000001,spec.y-spec.x),0.0,1.0)),spec.w);
}
vec2 normalShift(int edge,vec4 p,vec4 q,vec4 points,vec2 normal,float side,bool atStart,vec4 planes) {
    int width=textureSize(jointData,0).x;
    vec2 links=texelFetch(jointData,ivec2(edge%width,edge/width),0).xy;
    int link=int(atStart?links.x:links.y);
    if (link<0) return normal;
    vec4 joint=atStart?p:q;
    vec3 farWorld=record(link/2,1-link%2).xyz;
    vec4 far=viewProjection*vec4(farWorld,1);
    float j=joint.z+joint.w, f=far.z+far.w;
    if (f<=0.0) far=mix(joint,far,j/(j-f));
    vec2 start=atStart?points.xy:points.zw;
    vec2 end=(far.xy/far.w*.5+.5)*viewportSize;
    if (viewExtra.z>0.0) end-=viewExtra.xy;
    vec2 delta=end-start;
    if (length(delta)<=1e-5) return normal;
    bool hidden=texelFetch(styleData,ivec2(5,0),0).x>1.5;
    bool xAxis=jointAxis(start,points);
    vec4 neighbor=vec4(start,end);
    vec4 first=record(link/2,2),second=record(link/2,3);
    if (link%2!=0) { first.w=-first.w; second.w=-second.w; }
    vec4 neighborPlanes=markedDepthPlanes(viewProjection,viewportSize,joint,far,
        farWorld-record(link/2,link%2).xyz,first,second);
    if (!jointSample(points,vec2(p.z/p.w,q.z/q.w)*.5+.5,jointProbe(points,atStart,xAxis),xAxis,hidden,planes) ||
        !jointSample(neighbor,vec2(joint.z/joint.w,far.z/far.w)*.5+.5,jointProbe(neighbor,true,xAxis),xAxis,hidden,neighborPlanes)) return normal;
    vec2 other=normalize(vec2(-delta.y,delta.x))*side*(atStart?-1.0:1.0);
    vec2 bisector=normal+other;
    float denominator=1.0+dot(normal,other);
    if (denominator<=1e-5) return normal;
    // Intersection of the two displaced edge tangents, with a bounded miter.
    return bisector*min(1.0/denominator,4.0/max(length(bisector),1e-5));
}
void main() {
    bool mixed=texelFetch(styleData,ivec2(17,0),0).w>.5;
    int edge=mixed?gl_InstanceID/2:gl_InstanceID;
    vec4 a=record(edge,0), b=record(edge,1);
    vec3 toEye=perspectiveView!=0 ? eyePosition-a.xyz : viewDirection;
    vec4 n0=record(edge,2), n1=record(edge,3);
    float objectMode=texelFetch(styleData,ivec2(6,0),0).z;
    float facing0=dot(n0.xyz,toEye), facing1=dot(n1.xyz,toEye);
    float f0=objectFacing(facing0,n1.w,objectMode);
    float f1=objectFacing(facing1,n1.w,objectMode);
    edgeIndex=float(edge);faceChoice=(f1>=0.0&&f0<0.0)?1.0:0.0;clipInterval=vec2(0,1);screenEnds=vec4(0);chainSpan=vec3(0);
    endExtension=vec2(0);
    waveEnds=vec2(0);
    waveSide=0.0;
    normalSpans=vec4(0);
    normalSide=0.0;
    normalBaseSide=0.0;
    normalDepthPlanes=vec4(0);
    objectOutside=vec2(0);objectDepthGradient=vec2(0);rearCrease=0.0;
    bool chosen=edgeSelected(a.w,b.w,n0.w,n1.w,toEye,f0,f1);
    float jointExtension=0.0;
    if (chosen && texelFetch(styleData,ivec2(3,0),0).w>.5) {
        int width=textureSize(chainData,0).x;
        vec4 chain=texelFetch(chainData,ivec2(edge%width,edge/width),0);
        chainSpan=chain.xyz;
        vec2 lengths=texelFetch(styleData,ivec2(7,0),0).zw;
        // Keep chains that reach the authored length, allowing pixel rounding.
        chosen=chainSpan.z+1e-4>=lengths.x;
        int ends=int(chain.w);
        endExtension=vec2(float(ends&1),float((ends>>1)&1))*lengths.y;
        waveEnds=vec2(float(ends&1),float((ends>>1)&1));
        if (objectMode==0.0) jointExtension=lengths.y;
    }
    vec4 p=viewProjection*vec4(a.xyz,1.0), q=viewProjection*vec4(b.xyz,1.0);
    // Clip the segment against the near plane before dividing by w.
    float pa=p.z+p.w, qa=q.z+q.w;
    if (!chosen || (pa<=0.0 && qa<=0.0)) {
        gl_Position=vec4(2.0,2.0,2.0,1.0); lineCoord=vec2(0.0); segmentLength=0.0;
        worldA=vec3(0.0);worldB=vec3(0.0);inverseW=vec2(1.0);return;
    }
    vec3 wa=a.xyz, wb=b.xyz;
    if (pa<=0.0) { float t=pa/(pa-qa); p=mix(p,q,t); wa=mix(wa,wb,t);clipInterval.x=t;endExtension.x=0.0; }
    else if (qa<=0.0) { float t=qa/(qa-pa); q=mix(q,p,t); wb=mix(wb,wa,t);clipInterval.y=1.0-t;endExtension.y=0.0; }
    vec2 sa=p.xy/p.w*viewportSize*0.5, sb=q.xy/q.w*viewportSize*0.5;
    vec2 delta=sb-sa; float lengthValue=length(delta);
    vec2 tangent=delta/max(lengthValue,0.00001), normal=vec2(-tangent.y,tangent.x);
    vec2 shiftA=vec2(0), shiftB=vec2(0);
    float waveAmplitude=texelFetch(styleData,ivec2(18,0),0).x;
    if (waveAmplitude!=0.0) {
        waveSide=markNormalSide(n0,n1,toEye);
        if (pa<=0.0) waveEnds.x=1.0;
        if (qa<=0.0) waveEnds.y=1.0;
        normalDepthPlanes=markedDepthPlanes(viewProjection,viewportSize,p,q,wb-wa,n0,n1);
    }
    float normalOffset=texelFetch(styleData,ivec2(17,0),0).z;
    if (normalOffset!=0.0 && lengthValue>1e-5) {
        float side=markNormalSide(n0,n1,toEye);
        normalBaseSide=side;
        normalDepthPlanes=markedDepthPlanes(viewProjection,viewportSize,p,q,wb-wa,n0,n1);
        if (mixed) {
            int width=textureSize(visibilityData,0).x;
            normalSpans=texelFetch(visibilityData,ivec2(edge%width,edge/width),0);
            normalSpans.zw/=lengthValue;
            side=gl_InstanceID%2==0?1.0:-1.0;
            normalSide=side;
        }
        vec2 signedNormal=normal*side;
        vec4 points=vec4(sa+viewportSize*.5,sb+viewportSize*.5);
        if (viewExtra.z>0.0) points-=viewExtra.xyxy;
        bool joinA=pa>0.0 && (!mixed || (normalSpans.z>0.0 && normalSpans.x==side));
        bool joinB=qa>0.0 && (!mixed || (normalSpans.w>0.0 && normalSpans.y==side));
        shiftA=normalOffset*(joinA?normalShift(edge,p,q,points,signedNormal,side,true,normalDepthPlanes):signedNormal);
        shiftB=normalOffset*(joinB?normalShift(edge,p,q,points,signedNormal,side,false,normalDepthPlanes):signedNormal);
    }
    vec3 span=wb-wa;vec4 profiles=texelFetch(styleData,ivec2(14,0),0);
    bool collapsedRear=objectMode>=2.0 &&
        ((abs(facing0)<1e-7 && facing1<-1e-7) || (abs(facing1)<1e-7 && facing0<-1e-7));
    if ((abs(objectMode)==1.0 || collapsedRear) && abs(n1.w)==1.0) {
        vec4 face=faceChoice>.5 ? n1 : n0;
        if (collapsedRear) face=facing0<facing1 ? n0 : n1;
        vec3 inside=cross(face.xyz,span)*face.w;
        vec4 mid=(p+q)*.5;
        vec4 stepClip=viewProjection*vec4(inside,0.0);
        // Derivative of the perspective divide along the incident face.
        vec3 direction=(stepClip.xyz-mid.xyz/mid.w*stepClip.w)/mid.w*.5;
        vec2 projected=direction.xy*viewportSize;
        float determinant=delta.x*projected.y-delta.y*projected.x;
        if (abs(determinant)>1e-8) {
            objectOutside=-normal*sign(determinant);
            rearCrease=collapsedRear ? 1.0 : 0.0;
            float depthDelta=(q.z/q.w-p.z/p.w)*.5;
            objectDepthGradient=vec2(depthDelta*projected.y-delta.y*direction.z,
                                     delta.x*direction.z-depthDelta*projected.x)/determinant;
        }
    }
    vec3 reference=profiles.z>.5?texelFetch(styleData,ivec2(15,0),0).xyz:eyePosition;
    float closest=clamp(dot(reference-wa,span)/max(dot(span,span),1e-20),0.0,1.0);
    float distanceValue=length(reference-mix(wa,wb,closest));
    float boundReduction=profiles.x>.5?1.0:reduction(sizeReduction,distanceValue);
    float halfWidth=max(0.0,lineWidth*boundReduction*texelFetch(styleData,ivec2(3,0),0).z*0.5);
    float extent=halfWidth+max(1.0,texelFetch(styleData,ivec2(19,0),0).y);
    if (normalOffset!=0.0) {
        vec2 shifted=delta+shiftB-shiftA;
        lengthValue=length(shifted);
        tangent=shifted/max(lengthValue,1e-5);
        normal=vec2(-tangent.y,tangent.x);
    }
    vec2 reach=max(endExtension,jointExtension*vec2(float(pa>0.0),float(qa>0.0)));
    vec2 endBounds=vec2(-extent-reach.x,extent+reach.y);
    float lateralExtent=extent+abs(waveAmplitude);
    vec2 offset=tangent*mix(endBounds.x,endBounds.y,corner.x)+normal*corner.y*lateralExtent;
    // Shift the final stroke in screen space. Visibility and chain sampling
    // below still use the original, unshifted centerline.
    offset+=texelFetch(styleData,ivec2(17,0),0).xy+mix(shiftA,shiftB,corner.x);
    vec4 pos=mix(p,q,corner.x); pos.xy+=offset*2.0/viewportSize*pos.w;
    // Rasterize into a tile while keeping lengths, maps and dash phase in full-frame pixels.
    if (viewExtra.z>0.0) {
        vec2 scale=viewportSize/viewExtra.zw;
        pos.xy=pos.xy*scale+(scale-1.0-2.0*viewExtra.xy/viewExtra.zw)*pos.w;
    }
    pos.z-=0.000002*pos.w;
    gl_Position=pos;
    lineCoord=vec2(mix(endBounds.x,lengthValue+endBounds.y,corner.x),corner.y*lateralExtent);
    segmentLength=lengthValue;worldA=wa;worldB=wb;inverseW=vec2(1.0/p.w,1.0/q.w);
    screenEnds=vec4(sa+viewportSize*.5,sb+viewportSize*.5);
}
'''
FRAGMENT = JOINT_DEPTH + r'''
float reduction(vec4 spec,float distanceValue) {
    return mix(1.0,mix(1.0,spec.z,clamp((distanceValue-spec.x)/max(0.000001,spec.y-spec.x),0.0,1.0)),spec.w);
}
vec2 segmentDepths() {
    vec4 a=viewProjection*vec4(worldA,1), b=viewProjection*vec4(worldB,1);
    return vec2(a.z/a.w,b.z/b.w)*.5+.5;
}
float outlineDepthTap(ivec2 pixel,vec2 center,float depth) {
    float observed=texelFetch(sceneDepth,pixel,0).r;
    float owner=texelFetch(objectDepth,pixel,0).r;
    // A silhouette's inward taps can hit its own bevel before the edge.
    // Keep the depth of a different mesh when it is in front of that skin.
    bool inward=dot(vec2(pixel)+.5-center,objectOutside)<0.0;
    return inward && owner<1.0 && abs(observed-owner)<=1e-7 ? depth : observed;
}
float outlineCenterDepth(vec2 center,float depth,float sampledDepth) {
    if (dot(objectOutside,objectOutside)<.5) return sampledDepth;
    ivec2 outside=ivec2(floor(center+objectOutside*1.5));
    ivec2 base=ivec2(floor(center-.5));
    ivec2 size=textureSize(sceneDepth,0);
    if (any(lessThan(min(base,outside),ivec2(0))) ||
        any(greaterThanEqual(max(base+1,outside),size)) ||
        texelFetch(objectDepth,outside,0).r<1.0) return sampledDepth;
    vec2 f=fract(center-.5);
    float a=mix(outlineDepthTap(base,center,depth),
                outlineDepthTap(base+ivec2(1,0),center,depth),f.x);
    float b=mix(outlineDepthTap(base+ivec2(0,1),center,depth),
                outlineDepthTap(base+ivec2(1,1),center,depth),f.x);
    return mix(a,b,f.y);
}
bool spanMeetsMinimum(vec4 points,vec2 depths,float position,bool hidden) {
    float minimum=styleValue(7).z;
    vec4 span=readStyleTex(visibilityData,int(edgeIndex));
    float total;
    if (span.z>=segmentLength-.0001) total=span.z+span.x+span.y;
    else if (position<=span.z) total=span.z+span.x;
    else if (position>=segmentLength-span.w) total=span.w+span.y;
    else {
        total=spanLength(points,depths,segmentLength,position,-1.0,minimum,hidden);
        total+=spanLength(points,depths,segmentLength,position,1.0,max(0.0,minimum-total),hidden);
    }
    // A raster depth transition can shift with subpixel camera alignment.
    return total+.5>=minimum;
}
int jointLink(bool atStart) {
    vec2 links=readStyleTex(jointData,int(edgeIndex)).xy;
    return int(atStart?links.x:links.y);
}
bool extendsAtJoint(vec4 points,vec2 depths,bool atStart,bool hidden) {
    if (atStart?clipInterval.x>0.0:clipInterval.y<1.0) return false;
    int link=jointLink(atStart);
    if (link<0) return false;
    vec2 start=atStart?points.xy:points.zw;
    bool xAxis=jointAxis(start,points);
    float t=jointProbe(points,atStart,xAxis);
    if (!jointSample(points,depths,t,xAxis,hidden)) return false;
    if (directionInfo.w>.5 && !spanMeetsMinimum(points,depths,t*segmentLength,hidden)) return false;
    vec4 p=viewProjection*vec4(atStart?worldA:worldB,1);
    vec3 far=readStyleTex(edgeData,(link/2)*4+1-(link%2)).xyz;
    vec4 q=viewProjection*vec4(far,1);
    float pa=p.z+p.w, qa=q.z+q.w;
    if (qa<=0.0) q=mix(p,q,pa/(pa-qa));
    vec2 end=(q.xy/q.w*.5+.5)*viewportSize;
    if (viewExtra.z>0.0) end-=viewExtra.xy;
    if (length(end-start)<=1e-5) return false;
    vec4 neighbor=vec4(start,end);
    return jointSample(neighbor,vec2(p.z/p.w,q.z/q.w)*.5+.5,jointProbe(neighbor,true,xAxis),xAxis,!hidden);
}
float cutDistance(vec4 points,vec2 depths,float position,float reach,bool hidden) {
    float best=reach;
    for (float direction=-1.0;direction<=1.0;direction+=2.0) {
        float available=direction>0.0?segmentLength-position:position;
        float limit=min(available,best);
        if (limit<=0.0) continue;
        float gap=spanLength(points,depths,segmentLength,position,direction,limit,!hidden);
        if (gap>=limit) continue;
        // Probe just across the refined cut, inside the span that would extend.
        if (gap+.0625>=available) continue;
        float source=position+direction*(gap+.0625);
        // A depth sample right at a shared vertex must not turn a subpixel
        // spill onto the wrong incident edge into an extended stroke.
        bool atStart=direction<0.0;
        if (available-gap<1.0 && jointLink(atStart)>=0) {
            bool xAxis=jointAxis(atStart?points.xy:points.zw,points);
            float probe=jointProbe(points,atStart,xAxis);
            if (!jointSample(points,depths,probe,xAxis,hidden)) continue;
        }
        if (!spanSample(points,depths,source/segmentLength,hidden)) continue;
        if (directionInfo.w>.5 && !spanMeetsMinimum(points,depths,source,hidden)) continue;
        best=gap;
    }
    return best;
}
vec2 waveRun(vec2 depths,float position,float reach,bool hidden) {
    vec4 span=readStyleTex(visibilityData,int(edgeIndex));
    if (span.z>=segmentLength-.0001) return vec2(-span.x,segmentLength+span.y);
    if (span.z>0.0 && position<=span.z) return vec2(-span.x,span.z);
    if (span.w>0.0 && position>=segmentLength-span.w) return vec2(segmentLength-span.w,segmentLength+span.y);
    // A run contained inside one edge cannot be reached by the endpoint pass.
    bool forward=chainSpan.x<chainSpan.y;
    float before=spanLength(screenEnds,depths,segmentLength,position,-1.0,forward?segmentLength:reach,hidden,normalDepthPlanes,true);
    float after=spanLength(screenEnds,depths,segmentLength,position,1.0,forward?reach:segmentLength,hidden,normalDepthPlanes,true);
    return vec2(position-before,position+after);
}
float waveHeight(float x,vec3 wave,float origin,float direction) {
    return waveSide*wave.x*sin(6.28318530718*(x-origin)*direction/wave.y+wave.z);
}
float waveDistance(float reach,vec3 wave,vec2 run) {
    // Adjacent edges share coverage only while the run continues across
    // the joint. Ignore subpixel continuation within the depth search's
    // one-pixel/1024 precision so a cut at the joint keeps its round cap.
    if (waveEnds.x<.5 && run.x<-.001 && lineCoord.x<0.0) return reach;
    if (waveEnds.y<.5 && run.y>segmentLength+.001 && lineCoord.x>=segmentLength) return reach;
    bool forward=chainSpan.x<chainSpan.y;
    float origin=forward?run.x:run.y, direction=forward?1.0:-1.0;
    float low=max(lineCoord.x-reach,run.x), high=min(lineCoord.x+reach,run.y);
    if (high<low || abs(lineCoord.y)>abs(wave.x)+reach) return reach;
    int steps=int(clamp(ceil((high-low)/min(1.0,wave.y/8.0)),1.0,64.0));
    vec2 start=vec2(low,waveHeight(low,wave,origin,direction));
    float closest=reach*reach;
    for(int i=1;i<=steps;++i) {
        float x=mix(low,high,float(i)/float(steps));
        vec2 end=vec2(x,waveHeight(x,wave,origin,direction));
        vec2 span=end-start, delta=lineCoord-start;
        float t=clamp(dot(delta,span)/max(dot(span,span),1e-12),0.0,1.0);
        vec2 separation=delta-span*t;
        closest=min(closest,dot(separation,separation));
        start=end;
    }
    return sqrt(closest);
}
float cutWaveDistance(float reach,vec3 wave,vec2 depths,float position,bool hidden) {
    if (spanSample(screenEnds,depths,position/segmentLength,hidden,normalDepthPlanes,true)) {
        return waveDistance(reach,wave,waveRun(depths,position,reach,hidden));
    }
    // The displaced centerline stops at the cut, but its round cap can
    // cover pixels whose projection onto the source edge is across it.
    float limit=reach-abs(lineCoord.x-position);
    if (limit<=0.0) return reach;
    float closest=reach;
    for (float direction=-1.0;direction<=1.0;direction+=2.0) {
        float gap=spanLength(screenEnds,depths,segmentLength,position,direction,limit,!hidden,normalDepthPlanes,true);
        if (gap>=limit) continue;
        // Step just past the binary search's boundary uncertainty.
        float inside=position+direction*(gap+.002);
        if (inside<=0.0 || inside>=segmentLength ||
                !spanSample(screenEnds,depths,inside/segmentLength,hidden,normalDepthPlanes,true)) continue;
        closest=min(closest,waveDistance(reach,wave,waveRun(depths,inside,reach,hidden)));
    }
    return closest;
}
void main() {
    float t=clamp(lineCoord.x/max(segmentLength,0.00001),0.0,1.0);
    if (normalSide!=0.0) {
        // A single source edge can straddle spans with opposite directions.
        // The two offset copies keep disjoint parts of that source edge.
        float side=normalBaseSide;
        if (normalSpans.z>0.0 && t<=normalSpans.z) side=normalSpans.x;
        else if (normalSpans.w>0.0 && t>=1.0-normalSpans.w) side=normalSpans.y;
        if (side!=normalSide) discard;
    }
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
    float outside=max(max(-lineCoord.x-endExtension.x,lineCoord.x-segmentLength-endExtension.y),0.0);
    vec3 wave=styleValue(18).xyz;
    vec2 coverageOptions=styleValue(19).xy;
    vec4 filterSpec=styleValue(20);
    float reach=radius+coverageOptions.y;
    bool splitWave=wave.x!=0.0 && directionInfo.w>.5;
    vec2 run=vec2(0.0);
    float splitDistance=reach;
    if (splitWave) {
        if (segmentLength<=1e-5 || abs(lineCoord.y)>abs(wave.x)+reach) discard;
        splitDistance=cutWaveDistance(reach,wave,segmentDepths(),t*segmentLength,modes.x>1.5);
    } else if(modes.x>.5){
        vec4 points=screenEnds;
        if (viewExtra.z>0.0) points-=viewExtra.xyxy;
        vec2 center=mix(points.xy,points.zw,t);
        vec4 clip=viewProjection*vec4(mix(worldA,worldB,u),1);float depth=clip.z/clip.w*.5+.5;
        float scene=markedCenterDepth(center,points.zw-points.xy,depth,normalDepthPlanes);
        vec2 objectOptions=styleValue(6).zw;
        float objectMode=objectOptions.x;
        if (objectMode==-1.0 && objectOptions.y<.5 && depth>scene+.000002)
            scene=outlineCenterDepth(center,depth,scene);
        bool hidden=depth>scene+.000002;
        if (rearCrease>.5) {
            // The shared side face collapses in projection. The inward
            // sample reaches the nearer skin without blending with the
            // background, and the outward sample identifies an exterior.
            vec2 inward=center-objectOutside*1.5;
            float front=sampleDepth(objectDepth,inward);
            if (front<depth-.000002) {
                hidden=true;
                if (objectMode==2.0) {
                    float backing=sampleDepth(objectDepth,center+objectOutside*1.5);
                    bool frontHidden=front>sampleDepth(sceneDepth,inward)+.000002;
                    bool exterior=backing>=1.0 || backing<=front+.000002;
                    if (exterior && frontHidden==(modes.x>1.5)) discard;
                }
            }
        }
        bool wantHidden=modes.x>1.5;
        float extension=styleValue(7).w;
        float overrun=max(-lineCoord.x,lineCoord.x-segmentLength);
        bool extendsJoint=extension>0.0 && objectMode==0.0 && segmentLength>1e-5 &&
            overrun>0.0 && overrun<extension+reach && abs(lineCoord.y)<reach &&
            extendsAtJoint(points,segmentDepths(),lineCoord.x<0.0,wantHidden);
        if (extendsJoint) {
            outside=max(0.0,overrun-extension);
        } else if (wantHidden!=hidden) {
            if (extension<=0.0 || objectMode!=0.0 || segmentLength<=1e-5 || abs(lineCoord.y)>=reach) discard;
            overrun=max(0.0,overrun);
            float remaining=extension+reach-overrun;
            if (remaining<=0.0) discard;
            float gap=cutDistance(points,segmentDepths(),t*segmentLength,remaining,wantHidden);
            if (gap>=remaining) discard;
            // Beyond a connected end, a zero-distance hit can be bilinear
            // spill at the vertex. Only the joint test may extend that hit.
            if (overrun>0.0 && gap==0.0 && jointLink(lineCoord.x<0.0)>=0) discard;
            outside=max(0.0,gap+overrun-extension);
        } else if (directionInfo.w>.5 && segmentLength>1e-5) {
            if (!spanMeetsMinimum(points,segmentDepths(),t*segmentLength,wantHidden)) discard;
        }
        if (abs(objectMode)==1.0) {
            ivec2 pixel=ivec2(floor(center+objectOutside*1.5));
            ivec2 extent=textureSize(objectDepth,0);
            bool interior=false;
            if (dot(objectOutside,objectOutside)>.5 && all(greaterThanEqual(pixel,ivec2(0))) && all(lessThan(pixel,extent))) {
                float backing=texelFetch(objectDepth,pixel,0).r;
                float incidentDepth=depth+dot(vec2(pixel)+.5-center,objectDepthGradient);
                bool coplanar=objectOptions.y>.5 && abs(backing-incidentDepth)<=.000002;
                interior=backing<1.0 && (backing>depth+.000002 || coplanar);
            }
            if (interior!=(objectMode>0.0)) discard;
        }
    }
    if(dashOptions.x>.5&&dashOptions.z>0.0){
        float period=max(.00001,dashOptions.y+dashOptions.z),phase=mod(along+dashOptions.w,period);
        if(phase>dashOptions.y)outside=max(outside,min(phase-dashOptions.y,period-phase));
    }
    if (wave.x!=0.0 && !splitWave) {
        bool forward=chainSpan.x<chainSpan.y;
        float before=forward?chainSpan.x:chainSpan.z-chainSpan.x;
        float after=forward?chainSpan.z-chainSpan.y:chainSpan.y;
        run=vec2(waveEnds.x>.5?0.0:-before,segmentLength+(waveEnds.y>.5?0.0:after));
    }
    float distanceValue=splitWave?splitDistance:
        (wave.x!=0.0?waveDistance(reach,wave,run):length(vec2(outside,lineCoord.y)));
    float coverage=clamp(radius+0.5-distanceValue,0.0,1.0);
    if (filterSpec.x!=1.0 || filterSpec.y!=1.0) {
        vec4 samples=styleValue(21);
        coverage=filteredEdge(radius-distanceValue,filterSpec,samples)-
                 filteredEdge(-radius-distanceValue,filterSpec,samples);
    } else if (radius<.5 && coverageOptions.x>.5) {
        // When the pixel footprint spans both sides of a thin stroke,
        // subtract the uncovered portion beyond its opposite edge.
        coverage-=max(0.0,.5-radius-distanceValue);
    }
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
    interface.flat('VEC2','endExtension')
    interface.flat('VEC4','normalSpans');interface.flat('FLOAT','normalSide');interface.flat('FLOAT','normalBaseSide')
    interface.flat('FLOAT','waveSide');interface.flat('VEC2','waveEnds')
    interface.flat('VEC4','normalDepthPlanes')
    interface.flat('VEC2','objectOutside')
    interface.flat('VEC2','objectDepthGradient')
    interface.flat('FLOAT','rearCrease')
    info.vertex_out(interface)
    info.sampler(0, 'FLOAT_2D', 'edgeData')
    info.sampler(1, 'FLOAT_2D', 'extraNormals')
    info.sampler(2, 'FLOAT_2D', 'styleData')
    for slot,name in enumerate(('curveData','chainData','colorImage','sizeImage','coordinateData','sceneDepth','objectDepth','visibilityData','jointData'),3):info.sampler(slot,'FLOAT_2D',name)
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


def _object_depth(group, view, size, area_id):
    import gpu
    from . import gpu_depth
    texture = gpu_depth.draw(group['object_depth'], view, size, area_id)
    gpu.state.depth_test_set('NONE')
    gpu.state.depth_mask_set(False)
    gpu.state.blend_set('ALPHA')
    return texture


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
        size = (context.region.width, context.region.height)
        area_id = context.area.as_pointer()
        depth_texture=gpu_depth.draw(entry['depth'],view,size,area_id) if entry.get('depth') else None
        if depth_texture is None:
            if 'dummy_depth' not in entry:entry['dummy_depth']=gpu.types.GPUTexture((1,1),format='R32F',data=gpu.types.Buffer('FLOAT',1,[1.]))
            depth_texture=entry['dummy_depth']
        gpu.state.depth_test_set('NONE' if entry.get('depth') else 'LESS_EQUAL'); gpu.state.depth_mask_set(False); gpu.state.blend_set('ALPHA')
        _shader.bind()
        direction=inverse.to_3x3() @ __import__('mathutils').Vector((0,0,1))
        eye=(*inverse.translation,0.)
        sight=(*direction,0.)
        _shader.uniform_float('viewProjection',view.perspective_matrix)
        _shader.uniform_float('eyeInfo',eye)
        _shader.uniform_float('viewExtra',(*gpu.state.viewport_get()[:2],0.,0.))
        _shader.uniform_sampler('sceneDepth',depth_texture)
        _shader.uniform_sampler('objectDepth',depth_texture)
        last_object_depth = None
        for group in entry['groups']:
            if not len(group['records']):continue
            if group['object_depth'] is not None and group['object_depth'] is not last_object_depth:
                object_depth = _object_depth(group, view, size, area_id)
                _shader.bind()
                _shader.uniform_sampler('objectDepth', object_depth)
                last_object_depth = group['object_depth']
            if group['texture'] is None:_texture(group);entry['uploads']+=1
            viewport=(*size,group['width'],int(view.is_perspective))
            visibility=None
            normal_data=None
            if group['style']['needs_chain']:
                gpu_chain.update(group,view.perspective_matrix,viewport,eye,sight,area_id)
                if gpu_chain.visibility_minimum(group) or group['style']['params'][18,0]:
                    visibility=gpu_chain.update_visibility(group,view.perspective_matrix,viewport,eye,sight,depth_texture,area_id)
                elif group['style']['params'][17,3]:
                    normal_data=gpu_chain.update_normal_directions(group,view.perspective_matrix,viewport,eye,sight,depth_texture,area_id,cache=True)
                _shader.bind()
                _shader.uniform_sampler('sceneDepth',depth_texture)
            _shader.uniform_sampler('edgeData',group['texture'])
            _shader.uniform_sampler('extraNormals',group['normal_texture'])
            _shader.uniform_sampler('styleData',group['style_texture'])
            for name,key in (('curveData','curve_texture'),('chainData','chain_texture'),('colorImage','color_texture'),('sizeImage','size_texture'),('coordinateData','coordinate_texture')):_shader.uniform_sampler(name,group[key])
            if normal_data is None:normal_data=visibility if visibility is not None else group['chain_texture']
            _shader.uniform_sampler('visibilityData',normal_data)
            _shader.uniform_sampler('jointData',group.get('joint_texture',group['chain_texture']))
            _shader.uniform_float('directionInfo',(*direction,float(visibility is not None)))
            _shader.uniform_float('viewportInfo',viewport)
            _batch.draw_instanced(_shader, instance_count=len(group['records'])*(2 if group['style']['params'][17,3] else 1))
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
    if entry and any(isinstance(update.id,(bpy.types.Object,bpy.types.Mesh,bpy.types.Image,bpy.types.Collection)) and
                     (update.id.original.as_pointer() in entry['source_ids'] or isinstance(update.id,(bpy.types.Mesh,bpy.types.Collection))
                      or getattr(update.id,'type',None)=='MESH' or getattr(update.id,'instance_type',None)=='COLLECTION')
                     for update in depsgraph.updates):
        entry['dirty']=True


def collection_changed(_collection, _context=None):
    for entry in _states.values():
        entry['dirty'] = True


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
