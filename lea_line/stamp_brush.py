"""Fixed screen-oriented stamp sprites on native Freestyle and GP backends.

The controls are reconstructed from shipped declarations and native probes.
Stamp packing, raster AA and random generators are independent implementations.
"""
import json,math,threading
from collections import OrderedDict
import numpy as np
import bpy
from bpy.props import EnumProperty,FloatProperty,IntProperty,BoolProperty,StringProperty

TAG='rea_stamp_mask'
FIELDS=('brush_type','brush_map_on','brush_map_opacity','stretch','angle','groove','groove_number',
        'stamp_size','stamp_size_random','stamp_antialiasing','horizontal_space','horizontal_space_random',
        'vertical_space','vertical_space_random','stamp_reduction_start','stamp_reduction_end',
        'distortion_map_on','distortion_map_amount')
_images=OrderedDict()
_revisions=OrderedDict()

def invalidate(image):
    if image is None:return
    key=image.as_pointer();_revisions[key]=_revisions.get(key,0)+1;_revisions.move_to_end(key)
    while len(_revisions)>64:_revisions.popitem(last=False)

def active(brush):return getattr(brush,'brush_type','SIMPLE')!='SIMPLE'

def detail_map(brush,kind):
    if not getattr(brush,kind+'_map_on',False):return None
    owner=getattr(brush,'detail_source',None)
    if owner is None:return None
    socket=owner.inputs.get('Brush Map' if kind=='brush' else 'Distortion Map')
    if not socket or not socket.is_linked:return None
    node=socket.links[0].from_node
    if node.bl_idname!='LEATextureMapNode':raise ValueError(kind.title()+' Map requires a Texture Map node')
    if node.source_type!='IMAGE':raise ValueError(kind.title()+' Map currently supports Image sources')
    if node.image is None:return None
    from . import texture_maps
    texture_maps.check_image(node.image)
    if kind=='distortion' and node.uv_source!='SCREEN':raise ValueError('Distortion Map currently supports Screen UV only')
    if kind=='brush' and node.uv_source!='SCREEN':raise ValueError('Brush Map currently uses stamp-local UV with the Screen option')
    return node

def spec(brush):
    if not active(brush):return None
    result={key:getattr(brush,key) for key in FIELDS if not key.startswith('distortion')}
    # Original AA is a Multiple-only control. Keep Normal's existing default
    # edge coverage fixed, including its bitmap and live-graph cache identity.
    if brush.brush_type=='NORMAL':result['stamp_antialiasing']=.5
    result['seed']=getattr(brush,'random_seed',0)
    return result

