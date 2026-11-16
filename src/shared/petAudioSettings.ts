import type { PetCatalogSelection, PetCatalogPreview } from './petAudioCatalog'
export const PET_AUDIO_PURPOSE = 'pet_original_audio_formative_v1' as const
export type PetAudioSettingsInput = {
  purpose: typeof PET_AUDIO_PURPOSE
  baseUrl: string
  model: string
  apiKey: string
  enabled?: boolean
  tariffId?: string
}
export type PetCatalogControl = { file: string; state: string }
export type PetAudioRuntimeConfig = PetAudioSettingsInput & { revision: number; actionId: string; catalog?: PetCatalogSelection; control?: PetCatalogControl }
export type PetAudioSettingsCommand = { kind: 'read' } | { kind: 'previewCatalog' } | { kind: 'apply'; expectedRevision: number; actionId: string } | {
  kind: 'save' | 'clear'
  expectedRevision: number
  actionId: string
  config?: PetAudioSettingsInput
  catalogToken?: string
}
export type PetAudioSettingsSummary = {
  purpose: typeof PET_AUDIO_PURPOSE
  revision: number
  actionId: string | null
  configured: boolean
  enabled: boolean
  state: 'missing' | 'awaiting_verified_tariff' | 'ready' | 'tariff_unavailable' | 'catalog_recovery_required'
  tariffId: string
  catalog?: Omit<PetCatalogPreview, 'token'>
  catalogPreview?: PetCatalogPreview
  runtimeApplied?: boolean
  managed?: boolean
  baseUrl: string
  model: string
}
