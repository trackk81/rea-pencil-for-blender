"""Image/screen Texture Maps for native strokes and camera-based live preview.

The UI follows observed original fields. Sampling, blending and GN execution
are independent implementations; original pixel equivalence is not claimed.
"""
import math
import threading
from types import SimpleNamespace
import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty

TAG = 'rea_screen_texture_map'
WRAPS = [('REPEAT','Repeat',''),('CLAMP','Clamp',''),('MIRROR','Mirror',''),
         ('MIRROR_ONCE','Mirror Once',''),('CLIP','Clip','')]


def changed(owner, context):
    if getattr(owner,'bl_idname',None)=='LEATextureMapNode':
        from . import stamp_brush
        stamp_brush.invalidate(owner.image)
    from .ui import _sync_if_enabled
    _sync_if_enabled(owner, context)
    if not hasattr(bpy.data,'scenes'):return
    from . import object_uv
    tree=getattr(owner,'id_data',None)
    for scene in bpy.data.scenes:
        if getattr(getattr(scene,'lea_line',None),'tree',None)==tree:object_uv.request_prepare(scene)


def enabled_changed(brush, context):
    from . import ui
    from . import preview_material
    if ui._sync_busy or preview_material.is_suspended() or not hasattr(bpy.data,'scenes'):return
    with ui.suspend_sync():
        for kind in ('color','size'):
            socket = brush.inputs.get(kind.title()+' Map')
            if getattr(brush,kind+'_map_on') and socket and not socket.is_linked:
                node = brush.id_data.nodes.new('LEATextureMapNode')
                node.name = 'Texture Map'
                node.location = (brush.location.x-320,brush.location.y-(100 if kind=='size' else 0))
                brush.id_data.links.new(node.outputs[0],socket)
                brush.id_data.nodes.active=node
                for candidate in brush.id_data.nodes:candidate.select=candidate==node
    changed(brush,context)


class LEATextureMapNode(bpy.types.Node):
    bl_idname='LEATextureMapNode'
    bl_label='Texture Map'
    bl_icon='GREASEPENCIL'
    source_type: EnumProperty(items=[('IMAGE','Image',''),('OBJECTCOLOR','Object Color','Use a mesh Color Attribute; attribute alpha is ignored')],default='IMAGE',update=changed)
    image: PointerProperty(type=bpy.types.Image,update=changed)
    wrap_mode_u: EnumProperty(items=WRAPS,default='REPEAT',update=changed)
    wrap_mode_v: EnumProperty(items=WRAPS,default='REPEAT',update=changed)
    filter_mode: EnumProperty(items=[('POINT','Point',''),('BILINEAR','Bilinear','')],default='BILINEAR',update=changed)
    tiling: FloatVectorProperty(size=2,default=(1,1),min=-10000,max=10000,update=changed)
    offset: FloatVectorProperty(size=2,default=(0,0),min=-10000,max=10000,update=changed)
    rotation: FloatProperty(subtype='ANGLE',min=-20*math.pi,max=20*math.pi,default=0,update=changed)
    uv_source: EnumProperty(items=[('SCREEN','Screen',''),('OBJECTUV','Object UV','Sample the selected mesh UV layer')],default='SCREEN',update=changed)
    uv_selection_mode: EnumProperty(name='Selection Mode',items=[('INDEX','Index',''),('NAME','Name','')],default='INDEX',update=changed)
    uv_index: IntProperty(name='Index',min=0,max=7,default=0,update=changed)
    uv_name: StringProperty(name='Name',default='UVMap',update=changed)

    object_color_selection_mode: EnumProperty(name='Selection Mode',items=[('INDEX','Index',''),('NAME','Name','')],default='INDEX',update=changed)
    object_color_index: IntProperty(name='Index',min=0,max=7,default=0,update=changed)
    object_color_name: StringProperty(name='Name',default='Color',update=changed)

    @classmethod
    def poll(cls,tree):return tree.bl_idname=='LEALineTree'
    def init(self,context):
        self.width=140
        self.outputs.new('LEALineSocket','Output')
    def draw_buttons(self,context,layout):
        if self.id_data.ui_layout!='ORIGINAL':
            if self.source_type=='IMAGE':layout.template_ID(self,'image',open='image.open')
            else:layout.prop(self,'object_color_name',text='Color Attribute')
    def draw_buttons_ext(self,context,layout):
        draw_image(layout,self) if self.source_type=='IMAGE' else draw_object_color(layout,self)


