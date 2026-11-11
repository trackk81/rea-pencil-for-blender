"""Isolated, coalesced material-target Line Art evaluation for live previews.

Source Object.data and authored face marks are never swapped or edited. Each
generated preview has an owned private scene with full evaluated mesh proxies,
including unselected occluders. Only a successful detached GP capture replaces
the last valid output. All Blender work runs on the main thread.
"""
import uuid
import bpy
from .blender_compat import grease_pencil_data, lineart_width_property, write_mesh_marks
import numpy as np
from bpy.app.handlers import persistent
from . import free_view, material_scope, native_core

TAG = 'rea_private_material'
OWNER = 'rea_private_material_owner'
SOURCE = 'rea_private_material_source'
OUTPUT = 'rea_material_live_snapshot'
SCHEMA = 1
_entries = {}
_pending = {}
_stages = {}
_subscriptions = {}
_counters = {}
_revisions = {}
_source_states = {}
_registered = False
_busy = 0
_paused = False
_paused_geometry = set()
LINE_PROPERTIES = ('target_layer', 'target_material', 'use_contour', 'use_crease',
    'crease_threshold', 'use_crease_on_smooth', 'use_crease_on_sharp', 'use_material',
    'use_edge_mark', 'use_intersection', 'use_loose', 'use_shadow', 'use_light_contour',
    'use_multiple_levels', 'level_start', 'level_end', 'opacity', 'use_cache',
    'stroke_depth_offset', 'overscan')
CAMERA_PROPERTIES = ('type', 'lens', 'ortho_scale', 'sensor_fit', 'sensor_width',
    'sensor_height', 'shift_x', 'shift_y', 'clip_start', 'clip_end')
RENDER_PROPERTIES = ('resolution_x', 'resolution_y', 'resolution_percentage', 'pixel_aspect_x', 'pixel_aspect_y')


def _valid(block):
    try:
        return block is not None and bool(block.as_pointer())
    except ReferenceError:
        return False


def _pointer(block):
    return block.as_pointer() if _valid(block) else 0


def _owned(block, token=None):
    try:
        return bool(block and block.get(TAG) == SCHEMA and (token is None or block.get(OWNER) == token))
    except (ReferenceError, AttributeError):
        return False


def _preview_owned(preview):
    return _valid(preview) and preview.get('rea_pencil_preview') and preview.get('rea_pencil_preview_owner')


def _output_owned(preview, data=None):
    data = data if data is not None else preview.data
    return bool(_preview_owned(preview) and _valid(data) and data.get(OUTPUT)
                and data.get('rea_pencil_preview_owner') == preview.get('rea_pencil_preview_owner'))


def _tag(block, token, source):
    block[TAG], block[OWNER], block[SOURCE] = SCHEMA, token, source
    return block


def _count(scene, name, amount=1):
    row = _counters.setdefault(_pointer(scene), {})
    row[name] = row.get(name, 0) + amount


def stats(scene=None):
    rows = [_counters.get(_pointer(scene), {})] if scene else list(_counters.values())
    result = {name: sum(row.get(name, 0) for row in rows) for name in
              ('mesh_copies', 'evaluations', 'refreshes', 'failures', 'noops', 'transform_updates')}
    result['cache_entries'] = sum(_valid(entry.source) and (scene is None or entry.source == scene)
                                  for entry in _entries.values())
    result['private_scenes'] = sum(_owned(s) and (scene is None or s.get(SOURCE) == scene.get('rea_pencil_preview_id'))
                                   for s in bpy.data.scenes)
    result['pending_scenes'] = len(_pending)
    return result


def is_suspended():
    """True while Blender is decoding undo/load data or serializing/rendering."""
    return _paused


