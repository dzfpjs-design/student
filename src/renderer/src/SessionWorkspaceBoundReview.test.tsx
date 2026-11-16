import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import type { BoundReview } from './boundReview'
import * as LocalInputs from './LocalInputs'
import * as voice from './companionVoice'
import * as listening from './petListeningPlayback'
import { SessionWorkspace } from './SessionWorkspace'
import { clearVoiceCaptureContext, voiceCaptureReference, trackDiagnosticCapture } from './voiceCaptureContext'
import type { AgentTurn, DiagnosticAttempt, DiagnosticPromptResult, SessionWorkspaceState } from './types'

let microphone: ComponentProps<typeof LocalInputs.HandsFreeVoiceInput>
vi.mock('./LocalInputs', async (original) => {
  const actual = await original<typeof import('./LocalInputs')>()
  return { ...actual, HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null }, VoiceInputButton: () => null }
})
vi.mock('./VoiceCalibrationPanel', async () => {
  const { useEffect } = await import('react')
  return { VoiceCalibrationPanel: ({ onMicrophoneBindingReadyChange }: { onMicrophoneBindingReadyChange: (ready: boolean) => void }) => {
    useEffect(() => { onMicrophoneBindingReadyChange(true) }, [onMicrophoneBindingReadyChange])
    return null
  } }
})
// Device/model acquisition is outside this review binding test. The study and
// command panels, event bridge and boundReview validation remain real.
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: () => null }))

const stamp = '2026-09-05T12:00:00Z'
function workspace(status = 'active'): SessionWorkspaceState {
  return {
    session: { session_id: 'session-review-a', child_id: 'child-a', status: 'active', state: 'study', started_at: stamp, last_checkpoint_at: stamp, observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status: 'paused', snooze_until: null, recall_declared_empty: 0, homework_declared_empty: 0 },
    family_rule: { child_id: 'child-a', latest_end_time: '21:30', max_learning_minutes: 120, task_segment_minutes: 25, break_minutes: 5, pet_min_minutes: 0, competition_minutes: 0, observation_mode: 'shadow', reminder_budget_per_10m: 1, misclassification_shadow_threshold: 3, version: 1, updated_at: stamp },
    planning_context: { energy_level: 'medium', fixed_commitment_minutes: 0, fixed_commitment_note: null, tomorrow_event_subject: null, tomorrow_event_title: null, tomorrow_event_minutes: 0, updated_at: stamp },
    recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [], drafts: {}, latest_tutor_turn: null, daily_report_ready: false,
    plan: { plan_id: 'plan-a', plan_date: '2026-09-05', status: 'active', total_minutes: 5, generator: 'local_rule_engine', version: 1 },
    plan_items: [{ plan_item_id: 'plan-review-a', homework_id: null, subject: '数学', title: '二次方程复测', expected_minutes: 5, actual_minutes: null, priority: 1, status, position: 1, started_at: stamp, completed_at: null, active_seconds: 60, active_segment_started_at: null, source_type: 'due_review', source_ref: 'review-exact-a', completion_standard: '独立复测并保存回执', cognitive_load: 'medium', task_type: 'test_prep', completion_evidence_type: null, completion_note: null, completion_checklist: [], completion_artifact_id: null }],
    sensor_release: { opened_leases: 0, active_leases: 0, released_leases: 0, application_release_confirmed: false, evidence_level: 'not_opened', os_release_verified: false, release_verification: null, by_device: {} },
  }
}
const review = {
  plan_item_id: 'plan-review-a', session_id: 'session-review-a', child_id: 'child-a', review_task_id: 'review-exact-a', review_status: 'pending', attempt: null,
  diagnostic_item_id: 'question-exact-a', title: '独立判断根的个数', prompt: '这个方程有几个实数解？', track: 'school', answer_type: 'short_text',
} as BoundReview
const attempt: DiagnosticAttempt = { attempt_id: 'attempt-a', evidence_id: 'evidence-attempt-a', child_id: 'child-a', session_id: 'session-review-a', diagnostic_item_id: 'question-exact-a', review_task_id: 'review-exact-a', submitted_answer: '不会', correctness: 0, feedback: '这次还不确定，下次换一道变式再核对', created_at: stamp }
const pending: DiagnosticPromptResult = { status: 'waiting_answer', prompt: { prompt_id: 'prompt-review-a', item_version: 'version-a', diagnostic_item_id: review.diagnostic_item_id, review_task_id: review.review_task_id, child_id: review.child_id, origin_session_id: review.session_id, item: review, created_at: stamp }, attempt: null, reason: null }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
function submitEvent(answer = '不会'): Promise<boolean> {
  return new Promise((resolve) => window.dispatchEvent(new CustomEvent('hsc-study-learning-voice-action', { detail: { action: 'submit_diagnostic_answer', answer, resolve } })))
}

