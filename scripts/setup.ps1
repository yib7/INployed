<#
.SYNOPSIS
    One-stop setup for INployed. Writes your local .env, config.json, and
    master_experience.yaml so the app runs against YOUR data. With -AutoApply it
    also installs the browser half of auto-apply (README Step 7).

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

    -AutoApply installs Playwright (pinned below) and its Chromium into the
    project venv, for the auto-apply runner and the difficulty check.

.EXAMPLE
    ./scripts/setup.ps1                     # fast: drop example files into place
.EXAMPLE
    ./scripts/setup.ps1 -Mode long          # guided wizard with prompts
.EXAMPLE
    ./scripts/setup.ps1 -Mode long -InstallDeps    # also pip-install requirements
.EXAMPLE
    ./scripts/setup.ps1 -AutoApply          # also install Playwright + Chromium
#>
[CmdletBinding()]
param(
    [ValidateSet('fast', 'long')] [string]$Mode = 'fast',
    [string]$Root = '',
    [switch]$Force,
    [switch]$InstallDeps,
    [switch]$AutoApply,
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

# Resolve the repo root here, NOT as a param default: when the script is started
# with `powershell -File scripts\setup.ps1` (the documented fallback for a machine
# whose execution policy blocks `./scripts/setup.ps1`), PowerShell 5.1 binds the
# param defaults before $PSScriptRoot is populated, so a default of
# `Split-Path -Parent $PSScriptRoot` threw "Cannot bind argument to parameter
# 'Path' because it is an empty string" and the whole script died. $PSCommandPath
# is set by then under every launch form.
if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}
# The .NET file calls below resolve a relative path against the PROCESS working
# directory, which is not PowerShell's. Pin $Root to a full path once, here.
$Root = (Resolve-Path -LiteralPath $Root).ProviderPath

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }

# --- text I/O: UTF-8, no BOM, both directions --------------------------------
# Neither PowerShell 5.1 default is safe for the files this script owns, and both
# failures are SILENT, which is why they survived a green CI run that only
# checked the files exist:
#
#   * Writing. `Set-Content -Encoding UTF8` emits a BOM on 5.1. The Python side
#     parses local/config.json with plain utf-8, and json.loads rejects a leading
#     BOM -- a rejection jsonutil.read_json_dict swallows into {}. So every value
#     written here (min_score, followup_days, gdrive_root, mtime_stable_seconds)
#     was dropped on a fresh install and the dashboard ran on hardcoded defaults,
#     with nothing to show the user their config had been ignored.
#   * Reading. `Get-Content` decodes a BOM-less file as the ANSI codepage, so
#     .env.example's UTF-8 box-drawing comment rules came back as Windows-1252
#     mojibake and were written into the user's .env that way.
#
# UTF8Encoding($false) writes without a BOM; File::ReadAllText detects and strips
# one, so an existing BOM'd .env or config.json from an older run still reads.
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Read-TextFile($path)        { [System.IO.File]::ReadAllText($path) }
function Write-TextFile($path, $text) { [System.IO.File]::WriteAllText($path, $text, $script:Utf8NoBom) }

# The project venv README Step 2 builds, when it exists: bare `python` is the
# global interpreter (the venv is never activated), and the launcher only looks
# in venv\Scripts, so a package installed globally is one it cannot see.
function Get-ProjectPython {
    $venvPy = Join-Path (Join-Path (Join-Path $Root 'venv') 'Scripts') 'python.exe'
    if (Test-Path -LiteralPath $venvPy) { return $venvPy }
    return 'python'
}

# Run a native command and stop on a non-zero exit. PowerShell 5.1 ignores a
# native exit code, and under 'Stop' it turns a line the tool writes to stderr
# into a terminating error once stderr is redirected (`setup.ps1 2>&1 | Tee`);
# pip and playwright both write progress there. So the preference is relaxed
# for the call and the exit code is checked by hand.
function Invoke-Checked([string]$exe, [string[]]$argv) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $exe @argv; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
    if ($code -ne 0) { throw "'$exe $($argv -join ' ')' failed with exit code $code" }
}

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

Write-Step "INployed setup ($Mode mode) in $Root"