def unsupported_reason(scene, node=None):
    """Bounded viewport-only support; reject unproven visibility/instance paths."""
    camera = free_view.camera(scene)
    if not camera or camera.data.type not in {'ORTHO', 'PERSP'}:
        return 'Material live preview needs a perspective or orthographic view.'
    if len(scene.view_layers) != 1:
        return 'Material live preview currently supports one view layer.'
    if scene.render.use_simplify:
        return 'Material live preview does not support scene simplification yet.'
    layer = scene.view_layers[0]
    def hidden(branch):
        return branch.exclude or branch.hide_viewport or any(hidden(child) for child in branch.children)
    if hidden(layer.layer_collection):
        return 'Material live preview does not support hidden or excluded layer collections yet.'
    safe = {'SUBSURF', 'ARMATURE', 'MIRROR', 'TRIANGULATE', 'SMOOTH', 'LAPLACIANSMOOTH',
            'DISPLACE', 'CAST', 'SIMPLE_DEFORM', 'WAVE', 'WEIGHTED_NORMAL'}
    for obj in scene.objects:
        if obj.get('rea_pencil_preview') or _owned(obj):
            continue
        if obj.instance_type != 'NONE' or obj.particle_systems:
            return 'Material live preview does not support instances or particles yet.'
        if obj.type not in {'MESH', 'CAMERA', 'LIGHT', 'EMPTY'}:
            return 'Material live preview currently supports mesh sources only.'
        if obj.type != 'MESH':
            continue
        if obj.mode != 'OBJECT':
            return 'Material live preview refresh is paused while a source mesh is in Edit Mode.'
        if obj.hide_viewport or obj.hide_render or obj.hide_get(view_layer=layer) or not obj.visible_get(view_layer=layer):
            return 'Material live preview does not support hidden mesh sources yet.'
        for modifier in obj.modifiers:
            if modifier.show_viewport and modifier.type not in safe:
                return 'Material live preview cannot safely evaluate this mesh modifier yet.'
    return None


def _active_material_scene(scene):
    if not _valid(scene) or _owned(scene):
        return False
    settings = getattr(scene, 'lea_line', None)
    from . import object_uv
    return bool(settings and settings.preview_enabled and settings.tree and
                (getattr(settings,'preview_feature_brushes',False) or
                 object_uv.uses(scene) or
                 any(node.bl_idname == 'LEALineSetNode' and len(node.materials) for node in settings.tree.nodes)))


def subscribe(scene):
    _subscriptions[_pointer(scene)]=scene
    _source_states.setdefault(_pointer(scene),_source_state(scene))


def revision(scene):
    return _revisions.get(_pointer(scene),0)


def _source_state(scene):
    """Nongeometry inputs; generated outputs must not invalidate themselves."""
    def source(obj):return not obj.get('rea_pencil_preview') and not _owned(obj)
    from . import object_uv
    uv_active=object_uv.uses(scene)
    objects=[]
    layer=scene.view_layers[0]
    for obj in scene.objects:
        if not source(obj):continue
        objects.append((_pointer(obj),_pointer(obj.data),obj.type,_matrix(obj.matrix_world),obj.mode,
            obj.hide_render,obj.hide_viewport,obj.hide_get(view_layer=layer),obj.visible_get(view_layer=layer),
            obj.instance_type,len(obj.particle_systems),obj.lineart.usage,
            tuple(_pointer(s.material) for s in obj.material_slots),
            tuple((m.as_pointer(),m.type,m.show_viewport,m.show_render) for m in obj.modifiers),
            (tuple(layer.name for layer in obj.data.uv_layers),tuple((a.name,a.domain,a.data_type) for a in obj.data.color_attributes)) if uv_active and obj.type=='MESH' else ()))
    collections=[];pending=[scene.collection]
    while pending:
        collection=pending.pop();pending.extend(collection.children)
        collections.append((_pointer(collection),collection.hide_viewport,collection.hide_render,
            tuple(sorted(_pointer(o) for o in collection.objects if source(o))),
            tuple(sorted(_pointer(c) for c in collection.children))))
    def layers(branch):return (branch.collection.as_pointer(),branch.exclude,branch.hide_viewport,tuple(layers(c) for c in branch.children))
    camera=scene.camera
    projection=(_pointer(camera),_pointer(camera.data),tuple(getattr(camera.data,n) for n in CAMERA_PROPERTIES)) if camera else None
    return (scene.frame_current,projection,tuple(getattr(scene.render,n) for n in RENDER_PROPERTIES),scene.render.use_simplify,
            tuple((l.as_pointer(),layers(l.layer_collection)) for l in scene.view_layers),tuple(sorted(objects)),tuple(sorted(collections)))


