import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { awaitCdpResponse, ELECTRON_RENDERER_STARTUP_TIMEOUT_MS, releaseChildProcessHandles, stopWindowsProcessTree, windowsRemainingPids, windowsProcessTree, runWindowsCommand } from './electron-owned-process.mjs'

const immediateEventually = async (read) => await read()

test('renderer startup budget exceeds the local Agent cold-start health budget', () => {
  assert.ok(ELECTRON_RENDERER_STARTUP_TIMEOUT_MS > 60_000)
})

test('observation and document-camera runner use the shared Agent cold-start budget', async () => {
  const runner = await readFile(new URL('./run-observation-quality-electron-acceptance.mjs', import.meta.url), 'utf8')
  assert.match(runner, /import \{[^}]*ELECTRON_RENDERER_STARTUP_TIMEOUT_MS[^}]*\} from '\.\/electron-owned-process\.mjs'/)
  assert.match(runner, /const localAgentHealthTimeoutMs = ELECTRON_RENDERER_STARTUP_TIMEOUT_MS/)
  assert.match(runner, /'local agent did not start', localAgentHealthTimeoutMs/)
  assert.match(runner, /'restarted local agent did not start', localAgentHealthTimeoutMs/)
})

test('CDP response wait is bounded and clears settled pending requests', async () => {
  const waiting = new Map()
  const settled = awaitCdpResponse(waiting, 1, 'Runtime.evaluate', 100)
  const pending = waiting.get(1); waiting.delete(1); pending.resolve({ value: 'ok' })
  assert.deepEqual(await settled, { value: 'ok' })
  assert.equal(waiting.size, 0)
  const timeout = awaitCdpResponse(waiting, 2, 'Page.captureScreenshot', 1)
  await assert.rejects(timeout, /Page\.captureScreenshot timed out/)
  assert.equal(waiting.has(2), false)
})

test('knowledge-graph Electron restart bounds CDP commands on every platform', async () => {
  const source = await readFile(new URL('./e2e-resource-review-electron.mjs', import.meta.url), 'utf8')
  assert.match(source, /awaitCdpResponse\(pending, id, method\)/)
  assert.match(source, /Promise\.race\(\[cdp\.command\('Browser\.close'\).*sleep\(3_000\)\]\)/s)
})

test('voice harnesses reserve a bounded two-attempt visual-evidence budget', async () => {
  for (const name of ['electron-voice-agent-e2e.mjs', 'electron-voice-onboarding-e2e.mjs']) {
    const source = await readFile(new URL(`./${name}`, import.meta.url), 'utf8')
    assert.match(source, /const SCREENSHOT_TIMEOUT_MS = ELECTRON_RENDERER_STARTUP_TIMEOUT_MS/)
    assert.match(source, /attempt <= 2/)
    assert.match(source, /'visual_evidence_missing'/)
    assert.match(source, /process\.exitCode = 2/)
  }
})

test('observation and document-camera runner persist tracked Windows cleanup results', async () => {
  const runner = await readFile(new URL('./run-observation-quality-electron-acceptance.mjs', import.meta.url), 'utf8')
  assert.match(runner, /stopWindowsProcessTree/)
  assert.match(runner, /windowsRemainingPids/)
  assert.match(runner, /tracked_survivors/)
  assert.match(runner, /await stopOwnedProcess\(agent/)
})

test('Windows cleanup accepts a disappeared CIM process tree without a child exit event', async () => {
  const calls = []
  const run = async (command, args) => {
    calls.push({ command, args })
    if (command === 'powershell.exe' && args.at(-1).includes('ParentProcessId')) return { stdout: '101,102,103' }
    if (command === 'taskkill.exe') return { stdout: '' }
    if (command === 'powershell.exe') return { stdout: '' }
    throw new Error(`unexpected ${command}`)
  }
  const tracked = await stopWindowsProcessTree({ pid: 101, exitCode: null }, immediateEventually, run)
  assert.deepEqual(tracked, [101, 102, 103])
  assert.deepEqual(calls.find((call) => call.command === 'taskkill.exe').args, ['/PID', '101', '/T', '/F'])
})

test('Windows cleanup fails only when CIM still finds a tracked process', async () => {
  const run = async (command, args) => {
    if (command === 'powershell.exe' && args.at(-1).includes('ParentProcessId')) return { stdout: '101,102' }
    if (command === 'taskkill.exe') return { stdout: '' }
    return { stdout: '102' }
  }
  await assert.rejects(() => stopWindowsProcessTree({ pid: 101 }, immediateEventually, run), /survived cleanup: 102/)
})

test('CIM PID query ignores malformed process ids and released child streams cannot keep Node alive', async () => {
  const remaining = await windowsRemainingPids([5, 'bad', 5, -1], async () => ({ stdout: '5,not-a-pid' }))
  assert.deepEqual(remaining, [5])
  const stdout = { destroyed: false, destroy() { this.destroyed = true } }
  const stderr = { destroyed: false, destroy() { this.destroyed = true } }
  const child = { stdout, stderr, unrefCalled: false, unref() { this.unrefCalled = true } }
  releaseChildProcessHandles(child)
  assert.equal(stdout.destroyed, true)
  assert.equal(stderr.destroyed, true)
  assert.equal(child.unrefCalled, true)
})

test('Windows process ownership excludes children older than a recycled parent PID', { skip: process.platform !== 'win32' }, async () => {
  const fixture = "function Get-CimInstance { @(\n [pscustomobject]@{ProcessId=101;ParentProcessId=1;CreationDate=[datetime]'2026-10-07T16:00:00Z'},\n [pscustomobject]@{ProcessId=102;ParentProcessId=101;CreationDate=[datetime]'2026-10-07T16:00:01Z'},\n [pscustomobject]@{ProcessId=103;ParentProcessId=102;CreationDate=[datetime]'2026-10-07T16:00:02Z'},\n [pscustomobject]@{ProcessId=13628;ParentProcessId=102;CreationDate=[datetime]'2026-10-06T16:00:00Z'}\n ) }\n"
  const run = async (command, args) => runWindowsCommand(command, ['-NoProfile', '-Command', fixture + ';& {' + args.at(-1) + '}'])
  assert.deepEqual(await windowsProcessTree(101, run), [101, 102, 103])
  assert.deepEqual(await windowsProcessTree(999, run), [])
})
