import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import type { QwenRealtimeVoiceInput } from './QwenRealtimeVoiceInput'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, localApi, requestPetListeningAudio } from './api'
import * as inputs from './LocalInputs'
import * as voice from './companionVoice'
import * as audio from './petListeningAudio'
import { SessionWorkspace } from './SessionWorkspace'
import { clearVoiceCaptureContext, setVoiceQuestionCapture, voiceCaptureReference } from './voiceCaptureContext'
import type { AgentTurn, DiagnosticPromptResult, SessionWorkspaceState } from './types'
import type { PetMedia } from './petMedia'
import oralPackets from './test/oralCoreReceipts.json'
import { webcrypto } from 'node:crypto'
import { checkNativeWritingSubmission, submitNativeWriting } from './nativeWritingFlow'
import type { NativeWritingOriginal } from '../../shared/nativeWriting'

beforeEach(() => {
  for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
    vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
      handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
  }
})

let microphone: ComponentProps<typeof inputs.HandsFreeVoiceInput>
let nativeRecorder: ComponentProps<typeof QwenRealtimeVoiceInput> | undefined
vi.mock('./LocalInputs', async original => ({
  ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null },
  VoiceInputButton: () => null,
}))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), requestPetListeningAudio: vi.fn() }))
// The calibration component owns device readiness. Other tests in this file
// are not calibration tests, so this fixture reports a known synthetic binding.
vi.mock('./VoiceCalibrationPanel', async () => {
  const { useEffect } = await import('react')
  return { VoiceCalibrationPanel: ({ onMicrophoneBindingReadyChange }: { onMicrophoneBindingReadyChange: (ready: boolean) => void }) => {
    useEffect(() => { onMicrophoneBindingReadyChange(true) }, [onMicrophoneBindingReadyChange])
    return null
  } }
})
vi.mock('./QwenRealtimeVoiceInput', async original => ({
  ...await original<typeof import('./QwenRealtimeVoiceInput')>(),
  QwenRealtimeVoiceInput: (props: ComponentProps<typeof QwenRealtimeVoiceInput>) => { nativeRecorder = props; return null },
}))
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))

