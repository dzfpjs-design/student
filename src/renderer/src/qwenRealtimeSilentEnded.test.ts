import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'

// Native stop changes readyState without dispatching the ended event. Neither
// regression dispatches an event or supplies a replacement input/ASR result.
class MockTrack extends EventTarget {
  kind = 'audio'
  enabled = true
  readyState: 'live' | 'ended' = 'live'
  muted = false
  stop = vi.fn(() => { this.readyState = 'ended' })
  getSettings = () => ({ echoCancellation: true })
  constructor(readonly id: string) { super() }
}
type Internal = {
  receive: (raw: string) => void
  sender: { track: MockTrack | null }
  audioHealthEpoch: number
  options: { protectedPlaybackControl?: { isCurrent: () => boolean } }
}
const sessions: QwenRealtimeVoiceSession[] = []
let priorDesktop: typeof window.desktop
beforeEach(() => { priorDesktop = window.desktop; vi.useFakeTimers() })
afterEach(() => {
  sessions.splice(0).forEach(s => s.close())
  window.desktop = priorDesktop
  vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers()
})
async function connected() {
  const root = new MockTrack('owned-root'), cloud = new MockTrack('owned-cloud')
  Object.assign(root, { clone: vi.fn(() => cloud) })
  const stream = { getTracks: () => [root], getAudioTracks: () => [root] }
  const channel = Object.assign(new EventTarget(), { readyState: 'open', close: vi.fn(), send: vi.fn() })
  let duration = 0
  class Peer extends EventTarget {
    connectionState = 'connected'
    iceGatheringState = 'complete'
    localDescription = { sdp: 'public-offer' }
    sender = { track: cloud as MockTrack | null, replaceTrack: vi.fn(async (next: MockTrack | null) => { this.sender.track = next }) }
    addTrack = vi.fn(() => this.sender)
    createDataChannel = () => channel
    createOffer = vi.fn().mockResolvedValue({ type: 'offer', sdp: 'public-offer' })
    setLocalDescription = vi.fn().mockResolvedValue(undefined)
    setRemoteDescription = vi.fn().mockResolvedValue(undefined)
    close = vi.fn()
    getStats = vi.fn(async (): Promise<Map<string, Record<string, unknown>>> => new Map([['source', {
      id: 'owned-source', type: 'media-source', kind: 'audio', trackIdentifier: cloud.id,
      totalAudioEnergy: 0, totalSamplesDuration: ++duration,
    }]]))
  }
  vi.stubGlobal('RTCPeerConnection', Peer)
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn().mockResolvedValue(stream) } })
  window.desktop = { negotiateQwenRealtime: vi.fn().mockResolvedValue({ answerSdp: 'public-answer' }) } as unknown as typeof window.desktop
  const error = vi.fn(), status = vi.fn(), completed = vi.fn(), committed = vi.fn()
  const session = new QwenRealtimeVoiceSession({ childName: 'public synthetic', callbacks: {
    onStatus: status, onError: error, onInputCompleted: completed, onCommittedTurn: committed,
  } })
  sessions.push(session)
  await session.connect()
  const internal = session as unknown as Internal
  internal.receive(JSON.stringify({ type: 'session.updated' }))
  await Promise.resolve(); await Promise.resolve()
  const peer = (session as unknown as { peer: Peer }).peer
  expect(cloud.enabled).toBe(true)
  expect(status.mock.calls.at(-1)?.[0].phase).toBe('listening')
  return { root, cloud, session, internal, peer, error, status, completed, committed }
}

