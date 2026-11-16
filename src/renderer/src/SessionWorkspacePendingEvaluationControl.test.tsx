import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import * as inputs from './LocalInputs'
import { SessionWorkspace } from './SessionWorkspace'
import { readPendingEvaluation, savePendingEvaluation, type PendingEvaluationReceipt } from './pendingEvaluationReceipt'
import type { AgentTurn, SessionWorkspaceState } from './types'
import { clearVoiceCaptureContext, setVoiceActivityCapture, setVoiceQuestionCapture, voiceCaptureReference } from './voiceCaptureContext'
import { LOCAL_RECOVERY_FAILURE, LOCAL_RECOVERY_GUIDANCE } from './localVoiceRecovery'

let microphone: ComponentProps<typeof inputs.HandsFreeVoiceInput>
vi.mock('./LocalInputs', async original => ({
  ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null },
  VoiceInputButton: () => null,
}))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))

const now = '2026-09-09T08:00:00Z'
const workspace: SessionWorkspaceState = {
  session: { session_id: 'pending-control-session', child_id: 'pending-control-child', status: 'active', state: 'study',
    started_at: now, last_checkpoint_at: now, observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status: 'paused', snooze_until: null,
    recall_declared_empty: 0, homework_declared_empty: 0 },
  family_rule: { child_id: 'pending-control-child', latest_end_time: '21:30', max_learning_minutes: 90,
    task_segment_minutes: 25, break_minutes: 5, pet_min_minutes: 0, competition_minutes: 0,
    observation_mode: 'shadow', reminder_budget_per_10m: 1, misclassification_shadow_threshold: 3, version: 1, updated_at: now },
  planning_context: { energy_level: 'medium', fixed_commitment_minutes: 0, fixed_commitment_note: null,
    tomorrow_event_subject: null, tomorrow_event_title: null, tomorrow_event_minutes: 0, updated_at: now },
  recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [], drafts: {},
  latest_tutor_turn: null, daily_report_ready: false, plan: null, plan_items: [],
  sensor_release: { opened_leases: 0, active_leases: 0, released_leases: 0, application_release_confirmed: false,
    evidence_level: 'not_opened', os_release_verified: false, release_verification: null, by_device: {} },
}

