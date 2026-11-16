import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { runDiagnosticVoice } from './diagnosticVoiceFlow'
import type { DiagnosticPromptResult, SessionWorkspaceState } from './types'

const workspace = { session: { session_id: 'original-session', child_id: 'original-child' }, plan_items: [] } as unknown as SessionWorkspaceState
const action = { type: 'start_diagnostic', track: 'school' } as const
const prompt = { prompt_id: 'original-prompt', item_version: 'original-version', child_id: 'original-child', origin_session_id: 'original-session',
  item: { track: 'school', stimulus_text: '', prompt: '公开合成题' } }
const waiting = { status: 'waiting_answer', prompt } as unknown as DiagnosticPromptResult
afterEach(() => vi.restoreAllMocks())

describe('ordinary original diagnostic bridge admission', () => {
  it('awaits the one admission before preparing with the original idempotency key', async () => {
    const order: string[] = []
    vi.spyOn(localApi, 'diagnosticPrompt').mockImplementation(async () => { order.push('read'); return { status: 'no_item' } as DiagnosticPromptResult })
    const admit = vi.spyOn(localApi, 'admitOriginalClientExecution').mockImplementation(async () => { order.push('admit'); return { status: 'admitted' } })
    const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt').mockImplementation(async () => { order.push('prepare'); return waiting })
    const result = await runDiagnosticVoice(workspace, action, 'original-turn', () => true)
    expect(order).toEqual(['read', 'admit', 'prepare'])
    expect(admit).toHaveBeenCalledExactlyOnceWith('original-session', 'original-turn')
    expect(prepare).toHaveBeenCalledExactlyOnceWith('original-session', expect.objectContaining({ client_action_id: 'voice-diagnostic:original-turn', paper_authorized_turn_id: 'original-turn' }))
    expect(result.result).toBe(waiting)
  })

  it.each(['execution_receipt_required', 'timeout', 'error'])('does not repeat admission or prepare after %s', async mode => {
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({ status: 'no_item' } as DiagnosticPromptResult)
    const admit = vi.spyOn(localApi, 'admitOriginalClientExecution').mockImplementation(async () => {
      if (mode !== 'execution_receipt_required') throw new Error(mode)
      return { status: mode }
    })
    const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt')
    await expect(runDiagnosticVoice(workspace, action, 'original-turn', () => true)).rejects.toThrow('学习进度')
    expect(admit).toHaveBeenCalledTimes(1)
    expect(prepare).not.toHaveBeenCalled()
  })

  it('rechecks current ownership after the asynchronous admission before any prepare', async () => {
    let current = true
    let finish!: (v: { status: string }) => void
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({ status: 'no_item' } as DiagnosticPromptResult)
    const admit = vi.spyOn(localApi, 'admitOriginalClientExecution').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt')
    const run = runDiagnosticVoice(workspace, action, 'original-turn', () => current)
    await vi.waitFor(() => expect(admit).toHaveBeenCalledOnce())
    current = false; finish({ status: 'admitted' })
    await expect(run).rejects.toThrow('已暂停')
    expect(prepare).not.toHaveBeenCalled()
  })

  it('recovers the existing prompt without a second admission or prepare', async () => {
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(waiting)
    const admit = vi.spyOn(localApi, 'admitOriginalClientExecution')
    const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt')
    expect((await runDiagnosticVoice(workspace, action, 'original-turn', () => true)).result).toBe(waiting)
    expect(admit).not.toHaveBeenCalled(); expect(prepare).not.toHaveBeenCalled()
  })

  it('keeps the separately admitted continuation path unchanged', async () => {
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({ status: 'no_item' } as DiagnosticPromptResult)
    const admit = vi.spyOn(localApi, 'admitOriginalClientExecution')
    const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt').mockResolvedValue(waiting)
    await runDiagnosticVoice(workspace, action, 'prepared-continuation', () => true, false)
    expect(admit).not.toHaveBeenCalled(); expect(prepare).toHaveBeenCalledOnce()
  })
})
