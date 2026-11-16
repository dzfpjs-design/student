import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import { DocumentCameraCapture } from './DocumentCameraCapture'
import * as inputs from './LocalInputs'
import { emitCaptureReceipt } from './deviceCommandReceipt'
import { clearVoiceCaptureContext, publishOriginalAudioCaptureHint, setVoiceQuestionCapture, voiceCaptureReference } from './voiceCaptureContext'
import * as deviceConnection from './deviceConnectionRuntime'
import { CAMERA_DEVICE_COMMAND, CAMERA_DEVICE_STATUS } from './cameraDeviceRuntime'
import { TutorPlaybackController } from './tutorPlaybackController'
import type { AgentTurn, DiagnosticPromptResult, MistakeDialogue, OcrArtifact, SessionWorkspaceState } from './types'

let microphone: ComponentProps<typeof inputs.HandsFreeVoiceInput>
let capture: ComponentProps<typeof DocumentCameraCapture>
vi.mock('./LocalInputs', async (original) => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null }, VoiceInputButton: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: (props: typeof capture) => { capture = props; return null } }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))
const initial = { session: { session_id: 'synthetic-device-session', child_id: 'synthetic-child', status: 'active', state: 'check_in', observation_status: 'paused' },
  plan_items: [], recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [], drafts: {}, planning_context: {}, family_rule: {}, sensor_release: { active_leases: 0, by_device: {} } } as unknown as SessionWorkspaceState

