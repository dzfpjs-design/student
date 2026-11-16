import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop, ownedVoiceRuntimeConfig } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect, type ComponentProps } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as inputs from './LocalInputs'
import type { SessionWorkspaceState, TutorTurn } from './types'
import { readTutorCompletions } from './tutorCompletionJournal'
import { TutorPlaybackController } from './tutorPlaybackController'
import * as ocrEvidence from './tutorOcrEvidence'
import type { DocumentCameraCapture } from './DocumentCameraCapture'
import { emitCaptureReceipt } from './deviceCommandReceipt'
import * as voiceCapture from './voiceCaptureContext'

let camera: ComponentProps<typeof DocumentCameraCapture>
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: (props: typeof camera) => {
  camera = props
  useEffect(() => {
    const presence = (active: boolean) => window.dispatchEvent(new CustomEvent('hsc-document-capture-presence', { detail: { sessionId: props.sessionId, active } }))
    presence(true)
    return () => { presence(false) }
  }, [props.sessionId])
  return null
} }))

let microphone: ComponentProps<typeof inputs.HandsFreeVoiceInput>
vi.mock('./LocalInputs', async original => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null }, VoiceInputButton: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))
let visualUnavailable: (() => void) | undefined
vi.mock('./TutorVisual', () => ({ TutorVisual: (p: { binding: { turn_id: string }; onUnavailable?: () => void }) => {
  visualUnavailable = p.onUnavailable
  return <span>saved-figure:{p.binding.turn_id}</span>
} }))
const previous = (): TutorTurn => ({ turn_id: 'previous-tutor', session_id: 'synthetic-session', child_id: 'synthetic-child', subject: '数学', question: '为什么两个负数相乘是正数', answer: '先从数轴想一想。', hint_level: 1, created_at: new Date().toISOString() }) as TutorTurn
const workspace = (tutor = previous()): SessionWorkspaceState => ({ session: { session_id: 'synthetic-session', child_id: 'synthetic-child', status: 'active', state: 'check_in', observation_status: 'paused' },
  plan_items: [], recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [], drafts: {}, planning_context: {}, family_rule: {}, latest_tutor_turn: tutor,
  sensor_release: { active_leases: 0, by_device: {} } }) as unknown as SessionWorkspaceState
beforeEach(() => {
  vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getWakeWordSettings: vi.fn(async () => ({ childId: 'synthetic-child', wakeWord: '小知', enabled: true, inactivitySeconds: 60 })),
    getVoiceOnboarding: vi.fn(async () => ({ childId: 'synthetic-child', version: 1, step: 'completed', updatedAt: new Date().toISOString() })), saveVoiceOnboarding: vi.fn(), saveWakeWordSettings: vi.fn() }))
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
    vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
      handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
  }
  vi.spyOn(inputs, 'speakLocally').mockImplementation((_text, _session, done) => done?.('text_only'))
  vi.spyOn(localApi, 'agentTurn')
  vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async id => ({ agent_turn_id: id, tool_status: 'completed' }) as never)
  vi.spyOn(localApi, 'askTutor').mockResolvedValue({ ...previous(), turn_id: 'next-tutor', hint_level: 2, answer: '看一下乘法规律。' })
  vi.spyOn(localApi, 'saveTutorFeedback').mockResolvedValue({ feedback_id: 'feedback-saved' } as never)
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(workspace())
  vi.spyOn(localApi, 'saveSessionDraft').mockImplementation(async (_session, section, payload) => ({ section, payload, updated_at: new Date().toISOString() }))
  vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
  vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
})
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
it.each(['current', 'new_offer', 'another_case', 'paused', 'missing', 'joint_current', 'joint_replaced', 'joint_changed'])('reads back only the %s original question repair', async status => {
  const planned = { agent_turn_id: 'repair-turn', transcript: '我重新念一下', response_text: '先核对。',
    action: { type: 'repair_mistake_question', local_only: true }, runtime_capability: 'mistake.repair_question', runtime_execution_location: 'server' }
  const joint = status.startsWith('joint_')
  const message = joint ? '题干为四十五加十三，原作答更正为五十六。这两项都对吗？' : '更正题干为四十五加十三再乘四，原答案没变，对吗？'
  const pending = { mode: 'source_repair', review_id: 'offer-1', version: 'version-1', question: '45+13', original_answer: '原答案',
    ...(joint ? { replacement_answer: '五十六' } : {}) }
  const focus = { session_id: 'synthetic-session', child_id: 'synthetic-child', mistake_id: 'same-case', phase: 'remediation',
    state: { question_confirmation: pending } }
  vi.mocked(localApi.agentTurn).mockResolvedValue(planned as never)
  vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn: { ...planned, response_text: message, tool_status: 'completed' },
    execution: { capability: 'mistake.repair_question', response_text: message, result: focus, requires_child_reply: true } } as never)
  vi.spyOn(localApi, 'mistakeDialogue').mockResolvedValue({ dialogue: status === 'missing' ? null : {
    ...focus, phase: status === 'paused' ? 'paused' : focus.phase,
    mistake_id: status === 'another_case' ? 'other-case' : focus.mistake_id,
    state: { question_confirmation: { ...pending, review_id: status === 'new_offer' || status === 'joint_replaced' ? 'offer-2' : pending.review_id,
      ...(status === 'joint_changed' ? { replacement_answer: '五十七' } : {}) } } } } as never)
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('我重新念一下') })
  expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith('repair-turn')
  expect(vi.mocked(inputs.speakLocally).mock.calls.some(([text]) => text === message)).toBe(status === 'current' || status === 'joint_current')
  expect(localApi.askTutor).not.toHaveBeenCalled()
})

it.each(['current', 'closed', 'replaced', 'held', 'missing'])('delivers saved mistake teaching only to its %s focus', async status => {
  const planned = { agent_turn_id: 'mistake-teach', transcript: '答案是三', response_text: '核对中。',
    action: { type: 'continue_mistake_dialogue' }, runtime_capability: 'mistake.dialogue', runtime_execution_location: 'server' }
  const taught = { ...previous(), turn_id: 'saved-mistake-teaching', answer: '我们先看这一小步。' }
  vi.mocked(localApi.agentTurn).mockResolvedValue(planned as never)
  vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn: { ...planned, response_text: taught.answer, tool_status: 'completed' },
    execution: { capability: 'mistake.dialogue', response_text: taught.answer, tutor_turn: taught,
      result: { mistake_id: 'case-current' } } } as never)
  vi.spyOn(localApi, 'mistakeDialogue').mockResolvedValue({ dialogue: status === 'missing' ? null : {
    session_id: taught.session_id, child_id: taught.child_id,
    mistake_id: status === 'replaced' ? 'case-other' : 'case-current',
    phase: status === 'closed' ? 'closed' : 'remediation', state: {
      teaching_turn_id: taught.turn_id, ...(status === 'held' ? { foundation_hold: { reason: 'time_budget' } } : {}),
    } } } as never)
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('答案是三') })
  expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith('mistake-teach')
  expect(vi.mocked(inputs.speakLocally).mock.calls.some(([text]) => text === taught.answer)).toBe(status === 'current')
  expect(localApi.askTutor).not.toHaveBeenCalled()
})

