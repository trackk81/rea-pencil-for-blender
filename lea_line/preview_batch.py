"""Atomic owned GP data, ink, width and detached stroke-graph replacement."""
import threading
import math
import bpy
from . import preview_material, preview_camera, preview_style_stage, viewport


def _assign(preview, snapshot):
    preview.data = snapshot


def _assign_style(block, name, value):
    setattr(block, name, value)


def _style_plans(pairs, styles):
    candidates={preview.as_pointer():snapshot for preview,snapshot in pairs}
    plans=[];seen=set()
    for row in styles:
        if set(row)-{'preview','color','width','stroke','projection'} or 'preview' not in row:
            raise ValueError('Style update supports preview/color/width/stroke only')
        preview=row['preview'];key=preview_material._pointer(preview)
        if key not in candidates or key in seen:
            raise ValueError('Style update must name one unique batch output')
        seen.add(key);snapshot=candidates[key];plan={'preview':preview,'snapshot':snapshot}
        if 'stroke' in row:plan['stroke_payload']=row['stroke']
        if 'width' in row or 'projection' in row:
            width=float(row['width']) if 'width' in row else None
            if width is not None and (not math.isfinite(width) or width<=0):
                raise ValueError('Style width must be finite and positive')
            modifiers=[m for m in preview.modifiers if preview_camera._owned(m)]
            if len(modifiers)!=1:
                raise ValueError('Style width needs one existing owned camera graph')
            modifier=modifiers[0];graph=modifier.node_group
            if graph.library or graph.users!=1 or preview_camera.PREFIX+'radius' not in graph.nodes:
                raise ValueError('Shared/linked/incompatible width graph is refused')
            if 'projection' in row:
                if row['projection'] not in preview.users_scene:raise ValueError('Projection scene must own the preview')
                preview_camera.projection(row['projection'])
                plan['projection']=row['projection']
            plan.update(width=width if width is not None else 2*graph.nodes[preview_camera.PREFIX+'radius'].inputs[1].default_value,modifier=modifier,old_graph=graph)
        if 'color' in row:
            color=tuple(float(v) for v in row['color'])
            if len(color)!=3 or not all(math.isfinite(v) and 0<=v<=1 for v in color):
                raise ValueError('Style color must contain three finite values within0..1')
            if len(snapshot.materials)!=1 or len(preview.data.materials)!=1:
                raise ValueError('Uniform style color currently requires one ink slot')
            material=snapshot.materials[0]
            if (material is None or not viewport._owned(material,preview.get(viewport.OWNER)) or material.library
                    or material.grease_pencil is None):
                raise ValueError('Style color needs an owned ink material')
            lineart=[m for m in preview.modifiers if m.type=='LINEART']
            if len(lineart)!=1 or lineart[0].target_material!=material:
                raise ValueError('Style color needs the matching Line Art material')
            plan.update(color=color,old_material=material,lineart=lineart[0])
        plans.append(plan)
    return plans


def _discard_styles(plans, restore_slots=False):
    for plan in plans:
        if 'stroke_plan' in plan:preview_style_stage.discard(plan['stroke_plan'])
        if restore_slots and 'new_material' in plan:
            plan['snapshot'].materials[0]=plan['old_material']
        for key,collection in (('new_graph',bpy.data.node_groups),('new_material',bpy.data.materials)):
            block=plan.get(key)
            if preview_material._valid(block) and block.users==0:collection.remove(block)