beforeEach(() => {
  vi.spyOn(localApi, 'readVoiceInteractionHandoff').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, status: 'none', handoff_id: null, agent_action_id: null,
    deferred_progress: null, request: null, receipt: null, reason: null,
  }))
  vi.spyOn(localApi, 'invalidateVoiceInteractionHandoff').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, status: 'none', handoff_id: null, agent_action_id: null,
    deferred_progress: null, request: null, receipt: null, reason: null,
  }))
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NO_NETWORK_IN_RED_REPRO')))
  vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
    getWakeWordSettings: vi.fn(async () => ({ childId: workspace.session.child_id, wakeWord: '小知', enabled: true, inactivitySeconds: 60, updatedAt: now })),
    getVoiceOnboarding: vi.fn(async () => ({ childId: workspace.session.child_id, version: 1, step: 'completed', updatedAt: now })),
  }))
  vi.spyOn(inputs, 'speakLocally').mockImplementation(() => undefined)
  vi.spyOn(inputs, 'cancelLocalSpeech').mockImplementation(() => undefined)
  vi.spyOn(localApi, 'agentRuntimeStatus').mockRejectedValue(new Error('NO_RUNTIME_IN_RED_REPRO'))
  vi.spyOn(localApi, 'activityVoiceFocus').mockResolvedValue({ session_id: workspace.session.session_id,
    child_id: workspace.session.child_id, activity_focus: null, local_only: true, cloud_transmission_allowed: false })
  // Deliberately stop at semantic dispatch: this test cannot prove device or backend admission.
  vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new Error('SEMANTIC_DISPATCH_BOUNDARY'))
  vi.spyOn(localApi, 'executeAgentTurn').mockRejectedValue(new Error('UNEXPECTED_EXECUTION'))
  vi.spyOn(localApi, 'completeAgentTurn').mockRejectedValue(new Error('UNEXPECTED_COMPLETION'))
  vi.spyOn(localApi, 'saveSessionDraft').mockImplementation(async (_id, section, payload) => ({ section, payload, updated_at: now }))
})
afterEach(() => { cleanup(); localStorage.clear(); clearVoiceCaptureContext(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

async function mount(status: PendingEvaluationReceipt['status']) {
  const receipt: PendingEvaluationReceipt = { version: 1, sessionId: workspace.session.session_id,
    childId: workspace.session.child_id, turnId: 'original-evaluation-only', status, silent: false }
  savePendingEvaluation(receipt)
  vi.spyOn(localApi, 'agentTurnExecutionStatus').mockResolvedValue({ status })
  await act(async () => {
    render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />)
  })
  return receipt
}

it.each([
  ['remediate', '还需巩固的目标'],
  ['transfer_check', '换个情境再检查'],
  ['retention_due', '留待之后复查的目标'],
  ['retention_check', '检查是否还记得'],
])('renders the actual learning phase %s without repeating a finished task command', async (phase, title) => {
  const runtime = { session_id: workspace.session.session_id,
    active_objective: { objective_id: 'original-learning-goal', objective_type: 'learning' as const,
      summary: '按本题要求核对自己的书面作答', success_criteria: ['用过程支持结论'], status: 'active', phase },
    current_step: { step_id: 'finished-review-resume', capability_name: 'task.resume', status: 'completed',
      plan_status: 'completed', expected_evidence: [] } }
  vi.mocked(localApi.agentRuntimeStatus).mockResolvedValue(runtime)
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({ status: 'no_item', prompt: null,
    attempt: null, reason: null })
  await act(async () => {
    render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="初三" onBack={vi.fn()} />)
  })
  expect(screen.getByText(title)).toBeInTheDocument()
  expect(screen.getByText(runtime.active_objective.summary)).toBeInTheDocument()
  expect(screen.queryByText('现在一起完成')).not.toBeInTheDocument()
  expect(screen.getByText('目标标准：用过程支持结论')).toBeInTheDocument()
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(runtime.active_objective.status).toBe('active')
  expect(inputs.speakLocally).not.toHaveBeenCalled()
})

for (const status of ['in_progress', 'unknown'] as const) {
  it.each([
    ['device status', '帮我看看麦克风现在是不是正常的'],
    ['pause', '我需要缓一下，先把这项任务暂停好吗'],
    ['parent handoff', '请让妈妈来接着处理，我现在想休息'],
  ])(`keeps ${status} receipt but admits natural %s to semantic dispatch`, async (_kind, utterance) => {
    const receipt = await mount(status)
    await act(async () => { await microphone.onTranscript(utterance) })
    expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual(receipt)
    expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    expect(localApi.agentTurn).toHaveBeenCalledWith(receipt.sessionId, utterance, undefined, undefined, receipt.turnId)
  })

  it(`does not resubmit scoring on a bare retry while ${status}`, async () => {
    const receipt = await mount(status)
    await act(async () => { await microphone.onTranscript('再试一次') })
    expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual(receipt)
  })
}

function restricted(receipt: PendingEvaluationReceipt, action: AgentTurn['action'] = { type: 'none' }, capability = 'conversation.respond'): AgentTurn {
  return { session_id: receipt.sessionId, child_id: receipt.childId, agent_turn_id: 'new-control',
    transcript: '新的控制话语', intent: 'pending_evaluation_control', response_text: '新的控制回复',
    action: { ...action, pending_evaluation_control: { version: 'pending_evaluation_control_v1',
      evaluation_turn_id: receipt.turnId, mode: 'control_only' } }, tool_status: action.type === 'none' ? 'completed' : 'pending',
    runtime_capability: capability, runtime_execution_location: 'server', after_execution: 'stop',
    tool_error_code: null, completed_at: null, created_at: now, orchestrator_version: 'test' }
}

it('executes a admitted pause on the new turn, retaining the original receipt silently', async () => {
  const receipt = await mount('unknown')
  const turn = restricted(receipt, { type: 'pause_active_task' }, 'task.pause')
  vi.mocked(localApi.agentTurn).mockResolvedValue(turn)
  vi.mocked(localApi.executeAgentTurn).mockResolvedValue({ turn: { ...turn, tool_status: 'completed' },
    execution: { capability: 'task.pause', response_text: turn.response_text, result: { status: 'paused' }, evidence: ['task_paused'] } })
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(workspace)
  await act(async () => { await microphone.onTranscript('先停一下这项任务，我想休息') })
  expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith('new-control')
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual({ ...receipt, silent: true })
})

it('answers an independent resource question from the executed receipt while keeping the original evaluation', async () => {
  const receipt = await mount('in_progress')
  const utterance = '等评价的时候，你能告诉我有没有PET学习资料吗？'
  const turn = { ...restricted(receipt, { type: 'inspect_learning_system', scope: 'resources', track: 'pet',
    local_only: true, cloud_transmission_allowed: false }, 'system.inspect_learning'),
    transcript: utterance, intent: 'system.inspect_learning',
    response_text: '我有一百万道PET题。' }
  const response = '本机的PET练习资料已核对，尚未验证的题目不用于掌握判断。'
  vi.mocked(localApi.agentTurn).mockResolvedValue(turn)
  vi.mocked(localApi.executeAgentTurn).mockResolvedValue({ turn: { ...turn, response_text: response, tool_status: 'completed' },
    execution: { capability: 'system.inspect_learning', response_text: response,
      result: { cloud_transmission_allowed: false }, evidence: ['synthetic-local-resource-receipt'] } })
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(workspace)
  await act(async () => { await microphone.onTranscript(utterance, undefined, 'canonical-independent-voice', receipt.turnId) })
  expect(localApi.agentTurn).toHaveBeenCalledWith(receipt.sessionId, utterance, undefined, 'canonical-independent-voice', receipt.turnId)
  expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith(turn.agent_turn_id)
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual({ ...receipt, silent: true })
  expect(vi.mocked(inputs.speakLocally).mock.calls.some(([text]) => text === response)).toBe(true)
  expect(vi.mocked(inputs.speakLocally).mock.calls.some(([text]) => text.includes('一百万'))).toBe(false)
})

it.each([
  ['urgent_safety_support', 'find_parent_urgent', '先停下来，请马上找身边的大人陪着你。'],
  ['high_risk_child_safety', 'find_parent', '你不用一个人处理，请找身边可信任的大人。'],
  ['emotional_distress_support', 'none', '你不用马上说清楚，可以找信任的大人陪一会儿。'],
  ['playback_overlap_clarification', 'none', '这句话可能是跟读，也可能混入播放声音。原话已保留，暂不算独立作答。'],
])('delivers registry-shaped local %s through local admission without cloud binding or another evaluation', async (intent, type, response) => {
  const receipt = await mount('unknown')
  const bridge = type !== 'none'
  const capability = bridge ? 'safety.find_parent' : 'conversation.respond'
  const turn = restricted(receipt, { type, local_only: true, cloud_transmission_allowed: false }, capability)
  turn.intent = intent
  turn.runtime_execution_location = bridge ? 'client_device_bridge' : 'server'
  turn.tool_status = 'pending'
  turn.requires_control_admission = bridge
  turn.response_text = response
  vi.mocked(localApi.agentTurn).mockResolvedValue(turn)
  const admission = vi.spyOn(localApi, 'admitVoiceControlExecution').mockResolvedValue({ status: 'admitted' })
  vi.mocked(localApi.completeAgentTurn).mockResolvedValue({ ...turn, tool_status: 'completed' })
  vi.mocked(localApi.executeAgentTurn).mockResolvedValue({ turn: { ...turn, tool_status: 'completed' },
    execution: { capability, response_text: turn.response_text,
      result: { parent_contact_prompted: true }, evidence: ['parent_contact_prompted'] } })
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(workspace)
  await act(async () => { await microphone.onTranscript('请马上找妈妈来陪着我', undefined, 'local-safety-asr', receipt.turnId) })
  if (bridge) expect(admission).toHaveBeenCalledExactlyOnceWith(receipt.sessionId, turn.agent_turn_id)
  else expect(admission).not.toHaveBeenCalled()
  if (bridge) {
    expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).toHaveBeenCalledExactlyOnceWith(turn.agent_turn_id, response, 'completed', undefined,
      expect.objectContaining({ client_bridge_receipt: expect.objectContaining({ capability,
        execution_location: 'client_device_bridge', output: { parent_contact_prompted: true } }) }))
  } else {
    expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith(turn.agent_turn_id)
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  }
  expect(inputs.speakLocally).toHaveBeenCalledWith(turn.response_text, receipt.sessionId,
    expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true, sourceAgentTurnId: turn.agent_turn_id }))
  expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual({ ...receipt, silent: bridge })
})

