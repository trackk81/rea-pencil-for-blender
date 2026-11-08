"""Material face targets through Blender's public Freestyle selection callback.

The installed parameter_editor provides this hook before its usual edge filters.
Source meshes, face marks and user-owned line sets are never changed.
"""
from . import material_scope


def validate(scene, specifications):
    if not any(material_scope.active(node) for node, _ in specifications):
        return
    for obj in scene.objects:
        if obj.hide_render or obj.get('rea_pencil_preview'):
            continue
        if obj.instance_type != 'NONE' or obj.particle_systems:
            raise ValueError('Material face targets do not yet support collection/vertex/particle instances; use explicit mesh copies')
        if any(mod.show_render and mod.type in {'NODES', 'BOOLEAN'} for mod in obj.modifiers):
            raise ValueError('Material face targets do not yet support Geometry Nodes or Boolean material remapping')


def _before_line_set(scene, layer, line_set):
    from . import backend
    if not line_set.name.startswith(backend.PREFIX) or not scene.lea_line.tree:
        return
    index = int(line_set.name[len(backend.PREFIX):].split(':')[0])
    specifications = backend._specifications(scene.lea_line.tree)
    validate(scene, specifications)
    if index >= len(specifications):
        return
    node = specifications[index][0]
    from . import object_uv,hidden_mask
    if ':HIDDEN' in line_set.name:
        payload=object_uv.cached_payload(scene.name_full,scene.frame_current,line_set.name)
        if payload is not None:hidden_mask.select(payload.get('hidden_mask_names',()))
        else:
            import threading
            if threading.current_thread() is not threading.main_thread():raise RuntimeError('Hidden-mask snapshot is not ready; use F12')
            from . import routing
            effective=routing.line_brush(node,routing.connected_brush(node,'Brush'),'HIDDEN')
            hidden_mask.select(hidden_mask.names(effective))
    if not material_scope.active(node):
        return
    from freestyle.types import Operators, UnaryPredicate1D, Nature
    from parameter_editor import getQualifiedObjectName
    # Freestyle receives a render-scene copy; ID pointers differ from the node
    # graph's originals. Match qualified datablock identities, never pointers.
    materials = {getQualifiedObjectName(mat) for mat in material_scope.target_materials(node)}
    explicit = {getQualifiedObjectName(item.object) for item in node.objects if item.object}
    scope = {getQualifiedObjectName(obj) for obj in node.source_collection.all_objects} if node.source_collection else None
    selected_objects, selected_slots = set(), {}
    for obj in scene.objects:
        name = getQualifiedObjectName(obj)
        if scope is not None and name not in scope:
            continue
        if name in explicit:
            selected_objects.add(name)
        slots = {i for i, slot in enumerate(obj.material_slots) if slot.material and getQualifiedObjectName(slot.material) in materials}
        if slots:
            selected_slots[name] = slots

    class MaterialFaces(UnaryPredicate1D):
        def __call__(self, edge):
            shape = edge.viewshape
            name = shape.library_path + '/' + shape.name if shape.library_path else shape.name
            if name in selected_objects:
                return True
            indices = selected_slots.get(name)
            if not indices:
                return False
            fedge = edge.first_fedge
            while fedge is not None:
                if fedge.is_smooth:
                    selected = fedge.material_index in indices
                else:
                    selected = fedge.material_index_left in indices or (
                        not fedge.nature & Nature.BORDER and fedge.material_index_right in indices)
                if selected:
                    return True
                if fedge == edge.last_fedge:
                    break
                fedge = fedge.next_fedge
            return False

    Operators.select(MaterialFaces())


def register():
    import parameter_editor
    if _before_line_set not in parameter_editor.callbacks_lineset_pre:
        parameter_editor.callbacks_lineset_pre.append(_before_line_set)


def unregister():
    import parameter_editor
    if _before_line_set in parameter_editor.callbacks_lineset_pre:
        parameter_editor.callbacks_lineset_pre.remove(_before_line_set)
