"""Linear distance opacity through Blender 4.4's Freestyle alpha color attribute.

The native renderer packs alpha into a grayscale BYTE_COLOR attribute, then
reads its Color output. Undo that sRGB decode in our owned Line Style graph.
Distance-alpha styles and the explicit Linear Alpha option opt in; other legacy
outputs retain their appearance.
"""
import threading
import bpy

TAG='rea_distance_linear_alpha'


def sync(style,enabled,setter):
    graph=style.node_tree
    owned=bool(graph and graph.get(TAG)==1)
    if not enabled:
        if owned:setter(style,'use_nodes',False)
        return
    if not owned:
        if graph is not None:
            raise ValueError('Distance alpha correction cannot replace a custom Line Style shader graph')
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('Distance alpha shader preparation requires the main thread; use REA Render or F12')
        setter(style,'use_nodes',True)
        graph=style.node_tree;graph.nodes.clear()
        attr=graph.nodes.new('ShaderNodeAttribute');attr.name='Freestyle Alpha';attr.attribute_name='Alpha'
        gray=graph.nodes.new('ShaderNodeRGBToBW');graph.links.new(attr.outputs['Color'],gray.inputs[0]);value=gray.outputs[0]
        def math(op,a,b):
            node=graph.nodes.new('ShaderNodeMath');node.operation=op
            for v,socket in zip((a,b),node.inputs):
                if isinstance(v,bpy.types.NodeSocket):graph.links.new(v,socket)
                else:socket.default_value=v
            return node.outputs[0]
        low=math('MULTIPLY',value,12.92)
        high=math('SUBTRACT',math('MULTIPLY',math('POWER',value,1/2.4),1.055),.055)
        choose=math('LESS_THAN',value,.0031308)
        encoded=math('ADD',math('MULTIPLY',choose,low),math('MULTIPLY',math('SUBTRACT',1.,choose),high))
        out=graph.nodes.new('ShaderNodeOutputLineStyle');out.name='REA Linear Distance Alpha'
        out.inputs['Color Fac'].default_value=0.;out.inputs['Alpha Fac'].default_value=1.
        graph.links.new(encoded,out.inputs['Alpha']);graph[TAG]=1
    setter(style,'use_nodes',True)
