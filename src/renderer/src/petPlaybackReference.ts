import type { PetMedia } from './petMedia'
export type PetPcmControlReference = {
  version: 'pet_full_audio_control_reference_v1'; child_id: string; session_id: string
  media_id: string; prompt_id: string; item_version: string; diagnostic_item_id: string
  source_version: string; audio_sha256: string; source_review_event_id: string; spoken_text_sha256: string
  generation: number; local_only: true; cloud_transmission_allowed: false
}
const keys = ['version','child_id','session_id','media_id','prompt_id','item_version','diagnostic_item_id','source_version',
  'audio_sha256','source_review_event_id','spoken_text_sha256','generation','local_only','cloud_transmission_allowed']
export function isPetPcmControlReference(value: unknown): value is PetPcmControlReference {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const ref = value as Record<string, unknown>
  return Object.keys(ref).length === keys.length && keys.every(key => Object.prototype.hasOwnProperty.call(ref, key))
    && ref.version === 'pet_full_audio_control_reference_v1' && ref.local_only === true && ref.cloud_transmission_allowed === false
    && Number.isInteger(ref.generation) && Number(ref.generation) >= 1
    && ['child_id','session_id','media_id','prompt_id','item_version','diagnostic_item_id','source_version','source_review_event_id']
      .every(key => typeof ref[key] === 'string' && String(ref[key]).length > 0 && String(ref[key]).length <= 200)
    && ['audio_sha256','spoken_text_sha256'].every(key => typeof ref[key] === 'string' && /^[a-f0-9]{64}$/.test(String(ref[key])))
}
export function referenceMatchesMedia(ref: unknown, media: PetMedia): ref is PetPcmControlReference {
  return isPetPcmControlReference(ref) && ['child_id','session_id','media_id','prompt_id','item_version','diagnostic_item_id','source_version','generation']
    .every(key => ref[key as keyof PetPcmControlReference] === media[key as keyof PetMedia])
    && ref.audio_sha256 === media.audio.sha256
}
export function samePcmReference(a: PetPcmControlReference, b: unknown): boolean {
  return isPetPcmControlReference(b) && keys.every(key => a[key as keyof PetPcmControlReference] === b[key as keyof PetPcmControlReference])
}