const now = '2026-09-08T10:00:00Z'
const secret = 'synthetic-ae56c59ccc5c535fb09fb1f9'
function workspace(child = 'child-a', session = 'session-a'): SessionWorkspaceState {
  return {
    session: { session_id: session, child_id: child, status: 'active', state: 'study', started_at: now,
      last_checkpoint_at: now, observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status: 'paused', snooze_until: null, recall_declared_empty: 0, homework_declared_empty: 0 },
    family_rule: { child_id: child, latest_end_time: '21:30', max_learning_minutes: 90, task_segment_minutes: 25,
      break_minutes: 5, pet_min_minutes: 0, competition_minutes: 0, observation_mode: 'shadow',
      reminder_budget_per_10m: 1, misclassification_shadow_threshold: 3, version: 1, updated_at: now },
    planning_context: { energy_level: 'medium', fixed_commitment_minutes: 0, fixed_commitment_note: null,
      tomorrow_event_subject: null, tomorrow_event_title: null, tomorrow_event_minutes: 0, updated_at: now },
    recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [], drafts: {}, latest_tutor_turn: null,
    daily_report_ready: false,
    plan: { plan_id: 'plan-a', plan_date: '2026-09-08', status: 'active', total_minutes: 5, generator: 'local_rule_engine', version: 1 },
    plan_items: [{ plan_item_id: 'pet-task', homework_id: null, subject: '英语', title: '原题听力', expected_minutes: 5,
      actual_minutes: null, priority: 1, status: 'active', position: 1, started_at: now, completed_at: null,
      active_seconds: 0, active_segment_started_at: null, source_type: 'pet_daily', source_ref: 'exact-item',
      completion_standard: '先听原音再回答', cognitive_load: 'medium', task_type: 'written', completion_evidence_type: null,
      completion_note: null, completion_checklist: [], completion_artifact_id: null }],
    sensor_release: { opened_leases: 0, active_leases: 0, released_leases: 0, application_release_confirmed: false,
      evidence_level: 'not_opened', os_release_verified: false, release_verification: null, by_device: {} },
  }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
let current: SessionWorkspaceState
let prompt: DiagnosticPromptResult
let media: PetMedia
let turn: AgentTurn
let history: Map<string, Awaited<ReturnType<typeof localApi.petPlaybackActionReceipt>>['receipt']>
const envelope = () => ({ media: structuredClone(media), local_only: true as const, cloud_transmission_allowed: false as const })
const execution = () => ({ turn, execution: { capability: 'task.start', response_text: turn.response_text,
  result: { ...current.plan_items[0], diagnostic_prompt: prompt }, evidence: ['plan_item_started', 'diagnostic_prompt_bound'] } })
const spoken = () => vi.mocked(inputs.speakLocally).mock.calls.map(([text]) => text).join('\n')
async function mount() {
  let view!: ReturnType<typeof render>
  await act(async () => { view = render(<SessionWorkspace key={current.session.child_id} initial={current} childName="小辰" childGrade="四年级" onBack={vi.fn()} />) })
  return view
}
async function say() { await act(async () => { await microphone.onTranscript('开始这项任务') }) }

it.each([
  ['GOAL_CATALOG_SOURCE_CHANGED', '这项任务的资料已经变更。请让家长核对当前资料，并按新路线重新安排；不会自动换成另一道题。'],
  ['GOAL_CATALOG_SOURCE_UNBOUND', '这项旧任务的资料绑定无法核实。请让家长核对资料并重新安排，暂不开始或继续旧题。'],
  ['GOAL_RULE_PLAN_CHANGED', '家长已核对新的规则，这项旧安排没有自动换版。请按新路线重新安排，原来的学习记录仍会保留。'],
])('speaks exact goal conflict without generic retry or replacing the original task (%s)', async (code, message) => {
  current.plan_items[0].status = 'pending'
  current.plan_items[0].started_at = null
  current.plan_items[0].source_type = 'junior_exam_goal'
  turn.tool_status = 'pending'
  vi.mocked(localApi.executeAgentTurn).mockRejectedValue(new ApiError(message, 409, code))
  await mount()
  await say()
  expect(spoken()).toContain(message)
  expect(spoken()).not.toContain('你可以再试一次')
  expect(localApi.executeAgentTurn).toHaveBeenCalledTimes(1)
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
})

it.each([
  ['TASK_START_LEGACY_REVIEW_BUDGET_INSUFFICIENT', '今天剩余时间不够完成这项练习，先保留，不挤占休息时间。'],
  ['TASK_START_LEGACY_REVIEW_AUTHORIZATION_CHANGED', '材料的使用许可已变化，先请家长核对；现在不会开始这项练习。'],
])('speaks the current legacy review conflict without retrying or starting (%s)', async (code, message) => {
  current.plan_items[0].status = 'pending'
  current.plan_items[0].started_at = null
  current.plan_items[0].source_type = 'due_review'
  turn.tool_status = 'pending'
  vi.mocked(localApi.executeAgentTurn).mockRejectedValue(new ApiError(message, 409, code))
  await mount()
  await say()
  expect(spoken()).toContain(message)
  expect(spoken()).not.toContain('你可以再试一次')
  expect(localApi.executeAgentTurn).toHaveBeenCalledTimes(1)
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(requestPetListeningAudio).not.toHaveBeenCalled()
})

it.each([
  ['ANSWER_ORAL_FOLLOWUP_SOURCE_CHANGED', '原口语材料已变化，先核对材料，不会自动换题。'],
  ['ANSWER_ORAL_FOLLOWUP_CANCELLED', '刚才的口语练习请求已取消，没有重新开始。'],
  ['ANSWER_ORAL_FOLLOWUP_PROMPT_CHANGED', '当前题目已经变化，请告诉我想继续哪一道。'],
])('speaks oral followup rejection without a new prompt or repeat (%s)', async (code, message) => {
  turn.runtime_capability = 'oral.retry'
  turn.tool_status = 'pending'
  turn.action = { type: 'retry_oral_answer', capability: 'oral.retry' }
  vi.mocked(localApi.executeAgentTurn).mockRejectedValue(new ApiError(message, 409, code))
  await mount()
  await act(async () => { await microphone.onTranscript('我想把刚才的回答再说一遍') })
  expect(spoken()).toContain(message)
  expect(spoken()).not.toContain('你可以再试一次')
  expect(localApi.executeAgentTurn).toHaveBeenCalledTimes(1)
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(requestPetListeningAudio).not.toHaveBeenCalled()
})

beforeEach(() => {
  microphone = undefined as unknown as typeof microphone
  nativeRecorder = undefined
  current = workspace()
  prompt = { status: 'waiting_answer', reason: null, attempt: null, prompt: {
    prompt_id: 'original-prompt', diagnostic_item_id: 'exact-item', item_version: 'item-v1',
    child_id: 'child-a', origin_session_id: 'session-a', created_at: now, review_task_id: null,
    item: { diagnostic_item_id: 'exact-item', track: 'pet', answer_type: 'listening_text',
      pet_audio_modality: 'listening', title: '原题', prompt: 'Where should they meet?', stimulus_text: secret } as NonNullable<DiagnosticPromptResult['prompt']>['item'],
  } }
  turn = { agent_turn_id: 'original-turn', transcript: '开始这项任务', intent: 'start_next_task',
    action: { type: 'start_next_task' }, response_text: secret, tool_status: 'completed', tool_error_code: null,
    completed_at: now, created_at: now, orchestrator_version: 'test', runtime_capability: 'task.start', runtime_execution_location: 'server' }
  media = { media_id: 'original-media', prompt_id: 'original-prompt', item_version: 'item-v1', child_id: 'child-a',
    session_id: 'session-a', diagnostic_item_id: 'exact-item', source_version: 'original-source-v1', kind: 'listening', generation: 0, revision: 0,
    source_reviewed: true, status: 'ready', audio: { sha256: 'a'.repeat(64), frames: 16000, sample_rate: 16000 },
    qualification: { required_play_count: 1, task_complete_play_count: 0, task_delivery_qualified: false },
    local_only: true, cloud_transmission_allowed: false } as PetMedia
  history = new Map()
  vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
    getWakeWordSettings: vi.fn(async () => ({ childId: current.session.child_id, wakeWord: '小知', enabled: true, inactivitySeconds: 60, updatedAt: now })),
    getVoiceOnboarding: vi.fn(async () => ({ childId: current.session.child_id, version: 1, step: 'completed', updatedAt: now })),
  }))
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('UNEXPECTED_NETWORK')))
  vi.spyOn(voice, 'getCompanionVoiceState').mockReturnValue('idle')
  vi.spyOn(inputs, 'speakLocally').mockImplementation((_text, _session, done) => { done?.('text_only') })
  vi.spyOn(inputs, 'cancelLocalSpeech').mockImplementation(() => undefined)
  vi.spyOn(inputs, 'queueLocalPcm').mockImplementation(async (_pcm, _rate, _sid, start, options) => {
    if (options?.beforePlayback && !await options.beforePlayback(new AbortController().signal)) return 'superseded'
    start?.(); return 'finished'
  })
  vi.spyOn(audio, 'decodePetListening').mockResolvedValue({ pcm: new ArrayBuffer(32000), sampleRate: 16000 })
  vi.mocked(requestPetListeningAudio).mockReset().mockResolvedValue(new ArrayBuffer(44))
  vi.spyOn(localApi, 'agentTurn').mockImplementation(async () => turn)
  vi.spyOn(localApi, 'executeAgentTurn').mockImplementation(async () => execution())
  vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(turn)
  vi.spyOn(localApi, 'sessionWorkspace').mockImplementation(async () => current)
  vi.spyOn(localApi, 'saveSessionDraft').mockImplementation(async (_id, section, payload) => ({ section, payload, updated_at: now }))
  vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
  vi.spyOn(localApi, 'prepareDiagnosticPrompt').mockResolvedValue(prompt)
  vi.spyOn(localApi, 'answerDiagnosticPrompt').mockResolvedValue(prompt)
  vi.spyOn(localApi, 'petPromptMedia').mockImplementation(async () => ({ prompt_id: 'original-prompt', item_version: 'item-v1',
    child_id: 'child-a', session_id: 'session-a', listening: [structuredClone(media)], speaking: [], selection_required: false,
    status: 'bound_media', local_only: true, cloud_transmission_allowed: false }))
  vi.spyOn(localApi, 'petPcmControlReference').mockImplementation(async (sid, mid, hash) => {
    expect([sid, mid, hash]).toEqual([media.session_id, media.media_id, media.audio.sha256])
    expect(media.status).toBe('playing'); expect(media.generation).toBeGreaterThanOrEqual(1)
    return { version: 'pet_full_audio_control_reference_v1', child_id: media.child_id, session_id: media.session_id,
      media_id: media.media_id, prompt_id: media.prompt_id, item_version: media.item_version,
      diagnostic_item_id: media.diagnostic_item_id, source_version: media.source_version, generation: media.generation,
      audio_sha256: media.audio.sha256!, spoken_text_sha256: 'e'.repeat(64), source_review_event_id: 'parent-full-original-review',
      local_only: true, cloud_transmission_allowed: false }
  })
  vi.spyOn(localApi, 'petPlaybackActionReceipt').mockImplementation(async (_sid, _mid, key) => ({
    child_id: 'child-a', session_id: 'session-a', media_id: media.media_id, client_action_id: key,
    receipt: history.get(key) ?? null, current: structuredClone(media), automatic_playback_allowed: false,
    replay_decision: history.has(key) ? 'reuse_completed' : 'not_recorded', local_only: true, cloud_transmission_allowed: false,
  }))
  vi.spyOn(localApi, 'petMediaControl').mockImplementation(async (_sid, command) => {
    media.generation++; media.revision++; media.status = command.command === 'pause' ? 'paused' : 'playing'
    return envelope()
  })
  vi.spyOn(localApi, 'petMediaPlayback').mockImplementation(async (_sid, receipt) => {
    media.revision++
    if (receipt.event === 'ended') {
      media.status = 'playback_finished'; media.qualification.task_complete_play_count = 1
      history.set(receipt.client_action_id, { completion_accepted: true, generation: media.generation } as NonNullable<ReturnType<typeof history.get>>)
    }
    return envelope()
  })
})
afterEach(() => { cleanup(); clearVoiceCaptureContext(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('keeps capture blocked when native dialogue has no realtime configuration', async () => {
  await mount()
  await waitFor(() => expect(screen.getByText('唤醒词“小知”已保存，监听尚未启动')).toBeInTheDocument())
  expect(nativeRecorder).toBeUndefined()
  expect(microphone).toBeUndefined()
  expect(localApi.agentTurn).not.toHaveBeenCalled()
})

it('mounts the native recorder after configured policy and microphone readiness', async () => {
  Object.assign(window.desktop!, { getQwenRealtimeSettings: vi.fn(async () => ({ configured: true, enabled: true })) })
  await mount()
  await waitFor(() => expect(nativeRecorder?.nativeRuntime).toBeDefined())
  expect(nativeRecorder?.audioAllowed).toBe(true)
  expect(microphone).toBeUndefined()
  expect(localApi.agentTurn).not.toHaveBeenCalled()
})

it('accepts the explicit writing revision prompt after the same scheduled task becomes active', async () => {
  current.plan_items[0].status = 'pending'
  prompt.prompt!.item.answer_type = 'writing'
  vi.spyOn(localApi, 'diagnosticPrompt').mockImplementation(async () => structuredClone(prompt))
  const capture = vi.fn().mockRejectedValue(new Error('CAPTURE_BOUNDARY_STOP'))
  window.desktop!.captureNativeWritingOriginal = capture
  await mount()
  current = structuredClone(current)
  current.plan_items[0].status = 'active'
  await say()
  await waitFor(() => expect(screen.getByLabelText('输入挑战题答案')).toBeTruthy())
  const revised = structuredClone(prompt)
  revised.prompt!.prompt_id = 'explicit-new-writing-prompt'
  await act(async () => {
    window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated', {
      detail: { sessionId: current.session.session_id, result: revised },
    }))
  })
  fireEvent.change(screen.getByLabelText('输入挑战题答案'), { target: { value: 'This is the newly revised original.' } })
  fireEvent.click(screen.getByText('提交答案'))
  await waitFor(() => expect(capture).toHaveBeenCalled())
  expect(capture.mock.calls[0][0]).toMatchObject({
    prompt_id: revised.prompt!.prompt_id, item_version: revised.prompt!.item_version,
    diagnostic_item_id: revised.prompt!.diagnostic_item_id, child_id: current.session.child_id,
    session_id: current.session.session_id,
  })
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})