describe('voice device and missing receiver execution contracts', () => {
  beforeEach(() => {
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
      getWakeWordSettings: vi.fn(async () => ({ childId: 'synthetic-child', wakeWord: '小知', enabled: true, inactivitySeconds: 60 })),
      getVoiceOnboarding: vi.fn(async () => ({ childId: 'synthetic-child', version: 1, step: 'completed', updatedAt: new Date().toISOString() })),
      saveVoiceOnboarding: vi.fn(), saveWakeWordSettings: vi.fn(),
    }))
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
    for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
      vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
        handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
    }
    vi.spyOn(inputs, 'speakLocally').mockImplementation((_text, _session, done) => done?.('text_only'))
    vi.spyOn(localApi, 'agentTurn')
    vi.spyOn(localApi, 'admitOriginalClientExecution').mockResolvedValue({status:'admitted'})
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'askTutor')
    vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(initial)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
    vi.spyOn(localApi, 'taskStartChoice').mockResolvedValue({ session_id: initial.session.session_id,
      child_id: initial.session.child_id, reference: null })
  })
  afterEach(() => { cleanup(); clearVoiceCaptureContext(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
  const mount = async () => { await act(async () => { render(<SessionWorkspace initial={initial} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />) }) }
  const mountForLegacyDeviceRequest = async () => {
    await mount()
    const { session_id, child_id } = initial.session
    const binding = { prompt_id: 'original-audio-test', item_version: 'synthetic-v1' }
    await act(async () => {
      setVoiceQuestionCapture(session_id, child_id, { kind: 'diagnostic', binding })
      publishOriginalAudioCaptureHint(session_id, { status: 'waiting_answer', prompt: {
        ...binding, origin_session_id: session_id, child_id,
        item: { track: 'pet', assessment_mode: 'pet_reference_rubric', answer_type: 'read_aloud' },
      } } as DiagnosticPromptResult)
    })
  }
  const issue = async (action: AgentTurn['action'], text = '请帮我查看当前状态') => {
    vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'device-turn', response_text: '规划过渡句', action } as AgentTurn)
    await act(async () => { await microphone.onTranscript(text) })
  }

  it('routes original-mistake recapture to mistake dialogue without relying on prior button or phrase', async () => {
    const study = { ...initial, session: { ...initial.session, state: 'study' } } as SessionWorkspaceState
    vi.mocked(localApi.sessionWorkspace).mockResolvedValue(study)
    let requested: { forMistake?: boolean; accept?: () => void } | undefined
    const receive = (event: Event) => {
      requested = (event as CustomEvent).detail
      requested?.accept?.()
    }
    window.addEventListener('hsc-request-question-capture', receive)
    try {
      await act(async () => { render(<SessionWorkspace initial={study} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />) })
      await issue({ type: 'retry_capture', capability: 'document.retry_capture', execution_location: 'client_device_bridge',
        mistake_recapture_target: { mistake_id: 'original-mistake', source_version: 'frozen-source' }, local_only: true }, '再看清楚一点')
      expect(requested?.forMistake).toBe(true)
      expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    } finally { window.removeEventListener('hsc-request-question-capture', receive) }
  })

  it.each(['task_start_offer', 'task_source_gap'] as const)('binds the spoken recovery reference after execution (%s)', async kind => {
    const reference = { offer_turn_id: 'offer-turn', source_version: 'list-v1' }
    const turn = { agent_turn_id: 'offer-turn', response_text: '第1项，练习甲；第2项，练习乙。你想做哪项？',
      runtime_execution_location: 'server', runtime_capability: 'conversation.respond',
      action: { type: 'none', source: 'task_navigation_clarification', local_only: true,
        cloud_transmission_allowed: false, [kind]: { version: 'synthetic-offer' } } } as unknown as AgentTurn
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn,
      execution: { capability: 'conversation.respond', response_text: turn.response_text, evidence: [], result: {} } })
    vi.mocked(localApi.taskStartChoice).mockResolvedValue({ session_id: initial.session.session_id,
      child_id: initial.session.child_id, reference })
    await mount()
    vi.mocked(localApi.agentTurn).mockResolvedValue(turn)
    await act(async () => { await microphone.onTranscript('开始数学作业') })
    expect(voiceCaptureReference(initial.session.session_id, initial.session.child_id).task_choice_reference).toEqual(reference)
    expect(inputs.speakLocally).toHaveBeenLastCalledWith(turn.response_text, initial.session.session_id,
      expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  })

  it('restores a saved task-list reference on study startup without requiring a button', async () => {
    const study = { ...initial, session: { ...initial.session, state: 'study' } } as SessionWorkspaceState
    const reference = { offer_turn_id: 'saved-offer', source_version: 'saved-list' }
    vi.mocked(localApi.sessionWorkspace).mockResolvedValue(study)
    vi.mocked(localApi.taskStartChoice).mockResolvedValue({ session_id: study.session.session_id,
      child_id: study.session.child_id, reference })
    await act(async () => { render(<SessionWorkspace initial={study} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />) })
    expect(voiceCaptureReference(study.session.session_id, study.session.child_id).task_choice_reference).toEqual(reference)
  })

  it.each([
    ['missing', 'task_start_offer'], ['other-child', 'task_start_offer'], ['network', 'task_start_offer'],
    ['other-offer', 'task_start_offer'], ['missing', 'task_source_gap'], ['other-child', 'task_source_gap'],
    ['network', 'task_source_gap'], ['other-offer', 'task_source_gap'],
  ] as const)('does not speak an unverified offer (%s, %s)', async (mode, kind) => {
    const turn = { agent_turn_id: 'unverified-offer', response_text: '第1项旧任务；第2项旧任务。',
      runtime_execution_location: 'server', runtime_capability: 'conversation.respond',
      action: { type: 'none', source: 'task_navigation_clarification', local_only: true,
        [kind]: { version: 'synthetic-offer' } } } as unknown as AgentTurn
    const execute = vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn,
      execution: { capability: 'conversation.respond', response_text: turn.response_text, evidence: [], result: {} } })
    if (mode === 'network') vi.mocked(localApi.taskStartChoice).mockRejectedValue(new Error('offer unavailable'))
    else vi.mocked(localApi.taskStartChoice).mockResolvedValue({ session_id: initial.session.session_id,
      child_id: mode === 'other-child' ? 'another-child' : initial.session.child_id,
      reference: mode === 'missing' ? null : { offer_turn_id: 'other-offer', source_version: 'other-version' } })
    await mount()
    vi.mocked(localApi.agentTurn).mockResolvedValue(turn)
    await act(async () => { await microphone.onTranscript('开始作业') })
    expect(voiceCaptureReference(initial.session.session_id, initial.session.child_id).task_choice_reference).toBeUndefined()
    expect(vi.mocked(inputs.speakLocally).mock.calls.some(call => call[0] === turn.response_text)).toBe(false)
    expect(execute).toHaveBeenCalledTimes(1)
  })

  it.each(['current', 'missing', 'stale', 'other-child', 'network'] as const)('binds an unavailable listening original only to its completed current receipt (%s)', async mode => {
    const reference = { offer_turn_id: 'listening-start', source_version: 'original-gap' }
    const turn = { agent_turn_id: 'listening-start', response_text: '开始原听力题',
      runtime_execution_location: 'server', runtime_capability: 'task.start',
      action: { type: 'start_next_task', capability: 'task.start', execution_location: 'server' } } as AgentTurn
    const spoken = '原音还未准备好，可以把这项延期并保留进度。'
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({ turn: { ...turn, response_text: spoken }, execution: {
      capability: 'task.start', response_text: spoken, evidence: [], result: {
        status: 'awaiting_resources', reason: 'listening_material_missing', plan_item_id: 'original-pet',
        task_resource_gap: { version: 'task_listening_source_gap_v1' } } } })
    if (mode === 'network') vi.mocked(localApi.taskStartChoice).mockRejectedValue(new Error('offer unavailable'))
    else vi.mocked(localApi.taskStartChoice).mockResolvedValue({ session_id: initial.session.session_id,
      child_id: mode === 'other-child' ? 'another-child' : initial.session.child_id,
      reference: mode === 'missing' ? null : mode === 'stale' ? { ...reference, offer_turn_id: 'old-turn' } : reference })
    await mount()
    vi.mocked(localApi.agentTurn).mockResolvedValue(turn)
    await act(async () => { await microphone.onTranscript('开始下一项') })
    const bound = voiceCaptureReference(initial.session.session_id, initial.session.child_id).task_choice_reference
    if (mode === 'current') expect(bound).toEqual(reference)
    else expect(bound).toBeUndefined()
    expect(vi.mocked(inputs.speakLocally).mock.calls.some(call => call[0] === spoken)).toBe(mode === 'current')
  })

  it.each(['文字可读，当前页是数学小数练习', ''])('captures an observation-only page directly in check-in without entering Tutor or mistake storage (%s)', async (text) => {
    await mount()
    await issue({ type: 'request_capture', observation_only: true, local_only: true, capability: 'learning.capture_question', execution_location: 'client_device_bridge' }, '你能看到我在写什么作业吗')
    expect(capture.automation).toBe('capture_and_recognize')
    expect(capture.sessionId).toBe(initial.session.session_id)
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    const artifact = { artifact_id: 'page-artifact', extracted_text: text, confidence: 0.9, status: 'completed', details: { local_pipeline: { wrong_question_candidates: [{ text: '错误标记不应自动录入' }] } } } as unknown as OcrArtifact
    await act(async () => {
      capture.onArtifact?.(artifact)
      emitCaptureReceipt({ sessionId: capture.sessionId, commandId: capture.commandId!, status: text ? 'recognized' : 'failed', artifactId: artifact.artifact_id,
        ocrStatus: text ? 'completed' : 'failed', ...(text ? {} : { errorCode: 'OCR_EMPTY_TEXT' }) })
    })
    expect(localApi.askTutor).not.toHaveBeenCalled()
    expect(screen.queryByText('高拍仪已联动')).toBeNull()
    expect(inputs.speakLocally).toHaveBeenLastCalledWith(expect.stringContaining(text || '没有识别到可靠文字'), initial.session.session_id, expect.any(Function), expect.any(Function),
      expect.objectContaining({ localOnly: true, isCurrent: expect.any(Function), onPlaybackEnd: expect.any(Function), onNotDelivered: expect.any(Function) }))
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith('device-turn', expect.any(String), text ? 'completed' : 'failed', text ? undefined : 'OCR_EMPTY_TEXT', expect.any(Object))
  })

  it.each(['record_tutor_feedback', 'more_tutor_hint', 'update_reflection', 'complete_reflection', 'review_pending_recall'])('returns failed rather than false completion for absent %s receiver', async (type) => {
    await mount(); await issue({ type, decision: 'confirm' })
    expect(localApi.completeAgentTurn).toHaveBeenCalledExactlyOnceWith('device-turn', expect.any(String), 'failed', 'CLIENT_VOICE_ACTION_UNAVAILABLE')
  })

  it.each(['start_diagnostic', 'replay_diagnostic'])('uses the phase-independent %s executor and reports real service failure', async type => {
    const load = vi.spyOn(localApi, 'diagnosticPrompt').mockRejectedValue(new ApiError('Service unavailable', 503, 'LOCAL_SERVICE_OFFLINE'))
    await mount(); await issue({ type })
    expect(load).toHaveBeenCalledWith(initial.session.session_id, undefined)
    expect(localApi.completeAgentTurn).toHaveBeenCalledExactlyOnceWith('device-turn', expect.any(String), 'failed', 'LOCAL_SERVICE_OFFLINE')
  })

  it('explains missing original question binding and never submits to a later question', async () => {
    const answer = vi.spyOn(localApi, 'answerDiagnosticPrompt')
    await mount(); await issue({ type: 'submit_diagnostic_answer', answer: '90' })
    expect(answer).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).toHaveBeenCalledExactlyOnceWith('device-turn', expect.stringContaining('没有绑定待答题'), 'failed', 'CLIENT_VOICE_ACTION_UNAVAILABLE')
  })

  it('queries current renderer state without starting a capture or asserting physical power', async () => {
    await mountForLegacyDeviceRequest(); await issue({ type: 'query_device_status', device: 'all', capability: 'device.status', execution_location: 'client_device_bridge' })
    const receipt = vi.mocked(localApi.completeAgentTurn).mock.calls[0][4]?.client_bridge_receipt as { output: { devices: Array<{ track_live: boolean; frame_confirmed: boolean }> } }
    expect(receipt.output.devices).toEqual([expect.objectContaining({ device_type: 'document_camera' })])
    expect(receipt.output.devices.every((item) => !item.track_live && !item.frame_confirmed)).toBe(true)
    expect(screen.getByText(/物理电源状态无法判断/)).toBeTruthy()
  })

  it('completes an old all-devices request using the document camera bridge receipt alone', async () => {
    vi.spyOn(deviceConnection, 'connectRequestedVideoDevices').mockResolvedValue({
      requested_device: 'all', connected_at: new Date().toISOString(), devices: [
        { status: 'connected', device_type: 'document_camera', device_status: 'active', frame_confirmed: true,
          lease_confirmed: true, persisted_status: 'active' },
      ],
    })
    await mountForLegacyDeviceRequest()
    await issue({ type: 'connect_devices', device: 'all', capability: 'device.connect', execution_location: 'client_device_bridge' }, '连接摄像头和高拍仪')
    expect(deviceConnection.connectRequestedVideoDevices).toHaveBeenCalledWith(
      initial.session.session_id, 'all', expect.any(AbortSignal),
    )
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith('device-turn', expect.stringContaining('高拍仪已经连接并收到画面'),
      'completed', undefined, expect.objectContaining({ execution_receipt: expect.objectContaining({
        capability: 'device.connect', action_type: 'connect_devices', status: 'completed',
      }) }))
    expect(inputs.speakLocally).toHaveBeenLastCalledWith(expect.stringContaining(deviceConnection.OBSERVATION_CAMERA_REMOVED_MESSAGE),
      initial.session.session_id, expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
  })

  it('reports a missing bound document camera without suggesting that the observation camera could be repaired', async () => {
    vi.spyOn(deviceConnection, 'connectRequestedVideoDevices').mockResolvedValue({
      requested_device: 'all', connected_at: new Date().toISOString(), devices: [
        { status: 'failed', device_type: 'document_camera', error_code: 'BOUND_DEVICE_NOT_FOUND' },
      ],
    })
    await mountForLegacyDeviceRequest()
    await issue({ type: 'connect_devices', device: 'all', capability: 'device.connect', execution_location: 'client_device_bridge' }, '连接摄像头和高拍仪')
    const completed = vi.mocked(localApi.completeAgentTurn).mock.calls[0]
    expect(completed.slice(0, 4)).toEqual(['device-turn', expect.stringContaining('请把高拍仪的 USB 插头拔下再插紧'),
      'failed', 'BOUND_DEVICE_NOT_FOUND'])
    expect(completed[4]).toEqual(expect.objectContaining({ device_connection_attempt: expect.objectContaining({ requested_device: 'all' }) }))
    expect(completed[1]).toContain(deviceConnection.OBSERVATION_CAMERA_REMOVED_MESSAGE)
    expect(completed[1]).not.toContain('请把观察摄像头')
  })

  it.each([
    ['query_device_status', 'device.status', 'completed', undefined],
    ['connect_devices', 'device.connect', 'failed', deviceConnection.OBSERVATION_CAMERA_REMOVED],
    ['pause_observation', 'observation.pause', 'completed', undefined],
    ['resume_observation', 'observation.resume', 'failed', 'CLIENT_VOICE_ACTION_UNAVAILABLE'],
  ] as const)('answers an obsolete camera action without querying, opening, or resuming it (%s)', async (type, capability, status, errorCode) => {
    const dispatched = vi.fn()
    const bindings = vi.spyOn(localApi, 'deviceBindings')
    window.addEventListener(CAMERA_DEVICE_COMMAND, dispatched)
    window.addEventListener(CAMERA_DEVICE_STATUS, dispatched)
    try {
      await mountForLegacyDeviceRequest()
      bindings.mockClear()
      await issue({ type, device: 'camera', capability, execution_location: 'client_device_bridge' }, '打开观察摄像头')
      expect(vi.mocked(localApi.completeAgentTurn).mock.calls[0].slice(0, 4)).toEqual([
        'device-turn', deviceConnection.OBSERVATION_CAMERA_REMOVED_MESSAGE, status, errorCode,
      ])
      expect(dispatched).not.toHaveBeenCalled()
      expect(bindings).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener(CAMERA_DEVICE_COMMAND, dispatched)
      window.removeEventListener(CAMERA_DEVICE_STATUS, dispatched)
    }
  })

  it.each(['fresh', 'replay', 'withdrawn'])('continues a spoken answer with local oral feedback, without buttons (%s)', async mode => {
    const owner={child_id:initial.session.child_id,session_id:initial.session.session_id}
    const prompt={prompt_id:'oral-prompt',item_version:'oral-v1',child_id:owner.child_id,origin_session_id:owner.session_id,
      diagnostic_item_id:'oral-item',item:{track:'school',answer_type:'open_response',response_mode:'spoken_explanation'}}
    vi.spyOn(localApi,'diagnosticPrompt').mockResolvedValue({status:'waiting_answer',prompt} as DiagnosticPromptResult)
    const saved=vi.spyOn(localApi,'answerDiagnosticPrompt').mockResolvedValue({status:'answered',prompt,
      attempt:{attempt_id:'oral-attempt',feedback:'回答已保存'},local_learning_evaluation:{status:'local_feedback_required',
        feedback_turn_id:'not-authorized-cloud',local_feedback_turn_id:'local-oral-reference',local_only:true,cloud_transmission_allowed:false},
    } as DiagnosticPromptResult)
    const response='先对照原题要求，解释两种方法为什么相同。'
    const execute=vi.spyOn(localApi,'executeAgentTurn').mockResolvedValue({
      turn:{...owner,agent_turn_id:'local-oral-reference',action:{type:'none'},after_execution:'stop'} as AgentTurn,
      execution:{capability:'conversation.respond',response_text:response,evidence:[],
        ...(mode==='replay'?{replayed:true,replay_audio_allowed:false}:{}),
        result:{feedback_delivery_allowed:mode!=='withdrawn',feedback_status:mode==='withdrawn'?'blocked':'ready',
          objective_update_applied:false,local_only:true,cloud_transmission_allowed:false,
          oral_feedback:{version:'oral_teaching_v2',status:'content_self_check',prompt_id:prompt.prompt_id,
            item_version:prompt.item_version,feedback_version:'f1',source_version:'s1',
            local_only:true,cloud_transmission_allowed:false,mastery_claim_allowed:false}}}})
    await mount()
    await issue({type:'submit_diagnostic_answer',answer:'因为两种方法用了同一个数量关系',
      diagnostic_prompt_id:prompt.prompt_id,diagnostic_item_version:prompt.item_version},'因为两种方法用了同一个数量关系')
    expect(saved).toHaveBeenCalledExactlyOnceWith(owner.session_id,expect.objectContaining({
      prompt_id:prompt.prompt_id,item_version:prompt.item_version,authorized_turn_id:'device-turn'}))
    expect(execute).toHaveBeenCalledExactlyOnceWith('local-oral-reference')
    const spoken=vi.mocked(inputs.speakLocally).mock.calls.map(([text])=>text)
    if(mode==='fresh') {
      expect(screen.getByText(response)).toBeTruthy()
      expect(inputs.speakLocally).toHaveBeenLastCalledWith(response,owner.session_id,expect.any(Function),expect.any(Function),
        expect.objectContaining({localOnly:true,sourceAgentTurnId:'local-oral-reference'}))
    } else expect(spoken).not.toContain(response)
    expect(localApi.askTutor).not.toHaveBeenCalled()
  })

  it('preserves the admitted independent-question subject and node through the workspace executor', async () => {
    const prompt = { prompt_id: 'new-source-prompt', item_version: 'v1', child_id: initial.session.child_id,
      origin_session_id: initial.session.session_id, diagnostic_item_id: 'new-item',
      item: { track: 'school', subject: '数学', prompt: '求两个根', answer_type: 'closed' } }
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({ status: 'answered' } as DiagnosticPromptResult)
    const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt').mockResolvedValue({ status: 'waiting_answer', prompt } as DiagnosticPromptResult)
    await mount()
    await issue({ type: 'start_diagnostic', track: 'school', require_unexposed: true,
      subject: '数学', knowledge_node_id: 'quadratic-node', capability: 'diagnostic.start', execution_location: 'client_device_bridge' })
    expect(prepare).toHaveBeenCalledExactlyOnceWith(initial.session.session_id, expect.objectContaining({
      track: 'school', subject: '数学', knowledge_node_id: 'quadratic-node', require_unexposed: true,
      client_action_id: 'voice-diagnostic:device-turn',
    }))
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith('device-turn', expect.any(String), 'completed', undefined,
      expect.objectContaining({ execution_receipt: expect.objectContaining({ capability: 'diagnostic.start', output: expect.objectContaining({ prompt }) }) }))
  })

  it.each(['new', 'replayed'] as const)('interrupts old teaching only when a %s diagnostic takes over', async mode => {
    const prior = { turn_id: 'old-teaching', child_id: initial.session.child_id, session_id: initial.session.session_id,
      created_at: '2026-10-07T21:00:00.000000+00:00' }
    const prompt = { prompt_id: mode === 'new' ? 'new-original' : 'same-original', item_version: 'v1',
      child_id: initial.session.child_id, origin_session_id: initial.session.session_id,
      created_at: mode === 'new' ? '2026-10-07T21:00:01.000000+00:00' : '2026-10-07T20:59:59.000000+00:00',
      diagnostic_item_id: 'source-item', item: { track: 'school', subject: '数学', prompt: '来源原题', answer_type: 'closed' } }
    const waiting = { status: 'waiting_answer', prompt } as DiagnosticPromptResult
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(mode === 'new' ? { status: 'answered' } as DiagnosticPromptResult : waiting)
    const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt').mockResolvedValue(waiting)
    const interrupt = vi.spyOn(TutorPlaybackController.prototype, 'interrupt')
    await act(async () => { render(<SessionWorkspace initial={{ ...initial, latest_tutor_turn: prior } as SessionWorkspaceState}
      childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />) })
    await issue({ type: 'start_diagnostic', track: 'school' })
    if (mode === 'new') { expect(prepare).toHaveBeenCalledOnce(); expect(interrupt).toHaveBeenCalledOnce() }
    else { expect(prepare).not.toHaveBeenCalled(); expect(interrupt).not.toHaveBeenCalled() }
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith('device-turn', '来源原题', 'completed')
  })

  it('voices a saved-answer hold instead of starting evaluation or asking another question', async () => {
    const owner = { child_id: initial.session.child_id, session_id: initial.session.session_id }
    const prompt = { prompt_id: 'held-prompt', item_version: 'v1', child_id: owner.child_id,
      origin_session_id: owner.session_id, diagnostic_item_id: 'held-item', item: { track: 'school', answer_type: 'open_response' } }
    vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue({ status: 'waiting_answer', prompt } as DiagnosticPromptResult)
    vi.spyOn(localApi, 'answerDiagnosticPrompt').mockResolvedValue({ status: 'answered', prompt,
      attempt: { ...owner, attempt_id: 'held-answer', diagnostic_item_id: 'held-item', feedback: '回答已保存' },
      local_learning_evaluation: { status: 'blocked', reason: 'scheduled_break_control_changed',
        local_only: true, cloud_transmission_allowed: false } } as DiagnosticPromptResult)
    const execute = vi.spyOn(localApi, 'executeAgentTurn')
    const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt')
    await mount()
    await issue({ type: 'submit_diagnostic_answer', answer: '我的推理是这样的',
      diagnostic_prompt_id: prompt.prompt_id, diagnostic_item_version: prompt.item_version })
    expect(inputs.speakLocally).toHaveBeenLastCalledWith(expect.stringContaining('不用再答一遍'), owner.session_id,
      expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
    expect(execute).not.toHaveBeenCalled()
    expect(prepare).not.toHaveBeenCalled()
    expect(localApi.askTutor).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith('device-turn', expect.stringContaining('休息记录'),
      'completed')
  })

  it('reads the frozen writing original locally and persists playback before completing the device turn', async () => {
    const packet = { confirmation_id: 'confirmation', binding_version: 'a'.repeat(64),
      prompt_id: 'writing-prompt', item_version: 'version', artifact_id: 'page', revision_id: 'revision', response_text: '合成原稿',
      child_id: initial.session.child_id, session_id: initial.session.session_id,
      readback_text: '这份合成原稿读完后再确认。', readback_sha256: 'b'.repeat(64),
      expires_at: new Date(Date.now() + 60_000).toISOString(), local_only: true as const,
      cloud_transmission_allowed: false as const }
    vi.spyOn(localApi, 'writingOcrConfirmation').mockResolvedValue(packet)
    const receipt = vi.spyOn(localApi, 'recordWritingReadback').mockResolvedValue({})
    vi.mocked(inputs.speakLocally).mockImplementation((_text, _session, _done, started, policy) => {
      started?.(); policy?.onPlaybackEnd?.()
    })
    await mount()
    await issue({ type: 'read_writing_original', capability: 'writing.read_original',
      execution_location: 'client_device_bridge',
      writing_ocr_confirmation: { confirmation_id: packet.confirmation_id, binding_version: packet.binding_version } })
    expect(localApi.writingOcrConfirmation).toHaveBeenCalledExactlyOnceWith(initial.session.session_id, 'confirmation', packet.binding_version)
    expect(receipt.mock.calls.map(call => call[1].event)).toEqual(['started', 'ended'])
    expect(inputs.speakLocally).toHaveBeenCalledWith(packet.readback_text, initial.session.session_id,
      expect.any(Function), expect.any(Function), expect.objectContaining({localOnly:true}))
    expect(receipt.mock.invocationCallOrder[1]).toBeLessThan(vi.mocked(localApi.completeAgentTurn).mock.invocationCallOrder[0])
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith('device-turn', expect.any(String), 'completed', undefined,
      expect.objectContaining({client_bridge_receipt: expect.objectContaining({output: expect.objectContaining({status:'readback_completed'})})}))
    expect(localApi.askTutor).not.toHaveBeenCalled()
  })

  it('does not claim a writing readback when no audio was delivered', async () => {
    vi.spyOn(localApi, 'writingOcrConfirmation').mockResolvedValue({ confirmation_id: 'confirmation', binding_version: 'a'.repeat(64),
      child_id: initial.session.child_id, session_id: initial.session.session_id, prompt_id:'prompt', item_version:'v1',
      artifact_id:'page', revision_id:'revision', response_text:'original', readback_text:'Read this synthetic original.',
      readback_sha256:'b'.repeat(64), expires_at:new Date(Date.now()+60_000).toISOString(),
      local_only:true, cloud_transmission_allowed:false })
    const receipt=vi.spyOn(localApi,'recordWritingReadback')
    vi.mocked(inputs.speakLocally).mockImplementation((_text,_session,_done,_started,policy)=>policy?.onNotDelivered?.('unavailable'))
    await mount()
    await issue({type:'read_writing_original',capability:'writing.read_original',execution_location:'client_device_bridge',
      writing_ocr_confirmation:{confirmation_id:'confirmation',binding_version:'a'.repeat(64)}})
    expect(receipt).not.toHaveBeenCalled()
    expect(vi.mocked(localApi.completeAgentTurn).mock.calls[0][2]).toBe('failed')
    expect(localApi.askTutor).not.toHaveBeenCalled()
  })

  it.each([false,true])('continues after original confirmation using the persisted feedback turn (local=%s)',async local=>{
    const owner={child_id:initial.session.child_id,session_id:initial.session.session_id}
    const notice={status:local?'local_feedback_required':'feedback_required',feedback_turn_id:'evaluation-turn',
      ...(local?{local_feedback_turn_id:'reference-turn'}:{}),local_only:true,cloud_transmission_allowed:false}
    const execute=vi.spyOn(localApi,'executeAgentTurn').mockImplementation(async id=> id==='device-turn'
      ? {turn:{...owner,agent_turn_id:id,response_text:'原稿已保存。',action:{type:'confirm_writing_original'},local_evaluation:notice} as AgentTurn,
        execution:{capability:'writing.confirm_original',response_text:'原稿已保存。',result:{},evidence:[]}}
      : {turn:{...owner,agent_turn_id:id,response_text:'我们先核对原稿第一项要求。',action:{type:'none'},after_execution:'stop'} as AgentTurn,
        execution:{capability:local?'conversation.respond':'learning.evaluate_response',
          response_text:'我们先核对原稿第一项要求。',result:{},evidence:[]}})
    await mount()
    await issue({type:'confirm_writing_original',source:'agent_runtime_writing',capability:'writing.confirm_original',execution_location:'server'},'是我写的，识别没错')
    expect(execute.mock.calls.map(call=>call[0])).toEqual(['device-turn',local?'reference-turn':'evaluation-turn'])
    expect(screen.getByText('我们先核对原稿第一项要求。')).toBeTruthy()
    expect(inputs.speakLocally).toHaveBeenLastCalledWith('我们先核对原稿第一项要求。',initial.session.session_id,
      expect.any(Function),expect.any(Function),expect.objectContaining({localOnly:true}))
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  })

  it.each([
    ['writing.revise_original','revise_writing_original',false],
    ['writing.independent_check','check_writing_independently',false],
    ['task.start','start_next_task',true],
    ['task.resume','resume_paused_task',true],
  ] as const)('binds the next microphone capture to the actual %s prompt without requiring a page button',async(capability,type,nested)=>{
    const result={status:'waiting_answer',prompt:{prompt_id:'revision-prompt',item_version:'revision-version',
      child_id:initial.session.child_id,origin_session_id:initial.session.session_id,
      diagnostic_item_id:'revision-item',created_at:new Date().toISOString(),review_task_id:null,
      item:{diagnostic_item_id:'revision-item',track:'pet',answer_type:'writing',pet_audio_modality:null,
        prompt:'请修改纸上的原稿。'}},attempt:null,reason:null}
    vi.spyOn(localApi,'executeAgentTurn').mockResolvedValue({turn:{agent_turn_id:'device-turn',response_text:'请修改纸上的原稿。',action:{type}} as AgentTurn,
      execution:{capability,response_text:'请修改纸上的原稿。',result:nested?{status:'active',diagnostic_prompt:result}:result,evidence:[]}})
    await mount()
    await issue({type,source:'agent_runtime_writing',capability,execution_location:'server'},'我想继续练习')
    expect(voiceCaptureReference(initial.session.session_id,initial.session.child_id).question_reference)
      .toEqual({kind:'diagnostic',binding:{prompt_id:'revision-prompt',item_version:'revision-version'}})
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    expect(inputs.speakLocally).toHaveBeenLastCalledWith('请修改纸上的原稿。',initial.session.session_id,
      expect.any(Function),expect.any(Function),expect.objectContaining({localOnly:true}))
  })

  it.each(['task.start', 'task.resume'] as const)('binds and speaks the original correction after %s, without opening a panel', async capability => {
    const type = capability === 'task.start' ? 'start_next_task' : 'resume_paused_task'
    const binding = { session_id: initial.session.session_id, child_id: initial.session.child_id,
      mistake_id: 'saved-paper', phase: 'attempt', item_id: 'paper-check', source_version: 'original-v1' }
    const current = { ...binding, state: { question_text: '原题内容', submitted_answer: '72',
      next_activity: { kind: 'attempt', stage: 'near_transfer', item: { diagnostic_item_id: 'paper-check' } } },
      answer_binding: binding } as unknown as MistakeDialogue
    vi.spyOn(localApi, 'mistakeDialogue').mockResolvedValue({ dialogue: current })
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({
      turn: { agent_turn_id: 'device-turn', response_text: '规划过渡句', action: { type } } as AgentTurn,
      execution: { capability, response_text: '我们接着这道原题，请独立回答新题。', evidence: [],
        result: { status: 'active', source_type: 'mistake_review', mistake_dialogue: current } },
    })
    await mount()
    await issue({ type, capability, source: 'agent_runtime_execution', execution_location: 'server' }, '继续复习')
    expect(voiceCaptureReference(initial.session.session_id, initial.session.child_id).question_reference)
      .toEqual({ kind: 'mistake', binding })
    expect(inputs.speakLocally).toHaveBeenLastCalledWith('我们接着这道原题，请独立回答新题。', initial.session.session_id,
      expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  })

  it.each(['changed', 'unavailable', 'replay'] as const)('does not speak a stale correction or execute it again (%s)', async mode => {
    const current = { session_id: initial.session.session_id, child_id: initial.session.child_id,
      mistake_id: 'saved-paper', phase: 'diagnosis', state: { question_text: '旧题', submitted_answer: '72' } } as unknown as MistakeDialogue
    const load = vi.spyOn(localApi, 'mistakeDialogue')
    if (mode === 'unavailable') load.mockRejectedValue(new Error('read unavailable'))
    else load.mockResolvedValue({ dialogue: { ...current, mistake_id: 'another-paper' } })
    const execute = vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({
      turn: { agent_turn_id: 'device-turn', response_text: '不要重复旧讲解', action: { type: 'start_next_task' } } as AgentTurn,
      execution: { capability: 'task.start', response_text: '不要重复旧讲解', evidence: [],
        ...(mode === 'replay' ? { replayed: true, recorded_receipt: true, replay_audio_allowed: false } : {}),
        result: { status: 'active', source_type: 'mistake_review', mistake_dialogue: current } },
    })
    await mount()
    await issue({ type: 'start_next_task', capability: 'task.start', source: 'agent_runtime_execution', execution_location: 'server' }, '开始复习')
    expect(execute).toHaveBeenCalledTimes(1)
    expect(vi.mocked(inputs.speakLocally).mock.calls.some(call => call[0] === '不要重复旧讲解')).toBe(false)
    expect(voiceCaptureReference(initial.session.session_id, initial.session.child_id).question_reference).toBeNull()
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  })

  it.each(['task.start', 'task.resume'] as const)('uses the delivered next-day spoken question for microphone context (%s)', async capability => {
    const type = capability === 'task.start' ? 'start_next_task' : 'resume_paused_task'
    const text = '不求出具体根，说明这个方程的实数根情况和判断步骤。'
    const result = { status: 'waiting_answer', prompt: {
      prompt_id: 'next-day-oral-prompt', item_version: 'next-day-v1',
      child_id: initial.session.child_id, origin_session_id: initial.session.session_id,
      diagnostic_item_id: 'different-published-item', created_at: new Date().toISOString(), review_task_id: null,
      item: { diagnostic_item_id: 'different-published-item', track: 'school', answer_type: 'open_response',
        response_mode: 'spoken_explanation', prompt: text },
    }, attempt: null, reason: null }
    vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({
      turn: { agent_turn_id: 'device-turn', response_text: text, action: { type } } as AgentTurn,
      execution: { capability, response_text: text, evidence: [], result: {
        status: 'active', source_type: 'oral_daily_check', diagnostic_prompt: result,
      } },
    })
    await mount()
    vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'device-turn', response_text: '规划过渡句',
      runtime_execution_location: 'server', runtime_capability: capability,
      action: { type, capability, execution_location: 'server' },
    } as AgentTurn)
    await act(async () => { await microphone.onTranscript('开始今天安排的复查') })
    expect(voiceCaptureReference(initial.session.session_id, initial.session.child_id).question_reference)
      .toEqual({ kind: 'diagnostic', binding: { prompt_id: 'next-day-oral-prompt', item_version: 'next-day-v1' } })
    expect(inputs.speakLocally).toHaveBeenLastCalledWith(text, initial.session.session_id,
      expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  })

  it('explains a pending daily-check feedback without resubmitting the saved answer', async () => {
    vi.spyOn(localApi, 'executeAgentTurn').mockRejectedValue(new ApiError(
      '回答已保存，请先核对本次反馈，再结束复查；不用重新回答。', 409, 'ORAL_DAILY_CHECK_FEEDBACK_REQUIRED'))
    const answer = vi.spyOn(localApi, 'answerDiagnosticPrompt')
    await mount()
    vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'device-turn', response_text: '',
      runtime_execution_location: 'server', runtime_capability: 'task.complete',
      action: { type: 'complete_active_task', capability: 'task.complete', execution_location: 'server' },
    } as AgentTurn)
    await act(async () => { await microphone.onTranscript('这个复查结束吧') })
    expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith('device-turn')
    expect(inputs.speakLocally).toHaveBeenLastCalledWith(
      '回答已保存，请先核对本次反馈，再结束复查；不用重新回答。', initial.session.session_id,
      expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
    expect(answer).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  })

  it.each([{ replayed: true }, { recorded_receipt: { event_id: 'old-resume' } }])('does not reactivate the old prompt or speak its acknowledgement on resume replay (%j)', async receipt => {
    const execute = vi.spyOn(localApi, 'executeAgentTurn').mockResolvedValue({
      turn: { agent_turn_id: 'device-turn', response_text: '旧的计时开始提醒', action: { type: 'resume_paused_task' } } as AgentTurn,
      execution: { capability: 'task.resume', response_text: '旧的计时开始提醒', evidence: [], result: {
        ...receipt, status: 'paused', diagnostic_prompt: { status: 'waiting_answer', prompt: {
          prompt_id: 'old-prompt', item_version: 'v1', child_id: initial.session.child_id, origin_session_id: initial.session.session_id,
        } },
      } },
    })
    const resume = vi.spyOn(localApi, 'resumePlanItem')
    await mount()
    await issue({ type: 'resume_paused_task', source: 'agent_runtime_control', capability: 'task.resume', execution_location: 'server' })
    expect(execute).toHaveBeenCalledExactlyOnceWith('device-turn')
    expect(voiceCaptureReference(initial.session.session_id, initial.session.child_id).question_reference).toBeNull()
    expect(vi.mocked(inputs.speakLocally).mock.calls.map(([text]) => text)).not.toContain('旧的计时开始提醒')
    expect(resume).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  })

  it('refuses an unbound legacy resume instead of picking a paused task in the renderer', async () => {
    const resume = vi.spyOn(localApi, 'resumePlanItem')
    const start = vi.spyOn(localApi, 'startPlanItem')
    await mount()
    await issue({ type: 'resume_paused_task' }, '继续刚才的任务')
    expect(resume).not.toHaveBeenCalled()
    expect(start).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).toHaveBeenCalledExactlyOnceWith('device-turn', expect.stringContaining('暂停记录'), 'failed', 'CLIENT_VOICE_ACTION_UNAVAILABLE')
  })

  it.each(['execution_receipt_required', 'superseded', 'admitted'])('requires current control admission before replaying a bound question (%s)', async status => {
    const admission = vi.spyOn(localApi, 'admitVoiceControlExecution').mockResolvedValue({ status })
    const load = vi.spyOn(localApi, 'diagnosticPrompt').mockRejectedValue(new ApiError('Unavailable', 503, 'LOCAL_SERVICE_OFFLINE'))
    await mount()
    vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'bound-control', response_text: '控制请求',
      requires_control_admission: true, runtime_execution_location: 'client_device_bridge',
      action: { type: 'replay_diagnostic', diagnostic_prompt_id: 'original-prompt', diagnostic_item_version: 'v1' } } as AgentTurn)
    await act(async () => { await microphone.onTranscript('这一道再说一遍吧') })
    expect(admission).toHaveBeenCalledExactlyOnceWith(initial.session.session_id, 'bound-control')
    if (status === 'admitted') {
      expect(load).toHaveBeenCalledExactlyOnceWith(initial.session.session_id, 'original-prompt')
      expect(admission.mock.invocationCallOrder[0]).toBeLessThan(load.mock.invocationCallOrder[0])
    } else {
      expect(load).not.toHaveBeenCalled()
      expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    }
  })
})
