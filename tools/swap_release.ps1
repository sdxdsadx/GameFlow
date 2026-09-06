param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$Staged
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($Root)
$releaseRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'dist'))
$buildRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'build'))
$release = [IO.Path]::GetFullPath((Join-Path $releaseRoot 'GameFlow'))
$previous = [IO.Path]::GetFullPath((Join-Path $releaseRoot 'GameFlow.previous'))
$state = [IO.Path]::GetFullPath((Join-Path $releaseRoot 'GameFlow.state'))
$stagedRoot = [IO.Path]::GetFullPath($Staged)
if (-not $release.StartsWith($releaseRoot, [StringComparison]::OrdinalIgnoreCase) -or
    -not $previous.StartsWith($releaseRoot, [StringComparison]::OrdinalIgnoreCase) -or
    -not $state.StartsWith($releaseRoot, [StringComparison]::OrdinalIgnoreCase) -or
    -not $stagedRoot.StartsWith($buildRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Unsafe release paths.'
}
if (-not (Test-Path -LiteralPath (Join-Path $stagedRoot 'build_info.json'))) {
    throw 'Staged build is missing build_info.json.'
}
New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
if (Test-Path -LiteralPath $release) {
    # A frozen GameFlow uses a bootloader/child process pair.  Confirm the
    # listening service's own reported root before stopping that pair; never
    # terminate unrelated processes merely because they share the image name.
    try {
        $identity = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/identity' -TimeoutSec 3
        $identityRoot = [IO.Path]::GetFullPath([string]$identity.root).TrimEnd('\\')
        if ($identityRoot -ieq $release.TrimEnd('\\') -and [int]$identity.pid -gt 0) {
            $owner = Get-Process -Id ([int]$identity.pid) -ErrorAction Stop
            $ownerStarted = $owner.StartTime
            $cohort = @(Get-Process -Name 'GameFlow' -ErrorAction SilentlyContinue |
                Where-Object { [Math]::Abs(($_.StartTime - $ownerStarted).TotalSeconds) -le 3 })
            $cohort | Stop-Process -Force -ErrorAction Stop
            Start-Sleep -Seconds 2
        }
    } catch {
        # No matching local service is a valid state for an offline release.
    }
    New-Item -ItemType Directory -Force -Path $state | Out-Null
    foreach ($name in @('data', 'logs')) {
        $legacy = Join-Path $release $name
        $persistent = Join-Path $state $name
        if ((Test-Path -LiteralPath $legacy) -and -not (Test-Path -LiteralPath $persistent)) {
            Move-Item -LiteralPath $legacy -Destination $persistent -ErrorAction Stop
        }
    }
    if (Test-Path -LiteralPath $previous) {
        try {
            Remove-Item -LiteralPath $previous -Recurse -Force -ErrorAction Stop
        } catch {
            $retired = [IO.Path]::GetFullPath((Join-Path $releaseRoot (
                'GameFlow.retired-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))))
            if (-not $retired.StartsWith($releaseRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Unsafe retired release path: $retired"
            }
            Move-Item -LiteralPath $previous -Destination $retired -ErrorAction Stop
            Write-Warning "Previous release was in use and was retired for later cleanup: $retired"
        }
    }
    try {
        Move-Item -LiteralPath $release -Destination $previous -ErrorAction Stop
    } catch {
        New-Item -ItemType Directory -Path $previous -ErrorAction Stop | Out-Null
        Get-ChildItem -LiteralPath $release -Force | ForEach-Object {
            Move-Item -LiteralPath $_.FullName -Destination $previous -ErrorAction Stop
        }
    }
}
if (Test-Path -LiteralPath $release) {
    Get-ChildItem -LiteralPath $stagedRoot -Force | ForEach-Object {
        Move-Item -LiteralPath $_.FullName -Destination $release -ErrorAction Stop
    }
    Remove-Item -LiteralPath $stagedRoot -Force
} else {
    Move-Item -LiteralPath $stagedRoot -Destination $release -ErrorAction Stop
}
Write-Output "Release activated: $release"
