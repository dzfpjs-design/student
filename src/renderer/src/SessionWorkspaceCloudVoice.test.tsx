import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as inputs from './LocalInputs'
import * as voice from './companionVoice'
import type { AgentTurn, SessionWorkspaceState, VoiceInteractionResponse, VoiceInteractionHandoff, VoiceInteractionRequest } from './types'
import type { VoiceOnboardingProgress } from './voiceOnboardingRuntime'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import { QwenNativeConversation } from './qwenNativeConversation'
import type { QwenNativeRuntimeBridge } from './qwenNativeProtocol'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawn } from 'node:child_process'
import { NativeRealtimeCapabilityGateway, signNativeRealtimeCapability } from '../../main/native-realtime-capabilities'
import { VoiceSettingsStore } from '../../main/voice-settings'
import { resetNativeClientExecutionsForTests } from './nativeClientExecution'
import { resetNativeCompletionAuthoritiesForTests } from './nativeCompletionRecovery'

vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: { isEncryptionAvailable: () => true,
  encryptString: (value: string) => Buffer.from(value), decryptString: (value: Buffer) => value.toString() } }))

let microphone: ComponentProps<typeof inputs.HandsFreeVoiceInput>
const realLoopbackFetch = globalThis.fetch
let nativeRuntime: QwenNativeRuntimeBridge | undefined
let nativeWakeWord: string | undefined
vi.mock('./QwenRealtimeVoiceInput', () => ({ QwenRealtimeVoiceInput: (props: { nativeRuntime: QwenNativeRuntimeBridge; wakeWord?: string }) => {
  nativeRuntime = props.nativeRuntime
  nativeWakeWord = props.wakeWord
  return null
} }))
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
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
const decision = (values: Partial<VoiceInteractionResponse> = {}): VoiceInteractionResponse => ({
  route: 'clarify', action: 'none', response_text: '可以，我们慢慢聊。', source: 'cloud', ...values,
})
const initial = {
  session: { session_id: 's-a', child_id: 'a', status: 'active', state: 'check_in', observation_status: 'paused', snooze_until: null },
  planning_context: { energy_level: 'medium', fixed_commitment_minutes: 0 }, family_rule: { observation_mode: 'shadow' },
  recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [], plan: null, plan_items: [], drafts: {}, latest_tutor_turn: null,
  sensor_release: { opened_leases: 0, active_leases: 0, released_leases: 0, by_device: {} }, daily_report_ready: false,
} as unknown as SessionWorkspaceState