it.each(['awaiting_time', 'time_proposal'])('recovers the original %s time question by voice without executing or capturing', async phase => {
  camera = undefined as never
  const message = phase === 'awaiting_time' ? '讲解已经保存。你希望什么时候提醒复习这道题？'
    : '你希望在明天下午三点再看这道题，对吗？'
  vi.spyOn(localApi, 'continuationStatus').mockResolvedValue({ status: 'blocked', parent_turn_id: 'original', document_goal: {
    phase, root_turn_id: 'original', revision: 4, child_id: 'synthetic-child', session_id: 'synthetic-session', message } })
  const execute = vi.spyOn(localApi, 'executeAgentTurn')
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => {
    microphone.onRuntimeStatusChange?.({ phase: 'listening', speechActive: false, pendingUtterances: 0 } as never)
    microphone.onWake?.()
  })
  await waitFor(() => expect(vi.mocked(inputs.speakLocally).mock.calls.filter(([text]) => text === message)).toHaveLength(1), { timeout: 4000 })
  expect(execute).not.toHaveBeenCalled()
  expect(localApi.agentTurn).not.toHaveBeenCalled()
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(camera).toBeUndefined()
})

it('uses the saved server time receipt for natural voice scheduling, not the pre-execution reply', async () => {
  const response = '已保存：明天下午三点再看这道题。'
  const planned = { agent_turn_id: 'review-time-turn', transcript: '明天下午三点再看', response_text: '我先核对。',
    action: { type: 'control_document_review', capability: 'learning.review_schedule', local_only: true },
    runtime_capability: 'learning.review_schedule', runtime_execution_location: 'server' }
  vi.mocked(localApi.agentTurn).mockResolvedValue(planned as never)
  const execute = vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({
    turn: { ...planned, response_text: response, tool_status: 'completed' },
    execution: { capability: 'learning.review_schedule', response_text: response, evidence: ['review_schedule_receipt'],
      result: { status: 'scheduled', root_turn_id: 'original', revision: 5 } },
  } as never)
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('明天下午三点再看') })
  expect(execute).toHaveBeenCalledExactlyOnceWith('review-time-turn')
  expect(vi.mocked(inputs.speakLocally).mock.calls.some(([text]) => text === response)).toBe(true)
  expect(vi.mocked(inputs.speakLocally).mock.calls.some(([text]) => text === planned.response_text)).toBe(false)
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
})

it('executes a document tutor through its saved OCR identity, not editable question text', async () => {
  const confirmedTutor = vi.spyOn(ocrEvidence, 'askTutorWithOcrEvidence').mockResolvedValue({ ...previous(), turn_id: 'bound-tutor' })
  vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'bound-teaching', transcript: '', response_text: '接着看原题。',
    action: { type: 'ask_tutor', subject: '数学', question: '原确认题目', hint_level: 1, ocr_confirmation_id: 'original-confirmation',
      tutor_request_action_id: 'document-goal:original:tutor', document_goal_root_turn_id: 'original' },
    runtime_capability: 'learning.explain', runtime_execution_location: 'local_model' } as never)
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('继续看刚才那道题') })
  expect(confirmedTutor).toHaveBeenCalledWith('synthetic-session', '数学', 'original-confirmation', 'document-goal:original:tutor', 1)
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(localApi.saveSessionDraft).toHaveBeenCalledWith('synthetic-session', 'agent_command', expect.objectContaining({
    tutor_recovery: expect.objectContaining({ ocrConfirmationId: 'original-confirmation', actionId: 'document-goal:original:tutor' }) }))
  expect(localApi.completeAgentTurn).toHaveBeenCalledWith('bound-teaching', previous().answer, 'completed', undefined,
    expect.objectContaining({ execution_receipt: expect.objectContaining({ output: expect.objectContaining({ tutor_turn_id: 'bound-tutor' }) }) }))
})

it.each(['memory_choice', 'memory_stale'])('speaks original-question %s from the server without teaching or asking for clicks', async (status) => {
  const text = status === 'memory_choice' ? '第1题：十二加八。第2题：五减三。你想继续第几题？'
    : '刚才回顾的原题或记忆已经失效，需要重新核对。'
  const planned = { agent_turn_id: 'memory-choices', transcript: '继续刚才的题', response_text: '核对中。',
    action: { type: 'control_document_revisit', local_only: true, cloud_transmission_allowed: false },
    runtime_capability: 'learning.review_revisit', runtime_execution_location: 'server' }
  vi.mocked(localApi.agentTurn).mockResolvedValue(planned as never)
  vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn: { ...planned, tool_status: 'completed', response_text: text },
    execution: { capability: 'learning.review_revisit', response_text: text,
      result: { status, local_only: true, cloud_transmission_allowed: false, mastery_update_allowed: false } } } as never)
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('继续刚才的题') })
  expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith('memory-choices')
  expect(vi.mocked(inputs.speakLocally).mock.calls.some(([value]) => value === text)).toBe(true)
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
})

it.each(['location','undo','whole_review'])('speaks a saved %s without grading the short control or opening another question', async mode => {
  const text = '第一段已采用你刚才的更正，原话保留。这次只确认位置，没有重新评分。'
  const planned = { agent_turn_id: 'spoken-location', transcript: '第一段', response_text: '核对位置。',
    action: { type: 'control_document_revisit', local_only: true, cloud_transmission_allowed: false },
    runtime_capability: 'learning.review_revisit', runtime_execution_location: 'server' }
  vi.mocked(localApi.agentTurn).mockResolvedValue(planned as never)
  vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn: { ...planned, tool_status: 'completed', response_text: text },
    execution: { capability: 'learning.review_revisit', response_text: text, result: { status: 'check_feedback',
      [mode === 'location' ? 'spoken_revision_event' : mode === 'undo' ? 'spoken_undo_event' : 'whole_review']: { turn_id: 'spoken-location', status: 'resolved' },
      local_only: true, cloud_transmission_allowed: false, mastery_update_allowed: false } } } as never)
  const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt')
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('第一段') })
  expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith('spoken-location')
  expect(vi.mocked(inputs.speakLocally).mock.calls.filter(([value]) => value === text)).toHaveLength(1)
  expect(prepare).not.toHaveBeenCalled()
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
})

