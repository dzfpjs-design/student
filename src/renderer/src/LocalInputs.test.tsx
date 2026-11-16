import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { asrFailureReason, cancelLocalSpeech, childUtteranceCaptureTiming, companionPlaybackCaptureCooldownMs, companionPlaybackCaptureGate, dispatchHandsFreeTranscript, handsFreeVoiceCaptureAdvice, HandsFreeVoiceInput, householdEntryCommand, householdEntryPhrases, HouseholdStandbyVoiceInput, VoiceInputButton, isExpectedNonSpeechAsrError, isLikelyLocalSpeechEcho, isLowInformationVoiceTranscript, microphoneRecoveryDelayMs, shouldAutomaticallyRecoverMicrophone, shouldDeliverCompletedHandsFreeTranscript, shouldDeliverHandsFreeTranscript, shouldDeliverManualTranscript, shouldFinishCapturedUtterance, shouldProcessQueuedHandsFreeSegment, shouldUseStandbyTranscription, speakLocally, stripLocalCompanionSpeechEcho, voiceCaptureSignalAdvice, voiceProfileCaptureAdvice } from './LocalInputs'
import { ApiError, localApi } from './api'
import { claimVoiceCapture, releaseVoiceCapture, resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import { enrollLocalVoiceProfile } from './voiceProfile'
import { LocalAgentStreamingTtsProvider, type CompanionSpeechResult } from './companionVoice'
import { clearVoiceCaptureContext, trackDiagnosticCapture } from './voiceCaptureContext'
import type { DiagnosticPromptResult } from './types'

const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')

it('records stable ASR failure codes without serializing provider errors or transcripts', () => {
  expect(asrFailureReason(new ApiError('provider detail', 503, 'ASR_UPSTREAM_UNAVAILABLE')))
    .toEqual({ reason_code: 'ASR_UPSTREAM_UNAVAILABLE', failure_kind: 'api' })
  expect(asrFailureReason(new TypeError('private network detail')))
    .toEqual({ reason_code: 'ASR_CLIENT_NETWORK_OR_PROTOCOL', failure_kind: 'network_or_protocol' })
})

function wav(frequency: number, amplitude = 8_000): Uint8Array {
  const sampleRate = 16_000; const samples = sampleRate
  const bytes = new Uint8Array(44 + samples * 2); const view = new DataView(bytes.buffer)
  const write = (offset: number, value: string): void => { for (let index = 0; index < value.length; index += 1) view.setUint8(offset + index, value.charCodeAt(index)) }
  write(0, 'RIFF'); view.setUint32(4, 36 + samples * 2, true); write(8, 'WAVE'); write(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(36, 'data'); view.setUint32(40, samples * 2, true)
  for (let index = 0; index < samples; index += 1) view.setInt16(44 + index * 2, Math.sin(index * Math.PI * 2 * frequency / sampleRate) * amplitude, true)
  return bytes
}

describe('local-only reply delivery', () => {
  afterEach(() => {
    cancelLocalSpeech()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it.each([{ localOnly: true }, { localOnly: true, sourceAgentTurnId: 'private-turn' }])('never sends a private memory reply to the synthesis API: %j', async (privacy) => {
    const text = '只保存在本机的记忆：孩子担心在学校被嘲笑。'
    const onPlaybackStart = vi.fn()
    const localVoice = { name: 'Microsoft Huihui', voiceURI: 'local-huihui', lang: 'zh-CN', localService: true }
    const speak = vi.fn()
    const stream = vi.spyOn(LocalAgentStreamingTtsProvider.prototype, 'stream')
    vi.stubGlobal('AudioContext', class { currentTime = 0; close = vi.fn().mockResolvedValue(undefined) })
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), speak, getVoices: () => [localVoice] })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))

    const result = await new Promise<CompanionSpeechResult>((resolve) => speakLocally(text, 'memory-session', resolve, onPlaybackStart, privacy))

    expect(result).toBe('system_female_offline')
    expect(stream).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
    expect(speak).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ text, voice: localVoice }))
    const utterance = speak.mock.calls[0][0] as SpeechSynthesisUtterance
    utterance.onstart?.(new Event('start') as SpeechSynthesisEvent)
    expect(onPlaybackStart).toHaveBeenCalledOnce()
  })
})

describe('household first-entry phrases', () => {
  it('accepts the fixed grade and legacy profile label before setup, but only the chosen wake word afterwards', () => {
    expect(householdEntryPhrases({ childId: 'primary', wakeWord: '四年级同学', configured: false, entryPhrase: '四年级' }))
      .toEqual(['四年级', '四年级同学'])
    expect(householdEntryPhrases({ childId: 'primary', wakeWord: '小知', configured: true, entryPhrase: '四年级' }))
      .toEqual(['小知'])
    expect(householdEntryCommand({ childId: 'primary', wakeWord: '四年级同学', configured: false, entryPhrase: '四年级' }, '我是')).toBe('')
    expect(householdEntryCommand({ childId: 'primary', wakeWord: '小知', configured: true, entryPhrase: '四年级' }, '帮我看题')).toBe('帮我看题')
  })
})

