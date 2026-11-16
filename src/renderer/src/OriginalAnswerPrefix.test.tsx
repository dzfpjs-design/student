import { act, cleanup, render } from '@testing-library/react'
import { setTimeout as realDelay } from 'node:timers/promises'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { AdaptiveVoiceActivityDetector, HandsFreeVoiceInput, companionPlaybackCaptureCooldownMs, pcmCaptureBlockSamples } from './LocalInputs'
import * as voice from './companionVoice'
import { clearVoiceCaptureContext, trackDiagnosticCapture } from './voiceCaptureContext'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import type { DiagnosticPromptResult } from './types'

const mediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
const question = (id = 'original', oral = true) => ({ status: 'waiting_answer', prompt: {
  prompt_id: id, item_version: 'v-' + id, child_id: 'a', origin_session_id: 's',
  item: { track: 'pet', answer_type: oral ? 'speaking' : 'single_choice', assessment_mode: 'pet_reference_rubric' },
} }) as DiagnosticPromptResult
afterEach(() => {
  cleanup(); clearVoiceCaptureContext(); resetVoiceCaptureCoordinatorForTests()
  vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); localStorage.clear()
  if (mediaDevices) Object.defineProperty(navigator, 'mediaDevices', mediaDevices)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
})

async function capture(oral = true, systemAec = false, sampleRate = 16_000, standby = false) {
  vi.useFakeTimers()
  let state: ReturnType<typeof voice.getCompanionVoiceState> = 'idle'
  vi.spyOn(voice, 'getCompanionVoiceState').mockImplementation(() => state)
  const processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as null | ((event: AudioProcessingEvent) => void) }
  let echoMode: unknown = true
  const applyConstraints = vi.fn(async () => { echoMode = 'all' })
  const stop = vi.fn(), track = { stop, getSettings: () => ({ deviceId: 'original-mic', echoCancellation: echoMode }),
    getCapabilities: () => ({ echoCancellation: systemAec ? ['all', true, false] : [true, false] }), applyConstraints }
  let blockSamples = 0
  vi.stubGlobal('AudioContext', class {
    sampleRate = sampleRate
    createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
    createScriptProcessor = (samples: number) => { blockSamples = samples; return processor }
    close = vi.fn().mockResolvedValue(undefined)
  })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] }),
    enumerateDevices: vi.fn().mockResolvedValue([]),
  } })
  vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'prefix', status: 'active', expires_at: 'later' })
  vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'prefix', status: 'released' })
  const transcribe = vi.spyOn(localApi, 'transcribe').mockResolvedValue({ status: 'completed', cloud_used: true,
    transcript: 'I enjoy reading books.', voice_input_id: 'original-voice', capture_binding: { status: 'completed' } } as never)
  const transcribeStandby = vi.spyOn(localApi, 'transcribeStandby').mockResolvedValue({ status: 'completed',
    cloud_used: true, transcript: '嗯', persisted: false } as never)
  await trackDiagnosticCapture('s', async () => question('original', oral))
  const status = vi.fn(), onTranscript = vi.fn()
  const view = render(<HandsFreeVoiceInput sessionId="s" childId="a" purpose="agent_command"
    autoStart initialConversationActive={!standby} wakeWord={standby ? '小知' : undefined}
    onTranscript={onTranscript} onRuntimeStatusChange={status} />)
  const readyPhase = standby ? 'wake_standby' : 'listening'
  for (let i = 0; i < 100 && status.mock.lastCall?.[0].phase !== readyPhase; i += 1) {
    await act(async () => { await vi.advanceTimersByTimeAsync(1); await realDelay(2) })
  }
  expect(processor.onaudioprocess).toBeTypeOf('function')
  expect(status.mock.lastCall?.[0].phase).toBe(readyPhase)
  const frame = async (amplitude = 0.1, milliseconds = 256, samples = 4096) => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(milliseconds)
      const chunk = Float32Array.from({ length: samples }, (_, i) => amplitude * Math.sin((i + 1) * 2 * Math.PI * 180 / sampleRate))
      processor.onaudioprocess!({ inputBuffer: { getChannelData: () => chunk } } as unknown as AudioProcessingEvent)
    })
  }
  const silence = async (milliseconds = 1200) => { await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds) }) }
  const bytes = () => Uint8Array.from(atob(transcribe.mock.calls[0][2]), character => character.charCodeAt(0))
  return { state: (value: typeof state) => { state = value }, frame, silence, transcribe, transcribeStandby, status, onTranscript, view, bytes, stop, applyConstraints,
    blockSamples, nativeFrame: (amplitude: number) => frame(amplitude, blockSamples / sampleRate * 1000, blockSamples) }
}