it.each(['new', 'new_open_written', 'new_open_spoken', 'new_retry_spoken', 'new_supplement_spoken', 'replayed', 'other_child'])('handles a %s question using the server-bound voice target', async mode => {
  const opened = mode.startsWith('new_open') || mode === 'new_retry_spoken' || mode === 'new_supplement_spoken'
  const fresh = mode.startsWith('new')
  const text = opened ? '解释先求总量再平均分的理由，并用题目条件检查。' : '计算24加6乘3，请独立回答。'
  const planned = { agent_turn_id: 'independent-check', transcript: '换一道新题考我', response_text: '核对题源。',
    action: { type: 'control_document_revisit', local_only: true, cloud_transmission_allowed: false },
    runtime_capability: 'learning.review_revisit', runtime_execution_location: 'server' }
  const diagnostic = { status: mode === 'new_supplement_spoken' ? 'answered' : 'waiting_answer', prompt: { prompt_id: 'different-qualified-question', item_version: 'frozen-version',
    origin_session_id: 'synthetic-session', child_id: mode === 'other_child' ? 'other-child' : 'synthetic-child',
    item: { prompt: text, subject: '数学', track: 'school', answer_type: opened ? 'open_response' : 'number',
      assessment_mode: opened ? 'open_response_rubric' : 'objective',
      ...(opened ? { response_mode: mode.endsWith('spoken') ? 'spoken_explanation' : 'written' } : {}) } } }
  if (mode === 'new_open_spoken') Object.assign(diagnostic, { teaching_context: {
    version: 'document_independent_teaching_context_v1', status: 'linked_reference',
    learning_effect: 'not_established', delivery_verified: false, mastery_update_allowed: false,
    local_only: true, cloud_transmission_allowed: false,
    source_trace: { response_prompt_id: 'old-assisted-question', followup_question: '怎样核对总量？' },
  } })
  vi.mocked(localApi.agentTurn).mockResolvedValue(planned as never)
  vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn: { ...planned, tool_status: 'completed', response_text: text },
    execution: { capability: 'learning.review_revisit', response_text: text,
      result: { status: 'checking_new_question', diagnostic_prompt: diagnostic,
        ...(mode === 'new_retry_spoken' ? { practice_kind: 'spoken_retry' } : {}),
        ...(mode === 'new_supplement_spoken' ? { practice_kind: 'spoken_supplement' } : {}),
        ...(mode === 'replayed' ? { replayed: true, recorded_receipt: true } : {}) } } } as never)
  const capture = vi.spyOn(voiceCapture, 'setVoiceQuestionCapture')
  const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt')
  const events = vi.spyOn(window, 'dispatchEvent')
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript(mode === 'new_retry_spoken' ? '同一道题让我再讲一次' : '换一道新题考我') })
  expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith('independent-check')
  const bindings = capture.mock.calls.filter(([, , question]) => question?.kind === 'diagnostic'
    && question.binding.prompt_id === 'different-qualified-question')
  const prompts = events.mock.calls.filter(([event]) => event.type === 'hsc-diagnostic-prompt-updated')
  expect(bindings).toHaveLength(fresh ? 1 : 0)
  expect(prompts).toHaveLength(fresh ? 1 : 0)
  expect(vi.mocked(inputs.speakLocally).mock.calls.some(([value]) => value === text)).toBe(fresh)
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(prepare).not.toHaveBeenCalled()
})

it('presents the server original-question feedback and board without generating another Tutor', async () => {
  const planned = { agent_turn_id: 'revisit-answer', transcript: '十二加八是二十', response_text: '我先核对。',
    action: { type: 'control_document_revisit', capability: 'learning.review_revisit', local_only: true },
    runtime_capability: 'learning.review_revisit', runtime_execution_location: 'server' }
  const taught = { ...previous(), turn_id: 'saved-original-feedback', question: '十二支红笔和八支蓝笔一共多少支？',
    answer: '说说十二和八分别代表什么。', output_envelope: { verification_status: 'unverified', result: {
      content: '说说十二和八分别代表什么。', blackboard: { version: 'tutor_blackboard_v1', source: 'model_structured',
        title: '两类数量合并', subject: '数学', sync_mode: 'segment_estimate', segments: [{ segment_id: 'one',
          narration: '说说十二和八分别代表什么。', blocks: [{ block_id: 'q1', kind: 'question', text: '十二和八分别是什么？', tone: 'chalk' }] }] } } } }
  vi.mocked(localApi.agentTurn).mockResolvedValue(planned as never)
  vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({turn: { ...planned, response_text: taught.answer, tool_status: 'completed' },
    execution: { capability: 'learning.review_revisit', response_text: taught.answer,
      result: { status: 'feedback_ready', local_only: true, mastery_update_allowed: false }, tutor_turn: taught } } as never)
  vi.spyOn(localApi, 'tutorPlayback').mockResolvedValue({ playback: null })
  const playback = { playback_id: 'saved-review-playback', child_id: 'synthetic-child', session_id: 'synthetic-session',
    turn_id: taught.turn_id, source_version: 'synthetic-proof', generation: 1, revision: 1,
    status: 'paused', segment_index: 0, segment_started: false, presentation: taught.output_envelope.result.blackboard }
  const mutate = vi.spyOn(localApi, 'mutateTutorPlayback').mockImplementation(async (_session, operation, values) => ({
    playback: { ...playback, revision: ++playback.revision,
      status: operation === 'control' && values.command === 'resume' ? 'playing' : 'paused' },
  }) as never)
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('十二加八是二十') })
  expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith('revisit-answer')
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(vi.mocked(inputs.speakLocally).mock.calls.some(([text]) => text === planned.response_text)).toBe(false)
  expect(vi.mocked(inputs.speakLocally).mock.calls.some(([text]) => text === taught.answer)).toBe(true)
  expect(mutate).toHaveBeenCalledWith('synthetic-session', 'begin', expect.objectContaining({ turn_id: taught.turn_id,
    presentation: expect.objectContaining({ title: '两类数量合并' }) }))
  expect(await screen.findByText('十二和八分别是什么？')).toBeTruthy()
})

it('restores an unknown OCR Tutor request with the original identity and explicit voice retry', async () => {
  const confirmedTutor = vi.spyOn(ocrEvidence, 'askTutorWithOcrEvidence')
    .mockRejectedValueOnce(new ApiError('结果尚未确认', 409, 'TUTOR_RESULT_UNCONFIRMED'))
    .mockResolvedValue({ ...previous(), turn_id: 'recovered-ocr-tutor' })
  vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'owned-teaching', transcript: '', response_text: '接着看原题。',
    action: { type: 'ask_tutor', subject: '数学', question: '原确认题目', hint_level: 1, ocr_confirmation_id: 'original-confirmation',
      tutor_request_action_id: 'document-goal:original:tutor', document_goal_root_turn_id: 'original' },
    runtime_capability: 'learning.explain', runtime_execution_location: 'local_model' } as never)
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('继续刚才那道题') })
  expect(confirmedTutor).toHaveBeenCalledTimes(1)
  const saved = vi.mocked(localApi.saveSessionDraft).mock.calls.map(call => call[2] as { tutor_recovery?: unknown })
    .filter(payload => payload.tutor_recovery).at(-1)!
  expect(saved.tutor_recovery).toMatchObject({ ocrConfirmationId: 'original-confirmation', actionId: 'document-goal:original:tutor', requiresExplicitRetryConfirmation: true })
  cleanup()
  const initial = workspace()
  initial.drafts = { agent_command: { updated_at: new Date().toISOString(), payload: saved } } as never
  await act(async () => { render(<SessionWorkspace initial={initial} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  expect(confirmedTutor).toHaveBeenCalledTimes(1)
  await act(async () => { await microphone.onTranscript('确认再次请求') })
  expect(confirmedTutor).toHaveBeenLastCalledWith('synthetic-session', '数学', 'original-confirmation', 'document-goal:original:tutor', 1, undefined, undefined, true)
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
})

it('does not send a late or invalid document confirmation through the legacy Tutor path', async () => {
  const confirmation = vi.spyOn(ocrEvidence, 'confirmOcrTutorEvidence')
  const tutor = vi.spyOn(ocrEvidence, 'askTutorWithOcrEvidence')
  vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'late-confirmation', transcript: '题干是对的',
    response_text: '核对原题。', action: { type: 'confirm_ocr_text', document_goal_confirmation_blocked: true },
    runtime_capability: 'document.confirm_ocr', runtime_execution_location: 'client_device_bridge' } as never)
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('题干是对的') })
  expect(confirmation).not.toHaveBeenCalled()
  expect(tutor).not.toHaveBeenCalled()
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(screen.getByText(/原拍题目标已有后续记录或来源发生变化/)).toBeInTheDocument()
})

