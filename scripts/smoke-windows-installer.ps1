$ErrorActionPreference = "Stop"

$installer = Get-ChildItem "release" -Filter "*.exe" |
  Where-Object { $_.Name -notmatch "Uninstall" } |
  Sort-Object Length -Descending |
  Select-Object -First 1
if (-not $installer) {
  throw "No NSIS installer was found under release/."
}

$runnerTemp = if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
  [System.IO.Path]::GetTempPath()
} else {
  $env:RUNNER_TEMP
}
$smokeId = [Guid]::NewGuid().ToString('N')
$installDir = Join-Path $runnerTemp "hsc-installed-$smokeId"
$dataDir = Join-Path $runnerTemp "hsc-installed-data-$smokeId"
$formalInstallDir = Join-Path $env:LOCALAPPDATA "Programs\home-study-companion"
. (Join-Path $PSScriptRoot 'windows-installation-guard.ps1')
$packageMetadata = Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\package.json') -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
$registryReader = { Get-HscInstallRegistrySnapshot }
$pathExists = {
  param($path)
  try { Get-Item -LiteralPath $path -Force -ErrorAction Stop | Out-Null; return $true }
  catch [System.Management.Automation.ItemNotFoundException] { return $false }
}
Assert-HscIsolatedInstallSafe -ReadRegistry $registryReader -PathExists $pathExists `
  -AppId $packageMetadata.build.appId -NsisGuid $packageMetadata.build.nsis.guid `
  -FormalDir $formalInstallDir -InstallDir $installDir -DataDir $dataDir

function Stop-OwnedInstallProcesses([string]$ownedInstallDir) {
  $installPrefix = [System.IO.Path]::GetFullPath($ownedInstallDir).TrimEnd("\") + "\"
  $owned = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith(
      $installPrefix,
      [System.StringComparison]::OrdinalIgnoreCase
    )
  })
  foreach ($process in $owned) {
    $priorPreference = $ErrorActionPreference
    try {
      $ErrorActionPreference = "Continue"
      & taskkill.exe /PID $process.ProcessId /T /F *> $null
    } finally {
      $ErrorActionPreference = $priorPreference
    }
  }
  for ($attempt = 0; $attempt -lt 40; $attempt += 1) {
    $remaining = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
      $_.ExecutablePath -and $_.ExecutablePath.StartsWith(
        $installPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
      )
    })
    if ($remaining.Count -eq 0) {
      return
    }
    Start-Sleep -Milliseconds 250
  }
  throw "Smoke-owned installed process tree did not exit within 10 seconds."
}

if ((& $pathExists $installDir) -or (& $pathExists $dataDir)) { throw 'SMOKE_INSTALL_EXISTING_OR_UNCONFIRMED' }
New-Item -ItemType Directory -Path $dataDir -ErrorAction Stop | Out-Null
$env:HSC_DATA_DIR = $dataDir
$env:HSC_DATABASE_PATH = Join-Path $dataDir "home-study.db"

# Re-read immediately before the NSIS process in case a product was registered
# after the initial, pre-cleanup inspection.
Assert-HscTargetRegistryAbsent $registryReader
if (& $pathExists $installDir) { throw 'SMOKE_INSTALL_EXISTING_OR_UNCONFIRMED' }
$install = Start-Process -FilePath $installer.FullName -ArgumentList "/S", "/D=$installDir" -PassThru -Wait
if ($install.ExitCode -ne 0) {
  throw "NSIS installer exited with code $($install.ExitCode)."
}

$appExe = Get-ChildItem $installDir -Recurse -File -Filter "*.exe" |
  Where-Object { $_.Name -notmatch "Uninstall" } |
  Sort-Object Length -Descending |
  Select-Object -First 1
