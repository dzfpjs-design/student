import { afterEach, describe, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession, QWEN_REALTIME_TOOL_NAME } from './qwenRealtimeVoice'

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

function session(throwOnUnconfirmed = false) {
  const started = vi.fn(), ended = vi.fn(), abandoned = vi.fn(), status = vi.fn(), transcript = vi.fn(), error = vi.fn()
  const unconfirmed = vi.fn(() => { if (throwOnUnconfirmed) throw new Error('synthetic-private-receipt-error') })
  const value = new QwenRealtimeVoiceSession({ childName: 'test', requireCommittedTranscript: true,
    callbacks: { onStatus: status, onAssistantTranscript: transcript, onError: error,
      onToolCall: async () => ({ responseText: '合成回复', onPlaybackStart: started,
        onPlaybackEnd: ended, onPlaybackAbandoned: abandoned, onPlaybackUnconfirmed: unconfirmed }) } })
  const internal = Object.assign(value, { audioTrack: { enabled: true, getSettings: () => ({}) },
    microphoneReleased: true, channel: { readyState: 'open', send: vi.fn(), close: vi.fn() } }) as unknown as {
      receive: (raw: string) => void; close: () => void; playbackStarted: boolean
      markPlaybackStarted: () => void; drainingToolCalls: boolean
    }
  const emit = (event: Record<string, unknown>): void => internal.receive(JSON.stringify(event))
  const authorize = async (): Promise<void> => {
    emit({ type: 'conversation.item.input_audio_transcription.completed', transcript: '帮我解答' })
    emit({ type: 'response.created', response: { id: 'tool-response' } })
    emit({ type: 'response.function_call_arguments.done', response_id: 'tool-response', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'owned', arguments: '{}' })
    for (let i = 0; i < 8; i++) await Promise.resolve()
    emit({ type: 'response.done', response: { id: 'tool-response', status: 'completed' } })
    emit({ type: 'response.created', response: { id: 'reply-response' } })
  }
  return { internal, emit, authorize, started, ended, abandoned, unconfirmed, status, transcript, error }
}

describe('remote playback evidence ownership', () => {
  it('does not call a transcript delta or response request actual playback', async () => {
    vi.useFakeTimers()
    const probe = session()
    await probe.authorize()
    probe.emit({ type: 'response.audio_transcript.delta', response_id: 'reply-response', delta: '合成回复' })
    expect(probe.transcript).toHaveBeenCalledWith('合成回复')
    expect(probe.started).not.toHaveBeenCalled()
    expect(probe.status.mock.calls.map(([state]) => state.phase)).not.toContain('speaking')
    probe.internal.close()
  })

  it('never records delivery when no fresh media was observed', async () => {
    vi.useFakeTimers()
    const probe = session()
    await probe.authorize()
    probe.emit({ type: 'response.audio.done', response_id: 'reply-response' })
    await vi.advanceTimersByTimeAsync(1000)
    expect(probe.ended).not.toHaveBeenCalled()
    expect(probe.abandoned).toHaveBeenCalledOnce()
    probe.internal.close()
  })

  it('notifies a start arriving during drain for the still-owned reply', async () => {
    vi.useFakeTimers()
    const probe = session()
    await probe.authorize()
    probe.emit({ type: 'response.audio.done', response_id: 'reply-response' })
    probe.internal.markPlaybackStarted()
    expect(probe.started).toHaveBeenCalledOnce()
    probe.internal.close()
    expect(probe.ended).not.toHaveBeenCalled()
  })

  it('does not turn first media plus generation EOS into a complete delivery', async () => {
    vi.useFakeTimers()
    const probe = session()
    await probe.authorize()
    probe.internal.markPlaybackStarted()
    probe.emit({ type: 'response.audio.done', response_id: 'reply-response' })
    probe.emit({ type: 'response.done', response: { id: 'reply-response', status: 'completed' } })
    await vi.advanceTimersByTimeAsync(1000)
    expect(probe.ended).not.toHaveBeenCalled()
    expect(probe.unconfirmed).toHaveBeenCalledOnce()
    expect(probe.abandoned).not.toHaveBeenCalled()
    expect(probe.status.mock.calls.at(-1)?.[0].phase).toBe('listening')
    probe.emit({ type: 'response.audio.done', response_id: 'reply-response' })
    probe.internal.close()
    await vi.advanceTimersByTimeAsync(100_000)
    expect(probe.unconfirmed).toHaveBeenCalledOnce()
    expect(probe.ended).not.toHaveBeenCalled()
  })

  it('cancels during drain without later issuing an unconfirmed or successful receipt', async () => {
    vi.useFakeTimers()
    const probe = session()
    await probe.authorize()
    probe.internal.markPlaybackStarted()
    probe.emit({ type: 'response.audio.done', response_id: 'reply-response' })
    probe.internal.close()
    await vi.advanceTimersByTimeAsync(100_000)
    expect(probe.abandoned).toHaveBeenCalledOnce()
    expect(probe.unconfirmed).not.toHaveBeenCalled()
    expect(probe.ended).not.toHaveBeenCalled()
  })

  it('releases the sequential speech queue even when the receipt consumer throws', async () => {
    vi.useFakeTimers()
    const probe = session(true)
    await probe.authorize()
    probe.internal.markPlaybackStarted()
    probe.emit({ type: 'response.audio.done', response_id: 'reply-response' })
    await vi.advanceTimersByTimeAsync(1000)
    expect(probe.internal.drainingToolCalls).toBe(false)
    expect(probe.ended).not.toHaveBeenCalled()
    expect(probe.error).toHaveBeenCalledExactlyOnceWith('播放回执接收失败，原任务结果保留，未记为送达。')
    probe.internal.close()
  })
})
