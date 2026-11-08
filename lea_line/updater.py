"""Manual GitHub update check; verified packages apply on the next Blender start."""
import os
from pathlib import Path
import queue
import threading
import bpy
from . import update_core

_messages = queue.Queue()
_enabled = False
_epoch = 0
_busy = False
_release = None
_status = ''

def directory():
    return Path(bpy.utils.user_resource('CONFIG', create=True)) / 'rea_pencil_updates'

def current_version():
    from . import bl_info
    return tuple(bl_info['version'])

def _redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()

def _poll():
    global _busy, _release, _status
    if not _enabled:
        return None
    while True:
        try:
            epoch, kind, value = _messages.get_nowait()
        except queue.Empty:
            break
        if epoch != _epoch:
            continue
        _busy = False
        if kind == 'check':
            _release = value
            _status = ('새 버전 ' + '.'.join(map(str, value['version']))) if value else '현재 최신 버전입니다'
        elif kind == 'download':
            path, release = value
            try:
                update_core.stage(path, release, directory(), Path(__file__).parent)
                _status = '다운로드 완료 · Blender 재시작 후 적용'
                _release = None
            except Exception as exc:
                _status = '업데이트 준비 실패: ' + str(exc)
        else:
            _status = '업데이트 실패: ' + str(value)
        _redraw()
    return .2 if _busy else None

def _start(kind):
    global _busy, _status
    _busy = True
    _status = '새 버전 확인 중…' if kind == 'check' else '다운로드·무결성 검사 중…'
    epoch = _epoch
    current = current_version()
    release = dict(_release) if _release else None
    target = str(directory())
    blender_version = tuple(bpy.app.version)
    def work():
        try:
            value = update_core.latest(current) if kind == 'check' else (update_core.download(release, target, blender_version), release)
            _messages.put((epoch, kind, value))
        except Exception as exc:
            _messages.put((epoch, 'error', str(exc)))
    threading.Thread(target=work, daemon=True, name='REA-Pencil-Update').start()
    if not bpy.app.timers.is_registered(_poll):
        bpy.app.timers.register(_poll, first_interval=.2, persistent=True)

def draw(layout, context):
    box = layout.box()
    box.label(text='rea pencil ' + '.'.join(map(str, current_version())))
    row = box.row(align=True)
    row.enabled = not _busy
    row.operator('lea_line.check_update', text='업데이트 확인', icon='FILE_REFRESH')
    row.operator('lea_line.release_page', text='GitHub', icon='URL')
    if _release and not _busy:
        box.operator('lea_line.install_update', text='새 버전 다운로드', icon='IMPORT')
    if _status:
        box.label(text=_status)
    try:
        value = update_core.pending(directory())
        if value and value.get('state') == 'ready':
            box.label(text='모든 Blender를 종료한 후 다시 실행해 주세요', icon='INFO')
    except (OSError, ValueError):
        pass

class LEA_OT_check_update(bpy.types.Operator):
    bl_idname = 'lea_line.check_update'
    bl_label = '업데이트 확인'
    @classmethod
    def poll(cls, context): return not _busy
    def execute(self, context):
        _start('check')
        return {'FINISHED'}

class LEA_OT_install_update(bpy.types.Operator):
    bl_idname = 'lea_line.install_update'
    bl_label = '새 버전 다운로드'
    bl_description = 'GitHub 배포본을 검사하고 준비합니다. 적용하려면 Blender를 재시작하세요'
    @classmethod
    def poll(cls, context):
        return not _busy and _release is not None and not bpy.app.is_job_running('RENDER')
    def execute(self, context):
        _start('download')
        return {'FINISHED'}

class LEA_OT_release_page(bpy.types.Operator):
    bl_idname = 'lea_line.release_page'
    bl_label = 'GitHub 배포 페이지'
    def execute(self, context):
        bpy.ops.wm.url_open(url='https://github.com/' + update_core.REPOSITORY + '/releases')
        return {'FINISHED'}

class LEAUpdatePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__
    def draw(self, context): draw(self.layout, context)

CLASSES = (LEA_OT_check_update, LEA_OT_install_update, LEA_OT_release_page, LEAUpdatePreferences)

def register():
    global _enabled, _epoch, _status
    _epoch += 1
    _enabled = True
    from . import _update_error
    _status = _update_error
    for cls in CLASSES: bpy.utils.register_class(cls)

def unregister():
    global _enabled, _epoch, _busy, _release
    _enabled = False
    _epoch += 1
    _busy = False
    _release = None
    if bpy.app.timers.is_registered(_poll): bpy.app.timers.unregister(_poll)
    for cls in reversed(CLASSES):
        if cls.is_registered: bpy.utils.unregister_class(cls)
