"""Per-face material selection for explicit Line Art stroke snapshots.

Material targets are additive with explicit object targets. Scope is bounded by
the node's collection. Copies of the source meshes receive temporary face marks;
original mesh data and user face marks are never edited. This module does not
provide a live viewport cache. Call capture_strokes again after scene changes.
"""
from contextlib import contextmanager
import bpy
from . import native_core


def active(node):
    return bool(len(getattr(node, 'materials', ())))


def target_materials(node):
    return {item.material for item in getattr(node, 'materials', ()) if item.material}


def unsupported_reason(scene):
    """The snapshot uses a viewport graph; reject unobserved render variants."""
    if scene != bpy.context.scene:
        return 'Material Line Art snapshots require the active scene'
    if scene.render.use_simplify:
        return 'Render simplification requires the Freestyle material path'
    layer = bpy.context.view_layer
    def layer_hidden(branch):
        return branch.exclude or branch.hide_viewport or any(layer_hidden(child) for child in branch.children)
    if layer_hidden(layer.layer_collection):
        return 'Hidden or excluded layer collections require the Freestyle material path'
    safe_modifiers = {'SUBSURF', 'ARMATURE', 'MIRROR', 'TRIANGULATE', 'SMOOTH',
                      'LAPLACIANSMOOTH', 'DISPLACE', 'CAST', 'SIMPLE_DEFORM', 'WAVE', 'WEIGHTED_NORMAL'}
    def visit(collection):
        if collection.hide_render != collection.hide_viewport:
            return 'Collection render/viewport visibility differs'
        for child in collection.children:
            reason = visit(child)
            if reason:
                return reason
        return None
    reason = visit(scene.collection)
    if reason:
        return reason
    for obj in scene.objects:
        if obj.get('rea_pencil_preview'):
            continue
        # Swapping data on a linked Object ID is forbidden, even when the mesh
        # itself can be copied. A local object with linked mesh data is safe.
        if obj.type == 'MESH' and not obj.is_editable:
            return 'Read-only linked mesh objects require the Freestyle material path'
        if obj.instance_type != 'NONE':
            return 'Material face targets do not yet support instanced geometry'
        if obj.hide_render != obj.hide_viewport:
            return 'Object render/viewport visibility differs'
        if obj.hide_render:
            continue
        if obj.show_instancer_for_render != obj.show_instancer_for_viewport:
            return 'Instancer render/viewport visibility differs'
        if obj.type not in {'MESH', 'LIGHT', 'CAMERA', 'EMPTY', 'GREASEPENCIL'}:
            return 'Non-mesh Line Art sources require the Freestyle material path'
        if obj.type != 'MESH':
            continue
        if layer.objects.get(obj.name) == obj and obj.hide_get(view_layer=layer):
            return 'Temporarily hidden objects require the Freestyle material path'
        if obj.particle_systems:
            return 'Material face targets do not yet support particle geometry'
        for modifier in obj.modifiers:
            if modifier.show_render != modifier.show_viewport:
                return 'Modifier render/viewport visibility differs'
            if not modifier.show_render:
                continue
            if modifier.type not in safe_modifiers:
                return 'A modifier may change material assignments or face marks'
            if modifier.type == 'SUBSURF' and modifier.levels != modifier.render_levels:
                return 'Subdivision render/viewport levels differ'
    return None


@contextmanager
def scoped_faces(scene, node):
    """Temporarily bind copied meshes carrying one Line Set's OR selection.

    Every mesh remains in the scene, including unselected occluders. A material
    selection never makes its entire object a target. This context must cover
    evaluation only; restore before running a general render or saving a file.
    """
    reason = unsupported_reason(scene)
    if reason:
        raise ValueError(reason)
    materials = target_materials(node)
    objects = {item.object for item in getattr(node, 'objects', ()) if item.object}
    scope = set(node.source_collection.all_objects) if node.source_collection else None
    replacements = []
    report = {'marked_faces': 0, 'copied_meshes': 0}
    try:
        for obj in scene.objects:
            if obj.type != 'MESH':
                continue
            previous = obj.data
            copied = previous.copy()
            copied.name = '.rea pencil material face scope'
            replacements.append((obj, previous, copied))
            allowed = scope is None or obj in scope
            explicit = allowed and obj in objects
            slots = [slot.material for slot in obj.material_slots]
            marks = native_core.mesh_material_mask(copied, [slot in materials for slot in slots],
                                                   allowed=allowed, explicit=explicit)
            copied.polygons.foreach_set('use_freestyle_mark', marks)
            copied.update()
            obj.data = copied
            report['marked_faces'] += int(marks.sum())
            report['copied_meshes'] += 1
        yield report
    finally:
        for obj, previous, copied in replacements:
            # The initial assignment can fail on a protected object. Do not
            # repeat that forbidden write while restoring earlier replacements.
            if obj.data != previous:
                obj.data = previous
            if copied.users == 0:
                bpy.data.meshes.remove(copied)


def capture_strokes(scene, node, preview):
    """Return a detached GP data snapshot; caller owns and must remove it.

    Invoke after adding any final-width modifiers. Snapshot includes all enabled
    modifiers, including the camera pixel-radius nodes. Each Line Set is captured
    independently so different material selections never share a face-mark bit.
    """
    modifiers = [modifier for modifier in preview.modifiers if modifier.type == 'LINEART']
    if len(modifiers) != 1:
        raise ValueError('Material snapshot expects one generated Line Art modifier')
    modifier = modifiers[0]
    properties = {'source_type': 'SCENE', 'source_collection': None,
                  'use_face_mark': True, 'use_face_mark_invert': False,
                  'use_face_mark_boundaries': True, 'use_face_mark_keep_contour': False,
                  # Blender 4.4 clip-generated edges bypass face-mark selection,
                  # even on OCCLUSION_ONLY sources. They are artificial camera
                  # cuts, not selected source features; keep all geometry for
                  # occlusion while suppressing only these generated edges.
                  'use_clip_plane_boundaries': False}
    previous = {name: getattr(modifier, name) for name in properties}
    try:
        with scoped_faces(scene, node):
            for name, value in properties.items():
                setattr(modifier, name, value)
            graph = bpy.context.evaluated_depsgraph_get()
            copied = preview.evaluated_get(graph).data.copy()
            copied.name = '.rea pencil material strokes'
            return copied
    finally:
        for name, value in previous.items():
            setattr(modifier, name, value)
