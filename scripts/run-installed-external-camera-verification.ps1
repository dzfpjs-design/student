param(
  [string]$TaskName = 'HSC-App-Run',
  [string]$ProbeScript = 'C:\HSC\candidates\full-product-20260925\scripts\verify-installed-external-cameras.mjs',
  [string]$Output = 'C:\HSC\test-sessions\installed-external-cameras.json',
  [int]$Port = 9222
)

$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $TaskName
$originalAction = @($task.Actions)[0]
$execute = $originalAction.Execute
$originalArguments = $originalAction.Arguments
$workingDirectory = $originalAction.WorkingDirectory
$installRoot = Split-Path -Parent $execute

function Stop-InstalledApp {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Get-CimInstance Win32_Process |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($installRoot, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2
}

try {
  Stop-InstalledApp
  $debugParameters = @{
    Execute = $execute
    Argument = "--remote-debugging-port=$Port --remote-debugging-address=127.0.0.1"
  }
  if ($workingDirectory) { $debugParameters.WorkingDirectory = $workingDirectory }
  $debugAction = New-ScheduledTaskAction @debugParameters
  Set-ScheduledTask -TaskName $TaskName -Action $debugAction | Out-Null
  Start-ScheduledTask -TaskName $TaskName
  & 'C:\Program Files\nodejs\node.exe' $ProbeScript $Output $Port
  if ($LASTEXITCODE -ne 0) { throw "External camera probe failed with exit code $LASTEXITCODE" }
  Get-Content -Raw -Path $Output
}
finally {
  Stop-InstalledApp
  $restoreParameters = @{ Execute = $execute }
  if ($originalArguments) { $restoreParameters.Argument = $originalArguments }
  if ($workingDirectory) { $restoreParameters.WorkingDirectory = $workingDirectory }
  $restoredAction = New-ScheduledTaskAction @restoreParameters
  Set-ScheduledTask -TaskName $TaskName -Action $restoredAction | Out-Null
  Start-ScheduledTask -TaskName $TaskName
}
