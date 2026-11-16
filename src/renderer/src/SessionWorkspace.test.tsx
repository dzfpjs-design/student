import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createHash } from 'node:crypto'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { documentRevisionArtifact, hasMarkedWrongQuestion, isCaptureReadinessAcknowledgement, LocalObservation, SessionWorkspace } from './SessionWorkspace'
import { mediaDeviceInfoHash } from './mediaDevices'
import type { AgentTurn, DiagnosticAttempt, DiagnosticPromptResult, SessionWorkspaceState, TutorTurn, TutorPlaybackCheckpoint, TutorBlackboardPresentation } from './types'
import { decideWakeWordSetup, normalizeSpokenWakeWordCandidate } from './wakeWordRuntime'
import { emitCaptureReceipt, privacyStopKey } from './deviceCommandReceipt'
import * as LocalInputs from './LocalInputs'
import { LocalAgentStreamingTtsProvider } from './companionVoice'
import { readPendingEvaluation, savePendingEvaluation, type PendingEvaluationReceipt } from './pendingEvaluationReceipt'
import { pendingAgentRequests, saveAgentRequest } from './agentRequestJournal'
import { saveAgentExecution } from './agentExecutionJournal'
import { clearVoiceCaptureContext, setVoiceQuestionCapture, trackDiagnosticCapture, voiceCaptureReference } from './voiceCaptureContext'
import type { QwenRealtimeInputCapture, QwenRealtimeToolResult } from './qwenRealtimeVoice'
import { pendingRealtimeAdmissions, prepareRealtimeAdmission, updateRealtimeAdmission } from './realtimeAdmissionJournal'
import { SpeechArbiter } from './speechArbiter'
import { reconcileObservationRegistration } from './observationCameraStartup'
import { reconcileObservationCoverage } from './observationCoverageStartup'

const recordedCoverage = (epoch: string, boundary: 'start' | 'stop') => ({ status: 'recorded', event_id: `coverage-action:${createHash('sha256').update(`coverage-${boundary}:${epoch}`).digest('hex')}` })

const realtimeHarness = vi.hoisted(() => ({ onTurn: null as null | ((text: string, capture?: QwenRealtimeInputCapture) => Promise<QwenRealtimeToolResult>),
  onInputCompleted: null as null | ((text: string, capture?: QwenRealtimeInputCapture) => void),
  props: null as null | { onStatus?: (status: import('./realtimeVoiceRuntime').RealtimeVoiceStatus) => void;
    onPendingInputHandoff?: (inputs: import('./qwenRealtimeVoice').QwenRealtimeQueuedInput[], signal: AbortSignal, retry?: boolean) => Promise<boolean>;
    onPermanentFailure?: (message: string) => void;
    onRepeatRequired?: (scope: { sessionId: string; childId: string }) => void;
    onConversationActiveChange?: (active: boolean) => void; initialConversationActive?: boolean } }))
vi.mock('./QwenRealtimeVoiceInput', () => ({ QwenRealtimeVoiceInput: (props: { onTurn: typeof realtimeHarness.onTurn; onInputCompleted: typeof realtimeHarness.onInputCompleted } & NonNullable<typeof realtimeHarness.props>) => {
  realtimeHarness.onTurn = props.onTurn
  realtimeHarness.onInputCompleted = props.onInputCompleted
  realtimeHarness.props = props
  return null
} }))

import type { ControlledLaunchScope, ControlledLaunchIntent } from '../../shared/controlledLaunch'

// Explicit per-host native contract fixture; never installed for missing-native cases.
// A successful play() must model the playing state as well as its fulfilled promise.
function mockSuccessfulMediaPlay(this: HTMLMediaElement): Promise<void> {
  Object.defineProperties(this, {
    paused: { configurable: true, value: false },
    ended: { configurable: true, value: false },
  })
  return Promise.resolve()
}

function nativeLaunchFixture() {
  const intents = new Map<string, ControlledLaunchIntent>()
  return vi.fn(async (scope: ControlledLaunchScope): Promise<ControlledLaunchIntent> => {
    const key = JSON.stringify([scope.childId, scope.sessionId, scope.kind, scope.requestId])
    let intent = intents.get(key)
    if (!intent) {
      intent = {...scope, intentId: crypto.randomUUID(), activityId: null, state: 'prepared'}
      intents.set(key, intent)
    }
    return intent
  })
}

const workspace: SessionWorkspaceState = {
  session: {
    session_id: 'session-draft-1',
    child_id: 'child-junior-3',
    status: 'active',
    state: 'classroom_recall',
    started_at: '2026-08-26T10:00:00Z',
    last_checkpoint_at: '2026-08-26T10:00:00Z',
    observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status: 'paused',
    snooze_until: null,
    recall_declared_empty: 0,
    homework_declared_empty: 0
  },
  family_rule: {
    child_id: 'child-junior-3',
    latest_end_time: '21:30',
    max_learning_minutes: 120,
    task_segment_minutes: 25,
    break_minutes: 5,
    pet_min_minutes: 0,
    competition_minutes: 0,
    observation_mode: 'shadow',
    reminder_budget_per_10m: 1,
    misclassification_shadow_threshold: 3,
    version: 1,
    updated_at: '2026-08-26T10:00:00Z'
  },
  planning_context: {
    energy_level: 'medium',
    fixed_commitment_minutes: 0,
    fixed_commitment_note: null,
    tomorrow_event_subject: null,
    tomorrow_event_title: null,
    tomorrow_event_minutes: 0,
    updated_at: '2026-08-26T10:00:00Z'
  },
  recalls: [],
  homework: [],
  carryover_candidates: [],
  carryover_decisions: [],
  plan: null,
  plan_items: [],
  daily_report_ready: false,
  latest_tutor_turn: null,
  sensor_release: {
    opened_leases: 0,
    active_leases: 0,
    released_leases: 0,
    application_release_confirmed: false,
    evidence_level: 'not_opened',
    os_release_verified: false,
    release_verification: null,
    by_device: {}
  },
  drafts: {
    classroom_recall: {
      payload: { subject: '数学', summary: '今天讲了相似三角形' },
      updated_at: '2026-08-26T10:03:00Z'
    }
  }
}

const studyWorkspace: SessionWorkspaceState = {
  ...workspace,
  session: { ...workspace.session, state: 'study' },
  plan: {
    plan_id: 'plan-1', plan_date: '2026-08-26', status: 'active', total_minutes: 25,
    generator: 'local_rule_engine', version: 1
  },
  plan_items: [{
    plan_item_id: 'plan-item-1', homework_id: null, subject: '数学', title: '二次方程作业',
    expected_minutes: 25, actual_minutes: null, priority: 1, status: 'pending', position: 1,
    started_at: null, completed_at: null, active_seconds: 0, active_segment_started_at: null,
    source_type: 'school_homework', source_ref: null, completion_standard: '完成并检查',
    cognitive_load: 'medium', task_type: 'written', completion_evidence_type: null,
    completion_note: null, completion_checklist: [], completion_artifact_id: null
  }],
  drafts: {}
}

const petStudyWorkspace: SessionWorkspaceState = {
  ...studyWorkspace,
  session: { ...studyWorkspace.session, child_id: 'child-primary-4' },
  family_rule: { ...studyWorkspace.family_rule, child_id: 'child-primary-4', pet_min_minutes: 10 },
  plan_items: [{
    ...studyWorkspace.plan_items[0],
    plan_item_id: 'pet-plan-item-1', subject: '英语', title: '故事复述 · 说清人物、经过和结果',
    expected_minutes: 10, source_type: 'pet_daily', source_ref: 'diag-pet-daily-retell-1',
    completion_standard: '听或读短故事后，用英语复述人物、主要经过和结果',
    task_type: 'speaking'
  }]
}

const offlineChineseVoice = {
  name: 'Microsoft Huihui', voiceURI: 'local-huihui', lang: 'zh-CN', localService: true, default: true,
} satisfies SpeechSynthesisVoice

beforeEach(() => {
  // Peripheral reads must never reach a developer's running local service.
  // Tests exercising these contracts install their own explicit responses.
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('UNCONFIGURED_COMPONENT_API')))
  vi.spyOn(localApi, 'activityVoiceFocus').mockRejectedValue(new Error('ACTIVITY_FOCUS_NOT_CONFIGURED_IN_FIXTURE'))
  vi.spyOn(localApi, 'agentRuntimeStatus').mockRejectedValue(new Error('AGENT_RUNTIME_NOT_CONFIGURED_IN_FIXTURE'))
  vi.spyOn(localApi, 'readVoiceInteractionHandoff').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, status: 'none', handoff_id: null, agent_action_id: null,
    deferred_progress: null, request: null, receipt: null, reason: null,
  }))
  vi.spyOn(localApi, 'invalidateVoiceInteractionHandoff').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, status: 'none', handoff_id: null, agent_action_id: null,
    deferred_progress: null, request: null, receipt: null, reason: null,
  }))
  vi.spyOn(crypto.subtle, 'digest').mockImplementation(async (algorithm, data) => Uint8Array.from(createHash((typeof algorithm === 'string' ? algorithm : algorithm.name).replace('-', '').toLowerCase()).update(Buffer.from(data as ArrayBuffer)).digest()).buffer)
  vi.spyOn(localApi, 'beginObservationStream').mockImplementation(async (_session, epoch) => recordedCoverage(epoch, 'start'))
  vi.spyOn(localApi, 'endObservationStream').mockImplementation(async (_session, epoch) => recordedCoverage(epoch, 'stop'))
  vi.spyOn(localApi, 'confirmDeviceFrame').mockImplementation(async lease => ({ lease_id: lease, status: 'frame_confirmed', resolved_incidents: 0 }))
  vi.spyOn(localApi, 'agentExecutionRecoveryIndex').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, local_only: true, cloud_transmission_allowed: false,
    replay_allowed: false, has_more: false, entries: [] }))
  vi.spyOn(localApi, 'cancelTranscriptionAction').mockImplementation(async (_sid, key) => ({ client_action_id: key, status: 'cancelled' }))
  vi.spyOn(localApi, 'admitRealtimeVoice').mockImplementation(async (sid, key, text, _stamp, reference) => ({
    voice_input_id: 'canonical-' + key, client_action_id: key, session_id: sid, child_id: reference.child_id,
    original_transcript: text, status: 'completed', source_kind: 'realtime_transcript', raw_audio_available: false,
  }))
})

