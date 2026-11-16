param(
  [Parameter(Mandatory = $true)][string]$DevicePattern,
  [Parameter(Mandatory = $true)][string]$OutputPath,
  [Parameter(Mandatory = $true)][string]$ReportPath
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path $PSScriptRoot -Parent
$electron = Join-Path $projectRoot "node_modules\electron\dist\electron.exe"
$captureScript = Join-Path $projectRoot "services\local_agent\scripts\capture_document_camera_electron.js"
$logPath = "$ReportPath.process.log"

Remove-Item $OutputPath, $ReportPath, $logPath -Force -ErrorAction SilentlyContinue
try {
  & $electron $captureScript `
    --output $OutputPath `
    --report $ReportPath `
    --device-pattern $DevicePattern *>&1 |
    Set-Content $logPath -Encoding UTF8
  if (-not (Test-Path $ReportPath)) {
    throw "The Electron capture process exited without a device report."
  }
  $result = Get-Content $ReportPath -Raw | ConvertFrom-Json
  if ($result.status -ne "captured") {
    throw "Camera capture failed: $($result.error)"
  }
  exit 0
} catch {
  if (-not (Test-Path $ReportPath)) {
    @{ status = "failed"; error = $_.Exception.Message } |
      ConvertTo-Json -Compress |
      Set-Content $ReportPath -Encoding UTF8
  }
  exit 1
}