const typedWritingDraft = () => ({ input_kind: 'typed' as const, text: 'A typed draft before submission.', scope: {
  child_id: 'child-a', session_id: 'session-a', prompt_id: 'original-prompt', item_version: 'item-v1', diagnostic_item_id: 'exact-item',
} })

it.each(['query','submit','open_submit','unknown','changed_text','revoked_admission'] as const)('semantic native writing dispatch preserves the typed original (%s)',async mode=>{
  vi.stubGlobal('crypto',webcrypto)
  prompt.prompt!.item.answer_type='writing'
  if(mode==='open_submit')Object.assign(prompt.prompt!.item,{answer_type:'open_response',assessment_mode:'open_response_rubric',response_mode:'written'})
  current.drafts.study={updated_at:now,payload:{native_writing_draft:typedWritingDraft()}}
  const saved=typedWritingDraft(),hash=Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(saved.text))).toString('hex')
  const original:NativeWritingOriginal={...saved.scope,text_sha256:hash,artifact_id:'saved-native-original',original_revision:'a'.repeat(64),
    status:'completed',local_only:true,cloud_transmission_allowed:false}
  await submitNativeWriting({scope:saved.scope,text:saved.text,isCurrent:()=>true,capture:async()=>{throw Error('receipt lost')},answer:vi.fn(),
    read:async query=>({...query,state:'capture_unknown',original:null,result:null,local_only:true,cloud_transmission_allowed:false})}).catch(()=>undefined)
  const read=vi.spyOn(localApi,'nativeWritingReceipt').mockImplementation(async(_sid,query)=>({...query,state:'captured',original,result:null,
    local_only:true,cloud_transmission_allowed:false}))
  if(mode!=='unknown')await checkNativeWritingSubmission(saved.scope.child_id,saved.scope.session_id,()=>true)
  read.mockClear()
  const capture=vi.fn();window.desktop!.captureNativeWritingOriginal=capture
  vi.spyOn(localApi,'diagnosticPrompt').mockImplementation(async()=>structuredClone(prompt))
  vi.spyOn(localApi,'admitVoiceControlExecution').mockResolvedValue({status:mode==='revoked_admission'?'cancelled':'admitted'})
  vi.mocked(localApi.answerDiagnosticPrompt).mockImplementation(async(_sid,values)=>({...prompt,status:'answered',
    attempt:{attempt_id:'exact-native-attempt',child_id:saved.scope.child_id,session_id:saved.scope.session_id,
      diagnostic_item_id:saved.scope.diagnostic_item_id,submitted_answer:values.answer,feedback:'已保存'} as DiagnosticPromptResult['attempt']}))
  turn={...turn,tool_status:'pending',requires_control_admission:true,runtime_execution_location:'client_device_bridge',after_execution:'stop',
    runtime_capability:mode==='query'?'native_writing.submission_status':'native_writing.submit_retained',
    action:{type:mode==='query'?'check_native_writing_submission':'submit_retained_native_writing',source:'content_free_voice_control',
      native_writing_target:{...saved.scope,text_sha256:hash}}}
  await mount()
  await waitFor(()=>expect((screen.getByLabelText('输入挑战题答案') as HTMLTextAreaElement).value).toBe(saved.text))
  if(mode==='changed_text')fireEvent.change(screen.getByLabelText('输入挑战题答案'),{target:{value:'Changed typed original.'}})
  await act(async()=>{await microphone.onTranscript(mode==='query'?'刚才作文交上了吗':'继续提交本题')})
  expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
  expect(capture).not.toHaveBeenCalled()
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  if(mode==='submit' || mode==='open_submit') {
    expect(localApi.answerDiagnosticPrompt).toHaveBeenCalledTimes(1)
    expect(localApi.answerDiagnosticPrompt).toHaveBeenCalledWith('session-a',expect.objectContaining({
      answer:saved.text,prompt_id:saved.scope.prompt_id,writing_original_artifact_id:original.artifact_id,authorized_turn_id:turn.agent_turn_id}))
    expect(vi.mocked(localApi.answerDiagnosticPrompt).mock.calls[0][1].answer).not.toBe('继续提交本题')
  } else expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  if(mode==='unknown' || mode==='query')expect(read).toHaveBeenCalledTimes(1)
})
it.each(['query','resume'] as const)('restored answer_saved native writing uses natural explicit resume only (%s)',async mode=>{
  vi.stubGlobal('crypto',webcrypto)
  prompt.status='answer_saved'
  prompt.prompt!.item.answer_type='writing'
  const saved=typedWritingDraft()
  current.drafts.study={updated_at:now,payload:{native_writing_draft:saved}}
  const hash=Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from(saved.text))).toString('hex')
  const original:NativeWritingOriginal={...saved.scope,text_sha256:hash,artifact_id:'saved-native-original',original_revision:'a'.repeat(64),
    status:'completed',local_only:true,cloud_transmission_allowed:false}
  const read=vi.spyOn(localApi,'nativeWritingReceipt').mockImplementation(async(_sid,q)=>({...q,state:'answer_saved',original,result:null,
    local_only:true,cloud_transmission_allowed:false}))
  await submitNativeWriting({scope:saved.scope,text:saved.text,isCurrent:()=>true,capture:async()=>original,
    answer:async()=>{throw Error('post-save interrupted')}}).catch(()=>undefined)
  await checkNativeWritingSubmission(saved.scope.child_id,saved.scope.session_id,()=>true)
  read.mockClear()
  const target={...saved.scope,text_sha256:hash,saved_answer:{answer_action_id:'native-writing-answer:'+original.artifact_id,
    original_artifact_id:original.artifact_id,original_revision:original.original_revision,authorized_turn_id:'old-turn',payload_sha256:'b'.repeat(64)}}
  turn={...turn,tool_status:'pending',requires_control_admission:true,runtime_execution_location:'client_device_bridge',after_execution:'stop',
    runtime_capability:mode==='query'?'native_writing.submission_status':'native_writing.submit_retained',
    action:{type:mode==='query'?'check_native_writing_submission':'submit_retained_native_writing',source:'content_free_voice_control',native_writing_target:target}}
  const resume=vi.spyOn(localApi,'nativeWritingResume').mockImplementation(async(_sid,request)=>({
    ...saved.scope,text_sha256:hash,capture_action_id:request.capture_action_id,answer_action_id:target.saved_answer.answer_action_id,
    state:'answered',original,local_only:true,cloud_transmission_allowed:false,
    result:{...prompt,status:'answered',attempt:{attempt_id:'resumed-attempt',child_id:saved.scope.child_id,session_id:saved.scope.session_id,
      diagnostic_item_id:saved.scope.diagnostic_item_id,submitted_answer:saved.text,feedback:'saved'} as DiagnosticPromptResult['attempt']},
    resume:{client_action_id:request.client_action_id,authorized_turn_id:request.authorized_turn_id,original_authorized_turn_id:'old-turn',attempt_id:'resumed-attempt'}}))
  const capture=vi.fn();window.desktop!.captureNativeWritingOriginal=capture
  vi.spyOn(localApi,'diagnosticPrompt').mockImplementation(async()=>structuredClone(prompt))
  vi.spyOn(localApi,'admitVoiceControlExecution').mockResolvedValue({status:'admitted'})
  await mount()
  await waitFor(()=>expect((screen.getByLabelText('输入挑战题答案') as HTMLTextAreaElement).value).toBe(saved.text))
  await act(async()=>{await microphone.onTranscript(mode==='query'?'刚才作文交上了吗':'继续提交本题')})
  expect(capture).not.toHaveBeenCalled();expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  expect(resume).toHaveBeenCalledTimes(mode==='resume'?1:0)
  if(mode==='resume')expect(resume).toHaveBeenCalledWith('session-a',expect.objectContaining({target,authorized_turn_id:turn.agent_turn_id}))
})

