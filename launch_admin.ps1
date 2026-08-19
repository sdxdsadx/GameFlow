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

# GameFlow's desktop automation requires the Python environment that contains
# psutil, pywin32 and pyautogui.  PATH can put the older Python 3.8 installation
# first; that environment can serve the web page but silently disables every
# window click, foreground and screenshot operation.  Probe candidates instead
# of trusting PATH order, and use the same verified runtime for web and CLI runs.
$pythonCandidates = @(
    'D:\python\python.exe',
    (Get-Command python.exe -ErrorAction SilentlyContinue).Source,
    'C:\Users\26142\AppData\Local\Programs\Python\Python38\python.exe'
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
$python = $null
foreach ($candidate in $pythonCandidates) {
    & $candidate -c 'import psutil, win32con, win32gui, pyautogui' 2>$null
    if ($LASTEXITCODE -eq 0) {
        $python = $candidate
        break
    }
}
if (-not $python) {
    throw 'No Python runtime with psutil, pywin32 and pyautogui was found.'
}
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $python }

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
    Start-Process -FilePath $pythonw -WorkingDirectory $root -ArgumentList @(
        'main.py', '--config', (Join-Path $root 'config\workflow.json'), 'web'
    ) -WindowStyle Hidden
} else {
    if ($Force) {
        & $python main.py run $Mode --force
    } else {
        & $python main.py run $Mode
    }
    Read-Host 'Press Enter to close'
}
