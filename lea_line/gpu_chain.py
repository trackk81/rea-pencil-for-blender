"""GPU list ranking for connected selected edges, including closed loops.

Mesh adjacency is prepared once on Blender's main thread. Per-view selection,
screen lengths, loop anchors and prefix lengths stay on the GPU. Branches split
chains. The optional depth pass also measures continuous visibility runs.
"""
import math
import numpy as np
from .gpu_depth import CENTER_DEPTH, MARKED_DEPTH, SPAN_LENGTH

_shader=None
MAX_WAVE_MASK_BYTES=2*1024*1024


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


EDGE_SELECTION = r'''
float objectFacing(float facing, float winding, float mode) {
    // An exactly edge-on face cannot supply the interior side of a contour.
    return mode!=0.0 && abs(winding)==1.0 && abs(facing)<1e-7 ? -1e-7 : facing;
}
float markNormalSide(vec4 firstFace,vec4 secondFace,vec3 toEye) {
    // Compare the two faces in the same view as edge selection. A small
    // tolerance keeps coplanar faces on their authored winding side.
    float difference=dot(secondFace.xyz-firstFace.xyz,toEye);
    return -(difference>1e-6*length(toEye)?secondFace.w:firstFace.w);
}

// silhouetteMode: +1 adds an Outline edge, -1 gives Outline precedence
// over this fallback category, and 0 uses only the static selection.
bool edgeSelected(float fixedEdge,float silhouetteMode,float normalStart,float normalCount,
                  vec3 toEye,float f0,float f1) {
    if (silhouetteMode==0.0 || (fixedEdge>.5 && silhouetteMode>0.0))
        return fixedEdge>.5;
    bool outline=(f0>=0.0)!=(f1>=0.0);
    if (normalCount>2.0) {
        bool front=false,back=false;
        int width=textureSize(extraNormals,0).x;
        for (int i=0;i<int(normalCount);i++) {
            int index=int(normalStart)+i;
            float facing=dot(texelFetch(extraNormals,ivec2(index%width,index/width),0).xyz,toEye);
            front=front || facing>=0.0; back=back || facing<0.0;
        }
        outline=front && back;
    }
    return silhouetteMode>0.0 ? outline : fixedEdge>.5 && !outline;
}
'''