def _remove_private(token):
    scenes = [s for s in list(bpy.data.scenes) if _owned(s, token)]
    objects = []
    for obj in list(bpy.data.objects):
        if not _owned(obj, token):
            continue
        # A user-linked private object is no longer exclusively in our scene.
        if any(not _owned(s, token) for s in obj.users_scene):
            continue
        objects.append(obj)
    # Unlinking each proxy separately repeatedly tags the dependency graph.
    # Only exclusively private IDs admitted by the same ownership guard enter
    # this batch; user-linked objects and their data remain untouched.
    if objects:
        bpy.data.batch_remove(ids=objects)
    for scene in scenes:
        if _valid(scene):
            bpy.data.scenes.remove(scene)
    orphans = []
    for collection in (bpy.data.meshes, bpy.data.cameras, grease_pencil_data()):
        for block in list(collection):
            if _owned(block, token) and block.users == 0:
                orphans.append(block)
    if orphans:
        bpy.data.batch_remove(ids=orphans)


def _empty_data(preview, scene):
    data = grease_pencil_data().new('.rea material empty Line Art base')
    data['rea_pencil_preview'] = True
    data['rea_pencil_preview_owner'] = preview.get('rea_pencil_preview_owner')
    data['rea_pencil_preview_schema'] = 1
    layer = data.layers.new('Lines', set_active=True)
    layer.use_lights = False
    layer.frames.new(scene.frame_current)
    for material in preview.data.materials:
        data.materials.append(material)
    return data


def _remove_output(data):
    if (_valid(data) and data.users == 0 and data.get('rea_pencil_preview')
            and not any(stage['snapshot'] == data for stage in _stages.values())):
        grease_pencil_data().remove(data)


def _matrix(value):
    return tuple(v for row in value for v in row)


def _signature(scene, node, preview):
    modifier = next(m for m in preview.modifiers if m.type == 'LINEART')
    camera = free_view.camera(scene)
    values = tuple((_pointer(getattr(modifier, name)) if name == 'target_material' else getattr(modifier, name))
                   for name in (*LINE_PROPERTIES, lineart_width_property(modifier)))
    objects = tuple((_pointer(o), _pointer(o.data), _matrix(o.matrix_world), o.lineart.usage,
                     tuple(_pointer(s.material) for s in o.material_slots))
                    for o in scene.objects if o.type == 'MESH' and not o.get('rea_pencil_preview'))
    targets = (tuple(sorted(_pointer(i.object) for i in node.objects if i.object)),
               tuple(sorted(_pointer(i.material) for i in node.materials if i.material)),
               _pointer(node.source_collection),getattr(node,'target_mode','ALL'),
               tuple(sorted(_pointer(o) for o in node.source_collection.all_objects)) if node.source_collection else ())
    return (scene.frame_current, _pointer(camera), _matrix(camera.matrix_world),
            tuple(getattr(camera.data, name) for name in CAMERA_PROPERTIES),
            tuple(getattr(scene.render, name) for name in RENDER_PROPERTIES),
            free_view.dimensions(scene), values, objects, targets)


