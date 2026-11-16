import { afterEach, describe, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession, QWEN_REALTIME_TOOL_NAME } from './qwenRealtimeVoice'

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

function protocol() {
  const handle = vi.fn().mockResolvedValue({ responseText: 'saved', speak: false })
  const end = vi.fn(), abandoned = vi.fn(), status = vi.fn()
  vi.spyOn(console, 'warn').mockImplementation(() => undefined)
  const session = Object.assign(new QwenRealtimeVoiceSession({ childName: 'synthetic',
    requireCommittedTranscript: true, callbacks: { onStatus: status, onToolCall: handle } }), {
    audioTrack: { enabled: true, getSettings: () => ({}) }, microphoneReleased: true,
    channel: { readyState: 'open', send: () => undefined, close: () => undefined },
  }) as unknown as { receive: (raw: string) => void; close: () => void; activeResponseId: string | null
    responseActive: boolean; pendingPlayback: unknown; playbackAuthorized: boolean; completedTranscriptQueue: unknown[] }
  const send = (event: unknown) => session.receive(JSON.stringify(event))
  send({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'original', transcript: '保留我的完整原话' })
  const created = () => send({ type: 'response.created', response: { id: 'current' } })
  const tool = { type: 'response.function_call_arguments.done', response_id: 'current',
    name: QWEN_REALTIME_TOOL_NAME, call_id: 'original-tool', arguments: '{}' }
  return { session, send, created, tool, handle, end, abandoned, status }
}

describe('production response event boundary', () => {
  it.each([null, [], 42, 'event', { type: 42 }, { type: 'response.created', response: [] },
    { type: 'response.function_call_arguments.done', response_id: 'current', call_id: 42 },
    { type: 'response.created', response: { id: 42 } },
  ])('ignores malformed envelope %# without throwing or mutating the original', (event) => {
    const p = protocol(); p.created(); p.status.mockClear()
    expect(() => p.send(event)).not.toThrow()
    expect(p.session.activeResponseId).toBe('current'); expect(p.session.responseActive).toBe(true)
    expect(p.session.completedTranscriptQueue).toHaveLength(1)
    expect(p.handle).not.toHaveBeenCalled(); expect(p.status).not.toHaveBeenCalled()
    p.session.close()
  })

  it.each([undefined, '', ' ', 42, 'other'])('rejects an unowned tool identity %s and preserves the next exact tool', async (responseId) => {
    const p = protocol(); p.created()
    p.send({ ...p.tool, response_id: responseId, call_id: 'unowned' })
    await Promise.resolve()
    expect(p.handle).not.toHaveBeenCalled(); expect(p.session.completedTranscriptQueue).toHaveLength(1)
    p.send(p.tool); await Promise.resolve(); await Promise.resolve()
    expect(p.handle).toHaveBeenCalledExactlyOnceWith('保留我的完整原话')
    p.session.close()
  })

  it('rejects identified tools before creation without consuming their durable original', async () => {
    const p = protocol()
    p.send(p.tool); await Promise.resolve()
    expect(p.handle).not.toHaveBeenCalled(); expect(p.session.completedTranscriptQueue).toHaveLength(1)
    p.created(); p.send(p.tool); await Promise.resolve(); await Promise.resolve()
    expect(p.handle).toHaveBeenCalledExactlyOnceWith('保留我的完整原话')
    p.session.close()
  })

  it.each([
    { type: 'response.audio.done' },
    { type: 'response.done', response_id: 'current', response: { id: 'other', status: 'failed' } },
    { type: 'response.done', response: { status: 'failed' } },
    { type: 'response.done', response: { id: 'current' } },
    { type: 'response.done', response: { id: 'current', status: 'in_progress' } },
    { type: 'response.done', response_id: 'current' },
    { type: 'response.created' },
  ])('does not let an invalid event %# clear or drain current playback', async (event) => {
    vi.useFakeTimers()
    const p = protocol(); p.created()
    p.session.pendingPlayback = { responseText: 'current', onPlaybackEnd: p.end, onPlaybackAbandoned: p.abandoned }
    p.session.playbackAuthorized = true; p.status.mockClear()
    p.send(event); await vi.advanceTimersByTimeAsync(351)
    expect(p.session.activeResponseId).toBe('current'); expect(p.session.responseActive).toBe(true)
    expect(p.session.pendingPlayback).not.toBeNull(); expect(p.session.playbackAuthorized).toBe(true)
    expect(p.end).not.toHaveBeenCalled(); expect(p.abandoned).not.toHaveBeenCalled()
    expect(p.status).not.toHaveBeenCalled(); p.session.close()
  })

  it('rejects duplicate terminal and late tools after the identified response is done', async () => {
    const p = protocol()
    // No recovery consumer: ending the model response retires it, not the original evidence.
    p.created(); p.send({ type: 'response.done', response: { id: 'current', status: 'completed' } })
    p.send({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'next', transcript: '下一句话' })
    p.status.mockClear()
    p.send({ type: 'response.done', response: { id: 'current', status: 'completed' } })
    p.send(p.tool); await Promise.resolve()
    expect(p.handle).not.toHaveBeenCalled(); expect(p.session.completedTranscriptQueue).toHaveLength(1)
    expect(p.status).not.toHaveBeenCalled(); p.session.close()
  })

  it('emits only bounded fixed diagnostics without provider bodies or original identities', () => {
    const p = protocol(); p.created()
    for (let i = 0; i < 100; i++) {
      p.send({ ...p.tool, response_id: 'unknown-private-identity', arguments: 'private-content' })
      p.send(null)
      p.send({ type: 'response.created', response: { id: ' ' } })
    }
    expect(console.warn).toHaveBeenCalledTimes(3)
    expect(vi.mocked(console.warn).mock.calls.flat()).toEqual([
      'qwen_realtime_protocol_rejected:response_not_current',
      'qwen_realtime_protocol_rejected:invalid_event',
      'qwen_realtime_protocol_rejected:response_identity_invalid',
    ])
    p.session.close()
  })
})
