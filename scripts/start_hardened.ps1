$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install uv before starting the hardened build."
}

uv sync --frozen --python 3.12
if ($LASTEXITCODE -ne 0) {
    throw "Dependency synchronization failed."
}

uv run --frozen --python 3.12 python run.py
exit $LASTEXITCODE
