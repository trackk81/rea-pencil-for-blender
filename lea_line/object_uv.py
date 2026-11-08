"""Read-only Object UV sources shared by Freestyle and native live fields.

Nearest visible-facing surface is an independent reconstruction. Seams and
intersections are verified by bounded fixtures, not claimed universally equal.
"""
import bpy
import numpy as np
import threading
import time
from bpy.app.handlers import persistent
from mathutils.bvhtree import BVHTree

PREFIX='.rea object uv '


def surface_source(node):
    return getattr(node,'source_type',None)=='OBJECTCOLOR' or (getattr(node,'source_type','IMAGE')=='IMAGE' and getattr(node,'uv_source',None)=='OBJECTUV')


def color_source(node):return getattr(node,'source_type',None)=='OBJECTCOLOR'


def color_attribute(mesh,node):
    attrs=mesh.color_attributes
    if node.object_color_selection_mode=='NAME':return attrs.get(node.object_color_name)
    index=node.object_color_index
    return attrs[index] if index<len(attrs) else None



def validate_visibility(line_set,common):
    if not line_set.hidden:return
    from . import routing,texture_maps
    for job in routing.jobs(line_set,common,'HIDDEN'):
        if any(surface_source(n) for n in texture_maps.maps(job['brush']).values()):
            raise ValueError('Surface-map hidden-line face selection is not verified; use a separate Screen hidden brush or disable Hidden Lines')


def uses(scene):
    tree=getattr(getattr(scene,'lea_line',None),'tree',None)
    if tree is None:return False
    for brush in tree.nodes:
        if brush.bl_idname=='LEABrushDetailNode' and brush.enabled:
            from . import stamp_brush
            if stamp_brush.active(brush) or (brush.distortion_enabled and brush.distortion_map_on):return True
            from . import stroke_variation
            if any(s.is_linked for s in brush.outputs) and stroke_variation.snapshot(brush):return True
        if brush.bl_idname == 'LEALineSetNode':
            if brush.is_mask_hidden_lines:return True
            if brush.use_material_functions:return True
            from .independent_reduction import configured
            if configured(brush, 'VISIBLE') or configured(brush, 'HIDDEN'): return True
        if brush.bl_idname!='LEABrushNode':continue
        if getattr(brush,'linear_alpha',False):return True
        # Read flags here, never evaluate curve widgets just to detect use.
        # Draft brushes can contain unrendered/animated data; _specifications
        # validates only the connected rendering plan during preparation.
        reduction_socket=brush.inputs.get('Distance Reduction')
        reduction=reduction_socket.links[0].from_node if reduction_socket and reduction_socket.is_linked else None
        if reduction and reduction.bl_idname=='LEADistanceReductionNode':
            if (reduction.enabled and (reduction.reduce_thickness or reduction.reduce_opacity)
                    and (reduction.refer_object_on or getattr(reduction,'curve_enabled',False) or reduction.reduce_opacity)):return True
        elif getattr(brush,'distance_alpha',False):return True
        for kind in ('color','size'):
            if not getattr(brush,kind+'_map_on',False):continue
            if getattr(brush,'color_map_opacity' if kind=='color' else 'size_map_amount',0)<=0:continue
            socket=brush.inputs.get(kind.title()+' Map')
            if socket and socket.is_linked and surface_source(socket.links[0].from_node):return True
    return False


def qualified(block):return ((block.library.filepath if block.library else ''),block.name_full)


def depsgraph(scene):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Object UV evaluation requires the main thread')
    with bpy.context.temp_override(scene=scene,view_layer=scene.view_layers[0]):
        return bpy.context.evaluated_depsgraph_get()


