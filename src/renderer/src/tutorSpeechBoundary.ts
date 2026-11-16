import type { TutorTurn } from './types'

/** Local source restrictions also apply to derived narration and saved retries. */
export function tutorRequiresLocalSpeech(turn?: TutorTurn | null): boolean {
  const envelope = turn?.output_envelope as Record<string, unknown> | null | undefined
  const result = envelope?.result as Record<string, unknown> | null | undefined
  return envelope?.local_only === true || envelope?.cloud_transmission_allowed === false
    || result?.local_only === true || result?.cloud_transmission_allowed === false || result?.local_visual != null
}
