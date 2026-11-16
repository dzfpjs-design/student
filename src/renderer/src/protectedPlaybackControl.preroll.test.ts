import { afterEach, describe, expect, it, vi } from 'vitest'
import { ProtectedPlaybackControl, type LocalPlaybackControlPayload, type LocalPlaybackControlResult } from './protectedPlaybackControl'
import { PlaybackControlAdmission } from './playbackControlAdmission'
import { ProtectedPlaybackBarrier } from './protectedPlaybackBarrier'

type Call = { payload: LocalPlaybackControlPayload; signal: AbortSignal }
const taps: ProtectedPlaybackControl[] = []
afterEach(() => { taps.splice(0).forEach(t => t.dispose()); vi.useRealTimers() })
function samples(call: Call): number[] {
  const bytes = Uint8Array.from(atob(call.payload.wav_base64), c => c.charCodeAt(0)), view = new DataView(bytes.buffer)
  expect(view.getUint32(24, true)).toBe(16000)
  return Array.from({ length: view.getUint32(40, true) / 2 }, (_, i) => view.getInt16(44 + i * 2, true))
}
function fixture() {
  let now = 1000, admitted = true
  const local = { enabled: true, readyState: 'live', getSettings: () => ({ echoCancellation: 'all' }) } as unknown as MediaStreamTrack
  const cloud = { enabled: false, readyState: 'live' } as MediaStreamTrack
  let owner: any = { inputKind: 'realtime', sessionId: 'public-session', childId: 'public-child', localTrack: local, cloudTrack: cloud,
    lease: { id: '00000000-0000-4000-8000-000000000001', generation: 1, sessionId: 'public-session', kind: 'local_sapi', text: '公开合成播放参考。' } }
  let ledger = new PlaybackControlAdmission(owner.lease.id, owner.lease.generation, owner.sessionId)
  const barrier = new ProtectedPlaybackBarrier(), calls: Call[] = []
  const recognize = vi.fn(async (_s: string, payload: LocalPlaybackControlPayload, signal: AbortSignal): Promise<LocalPlaybackControlResult> => {
    calls.push({ payload, signal })
    return { status: 'not_control', control: null, playback_lease_id: payload.playback_lease_id, playback_generation: payload.playback_generation,
      capture_epoch: payload.capture_epoch, cloud_used: false, persisted: false, learning_evidence: false }
  })
  const cancel = vi.fn(() => true), hold = vi.fn(), fail = vi.fn()
  const build = () => {
    const tap = new ProtectedPlaybackControl({ current: () => owner, capture: () => owner, recognize, cancelExact: cancel, holdCloud: hold,
      failClosed: fail, barrier, now: () => now, admission: o => admitted && ledger.bind(o.lease.id, o.lease.generation, o.sessionId, o.childId) ? ledger : null })
    taps.push(tap); return tap
  }
  let tap = build()
  const frame = (value: number, count = 1, rate = 16000, advance = true) => {
    for (let i = 0; i < count; i++) { if (advance) now += 20; tap.processFrame(new Float32Array(rate / 50).fill(value), rate) }
  }
  return { get tap() { return tap }, get owner() { return owner }, set owner(x) { owner = x }, get ledger() { return ledger }, barrier, local, cloud, calls,
    recognize, cancel, hold, fail, frame, finish: (rate = 16000) => { frame(.05, 6, rate); frame(0, 13, rate) },
    admit: (value: boolean) => { admitted = value }, jump: (ms: number) => { now += ms },
    newLedger: () => { ledger = new PlaybackControlAdmission(owner.lease.id, owner.lease.generation, owner.sessionId) },
    reconnect: () => { tap.dispose(); owner = { ...owner, localTrack: { ...owner.localTrack }, cloudTrack: { ...owner.cloudTrack } }; tap = build() },
  }
}
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }
describe('actual controller bounded current-owner leading PCM preservation', () => {
  it('preserves the admitted weak 160ms before the first voiced frame exactly once', () => {
    const f = fixture(); f.frame(.001, 8); f.finish()
    expect(f.calls).toHaveLength(1)
    const pcm = samples(f.calls[0]); expect(pcm.slice(0, 2560)).toEqual(Array(2560).fill(33)); expect(pcm[2560]).toBe(1638)
    expect(pcm.filter(v => v === 33)).toHaveLength(2560)
    expect(f.cloud.enabled).toBe(false); expect(f.cancel).not.toHaveBeenCalled()
  })
  it('keeps only the most recent 160ms, trimming an earlier natural weak prefix', () => {
    const f = fixture(); f.frame(.0005, 12); f.frame(.001, 8); f.finish(); const pcm = samples(f.calls[0])
    expect(pcm.slice(0, 2560)).toEqual(Array(2560).fill(33)); expect(pcm).not.toContain(16)
  })
  it('copies only owned prefix samples, so mutation by the source cannot alter the encoded prefix', () => {
    const f = fixture(), weak = new Float32Array(2560).fill(.001)
    f.tap.processFrame(weak, 16000); weak.fill(.9); f.finish(); const pcm = samples(f.calls[0])
    expect(pcm.slice(0, 2560)).toEqual(Array(2560).fill(33)); expect(pcm).not.toContain(29490)
  })
  it('partial oldest-frame trimming never retains more than 160ms at 48k', () => {
    const f = fixture()
    for (let i = 0; i < 9; i++) f.tap.processFrame(new Float32Array(1024).fill(.001), 48000)
    f.finish(48000); const pcm = samples(f.calls[0])
    expect(pcm.filter(v => v === 33)).toHaveLength(2560); expect(pcm[2560]).toBe(1638)
  })
  it('prefix counts toward the original 2500ms cap even when wall time does not advance', () => {
    const f = fixture(), consumed = vi.spyOn(f.ledger, 'consume')
    f.frame(.001, 8, 16000, false); f.frame(.05, 118, 16000, false)
    expect(consumed).toHaveBeenCalledOnce(); expect(f.calls).toHaveLength(0)
    f.frame(.05, 20, 16000, false); expect(consumed).toHaveBeenCalledOnce(); expect(f.calls).toHaveLength(0)
  })
  it('silence and weak non-speech alone never invoke a recognizer or cancel', () => {
    const f = fixture(); f.frame(0, 200); f.frame(.001, 200)
    expect(f.calls).toHaveLength(0); expect(f.cancel).not.toHaveBeenCalled(); expect(f.hold).not.toHaveBeenCalledWith(false)
  })
  it('a noncontrol result discards the consumed prefix and does not replay it in a new segment', async () => {
    const f = fixture(); f.frame(.001, 8); f.finish(); await settle(); f.frame(0, 50); f.frame(.0005, 8); f.finish()
    expect(f.calls).toHaveLength(2); const pcm = samples(f.calls[1]); expect(pcm.slice(0, 2560)).toEqual(Array(2560).fill(16)); expect(pcm).not.toContain(33)
    expect(f.calls[1].payload.capture_epoch).toBeGreaterThan(f.calls[0].payload.capture_epoch); expect(f.cancel).not.toHaveBeenCalled()
  })
  it.each(['lease', 'generation', 'session', 'child', 'track', 'cloud-track', 'capture-epoch', 'reference'])('a %s transition cannot borrow old weak PCM', kind => {
    const f = fixture()
    if (kind === 'capture-epoch') f.owner = { ...f.owner, inputKind: 'segmented', submissionBlocked: true, captureEpoch: Symbol() }
    if (kind === 'reference') f.owner = { ...f.owner, lease: { ...f.owner.lease, kind: 'local_pcm', text: null, pcmReference: Object.freeze({
      version: 'pet_full_audio_control_reference_v1', child_id: f.owner.childId, session_id: f.owner.sessionId, media_id: 'media', prompt_id: 'prompt', item_version: 'item-v', diagnostic_item_id: 'item',
      source_version: 'source-v', audio_sha256: 'a'.repeat(64), spoken_text_sha256: 'b'.repeat(64), source_review_event_id: 'review', generation: 1, local_only: true, cloud_transmission_allowed: false,
    }) } }
    f.frame(.001, 8)
    if (kind === 'lease') f.owner = { ...f.owner, lease: { ...f.owner.lease, id: '00000000-0000-4000-8000-000000000002' } }
    if (kind === 'generation') f.owner = { ...f.owner, lease: { ...f.owner.lease, generation: 2 } }
    if (kind === 'session') f.owner = { ...f.owner, sessionId: 'next-public-session', lease: { ...f.owner.lease, sessionId: 'next-public-session' } }
    if (kind === 'child') f.owner = { ...f.owner, childId: 'next-public-child', lease: { ...f.owner.lease, id: '00000000-0000-4000-8000-000000000002' } }
    if (kind === 'track') f.owner = { ...f.owner, localTrack: { ...f.local } }
    if (kind === 'cloud-track') f.owner = { ...f.owner, cloudTrack: { ...f.cloud } }
    if (kind === 'capture-epoch') f.owner = { ...f.owner, captureEpoch: Symbol() }
    if (kind === 'reference') f.owner = { ...f.owner, lease: { ...f.owner.lease, pcmReference: Object.freeze({ ...f.owner.lease.pcmReference, source_review_event_id: 'next-review' }) } }
    if (['lease', 'generation', 'session', 'child'].includes(kind)) f.newLedger()
    f.frame(0, 20); f.finish(); expect(f.calls).toHaveLength(1); expect(samples(f.calls[0])).not.toContain(33)
  })
  it('foreign child cannot borrow the already bound lease or its prefix', () => {
    const f = fixture(); f.frame(.001, 8); f.owner = { ...f.owner, childId: 'foreign-child' }; f.finish()
    expect(f.calls).toHaveLength(0); expect(f.cancel).not.toHaveBeenCalled()
  })
  it('eligibility loss wipes prefix before the same track can become eligible again', () => {
    const f = fixture(); f.frame(.001, 8); f.local.enabled = false; f.frame(.001, 1); f.local.enabled = true; f.finish()
    expect(f.calls).toHaveLength(1); expect(samples(f.calls[0])).not.toContain(33)
  })
  it('absence of admission wipes prefix and denied sound does not become future prefix', () => {
    const f = fixture(); f.frame(.001, 8); f.admit(false); f.frame(.0005, 8); f.admit(true); f.finish()
    expect(f.calls).toHaveLength(1); const pcm = samples(f.calls[0]); expect(pcm).not.toContain(33); expect(pcm).not.toContain(16)
  })
  it('a real consumed ledger rejects new audio until its existing quiet/interval gates pass', async () => {
    const f = fixture(); f.finish(); await settle(); f.frame(.001, 2); f.frame(.05, 20)
    expect(f.calls).toHaveLength(1); f.frame(0, 50); f.finish(); expect(f.calls).toHaveLength(2); expect(samples(f.calls[1])).not.toContain(33)
  })
  it('tail barrier frames cannot contribute prefix after genuine quiet releases the barrier', () => {
    const f = fixture(); f.frame(.001, 8); f.barrier.begin(f.owner, 'other-current-playback', 1, 1000); f.frame(.0005, 2); f.frame(0, 50)
    expect(f.barrier.held()).toBe(false); f.finish(); const pcm = samples(f.calls[0]); expect(pcm).not.toContain(33); expect(pcm).not.toContain(16)
  })
  it('reconnection never inherits prefix or borrows the old source quiet observation', () => {
    const f = fixture(); f.frame(.001, 8); f.reconnect(); f.frame(.05, 6); f.frame(0, 13); expect(f.calls).toHaveLength(0)
    f.frame(0, 50); f.finish(); expect(f.calls).toHaveLength(1); expect(samples(f.calls[0])).not.toContain(33)
  })
  it('a stale idle prefix is discarded after a source-frame gap exceeding 160ms', () => {
    const f = fixture(); f.frame(.001, 8); f.jump(200); f.finish(); expect(samples(f.calls[0])).not.toContain(33)
  })
  it('a rate change cannot resample a prefix collected under a different source rate', () => {
    const f = fixture(); f.frame(.001, 8); f.finish(48000); expect(samples(f.calls[0])).not.toContain(33)
  })
  it('owner removal aborts an inflight request and late recognized data cannot cancel', async () => {
    const f = fixture(); let resolve!: (v: LocalPlaybackControlResult) => void
    f.recognize.mockImplementationOnce(async (_s, payload, signal) => { f.calls.push({ payload, signal }); return new Promise(r => { resolve = r }) })
    f.frame(.001, 8); f.finish(); const call = f.calls[0]; f.owner = null; f.frame(0)
    expect(call.signal.aborted).toBe(true)
    resolve({ status: 'recognized', control: 'stop_playback', playback_lease_id: call.payload.playback_lease_id, playback_generation: call.payload.playback_generation,
      capture_epoch: call.payload.capture_epoch, cloud_used: false, persisted: false, learning_evidence: false }); await settle(); expect(f.cancel).not.toHaveBeenCalled()
  })
  it('dispose clears prefix and permanently rejects additional natural frames', () => {
    const f = fixture(); f.frame(.001, 8); f.tap.dispose(); f.finish(); expect(f.calls).toHaveLength(0); expect(f.cancel).not.toHaveBeenCalled()
  })
  it('recognized stub data still requires every original local/cloud/learning receipt guard', async () => {
    const f = fixture(); f.recognize.mockImplementation(async (_s, payload, signal) => {
      f.calls.push({ payload, signal }); return { status: 'recognized', control: 'stop_playback', playback_lease_id: payload.playback_lease_id,
        playback_generation: payload.playback_generation, capture_epoch: payload.capture_epoch, cloud_used: true, persisted: false, learning_evidence: false } as unknown as LocalPlaybackControlResult
    })
    f.frame(.001, 8); f.finish(); await settle(); expect(f.calls).toHaveLength(1); expect(f.cancel).not.toHaveBeenCalled(); expect(f.cloud.enabled).toBe(false)
  })
})