class _Entry:
    def __init__(self, scene, preview):
        self.source, self.preview = scene, preview
        self.token = uuid.uuid4().hex
        self.source_token = scene.get('rea_pencil_preview_id', '')
        self.scene = self.camera = self.ink = None
        self.proxies = {}
        self.signature = None
        self.dirty = True
        self.mesh_keys = None  # None means conservative full refresh.

    def close_private(self):
        _remove_private(self.token)
        self.scene = self.camera = self.ink = None
        self.proxies.clear()

    def face_mask(self,node,source,proxy,allowed,explicit,materials):
        return native_core.mesh_material_mask(proxy.data,[s.material in materials for s in source.material_slots],
                                               allowed=allowed,explicit=allowed and source in explicit)

    def _create(self, preview):
        source = self.source
        self.scene = _tag(bpy.data.scenes.new('.rea private material evaluation'), self.token, self.source_token)
        self.scene.render.engine = source.render.engine
        camera_data = _tag(free_view.camera(source).data.copy(), self.token, self.source_token)
        # This is an evaluated static camera proxy, not a second animation rig.
        # A copied driver must never re-evaluate against the private scene.
        camera_data.animation_data_clear()
        self.camera = _tag(bpy.data.objects.new('.rea private material camera', camera_data), self.token, self.source_token)
        self.scene.collection.objects.link(self.camera)
        self.scene.camera = self.camera
        # Never copy a LineArt Object (Blender 4.4 copy_data crashes), and never
        # copy a nonempty live snapshot into the evaluation base.
        data = _tag(grease_pencil_data().new('.rea private empty ink'), self.token, self.source_token)
        layer = data.layers.new('Lines', set_active=True)
        layer.use_lights = False
        layer.frames.new(source.frame_current)
        for material in preview.data.materials:
            data.materials.append(material)
        self.ink = _tag(bpy.data.objects.new('.rea private material ink', data), self.token, self.source_token)
        self.scene.collection.objects.link(self.ink)
        self.ink.modifiers.new('.rea private native Line Art', 'LINEART')

    def _refresh_meshes(self, depsgraph):
        created = []
        try:
            sources = [o for o in self.source.objects if o.type == 'MESH' and not o.get('rea_pencil_preview')]
            for source in sources:
                key = _pointer(source)
                if self.mesh_keys is not None and key in self.proxies and key not in self.mesh_keys:
                    continue
                evaluated = source.evaluated_get(depsgraph)
                # Sources are admitted as meshes only. Copy the evaluated mesh
                # directly: new_from_object leaked native allocations after a
                # render/refresh cycle in Blender4.4 (shared Subsurf fixtures).
                mesh = evaluated.data.copy()
                _tag(mesh, self.token, self.source_token)
                created.append((source, mesh))
                # Do not clear material slots: Blender then resets face indices.
                for i, slot in enumerate(source.material_slots):
                    if i < len(mesh.materials):
                        mesh.materials[i] = slot.material
                    else:
                        mesh.materials.append(slot.material)
            for source, mesh in created:
                previous = self.proxies.get(_pointer(source))
                if previous:
                    proxy = previous['proxy']
                    old = proxy.data
                    proxy.data = mesh
                    if _owned(old, self.token) and old.users == 0:
                        bpy.data.meshes.remove(old)
                else:
                    proxy = _tag(bpy.data.objects.new('.rea proxy ' + source.name, mesh), self.token, self.source_token)
                    self.scene.collection.objects.link(proxy)
                self.proxies[_pointer(source)] = {'source': source, 'proxy': proxy, 'mask': None}
                _count(self.source, 'mesh_copies')
            live = {_pointer(o) for o in sources}
            for key in set(self.proxies) - live:
                item = self.proxies.pop(key)
                proxy, mesh = item['proxy'], item['proxy'].data
                if _owned(proxy, self.token):
                    bpy.data.objects.remove(proxy, do_unlink=True)
                if _owned(mesh, self.token) and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
        except Exception:
            for _, mesh in created:
                if _valid(mesh) and mesh.users == 0 and _owned(mesh, self.token):
                    bpy.data.meshes.remove(mesh)
            raise

    def capture(self, node, preview, geometry_dirty=False):
        fresh = not _valid(self.scene)
        if fresh:
            self._create(preview)
        layer = self.source.view_layers[0]
        with bpy.context.temp_override(scene=self.source, view_layer=layer):
            layer.update()
            depsgraph = bpy.context.evaluated_depsgraph_get()
            keys = {_pointer(o) for o in self.source.objects if o.type == 'MESH' and not o.get('rea_pencil_preview')}
            if fresh or geometry_dirty or keys != set(self.proxies):
                self._refresh_meshes(depsgraph)
            source_camera = free_view.camera(self.source).evaluated_get(depsgraph)
            self.camera.matrix_world = source_camera.matrix_world
            for name in CAMERA_PROPERTIES:
                setattr(self.camera.data, name, getattr(source_camera.data, name))
            for name in RENDER_PROPERTIES:
                setattr(self.scene.render, name, getattr(self.source.render, name))
            dimensions = free_view.dimensions(self.source)
            if dimensions:
                render = self.scene.render
                render.resolution_x, render.resolution_y = dimensions
                render.resolution_percentage = 100
                render.pixel_aspect_x = render.pixel_aspect_y = 1.
            # frame_set evaluates immediately even when the frame is unchanged.
            # The final private layer update below still evaluates all proxies.
            if self.scene.frame_current != self.source.frame_current or self.scene.frame_subframe != 0:
                self.scene.frame_set(self.source.frame_current)
            if not self.ink.data.layers[0].frames:
                self.ink.data.layers[0].frames.new(self.source.frame_current)
            self.ink.matrix_world = preview.matrix_world
            materials = material_scope.target_materials(node)
            explicit = {i.object for i in node.objects if i.object}
            scope = set(node.source_collection.all_objects) if node.source_collection else None
            for item in self.proxies.values():
                source, proxy = item['source'], item['proxy']
                proxy.matrix_world = source.evaluated_get(depsgraph).matrix_world
                proxy.lineart.usage = source.lineart.usage
                # Object-linked material-slot edits can be shading-only updates.
                # Refresh palette references without copying mesh topology.
                for i, slot in enumerate(source.material_slots):
                    if i < len(proxy.data.materials):
                        if proxy.data.materials[i] != slot.material:
                            proxy.data.materials[i] = slot.material
                    else:
                        proxy.data.materials.append(slot.material)
                while len(proxy.data.materials) > len(source.material_slots):
                    proxy.data.materials.pop(index=len(proxy.data.materials)-1)
                allowed = scope is None or source in scope
                mask = self.face_mask(node,source,proxy,allowed,explicit,materials)
                if item['mask'] is None or not np.array_equal(item['mask'], mask):
                    write_mesh_marks(proxy.data,'FACE',mask)
                    proxy.data.update()
                    item['mask'] = mask.copy()
            _count(self.source, 'transform_updates')
        source_modifier = next(m for m in preview.modifiers if m.type == 'LINEART')
        modifier = self.ink.modifiers[0]
        # Preserve post-accumulation opacity when copying evaluated raw ink.
        # The private graph evaluates geometry, so layer alpha is not baked
        # into the points and must travel with the copied layer.
        self.ink.data.layers[0].opacity = preview.data.layers[0].opacity
        for name in (*LINE_PROPERTIES, lineart_width_property(source_modifier)):
            setattr(modifier, name, getattr(source_modifier, name))
        modifier.target_layer = self.ink.data.layers[0].name
        modifier.source_type, modifier.source_collection = 'SCENE', None
        modifier.use_custom_camera, modifier.source_camera = True, self.camera
        modifier.use_face_mark, modifier.use_face_mark_invert = True, False
        modifier.use_face_mark_boundaries, modifier.use_face_mark_keep_contour = True, False
        modifier.use_clip_plane_boundaries = False
        # Match a repaired/replaced owned ink material without copying old strokes.
        for i, material in enumerate(preview.data.materials):
            if i < len(self.ink.data.materials):
                self.ink.data.materials[i] = material
            else:
                self.ink.data.materials.append(material)
        with bpy.context.temp_override(scene=self.scene, view_layer=self.scene.view_layers[0]):
            self.scene.view_layers[0].update()
            depsgraph = bpy.context.evaluated_depsgraph_get()
            if depsgraph.scene_eval.original != self.scene:
                raise RuntimeError('Private material evaluation selected the wrong scene')
            snapshot = self.ink.evaluated_get(depsgraph).data.copy()
        _count(self.source, 'evaluations')
        return snapshot


