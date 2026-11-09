"""Plan disjoint edge selections for per-category brushes.

This is an independent mapping to Freestyle, not Pencil+'s selection algorithm.
"""
FEATURES = (
    ('outline', 'silhouette'), ('border', 'border'),
    ('edge_mark', 'edge_mark'), ('material_boundary', 'material_boundary'),
    ('crease', 'crease'), ('ridge_valley', 'ridge_valley'),
    ('suggestive_contour', 'suggestive_contour'),
)
GPU_ONLY_EDGE_FEATURES = ('wireframe', 'smoothing_boundary', 'object_edge')
GPU_EDGE_BRUSH_PRIORITY = ('outline', 'object_edge', 'smoothing_boundary', 'material_boundary', 'edge_mark', 'crease', 'wireframe')


class ResolvedBrush:
    """Read-only effective brush values; never write linked settings to the ID."""
    def __init__(self, source, overrides):
        self.source = source
        self.overrides = overrides

    def __getattr__(self, name):
        if name in self.overrides:
            return self.overrides[name]
        return getattr(self.source, name)


def resolve_brush(brush):
    if isinstance(brush, ResolvedBrush):
        return brush
    overrides = {}
    detail_socket = brush.inputs.get('Brush Detail')
    if detail_socket and detail_socket.is_linked:
        detail = detail_socket.links[0].from_node
        if detail.bl_idname != 'LEABrushDetailNode':
            raise ValueError(f'{brush.name}: Brush Detail input requires a Brush Detail node')
        if detail.enabled:
            overrides['detail_source']=detail
            from .brush_detail import LEGACY_FIELDS, EXTRA_FIELDS
            overrides.update((name, getattr(detail, name)) for name in LEGACY_FIELDS + EXTRA_FIELDS)
            if detail.size_curve_enabled or detail.alpha_curve_enabled:
                from . import curves
                payload = curves.resolved_payload(detail)
                if detail.size_curve_enabled:
                    overrides['taper'] = True
                    overrides['size_curve_data'] = payload['SIZE']
                if detail.alpha_curve_enabled:
                    overrides['alpha_curve_data'] = payload['ALPHA']
    socket = brush.inputs.get('Distance Reduction')
    if socket and socket.is_linked:
        reduction = socket.links[0].from_node
        if reduction.bl_idname != 'LEADistanceReductionNode':
            raise ValueError(f'{brush.name}: Distance Reduction input requires a Distance Reduction node')
        overrides.update({
            'distance_fade': reduction.enabled and reduction.reduce_thickness,
            'distance_alpha': reduction.enabled and reduction.reduce_opacity,
            'distance_start': reduction.distance_start,
            'distance_end': reduction.distance_end,
            'far_thickness': reduction.far_thickness,
            'far_alpha': reduction.far_alpha,
            'distance_reference_enabled': reduction.enabled and getattr(reduction,'refer_object_on',False),
            'distance_reference_object': getattr(reduction,'object_reference',None),
        })
    if socket and socket.is_linked and reduction.enabled and getattr(reduction,'curve_enabled',False) and (reduction.reduce_thickness or reduction.reduce_opacity):
        from . import distance_curves
        overrides['distance_curve_data'],overrides['distance_curve_table']=distance_curves.resolve(reduction)
    return ResolvedBrush(brush, overrides) if overrides else brush


def feature_enabled(node, feature, visibility):
    """Resolve a feature without changing legacy shared visible/hidden selection."""
    if visibility == 'HIDDEN' and getattr(node, 'independent_hidden_edges', False):
        feature = 'hidden_' + feature
    return bool(getattr(node, feature, False))


def any_enabled(node, feature):
    """Whether an enabled visibility branch needs this geometric feature."""
    return any(enabled and feature_enabled(node, feature, visibility)
               for visibility, enabled in (('VISIBLE', node.visible), ('HIDDEN', node.hidden)))


