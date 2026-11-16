// @vitest-environment node
import { createHash } from 'node:crypto'
import { afterEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'
import { CaptureFrameMetadataCounter, type CaptureBinding } from './currentRealtimeCaptureMetadata'

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

function fixture() {
  vi.useFakeTimers()
  vi.stubGlobal('window', globalThis)
  let now = 0
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const binding: CaptureBinding = { owner: 'public-owner', captureOwner: 'public-owner', sessionId: 'public-session',
    childId: 'public-child', generation: 1, current: true, leaseId: 'public-lease',
    deviceHash: createHash('sha256').update('public-device').digest('hex') }
  const root = { id: 'public-root', kind: 'audio', readyState: 'live', enabled: true, muted: false,
    getSettings: () => ({ deviceId: 'public-device', echoCancellation: true }),
    getConstraints: () => ({ echoCancellation: true }), stop: vi.fn() }
  const cloud = { ...root, id: 'public-cloud', stop: vi.fn() }
  const stream = { getAudioTracks: () => [root], getTracks: () => [root] }
  const sender = { track: cloud }
  const report = () => new Map([['public-stats', { id: 'public-stats', type: 'media-source', kind: 'audio',
    trackIdentifier: cloud.id, totalAudioEnergy: 0, totalSamplesDuration: now / 1000 + 1 }]])
  let resolve!: (stats: ReturnType<typeof report>) => void
  const pending = new Promise<ReturnType<typeof report>>(yes => { resolve = yes })
  const getStats = vi.fn(() => now >= 1000 ? pending : Promise.resolve(report()))
  const peer = { connectionState: 'connected', getSenders: () => [sender], getStats, close: vi.fn() }
  const context = { state: 'running', sampleRate: 48000, currentTime: 0.01 }, processor = {}
  const counter = new CaptureFrameMetadataCounter(stream, context, processor)
  const session = new QwenRealtimeVoiceSession({ childName: 'public', captureMetadata: { binding: () => ({ ...binding }) },
    callbacks: { onStatus: vi.fn() } })
  // Use the actual Session, original observer, original getStats poll and real
  // frame counter with controlled structural events; never open a device.
  const internal = session as unknown as {
    metadataObserver: { opened: (s: typeof stream, r: typeof root, o: 'app_native_function_candidate') => void
      cloned: (r: typeof root, c: typeof cloud) => void; observeStats: (...args: unknown[]) => void }
    pollAudioHealth: () => Promise<void>; startAudioHealthPolling: () => void
    audioHealthReading: boolean; audioHealthReadDone: Promise<void> | null; audioHealthEpoch: number
  }
  Object.assign(session, { stream, localSourceTrack: root, audioTrack: cloud, peer, sender,
    microphoneReleased: true, audioHealthEpoch: 1, protectedControl: {
      readCaptureFrameMetadata: () => counter.read(stream, root, now), dispose: vi.fn() } })
  internal.metadataObserver.opened(stream, root, 'app_native_function_candidate')
  internal.metadataObserver.cloned(root, cloud)
  const frame = () => { context.currentTime += 0.02; counter.observe({ isTrusted: true, target: processor,
    inputBuffer: { numberOfChannels: 1, length: 1024, sampleRate: 48000 } }, now) }
  frame()
  return { session, internal, binding, peer, getStats, resolve: () => resolve(report()), frame,
    async advance(ms: number) { now += ms; await vi.advanceTimersByTimeAsync(ms) },
    setNow(value: number) { now = value } }
}

it('keeps original stats fresh at the actual1000.3ms boundary while next asynchronous poll is pending', async () => {
  const f = fixture()
  try {
    await f.internal.pollAudioHealth()
    f.internal.startAudioHealthPolling()
    await f.advance(500); await f.advance(500)
    f.setNow(1000.3); f.frame()
    const value = await f.session.readCaptureMetadata()
    expect(value.status).toBe('current_app_component_observed_unadmitted')
    expect(value.rtc_stats_observed_at).toBe(500)
    expect(Number(value.observed_at) - Number(value.rtc_stats_observed_at)).toBeCloseTo(500.3, 5)
    expect(f.internal.audioHealthReading).toBe(true)
    expect(f.getStats).toHaveBeenCalledTimes(3)
  } finally { f.session.close(); f.resolve(); await Promise.resolve() }
})

it('still rejects truly older than1000ms statistics and never overlaps a pending normal poll', async () => {
  const f = fixture()
  try {
    await f.internal.pollAudioHealth()
    f.internal.startAudioHealthPolling()
    await f.advance(500); await f.advance(500)
    // Poll at1000 remains pending, so further timer ticks cannot start another.
    await f.advance(500)
    expect(f.getStats).toHaveBeenCalledTimes(3)
    f.setNow(1500.3); f.frame()
    // Last accepted poll was500ms; no relaxed freshness or manufactured stats.
    const value = await f.session.readCaptureMetadata()
    expect(value.status).toBe('unavailable')
    expect(value.reason).toBe('CURRENT_APP_CAPTURE_BINDING_UNPROVEN')
    expect(f.internal.audioHealthReading).toBe(true)
  } finally { f.session.close(); f.resolve(); await Promise.resolve() }
})

it('close aborts the pending poll, clears timers and ignores its later response', async () => {
  const f = fixture()
  await f.internal.pollAudioHealth()
  f.internal.startAudioHealthPolling()
  await f.advance(500); await f.advance(500)
  const done = f.internal.audioHealthReadDone
  const observe = vi.spyOn(f.internal.metadataObserver, 'observeStats')
  f.session.close()
  await done
  f.resolve(); await Promise.resolve(); await f.advance(2000)
  expect(observe).not.toHaveBeenCalled()
  expect(f.getStats).toHaveBeenCalledTimes(3)
  expect(f.peer.close).toHaveBeenCalledOnce()
  expect(f.internal.audioHealthReading).toBe(false)
  expect(f.internal.audioHealthReadDone).toBeNull()
  expect(vi.getTimerCount()).toBe(0)
})

it('a changed epoch cannot publish the pending poll into the new capture generation', async () => {
  const f = fixture()
  try {
    await f.internal.pollAudioHealth()
    f.internal.startAudioHealthPolling()
    await f.advance(500); await f.advance(500)
    const done = f.internal.audioHealthReadDone
    const observe = vi.spyOn(f.internal.metadataObserver, 'observeStats')
    f.internal.audioHealthEpoch += 1
    f.resolve(); await done
    expect(observe).not.toHaveBeenCalled()
    f.frame()
    expect((await f.session.readCaptureMetadata()).status).toBe('unavailable')
  } finally { f.session.close() }
})
