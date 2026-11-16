// @vitest-environment node
import { createHash } from 'node:crypto'
import { expect, it } from 'vitest'
import { CaptureFrameMetadataCounter, RealtimeCaptureMetadataObserver, checkCurrentNativeMetadataPair,
  requireNativeJoint8hAdmission, type CaptureBinding, type CaptureState } from './currentRealtimeCaptureMetadata'

// Synthetic structural fixtures exercise the reader without opening a device.
function capture() {
  const root = { id: 'synthetic-root', kind: 'audio', readyState: 'live', enabled: true, muted: false,
    getSettings: () => ({ deviceId: 'synthetic-device', echoCancellation: true }), getConstraints: () => ({ echoCancellation: true }) }
  const cloud = { ...root, id: 'synthetic-cloud' }, stream = { getAudioTracks: () => [root] }
  const sender = { track: cloud }, peer = { connectionState: 'connected', getSenders: () => [sender] }
  const context = { state: 'running', sampleRate: 48000, currentTime: 0 }, processor = {}
  const binding: CaptureBinding = { owner: 'synthetic-owner', captureOwner: 'synthetic-owner', sessionId: 'synthetic-session',
    childId: 'synthetic-child', generation: 1, current: true, leaseId: 'synthetic-lease',
    deviceHash: createHash('sha256').update('synthetic-device').digest('hex') }
  const counter = new CaptureFrameMetadataCounter(stream, context, processor)
  const observer = new RealtimeCaptureMetadataObserver({ binding: () => ({ ...binding }) })
  observer.opened(stream, root, 'app_native_function_candidate'); observer.cloned(root, cloud)
  let now = 0
  const state = (): CaptureState => ({ stream, root, cloud, peer, sender, captureCurrent: true, phase: 'listening', epoch: 1,
    frame: counter.read(stream, root, now), documentTimeOrigin: 1234, now })
  const advance = (mediaAdvance = 0.02) => {
    now += 100; context.currentTime += mediaAdvance
    const event = { isTrusted: true, target: processor, inputBuffer: { numberOfChannels: 1, length: 1024, sampleRate: 48000,
      getChannelData: () => { throw Error('METADATA_MUST_NOT_READ_PCM') } } }
    counter.observe(event, now)
    observer.observeStats(peer, cloud, 1, [{ id: 'synthetic-stats', trackIdentifier: cloud.id,
      totalAudioEnergy: 1, totalSamplesDuration: context.currentTime }], now)
  }
  advance()
  return { binding, root, cloud, observer, advance, read: () => observer.read(state) }
}

it('reads increasing existing callback counters, hashes identity, and never grants device or acoustic proof', async () => {
  const c = capture(), a = await c.read(); c.advance(); const b = await c.read()
  expect(a.status).toBe('current_app_component_observed_unadmitted'); expect(a.frames).toBe(1024)
  expect(JSON.stringify(a)).not.toContain('synthetic-')
  const keys = ['owner_sha256', 'session_sha256', 'child_sha256', 'lease_sha256', 'device_sha256', 'root_track_sha256',
    'cloud_track_sha256', 'rtc_source_sha256', 'connection_generation', 'audio_health_epoch', 'document_time_origin']
  const expected = Object.fromEntries(keys.map(key => [key, a[key]]))
  expect(checkCurrentNativeMetadataPair(a, b, expected)).toEqual({ status: 'source_component_pair_observed_unadmitted',
    native_origin_verified: false, joint8h_admitted: false, acoustic_quality_verified: false })
  expect(() => checkCurrentNativeMetadataPair(a, a, expected)).toThrow('CURRENT_NATIVE_METADATA_PAIR_UNPROVEN')
  expect(() => checkCurrentNativeMetadataPair(a, { ...b, joint8h_admitted: true }, expected)).toThrow()
  expect(() => requireNativeJoint8hAdmission(a, b)).toThrow('INDEPENDENT_ACTUAL_NATIVE_MIC_RTC_SOURCE')
})

it('rejects an owner change during asynchronous identity hashing', async () => {
  const c = capture(), pending = c.read(); c.binding.current = false
  expect((await pending).status).toBe('unavailable')
})

it('rejects replacement sender tracks and stale statistics without taking another sample', async () => {
  const c = capture(); c.cloud.readyState = 'ended'
  expect((await c.read()).status).toBe('unavailable')
  c.cloud.readyState = 'live'; c.observer.invalidateStats()
  expect((await c.read()).status).toBe('unavailable')
})


