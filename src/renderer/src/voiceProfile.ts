/**
 * Local-only, deliberately low-stakes capture calibration. This is not a
 * biometric identity system: it only helps the UI decide whether hands-free
 * speech should wait for a manual confirmation. No WAV, embedding, or
 * reversible recording leaves browser memory.
 */
export type VoiceProfile = {
  version: 2
  childId: string
  consentedAt: string
  updatedAt: string
  sampleCount: number
  calibrationCount: number
  energy: number
  zeroCrossingRate: number
  spectralBands: number[]
  /** Aggregate variation across local calibration conditions. */
  environmentSpread: number
}
export type VoiceVerification = { status: 'not_enrolled' | 'match' | 'uncertain'; confidence: number; message: string }

type VoiceFeatures = Pick<VoiceProfile, 'energy' | 'zeroCrossingRate' | 'spectralBands'>
const key = (childId: string): string => `hsc.voice-profile.v2.${childId}`
const legacyKey = (childId: string): string => `hsc.voice-profile.v1.${childId}`
const MIN_CALIBRATION_SAMPLES = 3

function samplesFromWav(bytes: Uint8Array): Float32Array {
  if (bytes.length < 44) throw new Error('VOICE_CALIBRATION_AUDIO_INVALID')
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  if (String.fromCharCode(...bytes.slice(0, 4)) !== 'RIFF' || view.getUint16(22, true) !== 1 || view.getUint16(34, true) !== 16) throw new Error('VOICE_CALIBRATION_AUDIO_INVALID')
  const samples = new Float32Array(Math.floor((bytes.length - 44) / 2))
  for (let index = 0; index < samples.length; index += 1) samples[index] = view.getInt16(44 + index * 2, true) / 32768
  return samples
}

function features(samples: Float32Array): VoiceFeatures {
  const stride = Math.max(1, Math.floor(samples.length / 8000))
  let energy = 0; let zeroCrossings = 0; let count = 0; let previous = samples[0] ?? 0
  const bands = [0, 0, 0, 0]
  for (let index = 0; index < samples.length; index += stride) {
    const value = samples[index]; energy += value * value; if ((value >= 0) !== (previous >= 0)) zeroCrossings += 1
    const delta = Math.abs(value - previous); bands[Math.min(3, Math.floor(Math.min(0.999, delta * 4) * 4))] += Math.abs(value)
    previous = value; count += 1
  }
  const total = bands.reduce((sum, value) => sum + value, 0) || 1
  return { energy: Math.sqrt(energy / Math.max(count, 1)), zeroCrossingRate: zeroCrossings / Math.max(count, 1), spectralBands: bands.map((value) => value / total) }
}

function featureDistance(first: VoiceFeatures, second: VoiceFeatures): number {
  const bands = first.spectralBands.reduce((sum, value, index) => sum + Math.abs(value - second.spectralBands[index]), 0)
  return Math.min(1, Math.abs(first.energy - second.energy) / Math.max(first.energy, 0.02) * 0.35 + Math.abs(first.zeroCrossingRate - second.zeroCrossingRate) / 0.12 * 0.25 + bands / 2 * 0.4)
}

function profileFromStored(childId: string, raw: unknown): VoiceProfile | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as Partial<Omit<VoiceProfile, 'version'>> & { version?: number }
  if (value.childId !== childId || !Array.isArray(value.spectralBands) || value.spectralBands.length !== 4 || !value.spectralBands.every(Number.isFinite)) return null
  if (value.version === 2 && typeof value.calibrationCount === 'number' && typeof value.environmentSpread === 'number') return value as VoiceProfile
  if (value.version === 1 && typeof value.sampleCount === 'number' && typeof value.energy === 'number' && typeof value.zeroCrossingRate === 'number') {
    return { version: 2, childId, consentedAt: String(value.consentedAt ?? ''), updatedAt: String(value.updatedAt ?? ''), sampleCount: value.sampleCount, calibrationCount: 1, energy: value.energy, zeroCrossingRate: value.zeroCrossingRate, spectralBands: value.spectralBands, environmentSpread: 0 }
  }
  return null
}

export function localVoiceProfile(childId: string): VoiceProfile | null {
  try {
    const current = localStorage.getItem(key(childId))
    if (current) return profileFromStored(childId, JSON.parse(current))
    const legacy = localStorage.getItem(legacyKey(childId))
    return legacy ? profileFromStored(childId, JSON.parse(legacy)) : null
  } catch { return null }
}

function writeProfile(profile: VoiceProfile): VoiceProfile {
  localStorage.setItem(key(profile.childId), JSON.stringify(profile))
  localStorage.removeItem(legacyKey(profile.childId))
  return profile
}

