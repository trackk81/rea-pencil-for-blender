"""Render beauty with the independent GPU line pass, preserving the scene graph."""
import time
import bpy
from . import gpu_render, ui


def _render_beauty(scene, write_still):
    return bpy.ops.render.render(write_still=write_still, scene=scene.name)


def render(scene, *, write_still=False):
    if scene.render.engine not in {'BLENDER_EEVEE_NEXT', 'CYCLES', 'BLENDER_WORKBENCH'}:
        raise ValueError('장면 렌더 엔진을 EEVEE 또는 Cycles로 선택하세요')
    started = time.perf_counter()
    old_nodes = scene.use_nodes
    graph = scene.node_tree
    original_ids = {n.as_pointer() for n in graph.nodes} if graph else set()
    old_active = graph.nodes.active if graph else None
    flags = {k: getattr(scene.render, k) for k in
             ('engine', 'use_freestyle', 'use_compositing', 'use_sequencer')}
    old_auto = scene.lea_line.auto_sync
    hidden = []
    rewired = []
    node_states = []
    image = None
    try:
        with ui.suspend_sync():
            scene.lea_line.auto_sync = False
        # Replace generated line art, while keeping authored GP drawings in beauty.
        for obj in scene.objects:
            generated = obj.get('rea_pencil_preview') or (
                obj.type == 'GREASEPENCIL' and any(m.type == 'LINEART' for m in obj.modifiers))
            if generated and not obj.hide_render:
                hidden.append((obj, obj.hide_render))
                obj.hide_render = True
        line = gpu_render.render_lines(scene, capture=True)
        if line['status'] == 'cancelled':
            return line
        pixels = line.pop('pixels')
        image = bpy.data.images.new('.REA GPU line composite', width=pixels.shape[1],
            height=pixels.shape[0], alpha=True, float_buffer=True)
        image.alpha_mode = 'PREMUL'
        image.pixels.foreach_set(pixels.ravel())
        image.update()
        del pixels
        scene.use_nodes = True
        graph = scene.node_tree
        if old_nodes:
            sources = [n for n in graph.nodes if n.bl_idname == 'CompositorNodeRLayers'
                and (n.scene is None or n.scene == scene) and n.layer in ('', scene.view_layers[0].name)]
            if not sources:
                raise ValueError('GPU 선 합성에는 현재 장면의 Render Layers 노드가 필요합니다')
        else:
            # Do not change dormant authored nodes when compositing was disabled.
            for node in graph.nodes:
                if node.as_pointer() not in original_ids:
                    continue
                if node.bl_idname in {'CompositorNodeOutputFile', 'CompositorNodeViewer'}:
                    node_states.append((node, node.mute, None))
                    node.mute = True
                if node.bl_idname == 'CompositorNodeComposite':
                    node_states.append((node, node.mute, node.use_alpha))
                    node.mute = False
                    node.use_alpha = True
                    links = [(l.from_socket, l.to_socket) for l in node.inputs['Image'].links]
                    rewired.extend(links)
                    for link in list(node.inputs['Image'].links):
                        graph.links.remove(link)
            source = graph.nodes.new('CompositorNodeRLayers')
            source.layer = scene.view_layers[0].name
            outputs = [n for n in graph.nodes if n.bl_idname == 'CompositorNodeComposite']
            if not outputs:
                outputs = [graph.nodes.new('CompositorNodeComposite')]
            for node in outputs:
                graph.links.new(source.outputs['Image'], node.inputs['Image'])
            sources = [source]
        ink = graph.nodes.new('CompositorNodeImage')
        ink.image = image
        for source in sources:
            outgoing = [(l.from_socket, l.to_socket) for l in source.outputs['Image'].links]
            over = graph.nodes.new('CompositorNodeAlphaOver')
            over.inputs[0].default_value = 1.
            graph.links.new(source.outputs['Image'], over.inputs[1])
            graph.links.new(ink.outputs['Image'], over.inputs[2])
            for a, b in outgoing:
                if source.as_pointer() in original_ids:
                    rewired.append((a, b))
                graph.links.new(over.outputs['Image'], b)
        scene.render.use_freestyle = False
        scene.render.use_compositing = True
        scene.render.use_sequencer = False
        operation = _render_beauty(scene, write_still)
        return {'status': 'cancelled' if 'CANCELLED' in operation else 'ok',
                'seconds': time.perf_counter()-started, 'line_seconds': line['seconds'],
                'warnings': line['warnings'], 'excluded_line_helpers': [o.name for o, _ in hidden]}
    finally:
        if scene.node_tree:
            graph = scene.node_tree
            for node in list(graph.nodes):
                if node.as_pointer() not in original_ids:
                    graph.nodes.remove(node)
            for a, b in rewired:
                graph.links.new(a, b)
            for node, muted, alpha in node_states:
                node.mute = muted
                if alpha is not None:
                    node.use_alpha = alpha
            graph.nodes.active = old_active
        if image is not None:
            bpy.data.images.remove(image)
        gpu_render._restore_grease_pencils(hidden)
        for k, value in flags.items():
            setattr(scene.render, k, value)
        scene.use_nodes = old_nodes
        with ui.suspend_sync():
            scene.lea_line.auto_sync = old_auto