if (-not $appExe) {
  throw "The installed desktop executable was not found under $installDir."
}
$inventoryReportPath = Join-Path $dataDir "hardware-inventory-smoke.json"
$inventorySmoke = Start-Process -FilePath $appExe.FullName -ArgumentList "--hardware-inventory-smoke" -PassThru -Wait
if ($inventorySmoke.ExitCode -ne 0) {
  throw "Installed Electron hardware inventory smoke exited with code $($inventorySmoke.ExitCode)."
}
if (-not (Test-Path $inventoryReportPath)) {
  throw "Installed Electron hardware inventory smoke did not create its sanitized report."
}
$inventoryRaw = Get-Content $inventoryReportPath -Raw
$inventory = $inventoryRaw | ConvertFrom-Json
if (-not $inventory.supported -or $inventory.source -ne "windows_pnp_cim_v1") {
  throw "Installed Electron did not execute the native Windows PNP inventory path."
}
if ($inventoryRaw -match '"instanceId"|"pnp_device_id"|"serial_number"') {
  throw "Sanitized inventory report exposed a forbidden raw hardware identifier field."
}
$allowedInventoryFields = @(
  "name", "manufacturer", "driver_version", "driver_provider", "driver_date",
  "usb_vid", "usb_pid", "transport_hint", "instance_id_hash"
)
foreach ($item in @($inventory.items)) {
  $unexpectedFields = @($item.PSObject.Properties.Name | Where-Object { $_ -notin $allowedInventoryFields })
  if ($unexpectedFields.Count -gt 0) {
    throw "Sanitized inventory report contained unexpected fields: $($unexpectedFields -join ', ')."
  }
  if ($item.instance_id_hash -notmatch '^[0-9a-f]{64}$') {
    throw "Sanitized inventory report contained an invalid instance hash."
  }
}
$modelSettingsReportPath = Join-Path $dataDir "model-settings-smoke.json"
$modelSettingsSmoke = Start-Process -FilePath $appExe.FullName -ArgumentList "--model-settings-smoke" -PassThru -Wait
if ($modelSettingsSmoke.ExitCode -ne 0) {
  throw "Installed Electron model settings smoke exited with code $($modelSettingsSmoke.ExitCode)."
}
if (-not (Test-Path $modelSettingsReportPath)) {
  throw "Installed Electron model settings smoke did not create its sanitized report."
}
$modelSettingsRaw = Get-Content $modelSettingsReportPath -Raw
$modelSettingsResult = $modelSettingsRaw | ConvertFrom-Json
foreach ($field in @("safe_storage_available", "encrypted_round_trip", "plaintext_absent", "credentials_cleared")) {
  if ($modelSettingsResult.$field -ne $true) {
    throw "Installed Electron model settings smoke failed: $field."
  }
}
if (Test-Path (Join-Path $dataDir "model-settings.json")) {
  throw "Installed Electron model settings smoke left synthetic credentials behind."
}
if ($modelSettingsRaw -match "smoke-primary|smoke-fallback|encryptedApiKey") {
  throw "Installed Electron model settings smoke exposed credential material in its report."
}
$agentExe = Get-ChildItem $installDir -Recurse -Filter "home-study-local-agent.exe" |
  Select-Object -First 1
if (-not $agentExe) {
  throw "The packaged local agent resource was not installed."
}
$workerRoot = Join-Path $agentExe.DirectoryName 'smtp-worker'
$workerExe = Join-Path $workerRoot 'hsc-smtp-worker.exe'
$workerManifest = Join-Path $workerRoot 'worker-manifest.json'
if (-not (Test-Path -LiteralPath $workerExe -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $workerRoot '_internal') -PathType Container) -or
    -not (Test-Path -LiteralPath $workerManifest -PathType Leaf)) { throw 'SMTP_WORKER_PACKAGE_INCOMPLETE' }
