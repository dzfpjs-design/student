# Windows Runtime Stability Harness

## Scope And Boundaries

- `scripts/windows-runtime-stability.mjs` runs a short fault gate, a resumable short `recovery` drill, or an explicitly approved eight-hour soak.
- It uses unpackaged Electron 44 with the existing built main/preload/renderer and the **real packaged local-agent exe**. A temporary launcher redirects only `python -m home_study_agent` to that exe; the application's LocalServiceManager still owns startup and automatic crash recovery. This is not NSIS installation or installed-app acceptance.
- Profiles are newly generated under `%TEMP%/hsc-runtime-stability-*`, with an ownership nonce. No family profile, provider credentials, installed application, global network setting, or system clock is used or changed.
- Device permissions are deliberately denied. Browser network failure is injected through that renderer's CDP connection, not by disabling Windows networking. Non-loopback renderer requests and media endpoints are blocked; provider credentials are stripped and configured-cloud checks must be false.
- `runtime-stability-sqlite.py` reads only the nonce-owned temporary database, uses `mode=ro`, `query_only`, and `contextlib.closing`, and returns counts/integrity rather than child text.
- Repeated HTTP sessions and homework declarations exercise persistence/replay; they are not full learning-outcome or mastery acceptance. The separate packaged daily-learning acceptance covers wrong-answer/review generation.
- The harness records actual China learning dates and rechecks older-day summaries when a later day occurs. It does not fabricate elapsed calendar days. A short same-day gate cannot prove multi-day retention; an eight-hour run only crosses a day boundary if the actual start time permits it.

## Short Gate

Run on Windows with Node 22+ and the existing local Python; no downloads are performed:

```powershell
$root = 'C:\HSC\source-20260904-1250'
node "$root\scripts\windows-runtime-stability.mjs" --mode gate `
  --electron-bin "$root\node_modules\electron\dist\electron.exe" `
  --exe "$root\dist\home-study-local-agent.exe" `
  --python-bin "$root\.venv\Scripts\python.exe" `
  --model-dir "$root\services\local_agent\models" `
  --output-dir 'C:\HSC\device-evidence\runtime-stability' --gate-seconds 60