it.each(['restore', 'paste'] as const)('preserves all 4001 typed characters but blocks native submission (%s)', async mode => {
  const text = 'x'.repeat(4001)
  prompt.prompt!.item.answer_type = 'writing'
  if (mode === 'restore') current.drafts.study = { updated_at: now,
    payload: { native_writing_draft: { ...typedWritingDraft(), text } } }
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(prompt)
  const capture = vi.fn()
  window.desktop!.captureNativeWritingOriginal = capture
  await mount()
  await waitFor(() => expect(screen.getByLabelText('输入挑战题答案')).toBeTruthy())
  if (mode === 'paste') fireEvent.change(screen.getByLabelText('输入挑战题答案'), { target: { value: text } })
  await waitFor(() => expect((screen.getByLabelText('输入挑战题答案') as HTMLTextAreaElement).value).toBe(text))
  expect(screen.getByText('超出本次提交的 4000 字上限。原文未截断。')).toBeTruthy()
  expect((screen.getByText('提交答案') as HTMLButtonElement).disabled).toBe(true)
  if (mode === 'paste') await waitFor(() => expect(localApi.saveSessionDraft).toHaveBeenCalledWith('session-a', 'study',
    expect.objectContaining({ native_writing_draft: { ...typedWritingDraft(), text } })), { timeout: 1600 })
  expect(capture).not.toHaveBeenCalled()
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})