def connected_brush(node, socket_name):
    socket = node.inputs.get(socket_name)
    if not socket or not socket.is_linked:
        return None
    source = socket.links[0].from_node
    if source.bl_idname != 'LEABrushNode':
        raise ValueError(f'{node.name}: {socket_name} requires a Brush node')
    return resolve_brush(source)


def line_brush(node, brush, visibility):
    from .independent_reduction import apply
    result=apply(node,brush,visibility)
    seed=getattr(node,'random_seed',0)
    values={}
    if seed:values['random_seed']=seed
    if visibility=='HIDDEN':
        from . import hidden_mask
        masks=hidden_mask.objects(node)
        if masks:values['hidden_mask_objects']=masks
    if getattr(node,'use_material_functions',False):
        if visibility=='HIDDEN' or any(feature_enabled(node,f,visibility) for f,_ in FEATURES if f not in {'outline','border'}):
            raise ValueError('Material Line Functions currently supports visible Outline/Open Border only')
        values['material_functions_enabled']=True
    return ResolvedBrush(result,values) if values else result


def jobs(node, common_brush, visibility):
    apply=line_brush
    base = connected_brush(node, 'Hidden Brush') if visibility == 'HIDDEN' else common_brush
    base = base or common_brush
    base = apply(node, base, visibility)
    active = [(feature, flag) for feature, flag in FEATURES if feature_enabled(node, feature, visibility)]
    overrides = {}
    for feature, flag in active:
        name = f'Hidden {feature} Brush' if visibility == 'HIDDEN' else f'{feature} Brush'
        selected = connected_brush(node, name)
        if selected:
            overrides[feature] = apply(node, selected, visibility)
    if not overrides:
        # Preserve one native line set and legacy IDs for the common case.
        return [{'suffix': '', 'brush': base, 'include': [flag for _, flag in active], 'exclude': []}]
    result = []
    previous = []
    for feature, flag in active:
        result.append({'suffix': ':' + feature, 'brush': overrides.get(feature, base),
                       'include': [flag], 'exclude': list(previous)})
        previous.append(flag)
    return result


def live_brush(node, common_brush, visibility, *, gpu=False):
    """Resolve one unambiguous edge category without splitting overlaps.

    Contour/border grouping and multi-category precedence need separate geometry
    support. Keep the existing common-brush fallback for those cases.
    """
    base = connected_brush(node, 'Hidden Brush') if visibility == 'HIDDEN' else common_brush
    base = base or common_brush
    apply=line_brush
    base = apply(node, base, visibility)
    active = [feature for feature, _ in FEATURES if feature_enabled(node, feature, visibility)]
    supported = {'edge_mark', 'crease', 'material_boundary'}
    if gpu:
        active.extend(feature for feature in GPU_ONLY_EDGE_FEATURES if feature_enabled(node, feature, visibility))
        supported.update(GPU_EDGE_BRUSH_PRIORITY)
    if feature_enabled(node, 'intersection', visibility):
        active.append('intersection')
    prefix = 'Hidden ' if visibility == 'HIDDEN' else ''
    overrides = {feature: connected_brush(node, prefix + feature + ' Brush')
                 for feature in active}
    if len(active) == 1 and active[0] in supported:
        return apply(node, overrides[active[0]], visibility) if overrides[active[0]] else base, False
    return base, any(value is not None for value in overrides.values())


def gpu_edge_brushes(node, base, visibility, features, *, allow_border=False):
    """Split categories where the original overlap order is known."""
    active = {feature for feature in features if feature_enabled(node, feature, visibility)}
    priority = GPU_EDGE_BRUSH_PRIORITY + (('border',) if allow_border else ())
    if not active.issubset(priority):
        return []
    prefix = 'Hidden ' if visibility == 'HIDDEN' else ''
    result = []
    previous = []
    for feature in priority:
        if feature in active:
            specific = connected_brush(node, prefix + feature + ' Brush')
            brush = line_brush(node, specific, visibility) if specific is not None else base
            result.append((feature, brush, tuple(previous)))
            previous.append(feature)
    return result
