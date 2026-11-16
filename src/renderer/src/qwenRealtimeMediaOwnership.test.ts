import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'

const observers = vi.hoisted(() => [] as { observed?: () => void; disposed: boolean }[])
// This suite tests ownership, not PCM quality. Keep callbacks callable after
// disposal to exercise late notifications from a retired native observer.
vi.mock('./remotePlaybackObservation', () => ({ RemotePlaybackObservation: class {
  state = { observed: undefined as (() => void) | undefined, disposed: false }
  constructor() { observers.push(this.state) }
  arm(observed: () => void) { this.state.observed = observed }
  disarm() {}
  dispose() { this.state.disposed = true }
} }))

type Session = {
  attachRemoteAudio: (event: RTCTrackEvent) => void
  bindChannel: (channel: RTCDataChannel) => void
  remoteAudio: HTMLAudioElement | null
  playbackAuthorized: boolean
  setRemotePlaybackAuthorized: (authorized: boolean) => void
  pendingPlayback: { onPlaybackStart: () => void; onPlaybackAbandoned: () => void } | null
  receive: (raw: string) => void
  close: () => void
  connect: () => Promise<void>
}

describe('realtime media callback ownership', () => {
  const sessions: Session[] = []
  beforeEach(() => {
    observers.length = 0
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined)
  })
  afterEach(() => {
    sessions.splice(0).forEach(session => session.close())
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })
  function setup() {
    const onPlaybackStart = vi.fn(), onPlaybackAbandoned = vi.fn(), onError = vi.fn(), onStatus = vi.fn()
    const session = new QwenRealtimeVoiceSession({ childName: 'test', callbacks: { onStatus, onError } }) as unknown as Session
    sessions.push(session)
    return { session, onPlaybackStart, onPlaybackAbandoned, onError }
  }
  function attach(session: Session, id: string) {
    session.attachRemoteAudio({ track: { kind: 'audio' }, streams: [{ id }] } as unknown as RTCTrackEvent)
    return session.remoteAudio!
  }
  function channel() {
    const events = new EventTarget()
    return Object.assign(events, { readyState: 'open', close: vi.fn(), send: vi.fn() }) as unknown as RTCDataChannel
  }

  function microphone(readyState = 'live', releasing?: Promise<void>) {
    const track = Object.assign(new EventTarget(), { kind: 'audio', enabled: true, readyState,
      getSettings: () => ({ echoCancellation: true }), stop: vi.fn() })
    const cloudTrack = Object.assign(new EventTarget(), { kind:'audio',enabled:true,readyState,
      getSettings:()=>track.getSettings(),stop:vi.fn() })
    track.stop.mockImplementation(()=>{track.readyState='ended'})
    cloudTrack.stop.mockImplementation(()=>{cloudTrack.readyState='ended'})
    Object.assign(track,{clone:vi.fn(()=>cloudTrack)})
    const dataChannel = channel()
    class Peer extends EventTarget {
      iceGatheringState = 'complete'
      localDescription = { sdp: 'offer' }
      addTrack = vi.fn((value: unknown) => {
        expect(value).toBe(cloudTrack);expect(value).not.toBe(track)
        const sender={track:value,replaceTrack:vi.fn(async (next:unknown)=>{if(next&&releasing)await releasing;sender.track=next})}
        return sender
      })
      createDataChannel = () => dataChannel
      createOffer = vi.fn().mockResolvedValue({ type: 'offer', sdp: 'offer' })
      setLocalDescription = vi.fn().mockResolvedValue(undefined)
      setRemoteDescription = vi.fn().mockResolvedValue(undefined)
      close = vi.fn()
    }
    vi.stubGlobal('RTCPeerConnection', Peer)
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn().mockResolvedValue({
      getTracks: () => [track], getAudioTracks: () => [track],
    }) } })
    vi.stubGlobal('desktop', undefined)
    Object.assign(window, { desktop: { negotiateQwenRealtime: vi.fn().mockResolvedValue({ answerSdp: 'answer' }) } })
    return Object.assign(track,{cloudTrack})
  }

  it('reports a disconnected microphone immediately instead of remaining silently listening', async () => {
    const track = microphone()
    const { session, onError, onPlaybackAbandoned } = setup()
    await session.connect()
    session.pendingPlayback = { onPlaybackStart: vi.fn(), onPlaybackAbandoned }
    session.playbackAuthorized = true
    track.readyState='ended';track.dispatchEvent(new Event('ended'))
    expect(onError).toHaveBeenCalledExactlyOnceWith('麦克风音轨已断开，正在重新连接。')
    expect(track.enabled).toBe(true);expect(track.cloudTrack.enabled).toBe(false)
    expect(onPlaybackAbandoned).toHaveBeenCalledOnce()
    session.close()
    track.dispatchEvent(new Event('ended'))
    expect(onError).toHaveBeenCalledOnce()
  })

  it('rejects an already ended acquired microphone without negotiating a dead stream', async () => {
    const track = microphone('ended')
    const { session } = setup()
    await expect(session.connect()).rejects.toThrow('麦克风音轨已断开')
    expect(window.desktop!.negotiateQwenRealtime).not.toHaveBeenCalled()
    session.close()
    expect(track.stop).toHaveBeenCalledOnce()
  })

  it('negotiates advertised system AEC before registration and reports the actual scope', async () => {
    const track = microphone()
    let mode: unknown = true
    track.getSettings = () => ({ echoCancellation: mode as boolean })
    const apply = vi.fn(async () => { mode = 'all' })
    Object.assign(track, { getCapabilities: () => ({ echoCancellation: ['all', true, false] }), applyConstraints: apply })
    const policy = vi.fn(), capability = vi.fn(), registered = vi.fn()
    const session = new QwenRealtimeVoiceSession({ childName: 'test', fullDuplex: true, callbacks: {
      onStatus: vi.fn(), onEchoCancellationPolicy: policy, onDuplexCapability: capability,
      onMicrophoneOpen: async original => { expect(original).toBe(track);expect(track.enabled).toBe(true);expect(track.cloudTrack.enabled).toBe(false);expect(apply).toHaveBeenCalledOnce();registered() },
    } }) as unknown as Session
    sessions.push(session)
    await session.connect()
    expect(policy).toHaveBeenCalledExactlyOnceWith({ mode: 'all', negotiation: 'accepted', acoustic_quality_verified: false })
    expect(capability).toHaveBeenCalledExactlyOnceWith(true)
    expect(registered).toHaveBeenCalledOnce()
  })

  it.each(['close', 'ended'])('does not register or negotiate a peer after %s during AEC setup', async reason => {
    const track = microphone(), registered = vi.fn(), policy = vi.fn(), onError = vi.fn()
    let finish!: () => void
    const apply = vi.fn(() => new Promise<void>(resolve => { finish = resolve }))
    Object.assign(track, { getCapabilities: () => ({ echoCancellation: ['all'] }), applyConstraints: apply })
    const session = new QwenRealtimeVoiceSession({ childName: 'test', callbacks: {
      onStatus: vi.fn(), onError, onMicrophoneOpen: registered, onEchoCancellationPolicy: policy,
    } }) as unknown as Session
    sessions.push(session)
    const connecting = session.connect()
    for (let i = 0; i < 6 && !apply.mock.calls.length; i += 1) await Promise.resolve()
    expect(apply).toHaveBeenCalledOnce()
    if (reason === 'close') session.close()
    else { track.readyState = 'ended'; track.dispatchEvent(new Event('ended')) }
    finish()
    await connecting
    expect(onError).toHaveBeenCalledTimes(reason === 'ended' ? 1 : 0)
    expect(policy).not.toHaveBeenCalled()
    expect(registered).not.toHaveBeenCalled()
    expect(window.desktop!.negotiateQwenRealtime).not.toHaveBeenCalled()
    expect(track.enabled).toBe(true);expect(track.readyState).toBe('ended')
  })

  it('continues listening setup after a hung optional AEC negotiation without opening media early', async () => {
    vi.useFakeTimers()
    const track = microphone(), registered = vi.fn(), policy = vi.fn()
    Object.assign(track, { getCapabilities: () => ({ echoCancellation: ['all'] }),
      applyConstraints: vi.fn(() => new Promise<void>(() => undefined)) })
    const session = new QwenRealtimeVoiceSession({ childName: 'test', callbacks: {
      onStatus: vi.fn(), onMicrophoneOpen: registered, onEchoCancellationPolicy: policy,
    } }) as unknown as Session
    sessions.push(session)
    const connecting = session.connect()
    await vi.advanceTimersByTimeAsync(1199)
    expect(registered).not.toHaveBeenCalled();expect(track.enabled).toBe(true);expect(window.desktop!.negotiateQwenRealtime).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    await connecting
    expect(registered).toHaveBeenCalledOnce()
    expect(policy).toHaveBeenCalledWith({ mode: 'browser-selected', negotiation: 'timed_out', acoustic_quality_verified: false })
  })

  it('does not publish listening from a late track release after microphone disconnection', async () => {
    let release!: () => void
    const pending = new Promise<void>(resolve => { release = resolve })
    const track = microphone('live', pending), onStatus = vi.fn(), onError = vi.fn()
    const session = new QwenRealtimeVoiceSession({ childName: 'test', callbacks: { onStatus, onError } }) as unknown as Session
    sessions.push(session)
    await session.connect()
    session.receive(JSON.stringify({ type: 'session.updated' }))
    track.readyState='ended';track.dispatchEvent(new Event('ended'))
    release(); await pending; await Promise.resolve()
    expect(onStatus.mock.calls.at(-1)![0].phase).toBe('error')
    expect(track.enabled).toBe(true);expect(track.cloudTrack.enabled).toBe(false)
    expect(onError).toHaveBeenCalledOnce()
  })

  it('does not negotiate a new peer after the track ends while local registration is pending', async () => {
    let registered!: () => void
    const registration = new Promise<void>(resolve => { registered = resolve })
    const track = microphone(), onError = vi.fn()
    const session = new QwenRealtimeVoiceSession({ childName: 'test', callbacks: {
      onStatus: vi.fn(), onError, onMicrophoneOpen: () => registration,
    } }) as unknown as Session
    sessions.push(session)
    const connecting = session.connect()
    await Promise.resolve(); await Promise.resolve()
    track.readyState='ended';track.dispatchEvent(new Event('ended'))
    registered(); await connecting
    expect(window.desktop!.negotiateQwenRealtime).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledOnce()
  })

  it('mutes, stops and detaches the retired player instead of only removing its element', () => {
    const { session } = setup()
    session.playbackAuthorized = true
    const old = attach(session, 'old')
    const current = attach(session, 'current')
    expect(old.muted).toBe(true)
    expect(old.srcObject).toBeNull()
    expect(old.pause).toHaveBeenCalled()
    expect(old.isConnected).toBe(false)
    expect(current.isConnected).toBe(true)
    expect(current.muted).toBe(false)
  })

  it('does not attribute an old player start or error to the current reply', () => {
    const { session, onPlaybackStart, onPlaybackAbandoned, onError } = setup()
    const old = attach(session, 'old')
    session.pendingPlayback = { onPlaybackStart, onPlaybackAbandoned }
    session.setRemotePlaybackAuthorized(true)
    const retired = observers.at(-1)!
    const current = attach(session, 'current')
    expect(retired.disposed).toBe(true)
    retired.observed?.()
    old.dispatchEvent(new Event('playing'))
    old.dispatchEvent(new Event('error'))
    expect(onPlaybackStart).not.toHaveBeenCalled()
    expect(onPlaybackAbandoned).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
    current.dispatchEvent(new Event('playing'))
    expect(onPlaybackStart).not.toHaveBeenCalled()
    observers.at(-1)!.observed?.()
    expect(onPlaybackStart).toHaveBeenCalledOnce()
    current.dispatchEvent(new Event('error'))
    expect(onError).toHaveBeenCalledOnce()
    expect(onPlaybackAbandoned).toHaveBeenCalledOnce()
  })

  it('ignores retired data channel messages and errors but retains the current channel', () => {
    const { session, onError } = setup()
    const old = channel(), current = channel()
    session.bindChannel(old)
    session.bindChannel(current)
    const receive = vi.spyOn(session, 'receive')
    old.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ type: 'error', error: { message: 'old' } }) }))
    old.dispatchEvent(new Event('error'))
    expect(receive).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
    expect(old.close).toHaveBeenCalledOnce()
    current.dispatchEvent(new MessageEvent('message', { data: '{}' }))
    expect(receive).toHaveBeenCalledExactlyOnceWith('{}')
    current.dispatchEvent(new Event('error'))
    expect(onError).toHaveBeenCalledOnce()
  })

  it('does not reopen a retired channel from a delayed open event', () => {
    const { session, onError } = setup()
    const old = channel(), current = channel()
    session.bindChannel(old); session.bindChannel(current)
    old.dispatchEvent(new Event('open'))
    const receive = vi.spyOn(session, 'receive')
    current.dispatchEvent(new MessageEvent('message', { data: '{}' }))
    expect(receive).toHaveBeenCalledExactlyOnceWith('{}')
    old.dispatchEvent(new Event('error'))
    expect(onError).not.toHaveBeenCalled()
  })
})