it('queries the exact original evaluation by GET when natural status speech resolves semantically', async () => {
  vi.useFakeTimers()
  const receipt = await mount('unknown')
  const turn = restricted(receipt, { type: 'none', pending_evaluation_query: { turn_id: receipt.turnId } })
  vi.mocked(localApi.agentTurn).mockResolvedValue(turn)
  await act(async () => { await microphone.onTranscript('刚才的回答到底处理到哪一步了') })
  await act(async () => { await vi.advanceTimersByTimeAsync(1100) })
  expect(localApi.agentTurnExecutionStatus).toHaveBeenCalledWith(receipt.turnId)
  expect(vi.mocked(localApi.agentTurnExecutionStatus).mock.calls.every(([id]) => id === receipt.turnId)).toBe(true)
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual(receipt)
})

it.each(['submit_diagnostic_answer', 'submit_exam_answer', 'submit_pet_vocabulary_answer', 'submit_mistake_answer',
  'ask_tutor', 'confirm_writing_original'])('rejects returned %s before any tool write even with a restricted marker', async type => {
  const receipt = await mount('unknown')
  vi.mocked(localApi.agentTurn).mockResolvedValue(restricted(receipt, { type }, 'learning.evaluate_response'))
  await act(async () => { await microphone.onTranscript('先暂停，不要把这句当作答案') })
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual(receipt)
})

