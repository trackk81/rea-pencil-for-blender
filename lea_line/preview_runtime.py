"""Main-thread projection watcher; geometry/transforms stay in Blender's graph."""
import bpy
from bpy.app.handlers import persistent

_seen = {}
_registered = False


def _signature(scene):
    settings = getattr(scene, 'lea_line', None)
    if not settings or not settings.preview_enabled:
        return None
    from . import free_view
    entry=free_view._entries.get(scene.as_pointer())
    if entry:return ('FREE',entry['area'],entry['dimensions'],free_view.pixel_scale())
    from . import preview_camera
    camera = scene.camera
    if not camera and not bpy.app.background:
        view = free_view.available_view(scene)
        return ('NO_SCENE_CAMERA', view[0].as_pointer() if view else None)
    if not camera or camera.data.type not in {'ORTHO', 'PERSP'}:
        return ('unsupported', camera.as_pointer() if camera else None,
                camera.data.type if camera else None)
    view = free_view.available_view(scene) if free_view.wants_automatic(scene) else None
    return ('CAMERA', preview_camera.signature(scene), view[0].as_pointer() if view else None)


def mark_synced(scene):
    signature = _signature(scene)
    key = scene.as_pointer()
    if signature is None:
        _seen.pop(key, None)
    else:
        _seen[key] = signature
        if _registered and not bpy.app.timers.is_registered(watch_once):
            bpy.app.timers.register(watch_once, first_interval=1 / 30)


def watch_once():
    if bpy.app.is_job_running('RENDER'):
        return .2
    active = set()
    for scene in bpy.data.scenes:
        signature = _signature(scene)
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
    return 1 / 30 if active else None


@persistent
def reset(*_args):
    _seen.clear()
    from .ui import _sync_if_enabled
    for scene in bpy.data.scenes:
        if _signature(scene) is not None:
            _sync_if_enabled(scene.lea_line)
            mark_synced(scene)


def _initialize():
    if not _registered:
        return None
    if not hasattr(bpy.data, 'scenes'):
        return .1
    reset()
    return None


def register():
    global _registered
    _registered = True
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if reset not in handlers:
            handlers.append(reset)
    if hasattr(bpy.data, 'scenes'):
        _initialize()
    elif not bpy.app.timers.is_registered(_initialize):
        bpy.app.timers.register(_initialize, first_interval=0)


def unregister():
    global _registered
    _registered = False
    if bpy.app.timers.is_registered(_initialize):
        bpy.app.timers.unregister(_initialize)
    if bpy.app.timers.is_registered(watch_once):
        bpy.app.timers.unregister(watch_once)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if reset in handlers:
            handlers.remove(reset)
    _seen.clear()
