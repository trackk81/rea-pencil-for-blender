//! Optional edge ownership ABI v1. Detached arrays only; no Blender pointers or threads.
use std::{mem,panic,ptr,slice};

#[repr(C)]
#[derive(Clone,Copy)]
pub struct Input {
    edges:usize, loops:usize, faces:usize,
    marks:*const u8, loop_edges:*const i32, face_sizes:*const i32,
    materials:*const i32, normals:*const f64, flags:u32, cutoff:f64,
}
fn range<T>(p:*const T,n:usize)->Option<(usize,usize)> {
    if n==0{return Some((0,0));}
    let bytes=n.checked_mul(mem::size_of::<T>())?;
    let start=p as usize;
    if p.is_null() || start%mem::align_of::<T>()!=0 || bytes>isize::MAX as usize{return None;}
    Some((start,start.checked_add(bytes)?))
}
fn overlap(a:(usize,usize),b:(usize,usize))->bool {a.0<b.1 && b.0<a.1}
unsafe fn view<'a,T>(p:*const T,n:usize)->&'a [T] {if n==0{&[]}else{slice::from_raw_parts(p,n)}}

#[no_mangle]
pub extern "C" fn rea_edge_owners_abi_version()->u32 {1}

/// Caller supplies live detached buffers of the declared lengths. Output must
/// be distinct from input metadata and arrays. Error leaves output untouched.
/// 1=pointer/range,2=flags/cutoff,3=capacity,4=topology,5=nonmanifold,
/// 6=nonfinite normals,7=overlap,8=allocation,9=panic.
#[no_mangle]
pub unsafe extern "C" fn rea_edge_owners_v1(input:*const Input,out:*mut u8,capacity:usize)->i32 {
    let metadata=match range(input,1){Some(x)=>x,None=>return 1};
    let p=*input;
    if p.flags&!7!=0 || !p.cutoff.is_finite(){return 2;}
    if capacity<p.edges{return 3;}
    if p.edges==0{return if p.loops==0{0}else{4};}
    let output=match range(out.cast_const(),p.edges){Some(x)=>x,None=>return 1};
    let normal_len=match p.faces.checked_mul(3){Some(x)=>x,None=>return 1};
    let raw=[range(p.marks,p.edges),range(p.loop_edges,p.loops),range(p.face_sizes,p.faces),
             range(p.materials,if p.flags&1!=0{p.faces}else{0}),
             range(p.normals,if p.flags&4!=0{normal_len}else{0})];
    if overlap(output,metadata){return 7;}
    for item in raw {match item {None=>return 1,Some(r)=>if overlap(output,r){return 7}}}
    match panic::catch_unwind(|| {
        let marks=view(p.marks,p.edges);let loops=view(p.loop_edges,p.loops);let sizes=view(p.face_sizes,p.faces);
        let materials=view(p.materials,if p.flags&1!=0{p.faces}else{0});
        let normals=view(p.normals,if p.flags&4!=0{normal_len}else{0});
        if normals.iter().any(|v|!v.is_finite()){return 6;}
        let mut first=Vec::new();let mut second=Vec::new();let mut result=Vec::new();
        if first.try_reserve_exact(p.edges).is_err() || second.try_reserve_exact(p.edges).is_err() || result.try_reserve_exact(p.edges).is_err(){return 8;}
        first.resize(p.edges,usize::MAX);second.resize(p.edges,usize::MAX);result.resize(p.edges,0u8);
        let mut offset=0usize;
        for (face,&size) in sizes.iter().enumerate() {
            if size<0{return 4;}
            let end=match offset.checked_add(size as usize){Some(x) if x<=p.loops=>x,_=>return 4};
            for &edge in &loops[offset..end] {
                if edge<0 || edge as usize>=p.edges{return 4;}
                let e=edge as usize;
                if first[e]==usize::MAX{first[e]=face;}else if second[e]==usize::MAX{second[e]=face;}else{return 5;}
            }
            offset=end;
        }
        if offset!=p.loops{return 4;}
        for edge in 0..p.edges {
            let a=first[edge];let b=second[edge];let manifold=b!=usize::MAX;
            let mut owner=0;
            if manifold && p.flags&4!=0 {
                let a=a*3;let b=b*3;
                let dot=normals[a]*normals[b]+normals[a+1]*normals[b+1]+normals[a+2]*normals[b+2];
                if dot<p.cutoff{owner=3;}
            }
            if p.flags&2!=0 && marks[edge]!=0{owner=2;}
            if manifold && p.flags&1!=0 && materials[a]!=materials[b]{owner=1;}
            result[edge]=owner;
        }
        ptr::copy_nonoverlapping(result.as_ptr(),out,p.edges);0
    }) {Ok(code)=>code,Err(_)=>9}
}