def plans(scene,line_set=None,*,final=False):
    """Source objects and allowed material slots; never change authored data."""
    if len(scene.view_layers)!=1:
        raise ValueError('Surface maps currently require one view layer')
    if not scene.camera or scene.camera.data.type not in {'ORTHO','PERSP'}:
        raise ValueError('Surface maps require a perspective or orthographic scene camera')
    if scene.render.use_simplify or scene.render.use_motion_blur:
        raise ValueError('Surface maps do not yet support render simplification or motion blur')
    explicit={qualified(t.object) for t in line_set.objects if t.object} if line_set else set()
    materials={qualified(t.material) for t in line_set.materials if t.material} if line_set else set()
    scope={qualified(o) for o in line_set.source_collection.all_objects} if line_set and line_set.source_collection else None
    targeted=bool(explicit or materials)
    render_objects=set()
    def visible(branch):
        if branch.exclude or branch.collection.hide_render:return
        render_objects.update(qualified(o) for o in branch.collection.objects)
        for child in branch.children:visible(child)
    visible(scene.view_layers[0].layer_collection)
    result=[]
    for obj in scene.objects:
        if not obj.get('rea_pencil_preview') and not obj.hide_render and obj.instance_type!='NONE':
            raise ValueError('Surface maps do not yet support object/collection instances')
        if obj.type!='MESH' or obj.get('rea_pencil_preview') or obj.hide_render:continue
        if obj.lineart.usage in {'EXCLUDE','OCCLUSION_ONLY'}:continue
        if final and qualified(obj) not in render_objects:continue
        if not final and (obj.hide_viewport or obj.hide_get(view_layer=scene.view_layers[0])
                          or not obj.visible_get(view_layer=scene.view_layers[0])):continue
        key=qualified(obj)
        if scope is not None and key not in scope:continue
        slots=[i for i,s in enumerate(obj.material_slots) if s.material and qualified(s.material) in materials]
        if targeted and key not in explicit and not slots:continue
        if line_set and not targeted and line_set.target_mode=='LISTED' and not line_set.source_collection:continue
        if obj.instance_type!='NONE' or obj.particle_systems:
            raise ValueError('Surface maps do not yet support object/particle instances')
        for m in obj.modifiers:
            if m.show_viewport!=m.show_render or (m.type=='SUBSURF' and m.show_render and m.levels!=m.render_levels):
                raise ValueError('Surface maps require matching viewport/render modifier evaluation')
            if m.type=='NODES' and (m.show_viewport or m.show_render):
                raise ValueError('Surface maps do not yet support Geometry Nodes instances')
        result.append((obj,None if not targeted or key in explicit else tuple(slots)))
    return sorted(result,key=lambda pair:qualified(pair[0]))


def layer_name(mesh,node):
    if color_source(node):
        attr=color_attribute(mesh,node)
        return attr.name if attr else None
    if node.uv_selection_mode=='NAME':
        return node.uv_name if mesh.uv_layers.get(node.uv_name) else None
    return mesh.uv_layers[node.uv_index].name if node.uv_index<len(mesh.uv_layers) else None


