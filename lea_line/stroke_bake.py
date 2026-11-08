"""Render the current evaluated Line Art strokes once when scene views agree.

No cross-frame cache: every render evaluates its own geometry and camera. The
viewport graph is only used when its source geometry matches render settings.
"""
import time
from contextlib import contextmanager
import bpy


def ineligible_reason(scene):
    from . import backend
    if any(callback is not backend._render_pre for callback in bpy.app.handlers.render_pre):
        return 'Other render-pre handlers may change geometry after stroke preparation'
    if scene.render.use_simplify:
        return 'Render simplification differs from viewport evaluation'
    layer = bpy.context.view_layer
    def layer_hidden(branch):
        if branch.exclude:
            return True
        return branch.hide_viewport or any(layer_hidden(child) for child in branch.children)
    if layer_hidden(layer.layer_collection):
        return 'Hidden or excluded layer collections require native render evaluation'
    seen = set()
    def visit(collection):
        if collection in seen:
            return None
        seen.add(collection)
        if collection.hide_viewport != collection.hide_render:
            return 'Collection render/viewport visibility differs'
        for obj in collection.objects:
            if obj.get('rea_pencil_preview'):
                continue
            if obj.hide_render != obj.hide_viewport:
                return 'Object render/viewport visibility differs'
            if obj.hide_render:
                continue
            if obj.show_instancer_for_render != obj.show_instancer_for_viewport:
                return 'Instancer render/viewport visibility differs'
            if scene.objects.get(obj.name) == obj and obj.hide_get(view_layer=layer):
                return 'Temporarily hidden objects require native render evaluation'
            if obj.type not in {'MESH', 'LIGHT', 'CAMERA', 'EMPTY'}:
                return 'Non-mesh geometry requires native render evaluation'
            if obj.particle_systems:
                return 'Particle geometry requires native render evaluation'
            for modifier in obj.modifiers:
                if modifier.show_render != modifier.show_viewport:
                    return 'Modifier render/viewport visibility differs'
                if not modifier.show_render:
                    continue
                if modifier.type not in {'SUBSURF', 'ARMATURE', 'BEVEL', 'MIRROR', 'SOLIDIFY', 'ARRAY', 'TRIANGULATE', 'WEIGHTED_NORMAL', 'SMOOTH', 'LAPLACIANSMOOTH', 'DISPLACE', 'CAST', 'SIMPLE_DEFORM', 'WAVE', 'DECIMATE'}:
                    return 'Unvalidated modifier requires native render evaluation'
                if modifier.type == 'SUBSURF' and modifier.levels != modifier.render_levels:
                    return 'Subdivision levels differ'
            if obj.instance_type == 'COLLECTION' and obj.instance_collection:
                reason = visit(obj.instance_collection)
                if reason:
                    return reason
        for child in collection.children:
            reason = visit(child)
            if reason:
                return reason
        return None
    return visit(scene.collection)


@contextmanager
def prepared(scene, objects, material_nodes=None):
    started = time.perf_counter()
    reason = ineligible_reason(scene)
    if not reason:
        for obj in objects:
            for modifier in obj.modifiers:
                if not modifier.show_render and not modifier.show_viewport:
                    continue
                if modifier.show_render != modifier.show_viewport:
                    reason = 'Stroke modifier render/viewport visibility differs'
                elif modifier.type != 'LINEART' and not (modifier.type == 'NODES' and modifier.node_group
                        and modifier.node_group.get('rea_pixel_width_owned')):
                    reason = 'Custom stroke modifier requires native render evaluation'
    report = {'used': False, 'reason': reason, 'prepare_seconds': 0.0}
    if reason:
        yield report
        return
    replacements = []
    try:
        material_nodes = material_nodes or {}
        from . import material_scope
        # Snapshot all outputs before swapping any ID or tagging the graph.
        # Otherwise a later evaluation could invalidate an earlier snapshot.
        for obj in objects:
            node = material_nodes.get(obj.name)
            if node is not None:
                copied = material_scope.capture_strokes(scene, node, obj)
            else:
                graph = bpy.context.evaluated_depsgraph_get()
                copied = obj.evaluated_get(graph).data.copy()
            replacements.append((obj, obj.data, copied,
                                 [(mod, mod.show_render, mod.show_viewport) for mod in obj.modifiers]))
        for obj, previous, copied, flags in replacements:
            obj.data = copied
            for modifier, _, _ in flags:
                modifier.show_render = modifier.show_viewport = False
        report.update(used=True, prepare_seconds=time.perf_counter() - started,
                      strokes=sum(len(frame.drawing.strokes) for _, _, copied, _ in replacements
                                  for layer in copied.layers for frame in layer.frames))
        yield report
    finally:
        for obj, previous, copied, flags in replacements:
            obj.data = previous
            for modifier, show_render, show_viewport in flags:
                modifier.show_render, modifier.show_viewport = show_render, show_viewport
            if copied.users == 0:
                bpy.data.grease_pencils_v3.remove(copied)