SOURCE=EDGE_SELECTION+CENTER_DEPTH+MARKED_DEPTH+SPAN_LENGTH+r'''
vec4 readValue(sampler2D tex,int index){int w=textureSize(tex,0).x;return texelFetch(tex,ivec2(index%w,index/w),0);}
vec4 edge(int e,int slot){return readValue(edgeData,e*4+slot);}
bool selected(int e){
 vec4 a=edge(e,0),b=edge(e,1);
 if(b.w==0.0 || (a.w>.5 && b.w>0.0))return a.w>.5;
 vec3 eye=viewportInfo.w>.5?eyeInfo.xyz-a.xyz:directionInfo.xyz;
 vec4 n0=edge(e,2),n1=edge(e,3);
 float mode=readValue(styleData,6).z;
 float f0=objectFacing(dot(n0.xyz,eye),n1.w,mode);
 float f1=objectFacing(dot(n1.xyz,eye),n1.w,mode);
 return edgeSelected(a.w,b.w,n0.w,n1.w,eye,f0,f1);
}
bool projectEdge(int e,out vec4 p,out vec4 q){
 p=viewProjection*vec4(edge(e,0).xyz,1);q=viewProjection*vec4(edge(e,1).xyz,1);
 float a=p.z+p.w,b=q.z+q.w;if(a<=0.0&&b<=0.0)return false;
 if(a<=0.0)p=mix(p,q,a/(a-b));else if(b<=0.0)q=mix(q,p,b/(b-a));
 return true;
}
float edgeLength(int e){
 vec4 p,q;if(!projectEdge(e,p,q))return 0.0;
 return length((p.xy/p.w-q.xy/q.w)*viewportInfo.xy*.5);
}
vec4 waveMaskWord(int e,int word) {
 vec4 p,q;
 if(!projectEdge(e,p,q))return vec4(-1);
 vec2 a=p.xy/p.w*viewportInfo.xy*.5+viewportInfo.xy*.5;
 vec2 b=q.xy/q.w*viewportInfo.xy*.5+viewportInfo.xy*.5;
 vec2 d=b-a;
 float lengthValue=length(d);
 vec4 span=readValue(metaInput,e);
 // Endpoint runs already answer these edges without any interior march.
 if(lengthValue<64.0 || span.z+span.w>=lengthValue-.0001)return vec4(-1);
 int axis=abs(d.x)>=abs(d.y)?0:1;
 if(abs(d[axis])<1e-5 || any(lessThan(min(a,b),vec2(.01))) ||
    any(greaterThanEqual(max(a,b),viewportInfo.xy-.01)))return vec4(-1);
 vec4 planes=markedDepthPlanes(viewProjection,viewportInfo.xy,p,q,edge(e,1).xyz-edge(e,0).xyz,edge(e,2),edge(e,3));
 // The bilinear depth is continuous on this path. Keep the original search
 // for sloped planes, where incident-face correction can jump at a tap.
 if(any(notEqual(planes,vec4(0))))return vec4(-1);
 uint visible=0u,hidden=0u;
 vec2 depths=vec2(p.z/p.w,q.z/q.w)*.5+.5;
 for(int bit=0;bit<32;++bit){
  float at=float(word*32+bit)+.5;
  float t=clamp((at-a[axis])/d[axis],0.0,1.0);
  vec2 center=mix(a,b,t);
  float z=mix(depths.x,depths.y,t);
  float separation=z-centerDepth(center)-.000002;
  // Uncertain texels enter both masks. The original scan will refine them.
  if(separation<=.00000025)visible|=1u<<bit;
  if(separation>=-.00000025)hidden|=1u<<bit;
 }
 return vec4(float(visible&65535u),float(visible>>16),float(hidden&65535u),float(hidden>>16));
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
vec4 visibilityHead(int halfEdge,float minimum,bool marked,bool wave){
 vec4 link=initial(halfEdge,false,0.0);
 if(link.z<0.0)return vec4(-1,0,0,-1);
 vec4 p,q;projectEdge(halfEdge/2,p,q);
 if(halfEdge%2!=0){vec4 swap=p;p=q;q=swap;}
 vec2 origin=viewportInfo.xy*.5-vec2(counts.zw);
 vec4 points=vec4(p.xy/p.w,q.xy/q.w)*viewportInfo.xyxy*.5+origin.xyxy;
 vec2 depths=vec2(p.z/p.w,q.z/q.w)*.5+.5;
 vec4 planes=vec4(0);
 if(marked){
  int e=halfEdge/2;
  float traversal=halfEdge%2==0?1.0:-1.0;
  vec4 first=edge(e,2),second=edge(e,3);
  first.w*=traversal;second.w*=traversal;
  planes=markedDepthPlanes(viewProjection,viewportInfo.xy,p,q,(edge(e,1).xyz-edge(e,0).xyz)*traversal,first,second);
 }
 float head=spanLength(points,depths,link.y,0.0,1.0,minimum,readValue(styleData,5).x>1.5,planes,wave);
 return vec4(head>=link.y?link.x:-1.0,head,head,link.x);
}
vec4 visibilityResult(int edgeId){
 vec4 forward=readValue(metaInput,edgeId*2),reverse=readValue(metaInput,edgeId*2+1);
 // An unbroken visible cycle never reaches a terminal half-edge. Pointer
 // doubling otherwise sums many laps, giving every segment a different
 // wave origin. Reuse the selected chain's single-lap seam and distances.
 if(readValue(styleData,18).x!=0.0 && forward.x>=0.0 && reverse.x>=0.0){
  vec4 chain=readValue(chainPrefix,edgeId);
  bool direct=chain.x<chain.y;
  float before=direct?chain.x:chain.z-chain.x;
  float after=direct?chain.z-chain.y:chain.y;
  return vec4(max(0.0,before),max(0.0,after),forward.z,reverse.z);
 }
 // Adjacent lengths can only be reached from the matching end of this edge.
 float before=reverse.w>=0.0?readValue(metaInput,int(reverse.w)).y:0.0;
 float after=forward.w>=0.0?readValue(metaInput,int(forward.w)).y:0.0;
 return vec4(before,after,forward.z,reverse.z);
}
float preferredTie(float first,float second){
 return abs(second)>abs(first)?second:first;
}
vec4 directionHead(int halfEdge){
 vec4 head=visibilityHead(halfEdge,1e20,true,false);
 int e=halfEdge/2;
 vec3 toEye=viewportInfo.w>.5?eyeInfo.xyz-edge(e,0).xyz:directionInfo.xyz;
 float side=markNormalSide(edge(e,2),edge(e,3),toEye)*(halfEdge%2==0?1.0:-1.0);
 vec2 ends=readValue(topologyData,e).xy;
 // Quantize subpixel noise before summing so equal lengths remain ties.
 return vec4(head.x,side*round(head.y*1024.0),head.y>0.0?side*float(e+1):0.0,min(ends.x,ends.y));
}
vec4 cutDirectionLoop(int id,vec4 ranked){
 if(ranked.x<0.0){ranked.w=-1.0;return ranked;}
 vec4 head=readValue(directionHeads,id);
 vec2 ends=readValue(topologyData,id/2).xy;
 float far=id%2==0?ends.y:ends.x;
 if(far==ranked.w)head.x=-1.0;
 head.w=ranked.w;
 return head;
}
float directionVote(vec2 forward,vec2 reverse){
 float vote=forward.x-reverse.x;
 return sign(vote!=0.0?vote:preferredTie(forward.y,-reverse.y));
}
vec4 directionResult(int e){
 vec4 forward=readValue(metaInput,e*2),reverse=readValue(metaInput,e*2+1);
 ivec2 links=ivec2(readValue(jointData,e).xy);
 vec2 ends=readValue(topologyData,e).xy;
 // Each walk in a closed loop stops at the same seam. Do not add a
 // second lap when the starting endpoint already lies on that seam.
 vec2 atStart=links.x>=0 && ends.x!=forward.w?readValue(metaInput,links.x).yz:vec2(0);
 vec2 atEnd=links.y>=0 && ends.y!=reverse.w?readValue(metaInput,links.y).yz:vec2(0);
 float first=abs(readValue(directionHeads,e*2).y)/1024.0;
 float last=abs(readValue(directionHeads,e*2+1).y)/1024.0;
 return vec4(directionVote(forward.yz,atStart),directionVote(atEnd,reverse.yz),first,last);
}
bool hasWaveLoop(){
 return readValue(styleData,7).x>.5&&readValue(styleData,18).x!=0.0&&
        readValue(styleData,5).x<1.5;
}
int nextLoopHalf(int halfEdge){
 vec2 links=readValue(jointData,halfEdge/2).xy;
 return int(halfEdge%2==0?links.y:links.x);
}
vec2 loopPoint(int halfEdge){
 vec4 p=viewProjection*vec4(edge(halfEdge/2,halfEdge%2).xyz,1);
 return p.xy/p.w*viewportInfo.xy*.5;
}
vec2 loopStartKey(vec2 p,int policy){
 if(policy==2)return vec2(-p.x,p.y);
 if(policy==3)return p.yx;
 if(policy==4)return vec2(-p.y,p.x);
 return p;
}
void chooseWaveLoop(int id){
 vec4 rank=readValue(metaInput,id);
 if(rank.x<0.0)return;
 vec2 ends=readValue(topologyData,id/2).xy;
 int vertex=int(id%2==0?ends.x:ends.y);
 if(vertex!=int(rank.w))return;
 // The two half-edges leaving the canonical vertex describe the same loop.
 int other=nextLoopHalf(id^1);
 if(other<0||id>other)return;
 vec2 origin=loopPoint(id),lo=origin,hi=origin;
 float area=0.0;
 int cursor=id;
 int size=0;
 do {
  int next=nextLoopHalf(cursor);
  if(next<0||size>=counts.x)return;
  vec2 a=loopPoint(cursor),b=loopPoint(next);
  lo=min(lo,a);hi=max(hi,a);
  vec2 u=a-origin,v=b-origin;
  area+=u.x*v.y-u.y*v.x;
  cursor=next;size++;
 }while(cursor!=id);
 float angle=readValue(styleData,7).y;
 // Pencil+ weights the image-space direction by the projected loop bounds.
 vec2 axis=(hi-lo)*vec2(cos(angle),-sin(angle));
 int controls=int(readValue(styleData,18).w);
 int startPolicy=controls>>1;
 vec2 lowest=vec2(1e30);
 int selectedHalf=id;
 for(int i=0;i<size;i++){
  int next=nextLoopHalf(cursor);
  vec2 a=loopPoint(cursor)-origin,b=loopPoint(next)-origin;
  vec2 score=startPolicy==0?vec2(dot((a+b)*.5,axis),float(cursor/2)):
             loopStartKey(a,startPolicy);
  if(score.x<lowest.x||(score.x==lowest.x&&score.y<lowest.y)){
   lowest=score;selectedHalf=cursor;
  }
  cursor=next;
 }
 bool counterclockwise=(controls&1)!=0;
 bool reverse=(area>0.0)!=counterclockwise;
 int side=(selectedHalf%2)^int(reverse&&startPolicy==0);
 vec2 selectedEnds=readValue(topologyData,selectedHalf/2).xy;
 float seam=side==0?selectedEnds.x:selectedEnds.y;
 int width=imageSize(metaOutput).x;
 for(int i=0;i<size;i++){
  int e=cursor/2;
  imageStore(metaOutput,ivec2(e%width,e/width),vec4(seam,float((cursor%2)^int(reverse)),1,0));
  cursor=nextLoopHalf(cursor);
 }
}
void main(){
 int id=int(gl_GlobalInvocationID.x),mode=counts.y;
 if(mode==16){
  if(id<counts.x*2)chooseWaveLoop(id);
  return;
 }
 if(mode==15){
  int width=imageSize(metaOutput).x;
  if(id<counts.x)imageStore(metaOutput,ivec2(id%width,id/width),vec4(0));
  return;
 }
 if(mode==14){
  int width=imageSize(metaOutput).x,e=id/width;
  if(e<counts.x)imageStore(metaOutput,ivec2(id%width,e),waveMaskWord(e,id%width));
  return;
 }
 int limit=(mode==5||mode==8||mode==9||mode==12)?counts.x:counts.x*2;if(id>=limit)return;
 vec4 value;
 if(mode==1)value=initial(id,false,0.0);
 else if(mode==2||mode==4){
  value=readValue(metaInput,id);int next=int(value.x);
  if(next>=0){vec4 tail=readValue(metaInput,next);value=vec4(tail.x,value.y+tail.y,tail.z,mode==2?min(value.w,tail.w):tail.w);}
 }else if(mode==3){
  vec4 prior=readValue(metaInput,id);
  vec4 loop=hasWaveLoop()?readValue(waveLoop,id/2):vec4(0);
  value=prior.x<0.0?prior:initial(id,true,loop.z>.5?loop.x:prior.w);
 }else if(mode==9){
  // Keep the original selected links before list ranking changes them.
  value=vec4(readValue(metaInput,id*2+1).x,readValue(metaInput,id*2).x,0,0);
 }else if(mode==10)value=directionHead(id);
 else if(mode==11){
  value=readValue(metaInput,id);
  if(value.x>=0.0){
   vec4 tail=readValue(metaInput,int(value.x));
   value=vec4(tail.x,value.y+tail.y,preferredTie(value.z,tail.z),min(value.w,tail.w));
  }
 }else if(mode==12)value=directionResult(id);
 else if(mode==13)value=cutDirectionLoop(id,readValue(metaInput,id));
 else if(mode>=6){
  bool wave=readValue(styleData,18).x!=0.0;
  float minimum=wave?1e20:readValue(styleData,7).z;
  if(mode==6)value=visibilityHead(id,minimum,wave,wave);
  else if(mode==8)value=visibilityResult(id);
  else {
   value=readValue(metaInput,id);
   if(value.x>=0.0 && value.y<minimum){
    vec4 tail=readValue(metaInput,int(value.x));
    value.xy=vec2(tail.x,min(value.y+tail.y,minimum));
   }
  }
 }else{
  vec4 forward=readValue(metaInput,id*2),reverse=readValue(metaInput,id*2+1);
  float len=edgeLength(id),total=forward.y+reverse.y-len;
  bool direct=reverse.z<forward.z||(reverse.z==forward.z&&reverse.w<forward.w);
  vec4 options=readValue(styleData,7);
  bool wave=readValue(styleData,18).x!=0.0;
  bool open=reverse.z!=forward.z&&reverse.z>=0.0&&forward.z>=0.0;
  bool directedWave=wave&&open&&(options.x>.5||(viewportInfo.w>.5&&readValue(styleData,6).z==0.0))&&
      readValue(styleData,5).x<1.5;
  if((options.x>.5||wave)&&open){
   vec4 a=viewProjection*vec4(readValue(vertexData,int(reverse.z)).xyz,1);
   vec4 b=viewProjection*vec4(readValue(vertexData,int(forward.z)).xyz,1);
   vec2 axis=vec2(cos(options.y),sin(options.y));
   vec2 start=a.xy/max(a.w,.00001)*viewportInfo.xy;
   vec2 end=b.xy/max(b.w,.00001)*viewportInfo.xy;
   if(directedWave){
    // Pencil+ stores the direction in image coordinates with Y down.
    float angle=options.x>.5?options.y:-0.5235987756;
    float projection=dot(end-start,vec2(cos(angle),-sin(angle)));
    if(options.x>.5||abs(projection)>0.0001)direct=projection>0.0001;
   }else direct=wave ? (start.x==end.x ? start.y<end.y : start.x<end.x) : dot(start,axis)<dot(end,axis);
  }
  bool directedLoop=hasWaveLoop()&&readValue(waveLoop,id).z>.5;
  if(directedLoop)direct=readValue(waveLoop,id).y<.5;
  float begin=direct?reverse.y-len:forward.y;
  // A half-edge stops at this endpoint if its tail has only its own length.
  float ends=(reverse.y<=len?1.0:0.0)+(forward.y<=len?2.0:0.0);
  value=vec4(max(0.0,begin),max(0.0,begin+(direct?len:-len)),max(total,len),
             ends+((directedWave||directedLoop)?4.0:0.0));
 }
 int w=imageSize(metaOutput).x;imageStore(metaOutput,ivec2(id%w,id/w),value);
}
'''


