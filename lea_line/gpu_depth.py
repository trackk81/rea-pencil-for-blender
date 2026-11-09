"""Cached mesh-only GPU depth pass for visible/hidden stroke classification."""
import numpy as np


# Shared by raster coverage and the optional depth-split chain length pass.
CENTER_DEPTH = r'''
float centerDepth(vec2 center) {
    // Interpolate at the actual centerline. Nearest sampling can classify a
    // stroke as behind its own sloping surface.
    vec2 p=center-vec2(.5), f=fract(p);ivec2 a=ivec2(floor(p));
    ivec2 last=textureSize(sceneDepth,0)-1;
    float d00=texelFetch(sceneDepth,clamp(a,ivec2(0),last),0).r;
    float d10=texelFetch(sceneDepth,clamp(a+ivec2(1,0),ivec2(0),last),0).r;
    float d01=texelFetch(sceneDepth,clamp(a+ivec2(0,1),ivec2(0),last),0).r;
    float d11=texelFetch(sceneDepth,clamp(a+ivec2(1,1),ivec2(0),last),0).r;
    return mix(mix(d00,d10,f.x),mix(d01,d11,f.x),f.y);
}
'''

MARKED_DEPTH = r'''
vec3 incidentDepthPlane(mat4 projection,vec2 size,vec4 p,vec4 q,vec3 span,vec4 face) {
    vec2 delta=(q.xy/q.w-p.xy/p.w)*size*.5;
    vec4 mid=(p+q)*.5;
    vec4 stepClip=projection*vec4(cross(face.xyz,span)*face.w,0.0);
    vec3 stepValue=(stepClip.xyz-mid.xyz/mid.w*stepClip.w)/mid.w*.5;
    vec2 projected=stepValue.xy*size;
    float determinant=delta.x*projected.y-delta.y*projected.x;
    if (abs(determinant)<=1e-8) return vec3(0);
    float depthDelta=(q.z/q.w-p.z/p.w)*.5;
    return vec3((depthDelta*projected.y-delta.y*stepValue.z)/determinant,
                (delta.x*stepValue.z-depthDelta*projected.x)/determinant,sign(determinant));
}
vec4 markedDepthPlanes(mat4 projection,vec2 size,vec4 p,vec4 q,vec3 span,vec4 first,vec4 second) {
    vec3 a=incidentDepthPlane(projection,size,p,q,span,first);
    vec3 b=incidentDepthPlane(projection,size,p,q,span,second);
    if (a.z*b.z>=0.0) return vec4(0);
    return a.z<0.0?vec4(a.xy,b.xy):vec4(b.xy,a.xy);
}
float markedDepthTap(ivec2 pixel,vec2 center,vec2 delta,float depth,vec4 planes) {
    pixel=clamp(pixel,ivec2(0),textureSize(sceneDepth,0)-1);
    vec2 offset=vec2(pixel)+.5-center;
    vec2 gradient=delta.x*offset.y-delta.y*offset.x<0.0?planes.xy:planes.zw;
    float observed=texelFetch(sceneDepth,pixel,0).r;
    float expected=depth+dot(offset,gradient);
    // Only reproject a texel that matches the incident face. A nearer
    // surface whose depth differs from that plane still occludes the mark.
    return abs(observed-expected)<=.000002?depth:observed;
}
float markedCenterDepth(vec2 center,vec2 delta,float depth,vec4 planes) {
    if (all(equal(planes,vec4(0)))) return centerDepth(center);
    vec2 p=center-.5, f=fract(p);
    ivec2 a=ivec2(floor(p));
    float d00=markedDepthTap(a,center,delta,depth,planes);
    float d10=markedDepthTap(a+ivec2(1,0),center,delta,depth,planes);
    float d01=markedDepthTap(a+ivec2(0,1),center,delta,depth,planes);
    float d11=markedDepthTap(a+ivec2(1,1),center,delta,depth,planes);
    return mix(mix(d00,d10,f.x),mix(d01,d11,f.x),f.y);
}
'''