def stage(preview):
    """Hold the valid live snapshot and give final Line Art an empty base."""
    if not _output_owned(preview) or _pointer(preview) in _stages:
        return False
    scene = next((s for s in preview.users_scene if not _owned(s)), None)
    if scene is None:
        return False
    snapshot = preview.data
    empty = _empty_data(preview, scene)
    _stages[_pointer(preview)] = {'preview': preview, 'snapshot': snapshot,
                                'empty': empty, 'fake_user': snapshot.use_fake_user}
    snapshot.use_fake_user = True
    preview.data = empty
    return True


def _restore_stage(preview):
    staged = _stages.pop(_pointer(preview), None)
    if not staged:
        return False
    snapshot, empty = staged['snapshot'], staged['empty']
    if _valid(snapshot) and _preview_owned(preview):
        current = preview.data
        preview.data = snapshot
        snapshot.use_fake_user = staged['fake_user']
        _remove_output(current)
    _remove_output(empty)
    return True


def _discard_stage(preview):
    """Release a held snapshot without changing the caller's current GP data."""
    staged = _stages.pop(_pointer(preview), None)
    if staged and _valid(staged['snapshot']):
        staged['snapshot'].use_fake_user = staged['fake_user']
        _remove_output(staged['snapshot'])


def sync(scene, node, preview):
    """Refresh raw ink transactionally; native post-LineArt style stays intact."""
    global _busy
    result = {'ok': False, 'changed': False, 'preserved': False, 'warnings': [], 'writes': 0}
    if not _preview_owned(preview):
        result['warnings'] = ['Material live preview refuses an unowned output object.']
        return result
    if _paused:
        result['preserved'] = _output_owned(preview)
        result['warnings'] = ['Material live preview refresh is suspended during scene lifecycle changes.']
        return result
    _busy += 1
    try:
        subscribe(scene)
        restored = _restore_stage(preview)
        result['changed'], result['writes'] = restored, int(restored)
        result['preserved'] = _output_owned(preview)
        for modifier in preview.modifiers:
            if modifier.type == 'LINEART':
                for name in ('show_viewport', 'show_render'):
                    if getattr(modifier, name):
                        setattr(modifier, name, False)
                        result['writes'] += 1
        reason = unsupported_reason(scene, node)
        if reason:
            release(preview)
            result['warnings'] = [reason + (' Previous valid ink is retained.' if result['preserved'] else '')]
            return result
        key = (_pointer(scene), _pointer(preview))
        entry = _entries.get(key)
        if entry is None:
            entry = _entries[key] = _Entry(scene, preview)
        signature = _signature(scene, node, preview)
        if (not entry.dirty and entry.signature == signature
                and _output_owned(preview)):
            _count(scene, 'noops')
        else:
            snapshot = None
            try:
                snapshot = entry.capture(node, preview, geometry_dirty=entry.dirty)
                snapshot.name = '.rea material live strokes'
                # A copied evaluated private ID inherits its tags; replace them.
                for name in (TAG, OWNER, SOURCE):
                    if name in snapshot:
                        del snapshot[name]
                snapshot[OUTPUT] = True
                snapshot['rea_pencil_preview'] = True
                snapshot['rea_pencil_preview_owner'] = preview.get('rea_pencil_preview_owner')
                snapshot['rea_pencil_preview_schema'] = 1
                for layer in snapshot.layers:
                    layer.use_lights = False
                previous = preview.data
                preview.data = snapshot
                snapshot = None
                _remove_output(previous)
                entry.signature, entry.dirty = signature, False
                entry.mesh_keys = set()
                _count(scene, 'refreshes')
                result['changed'], result['writes'] = True, result['writes'] + 1
            except Exception as exc:
                if snapshot is not None and _valid(snapshot) and snapshot.users == 0:
                    grease_pencil_data().remove(snapshot)
                entry.dirty = True
                entry.close_private()
                _count(scene, 'failures')
                result['preserved'] = _output_owned(preview)
                result['warnings'] = [f'Material live preview update failed: {exc}' +
                                      (' Previous valid ink is retained.' if result['preserved'] else '')]
                return result
        for modifier in preview.modifiers:
            if modifier.type == 'LINEART':
                for name in ('show_viewport', 'show_render'):
                    if getattr(modifier, name):
                        setattr(modifier, name, False)
                        result['writes'] += 1
        result['ok'], result['preserved'] = True, True
        return result
    finally:
        _busy -= 1


