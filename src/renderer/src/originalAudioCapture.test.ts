import { afterEach, expect, it } from 'vitest'
import type { DiagnosticPromptResult } from './types'
import { clearVoiceCaptureContext, originalAudioCaptureRequired, setVoiceActivityCapture,
  setVoiceLocalRecoveryCapture, setVoicePendingEvaluationCapture, setVoiceQuestionCapture,
  subscribeVoiceCaptureContext, trackDiagnosticCapture } from './voiceCaptureContext'
import { publishOriginalAudioCaptureHint } from './voiceCaptureContext'

const oral = (id = 'p', type = 'speaking') => ({ status: 'waiting_answer', prompt: {
  prompt_id: id, item_version: `v-${id}`, child_id: 'a', origin_session_id: 's',
  item: { track: 'pet', answer_type: type, assessment_mode: 'pet_reference_rubric' },
} }) as DiagnosticPromptResult
afterEach(() => clearVoiceCaptureContext())

it('automatically requests original capture only for the exact current oral prompt', async () => {
  await trackDiagnosticCapture('s', async () => oral())
  expect(originalAudioCaptureRequired('s', 'a')).toBe(true)
  expect(originalAudioCaptureRequired('s', 'b')).toBe(false)
  setVoiceQuestionCapture('s', 'a', { kind: 'diagnostic', binding: { prompt_id: 'p', item_version: 'v-p' } })
  expect(originalAudioCaptureRequired('s', 'a')).toBe(true)
  setVoiceQuestionCapture('s', 'a', { kind: 'diagnostic', binding: { prompt_id: 'p', item_version: 'changed' } })
  expect(originalAudioCaptureRequired('s', 'a')).toBe(false)
})

it('allows only an explicitly replacing Runtime task result to take capture ownership from another question', async () => {
  setVoiceQuestionCapture('s', 'a', { kind: 'exam', binding: { run_id: 'r', position: 0, item_id: 'i', source_version: 'v' } })
  await trackDiagnosticCapture('s', async () => oral())
  expect(originalAudioCaptureRequired('s', 'a')).toBe(false)
  await trackDiagnosticCapture('s', async () => oral(), true)
  expect(originalAudioCaptureRequired('s', 'a')).toBe(true)
})

it('a Runtime response can add a hint only after its exact foreground ownership has been established', () => {
  publishOriginalAudioCaptureHint('s', oral())
  expect(originalAudioCaptureRequired('s', 'a')).toBe(false)
  setVoiceQuestionCapture('s', 'a', { kind: 'diagnostic', binding: { prompt_id: 'p', item_version: 'v-p' } })
  publishOriginalAudioCaptureHint('s', oral())
  expect(originalAudioCaptureRequired('s', 'a')).toBe(true)
  publishOriginalAudioCaptureHint('s', oral('other'))
  expect(originalAudioCaptureRequired('s', 'a')).toBe(false)
})

it('never turns objective, saved, answered, stale or foreign prompts into an original recorder', async () => {
  for (const changed of [oral('p', 'single_choice'), { ...oral(), status: 'answer_saved' },
    { ...oral(), status: 'answered' }, { ...oral(), reason: 'source_stale' },
    { ...oral(), status: 'awaiting_task_start' },
    { ...oral(), prompt: { ...oral().prompt!, origin_session_id: 'other' } }]) {
    await trackDiagnosticCapture('s', async () => changed as DiagnosticPromptResult)
    expect(originalAudioCaptureRequired('s', 'a')).toBe(false)
  }
})

it('gives activity, evaluation and protected recovery precedence and re-arms only a still-current prompt', async () => {
  await trackDiagnosticCapture('s', async () => oral())
  setVoiceActivityCapture('s', 'a', { activity_id: 'play', version: 'v', last_turn_id: 't' })
  expect(originalAudioCaptureRequired('s', 'a')).toBe(false)
  setVoiceActivityCapture('s', 'a', null)
  setVoicePendingEvaluationCapture('s', 'a', 'evaluation')
  expect(originalAudioCaptureRequired('s', 'a')).toBe(false)
  setVoicePendingEvaluationCapture('s', 'a', null)
  expect(originalAudioCaptureRequired('s', 'a')).toBe(true)
  setVoiceLocalRecoveryCapture('s', 'a', true)
  expect(originalAudioCaptureRequired('s', 'a')).toBe(false)
  clearVoiceCaptureContext('s')
  setVoiceLocalRecoveryCapture('s', 'a', false)
  expect(originalAudioCaptureRequired('s', 'a')).toBe(false)
})

it('notifies the actual mounted owner and does not resurrect a cancelled asynchronous load', async () => {
  const observed: boolean[] = []
  const unsubscribe = subscribeVoiceCaptureContext(() => observed.push(originalAudioCaptureRequired('s', 'a')))
  await trackDiagnosticCapture('s', async () => oral())
  expect(observed).toContain(true)
  let finish!: (value: DiagnosticPromptResult) => void
  const old = trackDiagnosticCapture('s', () => new Promise(resolve => { finish = resolve }))
  clearVoiceCaptureContext('s')
  finish(oral('old')); await old
  expect(originalAudioCaptureRequired('s', 'a')).toBe(false)
  expect(observed.at(-1)).toBe(false)
  unsubscribe()
})
