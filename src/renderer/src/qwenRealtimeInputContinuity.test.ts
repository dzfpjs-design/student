import { afterEach, describe, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'

type Session = Pick<QwenRealtimeVoiceSession, 'close' | 'takePendingInputsForReconnect' | 'resumePendingInputs'> & {
  receive: (raw: string) => void
}
const sessions: QwenRealtimeVoiceSession[] = []
afterEach(() => { sessions.splice(0).forEach(session => session.close()); vi.useRealTimers() })
const make = (callbacks: ConstructorParameters<typeof QwenRealtimeVoiceSession>[0]['callbacks']) => {
  const session = Object.assign(new QwenRealtimeVoiceSession({ childName: 'test', requireCommittedTranscript: true, callbacks }), {
    audioTrack: { enabled: true, getSettings: () => ({}), stop: vi.fn() },
    microphoneReleased: true, channel: { readyState: 'open', send: vi.fn(), close: vi.fn() },
  })
  sessions.push(session)
  return session as unknown as Session
}
const receive = (session: Session, type: string, values: Record<string, unknown> = {}) => {
  session.receive(JSON.stringify({ type, ...values }))
}
const completed = (session: Session, itemId: string, transcript: string) => {
  receive(session, 'input_audio_buffer.speech_started', { item_id: itemId })
  receive(session, 'conversation.item.input_audio_transcription.completed', { item_id: itemId, transcript })
}
const tool = (session: Session, callId: string) => {
  receive(session, 'response.created', { response: { id: callId } })
  receive(session, 'response.function_call_arguments.done', {
    response_id: callId, name: 'handle_child_turn', call_id: callId, arguments: '{}',
  })
}

describe('completed input continuity across transport reconnect', () => {
  it.each([
    ['admission', 'tool'], ['transcript', 'tool'], ['admission', 'committed'], ['transcript', 'committed'],
  ] as const)('retains the original and stops dispatch after %s notification throws in %s mode', async (failure, mode) => {
    const source = { sessionId: 'original-session', carryoverConfirmationId: null, reference: { child_id: 'original-child' } }
    const errors = vi.fn(), onToolCall = vi.fn(), onUntooledTurn = vi.fn(), onCommittedTurn = vi.fn()
    const old = make({ onStatus: vi.fn(), captureInputContext: () => source,
      onInputCompleted: () => { if (failure === 'admission') throw Error('private callback detail') },
      onUserTranscript: () => { if (failure === 'transcript') throw Error('private callback detail') },
      onError: errors, onToolCall, onUntooledTurn, ...(mode === 'committed' ? { onCommittedTurn } : {}) })
    receive(old, 'input_audio_buffer.speech_started', { item_id: 'failed-notification' })
    source.reference.child_id = 'later-child'
    expect(() => receive(old, 'conversation.item.input_audio_transcription.completed', {
      item_id: 'failed-notification', transcript: '完整原话不能丢失',
    })).not.toThrow()
    tool(old, 'after-failure')
    await Promise.resolve()
    expect(onToolCall).not.toHaveBeenCalled()
    expect(onUntooledTurn).not.toHaveBeenCalled()
    expect(onCommittedTurn).not.toHaveBeenCalled()
    expect(errors).toHaveBeenCalledWith(expect.not.stringContaining('private callback detail'))
    const pending = old.takePendingInputsForReconnect()
    expect(pending).toEqual([{ utterance: '完整原话不能丢失', capture: expect.objectContaining({
      sessionId: 'original-session', reference: { child_id: 'original-child' }, source: 'accepted_onset',
    }) }])
    expect(old.takePendingInputsForReconnect()).toEqual([])
    const recovered = vi.fn().mockResolvedValue(undefined)
    const next = make({ onStatus: vi.fn(), onUntooledTurn: recovered })
    Object.assign(next, { microphoneReleased: false, sender: { replaceTrack: vi.fn().mockResolvedValue(undefined) } })
    next.resumePendingInputs(pending); receive(next, 'session.updated')
    await vi.waitFor(() => expect(recovered).toHaveBeenCalledExactlyOnceWith('完整原话不能丢失', 'reconnect', pending[0].capture))
  })

  it('does not dispatch waiting originals or resume listening when an in-flight result arrives after notification failure', async () => {
    let finish!: (result: { responseText: string }) => void
    const toolCallback = vi.fn(() => new Promise<{ responseText: string }>(resolve => { finish = resolve }))
    const status = vi.fn()
    const old = make({ onStatus: status, onToolCall: toolCallback,
      onInputCompleted: text => { if (text === '第三句') throw Error('failed notification') } })
    completed(old, 'inflight-first', '第一句'); tool(old, 'inflight-tool')
    completed(old, 'waiting-second', '第二句'); tool(old, 'waiting-tool')
    completed(old, 'failed-third', '第三句')
    finish({ responseText: '旧结果不能重新开放输出' })
    await vi.waitFor(() => expect((old as unknown as { toolRunning: boolean }).toolRunning).toBe(false))
    expect(toolCallback).toHaveBeenCalledTimes(1)
    expect(status.mock.calls.at(-1)?.[0].phase).toBe('error')
    expect(status.mock.calls.at(-1)?.[0].pendingUtterances).toBe(3)
    expect((old as unknown as { audioTrack: { enabled: boolean } }).audioTrack.enabled).toBe(false)
    expect(old.takePendingInputsForReconnect()).toEqual([{ utterance: '第二句' }, { utterance: '第三句' }])
    expect(() => old.resumePendingInputs(['不得恢复到失败连接'])).toThrow('新连接')
  })

  it('preserves FIFO even when error consumers also throw and ignores later transport retries', () => {
    const old = make({ onStatus: status => { if (status.phase === 'error') throw Error('status failed') },
      onInputCompleted: text => { if (text === '第二句') throw Error('admission failed') },
      onError: () => { throw Error('error consumer failed') }, onToolCall: vi.fn() })
    completed(old, 'first-notification', '第一句')
    expect(() => completed(old, 'second-notification', '第二句')).not.toThrow()
    expect(() => completed(old, 'third-notification', '第三句')).not.toThrow()
    expect(old.takePendingInputsForReconnect()).toEqual([{ utterance: '第一句' }, { utterance: '第二句' }])
  })

  it('does not notify the transcript or dispatch after a synchronous privacy stop closes the session', async () => {
    const transcript = vi.fn(), committed = vi.fn()
    const old = make({ onStatus: vi.fn(), onInputCompleted: () => old.close('privacy_stop'),
      onUserTranscript: transcript, onCommittedTurn: committed })
    completed(old, 'privacy-stop', '停止监听')
    await Promise.resolve()
    expect(transcript).not.toHaveBeenCalled()
    expect(committed).not.toHaveBeenCalled()
    expect(old.takePendingInputsForReconnect()).toEqual([])
  })

  it('transfers only undispatched complete inputs, including repeated words, in arrival order', async () => {
    const old = make({ onStatus: vi.fn(), onToolCall: () => new Promise(() => {}), onUntooledTurn: vi.fn() })
    completed(old, 'one', '第一句已经交给运行时'); tool(old, 'call-one')
    completed(old, 'two', '继续'); tool(old, 'call-two')
    completed(old, 'three', '继续')
    receive(old, 'conversation.item.input_audio_transcription.delta', { item_id: 'four', text: '未完成' })
    const pending = old.takePendingInputsForReconnect()
    expect(pending).toEqual([{ utterance: '继续' }, { utterance: '继续' }])
    expect(old.takePendingInputsForReconnect()).toEqual([])
    old.close()
    const recovered = vi.fn().mockResolvedValue(undefined)
    const next = make({ onStatus: vi.fn(), onUntooledTurn: recovered })
    Object.assign(next, { microphoneReleased: false, sessionReady: false, sender: { replaceTrack: vi.fn().mockResolvedValue(undefined) } })
    next.resumePendingInputs(pending)
    expect(recovered).not.toHaveBeenCalled()
    receive(next, 'session.updated')
    await vi.waitFor(() => expect(recovered).toHaveBeenCalledTimes(2))
    expect(recovered.mock.calls).toEqual([['继续', 'reconnect'], ['继续', 'reconnect']])
  })

  it('keeps new tools behind restored inputs and does not fabricate old cloud tool outputs', async () => {
    let finish!: () => void
    const pending = new Promise<void>(resolve => { finish = resolve })
    const order: string[] = [], sent = vi.fn()
    const next = make({ onStatus: vi.fn(), onUntooledTurn: async text => { order.push(text); if (text === 'old-one') await pending },
      onToolCall: async text => { order.push(text); return { responseText: '', speak: false } } })
    Object.assign(next, { microphoneReleased: false, sessionReady: false, sender: { replaceTrack: vi.fn().mockResolvedValue(undefined) },
      channel: { readyState: 'open', send: sent, close: vi.fn() } })
    next.resumePendingInputs(['old-one', 'old-two'])
    receive(next, 'session.updated'); await Promise.resolve(); await Promise.resolve()
    completed(next, 'fresh', 'fresh'); tool(next, 'fresh-call')
    finish(); await pending
    await vi.waitFor(() => expect(order).toEqual(['old-one', 'old-two', 'fresh']))
    const outputs = sent.mock.calls.map(([raw]) => JSON.parse(raw)).filter(event => event.type === 'conversation.item.create')
    expect(outputs).toHaveLength(1)
    expect(outputs[0].item.call_id).toBe('fresh-call')
  })

  it('a second loss returns only restored input not already submitted to Runtime', async () => {
    const next = make({ onStatus: vi.fn(), onUntooledTurn: () => new Promise(() => {}) })
    Object.assign(next, { microphoneReleased: false, sessionReady: false, sender: { replaceTrack: vi.fn().mockResolvedValue(undefined) } })
    next.resumePendingInputs(['one', 'two', 'three'])
    receive(next, 'session.updated'); await Promise.resolve(); await Promise.resolve()
    expect(next.takePendingInputsForReconnect()).toEqual([{ utterance: 'two' }, { utterance: 'three' }])
  })
})