def release(preview, reset_geometry=False):
    """Drop one evaluator; optionally replace its raw snapshot with empty ink."""
    _discard_stage(preview)
    for key, entry in list(_entries.items()):
        if entry.preview == preview:
            entry.close_private()
            del _entries[key]
    if reset_geometry and _output_owned(preview):
        scene = next((s for s in preview.users_scene if not _owned(s)), None)
        if scene:
            previous = preview.data
            preview.data = _empty_data(preview, scene)
            _remove_output(previous)
            return True
    return False


def cleanup(scene=None):
    """Remove exclusively owned private assets; attached valid ink is retained."""
    global _busy
    _busy += 1
    try:
        from . import preview_features
        preview_features.clear(scene)
        for key, staged in list(_stages.items()):
            preview = staged['preview']
            if _valid(preview) and (scene is None or scene in preview.users_scene):
                _restore_stage(preview)
            elif not _valid(preview):
                _stages.pop(key)
                if _valid(staged['snapshot']):
                    staged['snapshot'].use_fake_user = staged['fake_user']
                    _remove_output(staged['snapshot'])
        for key, entry in list(_entries.items()):
            if scene is None or entry.source == scene:
                if _valid(entry.preview):
                    _restore_stage(entry.preview)
                entry.close_private()
                del _entries[key]
        tokens = {s.get(OWNER) for s in list(bpy.data.scenes) if _owned(s)
                  and (scene is None or s.get(SOURCE) == scene.get('rea_pencil_preview_id'))}
        for token in tokens:
            _remove_private(token)
        if scene is None:
            _revisions.clear()
            _source_states.clear()
            _pending.clear()
            _subscriptions.clear()
        else:
            _revisions.pop(_pointer(scene),None)
            _source_states.pop(_pointer(scene),None)
            _pending.pop(_pointer(scene), None)
            _subscriptions.pop(_pointer(scene), None)
    finally:
        _busy -= 1


