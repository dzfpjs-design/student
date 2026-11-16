import { beforeEach, describe, expect, it } from 'vitest'
import { AdaptiveVoiceActivityDetector, voiceCaptureSignalAdvice } from './LocalInputs'
import { addLocalVoiceCalibrationSample, isLocalVoiceProfileReady, localVoiceProfile, verifyLocalVoiceProfile } from './voiceProfile'

function wav(frequency = 220, amplitude = 8_000, seconds = 1, leadingSilenceSeconds = 0): Uint8Array {
  const sampleRate = 16_000; const speechSamples = Math.floor(sampleRate * seconds); const leadingSamples = Math.floor(sampleRate * leadingSilenceSeconds); const samples = speechSamples + leadingSamples
  const bytes = new Uint8Array(44 + samples * 2); const view = new DataView(bytes.buffer)
  const write = (offset: number, value: string): void => { for (let index = 0; index < value.length; index += 1) view.setUint8(offset + index, value.charCodeAt(index)) }
  write(0, 'RIFF'); view.setUint32(4, 36 + samples * 2, true); write(8, 'WAVE'); write(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(36, 'data'); view.setUint32(40, samples * 2, true)
  for (let index = leadingSamples; index < samples; index += 1) view.setInt16(44 + index * 2, Math.sin((index - leadingSamples) * Math.PI * 2 * frequency / sampleRate) * amplitude, true)
  return bytes
}

function transientWav(): Uint8Array {
  const bytes = wav(220, 0, 1)
  const view = new DataView(bytes.buffer)
  for (let index = 0; index < 160; index += 1) view.setInt16(44 + index * 2, index % 2 === 0 ? 20_000 : -20_000, true)
  return bytes
}

describe('local child voice adaptation', () => {
  beforeEach(() => localStorage.clear())

  it('requires three local-only calibration clips and never blends children', () => {
    addLocalVoiceCalibrationSample('child-a', wav(220))
    addLocalVoiceCalibrationSample('child-a', wav(230))
    expect(isLocalVoiceProfileReady(localVoiceProfile('child-a'))).toBe(false)
    addLocalVoiceCalibrationSample('child-a', wav(210))
    expect(isLocalVoiceProfileReady(localVoiceProfile('child-a'))).toBe(true)
    expect(localVoiceProfile('child-b')).toBeNull()
    expect(verifyLocalVoiceProfile('child-b', wav(220)).status).toBe('not_enrolled')
    expect(JSON.stringify(localVoiceProfile('child-a'))).not.toContain('RIFF')
  })

  it('uses a signal-quality re-ask policy without claiming ASR certainty', () => {
    expect(voiceCaptureSignalAdvice(wav(220, 0)).allowTranscription).toBe(false)
    expect(voiceCaptureSignalAdvice(wav(220, 8_000)).allowTranscription).toBe(true)
    expect(voiceCaptureSignalAdvice(wav(220, 1_000, 1, 8)).allowTranscription).toBe(true)
    expect(voiceCaptureSignalAdvice(wav(220, 250, 1, 8)).allowTranscription).toBe(true)
    expect(voiceCaptureSignalAdvice(wav(250, 32_767)).allowTranscription).toBe(false)
    expect(voiceCaptureSignalAdvice(transientWav()).allowTranscription).toBe(false)
  })

  it('learns quiet room noise, accepts sustained far-field speech, and rejects one impact', () => {
    const detector = new AdaptiveVoiceActivityDetector()
    for (let index = 0; index < 12; index += 1) expect(detector.observe(0.0029).speechDetected).toBe(false)
    expect(detector.observe(0.03).speechDetected).toBe(false)
    expect(detector.observe(0.0029).speechDetected).toBe(false)
    expect(detector.observe(0.006).speechDetected).toBe(false)
    expect(detector.observe(0.0062).speechDetected).toBe(false)
    expect(detector.observe(0.0061).speechDetected).toBe(true)
  })

  it('detects sustained quiet child speech without mistaking one sharp tap for speech', () => {
    const detector = new AdaptiveVoiceActivityDetector()
    for (let index = 0; index < 10; index += 1) expect(detector.observe(0.0014).speechDetected).toBe(false)
    expect(detector.observe(0.025).speechDetected).toBe(false)
    expect(detector.observe(0.0014).speechDetected).toBe(false)
    expect(detector.observe(0.0032).speechDetected).toBe(false)
    expect(detector.observe(0.0031).speechDetected).toBe(false)
    expect(detector.observe(0.0033).speechDetected).toBe(true)
  })
})
