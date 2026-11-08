"""RGB-mean Screen map displacement; alpha is ignored as observed natively."""
import numpy as np

def normals(points,closed=None):
    points=np.asarray(points,dtype=float);count=len(points)
    if count<2:return np.zeros_like(points)
    repeated=np.linalg.norm(points[0]-points[-1])<1e-4
    if closed is None:closed=repeated
    before=np.roll(points,1,axis=0);after=np.roll(points,-1,axis=0)
    if closed and repeated and count>2:before[0]=points[-2];after[-1]=points[1]
    elif not closed:before[0]=points[0];after[-1]=points[-1]
    tangent=after-before;length=np.linalg.norm(tangent,axis=1);length=np.maximum(length,1e-20)
    normal=np.column_stack((-tangent[:,1],tangent[:,0]))/length[:,None]
    if closed:
        area=np.sum(points[:,0]*after[:,1]-points[:,1]*after[:,0])
        if area>0:normal*=-1
    return normal

def live(g,geometry,rgb,amount,fields,scene):
    from . import preview_stroke as f,preview_split as s,preview_loop as loop,preview_camera
    geometry=s._normalize_closed(g,geometry)
    split=g.nodes.new('FunctionNodeSeparateColor');g.links.new(rgb,split.inputs[0])
    luma=f._math(g,'DIVIDE','Distortion RGB Mean',f._math(g,'ADD','Distortion RGB Sum',f._math(g,'ADD','Distortion RG',split.outputs['Red'],split.outputs['Green']),split.outputs['Blue']),3.)
    relative=f._vector(g,'SUBTRACT','Distortion Camera Relative',fields['position'],fields['camera_location'])
    x=f._vector(g,'DOT_PRODUCT','Distortion Project X',relative,fields['right']);y=f._vector(g,'DOT_PRODUCT','Distortion Project Y',relative,fields['up'])
    if preview_camera.projection(scene)[1]:
        x=f._math(g,'DIVIDE','Distortion Perspective X',x,fields['depth']);y=f._math(g,'DIVIDE','Distortion Perspective Y',y,fields['depth'])
    index=g.nodes.new('GeometryNodeInputIndex').outputs[0];curve=g.nodes.new('GeometryNodeCurveOfPoint');g.links.new(index,curve.inputs['Point Index'])
    nextp=g.nodes.new('GeometryNodeOffsetPointInCurve');g.links.new(index,nextp.inputs['Point Index']);nextp.inputs['Offset'].default_value=1
    nx=s._at(g,x,nextp.outputs['Point Index'],'FLOAT');ny=s._at(g,y,nextp.outputs['Point Index'],'FLOAT')
    cross=f._math(g,'SUBTRACT','Distortion Area Cross',f._math(g,'MULTIPLY','Distortion Cross XY',x,ny),f._math(g,'MULTIPLY','Distortion Cross YX',y,nx))
    area=loop._accumulate(g,cross,curve.outputs['Curve Index'],'FLOAT').outputs['Total']
    sign=f._math(g,'SUBTRACT','Distortion Outward Sign',1.,f._math(g,'MULTIPLY','Distortion Winding Sign',f._math(g,'GREATER_THAN','Distortion CCW',area,0.),2.))
    cyclic=g.nodes.new('GeometryNodeInputSplineCyclic').outputs[0]
    choose=g.nodes.new('GeometryNodeSwitch');choose.input_type='FLOAT';choose.inputs['False'].default_value=1.
    g.links.new(cyclic,choose.inputs['Switch']);g.links.new(sign,choose.inputs['True'])
    tangent=g.nodes.new('GeometryNodeInputTangent').outputs[0]
    tx=f._vector(g,'DOT_PRODUCT','Distortion Tangent X',tangent,fields['right']);ty=f._vector(g,'DOT_PRODUCT','Distortion Tangent Y',tangent,fields['up'])
    if preview_camera.projection(scene)[1]:
        td=f._vector(g,'DOT_PRODUCT','Distortion Tangent Depth',tangent,fields['forward'])
        tx=f._math(g,'SUBTRACT','Distortion Perspective Tangent X',tx,f._math(g,'MULTIPLY','Distortion Perspective DX',x,td))
        ty=f._math(g,'SUBTRACT','Distortion Perspective Tangent Y',ty,f._math(g,'MULTIPLY','Distortion Perspective DY',y,td))
    normal=f._combine(g,'Distortion Screen Normal',f._math(g,'MULTIPLY','Distortion Normal X',ty,-1.),tx)
    normal=f._vector(g,'NORMALIZE','Distortion Unit Normal',normal)
    coordinates=g.nodes.new('ShaderNodeSeparateXYZ');g.links.new(normal,coordinates.inputs[0])
    strength=f._math(g,'MULTIPLY','Distortion Pixel Amount',luma,float(amount))
    strength=f._math(g,'MULTIPLY','Distortion World Amount',strength,fields['pixel_size']);strength=f._math(g,'MULTIPLY','Distortion Signed Amount',strength,choose.outputs[0])
    dx=f._math(g,'MULTIPLY','Distortion World X',coordinates.outputs['X'],strength);dy=f._math(g,'MULTIPLY','Distortion World Y',coordinates.outputs['Y'],strength)
    offset=f._vector(g,'ADD','Distortion World Offset',f._vector(g,'SCALE','Distortion Right',fields['right'],dx),f._vector(g,'SCALE','Distortion Up',fields['up'],dy))
    geometry=f._store(g,geometry,'.rea distortion source','FLOAT_VECTOR','POINT',fields['position'])
    geometry=f._store(g,geometry,'.rea distortion offset','FLOAT_VECTOR','POINT',offset)
    return f._position(g,'Apply Distortion Map',geometry,offset=offset)

def resample(g,geometry,pixel_size):
    from . import preview_stroke as f
    length=g.nodes.new('GeometryNodeSplineLength').outputs['Length']
    count=f._math(g,'ADD','Distorted Pixel Count',f._math(g,'CEIL','Distorted Pixel Round',f._math(g,'DIVIDE','Distorted Pixel Length',length,pixel_size)),1.)
    count=f._math(g,'MINIMUM','Distorted Sample Budget',count,16384.)
    node=g.nodes.new('GeometryNodeResampleCurve');node.mode='COUNT';g.links.new(geometry,node.inputs['Curve']);g.links.new(count,node.inputs['Count'])
    return node.outputs[0]
