"""Private distance profiles, immutable CPU tables and native live curve fields."""
import json,threading
from collections import OrderedDict
from contextlib import contextmanager
import numpy as np
import bpy
from . import curves

DEFAULT_POINTS=[[0.,1.,'AUTO'],[.2,.4,'AUTO'],[1.,.1,'AUTO']]
CURVE='.rea distance profile'
SIZE='.rea distance profile size'
ALPHA='.rea distance profile alpha'
ZERO='.rea distance zero curves'
_tables=OrderedDict()
_collecting=None


def node_key(node):
    tree=node.id_data
    return (tree.library.filepath if tree.library else '',tree.name_full,node.name)


@contextmanager
def collect_resolutions():
    global _collecting
    previous=_collecting;result={};_collecting=result
    try:yield result
    finally:_collecting=previous


def resolve(node):
    if threading.current_thread() is not threading.main_thread():
        from . import object_uv
        prepared=object_uv.curve_resolution(node_key(node))
        if prepared is None:
            raise RuntimeError('Distance curve evaluation requires main-thread preparation; use F12')
        return prepared
    data=curves.resolved_payload(node)['SIZE'];key=json.dumps(data,sort_keys=True)
    if key not in _tables:
        mapping=curves.get_mapping_node(node,'SIZE').mapping;mapping.initialize()
        x=np.unique(np.r_[np.linspace(0,1,4097),np.clip([p[0] for p in data['points']],0,1)])
        y=np.clip([mapping.evaluate(mapping.curves[0],float(t)) for t in x],0.,1.)
        x.setflags(write=False);y.setflags(write=False);_tables[key]=(x,y)
        if len(_tables)>16:_tables.popitem(last=False)
    else:_tables.move_to_end(key)
    result=(data,_tables[key])
    if _collecting is not None:_collecting[node_key(node)]=result
    return result


def clear_cache():_tables.clear()


def draw(layout,node):
    layout.prop(node,'curve_enabled',text='Use Reduction Curve')
    if not node.curve_enabled:return
    owner=curves.get_mapping_node(node,'SIZE')
    if owner is not None:layout.template_curve_mapping(owner,'mapping')
    else:layout.label(text='Synchronize to initialize curve',icon='INFO')


def sync_live(graph,brush,setter):
    data=getattr(brush,'distance_curve_data',None);node=graph.nodes.get(CURVE)
    independent=getattr(brush,'distance_independent',False)
    if data is None and node is None and not independent:return (0,0)
    structural=0
    if node is None:
        node=graph.nodes.new('ShaderNodeFloatCurve');node.name=CURVE
        graph.links.new(graph.nodes['Distance Far Clamp'].outputs[0],node.inputs['Value'])
        minimum=graph.nodes.new('ShaderNodeMath');minimum.operation='MAXIMUM';minimum.inputs[1].default_value=0.
        graph.links.new(node.outputs[0],minimum.inputs[0])
        maximum=graph.nodes.new('ShaderNodeMath');maximum.operation='MINIMUM';maximum.inputs[1].default_value=1.
        graph.links.new(minimum.outputs[0],maximum.inputs[0])
        for name,source,target in ((SIZE,'Distance Size Factor','Reduced Radius'),(ALPHA,'Distance Alpha Factor','Reduced Opacity')):
            switch=graph.nodes.new('GeometryNodeSwitch');switch.name=name;switch.input_type='FLOAT'
            graph.links.new(graph.nodes[source].outputs[0],switch.inputs['False'])
            graph.links.new(maximum.outputs[0],switch.inputs['True'])
            graph.links.new(switch.outputs[0],graph.nodes[target].inputs[1])
        structural=9
    if graph.nodes.get(ZERO) is None:
        geometry=graph.nodes['Distance Enabled'].inputs['True'].links[0].from_socket
        radius=graph.nodes.new('GeometryNodeInputRadius')
        zero=graph.nodes.new('ShaderNodeMath');zero.operation='LESS_THAN';zero.inputs[1].default_value=1e-20
        graph.links.new(radius.outputs[0],zero.inputs[0])
        delete=graph.nodes.new('GeometryNodeDeleteGeometry');delete.domain='CURVE'
        graph.links.new(geometry,delete.inputs['Geometry']);graph.links.new(zero.outputs[0],delete.inputs['Selection'])
        choose=graph.nodes.new('GeometryNodeSwitch');choose.name=ZERO;choose.input_type='GEOMETRY'
        graph.links.new(geometry,choose.inputs['False']);graph.links.new(delete.outputs[0],choose.inputs['True'])
        graph.links.new(choose.outputs[0],graph.nodes['Distance Enabled'].inputs['True'])
        structural+=4
    setter(graph.nodes[SIZE].inputs['Switch'],'default_value',bool(data is not None and brush.distance_fade))
    setter(graph.nodes[ALPHA].inputs['Switch'],'default_value',bool(data is not None and brush.distance_alpha))
    setter(graph.nodes[ZERO].inputs['Switch'],'default_value',bool((data is not None or independent) and brush.distance_fade))
    changed=curves.apply_mapping(node.mapping,data) if data is not None else 0
    if changed:graph.update_tag()
    return structural,changed
