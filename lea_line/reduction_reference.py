"""Independent object-origin distance reduction; Blender IDs stay on main thread."""
import threading
import numpy as np
import bpy

INFO='.rea reduction reference'
SWITCH='.rea reduction origin'


def active(brush):
    return bool((getattr(brush,'distance_independent',False) or getattr(brush,'distance_reference_enabled',False) or getattr(brush,'distance_curve_data',None) is not None) and
                (getattr(brush,'distance_fade',False) or getattr(brush,'distance_alpha',False)))


def capture(scene,brush):
    if not active(brush):return None
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Reference distance snapshots require the Blender main thread; use REA Render or F12')
    if len(scene.view_layers)!=1 or not scene.camera:
        raise ValueError('Reference distance reduction requires one view layer and a scene camera')
    if scene.render.use_motion_blur:
        raise ValueError('Reference distance motion blur is not verified')
    if getattr(brush, 'distance_independent', False):
        from .routing import ResolvedBrush
        channels = {}
        for kind, spec in brush.distance_channels.items():
            if not spec['enabled']:
                channels[kind] = None
                continue
            proxy = ResolvedBrush(brush, {
                'distance_independent': False, 'distance_reference_enabled': spec['reference'],
                'distance_reference_object': spec['object'], 'distance_curve_table': spec['profile'],
                'distance_curve_data': spec['data'], 'distance_start': spec['near'], 'distance_end': spec['far'],
                'distance_fade': kind == 'SIZE', 'distance_alpha': kind == 'ALPHA',
                'far_thickness': spec['factor'], 'far_alpha': spec['factor']})
            # Even a linear camera channel needs a frozen origin for the custom shader.
            if not active(proxy):
                proxy.overrides['distance_reference_enabled'] = True
                proxy.overrides['distance_reference_object'] = scene.camera
            channels[kind] = capture(scene, proxy)
        return {'channels': channels}
    reference=getattr(brush,'distance_reference_enabled',False)
    obj=getattr(brush,'distance_reference_object',None) if reference else scene.camera
    profile=getattr(brush,'distance_curve_table',None)
    origin=(0.,0.,0.)
    if obj:
        # Freestyle may pass an evaluated Scene and evaluated node pointers.
        # Resolve their original IDs before checking scene membership.
        source_scene=bpy.data.scenes.get(scene.name_full) or scene
        obj=obj.original
        member=source_scene.objects.get(obj.name)
        if member is None or member.original!=obj:
            raise ValueError('Reduction Reference Object must belong to the rendered scene')
        from . import object_uv
        origin=tuple(obj.evaluated_get(object_uv.depsgraph(source_scene)).matrix_world.translation)
    return {'origin':origin,'near':float(brush.distance_start),'far':float(brush.distance_end),
            'size':bool(brush.distance_fade),'alpha':bool(brush.distance_alpha),
            'far_size':float(brush.far_thickness),'far_alpha':float(brush.far_alpha),'profile':profile}


def factors(payload,points):
    if 'channels' in payload:
        return tuple(factors(payload['channels'][kind], points)[index]
                     if payload['channels'][kind] is not None else np.ones(len(points))
                     for index, kind in enumerate(('SIZE', 'ALPHA')))
    distance=np.linalg.norm(np.asarray(points)-payload['origin'],axis=1)
    t=np.clip((distance-payload['near'])/(payload['far']-payload['near']),0.,1.)
    if payload.get('profile') is not None:
        profile=np.interp(t,*payload['profile'])
        return (profile if payload['size'] else np.ones_like(t),profile if payload['alpha'] else np.ones_like(t))
    return (1+t*(payload['far_size']-1) if payload['size'] else np.ones_like(t),
            1+t*(payload['far_alpha']-1) if payload['alpha'] else np.ones_like(t))


def sync_live(graph,brush,setter):
    """Add optional fields lazily, preserving existing off-state graph IDs."""
    enabled=bool(getattr(brush,'distance_reference_enabled',False) and (brush.distance_fade or brush.distance_alpha));switch=graph.nodes.get(SWITCH)
    if switch is None and not enabled:return 0
    writes=0
    if switch is None:
        distance=graph.nodes['Camera Distance'];camera=distance.inputs[1].links[0].from_socket
        info=graph.nodes.new('GeometryNodeObjectInfo');info.name=INFO;info.transform_space='RELATIVE'
        switch=graph.nodes.new('GeometryNodeSwitch');switch.name=SWITCH;switch.input_type='VECTOR'
        graph.links.new(camera,switch.inputs['False']);graph.links.new(info.outputs['Location'],switch.inputs['True'])
        graph.links.new(switch.outputs[0],distance.inputs[1]);writes+=5
    setter(switch.inputs['Switch'],'default_value',enabled)
    setter(graph.nodes[INFO].inputs['Object'],'default_value',getattr(brush,'distance_reference_object',None) if enabled else None)
    return writes


def draw(layout,node):
    layout.prop(node,'refer_object_on',text='Refer Object')
    row=layout.row();row.enabled=node.refer_object_on;row.prop(node,'object_reference',text='Reference Object')


class LEA_PT_reduction_settings(bpy.types.Panel):
    bl_idname='LEA_PT_reduction_settings';bl_label='Reduction Settings'
    bl_space_type='NODE_EDITOR';bl_region_type='UI';bl_category='REA Pencil Line';bl_order=101
    @classmethod
    def poll(cls,context):
        tree=getattr(getattr(context,'space_data',None),'edit_tree',None)
        return bool(tree and tree.bl_idname=='LEALineTree' and tree.ui_layout=='ORIGINAL'
                    and tree.nodes.active and tree.nodes.active.bl_idname=='LEADistanceReductionNode')
    def draw(self,context):
        node=context.space_data.edit_tree.nodes.active;layout=self.layout
        layout.use_property_split=True;layout.use_property_decorate=False
        layout.prop(node,'enabled',text='On');col=layout.column();col.enabled=node.enabled
        col.prop(node,'distance_start',text='Start');col.prop(node,'distance_end',text='End')
        from . import distance_curves
        distance_curves.draw(col,node)
        draw(col,node)
        from . import independent_reduction
        if independent_reduction.draw_channel_settings(col,node):
            return
        col.prop(node,'reduce_thickness',text='Size Reduction')
        row=col.row();row.enabled=node.reduce_thickness and not node.curve_enabled;row.prop(node,'far_thickness',text='Far Size')
        col.prop(node,'reduce_opacity',text='Alpha Reduction')
        row=col.row();row.enabled=node.reduce_opacity and not node.curve_enabled;row.prop(node,'far_alpha',text='Far Alpha')


def register():bpy.utils.register_class(LEA_PT_reduction_settings)
def unregister():
    if LEA_PT_reduction_settings.is_registered:bpy.utils.unregister_class(LEA_PT_reduction_settings)

