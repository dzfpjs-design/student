import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { cancelLocalSpeech, localPlaybackOverlap, queueLocalPcm, speakLocally } from './LocalInputs'

describe('local and managed playback provenance', () => {
  const text = '当前会话还没有可核对的页面记录，我不能判断你刚才写了什么。'
  let utterance: SpeechSynthesisUtterance
  beforeEach(() => {
    vi.useFakeTimers(); vi.setSystemTime(10_000)
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    vi.stubGlobal('speechSynthesis', { getVoices: () => [{ name: 'Microsoft Huihui', lang: 'zh-CN', localService: true }],
      speak: vi.fn((value) => { utterance = value }), cancel: vi.fn() })
    vi.spyOn(localApi, 'voicePlayback').mockImplementation(async (_session, body) => ({ playback_id: body.playback_id }))
  })
  afterEach(() => { cancelLocalSpeech(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })
  const start = () => utterance.onstart?.(new Event('start') as SpeechSynthesisEvent)

  it('registers only actual local-only playback and orders ended after started acknowledgement', async () => {
    let acknowledge!: (value: { playback_id: string }) => void
    vi.mocked(localApi.voicePlayback).mockImplementationOnce(() => new Promise((resolve) => { acknowledge = resolve }))
    speakLocally(text, 'local-page', undefined, undefined, { localOnly: true })
    expect(localApi.voicePlayback).not.toHaveBeenCalled()
    start()
    const body = vi.mocked(localApi.voicePlayback).mock.calls[0][1]
    vi.setSystemTime(11_000)
    let settled = false
    const candidate = localPlaybackOverlap('local-page', text, { startedAt: 10_010, endedAt: 10_990 }).then((value) => { settled = true; return value })
    cancelLocalSpeech()
    await Promise.resolve()
    expect(settled).toBe(false)
    expect(localApi.voicePlayback).toHaveBeenCalledTimes(1)
    acknowledge({ playback_id: body.playback_id })
    expect(await candidate).toEqual({ playback_id: body.playback_id })
    expect(localApi.voicePlayback).toHaveBeenLastCalledWith('local-page', { ...body, event: 'ended' })
    expect(await localPlaybackOverlap('other-child', text, { startedAt: 10_010, endedAt: 10_990 })).toBeUndefined()
    expect(await localPlaybackOverlap('local-page', text, { startedAt: 11_010, endedAt: 11_050 })).toBeUndefined()
  })

  it('does not register a cancelled late onstart or text-only failure', () => {
    speakLocally(text, 'cancelled', undefined, undefined, { localOnly: true })
    cancelLocalSpeech(); start()
    expect(localApi.voicePlayback).not.toHaveBeenCalled()
  })

  it('does not mark failed registration, mixed new content, short answers, or changed mathematics', async () => {
    const equation = '我们先核对这道数学题的完整计算步骤：3.5+2=5.5。'
    speakLocally(equation, 'math', undefined, undefined, { localOnly: true }); start()
    vi.setSystemTime(11_000)
    const interval = { startedAt: 10_010, endedAt: 10_990 }
    expect(await localPlaybackOverlap('math', equation.replace('3.5', '35'), interval)).toBeUndefined()
    expect(await localPlaybackOverlap('math', equation + '但是我要问另一道题', interval)).toBeUndefined()
    expect(await localPlaybackOverlap('math', '5.5', interval)).toBeUndefined()
    expect(await localPlaybackOverlap('math', equation.slice(2, -1), interval)).toEqual({ playback_id: expect.any(String) })
    cancelLocalSpeech()
    vi.mocked(localApi.voicePlayback).mockRejectedValue(new Error('offline'))
    speakLocally(text, 'offline', undefined, undefined, { localOnly: true }); start()
    vi.setSystemTime(12_000)
    expect(await localPlaybackOverlap('offline', text, { startedAt: 11_010, endedAt: 11_990 })).toBeUndefined()
  })

  it('keeps the exact same 4000-character bounded text for started, candidate matching and ended', async () => {
    const long = '这是保留在本地的页面文字。'.repeat(400)
    speakLocally(long, 'long', undefined, undefined, { localOnly: true }); start()
    vi.setSystemTime(11_000)
    const first = vi.mocked(localApi.voicePlayback).mock.calls[0][1]
    expect(first.text).toHaveLength(4000)
    expect(await localPlaybackOverlap('long', long, { startedAt: 10_010, endedAt: 10_990 })).toBeUndefined()
    cancelLocalSpeech(); await Promise.resolve()
    expect(localApi.voicePlayback).toHaveBeenLastCalledWith('long', { ...first, event: 'ended' })
  })

  it('local listening and speech share one output owner and settle only on actual end', async () => {
    const sources: EventTarget[] = []
    vi.stubGlobal('AudioContext', class {
      currentTime = 0; state = 'running'; destination = {}; close = vi.fn().mockResolvedValue(undefined)
      createBuffer(_channels: number, count: number, rate: number) { return { duration: count / rate, getChannelData: () => new Float32Array(count) } }
      createBufferSource() {
        let renderEnd = 0
        const source = Object.assign(new EventTarget(), { buffer: null, connect: vi.fn(), start: vi.fn(), stop: vi.fn() })
        source.start.mockImplementation((when: number) => { renderEnd = when + (source.buffer as unknown as AudioBuffer).duration })
        source.addEventListener('ended', () => { this.currentTime = renderEnd }, { once: true })
        sources.push(source); return source
      }
    })
    speakLocally('先听材料', 'same-session', undefined, undefined, { localOnly: true })
    const done = vi.fn()
    const listening = queueLocalPcm(new Int16Array([1, 2]).buffer, 16000, 'same-session', undefined, { onPlaybackEnd: done })
    expect(sources).toHaveLength(0)
    utterance.onend?.(new Event('end') as SpeechSynthesisEvent)
    await vi.advanceTimersByTimeAsync(100)
    expect(sources).toHaveLength(1); expect(done).not.toHaveBeenCalled()
    sources[0].dispatchEvent(new Event('ended'))
    await expect(listening).resolves.toBe('finished')
    expect(done).toHaveBeenCalledOnce()
  })

  it('stale audio never opens output and a consumer error does not lose settlement', async () => {
    const failed = vi.fn(() => { throw new Error('consumer unavailable') })
    await expect(queueLocalPcm(new Int16Array([1]).buffer, 16000, 'old-session', undefined,
      { isCurrent: () => false, onNotDelivered: failed })).resolves.toBe('expired')
    expect(failed).toHaveBeenCalledOnce()
  })

  it('PCM preserves the original queue admission read before opening any output', async () => {
    const opened = vi.fn(), done = vi.fn(), rejected = vi.fn()
    vi.stubGlobal('AudioContext', class { constructor() { opened(); throw Error('must not open') } })
    let resolve!: (allowed: boolean) => void
    const read = vi.fn(() => new Promise<boolean>(r => { resolve = r }))
    const pending = queueLocalPcm(new Int16Array([1]).buffer, 16000, 'admission', undefined,
      { beforePlayback: read, onPlaybackEnd: done, onNotDelivered: rejected })
    await vi.advanceTimersByTimeAsync(1)
    expect(read).toHaveBeenCalledOnce()
    expect(opened).not.toHaveBeenCalled()
    resolve(false)
    await expect(pending).resolves.toBe('superseded')
    expect(opened).not.toHaveBeenCalled(); expect(done).not.toHaveBeenCalled()
    expect(rejected).toHaveBeenCalledExactlyOnceWith('superseded')
  })
})
