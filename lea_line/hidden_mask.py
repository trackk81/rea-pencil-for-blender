"""Object-target hidden masks: original set-level workflow, independent backends."""
import bpy
import threading

def objects(line_set):
    # A render worker uses only the prepared qualified names in its line payload.
    if threading.current_thread() is not threading.main_thread():return ()
    tree=line_set.id_data
    outputs=[n for n in tree.nodes if n.bl_idname=='LEAOutputNode' and n.enabled]
    masks=[]
    for output in outputs:
        for socket in output.inputs:
            if not socket.is_linked:continue
            node=socket.links[0].from_node
            if node.bl_idname!='LEALineSetNode' or not node.enabled or not node.is_mask_hidden_lines or node.as_pointer()==line_set.as_pointer():continue
            if len(outputs)!=1:raise ValueError('Hidden masks currently require one Line output')
            if len(node.materials):raise ValueError('Hidden masks currently support object targets, not material-face targets')
            scope=set(node.source_collection.all_objects) if node.source_collection else set(bpy.context.scene.objects)
            targets=[item.object for item in node.objects if item.object]
            if not targets and (node.source_collection or node.target_mode=='ALL'):targets=list(scope)
            masks.extend(obj for obj in targets if obj in scope and obj.type=='MESH' and not obj.hide_render)
    return tuple(dict.fromkeys(masks))

def names(brush):
    return tuple((obj.library.filepath+'/' if obj.library else '')+obj.name_full for obj in getattr(brush,'hidden_mask_objects',()))

def select(mask_names):
    if not mask_names:return
    from freestyle.types import Operators,UnaryPredicate1D
    from freestyle.functions import GetOccludersF1D
    selected=set(mask_names);occluders=GetOccludersF1D()
    class Keep(UnaryPredicate1D):
        def __call__(self,edge):
            return not any(((shape.library_path+'/' if shape.library_path else '')+shape.name) in selected for shape in occluders(edge))
    Operators.select(Keep())

def live(g,geometry,brush,fields,scene):
    targets=getattr(brush,'hidden_mask_objects',())
    if not targets:return geometry
    links=g.links
    join=g.nodes.new('GeometryNodeJoinGeometry')
    for obj in targets:
        info=g.nodes.new('GeometryNodeObjectInfo');info.transform_space='RELATIVE';info.inputs['Object'].default_value=obj
        links.new(info.outputs['Geometry'],join.inputs[0])
    delta=g.nodes.new('ShaderNodeVectorMath');delta.operation='SUBTRACT';links.new(fields['position'],delta.inputs[0]);links.new(fields['camera_location'],delta.inputs[1])
    length=g.nodes.new('ShaderNodeVectorMath');length.operation='LENGTH';links.new(delta.outputs[0],length.inputs[0])
    limit=g.nodes.new('ShaderNodeMath');limit.operation='SUBTRACT';limit.inputs[1].default_value=1e-5;links.new(length.outputs['Value'],limit.inputs[0])
    ray=g.nodes.new('GeometryNodeRaycast');ray.data_type='FLOAT'
    links.new(join.outputs[0],ray.inputs['Target Geometry']);links.new(fields['camera_location'],ray.inputs['Source Position'])
    links.new(delta.outputs[0],ray.inputs['Ray Direction']);links.new(limit.outputs[0],ray.inputs['Ray Length'])
    from . import preview_camera
    if not preview_camera.projection(scene)[1]:
        offset=g.nodes.new('ShaderNodeVectorMath');offset.operation='SCALE';links.new(fields['forward'],offset.inputs[0]);links.new(fields['raw_depth'],offset.inputs['Scale'])
        origin=g.nodes.new('ShaderNodeVectorMath');origin.operation='SUBTRACT';links.new(fields['position'],origin.inputs[0]);links.new(offset.outputs[0],origin.inputs[1])
        links.new(origin.outputs[0],ray.inputs['Source Position']);links.new(fields['forward'],ray.inputs['Ray Direction']);links.new(fields['raw_depth'],limit.inputs[0])
    delete=g.nodes.new('GeometryNodeDeleteGeometry');delete.domain='POINT'
    links.new(geometry,delete.inputs['Geometry']);links.new(ray.outputs['Is Hit'],delete.inputs['Selection'])
    return delete.outputs[0]
