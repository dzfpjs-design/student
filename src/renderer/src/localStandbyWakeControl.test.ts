// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest'
import { LocalStandbyWakeControl } from './localStandbyWakeControl'
import { ProtectedPlaybackControl } from './protectedPlaybackControl'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'
import type { StandbyVoiceInput } from './types'
import { ApiError } from './api'

function fixture(text = '小知') {
  let now = 1000, word = '小知', eligible = true
  const local = { readyState: 'live', enabled: true } as MediaStreamTrack
  const cloud = { readyState: 'live', enabled: false } as MediaStreamTrack
  let capture = { inputKind: 'realtime' as const, sessionId: 'synthetic-session', childId: 'synthetic-child', localTrack: local, cloudTrack: cloud }
  const recognized = (key: string): StandbyVoiceInput => ({ client_action_id: key, transcript: text,
    duration_ms: 2400, status: 'completed', model_version: 'synthetic', cloud_used: false, persisted: false })
  const recognize = vi.fn(async (_wav: string, _signal: AbortSignal, key: string) => recognized(key))
  const onWake = vi.fn(), onUnavailable = vi.fn()
  const tap = new LocalStandbyWakeControl({ capture: () => capture, word: () => word, eligible: () => eligible,
    recognize, onWake, onUnavailable, now: () => now })
  const frames = (count = 38) => { for (let i = 0; i < count; i++) { tap.processFrame(new Float32Array(1024).fill(0.04), 16000); now += 64 } }
  return { tap, frames, recognize, recognized, onWake, onUnavailable, local, cloud,
    setWord: (v: string) => { word = v }, setEligible: (v: boolean) => { eligible = v },
    changeChild: () => { capture = { ...capture, childId: 'other-child' } } }
}
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
it.each(['小知', '小智', '小知，小知'])('local bare registered call %s only activates, with no Agent or cloud path', async text => {
  const f = fixture(text); f.frames(); await Promise.resolve(); await Promise.resolve()
  expect(f.recognize).toHaveBeenCalledTimes(1); expect(f.onWake).toHaveBeenCalledTimes(1)
  expect(f.cloud.enabled).toBe(false); f.tap.dispose()
})
it.each(['电视里的人在说话', '小知，隐私模式', '小知，停止监听', '小知，帮我复习地理'])('does not execute or activate a command tail %s', async text => {
  const f = fixture(text); f.frames(); await Promise.resolve(); await Promise.resolve()
  expect(f.onWake).not.toHaveBeenCalled(); f.tap.dispose()
})
it.each(['child', 'word', 'dispose'])('late standby result after %s change cannot activate another owner', async change => {
  const f = fixture(); let finish!: (value: StandbyVoiceInput) => void, key = ''
  f.recognize.mockImplementation((_wav, _signal, id) => { key = id; return new Promise(resolve => { finish = resolve }) })
  f.frames()
  if (change === 'child') f.changeChild(); else if (change === 'word') f.setWord('小伴'); else f.tap.dispose()
  finish(f.recognized(key)); await Promise.resolve(); await Promise.resolve()
  expect(f.onWake).not.toHaveBeenCalled(); f.tap.dispose()
})
it('standby inference is denied while cloud uplink is open or a protected output owns capture', () => {
  const f = fixture(); f.cloud.enabled = true; f.frames(); expect(f.recognize).not.toHaveBeenCalled()
  f.cloud.enabled = false; f.setEligible(false); f.frames(); expect(f.recognize).not.toHaveBeenCalled(); f.tap.dispose()
})
it('a fixed busy original decoder waits for a later bounded window without claiming cancellation or queuing concurrent requests', async () => {
  const f = fixture()
  f.recognize.mockRejectedValueOnce(new ApiError('busy', 409, 'LOCAL_ASR_BUSY'))
  f.frames(); await Promise.resolve(); await Promise.resolve()
  expect(f.onUnavailable).not.toHaveBeenCalled(); expect(f.onWake).not.toHaveBeenCalled()
  f.frames(17); await Promise.resolve(); await Promise.resolve()
  expect(f.recognize).toHaveBeenCalledTimes(2); expect(f.onWake).toHaveBeenCalledTimes(1); f.tap.dispose()
})
it('a five second ignored abort retires standby inference, ignores late result, and does not claim native cancellation', async () => {
  vi.useFakeTimers()
  const f = fixture(); let finish!: (value: StandbyVoiceInput) => void, key = ''
  f.recognize.mockImplementation((_wav, _signal, id) => { key = id; return new Promise(resolve => { finish = resolve }) })
  f.frames(); await vi.advanceTimersByTimeAsync(5001); f.frames(80)
  expect(f.onUnavailable).toHaveBeenCalledTimes(1); expect(f.recognize).toHaveBeenCalledTimes(1)
  finish(f.recognized(key)); await Promise.resolve(); expect(f.onWake).not.toHaveBeenCalled(); f.tap.dispose()
})
it('the protected control source fans out each frame through one original AudioContext and processor', async () => {
  const connect = vi.fn(), disconnect = vi.fn(), close = vi.fn(async () => undefined)
  const processor = { connect, disconnect, onaudioprocess: null as null | ((event: any) => void) }
  const source = { connect, disconnect }, gain = { connect, disconnect, gain: { value: 1 } }
  const Context = vi.fn(function () { return { createMediaStreamSource: () => source, createScriptProcessor: () => processor,
    createGain: () => gain, sampleRate: 48000, currentTime: 1, state: 'running', close, destination: {} } })
  vi.stubGlobal('AudioContext', Context)
  const fanout = vi.fn(), stop = new ProtectedPlaybackControl({ capture: () => null, current: () => null,
    recognize: vi.fn(), cancelExact: () => false, holdCloud: vi.fn(), failClosed: vi.fn() })
  const original = vi.spyOn(stop, 'processFrame')
  await stop.attach({ getAudioTracks: () => [] } as unknown as MediaStream, fanout)
  const frame = new Float32Array(1024).fill(0.03)
  processor.onaudioprocess!({ inputBuffer: { getChannelData: () => frame, numberOfChannels: 1, length: frame.length, sampleRate: 48000 } })
  expect(Context).toHaveBeenCalledTimes(1); expect(original).toHaveBeenCalledTimes(1); expect(fanout).toHaveBeenCalledTimes(1)
  expect(fanout.mock.calls[0][0]).toBe(original.mock.calls[0][0]); expect(fanout.mock.calls[0][1]).toBe(48000)
  stop.dispose(); expect(processor.onaudioprocess).toBeNull(); expect(close).toHaveBeenCalledTimes(1)
})
it('native standby gates uplink and local-only SAPI remains gated even with AEC all', () => {
  let active = false
  const session = new QwenRealtimeVoiceSession({ childName: 'Synthetic', fullDuplex: true,
    nativeActivation: { isActive: () => active, word: () => '小知', onWake: vi.fn(), recognizeStandby: vi.fn() },
    callbacks: { onStatus: vi.fn() } }) as any
  session.audioTrack = { enabled: false, readyState: 'live', getSettings: () => ({ echoCancellation: 'all' }) }
  session.microphoneReleased = true
  session.syncMicrophoneGate(); expect(session.audioTrack.enabled).toBe(false)
  active = true; session.syncMicrophoneGate(); expect(session.audioTrack.enabled).toBe(true)
  session.setExternalPlaybackActive(true, true); expect(session.audioTrack.enabled).toBe(false)
  session.audioTrack = null; session.close()
})
