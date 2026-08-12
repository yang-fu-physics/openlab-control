param(
    [string]$ReleaseRoot = "dist\OpenLabControl"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$resolvedReleaseRoot = (Resolve-Path -LiteralPath (Join-Path $projectRoot $ReleaseRoot)).Path
$expectedReleaseRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot "dist\OpenLabControl"))
if ($resolvedReleaseRoot -ne $expectedReleaseRoot) {
    throw "Unexpected release root: $resolvedReleaseRoot"
}

$mainExecutable = Join-Path $resolvedReleaseRoot "OpenLabControl.exe"
$scannerExecutable = Join-Path $resolvedReleaseRoot "InstrumentScanner.exe"
if (-not (Test-Path -LiteralPath $mainExecutable -PathType Leaf)) {
    throw "Missing packaged main executable: $mainExecutable"
}
if (-not (Test-Path -LiteralPath $scannerExecutable -PathType Leaf)) {
    throw "Missing packaged instrument scanner: $scannerExecutable"
}

# Both executables must stay beside the one shared PyInstaller _internal.
$releaseToolsPath = Join-Path $resolvedReleaseRoot "tools"
if (Test-Path -LiteralPath $releaseToolsPath) {
    throw "Release must not contain a tools directory: $releaseToolsPath"
}

$resourceDirectories = @(
    "configs",
    "examples",
    "docs",
    "templates",
    "integrations",
    "modules"
)
foreach ($directory in $resourceDirectories) {
    $source = Join-Path $projectRoot $directory
    $destination = Join-Path $resolvedReleaseRoot $directory
    if (Test-Path -LiteralPath $destination) {
        throw "Release resource already exists before staging: $destination"
    }
    Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
}

foreach ($file in @("README.md", "CHANGELOG.md", "SECURITY.md")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $file) -Destination $resolvedReleaseRoot -Force
}

foreach ($directory in @(
    "runs",
    "module_data",
    "wheels",
    "modules",
    "system_instruments",
    "runtime_packages",
    "trust_state"
)) {
    New-Item -ItemType Directory -Path (Join-Path $resolvedReleaseRoot $directory) -Force | Out-Null
}

$cacheNames = @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
foreach ($resourceDirectory in $resourceDirectories) {
    $stagedRoot = Join-Path $resolvedReleaseRoot $resourceDirectory
    Get-ChildItem -LiteralPath $stagedRoot -Directory -Recurse -Force |
        Where-Object { $_.Name -in $cacheNames } |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $stagedRoot -File -Recurse -Force |
        Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
        Remove-Item -Force
}

Write-Output "Staged Windows release: $resolvedReleaseRoot"