afterEach(async () => {
  const sessions = vi.isMockFunction(localApi.beginObservationStream) ? [...new Set(vi.mocked(localApi.beginObservationStream).mock.calls.map(([session]) => session))] : []
  const bindings = vi.isMockFunction(localApi.registerDevice) ? [...new Set(vi.mocked(localApi.registerDevice).mock.calls.filter(([, device]) => device === 'camera').map(([, , hash]) => hash))] : []
  if (vi.isMockFunction(localApi.releaseDeviceLease)) vi.mocked(localApi.releaseDeviceLease).mockImplementation(async leaseId => ({ lease_id: leaseId, status: 'released' }))
  if (vi.isMockFunction(localApi.endObservationStream)) vi.mocked(localApi.endObservationStream).mockImplementation(async (_session, epoch) => recordedCoverage(epoch, 'stop'))
  cleanup()
  await act(async () => { if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(5_100); else await Promise.resolve() })
  for (const binding of bindings) await act(async () => { await reconcileObservationRegistration(binding) })
  for (const session of sessions) await act(async () => { await reconcileObservationCoverage(session) })
  realtimeHarness.onTurn = null
  realtimeHarness.onInputCompleted = null
  realtimeHarness.props = null
  clearVoiceCaptureContext()
  localStorage.clear()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('child interaction surface', () => {
  it.each(['current', 'foreign_child', 'foreign_session', 'privacy'] as const)('speaks a fixed local recovery cue only for the current child/session: %s', async owner => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const speech = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => {})
    const agent = vi.spyOn(localApi, 'agentTurn'), admission = vi.mocked(localApi.admitRealtimeVoice)
    const view = render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.props?.onRepeatRequired).toEqual(expect.any(Function)))
    speech.mockClear(); admission.mockClear()
    if (owner === 'privacy') await act(async () => window.dispatchEvent(new CustomEvent('hsc-privacy-pause')))
    await act(async () => realtimeHarness.props?.onRepeatRequired?.({
      sessionId: owner === 'foreign_session' ? 'other-session' : workspace.session.session_id,
      childId: owner === 'foreign_child' ? 'other-child' : workspace.session.child_id }))
    if (owner === 'current') {
      expect(speech).toHaveBeenCalledWith(
        '刚才有一段声音没有识别完成。现在可以再说一次。', workspace.session.session_id,
        expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true, isCurrent: expect.any(Function) }))
      const lease = speech.mock.calls[0][4]!.isCurrent!
      expect(lease()).toBe(true); view.unmount(); expect(lease()).toBe(false)
    }
    else expect(speech).not.toHaveBeenCalled()
    expect(agent).not.toHaveBeenCalled(); expect(admission).not.toHaveBeenCalled()
  })
  it.each(['stored', 'stored_existing', 'failed', 'owner_changed', 'aborted'] as const)('hands terminal originals to durable admission without dispatch or false fallback: %s', async outcome => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const recorder = vi.spyOn(LocalInputs, 'HandsFreeVoiceInput').mockImplementation(() => <></>)
    const agent = vi.spyOn(localApi, 'agentTurn')
    if (outcome === 'failed') vi.mocked(localApi.admitRealtimeVoice).mockRejectedValueOnce(Error('storage unconfirmed'))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.props?.onPendingInputHandoff).toEqual(expect.any(Function)))
    const provider = realtimeHarness.props!, controller = new AbortController()
    const capture: QwenRealtimeInputCapture = { localInputId: 'terminal-main-input', sessionId: workspace.session.session_id,
      source: 'accepted_onset', capturedAt: Date.now(), carryoverConfirmationId: null,
      reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id) }
    if (outcome === 'owner_changed') capture.reference.child_id = 'child-primary-4'
    if (outcome === 'aborted') controller.abort()
    if (outcome === 'stored_existing') {
      await act(async () => realtimeHarness.onInputCompleted?.('保留这句原话', capture))
      await waitFor(() => expect(pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)[0]?.phase).toBe('stored'))
    }
    await act(async () => {
      provider.onStatus?.({ runtime: 'qwen_omni_realtime', phase: 'error', recoverable: false,
        speechActive: false, pendingUtterances: 1 })
      provider.onPermanentFailure?.('原话尚未处理')
    })
    expect(screen.getByText(/未处理原话的保存结果正在核对/)).toBeInTheDocument()
    recorder.mockClear()
    let stored!: boolean
    await act(async () => { stored = await provider.onPendingInputHandoff!([{ utterance: '保留这句原话', capture }], controller.signal) })
    expect(stored).toBe(outcome.startsWith('stored'))
    expect(agent).not.toHaveBeenCalled()
    if (outcome.startsWith('stored')) {
      expect(pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)).toMatchObject([
        { inputId: capture.localInputId, phase: 'stored', voiceInputId: 'canonical-' + capture.localInputId }])
      expect(localApi.admitRealtimeVoice).toHaveBeenCalledExactlyOnceWith(workspace.session.session_id,
        capture.localInputId, '保留这句原话', capture.capturedAt, capture.reference, expect.any(AbortSignal))
      await act(async () => provider.onStatus?.({ runtime: 'qwen_omni_realtime', phase: 'error', recoverable: false,
        speechActive: false, pendingUtterances: 0 }))
      await waitFor(() => expect(recorder).toHaveBeenCalled())
    } else {
      expect(recorder).not.toHaveBeenCalled()
      expect(screen.queryByText(/已回退到分段语音/)).not.toBeInTheDocument()
    }
  })

  it.each(['completed', 'prepared', 'not_found', 'cancelled', 'foreign', 'unsafe'] as const)('explicitly checks the terminal original before retry: %s', async status => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const agent = vi.spyOn(localApi, 'agentTurn'), admit = vi.mocked(localApi.admitRealtimeVoice)
    admit.mockRejectedValueOnce(Error('unconfirmed'))
    const receipt = vi.spyOn(localApi, 'realtimeAdmissionReceipt').mockResolvedValue({
      session_id: workspace.session.session_id, child_id: status === 'foreign' ? 'other-child' : workspace.session.child_id,
      client_action_id: 'terminal-retry-input', status: status === 'foreign' || status === 'unsafe' ? 'completed' : status,
      voice_input_id: ['completed', 'foreign', 'unsafe'].includes(status) ? 'canonical-terminal-retry-input' : null,
      local_only: true, cloud_transmission_allowed: false, replay_allowed: status === 'unsafe', mastery_update_allowed: false,
    } as Awaited<ReturnType<typeof localApi.realtimeAdmissionReceipt>>)
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.props?.onPendingInputHandoff).toEqual(expect.any(Function)))
    const capture: QwenRealtimeInputCapture = { localInputId: 'terminal-retry-input', sessionId: workspace.session.session_id,
      source: 'accepted_onset', capturedAt: Date.now(), carryoverConfirmationId: null,
      reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id) }
    const inputs = [{ utterance: '合成原话', capture }], handoff = realtimeHarness.props!.onPendingInputHandoff!
    const controller = new AbortController()
    let initial!: boolean, result!: boolean
    await act(async () => { initial = await handoff(inputs, controller.signal) })
    expect(initial).toBe(false)
    await act(async () => { result = await handoff(inputs, controller.signal, true) })
    expect(result).toBe(['completed', 'prepared', 'not_found'].includes(status))
    expect(receipt).toHaveBeenCalledOnce()
    expect(admit).toHaveBeenCalledTimes(['prepared', 'not_found'].includes(status) ? 2 : 1)
    expect(agent).not.toHaveBeenCalled()
    expect(pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)[0]?.phase)
      .toBe(result ? 'stored' : 'admitting')
  })

  it('coalesces concurrent explicit terminal checks under one identity', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const admit = vi.mocked(localApi.admitRealtimeVoice), agent = vi.spyOn(localApi, 'agentTurn')
    admit.mockRejectedValueOnce(Error('unconfirmed'))
    let finish!: (receipt: Awaited<ReturnType<typeof localApi.realtimeAdmissionReceipt>>) => void
    const receipt = vi.spyOn(localApi, 'realtimeAdmissionReceipt').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.props?.onPendingInputHandoff).toEqual(expect.any(Function)))
    const capture: QwenRealtimeInputCapture = { localInputId: 'terminal-single-input', sessionId: workspace.session.session_id,
      source: 'accepted_onset', capturedAt: Date.now(), carryoverConfirmationId: null,
      reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id) }
    const handoff = realtimeHarness.props!.onPendingInputHandoff!, inputs = [{ utterance: '合成原话', capture }], signal = new AbortController().signal
    await act(async () => { expect(await handoff(inputs, signal)).toBe(false) })
    let first!: Promise<boolean>, second!: Promise<boolean>
    await act(async () => { first = handoff(inputs, signal, true); second = handoff(inputs, signal, true) })
    expect(receipt).toHaveBeenCalledTimes(1)
    await act(async () => {
      finish({ session_id: workspace.session.session_id, child_id: workspace.session.child_id, client_action_id: capture.localInputId,
        status: 'not_found', voice_input_id: null, local_only: true, cloud_transmission_allowed: false,
        replay_allowed: false, mastery_update_allowed: false })
      expect(await Promise.all([first, second])).toEqual([true, true])
    })
    expect(admit).toHaveBeenCalledTimes(2); expect(agent).not.toHaveBeenCalled()
  })

  it('recovers the same terminal identity after the workspace remounts', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const admit = vi.mocked(localApi.admitRealtimeVoice), agent = vi.spyOn(localApi, 'agentTurn')
    admit.mockRejectedValueOnce(Error('unconfirmed'))
    const receipt = vi.spyOn(localApi, 'realtimeAdmissionReceipt').mockResolvedValue({
      session_id: workspace.session.session_id, child_id: workspace.session.child_id, client_action_id: 'terminal-remount-input',
      status: 'completed', voice_input_id: 'canonical-terminal-remount-input', local_only: true,
      cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false })
    const view = render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.props?.onPendingInputHandoff).toEqual(expect.any(Function)))
    const capture: QwenRealtimeInputCapture = { localInputId: 'terminal-remount-input', sessionId: workspace.session.session_id,
      source: 'accepted_onset', capturedAt: Date.now(), carryoverConfirmationId: null,
      reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id) }
    const inputs = [{ utterance: '合成原话', capture }], signal = new AbortController().signal
    await act(async () => { expect(await realtimeHarness.props!.onPendingInputHandoff!(inputs, signal)).toBe(false) })
    view.unmount(); realtimeHarness.props = null
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.props?.onPendingInputHandoff).toEqual(expect.any(Function)))
    let recovered = false
    await act(async () => { recovered = await realtimeHarness.props!.onPendingInputHandoff!(inputs, signal, true) })
    expect(recovered).toBe(true); expect(receipt).toHaveBeenCalledTimes(1)
    expect(admit).toHaveBeenCalledTimes(1); expect(agent).not.toHaveBeenCalled()
    expect(pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)[0]?.phase).toBe('stored')
  })

  it('does not resend or mark stored when privacy stops during a terminal receipt read', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const admit = vi.mocked(localApi.admitRealtimeVoice), agent = vi.spyOn(localApi, 'agentTurn')
    admit.mockRejectedValueOnce(Error('unconfirmed'))
    let finish!: (receipt: Awaited<ReturnType<typeof localApi.realtimeAdmissionReceipt>>) => void
    vi.spyOn(localApi, 'realtimeAdmissionReceipt').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.props?.onPendingInputHandoff).toEqual(expect.any(Function)))
    const capture: QwenRealtimeInputCapture = { localInputId: 'terminal-private-input', sessionId: workspace.session.session_id,
      source: 'accepted_onset', capturedAt: Date.now(), carryoverConfirmationId: null,
      reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id) }
    const handoff = realtimeHarness.props!.onPendingInputHandoff!, inputs = [{ utterance: '合成原话', capture }], signal = new AbortController().signal
    await act(async () => { expect(await handoff(inputs, signal)).toBe(false) })
    let pending!: Promise<boolean>
    await act(async () => { pending = handoff(inputs, signal, true) })
    await act(async () => window.dispatchEvent(new CustomEvent('hsc-privacy-pause')))
    let result!: boolean
    await act(async () => {
      finish({ session_id: workspace.session.session_id, child_id: workspace.session.child_id, client_action_id: capture.localInputId,
        status: 'prepared', voice_input_id: null, local_only: true, cloud_transmission_allowed: false,
        replay_allowed: false, mastery_update_allowed: false })
      result = await pending
    })
    expect(result).toBe(false); expect(admit).toHaveBeenCalledTimes(1); expect(agent).not.toHaveBeenCalled()
    expect(pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)[0]?.phase).toBe('cancel_requested')
  })

  it('preserves an unconfirmed RTC reply without silently advancing or resubmitting the original request', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const turn = { agent_turn_id: 'uncertain-reply-turn', intent: 'open_mistake_book', response_text: '原任务结果保留。',
      action: { type: 'open_mistake_book' }, tool_status: 'completed', completed_at: new Date().toISOString() } as AgentTurn
    const open = vi.fn().mockResolvedValue(undefined)
    const agent = vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} onOpenMistakeBook={open} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    const capture: QwenRealtimeInputCapture = { localInputId: 'uncertain-reply-input', capturedAt: Date.now(),
      sessionId: workspace.session.session_id, reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null }
    let result!: QwenRealtimeToolResult
    await act(async () => { result = await realtimeHarness.onTurn!('保留这个回答', capture) })
    expect(result.responseText).toBe('原任务结果保留。')
    expect(result.onPlaybackUnconfirmed).toEqual(expect.any(Function))
    await act(async () => {
      result.onPlaybackStart?.(); result.onPlaybackUnconfirmed?.()
      result.onPlaybackEnd?.(); result.onPlaybackStart?.(); result.onPlaybackUnconfirmed?.()
    })
    expect(screen.getByText('完整播报尚未核实，回复保留。')).toBeInTheDocument()
    expect(screen.getByText('原任务结果保留。')).toBeInTheDocument()
    expect(agent).toHaveBeenCalledOnce()
    expect(open).not.toHaveBeenCalled()
    expect(pendingAgentRequests(workspace.session.session_id)).toEqual([])
  })
  it('finishes the cloud tool silently, then admits its original oral prompt only after the new recorder actually delivers PCM', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }),
      getWakeWordSettings: async () => ({ childId: workspace.session.child_id, enabled: true, wakeWord: '小知', inactivitySeconds: 60 }),
      saveWakeWordSettings: async () => undefined }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    let recorderProps: React.ComponentProps<typeof LocalInputs.HandsFreeVoiceInput> | undefined
    vi.spyOn(LocalInputs, 'HandsFreeVoiceInput').mockImplementation(props => { recorderProps = props; return <></> })
    const played = vi.fn(), arbiter = new SpeechArbiter(() => undefined)
    vi.spyOn(LocalInputs, 'speakLocally').mockImplementation((message, sid, done, start, options = {}) => {
      arbiter.submit({ scope: sid!, beforePlay: options.beforePlayback, isCurrent: options.isCurrent,
        play: finish => { played(message); start?.(); done?.('system_offline'); options.onPlaybackEnd?.(); finish() } })
    })
    const result = { status: 'waiting_answer', prompt: { prompt_id: 'readiness-original', item_version: 'v1',
      child_id: workspace.session.child_id, origin_session_id: workspace.session.session_id,
      item: { track: 'pet', answer_type: 'speaking', assessment_mode: 'pet_reference_rubric',
        prompt: 'Tell me about your favourite book.', stimulus_text: null } }, attempt: null, reason: null } as DiagnosticPromptResult
    const read = vi.spyOn(localApi, 'diagnosticPrompt').mockImplementation(async sid => trackDiagnosticCapture(sid, async () => result))
    const turn = { agent_turn_id: 'readiness-turn', intent: 'start_diagnostic', response_text: '准备原题。',
      action: { type: 'start_diagnostic', track: 'pet' }, tool_status: 'pending' } as AgentTurn
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async (_id, text) => ({ ...turn, response_text: text, tool_status: 'completed' }))
    const view = render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    try {
      await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
      const provider = realtimeHarness.props!, onTurn = realtimeHarness.onTurn!
      await act(async () => {
        provider.onConversationActiveChange?.(true)
        provider.onStatus?.({ runtime: 'qwen_omni_realtime', phase: 'tool_running', speechActive: false, pendingUtterances: 1, recoverable: true })
      })
      let cloudReply!: QwenRealtimeToolResult
      await act(async () => { cloudReply = await onTurn('来一道英语口语题', { localInputId: 'readiness-input',
        sessionId: workspace.session.session_id, source: 'accepted_onset', carryoverConfirmationId: null,
        capturedAt: Date.now(), reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id) }) })
      expect(cloudReply).toMatchObject({ speak: false })
      expect(read).toHaveBeenCalled()
      expect(voiceCaptureReference(workspace.session.session_id, workspace.session.child_id).question_reference)
        .toEqual({ kind: 'diagnostic', binding: { prompt_id: result.prompt!.prompt_id, item_version: 'v1' } })
      expect(cloudReply.responseText).not.toContain('favourite book')
      expect(played).not.toHaveBeenCalledWith(result.prompt!.item.prompt)
      await act(async () => { provider.onStatus?.({ runtime: 'qwen_omni_realtime', phase: 'listening', speechActive: false, pendingUtterances: 0, recoverable: true }) })
      await waitFor(() => expect(recorderProps).toBeDefined())
      const ready = { runtime: 'segmented_local' as const, phase: 'listening' as const, speechActive: false, pendingUtterances: 0, recoverable: true }
      await act(async () => { recorderProps!.onRuntimeStatusChange?.({ ...ready, captureReady: false }) })
      expect(played).not.toHaveBeenCalledWith(result.prompt!.item.prompt)
      await act(async () => { recorderProps!.onRuntimeStatusChange?.({ ...ready, captureReady: true }) })
      await waitFor(() => expect(played).toHaveBeenCalledWith(result.prompt!.item.prompt))
      expect(played.mock.calls.filter(([text]) => text === result.prompt!.item.prompt)).toHaveLength(1)
      expect(read).toHaveBeenCalledWith(workspace.session.session_id, result.prompt!.prompt_id)
    } finally { arbiter.cancel(); view.unmount() }
  })

  it('automatically hands the exact oral task to WAV capture, waits for queue drain, and returns without another wake or click', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }),
      getWakeWordSettings: async () => ({ childId: workspace.session.child_id, enabled: true, wakeWord: '小知', inactivitySeconds: 60 }),
      saveWakeWordSettings: async () => undefined }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    let recorderProps: React.ComponentProps<typeof LocalInputs.HandsFreeVoiceInput> | undefined
    const recorder = vi.spyOn(LocalInputs, 'HandsFreeVoiceInput').mockImplementation(props => { recorderProps = props; return <></> })
    vi.spyOn(LocalInputs, 'speakLocally').mockImplementation((_text, _session, done) => { done?.('text_only') })
    const turn = { agent_turn_id: 'original-wav-answer-turn', intent: 'chat', response_text: '原回答已收到，尚未评分。',
      action: { type: 'none' }, tool_status: 'completed', completed_at: new Date().toISOString() } as AgentTurn
    const agent = vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async (_id, text) => ({ ...turn, response_text: text }))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    const provider = realtimeHarness.props!
    const listening = { runtime: 'qwen_omni_realtime' as const, phase: 'listening' as const,
      speechActive: false, pendingUtterances: 0, recoverable: true }
    await act(async () => {
      provider.onConversationActiveChange?.(true)
      provider.onStatus?.({ ...listening, pendingUtterances: 1 })
    })
    const prompt = { prompt_id: 'current-original-oral', item_version: 'original-v1',
      child_id: workspace.session.child_id, origin_session_id: workspace.session.session_id,
      item: { track: 'pet', answer_type: 'speaking', assessment_mode: 'pet_reference_rubric' } } as NonNullable<DiagnosticPromptResult['prompt']>
    await act(async () => { await trackDiagnosticCapture(workspace.session.session_id, async () => ({
      status: 'waiting_answer', prompt, attempt: null, reason: null,
    })) })
    expect(recorder).not.toHaveBeenCalled()
    await act(async () => { provider.onStatus?.(listening) })
    await waitFor(() => expect(recorderProps).toBeDefined())
    expect(recorderProps).toMatchObject({ autoStart: true, initialConversationActive: true,
      sessionId: workspace.session.session_id, childId: workspace.session.child_id, purpose: 'agent_command' })
    expect(voiceCaptureReference(workspace.session.session_id, workspace.session.child_id).question_reference)
      .toEqual({ kind: 'diagnostic', binding: { prompt_id: prompt.prompt_id, item_version: prompt.item_version } })
    await act(async () => { await recorderProps!.onTranscript('I enjoy reading books.', undefined, 'original-wav-voice') })
    await waitFor(() => expect(agent).toHaveBeenCalledTimes(1))
    expect(agent.mock.calls[0][3]).toBe('original-wav-voice')
    expect(localApi.admitRealtimeVoice).not.toHaveBeenCalled()
    await act(async () => { await trackDiagnosticCapture(workspace.session.session_id, async () => ({
      status: 'answer_saved', prompt, attempt: null, reason: null,
    })) })
    await waitFor(() => expect(realtimeHarness.props).not.toBe(provider))
    expect(realtimeHarness.props?.initialConversationActive).toBe(true)
  })

  it('resumes a provably unsent original through the existing Runtime execution instead of interpreting the new recovery words', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const item = updateRealtimeAdmission(prepareRealtimeAdmission(workspace.session.session_id, workspace.session.child_id,
      'recover-original-input'), 'stored', 'recover-original-voice')
    const read = vi.spyOn(localApi, 'realtimeResumePreview').mockResolvedValue({
      session_id: item.sessionId, child_id: item.childId, input_id: item.inputId, voice_input_id: item.voiceInputId,
      status: 'ready', resume_allowed: true, response_text: '原话已保存且未发送。', local_only: true,
      cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false, raw_audio_available: false,
    })
    const old = { session_id: item.sessionId, child_id: item.childId, agent_turn_id: 'resumed-original-turn',
      transcript: '原来那句话完整保留', intent: 'chat', response_text: '原话已经处理。',
      action: { type: 'none', voice_input_id: item.voiceInputId }, tool_status: 'completed', completed_at: new Date().toISOString() } as AgentTurn
    const resume = vi.spyOn(localApi, 'resumeRealtimeVoice').mockResolvedValue(old)
    const agent = vi.spyOn(localApi, 'agentTurn').mockRejectedValue(Error('No new interpretation'))
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async (_id, text) => ({ ...old, response_text: text }))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    await act(async () => { await realtimeHarness.onTurn!('继续刚才未处理的原话', {
      localInputId: 'fresh-resume-control', sessionId: item.sessionId, capturedAt: Date.now(),
      reference: voiceCaptureReference(item.sessionId, item.childId), source: 'accepted_onset', carryoverConfirmationId: null,
    }) })
    expect(read).toHaveBeenCalledExactlyOnceWith(item.sessionId, item.childId, item.inputId, expect.any(AbortSignal))
    expect(resume).toHaveBeenCalledExactlyOnceWith(item.sessionId, item.childId, item.inputId, item.voiceInputId, expect.any(AbortSignal))
    expect(agent).not.toHaveBeenCalled()
    expect(pendingRealtimeAdmissions(item.sessionId, item.childId)).toEqual([])
  })

  it('restores a pending privacy cancellation on mount without reopening audio or executing a task', async () => {
    const item = prepareRealtimeAdmission(workspace.session.session_id, workspace.session.child_id, 'restart-cancelled-input')
    updateRealtimeAdmission(item, 'cancel_requested')
    localStorage.setItem(privacyStopKey(workspace.session.session_id), 'stopped')
    vi.spyOn(localApi, 'pauseObservation').mockResolvedValue({ ...workspace.session, observation_status: 'paused' })
    const cancel = vi.spyOn(localApi, 'cancelTranscriptionAction').mockResolvedValue({ client_action_id: item.inputId, status: 'cancelled' })
    const read = vi.spyOn(localApi, 'realtimeAdmissionReceipt').mockResolvedValue({
      session_id: item.sessionId, child_id: item.childId, client_action_id: item.inputId,
      status: 'cancelled', voice_input_id: null, local_only: true, cloud_transmission_allowed: false,
      replay_allowed: false, mastery_update_allowed: false,
    })
    const agent = vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new Error('No replay'))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(pendingRealtimeAdmissions(item.sessionId, item.childId)).toEqual([]))
    expect(read).toHaveBeenCalledTimes(1)
    expect(agent).not.toHaveBeenCalled()
    expect(localStorage.getItem(privacyStopKey(item.sessionId))).toBe('stopped')
  })

  it('leaves a cancellation notice intact when its workspace exits before the receipt', async () => {
    const item = updateRealtimeAdmission(prepareRealtimeAdmission(workspace.session.session_id, workspace.session.child_id, 'exit-cancelled-input'), 'cancel_requested')
    vi.spyOn(localApi, 'cancelTranscriptionAction').mockResolvedValue({ client_action_id: item.inputId, status: 'cancelled' })
    let finish!: (value: Awaited<ReturnType<typeof localApi.realtimeAdmissionReceipt>>) => void
    const read = vi.spyOn(localApi, 'realtimeAdmissionReceipt').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const view = render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1))
    view.unmount()
    await act(async () => { finish({ session_id: item.sessionId, child_id: item.childId, client_action_id: item.inputId,
      status: 'cancelled', voice_input_id: null, local_only: true, cloud_transmission_allowed: false,
      replay_allowed: false, mastery_update_allowed: false }) })
    expect(pendingRealtimeAdmissions(item.sessionId, item.childId)).toEqual([item])
  })

  it('admits completed ASR before provider tool selection and reuses that receipt exactly once', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const turn = { agent_turn_id: 'completed-asr-turn', intent: 'chat', response_text: '原题已接纳。',
      action: { type: 'none' }, tool_status: 'completed', completed_at: new Date().toISOString() } as AgentTurn
    const agent = vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async (_id, text) => ({ ...turn, response_text: text }))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onInputCompleted).toEqual(expect.any(Function)))
    const capture: QwenRealtimeInputCapture = { localInputId: 'completed-asr-input', capturedAt: 100,
      sessionId: workspace.session.session_id, reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null }
    await act(async () => { realtimeHarness.onInputCompleted!('小知，原题我选B。', capture) })
    expect(localApi.admitRealtimeVoice).toHaveBeenCalledTimes(1)
    expect(agent).not.toHaveBeenCalled()
    expect(pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)[0]).toMatchObject({
      phase: 'stored', voiceInputId: 'canonical-completed-asr-input',
    })
    await act(async () => { await realtimeHarness.onTurn!('小知，原题我选B。', structuredClone(capture)) })
    expect(localApi.admitRealtimeVoice).toHaveBeenCalledTimes(1)
    expect(agent).toHaveBeenCalledTimes(1)
    expect(agent.mock.calls[0]).toEqual([workspace.session.session_id, '小知，原题我选B。', undefined,
      'canonical-completed-asr-input', undefined, undefined, 'realtime-canonical-completed-asr-input'])
    expect(pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)).toEqual([])
  })

  it.each(['AGENT_HANDOFF_UNCONFIRMED', 'AGENT_HANDOFF_SOURCE_CONFLICT'])('keeps %s original input and never asks for replay while allowing new conversation', async code => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const turn = { agent_turn_id: 'fresh-handoff-turn', intent: 'chat', response_text: '新问题可以继续。',
      action: { type: 'none' }, tool_status: 'completed', completed_at: new Date().toISOString() } as AgentTurn
    const agent = vi.spyOn(localApi, 'agentTurn').mockImplementationOnce(async (sid, _text, _overlap, _voice, _evaluation, _local, key) => {
      saveAgentRequest(sid, key!)
      throw new ApiError('原交接需要核对。', 409, code)
    }).mockResolvedValue(turn)
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async (_id, text) => ({ ...turn, response_text: text }))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    const capture: QwenRealtimeInputCapture = { localInputId: 'unknown-handoff-input', capturedAt: 100,
      sessionId: workspace.session.session_id, reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null }
    let result: QwenRealtimeToolResult | undefined
    await act(async () => { result = await realtimeHarness.onTurn!('合成原答案B', capture) })
    expect(pendingAgentRequests(workspace.session.session_id)).toHaveLength(1)
    expect(pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)).toMatchObject([
      { inputId: capture.localInputId, phase: 'dispatching', voiceInputId: 'canonical-unknown-handoff-input' },
    ])
    expect(result).toMatchObject({ speak: false })
    expect(screen.queryByText(/你可以再试一次|请再说一次/)).not.toBeInTheDocument()
    expect(screen.getAllByText(/交接/).some(item => item.textContent?.includes('不会自动重做'))).toBe(true)
    expect(complete).not.toHaveBeenCalled()
    await act(async () => { await realtimeHarness.onTurn!('我现在想问一个新问题', { ...capture, localInputId: 'fresh-handoff-input' }) })
    expect(agent).toHaveBeenCalledTimes(2)
    expect(agent.mock.calls[1][3]).toBe('canonical-fresh-handoff-input')
    expect(pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)).toHaveLength(1)
  })

  it('recovers a saved but undispatched ASR after workspace restart by voice and readonly receipt, without executing it', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const agent = vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new Error('Must never run the saved command'))
    const original = render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onInputCompleted).toEqual(expect.any(Function)))
    const capture: QwenRealtimeInputCapture = { localInputId: 'restart-completed-input', capturedAt: 100,
      sessionId: workspace.session.session_id, reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null }
    await act(async () => { realtimeHarness.onInputCompleted!('请保存我的原题作答', capture) })
    const originalTurn = realtimeHarness.onTurn
    original.unmount()
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => {
      expect(realtimeHarness.onTurn).toEqual(expect.any(Function))
      expect(realtimeHarness.onTurn).not.toBe(originalTurn)
    })
    expect(screen.getByText(/上次有原话的保存或交接结果待核对/)).toBeInTheDocument()
    const read = vi.spyOn(localApi, 'realtimeAdmissionReceipt').mockResolvedValue({
      session_id: workspace.session.session_id, child_id: workspace.session.child_id, client_action_id: capture.localInputId,
      status: 'completed', voice_input_id: 'canonical-restart-completed-input', local_only: true,
      cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false,
    })
    const readAgent = vi.spyOn(localApi, 'agentRequestReceipt').mockResolvedValue({
      session_id: workspace.session.session_id, client_action_id: 'realtime-canonical-restart-completed-input', status: 'unconfirmed',
      response_text: '原交接尚未确认，不会自动重做。', local_only: true,
      cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false,
    })
    expect(localStorage.getItem(privacyStopKey(workspace.session.session_id))).toBeNull()
    const blockedStorage = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {})
    let recoveryResult: QwenRealtimeToolResult | undefined
    await act(async () => { recoveryResult = await realtimeHarness.onTurn!('核对刚才请求', {
      ...capture, localInputId: 'new-voice-status-check', reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
    }) })
    blockedStorage.mockRestore()
    expect(recoveryResult).not.toMatchObject({ responseText: 'input_cancelled' })
    expect(read).toHaveBeenCalledExactlyOnceWith(workspace.session.session_id, capture.localInputId, expect.any(AbortSignal))
    expect(readAgent).toHaveBeenCalledExactlyOnceWith(workspace.session.session_id, 'realtime-canonical-restart-completed-input', expect.any(AbortSignal))
    expect(agent).not.toHaveBeenCalled()
    expect(localApi.admitRealtimeVoice).toHaveBeenCalledTimes(1)
    expect(pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)).toMatchObject([
      { inputId: capture.localInputId, phase: 'stored', voiceInputId: 'canonical-restart-completed-input' },
    ])
  })

  it.each(['deadline', 'privacy'] as const)('releases a hung readonly voice check on %s without replaying or acknowledging the original', async reason => {
    const diagnostics = vi.fn().mockResolvedValue({ recorded: true })
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ recordRendererDiagnostic: diagnostics,
      getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
        endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const turn = { agent_turn_id: 'new-conversation-after-recovery', intent: 'chat', response_text: '我们继续聊。',
      action: { type: 'none' }, tool_status: 'completed', completed_at: new Date().toISOString() } as AgentTurn
    const agent = vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async (_id, text) => ({ ...turn, response_text: text }))
    let finish!: (value: Awaited<ReturnType<typeof localApi.agentRequestReceipt>>) => void
    const read = vi.spyOn(localApi, 'agentRequestReceipt').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    const original = saveAgentRequest(workspace.session.session_id, 'hung-original-request')
    const capture: QwenRealtimeInputCapture = { localInputId: 'bounded-voice-status-check', capturedAt: 100,
      sessionId: workspace.session.session_id, reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null }
    vi.useFakeTimers()
    let checked!: Promise<QwenRealtimeToolResult>
    await act(async () => { checked = realtimeHarness.onTurn!('核对刚才请求', capture) })
    expect(read).toHaveBeenCalledTimes(1)
    if (reason === 'deadline') {
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
      await checked
      expect(screen.getByText(/原请求核对没有全部完成/)).toBeInTheDocument()
      expect(diagnostics).toHaveBeenCalledWith(expect.objectContaining({
        event: 'voice.recovery.receipt_check_incomplete', details: { reason: 'deadline' },
      }))
      const next = { ...capture, localInputId: 'fresh-conversation-input',
        reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id) }
      await act(async () => { await realtimeHarness.onTurn!('我们继续聊', next) })
      expect(agent).toHaveBeenCalledTimes(1)
      expect(agent.mock.calls[0][1]).toBe('我们继续聊')
    } else {
      await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
      expect(await checked).toMatchObject({ speak: false })
      expect(agent).not.toHaveBeenCalled()
    }
    expect(read.mock.calls[0][2]?.aborted).toBe(true)
    finish({ session_id: original.sessionId, client_action_id: original.actionId, status: 'recorded_completed',
      response_text: '原请求已完成。', local_only: true, cloud_transmission_allowed: false,
      replay_allowed: false, mastery_update_allowed: false })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(pendingAgentRequests(original.sessionId)).toEqual([original])
    expect(screen.queryByText('原请求已完成。')).not.toBeInTheDocument()
    expect(JSON.stringify(diagnostics.mock.calls)).not.toContain(original.actionId)
  })

  it('does not admit an input without durable handoff storage and persists cancellation before provider dispatch', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const agent = vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new Error('Must not execute'))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onInputCompleted).toEqual(expect.any(Function)))
    const capture: QwenRealtimeInputCapture = { localInputId: 'storage-blocked-input', capturedAt: 100,
      sessionId: workspace.session.session_id, reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null }
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {})
    await act(async () => { realtimeHarness.onInputCompleted!('原话不应发出', capture) })
    expect(localApi.admitRealtimeVoice).not.toHaveBeenCalled()
    setItem.mockRestore()
    await act(async () => { realtimeHarness.onInputCompleted!('这句话等待工具回调', { ...capture, localInputId: 'waiting-final-input' }) })
    await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
    expect(localApi.cancelTranscriptionAction).toHaveBeenCalledWith(workspace.session.session_id, 'waiting-final-input')
    expect(pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)[0].phase).toBe('cancel_requested')
    expect(agent).not.toHaveBeenCalled()
  })

  it('keeps realtime callbacks from a replacement connection ordered behind the active runtime turn', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const turn = { agent_turn_id: 'first-realtime-turn', intent: 'chat', response_text: '第一句的回复。',
      action: { type: 'none' }, tool_status: 'completed', completed_at: new Date().toISOString() } as AgentTurn
    let finish!: (value: AgentTurn) => void
    const agent = vi.spyOn(localApi, 'agentTurn').mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
      .mockResolvedValue({ ...turn, agent_turn_id: 'second-realtime-turn', response_text: '第二句的回复。' })
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async (_id, text) => ({ ...turn, response_text: text }))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    const capture: QwenRealtimeInputCapture = { localInputId: 'first-input', capturedAt: 100,
      sessionId: workspace.session.session_id, reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null }
    let first!: Promise<QwenRealtimeToolResult>
    let settled!: Promise<{ result?: QwenRealtimeToolResult; error?: unknown }>
    await act(async () => { first = realtimeHarness.onTurn!('第一句', capture) })
    await waitFor(() => expect(agent).toHaveBeenCalledTimes(1))
    await act(async () => { settled = realtimeHarness.onTurn!('第二句', { ...capture, localInputId: 'second-input' })
      .then(result => ({ result }), error => ({ error })) })
    expect(localApi.admitRealtimeVoice).toHaveBeenCalledTimes(2)
    expect(agent).toHaveBeenCalledTimes(1)
    let firstResult: QwenRealtimeToolResult | undefined, secondResult: Awaited<typeof settled> | undefined
    await act(async () => { finish(turn); [firstResult, secondResult] = await Promise.all([first, settled]) })
    expect(secondResult?.error).toBeUndefined()
    expect(agent.mock.calls.map(call => call[1])).toEqual(['第一句', '第二句'])
    expect([firstResult?.responseText, secondResult?.result?.responseText]).toEqual(['第一句的回复。', '第二句的回复。'])
  })

  it('preempts privacy even while a previous realtime callback owns a runtime turn', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const turn = { agent_turn_id: 'privacy-active-turn', intent: 'chat', response_text: '旧回复不能播出。',
      action: { type: 'none' }, tool_status: 'completed', completed_at: new Date().toISOString() } as AgentTurn
    let finish!: (value: AgentTurn) => void
    const agent = vi.spyOn(localApi, 'agentTurn').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const pause = vi.spyOn(localApi, 'pauseObservation').mockResolvedValue(workspace.session)
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    const capture: QwenRealtimeInputCapture = { localInputId: 'privacy-input', capturedAt: 100,
      sessionId: workspace.session.session_id, reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null }
    let first!: Promise<QwenRealtimeToolResult>, waiting!: Promise<QwenRealtimeToolResult>
    let privacy: QwenRealtimeToolResult | undefined, error: unknown
    await act(async () => { first = realtimeHarness.onTurn!('第一句', capture) })
    await waitFor(() => expect(agent).toHaveBeenCalledTimes(1))
    await act(async () => { waiting = realtimeHarness.onTurn!('第二句', { ...capture, localInputId: 'waiting-input' }) })
    await act(async () => { try { privacy = await realtimeHarness.onTurn!('关闭摄像头和麦克风') } catch (cause) { error = cause } })
    let cancelled: QwenRealtimeToolResult[] = []
    await act(async () => { finish(turn); cancelled = await Promise.all([first, waiting]) })
    expect(error).toBeUndefined()
    expect(privacy).toMatchObject({ speak: false })
    expect(pause).toHaveBeenCalledTimes(1)
    expect(agent).toHaveBeenCalledTimes(1)
    expect(cancelled.every(result => result.speak === false)).toBe(true)
    expect(localStorage.getItem(privacyStopKey(workspace.session.session_id))).toBe('stopped')
  })

  it('rechecks an input source after waiting for an older realtime runtime turn', async () => {
    const diagnostics = vi.fn().mockResolvedValue({ recorded: true })
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ recordRendererDiagnostic: diagnostics, getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), getVoices: () => [offlineChineseVoice], speak: vi.fn() })
    const turn = { agent_turn_id: 'first-source-turn', intent: 'chat', response_text: '第一句完成。',
      action: { type: 'none' }, tool_status: 'completed', completed_at: new Date().toISOString() } as AgentTurn
    let finish!: (value: AgentTurn) => void
    const agent = vi.spyOn(localApi, 'agentTurn').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
      { kind: 'diagnostic', binding: { prompt_id: 'original', item_version: 'v1' } })
    const capture: QwenRealtimeInputCapture = { localInputId: 'first-source', capturedAt: 100,
      sessionId: workspace.session.session_id, reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null }
    let first!: Promise<QwenRealtimeToolResult>, waiting!: Promise<QwenRealtimeToolResult>
    await act(async () => { first = realtimeHarness.onTurn!('第一句', capture) })
    await waitFor(() => expect(agent).toHaveBeenCalledTimes(1))
    await act(async () => { waiting = realtimeHarness.onTurn!('原题选B', { ...capture, localInputId: 'second-source' }) })
    setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
      { kind: 'diagnostic', binding: { prompt_id: 'different', item_version: 'v1' } })
    let held: QwenRealtimeToolResult | undefined
    await act(async () => { finish(turn); [, held] = await Promise.all([first, waiting]) })
    expect(agent).toHaveBeenCalledTimes(1)
    expect(held).toMatchObject({ speak: false })
    expect(diagnostics).toHaveBeenCalledWith(expect.objectContaining({ event: 'voice.realtime.input_source_checked',
      details: { input_stage: 'dispatch', source_status: 'source_changed' } }))
    const sourceEvents = diagnostics.mock.calls.map(call => call[0]).filter(x => x.event === 'voice.realtime.input_source_checked')
    expect(JSON.stringify(sourceEvents)).not.toContain('原题选B')
    expect(JSON.stringify(sourceEvents)).not.toContain('second-source')
  })

  it('submits an unchanged accepted realtime source through the existing runtime', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const turn = { agent_turn_id: 'realtime-current-source', intent: 'chat', response_text: '一起核对这道题。',
      action: { type: 'none' }, tool_status: 'completed', completed_at: new Date().toISOString() } as AgentTurn
    const agent = vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    const admit = vi.spyOn(localApi, 'admitRealtimeVoice').mockResolvedValue({ voice_input_id: 'canonical-realtime-voice',
      client_action_id: 'synthetic-input', child_id: workspace.session.child_id, session_id: workspace.session.session_id,
      status: 'completed', source_kind: 'realtime_transcript', raw_audio_available: false,
      original_transcript: '这道题选B' })
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
      { kind: 'diagnostic', binding: { prompt_id: 'original', item_version: 'v1' } })
    const capture: QwenRealtimeInputCapture = { localInputId: 'synthetic-input', capturedAt: 100,
      sessionId: workspace.session.session_id, reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null }
    let result: QwenRealtimeToolResult | undefined
    await act(async () => { result = await realtimeHarness.onTurn?.('这道题选B', capture) })
    expect(agent).toHaveBeenCalledTimes(1)
    expect(admit).toHaveBeenCalledWith(workspace.session.session_id, 'synthetic-input', '这道题选B', 100,
      capture.reference, expect.any(AbortSignal))
    expect(agent.mock.calls[0][3]).toBe('canonical-realtime-voice')
    expect(agent.mock.calls[0].slice(0, 2)).toEqual([workspace.session.session_id, '这道题选B'])
    expect(result?.responseText).toBe('一起核对这道题。')
  })

  it.each(['changed', 'unbound', 'missing', 'owner'] as const)('does not submit a realtime %s source as the current answer', async mode => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), getVoices: () => [offlineChineseVoice], speak: vi.fn() })
    const agent = vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new Error('must not submit a different source'))
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
      { kind: 'diagnostic', binding: { prompt_id: 'original', item_version: 'v1' } })
    const capture: QwenRealtimeInputCapture = { localInputId: 'synthetic-input', capturedAt: 100,
      sessionId: workspace.session.session_id, reference: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null }
    if (mode === 'changed') setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
      { kind: 'diagnostic', binding: { prompt_id: 'new-question', item_version: 'v1' } })
    if (mode === 'unbound') capture.source = 'unbound'
    if (mode === 'owner') capture.reference.child_id = 'other-child'
    let result: QwenRealtimeToolResult | undefined
    await act(async () => { result = await realtimeHarness.onTurn?.('这道题选B', mode === 'missing' ? undefined : capture) })
    expect(agent).not.toHaveBeenCalled()
    expect(result).toMatchObject({ speak: false })
  })

  it('still preempts privacy locally when realtime question ownership is missing', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const agent = vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new Error('privacy must remain local'))
    const pause = vi.spyOn(localApi, 'pauseObservation').mockResolvedValue(workspace.session)
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    await act(async () => { await realtimeHarness.onTurn?.('关闭摄像头和麦克风') })
    expect(agent).not.toHaveBeenCalled()
    expect(pause).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem(privacyStopKey(workspace.session.session_id))).toBe('stopped')
  })

  it.each([
    ['active', 'answered'], ['paused', 'answered'], ['paused', 'answer_saved'], ['paused', 'waiting_answer'],
    ['paused', 'reviewed'], ['paused', 'mismatched_review'],
  ] as const)('restores a %s school review with %s original without replaying commands', async (status, answerStatus) => {
    const initial: SessionWorkspaceState = { ...studyWorkspace, plan_items: [{ ...studyWorkspace.plan_items[0],
      status, source_type: 'due_review', source_ref: 'school-review:school', started_at: new Date().toISOString() }] }
    const item = initial.plan_items[0]
    const focus = vi.fn()
    const originalFocus = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: focus })
    const attempt = { attempt_id: 'written-attempt', evidence_id: 'written-evidence', child_id: initial.session.child_id,
      session_id: initial.session.session_id, diagnostic_item_id: 'school-variant', review_task_id: item.source_ref,
      correctness: null, submitted_answer: '书面解答', feedback: '原稿已保存' }
    const reviewed = ['reviewed', 'mismatched_review'].includes(answerStatus)
    vi.spyOn(localApi, 'planReviewQuestion').mockResolvedValue({ plan_item_id: item.plan_item_id, child_id: initial.session.child_id,
      session_id: initial.session.session_id, diagnostic_item_id: 'school-variant', review_task_id: item.source_ref,
      review_interval: 'school_open_near', review_status: reviewed ? 'completed' : 'pending', attempt: reviewed ? attempt : null, track: 'school', subject: '数学',
      title: '独立变式', prompt: '给出相似关系的依据', assessment_mode: 'open_response_rubric' } as never)
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({ status: reviewed ? 'answered' : answerStatus,
      attempt: answerStatus === 'mismatched_review' ? { ...attempt, attempt_id: 'different-attempt' }
        : reviewed || answerStatus === 'answered' ? attempt : null, prompt: {
      prompt_id: 'original-school-prompt', item_version: 'original-version', child_id: initial.session.child_id,
      origin_session_id: initial.session.session_id, diagnostic_item_id: 'school-variant', review_task_id: item.source_ref,
      item: { diagnostic_item_id: 'school-variant', prompt: '给出相似关系的依据', assessment_mode: 'open_response_rubric' },
    } } as never)
    const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt')
    const answer = vi.spyOn(localApi, 'answerDiagnostic')
    const complete = vi.spyOn(localApi, 'completePlanItem')
    const speech = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => undefined)
    render(<SessionWorkspace initial={initial} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await screen.findByText(answerStatus === 'mismatched_review'
      ? '暂停复习的评分与原回答尚未核对一致，原记录保留，未确认任务完成。'
      : answerStatus === 'reviewed' ? '这次复习的原稿和评分已保存。任务仍暂停，可以说“继续原任务”。'
      : answerStatus === 'waiting_answer'
      ? '原来的复习题已保留，任务仍暂停。准备好后说“继续原任务”。'
      : '这道复测的原稿已经保存，评分还需要核对，不用重复作答。核对后会接着安排后续练习。')
    expect(prepare).not.toHaveBeenCalled(); expect(answer).not.toHaveBeenCalled(); expect(complete).not.toHaveBeenCalled()
    expect(speech).not.toHaveBeenCalled()
    if (answerStatus === 'waiting_answer') {
      await waitFor(() => expect(focus.mock.instances.some(element =>
        (element as Element | undefined)?.getAttribute('data-question-viewport') === 'diagnostic')).toBe(true))
    }
    if (originalFocus) Object.defineProperty(Element.prototype, 'scrollIntoView', originalFocus)
    else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  })
  it('keeps short readiness phrases inside the active camera workflow', () => {
    expect(isCaptureReadinessAcknowledgement('放好了')).toBe(true)
    expect(isCaptureReadinessAcknowledgement('来了')).toBe(true)
    expect(isCaptureReadinessAcknowledgement('取消拍题')).toBe(false)
    expect(isCaptureReadinessAcknowledgement('这道题我不会')).toBe(false)
  })
  it('keeps voice and the document camera in the workspace without mounting observation for an enabled old profile', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const oldProfile: SessionWorkspaceState = { ...workspace, session: { ...workspace.session, observation_status: 'enabled' } }
    const { container } = render(<SessionWorkspace initial={oldProfile} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    const surface = container.querySelector('[data-interaction-mode="voice-only"]')
    expect(surface).toHaveClass('child-voice-only')
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    expect(surface?.querySelector('[aria-label="对学习伙伴说"]')).toBeInTheDocument()
    const companion = surface?.querySelector('[aria-label="语音学习伙伴"]')
    const deviceView = screen.getByLabelText('高拍仪工作区')
    expect(companion && deviceView && Boolean(companion.compareDocumentPosition(deviceView) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true)
    expect(screen.queryByLabelText('观察摄像头实时画面')).not.toBeInTheDocument()
    expect(screen.getByLabelText('高拍仪实时画面')).toBeInTheDocument()
  })

  it('docks the companion without remounting its voice controls and restores keyboard focus', async () => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const speak = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => {})
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const panel = screen.getByRole('complementary', { name: '语音学习伙伴' })
    const collapse = screen.getByRole('button', { name: '收起学习伙伴' })
    await waitFor(() => expect(realtimeHarness.props?.onRepeatRequired).toEqual(expect.any(Function)))
    const controls = panel.querySelector('.agent-voice-controls')

    fireEvent.click(collapse)

    expect(panel).toHaveAttribute('hidden')
    expect(panel.querySelector('.agent-voice-controls')).toBe(controls)
    const expand = screen.getByRole('button', { name: '展开学习伙伴' })
    expect(expand).toHaveAttribute('aria-controls', panel.id)
    expect(expand).toHaveFocus()
    await act(async () => realtimeHarness.props?.onRepeatRequired?.({ sessionId: workspace.session.session_id, childId: workspace.session.child_id }))
    expect(speak).toHaveBeenCalledWith(expect.stringContaining('现在可以再说一次'), workspace.session.session_id,
      expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))

    fireEvent.click(expand)

    expect(panel).not.toHaveAttribute('hidden')
    expect(panel.querySelector('.agent-voice-controls')).toBe(controls)
    expect(collapse).toHaveFocus()
  })
})

