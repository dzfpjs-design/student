import { afterEach, describe, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'

type Protocol = Pick<QwenRealtimeVoiceSession, 'close' | 'takePendingInputsForReconnect'> & {
  bindChannel: (channel: RTCDataChannel) => void; receive: (raw: string) => void
  audioTrack: { enabled: boolean }; replyGeneration: number; inputDeliveryFailed: boolean
  fail: (error: unknown) => void; pendingPlayback: unknown; playbackAuthorized: boolean
}
const sessions: Protocol[] = []
afterEach(() => { sessions.splice(0).forEach(session => session.close()); vi.useRealTimers(); vi.restoreAllMocks() })
function channel() {
  return Object.assign(new EventTarget(), { readyState: 'open', send: vi.fn(), close: vi.fn() }) as unknown as RTCDataChannel
}
function setup(callbacks: ConstructorParameters<typeof QwenRealtimeVoiceSession>[0]['callbacks']) {
  const session = Object.assign(new QwenRealtimeVoiceSession({ childName: 'synthetic', requireCommittedTranscript: true, callbacks }), {
    audioTrack: { enabled: true, getSettings: () => ({}) }, microphoneReleased: true,
  }) as unknown as Protocol
  sessions.push(session)
  const owned = channel(); session.bindChannel(owned)
  const emit = (type: string, values: Record<string, unknown> = {}) => session.receive(JSON.stringify({ type, ...values }))
  const final = (id: string, text: string) => {
    emit('input_audio_buffer.speech_started', { item_id: id })
    emit('conversation.item.input_audio_transcription.completed', { item_id: id, transcript: text })
  }
  const tool = (id: string) => {
    emit('response.created', { response: { id } })
    emit('response.function_call_arguments.done', { response_id: id, name: 'handle_child_turn', call_id: id, arguments: '{}' })
  }
  return { session, owned, emit, final, tool }
}

describe('current RTC transport retirement', () => {
  it('freezes capture and dispatch when the current data channel closes and preserves pending original identity', async () => {
    const source = { sessionId: 'original-session', reference: { child_id: 'original-child' }, carryoverConfirmationId: null }
    const status = vi.fn(), error = vi.fn(), handled = vi.fn()
    const p = setup({ onStatus: status, onError: error, captureInputContext: () => source, onToolCall: handled })
    p.final('waiting', '保留我的完整原话'); source.reference.child_id = 'later-child'
    p.owned.dispatchEvent(new Event('close'))
    expect(p.session.audioTrack.enabled).toBe(false)
    expect(error).toHaveBeenCalledOnce(); expect(status.mock.calls.at(-1)?.[0].phase).toBe('error')
    p.tool('late'); p.final('late-input', '不准继续派发')
    await Promise.resolve(); expect(handled).not.toHaveBeenCalled()
    expect(p.session.takePendingInputsForReconnect()).toEqual([{ utterance: '保留我的完整原话', capture: expect.objectContaining({
      reference: { child_id: 'original-child' }, source: 'accepted_onset', localInputId: expect.any(String),
    }) }])
    expect(() => p.session.fail(new Error('late transport error'))).not.toThrow()
    expect(error).toHaveBeenCalledOnce()
  })

  it('ignores close/error from a retired channel and fails once only for the current owner', () => {
    const status = vi.fn(), error = vi.fn()
    const p = setup({ onStatus: status, onError: error })
    const newer = channel(); p.session.bindChannel(newer)
    p.owned.dispatchEvent(new Event('close')); p.owned.dispatchEvent(new Event('error'))
    expect(p.session.audioTrack.enabled).toBe(true); expect(error).not.toHaveBeenCalled()
    newer.dispatchEvent(new Event('close')); newer.dispatchEvent(new Event('error'))
    expect(p.session.audioTrack.enabled).toBe(false); expect(error).toHaveBeenCalledOnce()
    expect(status.mock.calls.filter(([value]) => value.phase === 'error')).toHaveLength(1)
  })

  it('never revives an in-flight result or dispatches the next queued tool after failure', async () => {
    let resolve!: (result: { responseText: string; onPlaybackAbandoned: () => void }) => void
    const abandoned = vi.fn(), handled = vi.fn(() => new Promise<{ responseText: string; onPlaybackAbandoned: () => void }>(yes => { resolve = yes }))
    const p = setup({ onStatus: vi.fn(), onToolCall: handled })
    p.final('first', '第一句已交给Runtime'); p.tool('first-tool')
    p.final('second', '第二句尚未派发'); p.tool('second-tool')
    p.final('third', '第三句尚未派发')
    p.session.fail(new Error('transport failed'))
    resolve({ responseText: '旧回复不得重新播报', onPlaybackAbandoned: abandoned })
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    expect(handled).toHaveBeenCalledTimes(1); expect(abandoned).toHaveBeenCalledOnce()
    expect(p.session.playbackAuthorized).toBe(false)
    expect(p.owned.send).not.toHaveBeenCalledWith(expect.stringContaining('旧回复不得重新播报'))
    expect(p.session.takePendingInputsForReconnect()).toEqual([{ utterance: '第二句尚未派发' }, { utterance: '第三句尚未派发' }])
  })

  it('freezes before notifying broken consumers and does not let open replace a failed owner', () => {
    const p = setup({ onStatus: state => { if (state.phase === 'error') throw Error('private UI detail') },
      onError: () => { throw Error('private error detail') }, onToolCall: vi.fn() })
    p.final('pending', '原话不能丢')
    expect(() => p.session.fail(new Error('transport failure'))).not.toThrow()
    expect(p.session.audioTrack.enabled).toBe(false)
    const late = channel(); p.session.bindChannel(late); late.dispatchEvent(new Event('open'))
    expect(p.session.takePendingInputsForReconnect()).toEqual([{ utterance: '原话不能丢' }])
    expect((p.session as unknown as { channel: RTCDataChannel }).channel).toBe(p.owned)
  })

  it('does not schedule reconnection after an error consumer synchronously closes for privacy', () => {
    const status = vi.fn()
    const p = setup({ onStatus: status, onError: () => p.session.close('privacy_stop') })
    p.session.fail(new Error('transport failed'))
    expect(status.mock.calls.at(-1)?.[0].phase).toBe('idle')
    expect(p.session.takePendingInputsForReconnect()).toEqual([])
  })

  it('keeps failure terminal across delayed channel-open, interruption and peer-status callbacks', async () => {
    const status = vi.fn()
    const p = setup({ onStatus: status })
    const internal = p.session as unknown as { sessionCreated: boolean; peer: { connectionState: string }; handleConnectionState: () => void }
    internal.sessionCreated = true
    p.session.fail(new Error('current connection failed'))
    const calls = status.mock.calls.length
    p.owned.dispatchEvent(new Event('open'))
    ;(p.session as unknown as { interrupt: () => void }).interrupt()
    internal.peer = { connectionState: 'disconnected' }; internal.handleConnectionState()
    internal.peer.connectionState = 'connected'; internal.handleConnectionState()
    internal.peer = null as unknown as { connectionState: string }
    await Promise.resolve()
    expect(p.owned.send).not.toHaveBeenCalled()
    expect(status.mock.calls).toHaveLength(calls)
    expect(status.mock.calls.at(-1)?.[0].phase).toBe('error')
  })
})