it.each([16_000, 44_100, 48_000])('captures a 320ms original answer with the actual %iHz processor block size', async sampleRate => {
  const input = await capture(true, false, sampleRate)
  expect(input.blockSamples / sampleRate * 1000).toBeLessThanOrEqual(32)
  const frames = Math.ceil(sampleRate * 0.32 / input.blockSamples)
  for (let i = 0; i < frames; i += 1) await input.nativeFrame(0.1)
  expect(input.status.mock.lastCall?.[0].speechActive).toBe(true)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledOnce()
  const data = input.bytes(), duration = (data.length - 44) / 2 / 16000
  expect(duration).toBeGreaterThanOrEqual(0.32)
  expect(duration).toBeLessThan(0.36)
  expect(input.transcribe.mock.calls[0][5]?.question_reference).toEqual({ kind: 'diagnostic', binding: { prompt_id: 'original', item_version: 'v-original' } })
})

it.each(['嗯', '嗯嗯', '哦？', '啊'])('dispatches the original dialogue marker %s instead of silently filtering it', async transcript => {
  const input = await capture()
  input.transcribe.mockResolvedValue({ status: 'completed', cloud_used: true,
    transcript, voice_input_id: 'original-voice', capture_binding: { status: 'completed' } } as never)
  for (let i = 0; i < 10; i += 1) await input.nativeFrame(.1)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledOnce()
  expect(input.onTranscript).toHaveBeenCalledWith(transcript, undefined, 'original-voice', undefined, undefined, null)
})

it.each(['嗯', '哦？', '', '，。'])('keeps standby marker %s out of the active dialogue', async transcript => {
  const input = await capture(true, false, 16000, true)
  input.transcribeStandby.mockResolvedValue({ status: 'completed', cloud_used: true,
    transcript, persisted: false } as never)
  for (let i = 0; i < 10; i += 1) await input.nativeFrame(.1)
  await input.silence()
  expect(input.transcribeStandby).toHaveBeenCalledOnce()
  expect(input.transcribe).not.toHaveBeenCalled()
  expect(input.onTranscript).not.toHaveBeenCalled()
})

it('requires duration as well as consecutive blocks instead of accepting three tiny impulses', () => {
  const detector = new AdaptiveVoiceActivityDetector()
  for (let i = 0; i < 9; i += 1) expect(detector.observe(0.03, 10).speechDetected).toBe(false)
  expect(detector.observe(0.03, 10).speechDetected).toBe(true)
  detector.reset()
  expect(detector.observe(0.03, 300).speechDetected).toBe(false)
  detector.observe(0, 10)
  for (let i = 0; i < 3; i += 1) expect(detector.observe(0.03, 10).speechDetected).toBe(false)
})

it.each([8000, 16000, 22050, 32000, 44100, 48000, 96000, 192000])('uses a supported power-of-two buffer with at most 32ms at %iHz', sampleRate => {
  const block = pcmCaptureBlockSamples(sampleRate)
  expect([256, 512, 1024, 2048, 4096]).toContain(block)
  expect(block / sampleRate * 1000).toBeLessThanOrEqual(32)
})

it.each([0, NaN, Infinity, -1])('invalid audio rate %s cannot request an invalid native buffer', sampleRate => {
  expect(pcmCaptureBlockSamples(sampleRate)).toBe(1024)
})

it.each([0, NaN, Infinity, -1])('invalid block duration %s cannot confirm a speech candidate', duration => {
  const detector = new AdaptiveVoiceActivityDetector()
  for (let i = 0; i < 10; i += 1) expect(detector.observe(.1, duration).speechDetected).toBe(false)
})

it('freezes the original question on the first small block, before later confirmation', async () => {
  const input = await capture()
  await input.nativeFrame(.1)
  expect(input.status.mock.lastCall?.[0].speechActive).toBe(false)
  await act(async () => { await trackDiagnosticCapture('s', async () => question('changed')) })
  for (let i = 1; i < 10; i += 1) await input.nativeFrame(.1)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledOnce()
  expect(input.bytes().length).toBe(44 + 10 * 512 * 2)
  expect(input.transcribe.mock.calls[0][5]?.question_reference).toEqual({ kind: 'diagnostic', binding: { prompt_id: 'original', item_version: 'v-original' } })
})

