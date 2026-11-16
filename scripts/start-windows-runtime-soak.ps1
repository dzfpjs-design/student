<#
On-demand Task Scheduler launcher. Every run needs an explicit matching build
fingerprint; the eight-hour mode additionally requires main's final-build approval.
Only new HSC-Stability-* tasks and synthetic output directories are created.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$ElectronBin,
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string]$PythonBin,
    [Parameter(Mandatory=$true)][string]$ModelDir,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$ApprovedBuild,
    [ValidateSet('soak', 'recovery')][string]$Mode = 'soak',
    [ValidateRange(30,600)][int]$GateSeconds = 180,
    [string]$NodeBin,
    [string]$AppAsar,
    [string]$AppBinary,
    [string]$ResumeCheckpoint,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
if (!$NodeBin) { $NodeBin = (Get-Command node -CommandType Application -ErrorAction Stop).Source }
$node = (Resolve-Path -LiteralPath $NodeBin).Path
$Root = (Resolve-Path -LiteralPath $Root).Path
$ElectronBin = (Resolve-Path -LiteralPath $ElectronBin).Path
$Exe = (Resolve-Path -LiteralPath $Exe).Path
$PythonBin = (Resolve-Path -LiteralPath $PythonBin).Path
$ModelDir = (Resolve-Path -LiteralPath $ModelDir).Path
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
$harness = Join-Path $Root 'scripts/windows-runtime-stability.mjs'
foreach ($path in @($Root, $ElectronBin, $Exe, $PythonBin, $ModelDir, $harness)) {
    if (!(Test-Path -LiteralPath $path)) { throw "Missing runtime input: $path" }
}
$buildArguments = @('--print-build', '--exe', $Exe, '--electron-bin', $ElectronBin, '--python-bin', $PythonBin)
if ($AppAsar) { $AppAsar = (Resolve-Path -LiteralPath $AppAsar).Path; $buildArguments += @('--app-asar', $AppAsar) }
if ($AppBinary) { $AppBinary = (Resolve-Path -LiteralPath $AppBinary).Path; $buildArguments += @('--app-binary', $AppBinary) }
$actual = (& $node $harness @buildArguments | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $actual -cne $ApprovedBuild) { throw 'Approved final build does not match current binaries; nothing started.' }
if ($ResumeCheckpoint) {
    $saved = Get-Content -LiteralPath $ResumeCheckpoint -Raw | ConvertFrom-Json
    if ($saved.mode -ne $Mode -or $saved.build -cne $ApprovedBuild) { throw 'Resume checkpoint belongs to another mode/build.' }
    $ResumeCheckpoint = (Resolve-Path -LiteralPath $ResumeCheckpoint).Path
    $runArguments = @($harness, '--resume', $ResumeCheckpoint, '--approved-build', $ApprovedBuild)
} else {
    $runArguments = @($harness, '--mode', $Mode, '--electron-bin', $ElectronBin,
        '--exe', $Exe, '--python-bin', $PythonBin, '--model-dir', $ModelDir,
        '--output-dir', $OutputDir, '--approved-build', $ApprovedBuild)
    if ($Mode -eq 'recovery') { $runArguments += @('--gate-seconds', [string]$GateSeconds) }
    if ($AppAsar) { $runArguments += @('--app-asar', $AppAsar) }
    if ($AppBinary) { $runArguments += @('--app-binary', $AppBinary) }
}
# Task Scheduler accepts a command-line string, not a PowerShell argument array.
# Reject embedded quotes rather than risk changing the intended argument boundary.
foreach ($value in $runArguments) { if ($value.Contains('"') -or $value.Contains("`n")) { throw 'Invalid argument quoting' } }
$argumentLine = ($runArguments | ForEach-Object { '"' + $_.TrimEnd('\') + '"' }) -join ' '
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($ValidateOnly) {
    [pscustomobject]@{status='validated_only_not_started'; build=$actual; node=$node; root=$Root; mode=$Mode; account=$account; durable_launcher='TaskScheduler_Interactive'; resume=[bool]$ResumeCheckpoint} | ConvertTo-Json -Compress
    exit 0
}
$taskName = 'HSC-Stability-' + [Guid]::NewGuid().ToString('N')
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$action = New-ScheduledTaskAction -Execute $node -Argument $argumentLine -WorkingDirectory $Root
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 24) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$task = New-ScheduledTask -Action $action -Principal $principal -Settings $settings -Description "Synthetic isolated runtime acceptance ($Mode). No family data or physical-device success claim."
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$receipt = Join-Path $OutputDir ($taskName + '.json')
[pscustomobject]@{task=$taskName; build=$actual; node=$node; root=$Root; output=$OutputDir; mode=$Mode; resume=$ResumeCheckpoint; account=$account; started_at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $receipt -Encoding UTF8
Register-ScheduledTask -TaskName $taskName -InputObject $task -ErrorAction Stop | Out-Null
Start-ScheduledTask -TaskName $taskName
[pscustomobject]@{status='scheduled'; task=$taskName; receipt=$receipt; checkpoint_directory=$OutputDir; note='Requires NJNU to remain logged in; disconnecting SSH does not stop this task.'} | ConvertTo-Json -Compress
