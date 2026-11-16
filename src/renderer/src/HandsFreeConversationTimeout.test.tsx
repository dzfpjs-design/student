import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { HandsFreeVoiceInput } from './LocalInputs'
import { localApi } from './api'
import * as voice from './companionVoice'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import { clearVoiceCaptureContext, setVoiceQuestionCapture } from './voiceCaptureContext'
import { setTimeout as realDelay } from 'node:timers/promises'

let processor: { connect: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn>; onaudioprocess: ((event: AudioProcessingEvent) => void) | null }
let voiceState: voice.CompanionVoiceState
const subscribers = new Set<(state: voice.CompanionVoiceState) => void>()
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const settleCapturePipeline = async (): Promise<void> => {
  await act(async () => {
    for (let index = 0; index < 4; index += 1) await Promise.resolve()
  })
}
const mediaDescriptor = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')

beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(100_000); resetVoiceCaptureCoordinatorForTests()
  voiceState = 'idle'; subscribers.clear()
  vi.spyOn(voice, 'getCompanionVoiceState').mockImplementation(() => voiceState)
  vi.spyOn(voice, 'subscribeCompanionVoice').mockImplementation((handler) => {
    subscribers.add(handler); return () => { subscribers.delete(handler) }
  })
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('UNEXPECTED_NETWORK')))
  processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null }
  vi.stubGlobal('AudioContext', class {
    sampleRate = 16_000
    createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
    createScriptProcessor = () => processor
    close = vi.fn().mockResolvedValue(undefined)
  })
  const track = { stop: vi.fn(), getSettings: () => ({}), getCapabilities: () => ({}) }
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] }),
    enumerateDevices: vi.fn().mockResolvedValue([]),
  } })
  vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'timeout-test', status: 'active', expires_at: 'later' })
  vi.spyOn(localApi, 'renewDeviceLease').mockImplementation(async leaseId => ({ lease_id: leaseId, status: 'active', expires_at: new Date(Date.now() + 180_000).toISOString() }))
  vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'timeout-test', status: 'released' })
  vi.spyOn(localApi, 'transcribe').mockResolvedValue({ transcript: '我的答案是四', status: 'completed', cloud_used: true } as never)
})
afterEach(() => {
  cleanup(); clearVoiceCaptureContext(); resetVoiceCaptureCoordinatorForTests(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers()
  if (mediaDescriptor) Object.defineProperty(navigator, 'mediaDevices', mediaDescriptor)
})

async function mount(activate = true, childId?: string, wakeWord = '小知') {
  const onTranscript = vi.fn()
  const props = { sessionId: 'timeout-test', childId, purpose: 'agent_command', wakeWord, inactivitySeconds: 30, autoStart: true, onTranscript }
  const view = render(<HandsFreeVoiceInput {...props} externalActivationSignal={0} />)
  await advance(20)
  // WebCrypto runs outside the fake clock. Do not start capture deadlines until
  // the actual device registration has completed, even under suite contention.
  await act(async () => {
    for (let n=0;n<400 && !vi.mocked(localApi.registerDevice).mock.calls.length;n++) await realDelay(5)
  })
  expect(localApi.registerDevice).toHaveBeenCalledOnce()
  if (activate) { view.rerender(<HandsFreeVoiceInput {...props} externalActivationSignal={1} />); await advance(20) }
  return { view, props, onTranscript }
}
function listening() { expect(screen.getByRole('button', { name: /^正在听 · 连续对话(?: · \d+句处理中)?$/ })).toBeEnabled() }
async function frames(count: number) {
  for (let i = 0; i < count; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(256)
      processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => new Float32Array(4096).fill(.05) } } as unknown as AudioProcessingEvent)
    })
  }
}

it('waits for real asynchronous device hashing before testing the fake idle deadline', async () => {
  const digest=crypto.subtle.digest.bind(crypto.subtle)
  vi.spyOn(crypto.subtle,'digest').mockImplementation(async (algorithm,data) => {
    await realDelay(30);return digest(algorithm,data)
  })
  const {onTranscript}=await mount(false,undefined,'')
  listening();await frames(4);await advance(1_400);await settleCapturePipeline()
  expect(localApi.transcribe).toHaveBeenCalledOnce();expect(onTranscript).toHaveBeenCalledOnce()
})