it.each(['PENDING_EVALUATION_CONTROL_SOURCE_CHANGED', 'PENDING_EVALUATION_CONTROL_SCOPE_CHANGED'])('preserves receipt on %s without unrestricted retry', async code => {
  const receipt = await mount('unknown')
  vi.mocked(localApi.agentTurn).mockRejectedValue(new ApiError('changed', 409, code))
  await act(async () => { await microphone.onTranscript('请家长来帮我看一下') })
  expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual(receipt)
  cleanup()
  await act(async () => { render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />) })
  expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual(receipt)
  expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
})

it('keeps a frozen original id through the actual busy FIFO and retains ASR binding', async () => {
  const receipt = await mount('unknown')
  let resolve!: (turn: AgentTurn) => void
  const held = new Promise<AgentTurn>(done => { resolve = done })
  vi.mocked(localApi.agentTurn).mockReturnValueOnce(held).mockResolvedValue(restricted(receipt))
  let first!: Promise<void>
  await act(async () => { first = microphone.onTranscript('请检查设备', undefined, 'asr-one', receipt.turnId) as Promise<void> })
  await act(async () => { await microphone.onTranscript('再告诉妈妈原来的结果还没确认', undefined, 'asr-two', receipt.turnId) })
  expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
  await act(async () => { resolve(restricted(receipt)); await first })
  expect(vi.mocked(localApi.agentTurn).mock.calls.map(call => [call[3], call[4]]))
    .toEqual([['asr-one', receipt.turnId], ['asr-two', receipt.turnId]])
  expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual(receipt)
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
})

it('does not attach a newly pending evaluation to ASR captured without that identity', async () => {
  const receipt = await mount('unknown')
  await act(async () => { await microphone.onTranscript('先暂停一下', undefined, 'asr-without-pending') })
  expect(localApi.agentTurn).not.toHaveBeenCalled()
  expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual(receipt)
})