def active(brush,kind):
    if not getattr(brush,kind+'_map_on',False):return None
    amount=getattr(brush,'color_map_opacity' if kind=='color' else 'size_map_amount',0.)
    if amount<=0:return None
    socket=brush.inputs.get(kind.title()+' Map')
    if not socket or not socket.is_linked:return None
    node=socket.links[0].from_node
    if node.bl_idname!='LEATextureMapNode':raise ValueError(kind.title()+' Map requires a Texture Map node')
    if node.source_type=='OBJECTCOLOR':return node
    image=node.image
    if image is None:return None
    check_image(image)
    return node


def check_image(image):
    if image.source not in {'FILE','GENERATED'} or image.type not in {'IMAGE','UV_TEST'}:
        raise ValueError('Texture Map currently supports single FILE/GENERATED images, not sequences, movies or tiles')
    if image.alpha_mode!='STRAIGHT':
        raise ValueError('Texture Map currently requires Straight image alpha; other alpha modes are not verified')
    # Blender FILE images are lazily loaded: has_data may be false while a
    # valid, loadable size exists. Reading size/pixels requests the buffer.
    if min(image.size)<=0:
        raise ValueError('Texture Map image has no pixel data: '+image.name)


def maps(brush):
    result={kind:node for kind in ('color','size') if (node:=active(brush,kind)) is not None}
    if getattr(brush,'material_functions_enabled',False):
        from . import material_functions
        result['functions']=material_functions.map_node()
    from . import stamp_brush
    if getattr(brush,'distortion_enabled',False) and getattr(brush,'distortion_map_amount',0)>0:
        node=stamp_brush.detail_map(brush,'distortion')
        if node:result={'distortion':node,**result}
    return result


def validate(brush,scene=None,line_set=None):
    from . import stamp_brush
    if stamp_brush.active(brush):stamp_brush.detail_map(brush,'brush')
    result=maps(brush)
    from . import object_uv
    if scene and any(object_uv.surface_source(n) for n in result.values()):
        object_uv.plans(scene,line_set,final=True)
    missing=any(getattr(brush,k+'_map_on',False) and getattr(brush,'color_map_opacity' if k=='color' else 'size_map_amount',0)>0
                and k not in result for k in ('color','size'))
    return ['Texture Map: choose an image to apply the enabled map.'] if missing else []


def transformed(node,uv):
    uv=np.asarray(uv,dtype=np.float64)
    c,s=math.cos(node.rotation),math.sin(node.rotation)
    if getattr(node,'uv_source','SCREEN')=='OBJECTUV':s=-s
    # Observed native Screen probes favor clockwise rotation about UV origin,
    # followed by tiling and offset. Object UV native probes rotate oppositely.
    # Edge coverage still differs.
    return (uv@np.array(((c,-s),(s,c))))*np.asarray(node.tiling)+np.asarray(node.offset)


def wrap(values,mode):
    if mode=='REPEAT':return np.mod(values,1.),np.ones_like(values,dtype=bool)
    if mode=='MIRROR':return 1.-np.abs(np.mod(values,2.)-1.),np.ones_like(values,dtype=bool)
    if mode=='MIRROR_ONCE':return np.clip(np.abs(values),0,1),np.ones_like(values,dtype=bool)
    if mode=='CLIP':return np.clip(values,0,1),(values>=0)&(values<1)
    return np.clip(values,0,1),np.ones_like(values,dtype=bool)


def pixels(image):
    w,h=image.size
    data=np.empty(len(image.pixels),np.float32);image.pixels.foreach_get(data)
    channels=len(data)//(w*h)
    if channels not in {1,2,3,4} or len(data)!=w*h*channels:
        raise ValueError('Texture Map image has an unsupported pixel layout')
    data=data.reshape(h,w,channels)
    if channels<4:
        rgba=np.ones((h,w,4),np.float32)
        rgba[:,:,:3]=data[:,:,:1] if channels<=2 else data
        if channels==2:rgba[:,:,3]=data[:,:,1]
        data=rgba
    return data


def sample(node,uv,data=None):
    """Point or four-tap bilinear filtering with independent per-axis wraps."""
    data=pixels(node.image) if data is None else data
    h,w=data.shape[:2];p=transformed(node,uv)*[w,h]
    def tap(index):
        u,um=wrap((index[:,0]+.5)/w,node.wrap_mode_u)
        v,vm=wrap((index[:,1]+.5)/h,node.wrap_mode_v)
        x=np.minimum(w-1,np.floor(u*w).astype(np.int64));y=np.minimum(h-1,np.floor(v*h).astype(np.int64))
        return data[y,x]*(um&vm)[:,None]
    if node.filter_mode=='POINT':return tap(np.floor(p))
    p-=.5;floor=np.floor(p);t=p-floor
    return ((tap(floor)*(1-t[:,0,None])+tap(floor+[1,0])*t[:,0,None])*(1-t[:,1,None])+
            (tap(floor+[0,1])*(1-t[:,0,None])+tap(floor+[1,1])*t[:,0,None])*t[:,1,None])


