<#
.SYNOPSIS
    One-stop setup for the Job Scraper + Resume Tailor. Writes your local .env,
    config.json, and master_experience.yaml so the tool runs against YOUR data.

.DESCRIPTION
    Two flows, one code path:
      Fast (default) - copies the example files into place and writes sensible
                       config defaults. Edit .env + master_experience.yaml after.
      Long           - guided: prompts for your Bright Data / Google Cloud keys,
                       candidate name, Google Drive sync folder, and dashboard
                       preferences, then writes everything filled in.

    Nothing is overwritten unless you pass -Force. Re-run any time to revisit
    settings: your existing values are kept and shown as defaults. State lives in
    .env (secrets) and local/config.json (dashboard prefs) - both git-ignored.

.EXAMPLE
    ./scripts/setup.ps1                     # fast: drop example files into place
.EXAMPLE
    ./scripts/setup.ps1 -Mode long          # guided wizard with prompts
.EXAMPLE
    ./scripts/setup.ps1 -Mode long -InstallDeps    # also pip-install requirements
#>
[CmdletBinding()]
param(
    [ValidateSet('fast', 'long')] [string]$Mode = 'fast',
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$Force,
    [switch]$InstallDeps,
    # Long-mode values (optional; prompted when missing in long mode)
    [string]$BrightDataToken,
    [string]$BrightDataDataset,
    [string]$GcpProject,
    [string]$CandidateName,
    [string]$GDriveRoot,
    [int]$MinScore = 4,
    [int]$FollowupDays = 5
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }

# Prompt with a default; non-interactive callers pass the value as a param.
function Read-WithDefault($label, $default) {
    if ([string]::IsNullOrWhiteSpace($default)) { $shown = "" } else { $shown = " [$default]" }
    $val = Read-Host "$label$shown"
    if ([string]::IsNullOrWhiteSpace($val)) { return $default }
    return $val
}

# Use the param value if the caller supplied one; otherwise prompt. Lets the same
# long-mode flow run fully interactively OR fully from params (non-interactive).
function Resolve-Value($current, $label, $default) {
    if (-not [string]::IsNullOrWhiteSpace($current)) { return $current }
    return (Read-WithDefault $label $default)
}

# Set KEY=value in a .env text body, preserving comments/order. Adds the key if
# absent. Operates line-by-line (no regex replacement) so a '$' or other regex
# metacharacter in the value is written literally, never interpreted.
function Set-EnvValue($text, $key, $value) {
    $line = "$key=$value"
    $pattern = "^#?\s*$([regex]::Escape($key))="
    $nl = if ($text -match "`r`n") { "`r`n" } else { "`n" }   # preserve the file's line endings
    $lines = $text -split "`r?`n"
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) { $lines[$i] = $line; return ($lines -join $nl) }
    }
    return ($text.TrimEnd() + $nl + $line + $nl)
}

$envPath       = Join-Path $Root '.env'
$envExample    = Join-Path $Root '.env.example'
$rtDir         = Join-Path $Root 'resume_tailor_files'
$masterPath    = Join-Path $rtDir 'master_experience.yaml'
$masterExample = Join-Path $rtDir 'master_experience.example.yaml'
$cfgPath       = Join-Path (Join-Path $Root 'local') 'config.json'

Write-Step "Job Scraper setup ($Mode mode) in $Root"

# --- 1. .env ------------------------------------------------------------------
if ((Test-Path -LiteralPath $envPath) -and -not $Force) {
    Write-Skip ".env exists (use -Force to regenerate). Updating only provided values."
    $envText = Get-Content -LiteralPath $envPath -Raw
} else {
    if (-not (Test-Path -LiteralPath $envExample)) { throw ".env.example not found at $envExample" }
    $envText = Get-Content -LiteralPath $envExample -Raw
    Write-Ok "Seeded .env from .env.example"
}

if ($Mode -eq 'long') {
    Write-Step "Credentials and identity (press Enter to keep the shown default)"
    $BrightDataToken   = Resolve-Value $BrightDataToken   'Bright Data API token'   ''
    $BrightDataDataset = Resolve-Value $BrightDataDataset 'Bright Data dataset id'  ''
    $GcpProject        = Resolve-Value $GcpProject        'Google Cloud project id' ''
    $CandidateName     = Resolve-Value $CandidateName     'Your name (for resume filenames, no spaces)' ''
}