it.each(['root', 'cloud'] as const)('retires the current connected %s after silent native stop without inventing input', async which => {
  const t = await connected(), endedEvent = vi.fn()
  t[which].addEventListener('ended', endedEvent)
  t[which].stop()
  expect(t[which].readyState).toBe('ended')
  expect(endedEvent).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(1000)
  expect(t.error).toHaveBeenCalledExactlyOnceWith(which === 'root'
    ? '麦克风音轨已断开，正在重新连接。' : '麦克风传输音轨已断开，正在重新连接。')
  expect(t.status.mock.calls.at(-1)?.[0].phase).toBe('error')
  expect(t.cloud.enabled).toBe(false)
  expect(t.completed).not.toHaveBeenCalled(); expect(t.committed).not.toHaveBeenCalled()
  const reads = t.peer.getStats.mock.calls.length
  await vi.advanceTimersByTimeAsync(10_000)
  expect(t.error).toHaveBeenCalledOnce(); expect(t.peer.getStats).toHaveBeenCalledTimes(reads)
})
it.each(['root', 'cloud'] as const)('detects silently ended %s even while the live playback gate disables cloud input', async which => {
  const t = await connected(); t.session.setExternalPlaybackActive(true, true)
  expect(t.cloud.enabled).toBe(false)
  t[which].stop(); await vi.advanceTimersByTimeAsync(1000)
  expect(t.error).toHaveBeenCalledOnce()
})
it('leaves live disabled tracks paused and resumes the same transport without error', async () => {
  const t = await connected(); t.session.setExternalPlaybackActive(true, true)
  await vi.advanceTimersByTimeAsync(5000)
  expect(t.root.readyState).toBe('live'); expect(t.cloud.readyState).toBe('live')
  expect(t.cloud.enabled).toBe(false); expect(t.error).not.toHaveBeenCalled()
  t.session.setExternalPlaybackActive(false, true); await vi.advanceTimersByTimeAsync(1000)
  expect(t.cloud.enabled).toBe(true); expect(t.error).not.toHaveBeenCalled()
})
it('does not report deliberate source stops after close or revive the polling timer', async () => {
  const t = await connected(); t.session.close()
  expect(t.root.readyState).toBe('ended'); expect(t.cloud.readyState).toBe('ended')
  await vi.advanceTimersByTimeAsync(10_000)
  expect(t.error).not.toHaveBeenCalled(); expect(vi.getTimerCount()).toBe(0)
})
it('does not attribute an ended detached track to a different sender owner', async () => {
  const t = await connected(); t.internal.sender.track = new MockTrack('foreign-sender')
  t.root.stop(); t.cloud.stop(); await vi.advanceTimersByTimeAsync(1000)
  expect(t.error).not.toHaveBeenCalled(); expect(t.peer.getStats).not.toHaveBeenCalled()
})
it('does not fail a revoked protected owner from its retired ended tracks', async () => {
  const t = await connected()
  t.internal.options.protectedPlaybackControl = { isCurrent: () => false }
  t.root.stop(); t.cloud.stop(); await vi.advanceTimersByTimeAsync(1000)
  expect(t.error).not.toHaveBeenCalled()
})
it('can retire an ended current source while the previous bounded stats read is still pending', async () => {
  const t = await connected()
  let finish!: (value: Map<string, Record<string, unknown>>) => void
  t.peer.getStats.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  await vi.advanceTimersByTimeAsync(1000)
  t.root.stop(); await vi.advanceTimersByTimeAsync(1000)
  expect(t.error).toHaveBeenCalledOnce()
  finish(new Map()); await Promise.resolve(); await Promise.resolve()
  expect(t.error).toHaveBeenCalledOnce()
})
it('drops an old-epoch stats completion without failing a paused live current source', async () => {
  const t = await connected()
  let finish!: (value: Map<string, Record<string, unknown>>) => void
  t.peer.getStats.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  await vi.advanceTimersByTimeAsync(1000)
  const epoch = t.internal.audioHealthEpoch
  t.session.setExternalPlaybackActive(true, true)
  expect(t.internal.audioHealthEpoch).toBeGreaterThan(epoch)
  finish(new Map([['old', { id: 'old', type: 'media-source', kind: 'audio', trackIdentifier: t.cloud.id,
    totalAudioEnergy: 100, totalSamplesDuration: 100 }]]))
  await vi.advanceTimersByTimeAsync(10_000)
  expect(t.error).not.toHaveBeenCalled(); expect(t.cloud.readyState).toBe('live')
})
