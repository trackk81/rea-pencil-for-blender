"""Searchable Object/Material Add/Remove dialogs for the Line List workflow."""
import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, PointerProperty, StringProperty
from . import ui


class LEATargetChoice(bpy.types.PropertyGroup):
    content: PointerProperty(type=bpy.types.ID)
    selected: BoolProperty(default=False)


def matches(item, search):
    return bool(item.content and search.casefold() in item.content.name_full.casefold())


class LEA_UL_target_choices(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if item.content:
            layout.prop(item, 'selected', text=item.content.name_full)

    def filter_items(self, context, data, propname):
        search = context.window_manager.rea_pencil_target_search
        return [self.bitflag_filter_item if matches(item, search) else 0 for item in getattr(data, propname)], []


def clear(context):
    context.window_manager.rea_pencil_target_items.clear()
    context.window_manager.rea_pencil_target_search = ''


def populate(context, node, kind, action):
    clear(context)
    material = kind == 'MATERIAL'
    prop = 'materials' if material else 'objects'
    field = 'material' if material else 'object'
    existing = {getattr(item, field) for item in getattr(node, prop) if getattr(item, field)}
    selected = set(getattr(context, 'selected_objects', ()) or ())
    if material:
        selected = {slot.material for obj in selected for slot in obj.material_slots if slot.material}
    if action == 'REMOVE':
        candidates = existing
    else:
        excluded = set(existing)
        # The original dialog omits targets already assigned to sibling sets.
        for link in node.outputs[0].links:
            if link.to_node.bl_idname == 'LEAOutputNode':
                for sibling in ui._connected_line_sets(link.to_node):
                    excluded.update(getattr(item, field) for item in getattr(sibling, prop) if getattr(item, field))
        if material:
            candidates = {m for m in bpy.data.materials if m.grease_pencil is None} - excluded
        else:
            candidates = {o for o in context.scene.objects if o.type == 'MESH' and not o.get('rea_pencil_preview')} - excluded
    for content in sorted(candidates, key=lambda item: item.name_full.casefold()):
        item = context.window_manager.rea_pencil_target_items.add()
        item.content, item.selected = content, content in selected


class LEA_OT_target_select_all(bpy.types.Operator):
    bl_idname = 'lea_line.target_select_all'
    bl_label = 'Select Targets'
    mode: EnumProperty(items=[('ALL', 'All', ''), ('NONE', 'None', ''), ('INVERT', 'Invert', '')])

    def execute(self, context):
        wm = context.window_manager
        for item in wm.rea_pencil_target_items:
            visible = matches(item, wm.rea_pencil_target_search)
            item.selected = (visible if self.mode == 'ALL' else
                             visible and not item.selected if self.mode == 'INVERT' else False)
        return {'FINISHED'}


class LEA_OT_target_dialog(ui._LEATargetOperator, bpy.types.Operator):
    bl_idname = 'lea_line.target_dialog'
    bl_label = 'Objects / Materials'
    bl_description = 'Choose Objects or Materials from the searchable list, then confirm; selection is applied only on OK'
    bl_options = {'REGISTER', 'UNDO'}
    kind: EnumProperty(items=[('OBJECT', 'Objects', ''), ('MATERIAL', 'Materials', '')])
    action: EnumProperty(items=[('ADD', 'Add', ''), ('REMOVE', 'Remove', '')])
    selected_index: IntProperty(default=0)

    def invoke(self, context, event):
        node = self.target_node(context)
        if node is None or not node.id_data.is_editable:
            return {'CANCELLED'}
        populate(context, node, self.kind, self.action)
        return context.window_manager.invoke_props_dialog(self, width=400,
            title=('Add ' if self.action == 'ADD' else 'Remove ') + ('Objects' if self.kind == 'OBJECT' else 'Materials'))

    def cancel(self, context):
        clear(context)

    def draw(self, context):
        layout = self.layout;wm = context.window_manager
        row = layout.row(align=True)
        for mode, label in (('ALL', 'All'), ('NONE', 'None'), ('INVERT', 'Invert')):
            row.operator('lea_line.target_select_all', text=label).mode = mode
        layout.prop(wm, 'rea_pencil_target_search', text='', icon='VIEWZOOM')
        layout.template_list('LEA_UL_target_choices', '', wm, 'rea_pencil_target_items', self, 'selected_index', rows=8)
        if not wm.rea_pencil_target_items:
            layout.label(text='선택할 대상이 없습니다', icon='INFO')

    def execute(self, context):
        node = self.target_node(context)
        if node is None or not node.id_data.is_editable:
            clear(context)
            return {'CANCELLED'}
        material = self.kind == 'MATERIAL'
        prop, field, index = ('materials', 'material', 'material_index') if material else ('objects', 'object', 'object_index')
        kind = bpy.types.Material if material else bpy.types.Object
        selected = {item.content for item in context.window_manager.rea_pencil_target_items
                    if item.selected and isinstance(item.content, kind)}
        collection = getattr(node, prop)
        with ui.suspend_sync():
            if selected:
                node.target_mode = 'LISTED'
            if self.action == 'ADD':
                existing = {getattr(item, field) for item in collection}
                for content in sorted(selected - existing, key=lambda item: item.name_full.casefold()):
                    setattr(collection.add(), field, content)
            else:
                for i in reversed(range(len(collection))):
                    if getattr(collection[i], field) in selected:
                        collection.remove(i)
            setattr(node, index, max(0, len(collection) - 1))
        clear(context)
        if selected:
            ui._sync_if_enabled(node, context)
        return {'FINISHED'}


CLASSES = (LEATargetChoice, LEA_UL_target_choices, LEA_OT_target_select_all, LEA_OT_target_dialog)


def register():
    done = []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls);done.append(cls)
        bpy.types.WindowManager.rea_pencil_target_items = CollectionProperty(type=LEATargetChoice)
        bpy.types.WindowManager.rea_pencil_target_search = StringProperty(name='Search')
    except Exception:
        for name in ('rea_pencil_target_items', 'rea_pencil_target_search'):
            if hasattr(bpy.types.WindowManager, name):delattr(bpy.types.WindowManager, name)
        for cls in reversed(done):bpy.utils.unregister_class(cls)
        raise


def unregister():
    for name in ('rea_pencil_target_items', 'rea_pencil_target_search'):
        if hasattr(bpy.types.WindowManager, name):delattr(bpy.types.WindowManager, name)
    for cls in reversed(CLASSES):
        if cls.is_registered:bpy.utils.unregister_class(cls)
