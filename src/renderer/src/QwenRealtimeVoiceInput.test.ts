import { describe, expect, it } from 'vitest'
import { classifyQwenRealtimeError, QWEN_REALTIME_SESSION_REFRESH_MS, QWEN_38_REALTIME_SESSION_REFRESH_MS, realtimeSessionRefreshMs, routeQwenRealtimeUtterance } from './QwenRealtimeVoiceInput'

describe('Qwen realtime wake routing', () => {

  it('classifies realtime failures without persisting provider error text', () => {
    expect(classifyQwenRealtimeError('ASR_CLIENT_TIMEOUT：本地唤醒识别未按时返回。')).toBe('ASR_CLIENT_TIMEOUT')
    expect(classifyQwenRealtimeError('ASR_CLIENT_UNKNOWN：本地唤醒识别暂时不可用。')).toBe('ASR_CLIENT_UNKNOWN')
    expect(classifyQwenRealtimeError('音频活动后30秒仍未取得完整转写。恢复后请重说。')).toBe('QWEN_REALTIME_INPUT_ACTIVITY_UNCONFIRMED')
    expect(classifyQwenRealtimeError('这句话的语音转写失败，已停止本连接并保留其他已接纳原话；连接恢复后请重说未识别的一句。')).toBe('QWEN_REALTIME_INPUT_TRANSCRIPTION_FAILED')
    expect(classifyQwenRealtimeError('实时会话的输入身份容量已满，已停止本连接；需重新建立连接。')).toBe('QWEN_REALTIME_INPUT_IDENTITY_CAPACITY')
    expect(classifyQwenRealtimeError('系统阻止了实时语音播放。')).toBe('QWEN_REALTIME_MEDIA_PLAYBACK_BLOCKED')
    expect(classifyQwenRealtimeError('实时语音数据通道异常。')).toBe('QWEN_REALTIME_DATA_CHANNEL')
    expect(classifyQwenRealtimeError('千问实时语音返回错误。')).toBe('QWEN_REALTIME_PROVIDER_ERROR')
    expect(classifyQwenRealtimeError('Could not start audio source: NotReadableError')).toBe('QWEN_REALTIME_MICROPHONE')
    expect(classifyQwenRealtimeError('Requested device not found: NotFoundError')).toBe('QWEN_REALTIME_MICROPHONE')
    expect(classifyQwenRealtimeError('Realtime session expired after maximum duration.')).toBe('QWEN_REALTIME_SESSION_EXPIRED')
    expect(QWEN_REALTIME_SESSION_REFRESH_MS).toBeLessThan(300_000)
  })
  it('keeps unrelated speech in standby and never dispatches it to the agent', () => {
    expect(routeQwenRealtimeUtterance('电视里正在说话', '小知', false)).toEqual({ kind: 'standby_ignored', woke: false })
  })

  it('enters conversation on a wake-only turn without fabricating a command', () => {
    expect(routeQwenRealtimeUtterance('小知', '小知', false)).toEqual({ kind: 'wake_only', woke: true })
  })

  it('removes the wake word and preserves the complete following command', () => {
    expect(routeQwenRealtimeUtterance('小知，我先问数学，然后连接高拍仪', '小知', false)).toEqual({
      kind: 'conversation', utterance: '我先问数学，然后连接高拍仪', woke: true,
    })
  })

  it('keeps every complete utterance after conversation mode is active', () => {
    expect(routeQwenRealtimeUtterance('我还有第二个问题', '小知', true)).toEqual({
      kind: 'conversation', utterance: '我还有第二个问题', woke: false,
    })
  })
})

it('uses a long rotation only for the actual supported model and retains the conservative fallback', () => {
  expect(realtimeSessionRefreshMs('qwen3.8-omni-flash-realtime')).toBe(QWEN_38_REALTIME_SESSION_REFRESH_MS)
  expect(QWEN_38_REALTIME_SESSION_REFRESH_MS).toBe(110 * 60_000)
  for (const model of [undefined, 'qwen3.5-omni-flash-realtime', 'qwen3.8-omni-flash-realtime-untrusted']) {
    expect(realtimeSessionRefreshMs(model)).toBe(QWEN_REALTIME_SESSION_REFRESH_MS)
  }
})
