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
/* Optional CPU final line renderer. Buffers are detached, contiguous and
 * borrowed for the duration of the call. Indices must be valid for the input.
 * Prepare calls on distinct meshes may share one frame's atomic depth buffer.
 * Join all prepares before drawing/freeing. Output is premultiplied RGBA32F,
 * bottom row first. Rectangle coordinates are left, bottom, right, top.
 * Create/prepare return NULL on error; draw returns SIZE_MAX.
 */
typedef struct rea_cpu_frame rea_cpu_frame;
typedef struct rea_cpu_ink rea_cpu_ink;
typedef struct rea_cpu_style_v2 {
    uint32_t flags; /* 1 outline, 2 border, 4 crease, 8 reserve outline, 16 marked, 32 exclude marked */
    float min_cos, max_cos, radius, aa, rgba[4];
    const float *stamp; /* Optional tightly packed row-major coverage. Copied at create. */
    size_t stamp_width, stamp_height;
    float stamp_period;
} rea_cpu_style_v2;
typedef struct rea_cpu_mesh_v2 {
    const float (*vertices)[3];
    size_t vertex_count;
    const uint32_t (*triangles)[3];
    size_t triangle_count;
    const uint32_t (*edges)[2];
    size_t edge_count;
    const uint32_t *loop_edges, *loop_vertices;
    size_t loop_count;
    const uint32_t *sizes;
    const float (*normals)[3];
    size_t face_count;
    float projection[4][4], normal[3][3], eye[3];
    uint32_t perspective;
    float handedness;
    uint32_t owner;
    const uint32_t *style_ids;
    size_t style_count;
    const uint8_t *marks; /* Optional per-edge Freestyle selection marks. */
} rea_cpu_mesh_v2;
uint32_t rea_cpu_lines_abi_version(void);
rea_cpu_frame *rea_cpu_lines_create(size_t width, size_t height,
    const rea_cpu_style_v2 *styles, size_t count);
rea_cpu_ink *rea_cpu_lines_prepare(const rea_cpu_frame *frame, const rea_cpu_mesh_v2 *mesh);
size_t rea_cpu_lines_draw(const rea_cpu_frame *frame, const rea_cpu_ink *const *parts,
    size_t count, const size_t (*rect)[4], float (*output)[4]);
void rea_cpu_lines_free(rea_cpu_frame *frame);
void rea_cpu_lines_free_mesh(rea_cpu_ink *mesh);
#ifdef __cplusplus
}
#endif
#endif
