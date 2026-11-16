import { spawn } from 'node:child_process'

// Electron creates its first renderer only after the local Agent is healthy.
// The Agent has a documented 60-second cold-start budget, so leave a small
// renderer-publication margin rather than racing it with the old 20 seconds.
export const ELECTRON_RENDERER_STARTUP_TIMEOUT_MS = 70_000
export const CDP_COMMAND_TIMEOUT_MS = 30_000

export function awaitCdpResponse(waiting, id, method, timeoutMs = CDP_COMMAND_TIMEOUT_MS) {
  return new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => {
      waiting.delete(id)
      reject(new Error(`CDP ${method} timed out after ${timeoutMs}ms`))
    }, timeoutMs)
    waiting.set(id, {
      resolve(value) { clearTimeout(timer); resolvePromise(value) },
      reject(error) { clearTimeout(timer); reject(error) },
    })
  })
}

function validPids(pids) {
  return [...new Set(pids.map(Number).filter((pid) => Number.isSafeInteger(pid) && pid > 0))]
}

export async function runWindowsCommand(command, args) {
  return await new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = ''; let stderr = ''
    child.stdout.on('data', (chunk) => { stdout += String(chunk) })
    child.stderr.on('data', (chunk) => { stderr += String(chunk) })
    child.once('error', reject)
    child.once('close', (code) => code === 0 ? resolvePromise({ stdout, stderr }) : reject(new Error(`${command} exited ${code}: ${stderr}`)))
  })
}

export async function windowsProcessTree(pid, run = runWindowsCommand) {
  const rootPid = validPids([pid])[0]
  if (!rootPid) return []
  // ParentProcessId survives the parent's exit and can alias a later process.
  // Only a child born after its actual tracked parent belongs to this tree.
  const command = `$all=Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CreationDate; $roots=@($all | Where-Object { $_.ProcessId -eq ${rootPid} }); if($roots.Count -ne 1 -or -not $roots[0].CreationDate){return}; $owned=@($roots[0]); do { $next=@($all | Where-Object { $entry=$_; $parents=@($owned | Where-Object { $_.ProcessId -eq $entry.ParentProcessId }); $parents.Count -eq 1 -and $entry.CreationDate -and $entry.CreationDate -ge $parents[0].CreationDate -and @($owned.ProcessId) -notcontains $entry.ProcessId }); $owned += $next } while ($next.Count -gt 0); @($owned.ProcessId) -join ','`
  const output = await run('powershell.exe', ['-NoProfile', '-Command', command])
  return validPids(output.stdout.trim().split(','))
}

export async function windowsRemainingPids(pids, run = runWindowsCommand) {
  const tracked = validPids(pids)
  if (!tracked.length) return []
  const command = `$wanted=@(${tracked.join(',')}); @(Get-CimInstance Win32_Process | Where-Object { $wanted -contains [int]$_.ProcessId } | ForEach-Object { [int]$_.ProcessId }) -join ','`
  const output = await run('powershell.exe', ['-NoProfile', '-Command', command])
  return validPids(output.stdout.trim().split(','))
}

export async function stopWindowsProcessTree(child, eventually, run = runWindowsCommand) {
  if (!child?.pid) return []
  const tracked = await windowsProcessTree(child.pid, run)
  await run('taskkill.exe', ['/PID', String(child.pid), '/T', '/F']).catch(() => undefined)
  await eventually(async () => {
    const survivors = await windowsRemainingPids(tracked, run)
    if (survivors.length) throw new Error(`owned Windows process tree survived cleanup: ${survivors.join(',')}`)
    return true
  }, 'owned Windows Electron process tree cleanup', 7_000)
  return tracked
}

export function releaseChildProcessHandles(child) {
  child?.stdout?.destroy()
  child?.stderr?.destroy()
  child?.unref?.()
}