SPAN_LENGTH = r'''
bool spanSample(vec4 points,vec2 depths,float t,bool hidden,vec4 planes,bool outsideVisible) {
    vec2 center=mix(points.xy,points.zw,t);
    if (any(lessThan(center,vec2(0))) || any(greaterThanEqual(center,vec2(textureSize(sceneDepth,0)))))
        return outsideVisible && !hidden;
    // Snap along the dominant raster axis, while interpolating across the
    // stroke. This measures a depth cut at its texel boundary instead of
    // spreading it along the stroke by bilinear filtering.
    vec2 delta=points.zw-points.xy;
    bool xAxis=abs(delta.x)>=abs(delta.y);
    float axis=xAxis?center.x:center.y;
    float stepValue=xAxis?delta.x:delta.y;
    float sampleT=clamp(t+(floor(axis)+.5-axis)/stepValue,0.0,1.0);
    float depth=mix(depths.x,depths.y,sampleT);
    return (depth>markedCenterDepth(mix(points.xy,points.zw,sampleT),delta,depth,planes)+.000002)==hidden;
}
bool spanSample(vec4 points,vec2 depths,float t,bool hidden,vec4 planes) {
    return spanSample(points,depths,t,hidden,planes,false);
}
bool spanSample(vec4 points,vec2 depths,float t,bool hidden) {
    return spanSample(points,depths,t,hidden,vec4(0));
}
float spanLength(vec4 points,vec2 depths,float lengthValue,float start,float direction,float limit,bool hidden,vec4 planes,bool outsideVisible) {
    float available=direction>0.0?lengthValue-start:start;
    float stop=min(available,limit);
    if (!spanSample(points,depths,start/lengthValue,hidden,planes,outsideVisible)) return 0.0;
    float first=0.0,last=stop;
    if (outsideVisible && !hidden) {
        // Preserve phase through offscreen portions, but only march where
        // the depth buffer could change visibility.
        vec2 origin=mix(points.xy,points.zw,start/lengthValue);
        vec2 stepValue=(points.zw-points.xy)*(direction/lengthValue);
        vec2 size=vec2(textureSize(sceneDepth,0));
        for (int axis=0;axis<2;axis++) {
            if (abs(stepValue[axis])<1e-8) {
                if (origin[axis]<0.0 || origin[axis]>=size[axis]) return stop;
            } else {
                float a=-origin[axis]/stepValue[axis];
                float b=(size[axis]-origin[axis])/stepValue[axis];
                first=max(first,floor(min(a,b)));
                last=min(last,ceil(max(a,b)));
            }
        }
    }
    // Step at pixel intervals, then refine the first transition. All queries
    // stop once enough length has been found to satisfy the threshold.
    for (float stepIndex=1.0;stepIndex<last-first+1.0;stepIndex+=1.0) {
        float offset=first+stepIndex;
        float hi=min(offset,last);
        if (!spanSample(points,depths,(start+direction*hi)/lengthValue,hidden,planes,outsideVisible)) {
            float lo=offset-1.0;
            // Subpixel precision keeps the same cap coverage when a tile
            // changes where the fragment's search begins.
            for (int i=0;i<10;i++) {
                float middle=(lo+hi)*.5;
                if (spanSample(points,depths,(start+direction*middle)/lengthValue,hidden,planes,outsideVisible)) lo=middle;
                else hi=middle;
            }
            return (lo+hi)*.5;
        }
    }
    return stop;
}
float spanLength(vec4 points,vec2 depths,float lengthValue,float start,float direction,float limit,bool hidden,vec4 planes) {
    return spanLength(points,depths,lengthValue,start,direction,limit,hidden,planes,false);
}
float spanLength(vec4 points,vec2 depths,float lengthValue,float start,float direction,float limit,bool hidden) {
    return spanLength(points,depths,lengthValue,start,direction,limit,hidden,vec4(0));
}
'''