describe('workspace bound due-review integration', () => {
  let current: SessionWorkspaceState
  let question: ReturnType<typeof deferred<BoundReview>>
  const spoken = () => vi.mocked(LocalInputs.speakLocally).mock.calls.map(([text]) => text)
  async function mount() {
    let view!: ReturnType<typeof render>
    await act(async () => { view = render(<SessionWorkspace initial={current} childName="小辰" childGrade="初三" onBack={vi.fn()} />) })
    return view
  }
  async function present() {
    const view = await mount()
    await act(async () => { question.resolve(review) })
    return view
  }
  async function pause() {
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '暂停任务' })) })
    expect(current.plan_items[0].status).toBe('paused')
  }
  async function privacyStop() {
    await act(async () => { await microphone.onTranscript('停止全部监听') })
  }

  it('restores the original oral review, actively opens its conversation and waits for actual capture before playback', async () => {
    const originalReview = { ...review, track: 'pet', answer_type: 'speaking', assessment_mode: 'pet_reference_rubric',
      prompt: 'Tell me about your school.' } as BoundReview
    const originalPrompt = { ...pending, prompt: { ...pending.prompt!, item: originalReview } } as DiagnosticPromptResult
    vi.mocked(localApi.diagnosticPrompt).mockImplementation(async sid => trackDiagnosticCapture(sid, async () => originalPrompt))
    const view = await mount()
    try {
      await act(async () => { question.resolve(originalReview) })
      const playback = vi.mocked(LocalInputs.speakLocally).mock.calls.find(call => call[0].includes(originalReview.prompt))
      expect(playback).toBeDefined()
      const policy = playback![4]!
      expect(policy).toMatchObject({ localOnly: true, beforePlayback: expect.any(Function) })
      const priorActivation = microphone.externalActivationSignal ?? 0
      let admission!: Promise<boolean>
      await act(async () => { admission = policy.beforePlayback!(new AbortController().signal) })
      expect(microphone.externalActivationSignal).toBeGreaterThan(priorActivation)
      let allowed = false
      void admission.then(value => { allowed = value })
      await act(async () => { microphone.onRuntimeStatusChange?.({ runtime: 'segmented_local', phase: 'listening',
        captureReady: false, speechActive: false, pendingUtterances: 0, recoverable: true }) })
      expect(allowed).toBe(false)
      await act(async () => { microphone.onRuntimeStatusChange?.({ runtime: 'segmented_local', phase: 'listening',
        captureReady: true, speechActive: false, pendingUtterances: 0, recoverable: true }) })
      expect(await admission).toBe(true)
      expect(localApi.diagnosticPrompt).toHaveBeenCalledWith(current.session.session_id, originalPrompt.prompt!.prompt_id)
      expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
    } finally { view.unmount() }
  })

  beforeEach(() => {
    clearVoiceCaptureContext()
    current = workspace()
    question = deferred<BoundReview>()
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
      getWakeWordSettings: vi.fn(async () => ({ childId: 'child-a', wakeWord: '小知', enabled: true, inactivitySeconds: 60, updatedAt: stamp })),
      saveWakeWordSettings: vi.fn(),
      getVoiceOnboarding: vi.fn(async () => ({ childId: 'child-a', version: 1, step: 'completed', updatedAt: stamp })),
      saveVoiceOnboarding: vi.fn(),
    }))
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('UNEXPECTED_NETWORK')))
    for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
      vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
        handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
    }
    vi.spyOn(voice, 'getCompanionVoiceState').mockReturnValue('idle')
    vi.spyOn(LocalInputs, 'speakLocally').mockImplementation((_text, _session, done) => { done?.('text_only') })
    vi.spyOn(localApi, 'planReviewQuestion').mockReturnValueOnce(question.promise).mockResolvedValue(review)
    vi.spyOn(localApi, 'answerDiagnostic').mockResolvedValue(attempt)
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(pending)
    vi.spyOn(localApi, 'answerDiagnosticPrompt').mockResolvedValue({ ...pending, status: 'answered', attempt })
    vi.spyOn(localApi, 'nextDiagnostic')
    vi.spyOn(localApi, 'sessionWorkspace').mockImplementation(async () => current)
    vi.spyOn(localApi, 'completePlanItem').mockImplementation(async () => { current = workspace('completed') })
    vi.spyOn(localApi, 'pausePlanItem').mockImplementation(async () => { current = workspace('paused') })
    vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new Error('UNEXPECTED_AGENT_TURN'))
  })
  afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it.each([
    ['school_open_near', '换一道类似题'],
    ['school_open_far', '换个情境试一试'],
    ['school_open_7d', '一周后再检查'],
  ])('presents school interval %s without internal codes or changing task identity', async (interval, label) => {
    const title = `独立判断根的个数 · ${interval}复测`
    current.plan_items[0] = { ...current.plan_items[0], title, source_ref: 'school-review:exact-a' }
    await mount()
    expect(screen.getAllByText(`独立判断根的个数 · ${label}`).length).toBeGreaterThan(0)
    expect(document.body.textContent).not.toContain(interval)
    expect(current.plan_items[0].title).toBe(title)
    expect(current.plan_items[0].source_ref).toBe('school-review:exact-a')
  })

  it('automatically reads the exact active plan question after completed onboarding restores', async () => {
    await present()
    expect(localApi.planReviewQuestion).toHaveBeenCalledExactlyOnceWith('plan-review-a')
    expect(screen.getByText(review.prompt)).toBeTruthy()
    expect(spoken()).toEqual([`我们复习“${review.title}”。先自己试一试，不会也可以直接告诉我。${review.prompt}`])
    expect(localApi.nextDiagnostic).not.toHaveBeenCalled()
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
  })

  it('does not lose an immediately loaded question behind completed-onboarding initialization', async () => {
    vi.mocked(localApi.planReviewQuestion).mockReset().mockResolvedValue(review)
    await mount()
    expect(screen.getByText(review.prompt)).toBeTruthy()
    expect(spoken().filter((text) => text.includes(review.prompt))).toHaveLength(1)
  })

  it('reads both writing material and task requirements locally and binds the next voice capture', async () => {
    const writing = { ...review, answer_type: 'writing', track: 'pet', title: '邮件写作复查',
      stimulus_text: 'Your friend has invited you to a concert.', prompt: 'Write a reply explaining when you can go and ask one question.' } as BoundReview
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue({ ...pending, prompt: { ...pending.prompt!, item: writing } })
    await mount()
    await act(async () => { question.resolve(writing) })
    expect(spoken()).toHaveLength(1)
    expect(spoken()[0]).toContain(writing.stimulus_text)
    expect(spoken()[0]).toContain(writing.prompt)
    expect(vi.mocked(LocalInputs.speakLocally).mock.calls[0][4]).toEqual({ localOnly: true, isCurrent: expect.any(Function) })
    expect(voiceCaptureReference(writing.session_id, writing.child_id).question_reference).toEqual({
      kind: 'diagnostic', binding: { prompt_id: pending.prompt!.prompt_id, item_version: pending.prompt!.item_version },
    })
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
  })

  it('does not read or bind a prompt belonging to another child', async () => {
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue({ ...pending,
      prompt: { ...pending.prompt!, child_id: 'different-child' } })
    await present()
    expect(spoken().some(text => text.includes(review.prompt))).toBe(false)
    expect(voiceCaptureReference(review.session_id, review.child_id).question_reference).toBeNull()
  })

  it.each([
    ['task.start', 'start_next_task', 'pending', '开始任务'],
    ['task.resume', 'resume_paused_task', 'paused', '继续刚才的任务'],
  ])('does not interrupt the automatically read bound question with the server %s acknowledgement', async (capability, type, status, utterance) => {
    current = workspace(status)
    await mount()
    expect(localApi.planReviewQuestion).not.toHaveBeenCalled()
    const turn = {
      agent_turn_id: 'server-start-bound-review', response_text: '好，计时开始。需要提示就叫我。',
      runtime_execution_location: 'server', runtime_capability: capability,
      action: { type, capability, execution_location: 'server', source: 'agent_runtime_deterministic' },
    } as AgentTurn
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn)
    const execute = vi.spyOn(localApi, 'executeAgentTurn').mockImplementationOnce(async () => {
      current = workspace('active')
      return { turn, execution: { capability, response_text: turn.response_text, result: current.plan_items[0], evidence: ['task_started'] } }
    })
    const legacyStart = vi.spyOn(localApi, 'startPlanItem')
    const legacyResume = vi.spyOn(localApi, 'resumePlanItem')
    const refresh = deferred<void>()
    vi.spyOn(localApi, 'agentRuntimeStatus').mockImplementationOnce(async () => {
      await refresh.promise
      throw new Error('Optional runtime status unavailable')
    })
    let command!: Promise<void>
    await act(async () => { command = Promise.resolve(microphone.onTranscript(utterance)) })
    expect(execute).toHaveBeenCalledExactlyOnceWith(turn.agent_turn_id)
    expect(localApi.planReviewQuestion).toHaveBeenCalledExactlyOnceWith('plan-review-a')
    await act(async () => { question.resolve(review) })
    const questionPrompt = `我们复习“${review.title}”。先自己试一试，不会也可以直接告诉我。${review.prompt}`
    expect(spoken()).toEqual([questionPrompt])
    await act(async () => { refresh.resolve(); await command })
    expect(spoken()).toEqual([questionPrompt])
    expect(spoken()).not.toContain(turn.response_text)
    expect(legacyStart).not.toHaveBeenCalled()
    expect(legacyResume).not.toHaveBeenCalled()
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
  })

  it.each(['task.start', 'task.resume'])('gives the due-review panel sole listening ownership after %s returns its prompt', async capability => {
    current = workspace('pending')
    const heardReview = { ...review, subject: '英语', track: 'pet', answer_type: 'listening_text',
      title: '辨别地点', prompt: 'Where will they meet?' } as BoundReview
    const heardPrompt = { ...pending, prompt: { ...pending.prompt!, item: heardReview } }
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(heardPrompt)
    const play = vi.spyOn(listening, 'playPetListening').mockResolvedValue({
      status: 'finished', response: '原音播完了，请说出你的答案。' })
    const owner = { agent_turn_id: 'listen-review-start', response_text: '先播放原题录音。',
      runtime_execution_location: 'server', runtime_capability: capability,
      action: { type: capability === 'task.start' ? 'start_next_task' : 'resume_paused_task',
        capability, execution_location: 'server', source: 'agent_runtime_deterministic' } } as AgentTurn
    vi.mocked(localApi.agentTurn).mockResolvedValue(owner)
    vi.spyOn(localApi, 'executeAgentTurn').mockImplementation(async () => {
      current = workspace('active')
      return { turn: owner, execution: { capability, response_text: owner.response_text,
        result: { ...current.plan_items[0], diagnostic_prompt: heardPrompt }, evidence: ['task_started'] } }
    })
    await mount()
    await act(async () => { await microphone.onTranscript('开始今天的听力复习') })
    await act(async () => { question.resolve(heardReview) })
    expect(localApi.planReviewQuestion).toHaveBeenCalledExactlyOnceWith('plan-review-a')
    expect(play).toHaveBeenCalledOnce()
    expect(play.mock.calls[0][1]).toEqual(heardPrompt.prompt)
    expect(spoken().filter(text => text === '原音播完了，请说出你的答案。')).toHaveLength(1)
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
  })

  it('reuses the original automatic playback receipt when the same review remounts', async () => {
    const heardReview = { ...review, track: 'pet', answer_type: 'listening_text' } as BoundReview
    const heardPrompt = { ...pending, prompt: { ...pending.prompt!, item: heardReview } }
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(heardPrompt)
    vi.mocked(localApi.planReviewQuestion).mockReset().mockResolvedValue(heardReview)
    const play = vi.spyOn(listening, 'playPetListening').mockResolvedValue({
      status: 'finished', response: '原播放记录已保存，不重复播放。' })
    const first = await mount()
    expect(play).toHaveBeenCalledOnce()
    first.unmount()
    await mount()
    expect(play).toHaveBeenCalledTimes(2)
    expect(play.mock.calls[1][2]).toBe(play.mock.calls[0][2])
    expect(play.mock.calls[0][2]).toContain(pending.prompt!.prompt_id)
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
  })

  it.each(['retained', 'revoked', 'other_prompt', 'read_failed'])('rechecks an early listening reply before speaking its %s recovery', async state => {
    const heardReview = { ...review, track: 'pet', answer_type: 'listening_text' } as BoundReview
    const note = { version: 'listening_review_deferred_v1', speech_retained: true, formal_answer_saved: false,
      automatic_submission_allowed: false, new_confirmation_required: true, delivery_complete: false,
      local_only: true, cloud_transmission_allowed: false, source_turn_id: 'early-turn', source_voice_input_id: 'early-voice',
      message: '还没有听完，我先记住你刚才说的。' }
    const original = { ...pending, prompt: { ...pending.prompt!, item: heardReview }, deferred_answer: note }
    const refreshed = { ...original, deferred_answer: { ...note,
      speech_retained: state !== 'revoked', new_confirmation_required: state !== 'revoked',
      delivery_complete: true, message: '刚才的话还留着，请重新确认你的答案。' },
      prompt: { ...original.prompt, prompt_id: state === 'other_prompt' ? 'other' : original.prompt.prompt_id } }
    vi.mocked(localApi.planReviewQuestion).mockReset().mockResolvedValue(heardReview)
    const read = vi.mocked(localApi.diagnosticPrompt).mockReset().mockResolvedValueOnce(original)
    if (state === 'read_failed') read.mockRejectedValue(new Error('offline'))
    else read.mockResolvedValue(refreshed)
    vi.spyOn(listening, 'playPetListening').mockResolvedValue({ status: 'finished', response: '原音已播完。' })
    await mount()
    expect(read).toHaveBeenCalledTimes(2)
    if (state === 'retained') expect(spoken()).toEqual([refreshed.deferred_answer.message])
    else expect(spoken()).not.toContain(refreshed.deferred_answer.message)
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
  })

  it('submits the exact review ID through the event bridge and completes only after the answer receipt', async () => {
    await present()
    const saved = deferred<DiagnosticAttempt>()
    vi.mocked(localApi.answerDiagnostic).mockReturnValueOnce(saved.promise)
    let pending!: Promise<boolean>
    await act(async () => { pending = submitEvent('不会') })
    expect(localApi.answerDiagnostic).toHaveBeenCalledExactlyOnceWith('session-review-a', 'question-exact-a', '不会', 'review-exact-a')
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
    await act(async () => { saved.resolve(attempt); expect(await pending).toBe(true) })
    expect(localApi.completePlanItem).toHaveBeenCalledExactlyOnceWith('plan-review-a', 1, expect.objectContaining({ type: 'child_checked', checklist: ['review_completed', 'self_tested'] }))
    expect(current.plan_items[0].status).toBe('completed')
    expect(spoken().filter((text) => text.includes(attempt.feedback))).toHaveLength(1)
    expect(screen.queryByText(review.prompt)).toBeNull()
    expect(localApi.nextDiagnostic).not.toHaveBeenCalled()
  })

  it('does not turn an early spoken answer into another audio playback or task completion', async () => {
    const heardReview = { ...review, track: 'pet', answer_type: 'listening_text' } as BoundReview
    const prompt = { ...pending, prompt: { ...pending.prompt!, item: heardReview } }
    const note = { version: 'listening_review_deferred_v1', speech_retained: true, formal_answer_saved: false,
      automatic_submission_allowed: false, new_confirmation_required: true, delivery_complete: false,
      local_only: true, cloud_transmission_allowed: false, source_turn_id: 'early-turn', source_voice_input_id: 'early-voice',
      message: '刚才的话还留着，听完后再确认。' }
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(prompt)
    vi.mocked(localApi.planReviewQuestion).mockReset().mockResolvedValue(heardReview)
    const play = vi.spyOn(listening, 'playPetListening').mockResolvedValue({ status: 'paused', response: '原音已暂停。' })
    await mount()
    expect(play).toHaveBeenCalledOnce()
    const turn = { agent_turn_id: 'early-turn', response_text: '收到', action: { type: 'submit_diagnostic_answer', answer: 'seven',
      diagnostic_prompt_id: prompt.prompt!.prompt_id, diagnostic_item_version: prompt.prompt!.item_version } } as AgentTurn
    vi.mocked(localApi.agentTurn).mockResolvedValue(turn)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    vi.mocked(localApi.diagnosticPrompt).mockResolvedValue({ ...prompt, deferred_answer: note })
    vi.mocked(localApi.answerDiagnosticPrompt).mockRejectedValue(new ApiError('generic conflict', 409,
      'DIAGNOSTIC_PROMPT_LISTENING_REVIEW_DELIVERY_REQUIRED'))
    await act(async () => { await microphone.onTranscript('seven') })
    expect(play).toHaveBeenCalledOnce()
    expect(spoken().filter(text => text === note.message)).toHaveLength(1)
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
    expect(localApi.answerDiagnosticPrompt).toHaveBeenCalledOnce()
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
  })

  it('does not replace actual review feedback with the generic voice-command acknowledgement', async () => {
    await present()
    const turn = { agent_turn_id: 'turn-review-answer', response_text: '收到，我会保存这次作答并告诉你反馈。', action: { type: 'submit_diagnostic_answer', answer: '不会', diagnostic_prompt_id: 'prompt-review-a', diagnostic_item_version: 'version-a' } } as AgentTurn
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    await act(async () => { await microphone.onTranscript('不会') })
    expect(localApi.answerDiagnosticPrompt).toHaveBeenCalledExactlyOnceWith('session-review-a', { prompt_id: 'prompt-review-a', item_version: 'version-a', answer: '不会', client_action_id: 'diagnostic-answer:prompt-review-a', authorized_turn_id: 'turn-review-answer' })
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
    expect(spoken().filter((text) => text.includes(attempt.feedback))).toHaveLength(1)
    expect(spoken()).not.toContain(turn.response_text)
  })

  it('does not load or submit a paused due review', async () => {
    current = workspace('paused')
    await mount()
    await act(async () => { expect(await submitEvent()).toBe(false) })
    expect(localApi.planReviewQuestion).not.toHaveBeenCalled()
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
  })

  it('invalidates an answer binding lookup when the active task is paused', async () => {
    await present()
    const binding = deferred<BoundReview>()
    vi.mocked(localApi.planReviewQuestion).mockReturnValueOnce(binding.promise)
    let pending!: Promise<boolean>
    await act(async () => { pending = submitEvent() })
    await pause()
    await act(async () => { binding.resolve(review); expect(await pending).toBe(false) })
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
    await act(async () => { expect(await submitEvent()).toBe(false) })
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
  })

  it.each(['privacy', 'unmount'] as const)('does not speak or submit a question arriving after %s', async (stop) => {
    const view = await mount()
    if (stop === 'privacy') await privacyStop()
    else view.unmount()
    const count = spoken().length
    await act(async () => { question.resolve(review) })
    expect(spoken()).toHaveLength(count)
    expect(localApi.answerDiagnostic).not.toHaveBeenCalled()
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
  })

  it('does not complete or speak a late answer receipt after privacy stop', async () => {
    await present()
    const saved = deferred<DiagnosticAttempt>()
    vi.mocked(localApi.answerDiagnostic).mockReturnValueOnce(saved.promise)
    let pending!: Promise<boolean>
    await act(async () => { pending = submitEvent() })
    await privacyStop()
    const count = spoken().length
    await act(async () => { saved.resolve(attempt); expect(await pending).toBe(false) })
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
    expect(spoken()).toHaveLength(count)
  })

  it('does not speak a late completion receipt after unmount', async () => {
    const view = await present()
    const completed = deferred<void>()
    vi.mocked(localApi.completePlanItem).mockReturnValueOnce(completed.promise)
    let pending!: Promise<boolean>
    await act(async () => { pending = submitEvent() })
    expect(localApi.completePlanItem).toHaveBeenCalledTimes(1)
    view.unmount()
    const count = spoken().length
    await act(async () => { completed.resolve(); await pending })
    expect(spoken()).toHaveLength(count)
  })
})
