$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Security

$projectRoot = Split-Path $PSScriptRoot -Parent
$credentialPath = "C:\HSC\incoming\dashscope-asr-key.dpapi"
$targetUserData = Join-Path $env:APPDATA "home-study-companion"
$resultPath = "C:\HSC\install-logs\asr-provision-result.json"
$exitPath = "C:\HSC\install-logs\asr-provision.exit"
$electron = Join-Path $projectRoot "node_modules\electron\dist\electron.exe"
$provisioner = Join-Path $PSScriptRoot "provision-windows-asr-settings.mjs"

Remove-Item $resultPath, $exitPath -Force -ErrorAction SilentlyContinue
$exitCode = 1
try {
  if (-not (Test-Path $credentialPath)) { throw "The one-time DPAPI ASR credential package is missing." }
  if (-not (Test-Path $electron)) { throw "The Electron runtime is missing." }

  $protected = [System.IO.File]::ReadAllBytes($credentialPath)
  $clearBytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
    $protected,
    $null,
    [System.Security.Cryptography.DataProtectionScope]::CurrentUser
  )
  Remove-Item $credentialPath -Force
  $apiKey = [System.Text.Encoding]::UTF8.GetString($clearBytes).Trim()
  [Array]::Clear($clearBytes, 0, $clearBytes.Length)
  if ($apiKey.Length -lt 8) { throw "The decrypted ASR API key is incomplete." }

  $payload = @{
    provider = "dashscope"
    apiKey = $apiKey
    baseUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model = "qwen3-asr-flash"
  } | ConvertTo-Json -Compress
  $apiKey = $null
  $env:HSC_TARGET_USER_DATA_DIR = $targetUserData
  $env:HSC_PROVISION_ASR_SETTINGS_JSON = $payload
  $env:HSC_ASR_PROVISION_RESULT_PATH = $resultPath
  $payload = $null

  & $electron $provisioner | Out-Null
  if (-not (Test-Path $resultPath)) { throw "Electron did not write an ASR provisioning result." }
  $result = Get-Content $resultPath -Raw | ConvertFrom-Json
  if ($result.configured -ne $true -or $result.plaintextAbsent -ne $true) {
    throw "Electron did not confirm encrypted ASR settings provisioning."
  }
  $exitCode = 0
} catch {
  @{ configured = $false; error = $_.Exception.Message } |
    ConvertTo-Json -Compress |
    Set-Content $resultPath -Encoding UTF8
} finally {
  Remove-Item Env:HSC_PROVISION_ASR_SETTINGS_JSON -ErrorAction SilentlyContinue
  Remove-Item Env:HSC_ASR_PROVISION_RESULT_PATH -ErrorAction SilentlyContinue
  Remove-Item Env:HSC_TARGET_USER_DATA_DIR -ErrorAction SilentlyContinue
  Remove-Item $credentialPath -Force -ErrorAction SilentlyContinue
  Set-Content $exitPath -Value $exitCode -Encoding ASCII
}

exit $exitCode
