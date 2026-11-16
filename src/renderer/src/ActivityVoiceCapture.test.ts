import { afterEach, expect, it } from 'vitest'
import { clearVoiceCaptureContext, setVoiceActivityCapture, setVoiceQuestionCapture, setVoicePendingEvaluationCapture, trackDiagnosticCapture, voiceCaptureReference } from './voiceCaptureContext'
import type { DiagnosticPromptResult } from './types'
import { createActivityFocusReader, type ActivityVoiceFocus } from './activityVoiceFocus'

const focus = { activity_id: 'activity-a', version: 'version-a', last_turn_id: 'turn-a' }
const question = { kind: 'diagnostic' as const, binding: { prompt_id: 'prompt-a', item_version: 'item-a' } }
afterEach(() => clearVoiceCaptureContext())

it('freezes activity and original question independently, preserving the recorded version after focus advances', () => {
  setVoiceQuestionCapture('session-a', 'child-a', question)
  setVoiceActivityCapture('session-a', 'child-a', focus)
  const recorded = voiceCaptureReference('session-a', 'child-a')
  setVoiceActivityCapture('session-a', 'child-a', { ...focus, version: 'version-b', last_turn_id: 'turn-b' })
  expect(recorded).toEqual({ child_id: 'child-a', question_reference: question, activity_reference: focus })
  setVoiceActivityCapture('session-a', 'child-a', null)
  expect(voiceCaptureReference('session-a', 'child-a')).toEqual({ child_id: 'child-a', question_reference: question })
  expect(recorded.activity_reference).toEqual(focus)
})

it('does not expose another child focus and clearing a question does not clear the activity', () => {
  setVoiceQuestionCapture('session-a', 'child-a', question)
  setVoiceActivityCapture('session-a', 'child-a', focus)
  expect(voiceCaptureReference('session-a', 'child-b')).toEqual({ child_id: 'child-b', question_reference: null })
  setVoiceQuestionCapture('session-a', 'child-a', null)
  expect(voiceCaptureReference('session-a', 'child-a').activity_reference).toEqual(focus)
  clearVoiceCaptureContext('session-a')
  expect(voiceCaptureReference('session-a', 'child-a').activity_reference).toBeUndefined()
})

it('diagnostic reads and failed replacement never erase the active activity reference', async () => {
  setVoiceQuestionCapture('session-a', 'child-a', question)
  setVoiceActivityCapture('session-a', 'child-a', focus)
  const result = { status: 'waiting_answer', prompt: { origin_session_id: 'session-a', child_id: 'child-a',
    prompt_id: 'prompt-b', item_version: 'item-b' } } as DiagnosticPromptResult
  await trackDiagnosticCapture('session-a', async () => result)
  expect(voiceCaptureReference('session-a', 'child-a').activity_reference).toEqual(focus)
  await expect(trackDiagnosticCapture('session-a', async () => { throw Error('failed') }, true)).rejects.toThrow('failed')
  expect(voiceCaptureReference('session-a', 'child-a').activity_reference).toEqual(focus)
})

it('rejects late and cross-child focus reads, and null current focus clears only activity', async () => {
  const reader = createActivityFocusReader()
  setVoiceQuestionCapture('session-a', 'child-a', question)
  let resolve!: (value: ActivityVoiceFocus) => void
  const envelope: ActivityVoiceFocus = { session_id: 'session-a', child_id: 'child-a', activity_focus: focus,
    local_only: true, cloud_transmission_allowed: false }
  const old = reader.refresh('session-a', 'child-a', () => new Promise(done => { resolve = done }), () => true)
  await reader.refresh('session-a', 'child-a', async () => ({ ...envelope, activity_focus: null }), () => true)
  resolve(envelope)
  expect(await old).toBe(false)
  expect(await reader.refresh('session-a', 'child-a', async () => ({ ...envelope, child_id: 'child-b' }), () => true)).toBe(false)
  expect(voiceCaptureReference('session-a', 'child-a')).toEqual({ child_id: 'child-a', question_reference: question })
  expect(await reader.refresh('session-a', 'child-a', async () => envelope, () => true)).toBe(true)
  const invalid = reader.refresh('session-a', 'child-a', () => new Promise(done => { resolve = done }), () => true)
  reader.invalidate()
  resolve({ ...envelope, activity_focus: null })
  expect(await invalid).toBe(false)
  expect(voiceCaptureReference('session-a', 'child-a').activity_reference).toEqual(focus)
})

