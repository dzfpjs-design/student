param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('Start', 'Status', 'Stop')]
  [string]$Action,
  [string]$Repo,
  [string]$ProfileRoot = (Join-Path $env:APPDATA 'home-study-companion'),
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\home-study-companion'),
  [string]$OutputRoot = 'C:\HSC\test-sessions',
  [string]$SessionId
)

$ErrorActionPreference = 'Stop'
if (-not $Repo) { $Repo = Split-Path $PSScriptRoot -Parent }
$python = Join-Path $Repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
  throw "Candidate Python runtime is missing: $python"
}
$collector = Join-Path $Repo 'scripts\collect_windows_test_session.py'
if (-not (Test-Path -LiteralPath $collector)) {
  throw "Test-session collector is missing: $collector"
}
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

function Write-DeviceInventory([string]$Path) {
  $devices = @()
  try {
    $devices = @(Get-PnpDevice -PresentOnly -ErrorAction Stop |
      Where-Object { $_.Class -in @('Camera', 'Image', 'AudioEndpoint', 'MEDIA') } |
      Select-Object Class, FriendlyName, Status)
    $payload = @{
      captured_at = [DateTime]::UtcNow.ToString('o')
      computer_name = $env:COMPUTERNAME
      os = [Environment]::OSVersion.VersionString
      is_64_bit = [Environment]::Is64BitOperatingSystem
      devices = $devices
    }
  } catch {
    $payload = @{
      captured_at = [DateTime]::UtcNow.ToString('o')
      inventory_status = 'unavailable'
      error_type = $_.Exception.GetType().Name
    }
  }
  $payload | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Path -Encoding UTF8
}

$arguments = @(
  $collector,
  '--action', $Action.ToLowerInvariant(),
  '--profile-root', $ProfileRoot,
  '--output-root', $OutputRoot,
  '--install-root', $InstallRoot
)
if ($SessionId) { $arguments += @('--session-id', $SessionId) }

if ($Action -eq 'Stop') {
  $activePath = Join-Path $OutputRoot 'active-test-session.json'
  if (-not (Test-Path -LiteralPath $activePath)) { throw 'No active Windows test session was found.' }
  $active = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
  Write-DeviceInventory (Join-Path $active.session_dir 'device-inventory-stop.json')
}

$json = & $python @arguments
if ($LASTEXITCODE -ne 0) { throw ($json -join [Environment]::NewLine) }
$result = $json | Select-Object -Last 1 | ConvertFrom-Json

if ($Action -eq 'Start') {
  Write-DeviceInventory (Join-Path $result.session_dir 'device-inventory-start.json')
  Write-Host "测试记录已开始。会话: $($result.session_id)" -ForegroundColor Green
  Write-Host '请完成语音对话和高拍仪拍题入错题库流程；测试结束后运行 Stop。'
} elseif ($Action -eq 'Stop') {
  Write-Host "测试记录已封存: $($result.archive_path)" -ForegroundColor Green
  Write-Host '该压缩包含家庭私密数据库快照，只保留在本机，不会自动上传。' -ForegroundColor Yellow
}
$result | ConvertTo-Json -Depth 8
