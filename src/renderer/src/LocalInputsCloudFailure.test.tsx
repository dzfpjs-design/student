import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { cancelLocalSpeech, companionPlaybackCaptureCooldownMs, HandsFreeVoiceInput, speakLocally } from './LocalInputs'
import { getCompanionVoiceState, LocalAgentStreamingTtsProvider } from './companionVoice'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'

const notice = '云端暂时没听清，这句话没有执行，请稍后再说'
const unavailable = () => new ApiError('cloud recognizer unavailable', 503, 'ASR_CLOUD_DIALOGUE_UNAVAILABLE')
const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')

describe('hands-free cloud dialogue failure feedback', () => {
  const processors: Array<{ onaudioprocess: ((event: AudioProcessingEvent) => void) | null }> = []
  const localVoice = { name: 'Microsoft Huihui', voiceURI: 'local-huihui', lang: 'zh-CN', localService: true }
  let speak: ReturnType<typeof vi.fn<(utterance: SpeechSynthesisUtterance) => void>>
  let voices: typeof localVoice[]
  let onTranscript: ReturnType<typeof vi.fn<(text: string) => void>>

  beforeEach(() => {
    vi.useFakeTimers()
    processors.length = 0
    voices = [localVoice]
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
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), speak, getVoices: () => voices, addEventListener: vi.fn(), removeEventListener: vi.fn() })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('unmocked network forbidden')))
    const track = { stop: vi.fn(), getSettings: () => ({}), getCapabilities: () => ({}) }
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] }),
      enumerateDevices: vi.fn().mockResolvedValue([]),
    } })
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'asr-mic', status: 'active', expires_at: 'later' })
    vi.spyOn(localApi, 'renewDeviceLease').mockImplementation(async leaseId => ({ lease_id: leaseId, status: 'active', expires_at: new Date(Date.now() + 180_000).toISOString() }))
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'asr-mic', status: 'released' })
    vi.spyOn(localApi, 'transcribe').mockRejectedValue(unavailable())
    vi.spyOn(localApi, 'transcribeStandby').mockRejectedValue(unavailable())
    vi.spyOn(localApi, 'cancelTranscriptionAction').mockResolvedValue({} as never)
    vi.spyOn(localApi, 'voicePlayback').mockImplementation(async (_session, body) => ({ playback_id: body.playback_id }))
    vi.spyOn(localApi, 'transcriptionAction').mockResolvedValue({} as never)
    vi.spyOn(LocalAgentStreamingTtsProvider.prototype, 'stream')
  })

  afterEach(() => {
    cleanup(); cancelLocalSpeech(); resetVoiceCaptureCoordinatorForTests(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers()
    if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
    else Reflect.deleteProperty(navigator, 'mediaDevices')
  })

  const mount = async (wakeWord?: string, activation = 0) => {
    let view!: ReturnType<typeof render>
    await act(async () => {
      view = render(<HandsFreeVoiceInput sessionId="cloud-asr" childId="a" purpose="agent_command" autoStart wakeWord={wakeWord} externalActivationSignal={activation} inactivitySeconds={5} onTranscript={onTranscript} />)
      await vi.advanceTimersByTimeAsync(1)
    })
    // Device identity hashing uses real async crypto even with fake timers.
    // Let mount effects commit, then wait for the lease before injecting audio.
    await act(async () => { await vi.waitFor(() => expect(localApi.registerDevice).toHaveBeenCalled()) })
    if (activation) await act(async () => {
      view.rerender(<HandsFreeVoiceInput sessionId="cloud-asr" childId="a" purpose="agent_command" autoStart wakeWord={wakeWord} externalActivationSignal={activation + 1} inactivitySeconds={5} onTranscript={onTranscript} />)
    })
    return view
  }
  const capture = async () => {
    for (let index = 0; index < 4; index += 1) await act(async () => {
      await vi.advanceTimersByTimeAsync(256)
      processors.at(-1)?.onaudioprocess?.({ inputBuffer: { getChannelData: () => new Float32Array(4096).fill(0.05) } } as unknown as AudioProcessingEvent)
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(1_200) })
  }
  const finishNotice = async () => { await act(async () => {
    const utterance = speak.mock.calls.at(-1)?.[0]
    utterance?.onend?.(new Event('end') as SpeechSynthesisEvent)
    await vi.advanceTimersByTimeAsync(companionPlaybackCaptureCooldownMs + 1)
  }) }

  it('speaks a generic local-only notice without submitting text, using standby fallback, or simulating success', async () => {
    await mount()
    await capture()
    expect(localApi.transcribe).toHaveBeenCalledTimes(1)
    expect(speak).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ text: notice, voice: localVoice }))
    expect(screen.getByText(notice)).toBeTruthy()
    expect(onTranscript).not.toHaveBeenCalled()
    expect(localApi.transcribeStandby).not.toHaveBeenCalled()
    expect(LocalAgentStreamingTtsProvider.prototype.stream).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('does not repeat notices for failures during or after its own playback', async () => {
    await mount()
    await capture()
    await capture()
    await finishNotice()
    await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
    await capture()
    expect(localApi.transcribe).toHaveBeenCalledTimes(2)
    expect(speak).toHaveBeenCalledTimes(1)
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it('can report a new outage after successful real speech and the cooldown', async () => {
    await mount()
    await capture()
    await finishNotice()
    vi.mocked(localApi.transcribe).mockResolvedValueOnce({ transcript: '请帮我学分数', status: 'completed', cloud_used: true } as Awaited<ReturnType<typeof localApi.transcribe>>)
    await capture()
    await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
    await capture()
    expect(speak).toHaveBeenCalledTimes(2)
    expect(onTranscript).toHaveBeenCalledExactlyOnceWith('请帮我学分数', undefined, undefined, undefined, undefined, null)
  })

  it.each(['privacy', 'unmount', 'child', 'stop'] as const)('does not speak a late failure after %s', async (change) => {
    let reject!: (cause: unknown) => void
    vi.mocked(localApi.transcribe).mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail }))
    const view = await mount()
    await capture()
    expect(localApi.transcribe).toHaveBeenCalledTimes(1)
    await act(async () => {
      if (change === 'privacy') window.dispatchEvent(new Event('hsc-privacy-pause'))
      else if (change === 'unmount') view.unmount()
      else if (change === 'child') view.rerender(<HandsFreeVoiceInput sessionId="cloud-asr-b" childId="b" purpose="agent_command" onTranscript={onTranscript} />)
      else if (change === 'stop') fireEvent.click(screen.getByRole('button'))
    })
    await act(async () => { reject(unavailable()) })
    expect(speak).not.toHaveBeenCalled()
    expect(onTranscript).not.toHaveBeenCalled()
    expect(vi.mocked(localApi.transcribe).mock.calls[0][3]?.aborted).toBe(true)
  })

  it('keeps a delayed ASR failure visible in standby until real recognition recovers, without repeating notices', async () => {
    let reject!: (cause: unknown) => void
    vi.mocked(localApi.transcribe).mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail }))
    await mount('小知', 1); await capture()
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000) })
    expect(screen.getByRole('button', { name: '正在听 · 连续对话 · 1句处理中' })).toBeEnabled()
    await act(async () => reject(unavailable()))
    expect(speak).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ text: notice }))
    expect(onTranscript).not.toHaveBeenCalled()
    await finishNotice()
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000) })
    expect(screen.getByRole('button', { name: '语音识别暂不可用 · 正在重试' })).toBeEnabled()
    await capture()
    expect(localApi.transcribeStandby).toHaveBeenCalledOnce()
    expect(speak).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: '语音识别暂不可用 · 正在重试' })).toBeEnabled()
    vi.mocked(localApi.transcribeStandby).mockResolvedValueOnce({ transcript: '今天风很轻', status: 'completed', cloud_used: false } as Awaited<ReturnType<typeof localApi.transcribeStandby>>)
    await capture()
    expect(screen.getByRole('button', { name: '正在听唤醒词 · 小知' })).toBeEnabled()
    expect(onTranscript).not.toHaveBeenCalled()
    expect(speak).toHaveBeenCalledTimes(1)
  })

  it('never announces this error from wake standby ASR', async () => {
    await mount('小知')
    await capture()
    expect(localApi.transcribeStandby).toHaveBeenCalledTimes(1)
    expect(localApi.transcribe).not.toHaveBeenCalled()
    expect(speak).not.toHaveBeenCalled()
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it.each([
    ['wake standby', 'ASR_NO_HUMAN_SPEECH', '小知'],
    ['continuous conversation', 'ASR_NO_MEANINGFUL_SPEECH', undefined],
  ] as const)('silently discards %s input rejected as non-speech', async (_label, code, wakeWord) => {
    const rejection = new ApiError('private recognizer detail', 503, code)
    if (wakeWord) vi.mocked(localApi.transcribeStandby).mockRejectedValueOnce(rejection)
    else vi.mocked(localApi.transcribe).mockRejectedValueOnce(rejection)
    await mount(wakeWord)
    await capture()
    expect(speak).not.toHaveBeenCalled()
    expect(onTranscript).not.toHaveBeenCalled()
    expect(screen.queryByText('private recognizer detail')).toBeNull()
  })

  it('does not announce intentional transport cancellation as a failed instruction', async () => {
    vi.mocked(localApi.transcribe).mockRejectedValueOnce(new DOMException('cancelled', 'AbortError'))
    await mount()
    await capture()
    expect(speak).not.toHaveBeenCalled()
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it('announces other recognition failures locally without reading internal error details', async () => {
    vi.mocked(localApi.transcribe).mockRejectedValueOnce(new ApiError('internal transport detail', 500, 'OTHER_ERROR'))
    await mount(); await capture()
    expect(speak).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ text: '刚才这句话没有识别成功，也没有执行。请稍后再说一次。' }))
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it('does not interrupt an existing reply and can notify on a subsequent failure once it finishes', async () => {
    await mount()
    await act(async () => { speakLocally('正在讲解的内容', 'cloud-asr', undefined, undefined, { localOnly: true }) })
    await capture()
    expect(speak).toHaveBeenCalledTimes(1)
    await finishNotice()
    await capture()
    expect(speak).toHaveBeenLastCalledWith(expect.objectContaining({ text: notice }))
    expect(speak).toHaveBeenCalledTimes(2)
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it('cancels its own notice if privacy stops while local voices are still loading', async () => {
    voices = []
    await mount()
    await capture()
    expect(getCompanionVoiceState()).toBe('preparing')
    await act(async () => {
      window.dispatchEvent(new Event('hsc-privacy-pause'))
      voices = [localVoice]
      await vi.advanceTimersByTimeAsync(2_000)
    })
    expect(speak).not.toHaveBeenCalled()
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it('does not cancel an unrelated reply that replaced the failure notice', async () => {
    const view = await mount()
    await capture()
    await act(async () => { speakLocally('新的学习回复', undefined, undefined, undefined, { localOnly: true }) })
    expect(getCompanionVoiceState()).toBe('speaking')
    const cancellations = vi.mocked(window.speechSynthesis.cancel).mock.calls.length
    view.unmount()
    expect(window.speechSynthesis.cancel).toHaveBeenCalledTimes(cancellations)
    expect(getCompanionVoiceState()).toBe('speaking')
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it('preserves active and queued recordings across tutorial wake-policy changes without requiring another wake', async () => {
    const replies: Array<(value: Awaited<ReturnType<typeof localApi.transcribe>>) => void> = []
    vi.mocked(localApi.transcribe).mockImplementation(() => new Promise((resolve) => { replies.push(resolve) }))
    let view!: ReturnType<typeof render>
    await act(async () => {
      view = render(<HandsFreeVoiceInput sessionId="cloud-asr" childId="a" purpose="wake_word_setup" autoStart onTranscript={onTranscript} />)
      await vi.advanceTimersByTimeAsync(1)
    })
    await capture()
    await capture()
    expect(localApi.transcribe).toHaveBeenCalledTimes(1)
    const pendingSignal = vi.mocked(localApi.transcribe).mock.calls[0][3]!
    await act(async () => {
      view.rerender(<HandsFreeVoiceInput sessionId="cloud-asr" childId="a" purpose="agent_command" wakeWord="小知" autoStart onTranscript={onTranscript} />)
    })
    expect(pendingSignal.aborted).toBe(false)
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(localApi.releaseDeviceLease).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /正在听 · 连续对话/ })).toBeTruthy()
    await act(async () => { replies[0]({ transcript: '我想先跳过教程', status: 'completed', cloud_used: true } as Awaited<ReturnType<typeof localApi.transcribe>>) })
    expect(localApi.transcribe).toHaveBeenCalledTimes(2)
    expect(vi.mocked(localApi.transcribe).mock.calls[1][1]).toBe('wake_word_setup')
    await act(async () => { replies[1]({ transcript: '接着帮我学分数', status: 'completed', cloud_used: true } as Awaited<ReturnType<typeof localApi.transcribe>>) })
    await capture()
    expect(vi.mocked(localApi.transcribe).mock.calls[2][1]).toBe('agent_command')
    await act(async () => { replies[2]({ transcript: '再看看英语', status: 'completed', cloud_used: true } as Awaited<ReturnType<typeof localApi.transcribe>>) })
    expect(onTranscript.mock.calls.map(([text]) => text)).toEqual(['我想先跳过教程', '接着帮我学分数', '再看看英语'])
    expect(localApi.transcribeStandby).not.toHaveBeenCalled()
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(localApi.releaseDeviceLease).not.toHaveBeenCalled()
  })
})
