"""Optional Rust bulk kernels with a NumPy fallback and a stable C ABI.

The Rust DLL receives owned contiguous arrays, never Blender RNA pointers.
ctypes.CDLL releases the GIL for its call; Blender extraction/writes stay on the
calling Blender thread. Loading failure or a rejected AUTO call falls back to
NumPy. Explicit backend='RUST' is strict so tests cannot silently benchmark it.
"""
import ctypes
from pathlib import Path
import platform
import sys

import numpy as np

ABI_VERSION = 1
NATIVE_MIN_FACES = 4096
NATIVE_MIN_EDGES = 2048
_library = None
_attempted = False
_reason = 'Not loaded'
_path = None
_last_engine = None
_owner_library = None
_owner_function = None
_owner_reason = 'Not loaded'
_owner_engine = None


class _OwnerInput(ctypes.Structure):
    _fields_ = [('edges', ctypes.c_size_t), ('loops', ctypes.c_size_t), ('faces', ctypes.c_size_t),
                ('marks', ctypes.POINTER(ctypes.c_uint8)),
                ('loop_edges', ctypes.POINTER(ctypes.c_int32)),
                ('face_sizes', ctypes.POINTER(ctypes.c_int32)),
                ('materials', ctypes.POINTER(ctypes.c_int32)),
                ('normals', ctypes.POINTER(ctypes.c_double)),
                ('flags', ctypes.c_uint32), ('cutoff', ctypes.c_double)]


def ownership_function():
    """Resolve the optional extension without disabling old material ABI1 DLLs."""
    global _owner_library, _owner_function, _owner_reason
    library = _load()
    if library is None:
        _owner_reason = _reason
        return None
    if library is _owner_library:
        return _owner_function
    _owner_library, _owner_function = library, None
    try:
        version = library.rea_edge_owners_abi_version
        version.argtypes = []
        version.restype = ctypes.c_uint32
        if version() != 1:
            raise RuntimeError('Unsupported ownership ABI')
        function = library.rea_edge_owners_v1
        function.argtypes = [ctypes.POINTER(_OwnerInput), ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
        function.restype = ctypes.c_int32
        _owner_function, _owner_reason = function, 'Available'
    except (OSError, AttributeError, RuntimeError) as error:
        _owner_reason = str(error)
    return _owner_function


def edge_owner_buffer(marks, loops, sizes, materials, normals, flags, cutoff):
    """Strict internal numeric bridge. Callers implement NumPy fallback.

    Validate actual owned array extents before constructing the C metadata.
    Never pass RNA memory or use this bridge to suppress authored-data errors.
    """
    global _owner_engine, _owner_reason
    marks = np.asarray(marks, dtype=np.bool_)
    if marks.ndim != 1:
        raise ValueError('Edge marks must be one-dimensional')
    marks = np.ascontiguousarray(marks)
    loops, sizes, materials = _indices(loops), _indices(sizes), _indices(materials)
    normals = np.ascontiguousarray(normals, dtype=np.float64)
    if flags not in range(8) or not np.isfinite(cutoff):
        raise ValueError('Invalid ownership flags or cutoff')
    if flags & 1 and len(materials) != len(sizes):
        raise ValueError('Ownership material buffer length mismatch')
    if flags & 4 and normals.shape != (len(sizes), 3):
        raise ValueError('Ownership normal buffer shape mismatch')
    function = ownership_function()
    if function is None:
        raise RuntimeError(_owner_reason)
    u8, i32, f64 = ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_double)
    view = _OwnerInput(len(marks), len(loops), len(sizes), marks.ctypes.data_as(u8),
                       loops.ctypes.data_as(i32), sizes.ctypes.data_as(i32),
                       materials.ctypes.data_as(i32), normals.ctypes.data_as(f64), flags, cutoff)
    output = np.empty(len(marks), dtype=np.uint8)
    code = function(ctypes.byref(view), output.ctypes.data_as(u8), len(output))
    if code:
        _owner_reason = f'Native edge ownership rejected buffers with error {code}'
        raise RuntimeError(_owner_reason)
    _owner_engine = 'RUST'
    _owner_reason = 'Available'
    return output


def _filename():
    if sys.platform == 'win32':
        return 'rea_pencil_core.dll'
    if sys.platform == 'darwin':
        return 'librea_pencil_core.dylib'
    return 'librea_pencil_core.so'


