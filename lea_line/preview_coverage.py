"""Capture conservative open-chain eligibility before resampling/cap extension."""
from . import preview_split as f
NAME='.rea_coverage_open'

def store_open(graph,geometry,enabled):
    def node(kind,name):
        n=graph.nodes.new(kind);n.name=n.label='Coverage '+name;return n
    index=node('GeometryNodeInputIndex','Curve Index').outputs[0]
    first=node('GeometryNodePointsOfCurve','First Point')
    last=node('GeometryNodePointsOfCurve','Last Point')
    for n in (first,last):
        f._wire(graph,index,n.inputs['Curve Index'])
        f._wire(graph,index,n.inputs['Weights'])
    f._wire(graph,f._math(graph,'SUBTRACT',first.outputs['Total'],1,integer=True),last.inputs['Sort Index'])
    position=node('GeometryNodeInputPosition','Position').outputs[0]
    delta=f._vector(graph,'SUBTRACT',f._at(graph,position,first.outputs['Point Index'],'FLOAT_VECTOR'),f._at(graph,position,last.outputs['Point Index'],'FLOAT_VECTOR'))
    cyclic=node('GeometryNodeInputSplineCyclic','Cyclic').outputs[0]
    eligible=f._bool(graph,'AND',f._bool(graph,'NOT',cyclic),f._math(graph,'GREATER_THAN',f._vector(graph,'LENGTH',delta),1e-5))
    capture=node('GeometryNodeCaptureAttribute','Open Eligibility')
    capture.domain='CURVE'
    capture.capture_items.clear()
    capture.capture_items.new('BOOLEAN','Open')
    f._wire(graph,geometry,capture.inputs['Geometry'])
    f._wire(graph,eligible,capture.inputs['Open'])
    return f._switch(graph,'GEOMETRY',enabled,geometry,capture.outputs['Geometry']),capture.outputs['Open']

def factor(graph,enabled,amount,open_field):
    selected=f._switch(graph,'FLOAT',open_field,1,amount)
    return f._switch(graph,'FLOAT',enabled,1,selected)

def clear(graph,geometry):
    # Anonymous capture is released by native attribute propagation when unused.
    return geometry