it('does not revive a confirmed short answer after privacy stop or a late native frame', async () => {
  const input = await capture()
  for (let i = 0; i < 10; i += 1) await input.nativeFrame(.1)
  expect(input.status.mock.lastCall?.[0].speechActive).toBe(true)
  await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
  await input.nativeFrame(.1); await input.silence()
  expect(input.stop).toHaveBeenCalledOnce()
  expect(input.transcribe).not.toHaveBeenCalled()
  expect(input.status.mock.lastCall?.[0].speechActive).toBe(false)
})

it.each([16_000, 44_100, 48_000])('does not dispatch a sub-96ms candidate or attach it to the next %iHz answer', async sampleRate => {
  const input = await capture(true, false, sampleRate)
  for (let i = 0; i < 3; i += 1) await input.frame(0.1, 10, Math.floor(sampleRate * .01))
  expect(input.status.mock.lastCall?.[0].speechActive).toBe(false)
  await input.nativeFrame(0); await input.silence()
  expect(input.transcribe).not.toHaveBeenCalled()
  await act(async () => { await trackDiagnosticCapture('s', async () => question('next')) })
  for (let i = 0; i < Math.ceil(sampleRate * .32 / input.blockSamples); i += 1) await input.nativeFrame(0.1)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledOnce()
  expect(input.transcribe.mock.calls[0][5]?.question_reference).toEqual({ kind: 'diagnostic', binding: { prompt_id: 'next', item_version: 'v-next' } })
})

it.each([16_000, 48_000])('bounds silent preroll by time and retains every voiced block at %iHz', async sampleRate => {
  const input = await capture(true, false, sampleRate)
  for (let i = 0; i < Math.ceil(sampleRate * 3 / input.blockSamples); i += 1) await input.nativeFrame(0)
  const frames = Math.ceil(sampleRate * .32 / input.blockSamples)
  for (let i = 0; i < frames; i += 1) await input.nativeFrame(0.1)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledOnce()
  const data = input.bytes(), duration = (data.length - 44) / 2 / 16000
  expect(duration).toBeGreaterThanOrEqual(1.4 + .32 - .001)
  expect(duration).toBeLessThan(1.76)
  expect(new DataView(data.buffer).getInt16(44, true)).toBe(0)
})

it('negotiates system AEC in the actual original-answer recorder without weakening playback suppression', async () => {
  const input = await capture(true, true)
  expect(input.applyConstraints).toHaveBeenCalledExactlyOnceWith({ echoCancellation: { exact: 'all' } })
  input.state('speaking'); for (let i = 0; i < 4; i += 1) await input.frame(0.8)
  expect(input.transcribe).not.toHaveBeenCalled()
  input.state('idle'); await input.silence(companionPlaybackCaptureCooldownMs + 1)
  for (let i = 0; i < 3; i += 1) await input.frame(0.1)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledOnce()
  expect(input.bytes().length).toBe(44 + 3 * 4096 * 2)
})

it('retains the first post-playback samples of a continuing original answer, but requires three unblocked frames', async () => {
  const input = await capture()
  input.state('speaking'); await input.frame(0.8)
  input.state('idle')
  for (let i = 0; i < 3; i += 1) await input.frame(0.03)
  expect(input.status.mock.lastCall?.[0].speechActive).toBe(false)
  expect(input.transcribe).not.toHaveBeenCalled()
  await input.frame(0.1); expect(input.status.mock.lastCall?.[0].speechActive).toBe(false)
  await input.frame(0.1); expect(input.status.mock.lastCall?.[0].speechActive).toBe(false)
  await input.frame(0.1); expect(input.status.mock.lastCall?.[0].speechActive).toBe(true)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledTimes(1)
  const data = input.bytes(), header = new DataView(data.buffer)
  expect(data.length).toBe(44 + 6 * 4096 * 2)
  expect(header.getInt16(44, true)).toBe(Math.trunc(0.03 * Math.sin(2 * Math.PI * 180 / 16000) * 0x7fff))
  expect(header.getInt16(44 + 3 * 4096 * 2, true)).toBe(Math.trunc(0.1 * Math.sin(2 * Math.PI * 180 / 16000) * 0x7fff))
  expect(input.transcribe.mock.calls[0][5]?.question_reference).toEqual({ kind: 'diagnostic', binding: { prompt_id: 'original', item_version: 'v-original' } })
  expect(input.onTranscript).toHaveBeenCalledWith('I enjoy reading books.', undefined, 'original-voice', undefined, undefined, null)
})