class Sampler:
    """One immutable world-space triangle/UV snapshot per final line job."""
    def __init__(self,scene,line_set,chosen):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('Object UV mesh snapshots must be captured on the Blender main thread')
        self.values={kind:[] for kind,n in chosen.items() if surface_source(n)}
        self.channels={kind:4 if color_source(n) else 2 for kind,n in chosen.items() if surface_source(n)}
        self.fallback_alpha={kind:0. if getattr(n,'rule_mode',False) else 1. for kind,n in chosen.items() if surface_source(n)}
        self.vertices=[];self.bvh=None
        camera=scene.camera;cam=np.array(camera.matrix_world.translation)
        forward=np.array(camera.matrix_world.to_quaternion()@__import__('mathutils').Vector((0,0,-1)))
        deps=depsgraph(scene)
        for obj,slots in plans(scene,line_set,final=True):
            evaluated=obj.evaluated_get(deps);mesh=evaluated.data;mesh.calc_loop_triangles()
            if not mesh.loop_triangles:continue
            positions=np.empty((len(mesh.vertices),3),np.float32);mesh.vertices.foreach_get('co',positions.ravel())
            matrix=np.array(evaluated.matrix_world,dtype=np.float64)
            positions=positions@matrix[:3,:3].T+matrix[:3,3]
            indices=np.empty((len(mesh.loop_triangles),3),np.int32);mesh.loop_triangles.foreach_get('vertices',indices.ravel())
            loops=np.empty_like(indices);mesh.loop_triangles.foreach_get('loops',loops.ravel())
            triangles=positions[indices]
            keep=np.ones(len(triangles),bool)
            if slots is not None:
                faces=np.empty(len(triangles),np.int32);mesh.loop_triangles.foreach_get('polygon_index',faces)
                mat=np.empty(len(mesh.polygons),np.int32);mesh.polygons.foreach_get('material_index',mat)
                keep=np.isin(mat[faces],slots)
            normal=np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])
            direction=cam-triangles.mean(1) if camera.data.type=='PERSP' else -forward
            front=(normal*direction).sum(1)>0
            if np.any(front&keep):keep &= front
            if not keep.any():continue
            self.vertices.append(triangles[keep])
            for kind in self.values:
                name=layer_name(mesh,chosen[kind])
                if getattr(chosen[kind],'rule_mode',False):
                    from . import material_functions
                    faces=np.empty(len(mesh.loop_triangles),np.int32);mesh.loop_triangles.foreach_get('polygon_index',faces)
                    colors=material_functions.values(mesh,obj,chosen[kind])[faces[keep]]
                    self.values[kind].append(np.repeat(colors[:,None,:],3,axis=1))
                elif color_source(chosen[kind]):
                    attr=color_attribute(mesh,chosen[kind])
                    rgba=np.zeros((int(keep.sum()),3,4),np.float32)
                    if attr:
                        data=np.empty((len(attr.data),4),np.float32);attr.data.foreach_get('color',data.ravel())
                        rgba=data[(indices if attr.domain=='POINT' else loops)[keep]].copy()
                    # Native FLOAT/BYTE POINT/CORNER probes ignore attribute alpha.
                    rgba[:,:,3]=1.;self.values[kind].append(rgba)
                elif name:
                    uv=np.empty((len(mesh.loops),2),np.float32);mesh.uv_layers[name].data.foreach_get('uv',uv.ravel())
                    self.values[kind].append(uv[loops[keep]])
                else:self.values[kind].append(np.zeros((int(keep.sum()),3,2),np.float32))
        if self.vertices:
            self.vertices=np.concatenate(self.vertices)
            self.values={k:np.concatenate(v) for k,v in self.values.items()}
            self.vertices.setflags(write=False)
            for value in self.values.values():value.setflags(write=False)
            vertices=self.vertices.reshape(-1,3)
            self.bvh=BVHTree.FromPolygons(vertices.tolist(),np.arange(len(vertices)).reshape(-1,3).tolist(),all_triangles=True)

    def sample(self,points,kind):
        result=np.zeros((len(points),self.channels[kind]),np.float64)
        if self.channels[kind]==4:result[:,3]=getattr(self,'fallback_alpha',{}).get(kind,1.)
        if self.bvh is None:return result
        if not len(points):return result
        # Bound query/temporary arrays for production strokes with many samples.
        for start in range(0,len(points),8192):
            self._sample_into(points[start:start+8192],kind,result[start:start+8192])
        return result

    def _sample_into(self,points,kind,result):
        hits=[self.bvh.find_nearest(point) for point in points]
        rows=np.array([row for row,hit in enumerate(hits) if hit[2] is not None],dtype=np.intp)
        if not len(rows):return
        indices=np.array([hits[row][2] for row in rows],dtype=np.intp)
        nearest=np.array([hits[row][0] for row in rows],dtype=np.float64)
        triangles=self.vertices[indices];a=triangles[:,0];v0=triangles[:,1]-a;v1=triangles[:,2]-a;v2=nearest-a
        dot=lambda x,y:np.einsum('ij,ij->i',x,y)
        d00=dot(v0,v0);d01=dot(v0,v1);d11=dot(v1,v1);den=d00*d11-d01*d01
        valid=np.abs(den)>=1e-20
        if valid.any():
            v=(d11[valid]*dot(v2,v0)[valid]-d01[valid]*dot(v2,v1)[valid])/den[valid]
            w=(d00[valid]*dot(v2,v1)[valid]-d01[valid]*dot(v2,v0)[valid])/den[valid]
            weights=np.column_stack((1-v-w,v,w))
            result[rows[valid]]=np.einsum('ij,ijk->ik',weights,self.values[kind][indices[valid]])


