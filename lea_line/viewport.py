"""Optional Blender 4.4 native Grease Pencil Line Art camera preview.

These objects are preview-only. Final Freestyle rendering remains independent;
Line Art geometry, thickness and feature classification are not pixel-identical.
"""
import uuid
import math

import bpy

from . import free_view
from . import backend, routing, material_scope, preview_camera, preview_stroke, preview_material
from .width_profile import live_coverage_supported, live_coverage_width, live_variable_coverage_supported

TAG = "rea_pencil_preview"
OWNER = "rea_pencil_preview_owner"
KEY = "rea_pencil_preview_key"
OWNER_SCENE_KEY = "rea_pencil_preview_id"
SCHEMA_KEY = "rea_pencil_preview_schema"
SCHEMA_VERSION = 1
_last_sync_stats = {}


def last_sync_stats():
    """Writes performed by the last sync; reading this never dirties Blender IDs."""
    return dict(_last_sync_stats)


def _same(current, desired):
    if isinstance(desired, (tuple, list)):
        return len(current) == len(desired) and all(_same(a, b) for a, b in zip(current, desired))
    if isinstance(desired, float):
        return math.isclose(current, desired, rel_tol=1e-6, abs_tol=1e-7)
    return current == desired


def _set(block, name, value, category="geometry_writes"):
    # Assigning an unchanged RNA property can still invalidate dependency graph
    # geometry. Compare the live value instead of caching potentially stale state.
    if _same(getattr(block, name), value):
        return False
    setattr(block, name, value)
    _last_sync_stats[category] = _last_sync_stats.get(category, 0) + 1
    return True


def _owner(scene):
    token = scene.get(OWNER_SCENE_KEY)
    if token and any(other != scene and other.get(OWNER_SCENE_KEY) == token for other in bpy.data.scenes):
        # Scene duplication copies ID properties and may link the same objects.
        # Detach the copy's old preview links before assigning it a fresh owner.
        clear_preview(scene)
        scene[OWNER_SCENE_KEY] = uuid.uuid4().hex
    if not scene.get(OWNER_SCENE_KEY):
        scene[OWNER_SCENE_KEY] = uuid.uuid4().hex
    return scene[OWNER_SCENE_KEY]


def _owned(block, owner):
    return block.get(TAG, False) and block.get(OWNER) == owner


def _scene_collections(scene):
    result = {scene.collection}
    pending = [scene.collection]
    while pending:
        for collection in pending.pop().children:
            if collection not in result:
                result.add(collection)
                pending.append(collection)
    return result


def _remove_object(scene, obj):
    if len(obj.users_scene) > 1:
        local = _scene_collections(scene)
        shared = set().union(*(_scene_collections(other) for other in obj.users_scene if other != scene))
        for collection in list(obj.users_collection):
            if collection in local and collection not in shared:
                collection.objects.unlink(obj)
        # Never mutate a shared child collection to remove another scene's data.
        return
    preview_material.release(obj)
    data = obj.data
    preview_stroke.cleanup(obj)
    preview_camera.cleanup(obj)
    from . import preview_texture
    preview_texture.cleanup(obj)
    bpy.data.objects.remove(obj, do_unlink=True)
    if data and data.users == 0 and data.get(TAG):
        bpy.data.grease_pencils_v3.remove(data)


def clear_preview(scene):
    """Remove only data explicitly tagged as this scene's preview assets."""
    preview_material.cleanup(scene)
    owner = scene.get(OWNER_SCENE_KEY)
    if not owner:
        return
    candidates = [obj for obj in scene.objects if _owned(obj, owner)]
    materials = {material for obj in candidates for material in obj.data.materials if material}
    for obj in candidates:
        if _owned(obj, owner):
            _remove_object(scene, obj)
    for material in materials:
        if _owned(material, owner) and material.users == 0:
            bpy.data.materials.remove(material)


def _material(owner, key):
    material = bpy.data.materials.new("rea pencil preview ink " + key)
    material[TAG] = True
    material[OWNER] = owner
    material[SCHEMA_KEY] = SCHEMA_VERSION
    bpy.data.materials.create_gpencil_data(material)
    return material


def _object(scene, owner, key):
    obj = next((obj for obj in scene.objects if _owned(obj, owner) and obj.get(KEY) == key), None)
    if obj is None:
        data = bpy.data.grease_pencils_v3.new("rea pencil preview strokes")
        data[TAG] = True
        data[OWNER] = owner
        data[SCHEMA_KEY] = SCHEMA_VERSION
        obj = bpy.data.objects.new("rea pencil preview " + key, data)
        obj[TAG] = True
        obj[OWNER] = owner
        obj[KEY] = key
        obj[SCHEMA_KEY] = SCHEMA_VERSION
        scene.collection.objects.link(obj)
        layer = data.layers.new("Lines", set_active=True)
        layer.frames.new(scene.frame_current)
        layer.use_lights = False
        material = _material(owner, key)
        data.materials.append(material)
        obj.modifiers.new("rea pencil native Line Art", "LINEART")
        _last_sync_stats["objects_created"] += 1
    _set(obj, "hide_render", True, "visibility_writes")
    _set(obj, "hide_viewport", bool(obj.get("rea_stamp_zero_alpha",False)), "visibility_writes")
    if obj.hide_get(view_layer=scene.view_layers[0]):
        obj.hide_set(False, view_layer=scene.view_layers[0])
        _last_sync_stats["visibility_writes"] += 1
    return obj