def mark_dirty(scene, geometry=False, objects=None):
    if not _valid(scene) or _owned(scene):
        return
    settings = getattr(scene, 'lea_line', None)
    if not settings or not settings.preview_enabled:
        return
    _revisions[_pointer(scene)]=revision(scene)+1
    row = _pending.setdefault(_pointer(scene), {'scene': scene, 'geometry': False})
    row['geometry'] = row['geometry'] or bool(geometry)
    if geometry:
        for entry in _entries.values():
            if entry.source == scene:
                entry.dirty = True
                if objects is None:
                    entry.mesh_keys = None
                elif entry.mesh_keys is not None:
                    entry.mesh_keys.update(objects)
    if _registered and not _paused and not bpy.app.timers.is_registered(watch_once):
        bpy.app.timers.register(watch_once, first_interval=1/30)


@persistent
def _depsgraph_update(scene, depsgraph):
    if _busy or _owned(scene):
        return
    if any(not _valid(entry.source) or not _valid(entry.preview) for entry in _entries.values()):
        if _registered and not bpy.app.timers.is_registered(watch_once):
            bpy.app.timers.register(watch_once, first_interval=1/30)
    if _pointer(scene) not in _subscriptions:
        return
    if not _active_material_scene(scene):
        _subscriptions.pop(_pointer(scene), None)
        _source_states.pop(_pointer(scene),None)
        return
    changed, geometry = False, False
    objects = set()
    for update in depsgraph.updates:
        block = update.id.original
        if _owned(block) or getattr(block, 'get', lambda *_: False)('rea_pencil_preview'):
            continue
        if isinstance(block, (bpy.types.Object, bpy.types.Mesh, bpy.types.Camera, bpy.types.Collection, bpy.types.Scene)):
            changed = True
            geometry = geometry or isinstance(block, bpy.types.Mesh) or (isinstance(block, bpy.types.Object)
                       and block.type == 'MESH' and update.is_updated_geometry)
            if isinstance(block, bpy.types.Mesh):
                # A shared datablock can have many independently modified users.
                objects.update(_pointer(o) for o in scene.objects
                               if o.type == 'MESH' and o.data == block and not o.get('rea_pencil_preview'))
            elif isinstance(block, bpy.types.Object) and block.type == 'MESH' and update.is_updated_geometry:
                objects.add(_pointer(block))
    if changed:
        state=_source_state(scene)
        previous=_source_states.get(_pointer(scene))
        _source_states[_pointer(scene)]=state
        # Blender reports source Scene/Collection/Object records again when
        # only owned GP snapshots were replaced. Real mesh geometry edits
        # remain unconditional, including mark changes with unchanged topology.
        if not geometry and previous==state:return
        if _paused:
            if geometry:
                _paused_geometry.add(_pointer(scene))
        else:
            mark_dirty(scene, geometry, objects=objects if objects else None)


