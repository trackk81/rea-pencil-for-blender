"""Main-thread style snapshots and GPU image/curve sampling for line previews."""
import math
import numpy as np
import bpy
from . import texture_maps,curves,object_uv

WRAP={'REPEAT':0,'CLAMP':1,'MIRROR':2,'MIRROR_ONCE':3,'CLIP':4}


def validate(brush):
    chosen=texture_maps.maps(brush)
    if set(chosen)-{'color','size'}:raise ValueError('GPU: 왜곡 맵·Line Functions는 Native를 사용하세요')
    for n in chosen.values():
        if n.source_type=='IMAGE':
            texture_maps.check_image(n.image)
            if n.image.size[0]*n.image.size[1]>16_777_216:raise ValueError('GPU: 16M 픽셀을 넘는 맵은 Native를 사용하세요')
    if (getattr(brush,'loop_start','KEEP')!='KEEP' or getattr(brush,'loop_orientation','KEEP')!='KEEP') and needs_chain(brush):
        raise ValueError('GPU: 사용자 지정 루프 시작/방향은 Native를 사용하세요')
    return chosen


def needs_chain(brush):
    return any(getattr(brush,k,False) for k in ('dashed','taper','size_curve_enabled','alpha_curve_enabled'))


def snapshot(brush,scale=1.,hidden=False,depth=False):
    chosen=validate(brush);table=np.ones((4097,4),np.float32)
    size_on=bool(getattr(brush,'size_curve_enabled',False) or getattr(brush,'taper',False));alpha_on=bool(getattr(brush,'alpha_curve_enabled',False))
    if size_on or alpha_on:
        from . import preview_stroke
        tree=bpy.data.node_groups.new('.rea GPU curve snapshot','ShaderNodeTree')
        try:
            node=tree.nodes.new('ShaderNodeFloatCurve')
            for channel,on,payload in ((0,size_on,preview_stroke._size_data(brush)),(1,alpha_on,getattr(brush,'alpha_curve_data',None))):
                if not on:continue
                curves.apply_mapping(node.mapping,payload);node.mapping.initialize();node.mapping.update()
                values=[node.mapping.evaluate(node.mapping.curves[0],i/4096) for i in range(4097)]
                table[:,channel]=np.maximum(0,np.clip(values,payload['clip_min_y'],payload['clip_max_y']))
        finally:bpy.data.node_groups.remove(tree)
    table[:,1]=np.clip(table[:,1],0,1)
    params=np.zeros((17,4),np.float32)
    params[3]=(size_on,alpha_on,max(1.,float(table[:,0].max())),needs_chain(brush))
    params[4]=(bool(getattr(brush,'dashed',False)),max(.00001,getattr(brush,'dash',10)*scale),max(0,getattr(brush,'gap',5)*scale),getattr(brush,'dash_phase',0)*scale)
    params[5]=(2 if hidden else 1 if depth else 0,8,getattr(brush,'color_map_opacity',1.),getattr(brush,'size_map_amount',1.))
    params[6]=(bool(getattr(brush,'split_angle_enabled',False)),max(0.,math.pi-getattr(brush,'split_angle',math.pi/2)),0,0)
    params[7]=(bool(getattr(brush,'line_direction_enabled',False)),getattr(brush,'line_direction',0.),0,0)
    references=[]
    channels=getattr(brush,'distance_channels',{})
    for index,kind in enumerate(('SIZE','ALPHA')):
        spec=channels.get(kind)
        enabled=spec['enabled'] if spec else getattr(brush,'distance_fade' if kind=='SIZE' else 'distance_alpha',False)
        profile=spec.get('profile') if spec else getattr(brush,'distance_curve_table',None)
        reference=spec.get('reference') if spec else getattr(brush,'distance_reference_enabled',False)
        obj=spec.get('object') if spec else getattr(brush,'distance_reference_object',None)
        if enabled and profile is not None:
            x,y=profile;table[:,index+2]=np.clip(np.interp(np.linspace(0,1,4097),x,y),0,1);params[14,index]=1
        if enabled and reference:
            params[14,index+2]=1;params[15+index,:3]=tuple(obj.matrix_world.translation) if obj else (0,0,0)
            if obj:references.append(obj)
    images={};image_cache={};size_gain=1.
    for kind,n in chosen.items():
        slot=8 if kind=='color' else 11
        typ=3 if n.source_type=='OBJECTCOLOR' else 2 if n.uv_source=='OBJECTUV' else 1
        params[slot]=(typ,WRAP[n.wrap_mode_u],WRAP[n.wrap_mode_v],n.filter_mode=='BILINEAR')
        angle=n.rotation*(-1 if n.uv_source=='OBJECTUV' else 1)
        params[slot+1]=(*n.tiling,*n.offset);params[slot+2]=(math.cos(angle),math.sin(angle),0,0)
        if n.source_type=='IMAGE':
            key=n.image.as_pointer()
            if key not in image_cache:image_cache[key]=texture_maps.pixels(n.image)
            images[kind]=image_cache[key]
            if kind=='size':
                rgba=images[kind];maximum=float(np.max((rgba[:,:,:3]@np.array((.2126,.7152,.0722)))*rgba[:,:,3]))
                size_gain=max(1.,1-brush.size_map_amount+brush.size_map_amount*maximum)
    params[3,2]*=size_gain
    return {'params':params,'curves':table,'maps':chosen,'images':images,'references':references,'needs_chain':needs_chain(brush)}


