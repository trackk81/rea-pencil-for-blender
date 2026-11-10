"""rea pencil for blender: independent node-driven line rendering."""

bl_info = {
    "name": "rea pencil for blender",
    "author": "rea pencil contributors",
    "version": (0, 28, 16),
    "blender": (4, 4, 0),
    "location": "View3D > Sidebar > REA Pencil Line / Node Editor > REA Pencil Line",
    "description": "Node-driven outlines, creases, material boundaries and hidden lines",
    "category": "Render",
}

from . import update_core as _update_core
from pathlib import Path as _Path
import bpy as _bpy
_update_error = ''
try:
    _applied = _update_core.apply_pending(_Path(_bpy.utils.user_resource('CONFIG', create=True)) / 'rea_pencil_updates', _Path(__file__).parent)
except Exception as _exc:
    _applied = False
    _update_error = 'Update postponed: ' + str(_exc)
    print('rea pencil:', _update_error)
if _applied:
    import sys as _sys
    _sys.modules.pop(__name__ + '.update_core', None)
    globals().pop('update_core', None)
    exec(compile(_Path(__file__).read_bytes(), __file__, 'exec'), globals())
else:
    from . import backend, ui, presets, reference_controls, preview_material, updater, gpu_preview, gpu_render, f12_render, brush_sample

    _registered = False


    def register():
        global _registered
        if _registered:
            return
        completed = []
        try:
            for module in (reference_controls, ui, presets, preview_material, backend, gpu_preview, gpu_render, f12_render, updater, brush_sample):
                module.register()
                completed.append(module)
        except Exception:
            for module in reversed(completed):
                module.unregister()
            raise
        _registered = True


    def unregister():
        global _registered
        if not _registered:
            return
        import bpy
        from . import viewport
        from . import render_cache
        brush_sample.unregister()
        updater.unregister()
        f12_render.unregister()
        gpu_render.unregister()
        gpu_preview.unregister()
        render_cache.clear()
        preview_material.unregister()
        for scene in list(bpy.data.scenes):
            viewport.clear_preview(scene)
        backend.unregister()
        presets.unregister()
        ui.unregister()
        reference_controls.unregister()
        _registered = False
