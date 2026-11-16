param([string]$Endpoint = $env:HSC_QWEN_REALTIME_ENDPOINT)

# Fail before consuming the one-time credential or touching existing settings.
# The destination must be explicitly supplied by the caller or environment.
[Uri]$endpointUri = $null
if ([string]::IsNullOrWhiteSpace($Endpoint) -or
    -not [Uri]::TryCreate($Endpoint.Trim(), [UriKind]::Absolute, [ref]$endpointUri) -or
    $endpointUri.Scheme -ne 'https' -or -not [string]::IsNullOrEmpty($endpointUri.UserInfo) -or
    -not [string]::IsNullOrEmpty($endpointUri.Query) -or -not [string]::IsNullOrEmpty($endpointUri.Fragment) -or
    $endpointUri.Host -notmatch '^[a-zA-Z0-9_-]+\.(cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com$') {
  [Console]::Error.WriteLine('A valid Bailian workspace HTTPS endpoint is required via -Endpoint or HSC_QWEN_REALTIME_ENDPOINT.')
  exit 2
}
$resolvedEndpoint = $endpointUri.GetLeftPart([UriPartial]::Authority)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security

$projectRoot = Split-Path $PSScriptRoot -Parent
$credentialPath = 'C:\HSC\incoming\qwen-realtime-key.dpapi'
$targetUserData = Join-Path $env:APPDATA 'home-study-companion'
$resultPath = 'C:\HSC\install-logs\qwen-realtime-provision-result.json'
$exitPath = 'C:\HSC\install-logs\qwen-realtime-provision.exit'
$runtimeLogPath = 'C:\HSC\install-logs\qwen-realtime-provision-runtime.log'
$runtimeErrorPath = 'C:\HSC\install-logs\qwen-realtime-provision-runtime.error.log'
$electron = Join-Path $projectRoot '.electron-runtime\electron.exe'
$provisioner = Join-Path $PSScriptRoot 'provision-windows-qwen-realtime-settings.mjs'

New-Item -ItemType Directory -Path (Split-Path $resultPath -Parent) -Force | Out-Null
Remove-Item $resultPath, $exitPath, $runtimeLogPath, $runtimeErrorPath -Force -ErrorAction SilentlyContinue
$exitCode = 1
try {
  if (-not (Test-Path $credentialPath)) { throw 'The one-time DPAPI realtime voice credential package is missing.' }
  if (-not (Test-Path $electron)) { throw 'The isolated Electron runtime is missing.' }

  $protected = [IO.File]::ReadAllBytes($credentialPath)
  $clearBytes = [Security.Cryptography.ProtectedData]::Unprotect(
    $protected,
    $null,
    [Security.Cryptography.DataProtectionScope]::CurrentUser
  )
  Remove-Item $credentialPath -Force
  $apiKey = [Text.Encoding]::UTF8.GetString($clearBytes).Trim()
  [Array]::Clear($clearBytes, 0, $clearBytes.Length)
  if ($apiKey.Length -lt 16) { throw 'The decrypted realtime voice credential is incomplete.' }

  $env:HSC_TARGET_USER_DATA_DIR = $targetUserData
  $env:HSC_QWEN_REALTIME_ENDPOINT = $resolvedEndpoint
  $env:HSC_PROVISION_QWEN_REALTIME_KEY = $apiKey
  $apiKey = $null
  $process = Start-Process -FilePath $electron -ArgumentList @($provisioner) -PassThru `
    -RedirectStandardOutput $runtimeLogPath -RedirectStandardError $runtimeErrorPath
  if (-not $process.WaitForExit(30000)) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw 'Electron realtime voice provisioner timed out.'
  }
  $settingsPath = Join-Path $targetUserData 'qwen-realtime-settings.json'
  if (-not (Test-Path $settingsPath)) {
    throw "Electron realtime voice provisioner did not write settings (exit code $($process.ExitCode))."
  }
  $stored = Get-Content $settingsPath -Raw | ConvertFrom-Json
  if ($stored.model -ne 'qwen3.8-omni-flash-realtime' -or $stored.enabled -ne $true -or
      [string]::IsNullOrWhiteSpace($stored.encryptedApiKey)) {
    throw 'Realtime voice settings validation failed.'
  }
  @{
    configured = $true
    endpoint = $stored.endpoint
    model = $stored.model
    voice = $stored.voice
    encrypted_key_present = $true
  } | ConvertTo-Json -Compress | Set-Content $resultPath -Encoding UTF8
  $exitCode = 0
} catch {
  @{ configured = $false; error = $_.Exception.Message } |
    ConvertTo-Json -Compress | Set-Content $resultPath -Encoding UTF8
} finally {
  Remove-Item Env:HSC_TARGET_USER_DATA_DIR -ErrorAction SilentlyContinue
  Remove-Item Env:HSC_QWEN_REALTIME_ENDPOINT -ErrorAction SilentlyContinue
  Remove-Item Env:HSC_PROVISION_QWEN_REALTIME_KEY -ErrorAction SilentlyContinue
  Remove-Item $credentialPath -Force -ErrorAction SilentlyContinue
  Set-Content $exitPath -Value $exitCode -Encoding ASCII
}

exit $exitCode