it.each(['activity_first', 'question_first'] as const)('preserves concurrent question and activity recovery: %s', async order => {
  const reader = createActivityFocusReader()
  let questionReady!: (value: DiagnosticPromptResult) => void
  let activityReady!: (value: ActivityVoiceFocus) => void
  const item = { status: 'waiting_answer', prompt: { origin_session_id: 'session-a', child_id: 'child-a',
    prompt_id: 'prompt-a', item_version: 'item-a' } } as DiagnosticPromptResult
  const restoringQuestion = trackDiagnosticCapture('session-a', () => new Promise(done => { questionReady = done }))
  const restoringActivity = reader.refresh('session-a', 'child-a', () => new Promise(done => { activityReady = done }), () => true)
  const payload: ActivityVoiceFocus = { child_id: 'child-a', session_id: 'session-a', activity_focus: focus,
    local_only: true, cloud_transmission_allowed: false }
  if (order === 'activity_first') { activityReady(payload); await restoringActivity; questionReady(item) }
  else { questionReady(item); await restoringQuestion; activityReady(payload) }
  await Promise.all([restoringQuestion, restoringActivity])
  expect(voiceCaptureReference('session-a', 'child-a')).toEqual({ child_id: 'child-a', question_reference: question, activity_reference: focus })
})

it('does not revive activity after a privacy clear while its read is pending', async () => {
  const reader = createActivityFocusReader()
  let resolve!: (value: ActivityVoiceFocus) => void
  const pending = reader.refresh('session-a', 'child-a', () => new Promise(done => { resolve = done }), () => true)
  clearVoiceCaptureContext('session-a')
  resolve({ child_id: 'child-a', session_id: 'session-a', activity_focus: focus, local_only: true, cloud_transmission_allowed: false })
  expect(await pending).toBe(false)
  expect(voiceCaptureReference('session-a', 'child-a').activity_reference).toBeUndefined()
})

it.each(['activity_first', 'question_first'] as const)('keeps pending changes independent of concurrent focus restores: %s', async order => {
  const reader = createActivityFocusReader()
  let questionReady!: (value: DiagnosticPromptResult) => void
  let activityReady!: (value: ActivityVoiceFocus) => void
  const restoringQuestion = trackDiagnosticCapture('session-a', () => new Promise(done => { questionReady = done }))
  const restoringActivity = reader.refresh('session-a', 'child-a', () => new Promise(done => { activityReady = done }), () => true)
  const item = { status: 'waiting_answer', prompt: { origin_session_id: 'session-a', child_id: 'child-a',
    prompt_id: 'prompt-a', item_version: 'item-a' } } as DiagnosticPromptResult
  const payload: ActivityVoiceFocus = { session_id: 'session-a', child_id: 'child-a', activity_focus: focus,
    local_only: true, cloud_transmission_allowed: false }
  setVoicePendingEvaluationCapture('session-a', 'child-a', 'evaluation-a')
  if (order === 'activity_first') { activityReady(payload); await restoringActivity }
  else { questionReady(item); await restoringQuestion }
  setVoicePendingEvaluationCapture('session-a', 'child-a', 'evaluation-b')
  if (order === 'activity_first') questionReady(item)
  else activityReady(payload)
  await Promise.all([restoringQuestion, restoringActivity])
  const recorded = voiceCaptureReference('session-a', 'child-a')
  expect(recorded).toEqual({ child_id: 'child-a', question_reference: question, activity_reference: focus,
    pending_evaluation_turn_id: 'evaluation-b' })
  expect(voiceCaptureReference('session-a', 'child-b').pending_evaluation_turn_id).toBeUndefined()
  setVoicePendingEvaluationCapture('session-a', 'child-a', null)
  expect(voiceCaptureReference('session-a', 'child-a')).toEqual({ child_id: 'child-a', question_reference: question, activity_reference: focus })
  expect(recorded.pending_evaluation_turn_id).toBe('evaluation-b')
})
