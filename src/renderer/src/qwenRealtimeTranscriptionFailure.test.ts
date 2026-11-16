import { afterEach, describe, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'

type Protocol = Pick<QwenRealtimeVoiceSession, 'close' | 'takePendingInputsForReconnect'> & {
  receive: (raw: string) => void
  audioTrack: { enabled: boolean }
  inputDeliveryFailed: boolean
  completedTranscriptQueue: unknown[]
}
const sessions: Protocol[] = []
afterEach(() => { sessions.splice(0).forEach(session => session.close()); vi.restoreAllMocks() })

function setup() {
  const saved = vi.fn(), error = vi.fn(), status = vi.fn(), tool = vi.fn()
  const capture = vi.fn(() => ({ sessionId: 'synthetic-session',
    reference: { child_id: 'synthetic-child' }, carryoverConfirmationId: null }))
  const session = Object.assign(new QwenRealtimeVoiceSession({ childName: 'synthetic', requireCommittedTranscript: true,
    callbacks: { onStatus: status, onError: error, onToolCall: tool,
      captureInputContext: capture, onInputCompleted: saved } }), {
    audioTrack: { enabled: true, getSettings: () => ({}), stop: vi.fn() }, microphoneReleased: true,
    channel: { readyState: 'open', send: vi.fn(), close: vi.fn() },
  }) as unknown as Protocol
  sessions.push(session)
  const emit = (type: string, values: Record<string, unknown> = {}) => session.receive(JSON.stringify({ type, ...values }))
  const start = (id: string) => emit('input_audio_buffer.speech_started', { item_id: id })
  const final = (id: string) => emit('conversation.item.input_audio_transcription.completed', {
    item_id: id, transcript: '已经可靠接纳的合成原话' })
  const failed = (item_id?: unknown) => emit('conversation.item.input_audio_transcription.failed', {
    ...(item_id !== undefined ? { item_id } : {}), error: { code: 'synthetic_asr_failure', message: '禁止泄露的供应商错误正文' } })
  return { session, saved, error, status, tool, capture, emit, start, final, failed }
}

describe('supplier transcription failure belongs only to a pending admitted input', () => {
  it('retires the matching connection instead of silently remaining in transcribing', () => {
    const p = setup(); p.start('pending'); p.emit('input_audio_buffer.speech_stopped', { item_id: 'pending' })
    p.failed('pending')
    expect(p.session.inputDeliveryFailed).toBe(true); expect(p.session.audioTrack.enabled).toBe(false)
    expect(p.error).toHaveBeenCalledOnce()
    expect(p.error.mock.calls[0][0]).not.toContain('禁止泄露')
    expect(p.status).toHaveBeenLastCalledWith(expect.objectContaining({ phase: 'error', speechActive: false }))
    expect(p.saved).not.toHaveBeenCalled(); expect(p.tool).not.toHaveBeenCalled()
  })

  it('preserves another completed original exactly once and ignores late success after retirement', () => {
    const p = setup(); p.start('completed'); p.final('completed')
    const original = structuredClone(p.session.completedTranscriptQueue[0])
    p.start('failed'); p.failed('failed'); p.final('failed'); p.failed('failed')
    expect(p.saved).toHaveBeenCalledOnce(); expect(p.error).toHaveBeenCalledOnce()
    expect(p.session.takePendingInputsForReconnect()).toEqual([original])
    expect(p.session.takePendingInputsForReconnect()).toEqual([])
    expect(p.tool).not.toHaveBeenCalled()
  })

  it('does not let a completed, denied or unknown item retire the current input', () => {
    const p = setup(); p.start('completed'); p.final('completed')
    p.session.audioTrack.enabled = false; p.start('denied'); p.session.audioTrack.enabled = true
    p.start('current'); p.failed('completed'); p.failed('denied'); p.failed('unknown')
    expect(p.error).not.toHaveBeenCalled(); expect(p.session.inputDeliveryFailed).toBe(false)
    p.final('current'); expect(p.saved).toHaveBeenCalledTimes(2)
  })

  it('rejects missing and malformed failure identities without consuming a current input', () => {
    const p = setup(); p.start('current'); p.failed(); p.failed(''); p.failed({ borrowed: 'current' })
    expect(p.error).not.toHaveBeenCalled(); p.final('current'); expect(p.saved).toHaveBeenCalledOnce()
  })

  it('releases only failed denied slots so background failures cannot exhaust later child input', () => {
    const p = setup(); p.session.audioTrack.enabled = false
    for (let i = 0; i < 64; i++) { p.start('denied-' + i); p.failed('denied-' + i) }
    p.session.audioTrack.enabled = true; p.start('current'); p.final('current')
    expect(p.error).not.toHaveBeenCalled(); expect(p.saved).toHaveBeenCalledOnce()
    expect(p.session.inputDeliveryFailed).toBe(false)
  })
})