def render_payload(scene,line_set,brush):
    chosen=maps(brush)
    from . import reduction_reference
    reference=reduction_reference.capture(scene,brush)
    from . import stroke_variation
    variation=stroke_variation.snapshot(brush)
    from . import stamp_brush
    stamps=stamp_brush.spec(brush)
    from . import backend
    if stamps is not None:stamps['texture_width']=brush.thickness*backend.width_scale(scene,line_set)
    from . import hidden_mask
    mask_names=hidden_mask.names(brush)
    if not chosen and reference is None and variation is None and not mask_names and stamps is None:return None
    from . import object_uv
    surface=object_uv.Sampler(scene,line_set,chosen) if any(object_uv.surface_source(n) for n in chosen.values()) else None
    # No Blender IDs are retained by the worker's shader payload.
    frozen={kind:SimpleNamespace(**{name:tuple(getattr(n,name)) if name in {'tiling','offset'} else getattr(n,name)
                 for name in ('source_type','object_color_selection_mode','object_color_index','object_color_name','uv_source','uv_selection_mode','uv_index','uv_name','tiling','offset','rotation','wrap_mode_u','wrap_mode_v','filter_mode')})
            for kind,n in chosen.items()}
    buffers={kind:pixels(n.image) for kind,n in chosen.items() if n.source_type=='IMAGE'}
    for buffer in buffers.values():buffer.setflags(write=False)
    camera_matrix=np.array(scene.camera.matrix_world,dtype=np.float64);camera_matrix.setflags(write=False)
    return {'brush':SimpleNamespace(color_map_opacity=float(brush.color_map_opacity),size_map_amount=float(brush.size_map_amount),distortion_map_amount=float(getattr(brush,'distortion_map_amount',0))*backend.width_scale(scene,line_set)),
            'chosen':frozen,'buffers':buffers,'surface':surface,'reference_reduction':reference,'variation':variation,'stamps':stamps,'hidden_mask_names':mask_names,
            'camera_matrix':camera_matrix,
            'width':max(1,int(scene.render.resolution_x*scene.render.resolution_percentage/100)),
            'height':max(1,int(scene.render.resolution_y*scene.render.resolution_percentage/100))}


def final_shaders(scene,layer,line_set):
    from . import object_uv
    if threading.current_thread() is not threading.main_thread():
        cached=object_uv.cached_payload(scene.name_full,scene.frame_current,line_set.name)
        if cached is not None:return [] if cached.get('noop') else payload_shaders(cached)
    from . import backend,routing
    if not line_set.name.startswith(backend.PREFIX) or not scene.lea_line.tree:return []
    text=line_set.name[len(backend.PREFIX):];index=int(text.split(':')[0]);visibility=text.split(':')[1]
    specifications=backend._specifications(scene.lea_line.tree)
    if index>=len(specifications):return []
    node,common=specifications[index]
    suffix=text[len(str(index))+1+len(visibility):]
    job=next((j for j in routing.jobs(node,common,visibility) if j['suffix']==suffix),None)
    if job is None:return []
    brush=job['brush'];chosen=maps(brush)
    from . import reduction_reference
    reference=reduction_reference.active(brush)
    if getattr(brush,'distance_alpha',False) and threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Distance alpha snapshot is not ready. Use REA Render or F12 for a single-frame render.')
    from . import stroke_variation
    variation=stroke_variation.snapshot(brush)
    from . import stamp_brush
    if not chosen and not reference and variation is None and not stamp_brush.active(brush):return []
    if variation and threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Stroke variation snapshot is not ready. Use REA Render or F12.')
    if (reference or any(object_uv.surface_source(n) for n in chosen.values())) and threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Surface map snapshot is not ready. Use REA Render or F12 for a single-frame render; Surface map animation is not supported yet.')
    return payload_shaders(render_payload(scene,node,brush))


