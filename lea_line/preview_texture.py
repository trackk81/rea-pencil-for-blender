"""Optional native field evaluation of screen maps after camera/stroke effects."""
import json
import bpy
from . import texture_maps as tex, preview_camera


def owned(m):return m.type=='NODES' and m.node_group and m.node_group.get(tex.TAG)==1


def cleanup(obj):
    for m in list(obj.modifiers):
        if owned(m):
            g=m.node_group;obj.modifiers.remove(m)
            if g.users==0:bpy.data.node_groups.remove(g)


def build(scene,brush,chosen,line_set=None,stamp_pattern=(1., 0.)):
    g=bpy.data.node_groups.new('.rea screen texture maps','GeometryNodeTree');g[tex.TAG]=1
    try:
        g.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
        g.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
        links=g.links
        def node(kind):return g.nodes.new(kind)
        def feed(value,socket):
            if isinstance(value,bpy.types.NodeSocket):links.new(value,socket)
            else:socket.default_value=value
        def math(op,a,b=None):
            n=node('ShaderNodeMath');n.operation=op;feed(a,n.inputs[0])
            if b is not None:feed(b,n.inputs[1])
            return n.outputs[0]
        def vector(x,y,z=0):
            n=node('ShaderNodeCombineXYZ')
            for v,s in zip((x,y,z),n.inputs):feed(v,s)
            return n.outputs[0]
        def color_mix(a,b,t):
            n=node('ShaderNodeMixRGB');feed(t,n.inputs[0]);feed(a,n.inputs[1]);feed(b,n.inputs[2]);return n.outputs[0]
        from . import preview_stroke, stamp_brush
        geometry,fields=preview_stroke._project(g,node('NodeGroupInput').outputs[0],scene)
        if 'distortion' not in chosen:
            spacing, jitter = stamp_pattern
            geometry=preview_stroke._resample(g,geometry,'Texture Pixel Samples',spacing)
            if jitter:
                geometry=stamp_brush.jitter_stamps(g, geometry, jitter)
        geometry=preview_stroke._unproject(g,geometry,fields)
        camera,perspective,factor,_=preview_camera.projection(scene)
        g.nodes['Projection Pixel Unit'].outputs[0].default_value=factor
        g.nodes['Projection Pixel Aspect'].outputs[0].default_value=scene.render.pixel_aspect_x/scene.render.pixel_aspect_y
        for name in ('Restore Depth','Restore Pixel Size'):g.nodes[name].inputs['Switch'].default_value=perspective
        rel=node('ShaderNodeVectorMath');rel.operation='SUBTRACT'
        links.new(fields['position'],rel.inputs[0]);links.new(fields['camera_location'],rel.inputs[1])
        frame=camera.data.view_frame(scene=scene)
        divisor=abs(frame[0].z) if perspective else 1
        coords=[]
        for axis,basis in ((0,'right'),(1,'up')):
            dot=node('ShaderNodeVectorMath');dot.operation='DOT_PRODUCT'
            links.new(rel.outputs[0],dot.inputs[0]);links.new(fields[basis],dot.inputs[1])
            p=math('DIVIDE',dot.outputs['Value'],fields['depth']) if perspective else dot.outputs['Value']
            lo=min(v[axis] for v in frame)/divisor;hi=max(v[axis] for v in frame)/divisor
            coords.append(math('DIVIDE',math('SUBTRACT',p,lo),hi-lo))
        from . import object_uv
        object_coords=object_uv.build_fields(g,scene,line_set,chosen,fields) if any(object_uv.surface_source(n) for n in chosen.values()) else {}
        def sample(n,kind):
            if n.source_type=='OBJECTCOLOR':return object_coords[kind],1.
            import math as m
            x,y=object_coords[kind] if n.uv_source=='OBJECTUV' else coords
            angle=-n.rotation if n.uv_source=='OBJECTUV' else n.rotation
            u=math('ADD',math('MULTIPLY',math('ADD',math('MULTIPLY',x,m.cos(angle)),math('MULTIPLY',y,m.sin(angle))),n.tiling[0]),n.offset[0])
            v=math('ADD',math('MULTIPLY',math('SUBTRACT',math('MULTIPLY',y,m.cos(angle)),math('MULTIPLY',x,m.sin(angle))),n.tiling[1]),n.offset[1])
            w,h=n.image.size
            px=math('MULTIPLY',u,w);py=math('MULTIPLY',v,h)
            if n.filter_mode=='BILINEAR':px=math('SUBTRACT',px,.5);py=math('SUBTRACT',py,.5)
            ix=math('FLOOR',px);iy=math('FLOOR',py)
            def wrap(value,mode):
                mask=1.
                if mode=='REPEAT':return math('FRACT',value),mask
                if mode=='MIRROR':return math('PINGPONG',value,1.),mask
                if mode=='MIRROR_ONCE':value=math('ABSOLUTE',value)
                if mode=='CLIP':mask=math('MULTIPLY',math('GREATER_THAN',value,0.),math('LESS_THAN',value,1.))
                return math('MINIMUM',math('MAXIMUM',value,0.),1.),mask
            def tap(dx,dy):
                u,um=wrap(math('DIVIDE',math('ADD',ix,dx+.5),w),n.wrap_mode_u)
                v,vm=wrap(math('DIVIDE',math('ADD',iy,dy+.5),h),n.wrap_mode_v)
                image=node('GeometryNodeImageTexture');image.interpolation='Closest';image.extension='EXTEND'
                image.inputs['Image'].default_value=n.image;links.new(vector(u,v),image.inputs['Vector'])
                # GeometryNodeImageTexture exposes associated RGB; the CPU
                # image buffer uses straight RGBA. Normalize each tap before
                # filtering so translucent images share the same convention.
                straight=node('ShaderNodeVectorMath');straight.operation='SCALE'
                links.new(image.outputs['Color'],straight.inputs[0])
                links.new(math('DIVIDE',1.,image.outputs['Alpha']),straight.inputs['Scale'])
                mask=math('MULTIPLY',um,vm)
                multiply=node('ShaderNodeMixRGB');multiply.blend_type='MULTIPLY';multiply.inputs[0].default_value=1.
                links.new(straight.outputs['Vector'],multiply.inputs[1]);feed(mask,multiply.inputs[2])
                return multiply.outputs[0],math('MULTIPLY',image.outputs['Alpha'],mask)
            if n.filter_mode=='POINT':return tap(0,0)
            taps=[tap(dx,dy) for dx,dy in ((0,0),(1,0),(0,1),(1,1))]
            tx=math('FRACT',px);ty=math('FRACT',py)
            rgb=color_mix(color_mix(taps[0][0],taps[1][0],tx),color_mix(taps[2][0],taps[3][0],tx),ty)
            def mix(a,b,t):return math('ADD',math('MULTIPLY',a,math('SUBTRACT',1.,t)),math('MULTIPLY',b,t))
            alpha=mix(mix(taps[0][1],taps[1][1],tx),mix(taps[2][1],taps[3][1],tx),ty)
            return rgb,alpha
        for kind,n in chosen.items():
            rgb,alpha=sample(n,kind)
            if kind=='distortion':
                from . import distortion_map,backend
                geometry=distortion_map.live(g,geometry,rgb,brush.distortion_map_amount*backend.width_scale(scene,line_set),fields,scene)
                geometry=distortion_map.resample(g,geometry,fields['pixel_size'])
            elif kind in {'color','functions'}:
                if kind=='functions':
                    separate=node('FunctionNodeSeparateColor');links.new(rgb,separate.inputs[0]);alpha=separate.outputs['Alpha']
                factor=math('MULTIPLY',alpha,brush.color_map_opacity if kind=='color' else 1.)
                base=(*brush.color,1.)
                if kind=='functions':
                    previous=node('GeometryNodeInputNamedAttribute');previous.data_type='FLOAT_COLOR';previous.inputs['Name'].default_value='vertex_color'
                    existing=node('GeometryNodeSwitch');existing.input_type='RGBA'
                    links.new(previous.outputs['Exists'],existing.inputs['Switch']);existing.inputs['False'].default_value=base
                    links.new(previous.outputs['Attribute'],existing.inputs['True']);base=existing.outputs[0]
                mixed=color_mix(base,rgb,factor)
                store=node('GeometryNodeStoreNamedAttribute');store.data_type='FLOAT_COLOR';store.domain='POINT'
                store.inputs['Name'].default_value='vertex_color'
                links.new(geometry,store.inputs['Geometry']);links.new(mixed,store.inputs['Value']);geometry=store.outputs[0]
            else:
                separate=node('FunctionNodeSeparateColor');separate.mode='RGB';links.new(rgb,separate.inputs[0])
                luma=math('ADD',math('ADD',math('MULTIPLY',separate.outputs['Red'],.2126),math('MULTIPLY',separate.outputs['Green'],.7152)),math('MULTIPLY',separate.outputs['Blue'],.0722))
                factor=math('ADD',1.-brush.size_map_amount,math('MULTIPLY',math('MULTIPLY',luma,alpha),brush.size_map_amount))
                radius=node('GeometryNodeInputRadius').outputs[0]
                set_radius=node('GeometryNodeSetCurveRadius')
                links.new(geometry,set_radius.inputs['Curve']);links.new(math('MULTIPLY',radius,factor),set_radius.inputs['Radius']);geometry=set_radius.outputs[0]
                if n.source_type=='OBJECTCOLOR':
                    # GP can rasterize faint pixels for zero-radius curves.
                    # Domain adaptation averages nonnegative point radii per
                    # curve, preserving any curve with a positive-width part.
                    remove=node('GeometryNodeDeleteGeometry');remove.domain='CURVE'
                    links.new(geometry,remove.inputs['Geometry'])
                    links.new(math('LESS_THAN',node('GeometryNodeInputRadius').outputs[0],1e-20),remove.inputs['Selection'])
                    geometry=remove.outputs[0]
        from . import stroke_variation
        from . import hidden_mask
        geometry=hidden_mask.live(g,geometry,brush,fields,scene)
        geometry=stroke_variation.live(g,geometry,brush)
        geometry=stamp_brush.live_geometry(g,geometry,brush)
        links.new(geometry,node('NodeGroupOutput').inputs[0])
        return g
    except Exception:
        bpy.data.node_groups.remove(g);raise