@persistent
def _frame_change(scene, _depsgraph=None):
    if _busy or _owned(scene) or _pointer(scene) not in _subscriptions:
        return
    if _paused:
        _paused_geometry.add(_pointer(scene))
    else:
        mark_dirty(scene, geometry=True)


def watch_once():
    """Coalesced main-thread tick; failures wait for a new edit or explicit sync."""
    if _busy or _paused or bpy.app.is_job_running('RENDER'):
        return .1 if _pending else None
    for key, entry in list(_entries.items()):
        if not _valid(entry.source) or not _valid(entry.preview):
            orphan = entry.preview if _preview_owned(entry.preview) and not entry.preview.users_scene else None
            entry.close_private()
            del _entries[key]
            if orphan:
                # Scene deletion can leave its generated object in an orphan
                # collection. Only dispose outputs that belong to no scene.
                from . import viewport
                owner = orphan.get(viewport.OWNER)
                materials = [m for m in orphan.data.materials if m]
                viewport._remove_object(None, orphan)
                for material in materials:
                    if viewport._owned(material, owner) and material.users == 0:
                        bpy.data.materials.remove(material)
    for key, scene in list(_subscriptions.items()):
        if not _valid(scene):
            _subscriptions.pop(key)
            _pending.pop(key, None)
            _source_states.pop(key,None)
            _revisions.pop(key,None)
    work = list(_pending.values())
    _pending.clear()
    from . import viewport
    for row in work:
        scene = row['scene']
        if not _valid(scene):
            continue
        settings = getattr(scene, 'lea_line', None)
        if not _active_material_scene(scene):
            cleanup(scene)
            continue
        try:
            warnings = viewport.sync_preview(scene)
            message = '\n'.join(warnings) if warnings else 'Material live preview synchronized'
            if settings.last_message != message:
                settings.last_message = message
        except Exception as exc:
            settings.last_message = 'Material live preview update failed: ' + str(exc)
    return 1/30 if _pending else None


@persistent
def _suspend(*_args):
    global _paused, _busy
    _paused = True
    _paused_geometry.clear()
    _busy += 1
    try:
        for entry in list(_entries.values()):
            entry.close_private()
        _pending.clear()
    finally:
        _busy -= 1


@persistent
def _render_suspend(*_args):
    """Pause only: destroying a live depsgraph in render_init leaks in 4.4."""
    global _paused
    _paused = True
    _paused_geometry.clear()


@persistent
def _resume(*_args):
    global _paused
    _paused = False
    for scene in list(bpy.data.scenes):
        if _pointer(scene) in _subscriptions:
            mark_dirty(scene, geometry=_pointer(scene) in _paused_geometry)
    _paused_geometry.clear()


@persistent
def _reset_before(*_args):
    global _paused
    _paused = True
    cleanup()


@persistent
def _reset_after(*_args):
    global _paused
    cleanup()
    _paused = False
    for scene in list(bpy.data.scenes):
        mark_dirty(scene, geometry=True)


_HANDLERS = (('depsgraph_update_post', _depsgraph_update), ('frame_change_post', _frame_change),
             ('save_pre', _suspend), ('save_post', _resume),
             ('save_post_fail', _resume),
             ('render_init', _render_suspend), ('render_complete', _resume), ('render_cancel', _resume),
             ('undo_pre', _reset_before), ('redo_pre', _reset_before),
             ('undo_post', _reset_after), ('redo_post', _reset_after),
             ('load_pre', _reset_before), ('load_post', _reset_after))


def register():
    global _registered
    _registered = True
    for name, callback in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if callback not in handlers:
            handlers.append(callback)


def unregister():
    global _registered, _paused
    _registered = False
    if bpy.app.timers.is_registered(watch_once):
        bpy.app.timers.unregister(watch_once)
    for name, callback in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if callback in handlers:
            handlers.remove(callback)
    cleanup()
    _paused = False
    _counters.clear()
