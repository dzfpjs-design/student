// @vitest-environment node
import { afterEach, expect, it } from 'vitest'
import { nativeVoiceOnsetContext } from './nativeVoiceOnsetContext'
import { clearVoiceCaptureContext, setVoiceLocalRecoveryCapture, setVoicePendingEvaluationCapture,
  setVoiceQuestionCapture, voiceCaptureReference } from './voiceCaptureContext'
afterEach(() => clearVoiceCaptureContext())
it('a fresh native onset starts independently of an old local recovery without upgrading the old original', () => {
  setVoiceLocalRecoveryCapture('session', 'child', true)
  const old = Object.freeze({ source: 'accepted_onset' as const, localInputId: 'old-local-only-input', capturedAt: 1,
    sessionId: 'session', reference: Object.freeze(voiceCaptureReference('session', 'child')), carryoverConfirmationId: null })
  const fresh = nativeVoiceOnsetContext('session', 'child')
  expect(fresh).toEqual({ sessionId: 'session', reference: { child_id: 'child' }, carryoverConfirmationId: null })
  expect(old.reference.local_recovery_only).toBe(true); expect(old.localInputId).toBe('old-local-only-input')
  expect(voiceCaptureReference('session', 'child').local_recovery_only).toBe(true)
})
it('an unresolved old evaluation does not grant scoring to the fresh independent native utterance', () => {
  setVoiceQuestionCapture('session', 'child', { kind: 'diagnostic', binding: { prompt_id: 'old', item_version: 'version' } })
  setVoicePendingEvaluationCapture('session', 'child', 'uncertain-old-evaluation')
  const old = voiceCaptureReference('session', 'child'), fresh = nativeVoiceOnsetContext('session', 'child')
  expect(fresh.reference).toEqual({ child_id: 'child' })
  expect(old.pending_evaluation_turn_id).toBe('uncertain-old-evaluation')
  expect(voiceCaptureReference('session', 'child')).toEqual(old)
})
it('ordinary current question provenance is preserved for a separately validated explicit scoring tool and never crosses children', () => {
  const question = { kind: 'diagnostic' as const, binding: { prompt_id: 'current', item_version: 'version' } }
  setVoiceQuestionCapture('session', 'child', question)
  expect(nativeVoiceOnsetContext('session', 'child').reference.question_reference).toEqual(question)
  expect(nativeVoiceOnsetContext('session', 'other-child').reference.question_reference).toBeNull()
})