def replace_snapshots(pairs, styles=()):
    """Commit detached snapshots together, or restore every previous GP ID.

    Validation failures leave all candidates with the caller. Once validation
    succeeds, a commit failure disposes unused owned candidates after rollback.
    Optional styles carry color/base width and detached non-dashed stroke
    graphs; the live caller enforces supported effects. Owned materials/graphs
    are prepared before commit. Blender data access stays on the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Preview snapshot replacement needs the main thread')
    if preview_material.is_suspended() or bpy.app.is_job_running('RENDER'):
        raise RuntimeError('Preview snapshots cannot be replaced during render/lifecycle restoration')
    pairs=list(pairs);styles=list(styles)
    if not pairs:
        if styles:raise ValueError('Style update requires snapshot batch outputs')
        return 0
    objects=set();candidates=set();previous=[]
    for preview,snapshot in pairs:
        if not preview_material._preview_owned(preview):
            raise ValueError('Preview output is not owned')
        if preview.library or len(preview.users_scene)!=1:
            raise ValueError('Linked/shared-scene preview output cannot be replaced')
        if preview_material._pointer(preview) in preview_material._stages:
            raise ValueError('Preview output is held by final-render staging')
        old=preview.data;owner=preview.get(viewport.OWNER)
        for data in (old,snapshot):
            if (not preview_material._valid(data) or data.bl_rna.identifier!='GreasePencilv3'
                    or data.library or not data.get(viewport.TAG) or data.get(viewport.OWNER)!=owner):
                raise ValueError('Snapshot ownership/type mismatch')
        if not snapshot.get(preview_material.OUTPUT) or snapshot.users or snapshot.use_fake_user:
            raise ValueError('Candidate must be a detached owned output snapshot')
        if old.users>1+int(old.use_fake_user):
            raise ValueError('Shared output geometry cannot be replaced transactionally')
        key=preview.as_pointer();candidate=snapshot.as_pointer()
        if key in objects or candidate in candidates:
            raise ValueError('Duplicate preview output or candidate')
        objects.add(key);candidates.add(candidate)
        previous.append((preview,old,old.use_fake_user))
    plans=_style_plans(pairs,styles)
    attachments=[]
    try:
        for plan in plans:
            if 'stroke_payload' in plan:
                plan['stroke_plan']=preview_style_stage.prepare(plan['preview'],plan['stroke_payload'])
            if 'width' in plan:
                graph=plan['new_graph']=plan['old_graph'].copy()
                graph.use_fake_user=False
                if 'projection' in plan:preview_camera.sync_fields(graph,plan['projection'])
                graph.nodes[preview_camera.PREFIX+'radius'].inputs[1].default_value=plan['width']*.5
                attachments.append((plan['modifier'],'node_group',plan['old_graph'],graph))
            if 'color' in plan:
                material=plan['new_material']=plan['old_material'].copy()
                material.use_fake_user=False
                material.grease_pencil.color=(*plan['color'],plan['old_material'].grease_pencil.color[3])
                plan['snapshot'].materials[0]=material
                attachments.append((plan['lineart'],'target_material',plan['old_material'],material))
    except Exception:
        _discard_styles(plans,restore_slots=True)
        raise
    # Hold all old snapshots even while their object references are exchanged.
    try:
        for _,old,_ in previous:old.use_fake_user=True
        for plan in plans:
            if 'stroke_plan' in plan:attachments.extend(preview_style_stage.attachments(plan['stroke_plan']))
        for preview,snapshot in pairs:_assign(preview,snapshot)
        for block,name,_,value in attachments:_assign_style(block,name,value)
    except Exception:
        for preview,old,_ in previous:preview.data=old
        # Line Art accepts only a material already present in the object's GP
        # palette, so restore geometry before restoring its material target.
        for block,name,old,_ in attachments:setattr(block,name,old)
        for plan in plans:
            if 'stroke_plan' in plan:preview_style_stage.rollback(plan['stroke_plan'])
        for _,old,fake_user in previous:old.use_fake_user=fake_user
        for _,snapshot in pairs:preview_material._remove_output(snapshot)
        _discard_styles(plans)
        raise
    for _,old,fake_user in previous:
        old.use_fake_user=fake_user
        preview_material._remove_output(old)
    for plan in plans:
        if 'stroke_plan' in plan:preview_style_stage.finish(plan['stroke_plan'])
        for key,collection in (('old_graph',bpy.data.node_groups),('old_material',bpy.data.materials)):
            block=plan.get(key)
            if preview_material._valid(block) and block.users==0:collection.remove(block)
    return len(pairs)
