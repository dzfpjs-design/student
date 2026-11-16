$ErrorActionPreference = "Stop"

$projectRoot = Split-Path $PSScriptRoot -Parent
$logPath = Join-Path $projectRoot ".windows-installer-smoke.log"
$exitPath = Join-Path $projectRoot ".windows-installer-smoke.exit"

Remove-Item $logPath, $exitPath -Force -ErrorAction SilentlyContinue
Set-Location $projectRoot

$exitCode = 0
try {
  & "$PSScriptRoot\smoke-windows-installer.ps1" *>&1 |
    Tee-Object -FilePath $logPath
} catch {
  $exitCode = 1
  $_ | Out-String | Add-Content -Path $logPath
} finally {
  Set-Content -Path $exitPath -Value $exitCode
}

exit $exitCode