it('shows a study save rejection without truncating the oversized typed draft or claiming it saved', async () => {
  const text = '稿'.repeat(4001)
  prompt.prompt!.item.answer_type = 'writing'
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(prompt)
  vi.mocked(localApi.saveSessionDraft).mockRejectedValue(new ApiError('草稿内容过长，请先提交当前内容。', 422, 'INVALID_SESSION_DRAFT'))
  await mount()
  await waitFor(() => expect(screen.getByLabelText('输入挑战题答案')).toBeTruthy())
  fireEvent.change(screen.getByLabelText('输入挑战题答案'), { target: { value: text } })
  await waitFor(() => expect(screen.getByText('草稿保存失败，内容仍在当前页面')).toBeTruthy(), { timeout: 1600 })
  expect((screen.getByLabelText('输入挑战题答案') as HTMLTextAreaElement).value).toBe(text)
  expect(screen.queryByText('草稿已保存')).toBeNull()
})
it('preserves a recovered typed draft when resuming the same paused prompt', async () => {
  current.plan_items[0].status = 'paused'
  prompt.prompt!.item.answer_type = 'writing'
  current.drafts.study = { updated_at: now, payload: { native_writing_draft: typedWritingDraft() } }
  vi.spyOn(localApi, 'diagnosticPrompt').mockImplementation(async () => structuredClone(prompt))
  await mount()
  await waitFor(() => expect((screen.getByLabelText('输入挑战题答案') as HTMLTextAreaElement).value).toBe(typedWritingDraft().text))
  current = structuredClone(current)
  current.plan_items[0].status = 'active'
  await say()
  expect((screen.getByLabelText('输入挑战题答案') as HTMLTextAreaElement).value).toBe(typedWritingDraft().text)
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})
it('does not replace new typing with a late saved-draft verification', async () => {
  prompt.prompt!.item.answer_type = 'writing'
  current.drafts.study = { updated_at: now, payload: { native_writing_draft: typedWritingDraft() } }
  const pending = deferred<DiagnosticPromptResult>()
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValueOnce(prompt).mockReturnValue(pending.promise)
  await mount()
  await waitFor(() => expect(screen.getByLabelText('输入挑战题答案')).toBeTruthy())
  fireEvent.change(screen.getByLabelText('输入挑战题答案'), { target: { value: 'My newer typed draft.' } })
  await act(async () => { pending.resolve(prompt); await pending.promise })
  expect((screen.getByLabelText('输入挑战题答案') as HTMLTextAreaElement).value).toBe('My newer typed draft.')
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})
it('autosaves a typed writing draft with its exact original scope, without capturing an artifact', async () => {
  prompt.prompt!.item.answer_type = 'writing'
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(prompt)
  const capture = vi.fn()
  window.desktop!.captureNativeWritingOriginal = capture
  await mount()
  await waitFor(() => expect(screen.getByLabelText('输入挑战题答案')).toBeTruthy())
  fireEvent.change(screen.getByLabelText('输入挑战题答案'), { target: { value: typedWritingDraft().text } })
  await waitFor(() => expect(localApi.saveSessionDraft).toHaveBeenCalledWith('session-a', 'study',
    expect.objectContaining({ native_writing_draft: typedWritingDraft() })), { timeout: 1600 })
  expect(capture).not.toHaveBeenCalled()
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})

it.each(['exact', 'child_id', 'session_id', 'prompt_id', 'item_version', 'diagnostic_item_id', 'asr'] as const)(
  'restores only the freshly verified exact typed writing draft (%s)', async changed => {
    prompt.prompt!.item.answer_type = 'writing'
    const saved = typedWritingDraft()
    if (changed !== 'exact' && changed !== 'asr') saved.scope[changed] = 'different'
    current.drafts.study = { updated_at: now,
      payload: { native_writing_draft: changed === 'asr' ? { ...saved, input_kind: 'asr' } : saved } }
    const read = vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(prompt)
    const capture = vi.fn()
    window.desktop!.captureNativeWritingOriginal = capture
    await mount()
    await waitFor(() => expect(screen.getByLabelText('输入挑战题答案')).toBeTruthy())
    await waitFor(() => expect((screen.getByLabelText('输入挑战题答案') as HTMLTextAreaElement).value)
      .toBe(changed === 'exact' ? saved.text : ''))
    expect(read).toHaveBeenCalled()
    expect(capture).not.toHaveBeenCalled()
    expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  })

it.each(['active', 'paused'] as const)('restores the %s PET original read-only without a second prepare or automatic audio replay', async status => {
  current.plan_items[0].status = status
  vi.spyOn(localApi, 'nextDiagnostic').mockResolvedValue({ item: prompt.prompt!.item, pet_baseline: null })
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(prompt)
  setVoiceQuestionCapture('session-a', 'child-a', { kind: 'diagnostic', binding: { prompt_id: 'original-prompt', item_version: 'item-v1' } })
  await mount()
  await waitFor(() => expect(screen.getByText('Where should they meet?')).toBeTruthy())
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.nextDiagnostic).not.toHaveBeenCalled()
  expect(inputs.queueLocalPcm).not.toHaveBeenCalled()
  expect(voiceCaptureReference('session-a', 'child-a').question_reference?.binding)
    .toEqual({ prompt_id: 'original-prompt', item_version: 'item-v1' })
  expect(spoken()).not.toContain(secret)
})

it('reads durable activity focus on mount and after actual execution without erasing the original question on end', async () => {
  let focus: { activity_id: string; version: string; last_turn_id: string } | null = {
    activity_id: 'activity-a', version: 'v1', last_turn_id: 'turn-before-restart',
  }
  const read = vi.spyOn(localApi, 'activityVoiceFocus').mockImplementation(async () => ({
    session_id: 'session-a', child_id: 'child-a', activity_focus: focus, local_only: true, cloud_transmission_allowed: false,
  }))
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(prompt)
  setVoiceQuestionCapture('session-a', 'child-a', { kind: 'diagnostic', binding: { prompt_id: 'original-prompt', item_version: 'item-v1' } })
  await mount()
  expect(voiceCaptureReference('session-a', 'child-a').activity_reference).toEqual(focus)
  vi.mocked(localApi.executeAgentTurn).mockImplementation(async () => {
    focus = null
    return { ...execution(), execution: { ...execution().execution, capability: 'companion.end_activity' } }
  })
  await say()
  expect(read.mock.calls.length).toBeGreaterThan(1)
  expect(voiceCaptureReference('session-a', 'child-a')).toEqual({ child_id: 'child-a',
    question_reference: { kind: 'diagnostic', binding: { prompt_id: 'original-prompt', item_version: 'item-v1' } } })
})

it('refreshes focus after two consecutive directly completed activity replies without inventing executions', async () => {
  let focus = { activity_id: 'activity-a', version: 'v1', last_turn_id: 'turn-1' }
  vi.spyOn(localApi, 'activityVoiceFocus').mockImplementation(async () => ({ session_id: 'session-a', child_id: 'child-a',
    activity_focus: focus, local_only: true, cloud_transmission_allowed: false }))
  await mount()
  let round = 1
  vi.mocked(localApi.agentTurn).mockImplementation(async () => {
    round++
    focus = { ...focus, version: `v${round}`, last_turn_id: `turn-${round}` }
    return { ...turn, intent: 'companion_activity_reply', runtime_capability: 'conversation.respond', runtime_execution_location: 'server',
      action: { type: 'none' }, tool_status: 'completed', response_text: '故事继续了。' }
  })
  await say()
  expect(voiceCaptureReference('session-a', 'child-a').activity_reference).toEqual(focus)
  const firstReplyCapture = voiceCaptureReference('session-a', 'child-a')
  await say()
  expect(voiceCaptureReference('session-a', 'child-a').activity_reference).toEqual({ activity_id: 'activity-a', version: 'v3', last_turn_id: 'turn-3' })
  expect(firstReplyCapture.activity_reference).toEqual({ activity_id: 'activity-a', version: 'v2', last_turn_id: 'turn-2' })
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(screen.getByText('故事继续了。')).toBeTruthy()
})

