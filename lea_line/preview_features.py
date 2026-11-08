"""Opt-in visible mixed solid/size-curve brushes with transactional outputs.

Private capture scenes never survive a refresh, Undo or file operation. Stable
output/camera graph IDs are reused; unchanged updates perform no Blender writes.
"""
import json
import bpy
import numpy as np
from . import routing,backend,preview_material as private,preview_camera,preview_ownership,preview_batch
from .width_profile import live_coverage_width,live_coverage_supported

FEATURES=(('material_boundary',1),('edge_mark',2),('crease',3))
_cache={}


def clear(scene=None):
    for key in list(_cache):
        if scene is None or key[0]==scene.as_pointer():del _cache[key]


def prune(scene,desired):
    for key,value in list(_cache.items()):
        if key[0]==scene.as_pointer() and not set(value['keys']).issubset(desired):del _cache[key]


def _supported(brush):
    from . import stamp_brush
    if stamp_brush.active(brush):return False
    from . import stroke_variation
    if stroke_variation.snapshot(brush):return False
    from . import texture_maps
    if texture_maps.maps(brush):return False
    if getattr(brush,'caps','ROUND') not in {'ROUND','BUTT','SQUARE'}:return False
    if (getattr(brush,'taper',False) or getattr(brush,'size_curve_enabled',False)) and not getattr(brush,'line_direction_enabled',False):return False
    flags=('dashed','alpha_curve_enabled','distance_fade','distance_alpha',
           'split_angle_enabled','distortion_enabled')
    values=('min_line_length','extend','offset_x','offset_y','normal_offset')
    return (not any(getattr(brush,f,False) for f in flags) and not any(getattr(brush,f,0) for f in values)
            and getattr(brush,'loop_start','KEEP')=='KEEP' and getattr(brush,'loop_orientation','KEEP')=='KEEP')


def _stroke_style(brush,enabled):
    needed=enabled and (brush.caps!='ROUND' or getattr(brush,'taper',False) or getattr(brush,'size_curve_enabled',False))
    fields=('caps','taper','taper_start','taper_end','size_curve_enabled','line_direction_enabled','line_direction','thickness')
    payload={name:getattr(brush,name,None) for name in fields} if needed else {}
    if needed and getattr(brush,'size_curve_enabled',False):payload['size_curve_data']=brush.size_curve_data
    return bool(needed),json.dumps(payload,sort_keys=True)


class _FeatureEntry(private._Entry):
    def __init__(self,scene,preview,node,code,active,owners_cache=None):
        super().__init__(scene,preview);self.node=node;self.code=code;self.active=active
        self.owners_cache=owners_cache if owners_cache is not None else {}

    def _target_sets(self):
        explicit={i.object for i in self.node.objects if i.object}
        scope=set(self.node.source_collection.all_objects) if self.node.source_collection else None
        return explicit,scope

    def targeted(self,source):
        targets=getattr(self,'_capture_targets',None)
        explicit,scope=self._target_sets() if targets is None else targets
        if not explicit and scope is None and getattr(self.node,'target_mode','ALL')=='LISTED':
            return False
        return (scope is None or source in scope) and (not explicit or source in explicit)

    def capture(self,node,preview,geometry_dirty=False):
        # Capture is synchronous on Blender's main thread. Snapshot target
        # membership once for its mesh and face passes, never across refreshes.
        self._capture_targets=self._target_sets()
        try:
            return super().capture(node,preview,geometry_dirty=geometry_dirty)
        finally:
            self._capture_targets=None

    def _refresh_meshes(self,depsgraph):
        super()._refresh_meshes(depsgraph)
        for item in self.proxies.values():
            source,proxy=item['source'],item['proxy']
            if self.targeted(source):
                matrix=source.evaluated_get(depsgraph).matrix_world
                key=(source.as_pointer(),private._pointer(source.data),private._matrix(matrix),
                     float(self.node.crease_angle),tuple(self.active))
                owners=self.owners_cache.get(key)
                if owners is None or len(owners)!=len(proxy.data.edges):
                    owners,_=preview_ownership.edge_owners(proxy.data,matrix,self.node.crease_angle,self.active)
                    self.owners_cache[key]=owners
                mask=owners==self.code
            else:mask=np.zeros(len(proxy.data.edges),dtype=np.bool_)
            proxy.data.edges.foreach_set('use_freestyle_mark',mask);proxy.data.update()

    def face_mask(self,node,source,proxy,allowed,explicit,materials):
        return np.full(len(proxy.data.polygons),self.targeted(source),dtype=np.bool_)


