$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if ($root -notmatch '\\candidates\\') { throw 'Only an isolated candidates directory is allowed.' }
Set-Location -LiteralPath $root
Get-ChildItem Env:HSC_* | ForEach-Object { Remove-Item "Env:$($_.Name)" }
Get-ChildItem Env: | Where-Object { $_.Name -match 'API_KEY|ACCESS_KEY|SECRET|TOKEN|CREDENTIAL|PASSWORD|KEYCHAIN|^NODE_OPTIONS$|^ELECTRON_RUN_AS_NODE$|^ELECTRON_RENDERER_URL$' } | ForEach-Object { Remove-Item "Env:$($_.Name)" }
$evidence = Join-Path $root ('desktop-check-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss'))
$env:HSC_RUNTIME_MODE = 'trial'
$env:HSC_TRIAL_PROFILE_ID = 'desktop-smoke-' + [Guid]::NewGuid().ToString('N')
$profile = Join-Path ([Environment]::GetFolderPath('ApplicationData')) ('hsc-trial-profiles\' + $env:HSC_TRIAL_PROFILE_ID)
$env:HSC_DATA_DIR = Join-Path $profile 'family-data'
$env:HSC_PILOT_TEST_EMAIL_ENABLED = '0'
$env:HTTP_PROXY = 'http://127.0.0.1:9'
$env:HTTPS_PROXY = 'http://127.0.0.1:9'
$env:ALL_PROXY = 'http://127.0.0.1:9'
$env:NO_PROXY = '127.0.0.1,localhost,::1'
foreach ($directory in @($evidence)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
$apps = @(Get-ChildItem 'release/win-unpacked' -Filter '*.exe' -File)
if ($apps.Count -ne 1) { throw 'Expected one unpacked desktop executable.' }
$checks = @()
$failure = $null
try {
foreach ($flag in @('model-settings-smoke', 'hardware-inventory-smoke')) {
    $process = Start-Process -FilePath $apps[0].FullName -ArgumentList ('--' + $flag) -PassThru -WindowStyle Hidden -WorkingDirectory $apps[0].DirectoryName -RedirectStandardOutput (Join-Path $evidence ($flag + '-stdout.log')) -RedirectStandardError (Join-Path $evidence ($flag + '-stderr.log'))
    $null = $process.Handle
    try {
        if (-not $process.WaitForExit(90000)) { throw "$flag timed out" }
        $process.Refresh()
        if ($process.ExitCode -ne 0) { throw "$flag exited with $($process.ExitCode)" }
        if (-not (Test-Path (Join-Path $env:HSC_DATA_DIR ($flag + '.json')))) { throw "$flag evidence missing" }
        Copy-Item (Join-Path $env:HSC_DATA_DIR ($flag + '.json')) (Join-Path $evidence ($flag + '.json'))
        $checks += $flag
    } finally {
        if (-not $process.HasExited) { & taskkill.exe /PID $process.Id /T /F | Out-Null; $process.WaitForExit() }
    }
}
} catch { $failure = $_.Exception.Message }
@{status=$(if ($failure) {'failed'} else {'passed'}); error=$failure; checks=$checks; evidence=$evidence; trial_data=$env:HSC_DATA_DIR; session_id=[System.Diagnostics.Process]::GetCurrentProcess().SessionId; installed=$false; actual_voice_or_camera_stream_verified=$false} |
    ConvertTo-Json | Set-Content -Encoding UTF8 'candidate-desktop-status.json'
if ($failure) { throw $failure }
