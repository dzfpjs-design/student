param([string]$Repo = "C:\HSC\source-20260904-1250")
$ErrorActionPreference = "Stop"
$installDir = Join-Path $env:LOCALAPPDATA "Programs\home-study-companion"
$appExe = Get-ChildItem $installDir -Filter *.exe | Where-Object Name -NotMatch "uninstall" | Select-Object -First 1
if (-not $appExe) { throw "Existing installed application not found" }
$installer = Get-ChildItem (Join-Path $Repo "release") -Filter "*Setup*.exe" | Where-Object Name -NotMatch "__uninstaller" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $installer) { throw "New installer not found" }
$backupDir = Join-Path "C:\HSC\device-evidence" ("pre-memory-upgrade-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory $backupDir | Out-Null
$database = Join-Path $env:APPDATA "home-study-companion\family-data\home-study.db"
$backup = Join-Path $backupDir "home-study.db"
$python = Join-Path $Repo ".venv\Scripts\python.exe"
$backupCode = @'
import sqlite3,sys
from pathlib import Path
source=sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+'?mode=ro',uri=True)
target=sqlite3.connect(sys.argv[2])
source.backup(target)
assert target.execute('PRAGMA quick_check').fetchone()[0]=='ok'
target.close()
source.close()
'@
& $python -c $backupCode $database $backup
if ($LASTEXITCODE -ne 0) { throw "Database backup failed; installation not started" }
$running = @(Get-Process | Where-Object { $_.Path -eq $appExe.FullName })
foreach ($process in $running) {
  if ($process.MainWindowHandle -ne 0) { [void]$process.CloseMainWindow() }
}
$deadline = (Get-Date).AddSeconds(30)
do {
  Start-Sleep -Milliseconds 300
  $remaining = @(Get-Process | Where-Object { $_.Path -eq $appExe.FullName })
} while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline)
if ($remaining.Count -gt 0) { throw "Application did not exit gracefully; installation not started" }
$install = Start-Process -FilePath $installer.FullName -ArgumentList "/S" -Wait -PassThru
if ($install.ExitCode -ne 0) { throw "Installer failed: $($install.ExitCode); backup retained at $backupDir" }
$packagedAgent = Join-Path $Repo "dist\home-study-local-agent.exe"
$installedAgent = Join-Path $installDir "resources\local-agent\home-study-local-agent.exe"
if ((Get-FileHash $packagedAgent).Hash -ne (Get-FileHash $installedAgent).Hash) { throw "Installed agent differs from verified build" }
if ((Get-FileHash (Join-Path $Repo "release\win-unpacked\resources\app.asar")).Hash -ne (Get-FileHash (Join-Path $installDir "resources\app.asar")).Hash) { throw "Installed UI differs from verified build" }
Start-ScheduledTask -TaskName "HSC-App-Debug-Temporary"
$record = @{
  installer_sha256 = (Get-FileHash $installer.FullName).Hash
  installer_bytes = $installer.Length
  installed_agent_sha256 = (Get-FileHash $installedAgent).Hash
  installed_asar_sha256 = (Get-FileHash (Join-Path $installDir "resources\app.asar")).Hash
  backup_path = $backup
  upgraded_at = (Get-Date).ToUniversalTime().ToString("o")
  graceful_exit = $true
  launch_requested = $true
}
$record | ConvertTo-Json | Set-Content (Join-Path $backupDir "upgrade.json") -Encoding UTF8
Write-Output ("Upgrade installed; evidence: " + $backupDir)
