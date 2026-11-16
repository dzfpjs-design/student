param(
  [string]$Repo = 'C:\HSC\candidates\material-20260906',
  [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedInstallerHash,
  [string]$ProfileRoot = (Join-Path $env:APPDATA 'home-study-companion'),
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\home-study-companion'),
  [string]$CloseTaskName = 'HSC-App-Close'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$profileRoot = [IO.Path]::GetFullPath($ProfileRoot)
$installRoot = [IO.Path]::GetFullPath($InstallRoot)
if ($profileRoot -eq $installRoot) { throw 'Profile and installation roots must be different.' }
$installer = @(Get-ChildItem (Join-Path $Repo 'release') -Filter '*Setup*.exe')
if ($installer.Count -ne 1) { throw 'Expected exactly one installer' }
$expected = $ExpectedInstallerHash
if ((Get-FileHash $installer[0].FullName).Hash.ToLowerInvariant() -ne $expected) { throw 'Unverified installer' }
if (!(Test-Path (Join-Path $profileRoot 'family-data\home-study.db'))) { throw 'Existing family database missing' }
$backup = Join-Path 'C:\HSC\deployment-backups' (Get-Date -Format 'yyyyMMdd-HHmmss')
New-Item -ItemType Directory $backup | Out-Null
$state = @{status='closing'; backup=$backup; installer_sha256=$expected; at=[DateTime]::UtcNow.ToString('o')}
function Save-State { $state | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $backup 'deployment.json') -Encoding UTF8 }
Save-State
try {
  Start-ScheduledTask -TaskName $CloseTaskName
  $deadline = (Get-Date).AddSeconds(40)
  do {
    Start-Sleep -Milliseconds 500
    $running = @(Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($installRoot + '\', [StringComparison]::OrdinalIgnoreCase) })
  } while ($running.Count -gt 0 -and (Get-Date) -lt $deadline)
  if ($running.Count -gt 0) { throw 'Owned application did not close; no installation performed' }
  $state.status='backing_up'; Save-State
  Copy-Item $profileRoot (Join-Path $backup 'profile') -Recurse
  Copy-Item $installRoot (Join-Path $backup 'installation') -Recurse
  $manifest = @()
  foreach ($file in Get-ChildItem $profileRoot -File -Recurse) {
    $relative = $file.FullName.Substring($profileRoot.Length + 1)
    $hash = (Get-FileHash $file.FullName).Hash
    if ((Get-FileHash (Join-Path (Join-Path $backup 'profile') $relative)).Hash -ne $hash) { throw "Backup hash mismatch: $relative" }
    $manifest += @{path=$relative; sha256=$hash; bytes=$file.Length}
  }
  $manifest | ConvertTo-Json | Set-Content (Join-Path $backup 'profile-manifest.json') -Encoding UTF8
  $state.backup_files=$manifest.Count
  $state.status='installing'; Save-State
  # The installer is per-user by default. Deployment may run under a service
  # account, so always pin the already-validated target installation root.
  # NSIS requires /D= to be the final argument.
  $p = Start-Process $installer[0].FullName -ArgumentList @('/S', "/D=$installRoot") -PassThru
  if (!$p.WaitForExit(180000)) { throw 'Installer timeout; inspect process before retrying' }
  if ($p.ExitCode -ne 0) { throw "Installer exit $($p.ExitCode)" }
  foreach ($relative in @('resources\local-agent\home-study-local-agent.exe','resources\app.asar')) {
    if ((Get-FileHash (Join-Path $installRoot $relative)).Hash -ne (Get-FileHash (Join-Path (Join-Path $Repo 'release\win-unpacked') $relative)).Hash) { throw "Installed artifact mismatch: $relative" }
  }
  foreach ($name in @('model-settings.json','voice-settings.json','asr-settings.json','qwen-realtime-settings.json','data-encryption-key.bin','companion-wake-words.json')) {
    $before = Join-Path (Join-Path $backup 'profile') $name
    $after = Join-Path $profileRoot $name
    if (Test-Path -LiteralPath $before) {
      if (!(Test-Path -LiteralPath $after) -or (Get-FileHash -LiteralPath $after).Hash -ne (Get-FileHash -LiteralPath $before).Hash) {
        throw "Configuration changed: $name"
      }
    } elseif (Test-Path -LiteralPath $after) {
      throw "Configuration unexpectedly created: $name"
    }
  }
  $state.status='installed_not_launched'; $state.encrypted_settings_preserved=$true; Save-State
  Write-Output ($state | ConvertTo-Json -Compress)
} catch {
  $state.status='failed'; $state.error=$_.Exception.Message; Save-State
  throw
}
