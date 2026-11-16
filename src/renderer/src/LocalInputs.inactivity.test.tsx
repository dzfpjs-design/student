import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { HandsFreeVoiceInput, cancelLocalSpeech, asrFailureReason } from './LocalInputs'
import { ApiError, localApi } from './api'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'

const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
let processor: { onaudioprocess: ((event: AudioProcessingEvent) => void) | null }
let transcribe: ReturnType<typeof vi.spyOn>
let ended: (() => void) | undefined
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-28T12:00:00Z'))
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  const track = { readyState: 'live', enabled: true, stop: vi.fn(), getSettings: () => ({ deviceId: 'synthetic-mic' }),
    getCapabilities: () => ({}), addEventListener: vi.fn((event: string, callback: () => void) => { if (event === 'ended') ended = callback }), removeEventListener: vi.fn() }
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] }
  vi.stubGlobal('AudioContext', class {
    sampleRate = 16_000; state = 'running'; currentTime = 0
    createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
    createScriptProcessor = () => (processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null } as never)
    close = vi.fn().mockResolvedValue(undefined)
  })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    getUserMedia: vi.fn().mockResolvedValue(stream), enumerateDevices: vi.fn().mockResolvedValue([]),
  } })
  vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'synthetic-lease', status: 'active', expires_at: '2027-01-01T00:00:00Z' })
  vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'synthetic-lease', status: 'released' })
  vi.spyOn(localApi, 'renewDeviceLease').mockResolvedValue({ lease_id: 'synthetic-lease', status: 'active', expires_at: '2027-01-01T00:00:00Z' })
  transcribe = vi.spyOn(localApi, 'transcribe').mockRejectedValue(new ApiError('synthetic non-speech', 409, 'ASR_NO_MEANINGFUL_SPEECH'))
  vi.spyOn(localApi, 'transcribeStandby').mockRejectedValue(new ApiError('synthetic non-speech', 409, 'ASR_NO_MEANINGFUL_SPEECH'))
})
afterEach(() => {
  cleanup(); cancelLocalSpeech(); resetVoiceCaptureCoordinatorForTests()
  vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers()
  if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
})

async function fixture(options?: { wakeWord?: string }) {
  const status = vi.fn(), dispatched = vi.fn(), woke = vi.fn()
  render(<HandsFreeVoiceInput sessionId="synthetic-session" purpose="agent_command" wakeWord={options ? options.wakeWord : '小知'}
    autoStart initialConversationActive inactivitySeconds={30} onTranscript={dispatched} onWake={woke} onRuntimeStatusChange={status} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  return { status, dispatched, woke }
}

it('quiet control returns to wake standby after inactivity', async () => {
  const { status, dispatched } = await fixture()
  await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
  expect(status.mock.lastCall?.[0].phase).toBe('wake_standby')
  expect(dispatched).not.toHaveBeenCalled()
  expect(transcribe).not.toHaveBeenCalled()
})

it('unaccepted background VAD and rejected ASR expire to standby without dispatching residual queued speech', async () => {
  const { status, dispatched } = await fixture()
  const chunk = Float32Array.from({ length: 512 }, (_, i) => 0.1 * Math.sin(i * 2 * Math.PI * 220 / 16000))
  const silence = new Float32Array(512)
  await act(async () => {
    for (let cycle = 0; cycle < 40; cycle += 1) {
      for (let frame = 0; frame < 8; frame += 1) {
        processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => chunk } } as unknown as AudioProcessingEvent)
        await vi.advanceTimersByTimeAsync(32)
      }
      for (let frame = 0; frame < 55; frame += 1) {
        processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => silence } } as unknown as AudioProcessingEvent)
        await vi.advanceTimersByTimeAsync(32)
      }
    }
  })
  expect(Date.now()).toBeGreaterThan(new Date('2026-10-28T12:01:00Z').getTime())
  expect(transcribe.mock.calls.length + vi.mocked(localApi.transcribeStandby).mock.calls.length).toBeGreaterThan(20)
  expect(dispatched).not.toHaveBeenCalled()
  expect(status.mock.lastCall?.[0].phase).toBe('wake_standby')
  expect(localApi.transcribeStandby).toHaveBeenCalled()
})

