import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RemotePlaybackObservation } from './remotePlaybackObservation'

describe('fresh remote PCM observation', () => {
  let clock: number, signal: number, state: AudioContextState
  let close: ReturnType<typeof vi.fn>, disconnect: ReturnType<typeof vi.fn>
  let resume: ReturnType<typeof vi.fn>, resolveResume: () => void
  let audio: HTMLAudioElement, stream: MediaStream, track: { readyState: string }
  let current: RemotePlaybackObservation | undefined
  beforeEach(() => {
    vi.useFakeTimers()
    clock = 0; signal = 0; state = 'running'
    close = vi.fn(async () => { state = 'closed' }); disconnect = vi.fn()
    resume = vi.fn(() => new Promise<void>(resolve => { resolveResume = () => { state = 'running'; resolve() } }))
    vi.stubGlobal('AudioContext', class {
      sampleRate = 48000; destination = {}
      get currentTime() { return clock }
      get state() { return state }
      close = close; resume = resume
      createAnalyser() { return { fftSize: 512, connect: vi.fn(), disconnect,
        getFloatTimeDomainData: (samples: Float32Array) => samples.fill(signal) } }
      createGain() { return { gain: { value: 1 }, connect: vi.fn(), disconnect } }
      createMediaStreamSource() { return { connect: vi.fn(), disconnect } }
    })
    audio = { muted: false, paused: false, readyState: 2 } as HTMLAudioElement
    track = { readyState: 'live' }
    stream = { getAudioTracks: () => [track] } as unknown as MediaStream
  })
  afterEach(() => { current?.dispose(); current = undefined; vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })
  const observe = () => { current = new RemotePlaybackObservation(audio, stream); return current }
  const tick = async () => { clock += 0.02; await vi.advanceTimersByTimeAsync(20) }

  it('ignores primed silence and waits for fresh samples on an active player', async () => {
    const started = vi.fn(), unavailable = vi.fn()
    observe().arm(started, unavailable)
    await tick(); expect(started).not.toHaveBeenCalled()
    signal = 0.1; await tick()
    expect(started).toHaveBeenCalledOnce(); expect(unavailable).not.toHaveBeenCalled()
    await tick(); expect(started).toHaveBeenCalledOnce()
  })

  it.each(['muted', 'paused', 'buffering'] as const)('does not accept PCM while the player is %s', async mode => {
    if (mode === 'buffering') Object.assign(audio, { readyState: 1 })
    else Object.assign(audio, { [mode]: true })
    signal = 0.1
    const started = vi.fn(); observe().arm(started, vi.fn())
    await tick(); expect(started).not.toHaveBeenCalled()
  })

  it('does not accept a held pre-authorization sample or comfort noise', async () => {
    signal = 0.1
    const started = vi.fn(); observe().arm(started, vi.fn())
    await vi.advanceTimersByTimeAsync(40)
    expect(started).not.toHaveBeenCalled()
    signal = 0.0001; await tick(); expect(started).not.toHaveBeenCalled()
    signal = 0.1; await tick(); expect(started).toHaveBeenCalledOnce()
  })

  it('disarms a previous owner without allowing a queued frame to revive it', async () => {
    const old = vi.fn(), next = vi.fn(); const observer = observe()
    observer.arm(old, vi.fn()); observer.arm(next, vi.fn())
    signal = 0.1; await tick()
    expect(old).not.toHaveBeenCalled(); expect(next).toHaveBeenCalledOnce()
  })

  it('closes a cancelled context and ignores its late resume', async () => {
    state = 'suspended'
    const started = vi.fn(), unavailable = vi.fn(); observe().arm(started, unavailable)
    current!.dispose(); resolveResume(); await tick()
    expect(started).not.toHaveBeenCalled(); expect(unavailable).not.toHaveBeenCalled()
    expect(close).toHaveBeenCalledOnce(); expect(disconnect).toHaveBeenCalledTimes(3)
  })

  it('bounds a suspended context without authorizing a late resume', async () => {
    state = 'suspended'
    const started = vi.fn(), unavailable = vi.fn(); observe().arm(started, unavailable)
    await vi.advanceTimersByTimeAsync(1500)
    expect(unavailable).toHaveBeenCalledOnce()
    resolveResume(); signal = 0.1; await tick(); expect(started).not.toHaveBeenCalled()
  })

  it.each(['ended', 'invalid_samples'])('fails closed on %s', async mode => {
    const unavailable = vi.fn(), started = vi.fn(); observe().arm(started, unavailable)
    if (mode === 'ended') track.readyState = 'ended'
    else signal = Number.NaN
    await tick()
    expect(unavailable).toHaveBeenCalledOnce(); expect(started).not.toHaveBeenCalled()
  })

  it('releases an allocated context when graph construction fails', () => {
    vi.stubGlobal('AudioContext', class { close = close; createAnalyser() { throw new Error('graph_failure') } })
    expect(() => observe()).toThrow('graph_failure')
    expect(close).toHaveBeenCalledOnce()
  })
})