if ($BrightDataToken)   { $envText = Set-EnvValue $envText 'BRIGHT_DATA_API_TOKEN'   $BrightDataToken }
if ($BrightDataDataset) { $envText = Set-EnvValue $envText 'BRIGHT_DATA_DATASET_ID'  $BrightDataDataset }
if ($GcpProject)        { $envText = Set-EnvValue $envText 'GOOGLE_CLOUD_PROJECT'    $GcpProject }
if ($CandidateName)     { $envText = Set-EnvValue $envText 'RESUME_TAILOR_CANDIDATE' ($CandidateName -replace '\s+', '_') }

Set-Content -LiteralPath $envPath -Value $envText -Encoding UTF8 -NoNewline
Write-Ok "Wrote $envPath"

# --- 2. master_experience.yaml ------------------------------------------------
$haveMaster  = [bool](Test-Path -LiteralPath $masterPath)
$haveExample = [bool](Test-Path -LiteralPath $masterExample)
if ($haveMaster -and -not $Force) {
    Write-Skip "master_experience.yaml exists - left untouched."
} elseif ($haveExample) {
    Copy-Item -LiteralPath $masterExample -Destination $masterPath -Force
    Write-Ok "Created master_experience.yaml from the template - EDIT IT with your real experience."
} else {
    Write-Skip "No master_experience.example.yaml found; skipping."
}

# --- 3. local/config.json (dashboard prefs) -----------------------------------
$cfg = [ordered]@{ gdrive_root = ''; mtime_stable_seconds = 30; min_score = $MinScore; followup_days = $FollowupDays }
if (Test-Path -LiteralPath $cfgPath) {
    try {
        $existing = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
        foreach ($p in $existing.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
    } catch { Write-Skip "Existing config.json unreadable; writing fresh defaults." }
}
if ($Mode -eq 'long') {
    Write-Step "Dashboard preferences"
    $GDriveRoot = Resolve-Value $GDriveRoot 'Google Drive sync folder (where scraped CSVs land)' ([string]$cfg.gdrive_root)
    if (-not $PSBoundParameters.ContainsKey('MinScore')) {
        $cfg.min_score = [int](Read-WithDefault 'Minimum score to surface in High-Score tab' ([string]$cfg.min_score))
    }
    if (-not $PSBoundParameters.ContainsKey('FollowupDays')) {
        $cfg.followup_days = [int](Read-WithDefault 'Days after applying to nudge a follow-up' ([string]$cfg.followup_days))
    }
}
if ($PSBoundParameters.ContainsKey('MinScore'))     { $cfg.min_score = $MinScore }
if ($PSBoundParameters.ContainsKey('FollowupDays')) { $cfg.followup_days = $FollowupDays }
if ($GDriveRoot) { $cfg.gdrive_root = $GDriveRoot }

$cfgDir = Split-Path $cfgPath -Parent
if (-not (Test-Path -LiteralPath $cfgDir)) { New-Item -ItemType Directory -Path $cfgDir | Out-Null }
($cfg | ConvertTo-Json) | Set-Content -LiteralPath $cfgPath -Encoding UTF8
Write-Ok "Wrote $cfgPath"

# --- 4. dependencies (optional) -----------------------------------------------
if ($InstallDeps) {
    Write-Step "Installing Python dependencies (requirements.txt)"
    python -m pip install -r (Join-Path $Root 'requirements.txt')
    Write-Ok "Dependencies installed"
}

# --- 5. next steps ------------------------------------------------------------
Write-Step "Done. Next steps:"
Write-Host @"
    1. Edit  resume_tailor_files/master_experience.yaml  with your real experience.
    2. Fill any blanks in  .env  (Bright Data + Google Cloud keys).
    3. Authenticate Google Cloud:  gcloud auth application-default login
    4. (Scraping) run your own pipeline:  python scraper.py   then   python score_jobs.py
       or follow docs/HANDOFF.md to run it on a GCP VM via cron.
    5. (Dashboard) launch:  python local/ui.py   (or local/open_dashboard.pyw)
"@ -ForegroundColor Gray