async function emitUtterance() {
  const voice = Float32Array.from({ length: 512 }, (_, i) => 0.1 * Math.sin(i * 2 * Math.PI * 220 / 16000))
  const silence = new Float32Array(512)
  await act(async () => {
    for (let frame = 0; frame < 8; frame += 1) {
      processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => voice } } as unknown as AudioProcessingEvent)
      await vi.advanceTimersByTimeAsync(32)
    }
    for (let frame = 0; frame < 40; frame += 1) {
      processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => silence } } as unknown as AudioProcessingEvent)
      await vi.advanceTimersByTimeAsync(32)
    }
  })
}

it('keeps the original pending answer across the deadline and extends activity only on accepted dispatch', async () => {
  let resolve!: (value: unknown) => void
  transcribe.mockImplementationOnce(() => new Promise(done => { resolve = done }))
  const { status, dispatched } = await fixture()
  await act(async () => { await vi.advanceTimersByTimeAsync(29_000) })
  await emitUtterance()
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
  expect(status.mock.lastCall?.[0].phase).toBe('listening')
  expect(status.mock.lastCall?.[0].pendingUtterances).toBe(1)
  await act(async () => {
    resolve({ status: 'completed', cloud_used: true, transcript: '我想先学数学', voice_input_id: 'synthetic-input' })
    await vi.advanceTimersByTimeAsync(1)
  })
  expect(dispatched).toHaveBeenCalledExactlyOnceWith('我想先学数学', undefined, undefined, undefined, undefined, null)
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  expect(status.mock.lastCall?.[0].phase).toBe('listening')
  await act(async () => { await vi.advanceTimersByTimeAsync(16_000) })
  expect(status.mock.lastCall?.[0].phase).toBe('wake_standby')
})

it('automatic microphone reconnection preserves the original activity deadline', async () => {
  const { status, dispatched } = await fixture()
  await act(async () => { await vi.advanceTimersByTimeAsync(29_500); ended?.(); await vi.advanceTimersByTimeAsync(2_000) })
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
  expect(status.mock.lastCall?.[0].phase).toBe('wake_standby')
  expect(dispatched).not.toHaveBeenCalled()
})

it('hard ASR failure is visibly unavailable until a genuine completed recognition restores health', async () => {
  transcribe.mockRejectedValueOnce(new ApiError('synthetic provider unavailable', 503, 'ASR_CLOUD_DIALOGUE_UNAVAILABLE'))
    .mockResolvedValueOnce({ status: 'completed', cloud_used: true, transcript: '我想先学数学', voice_input_id: 'synthetic-input' })
  const { status, dispatched } = await fixture()
  await emitUtterance()
  expect(status.mock.lastCall?.[0]).toMatchObject({ phase: 'error', detail: expect.any(String) })
  expect(screen.getByRole('button', { name: '语音识别暂不可用 · 正在重试' })).toBeEnabled()
  cancelLocalSpeech()
  await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
  await emitUtterance()
  expect(dispatched).toHaveBeenCalledOnce()
  expect(status.mock.lastCall?.[0].phase).toBe('listening')
  expect(screen.queryByRole('button', { name: '语音识别暂不可用 · 正在重试' })).toBeNull()
})

