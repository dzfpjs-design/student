// @vitest-environment node
import workspaceSource from './SessionWorkspace.tsx?raw'
import { describe, expect, it, vi } from 'vitest'
import { AgentContinuationDriver } from './agentContinuationDriver'
import { transformSync } from 'esbuild'

const tick = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }

describe('continuation historical receipt is not execution permission', () => {
  it.each(['awaiting_receipt', 'failed'])('reports %s without retry or execution', async (status) => {
    const driver = new AgentContinuationDriver()
    const request = vi.fn(async () => ({ status })), execute = vi.fn(), failed = vi.fn()
    driver.offer({ idle: () => true, request, execute, failed })()
    await tick(); driver.flush(); await tick()
    expect(request).toHaveBeenCalledTimes(1)
    expect(execute).not.toHaveBeenCalled()
    expect(failed).toHaveBeenCalledTimes(1)
  })
  it('never replays or reports failure for an already completed receipt', async () => {
    const driver = new AgentContinuationDriver()
    const request = vi.fn(async () => ({ status: 'completed' })), execute = vi.fn(), failed = vi.fn()
    driver.offer({ idle: () => true, request, execute, failed })()
    await tick(); driver.flush()
    expect(request).toHaveBeenCalledTimes(1)
    expect(execute).not.toHaveBeenCalled()
    expect(failed).not.toHaveBeenCalled()
  })
  it('suppresses a late unknown receipt after interruption or child change', async () => {
    const driver = new AgentContinuationDriver()
    let resolve!: (value: { status: string }) => void
    const execute = vi.fn(), failed = vi.fn()
    driver.offer({ idle: () => true, request: () => new Promise((done) => { resolve = done }), execute, failed })()
    driver.interrupt(); resolve({ status: 'awaiting_receipt' }); await tick()
    expect(execute).not.toHaveBeenCalled(); expect(failed).not.toHaveBeenCalled()
  })
  it('does not call admission evidence proof that a device action was sent', () => {
    const source = workspaceSource
    const poll = source.slice(source.indexOf('continuationPollRef.current = async'), source.indexOf('const timer = window.setInterval', source.indexOf('continuationPollRef.current = async')))
    expect(poll).not.toContain('后续操作已经发出')
    expect(poll).toContain('不能据此判断是否已经执行')
    expect(poll).toContain('localOnly: true')
  })
  it.each([['ready', true], ['awaiting_receipt', true], ['ready', false], ['awaiting_receipt', false]] as const)(
    'actual workspace poll status=%s interrupted=%s preserves input scope', async (status, interrupted) => {
    const source = workspaceSource
    const start = source.indexOf('continuationPollRef.current = async')
    const code = source.slice(start, source.indexOf('\n  useEffect', start)).replace('continuationPollRef.current = ', '')
    const javascript = transformSync(`const poll = ${code}`, { loader: 'ts', target: 'es2022' }).code
    const driver = new AgentContinuationDriver()
    let resolve!: (value: { status: string; parent_turn_id: string }) => void
    const schedule = vi.fn(), reply = vi.fn(), speak = vi.fn()
    const poll = new Function('continuationDriver', 'continuationIdle', 'commandApi', 'workspace',
      'continuationHandledRef', 'setReply', 'speakReply', 'scheduleContinuation', `${javascript}\nreturn poll`)(
      driver, () => true, { continuationStatus: () => new Promise((done) => { resolve = done }) },
      { session: { session_id: 'old-session' } }, { current: new Set<string>() }, reply, speak, schedule)
    const running = poll()
    if (interrupted) driver.interrupt()
    resolve({ status, parent_turn_id: 'old-parent' })
    await running
    expect(schedule).toHaveBeenCalledTimes(!interrupted && status === 'ready' ? 1 : 0)
    expect(reply).toHaveBeenCalledTimes(!interrupted && status === 'awaiting_receipt' ? 1 : 0)
    expect(speak).toHaveBeenCalledTimes(!interrupted && status === 'awaiting_receipt' ? 1 : 0)
  })
})
