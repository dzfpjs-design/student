import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { runDiagnosticVoice } from './diagnosticVoiceFlow'
import type { DiagnosticPromptResult, SessionWorkspaceState } from './types'

vi.mock('./api', async original => ({ ApiError: (await original<typeof import('./api')>()).ApiError,
  localApi: { diagnosticPrompt: vi.fn(), admitOriginalClientExecution: vi.fn(), prepareDiagnosticPrompt: vi.fn(), answerDiagnosticPrompt: vi.fn(), resumeDiagnosticPrompt: vi.fn(), completePlanItem: vi.fn(), planReviewQuestion:vi.fn() } }))
const workspace = { session: { session_id: 'session-a', child_id: 'child-a', state: 'check_in' }, plan_items: [] } as unknown as SessionWorkspaceState
const empty = { status: 'no_item', prompt: null, attempt: null, reason: null } as DiagnosticPromptResult
const binding = { diagnostic_prompt_id: 'prompt-a', diagnostic_item_version: 'v1' }
const waiting = { ...empty, status: 'waiting_answer', prompt: {
  prompt_id: 'prompt-a', item_version: 'v1', diagnostic_item_id: 'item-a', review_task_id: null,
  child_id: 'child-a', origin_session_id: 'session-a', created_at: 'now',
  item: { diagnostic_item_id: 'item-a', track: 'school', subject: '数学', prompt: '结果是多少？', stimulus_text: '3加4' },
} } as DiagnosticPromptResult

