# rea pencil native core

The std-only Rust cdylib exposes a versioned C ABI for bulk material face masks.
`lea_line/native_core.py` copies Blender material indices into contiguous arrays,
then selects Rust for at least 4,096 faces or NumPy for smaller inputs/fallback.
Blender RNA extraction and writes remain on Blender's calling thread.

Build on Windows with the project-local Rust toolchain and installed MSVC:

```powershell
& ./native/build.ps1
```

The script runs offline unit tests, builds the release DLL with a static C runtime,
and copies the DLL and SHA-256 build manifest into `lea_line/native`.
No crate download is required. The manifest records the toolchain and source hash.

Blender integration checks are in `scripts/validate_native_core_v11.py` and
`scripts/validate_native_material_scope_v11.py`. `scripts/benchmark_native_core_v11.py`
measures both the isolated kernel and full temporary-mesh preparation. The v11
names identify the Rust work iteration shipped with add-on 0.10.0.

This kernel does not render strokes, access Blender objects, launch workers,
or improve every render by its isolated microbenchmark ratio.

The ownership extension shipped in0.20.4 in `src/ownership.rs` adds the optional
`rea_edge_owners_abi_version` and `rea_edge_owners_v1` exports. Material ABI1
and its error codes are unchanged. The header documents the separate ownership
input layout and errors. Old ABI1 DLLs remain usable for material masks;
ownership callers must detect missing exports and fall back to NumPy.

The preserved v57 candidate lives in `native/core_candidate_v57/release`.
Validation uses `validate_core_ownership_v57.py` and
`validate_core_material_v57.py`; actual dense render comparisons use
`benchmark_core_ownership_v57.py`. The0.20.4 Python loader enables this optional
export at2048edges and above, keeping NumPy below that threshold and for absent,
old or rejected DLL calls. Release package/Undo/save-load/source invalidation
checks are recorded in `artifacts/release_v59` and `package_validation_v59.json`.
