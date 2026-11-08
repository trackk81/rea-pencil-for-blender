"""Private, editable curve data for shared Brush Detail nodes.

Curve widgets read owned Float Curve nodes. Creation, copying and preset imports
are explicit data operations; drawing and snapshots never allocate or modify IDs.
"""
import copy
import math
import uuid
import bpy
from bpy.app.handlers import persistent

TAG = 'rea_detail_curves'
OWNER = 'rea_detail_curve_owner'
SCHEMA = 'rea_detail_curve_schema'
KIND = 'rea_detail_curve_kind'
PENDING = 'rea_detail_curve_copy_source'
KINDS = ('SIZE', 'ALPHA')
MAPPING_FIELDS = ('extend', 'tone', 'use_clip', 'clip_min_x', 'clip_max_x',
                  'clip_min_y', 'clip_max_y', 'black_level', 'white_level')
DEFAULT = {
    'points': [[0.0, .25, 'AUTO'], [.5, 1.0, 'AUTO'], [1.0, .25, 'AUTO']],
    'extend': 'EXTRAPOLATED', 'tone': 'STANDARD', 'use_clip': True,
    'clip_min_x': 0.0, 'clip_max_x': 1.0, 'clip_min_y': 0.0, 'clip_max_y': 1.0,
    'black_level': [0.0, 0.0, 0.0], 'white_level': [1.0, 1.0, 1.0],
}


def default_payload():
    return {kind: copy.deepcopy(DEFAULT) for kind in KINDS}


def _owned(detail, tree):
    token = detail.get(OWNER)
    return bool(tree and token and tree.bl_idname == 'ShaderNodeTree'
                and tree.get(TAG) is True and tree.get(SCHEMA) == 1 and tree.get(OWNER) == token)


def _mapping_node(tree, kind):
    matches = [node for node in tree.nodes
               if node.bl_idname == 'ShaderNodeFloatCurve' and node.get(KIND) == kind]
    return matches[0] if len(matches) == 1 else None


def get_mapping_node(detail, kind):
    """Read only: return a CurveMapping owner suitable for template_curve_mapping."""
    if kind not in KINDS:
        raise ValueError('Unknown stroke curve kind')
    tree = getattr(detail, 'curve_tree', None)
    return _mapping_node(tree, kind) if _owned(detail, tree) else None


def snapshot_mapping(mapping):
    result = {'points': [[float(point.location.x), float(point.location.y), point.handle_type]
                         for point in mapping.curves[0].points]}
    for name in MAPPING_FIELDS:
        value = getattr(mapping, name)
        result[name] = list(value) if name in {'black_level', 'white_level'} else value
    return result


def export_detail(detail):
    """Read-only JSON payload; old/uninitialized data uses the documented defaults."""
    result = default_payload()
    if detail.bl_idname=='LEADistanceReductionNode':
        from .distance_curves import DEFAULT_POINTS
        for value in result.values():value['points']=copy.deepcopy(DEFAULT_POINTS)
    for kind in KINDS:
        node = get_mapping_node(detail, kind)
        if node:
            result[kind] = snapshot_mapping(node.mapping)
    return result


def _animated(tree):
    animation = tree.animation_data if tree else None
    return bool(animation and (animation.action or len(animation.drivers) or len(animation.nla_tracks)))


def validate_import_target(detail):
    if not detail.id_data.is_editable or detail.is_property_readonly('curve_tree'):
        raise ValueError('This linked graph is read-only')
    tree = detail.curve_tree
    if tree and (tree.library is not None or not tree.is_editable):
        raise ValueError('Linked stroke curve data is preserved; make it local before applying a preset')
    if _animated(tree):
        raise ValueError('Animated stroke curve data is preserved; apply presets on an unanimated Detail')


def resolved_payload(detail):
    """Read and validate current widget values immediately before renderer sync."""
    if not all(get_mapping_node(detail, kind) is not None for kind in KINDS):
        raise ValueError('Brush Detail has missing or unowned stroke curve data; synchronize the graph to initialize it')
    if _animated(detail.curve_tree):
        raise ValueError('Animated stroke curves are not supported by this renderer')
    return validate_payload(export_detail(detail))


def _number(value, lower=-3.402823466e38, upper=3.402823466e38):
    return type(value) in {int, float} and math.isfinite(value) and lower <= value <= upper


