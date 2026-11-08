param([switch]$SkipTests)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$localCargo = Join-Path $projectRoot '.tools/cargo/bin/cargo.exe'
if (Test-Path -LiteralPath $localCargo) {
    $env:RUSTUP_HOME = Join-Path $projectRoot '.tools/rustup'
    $env:CARGO_HOME = Join-Path $projectRoot '.tools/cargo'
    $cargoCommand = $localCargo
} else {
    $cargoCommand = (Get-Command cargo -ErrorAction Stop).Source
}
$env:CARGO_TARGET_DIR = Join-Path $PSScriptRoot 'target'
# Bundle the MSVC CRT into this small optional library. Blender installations
# should not need a separate Visual C++ redistributable for our computation.
$env:RUSTFLAGS = (($env:RUSTFLAGS + ' -C target-feature=+crt-static').Trim())
$manifest = Join-Path $PSScriptRoot 'rea_pencil_core/Cargo.toml'
if (-not $SkipTests) {
    & $cargoCommand test --manifest-path $manifest --offline
    if ($LASTEXITCODE -ne 0) { throw 'Native unit tests failed' }
}
& $cargoCommand build --manifest-path $manifest --release --offline
if ($LASTEXITCODE -ne 0) { throw 'Native release build failed' }
$nativeOutput = Join-Path $projectRoot 'lea_line/native'
New-Item -ItemType Directory -Path $nativeOutput -Force | Out-Null
$dllSource = Join-Path $env:CARGO_TARGET_DIR 'release/rea_pencil_core.dll'
$dllOutput = Join-Path $nativeOutput 'rea_pencil_core.dll'
Copy-Item -LiteralPath $dllSource -Destination $dllOutput -Force
$buildInfo = [ordered]@{
    abi_version = 1
    target = 'x86_64-pc-windows-msvc'
    cargo = (& $cargoCommand --version | Out-String).Trim()
    rustc = (& (Join-Path (Split-Path $cargoCommand -Parent) 'rustc.exe') --version | Out-String).Trim()
    crt_linkage = 'static'
    dll_sha256 = (Get-FileHash -LiteralPath $dllOutput -Algorithm SHA256).Hash.ToLowerInvariant()
    rust_source_sha256 = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'rea_pencil_core/src/lib.rs') -Algorithm SHA256).Hash.ToLowerInvariant()
    rust_source_files_sha256 = [ordered]@{
        'src/lib.rs' = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'rea_pencil_core/src/lib.rs') -Algorithm SHA256).Hash.ToLowerInvariant()
        'src/ownership.rs' = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'rea_pencil_core/src/ownership.rs') -Algorithm SHA256).Hash.ToLowerInvariant()
        'include/rea_pencil_core.h' = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'rea_pencil_core/include/rea_pencil_core.h') -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    optional_exports = @('rea_edge_owners_abi_version', 'rea_edge_owners_v1')
    algorithms = @('collection scope AND explicit-object OR material-slot face mask', 'detached manifold edge ownership: material > marked > crease')
}
$buildInfo | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $nativeOutput 'build_info.json') -Encoding UTF8
Write-Output $buildInfo
