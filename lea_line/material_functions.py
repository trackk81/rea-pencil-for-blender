"""Material Line Functions: visible outline replacement through frozen face data.

The initial supported domain is visible contour/open-border lines. Other native
Line Functions and hidden-mask semantics are explicitly rejected, not simulated.
"""
import threading
from types import SimpleNamespace
import bpy
from bpy.props import BoolProperty,FloatProperty,FloatVectorProperty,PointerProperty


def changed(owner,context):
    if not hasattr(bpy.data,'scenes'):return
    from . import ui,object_uv
    for scene in bpy.data.scenes:
        tree=getattr(getattr(scene,'lea_line',None),'tree',None)
        if tree and any(n.bl_idname=='LEALineSetNode' and n.use_material_functions for n in tree.nodes):
            ui._sync_if_enabled(scene.lea_line,context);object_uv.request_prepare(scene)


class LEALineFunctionsNode(bpy.types.Node):
    bl_idname='LEALineFunctionsNode';bl_label='REA Pencil Line Functions';bl_icon='GREASEPENCIL'
    enabled:BoolProperty(name='On',default=True,update=changed)
    outline_on:BoolProperty(name='Outline',default=False,update=changed)
    outline_color:FloatVectorProperty(name='Color',subtype='COLOR',size=3,min=0,max=1,default=(0,0,0),update=changed)
    outline_amount:FloatProperty(name='Amount',min=0,max=1,default=1,update=changed)
    @classmethod
    def poll(cls,tree):return tree.bl_idname=='ShaderNodeTree'
    def init(self,context):self.width=230
    def draw_buttons(self,context,layout):
        layout.prop(self,'enabled');layout.label(text='Replace Line Color')
        row=layout.row();row.enabled=self.enabled;row.prop(self,'outline_on')
        col=layout.column();col.enabled=self.enabled and self.outline_on
        col.prop(self,'outline_color');col.prop(self,'outline_amount')
        layout.label(text='Visible outline / open border',icon='INFO')


def node(material):
    holder=getattr(material,'lea_line_functions',None) if material else None
    return next((n for n in holder.node_tree.nodes if n.bl_idname=='LEALineFunctionsNode' and n.enabled),None) if holder and holder.node_tree else None


def rules():
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Material Line Functions require main-thread preparation; use F12')
    return {(m.library.filepath if m.library else '',m.name_full):(*n.outline_color,n.outline_amount)
            for m in bpy.data.materials if (n:=node(m)) and n.outline_on}


def map_node():
    return SimpleNamespace(source_type='OBJECTCOLOR',rule_mode=True,rule_colors=rules(),image=None,
        object_color_selection_mode='NAME',object_color_index=0,object_color_name='',uv_source='OBJECTUV',
        uv_selection_mode='INDEX',uv_index=0,uv_name='',tiling=(1.,1.),offset=(0.,0.),rotation=0.,
        wrap_mode_u='CLAMP',wrap_mode_v='CLAMP',filter_mode='POINT')


def values(mesh,obj,n):
    import numpy as np
    colors=np.zeros((max(1,len(obj.material_slots)),4),np.float32)
    for i,slot in enumerate(obj.material_slots):
        mat=slot.material
        if mat:colors[i]=n.rule_colors.get((mat.library.filepath if mat.library else '',mat.name_full),(0,0,0,0))
    indices=np.empty(len(mesh.polygons),np.int32);mesh.polygons.foreach_get('material_index',indices)
    return colors[np.minimum(indices,len(colors)-1)]


def live_field(g,geometry,n):
    color=None
    for material in bpy.data.materials:
        key=(material.library.filepath if material.library else '',material.name_full)
        if key not in n.rule_colors:continue
        selection=g.nodes.new('GeometryNodeMaterialSelection');selection.inputs['Material'].default_value=material
        choose=g.nodes.new('GeometryNodeSwitch');choose.input_type='RGBA'
        choose.inputs['False'].default_value=(0,0,0,0);choose.inputs['True'].default_value=n.rule_colors[key]
        g.links.new(selection.outputs[0],choose.inputs['Switch'])
        if color is not None:g.links.new(color,choose.inputs['False'])
        color=choose.outputs[0]
    return color


class LEA_OT_create_line_functions(bpy.types.Operator):
    bl_idname='lea_line.create_line_functions';bl_label='Create Line Functions';bl_options={'REGISTER','UNDO'}
    @classmethod
    def poll(cls,context):return getattr(context,'material',None) is not None
    def execute(self,context):
        material=context.material
        holder=material.lea_line_functions
        if holder is None:
            holder=bpy.data.materials.new(material.name+' Line Functions');holder.use_nodes=True
            material.lea_line_functions=holder
        n=node(material)
        if n is None:n=holder.node_tree.nodes.new('LEALineFunctionsNode')
        n.outline_on=True;holder.node_tree.nodes.active=n
        return {'FINISHED'}


class LEA_PT_material_functions(bpy.types.Panel):
    bl_label='REA Pencil Line Functions';bl_idname='LEA_PT_material_functions';bl_space_type='PROPERTIES';bl_region_type='WINDOW';bl_context='material'
    @classmethod
    def poll(cls,context):return getattr(context,'material',None) is not None
    def draw(self,context):
        layout=self.layout;m=context.material
        layout.prop(m,'lea_line_functions',text='Line Functions')
        n=node(m)
        if n:n.draw_buttons(context,layout)
        else:layout.operator('lea_line.create_line_functions',icon='ADD')
        layout.label(text='Enable Material Line Functions on the Line Set',icon='INFO')


def register():
    for cls in (LEALineFunctionsNode,LEA_OT_create_line_functions,LEA_PT_material_functions):bpy.utils.register_class(cls)
    bpy.types.Material.lea_line_functions=PointerProperty(type=bpy.types.Material,update=changed)


def unregister():
    if hasattr(bpy.types.Material,'lea_line_functions'):del bpy.types.Material.lea_line_functions
    for cls in (LEA_PT_material_functions,LEA_OT_create_line_functions,LEALineFunctionsNode):
        if cls.is_registered:bpy.utils.unregister_class(cls)