def mask(brush,width):
    if threading.current_thread() is not threading.main_thread():raise RuntimeError('Stamp images require main-thread preparation; use F12')
    from . import texture_maps as tex
    values=spec(brush);node=detail_map(brush,'brush')
    source=(node.image.as_pointer(),node.image.name_full,tuple(node.image.size),_revisions.get(node.image.as_pointer(),0)) if node else None
    mapping=(node.wrap_mode_u,node.wrap_mode_v,node.filter_mode,tuple(node.tiling),tuple(node.offset),node.rotation) if node else None
    key=json.dumps((values,width,source,mapping),sort_keys=True)
    image=_images.get(key)
    try:
        if image is not None and bpy.data.images.get(image.name)!=image:image=None
    except ReferenceError:image=None
    if image and bpy.data.images.get(image.name)==image:
        _images.move_to_end(key);return image
    buffer=tex.pixels(node.image) if node else None
    size=128;yy,xx=np.mgrid[:size,:size];x=(xx+.5)/size*2-1;y=(yy+.5)/size*2-1
    c,s=math.cos(brush.angle),math.sin(brush.angle);u=c*x+s*y;v=-s*x+c*y
    ax=max(.02,1-max(0.,brush.stretch));ay=max(.02,1+min(0.,brush.stretch))
    def ellipse(cx,cy,r):
        d=np.sqrt(((u-cx)/ax)**2+((v-cy)/ay)**2)/r
        aa=max(.001,values['stamp_antialiasing'])
        return np.clip((1-d)*width*r/(2*aa)+.5,0,1)
    alpha=ellipse(0,0,1.)
    if brush.brush_type=='MULTIPLE':
        rng=np.random.default_rng(values['seed']);r=max(.01,brush.stamp_size/100.)
        sx=max(.025,2*r*(1+brush.horizontal_space));sy=max(.025,2*r*(1+brush.vertical_space))
        xs=np.arange(-1+r,1,sx);ys=np.arange(-1+r,1,sy)
        if len(xs)*len(ys)>4096:raise ValueError('Multiple stamp grid exceeds 4096 components; increase Size or spacing')
        packed=np.zeros_like(alpha)
        for cy in ys:
            for cx in xs:
                if cx*cx+cy*cy>1:continue
                radius=max(.01,r*(1+rng.uniform(-1,1)*brush.stamp_size_random/100.))
                px=cx+rng.uniform(-1,1)*sx*brush.horizontal_space_random/200.
                py=cy+rng.uniform(-1,1)*sy*brush.vertical_space_random/200.
                packed=np.maximum(packed,ellipse(px,py,radius))
        alpha*=packed
    if brush.groove:
        phase=(np.arctan2(v,u)+math.pi)/(2*math.pi)*brush.groove_number
        alpha*=1-brush.groove*(.5+.5*np.cos(phase*2*math.pi))
    if node:
        uv=np.column_stack(((x.ravel()+1)/2,(y.ravel()+1)/2))
        rgb=tex.sample(node,uv,buffer)[:,:3].mean(1).reshape(size,size)
        alpha*=1-brush.brush_map_opacity+brush.brush_map_opacity*rgb
    data=np.ones((size,size,4),np.float32);data[:,:,3]=np.clip(alpha,0,1)
    image=bpy.data.images.new('.rea stamp mask',width=size,height=size,float_buffer=True);image[TAG]=1
    image.pixels.foreach_set(data.ravel());image.update();image.pack();_images[key]=image
    while len(_images)>16:_images.popitem(last=False)
    prune();return image

def prune():
    for image in list(bpy.data.images):
        if image.get(TAG) and image.users==0 and image not in _images.values():bpy.data.images.remove(image)

def sync_style(style,brush,width,setter):
    graph=style.node_tree
    on=active(brush)
    if graph is None:return
    multiply=graph.nodes.get('REA Stamp Alpha');out=graph.nodes.get('REA Linear Distance Alpha')
    if not on:
        if multiply and out:
            base=multiply.inputs[0].links[0].from_socket
            if not out.inputs['Alpha'].is_linked or out.inputs['Alpha'].links[0].from_socket!=base:graph.links.new(base,out.inputs['Alpha'])
            setter(graph.nodes['REA Stamp Image'],'image',None)
        return
    if multiply is None:
        uv=graph.nodes.new('ShaderNodeUVAlongStroke');uv.name='REA Stamp UV'
        # Blender4.4 Freestyle exposes V from0(top) to-1(bottom).
        shift=graph.nodes.new('ShaderNodeVectorMath');shift.operation='ADD';shift.inputs[1].default_value=(0.,1.,0.)
        graph.links.new(uv.outputs[0],shift.inputs[0])
        tex=graph.nodes.new('ShaderNodeTexImage');tex.name='REA Stamp Image';tex.extension='EXTEND'
        graph.links.new(shift.outputs[0],tex.inputs['Vector'])
        multiply=graph.nodes.new('ShaderNodeMath');multiply.name='REA Stamp Alpha';multiply.operation='MULTIPLY'
        graph.links.new(out.inputs['Alpha'].links[0].from_socket,multiply.inputs[0]);graph.links.new(tex.outputs['Alpha'],multiply.inputs[1])
    setter(graph.nodes['REA Stamp Image'],'image',mask(brush,width))
    if not out.inputs['Alpha'].is_linked or out.inputs['Alpha'].links[0].from_socket!=multiply.outputs[0]:graph.links.new(multiply.outputs[0],out.inputs['Alpha'])
    setter(style,'texture_spacing',1.);setter(style,'caps','BUTT')

