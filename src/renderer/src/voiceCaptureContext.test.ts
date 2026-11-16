import { afterEach, expect, it } from 'vitest'
import { clearVoiceCaptureContext, releaseVoiceQuestionCapture, setVoiceQuestionCapture, setVoiceTaskChoiceCapture, trackTaskChoiceCapture, trackDiagnosticCapture, voiceCaptureReference, voiceQuestionCaptureEpoch,
  publishVoiceDocumentCapture, releaseVoiceDocumentCapture, setVoiceActivityCapture, setVoicePendingEvaluationCapture } from './voiceCaptureContext'
import type { DiagnosticPromptResult, DocumentPageRevision } from './types'

const result = (id: string, child = 'a') => ({ status: 'waiting_answer', prompt: {
  prompt_id: id, item_version: `version-${id}`, child_id: child, origin_session_id: 's',
} }) as DiagnosticPromptResult
afterEach(() => clearVoiceCaptureContext())

const page = (id = 'page-a', child = 'a') => ({ revision_id: id, artifact_id: `artifact-${id}`,
  session_id: 's', child_id: child, content_sha256: 'c'.repeat(64), extracted_text: 'unconfirmed OCR',
  page_source: { version: 'document_page_source_v1', status: 'retained', sha256: 'd'.repeat(64),
    byte_length: 128, media_type: 'image/png', encrypted: true, local_only: true,
    cloud_transmission_allowed: false, retention_reason: 'accepted_document_page_revision' } }) as DocumentPageRevision

it('freezes a fallback document without stealing primary question, activity, evaluation or choice ownership', () => {
  publishVoiceDocumentCapture('s', 'a', page())
  const frozen = voiceCaptureReference('s', 'a')
  publishVoiceDocumentCapture('s', 'a', page('page-b'))
  expect(frozen.question_reference).toMatchObject({ kind: 'document', binding: { revision_id: 'page-a' } })
  expect(voiceCaptureReference('s', 'a').question_reference).toMatchObject({ kind: 'document', binding: { revision_id: 'page-b' } })
  expect(voiceCaptureReference('s', 'b').question_reference).toBeNull()
  setVoiceQuestionCapture('s', 'a', { kind: 'diagnostic', binding: { prompt_id: 'p', item_version: 'v' } })
  const epoch = voiceQuestionCaptureEpoch('s')
  publishVoiceDocumentCapture('s', 'a', page('page-c'))
  expect(voiceQuestionCaptureEpoch('s')).toBe(epoch)
  expect(voiceCaptureReference('s', 'a').question_reference?.kind).toBe('diagnostic')
  setVoiceQuestionCapture('s', 'a', null)
  setVoiceActivityCapture('s', 'a', { activity_id: 'activity', version: 'v', last_turn_id: 'turn' })
  expect(voiceCaptureReference('s', 'a').question_reference).toBeNull()
  setVoiceActivityCapture('s', 'a', null)
  setVoicePendingEvaluationCapture('s', 'a', 'evaluation')
  expect(voiceCaptureReference('s', 'a').question_reference).toBeNull()
  setVoicePendingEvaluationCapture('s', 'a', null)
  setVoiceTaskChoiceCapture('s', 'a', { offer_turn_id: 'offer', source_version: 'v' })
  expect(voiceCaptureReference('s', 'a').question_reference).toBeUndefined()
})

