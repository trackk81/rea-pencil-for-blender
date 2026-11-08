"""Default-off measured endpoint coverage using native curve fields only."""
from . import preview_coverage,preview_split as f
ENABLED='Endpoint Coverage Enabled'
def enabled(brush,scene):
    settings=scene.lea_line
    if not getattr(settings,'preview_endpoint_coverage',False) or settings.preview_simple_coverage:return False
    if getattr(brush,'caps','ROUND')!='ROUND' or not getattr(brush,'line_direction_enabled',False) or not getattr(brush,'size_curve_enabled',False):return False
    # SIZE takes precedence over an inert legacy taper flag in routing.
    flags=('dashed','alpha_curve_enabled','split_angle_enabled','distortion_enabled','distance_fade','distance_alpha')
    values=('min_line_length','extend','offset_x','offset_y','normal_offset')
    return (not any(getattr(brush,k,False) for k in flags) and not any(getattr(brush,k,0) for k in values)
            and getattr(brush,'loop_start','KEEP')=='KEEP' and getattr(brush,'loop_orientation','KEEP')=='KEEP')
def capture(graph,geometry,enabled_socket):
    # Anonymous CURVE eligibility is computed in projected pixels before
    # resampling or cap extension; false selects the original geometry directly.
    return preview_coverage.store_open(graph,geometry,enabled_socket)
def radius(graph,original,open_field,end_direction,enabled_socket):
    width=f._math(graph,'MULTIPLY',original,2.)
    thin=f._math(graph,'MULTIPLY',width,width)
    middle=f._math(graph,'SUBTRACT',f._math(graph,'MULTIPLY',width,2.),1.)
    thick=f._math(graph,'ADD',width,1.)
    profile=f._switch(graph,'FLOAT',f._math(graph,'LESS_THAN',width,2.),thick,middle)
    profile=f._switch(graph,'FLOAT',f._math(graph,'LESS_THAN',width,1.),profile,thin)
    delta=f._math(graph,'SUBTRACT',f._math(graph,'MULTIPLY',profile,.5),original)
    mask=f._math(graph,'MULTIPLY',f._math(graph,'ABSOLUTE',end_direction),open_field)
    adjusted=f._math(graph,'ADD',original,f._math(graph,'MULTIPLY',delta,f._math(graph,'MULTIPLY',mask,.5)))
    # A constant disabled selector bypasses the correction rather than merely
    # multiplying its result by zero, preserving the old radius input exactly.
    return f._switch(graph,'FLOAT',enabled_socket,original,adjusted)