it('does not cut a buffered onset when the old idle polling window expires', async () => {
  const input = await capture()
  await input.silence(11_300)
  input.state('speaking'); await input.frame(0.8)
  input.state('idle'); for (let i = 0; i < 3; i += 1) await input.frame(0.03)
  for (let i = 0; i < 3; i += 1) await input.frame(0.1)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledTimes(1)
  expect(input.bytes().length).toBe(44 + 6 * 4096 * 2)
  expect(new DataView(input.bytes().buffer).getInt16(44, true)).toBe(Math.trunc(0.03 * Math.sin(2 * Math.PI * 180 / 16000) * 0x7fff))
})

it('does not admit isolated playback-tail audio without confirmed speech outside the window', async () => {
  const input = await capture()
  input.state('speaking'); await input.frame(0.8)
  input.state('idle'); for (let i = 0; i < 3; i += 1) await input.frame(0.03)
  await input.frame(0); await input.silence(12_100)
  expect(input.transcribe).not.toHaveBeenCalled()
  expect(input.status.mock.lastCall?.[0]).toMatchObject({ speechActive: false, capturePending: false })
})

it.each([1, 2])('discards a prefix after only %i unblocked candidate frames and never attaches it to the next answer', async count => {
  const input = await capture()
  input.state('speaking'); await input.frame(0.8)
  input.state('idle'); for (let i = 0; i < 3; i += 1) await input.frame(0.03)
  for (let i = 0; i < count; i += 1) await input.frame(0.1)
  expect(input.status.mock.lastCall?.[0].speechActive).toBe(false)
  await input.silence()
  expect(input.transcribe).not.toHaveBeenCalled()
  expect(input.status.mock.lastCall?.[0].capturePending).toBe(false)
  await act(async () => { await trackDiagnosticCapture('s', async () => question('next')) })
  for (let i = 0; i < 3; i += 1) await input.frame(0.1)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledTimes(1)
  expect(input.bytes().length).toBe(44 + 3 * 4096 * 2)
  expect(input.transcribe.mock.calls[0][5]?.question_reference).toEqual({ kind: 'diagnostic', binding: { prompt_id: 'next', item_version: 'v-next' } })
})

it('keeps ordinary dialogue on the existing echo mute policy', async () => {
  const input = await capture(false)
  input.state('speaking'); await input.frame(0.8)
  input.state('idle'); for (let i = 0; i < 3; i += 1) await input.frame(0.03)
  for (let i = 0; i < 3; i += 1) await input.frame(0.1)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledTimes(1)
  expect(input.bytes().length).toBe(44 + 3 * 4096 * 2)
})

it('freezes original ownership in the first buffered candidate instead of relabelling later UI changes', async () => {
  const input = await capture()
  input.state('speaking'); await input.frame(0.8)
  input.state('idle'); await input.frame(0.03)
  await act(async () => { await trackDiagnosticCapture('s', async () => question('newer')) })
  for (let i = 0; i < 2; i += 1) await input.frame(0.03)
  for (let i = 0; i < 3; i += 1) await input.frame(0.1)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledTimes(1)
  expect(input.transcribe.mock.calls[0][5]?.question_reference).toEqual({ kind: 'diagnostic', binding: { prompt_id: 'original', item_version: 'v-original' } })
  expect(input.bytes().length).toBe(44 + 6 * 4096 * 2)
})

it('clears a buffered prefix when playback starts again and does not combine two responses', async () => {
  const input = await capture()
  input.state('speaking'); await input.frame(0.8)
  input.state('idle'); for (let i = 0; i < 3; i += 1) await input.frame(0.03)
  input.state('speaking'); await input.frame(0.8)
  input.state('idle'); await input.silence(companionPlaybackCaptureCooldownMs + 1)
  for (let i = 0; i < 3; i += 1) await input.frame(0.1)
  await input.silence()
  expect(input.transcribe).toHaveBeenCalledTimes(1)
  expect(input.bytes().length).toBe(44 + 3 * 4096 * 2)
})

it.each(['privacy', 'unmount'] as const)('never dispatches buffered audio after %s', async reason => {
  const input = await capture()
  input.state('speaking'); await input.frame(0.8)
  input.state('idle'); for (let i = 0; i < 3; i += 1) await input.frame(0.03)
  await act(async () => { if (reason === 'privacy') window.dispatchEvent(new Event('hsc-privacy-pause')); else input.view.unmount() })
  for (let i = 0; i < 3; i += 1) await input.frame(0.1)
  await input.silence()
  expect(input.transcribe).not.toHaveBeenCalled(); expect(input.stop).toHaveBeenCalled()
})