$workerRootFull = [IO.Path]::GetFullPath($workerRoot).TrimEnd('\')
$ancestor = Get-Item -LiteralPath $workerRootFull
while ($ancestor) {
  if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'SMTP_WORKER_REPARSE_POINT' }
  $ancestor = $ancestor.Parent
}
$workerTree = @(Get-ChildItem -LiteralPath $workerRootFull -Recurse -Force)
if (@($workerTree | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) { throw 'SMTP_WORKER_REPARSE_POINT' }
$workerData = Get-Content -LiteralPath $workerManifest -Raw | ConvertFrom-Json
$workerEntries = @($workerData.files.PSObject.Properties)
if ($workerData.version -ne 1 -or $workerEntries.Count -lt 2 -or $workerEntries.Count -gt 4096) { throw 'SMTP_WORKER_MANIFEST_INVALID' }
$expectedFiles = @{}
foreach ($entry in $workerEntries) {
  if ($entry.Name -match '(^/|\\|:|(^|/)\.\.(/|$)|(^|/)\.(\/|$)|//)') { throw 'SMTP_WORKER_MANIFEST_INVALID' }
  $file = [IO.Path]::GetFullPath((Join-Path $workerRootFull $entry.Name))
  if (-not $file.StartsWith($workerRootFull + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'SMTP_WORKER_MANIFEST_INVALID' }
  $item = Get-Item -LiteralPath $file -ErrorAction Stop
  if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
      $item.Length -ne $entry.Value.size -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower() -ne $entry.Value.sha256) { throw 'SMTP_WORKER_INTEGRITY_MISMATCH' }
  $expectedFiles[$file.ToLowerInvariant()] = $true
}
foreach ($item in @($workerTree | Where-Object { -not $_.PSIsContainer -and $_.Name -ne 'worker-manifest.json' })) {
  if (-not $expectedFiles.ContainsKey($item.FullName.ToLowerInvariant())) { throw 'SMTP_WORKER_INTEGRITY_MISMATCH' }
}
$localModels = Get-ChildItem $installDir -Recurse -Directory -Filter "local-models" |
  Select-Object -First 1
if (-not $localModels) {
  throw "The packaged local model directory was not installed."
}
$modelManifestPath = Join-Path $localModels.FullName "model-manifest.json"
if (-not (Test-Path $modelManifestPath)) {
  throw "The packaged ASR model manifest was not installed."
}
$modelManifest = Get-Content $modelManifestPath -Raw | ConvertFrom-Json
$asrModelDir = Join-Path $localModels.FullName $modelManifest.model_dir
foreach ($modelProperty in $modelManifest.files.PSObject.Properties) {
  $modelFile = $modelProperty.Name
  $expected = $modelProperty.Value
  $modelPath = Join-Path $asrModelDir $modelFile
  if (-not (Test-Path $modelPath) -or (Get-Item $modelPath).Length -lt [long]$expected.minimum_bytes) {
    throw "Required packaged ASR model file is missing or truncated: $modelFile"
  }
  $actualHash = (Get-FileHash $modelPath -Algorithm SHA256).Hash.ToLower()
  if ($actualHash -ne $expected.sha256) {
    throw "Required packaged ASR model file failed SHA-256 verification: $modelFile"
  }
}

$existingApp = Get-CimInstance Win32_Process |
  Where-Object { $_.ExecutablePath -eq $appExe.FullName } |
  Select-Object -First 1
$appProcess = if ($existingApp) {
  Get-Process -Id $existingApp.ProcessId
} else {
  Start-Process -FilePath $appExe.FullName -PassThru
}

try {
  $windowReady = $false
  $databaseReady = $false
  for ($attempt = 0; $attempt -lt 120; $attempt += 1) {
    if ($appProcess.HasExited) {
      throw "Installed desktop app exited early with code $($appProcess.ExitCode)."
    }
    $current = Get-Process -Id $appProcess.Id -ErrorAction SilentlyContinue
    if ($current -and $current.MainWindowHandle -ne 0) {
      $windowReady = $true
    }
    if (Test-Path $env:HSC_DATABASE_PATH) {
      $databaseReady = $true
    }
    if ($windowReady -and $databaseReady) {
      break
    }
    Start-Sleep -Milliseconds 500
  }
  if (-not $windowReady) {
    throw "Installed desktop app did not expose a usable main window within 60 seconds."
  }
  if (-not $databaseReady) {
    throw "Installed desktop app did not start the local service or create SQLite within 60 seconds."
  }

  $runtimeState = & .venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'$env:HSC_DATABASE_PATH'); print(c.execute('select clean_shutdown from runtime_state where singleton_id=1').fetchone()[0]); print(c.execute('select max(version) from schema_migration').fetchone()[0]); tables=('audit_event','voice_input','agent_turn','observable_event','intervention','mistake_case','capture_artifact','model_call','document_observation_segment','daily_observation_report_snapshot'); print(c.execute('select count(*) from sqlite_master where type=? and name in ('+','.join('?'*len(tables))+')',('table',*tables)).fetchone()[0]); c.close()"
  $runtimeLines = @($runtimeState | ForEach-Object { $_.Trim() })
  if ($runtimeLines[0] -ne "0") {
    throw "The packaged local service did not record an active runtime."
  }
  if ($runtimeLines[1] -ne "99") {
    throw "The packaged local service created unexpected database schema version $($runtimeLines[1])."
  }
  if ($runtimeLines[2] -ne "10") {
    throw "The packaged local service is missing one or more durable behavior, agent, device, mistake or model audit tables."
  }

  $diagnosticRoot = Join-Path $dataDir "diagnostics"
  $diagnosticLog = Join-Path $diagnosticRoot "logs\runtime.jsonl"
  $crashDumpRoot = Join-Path $diagnosticRoot "crashes"
  $diagnosticReady = $false
  for ($attempt = 0; $attempt -lt 40; $attempt += 1) {
    if ((Test-Path $diagnosticLog) -and (Test-Path $crashDumpRoot)) {
      $diagnosticReady = $true
      break
    }
    Start-Sleep -Milliseconds 250
  }
  if (-not $diagnosticReady) {
    throw "Installed desktop app did not create its local runtime log and crash-dump directory."
  }
  $diagnosticRaw = Get-Content $diagnosticLog -Raw
  if ($diagnosticRaw -notmatch '"event":"app_process_started"' -or $diagnosticRaw -notmatch '"event":"service_ready"') {
    throw "Installed desktop diagnostics did not capture process and local-service lifecycle events."
  }
  if ($diagnosticRaw -match 'sk-[A-Za-z0-9._-]{8,}' -or $diagnosticRaw -match '"(?:apiKey|sessionToken|authorization|rawAudio|rawImage|rawVideo)"\s*:\s*"(?!\[REDACTED\])') {
    throw "Installed desktop diagnostics exposed a credential or raw-media field."
  }

  $automaticBackupReady = $false
  $automaticBackupWaitSeconds = 60
  $automaticBackupPollMilliseconds = 250
  $automaticBackupAttempts = [int][Math]::Ceiling(
    ($automaticBackupWaitSeconds * 1000) / $automaticBackupPollMilliseconds
  )
  for ($attempt = 0; $attempt -lt $automaticBackupAttempts; $attempt += 1) {
    $backupCount = & .venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'$env:HSC_DATABASE_PATH'); print(c.execute('select count(*) from backup_manifest where reason=? and integrity_status=?', ('daily_automatic', 'ok')).fetchone()[0]); c.close()"
    if ([int]$backupCount -gt 0) {
      $automaticBackupReady = $true
      break
    }
    Start-Sleep -Milliseconds $automaticBackupPollMilliseconds
  }
  if (-not $automaticBackupReady) {
    throw "The packaged local service did not create its startup daily backup within $automaticBackupWaitSeconds seconds."
  }

  $runKey = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -ErrorAction SilentlyContinue
  $autoLaunch = $runKey.PSObject.Properties |
    Where-Object { $_.Value -is [string] -and $_.Value.Contains($appExe.FullName) } |
    Select-Object -First 1
  if (-not $autoLaunch) {
    throw "First packaged launch did not register Windows login auto-start."
  }

  Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class HscWindowClose {
  [DllImport("user32.dll", SetLastError = true)]
  public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
}
"@
  $current = Get-Process -Id $appProcess.Id
  if (-not [HscWindowClose]::PostMessage($current.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)) {
    throw "Failed to send WM_CLOSE to the installed desktop app."
  }
  if (-not $appProcess.WaitForExit(20000)) {
    throw "Installed desktop app did not exit within 20 seconds after WM_CLOSE."
  }

  $cleanShutdown = & .venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'$env:HSC_DATABASE_PATH'); print(c.execute('select clean_shutdown from runtime_state where singleton_id=1').fetchone()[0]); c.close()"
  if ($cleanShutdown.Trim() -ne "1") {
    throw "Installed desktop app exited without a clean local-service shutdown."
  }
} finally {
  Stop-OwnedInstallProcesses $installDir
}

