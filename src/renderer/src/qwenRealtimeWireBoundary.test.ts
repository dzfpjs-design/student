import { afterEach, describe, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'

const sessions: QwenRealtimeVoiceSession[] = []
afterEach(() => { sessions.splice(0).forEach(session => session.close()); vi.useRealTimers() })
function make(callbacks: ConstructorParameters<typeof QwenRealtimeVoiceSession>[0]['callbacks']) {
  const sent: Record<string, unknown>[] = []
  const session = Object.assign(new QwenRealtimeVoiceSession({ childName: 'synthetic', requireCommittedTranscript: true, callbacks }), {
    audioTrack: { enabled: true, getSettings: () => ({}), stop: vi.fn() }, microphoneReleased: true,
    channel: { readyState: 'open', send: (raw: string) => sent.push(JSON.parse(raw)), close: vi.fn() },
  })
  sessions.push(session)
  return { session: session as unknown as Pick<QwenRealtimeVoiceSession, 'close' | 'takePendingInputsForReconnect'> & {
    receive: (raw: string) => void; activeResponseId: string | null; pendingPlayback: unknown;
    responseActive: boolean; seenResponseIds: Set<string>;
  }, sent }
}
const event = (session: ReturnType<typeof make>['session'], type: string, fields: Record<string, unknown> = {}) =>
  session.receive(JSON.stringify({ type, ...fields }))
const input = (session: ReturnType<typeof make>['session'], itemId: string, transcript: string) => {
  event(session, 'input_audio_buffer.speech_started', { item_id: itemId })
  event(session, 'conversation.item.input_audio_transcription.completed', { item_id: itemId, transcript })
}
const tool = (session: ReturnType<typeof make>['session'], id: string) => {
  event(session, 'response.created', { response: { id } })
  event(session, 'response.function_call_arguments.done', { response_id: id, call_id: id,
    name: 'handle_child_turn', arguments: '{}' })
}
const output = (sent: Record<string, unknown>[]) => sent.filter(value => value.type === 'conversation.item.create')

describe('production RTC result wire and original response ownership', () => {
  it('never sends local-only response text, even when the caller returned the original body', async () => {
    const local = '仅本地合成证据：原题作答与孩子记忆，不许回填。'
    const { session, sent } = make({ onStatus: vi.fn(), onToolCall: async () => ({ responseText: local, speak: false }) })
    input(session, 'local-input', '请读原作答'); tool(session, 'local-response')
    await vi.waitFor(() => expect(output(sent)).toHaveLength(1))
    expect(JSON.stringify(sent)).not.toContain(local)
    expect(sent.some(value => value.type === 'response.create')).toBe(false)
    expect(JSON.parse((output(sent)[0].item as { output: string }).output)).toEqual({ response_text: 'local_reply_no_remote_speech' })
  })

  it('does not send a superseded reply body and retains the later complete human input', async () => {
    const local = '旧回复中的本地资料不应在取消后发送。'
    let resolve!: (value: { responseText: string }) => void
    const { session, sent } = make({ onStatus: vi.fn(), onToolCall: () => new Promise(yes => { resolve = yes }) })
    input(session, 'old-input', '第一句'); tool(session, 'old-response')
    input(session, 'later-input', '第二句完整原话')
    resolve({ responseText: local })
    await vi.waitFor(() => expect(output(sent)).toHaveLength(1))
    expect(JSON.stringify(sent)).not.toContain(local)
    expect(JSON.parse((output(sent)[0].item as { output: string }).output)).toEqual({
      response_text: 'reply_abandoned_no_remote_speech', playback_abandoned: true,
    })
    expect(session.takePendingInputsForReconnect()).toEqual([{ utterance: '第二句完整原话' }])
  })

  it('keeps an authorized public reply verbatim without granting complete RTC delivery', async () => {
    const text = '这是已授权的合成公共回复。', ended = vi.fn(), unconfirmed = vi.fn()
    const { session, sent } = make({ onStatus: vi.fn(), onToolCall: async () => ({ responseText: text,
      onPlaybackEnd: ended, onPlaybackUnconfirmed: unconfirmed }) })
    input(session, 'public-input', '讲个故事'); tool(session, 'public-response')
    await vi.waitFor(() => expect(sent.some(value => value.type === 'response.create')).toBe(true))
    expect(JSON.parse((output(sent)[0].item as { output: string }).output)).toEqual({ response_text: text })
    expect(ended).not.toHaveBeenCalled(); expect(unconfirmed).not.toHaveBeenCalled()
  })

  it('does not let an old response.created consume a newer pending original utterance', async () => {
    const recover = vi.fn().mockResolvedValue(undefined), handle = vi.fn().mockResolvedValue({ responseText: '', speak: false })
    const { session } = make({ onStatus: vi.fn(), onToolCall: handle, onUntooledTurn: recover })
    input(session, 'first-input', '旧原话'); tool(session, 'first-response')
    await vi.waitFor(() => expect(handle).toHaveBeenCalledTimes(1))
    event(session, 'response.done', { response: { id: 'first-response', status: 'completed' } })
    input(session, 'second-input', '新原话，不能被旧响应拿走')
    event(session, 'response.created', { response: { id: 'second-response' } })
    event(session, 'response.created', { response: { id: 'first-response' } })
    event(session, 'response.done', { response: { id: 'first-response', status: 'completed' } })
    await Promise.resolve()
    expect(session.activeResponseId).toBe('second-response')
    expect(recover).not.toHaveBeenCalled()
    expect(session.takePendingInputsForReconnect()).toEqual([{ utterance: '新原话，不能被旧响应拿走' }])
  })

  it('does not clear an in-flight original tool binding on duplicate creation', async () => {
    let finish!: (value: { responseText: string; speak: false }) => void
    const handle = vi.fn(() => new Promise<{ responseText: string; speak: false }>(yes => { finish = yes }))
    const { session } = make({ onStatus: vi.fn(), onToolCall: handle })
    input(session, 'one-input', '一句原话'); tool(session, 'one-response')
    input(session, 'two-input', '另一个原话')
    event(session, 'response.created', { response: { id: 'one-response' } })
    event(session, 'response.function_call_arguments.done', { response_id: 'one-response', call_id: 'duplicate-new-call',
      name: 'handle_child_turn', arguments: '{}' })
    expect(session.takePendingInputsForReconnect()).toEqual([{ utterance: '另一个原话' }])
    expect(handle).toHaveBeenCalledTimes(1)
    finish({ responseText: '', speak: false })
    await Promise.resolve()
  })

  it('retires response admission instead of evicting old identities at the bounded limit', () => {
    const status = vi.fn(), error = vi.fn()
    const { session, sent } = make({ onStatus: status, onError: error })
    input(session, 'waiting-original', '完整原话等待接续')
    for (let index = 0; index < 2048; index++) session.seenResponseIds.add(`seen-${index}`)
    event(session, 'response.created', { response: { id: 'over-limit-response' } })
    expect(error).toHaveBeenCalledOnce()
    expect(status.mock.calls.at(-1)?.[0].phase).toBe('error')
    expect(session.seenResponseIds.size).toBe(2048)
    event(session, 'response.function_call_arguments.done', { response_id: 'over-limit-response', call_id: 'new-call',
      name: 'handle_child_turn', arguments: '{}' })
    expect(sent.some(value => value.type === 'conversation.item.create')).toBe(false)
    expect(session.takePendingInputsForReconnect()).toEqual([{ utterance: '完整原话等待接续' }])
  })
})