# --- 1. .env ------------------------------------------------------------------
if ((Test-Path -LiteralPath $envPath) -and -not $Force) {
    Write-Skip ".env exists (use -Force to regenerate). Updating only provided values."
    $envText = Read-TextFile $envPath
} else {
    if (-not (Test-Path -LiteralPath $envExample)) { throw ".env.example not found at $envExample" }
    $envText = Read-TextFile $envExample
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

Write-TextFile $envPath $envText
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
# mtime_stable_seconds is a WATCHER-only key (how long a freshly synced file must
# stop changing before watcher.py opens it). It is deliberately not in the
# Settings tab schema; watcher.load_config() reads config.json directly and
# defaults it to 30, so seeding it here just makes the value visible to editors.
# Keep the 30 below in sync with watcher.DEFAULT_CONFIG: an explicit file value
# always beats that default, so a stale seed would pin every new install.
$cfg = [ordered]@{ gdrive_root = ''; mtime_stable_seconds = 30; min_score = $MinScore; followup_days = $FollowupDays }
if (Test-Path -LiteralPath $cfgPath) {
    try {
        $existing = Read-TextFile $cfgPath | ConvertFrom-Json
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
Write-TextFile $cfgPath (($cfg | ConvertTo-Json) + "`r`n")
Write-Ok "Wrote $cfgPath"

# --- 4. dependencies (optional) -----------------------------------------------
if ($InstallDeps) {
    Write-Step "Installing Python dependencies (requirements.txt)"
    # Upgrade pip first, same as README Step 2: a fresh interpreter ships pip 25.2,
    # which carries six advisories fixed by 26.2 (path traversal in entry-point
    # names, a symlink escape in the fallback tar extractor, a doubly-encoded
    # index URL). This is the installer, not a project pin, so it is not in
    # requirements.txt.
    $py = Get-ProjectPython
    Invoke-Checked $py @('-m', 'pip', 'install', '--upgrade', 'pip')
    Invoke-Checked $py @('-m', 'pip', 'install', '-r', (Join-Path $Root 'requirements.txt'))
    Write-Ok "Dependencies installed (into $py)"
}

# --- 5. auto-apply browser (optional, README Step 7) ---------------------------
# The auto-apply runner and the difficulty check drive a browser through
# Playwright: Google Chrome when it is installed, else the Chromium that
# `playwright install chromium` downloads into %LOCALAPPDATA%\ms-playwright.
# Optional, so README Step 2 stays small for everyone who never auto-applies.
# Held at 1.61.0: on 1.62.0 and 1.63.0, closing a page that a second failed
# load left on Chrome's error page never returns, so a run can hang. CI's
# browser tests install the same version, and tests/test_setup_script.py
# checks that every place naming the version agrees.
$PlaywrightPin = 'playwright==1.61.0'
if ($AutoApply) {
    Write-Step "Installing Playwright and its Chromium for auto-apply"
    $py = Get-ProjectPython
    Invoke-Checked $py @('-m', 'pip', 'install', $PlaywrightPin)
    Invoke-Checked $py @('-m', 'playwright', 'install', 'chromium')
    Write-Ok "Installed $PlaywrightPin and its Chromium (into $py)"
}

# --- 6. next steps ------------------------------------------------------------
Write-Step "Done. Next steps:"
Write-Host @"
    1. Launch the dashboard: double-click  "Open INployed Dashboard.cmd"  in the
       project folder. That is the single entry point for every later launch.
    2. Set your keys and options in the dashboard's Settings tab. One form covers
       the .env keys, paths, search, scoring, resume, and apply answers, so nothing
       needs editing by hand.
    3. Enter your experience in the Resume Data tab, which writes
       resume_tailor_files/master_experience.yaml for you.
    4. (Only if you bill a Google Cloud project; skip if you use Gemini API keys)
       authenticate it:  gcloud auth application-default login
    5. (Scraping) run your own pipeline from the venv README Step 2 built:
       venv\Scripts\python.exe pipeline\scraper.py   then   venv\Scripts\python.exe pipeline\score_jobs.py
       or run it on a small GCP VM via cron, managed from Settings -> VM.
    6. (Auto-apply) install its browser once:
       powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -AutoApply
"@ -ForegroundColor Gray
