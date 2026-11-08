"""Read-only UI references for controls whose rendering is not yet implemented.

The fields live on WindowManager, never on a render node or in the render cache.
All displays of this data are disabled and explain the unsupported behavior.
"""
import bpy
from .reference_schema import SCHEMA

UNAVAILABLE = 'Not implemented in rea. Reference display only; this control does not affect rendering.'


class LEA_OT_reference_unavailable(bpy.types.Operator):
    bl_idname = 'lea_line.reference_unavailable'
    bl_label = 'Not Implemented'
    bl_description = UNAVAILABLE
    def execute(self, context):
        self.report({'INFO'}, UNAVAILABLE)
        return {'CANCELLED'}


GROUP_CLASSES = []
for group, fields in SCHEMA.items():
    annotations = {}
    for name, definition in fields.items():
        kwargs = dict(definition['kwargs'])
        kwargs['name'] = name.replace('_', ' ').title()
        kwargs['description'] = UNAVAILABLE
        annotations[name] = getattr(bpy.props, definition['kind'])(**kwargs)
    GROUP_CLASSES.append(type('LEAReference_' + group, (bpy.types.PropertyGroup,), {'__annotations__': annotations}))


class LEAReferenceData(bpy.types.PropertyGroup):
    pass


LEAReferenceData.__annotations__ = {group: bpy.props.PointerProperty(type=cls)
                                 for group, cls in zip(SCHEMA, GROUP_CLASSES)}


def data(context, group):
    manager = getattr(context, 'window_manager', None) or bpy.context.window_manager
    return getattr(manager.lea_reference, group)


def prop(context, layout, group, name, text=None, **kwargs):
    row = layout.row(align=True)
    row.enabled = False
    row.prop(data(context, group), name, text=text if text is not None else name.replace('_', ' ').title(), **kwargs)
    return row


def unavailable(layout, text='None', icon='NONE'):
    row = layout.row(align=True)
    row.enabled = False
    row.operator('lea_line.reference_unavailable', text=text, icon=icon)
    return row


def button(layout, text='None', icon='NONE'):
    return unavailable(layout, text, icon)


def enum(context, layout, group, name, text, reverse=False):
    split = layout.split(factor=.4)
    label = split.row()
    label.alignment = 'RIGHT'
    label.label(text=text)
    row = split.row(align=True)
    row.use_property_split = False
    row.enabled = False
    items = list(SCHEMA[group][name]['kwargs']['items'])
    for item in reversed(items) if reverse else items:
        row.prop_enum(data(context, group), name, item[0], text=item[1])


def node_link(context, layout, group, on_name, label, value_name=None):
    row = layout.row(align=True)
    row.use_property_split = False
    row.enabled = False
    row.prop(data(context, group), on_name, text=label)
    row.operator('lea_line.reference_unavailable', text='None')
    if value_name:
        row.prop(data(context, group), value_name, text='')


def pair(context, layout, label, left_owner, left_prop, right_group, right_prop, right_label='Random'):
    split = layout.split(factor=.4)
    caption = split.row()
    caption.alignment = 'RIGHT'
    caption.label(text=label)
    row = split.row(align=True)
    row.use_property_split = False
    row.prop(left_owner, left_prop, text='')
    right = row.row(align=True)
    right.enabled = False
    right.prop(data(context, right_group), right_prop, text=right_label)


CLASSES = (*GROUP_CLASSES, LEAReferenceData, LEA_OT_reference_unavailable)
def register():
    # A failed enable/reloaded module can leave the old dynamically generated
    # PropertyGroup classes registered even when this module has new identities.
    unregister()
    registered = []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
        bpy.types.WindowManager.lea_reference = bpy.props.PointerProperty(type=LEAReferenceData)
    except Exception:
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise


def unregister():
    if hasattr(bpy.types.WindowManager, 'lea_reference'):
        del bpy.types.WindowManager.lea_reference
    for cls in reversed(CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
            continue
        base = cls.__bases__[0]
        identifier = cls.__name__
        if issubclass(cls, bpy.types.Operator):
            category, action = cls.bl_idname.split('.', 1)
            identifier = category.upper() + '_OT_' + action
        registered = base.bl_rna_get_subclass_py(identifier, None)
        if registered is not None:
            bpy.utils.unregister_class(registered)