def sprite_shader(values):
    from freestyle.types import StrokeShader,StrokeVertex
    class StampSprites(StrokeShader):
        def shade(self,stroke):
            # StrokeVertex's copy constructor does not preserve all stroke-owned
            # attribute/length state. Freeze it before removing source vertices.
            source=[(StrokeVertex(p),tuple(p.point),sum(p.attribute.thickness),
                     float(p.u) if math.isfinite(p.u) else 0.,tuple(p.attribute.color),
                     p.attribute.alpha,p.attribute.visible) for p in stroke]
            if len(source)>16384:raise ValueError('Stamp stroke exceeds 16384 sprite samples')
            selected=[]
            for base,position,width,t,color,alpha,visible in source:
                if not visible:continue
                cx,cy=position;diameter=max(.001,width)
                factor=values['stamp_reduction_start']*(1-t)+values['stamp_reduction_end']*t
                diameter*=factor
                if diameter<1e-6:continue
                selected.append((position,diameter,color,alpha))
            if not selected:stroke.remove_all_vertices();return
            # Allocate once in native code instead of repeatedly inserting vertices.
            stroke.resample(len(selected)*4)
            for i,p in enumerate(stroke):
                position,diameter,color,alpha=selected[i//4];cx,cy=position
                f=(0.,.0001,.9999,1.)[i%4];p.point=(cx+(f-.5)*diameter,cy)
                p.attribute.thickness=(diameter*.5,diameter*.5);p.attribute.visible=i%4!=3
                p.attribute.color=color;p.attribute.alpha=alpha
            stroke.update_length()
            total_length=stroke.length_2d
            for i,p in enumerate(stroke):
                p.curvilinear_abscissa=(0.,.0001,.9999,1.)[i%4]*selected[i//4][1]
                p.stroke_length=total_length
    return StampSprites()

def live_geometry(g,geometry,brush):
    if not active(brush):return geometry
    from . import preview_stroke as f
    # Retain native radius fields; the bitmap defines fixed screen orientation.
    parameter=g.nodes.new('GeometryNodeSplineParameter')
    factor=f._math(g,'ADD','Stamp End Reduction',
        f._math(g,'MULTIPLY','Stamp Reduction Delta',parameter.outputs['Factor'],brush.stamp_reduction_end-brush.stamp_reduction_start),brush.stamp_reduction_start)
    radius=g.nodes.new('GeometryNodeInputRadius').outputs[0]
    scaled=f._math(g,'MULTIPLY','Stamp Sprite Radius',radius,factor)
    node=g.nodes.new('GeometryNodeSetCurveRadius');g.links.new(geometry,node.inputs['Curve']);g.links.new(scaled,node.inputs['Radius'])
    return node.outputs[0]

def sync_material(material,brush,width,setter):
    ink=material.grease_pencil
    if active(brush):
        setter(ink,'mode','DOTS');setter(ink,'stroke_style','TEXTURE');setter(ink,'stroke_image',mask(brush,width))
        setter(ink,'mix_stroke_factor',1.);setter(ink,'alignment_mode','FIXED');setter(ink,'texture_scale',(1.,1.));setter(ink,'texture_angle',0.)
    else:
        setter(ink,'mode','LINE');setter(ink,'stroke_style','SOLID')
        if ink.stroke_image and ink.stroke_image.get(TAG):setter(ink,'stroke_image',None)

def map_enabled(owner,context):
    from . import ui
    if ui._sync_busy or not hasattr(bpy.data,'scenes'):return
    with ui.suspend_sync():
        for kind,name in (('brush','Brush Map'),('distortion','Distortion Map')):
            socket=owner.inputs.get(name)
            if getattr(owner,kind+'_map_on') and socket and not socket.is_linked:
                node=owner.id_data.nodes.new('LEATextureMapNode');node.location=(owner.location.x-250,owner.location.y)
                owner.id_data.links.new(node.outputs[0],socket)
    from .texture_maps import changed
    changed(owner,context)

def clear():_images.clear();_revisions.clear();prune()

class LEA_OT_detail_map(bpy.types.Operator):
    bl_idname='lea_line.detail_map';bl_label='Brush Detail Map';bl_options={'REGISTER','UNDO'}
    tree_name:StringProperty();node_name:StringProperty()
    kind:EnumProperty(items=[('brush','Brush Map',''),('distortion','Distortion Map','')])
    action:EnumProperty(items=[('CREATE','Create',''),('SELECT','Select',''),('UNLINK','Unlink','')])
    def execute(self,context):
        from . import ui
        tree=bpy.data.node_groups.get(self.tree_name);owner=tree.nodes.get(self.node_name) if tree else None
        if not owner or owner.bl_idname!='LEABrushDetailNode':return {'CANCELLED'}
        socket=owner.inputs.get('Brush Map' if self.kind=='brush' else 'Distortion Map')
        with ui.suspend_sync():
            if self.action=='UNLINK':
                for link in list(socket.links):tree.links.remove(link)
                setattr(owner,self.kind+'_map_on',False)
            elif self.action=='SELECT':
                if not socket.is_linked:return {'CANCELLED'}
                ui._activate_node(tree,socket.links[0].from_node)
            else:
                n=tree.nodes.new('LEATextureMapNode');n.location=(owner.location.x-250,owner.location.y)
                tree.links.new(n.outputs[0],socket);setattr(owner,self.kind+'_map_on',True);ui._activate_node(tree,n)
        ui._sync_if_enabled(owner,context);return {'FINISHED'}

def draw_map(layout,owner,kind):
    name='Brush Map' if kind=='brush' else 'Distortion Map'
    row=layout.row(align=True);row.prop(owner,kind+'_map_on',text=name)
    socket=owner.inputs.get(name);node=socket.links[0].from_node if socket and socket.is_linked else None
    controls=row.row(align=True);controls.enabled=getattr(owner,kind+'_map_on')
    op=controls.operator('lea_line.detail_map',text=node.name if node else 'None',icon='TEXTURE' if node else 'ADD')
    op.tree_name=owner.id_data.name;op.node_name=owner.name;op.kind=kind;op.action='SELECT' if node else 'CREATE'
    if node:
        op=controls.operator('lea_line.detail_map',text='',icon='X');op.tree_name=owner.id_data.name;op.node_name=owner.name;op.kind=kind;op.action='UNLINK'
    controls.prop(owner,'brush_map_opacity' if kind=='brush' else 'distortion_map_amount',text='Opacity' if kind=='brush' else 'Amount')

def draw_editor(context,layout,detail):
    from . import reference_detail as ref
    ref._preset_row(layout,detail,'BRUSH')
    ref._enum(layout,detail,'brush_type','Brush Type',(('NORMAL','Normal'),('MULTIPLE','Multiple'),('SIMPLE','Simple')))
    col=layout.column();col.enabled=detail.brush_type!='SIMPLE';draw_map(col,detail,'brush')
    col.prop(detail,'stretch');col.prop(detail,'angle')
    row=col.row(align=True);row.prop(detail,'groove');row.prop(detail,'groove_number')
    multi=col.column();multi.enabled=detail.brush_type=='MULTIPLE'
    row=multi.row(align=True);row.prop(detail,'stamp_size');row.prop(detail,'stamp_size_random',text='Random')
    multi.prop(detail,'stamp_antialiasing')
    for a,b in (('horizontal_space','horizontal_space_random'),('vertical_space','vertical_space_random')):
        row=multi.row(align=True);row.prop(detail,a);row.prop(detail,b,text='Random')
    row=col.row(align=True);row.label(text='Reduction');row.prop(detail,'stamp_reduction_start',text='Start');row.prop(detail,'stamp_reduction_end',text='End')