it.each(['privacy', 'unmount', 'speaking'])('does not publish a late original review after %s', async interruption => {
  camera = undefined as never
  let finish!: (value: ocrEvidence.OcrTutorReview) => void
  const read = vi.spyOn(ocrEvidence, 'readOcrDocumentGoal').mockImplementation(() => new Promise(resolve => { finish = resolve }))
  vi.spyOn(localApi, 'continuationStatus').mockResolvedValue({ status: 'blocked', parent_turn_id: 'original', document_goal: {
    phase: 'awaiting_confirmation', root_turn_id: 'original', revision: 1, artifact_id: 'saved-page',
    child_id: 'synthetic-child', session_id: 'synthetic-session' } })
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => {
    microphone.onRuntimeStatusChange?.({ phase: 'listening', speechActive: false, pendingUtterances: 0 } as never)
    microphone.onWake?.()
  })
  await waitFor(() => expect(read).toHaveBeenCalledOnce(), { timeout: 4000 })
  const readSignal = read.mock.calls[0][3]
  expect(readSignal).toBeInstanceOf(AbortSignal)
  await act(async () => {
    if (interruption === 'unmount') cleanup()
    else if (interruption === 'privacy') window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))
    else microphone.onRuntimeStatusChange?.({ phase: 'listening', speechActive: true, pendingUtterances: 0 } as never)
    finish({ artifactId: 'saved-page', text: '待恢复原题', status: 'needs_confirmation', createdAt: new Date().toISOString() })
  })
  if (interruption !== 'speaking') expect(readSignal?.aborted).toBe(true)
  expect(screen.queryByText('待核对的原题')).not.toBeInTheDocument()
  expect(camera).toBeUndefined()
  expect(vi.mocked(inputs.speakLocally).mock.calls.some(([text]) => text.startsWith('上次拍下的原题还在'))).toBe(false)
})

