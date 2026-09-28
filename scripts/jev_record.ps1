# Record or replay live Jev answers for a fixture set (SP8 tuning harness).
#
#   .\scripts\jev_record.ps1 -Cap 0.20                     # record the runner tests
#   .\scripts\jev_record.ps1 -Mode replay                  # replay them: no key, no network
#   .\scripts\jev_record.ps1 -Target matrix -Cap 0.20      # record the flow matrix's real column
#   .\scripts\jev_record.ps1 -Target captures -Cap 0.05    # record the local captures' page reads
#   .\scripts\jev_record.ps1 -Target matrix -Dry           # the dry run: fake answers, no key
#   .\scripts\jev_record.ps1 -Mode replay -Prune           # replay, then drop unused keys
#
# Targets:
#   runner    tests/test_apply_run.py, tests/test_apply_run_boundaries.py,
#             tests/test_screening.py (the screening set's real judge) and
#             tests/test_apply_assess.py (the difficulty check's real judge);
#             the same files as RUNNER_TESTS in tests/jev_harness.py
#             (cache tests/fixtures/jev_cache/cache.json, committed)
#   matrix    scripts/apply_matrix.py --real, one run per registered flow
#             (cache tests/fixtures/jev_cache/matrix_cache.json, committed)
#   captures  tests/test_capture_reads.py over tests/fixtures/local_captures/
#             (cache and results in its _jev folder: local only, never committed)
#
# Record mode reads TYPESAFE_API_KEY from the environment, else from the one row
# in .env, and exports it to this one process only; the value is never printed.
# Every live entry point stops at the cap (AUTO_APPLY_RECORD_USD_CAP, from -Cap):
# a request whose estimated cost would pass it is never sent. A live recording
# must pass -Cap (no default, and the Python entry points refuse one without
# the variable); a dry run or a replay spends nothing and takes 0.88 (what the
# cycle's approval had left under its limit after SP8b) when it is left out.
# -Dry answers with
# the fake at each request's estimated size into a temp copy of the cache, with
# no key: the request count and the spend a recording would make. When the run
# ends, the variables this script set are removed, so a later plain pytest stays
# on the fake, and the key is dropped when this script loaded it. A recording or
# a replay of the runner or matrix target ends with scripts/jev_thresholds.py
# over its cache.
#
# -Prune (SP8, -Mode replay only, -Target runner or matrix) rewrites the
# target's cache to keep only the keys that replay used, once the replay had
# 0 misses and 0 failures (jev.prune_cache); otherwise it prints the refusal
# and leaves the cache as it was. Over time a cache picks up keys no test
# replays any more (a fixture changed, a test was removed); -Prune drops
# them. It never runs in -Mode record: a recording's cache is meant to grow.
# -Prune also refuses together with -Flows (SP8 review): a narrowed matrix run
# never touches the left-out flows' requests, so their keys would look unused
# and be dropped even though a full run still needs them. The runner target
# refuses the same way at session finish when the run was not the whole
# RUNNER_TESTS set (a -k/-m filter, a deselected item, or a node id narrower
# than a file).
#
# Pure ASCII on purpose (PowerShell 5.1).
param(
    [ValidateSet("record", "replay")]
    [string]$Mode = "record",
    [ValidateSet("runner", "matrix", "captures")]
    [string]$Target = "runner",
    [double]$Cap = 0.88,
    [string]$Cache = "",
    [string]$Flows = "",
    [string]$Json = "",
    [switch]$Dry,
    [switch]$Prune
)

