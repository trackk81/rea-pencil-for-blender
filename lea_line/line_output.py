"""Transparent Freestyle-only output with temporary compositor routing."""
import time
import bpy
from . import backend,ui


def _invoke_render(scene,write_still):
    return bpy.ops.render.render(write_still=write_still,scene=scene.name)


def render(scene,view_layer=None,write_still=False):
    if not scene.camera:raise ValueError('렌더용 카메라를 먼저 지정하세요')
    if not scene.lea_line.tree:raise ValueError('라인 그래프를 먼저 만드세요')
    layer=view_layer or scene.view_layers[0]
    if layer.name not in scene.view_layers:raise ValueError('현재 장면의 뷰 레이어를 선택하세요')
    started=time.perf_counter();old_nodes=scene.use_nodes
    existing=list(scene.node_tree.nodes) if scene.node_tree else []
    original_ids={n.as_pointer() for n in existing}
    old_active=scene.node_tree.nodes.active if scene.node_tree else None
    file_outputs=[(n,n.mute) for n in existing if n.bl_idname=='CompositorNodeOutputFile']
    flags={k:getattr(scene.render,k) for k in ('use_freestyle','use_compositing','use_sequencer')}
    image=scene.render.image_settings
    format_state={k:getattr(image,k) for k in ('file_format','color_mode','color_depth')}
    layers=[(v,v.use,v.freestyle_settings.as_render_pass) for v in scene.view_layers]
    old_auto=scene.lea_line.auto_sync;outputs=[]
    try:
        warnings=backend.sync_scene(scene)
        with ui.suspend_sync():scene.lea_line.auto_sync=False
        scene.render.use_freestyle=True;scene.render.use_compositing=True;scene.render.use_sequencer=False
        image.file_format='PNG';image.color_mode='RGBA';image.color_depth='8'
        for v,_,_ in layers:
            v.use=v==layer
            if v==layer:v.freestyle_settings.as_render_pass=True
        scene.use_nodes=True;graph=scene.node_tree
        for node,_ in file_outputs:node.mute=True
        source=graph.nodes.new('CompositorNodeRLayers');source.label='REA · 선만 출력';source.layer=layer.name
        line=source.outputs.get('Freestyle')
        if line is None:raise RuntimeError('Freestyle 렌더 패스를 만들지 못했습니다')
        composites=[n for n in graph.nodes if n.bl_idname=='CompositorNodeComposite']
        if not composites:composites=[graph.nodes.new('CompositorNodeComposite')]
        # Blender4.4 does not expose a Composite active-output flag. Route each
        # output temporarily so node-editor selection cannot choose the beauty.
        for node in composites:
            links=[(l.from_socket,l.to_socket) for l in node.inputs['Image'].links]
            alpha=getattr(node,'use_alpha',None)
            outputs.append((node,links,node.mute,alpha,node.as_pointer() in original_ids))
            node.mute=False
            if alpha is not None:node.use_alpha=True
            graph.links.new(line,node.inputs['Image'])
        result=_invoke_render(scene,write_still)
        if 'FINISHED' not in result:raise RuntimeError('라인 렌더가 취소되었습니다')
        return {'seconds':time.perf_counter()-started,'warnings':warnings,'view_layer':layer.name}
    finally:
        graph=scene.node_tree
        if graph:
            for node,links,muted,alpha,original in outputs:
                if original:
                    for link in list(node.inputs['Image'].links):graph.links.remove(link)
                    for a,b in links:graph.links.new(a,b)
                    node.mute=muted
                    if alpha is not None:node.use_alpha=alpha
            for node in list(graph.nodes):
                if node.as_pointer() not in original_ids:graph.nodes.remove(node)
            graph.nodes.active=old_active
        for node,value in file_outputs:node.mute=value
        for v,used,separate in layers:v.use=used;v.freestyle_settings.as_render_pass=separate
        for key,value in flags.items():setattr(scene.render,key,value)
        image.file_format=format_state['file_format'];image.color_mode=format_state['color_mode'];image.color_depth=format_state['color_depth']
        scene.use_nodes=old_nodes
        with ui.suspend_sync():scene.lea_line.auto_sync=old_auto