def payload_shaders(payload):
    if not payload['chosen'] and payload.get('reference_reduction') is None and payload.get('variation') is None and payload.get('stamps') is None:return []
    brush=payload['brush'];chosen=payload['chosen'];buffers=payload['buffers'];surface=payload['surface']
    camera_matrix=payload['camera_matrix'];width=payload['width'];height=payload['height']
    from freestyle.types import StrokeShader
    from freestyle.shaders import SamplingShader
    class ScreenMapShader(StrokeShader):
        def shade(self,stroke):
            points=list(stroke)
            if not points:return
            uv=np.array([(v.point.x/width,v.point.y/height) for v in points])
            world=None
            reference=payload.get('reference_reduction')
            if surface or reference:
                camera_points=np.array([v.point_3d[:] for v in points])
                world=camera_points@camera_matrix[:3,:3].T+camera_matrix[:3,3]
            samples={kind:(surface.sample(world,kind) if n.source_type=='OBJECTCOLOR' else sample(n,surface.sample(world,kind) if n.uv_source=='OBJECTUV' else uv,buffers[kind])) for kind,n in chosen.items() if kind!='distortion'}
            if reference:
                from . import reduction_reference
                size,alpha=reduction_reference.factors(reference,world)
                for i,v in enumerate(points):
                    v.attribute.thickness=tuple(max(0.,a*size[i]) for a in v.attribute.thickness)
                    v.attribute.alpha*=float(alpha[i])
            for i,v in enumerate(points):
                if 'color' in samples:
                    rgba=samples['color'][i];fac=brush.color_map_opacity*rgba[3]
                    v.attribute.color=tuple((1-fac)*v.attribute.color[j]+fac*rgba[j] for j in range(3))
                if 'functions' in samples:
                    rgba=samples['functions'][i];fac=rgba[3]
                    v.attribute.color=tuple((1-fac)*v.attribute.color[j]+fac*rgba[j] for j in range(3))
                if 'size' in samples:
                    rgba=samples['size'][i];luma=float(rgba[:3]@np.array((.2126,.7152,.0722)))
                    fac=(1-brush.size_map_amount)+brush.size_map_amount*luma*rgba[3]
                    v.attribute.thickness=tuple(max(0.,a*fac) for a in v.attribute.thickness)
    result=[]
    if 'distortion' in chosen:
        class DistortionShader(StrokeShader):
            def shade(self,stroke):
                points=list(stroke)
                if len(points)<2:return
                from . import distortion_map
                xy=np.array([v.point[:] for v in points]);uv=xy/np.array([width,height])
                rgb=sample(chosen['distortion'],uv,buffers['distortion'])[:,:3].mean(1)
                offset=distortion_map.normals(xy)*rgb[:,None]*brush.distortion_map_amount
                for i,v in enumerate(points):v.point=tuple(xy[i]+offset[i])
                stroke.update_length()
        result.append(DistortionShader())
    result.extend([SamplingShader(1.),ScreenMapShader()])
    if payload.get('variation'):
        from . import stroke_variation
        result.append(stroke_variation.shader(payload['variation']))
    if payload.get('stamps'):
        from . import stamp_brush
        result.append(stamp_brush.sprite_shader(payload['stamps']))
    return result


def draw_image(layout,node):
    layout.use_property_split=True;layout.use_property_decorate=False
    layout.template_ID(node,'image',new='image.new',open='image.open')
    col=layout.column(align=True);col.prop(node,'wrap_mode_u',text='Wrap Mode U');col.prop(node,'wrap_mode_v',text='V')
    layout.prop(node,'filter_mode',text='Filter Mode')
    layout.prop(node,'uv_source',text='UV Source')
    col=layout.column(align=True);col.enabled=node.uv_source=='OBJECTUV'
    col.prop(node,'uv_selection_mode',text='Selection Mode')
    row=col.row();row.enabled=node.uv_selection_mode=='INDEX';row.prop(node,'uv_index',text='Index')
    row=col.row();row.enabled=node.uv_selection_mode=='NAME';row.prop(node,'uv_name',text='Name')
    col=layout.column(align=True);col.prop(node,'tiling',index=0,text='Tiling U');col.prop(node,'tiling',index=1,text='V')
    col=layout.column(align=True);col.prop(node,'offset',index=0,text='Offset U');col.prop(node,'offset',index=1,text='V')
    layout.prop(node,'rotation',text='Rotation')


def draw_object_color(layout,node):
    layout.use_property_split=True;layout.use_property_decorate=False
    col=layout.column(align=True);col.prop(node,'object_color_selection_mode',text='Selection Mode')
    row=col.row();row.enabled=node.object_color_selection_mode=='INDEX';row.prop(node,'object_color_index',text='Index')
    row=col.row();row.enabled=node.object_color_selection_mode=='NAME';row.prop(node,'object_color_name',text='Name')



def selected(context):
    space=getattr(context,'space_data',None)
    tree=getattr(space,'edit_tree',None)
    node=tree.nodes.active if tree and tree.bl_idname=='LEALineTree' else None
    return node if node and node.bl_idname=='LEATextureMapNode' else None