describe('page independent diagnostic voice', () => {
  beforeEach(() => { vi.resetAllMocks(); vi.mocked(localApi.admitOriginalClientExecution).mockResolvedValue({status:'admitted'}) })
  it('explicit independent challenge preserves a completed review instead of reusing it', async () => {
    const context = { ...workspace, plan_items: [{ plan_item_id: 'review-old', status: 'active',
      source_type: 'due_review', source_ref: 'review-source' }] } as SessionWorkspaceState
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue({ ...waiting, status: 'answered',
      prompt: { ...waiting.prompt!, item: { ...waiting.prompt!.item, assessment_mode: 'open_response_rubric' } } })
    vi.mocked(localApi.prepareDiagnosticPrompt).mockResolvedValue(waiting)
    await runDiagnosticVoice(context, { type: 'start_diagnostic', track: 'school', subject: '数学', knowledge_node_id: 'current-node', require_unexposed: true }, 'explicit-new', () => true)
    expect(localApi.prepareDiagnosticPrompt).toHaveBeenCalledWith('session-a', {
      track: 'school', preferred_item_id: undefined, review_task_id: undefined,
      require_unexposed: true, client_action_id: 'voice-diagnostic:explicit-new', paper_authorized_turn_id:'explicit-new',
      subject: '数学', knowledge_node_id: 'current-node',
    })
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
    expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  })
  it('requests an unseen source for a new independent challenge and does not label a repeat as new', async () => {
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue({ ...empty, status: 'answered' })
    vi.mocked(localApi.prepareDiagnosticPrompt).mockResolvedValue({ ...empty, reason: 'unexposed_item_unavailable' })
    const result = await runDiagnosticVoice(workspace, { type: 'start_diagnostic', track: 'school' }, 'fresh-question', () => true)
    expect(localApi.prepareDiagnosticPrompt).toHaveBeenCalledWith('session-a', expect.objectContaining({ require_unexposed: true }))
    expect(result.response).toContain('没有核对通过且未见过的新题')
    expect(result.playbackAllowed).not.toBe(true)
    expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  })
  it.each(['start_diagnostic', 'replay_diagnostic', 'submit_diagnostic_answer'] as const)('keeps a withdrawn daily source from silently starting or grading another question: %s', async type => {
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue({ ...waiting, status: 'invalidated', reason: 'source_withdrawn' })
    const result = await runDiagnosticVoice(workspace, { type, ...binding, answer: '7' }, 'withdrawn-source', () => true)
    expect(result.response).toContain('之前保存的回答还在')
    expect(result.response).toContain('等合适的新题补齐')
    expect(result.response).not.toContain('重新选题')
    expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
    expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
    expect(localApi.resumeDiagnosticPrompt).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
  })
  it('explains saved answers and held feedback without inventing mastery or a next task', async () => {
    const saved = { ...waiting, status: 'answered', attempt: { attempt_id: 'saved', feedback: '已保存',
      child_id: 'child-a', session_id: 'session-a', diagnostic_item_id: 'item-a' },
      local_learning_evaluation: { status: 'blocked', reason: 'scheduled_break_control_changed',
        local_only: true, cloud_transmission_allowed: false } } as DiagnosticPromptResult
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(waiting)
    vi.mocked(localApi.answerDiagnosticPrompt).mockResolvedValue(saved)
    const result = await runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '7', ...binding }, 'saved-break', () => true)
    expect(result.response).toContain('不用再答一遍')
    expect(result.response).toContain('休息记录')
    expect(result.response).not.toContain('后续还会')
    expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
  })
  it.each(['valid', 'wrong_attempt', 'wrong_child', 'correct_answer', 'wrong_task'])('keeps proactive foundation followup bound to its saved result: %s', async change => {
    const context = { ...workspace, plan_items: [{ plan_item_id: 'foundation-task', task_type: 'goal_prerequisite', status: 'active' }] } as SessionWorkspaceState
    const result = { ...waiting, status: 'answered', attempt: {
      attempt_id: 'saved-attempt', evidence_id: 'saved-evidence', child_id: 'child-a', session_id: 'session-a',
      diagnostic_item_id: 'item-a', submitted_answer: '8', feedback: '还需核对', correctness: 0, score_status: 'verified_local', created_at: 'now',
    }, foundation_remediation: {
      origin: { prompt_id: 'prompt-a', item_version: 'v1', attempt_id: 'saved-attempt', evidence_id: 'saved-evidence', plan_item_id: 'foundation-task' },
      mistake_id: 'actual-mistake', phase: 'diagnosis_probe', response_text: '我们先核对一个步骤：三加四是多少？', local_only: true, cloud_transmission_allowed: false,
    } } as DiagnosticPromptResult
    if (change === 'wrong_attempt') result.foundation_remediation!.origin.attempt_id = 'another-attempt'
    if (change === 'wrong_child') result.attempt!.child_id = 'sibling'
    if (change === 'correct_answer') result.attempt!.correctness = 1
    if (change === 'wrong_task') result.foundation_remediation!.origin.plan_item_id = 'other-task'
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(waiting)
    vi.mocked(localApi.answerDiagnosticPrompt).mockResolvedValue(result)
    const operation = runDiagnosticVoice(context, { type: 'submit_diagnostic_answer', answer: '8', ...binding }, 'foundation-answer', () => true)
    if (change === 'valid') expect((await operation).response).toBe(result.foundation_remediation!.response_text)
    else await expect(operation).rejects.toThrow('后续核对与原题')
    expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
  })
  it('preserves early listening speech without replaying audio or consuming its later confirmation key', async () => {
    const listening = { ...waiting, prompt: { ...waiting.prompt!, review_task_id: 'review-a', item: { ...waiting.prompt!.item,
      track: 'pet', answer_type: 'listening_text' } } }
    const retained = { ...listening, deferred_answer: { version: 'listening_review_deferred_v1',
      source_turn_id: 'early', source_voice_input_id: 'early-voice', speech_retained: true, formal_answer_saved: false,
      automatic_submission_allowed: false, new_confirmation_required: true, delivery_complete: false,
      local_only: true, cloud_transmission_allowed: false, message: '刚才的话留着，听完后再确认。' } }
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValueOnce(listening).mockResolvedValue(retained)
    vi.mocked(localApi.answerDiagnosticPrompt).mockRejectedValueOnce(new ApiError('generic conflict', 409,
      'DIAGNOSTIC_PROMPT_LISTENING_REVIEW_DELIVERY_REQUIRED'))
    const result = await runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: 'seven', ...binding }, 'early', () => true)
    expect(result.response).toBe(retained.deferred_answer.message)
    expect(result).toMatchObject({ playbackAllowed: false, result: { status: 'waiting_answer', attempt: null } })
    vi.mocked(localApi.answerDiagnosticPrompt).mockResolvedValue({ ...listening, status: 'answered', attempt: { attempt_id: 'new-answer', feedback: '已保存' } } as DiagnosticPromptResult)
    await runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: 'seven', ...binding }, 'confirmed-later', () => true)
    const calls = vi.mocked(localApi.answerDiagnosticPrompt).mock.calls
    expect(calls[1][1].client_action_id).not.toBe(calls[0][1].client_action_id)
    expect(calls[1][1].authorized_turn_id).toBe('confirmed-later')
    expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
  })
  it('does not speak the transcript instead of the original listening source', async () => {
    const listening = { ...waiting, prompt: { ...waiting.prompt!, item: { ...waiting.prompt!.item,
      track: 'pet', answer_type: 'text', pet_audio_modality: 'listening' as const, stimulus_text: 'secret original transcript' } } }
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(listening)
    const result = await runDiagnosticVoice(workspace, { type: 'start_diagnostic' }, 'listen', () => true)
    expect(result.response).not.toContain('secret original transcript')
    expect(result.response).toContain('结果是多少')
  })
  it('prepares the concrete question during check in, with no screen listener', async () => {
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(empty)
    vi.mocked(localApi.prepareDiagnosticPrompt).mockResolvedValue(waiting)
    const result = await runDiagnosticVoice(workspace, { type: 'start_diagnostic' }, 'turn-a', () => true)
    expect(result.response).toContain('3加4')
    expect(result.response).toContain('结果是多少')
    expect(result.result.status).toBe('waiting_answer')
  })
  it('does not prepare another question on repeated start or replay', async () => {
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(waiting)
    await runDiagnosticVoice(workspace, { type: 'start_diagnostic' }, 'turn-b', () => true)
    expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  })
  it('reuses one answer key after a lost receipt, without changing the item version', async () => {
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(waiting)
    vi.mocked(localApi.answerDiagnosticPrompt).mockRejectedValueOnce(new Error('lost receipt')).mockResolvedValueOnce({ ...waiting, status: 'answered', attempt: { attempt_id: 'a', feedback: '已保存' } } as DiagnosticPromptResult)
    await expect(runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '7', ...binding }, 'turn-c', () => true)).rejects.toThrow('lost receipt')
    await runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '7', ...binding }, 'turn-d', () => true)
    const first = vi.mocked(localApi.answerDiagnosticPrompt).mock.calls[0][1]
    const second = vi.mocked(localApi.answerDiagnosticPrompt).mock.calls[1][1]
    expect(first.authorized_turn_id).toBe('turn-c')
    expect(second).toEqual({ ...first, authorized_turn_id: 'turn-d' })
  })
  it('resumes a durable answer instead of attaching another utterance', async () => {
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue({ ...waiting, status: 'answer_saved' })
    vi.mocked(localApi.resumeDiagnosticPrompt).mockResolvedValue({ ...waiting, status: 'answered', attempt: { attempt_id: 'a', feedback: '原回答已保存' } } as DiagnosticPromptResult)
    const result = await runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '另一句话', ...binding }, 'turn-e', () => true)
    expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
    expect(result.response).toContain('原回答已保存')
  })
  it('does not grade without a question, invent an item, or continue a cancelled request', async () => {
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(empty)
    await expect(runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '7' }, 'turn-f', () => true)).rejects.toThrow('没有绑定待答题')
    await expect(runDiagnosticVoice(workspace, { type: 'start_diagnostic' }, 'turn-g', () => false)).rejects.toThrow('暂停')
    expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
    expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  })
  it('rejects a late answer if a response carries a different question binding', async () => {
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue({ ...waiting, prompt: { ...waiting.prompt!, prompt_id: 'later-question' } })
    await expect(runDiagnosticVoice(workspace, { type: 'submit_diagnostic_answer', answer: '7', ...binding }, 'late', () => true)).rejects.toThrow('版本已变化')
    expect(localApi.diagnosticPrompt).toHaveBeenCalledWith('session-a', 'prompt-a')
    expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  })
  it('recovers post-scoring effects and next prompt after a lost completion response', async () => {
    const attempt = { attempt_id: 'saved-attempt', child_id: 'child-a', session_id: 'session-a',
      review_task_id: 'review-a', evidence_id: 'evidence-a', feedback: '原回答已评分',
      next_specialized_item: { ...waiting.prompt!.item, diagnostic_item_id: 'next-item' } }
    const answered = { ...waiting, status: 'answered', attempt } as DiagnosticPromptResult
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(answered)
    vi.mocked(localApi.resumeDiagnosticPrompt).mockResolvedValue(answered)
    vi.mocked(localApi.prepareDiagnosticPrompt).mockResolvedValue(waiting)
    const reviewing = { ...workspace, plan_items: [{ plan_item_id: 'plan-a', source_type: 'due_review',
      source_ref: 'review-a', status: 'active', actual_minutes: 0 }] } as unknown as SessionWorkspaceState
    await runDiagnosticVoice(reviewing, { type: 'submit_diagnostic_answer', answer: '7', ...binding }, 'recover', () => true)
    expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
    expect(localApi.resumeDiagnosticPrompt).toHaveBeenCalledWith('session-a', waiting.prompt)
    expect(localApi.completePlanItem).toHaveBeenCalledWith('plan-a', expect.any(Number), expect.any(Object))
    expect(localApi.prepareDiagnosticPrompt).toHaveBeenCalledWith('session-a', expect.objectContaining({
      preferred_item_id: 'next-item', client_action_id: 'diagnostic-after:saved-attempt',
    }))
  })
})


it('ordinary active due review keeps its trusted preferred binding ahead of a paper selection',async()=>{
  const review={plan_item_id:'due-plan',session_id:'session-a',child_id:'child-a',review_task_id:'due-review',
    diagnostic_item_id:'due-item',track:'school',prompt:'原复测题',review_status:'pending'}
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(empty)
  vi.mocked(localApi.planReviewQuestion).mockResolvedValue(review as never)
  vi.mocked(localApi.prepareDiagnosticPrompt).mockResolvedValue(waiting)
  await runDiagnosticVoice({...workspace,plan_items:[{plan_item_id:'due-plan',source_type:'due_review',source_ref:'due-review',status:'active'}]} as SessionWorkspaceState,
    {type:'start_diagnostic'},'due-turn',()=>true)
  const request=vi.mocked(localApi.prepareDiagnosticPrompt).mock.calls.at(-1)![1]
  expect(request.preferred_item_id).toBe('due-item');expect(request.review_task_id).toBe('due-review')
  expect(request).not.toHaveProperty('paper_authorized_turn_id')
})