it('loads a scoped school listening question through original audio and playback receipts', async () => {
  current.plan_items = []
  prompt.prompt!.item = { ...prompt.prompt!.item, track: 'school', subject: '英语', answer_type: 'listening_text',
    pet_audio_modality: 'listening', stimulus_text: null }
  vi.spyOn(localApi, 'nextDiagnostic').mockResolvedValue({ item: prompt.prompt!.item, pet_baseline: null })
  await mount()
  await act(async () => {
    await new Promise<boolean>(resolve => window.dispatchEvent(new CustomEvent('hsc-study-learning-voice-action', {
      detail: { action: 'start_diagnostic', track: 'school', resolve },
    })))
  })
  await waitFor(() => expect(requestPetListeningAudio).toHaveBeenCalled())
  await waitFor(() => expect(localApi.petMediaPlayback).toHaveBeenCalledWith('session-a', expect.objectContaining({
    media_id: 'original-media', event: 'ended', audio_sha256: 'a'.repeat(64),
  })))
  expect(localApi.prepareDiagnosticPrompt).toHaveBeenCalledWith('session-a', expect.objectContaining({
    track: 'school', preferred_item_id: 'exact-item',
  }))
  expect(inputs.queueLocalPcm).toHaveBeenCalled()
  expect(spoken()).not.toContain(secret)
  expect(localApi.nextDiagnostic).toHaveBeenCalledTimes(1)
})

it('ignores an automatic original read arriving after a new task prompt update', async () => {
  const pending = deferred<DiagnosticPromptResult>()
  vi.spyOn(localApi, 'diagnosticPrompt').mockReturnValue(pending.promise)
  vi.spyOn(localApi, 'nextDiagnostic').mockResolvedValue({ item: prompt.prompt!.item, pet_baseline: null })
  await mount()
  const newer = structuredClone(prompt)
  newer.prompt!.prompt_id = 'newer-prompt'
  newer.prompt!.item.prompt = 'Newer exact question'
  await act(async () => {
    window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated', { detail: { sessionId: 'session-a', result: newer } }))
    pending.resolve(prompt)
    await pending.promise
  })
  expect(screen.getByText('Newer exact question')).toBeTruthy()
  expect(screen.queryByText('Where should they meet?')).toBeNull()
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
})

it('starts original audio from the returned task prompt and original turn, without preparing or speaking its transcript', async () => {
  await mount(); await say()
  expect(localApi.petPromptMedia).toHaveBeenCalledWith('session-a', 'original-prompt', 'item-v1')
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(inputs.queueLocalPcm).toHaveBeenCalledOnce()
  const reference = vi.mocked(inputs.queueLocalPcm).mock.calls[0][4]!.pcmControlReference!
  expect(reference).toMatchObject({ media_id: 'original-media', prompt_id: 'original-prompt', item_version: 'item-v1',
    generation: 1, source_version: 'original-source-v1', audio_sha256: 'a'.repeat(64) })
  expect(JSON.stringify(reference)).not.toContain(secret)
  expect(vi.mocked(localApi.petMediaPlayback).mock.calls.map(([, value]) => value.client_action_id))
    .toEqual(['original-turn:pet:started', 'original-turn:pet:ended'])
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toEqual({ kind: 'diagnostic',
    binding: { prompt_id: 'original-prompt', item_version: 'item-v1' } })
  expect(screen.getByText('Where should they meet?')).toBeTruthy()
  expect(spoken()).not.toContain(secret)
  expect(spoken()).toContain('原音播完了')
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
})

it.each(['missing', 'withdrawn', 'interrupted', 'lost_receipt'] as const)('keeps %s unqualified with safe recovery and never falls back to transcript TTS', async fault => {
  if (fault === 'missing') vi.mocked(localApi.petPromptMedia).mockImplementation(async () => ({
    prompt_id: 'original-prompt', item_version: 'item-v1', child_id: 'child-a', session_id: 'session-a',
    listening: [], speaking: [], selection_required: false, status: 'bound_media', local_only: true, cloud_transmission_allowed: false,
  }))
  if (fault === 'withdrawn') media.source_reviewed = false
  if (fault === 'interrupted') vi.mocked(inputs.queueLocalPcm).mockImplementation(async (_p, _r, _s, start) => { start?.(); return 'cancelled' })
  if (fault === 'lost_receipt') vi.mocked(localApi.petMediaPlayback).mockRejectedValue(new Error(secret))
  await mount(); await say()
  expect(spoken()).not.toContain(secret)
  expect(spoken()).toMatch(/原音|播放/)
  expect(spoken()).not.toContain('原音播完了')
  expect(history.size).toBe(0)
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
})

it('reuses the original completed playback receipt after remount, not another audio generation', async () => {
  const view = await mount(); await say()
  expect(inputs.queueLocalPcm).toHaveBeenCalledOnce()
  view.unmount()
  await mount(); await say()
  expect(inputs.queueLocalPcm).toHaveBeenCalledOnce()
  expect(localApi.petMediaControl).toHaveBeenCalledOnce()
  expect(spoken()).toContain('没有重复播放')
})

it('recovers an accepted ended receipt whose HTTP response was lost, without replaying audio or task side effects', async () => {
  const save = vi.mocked(localApi.petMediaPlayback).getMockImplementation()!
  vi.mocked(localApi.petMediaPlayback).mockImplementation(async (...args) => {
    const saved = await save(...args)
    if (args[1].event === 'ended') throw new Error('LOST_RESPONSE')
    return saved
  })
  const view = await mount(); await say()
  expect(history.has('original-turn:pet:ended')).toBe(true)
  expect(spoken()).toContain('播放尚未确认')
  view.unmount(); await mount(); await say()
  expect(inputs.queueLocalPcm).toHaveBeenCalledOnce()
  expect(localApi.petMediaControl).toHaveBeenCalledOnce()
  expect(spoken()).toContain('没有重复播放')
  expect(spoken()).not.toContain(secret)
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
})