def _sync(scene,node,common,index):
    """Return handled/key/warning result, or None for the legacy preview path."""
    from . import viewport
    active=[f for f,_ in routing.FEATURES if routing.feature_enabled(node,f,'VISIBLE')]
    if routing.feature_enabled(node,'intersection','VISIBLE'):active.append('intersection')
    if len(active)<2 or not any(routing.connected_brush(node,f+' Brush') for f in active):return None
    result={'handled':False,'keys':[],'warnings':[]}
    apply=routing.line_brush
    brushes={f:apply(node,routing.connected_brush(node,f+' Brush') or common,'VISIBLE') for f in active}
    reason=None
    if set(active)-{f for f,_ in FEATURES} or len(node.materials):
        reason='Mixed live brushes support marked edges, creases and material boundaries with object targets.'
    elif not all(_supported(b) for b in brushes.values()):
        reason='Mixed live brushes support solid strokes and directed size curves; dashed/alpha/distance/advanced effects use the common brush.'
    else:
        reason=private.unsupported_reason(scene,node)
        if not reason and any(m.show_viewport for o in scene.objects if o.type=='MESH' for m in o.modifiers):
            reason='Mixed live brushes do not yet support evaluated mesh modifiers.'
    if reason:
        result['warnings']=[reason+' Using the common-brush preview.'];return result
    owner=viewport._owner(scene);prefix=f'{index}:VISIBLE:feature:'
    old=[o for o in scene.objects if viewport._owned(o,owner) and str(o.get(viewport.KEY,'')).startswith(prefix)
         and private._output_owned(o)]
    token=(scene.as_pointer(),node.as_pointer(),index);created=[];pairs=[];metadata=[]
    # Keep fixed category slots, including an empty snapshot for an inactive
    # category. Recreating slots changed same-depth cap blending after toggles.
    ordered=[(f,code,brushes.get(f,common)) for f,code in FEATURES]
    stroke_styles={f:_stroke_style(b,f in active) for f,_,b in ordered}
    projection_key=json.dumps(preview_camera.signature(scene))
    private.subscribe(scene)
    private._busy+=1
    try:
        first=next((o for o in old if o.get(viewport.KEY)==prefix+ordered[0][0]),None)
        # Source geometry revisions come from the existing coalesced main-thread watcher.
        sig=(private.revision(scene),tuple(active),node.crease_angle,backend.width_scale(scene,node),bool(scene.lea_line.preview_simple_coverage),bool(scene.lea_line.preview_endpoint_coverage),
             tuple((f,float(b.thickness),float(b.alpha),tuple(b.color),stroke_styles[f]) for f,_,b in ordered),
             private._signature(scene,node,first) if first else None)
        cached=_cache.get(token)
        if cached and cached['signature']==sig and len(old)==len(ordered):
            result.update(handled=True,keys=cached['keys']);return result
        styles=[];keys=[]
        # One main-thread refresh has the same evaluated source geometry for
        # each category. Share classification, never proxy data or old frames.
        owners_cache={}
        for feature,code,brush in ordered:
            key=prefix+feature;keys.append(key)
            existed=next((o for o in scene.objects if viewport._owned(o,owner) and o.get(viewport.KEY)==key),None)
            output=viewport._object(scene,owner,key)
            if existed is None:created.append(output)
            material=output.data.materials[0]
            if material.grease_pencil is None:bpy.data.materials.create_gpencil_data(material)
            modifier=next(m for m in output.modifiers if m.type=='LINEART')
            metadata.append((modifier,modifier.opacity,modifier.thickness))
            modifier.target_layer=output.data.layers[0].name;modifier.target_material=material
            for flag in ('use_contour','use_crease','use_material','use_intersection','use_loose','use_shadow','use_light_contour'):
                setattr(modifier,flag,False)
            modifier.use_edge_mark=True;modifier.opacity=brush.alpha
            width=brush.thickness*backend.width_scale(scene, node)
            if scene.lea_line.preview_simple_coverage and live_coverage_supported(brush):width=live_coverage_width(width)
            modifier.thickness=max(1,min(200,round(width)));modifier.use_multiple_levels=False
            modifier.level_start=modifier.level_end=0;modifier.show_viewport=modifier.show_render=False
            if existed is None:preview_camera.sync(output,scene,width,viewport._set)
            entry=_FeatureEntry(scene,output,node,code,active,owners_cache=owners_cache)
            snapshot=None
            try:
                snapshot=entry.capture(node,output,geometry_dirty=True)
                for name in (private.TAG,private.OWNER,private.SOURCE):
                    if name in snapshot:del snapshot[name]
            finally:entry.close_private()
            snapshot[viewport.TAG]=True;snapshot[viewport.OWNER]=owner;snapshot[private.OUTPUT]=True
            pairs.append((output,snapshot))
            style={'preview':output}
            needed,stroke_key=stroke_styles[feature]
            stroke_key=projection_key+':'+str(backend.width_scale(scene,node))+':'+str(bool(scene.lea_line.preview_simple_coverage))+':'+str(bool(scene.lea_line.preview_endpoint_coverage))+':'+stroke_key
            if output.get('rea_mixed_stroke_style')!=stroke_key:
                style['stroke']={'brush':brush,'scene':scene,'scale':backend.width_scale(scene, node),'needed':needed}
            style['_stroke_key']=stroke_key
            if output.get('rea_mixed_projection')!=projection_key:style['projection']=scene
            if not viewport._same(material.grease_pencil.color[:3],tuple(brush.color)):style['color']=tuple(brush.color)
            graph=next(m.node_group for m in output.modifiers if preview_camera._owned(m))
            if not viewport._same(graph.nodes[preview_camera.PREFIX+'radius'].inputs[1].default_value,width*.5):style['width']=width
            styles.append(style)
        stroke_keys=[style.pop('_stroke_key') for style in styles]
        preview_batch.replace_snapshots(pairs,styles=styles)
        for (output,_),stroke_key in zip(pairs,stroke_keys):
            output['rea_mixed_stroke_style']=stroke_key;output['rea_mixed_projection']=projection_key
        _cache[token]={'keys':keys,'signature':(private.revision(scene),tuple(active),node.crease_angle,backend.width_scale(scene,node),bool(scene.lea_line.preview_simple_coverage),bool(scene.lea_line.preview_endpoint_coverage),
            tuple((f,float(b.thickness),float(b.alpha),tuple(b.color),stroke_styles[f]) for f,_,b in ordered),private._signature(scene,node,pairs[0][0]))}
        viewport._last_sync_stats['geometry_writes']+=len(pairs)
        result.update(handled=True,keys=keys)
    except Exception as error:
        for _,snapshot in pairs:
            if private._valid(snapshot):private._remove_output(snapshot)
        for modifier,opacity,thickness in metadata:
            try:modifier.opacity=opacity;modifier.thickness=thickness
            except ReferenceError:pass
        for output in created:viewport._remove_object(scene,output)
        result.update(handled=bool(old),keys=[o.get(viewport.KEY) for o in old],
                      warnings=[f'Mixed-brush update failed: {error}. '+('Previous valid ink retained.' if old else 'Using common-brush preview.')])
    finally:private._busy-=1
    return result


def sync(scene,node,common,index):
    """Commit geometry first, then guarded overlap order with link rollback.

    Failed geometry/style transactions retain their previous output and order.
    The cached no-op path still repairs order after a load or external relink.
    """
    result=_sync(scene,node,common,index)
    if result and result.get('handled') and not result.get('warnings'):
        from .preview_order import normalize
        try:
            normalize(scene,f'{index}:VISIBLE:feature:')
        except Exception:
            result['warnings'].append('Mixed brush overlap order could not be updated; previous ordering retained.')
    return result
