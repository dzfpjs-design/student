import { describe, expect, it } from 'vitest'
import { selectVoiceCaptureMode, selectVoiceCaptureHandoff } from './voiceCaptureMode'

const ready = {
  runtimePolicy: {version:1 as const,capture:'configured' as const,runtimeEpoch:7}, runtimeEpoch:7,
  realtimeSettingsLoaded: true, realtimeConfigured: true, realtimeEnabled: true,
  realtimeFailed: false, onboardingSupported: true, onboardingPhase: 'idle' as const,
  privacyStopped: false, microphoneReady: true, wakeSettingsLoaded: true,
  editingWakeWord: false,
}

describe('voice capture startup ownership', () => {
  it('mounts no recorder while provider settings or onboarding state are unresolved', () => {
    expect(selectVoiceCaptureMode({ ...ready, realtimeSettingsLoaded: false })).toBe('blocked')
    expect(selectVoiceCaptureMode({ ...ready, onboardingPhase: 'loading' })).toBe('blocked')
    expect(selectVoiceCaptureMode({ ...ready, onboardingPhase: 'saving' })).toBe('blocked')
  })

  it('hands a deferred or completed child directly to the single realtime route', () => {
    expect(selectVoiceCaptureMode(ready)).toBe('qwen_realtime')
  })

  it('uses segmented capture only for an actual tutorial or a proven realtime failure', () => {
    expect(selectVoiceCaptureMode({ ...ready, onboardingPhase: 'naming' })).toBe('segmented')
    expect(selectVoiceCaptureMode({ ...ready, realtimeFailed: true })).toBe('segmented')
  })

  it('uses WAV capture for an active original-audio task without changing ordinary cloud conversation', () => {
    expect(selectVoiceCaptureMode({ ...ready, originalAudioRequired: true })).toBe('segmented')
    expect(selectVoiceCaptureMode({ ...ready, originalAudioRequired: false })).toBe('qwen_realtime')
  })

  it('keeps normal cloud conversation visibly blocked on unavailable cloud instead of switching to fixed Q&A', () => {
    expect(selectVoiceCaptureMode({ ...ready, nativeDialogueRequired: true, realtimeFailed: true })).toBe('blocked')
    expect(selectVoiceCaptureMode({ ...ready, nativeDialogueRequired: true, realtimeConfigured: false })).toBe('blocked')
    expect(selectVoiceCaptureMode({ ...ready, nativeDialogueRequired: true, runtimePolicy: {version:1,capture:'bounded_segmented',runtimeEpoch:7} })).toBe('blocked')
    expect(selectVoiceCaptureMode({ ...ready, nativeDialogueRequired: true, onboardingPhase: 'naming' })).toBe('segmented')
  })

  it('waits for speech and the original queue to settle before replacing a recorder', () => {
    const idle = { speechActive: false, pendingUtterances: 0, phase: 'listening' as const }
    for (const status of [{ ...idle, speechActive: true }, { ...idle, pendingUtterances: 1 },
      { ...idle, capturePending: true },
      ...['connecting', 'transcribing', 'tool_running', 'speaking', 'backpressure'].map(phase => ({ ...idle, phase }))]) {
      expect(selectVoiceCaptureHandoff('qwen_realtime', 'segmented', status, false)).toBe('qwen_realtime')
      expect(selectVoiceCaptureHandoff('segmented', 'qwen_realtime', status, false)).toBe('segmented')
    }
    expect(selectVoiceCaptureHandoff('qwen_realtime', 'segmented', idle, true)).toBe('qwen_realtime')
    expect(selectVoiceCaptureHandoff('qwen_realtime', 'segmented', idle, false)).toBe('segmented')
    expect(selectVoiceCaptureHandoff('qwen_realtime', 'segmented', { ...idle, speechActive: undefined }, false)).toBe('qwen_realtime')
    expect(selectVoiceCaptureHandoff('qwen_realtime', 'blocked', idle, true)).toBe('blocked')
    expect(selectVoiceCaptureHandoff('qwen_realtime', 'segmented', idle, true, true)).toBe('segmented')
  })
})