describe('continuous document evidence bridge', () => {
  it('bounds read-only checks to the original pending turn and blocks retry from creating a new answer', async () => {
    vi.useFakeTimers()
    const receipt: PendingEvaluationReceipt = { version: 1, sessionId: workspace.session.session_id, childId: workspace.session.child_id, turnId: 'original-evaluation', status: 'in_progress', silent: false }
    savePendingEvaluation(receipt)
    const status = vi.spyOn(localApi, 'agentTurnExecutionStatus').mockResolvedValue({ status: 'in_progress' })
    const agent = vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new ApiError('restricted dispatch unavailable', 409, 'PENDING_EVALUATION_CONTROL_SCOPE_CHANGED'))
    const execute = vi.spyOn(localApi, 'executeAgentTurn')
    const complete = vi.spyOn(localApi, 'completeAgentTurn')
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
    expect(status).toHaveBeenCalledTimes(4)
    expect(status.mock.calls.every(([id]) => id === 'original-evaluation')).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(status).toHaveBeenCalledTimes(4)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '重试' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(agent).toHaveBeenCalledExactlyOnceWith(receipt.sessionId, '重试', undefined, undefined, receipt.turnId)
    expect(execute).not.toHaveBeenCalled()
    expect(complete).not.toHaveBeenCalled()
    expect(readPendingEvaluation(receipt.sessionId, receipt.childId)?.turnId).toBe(receipt.turnId)
  })

  it.each(['current', 'obsolete', 'other_session', 'missing_session', 'other_child'] as const)('recovers completed evaluation through GET only (%s)', async (scope) => {
    vi.useFakeTimers()
    const receipt: PendingEvaluationReceipt = { version: 1, sessionId: workspace.session.session_id, childId: workspace.session.child_id, turnId: 'restored-evaluation', status: 'in_progress', silent: false }
    savePendingEvaluation(receipt)
    const turn = {
      agent_turn_id: receipt.turnId, session_id: scope === 'missing_session' ? undefined : scope === 'other_session' ? 'other-session' : receipt.sessionId,
      child_id: scope === 'other_child' ? 'other-child' : receipt.childId,
      intent: 'learning.evaluate_response', response_text: '旧的待处理回复', action: { type: 'none' }, tool_status: 'pending', completed_at: null,
      transcript: '原作答', orchestrator_version: 'agent_runtime_deterministic', tool_error_code: null, created_at: new Date().toISOString(),
    } as AgentTurn
    const evaluatedResponse = '已持久化评价的追问'
    const status = vi.spyOn(localApi, 'agentTurnExecutionStatus').mockResolvedValue({ status: 'completed', execution: { turn, execution: { capability: 'learning.evaluate_response', response_text: evaluatedResponse, result: { objective_update_applied: scope !== 'obsolete' }, evidence: ['independent_response'] } } })
    const execute = vi.spyOn(localApi, 'executeAgentTurn')
    const complete = vi.spyOn(localApi, 'completeAgentTurn')
    const speak = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => undefined)
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(status).toHaveBeenCalledWith(receipt.turnId)
    expect(execute).not.toHaveBeenCalled()
    expect(complete).not.toHaveBeenCalled()
    if (scope === 'current') {
      expect(screen.getByText(evaluatedResponse)).toBeInTheDocument()
      expect(screen.queryByText(turn.response_text)).not.toBeInTheDocument()
      expect(speak).toHaveBeenCalledWith(evaluatedResponse, receipt.sessionId, expect.any(Function), expect.any(Function), expect.objectContaining({ sourceAgentTurnId: receipt.turnId }))
      expect(readPendingEvaluation(receipt.sessionId, receipt.childId)).toBeNull()
    } else {
      expect(speak).not.toHaveBeenCalled()
      expect(screen.queryByText(turn.response_text)).not.toBeInTheDocument()
      expect(screen.queryByText(evaluatedResponse)).not.toBeInTheDocument()
      if (scope !== 'obsolete') expect(readPendingEvaluation(receipt.sessionId, receipt.childId)?.status).toBe('unknown')
    }
  })

  it('restores an unknown outcome after refresh and checks the same turn without resubmitting', async () => {
    vi.useFakeTimers()
    const receipt: PendingEvaluationReceipt = { version: 1, sessionId: workspace.session.session_id, childId: workspace.session.child_id, turnId: 'unknown-original', status: 'unknown', silent: false }
    savePendingEvaluation(receipt)
    const status = vi.spyOn(localApi, 'agentTurnExecutionStatus').mockResolvedValue({ status: 'unknown' })
    const agent = vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new ApiError('restricted dispatch unavailable', 409, 'PENDING_EVALUATION_CONTROL_SCOPE_CHANGED'))
    const execute = vi.spyOn(localApi, 'executeAgentTurn')
    const first = render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
    expect(status).toHaveBeenCalledTimes(1)
    first.unmount()
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(status.mock.calls).toEqual([[receipt.turnId], [receipt.turnId]])
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '再试一次' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByText(/不会把重试作为新作答/)).toBeInTheDocument()
    fireEvent.change(input, { target: { value: '核对评价状态' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(status).toHaveBeenLastCalledWith(receipt.turnId)
    expect(agent).toHaveBeenCalledExactlyOnceWith(receipt.sessionId, '再试一次', undefined, undefined, receipt.turnId)
    expect(execute).not.toHaveBeenCalled()
  })

  it.each(['LEARNING_EVALUATION_IN_PROGRESS', 'LEARNING_EVALUATION_RESULT_UNKNOWN'])('preserves runtime evidence and does not retry or overwrite %s', async (code) => {
    const turn = { agent_turn_id: 'evaluation-pending', intent: 'learning.evaluate_response', action: { type: 'ask_tutor', source: 'agent_runtime_deterministic', capability: 'learning.evaluate_response' } } as AgentTurn
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    const execute = vi.spyOn(localApi, 'executeAgentTurn').mockImplementation(async (turnId) => {
      expect(readPendingEvaluation(workspace.session.session_id, workspace.session.child_id)?.turnId).toBe(turnId)
      throw new ApiError('评价未确认', 409, code)
    })
    const complete = vi.spyOn(localApi, 'completeAgentTurn')
    const askTutor = vi.spyOn(localApi, 'askTutor')
    const clear = vi.spyOn(localApi, 'clearSessionDraft')
    const speak = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => undefined)
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '这是我的原回答' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.getByText(/原回答与证据已保留/)).toBeInTheDocument())
    expect(input).toHaveValue('这是我的原回答')
    expect(execute).toHaveBeenCalledTimes(1)
    expect(complete).not.toHaveBeenCalled()
    expect(askTutor).not.toHaveBeenCalled()
    expect(clear).not.toHaveBeenCalled()
    expect(speak).not.toHaveBeenCalled()
  })

  it('does not present or speak an obsolete evaluation, nor refresh its old objective', async () => {
    const turn = { agent_turn_id: 'evaluation-old', intent: 'learning.evaluate_response', action: { type: 'ask_tutor', source: 'agent_runtime_deterministic', capability: 'learning.evaluate_response' }, response_text: '旧追问' } as AgentTurn
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn, execution: { capability: 'learning.evaluate_response', result: { objective_update_applied: false }, response_text: '旧追问', evidence: ['independent_response'], tutor_turn: { answer: '旧追问' } as TutorTurn } })
    const complete = vi.spyOn(localApi, 'completeAgentTurn')
    const refresh = vi.spyOn(localApi, 'sessionWorkspace')
    const speak = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => undefined)
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '旧话题回答' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.getByText(/不会恢复旧目标或继续旧追问/)).toBeInTheDocument())
    expect(screen.queryByText('旧追问')).not.toBeInTheDocument()
    expect(complete).not.toHaveBeenCalled()
    expect(refresh).not.toHaveBeenCalled()
    expect(speak).not.toHaveBeenCalled()
  })

  it('announces revoked evaluation authority without resubmitting or leaving a pending blocker', async () => {
    const turn = { agent_turn_id: 'evaluation-revoked', intent: 'learning.evaluate_response', action: { type: 'ask_tutor', source: 'agent_runtime_deterministic', capability: 'learning.evaluate_response' } } as AgentTurn
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    const execute = vi.spyOn(localApi, 'executeAgentTurn').mockRejectedValue(new ApiError('source changed', 409, 'LEARNING_EVALUATION_SOURCE_CHANGED'))
    const complete = vi.spyOn(localApi, 'completeAgentTurn')
    const askTutor = vi.spyOn(localApi, 'askTutor')
    const clear = vi.spyOn(localApi, 'clearSessionDraft')
    const speak = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => undefined)
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '这是我的原回答' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.getAllByText(/不用重复提交答案/).length).toBeGreaterThan(0))
    expect(input).toHaveValue('这是我的原回答')
    expect(readPendingEvaluation(workspace.session.session_id, workspace.session.child_id)).toBeNull()
    expect(execute).toHaveBeenCalledTimes(1)
    expect(complete).not.toHaveBeenCalled()
    expect(askTutor).not.toHaveBeenCalled()
    expect(clear).not.toHaveBeenCalled()
    expect(speak.mock.calls.some(call => String(call[0]).includes('不用重复提交答案'))).toBe(true)
  })

  it.each([
    { flags: { local_only: true }, intent: 'supportive_chat' },
    { flags: { cloud_transmission_allowed: false }, intent: 'supportive_chat' },
    { flags: {}, intent: 'memory_command' },
  ])('keeps private replies local and forwards their source turn with policy %j', async ({ flags, intent }) => {
    const turn = {
      agent_turn_id: 'memory-private', intent, response_text: '只保存在家里的私人记忆原文',
      action: { type: 'none', ...flags },
    } as AgentTurn
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const speak = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => undefined)
    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '你记得关于我的什么' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(speak).toHaveBeenCalledWith(turn.response_text, workspace.session.session_id, expect.any(Function), expect.any(Function),
      { localOnly: true, sourceAgentTurnId: 'memory-private', isCurrent: expect.any(Function), onPlaybackEnd: expect.any(Function), onNotDelivered: expect.any(Function), ordinaryResponseAllowed: true }))
  })

  it('accepts a capture without blocking the next utterance, then records OCR failure on its original turn', async () => {
    const turn = {
      agent_turn_id: 'capture-waiting', transcript: '帮我拍题', intent: 'capture_question',
      response_text: '我来拍题', action: { type: 'request_capture' },
      tool_status: 'pending', tool_error_code: null, completed_at: null, created_at: '', orchestrator_version: 'test',
    } as AgentTurn
    const agent = vi.spyOn(localApi, 'agentTurn').mockResolvedValueOnce(turn).mockResolvedValueOnce({ ...turn, agent_turn_id: 'next-chat', action: { type: 'none' }, response_text: '仍能继续对话' })
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    // Exercise the panel routing with a real acknowledgement but hold the
    // device surface open, as when the child has not placed the page yet.
    const accept = (event: Event): void => {
      const detail = (event as CustomEvent<{ accept: () => void }>).detail
      detail.accept()
      event.stopImmediatePropagation()
    }
    window.addEventListener('hsc-request-question-capture', accept)
    try {
      render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
      const input = screen.getByLabelText('对学习伙伴说')
      fireEvent.change(input, { target: { value: '帮我拍题' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      await waitFor(() => expect(screen.getByText(/拍题请求已发出/)).toBeInTheDocument())
      expect(complete).not.toHaveBeenCalled()
      fireEvent.change(input, { target: { value: '说点别的' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      await waitFor(() => expect(agent).toHaveBeenCalledTimes(2))
      await waitFor(() => expect(complete).toHaveBeenCalledWith('next-chat', expect.any(String), 'completed'))
      act(() => emitCaptureReceipt({ sessionId: workspace.session.session_id, commandId: 'capture-waiting', status: 'failed', errorCode: 'OCR_FAILED' }))
      await waitFor(() => expect(complete).toHaveBeenCalledWith('capture-waiting', expect.any(String), 'failed', 'OCR_FAILED', expect.any(Object)))
    } finally { window.removeEventListener('hsc-request-question-capture', accept) }
  })

  it.each(['停止全部监听', '关闭摄像头和麦克风', '别听了'])('preempts busy work locally for %s, even when persistence fails', async (stopText) => {
    let finish!: (turn: AgentTurn) => void
    const agent = vi.spyOn(localApi, 'agentTurn').mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    const order: string[] = []
    const stopped = (): void => { order.push('local-release') }
    window.addEventListener('hsc-privacy-pause', stopped)
    const pause = vi.spyOn(localApi, 'pauseObservation').mockImplementation(async () => { order.push('persist'); throw new Error('offline') })
    const capture = vi.fn()
    window.addEventListener('hsc-request-question-capture', capture)
    const speak = vi.fn()
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), getVoices: () => [], speak })
    try {
      await act(async () => { render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />) })
      const input = screen.getByLabelText('对学习伙伴说')
      fireEvent.change(input, { target: { value: '帮我拍题' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      fireEvent.change(input, { target: { value: '排队的旧话' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      fireEvent.change(input, { target: { value: stopText } })
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(order).toEqual(['local-release', 'persist'])
      expect(pause).toHaveBeenCalledTimes(1)
      expect(agent).toHaveBeenCalledTimes(1)
      expect(screen.getByRole('button', { name: '发送' })).toBeInTheDocument()
      await waitFor(() => expect(screen.getByText(/隐私选择尚未保存到服务/)).toBeInTheDocument())
      expect(localStorage.getItem(privacyStopKey(workspace.session.session_id))).toBe('stopped')
      await act(async () => finish({ agent_turn_id: 'late-capture', action: { type: 'request_capture' }, response_text: '旧回复' } as AgentTurn))
      expect(capture).not.toHaveBeenCalled()
      expect(speak).not.toHaveBeenCalled()
      expect(agent).toHaveBeenCalledTimes(1)
      expect(complete).toHaveBeenCalledWith('late-capture', expect.any(String), 'failed', 'CLIENT_COMMAND_CANCELLED')
      expect(screen.getByText(/隐私选择尚未保存到服务/)).toBeInTheDocument()
    } finally {
      window.removeEventListener('hsc-privacy-pause', stopped)
      window.removeEventListener('hsc-request-question-capture', capture)
    }
  })

  it('keeps privacy stopped after remount and retries saving without opening a device', async () => {
    localStorage.setItem(privacyStopKey(workspace.session.session_id), 'stopped')
    const getUserMedia = vi.fn()
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia, enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const pause = vi.spyOn(localApi, 'pauseObservation').mockRejectedValueOnce(new Error('offline')).mockResolvedValue(workspace.session)
    const enabled = { ...studyWorkspace, session: { ...studyWorkspace.session, observation_status: 'enabled' as const } }
    const first = render(<SessionWorkspace initial={enabled} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(screen.getByText(/隐私选择尚未保存到服务/)).toBeInTheDocument())
    first.unmount()
    render(<SessionWorkspace initial={enabled} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(screen.getByText(/隐私选择已保存/)).toBeInTheDocument())
    expect(pause).toHaveBeenCalledTimes(2)
    expect(getUserMedia).not.toHaveBeenCalled()
  })

  it('carries spoken mistake-photo intent to automatic capture without requiring a mistake button', async () => {
    const turn: AgentTurn = {
      agent_turn_id: 'spoken-mistake-photo', transcript: '帮我拍错题', intent: 'capture_question',
      response_text: '我来拍这道题。', action: { type: 'request_capture' },
      orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: new Date().toISOString()
    }
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(studyWorkspace)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const capture = vi.fn()
    window.addEventListener('hsc-request-question-capture', capture)
    try {
      render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
      // The input below is the same transcript queue used after ASR. Hardware
      // speech is separately exercised by the isolated Electron acceptance.
      const input = screen.getByLabelText('对学习伙伴说')
      fireEvent.change(input, { target: { value: '帮我拍错题' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      await waitFor(() => expect(capture).toHaveBeenCalledTimes(1))
      expect(capture.mock.calls[0][0].detail).toMatchObject({ forMistake: true, sessionId: studyWorkspace.session.session_id, commandId: 'spoken-mistake-photo' })
    } finally { window.removeEventListener('hsc-request-question-capture', capture) }
  })

  it('reuses the same local OCR artifact for tutoring and mistake capture', () => {
    const artifact = documentRevisionArtifact({
      revision_id: 'revision-1',
      artifact_id: 'artifact-local-1',
      revision_index: 2,
      previous_revision_id: 'revision-0',
      extracted_text: '解方程 x²-5x+6=0',
      ocr_confidence: 0.91,
      review_required: false,
      change_summary: { similarity: 0.4, added_lines: ['解方程 x²-5x+6=0'], removed_lines: [], changed: true },
      raw_image_transmitted: false,
      created_at: '2026-08-30T00:00:00Z'
    })

    expect(artifact.artifact_id).toBe('artifact-local-1')
    expect(artifact.details.engine).toBe('continuous_document_monitor')
    expect(artifact.local_path).toBeNull()
    expect(artifact.details.quality?.passed).toBe(true)
  })

  it('starts the mistake flow only when local OCR found a marked target question', () => {
    const base = documentRevisionArtifact({
      revision_id: 'revision-2', artifact_id: 'artifact-local-2', revision_index: 1,
      previous_revision_id: null, extracted_text: '2. Would you like a banana?',
      ocr_confidence: 0.94, review_required: false,
      change_summary: { similarity: 0, added_lines: [], removed_lines: [], changed: true },
      raw_image_transmitted: false, created_at: '2026-08-30T00:00:00Z'
    })
    expect(hasMarkedWrongQuestion(base)).toBe(false)
    expect(hasMarkedWrongQuestion({
      ...base,
      details: {
        ...base.details,
        local_pipeline: {
          engine: 'local-test', route: 'printed',
          quality: { passed: true, issues: [], metrics: {} }, question_blocks: [],
          wrong_question_candidates: [{
            text: '2. Would you like a banana?', confidence: 0.88, ocr_confidence: 0.94,
            bbox_xywh: [10, 20, 300, 40], red_pixel_count: 20, red_mark_count: 1,
            evidence: 'red_correction_mark_near_question', requires_child_confirmation: true
          }],
          manual_review: { required: false, reasons: [], handwriting: 'not_detected', math: 'not_detected' },
          queue: { task_id: 'task-1', behavior_fps_cap: 2 }
        }
      }
    })).toBe(true)
  })
})

async function mountCameraWithoutFrames(releaseFails = false): Promise<{
  stop: ReturnType<typeof vi.fn>
  register: ReturnType<typeof vi.spyOn>
  release: ReturnType<typeof vi.spyOn>
  incident: ReturnType<typeof vi.spyOn>
}> {
  const device = { kind: 'videoinput' as const, deviceId: 'no-frame-camera', groupId: 'no-frame-group', label: 'L523' }
  const stop = vi.fn()
  const track = {
    label: device.label, readyState: 'live', getSettings: () => ({ deviceId: device.deviceId, groupId: device.groupId, width: 640, height: 360, frameRate: 15 }),
    getCapabilities: () => ({}), addEventListener: vi.fn(), stop,
  } as unknown as MediaStreamTrack
  const stream = { getVideoTracks: () => [track], getTracks: () => [track] } as unknown as MediaStream
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true, value: { enumerateDevices: vi.fn().mockResolvedValue([device]), getUserMedia: vi.fn().mockResolvedValue(stream) },
  })
  const bindingHash = await mediaDeviceInfoHash(device)
  vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([{ device_type: 'camera', system_id_hash: bindingHash, label: 'L523', capabilities: {}, verified_at: '', updated_at: '' }])
  const register = vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'no-frame-lease', status: 'active', expires_at: '' })
  vi.spyOn(localApi, 'startObservationQualification').mockResolvedValue({
    status: 'qualifying', scene_quality: 'not_evaluated', person_baseline: 'not_evaluated',
    reason: '正在确认取景。', source: 'renderer', attempt_count: 1, qualified_at: null,
    updated_at: '2026-08-28T00:00:00Z', raw_frame_stored: false, longitudinal_inference_enabled: false,
  })
  const release = vi.spyOn(localApi, 'releaseDeviceLease')
  if (releaseFails) release.mockRejectedValue(new ApiError('租约服务暂不可用。', 503, 'LEASE_UNAVAILABLE'))
  else release.mockResolvedValue({ lease_id: 'no-frame-lease', status: 'released' })
  const incident = vi.spyOn(localApi, 'recordDeviceIncident').mockResolvedValue({} as never)
  vi.spyOn(localApi, 'beginObservationStream').mockImplementation(async (_session, epoch) => recordedCoverage(epoch, 'start'))
  vi.spyOn(localApi, 'endObservationStream').mockImplementation(async (_session, epoch) => recordedCoverage(epoch, 'stop'))
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
  render(<LocalObservation sessionId="session-no-frame" active enabled snoozeUntil={null} />)
  await vi.waitFor(() => expect(register).toHaveBeenCalledTimes(1))
  return { stop, register, release, incident }
}

describe('spoken wake-word initialization', () => {
  it('accepts a short chosen name and rejects setup-prompt fragments', () => {
    expect(normalizeSpokenWakeWordCandidate('以后就叫你小知。')).toBe('小知')
    expect(normalizeSpokenWakeWordCandidate('你的名字是小伴')).toBe('小伴')
    expect(normalizeSpokenWakeWordCandidate('这个名')).toBeNull()
    expect(normalizeSpokenWakeWordCandidate('这个名字我没有听清')).toBeNull()
    expect(decideWakeWordSetup('小知', null)).toEqual({ type: 'candidate', candidate: '小知' })
    expect(decideWakeWordSetup('确认', '小知')).toEqual({ type: 'confirm', candidate: '小知' })
    expect(decideWakeWordSetup('换一个', '小知')).toEqual({ type: 'reject' })
  })
})

describe('SessionWorkspace draft recovery', () => {
  beforeEach(() => {
    vi.spyOn(localApi, 'saveSessionDraft').mockImplementation(async (_session, section, payload) => ({ section, payload, updated_at: new Date().toISOString() }))
    let playback: TutorPlaybackCheckpoint | null = null
    vi.spyOn(localApi, 'tutorPlayback').mockImplementation(async () => ({ playback }))
    vi.spyOn(localApi, 'mutateTutorPlayback').mockImplementation(async (sessionId, operation, values) => {
      if (operation === 'begin') playback = {
        playback_id: 'component-playback', turn_id: String(values.turn_id), session_id: sessionId, child_id: workspace.session.child_id,
        source_version: 'component-source', presentation_version: 'component-presentation', generation: 1, revision: 1,
        status: 'paused', segment_index: 0, segment_started: false, reason: null, replayed: false, evidence_kind: 'playback_only',
        presentation: values.presentation as TutorBlackboardPresentation,
      }
      else {
        if (!playback || playback.revision !== values.expected_revision || playback.generation !== values.generation) throw new Error('COMPONENT_PLAYBACK_BINDING')
        playback = { ...playback, revision: playback.revision + 1 }
        if (operation === 'control') playback = { ...playback, generation: playback.generation + 1, segment_started: false,
          status: values.command === 'resume' ? 'playing' : values.command === 'cancel' ? 'cancelled' : 'paused' }
        else playback = { ...playback, segment_started: values.event === 'started',
          segment_index: playback.segment_index + Number(values.event === 'ended') }
      }
      return { playback: playback! }
    })
  })
  it.each([true, false])('keeps structured Tutor playback owned when a committed ASR returns (private: %s)', async localOnly => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ getQwenRealtimeSettings: async () => ({ configured: true, enabled: true,
      endpoint: 'synthetic-invalid', model: 'qwen3.8-omni-flash-realtime', voice: 'synthetic' }) }))
    vi.stubGlobal('navigator', { mediaDevices: { enumerateDevices: vi.fn().mockResolvedValue([]) } })
    const speech = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => undefined)
    const question = '用求根公式解 x²-5x+6=0。'
    const answer = '先观察标准形式，你能说出 a、b、c 吗？'
    const turn = { agent_turn_id: 'voice-owned-tutor-agent', intent: 'learning.explain',
      transcript: '讲解刚才那道题', response_text: '先看原题。', tool_status: 'pending',
      runtime_execution_location: 'local_model', runtime_capability: 'learning.explain',
      action: { type: 'ask_tutor', subject: '数学', question, local_only: localOnly } } as AgentTurn
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async id => ({ ...turn, agent_turn_id: id, tool_status: 'completed' }))
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    vi.spyOn(localApi, 'askTutor').mockResolvedValue({ turn_id: 'voice-owned-tutor', child_id: studyWorkspace.session.child_id,
      session_id: studyWorkspace.session.session_id, answer, question, subject: '数学', hint_level: 1,
      plan_item_id: null, provider: 'test', model: 'test', status: 'completed', parent_turn_id: null,
      response_kind: 'hint', verification_status: 'not_applicable_hint', safety_status: 'passed',
      strategy_experiment_id: null, strategy_version: null, created_at: new Date().toISOString(),
      output_envelope: { request_id: 'owned-request', output_type: 'tutoring', reason_summary: '观察系数',
        confidence: 0.65, evidence_refs: [], model_provider: 'test', model_version: 'test', prompt_version: 'test',
        knowledge_version: 'test', policy_version: 'test', verification_status: 'partially_verified',
        safety_status: 'passed', fallback_used: false,
        result: { blackboard: { version: 'tutor_blackboard_v1', source: 'model_structured',
        sync_mode: 'segment_estimate', subject: '数学', title: '观察系数', segments: [{ segment_id: 's1', narration: answer,
          duration_hint_ms: 2000, blocks: [{ kind: 'question', text: 'a、b、c 是什么？', tone: 'chalk' }] }] } } },
    })
    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await waitFor(() => expect(realtimeHarness.onTurn).toEqual(expect.any(Function)))
    let reply!: QwenRealtimeToolResult
    await act(async () => { reply = await realtimeHarness.onTurn!('讲解刚才那道题', {
      localInputId: 'voice-owned-tutor-input', capturedAt: Date.now(), sessionId: studyWorkspace.session.session_id,
      reference: voiceCaptureReference(studyWorkspace.session.session_id, studyWorkspace.session.child_id),
      source: 'accepted_onset', carryoverConfirmationId: null,
    }) })
    expect(reply).toMatchObject({ responseText: 'tutor_playback_owned', speak: false })
    const narrated = speech.mock.calls.filter(([text]) => text === answer)
    expect(narrated).toHaveLength(1)
    expect(localApi.askTutor).toHaveBeenCalledTimes(1)
    await act(async () => { narrated[0][3]?.(); await Promise.resolve() })
    await waitFor(() => expect(localApi.mutateTutorPlayback).toHaveBeenCalledWith(studyWorkspace.session.session_id,
      'segment', expect.objectContaining({ segment_id: 's1', event: 'started' })))
    await act(async () => { narrated[0][4]?.onPlaybackEnd?.(); await Promise.resolve() })
    await waitFor(() => expect(localApi.mutateTutorPlayback).toHaveBeenCalledWith(studyWorkspace.session.session_id,
      'segment', expect.objectContaining({ segment_id: 's1', event: 'ended' })))
    expect(vi.mocked(localApi.mutateTutorPlayback).mock.calls.some(([, op, value]) => op === 'control' && value.command === 'pause')).toBe(false)
  })

  it('passes every structured homework field from voice intent into durable storage', async () => {
    const homeworkWorkspace: SessionWorkspaceState = {
      ...workspace,
      session: { ...workspace.session, state: 'homework_capture', child_id: 'child-primary-4' },
      family_rule: { ...workspace.family_rule, child_id: 'child-primary-4' },
      drafts: {}
    }
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({
      agent_turn_id: 'structured-homework-turn',
      transcript: '英语背诵课本第十二页到第十三页，明天检查，老师要求脱稿，大概十五分钟',
      intent: 'add_homework', response_text: '已记下英语背诵任务。',
      action: {
        type: 'add_homework', subject: '英语', title: '英语背诵课本第十二页到第十三页',
        expected_minutes: 15, task_type: 'memorization', range_text: '第十二页到第十三页',
        due_at: '2026-08-27', special_requirements: '脱稿'
      },
      orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: new Date().toISOString()
    } as AgentTurn)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const addHomework = vi.spyOn(localApi, 'addHomework').mockResolvedValue({} as never)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(homeworkWorkspace)

    render(<SessionWorkspace initial={homeworkWorkspace} childName="小宇" childGrade="四年级" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '英语背诵课本第十二页到第十三页' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => expect(addHomework).toHaveBeenCalledWith(
      'session-draft-1', '英语', '英语背诵课本第十二页到第十三页', 15,
      {
        taskType: 'memorization', rangeText: '第十二页到第十三页',
        dueAt: '2026-08-27', specialRequirements: '脱稿'
      }
    ))
  })

  it('treats a bare household wake as wake-only and never invents a learning request', async () => {
    const agentTurn = vi.spyOn(localApi, 'agentTurn')
    vi.spyOn(localApi, 'agentRuntimeStatus').mockRejectedValue(new Error('runtime unavailable'))
    const consumed = vi.fn()
    const spoken: string[] = []
    class FakeUtterance {
      text: string
      voice: SpeechSynthesisVoice | null = null
      constructor(text: string) { this.text = text }
    }
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    vi.stubGlobal('speechSynthesis', {
      cancel: vi.fn(),
      getVoices: () => [offlineChineseVoice],
      speak: (utterance: FakeUtterance) => { spoken.push(utterance.text) },
    })

    render(<SessionWorkspace initial={{ ...workspace, drafts: {} }} childName="小辰" childGrade="初三" initialWakeOnly onInitialVoiceCommandConsumed={consumed} onBack={vi.fn()} />)

    await waitFor(() => expect(consumed).toHaveBeenCalledTimes(1))
    expect(agentTurn).not.toHaveBeenCalled()
    expect(screen.getByText(/我在，小辰/)).toBeInTheDocument()
    expect(spoken.some((message) => message.includes('我在'))).toBe(true)
  })

  it.each(['delivery', 'execution'] as const)('keeps the unknown %s reminder on the first household wake without cloud speech', async kind => {
    const pending = saveAgentRequest(workspace.session.session_id, 'first-wake-operation')
    if (kind === 'execution') saveAgentExecution(pending, workspace.session.child_id, 'first-wake-turn')
    const agentTurn = vi.spyOn(localApi, 'agentTurn')
    vi.spyOn(localApi, 'agentRuntimeStatus').mockRejectedValue(new Error('runtime unavailable'))
    const managed = vi.spyOn(LocalAgentStreamingTtsProvider.prototype, 'stream')
    const consumed = vi.fn(), spoken: string[] = []
    class FakeUtterance {
      text: string
      voice: SpeechSynthesisVoice | null = null
      constructor(text: string) { this.text = text }
    }
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), getVoices: () => [offlineChineseVoice],
      speak: (utterance: FakeUtterance) => { spoken.push(utterance.text) } })
    render(<SessionWorkspace initial={{ ...workspace, drafts: {} }} childName="小辰" childGrade="初三" initialWakeOnly onInitialVoiceCommandConsumed={consumed} onBack={vi.fn()} />)
    await waitFor(() => expect(consumed).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/核对刚才请求/)).toBeInTheDocument()
    expect(spoken.some(message => message.includes('核对刚才请求'))).toBe(true)
    expect(spoken.some(message => message.includes('我在，小辰'))).toBe(false)
    expect(agentTurn).not.toHaveBeenCalled()
    expect(managed).not.toHaveBeenCalled()
  })

  it('does not acknowledge an inaudible proactive prompt as delivered', async () => {
    const speech = vi.spyOn(LocalInputs, 'speakLocally')
    const managed = vi.spyOn(LocalAgentStreamingTtsProvider.prototype, 'stream')
    const acknowledge = vi.spyOn(localApi, 'acknowledgeProactiveAgentPrompt').mockResolvedValue({
      prompt_id: 'prompt-reminder-1', status: 'acknowledged',
    })
    class FakeUtterance {
      text: string
      voice: SpeechSynthesisVoice | null = null
      constructor(text: string) { this.text = text }
    }
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    vi.stubGlobal('speechSynthesis', {
      cancel: vi.fn(), getVoices: () => [], speak: vi.fn(),
    })
    render(<SessionWorkspace initial={{ ...workspace, drafts: {} }} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    act(() => window.dispatchEvent(new CustomEvent('hsc-agent-intervention', {
      detail: { message: '下一项是数学订正，准备好就直接说开始。', promptId: 'prompt-reminder-1' },
    })))

    expect(screen.getByText('下一项是数学订正，准备好就直接说开始。')).toBeInTheDocument()
    await waitFor(() => expect(speech).toHaveBeenCalledWith('下一项是数学订正，准备好就直接说开始。', workspace.session.session_id,
      expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true, priority: 'reminder', onPlaybackEnd: expect.any(Function) })))
    expect(acknowledge).not.toHaveBeenCalled()
    expect(managed).not.toHaveBeenCalled()
  })

  it('acknowledges reminders once on actual playback end, never on voice admission', async () => {
    const speech = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation((_text, _sid, done) => done?.('system_offline'))
    const acknowledge = vi.spyOn(localApi, 'acknowledgeProactiveAgentPrompt').mockResolvedValue({ prompt_id: 'audible-reminder', status: 'acknowledged' })
    render(<SessionWorkspace initial={{ ...workspace, drafts: {} }} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    act(() => window.dispatchEvent(new CustomEvent('hsc-agent-intervention', {
      detail: { message: '休息到了，准备好后继续。', promptId: 'audible-reminder' },
    })))
    const call = speech.mock.calls.find(([text]) => text === '休息到了，准备好后继续。')!
    expect(acknowledge).not.toHaveBeenCalled()
    act(() => { call[4]?.onPlaybackEnd?.(); call[4]?.onPlaybackEnd?.() })
    await waitFor(() => expect(acknowledge).toHaveBeenCalledExactlyOnceWith(workspace.session.session_id, 'audible-reminder'))
  })

  it('refreshes a server-settled mistake review without completing the task a second time', async () => {
    const active: SessionWorkspaceState = { ...studyWorkspace, plan_items: [{ ...studyWorkspace.plan_items[0],
      source_type: 'mistake_review', source_ref: 'mistake-delayed', status: 'active',
      started_at: '2026-08-26T10:00:00Z', active_segment_started_at: '2026-08-26T10:00:00Z' }] }
    const completed = { ...active.plan_items[0], status: 'completed' as const,
      active_segment_started_at: null, actual_minutes: 5, completion_evidence_type: 'review_evidence' as const }
    const reply = '本次复习任务已收好。后续仍按计划复测。'
    const turn = { agent_turn_id: 'review-settled-turn', action: { type: 'continue_mistake_dialogue' },
      response_text: reply, runtime_capability: 'mistake.dialogue', runtime_execution_location: 'server' } as AgentTurn
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue({ ...active, plan_items: [completed] })
    vi.spyOn(localApi, 'mistakeDialogue').mockResolvedValue({ dialogue: null })
    vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
    const complete = vi.spyOn(localApi, 'completePlanItem').mockResolvedValue(undefined)
    const execute = vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn, execution: {
      capability: 'mistake.dialogue', response_text: reply, evidence: ['mistake_progress'],
      result: { mistake_id: 'mistake-delayed', plan_completion: { plan_item_id: completed.plan_item_id } },
    } })
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const speech = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation((_text, _sid, done) => done?.('system_offline'))
    render(<SessionWorkspace initial={active} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: '答案是C' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(execute).toHaveBeenCalledExactlyOnceWith(turn.agent_turn_id))
    await waitFor(() => expect(screen.getByText(/复测已记录/)).toBeInTheDocument())
    await waitFor(() => expect(speech.mock.calls.some(([text]) => text === reply)).toBe(true))
    expect(complete).not.toHaveBeenCalled()
  })

  it('persists task-specific completion checks when the child says the task was completed and checked', async () => {
    const activeWorkspace: SessionWorkspaceState = {
      ...studyWorkspace,
      plan_items: [{
        ...studyWorkspace.plan_items[0], status: 'active',
        started_at: '2026-08-26T10:00:00Z', active_segment_started_at: '2026-08-26T10:00:00Z'
      }]
    }
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({
      agent_turn_id: 'voice-complete-checked', transcript: '完成并检查了', intent: 'complete_task',
      response_text: '收到。我会记录这项已经完成并检查，然后看看下一项。',
      action: { type: 'complete_active_task', completion_evidence_type: 'child_checked' },
      runtime_capability: 'task.complete', runtime_execution_location: 'server',
      orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: '2026-08-26T10:10:00Z'
    })
    const complete = vi.spyOn(localApi, 'completePlanItem').mockResolvedValue(undefined)
    const savedTask = { ...activeWorkspace.plan_items[0], status: 'completed', completion_evidence_type: 'child_checked',
      completion_note: '孩子逐项确认已完成并检查任务要求', completion_checklist: ['task_finished', 'missing_items_checked'] }
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue({ ...activeWorkspace, plan_items: [savedTask] } as SessionWorkspaceState)
    vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
    const execution = vi.spyOn(localApi, 'executeAgentTurn').mockImplementation(async () => ({
      turn: { ...await vi.mocked(localApi.agentTurn).mock.results[0].value, response_text: '已保存完成及检查记录。' },
      execution: { capability: 'task.complete', result: savedTask, response_text: '已保存完成及检查记录。', evidence: ['persisted_task'] },
    }))
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={activeWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: '完成并检查了' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    await waitFor(() => expect(execution).toHaveBeenCalledExactlyOnceWith('voice-complete-checked'))
    expect(complete).not.toHaveBeenCalled()
    expect((await execution.mock.results[0].value).execution.result).toMatchObject({ plan_item_id: 'plan-item-1',
      completion_evidence_type: 'child_checked', completion_checklist: ['task_finished', 'missing_items_checked'] })
    await waitFor(() => expect(screen.getByText('已保存完成及检查记录。')).toBeTruthy())
  })

  it('persists voice-reported correction status and resets reminder preferences without child clicks', async () => {
    const activeWorkspace: SessionWorkspaceState = {
      ...studyWorkspace,
      plan_items: [{
        ...studyWorkspace.plan_items[0], status: 'active',
        started_at: '2026-08-26T10:00:00Z', active_segment_started_at: '2026-08-26T10:00:00Z'
      }]
    }
    vi.spyOn(localApi, 'agentTurn')
      .mockResolvedValueOnce({
        agent_turn_id: 'voice-needs-correction', transcript: '做完但要订正', intent: 'complete_task',
        response_text: '收到。我会把这项记录为已经做完但仍需订正，不会算成完全掌握。',
        action: { type: 'complete_active_task', completion_evidence_type: 'needs_correction' },
        runtime_capability: 'task.complete', runtime_execution_location: 'server',
        orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
        completed_at: null, created_at: '2026-08-26T10:10:00Z'
      })
      .mockResolvedValueOnce({
        agent_turn_id: 'voice-reset-reminders', transcript: '重置提示偏好', intent: 'reset_behavior_personalization',
        response_text: '好，我只重置本机的提醒偏好。',
        action: { type: 'reset_behavior_personalization' },
        orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
        completed_at: null, created_at: '2026-08-26T10:11:00Z'
      })
    const complete = vi.spyOn(localApi, 'completePlanItem').mockResolvedValue(undefined)
    const execution = vi.spyOn(localApi, 'executeAgentTurn').mockImplementation(async () => ({
      turn: { ...await vi.mocked(localApi.agentTurn).mock.results[0].value, response_text: '已保存仍需订正，不算完全掌握。' },
      execution: { capability: 'task.complete', result: { ...activeWorkspace.plan_items[0], status: 'completed', completion_evidence_type: 'needs_correction',
        completion_note: '孩子通过语音明确表示已完成但仍需订正', completion_checklist: [] }, response_text: '已保存仍需订正，不算完全掌握。', evidence: ['persisted_task'] },
    }))
    vi.spyOn(localApi, 'sessionWorkspace').mockImplementation(async () => ({ ...activeWorkspace, plan_items: [{ ...activeWorkspace.plan_items[0], status: 'completed', completion_evidence_type: 'needs_correction', completion_checklist: [] }] }))
    vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
    const reset = vi.spyOn(localApi, 'resetSessionBehaviorPersonalization').mockResolvedValue({ minimum_feedback_samples: 4, boundary: '只会降低提醒频率。', items: [] })
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={activeWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '做完但要订正' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(execution).toHaveBeenCalledExactlyOnceWith('voice-needs-correction'))
    expect(complete).not.toHaveBeenCalled()
    expect((await execution.mock.results[0].value).execution.result).toMatchObject({ plan_item_id: 'plan-item-1', completion_evidence_type: 'needs_correction', completion_checklist: [] })
    await waitFor(() => expect(screen.getByText('已保存仍需订正，不算完全掌握。')).toBeTruthy())

    fireEvent.change(input, { target: { value: '重置提示偏好' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(reset).toHaveBeenCalledWith('session-draft-1'))
  })

  it('keeps the reply visible and labels a child-requested speech interruption', () => {
    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    fireEvent.click(screen.getByTitle('停止伙伴播报'))

    expect(screen.getByText('播报已停止')).toBeInTheDocument()
    expect(screen.getByText('我在这儿。')).toBeInTheDocument()
  })

  it('does not claim an unscheduled greeting is synthesizing or require predefined commands', () => {
    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    expect(screen.getByText('我在这儿。')).toBeInTheDocument()
    expect(screen.queryByText('正在准备声音')).not.toBeInTheDocument()
    expect(screen.queryByText(/你可以直接对我说/)).not.toBeInTheDocument()
  })

  it('confirms recovery only after the current observation stream supplies a usable first frame', async () => {
    vi.useFakeTimers()
    await mountCameraWithoutFrames()
    expect(localApi.confirmDeviceFrame).not.toHaveBeenCalled()
    const video = document.querySelector('.observation-preview') ?? document.querySelector('video')!
    Object.defineProperties(video, { readyState: { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA },
      videoWidth: { configurable: true, value: 640 }, videoHeight: { configurable: true, value: 360 } })
    await act(async () => { fireEvent.loadedData(video) })
    expect(localApi.confirmDeviceFrame).toHaveBeenCalledExactlyOnceWith('no-frame-lease', 640, 360)
    await act(async () => { fireEvent.timeUpdate(video); fireEvent.resize(video); await vi.advanceTimersByTimeAsync(200) })
    expect(localApi.confirmDeviceFrame).toHaveBeenCalledTimes(1)
  })

  it('stops and records exactly one local failure when no first frame arrives within five seconds', async () => {
    vi.useFakeTimers()
    const fixture = await mountCameraWithoutFrames()

    await act(async () => { await vi.advanceTimersByTimeAsync(12_000) })

    expect(localApi.confirmDeviceFrame).not.toHaveBeenCalled()
    expect(fixture.stop).toHaveBeenCalledTimes(1)
    expect(fixture.release).toHaveBeenCalledWith('no-frame-lease', 'first_frame_timeout', expect.any(AbortSignal))
    expect(fixture.incident).toHaveBeenCalledTimes(1)
    expect(fixture.incident).toHaveBeenCalledWith('session-no-frame', 'camera', 'NO_USABLE_VIDEO_FRAME', expect.any(String))
    expect(screen.getByText(/12 秒内没有可用画面，已停止本次读取/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新打开并检测' })).toBeInTheDocument()
    expect(screen.getByText(/不记录行为状态或长期记录/)).toBeInTheDocument()
  })

  it('automatically reopens the observation camera after a lost video feed', async () => {
    vi.useFakeTimers()
    const fixture = await mountCameraWithoutFrames()
    const getUserMedia = vi.mocked(navigator.mediaDevices.getUserMedia)
    const bindings = vi.mocked(localApi.deviceBindings)
    const reattached = new Promise<void>((resolve) => {
      fixture.register.mockImplementationOnce(async () => {
        resolve()
        return { lease_id: 'reconnected-lease', status: 'active', expires_at: '' }
      })
    })

    await act(async () => { await vi.advanceTimersByTimeAsync(12_000) })
    expect(fixture.release).toHaveBeenCalledWith('no-frame-lease', 'first_frame_timeout', expect.any(AbortSignal))
    expect(screen.getByText(/12 秒内没有可用画面，已停止本次读取/)).toBeInTheDocument()
    // The committed reconnect effect starts attempt 1 with a 2-second delay.
    await act(async () => { await vi.advanceTimersByTimeAsync(1_999) })
    expect(bindings).toHaveBeenCalledTimes(1)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
      expect(bindings).toHaveBeenCalledTimes(2)
      // Native Web Crypto device hashing is not driven by the fake clock.
      await reattached
    })

    expect(getUserMedia).toHaveBeenCalledTimes(2)
    expect(fixture.register).toHaveBeenCalledTimes(2)
  })

  it('reopens the observation camera when the child says to resume after a local frame failure', async () => {
    vi.useFakeTimers()
    const fixture = await mountCameraWithoutFrames()
    const getUserMedia = vi.mocked(navigator.mediaDevices.getUserMedia)
    const reattached = new Promise<void>((resolve) => {
      fixture.register.mockImplementationOnce(async () => {
        resolve()
        return { lease_id: 'voice-resumed-lease', status: 'active', expires_at: '' }
      })
    })

    await act(async () => { await vi.advanceTimersByTimeAsync(12_000) })
    expect(fixture.release).toHaveBeenCalledWith('no-frame-lease', 'first_frame_timeout', expect.any(AbortSignal))
    vi.spyOn(localApi, 'resumeObservation').mockResolvedValue({ ...workspace.session, session_id: 'session-no-frame', observation_status: 'enabled' })

    await act(async () => {
      window.dispatchEvent(new CustomEvent('hsc-camera-observation-resume'))
      // Device hashing runs outside the fake clock; wait for its real receipt.
      await reattached
    })

    expect(getUserMedia).toHaveBeenCalledTimes(2)
    expect(fixture.register).toHaveBeenCalledTimes(2)
  })

  it('stops the browser track even when lease release fails and reports that boundary truthfully', async () => {
    vi.useFakeTimers()
    const fixture = await mountCameraWithoutFrames(true)

    await act(async () => { await vi.advanceTimersByTimeAsync(12_000) })

    expect(fixture.stop).toHaveBeenCalledTimes(1)
    expect(fixture.release).toHaveBeenCalledWith('no-frame-lease', 'first_frame_timeout', expect.any(AbortSignal))
    expect(screen.getByText(/旧连接的关闭状态还未确认/)).toBeInTheDocument()
  })

  it('finishes a voice privacy pause with a truthful lease-release failure state', async () => {
    const fixture = await mountCameraWithoutFrames(true)

    window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))

    await waitFor(() => expect(screen.getByText(/浏览器已停止读取，但本地服务未确认设备租约释放/)).toBeInTheDocument())
    expect(fixture.stop).toHaveBeenCalledTimes(1)
    expect(fixture.release).toHaveBeenCalledWith('no-frame-lease', 'voice_privacy_pause', expect.any(AbortSignal))
  })

  it('requires an explicit decision for each deferred task before planning', async () => {
    const deferredWorkspace: SessionWorkspaceState = {
      ...workspace,
      session: { ...workspace.session, state: 'homework_capture' },
      carryover_candidates: [{
        source_plan_item_id: 'source-plan-item-1', source_session_id: 'source-session-1',
        source_date: '2026-08-25', subject: '数学', title: '判别式练习', expected_minutes: 20,
        task_type: 'written', range_text: null, due_at: null, special_requirements: null,
        reschedule_reason: '今晚时间不够，明天先确认是否继续'
      }],
      drafts: {}
    }
    const review = vi.spyOn(localApi, 'reviewDeferredTask').mockResolvedValue({} as never)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue({
      ...deferredWorkspace,
      homework: [{
        homework_id: 'copied-homework-1', subject: '数学', task_type: 'written', title: '判别式练习',
        range_text: null, expected_minutes: 20, due_at: null, special_requirements: null,
        source_artifact_id: null, ocr_confidence: null, confirmation_status: 'child_confirmed', status: 'pending'
      }],
      carryover_candidates: [],
      carryover_decisions: [{
        review_id: 'review-1', source_plan_item_id: 'source-plan-item-1', source_session_id: 'source-session-1',
        decision: 'continue_today', reason: '孩子确认今天继续完成', target_homework_id: 'copied-homework-1',
        reviewed_at: '2026-08-26T10:05:00Z'
      }]
    })

    render(<SessionWorkspace initial={deferredWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    expect(screen.getByText('先处理上次延期的任务')).toBeInTheDocument()
    expect(screen.getByText(/今晚时间不够，明天先确认是否继续/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /制定今天的计划/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: '今天没有作业' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '今天继续' }))

    await waitFor(() => expect(review).toHaveBeenCalledWith(
      'session-draft-1', 'source-plan-item-1', 'continue_today', '孩子确认今天继续完成'
    ))
    expect(await screen.findByText('数学 · 书面作业 · 20分钟')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /制定今天的计划/ })).toBeEnabled()
  })

  it('distinguishes a planned break from a temporarily paused task', () => {
    const plannedBreakWorkspace: SessionWorkspaceState = {
      ...studyWorkspace,
      session: { ...studyWorkspace.session, state: 'break' },
      plan_items: [{
        ...studyWorkspace.plan_items[0],
        plan_item_id: 'planned-break-1', subject: '休息', title: '站起来活动一下',
        source_type: 'planned_break', task_type: 'break', status: 'active'
      }]
    }
    const { unmount } = render(
      <SessionWorkspace initial={plannedBreakWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />
    )
    expect(screen.getByRole('button', { name: '结束休息，继续计划' })).toBeInTheDocument()

    unmount()
    render(<SessionWorkspace initial={{
      ...studyWorkspace,
      session: { ...studyWorkspace.session, state: 'break' },
      plan_items: [{ ...studyWorkspace.plan_items[0], status: 'paused' }]
    }} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    expect(screen.getByRole('button', { name: '继续刚才的任务' })).toBeInTheDocument()
  })

  it('does not offer observation resume after the learning session is inactive', () => {
    render(<LocalObservation sessionId="session-completed" active={false} enabled={true} snoozeUntil={null} />)

    expect(screen.getByText('本地观察已关闭')).toBeInTheDocument()
    expect(screen.getByText(/学习会话已经结束/)).toBeInTheDocument()
    expect(screen.queryByTitle('恢复本地观察')).not.toBeInTheDocument()
  })

  it('turns dark-scene and missing-baseline diagnostics into actionable, privacy-safe retry steps', async () => {
    const device = { kind: 'videoinput' as const, deviceId: 'guided-camera', groupId: 'guided-group', label: 'Guided Camera' }
    const stop = vi.fn()
    const track = {
      label: device.label, readyState: 'live', getSettings: () => ({ deviceId: device.deviceId, groupId: device.groupId, width: 640, height: 360, frameRate: 10 }),
      getCapabilities: () => ({}), addEventListener: vi.fn(), stop,
    } as unknown as MediaStreamTrack
    const stream = { getVideoTracks: () => [track], getTracks: () => [track] } as unknown as MediaStream
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true, value: { enumerateDevices: vi.fn().mockResolvedValue([device]), getUserMedia: vi.fn().mockResolvedValue(stream) },
    })
    const bindingHash = await mediaDeviceInfoHash(device)
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([{ device_type: 'camera', system_id_hash: bindingHash, label: device.label, capabilities: {}, verified_at: '', updated_at: '' }])
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'guided-lease', status: 'active', expires_at: '' })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'guided-lease', status: 'released' })
    vi.spyOn(localApi, 'startObservationQualification').mockResolvedValue({
      status: 'qualifying', scene_quality: 'not_evaluated', person_baseline: 'not_evaluated', reason: '正在确认取景。', source: 'renderer',
      attempt_count: 1, qualified_at: null, updated_at: '2026-08-28T00:00:00Z', raw_frame_stored: false, longitudinal_inference_enabled: false,
    })
    const analyze = vi.spyOn(localApi, 'analyzeObservation')
      .mockResolvedValueOnce({
        state: 'unknown', confidence: 0, reason: '画面太暗。', duration_ms: 0, model_version: 'opencv_rules_v1', shadow: false,
        observation_ready: false, raw_frame_stored: false, intervention: null,
        scene_quality: { status: 'too_dark', message: '画面太暗，请打开正面照明，并把摄像头转向孩子常坐的位置。' },
        person_baseline: { status: 'not_evaluated', message: '取景未通过。' }, metrics: {},
      } as never)
      .mockResolvedValueOnce({
        state: 'unknown', confidence: 0, reason: '等待人物基线。', duration_ms: 0, model_version: 'opencv_rules_v1', shadow: false,
        observation_ready: false, raw_frame_stored: false, intervention: null,
        scene_quality: { status: 'ready', message: '取景和清晰度满足本地观察的基础条件。' },
        person_baseline: { status: 'awaiting_person', message: '请让孩子在常坐位置正对镜头几秒，确认取景后再开始观察。' }, metrics: {},
      } as never)
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,synthetic')
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      drawImage: vi.fn(), getImageData: () => {
        const pixels = new Uint8ClampedArray(320 * 180 * 4)
        for (let index = 0; index < pixels.length; index += 4) { const value = (index / 4) % 2 ? 110 : 170; pixels[index] = value; pixels[index + 1] = value; pixels[index + 2] = value; pixels[index + 3] = 255 }
        return { data: pixels, width: 320, height: 180 } as ImageData
      },
    } as unknown as CanvasRenderingContext2D)

    const view = render(<LocalObservation sessionId="session-guided" active enabled snoozeUntil={null} />)
    await waitFor(() => expect(localApi.startObservationQualification).toHaveBeenCalledWith('session-guided', expect.any(AbortSignal)))
    const video = view.container.querySelector('video')!
    let sampledMediaTime = 0
    Object.defineProperties(video, {
      readyState: { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA }, videoWidth: { configurable: true, value: 640 }, videoHeight: { configurable: true, value: 360 },
      currentTime: { configurable: true, get: () => sampledMediaTime },
      getVideoPlaybackQuality: { configurable: true, value: () => ({ totalVideoFrames: Math.floor(sampledMediaTime * 10) }) },
    })
    fireEvent.loadedData(video)

    expect(await screen.findByText('先补光，再重新检测')).toBeInTheDocument()
    expect(screen.getByText(/打开正面照明，避免背光或镜头直对灯光/)).toBeInTheDocument()
    expect(screen.getByText(/不会保存、上传或展示原始画面/)).toBeInTheDocument()
    sampledMediaTime = 1 // A fresh decoded frame, never reusing the old sample.
    fireEvent.click(screen.getByRole('button', { name: '立即重新检测' }))
    expect(await screen.findByText('请入镜并正对镜头')).toBeInTheDocument()
    expect(screen.getByText(/常坐位置正对镜头几秒/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '暂停本地观察' })).toBeInTheDocument()
    expect(analyze).toHaveBeenCalledTimes(2)
  })

  it('keeps a still detailed room healthy while the media clock continues', async () => {
    vi.useFakeTimers()
    const device = { kind: 'videoinput' as const, deviceId: 'still-camera', groupId: 'still-group', label: 'Still Camera' }
    const stop = vi.fn()
    const track = {
      label: device.label, readyState: 'live', enabled: true, muted: false, getSettings: () => ({ deviceId: device.deviceId, groupId: device.groupId, width: 640, height: 360, frameRate: 10 }),
      getCapabilities: () => ({}), addEventListener: vi.fn(), stop,
    } as unknown as MediaStreamTrack
    const stream = { getVideoTracks: () => [track], getTracks: () => [track] } as unknown as MediaStream
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true, value: { enumerateDevices: vi.fn().mockResolvedValue([device]), getUserMedia: vi.fn().mockResolvedValue(stream) },
    })
    const bindingHash = await mediaDeviceInfoHash(device)
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([{ device_type: 'camera', system_id_hash: bindingHash, label: device.label, capabilities: {}, verified_at: '', updated_at: '' }])
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'still-lease', status: 'active', expires_at: '' })
    const release = vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'still-lease', status: 'released' })
    vi.spyOn(localApi, 'beginObservationStream').mockImplementation(async (_session, epoch) => recordedCoverage(epoch, 'start'))
    vi.spyOn(localApi, 'endObservationStream').mockImplementation(async (_session, epoch) => recordedCoverage(epoch, 'stop'))
    vi.spyOn(localApi, 'startObservationQualification').mockResolvedValue({
      status: 'qualifying', scene_quality: 'not_evaluated', person_baseline: 'not_evaluated', reason: '正在确认取景。', source: 'renderer',
      attempt_count: 1, qualified_at: null, updated_at: '2026-08-28T00:00:00Z', raw_frame_stored: false, longitudinal_inference_enabled: false,
    })
    vi.spyOn(localApi, 'analyzeObservation').mockResolvedValue({
      state: 'unknown', confidence: 0, reason: '等待人物基线。', duration_ms: 0, model_version: 'opencv_rules_v1', shadow: false,
      observation_ready: false, raw_frame_stored: false, intervention: null,
      scene_quality: { status: 'ready', message: '取景可用。' }, person_baseline: { status: 'awaiting_person', message: '等待人物基线。' }, metrics: {},
    } as never)
    const quality = vi.spyOn(localApi, 'recordRendererObservationQuality').mockResolvedValue({} as never)
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/jpeg;base64,synthetic')
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      drawImage: vi.fn(), getImageData: () => {
        const pixels = new Uint8ClampedArray(320 * 180 * 4)
        for (let index = 0; index < pixels.length; index += 4) {
          const value = (index / 4) % 2 ? 110 : 170
          pixels[index] = value; pixels[index + 1] = value; pixels[index + 2] = value; pixels[index + 3] = 255
        }
        return { data: pixels, width: 320, height: 180 } as ImageData
      },
    } as unknown as CanvasRenderingContext2D)

    const view = render(<LocalObservation sessionId="session-still" active enabled snoozeUntil={null} />)
    await vi.waitFor(() => expect(localApi.startObservationQualification).toHaveBeenCalledWith('session-still', expect.any(AbortSignal)))
    const video = view.container.querySelector('video')!
    Object.defineProperties(video, {
      readyState: { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA },
      videoWidth: { configurable: true, value: 640 }, videoHeight: { configurable: true, value: 360 },
    })
    fireEvent.loadedData(video)
    let decodedFrames = 1
    Object.defineProperty(video, 'getVideoPlaybackQuality', { configurable: true, value: () => ({ totalVideoFrames: decodedFrames, droppedVideoFrames: 0 }) })
    for (let sample = 0; sample < 17; sample += 1) {
      decodedFrames += 20
      await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    }

    expect(quality).not.toHaveBeenCalled()
    expect(stop).not.toHaveBeenCalled()
    expect(release).not.toHaveBeenCalledWith('still-lease', 'frame_health_unusable')
    expect(video.srcObject).toBe(stream)
    expect(screen.queryByText(/连续画面几乎没有变化/)).not.toBeInTheDocument()
    expect(screen.queryByText('观察摄像头已连接 · 画面不可用')).not.toBeInTheDocument()
  })

  it('lets a child reset only the local reminder preference from an active session', async () => {
    vi.spyOn(localApi, 'resetSessionBehaviorPersonalization').mockResolvedValue({ minimum_feedback_samples: 4, boundary: '只会降低提醒频率。', items: [] })
    render(<LocalObservation sessionId="session-preference" active enabled={false} snoozeUntil={null} />)
    fireEvent.click(screen.getByRole('button', { name: '重置提示偏好' }))
    await waitFor(() => expect(localApi.resetSessionBehaviorPersonalization).toHaveBeenCalledWith('session-preference'))
    expect(await screen.findByText(/已重置提示偏好/)).toBeInTheDocument()
  })

  it('restores a persisted reminder and saves the child response without showing its internal id', async () => {
    vi.spyOn(localApi, 'pendingSessionBehaviorIntervention').mockResolvedValue({
      status: 'pending', intervention: {
        intervention_id: 'never-render-this-id', message: '要不要把卡住的一步圈出来？',
        expires_at: '2026-08-28T10:15:00Z', requires_child_feedback: true,
      },
    })
    const respond = vi.spyOn(localApi, 'respondToSessionBehaviorIntervention').mockResolvedValue({
      intervention_id: 'never-render-this-id', feedback: 'declined',
    })
    render(<LocalObservation sessionId="session-reminder" active enabled={false} snoozeUntil={null} />)

    expect(await screen.findByText('要不要把卡住的一步圈出来？')).toBeInTheDocument()
    expect(screen.queryByText('never-render-this-id')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '这类提醒少一点' }))
    await waitFor(() => expect(respond).toHaveBeenCalledWith('session-reminder', 'never-render-this-id', false, 'less_like_this', expect.any(String)))
    expect(screen.getByText(/这类提醒会更少/)).toBeInTheDocument()
  })

  it('reuses the same feedback action after a lost response so retry cannot create a second answer', async () => {
    vi.spyOn(localApi, 'pendingSessionBehaviorIntervention').mockResolvedValue({
      status: 'pending', intervention: {
        intervention_id: 'persistent-prompt-id', message: '要不要先圈出卡住的一步？',
        expires_at: '2026-08-28T10:15:00Z', requires_child_feedback: true,
      },
    })
    const respond = vi.spyOn(localApi, 'respondToSessionBehaviorIntervention')
      .mockRejectedValueOnce(new ApiError('网络响应没有回来'))
      .mockResolvedValueOnce({ intervention_id: 'persistent-prompt-id', feedback: 'accepted' })
    render(<LocalObservation sessionId="session-retry-reminder" active enabled={false} snoozeUntil={null} />)
    await screen.findByText('要不要先圈出卡住的一步？')
    fireEvent.click(screen.getByRole('button', { name: '愿意' }))
    await waitFor(() => expect(screen.getByText(/回答没有保存/)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '核对原回答' }))
    await waitFor(() => expect(respond).toHaveBeenCalledTimes(2))
    expect(respond.mock.calls[0][4]).toBe(respond.mock.calls[1][4])
    expect(screen.getByText(/愿意试一试/)).toBeInTheDocument()
  })

  it('does not reopen old observation-camera reminders when a saved study session is restored', async () => {
    const pending = vi.spyOn(localApi, 'pendingSessionBehaviorIntervention')
      .mockResolvedValue({ status: 'pending', intervention: {
        intervention_id: 'retired-room-camera-card', message: '要不要先圈出卡住的一步？',
        expires_at: '2026-08-28T10:15:00Z', requires_child_feedback: true, trigger_source: 'local_observation',
      } })
    await act(async () => { render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />) })
    expect(screen.queryByText('要不要先圈出卡住的一步？')).not.toBeInTheDocument()
    expect(pending).not.toHaveBeenCalled()
  })

  it('removes a stale card after refresh recovery reports no pending reminder', async () => {
    vi.spyOn(localApi, 'pendingSessionBehaviorIntervention')
      .mockResolvedValueOnce({ status: 'pending', intervention: {
        intervention_id: 'lost-event-card-id', message: '上次提醒仍在屏幕上。',
        expires_at: '2026-08-28T10:15:00Z', requires_child_feedback: true,
      } })
      .mockResolvedValueOnce({ status: 'no_pending' })
    const first = render(<LocalObservation sessionId="session-lost-event" active enabled={false} snoozeUntil={null} />)
    expect(await screen.findByText('上次提醒仍在屏幕上。')).toBeInTheDocument()
    // A session reload preserves the mounted component while its durable query
    // changes from pending to no_pending; stale React state must still be cleared.
    first.rerender(<LocalObservation sessionId="session-lost-event-reloaded" active enabled={false} snoozeUntil={null} />)
    await waitFor(() => expect(screen.queryByText('上次提醒仍在屏幕上。')).not.toBeInTheDocument())
  })

  it('returns from the completion page without treating the stale parent workspace as active', () => {
    const onBack = vi.fn()
    render(<SessionWorkspace initial={{
      ...workspace,
      session: { ...workspace.session, status: 'completed', state: 'completed' },
      drafts: {}
    }} childName="小辰" childGrade="初三" onBack={onBack} />)

    fireEvent.click(screen.getByRole('button', { name: '立即回到首页' }))

    expect(onBack).toHaveBeenCalledWith(false)
  })

  it('automatically returns to household wake standby after completion', async () => {
    vi.useFakeTimers()
    const onBack = vi.fn()
    render(<SessionWorkspace initial={{
      ...workspace,
      session: { ...workspace.session, status: 'completed', state: 'completed' },
      drafts: {}
    }} childName="小辰" childGrade="初三" onBack={onBack} />)

    expect(screen.getByText(/即将自动回到家庭待机/)).toBeInTheDocument()
    await vi.advanceTimersByTimeAsync(8_000)
    expect(onBack).toHaveBeenCalledWith(false)
    vi.useRealTimers()
  })

  it('confirms a usable StrictMode camera frame once and reports lease release after pausing', async () => {
    const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
    const device = {
      kind: 'videoinput' as const,
      deviceId: 'strict-mode-camera',
      groupId: 'strict-mode-group',
      label: 'Strict Mode Camera',
    }
    const stop = vi.fn()
    const track = {
      label: device.label,
      readyState: 'live',
      getSettings: () => ({ deviceId: device.deviceId, groupId: device.groupId, width: 640, height: 360, frameRate: 15 }),
      getCapabilities: () => ({}),
      addEventListener: vi.fn(),
      stop,
    } as unknown as MediaStreamTrack
    const stream = {
      getVideoTracks: () => [track],
      getTracks: () => [track],
    } as unknown as MediaStream
    const getUserMedia = vi.fn().mockResolvedValue(stream)
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: vi.fn().mockResolvedValue([device]), getUserMedia },
    })
    const bindingHash = await mediaDeviceInfoHash({ ...device, deviceId: 'previous-electron-profile-camera-id' })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([{
      device_type: 'camera', system_id_hash: bindingHash, label: device.label,
      capabilities: {}, verified_at: '2026-08-27T00:00:00Z', updated_at: '2026-08-27T00:00:00Z',
    }])
    const register = vi.spyOn(localApi, 'registerDevice').mockResolvedValue({
      lease_id: 'lease-current', status: 'active', expires_at: '2026-08-27T00:03:00Z',
    })
    let resolveQualification: ((value: Awaited<ReturnType<typeof localApi.startObservationQualification>>) => void) | undefined
    const startQualification = vi.spyOn(localApi, 'startObservationQualification').mockImplementation(() => new Promise((resolve) => { resolveQualification = resolve }))
    const analyzeObservation = vi.spyOn(localApi, 'analyzeObservation')
    const release = vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'lease-current', status: 'released' })
    const incident = vi.spyOn(localApi, 'recordDeviceIncident').mockResolvedValue({} as never)
    vi.spyOn(localApi, 'beginObservationStream').mockImplementation(async (_session, epoch) => recordedCoverage(epoch, 'start'))
    vi.spyOn(localApi, 'endObservationStream').mockImplementation(async (_session, epoch) => recordedCoverage(epoch, 'stop'))
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(mockSuccessfulMediaPlay)

    const rendered = render(<StrictMode><LocalObservation sessionId="session-camera" active enabled snoozeUntil={null} /></StrictMode>)
    await waitFor(() => expect(register).toHaveBeenCalledTimes(1))
    const video = rendered.container.querySelector('video')!
    Object.defineProperties(video, {
      readyState: { configurable: true, value: HTMLMediaElement.HAVE_CURRENT_DATA },
      videoWidth: { configurable: true, value: 640 },
      videoHeight: { configurable: true, value: 360 },
    })
    fireEvent.loadedData(video)
    await waitFor(() => expect(startQualification).toHaveBeenCalledWith('session-camera', expect.any(AbortSignal)))
    expect(analyzeObservation).not.toHaveBeenCalled()
    vi.spyOn(localApi, 'pauseObservation').mockResolvedValue({ ...workspace.session, observation_status: 'paused' })
    fireEvent.click(screen.getByTitle('暂停本地观察'))
    await waitFor(() => expect(release).toHaveBeenCalledWith('lease-current', 'voice_camera_pause', expect.any(AbortSignal)))
    await act(async () => resolveQualification?.({
      status: 'qualifying', scene_quality: 'not_evaluated', person_baseline: 'not_evaluated',
      reason: '正在确认取景。', source: 'renderer', attempt_count: 1, qualified_at: null,
      updated_at: '2026-08-28T00:00:00Z', raw_frame_stored: false, longitudinal_inference_enabled: false,
    }))
    await waitFor(() => expect(screen.getByText('本地观察已暂停')).toBeInTheDocument())
    expect(screen.queryByText(/本地观察已开启/)).not.toBeInTheDocument()

    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(getUserMedia).toHaveBeenCalledWith(expect.objectContaining({
      video: expect.objectContaining({ deviceId: { exact: 'strict-mode-camera' } }),
    }))
    expect(register).toHaveBeenCalledTimes(1)
    expect(register).toHaveBeenCalledWith('session-camera', 'camera', bindingHash, expect.any(Object))
    expect(startQualification).toHaveBeenCalledWith('session-camera', expect.any(AbortSignal))
    expect(incident).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByText(/应用已确认释放摄像头/)).toBeInTheDocument())
    expect(release).toHaveBeenCalledWith('lease-current', 'voice_camera_pause', expect.any(AbortSignal))
    rendered.unmount()
    expect(stop).toHaveBeenCalled()
    if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
    else Reflect.deleteProperty(navigator, 'mediaDevices')
  })

  it('separates the child statement from the system candidate and shows its evidence boundary', () => {
    render(<SessionWorkspace initial={{
      ...workspace,
      recalls: [
        {
          recall_id: 'statement-1', subject: '数学', summary: '老师讲了判别式。', confidence: 1,
          confirmation_status: 'child_confirmed', record_kind: 'child_statement', candidate_nodes: [],
          textbook_mapping: null, evidence_sources: ['孩子口述'], created_at: '2026-08-26T10:05:00Z'
        },
        {
          recall_id: 'candidate-1', subject: '数学', summary: '可能相关的知识点：根的判别式。', confidence: 0.55,
          confirmation_status: 'inferred', record_kind: 'system_candidate',
          candidate_nodes: [{ title: '根的判别式' }],
          textbook_mapping: { status: 'course_index_only', label: '本地课程索引，未绑定教材页码' },
          evidence_sources: ['孩子口述', '本地课程索引'], created_at: '2026-08-26T10:05:00Z'
        }
      ]
    }} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    expect(screen.getByText('老师讲了判别式。')).toBeInTheDocument()
    expect(screen.getByText(/候选知识点：根的判别式 · 本地课程索引，未绑定教材页码 · 依据：孩子口述、本地课程索引/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确实讲了' })).toBeInTheDocument()
  })

  it('restores a classroom draft and autosaves the latest edit after the debounce', async () => {
    vi.useFakeTimers()
    const saveDraft = vi.spyOn(localApi, 'saveSessionDraft').mockResolvedValue({
      section: 'classroom_recall',
      payload: {},
      updated_at: '2026-08-26T10:04:00Z'
    })

    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    const recall = screen.getByPlaceholderText('不用说得很完整，想到什么就写什么。')
    expect(recall).toHaveValue('今天讲了相似三角形')
    expect(screen.getByText('已恢复并保存草稿')).toBeInTheDocument()

    fireEvent.change(recall, { target: { value: '今天讲了相似三角形的判定方法' } })
    expect(screen.getByText('正在保存草稿')).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(650) })

    expect(saveDraft).toHaveBeenCalledTimes(1)
    expect(saveDraft).toHaveBeenCalledWith('session-draft-1', 'classroom_recall', {
      subject: '数学',
      summary: '今天讲了相似三角形的判定方法'
    })
    expect(screen.getByText('已恢复并保存草稿')).toBeInTheDocument()
  })

  it('clears the persisted draft after the recalled content is committed', async () => {
    vi.spyOn(localApi, 'addRecall').mockResolvedValue({
      recall_id: 'recall-1', subject: '数学', summary: '今天讲了相似三角形', confidence: 1,
      confirmation_status: 'child_confirmed', record_kind: 'child_statement', candidate_nodes: [],
      textbook_mapping: null, evidence_sources: ['孩子口述'], created_at: '2026-08-26T10:05:00Z'
    })
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue({
      ...workspace,
      recalls: [{
        recall_id: 'recall-1', subject: '数学', summary: '今天讲了相似三角形', confidence: 1,
        confirmation_status: 'child_confirmed', record_kind: 'child_statement', candidate_nodes: [],
        textbook_mapping: null, evidence_sources: ['孩子口述'], created_at: '2026-08-26T10:05:00Z'
      }],
      drafts: {}
    })
    const clearDraft = vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({
      section: 'classroom_recall', deleted: true
    })

    render(<SessionWorkspace initial={workspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /记下来/ }))

    await waitFor(() => expect(clearDraft).toHaveBeenCalledWith('session-draft-1', 'classroom_recall'))
    expect(screen.getByPlaceholderText('不用说得很完整，想到什么就写什么。')).toHaveValue('')
  })

  it('queues every completed child utterance in order and only speaks after catching up', async () => {
    const makeTurn = (id: string, transcript: string, response: string): AgentTurn => ({
      agent_turn_id: id,
      transcript,
      intent: 'supportive_chat',
      response_text: response,
      action: { type: 'none' },
      orchestrator_version: 'hybrid_companion_v1',
      tool_status: 'pending',
      tool_error_code: null,
      completed_at: null,
      created_at: '2026-08-26T10:05:00Z',
    })
    const firstTurn = makeTurn('agent-turn-first', '第一句话', '较旧的回复')
    const secondTurn = makeTurn('agent-turn-second', '第二句话', '第二句的回复')
    const thirdTurn = makeTurn('agent-turn-third', '第三句话', '这是最后的回复')
    let resolveFirst!: (turn: AgentTurn) => void
    const firstRequest = new Promise<AgentTurn>((resolve) => { resolveFirst = resolve })
    const agentTurn = vi.spyOn(localApi, 'agentTurn')
      .mockReturnValueOnce(firstRequest)
      .mockResolvedValueOnce(secondTurn)
      .mockResolvedValueOnce(thirdTurn)
    const turns = new Map([[firstTurn.agent_turn_id, firstTurn], [secondTurn.agent_turn_id, secondTurn], [thirdTurn.agent_turn_id, thirdTurn]])
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async (turnId) => turns.get(turnId)!)
    vi.spyOn(localApi, 'saveSessionDraft').mockResolvedValue({
      section: 'agent_command', payload: {}, updated_at: '2026-08-26T10:04:00Z',
    })
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const spoken: string[] = []
    class FakeUtterance {
      text: string
      voice: SpeechSynthesisVoice | null = null
      constructor(text: string) { this.text = text }
    }
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    vi.stubGlobal('speechSynthesis', {
      cancel: vi.fn(),
      getVoices: () => [offlineChineseVoice],
      speak: (utterance: FakeUtterance) => { spoken.push(utterance.text) },
    })

    await act(async () => { render(<SessionWorkspace initial={{ ...workspace, drafts: {} }} childName="小辰" childGrade="初三" onBack={vi.fn()} />) })
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '第一句话' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(agentTurn).toHaveBeenCalledWith(workspace.session.session_id, '第一句话'))

    fireEvent.change(input, { target: { value: '第二句话' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.change(input, { target: { value: '第三句话' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(agentTurn).toHaveBeenCalledTimes(1)
    expect(screen.getByText('听到了，已按顺序排队2句。我会逐句处理，不会漏掉。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '已排队 2 句' })).toBeInTheDocument()
    await act(async () => { resolveFirst(firstTurn); await firstRequest })
    await waitFor(() => expect(agentTurn).toHaveBeenCalledTimes(3))
    expect(agentTurn.mock.calls.map((call) => call[1])).toEqual(['第一句话', '第二句话', '第三句话'])
    await waitFor(() => expect(complete).toHaveBeenCalledTimes(3))
    expect(await screen.findByText('这是最后的回复')).toBeInTheDocument()
    expect(spoken).toEqual(['这是最后的回复'])
    expect(spoken).not.toContain('较旧的回复')
    expect(spoken).not.toContain('第二句的回复')
  })

  it('dispatches spoken pause and defer actions without task controls', async () => {
    const activeWorkspace: SessionWorkspaceState = {
      ...studyWorkspace,
      plan_items: [{
        ...studyWorkspace.plan_items[0],
        status: 'active',
        started_at: '2026-08-26T10:05:00Z',
        active_segment_started_at: '2026-08-26T10:05:00Z',
      }],
    }
    const makeTurn = (id: string, transcript: string, action: AgentTurn['action']): AgentTurn => ({
      agent_turn_id: id,
      transcript,
      intent: action.type === 'pause_active_task' ? 'pause_task' : 'defer_task',
      response_text: action.type === 'pause_active_task' ? '已暂停。' : '已延期。',
      action,
      orchestrator_version: 'hybrid_companion_v1',
      tool_status: 'pending',
      tool_error_code: null,
      completed_at: null,
      created_at: '2026-08-26T10:05:00Z',
    })
    vi.spyOn(localApi, 'agentTurn')
      .mockResolvedValueOnce(makeTurn('voice-pause', '先暂停这项', { type: 'pause_active_task' }))
      .mockResolvedValueOnce(makeTurn('voice-defer', '这项明天再做', { type: 'reschedule_current_task', reason: '这项明天再做' }))
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    const pause = vi.spyOn(localApi, 'pausePlanItem').mockResolvedValue()
    const defer = vi.spyOn(localApi, 'reschedulePlanItem').mockResolvedValue()
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(activeWorkspace)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={activeWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '先暂停这项' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(pause).toHaveBeenCalledWith('plan-item-1'))

    fireEvent.change(input, { target: { value: '这项明天再做' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(defer).toHaveBeenCalledWith('plan-item-1', '这项明天再做'))
  })

  it('starts and ends bounded companion activities through the durable service', async () => {
    const makeTurn = (id: string, transcript: string, action: AgentTurn['action']): AgentTurn => ({
      agent_turn_id: id, transcript, intent: action.type,
      response_text: action.type === 'start_companion_activity' ? '我们开始一起编故事。' : '故事到这里，回到刚才的任务。',
      action, orchestrator_version: 'hybrid_companion_v1', tool_status: 'pending',
      tool_error_code: null, completed_at: null, created_at: '2026-08-26T10:05:00Z',
    })
    vi.spyOn(localApi, 'agentTurn')
      .mockResolvedValueOnce(makeTurn('voice-story-start', '一起编故事', {
        type: 'start_companion_activity', activity_type: 'interactive_story', topic: '一起编故事',
      }))
      .mockResolvedValueOnce(makeTurn('voice-story-end', '故事结束', { type: 'end_companion_activity' }))
    const start = vi.spyOn(localApi, 'startCompanionActivity').mockResolvedValue({ status: 'active' })
    const end = vi.spyOn(localApi, 'endCompanionActivity').mockResolvedValue({ status: 'completed' })
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(studyWorkspace)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '一起编故事' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(start).toHaveBeenCalledWith(
      studyWorkspace.session.session_id, 'interactive_story', '一起编故事', 'voice-story-start'
    ))

    fireEvent.change(input, { target: { value: '故事结束' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(end).toHaveBeenCalledWith(studyWorkspace.session.session_id))
  })

  it('plays only backend-approved local content and stops the exact activity without automatic task resume', async () => {
    const makeTurn = (id: string, transcript: string, action: AgentTurn['action']): AgentTurn => ({
      agent_turn_id: id, transcript, intent: action.type, response_text: '受控内容操作', action,
      orchestrator_version: 'hybrid_companion_v1', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: '2026-09-01T10:00:00Z',
    })
    vi.spyOn(localApi, 'agentTurn')
      .mockResolvedValueOnce(makeTurn('controlled-start-turn', '播放分数视频', { type: 'start_controlled_content', query: '分数' }))
      .mockResolvedValueOnce(makeTurn('controlled-stop-turn', '停止视频', { type: 'stop_controlled_content' }))
    const playback = {
      content_session_id: 'content-session-1', child_id: studyWorkspace.session.child_id,
      session_id: studyWorkspace.session.session_id, curriculum_row_id: 7, title: '分数的意义',
      local_path: '/tmp/approved-fraction.mp4', status: 'active' as const, allowed_minutes: 6,
      actual_seconds: 0, paused_plan_item_id: 'plan-item-1', started_at: new Date().toISOString(),
      ends_at: new Date(Date.now() + 360_000).toISOString(), ended_at: null, end_reason: null,
      remaining_seconds: 360,
    }
    const start = vi.spyOn(localApi, 'startControlledContent').mockResolvedValue(playback)
    const end = vi.spyOn(localApi, 'endControlledContent').mockResolvedValue({ ...playback, status: 'stopped', remaining_seconds: 0, task_resumed: false })
    vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({ playback: null })
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(studyWorkspace)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const open = vi.fn().mockResolvedValue({ opened: true })
    const close = vi.fn().mockResolvedValue({ closed: true, sessionId: playback.content_session_id })
    const prepare = nativeLaunchFixture()
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
      prepareControlledLaunch: prepare, listControlledLaunches: vi.fn().mockResolvedValue([]),
      openControlledMedia: open, closeControlledMedia: close,
      onControlledMediaClosed: vi.fn(() => vi.fn()),
    }))

    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '播放分数视频' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1))
    expect(prepare).toHaveBeenCalledWith({kind:'video',childId:studyWorkspace.session.child_id,
      sessionId:studyWorkspace.session.session_id,requestId:'controlled-start-turn'})
    const intent = await prepare.mock.results[0].value
    expect(start).toHaveBeenCalledWith(studyWorkspace.session.session_id, '分数', 'controlled-start-turn', undefined, intent.intentId, undefined)
    await waitFor(() => expect(open).toHaveBeenCalledWith({ path: playback.local_path, title: playback.title, maxSeconds: 360, contentSessionId: playback.content_session_id, nativeIntentId:intent.intentId }))

    fireEvent.change(input, { target: { value: '停止视频' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(close).toHaveBeenCalledWith('child_requested', playback.content_session_id))
    await waitFor(() => expect(end).toHaveBeenCalledWith(studyWorkspace.session.session_id, 'child_requested', expect.any(Number), playback.content_session_id))
  })

  it('retries the same deferred task after a lost receipt and a workspace remount', async () => {
    const first = {source_plan_item_id:'task-a', source_version:'a'.repeat(64), source_session_id:'old-session',
      source_date:'2026-09-08', subject:'数学', title:'原作业', expected_minutes:10,
      task_type:'written' as const, range_text:null, due_at:null, special_requirements:null, reschedule_reason:'时间不够'}
    const second = {...first, source_plan_item_id:'task-b', title:'另一份作业'}
    const initial: SessionWorkspaceState = {...workspace, session:{...workspace.session, state:'homework_capture'},
      carryover_candidates:[first, second], drafts:{}}
    let turnIndex = 0
    vi.spyOn(localApi, 'agentTurn').mockImplementation(async () => ({
      agent_turn_id:`carryover-turn-${++turnIndex}`, transcript:'今天继续', intent:'carryover_continue_today',
      response_text:'好', action:{type:'review_first_carryover', decision:'continue_today'},
      orchestrator_version:'hybrid_companion_v1', tool_status:'pending', tool_error_code:null,
      completed_at:null, created_at:new Date().toISOString(),
    }))
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    const reason = '孩子语音确认今天继续完成'
    const receipt = {review_id:'review-a', source_plan_item_id:'task-a', source_session_id:'old-session',
      child_id:initial.session.child_id, target_session_id:initial.session.session_id,
      decision:'continue_today' as const, reason, target_homework_id:'homework-a', reviewed_at:new Date().toISOString()}
    const review = vi.spyOn(localApi, 'reviewDeferredTask')
      .mockRejectedValueOnce(new Error('response lost after commit')).mockResolvedValue(receipt)
    const after = {...initial, carryover_candidates:[second]}
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(after)
    const view = render(<SessionWorkspace initial={initial} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const send = () => {
      const input = screen.getByLabelText('对学习伙伴说')
      fireEvent.change(input, {target:{value:'今天继续'}})
      fireEvent.keyDown(input, {key:'Enter'})
    }
    send()
    await waitFor(() => expect(complete).toHaveBeenCalled())
    expect(review.mock.calls[0]?.[1]).toBe('task-a')
    view.unmount()
    render(<SessionWorkspace initial={after} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    send()
    await waitFor(() => expect(review).toHaveBeenCalledTimes(2))
    expect(review.mock.calls[1]).toEqual(review.mock.calls[0])
    expect(review.mock.calls[1]?.[4]).toBe('carryover-turn-1:carryover')
    await waitFor(() => expect(complete).toHaveBeenCalledTimes(2))
  })

  it.each(['video', 'game'])('retains an active %s session after reload until native closure is confirmed', async kind => {
    const active = {content_session_id:'media-old', game_session_id:'game-old',
      child_id:studyWorkspace.session.child_id, session_id:studyWorkspace.session.session_id,
      started_at:new Date().toISOString(), status:'active'}
    const closed = vi.fn().mockResolvedValue({closed:false})
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({[kind === 'video' ? 'closeControlledMedia' : 'closeControlledGame']:closed}))
    vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({playback:kind === 'video' ? active as never : null})
    vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({game:kind === 'game' ? active as never : null})
    const end = kind === 'video' ? vi.spyOn(localApi, 'endControlledContent').mockResolvedValue({} as never)
      : vi.spyOn(localApi, 'endControlledGame').mockResolvedValue({} as never)
    const view = render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(closed).toHaveBeenCalledWith('renderer_reloaded', kind === 'video' ? active.content_session_id : active.game_session_id)
    expect(end).not.toHaveBeenCalled()
    view.unmount()
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(end).not.toHaveBeenCalled()
  })

  it('reads a changed original locally and excludes an already queued confirmation before accepting a new one', async () => {
    const first = {source_plan_item_id:'task-a', source_version:'a'.repeat(64), source_session_id:'old-session',
      source_date:'2026-09-08', subject:'数学', title:'原作业', expected_minutes:10, task_type:'written' as const,
      range_text:null, due_at:null, special_requirements:null, reschedule_reason:'时间不够'}
    const revised = {...first, source_version:'b'.repeat(64), title:'新的第六页作业'}
    const other = {...first, source_plan_item_id:'task-b', title:'不要误处理的作业'}
    const initial: SessionWorkspaceState = {...workspace, session:{...workspace.session, state:'homework_capture'},
      carryover_candidates:[first, other], drafts:{}}
    const after = {...initial, carryover_candidates:[other, revised]}
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(after)
    const speech = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => undefined)
    let count = 0
    vi.spyOn(localApi, 'agentTurn').mockImplementation(async () => ({
      agent_turn_id:`reconfirm-turn-${++count}`, transcript:'今天继续', intent:'carryover_continue_today',
      response_text:'好', action:{type:'review_first_carryover', decision:'continue_today'},
      orchestrator_version:'hybrid_companion_v1', tool_status:'pending', tool_error_code:null,
      completed_at:null, created_at:new Date().toISOString(),
    }))
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    let rejectFirst!: (error: Error) => void
    const review = vi.spyOn(localApi, 'reviewDeferredTask')
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
      .mockResolvedValue({review_id:'review-a', source_plan_item_id:'task-a', source_session_id:'old-session',
        child_id:initial.session.child_id, target_session_id:initial.session.session_id, decision:'continue_today',
        reason:'孩子语音确认今天继续完成', target_homework_id:'homework-a', reviewed_at:new Date().toISOString()})
    render(<SessionWorkspace initial={initial} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const send = () => {
      const input = screen.getByLabelText('对学习伙伴说')
      fireEvent.change(input, {target:{value:'今天继续'}})
      fireEvent.keyDown(input, {key:'Enter'})
    }
    send()
    await waitFor(() => expect(review).toHaveBeenCalledTimes(1))
    send()
    await act(async () => rejectFirst(new ApiError('原内容变更', 409, 'DEFERRED_TASK_SOURCE_CHANGED', false, undefined,
      {not_applied:true, session_id:initial.session.session_id, child_id:initial.session.child_id,
        client_action_id:'reconfirm-turn-1:carryover', candidate:revised})))
    await waitFor(() => expect(complete).toHaveBeenCalledTimes(2))
    expect(review).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(speech.mock.calls.filter(([text]) => text.includes('新的第六页作业'))).toHaveLength(2))
    const readbacks = speech.mock.calls.filter(([text]) => text.includes('新的第六页作业'))
    expect(readbacks.length).toBeGreaterThan(0)
    expect(readbacks.at(-1)?.[4]?.localOnly).toBe(true)
    expect(complete.mock.calls.every(call => !call[1]?.includes('新的第六页作业'))).toBe(true)
    await act(async () => { readbacks.at(-1)?.[4]?.onPlaybackEnd?.() })
    send()
    await waitFor(() => expect(review).toHaveBeenCalledTimes(2))
    expect(review.mock.calls[1]?.[1]).toBe('task-a')
    expect(review.mock.calls[1]?.[4]).toBe('reconfirm-turn-3:carryover')
    expect(review.mock.calls[1]?.[5]).toBe(revised.source_version)
    await waitFor(() => expect(complete).toHaveBeenCalledTimes(3))
  })

  it.each(['reply_arrives', 'reply_lost', 'runtime_refresh', 'reply_lost_current'])('preserves a dispatched completion across ownership or reply loss: %s', async phase => {
    const turn = {agent_turn_id:'completion-owner', transcript:'你好', intent:'chat', action:{type:'none'},
      response_text:'你好', orchestrator_version:'hybrid_companion_v1', tool_status:'pending',
      tool_error_code:null, completed_at:null, created_at:new Date().toISOString()} as AgentTurn
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    let resolve!: () => void, reject!: (error: Error) => void
    const delayed = new Promise<void>((yes, no) => { resolve = yes; reject = no })
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async () => {
      if (phase !== 'runtime_refresh') await delayed
      return {...turn, tool_status:'completed'}
    })
    const runtime = vi.spyOn(localApi, 'agentRuntimeStatus').mockImplementation(async () => {
      if (phase === 'runtime_refresh' && complete.mock.calls.length) await delayed
      return null as never
    })
    const speak = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => undefined)
    const view = render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), {target:{value:'你好'}})
    fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'), {key:'Enter'})
    await waitFor(() => expect(complete).toHaveBeenCalledTimes(1))
    if (phase === 'runtime_refresh') await waitFor(() => expect(runtime.mock.calls.length).toBeGreaterThan(1))
    if (phase !== 'reply_lost_current') view.unmount()
    await act(async () => {
      if (phase.startsWith('reply_lost')) reject(new Error('SYNTHETIC_RECEIPT_REPLY_LOST'))
      else resolve()
      for (let index = 0; index < 12; index++) await Promise.resolve()
    })
    expect(complete).toHaveBeenCalledTimes(1)
    expect(complete.mock.calls[0]?.[2]).toBe('completed')
    if (phase === 'reply_lost_current') {
      expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('你好')
      expect(screen.getAllByText(/原操作的结果回执尚未核对/)).not.toHaveLength(0)
    } else expect(speak).not.toHaveBeenCalled()
  })

  it.each(['video', 'game'].flatMap(kind => ['active', 'stopped'].map(status => ({kind, status}))))(
    'does not relaunch a recorded $kind start receipt in $status state', async ({kind, status}) => {
    const action: AgentTurn['action'] = {type:kind === 'video' ? 'start_controlled_content' : 'start_controlled_game', query:'math'}
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({agent_turn_id:'recorded-start', transcript:'打开数学',
      intent:action.type, action, runtime_execution_location:'client_device_bridge',
      runtime_capability:kind === 'video' ? 'controlled_content.play' : 'controlled_game.play',
      response_text:'开始播放', orchestrator_version:'hybrid_companion_v1', tool_status:'pending',
      tool_error_code:null, completed_at:null, created_at:new Date().toISOString()})
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(studyWorkspace)
    vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({playback:null})
    vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({game:null})
    const historical = {content_session_id:'old-media', game_session_id:'old-game', title:'math',
      child_id:studyWorkspace.session.child_id, session_id:studyWorkspace.session.session_id,
      local_path:'/tmp/approved.mp4', executable_path:'C:\\Math.exe', remaining_seconds:60, allowed_minutes:1,
      started_at:new Date().toISOString(), status, replayed:true, launch_allowed:false}
    if (kind === 'video') vi.spyOn(localApi, 'startControlledContent').mockResolvedValue(historical as never)
    else vi.spyOn(localApi, 'startControlledGame').mockResolvedValue(historical as never)
    const end = kind === 'video' ? vi.spyOn(localApi, 'endControlledContent').mockResolvedValue({} as never)
      : vi.spyOn(localApi, 'endControlledGame').mockResolvedValue({} as never)
    const open = vi.fn().mockResolvedValue({opened:true, processId:911})
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({prepareControlledLaunch:nativeLaunchFixture(), listControlledLaunches:vi.fn().mockResolvedValue([]),
      [kind === 'video' ? 'openControlledMedia' : 'openControlledGame']:open,
      [kind === 'video' ? 'closeControlledMedia' : 'closeControlledGame']:vi.fn().mockResolvedValue({closed:true})}))
    const view = render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), {target:{value:'打开数学'}})
    fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'), {key:'Enter'})
    await waitFor(() => expect(complete).toHaveBeenCalled())
    expect(open).not.toHaveBeenCalled()
    expect(complete.mock.calls.at(-1)?.[1]).not.toMatch(/开始播放|开始“/)
    expect(complete.mock.calls.at(-1)?.[0]).toBe('recorded-start')
    expect(complete.mock.calls.at(-1)?.[2], JSON.stringify(complete.mock.calls.at(-1)?.slice(1))).toBe('completed')
    expect(complete.mock.calls.at(-1)?.[4]).toMatchObject({execution_receipt:{output:{
      launch_performed:false, replayed:true, status,
      [kind === 'video' ? 'content_session_id' : 'game_session_id']:kind === 'video' ? 'old-media' : 'old-game',
    }}})
    view.unmount()
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(end).not.toHaveBeenCalled()
  })

  it.each(['video', 'game'])('ignores a closed event for another %s activity', async kind => {
    const action: AgentTurn['action'] = {type:kind === 'video' ? 'start_controlled_content' : 'start_controlled_game', query:'math'}
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({agent_turn_id:'scoped-start', transcript:'打开数学',
      intent:action.type, action, response_text:'开始播放', orchestrator_version:'hybrid_companion_v1', tool_status:'pending',
      tool_error_code:null, completed_at:null, created_at:new Date().toISOString()})
    vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({playback:null})
    vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({game:null})
    const active = {content_session_id:'current-video', game_session_id:'current-game', title:'math',
      child_id:studyWorkspace.session.child_id, session_id:studyWorkspace.session.session_id,
      local_path:'/tmp/approved.mp4', executable_path:'C:\\Math.exe', remaining_seconds:60, allowed_minutes:1,
      started_at:new Date().toISOString(), status:'active'}
    if (kind === 'video') vi.spyOn(localApi, 'startControlledContent').mockResolvedValue(active as never)
    else {
      vi.spyOn(localApi, 'startControlledGame').mockResolvedValue(active as never)
      vi.spyOn(localApi, 'bindControlledGameProcess').mockResolvedValue(active as never)
    }
    const end = kind === 'video' ? vi.spyOn(localApi, 'endControlledContent').mockResolvedValue({...active, status:'stopped'} as never)
      : vi.spyOn(localApi, 'endControlledGame').mockResolvedValue({...active, status:'stopped'} as never)
    let closed!: (reason:string, id:string) => void
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
      prepareControlledLaunch: nativeLaunchFixture(), listControlledLaunches: vi.fn().mockResolvedValue([]),
      [kind === 'video' ? 'openControlledMedia' : 'openControlledGame']:vi.fn().mockResolvedValue({opened:true, processId:911}),
      [kind === 'video' ? 'closeControlledMedia' : 'closeControlledGame']:vi.fn().mockResolvedValue({closed:false}),
      [kind === 'video' ? 'onControlledMediaClosed' : 'onControlledGameClosed']:vi.fn(callback => { closed = callback; return vi.fn() }),
    }))
    const view = render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), {target:{value:'打开数学'}})
    fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'), {key:'Enter'})
    await waitFor(() => expect(complete).toHaveBeenCalled())
    await act(async () => closed('window_closed', 'old-activity'))
    expect(end).not.toHaveBeenCalled()
    await act(async () => closed('window_closed', kind === 'video' ? active.content_session_id : active.game_session_id))
    expect(end).toHaveBeenCalledTimes(1)
    view.unmount()
  })

  it.each(['video', 'game'])('rejects a recovered %s activity belonging to another child before device closure', async kind => {
    const foreign = {content_session_id:'foreign-video', game_session_id:'foreign-game', status:'active',
      child_id:'another-child', session_id:studyWorkspace.session.session_id}
    vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({playback:kind === 'video' ? foreign as never : null})
    vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({game:kind === 'game' ? foreign as never : null})
    const close = vi.fn().mockResolvedValue({closed:true})
    const end = kind === 'video' ? vi.spyOn(localApi, 'endControlledContent').mockResolvedValue({} as never)
      : vi.spyOn(localApi, 'endControlledGame').mockResolvedValue({} as never)
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({[kind === 'video' ? 'closeControlledMedia' : 'closeControlledGame']:close}))
    const view = render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(close).not.toHaveBeenCalled()
    expect(end).not.toHaveBeenCalled()
    view.unmount()
  })

  it.each(['video', 'game'])('does not launch %s after its requesting workspace has unmounted', async kind => {
    const action: AgentTurn['action'] = {type:kind === 'video' ? 'start_controlled_content' : 'start_controlled_game', query:'math'}
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({agent_turn_id:'late-start', transcript:'打开数学',
      intent:action.type, action, response_text:'开始播放', orchestrator_version:'hybrid_companion_v1', tool_status:'pending',
      tool_error_code:null, completed_at:null, created_at:new Date().toISOString()})
    vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({playback:null})
    vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({game:null})
    let resolve!: (value: never) => void
    const start = kind === 'video' ? vi.spyOn(localApi, 'startControlledContent') : vi.spyOn(localApi, 'startControlledGame')
    start.mockImplementation(() => new Promise(done => { resolve = done }) as never)
    const open = vi.fn().mockResolvedValue({opened:true, processId:911})
    const close = vi.fn().mockResolvedValue({closed:false})
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({prepareControlledLaunch:nativeLaunchFixture(), listControlledLaunches:vi.fn().mockResolvedValue([]),
      [kind === 'video' ? 'openControlledMedia' : 'openControlledGame']:open,
      [kind === 'video' ? 'closeControlledMedia' : 'closeControlledGame']:close}))
    const view = render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), {target:{value:'打开数学'}})
    fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'), {key:'Enter'})
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1))
    view.unmount()
    await act(async () => { resolve({content_session_id:'late-media', game_session_id:'late-game', title:'math',
      child_id:studyWorkspace.session.child_id, session_id:studyWorkspace.session.session_id,
      local_path:'/tmp/approved.mp4', executable_path:'C:\\Math.exe', remaining_seconds:60, allowed_minutes:1,
      started_at:new Date().toISOString(), status:'active', launch_allowed:true} as never) })
    expect(open).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
  })

  it.each(['video', 'game'])('does not settle a failed %s launch until the native process is closed', async kind => {
    const action: AgentTurn['action'] = {type:kind === 'video' ? 'start_controlled_content' : 'start_controlled_game', query:'math'}
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({agent_turn_id:'launch-uncertain', transcript:'打开数学',
      intent:action.type, action, response_text:'开始播放', orchestrator_version:'hybrid_companion_v1', tool_status:'pending',
      tool_error_code:null, completed_at:null, created_at:new Date().toISOString()})
    vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({playback:null})
    vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({game:null})
    const active = {content_session_id:'media-new', game_session_id:'game-new', title:'math',
      child_id:studyWorkspace.session.child_id, session_id:studyWorkspace.session.session_id,
      local_path:'/tmp/approved.mp4', executable_path:'C:\\Math.exe', remaining_seconds:60, allowed_minutes:1,
      started_at:new Date().toISOString(), status:'active'}
    if (kind === 'video') vi.spyOn(localApi, 'startControlledContent').mockResolvedValue(active as never)
    else vi.spyOn(localApi, 'startControlledGame').mockResolvedValue(active as never)
    const end = kind === 'video' ? vi.spyOn(localApi, 'endControlledContent').mockResolvedValue({} as never)
      : vi.spyOn(localApi, 'endControlledGame').mockResolvedValue({} as never)
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
      prepareControlledLaunch: nativeLaunchFixture(), listControlledLaunches: vi.fn().mockResolvedValue([]),
      [kind === 'video' ? 'openControlledMedia' : 'openControlledGame']:vi.fn().mockRejectedValue(new Error('launch response lost')),
      [kind === 'video' ? 'closeControlledMedia' : 'closeControlledGame']:vi.fn().mockResolvedValue({closed:false}),
    }))
    const view = render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, {target:{value:'打开数学'}})
    fireEvent.keyDown(input, {key:'Enter'})
    await waitFor(() => expect(complete).toHaveBeenCalled())
    expect(complete.mock.calls.at(-1)?.[2]).toBe('failed')
    expect(end).not.toHaveBeenCalled()
    view.unmount()
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(end).not.toHaveBeenCalled()
  })

  it.each(['video', 'game'].flatMap(kind => ['missing', 'rejected', 'unconfirmed', 'wrong_receipt'].map(failure => ({kind, failure}))))(
    'does not settle or announce $kind closed without a device receipt: $failure', async ({kind, failure}) => {
      const action: AgentTurn['action'] = { type: kind === 'video' ? 'stop_controlled_content' : 'stop_controlled_game' }
      vi.spyOn(localApi, 'agentTurn').mockResolvedValue({
        agent_turn_id: 'stop-unconfirmed', transcript: '停下来', intent: action.type, action,
        response_text: '已经关闭', orchestrator_version: 'hybrid_companion_v1', tool_status: 'pending',
        tool_error_code: null, completed_at: null, created_at: new Date().toISOString(),
      })
      const active = {content_session_id:'stop-video', game_session_id:'stop-game', status:'active',
        child_id:studyWorkspace.session.child_id, session_id:studyWorkspace.session.session_id, started_at:new Date().toISOString()}
      let activeAtCommand = false
      vi.spyOn(localApi, 'activeControlledContent').mockImplementation(async () =>
        ({playback:activeAtCommand && kind === 'video' ? active as never : null}))
      vi.spyOn(localApi, 'activeControlledGame').mockImplementation(async () =>
        ({game:activeAtCommand && kind === 'game' ? active as never : null}))
      vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(studyWorkspace)
      const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
      const end = kind === 'video'
        ? vi.spyOn(localApi, 'endControlledContent').mockResolvedValue({} as never)
        : vi.spyOn(localApi, 'endControlledGame').mockResolvedValue({} as never)
      const close = failure === 'rejected' ? vi.fn().mockRejectedValue(new Error('device disconnected'))
        : vi.fn().mockResolvedValue(failure === 'wrong_receipt' ? {closed:true,sessionId:'another-activity'} : {closed: false})
      vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop(failure === 'missing' ? {} : {
        [kind === 'video' ? 'closeControlledMedia' : 'closeControlledGame']: close,
      }))
      render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
      await act(async () => { await Promise.resolve() })
      activeAtCommand = true
      const input = screen.getByLabelText('对学习伙伴说')
      fireEvent.change(input, {target: {value: '停下来'}})
      fireEvent.keyDown(input, {key: 'Enter'})
      await waitFor(() => expect(complete).toHaveBeenCalled())
      expect(end).not.toHaveBeenCalled()
      if (failure !== 'missing') expect(close).toHaveBeenCalledWith('child_requested', kind === 'video' ? active.content_session_id : active.game_session_id)
      expect(complete.mock.calls.at(-1)?.[2]).toBe('failed')
      expect(complete.mock.calls.at(-1)?.[1]).toContain('尚未确认')
      expect(complete.mock.calls.at(-1)?.[1]).not.toContain('已经关闭')
    },
  )

  it('starts only a backend-approved game process and binds its pid before voice control returns', async () => {
    const makeTurn = (id: string, transcript: string, action: AgentTurn['action']): AgentTurn => ({
      agent_turn_id: id, transcript, intent: action.type, response_text: '受控游戏操作', action,
      orchestrator_version: 'hybrid_companion_v1', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: '2026-09-01T10:00:00Z',
    })
    vi.spyOn(localApi, 'agentTurn')
      .mockResolvedValueOnce(makeTurn('controlled-game-start', '打开数学游戏', { type: 'start_controlled_game', query: '数学' }))
      .mockResolvedValueOnce(makeTurn('controlled-game-stop', '停止游戏', { type: 'stop_controlled_game' }))
    const game = {
      game_session_id: 'game-session-1', child_id: studyWorkspace.session.child_id,
      session_id: studyWorkspace.session.session_id, approval_id: 'approval-1', title: '数学挑战',
      executable_path: 'C:\\Games\\Math.exe', status: 'active' as const, allowed_minutes: 6,
      actual_seconds: 0, paused_plan_item_id: 'plan-item-1', os_process_id: null,
      started_at: new Date().toISOString(), ends_at: new Date(Date.now() + 360_000).toISOString(),
      ended_at: null, end_reason: null, remaining_seconds: 360,
    }
    const start = vi.spyOn(localApi, 'startControlledGame').mockResolvedValue(game)
    const bind = vi.spyOn(localApi, 'bindControlledGameProcess').mockResolvedValue({ ...game, os_process_id: 811 })
    const end = vi.spyOn(localApi, 'endControlledGame').mockResolvedValue({ ...game, status: 'stopped', remaining_seconds: 0, task_resumed: false })
    vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({ game: null })
    vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({ playback: null })
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(studyWorkspace)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const open = vi.fn().mockResolvedValue({ opened: true, processId: 811 })
    const close = vi.fn().mockResolvedValue({ closed: true, sessionId: game.game_session_id })
    const prepare = nativeLaunchFixture()
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
      prepareControlledLaunch: prepare, listControlledLaunches: vi.fn().mockResolvedValue([]),
      openControlledGame: open, closeControlledGame: close,
      onControlledGameClosed: vi.fn(() => vi.fn()),
      onControlledMediaClosed: vi.fn(() => vi.fn()),
    }))

    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '打开数学游戏' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1))
    expect(prepare).toHaveBeenCalledWith({kind:'game',childId:studyWorkspace.session.child_id,
      sessionId:studyWorkspace.session.session_id,requestId:'controlled-game-start'})
    const intent = await prepare.mock.results[0].value
    expect(start).toHaveBeenCalledWith(studyWorkspace.session.session_id, '数学', 'controlled-game-start', undefined, intent.intentId, undefined)
    await waitFor(() => expect(open).toHaveBeenCalledWith({
      path: game.executable_path, title: game.title, maxSeconds: 360, gameSessionId: game.game_session_id,
      nativeIntentId: intent.intentId,
    }))
    expect(bind).toHaveBeenCalledWith(studyWorkspace.session.session_id, game.game_session_id, 811)

    fireEvent.change(input, { target: { value: '停止游戏' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(close).toHaveBeenCalledWith('child_requested', game.game_session_id))
    await waitFor(() => expect(end).toHaveBeenCalledWith(studyWorkspace.session.session_id, 'child_requested', expect.any(Number), game.game_session_id))
  })

  it.each(['global', 'study'].flatMap((surface) => [
    'ordinary_after_memory', 'private', 'cloud_denied', 'no_owner', 'plain', 'server', 'server_private',
  ].map((policy) => ({ surface, policy }))))('preserves tutor speech ownership and privacy: $surface / $policy', async ({ surface, policy }) => {
    const speech = vi.spyOn(LocalInputs, 'speakLocally')
    const managed = vi.spyOn(LocalAgentStreamingTtsProvider.prototype, 'stream').mockImplementation(async function* () {
      yield { pcm16: new Int16Array([1, 2]).buffer, sampleRate: 24_000 }
    })
    vi.stubGlobal('AudioContext', class {
      currentTime = 0
      destination = {}
      close = vi.fn().mockResolvedValue(undefined)
      createBuffer(_channels: number, length: number, sampleRate: number) {
        return { duration: length / sampleRate, getChannelData: () => new Float32Array(length) }
      }
      createBufferSource() {
        return { buffer: null, connect: vi.fn(), stop: vi.fn(), addEventListener: vi.fn(), start: vi.fn() }
      }
    })
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(readonly text: string) {} })
    const browserSpeak = vi.fn()
    vi.stubGlobal('speechSynthesis', {
      cancel: vi.fn(), getVoices: () => [{ ...offlineChineseVoice, localService: false }], speak: browserSpeak,
    })
    const initial = { ...(surface === 'study' ? studyWorkspace : workspace), drafts: {} }
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(initial)
    const question = '数学里的sin和cos有什么区别'
    const owner: AgentTurn = {
      agent_turn_id: policy === 'no_owner' ? '' : 'global-tutor-turn', transcript: question, intent: 'tutor_question',
      response_text: '好，我按你的问题来讲。',
      action: { type: 'ask_tutor', question, subject: '数学',
        ...(policy === 'private' ? { local_only: true } : {}),
        ...(policy === 'cloud_denied' ? { cloud_transmission_allowed: false } : {}),
      },
      ...(policy.startsWith('server') ? { runtime_execution_location: 'server' } : {}),
      orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: '2026-08-26T10:05:00Z',
    }
    const agent = vi.spyOn(localApi, 'agentTurn').mockResolvedValue(owner)
    const tutor: TutorTurn = {
      turn_id: 'general-tutor-result', session_id: workspace.session.session_id,
      child_id: workspace.session.child_id, plan_item_id: null, subject: '数学', question,
      answer: '先把它们看成直角三角形中两种边长比。', provider: 'test', model: 'test',
      status: 'completed', hint_level: 1, parent_turn_id: null, response_kind: 'hint',
      verification_status: 'not_applicable_hint', safety_status: 'passed',
      strategy_experiment_id: null, strategy_version: null, output_envelope: {
        request_id: 'global-board-request', output_type: 'tutoring', result: { blackboard: {
          version: 'tutor_blackboard_v1', source: 'model_structured', sync_mode: 'segment_estimate',
          subject: '数学', title: '正弦与余弦', segments: [{
            segment_id: 'global-s1', narration: '先看两种边长比。', duration_hint_ms: 1800,
            blocks: [{ kind: 'heading', text: '两种边长比', tone: 'yellow' }],
          }],
        } }, reason_summary: '概念提示', confidence: .65, evidence_refs: [], model_provider: 'test',
        model_version: 'test', prompt_version: 'v1', knowledge_version: null, policy_version: 'v1',
        verification_status: 'partially_verified', safety_status: 'passed', fallback_used: false,
      },
      created_at: '2026-08-26T10:05:00Z',
    }
    if (policy === 'plain') {
      tutor.output_envelope = null
      // No-board speech now reserves an explicit current Tutor playback receipt
      // before audio starts. This fixture remains synthetic; source assertions
      // below still bind the actual Tutor and Agent turn used by the caller.
      vi.spyOn(localApi, 'tutorVoicePlayback').mockImplementation(async (sid, operation, body) => ({
        playback_id: 'synthetic-tutor-voice-playback', turn_id: tutor.turn_id,
        child_id: tutor.child_id, session_id: sid, source_version: 'synthetic-tutor-source-version', generation: 1,
        status: operation === 'reserve' ? 'reserved' : operation === 'start' ? 'started' : operation === 'natural-end' ? 'finished' : 'cancelled',
        lease_id: operation === 'reserve' ? null : body.lease_id as string ?? null,
        lease_generation: operation === 'reserve' ? null : body.lease_generation as number ?? null,
        lease_kind: operation === 'reserve' ? null : body.lease_kind as 'local_sapi' | 'managed_audio' | 'local_pcm' ?? null,
      }))
    }
    const askTutor = vi.spyOn(localApi, 'askTutor').mockResolvedValue(tutor)
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({
      turn: { ...owner, response_text: tutor.answer, action: { ...owner.action, ...(policy === 'server_private' ? { cloud_transmission_allowed: false } : {}) } },
      execution: { capability: 'learning.explain', response_text: tutor.answer, result: {}, evidence: [], tutor_turn: tutor },
    })
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async id => ({ agent_turn_id: id, tool_status: 'completed' }) as AgentTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={initial} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    if (policy === 'ordinary_after_memory') {
      agent.mockResolvedValueOnce({ ...owner, agent_turn_id: 'private-memory-before-tutor', intent: 'memory_command', action: { type: 'none', local_only: true }, response_text: '只在家里保存的记忆' } as AgentTurn)
      fireEvent.change(input, { target: { value: '你记得什么' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      await waitFor(() => expect(speech).toHaveBeenCalledWith('只在家里保存的记忆', initial.session.session_id, expect.any(Function), expect.any(Function),
        expect.objectContaining({ localOnly: true, sourceAgentTurnId: 'private-memory-before-tutor', isCurrent: expect.any(Function) })))
      expect(managed).not.toHaveBeenCalled()
      await waitFor(() => expect(input).toHaveValue(''))
    }
    fireEvent.change(input, { target: { value: question } })
    fireEvent.keyDown(input, { key: 'Enter' })

    if (!policy.startsWith('server')) await waitFor(() => expect(askTutor).toHaveBeenCalledWith(
      workspace.session.session_id, '数学', question, 1, undefined, expect.any(String)
    ))
    expect((await screen.findAllByText('先把它们看成直角三角形中两种边长比。')).length).toBeGreaterThan(0)
    if (policy !== 'plain') expect(await screen.findByRole('region', { name: '数学讲解黑板' })).toBeInTheDocument()
    const localOnly = ['private', 'cloud_denied', 'no_owner', 'server_private'].includes(policy)
    const narrated = policy === 'plain' ? tutor.answer : '先看两种边长比。'
    await waitFor(() => expect(speech.mock.calls.find(([text]) => text === narrated)?.[4]).toEqual(expect.objectContaining({
      localOnly, ...(owner.agent_turn_id ? { sourceAgentTurnId: owner.agent_turn_id } : {}),
    })))
    if (localOnly) expect(managed).not.toHaveBeenCalled()
    else await waitFor(() => expect(managed).toHaveBeenCalledWith(expect.objectContaining({ text: narrated, sourceAgentTurnId: owner.agent_turn_id, localOnly: false })))
    if (policy === 'plain') expect(localApi.tutorVoicePlayback).toHaveBeenCalledWith(initial.session.session_id,
      'reserve', expect.objectContaining({ turn_id: tutor.turn_id, client_action_id: expect.any(String) }), expect.any(AbortSignal))
    expect(browserSpeak).not.toHaveBeenCalled()
  })

  it('clears the previous tutor blackboard before starting a new camera question', async () => {
    const staleTutor: TutorTurn = {
      turn_id: 'stale-equation-turn', session_id: studyWorkspace.session.session_id,
      child_id: studyWorkspace.session.child_id, plan_item_id: 'plan-item-1', subject: '数学',
      question: '判别式怎么看根的数量？', answer: '判别式讲解', provider: 'test', model: 'test',
      status: 'completed', hint_level: 6, parent_turn_id: null, response_kind: 'full_explanation',
      verification_status: 'partially_verified', safety_status: 'passed',
      strategy_experiment_id: null, strategy_version: null, created_at: '2026-08-26T10:05:00Z',
      output_envelope: {
        request_id: 'stale-board-request', output_type: 'tutoring', reason_summary: '旧讲解',
        confidence: 0.6, evidence_refs: [], model_provider: 'test', model_version: 'test',
        prompt_version: 'v1', knowledge_version: null, policy_version: 'v1',
        verification_status: 'partially_verified', safety_status: 'passed', fallback_used: false,
        result: { blackboard: {
          version: 'tutor_blackboard_v1', source: 'model_structured', sync_mode: 'segment_estimate',
          subject: '数学', title: '根的判别式', segments: [{
            segment_id: 'stale-s1', narration: '旧方程讲解', duration_hint_ms: 1000,
            blocks: [{ kind: 'heading', text: '一元二次方程', tone: 'yellow' }],
          }],
        } },
      },
    }
    const captureWorkspace = { ...studyWorkspace, latest_tutor_turn: staleTutor }
    vi.mocked(localApi.tutorPlayback).mockResolvedValue({ playback: {
      playback_id: 'restored-board', turn_id: staleTutor.turn_id, child_id: staleTutor.child_id, session_id: staleTutor.session_id,
      source_version: 'old-source', presentation_version: 'old-presentation', generation: 1, revision: 1, status: 'paused',
      segment_index: 0, segment_started: true, reason: null, replayed: false, evidence_kind: 'playback_only',
      presentation: staleTutor.output_envelope!.result!.blackboard as TutorBlackboardPresentation,
    } })
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({
      agent_turn_id: 'capture-new-question', transcript: '帮我看一下这个题。',
      intent: 'capture_question', response_text: '好，把题目放到高拍仪下。',
      action: { type: 'request_capture' }, orchestrator_version: 'agent_runtime_deterministic_v1',
      tool_status: 'pending', tool_error_code: null, completed_at: null,
      created_at: new Date().toISOString(),
    })
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={captureWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const board = await screen.findByRole('region', { name: '数学讲解黑板' })
    fireEvent.click(screen.getByRole('button', { name: '收起学习伙伴' }))
    expect(board).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '展开学习伙伴' }))
    expect(screen.getByRole('region', { name: '数学讲解黑板' })).toBe(board)
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: '帮我看一下这个题。' } })
    fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'), { key: 'Enter' })

    await waitFor(() => expect(screen.queryByRole('region', { name: '数学讲解黑板' })).not.toBeInTheDocument())
    expect(screen.queryByText('一元二次方程')).not.toBeInTheDocument()
  })

  it.each([true, false])('projects a tutor answer from the first audible frame or complete text-only fallback (local voice: %s)', async (hasLocalVoice) => {
    const speech = vi.spyOn(LocalInputs, 'speakLocally')
    class FakeUtterance {
      voice: SpeechSynthesisVoice | null = null
      lang = ''
      rate = 1
      onstart: (() => void) | null = null
      onend: (() => void) | null = null
      onerror: (() => void) | null = null
      constructor(readonly text: string) {}
    }
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    const speak = vi.fn((_utterance: FakeUtterance) => undefined)
    vi.stubGlobal('speechSynthesis', {
      cancel: vi.fn(), getVoices: () => hasLocalVoice ? [offlineChineseVoice] : [],
      speak,
    })
    const question = '为什么判别式大于零时有两个根？'
    const localPrompt = '讲完后我再确认一个小点：你能用自己的话说说“函数图像与交点”吗？'
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({
      agent_turn_id: 'board-agent-turn', transcript: question, intent: 'tutor_question',
      response_text: '我们一边讲一边在黑板上看。', action: { type: 'ask_tutor', question, subject: '数学', local_learning_context: {
        status: 'matched', cloud_transmission_allowed: false, mastery_update_allowed: false,
        audit_event_id: 'atomic-context-board-1', matched_node_count: 3, approved_node_count: 0,
        hypothesis_count: 1, diagnostic_prompt: localPrompt,
      } },
      runtime_execution_location: 'local_model', runtime_capability: 'learning.explain',
      orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: '2026-08-26T10:05:00Z',
    })
    vi.spyOn(localApi, 'askTutor').mockResolvedValue({
      turn_id: 'board-tutor-turn', session_id: studyWorkspace.session.session_id,
      child_id: studyWorkspace.session.child_id, plan_item_id: 'plan-item-1', subject: '数学', question,
      answer: '判别式大于零，就表示抛物线和x轴有两个交点。', provider: 'test', model: 'test',
      status: 'completed', hint_level: 1, parent_turn_id: null, response_kind: 'hint',
      verification_status: 'not_applicable_hint', safety_status: 'passed',
      strategy_experiment_id: null, strategy_version: null,
      output_envelope: {
        request_id: 'board-request', output_type: 'tutoring',
        result: {
          content: '判别式大于零，就表示抛物线和x轴有两个交点。',
          blackboard: {
            version: 'tutor_blackboard_v1', source: 'model_structured', sync_mode: 'segment_estimate',
            subject: '数学', title: '判别式与根',
            segments: [{
              segment_id: 'segment-1', narration: '判别式大于零，就表示抛物线和x轴有两个交点。', duration_hint_ms: 2400,
              blocks: [
                { kind: 'heading', tone: 'yellow', text: '两个交点' },
                { kind: 'formula', tone: 'cyan', text: '判别式', latex: '\\Delta=b^2-4ac>0' },
              ],
            }],
          },
        },
        reason_summary: '先给直观关系。', confidence: 0.65, evidence_refs: ['plan-item-1'],
        model_provider: 'test', model_version: 'test', prompt_version: 'tutor_layered_blackboard_v4',
        knowledge_version: 'local_knowledge_catalog_v1', policy_version: 'tutor_safety_v2',
        verification_status: 'partially_verified', safety_status: 'passed', fallback_used: false,
      },
      created_at: '2026-08-26T10:05:00Z',
    })
    const completeAgentTurn = vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(async id => ({ agent_turn_id: id, tool_status: 'completed' }) as AgentTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: question } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(await screen.findByRole('region', { name: '数学讲解黑板' })).toBeInTheDocument()
    expect(speech.mock.calls.find(([text]) => text.includes('判别式大于零'))?.[4]).toEqual(expect.objectContaining({ localOnly: true, sourceAgentTurnId: 'board-agent-turn' }))
    if (hasLocalVoice) {
      await waitFor(() => expect(speak).toHaveBeenCalled())
      expect(screen.queryByText('两个交点')).not.toBeInTheDocument()
      act(() => speak.mock.calls[0][0].onstart?.())
    }
    expect(await screen.findByText('两个交点')).toBeInTheDocument()
    expect(screen.getByLabelText('判别式')).toBeInTheDocument()
    expect(vi.mocked(localApi.askTutor)).toHaveBeenCalledWith(
      studyWorkspace.session.session_id, '数学', question, 1, undefined, expect.any(String),
    )
    await waitFor(() => expect(completeAgentTurn).toHaveBeenCalledWith(
      'board-agent-turn', expect.stringContaining(localPrompt), 'completed', undefined,
      expect.objectContaining({
        execution_receipt: expect.objectContaining({
          capability: 'learning.explain', execution_location: 'local_model', action_type: 'ask_tutor',
          output: expect.objectContaining({ tutor_turn_id: 'board-tutor-turn', blackboard_present: true }),
        }),
      }),
    ))
    if (hasLocalVoice) {
      expect(speak.mock.calls.some(([utterance]) => utterance.text.includes(localPrompt))).toBe(false)
      await act(async () => { speak.mock.calls[0][0].onend?.(); await Promise.resolve() })
      await waitFor(() => expect(speak.mock.calls.some(([utterance]) => utterance.text.includes(localPrompt))).toBe(true))
      expect(speech.mock.calls.find(([text]) => text.includes(localPrompt))?.[4]).toEqual(expect.objectContaining({ localOnly: true, sourceAgentTurnId: 'board-agent-turn' }))
    } else {
      expect(speak).not.toHaveBeenCalled()
      expect(speech.mock.calls.some(([text]) => text.includes(localPrompt))).toBe(false)
    }
  })

  it('records a child switch only after the destination owner returns durable evidence', async () => {
    class FakeUtterance { constructor(readonly text: string) {} voice: SpeechSynthesisVoice | null = null }
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), getVoices: () => [], speak: vi.fn() })
    const openMistakeBook = vi.fn().mockResolvedValue(undefined)
    const switchChild = vi.fn().mockImplementation(async (target, beforeActivate) => {
      expect(localApi.completeAgentTurn).not.toHaveBeenCalledWith('switch-child-turn', expect.anything(), 'completed', expect.anything(), expect.anything())
      await beforeActivate({ profile_switched: true, target_child_id: target, target_session_id: 'destination-session', previous_session_id: workspace.session.session_id })
    })
    vi.spyOn(localApi, 'agentTurn')
      .mockResolvedValueOnce({
        agent_turn_id: 'open-mistake-book-turn', transcript: '打开错题本', intent: 'open_mistake_book',
        response_text: '好，我现在打开你的错题本。', action: { type: 'open_mistake_book' },
        orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
        completed_at: null, created_at: '2026-08-26T10:05:00Z',
      })
      .mockResolvedValueOnce({
        agent_turn_id: 'switch-child-turn', transcript: '我是初三', intent: 'switch_child_profile',
        response_text: '明白，我切换到你的独立档案。',
        action: { type: 'switch_child_profile', target_child_id: 'child-junior-3' },
        orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
        completed_at: null, created_at: '2026-08-26T10:05:01Z',
      })
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={{ ...workspace, drafts: {} }} childName="小辰" childGrade="初三" onOpenMistakeBook={openMistakeBook} onSwitchChild={switchChild} onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '打开错题本' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(openMistakeBook).toHaveBeenCalledTimes(1))

    fireEvent.change(input, { target: { value: '我是初三' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(switchChild).toHaveBeenCalledWith('child-junior-3', expect.any(Function), 'switch-child-turn-switch'))
    await waitFor(() => expect(localApi.completeAgentTurn).toHaveBeenCalledWith('switch-child-turn', expect.any(String), 'completed', undefined, expect.objectContaining({
      execution_receipt: expect.objectContaining({ output: expect.objectContaining({ target_session_id: 'destination-session', profile_switched: true }) }),
    })))
  })

  it.each(['switch_response_lost', 'ack_response_lost'] as const)('retains a profile handoff without failed overwrite when %s', async (failure) => {
    const { readProfileSwitchHandoff } = await import('./profileSwitchHandoff')
    class FakeUtterance { constructor(readonly text: string) {} voice: SpeechSynthesisVoice | null = null }
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), getVoices: () => [], speak: vi.fn() })
    const switchChild = vi.fn().mockImplementation(async (target, beforeActivate, clientActionId) => {
      expect(readProfileSwitchHandoff()).toMatchObject({ turnId: 'profile-durable-turn', clientActionId, targetChildId: target, sourceSessionId: workspace.session.session_id })
      if (failure === 'switch_response_lost') throw new Error('switch response lost after commit')
      await beforeActivate({ profile_switched: true, target_child_id: target, target_session_id: 'destination-session', previous_session_id: workspace.session.session_id })
    })
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({
      agent_turn_id: 'profile-durable-turn', transcript: '现在轮到另一位孩子学习了', intent: 'switch_child_profile',
      response_text: '正在切换。', action: { type: 'switch_child_profile', target_child_id: 'child-junior-3' },
      orchestrator_version: 'cloud-first', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: '2026-09-07T10:05:00Z',
    })
    const ack = vi.spyOn(localApi, 'completeAgentTurn').mockRejectedValue(new Error('acknowledgement lost'))
    render(<SessionWorkspace initial={{ ...workspace, drafts: {} }} childName="小辰" childGrade="初三" onSwitchChild={switchChild} onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '现在轮到另一位孩子学习了' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(switchChild).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(input).not.toBeDisabled())
    if (failure === 'ack_response_lost') {
      await waitFor(() => expect(ack).toHaveBeenCalledTimes(1))
      expect(readProfileSwitchHandoff()?.receipt?.output.target_session_id).toBe('destination-session')
      expect(ack.mock.calls[0][2]).toBe('completed')
    } else expect(ack).not.toHaveBeenCalled()
    expect(readProfileSwitchHandoff()?.clientActionId).toBe('profile-durable-turn-switch')
    expect(ack.mock.calls.some((call) => call[2] === 'failed')).toBe(false)
  })

  it('cancels a queued utterance on privacy pause without clearing typed text', async () => {
    const firstTurn: AgentTurn = {
      agent_turn_id: 'agent-turn-privacy', transcript: '第一句话', intent: 'supportive_chat',
      response_text: '稍后才返回的旧回复', action: { type: 'none' },
      orchestrator_version: 'hybrid_companion_v1', tool_status: 'pending',
      tool_error_code: null, completed_at: null, created_at: '2026-08-26T10:05:00Z',
    }
    let resolveFirst!: (turn: AgentTurn) => void
    const firstRequest = new Promise<AgentTurn>((resolve) => { resolveFirst = resolve })
    const agentTurn = vi.spyOn(localApi, 'agentTurn').mockReturnValue(firstRequest)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(firstTurn)
    vi.spyOn(localApi, 'saveSessionDraft').mockResolvedValue({
      section: 'agent_command', payload: {}, updated_at: '2026-08-26T10:04:00Z',
    })
    const clearDraft = vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const speak = vi.fn()
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), getVoices: () => [], speak })

    render(<SessionWorkspace initial={{ ...workspace, drafts: {} }} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '第一句话' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(agentTurn).toHaveBeenCalledTimes(1))
    fireEvent.change(input, { target: { value: '暂停后仍保留的文字' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))

    await act(async () => { resolveFirst(firstTurn); await firstRequest })
    await waitFor(() => expect(screen.getByRole('button', { name: '发送' })).toBeEnabled())
    expect(agentTurn).toHaveBeenCalledTimes(1)
    expect(input).toHaveValue('暂停后仍保留的文字')
    expect(clearDraft).not.toHaveBeenCalled()
    expect(speak).not.toHaveBeenCalled()
  })

  it('shows application lease evidence without pretending Windows release was verified', () => {
    render(<SessionWorkspace initial={{
      ...workspace,
      session: { ...workspace.session, status: 'completed', state: 'completed' },
      sensor_release: {
        opened_leases: 2,
        active_leases: 0,
        released_leases: 2,
        application_release_confirmed: true,
        evidence_level: 'application_record_only',
        os_release_verified: false,
        release_verification: null,
        by_device: {
          camera: { opened: 1, active: 0, released: 1, last_release_reason: 'session_completed', last_released_at: '2026-08-26T11:00:00Z' },
          microphone: { opened: 1, active: 0, released: 1, last_release_reason: 'session_completed', last_released_at: '2026-08-26T11:00:00Z' }
        }
      }
    }} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    expect(screen.getByText(/本地服务已记录本次2个设备租约全部释放/)).toBeInTheDocument()
    expect(screen.getByText(/Windows系统级占用状态仍将在真实设备上复核/)).toBeInTheDocument()

    cleanup()
    render(<SessionWorkspace initial={{
      ...workspace,
      session: { ...workspace.session, status: 'completed', state: 'completed' },
      sensor_release: {
        opened_leases: 1,
        active_leases: 1,
        released_leases: 0,
        application_release_confirmed: false,
        evidence_level: 'application_record_only',
        os_release_verified: false,
        release_verification: null,
        by_device: {}
      }
    }} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    expect(screen.getByText(/仍记录1个活动设备租约/)).toBeInTheDocument()
  })

  it('shows persisted Windows-level evidence only after parent verification passed', () => {
    render(<SessionWorkspace initial={{
      ...workspace,
      session: { ...workspace.session, status: 'completed', state: 'completed' },
      sensor_release: {
        opened_leases: 1,
        active_leases: 0,
        released_leases: 1,
        application_release_confirmed: true,
        evidence_level: 'windows_session_verified',
        os_release_verified: true,
        release_verification: {
          result: 'passed',
          used_device_types: ['camera'],
          reopened_device_types: ['camera'],
          device_manager_healthy: true,
          windows_privacy_indicator_clear: true,
          physical_indicator_status: 'clear',
          platform_name: 'Windows',
          platform_version: '10.0.26100',
          verified_at: '2026-08-26T10:30:00Z',
        },
        by_device: {
          camera: { opened: 1, active: 0, released: 1, last_release_reason: 'session_completed', last_released_at: '2026-08-26T10:20:00Z' }
        }
      }
    }} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    expect(screen.getByText(/完成 Windows 隐私指示和其他应用重开复核/)).toBeInTheDocument()
    expect(screen.queryByText(/仍将在真实设备上复核/)).not.toBeInTheDocument()
  })

  it('keeps a sensitive tutor question local and does not offer cloud retry', async () => {
    vi.spyOn(localApi, 'saveSessionDraft').mockResolvedValue({
      section: 'study', payload: {}, updated_at: '2026-08-26T10:04:00Z'
    })
    vi.spyOn(localApi, 'askTutor').mockRejectedValue(new ApiError(
      '这段内容不会上传。请先停下来，现在就去找家长或你信任的大人。',
      422,
      'SENSITIVE_TEXT_LOCAL_ONLY',
      false,
      'find_parent'
    ))

    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    const input = screen.getByLabelText('输入学习问题')
    fireEvent.change(input, { target: { value: '我有一件很危险的事情不敢说' } })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('这段内容不会上传')
    expect(screen.queryByRole('button', { name: '重试本题' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '我知道了，继续当前任务' })).toBeInTheDocument()
    expect(input).toHaveValue('我有一件很危险的事情不敢说')
  })

  it('reopens a persisted multi-question capture queue after the workspace reloads', async () => {
    vi.spyOn(localApi, 'mistakeDialogue').mockResolvedValue({ dialogue: {
      session_id: studyWorkspace.session.session_id, child_id: 'child-junior-3',
      candidate_id: 'candidate-recovery-2', mistake_id: null, phase: 'confirm',
      state: { artifact_id: 'artifact-recovery-1', question_text: '2. 找规律。', submitted_answer: '12' }, response_text: '原答案是12，对吗？'
    } })
    vi.spyOn(localApi, 'beginMistakeDialogue').mockResolvedValue({
      session_id: studyWorkspace.session.session_id, child_id: 'child-junior-3',
      candidate_id: 'candidate-recovery-2', mistake_id: null, phase: 'confirm',
      state: { artifact_id: 'artifact-recovery-1', question_text: '2. 找规律。', submitted_answer: '12' }, response_text: '原答案是12，对吗？'
    })
    vi.spyOn(localApi, 'mistakeCaptureQueues').mockResolvedValue([{
      artifact_id: 'artifact-recovery-1', extracted_text: '1. 计算125×32。\n2. 找规律。',
      confidence: 0.92, local_path: null, status: 'completed', pending_count: 2, candidate_count: 3,
      details: { lines: [], engine: 'local-test' }
    }])
    vi.spyOn(localApi, 'prepareMistakeCandidates').mockResolvedValue([
      {
        candidate_id: 'candidate-recovery-1', artifact_id: 'artifact-recovery-1', child_id: 'child-junior-3',
        question_index: 0, question_text: '1. 计算125×32。', region: null, confidence: 0.92,
        status: 'saved', mistake_id: 'mistake-1', created_at: '', updated_at: ''
      },
      {
        candidate_id: 'candidate-recovery-2', artifact_id: 'artifact-recovery-1', child_id: 'child-junior-3',
        question_index: 1, question_text: '2. 找规律。', region: null, confidence: 0.92,
        status: 'pending', mistake_id: null, created_at: '', updated_at: ''
      },
      {
        candidate_id: 'candidate-recovery-3', artifact_id: 'artifact-recovery-1', child_id: 'child-junior-3',
        question_index: 2, question_text: '3. 面积。', region: null, confidence: 0.92,
        status: 'pending', mistake_id: null, created_at: '', updated_at: ''
      }
    ])
    vi.spyOn(localApi, 'mistakeKnowledgeCandidates').mockResolvedValue([])

    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    expect(screen.queryByRole('button', { name: '继续整理拍题（2 道）' })).not.toBeInTheDocument()
    expect(await screen.findByText('2. 找规律。')).toBeInTheDocument()
    expect(screen.getByText('当时的答案：12')).toBeInTheDocument()
    expect(screen.getAllByText('原答案是12，对吗？').length).toBeGreaterThan(0)
  })

  it('shows the persisted output-envelope reason instead of hiding model uncertainty', async () => {
    vi.spyOn(localApi, 'saveSessionDraft').mockResolvedValue({
      section: 'study', payload: {}, updated_at: '2026-08-26T10:04:00Z'
    })
    vi.spyOn(localApi, 'askTutor').mockResolvedValue({
      turn_id: 'turn-1', session_id: 'session-draft-1', child_id: 'child-junior-3',
      plan_item_id: 'plan-item-1', subject: '数学', question: '判别式怎么看？',
      answer: '先写出判别式，再比较它和0。', provider: 'qwen', model: 'qwen-test',
      status: 'completed', hint_level: 1, parent_turn_id: null, response_kind: 'hint',
      verification_status: 'not_applicable_hint', safety_status: 'passed',
      strategy_experiment_id: null, strategy_version: null,
      output_envelope: {
        request_id: 'request-1', output_type: 'tutoring',
        result: { content: '先写出判别式，再比较它和0。' },
        reason_summary: '当前仅提供一层方法提示，未把它当作最终答案。', confidence: 0.65,
        evidence_refs: ['plan-item-1'], model_provider: 'qwen', model_version: 'qwen-test',
        prompt_version: 'tutor_layered_v2', knowledge_version: 'local_knowledge_catalog_v1',
        policy_version: 'tutor_safety_v2', verification_status: 'partially_verified',
        safety_status: 'passed', fallback_used: false
      },
      created_at: '2026-08-26T10:05:00Z'
    })

    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('输入学习问题'), { target: { value: '判别式怎么看？' } })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByText('先写出判别式，再比较它和0。')).toBeInTheDocument()
    expect(screen.getByText('当前仅提供一层方法提示，未把它当作最终答案。')).toBeInTheDocument()
  })

  it('shows a read-aloud baseline task without presenting local transcription as pronunciation scoring', async () => {
    vi.spyOn(localApi, 'saveSessionDraft').mockResolvedValue({
      section: 'study', payload: {}, updated_at: '2026-08-26T10:04:00Z'
    })
    vi.spyOn(localApi, 'nextDiagnostic').mockResolvedValue({
      item: {
        diagnostic_item_id: 'diag-pet-read-aloud-1', knowledge_node_id: 'node-pet-speaking-read-aloud',
        track: 'pet', subject: '英语', title: '朗读参考', prompt: '请先默读，再完整朗读。',
        answer_type: 'read_aloud', difficulty: 1,
        stimulus_text: 'On Saturday morning, Ben walks to the market with his father.',
        stimulus_asset: null, assessment_mode: 'pet_reference_rubric'
      },
      pet_baseline: {
        definition_version: 'pet_baseline_v1', status: 'in_progress', completed_tasks: 3,
        required_tasks: 5, evidence_count: 3, reference_only: true,
        boundary: '完成仅表示规定任务已有证据，不代表达到PET通过水平。',
        dimensions: [
          { key: 'reading', label: '阅读', score_kind: 'objective', completed_tasks: 1, required_tasks: 1, status: 'completed', reference_score: 100, tasks: [] },
          { key: 'listening', label: '听力', score_kind: 'objective', completed_tasks: 1, required_tasks: 1, status: 'completed', reference_score: 100, tasks: [] },
          { key: 'writing', label: '写作', score_kind: 'reference', completed_tasks: 1, required_tasks: 1, status: 'completed', reference_score: 80, tasks: [] },
          { key: 'speaking', label: '口语', score_kind: 'reference', completed_tasks: 0, required_tasks: 2, status: 'not_started', reference_score: null, tasks: [] }
        ]
      }
    })

    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'PET' }))
    fireEvent.click(screen.getByRole('button', { name: '来一道题' }))

    expect(await screen.findByText('On Saturday morning, Ben walks to the market with his father.')).toBeInTheDocument()
    expect(screen.getByText('3/5项已有证据 · 参考评估')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('直接朗读；这里会显示本地转写，提交前可以核对。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '播放听力材料' })).not.toBeInTheDocument()
  })

  it('loads the bound PET activity and requires the task to start before answer submission', async () => {
    const activeWorkspace: SessionWorkspaceState = {
      ...petStudyWorkspace,
      plan_items: [{
        ...petStudyWorkspace.plan_items[0],
        status: 'active', started_at: '2026-08-26T10:05:00Z', active_segment_started_at: '2026-08-26T10:05:00Z'
      }]
    }
    const item: NonNullable<DiagnosticPromptResult['prompt']>['item'] = {
        diagnostic_item_id: 'diag-pet-daily-retell-1', knowledge_node_id: 'node-pet-reading-main-idea',
        track: 'pet', subject: '英语', title: '阅读主旨',
        prompt: 'Read or listen to the short story, then retell it in English.',
        answer_type: 'speaking', difficulty: 1,
        stimulus_text: 'Lily found a small dog near the library. She helped it go home safely.',
        stimulus_asset: null, assessment_mode: 'pet_reference_rubric'
      }
    const nextDiagnostic = vi.spyOn(localApi, 'nextDiagnostic')
    const readOriginal = vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({
      status: 'waiting_answer', prompt: { prompt_id: 'bound-pet-original', item_version: 'v1',
        child_id: activeWorkspace.session.child_id, origin_session_id: activeWorkspace.session.session_id,
        diagnostic_item_id: item.diagnostic_item_id, item, review_task_id: null, created_at: '2026-08-26T10:05:00Z' },
      attempt: null, reason: null,
    })
    vi.spyOn(localApi, 'startPlanItem').mockResolvedValue(undefined)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(activeWorkspace)

    render(<SessionWorkspace initial={petStudyWorkspace} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)

    expect(await screen.findByText('今日PET活动')).toBeInTheDocument()
    expect(nextDiagnostic).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'PET' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '开始这项' }))
    await waitFor(() => expect(readOriginal).toHaveBeenCalledWith('session-draft-1'))
    expect(nextDiagnostic).not.toHaveBeenCalled()
    expect(await screen.findByText('Lily found a small dog near the library. She helped it go home safely.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '播放故事' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('输入挑战题答案'), {
      target: { value: 'Lily found a dog near the library and helped it return home safely.' }
    })
    await waitFor(() => expect(screen.getByRole('button', { name: '提交答案' })).toBeEnabled())
  })

  it('voice-starts a planned PET vocabulary task and immediately presents its first card', async () => {
    const pendingVocabularyWorkspace: SessionWorkspaceState = {
      ...petStudyWorkspace,
      plan_items: [{
        ...petStudyWorkspace.plan_items[0],
        title: 'PET词汇间隔复习 · 1张到期',
        source_ref: 'pet-vocabulary-due:1',
        task_type: 'memorization',
      }]
    }
    const activeVocabularyWorkspace: SessionWorkspaceState = {
      ...pendingVocabularyWorkspace,
      plan_items: [{
        ...pendingVocabularyWorkspace.plan_items[0],
        status: 'active',
        started_at: '2026-08-26T10:05:00Z',
        active_segment_started_at: '2026-08-26T10:05:00Z',
      }]
    }
    const turns: AgentTurn[] = [{
      agent_turn_id: 'pet-plan-voice-start', transcript: '开始任务', intent: 'start_task',
      response_text: '好，开始今天的任务。', action: { type: 'start_next_task' },
      orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: new Date().toISOString()
    }, {
      agent_turn_id: 'pet-plan-voice-answer', transcript: '图书馆', intent: 'submit_pet_vocabulary_answer',
      response_text: '收到。', action: { type: 'submit_pet_vocabulary_answer', answer: '图书馆', vocabulary_prompt_id: 'pet-plan-prompt', vocabulary_card_version: 'v1' },
      orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: new Date().toISOString()
    }]
    vi.spyOn(localApi, 'agentTurn').mockImplementation(async () => turns.shift()!)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const startPlanItem = vi.spyOn(localApi, 'startPlanItem').mockResolvedValue(undefined)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(activeVocabularyWorkspace)
    const nextPetVocabulary = vi.spyOn(localApi, 'nextPetVocabulary').mockResolvedValue({
      card: {
        card_id: 'pet-card-library-plan', review_mode: 'recognition',
        prompt: 'library 在这里可以表示什么意思？用中文或简单英文回答。',
        focus_kind: 'word', due_at: new Date().toISOString(), source_ref: 'textbook-page-1'
      },
      summary: { deck_size: 2, new_count: 2, learning_count: 0, due_count: 2, total_attempts: 0, lapse_count: 0 },
      sync: { available_confirmed: 2, inserted: 2, skipped_without_meaning: 0, deck_size: 2 }
    })
    vi.spyOn(localApi, 'answerBoundPetVocabulary').mockResolvedValue({
      result: {
        attempt_id: 'pet-plan-attempt-1', card_id: 'pet-card-library-plan', review_mode: 'recognition',
        correct: true, expected: ['图书馆'], next_due_at: new Date().toISOString(), interval_days: 1,
        feedback: '答对了，已经安排下一次间隔复习。'
      },
      next_card: null,
      summary: { deck_size: 2, new_count: 1, learning_count: 1, due_count: 1, total_attempts: 1, lapse_count: 0 }
    })
    const completePlanItem = vi.spyOn(localApi, 'completePlanItem').mockResolvedValue(undefined)

    render(<SessionWorkspace initial={pendingVocabularyWorkspace} childName="小宇" childGrade="四年级" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '开始任务' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => expect(startPlanItem).toHaveBeenCalledWith('pet-plan-item-1'))
    await waitFor(() => expect(nextPetVocabulary).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/library 在这里/)).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '图书馆' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(completePlanItem).toHaveBeenCalledWith(
      'pet-plan-item-1',
      expect.any(Number),
      expect.objectContaining({
        type: 'child_checked',
        checklist: ['recall_attempted', 'uncertain_parts_marked'],
      })
    ))
    expect(await screen.findByText(/这项已经自动记录完成/)).toBeInTheDocument()
  })

  it('automatically presents a competition diagnostic after the voice-started task becomes active', async () => {
    const competitionWorkspace: SessionWorkspaceState = {
      ...studyWorkspace,
      plan_items: [{
        ...studyWorkspace.plan_items[0],
        status: 'active', source_type: 'competition_math', source_ref: 'competition_math.logic',
        title: '基线诊断 · 条件分析与逻辑推理', task_type: 'reasoning',
        started_at: '2026-08-26T10:05:00Z', active_segment_started_at: '2026-08-26T10:05:00Z'
      }]
    }
    const nextDiagnostic = vi.spyOn(localApi, 'nextDiagnostic').mockResolvedValue({
      item: {
        diagnostic_item_id: 'diag-oly-logic-1', knowledge_node_id: 'node-oly-logic',
        track: 'competition_math', subject: '数学', title: '逻辑推理',
        prompt: '甲不是第一，乙在丙前面，三人的名次分别是什么？',
        answer_type: 'text', difficulty: 2, stimulus_text: null, stimulus_asset: null,
        assessment_mode: 'objective'
      },
      pet_baseline: null
    })

    render(<SessionWorkspace initial={competitionWorkspace} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)

    expect(await screen.findByText('甲不是第一，乙在丙前面，三人的名次分别是什么？')).toBeInTheDocument()
    expect(nextDiagnostic).toHaveBeenCalledWith('session-draft-1', 'competition_math', undefined)
  })

  it('automatically presents the matching junior subject diagnostic without a child click', async () => {
    const juniorWorkspace: SessionWorkspaceState = {
      ...studyWorkspace,
      plan_items: [{
        ...studyWorkspace.plan_items[0],
        status: 'active', source_type: 'junior_exam_goal', source_ref: 'cycle-junior-1',
        subject: '物理', title: '基线诊断 · 物理实验变量与结论', task_type: 'test_prep',
        started_at: '2026-08-26T10:05:00Z', active_segment_started_at: '2026-08-26T10:05:00Z'
      }]
    }
    const nextDiagnostic = vi.spyOn(localApi, 'nextDiagnostic').mockResolvedValue({
      item: {
        diagnostic_item_id: 'diag-j3-physics-experiment-1', knowledge_node_id: 'node-j3-physics-experiment-reasoning',
        track: 'school', subject: '物理', title: '物理实验变量与结论',
        prompt: '探究摩擦力与压力的关系时，为什么使用同一接触面？',
        answer_type: 'text', difficulty: 1, stimulus_text: null, stimulus_asset: null,
        assessment_mode: 'objective'
      },
      pet_baseline: null
    })

    render(<SessionWorkspace initial={juniorWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    expect(await screen.findByText('探究摩擦力与压力的关系时，为什么使用同一接触面？')).toBeInTheDocument()
    expect(nextDiagnostic).toHaveBeenCalledWith('session-draft-1', 'school', undefined, '物理')
  })

  it.each(['active', 'needs_review'])('loads and submits a diagnostic through voice with %s receipt', async (receiptStatus) => {
    const turns: AgentTurn[] = [
      {
        agent_turn_id: 'diag-voice-start', transcript: '来一道校内题', intent: 'start_diagnostic',
        response_text: '好，我准备一道适合当前进度的能力挑战。', action: { type: 'start_diagnostic', track: 'school' },
        orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null, completed_at: null, created_at: new Date().toISOString()
      },
      {
        agent_turn_id: 'diag-voice-answer', transcript: '答案是B', intent: 'submit_diagnostic_answer',
        response_text: '收到，我会保存这次作答并告诉你反馈。', action: { type: 'submit_diagnostic_answer', answer: 'B', diagnostic_prompt_id: 'prompt-voice-1', diagnostic_item_version: 'v1' },
        orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null, completed_at: null, created_at: new Date().toISOString()
      }
    ]
    vi.spyOn(localApi, 'agentTurn').mockImplementation(async () => turns.shift()!)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const nextDiagnostic = {
      item: {
        diagnostic_item_id: 'diag-voice-1', knowledge_node_id: 'node-factors', track: 'school', subject: '数学',
        title: '因式分解', prompt: '下列哪一项等于x平方减一？', answer_type: 'choice', difficulty: 1,
        stimulus_text: null, stimulus_asset: null, assessment_mode: 'objective'
      }, pet_baseline: null
    }
    let attempt = {
      attempt_id: 'attempt-voice-1', diagnostic_item_id: 'diag-voice-1', submitted_answer: 'B', created_at: new Date().toISOString(),
      feedback: '回答正确。', correctness: 1, pet_baseline: null, knowledge_graph: { status: 'recorded', node_id: 'node-factors' },
      specialized_assessment: { run_id: 'run-voice-1', track: 'junior_exam', phase: 'baseline', status: 'active', total_items: 3, completed_items: 1, next_item_id: 'diag-voice-2', domain_results: { 'junior_exam.数学.方程与不等式': 1 } },
      next_specialized_item: {
        diagnostic_item_id: 'diag-voice-2', knowledge_node_id: 'node-factors', track: 'school', subject: '数学',
        title: '因式分解迁移', prompt: '下一题，请说明平方差公式。', answer_type: 'text', difficulty: 2,
        stimulus_text: null, stimulus_asset: null, assessment_mode: 'objective'
      }
    } as DiagnosticAttempt
    if (receiptStatus === 'needs_review') attempt = {
      attempt_id: 'attempt-voice-1', diagnostic_item_id: 'diag-voice-1', submitted_answer: 'B',
      feedback: '作答已保存，当前评分方式不能可靠确认对错，需要进一步核实。', correctness: 1,
      score_status: 'needs_review', mastery_update_allowed: false,
      specialized_assessment: { run_id: 'run-voice-1', track: 'junior_exam', phase: 'baseline',
        status: 'completed', total_items: 1, completed_items: 1, next_item_id: null,
        domain_results: {}, qualified_items: 0, unverified_items: 1, learning_status: 'needs_verification' },
      next_specialized_item: null,
    } as DiagnosticAttempt
    const prepared = { status: 'waiting_answer', prompt: { prompt_id: 'prompt-voice-1', item_version: 'v1', diagnostic_item_id: 'diag-voice-1', review_task_id: null, child_id: studyWorkspace.session.child_id, origin_session_id: 'session-draft-1', item: nextDiagnostic.item, created_at: new Date().toISOString() }, attempt: null, reason: null } as DiagnosticPromptResult
    const admit = vi.spyOn(localApi, 'admitOriginalClientExecution').mockResolvedValue({ status: 'admitted' })
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValueOnce({ status: 'no_item', prompt: null, attempt: null, reason: null }).mockResolvedValue(prepared)
    const prepareDiagnostic = vi.spyOn(localApi, 'prepareDiagnosticPrompt').mockResolvedValueOnce(prepared).mockResolvedValue({ ...prepared, prompt: { ...prepared.prompt!, prompt_id: 'prompt-voice-2', item_version: 'v2', diagnostic_item_id: 'diag-voice-2', item: attempt.next_specialized_item! } })
    const answerDiagnostic = vi.spyOn(localApi, 'answerDiagnosticPrompt').mockResolvedValue({ ...prepared, status: 'answered', attempt })
    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '来一道校内题' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByText('下列哪一项等于x平方减一？', { selector: '.diagnostic-question p' })).toBeInTheDocument()
    expect(prepareDiagnostic).toHaveBeenCalledWith('session-draft-1', expect.objectContaining({ track: 'school' }))
    expect(admit).toHaveBeenCalledExactlyOnceWith('session-draft-1', 'diag-voice-start')

    fireEvent.change(input, { target: { value: '答案是B' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(answerDiagnostic).toHaveBeenCalledWith('session-draft-1', { prompt_id: 'prompt-voice-1', item_version: 'v1', answer: 'B', client_action_id: 'diagnostic-answer:prompt-voice-1', authorized_turn_id: 'diag-voice-answer' }))
    if (receiptStatus === 'needs_review') {
      expect((await screen.findAllByText(/当前评分方式不能可靠确认对错/)).length).toBeGreaterThan(0)
      expect(screen.queryByText(/本次能力域参考值100分/)).not.toBeInTheDocument()
    } else expect(await screen.findByText('下一题，请说明平方差公式。')).toBeInTheDocument()
    expect(admit).toHaveBeenCalledTimes(1)
  })

  it('runs a durable PET vocabulary round entirely through voice actions', async () => {
    const turns: AgentTurn[] = [
      {
        agent_turn_id: 'pet-vocabulary-start', transcript: '背单词', intent: 'start_pet_vocabulary_review',
        response_text: '好，我们复习到期词汇。', action: { type: 'start_pet_vocabulary_review' },
        orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
        completed_at: null, created_at: new Date().toISOString()
      },
      {
        agent_turn_id: 'pet-vocabulary-answer', transcript: '图书馆', intent: 'submit_pet_vocabulary_answer',
        response_text: '收到。', action: { type: 'submit_pet_vocabulary_answer', answer: '图书馆', vocabulary_prompt_id: 'pet-prompt-library', vocabulary_card_version: 'v1' },
        orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
        completed_at: null, created_at: new Date().toISOString()
      },
      {
        agent_turn_id: 'pet-vocabulary-cancel', transcript: '单词先到这里', intent: 'cancel_pet_vocabulary_review',
        response_text: '好，词汇复习先到这里。', action: { type: 'cancel_pet_vocabulary_review', vocabulary_prompt_id: 'pet-prompt-collect', vocabulary_card_version: 'v2' },
        orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null,
        completed_at: null, created_at: new Date().toISOString()
      }
    ]
    vi.spyOn(localApi, 'agentTurn').mockImplementation(async () => turns.shift()!)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const nextPetVocabulary = vi.spyOn(localApi, 'nextPetVocabulary').mockResolvedValue({
      card: {
        card_id: 'pet-card-library', review_mode: 'recognition',
        prompt: 'library 在这里可以表示什么意思？用中文或简单英文回答。',
        focus_kind: 'word', due_at: new Date().toISOString(), source_ref: 'textbook-page-1'
      },
      summary: { deck_size: 2, new_count: 2, learning_count: 0, due_count: 2, total_attempts: 0, lapse_count: 0 },
      sync: { available_confirmed: 2, inserted: 2, skipped_without_meaning: 0, deck_size: 2 }
    })
    const answerPetVocabulary = vi.spyOn(localApi, 'answerBoundPetVocabulary').mockResolvedValue({
      result: {
        attempt_id: 'pet-attempt-1', card_id: 'pet-card-library', review_mode: 'recognition',
        correct: true, expected: ['图书馆'], next_due_at: new Date().toISOString(),
        interval_days: 1, feedback: '答对了，已经安排下一次间隔复习。'
      },
      next_card: {
        card_id: 'pet-card-collect', review_mode: 'recognition',
        prompt: 'collect 在这里可以表示什么意思？',
        focus_kind: 'word', due_at: new Date().toISOString(), source_ref: 'textbook-page-2'
      },
      summary: { deck_size: 2, new_count: 1, learning_count: 1, due_count: 1, total_attempts: 1, lapse_count: 0 }
    })
    const cancelPetVocabulary = vi.spyOn(localApi, 'cancelBoundPetVocabulary').mockResolvedValue({ status: 'cancelled' })
    render(<SessionWorkspace initial={petStudyWorkspace} childName="小宇" childGrade="四年级" onBack={vi.fn()} />)

    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '背单词' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByText(/library 在这里/)).toBeInTheDocument()
    expect(nextPetVocabulary).toHaveBeenCalledWith('session-draft-1')

    fireEvent.change(input, { target: { value: '图书馆' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(answerPetVocabulary).toHaveBeenCalledWith('session-draft-1', '图书馆', 'pet-prompt-library', 'v1', 'pet-vocabulary-answer'))
    expect(await screen.findByText(/collect 在这里/)).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '单词先到这里' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(cancelPetVocabulary).toHaveBeenCalledWith('session-draft-1', 'pet-prompt-collect', 'v2'))
  })

  it.each([true, false])('acknowledges a spoken reflection only after durable save (success=%s)', async (success) => {
    const reflectionWorkspace: SessionWorkspaceState = {
      ...studyWorkspace, session: { ...studyWorkspace.session, state: 'reflection' }
    }
    let finishSave!: () => void
    let failSave!: () => void
    const pending = new Promise<Awaited<ReturnType<typeof localApi.saveSessionDraft>>>((resolve, reject) => {
      finishSave = () => resolve({ section: 'reflection', payload: { learned_text: '标准形式' }, updated_at: new Date().toISOString() })
      failSave = () => reject(new Error('synthetic durable storage failure'))
    })
    const save = vi.spyOn(localApi, 'saveSessionDraft').mockImplementation(async (_session, section, payload) => {
      if (section === 'reflection') return pending
      return { section, payload, updated_at: new Date().toISOString() }
    })
    const resolve = vi.fn()
    render(<SessionWorkspace initial={reflectionWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    await act(async () => window.dispatchEvent(new CustomEvent('hsc-reflection-voice-action', {
      detail: { action: 'update', field: 'learned', value: '标准形式', resolve }
    })))
    await waitFor(() => expect(save).toHaveBeenCalledWith('session-draft-1', 'reflection', {
      learned_text: '标准形式', difficult_text: '', next_action: ''
    }))
    expect(resolve).not.toHaveBeenCalled()
    expect(screen.getByLabelText('今天真正学会了什么？')).toHaveValue('标准形式')
    await act(async () => { if (success) finishSave(); else failSave() })
    await waitFor(() => expect(resolve).toHaveBeenCalledExactlyOnceWith(success))
    if (!success) expect(screen.getAllByText('草稿保存失败，内容仍在当前页面').length).toBeGreaterThan(0)
  })

  it('records and completes reflection through continuous voice actions', async () => {
    const reflectionWorkspace: SessionWorkspaceState = {
      ...studyWorkspace,
      session: { ...studyWorkspace.session, state: 'reflection' },
      plan_items: studyWorkspace.plan_items.map((item) => ({ ...item, status: 'completed', completed_at: new Date().toISOString() }))
    }
    const completedWorkspace: SessionWorkspaceState = {
      ...reflectionWorkspace,
      session: { ...reflectionWorkspace.session, state: 'completed', status: 'completed' }
    }
    const turns: AgentTurn[] = [
      {
        agent_turn_id: 'reflection-learned', transcript: '今天学会了分解因式', intent: 'reflection_learned',
        response_text: '记下来了。还有哪里不太确定？', action: { type: 'update_reflection', field: 'learned', value: '分解因式' },
        orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null, completed_at: null, created_at: new Date().toISOString()
      },
      {
        agent_turn_id: 'reflection-complete', transcript: '结束今天学习', intent: 'reflection_complete',
        response_text: '好，我保存今天的复盘并关闭摄像头和麦克风。', action: { type: 'complete_reflection', skipped: false },
        orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null, completed_at: null, created_at: new Date().toISOString()
      }
    ]
    vi.spyOn(localApi, 'agentTurn').mockImplementation(async () => turns.shift()!)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    vi.spyOn(localApi, 'saveSessionDraft').mockResolvedValue({ section: 'reflection', payload: {}, updated_at: new Date().toISOString() })
    const completeReflection = vi.spyOn(localApi, 'completeReflection').mockResolvedValue(completedWorkspace)
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(completedWorkspace)
    render(<SessionWorkspace initial={reflectionWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)

    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '今天学会了分解因式' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.getByLabelText('今天真正学会了什么？')).toHaveValue('分解因式'))

    fireEvent.change(input, { target: { value: '结束今天学习' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(completeReflection).toHaveBeenCalledWith('session-draft-1', expect.objectContaining({ learnedText: '分解因式', skipped: false })))
  })

  it('executes a classified Tutor check reply once through its durable server owner', async () => {
    const transcript = '我先找出方程的三个系数。'
    const reply = '这次对检查问题的回答已保留，仍属于有提示练习，尚未完成点评或独立核验。'
    const turn: AgentTurn = {
      agent_turn_id: 'owned-tutor-check', transcript, intent: 'tutor.capture_check_response',
      response_text: '我先保存这次检查回答。', action: { type: 'submit_tutor_check_response' },
      runtime_execution_location: 'server', runtime_capability: 'tutor.capture_check_response',
      orchestrator_version: 'agent_runtime_planner_v1', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: new Date().toISOString()
    }
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn)
    const execute = vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({
      turn: { ...turn, tool_status: 'completed', response_text: reply },
      execution: { capability: 'tutor.capture_check_response', response_text: reply,
        result: { status: 'response_saved', mastery_update_allowed: false, independent_evidence: false },
        evidence: ['child_check_response'] }
    })
    const tutor = vi.spyOn(localApi, 'askTutor')
    const grade = vi.spyOn(localApi, 'answerDiagnosticPrompt')
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(studyWorkspace)
    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: transcript } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByText(reply)).toBeInTheDocument()
    expect(execute).toHaveBeenCalledTimes(1)
    expect(execute).toHaveBeenCalledWith('owned-tutor-check')
    expect(tutor).not.toHaveBeenCalled()
    expect(grade).not.toHaveBeenCalled()
  })

  it('uses the generic runtime executor for migrated deterministic server actions and shows the durable objective', async () => {
    vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({
      session_id: 'session-draft-1',
      active_objective: {
        objective_id: 'objective-1', objective_type: 'execution', summary: '了解今天的学习进度',
        success_criteria: ['听到已完成和未完成数量'], status: 'active'
      },
      current_step: {
        step_id: 'step-1', capability_name: 'planning.describe', status: 'completed',
        plan_status: 'completed', expected_evidence: ['plan_status_read']
      }
    })
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({
      agent_turn_id: 'runtime-plan-status', transcript: '今天还有多少任务', intent: 'planning.describe',
      response_text: '我看一下今天的进度。',
      action: { type: 'describe_plan' },
      runtime_execution_location: 'server', runtime_capability: 'planning.describe',
      orchestrator_version: 'agent_runtime_planner_v1', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: new Date().toISOString()
    })
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({
      turn: {
        agent_turn_id: 'runtime-plan-status', transcript: '今天还有多少任务', intent: 'planning.describe',
        response_text: '今天共有1项学习任务，已经完成0项。', action: { type: 'describe_plan' },
        orchestrator_version: 'agent_runtime_planner_v1', tool_status: 'completed', tool_error_code: null,
        completed_at: new Date().toISOString(), created_at: new Date().toISOString()
      },
      execution: {
        capability: 'planning.describe', response_text: '今天共有1项学习任务，已经完成0项。',
        result: { total: 1, completed: 0 }, evidence: ['plan_status_read']
      }
    })
    const legacyOutcome = vi.spyOn(localApi, 'completeAgentTurn')
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(studyWorkspace)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    expect(await screen.findByText('了解今天的学习进度')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: '今天还有多少任务' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByText('今天共有1项学习任务，已经完成0项。')).toBeInTheDocument()
    expect(localApi.executeAgentTurn).toHaveBeenCalledWith('runtime-plan-status')
    expect(legacyOutcome.mock.calls.some(([turnId]) => turnId === 'runtime-plan-query')).toBe(false)
  })

  it('executes deterministic learning evaluation through the runtime instead of calling the tutor again', async () => {
    vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({
      session_id: 'session-draft-1',
      active_objective: {
        objective_id: 'learning-objective-1', objective_type: 'learning', summary: '理解浮力与排液重力的关系',
        success_criteria: ['能用自己的话解释关键关系'], status: 'active'
      },
      current_step: {
        step_id: 'learning-step-2', capability_name: 'learning.evaluate_response', status: 'completed',
        plan_status: 'waiting_child', expected_evidence: ['independent_response']
      }
    })
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({
      agent_turn_id: 'runtime-learning-eval', transcript: '浮力等于排开的液体受到的重力',
      intent: 'learning.evaluate_response', response_text: '我先检查你自己的说法。',
      action: {
        type: 'ask_tutor', capability: 'learning.evaluate_response', source: 'agent_runtime_deterministic',
        execution_location: 'local_model', question: '只评估当前目标下的孩子回答', subject: '物理'
      },
      orchestrator_version: 'agent_runtime_deterministic_v1', tool_status: 'pending', tool_error_code: null,
      completed_at: null, created_at: new Date().toISOString()
    })
    const execution = vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({
      turn: {
        agent_turn_id: 'runtime-learning-eval', transcript: '浮力等于排开的液体受到的重力',
        intent: 'learning.evaluate_response', response_text: '关键关系说对了。现在换一个情境再判断。',
        action: { type: 'ask_tutor' }, orchestrator_version: 'agent_runtime_deterministic_v1',
        tool_status: 'completed', tool_error_code: null, completed_at: new Date().toISOString(),
        created_at: new Date().toISOString()
      },
      execution: {
        capability: 'learning.evaluate_response', response_text: '关键关系说对了。现在换一个情境再判断。',
        result: { outcome_level: 'independent' }, evidence: ['independent_response', 'model_rubric']
      }
    })
    const askTutor = vi.spyOn(localApi, 'askTutor')
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(studyWorkspace)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: '浮力等于排开的液体受到的重力' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByText('关键关系说对了。现在换一个情境再判断。')).toBeInTheDocument()
    expect(execution).toHaveBeenCalledWith('runtime-learning-eval')
    expect(askTutor).not.toHaveBeenCalled()
  })
})