it.each(['current_source_invalid', 'await_recovery'] as const)('honors persisted %s rather than replaying the task listening request', async decision => {
  vi.mocked(localApi.petPlaybackActionReceipt).mockImplementation(async (_sid, _mid, key) => ({
    child_id: 'child-a', session_id: 'session-a', media_id: media.media_id, client_action_id: key,
    receipt: null, current: structuredClone(media), automatic_playback_allowed: false,
    replay_decision: decision, local_only: true, cloud_transmission_allowed: false,
  }))
  await mount(); await say()
  expect(inputs.queueLocalPcm).not.toHaveBeenCalled()
  expect(localApi.petMediaControl).not.toHaveBeenCalled()
  expect(spoken()).toMatch(/已经开始|原题材料已变化/)
  expect(spoken()).not.toContain(secret)
})

it('settles only a durable pending receipt, never treats the task receipt as listening qualification', async () => {
  media.status = 'pending_receipt'
  media.receipt_retry_after_ms = 0
  media.pending_playback_receipt = { media_id: media.media_id, generation: 0, expected_revision: 0,
    client_action_id: 'original-turn:pet:ended', event: 'ended', audio_sha256: media.audio.sha256!,
    frame_position: media.audio.frames!, monotonic_ms: 2000 }
  await mount(); await say()
  expect(localApi.petMediaPlayback).toHaveBeenCalledWith('session-a', media.pending_playback_receipt)
  expect(inputs.queueLocalPcm).not.toHaveBeenCalled()
  expect(localApi.petMediaControl).not.toHaveBeenCalled()
  expect(spoken()).toContain('未重复播放')
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})

it('does not play or certify a saved reference answer returned by task execution', async () => {
  prompt.status = 'answer_saved'
  await mount(); await say()
  expect(localApi.petPromptMedia).not.toHaveBeenCalled()
  expect(spoken()).toContain('不重复播放或计算听力成绩')
  expect(spoken()).not.toContain(secret)
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})

it.each(['reading', 'listening'] as const)('keeps %s awaiting task start unanswerable and clears only its original capture', async skill => {
  if (skill === 'reading') { prompt.prompt!.item.answer_type = 'short_text'; prompt.prompt!.item.pet_audio_modality = null }
  const original = structuredClone(prompt)
  prompt.status = 'awaiting_task_start'
  prompt.reason = 'scheduled_pet_task_not_started'
  await mount()
  await act(async () => {
    setVoiceQuestionCapture('session-a', 'child-a', { kind: 'diagnostic', binding: { prompt_id: 'original-prompt', item_version: 'item-v1' } })
    window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated', { detail: { sessionId: 'session-a', result: original } }))
  })
  await say()
  expect(localApi.petPromptMedia).not.toHaveBeenCalled()
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toBeNull()
  expect(screen.queryByText('Where should they meet?')).toBeNull()
  expect(spoken()).toMatch(/继续.*原任务/)
  expect(spoken()).not.toContain(secret)
})

it('preserves the actual resource-blocked response instead of asking to resume the missing-audio task again', async () => {
  prompt.status = 'awaiting_task_start'
  prompt.reason = 'scheduled_pet_task_not_started'
  current.plan_items[0].status = 'pending'
  current.plan_items[0].started_at = null
  const response = '这道听力题缺少已审核原音，请家长补齐原音后再继续；尚未开始计时。'
  vi.mocked(localApi.executeAgentTurn).mockImplementation(async () => ({
    turn: { ...turn, response_text: response },
    execution: { capability: 'task.start', response_text: response, evidence: [],
      result: { status: 'awaiting_resources', reason: 'PET_LISTENING_RESOURCE_UNAVAILABLE', diagnostic_prompt: prompt } },
  }))
  await mount(); await say()
  expect(screen.getByText(response)).toBeTruthy()
  expect(spoken()).toContain(response)
  expect(spoken()).not.toContain('继续原任务')
  expect(spoken()).not.toContain(secret)
  expect(localApi.petPromptMedia).not.toHaveBeenCalled()
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(inputs.queueLocalPcm).not.toHaveBeenCalled()
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toBeNull()
  expect(current.plan_items[0]).toMatchObject({ status: 'pending', started_at: null })
})

it('does not clear a different displayed question/capture when an awaiting task receipt arrives', async () => {
  const unrelated = structuredClone(prompt)
  unrelated.prompt!.prompt_id = 'other-prompt'
  unrelated.prompt!.diagnostic_item_id = 'other-item'
  unrelated.prompt!.item.diagnostic_item_id = 'other-item'
  unrelated.prompt!.item.prompt = 'Other current question'
  prompt.status = 'awaiting_task_start'
  await mount()
  await act(async () => {
    setVoiceQuestionCapture('session-a', 'child-a', { kind: 'diagnostic', binding: { prompt_id: 'other-prompt', item_version: 'item-v1' } })
    window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated', { detail: { sessionId: 'session-a', result: unrelated } }))
  })
  await say()
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toMatchObject({ binding: { prompt_id: 'other-prompt' } })
  expect(screen.getByText('Other current question')).toBeTruthy()
  expect(localApi.petPromptMedia).not.toHaveBeenCalled()
})

it('recovered GET remains awaiting through voice replay, then a new task start binds that same original', async () => {
  const awaiting: DiagnosticPromptResult = { ...prompt, status: 'awaiting_task_start', plan_item_id: 'pet-task',
    reason: 'scheduled_pet_task_not_started' }
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(awaiting)
  const replay: AgentTurn = { ...turn, runtime_capability: undefined, runtime_execution_location: undefined,
    action: { type: 'replay_diagnostic' } }
  vi.mocked(localApi.agentTurn).mockResolvedValueOnce(replay).mockImplementation(async () => turn)
  await mount(); await say()
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.petPromptMedia).not.toHaveBeenCalled()
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toBeNull()
  expect(spoken()).toMatch(/继续.*原任务/)
  await say()
  expect(localApi.executeAgentTurn).toHaveBeenCalledOnce()
  expect(localApi.petPromptMedia).toHaveBeenCalledWith('session-a', 'original-prompt', 'item-v1')
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toMatchObject({ binding: { prompt_id: 'original-prompt', item_version: 'item-v1' } })
})

it('awaiting event cannot hide a different prompt of the same item', async () => {
  const other = structuredClone(prompt)
  other.prompt!.prompt_id = 'other-prompt'
  await mount()
  await act(async () => {
    window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated', { detail: { sessionId: 'session-a', result: other } }))
    window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated', { detail: { sessionId: 'session-a', result: { ...prompt, status: 'awaiting_task_start' } } }))
  })
  expect(screen.getByText('Where should they meet?')).toBeTruthy()
  expect(localApi.petPromptMedia).not.toHaveBeenCalled()
})