function frameCounterFixture() {
  const root = { id: 'synthetic-frame-root', kind: 'audio', readyState: 'live', enabled: true, muted: false, getSettings: () => ({}) }
  const stream = { getAudioTracks: () => [root] }, context = { state: 'running', sampleRate: 48000, currentTime: 0 }, processor = {}
  const counter = new CaptureFrameMetadataCounter(stream, context, processor)
  const event = () => ({ isTrusted: true, target: processor, inputBuffer: { numberOfChannels: 1, length: 1024, sampleRate: 48000,
    getChannelData: () => { throw Error('METADATA_MUST_NOT_READ_PCM') } } })
  return { context, processor, counter, event, read: (now: number) => counter.read(stream, root, now) }
}

it('accepts distinct trusted native callbacks sharing a rendering-clock tick without manufacturing time progress', () => {
  const f = frameCounterFixture(); f.context.currentTime = 0.02
  f.counter.observe(f.event(), 100); const a = f.read(100)
  f.counter.observe(f.event(), 101); const b = f.read(101)
  expect(b.current).toBe(true); expect(b.bad_callbacks).toBe(0)
  expect(b.frames).toBe(a.frames + 1024); expect(b.trusted_callbacks).toBe(a.trusted_callbacks + 1)
  expect(b.media_time).toBe(a.media_time)
  f.context.currentTime = 0.04; f.counter.observe(f.event(), 120)
  expect(f.read(120).media_time).toBeGreaterThan(b.media_time)
})

it('requires actual rendering-clock progress before first health and bounds freshness despite repeated callbacks', () => {
  const f = frameCounterFixture(); f.counter.observe(f.event(), 10)
  expect(f.read(10).current).toBe(false); expect(f.read(10).bad_callbacks).toBe(0)
  f.context.currentTime = 0.02; f.counter.observe(f.event(), 100)
  expect(f.read(100).current).toBe(true)
  f.counter.observe(f.event(), 1100); expect(f.read(1100).current).toBe(true)
  f.counter.observe(f.event(), 1101); expect(f.read(1101).current).toBe(false)
  expect(f.read(1101).bad_callbacks).toBe(0)
  f.context.currentTime = 0.04; f.counter.observe(f.event(), 1120)
  expect(f.read(1120).current).toBe(true)
})

it('still rejects equal-clock metadata pairs despite valid callbacks and newer RTC statistics', async () => {
  const c = capture(), a = await c.read(); c.advance(0); const b = await c.read()
  expect(a.status).toBe('current_app_component_observed_unadmitted'); expect(b.status).toBe(a.status)
  expect(b.trusted_callbacks).toBeGreaterThan(Number(a.trusted_callbacks)); expect(b.media_time).toBe(a.media_time)
  const identities = ['owner_sha256', 'session_sha256', 'child_sha256', 'lease_sha256', 'device_sha256', 'root_track_sha256',
    'cloud_track_sha256', 'rtc_source_sha256', 'connection_generation', 'audio_health_epoch', 'document_time_origin']
  expect(() => checkCurrentNativeMetadataPair(a, b, Object.fromEntries(identities.map(k => [k, a[k]])))).toThrow('CURRENT_NATIVE_METADATA_PAIR_UNPROVEN')
})

it.each(['untrusted', 'wrong_target', 'channels', 'length', 'rate', 'suspended', 'backward_media', 'nonfinite_media', 'backward_observation'] as const)(
  'keeps invalid %s callbacks latched and never makes them healthy with later valid callbacks', violation => {
    const f = frameCounterFixture(); f.context.currentTime = 0.02; f.counter.observe(f.event(), 100)
    const e = f.event(); let now = 110
    if (violation === 'untrusted') e.isTrusted = false
    if (violation === 'wrong_target') e.target = {}
    if (violation === 'channels') e.inputBuffer.numberOfChannels = 2
    if (violation === 'length') e.inputBuffer.length = 0
    if (violation === 'rate') e.inputBuffer.sampleRate = 16000
    if (violation === 'suspended') f.context.state = 'suspended'
    if (violation === 'backward_media') f.context.currentTime = 0.01
    if (violation === 'nonfinite_media') f.context.currentTime = NaN
    if (violation === 'backward_observation') now = 99
    f.counter.observe(e, now); expect(f.read(110).bad_callbacks).toBe(1)
    f.context.state = 'running'; f.context.currentTime = 0.04; f.counter.observe(f.event(), 120)
    expect(f.read(120).bad_callbacks).toBe(1); expect(f.read(120).current).toBe(false)
  })
