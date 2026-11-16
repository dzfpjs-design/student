import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import * as LocalInputs from './LocalInputs'
import * as voice from './companionVoice'
import * as ocr from './tutorOcrEvidence'
import * as petPlayback from './petListeningPlayback'
import { LEGACY_VOICE_EVENT_TIMEOUT_MS, requestOwnedVoiceEvent, type LocalCourseRecoveryContext } from './voiceJourneyRuntime'
import { SessionWorkspace } from './SessionWorkspace'
import { saveAgentRequest, AGENT_REQUEST_UNCONFIRMED } from './agentRequestJournal'
import { clearVoiceCaptureContext, voiceCaptureReference } from './voiceCaptureContext'
import { idleRealtimeVoiceStatus } from './realtimeVoiceRuntime'
import type { AgentTurn, DiagnosticItem, MistakeDialogue, SessionWorkspaceState, TutorTurn } from './types'
import type { ExamListeningState } from './examListeningDelivery'
import type { PetMedia } from './petMedia'

let microphone: ComponentProps<typeof LocalInputs.HandsFreeVoiceInput>
vi.mock('./LocalInputs', async (original) => {
  const actual = await original<typeof import('./LocalInputs')>()
  return { ...actual, HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null }, VoiceInputButton: () => null }
})
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))

