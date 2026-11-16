param(
  [string]$Repo = 'C:\HSC\candidates\full-product-20260925',
  [int]$DebugPort = 9222
)
$ErrorActionPreference = 'Stop'
throw 'SINGLE_FILE_AGENT_UPDATE_DISABLED_USE_FULL_INSTALLER'
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\home-study-companion'
$appExe = Get-ChildItem -LiteralPath $installDir -Filter '*.exe' -File |
  Where-Object Name -NotMatch 'uninstall' | Select-Object -First 1
if (-not $appExe) { throw 'Installed application executable not found.' }
$builtAgent = Join-Path $Repo 'dist\home-study-local-agent.exe'
$installedAgent = Join-Path $installDir 'resources\local-agent\home-study-local-agent.exe'
if (-not (Test-Path -LiteralPath $builtAgent)) { throw 'Built local agent not found.' }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupDir = Join-Path 'C:\HSC\device-evidence' "pre-agent-hotfix-$stamp"
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$database = Join-Path $env:APPDATA 'home-study-companion\family-data\home-study.db'
$databaseBackup = Join-Path $backupDir 'home-study.db'
$python = Join-Path $Repo '.venv\Scripts\python.exe'
$backupCode = @'
import sqlite3, sys
source = sqlite3.connect('file:' + sys.argv[1].replace('\\', '/') + '?mode=ro', uri=True)
target = sqlite3.connect(sys.argv[2])
source.backup(target)
assert target.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
target.close()
source.close()
'@
& $python -c $backupCode $database $databaseBackup
if ($LASTEXITCODE -ne 0) { throw 'Database backup failed.' }

$running = @(Get-Process | Where-Object { $_.Path -eq $appExe.FullName })
if ($running.Count -gt 0) {
  $main = $running | Where-Object MainWindowHandle -ne 0 | Select-Object -First 1
  if (-not $main) { throw 'Running desktop is not visible to this session; request renderer close first.' }
  [void]$main.CloseMainWindow()
  $deadline = (Get-Date).AddSeconds(30)
  do {
    Start-Sleep -Milliseconds 300
    $remaining = @(Get-Process | Where-Object { $_.Path -eq $appExe.FullName })
  } while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline)
  if ($remaining.Count -gt 0) { throw 'Desktop did not exit gracefully; hotfix not installed.' }
}

Copy-Item -LiteralPath $installedAgent -Destination (Join-Path $backupDir 'home-study-local-agent.exe')
Copy-Item -LiteralPath $builtAgent -Destination $installedAgent -Force
$builtHash = (Get-FileHash -LiteralPath $builtAgent -Algorithm SHA256).Hash
$installedHash = (Get-FileHash -LiteralPath $installedAgent -Algorithm SHA256).Hash
if ($builtHash -ne $installedHash) { throw 'Installed local agent hash mismatch.' }

Start-Process -FilePath $appExe.FullName -ArgumentList @(
  "--remote-debugging-port=$DebugPort", '--remote-debugging-address=127.0.0.1'
)
@{
  installed_agent_sha256 = $installedHash
  database_backup = $databaseBackup
  prior_agent_backup = (Join-Path $backupDir 'home-study-local-agent.exe')
  graceful_exit = $true
  launched = $true
  deployed_at = [DateTime]::UtcNow.ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backupDir 'deployment.json') -Encoding UTF8
Write-Output "DEPLOY_OK evidence=$backupDir sha256=$installedHash"