it.each([{ phase: 'check_in', restart: false, mode: 'capture' }, { phase: 'study', restart: false, mode: 'capture' },
  { phase: 'check_in', restart: true, mode: 'capture' }, { phase: 'study', restart: true, mode: 'capture' },
  ...['inspect', 'review'].flatMap(mode => [false, true].map(restart => ({ phase: 'study', restart, mode })))
])('continues the original $mode after voice confirmation in $phase (restart=$restart) without a child click or duplicate Tutor', async ({ phase, restart, mode }) => {
  camera = undefined as never
  Object.assign(window.desktop!, { getRuntimeConfig: vi.fn(async () => ownedVoiceRuntimeConfig()) })
  const initial = workspace()
  initial.session.state = phase as SessionWorkspaceState['session']['state']
  if (phase === 'study') initial.plan_items = [{ plan_item_id: 'current-task', status: 'active', subject: '数学', title: '原作业',
    source_type: 'school_homework', task_type: 'written', completion_checklist: [], expected_minutes: 20,
    active_seconds: 0, active_segment_started_at: null, completion_standard: '完成并检查' }] as never
  vi.mocked(localApi.sessionWorkspace).mockResolvedValue(initial)
  vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({ playback: null })
  vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({ game: null })
  const root = 'original-capture'
  const teaching = { agent_turn_id: 'original-teaching', transcript: '', response_text: '接着看原题。',
    runtime_capability: 'learning.explain', runtime_execution_location: 'local_model',
    action: { type: 'ask_tutor', question: '12+8=?', subject: '数学', hint_level: 1, ocr_confirmation_id: 'confirmed-original',
      tutor_request_action_id: `document-goal:${root}:tutor`, document_goal_root_turn_id: root } }
  vi.mocked(localApi.agentTurn).mockResolvedValueOnce({ agent_turn_id: root, transcript: '看这题，讲完再提醒复习', response_text: '先看原题。',
    action: mode === 'capture' ? { type: 'request_capture' } : { type: 'control_document_monitor', command: mode, defer_document_review: true },
    runtime_capability: mode === 'capture' ? 'learning.capture_question' : 'document.monitor_control', runtime_execution_location: 'client_device_bridge', after_execution: 'replan' } as never)
    .mockResolvedValueOnce({ agent_turn_id: 'confirm-voice', transcript: '题干是对的', response_text: '已经核对原题。',
      action: { type: 'confirm_ocr_text', document_goal_confirmation_root: root, document_goal_confirmation_revision: 1 },
      runtime_capability: 'document.confirm_ocr', runtime_execution_location: 'client_device_bridge' } as never)
  let prepared = false
  let remounted = false
  vi.spyOn(localApi, 'continuationStatus').mockImplementation(async () => ({ status: prepared ? 'ready' : remounted ? 'blocked' : 'none', parent_turn_id: prepared || remounted ? root : undefined,
    ...(remounted && !prepared ? { document_goal: { phase: 'awaiting_confirmation', root_turn_id: root, revision: 1,
      artifact_id: 'original-page', child_id: initial.session.child_id, session_id: initial.session.session_id } } : {}) }))
  vi.spyOn(localApi, 'continueAgentTurn').mockResolvedValue({ status: 'ready', turn: teaching } as never)
  vi.spyOn(localApi, 'admitContinuationExecution').mockResolvedValue({ status: 'admitted' })
  const tutor = vi.spyOn(ocrEvidence, 'askTutorWithOcrEvidence').mockResolvedValue({ ...previous(), turn_id: 'saved-original-tutor' })
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith(`/document-goals/${root}/review`)) return { ok: true, json: async () => ({
      root_turn_id: root, session_id: initial.session.session_id, child_id: initial.session.child_id, revision: 1,
      phase: 'awaiting_confirmation', local_only: true, cloud_transmission_allowed: false, mastery_update_allowed: false,
      review: { artifactId: 'original-page', text: '12+8=?', status: 'needs_confirmation', createdAt: new Date().toISOString() } }) }
    if (url.endsWith('/tutor-ocr-confirmations')) return { ok: true, json: async () => ({ confirmation_id: 'confirmed-original', confirmed_text: '12+8=?',
      confirmation_kind: 'ocr_verified', confirmed_at: new Date().toISOString(), expires_at: new Date(Date.now() + 3600000).toISOString() }) }
    if (url.endsWith(`/document-goals/${root}/confirm`)) {
      prepared = true
      return { ok: true, json: async () => ({ status: 'ready', document_goal: { root_turn_id: root, session_id: initial.session.session_id,
        artifact_id: 'original-page', confirmation_id: 'confirmed-original' } }) }
    }
    throw new Error('Unexpected transport: ' + url)
  })
  vi.stubGlobal('fetch', fetcher)
  await act(async () => { render(<SessionWorkspace initial={initial} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  if (mode !== 'capture') window.addEventListener('hsc-document-monitor-voice-action', event => {
    event.preventDefault()
    const detail = (event as CustomEvent).detail
    expect(detail.deferReview).toBe(true)
    detail.report({ artifact_id: 'original-page', revision_id: 'original-version', page_status: 'text_available', response_text: '原页已经记录。' })
    detail.resolve(true)
  }, { once: true })
  await act(async () => { microphone.onRuntimeStatusChange?.({ phase: 'listening', speechActive: false, pendingUtterances: 0 } as never)
    await microphone.onTranscript('看这题，讲完再提醒复习') })
  if (mode === 'capture') {
    await waitFor(() => expect(camera?.commandId).toBe(root))
    await act(async () => {
      camera.onArtifact?.({ artifact_id: 'original-page', extracted_text: '12+8=?', confidence: .99, status: 'completed', local_path: null,
        details: { lines: [], engine: 'controlled', ocr_confidence: .99, quality: { passed: true, issues: [], messages: [], metrics: {}, version: 'test' } } })
      emitCaptureReceipt({ sessionId: initial.session.session_id, commandId: root, status: 'recognized', artifactId: 'original-page', ocrStatus: 'completed' })
    })
  }
  await waitFor(() => expect(localApi.completeAgentTurn).toHaveBeenCalledWith(root, expect.any(String), 'completed', undefined, expect.anything()))
  if (mode !== 'capture') {
    expect(tutor).not.toHaveBeenCalled()
    expect(localApi.continueAgentTurn).not.toHaveBeenCalled()
    remounted = true
  }
  if (restart) {
    cleanup()
    camera = undefined as never
    remounted = true
    await act(async () => { render(<SessionWorkspace initial={initial} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
    await act(async () => {
      microphone.onRuntimeStatusChange?.({ phase: 'listening', speechActive: false, pendingUtterances: 0 } as never)
      microphone.onWake?.()
    })
    await waitFor(() => expect(screen.getByText('待核对的原题')).toBeInTheDocument(), { timeout: 5000 })
    expect(camera).toBeUndefined()
    expect(tutor).not.toHaveBeenCalled()
  }
  if (mode !== 'capture' && !restart) {
    await act(async () => { microphone.onWake?.() })
    await waitFor(() => expect(screen.getByText('待核对的原题')).toBeInTheDocument(), { timeout: 5000 })
    expect(camera).toBeUndefined()
  }
  await act(async () => { await microphone.onTranscript('题干是对的') })
  await waitFor(() => expect(tutor).toHaveBeenCalledTimes(1), { timeout: 5000 })
  expect(tutor).toHaveBeenCalledWith(initial.session.session_id, '数学', 'confirmed-original', `document-goal:${root}:tutor`, 1)
  expect(localApi.admitContinuationExecution).toHaveBeenCalledWith(initial.session.session_id, 'original-teaching')
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(fetcher.mock.calls.filter(call => call[0].endsWith(`/document-goals/${root}/confirm`))).toHaveLength(1)
  await waitFor(() => expect(localApi.completeAgentTurn).toHaveBeenCalledWith('original-teaching', previous().answer, 'completed', undefined, expect.anything()))
})
it('restores the saved visual-tutor figure from its playback turn, not the latest tutor and without speaking', async () => {
  const interrupt = vi.spyOn(TutorPlaybackController.prototype, 'interrupt').mockImplementation(() => {})
  vi.spyOn(localApi, 'tutorPlayback').mockResolvedValue({ playback: {
    child_id: 'synthetic-child', session_id: 'synthetic-session', turn_id: 'original-visual-turn',
    playback_id: 'saved-playback', status: 'paused', segment_index: 0, segment_started: false,
    presentation: { version: 'tutor_blackboard_v1', source: 'visual_teaching_v1', subject: '物理', title: '原图讲解',
      sync_mode: 'segment_estimate', segments: [{ segment_id: 'first', narration: '观察原图。', blocks: [] }] },
  } } as never)
  render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />)
  await screen.findByText('saved-figure:original-visual-turn')
  expect(screen.queryByText('saved-figure:previous-tutor')).toBeNull()
  expect(inputs.speakLocally).not.toHaveBeenCalled()
  act(() => visualUnavailable?.())
  expect(interrupt).toHaveBeenCalledOnce()
  await screen.findByText('原题图形暂时无法核对，讲解已暂停；核对材料后可以继续。')
})
let activeSpeech: void | Promise<void>
async function speak(action: string, tutor = previous(), waitForCompletion = true, phase = 'check_in') {
  vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'synthetic-voice-turn', transcript: '再帮我解释一点', response_text: '我们接着看。', action: { type: action, outcome: 'still_stuck' }, runtime_execution_location: 'client_device_bridge', runtime_capability: action === 'more_tutor_hint' ? 'tutor.more_hint' : 'tutor.record_feedback' } as never)
  const initial = workspace(tutor)
  initial.session.state = phase as SessionWorkspaceState['session']['state']
  await act(async () => { render(<SessionWorkspace initial={initial} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { activeSpeech = microphone.onTranscript('再帮我解释一点'); if (waitForCompletion) await activeSpeech })
}
it('uses the runtime-owned independent question identity without attaching the previous tutor', async () => {
  const question = '两个负数相乘为什么得到正数？'
  const actionId = 'foreground-question:original-independent'
  vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'independent-turn', transcript: question,
    response_text: '我来说明这个问题。', action: { type: 'ask_tutor', question, subject: '数学', hint_level: 6,
      tutor_request_action_id: actionId, local_only: true }, runtime_execution_location: 'local_model',
    runtime_capability: 'learning.explain' } as never)
  vi.mocked(localApi.askTutor).mockResolvedValue({ ...previous(), turn_id: 'independent-lesson', question,
    hint_level: 6, answer: '两个负数相乘得到正数，可以从分配律保持一致来理解。' })
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript(question) })
  expect(localApi.askTutor).toHaveBeenCalledExactlyOnceWith('synthetic-session', '数学', question, 6, undefined, actionId)
  expect(localApi.saveSessionDraft).toHaveBeenCalledWith('synthetic-session', 'agent_command', expect.objectContaining({
    tutor_recovery: expect.objectContaining({ turnId: 'independent-turn', actionId, question, hintLevel: 6 }),
  }))
  expect(localApi.completeAgentTurn).toHaveBeenCalledWith('independent-turn',
    '两个负数相乘得到正数，可以从分配律保持一致来理解。', 'completed', undefined, expect.anything())
})
it('routes a bound follow-up to the saved source and retains it for recovery', async () => {
  const question = '为什么要取倒数？'
  vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'follow-turn', transcript: question,
    response_text: '我们接着看。', action: { type: 'ask_tutor', question, subject: '数学', hint_level: 6,
      question_relation: 'followup', tutor_followup_binding: { turn_id: 'previous-tutor', source_sha256: 'proof' } },
    runtime_execution_location: 'local_model', runtime_capability: 'learning.explain' } as never)
  const followup = vi.spyOn(localApi, 'askTutorFollowup').mockResolvedValue({ ...previous(), turn_id: 'follow-result', answer: '我们接着看倒数。' })
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  const input = screen.getByLabelText('对学习伙伴说')
  fireEvent.change(input, { target: { value: question } })
  fireEvent.keyDown(input, { key: 'Enter' })
  await waitFor(() => expect(followup).toHaveBeenCalledTimes(1))
  expect(followup).toHaveBeenCalledExactlyOnceWith('synthetic-session', 'previous-tutor', question, 'follow-turn-followup')
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(localApi.saveSessionDraft).toHaveBeenCalledWith('synthetic-session', 'agent_command', expect.objectContaining({
    tutor_recovery: expect.objectContaining({ actionId: 'follow-turn-followup', followupSourceTurnId: 'previous-tutor', question }),
  }))
})
it('does not send a follow-up when its local source binding is unavailable', async () => {
  const question = '为什么要取倒数？'
  vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'follow-unbound', transcript: question,
    response_text: '我们接着看。', action: { type: 'ask_tutor', question, subject: '数学', hint_level: 6,
      question_relation: 'followup', tutor_followup_binding: null },
    runtime_execution_location: 'local_model', runtime_capability: 'learning.explain' } as never)
  const followup = vi.spyOn(localApi, 'askTutorFollowup')
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  const input = screen.getByLabelText('对学习伙伴说')
  fireEvent.change(input, { target: { value: question } })
  fireEvent.keyDown(input, { key: 'Enter' })
  await screen.findByText('刚才的讲解来源暂时无法核对，请把这次想问的内容重新说清楚。')
  expect(followup).not.toHaveBeenCalled()
  expect(localApi.askTutor).not.toHaveBeenCalled()
})
it('continues the actual global question with a durable parent turn before study stage', async () => {
  await speak('more_tutor_hint')
  await waitFor(() => expect(localApi.askTutor).toHaveBeenCalledWith('synthetic-session', '数学', previous().question, 2, 'previous-tutor', 'synthetic-voice-turn-hint'))
  expect(localApi.completeAgentTurn).toHaveBeenCalledWith('synthetic-voice-turn', '看一下乘法规律。', 'completed', undefined, expect.anything())
})
it('keeps local visual narration off cloud speech even when the original voice action permits cloud speech', async () => {
  const turn = { ...previous(), turn_id: 'visual-tutor', answer: '请先观察图中已经确认的闭合边界。',
    output_envelope: { result: { local_visual: { local_only: true, cloud_transmission_allowed: false } } } } as unknown as TutorTurn
  vi.mocked(localApi.askTutor).mockResolvedValue(turn)
  await speak('more_tutor_hint')
  await waitFor(() => expect(inputs.speakLocally).toHaveBeenCalledWith(turn.answer, 'synthetic-session', expect.any(Function), expect.any(Function),
    expect.objectContaining({ localOnly: true })))
})
it('persists the visual narration restriction when tutor acknowledgement fails', async () => {
  const turn = { ...previous(), turn_id: 'visual-tutor', answer: '本地图形提示。',
    output_envelope: { result: { local_visual: { local_only: true, cloud_transmission_allowed: false } } } } as unknown as TutorTurn
  vi.mocked(localApi.askTutor).mockResolvedValue(turn)
  vi.mocked(localApi.completeAgentTurn).mockRejectedValue(new Error('lost acknowledgement'))
  await speak('more_tutor_hint')
  const saved = readTutorCompletions('synthetic-session', 'synthetic-child')
  expect(saved).toHaveLength(1)
  expect(saved[0].localOnly).toBe(true)
  expect(saved[0].tutor?.turn_id).toBe('visual-tutor')
})
it.each(['TUTOR_QUESTION_REFERENCE_CHANGED', 'TUTOR_QUESTION_LOCAL_ONLY', 'FOREGROUND_TUTOR_REPLY_SCOPE_CHANGED'])('keeps %s out of the cloud retry loop, including after remount', async code => {
  vi.mocked(localApi.askTutor).mockRejectedValueOnce(new ApiError('source blocked', 409, code))
  await speak('more_tutor_hint')
  expect(localApi.askTutor).toHaveBeenCalledTimes(1)
  const saved = vi.mocked(localApi.saveSessionDraft).mock.calls
    .map(call => call[2] as { tutor_recovery?: { sourceBlocked?: string } }).reverse().find(row => row.tutor_recovery?.sourceBlocked)
  expect(saved?.tutor_recovery?.sourceBlocked).toBe(code === 'TUTOR_QUESTION_REFERENCE_CHANGED' ? 'changed'
    : code === 'FOREGROUND_TUTOR_REPLY_SCOPE_CHANGED' ? 'foreground_reply' : 'local_only')
  expect(screen.queryByRole('button', { name: '重试讲解' })).not.toBeInTheDocument()
  cleanup()
  const initial = workspace()
  initial.drafts = { agent_command: { updated_at: new Date().toISOString(), payload: saved } } as SessionWorkspaceState['drafts']
  await act(async () => { render(<SessionWorkspace initial={initial} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('重试讲解') })
  await act(async () => { await microphone.onTranscript('确认再次请求') })
  expect(localApi.askTutor).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('button', { name: '确认再次请求' })).not.toBeInTheDocument()
})
it('saves global tutor feedback with a stable request identity', async () => {
  await speak('record_tutor_feedback')
  await waitFor(() => expect(localApi.saveTutorFeedback).toHaveBeenCalledWith('previous-tutor', '再帮我解释一点', 'still_stuck', 'synthetic-voice-turn-feedback'))
})
it.each(['study', 'reflection'])('keeps hint and feedback ownership in the runtime during %s', async phase => {
  await speak('more_tutor_hint', previous(), true, phase)
  expect(localApi.askTutor).toHaveBeenCalledWith('synthetic-session', '数学', previous().question, 2, 'previous-tutor', 'synthetic-voice-turn-hint')
  cleanup()
  vi.mocked(localApi.completeAgentTurn).mockClear()
  await speak('record_tutor_feedback', previous(), true, phase)
  expect(localApi.saveTutorFeedback).toHaveBeenCalledWith('previous-tutor', '再帮我解释一点', 'still_stuck', 'synthetic-voice-turn-feedback')
})
it.each(['other-child', 'stale'])('does not use %s tutor as current evidence', async problem => {
  const tutor = previous()
  if (problem === 'other-child') tutor.child_id = 'someone-else'
  else tutor.created_at = '2020-01-01T00:00:00Z'
  await speak('more_tutor_hint', tutor)
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).toHaveBeenCalledWith('synthetic-voice-turn', expect.any(String), 'failed', 'CLIENT_VOICE_ACTION_UNAVAILABLE')
})