def mesh_maps(mesh,payload,chosen):
    surface={k:n for k,n in chosen.items() if object_uv.surface_source(n)}
    count=len(payload['endpoints'])
    if not surface:return None
    if np.any(payload['face_counts']>2):raise ValueError('GPU: 비매니폴드 모서리의 Object UV/Color 선택은 Native를 사용하세요')
    output=np.zeros((count,8,4),np.float32)
    nl=len(mesh.loops);edge_loops=np.full(len(mesh.edges),nl,np.int32);last=np.full(len(mesh.edges),-1,np.int32)
    ids=np.arange(nl,dtype=np.int32);np.minimum.at(edge_loops,payload['loop_edges'],ids);np.maximum.at(last,payload['loop_edges'],ids)
    starts=np.empty(len(mesh.polygons),np.int32);mesh.polygons.foreach_get('loop_start',starts)
    lengths=np.empty(len(mesh.polygons),np.int32);mesh.polygons.foreach_get('loop_total',lengths)
    next_loop=ids+1;next_loop[starts+lengths-1]=starts
    loop_vertices=np.empty(nl,np.int32);mesh.loops.foreach_get('vertex_index',loop_vertices)
    mask=payload['chosen_mask'];endpoints=payload['endpoints'];face_loops=(edge_loops[mask],last[mask])
    for kind,n in surface.items():
        base=0 if kind=='color' else 4
        if n.source_type=='OBJECTCOLOR':
            attr=object_uv.color_attribute(mesh,n)
            if attr is None or attr.domain not in {'POINT','CORNER'}:raise ValueError('GPU: 선택한 Color Attribute가 없거나 미지원 도메인입니다')
            rgba=np.empty((len(attr.data),4),np.float32);attr.data.foreach_get('color',rgba.ravel());rgba[:,3]=1.
        else:
            name=object_uv.layer_name(mesh,n)
            if name is None:raise ValueError('GPU: 선택한 Object UV 레이어가 없는 메시입니다')
            uv=np.empty((nl,2),np.float32);mesh.uv_layers[name].data.foreach_get('uv',uv.ravel())
        for face,l0 in enumerate(face_loops):
            l1=next_loop[l0];forward=loop_vertices[l0]==endpoints[:,0]
            la=np.where(forward,l0,l1);lb=np.where(forward,l1,l0)
            if n.source_type=='OBJECTCOLOR':
                output[:,base+face*2]=rgba[endpoints[:,0] if attr.domain=='POINT' else la]
                output[:,base+face*2+1]=rgba[endpoints[:,1] if attr.domain=='POINT' else lb]
            else:output[:,base+face,:2]=uv[la];output[:,base+face,2:]=uv[lb]
    return output


