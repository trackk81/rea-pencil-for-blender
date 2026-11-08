"""Visible ownership for the opt-in mixed-brush live preview.

    0=no line, 1=material boundary, 2=marked edge, 3=crease.
Only the three categories measured in v35/v36 are modeled. Native Line Art
still owns visibility and occlusion. Call on evaluated, generated mesh copies
from Blender's main thread; never replace authored edge marks with this result.
"""
import math
import numpy as np

MATERIAL, MARKED, CREASE = 1, 2, 3


def _read_int_attribute(mesh,name,domain,values):
    """Read the native integer span when its schema matches the RNA view."""
    attribute=mesh.attributes.get(name)
    if attribute and attribute.domain==domain and attribute.data_type=='INT' and len(attribute.data)==len(values):
        attribute.data.foreach_get('value',values)
        return True
    return False


def edge_owners(mesh, matrix_world, crease_threshold, active=('material_boundary','edge_mark','crease'), *, backend='AUTO'):
    """Return disjoint owners and diagnostics without mutating Blender data.

    Crease convention follows the current preview's Line Art angle control.
    Smooth/sharp exceptions, nonmanifold edges and arbitrary hidden behavior
    remain unsupported. AUTO uses the optional Rust export for large meshes;
    absent/old DLLs and rejected numeric calls retain the NumPy path.
    """
    count=len(mesh.edges)
    if backend not in ('AUTO','NUMPY','RUST'):
        raise ValueError('backend must be AUTO, NUMPY or RUST')
    active=set(active)
    if active-{'material_boundary','edge_mark','crease'}:
        raise ValueError('Unsupported category in experimental ownership')
    threshold=float(crease_threshold)
    if not math.isfinite(threshold) or not 0<=threshold<=math.pi+1e-6:
        raise ValueError('Crease threshold must be finite and within0..pi')
    threshold=min(math.pi,threshold)  # Blender RNA stores pi rounded to float32.
    owners=np.zeros(count,dtype=np.uint8)
    marks=np.empty(count,dtype=np.bool_)
    mesh.edges.foreach_get('use_freestyle_mark',marks)
    loops=np.empty(len(mesh.loops),dtype=np.int32)
    if not _read_int_attribute(mesh,'.corner_edge','CORNER',loops):
        mesh.loops.foreach_get('edge_index',loops)
    totals=np.empty(len(mesh.polygons),dtype=np.int32)
    mesh.polygons.foreach_get('loop_total',totals)
    counts=np.bincount(loops,minlength=count)
    if np.any(counts>2):
        raise ValueError('Nonmanifold edge ownership is not verified')
    manifold=np.flatnonzero(counts==2)
    from . import native_core
    native_core._owner_engine='NUMPY'
    use_native=backend=='RUST' or backend=='AUTO' and count>=native_core.NATIVE_MIN_EDGES
    if use_native:
        function=native_core.ownership_function()
        if function is None and backend=='RUST':
            raise RuntimeError(native_core._owner_reason)
        if function is not None:
            materials=np.zeros(len(totals),np.int32)
            normals=np.zeros((len(totals),3),np.float64)
            flags=int('material_boundary' in active)|2*int('edge_mark' in active)
            if len(manifold):
                if (not _read_int_attribute(mesh,'material_index','FACE',materials)
                        or np.any(materials<0) or np.any(materials>32767)):
                    mesh.polygons.foreach_get('material_index',materials)
                if 'crease' in active:
                    raw=np.empty((len(totals),3),np.float32)
                    mesh.polygons.foreach_get('normal',raw.ravel())
                    matrix=np.asarray(matrix_world.to_3x3(),np.float64)
                    if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix))<1e-12:
                        raise ValueError('Singular object transform is not supported')
                    normals=raw@np.linalg.inv(matrix)
                    length=np.linalg.norm(normals,axis=1)
                    if np.any(length<=1e-12):
                        raise ValueError('Degenerate face normal ownership is not verified')
                    normals/=length[:,None]
                    flags|=4
            try:
                owners=native_core.edge_owner_buffer(marks,loops,totals,materials,normals,flags,
                                                     math.cos(math.pi-threshold)-1e-7)
            except RuntimeError:
                if backend=='RUST':raise
            else:
                return owners,{'edges':count,'manifold_edges':len(manifold),
                               'loose_edges':int(np.count_nonzero(counts==0)),
                               'boundary_edges':int(np.count_nonzero(counts==1)),
                               'owned_counts':{str(i):int(np.count_nonzero(owners==i)) for i in (MATERIAL,MARKED,CREASE)}}
    if len(manifold):
        faces=np.repeat(np.arange(len(totals),dtype=np.int32),totals)
        order=np.argsort(loops,kind='stable')
        starts=np.cumsum(counts)-counts
        first=faces[order[starts[manifold]]]
        second=faces[order[starts[manifold]+1]]
        materials=np.empty(len(totals),dtype=np.int32)
        if (not _read_int_attribute(mesh,'material_index','FACE',materials)
                or np.any(materials<0) or np.any(materials>32767)):
            mesh.polygons.foreach_get('material_index',materials)
        if 'crease' in active:
            normals=np.empty((len(totals),3),dtype=np.float32)
            mesh.polygons.foreach_get('normal',normals.ravel())
            matrix=np.asarray(matrix_world.to_3x3(),dtype=np.float64)
            if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix))<1e-12:
                raise ValueError('Singular object transform is not supported')
            normals=normals@np.linalg.inv(matrix)
            length=np.linalg.norm(normals,axis=1)
            if np.any(length<=1e-12):
                raise ValueError('Degenerate face normal ownership is not verified')
            normals/=length[:,None]
            dot=np.sum(normals[first]*normals[second],axis=1)
            owners[manifold[dot<math.cos(math.pi-threshold)-1e-7]]=CREASE
        if 'edge_mark' in active:owners[marks]=MARKED
        if 'material_boundary' in active:
            owners[manifold[materials[first]!=materials[second]]]=MATERIAL
    elif 'edge_mark' in active:
        owners[marks]=MARKED
    return owners,{'edges':count,'manifold_edges':len(manifold),'loose_edges':int(np.count_nonzero(counts==0)),
                   'boundary_edges':int(np.count_nonzero(counts==1)),
                   'owned_counts':{str(i):int(np.count_nonzero(owners==i)) for i in (MATERIAL,MARKED,CREASE)}}