$upgradeSentinelId = "upgrade-smoke-$([Guid]::NewGuid().ToString('N'))"
$upgradeHomeworkId = "upgrade-homework-$([Guid]::NewGuid().ToString('N'))"
$upgradePlanItemId = "upgrade-plan-item-$([Guid]::NewGuid().ToString('N'))"
$prepareUpgrade = @"
import sqlite3
connection = sqlite3.connect(r'$env:HSC_DATABASE_PATH')
connection.execute("INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, 'system', 'upgrade_smoke_sentinel', NULL, '{}', datetime('now'))", ('$upgradeSentinelId',))
connection.execute("""
INSERT INTO homework_item(
  homework_id, child_id, session_id, subject, task_type, title, expected_minutes,
  confirmation_status, status, created_at, client_action_id
) VALUES (?, 'child-junior-3', 'upgrade-smoke-session', 'math', 'written', 'upgrade status repair smoke',
          20, 'child_confirmed', 'pending', datetime('now'), 'upgrade_smoke_homework_status')
""", ('$upgradeHomeworkId',))
connection.execute("""
INSERT INTO plan_item(
  plan_item_id, plan_id, homework_id, subject, title, expected_minutes, actual_minutes,
  priority, status, position, source_type
) VALUES (?, 'upgrade-smoke-plan', ?, 'math', 'upgrade status repair smoke', 20, 20,
          1, 'completed', 1, 'school_homework')
""", ('$upgradePlanItemId', '$upgradeHomeworkId'))
connection.execute("DROP INDEX IF EXISTS idx_deletion_job_client_action")
connection.execute("DROP TABLE IF EXISTS child_safety_event")
connection.execute("ALTER TABLE deletion_job RENAME TO deletion_job_v48")
connection.execute("""
CREATE TABLE deletion_job (
  deletion_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  status TEXT NOT NULL,
  deleted_counts_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT
)
""")
connection.execute("""
INSERT INTO deletion_job(
  deletion_id, child_id, scope, status, deleted_counts_json, created_at, completed_at
)
SELECT deletion_id, child_id, scope, status, deleted_counts_json, created_at, completed_at
FROM deletion_job_v48
""")
connection.execute("DROP TABLE deletion_job_v48")
connection.execute("DELETE FROM schema_migration WHERE version >= 48")
connection.commit()
connection.close()
"@
$prepareUpgradePath = Join-Path $dataDir "prepare-upgrade-smoke.py"
Set-Content -Path $prepareUpgradePath -Value $prepareUpgrade -Encoding ASCII
& .venv\Scripts\python.exe $prepareUpgradePath
if ($LASTEXITCODE -ne 0) {
  throw "Failed to prepare the synthetic v47 covering-upgrade database."
}
Remove-Item $prepareUpgradePath -Force

