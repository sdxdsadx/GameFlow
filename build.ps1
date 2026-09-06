param([switch]$SkipTests)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root
$python = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { throw 'Python Launcher (py.exe) was not found.' }

if (-not $SkipTests) {
    & $python -3 -m unittest discover -s tests -p 'test_*.py'
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
}
& $python -3 tools\portable_audit.py $root
if ($LASTEXITCODE -ne 0) { throw 'Portable audit failed.' }
$stagingRoot = Join-Path $root 'build\release-staging'
$resolvedStaging = [IO.Path]::GetFullPath($stagingRoot)
$resolvedBuild = [IO.Path]::GetFullPath((Join-Path $root 'build'))
if (-not $resolvedStaging.StartsWith($resolvedBuild, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe staging path: $resolvedStaging"
}
if (Test-Path -LiteralPath $resolvedStaging) {
    Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $resolvedStaging | Out-Null
& $python -3 -m PyInstaller --noconfirm --clean --onedir --name GameFlow `
    --distpath $resolvedStaging `
    --hidden-import win32api --hidden-import win32con --hidden-import win32gui `
    --hidden-import win32process --hidden-import psutil --hidden-import PIL.Image `
    --hidden-import cv2 --hidden-import numpy --hidden-import pyautogui main.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

$output = Join-Path $resolvedStaging 'GameFlow'
Copy-Item -LiteralPath (Join-Path $root 'README.md') -Destination $output -Force
Copy-Item -LiteralPath (Join-Path $root 'launch_admin.ps1') -Destination $output -Force
Copy-Item -LiteralPath (Join-Path $root 'start.bat') -Destination $output -Force
Copy-Item -LiteralPath (Join-Path $root 'start_hidden.vbs') -Destination $output -Force
New-Item -ItemType Directory -Force -Path (Join-Path $output 'config') | Out-Null
Copy-Item -LiteralPath (Join-Path $root 'config\workflow.json') -Destination (Join-Path $output 'config\workflow.json') -Force
Copy-Item -LiteralPath (Join-Path $root 'config\resource_manifest.json') `
    -Destination (Join-Path $output 'config\resource_manifest.json') -Force
& $python -3 tools\stage_resources.py (Join-Path $root 'resources') `
    (Join-Path $output 'resources') --manifest (Join-Path $root 'config\resource_manifest.json')
if ($LASTEXITCODE -ne 0) { throw 'Bundled resource staging failed.' }
& $python -3 tools\rebase_bundled_profiles.py $output
if ($LASTEXITCODE -ne 0) { throw 'Bundled profile rebasing failed.' }
$manifest = Get-Content -LiteralPath (Join-Path $root 'config\resource_manifest.json') -Raw | ConvertFrom-Json
$bundledVersions = [ordered]@{}
foreach ($include in $manifest.includes) {
    foreach ($required in $include.required) {
        $relative = Join-Path 'resources' (Join-Path $include.source $required)
        $resource = Get-Item -LiteralPath (Join-Path $output $relative)
        $bundledVersions[$relative.Replace('\\', '/')] = [ordered]@{
            file_version = $resource.VersionInfo.FileVersion
            product_version = $resource.VersionInfo.ProductVersion
            size = $resource.Length
        }
    }
}
$gitCommit = (& git -C $root rev-parse HEAD 2>$null)
$gitDirty = [bool]((& git -C $root status --porcelain 2>$null) | Select-Object -First 1)
$buildInfo = [ordered]@{
    build_id = (Get-Date -Format 'yyyyMMdd-HHmmss')
    built_at = (Get-Date).ToUniversalTime().ToString('o')
    git_commit = $gitCommit
    git_dirty = $gitDirty
    python_requirements = @(Get-Content -LiteralPath (Join-Path $root 'requirements.txt'))
    resource_manifest_version = $manifest.version
    bundled_resources = $bundledVersions
} | ConvertTo-Json -Depth 6
Set-Content -LiteralPath (Join-Path $output 'build_info.json') -Value $buildInfo -Encoding UTF8
& $python -3 tools\portable_audit.py $output
if ($LASTEXITCODE -ne 0) { throw 'Build output portable audit failed.' }
& (Join-Path $output 'GameFlow.exe') status
if ($LASTEXITCODE -ne 0) { throw 'Staged executable self-check failed.' }
$stagedState = Join-Path $resolvedStaging 'GameFlow.state'
if (Test-Path -LiteralPath $stagedState) {
    Remove-Item -LiteralPath $stagedState -Recurse -Force
}
& powershell -NoProfile -ExecutionPolicy Bypass -File tools\swap_release.ps1 `
    -Root $root -Staged $output
if ($LASTEXITCODE -ne 0) { throw 'Release activation failed.' }
$release = Join-Path $root 'dist\GameFlow'
& $python -3 tools\rebase_bundled_profiles.py $release
if ($LASTEXITCODE -ne 0) { throw 'Activated profile rebasing failed.' }
& $python -3 tools\portable_audit.py $release
if ($LASTEXITCODE -ne 0) { throw 'Activated release portable audit failed.' }
Write-Output "Release build completed: $release"