def texture_rgba(array):
    import gpu
    array=np.ascontiguousarray(array,dtype=np.float32)
    h,w=array.shape[:2]
    return gpu.types.GPUTexture((w,h),format='RGBA32F',data=gpu.types.Buffer('FLOAT',array.size,array.ravel()))


def upload(group):
    from . import gpu_chain
    style=group.get('style')
    if style is None:style=group['style']=snapshot(group['brush'])
    params=style['params'].copy();brush=group['brush']
    params[0]=(*brush.color,brush.alpha);params[1]=group['size_reduction'];params[2]=group['alpha_reduction']
    group['style_texture']=texture_rgba(params[None,:,:])
    group['curve_texture']=texture_rgba(style['curves'][None,:,:])
    group['coordinate_texture']=gpu_chain.texture(group.get('map_coordinates',np.zeros((1,4),np.float32)))
    cache={}
    for kind in ('color','size'):
        image=style['images'].get(kind)
        if image is None:image=np.ones((1,1,4),np.float32)
        key=id(image)
        if key not in cache:cache[key]=texture_rgba(image)
        group[kind+'_texture']=cache[key]
    group['chain_texture']=gpu_chain.texture(np.zeros((1,4),np.float32))


GLSL=r'''
vec4 styleValue(int i){return texelFetch(styleData,ivec2(i,0),0);}
vec4 readStyleTex(sampler2D tex,int index){int w=textureSize(tex,0).x;return texelFetch(tex,ivec2(index%w,index/w),0);}
float curveValue(float t,int channel){
 float p=clamp(t,0.0,1.0)*float(textureSize(curveData,0).x-1);int a=int(floor(p)),b=min(a+1,textureSize(curveData,0).x-1);
 return mix(texelFetch(curveData,ivec2(a,0),0)[channel],texelFetch(curveData,ivec2(b,0),0)[channel],p-float(a));
}
float wrapped(float x,int mode,out bool valid){
 valid=true;if(mode==0)return fract(x);if(mode==2)return 1.0-abs(mod(x,2.0)-1.0);
 if(mode==3)return clamp(abs(x),0.0,1.0);if(mode==4)valid=x>=0.0&&x<1.0;return clamp(x,0.0,1.0);
}
vec4 imageTap(sampler2D image,vec2 index,vec4 props){
 vec2 size=vec2(textureSize(image,0));bool vu,vv;
 float u=wrapped((index.x+.5)/size.x,int(props.y),vu),v=wrapped((index.y+.5)/size.y,int(props.z),vv);
 ivec2 pixel=min(ivec2(size)-1,ivec2(floor(vec2(u,v)*size)));
 return vu&&vv?texelFetch(image,pixel,0):vec4(0);
}
vec4 mapSample(sampler2D image,int slot,int dataBase,float u){
 vec4 props=styleValue(slot);int typ=int(props.x);if(typ==0)return vec4(1);
 int face=int(faceChoice),index=int(edgeIndex)*8+dataBase;
 if(typ==3)return mix(readStyleTex(coordinateData,index+face*2),readStyleTex(coordinateData,index+face*2+1),u);
 vec2 uv=(gl_FragCoord.xy-viewExtra.xy)/viewportSize;
 if(typ==2){vec4 ends=readStyleTex(coordinateData,index+face);uv=mix(ends.xy,ends.zw,u);}
 vec4 transform=styleValue(slot+1),rot=styleValue(slot+2);
 uv=vec2(uv.x*rot.x+uv.y*rot.y,-uv.x*rot.y+uv.y*rot.x)*transform.xy+transform.zw;
 vec2 p=uv*vec2(textureSize(image,0));if(props.w<.5)return imageTap(image,floor(p),props);
 p-=.5;vec2 base=floor(p),f=fract(p);
 return mix(mix(imageTap(image,base,props),imageTap(image,base+vec2(1,0),props),f.x),mix(imageTap(image,base+vec2(0,1),props),imageTap(image,base+vec2(1,1),props),f.x),f.y);
}
'''
