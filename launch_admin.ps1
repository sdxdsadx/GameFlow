param(
    [string]$Mode = "web",
    [switch]$Force
)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $isAdmin) {
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"' + $MyInvocation.MyCommand.Path + '"'),
        $Mode
    )
    if ($Force) {
        $arguments += '-Force'
    }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments
    exit
}

Set-Location -LiteralPath $root
$smtpUser = [Environment]::GetEnvironmentVariable('GAMEFLOW_SMTP_USER', 'User')
$smtpAuthCode = [Environment]::GetEnvironmentVariable('GAMEFLOW_SMTP_AUTH_CODE', 'User')
if ($smtpUser) { $env:GAMEFLOW_SMTP_USER = $smtpUser }
if ($smtpAuthCode) { $env:GAMEFLOW_SMTP_AUTH_CODE = $smtpAuthCode }
if ($Mode -eq 'web') {
    $existingState = $null
    try {
        $existingState = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/status' -TimeoutSec 2
    } catch {}
    $staleState = $false
    if ($existingState -and $existingState.state.running) {
        $activeNames = @($existingState.state.active)
        $runningNames = @($existingState.runs | Where-Object { $_.status -eq 'running' } |
            ForEach-Object { $_.workflow })
        $staleState = ($activeNames.Count -gt 0 -and
            @($activeNames | Where-Object { $runningNames -contains $_ }).Count -eq 0)
    }
    if ($existingState -and $existingState.state.running -and -not $staleState) {
        Start-Process 'http://127.0.0.1:8765/'
        exit
    }
    if ($existingState) {
        # SO_REUSEADDR may briefly leave more than one old listener behind. Remove
        # every owner that becomes visible before launching the replacement.
        for ($attempt = 0; $attempt -lt 5; $attempt++) {
            $owners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique)
            if (-not $owners) { break }
            $owners | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Milliseconds 400
        }
    }
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $pythonw) {
        $pythonw = (Get-Command python.exe).Source
    }
    Start-Process -FilePath $pythonw -WorkingDirectory $root -ArgumentList @(
        'main.py', '--config', (Join-Path $root 'config\workflow.json'), 'web'
    ) -WindowStyle Hidden
} else {
    if ($Force) {
        python main.py run $Mode --force
    } else {
        python main.py run $Mode
    }
    Read-Host 'Press Enter to close'
}