def sync(obj,brush,scene,line_set=None):
    chosen=tex.maps(brush)
    from . import stroke_variation
    variation=stroke_variation.snapshot(brush)
    from . import stamp_brush
    stamps=stamp_brush.spec(brush)
    if not chosen and variation is None and stamps is None and not getattr(brush,'hidden_mask_objects',()):cleanup(obj);return
    from . import backend
    stamp_map = stamp_brush.detail_map(brush, 'brush') if stamps else None
    pattern = stamp_brush.preview_pattern(stamp_map, brush.thickness * backend.width_scale(scene, line_set)) if stamp_map else (1., 0.)
    camera, _, _, _ = preview_camera.projection(scene)
    frame=camera.data.view_frame(scene=scene)
    from . import object_uv
    schema=object_uv.schema_key(scene,line_set,chosen)
    key=json.dumps((tuple(tuple(v) for v in frame),preview_camera.signature(scene),tuple(brush.color),schema,
        brush.color_map_opacity,brush.size_map_amount,variation,stamps,pattern,getattr(brush,'distortion_map_amount',0),tuple(o.as_pointer() for o in getattr(brush,'hidden_mask_objects',())),
        [(kind,n.source_type,n.object_color_selection_mode,n.object_color_index,n.object_color_name,n.image.as_pointer() if n.image else None,tuple(n.image.size) if n.image else None,n.wrap_mode_u,n.wrap_mode_v,n.filter_mode,
          tuple(n.tiling),tuple(n.offset),n.rotation,n.uv_source,n.uv_selection_mode,n.uv_index,n.uv_name,
          sorted(getattr(n,'rule_colors',{}).items())) for kind,n in chosen.items()]))
    modifier=next((m for m in obj.modifiers if owned(m)),None)
    if modifier and modifier.node_group.get('signature')==key:
        preview_camera.sync_fields(modifier.node_group,scene)
        index=list(obj.modifiers).index(modifier)
        if index!=len(obj.modifiers)-1:obj.modifiers.move(index,len(obj.modifiers)-1)
        return
    graph=build(scene,brush,chosen,line_set,pattern);graph['signature']=key
    if any(object_uv.surface_source(n) for n in chosen.values()):object_uv.request_prepare(scene)
    old=modifier.node_group if modifier else None
    if modifier is None:modifier=obj.modifiers.new('.rea screen texture maps','NODES')
    try:modifier.node_group=graph;modifier.show_render=False;modifier.show_viewport=True
    except Exception:
        if old:modifier.node_group=old
        else:obj.modifiers.remove(modifier)
        if graph.users==0:bpy.data.node_groups.remove(graph)
        raise
    if old and old.users==0:bpy.data.node_groups.remove(old)
    # Always evaluate after the width and stroke shape modifiers.
    index=list(obj.modifiers).index(modifier)
    if index!=len(obj.modifiers)-1:obj.modifiers.move(index,len(obj.modifiers)-1)
