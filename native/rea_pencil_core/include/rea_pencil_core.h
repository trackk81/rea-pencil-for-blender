#ifndef REA_PENCIL_CORE_H
#define REA_PENCIL_CORE_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
uint32_t rea_core_abi_version(void);
/* Flags: 1 = allowed by collection, 2 = explicit object target.
 * Error: 0 success, 1 null, 2 flags, 3 size/alignment/overflow,
 * 4 capacity, 5 caught panic, 6 overlapping input/output.
 * Output contains one byte (0 or 1) per face. All arrays are contiguous.
 * The caller owns storage; no pointers are retained after return.
 * Inputs must not overlap output and must remain readable during the call.
 * Zero faces accept null buffers. Uniform masks need no input buffers.
 */
int32_t rea_material_face_mask_v1(const int32_t *indices, size_t face_count,
    const uint8_t *selected_slots, size_t slot_count, uint32_t flags,
    uint8_t *output, size_t output_capacity);
/* Optional ownership extension; resolve dynamically for old ABI1 DLLs.
 * Do not pack this struct. size_t and pointer fields use the host ABI.
 * marks[edges], loop_edges[loops], face_sizes[faces], materials[faces],
 * normals[faces*3] are contiguous detached arrays. Normals are normalized
 * world-space float64 triples; flags1=material,2=marked,4=crease.
 * Faces are contiguous ranges in loop_edges with lengths face_sizes.
 * Material and normal arrays are required only when their flag is enabled.
 * Crease uses dot < cutoff. Priority material1 > marked2 > crease3;0=none.
 * Caller owns live allocations matching declared lengths; no concurrent
 * mutation, retained pointers, Blender RNA access or internal threads.
 * Output must not overlap any input or this metadata. Errors leave it intact.
 * Ownership errors (independent of material codes):1pointer/range,
 * 2flags/cutoff,3capacity,4topology,5nonmanifold,6nonfinite normals,
 * 7overlap,8allocation,9caught panic. Zero edges require zero loops.
 */
typedef struct rea_edge_owners_input_v1 {
    size_t edges, loops, faces;
    const uint8_t *marks;
    const int32_t *loop_edges, *face_sizes, *materials;
    const double *normals;
    uint32_t flags;
    double cutoff;
} rea_edge_owners_input_v1;
uint32_t rea_edge_owners_abi_version(void);
int32_t rea_edge_owners_v1(const rea_edge_owners_input_v1 *input,
    uint8_t *output, size_t output_capacity);
#ifdef __cplusplus
}
#endif
#endif