/**
 * Service records use a separately versioned payload.  Convert the small,
 * validated feature subset explicitly instead of pretending a v3 record is a
 * renderer v2 profile.  This is the sole recovery path after browser storage
 * is cleared: still no audio or reversible embedding is written locally.
 */
export function hydrateLocalVoiceProfileFromService(childId: string, raw: unknown, timestamps?: { consentedAt?: unknown; updatedAt?: unknown }): VoiceProfile | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as Record<string, unknown>
  const number = (name: string): number | null => typeof value[name] === 'number' && Number.isFinite(value[name]) ? value[name] as number : null
  const bands = value.spectralBands
  const sampleCount = number('sampleCount'); const energy = number('energy'); const zeroCrossingRate = number('zeroCrossingRate'); const environmentSpread = number('environmentSpread')
  if (value.version !== 3 || value.childId !== childId || value.calibrationCount !== 3 || sampleCount === null || sampleCount < 16_000 || energy === null || energy <= 0 || energy > 1 || zeroCrossingRate === null || zeroCrossingRate < 0 || zeroCrossingRate > 1 || environmentSpread === null || environmentSpread < 0 || environmentSpread > 1 || !Array.isArray(bands) || bands.length !== 4 || !bands.every((item) => typeof item === 'number' && Number.isFinite(item) && item >= 0 && item <= 1) || Math.abs(bands.reduce((sum, item) => sum + item, 0) - 1) > .05) return null
  const consentedAt = typeof timestamps?.consentedAt === 'string' ? timestamps.consentedAt : new Date().toISOString()
  const updatedAt = typeof timestamps?.updatedAt === 'string' ? timestamps.updatedAt : consentedAt
  return writeProfile({ version: 2, childId, consentedAt, updatedAt, sampleCount, calibrationCount: 3, energy, zeroCrossingRate, spectralBands: [...bands], environmentSpread })
}

/** Adds one consented calibration clip. Only derived aggregate features are kept. */
export function addLocalVoiceCalibrationSample(childId: string, wav: Uint8Array): VoiceProfile {
  const samples = samplesFromWav(wav)
  if (samples.length < 16_000) throw new Error('VOICE_CALIBRATION_TOO_SHORT')
  const current = localVoiceProfile(childId)
  const next = features(samples)
  const now = new Date().toISOString()
  if (!current || current.calibrationCount >= MIN_CALIBRATION_SAMPLES) {
    return writeProfile({ version: 2, childId, consentedAt: now, updatedAt: now, sampleCount: samples.length, calibrationCount: 1, ...next, environmentSpread: 0 })
  }
  const weight = current.calibrationCount
  const merged: VoiceFeatures = {
    energy: (current.energy * weight + next.energy) / (weight + 1),
    zeroCrossingRate: (current.zeroCrossingRate * weight + next.zeroCrossingRate) / (weight + 1),
    spectralBands: current.spectralBands.map((value, index) => (value * weight + next.spectralBands[index]) / (weight + 1)),
  }
  return writeProfile({ ...current, updatedAt: now, sampleCount: current.sampleCount + samples.length, calibrationCount: weight + 1, ...merged, environmentSpread: Math.max(current.environmentSpread, featureDistance(merged, next)) })
}

/** Compatibility helper for existing callers/tests; production UI uses three clips. */
export function enrollLocalVoiceProfile(childId: string, wav: Uint8Array): VoiceProfile {
  let profile = addLocalVoiceCalibrationSample(childId, wav)
  const firstSampleCount = profile.sampleCount
  while (profile.calibrationCount < MIN_CALIBRATION_SAMPLES) profile = addLocalVoiceCalibrationSample(childId, wav)
  return writeProfile({ ...profile, sampleCount: firstSampleCount })
}

export function deleteLocalVoiceProfile(childId: string): void { localStorage.removeItem(key(childId)); localStorage.removeItem(legacyKey(childId)) }
export function isLocalVoiceProfileReady(profile: VoiceProfile | null): boolean { return Boolean(profile && profile.calibrationCount >= MIN_CALIBRATION_SAMPLES) }

export function verifyLocalVoiceProfile(childId: string, wav: Uint8Array): VoiceVerification {
  const profile = localVoiceProfile(childId)
  if (!profile || !isLocalVoiceProfileReady(profile)) return { status: 'not_enrolled', confidence: 0, message: '尚未完成三段本机语音校准。' }
  const current = features(samplesFromWav(wav))
  const distance = featureDistance(profile, current)
  const tolerance = 0.38 + Math.min(0.14, profile.environmentSpread * 0.5)
  const confidence = Math.max(0, Math.min(1, 1 - distance))
  return distance <= tolerance
    ? { status: 'match', confidence, message: '本机声音与已校准的环境特征相近。' }
    : { status: 'uncertain', confidence, message: '声音或环境特征不确定；请确认正在说话的是本人，或改用文字。' }
}
