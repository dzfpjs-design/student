import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as LocalInputs from './LocalInputs'
import * as voice from './companionVoice'
import type { AgentTurn, SessionWorkspaceState, VoiceInteractionResponse, VoiceInteractionRecovery, VoiceInteractionHandoff, VoiceInteractionRequest } from './types'
import type { VoiceOnboardingProgress } from './voiceOnboardingRuntime'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import { saveAgentRequest, AGENT_REQUEST_UNCONFIRMED } from './agentRequestJournal'
import * as ordinaryResponse from './ordinaryResponseGrant'

let microphone: ComponentProps<typeof LocalInputs.HandsFreeVoiceInput>
let realMicrophone = false
const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
vi.mock('./LocalInputs', async (original) => {
  const actual = await original<typeof import('./LocalInputs')>()
  return { ...actual, HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return realMicrophone ? <actual.HandsFreeVoiceInput {...props} /> : null }, VoiceInputButton: () => null }
})
vi.mock('./VoiceCalibrationPanel', async () => {
  const { useEffect } = await import('react')
  return { VoiceCalibrationPanel: ({ onMicrophoneBindingReadyChange }: { onMicrophoneBindingReadyChange: (ready: boolean) => void }) => {
    useEffect(() => { onMicrophoneBindingReadyChange(true) }, [onMicrophoneBindingReadyChange])
    return null
  } }
})