@free_view.context
def sync_preview(scene, material_render=False):
    """Synchronize connected line sets into live native Line Art modifiers.

    Returns a list of limitations/warnings. Does not render, launch a process,
    change the selection, or alter any source mesh/material.
    """
    from . import preview_features
    _last_sync_stats.clear()
    _last_sync_stats.update(geometry_writes=0, material_writes=0, visibility_writes=0, objects_created=0)
    if preview_material.is_suspended():
        # Blender may invoke NodeTree.update while undo/load is decoding IDs.
        # Creating a private Scene/camera in that interval can crash its window
        # scene synchronization before the corresponding post handler runs.
        return ['Live preview refresh is deferred during Blender scene restoration.']
    if bpy.app.version[:2] != (4, 4):
        raise ValueError("Native preview has been validated for Blender 4.4 only")
    tree = scene.lea_line.tree
    if tree is None:
        clear_preview(scene)
        return []
    specifications = backend._specifications(tree)
    if free_view.camera(scene) is None:
        clear_preview(scene)
        return ["Viewport preview needs a scene camera; set the active camera first."]
    if free_view.camera(scene).data.type not in {'PERSP', 'ORTHO'}:
        clear_preview(scene)
        return ["Live preview supports perspective and orthographic scene cameras."]
    owner = _owner(scene)
    desired = set()
    warnings = ['Live preview uses native Line Art; chaining and antialiasing differ from final Freestyle lines.']
    for index, (node, brush) in enumerate(specifications):
        if free_view.dimensions(scene) and (material_scope.active(node) or node.use_material_functions):
            raise ValueError('Free View material-face evaluation is not yet verified; select Scene Camera')
        material_live = material_scope.active(node) and not material_render
        target_collection = backend.source_collection(scene, node)
        for visibility, enabled in (("VISIBLE", node.visible), ("HIDDEN", node.hidden)):
            if not enabled:
                continue
            if visibility=='VISIBLE' and getattr(scene.lea_line,'preview_feature_brushes',False) and not material_render:
                mixed=preview_features.sync(scene,node,brush,index)
                if mixed:
                    warnings.extend(mixed['warnings'])
                    if mixed['handled']:
                        desired.update(mixed['keys'])
                        continue
            features = {feature: routing.feature_enabled(node, feature, visibility)
                        for feature, _ in routing.FEATURES}
            features['intersection'] = routing.feature_enabled(node, 'intersection', visibility)
            if features['outline'] != features['border']:
                warnings.append(f"{node.name} ({visibility.lower()}): native preview groups outline/open-border contours; independent selection is unavailable.")
            if features['ridge_valley'] or features['suggestive_contour']:
                warnings.append(f"{node.name} ({visibility.lower()}): ridge/valley and suggestive contours are final-render-only.")
            selected_brush, deferred_override = routing.live_brush(node, brush, visibility)
            from . import stamp_brush,stamp_constant_alpha
            if (stamp_brush.active(selected_brush) and visibility=='VISIBLE'
                    and not material_render and not free_view.dimensions(scene) and not node.use_material_functions
                    and not getattr(selected_brush,'distance_alpha',False)):
                selected_brush=stamp_constant_alpha.fold(selected_brush)

            if free_view.dimensions(scene):
                from . import texture_maps
                if texture_maps.maps(selected_brush):raise ValueError('Free View texture mapping is not yet verified; select Scene Camera')
            if deferred_override:
                warnings.append(f"{node.name} ({visibility.lower()}): live feature brushes require a sole marked-edge, crease or material-boundary selection; this selection uses the common brush.")
            width = selected_brush.thickness * backend.width_scale(scene, node)
            key = f"{index}:{visibility}"
            desired.add(key)
            obj = _object(scene, owner, key)
            if material_render:
                from . import preview_texture
                preview_texture.cleanup(obj)
                # A detached live snapshot must never become native Line Art's
                # input strokes. Hold it across final staging, including failure.
                preview_material.stage(obj)
            elif not material_live:
                # Changing targets back to objects returns to an empty native
                # Line Art base and releases this set's private evaluator.
                preview_material.release(obj, reset_geometry=True)
            layer = obj.data.layers[0]
            _set(layer, "use_lights", False, "material_writes")
            material = obj.data.materials[0] if obj.data.materials else None
            if material is None or not _owned(material, owner):
                # Undo may also drop the material tags. Never claim or edit an
                # unowned ID; replace only our generated object's material slot.
                material = _material(owner, key)
                if obj.data.materials:
                    obj.data.materials[0] = material
                else:
                    obj.data.materials.append(material)
                _last_sync_stats['material_writes'] += 1
            # Blender 4.4 undo can restore the Material ID without its GP style.
            # Repair this generated asset before reading/writing stroke fields.
            if material.grease_pencil is None:
                bpy.data.materials.create_gpencil_data(material)
                _last_sync_stats['material_writes'] += 1
            from . import stamp_brush
            stamp_brush.sync_material(material,selected_brush,width,_set)
            # Native reference probes apply scalar alpha after the stamp mask
            # is combined within a Line Set. Native and material snapshots are
            # verified for camera/visible scalar alpha, with curve alpha retained
            # on points. Keep the other unverified routes on their existing path.
            stamp_layer_alpha = (stamp_brush.active(selected_brush) and visibility == 'VISIBLE'
                                 and not material_render
                                 and not free_view.dimensions(scene) and not node.use_material_functions
                                 and not getattr(selected_brush, 'distance_alpha', False))
            _set(layer, 'opacity', selected_brush.alpha if stamp_layer_alpha else 1., 'material_writes')
            ink = material.grease_pencil
            _set(ink, "show_stroke", True, "material_writes")
            _set(ink, "show_fill", False, "material_writes")
            # Keep material alpha opaque. Scalar alpha belongs to either the
            # layer or generated points, and must not be applied a second time.
            _set(ink, "color", (*selected_brush.color, 1.0), "material_writes")
            modifier = next(mod for mod in obj.modifiers if mod.type == "LINEART")
            geometry = {
                "target_layer": layer.name,
                "target_material": material,
                "source_type": "COLLECTION" if target_collection is not None else "SCENE",
                "source_collection": target_collection,
                "use_custom_camera": True,
                "source_camera": free_view.camera(scene),
                "use_contour": features['outline'] or features['border'],
                "use_crease": features['crease'],
                "crease_threshold": node.crease_angle,
                "use_crease_on_smooth": True,
                "use_crease_on_sharp": True,
                "use_material": features['material_boundary'],
                "use_edge_mark": features['edge_mark'],
                "use_intersection": features['intersection'],
                "use_loose": False,
                "use_shadow": False,
                "use_light_contour": False,
                # Clipping a large face must not invent a geometric edge at the
                # camera near/far plane (observed on the comparison ground plane).
                "use_clip_plane_boundaries": False,
                "use_multiple_levels": visibility == "HIDDEN",
                "level_start": 0 if visibility == "VISIBLE" else 1,
                "level_end": 0 if visibility == "VISIBLE" else 128,
                "opacity": 1. if stamp_layer_alpha else selected_brush.alpha,
                "thickness": max(1, min(200, round(width))),
                "overscan": 1. if free_view.dimensions(scene) else 0.,
                "use_cache": False,  # Independent sets have independent filters.
                "show_viewport": not material_live,
                "show_render": not material_live,  # Object is excluded from final renders.
            }
            for name, value in geometry.items():
                _set(modifier, name, value)
            if material_live:
                result = preview_material.sync(scene, node, obj)
                _last_sync_stats['geometry_writes'] += result.get('writes', int(result.get('changed', False)))
                warnings.extend(result.get('warnings', ()))
                if not result.get('ok', True) and not result.get('preserved', False):
                    desired.discard(key)
                    _remove_object(scene, obj)
                    continue
            if material_render:
                # Fast final rendering owns its own coverage/width stage.
                # Keep live topology intact, but exclude it from both evaluations.
                preview_camera.set_enabled(obj, False, _set)
                preview_stroke.set_enabled(obj, False, _set)
            else:
                live_width = width
                if getattr(scene.lea_line, 'preview_simple_coverage', False):
                    if live_coverage_supported(selected_brush):
                        live_width = live_coverage_width(width)
                    elif not live_variable_coverage_supported(selected_brush):
                        warnings.append('Simple live coverage supports uniform solid strokes or directed open size curves; unsupported styles keep geometric width.')
                _last_sync_stats['geometry_writes'] += preview_camera.sync(obj, scene, live_width, _set)
                result = preview_stroke.sync(obj, selected_brush, scene, backend.width_scale(scene, node), _set)
                _last_sync_stats['geometry_writes'] += result['structural_writes'] + result['curve_writes']
                warnings.extend(result['warnings'])
                from . import preview_texture
                preview_texture.sync(obj,selected_brush,scene,node)
            zero_alpha=stamp_brush.active(selected_brush) and selected_brush.alpha==0
            if bool(obj.get('rea_stamp_zero_alpha',False))!=zero_alpha:obj['rea_stamp_zero_alpha']=zero_alpha
            _set(obj,'hide_viewport',zero_alpha,'visibility_writes')
            # RNA setters already tag the precise affected ID; an unconditional
            # obj.update_tag() here would force geometry on color-only/no-op sync.
    for obj in list(scene.objects):
        if _owned(obj, owner) and obj.get(KEY) not in desired:
            _remove_object(scene, obj)
    for material in list(bpy.data.materials):
        if _owned(material, owner) and material.users == 0:
            bpy.data.materials.remove(material)
    preview_features.prune(scene,desired)
    if not material_render:
        from . import curves, preview_runtime
        curves.mark_synced(scene)
        preview_runtime.mark_synced(scene)
    return list(dict.fromkeys(warnings))