def resources():
    global _shader
    if _shader:return _shader
    import gpu
    info=gpu.types.GPUShaderCreateInfo();info.local_group_size(64,1,1)
    for slot,name in enumerate(('edgeData','extraNormals','topologyData','vertexData','adjacencyData','metaInput','styleData','sceneDepth')):info.sampler(slot,'FLOAT_2D',name)
    # Preserve the draw shader's scene and object depth slots across compute.
    info.sampler(11,'FLOAT_2D','jointData')
    info.sampler(12,'FLOAT_2D','directionHeads')
    info.sampler(13,'FLOAT_2D','chainPrefix')
    info.sampler(14,'FLOAT_2D','waveLoop')
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


def _bind(group,projection,viewport,eye,direction,depth):
    shader=resources();shader.bind()
    shader.uniform_float('viewProjection',projection);shader.uniform_float('viewportInfo',viewport)
    shader.uniform_float('eyeInfo',eye);shader.uniform_float('directionInfo',direction)
    for name,key in (('edgeData','texture'),('extraNormals','normal_texture'),('topologyData','chain_edge_texture'),('vertexData','chain_vertex_texture'),('adjacencyData','chain_adjacency_texture'),('styleData','style_texture')):shader.uniform_sampler(name,group[key])
    shader.uniform_sampler('sceneDepth',depth)
    shader.uniform_sampler('jointData',group['joint_texture'])
    shader.uniform_sampler('directionHeads',group.get('direction_heads',group['normal_texture']))
    shader.uniform_sampler('chainPrefix',group['normal_texture'])
    shader.uniform_sampler('waveLoop',group['normal_texture'])
    return shader