it('drains two original conversation ASR requests before entering standby and never dispatches later unaddressed standby speech', async () => {
  const reject: Array<(reason: unknown) => void> = []
  transcribe.mockImplementation(() => new Promise((_done, fail) => { reject.push(fail) }))
  const { status, dispatched } = await fixture()
  await emitUtterance()
  await emitUtterance()
  expect(transcribe).toHaveBeenCalledTimes(1)
  expect(status.mock.lastCall?.[0].pendingUtterances).toBe(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
  expect(status.mock.lastCall?.[0].phase).toBe('listening')
  await act(async () => { reject[0](new ApiError('synthetic non-speech', 409, 'ASR_NO_MEANINGFUL_SPEECH')); await vi.advanceTimersByTimeAsync(1) })
  expect(transcribe).toHaveBeenCalledTimes(2)
  expect(status.mock.lastCall?.[0].pendingUtterances).toBe(1)
  expect(status.mock.lastCall?.[0].phase).toBe('listening')
  await act(async () => { reject[1](new ApiError('synthetic non-speech', 409, 'ASR_NO_MEANINGFUL_SPEECH')); await vi.advanceTimersByTimeAsync(1) })
  expect(status.mock.lastCall?.[0].pendingUtterances).toBe(0)
  expect(status.mock.lastCall?.[0].phase).toBe('wake_standby')
  vi.mocked(localApi.transcribeStandby).mockResolvedValue({ status: 'completed', transcript: '没有叫唤醒词的背景话', cloud_used: false } as never)
  await emitUtterance()
  expect(transcribe).toHaveBeenCalledTimes(2)
  expect(localApi.transcribeStandby).toHaveBeenCalledOnce()
  expect(dispatched).not.toHaveBeenCalled()
})

it('maps an arbitrary client Error name to one finite diagnostic code', () => {
  const cause = new Error('synthetic private provider detail')
  cause.name = 'VendorSecretAndUnboundedName'
  expect(asrFailureReason(cause)).toEqual({ reason_code: 'ASR_CLIENT_UNKNOWN', failure_kind: 'client' })
})

it('completed local recognition is healthy without asserting a network failure or an upload history', async () => {
  transcribe.mockResolvedValueOnce({ status: 'completed', cloud_used: false, transcript: '我想先学数学', voice_input_id: 'synthetic-local-input' })
  const { status, dispatched } = await fixture()
  await emitUtterance()
  expect(dispatched).toHaveBeenCalledOnce()
  expect(status.mock.lastCall?.[0].phase).toBe('listening')
  expect(status.mock.lastCall?.[0].detail).toBeUndefined()
  expect(screen.queryByText(/网络语音识别暂时不可用|没有上传|未上传/)).toBeNull()
})

it.each(['小知', '小 智！'])('registered bare wake acknowledges locally in an active conversation without dispatch or stuck pending: %s', async (transcript) => {
  transcribe.mockResolvedValueOnce({ status: 'completed', cloud_used: false, transcript, voice_input_id: 'synthetic-bare-wake' })
  const { status, dispatched, woke } = await fixture()
  await emitUtterance()
  expect(woke).toHaveBeenCalledOnce()
  expect(dispatched).not.toHaveBeenCalled()
  expect(status.mock.lastCall?.[0]).toMatchObject({ phase: 'listening', pendingUtterances: 0 })
})

it.each([
  { wakeWord: '小伴', transcript: '小知' },
  { wakeWord: undefined, transcript: '小知' },
  { wakeWord: '小知', transcript: '小知，开始数学' },
])('does not intercept another child name, an unregistered name, or a wake phrase with a command: %j', async ({ wakeWord, transcript }) => {
  transcribe.mockResolvedValueOnce({ status: 'completed', cloud_used: false, transcript, voice_input_id: 'synthetic-ordinary-speech' })
  const { status, dispatched, woke } = await fixture({ wakeWord })
  await emitUtterance()
  expect(woke).not.toHaveBeenCalled()
  expect(dispatched).toHaveBeenCalledExactlyOnceWith(transcript, undefined, undefined, undefined, undefined, null)
  expect(status.mock.lastCall?.[0].pendingUtterances).toBe(0)
})

it('a registered wake prefix keeps an immediate privacy stop ahead of wake acknowledgement', async () => {
  transcribe.mockResolvedValueOnce({ status: 'completed', cloud_used: false, transcript: '小知，停止全部监听', voice_input_id: 'synthetic-privacy-stop' })
  const { status, dispatched, woke } = await fixture()
  await emitUtterance()
  expect(woke).not.toHaveBeenCalled()
  expect(dispatched).toHaveBeenCalledExactlyOnceWith('停止全部监听', undefined, undefined, undefined, undefined, null)
  expect(status.mock.lastCall?.[0].pendingUtterances).toBe(0)
})
