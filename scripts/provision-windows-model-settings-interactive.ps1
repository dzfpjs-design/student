$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Security

$projectRoot = Split-Path $PSScriptRoot -Parent
$credentialPath = "C:\HSC\incoming\deepseek-key.dpapi"
$targetUserData = Join-Path $env:APPDATA "home-study-companion"
$resultPath = "C:\HSC\install-logs\model-provision-result.json"
$exitPath = "C:\HSC\install-logs\model-provision.exit"
$electron = Join-Path $projectRoot "node_modules\electron\dist\electron.exe"
$provisioner = Join-Path $PSScriptRoot "provision-windows-model-settings.mjs"

Remove-Item $resultPath, $exitPath -Force -ErrorAction SilentlyContinue
$exitCode = 1
try {
  if (-not (Test-Path $credentialPath)) { throw "The one-time DPAPI credential package is missing." }
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
  if ($apiKey.Length -lt 8) { throw "The decrypted API key is incomplete." }

  $payload = @{
    provider = "deepseek"
    apiKey = $apiKey
    baseUrl = "https://api.deepseek.com"
    model = "deepseek-v4-pro"
    voiceIntentModel = "deepseek-v4-flash"
  } | ConvertTo-Json -Compress
  $apiKey = $null
  $env:HSC_TARGET_USER_DATA_DIR = $targetUserData
  $env:HSC_PROVISION_MODEL_SETTINGS_JSON = $payload
  $env:HSC_MODEL_PROVISION_RESULT_PATH = $resultPath
  $payload = $null

  & $electron $provisioner | Out-Null
  if (-not (Test-Path $resultPath)) { throw "Electron did not write a provisioning result." }
  $result = Get-Content $resultPath -Raw | ConvertFrom-Json
  if ($result.configured -ne $true -or $result.plaintextAbsent -ne $true) {
    throw "Electron did not confirm encrypted model settings provisioning."
  }
  $exitCode = 0
} catch {
  @{ configured = $false; error = $_.Exception.Message } |
    ConvertTo-Json -Compress |
    Set-Content $resultPath -Encoding UTF8
} finally {
  Remove-Item Env:HSC_PROVISION_MODEL_SETTINGS_JSON -ErrorAction SilentlyContinue
  Remove-Item Env:HSC_MODEL_PROVISION_RESULT_PATH -ErrorAction SilentlyContinue
  Remove-Item Env:HSC_TARGET_USER_DATA_DIR -ErrorAction SilentlyContinue
  Remove-Item $credentialPath -Force -ErrorAction SilentlyContinue
  Set-Content $exitPath -Value $exitCode -Encoding ASCII
}

exit $exitCode
