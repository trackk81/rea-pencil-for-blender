"""Cached mesh-only GPU depth pass for visible/hidden stroke classification."""
import numpy as np


def snapshot(depsgraph):
    vertices=[];triangles=[];offset=0
    for instance in depsgraph.object_instances:
        obj=instance.object
        if obj.type!='MESH' or not instance.show_self:continue
        if depsgraph.mode != 'RENDER' and not instance.is_instance and not obj.original.visible_get(view_layer=depsgraph.view_layer):continue
        mesh=obj.to_mesh()
        try:
            mesh.calc_loop_triangles();v=np.empty((len(mesh.vertices),3),np.float32);mesh.vertices.foreach_get('co',v.ravel())
            t=np.empty((len(mesh.loop_triangles),3),np.int32);mesh.loop_triangles.foreach_get('vertices',t.ravel())
            m=np.asarray(instance.matrix_world,np.float32);vertices.append(v@m[:3,:3].T+m[:3,3]);triangles.append(t+offset);offset+=len(v)
        finally:obj.to_mesh_clear()
    return {'vertices':np.concatenate(vertices) if vertices else np.zeros((0,3),np.float32),
            'triangles':np.concatenate(triangles) if triangles else np.zeros((0,3),np.int32),'areas':{}}


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