it('consumes two already-persisted pending activity replies without execute and refreshes each next capture', async () => {
  const receipt = await mount('unknown')
  const question = { kind: 'diagnostic' as const, binding: { prompt_id: 'original-question', item_version: 'original-version' } }
  const initial = { activity_id: 'original-story', version: 'story-version', last_turn_id: 'story-start' }
  setVoiceQuestionCapture(receipt.sessionId, receipt.childId, question)
  setVoiceActivityCapture(receipt.sessionId, receipt.childId, initial)
  const captures = []
  for (const index of [1, 2]) {
    captures.push(voiceCaptureReference(receipt.sessionId, receipt.childId))
    const focus = { ...initial, last_turn_id: `story-reply-${index}` }
    const turn = { ...restricted(receipt), agent_turn_id: focus.last_turn_id, intent: 'companion_activity_reply',
      response_text: `第${index}轮故事回复`, activity_focus: focus }
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn)
    vi.mocked(localApi.activityVoiceFocus).mockResolvedValueOnce({ session_id: receipt.sessionId, child_id: receipt.childId,
      activity_focus: focus, local_only: true, cloud_transmission_allowed: false })
    await act(async () => { await microphone.onTranscript(`故事选择${index}`, undefined, `story-asr-${index}`, receipt.turnId) })
    expect(voiceCaptureReference(receipt.sessionId, receipt.childId)).toEqual({ child_id: receipt.childId,
      question_reference: question, pending_evaluation_turn_id: receipt.turnId, activity_reference: focus })
    expect(inputs.speakLocally).toHaveBeenCalledWith(turn.response_text, receipt.sessionId,
      expect.any(Function), expect.any(Function), expect.objectContaining({ sourceAgentTurnId: turn.agent_turn_id }))
  }
  expect(captures.map(value => value.activity_reference?.last_turn_id)).toEqual(['story-start', 'story-reply-1'])
  expect(captures.every(value => value.pending_evaluation_turn_id === receipt.turnId && value.question_reference?.kind === 'diagnostic')).toBe(true)
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toEqual(receipt)
})

async function mountCorruptRecovery() {
  const key = `hsc-pending-evaluation-v1:${workspace.session.session_id}`
  localStorage.setItem(key, '{retained-broken-journal')
  await act(async () => { render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />) })
  return key
}

function recoverySafety(): AgentTurn {
  return { ...restricted({ version: 1, sessionId: workspace.session.session_id, childId: workspace.session.child_id,
    turnId: 'unused', status: 'unknown', silent: false }), agent_turn_id: 'independent-safety',
    intent: 'urgent_safety_support', tool_status: 'pending', requires_control_admission: true, runtime_capability: 'safety.find_parent',
    runtime_execution_location: 'client_device_bridge', response_text: '请现在直接去找身边的大人。',
    action: { type: 'find_parent_urgent', local_only: true, cloud_transmission_allowed: false,
      local_recovery_control: { version: 'local_voice_recovery_v1', mode: 'safety_only' } } }
}

it('keeps a corrupt journal but dispatches independently captured local safety without old references', async () => {
  const key = await mountCorruptRecovery()
  expect(screen.getByText(LOCAL_RECOVERY_GUIDANCE)).toBeInTheDocument()
  expect(screen.getByText(/上次作答评价的恢复记录无法读取/).parentElement).toHaveClass('agent-tool-recovery')
  expect(screen.queryByText('现在一起完成')).not.toBeInTheDocument()
  expect(voiceCaptureReference(workspace.session.session_id, workspace.session.child_id)).toEqual({
    child_id: workspace.session.child_id, local_recovery_only: true })
  const turn = recoverySafety()
  vi.mocked(localApi.agentTurn).mockResolvedValue(turn)
  const admit = vi.spyOn(localApi, 'admitVoiceControlExecution').mockResolvedValue({ status: 'admitted' })
  vi.mocked(localApi.completeAgentTurn).mockResolvedValue({ ...turn, tool_status: 'completed' })
  await act(async () => { await microphone.onTranscript('请大人来陪我', undefined, 'recovery-asr', undefined, true) })
  expect(localApi.agentTurn).toHaveBeenCalledExactlyOnceWith(workspace.session.session_id,
    '请大人来陪我', undefined, 'recovery-asr', undefined, true)
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).toHaveBeenCalledTimes(1)
  expect(admit).toHaveBeenCalledExactlyOnceWith(workspace.session.session_id, turn.agent_turn_id)
  expect(admit.mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(localApi.completeAgentTurn).mock.invocationCallOrder[0])
  expect(vi.mocked(localApi.completeAgentTurn).mock.calls[0][0]).toBe(turn.agent_turn_id)
  expect(localStorage.getItem(key)).toBe('{retained-broken-journal')
  expect(inputs.speakLocally).toHaveBeenCalledWith(turn.response_text, workspace.session.session_id,
    expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
})