def validate_payload(payload):
    """Validate both curves without allocating IDs or partially applying presets."""
    if not isinstance(payload, dict) or set(payload) != set(KINDS):
        raise ValueError('Curve payload must contain SIZE and ALPHA')
    result = copy.deepcopy(payload)
    for kind, data in result.items():
        if not isinstance(data, dict) or set(data) != set(DEFAULT):
            raise ValueError('Invalid ' + kind + ' curve fields')
        points = data['points']
        if not isinstance(points, list) or not 2 <= len(points) <= 256:
            raise ValueError('A stroke curve requires 2 to 256 points')
        for point in points:
            if (not isinstance(point, (list, tuple)) or len(point) != 3
                    or not _number(point[0]) or not _number(point[1])
                    or point[2] not in {'AUTO', 'AUTO_CLAMPED', 'VECTOR'}):
                raise ValueError('Invalid stroke curve point')
        points.sort(key=lambda point: point[0])
        if points[0][0] >= points[-1][0]:
            raise ValueError('A stroke curve needs a nonzero horizontal range')
        if data['extend'] not in {'HORIZONTAL', 'EXTRAPOLATED'} or data['tone'] not in {'STANDARD', 'FILMLIKE'}:
            raise ValueError('Invalid stroke curve interpolation')
        if type(data['use_clip']) is not bool:
            raise ValueError('Invalid stroke curve clipping flag')
        for name in ('clip_min_x', 'clip_max_x', 'clip_min_y', 'clip_max_y'):
            if not _number(data[name], -100, 100):
                raise ValueError('Invalid curve clipping bounds')
        if data['clip_min_x'] >= data['clip_max_x'] or data['clip_min_y'] >= data['clip_max_y']:
            raise ValueError('Curve clipping bounds must have positive width and height')
        for name in ('black_level', 'white_level'):
            if not isinstance(data[name], (list, tuple)) or len(data[name]) != 3 or not all(_number(value) for value in data[name]):
                raise ValueError('Invalid curve level values')
    return result


def apply_mapping(mapping, data, setter=None):
    """Copy an already validated curve into a native mapping; return change count."""
    changes = 0
    def assign(owner, name, value):
        nonlocal changes
        if setter is not None:
            changed = setter(owner, name, value)
        else:
            current = getattr(owner, name)
            if isinstance(value, (tuple, list)):
                changed = len(current) != len(value) or any(not math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6) for a, b in zip(current, value))
            elif isinstance(value, float):
                changed = not math.isclose(current, value, rel_tol=1e-6, abs_tol=1e-6)
            else:
                changed = current != value
            if changed:
                setattr(owner, name, value)
        changes += bool(changed)
    for name in MAPPING_FIELDS:
        assign(mapping, name, data[name])
    curve = mapping.curves[0]
    points = data['points']
    if len(curve.points) != len(points):
        while len(curve.points) > 2:
            curve.points.remove(curve.points[1])
        for x, y, _ in points[1:-1]:
            curve.points.new(x, y)
        changes += 1
    for point, (x, y, handle) in zip(curve.points, points):
        assign(point, 'location', (x, y))
        assign(point, 'handle_type', handle)
    if changes:
        mapping.update()
    return changes


def _create_tree(token):
    tree = bpy.data.node_groups.new('.rea pencil stroke curves', 'ShaderNodeTree')
    tree[TAG], tree[OWNER], tree[SCHEMA] = True, token, 1
    try:
        for kind, title in (('SIZE', 'Stroke Size Reduction'), ('ALPHA', 'Stroke Alpha Reduction')):
            node = tree.nodes.new('ShaderNodeFloatCurve')
            node[KIND] = kind
            node.name = node.label = title
            apply_mapping(node.mapping, DEFAULT)
            for index, point in enumerate(node.mapping.curves[0].points):
                point.select = index == len(node.mapping.curves[0].points) - 1
        return tree
    except Exception:
        if tree.users == 0:
            bpy.data.node_groups.remove(tree)
        raise


def _remove_unused_owned(tree, token):
    if (tree and tree.library is None and tree.users == 0 and tree.get(TAG) is True
            and tree.get(SCHEMA) == 1 and tree.get(OWNER) == token):
        bpy.data.node_groups.remove(tree)


def _commit(detail, candidate, token, cleanup_previous=True):
    from .ui import suspend_sync
    previous = detail.curve_tree
    previous_token = detail.get(OWNER)
    previously_owned = _owned(detail, previous)
    with suspend_sync():
        try:
            detail[OWNER] = token
            detail.curve_tree = candidate
        except Exception:
            if previous_token is None:
                if OWNER in detail:
                    del detail[OWNER]
            else:
                detail[OWNER] = previous_token
            detail.curve_tree = previous
            raise
    if previously_owned and cleanup_previous:
        _remove_unused_owned(previous, previous_token)


