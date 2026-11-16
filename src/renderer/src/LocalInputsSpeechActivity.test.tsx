import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { cancelLocalSpeech, companionPlaybackCaptureCooldownMs, HandsFreeVoiceInput, speakLocally } from './LocalInputs'
import type { RealtimeVoiceStatus } from './realtimeVoiceRuntime'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import { getCompanionVoiceState } from './companionVoice'

const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
type AsrResult = Awaited<ReturnType<typeof localApi.transcribe>>

describe('hands-free PCM speech activity contract', () => {
  const processors: Array<{ onaudioprocess: ((event: AudioProcessingEvent) => void) | null }> = []
  const statuses: RealtimeVoiceStatus[] = []
  const onStatus = (status: RealtimeVoiceStatus): void => { statuses.push(status) }
  let track: EventTarget & { stop: ReturnType<typeof vi.fn>; getSettings: () => object; getCapabilities: () => object }
  let resolveAsr: (result: AsrResult) => void
  let onTranscript: ReturnType<typeof vi.fn<(text: string) => void>>
  let speak: ReturnType<typeof vi.fn<(utterance: SpeechSynthesisUtterance) => void>>

  beforeEach(() => {
    vi.useFakeTimers()
    processors.length = 0
    statuses.length = 0
    onTranscript = vi.fn()
    speak = vi.fn((utterance: SpeechSynthesisUtterance) => { utterance.onstart?.(new Event('start') as SpeechSynthesisEvent) })
    class AudioContext {
      sampleRate = 16_000
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createScriptProcessor = () => {
        const processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as ((event: AudioProcessingEvent) => void) | null }
        processors.push(processor)
        return processor
      }
      close = vi.fn().mockResolvedValue(undefined)
    }
    vi.stubGlobal('AudioContext', AudioContext)
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    vi.stubGlobal('speechSynthesis', {
      cancel: vi.fn(), speak, getVoices: () => [{ name: 'Microsoft Huihui', lang: 'zh-CN', localService: true }],
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
    track = Object.assign(new EventTarget(), { stop: vi.fn(), getSettings: () => ({}), getCapabilities: () => ({}) })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] }),
      enumerateDevices: vi.fn().mockResolvedValue([]),
    } })
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'activity-mic', status: 'active', expires_at: 'later' })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'activity-mic', status: 'released' })
    vi.spyOn(localApi, 'transcribe').mockImplementation(() => new Promise((resolve) => { resolveAsr = resolve }))
    vi.spyOn(localApi, 'cancelTranscriptionAction').mockResolvedValue({} as never)
    vi.spyOn(localApi, 'transcriptionAction').mockResolvedValue({} as never)
  })

  afterEach(() => {
    cleanup()
    cancelLocalSpeech()
    resetVoiceCaptureCoordinatorForTests()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
    if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
    else Reflect.deleteProperty(navigator, 'mediaDevices')
  })

  const props = () => ({ sessionId: 'activity-session', purpose: 'agent_command', onTranscript, onRuntimeStatusChange: onStatus })
  const mount = async () => {
    let view!: ReturnType<typeof render>
    await act(async () => {
      view = render(<HandsFreeVoiceInput {...props()} autoStart />)
      await vi.advanceTimersByTimeAsync(1)
    })
    await act(async () => { await vi.waitFor(() => expect(localApi.registerDevice).toHaveBeenCalled()) })
    return view
  }
  const frame = async (amplitude = 0.05, processor = processors.at(-1)!) => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(256)
      const samples = Float32Array.from({ length: 4096 }, (_, index) => Math.sin(index * Math.PI / 20) * amplitude)
      processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => samples } } as unknown as AudioProcessingEvent)
    })
  }
  const utterance = async () => { for (let index = 0; index < 4; index += 1) await frame() }
  const endpoint = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(1_200) }) }
  const last = () => statuses.at(-1)!

  it('does not equate an open listener, keyboard events or isolated noise frames with speech', async () => {
    await mount()
    expect(last()).toMatchObject({ phase: 'listening', speechActive: false, pendingUtterances: 0 })
    await act(async () => { fireEvent.keyDown(window, { key: 'a' }) })
    for (let index = 0; index < 3; index += 1) { await frame(0.5); await frame(0) }
    expect(statuses.some((status) => status.speechActive)).toBe(false)
    expect(localApi.transcribe).not.toHaveBeenCalled()
  })

  it('publishes on the third sustained frame before ASR and hands off without a false idle gap', async () => {
    await mount()
    await frame(); await frame()
    expect(last().speechActive).toBe(false)
    const start = statuses.length
    await frame()
    expect(last()).toMatchObject({ speechActive: true, pendingUtterances: 0 })
    expect(localApi.transcribe).not.toHaveBeenCalled()
    await frame()
    await endpoint()
    expect(last()).toMatchObject({ speechActive: false, pendingUtterances: 1 })
    expect(statuses.slice(start).every((status) => status.speechActive || status.pendingUtterances > 0)).toBe(true)
    await act(async () => { resolveAsr({ transcript: 'start learning', status: 'completed', cloud_used: true } as AsrResult) })
    expect(last()).toMatchObject({ speechActive: false, pendingUtterances: 0 })
    expect(onTranscript).toHaveBeenCalledOnce()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('captures and dispatches a whole utterance while offline speech has not started', async () => {
    await mount()
    speak.mockImplementation(() => undefined)
    await act(async () => { speakLocally('仍在准备，不是播放', undefined, undefined, undefined, { localOnly: true }) })
    expect(getCompanionVoiceState()).toBe('preparing')
    await utterance()
    expect(last().speechActive).toBe(true)
    await endpoint()
    expect(localApi.transcribe).toHaveBeenCalledOnce()
    await act(async () => { resolveAsr({ transcript: '我还没有做完作业', status: 'completed', cloud_used: true } as AsrResult) })
    expect(onTranscript).toHaveBeenCalledOnce()
    expect(onTranscript.mock.calls[0][0]).toBe('我还没有做完作业')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('actual playback start clears the preparing utterance and preserves tail protection even with no playback PCM frame', async () => {
    await mount()
    speak.mockImplementation(() => undefined)
    await utterance()
    expect(last().speechActive).toBe(true)
    await act(async () => { speakLocally('公开本地播报', undefined, undefined, undefined, { localOnly: true }) })
    await act(async () => {
      const current = speak.mock.calls.at(-1)![0]
      current.onstart?.(new Event('start') as SpeechSynthesisEvent)
      current.onend?.(new Event('end') as SpeechSynthesisEvent)
    })
    expect(last().speechActive).toBe(false)
    await frame(); await frame()
    expect(last().speechActive).toBe(false)
    expect(localApi.transcribe).not.toHaveBeenCalled()
    await endpoint()
    expect(localApi.transcribe).not.toHaveBeenCalled()
    await utterance(); await endpoint()
    expect(localApi.transcribe).toHaveBeenCalledOnce()
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it('holds through the existing endpoint silence, but detects a new utterance while ASR is pending', async () => {
    await mount()
    await utterance()
    await frame(0)
    expect(last().speechActive).toBe(true)
    await endpoint()
    await utterance()
    expect(last()).toMatchObject({ speechActive: true, pendingUtterances: 1 })
    await endpoint()
    expect(last()).toMatchObject({ speechActive: false, pendingUtterances: 2 })
    expect(localApi.transcribe).toHaveBeenCalledTimes(1)
  })

  it('does not grant acoustic interruption during TTS or from a single post-playback frame', async () => {
    await mount()
    await utterance()
    expect(last().speechActive).toBe(true)
    await act(async () => { speakLocally('local playback', undefined, undefined, undefined, { localOnly: true }) })
    expect(last().speechActive).toBe(false)
    const cancellations = vi.mocked(speechSynthesis.cancel).mock.calls.length
    await utterance()
    expect(last().speechActive).toBe(false)
    expect(speechSynthesis.cancel).toHaveBeenCalledTimes(cancellations)
    await act(async () => {
      speak.mock.calls.at(-1)![0].onend?.(new Event('end') as SpeechSynthesisEvent)
      await vi.advanceTimersByTimeAsync(companionPlaybackCaptureCooldownMs + 1)
    })
    await frame()
    expect(last().speechActive).toBe(false)
    await frame(); await frame()
    expect(last().speechActive).toBe(true)
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it.each(['privacy', 'unmount', 'context', 'stop', 'track-ended'] as const)('resets activity on %s and ignores late audio callbacks', async (reason) => {
    const view = await mount()
    await utterance()
    expect(last().speechActive).toBe(true)
    const oldProcessor = processors.at(-1)!
    await act(async () => {
      if (reason === 'privacy') window.dispatchEvent(new Event('hsc-privacy-pause'))
      else if (reason === 'unmount') view.unmount()
      else if (reason === 'context') view.rerender(<HandsFreeVoiceInput {...props()} sessionId="other-session" />)
      else if (reason === 'stop') fireEvent.click(screen.getByRole('button'))
      else track.dispatchEvent(new Event('ended'))
    })
    expect(last()).toMatchObject({ speechActive: false, pendingUtterances: 0 })
    await frame(0.05, oldProcessor)
    expect(last().speechActive).toBe(false)
    expect(track.stop).toHaveBeenCalled()
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it('clears pending and active together on privacy stop and ignores a late ASR result', async () => {
    await mount()
    await utterance(); await endpoint(); await utterance()
    expect(last()).toMatchObject({ speechActive: true, pendingUtterances: 1 })
    await act(async () => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
    expect(last()).toMatchObject({ phase: 'idle', speechActive: false, pendingUtterances: 0 })
    await act(async () => { resolveAsr({ transcript: 'late result', status: 'completed', cloud_used: true } as AsrResult) })
    expect(last()).toMatchObject({ speechActive: false, pendingUtterances: 0 })
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it('keeps ASR pending while backpressure releases the microphone', async () => {
    await mount()
    for (let index = 0; index < 8; index += 1) { await utterance(); await endpoint() }
    expect(last()).toMatchObject({ phase: 'backpressure', speechActive: false, pendingUtterances: 8 })
    expect(track.stop).toHaveBeenCalled()
    expect(localApi.transcribe).toHaveBeenCalledTimes(1)
  })

  it('clears the pending handoff after an ASR failure without inventing child input', async () => {
    vi.mocked(localApi.transcribe).mockRejectedValueOnce(new Error('isolated ASR failure'))
    await mount()
    await utterance(); await endpoint()
    expect(last()).toMatchObject({ speechActive: false, pendingUtterances: 0 })
    expect(onTranscript).not.toHaveBeenCalled()
    expect(speak).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ text: '刚才这句话没有识别成功，也没有执行。请稍后再说一次。' }))
  })

  it('publishes live frame changes to the current subscriber before a React effect can run', async () => {
    const view = await mount()
    const replacement = vi.fn()
    await act(async () => { view.rerender(<HandsFreeVoiceInput {...props()} autoStart onRuntimeStatusChange={replacement} />) })
    await frame(); await frame()
    const oldCalls = statuses.length
    await act(async () => {
      processors.at(-1)!.onaudioprocess?.({ inputBuffer: { getChannelData: () => new Float32Array(4096).fill(0.05) } } as unknown as AudioProcessingEvent)
      expect(replacement).toHaveBeenLastCalledWith(expect.objectContaining({ speechActive: true }))
    })
    expect(statuses).toHaveLength(oldCalls)
  })
})