def mesh_triangles(mesh):
    mesh.calc_loop_triangles()
    triangles = np.empty((len(mesh.loop_triangles), 3), np.int32)
    mesh.loop_triangles.foreach_get('vertices', triangles.ravel())
    return triangles


def merge_members(collection):
    """Resolve marked collections and instance sources in child-first order."""
    members = {}
    visited = set()

    def visit(item):
        if item in visited:
            return
        visited.add(item)
        for child in item.children:
            visit(child)
        for obj in item.objects:
            if obj.type == 'EMPTY' and obj.instance_type == 'COLLECTION' and obj.instance_collection:
                visit(obj.instance_collection)
        if getattr(item, 'lea_line_merge_group', False):
            for obj in item.all_objects:
                if obj.type == 'MESH':
                    members.setdefault(obj.original, item)

    visit(collection)
    return members


def snapshot(depsgraph, members=None):
    vertices=[];triangles=[];offset=0
    parts = {}
    for instance in depsgraph.object_instances:
        obj=instance.object
        if obj.type!='MESH' or not instance.show_self:continue
        if depsgraph.mode != 'RENDER' and not instance.is_instance and not obj.original.visible_get(view_layer=depsgraph.view_layer):continue
        mesh=obj.to_mesh()
        try:
            v=np.empty((len(mesh.vertices),3),np.float32);mesh.vertices.foreach_get('co',v.ravel())
            t=mesh_triangles(mesh)
            m=np.asarray(instance.matrix_world,np.float32)
            world = v@m[:3,:3].T+m[:3,3]
            vertices.append(world);triangles.append(t+offset);offset+=len(v)
            # Group membership belongs to the source object. Every evaluated
            # occurrence contributes its own world transform, including repeats.
            group = members.get(obj.original) if members else None
            if group is not None:
                parts.setdefault(group, []).append((world, t))
        finally:obj.to_mesh_clear()
    merge_depths = {}
    for group, geometry in parts.items():
        points, indices = zip(*geometry)
        offsets = np.cumsum([0] + [len(array) for array in points[:-1]])
        merge_depths[group] = {'vertices': np.concatenate(points),
                               'triangles': np.concatenate([array + start for array, start in zip(indices, offsets)])}
    return {'vertices':np.concatenate(vertices) if vertices else np.zeros((0,3),np.float32),
            'triangles':np.concatenate(triangles) if triangles else np.zeros((0,3),np.int32),
            'areas':{}, 'merge_depths': merge_depths}


def draw(data,view,size,area_id):
    import gpu
    from gpu_extras.batch import batch_for_shader
    if 'shader' not in data:
        data['shader']=gpu.shader.from_builtin('UNIFORM_COLOR')
        data['batch']=batch_for_shader(data['shader'],'TRIS',{'pos':data['vertices']},indices=data['triangles']) if len(data['triangles']) else None
    area=data['areas'].get(area_id)
    if area is None or area['size']!=size:
        tex=gpu.types.GPUTexture(size,format='DEPTH_COMPONENT32F')
        area={'size':size,'texture':tex,'framebuffer':gpu.types.GPUFrameBuffer(depth_slot=tex)};data['areas'][area_id]=area
    viewport=gpu.state.viewport_get();scissor=gpu.state.scissor_get()
    try:
        with area['framebuffer'].bind():
            gpu.state.viewport_set(0,0,*size);gpu.state.scissor_set(0,0,*size)
            gpu.state.depth_test_set('LESS_EQUAL');gpu.state.depth_mask_set(True);gpu.state.blend_set('NONE')
            area['framebuffer'].clear(depth=1.)
            if data['batch']:
                with gpu.matrix.push_pop():
                    gpu.matrix.load_matrix(view.view_matrix);gpu.matrix.load_projection_matrix(view.window_matrix)
                    data['shader'].bind();data['shader'].uniform_float('color',(1,1,1,1));data['batch'].draw(data['shader'])
    finally:gpu.state.viewport_set(*viewport);gpu.state.scissor_set(*scissor)
    return area['texture']