it.each(['success', 'failure'])('drains speech queued during %s recovery without erasing it', async outcome => {
  let settle!: () => void
  vi.mocked(localApi.askTutor).mockImplementationOnce(() => new Promise((resolve, reject) => {
    settle = () => outcome === 'success' ? resolve({ ...previous(), answer: '恢复后的提示。' }) : reject(new Error('temporary failure'))
  }))
  vi.mocked(localApi.agentTurn).mockImplementation(async (_sid, transcript) => ({ agent_turn_id: `queued-${transcript}`, transcript, response_text: '好的。', action: { type: 'none' } }) as never)
  const initial = workspace()
  initial.drafts = { agent_command: { updated_at: new Date().toISOString(), payload: { tutor_recovery: { version: 1, turnId: 'old-voice-turn', actionId: 'retry-hint-action', subject: '数学', question: previous().question,
    hintLevel: 2, parentTurnId: 'previous-tutor', actionType: 'more_tutor_hint', createdAt: new Date().toISOString(), attempts: 1, requiresExplicitRetryConfirmation: true } } } } as SessionWorkspaceState['drafts']
  await act(async () => { render(<SessionWorkspace initial={initial} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { void microphone.onTranscript('确认再次请求') })
  await waitFor(() => expect(localApi.askTutor).toHaveBeenCalled())
  await act(async () => { void microphone.onTranscript('我还有一个问题'); void microphone.onTranscript('英语作业也要记上') })
  await act(async () => { settle() })
  await waitFor(() => expect(localApi.agentTurn).toHaveBeenCalledTimes(2))
  expect(vi.mocked(localApi.agentTurn).mock.calls.map(call => call[1])).toEqual(['我还有一个问题', '英语作业也要记上'])
  expect(vi.mocked(localApi.askTutor).mock.calls[0][4]).toBe('previous-tutor')
})

const pendingResults = () => readTutorCompletions('synthetic-session', 'synthetic-child')
const answerPlaybackCount = () => vi.mocked(inputs.speakLocally).mock.calls.filter(call => call[0] === '看一下乘法规律。').length
async function remountWithRecovery() {
  const saved = vi.mocked(localApi.saveSessionDraft).mock.calls.filter(call => call[1] === 'agent_command').at(-1)?.[2]
  cleanup()
  const initial = workspace()
  if (saved) initial.drafts = { agent_command: { payload: saved, updated_at: new Date().toISOString() } }
  await act(async () => { render(<SessionWorkspace initial={initial} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
}

it.each(['more_tutor_hint', 'ask_tutor'])('restores %s result after ack loss and retries only the identical receipt', async action => {
  vi.mocked(localApi.completeAgentTurn).mockRejectedValueOnce(new Error('ack lost'))
  await speak(action)
  await waitFor(() => expect(pendingResults()).toHaveLength(1))
  const saved = pendingResults()[0]
  expect(saved.tutor?.turn_id).toBe('next-tutor')
  expect(saved.acknowledged).toBe(false)
  expect(answerPlaybackCount()).toBe(0)
  const firstReceipt = vi.mocked(localApi.completeAgentTurn).mock.calls[0]
  await remountWithRecovery()
  await act(async () => { await microphone.onTranscript('重试讲解') })
  await waitFor(() => expect(pendingResults()).toHaveLength(0))
  expect(localApi.askTutor).toHaveBeenCalledTimes(1)
  expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
  expect(vi.mocked(localApi.completeAgentTurn).mock.calls[1]).toEqual(firstReceipt)
  expect(vi.mocked(localApi.completeAgentTurn).mock.calls.every(call => call[2] === 'completed')).toBe(true)
  expect(answerPlaybackCount()).toBe(1)
})

it('keeps acknowledged results when draft cleanup fails and does not replay the model, receipt or speech after remount', async () => {
  vi.mocked(localApi.clearSessionDraft).mockRejectedValueOnce(new Error('delete response lost')).mockResolvedValue({ section: 'agent_command', deleted: true })
  await speak('more_tutor_hint')
  await waitFor(() => expect(pendingResults()[0]).toMatchObject({ acknowledged: true, presentationStarted: true }))
  expect(answerPlaybackCount()).toBe(1)
  await remountWithRecovery()
  await act(async () => { await microphone.onTranscript('重试讲解') })
  await waitFor(() => expect(pendingResults()).toHaveLength(0))
  expect(localApi.askTutor).toHaveBeenCalledTimes(1)
  expect(localApi.completeAgentTurn).toHaveBeenCalledTimes(1)
  expect(answerPlaybackCount()).toBe(1)
  expect(localApi.clearSessionDraft).toHaveBeenCalledTimes(2)
})

it('does not turn saved feedback into a failed action when projection refresh fails', async () => {
  vi.mocked(localApi.sessionWorkspace).mockRejectedValue(new Error('projection offline'))
  await speak('record_tutor_feedback')
  await waitFor(() => expect(localApi.completeAgentTurn).toHaveBeenCalled())
  expect(localApi.saveTutorFeedback).toHaveBeenCalledTimes(1)
  expect(localApi.completeAgentTurn).toHaveBeenCalledWith('synthetic-voice-turn', expect.any(String), 'completed', undefined,
    expect.objectContaining({ client_bridge_receipt: expect.objectContaining({ output: { feedback_id: 'feedback-saved' } }) }))
  expect(vi.mocked(localApi.completeAgentTurn).mock.calls.every(call => call[2] === 'completed')).toBe(true)
  expect(await screen.findByText('结果已保存，页面状态暂未刷新。')).toBeInTheDocument()
})

it('retries only the saved feedback receipt after ack loss, not the feedback mutation', async () => {
  vi.mocked(localApi.completeAgentTurn).mockRejectedValueOnce(new Error('ack lost'))
  await speak('record_tutor_feedback')
  await waitFor(() => expect(pendingResults()).toHaveLength(1))
  const first = vi.mocked(localApi.completeAgentTurn).mock.calls[0]
  await remountWithRecovery()
  await act(async () => { await microphone.onTranscript('重试讲解') })
  await waitFor(() => expect(pendingResults()).toHaveLength(0))
  expect(localApi.saveTutorFeedback).toHaveBeenCalledTimes(1)
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(vi.mocked(localApi.completeAgentTurn).mock.calls[1]).toEqual(first)
})

it('journals a late model result after unmount without speaking or sending a failed receipt', async () => {
  let resolveTutor!: (tutor: TutorTurn) => void
  vi.mocked(localApi.askTutor).mockImplementationOnce(() => new Promise(resolve => { resolveTutor = resolve }))
  await speak('more_tutor_hint', previous(), false)
  await waitFor(() => expect(localApi.askTutor).toHaveBeenCalledTimes(1))
  cleanup()
  await act(async () => { resolveTutor({ ...previous(), turn_id: 'next-tutor', hint_level: 2, answer: '看一下乘法规律。' }); await activeSpeech })
  expect(pendingResults()).toHaveLength(1)
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(answerPlaybackCount()).toBe(0)
  await remountWithRecovery()
  await act(async () => { await microphone.onTranscript('重试讲解') })
  await waitFor(() => expect(pendingResults()).toHaveLength(0))
  expect(localApi.askTutor).toHaveBeenCalledTimes(1)
  expect(vi.mocked(localApi.completeAgentTurn).mock.calls.every(call => call[2] === 'completed')).toBe(true)
})

it('retains typed input while a known-result acknowledgement is pending', async () => {
  let finishAck!: () => void
  vi.mocked(localApi.completeAgentTurn).mockImplementationOnce(() => new Promise(resolve => { finishAck = () => resolve({ agent_turn_id: 'synthetic-voice-turn', tool_status: 'completed' } as never) }))
  await speak('more_tutor_hint', previous(), false)
  await waitFor(() => expect(localApi.completeAgentTurn).toHaveBeenCalledTimes(1))
  fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: '新的问题还没有说完' } })
  await act(async () => { finishAck(); await activeSpeech })
  await waitFor(() => expect(pendingResults()).toHaveLength(0))
  expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('新的问题还没有说完')
  expect(localApi.clearSessionDraft).not.toHaveBeenCalled()
  expect(localApi.saveSessionDraft).toHaveBeenCalledWith('synthetic-session', 'agent_command', { text: '新的问题还没有说完', tutor_recovery: null })
})

it('keeps a received result in memory when local journaling fails, and retries storage without asking the model again', async () => {
  const storage = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage full') })
  await speak('more_tutor_hint')
  await waitFor(() => expect(localApi.askTutor).toHaveBeenCalledTimes(1))
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  storage.mockRestore()
  await act(async () => { await microphone.onTranscript('重试讲解') })
  await waitFor(() => expect(localApi.completeAgentTurn).toHaveBeenCalledTimes(1))
  expect(localApi.askTutor).toHaveBeenCalledTimes(1)
  expect(pendingResults()).toHaveLength(0)
})

it.each(['ack', 'cleanup'])('retains a result obtained through the legacy recovery entry when %s fails', async failure => {
  const initial = workspace()
  initial.drafts = { agent_command: { updated_at: new Date().toISOString(), payload: { text: '我的原问题', tutor_recovery: {
    version: 1, turnId: 'old-voice-turn', actionId: 'retry-hint-action', subject: '数学', question: previous().question,
    hintLevel: 2, parentTurnId: 'previous-tutor', actionType: 'more_tutor_hint', createdAt: new Date().toISOString(), attempts: 1, requiresExplicitRetryConfirmation: true,
  } } } }
  if (failure === 'ack') vi.mocked(localApi.completeAgentTurn).mockRejectedValueOnce(new Error('ack lost'))
  else vi.mocked(localApi.clearSessionDraft).mockRejectedValueOnce(new Error('delete failed'))
  await act(async () => { render(<SessionWorkspace initial={initial} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('确认再次请求') })
  await waitFor(() => expect(pendingResults()).toHaveLength(1))
  expect(pendingResults()[0].acknowledged).toBe(failure === 'cleanup')
  await remountWithRecovery()
  await act(async () => { await microphone.onTranscript('重试讲解') })
  await waitFor(() => expect(pendingResults()).toHaveLength(0))
  expect(localApi.askTutor).toHaveBeenCalledExactlyOnceWith('synthetic-session', '数学', previous().question, 2, 'previous-tutor', 'retry-hint-action', true)
  const receipts = vi.mocked(localApi.completeAgentTurn).mock.calls
  expect(receipts.every(call => call[2] === 'completed')).toBe(true)
  if (failure === 'ack') expect(receipts[0]).toEqual(receipts[1])
  else expect(receipts).toHaveLength(1)
  expect(answerPlaybackCount()).toBe(1)
})

it('does not accept a nonterminal acknowledgement as saved completion', async () => {
  vi.mocked(localApi.completeAgentTurn).mockResolvedValueOnce({ agent_turn_id: 'synthetic-voice-turn', tool_status: 'pending' } as never)
  await speak('more_tutor_hint')
  expect(pendingResults()[0].acknowledged).toBe(false)
  expect(answerPlaybackCount()).toBe(0)
  await act(async () => { await microphone.onTranscript('重试讲解') })
  expect(localApi.askTutor).toHaveBeenCalledTimes(1)
  expect(pendingResults()).toHaveLength(0)
})

it.each(['cancelled', 'invalidated'] as const)('does not restore a %s old-question board or speak after reload', async status => {
  vi.spyOn(localApi, 'tutorPlayback').mockResolvedValue({ playback: {
    child_id: 'synthetic-child', session_id: 'synthetic-session', turn_id: 'old-question-turn',
    playback_id: 'old-question-playback', status, segment_index: 0, segment_started: false,
    presentation: { version: 'tutor_blackboard_v1', source: 'model_structured', subject: '数学', title: '已经换题的旧讲解',
      sync_mode: 'segment_estimate', segments: [{ segment_id: 'old-segment', narration: '上一题的提示。', blocks: [] }] },
  } } as never)
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  expect(localApi.tutorPlayback).toHaveBeenCalled()
  expect(screen.queryByText('已经换题的旧讲解')).not.toBeInTheDocument()
  expect(inputs.speakLocally).not.toHaveBeenCalled()
})


it.each([false, true])('respects server coaching playback permission %s on an explicit retry receipt', async allowed => {
  const text = '你刚才的回答保留了，我们核对下一小步。'
  const planned = { agent_turn_id: 'explicit-coaching-retry', transcript: '重试刚才的点评', response_text: '核对原回执。',
    action: { type: 'retry_tutor_check_coaching', local_only: true, cloud_transmission_allowed: false },
    runtime_capability: 'tutor.retry_check_coaching', runtime_execution_location: 'server' }
  vi.mocked(localApi.agentTurn).mockResolvedValue(planned as never)
  vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn: { ...planned, tool_status: 'completed', response_text: text },
    execution: { capability: 'tutor.retry_check_coaching', response_text: text, replay_audio_allowed: allowed,
      tutor_turn: { ...previous(), turn_id: 'saved-retry-coaching', hint_level: 1, answer: text },
      result: { coaching_status: 'completed' }, evidence: ['saved_check_coaching'] } } as never)
  await act(async () => { render(<SessionWorkspace initial={workspace()} childName="测试同学" childGrade="四年级" onBack={vi.fn()} />) })
  await act(async () => { await microphone.onTranscript('重试刚才的点评') })
  expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith('explicit-coaching-retry')
  expect(vi.mocked(inputs.speakLocally).mock.calls.filter(([spoken]) => spoken === text)).toHaveLength(allowed ? 1 : 0)
  expect(localApi.askTutor).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
})