def build_fields(g,scene,line_set,chosen,fields):
    """Live Object Info / nearest-surface fields depend directly on source UVs."""
    from . import preview_material
    preview_material.subscribe(scene)
    links=g.links
    def math(op,a,b):
        n=g.nodes.new('ShaderNodeMath');n.operation=op
        for v,s in zip((a,b),n.inputs):
            if isinstance(v,bpy.types.NodeSocket):links.new(v,s)
            else:s.default_value=v
        return n.outputs[0]
    def vector(op,a,b):
        n=g.nodes.new('ShaderNodeVectorMath');n.operation=op
        for v,s in zip((a,b),n.inputs):links.new(v,s)
        return n.outputs['Value'] if op=='DOT_PRODUCT' else n.outputs[0]
    join=g.nodes.new('GeometryNodeJoinGeometry')
    deps=depsgraph(scene)
    signature=[]
    for obj,slots in plans(scene,line_set):
        info=g.nodes.new('GeometryNodeObjectInfo');info.transform_space='RELATIVE';info.inputs['Object'].default_value=obj
        geometry=info.outputs['Geometry'];names={}
        for kind,n in chosen.items():
            if not surface_source(n):continue
            name=layer_name(obj.evaluated_get(deps).data,n);names[kind]=name
            store=g.nodes.new('GeometryNodeStoreNamedAttribute');store.data_type='FLOAT_COLOR' if color_source(n) else 'FLOAT_VECTOR';store.domain='CORNER'
            store.inputs['Name'].default_value=PREFIX+kind;links.new(geometry,store.inputs['Geometry'])
            if color_source(n):store.inputs['Value'].default_value=(0.,0.,0.,1.)
            if getattr(n,'rule_mode',False):
                from . import material_functions
                store.inputs['Value'].default_value=(0,0,0,0)
                color=material_functions.live_field(g,geometry,n)
                if color is not None:links.new(color,store.inputs['Value'])
            elif name:
                value=g.nodes.new('GeometryNodeInputNamedAttribute');value.data_type='FLOAT_COLOR' if color_source(n) else 'FLOAT_VECTOR';value.inputs['Name'].default_value=name
                links.new(value.outputs['Attribute'],store.inputs['Value'])
            geometry=store.outputs[0]
        if slots is not None:
            material=g.nodes.new('GeometryNodeInputMaterialIndex').outputs[0];keep=0.
            for index in slots:keep=math('MAXIMUM',keep,math('COMPARE',material,float(index)))
            delete=g.nodes.new('GeometryNodeDeleteGeometry');delete.domain='FACE'
            links.new(geometry,delete.inputs['Geometry']);links.new(math('SUBTRACT',1.,keep),delete.inputs['Selection']);geometry=delete.outputs[0]
        direction=(vector('SUBTRACT',fields['camera_location'],fields['position']) if scene.camera.data.type=='PERSP'
                   else vector('SUBTRACT',fields['camera_location'],fields['camera_location']))
        if scene.camera.data.type=='ORTHO':
            negate=g.nodes.new('ShaderNodeVectorMath');negate.operation='SCALE';links.new(fields['forward'],negate.inputs[0]);negate.inputs['Scale'].default_value=-1
            direction=negate.outputs[0]
        normal=g.nodes.new('GeometryNodeInputNormal').outputs[0]
        delete=g.nodes.new('GeometryNodeDeleteGeometry');delete.domain='FACE'
        links.new(geometry,delete.inputs['Geometry']);links.new(math('LESS_THAN',vector('DOT_PRODUCT',normal,direction),1e-12),delete.inputs['Selection'])
        size=g.nodes.new('GeometryNodeAttributeDomainSize');size.component='MESH';links.new(delete.outputs[0],size.inputs['Geometry'])
        choose=g.nodes.new('GeometryNodeSwitch');choose.input_type='GEOMETRY'
        links.new(math('GREATER_THAN',size.outputs['Face Count'],0.),choose.inputs['Switch'])
        links.new(geometry,choose.inputs['False']);links.new(delete.outputs[0],choose.inputs['True'])
        links.new(choose.outputs[0],join.inputs[0]);signature.append((obj.name_full,slots,names))
    result={}
    for kind,n in chosen.items():
        if not surface_source(n):continue
        attr=g.nodes.new('GeometryNodeInputNamedAttribute');attr.data_type='FLOAT_COLOR' if color_source(n) else 'FLOAT_VECTOR';attr.inputs['Name'].default_value=PREFIX+kind
        sample=g.nodes.new('GeometryNodeSampleNearestSurface');sample.data_type='FLOAT_COLOR' if color_source(n) else 'FLOAT_VECTOR'
        links.new(join.outputs[0],sample.inputs['Mesh']);links.new(attr.outputs['Attribute'],sample.inputs['Value'])
        links.new(fields['position'],sample.inputs['Sample Position'])
        if color_source(n):
            result[kind]=sample.outputs['Value'];continue
        split=g.nodes.new('ShaderNodeSeparateXYZ');links.new(sample.outputs['Value'],split.inputs[0])
        result[kind]=(split.outputs['X'],split.outputs['Y'])
    return result


