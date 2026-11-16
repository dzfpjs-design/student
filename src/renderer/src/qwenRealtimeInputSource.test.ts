import { afterEach, describe, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'

const sessions: QwenRealtimeVoiceSession[] = []
afterEach(() => { sessions.splice(0).forEach(session => session.close()); vi.useRealTimers() })
const make = (callbacks: ConstructorParameters<typeof QwenRealtimeVoiceSession>[0]['callbacks']) => {
  const session = Object.assign(new QwenRealtimeVoiceSession({ childName: 'test', requireCommittedTranscript: true, callbacks }), {
    audioTrack: { enabled: true, getSettings: () => ({}), stop: vi.fn() },
    microphoneReleased: true, channel: { readyState: 'open', send: vi.fn(), close: vi.fn() },
  })
  sessions.push(session)
  return session as unknown as {
    receive: (raw: string) => void
    takePendingInputsForReconnect: QwenRealtimeVoiceSession['takePendingInputsForReconnect']
    resumePendingInputs: QwenRealtimeVoiceSession['resumePendingInputs']
  }
}
const receive = (session: ReturnType<typeof make>, type: string, fields: Record<string, unknown> = {}) => {
  session.receive(JSON.stringify({ type, ...fields }))
}
const done = (session: ReturnType<typeof make>, item_id: string | undefined, transcript = '这道题选B') => {
  receive(session, 'conversation.item.input_audio_transcription.completed', { item_id, transcript })
}
const tool = (session: ReturnType<typeof make>, id: string) => {
  receive(session, 'response.created', { response: { id } })
  receive(session, 'response.function_call_arguments.done', { response_id: id, call_id: id,
    name: 'handle_child_turn', arguments: '{}' })
}
const context = (prompt = 'original') => ({ sessionId: 'same-session', carryoverConfirmationId: 'carryover-original',
  reference: { child_id: 'child', question_reference: { kind: 'diagnostic' as const,
    binding: { prompt_id: prompt, item_version: 'original-v1' } } } })

describe('realtime capture source follows the accepted human input', () => {
  it('notifies final-input storage immediately, before a tool or recovery, with the frozen source', () => {
    const source = context(), onInputCompleted = vi.fn(), onToolCall = vi.fn()
    const session = make({ onStatus: vi.fn(), captureInputContext: () => source, onInputCompleted, onToolCall })
    receive(session, 'input_audio_buffer.speech_started', { item_id: 'completed-input' })
    source.reference.question_reference.binding.prompt_id = 'later-page'
    receive(session, 'conversation.item.input_audio_transcription.delta', { item_id: 'completed-input', text: '预览' })
    expect(onInputCompleted).not.toHaveBeenCalled()
    done(session, 'completed-input', '小知，原题选B。')
    done(session, 'completed-input', '小知，原题选B。')
    expect(onInputCompleted).toHaveBeenCalledExactlyOnceWith('小知，原题选B。', expect.objectContaining({
      reference: context().reference, source: 'accepted_onset', localInputId: expect.any(String),
    }))
    expect(onToolCall).not.toHaveBeenCalled()
  })

  it('does not admit a gated speaker echo as a final input', () => {
    const onInputCompleted = vi.fn()
    const session = make({ onStatus: vi.fn(), captureInputContext: () => context(), onInputCompleted })
    Object.assign(session, { audioTrack: { enabled: false, getSettings: () => ({}), stop: vi.fn() } })
    receive(session, 'input_audio_buffer.speech_started', { item_id: 'echo' })
    done(session, 'echo', '扬声器回声')
    expect(onInputCompleted).not.toHaveBeenCalled()
  })

  it('does not replay consumed human input after an unsolicited model response', async () => {
    const onToolCall = vi.fn().mockResolvedValue({ responseText: '', speak: false })
    const onUntooledTurn = vi.fn().mockResolvedValue(undefined)
    const session = make({ onStatus: vi.fn(), captureInputContext: () => context(), onToolCall, onUntooledTurn })
    receive(session, 'input_audio_buffer.speech_started', { item_id: 'consumed' })
    done(session, 'consumed'); tool(session, 'consumed-tool')
    await vi.waitFor(() => expect(onToolCall).toHaveBeenCalledTimes(1))
    receive(session, 'response.done', { response: { id: 'consumed-tool', status: 'completed' } })
    receive(session, 'response.created', { response: { id: 'unsolicited' } })
    receive(session, 'response.audio_transcript.delta', { response_id: 'unsolicited', delta: '现在开始解释这道题' })
    receive(session, 'response.done', { response: { id: 'unsolicited', status: 'completed' } })
    await Promise.resolve()
    expect(onUntooledTurn).not.toHaveBeenCalled()
    expect(onToolCall).toHaveBeenCalledTimes(1)
    expect(session.takePendingInputsForReconnect()).toEqual([])
  })

  it('recovers only a new pending human input when model output bypasses its tool', async () => {
    let source = context('old')
    const onToolCall = vi.fn().mockResolvedValue({ responseText: '', speak: false })
    const onUntooledTurn = vi.fn().mockResolvedValue(undefined)
    const session = make({ onStatus: vi.fn(), captureInputContext: () => source, onToolCall, onUntooledTurn })
    receive(session, 'input_audio_buffer.speech_started', { item_id: 'old' }); done(session, 'old'); tool(session, 'old-tool')
    await vi.waitFor(() => expect(onToolCall).toHaveBeenCalledTimes(1))
    receive(session, 'response.done', { response: { id: 'old-tool', status: 'completed' } })
    source = context('new')
    receive(session, 'input_audio_buffer.speech_started', { item_id: 'new' }); done(session, 'new', '我选D')
    receive(session, 'response.created', { response: { id: 'new-response' } })
    receive(session, 'response.audio_transcript.delta', { response_id: 'new-response', delta: '直接回答' })
    receive(session, 'response.done', { response: { id: 'new-response', status: 'completed' } })
    await vi.waitFor(() => expect(onUntooledTurn).toHaveBeenCalledTimes(1))
    expect(onUntooledTurn).toHaveBeenCalledWith('我选D', undefined, expect.objectContaining({ reference: context('new').reference }))
    expect(onToolCall).toHaveBeenCalledTimes(1)
    expect(session.takePendingInputsForReconnect()).toEqual([])
  })

  it('freezes the source at onset rather than final ASR or tool dispatch', async () => {
    const source = context(), captureInputContext = vi.fn(() => source), onToolCall = vi.fn().mockResolvedValue({ responseText: '', speak: false })
    const session = make({ onStatus: vi.fn(), captureInputContext, onToolCall })
    receive(session, 'input_audio_buffer.speech_started', { item_id: 'one' })
    source.reference.question_reference.binding.prompt_id = 'new-page'
    done(session, 'one'); tool(session, 'one-tool')
    await vi.waitFor(() => expect(onToolCall).toHaveBeenCalledTimes(1))
    const capture = onToolCall.mock.calls[0][1]
    expect(capture).toMatchObject({ ...context(), source: 'accepted_onset' })
    expect(capture.localInputId).toEqual(expect.any(String))
    expect(captureInputContext).toHaveBeenCalledTimes(1)
  })

  it('preserves separate frozen sources and input keys through two reconnects', async () => {
    let current = context('first')
    const original = make({ onStatus: vi.fn(), captureInputContext: () => current,
      onToolCall: () => new Promise(() => {}), onUntooledTurn: vi.fn() })
    receive(original, 'input_audio_buffer.speech_started', { item_id: 'active' }); done(original, 'active'); tool(original, 'active-tool')
    receive(original, 'input_audio_buffer.speech_started', { item_id: 'waiting-one' }); done(original, 'waiting-one', '继续')
    current = context('second')
    receive(original, 'input_audio_buffer.speech_started', { item_id: 'waiting-two' }); done(original, 'waiting-two', '继续')
    const pending = original.takePendingInputsForReconnect()
    const middle = make({ onStatus: vi.fn(), onUntooledTurn: vi.fn() })
    middle.resumePendingInputs(pending)
    const transferred = middle.takePendingInputsForReconnect()
    expect(transferred).toEqual(pending)
    expect(transferred).toHaveLength(2)
    expect(transferred[0]).toMatchObject({ utterance: '继续', capture: { reference: context('first').reference } })
    expect(transferred[1]).toMatchObject({ utterance: '继续', capture: { reference: context('second').reference } })
    const [one, two] = transferred as unknown as { capture: { localInputId: string } }[]
    expect(one.capture.localInputId).not.toBe(two.capture.localInputId)
  })

  it('binds one anonymous onset to its later final ID without taking a fresh source', async () => {
    let current = context('anonymous-original')
    const onToolCall = vi.fn().mockResolvedValue({ responseText: '', speak: false })
    const session = make({ onStatus: vi.fn(), captureInputContext: () => current, onToolCall })
    receive(session, 'input_audio_buffer.speech_started')
    current = context('next-page')
    done(session, 'late-id'); tool(session, 'late-tool')
    await vi.waitFor(() => expect(onToolCall).toHaveBeenCalledTimes(1))
    expect(onToolCall.mock.calls[0][1]).toMatchObject({ reference: context('anonymous-original').reference, source: 'accepted_onset' })
  })

  it('marks missing onset explicitly instead of calling it an onset snapshot', async () => {
    const onToolCall = vi.fn().mockResolvedValue({ responseText: '', speak: false })
    const session = make({ onStatus: vi.fn(), captureInputContext: () => context(), onToolCall })
    done(session, 'without-onset'); tool(session, 'missing-tool')
    await vi.waitFor(() => expect(onToolCall).toHaveBeenCalledTimes(1))
    expect(onToolCall.mock.calls[0][1]).toMatchObject({ source: 'unbound' })
  })

  it('carries the same source through missing-tool recovery without sending it on the wire', async () => {
    vi.useFakeTimers()
    const onUntooledTurn = vi.fn().mockResolvedValue(undefined), send = vi.fn()
    const session = make({ onStatus: vi.fn(), captureInputContext: () => context(), onUntooledTurn })
    Object.assign(session, { channel: { readyState: 'open', send, close: vi.fn() } })
    receive(session, 'input_audio_buffer.speech_started', { item_id: 'source-one' }); done(session, 'source-one')
    await vi.advanceTimersByTimeAsync(8_001)
    expect(onUntooledTurn).toHaveBeenCalledWith('这道题选B', undefined, expect.objectContaining({
      reference: context().reference, source: 'accepted_onset' }))
    expect(JSON.stringify(send.mock.calls)).not.toContain('original-v1')
    expect(JSON.stringify(send.mock.calls)).not.toContain('carryover-original')
  })
})
