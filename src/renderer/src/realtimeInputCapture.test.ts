import { afterEach, describe, expect, it } from 'vitest'
import type { QwenRealtimeInputCapture } from './qwenRealtimeVoice'
import { clearVoiceCaptureContext, setVoiceQuestionCapture, setVoicePendingEvaluationCapture, voiceCaptureReference } from './voiceCaptureContext'
import { realtimeInputSourceStatus } from './realtimeInputCapture'

afterEach(() => clearVoiceCaptureContext())
const snapshot = (): QwenRealtimeInputCapture => ({ sessionId: 'session', reference: voiceCaptureReference('session', 'child'),
  localInputId: 'synthetic-input', capturedAt: 100, source: 'accepted_onset', carryoverConfirmationId: null })

describe('realtime source cannot be rebound at dispatch', () => {
  it('rejects a new question without changing the captured original', () => {
    setVoiceQuestionCapture('session', 'child', { kind: 'diagnostic', binding: { prompt_id: 'original', item_version: 'v1' } })
    const capture = snapshot()
    expect(realtimeInputSourceStatus(capture, 'session', 'child')).toBe('current')
    setVoiceQuestionCapture('session', 'child', { kind: 'diagnostic', binding: { prompt_id: 'new', item_version: 'v1' } })
    expect(realtimeInputSourceStatus(capture, 'session', 'child')).toBe('source_changed')
    expect(capture.reference.question_reference?.binding).toMatchObject({ prompt_id: 'original' })
  })
  it('does not acquire a later evaluation or borrow another child/session', () => {
    const capture = snapshot()
    setVoicePendingEvaluationCapture('session', 'child', 'new-evaluation')
    expect(realtimeInputSourceStatus(capture, 'session', 'child')).toBe('source_changed')
    expect(realtimeInputSourceStatus(capture, 'other-session', 'child')).toBe('owner_changed')
    expect(realtimeInputSourceStatus(capture, 'session', 'another-child')).toBe('owner_changed')
  })
  it('does not infer an onset from a final-only transcript or missing capture', () => {
    expect(realtimeInputSourceStatus({ ...snapshot(), source: 'unbound' }, 'session', 'child')).toBe('unbound')
    expect(realtimeInputSourceStatus(undefined, 'session', 'child')).toBe('missing')
  })
  it('compares identities independently of object property order', () => {
    setVoiceQuestionCapture('session', 'child', { kind: 'diagnostic', binding: { prompt_id: 'original', item_version: 'v1' } })
    const capture = snapshot()
    capture.reference = { question_reference: { binding: { item_version: 'v1', prompt_id: 'original' }, kind: 'diagnostic' }, child_id: 'child' }
    expect(realtimeInputSourceStatus(capture, 'session', 'child')).toBe('current')
  })
})