def schema_key(scene,line_set,chosen):
    if not any(surface_source(n) for n in chosen.values()):return None
    deps=depsgraph(scene)
    return [(obj.as_pointer(),slots,tuple((kind,layer_name(obj.evaluated_get(deps).data,n)) for kind,n in chosen.items() if surface_source(n)))
            for obj,slots in plans(scene,line_set)]


# Single-frame asynchronous renders consume immutable main-thread snapshots.
_render_cache={}
_curve_specs={}
_pending={}
_dirty=set()
_states={}
_style_ready=set()
_active={}
_preparing=False
_keymaps=[]


def cached_payload(scene_name,frame,line_name):
    active=_active.get(scene_name)
    if active and active[0]==frame:return active[1].get((frame,line_name))
    if scene_name in _dirty:return None
    return _render_cache.get(scene_name,{}).get((frame,line_name))


def curve_resolution(key):
    for active in _active.values():
        if len(active)>3 and key in active[3]:return active[3][key]
    return None


def prepared(scene_name,frame):
    active=_active.get(scene_name)
    if active and active[0]==frame:return active[2]
    return (scene_name not in _dirty and scene_name in _style_ready
            and any(key[0]==frame for key in _render_cache.get(scene_name,{})))


def prepare_scene(scene,apply_styles=None):
    global _preparing
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Object UV render preparation requires the main thread')
    if bpy.app.is_job_running('RENDER'):
        raise RuntimeError('Object UV preparation waits until the current render finishes')
    if not uses(scene):
        if apply_styles and getattr(getattr(scene,'lea_line',None),'tree',None):
            from . import backend
            backend.sync_scene(scene)
        _curve_specs.pop(scene.name_full,None)
        _render_cache.pop(scene.name_full,None);_style_ready.discard(scene.name_full);_dirty.discard(scene.name_full);return
    from . import backend,routing,texture_maps,preview_material,distance_curves
    _preparing=True
    try:
        apply_styles=scene.lea_line.auto_sync if apply_styles is None else apply_styles
        if apply_styles:backend.sync_scene(scene)
        scene.view_layers[0].update()
        payloads={}
        with distance_curves.collect_resolutions() as curve_specs:
            for index,(line,common) in enumerate(backend._specifications(scene.lea_line.tree)):
                for visibility,enabled in (('VISIBLE',line.visible),('HIDDEN',line.hidden)):
                    if not enabled:continue
                    for job in routing.jobs(line,common,visibility):
                        key=(scene.frame_current,f'{backend.PREFIX}{index}:{visibility}'+job['suffix'])
                        payloads[key]=texture_maps.render_payload(scene,line,job['brush']) or {'noop':True}
        _render_cache[scene.name_full]=payloads
        _curve_specs[scene.name_full]=(scene.frame_current,dict(curve_specs))
        _dirty.discard(scene.name_full);_pending.pop(scene.name_full,None)
        if apply_styles:_style_ready.add(scene.name_full)
        else:_style_ready.discard(scene.name_full)
        _states[scene.name_full]=preview_material._source_state(scene)
    finally:_preparing=False


def request_prepare(scene):
    if threading.current_thread() is not threading.main_thread() or _preparing:return
    from . import preview_material
    if preview_material.is_suspended():
        _dirty.add(scene.name_full);_pending[scene.name_full]=time.monotonic()
        if not bpy.app.timers.is_registered(_prepare_tick):bpy.app.timers.register(_prepare_tick,first_interval=.2)
        return
    if not uses(scene):
        _curve_specs.pop(scene.name_full,None)
        _render_cache.pop(scene.name_full,None);_style_ready.discard(scene.name_full);_dirty.discard(scene.name_full);_pending.pop(scene.name_full,None);return
    _dirty.add(scene.name_full);_pending[scene.name_full]=time.monotonic()
    if not bpy.app.timers.is_registered(_prepare_tick):bpy.app.timers.register(_prepare_tick,first_interval=.2)


def _prepare_tick():
    from . import preview_material
    if preview_material.is_suspended():return .1 if _pending else None
    if bpy.app.is_job_running('RENDER'):return .2 if _pending else None
    for name,stamp in list(_pending.items()):
        if time.monotonic()-stamp<.2:continue
        scene=bpy.data.scenes.get(name)
        _pending.pop(name,None)
        if scene:
            try:prepare_scene(scene)
            except Exception as exc:
                scene.lea_line.last_message='Object UV render preparation: '+str(exc)
    return .1 if _pending else None


