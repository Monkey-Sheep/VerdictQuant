param(
    [string]$ReleaseDir = $PSScriptRoot,
    [string]$ZipPath = ""
)

$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ReleaseDir).Path)
$prefix = $root.TrimEnd("\") + "\"
$utf8 = [System.Text.UTF8Encoding]::new($false)
$manifestPath = Join-Path $root "BUILD-MANIFEST.json"
$sumsPath = Join-Path $root "SHA256SUMS.txt"

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "BUILD-MANIFEST.json is missing."
}
if (-not (Test-Path -LiteralPath $sumsPath -PathType Leaf)) {
    throw "SHA256SUMS.txt is missing."
}
foreach ($requiredExecutable in @(
    "VerdictQuant.exe",
    "VerdictQuantCLI.exe",
    "VerdictQuantUpdater.exe"
)) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $requiredExecutable) -PathType Leaf)) {
        throw "$requiredExecutable is missing."
    }
}

$manifest = [System.IO.File]::ReadAllText($manifestPath, $utf8) | ConvertFrom-Json
if ($manifest.product -ne "VerdictQuant") {
    throw "Unexpected release product: $($manifest.product)"
}
if ([string]$manifest.version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Unexpected release version: $($manifest.version)"
}
if ($manifest.paper_only -ne $true -or $manifest.live_execution -ne $false) {
    throw "Release safety flags are invalid."
}

$expected = New-Object "System.Collections.Generic.List[string]"
foreach ($file in $manifest.files) {
    $normalized = ([string]$file.path).Replace("/", "\")
    $fullPath = [System.IO.Path]::GetFullPath((Join-Path $root $normalized))
    if (-not $fullPath.StartsWith(
        $prefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Manifest path escaped the release directory: $($file.path)"
    }
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "Manifest file is missing: $($file.path)"
    }
    $item = Get-Item -LiteralPath $fullPath
    if ($item.Length -ne [int64]$file.bytes) {
        throw "File size mismatch: $($file.path)"
    }
    $hash = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash
    if ($hash -ne [string]$file.sha256) {
        throw "SHA-256 mismatch: $($file.path)"
    }
    $expected.Add("$hash  $($file.path)") | Out-Null
}

$actual = @([System.IO.File]::ReadAllLines($sumsPath, $utf8))
if ($actual.Count -ne $expected.Count) {
    throw "SHA256SUMS.txt line count does not match the manifest."
}
for ($index = 0; $index -lt $expected.Count; $index++) {
    if ($actual[$index] -ne $expected[$index]) {
        throw "SHA256SUMS.txt mismatch at line $($index + 1)."
    }
}

$temporaryFiles = @(
    Get-ChildItem -LiteralPath $root -Recurse -Force -File |
        Where-Object { $_.Name -match '^\..*\.tmp(?:\.zip)?$' }
)
if ($temporaryFiles.Count -ne 0) {
    throw "Release contains temporary files."
}

if (-not [string]::IsNullOrWhiteSpace($ZipPath)) {
    $resolvedZip = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ZipPath).Path)
    $sidecarPath = "$resolvedZip.sha256"
    if (-not (Test-Path -LiteralPath $sidecarPath -PathType Leaf)) {
        throw "Archive SHA-256 sidecar is missing."
    }
    $sidecar = [System.IO.File]::ReadAllText($sidecarPath, $utf8).Trim()
    if ($sidecar -notmatch '^([0-9a-fA-F]{64})  (.+)$') {
        throw "Archive SHA-256 sidecar format is invalid."
    }
    if ($Matches[2] -ne [System.IO.Path]::GetFileName($resolvedZip)) {
        throw "Archive SHA-256 sidecar names a different file."
    }
    $actualZipHash = (Get-FileHash -LiteralPath $resolvedZip -Algorithm SHA256).Hash
    if ($actualZipHash -ne $Matches[1]) {
        throw "Archive SHA-256 does not match its sidecar."
    }
}

Write-Output "Release verified: version=$($manifest.version) files=$($expected.Count) paper_only=true"
