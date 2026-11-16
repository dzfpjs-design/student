import type { VoiceCalibrationMicrophone } from './api'
import type { VoiceProfile } from './voiceProfile'

export type PendingVoiceCalibrationSave = {
  childId: string
  profile: VoiceProfile
  vocabulary: string[]
  microphone: VoiceCalibrationMicrophone
  clientActionId: string
  payloadHash: string
  createdAt: string
}

const ttlMs = 24 * 60 * 60 * 1000
const keyFor = (childId: string): string => `hsc.voice-calibration.pending-save.v1.${childId}`
const deleteKeyFor = (childId: string): string => `hsc.voice-calibration.pending-delete.v1.${childId}`

function normalizeVocabulary(words: string[]): string[] {
  return [...new Set(words.map((word) => word.trim()).filter(Boolean))]
}

async function payloadHash(childId: string, profile: VoiceProfile, vocabulary: string[], microphone: VoiceCalibrationMicrophone): Promise<string> {
  const payload = JSON.stringify({ childId, profile, vocabulary: normalizeVocabulary(vocabulary), microphone })
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(payload))
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

function valid(value: unknown, childId: string): value is PendingVoiceCalibrationSave {
  if (!value || typeof value !== 'object') return false
  const record = value as Partial<PendingVoiceCalibrationSave>
  const createdAt = typeof record.createdAt === 'string' ? Date.parse(record.createdAt) : Number.NaN
  return record.childId === childId && Number.isFinite(createdAt) && Date.now() - createdAt <= ttlMs && Date.now() >= createdAt - 5 * 60 * 1000
    && typeof record.clientActionId === 'string' && record.clientActionId.length >= 8
    && typeof record.payloadHash === 'string' && /^[a-f0-9]{64}$/.test(record.payloadHash)
    && Boolean(record.profile) && Array.isArray(record.vocabulary)
    && Boolean(record.microphone) && typeof record.microphone?.system_id_hash === 'string'
}

/** Stores only aggregate features and retry metadata; audio buffers never enter localStorage. */
export function loadPendingVoiceCalibrationSave(childId: string): PendingVoiceCalibrationSave | null {
  try {
    const raw = localStorage.getItem(keyFor(childId))
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (valid(parsed, childId)) return parsed
  } catch { /* clear an unreadable pending record below */ }
  localStorage.removeItem(keyFor(childId))
  return null
}

export async function preparePendingVoiceCalibrationSave(
  childId: string, profile: VoiceProfile, vocabulary: string[], microphone: VoiceCalibrationMicrophone
): Promise<PendingVoiceCalibrationSave> {
  const cleanedVocabulary = normalizeVocabulary(vocabulary)
  const hash = await payloadHash(childId, profile, cleanedVocabulary, microphone)
  const existing = loadPendingVoiceCalibrationSave(childId)
  const record: PendingVoiceCalibrationSave = existing?.payloadHash === hash
    ? existing
    : { childId, profile, vocabulary: cleanedVocabulary, microphone, clientActionId: crypto.randomUUID(), payloadHash: hash, createdAt: new Date().toISOString() }
  localStorage.setItem(keyFor(childId), JSON.stringify(record))
  return record
}

export function clearPendingVoiceCalibrationSave(childId: string): void {
  localStorage.removeItem(keyFor(childId))
}

export type PendingVoiceCalibrationDelete = { childId: string; clientActionId: string; createdAt: string }

export function loadPendingVoiceCalibrationDelete(childId: string): PendingVoiceCalibrationDelete | null {
  try {
    const record: unknown = JSON.parse(localStorage.getItem(deleteKeyFor(childId)) ?? 'null')
    if (record && typeof record === 'object') {
      const value = record as Partial<PendingVoiceCalibrationDelete>
      const createdAt = typeof value.createdAt === 'string' ? Date.parse(value.createdAt) : Number.NaN
      if (value.childId === childId && typeof value.clientActionId === 'string' && value.clientActionId.length >= 8 && Number.isFinite(createdAt) && Date.now() - createdAt <= ttlMs) return value as PendingVoiceCalibrationDelete
    }
  } catch { /* clear invalid record below */ }
  localStorage.removeItem(deleteKeyFor(childId))
  return null
}

export function preparePendingVoiceCalibrationDelete(childId: string): PendingVoiceCalibrationDelete {
  const existing = loadPendingVoiceCalibrationDelete(childId)
  const record = existing ?? { childId, clientActionId: crypto.randomUUID(), createdAt: new Date().toISOString() }
  localStorage.setItem(deleteKeyFor(childId), JSON.stringify(record))
  return record
}

export function clearPendingVoiceCalibrationDelete(childId: string): void {
  localStorage.removeItem(deleteKeyFor(childId))
}