it('releases only the document owner and cannot restore a cleared or foreign page', () => {
  const old = publishVoiceDocumentCapture('s', 'a', page())!
  const current = publishVoiceDocumentCapture('s', 'a', page('page-b'))!
  releaseVoiceDocumentCapture('s', 'a', old)
  expect(voiceCaptureReference('s', 'a').question_reference?.kind).toBe('document')
  releaseVoiceDocumentCapture('s', 'b', current)
  expect(voiceCaptureReference('s', 'a').question_reference?.kind).toBe('document')
  releaseVoiceDocumentCapture('s', 'a', current)
  expect(voiceCaptureReference('s', 'a').question_reference).toBeNull()
  publishVoiceDocumentCapture('s', 'a', page())
  clearVoiceCaptureContext('s')
  releaseVoiceDocumentCapture('s', 'a', old)
  expect(voiceCaptureReference('s', 'a').question_reference).toBeNull()
  expect(publishVoiceDocumentCapture('s', 'a', page('foreign', 'b'))).toBeUndefined()
  expect(publishVoiceDocumentCapture('s', 'a', { ...page(), content_sha256: 'arbitrary' })).toBeUndefined()
  expect(publishVoiceDocumentCapture('s', 'a', { ...page(), page_source: undefined })).toBeUndefined()
})

it('freezes a task offer at capture and isolates children and later choices', () => {
  const reference = { offer_turn_id: 'offer-a', source_version: 'v1' }
  setVoiceTaskChoiceCapture('s', 'a', reference)
  const recorded = voiceCaptureReference('s', 'a')
  reference.source_version = 'mutated'
  setVoiceTaskChoiceCapture('s', 'a', { offer_turn_id: 'offer-b', source_version: 'v2' })
  expect(recorded.task_choice_reference).toEqual({ offer_turn_id: 'offer-a', source_version: 'v1' })
  expect(voiceCaptureReference('s', 'b').task_choice_reference).toBeUndefined()
  setVoiceQuestionCapture('s', 'a', { kind: 'diagnostic', binding: { prompt_id: 'new', item_version: 'v3' } })
  expect(voiceCaptureReference('s', 'a').task_choice_reference).toBeUndefined()
})

it('does not restore a stale task-list load after a newer list or privacy stop', async () => {
  type Result = import('./voiceCaptureContext').TaskStartChoiceResult
  let finish!: (value: Result) => void
  const value: Result = { session_id: 's', child_id: 'a', reference: { offer_turn_id: 'old', source_version: 'v1' } }
  const old = trackTaskChoiceCapture('s', 'a', () => new Promise(resolve => { finish = resolve }))
  setVoiceTaskChoiceCapture('s', 'a', { offer_turn_id: 'new', source_version: 'v2' })
  finish(value); await old
  expect(voiceCaptureReference('s', 'a').task_choice_reference?.offer_turn_id).toBe('new')
  const stopped = trackTaskChoiceCapture('s', 'a', () => new Promise(resolve => { finish = resolve }))
  clearVoiceCaptureContext('s'); finish(value); await stopped
  expect(voiceCaptureReference('s', 'a').task_choice_reference).toBeUndefined()
  await trackTaskChoiceCapture('s', 'a', async () => ({ ...value, child_id: 'b' }))
  expect(voiceCaptureReference('s', 'a').task_choice_reference).toBeUndefined()
})

it('freezes an original reference for a queued segment and isolates the child', async () => {
  await trackDiagnosticCapture('s', async () => result('first'))
  const captured = voiceCaptureReference('s', 'a')
  await trackDiagnosticCapture('s', async () => result('second'))
  expect(captured.question_reference).toEqual({ kind: 'diagnostic', binding: { prompt_id: 'first', item_version: 'version-first' } })
  expect(voiceCaptureReference('s', 'b').question_reference).toBeNull()
  expect(voiceCaptureReference('s', 'a').question_reference).toMatchObject({ binding: { prompt_id: 'second' } })
})

it('does not resurrect a late read after replacement or privacy stop', async () => {
  let finish!: (value: DiagnosticPromptResult) => void
  const pending = trackDiagnosticCapture('s', () => new Promise(resolve => { finish = resolve }))
  await trackDiagnosticCapture('s', async () => result('second'), true)
  finish(result('first')); await pending
  expect(voiceCaptureReference('s', 'a').question_reference).toMatchObject({ binding: { prompt_id: 'second' } })
  const late = trackDiagnosticCapture('s', () => new Promise(resolve => { finish = resolve }))
  clearVoiceCaptureContext('s'); finish(result('second')); await late
  expect(voiceCaptureReference('s', 'a').question_reference).toBeNull()
})

