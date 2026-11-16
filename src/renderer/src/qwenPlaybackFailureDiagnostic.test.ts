import { describe, expect, it, vi } from 'vitest'
import { classifyQwenRealtimeError } from './QwenRealtimeVoiceInput'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'

describe('playback failure diagnostic', () => {
  it.each([
    ['NotAllowedError', 'QWEN_PLAYBACK_PLAY_NOT_ALLOWED'],
    ['NotSupportedError', 'QWEN_PLAYBACK_PLAY_NOT_SUPPORTED'],
    ['AbortError', 'QWEN_PLAYBACK_PLAY_ABORTED'],
    ['InvalidStateError', 'QWEN_PLAYBACK_PLAY_INVALID_STATE'],
    ['private details', 'QWEN_PLAYBACK_PLAY_OTHER'],
  ])('reports only the reviewed error class for play rejection %s', async (name, expected) => {
    const onError = vi.fn(), onStatus = vi.fn()
    const value = new QwenRealtimeVoiceSession({ childName: 'synthetic', callbacks: { onError, onStatus } })
    const state = value as unknown as { remoteAudio: unknown; setRemotePlaybackAuthorized: (value: boolean) => void }
    state.remoteAudio = { muted: false, play: () => Promise.reject({ name, message: 'private details must not be logged' }) }
    state.setRemotePlaybackAuthorized(true)
    await Promise.resolve(); await Promise.resolve()
    expect(onError).toHaveBeenCalledOnce()
    const message = onError.mock.calls[0][0] as string
    expect(classifyQwenRealtimeError(message)).toBe(expected)
    expect(message).not.toContain('private details')
  })
  it.each([
    ['实时语音播放观测不可用，不能确认回复已送达。', 'QWEN_PLAYBACK_OBSERVER_CREATE_FAILED'],
    ['保守收音路径缺少播放观测，正在恢复连接。', 'QWEN_PLAYBACK_OBSERVER_MISSING'],
    ['保守收音路径的播放观测不可用，正在恢复连接。', 'QWEN_PLAYBACK_OBSERVER_UNAVAILABLE'],
    ['保守收音路径无法确认播放观测健康，正在恢复连接。', 'QWEN_PLAYBACK_OBSERVER_QUIET_UNCONFIRMED'],
    ['实时语音播放器异常。', 'QWEN_PLAYBACK_ELEMENT_ERROR'],
  ])('distinguishes the fixed local failure site', (message, expected) => {
    expect(classifyQwenRealtimeError(message)).toBe(expected)
  })
})