it.each(['reading', 'writing'] as const)('preserves %s task question capture without listening playback', async kind => {
  prompt.prompt!.item.answer_type = kind === 'reading' ? 'short_text' : 'writing'
  prompt.prompt!.item.pet_audio_modality = null
  turn.response_text = '请回答当前原题。'
  await mount(); await say()
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toMatchObject({ kind: 'diagnostic', binding: { prompt_id: 'original-prompt' } })
  expect(localApi.petPromptMedia).not.toHaveBeenCalled()
  expect(spoken()).toContain('请回答当前原题')
})

it('consumes unmodified real oral retry turn and execution packets', async () => {
  const command = oralPackets.packets[13]
  const completed = oralPackets.packets[14]
  expect(command.path.endsWith('/agent-turn')).toBe(true)
  expect(completed.path.endsWith('/execute')).toBe(true)
  turn = structuredClone(command.response) as unknown as AgentTurn
  const response = structuredClone(completed.response) as unknown as Awaited<ReturnType<typeof localApi.executeAgentTurn>>
  current = workspace(turn.child_id!, turn.session_id!)
  current.plan = null
  current.plan_items = []
  vi.mocked(localApi.executeAgentTurn).mockResolvedValueOnce(response)
  await mount()
  await act(async () => { await microphone.onTranscript(turn.transcript) })
  const result = response.execution.result as DiagnosticPromptResult
  expect(localApi.executeAgentTurn).toHaveBeenCalledExactlyOnceWith(turn.agent_turn_id)
  expect(voiceCaptureReference(turn.session_id!, turn.child_id!).question_reference).toMatchObject({
    kind: 'diagnostic', binding: { prompt_id: result.prompt!.prompt_id, item_version: result.prompt!.item_version },
  })
  expect(spoken()).toContain(response.execution.response_text)
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  expect(requestPetListeningAudio).not.toHaveBeenCalled()
})

it.each(['oral.retry', 'oral.independent_check'])('binds the actual %s prompt to the next spoken answer', async capability => {
  prompt.prompt!.item.answer_type = 'speaking'
  prompt.prompt!.item.pet_audio_modality = null
  prompt.prompt!.prompt_id = 'new-oral-prompt'
  turn.runtime_capability = capability
  turn.action.type = capability === 'oral.retry' ? 'retry_oral_answer' : 'check_oral_independently'
  turn.action.capability = capability
  turn.response_text = '请看这道口语题，准备好后说出你的回答。'
  vi.mocked(localApi.executeAgentTurn).mockResolvedValueOnce({ turn, execution: {
    capability, response_text: turn.response_text, result: { ...prompt, local_only: true, cloud_transmission_allowed: false }, evidence: [],
  } } as never)
  await mount(); await say()
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toMatchObject({ kind: 'diagnostic', binding: { prompt_id: 'new-oral-prompt' } })
  expect(localApi.petPromptMedia).not.toHaveBeenCalled()
  expect(spoken()).toContain('请看这道口语题')
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
})

it.each(['oral.retry', 'oral.independent_check'])('does not speak or create another question from a replayed %s receipt', async capability => {
  prompt.prompt!.item.answer_type = 'speaking'
  prompt.prompt!.item.pet_audio_modality = null
  turn.runtime_capability = capability
  turn.action.type = capability === 'oral.retry' ? 'retry_oral_answer' : 'check_oral_independently'
  turn.action.capability = capability
  turn.response_text = '原来的记录已保存，这次没有重复出题。'
  vi.mocked(localApi.executeAgentTurn).mockResolvedValueOnce({ turn, execution: {
    capability, response_text: turn.response_text, replayed: true, recorded_receipt: true, replay_audio_allowed: false,
    result: { ...prompt, local_only: true, cloud_transmission_allowed: false }, evidence: [],
  } } as never)
  await mount(); await say()
  expect(spoken()).not.toContain(turn.response_text)
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.petPromptMedia).not.toHaveBeenCalled()
})

it('rejects a crossed-child task prompt before display or audio', async () => {
  prompt.prompt!.child_id = 'child-b'
  await mount(); await say()
  expect(localApi.petPromptMedia).not.toHaveBeenCalled()
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toBeNull()
  expect(screen.queryByText('Where should they meet?')).toBeNull()
  expect(spoken()).not.toContain(secret)
})

it.each(['execution', 'media'] as const)('ignores a late %s response after switching to another child', async phase => {
  const receipt = execution()
  const delayedExecution = deferred<ReturnType<typeof execution>>()
  const delayedMedia = deferred<Awaited<ReturnType<typeof localApi.petPromptMedia>>>()
  if (phase === 'execution') vi.mocked(localApi.executeAgentTurn).mockReturnValueOnce(delayedExecution.promise)
  else vi.mocked(localApi.petPromptMedia).mockReturnValueOnce(delayedMedia.promise)
  const view = await mount()
  let command!: Promise<unknown>
  await act(async () => { command = Promise.resolve(microphone.onTranscript('开始这项任务')) })
  await vi.waitFor(() => expect(phase === 'execution' ? localApi.executeAgentTurn : localApi.petPromptMedia).toHaveBeenCalled())
  view.unmount(); current = workspace('child-b', 'session-b')
  await mount()
  await act(async () => {
    if (phase === 'execution') delayedExecution.resolve(receipt)
    else delayedMedia.resolve({ prompt_id: 'original-prompt', item_version: 'item-v1', child_id: 'child-a', session_id: 'session-a',
      listening: [media], speaking: [], selection_required: false, status: 'bound_media', local_only: true, cloud_transmission_allowed: false })
    await command
  })
  expect(inputs.queueLocalPcm).not.toHaveBeenCalled()
  expect(localApi.petMediaControl).not.toHaveBeenCalled()
  expect(voiceCaptureReference('session-b', 'child-b').question_reference).toBeNull()
  expect(screen.queryByText('Where should they meet?')).toBeNull()
  expect(spoken()).not.toContain(secret)
})

it('does not bind or play a task execution that arrives after the same workspace privacy context was stopped', async () => {
  const delayed = deferred<ReturnType<typeof execution>>()
  vi.mocked(localApi.executeAgentTurn).mockReturnValueOnce(delayed.promise)
  await mount()
  let command!: Promise<unknown>
  await act(async () => { command = Promise.resolve(microphone.onTranscript('开始这项任务')) })
  await vi.waitFor(() => expect(localApi.executeAgentTurn).toHaveBeenCalled())
  await act(async () => {
    window.dispatchEvent(new Event('hsc-privacy-pause'))
    delayed.resolve(execution())
    await command
  })
  expect(localApi.petPromptMedia).not.toHaveBeenCalled()
  expect(screen.queryByText('Where should they meet?')).toBeNull()
  expect(voiceCaptureReference('session-a', 'child-a').question_reference).toBeNull()
  expect(spoken()).not.toContain(secret)
})
