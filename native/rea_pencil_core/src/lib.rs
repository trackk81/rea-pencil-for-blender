//! Pure bulk computations behind a Python-version-independent C ABI.
//!
//! Blender owns every source mesh. Callers pass detached contiguous buffers;
//! this crate never calls Blender, retains pointers, or creates worker threads.

use std::{mem, panic, ptr, slice};

// Optional exports are versioned independently; the material ABI stays at1.
mod ownership;
mod cpu_lines;

pub const ABI_VERSION: u32 = 1;
const ALLOWED: u32 = 1;
const EXPLICIT: u32 = 2;
const OK: i32 = 0;
const NULL_POINTER: i32 = 1;
const INVALID_FLAGS: i32 = 2;
const INVALID_BUFFER: i32 = 3;
const OUTPUT_TOO_SMALL: i32 = 4;
const INTERNAL_PANIC: i32 = 5;
const OVERLAPPING_BUFFER: i32 = 6;

#[no_mangle]
pub extern "C" fn rea_core_abi_version() -> u32 {
    ABI_VERSION
}

fn byte_range<T>(pointer: *const T, count: usize) -> Option<(usize, usize)> {
    let bytes = count.checked_mul(mem::size_of::<T>())?;
    if bytes > isize::MAX as usize {
        return None;
    }
    let start = pointer as usize;
    if start % mem::align_of::<T>() != 0 {
        return None;
    }
    Some((start, start.checked_add(bytes)?))
}

fn overlaps(left: (usize, usize), right: (usize, usize)) -> bool {
    left.0 < right.1 && right.0 < left.1
}

