# Record or replay the Jev answers the runner tests make (SP8 tuning harness).
#
#   .\scripts\jev_record.ps1                 # record: needs TYPESAFE_API_KEY, spends money
#   .\scripts\jev_record.ps1 -Mode replay    # replay: no key, no network
#   .\scripts\jev_record.ps1 -Cap 0.50       # stop recording once live spend passes 0.50 USD
#
# Record mode reads TYPESAFE_API_KEY from the environment, else from the one row
# in .env, and exports it to this one pytest process only; the value is never
# printed. After the run the mode and cap variables are removed again so a
# later plain pytest stays on the fake, and the key is dropped when this
# script loaded it. Both modes end with scripts/jev_thresholds.py over the
# cache and outcomes.jsonl. Pure ASCII on purpose (PowerShell 5.1).
param(
    [ValidateSet("record", "replay")]
    [string]$Mode = "record",
    [double]$Cap = 1.00,
    [string]$Cache = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$loadedKey = $false
if ($Mode -eq "record" -and -not $env:TYPESAFE_API_KEY) {
    $envFile = Join-Path $root ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^\s*TYPESAFE_API_KEY\s*=\s*(.*)$') {
                $env:TYPESAFE_API_KEY = $Matches[1].Trim().Trim('"').Trim("'")
                $loadedKey = $true
                break
            }
        }
    }
    if (-not $env:TYPESAFE_API_KEY) {
        Write-Host "TYPESAFE_API_KEY is not set and .env has no row for it."
        Write-Host "Create a key at console.typesafe.ai/keys and add TYPESAFE_API_KEY=<key> to .env."
        exit 2
    }
}

$env:AUTO_APPLY_TEST_JEV = $Mode
$env:AUTO_APPLY_RECORD_USD_CAP = $Cap.ToString([System.Globalization.CultureInfo]::InvariantCulture)
if ($Cache) { $env:AUTO_APPLY_JEV_CACHE = $Cache }
$env:QT_QPA_PLATFORM = "offscreen"

Write-Host "jev $Mode over the runner tests (cap $Cap USD)"
python -m pytest tests/test_apply_run.py tests/test_apply_run_boundaries.py -q
$code = $LASTEXITCODE

$thresholdArgs = @()
if ($Cache) { $thresholdArgs = @("--cache", $Cache) }
python scripts/jev_thresholds.py @thresholdArgs

Remove-Item Env:AUTO_APPLY_TEST_JEV -ErrorAction SilentlyContinue
Remove-Item Env:AUTO_APPLY_RECORD_USD_CAP -ErrorAction SilentlyContinue
if ($Cache) { Remove-Item Env:AUTO_APPLY_JEV_CACHE -ErrorAction SilentlyContinue }
if ($loadedKey) { Remove-Item Env:TYPESAFE_API_KEY -ErrorAction SilentlyContinue }
exit $code
