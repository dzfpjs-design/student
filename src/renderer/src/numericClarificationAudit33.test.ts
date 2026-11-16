import { beforeEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { runDiagnosticVoice } from './diagnosticVoiceFlow'
import type { DiagnosticPromptResult, SessionWorkspaceState } from './types'

vi.mock('./api', async original => ({ ...(await original<typeof import('./api')>()), localApi: {
  diagnosticPrompt: vi.fn(), answerDiagnosticPrompt: vi.fn(), resumeDiagnosticPrompt: vi.fn(),
  prepareDiagnosticPrompt: vi.fn(), completePlanItem: vi.fn(),
} }))
const workspace = { session: { session_id:'session-a', child_id:'child-a' }, plan_items:[] } as unknown as SessionWorkspaceState
const prompt = { prompt_id:'original', item_version:'v1', diagnostic_item_id:'item-a', child_id:'child-a',
  origin_session_id:'session-a', review_task_id:null, item:{ track:'competition_math', prompt:'How many?', stimulus_text:'' } }
const original = { status:'answered', prompt, numeric_clarification:{ parent_prompt_id:'original', parent_item_version:'v1', parent_attempt_id:'attempt-a' },
  attempt:{ attempt_id:'attempt-a', child_id:'child-a', session_id:'session-a', diagnostic_item_id:'item-a', correctness:null,
    mastery_update_allowed:false, score_status:'needs_review', feedback:'One number, please.' } } as unknown as DiagnosticPromptResult
const waiting = { status:'waiting_answer', prompt:{ ...prompt, prompt_id:'clarification' }, attempt:null } as DiagnosticPromptResult
const action = { type:'submit_diagnostic_answer' as const, diagnostic_prompt_id:'original', diagnostic_item_version:'v1', answer:'two or three' }

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(original)
  vi.mocked(localApi.resumeDiagnosticPrompt).mockResolvedValue(original)
  vi.mocked(localApi.prepareDiagnosticPrompt).mockResolvedValue(waiting)
})

it.each(['child', 'session', 'focus', 'privacy'])('discards a preparation reply after %s epoch changes without another answer', async () => {
  let current = true
  let deliver!: (value: DiagnosticPromptResult) => void
  const deferred = new Promise<DiagnosticPromptResult>(resolve => { deliver = resolve })
  vi.mocked(localApi.prepareDiagnosticPrompt).mockReturnValue(deferred)
  const running = runDiagnosticVoice(workspace, action, 'original-turn', () => current)
  await vi.waitFor(() => expect(localApi.prepareDiagnosticPrompt).toHaveBeenCalledTimes(1))
  current = false
  deliver(waiting)
  await expect(running).rejects.toThrow('暂停')
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.completePlanItem).not.toHaveBeenCalled()
})

it.each(['child', 'session'])('rejects cross-%s recovered successor even when the item matches', async field => {
  vi.mocked(localApi.prepareDiagnosticPrompt).mockResolvedValue({ ...waiting, prompt:{ ...waiting.prompt!,
    ...(field === 'child' ? { child_id:'child-b' } : { origin_session_id:'session-b' }) } })
  await expect(runDiagnosticVoice(workspace, action, 'original-turn', () => true)).rejects.toThrow('澄清题目已变化')
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.completePlanItem).not.toHaveBeenCalled()
})

it('does not invite another answer when original-key replay returns the cancelled older focus', async () => {
  vi.mocked(localApi.prepareDiagnosticPrompt).mockResolvedValue({ ...waiting, status:'cancelled' })
  const result = await runDiagnosticVoice(workspace, action, 'original-turn', () => true)
  expect(result.response).toContain('没有进入待答状态')
  expect(result.result.status).toBe('cancelled')
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.prepareDiagnosticPrompt).toHaveBeenCalledWith('session-a', expect.objectContaining({client_action_id:'numeric-clarification:attempt-a'}))
})
