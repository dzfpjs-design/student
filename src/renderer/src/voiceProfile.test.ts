import { afterEach, describe, expect, it } from 'vitest'
import { deleteLocalVoiceProfile, hydrateLocalVoiceProfileFromService, isLocalVoiceProfileReady, localVoiceProfile, verifyLocalVoiceProfile } from './voiceProfile'

function wav(): Uint8Array {
  const samples = 16_000; const bytes = new Uint8Array(44 + samples * 2); const view = new DataView(bytes.buffer)
  for (const [offset, text] of [[0, 'RIFF'], [8, 'WAVE'], [12, 'fmt '], [36, 'data']] as const) for (let index = 0; index < text.length; index += 1) view.setUint8(offset + index, text.charCodeAt(index))
  view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, 16_000, true); view.setUint16(34, 16, true)
  for (let index = 0; index < samples; index += 1) view.setInt16(44 + index * 2, Math.sin(index / 20) * 8000, true)
  return bytes
}

const serviceProfile = (childId: string) => ({ version: 3, childId, sampleCount: 48_000, calibrationCount: 3, energy: .12, zeroCrossingRate: .08, spectralBands: [.2, .3, .3, .2], environmentSpread: .1 })

describe('service voice-profile hydration', () => {
  afterEach(() => localStorage.clear())

  it('recovers an empty renderer store from the validated service profile and keeps children isolated', () => {
    expect(localVoiceProfile('child-a')).toBeNull()
    expect(hydrateLocalVoiceProfileFromService('child-a', serviceProfile('child-a'))).not.toBeNull()
    expect(isLocalVoiceProfileReady(localVoiceProfile('child-a'))).toBe(true)
    expect(hydrateLocalVoiceProfileFromService('child-b', serviceProfile('child-b'))).not.toBeNull()
    expect(localVoiceProfile('child-a')?.childId).toBe('child-a')
    expect(localVoiceProfile('child-b')?.childId).toBe('child-b')
  })

  it('never verifies after the recovered profile is deleted', () => {
    hydrateLocalVoiceProfileFromService('child-a', serviceProfile('child-a'))
    deleteLocalVoiceProfile('child-a')
    expect(verifyLocalVoiceProfile('child-a', wav()).status).toBe('not_enrolled')
  })
})
