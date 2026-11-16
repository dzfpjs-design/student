import { validVoiceRuntimePolicy, type VoiceRuntimePolicy } from '../../shared/voiceRuntimePolicy'
import type { VoiceOnboardingSnapshot } from './voiceOnboardingRuntime'

export type VoiceCaptureMode = 'blocked' | 'qwen_realtime' | 'segmented'

export function selectVoiceCaptureMode(input: {
  runtimePolicy: VoiceRuntimePolicy | null
  runtimeEpoch: number | null
  realtimeSettingsLoaded: boolean
  realtimeConfigured: boolean
  realtimeEnabled: boolean
  realtimeFailed: boolean
  onboardingSupported: boolean
  onboardingPhase: VoiceOnboardingSnapshot['phase'] | null
  privacyStopped: boolean
  microphoneReady: boolean
  wakeSettingsLoaded: boolean
  editingWakeWord: boolean
  nativeDialogueRequired?: boolean
  originalAudioRequired?: boolean
}): VoiceCaptureMode {
  if (!validVoiceRuntimePolicy(input.runtimePolicy,input.runtimeEpoch) || input.runtimePolicy.capture === 'blocked') return 'blocked'
  if (!input.realtimeSettingsLoaded
      || (input.onboardingSupported && (!input.onboardingPhase
        || ['loading', 'saving'].includes(input.onboardingPhase)))) return 'blocked'
  const tutorialActive = input.onboardingSupported && input.onboardingPhase !== 'idle'
  if (input.runtimePolicy.capture === 'configured' && input.realtimeConfigured && input.realtimeEnabled && !input.realtimeFailed
      && !input.privacyStopped && input.microphoneReady && input.wakeSettingsLoaded
      && !tutorialActive && !input.editingWakeWord && !input.originalAudioRequired) return 'qwen_realtime'
  if (input.nativeDialogueRequired && !tutorialActive && !input.editingWakeWord && !input.originalAudioRequired) return 'blocked'
  return 'segmented'
}

export function selectVoiceCaptureHandoff(current: VoiceCaptureMode, desired: VoiceCaptureMode,
  status: { speechActive?: boolean; capturePending?: boolean; pendingUtterances: number; phase: string }, busy: boolean,
  immediateRelease = false): VoiceCaptureMode {
  if (immediateRelease || current === 'blocked' || desired === 'blocked' || current === desired) return desired
  if (busy || status.speechActive || status.capturePending || status.pendingUtterances > 0
    || (status.speechActive === undefined && !['idle', 'wake_standby', 'error'].includes(status.phase))
    || ['connecting', 'transcribing', 'tool_running', 'speaking', 'backpressure', 'recovering'].includes(status.phase)) return current
  return desired
}