it('freezes each typed source and does not replace an exam with a background diagnostic read', async () => {
  const question = { kind: 'exam' as const, binding: { run_id: 'exam-a', position: 0, item_id: 'item-a', source_version: 'source-a' } }
  setVoiceQuestionCapture('s', 'a', question)
  const original = voiceCaptureReference('s', 'a')
  question.binding.position = 1
  await trackDiagnosticCapture('s', async () => result('unrelated-background-prompt'))
  expect(voiceCaptureReference('s', 'a').question_reference).toEqual(original.question_reference)
  expect(original.question_reference).toMatchObject({ kind: 'exam', binding: { position: 0 } })
  setVoiceQuestionCapture('s', 'a', { kind: 'vocabulary', binding: { prompt_session_id: 'card-a', card_version: 'v1' } })
  expect(original.question_reference?.kind).toBe('exam')
  expect(voiceCaptureReference('s', 'a').question_reference?.kind).toBe('vocabulary')
  setVoiceQuestionCapture('s', 'a', null)
  expect(voiceCaptureReference('s', 'a').question_reference).toBeNull()
})

it('does not keep a cancelled prompt or a replacement whose request failed', async () => {
  await trackDiagnosticCapture('s', async () => result('first'))
  await trackDiagnosticCapture('s', async () => ({ ...result('first'), status: 'cancelled' }))
  expect(voiceCaptureReference('s', 'a').question_reference).toBeNull()
  await trackDiagnosticCapture('s', async () => result('second'))
  await expect(trackDiagnosticCapture('s', async () => { throw new Error('offline') }, true)).rejects.toThrow('offline')
  expect(voiceCaptureReference('s', 'a').question_reference).toBeNull()
})

it('does not steal a visible source when a background diagnostic read cannot replace it', async () => {
  setVoiceQuestionCapture('s', 'a', { kind: 'mistake', binding: { session_id: 's', child_id: 'a',
    mistake_id: 'm', phase: 'attempt', item_id: 'i', source_version: 'v' } })
  const owner = voiceQuestionCaptureEpoch('s')
  await trackDiagnosticCapture('s', async () => result('background'))
  expect(voiceQuestionCaptureEpoch('s')).toBe(owner)
  releaseVoiceQuestionCapture('s', 'a', owner)
  expect(voiceCaptureReference('s', 'a').question_reference).toBeNull()
})

it('does not release a later authoritative owner even when it names the same question', () => {
  const question = { kind: 'diagnostic' as const, binding: { prompt_id: 'p', item_version: 'v' } }
  setVoiceQuestionCapture('s', 'a', question)
  const old = voiceQuestionCaptureEpoch('s')
  setVoiceQuestionCapture('s', 'a', question)
  releaseVoiceQuestionCapture('s', 'a', old)
  expect(voiceCaptureReference('s', 'a').question_reference).toEqual(question)
  releaseVoiceQuestionCapture('s', 'b', voiceQuestionCaptureEpoch('s'))
  expect(voiceCaptureReference('s', 'a').question_reference).toEqual(question)
})

 it('keeps a scored school original as a help-only reference, without answer ownership', async () => {
  const scored = { ...result('scored'), status: 'answered',
    prompt: { ...result('scored').prompt!, diagnostic_item_id: 'item', item: { track: 'school', answer_type: 'text' } },
    attempt: { diagnostic_item_id: 'item', child_id: 'a', session_id: 's' },
  } as DiagnosticPromptResult
  await trackDiagnosticCapture('s', async () => scored)
  expect(voiceCaptureReference('s', 'a').question_reference).toEqual({
    kind: 'diagnostic_help', binding: { prompt_id: 'scored', item_version: 'version-scored' },
  })
  expect(voiceCaptureReference('s', 'b').question_reference).toBeNull()
})
