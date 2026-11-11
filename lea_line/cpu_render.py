"""CPU-only final ink with solid or image-stamp coverage."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from ctypes import (POINTER, Structure, addressof, byref, c_float, c_size_t, c_uint32,
                    c_void_p)
import math
import os
import time
from itertools import groupby

import numpy as np
from mathutils import Vector

from .blender_compat import read_mesh_marks
from . import backend, gpu_depth, gpu_preview, gpu_styles, native_core, width_profile


class _Reduction(Structure):
    _fields_ = [('range_factor', c_float * 3), ('enabled', c_uint32),
                ('reference', c_float * 3), ('curve', c_void_p)]


class _Style(Structure):
    _fields_ = [('flags', c_uint32), ('min_cos', c_float), ('max_cos', c_float),
                ('radius', c_float), ('aa', c_float), ('rgba', c_float * 4),
                ('stamp', c_void_p), ('stamp_width', c_size_t), ('stamp_height', c_size_t),
                ('stamp_period', c_float), ('reduction', _Reduction * 2), ('stroke_curves', c_void_p * 2),
                ('wave', c_float * 3), ('dash', c_float * 3)]


class _Mesh(Structure):
    _fields_ = [
        ('vertices', c_void_p), ('vertex_count', c_size_t),
        ('triangles', c_void_p), ('triangle_count', c_size_t),
        ('edges', c_void_p), ('edge_count', c_size_t),
        ('loop_edges', c_void_p), ('loop_vertices', c_void_p), ('loop_count', c_size_t),
        ('sizes', c_void_p), ('normals', c_void_p), ('face_count', c_size_t),
        ('projection', c_float * 16), ('normal', c_float * 9), ('eye', c_float * 3),
        ('perspective', c_uint32), ('handedness', c_float), ('owner', c_uint32),
        ('style_ids', c_void_p), ('style_count', c_size_t), ('marks', c_void_p),
        ('world', c_float * 12),
        ('face_masks', c_void_p),
        ('face_materials', c_void_p),
    ]


def _api():
    library = native_core._load()
    try:
        version = library.rea_cpu_lines_abi_version
    except AttributeError as error:
        raise ValueError('CPU 선 렌더용 Rust 모듈이 설치되어 있지 않습니다') from error
    version.argtypes, version.restype = [], c_uint32
    if version() != 7:
        raise ValueError('CPU 선 렌더용 Rust 모듈 버전이 호환되지 않습니다')
    signatures = {
        'create': ([c_size_t, c_size_t, POINTER(_Style), c_size_t], c_void_p),
        'prepare': ([c_void_p, POINTER(_Mesh)], c_void_p),
        'draw': ([c_void_p, POINTER(c_void_p), c_size_t, POINTER(c_size_t), c_void_p], c_size_t),
        'free': ([c_void_p], None),
        'free_mesh': ([c_void_p], None),
    }
    functions = []
    for name, (arguments, result) in signatures.items():
        function = getattr(library, 'rea_cpu_lines_' + name)
        function.argtypes, function.restype = arguments, result
        functions.append(function)
    return functions


def snapshot(scene, depsgraph):
    jobs = [
        job
        for _, group in groupby(gpu_preview._jobs(scene, render=depsgraph.mode == 'RENDER'),
                                key=lambda job: id(job[0].source))
        for job in sorted(group, key=lambda job: not job[0]._gpu_hidden)
    ]
    styles = []
    coverage = []
    reduction_curves = []
    camera_references = []
    stroke_curves = []
    for node, brush, _ in jobs:
        if (node._gpu_merge_mode or node.object_edge
                or any(getattr(node, k) for k in ('wireframe',
                                                 'smoothing_boundary', 'ridge_valley', 'suggestive_contour'))):
            raise ValueError(f'CPU 선 렌더: {node.name}은 보이는 Outline, Open Edge, Normal Angle, Selected Edges, Material Boundary를 지원합니다')
        if node._gpu_hidden and (node.outline or node.border or node.edge_mark
                                 or not (node.crease or node.material_boundary)):
            raise ValueError(f'CPU 선 렌더: {node.name}의 숨은 선은 Normal Angle과 Material Boundary에서 지원합니다')
        scale = backend.width_scale(scene, node)
        params = gpu_styles.snapshot(brush, scale, output=node.line_output)
        period = float(params['params'][22,0])
        unsupported_chain = any(getattr(brush, key, False) for key in
                                ('min_line_length', 'extend', 'normal_offset'))
        if ((getattr(brush, 'brush_type', 'SIMPLE') != 'SIMPLE' and not period)
                or unsupported_chain or params['maps']
                or np.any(params['params'][17, :3])
                or node.line_output.over_sampling != 1):
            raise ValueError(f'CPU 선 렌더: {node.name}은 오버샘플링 1에서 Simple·질감 브러시를 지원합니다. 추가 맵과 오프셋·길이 효과는 지원하지 않습니다')
        outline, border = node.outline, node.border or (node.outline and node.outline_open)
        reserve = (node.crease or node.edge_mark or node.material_boundary) and not outline
        flags = int(outline) | int(border) << 1 | int(node.crease) << 2 | int(reserve) << 3
        flags |= int(node.edge_mark) << 4 | int('edge_mark' in node._gpu_exclude) << 5
        flags |= int(node.material_boundary) << 6 | int('material_boundary' in node._gpu_exclude) << 7
        flags |= int(node._gpu_hidden) << 8
        width = brush.thickness * scale
        if not period and scene.lea_line.stroke_width_profile == 'PENCIL_SIMPLE':
            width = width_profile.coverage_width(width, 'PENCIL_SIMPLE')
        stamp = np.ascontiguousarray(params['images']['size'][:,:,0]) if period else None
        height, columns = stamp.shape if stamp is not None else (0, 0)
        coverage.append(stamp)
        channels = []
        from_camera = []
        for index, kind in enumerate(('SIZE', 'ALPHA')):
            near, far, factor, enabled = gpu_preview._channel(brush, kind)
            curve = (np.ascontiguousarray(params['curves'][:,index+2])
                     if enabled and params['params'][14,index] else None)
            reduction_curves.append(curve)
            from_camera.append(bool(enabled and not params['params'][14,index+2]))
            channels.append(_Reduction((c_float*3)(near, far, factor), int(enabled),
                                       (c_float*3)(*params['params'][15+index,:3]),
                                       curve.ctypes.data if curve is not None else None))
        camera_references.append(from_camera)
        profiles = [np.ascontiguousarray(params['curves'][:,index])
                    if params['params'][3,index] else None for index in range(2)]
        wave = bool(gpu_styles.wave_amount(brush))
        direction = getattr(brush, 'line_direction_enabled', False)
        shaped_stroke = any(profile is not None for profile in profiles) or getattr(brush, 'dashed', False)
        if (shaped_stroke and direction or (wave or shaped_stroke)
                and getattr(brush, 'split_angle_enabled', False)):
            raise ValueError(f'CPU 선 렌더: {node.name}의 스트로크 곡선과 방향 지정·각도 분할 조합은 미지원입니다')
        dash = params['params'][4, 1:] if params['params'][4, 0] else (0., 0., 0.)
        if wave and direction:
            # The inactive dash phase carries the angle without changing the native ABI.
            flags |= 1 << 9
            # Bit 10 is winding, bits 11–13 select the loop start point.
            flags |= int(params['params'][18, 3]) << 10
            dash = (0., 0., float(params['params'][7, 1]))
        stroke_curves.extend(profiles)
        styles.append(_Style(flags, math.cos(node.normal_angle_max)-2e-7,
                             math.cos(node.normal_angle_min)+2e-7, width*0.5,
                             node.line_output.antialiasing, (c_float*4)(*brush.color, brush.alpha),
                             stamp.ctypes.data if stamp is not None else None, columns, height, period, (_Reduction*2)(*channels),
                             (c_void_p*2)(*(profile.ctypes.data if profile is not None else None
                                            for profile in profiles)),
                             (c_float*3)(*params['params'][18, :3]),
                             (c_float*3)(*dash)))
    return {'jobs': jobs, 'styles': (_Style * len(styles))(*styles), 'stamp_coverage': coverage,
            'reduction_curves': reduction_curves, 'camera_references': camera_references,
            'stroke_curves': stroke_curves}


def _array(collection, name, shape, dtype):
    buffer = np.empty(shape, dtype)
    collection.foreach_get(name, buffer.ravel())
    return buffer


def _targeted(mesh, obj, parent, node, targets):
    if targets is None or obj.original in targets or parent in targets:
        return True
    material_filter = node._gpu_material_filter
    if material_filter is None:
        return False
    scope, materials = material_filter
    return (scope is None or obj.original in scope) and gpu_preview._material_face_selection(mesh, obj, materials)


def _packet(instance, owner, projection, world, perspective, jobs):
    obj = instance.object
    mesh = obj.data
    if not mesh.vertices:
        return None
    parent = instance.parent.original if instance.is_instance and instance.parent else None
    matching = {}
    style_ids = []
    selections = []
    for i, (node, _, targets) in enumerate(jobs):
        key = id(node._gpu_material_filter), id(targets)
        if key not in matching:
            matching[key] = _targeted(mesh, obj, parent, node, targets)
        selected = matching[key]
        if isinstance(selected, np.ndarray):
            if node.outline or node.border or node.edge_mark:
                raise ValueError('CPU 선 렌더: 일부 면의 재질 선택은 Normal Angle과 Material Boundary에서 지원합니다')
            style_ids.append(i)
            selections.append(selected)
        elif selected:
            style_ids.append(i)
            selections.append(None)
    face_masks = (c_void_p * len(selections))(
        *(mask.ctypes.data if mask is not None else None for mask in selections)
    ) if any(mask is not None for mask in selections) else None
    style_ids = np.array(style_ids, dtype=np.uint32)
    vertices = _array(mesh.vertices, 'co', (len(mesh.vertices),3), np.float32)
    triangles = gpu_depth.mesh_triangles(mesh)
    edges = np.empty((0,2), np.int32)
    sizes = np.empty(0, np.int32)
    loop_edges = loop_vertices = sizes
    normals = np.empty((0,3), np.float32)
    marks = None
    face_materials = None
    transform = instance.matrix_world.copy()
    normal = (0.,)*9
    eye = (0.,)*3
    handedness = 1.
    if len(style_ids):
        edges = _array(mesh.edges, 'vertices', (len(mesh.edges),2), np.int32)
        if any(jobs[i][0].edge_mark or 'edge_mark' in jobs[i][0]._gpu_exclude for i in style_ids):
            marks = read_mesh_marks(mesh, 'EDGE')
        if any(jobs[i][0].material_boundary or 'material_boundary' in jobs[i][0]._gpu_exclude for i in style_ids):
            face_materials = _array(mesh.polygons, 'material_index', len(mesh.polygons), np.int32)
        if mesh.loops:
            loop_edges = _array(mesh.attributes['.corner_edge'].data, 'value', len(mesh.loops), np.int32)
            loop_vertices = _array(mesh.attributes['.corner_vert'].data, 'value', len(mesh.loops), np.int32)
        sizes = _array(mesh.polygons, 'loop_total', len(mesh.polygons), np.int32)
        normals = _array(mesh.polygon_normals, 'vector', (len(mesh.polygons),3), np.float32)
        axes = transform.to_3x3()
        inverse = axes.inverted_safe()
        normal = tuple(v for row in inverse.transposed() for v in row)
        eye = (transform.inverted_safe() @ world.translation if perspective else
               inverse @ (world.to_3x3() @ Vector((0,0,1))))
        handedness = -1. if axes.determinant() < 0 else 1.
    clip = projection @ transform
    packet = _Mesh(vertices.ctypes.data,len(vertices),triangles.ctypes.data,len(triangles),
                   edges.ctypes.data,len(edges),loop_edges.ctypes.data,loop_vertices.ctypes.data,len(loop_edges),
                   sizes.ctypes.data,normals.ctypes.data,len(sizes),
                   (c_float*16)(*(v for row in clip for v in row)), (c_float*9)(*normal),
                   (c_float*3)(*eye),perspective,handedness,owner,
                   style_ids.ctypes.data,len(style_ids), marks.ctypes.data if marks is not None else None,
                   (c_float*12)(*(transform[row][col] for row in range(3) for col in range(4))),
                   addressof(face_masks) if face_masks is not None else None,
                   face_materials.ctypes.data if face_materials is not None else None)
    # Keep every ctypes input alive until its worker returns.
    return packet, (vertices, triangles, edges, loop_edges, loop_vertices, sizes, normals,
                    style_ids, marks, face_masks, selections, face_materials)


def draw(scene, depsgraph, entry, frame, bounds, *, world=None, shift_x=None):
    from .gpu_render import _camera_projection
    create, prepare, raster, release, release_mesh = _api()
    width, height = frame
    left, bottom, right, top = bounds
    camera = scene.camera.evaluated_get(depsgraph)
    if camera.data.type not in {'PERSP', 'ORTHO'}:
        raise ValueError('CPU 선 렌더는 원근 및 직교 카메라를 지원합니다')
    if world is None:
        world = camera.matrix_world.normalized()
    projection = _camera_projection(scene, depsgraph, camera, frame, shift_x) @ world.inverted()
    jobs = entry['jobs']
    styles = (_Style * len(entry['styles']))(*entry['styles'])
    for style, references in zip(styles, entry['camera_references']):
        for reduction, from_camera in zip(style.reduction, references):
            if from_camera:
                reduction.reference[:] = world.translation
    context = create(width,height,styles,len(styles))
    if not context:
        raise MemoryError('CPU 선 렌더의 깊이 버퍼를 할당할 수 없습니다')
    futures = []
    pending = deque()
    workers = min(12, os.cpu_count() or 1)
    test_break = entry['test_break']
    tri_count = 0
    start = time.perf_counter()

    def prepare_packet(packet):
        handle = prepare(context, byref(packet[0]))
        if not handle:
            raise RuntimeError('CPU 선 렌더가 메시를 처리하지 못했습니다')
        return handle

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for owner, instance in enumerate(depsgraph.object_instances):
                if test_break():
                    return None
                if instance.object.type != 'MESH' or not instance.show_self:
                    continue
                packet = _packet(instance,owner,projection,world,camera.data.type=='PERSP',jobs)
                if packet is None:
                    continue
                tri_count += packet[0].triangle_count
                future = pool.submit(prepare_packet,packet)
                futures.append(future)
                pending.append(future)
                if len(pending) >= workers*2:
                    pending.popleft().result()
            handles = [future.result() for future in futures]
        entry['cpu_prepare_seconds'] = time.perf_counter()-start
        if test_break():
            return None
        result = np.empty((top-bottom,right-left,4),np.float32)
        ink = (c_void_p*len(handles))(*handles)
        rect = (c_size_t*4)(*bounds)
        started = time.perf_counter()
        edges = raster(context,ink,len(handles),rect,result.ctypes.data)
        if edges == c_size_t(-1).value:
            raise RuntimeError('CPU 선 렌더의 래스터 작업이 실패했습니다')
        entry.update(edges=edges, depth_triangles=tri_count, render_tiles=1,
                     cpu_raster_seconds=time.perf_counter()-started, gpu_texture_limit=None)
        return result
    finally:
        for future in futures:
            if not future.cancelled() and future.exception() is None:
                release_mesh(future.result())
        release(context)