/// Compute `allowed && (explicit_object || selected_slots[material_index])`.
/// Negative/out-of-range indices are unselected. Nonzero slot bytes mean true.
/// The output contains exactly 0 or 1. No output is written on validation error.
///
/// Flags: bit 0 = collection scope allows this object, bit 1 = explicit object
/// target. Empty input accepts null pointers. Uniform results do not require
/// material indices or selected slots. Other callers must provide readable
/// buffers of the declared lengths and a distinct writable output allocation.
/// Error codes: 0 success, 1 null, 2 flags, 3 size/alignment/overflow,
/// 4 output capacity, 5 caught panic, 6 overlapping input/output.
///
/// # Safety
/// Non-null pointers must address live allocations of their stated lengths.
/// C cannot verify allocation ownership or concurrent mutation by another caller.
#[no_mangle]
pub unsafe extern "C" fn rea_material_face_mask_v1(
    indices: *const i32,
    face_count: usize,
    selected_slots: *const u8,
    slot_count: usize,
    flags: u32,
    output: *mut u8,
    output_capacity: usize,
) -> i32 {
    if flags & !(ALLOWED | EXPLICIT) != 0 {
        return INVALID_FLAGS;
    }
    if output_capacity < face_count {
        return OUTPUT_TOO_SMALL;
    }
    if face_count == 0 {
        return OK;
    }
    if output.is_null() {
        return NULL_POINTER;
    }
    let output_range = match byte_range(output.cast_const(), face_count) {
        Some(range) => range,
        None => return INVALID_BUFFER,
    };
    let uniform = flags & ALLOWED == 0 || flags & EXPLICIT != 0 || slot_count == 0;
    if uniform {
        let value = u8::from(flags & ALLOWED != 0 && flags & EXPLICIT != 0);
        ptr::write_bytes(output, value, face_count);
        return OK;
    }
    if indices.is_null() || selected_slots.is_null() {
        return NULL_POINTER;
    }
    let indices_range = match byte_range(indices, face_count) {
        Some(range) => range,
        None => return INVALID_BUFFER,
    };
    let slots_range = match byte_range(selected_slots, slot_count) {
        Some(range) => range,
        None => return INVALID_BUFFER,
    };
    if overlaps(output_range, indices_range) || overlaps(output_range, slots_range) {
        return OVERLAPPING_BUFFER;
    }
    // A panic must never unwind across ctypes' C call frame.
    match panic::catch_unwind(|| {
        let input = slice::from_raw_parts(indices, face_count);
        let slots = slice::from_raw_parts(selected_slots, slot_count);
        let result = slice::from_raw_parts_mut(output, face_count);
        for (&index, value) in input.iter().zip(result.iter_mut()) {
            *value = u8::from(index >= 0 && slots.get(index as usize).copied().unwrap_or(0) != 0);
        }
    }) {
        Ok(()) => OK,
        Err(_) => INTERNAL_PANIC,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(indices: &[i32], slots: &[u8], flags: u32) -> Vec<u8> {
        let mut result = vec![99; indices.len()];
        let code = unsafe { rea_material_face_mask_v1(indices.as_ptr(), indices.len(), slots.as_ptr(), slots.len(), flags, result.as_mut_ptr(), result.len()) };
        assert_eq!(code, OK);
        result
    }

    #[test]
    fn union_and_scope() {
        let indices = [-1, 0, 1, 2, 3, i32::MAX];
        assert_eq!(run(&indices, &[0, 1, 2], ALLOWED), [0, 0, 1, 1, 0, 0]);
        assert_eq!(run(&indices, &[0, 1], ALLOWED | EXPLICIT), [1; 6]);
        assert_eq!(run(&indices, &[1, 1], EXPLICIT), [0; 6]);
        assert_eq!(run(&indices, &[], ALLOWED), [0; 6]);
    }

    #[test]
    fn empty_and_uniform_need_no_input() {
        unsafe {
            assert_eq!(rea_material_face_mask_v1(ptr::null(), 0, ptr::null(), 0, 1, ptr::null_mut(), 0), OK);
            let mut output = [99; 7];
            assert_eq!(rea_material_face_mask_v1(ptr::null(), 7, ptr::null(), 0, 3, output.as_mut_ptr(), 7), OK);
            assert_eq!(output, [1; 7]);
        }
    }

    #[test]
    fn rejected_calls_leave_output_untouched() {
        let indices = [0, 1];
        let slots = [1, 0];
        let mut output = [99; 2];
        unsafe {
            assert_eq!(rea_material_face_mask_v1(indices.as_ptr(), 2, slots.as_ptr(), 2, 4, output.as_mut_ptr(), 2), INVALID_FLAGS);
            assert_eq!(rea_material_face_mask_v1(indices.as_ptr(), 2, slots.as_ptr(), 2, 1, output.as_mut_ptr(), 1), OUTPUT_TOO_SMALL);
            assert_eq!(rea_material_face_mask_v1(ptr::null(), 2, slots.as_ptr(), 2, 1, output.as_mut_ptr(), 2), NULL_POINTER);
            assert_eq!(rea_material_face_mask_v1(indices.as_ptr(), 2, slots.as_ptr(), 2, 1, ptr::null_mut(), 2), NULL_POINTER);
            assert_eq!(rea_material_face_mask_v1(indices.as_ptr(), 2, slots.as_ptr(), 2, 1, indices.as_ptr() as *mut u8, 2), OVERLAPPING_BUFFER);
            assert_eq!(rea_material_face_mask_v1(1_usize as *const i32, 2, slots.as_ptr(), 2, 1, output.as_mut_ptr(), 2), INVALID_BUFFER);
            assert_eq!(rea_material_face_mask_v1(indices.as_ptr(), usize::MAX, slots.as_ptr(), 2, 1, output.as_mut_ptr(), usize::MAX), INVALID_BUFFER);
        }
        assert_eq!(output, [99; 2]);
    }

    #[test]
    fn deterministic_large_input() {
        let mut seed = 0x12345678_u32;
        let indices: Vec<i32> = (0..100_000).map(|_| {
            seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
            (seed % 300) as i32 - 20
        }).collect();
        let slots: Vec<u8> = (0..257).map(|index| u8::from(index % 7 == 0)).collect();
        let expected: Vec<u8> = indices.iter().map(|&index| u8::from(index >= 0 && (index as usize) < slots.len() && slots[index as usize] != 0)).collect();
        assert_eq!(run(&indices, &slots, ALLOWED), expected);
    }
}
