"""GPU list ranking for connected selected edges, including closed loops.

Mesh adjacency is prepared once on Blender's main thread. Per-view selection,
screen lengths, loop anchors and prefix lengths stay on the GPU. Branches split
chains; visibility gaps do not reset their phase. This is not Native chaining.
"""
import math
import numpy as np

_shader=None


def texture(data):
    import gpu
    data=np.asarray(data,dtype=np.float32).reshape(-1,4)
    n=max(1,len(data));w=min(4096,n);h=(n+w-1)//w
    padded=np.zeros((w*h,4),np.float32);padded[:len(data)]=data
    return gpu.types.GPUTexture((w,h),format='RGBA32F',data=gpu.types.Buffer('FLOAT',padded.size,padded.ravel()))


def topology(vertices,endpoints):
    """GPU vertex position/adjacency and endpoint indices, with stable IDs."""
    endpoints=np.asarray(endpoints,np.int32).reshape(-1,2);vertices=np.asarray(vertices,np.float32).reshape(-1,3)
    flat=endpoints.ravel();counts=np.bincount(flat,minlength=len(vertices))
    if counts.max(initial=0)>64:raise ValueError('GPU 곡선/점선: 한 꼭짓점에 64개 넘는 선이 연결된 경우 Native를 사용하세요')
    offsets=np.cumsum(np.r_[0,counts[:-1]+1]);order=np.argsort(flat,kind='stable')
    adjacency=np.zeros((len(vertices)+len(flat),4),np.float32)
    adjacency[offsets,0]=counts
    locations=np.arange(len(flat))+np.repeat(np.arange(len(vertices))+1,counts)
    adjacency[locations,0]=np.repeat(np.arange(len(endpoints)),2)[order]
    vertex_data=np.zeros((len(vertices),4),np.float32);vertex_data[:,:3]=vertices;vertex_data[:,3]=offsets
    edge_data=np.zeros((len(endpoints),4),np.float32);edge_data[:,:2]=endpoints
    return vertex_data,edge_data,adjacency


SOURCE=r'''
vec4 readValue(sampler2D tex,int index){int w=textureSize(tex,0).x;return texelFetch(tex,ivec2(index%w,index/w),0);}
vec4 edge(int e,int slot){return readValue(edgeData,e*4+slot);}
bool selected(int e){
 vec4 a=edge(e,0),b=edge(e,1);
 if(a.w>.5)return true;
 if(b.w<.5)return false;
 vec3 eye=viewportInfo.w>.5?eyeInfo.xyz-a.xyz:directionInfo.xyz;
 bool front=dot(edge(e,2).xyz,eye)>=0.0,back=dot(edge(e,3).xyz,eye)<0.0;
 int count=int(edge(e,3).w);
 if(count>2){front=false;back=false;int start=int(edge(e,2).w);
  for(int i=0;i<count;i++){float d=dot(readValue(extraNormals,start+i).xyz,eye);front=front||d>=0.0;back=back||d<0.0;}
 }else{return front==back;}
 return front&&back;
}
float edgeLength(int e){
 vec4 p=viewProjection*vec4(edge(e,0).xyz,1),q=viewProjection*vec4(edge(e,1).xyz,1);
 float a=p.z+p.w,b=q.z+q.w;if(a<=0.0&&b<=0.0)return 0.0;
 if(a<=0.0)p=mix(p,q,a/(a-b));else if(b<=0.0)q=mix(q,p,b/(b-a));
 return length((p.xy/p.w-q.xy/q.w)*viewportInfo.xy*.5);
}
vec4 initial(int halfEdge,bool cut,float anchor){
 int e=halfEdge/2,side=halfEdge%2;
 if(!selected(e)||edgeLength(e)<.00001)return vec4(-1,0,-1,-1);
 ivec2 ends=ivec2(readValue(topologyData,e).xy);
 int start=side==0?ends.x:ends.y,far=side==0?ends.y:ends.x;
 vec4 world=readValue(vertexData,far);vec4 clip=viewProjection*vec4(world.xyz,1);
 int next=-1,count=0,offset=int(world.w),degree=int(readValue(adjacencyData,offset).x);
 if((!cut||far!=int(anchor))&&clip.z+clip.w>0.0){
  for(int i=0;i<degree;i++){int neighbor=int(readValue(adjacencyData,offset+1+i).x);
   if(neighbor!=e&&selected(neighbor)&&edgeLength(neighbor)>.00001){
    ivec2 v=ivec2(readValue(topologyData,neighbor).xy);next=neighbor*2+(v.x==far?0:1);count++;
   }
  }
 }
 if(count!=1)next=-1;
 vec4 split=readValue(styleData,6);
 if(next>=0&&split.x>.5){
  ivec2 neighbor=ivec2(readValue(topologyData,next/2).xy);int other=neighbor.x==far?neighbor.y:neighbor.x;
  vec4 a=viewProjection*vec4(readValue(vertexData,start).xyz,1),b=viewProjection*vec4(world.xyz,1),c=viewProjection*vec4(readValue(vertexData,other).xyz,1);
  vec2 incoming=(b.xy/max(b.w,.00001)-a.xy/max(a.w,.00001))*viewportInfo.xy;
  vec2 outgoing=(c.xy/max(c.w,.00001)-b.xy/max(b.w,.00001))*viewportInfo.xy;
  if(dot(normalize(incoming),normalize(outgoing))<cos(split.y))next=-1;
 }
 return vec4(float(next),edgeLength(e),float(far),cut?float(start):float(min(start,far)));
}
void main(){
 int id=int(gl_GlobalInvocationID.x),mode=counts.y;
 int limit=mode==5?counts.x:counts.x*2;if(id>=limit)return;
 vec4 value;
 if(mode==1)value=initial(id,false,0.0);
 else if(mode==2||mode==4){
  value=readValue(metaInput,id);int next=int(value.x);
  if(next>=0){vec4 tail=readValue(metaInput,next);value=vec4(tail.x,value.y+tail.y,tail.z,mode==2?min(value.w,tail.w):tail.w);}
 }else if(mode==3){
  vec4 prior=readValue(metaInput,id);
  value=prior.x<0.0?prior:initial(id,true,prior.w);
 }else{
  vec4 forward=readValue(metaInput,id*2),reverse=readValue(metaInput,id*2+1);
  float len=edgeLength(id),total=forward.y+reverse.y-len;
  bool direct=reverse.z<forward.z||(reverse.z==forward.z&&reverse.w<forward.w);
  vec4 options=readValue(styleData,7);
  if(options.x>.5&&reverse.z!=forward.z&&reverse.z>=0.0&&forward.z>=0.0){
   vec4 a=viewProjection*vec4(readValue(vertexData,int(reverse.z)).xyz,1);
   vec4 b=viewProjection*vec4(readValue(vertexData,int(forward.z)).xyz,1);
   vec2 axis=vec2(cos(options.y),sin(options.y));
   direct=dot(a.xy/max(a.w,.00001)*viewportInfo.xy,axis)<dot(b.xy/max(b.w,.00001)*viewportInfo.xy,axis);
  }
  float begin=direct?reverse.y-len:forward.y;
  value=vec4(max(0.0,begin),max(0.0,begin+(direct?len:-len)),max(total,len),1);
 }
 int w=imageSize(metaOutput).x;imageStore(metaOutput,ivec2(id%w,id/w),value);
}
'''


