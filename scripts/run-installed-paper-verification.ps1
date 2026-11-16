param(
  [string]$TaskName = 'HSC-App-Run',
  [string]$ProbeScript = 'C:\HSC\candidates\full-product-20260925\scripts\probe-live-physics-paper.mjs',
  [string]$Output = 'C:\HSC\test-sessions\installed-paper-verification.json',
  [string]$IsolationRoot = 'C:\HSC\isolated-paper-acceptance',
  [string]$ChildId = 'child-junior-3',
  [int]$Port = 9235
)

$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $TaskName
$originalAction = @($task.Actions)[0]
$execute = $originalAction.Execute
$originalArguments = $originalAction.Arguments
$workingDirectory = $originalAction.WorkingDirectory
$installRoot = Split-Path -Parent $execute
$runRoot = Join-Path $IsolationRoot (Get-Date -Format 'yyyyMMdd-HHmmss')
$dataRoot = Join-Path $runRoot 'family-data'
New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null

function Stop-InstalledApp {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Get-CimInstance Win32_Process |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($installRoot, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2
}

$failure = $null
try {
  Stop-InstalledApp
  $databasePath = Join-Path $dataRoot 'home-study.db'
  $isolatedCommand = "`$env:HSC_DATA_DIR='$dataRoot';`$env:HSC_DATABASE_PATH='$databasePath';& '$execute' --remote-debugging-port=$Port --remote-debugging-address=127.0.0.1"
  $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($isolatedCommand))
  $debugAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $encodedCommand" `
    -WorkingDirectory $workingDirectory
  Set-ScheduledTask -TaskName $TaskName -Action $debugAction | Out-Null
  Start-ScheduledTask -TaskName $TaskName
  & 'C:\Program Files\nodejs\node.exe' $ProbeScript $Output $ChildId $Port --prepare-study
  if ($LASTEXITCODE -ne 0) { throw "Installed paper probe failed with exit code $LASTEXITCODE" }
  Get-Content -Raw -Path $Output
}
catch {
  $failure = $_
}
finally {
  Stop-InstalledApp
  $restoreParameters = @{ Execute = $execute }
  if ($originalArguments) { $restoreParameters.Argument = $originalArguments }
  if ($workingDirectory) { $restoreParameters.WorkingDirectory = $workingDirectory }
  Set-ScheduledTask -TaskName $TaskName -Action (New-ScheduledTaskAction @restoreParameters) | Out-Null
  Start-ScheduledTask -TaskName $TaskName
}
if ($failure) {
  [Console]::Error.WriteLine($failure.ToString())
  [Environment]::Exit(1)
}
