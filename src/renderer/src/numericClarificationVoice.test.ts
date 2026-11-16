import { beforeEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { runDiagnosticVoice } from './diagnosticVoiceFlow'
import type { DiagnosticPromptResult, SessionWorkspaceState } from './types'

vi.mock('./api', async original => ({ ...(await original<typeof import('./api')>()), localApi: {
  diagnosticPrompt: vi.fn(), answerDiagnosticPrompt: vi.fn(), resumeDiagnosticPrompt: vi.fn(),
  prepareDiagnosticPrompt: vi.fn(), completePlanItem: vi.fn(),
} }))
const workspace = { session: { session_id: 's', child_id: 'c' }, plan_items: [] } as unknown as SessionWorkspaceState
const prompt = { prompt_id: 'p', item_version: 'v', diagnostic_item_id: 'i', child_id: 'c', origin_session_id: 's',
  review_task_id: null, item: { track: 'competition_math', prompt: '一共有多少本书？', stimulus_text: '' } }
const offer = { parent_prompt_id: 'p', parent_item_version: 'v', parent_attempt_id: 'a' }
const waiting = { status: 'waiting_answer', prompt, attempt: null } as DiagnosticPromptResult
const saved = { status: 'answered', prompt, numeric_clarification: offer, attempt: {
  attempt_id: 'a', diagnostic_item_id: 'i', child_id: 'c', session_id: 's', correctness: null,
  score_status: 'needs_review', mastery_update_allowed: false, feedback: '请只说一个数。',
} } as unknown as DiagnosticPromptResult
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(waiting)
  vi.mocked(localApi.answerDiagnosticPrompt).mockResolvedValue(saved)
  vi.mocked(localApi.resumeDiagnosticPrompt).mockResolvedValue(saved)
  vi.mocked(localApi.prepareDiagnosticPrompt).mockResolvedValue({ ...waiting, prompt: { ...waiting.prompt!, prompt_id: 'p2' } })
})
it('arms the exact clarification before asking the child for another answer', async () => {
  const result = await runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '两三',
    diagnostic_prompt_id: 'p', diagnostic_item_version: 'v' }, 'turn', () => true)
  expect(localApi.prepareDiagnosticPrompt).toHaveBeenCalledWith('s', {
    track: 'competition_math', preferred_item_id: 'i', review_task_id: undefined,
    client_action_id: 'numeric-clarification:a', numeric_clarification: offer,
  })
  expect(result.result).toMatchObject({ status: 'waiting_answer', prompt: { prompt_id: 'p2' } })
  expect(result.response).toContain('只说一个数')
  expect(localApi.completePlanItem).not.toHaveBeenCalled()
})

it('recovers a lost preparation reply with the same original key, not another answer', async () => {
  vi.mocked(localApi.prepareDiagnosticPrompt).mockRejectedValueOnce(new Error('lost reply'))
  const action = { type: 'submit_diagnostic_answer' as const, answer: '两三', diagnostic_prompt_id: 'p', diagnostic_item_version: 'v' }
  await expect(runDiagnosticVoice(workspace, action, 'turn', () => true)).rejects.toThrow('lost reply')
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(saved)
  const recovered = await runDiagnosticVoice(workspace, action, 'recover', () => true)
  const calls = vi.mocked(localApi.prepareDiagnosticPrompt).mock.calls
  expect(calls[0]).toEqual(calls[1])
  expect(localApi.answerDiagnosticPrompt).toHaveBeenCalledTimes(1)
  expect(recovered.result.prompt?.prompt_id).toBe('p2')
})

it('does not create a clarification after privacy or session scope changes', async () => {
  let current = true
  vi.mocked(localApi.answerDiagnosticPrompt).mockImplementation(async () => { current = false; return saved })
  await expect(runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '两三',
    diagnostic_prompt_id: 'p', diagnostic_item_version: 'v' }, 'turn', () => current)).rejects.toThrow('暂停')
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
})

it('rejects a response bound to another original before preparing anything', async () => {
  vi.mocked(localApi.answerDiagnosticPrompt).mockResolvedValue({ ...saved,
    numeric_clarification: { ...offer, parent_attempt_id: 'another-original' } })
  await expect(runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '两三',
    diagnostic_prompt_id: 'p', diagnostic_item_version: 'v' }, 'turn', () => true)).rejects.toThrow('未核对一致')
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
})

it('never invites an answer when the successor belongs to another question', async () => {
  vi.mocked(localApi.prepareDiagnosticPrompt).mockResolvedValue({ ...waiting,
    prompt: { ...waiting.prompt!, prompt_id: 'p2', diagnostic_item_id: 'unrelated' } })
  await expect(runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '两三',
    diagnostic_prompt_id: 'p', diagnostic_item_version: 'v' }, 'turn', () => true)).rejects.toThrow('澄清题目已变化')
})
