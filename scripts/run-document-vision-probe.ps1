param([switch]$InteractiveRun)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$taskName = 'HSC-Document-Vision-Probe-Temporary'
if (-not $InteractiveRun) {
  $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -InteractiveRun" -WorkingDirectory $root
  $principal = New-ScheduledTaskPrincipal -UserId 'NJNU' -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force | Out-Null
  Start-ScheduledTask -TaskName $taskName
  Write-Output 'Started isolated synthetic-page probe in the authorized user session.'
  exit 0
}
Set-Location $root
$output = 'C:\HSC\device-evidence\document-vision-probe.jsonl'
& "$root\node_modules\electron\dist\electron.exe" "$PSScriptRoot\probe-windows-document-vision.mjs" 2>$null | Set-Content $output -Encoding UTF8
$code = $LASTEXITCODE
Set-Content 'C:\HSC\device-evidence\document-vision-probe.exit' -Value $code -Encoding ASCII
exit $code