it.each([10_700, 11_600])('preserves the whole late-starting phrase and its original question across the idle capture deadline (%ims)', async startAt => {
  const child = 'synthetic-capture-child'
  const first = { kind: 'diagnostic' as const, binding: { prompt_id: 'original-question', item_version: 'v1' } }
  const later = { kind: 'diagnostic' as const, binding: { prompt_id: 'later-question', item_version: 'v2' } }
  const { onTranscript } = await mount(false, child, '')
  setVoiceQuestionCapture('timeout-test', child, first)
  await advance(startAt); await frames(1)
  // A new page/question arrives while the original answer is still spoken.
  setVoiceQuestionCapture('timeout-test', child, later)
  await frames(7)
  expect(localApi.transcribe).not.toHaveBeenCalled()
  await advance(1_400); await settleCapturePipeline()
  expect(localApi.transcribe).toHaveBeenCalledOnce()
  const call = vi.mocked(localApi.transcribe).mock.calls[0]
  expect(call[5]).toEqual({ child_id: child, question_reference: first })
  const wav = Uint8Array.from(atob(call[2]), value => value.charCodeAt(0))
  expect(wav.length).toBe(44 + 8 * 4096 * 2)
  expect(onTranscript).toHaveBeenCalledOnce()
  await frames(4); await advance(1_400); await settleCapturePipeline()
  expect(localApi.transcribe).toHaveBeenCalledTimes(2)
  expect(vi.mocked(localApi.transcribe).mock.calls[1][5]).toEqual({ child_id: child, question_reference: later })
  expect(onTranscript).toHaveBeenCalledTimes(2)
})

it('discards an unconfirmed onset at the wait deadline and captures the next real phrase independently', async () => {
  await mount(false, undefined, '')
  await advance(11_600); await frames(1); await advance(2_000)
  expect(localApi.transcribe).not.toHaveBeenCalled()
  await frames(4); await advance(1_400); await settleCapturePipeline()
  expect(localApi.transcribe).toHaveBeenCalledOnce()
  const wav = atob(vi.mocked(localApi.transcribe).mock.calls[0][2])
  expect(wav.length).toBe(44 + 4 * 4096 * 2)
})

it('keeps a long child utterance across the idle deadline, then returns only after silence', async () => {
  const { onTranscript } = await mount()
  await advance(28_000); await frames(16)
  listening()
  await advance(1_400); await settleCapturePipeline()
  expect(onTranscript).toHaveBeenCalledOnce()
  await advance(28_000); listening()
  await frames(4); await advance(1_400); await settleCapturePipeline()
  expect(onTranscript).toHaveBeenCalledTimes(2)
  await advance(29_000); listening()
  await advance(2_000)
  expect(screen.getByRole('button', { name: '正在听唤醒词 · 小知' })).toBeEnabled()
})

it('does not enter wake standby while the captured utterance is waiting for ASR', async () => {
  let resolve!: (value: Awaited<ReturnType<typeof localApi.transcribe>>) => void
  vi.mocked(localApi.transcribe).mockImplementation(() => new Promise(done => { resolve = done }))
  const { onTranscript } = await mount()
  await advance(26_000); await frames(4); await advance(1_400); await settleCapturePipeline()
  expect(localApi.transcribe).toHaveBeenCalledOnce()
  await advance(40_000); listening()
  await act(async () => resolve({ transcript: '我的答案是四', status: 'completed', cloud_used: true } as never))
  expect(onTranscript).toHaveBeenCalledOnce()
  await advance(29_000); listening()
})

it('keeps the reply window open through a long companion explanation', async () => {
  await mount(); await advance(28_000)
  await act(async () => { voiceState = 'speaking'; subscribers.forEach(handler => handler(voiceState)) })
  await advance(40_000); listening()
  await act(async () => { voiceState = 'idle'; subscribers.forEach(handler => handler(voiceState)) })
  await advance(29_000); listening()
  await advance(2_000)
  expect(screen.getByRole('button', { name: '正在听唤醒词 · 小知' })).toBeEnabled()
})

it('keeps the reply window while Agent work is in flight and resets when it finishes', async () => {
  const { view, props } = await mount(); await advance(28_000)
  view.rerender(<HandsFreeVoiceInput {...props} externalActivationSignal={1} interactionBusy />)
  await advance(40_000); listening()
  view.rerender(<HandsFreeVoiceInput {...props} externalActivationSignal={1} interactionBusy={false} />)
  await advance(29_000); listening()
  await advance(2_000)
  expect(screen.getByRole('button', { name: '正在听唤醒词 · 小知' })).toBeEnabled()
})

it('does not wake a standby conversation because the agent is busy', async () => {
  const { view, props } = await mount(false)
  view.rerender(<HandsFreeVoiceInput {...props} externalActivationSignal={0} interactionBusy />)
  await advance(40_000)
  expect(screen.getByRole('button', { name: '正在听唤醒词 · 小知' })).toBeEnabled()
})

it('preserves continuous dialogue when it replaces a failed realtime transport', async () => {
  render(<HandsFreeVoiceInput
    sessionId="timeout-test"
    purpose="agent_command"
    wakeWord="小知"
    inactivitySeconds={30}
    autoStart
    initialConversationActive
    onTranscript={() => undefined}
  />)
  await advance(20)
  await vi.waitFor(() => listening(), { timeout: 1_000 })
})

it('does not let ongoing work reactivate an explicit privacy pause', async () => {
  const { view, props } = await mount()
  view.rerender(<HandsFreeVoiceInput {...props} externalActivationSignal={1} interactionBusy />)
  await act(async () => window.dispatchEvent(new Event('hsc-privacy-pause')))
  await advance(40_000)
  expect(screen.queryByRole('button', { name: '正在听 · 连续对话' })).toBeNull()
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce()
})