function workspace(childId = 'child-a'): SessionWorkspaceState {
  return {
    session: { session_id: `session-${childId}`, child_id: childId, status: 'active', state: 'check_in', observation_status: 'paused', snooze_until: null },
    planning_context: { energy_level: 'medium', fixed_commitment_minutes: 0, fixed_commitment_note: null, tomorrow_event_subject: null, tomorrow_event_title: null, tomorrow_event_minutes: 0 },
    family_rule: { observation_mode: 'shadow' }, recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [],
    plan: null, plan_items: [], drafts: {}, latest_tutor_turn: null,
    sensor_release: { opened_leases: 0, active_leases: 0, released_leases: 0, by_device: {} }, daily_report_ready: false,
  } as unknown as SessionWorkspaceState
}
const progress = (step: VoiceOnboardingProgress['step'], childId = 'child-a'): VoiceOnboardingProgress => ({ childId, version: 1, step, updatedAt: '2026-09-05T12:00:00Z' })
const cloudReply = (action: VoiceInteractionResponse['action'] = 'none', values: Partial<VoiceInteractionResponse> = {}): VoiceInteractionResponse => ({
  route: action === 'none' ? 'clarify' : 'onboarding', action, source: 'cloud', response_text: '我们慢慢来。', ...values,
})
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('voice onboarding in the workspace', () => {
  let names: Map<string, DesktopWakeWordSettings>
  let records: Map<string, VoiceOnboardingProgress>
  let desktop: { getWakeWordSettings: ReturnType<typeof vi.fn>; saveWakeWordSettings: ReturnType<typeof vi.fn>; getVoiceOnboarding: ReturnType<typeof vi.fn>; saveVoiceOnboarding: ReturnType<typeof vi.fn> }
  const setName = (childId = 'child-a') => names.set(childId, { childId, wakeWord: '小知', enabled: true, inactivitySeconds: 60, updatedAt: '2026-09-05T12:00:00Z' })
  const say = async (text: string) => { await act(async () => { await microphone.onTranscript(text) }) }
  const wake = async () => { await act(async () => { microphone.onWake?.() }) }
  const respond = (action: VoiceInteractionResponse['action'], values: Partial<VoiceInteractionResponse> = {}) => {
    vi.mocked(localApi.voiceInteraction).mockResolvedValueOnce(cloudReply(action, values))
  }
  const candidate = (name = '小知') => respond('name_candidate', { candidate: name, response_text: `我听到的唤醒名字是“${name}”。这样叫我好吗？` })
  const resumeTutorial = () => {
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce({
      agent_turn_id: 'resume-tutorial', response_text: '云端规划已选择重新练习。', intent: 'resume_voice_tutorial',
      runtime_capability: 'onboarding.resume', runtime_execution_location: 'client_device_bridge',
      action: { type: 'resume_voice_tutorial', capability: 'onboarding.resume', execution_location: 'client_device_bridge', source: 'agent_runtime_cloud' },
    } as AgentTurn)
  }
  async function mount(props: Partial<ComponentProps<typeof SessionWorkspace>> = {}) {
    let view!: ReturnType<typeof render>
    await act(async () => { view = render(<SessionWorkspace initial={workspace()} childName="小辰" childGrade="初三" onBack={vi.fn()} {...props} />) })
    return view
  }

  beforeEach(() => {
    vi.spyOn(localApi, 'agentExecutionRecoveryIndex').mockImplementation(async (session, child) => ({
      session_id: session, child_id: child, local_only: true, cloud_transmission_allowed: false,
      replay_allowed: false, has_more: false, entries: [] }))
    realMicrophone = false
    names = new Map()
    records = new Map()
    desktop = {
      getWakeWordSettings: vi.fn(async (id: string) => names.get(id) ?? null),
      saveWakeWordSettings: vi.fn(async (value: DesktopWakeWordSettings) => { names.set(value.childId, value); return value }),
      getVoiceOnboarding: vi.fn(async (id: string) => records.get(id) ?? null),
      saveVoiceOnboarding: vi.fn(async (value: VoiceOnboardingProgress) => { records.set(value.childId, value); return value }),
    }
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop(desktop))
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
    vi.spyOn(voice, 'getCompanionVoiceState').mockReturnValue('idle')
    vi.spyOn(LocalInputs, 'speakLocally').mockImplementation((_text, _session, done) => { done?.('text_only') })
    vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new Error('PRACTICE_MUST_NOT_REACH_AGENT'))
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    vi.spyOn(localApi, 'voiceInteraction').mockResolvedValue(cloudReply())
    vi.spyOn(localApi, 'voiceInteractionRecovery').mockImplementation(async (session, child) => ({ session_id: session, child_id: child, status: 'none', original_action_key: null, request: null, response: null, reason: null }))
    vi.spyOn(localApi, 'retryVoiceInteraction')
    const noHandoff = (session: string, child: string): VoiceInteractionHandoff => ({ session_id: session, child_id: child,
      status: 'none', handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null })
    let handoff: VoiceInteractionHandoff | null = null
    let handoffSource: VoiceInteractionRequest | null = null
    vi.spyOn(localApi, 'readVoiceInteractionHandoff').mockImplementation(async (session, child) => {
      if (!handoff || handoff.session_id !== session || handoff.child_id !== child) return noHandoff(session, child)
      if (handoff.status === 'needs_deferral' && records.get(child)?.updatedAt === handoff.deferred_progress?.updatedAt
        && records.get(child)?.step === 'deferred') return { ...handoff, status: 'ready', request: handoffSource }
      return handoff
    })
    vi.spyOn(localApi, 'invalidateVoiceInteractionHandoff').mockImplementation(async (session, child) => {
      handoff = null; handoffSource = null; return noHandoff(session, child)
    })
    vi.spyOn(localApi, 'prepareVoiceInteractionHandoff').mockImplementation(async (session, child, key) => {
      const retry = vi.mocked(localApi.retryVoiceInteraction).mock.results.at(-1)?.value
      const restored = retry ? await retry : null
      handoffSource = restored?.original_action_key === key ? restored.request
        : vi.mocked(localApi.voiceInteraction).mock.calls.at(-1)?.[1] ?? null
      handoff = { ...noHandoff(session, child), status: 'needs_deferral', handoff_id: `voice-interaction-handoff:${'a'.repeat(64)}`, agent_action_id: 'handoff-agent-one',
        deferred_progress: { childId: child, version: 1, step: 'deferred', updatedAt: '2026-10-15T03:00:00.000Z' } }
      return handoff
    })
    vi.spyOn(localApi, 'dispatchVoiceInteractionHandoff').mockImplementation(async (session) => {
      const request = handoffSource!
      handoff = { ...handoff!, status: 'received', request: null }
      return request.voice_input_id ? localApi.agentTurn(session, request.transcript, undefined, request.voice_input_id)
        : localApi.agentTurn(session, request.transcript)
    })
    vi.spyOn(localApi, 'askTutor')
    vi.spyOn(localApi, 'addHomework')
    vi.spyOn(localApi, 'startDocumentMonitor')
    vi.spyOn(localApi, 'registerDevice')
    vi.spyOn(localApi, 'renewDeviceLease').mockImplementation(async leaseId => ({ lease_id: leaseId, status: 'active', expires_at: new Date(Date.now() + 180_000).toISOString() }))
    vi.spyOn(localApi, 'saveSessionDraft')
  })
  afterEach(() => {
    cleanup(); localStorage.clear(); resetVoiceCaptureCoordinatorForTests(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers()
    if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
    else Reflect.deleteProperty(navigator, 'mediaDevices')
  })

  it('checks an original receipt before naming/tutorial cloud routing', async () => {
    saveAgentRequest('session-child-a', 'original-lost-key')
    const read = vi.spyOn(localApi, 'agentRequestReceipt').mockResolvedValue({
      session_id: 'session-child-a', client_action_id: 'original-lost-key', status: 'interpreted',
      response_text: '原请求有记录，但后续操作还没有确认。', local_only: true,
      cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false,
    })
    await mount()
    vi.mocked(localApi.voiceInteraction).mockClear()
    await say('核对刚才请求')
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
      expect(read).toHaveBeenCalledWith('session-child-a', 'original-lost-key', expect.any(AbortSignal))
    expect(localApi.agentTurn).not.toHaveBeenCalled()
  })

  it('changes an existing wake name through a cloud action and confirmation, preserving it on reload', async () => {
    setName(); records.set('child-a', progress('completed'))
    vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'rename', response_text: '云端不应声称已保存',
      action: { type: 'configure_wake_word', candidate: '小枝小枝' }, runtime_capability: 'voice.configure_wake_word', runtime_execution_location: 'client_device_bridge' } as AgentTurn)
    const view = await mount()
    await say('把唤醒词改为小枝小枝')
    expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith('rename', expect.any(String), 'completed', undefined,
      expect.objectContaining({ client_bridge_receipt: expect.objectContaining({ output: { configuration_started: true, phase: 'confirm_name', wake_word_saved: false } }) }))
    respond('reject_name')
    await say('不是这个，我还没有决定')
    expect(names.get('child-a')?.wakeWord).toBe('小知')
    respond('name_candidate', { candidate: '小芽同学' })
    await say('叫小芽同学怎么样')
    respond('confirm_name')
    await say('就用刚才这个名字')
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledTimes(1)
    expect(microphone.wakeWord).toBe('小芽同学')
    expect(records.get('child-a')?.step).toBe('completed')
    expect(LocalInputs.speakLocally).toHaveBeenCalledWith(expect.stringContaining('小芽同学'), expect.anything(), expect.anything(), expect.any(Function), expect.objectContaining({ localOnly: true, onPlaybackEnd: expect.any(Function) }))
    view.unmount(); await mount()
    expect(microphone.wakeWord).toBe('小芽同学')
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledTimes(1)
  })

  it('keeps the old wake word when daily rename persistence fails and does not announce success', async () => {
    setName(); records.set('child-a', progress('deferred'))
    vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'rename', response_text: '已修改', action: { type: 'configure_wake_word', candidate: '小枝小枝' }, runtime_capability: 'voice.configure_wake_word', runtime_execution_location: 'client_device_bridge' } as AgentTurn)
    await mount(); await say('以后叫你小枝小枝吧')
    desktop.saveWakeWordSettings.mockRejectedValue(new Error('disk unavailable'))
    vi.mocked(LocalInputs.speakLocally).mockClear()
    respond('confirm_name'); await say('对，就叫这个')
    expect(names.get('child-a')?.wakeWord).toBe('小知')
    expect(LocalInputs.speakLocally).not.toHaveBeenCalledWith(expect.stringMatching(/已改为|已修改/), expect.anything(), expect.anything(), expect.anything(), expect.anything())
    expect(screen.getByText(/名字还没有保存成功/)).toBeTruthy()
  })

  it('routes playback-overlap evidence directly to agent during naming and preserves metadata through the queue', async () => {
    const first = deferred<AgentTurn>()
    vi.mocked(localApi.agentTurn).mockReturnValueOnce(first.promise).mockResolvedValue({ agent_turn_id: 'echo-second', response_text: '', action: { type: 'none', suppress_speech: true } } as AgentTurn)
    await mount()
    vi.mocked(LocalInputs.speakLocally).mockClear()
    let a!: Promise<void>; let b!: Promise<void>
    await act(async () => {
      a = Promise.resolve(microphone.onTranscript('请先给我起一个短名字，确认以后我才会保存这个名字', { playback_id: 'played-first' }))
      b = Promise.resolve(microphone.onTranscript('当前会话还没有可核对的页面记录，暂时不能判断你在写什么', { playback_id: 'played-second' }))
    })
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    await act(async () => {
      first.resolve({ agent_turn_id: 'echo-first', response_text: '', action: { type: 'none', suppress_speech: true } } as AgentTurn)
      await a; await b
    })
    expect(localApi.agentTurn).toHaveBeenCalledTimes(2)
    expect(localApi.agentTurn).toHaveBeenNthCalledWith(1, 'session-child-a', expect.stringContaining('起一个短名字'), 'played-first')
    expect(localApi.agentTurn).toHaveBeenNthCalledWith(2, 'session-child-a', expect.stringContaining('页面记录'), 'played-second')
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    expect(LocalInputs.speakLocally).not.toHaveBeenCalled()
  })

  it('does not fabricate checklist completion when the legacy action lacks runtime evidence', async () => {
    setName(); records.set('child-a', progress('completed'))
    vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'legacy-complete', action: { type: 'complete_active_task', completion_evidence_type: 'child_checked' }, response_text: '全部检查好了' } as AgentTurn)
    const complete = vi.spyOn(localApi, 'completePlanItem')
    await mount(); await say('我说做完了不是说已经检查过了')
    expect(complete).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith('legacy-complete', expect.any(String), 'failed', 'CLIENT_VOICE_ACTION_UNAVAILABLE')
  })

  function recordingHarness() {
    realMicrophone = true
    vi.useFakeTimers()
    const processors: Array<{ connect: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn>; onaudioprocess: ((event: AudioProcessingEvent) => void) | null }> = []
    class FakeAudioContext {
      sampleRate = 16_000
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createScriptProcessor = () => {
        const processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as ((event: AudioProcessingEvent) => void) | null }
        processors.push(processor)
        return processor
      }
      close = vi.fn().mockResolvedValue(undefined)
    }
    vi.stubGlobal('AudioContext', FakeAudioContext)
    const stop = vi.fn()
    const track = { stop, getSettings: () => ({}), getCapabilities: () => ({}) }
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia, enumerateDevices: vi.fn().mockResolvedValue([]) } })
    vi.mocked(localApi.registerDevice).mockResolvedValue({ lease_id: 'tutorial-mic', status: 'active', expires_at: 'later' })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'tutorial-mic', status: 'released' })
    const capture = async () => {
      for (let index = 0; index < 4; index += 1) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(256)
          processors.at(-1)?.onaudioprocess?.({ inputBuffer: { getChannelData: () => new Float32Array(4096).fill(0.05) } } as unknown as AudioProcessingEvent)
        })
      }
      await act(async () => { await vi.advanceTimersByTimeAsync(1_200) })
    }
    return { capture, getUserMedia, stop }
  }

  it('keeps narration out of ASR, then delivers a no-wake privacy stop after the acoustic tail', async () => {
    const harness = recordingHarness()
    setName()
    records.set('child-a', progress('wake_practice'))
    let voiceState: voice.CompanionVoiceState = 'speaking'
    vi.mocked(voice.getCompanionVoiceState).mockImplementation(() => voiceState)
    const cancel = vi.spyOn(voice, 'stopCompanionSpeech')
    const pending = deferred<Awaited<ReturnType<typeof localApi.transcribe>>>()
    const asr = vi.spyOn(localApi, 'transcribe').mockReturnValueOnce(pending.promise)
    await mount()
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(harness.getUserMedia).toHaveBeenCalled()
    expect(cancel).not.toHaveBeenCalled()
    await harness.capture()
    expect(asr).not.toHaveBeenCalled()
    expect(cancel).not.toHaveBeenCalled()
    expect(records.get('child-a')?.step).toBe('wake_practice')
    voiceState = 'idle'
    await act(async () => { await vi.advanceTimersByTimeAsync(LocalInputs.companionPlaybackCaptureCooldownMs + 1) })
    pending.resolve({ transcript: '停止全部监听', status: 'completed', cloud_used: true } as Awaited<ReturnType<typeof localApi.transcribe>>)
    await harness.capture()
    expect(asr).toHaveBeenCalledTimes(1)
    expect(cancel).toHaveBeenCalled()
    expect(harness.stop).toHaveBeenCalled()
    expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('tutorial-mic', expect.any(String))
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
  })

  it.each(['小知', '停止全部监听'])('uses actual tutorial ASR for %s, with privacy independent of cloud routing', async (transcript) => {
    const harness = recordingHarness()
    setName()
    records.set('child-a', progress('wake_practice'))
    const asr = vi.spyOn(localApi, 'transcribeStandby').mockResolvedValue({ client_action_id: 'wake-practice', transcript, duration_ms: 320, model_version: 'standby-local', status: 'completed', persisted: false, cloud_used: false })
    await mount()
    // Registration includes asynchronous device hashing. Wait for the actual
    // recorder-ready state rather than assuming one fake millisecond is enough.
    await vi.waitFor(async () => {
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(screen.getByRole('button', { name: '正在听唤醒词 · 小知' })).toBeTruthy()
    }, { timeout: 2_000, interval: 20 })
    expect(microphone.wakeWord).toBe('小知')
    await harness.capture()
    expect(asr).toHaveBeenCalledTimes(1)
    expect(records.get('child-a')?.step).toBe(transcript === '小知' ? 'study_practice' : 'wake_practice')
    if (transcript !== '小知') {
      expect(microphone.autoStart).toBe(false)
      expect(harness.stop).toHaveBeenCalled()
      expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('tutorial-mic', expect.any(String))
      expect(localApi.voiceInteraction).not.toHaveBeenCalled()
      expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
    } else expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
  })

  it('confirms and saves a name, validates a real wake callback, and practices without real tasks or devices', async () => {
    await mount()
    candidate()
    await say('小知')
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    respond('confirm_name')
    await say('我觉得这个名字挺好')
    expect(vi.mocked(localApi.voiceInteraction).mock.calls[1][1].context).toEqual({ phase: 'confirm_name', pending_name: '小知' })
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledTimes(1)
    expect(records.get('child-a')?.step).toBe('wake_practice')
    expect(microphone).toMatchObject({ wakeWord: '小知', autoStart: true, externalActivationSignal: 0, standbyOnWakeWordEnable: true })
    await say('小知')
    expect(records.get('child-a')?.step).toBe('wake_practice')
    await wake()
    expect(records.get('child-a')?.step).toBe('study_practice')
    respond('advance')
    await say('数学今天讲了分数，老师留了三道练习')
    respond('advance')
    await say('这道题我不太懂，可以帮我看看吗')
    respond('advance')
    await say('今天就先学到这里吧')
    expect(records.get('child-a')?.step).toBe('completed')
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(localApi.askTutor).not.toHaveBeenCalled()
    expect(localApi.addHomework).not.toHaveBeenCalled()
    expect(localApi.registerDevice).not.toHaveBeenCalled()
    expect(localApi.startDocumentMonitor).not.toHaveBeenCalled()
    expect(vi.mocked(LocalInputs.speakLocally).mock.calls.some(([text]) => text.startsWith('欢迎回家'))).toBe(false)
    expect(vi.mocked(localApi.saveSessionDraft).mock.calls.some(([, section]) => section === 'agent_command')).toBe(false)
    const calls = vi.mocked(LocalInputs.speakLocally).mock.calls
    expect(calls.find(([text]) => text.startsWith('我听到的唤醒名字'))?.[4]).toEqual({ localOnly: true, speechKey: expect.stringMatching(/^onboarding-response:session-child-a:\d+$/), onPlaybackEnd: expect.any(Function), onNotDelivered: expect.any(Function) })
    expect(calls.find(([text]) => text.startsWith('名字“小知”'))?.[4]).toEqual({ localOnly: true, speechKey: expect.stringMatching(/^onboarding-response:session-child-a:\d+$/), onPlaybackEnd: expect.any(Function), onNotDelivered: expect.any(Function) })
    expect(calls.find(([text]) => text.startsWith('听到你叫我了'))?.[4]).toEqual({ localOnly: false, speechKey: expect.stringMatching(/^onboarding-response:session-child-a:\d+$/), onPlaybackEnd: expect.any(Function), onNotDelivered: expect.any(Function) })
    expect(calls.find(([text]) => text.startsWith('说清楚了'))?.[4]).toEqual({ localOnly: false, speechKey: expect.stringMatching(/^onboarding-response:session-child-a:\d+$/), onPlaybackEnd: expect.any(Function), onNotDelivered: expect.any(Function) })
  })

  it('does not arm wake detection until the tutorial prompt has actually finished playing', async () => {
    setName()
    const listeners = new Set<(state: voice.CompanionVoiceState) => void>()
    vi.spyOn(voice, 'subscribeCompanionVoice').mockImplementation((listener) => { listeners.add(listener); return () => { listeners.delete(listener) } })
    vi.mocked(voice.getCompanionVoiceState).mockReturnValue('speaking')
    await mount()
    expect(microphone.autoStart).toBe(true)
    expect(microphone.wakeWord).toBeUndefined()
    await wake()
    expect(records.get('child-a')?.step).toBe('wake_practice')
    await say('小知')
    expect(records.get('child-a')?.step).toBe('wake_practice')
    await act(async () => {
      vi.mocked(voice.getCompanionVoiceState).mockReturnValue('idle')
      listeners.forEach((listener) => listener('idle'))
    })
    expect(microphone).toMatchObject({ autoStart: true, wakeWord: '小知', externalActivationSignal: 0, standbyOnWakeWordEnable: true })
    await wake()
    expect(records.get('child-a')?.step).toBe('study_practice')
  })

  it('keeps narration speech for cloud clarification without treating it as a rehearsal success, and preempts for privacy', async () => {
    setName()
    records.set('child-a', progress('study_practice'))
    vi.mocked(voice.getCompanionVoiceState).mockReturnValue('speaking')
    const cancel = vi.spyOn(LocalInputs, 'cancelLocalSpeech')
    await mount()
    expect(microphone.autoStart).toBe(true)
    await say('今天数学学了分数，作业是做三道题')
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    expect(records.get('child-a')?.step).toBe('study_practice')
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
    await say('停止全部监听')
    expect(cancel).toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(microphone.autoStart).toBe(false)
  })

  it('does not advance or double-submit until a successful progress save, and supports voice retry', async () => {
    setName()
    records.set('child-a', progress('wake_practice'))
    await mount()
    const saved = deferred<VoiceOnboardingProgress>()
    desktop.saveVoiceOnboarding.mockImplementationOnce(async () => {
      const receipt = await saved.promise
      records.set(receipt.childId, receipt)
      return receipt
    })
    await wake()
    await wake()
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/听到你叫我了/)).toBeNull()
    await act(async () => { saved.resolve(progress('study_practice')) })
    desktop.saveVoiceOnboarding.mockRejectedValueOnce(new Error('disk full'))
    respond('advance')
    await say('今天数学学了分数，作业是做三道题')
    expect(screen.getByText(/当前设置中需要重试|还没有确认完成/)).toBeTruthy()
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('今天数学学了分数，作业是做三道题')
    expect(records.get('child-a')?.step).toBe('study_practice')
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    const attempts = desktop.saveVoiceOnboarding.mock.calls.length
    respond('advance')
    await say('请再试着保存刚才那一步')
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledTimes(attempts + 1)
    expect(records.get('child-a')?.step).toBe('document_practice')
  })

  it('keeps legacy names, resumes progress after reload and supports defer and restart', async () => {
    setName()
    records.set('child-a', progress('document_practice'))
    const first = await mount()
    expect(screen.getByText(/再练习说/)).toBeTruthy()
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    respond('skip')
    await say('以后再学')
    expect(records.get('child-a')?.step).toBe('deferred')
    first.unmount()
    await mount()
    resumeTutorial()
    await say('重新教程')
    expect(records.get('child-a')?.step).toBe('wake_practice')
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
  })

  it.each([true, false])('executes a natural cloud restart only after durable storage and restores it after refresh (named=%s)', async (named) => {
    if (named) setName()
    records.set('child-a', progress('deferred'))
    const first = await mount()
    const pending = deferred<VoiceOnboardingProgress>()
    desktop.saveVoiceOnboarding.mockImplementationOnce(async () => {
      const saved = await pending.promise
      records.set(saved.childId, saved)
      return saved
    })
    resumeTutorial()
    const spokenBefore = vi.mocked(LocalInputs.speakLocally).mock.calls.length
    let command!: Promise<void>
    await act(async () => { command = Promise.resolve(microphone.onTranscript('我忘了怎么叫你了，再带我练一次')) })
    expect(localApi.agentTurn).toHaveBeenCalledExactlyOnceWith('session-child-a', '我忘了怎么叫你了，再带我练一次')
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    expect(records.get('child-a')?.step).toBe('deferred')
    await act(async () => { pending.resolve(progress('wake_practice')); await command })
    expect(records.get('child-a')?.step).toBe('wake_practice')
    const output = { onboarding_restarted: true, onboarding_phase: named ? 'active' : 'naming', onboarding_step: named ? 'wake_practice' : null }
    const receipt = expect.objectContaining({ capability: 'onboarding.resume', execution_location: 'client_device_bridge', action_type: 'resume_voice_tutorial', status: 'completed', output })
    expect(localApi.completeAgentTurn).toHaveBeenCalledExactlyOnceWith('resume-tutorial', expect.any(String), 'completed', undefined, { execution_receipt: receipt, client_bridge_receipt: receipt })
    const newSpeech = vi.mocked(LocalInputs.speakLocally).mock.calls.slice(spokenBefore)
    expect(newSpeech).toHaveLength(1)
    expect(newSpeech[0][0]).toContain(named ? '名字“小知”' : '先给我起个短名字')
    expect(newSpeech[0][0]).not.toContain('云端规划已选择')
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    first.unmount()
    await mount()
    expect(screen.getByText(named ? /名字“小知”/ : /先给我起个短名字/)).toBeTruthy()
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledTimes(1)
  })

  it.each(['failure', 'mismatch', 'privacy', 'unmount'] as const)('never completes a tutorial restart after %s', async (failure) => {
    setName()
    records.set('child-a', progress('deferred'))
    const view = await mount()
    const pending = deferred<VoiceOnboardingProgress>()
    if (failure === 'failure') desktop.saveVoiceOnboarding.mockRejectedValueOnce(new Error('disk full'))
    else desktop.saveVoiceOnboarding.mockReturnValueOnce(pending.promise)
    resumeTutorial()
    let command!: Promise<void>
    await act(async () => { command = Promise.resolve(microphone.onTranscript('我忘了怎么叫你了，再带我练一次')) })
    if (failure === 'privacy') await say('停止全部监听')
    else if (failure === 'unmount') view.unmount()
    const spokenBefore = vi.mocked(LocalInputs.speakLocally).mock.calls.length
    await act(async () => { pending.resolve(progress(failure === 'mismatch' ? 'deferred' : 'wake_practice')); await command })
    expect(vi.mocked(localApi.completeAgentTurn).mock.calls.some(([, , status]) => status === 'completed')).toBe(false)
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith('resume-tutorial', expect.any(String), 'failed', expect.any(String))
    expect(records.get('child-a')?.step).toBe('deferred')
    if (failure === 'privacy' || failure === 'unmount') expect(LocalInputs.speakLocally).toHaveBeenCalledTimes(spokenBefore)
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledTimes(1)
  })

  it('keeps learning drafts and never repeats a restart or rewrites its outcome when acknowledgement is lost', async () => {
    setName()
    records.set('child-a', progress('deferred'))
    const initial = workspace()
    initial.drafts.agent_command = { updated_at: new Date().toISOString(), payload: { text: '还没问完的数学题' } }
    const clearDraft = vi.spyOn(localApi, 'clearSessionDraft')
    await mount({ initial })
    resumeTutorial()
    vi.mocked(localApi.completeAgentTurn).mockRejectedValueOnce(new Error('ack lost'))
    const spokenBefore = vi.mocked(LocalInputs.speakLocally).mock.calls.length
    await say('我忘了怎么叫你了，再带我练一次')
    expect(records.get('child-a')?.step).toBe('wake_practice')
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledTimes(1)
    expect(localApi.completeAgentTurn).toHaveBeenCalledTimes(1)
    expect(vi.mocked(localApi.completeAgentTurn).mock.calls[0][2]).toBe('completed')
    expect(screen.getByText(/执行回执尚未确认/)).toBeTruthy()
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('还没问完的数学题')
    expect(clearDraft).not.toHaveBeenCalled()
    expect(vi.mocked(LocalInputs.speakLocally).mock.calls.slice(spokenBefore)).toHaveLength(1)
  })

  it('does not execute the tutorial action under an inconsistent server execution contract', async () => {
    setName()
    records.set('child-a', progress('deferred'))
    await mount()
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce({ agent_turn_id: 'bad-resume', response_text: '不能执行', action: {
      type: 'resume_voice_tutorial', capability: 'onboarding.resume', execution_location: 'server',
    } } as AgentTurn)
    await say('我忘了怎么叫你了，再带我练一次')
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).toHaveBeenCalledWith('bad-resume', expect.any(String), 'failed', 'CLIENT_VOICE_ACTION_UNAVAILABLE')
  })

  it('holds the incoming entry command until deferred is saved, then forwards the original once without repetition', async () => {
    setName()
    respond('none', { route: 'agent', response_text: '先不练习，我们处理刚才的看题请求。' })
    const saved = deferred<VoiceOnboardingProgress>()
    desktop.saveVoiceOnboarding.mockImplementation(async (value: VoiceOnboardingProgress) => {
      const receipt = value.step === 'deferred' ? await saved.promise : value
      records.set(receipt.childId, receipt)
      return receipt
    })
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce({ agent_turn_id: 'entry-turn', action: { type: 'none' }, response_text: '收到原来的看题请求。' } as AgentTurn)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    await mount({ initialVoiceCommand: '打开高拍仪帮我看题' })
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(records.get('child-a')?.step).toBe('wake_practice')
    expect(localApi.startDocumentMonitor).not.toHaveBeenCalled()
    expect(localApi.registerDevice).not.toHaveBeenCalled()
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(desktop.saveVoiceOnboarding.mock.calls.at(-1)?.[0].step).toBe('deferred'))
    await act(async () => { saved.resolve(desktop.saveVoiceOnboarding.mock.calls.at(-1)![0]) })
    expect(records.get('child-a')?.step).toBe('deferred')
    expect(localApi.agentTurn).toHaveBeenCalledExactlyOnceWith('session-child-a', '打开高拍仪帮我看题')
    expect(screen.queryByText(/请重新说一次/)).toBeNull()
  })

  it.each(['onboarding', 'agent'] as const)('keeps the real recorder in conversation after %s handoff, accepts the next sentence, then honors inactivity standby', async (route) => {
    const harness = recordingHarness()
    setName()
    records.set('child-a', progress('study_practice'))
    const original = route === 'agent' ? '教程先放一边，先帮我学分数' : '我想晚点再练教程'
    respond(route === 'agent' ? 'none' : 'skip', { route })
    const asr = vi.spyOn(localApi, 'transcribe')
      .mockResolvedValueOnce({ transcript: original, status: 'completed', cloud_used: true } as Awaited<ReturnType<typeof localApi.transcribe>>)
      .mockResolvedValue({ transcript: '还有英语作业也想问你', status: 'completed', cloud_used: true } as Awaited<ReturnType<typeof localApi.transcribe>>)
    const standby = vi.spyOn(localApi, 'transcribeStandby').mockResolvedValue({ transcript: '小知', status: 'completed', cloud_used: false } as Awaited<ReturnType<typeof localApi.transcribeStandby>>)
    vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'handoff-turn', action: { type: 'none', suppress_speech: true }, response_text: '收到。' } as AgentTurn)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    await mount()
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    const opens = harness.getUserMedia.mock.calls.length
    await harness.capture()
    expect(records.get('child-a')?.step).toBe('deferred')
    expect(microphone.wakeWord).toBe('小知')
    expect(screen.getByRole('button', { name: /正在听 · 连续对话/ })).toBeTruthy()
    expect(harness.getUserMedia).toHaveBeenCalledTimes(opens)
    expect(harness.stop).not.toHaveBeenCalled()
    await harness.capture()
    expect(asr).toHaveBeenCalledTimes(2)
    expect(asr.mock.calls[1][1]).toBe('agent_command')
    expect(standby).not.toHaveBeenCalled()
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    expect(vi.mocked(localApi.agentTurn).mock.calls.map(([, text]) => text)).toEqual(route === 'agent' ? [original, '还有英语作业也想问你'] : ['还有英语作业也想问你'])
    await act(async () => { await vi.advanceTimersByTimeAsync(61_000) })
    expect(screen.getByRole('button', { name: '正在听唤醒词 · 小知' })).toBeTruthy()
    await harness.capture()
    expect(standby).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: /正在听 · 连续对话/ })).toBeTruthy()
    expect(harness.getUserMedia).toHaveBeenCalledTimes(opens)
  })

  it('holds proactive and tutor recovery speech until the final tutorial narration ends', async () => {
    setName()
    records.set('child-a', progress('study_practice'))
    const initial = workspace()
    initial.drafts.agent_command = { updated_at: new Date().toISOString(), payload: { tutor_recovery: {
      version: 1, turnId: 'old-turn', actionId: 'old-action-id', subject: '数学', question: '为什么', attempts: 1, createdAt: new Date().toISOString(),
    } } } as NonNullable<SessionWorkspaceState['drafts']['agent_command']>
    const listeners = new Set<(state: voice.CompanionVoiceState) => void>()
    vi.spyOn(voice, 'subscribeCompanionVoice').mockImplementation((listener) => { listeners.add(listener); return () => { listeners.delete(listener) } })
    vi.mocked(voice.getCompanionVoiceState).mockReturnValue('speaking')
    await mount({ initial })
    const intervene = () => window.dispatchEvent(new CustomEvent('hsc-agent-intervention', { detail: { message: '自动提醒不应抢话' } }))
    await act(async () => { intervene() })
    respond('skip')
    await say('跳过教程')
    await act(async () => { intervene() })
    const messages = () => vi.mocked(LocalInputs.speakLocally).mock.calls.map(([text]) => text)
    expect(messages().some((text) => /欢迎回家|自动提醒不应抢话|这次讲解暂时没有完成/.test(text))).toBe(false)
    await act(async () => {
      vi.mocked(voice.getCompanionVoiceState).mockReturnValue('idle')
      listeners.forEach((listener) => listener('idle'))
    })
    expect(messages().some((text) => text.includes('这次讲解暂时没有完成'))).toBe(true)
    expect(microphone.autoStart).toBe(true)
    expect(localApi.askTutor).not.toHaveBeenCalled()
  })

  it.each(['小知', '停止全部监听', '小知，停止全部监听'])('does not submit narration echo and resumes tutorial ASR after the acoustic tail: %s', async (transcript) => {
    const harness = recordingHarness()
    setName()
    records.set('child-a', progress('wake_practice'))
    const listeners = new Set<(state: voice.CompanionVoiceState) => void>()
    vi.spyOn(voice, 'subscribeCompanionVoice').mockImplementation((listener) => { listeners.add(listener); return () => { listeners.delete(listener) } })
    vi.mocked(voice.getCompanionVoiceState).mockReturnValue('speaking')
    const pending = deferred<Awaited<ReturnType<typeof localApi.transcribeStandby>>>()
    const asr = vi.spyOn(localApi, 'transcribeStandby').mockReturnValueOnce(pending.promise)
    await mount()
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    await harness.capture()
    expect(asr).not.toHaveBeenCalled()
    const opens = harness.getUserMedia.mock.calls.length
    await act(async () => {
      vi.mocked(voice.getCompanionVoiceState).mockReturnValue('idle')
      listeners.forEach((listener) => listener('idle'))
      await vi.advanceTimersByTimeAsync(LocalInputs.companionPlaybackCaptureCooldownMs + 1)
    })
    expect(harness.getUserMedia).toHaveBeenCalledTimes(opens)
    await harness.capture()
    expect(asr).toHaveBeenCalledTimes(1)
    expect(asr.mock.calls[0][1]?.aborted).toBe(false)
    await act(async () => { pending.resolve({ client_action_id: 'wake-after-tail', transcript, duration_ms: 320, model_version: 'standby-local', status: 'completed', persisted: false, cloud_used: false }) })
    expect(records.get('child-a')?.step).toBe(transcript === '小知' ? 'study_practice' : 'wake_practice')
    if (transcript.includes('停止全部监听')) {
      expect(microphone.autoStart).toBe(false)
      expect(harness.stop).toHaveBeenCalled()
      expect(localApi.releaseDeviceLease).toHaveBeenCalledWith('tutorial-mic', expect.any(String))
      expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    } else expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
  })

  it('preempts pending progress and late wake callbacks immediately for privacy stop', async () => {
    setName()
    records.set('child-a', progress('wake_practice'))
    await mount()
    const saved = deferred<VoiceOnboardingProgress>()
    desktop.saveVoiceOnboarding.mockReturnValueOnce(saved.promise)
    await wake()
    const staleWake = microphone.onWake
    await say('停止全部监听')
    const spoken = vi.mocked(LocalInputs.speakLocally).mock.calls.length
    await act(async () => { saved.resolve(progress('study_practice')); staleWake?.() })
    expect(vi.mocked(LocalInputs.speakLocally)).toHaveBeenCalledTimes(spoken)
    expect(screen.queryByText(/听到你叫我了/)).toBeNull()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
  })

  it('ignores a pending name save when the child switches and never starts practice for the wrong child', async () => {
    const first = await mount()
    candidate()
    await say('小知')
    const saved = deferred<DesktopWakeWordSettings>()
    desktop.saveWakeWordSettings.mockReturnValueOnce(saved.promise)
    respond('confirm_name')
    let confirmation!: Promise<void>
    await act(async () => { confirmation = Promise.resolve(microphone.onTranscript('确认')) })
    first.unmount()
    await mount({ initial: workspace('child-b') })
    await act(async () => {
      saved.resolve({ childId: 'child-a', wakeWord: '小知', enabled: true, inactivitySeconds: 60, updatedAt: '2026-09-05T12:00:00Z' })
      await confirmation
    })
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
    expect(screen.getByText(/先给我起个短名字/)).toBeTruthy()
  })

  it('routes typed practice through cloud understanding and only persists progress, like hands-free practice', async () => {
    setName()
    records.set('child-a', progress('document_practice'))
    await mount()
    respond('advance')
    const input = screen.getByLabelText('对学习伙伴说')
    await act(async () => { fireEvent.change(input, { target: { value: '帮我看一下这道题' } }); fireEvent.keyDown(input, { key: 'Enter' }) })
    expect(records.get('child-a')?.step).toBe('finish_practice')
    expect(localApi.voiceInteraction).toHaveBeenCalledExactlyOnceWith('session-child-a', expect.objectContaining({ transcript: '帮我看一下这道题', context: { phase: 'tutorial', step: 'document_practice', wake_word: '小知' } }), expect.any(AbortSignal))
    expect(localApi.agentTurn).not.toHaveBeenCalled()
  })

  it('allows real voice requests after deferring without a name, including after reload, and names only on explicit restart', async () => {
    const first = await mount()
    respond('skip')
    await say('以后再学')
    expect(records.get('child-a')?.step).toBe('deferred')
    expect(screen.getByText(/暂时还没设置唤醒名字/)).toBeTruthy()
    await say('今天数学作业三道题')
    expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    expect(screen.queryByText(/我听到的唤醒名字/)).toBeNull()
    first.unmount()
    await mount()
    expect(microphone).toMatchObject({ wakeWord: undefined, autoStart: true })
    await say('帮我看这道题')
    expect(localApi.agentTurn).toHaveBeenCalledTimes(2)
    resumeTutorial()
    await say('重新教程')
    candidate()
    await say('小知')
    respond('confirm_name')
    await say('确认')
    expect(records.get('child-a')?.step).toBe('wake_practice')
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledTimes(1)
  })

  it('does not consume a completed user entry request while progress is loading', async () => {
    setName()
    const loading = deferred<VoiceOnboardingProgress>()
    desktop.getVoiceOnboarding.mockReturnValueOnce(loading.promise)
    await mount({ initialVoiceCommand: '今天数学作业三道题' })
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    await act(async () => { loading.resolve(progress('completed')) })
    expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
  })

  it('does not overwrite unreadable names and retries reading by voice', async () => {
    setName()
    desktop.getWakeWordSettings.mockRejectedValueOnce(new Error('corrupt file'))
    await mount()
    expect(screen.getByText(/不会覆盖原来的名字/)).toBeTruthy()
    expect(desktop.getVoiceOnboarding).not.toHaveBeenCalled()
    await say('今天数学作业三道题')
    expect(screen.getByText(/刚才的话保留在输入框，尚未执行/)).toBeTruthy()
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('今天数学作业三道题')
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    await say('重试')
    expect(records.get('child-a')?.step).toBe('wake_practice')
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
  })

  it('asks for a different name on conflict but retries the same confirmed name on disk failure', async () => {
    await mount()
    candidate()
    await say('小知')
    desktop.saveWakeWordSettings.mockRejectedValueOnce(new Error('这个唤醒名字已经有人使用了。请换一个名字，避免进入另一位孩子的学习记录。'))
    respond('confirm_name')
    await say('确认')
    expect(screen.getByText('这个唤醒名字已经有人使用了。请换一个名字，避免进入另一位孩子的学习记录。', { selector: '.agent-dialogue span' })).toBeTruthy()
    candidate('小月')
    await say('小月')
    desktop.saveWakeWordSettings.mockRejectedValueOnce(new Error('disk full'))
    respond('confirm_name')
    await say('确认')
    respond('confirm_name')
    await say('请再保存我刚才确认的名字')
    expect(desktop.saveWakeWordSettings.mock.calls.map(([value]) => value.wakeWord)).toEqual(['小知', '小月', '小月'])
    expect(records.get('child-a')?.step).toBe('wake_practice')
  })

  it('accepts the stores punctuation normalization for a parent-edited name', async () => {
    setName()
    records.set('child-a', progress('completed'))
    await mount()
    fireEvent.click(screen.getByRole('button', { name: '修改唤醒词' }))
    fireEvent.change(screen.getByLabelText('唤醒词'), { target: { value: '小月。' } })
    desktop.saveWakeWordSettings.mockImplementationOnce(async (value: DesktopWakeWordSettings) => ({ ...value, wakeWord: '小月' }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '保存并开始监听' })) })
    expect(screen.queryByText(/名字还没有保存成功/)).toBeNull()
    expect(microphone.wakeWord).toBe('小月')
  })

  const failedNameRecovery = (status: VoiceInteractionRecovery['status'] = 'retryable', values: Partial<VoiceInteractionRecovery> = {}): VoiceInteractionRecovery => ({
    session_id: 'session-child-a', child_id: 'child-a', status, original_action_key: 'original-name',
    request: { transcript: '以后叫你小月', client_action_id: 'original-name', context: { phase: 'naming' }, voice_input_id: 'original-asr' },
    response: null, reason: 'VOICE_INTERACTION_TIMEOUT', ...values,
  })

  it('retries the stored original name request by voice without sending the retry phrase to the model', async () => {
    await mount()
    vi.mocked(localApi.voiceInteraction).mockRejectedValueOnce(new Error('VOICE_INTERACTION_TIMEOUT'))
    vi.mocked(localApi.voiceInteractionRecovery).mockResolvedValue(failedNameRecovery())
    await say('以后叫你小月')
    vi.mocked(localApi.retryVoiceInteraction).mockResolvedValue(failedNameRecovery('completed', { response: cloudReply('name_candidate', { candidate: '小月', response_text: '你想叫我小月，对吗？' }) }))
    await say('再试一次')
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    expect(localApi.retryVoiceInteraction).toHaveBeenCalledWith('session-child-a', 'child-a', 'original-name', expect.any(String), { phase: 'naming' }, expect.any(AbortSignal))
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    respond('confirm_name'); await say('确认')
    expect(names.get('child-a')?.wakeWord).toBe('小月')
  })

  it('recovers the failed original on a fresh mount and offers a spoken retry without automatically sending it', async () => {
    vi.mocked(localApi.voiceInteractionRecovery).mockResolvedValue(failedNameRecovery())
    const view = await mount()
    expect(screen.getByText(/说“再试一次”.*刚才/, { selector: '.agent-dialogue span' })).toBeTruthy()
    expect(localApi.retryVoiceInteraction).not.toHaveBeenCalled()
    view.unmount()
    await mount()
    vi.mocked(localApi.retryVoiceInteraction).mockResolvedValue(failedNameRecovery('completed', { response: cloudReply('name_candidate', { candidate: '小月' }) }))
    await say('重试')
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(localApi.retryVoiceInteraction).toHaveBeenCalledTimes(1)
  })

  it.each(['pending', 'outcome_unknown'] as const)('never redispatches a stored %s attempt from a retry utterance', async status => {
    vi.mocked(localApi.voiceInteractionRecovery).mockResolvedValue(failedNameRecovery(status))
    await mount(); await say('再试一次')
    expect(localApi.retryVoiceInteraction).not.toHaveBeenCalled()
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(screen.getByText(/不会重复提交/, { selector: '.agent-dialogue span' })).toBeTruthy()
  })

  it('rejects a recovery owned by another child before applying or retrying the original', async () => {
    vi.mocked(localApi.voiceInteractionRecovery).mockResolvedValue(failedNameRecovery('completed', { child_id: 'child-b', response: cloudReply('name_candidate', { candidate: '小月' }) }))
    await mount(); await say('再试一次')
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(localApi.retryVoiceInteraction).not.toHaveBeenCalled()
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
  })

  it('does not dispatch a retry when the durable recovery cannot be read', async () => {
    vi.mocked(localApi.voiceInteractionRecovery).mockRejectedValue(new Error('disk unavailable'))
    await mount(); await say('再试一次')
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(localApi.retryVoiceInteraction).not.toHaveBeenCalled()
    expect(screen.getByText(/恢复记录.*无法确认/, { selector: '.agent-dialogue span' })).toBeTruthy()
  })

  it('keeps the original ASR binding when a recovered interpretation routes to the Agent', async () => {
    vi.mocked(localApi.voiceInteractionRecovery).mockResolvedValue(failedNameRecovery())
    vi.mocked(localApi.retryVoiceInteraction).mockResolvedValue(failedNameRecovery('completed', { response: cloudReply('none', { route: 'agent' }) }))
    vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'original-command', response_text: '', action: { type: 'none', suppress_speech: true } } as AgentTurn)
    await mount(); await act(async () => { await microphone.onTranscript('再试一次', undefined, 'retry-asr') })
    expect(localApi.agentTurn).toHaveBeenCalledWith('session-child-a', '以后叫你小月', undefined, 'original-asr')
  })

  it('replays a stored confirmation after remount and saves the name before advancing', async () => {
    const confirmation = failedNameRecovery('completed', {
      request: { transcript: '就用这个名字', client_action_id: 'confirmation',
        context: { phase: 'confirm_name', pending_name: '小月' }, voice_input_id: 'confirmation-asr' },
      response: cloudReply('confirm_name'),
    })
    vi.mocked(localApi.voiceInteractionRecovery).mockResolvedValue(confirmation)
    vi.mocked(localApi.retryVoiceInteraction).mockResolvedValue(confirmation)
    await mount()
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    await say('再试一次')
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(names.get('child-a')?.wakeWord).toBe('小月')
    expect(records.get('child-a')?.step).toBe('wake_practice')
  })

  it('keeps a completed confirmation recoverable when the name cannot be saved', async () => {
    const confirmation = failedNameRecovery('completed', {
      request: { transcript: '确认', client_action_id: 'confirmation',
        context: { phase: 'confirm_name', pending_name: '小月' } }, response: cloudReply('confirm_name'),
    })
    vi.mocked(localApi.voiceInteractionRecovery).mockResolvedValue(confirmation)
    vi.mocked(localApi.retryVoiceInteraction).mockResolvedValue(confirmation)
    desktop.saveWakeWordSettings.mockRejectedValueOnce(new Error('disk unavailable'))
    await mount(); await say('再试一次')
    expect(names.has('child-a')).toBe(false)
    expect(records.has('child-a')).toBe(false)
    await say('再试一次')
    expect(names.get('child-a')?.wakeWord).toBe('小月')
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
  })

  it('restores the pending name on spoken retry after the initial recovery read fails', async () => {
    const confirmation = failedNameRecovery('completed', {
      request: { transcript: '确认', client_action_id: 'confirmation',
        context: { phase: 'confirm_name', pending_name: '小月' } }, response: cloudReply('confirm_name'),
    })
    vi.mocked(localApi.voiceInteractionRecovery).mockRejectedValueOnce(new Error('temporary read failure'))
      .mockResolvedValue(confirmation)
    vi.mocked(localApi.retryVoiceInteraction).mockResolvedValue(confirmation)
    await mount(); await say('再试一次')
    expect(names.get('child-a')?.wakeWord).toBe('小月')
    expect(records.get('child-a')?.step).toBe('wake_practice')
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
  })

  it('leaves completed tutorial progress intact when daily name editing yields to learning', async () => {
    setName(); records.set('child-a', progress('completed'))
    vi.mocked(localApi.agentTurn).mockResolvedValueOnce({ agent_turn_id: 'daily-name', response_text: '',
      action: { type: 'configure_wake_word', candidate: '小芽同学' }, runtime_capability: 'voice.configure_wake_word',
      runtime_execution_location: 'client_device_bridge' } as AgentTurn)
      .mockResolvedValue({ agent_turn_id: 'daily-study', response_text: '', action: { type: 'none', suppress_speech: true } } as AgentTurn)
    await mount(); await say('修改唤醒词')
    respond('none', { route: 'agent' })
    await say('先帮我学分数')
    expect(localApi.prepareVoiceInteractionHandoff).not.toHaveBeenCalled()
    expect(localApi.agentTurn).toHaveBeenLastCalledWith('session-child-a', '先帮我学分数')
    expect(records.get('child-a')?.step).toBe('completed')
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
  })

  it.each(['failed', 'pending'] as const)('reads the original handoff on explicit retry after a deferred-profile recovery read is %s', async (initialRead) => {
    setName(); records.set('child-a', progress('deferred'))
    const delayed = deferred<VoiceInteractionHandoff>()
    const reader = vi.mocked(localApi.readVoiceInteractionHandoff)
    if (initialRead === 'failed') reader.mockRejectedValueOnce(new Error('temporary read failure'))
    else reader.mockReturnValueOnce(delayed.promise)
    reader.mockResolvedValue({ session_id: 'session-child-a', child_id: 'child-a', status: 'ready',
        handoff_id: `voice-interaction-handoff:${'a'.repeat(64)}`, agent_action_id: 'handoff-agent-one',
        deferred_progress: { ...progress('deferred'), step: 'deferred', updatedAt: '2026-10-15T03:00:00.000Z' },
        request: { transcript: '帮我学分数', client_action_id: 'original-learning', context: { phase: 'tutorial', step: 'study_practice' } },
        receipt: null, reason: null })
    vi.mocked(localApi.dispatchVoiceInteractionHandoff).mockResolvedValue({ agent_turn_id: 'restored', response_text: '',
      action: { type: 'none', suppress_speech: true } } as AgentTurn)
    await mount()
    expect(localApi.dispatchVoiceInteractionHandoff).not.toHaveBeenCalled()
    await say('再试一次')
    expect(localApi.readVoiceInteractionHandoff).toHaveBeenCalledTimes(2)
    expect(localApi.dispatchVoiceInteractionHandoff).toHaveBeenCalledOnce()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    if (initialRead === 'pending') await act(async () => { delayed.resolve({ session_id: 'session-child-a', child_id: 'child-a',
      status: 'none', handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }) })
  })

  it.each(['received', 'outcome_unknown'] as const)('keeps a restored %s handoff read-only even on explicit retry', async (status) => {
    setName(); records.set('child-a', progress('deferred'))
    vi.mocked(localApi.readVoiceInteractionHandoff).mockResolvedValue({ session_id: 'session-child-a', child_id: 'child-a', status,
      handoff_id: `voice-interaction-handoff:${'a'.repeat(64)}`, agent_action_id: 'handoff-agent-one',
      deferred_progress: { ...progress('deferred'), step: 'deferred', updatedAt: '2026-10-15T03:00:00.000Z' }, request: null, receipt: null, reason: null })
    await mount(); await say('再试一次')
    expect(localApi.dispatchVoiceInteractionHandoff).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
  })

  it('does not turn a stale handoff retry into a new Agent request after invalidation', async () => {
    setName(); records.set('child-a', progress('deferred'))
    const value: VoiceInteractionHandoff = { session_id: 'session-child-a', child_id: 'child-a', status: 'ready',
      handoff_id: `voice-interaction-handoff:${'a'.repeat(64)}`, agent_action_id: 'handoff-agent-one',
      deferred_progress: { childId: 'child-a', version: 1, step: 'deferred', updatedAt: '2026-10-15T03:00:00.000Z' },
      request: { transcript: '帮我学分数', client_action_id: 'original-learning', context: { phase: 'tutorial', step: 'study_practice' } },
      receipt: null, reason: null }
    vi.mocked(localApi.readVoiceInteractionHandoff).mockResolvedValueOnce(value).mockResolvedValue({ ...value,
      status: 'invalidated', request: null, reason: 'VOICE_INTERACTION_HANDOFF_INVALIDATED' })
    await mount(); await say('再试一次')
    expect(localApi.dispatchVoiceInteractionHandoff).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    expect(screen.getByText(/原学习要求的交接已取消或已变化/)).toBeTruthy()
  })

  it('keeps the original learning transcript out of drafts when handoff read fails after deferral', async () => {
    setName(); records.set('child-a', progress('study_practice'))
    respond('none', { route: 'agent' })
    await mount()
    vi.mocked(localApi.readVoiceInteractionHandoff).mockRejectedValueOnce(new Error('read unavailable after save'))
    await say('帮我解决这道私人作业问题')
    expect(records.get('child-a')?.step).toBe('deferred')
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 650)) })
    expect(JSON.stringify(vi.mocked(localApi.saveSessionDraft).mock.calls)).not.toContain('帮我解决这道私人作业问题')
    expect(localApi.agentTurn).not.toHaveBeenCalled()
  })

  it.each(['帮我学分数', '帮我学分数\n然后核对这道题'])('removes the full recovered original from drafts after successful handoff: %s', async (original) => {
    setName(); records.set('child-a', progress('study_practice'))
    const request = { transcript: original, client_action_id: 'original-learning', context: { phase: 'tutorial' as const, step: 'study_practice', wake_word: '小知' } }
    const failed = failedNameRecovery('retryable', { request })
    const completed = { ...failed, status: 'completed' as const, response: cloudReply('none', { route: 'agent' }) }
    vi.mocked(localApi.voiceInteraction).mockRejectedValueOnce(new Error('original model unavailable'))
    vi.mocked(localApi.voiceInteractionRecovery).mockResolvedValue(failed)
    vi.mocked(localApi.retryVoiceInteraction).mockResolvedValue(completed)
    vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'restored-learning', response_text: '', action: { type: 'none', suppress_speech: true } } as AgentTurn)
    await mount(); await say(original)
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue(original.replace(/\n/g, ''))
    await say('再试一次')
    expect(localApi.agentTurn).toHaveBeenCalledExactlyOnceWith('session-child-a', original)
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 650)) })
    expect(JSON.stringify(vi.mocked(localApi.saveSessionDraft).mock.calls)).not.toContain(original.split('\n')[0])
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('')
  })

  it('keeps learning queued before newer speech without invalidating its handoff', async () => {
    setName(); records.set('child-a', progress('study_practice'))
    const first = deferred<AgentTurn>()
    vi.mocked(localApi.agentTurn).mockReturnValueOnce(first.promise).mockResolvedValue({ agent_turn_id: 'queued',
      response_text: '', action: { type: 'none', suppress_speech: true } } as AgentTurn)
    respond('none', { route: 'agent' })
    await mount()
    let older!: Promise<void>; let learning!: Promise<void>; let newer!: Promise<void>
    await act(async () => {
      older = Promise.resolve(microphone.onTranscript('先核对这句话', { playback_id: 'played' }))
      learning = Promise.resolve(microphone.onTranscript('帮我学分数'))
    })
    await act(async () => { newer = Promise.resolve(microphone.onTranscript('还有英语作业')) })
    expect(localApi.prepareVoiceInteractionHandoff).not.toHaveBeenCalled()
    await act(async () => {
      first.resolve({ agent_turn_id: 'older', response_text: '', action: { type: 'none', suppress_speech: true } } as AgentTurn)
      await older; await learning; await newer
    })
    expect(localApi.agentTurn).toHaveBeenCalledTimes(3)
    expect(localApi.agentTurn).toHaveBeenNthCalledWith(2, 'session-child-a', '帮我学分数')
    expect(localApi.agentTurn).toHaveBeenNthCalledWith(3, 'session-child-a', '还有英语作业')
    expect(localApi.dispatchVoiceInteractionHandoff).toHaveBeenCalledOnce()
  })

  it.each([false, true])('forwards the original learning request after retrying a failed tutorial deferral, remount=%s', async (remount) => {
    setName(); records.set('child-a', progress('study_practice'))
    const original = '帮我讲一下分数约分'
    const context = { phase: 'tutorial' as const, step: 'study_practice', wake_word: '小知' }
    const saved = failedNameRecovery('completed', {
      request: { transcript: original, client_action_id: 'learning-handoff', context, voice_input_id: 'learning-asr' },
      response: cloudReply('none', { route: 'agent' }),
    })
    vi.mocked(localApi.voiceInteraction).mockResolvedValue(saved.response!)
    vi.mocked(localApi.voiceInteractionRecovery).mockResolvedValue(saved)
    vi.mocked(localApi.retryVoiceInteraction).mockResolvedValue(saved)
    vi.mocked(localApi.agentTurn).mockResolvedValue({ agent_turn_id: 'learning-turn', response_text: '',
      action: { type: 'none', suppress_speech: true } } as AgentTurn)
    desktop.saveVoiceOnboarding.mockRejectedValueOnce(new Error('disk unavailable'))
    const view = await mount()
    await act(async () => { await microphone.onTranscript(original, undefined, 'learning-asr') })
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    if (remount) { view.unmount(); await mount(); expect(localApi.agentTurn).not.toHaveBeenCalled() }
    await say('再试一次')
    expect(records.get('child-a')?.step).toBe('deferred')
    expect(localApi.agentTurn).toHaveBeenCalledWith('session-child-a', original, undefined, 'learning-asr')
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    expect(localApi.prepareVoiceInteractionHandoff).toHaveBeenCalledTimes(1)
  })
  it.each(['deferred','completed'] as const)('E3 settled unnamed tutorial should capture ordinary agent command (step=%s)', async (step) => {
    records.set('child-a', progress(step))
    await mount()
    expect(microphone.autoStart).toBe(true)
    expect(microphone.wakeWord).toBeUndefined()
    expect(microphone.purpose).toBe('agent_command')
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
  })

  it('bare wake keeps the saved question visible but only gives a short local acknowledgement without granting or submitting an answer', async () => {
    setName(); records.set('child-a', progress('deferred'))
    const runtime = { session_id:'session-child-a', active_objective:{objective_id:'old-objective',objective_type:'learning',summary:'原来待答的公开合成数学题',status:'active',success_criteria:[]},
      current_step:{step_id:'old-step',capability_name:'learning.request_response',status:'waiting_child',plan_status:'waiting_child',expected_evidence:[]} }
    const original = structuredClone(runtime)
    vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue(runtime as never)
    const answer = vi.spyOn(localApi, 'answerDiagnosticPrompt')
    const execute = vi.spyOn(localApi, 'executeAgentTurn')
    const grant = vi.spyOn(ordinaryResponse, 'ordinaryPromptPlayback')
    await mount()
    grant.mockClear()
    const spokenBefore = vi.mocked(LocalInputs.speakLocally).mock.calls.length
    await wake()
    const calls = vi.mocked(LocalInputs.speakLocally).mock.calls.slice(spokenBefore)
    expect(calls).toHaveLength(1)
    expect(calls[0][0]).toBe('我在。')
    expect(calls[0][4]).toMatchObject({localOnly:true})
    expect(calls[0][4]).not.toHaveProperty('ordinaryResponseAllowed')
    expect(grant).not.toHaveBeenCalled()
    expect(screen.getByText(/我在，小辰。我们继续“原来待答的公开合成数学题”/)).toBeInTheDocument()
    expect(runtime).toEqual(original)
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    expect(localApi.askTutor).not.toHaveBeenCalled()
    expect(answer).not.toHaveBeenCalled()
    expect(execute).not.toHaveBeenCalled()
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
  })

  it('bare wake keeps an unconfirmed original request untouched and only acknowledges locally instead of reading or replaying it', async () => {
    setName(); records.set('child-a', progress('deferred'))
    saveAgentRequest('session-child-a', 'synthetic-unconfirmed-action')
    const key = 'hsc-agent-request-v1:session-child-a:synthetic-unconfirmed-action'
    const original = localStorage.getItem(key)
    const answer = vi.spyOn(localApi, 'answerDiagnosticPrompt')
    const execute = vi.spyOn(localApi, 'executeAgentTurn')
    const grant = vi.spyOn(ordinaryResponse, 'ordinaryPromptPlayback')
    await mount()
    grant.mockClear()
    const spokenBefore = vi.mocked(LocalInputs.speakLocally).mock.calls.length
    await wake()
    const calls = vi.mocked(LocalInputs.speakLocally).mock.calls.slice(spokenBefore)
    expect(calls).toHaveLength(1)
    expect(calls[0][0]).toBe('我在。')
    expect(calls[0][4]).toMatchObject({localOnly:true})
    expect(calls[0][4]).not.toHaveProperty('ordinaryResponseAllowed')
    expect(grant).not.toHaveBeenCalled()
    expect(screen.getByText(AGENT_REQUEST_UNCONFIRMED)).toBeInTheDocument()
    expect(localStorage.getItem(key)).toBe(original)
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    expect(localApi.askTutor).not.toHaveBeenCalled()
    expect(answer).not.toHaveBeenCalled()
    expect(execute).not.toHaveBeenCalled()
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
  })

  it('bare wake on household entry uses the same short local acknowledgement exactly once', async () => {
    setName(); records.set('child-a', progress('deferred'))
    const consumed = vi.fn()
    const grant = vi.spyOn(ordinaryResponse, 'ordinaryPromptPlayback')
    const answer = vi.spyOn(localApi, 'answerDiagnosticPrompt')
    const execute = vi.spyOn(localApi, 'executeAgentTurn')
    await mount({initialWakeOnly:true,onInitialVoiceCommandConsumed:consumed})
    const calls = vi.mocked(LocalInputs.speakLocally).mock.calls.filter(call => call[0] === '我在。')
    expect(calls).toHaveLength(1)
    expect(calls[0][4]).toMatchObject({localOnly:true})
    expect(calls[0][4]).not.toHaveProperty('ordinaryResponseAllowed')
    expect(grant.mock.calls.filter(call => call[0].text === '我在。')).toHaveLength(0)
    expect(consumed).toHaveBeenCalledOnce()
    expect(screen.getByText(/我们先用半分钟看一下今天的精力和安排/)).toBeInTheDocument()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
    expect(answer).not.toHaveBeenCalled()
    expect(execute).not.toHaveBeenCalled()
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
  })

})
