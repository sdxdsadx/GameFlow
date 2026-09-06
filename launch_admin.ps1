param([string]$Mode = 'web', [switch]$Force)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$url = 'http://127.0.0.1:8765/'
$statusUrl = $url + 'api/status'
$identityUrl = $url + 'api/identity'
$localExe = Join-Path $root 'GameFlow.exe'
$builtExe = Join-Path $root 'dist\GameFlow\GameFlow.exe'
$resolvedExe = if (Test-Path -LiteralPath $localExe) { $localExe } elseif (
    Test-Path -LiteralPath $builtExe) { $builtExe } else { $null }
$expectedRoot = if ($resolvedExe) { Split-Path -Parent $resolvedExe } else { $root }
if ($Mode -eq 'web') {
    $busy = $false
    try {
        $identity = Invoke-RestMethod -Uri $identityUrl -TimeoutSec 2
        $actualRoot = [IO.Path]::GetFullPath([string]$identity.root).TrimEnd('\')
        $wantedRoot = [IO.Path]::GetFullPath($expectedRoot).TrimEnd('\')
        $isElevated = [bool]$identity.elevated
        if ($actualRoot -ieq $wantedRoot -and $isElevated) {
            Start-Process $url
            exit
        }
        # A legacy/non-elevated build may still answer /api/identity without
        # the elevated field.  It must not be treated as a reusable server:
        # stop it when it owns this GameFlow root, then continue to the
        # elevation branch below.
        if ([int]$identity.pid -gt 0 -and $actualRoot -ieq $wantedRoot) {
            Stop-Process -Id ([int]$identity.pid) -Force -ErrorAction Stop
            Start-Sleep -Milliseconds 500
        }
    } catch {
        # Legacy builds predate /api/identity. Only replace an owner that is
        # positively identified as GameFlow; never kill an unrelated service.
        try {
            Invoke-RestMethod -Uri $statusUrl -TimeoutSec 2 | Out-Null
            $connection = Get-NetTCPConnection -LocalPort 8765 -State Listen `
                -ErrorAction Stop | Select-Object -First 1
            $owner = Get-Process -Id $connection.OwningProcess -ErrorAction Stop
            $legacyStatus = Invoke-RestMethod -Uri $statusUrl -TimeoutSec 2
            if ($owner.ProcessName -ieq 'GameFlow' -and [bool]$legacyStatus.state.running) {
                $busy = $true
            } elseif ($owner.ProcessName -ieq 'GameFlow') {
                Stop-Process -Id $owner.Id -Force -ErrorAction Stop
                Start-Sleep -Milliseconds 500
            }
        } catch {}
    }
    if ($busy) {
        Start-Process $url
        exit
    }
}
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    if ($Mode -eq 'web' -and $resolvedExe) {
        $exeArg = $resolvedExe.Replace("'", "''")
        $workArg = (Split-Path -Parent $resolvedExe).Replace("'", "''")
        $elevatedCommand = "Start-Process -FilePath '$exeArg' " +
            "-ArgumentList 'web --no-browser' -WorkingDirectory '$workArg' " +
            "-WindowStyle Hidden"
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden `
            -ArgumentList @('-NoProfile', '-WindowStyle', 'Hidden',
                '-ExecutionPolicy', 'Bypass', '-Command', $elevatedCommand)
        $ready = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            try {
                $probeIdentity = Invoke-RestMethod -Uri $identityUrl -TimeoutSec 2
                if ([bool]$probeIdentity.elevated) {
                    $ready = $true
                    break
                }
            } catch {
                Start-Sleep -Milliseconds 250
            }
        }
        if ($ready) { Start-Process $url }
        exit
    }
    $arguments = @('-NoProfile', '-WindowStyle', 'Hidden',
        '-ExecutionPolicy', 'Bypass', '-File',
        ('"' + $MyInvocation.MyCommand.Path + '"'), $Mode)
    if ($Force) { $arguments += '-Force' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs `
        -WindowStyle Hidden -ArgumentList $arguments
    exit
}

if ($Mode -eq 'stop-server') {
    Get-Process -Name 'GameFlow' -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    exit
}

if ($resolvedExe) {
    $program = $resolvedExe
    $arguments = if ($Mode -eq 'web') { @('web') } else {
        @('run', $Mode) + $(if ($Force) { @('--force') } else { @() })
    }
    $workingDirectory = Split-Path -Parent $resolvedExe
} else {
    $program = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
    if (-not $program) {
        throw 'GameFlow build was not found. Run build.ps1 first.'
    }
    $arguments = @('-3', 'main.py') + $(if ($Mode -eq 'web') { @('web') } else {
        @('run', $Mode) + $(if ($Force) { @('--force') } else { @() })
    })
    $workingDirectory = $root
}

if ($Mode -eq 'web') {
    Start-Process -FilePath $program -ArgumentList ($arguments + '--no-browser') `
        -WorkingDirectory $workingDirectory -WindowStyle Hidden | Out-Null
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            Invoke-RestMethod -Uri $statusUrl -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $ready) {
        throw 'GameFlow startup timed out. Check logs\gameflow.log.'
    }
    Start-Process $url
} else {
    & $program @arguments
    Read-Host 'Press Enter to close'
}