```

The gate checks synthetic session replay, SQLite quick_check, renderer reload, visible network-error recovery, real permission denial, and automatic recovery after taskkilling only the recorded packaged-backend PID tree. PID, executable path, and process creation time are compared before cleanup; no image-name or broad process kill is used. Successful gate cleanup removes only its temporary profile.

Outputs under a new `stability-*` evidence directory:

- `checkpoint.json`: current stage/status, binary fingerprint, owned process identities, cumulative observed time, interruptions/failures and latest compact sample. No session token is persisted.
- `build-manifest.json`: absolute repo/Node paths and per-file path, byte count and SHA256. Schema 2 binds every file under `out` (including non-JS assets), the selected Electron executable and its complete runtime directory, the Node and Python executables, packaged backend, and the four Node/Python harness files that execute the check. Optional `--app-asar` and `--app-binary` hash the complete selected files without launching an installed app. File labels and lengths are bound into the aggregate fingerprint; schema-1 approvals are no longer valid.
- `samples.jsonl`: tree RSS, CPU delta, process/handle count, loopback port, SQLite integrity, file/WAL sizes, and per-child session counts.
- `isolation.jsonl`: nonce-profile setup, packaged process launches, denied permissions.
- `synthetic-final.png` or `synthetic-failure.png`: synthetic renderer evidence.

RSS/CPU are observations, not a performance-budget pass or a memory-leak verdict. CPU after process-tree changes is reset/clamped rather than interpreted as negative consumption.

## Eight-Hour Launch Gate

**Do not start until main supplies the frozen final build.** First run a fresh short gate against that build. Use identical optional package inputs for the gate and scheduled launch, then explicitly supply that exact fingerprint. Read-only fingerprint computation is also available with `--print-build --exe PATH --electron-bin PATH --python-bin PATH [--app-asar PATH] [--app-binary PATH]`.

Hashing optional installed artifacts proves their identity, not that the installed application was executed. The runtime remains source Electron + built `out` + packaged backend. It does not attest the NSIS installer, the complete installed resource directory, Python dependencies, or local model file contents. Neither optional package path is ever used as an executable launch target.

This Windows host blocks unsigned PowerShell scripts by default. Open the command session with `powershell -NoProfile -ExecutionPolicy Bypass`, then run the commands below in that session. This is a process-only policy choice for the owned harness, not a machine/user policy change. For SSH invocation, use the same prefix with `-File scripts/start-windows-runtime-soak.ps1` and its named parameters.

```powershell
# NJNU must remain logged in. The scheduled process is independent of SSH.
$root = 'C:\HSC\source-20260904-1250'
$approved = '<final gate build fingerprint, 64 hex characters>'
$launch = @{
  Root = $root
  ElectronBin = "$root\node_modules\electron\dist\electron.exe"
  Exe = "$root\dist\home-study-local-agent.exe"
  PythonBin = "$root\.venv\Scripts\python.exe"
  ModelDir = "$root\services\local_agent\models"
  OutputDir = 'C:\HSC\device-evidence\runtime-stability'
  ApprovedBuild = $approved
  # Optional, read-only artifact binding; also pass corresponding flags to the gate:
  # AppAsar = "$root\release\win-unpacked\resources\app.asar"
  # AppBinary = '<absolute installed application exe path>'
  # NodeBin = 'C:\Program Files\nodejs\node.exe'
}
# Read-only validation: no task, profile, or eight-hour run is created.
& "$root\scripts\start-windows-runtime-soak.ps1" @launch -ValidateOnly
# Execute ONLY after main explicitly approves the final frozen build:
& "$root\scripts\start-windows-runtime-soak.ps1" @launch
```

The launcher registers a unique on-demand `HSC-Stability-*` task under NJNU's interactive token, without credentials or elevation. It writes a task receipt in OutputDir and starts Node directly through Task Scheduler. Disconnecting SSH does not own or terminate that process. Logging out, sleeping, or rebooting is not continuous runtime acceptance; resume explicitly after an interruption. No system power settings or installed applications are changed. Registration permission and actual detached execution remain target-runtime checks until the approved launch; `-ValidateOnly` does not prove them.

The current mode targets 28,800 observed seconds. It samples about once a minute and adds synthetic sessions/renderer reloads about every 30 minutes after the initial fault gate. Snapshot gaps exceeding two sampling intervals receive no time credit. Runner downtime never advances the checkpoint. Automatic backend crash injection is performed once per launch/resume; repeated crash cycling is not claimed.

## Status And Resume

```powershell
node "$root\scripts\windows-runtime-stability.mjs" --status '<checkpoint.json>'
Get-ScheduledTaskInfo -TaskName '<exact task name from launch receipt>'
# Only after the previous runner has stopped; keeps the same approved build.
& "$root\scripts\start-windows-runtime-soak.ps1" @launch -ResumeCheckpoint '<checkpoint.json>'
```

Status includes runner identity/liveness, checkpoint age, observed seconds, failures, and latest sample. A dead runner is reported as interrupted rather than trusted as still running. Only one operator should launch/resume a given checkpoint. After the runner and its owned children have stopped, remove only its exact scheduled-task name with `Unregister-ScheduledTask -TaskName '<exact receipt task name>' -Confirm:$false`; never use a wildcard or remove unrelated tasks.

Resume refuses an active previous runner, a changed build, a completed run, or mismatched temporary ownership. It reclaims only saved owned process identities, keeps prior failures and interruption counts, and reruns the fault checks before observing again. A run with prior failures ends as `completed_with_failures`, not a clean pass. Interrupted soak profiles are retained for recovery; do not delete them while resuming. A gate cannot be upgraded in place to a soak.

## Short Detached Recovery Drill

This mode has a 30..600 second observation target, is permanently labeled `recovery`, and cannot resume as a soak. It uses exactly the same scheduled launcher, checkpoint, identity cleanup and resume paths. No final eight-hour approval is implied by approving a short-drill fingerprint.

```powershell
# Use a fresh output directory and the current schema-2 fingerprint in $launch.
& "$root\scripts\start-windows-runtime-soak.ps1" @launch -Mode recovery -GateSeconds 180
# Disconnect this SSH connection. In a NEW SSH connection, find its checkpoint:
& "$root\scripts\verify-windows-runtime-recovery.ps1" -Action Observe -Checkpoint '<checkpoint.json>'
# Disconnect again. Once the initial three journeys/fault checks finish, in NEW SSH:
& "$root\scripts\verify-windows-runtime-recovery.ps1" -Action Interrupt -Checkpoint '<checkpoint.json>'
node "$root\scripts\windows-runtime-stability.mjs" --status '<checkpoint.json>'
# New scheduled runner; leave the same approved inputs and profile unchanged:
& "$root\scripts\start-windows-runtime-soak.ps1" @launch -Mode recovery -ResumeCheckpoint '<checkpoint.json>'
# After status reports passed and the scheduled task stops:
& "$root\scripts\verify-windows-runtime-recovery.ps1" -Action Verify -Checkpoint '<checkpoint.json>'
```

The interrupt helper rejects non-recovery checkpoints, mismatched nonce/temp roots, and unverified process identities. It kills only the recorded runner, leaving descendants intentionally for resume to reclaim. It writes observer, pre-interruption, and final verification evidence next to the checkpoint. Observe/Interrupt require distinct SSH connections. Successful recovery cleans up its synthetic profile; soak keeps its profile for inspection. Remove only the two exact task names from their receipts after verification, never other tasks.

Final acceptance must separately state: short fault results; actual observed hours; interruption/failure counts; actual learning dates seen; resource trend; SQLite result; cleanup; physical-device and installed-app limitations. No eight-hour or multi-day result exists merely because this harness is implemented.

## Harness Tests

```powershell
node "$root\scripts\windows-runtime-stability.test.mjs"
& "$root\.venv\Scripts\python.exe" "$root\scripts\test_runtime_stability_sqlite.py"
```

The Python tests verify real SQLite closure on success and failure and reject unrelated profiles before connecting. Tests are small and are not a release regression suite.

## Recorded Short-Gate Evidence

On Windows, `C:\HSC\device-evidence\runtime-stability\stability-cNQA4T\checkpoint.json` passed a 62-second observation, including actual packaged-backend automatic recovery in 14,449 ms. A subsequent pre-schema-2 run, `stability-ZgqSlo\checkpoint.json`, passed 32 seconds with all seven checks true and owned-profile cleanup successful. Its latest sample was 738 MB aggregate RSS across ten processes, SQLite quick_check ok, three synthetic completed sessions across two children, and zero model-call/capture rows. These snapshots do not establish a memory trend or eight-hour stability.

Schema-2 short recovery passed on 2026-09-06 UTC. Evidence is under `C:\HSC\device-evidence\runtime-recovery-20260906\stability-7dywd3`, including `recovery-verification.json`, checkpoint and complete manifest. Source-Electron runtime digest: `ac1eb23666dd58e020f801dd17d665bafc500d219fbd6d79b05f7b2461d15b4c` (not a final-build approval).

- Actual Task Scheduler launch survived the originating SSH connection closing, and a later separate SSH observation connection closing.
- The owned Node runner was forcibly interrupted at 63.37 observed seconds, leaving descendants deliberately. Status reported `interrupted_runner_not_alive` with unchanged accumulated time.
- A new scheduled runner resumed the same profile/checkpoint, reclaimed old identities, preserved the three existing sessions and added three more. An early resumed sample was 64.69 observed seconds; downtime did not become observation credit.
- Final result: `passed_short_recovery_not_eight_hours`, 180.725 observed seconds, one intentional interruption, no recorded failures, all seven fault/cleanup checks true, two children with three completed synthetic sessions each, SQLite quick_check ok, zero cloud-call/capture rows.
- Old and resumed process identities were gone; the owned temporary profile was removed. Only the two exact tasks created by this drill were unregistered. The intentionally killed task returned 4294967295; the resumed task returned 0.
- Read-only hashing also succeeded against the existing full `release/win-unpacked/resources/app.asar` and application executable. This did not launch or validate the installed application, and those optional artifacts were not inputs to this recovery run.

Five Node tests and two real-SQLite Python tests passed locally and on Windows. They include per-input fingerprint mutation detection for image assets, runtime inputs and optional package files. The final scheduled launcher also passed Windows `-ValidateOnly`. No scheduled eight-hour task has been registered or started. Actual eight-hour behavior, final-build/installed-ASAR execution and physical-device behavior remain unverified. This harness saw only learning date 2026-09-06; separate backend synthetic-date acceptance is not counted as a real calendar rollover here.

Freeze scope is six new scripts (runtime, SQLite probe, two tests, scheduled launcher, short recovery verifier) and this document. No application code, family data, machine policy, existing scripts or installed resources were edited. Runtime fingerprint schema changed; recompute the final approval with the same optional inputs used by the final gate. Do not reuse the old schema-1 digest.