describe('conservative playback echo filtering', () => {
  const spoken = '我们再想想，方程里每一项的次数是看未知数的指数，最高次数是三，不是四。'
  let clock = 100_000
  let utterance: SpeechSynthesisUtterance

  beforeEach(() => {
    clock += 100_000
    vi.useFakeTimers()
    vi.setSystemTime(clock)
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    vi.stubGlobal('speechSynthesis', {
      cancel: vi.fn(), getVoices: () => [],
      speak: (value: SpeechSynthesisUtterance) => { utterance = value },
    })
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

  const duringPlayback = () => ({ startedAt: clock + 200, endedAt: clock + 1_000 })
  const startPlayback = (text = spoken) => {
    speakLocally(text)
    utterance.onstart?.(new Event('start') as SpeechSynthesisEvent)
    vi.setSystemTime(clock + 1_000)
  }

  it('blocks microphone capture throughout playback and its acoustic tail', () => {
    expect(companionPlaybackCaptureGate('speaking', 1_000, 0)).toEqual({
      blocked: true,
      blockedUntil: 1_000 + companionPlaybackCaptureCooldownMs,
    })
    expect(companionPlaybackCaptureGate('idle', 1_849, 1_850).blocked).toBe(true)
    expect(companionPlaybackCaptureGate('idle', 1_850, 1_850).blocked).toBe(false)
  })

  it('requires capture timing even for an identical long transcript', () => {
    startPlayback()
    expect(stripLocalCompanionSpeechEcho(spoken)).toEqual({ transcript: spoken, echoDetected: false, removedCharacters: 0 })
  })

  it('does not confuse a pending playback request with audible output', () => {
    speakLocally(spoken)
    vi.setSystemTime(clock + 1_000)
    expect(isLikelyLocalSpeechEcho(spoken, duringPlayback())).toBe(false)
  })

  it.each(['三', '次数是三', '最高次数是三', '方程里每一项的次数是看未知数的指数'])('retains the child repeating the answer: %s', (answer) => {
    startPlayback()
    expect(stripLocalCompanionSpeechEcho(answer, duringPlayback())).toEqual({ transcript: answer, echoDetected: false, removedCharacters: 0 })
  })

  it('retains even an exact short spoken answer during playback', () => {
    startPlayback('最高次数是三')
    expect(isLikelyLocalSpeechEcho('最高次数是三', duringPlayback())).toBe(false)
  })

  it.each([
    '方程里每一项的次数是看未知数的指数，我觉得最高次数是四。',
    `次数是四。${spoken}`,
    `${spoken}不对，我觉得是四。`,
    `我记住了，${spoken}我是这样理解的。`,
    '最高次数是三不是四，方程里每一项的次数是看未知数的指数。',
  ])('retains mixed or reordered speech without deleting the child contribution: %s', (transcript) => {
    startPlayback()
    expect(stripLocalCompanionSpeechEcho(transcript, duringPlayback())).toEqual({ transcript, echoDetected: false, removedCharacters: 0 })
  })

  it('discards an exact long transcript captured wholly during companion playback', () => {
    startPlayback()
    expect(stripLocalCompanionSpeechEcho(spoken, duringPlayback())).toEqual({ transcript: '', echoDetected: true, removedCharacters: spoken.length })
  })

  it('retains a child recitation immediately after playback ends, without an estimated echo tail', () => {
    startPlayback()
    utterance.onend?.(new Event('end') as SpeechSynthesisEvent)
    vi.setSystemTime(clock + 1_500)
    expect(isLikelyLocalSpeechEcho(spoken, { startedAt: clock + 1_001, endedAt: clock + 1_500 })).toBe(false)
  })

  it('retains an utterance crossing the playback end rather than assuming it is all echo', () => {
    startPlayback()
    utterance.onend?.(new Event('end') as SpeechSynthesisEvent)
    vi.setSystemTime(clock + 1_500)
    expect(isLikelyLocalSpeechEcho(spoken, { startedAt: clock + 200, endedAt: clock + 1_500 })).toBe(false)
  })

  it('uses capture time, not delayed ASR completion time, for an echo candidate', () => {
    startPlayback()
    utterance.onend?.(new Event('end') as SpeechSynthesisEvent)
    vi.setSystemTime(clock + 90_000)
    expect(isLikelyLocalSpeechEcho(spoken, duringPlayback())).toBe(true)
  })

  it('does not extend the old playback window when an identical new request is pending', async () => {
    startPlayback()
    cancelLocalSpeech()
    vi.setSystemTime(clock + 1_200)
    speakLocally(spoken)
    vi.setSystemTime(clock + 1_500)
    await Promise.resolve()
    expect(isLikelyLocalSpeechEcho(spoken, { startedAt: clock + 1_201, endedAt: clock + 1_500 })).toBe(false)
  })

  it('closes the playback window on output failure', () => {
    startPlayback()
    utterance.onerror?.(new Event('error') as SpeechSynthesisErrorEvent)
    vi.setSystemTime(clock + 1_500)
    expect(isLikelyLocalSpeechEcho(spoken, { startedAt: clock + 1_001, endedAt: clock + 1_500 })).toBe(false)
  })

  it('retains speech that began before playback and unknown or invalid timing', () => {
    startPlayback()
    for (const interval of [
      { startedAt: clock - 100, endedAt: clock + 1_000 },
      { startedAt: clock + 2_000, endedAt: clock + 1_000 },
      { startedAt: NaN, endedAt: clock + 1_000 },
      { startedAt: clock, endedAt: Infinity },
    ]) expect(isLikelyLocalSpeechEcho(spoken, interval)).toBe(false)
  })

  it('retains changed mathematical symbols instead of normalizing them into a match', () => {
    const explanation = '方程a²+b²=5里每一项的次数是看未知数的指数，请再说说你的答案。'
    startPlayback(explanation)
    expect(isLikelyLocalSpeechEcho(explanation.replace('a²+b²', 'a³-b²'), duringPlayback())).toBe(false)
  })

  it.each([
    ['3.5', '35'], ['a(b+c)', 'ab+c'], ['A', 'a'],
  ])('retains an answer changing %s to %s', (reference, answer) => {
    const explanation = `我认为这个题目的答案应该写成${reference}，因为这里需要先计算再检查。`
    startPlayback(explanation)
    expect(isLikelyLocalSpeechEcho(explanation.replace(reference, answer), duringPlayback())).toBe(false)
  })

  it('keeps playback audio out of ASR and delivers new speech after the acoustic tail', async () => {
    const diagnostic = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('desktop', { recordRendererDiagnostic: diagnostic })
    const processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as ((event: AudioProcessingEvent) => void) | null }
    class FakeAudioContext {
      sampleRate = 16_000
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createScriptProcessor = () => processor
      close = vi.fn().mockResolvedValue(undefined)
    }
    vi.stubGlobal('AudioContext', FakeAudioContext)
    const track = { stop: vi.fn(), getSettings: () => ({}), getCapabilities: () => ({}) }
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] }),
      enumerateDevices: vi.fn().mockResolvedValue([]),
    } })
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'echo-test', status: 'active', expires_at: 'later' })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'echo-test', status: 'released' })
    const pendingAsr: Array<(result: Awaited<ReturnType<typeof localApi.transcribe>>) => void> = []
    const transcribe = vi.spyOn(localApi, 'transcribe').mockImplementation(() => new Promise((resolve) => pendingAsr.push(resolve)))
    const onTranscript = vi.fn()
    render(<HandsFreeVoiceInput sessionId="echo-test" purpose="agent_command" onTranscript={onTranscript} />)
    fireEvent.click(screen.getByRole('button', { name: '免按键对话' }))
    await vi.waitFor(async () => {
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(screen.getByRole('button', { name: '正在听 · 连续对话' })).toBeEnabled()
    })

    const capture = async () => {
      for (let index = 0; index < 4; index += 1) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(256)
          processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => new Float32Array(4096).fill(0.05) } } as unknown as AudioProcessingEvent)
        })
      }
      await act(async () => { await vi.advanceTimersByTimeAsync(1_200) })
    }
    speakLocally(spoken)
    utterance.onstart?.(new Event('start') as SpeechSynthesisEvent)
    for (let index = 0; index < 4; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(256)
        processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => new Float32Array(4096).fill(0.05) } } as unknown as AudioProcessingEvent)
      })
    }
    await act(async () => { await vi.advanceTimersByTimeAsync(13_000) })
    expect(transcribe).not.toHaveBeenCalled()
    await act(async () => {
      utterance.onend?.(new Event('end') as SpeechSynthesisEvent)
      await Promise.resolve()
      await vi.advanceTimersByTimeAsync(companionPlaybackCaptureCooldownMs + 1)
    })
    await capture()
    expect(transcribe).toHaveBeenCalledTimes(1)
    await act(async () => {
      pendingAsr[0]({ transcript: spoken, status: 'completed', cloud_used: true } as Awaited<ReturnType<typeof localApi.transcribe>>)
    })
    expect(onTranscript).toHaveBeenCalledTimes(1)
    expect(onTranscript).toHaveBeenLastCalledWith(spoken, undefined, undefined, undefined, undefined, null)
    expect(diagnostic).toHaveBeenCalledWith(expect.objectContaining({ event: 'voice.capture.playback_gate_started' }))
    expect(diagnostic).toHaveBeenCalledWith(expect.objectContaining({ event: 'voice.capture.playback_gate_ended' }))
    expect(diagnostic.mock.calls.filter(([entry]) => entry.event === 'voice.asr.playback_echo_discarded')).toHaveLength(0)
  })
})