def resources():
    global _shader
    if _shader:return _shader
    import gpu
    info=gpu.types.GPUShaderCreateInfo();info.local_group_size(64,1,1)
    for slot,name in enumerate(('edgeData','extraNormals','topologyData','vertexData','adjacencyData','metaInput','styleData')):info.sampler(slot,'FLOAT_2D',name)
    info.image(0,'RGBA32F','FLOAT_2D','metaOutput',qualifiers={'WRITE'})
    for kind,name in (('MAT4','viewProjection'),('VEC4','viewportInfo'),('VEC4','eyeInfo'),('VEC4','directionInfo'),('IVEC4','counts')):info.push_constant(kind,name)
    info.compute_source(SOURCE);_shader=gpu.shader.create_from_info(info);return _shader


def prepare(group):
    vertex,edge,adj=topology(group['chain_vertices'],group['chain_endpoints'])
    group['chain_vertex_texture']=texture(vertex);group['chain_edge_texture']=texture(edge);group['chain_adjacency_texture']=texture(adj)
    import gpu
    n=len(group['records'])*2;w=min(4096,max(1,n));h=max(1,(n+w-1)//w)
    group['chain_ping']=[gpu.types.GPUTexture((w,h),format='RGBA32F') for _ in range(2)]
    n=len(group['records']);w=min(4096,max(1,n));h=max(1,(n+w-1)//w)
    group['chain_views']={};group['chain_result_size']=(w,h)


def update(group,projection,viewport,eye,direction,area_id=0):
    import gpu
    n=len(group['records'])
    if not n:return
    if 'chain_ping' not in group:prepare(group)
    signature=tuple(v for row in projection for v in row)+tuple(viewport)+tuple(eye)+tuple(direction)
    view=group['chain_views'].get(area_id)
    if view is None:
        view={'texture':gpu.types.GPUTexture(group['chain_result_size'],format='RGBA32F'),'signature':None};group['chain_views'][area_id]=view
    group['chain_texture']=view['texture']
    if view['signature']==signature:return
    shader=resources();shader.bind()
    shader.uniform_float('viewProjection',projection);shader.uniform_float('viewportInfo',viewport)
    shader.uniform_float('eyeInfo',eye);shader.uniform_float('directionInfo',direction)
    for name,key in (('edgeData','texture'),('extraNormals','normal_texture'),('topologyData','chain_edge_texture'),('vertexData','chain_vertex_texture'),('adjacencyData','chain_adjacency_texture'),('styleData','style_texture')):shader.uniform_sampler(name,group[key])
    ping=group['chain_ping'];current=0;steps=max(1,math.ceil(math.log2(max(2,2*n))))
    def dispatch(mode,source,target,count):
        shader.uniform_sampler('metaInput',source);shader.image('metaOutput',target);shader.uniform_int('counts',(n,mode,0,0));gpu.compute.dispatch(shader,(count+63)//64,1,1)
    dispatch(1,group['texture'],ping[0],2*n)
    for _ in range(steps):dispatch(2,ping[current],ping[1-current],2*n);current=1-current
    dispatch(3,ping[current],ping[1-current],2*n);current=1-current
    for _ in range(steps):dispatch(4,ping[current],ping[1-current],2*n);current=1-current
    dispatch(5,ping[current],group['chain_texture'],n)
    view['signature']=signature;group['chain_updates']=group.get('chain_updates',0)+1


def reset():
    global _shader
    _shader=None