it.each(['ordinary-asr', undefined])('does not upgrade already captured ordinary speech (%s) from a later invalid journal', async voiceId => {
  const key = await mountCorruptRecovery()
  await act(async () => { await microphone.onTranscript('我想继续答题', undefined, voiceId, undefined, false) })
  expect(localApi.agentTurn).not.toHaveBeenCalled()
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localStorage.getItem(key)).toBe('{retained-broken-journal')
})

it.each(['wrong-child', 'no-marker', 'scoring', 'unavailable'])('fails local recovery %s without general retry or claiming delivery', async mode => {
  const key = await mountCorruptRecovery()
  const turn = recoverySafety()
  if (mode === 'wrong-child') turn.child_id = 'other-child'
  if (mode === 'no-marker') delete turn.action.local_recovery_control
  if (mode === 'scoring') { turn.action.type = 'submit_diagnostic_answer'; turn.runtime_capability = 'learning.evaluate_response' }
  if (mode === 'unavailable') vi.mocked(localApi.agentTurn).mockRejectedValue(new ApiError('unavailable', 409, 'LOCAL_RECOVERY_SOURCE_CHANGED'))
  else vi.mocked(localApi.agentTurn).mockResolvedValue(turn)
  await act(async () => { await microphone.onTranscript('需要帮助', undefined, 'recovery-asr', undefined, true) })
  expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(localStorage.getItem(key)).toBe('{retained-broken-journal')
  expect(inputs.speakLocally).toHaveBeenCalledWith(LOCAL_RECOVERY_FAILURE, workspace.session.session_id,
    expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
})

it('retains corrupt recovery and only offers local help when local safety admission changes', async () => {
  const key = await mountCorruptRecovery()
  vi.mocked(localApi.agentTurn).mockResolvedValue(recoverySafety())
  const admit = vi.spyOn(localApi, 'admitVoiceControlExecution').mockResolvedValue({ status: 'source_changed' })
  await act(async () => { await microphone.onTranscript('需要帮助', undefined, 'recovery-asr', undefined, true) })
  expect(admit).toHaveBeenCalledExactlyOnceWith(workspace.session.session_id, 'independent-safety')
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(localStorage.getItem(key)).toBe('{retained-broken-journal')
  expect(inputs.speakLocally).toHaveBeenCalledWith(LOCAL_RECOVERY_FAILURE, workspace.session.session_id,
    expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
})

it.each(['safety', 'ordinary'])('keeps the original journal when silent persistence fails (%s)', async kind => {
  const receipt = await mount('unknown')
  const original = localStorage.getItem(`hsc-pending-evaluation-v1:${receipt.sessionId}`)
  const turn = kind === 'safety'
    ? { ...restricted(receipt, { type: 'find_parent_urgent', local_only: true, cloud_transmission_allowed: false }, 'safety.find_parent'),
      intent: 'urgent_safety_support', runtime_execution_location: 'client_device_bridge' as const, requires_control_admission: true }
    : restricted(receipt, { type: 'pause_active_task' }, 'task.pause')
  const admission = vi.spyOn(localApi, 'admitVoiceControlExecution').mockResolvedValue({ status: 'admitted' })
  vi.mocked(localApi.agentTurn).mockResolvedValue(turn)
  vi.mocked(localApi.completeAgentTurn).mockResolvedValue({ ...turn, tool_status: 'completed' })
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError') })
  await act(async () => { await microphone.onTranscript('请马上找妈妈帮我', undefined, 'safe-capture', receipt.turnId) })
  expect(localStorage.getItem(`hsc-pending-evaluation-v1:${receipt.sessionId}`)).toBe(original)
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  if (kind === 'safety') {
    expect(admission).toHaveBeenCalledExactlyOnceWith(receipt.sessionId, turn.agent_turn_id)
    expect(localApi.completeAgentTurn).toHaveBeenCalledTimes(1)
    expect(inputs.speakLocally).toHaveBeenCalledWith(turn.response_text, receipt.sessionId,
      expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
  } else {
    expect(admission).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  }
})
