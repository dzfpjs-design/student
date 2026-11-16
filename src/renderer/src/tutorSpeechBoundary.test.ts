import { expect, it } from 'vitest'
import { tutorRequiresLocalSpeech } from './tutorSpeechBoundary'
import type { TutorTurn } from './types'
it.each([{ local_only: true }, { cloud_transmission_allowed: false }, { result: { local_visual: {} } },
  { result: { local_only: true } }, { result: { cloud_transmission_allowed: false } }, { result: { local_visual: 'malformed' } }])('never sends local source narration through cloud speech %#', envelope => {
  expect(tutorRequiresLocalSpeech({ output_envelope: envelope } as unknown as TutorTurn)).toBe(true)
})
it('does not create a new restriction on ordinary authorized tutor output', () => {
  expect(tutorRequiresLocalSpeech()).toBe(false)
  expect(tutorRequiresLocalSpeech({ output_envelope: { result: { blackboard: {} } } } as unknown as TutorTurn)).toBe(false)
})
