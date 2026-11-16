param([switch]$ProbeProviderDurability, [switch]$ProbeKeyLoss, [ValidatePattern('^[a-zA-Z0-9_-]{0,32}$')][string]$RunLabel = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if ($root -notmatch '\\candidates\\') { throw 'Only an isolated candidates directory is allowed.' }
Set-Location -LiteralPath $root
$statusStem = if ($ProbeKeyLoss) {'candidate-desktop-key-loss'} elseif ($ProbeProviderDurability) {'candidate-desktop-ui-provider-probe'} else {'candidate-desktop-ui'}
$statusFile = $statusStem + $(if ($RunLabel) {'-' + $RunLabel} else {''}) + '-status.json'
if (Test-Path $statusFile) { throw 'Existing acceptance status cannot be overwritten' }
Get-ChildItem Env:HSC_* | ForEach-Object { Remove-Item "Env:$($_.Name)" }
Get-ChildItem Env: | Where-Object { $_.Name -match 'API_KEY|ACCESS_KEY|SECRET|TOKEN|CREDENTIAL|PASSWORD|KEYCHAIN|^NODE_OPTIONS$|^ELECTRON_RUN_AS_NODE$|^ELECTRON_RENDERER_URL$' } | ForEach-Object { Remove-Item "Env:$($_.Name)" }
$evidence = Join-Path $root ('desktop-ui-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $evidence | Out-Null
$env:HSC_RUNTIME_MODE = 'trial'
$env:HSC_TRIAL_PROFILE_ID = 'desktop-ui-' + [Guid]::NewGuid().ToString('N')
$profile = Join-Path ([Environment]::GetFolderPath('ApplicationData')) ('hsc-trial-profiles\' + $env:HSC_TRIAL_PROFILE_ID)
$env:HSC_DATA_DIR = Join-Path $profile 'family-data'
$env:HSC_PILOT_TEST_EMAIL_ENABLED = '0'
$env:HTTP_PROXY = 'http://127.0.0.1:9'
$env:HTTPS_PROXY = 'http://127.0.0.1:9'
$env:ALL_PROXY = 'http://127.0.0.1:9'
$env:NO_PROXY = '127.0.0.1,localhost,::1'
$apps = @(Get-ChildItem 'release/win-unpacked' -Filter '*.exe' -File)
if ($apps.Count -ne 1) { throw 'Expected one packaged desktop executable.' }
$checks = @()
$failure = $null
$originalKey = Join-Path $evidence 'original-media-key.bin'
$keyPath = Join-Path $profile 'data-encryption-key.bin'
$databaseBeforeLoss = @()
$databaseAfterLoss = @()
try {
foreach ($phase in $(if ($ProbeKeyLoss) { @('setup', 'restart', 'key-loss', 'recovered') } else { @('setup', 'restart') })) {
    if ($phase -eq 'key-loss') {
        if (-not (Test-Path $keyPath) -or (Test-Path $originalKey)) { throw 'Original key missing or evidence already exists' }
        $databaseBeforeLoss = @(Get-ChildItem $env:HSC_DATA_DIR -Filter 'home-study.db*' -File | Sort-Object Name | ForEach-Object { [ordered]@{name=$_.Name; sha256=(Get-FileHash $_.FullName -Algorithm SHA256).Hash} })
        if ($databaseBeforeLoss.Count -eq 0) { throw 'Existing synthetic learning database missing' }
        Move-Item -LiteralPath $keyPath -Destination $originalKey
    }
    if ($phase -eq 'recovered') {
        if (Test-Path $keyPath) { throw 'Missing key was silently replaced; refusing overwrite' }
        $databaseAfterLoss = @(Get-ChildItem $env:HSC_DATA_DIR -Filter 'home-study.db*' -File | Sort-Object Name | ForEach-Object { [ordered]@{name=$_.Name; sha256=(Get-FileHash $_.FullName -Algorithm SHA256).Hash} })
        if (($databaseBeforeLoss | ConvertTo-Json -Compress) -ne ($databaseAfterLoss | ConvertTo-Json -Compress)) { throw 'Synthetic database changed during key loss' }
        Move-Item -LiteralPath $originalKey -Destination $keyPath
    }
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop()
    $arguments = @('--remote-debugging-address=127.0.0.1', ('--remote-debugging-port=' + $port), '--use-fake-device-for-media-stream', '--mute-audio', '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=<local>')
    $process = Start-Process -FilePath $apps[0].FullName -ArgumentList $arguments -PassThru -WorkingDirectory $apps[0].DirectoryName -RedirectStandardOutput (Join-Path $evidence ($phase + '-stdout.log')) -RedirectStandardError (Join-Path $evidence ($phase + '-stderr.log'))
    $null = $process.Handle
    try {
        & node.exe (Join-Path $PSScriptRoot 'verify-windows-candidate-ui.mjs') $port (Join-Path $evidence ($phase + '.json')) $(if ($phase -eq 'recovered') {'restart'} else {$phase})
        if ($LASTEXITCODE -ne 0) { throw "$phase actual UI acceptance failed" }
        $checks += $phase
        if ($phase -eq 'setup' -and $ProbeProviderDurability) {
            $statePath = Join-Path $profile 'browser-session\Local State'
            $observations = @()
            $deadline = [DateTime]::UtcNow.AddSeconds(45)
            do {
                $present = $false
                if (Test-Path $statePath) {
                    try { $state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json; $present = [bool]$state.os_crypt.encrypted_key } catch { }
                }
                $observations += @{utc=[DateTime]::UtcNow.ToString('o'); protected_provider_key_on_disk=$present}
                if (-not $present) { Start-Sleep -Milliseconds 500 }
            } while (-not $present -and [DateTime]::UtcNow -lt $deadline)
            $observations | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $evidence 'provider-persistence-observations.json')
            if (-not $present) { throw 'Diagnostic: protected provider key was not persisted within 45 seconds' }
        }
    } finally {
        if (-not $process.HasExited) { & taskkill.exe /PID $process.Id /T /F | Out-Null; $process.WaitForExit(15000) | Out-Null }
        $process.Refresh()
        if (-not $process.HasExited) { throw 'Owned desktop process could not be stopped' }
    }
}
} catch { $failure = $_.Exception.Message } finally {
    if ((Test-Path $originalKey) -and -not (Test-Path $keyPath)) { Move-Item -LiteralPath $originalKey -Destination $keyPath }
}
@{status=$(if ($failure) {'failed'} else {'passed'}); error=$failure; checks=$checks; evidence=$evidence; trial_data=$env:HSC_DATA_DIR; database_unchanged_during_key_loss=($ProbeKeyLoss -and $checks -contains 'recovered'); session_id=[System.Diagnostics.Process]::GetCurrentProcess().SessionId; installed=$false; actual_voice_or_camera_stream_verified=$false; fake_media_to_prevent_family_capture=$true} |
    ConvertTo-Json | Set-Content -Encoding UTF8 $statusFile
if ($failure) { throw $failure }
