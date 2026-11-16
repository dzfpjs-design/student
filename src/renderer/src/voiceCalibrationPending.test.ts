import { beforeEach, describe, expect, it } from 'vitest'
import type { VoiceCalibrationMicrophone } from './api'
import { clearPendingVoiceCalibrationDelete, clearPendingVoiceCalibrationSave, loadPendingVoiceCalibrationDelete, loadPendingVoiceCalibrationSave, preparePendingVoiceCalibrationDelete, preparePendingVoiceCalibrationSave } from './voiceCalibrationPending'
import type { VoiceProfile } from './voiceProfile'

const profile = (childId: string): VoiceProfile => ({
  version: 2, childId, consentedAt: '2026-08-28T10:00:00.000Z', updatedAt: '2026-08-28T10:00:00.000Z',
  sampleCount: 96_000, calibrationCount: 3, energy: .12, zeroCrossingRate: .08,
  spectralBands: [.2, .3, .3, .2], environmentSpread: .1,
})
const microphone: VoiceCalibrationMicrophone = {
  system_id_hash: 'a'.repeat(64), label: 'USB Microphone',
  capabilities: { transport: 'browser-media-device', echo_cancellation_requested: true, noise_suppression_requested: true, auto_gain_control_requested: true },
}

describe('pending local voice calibration save', () => {
  beforeEach(() => localStorage.clear())

  it('survives refresh with only feature summaries and reuses its action for the same payload', async () => {
    const first = await preparePendingVoiceCalibrationSave('child-primary-4', profile('child-primary-4'), ['奥数'], microphone)
    const restored = loadPendingVoiceCalibrationSave('child-primary-4')
    const retried = await preparePendingVoiceCalibrationSave('child-primary-4', profile('child-primary-4'), ['奥数'], microphone)
    expect(restored).toMatchObject({ clientActionId: first.clientActionId, profile: { calibrationCount: 3 }, vocabulary: ['奥数'] })
    expect(retried.clientActionId).toBe(first.clientActionId)
    expect(JSON.stringify(restored)).not.toContain('RIFF')
  })

  it('keeps pending retries isolated per child and clears only the acknowledged child', async () => {
    await preparePendingVoiceCalibrationSave('child-primary-4', profile('child-primary-4'), ['奥数'], microphone)
    await preparePendingVoiceCalibrationSave('child-junior-3', profile('child-junior-3'), ['化学'], { ...microphone, system_id_hash: 'b'.repeat(64) })
    clearPendingVoiceCalibrationSave('child-primary-4')
    expect(loadPendingVoiceCalibrationSave('child-primary-4')).toBeNull()
    expect(loadPendingVoiceCalibrationSave('child-junior-3')?.vocabulary).toEqual(['化学'])
  })

  it('keeps a delete action durable and isolated for response-loss replay', () => {
    const first = preparePendingVoiceCalibrationDelete('child-primary-4')
    const replay = preparePendingVoiceCalibrationDelete('child-primary-4')
    preparePendingVoiceCalibrationDelete('child-junior-3')
    expect(replay.clientActionId).toBe(first.clientActionId)
    clearPendingVoiceCalibrationDelete('child-primary-4')
    expect(loadPendingVoiceCalibrationDelete('child-primary-4')).toBeNull()
    expect(loadPendingVoiceCalibrationDelete('child-junior-3')).not.toBeNull()
  })
})