if ($Prune -and $Mode -ne "replay") {
    Write-Host "-Prune is only valid with -Mode replay."
    exit 2
}
if ($Prune -and $Target -eq "captures") {
    Write-Host "-Prune supports -Target runner or -Target matrix, not captures."
    exit 2
}
if ($Prune -and $Dry) {
    Write-Host "-Prune is not valid with -Dry: a dry run estimates a recording, it replays nothing."
    exit 2
}
if ($Prune -and $Flows) {
    Write-Host "-Prune refuses together with -Flows: a narrowed matrix run never touches the flows left out, so their keys would look unused and be dropped."
    exit 2
}

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$live = ($Mode -eq "record") -and (-not $Dry)
# A live recording names its own cap: what the spend ledger has left under the
# cycle's limit. Checked before the key is read. A NaN cap never stops.
if ($live -and -not $PSBoundParameters.ContainsKey("Cap")) {
    Write-Host "A live recording needs -Cap <USD>: at most what the spend ledger has left under the limit."
    exit 2
}
if ([double]::IsNaN($Cap) -or [double]::IsInfinity($Cap) -or $Cap -le 0) {
    Write-Host "-Cap must be a finite USD amount above 0."
    exit 2
}
$loadedKey = $false
if ($live -and -not $env:TYPESAFE_API_KEY) {
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

$capText = $Cap.ToString([System.Globalization.CultureInfo]::InvariantCulture)
$env:AUTO_APPLY_RECORD_USD_CAP = $capText
$env:QT_QPA_PLATFORM = "offscreen"
$set = @("AUTO_APPLY_RECORD_USD_CAP")
$code = 0
try {
    if ($Target -eq "runner") {
        $env:AUTO_APPLY_TEST_JEV = $Mode
        $set += "AUTO_APPLY_TEST_JEV"
        if ($Dry) { $env:AUTO_APPLY_RECORD_DRY = "1"; $set += "AUTO_APPLY_RECORD_DRY" }
        if ($Cache) { $env:AUTO_APPLY_JEV_CACHE = $Cache; $set += "AUTO_APPLY_JEV_CACHE" }
        if ($Prune) { $env:AUTO_APPLY_JEV_PRUNE = "1"; $set += "AUTO_APPLY_JEV_PRUNE" }
        Write-Host "jev $Mode over the runner tests (cap $capText USD, dry $Dry)"
        python -m pytest tests/test_apply_run.py tests/test_apply_run_boundaries.py tests/test_screening.py tests/test_apply_assess.py -q
        $code = $LASTEXITCODE
        if (-not $Dry) {
            $thresholdArgs = @()
            if ($Cache) { $thresholdArgs = @("--cache", $Cache) }
            python scripts/jev_thresholds.py @thresholdArgs
        }
    }
    elseif ($Target -eq "matrix") {
        $real = $Mode
        if ($Dry) { $real = "dry" }
        $matrixArgs = @("scripts/apply_matrix.py", "--real", $real, "--verbose")
        if ($Mode -eq "replay") { $matrixArgs += @("--seeds", "0") }
        if ($Cache) { $matrixArgs += @("--real-cache", $Cache) }
        if ($Flows) { $matrixArgs += @("--flows", $Flows) }
        if ($Json) { $matrixArgs += @("--json", $Json) }
        if ($Prune) { $matrixArgs += @("--real-prune") }
        Write-Host "jev $real over the flow matrix (cap $capText USD)"
        python @matrixArgs
        $code = $LASTEXITCODE
        if (-not $Dry) {
            $matrixCache = "tests/fixtures/jev_cache/matrix_cache.json"
            if ($Cache) { $matrixCache = $Cache }
            python scripts/jev_thresholds.py --cache $matrixCache --no-outcomes
        }
    }
    else {
        $captureMode = $Mode
        if ($Dry) { $captureMode = "dry" }
        $env:AUTO_APPLY_CAPTURE_JEV = $captureMode
        $set += "AUTO_APPLY_CAPTURE_JEV"
        Write-Host "jev $captureMode over the local captures' page reads (cap $capText USD)"
        python -m pytest tests/test_capture_reads.py -q -rsx
        $code = $LASTEXITCODE
        $summary = "tests/fixtures/local_captures/_jev/summary.txt"
        if (Test-Path $summary) { Get-Content $summary }
    }
}
finally {
    foreach ($name in $set) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
    if ($loadedKey) { Remove-Item Env:TYPESAFE_API_KEY -ErrorAction SilentlyContinue }
}
exit $code
