<# Small recovery drill only. Never targets a soak, family profile, or installed app. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Observe','Interrupt','Verify')][string]$Action,
    [Parameter(Mandatory=$true)][string]$Checkpoint
)
$ErrorActionPreference = 'Stop'
$Checkpoint = (Resolve-Path -LiteralPath $Checkpoint).Path
$s = Get-Content -LiteralPath $Checkpoint -Raw | ConvertFrom-Json
if ($s.mode -ne 'recovery' -or $s.target_seconds -gt 600) { throw 'Only short recovery checkpoints are allowed' }
$runDir = Split-Path -Parent $Checkpoint
if ($runDir -cne $s.runDir) { throw 'Evidence directory mismatch' }
$temp = [IO.Path]::GetFullPath($s.temp)
if ((Split-Path -Parent $temp).TrimEnd('\') -ine ([IO.Path]::GetTempPath()).TrimEnd('\') -or (Split-Path -Leaf $temp) -notlike 'hsc-runtime-stability-*') { throw 'Not an owned temporary profile' }
if ($Action -ne 'Verify') {
    $owner = Get-Content -LiteralPath (Join-Path $temp 'owner.json') -Raw | ConvertFrom-Json
    if ($owner.owner -cne $s.owner -or $owner.synthetic_only -ne $true) { throw 'Profile ownership mismatch' }
}
function Get-OwnedProcess($identity) {
    if (!$identity -or $identity.pid -le 0) { return $null }
    $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$identity.pid)
    if ($p -and $p.ExecutablePath -ieq $identity.exe -and $p.CreationDate.ToUniversalTime().ToString('o') -ceq $identity.created) { return $p }
    return $null
}
function Save-Proof($name, $value) {
    $path = Join-Path $runDir $name
    $tmp = $path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    $value | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $path -Force
}
$runner = Get-OwnedProcess $s.runner
if ($Action -eq 'Observe') {
    if (!$runner -or $s.status -ne 'running') { throw 'Runner is not alive' }
    $proof = @{at=[DateTime]::UtcNow.ToString('o'); observer_pid=$PID; ssh_connection=$env:SSH_CONNECTION; runner=$s.runner; observed_seconds=$s.observed_seconds; updated_at=$s.updated_at; owner=$s.owner}
    Save-Proof 'recovery-detached-observation.json' $proof
    @{status='owned_runner_alive_in_new_observer'; observed_seconds=$s.observed_seconds; stage=$s.stage} | ConvertTo-Json -Compress
} elseif ($Action -eq 'Interrupt') {
    $observed = Get-Content -LiteralPath (Join-Path $runDir 'recovery-detached-observation.json') -Raw | ConvertFrom-Json
    if (!$runner -or $s.status -ne 'running' -or $s.journeys -lt 3 -or !$s.checks.backend_crash_automatic_recovery) { throw 'Wait for initial short fault checks before interrupting' }
    if ($observed.owner -cne $s.owner -or $observed.runner.pid -ne $s.runner.pid -or $observed.ssh_connection -eq $env:SSH_CONNECTION) { throw 'Observe and interrupt using separate SSH connections to demonstrate disconnect survival' }
    Save-Proof 'recovery-before-interrupt.json' @{at=[DateTime]::UtcNow.ToString('o'); checkpoint=$s; ssh_disconnect_survived=$true; observer_pid=$PID}
    # Kill only the verified runner, deliberately leaving owned descendants for resume cleanup.
    Stop-Process -Id $runner.ProcessId -Force -ErrorAction Stop
    Start-Sleep -Seconds 2
    if (Get-OwnedProcess $s.runner) { throw 'Runner did not stop' }
    $after = Get-Content -LiteralPath $Checkpoint -Raw | ConvertFrom-Json
    if ($after.observed_seconds -ne $s.observed_seconds) { throw 'Unexpected concurrent checkpoint advance; inspect evidence before resuming' }
    @{status='runner_interrupted_children_retained_for_resume'; observed_seconds=$after.observed_seconds} | ConvertTo-Json -Compress
} else {
    $before = (Get-Content -LiteralPath (Join-Path $runDir 'recovery-before-interrupt.json') -Raw | ConvertFrom-Json).checkpoint
    if ($s.owner -cne $before.owner -or $s.build -cne $before.build -or $s.runner.pid -eq $before.runner.pid) { throw 'Not the same owned run with a new runner' }
    if ($s.status -ne 'passed' -or $s.interruptions -ne 1 -or $s.failures.Count -ne 0 -or $runner) { throw 'Recovery has not cleanly completed' }
    if (!$s.checks.owned_pid_tree_stopped -or !$s.checks.temporary_profile_removed -or (Test-Path -LiteralPath $temp)) { throw 'Owned cleanup incomplete' }
    foreach ($identity in @($before.runner, $before.electron) + @($before.owned)) { if (Get-OwnedProcess $identity) { throw 'Pre-interruption owned process still alive' } }
    if ($s.latest.sqlite.quick_check -ne 'ok' -or $s.latest.sqlite.counts.daily_session -ne ($before.journeys + 3) -or $s.journeys -ne ($before.journeys + 3)) { throw 'Session durability mismatch' }
    if ($s.latest.sqlite.counts.model_call -ne 0 -or $s.latest.sqlite.counts.capture_artifact -ne 0) { throw 'Unexpected cloud/device activity' }
    $proof = @{status='passed_short_recovery_not_eight_hours'; at=[DateTime]::UtcNow.ToString('o'); build=$s.build; interruptions=$s.interruptions; observed_seconds=$s.observed_seconds; ssh_disconnect_survived=$true; resumed_new_runner=$true; sqlite=$s.latest.sqlite; checks=$s.checks}
    Save-Proof 'recovery-verification.json' $proof
    $proof | ConvertTo-Json -Depth 8 -Compress
}
