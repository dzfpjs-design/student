$ErrorActionPreference = "Stop"

$exe = Resolve-Path "dist/home-study-local-agent.exe"
$port = 18765
$runnerTemp = if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
  [System.IO.Path]::GetTempPath()
} else {
  $env:RUNNER_TEMP
}
$dataDir = Join-Path $runnerTemp "home-study-agent-smoke"
$sessionToken = "windows-ci-session-token"

Remove-Item $dataDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $dataDir | Out-Null
$env:HSC_DATA_DIR = $dataDir
$env:HSC_DATABASE_PATH = Join-Path $dataDir "home-study.db"
$env:HSC_SESSION_TOKEN = $sessionToken
$env:HSC_DATA_ENCRYPTION_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

$process = Start-Process -FilePath $exe -ArgumentList "--host", "127.0.0.1", "--port", $port -PassThru -WindowStyle Hidden
try {
  $healthy = $false
  for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
    if ($process.HasExited) {
      throw "Packaged local agent exited early with code $($process.ExitCode)."
    }
    try {
      $response = Invoke-RestMethod -Uri "http://127.0.0.1:$port/v1/health" -Headers @{ "X-HSC-Session" = $sessionToken } -TimeoutSec 2
      if ($response.status -eq "ok") {
        $healthy = $true
        break
      }
    } catch {
      Start-Sleep -Milliseconds 500
    }
  }
  if (-not $healthy) {
    throw "Packaged local agent did not become healthy within 30 seconds."
  }
  $parentSetup = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/v1/parent/setup" -Headers @{ "X-HSC-Session" = $sessionToken } -ContentType "application/json" -Body '{"pin":"2468"}' -TimeoutSec 5
  $dashboard = Invoke-RestMethod -Uri "http://127.0.0.1:$port/v1/parent/dashboard" -Headers @{ "X-HSC-Session" = $sessionToken; "X-HSC-Parent" = $parentSetup.parent_token } -TimeoutSec 5
  if (-not $dashboard.sensor_release_platform_eligible) {
    throw "Packaged local agent did not expose the Windows-only sensor release verification gate."
  }
  & .venv\Scripts\python.exe scripts/verify-parent-lockout.py `
    --base-url "http://127.0.0.1:$port" `
    --session-token $sessionToken `
    --correct-pin "2468" `
    --phase lock
  if ($LASTEXITCODE -ne 0) {
    throw "Packaged local agent failed the parent PIN lockout phase."
  }
  if (-not (Test-Path $env:HSC_DATABASE_PATH)) {
    throw "Packaged local agent was healthy but did not create its durable SQLite database."
  }
  $schemaVersion = & .venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'$env:HSC_DATABASE_PATH'); print(c.execute('select max(version) from schema_migration').fetchone()[0]); c.close()"
  if ($schemaVersion.Trim() -ne "99") {
    throw "Packaged local agent created unexpected database schema version $($schemaVersion.Trim())."
  }
  $observationTable = & .venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'$env:HSC_DATABASE_PATH'); print(c.execute('SELECT COUNT(*) FROM sqlite_master WHERE type=? AND name=?',('table','document_observation_segment')).fetchone()[0]); c.close()"
  if ($LASTEXITCODE -ne 0 -or $observationTable.Trim() -ne "1") {
    throw "Packaged local agent did not create the document observation segment schema."
  }
  $snapshotTable = & .venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'$env:HSC_DATABASE_PATH'); print(c.execute('SELECT COUNT(*) FROM sqlite_master WHERE type=? AND name=?',('table','daily_observation_report_snapshot')).fetchone()[0]); c.close()"
  if ($LASTEXITCODE -ne 0 -or $snapshotTable.Trim() -ne "1") {
    throw "Packaged local agent did not create the frozen daily observation snapshot schema."
  }
  $automaticBackupReady = $false
  for ($attempt = 0; $attempt -lt 40; $attempt += 1) {
    $backupCount = & .venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'$env:HSC_DATABASE_PATH'); print(c.execute('select count(*) from backup_manifest').fetchone()[0]); c.close()"
    if ([int]$backupCount -gt 0) {
      $automaticBackupReady = $true
      break
    }
    Start-Sleep -Milliseconds 250
  }
  if (-not $automaticBackupReady) {
    throw "Packaged local agent did not create its startup recovery point."
  }
  Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/v1/runtime/shutdown" -Headers @{ "X-HSC-Session" = $sessionToken } -TimeoutSec 2 | Out-Null
  if (-not $process.WaitForExit(5000)) {
    throw "Packaged local agent did not exit gracefully within 5 seconds."
  }
  $cleanShutdown = & .venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'$env:HSC_DATABASE_PATH'); print(c.execute('select clean_shutdown from runtime_state where singleton_id=1').fetchone()[0]); c.close()"
  if ($cleanShutdown.Trim() -ne "1") {
    throw "Packaged local agent exited but did not persist a clean runtime shutdown."
  }

  $restartProcess = Start-Process -FilePath $exe -ArgumentList "--host", "127.0.0.1", "--port", $port -PassThru -WindowStyle Hidden
  try {
    $restartHealthy = $false
    for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
      if ($restartProcess.HasExited) {
        throw "Restarted packaged local agent exited early with code $($restartProcess.ExitCode)."
      }
      try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$port/v1/health" -Headers @{ "X-HSC-Session" = $sessionToken } -TimeoutSec 2
        if ($response.status -eq "ok") {
          $restartHealthy = $true
          break
        }
      } catch {
        Start-Sleep -Milliseconds 500
      }
    }
    if (-not $restartHealthy) {
      throw "Restarted packaged local agent did not become healthy within 30 seconds."
    }
    & .venv\Scripts\python.exe scripts/verify-parent-lockout.py `
      --base-url "http://127.0.0.1:$port" `
      --session-token $sessionToken `
      --correct-pin "2468" `
      --phase assert-locked
    if ($LASTEXITCODE -ne 0) {
      throw "Packaged local agent did not preserve the parent PIN lock across restart."
    }
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/v1/runtime/shutdown" -Headers @{ "X-HSC-Session" = $sessionToken } -TimeoutSec 2 | Out-Null
    if (-not $restartProcess.WaitForExit(5000)) {
      throw "Restarted packaged local agent did not exit gracefully within 5 seconds."
    }
    $restartCleanShutdown = & .venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'$env:HSC_DATABASE_PATH'); print(c.execute('select clean_shutdown from runtime_state where singleton_id=1').fetchone()[0]); c.close()"
    if ($restartCleanShutdown.Trim() -ne "1") {
      throw "Restarted packaged local agent exited without persisting a clean runtime shutdown."
    }
  } finally {
    if (-not $restartProcess.HasExited) {
      Stop-Process -Id $restartProcess.Id -Force
      $restartProcess.WaitForExit()
    }
  }
  Write-Host "Packaged Windows local agent health, schema, automatic backup, parent lockout persistence, and graceful shutdown smoke passed."
} finally {
  if (-not $process.HasExited) {
    Stop-Process -Id $process.Id -Force
    $process.WaitForExit()
  }
}