def _dispatcher(shader,n,origin=(0,0)):
    import gpu
    def dispatch(mode,source,target,count):
        shader.uniform_sampler('metaInput',source);shader.image('metaOutput',target);shader.uniform_int('counts',(n,mode,*origin));gpu.compute.dispatch(shader,(count+63)//64,1,1)
    return dispatch


def tracks_joints(params):
    if params[6,2] == 0:
        return bool(params[7,3] or params[17,2])
    return bool(params[18,0])


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
    params=group['style']['params']
    directed_loops = bool(params[7,0] and params[18,0] and params[5,0] < 1.5)
    track_joints = tracks_joints(params) or directed_loops
    if track_joints and 'joints' not in view:
        view['joints']=gpu.types.GPUTexture(group['chain_result_size'],format='RGBA32F')
    group['joint_texture']=view.get('joints',view['texture'])
    if view['signature']==signature:return
    shader=_bind(group,projection,viewport,eye,direction,group['normal_texture'])
    ping=group['chain_ping'];current=0;steps=max(1,math.ceil(math.log2(max(2,2*n))))
    dispatch=_dispatcher(shader,n)
    dispatch(1,group['texture'],ping[0],2*n)
    if track_joints:dispatch(9,ping[0],view['joints'],n)
    for _ in range(steps):dispatch(2,ping[current],ping[1-current],2*n);current=1-current
    if directed_loops:
        if 'wave_loop' not in view:
            view['wave_loop']=gpu.types.GPUTexture(group['chain_result_size'],format='RGBA32F')
        loop=view['wave_loop']
        dispatch(15,ping[current],loop,n)
        dispatch(16,ping[current],loop,2*n)
        shader.uniform_sampler('waveLoop',loop)
    dispatch(3,ping[current],ping[1-current],2*n);current=1-current
    for _ in range(steps):dispatch(4,ping[current],ping[1-current],2*n);current=1-current
    dispatch(5,ping[current],group['chain_texture'],n)
    view['signature']=signature;group['chain_updates']=group.get('chain_updates',0)+1


def visibility_minimum(group):
    params=group['style']['params']
    # Outline and Object use an additional depth test that is not in this pass.
    return float(params[7,2]) if params[6,2]==0.0 else 0.0


def update_visibility(group,projection,viewport,eye,direction,depth,area_id,origin=None):
    import gpu
    view=group['chain_views'][area_id]
    # Preview snapshots own both the chain and the occluder geometry.
    signature=view['signature'] if origin is None else None
    if signature is not None and view.get('visibility_signature')==signature:
        return view['visibility']
    if 'visibility' not in view:
        view['visibility']=gpu.types.GPUTexture(group['chain_result_size'],format='RGBA32F')
    n=len(group['records'])
    shader=_bind(group,projection,viewport,eye,direction,depth)
    dispatch=_dispatcher(shader,n,origin or (0,0))
    ping=group['chain_ping'];current=0
    dispatch(6,group['texture'],ping[0],2*n)
    for _ in range(max(1,math.ceil(math.log2(max(2,2*n))))):
        dispatch(7,ping[current],ping[1-current],2*n);current=1-current
    shader.uniform_sampler('chainPrefix',group['chain_texture'])
    dispatch(8,ping[current],view['visibility'],n)
    width,height=int(viewport[0]),int(viewport[1])
    mask = None
    if (group['style']['params'][18,0] and (origin is None or origin==(0,0))
            and (depth.width,depth.height)==(width,height)):
        words=(max(width,height)+31)//32
        if 8<=n<=gpu.capabilities.max_texture_size_get() and words*n*16<=MAX_WAVE_MASK_BYTES:
            mask=view.get('wave_mask')
            if mask is None or (mask.width,mask.height)!=(words,n):
                mask=gpu.types.GPUTexture((words,n),format='RGBA32F')
            dispatch(14,view['visibility'],mask,words*n)
    view['wave_mask']=mask
    view['visibility_signature']=signature
    return view['visibility']


def update_normal_directions(group,projection,viewport,eye,direction,depth,area_id,cache=False):
    import gpu
    view=group['chain_views'][area_id]
    signature=view['signature'] if cache else None
    if signature is not None and view.get('normal_signature')==signature:
        return view['normal_directions']
    if 'normal_directions' not in view:
        view['normal_directions']=gpu.types.GPUTexture(group['chain_result_size'],format='RGBA32F')
    n=len(group['records'])
    ping=group['chain_ping']
    if 'direction_heads' not in group:
        group['direction_heads']=gpu.types.GPUTexture((ping[0].width,ping[0].height),format='RGBA32F')
    heads=group['direction_heads']
    shader=_bind(group,projection,viewport,eye,direction,depth)
    dispatch=_dispatcher(shader,n)
    # Do not bind the output head texture as an input during its write.
    shader.uniform_sampler('directionHeads',group['normal_texture'])
    dispatch(10,group['texture'],heads,2*n)
    shader.uniform_sampler('directionHeads',heads)
    steps=max(1,math.ceil(math.log2(max(2,2*n))))
    dispatch(11,heads,ping[0],2*n)
    current=0
    for _ in range(steps-1):
        dispatch(11,ping[current],ping[1-current],2*n);current=1-current
    dispatch(13,ping[current],ping[1-current],2*n);current=1-current
    for _ in range(steps):
        dispatch(11,ping[current],ping[1-current],2*n);current=1-current
    dispatch(12,ping[current],view['normal_directions'],n)
    view['normal_signature']=signature
    return view['normal_directions']


def reset():
    global _shader
    _shader=None
