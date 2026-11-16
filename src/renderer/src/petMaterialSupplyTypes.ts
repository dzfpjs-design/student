import type { PetMedia } from './petMedia'

/** Supplied by main from real persisted typed prompts, never guessed from a plan. */
export type ParentPetPrompt = {
  child_id: string; origin_session_id: string; prompt_id: string; item_version: string
  diagnostic_item_id: string; status: 'waiting_answer' | 'answer_saved' | 'answered' | 'cancelled' | 'invalidated'
  created_at: string
  item: { track: 'pet' | 'school'; prompt: string; stimulus_text?: string | null; pet_audio_modality: 'listening' | 'speaking' | null }
}
export type ParentPetMaterial = {
  artifact_id: string; status: string; prompt_id: string; item_version: string; diagnostic_item_id: string
  origin: 'local_synthesized_practice'; original_recording_verified: false
  parent_review_required: true; engine?: string | null; requirement_version?: string | null
  local_only: true; cloud_transmission_allowed: false
}
export type ParentPetMedia = PetMedia & { artifact_id: string | null }
export type ParentPetCollection = {
  child_id: string; session_id: string; prompt_id: string; item_version: string
  listening: ParentPetMedia[]; speaking: ParentPetMedia[]; materials: ParentPetMaterial[]
  local_only: true; cloud_transmission_allowed: false
}
/** Main must use the existing local+parent tokens and whitelist PET paths. */
export type ParentPetRequest = <T>(path: string, init?: RequestInit) => Promise<T>
export type ParentPetPreviewRequest = (sessionId: string, mediaId: string, sha256: string, signal: AbortSignal) => Promise<ArrayBuffer>
export type ParentPetMaterialsPanelProps = {
  childId: string; prompts: ParentPetPrompt[]; request: ParentPetRequest
  requestPreviewAudio: ParentPetPreviewRequest; onRefreshPrompts?: () => void
  loading?: boolean; error?: string | null
}

export type ParentPetPending = { client_action_id: string } & (
  { kind: 'materialize' } | { kind: 'bind'; artifact_id: string } |
  { kind: 'review'; media_id: string; audio_sha256: string; operation: 'approve' | 'retract'; full_spoken_reference?: true }
)

export const parentPetPendingPrefix = (childId: string): string => `hsc-parent-pet-material:${encodeURIComponent(childId)}:`
export function clearParentPetMaterialPending(childId: string): void {
  Object.keys(localStorage).filter(key => key.startsWith(parentPetPendingPrefix(childId))).forEach(key => localStorage.removeItem(key))
}
