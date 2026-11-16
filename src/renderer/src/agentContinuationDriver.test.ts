import { describe, expect, it, vi } from 'vitest'
import { AgentContinuationDriver } from './agentContinuationDriver'
import type { AgentTurn } from './types'

const turn = { agent_turn_id: 'result-turn', transcript: '' } as AgentTurn
const tick = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }
describe('result continuation arbitration', () => {
  it('waits for speech and execution to settle, then executes exactly once', async () => {
    const driver = new AgentContinuationDriver()
    let idle = false
    const request = vi.fn(async () => ({ status: 'ready', turn }))
    const execute = vi.fn(async () => undefined)
    const finish = driver.offer({ idle: () => idle, request, execute, failed: vi.fn() })
    driver.flush(); expect(request).not.toHaveBeenCalled()
    finish(); expect(request).not.toHaveBeenCalled()
    idle = true; driver.flush(); driver.flush(); await tick()
    expect(request).toHaveBeenCalledTimes(1)
    expect(execute).toHaveBeenCalledTimes(1)
  })
  it('never executes a late result after child speech or cancellation', async () => {
    const driver = new AgentContinuationDriver()
    let resolve!: (value: { status: string; turn: AgentTurn }) => void
    const execute = vi.fn(async () => undefined)
    driver.offer({ idle: () => true, request: () => new Promise((done) => { resolve = done }), execute, failed: vi.fn() })()
    driver.interrupt(); resolve({ status: 'ready', turn }); await tick()
    expect(execute).not.toHaveBeenCalled()
  })
  it('reports uncertainty without automatically repeating a model call', async () => {
    const driver = new AgentContinuationDriver()
    const failed = vi.fn(), request = vi.fn(async () => { throw new Error('lost receipt') })
    driver.offer({ idle: () => true, request, execute: vi.fn(), failed })()
    await tick(); driver.flush(); await tick()
    expect(failed).toHaveBeenCalledTimes(1); expect(request).toHaveBeenCalledTimes(1)
  })
  it('holds a prepared result during temporary activity and resumes without replanning', async () => {
    const driver = new AgentContinuationDriver()
    let idle = true
    let resolve!: (value: { status: string; turn: AgentTurn }) => void
    const request = vi.fn(() => new Promise<{ status: string; turn: AgentTurn }>((done) => { resolve = done }))
    const execute = vi.fn(async () => undefined)
    driver.offer({ idle: () => idle, request, execute, failed: vi.fn() })()
    idle = false; resolve({ status: 'ready', turn }); await tick()
    expect(execute).not.toHaveBeenCalled()
    idle = true; driver.flush(); await tick()
    expect(request).toHaveBeenCalledTimes(1); expect(execute).toHaveBeenCalledTimes(1)
  })
  it('keeps failure feedback until speech is idle without repeating the request', async () => {
    const driver = new AgentContinuationDriver()
    let idle = true
    let reject!: (cause: Error) => void
    const request = vi.fn(() => new Promise<{ status: string }>((_, fail) => { reject = fail }))
    const failed = vi.fn()
    driver.offer({ idle: () => idle, request, execute: vi.fn(), failed })()
    idle = false; reject(new Error('lost response')); await tick()
    expect(failed).not.toHaveBeenCalled()
    idle = true; driver.flush(); await tick()
    expect(failed).toHaveBeenCalledTimes(1)
    expect(request).toHaveBeenCalledTimes(1)
  })
})