def _load():
    global _library, _attempted, _reason, _path
    if _attempted:
        return _library
    _attempted = True
    _path = Path(__file__).with_name('native') / _filename()
    if not _path.is_file():
        _reason = 'Optional native library is not installed'
        return None
    try:
        library = ctypes.CDLL(str(_path.resolve()))
        library.rea_core_abi_version.argtypes = []
        library.rea_core_abi_version.restype = ctypes.c_uint32
        version = library.rea_core_abi_version()
        if version != ABI_VERSION:
            raise RuntimeError(f'Native ABI {version} does not match {ABI_VERSION}')
        function = library.rea_material_face_mask_v1
        function.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t,
                             ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_uint32,
                             ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
        function.restype = ctypes.c_int32
        _library, _reason = library, 'Available'
    except (OSError, AttributeError, RuntimeError) as error:
        _library, _reason = None, str(error)
    return _library


def status():
    _load()
    return {'available': _library is not None, 'reason': _reason, 'abi_version': ABI_VERSION,
            'path': str(_path), 'machine': platform.machine(), 'minimum_faces': NATIVE_MIN_FACES,
            'last_engine': _last_engine}


def _indices(values):
    array = np.asarray(values)
    if array.ndim != 1:
        raise ValueError('Material indices must be a one-dimensional array')
    if array.size == 0:
        return np.empty(0, dtype=np.int32)
    if array.dtype.kind not in 'iu':
        raise TypeError('Material indices must be integers')
    if array.dtype != np.int32:
        limits = np.iinfo(np.int32)
        if array.min() < limits.min or array.max() > limits.max:
            raise ValueError('Material indices exceed the signed 32-bit ABI range')
    return np.ascontiguousarray(array, dtype=np.int32)


def _numpy_mask(indices, slots, allowed, explicit):
    result = np.zeros(len(indices), dtype=np.bool_)
    if not allowed:
        return result
    if explicit:
        result.fill(True)
    elif len(slots):
        valid = (indices >= 0) & (indices < len(slots))
        result[valid] = slots[indices[valid]]
    return result


def material_face_mask(indices, selected_slots, *, allowed=True, explicit=False, backend='AUTO'):
    """Return an owned bool mask for collection AND (object OR material face).

    selected_slots is a boolean sequence indexed by material slot. Invalid face
    indices select nothing. AUTO uses the optional DLL for sufficiently large
    buffers; NUMPY and RUST are explicit validation/benchmark choices.
    """
    global _last_engine, _reason
    if backend not in {'AUTO', 'NUMPY', 'RUST'}:
        raise ValueError('backend must be AUTO, NUMPY or RUST')
    indices = _indices(indices)
    slots = np.asarray(selected_slots, dtype=np.bool_)
    if slots.ndim != 1:
        raise ValueError('Selected slots must be a one-dimensional array')
    slots = np.ascontiguousarray(slots)
    use_native = backend == 'RUST' or backend == 'AUTO' and len(indices) >= NATIVE_MIN_FACES
    library = _load() if use_native else None
    if library is not None:
        result = np.empty(len(indices), dtype=np.bool_)
        flags = int(bool(allowed)) | (int(bool(explicit)) << 1)
        code = library.rea_material_face_mask_v1(
            indices.ctypes.data_as(ctypes.POINTER(ctypes.c_int32)), len(indices),
            slots.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)), len(slots), flags,
            result.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)), len(result))
        if code == 0:
            _last_engine = 'RUST'
            return result
        _reason = f'Native material mask rejected buffers with error {code}'
        if backend == 'RUST':
            raise RuntimeError(_reason)
    elif backend == 'RUST':
        raise RuntimeError(_reason)
    _last_engine = 'NUMPY'
    return _numpy_mask(indices, slots, bool(allowed), bool(explicit))


def mesh_material_mask(mesh, selected_slots, *, allowed=True, explicit=False, backend='AUTO'):
    """Extract Blender polygon indices once, then compute outside Blender RNA.

    This function does not modify mesh data or tags. Caller may bulk-assign the
    returned bool buffer to a generated/copy mesh's use_freestyle_mark field.
    """
    indices = np.empty(len(mesh.polygons), dtype=np.int32)
    if allowed and not explicit and len(selected_slots):
        mesh.polygons.foreach_get('material_index', indices)
    else:
        # Uniform cases never read material indices in either implementation.
        indices.fill(0)
    return material_face_mask(indices, selected_slots, allowed=allowed, explicit=explicit, backend=backend)