$upgradeInstall = Start-Process -FilePath $installer.FullName -ArgumentList "/S", "/D=$installDir" -PassThru -Wait
if ($upgradeInstall.ExitCode -ne 0) {
  throw "NSIS covering upgrade exited with code $($upgradeInstall.ExitCode)."
}
$upgradedAppExe = Get-ChildItem $installDir -Recurse -File -Filter "*.exe" |
  Where-Object { $_.Name -notmatch "Uninstall" } |
  Sort-Object Length -Descending |
  Select-Object -First 1
if (-not $upgradedAppExe) {
  throw "Desktop executable was missing after the covering upgrade."
}
$upgradedProcess = Start-Process -FilePath $upgradedAppExe.FullName -PassThru
try {
  $upgradeWindowReady = $false
  for ($attempt = 0; $attempt -lt 120; $attempt += 1) {
    if ($upgradedProcess.HasExited) {
      throw "Upgraded desktop app exited early with code $($upgradedProcess.ExitCode)."
    }
    $currentUpgrade = Get-Process -Id $upgradedProcess.Id -ErrorAction SilentlyContinue
    if ($currentUpgrade -and $currentUpgrade.MainWindowHandle -ne 0) {
      $upgradeWindowReady = $true
      break
    }
    Start-Sleep -Milliseconds 500
  }
  if (-not $upgradeWindowReady) {
    throw "Upgraded desktop app did not expose a usable window within 60 seconds."
  }

  $upgradeStateScript = @"
import sqlite3
connection = sqlite3.connect(r'$env:HSC_DATABASE_PATH')
print(connection.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM audit_event WHERE audit_id = ? AND action = 'upgrade_smoke_sentinel'", ('$upgradeSentinelId',)).fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM pragma_table_info('deletion_job') WHERE name IN ('client_action_id', 'target_type', 'residual_paths_json', 'verification_json', 'updated_at')").fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name = 'idx_deletion_job_client_action'").fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM pragma_table_info('child_safety_event') WHERE name IN ('risk_level', 'category', 'status', 'guidance_json', 'acknowledgement_action_id')").fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM homework_item WHERE homework_id = ? AND status = 'completed'", ('$upgradeHomeworkId',)).fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'period_report_refresh_state'").fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM pragma_table_info('period_report') WHERE name = 'updated_at'").fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'observation_calibration_run'").fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'document_observation_segment'").fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'daily_observation_report_snapshot'").fetchone()[0])
connection.close()
"@
  $upgradeStatePath = Join-Path $dataDir "verify-upgrade-state-smoke.py"
  Set-Content -Path $upgradeStatePath -Value $upgradeStateScript -Encoding ASCII
  $upgradeState = & .venv\Scripts\python.exe $upgradeStatePath
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to inspect the covering-upgrade database state."
  }
  Remove-Item $upgradeStatePath -Force
  $upgradeLines = @($upgradeState | ForEach-Object { $_.Trim() })
  if ($upgradeLines[0] -ne "99") {
    throw "Covering upgrade did not migrate SQLite to schema 95."
  }
  if ($upgradeLines[1] -ne "1") {
    throw "Covering upgrade did not preserve the durable family-data sentinel."
  }
  if ($upgradeLines[2] -ne "5" -or $upgradeLines[3] -ne "1") {
    throw "Covering upgrade did not migrate the v47 deletion job table and idempotency index."
  }
  if ($upgradeLines[4] -ne "5") {
    throw "Covering upgrade did not create the child safety event schema."
  }
  if ($upgradeLines[5] -ne "1") {
    throw "Covering upgrade did not repair the completed homework status."
  }
  if ($upgradeLines[6] -ne "1" -or $upgradeLines[7] -ne "1") {
    throw "Covering upgrade did not create the automatic period-report refresh schema."
  }
  if ($upgradeLines[8] -ne "1") {
    throw "Covering upgrade did not create the observation calibration schema."
  }
  if ($upgradeLines[9] -ne "1") {
    throw "Covering upgrade did not create the document observation segment schema."
  }
  if ($upgradeLines[10] -ne "1") {
    throw "Covering upgrade did not create the frozen daily observation snapshot schema."
  }

  $upgradeBackupDir = Join-Path $dataDir "upgrade-backups"
  $upgradeMetadataPath = Get-ChildItem $upgradeBackupDir -Filter "pre-upgrade-*.json" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
  if (-not $upgradeMetadataPath) {
    throw "Covering upgrade did not create pre-upgrade recovery metadata."
  }
  $upgradeMetadata = Get-Content $upgradeMetadataPath.FullName -Raw | ConvertFrom-Json
  if ($upgradeMetadata.status -ne "completed" -or $upgradeMetadata.source_schema_version -ne 47 -or $upgradeMetadata.target_schema_version -ne 99) {
    throw "Covering upgrade recovery metadata did not record a completed 47-to-95 migration."
  }
  $upgradeSnapshot = Join-Path $upgradeBackupDir $upgradeMetadata.backup_file
  if (-not (Test-Path $upgradeSnapshot)) {
    throw "Covering upgrade did not retain its pre-upgrade database snapshot."
  }
  $upgradeSnapshotHash = (Get-FileHash $upgradeSnapshot -Algorithm SHA256).Hash.ToLower()
  if ($upgradeSnapshotHash -ne $upgradeMetadata.sha256) {
    throw "Covering upgrade snapshot failed its recorded SHA-256 check."
  }

  $currentUpgrade = Get-Process -Id $upgradedProcess.Id
  if (-not [HscWindowClose]::PostMessage($currentUpgrade.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)) {
    throw "Failed to send WM_CLOSE to the upgraded desktop app."
  }
  if (-not $upgradedProcess.WaitForExit(20000)) {
    throw "Upgraded desktop app did not exit within 20 seconds after WM_CLOSE."
  }
  $upgradedCleanShutdown = & .venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'$env:HSC_DATABASE_PATH'); print(c.execute('select clean_shutdown from runtime_state where singleton_id=1').fetchone()[0]); c.close()"
  if ($upgradedCleanShutdown.Trim() -ne "1") {
    throw "Upgraded desktop app exited without a clean local-service shutdown."
  }
} finally {
  Stop-OwnedInstallProcesses $installDir
}

