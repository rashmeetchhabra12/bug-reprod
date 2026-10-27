[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "start"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$BugRepro = Join-Path $RepoRoot ".venv\Scripts\bugrepro.exe"
$StateDir = Join-Path $env:LOCALAPPDATA "BugRepro\demo"
$ServiceState = Join-Path $StateDir "service.json"
$DemoState = Join-Path $StateDir "demo.json"
$Ports = @{ buggy = 8103; fixed = 8104 }

function Get-StartStamp([System.Diagnostics.Process]$Process) {
    return $Process.StartTime.ToUniversalTime().ToString("o")
}

function Test-PortFree([int]$Port) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    return -not $listener
}

function Get-PortOwner([int]$Port) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) { return $listener.OwningProcess }
    return $null
}

function Wait-Healthy([string]$Url, [System.Diagnostics.Process]$Process) {
    for ($i = 0; $i -lt 80; $i++) {
        if ($Process.HasExited) { return $false }
        try {
            $response = Invoke-WebRequest -UseBasicParsing "$Url/healthz" -TimeoutSec 1
            if ($response.StatusCode -eq 200) { return $true }
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    return $false
}

function Read-DemoState {
    if (-not (Test-Path -LiteralPath $DemoState)) { return $null }
    try { return (Get-Content -LiteralPath $DemoState -Raw | ConvertFrom-Json) }
    catch { return $null }
}

function Stop-TrackedProcess($Entry) {
    if ($null -eq $Entry) { return $false }
    $process = Get-Process -Id $Entry.pid -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $false }
    if ((Get-StartStamp $process) -ne $Entry.started) { return $false }
    Stop-Process -Id $Entry.pid -Force
    return $true
}

function Remove-StateFiles {
    foreach ($path in @($DemoState, $ServiceState)) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
    }
}

function Stop-Demo([switch]$Quiet) {
    $state = Read-DemoState
    if ($null -eq $state) {
        if (-not $Quiet) { Write-Host "No demo session is recorded. Nothing to stop." }
        return
    }

    if (Test-Path -LiteralPath $ServiceState) {
        try {
            & $BugRepro agent stop --state-file $ServiceState | Out-Null
        } catch {
            Write-Warning "Could not ask the agent service to stop; it may already be gone."
        }
    }

    foreach ($entry in @($state.buggy, $state.fixed)) {
        if (Stop-TrackedProcess $entry) {
            Write-Host ("Stopped {0} (PID {1})." -f $entry.name, $entry.pid)
        }
    }

    Remove-StateFiles
    if (-not $Quiet) { Write-Host "Demo stopped. No unrelated process was touched." }
}

if ($Action -eq "stop") {
    Stop-Demo
    exit 0
}

if ($Action -eq "status") {
    $state = Read-DemoState
    if ($null -eq $state) {
        Write-Host "No demo session is recorded."
        exit 0
    }
    Write-Host "Buggy:  $($state.buggyUrl)"
    Write-Host "Fixed:  $($state.fixedUrl)"
    Write-Host "Agent:  $($state.serviceUrl)"
    foreach ($entry in @($state.buggy, $state.fixed, $state.service)) {
        $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        $alive = $null -ne $process -and (Get-StartStamp $process) -eq $entry.started
        Write-Host ("{0}: {1}" -f $entry.name, $(if ($alive) { "running (PID $($entry.pid))" } else { "not running" }))
    }
    exit 0
}

