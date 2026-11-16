import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { runDiagnosticVoice } from './diagnosticVoiceFlow'
import type { DiagnosticPromptResult, SessionWorkspaceState } from './types'

const workspace = { session: { session_id: 'session', child_id: 'child', state: 'study' }, plan_items: [
  { plan_item_id: 'plan', source_type: 'due_review', source_ref: 'review', status: 'active', actual_minutes: 0 },
] } as unknown as SessionWorkspaceState
const attempt = { attempt_id: 'answer', evidence_id: 'evidence', child_id: 'child', session_id: 'session',
  review_task_id: 'review', diagnostic_item_id: 'item', correctness: null, submitted_answer: '原稿', feedback: '原稿已保存' }
const answered = { status: 'answered', prompt: { prompt_id: 'prompt', item_version: 'v1', diagnostic_item_id: 'item',
  child_id: 'child', origin_session_id: 'session', review_task_id: 'review',
  item: { diagnostic_item_id: 'item', assessment_mode: 'open_response_rubric', prompt: '解释你的依据' } }, attempt } as DiagnosticPromptResult
const review = { plan_item_id: 'plan', child_id: 'child', session_id: 'session', review_task_id: 'review',
  diagnostic_item_id: 'item', prompt: '解释你的依据', assessment_mode: 'open_response_rubric', review_interval: 'school_open_near',
  review_status: 'pending', attempt: null }
afterEach(() => vi.restoreAllMocks())
it.each(['start_diagnostic', 'replay_diagnostic', 'submit_diagnostic_answer'] as const)('retains the original awaiting school review on %s', async type => {
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(answered)
  vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValue(review as never)
  const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt')
  const submit = vi.spyOn(localApi, 'answerDiagnosticPrompt')
  const resume = vi.spyOn(localApi, 'resumeDiagnosticPrompt')
  const complete = vi.spyOn(localApi, 'completePlanItem')
  const result = await runDiagnosticVoice(workspace, { type, answer: '再说一遍', diagnostic_prompt_id: 'prompt', diagnostic_item_version: 'v1' }, 'voice', () => true)
  expect(result.response).toContain('原稿已经保存')
  expect(result.response).toContain('评分')
  expect(result.result).toBe(answered)
  for (const call of [prepare, submit, resume, complete]) expect(call).not.toHaveBeenCalled()
})
it('does not report saved success for a different child or review receipt', async () => {
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({ ...answered, attempt: { ...attempt, child_id: 'other' } } as never)
  vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValue(review as never)
  await expect(runDiagnosticVoice(workspace, { type: 'replay_diagnostic' }, 'voice', () => true)).rejects.toThrow('不匹配')
})
it('recovers answer_saved processing once but does not confuse it with a reliable score', async () => {
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({ ...answered, status: 'answer_saved', attempt: null })
  vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValue(review as never)
  const resume = vi.spyOn(localApi, 'resumeDiagnosticPrompt').mockResolvedValue(answered)
  const complete = vi.spyOn(localApi, 'completePlanItem')
  const submit = vi.spyOn(localApi, 'answerDiagnosticPrompt')
  const result = await runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '新话语',
    diagnostic_prompt_id: 'prompt', diagnostic_item_version: 'v1' }, 'voice', () => true)
  expect(resume).toHaveBeenCalledExactlyOnceWith('session', answered.prompt)
  expect(result.response).toContain('评分还需要核对')
  expect(submit).not.toHaveBeenCalled(); expect(complete).not.toHaveBeenCalled()
})
it('finishes execution only from the exact reliably reviewed receipt, even when raw correctness is null', async () => {
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(answered)
  vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValue({ ...review, review_status: 'completed', attempt } as never)
  vi.spyOn(localApi, 'resumeDiagnosticPrompt').mockResolvedValue(answered)
  const complete = vi.spyOn(localApi, 'completePlanItem').mockResolvedValue({} as never)
  const result = await runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '完成了',
    diagnostic_prompt_id: 'prompt', diagnostic_item_version: 'v1' }, 'voice', () => true)
  expect(complete).toHaveBeenCalledExactlyOnceWith('plan', expect.any(Number), expect.any(Object))
  expect(result.response).not.toContain('已经掌握')
})
it('rejects a later review receipt for a different original answer', async () => {
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(answered)
  vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValue({ ...review, review_status: 'completed', attempt: { ...attempt, attempt_id: 'other' } } as never)
  vi.spyOn(localApi, 'resumeDiagnosticPrompt').mockResolvedValue(answered)
  const complete = vi.spyOn(localApi, 'completePlanItem')
  await expect(runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '完成了',
    diagnostic_prompt_id: 'prompt', diagnostic_item_version: 'v1' }, 'voice', () => true)).rejects.toThrow('尚未核对一致')
  expect(complete).not.toHaveBeenCalled()
})