$uninstaller = Get-ChildItem $installDir -Recurse -Filter "*.exe" |
  Where-Object { $_.Name -match "Uninstall" } |
  Select-Object -First 1
if (-not $uninstaller) {
  throw "The NSIS uninstaller was not found."
}
$uninstall = Start-Process -FilePath $uninstaller.FullName -ArgumentList "/S" -PassThru -Wait
if ($uninstall.ExitCode -ne 0) {
  throw "NSIS uninstaller exited with code $($uninstall.ExitCode)."
}
Start-Sleep -Seconds 2
if (Test-Path $appExe.FullName) {
  throw "Desktop executable remained after silent uninstall."
}
$runKeyAfterUninstall = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -ErrorAction SilentlyContinue
$staleAutoLaunch = $runKeyAfterUninstall.PSObject.Properties |
  Where-Object { $_.Value -is [string] -and $_.Value.Contains($appExe.FullName) } |
  Select-Object -First 1
if ($staleAutoLaunch) {
  throw "Windows login auto-start entry remained after uninstall."
}
if (-not (Test-Path $env:HSC_DATABASE_PATH)) {
  throw "Uninstall unexpectedly deleted the family's durable local database."
}

Write-Host "Windows installer install, safe model storage, covering upgrade, pre-upgrade recovery point, persistence, graceful close, and uninstall smoke passed."