# ---------------------------------------------------------------- start
if (-not (Test-Path -LiteralPath $BugRepro)) {
    Write-Host "ERROR: bugrepro is not installed in .venv. Run: .\.venv\Scripts\python.exe -m pip install -e `".[dev]`"" -ForegroundColor Red
    exit 2
}

$existing = Read-DemoState
if ($null -ne $existing) {
    $alive = @()
    foreach ($entry in @($existing.buggy, $existing.fixed, $existing.service)) {
        $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if ($null -ne $process -and (Get-StartStamp $process) -eq $entry.started) {
            $alive += $entry.name
        }
    }
    if ($alive.Count -gt 0) {
        Write-Host "ERROR: A demo session is already running ($($alive -join ', ')). Run .\scripts\demo.ps1 stop first." -ForegroundColor Red
        exit 2
    }
    Remove-StateFiles
}

foreach ($port in $Ports.Values) {
    if (-not (Test-PortFree $port)) {
        $owner = Get-PortOwner $port
        Write-Host "ERROR: Port $port is already in use (PID $owner). Refusing to kill it. Free the port or choose another demo script." -ForegroundColor Red
        exit 2
    }
}

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$startedEntries = @()
$serviceEntry = $null

try {
    $buggy = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "bugrepro", "serve", "--app", "tasks", "--mode", "buggy",
        "--port", "$($Ports.buggy)"
    ) -PassThru -WindowStyle Hidden
    $startedEntries += [pscustomobject]@{
        name = "buggy"; pid = $buggy.Id; started = (Get-StartStamp $buggy)
    }

    $fixed = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "bugrepro", "serve", "--app", "tasks", "--mode", "fixed",
        "--port", "$($Ports.fixed)"
    ) -PassThru -WindowStyle Hidden
    $startedEntries += [pscustomobject]@{
        name = "fixed"; pid = $fixed.Id; started = (Get-StartStamp $fixed)
    }

    $buggyUrl = "http://127.0.0.1:$($Ports.buggy)"
    $fixedUrl = "http://127.0.0.1:$($Ports.fixed)"
    if (-not (Wait-Healthy $buggyUrl $buggy)) {
        throw "The buggy task app did not become healthy at $buggyUrl."
    }
    if (-not (Wait-Healthy $fixedUrl $fixed)) {
        throw "The fixed task app did not become healthy at $fixedUrl."
    }

    $service = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "bugrepro", "agent", "serve", "--port", "0",
        "--state-file", "`"$ServiceState`"",
        "--run-root", "`"$(Join-Path $RepoRoot 'runs')`"",
        "--idle-timeout", "1800", "--session-idle-timeout", "900"
    ) -PassThru -WindowStyle Hidden

    $deadline = (Get-Date).AddSeconds(40)
    while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $ServiceState)) {
        if ($service.HasExited) { throw "The agent service exited during startup." }
        Start-Sleep -Milliseconds 200
    }
    if (-not (Test-Path -LiteralPath $ServiceState)) {
        throw "The agent service did not write its state file."
    }

    $serviceInfo = Get-Content -LiteralPath $ServiceState -Raw | ConvertFrom-Json
    $serviceEntry = [pscustomobject]@{
        name = "agent"; pid = $service.Id; started = (Get-StartStamp $service)
    }
    $startedEntries += $serviceEntry

    $record = [pscustomobject]@{
        buggy = $startedEntries[0]
        fixed = $startedEntries[1]
        service = $serviceEntry
        buggyUrl = $buggyUrl
        fixedUrl = $fixedUrl
        serviceUrl = $serviceInfo.url
    }
    $record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $DemoState -Encoding UTF8

    Write-Host ""
    Write-Host "Task list, buggy:  $buggyUrl"
    Write-Host "Task list, fixed:  $fixedUrl"
    Write-Host "Agent service:    $($serviceInfo.url)"
    Write-Host "Service state:    $ServiceState"
    Write-Host "Runs/evidence:    $(Join-Path $RepoRoot 'runs')"
    Write-Host ""
    Write-Host "Next: open docs\USAGE.md or docs\EXAMPLES.md, or run:"
    Write-Host "  .\.venv\Scripts\bugrepro.exe explore start --spec-file examples\task-exploration.json --driver alan-agent --label live-task-buggy"
    Write-Host "Stop:"
    Write-Host "  .\scripts\demo.ps1 stop"
} catch {
    Write-Host "Startup failed: $($_.Exception.Message)"
    if (Test-Path -LiteralPath $ServiceState) {
        try { & $BugRepro agent stop --state-file $ServiceState | Out-Null } catch { }
    }
    foreach ($entry in $startedEntries) {
        if ($entry.name -ne "agent") { Stop-TrackedProcess $entry | Out-Null }
    }
    Remove-StateFiles
    exit 2
}