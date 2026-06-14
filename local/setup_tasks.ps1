# Register the LinkedInJobsWatcher Windows scheduled task AND drop a desktop
# shortcut that opens the dashboard on demand.
# Run once from an elevated or normal PowerShell prompt - it registers the task
# for the current user only and never requires admin.
#
# Re-running is safe: existing task is unregistered first, shortcut is overwritten.
#
#   -NoShortcut   skip creating/refreshing the desktop shortcut
#   -NoTask       skip (re)registering the scheduled task

[CmdletBinding()]
param(
    [string]$TaskName = "LinkedInJobsWatcher",
    [string]$PythonW  = "",  # optional override; if empty we auto-detect
    [switch]$NoShortcut,
    [switch]$NoTask
)

$ErrorActionPreference = "Stop"

$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script   = Join-Path $Here "watcher.py"
$Launcher = Join-Path $Here "open_dashboard.pyw"
$WorkDir  = $Here
$XmlPath  = Join-Path $Here "task.xml"

if (-not (Test-Path $Script))   { throw "watcher.py not found at $Script" }
if (-not (Test-Path $XmlPath))  { throw "task.xml not found at $XmlPath" }
if (-not (Test-Path $Launcher)) { throw "open_dashboard.pyw not found at $Launcher" }

function Resolve-PythonW {
    if ($PythonW -and (Test-Path $PythonW)) { return $PythonW }

    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $py = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidate = Join-Path (Split-Path -Parent $py.Source) "pythonw.exe"
        if (Test-Path $candidate) { return $candidate }
        return $py.Source   # fall back; will flash console but still work
    }

    throw "Could not locate pythonw.exe / python.exe on PATH. Pass -PythonW explicitly."
}

$PythonWPath = Resolve-PythonW
$UserId      = "$env:USERDOMAIN\$env:USERNAME"

# --- Desktop shortcut: open the dashboard on demand ---------------------------
function New-DashboardShortcut {
    param([string]$PythonWPath, [string]$Launcher, [string]$WorkDir)

    # GetFolderPath("Desktop") respects OneDrive / folder-redirection.
    $desktop = [Environment]::GetFolderPath("Desktop")
    if (-not (Test-Path $desktop)) {
        Write-Warning "Desktop folder not found ($desktop) - skipping shortcut."
        return
    }
    $lnk = Join-Path $desktop "LinkedIn Jobs Dashboard.lnk"
    $shell = New-Object -ComObject WScript.Shell
    try {
        $sc = $shell.CreateShortcut($lnk)
        $sc.TargetPath       = $PythonWPath
        $sc.Arguments        = '"{0}"' -f $Launcher
        $sc.WorkingDirectory = $WorkDir
        $sc.WindowStyle      = 1
        $sc.Description       = "Open the LinkedIn Jobs triage dashboard"
        # pythonw.exe carries the Python icon; clearly identifiable on the desktop.
        $sc.IconLocation     = "$PythonWPath,0"
        $sc.Save()
        Write-Host "Desktop shortcut created: $lnk"
    } finally {
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
    }
}

if (-not $NoShortcut) {
    New-DashboardShortcut -PythonWPath $PythonWPath -Launcher $Launcher -WorkDir $WorkDir
}

# --- Scheduled task -----------------------------------------------------------
if (-not $NoTask) {
    Write-Host ""
    Write-Host "Registering scheduled task '$TaskName'"
    Write-Host "  pythonw  : $PythonWPath"
    Write-Host "  script   : $Script"
    Write-Host "  workdir  : $WorkDir"
    Write-Host "  user     : $UserId"

    # Read template and substitute.
    $xml = Get-Content -Raw -Path $XmlPath -Encoding UTF8
    $xml = $xml.Replace('{{PYTHONW}}', $PythonWPath)
    $xml = $xml.Replace('{{SCRIPT}}',  $Script)
    $xml = $xml.Replace('{{WORKDIR}}', $WorkDir)
    $xml = $xml.Replace('{{USERID}}',  $UserId)
    # Register-ScheduledTask receives a UTF-16 string; strip any encoding declaration
    # so the XML parser doesn't choke with "unable to switch the encoding".
    $xml = $xml -replace '<\?xml[^>]*\?>', '<?xml version="1.0"?>'

    # Unregister if it already exists (idempotent re-install).
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "Removed existing task."
    } catch {
        # No existing task - ignore.
    }

    # The <BootTrigger> needs admin rights to register. If we're not elevated,
    # registration fails with Access denied - so fall back to a boot-trigger-less
    # version rather than leaving the task unregistered. The LogonTrigger already
    # fires after a boot/restart, and StartWhenAvailable replays missed daily
    # runs on the next boot, so boot coverage holds either way.
    $bootTrigger = $true
    try {
        Register-ScheduledTask -Xml $xml -TaskName $TaskName -ErrorAction Stop | Out-Null
    } catch {
        Write-Warning "Could not register with the system-startup BootTrigger (needs admin): $($_.Exception.Message)"
        Write-Warning "Falling back to logon/unlock/wake/daily triggers (these already cover boot)."
        $xmlNoBoot = $xml -replace '(?s)\s*<BootTrigger>.*?</BootTrigger>', ''
        Register-ScheduledTask -Xml $xmlNoBoot -TaskName $TaskName | Out-Null
        $bootTrigger = $false
    }

    Write-Host ""
    Write-Host "Task registered. Triggers:"
    Write-Host "  - At user logon (fires after a boot / restart)"
    if ($bootTrigger) {
        Write-Host "  - At system startup (2 min after boot - explicit boot coverage)"
    } else {
        Write-Host "  - (system-startup BootTrigger skipped - re-run elevated to add it)"
    }
    Write-Host "  - On session unlock"
    Write-Host "  - On resume from sleep (System event Power-Troubleshooter ID 1)"
    Write-Host "  - 6x daily (10:10/20/30 + 19:10/20/30); missed runs fire on next boot"
    Write-Host ""
    Write-Host "To start it now without waiting for a trigger:"
    Write-Host "  Start-ScheduledTask -TaskName $TaskName"
}

Write-Host ""
Write-Host "Open the dashboard any time: double-click 'LinkedIn Jobs Dashboard' on the desktop."
Write-Host "Logs:  `$env:LOCALAPPDATA\linkedin_watcher\watcher.log"
Write-Host "Config: $Here\config.json (auto-generated on first run)"
