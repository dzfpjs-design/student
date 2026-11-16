import { afterEach, describe, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'

type Protocol = Pick<QwenRealtimeVoiceSession, 'close' | 'takePendingInputsForReconnect'> & {
  receive: (raw: string) => void; audioTrack: { enabled: boolean }; inputDeliveryFailed: boolean
  inputItemPermissions: Map<string, boolean>; inputStartEventIds: Set<string>; committedAudioItemIds: Set<string>
  completedTranscriptQueue: unknown[]
}
const sessions: Protocol[] = []
afterEach(() => { sessions.splice(0).forEach(session => session.close()); vi.restoreAllMocks() })
function setup() {
  const saved = vi.fn(), error = vi.fn(), status = vi.fn(), capture = vi.fn(() => ({
    sessionId: 'synthetic-session', reference: { child_id: 'synthetic-child' }, carryoverConfirmationId: null,
  }))
  const session = Object.assign(new QwenRealtimeVoiceSession({ childName: 'synthetic', requireCommittedTranscript: true,
    callbacks: { onStatus: status, onError: error, captureInputContext: capture, onInputCompleted: saved } }), {
    audioTrack: { enabled: true, getSettings: () => ({}), stop: vi.fn() }, microphoneReleased: true,
    channel: { readyState: 'open', send: vi.fn(), close: vi.fn() },
  }) as unknown as Protocol
  sessions.push(session)
  const emit = (type: string, values: Record<string, unknown> = {}) => session.receive(JSON.stringify({ type, ...values }))
  const start = (id: string, event_id?: string) => emit('input_audio_buffer.speech_started', { item_id: id, event_id })
  const final = (id: string) => emit('conversation.item.input_audio_transcription.completed', { item_id: id, transcript: '完整原话不应被重放' })
  return { session, saved, error, status, capture, emit, start, final }
}

describe('input identity lifetime is not an eviction cache', () => {
  it('never reauthorizes a denied item after capacity pressure with a new event identity', () => {
    const p = setup(); p.session.audioTrack.enabled = false; p.start('denied-original', 'first-denied-event')
    p.session.audioTrack.enabled = true
    for (let i = 0; i < 2048; i++) p.session.inputItemPermissions.set('other-' + i, false)
    p.start('fresh-pressure', 'pressure-event')
    p.start('denied-original', 'new-denied-event'); p.final('denied-original')
    expect(p.saved).not.toHaveBeenCalled(); expect(p.session.inputDeliveryFailed).toBe(true)
    expect(p.session.audioTrack.enabled).toBe(false); expect(p.error).toHaveBeenCalledOnce()
    expect(p.session.inputItemPermissions.get('denied-original')).toBe(false)
  })

  it('retires at the actual limit before creating another source and preserves an untouched original', () => {
    const p = setup(); p.start('original'); p.final('original')
    const original = structuredClone(p.session.completedTranscriptQueue[0])
    for (let i = p.session.inputItemPermissions.size; i < 2048; i++) p.session.inputItemPermissions.set('fill-' + i, false)
    const captures = p.capture.mock.calls.length
    p.start('overflow')
    expect(p.session.inputItemPermissions.size).toBe(2048); expect(p.capture).toHaveBeenCalledTimes(captures)
    expect(p.session.inputDeliveryFailed).toBe(true); expect(p.saved).toHaveBeenCalledOnce()
    expect(p.session.takePendingInputsForReconnect()).toEqual([original])
  })

  it('never drops an old anonymous onset event identity to authorize its replay', () => {
    const p = setup()
    for (let i = 0; i < 2048; i++) p.session.inputStartEventIds.add('event-' + i)
    p.emit('input_audio_buffer.speech_started', { event_id: 'new-event' })
    p.emit('input_audio_buffer.speech_started', { event_id: 'event-0' })
    p.final('later-id')
    expect(p.capture).not.toHaveBeenCalled(); expect(p.saved).not.toHaveBeenCalled()
    expect(p.session.inputStartEventIds.size).toBe(2048); expect(p.session.inputStartEventIds.has('event-0')).toBe(true)
    expect(p.session.audioTrack.enabled).toBe(false); expect(p.error).toHaveBeenCalledOnce()
  })

  it('never evicts a consumed audio identity to readmit its late completion', () => {
    const p = setup()
    for (let i = 0; i < 2048; i++) p.session.committedAudioItemIds.add('completed-' + i)
    p.start('new-final'); p.final('new-final'); p.final('completed-0')
    expect(p.saved).not.toHaveBeenCalled(); expect(p.session.committedAudioItemIds.size).toBe(2048)
    expect(p.session.committedAudioItemIds.has('completed-0')).toBe(true)
    expect(p.session.inputDeliveryFailed).toBe(true); expect(p.error).toHaveBeenCalledOnce()
  })

  it('accepts an existing last identity at capacity without freezing normal completion', () => {
    const p = setup(); p.start('last-existing')
    for (let i = 1; i < 2048; i++) p.session.inputItemPermissions.set('fill-' + i, false)
    p.final('last-existing'); p.final('last-existing')
    expect(p.saved).toHaveBeenCalledOnce(); expect(p.error).not.toHaveBeenCalled()
    expect(p.session.audioTrack.enabled).toBe(true)
  })
})