@persistent
def _source_changed(scene,depsgraph):
    if _preparing or threading.current_thread() is not threading.main_thread() or bpy.app.is_job_running('RENDER'):return
    for name in list(_render_cache):
        if bpy.data.scenes.get(name) is None:
            _curve_specs.pop(name,None)
            _render_cache.pop(name,None);_states.pop(name,None);_style_ready.discard(name);_dirty.discard(name);_pending.pop(name,None)
    if not uses(scene):return
    from . import preview_material
    if preview_material.is_suspended():return
    geometry=False;other=False
    for update in depsgraph.updates:
        block=update.id.original
        if getattr(block,'get',lambda *_:False)('rea_pencil_preview'):continue
        if isinstance(block,bpy.types.Mesh):geometry=True
        elif isinstance(block,(bpy.types.Object,bpy.types.Camera,bpy.types.Collection,bpy.types.Scene)):other=True
        elif isinstance(block,bpy.types.Image):
            tree=scene.lea_line.tree
            if any(n.bl_idname=='LEATextureMapNode' and n.image==block for n in tree.nodes):
                geometry=True
                from . import stamp_brush
                stamp_brush.invalidate(block)
                from .ui import _sync_if_enabled
                _sync_if_enabled(scene.lea_line)
    if not geometry and not other:return
    state=preview_material._source_state(scene)
    if geometry or state!=_states.get(scene.name_full):
        _states[scene.name_full]=state;request_prepare(scene)


@persistent
def clear_cache(*args):
    from . import stamp_brush
    stamp_brush.clear()
    _curve_specs.clear()
    _render_cache.clear();_pending.clear();_dirty.clear();_states.clear();_style_ready.clear();_active.clear()


@persistent
def _render_init(scene,*args):
    name=scene.name_full
    if name not in _dirty and any(k[0]==scene.frame_current for k in _render_cache.get(name,{})):
        frame,specs=_curve_specs.get(name,(scene.frame_current,{}))
        _active[name]=(scene.frame_current,dict(_render_cache[name]),name in _style_ready,dict(specs) if frame==scene.frame_current else {})


@persistent
def _render_done(*args):_active.clear()


class LEA_OT_prepared_render(bpy.types.Operator):
    bl_idname='lea_line.prepared_render'
    bl_label='Render Image'
    bl_description='Render the scene with REA GPU lines when enabled, otherwise prepare native rendering'
    @classmethod
    def poll(cls,context):return context.scene is not None
    def execute(self,context):
        return bpy.ops.lea_line.render_f12('EXEC_DEFAULT')
    def invoke(self,context,event):return self.execute(context)


def register():
    bpy.utils.register_class(LEA_OT_prepared_render)
    bpy.app.handlers.depsgraph_update_post.append(_source_changed)
    bpy.app.handlers.render_init.append(_render_init)
    bpy.app.handlers.render_complete.append(_render_done);bpy.app.handlers.render_cancel.append(_render_done)
    for handlers in (bpy.app.handlers.load_pre,bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre):handlers.append(clear_cache)
    keyconfig=bpy.context.window_manager.keyconfigs.addon
    if keyconfig:
        keymap=keyconfig.keymaps.new(name='Screen',space_type='EMPTY')
        item=keymap.keymap_items.new('lea_line.prepared_render','F12','PRESS')
        _keymaps.append((keymap,item))


def unregister():
    if _source_changed in bpy.app.handlers.depsgraph_update_post:bpy.app.handlers.depsgraph_update_post.remove(_source_changed)
    if _render_init in bpy.app.handlers.render_init:bpy.app.handlers.render_init.remove(_render_init)
    for handlers in (bpy.app.handlers.render_complete,bpy.app.handlers.render_cancel):
        if _render_done in handlers:handlers.remove(_render_done)
    for handlers in (bpy.app.handlers.load_pre,bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre):
        if clear_cache in handlers:handlers.remove(clear_cache)
    if bpy.app.timers.is_registered(_prepare_tick):bpy.app.timers.unregister(_prepare_tick)
    for keymap,item in _keymaps:keymap.keymap_items.remove(item)
    _keymaps.clear();clear_cache()
    if LEA_OT_prepared_render.is_registered:bpy.utils.unregister_class(LEA_OT_prepared_render)