def ensure_detail(detail):
    """Create/migrate private data explicitly; never called from a draw function."""
    if PENDING in detail:
        _finish_copy(detail)
    if all(get_mapping_node(detail, kind) is not None for kind in KINDS):
        return detail.curve_tree
    if not detail.id_data.is_editable:
        return None
    validate_import_target(detail)
    payload = export_detail(detail)
    token = uuid.uuid4().hex
    tree = _create_tree(token)
    try:
        for kind in KINDS:
            apply_mapping(_mapping_node(tree, kind).mapping, payload[kind])
        _commit(detail, tree, token)
        return tree
    except Exception:
        _remove_unused_owned(tree, token)
        raise


def import_detail(detail, payload):
    """Atomically import both curves by staging a private tree before committing."""
    validated = validate_payload(payload)
    validate_import_target(detail)
    token = uuid.uuid4().hex
    tree = _create_tree(token)
    try:
        for kind in KINDS:
            apply_mapping(_mapping_node(tree, kind).mapping, validated[kind])
        _commit(detail, tree, token)
    except Exception:
        _remove_unused_owned(tree, token)
        raise
    from .ui import _sync_if_enabled
    _sync_if_enabled(detail)


def copy_detail(detail, source):
    """A duplicated Detail owns independent curves; linked Brushes still share it."""
    # Blender calls Node.copy before it counts the copied ID pointer's user.
    # Replacing curve_tree here would undercount the source and leak the copy.
    # A fresh owner token makes the transient shared pointer read-only to our UI.
    from .ui import suspend_sync
    source_tree = source.curve_tree
    source_token = source.get(PENDING, source.get(OWNER, ''))
    trusted = bool(source_tree and source_token and source_tree.get(TAG) is True
                   and source_tree.get(SCHEMA) == 1 and source_tree.get(OWNER) == source_token)
    with suspend_sync():
        detail[PENDING] = source_token if trusted else ''
        detail[OWNER] = uuid.uuid4().hex
    if not bpy.app.timers.is_registered(flush_copies):
        bpy.app.timers.register(flush_copies, first_interval=0)


def _finish_copy(detail):
    source = detail.curve_tree
    token = detail.get(OWNER)
    source_token = detail.get(PENDING)
    if (source and source_token and source.get(TAG) is True
            and source.get(SCHEMA) == 1 and source.get(OWNER) == source_token):
        candidate = source.copy()
        candidate[OWNER] = token
    else:
        candidate = _create_tree(token)
        if detail.bl_idname=='LEADistanceReductionNode':
            for kind,data in export_detail(detail).items():
                apply_mapping(_mapping_node(candidate,kind).mapping,data)
    try:
        _commit(detail, candidate, token, cleanup_previous=False)
        del detail[PENDING]
    except Exception:
        _remove_unused_owned(candidate, token)
        raise


def flush_copies():
    """Finish queued copies after Blender has counted the duplicate's ID users."""
    for tree in list(bpy.data.node_groups):
        if tree.bl_idname == 'LEALineTree' and tree.is_editable:
            for node in tree.nodes:
                if node.bl_idname in {'LEABrushDetailNode','LEADistanceReductionNode'} and PENDING in node:
                    _finish_copy(node)
    cleanup_orphans()
    return None


def cleanup_orphans():
    """ID removal can decrement child users after Node.free has returned."""
    for tree in list(bpy.data.node_groups):
        if (tree.bl_idname=='ShaderNodeTree' and tree.get(TAG) is True
                and tree.get(SCHEMA)==1 and tree.get(OWNER) and tree.users==0
                and not tree.use_fake_user and tree.library is None):
            bpy.data.node_groups.remove(tree)
    return None


def free_detail(detail):
    tree = getattr(detail, 'curve_tree', None)
    token = detail.get(OWNER)
    owned = _owned(detail, tree)
    if tree:
        detail.curve_tree = None
    if owned:
        _remove_unused_owned(tree, token)
        if not bpy.app.timers.is_registered(cleanup_orphans):
            bpy.app.timers.register(cleanup_orphans,first_interval=0)


def upgrade_tree(tree):
    if tree is None or tree.bl_idname != 'LEALineTree' or not tree.is_editable:
        return
    for node in tree.nodes:
        if node.bl_idname in {'LEABrushDetailNode','LEADistanceReductionNode'}:
            ensure_detail(node)


# CurveMapping widget changes have no Python property update callback. Poll only
# while an auto-synchronized scene uses an enabled custom curve. Selection and
# point selection is excluded, so it never triggers native renderer writes.
_seen = {}
_registered = False
_notification_owner = object()


def _mapping_changed():
    """GUI hint only: coalesce notifications; mutate no Blender data here."""
    if _registered and _seen and not bpy.app.timers.is_registered(_wake_once):
        bpy.app.timers.register(_wake_once, first_interval=0)


