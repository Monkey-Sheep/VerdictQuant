param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required to build the Windows release."
}

uv sync --frozen --extra dev --extra release --python 3.12
if ($LASTEXITCODE -ne 0) {
    throw "Release dependencies could not be synchronized."
}

if (-not $SkipTests) {
    $env:QT_QPA_PLATFORM = "offscreen"
    uv run --frozen --extra dev pytest tests\unit
    if ($LASTEXITCODE -ne 0) {
        throw "Unit verification failed; release was not built."
    }
    uv run --frozen --extra dev pytest tests\integration tests\property tests\e2e -m "not live"
    if ($LASTEXITCODE -ne 0) {
        throw "Non-live system verification failed; release was not built."
    }
}

uv sync --frozen --extra release --python 3.12
if ($LASTEXITCODE -ne 0) {
    throw "Release-only environment could not be synchronized."
}

uv run --frozen --extra release pyinstaller --noconfirm --clean packaging\verdictquant.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$releaseDir = Join-Path $repoRoot "dist\VerdictQuant"
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination $releaseDir -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "README.md") -Destination $releaseDir -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "HARDENED_BUILD.md") -Destination $releaseDir -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "HEADLESS_AUTOMATION.md") -Destination $releaseDir -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "QUANT_PAPER_SYSTEM.md") -Destination $releaseDir -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "UPSTREAM_INTEGRATION.md") -Destination $releaseDir -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "THIRD_PARTY_NOTICES.md") -Destination $releaseDir -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "USER_GUIDE_CN.md") -Destination $releaseDir -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "scripts\verify_windows_release.ps1") -Destination (Join-Path $releaseDir "VERIFY_RELEASE.ps1") -Force
uv run --frozen --extra release python scripts\render_user_guide.py `
    (Join-Path $repoRoot "USER_GUIDE_CN.md") `
    (Join-Path $releaseDir "USER_GUIDE_CN.html")
if ($LASTEXITCODE -ne 0) {
    throw "The local HTML user guide could not be rendered."
}

$smokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("verdictquant-smoke-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $smokeRoot | Out-Null
$previousDb = $env:VERDICTQUANT_QUANT_DB
$previousConfig = $env:VERDICTQUANT_QUANT_CONFIG
try {
    $env:VERDICTQUANT_QUANT_DB = Join-Path $smokeRoot "paper.db"
    $env:VERDICTQUANT_QUANT_CONFIG = Join-Path $smokeRoot "config.json"
    & (Join-Path $releaseDir "VerdictQuantCLI.exe") --pretty doctor
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged CLI doctor smoke test failed."
    }
}
finally {
    $env:VERDICTQUANT_QUANT_DB = $previousDb
    $env:VERDICTQUANT_QUANT_CONFIG = $previousConfig
    $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $resolvedSmoke = [System.IO.Path]::GetFullPath($smokeRoot)
    if (
        $resolvedSmoke.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase) -and
        ([System.IO.Path]::GetFileName($resolvedSmoke)).StartsWith("verdictquant-smoke-")
    ) {
        Remove-Item -LiteralPath $resolvedSmoke -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$releasePrefix = [System.IO.Path]::GetFullPath($releaseDir).TrimEnd("\") + "\"
$manifestEntries = @(
    Get-ChildItem -LiteralPath $releaseDir -Recurse -File |
        Sort-Object FullName |
        ForEach-Object {
            $filePath = [System.IO.Path]::GetFullPath($_.FullName)
            if (-not $filePath.StartsWith(
                $releasePrefix,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                throw "Refusing to hash a file outside the release directory: $filePath"
            }
            [ordered]@{
                path = $filePath.Substring($releasePrefix.Length).Replace("\", "/")
                bytes = $_.Length
                sha256 = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash
            }
        }
)
$productVersion = (
    uv run --frozen --extra release python -c `
        "from pa_agent.brand import PRODUCT_VERSION; print(PRODUCT_VERSION)"
).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($productVersion)) {
    throw "VerdictQuant product version could not be resolved."
}
$manifest = [ordered]@{
    product = "VerdictQuant"
    version = $productVersion
    paper_only = $true
    live_execution = $false
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    files = $manifestEntries
}
$manifestPath = Join-Path $releaseDir "BUILD-MANIFEST.json"
$manifestTemp = Join-Path $releaseDir (
    ".BUILD-MANIFEST." + [guid]::NewGuid().ToString("N") + ".tmp"
)
[System.IO.File]::WriteAllText(
    $manifestTemp,
    ($manifest | ConvertTo-Json -Depth 8) + "`n",
    [System.Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $manifestTemp -Destination $manifestPath -Force
$sumLines = @(
    $manifestEntries | ForEach-Object { "$($_.sha256)  $($_.path)" }
)
$sumsPath = Join-Path $releaseDir "SHA256SUMS.txt"
$sumsTemp = Join-Path $releaseDir (
    ".SHA256SUMS." + [guid]::NewGuid().ToString("N") + ".tmp"
)
[System.IO.File]::WriteAllLines(
    $sumsTemp,
    $sumLines,
    [System.Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $sumsTemp -Destination $sumsPath -Force

$zipPath = Join-Path $repoRoot "dist\VerdictQuant-windows-x64.zip"
$zipTemp = Join-Path (Split-Path -Parent $zipPath) (
    ".VerdictQuant-windows-x64." + [guid]::NewGuid().ToString("N") + ".tmp.zip"
)
try {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::Open(
        $zipTemp,
        [System.IO.Compression.ZipArchiveMode]::Create
    )
    try {
        Get-ChildItem -LiteralPath $releaseDir -Recurse -File |
            Sort-Object FullName |
            ForEach-Object {
                $filePath = [System.IO.Path]::GetFullPath($_.FullName)
                if (-not $filePath.StartsWith(
                    $releasePrefix,
                    [System.StringComparison]::OrdinalIgnoreCase
                )) {
                    throw "Refusing to archive a file outside the release directory: $filePath"
                }
                $relativePath = $filePath.Substring($releasePrefix.Length).Replace("\", "/")
                $entryName = "VerdictQuant/" + $relativePath
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $archive,
                    $filePath,
                    $entryName,
                    [System.IO.Compression.CompressionLevel]::Optimal
                ) | Out-Null
            }
    }
    finally {
        if ($null -ne $archive) {
            $archive.Dispose()
        }
    }
    Move-Item -LiteralPath $zipTemp -Destination $zipPath -Force
}
finally {
    Remove-Item -LiteralPath $zipTemp -Force -ErrorAction SilentlyContinue
}

$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$zipSidecarPath = "$zipPath.sha256"
[System.IO.File]::WriteAllText(
    $zipSidecarPath,
    "$zipHash  $([System.IO.Path]::GetFileName($zipPath))`n",
    [System.Text.UTF8Encoding]::new($false)
)

& (Join-Path $repoRoot "scripts\verify_windows_release.ps1") `
    -ReleaseDir $releaseDir `
    -ZipPath $zipPath

Write-Host "Release created at: $releaseDir\VerdictQuant.exe"
Write-Host "AI/automation CLI: $releaseDir\VerdictQuantCLI.exe"
Write-Host "Standalone updater: $releaseDir\VerdictQuantUpdater.exe"
Write-Host "Shareable archive: $zipPath"
Write-Host "Archive SHA-256: $zipHash"