class _TexturePanel:
    bl_space_type='NODE_EDITOR';bl_region_type='UI';bl_category='REA Pencil Line';bl_order=100
    @classmethod
    def poll(cls,context):return selected(context) is not None


class LEA_PT_texture_source(_TexturePanel,bpy.types.Panel):
    bl_label='Source Type';bl_idname='LEA_PT_texture_source'
    def draw(self,context):
        node=selected(context);row=self.layout.row(align=True)
        row.prop_enum(node,'source_type','IMAGE')
        row.prop_enum(node,'source_type','OBJECTCOLOR')


class LEA_PT_texture_image(_TexturePanel,bpy.types.Panel):
    bl_label='Image and UV';bl_idname='LEA_PT_texture_image';bl_order=101
    @classmethod
    def poll(cls,context):return selected(context) is not None and selected(context).source_type=='IMAGE'
    def draw(self,context):draw_image(self.layout,selected(context))


class LEA_PT_texture_object_color(_TexturePanel,bpy.types.Panel):
    bl_label='Color Attribute';bl_idname='LEA_PT_texture_object_color';bl_order=102
    @classmethod
    def poll(cls,context):return selected(context) is not None and selected(context).source_type=='OBJECTCOLOR'
    def draw(self,context):draw_object_color(self.layout,selected(context))


class LEA_OT_texture_select(bpy.types.Operator):
    bl_idname='lea_line.texture_select';bl_label='Texture Map'
    tree_name:StringProperty();brush_name:StringProperty();kind:EnumProperty(items=[('color','Color Map',''),('size','Size Map','')])
    def execute(self,context):
        tree=bpy.data.node_groups.get(self.tree_name);brush=tree.nodes.get(self.brush_name) if tree else None
        if brush is None or brush.bl_idname!='LEABrushNode':return {'CANCELLED'}
        socket=brush.inputs.get(self.kind.title()+' Map')
        if not socket or not socket.is_linked:return {'CANCELLED'}
        node=socket.links[0].from_node
        if node.bl_idname!='LEATextureMapNode':return {'CANCELLED'}
        tree.nodes.active=node
        for candidate in tree.nodes:candidate.select=candidate==node
        return {'FINISHED'}


def draw_brush_map(context,layout,brush,kind):
    title=kind.title()+' Map';split=layout.row().split(factor=.4);split.use_property_split=False
    heading=split.row(align=True);heading.alignment='RIGHT'
    heading.prop(brush,kind+'_map_on',text='');heading.label(text=title);heading.label(text='',icon='TEXTURE')
    controls=split.split(factor=1.).row(align=True);controls.enabled=getattr(brush,kind+'_map_on')
    socket=brush.inputs.get(title);node=socket.links[0].from_node if socket and socket.is_linked else None
    op=controls.operator('lea_line.texture_select',text=node.name if node else 'None')
    op.tree_name=brush.id_data.name;op.brush_name=brush.name;op.kind=kind
    controls.prop(brush,'color_map_opacity' if kind=='color' else 'size_map_amount',text='Opacity' if kind=='color' else 'Amount')


def register():
    from . import stamp_brush
    bpy.utils.register_class(stamp_brush.LEA_OT_detail_map)
    from . import material_functions
    material_functions.register()
    from . import free_view
    free_view.register()
    for cls in (LEATextureMapNode,LEA_OT_texture_select,LEA_PT_texture_source,LEA_PT_texture_image,LEA_PT_texture_object_color):bpy.utils.register_class(cls)
    import parameter_editor
    if final_shaders not in parameter_editor.callbacks_modifiers_post:parameter_editor.callbacks_modifiers_post.append(final_shaders)
    from . import object_uv
    object_uv.register()
    from . import reduction_reference
    reduction_reference.register()


def unregister():
    from . import stamp_brush
    stamp_brush.clear()
    if stamp_brush.LEA_OT_detail_map.is_registered:bpy.utils.unregister_class(stamp_brush.LEA_OT_detail_map)
    from . import material_functions
    from . import free_view
    free_view.unregister()
    material_functions.unregister()
    from . import reduction_reference
    reduction_reference.unregister()
    from . import object_uv
    object_uv.unregister()
    import parameter_editor
    if final_shaders in parameter_editor.callbacks_modifiers_post:parameter_editor.callbacks_modifiers_post.remove(final_shaders)
    for cls in reversed((LEATextureMapNode,LEA_OT_texture_select,LEA_PT_texture_source,LEA_PT_texture_image,LEA_PT_texture_object_color)):
        if cls.is_registered:bpy.utils.unregister_class(cls)