describe('workspace cloud voice routing', () => {
  let settingsDirectory: string | undefined
  let desktop: { getWakeWordSettings: ReturnType<typeof vi.fn>; saveWakeWordSettings: ReturnType<typeof vi.fn>; getVoiceOnboarding: ReturnType<typeof vi.fn>; saveVoiceOnboarding: ReturnType<typeof vi.fn> }
  const say = async (text: string) => { await act(async () => { await microphone.onTranscript(text) }) }
  const mount = async () => { let view!: ReturnType<typeof render>; await act(async () => {
    view = render(<SessionWorkspace initial={initial} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
  }); return view }
  beforeEach(() => {
    realMicrophone = false
    desktop = {
      getWakeWordSettings: vi.fn(async () => null), saveWakeWordSettings: vi.fn(async (value) => value),
      getVoiceOnboarding: vi.fn(async () => null), saveVoiceOnboarding: vi.fn(async (value) => value),
    }
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop(desktop))
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('no unmocked network')))
    vi.spyOn(voice, 'getCompanionVoiceState').mockReturnValue('idle')
    vi.spyOn(inputs, 'speakLocally').mockImplementation((_message, _session, done) => { done?.('text_only') })
    vi.spyOn(localApi, 'voiceInteraction').mockResolvedValue(decision())
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({ agent_turn_id: 'turn', action: { type: 'none' }, response_text: '我们来学数学。' } as AgentTurn)
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
    const noHandoff = (session: string, child: string): VoiceInteractionHandoff => ({ session_id: session, child_id: child,
      status: 'none', handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null })
    let handoff: VoiceInteractionHandoff | null = null
    let source: VoiceInteractionRequest | null = null
    vi.spyOn(localApi, 'readVoiceInteractionHandoff').mockImplementation(async (session, child) => {
      if (!handoff || handoff.session_id !== session || handoff.child_id !== child) return noHandoff(session, child)
      const saved = desktop.saveVoiceOnboarding.mock.results.at(-1)
      const receipt = saved?.type === 'return' ? await saved.value.catch(() => null) : null
      if (handoff.status === 'needs_deferral' && receipt?.step === 'deferred' && receipt.updatedAt === handoff.deferred_progress?.updatedAt)
        return { ...handoff, status: 'ready', request: source }
      return handoff
    })
    vi.spyOn(localApi, 'invalidateVoiceInteractionHandoff').mockImplementation(async (session, child) => {
      handoff = null; source = null; return noHandoff(session, child)
    })
    vi.spyOn(localApi, 'prepareVoiceInteractionHandoff').mockImplementation(async (session, child) => {
      source = vi.mocked(localApi.voiceInteraction).mock.calls.at(-1)?.[1] ?? null
      handoff = { ...noHandoff(session, child), status: 'needs_deferral', handoff_id: `voice-interaction-handoff:${'a'.repeat(64)}`,
        agent_action_id: 'handoff-agent-one', deferred_progress: { childId: child, version: 1, step: 'deferred', updatedAt: '2026-10-15T03:00:00.000Z' } }
      return handoff
    })
    vi.spyOn(localApi, 'dispatchVoiceInteractionHandoff').mockImplementation(async (session) => {
      const request = source!
      handoff = { ...handoff!, status: 'received', request: null }
      return request.voice_input_id ? localApi.agentTurn(session, request.transcript, undefined, request.voice_input_id)
        : localApi.agentTurn(session, request.transcript)
    })
  })
  afterEach(async () => {
    cleanup(); localStorage.clear(); resetVoiceCaptureCoordinatorForTests(); resetNativeClientExecutionsForTests(); resetNativeCompletionAuthoritiesForTests(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers()
    if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
    else Reflect.deleteProperty(navigator, 'mediaDevices')
    if (settingsDirectory) { await rm(settingsDirectory, { recursive: true, force: true }); settingsDirectory = undefined }
  })

  it('restores original hands-free naming automatically after a readonly policy transient without a child click', async () => {
    vi.useFakeTimers()
    microphone = undefined as unknown as typeof microphone
    let healthy = false
    const policyRead = vi.fn(async () => new Response(JSON.stringify({ version: 1, capture: healthy ? 'configured' : 'blocked' }), { status: healthy ? 200 : 503 }))
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop(desktop, { status: 'ready', localApiUrl: 'http://127.0.0.1:40295', sessionToken: 'synthetic-workspace-policy-token', runtimeEpoch: 1 }, policyRead as typeof fetch))
    await mount()
    expect(microphone).toBeUndefined()
    healthy = true
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(microphone.autoStart).toBe(true)
    expect(microphone.purpose).toBe('wake_word_setup')
    await say('我今天有点累，能先聊会儿吗')
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    expect(localApi.voiceInteraction).toHaveBeenCalledWith('s-a', expect.objectContaining({ transcript: '我今天有点累，能先聊会儿吗', context: { phase: 'naming' } }), expect.any(AbortSignal))
    expect(policyRead).toHaveBeenCalledTimes(2)
  })

  it.each(['我想先聊聊天', '停止全部监听'])('drops tutorial narration audio and accepts the next real utterance without requiring wake: %s', async (transcript) => {
    realMicrophone = true
    vi.useFakeTimers()
    const processors: Array<{ connect: () => void; disconnect: () => void; onaudioprocess: ((event: AudioProcessingEvent) => void) | null }> = []
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
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'mic', status: 'active', expires_at: 'later' })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'mic', status: 'released' })
    vi.spyOn(localApi, 'transcribe').mockResolvedValue({ transcript, status: 'completed', cloud_used: true } as Awaited<ReturnType<typeof localApi.transcribe>>)
    let voiceState: voice.CompanionVoiceState = 'speaking'
    vi.mocked(voice.getCompanionVoiceState).mockImplementation(() => voiceState)
    await mount()
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(getUserMedia).toHaveBeenCalled()
    for (let index = 0; index < 4; index += 1) await act(async () => {
      await vi.advanceTimersByTimeAsync(256)
      processors.at(-1)?.onaudioprocess?.({ inputBuffer: { getChannelData: () => new Float32Array(4096).fill(0.05) } } as unknown as AudioProcessingEvent)
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(1_200) })
    expect(localApi.transcribe).not.toHaveBeenCalled()
    voiceState = 'idle'
    await act(async () => { await vi.advanceTimersByTimeAsync(inputs.companionPlaybackCaptureCooldownMs + 1) })
    for (let index = 0; index < 4; index += 1) await act(async () => {
      await vi.advanceTimersByTimeAsync(256)
      processors.at(-1)?.onaudioprocess?.({ inputBuffer: { getChannelData: () => new Float32Array(4096).fill(0.05) } } as unknown as AudioProcessingEvent)
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(1_200) })
    expect(localApi.transcribe).toHaveBeenCalledTimes(1)
    if (transcript === '停止全部监听') {
      expect(stop).toHaveBeenCalled()
      expect(microphone.autoStart).toBe(false)
      expect(localApi.voiceInteraction).not.toHaveBeenCalled()
    } else expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
  })

  it('routes natural conversation exactly once, speaks clarification locally and does not name it', async () => {
    await mount()
    await say('我今天有点累，能先聊会儿吗')
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    expect(localApi.voiceInteraction).toHaveBeenCalledWith('s-a', expect.objectContaining({ transcript: '我今天有点累，能先聊会儿吗', context: { phase: 'naming' } }), expect.any(AbortSignal))
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(inputs.speakLocally).toHaveBeenCalledWith('可以，我们慢慢聊。', 's-a', expect.any(Function), expect.any(Function), { localOnly: true, onPlaybackEnd: expect.any(Function), onNotDelivered: expect.any(Function), speechKey: expect.stringMatching(/^onboarding-response:s-a:\d+$/) })
  })

  it('persists deferred then sends the original learning request without requiring repetition', async () => {
    vi.mocked(localApi.voiceInteraction).mockResolvedValue(decision({ route: 'agent', action: 'skip' }))
    await mount()
    const saved = deferred<VoiceOnboardingProgress>()
    desktop.saveVoiceOnboarding.mockReturnValueOnce(saved.promise)
    let task!: Promise<unknown>
    await act(async () => { task = Promise.resolve(microphone.onTranscript('先不练了，教我分数吧')) })
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    await waitFor(() => expect(desktop.saveVoiceOnboarding).toHaveBeenCalledOnce())
    await act(async () => { saved.resolve(desktop.saveVoiceOnboarding.mock.calls[0][0]); await task })
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledWith(expect.objectContaining({ step: 'deferred' }))
    expect(localApi.agentTurn).toHaveBeenCalledExactlyOnceWith('s-a', '先不练了，教我分数吧')
    await say('还有英语')
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    expect(localApi.agentTurn).toHaveBeenLastCalledWith('s-a', '还有英语')
  })

  it('understands candidate and negative confirmation through cloud decisions, never saving a rejected name', async () => {
    vi.mocked(localApi.voiceInteraction)
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小月', response_text: '叫小月好吗？' }))
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'reject_name', response_text: '好，先不保存。' }))
    await mount()
    await say('我觉得叫你小月挺好听的')
    await say('不是，我还没想好呢')
    expect(vi.mocked(localApi.voiceInteraction).mock.calls[1][1].context).toEqual({ phase: 'confirm_name', pending_name: '小月' })
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    await say('让我再想想')
    expect(vi.mocked(localApi.voiceInteraction).mock.calls[2][1].context.phase).toBe('naming')
  })

  it('queues a new request during confirmed name storage and reads the saved name before routing it', async () => {
    vi.mocked(localApi.voiceInteraction)
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小月' }))
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'confirm_name' }))
      .mockResolvedValueOnce(decision({ route: 'agent', action: 'none' }))
    await mount()
    await say('叫你小月吧')
    const saved = deferred<DesktopWakeWordSettings>()
    desktop.saveWakeWordSettings.mockReturnValueOnce(saved.promise)
    let first!: Promise<unknown>
    let second!: Promise<unknown>
    await act(async () => { first = Promise.resolve(microphone.onTranscript('我觉得这个名字挺好')); second = Promise.resolve(microphone.onTranscript('先教我英语')) })
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(2)
    await act(async () => { saved.resolve(desktop.saveWakeWordSettings.mock.calls[0][0]); await Promise.all([first, second]) })
    expect(vi.mocked(localApi.voiceInteraction).mock.calls[2][1].context).toEqual({ phase: 'tutorial', step: 'wake_practice', wake_word: '小月' })
    expect(localApi.agentTurn).toHaveBeenCalledExactlyOnceWith('s-a', '先教我英语')
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledTimes(1)
  })

  it('does not hand off a learning request on a failed or mismatched deferred receipt', async () => {
    vi.mocked(localApi.voiceInteraction).mockResolvedValue(decision({ route: 'agent', action: 'none' }))
    desktop.saveVoiceOnboarding.mockImplementation(async (value) => ({ ...value, step: 'completed' }))
    await mount()
    await say('不用教程了，先帮我看题')
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('')
    expect(screen.getByText(/刚才的学习要求已保留/)).toBeTruthy()
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledTimes(1)
  })

  it.each(['unavailable', 'local_privacy'] as const)('does not mutate names or fall back to Agent for source %s', async (source) => {
    vi.mocked(localApi.voiceInteraction).mockResolvedValue(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小月', source }))
    await mount()
    await say('我们聊聊天')
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    vi.mocked(localApi.voiceInteraction).mockResolvedValue(decision())
    await say('然后呢')
    expect(vi.mocked(localApi.voiceInteraction).mock.calls[1][1].context.phase).toBe('naming')
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('我们聊聊天')
  })

  it('hands a scoped local privacy command to Agent once without interpreting it as a stop-all', async () => {
    vi.mocked(localApi.voiceInteraction).mockResolvedValue(decision({ route: 'agent', action: 'none', source: 'local_privacy' }))
    const pause = vi.spyOn(localApi, 'pauseObservation')
    await mount()
    await say('停止说话')
    expect(localApi.agentTurn).toHaveBeenCalledExactlyOnceWith('s-a', '停止说话')
    expect(pause).not.toHaveBeenCalled()
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledWith(expect.objectContaining({ step: 'deferred' }))
  })

  it('speaks private clarification locally without changing onboarding or forwarding the original text', async () => {
    vi.mocked(localApi.voiceInteraction).mockResolvedValue(decision({ source: 'local_privacy', response_text: '这件事留在本机处理。' }))
    await mount()
    await say('我想聊一件家庭私事')
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
    expect(inputs.speakLocally).toHaveBeenCalledWith('这件事留在本机处理。', 's-a', expect.any(Function), expect.any(Function), { localOnly: true, onPlaybackEnd: expect.any(Function), onNotDelivered: expect.any(Function), speechKey: expect.stringMatching(/^onboarding-response:s-a:\d+$/) })
  })

  it('immediately stops tutorial routing and ignores a late confirmation', async () => {
    const pending = deferred<VoiceInteractionResponse>()
    vi.mocked(localApi.voiceInteraction).mockReturnValueOnce(pending.promise)
    await mount()
    let task!: Promise<unknown>
    await act(async () => { task = Promise.resolve(microphone.onTranscript('叫你小月')) })
    const cancel = vi.spyOn(inputs, 'cancelLocalSpeech')
    await say('停止教程')
    expect(cancel).toHaveBeenCalled()
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledWith(expect.objectContaining({ step: 'deferred' }))
    const spoken = vi.mocked(inputs.speakLocally).mock.calls.length
    await act(async () => { pending.resolve(decision({ route: 'onboarding', action: 'confirm_name' })); await task })
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    expect(inputs.speakLocally).toHaveBeenCalledTimes(spoken)
  })

  it('accepts typed and hands-free speech during narration and queues continuous utterances', async () => {
    vi.mocked(voice.getCompanionVoiceState).mockReturnValue('speaking')
    const pending = deferred<VoiceInteractionResponse>()
    vi.mocked(localApi.voiceInteraction).mockReturnValueOnce(pending.promise)
    await mount()
    expect(microphone.transcriptGuard?.('我想学英语', { startedAt: 0 } as never) ?? true).toBe(true)
    let first!: Promise<unknown>
    let second!: Promise<unknown>
    await act(async () => {
      first = Promise.resolve(microphone.onTranscript('我想学英语'))
      second = Promise.resolve(microphone.onTranscript('先聊一下也行'))
    })
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    await act(async () => { pending.resolve(decision()); await Promise.all([first, second]) })
    expect(vi.mocked(localApi.voiceInteraction).mock.calls.map(([, body]) => body.transcript)).toEqual(['我想学英语', '先聊一下也行'])
    await act(async () => {
      fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: '现在感觉好多了' } })
      fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'), { key: 'Enter' })
    })
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(3)
  })

  it('preserves the original input on timeout with no agent fallback or automatic retry', async () => {
    vi.useFakeTimers()
    vi.mocked(localApi.voiceInteraction).mockReturnValue(new Promise(() => undefined))
    await mount()
    let task!: Promise<unknown>
    await act(async () => { task = Promise.resolve(microphone.onTranscript('我想先休息')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(30_001); await task })
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('我想先休息')
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    expect(localApi.agentTurn).not.toHaveBeenCalled()
  })

  it.each([
    ['privacy', 'onboarding'], ['unmount', 'onboarding'], ['privacy', 'agent'], ['unmount', 'agent'],
  ] as const)('ignores a late cloud %s cancellation with route %s and cancels the queue', async (reason, route) => {
    const pending = deferred<VoiceInteractionResponse>()
    vi.mocked(localApi.voiceInteraction).mockReturnValueOnce(pending.promise)
    const view = await mount()
    let first!: Promise<unknown>
    let second!: Promise<unknown>
    await act(async () => { first = Promise.resolve(microphone.onTranscript('叫你小月')); second = Promise.resolve(microphone.onTranscript('好的就这样')) })
    if (reason === 'privacy') await say('停止全部监听')
    else view.unmount()
    const spoken = vi.mocked(inputs.speakLocally).mock.calls.length
    await act(async () => { pending.resolve(decision({ route, action: route === 'agent' ? 'none' : 'name_candidate', candidate: '小月' })); await Promise.all([first, second]) })
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
    expect(localApi.agentTurn).not.toHaveBeenCalled()
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(1)
    expect(inputs.speakLocally).toHaveBeenCalledTimes(spoken)
  })
  it('confirms the existing nickname when a whole affirmation and cloud homophone agree, retaining original speech', async () => {
    vi.mocked(localApi.voiceInteraction)
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小知' }))
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小汁' }))
    await mount(); await say('叫你小知')
    await say('对，就叫小汁。')
    expect(vi.mocked(localApi.voiceInteraction).mock.calls[1][1]).toEqual(expect.objectContaining({
      transcript: '对，就叫小汁。', context: { phase: 'confirm_name', pending_name: '小知' },
    }))
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ childId: 'a', wakeWord: '小知', enabled: true }))
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ childId: 'a', step: 'wake_practice' }))
    expect(localApi.agentTurn).not.toHaveBeenCalled()
  })

  it.each([
    ['不，就不叫小汁', '小汁'], ['改叫小汁', '小汁'], ['对，就叫小雨', '小雨'],
    ['对，就叫小汁。然后教我数学', '小汁'], ['对，就叫小汁。停止全部监听', '小汁'],
    ['小汁', '小汁'], ['对，就叫小汁', '小雨'],
  ])('retains an actual new candidate or refusal without inventing confirmation: %s', async (transcript, candidate) => {
    vi.mocked(localApi.voiceInteraction)
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小知' }))
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate }))
    await mount(); await say('叫你小知'); await say(transcript)
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
  })

  it('does not turn an unbound initial naming utterance into a confirmed nickname', async () => {
    vi.mocked(localApi.voiceInteraction).mockResolvedValue(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小汁' }))
    await mount(); await say('对，就叫小汁。')
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
  })

  it('keeps a cloud refusal even if the speech sounds like the public confirmation', async () => {
    vi.mocked(localApi.voiceInteraction)
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小知' }))
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'reject_name' }))
    await mount(); await say('叫你小知'); await say('对，就叫小汁。')
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled()
  })

  it('awaits both original settings and named-progress saves before finishing the homophone confirmation', async () => {
    vi.mocked(localApi.voiceInteraction)
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小知' }))
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小汁' }))
    await mount(); await say('叫你小知')
    const wake = deferred<DesktopWakeWordSettings>(), progress = deferred<VoiceOnboardingProgress>()
    desktop.saveWakeWordSettings.mockReturnValueOnce(wake.promise); desktop.saveVoiceOnboarding.mockReturnValueOnce(progress.promise)
    let done = false, task!: Promise<unknown>
    await act(async () => { task = Promise.resolve(microphone.onTranscript('对，就叫小汁。')).then(() => { done = true }) })
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledTimes(1)
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled(); expect(done).toBe(false)
    await act(async () => { wake.resolve(desktop.saveWakeWordSettings.mock.calls[0][0]); await Promise.resolve() })
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledTimes(1); expect(done).toBe(false)
    await act(async () => { progress.resolve(desktop.saveVoiceOnboarding.mock.calls[0][0]); await task })
    expect(done).toBe(true)
  })

  it('preserves the original confirmation on settings failure and retries only after a fresh explicit utterance', async () => {
    vi.mocked(localApi.voiceInteraction)
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小知' }))
      .mockResolvedValue(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小汁' }))
    await mount(); await say('叫你小知')
    desktop.saveWakeWordSettings.mockRejectedValueOnce(new Error('synthetic storage failure'))
    await say('对，就叫小汁。')
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledTimes(1); expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('对，就叫小汁。')
    await say('对，就叫小汁。')
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledTimes(2)
    expect(desktop.saveWakeWordSettings).toHaveBeenLastCalledWith(expect.objectContaining({ childId: 'a', wakeWord: '小知' }))
    expect(desktop.saveVoiceOnboarding).toHaveBeenCalledTimes(1)
  })

  it('retains failed named-progress recovery without resaving the wake setting or retrying automatically', async () => {
    vi.mocked(localApi.voiceInteraction)
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小知' }))
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小汁' }))
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'advance' }))
    await mount(); await say('叫你小知')
    desktop.saveVoiceOnboarding.mockRejectedValueOnce(new Error('synthetic progress failure'))
    await say('对，就叫小汁。')
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledTimes(1); expect(desktop.saveVoiceOnboarding).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('对，就叫小汁。')
    await say('重试')
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledTimes(1); expect(desktop.saveVoiceOnboarding).toHaveBeenCalledTimes(2)
    expect(desktop.saveVoiceOnboarding).toHaveBeenLastCalledWith(expect.objectContaining({ childId: 'a', step: 'wake_practice' }))
    expect(localApi.voiceInteraction).toHaveBeenCalledTimes(2)
  })

  it.each(['privacy','unmount','child'] as const)('does not persist a late homophone decision after %s changes the context', async reason => {
    const late = deferred<VoiceInteractionResponse>()
    vi.mocked(localApi.voiceInteraction)
      .mockResolvedValueOnce(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小知' }))
      .mockReturnValueOnce(late.promise)
    const view = await mount(); await say('叫你小知')
    let task!: Promise<unknown>
    await act(async () => { task = Promise.resolve(microphone.onTranscript('对，就叫小汁。')) })
    if (reason === 'privacy') await say('停止全部监听')
    else if (reason === 'unmount') view.unmount()
    else await act(async () => { view.rerender(<SessionWorkspace key="s-b" initial={{ ...initial, session: { ...initial.session, session_id: 's-b', child_id: 'b' } }} childName="另一位合成同学" childGrade="四年级" onBack={vi.fn()} />) })
    await act(async () => { late.resolve(decision({ route: 'onboarding', action: 'name_candidate', candidate: '小汁' })); await task })
    expect(desktop.saveWakeWordSettings).not.toHaveBeenCalled(); expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
  })

  it.each(['failed', 'saved'] as const)('handles a native voice rename and durable confirmation: %s', async outcome => {
    microphone = undefined as unknown as typeof microphone
    nativeRuntime = undefined
    const old = { childId: 'child-a', wakeWord: '小知', enabled: true, inactivitySeconds: 60, updatedAt: '2026-10-10T12:00:00Z' }
    settingsDirectory = await mkdtemp(join(tmpdir(), 'hsc-synthetic-native-rename-'))
    const store = new VoiceSettingsStore(settingsDirectory)
    await store.saveWakeWord(old)
    desktop.getWakeWordSettings.mockImplementation(async (childId: string) => store.loadWakeWord(childId))
    desktop.saveWakeWordSettings.mockImplementation(async value => store.saveWakeWord(value))
    desktop.getVoiceOnboarding.mockResolvedValue({ childId: 'child-a', version: 1, step: 'completed', updatedAt: old.updatedAt })
    const candidate = outcome === 'saved' ? '小枝小枝' : '小知'
    const catalog = { version: 1 as const, protocol: 'qwen_runtime_capability_v1' as const, capabilities: [{
      name: 'voice.configure_wake_word', tool_name: 'voice_configure_wake_word', description: 'Change the name.',
      required_arguments: ['candidate'], arguments_schema: { type: 'object', additionalProperties: false,
        required: ['candidate'], properties: { candidate: { type: 'string' } } },
    }] }
    const identity = { version: 1, voice_input_id: 'voice', supplier_session_id: 'supplier', input_item_id: 'item', response_id: 'response', call_id: 'call' }
    const turn = { agent_turn_id: 'public-rename', session_id: 's-a', child_id: 'child-a', transcript: '把唤醒词改为小知',
      action: { type: 'configure_wake_word', candidate, voice_input_id: 'voice', source: 'qwen_realtime_capability' },
      runtime_capability: 'voice.configure_wake_word', runtime_execution_location: 'client_device_bridge', tool_status: 'pending', response_text: '' }
    const dispatch = { version: 1, dispatch_id: 'dispatch', action_id: 'action', status: 'pending', turn }
    const descriptor = { version: 1, dispatch_id: 'dispatch', action_id: 'action', agent_turn_id: 'public-rename',
      capability_name: 'voice.configure_wake_word', action_type: 'configure_wake_word', session_id: 's-a', child_id: 'child-a',
      storage_generation: 'synthetic-generation', identity }
    const pending = { version: 1, proposal_status: 'admitted', execution: { status: 'pending', location: 'client_device_bridge' }, public_result: {} }
    let completed = false
    const forward = vi.fn(async (request: { operation: string; body?: Record<string, unknown> }) => {
      if (request.operation === 'input') return { voice_input_id: 'voice' }
      if (request.operation === 'proposal') return { ...pending, client_dispatch: dispatch }
      if (request.operation === 'client' && request.body?.phase === 'claim') return { version: 1, status: 'claimed', client_dispatch: dispatch, completion_descriptor: descriptor }
      if (request.operation === 'client' && request.body?.phase === 'complete') {
        completed = true
        return { ...pending, execution: { status: 'completed', location: 'client_device_bridge' }, host_turn: { ...turn, tool_status: 'completed' } }
      }
      if (request.operation === 'receipt') return { ...pending, execution: { status: completed ? 'completed' : 'pending', location: 'client_device_bridge' } }
      throw Error('UNEXPECTED_SYNTHETIC_NATIVE_REQUEST')
    })
    vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ ...desktop,
      getQwenRealtimeSettings: async () => ({ configured: true, enabled: true, model: 'qwen3.8-omni-flash-realtime' }),
      forwardQwenNativeRuntime: forward }))
    let view!: ReturnType<typeof render>
    await act(async () => { view = render(<SessionWorkspace initial={{ ...initial, session: { ...initial.session, child_id: 'child-a' } }} childName="合成孩子" childGrade="初三" onBack={vi.fn()} />) })
    await waitFor(() => expect(nativeRuntime).toBeDefined())
    const ledger = new QwenNativeConversation('supplier', { ...nativeRuntime!, catalog }, () => true, 'host')
    expect(ledger.startInput('item', { sessionId: 's-a', reference: { child_id: 'child-a' }, localInputId: 'onset', capturedAt: 1,
      carryoverConfirmationId: null, source: 'accepted_onset' })).toBe(true)
    await act(async () => {
      await ledger.completeInput('item', '把唤醒词改为小知')
      expect(ledger.bindResponse('response', 'item')).toBe(true)
      await ledger.propose('response', 'call', 'voice_configure_wake_word', JSON.stringify({ args: { candidate } }))
    })
    await waitFor(() => expect(microphone?.autoStart).toBe(true))
    expect(microphone.purpose).toBe('wake_word_setup')
    expect(forward.mock.calls.some(([request]) => request.operation === 'client' && request.body?.phase === 'claim')).toBe(true)
    nativeRuntime = undefined
    if (outcome === 'failed') desktop.saveWakeWordSettings.mockRejectedValueOnce(new Error('synthetic daily save failure'))
    vi.mocked(localApi.voiceInteraction).mockResolvedValue(outcome === 'failed'
      ? decision({ route: 'onboarding', action: 'name_candidate', candidate: '小汁' })
      : decision({ route: 'onboarding', action: 'confirm_name', candidate }))
    await say(outcome === 'failed' ? '对，就叫小汁。' : '确认')
    expect(desktop.saveWakeWordSettings).toHaveBeenCalledTimes(1)
    if (outcome === 'failed') expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('对，就叫小汁。')
    expect(desktop.saveVoiceOnboarding).not.toHaveBeenCalled()
    expect((await new VoiceSettingsStore(settingsDirectory).loadWakeWord('child-a'))?.wakeWord).toBe(outcome === 'saved' ? candidate : old.wakeWord)
    if (outcome === 'saved') await waitFor(() => expect(nativeRuntime).toBeDefined())
    else expect(nativeRuntime).toBeUndefined()
    view.unmount()
  })

  it('does not automatically open the microphone when the parent edits the name manually', async () => {
    microphone = undefined as unknown as typeof microphone
    desktop.getWakeWordSettings.mockResolvedValue({ childId: 'a', wakeWord: '小知', enabled: true,
      inactivitySeconds: 60, updatedAt: '2026-10-10T12:00:00Z' })
    desktop.getVoiceOnboarding.mockResolvedValue({ childId: 'a', version: 1, step: 'completed', updatedAt: '2026-10-10T12:00:00Z' })
    await mount()
    fireEvent.click(await screen.findByRole('button', { name: '修改唤醒词' }))
    await waitFor(() => expect(microphone).toBeDefined())
    expect(microphone.autoStart).toBe(false)
  })

  it('carries native voice rename through the real loopback server and restores the confirmed name', async () => {
    microphone = undefined as unknown as typeof microphone
    nativeRuntime = undefined
    settingsDirectory = await mkdtemp(join(tmpdir(), 'hsc-synthetic-native-loopback-'))
    const directory = settingsDirectory
    const python = join(process.cwd(), process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python')
    const server = spawn(python, ['services/local_agent/tests/native_rename_loopback_server.py', directory], {
      cwd: process.cwd(), env: { PATH: process.env.PATH ?? '', TMPDIR: tmpdir(), TEMP: tmpdir(),
        ...(process.env.SystemRoot ? { SystemRoot: process.env.SystemRoot } : {}), PYTHONPATH: join(process.cwd(), 'services/local_agent/src') },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stderr = ''
    server.stderr.on('data', chunk => { stderr += String(chunk).slice(0, 3000) })
    try {
      const port = await new Promise<string>((resolve, reject) => {
        let data = ''
        const timeout = setTimeout(() => reject(Error('SYNTHETIC_SERVER_START_TIMEOUT')), 10_000)
        server.once('error', reject)
        server.once('exit', code => reject(Error(`SYNTHETIC_SERVER_EXIT_${code}:${stderr}`)))
        server.stdout.on('data', chunk => { data += String(chunk); const line = data.match(/^(\d+)\r?\n/); if (line) { clearTimeout(timeout); resolve(line[1]) } })
      })
      const url = `http://127.0.0.1:${port}`
      const token = 'synthetic-native-rename-token'
      const request = async (path: string, method: string, body?: unknown, extra: Record<string, string> = {}) => {
        const response = await realLoopbackFetch(url + path, { method, headers: { 'X-HSC-Session': token, 'Content-Type': 'application/json', ...extra },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
        if (!response.ok) throw Error(`SYNTHETIC_HTTP_${path}_${response.status}:${await response.text()}`)
        return response.json()
      }
      await vi.waitFor(async () => { const response = await realLoopbackFetch(url + '/v1/health', { headers: { 'X-HSC-Session': token } }); expect(response.ok).toBe(true) }, { timeout: 10_000 })
      const parent = await request('/v1/parent/setup', 'POST', { pin: '8264' }) as { parent_token: string }
      await request('/v1/parent/privacy', 'PUT', { cloud_model_text_allowed: true, cloud_audio_allowed: true }, { 'X-HSC-Parent': parent.parent_token })
      const opened = await request('/v1/sessions', 'POST', { child_id: 'child-primary-4', client_action_id: 'synthetic-rename-session' }) as { session_id: string }
      const child = 'child-primary-4', sid = opened.session_id
      const store = new VoiceSettingsStore(directory)
      const old = { childId: child, wakeWord: '小知', enabled: true, inactivitySeconds: 60, updatedAt: '2026-10-10T12:00:00Z' }
      await store.saveWakeWord(old)
      desktop.getWakeWordSettings.mockImplementation(async (childId: string) => store.loadWakeWord(childId))
      desktop.saveWakeWordSettings.mockImplementation(async value => store.saveWakeWord(value))
      desktop.getVoiceOnboarding.mockResolvedValue({ childId: child, version: 1, step: 'completed', updatedAt: old.updatedAt })
      vi.mocked(localApi.completeAgentTurn).mockRestore()
      const owner = { status: 'ready', localApiUrl: url, sessionToken: token, runtimeEpoch: 1, child: {}, revision: 0 }
      const sender = { sender: { id: 92 } }
      const gateway = new NativeRealtimeCapabilityGateway({ allowed: event => event.sender.id === 92, current: () => owner,
        policy: async () => ({ capture: 'configured', runtimeEpoch: 1 }), sign: proof => signNativeRealtimeCapability('cd'.repeat(32), proof), fetch: realLoopbackFetch })
      const host = gateway.register(sender)
      const operations: Array<{ operation: string; phase?: unknown }> = []
      vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({ ...desktop,
        getQwenRealtimeSettings: async () => ({ configured: true, enabled: true, model: 'qwen3.8-omni-flash-realtime' }),
        forwardQwenNativeRuntime: (value: Parameters<typeof gateway.forward>[1]) => {
          operations.push({ operation: value.operation, phase: value.body?.phase })
          return gateway.forward(sender, value)
        } }, { status: 'ready', localApiUrl: url, sessionToken: token, runtimeEpoch: 1 }))
      let view!: ReturnType<typeof render>
      await act(async () => { view = render(<SessionWorkspace initial={{ ...initial, session: { ...initial.session, session_id: sid, child_id: child } }} childName="合成孩子" childGrade="初三" onBack={vi.fn()} />) })
      await waitFor(() => expect(nativeRuntime).toBeDefined())
      const bridge = nativeRuntime!
      const catalog = await bridge.loadCatalog(host)
      const ledger = new QwenNativeConversation('supplier', { ...bridge, catalog }, () => true, host)
      expect(ledger.startInput('item', { sessionId: sid, reference: { child_id: child }, localInputId: 'synthetic-onset', capturedAt: Date.now(),
        carryoverConfirmationId: null, source: 'accepted_onset' })).toBe(true)
      let receipt!: Awaited<ReturnType<typeof ledger.propose>>
      await act(async () => {
        await ledger.completeInput('item', '以后叫你小枝小枝')
        expect(ledger.bindResponse('response', 'item')).toBe(true)
        receipt = await ledger.propose('response', 'call', 'runtime_voice_configure_wake_word', JSON.stringify({ args: { candidate: '小枝小枝' } }))
      })
      expect({ execution: receipt.execution, public_result: receipt.public_result, operations }).toMatchObject({ execution: { status: 'completed' } })
      expect(operations.some(item => item.operation === 'client' && item.phase === 'claim')).toBe(true)
      expect(operations.some(item => item.operation === 'client' && item.phase === 'complete')).toBe(true)
      expect((await new VoiceSettingsStore(directory).loadWakeWord(child))?.wakeWord).toBe('小知')
      await waitFor(() => expect(microphone?.autoStart).toBe(true))
      vi.mocked(localApi.voiceInteraction).mockResolvedValue(decision({ route: 'onboarding', action: 'confirm_name', candidate: '小枝小枝' }))
      nativeRuntime = undefined
      await say('确认')
      expect((await new VoiceSettingsStore(directory).loadWakeWord(child))?.wakeWord).toBe('小枝小枝')
      await waitFor(() => expect(nativeRuntime).toBeDefined())
      view.unmount()
      nativeRuntime = undefined; nativeWakeWord = undefined
      await act(async () => { view = render(<SessionWorkspace initial={{ ...initial, session: { ...initial.session, session_id: sid, child_id: child } }} childName="合成孩子" childGrade="初三" onBack={vi.fn()} />) })
      await waitFor(() => expect(nativeWakeWord).toBe('小枝小枝'))
      view.unmount(); gateway.close(sender, host)
    } finally {
      server.kill('SIGTERM')
      await new Promise<void>(resolve => {
        if (server.exitCode !== null) { resolve(); return }
        const timer = setTimeout(() => { server.kill('SIGKILL'); resolve() }, 3000)
        timer.unref()
        server.once('exit', () => { clearTimeout(timer); resolve() })
      })
    }
  }, 25_000)

})
