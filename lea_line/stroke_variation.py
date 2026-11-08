"""Seeded stroke variation shared by prepared Freestyle and native live fields.

This reconstructs the controls, not the original private random generator.
Stroke ordering differs between Line Art and Freestyle.
"""
import colorsys
import numpy as np

FIELDS=('color_space_type','color_space_red','color_space_green','color_space_blue',
        'color_space_hue','color_space_saturation','color_space_value','stroke_size_random')


def snapshot(brush):
    ranges=tuple(float(getattr(brush,'color_space_'+name,0.)) for name in (
        ('hue','saturation','value') if getattr(brush,'color_space_type','RGB')=='HSV' else ('red','green','blue')))
    size=float(getattr(brush,'stroke_size_random',0.))/100.
    if not size and not any(ranges):return None
    return {'space':getattr(brush,'color_space_type','RGB'),'ranges':ranges,'size':size,
            'seed':int(getattr(brush,'random_seed',0))}


def random_value(index,seed,channel):
    # Every intermediate integer stays below 2**24 so native float math is exact.
    value=(int(index)%65521+int(seed)%65521+channel*1543+12345)%65521
    for multiplier,offset in ((109,127),(131,173)):
        value=(value*multiplier+offset)%65521
    return value/65521.


def factors(spec,index,color):
    offsets=[(2*random_value(index,spec['seed'],i)-1)*r for i,r in enumerate(spec['ranges'])]
    base=colorsys.rgb_to_hsv(*color) if spec['space']=='HSV' else color
    value=[base[i]+offsets[i] for i in range(3)]
    if spec['space']=='HSV':
        value[0]%=1.;value[1:]=np.clip(value[1:],0,1);value=colorsys.hsv_to_rgb(*value)
    else:value=np.clip(value,0,1)
    return value,1+(2*random_value(index,spec['seed'],3)-1)*spec['size']


def shader(spec):
    from freestyle.types import StrokeShader
    class Variation(StrokeShader):
        def __init__(self):super().__init__();self.index=0
        def shade(self,stroke):
            index=self.index;self.index+=1
            for point in stroke:
                color,size=factors(spec,index,tuple(point.attribute.color))
                if any(spec['ranges']):point.attribute.color=tuple(color)
                if spec['size']:point.attribute.thickness=tuple(max(0.,x*size) for x in point.attribute.thickness)
    return Variation()


def live(g,geometry,brush):
    spec=snapshot(brush)
    if spec is None:return geometry
    links=g.links
    def node(kind):return g.nodes.new(kind)
    def feed(value,socket):
        import bpy
        if isinstance(value,bpy.types.NodeSocket):links.new(value,socket)
        else:socket.default_value=value
    def math(op,a,b=0.):
        n=node('ShaderNodeMath');n.operation=op;feed(a,n.inputs[0]);feed(b,n.inputs[1]);return n.outputs[0]
    index=node('GeometryNodeInputIndex').outputs[0]
    capture=node('GeometryNodeCaptureAttribute');capture.domain='CURVE';capture.capture_items.clear();capture.capture_items.new('FLOAT','Stroke Index')
    links.new(geometry,capture.inputs['Geometry']);links.new(index,capture.inputs['Stroke Index']);geometry=capture.outputs['Geometry']
    def rnd(channel):
        value=math('MODULO',math('ADD',capture.outputs['Stroke Index'],spec['seed']%65521+channel*1543+12345),65521.)
        for m,b in ((109.,127.),(131.,173.)):value=math('MODULO',math('ADD',math('MULTIPLY',value,m),b),65521.)
        return math('SUBTRACT',math('MULTIPLY',math('DIVIDE',value,65521.),2.),1.)
    if any(spec['ranges']):
        source=node('GeometryNodeInputNamedAttribute');source.data_type='FLOAT_COLOR';source.inputs['Name'].default_value='vertex_color'
        choose=node('GeometryNodeSwitch');choose.input_type='RGBA'
        links.new(source.outputs['Exists'],choose.inputs['Switch']);choose.inputs['False'].default_value=(*brush.color,1.)
        links.new(source.outputs['Attribute'],choose.inputs['True'])
        split=node('FunctionNodeSeparateColor');split.mode=spec['space'];links.new(choose.outputs[0],split.inputs[0])
        combine=node('FunctionNodeCombineColor');combine.mode=spec['space'];combine.inputs['Alpha'].default_value=1.
        for i,amount in enumerate(spec['ranges']):
            value=math('ADD',split.outputs[i],math('MULTIPLY',rnd(i),amount))
            value=math('FRACT',math('ADD',value,1.)) if i==0 and spec['space']=='HSV' else math('MINIMUM',math('MAXIMUM',value,0.),1.)
            links.new(value,combine.inputs[i])
        store=node('GeometryNodeStoreNamedAttribute');store.data_type='FLOAT_COLOR';store.domain='POINT';store.inputs['Name'].default_value='vertex_color'
        links.new(geometry,store.inputs['Geometry']);links.new(combine.outputs[0],store.inputs['Value']);geometry=store.outputs[0]
    if spec['size']:
        radius=node('GeometryNodeInputRadius').outputs[0];set_radius=node('GeometryNodeSetCurveRadius')
        links.new(geometry,set_radius.inputs['Curve']);links.new(math('MULTIPLY',radius,math('ADD',1.,math('MULTIPLY',rnd(3),spec['size']))),set_radius.inputs['Radius']);geometry=set_radius.outputs[0]
    return geometry
