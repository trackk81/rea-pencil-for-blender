"""Guarded ordering for three exclusive root-linked mixed preview outputs."""
import threading
import bpy
from . import preview_material as private
PRIORITY=('crease','edge_mark','material_boundary')
def _link(collection,obj):collection.objects.link(obj)
def _rendering():return bpy.app.is_job_running('RENDER')
def normalize(scene,prefix):
    from . import viewport
    if threading.current_thread() is not threading.main_thread() or private.is_suspended() or _rendering():
        return False
    # The regular owner accessor may detach previews on duplicated scenes.
    # A link-order helper must be read-only until all guards pass.
    owner=scene.get(viewport.OWNER_SCENE_KEY)
    if not owner or any(s!=scene and s.get(viewport.OWNER_SCENE_KEY)==owner for s in bpy.data.scenes):return False
    group=[o for o in scene.objects if viewport._owned(o,owner) and str(o.get(viewport.KEY,'')).startswith(prefix)]
    lookup={o.get(viewport.KEY):o for o in group}
    if len(group)!=3 or set(lookup)!={prefix+f for f in PRIORITY}:return False
    root=scene.collection
    for obj in group:
        if (not private._output_owned(obj) or obj.use_fake_user or tuple(obj.users_scene)!=(scene,)
                or tuple(obj.users_collection)!=(root,)):
            return False
    # Moving the group to the root's end must not reorder it against other GP.
    if any(o.type=='GREASEPENCIL' and o not in group for o in scene.objects):return False
    current=[o for o in root.objects if o in group]
    desired=[lookup[prefix+f] for f in PRIORITY]
    if current==desired:return False
    private._busy+=1
    try:
        try:
            for obj in current:root.objects.unlink(obj)
            for obj in desired:_link(root,obj)
        except Exception:
            for obj in group:
                if root in obj.users_collection:root.objects.unlink(obj)
            for obj in current:root.objects.link(obj)
            raise
    finally:private._busy-=1
    return True

