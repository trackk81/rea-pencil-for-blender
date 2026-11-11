"""Detached copies of post-Line-Art style graphs for atomic mixed updates."""
import uuid
import bpy
from .blender_compat import grease_pencil_data
from . import preview_stroke as stroke


def prepare(preview,payload):
    if set(payload)-{'brush','scene','scale','needed'} or not {'brush','scene'}<=set(payload):
        raise ValueError('Invalid stroke staging payload')
    scene,brush=payload['scene'],payload['brush']
    if scene not in preview.users_scene or (payload.get('needed',True) and getattr(brush,'dashed',False)):
        raise ValueError('Stroke staging requires the owning scene and a non-dashed brush')
    mods=stroke._modifiers(preview)
    if len(mods)>1 or (mods and (mods[0].node_group.library or mods[0].node_group.users!=1)):
        raise ValueError('Shared/linked/duplicate stroke graphs cannot be staged')
    old=mods[0] if mods else None
    plan={'preview':preview,'modifier':old,'old_graph':old.node_group if old else None,
          'old_owner':preview.get(stroke.OWNER),'new_graph':None,'created':None,
          'enabled':bool(payload.get('needed',True))}
    if not plan['enabled']:return plan
    token=plan['old_owner'] or uuid.uuid4().hex;plan['token']=token
    data=grease_pencil_data().new('.rea detached style staging')
    obj=bpy.data.objects.new('.rea detached style staging',data);obj[stroke.OWNER]=token
    detached=None
    try:
        if old:
            mod=obj.modifiers.new(stroke.MODIFIER_NAME,'NODES');mod.node_group=old.node_group.copy()
        stroke.sync(obj,brush,scene,float(payload.get('scale',1)))
        staged=stroke._modifiers(obj)
        if len(staged)!=1:raise ValueError('Active brush did not produce one stroke graph')
        detached=staged[0].node_group;staged[0].node_group=None
        plan['new_graph']=detached
        return plan
    finally:
        graphs=[m.node_group for m in obj.modifiers if m.type=='NODES' and m.node_group]
        bpy.data.objects.remove(obj,do_unlink=True);grease_pencil_data().remove(data)
        for graph in graphs:
            if graph.users==0:bpy.data.node_groups.remove(graph)


def attachments(plan):
    obj=plan['preview'];mod=plan['modifier'];items=[]
    if plan['enabled']:
        if mod is None:
            mod=obj.modifiers.new(stroke.MODIFIER_NAME,'NODES');mod.show_viewport=False;mod.show_render=False
            plan['created']=mod;plan['modifier']=mod
        if obj.get(stroke.OWNER)!=plan['token']:obj[stroke.OWNER]=plan['token']
        items.append((mod,'node_group',mod.node_group,plan['new_graph']))
    if mod:
        items.extend(((mod,'show_viewport',mod.show_viewport,plan['enabled']),
                      (mod,'show_render',mod.show_render,False)))
    return items


def rollback(plan):
    if plan['created'] is not None:
        plan['preview'].modifiers.remove(plan['created'])
    if plan['old_owner'] is None:
        if stroke.OWNER in plan['preview']:del plan['preview'][stroke.OWNER]
    else:plan['preview'][stroke.OWNER]=plan['old_owner']


def discard(plan):
    graph=plan['new_graph']
    if graph and graph.users==0:bpy.data.node_groups.remove(graph)


def finish(plan):
    old=plan['old_graph']
    if plan['enabled'] and old and old.users==0:bpy.data.node_groups.remove(old)