describe('voice input mode arbitration', () => {
  afterEach(() => {
    cleanup()
    resetVoiceCaptureCoordinatorForTests()
    vi.restoreAllMocks()
    vi.useRealTimers()
    if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
    else Reflect.deleteProperty(navigator, 'mediaDevices')
    localStorage.clear()
  })

  it('drops isolated ASR filler without swallowing short meaningful replies', () => {
    expect(isLowInformationVoiceTranscript('啊。')).toBe(true)
    expect(isLowInformationVoiceTranscript('嗯嗯')).toBe(true)
    expect(isLowInformationVoiceTranscript('哎')).toBe(true)
    expect(isLowInformationVoiceTranscript('好')).toBe(false)
    expect(isLowInformationVoiceTranscript('对')).toBe(false)
    expect(isLowInformationVoiceTranscript('我不会')).toBe(false)
  })

  it('keeps nonempty dialogue markers for contextual understanding but not in wake standby', () => {
    for (const transcript of ['嗯', '嗯嗯', '哦？', '啊', '哎']) {
      expect(isLowInformationVoiceTranscript(transcript, true)).toBe(false)
      expect(isLowInformationVoiceTranscript(transcript, false)).toBe(true)
    }
    for (const transcript of ['', '  ', '，。！？', '...']) {
      expect(isLowInformationVoiceTranscript(transcript, true)).toBe(true)
    }
  })

  it('uses non-persistent ASR until the wake word opens a conversation', () => {
    expect(shouldUseStandbyTranscription('小知', false)).toBe(true)
    expect(shouldUseStandbyTranscription('小知', true)).toBe(false)
    expect(shouldUseStandbyTranscription(undefined, false)).toBe(false)
  })

  it('ends child phrases promptly and bounds a noisy continuous segment without dropping it', () => {
    expect(childUtteranceCaptureTiming(false)).toEqual({ maximumMs: 10_000, silenceMs: 800, maximumUtteranceMs: 6_000 })
    expect(childUtteranceCaptureTiming(true)).toEqual({ maximumMs: 12_000, silenceMs: 1_050, maximumUtteranceMs: 8_000 })
    const timing = childUtteranceCaptureTiming(true)
    expect(shouldFinishCapturedUtterance(7_900, 0, 1_000, 7_850, timing)).toBe(false)
    expect(shouldFinishCapturedUtterance(9_050, 0, 1_000, 9_000, timing)).toBe(true)
    expect(shouldFinishCapturedUtterance(5_000, 0, 1_000, 3_900, timing)).toBe(true)
    expect(shouldFinishCapturedUtterance(12_100, 0, 11_000, 12_000, timing)).toBe(false)
    expect(shouldFinishCapturedUtterance(12_100, 0, 0, 0, timing)).toBe(true)
    expect(shouldFinishCapturedUtterance(19_000, 0, 11_000, 18_900, timing)).toBe(true)
  })

  it('sends quiet but sustained far-field speech to cloud ASR', () => {
    const quietSpeech = wav(210, 250)
    const view = new DataView(quietSpeech.buffer)
    for (let index = 0; index < 3_200; index += 1) view.setInt16(44 + index * 2, 0, true)
    for (let index = 12_800; index < 16_000; index += 1) view.setInt16(44 + index * 2, 0, true)
    const advice = voiceCaptureSignalAdvice(quietSpeech)
    expect(advice.allowTranscription).toBe(true)
  })

  it('does not apply a second energy gate after streaming VAD detects an utterance', () => {
    const intermittentFarFieldSpeech = wav(210, 90)
    const view = new DataView(intermittentFarFieldSpeech.buffer)
    for (let index = 0; index < 16_000; index += 1) {
      if (index % 1_600 > 240) view.setInt16(44 + index * 2, 0, true)
    }

    expect(voiceCaptureSignalAdvice(intermittentFarFieldSpeech).allowTranscription).toBe(false)
    expect(handsFreeVoiceCaptureAdvice(intermittentFarFieldSpeech).allowTranscription).toBe(true)
  })

  it('treats non-speech ASR results as normal standby input instead of microphone failures', () => {
    expect(isExpectedNonSpeechAsrError(new ApiError('environment sound', 503, 'ASR_NO_HUMAN_SPEECH'))).toBe(true)
    expect(isExpectedNonSpeechAsrError(new ApiError('filler', 503, 'ASR_NO_MEANINGFUL_SPEECH'))).toBe(true)
    expect(isExpectedNonSpeechAsrError(new ApiError('service offline', 503, 'LOCAL_SERVICE_OFFLINE'))).toBe(false)
  })

  it('bounds microphone reconnect backoff and never retries an explicit permission denial', () => {
    expect([1, 2, 3, 6, 20].map(microphoneRecoveryDelayMs)).toEqual([1_000, 2_000, 4_000, 30_000, 30_000])
    expect(shouldAutomaticallyRecoverMicrophone(new DOMException('gone', 'NotFoundError'))).toBe(true)
    expect(shouldAutomaticallyRecoverMicrophone(new DOMException('denied', 'NotAllowedError'))).toBe(false)
  })

  it('disables manual capture while hands-free capture owns the microphone', async () => {
    claimVoiceCapture('external-hands-free')
    render(<VoiceInputButton sessionId="session-1" purpose="agent_command" onTranscript={() => undefined} />)

    const button = screen.getByRole('button', { name: '开始说话' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', '免按键对话正在使用麦克风')
    releaseVoiceCapture('external-hands-free')
    await waitFor(() => expect(button).toBeEnabled())
  })

  it('disables hands-free capture while manual capture owns the microphone', async () => {
    claimVoiceCapture('external-manual')
    render(<HandsFreeVoiceInput sessionId="session-1" purpose="agent_command" onTranscript={() => undefined} />)

    const button = screen.getByRole('button', { name: '免按键对话' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', '单次录音正在使用麦克风')
    releaseVoiceCapture('external-manual')
    await waitFor(() => expect(button).toBeEnabled())
  })

  it('cancels microphone startup and stops a stream that arrives after cancellation', async () => {
    const deferred: { resolve: ((stream: MediaStream) => void) | null } = { resolve: null }
    const getUserMedia = vi.fn(() => new Promise<MediaStream>((resolve) => { deferred.resolve = resolve }))
    const stop = vi.fn()
    const stream = { getTracks: () => [{ stop }] } as unknown as MediaStream
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } })
    render(<VoiceInputButton sessionId="session-1" purpose="agent_command" onTranscript={() => undefined} />)

    const button = screen.getByRole('button', { name: '开始说话' })
    fireEvent.click(button)
    fireEvent.click(await screen.findByRole('button', { name: '取消打开麦克风' }))
    if (deferred.resolve) deferred.resolve(stream)

    expect(getUserMedia).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: '开始说话' })).toBeEnabled()
  })

  it('stops a late stream when the child context changes during startup', async () => {
    const deferred: { resolve: ((stream: MediaStream) => void) | null } = { resolve: null }
    const stop = vi.fn()
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn(() => new Promise<MediaStream>((resolve) => { deferred.resolve = resolve })) } })
    const view = render(<VoiceInputButton sessionId="session-1" purpose="agent_command" childId="child-a" onTranscript={() => undefined} />)
    fireEvent.click(screen.getByRole('button', { name: '开始说话' }))
    view.rerender(<VoiceInputButton sessionId="session-1" purpose="agent_command" childId="child-b" onTranscript={() => undefined} />)
    if (deferred.resolve) deferred.resolve({ getTracks: () => [{ stop }] } as unknown as MediaStream)
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1))
  })

  it('releases a lease exactly once when registration returns after manual cancellation', async () => {
    const deferredStream: { resolve: ((stream: MediaStream) => void) | null } = { resolve: null }
    const deferredLease: { resolve: ((value: { lease_id: string; status: string; expires_at: string }) => void) | null } = { resolve: null }
    const stop = vi.fn()
    const track = { stop, getSettings: () => ({ deviceId: 'mic-1' }), getCapabilities: () => ({}) } as unknown as MediaStreamTrack
    const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream
    class FakeAudioContext {
      sampleRate = 16_000; currentTime = 0
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createScriptProcessor = () => ({ connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null })
      close = vi.fn().mockResolvedValue(undefined)
    }
    vi.stubGlobal('AudioContext', FakeAudioContext)
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: vi.fn(() => new Promise<MediaStream>((resolve) => { deferredStream.resolve = resolve })), enumerateDevices: vi.fn().mockResolvedValue([]),
    } })
    const register = vi.spyOn(localApi, 'registerDevice').mockImplementation(() => new Promise((resolve) => { deferredLease.resolve = resolve }))
    const release = vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'late-lease', status: 'released' })
    render(<VoiceInputButton sessionId="session-1" purpose="agent_command" onTranscript={() => undefined} />)
    fireEvent.click(screen.getByRole('button', { name: '开始说话' }))
    deferredStream.resolve?.(stream)
    await waitFor(() => expect(register).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: '取消打开麦克风' }))
    deferredLease.resolve?.({ lease_id: 'late-lease', status: 'active', expires_at: 'later' })
    await waitFor(() => expect(release).toHaveBeenCalledWith('late-lease', 'voice_open_cancelled_after_registration'))
    expect(release).toHaveBeenCalledTimes(1)
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it.each(['unmount', 'privacy', 'session', 'child'] as const)('cancels manual ASR and rejects a late transcript after %s', async (change) => {
    const processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as ((event: AudioProcessingEvent) => void) | null }
    vi.stubGlobal('AudioContext', class {
      sampleRate = 16_000
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createScriptProcessor = () => processor
      close = vi.fn().mockResolvedValue(undefined)
    })
    const track = { stop: vi.fn(), getSettings: () => ({}), getCapabilities: () => ({}) }
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] }),
      enumerateDevices: vi.fn().mockResolvedValue([]),
    } })
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'manual-late', status: 'active', expires_at: 'later' })
    const release = vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'manual-late', status: 'released' })
    let resolve!: (result: Awaited<ReturnType<typeof localApi.transcribe>>) => void
    const transcribe = vi.spyOn(localApi, 'transcribe').mockImplementation(() => new Promise((done) => { resolve = done }))
    const onTranscript = vi.fn()
    const view = render(<VoiceInputButton sessionId="session-a" childId="child-a" purpose="agent_command" onTranscript={onTranscript} />)
    fireEvent.click(screen.getByRole('button', { name: '开始说话' }))
    const stop = await screen.findByRole('button', { name: '结束并识别' })
    for (let index = 0; index < 4; index += 1) processor.onaudioprocess?.({
      inputBuffer: { getChannelData: () => new Float32Array(4096).fill(0.05) },
    } as unknown as AudioProcessingEvent)
    fireEvent.click(stop)
    await waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1))
    const signal = transcribe.mock.calls[0][3]!
    expect(signal.aborted).toBe(false)
    if (change === 'unmount') view.unmount()
    else if (change === 'privacy') act(() => { window.dispatchEvent(new Event('hsc-privacy-pause')) })
    else view.rerender(<VoiceInputButton sessionId={change === 'session' ? 'session-b' : 'session-a'} childId={change === 'child' ? 'child-b' : 'child-a'} purpose="agent_command" onTranscript={onTranscript} />)
    expect(signal.aborted).toBe(true)
    await act(async () => { resolve({ transcript: '这是之前孩子的回答', status: 'completed', cloud_used: true } as Awaited<ReturnType<typeof localApi.transcribe>>) })
    expect(onTranscript).not.toHaveBeenCalled()
    expect(release).toHaveBeenCalledTimes(1)
    vi.unstubAllGlobals()
  })

  it('stops a late hands-free stream after unmount without registering a ghost capture', async () => {
    const deferred: { resolve: ((stream: MediaStream) => void) | null } = { resolve: null }
    const stop = vi.fn()
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn(() => new Promise<MediaStream>((resolve) => { deferred.resolve = resolve })) } })
    const register = vi.spyOn(localApi, 'registerDevice')
    const view = render(<HandsFreeVoiceInput sessionId="session-1" purpose="agent_command" onTranscript={() => undefined} />)
    fireEvent.click(screen.getByRole('button', { name: '免按键对话' }))
    view.unmount()
    deferred.resolve?.({ getTracks: () => [{ stop }] } as unknown as MediaStream)
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1))
    expect(register).not.toHaveBeenCalled()
  })

  it('uses the uniquely resolved bound microphone for both manual and hands-free opening', async () => {
    const getUserMedia = vi.fn(() => new Promise<MediaStream>(() => undefined))
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } })
    const manual = render(<VoiceInputButton sessionId="session-1" purpose="agent_command" audioDeviceId="bound-mic" onTranscript={() => undefined} />)
    fireEvent.click(screen.getByRole('button', { name: '开始说话' }))
    expect(getUserMedia).toHaveBeenLastCalledWith(expect.objectContaining({ audio: expect.objectContaining({ deviceId: { exact: 'bound-mic' } }) }))
    fireEvent.click(screen.getByRole('button', { name: '取消打开麦克风' }))
    manual.unmount()

    render(<HandsFreeVoiceInput sessionId="session-1" purpose="agent_command" audioDeviceId="bound-mic" onTranscript={() => undefined} />)
    fireEvent.click(screen.getByRole('button', { name: '免按键对话' }))
    expect(getUserMedia).toHaveBeenLastCalledWith(expect.objectContaining({ audio: expect.objectContaining({ deviceId: { exact: 'bound-mic' } }) }))
    fireEvent.click(screen.getByRole('button', { name: '取消打开麦克风' }))
  })

  it('falls back to the computer default microphone when a saved hands-free device disappeared', async () => {
    const stop = vi.fn()
    const track = { stop, getSettings: () => ({ deviceId: 'default-mic' }), getCapabilities: () => ({}) } as unknown as MediaStreamTrack
    const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream
    const getUserMedia = vi.fn()
      .mockRejectedValueOnce(new DOMException('gone', 'NotFoundError'))
      .mockResolvedValueOnce(stream)
    vi.stubGlobal('AudioContext', class {
      sampleRate = 16_000
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createScriptProcessor = () => ({ connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null })
      close = vi.fn().mockResolvedValue(undefined)
    })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia,
      enumerateDevices: vi.fn().mockResolvedValue([{ kind: 'audioinput', deviceId: 'default-mic', groupId: 'desk', label: 'Computer Microphone' }]),
    } })
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'default-fallback', status: 'active', expires_at: 'later' })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'default-fallback', status: 'released' })

    render(<HandsFreeVoiceInput sessionId="session-1" purpose="agent_command" audioDeviceId="missing-bound-mic" onTranscript={() => undefined} />)
    fireEvent.click(screen.getByRole('button', { name: '免按键对话' }))

    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(2))
    expect(getUserMedia).toHaveBeenNthCalledWith(1, expect.objectContaining({ audio: expect.objectContaining({ deviceId: { exact: 'missing-bound-mic' } }) }))
    expect(getUserMedia).toHaveBeenNthCalledWith(2, expect.objectContaining({ audio: expect.not.objectContaining({ deviceId: expect.anything() }) }))
    expect(await screen.findByRole('button', { name: '正在听 · 连续对话' })).toBeEnabled()
  })

  it('automatically enters local wake-word standby after initialization', async () => {
    const stop = vi.fn()
    const track = { stop, getSettings: () => ({ deviceId: 'wake-mic' }), getCapabilities: () => ({}) } as unknown as MediaStreamTrack
    const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream
    class FakeAudioContext {
      sampleRate = 16_000; currentTime = 0
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createScriptProcessor = () => ({ connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null })
      close = vi.fn().mockResolvedValue(undefined)
    }
    vi.stubGlobal('AudioContext', FakeAudioContext)
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValue(stream), enumerateDevices: vi.fn().mockResolvedValue([]),
    } })
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'wake-lease', status: 'active', expires_at: 'later' })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'wake-lease', status: 'released' })

    render(<HandsFreeVoiceInput sessionId="session-1" purpose="agent_command" wakeWord="小知" autoStart onTranscript={() => undefined} />)

    expect(await screen.findByRole('button', { name: '正在听唤醒词 · 小知' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '正在听唤醒词 · 小知' }))
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1))
  })

  it('preserves an active conversation on automatic original-audio handoff and does not turn unmount into standby', async () => {
    const stop = vi.fn(), changed = vi.fn()
    const track = { stop, getSettings: () => ({ deviceId: 'handoff-mic' }), getCapabilities: () => ({}) } as unknown as MediaStreamTrack
    const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream
    vi.stubGlobal('AudioContext', class {
      sampleRate = 16_000; currentTime = 0
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createScriptProcessor = () => ({ connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null })
      close = vi.fn().mockResolvedValue(undefined)
    })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValue(stream), enumerateDevices: vi.fn().mockResolvedValue([]),
    } })
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'handoff-lease', status: 'active', expires_at: 'later' })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'handoff-lease', status: 'released' })
    const view = render(<HandsFreeVoiceInput sessionId="session-1" purpose="agent_command" wakeWord="小知" autoStart
      initialConversationActive onConversationActiveChange={changed} onTranscript={() => undefined} />)
    expect(await screen.findByRole('button', { name: '正在听 · 连续对话' })).toBeEnabled()
    expect(changed).toHaveBeenLastCalledWith(true)
    view.unmount()
    expect(changed).not.toHaveBeenCalledWith(false)
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1))
  })

  it('automatically captures actual synthetic PCM with its onset prompt and dispatches the WAV receipt without clicking', async () => {
    vi.useFakeTimers()
    const stop = vi.fn(), onTranscript = vi.fn(), status = vi.fn()
    const track = { stop, getSettings: () => ({ deviceId: 'original-mic' }), getCapabilities: () => ({}) } as unknown as MediaStreamTrack
    const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream
    const processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as null | ((event: AudioProcessingEvent) => void) }
    vi.stubGlobal('AudioContext', class {
      sampleRate = 16_000; currentTime = 0
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createScriptProcessor = () => processor
      close = vi.fn().mockResolvedValue(undefined)
    })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: vi.fn().mockResolvedValue(stream), enumerateDevices: vi.fn().mockResolvedValue([]),
    } })
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'original-lease', status: 'active', expires_at: 'later' })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'original-lease', status: 'released' })
    const question = (id: string) => ({ status: 'waiting_answer', reason: null, prompt: { prompt_id: id, item_version: 'v1',
      child_id: 'child-a', origin_session_id: 'original-session',
      item: { track: 'pet', answer_type: 'speaking', assessment_mode: 'pet_reference_rubric' } } }) as DiagnosticPromptResult
    const transcribe = vi.spyOn(localApi, 'transcribe').mockImplementation(async () => ({ status: 'completed', cloud_used: true,
      voice_input_id: 'pcm-original-id', transcript: 'I enjoy reading.', capture_binding: { status: 'completed',
        prompt_id: 'onset-prompt', item_version: 'v1', raw_audio_available: true } } as never))
    await trackDiagnosticCapture('original-session', async () => question('onset-prompt'))
    const view = render(<HandsFreeVoiceInput sessionId="original-session" childId="child-a" purpose="agent_command"
      wakeWord="小知" autoStart initialConversationActive onTranscript={onTranscript} onRuntimeStatusChange={status} />)
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(processor.onaudioprocess).toEqual(expect.any(Function))
      expect(status.mock.calls.some(([value]) => value.captureReady === true)).toBe(false)
      const chunk = Float32Array.from({ length: 4096 }, (_, i) => 0.15 * Math.sin((i + 1) * 2 * Math.PI * 180 / 16000))
      await act(async () => {
        processor.onaudioprocess!({ inputBuffer: { getChannelData: () => chunk } } as unknown as AudioProcessingEvent)
        expect(status.mock.lastCall?.[0]).toMatchObject({ capturePending: true, speechActive: false, captureReady: true })
        // A later UI prompt cannot relabel the already-started original.
        await trackDiagnosticCapture('original-session', async () => question('newer-prompt'))
        for (let i = 0; i < 7; i += 1) {
          await vi.advanceTimersByTimeAsync(256)
          processor.onaudioprocess!({ inputBuffer: { getChannelData: () => chunk } } as unknown as AudioProcessingEvent)
        }
        await vi.advanceTimersByTimeAsync(1200)
      })
      expect(transcribe).toHaveBeenCalledTimes(1)
      const [session, purpose, encoded, , , reference] = transcribe.mock.calls[0]
      expect([session, purpose]).toEqual(['original-session', 'agent_command'])
      expect(reference?.question_reference).toEqual({ kind: 'diagnostic', binding: { prompt_id: 'onset-prompt', item_version: 'v1' } })
      const bytes = Uint8Array.from(atob(encoded), character => character.charCodeAt(0)), header = new DataView(bytes.buffer)
      expect(bytes.length).toBe(44 + 8 * 4096 * 2)
      expect(header.getUint32(24, true)).toBe(16000)
      expect(header.getInt16(44, true)).toBeGreaterThan(0)
      expect(onTranscript).toHaveBeenCalledWith('I enjoy reading.', undefined, 'pcm-original-id', undefined, undefined, null)
    } finally {
      view.unmount()
      expect(status.mock.lastCall?.[0]).toMatchObject({ captureReady: false })
      clearVoiceCaptureContext('original-session')
    }
  })

  function segmentedPressFixture() {
    vi.useFakeTimers()
    const track = { stop: vi.fn(), getSettings: () => ({ deviceId: 'space-mic' }), getCapabilities: () => ({}) } as unknown as MediaStreamTrack
    const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream
    const processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as null | ((event: AudioProcessingEvent) => void) }
    vi.stubGlobal('AudioContext', class {
      sampleRate = 16_000; currentTime = 0
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createScriptProcessor = () => processor
      close = vi.fn().mockResolvedValue(undefined)
    })
    const getUserMedia = vi.fn().mockResolvedValue(stream)
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia, enumerateDevices: vi.fn().mockResolvedValue([]) } })
    vi.spyOn(localApi, 'registerDevice').mockResolvedValue({ lease_id: 'space-mic', status: 'active', expires_at: 'later' })
    vi.spyOn(localApi, 'releaseDeviceLease').mockResolvedValue({ lease_id: 'space-mic', status: 'released' })
    const transcribe = vi.spyOn(localApi, 'transcribe').mockResolvedValue({ status: 'completed', cloud_used: true,
      voice_input_id: 'space-voice', transcript: '我想继续解答这道题。' } as never)
    const onTranscript = vi.fn()
    const speak = async (count: number, leadingSign = 1): Promise<void> => {
      const samples = Float32Array.from({ length: 4096 }, (_, index) => leadingSign * 0.15 * Math.sin((index + 1) * 2 * Math.PI * 180 / 16000))
      for (let index = 0; index < count; index++) {
        processor.onaudioprocess!({ inputBuffer: { getChannelData: () => samples } } as unknown as AudioProcessingEvent)
        await vi.advanceTimersByTimeAsync(256)
      }
    }
    const space = (event: 'keyDown' | 'keyUp', repeat = false): void => { fireEvent[event](window, { code: 'Space', key: ' ', repeat }) }
    return { onTranscript, transcribe, getUserMedia, speak, space }
  }

  it('keeps segmented manual input private while idle and holds through a pause until a single Space release', async () => {
    const f = segmentedPressFixture()
    render(<HandsFreeVoiceInput sessionId="space-segmented" childId="child-a" purpose="agent_command" autoStart inputMode="manual" onTranscript={f.onTranscript} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(1); await f.speak(4); await vi.advanceTimersByTimeAsync(1_500) })
    expect(f.transcribe).not.toHaveBeenCalled()
    expect(f.getUserMedia).toHaveBeenCalledOnce()
    f.space('keyDown')
    await act(async () => { await f.speak(4); await vi.advanceTimersByTimeAsync(1_500) })
    f.space('keyDown', true)
    expect(screen.getByRole('status')).toHaveTextContent('松开空格发送')
    expect(f.transcribe).not.toHaveBeenCalled()
    await act(async () => { await f.speak(4); f.space('keyUp'); f.space('keyUp'); await vi.advanceTimersByTimeAsync(200) })
    expect(f.transcribe).toHaveBeenCalledOnce()
    expect(f.onTranscript).toHaveBeenCalledOnce()
    expect(atob(f.transcribe.mock.calls[0][2])).toHaveLength(44 + 8 * 4096 * 2)
  })

  it('cancels segmented Space capture when the question changes or the window loses focus', async () => {
    const f = segmentedPressFixture()
    const scope = 'space-cancel'
    const question = (prompt_id: string) => ({ status: 'waiting_answer', reason: null, prompt: { prompt_id, item_version: 'v1',
      child_id: 'child-a', origin_session_id: scope, item: { track: 'pet', answer_type: 'speaking', assessment_mode: 'pet_reference_rubric' } } }) as DiagnosticPromptResult
    await trackDiagnosticCapture(scope, async () => question('first'))
    const view = render(<HandsFreeVoiceInput sessionId={scope} childId="child-a" purpose="agent_command" autoStart inputMode="manual" onTranscript={f.onTranscript} />)
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      f.space('keyDown')
      await act(async () => { await f.speak(4); await trackDiagnosticCapture(scope, async () => question('replacement')); f.space('keyUp'); await vi.advanceTimersByTimeAsync(200) })
      expect(f.transcribe).not.toHaveBeenCalled()
      f.space('keyDown')
      await act(async () => { await f.speak(4); fireEvent.blur(window); f.space('keyUp'); await vi.advanceTimersByTimeAsync(200) })
      expect(f.transcribe).not.toHaveBeenCalled()
    } finally { view.unmount(); clearVoiceCaptureContext(scope) }
  })

  it('does not submit segmented Space audio to a different visible mistake coaching question', async () => {
    const f = segmentedPressFixture()
    const sessionId = 'space-mistake-change'
    const ownerId = 'space-mistake-owner'
    const question = 'a'.repeat(64)
    const publishMistake = (id: string, generation: number): void => {
      window.dispatchEvent(new CustomEvent('hsc-mistake-learning-surface', { detail: {
        owner_id: ownerId, child_id: 'child-a', session_id: sessionId, mistake_id: id,
        surface: { child_id: 'child-a', session_id: sessionId, mistake_id: id, question_visible: true,
          focus_generation: generation, learning_session_id: 'coaching', source_revision: question, practice_item_id: null },
      } }))
    }
    render(<><div data-mistake-learning-owner={ownerId} data-mistake-learning-id="first" data-mistake-learning-session={sessionId}>
      <p data-mistake-learning-question="original">第一道错题</p>
    </div><HandsFreeVoiceInput sessionId={sessionId} childId="child-a" purpose="agent_command" autoStart inputMode="manual" onTranscript={f.onTranscript} /></>)
    const original = screen.getByText('第一道错题')
    for (const element of [original, original.parentElement!]) {
      vi.spyOn(element, 'getBoundingClientRect').mockReturnValue({ width: 120, height: 30 } as DOMRect)
    }
    try {
      publishMistake('first', 1)
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      f.space('keyDown')
      await act(async () => { await f.speak(4); publishMistake('first', 2); f.space('keyUp'); await vi.advanceTimersByTimeAsync(200) })
      expect(f.transcribe).not.toHaveBeenCalled()
      expect(screen.getByRole('status')).toHaveTextContent('请在当前页面再说一次')
    } finally {
      window.dispatchEvent(new CustomEvent('hsc-mistake-learning-surface', { detail: {
        owner_id: ownerId, child_id: 'child-a', session_id: sessionId, mistake_id: 'first', surface: null,
      } }))
    }
  })

  it('cancels segmented Space when the learning page is hidden, even if it returns before keyup', async () => {
    const f = segmentedPressFixture()
    const onKeyboardCaptureStart = vi.fn()
    const input = (visibleLearningSource: boolean, collapsed: boolean) => <div data-companion-collapsed={collapsed}>
      <HandsFreeVoiceInput sessionId="segmented-route" childId="child-a" purpose="agent_command" autoStart inputMode="manual"
        visibleLearningSource={visibleLearningSource} onKeyboardCaptureStart={onKeyboardCaptureStart} onTranscript={f.onTranscript} />
    </div>
    const view = render(input(true, true))
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    f.space('keyDown')
    expect(onKeyboardCaptureStart).toHaveBeenCalledOnce()
    view.rerender(input(true, false))
    await act(async () => { await f.speak(4) })
    expect(screen.getByRole('status')).toHaveTextContent('松开空格发送')

    view.rerender(input(false, false))
    expect(screen.getByRole('status')).toHaveTextContent('请在当前页面再说一次')
    view.rerender(input(true, false))
    f.space('keyUp')
    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    expect(f.transcribe).not.toHaveBeenCalled()

    f.space('keyDown')
    await act(async () => { await f.speak(4); f.space('keyUp'); await vi.advanceTimersByTimeAsync(200) })
    expect(f.transcribe).toHaveBeenCalledOnce()
    expect(f.getUserMedia).toHaveBeenCalledOnce()
  })

  it('uses one segmented recorder for screen dictation and warns on release before capture is ready', async () => {
    const f = segmentedPressFixture()
    render(<HandsFreeVoiceInput sessionId="screen-segmented" childId="child-a" purpose="agent_command" autoStart inputMode="manual" onTranscript={f.onTranscript} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    f.space('keyDown')
    f.space('keyUp')
    expect(screen.getByRole('status')).toHaveTextContent('麦克风尚未准备好')
    expect(f.transcribe).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '开始说话' }))
    await act(async () => { await f.speak(4); await vi.advanceTimersByTimeAsync(1_500); await f.speak(4) })
    expect(f.transcribe).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '结束并发送' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    expect(f.transcribe).toHaveBeenCalledOnce()
    expect(f.getUserMedia).toHaveBeenCalledOnce()
  })

  it('drops a partial automatic capture on Space takeover and resumes hands-free after release', async () => {
    const f = segmentedPressFixture()
    render(<HandsFreeVoiceInput sessionId="space-takeover" childId="child-a" purpose="agent_command" autoStart inputMode="hands-free" onTranscript={f.onTranscript} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(1); await f.speak(4, -1) })
    f.space('keyDown')
    await act(async () => { await f.speak(4); f.space('keyUp'); await vi.advanceTimersByTimeAsync(200) })
    expect(f.transcribe).toHaveBeenCalledOnce()
    const bytes = Uint8Array.from(atob(f.transcribe.mock.calls[0][2]), char => char.charCodeAt(0))
    expect(bytes).toHaveLength(44 + 4 * 4096 * 2)
    expect(new DataView(bytes.buffer).getInt16(44, true)).toBeGreaterThan(0)
    await act(async () => { await f.speak(4); await vi.advanceTimersByTimeAsync(1_500) })
    expect(f.transcribe).toHaveBeenCalledTimes(2)
    expect(f.getUserMedia).toHaveBeenCalledOnce()
  })

  it('automatically reopens hands-free listening after a transient microphone failure', async () => {
    vi.useFakeTimers()
    const incident = vi.spyOn(localApi, 'recordDeviceIncident').mockResolvedValue({} as never)
    const getUserMedia = vi.fn()
      .mockRejectedValueOnce(new DOMException('gone', 'NotFoundError'))
      .mockImplementationOnce(() => new Promise<MediaStream>(() => undefined))
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } })

    render(<HandsFreeVoiceInput sessionId="session-1" purpose="agent_command" wakeWord="小知" autoStart onTranscript={() => undefined} />)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/未找到麦克风/)).toBeInTheDocument()
    expect(screen.queryByText(/gone/)).toBeNull()
    expect(incident).toHaveBeenCalledOnce()
    await act(async () => { vi.advanceTimersByTime(1_000); await Promise.resolve(); await Promise.resolve() })
    expect(getUserMedia).toHaveBeenCalledTimes(2)
    expect(incident).toHaveBeenCalledOnce()
  })

  it('automatically restores household wake standby after a transient microphone failure', async () => {
    vi.useFakeTimers()
    const getUserMedia = vi.fn()
      .mockRejectedValueOnce(new DOMException('gone', 'NotFoundError'))
      .mockImplementationOnce(() => new Promise<MediaStream>(() => undefined))
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia,
      enumerateDevices: vi.fn().mockResolvedValue([{ kind: 'audioinput', deviceId: 'only-mic', groupId: 'desk', label: '高拍仪麦克风' }]),
    } })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([])

    render(<HouseholdStandbyVoiceInput profiles={[{ childId: 'child-a', wakeWord: '小知', configured: true, handsFree: true }]} onWake={() => undefined} />)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/未找到麦克风/)).toBeInTheDocument()
    expect(screen.queryByText(/gone/)).toBeNull()
    await act(async () => { vi.advanceTimersByTime(1_000); await Promise.resolve(); await Promise.resolve() })
    expect(getUserMedia).toHaveBeenCalledTimes(2)
  })

  it.each(['hands-free', 'household'])('explains denied microphone permission without retrying in %s mode', async (mode) => {
    vi.useFakeTimers()
    const onRuntimeStatusChange = vi.fn()
    const getUserMedia = vi.fn().mockRejectedValue(new DOMException('Permission denied by system', 'NotAllowedError'))
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia,
      enumerateDevices: vi.fn().mockResolvedValue([{ kind: 'audioinput', deviceId: 'only-mic', groupId: 'desk', label: '高拍仪麦克风' }]),
    } })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([])
    render(mode === 'hands-free'
      ? <HandsFreeVoiceInput sessionId="session-1" purpose="agent_command" wakeWord="小知" autoStart onTranscript={() => undefined} onRuntimeStatusChange={onRuntimeStatusChange} />
      : <HouseholdStandbyVoiceInput profiles={[{ childId: 'child-a', wakeWord: '小知', configured: true, handsFree: true }]} onWake={() => undefined} />)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/麦克风权限被拒绝/)).toBeInTheDocument()
    expect(screen.queryByText(/Permission denied by system|正在自动重连/)).toBeNull()
    if (mode === 'hands-free') expect(onRuntimeStatusChange.mock.lastCall?.[0]).toMatchObject({ phase: 'error', speechActive: false })
    await act(async () => { vi.advanceTimersByTime(31_000); await Promise.resolve() })
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    if (mode === 'hands-free') expect(onRuntimeStatusChange.mock.lastCall?.[0]).toMatchObject({ phase: 'error', speechActive: false })
  })

  it('speaks first-time identity guidance before opening the household microphone', async () => {
    const getUserMedia = vi.fn(() => new Promise<MediaStream>(() => undefined))
    const speak = vi.fn()
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), speak, getVoices: () => [{ name: 'local', lang: 'zh-CN', localService: true }] })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia,
      enumerateDevices: vi.fn().mockResolvedValue([{ kind: 'audioinput', deviceId: 'only-mic', groupId: 'desk', label: '高拍仪麦克风' }]),
    } })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([])
    render(<HouseholdStandbyVoiceInput profiles={[{ childId: 'child-a', wakeWord: '四年级同学', configured: false, entryPhrase: '四年级', handsFree: true }]} onWake={() => undefined} />)
    await waitFor(() => expect(speak).toHaveBeenCalledTimes(1))
    expect(speak.mock.calls[0][0].text).toContain('请告诉我你的年级')
    expect(speak.mock.calls[0][0].text).toContain('说“四年级”')
    expect(speak.mock.calls[0][0].text).toContain('年级会固定')
    expect(getUserMedia).not.toHaveBeenCalled()
    await act(async () => { speak.mock.calls[0][0].onend?.() })
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1))
  })

  it('announces configured wake words once before opening the household microphone', async () => {
    const getUserMedia = vi.fn(() => new Promise<MediaStream>(() => undefined))
    const speak = vi.fn()
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), speak, getVoices: () => [{ name: 'local', lang: 'zh-CN', localService: true }] })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia,
      enumerateDevices: vi.fn().mockResolvedValue([{ kind: 'audioinput', deviceId: 'only-mic', groupId: 'desk', label: '默认麦克风' }]),
    } })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([])
    render(<HouseholdStandbyVoiceInput profiles={[
      { childId: 'child-a', wakeWord: '小知', configured: true, handsFree: true },
      { childId: 'child-b', wakeWord: '小语', configured: true, handsFree: true },
    ]} onWake={() => undefined} />)
    await waitFor(() => expect(speak).toHaveBeenCalledTimes(1))
    expect(speak.mock.calls[0][0].text).toBe('我在这里。需要我时，请说“小知”或者“小语”。')
    expect(getUserMedia).not.toHaveBeenCalled()
    await act(async () => { speak.mock.calls[0][0].onend?.() })
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1))
  })

  it('uses the Windows default microphone even when a document camera is bound', async () => {
    const getUserMedia = vi.fn(() => new Promise<MediaStream>(() => undefined))
    const speak = vi.fn()
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), speak, getVoices: () => [{ name: 'local', lang: 'zh-CN', localService: true }] })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia,
      enumerateDevices: vi.fn().mockResolvedValue([
        { kind: 'audioinput', deviceId: 'default', groupId: 'regular', label: 'Default - L523 (AC Interface) (1d6b:0102)' },
        { kind: 'audioinput', deviceId: 'laptop-mic', groupId: 'laptop', label: '麦克风阵列' },
        { kind: 'audioinput', deviceId: 'desk-mic', groupId: 'desk', label: '麦克风 (HDAudio) (eba4:1303)' },
        { kind: 'videoinput', deviceId: 'desk-camera', groupId: 'desk', label: 'Usb HDCamera (eba4:1303)' },
      ]),
    } })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([{
      device_type: 'document_camera', system_id_hash: 'rotated-browser-id', label: 'Usb HDCamera (eba4:1303)',
      capabilities: {}, verified_at: 'now', updated_at: 'now',
    }])

    render(<HouseholdStandbyVoiceInput profiles={[{ childId: 'child-a', wakeWord: '小知', configured: true, handsFree: true }]} onWake={() => undefined} />)

    await waitFor(() => expect(speak).toHaveBeenCalledOnce())
    await act(async () => { speak.mock.calls[0][0].onend?.() })
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledWith({
      audio: expect.not.objectContaining({ deviceId: expect.anything() }), video: false,
    }))
  })

  it.each([
    { transcript: '小知，帮我复习', accepted: true },
    { transcript: '小知小语', accepted: false },
  ])('uses a single explicit home capture for identity and handles ambiguity: %j', async ({ transcript, accepted }) => {
    vi.useFakeTimers()
    const processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as ((event: AudioProcessingEvent) => void) | null }
    vi.stubGlobal('AudioContext', class {
      sampleRate = 16_000
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createScriptProcessor = () => processor
      close = vi.fn().mockResolvedValue(undefined)
    })
    const track = { stop: vi.fn(), readyState: 'live', enabled: true, getSettings: () => ({}), getCapabilities: () => ({}) }
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia, enumerateDevices: vi.fn().mockResolvedValue([{ kind: 'audioinput', deviceId: 'default', label: '默认麦克风' }]),
    } })
    vi.spyOn(localApi, 'deviceBindings').mockResolvedValue([])
    const transcribe = vi.spyOn(localApi, 'transcribeHouseholdEntry').mockResolvedValue({
      transcript, duration_ms: 1000, model_version: 'home-test', client_action_id: 'home-speech',
      status: 'completed', persisted: false, cloud_used: false,
    })
    const onWake = vi.fn()
    const profiles = [
      { childId: 'a', wakeWord: '小知', configured: true },
      { childId: 'b', wakeWord: '小语', configured: true },
    ]
    render(<HouseholdStandbyVoiceInput profiles={profiles} onWake={onWake} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByRole('status')).toHaveTextContent('按住空格')
    expect(getUserMedia).not.toHaveBeenCalled()
    fireEvent.keyDown(window, { code: 'Space', key: ' ' })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(processor.onaudioprocess).toEqual(expect.any(Function))
    const voiced = new Float32Array(1024).map((_, index) => 0.12 * Math.sin(index * 0.1))
    const feedSpeech = (): void => { for (let index = 0; index < 12; index++)
      processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => voiced } } as unknown as AudioProcessingEvent) }
    act(feedSpeech)
    expect(screen.getByRole('status')).toHaveTextContent('正在听，松开空格发送')
    await act(async () => { await vi.advanceTimersByTimeAsync(1800) })
    expect(transcribe).not.toHaveBeenCalled()
    act(feedSpeech)
    fireEvent.keyUp(window, { code: 'Space', key: ' ' })
    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    expect(transcribe).toHaveBeenCalledTimes(1)
    expect(track.stop).toHaveBeenCalledTimes(1)
    if (accepted) expect(onWake).toHaveBeenCalledExactlyOnceWith(profiles[0], '帮我复习', undefined)
    else {
      expect(onWake).not.toHaveBeenCalled()
      expect(screen.getByRole('status')).toHaveTextContent('匹配到多个学习档案')
    }
  })

  it('keeps a freshly paused Home microphone closed for Space and saved hands-free preference', async () => {
    const enumerateDevices = vi.fn().mockResolvedValue([{ kind: 'audioinput', deviceId: 'default', label: '默认麦克风' }])
    const getUserMedia = vi.fn()
    const onWake = vi.fn()
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { enumerateDevices, getUserMedia } })
    render(<HouseholdStandbyVoiceInput enabled={false}
      profiles={[{ childId: 'a', wakeWord: '小知', configured: true, handsFree: true }]} onWake={onWake} />)
    fireEvent.keyDown(window, { code: 'Space', key: ' ' })
    fireEvent.keyUp(window, { code: 'Space', key: ' ' })
    expect(screen.getByRole('status')).toHaveTextContent('家庭麦克风已暂停')
    expect(enumerateDevices).not.toHaveBeenCalled()
    expect(getUserMedia).not.toHaveBeenCalled()
    expect(onWake).not.toHaveBeenCalled()
  })

  it('does not let a proactive activation reopen the microphone after privacy pause', async () => {
    const getUserMedia = vi.fn(() => new Promise<MediaStream>(() => undefined))
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } })
    const view = render(<HandsFreeVoiceInput sessionId="session-1" purpose="agent_command" wakeWord="小知" autoStart externalActivationSignal={0} onTranscript={() => undefined} />)
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1))
    window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))
    view.rerender(<HandsFreeVoiceInput sessionId="session-1" purpose="agent_command" wakeWord="小知" autoStart externalActivationSignal={1} onTranscript={() => undefined} />)
    await Promise.resolve()
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('uses the child voice profile for adaptation hints without blocking speech', () => {
    enrollLocalVoiceProfile('child-a', wav(220, 4_000))
    const differentVoice = wav(5_000, 25_000)
    expect(voiceProfileCaptureAdvice('child-a', differentVoice, false)).toMatchObject({ allowAutomaticSubmit: true })
    expect(voiceProfileCaptureAdvice('child-a', differentVoice, true)).toMatchObject({ allowAutomaticSubmit: true })
  })

  it.each([
    ['stop during processing', false, 4, 4, false],
    ['privacy pause during processing', false, 4, 4, true],
    ['unmount during processing', false, 4, 4, true],
    ['an old request after a new hands-free round', true, 4, 5, false],
  ])('drops a late hands-free transcript after %s', (_reason, active, expected, current, aborted) => {
    const controller = new AbortController()
    if (aborted) controller.abort()
    expect(shouldDeliverHandsFreeTranscript(active, expected, current, controller.signal)).toBe(false)
  })

  it('dispatches consecutive hands-free transcripts without waiting for the earlier agent turn', async () => {
    let firstComplete: (() => void) | undefined
    const calls: string[] = []
    const onTranscript = vi.fn((text: string) => new Promise<void>((resolve) => { calls.push(text); if (text === '第一句') firstComplete = resolve; else resolve() }))
    expect(dispatchHandsFreeTranscript(() => true, onTranscript, '第一句')).toBe(true)
    expect(dispatchHandsFreeTranscript(() => true, onTranscript, '第二句')).toBe(true)
    await Promise.resolve()
    expect(calls).toEqual(['第一句', '第二句'])
    firstComplete?.()
  })

  it('does not dispatch a transcript from a stopped hands-free round', () => {
    const onTranscript = vi.fn()
    expect(dispatchHandsFreeTranscript(() => false, onTranscript, '已停止')).toBe(false)
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it('reports a rejected agent action without blocking later transcript dispatch', async () => {
    const onError = vi.fn()
    const onTranscript = vi.fn((text: string) => text === '第一句' ? Promise.reject(new Error('agent failed')) : Promise.resolve())
    dispatchHandsFreeTranscript(() => true, onTranscript, '第一句', onError)
    dispatchHandsFreeTranscript(() => true, onTranscript, '第二句', onError)
    await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: 'agent failed' })))
    expect(onTranscript.mock.calls.map((call) => call[0])).toEqual(['第一句', '第二句'])
  })

  it('keeps queued audio valid while capture pauses for backpressure', () => {
    const controller = new AbortController()
    expect(shouldProcessQueuedHandsFreeSegment(8, 8, controller.signal)).toBe(true)
    controller.abort()
    expect(shouldProcessQueuedHandsFreeSegment(8, 8, controller.signal)).toBe(false)
  })

  it('never delivers an ignored ASR response to an agent callback', async () => {
    const onTranscript = vi.fn()
    const allowed = shouldDeliverCompletedHandsFreeTranscript('ignored', true, 7, 7, new AbortController().signal)
    if (allowed) dispatchHandsFreeTranscript(() => true, onTranscript, '取消的结果')
    expect(onTranscript).not.toHaveBeenCalled()
  })

  it('never delivers an ignored manual ASR response to an agent callback', () => {
    expect(shouldDeliverManualTranscript('ignored')).toBe(false)
    expect(shouldDeliverManualTranscript('completed')).toBe(true)
  })
})