function workspace(state = 'study'): SessionWorkspaceState {
  const now = new Date().toISOString()
  return {
    session: { session_id: 'voice-journey', child_id: 'child-a', status: 'active', state, started_at: now, last_checkpoint_at: now, observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status: 'paused', snooze_until: null, recall_declared_empty: 0, homework_declared_empty: 0 },
    family_rule: { child_id: 'child-a', latest_end_time: '21:30', max_learning_minutes: 120, task_segment_minutes: 25, break_minutes: 5, pet_min_minutes: 0, competition_minutes: 0, observation_mode: 'shadow', reminder_budget_per_10m: 1, misclassification_shadow_threshold: 3, version: 1, updated_at: now },
    planning_context: { energy_level: 'medium', fixed_commitment_minutes: 0, fixed_commitment_note: null, tomorrow_event_subject: null, tomorrow_event_title: null, tomorrow_event_minutes: 0, updated_at: now },
    recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [], drafts: {}, latest_tutor_turn: null, daily_report_ready: false,
    plan: { plan_id: 'plan-a', plan_date: now.slice(0, 10), status: 'active', total_minutes: 5, generator: 'local_rule_engine', version: 1 },
    plan_items: [{ plan_item_id: 'math-task', homework_id: null, subject: '数学', title: '数学作业', expected_minutes: 5, actual_minutes: null, priority: 1, status: 'active', position: 1, started_at: now, completed_at: null, active_seconds: 60, active_segment_started_at: null, source_type: 'school_homework', source_ref: null, completion_standard: '独立完成', cognitive_load: 'medium', task_type: 'written', completion_evidence_type: null, completion_note: null, completion_checklist: [], completion_artifact_id: null }],
    sensor_release: { opened_leases: 0, active_leases: 0, released_leases: 0, application_release_confirmed: false, evidence_level: 'not_opened', os_release_verified: false, release_verification: null, by_device: {} },
  }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
const recovery = () => ({ version: 1, sessionId: 'voice-journey', childId: 'child-a', actionId: 'saved-tutor-action', subject: '数学', question: '为什么要先列方程？', hintLevel: 1, createdAt: new Date().toISOString() })
const tutor = (): TutorTurn => ({ turn_id: 'saved-tutor', session_id: 'voice-journey', child_id: 'child-a', subject: '数学', question: recovery().question, answer: '先找出数量关系，再用字母表示未知数。', hint_level: 1, created_at: new Date().toISOString(), output_envelope: null } as TutorTurn)
let turnSequence = 0
function turn(action: AgentTurn['action'], text = '收到。'): AgentTurn {
  return { agent_turn_id: `journey-${++turnSequence}`, transcript: '', intent: action.type, response_text: text, action, tool_status: 'pending', tool_error_code: null, completed_at: null, created_at: new Date().toISOString(), orchestrator_version: 'test-contract' }
}

const acknowledgeTurn: typeof localApi.completeAgentTurn = async (id, response, status = 'completed', error) => ({
  ...turn({ type: 'none' }, response), agent_turn_id: id, tool_status: status,
  tool_error_code: error ?? null, completed_at: new Date().toISOString(),
})

describe('cross-activity voice journey contracts', () => {
  let current: SessionWorkspaceState
  const spoken = () => vi.mocked(LocalInputs.speakLocally).mock.calls.map(([text]) => text)
  const say = async (text: string) => { await act(async () => { await microphone.onTranscript(text) }) }
  async function mount() {
    let view!: ReturnType<typeof render>
    await act(async () => { view = render(<SessionWorkspace initial={current} childName="小辰" childGrade="初三" onBack={vi.fn()} />) })
    return view
  }
  const pageReviewTurn = () => ({ ...turn({ type: 'control_document_monitor', command: 'review',
    voice_input_id: 'original-voice', voice_input_binding: { child_id: 'child-a', session_id: 'voice-journey',
      question_validation: 'valid', question_reference: { kind: 'document', binding: {
        revision_id: 'original-page', artifact_id: 'original-artifact', content_sha256: 'c'.repeat(64), page_sha256: 'd'.repeat(64),
      } } } }), runtime_capability: 'document.monitor_control', runtime_execution_location: 'client_device_bridge' as const })
  const originalPage = () => ({ revision_id: 'original-page', artifact_id: 'original-artifact', child_id: 'child-a',
    session_id: 'voice-journey', content_sha256: 'c'.repeat(64), revision_index: 1,
    extracted_text: '原页问题：物体沿斜面运动时，哪些力做功？', review_required: true,
    page_source: { sha256: 'd'.repeat(64), status: 'retained' },
    source_voice_input_id: 'original-voice', local_only: true, cloud_transmission_allowed: false, mastery_update_allowed: false, page_status: 'text_available',
  }) as Awaited<ReturnType<typeof localApi.originalDocumentVoiceSource>>

  it('reads a voice-bound original page into the saved study review without recapturing the live camera', async () => {
    const requested = pageReviewTurn()
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    const read = vi.spyOn(localApi, 'originalDocumentVoiceSource').mockResolvedValue(originalPage())
    const events = vi.fn()
    window.addEventListener('hsc-document-monitor-voice-action', events)
    await mount()
    await say('刚才这道题我卡住了，能陪我理一理吗')
    expect(read).toHaveBeenCalledExactlyOnceWith('voice-journey', requested.agent_turn_id, expect.any(AbortSignal))
    expect(events).not.toHaveBeenCalled()
    expect(spoken().join(' ')).toContain('物体沿斜面运动')
    await waitFor(() => expect(localApi.saveSessionDraft).toHaveBeenCalledWith('voice-journey', 'study', expect.objectContaining({
      tutor_ocr_review: expect.objectContaining({ artifactId: 'original-artifact', text: originalPage().extracted_text }),
    })))
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith(requested.agent_turn_id, expect.any(String), 'completed', undefined,
      expect.objectContaining({ execution_receipt: expect.objectContaining({ output:
        expect.objectContaining({ revision_id: 'original-page', artifact_id: 'original-artifact', page_status: 'text_available' }) }) }))
    window.removeEventListener('hsc-document-monitor-voice-action', events)
  })

  it('does not speak or replace study evidence when original page reading settles after unmount', async () => {
    const requested = pageReviewTurn(), pending = deferred<Awaited<ReturnType<typeof localApi.originalDocumentVoiceSource>>>()
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    const read = vi.spyOn(localApi, 'originalDocumentVoiceSource').mockReturnValue(pending.promise)
    const view = await mount()
    let speaking!: Promise<void>
    await act(async () => { speaking = Promise.resolve(microphone.onTranscript('帮我看开口时那页题目')) })
    await waitFor(() => expect(read).toHaveBeenCalled())
    view.unmount()
    await act(async () => { pending.resolve(originalPage()); await speaking })
    expect(spoken().join(' ')).not.toContain('物体沿斜面运动')
    expect(vi.mocked(localApi.saveSessionDraft).mock.calls.some(([, section, payload]) =>
      section === 'study' && (payload.tutor_ocr_review as { artifactId?: string } | undefined)?.artifactId === 'original-artifact')).toBe(false)
  })

  it('waits for the original study page to save before readback or a completed device receipt', async () => {
    const requested = pageReviewTurn(), saved = deferred<Awaited<ReturnType<typeof localApi.saveSessionDraft>>>()
    current.drafts.study = { updated_at: new Date().toISOString(), payload: { completion_note: '保留原完成备注' } }
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    vi.spyOn(localApi, 'originalDocumentVoiceSource').mockResolvedValue(originalPage())
    vi.mocked(localApi.saveSessionDraft).mockImplementation(async (_id, section, payload) => {
      if (section === 'study') return saved.promise
      return { section, payload, updated_at: new Date().toISOString() }
    })
    await mount()
    let speaking!: Promise<void>
    await act(async () => { speaking = Promise.resolve(microphone.onTranscript('帮我理一下刚才那页')) })
    await waitFor(() => expect(localApi.saveSessionDraft).toHaveBeenCalledWith('voice-journey', 'study', expect.objectContaining({
      tutor_ocr_review: expect.objectContaining({ artifactId: 'original-artifact' }),
    })))
    expect(spoken().join(' ')).not.toContain('物体沿斜面运动')
    expect(localApi.completeAgentTurn).not.toHaveBeenCalledWith(requested.agent_turn_id, expect.anything(), 'completed', expect.anything(), expect.anything())
    const payload = vi.mocked(localApi.saveSessionDraft).mock.calls.find(([, section]) => section === 'study')![2]
    expect(payload).toHaveProperty('diagnostic_track', 'school')
    await act(async () => { saved.resolve({ section: 'study', payload, updated_at: new Date().toISOString() }); await speaking })
    expect(spoken().join(' ')).toContain('物体沿斜面运动')
  })

  it('does not announce a saved original page when its study draft write fails', async () => {
    const requested = pageReviewTurn()
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    vi.spyOn(localApi, 'originalDocumentVoiceSource').mockResolvedValue(originalPage())
    vi.mocked(localApi.saveSessionDraft).mockImplementation(async (_id, section, payload) => {
      if (section === 'study') throw new Error('disk unavailable')
      return { section, payload, updated_at: new Date().toISOString() }
    })
    await mount()
    await say('帮我核对原页')
    expect(spoken().join(' ')).not.toContain('物体沿斜面运动')
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith(requested.agent_turn_id, expect.any(String), 'failed', expect.any(String))
  })

  it('does not read back a late original page save after the study workspace is unmounted', async () => {
    const saved = deferred<Awaited<ReturnType<typeof localApi.saveSessionDraft>>>()
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(pageReviewTurn())
    vi.spyOn(localApi, 'originalDocumentVoiceSource').mockResolvedValue(originalPage())
    vi.mocked(localApi.saveSessionDraft).mockImplementation(async (_id, section, payload) => {
      if (section === 'study') return saved.promise
      return { section, payload, updated_at: new Date().toISOString() }
    })
    const view = await mount()
    let speaking!: Promise<void>
    await act(async () => { speaking = Promise.resolve(microphone.onTranscript('核对我刚才那页')) })
    await waitFor(() => expect(localApi.saveSessionDraft).toHaveBeenCalledWith('voice-journey', 'study', expect.anything()))
    view.unmount()
    await act(async () => { saved.resolve({ section: 'study', payload: {}, updated_at: new Date().toISOString() }); await speaking })
    expect(spoken().join(' ')).not.toContain('物体沿斜面运动')
  })

  it('keeps the newer page when an earlier owned draft save finishes late', async () => {
    const saved = deferred<Awaited<ReturnType<typeof localApi.saveSessionDraft>>>()
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(pageReviewTurn())
    vi.spyOn(localApi, 'originalDocumentVoiceSource').mockResolvedValue(originalPage())
    let firstStudy = true
    vi.mocked(localApi.saveSessionDraft).mockImplementation(async (_id, section, payload) => {
      if (section === 'study' && firstStudy) { firstStudy = false; return saved.promise }
      return { section, payload, updated_at: new Date().toISOString() }
    })
    await mount()
    let speaking!: Promise<void>
    await act(async () => { speaking = Promise.resolve(microphone.onTranscript('核对我开口时那页')) })
    await waitFor(() => expect(localApi.saveSessionDraft).toHaveBeenCalledWith('voice-journey', 'study', expect.anything()))
    const newer = { ...originalPage(), artifact_id: 'newer-artifact', extracted_text: '下一页的题干，不能被旧回执清除。' }
    const resolve = vi.fn()
    await act(async () => { window.dispatchEvent(new CustomEvent('hsc-document-review-revision', { cancelable: true,
      detail: { sessionId: 'voice-journey', childId: 'child-a', revision: newer, resolve } })) })
    await act(async () => { saved.resolve({ section: 'study', payload: {}, updated_at: new Date().toISOString() }); await speaking })
    await waitFor(() => expect(resolve).toHaveBeenCalledWith(true))
    expect(spoken().join(' ')).not.toContain('物体沿斜面运动')
    expect(vi.mocked(localApi.saveSessionDraft).mock.calls.filter(([, section]) => section === 'study').at(-1)?.[2])
      .toEqual(expect.objectContaining({ tutor_ocr_review: expect.objectContaining({ artifactId: 'newer-artifact', text: newer.extracted_text }) }))
  })
  it('reads child-friendly school review names when generating the daily plan', async () => {
    current = workspace('planning')
    const generated = workspace('planning')
    generated.plan_items[0] = { ...generated.plan_items[0], status: 'pending',
      source_type: 'due_review', source_ref: 'school-review:one', title: '数量关系 · school_open_near复测' }
    vi.spyOn(localApi, 'generatePlan').mockResolvedValue(generated)
    vi.mocked(localApi.agentTurn).mockResolvedValue(turn({ type: 'generate_plan' }))
    await mount()
    await say('帮我安排今天的学习')
    expect(localApi.generatePlan).toHaveBeenCalledExactlyOnceWith('voice-journey')
    expect(spoken().join(' ')).toContain('数量关系 · 换一道类似题')
    expect(spoken().join(' ')).not.toContain('school_open_')
    expect(generated.plan_items[0].title).toBe('数量关系 · school_open_near复测')
  })

  function restoreStudyQuestion() {
    current.drafts.study = { updated_at: new Date().toISOString(), payload: { question: recovery().question, tutor_request_recovery: recovery() } }
  }
  beforeEach(() => {
    current = workspace()
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
      getWakeWordSettings: vi.fn(async () => ({ childId: 'child-a', wakeWord: '小知', enabled: true, inactivitySeconds: 60, updatedAt: new Date().toISOString() })),
      saveWakeWordSettings: vi.fn(),
      getVoiceOnboarding: vi.fn(async () => ({ childId: 'child-a', version: 1, step: 'completed', updatedAt: new Date().toISOString() })), saveVoiceOnboarding: vi.fn(),
    }))
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('UNEXPECTED_NETWORK')))
    for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
      vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
        handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
    }
    vi.spyOn(voice, 'getCompanionVoiceState').mockReturnValue('idle')
    vi.spyOn(LocalInputs, 'speakLocally').mockImplementation((_text, _session, done) => { done?.('text_only') })
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(turn({ type: 'none' }, '普通表达已收到。'))
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(acknowledgeTurn)
    vi.spyOn(localApi, 'sessionWorkspace').mockImplementation(async () => current)
    vi.spyOn(localApi, 'saveSessionDraft').mockImplementation(async (_id, section, payload) => ({ section, payload, updated_at: new Date().toISOString() }))
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    vi.spyOn(localApi, 'askTutor').mockResolvedValue(tutor())
    vi.spyOn(localApi, 'pausePlanItem').mockImplementation(async () => { current = { ...current, plan_items: current.plan_items.map((item) => ({ ...item, status: 'paused' })) } })
  })
  afterEach(() => { cleanup(); clearVoiceCaptureContext(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

  it.each([
    ['你好小知，今天见到你很开心。', '很高兴见到你，我们聊聊今天吧。'],
    ['今天上课好累，想和你聊一会儿。', '听起来今天挺累的。哪一段让你特别费劲？'],
    ['一元二次方程', '你是想聊聊今天学的内容，还是有具体问题？'],
  ])('speaks the current independent dialogue without teaching the retained page: %s', async (quote, reply) => {
    current.drafts.study = { updated_at: new Date().toISOString(), payload: { tutor_ocr_review: {
      artifactId: originalPage().artifact_id, text: originalPage().extracted_text,
    } } }
    const originalDraft = structuredClone(current.drafts.study)
    const requested = { ...turn({ type: 'none', source: 'agent_runtime_planner' }, reply),
      transcript: quote, runtime_capability: 'conversation.respond', runtime_execution_location: 'server' as const }
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn: requested,
      execution: { capability: 'conversation.respond', result: null, evidence: [], response_text: reply } })
    const read = vi.spyOn(localApi, 'originalDocumentVoiceSource')
    await mount()
    await say(quote)
    expect(spoken()).toContain(reply)
    expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith(requested.agent_turn_id)
    expect(read).not.toHaveBeenCalled()
    expect(localApi.askTutor).not.toHaveBeenCalled()
    expect(current.drafts.study).toEqual(originalDraft)
    expect(vi.mocked(localApi.saveSessionDraft).mock.calls.some(([, section]) => section === 'study')).toBe(false)
    expect(spoken().join(' ')).not.toContain(originalPage().extracted_text)
  })

  it('speaks the inspected local resources receipt rather than an invented planning count', async () => {
    const quote = '我们先不看这张纸，你有PET学习资料可以给我介绍吗？'
    const requested = { ...turn({ type: 'inspect_learning_system', scope: 'resources', track: 'pet' }, '我有一百万道PET题。'),
      transcript: quote, runtime_capability: 'system.inspect_learning', runtime_execution_location: 'server' as const }
    const verified = { ...requested, response_text: '本机有PET练习资料，未经校准的内容不能用于掌握判断。' }
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn: verified,
      execution: { capability: 'system.inspect_learning', result: { cloud_transmission_allowed: false },
        evidence: ['synthetic-local-resource-receipt'], response_text: verified.response_text } })
    const read = vi.spyOn(localApi, 'originalDocumentVoiceSource')
    await mount()
    await say(quote)
    expect(spoken()).toContain(verified.response_text)
    expect(spoken().join(' ')).not.toContain('一百万')
    expect(read).not.toHaveBeenCalled()
    expect(localApi.askTutor).not.toHaveBeenCalled()
  })

  it('does not label saved wake settings as a working listener before microphone startup', async () => {
    await mount()
    expect(screen.queryByText('唤醒词“小知”已启用')).not.toBeInTheDocument()
    expect(screen.getByText('唤醒词“小知”已保存，监听尚未启动')).toBeInTheDocument()
  })

  it('shows a microphone failure instead of claiming wake recognition is enabled', async () => {
    await mount()
    await act(async () => microphone.onRuntimeStatusChange?.({ ...idleRealtimeVoiceStatus, phase: 'error', detail: '麦克风权限被拒绝' }))
    expect(screen.queryByText('唤醒词“小知”已启用')).not.toBeInTheDocument()
    expect(screen.getByText('麦克风暂不可用，唤醒监听未启动')).toBeInTheDocument()
  })

  it('does not promise wake standby during a conversation with the wake word disabled', async () => {
    vi.mocked(window.desktop!.getWakeWordSettings!).mockResolvedValue({ childId: 'child-a', wakeWord: '小知', enabled: false, inactivitySeconds: 60, updatedAt: new Date().toISOString() })
    await mount()
    await act(async () => microphone.onRuntimeStatusChange?.({ ...idleRealtimeVoiceStatus, phase: 'listening' }))
    expect(screen.getByText('正在听你说，唤醒词待机未启用')).toBeInTheDocument()
    expect(screen.queryByText('连续对话中，静默60秒后待机')).not.toBeInTheDocument()
  })

  it('offers the canonical whole-paper resume command when a saved exam owner cannot be verified', async () => {
    const requested = { ...turn({ type: 'start_exam_simulation' }), agent_turn_id: 'exam-owner-read-failure', runtime_capability: 'exam.start', runtime_execution_location: 'server' as const }
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn: requested,
      execution: { capability: 'exam.start', response_text: '不应播出的成功 ACK', result: { run_id: 'owner-run', child_id: 'child-a', session_id: 'voice-journey', status: 'active', current_position: 1 }, evidence: [] } })
    vi.spyOn(localApi, 'activeExamSimulation').mockRejectedValue(new Error('current owner unavailable'))
    const playback = vi.spyOn(petPlayback, 'playPetListening')
    await mount(); await say('开始模拟卷')
    expect(screen.getByText(/再说“继续模拟卷”/)).toBeInTheDocument()
    expect(spoken()).toEqual(expect.arrayContaining([expect.stringContaining('继续模拟卷')]))
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    expect(playback).not.toHaveBeenCalled()
    expect(spoken()).not.toContain('不应播出的成功 ACK')
    expect(spoken()).not.toContain('继续模拟考')
  })

  it('consumes a fresh listening cue in the main voice shell using canonical media and only the current exam answer owner', async () => {
    const item: DiagnosticItem = { diagnostic_item_id: 'canonical-item', knowledge_node_id: 'listening-node', track: 'school', subject: '英语',
      title: '公开合成听力题', prompt: 'Where will they meet?', answer_type: 'listening_text', difficulty: 2,
      pet_audio_modality: 'listening', stimulus_text: null, stimulus_asset: null }
    const binding = { run_id: 'listening-run', position: 1, item_id: item.diagnostic_item_id, source_version: 'slot-version' }
    const media: PetMedia = { version: 'pet_media_v1', media_id: 'canonical-media', child_id: 'child-a', session_id: 'voice-journey',
      prompt_id: 'canonical-prompt', item_version: 'canonical-version', diagnostic_item_id: item.diagnostic_item_id,
      source_version: 'audited-source', kind: 'listening', revision: 0, generation: 0, status: 'ready', source_reviewed: true,
      resume_required: true, frame_position: 0, receipt_grace_ms: 0, audio: { status: 'valid', sha256: 'a'.repeat(64) },
      qualification: { listening_delivery_qualified: false, skill_qualified: false, blockers: [], required_play_count: 1,
        task_complete_play_count: 0, task_delivery_qualified: false, delivery_requirement: { version: 'required-v1' } }, local_only: true, cloud_transmission_allowed: false }
    const exam: ExamListeningState = { run_id: binding.run_id, child_id: 'child-a', session_id: 'voice-journey', status: 'active',
      current_position: 1, current_item: item, delivery_status: 'active', answer_binding: binding,
      audio_delivery: { version: 'exam_listening_delivery_v1', run_id: binding.run_id, child_id: 'child-a', session_id: 'voice-journey',
        position: 1, item_id: item.diagnostic_item_id, unit_key: 'audited-unit', prompt_id: media.prompt_id, item_version: media.item_version,
        media_item_id: item.diagnostic_item_id, control_revision: 1, playback_allowed: true,
        resource: { media_id: media.media_id, source_version: media.source_version, audio_sha256: media.audio.sha256!, item_version: media.item_version,
          requirement_version: 'required-v1', required_play_count: 1, generation: 0, revision: 0, status: 'ready', completed_play_count: 0 },
        media_prompt: { child_id: 'child-a', origin_session_id: 'voice-journey', prompt_id: media.prompt_id, item_version: media.item_version,
          diagnostic_item_id: item.diagnostic_item_id, owner_kind: 'exam', item }, local_only: true, cloud_transmission_allowed: false } }
    const requested = { ...turn({ type: 'start_exam_simulation' }), agent_turn_id: 'fresh-listening-main-shell', runtime_capability: 'exam.start', runtime_execution_location: 'server' as const }
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn: requested, execution: { capability: 'exam.start', response_text: '原听力题', result: exam, evidence: [] } })
    vi.spyOn(localApi, 'activeExamSimulation').mockResolvedValue({ exam })
    vi.spyOn(localApi, 'petPromptMedia').mockResolvedValue({ child_id: 'child-a', session_id: 'voice-journey', prompt_id: media.prompt_id,
      item_version: media.item_version, listening: [media], speaking: [], selection_required: false, status: 'bound_media', local_only: true, cloud_transmission_allowed: false })
    vi.spyOn(localApi, 'petMediaControl').mockResolvedValue({ media: { ...media, generation: 1, revision: 1, status: 'playing' }, local_only: true, cloud_transmission_allowed: false })
    vi.spyOn(petPlayback, 'playPetListening').mockResolvedValue({ status: 'finished', response: '原音播完了，请说出你的答案。' })
    await mount(); await say('开始模拟卷')
    expect(petPlayback.playPetListening).toHaveBeenCalledWith('voice-journey', exam.audio_delivery!.media_prompt,
      'fresh-listening-main-shell:exam', expect.any(Function), expect.any(AbortSignal), 4,
      expect.objectContaining({ media_id: 'canonical-media', generation: 1, status: 'playing' }), expect.any(Function))
    expect(voiceCaptureReference('voice-journey', 'child-a').question_reference).toEqual({ kind: 'exam', binding })
    expect(screen.getByRole('region', { name: '当前考试题' })).toHaveTextContent('Where will they meet?')
    expect(screen.getByRole('region', { name: '当前考试题' })).toHaveTextContent('原音播完了')
    const reading: ExamListeningState = { ...exam, current_position: 2,
      current_item: { ...item, diagnostic_item_id: 'reading-item', prompt: 'Which day should the book be returned?', answer_type: 'text',
        pet_audio_modality: null, stimulus_text: 'Public original reading note: return the book on Friday.', stimulus_asset: 'pet-study-room' },
      audio_delivery: null, answer_binding: { run_id: binding.run_id, position: 2, item_id: 'reading-item', source_version: 'reading-source' } }
    const next = { ...turn({ type: 'none' }), agent_turn_id: 'mixed-reading-shell', runtime_capability: 'exam.submit', runtime_execution_location: 'server' as const }
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(next)
    vi.mocked(localApi.executeAgentTurn).mockResolvedValueOnce({ turn: next, execution: { capability: 'exam.submit', response_text: '下一题', result: { exam: reading }, evidence: [] } })
    vi.mocked(localApi.activeExamSimulation).mockResolvedValue({ exam: reading })
    await say('提交刚才的考试答案')
    const question = screen.getByRole('region', { name: '当前考试题' })
    expect(question).toHaveTextContent('Which day should the book be returned?')
    expect(question).toHaveTextContent('Public original reading note: return the book on Friday.')
    expect(question).not.toHaveTextContent('Where will they meet?')
    expect(question.querySelector('img')).toHaveAttribute('src', expect.stringContaining('pet-study-room'))
    expect(petPlayback.playPetListening).toHaveBeenCalledTimes(1)
    expect(voiceCaptureReference('voice-journey', 'child-a').question_reference).toEqual({ kind: 'exam', binding: reading.answer_binding })
  })

  it('freezes the displayed exam slot from its real server execution response and clears it on pause', async () => {
    const binding = { run_id: 'run-a', position: 1, item_id: 'exam-item-a', source_version: 'exam-source-a' }
    const original: DiagnosticItem = { diagnostic_item_id: 'exam-item-a', knowledge_node_id: 'exam-node', track: 'pet', subject: '英语', title: '阅读', prompt: 'What is the note about?', answer_type: 'text', difficulty: 1, stimulus_text: 'Public original note', stimulus_asset: null }
    const startTurn = { ...turn({ type: 'start_exam_simulation' }), runtime_capability: 'exam.start', runtime_execution_location: 'server' as const }
    const pauseTurn = { ...turn({ type: 'pause_exam_simulation' }), runtime_capability: 'exam.pause', runtime_execution_location: 'server' as const }
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(startTurn).mockResolvedValueOnce(pauseTurn)
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValueOnce({ turn: startTurn,
      execution: { capability: 'exam.start', response_text: '第一题', result: { run_id: 'run-a', child_id: 'child-a', session_id: 'voice-journey', status: 'active', current_position: 1, current_item: original, answer_binding: binding }, evidence: [] } })
      .mockResolvedValueOnce({ turn: pauseTurn,
        execution: { capability: 'exam.pause', response_text: '已暂停', result: { run_id: 'run-a', child_id: 'child-a', session_id: 'voice-journey', status: 'paused' }, evidence: [] } })
    vi.spyOn(localApi, 'activeExamSimulation')
      .mockResolvedValueOnce({ exam: { run_id: 'run-a', child_id: 'child-a', session_id: 'voice-journey', status: 'active', current_position: 1, current_item: original, delivery_status: 'ready', answer_binding: binding } })
      .mockResolvedValueOnce({ exam: { run_id: 'run-a', child_id: 'child-a', session_id: 'voice-journey', status: 'paused', current_position: 1, current_item: original, delivery_status: 'ready', answer_binding: null } })
    await mount(); await say('开始模拟卷')
    const captured = voiceCaptureReference('voice-journey', 'child-a')
    expect(captured.question_reference).toEqual({ kind: 'exam', binding })
    await say('先暂停模拟卷')
    expect(voiceCaptureReference('voice-journey', 'child-a').question_reference).toBeNull()
    expect(captured.question_reference).toEqual({ kind: 'exam', binding })
  })

  it('binds the shown vocabulary card and then the next card, without changing earlier captures', async () => {
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn({ type: 'start_pet_vocabulary_review' }))
      .mockResolvedValueOnce(turn({ type: 'submit_pet_vocabulary_answer', answer: 'book', vocabulary_prompt_id: 'word-a', vocabulary_card_version: 'v1' }))
    vi.spyOn(localApi, 'nextPetVocabulary').mockResolvedValue({ card: { prompt_session_id: 'word-a', card_version: 'v1', prompt: '书的英文是什么？' }, summary: { deck_size: 2 } } as never)
    vi.spyOn(localApi, 'answerBoundPetVocabulary').mockResolvedValue({ result: { expected: 'book', correct: true, feedback: '这次回答正确。' }, next_card: { prompt_session_id: 'word-b', card_version: 'v2', prompt: '现在看下一张词卡。' } } as never)
    await mount(); await say('开始复习单词')
    const original = voiceCaptureReference('voice-journey', 'child-a')
    expect(original.question_reference).toEqual({ kind: 'vocabulary', binding: { prompt_session_id: 'word-a', card_version: 'v1' } })
    await say('我的答案是book')
    expect(voiceCaptureReference('voice-journey', 'child-a').question_reference).toEqual({ kind: 'vocabulary', binding: { prompt_session_id: 'word-b', card_version: 'v2' } })
    expect(original.question_reference).toMatchObject({ binding: { prompt_session_id: 'word-a' } })
  })

  it('preserves each original recording through the busy voice FIFO', async () => {
    const pending = deferred<AgentTurn>()
    vi.mocked(localApi.agentTurn).mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(turn({ type: 'none' }, '先保留你的补充。'))
    await mount()
    let first!: Promise<unknown>
    let second!: Promise<unknown>
    await act(async () => {
      first = Promise.resolve(microphone.onTranscript('我的答案是十二', undefined, 'voice-original-answer'))
      second = Promise.resolve(microphone.onTranscript('等一下，我还没有说完', undefined, 'voice-original-correction'))
    })
    expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
    expect(microphone.interactionBusy).toBe(true)
    await act(async () => {
      pending.resolve(turn({ type: 'none' }, '我等你补充。'))
      await Promise.all([first, second])
    })
    expect(vi.mocked(localApi.agentTurn).mock.calls.map(([session, text, , id]) => ({ session, text, id }))).toEqual([
      { session: 'voice-journey', text: '我的答案是十二', id: 'voice-original-answer' },
      { session: 'voice-journey', text: '等一下，我还没有说完', id: 'voice-original-correction' },
    ])
    expect(microphone.interactionBusy).toBe(false)
  })

  it('routes natural listening pause to the bound original media and records the real control result', async () => {
    const binding = { version: 'pet_listening_voice_v1', child_id: 'child-a', session_id: 'voice-journey',
      media_id: 'original-audio', prompt_id: 'original-prompt', item_version: 'v1', source_version: 'source1', generation: 1, expected_revision: 3 }
    const request = turn({ type: 'control_pet_listening', capability: 'pet.listening_control', execution_location: 'client_device_bridge',
      command: 'pause', pet_listening_binding: binding })
    vi.mocked(localApi.agentTurn).mockResolvedValue(request)
    const stop = vi.spyOn(petPlayback, 'stopPetListening').mockReturnValue(true)
    const control = vi.spyOn(localApi, 'petListeningAgentControl').mockResolvedValue({ media: { ...binding, status: 'paused' },
      replayed: false, replay_audio_allowed: false, local_only: true, cloud_transmission_allowed: false } as never)
    await mount(); await say('听力先停一下')
    expect(stop).toHaveBeenCalledWith('voice-journey', 'original-audio')
    expect(control).toHaveBeenCalledWith('voice-journey', request.agent_turn_id)
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith(request.agent_turn_id, expect.stringContaining('已暂停'), 'completed', undefined,
      expect.objectContaining({ execution_receipt: expect.objectContaining({ action_type: 'control_pet_listening' }) }))
  })

  it('microphone speech interrupts a busy original listening action before its playback promise completes', async () => {
    const original = { prompt_id: 'p', item_version: 'v', child_id: 'child-a', diagnostic_item_id: 'i',
      origin_session_id: 'voice-journey', item: { track: 'pet', answer_type: 'listening_text', prompt: 'Where?' } }
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({ status: 'waiting_answer', prompt: original, attempt: null } as never)
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn({ type: 'start_diagnostic', track: 'pet' }))
      .mockResolvedValueOnce(turn({ type: 'none' }, '听力先暂停。'))
    const playback = deferred<petPlayback.PetListeningResult>()
    const play = vi.spyOn(petPlayback, 'playPetListening').mockReturnValue(playback.promise)
    const interrupt = vi.spyOn(petPlayback, 'interruptPetListening').mockImplementation(() => {
      if (!play.mock.calls.length) return false
      playback.resolve({ status: 'paused', response: '原音暂停。' }); return true
    })
    await mount()
    let first!: Promise<void>
    await act(async () => { first = microphone.onTranscript('做听力吧') as Promise<void> })
    await vi.waitFor(() => expect(play).toHaveBeenCalledOnce())
    await say('等我一下')
    await act(async () => { await first })
    expect(interrupt).toHaveBeenLastCalledWith('voice-journey')
    await vi.waitFor(() => expect(localApi.agentTurn).toHaveBeenCalledTimes(2))
    expect(vi.mocked(localApi.agentTurn).mock.calls[1][1]).toBe('等我一下')
  })

  it('does not replay listening on a recovered control receipt or stop another child audio', async () => {
    const binding = { version: 'pet_listening_voice_v1', child_id: 'child-a', session_id: 'voice-journey',
      media_id: 'original-audio', prompt_id: 'original-prompt', item_version: 'v1', source_version: 'source1', generation: 1, expected_revision: 3 }
    vi.mocked(localApi.agentTurn).mockResolvedValue(turn({ type: 'control_pet_listening', capability: 'pet.listening_control',
      execution_location: 'client_device_bridge', command: 'resume', pet_listening_binding: binding }))
    const play = vi.spyOn(petPlayback, 'playPetListening').mockResolvedValue({ status: 'finished', response: 'not expected' })
    const stop = vi.spyOn(petPlayback, 'stopPetListening').mockReturnValue(false)
    const control = vi.spyOn(localApi, 'petListeningAgentControl').mockResolvedValue({ media: { ...binding, status: 'playing' },
      replayed: true, replay_audio_allowed: false, local_only: true, cloud_transmission_allowed: false } as never)
    await mount(); await say('接着放听力吧')
    expect(control).toHaveBeenCalledOnce(); expect(play).not.toHaveBeenCalled()
    vi.mocked(localApi.agentTurn).mockResolvedValue(turn({ type: 'control_pet_listening', capability: 'pet.listening_control',
      execution_location: 'client_device_bridge', command: 'pause', pet_listening_binding: { ...binding, child_id: 'other-child' } }))
    await say('先停一下听力')
    expect(control).toHaveBeenCalledOnce(); expect(stop).not.toHaveBeenCalled()
  })

  it('keeps microphone failures visible outside hidden child controls and clears them on recovery', async () => {
    await mount()
    act(() => microphone.onRuntimeStatusChange?.({ runtime: 'segmented_local', phase: 'error',
      speechActive: false, pendingUtterances: 0, recoverable: true, detail: '麦克风连接中断，正在自动重连。' }))
    const status = screen.getByText('麦克风连接中断，正在自动重连。')
    expect(status).toHaveAttribute('role', 'status')
    expect(status.closest('.agent-voice-controls')).toBeNull()
    act(() => microphone.onRuntimeStatusChange?.({ runtime: 'segmented_local', phase: 'listening',
      speechActive: false, pendingUtterances: 0, recoverable: true }))
    expect(screen.queryByText('麦克风连接中断，正在自动重连。')).toBeNull()
  })

  it('executes the next persisted result turn without inventing another child utterance', async () => {
    const first = { ...turn({ type: 'pause_active_task' }, '先暂停当前任务。'), after_execution: 'replan' as const }
    const next = turn({ type: 'none' }, '任务已暂停，接下来想休息多久？')
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(first)
    const resume = vi.spyOn(localApi, 'continueAgentTurn').mockResolvedValue({ status: 'ready', turn: next })
    await mount()
    await say('先暂停，再帮我安排休息')
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(resume).toHaveBeenCalledWith('voice-journey', first.agent_turn_id)
    expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith(next.agent_turn_id, next.response_text, 'completed')
    expect(spoken()).toContain(next.response_text)
  })

  it('discards an in-flight automatic plan when the child speaks again', async () => {
    const first = { ...turn({ type: 'pause_active_task' }), after_execution: 'replan' as const }
    const next = turn({ type: 'none' }, '这句旧计划不能播放。')
    const pending = deferred<Awaited<ReturnType<typeof localApi.continueAgentTurn>>>()
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(first)
    const resume = vi.spyOn(localApi, 'continueAgentTurn').mockReturnValue(pending.promise)
    await mount(); await say('暂停再安排休息')
    expect(resume).toHaveBeenCalledTimes(1)
    await say('不用安排了')
    await act(async () => { pending.resolve({ status: 'ready', turn: next }); await Promise.resolve() })
    expect(spoken()).not.toContain(next.response_text)
    expect(vi.mocked(localApi.completeAgentTurn).mock.calls.some(([id]) => id === next.agent_turn_id)).toBe(false)
  })

  it('recovers a persisted continuation only after waking, without another child command', async () => {
    vi.useFakeTimers()
    const next = turn({ type: 'none' }, '上一步已经保存，接下来请告诉我你的安排。')
    const status = vi.spyOn(localApi, 'continuationStatus').mockResolvedValue({ status: 'ready', parent_turn_id: 'restored-parent' })
    const resume = vi.spyOn(localApi, 'continueAgentTurn').mockResolvedValue({ status: 'ready', turn: next })
    await mount()
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(status).not.toHaveBeenCalled()
    await act(async () => { microphone.onWake?.(); await vi.advanceTimersByTimeAsync(2_000) })
    expect(resume).toHaveBeenCalledWith('voice-journey', 'restored-parent')
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(spoken()).toContain(next.response_text)
    await act(async () => { await vi.advanceTimersByTimeAsync(4_000) })
    expect(resume).toHaveBeenCalledTimes(1)
  })

  it('speaks the restored receipt warning on wake and holds autonomous continuation until checked', async () => {
    vi.useFakeTimers()
    saveAgentRequest('voice-journey', 'lost-voice-request')
    const status = vi.spyOn(localApi, 'continuationStatus').mockResolvedValue({ status: 'ready', parent_turn_id: 'old-parent' })
    const resume = vi.spyOn(localApi, 'continueAgentTurn')
    await mount()
    await act(async () => { microphone.onWake?.(); await vi.advanceTimersByTimeAsync(2500) })
    expect(spoken()).toContain(AGENT_REQUEST_UNCONFIRMED)
    expect(status).not.toHaveBeenCalled()
    expect(resume).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(vi.mocked(LocalInputs.speakLocally).mock.calls.find(([text]) => text === AGENT_REQUEST_UNCONFIRMED)?.[4]).toMatchObject({ localOnly: true })
  })

  it('gives untranscribed speech priority over a pending result continuation', async () => {
    vi.useFakeTimers()
    const status = vi.spyOn(localApi, 'continuationStatus').mockResolvedValue({ status: 'pending', parent_turn_id: 'pending-parent' })
    const next = turn({ type: 'none' })
    const resume = vi.spyOn(localApi, 'continueAgentTurn').mockResolvedValue({ status: 'ready', turn: next })
    await mount()
    await act(async () => {
      microphone.onWake?.()
      microphone.onRuntimeStatusChange?.({ runtime: 'segmented_local', phase: 'listening', speechActive: true, pendingUtterances: 0, recoverable: true })
      await vi.advanceTimersByTimeAsync(2_000)
    })
    expect(status).not.toHaveBeenCalled(); expect(resume).not.toHaveBeenCalled()
    await act(async () => {
      microphone.onRuntimeStatusChange?.({ runtime: 'segmented_local', phase: 'transcribing', speechActive: false, pendingUtterances: 1, recoverable: true })
      await vi.advanceTimersByTimeAsync(2_000)
    })
    expect(resume).not.toHaveBeenCalled()
    await act(async () => {
      microphone.onRuntimeStatusChange?.({ runtime: 'segmented_local', phase: 'listening', speechActive: false, pendingUtterances: 0, recoverable: true })
      await vi.advanceTimersByTimeAsync(2_000)
    })
    expect(resume).toHaveBeenCalledTimes(1)
  })

  it('announces an unconfirmed dispatched result once after waking and never repeats it', async () => {
    vi.useFakeTimers()
    vi.spyOn(localApi, 'continuationStatus').mockResolvedValue({ status: 'awaiting_receipt', parent_turn_id: 'dispatched-parent' })
    const resume = vi.spyOn(localApi, 'continueAgentTurn')
    await mount()
    await act(async () => { microphone.onWake?.(); await vi.advanceTimersByTimeAsync(6_000) })
    expect(resume).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(spoken().filter((line) => line.includes('不能据此判断是否已经执行'))).toHaveLength(1)
  })

  it('routes check-in, multiple subjects, planning, tutoring, a break/activity and next-day reflection without child clicks', async () => {
    current = { ...workspace('check_in'), plan: null, plan_items: [] }
    const issue = async (text: string, action: AgentTurn['action']) => {
      vi.mocked(localApi.agentTurn).mockResolvedValueOnce({ ...turn(action, `已处理：${text}`), ...(action.type === 'complete_active_task' ? { runtime_capability: 'task.complete', runtime_execution_location: 'server' as const } : {}) })
      await say(text)
    }
    vi.spyOn(localApi, 'executeAgentTurn').mockImplementation(async () => {
      const active = current.plan_items.find((item) => item.status === 'active')!
      current = { ...current, plan_items: current.plan_items.map((item) => item.plan_item_id === active.plan_item_id ? { ...item, status: 'completed' } : item) }
      return { turn: turn({ type: 'complete_active_task' }, '服务端已保存任务完成'), execution: { capability: 'task.complete', result: current.plan_items.find((item) => item.plan_item_id === active.plan_item_id), evidence: ['persisted_task'], response_text: '服务端已保存任务完成' } }
    })
    vi.spyOn(localApi, 'transition').mockImplementation(async (_session, from, to) => {
      expect(from).toBe(current.session.state)
      current = { ...current, session: { ...current.session, state: to } }
      return current.session
    })
    vi.spyOn(localApi, 'addRecall').mockImplementation(async (_session, subject, summary) => {
      const item = { recall_id: `recall-${subject}`, subject, summary } as SessionWorkspaceState['recalls'][number]
      current = { ...current, recalls: [...current.recalls, item] }
      return item
    })
    vi.spyOn(localApi, 'addHomework').mockImplementation(async (_session, subject, title, minutes) => {
      const item = { homework_id: `homework-${subject}`, subject, title, expected_minutes: minutes, task_type: 'written' } as SessionWorkspaceState['homework'][number]
      current = { ...current, homework: [...current.homework, item] }
      return item
    })
    vi.spyOn(localApi, 'generatePlan').mockImplementation(async () => {
      current = { ...current, session: { ...current.session, state: 'plan_review' }, plan: workspace().plan,
        plan_items: current.homework.map((item, index) => ({ ...workspace().plan_items[0], plan_item_id: `task-${index}`, subject: item.subject, title: item.title, status: 'pending' })) }
      return current
    })
    vi.spyOn(localApi, 'startPlanItem').mockImplementation(async (id) => { current = { ...current, plan_items: current.plan_items.map((item) => item.plan_item_id === id ? { ...item, status: 'active' } : item) } })
    vi.spyOn(localApi, 'completePlanItem').mockImplementation(async (id) => { current = { ...current, plan_items: current.plan_items.map((item) => item.plan_item_id === id ? { ...item, status: 'completed' } : item) } })
    vi.spyOn(localApi, 'beginBreak').mockImplementation(async () => {
      current = { ...current, session: { ...current.session, state: 'break' }, plan_items: current.plan_items.map((item) => item.status === 'active' ? { ...item, status: 'paused' } : item) }
      return current
    })
    vi.spyOn(localApi, 'resumeSession').mockImplementation(async () => {
      current = { ...current, session: { ...current.session, state: 'study' }, plan_items: current.plan_items.map((item) => item.status === 'paused' ? { ...item, status: 'active' } : item) }
      return current
    })
    vi.spyOn(localApi, 'startCompanionActivity').mockResolvedValue({} as Awaited<ReturnType<typeof localApi.startCompanionActivity>>)
    vi.spyOn(localApi, 'endCompanionActivity').mockResolvedValue({} as Awaited<ReturnType<typeof localApi.endCompanionActivity>>)
    vi.spyOn(localApi, 'saveTutorFeedback').mockResolvedValue({ error_hypothesis: '孩子口述理解，仍待独立检验' } as Awaited<ReturnType<typeof localApi.saveTutorFeedback>>)
    vi.spyOn(localApi, 'completeReflection').mockImplementation(async () => {
      current = { ...current, session: { ...current.session, state: 'completed', status: 'completed' } }
      return current
    })
    await mount()
    await issue('准备好了', { type: 'transition', target: 'classroom_recall' })
    await issue('数学学了方程', { type: 'add_recall', subject: '数学', summary: '方程' })
    await issue('英语学了过去时', { type: 'add_recall', subject: '英语', summary: '过去时' })
    await issue('开始说作业', { type: 'transition', target: 'homework_capture' })
    await issue('数学写三道题', { type: 'add_homework', subject: '数学', title: '三道题', expected_minutes: 15 })
    await issue('英语写练习册', { type: 'add_homework', subject: '英语', title: '练习册', expected_minutes: 20 })
    await issue('安排计划', { type: 'generate_plan' })
    expect(current.homework.map((item) => item.subject)).toEqual(['数学', '英语'])
    await issue('按这个开始', { type: 'transition', target: 'study' })
    await issue('开始任务', { type: 'start_next_task' })
    await issue(recovery().question, { type: 'ask_tutor', question: recovery().question, subject: '数学' })
    expect(spoken()).toContain(tutor().answer)
    expect(spoken()).not.toContain(`已处理：${recovery().question}`)
    await issue('我明白数量关系了', { type: 'record_tutor_feedback', outcome: 'understood', feedback_text: '数量关系' })
    expect(localApi.saveTutorFeedback).toHaveBeenCalled()
    await issue('休息一下', { type: 'begin_break' })
    await issue('讲一个小故事', { type: 'start_companion_activity', activity_type: 'interactive_story', topic: '勇敢' })
    await issue('故事先到这里', { type: 'end_companion_activity' })
    await issue('回到刚才的作业', { type: 'resume_study' })
    expect(current.plan_items[0].status).toBe('active')
    await issue('数学完成了', { type: 'complete_active_task' })
    await issue('开始英语', { type: 'start_next_task' })
    await issue('英语也完成了', { type: 'complete_active_task' })
    expect(localApi.executeAgentTurn).toHaveBeenCalledTimes(2)
    expect(localApi.completePlanItem).not.toHaveBeenCalled()
    await issue('结束学习', { type: 'request_end' })
    expect(current.session.state).toBe('reflection')
    await issue('今天学会了列方程', { type: 'update_reflection', field: 'learned', value: '会列方程' })
    await issue('还不确定过去时', { type: 'update_reflection', field: 'difficult', value: '过去时' })
    await issue('明天先复习英语', { type: 'update_reflection', field: 'next_action', value: '复习英语' })
    await issue('保存复盘', { type: 'complete_reflection' })
    expect(localApi.completeReflection).toHaveBeenCalledWith('voice-journey', expect.objectContaining({ learnedText: '会列方程', difficultText: '过去时', nextAction: '复习英语' }))
    expect(current.session.state).toBe('completed')
  })

  it.each([{ type: 'start_diagnostic' }, { type: 'submit_diagnostic_answer', answer: '四' }, { type: 'complete_reflection' }] as AgentTurn['action'][])('does not wedge the voice queue when %j has no owning screen', async (action) => {
    current = workspace('break')
    await mount()
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn(action))
    await say('请求另一项操作')
    await say('我想继续刚才的数学作业')
    expect(localApi.agentTurn).toHaveBeenCalledTimes(2)
    expect(spoken().at(-1)).toBe('普通表达已收到。')
  })

  it('does not consume a real sentence containing recovery words', async () => {
    current.drafts.agent_command = { updated_at: new Date().toISOString(), payload: { tutor_recovery: { ...recovery(), turnId: 'old-turn', attempts: 1 } } }
    await mount()
    await say('今天老师让我们保留问题明天重新问')
    expect(localApi.agentTurn).toHaveBeenCalledWith('voice-journey', '今天老师让我们保留问题明天重新问')
    expect(localApi.askTutor).not.toHaveBeenCalled()
  })

  it('retries a persisted study question by voice with the original request identity and local-only narration', async () => {
    restoreStudyQuestion()
    await mount()
    await say('重试本题')
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(localApi.askTutor).toHaveBeenCalledExactlyOnceWith('voice-journey', '数学', recovery().question, 1, undefined, 'saved-tutor-action', false)
    expect(vi.mocked(LocalInputs.speakLocally).mock.calls.find(([text]) => text === tutor().answer)?.[4]).toEqual(expect.objectContaining({ localOnly: true }))
  })

  it('dispatches study recovery with the original child, session and live cancellation signal', async () => {
    restoreStudyQuestion()
    await mount()
    const receive = vi.fn()
    window.addEventListener('hsc-study-tutor-recovery', receive)
    try { await say('重试本题') }
    finally { window.removeEventListener('hsc-study-tutor-recovery', receive) }
    expect(receive).toHaveBeenCalledOnce()
    const detail = (receive.mock.calls[0][0] as CustomEvent).detail
    expect(detail).toMatchObject({ command: 'retry', sessionId: 'voice-journey', childId: 'child-a' })
    expect(detail.signal).toBeInstanceOf(AbortSignal)
  })

  it.each(['retry', 'confirm', 'keep', 'continue'] as const)('does not claim foreign or missing identity for study recovery %s', async command => {
    restoreStudyQuestion()
    await mount()
    const saves = vi.mocked(localApi.saveSessionDraft).mock.calls.length
    const speech = spoken().length
    for (const identity of [{}, { sessionId: 'foreign-session', childId: 'child-a' }, { sessionId: 'voice-journey', childId: 'child-b' }]) {
      const resolve = vi.fn()
      const event = new CustomEvent('hsc-study-tutor-recovery', { cancelable: true, detail: { ...identity, command, resolve } })
      await act(async () => { window.dispatchEvent(event) })
      expect(event.defaultPrevented).toBe(false)
      expect(resolve).not.toHaveBeenCalled()
    }
    expect(localApi.askTutor).not.toHaveBeenCalled()
    expect(localApi.saveSessionDraft).toHaveBeenCalledTimes(saves)
    expect(spoken()).toHaveLength(speech)
  })

  it.each(['claimed', 'cancelled'] as const)('ignores %s study recovery without requesting or changing saved work', async state => {
    restoreStudyQuestion()
    await mount()
    const signal = new AbortController()
    const resolve = vi.fn()
    const event = new CustomEvent('hsc-study-tutor-recovery', { cancelable: true, detail: {
      sessionId: 'voice-journey', childId: 'child-a', command: 'retry', signal: signal.signal, resolve,
    } })
    if (state === 'claimed') event.preventDefault()
    else signal.abort()
    await act(async () => { window.dispatchEvent(event) })
    expect(resolve).not.toHaveBeenCalled()
    expect(localApi.askTutor).not.toHaveBeenCalled()
  })

  it('does not publish or clear a tutor result after the original recovery signal is cancelled', async () => {
    restoreStudyQuestion()
    await mount()
    const pending = deferred<TutorTurn>()
    vi.mocked(localApi.askTutor).mockReturnValueOnce(pending.promise)
    const controller = new AbortController()
    let requested!: Promise<boolean>
    await act(async () => { requested = requestOwnedVoiceEvent('hsc-study-tutor-recovery', {
      sessionId: 'voice-journey', childId: 'child-a', command: 'retry', signal: controller.signal,
    }, controller.signal).catch(() => false) })
    expect(localApi.askTutor).toHaveBeenCalledOnce()
    const speech = spoken().length
    await act(async () => { controller.abort(); pending.resolve(tutor()); await requested })
    expect(spoken()).toHaveLength(speech)
    expect(vi.mocked(localApi.saveSessionDraft).mock.calls.some(([, section, payload]) => section === 'study' && payload.tutor_request_recovery === null)).toBe(false)
    expect(screen.getByRole('textbox', { name: '输入学习问题' })).toHaveValue(recovery().question)
  })

  it('does not announce or clear the visible recovery cancelled during preserve-draft persistence', async () => {
    restoreStudyQuestion()
    await mount()
    const controller = new AbortController()
    const saved = deferred<{ section: string; payload: Record<string, unknown>; updated_at: string }>()
    vi.mocked(localApi.saveSessionDraft).mockImplementationOnce(() => saved.promise as never)
    const speech = spoken().length
    let requested!: Promise<boolean>
    await act(async () => { requested = requestOwnedVoiceEvent('hsc-study-tutor-recovery', {
      sessionId: 'voice-journey', childId: 'child-a', command: 'keep', signal: controller.signal,
    }, controller.signal).catch(() => false) })
    await act(async () => {
      controller.abort()
      saved.resolve({ section: 'study', payload: {}, updated_at: new Date().toISOString() })
      await requested
    })
    expect(localApi.askTutor).not.toHaveBeenCalled()
    expect(spoken()).toHaveLength(speech)
    expect(screen.getByRole('textbox', { name: '输入学习问题' })).toHaveValue(recovery().question)
    // A later explicit retry still has the original visible request identity.
    await say('重试本题')
    expect(localApi.askTutor).toHaveBeenCalledExactlyOnceWith('voice-journey', '数学', recovery().question, 1, undefined, 'saved-tutor-action', false)
  })

  it('does not release a newer retry when the cancelled old request finally returns', async () => {
    restoreStudyQuestion()
    await mount()
    const old = deferred<TutorTurn>()
    const fresh = deferred<TutorTurn>()
    vi.mocked(localApi.askTutor).mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise)
    const controller = new AbortController()
    let first!: Promise<boolean>
    await act(async () => { first = requestOwnedVoiceEvent('hsc-study-tutor-recovery', {
      sessionId: 'voice-journey', childId: 'child-a', command: 'retry', signal: controller.signal,
    }, controller.signal).catch(() => false) })
    await act(async () => { controller.abort(); await first })
    await say('继续当前任务')
    let second!: Promise<unknown>
    await act(async () => { second = Promise.resolve(microphone.onTranscript('重试本题')) })
    expect(localApi.askTutor).toHaveBeenCalledTimes(2)
    await act(async () => { old.resolve({ ...tutor(), answer: '旧讲解不能播放。' }) })
    await say('重试本题')
    expect(localApi.askTutor).toHaveBeenCalledTimes(2)
    await act(async () => { fresh.resolve({ ...tutor(), answer: '当前讲解仍由新请求持有。' }); await second })
    expect(spoken()).not.toContain('旧讲解不能播放。')
    expect(spoken().filter(text => text === '当前讲解仍由新请求持有。')).toHaveLength(1)
  })

  it('does not begin blackboard playback after recovery is cancelled during the saved-playback lookup', async () => {
    restoreStudyQuestion()
    const read = vi.spyOn(localApi, 'tutorPlayback').mockResolvedValue({ playback: null })
    const mutate = vi.spyOn(localApi, 'mutateTutorPlayback')
    await mount()
    const pending = deferred<{ playback: null }>()
    read.mockReturnValueOnce(pending.promise)
    const taught = tutor()
    taught.output_envelope = { result: { blackboard: { version: 'tutor_blackboard_v1', source: 'model_structured',
      sync_mode: 'segment_estimate', subject: '数学', title: '数量关系', segments: [{ segment_id: 'saved-segment',
        narration: '这段取消后不应开始。', duration_hint_ms: 1000, blocks: [{ kind: 'text', text: '原题', tone: 'chalk' }] }],
    } } } as unknown as TutorTurn['output_envelope']
    vi.mocked(localApi.askTutor).mockResolvedValue(taught)
    const controller = new AbortController()
    let requested!: Promise<boolean>
    await act(async () => { requested = requestOwnedVoiceEvent('hsc-study-tutor-recovery', {
      sessionId: 'voice-journey', childId: 'child-a', command: 'retry', signal: controller.signal,
    }, controller.signal).catch(() => false) })
    expect(read.mock.results.at(-1)?.value).toBe(pending.promise)
    await act(async () => { controller.abort(); pending.resolve({ playback: null }); await requested })
    expect(mutate).not.toHaveBeenCalled()
    expect(spoken()).not.toContain('这段取消后不应开始。')
    expect(vi.mocked(localApi.saveSessionDraft).mock.calls.some(([, section, payload]) => section === 'study' && payload.tutor_request_recovery === null)).toBe(false)
  })

  it('requires explicit spoken confirmation for an unknown tutor outcome, without creating a new action ID', async () => {
    restoreStudyQuestion()
    await mount()
    vi.mocked(localApi.askTutor).mockRejectedValueOnce(new ApiError('结果未知', 409, 'TUTOR_RESULT_UNCONFIRMED'))
    await say('重试本题')
    await say('重试本题')
    expect(localApi.askTutor).toHaveBeenCalledTimes(1)
    expect(spoken().at(-1)).toContain('确认再次请求')
    await say('确认再次请求')
    expect(localApi.askTutor).toHaveBeenLastCalledWith('voice-journey', '数学', recovery().question, 1, undefined, 'saved-tutor-action', true)
    expect(localApi.agentTurn).not.toHaveBeenCalled()
  })

  it.each(['privacy', 'unmount', 'pause'] as const)('does not speak or clear saved work for a tutor response arriving after %s', async (stop) => {
    restoreStudyQuestion()
    const view = await mount()
    const pending = deferred<TutorTurn>()
    vi.mocked(localApi.askTutor).mockReturnValueOnce(pending.promise)
    let speaking!: Promise<void>
    await act(async () => { speaking = Promise.resolve(microphone.onTranscript('重试本题')) })
    expect(localApi.askTutor).toHaveBeenCalledTimes(1)
    if (stop === 'privacy') await say('停止全部监听')
    else if (stop === 'unmount') view.unmount()
    else {
      vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn({ type: 'pause_active_task' }, '已暂停。'))
      await say('暂停学习')
    }
    const count = spoken().length
    await act(async () => { pending.resolve(tutor()); await speaking })
    expect(spoken()).toHaveLength(count)
    expect(vi.mocked(localApi.saveSessionDraft).mock.calls.some(([, section, payload]) => section === 'study' && payload.tutor_request_recovery === null)).toBe(false)
  })

  it.each(['confirm', 'retry', 'cancel'] as const)('ignores an unscoped or foreign %s capture command without altering the original page', async command => {
    const review: ocr.OcrTutorReview = { artifactId: 'saved-page', text: '求这个方程的解。', status: 'needs_confirmation', createdAt: new Date().toISOString() }
    current.drafts.study = { updated_at: new Date().toISOString(), payload: { tutor_ocr_review: review } }
    await mount()
    const confirm = vi.spyOn(ocr, 'confirmOcrTutorEvidence')
    const ask = vi.spyOn(ocr, 'askTutorWithOcrEvidence')
    for (const identity of [{}, { sessionId: 'another-session', childId: 'child-a' }, { sessionId: 'voice-journey', childId: 'child-b' }]) {
      const resolve = vi.fn()
      const event = new CustomEvent(`hsc-${command}-question-capture`, {
        cancelable: true, detail: { ...identity, resolve }
      })
      await act(async () => { window.dispatchEvent(event) })
      expect(event.defaultPrevented).toBe(false)
      expect(resolve).not.toHaveBeenCalled()
      expect(screen.getByLabelText('校对后的题干')).toHaveValue(review.text)
    }
    expect(confirm).not.toHaveBeenCalled()
    expect(ask).not.toHaveBeenCalled()
  })

  it.each(['confirm', 'retry', 'cancel'] as const)('ignores a %s capture command already owned or cancelled', async command => {
    const review: ocr.OcrTutorReview = { artifactId: 'saved-page', text: '求这个方程的解。', status: 'needs_confirmation', createdAt: new Date().toISOString() }
    current.drafts.study = { updated_at: new Date().toISOString(), payload: { tutor_ocr_review: review } }
    await mount()
    const confirm = vi.spyOn(ocr, 'confirmOcrTutorEvidence')
    for (const owned of [true, false]) {
      const controller = new AbortController(), resolve = vi.fn()
      if (!owned) controller.abort()
      const event = new CustomEvent(`hsc-${command}-question-capture`, {
        cancelable: true, detail: { sessionId: 'voice-journey', childId: 'child-a', signal: controller.signal, resolve }
      })
      if (owned) event.preventDefault()
      await act(async () => { window.dispatchEvent(event) })
      expect(resolve).not.toHaveBeenCalled()
      expect(screen.getByLabelText('校对后的题干')).toHaveValue(review.text)
    }
    expect(confirm).not.toHaveBeenCalled()
  })

  it('does not launch an explanation from a capture confirmation cancelled before its receipt', async () => {
    const review: ocr.OcrTutorReview = { artifactId: 'saved-page', text: '求这个方程的解。', status: 'needs_confirmation', createdAt: new Date().toISOString() }
    current.drafts.study = { updated_at: new Date().toISOString(), payload: { tutor_ocr_review: review } }
    await mount()
    const late = deferred<ocr.OcrTutorReview>(), controller = new AbortController()
    const confirm = vi.spyOn(ocr, 'confirmOcrTutorEvidence').mockReturnValueOnce(late.promise)
    const ask = vi.spyOn(ocr, 'askTutorWithOcrEvidence')
    let pending!: Promise<unknown>
    await act(async () => { pending = requestOwnedVoiceEvent('hsc-confirm-question-capture', {
      sessionId: 'voice-journey', childId: 'child-a', signal: controller.signal
    }, controller.signal).catch(error => error) })
    expect(confirm).toHaveBeenCalledOnce()
    await act(async () => { controller.abort() })
    expect(await pending).toMatchObject({ name: 'AbortError' })
    await act(async () => { late.resolve({ ...review, status: 'confirmed', confirmationId: 'late-confirmation' }) })
    expect(ask).not.toHaveBeenCalled()
    expect(screen.getByLabelText('校对后的题干')).toHaveValue(review.text)
    expect(screen.queryByText('题干已确认')).not.toBeInTheDocument()
  })

  it.each(['cancel', 'retry'] as const)('does not let an old confirmation steal a new page explanation after %s', async command => {
    const review: ocr.OcrTutorReview = { artifactId: 'saved-page', text: '求这个方程的解。', status: 'needs_confirmation', createdAt: new Date().toISOString() }
    current.drafts.study = { updated_at: new Date().toISOString(), payload: { tutor_ocr_review: review } }
    await mount()
    const oldReceipt = deferred<ocr.OcrTutorReview>(), newReceipt = deferred<ocr.OcrTutorReview>()
    const confirm = vi.spyOn(ocr, 'confirmOcrTutorEvidence').mockReturnValueOnce(oldReceipt.promise).mockReturnValueOnce(newReceipt.promise)
    const ask = vi.spyOn(ocr, 'askTutorWithOcrEvidence').mockResolvedValue(tutor())
    const scope = { sessionId: 'voice-journey', childId: 'child-a' }
    let first!: Promise<boolean>, second!: Promise<boolean>
    await act(async () => { first = requestOwnedVoiceEvent('hsc-confirm-question-capture', scope) })
    expect(confirm).toHaveBeenCalledTimes(1)
    await act(async () => { expect(await requestOwnedVoiceEvent(`hsc-${command}-question-capture`, scope)).toBe(true) })
    const newReview = { ...review, artifactId: 'new-page', text: '这次求面积。' }
    await act(async () => { window.dispatchEvent(new CustomEvent('hsc-document-review-revision', {
      detail: { sessionId: scope.sessionId, revision: { artifact_id: newReview.artifactId,
        extracted_text: newReview.text, created_at: newReview.createdAt, review_required: false } }
    })) })
    await act(async () => { second = requestOwnedVoiceEvent('hsc-confirm-question-capture', scope) })
    expect(confirm).toHaveBeenCalledTimes(2)
    await act(async () => { oldReceipt.resolve({ ...review, status: 'confirmed', confirmationId: 'old-confirmation' }); expect(await first).toBe(false) })
    expect(ask).not.toHaveBeenCalled()
    await act(async () => { newReceipt.resolve({ ...newReview, status: 'confirmed', confirmationId: 'new-confirmation' }); expect(await second).toBe(true) })
    expect(ask).toHaveBeenCalledExactlyOnceWith('voice-journey', '数学', 'new-confirmation', expect.any(String), 1,
      undefined, expect.any(AbortSignal), false)
    expect(ask.mock.calls[0][6]?.aborted).toBe(false)
  })

  it('keeps a pending confirmation when a duplicate command is rejected', async () => {
    const review: ocr.OcrTutorReview = { artifactId: 'saved-page', text: '求这个方程的解。', status: 'needs_confirmation', createdAt: new Date().toISOString() }
    current.drafts.study = { updated_at: new Date().toISOString(), payload: { tutor_ocr_review: review } }
    await mount()
    const late = deferred<ocr.OcrTutorReview>()
    const confirm = vi.spyOn(ocr, 'confirmOcrTutorEvidence').mockReturnValueOnce(late.promise)
    const ask = vi.spyOn(ocr, 'askTutorWithOcrEvidence').mockResolvedValue(tutor())
    const scope = { sessionId: 'voice-journey', childId: 'child-a' }
    let pending!: Promise<boolean>
    await act(async () => { pending = requestOwnedVoiceEvent('hsc-confirm-question-capture', scope) })
    await act(async () => { expect(await requestOwnedVoiceEvent('hsc-confirm-question-capture', scope)).toBe(false) })
    expect(confirm).toHaveBeenCalledOnce()
    await act(async () => { late.resolve({ ...review, status: 'confirmed', confirmationId: 'owned-confirmation' }); expect(await pending).toBe(true) })
    expect(ask).toHaveBeenCalledOnce()
    expect(ask.mock.calls[0][2]).toBe('owned-confirmation')
  })

  it('waits for slow OCR confirmation and lets the resulting explanation own speech', async () => {
    const review: ocr.OcrTutorReview = { artifactId: 'saved-page', text: '求这个方程的解。', status: 'needs_confirmation', createdAt: new Date().toISOString() }
    current.drafts.study = { updated_at: new Date().toISOString(), payload: { tutor_ocr_review: review } }
    await mount()
    const confirmation = deferred<ocr.OcrTutorReview>()
    const confirm = vi.spyOn(ocr, 'confirmOcrTutorEvidence').mockReturnValueOnce(confirmation.promise)
    const ask = vi.spyOn(ocr, 'askTutorWithOcrEvidence').mockResolvedValue(tutor())
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn({ type: 'confirm_ocr_text' }, '通用确认ACK不应播出'))
    vi.useFakeTimers()
    let command!: Promise<void>
    await act(async () => { command = Promise.resolve(microphone.onTranscript('确认题干')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(300) })
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(ask).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    expect(spoken()).not.toContain('现在还没有可以确认的题干。你可以说“帮我看这题”重新拍题。')
    await act(async () => {
      confirmation.resolve({ ...review, status: 'confirmed', confirmationId: 'saved-confirmation', confirmationKind: 'ocr_verified' })
      await command
    })
    expect(ask).toHaveBeenCalledTimes(1)
    expect(ask.mock.calls[0][2]).toBe('saved-confirmation')
    expect(spoken()).toContain(tutor().answer)
    expect(spoken()).not.toContain('通用确认ACK不应播出')
  })

  it('cannot resurrect OCR confirmation or ask a tutor after a spoken privacy stop', async () => {
    const review: ocr.OcrTutorReview = { artifactId: 'saved-page', text: '求这个方程的解。', status: 'needs_confirmation', createdAt: new Date().toISOString() }
    current.drafts.study = { updated_at: new Date().toISOString(), payload: { tutor_ocr_review: review } }
    await mount()
    const confirmation = deferred<ocr.OcrTutorReview>()
    vi.spyOn(ocr, 'confirmOcrTutorEvidence').mockReturnValueOnce(confirmation.promise)
    const ask = vi.spyOn(ocr, 'askTutorWithOcrEvidence')
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn({ type: 'confirm_ocr_text' }))
    let command!: Promise<void>
    await act(async () => { command = Promise.resolve(microphone.onTranscript('确认题干')) })
    await say('停止全部监听')
    const count = spoken().length
    await act(async () => { confirmation.resolve({ ...review, status: 'confirmed', confirmationId: 'late-confirmation' }); await command })
    expect(ask).not.toHaveBeenCalled()
    expect(spoken()).toHaveLength(count)
  })

  it('keeps the original tutor request when retaining the question cannot be saved, then accepts a spoken retry', async () => {
    restoreStudyQuestion()
    await mount()
    vi.mocked(localApi.saveSessionDraft).mockRejectedValueOnce(new Error('disk unavailable'))
    await say('保留题干')
    expect(spoken()).toContain('这一步没有保存好，原记录仍保留。请再说一次“保留题干”。')
    expect(localApi.askTutor).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    await say('重试本题')
    expect(localApi.askTutor).toHaveBeenCalledWith('voice-journey', '数学', recovery().question, 1, undefined, recovery().actionId, false)
  })

  it.each([
    { type: 'control_mistake_activity', command: 'reload' },
    { type: 'submit_mistake_answer', answer: '四' },
  ] as AgentTurn['action'][])('releases the voice queue without a success ACK when $type has no receiver', async (action) => {
    vi.useFakeTimers()
    current = workspace('break')
    await mount()
    const requested = turn(action, '不应播出的成功 ACK')
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    let pending!: Promise<void>
    await act(async () => { pending = microphone.onTranscript('执行刚才的操作') as Promise<void> })
    expect(localApi.completeAgentTurn).not.toHaveBeenCalledWith(requested.agent_turn_id, expect.anything(), 'completed', expect.anything(), expect.anything())
    await act(async () => { await vi.advanceTimersByTimeAsync(LEGACY_VOICE_EVENT_TIMEOUT_MS); await pending })
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith(requested.agent_turn_id, expect.stringContaining('没有收到处理回执'), 'failed', 'CLIENT_VOICE_RESULT_UNCONFIRMED')
    expect(spoken()).not.toContain('不应播出的成功 ACK')
    await say('今天英语背得很顺利')
    expect(localApi.agentTurn).toHaveBeenLastCalledWith('voice-journey', '今天英语背得很顺利')
  })

  it('rejects an unclaimed mistake-capture action immediately without claiming success', async () => {
    current = workspace('break')
    await mount()
    const requested = turn({ type: 'control_mistake_capture', command: 'save' }, '不应播出的成功 ACK')
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    await say('保存刚才的作答')
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith(requested.agent_turn_id,
      expect.stringContaining('当前没有正在核对的作答记录'), 'failed', 'CLIENT_VOICE_ACTION_UNAVAILABLE')
    expect(spoken()).not.toContain('不应播出的成功 ACK')
    await say('今天英语背得很顺利')
    expect(localApi.agentTurn).toHaveBeenLastCalledWith('voice-journey', '今天英语背得很顺利')
  })

  it.each(['child', 'session'])('does not reopen a persisted mistake belonging to another %s', async (wrong) => {
    vi.spyOn(localApi, 'mistakeDialogue').mockResolvedValue({ dialogue: {
      session_id: wrong === 'session' ? 'other-session' : current.session.session_id,
      child_id: wrong === 'child' ? 'other-child' : current.session.child_id,
      candidate_id: 'unrelated-candidate', mistake_id: null, phase: 'confirm', response_text: '另一孩子的问题',
      state: { artifact_id: 'unrelated-page', question_text: '另一孩子的问题' },
    } })
    vi.spyOn(localApi, 'mistakeCaptureQueues').mockResolvedValue([])
    const begin = vi.spyOn(localApi, 'beginMistakeDialogue')
    await mount()
    expect(begin).not.toHaveBeenCalled()
    expect(screen.queryByText('另一孩子的问题')).not.toBeInTheDocument()
  })

  it('retries the actual queued paper dialogue by voice and waits for its verified readback', async () => {
    const focus: MistakeDialogue = { session_id: 'voice-journey', child_id: 'child-a', candidate_id: 'candidate-a',
      mistake_id: null, phase: 'answer', response_text: '这一题你当时写了什么？',
      state: { artifact_id: 'paper-a', question_text: '求这道题的结果。' } }
    let latest: MistakeDialogue | null = null
    const read = deferred<{ dialogue: MistakeDialogue | null }>()
    vi.spyOn(localApi, 'mistakeDialogue').mockImplementation(async () => latest ? read.promise : { dialogue: null })
    vi.spyOn(localApi, 'mistakeCaptureQueues').mockResolvedValue([{
      artifact_id: 'paper-a', extracted_text: focus.state.question_text!, confidence: .9,
      local_path: null, status: 'completed', pending_count: 1, candidate_count: 1, details: { lines: [], engine: 'synthetic' },
    }])
    const begin = vi.spyOn(localApi, 'beginMistakeDialogue').mockRejectedValueOnce(new Error('offline'))
      .mockImplementation(async () => { latest = focus; return focus })
    await mount()
    await screen.findByText('题目还没核对成功。请说“重试整理错题”，我会从保存的位置继续。', { selector: '[role="alert"]' })
    const requested = turn({ type: 'control_mistake_capture', command: 'open' }, '重复的预制确认')
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    let pending!: Promise<void>
    await act(async () => { pending = Promise.resolve(microphone.onTranscript('重试整理错题')) })
    await waitFor(() => expect(begin).toHaveBeenCalledTimes(2))
    expect(localApi.completeAgentTurn).not.toHaveBeenCalledWith(requested.agent_turn_id, expect.anything(), 'completed', expect.anything(), expect.anything())
    await act(async () => { read.resolve({ dialogue: focus }); await pending })
    expect(begin.mock.calls[0]).toEqual(begin.mock.calls[1])
    expect(screen.getByText(focus.state.question_text!)).toBeInTheDocument()
    expect(spoken()).toContain(focus.response_text)
    expect(spoken()).not.toContain('重复的预制确认')
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith(requested.agent_turn_id, focus.response_text, 'completed')
  })

  it('immediately releases an unclaimed document request instead of waiting for the legacy deadline', async () => {
    current = workspace('break')
    await mount()
    const requested = turn({ type: 'control_document_monitor', command: 'inspect' }, '不应播出的成功 ACK')
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
    await say('检查当前页')
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith(requested.agent_turn_id, expect.stringContaining('这次页面检查没有完成'), 'failed', 'CLIENT_VOICE_ACTION_UNAVAILABLE')
    expect(spoken()).not.toContain('不应播出的成功 ACK')
  })

  it('explains the check-in preview without suggesting a disconnected document camera', async () => {
    current = { ...workspace('check_in'), plan: null, plan_items: [] }
    await mount()
    const listener = (event: Event): void => {
      event.preventDefault()
      const { resolve } = (event as CustomEvent<{ resolve: (handled: boolean) => void }>).detail
      resolve(false)
    }
    window.addEventListener('hsc-document-monitor-voice-action', listener, { once: true })
    try {
      const requested = turn({ type: 'control_document_monitor', command: 'inspect' }, '不应播出的成功 ACK')
      vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
      const command = screen.getByRole('textbox', { name: '对学习伙伴说' })
      fireEvent.change(command, { target: { value: '检查当前页' } })
      fireEvent.keyDown(command, { key: 'Enter' })
      const guidance = '签到时，高拍仪只显示预览。进入作业登记或开始学习后，再说“检查当前页”。'
      await waitFor(() => expect(localApi.completeAgentTurn).toHaveBeenCalledWith(requested.agent_turn_id, guidance, 'failed', 'CLIENT_VOICE_ACTION_UNAVAILABLE'))
      expect(spoken()).toContain(guidance)
      expect(spoken().join(' ')).not.toMatch(/检查高拍仪连接|不应播出的成功 ACK/)
    } finally { window.removeEventListener('hsc-document-monitor-voice-action', listener) }
  })

  it('keeps a claimed page operation pending past twenty seconds and uses only its final source receipt', async () => {
    vi.useFakeTimers()
    current = workspace('break')
    await mount()
    let received!: { resolve: (handled: boolean) => void; report: (output: unknown) => void; signal: AbortSignal }
    const listener = (event: Event) => { event.preventDefault(); received = (event as CustomEvent).detail }
    window.addEventListener('hsc-document-monitor-voice-action', listener)
    try {
      const requested = turn({ type: 'control_document_monitor', command: 'inspect', capability: 'document.monitor_control', execution_location: 'client_device_bridge' }, '不得提前播报')
      vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
      let pending!: Promise<void>
      await act(async () => { pending = microphone.onTranscript('检查当前页') as Promise<void> })
      expect(received).toEqual(expect.objectContaining({ sessionId: current.session.session_id }))
      await act(async () => { await vi.advanceTimersByTimeAsync(45_000) })
      expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
      expect(received.signal.aborted).toBe(false)
      const output = { artifact_id: 'slow-page', revision_id: 'slow-version', page_status: 'text_available', response_text: '这一页已取得，文字仍需要核对。' }
      await act(async () => { received.report(output); received.resolve(true); await pending })
      expect(localApi.completeAgentTurn).toHaveBeenCalledWith(requested.agent_turn_id, output.response_text, 'completed', undefined, expect.objectContaining({
        execution_receipt: expect.objectContaining({ output })
      }))
      expect(spoken()).not.toContain('不得提前播报')
    } finally { window.removeEventListener('hsc-document-monitor-voice-action', listener) }
  })

  it('waits for immediate recovery persistence before invoking Tutor and retries storage failure by voice', async () => {
    vi.useFakeTimers()
    await mount()
    const save = deferred<Awaited<ReturnType<typeof localApi.saveSessionDraft>>>()
    vi.mocked(localApi.saveSessionDraft).mockImplementationOnce(() => save.promise)
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn({ type: 'ask_tutor', question: '为什么要先列方程？', subject: '数学' }))
    let pending!: Promise<void>
    await act(async () => { pending = microphone.onTranscript('讲解这题') as Promise<void> })
    expect(localApi.saveSessionDraft).toHaveBeenCalledWith('voice-journey', 'agent_command', expect.objectContaining({ tutor_recovery: expect.objectContaining({ question: '为什么要先列方程？', requiresExplicitRetryConfirmation: true }) }))
    expect(localApi.askTutor).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(600) })
    expect(localApi.saveSessionDraft).toHaveBeenCalledTimes(1)
    await act(async () => {
      save.resolve({ section: 'agent_command', payload: {}, updated_at: new Date().toISOString() })
      await pending
    })
    expect(localApi.askTutor).toHaveBeenCalledTimes(1)
    vi.mocked(localApi.askTutor).mockClear()
    vi.mocked(localApi.saveSessionDraft).mockRejectedValue(new Error('disk offline'))
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn({ type: 'ask_tutor', question: '为什么移项？', subject: '数学' }))
    await say('解释移项')
    expect(localApi.askTutor).not.toHaveBeenCalled()
    expect(spoken().some((text) => text.includes('这次没有请求讲解'))).toBe(true)
    vi.mocked(localApi.saveSessionDraft).mockImplementation(async (_id, section, payload) => ({ section, payload, updated_at: new Date().toISOString() }))
    await say('重试讲解')
    expect(localApi.askTutor).toHaveBeenCalledTimes(1)
  })

  it('ignores a legacy receiver arriving after timeout and does not replace the recovery prompt with a success ACK', async () => {
    vi.useFakeTimers()
    current = workspace('break')
    await mount()
    let finish!: (handled: boolean) => void
    const listener = (event: Event) => { finish = (event as CustomEvent).detail.resolve }
    window.addEventListener('hsc-mistake-recovery-voice-action', listener)
    try {
      const requested = turn({ type: 'control_mistake_activity', command: 'reload' }, '迟到成功 ACK')
      vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
      let pending!: Promise<void>
      await act(async () => { pending = microphone.onTranscript('查看当前页面') as Promise<void> })
      await act(async () => { await vi.advanceTimersByTimeAsync(LEGACY_VOICE_EVENT_TIMEOUT_MS); await pending })
      const completed = vi.mocked(localApi.completeAgentTurn).mock.calls.length
      const speech = spoken().length
      await act(async () => { finish(true) })
      expect(localApi.completeAgentTurn).toHaveBeenCalledTimes(completed)
      expect(spoken()).toHaveLength(speech)
      expect(spoken()).not.toContain('迟到成功 ACK')
    } finally { window.removeEventListener('hsc-mistake-recovery-voice-action', listener) }
  })

  it('does not mark an explicitly declined legacy action as completed', async () => {
    current = workspace('break')
    await mount()
    const listener = (event: Event) => { (event as CustomEvent).detail.resolve(false) }
    window.addEventListener('hsc-document-monitor-voice-action', listener)
    try {
      const requested = turn({ type: 'control_document_monitor', command: 'inspect' }, '不应播报的成功确认')
      vi.mocked(localApi.agentTurn).mockResolvedValueOnce(requested)
      await say('查看当前页面')
      expect(localApi.completeAgentTurn).toHaveBeenCalledWith(requested.agent_turn_id, expect.stringContaining('这次页面检查没有完成'), 'failed', 'CLIENT_VOICE_ACTION_UNAVAILABLE')
      expect(spoken()).not.toContain('不应播报的成功确认')
      expect(spoken().some(text => text.includes('监控目前没有在当前任务中运行'))).toBe(false)
      await say('今天英语背得很顺利')
      expect(localApi.agentTurn).toHaveBeenLastCalledWith('voice-journey', '今天英语背得很顺利')
    } finally { window.removeEventListener('hsc-document-monitor-voice-action', listener) }
  })

  it('persists and restores local course evidence without sending it to cloud Tutor or cloud TTS', async () => {
    let playback: import('./types').TutorPlaybackCheckpoint | null = null
    vi.spyOn(localApi, 'tutorPlayback').mockImplementation(async () => ({ playback }))
    vi.spyOn(localApi, 'mutateTutorPlayback').mockImplementation(async (sessionId, operation, values) => {
      if (operation === 'begin') playback = {
        playback_id: 'course-playback', turn_id: String(values.turn_id), session_id: sessionId, child_id: 'child-a',
        source_version: 'course-source', presentation_version: 'course-presentation', generation: 1, revision: 1,
        status: 'paused', segment_index: 0, segment_started: false, reason: null, replayed: false, evidence_kind: 'playback_only',
        presentation: values.presentation as import('./types').TutorBlackboardPresentation,
      }
      else {
        if (!playback || playback.revision !== values.expected_revision || playback.generation !== values.generation) throw new Error('PLAYBACK_BINDING')
        playback = { ...playback, revision: playback.revision + 1 }
        if (operation === 'control') playback = { ...playback, generation: playback.generation + 1, segment_started: false,
          status: values.command === 'resume' ? 'playing' : values.command === 'cancel' ? 'cancelled' : 'paused' }
        else playback = { ...playback, segment_started: values.event === 'started',
          segment_index: playback.segment_index + Number(values.event === 'ended') }
      }
      return { playback: playback! }
    })
    const course: LocalCourseRecoveryContext = {
      status: 'matched', cloud_transmission_allowed: false, mastery_update_allowed: false,
      diagnostic_prompt: '请比较本地课程和今天这道题的数量关系。',
      evidence: [{ segment_id: 'segment-local-a', resource_id: 'resource-local-a', title: '本地课本原文', subject: '数学', grade: '初三', page_number: 12, source_ref: 'local:lesson-a', excerpt: '未经审核的课程原文只保留在本机。', excerpt_start_char: 0, review_status: 'unreviewed', usage: 'source_comparison_not_verified_curriculum_alignment' }],
    }
    const action: AgentTurn['action'] = { type: 'ask_tutor', question: recovery().question, local_learning_context: {
      status: 'matched', cloud_transmission_allowed: false, mastery_update_allowed: false,
      audit_event_id: null, matched_node_count: 0, approved_node_count: 0, hypothesis_count: 0, diagnostic_prompt: null, course_context: course,
    } }
    vi.useFakeTimers()
    const first = await mount()
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce(turn(action))
    vi.mocked(localApi.askTutor).mockRejectedValueOnce(new ApiError('本机服务中断', 503, 'LOCAL_SERVICE_OFFLINE'))
    await say(recovery().question)
    // The failed request's recovery is durable without advancing the debounce.
    const payload = vi.mocked(localApi.saveSessionDraft).mock.calls.filter(([, section, saved]) => section === 'agent_command' && saved.tutor_recovery).at(-1)?.[2]
    expect(payload).toBeTruthy()
    expect((payload!.tutor_recovery as { courseContext: unknown }).courseContext).toEqual(course)
    first.unmount()
    current = workspace()
    current.drafts.agent_command = { updated_at: new Date().toISOString(), payload: JSON.parse(JSON.stringify(payload)) }
    const taught = tutor()
    taught.output_envelope = { result: { blackboard: { version: 'tutor_blackboard_v1', title: '数量关系', segments: [] } } } as unknown as TutorTurn['output_envelope']
    vi.mocked(localApi.askTutor).mockResolvedValue(taught)
    vi.mocked(localApi.completeAgentTurn).mockImplementation(acknowledgeTurn)
    await mount()
    await say('重试讲解')
    expect(screen.getByText(course.evidence[0].excerpt)).toBeTruthy()
    expect(JSON.stringify(vi.mocked(localApi.askTutor).mock.calls)).not.toContain(course.evidence[0].excerpt)
    expect(JSON.stringify(vi.mocked(localApi.askTutor).mock.calls)).not.toContain(course.diagnostic_prompt)
    const narration = vi.mocked(LocalInputs.speakLocally).mock.calls.find(([text]) => text.includes('这里是本机课程中的相关片段'))
    expect(narration?.[4]).toMatchObject({ localOnly: true })
    // Text-only recovery reveals the current evidence, not future segments.
    expect(spoken()).not.toContain(course.diagnostic_prompt)
    expect(playback).toMatchObject({ segment_index: 0 })
  })
})
