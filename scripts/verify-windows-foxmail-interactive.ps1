param(
  [string]$ProjectRoot = 'C:\HSC\candidates\full-product-20260925',
  [string]$TaskName = 'HSC-App-Run',
  [Parameter(Mandatory=$true)][string]$Recipient,
  [string]$OutputRoot = 'C:\HSC\artifacts'
)

$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$originalAction = @($task.Actions)[0]
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logPath = Join-Path $OutputRoot "foxmail-interactive-$stamp.log"
$statePath = Join-Path $OutputRoot "foxmail-interactive-$stamp.json"
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

$command = @"
`$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath '$ProjectRoot'
`$env:HSC_FOXMAIL_LIVE_RECIPIENT = '$Recipient'
& 'C:\Program Files\nodejs\npx.cmd' vitest run src/main/foxmail-delivery.live.test.ts --reporter=dot *> '$logPath'
`$code = `$LASTEXITCODE
@{ exit_code = `$code; completed_at = [DateTime]::UtcNow.ToString('o') } |
  ConvertTo-Json -Compress | Set-Content -LiteralPath '$statePath' -Encoding UTF8
exit `$code
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
$verificationAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -EncodedCommand $encoded"

try {
  Remove-Item -LiteralPath $logPath, $statePath -Force -ErrorAction SilentlyContinue
  Set-ScheduledTask -TaskName $TaskName -Action $verificationAction | Out-Null
  Start-ScheduledTask -TaskName $TaskName
  $deadline = (Get-Date).AddSeconds(75)
  while (-not (Test-Path -LiteralPath $statePath) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
  }
  if (-not (Test-Path -LiteralPath $statePath)) { throw 'FOXMAIL_INTERACTIVE_TEST_TIMEOUT' }
  $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
  if ([int]$state.exit_code -ne 0) {
    $tail = if (Test-Path -LiteralPath $logPath) { (Get-Content -LiteralPath $logPath -Tail 40) -join [Environment]::NewLine } else { '' }
    throw "FOXMAIL_INTERACTIVE_TEST_FAILED`n$tail"
  }
  [ordered]@{
    status = 'passed'
    recipient = $Recipient
    transport = 'simple_mapi'
    interactive_task = $TaskName
    log_path = $logPath
    completed_at = $state.completed_at
    family_data_included = $false
  } | ConvertTo-Json -Compress
}
finally {
  $restore = @{ Execute = $originalAction.Execute }
  if ($originalAction.Arguments) { $restore.Argument = $originalAction.Arguments }
  if ($originalAction.WorkingDirectory) { $restore.WorkingDirectory = $originalAction.WorkingDirectory }
  Set-ScheduledTask -TaskName $TaskName -Action (New-ScheduledTaskAction @restore) | Out-Null
}