def _wake_once():
    if not _registered or not _seen:
        return None
    if bpy.app.is_job_running('RENDER'):
        return .2
    watch_once()
    return None


def _subscribe_mapping():
    bpy.msgbus.clear_by_owner(_notification_owner)
    if _registered:
        bpy.msgbus.subscribe_rna(key=(bpy.types.ShaderNodeFloatCurve, 'mapping'),
                                owner=_notification_owner, args=(), notify=_mapping_changed)


def _scene_signature(scene):
    settings = getattr(scene, 'lea_line', None)
    if not settings or not (settings.auto_sync or settings.preview_enabled) or not settings.tree:
        return None
    values = []
    for node in settings.tree.nodes:
        if node.bl_idname=='LEADistanceReductionNode':
            direct = any(link.to_node.bl_idname == 'LEALineSetNode' for s in node.outputs for link in s.links)
            if node.enabled and node.curve_enabled and (direct or node.reduce_thickness or node.reduce_opacity) and any(s.is_linked for s in node.outputs):
                values.append((node.name,{'DISTANCE':export_detail(node)['SIZE']}))
            continue
        if (node.bl_idname != 'LEABrushDetailNode' or not node.enabled
                or not (node.size_curve_enabled or node.alpha_curve_enabled)):
            continue
        if not any(socket.is_linked for socket in node.outputs):
            continue
        payload = export_detail(node)
        active = {}
        for kind, enabled in (('SIZE', node.size_curve_enabled), ('ALPHA', node.alpha_curve_enabled)):
            if enabled:
                active[kind] = payload[kind]
        values.append((node.name, active))
    return values or None


def mark_synced(scene):
    signature = _scene_signature(scene)
    key = scene.as_pointer()
    if signature is None:
        _seen.pop(key, None)
    else:
        _seen[key] = signature
        if _registered and not bpy.app.timers.is_registered(watch_once):
            bpy.app.timers.register(watch_once, first_interval=_interval())


def _interval():
    return 1 / 30 if any(getattr(s, 'lea_line', None) and s.lea_line.preview_enabled
                        for s in bpy.data.scenes) else .2


def watch_once():
    """Main-thread edit watcher, also directly callable by CPU integration tests."""
    if bpy.app.is_job_running('RENDER'):
        return .2
    active = set()
    for scene in bpy.data.scenes:
        signature = _scene_signature(scene)
        if signature is None:
            continue
        key = scene.as_pointer()
        active.add(key)
        if signature != _seen.get(key):
            _seen[key] = signature
            from .ui import _sync_if_enabled
            _sync_if_enabled(scene.lea_line)
    for key in set(_seen) - active:
        del _seen[key]
    return _interval() if active else None


def reset_watch():
    # Message-bus subscriptions are cleared when another file is loaded.
    _subscribe_mapping()
    _seen.clear()
    for scene in bpy.data.scenes:
        mark_synced(scene)


@persistent
def _history_changed(*_args):
    # Undo can re-enable a curve without invoking the BoolProperty callback.
    # Keep no RNA references across undo: all scene/node values are read afresh.
    flush_copies()
    reset_watch()
    for scene in bpy.data.scenes:
        if _scene_signature(scene) is not None:
            from .ui import _sync_if_enabled
            _sync_if_enabled(scene.lea_line)


def _initialize():
    if not _registered:
        return None
    if not hasattr(bpy.data, 'scenes') or not hasattr(bpy.data, 'node_groups'):
        return .1
    for tree in list(bpy.data.node_groups):
        upgrade_tree(tree)
    reset_watch()
    return None


def register():
    global _registered
    _registered = True
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _history_changed not in handlers:
            handlers.append(_history_changed)
    if hasattr(bpy.data, 'scenes') and hasattr(bpy.data, 'node_groups'):
        _initialize()
    elif not bpy.app.timers.is_registered(_initialize):
        bpy.app.timers.register(_initialize, first_interval=0)


def unregister():
    from . import distance_curves
    distance_curves.clear_cache()
    global _registered
    _registered = False
    if bpy.app.timers.is_registered(_initialize):
        bpy.app.timers.unregister(_initialize)
    bpy.msgbus.clear_by_owner(_notification_owner)
    if bpy.app.timers.is_registered(_wake_once):
        bpy.app.timers.unregister(_wake_once)
    if bpy.app.timers.is_registered(watch_once):
        bpy.app.timers.unregister(watch_once)
    if bpy.app.timers.is_registered(flush_copies):
        bpy.app.timers.unregister(flush_copies)
    if bpy.app.timers.is_registered(cleanup_orphans):
        bpy.app.timers.unregister(cleanup_orphans)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _history_changed in handlers:
            handlers.remove(_history_changed)
    _seen.clear()
