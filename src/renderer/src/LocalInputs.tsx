import { SegmentedBargeEscrow } from './segmentedBargeEscrow'
import { APP_SPACE_VOICE_PRIORITY, registerAppSpaceVoice } from './appSpaceVoice'
import type { VoiceInputMode } from './voiceInputPreference'
import { playListenReadyTone } from './listenReadyTone'
import { configureListenReadyPlayback, observeListenReady, cancelListenReady, clearListenReadyGuidance, listenReadyPending, LISTEN_READY_GUIDANCE, type ListenReadyNoticeEnd, type ListenReadyCapture } from './listenReadyCue'
import { ProtectedPlaybackControl } from './protectedPlaybackControl'
import { LocalAttentionControl } from './localAttentionControl'
import { protectedPlaybackBarrier, PLAYBACK_TAIL_FAILURE } from './protectedPlaybackBarrier'
import { AdaptiveVoiceActivityDetector } from './voiceActivityDetector'
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { FileScan, Mic, Square } from 'lucide-react'
import { ApiError, localApi } from './api'
import { voiceCaptureReference, voiceCarryoverConfirmationCapture, clearVoiceCaptureContext, originalAudioCaptureRequired, type VoiceCaptureReference } from './voiceCaptureContext'
import { LOCAL_RECOVERY_FAILURE, LocalVoiceRecoveryError, transcribeCapturedVoice } from './localVoiceRecovery'
import { browserMediaErrorName, isMediaDeviceError, mediaDeviceFailure, mediaDeviceRegistration, resolveCompanionAudioDevice } from './mediaDevices'
import { claimVoiceCapture, getVoiceCaptureOwner, releaseVoiceCapture, subscribeVoiceCapture } from './voiceCaptureCoordinator'
import { getCompanionVoiceState, getCompanionPlaybackLease, companionPlaybackLeaseMatches, type CompanionSpeechOptions, type CompanionSpeechResult, LocalAgentStreamingTtsProvider, PcmPlaybackCache, playLocalPcm, speakCompanion, stopCompanionSpeech, subscribeCompanionVoice } from './companionVoice'
import { SpeechArbiter } from './speechArbiter'
import type { SpeechDisposition } from './speechArbiter'
import { verifyLocalVoiceProfile } from './voiceProfile'
import type { RealtimeVoiceStatus } from './realtimeVoiceRuntime'
import { matchWakeWord, shouldReturnToWakeStandby } from './wakeWordRuntime'
import { isImmediatePrivacyStop } from './deviceCommandReceipt'
import { negotiateMicrophoneEchoCancellation, type MicrophoneEchoPolicy , microphoneEchoMode } from './microphoneEchoCancellation'
import { voiceCaptureScopeStamp } from './voiceCaptureContext'
import { mistakeLearningOnset } from './mistakeLearningVoice'
import { ordinaryPromptSourceFact, ordinaryResponseGrant, revokeOrdinaryResponses, registerOrdinaryCapture, OrdinaryResponseGrant, sameResponseValue, REPEAT_RESPONSE , recoverOrdinaryListenReady } from './ordinaryResponseGrant'
import { onboardingResponseGrant, revokeOnboardingResponses, registerOnboardingCapture, type OnboardingResponseGrant , recoverOnboardingListenReady } from './onboardingResponseGrant'

type CapturedSpeechInterval = { startedAt: number; endedAt: number }
export type PlaybackOverlap = { playback_id: string }
export type VoiceTranscriptHandler = (text: string, overlap?: PlaybackOverlap, voiceInputId?: string,
  pendingEvaluationTurnId?: string, localRecoveryOnly?: boolean, carryoverConfirmationId?: string | null) => void | Promise<void>
type LocalSpeechEchoGuard = { text: string; startedAt: number; endedAt: number | null; sessionId?: string; playbackId?: string; registered?: Promise<boolean> }
let recentLocalSpeech: LocalSpeechEchoGuard[] = []
let voiceCaptureSequence = 0
const companionSpeechCache = new PcmPlaybackCache()
const microphoneOpenTimeoutMs = 10_000
const microphoneRecoveryMaximumDelayMs = 30_000
export const companionPlaybackCaptureCooldownMs = 850
const cloudDialogueFailureNotice = '云端暂时没听清，这句话没有执行，请稍后再说'
const cloudDialogueNoticeCooldownMs = 30_000

function recordVoiceDiagnostic(event: string, level: 'debug' | 'info' | 'warn' | 'error' = 'info', details?: Record<string, unknown>): void {
  void window.desktop?.recordRendererDiagnostic?.({ event, level, details }).catch(() => undefined)
}

export type VoiceActivityObservation = {
  voiced: boolean
  speechDetected: boolean
  threshold: number
  noiseFloor: number
}

export type UtteranceCaptureTiming = {
  maximumMs: number
  silenceMs: number
  maximumUtteranceMs: number
}

export function shouldFinishCapturedUtterance(
  now: number,
  waitStartedAt: number,
  speechStartedAt: number,
  lastSpeechAt: number,
  timing: UtteranceCaptureTiming,
): boolean {
  // The idle polling window must not cut an utterance that started near its end.
  if (speechStartedAt <= 0) return now - waitStartedAt >= timing.maximumMs
  return now - speechStartedAt >= timing.maximumUtteranceMs || now - lastSpeechAt >= timing.silenceMs
}

export function companionPlaybackCaptureGate(
  voiceState: ReturnType<typeof getCompanionVoiceState>,
  now: number,
  blockedUntil: number,
): { blocked: boolean; blockedUntil: number } {
  const nextBlockedUntil = voiceState === 'speaking'
    ? Math.max(blockedUntil, now + companionPlaybackCaptureCooldownMs)
    : blockedUntil
  return { blocked: voiceState === 'speaking' || now < nextBlockedUntil, blockedUntil: nextBlockedUntil }
}

export { AdaptiveVoiceActivityDetector } from './voiceActivityDetector'
export function pcmCaptureBlockSamples(sampleRate: number): number {
  if (!Number.isFinite(sampleRate) || sampleRate <= 0) return 1024
  return Math.max(256, Math.min(4096, 2 ** Math.floor(Math.log2(sampleRate * .032))))
}

export function microphoneRecoveryDelayMs(attempt: number): number {
  return Math.min(1_000 * (2 ** Math.max(0, attempt - 1)), microphoneRecoveryMaximumDelayMs)
}

export function shouldAutomaticallyRecoverMicrophone(cause: unknown): boolean {
  const name = browserMediaErrorName(cause)
  return name !== 'NotAllowedError' && name !== 'PermissionDeniedError'
}

export function isExpectedNonSpeechAsrError(cause: unknown): cause is ApiError {
  return cause instanceof ApiError && [
    'ASR_NO_HUMAN_SPEECH',
    'ASR_NO_MEANINGFUL_SPEECH',
    'ASR_NO_SPEECH',
  ].includes(cause.code ?? '')
}

export function asrFailureReason(cause: unknown): { reason_code: string; failure_kind: string } {
  if (cause instanceof ApiError) return {
    reason_code: cause.code || `HTTP_${cause.status ?? 'UNKNOWN'}`,
    failure_kind: 'api',
  }
  const name = cause instanceof Error ? cause.name : ''
  if (name === 'TimeoutError') return { reason_code: 'ASR_CLIENT_TIMEOUT', failure_kind: 'timeout' }
  if (name === 'TypeError') return { reason_code: 'ASR_CLIENT_NETWORK_OR_PROTOCOL', failure_kind: 'network_or_protocol' }
  return { reason_code: 'ASR_CLIENT_UNKNOWN', failure_kind: 'client' }
}

/** Kept pure so every stop path can prove a late ASR result cannot act. */
export function shouldDeliverHandsFreeTranscript(active: boolean, expectedGeneration: number, currentGeneration: number, signal: AbortSignal): boolean {
  return active && expectedGeneration === currentGeneration && !signal.aborted
}

export function shouldDeliverCompletedHandsFreeTranscript(status: 'completed' | 'ignored', active: boolean, expectedGeneration: number, currentGeneration: number, signal: AbortSignal): boolean {
  return status === 'completed' && shouldDeliverHandsFreeTranscript(active, expectedGeneration, currentGeneration, signal)
}

/** Manual capture has no listening loop, but an ignored ASR result is still
 * never a valid input to an agent or form callback. */
export function shouldDeliverManualTranscript(status: 'completed' | 'ignored'): boolean {
  return status === 'completed'
}

export function dispatchHandsFreeTranscript(
  isCurrent: () => boolean,
  onTranscript: VoiceTranscriptHandler,
  transcript: string,
  onError?: (cause: unknown) => void,
  overlap?: PlaybackOverlap,
  voiceInputId?: string,
  pendingEvaluationTurnId?: string,
  localRecoveryOnly?: boolean,
  carryoverConfirmationId?: string | null
): boolean {
  if (!isCurrent()) return false
  try {
    void Promise.resolve(voiceInputId || carryoverConfirmationId !== undefined
      ? onTranscript(transcript, voiceInputId && localRecoveryOnly ? undefined : overlap, voiceInputId,
        voiceInputId && !localRecoveryOnly ? pendingEvaluationTurnId : undefined,
        voiceInputId && localRecoveryOnly ? true : undefined, carryoverConfirmationId ?? null)
      : overlap ? onTranscript(transcript, overlap) : onTranscript(transcript)).catch((cause) => {
      if (isCurrent()) onError?.(cause)
    })
  } catch (cause) {
    if (isCurrent()) onError?.(cause)
  }
  return isCurrent()
}

/** A captured segment may finish after listening pauses for backpressure. It is
 * still current unless the user stopped, privacy paused, or the context changed. */
export function shouldProcessQueuedHandsFreeSegment(expectedGeneration: number, currentGeneration: number, signal: AbortSignal): boolean {
  return expectedGeneration === currentGeneration && !signal.aborted
}

function useVoiceCaptureOwner(mode: string): { owner: string; available: boolean } {
  const owner = useRef('')
  if (!owner.current) owner.current = `${mode}-${++voiceCaptureSequence}`
  const activeOwner = useSyncExternalStore(subscribeVoiceCapture, getVoiceCaptureOwner, getVoiceCaptureOwner)
  return { owner: owner.current, available: activeOwner === null || activeOwner === owner.current }
}

export function VoiceInputButton({ sessionId, purpose, onTranscript, onCaptured, label, skipTranscription = false, childId, audioDeviceId }: {
  sessionId?: string
  purpose: string
  onTranscript: VoiceTranscriptHandler
  /** Runs locally before the audio leaves memory; used for consented calibration only. */
  onCaptured?: (wav: Uint8Array) => void
  label?: string
  skipTranscription?: boolean
  /** Optional, local-only similarity prompt; never performs identity authentication. */
  childId?: string
  /** Explicit user-selected device; absence deliberately lets the OS choose. */
  audioDeviceId?: string
}): React.JSX.Element {
  const recorder = useRef<PcmRecorder | null>(null)
  const lease = useRef<string | null>(null)
  const opening = useRef<AbortController | null>(null)
  const openingGeneration = useRef(0)
  const transcription = useRef<AbortController | null>(null)
  const manualReference = useRef<VoiceCaptureReference | undefined>(undefined)
  const manualCarryoverConfirmation = useRef<string | null>(null)
  const [state, setState] = useState<'idle' | 'starting' | 'recording' | 'processing'>('idle')
  const [error, setError] = useState<string | null>(null)
  const capture = useVoiceCaptureOwner('manual')

  const release = (reason: string): void => {
    recorder.current?.dispose(); recorder.current = null
    if (lease.current) void localApi.releaseDeviceLease(lease.current, reason).catch(() => undefined)
    lease.current = null
    releaseVoiceCapture(capture.owner)
  }
  const cancelOpening = (reason: string): void => {
    manualReference.current = undefined
    manualCarryoverConfirmation.current = null
    openingGeneration.current += 1
    transcription.current?.abort(reason)
    transcription.current = null
    opening.current?.abort(reason)
    opening.current = null
    release(reason)
    setState('idle')
  }

  useEffect(() => () => {
    cancelOpening('voice_component_unmounted')
  }, [])

  useEffect(() => {
    const pause = (): void => {
      cancelOpening('voice_privacy_pause')
    }
    window.addEventListener('hsc-privacy-pause', pause)
    return () => window.removeEventListener('hsc-privacy-pause', pause)
  }, [])

  useEffect(() => () => { cancelOpening('voice_input_context_changed') }, [audioDeviceId, childId, sessionId, purpose])

  const toggle = async (): Promise<void> => {
    setError(null)
    if (state === 'starting') {
      cancelOpening('voice_open_cancelled_by_parent')
      setError('已取消打开麦克风。')
      return
    }
    if (state === 'recording' && recorder.current) {
      const generation = openingGeneration.current
      const controller = new AbortController()
      transcription.current = controller
      const isCurrent = (): boolean => generation === openingGeneration.current && !controller.signal.aborted
      const capturedCarryoverConfirmation = manualCarryoverConfirmation.current
      setState('processing')
      try {
        const wav = await recorder.current.stop()
        if (!isCurrent()) return
        recorder.current = null
        onCaptured?.(wav)
        const signalAdvice = voiceCaptureSignalAdvice(wav)
        if (!skipTranscription && !signalAdvice.allowTranscription) {
          setError(signalAdvice.notice)
          return
        }
        const speakerAdvice = voiceProfileCaptureAdvice(childId, wav, false)
        if (speakerAdvice.notice) setError(speakerAdvice.notice)
        if (!skipTranscription) {
          if (!sessionId) throw new Error('VOICE_TRANSCRIPTION_SESSION_REQUIRED')
          const reference = manualReference.current
          const result = reference && (reference.pending_evaluation_turn_id || reference.local_recovery_only)
            ? await transcribeCapturedVoice(sessionId, childId, purpose, bytesToBase64(wav), controller.signal, crypto.randomUUID(), reference, isCurrent)
            : await localApi.transcribe(sessionId, purpose, bytesToBase64(wav), controller.signal)
          if (!isCurrent()) return
          if (!shouldDeliverManualTranscript(result.status)) {
            setError('这段语音已取消或忽略，不会交给学习伙伴处理。')
            return
          }
          const localRecovery = result.capture_binding?.local_recovery_only === true
          if (localRecovery && !reference?.pending_evaluation_turn_id && !reference?.local_recovery_only) throw new LocalVoiceRecoveryError()
          if (!result.cloud_used && !localRecovery) setError('网络语音识别暂时不可用，刚才已用本地识别；如有听错，请再说一遍。')
          const returnedPending = result.capture_binding?.pending_evaluation_turn_id
          if (!localRecovery && returnedPending !== reference?.pending_evaluation_turn_id) throw new Error('这段录音的原评价绑定未通过核对，没有作为新答案提交。')
          if (localRecovery) {
            if (purpose !== 'agent_command') throw new LocalVoiceRecoveryError()
            await onTranscript(result.transcript, undefined, result.voice_input_id, undefined, true, capturedCarryoverConfirmation)
          } else if (returnedPending && result.capture_binding?.status === 'completed') {
            await onTranscript(result.transcript, undefined, result.voice_input_id, returnedPending, undefined, capturedCarryoverConfirmation)
          } else await onTranscript(result.transcript, undefined, undefined, undefined, undefined, capturedCarryoverConfirmation)
        }
      } catch (cause) {
        if (isCurrent() && cause instanceof LocalVoiceRecoveryError) speakLocally(LOCAL_RECOVERY_FAILURE, sessionId, undefined, undefined, { localOnly: true, isCurrent })
        if (isCurrent()) setError(cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : '语音没有识别成功。')
      } finally {
        if (isCurrent()) {
          transcription.current = null
          const completedLease = lease.current
          lease.current = null
          releaseVoiceCapture(capture.owner)
          setState('idle')
          if (completedLease) await localApi.releaseDeviceLease(completedLease, 'recording_finished').catch(() => undefined)
        }
      }
      return
    }
    if (!claimVoiceCapture(capture.owner)) {
      setError('免按键对话正在使用麦克风，请先停止后再单次录音。')
      return
    }
    setState('starting')
    const controller = new AbortController()
    const generation = ++openingGeneration.current
    opening.current = controller
    const timeout = window.setTimeout(() => {
      if (generation !== openingGeneration.current) return
      controller.abort('voice_open_timeout')
      release('voice_open_timeout')
      setState('idle'); setError('打开麦克风超时，请检查设备是否被占用后重试。')
    }, microphoneOpenTimeoutMs)
    try {
      cancelLocalSpeech()
      manualReference.current = sessionId && childId ? voiceCaptureReference(sessionId, childId) : undefined
      manualCarryoverConfirmation.current = voiceCarryoverConfirmationCapture(sessionId, childId)
      const nextRecorder = await PcmRecorder.start(audioDeviceId, controller.signal)
      if (controller.signal.aborted || generation !== openingGeneration.current) {
        nextRecorder.dispose()
        return
      }
      recorder.current = nextRecorder
      const registration = await mediaDeviceRegistration(recorder.current.audioTrack())
      if (controller.signal.aborted || generation !== openingGeneration.current) {
        nextRecorder.dispose()
        return
      }
      if (sessionId) {
        const registered = await localApi.registerDevice(sessionId, 'microphone', registration.systemIdHash, registration.capabilities)
        if (controller.signal.aborted || generation !== openingGeneration.current) {
          void localApi.releaseDeviceLease(registered.lease_id, 'voice_open_cancelled_after_registration').catch(() => undefined)
          nextRecorder.dispose()
          return
        }
        lease.current = registered.lease_id
      }
      if (controller.signal.aborted || generation !== openingGeneration.current) { nextRecorder.dispose(); return }
      setState('recording')
    } catch (cause) {
      if (generation === openingGeneration.current) {
        release('voice_open_failed')
        setState('idle')
        const message = controller.signal.reason === 'voice_open_timeout' ? '打开麦克风超时，请检查设备是否被占用后重试。' : controller.signal.aborted ? '已取消打开麦克风。' : mediaDeviceFailure(cause, '麦克风').message
        setError(message)
        if (sessionId && !controller.signal.aborted) {
          const failure = mediaDeviceFailure(cause, '麦克风')
          void localApi.recordDeviceIncident(sessionId, 'microphone', failure.code, failure.message).catch(() => undefined)
        }
      }
    } finally {
      window.clearTimeout(timeout)
      if (opening.current === controller) opening.current = null
    }
  }

  return (
    <div className="local-input-control">
      <button className={`secondary-button ${state === 'recording' ? 'is-recording' : ''}`} disabled={state === 'processing' || !capture.available} title={!capture.available ? '免按键对话正在使用麦克风' : undefined} onClick={() => void toggle()}>
        {state === 'recording' ? <Square size={16} /> : <Mic size={17} />}
        {state === 'starting' ? '取消打开麦克风' : state === 'recording' ? '结束并识别' : state === 'processing' ? '正在听懂' : label ?? '开始说话'}
      </button>
      {error && <span>{error}</span>}
    </div>
  )
}

type SegmentedGestureSource = 'keyboard' | 'screen'
const segmentedScopeChangedNotice = '当前语音输入已取消，请在当前页面再说一次。'

export function HandsFreeVoiceInput({ sessionId, purpose, onTranscript, onRuntimeStatusChange, childId, audioDeviceId, wakeWord, inactivitySeconds = 60, autoStart = false, externalActivationSignal = 0, initialConversationActive = false, standbyOnWakeWordEnable = false, interactionBusy = false, onWake, onConversationActiveChange, transcriptGuard, inputMode, onInputModeChange, spaceEnabled = true, onKeyboardCaptureStart, visibleLearningSource = true }: {
  sessionId: string
  purpose: string
  onTranscript: VoiceTranscriptHandler
  onRuntimeStatusChange?: (status: RealtimeVoiceStatus) => void
  childId?: string
  /** A uniquely resolved, parent-bound input; otherwise the OS default is used. */
  audioDeviceId?: string
  wakeWord?: string
  inactivitySeconds?: number
  autoStart?: boolean
  externalActivationSignal?: number
  /** Preserve continuous dialogue when replacing another voice transport. */
  initialConversationActive?: boolean
  /** Enter wake standby when a previously disabled wake policy is armed. */
  standbyOnWakeWordEnable?: boolean
  /** Keep an already active conversation open while its current turn executes. */
  interactionBusy?: boolean
  onWake?: () => void
  onConversationActiveChange?: (active: boolean) => void
  /** Tutorial progression guard; privacy stop always bypasses it. */
  transcriptGuard?: (text: string, capturedSpeech?: CapturedSpeechInterval) => boolean
  inputMode?: VoiceInputMode
  onInputModeChange?: (mode: VoiceInputMode) => void
  spaceEnabled?: boolean
  onKeyboardCaptureStart?: () => void
  visibleLearningSource?: boolean
}): React.JSX.Element {
  const handlers = useRef({ onTranscript, onWake, onConversationActiveChange, transcriptGuard })
  handlers.current = { onTranscript, onWake, onConversationActiveChange, transcriptGuard }
  const controlIdentity = useRef({sessionId,childId,audioDeviceId})
  controlIdentity.current = {sessionId,childId,audioDeviceId}
  const inputPolicy = useRef({ wakeWord, purpose, inactivitySeconds, visibleLearningSource })
  inputPolicy.current = { wakeWord, purpose, inactivitySeconds, visibleLearningSource }
  const interactionBusyRef = useRef(interactionBusy)
  const active = useRef(false)
  const recorder = useRef<PcmRecorder | null>(null)
  const inputModeRef = useRef(inputMode)
  inputModeRef.current = inputMode
  const spaceBinding = useRef<ReturnType<typeof registerAppSpaceVoice> | null>(null)
  const explicitGesture = useRef<{ source: SegmentedGestureSource; purpose: string; scope?: ReturnType<typeof voiceCaptureScopeStamp>;
    mistakeScope: string; visibleLearningSource: boolean; armed: boolean; ready: boolean } | null>(null)
  const [explicitState, setExplicitState] = useState<'waiting' | 'ready' | null>(null)
  const rejectedOnboardingUtterance = useRef<OnboardingRejectedUtterance>({blocked:false})
  const lease = useRef<string | null>(null)
  const leaseRenewalCleanup = useRef<(() => void) | null>(null)
  const opening = useRef<AbortController | null>(null)
  const generation = useRef(0)
  const transcriptions = useRef(new Set<AbortController>())
  const pendingSegments = useRef(0)
  const conversationActive = useRef(initialConversationActive || !wakeWord)
  const previousWakeWord = useRef(wakeWord)
  const lastConversationActivityAt = useRef(0)
  const seenExternalActivation = useRef(externalActivationSignal)
  const privacyPaused = useRef(false)
  const cloudFailureAnnounced = useRef(false)
  const lastCloudFailureNoticeAt = useRef<number | null>(null)
  const cloudFailureOwnsSpeech = useRef(false)
  const automaticRecoveryEnabled = useRef(autoStart)
  const recoveryNeeded = useRef(false)
  const recoveryAttempt = useRef(0)
  const recoveryTimer = useRef<number | null>(null)
  const [recoverySignal, setRecoverySignal] = useState(0)
  const [state, setState] = useState<'idle' | 'starting' | 'wake_standby' | 'listening' | 'catching_up' | 'playback_tail'>('idle')
  const [pendingCount, setPendingCount] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [recognitionUnavailable, setRecognitionUnavailable] = useState(false)
  const capture = useVoiceCaptureOwner('hands-free')
  const maximumPendingSegments = 8
  const runtimeStatus = useRef<RealtimeVoiceStatus>({ runtime: 'segmented_local', phase: 'idle', speechActive: false, pendingUtterances: 0, recoverable: true })
  const captureFailed = useRef(false)
  const lastReportedDeviceFailure = useRef<string | null>(null)
  const statusHandler = useRef(onRuntimeStatusChange)
  statusHandler.current = onRuntimeStatusChange
  const publishRuntimeStatus = (update: Partial<RealtimeVoiceStatus>): void => {
    runtimeStatus.current = { ...runtimeStatus.current, ...update }
    statusHandler.current?.(runtimeStatus.current)
  }

  const settleExplicitGesture = (): void => {
    explicitGesture.current = null
    setExplicitState(null)
  }

  const gestureScopeCurrent = (gesture: NonNullable<typeof explicitGesture.current>): boolean => {
    if (gesture.purpose !== inputPolicy.current.purpose || gesture.visibleLearningSource !== inputPolicy.current.visibleLearningSource) return false
    if (!childId || !gesture.scope) return true
    const current = voiceCaptureScopeStamp(sessionId, childId)
    return current.value === gesture.scope.value && current.documentOwner === gesture.scope.documentOwner
      && JSON.stringify(mistakeLearningOnset(sessionId, childId)) === gesture.mistakeScope
  }

  const cancelExplicitGesture = (): void => {
    if (!explicitGesture.current) return
    recorder.current?.cancelExplicitTurn()
    settleExplicitGesture()
    publishRuntimeStatus({ capturePending: false })
  }

  const armExplicitGesture = (current: PcmRecorder): boolean => {
    const gesture = explicitGesture.current
    if (!gesture || !gestureScopeCurrent(gesture) || privacyPaused.current || !active.current) return false
    if (gesture.armed) return true
    enterConversation(false)
    gesture.armed = current.beginExplicitTurn(() => {
      if (explicitGesture.current !== gesture) return
      if (!gestureScopeCurrent(gesture)) { cancelExplicitGesture(); return }
      gesture.ready = true
      setExplicitState('ready')
    }, () => {
      if (explicitGesture.current !== gesture) return
      settleExplicitGesture()
      setError('录音已到时限，没有发送。请分段再说。')
    })
    return gesture.armed
  }

  const ordinaryRepeatPending=useRef<(ReturnType<typeof voiceCaptureScopeStamp>&{ordinaryPromptIdentity?:object|null})|null>(null)
  const announceInputFailure = (message: string, current:()=>boolean=()=>true, bound=15000): void => {
    const now = Date.now(), owner = generation.current
    if (!current() || !conversationActive.current || privacyPaused.current || cloudFailureAnnounced.current
      || (lastCloudFailureNoticeAt.current !== null && now - lastCloudFailureNoticeAt.current < cloudDialogueNoticeCooldownMs)) return
    cloudFailureAnnounced.current = true
    lastCloudFailureNoticeAt.current = now
    speakLocally(message, sessionId, undefined, () => { cloudFailureOwnsSpeech.current = true }, {
      localOnly: true, priority: 'reminder', speechKey: 'voice-input-failure', expiresAt: now + bound,
      onPreparationStart: () => { cloudFailureOwnsSpeech.current = true },
      isCurrent: () => current() && generation.current === owner && !privacyPaused.current && conversationActive.current,
    })
  }

  useEffect(() => {
    const phase = recognitionUnavailable ? 'error' : state === 'starting' || state === 'playback_tail' ? 'connecting' : state === 'wake_standby' ? 'wake_standby' : state === 'listening' ? 'listening' : state === 'catching_up' ? 'backpressure' : captureFailed.current ? 'error' : 'idle'
    publishRuntimeStatus({ phase, detail: error ?? undefined })
  }, [error, onRuntimeStatusChange, recognitionUnavailable, state])

  const release = (reason: string): void => {
    leaseRenewalCleanup.current?.()
    leaseRenewalCleanup.current = null
    recorder.current?.dispose()
    recorder.current = null
    if (explicitGesture.current) settleExplicitGesture()
    publishRuntimeStatus({ speechActive: false, capturePending: false, captureReady: false })
    if (lease.current) void localApi.releaseDeviceLease(lease.current, reason).catch(() => undefined)
    lease.current = null
    releaseVoiceCapture(capture.owner)
  }

  const clearRecoveryTimer = (): void => {
    if (recoveryTimer.current !== null) window.clearTimeout(recoveryTimer.current)
    recoveryTimer.current = null
  }

  const scheduleAutomaticRecovery = (notice?: string): void => {
    if (notice) setError(notice)
    recoveryNeeded.current = true
    if (!autoStart || !automaticRecoveryEnabled.current || privacyPaused.current || pendingSegments.current > 0 || recoveryTimer.current !== null) return
    const attempt = ++recoveryAttempt.current
    const delay = microphoneRecoveryDelayMs(attempt)
    recoveryTimer.current = window.setTimeout(() => {
      recoveryTimer.current = null
      setRecoverySignal((value) => value + 1)
    }, delay)
  }

  const stop = (reason = 'hands_free_stopped'): void => {
    captureFailed.current = false
    setRecognitionUnavailable(false)
    if (cloudFailureOwnsSpeech.current) {
      cloudFailureOwnsSpeech.current = false
      cancelLocalSpeech()
    }
    clearRecoveryTimer()
    recoveryNeeded.current = false
    generation.current += 1
    active.current = false
    opening.current?.abort(reason)
    opening.current = null
    transcriptions.current.forEach((controller) => controller.abort(reason))
    transcriptions.current.clear()
    pendingSegments.current = 0
    conversationActive.current = false
    if (reason !== 'hands_free_unmounted' && reason !== 'hands_free_input_context_changed') handlers.current.onConversationActiveChange?.(false)
    lastConversationActivityAt.current = 0
    setPendingCount(0)
    publishRuntimeStatus({ phase: 'idle', speechActive: false, pendingUtterances: 0 })
    release(reason)
    if(reason==='hands_free_unmounted')clearListenReadyGuidance(sessionId,childId,capture.owner)
    if(reason==='hands_free_unmounted'||(reason==='hands_free_input_context_changed'
      &&(controlIdentity.current.sessionId!==sessionId||controlIdentity.current.childId!==childId)))rejectedOnboardingUtterance.current.blocked=false
    setState('idle')
  }

  useEffect(() => {
    let wasSpeaking = getCompanionVoiceState() === 'speaking'
    const unsubscribeSpeech = subscribeCompanionVoice((state) => {
      if (conversationActive.current && !privacyPaused.current && (wasSpeaking || state === 'speaking')) {
        lastConversationActivityAt.current = Date.now()
      }
      wasSpeaking = state === 'speaking'
      if (['idle', 'interrupted', 'error'].includes(state)) cloudFailureOwnsSpeech.current = false
      if (state === 'speaking') recorder.current?.resetSpeechActivity()
    })
    const privacyPause = (): void => {
      clearVoiceCaptureContext(sessionId)
      privacyPaused.current = true
      automaticRecoveryEnabled.current = false
      stop('hands_free_privacy_pause')
    }
    window.addEventListener('hsc-privacy-pause', privacyPause)
    return () => {
      unsubscribeSpeech()
      window.removeEventListener('hsc-privacy-pause', privacyPause)
      stop('hands_free_unmounted')
    }
  }, [])

  useEffect(() => {
    const wasBusy = interactionBusyRef.current
    interactionBusyRef.current = interactionBusy
    if (conversationActive.current && !privacyPaused.current && (wasBusy || interactionBusy)) {
      lastConversationActivityAt.current = Date.now()
    }
  }, [interactionBusy])

  useEffect(() => {
    automaticRecoveryEnabled.current = autoStart && !privacyPaused.current
    if (!autoStart) {
      clearRecoveryTimer()
      recoveryNeeded.current = false
    }
  }, [autoStart])

  // A child/profile switch must never leave a loop attached to the previous
  // child's microphone.  stop() aborts an in-flight open and releases a late
  // registration lease exactly once.
  useEffect(() => () => { stop('hands_free_input_context_changed') }, [audioDeviceId, childId, sessionId])

  // A tutorial handoff changes the wake policy, not the owner of the audio.
  // Keep the live conversation and queued recordings; the loop reads the new
  // policy for subsequent captures. Identity/device changes still cancel above.
  useEffect(() => {
    const previous = previousWakeWord.current
    previousWakeWord.current = wakeWord
    if (!active.current || privacyPaused.current) return
    if (!wakeWord) enterConversation(false)
    else if (!previous && standbyOnWakeWordEnable && pendingSegments.current === 0 && !interactionBusyRef.current) returnToWakeStandby()
  }, [standbyOnWakeWordEnable, wakeWord])

  const enterConversation = (announceWake: boolean): void => {
    if (!conversationActive.current) cloudFailureAnnounced.current = false
    conversationActive.current = true
    handlers.current.onConversationActiveChange?.(true)
    lastConversationActivityAt.current = Date.now()
    setError(null)
    if(active.current)setState(protectedPlaybackBarrier.held()?'playback_tail':'listening')
    if (announceWake) {
      recordVoiceDiagnostic('voice.wake.detected', 'info', { mode: 'wake', woke: true, local_only: true })
      handlers.current.onWake?.()
    }
  }

  const returnToWakeStandby = (): void => {
    if (!inputPolicy.current.wakeWord || !active.current) return
    conversationActive.current = false
    handlers.current.onConversationActiveChange?.(false)
    lastConversationActivityAt.current = 0
    setState('wake_standby')
  }

  const checkConversationInactivity = (): void => {
    if (!active.current || !conversationActive.current || privacyPaused.current) return
    const now = Date.now()
    // Only actual companion delivery/work renews the conversation. Unaccepted
    // VAD and pending ASR postpone the switch without extending its deadline.
    if (interactionBusyRef.current || ['speaking', 'preparing'].includes(getCompanionVoiceState())) {
      lastConversationActivityAt.current = now
      return
    }
    // Drain the original capture/queue before changing policy. No already
    // captured conversation segment may become a fresh standby command.
    if (runtimeStatus.current.speechActive || runtimeStatus.current.capturePending || pendingSegments.current > 0) return
    if (shouldReturnToWakeStandby(true, lastConversationActivityAt.current, now, inputPolicy.current.inactivitySeconds)) returnToWakeStandby()
  }

  useEffect(() => {
    const timer = window.setInterval(() => {
      checkConversationInactivity()
    }, 1_000)
    return () => window.clearInterval(timer)
  }, [inactivitySeconds, wakeWord])

  const listenLoop = async (runGeneration: number): Promise<void> => {
    let openTimeout: number | null = null
    let protectedTailTimeout: number | null = null
    let protectedTailRecorder: PcmRecorder | null = null
    let captureFailure: string | null = null
    let stopLeaseRenewal: (() => void) | null = null
    let captureTrack: MediaStreamTrack | null = null
    let captureEnded: (() => void) | null = null
    let processingTail = Promise.resolve()
    let runPending = 0
    let utteranceCapture: { purpose: string; standby: boolean; reference?: VoiceCaptureReference; carryoverConfirmationId: string | null } | undefined

    const updatePending = (next: number): void => {
      runPending = next
      if (runGeneration !== generation.current) return
      pendingSegments.current = next
      setPendingCount(next)
      publishRuntimeStatus({ pendingUtterances: next })
      if (next === 0) checkConversationInactivity()
      if (!active.current) setState(next > 0 ? 'catching_up' : 'idle')
      if (next === 0 && recoveryNeeded.current) scheduleAutomaticRecovery()
    }

    const nextResponseCompleted=(grant:OnboardingResponseGrant|OrdinaryResponseGrant):void=>{if(grant instanceof OrdinaryResponseGrant)grant.markAccepted();recorder.current?.completeResponse(grant)}
    const queueSegment = (wav: Uint8Array, wakeStandbyAtCapture: boolean, capturedSpeech: CapturedSpeechInterval | undefined,
      capturedPurpose: string, capturedReference: VoiceCaptureReference | undefined, capturedCarryoverConfirmation: string | null,
      response?: { grant: OnboardingResponseGrant|OrdinaryResponseGrant; isCurrent: () => boolean }): void => {
      if (capturedReference?.pending_evaluation_turn_id || capturedReference?.local_recovery_only) capturedPurpose = 'agent_command'
      processingTail = processingTail.then(async () => {
        if (runGeneration !== generation.current) return
        const transcriptionController = new AbortController()
        transcriptions.current.add(transcriptionController)
        const asrActionId = crypto.randomUUID()
        const unsubscribeResponse = response?.grant.subscribeRevoked(() => transcriptionController.abort('onboarding_response_revoked'))
        try {
          const ordinary=response?.grant instanceof OrdinaryResponseGrant?response.grant:null
          if (response && (capturedPurpose !== (ordinary?'agent_command':'wake_word_setup') || wakeStandbyAtCapture || !response.isCurrent())) return
          if(response&&!await response.grant.beforeSubmit(transcriptionController.signal))return
          if(response&&!response.isCurrent())return
          const startedAt = Date.now()
          recordVoiceDiagnostic('voice.asr.started', 'info', { mode: wakeStandbyAtCapture ? 'wake' : 'conversation', audio_bytes: wav.length })
          const result = response
            ? await localApi.transcribe(sessionId, capturedPurpose, bytesToBase64(wav), transcriptionController.signal, asrActionId, capturedReference, true, ordinary?.binding)
            : wakeStandbyAtCapture
            ? await localApi.transcribeStandby(bytesToBase64(wav), transcriptionController.signal, asrActionId)
            : capturedReference
              ? await transcribeCapturedVoice(sessionId, childId, capturedPurpose, bytesToBase64(wav), transcriptionController.signal, asrActionId, capturedReference,
                () => shouldProcessQueuedHandsFreeSegment(runGeneration, generation.current, transcriptionController.signal))
              : await localApi.transcribe(sessionId, capturedPurpose, bytesToBase64(wav), transcriptionController.signal, asrActionId)
          recordVoiceDiagnostic('voice.asr.completed', 'info', {
            mode: wakeStandbyAtCapture ? 'wake' : 'conversation',
            latency_ms: Date.now() - startedAt,
            transcript_chars: result.transcript.trim().length,
            cloud_used: result.cloud_used,
            status: result.status,
          })
          if (!shouldProcessQueuedHandsFreeSegment(runGeneration, generation.current, transcriptionController.signal) || (response && !response.isCurrent())) return
          if (result.status !== 'completed') {
            setError('这段语音已取消或忽略，不会交给学习伙伴处理。')
            return
          }
          setRecognitionUnavailable(false)
          setError(null)
          const returnedCapture=('capture_binding' in result?result.capture_binding:undefined) as import('./types').VoiceInput['capture_binding']
          if (response && (result.cloud_used !== false || !('local_only' in result) || result.local_only !== true || !('capture_binding' in result)
            || returnedCapture?.status !== 'completed' || returnedCapture.local_recovery_only
            || returnedCapture.pending_evaluation_turn_id
            || (ordinary ? returnedCapture.raw_audio_available!==false
              ||!sameResponseValue(returnedCapture.ordinary_response_binding,ordinary.binding)
              ||!sameResponseValue(returnedCapture.question_reference??null,capturedReference?.question_reference??null)
              ||!sameResponseValue(returnedCapture.activity_reference??null,capturedReference?.activity_reference??null)
              ||!sameResponseValue(returnedCapture.task_choice_reference??null,capturedReference?.task_choice_reference??null)
              ||!!capturedReference?.question_reference&&returnedCapture.question_validation!=='valid'
              ||!!capturedReference?.activity_reference&&returnedCapture.activity_validation!=='valid'
              :returnedCapture.question_reference||returnedCapture.activity_reference||returnedCapture.prompt_id||returnedCapture.item_version))) {
            throw new Error('及时回复的本机采集凭据未通过核对，请再说一次。')
          }
          const localRecovery = 'capture_binding' in result && result.capture_binding?.local_recovery_only === true
          let transcript = result.transcript.trim()
          if (isLowInformationVoiceTranscript(transcript, !wakeStandbyAtCapture)) {
            recordVoiceDiagnostic('voice.asr.low_information_filtered', 'info', { transcript_chars: transcript.length })
            return
          }
          const echoFilter = stripLocalCompanionSpeechEcho(transcript, capturedSpeech)
          if (echoFilter.echoDetected) {
            recordVoiceDiagnostic('voice.asr.playback_echo_discarded', 'info', {
              transcript_chars: transcript.length,
              removed_chars: echoFilter.removedCharacters,
            })
            return
          }
          if (transcript) {
            const returnedPending = 'capture_binding' in result ? result.capture_binding?.pending_evaluation_turn_id : undefined
            if (!localRecovery && returnedPending !== capturedReference?.pending_evaluation_turn_id) {
              throw new Error('这段录音的原评价绑定未通过核对，没有把它作为新答案提交。请先核对原记录。')
            }
            const playbackOverlap = localRecovery ? undefined : await localPlaybackOverlap(sessionId, transcript, capturedSpeech)
            if (!shouldProcessQueuedHandsFreeSegment(runGeneration, generation.current, transcriptionController.signal) || (response && !response.isCurrent())) return
            const wakeWord = inputPolicy.current.wakeWord
            const wake = wakeWord ? matchWakeWord(transcript, wakeWord) : null
            if (wake?.matched && isImmediatePrivacyStop(wake.command)) transcript = wake.command
            if (!localRecovery && !isImmediatePrivacyStop(transcript) && handlers.current.transcriptGuard?.(transcript, capturedSpeech) === false) return
            if (wake?.matched && !wake.command && !isImmediatePrivacyStop(transcript)) {
              enterConversation(true)
              return
            }
            if (wakeWord && wakeStandbyAtCapture && !conversationActive.current && !isImmediatePrivacyStop(transcript)) {
              if (!wake?.matched) return
              transcript = wake.command
              enterConversation(!transcript)
              if (!transcript) return
            }
            // Recovery can re-arm the notice, but hearing our own notice back
            // must not restart a failure/playback feedback loop.
            const compact = (value: string): string => value.replace(/[\s，。！？,.!?]/g, '')
            if (!wakeStandbyAtCapture && compact(transcript) !== compact(cloudDialogueFailureNotice)) cloudFailureAnnounced.current = false
            lastConversationActivityAt.current = Date.now()
            if(response){unsubscribeResponse?.();nextResponseCompleted(response.grant)}
            if (capturedPurpose !== 'wake_word_setup' || isImmediatePrivacyStop(transcript)) cancelLocalSpeech()
            recordVoiceDiagnostic('voice.asr.transcript_dispatched', 'info', { transcript_chars: transcript.length })
            dispatchHandsFreeTranscript(
              () => shouldProcessQueuedHandsFreeSegment(runGeneration, generation.current, transcriptionController.signal) && (!response || (ordinary?ordinary.handoffCurrent():response.isCurrent())),
              handlers.current.onTranscript,
              transcript,
              (cause) => setError(cause instanceof Error ? cause.message : '这句话暂时没有处理成功。'),
              playbackOverlap,
              'capture_binding' in result && result.capture_binding?.status === 'completed' ? result.voice_input_id : undefined,
              returnedPending,
              localRecovery,
              capturedCarryoverConfirmation
            )
          }
        } catch (cause) {
          if (transcriptionController.signal.aborted) {
            if (!wakeStandbyAtCapture) {
              void localApi.cancelTranscriptionAction(sessionId, asrActionId)
                .then(() => localApi.transcriptionAction(sessionId, asrActionId))
                .catch(() => undefined)
            }
            return
          }
          if (runGeneration !== generation.current) return
          if ((cause instanceof Error || cause instanceof DOMException) && cause.name === 'AbortError') return
          if (isExpectedNonSpeechAsrError(cause)) {
            recordVoiceDiagnostic('voice.asr.non_speech_discarded', 'info', {
              mode: wakeStandbyAtCapture ? 'wake' : 'conversation', reason_code: cause.code,
            })
            return
          }
          setRecognitionUnavailable(true)
          recordVoiceDiagnostic('voice.asr.failed', 'error', {
            mode: wakeStandbyAtCapture ? 'wake' : 'conversation',
            ...asrFailureReason(cause),
          })
          if (cause instanceof LocalVoiceRecoveryError) {
            setError(LOCAL_RECOVERY_FAILURE)
            if (!wakeStandbyAtCapture) announceInputFailure(LOCAL_RECOVERY_FAILURE)
            return
          }
          if (cause instanceof ApiError && cause.code === 'ASR_CLOUD_DIALOGUE_UNAVAILABLE') {
            setError(cloudDialogueFailureNotice)
            if (!wakeStandbyAtCapture) announceInputFailure(cloudDialogueFailureNotice)
            return
          }
          setError(cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : '这句话暂时没有识别成功。')
          if (!wakeStandbyAtCapture) announceInputFailure('刚才这句话没有识别成功，也没有执行。请稍后再说一次。')
        } finally {
          unsubscribeResponse?.()
          response?.grant.revoke()
          transcriptions.current.delete(transcriptionController)
        }
      }).catch((cause) => {
        if (runGeneration === generation.current) {
          setError(cause instanceof Error ? cause.message : '语音队列暂时没有处理成功。')
          if (!wakeStandbyAtCapture) announceInputFailure('刚才的语音处理没有完成，请稍后再说一次。')
        }
      }).finally(() => updatePending(Math.max(0, runPending - 1)))
    }

    try {
      setState('starting')
      const controller = new AbortController()
      opening.current = controller
      openTimeout = window.setTimeout(() => {
        if (opening.current !== controller) return
        captureFailure = '打开麦克风超时，正在自动重连。'
        controller.abort('hands_free_open_timeout')
      }, microphoneOpenTimeoutMs)
      if(protectedPlaybackBarrier.held()&&(!childId||!protectedPlaybackBarrier.reserveOpen({sessionId,childId},performance.now()))){automaticRecoveryEnabled.current=false;captureFailure=PLAYBACK_TAIL_FAILURE;setError(PLAYBACK_TAIL_FAILURE);throw new Error(PLAYBACK_TAIL_FAILURE)}
      const tailToken=protectedPlaybackBarrier.token()
      const watchTailDeadline=():void=>{
        if(!tailToken||controller.signal.aborted||runGeneration!==generation.current||!active.current)return
        protectedTailTimeout=window.setTimeout(()=>{
          protectedTailTimeout=null
          if(controller.signal.aborted||runGeneration!==generation.current||!active.current||protectedPlaybackBarrier.token()!==tailToken)return
          if(!protectedPlaybackBarrier.tick(performance.now())){watchTailDeadline();return}
          captureFailure=PLAYBACK_TAIL_FAILURE;captureFailed.current=true;active.current=false
          controller.abort('protected_tail_deadline');protectedTailRecorder?.dispose()
          if(opening.current===controller)opening.current=null
          release('protected_tail_deadline');setState('idle');setError(PLAYBACK_TAIL_FAILURE)
          if(childId&&!protectedPlaybackBarrier.mayOpen({sessionId,childId},performance.now()))automaticRecoveryEnabled.current=false
          scheduleAutomaticRecovery(PLAYBACK_TAIL_FAILURE)
        },protectedPlaybackBarrier.remainingMs(performance.now()))
      }
      watchTailDeadline()
      const nextRecorder = await PcmRecorder.start(audioDeviceId, controller.signal, (speechActive) => {
        if (runGeneration === generation.current && (active.current || !speechActive)) {
          publishRuntimeStatus({ speechActive })
        }
      }, () => {
        if (runGeneration !== generation.current || !active.current) return
        publishRuntimeStatus({ capturePending: true })
        const standby = shouldUseStandbyTranscription(inputPolicy.current.wakeWord, conversationActive.current)
        utteranceCapture = { purpose: inputPolicy.current.purpose, standby,
          carryoverConfirmationId: !standby ? voiceCarryoverConfirmationCapture(sessionId, childId) : null,
          reference: !standby && childId ? voiceCaptureReference(sessionId, childId) : undefined }
      }, 0, () => {
        if (runGeneration === generation.current && active.current) publishRuntimeStatus({ captureReady: true })
      }, {
        eligible: () => Boolean(childId && conversationActive.current
          && originalAudioCaptureRequired(sessionId, childId)),
        discarded: () => {
          if (runGeneration !== generation.current || !active.current) return
          utteranceCapture = undefined
          if (runtimeStatus.current.capturePending) publishRuntimeStatus({ capturePending: false })
        },
      }, childId?{sessionId,childId,response:{owner:capture.owner,rejectedUtterance:rejectedOnboardingUtterance.current,isCurrent:()=>active.current&&runGeneration===generation.current&&!privacyPaused.current
        &&inputPolicy.current.purpose==='wake_word_setup'&&!shouldUseStandbyTranscription(inputPolicy.current.wakeWord,conversationActive.current)
        &&!voiceCaptureReference(sessionId,childId).question_reference&&!voiceCaptureReference(sessionId,childId).activity_reference
        &&!voiceCaptureReference(sessionId,childId).pending_evaluation_turn_id&&!voiceCaptureReference(sessionId,childId).local_recovery_only
        &&!voiceCaptureReference(sessionId,childId).task_choice_reference,onUnsupported:()=>{if(active.current&&runGeneration===generation.current&&!privacyPaused.current){setError(REPEAT_RESPONSE);ordinaryRepeatPending.current=voiceCaptureScopeStamp(sessionId,childId)}},
        onRepeatGiven:()=>{if(active.current&&runGeneration===generation.current&&!privacyPaused.current)ordinaryRepeatPending.current=null},
        onCueReady:()=>{if(active.current&&runGeneration===generation.current&&!privacyPaused.current){ordinaryRepeatPending.current=null;setError(null)}},
        onQuiet:(actualCapture)=>{if(getVoiceCaptureOwner()===capture.owner&&!listenReadyPending(sessionId,childId,capture.owner)&&ordinaryRepeatPending.current&&active.current&&runGeneration===generation.current&&!privacyPaused.current){const notice=ordinaryRepeatPending.current;ordinaryRepeatPending.current=null;if(microphoneEchoMode(actualCapture.track)!=='all'){recoverOnboardingListenReady(sessionId,childId,actualCapture);return}announceInputFailure(REPEAT_RESPONSE,()=>{const now=voiceCaptureScopeStamp(sessionId,childId);return getVoiceCaptureOwner()===capture.owner&&now.value===notice.value&&now.documentOwner===notice.documentOwner},12000)}}},ordinary:{
        owner:capture.owner,rejectedUtterance:rejectedOnboardingUtterance.current,
        isCurrent:()=>active.current&&runGeneration===generation.current&&!privacyPaused.current
          &&inputPolicy.current.purpose==='agent_command'&&!shouldUseStandbyTranscription(inputPolicy.current.wakeWord,conversationActive.current)
          &&!voiceCaptureReference(sessionId,childId).pending_evaluation_turn_id&&!voiceCaptureReference(sessionId,childId).local_recovery_only,
        onUnsupported:()=>{if(active.current&&runGeneration===generation.current&&!privacyPaused.current){setError(REPEAT_RESPONSE);ordinaryRepeatPending.current={...voiceCaptureScopeStamp(sessionId,childId),ordinaryPromptIdentity:ordinaryPromptSourceFact(sessionId,childId)?.identity??null}}},
        onRepeatGiven:()=>{if(active.current&&runGeneration===generation.current&&!privacyPaused.current)ordinaryRepeatPending.current=null},
        onCueReady:()=>{if(active.current&&runGeneration===generation.current&&!privacyPaused.current){ordinaryRepeatPending.current=null;setError(null)}},
        onQuiet:(actualCapture)=>{if(getVoiceCaptureOwner()===capture.owner&&!listenReadyPending(sessionId,childId,capture.owner)&&ordinaryRepeatPending.current&&active.current&&runGeneration===generation.current&&!privacyPaused.current){const notice=ordinaryRepeatPending.current;ordinaryRepeatPending.current=null;const prompt=ordinaryPromptSourceFact(sessionId,childId);if(!prompt||notice.ordinaryPromptIdentity!==prompt.identity)return;if(microphoneEchoMode(actualCapture.track)!=='all'){recoverOrdinaryListenReady(sessionId,childId,actualCapture);return}announceInputFailure(REPEAT_RESPONSE,()=>{const now=voiceCaptureScopeStamp(sessionId,childId);return getVoiceCaptureOwner()===capture.owner&&now.value===notice.value&&now.documentOwner===notice.documentOwner&&ordinaryPromptSourceFact(sessionId,childId)?.identity===notice.ordinaryPromptIdentity},12000)}}}}:undefined)
      protectedTailRecorder=nextRecorder
      if (!active.current || runGeneration !== generation.current || controller.signal.aborted) { nextRecorder.dispose(); return }
      nextRecorder.setAutomaticEndpointing(inputModeRef.current !== 'manual')
      recorder.current = nextRecorder
      captureTrack = nextRecorder.audioTrack()
      captureEnded = () => {
        if (!active.current || runGeneration !== generation.current) return
        captureFailure = '麦克风连接中断，正在自动重连。'
        active.current = false
        nextRecorder.dispose()
      }
      captureTrack.addEventListener?.('ended', captureEnded, { once: true })
      const registration = await mediaDeviceRegistration(nextRecorder.audioTrack())
      if (!active.current || runGeneration !== generation.current || controller.signal.aborted) { nextRecorder.dispose(); return }
      const registered = await localApi.registerDevice(sessionId, 'microphone', registration.systemIdHash, registration.capabilities)
      if (!active.current || runGeneration !== generation.current || controller.signal.aborted) {
        void localApi.releaseDeviceLease(registered.lease_id, 'hands_free_open_cancelled_after_registration').catch(() => undefined)
        nextRecorder.dispose()
        return
      }
      window.clearTimeout(openTimeout); openTimeout = null
      opening.current = null
      lease.current = registered.lease_id
      // Renew only this original live recorder/lease. A retired flight never
      // updates a replacement owner, and abort does not claim server rollback.
      let renewalStopped = false
      let renewalController: AbortController | null = null
      let renewalDeadline: number | null = null
      const ownsLease = (): boolean => !renewalStopped && active.current && !privacyPaused.current
        && generation.current === runGeneration && recorder.current === nextRecorder
        && lease.current === registered.lease_id && getVoiceCaptureOwner() === capture.owner
        && controlIdentity.current.sessionId === sessionId && controlIdentity.current.childId === childId
        && controlIdentity.current.audioDeviceId === audioDeviceId
      const failRenewal = (): void => {
        if (!ownsLease()) return
        captureFailed.current = true
        captureFailure = '麦克风使用状态续期未确认，已暂停收音，正在自动重新核对连接。'
        active.current = false
        nextRecorder.dispose()
      }
      const renewalTimer = window.setInterval(() => {
        if (!ownsLease() || renewalController) return
        const controller = new AbortController()
        renewalController = controller
        renewalDeadline = window.setTimeout(() => { controller.abort('microphone_lease_renewal_timeout'); failRenewal() }, 10_000)
        void localApi.renewDeviceLease(registered.lease_id, controller.signal).then(receipt => {
          if (!ownsLease() || controller.signal.aborted) return
          const expires = Date.parse(receipt.expires_at)
          if (receipt.lease_id !== registered.lease_id || receipt.status !== 'active'
            || !Number.isFinite(expires) || expires <= Date.now()) failRenewal()
        }).catch(() => { failRenewal() }).finally(() => {
          if (renewalController !== controller) return
          if (renewalDeadline !== null) window.clearTimeout(renewalDeadline)
          renewalDeadline = null
          renewalController = null
        })
      }, 30_000)
      stopLeaseRenewal = () => {
        renewalStopped = true
        window.clearInterval(renewalTimer)
        if (renewalDeadline !== null) window.clearTimeout(renewalDeadline)
        renewalDeadline = null
        renewalController?.abort('microphone_lease_retired')
        renewalController = null
        if (leaseRenewalCleanup.current === stopLeaseRenewal) leaseRenewalCleanup.current = null
      }
      leaseRenewalCleanup.current = stopLeaseRenewal
      if(childId)nextRecorder.enableProtectedControl({sessionId,childId,leaseId:registered.lease_id,
        attentionWakeWord:()=>inputPolicy.current.wakeWord,onAttentionWake:()=>enterConversation(true),
        isCurrent:()=>controlIdentity.current.sessionId===sessionId&&controlIdentity.current.childId===childId&&controlIdentity.current.audioDeviceId===audioDeviceId&&active.current&&runGeneration===generation.current&&!privacyPaused.current&&recorder.current===nextRecorder&&lease.current===registered.lease_id&&getVoiceCaptureOwner()===capture.owner,
        onHold:held=>{if(active.current&&runGeneration===generation.current)setState(held?'playback_tail':inputPolicy.current.wakeWord&&!conversationActive.current?'wake_standby':'listening')},
        onFailure:()=>{captureFailure=PLAYBACK_TAIL_FAILURE;active.current=false;setError(PLAYBACK_TAIL_FAILURE);if(!protectedPlaybackBarrier.mayOpen({sessionId,childId},performance.now()))automaticRecoveryEnabled.current=false;nextRecorder.dispose()}})
      recoveryAttempt.current = 0
      recoveryNeeded.current = false
      lastReportedDeviceFailure.current = null
      setState(protectedPlaybackBarrier.held()?'playback_tail':inputPolicy.current.wakeWord&&!conversationActive.current?'wake_standby':'listening')
      if (explicitGesture.current && !armExplicitGesture(nextRecorder)) cancelExplicitGesture()

      while (active.current && runGeneration === generation.current) {
        const timing = childUtteranceCaptureTiming(conversationActive.current)
        const heardSpeech = await nextRecorder.waitForUtterance(timing.maximumMs, timing.silenceMs, timing.maximumUtteranceMs)
        if (!active.current || runGeneration !== generation.current) break
        if (!heardSpeech) {
          nextRecorder.discardSegment()
          publishRuntimeStatus({ capturePending: false })
          utteranceCapture = undefined
          checkConversationInactivity()
          continue
        }
        const capturedSpeech = nextRecorder.capturedSpeechInterval()
        const captured = utteranceCapture
        utteranceCapture = undefined
        // Reserve the ASR handoff before clearing VAD so subscribers never see
        // an idle gap between a finished utterance and its pending transcript.
        updatePending(runPending + 1)
        publishRuntimeStatus({ capturePending: false })
        const response = nextRecorder.onboardingCapture()
        const wav = nextRecorder.takeSegment()
        const signalAdvice = handsFreeVoiceCaptureAdvice(wav)
        if (!signalAdvice.allowTranscription) {
          recordVoiceDiagnostic('voice.capture.signal_rejected', 'warn', { confidence: signalAdvice.confidence, audio_bytes: wav.length })
          if (conversationActive.current) setError(signalAdvice.notice)
          updatePending(Math.max(0, runPending - 1))
          continue
        }
        recordVoiceDiagnostic('voice.capture.segment_queued', 'info', {
          policy: 'post_vad_structural_v2',
          audio_bytes: wav.length,
          mode: conversationActive.current ? 'conversation' : 'wake',
        })
        const speakerAdvice = voiceProfileCaptureAdvice(childId, wav, true)
        if (!speakerAdvice.allowAutomaticSubmit) {
          setError(speakerAdvice.notice ?? '我还不能确认是你的声音。请靠近绑定的麦克风，重新说一遍唤醒词和刚才那句话。')
          updatePending(Math.max(0, runPending - 1))
          continue
        }
        queueSegment(wav, captured?.standby ?? shouldUseStandbyTranscription(inputPolicy.current.wakeWord, conversationActive.current),
          capturedSpeech, captured?.purpose ?? inputPolicy.current.purpose, captured?.reference, captured?.carryoverConfirmationId ?? null,response)
        if (runPending >= maximumPendingSegments) {
          active.current = false
          captureFailure = '语音处理队列已追平，正在自动恢复收音。'
          setState('catching_up')
          setError(`已有${runPending}句话等待处理，已暂停收音；处理完成后可继续。`)
          announceInputFailure('我还在处理前面的话，暂时暂停收音。请稍等一下，处理完会恢复。')
        }
      }
    } catch (cause) {
      if (!active.current || runGeneration !== generation.current) return
      captureFailed.current = true
      const detail = captureFailure ?? (isMediaDeviceError(cause) ? mediaDeviceFailure(cause, '麦克风').message
        : cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : '免按键对话暂时不可用。')
      captureFailure = shouldAutomaticallyRecoverMicrophone(cause) ? `${detail} 正在自动重连。` : null
      setError(captureFailure ?? detail)
      announceInputFailure('麦克风暂时不可用，我现在听不到新的话。请检查连接；状态恢复后再继续。')
      if (isMediaDeviceError(cause)) {
        const failure = mediaDeviceFailure(cause, '麦克风')
        if (lastReportedDeviceFailure.current !== failure.code) {
          lastReportedDeviceFailure.current = failure.code
          void localApi.recordDeviceIncident(sessionId, 'microphone', failure.code, failure.message).catch(() => undefined)
        }
      }
      active.current = false
    } finally {
      stopLeaseRenewal?.()
      if (openTimeout !== null) window.clearTimeout(openTimeout)
      if(protectedTailTimeout!==null)window.clearTimeout(protectedTailTimeout)
      if (captureTrack && captureEnded) captureTrack.removeEventListener?.('ended', captureEnded)
      if (runGeneration === generation.current) {
        opening.current = null
        release('hands_free_capture_ended')
        if (runPending === 0) setState('idle')
        if (captureFailure) scheduleAutomaticRecovery(captureFailure)
      }
    }
  }

  const startListening = (activateConversation = false, preserveSpeech = false, explicitResume = false, preserveActivity = false): void => {
    captureFailed.current = false
    setError(null)
    if (privacyPaused.current && !explicitResume) return
    if (explicitResume) {
      privacyPaused.current = false
      automaticRecoveryEnabled.current = autoStart
      recoveryNeeded.current = false
      clearRecoveryTimer()
    }
    if (active.current || pendingSegments.current > 0) {
      if (activateConversation) enterConversation(false)
      return
    }
    if (!claimVoiceCapture(capture.owner)) {
      setError('单次录音正在使用麦克风，请先结束识别。')
      return
    }
    if (!preserveSpeech) cancelLocalSpeech()
    conversationActive.current = activateConversation || !wakeWord
    handlers.current.onConversationActiveChange?.(conversationActive.current)
    lastConversationActivityAt.current = conversationActive.current
      ? preserveActivity && lastConversationActivityAt.current > 0 ? lastConversationActivityAt.current : Date.now()
      : 0
    active.current = true
    const runGeneration = ++generation.current
    void listenLoop(runGeneration)
  }

  const toggle = (): void => {
    if (active.current || pendingSegments.current > 0) {
      automaticRecoveryEnabled.current = false
      stop()
      return
    }
    if(childId)protectedPlaybackBarrier.explicitRetry({sessionId,childId},performance.now())
    automaticRecoveryEnabled.current = autoStart
    startListening(!wakeWord, false, true)
  }

  const canUseExplicitInput = (): boolean => Boolean(inputModeRef.current) && spaceEnabled && !privacyPaused.current
    && !document.hidden && controlIdentity.current.sessionId === sessionId && controlIdentity.current.childId === childId
    && controlIdentity.current.audioDeviceId === audioDeviceId

  const beginExplicitGesture = (source: SegmentedGestureSource): boolean => {
    if (!canUseExplicitInput() || explicitGesture.current || pendingSegments.current >= maximumPendingSegments) return false
    const owner = getVoiceCaptureOwner()
    if (owner && owner !== capture.owner) return false
    const playback = getCompanionPlaybackLease(true)
    if (playback?.sessionId === sessionId) {
      const tail = childId ? protectedPlaybackBarrier.begin({sessionId, childId}, playback.id, playback.generation, performance.now()) : null
      if (!cancelExactLocalSpeech(playback.id, playback.generation, true) && tail) protectedPlaybackBarrier.rollback(tail)
    }
    if (childId) protectedPlaybackBarrier.explicitRetry({sessionId, childId}, performance.now())
    explicitGesture.current = { source, purpose, scope: childId ? voiceCaptureScopeStamp(sessionId, childId) : undefined,
      mistakeScope: JSON.stringify(childId ? mistakeLearningOnset(sessionId, childId) : null), visibleLearningSource, armed: false, ready: false }
    setError(null)
    setExplicitState('waiting')
    publishRuntimeStatus({ capturePending: true })
    if (!active.current) startListening(true, true)
    if (!active.current || (recorder.current && lease.current && !armExplicitGesture(recorder.current))) {
      cancelExplicitGesture()
      return false
    }
    if (source === 'keyboard') onKeyboardCaptureStart?.()
    return true
  }

  const finishExplicitGesture = (source: SegmentedGestureSource): void => {
    const gesture = explicitGesture.current
    if (!gesture || gesture.source !== source) return
    if (!canUseExplicitInput() || !gestureScopeCurrent(gesture)) {
      cancelExplicitGesture()
      setError(segmentedScopeChangedNotice)
      return
    }
    if (!gesture.ready) {
      cancelExplicitGesture()
      setError('麦克风尚未准备好，请等“正在听”的提示出现后再说。')
      return
    }
    recorder.current?.finishExplicitTurn()
    settleExplicitGesture()
  }

  const explicitControls = useRef({ canUseExplicitInput, beginExplicitGesture, finishExplicitGesture, cancelExplicitGesture })
  explicitControls.current = { canUseExplicitInput, beginExplicitGesture, finishExplicitGesture, cancelExplicitGesture }
  useLayoutEffect(() => {
    const gesture = explicitGesture.current
    if (!gesture || gesture.visibleLearningSource === visibleLearningSource) return
    spaceBinding.current?.cancel()
    explicitControls.current.cancelExplicitGesture()
    setError(segmentedScopeChangedNotice)
  }, [visibleLearningSource])

  useEffect(() => {
    if (inputMode === undefined) return
    const current = registerAppSpaceVoice({ priority: APP_SPACE_VOICE_PRIORITY.focused,
      eligible: () => explicitControls.current.canUseExplicitInput(),
      begin: () => explicitControls.current.beginExplicitGesture('keyboard'),
      finish: () => explicitControls.current.finishExplicitGesture('keyboard'),
      cancel: () => explicitControls.current.cancelExplicitGesture() })
    spaceBinding.current = current
    const cancelScreen = (): void => { if (explicitGesture.current?.source === 'screen') explicitControls.current.cancelExplicitGesture() }
    const hidden = (): void => { if (document.hidden) cancelScreen() }
    window.addEventListener('blur', cancelScreen)
    document.addEventListener('visibilitychange', hidden)
    return () => {
      current.unregister()
      cancelScreen()
      if (spaceBinding.current === current) spaceBinding.current = null
      window.removeEventListener('blur', cancelScreen)
      document.removeEventListener('visibilitychange', hidden)
    }
  }, [inputMode !== undefined, sessionId, childId, audioDeviceId, purpose])

  useEffect(() => {
    spaceBinding.current?.refresh()
    if (!spaceEnabled) explicitControls.current.cancelExplicitGesture()
  }, [spaceEnabled])

  useEffect(() => {
    spaceBinding.current?.cancel()
    explicitControls.current.cancelExplicitGesture()
    recorder.current?.setAutomaticEndpointing(inputMode !== 'manual')
  }, [inputMode])

  useEffect(() => {
    if (!autoStart || !automaticRecoveryEnabled.current || privacyPaused.current || !capture.available || active.current || pendingSegments.current > 0) return
    const expired = shouldReturnToWakeStandby(conversationActive.current, lastConversationActivityAt.current, Date.now(), inputPolicy.current.inactivitySeconds)
    startListening((conversationActive.current && !expired) || !wakeWord, true, false, true)
  }, [autoStart, audioDeviceId, capture.available, childId, initialConversationActive, pendingCount, recoverySignal, wakeWord])

  useEffect(() => {
    if (externalActivationSignal <= seenExternalActivation.current) return
    seenExternalActivation.current = externalActivationSignal
    if (!autoStart || !wakeWord) return
    startListening(true, true)
  }, [autoStart, externalActivationSignal, wakeWord])

  if (inputMode !== undefined) {
    const hint = error || (explicitState === 'waiting' ? '麦克风准备中，请等提示后再说'
      : explicitState === 'ready' ? (explicitGesture.current?.source === 'keyboard' ? '正在听，松开空格发送' : '正在听，说完点“结束并发送”')
        : pendingCount > 0 ? '正在处理你刚才说的话'
          : inputMode === 'manual' ? '按住空格说话，松开发送' : state === 'wake_standby' && wakeWord ? `说“${wakeWord}”唤醒，也可以按住空格说话` : '免按监听中，也可以按住空格说话')
    return <div className="agent-realtime-voice" data-testid="segmented-voice-controls" data-input-mode={inputMode}>
      <div className="agent-realtime-actions">
        <button type="button" className="primary-button agent-realtime-talk" aria-keyshortcuts="Space"
          disabled={!spaceEnabled || !capture.available || explicitGesture.current?.source === 'keyboard'}
          onClick={event => {
            if (event.detail > 0) event.currentTarget.blur()
            if (explicitGesture.current) finishExplicitGesture('screen')
            else beginExplicitGesture('screen')
          }}>
          {explicitState ? <Square size={16} aria-hidden="true" /> : <Mic size={16} aria-hidden="true" />}
          {explicitState ? '结束并发送' : '开始说话'}
        </button>
        {onInputModeChange && <button type="button" className="secondary-button agent-realtime-mode" role="switch"
          aria-checked={inputMode === 'hands-free'} disabled={!spaceEnabled || Boolean(explicitState)} onClick={event => {
            if (event.detail > 0) event.currentTarget.blur()
            onInputModeChange(inputMode === 'manual' ? 'hands-free' : 'manual')
          }}>免按监听</button>}
      </div>
      <span className="agent-realtime-hint" role="status">{hint}</span>
    </div>
  }

  return (
    <div className="local-input-control hands-free-control">
      <button className={`secondary-button ${state !== 'idle' ? 'is-recording' : ''}`} disabled={!capture.available} title={!capture.available ? '单次录音正在使用麦克风' : undefined} onClick={toggle}>
        {state === 'idle' ? <Mic size={17} /> : <Square size={16} />}
        {recognitionUnavailable && state !== 'idle' ? '语音识别暂不可用 · 正在重试' : state === 'starting' ? '取消打开麦克风' : state === 'playback_tail' ? '正在确认播放尾部静音 · 可停止' : state === 'wake_standby' ? `正在听唤醒词 · ${wakeWord}` : state === 'listening' ? `正在听 · 连续对话${pendingCount > 0 ? ` · ${pendingCount}句处理中` : ''}` : state === 'catching_up' ? `正在处理 · ${pendingCount}句` : wakeWord ? '启动唤醒监听' : '免按键对话'}
      </button>
      {error && <span>{error}</span>}
    </div>
  )
}

export type HouseholdWakeProfile = {
  childId: string
  wakeWord: string
  configured: boolean
  entryPhrase?: string
  handsFree?: boolean
}

export function householdEntryPhrases(profile: HouseholdWakeProfile): string[] {
  const candidates = profile.configured
    ? [profile.wakeWord]
    : [profile.entryPhrase ?? '', profile.wakeWord]
  return [...new Set(candidates.map((value) => value.trim()).filter(Boolean))]
}

export function householdEntryCommand(profile: HouseholdWakeProfile, matchedCommand: string): string {
  return profile.configured ? matchedCommand : ''
}

function householdPromptPhrase(profile: HouseholdWakeProfile): string {
  return profile.configured ? profile.wakeWord : profile.entryPhrase?.trim() || profile.wakeWord
}

export function HouseholdStandbyVoiceInput({ profiles, onWake, enabled = true }: {
  profiles: HouseholdWakeProfile[]
  onWake: (profile: HouseholdWakeProfile, command: string, entry?:import('./types').HouseholdNativeEntry) => void | Promise<void>
  enabled?: boolean
}): React.JSX.Element {
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled
  const active = useRef(false)
  const recorder = useRef<PcmRecorder | null>(null)
  const runAbort = useRef<AbortController | null>(null)
  const heldSpace = useRef<{ ready: boolean; released: boolean } | null>(null)
  const routingWake = useRef(false)
  const generation = useRef(0)
  const privacyPaused = useRef(false)
  const introductionSpoken = useRef(false)
  const introducing = useRef(false)
  const recoveryAttempt = useRef(0)
  const recoveryTimer = useRef<number | null>(null)
  const [recoverySignal, setRecoverySignal] = useState(0)
  const [deviceResolutionSignal, setDeviceResolutionSignal] = useState(0)
  const [audioResolution, setAudioResolution] = useState<{
    status: 'resolving' | 'ready' | 'blocked'
    deviceId?: string
    message?: string
  }>({ status: 'resolving' })
  const [state, updateState] = useState<'idle' | 'starting' | 'listening' | 'recording' | 'processing' | 'error'>('idle')
  const phase = useRef<typeof state>('idle')
  const setState = (value: typeof state): void => { phase.current = value; updateState(value) }
  const [message, setMessage] = useState('正在准备家庭唤醒监听。')
  const capture = useVoiceCaptureOwner('household-standby')
  const profileScope = JSON.stringify(profiles.map(({ childId, wakeWord, configured, entryPhrase, handsFree }) =>
    [childId, wakeWord, configured, entryPhrase, handsFree]))
  const optedIn = profiles.filter((profile) => profile.handsFree)
  const automatic = optedIn.length > 0
  const allPhrases = profiles.map(householdPromptPhrase).join('”或“')
  const standbyMessage = automatic
    ? `待机中，说“${optedIn.map(householdPromptPhrase).join('”或“')}”开始。也可以按住空格说话。`
    : `按住空格说“${allPhrases}”，松开发送。`

  const stop = (reason: string): void => {
    if (introducing.current) { introducing.current = false; cancelLocalSpeech() }
    if (recoveryTimer.current !== null) window.clearTimeout(recoveryTimer.current)
    recoveryTimer.current = null
    generation.current += 1
    active.current = false
    heldSpace.current = null
    runAbort.current?.abort(reason)
    runAbort.current = null
    recorder.current?.dispose()
    recorder.current = null
    releaseVoiceCapture(capture.owner)
    if (reason !== 'household_wake_matched') setState('idle')
  }
  const armSpace = (target: PcmRecorder): boolean => {
    const hold = heldSpace.current
    if (!hold) return false
    return target.beginExplicitTurn(() => {
      if (heldSpace.current !== hold || hold.released) return
      hold.ready = true
      setState('recording')
      setMessage('正在听，松开空格发送。')
    })
  }
  const cancelSpace = (): void => {
    if (!heldSpace.current) return
    heldSpace.current = null
    if (automatic && recorder.current) {
      recorder.current.cancelExplicitTurn()
      setState('listening')
    } else stop('household_space_cancelled')
    if (!privacyPaused.current) setMessage(standbyMessage)
  }

  useEffect(() => {
    const privacyPause = (): void => {
      privacyPaused.current = true
      stop('household_privacy_pause')
      setMessage('家庭麦克风已暂停。')
    }
    window.addEventListener('hsc-privacy-pause', privacyPause)
    return () => {
      window.removeEventListener('hsc-privacy-pause', privacyPause)
      stop('household_standby_unmounted')
    }
  }, [])

  useEffect(() => () => stop('household_profiles_changed'), [profileScope])

  useEffect(() => {
    if (!enabled) {
      stop('household_disabled')
      setMessage('家庭麦克风已暂停。请从下面选择学习档案。')
    }
  }, [enabled])

  useEffect(() => {
    const homeSpace = registerAppSpaceVoice({
      priority: APP_SPACE_VOICE_PRIORITY.general,
      eligible: () => enabledRef.current && profiles.length > 0 && !privacyPaused.current && !routingWake.current
        && audioResolution.status === 'ready' && Boolean(navigator.mediaDevices?.getUserMedia)
        && (!active.current || getVoiceCaptureOwner() === capture.owner),
      begin: () => {
        if (heldSpace.current || introducing.current || phase.current === 'processing' || active.current && !recorder.current) return false
        const owner = getVoiceCaptureOwner()
        if (owner && owner !== capture.owner) return false
        heldSpace.current = { ready: false, released: false }
        if (recorder.current && !armSpace(recorder.current)) {
          heldSpace.current = null
          return false
        }
        setState('starting')
        setMessage('正在准备麦克风，请继续按住空格。')
        if (!recorder.current) {
          setRecoverySignal((value) => value + 1)
        }
        return true
      },
      finish: () => {
        const hold = heldSpace.current
        if (!hold) return
        if (!recorder.current || !hold.ready) {
          cancelSpace()
          setMessage('麦克风还没准备好，请再按住空格。')
          return
        }
        hold.released = true
        recorder.current.finishExplicitTurn()
        setState('processing')
        setMessage('正在识别刚才的话。')
      },
      cancel: cancelSpace,
    })
    const captureChanged = subscribeVoiceCapture(() => {
      homeSpace.refresh()
      if (active.current && getVoiceCaptureOwner() !== capture.owner) stop('household_capture_owner_changed')
    })
    return () => { captureChanged(); homeSpace.unregister() }
  }, [audioResolution.deviceId, audioResolution.status, capture.owner, profiles, standbyMessage, automatic])

  useEffect(() => {
    if (!enabled) return
    const mediaDevices = navigator.mediaDevices
    if (!mediaDevices?.enumerateDevices) {
      setAudioResolution({ status: 'blocked', message: '无法读取麦克风列表，家庭唤醒监听没有启动。' })
      return
    }
    let cancelled = false
    setAudioResolution({ status: 'resolving' })
    setMessage('正在准备Windows默认麦克风。')
    void mediaDevices.enumerateDevices().then(async (devices) => {
      if (cancelled) return
      const match = await resolveCompanionAudioDevice(devices)
      if (cancelled) return
      if (match) {
        setAudioResolution({ status: 'ready', deviceId: match.device?.deviceId })
        recordVoiceDiagnostic('voice.household.microphone_resolved', 'info', {
          selection_method: match.method,
        })
        return
      }
      const message = '无法准备Windows默认麦克风，家庭唤醒监听没有启动。'
      setAudioResolution({ status: 'blocked', message })
      setMessage(message)
      recordVoiceDiagnostic('voice.household.microphone_unresolved', 'error', {
        physical_input_count: devices.filter((item) => item.kind === 'audioinput').length,
      })
    }).catch((cause) => {
      if (cancelled) return
      const message = '无法准备Windows默认麦克风，家庭唤醒监听没有启动。请检查系统权限。'
      setAudioResolution({ status: 'blocked', message })
      setMessage(message)
      recordVoiceDiagnostic('voice.household.microphone_resolution_failed', 'error', { error: cause })
    })
    return () => { cancelled = true }
  }, [deviceResolutionSignal, enabled])

  useEffect(() => {
    const mediaDevices = navigator.mediaDevices
    if (!mediaDevices?.addEventListener) return
    const changed = (): void => {
      stop('household_media_devices_changed')
      setAudioResolution({ status: 'resolving' })
      setDeviceResolutionSignal((value) => value + 1)
    }
    mediaDevices.addEventListener('devicechange', changed)
    return () => mediaDevices.removeEventListener('devicechange', changed)
  }, [])

  useEffect(() => {
    if (!enabled || profiles.length === 0 || audioResolution.status !== 'ready' || privacyPaused.current || routingWake.current) return
    if (!automatic && !heldSpace.current) {
      if (!active.current && phase.current === 'idle') setMessage(standbyMessage)
      return
    }
    if (!capture.available || active.current || !navigator.mediaDevices?.getUserMedia) return
    if (!claimVoiceCapture(capture.owner)) return
    active.current = true
    const runGeneration = ++generation.current
    const controller = new AbortController()
    runAbort.current = controller
    const isCurrent = (): boolean => enabledRef.current && active.current && runGeneration === generation.current && !controller.signal.aborted
      && getVoiceCaptureOwner() === capture.owner
    void (async () => {
      try {
        setState('starting')
        if (automatic && !heldSpace.current && !introductionSpoken.current) {
          introductionSpoken.current = true
          introducing.current = true
          const needsSetup = optedIn.some((profile) => !profile.configured)
          const wakeWords = optedIn.map(householdPromptPhrase).join('”或者“')
          const greeting = needsSetup
            ? `你好，我是学习伙伴。第一次使用，请告诉我你的年级，说“${wakeWords}”。进入后年级会固定在你自己的学习档案，我再带你说出想用的唤醒词。`
            : `我在这里。需要我时，请说“${wakeWords}”。`
          setMessage(greeting)
          recordVoiceDiagnostic('voice.household.initialization_prompt_started', 'info', {
            initialization_required: needsSetup,
          })
          // Do not open the microphone until the identity prompt has finished.
          await new Promise<void>((resolve) => {
            let delivered = false
            const finish = (): void => { window.clearTimeout(timer); unsubscribe(); resolve() }
            const unsubscribe = subscribeCompanionVoice(() => {
              if (delivered && ['idle', 'interrupted', 'error'].includes(getCompanionVoiceState())) finish()
            })
            const timer = window.setTimeout(() => { if (introducing.current) cancelLocalSpeech(); finish() }, 20_000)
            speakLocally(greeting, undefined, () => {
              delivered = true
              if (['idle', 'interrupted', 'error'].includes(getCompanionVoiceState())) finish()
            }, undefined, { localOnly: true })
          })
          introducing.current = false
          recordVoiceDiagnostic('voice.household.initialization_prompt_completed', 'info', {
            initialization_required: needsSetup,
          })
          if (!isCurrent()) return
        }
        const opening = window.setTimeout(() => {
          if (!isCurrent() || recorder.current) return
          stop('household_microphone_timeout')
          setState('error')
          setMessage('打开麦克风超时，请检查设备后再试。')
        }, microphoneOpenTimeoutMs)
        const nextRecorder = await PcmRecorder.start(audioResolution.deviceId, controller.signal, undefined, undefined,
          automatic && !heldSpace.current ? companionPlaybackCaptureCooldownMs : 0)
          .finally(() => window.clearTimeout(opening))
        if (!isCurrent()) { nextRecorder.dispose(); return }
        nextRecorder.setAutomaticEndpointing(automatic)
        recorder.current = nextRecorder
        if (heldSpace.current && !armSpace(nextRecorder)) {
          stop('household_space_recording_unavailable')
          setMessage('麦克风暂未就绪，请再按住空格。')
          return
        }
        const track = nextRecorder.audioTrack()
        const ended = (): void => {
          if (!isCurrent()) return
          stop('household_microphone_disconnected')
          setState('error')
          setMessage(automatic ? '家庭麦克风连接中断，正在自动重连。' : '麦克风已断开，连接后请再按住空格。')
          if (automatic) {
            const delay = microphoneRecoveryDelayMs(++recoveryAttempt.current)
            recoveryTimer.current = window.setTimeout(() => { recoveryTimer.current = null; setRecoverySignal((value) => value + 1) }, delay)
          }
        }
        track.addEventListener?.('ended', ended, { once: true })
        recoveryAttempt.current = 0
        if (!heldSpace.current) {
          setState('listening')
          setMessage(standbyMessage)
        }
        while (isCurrent()) {
          const heardSpeech = await nextRecorder.waitForUtterance()
          if (!isCurrent()) break
          const hold = heldSpace.current
          // In manual mode, an automatic endpoint or late wake can never submit PCM.
          if (hold && !hold.released) {
            if (!automatic) {
              stop('household_space_unreleased')
              setMessage('这段尚未发送，请松开空格后重试。')
              return
            }
            nextRecorder.cancelExplicitTurn()
            heldSpace.current = null
            setState('listening')
            setMessage(standbyMessage)
            continue
          }
          if (hold) heldSpace.current = null
          const continueListening = (message: string): boolean => {
            if (!automatic) stop('household_space_finished')
            else setState('listening')
            setMessage(message)
            return automatic
          }
          if (!heardSpeech || !automatic && !hold) {
            nextRecorder.discardSegment()
            if (!automatic || hold) {
              if (!continueListening('没有收到清楚的语音，请再按住空格。')) return
            }
            continue
          }
          const wav = nextRecorder.takeSegment()
          // Only the completed segment is retained for the local recognition call.
          if (!automatic) {
            nextRecorder.dispose()
            recorder.current = null
          }
          const advice = handsFreeVoiceCaptureAdvice(wav)
          if (!advice.allowTranscription) {
            recordVoiceDiagnostic('voice.capture.signal_rejected', 'warn', { mode: 'household_wake', confidence: advice.confidence, audio_bytes: wav.length })
            if (!continueListening(advice.notice)) return
            continue
          }
          recordVoiceDiagnostic('voice.capture.segment_queued', 'info', {
            policy: 'post_vad_structural_v2',
            audio_bytes: wav.length,
            mode: 'household_wake',
          })
          setState('processing')
          const startedAt = Date.now()
          recordVoiceDiagnostic('voice.asr.started', 'info', { mode: 'household_wake', audio_bytes: wav.length })
          let result
          try {
            result = await localApi.transcribeHouseholdEntry(bytesToBase64(wav), controller.signal)
          } catch (cause) {
            if (!isCurrent()) return
            if (isExpectedNonSpeechAsrError(cause)) {
              recordVoiceDiagnostic('voice.asr.non_speech_discarded', 'info', {
                mode: 'household_wake', reason_code: cause.code,
              })
              if (!continueListening(hold ? '没有收到清楚的语音，请再按住空格。' : standbyMessage)) return
              continue
            }
            throw cause
          }
          recordVoiceDiagnostic('voice.asr.completed', 'info', {
            mode: 'household_wake', latency_ms: Date.now() - startedAt,
            transcript_chars: result.transcript.trim().length, cloud_used: result.cloud_used,
          })
          if (!isCurrent()) break
          const matches = profiles.flatMap((profile) => {
            const match = householdEntryPhrases(profile).map((phrase) => matchWakeWord(result.transcript, phrase)).find((candidate) => candidate.matched)
            return match ? [{ profile, match }] : []
          })
          const matched = matches.length === 1 ? matches[0] : undefined
          if (!matched || !hold && !matched.profile.handsFree) {
            const notice = matches.length > 1 ? '这句话匹配到多个学习档案，请只说一个唤醒词或年级。'
              : hold ? '没有确认学习档案，请按住空格说出你的唤醒词或年级。'
                : standbyMessage
            if (!continueListening(notice)) return
            continue
          }
          routingWake.current = true
          stop('household_wake_matched')
          recordVoiceDiagnostic('voice.household.identity_selected', 'info', {
            selection_method: matched.profile.configured ? 'wake_word' : 'spoken_grade',
            initialization_required: !matched.profile.configured,
          })
          setMessage(matched.profile.configured ? `已识别${matched.profile.wakeWord}，正在进入。` : '已识别年级，正在进入你的固定学习档案。')
          // The grade phrase only selects the durable child profile. It must
          // never leak into onboarding as a wake-word candidate or command.
          try {
            await onWake(matched.profile, householdEntryCommand(matched.profile, matched.match.command),
              result.entry_receipt?{entry_receipt:result.entry_receipt,transcript:result.transcript}:undefined)
          } finally { routingWake.current = false }
          return
        }
      } catch (cause) {
        if (!isCurrent()) return
        recordVoiceDiagnostic('voice.household_listener.failed', 'error', { error: cause })
        stop('household_microphone_failed')
        setState('error')
        setMessage(isMediaDeviceError(cause) ? mediaDeviceFailure(cause, '麦克风').message
          : cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : '家庭唤醒监听暂时不可用。')
        if (automatic && shouldAutomaticallyRecoverMicrophone(cause) && !privacyPaused.current) {
          const delay = microphoneRecoveryDelayMs(++recoveryAttempt.current)
          recoveryTimer.current = window.setTimeout(() => { recoveryTimer.current = null; setRecoverySignal((value) => value + 1) }, delay)
        }
      }
    })()
  }, [audioResolution.deviceId, audioResolution.status, capture.available, profiles, recoverySignal, automatic, standbyMessage, enabled])

  return <div className={`household-standby-voice is-${state}`} role="status" aria-live="polite"><Mic size={18} /><span>{message}</span></div>
}

export function OcrImageInput({ sessionId, onText }: { sessionId: string; onText: (text: string) => void }): React.JSX.Element {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const handle = async (file: File | undefined): Promise<void> => {
    if (!file) return
    if (!file.type.startsWith('image/') || file.size > 16_000_000) {
      setMessage('请选择不超过16MB的图片。')
      return
    }
    setBusy(true); setMessage(null)
    try {
      const bytes = new Uint8Array(await file.arrayBuffer())
      const result = await localApi.runOcr(sessionId, bytesToBase64(bytes))
      if (!result.extracted_text.trim()) setMessage('没有识别到清晰文字，可以重新拍摄或手工输入。')
      else { onText(result.extracted_text); setMessage(`本地识别完成，平均置信度${Math.round(result.confidence * 100)}%。请确认后再加入作业。`) }
    } catch (cause) { setMessage(cause instanceof ApiError ? cause.message : '图片识别失败。') } finally { setBusy(false) }
  }
  return (
    <div className="local-input-control">
      <label className="secondary-button file-input-button"><FileScan size={17} />{busy ? '本地识别中' : '从高拍仪或图片识别'}<input type="file" accept="image/*" disabled={busy} onChange={(event) => void handle(event.target.files?.[0])} /></label>
      {message && <span>{message}</span>}
    </div>
  )
}

const companionSpeechArbiter = new SpeechArbiter(() => stopCompanionSpeech())
configureListenReadyPlayback({cancel:key=>companionSpeechArbiter.cancel(key),
  tone:(attempt,sessionId)=>playListenReadyTone(companionSpeechArbiter,attempt,sessionId),
  guidance:(attempt,sessionId)=>new Promise(resolve=>{
    const text=[...(attempt.repeatRequired?[REPEAT_RESPONSE]:[]),...(attempt.guidanceRequired?[LISTEN_READY_GUIDANCE]:[])].join(' ')
    let actual:{id:string;generation:number;kind:'local_sapi'|'local_pcm'}|null=null,settled=false
    const done=(natural:ListenReadyNoticeEnd|false)=>{if(settled)return;settled=true;resolve(natural)}
    speakLocally(text,sessionId,undefined,()=>{
      const lease=getCompanionPlaybackLease()
      if(!attempt.current()||!lease||lease.sessionId!==sessionId||lease.text!==text
        ||!['local_sapi','local_pcm'].includes(lease.kind)){done(false);return}
      actual={id:lease.id,generation:lease.generation,kind:lease.kind as 'local_sapi'|'local_pcm'}
    },{localOnly:true,priority:'reminder',speechKey:attempt.key,expiresAt:attempt.deadline,isCurrent:attempt.current,
      onPlaybackEnd:()=>done(actual&&attempt.current()&&getCompanionVoiceState()==='idle'
        ?{naturalEnd:true,endedAt:Date.now(),sessionId,leaseId:actual.id,leaseGeneration:actual.generation,leaseKind:actual.kind}:false),onNotDelivered:()=>done(false)})
  })})


export function queueManagedReply(provider: import('./companionVoice').TtsProvider, text: string, sessionId: string,
  onPlaybackStart?: () => void, options: CompanionSpeechOptions = {}): Promise<CompanionSpeechResult> {
  options = { ...options, localPlaybackSessionId: sessionId }
  return new Promise(resolve => {
    let result: CompanionSpeechResult = 'text_only'
    companionSpeechArbiter.submit({ scope: sessionId, priority: options.priority, key: options.speechKey,
      expiresAt: options.expiresAt, isCurrent: options.isCurrent, beforePlay: options.beforePlayback,
      play: (finish, unavailable) => {
        void speakCompanion(text, provider, undefined, () => {
          noteLocalCompanionSpeech(text, Date.now(), sessionId); onPlaybackStart?.()
        }, { ...options, localOnly: false, managedVoiceRequired: true,
          onPlaybackEnd: () => { options.onPlaybackEnd?.(); finish() },
        }).then(value => { result = value; if (value !== 'managed') unavailable() }).catch(unavailable)
      },
      settled: disposition => {
        if (disposition !== 'finished') options.onNotDelivered?.(disposition)
        resolve(disposition === 'finished' ? 'managed' : result)
      },
    })
  })
}

export function queueLocalPcm(pcm: ArrayBuffer, sampleRate: number, sessionId: string,
  onPlaybackStart?: () => void, options: CompanionSpeechOptions = {}): Promise<SpeechDisposition> {
  options = { ...options, localPlaybackSessionId: sessionId }
  return new Promise(resolve => {
    companionSpeechArbiter.submit({ scope: sessionId, priority: 'lesson', key: options.speechKey,
      expiresAt: options.expiresAt, isCurrent: options.isCurrent,
      beforePlay: options.beforePlayback,
      playbackTimeoutMs: pcm.byteLength / (sampleRate * 2) * 1000 + 15_000,
      play: (finish, unavailable) => {
        void playLocalPcm(pcm, sampleRate, onPlaybackStart, options).then(result => {
          if (result === 'finished') finish()
          else unavailable()
        }).catch(unavailable)
      },
      settled: result => {
        try { if (result !== 'finished') options.onNotDelivered?.(result) }
        finally { resolve(result) }
      },
    })
  })
}

export function speakLocally(text: string, sessionId?: string, onResult?: (result: CompanionSpeechResult) => void, onPlaybackStart?: () => void, options: CompanionSpeechOptions = {}): void {
  if (!text.trim()) { onResult?.('text_only'); return }
  options = { ...options, localPlaybackSessionId: sessionId }
  let dropped = false, notified = false
  let unsubscribe: (() => void) | undefined
  const notify = (result: CompanionSpeechResult): void => {
    if (dropped || notified) return
    notified = true
    recordVoiceDiagnostic('voice.tts.delivery', result === 'text_only' ? 'error' : result === 'managed' ? 'info' : 'warn', { delivery: result })
    onResult?.(result)
  }
  companionSpeechArbiter.submit({ scope: sessionId ?? 'initialization', priority: options.priority,
    expiresAt: options.expiresAt, key: options.speechKey, isCurrent: options.isCurrent,
    beforePlay: options.beforePlayback,
    play: (finish, unavailable) => {
      unsubscribe = subscribeCompanionVoice(state => { if (state === 'error') unavailable() })
      void speakCompanion(text, sessionId && !options.localOnly ? new LocalAgentStreamingTtsProvider(sessionId, options) : null, companionSpeechCache, () => {
        if (dropped) return
        noteLocalCompanionSpeech(text, Date.now(), sessionId)
        onPlaybackStart?.()
      }, { ...options, onPlaybackEnd: () => {
        if (dropped) return
        options.onPlaybackEnd?.()
        finish()
      } }).then(result => {
        notify(result)
        if (result === 'text_only') unavailable()
      }).catch(() => unavailable())
    },
    settled: disposition => {
      unsubscribe?.()
      if (disposition === 'unavailable') notify('text_only')
      dropped = disposition !== 'finished'
      if (disposition !== 'finished') options.onNotDelivered?.(disposition)
      recordVoiceDiagnostic('voice.tts.arbitration', disposition === 'unavailable' || disposition === 'full' ? 'warn' : 'info',
        { disposition, priority: options.priority ?? 'dialogue' })
    },
  })
}

export function cancelExactLocalSpeech(id: string, generation: number, includePreparing = false): boolean {
  if (!companionPlaybackLeaseMatches(id, generation, includePreparing)) return false
  companionSpeechArbiter.cancel()
  return true
}

export function cancelKeyedLocalSpeech(speechKey: string): void {
  companionSpeechArbiter.cancel(speechKey)
}

export function cancelLocalSpeech(speechKey?: string): void {
  cancelListenReady()
  revokeOnboardingResponses()
  revokeOrdinaryResponses()
  companionSpeechArbiter.cancel(speechKey)
}

type OriginalPrefixPolicy = { eligible: () => boolean; discarded: () => void }
type OnboardingRejectedUtterance = {blocked:boolean}
type ResponseCaptureScope={owner:string;rejectedUtterance:OnboardingRejectedUtterance;isCurrent:()=>boolean;onUnsupported:()=>void;onQuiet?:(capture:ListenReadyCapture)=>void;onCueReady?:()=>void;onRepeatGiven?:()=>void}
type OnboardingRecorderScope = {sessionId:string;childId:string;response?:ResponseCaptureScope;ordinary?:ResponseCaptureScope}

class PcmRecorder {
  private releaseResponseCapture: (() => void) | undefined
  private responseRegistration: { policy: ResponseCaptureScope; lane: 'onboarding' | 'ordinary'; track: MediaStreamTrack; mode: ReturnType<typeof microphoneEchoMode> } | null = null
  private responseGrant: OnboardingResponseGrant | OrdinaryResponseGrant | null = null
  private responseUnsubscribe: (() => void) | undefined
  private unsupportedResponse: OnboardingResponseGrant | OrdinaryResponseGrant | null = null
  // A refused response is one utterance, even after its grant or tail expires.
  // Only observed silence PCM can release it; never retain the rejected audio.
  private get rejectedResponse(): boolean {return (this.responseScope?.response?.isCurrent()?this.responseScope.response:this.responseScope?.ordinary)?.rejectedUtterance.blocked===true}
  private set rejectedResponse(blocked:boolean) {const holder=this.responseScope?.response?.isCurrent()?this.responseScope.response:this.responseScope?.ordinary;if(holder)holder.rejectedUtterance.blocked=blocked}
  private readonly cueActivity = new AdaptiveVoiceActivityDetector()
  private rejectedResponseQuietMs = 0
  private rejectedResponseFrameAt = 0
  private readonly rejectedResponseActivity = new AdaptiveVoiceActivityDetector()
  private protectedControl: ProtectedPlaybackControl | null = null
  private attentionControl: LocalAttentionControl | null = null
  private protectedInputHeld = false
  private bargeInputHeld = false
  private bargeEscrow: SegmentedBargeEscrow | null = null
  private readonly protectedCaptureEpoch = Symbol()
  private normalInputBlocked(): boolean {
    const state=getCompanionVoiceState()
    return this.protectedInputHeld||this.bargeInputHeld||protectedPlaybackBarrier.held()||state==='speaking'||Date.now()<this.playbackBlockedUntil
  }
  enableProtectedControl(options: {sessionId:string;childId:string;leaseId:string;isCurrent:()=>boolean;onHold:(held:boolean)=>void;onFailure:()=>void;attentionWakeWord?:()=>string|undefined;onAttentionWake?:()=>void}): void {
    if(this.disposed||this.protectedControl)return
    const capture=()=>!this.disposed&&options.isCurrent()?{sessionId:options.sessionId,childId:options.childId,inputKind:'segmented' as const,
      localTrack:this.audioTrack(),captureEpoch:this.protectedCaptureEpoch,submissionBlocked:this.normalInputBlocked()}:null
    const current=()=>{const owned=capture(),lease=getCompanionPlaybackLease();return owned&&lease?{...owned,lease}:null}
    const recognize:ConstructorParameters<typeof ProtectedPlaybackControl>[0]['recognize']=async(session,payload,signal)=>{
        if(!payload.attention_wake_word)return localApi.localPlaybackControl(session,payload,signal)
        const started=Date.now()
        recordVoiceDiagnostic('voice.wake.control_started','info',{mode:'wake',local_only:true})
        try{
          const result=await localApi.localPlaybackControl(session,payload,signal)
          recordVoiceDiagnostic('voice.wake.control_result','info',{mode:'wake',local_only:true,woke:result.control==='wake_companion',elapsed_ms:Date.now()-started})
          return result
        }catch(error){recordVoiceDiagnostic('voice.wake.control_result','warn',{mode:'wake',local_only:true,woke:false,elapsed_ms:Date.now()-started});throw error}
      }
    const holdProtected=(held:boolean)=>{const changed=this.protectedInputHeld!==held;this.protectedInputHeld=held;if(held)this.clearCapturedAudio();if(changed)options.onHold(held||this.bargeInputHeld)}
    this.protectedControl=new ProtectedPlaybackControl({capture,current,recognize,
      cancelExact:cancelExactLocalSpeech,attentionWakeWord:options.attentionWakeWord,onAttentionWake:options.onAttentionWake,
      holdCloud:holdProtected,
      failClosed:()=>{recordVoiceDiagnostic('voice.capture.protected_tail_failed','warn',{reason_code:'quiet_not_verified'});options.onFailure()}})
    this.attentionControl=new LocalAttentionControl({capture,current,recognize,
      word:()=>options.attentionWakeWord?.(),cancelExact:cancelExactLocalSpeech,holdInput:holdProtected,
      onWake:()=>options.onAttentionWake?.()})
    this.bargeEscrow=new SegmentedBargeEscrow({capture:()=>{
      const policy=this.responseScope?.ordinary,track=this.audioTrack(),stamp=voiceCaptureScopeStamp(options.sessionId,options.childId)
      return !this.disposed&&options.isCurrent()&&policy?.isCurrent()&&getVoiceCaptureOwner()===policy.owner
        ?{sessionId:options.sessionId,childId:options.childId,track,owner:policy.owner,leaseId:options.leaseId,epoch:this.protectedCaptureEpoch,scopeValue:stamp.value,documentOwner:stamp.documentOwner}:null
    },hold:held=>{const changed=this.bargeInputHeld!==held;this.bargeInputHeld=held;if(held)this.clearCapturedAudio();if(changed)options.onHold(held||this.protectedInputHeld)},
      failClosed:()=>{recordVoiceDiagnostic('voice.capture.unknown_overlap_held','warn',{reason_code:'source_or_close_unknown'});options.onFailure()}})

  }
  private chunks: Float32Array[] = []
  private heardSpeech = false
  private lastSpeechAt = 0
  private speechStartedAt = 0
  private candidateStartedAt = 0
  private lastCandidateAt = 0
  private readonly activity = new AdaptiveVoiceActivityDetector()
  private disposed = false
  private speechActive = false
  private playbackBlockedUntil: number
  private playbackGateActive = false
  private automaticEndpointing = true
  private explicitTurn: 'idle' | 'held' | 'released' = 'idle'
  private explicitStartedAt = 0
  private explicitReady: (() => void) | undefined
  private explicitExpired: (() => void) | undefined
  private unsubscribePlayback: (() => void) | null = null
  private originalPrefix: Float32Array[] = []
  private originalPrefixSamples = 0
  private originalPrefixOnset = 0
  private readonly prefixActivity = new AdaptiveVoiceActivityDetector()
  private awaitingPrefixConfirmation = false
  private constructor(
    private readonly stream: MediaStream,
    private readonly context: AudioContext,
    private readonly source: MediaStreamAudioSourceNode,
    private readonly processor: ScriptProcessorNode,
    private readonly onSpeechActivity?: (active: boolean) => void,
    private readonly onUtteranceStarted?: () => void,
    initialPlaybackCooldownMs = 0,
    private readonly originalPrefixPolicy?: OriginalPrefixPolicy,
    private readonly responseScope?: OnboardingRecorderScope,
    private readonly echoPolicy?: MicrophoneEchoPolicy,
  ) {
    this.playbackBlockedUntil = Date.now() + Math.max(0, initialPlaybackCooldownMs)
  }

  private observePlayback(): void {
    let wasSpeaking=getCompanionVoiceState()==='speaking'
    const hold=():void=>{this.playbackBlockedUntil=Math.max(this.playbackBlockedUntil,Date.now()+companionPlaybackCaptureCooldownMs);this.clearCapturedAudio()}
    if(wasSpeaking)hold()
    this.unsubscribePlayback=subscribeCompanionVoice(state=>{
      const speaking=state==='speaking'
      // A genuine start clears the unfinished utterance immediately, even if
      // no processor frame arrives before the genuine end/interruption.
      if(speaking||wasSpeaking)hold()
      wasSpeaking=speaking
    })
  }

  static async start(audioDeviceId?: string, signal?: AbortSignal, onSpeechActivity?: (active: boolean) => void,
    onUtteranceStarted?: () => void, initialPlaybackCooldownMs = 0, onCaptureReady?: () => void,
    originalPrefixPolicy?: OriginalPrefixPolicy, protectedScope?: OnboardingRecorderScope): Promise<PcmRecorder> {
    if(protectedPlaybackBarrier.held()&&(!protectedScope||!protectedPlaybackBarrier.mayOpen(protectedScope,performance.now())))throw new Error(PLAYBACK_TAIL_FAILURE)
    const opened = await getMicrophoneStream(audioDeviceId, signal)
    const stream = opened.stream
    if (opened.usedDefaultFallback) {
      recordVoiceDiagnostic('voice.realtime.microphone_fallback', 'warn', { name: opened.failureName })
    }
    let context: AudioContext | null = null
    try {
      if (signal?.aborted) throw new DOMException('Microphone opening cancelled.', 'AbortError')
      const track = stream.getAudioTracks()[0]
      if (!track) throw new Error('未获得可用的麦克风音轨。')
      const echoPolicy = await negotiateMicrophoneEchoCancellation(track)
      recordVoiceDiagnostic('voice.capture.echo_cancellation_policy',
        echoPolicy.negotiation === 'rejected' || echoPolicy.negotiation === 'timed_out' ? 'warn' : 'info', {
          echo_mode: echoPolicy.mode, echo_negotiation: echoPolicy.negotiation, acoustic_quality_verified: false,
        })
      if (signal?.aborted) throw new DOMException('Microphone opening cancelled.', 'AbortError')
      if (track.readyState === 'ended') throw new DOMException('Microphone ended during setup.', 'NotReadableError')
      context = new AudioContext()
      const source = context.createMediaStreamSource(stream)
      const processor = context.createScriptProcessor(pcmCaptureBlockSamples(context.sampleRate), 1, 1)
      const instance = new PcmRecorder(stream, context, source, processor, onSpeechActivity, onUtteranceStarted,
        initialPlaybackCooldownMs, originalPrefixPolicy, protectedScope, echoPolicy)
      let frameReceived = false
      processor.onaudioprocess = (event) => {
        if (instance.disposed) return
        const chunk = new Float32Array(event.inputBuffer.getChannelData(0))
        if (!chunk.length) return
        if (!frameReceived) { frameReceived = true; onCaptureReady?.() }
        const now = Date.now()
        let energy = 0
        for (const sample of chunk) energy += sample * sample
        const rms = Math.sqrt(energy / chunk.length)
        const voiceState = getCompanionVoiceState()
        const playbackGate = companionPlaybackCaptureGate(voiceState, now, instance.playbackBlockedUntil)
        instance.playbackBlockedUntil = playbackGate.blockedUntil
        instance.protectedControl?.processFrame(chunk,instance.context.sampleRate)
        instance.attentionControl?.processFrame(chunk,instance.context.sampleRate)
        if(instance.disposed)return
        instance.syncResponseCaptureRegistration()
        instance.bargeEscrow?.processFrame(chunk,instance.context.sampleRate)
        if(instance.disposed)return
        if(instance.protectedInputHeld||protectedPlaybackBarrier.held()||(instance.bargeInputHeld&&!instance.bargeEscrow?.mayObserveRecoveryQuiet(chunk,instance.context.sampleRate))){instance.clearCapturedAudio();return}
        const response=instance.responseScope, responsePolicy=response?.response?.isCurrent()?response.response:response?.ordinary, ownedCapture=instance.responseCapture()
        const cue=ownedCapture&&response?observeListenReady(response.sessionId,response.childId,ownedCapture,
          instance.cueActivity.observe(rms,chunk.length/instance.context.sampleRate*1000).voiced,chunk.length/instance.context.sampleRate*1000,now):{blocked:false,reject:false}
        if(cue.reject)instance.rejectResponseUtterance()
        if(instance.rejectedResponse){
          if(instance.rejectedResponseFrameAt&&now-instance.rejectedResponseFrameAt>500)instance.rejectedResponseQuietMs=0
          instance.rejectedResponseFrameAt=now
          const quiet=instance.rejectedResponseActivity.observe(rms,chunk.length/instance.context.sampleRate*1000)
          instance.rejectedResponseQuietMs=quiet.voiced?0:instance.rejectedResponseQuietMs+chunk.length/instance.context.sampleRate*1000
          if(instance.rejectedResponseQuietMs>=childUtteranceCaptureTiming(true).silenceMs&&!instance.bargeInputHeld){
            instance.rejectedResponse=false;instance.rejectedResponseQuietMs=0;instance.rejectedResponseFrameAt=0;instance.rejectedResponseActivity.reset()
            const quietCapture=instance.responseCapture();if(responsePolicy?.isCurrent()&&quietCapture)responsePolicy.onQuiet?.(quietCapture)
          }
          instance.clearCapturedAudio();return
        }
        // Quiet observers may run in parallel; quarantine never admits normal PCM/ASR.
        if(instance.bargeInputHeld){instance.clearCapturedAudio();return}
        if(cue.blocked){instance.clearCapturedAudio();return}
        if(instance.responseGrant&&!instance.responseCurrent()){instance.rejectResponseUtterance();instance.clearCapturedAudio();return}
        if(instance.explicitTurn==='released'||(!instance.automaticEndpointing&&instance.explicitTurn==='idle'))return
        const availableResponse=!instance.responseGrant&&responsePolicy?.isCurrent()&&response
          ?response.response?.isCurrent()?onboardingResponseGrant(response.sessionId,response.childId):ordinaryResponseGrant(response.sessionId,response.childId):null
        const responseCanBuffer=!!ownedCapture&&!!response&&!!(instance.responseGrant??availableResponse)?.canBuffer(response.sessionId,response.childId,ownedCapture)
        if(playbackGate.blocked&&voiceState!=='speaking'&&availableResponse&&!responseCanBuffer&&instance.unsupportedResponse!==availableResponse){
          instance.unsupportedResponse=availableResponse;responsePolicy?.onUnsupported()}
        if(playbackGate.blocked&&voiceState!=='speaking'&&availableResponse&&!responseCanBuffer
          &&instance.rejectedResponseActivity.observe(rms,chunk.length/instance.context.sampleRate*1000).voiced){
          instance.rejectResponseUtterance();instance.clearCapturedAudio();return
        }
        if (playbackGate.blocked && !responseCanBuffer) {
          if (!instance.playbackGateActive) {
            instance.playbackGateActive = true
            recordVoiceDiagnostic('voice.capture.playback_gate_started')
          }
          if (voiceState !== 'speaking' && (instance.originalPrefixOnset > 0 || originalPrefixPolicy?.eligible())) {
            // Memory-only onset, not human identity or permission to interrupt/submit.
            instance.holdOriginalPrefix(chunk, rms, now)
          } else instance.clearCapturedAudio()
          return
        }
        if (instance.playbackGateActive) {
          instance.playbackGateActive = false
          recordVoiceDiagnostic('voice.capture.playback_gate_ended')
        }
        if(instance.explicitTurn==='held'&&instance.explicitReady){
          const ready=instance.explicitReady;instance.explicitReady=undefined;ready()
          if(instance.explicitTurn!=='held')return
        }
        const activity = instance.activity.observe(rms, chunk.length / instance.context.sampleRate * 1000)
        if (instance.originalPrefix.length) {
          if (instance.originalPrefixOnset && activity.voiced) {
            instance.chunks.push(...instance.originalPrefix)
            instance.candidateStartedAt = instance.originalPrefixOnset
            instance.awaitingPrefixConfirmation = true
            recordVoiceDiagnostic('voice.capture.original_prefix_admitted', 'info', { samples: instance.originalPrefixSamples })
            instance.clearOriginalPrefix(false)
          } else instance.clearOriginalPrefix(true)
        }
        if (instance.awaitingPrefixConfirmation && !activity.voiced && !instance.heardSpeech) {
          // A tail-only sound cannot be prepended to a later, different utterance.
          instance.chunks = []; instance.candidateStartedAt = 0
          instance.awaitingPrefixConfirmation = false
          originalPrefixPolicy?.discarded()
        }
        instance.chunks.push(chunk)
        // Bound idle preroll by audio time, never by the new smaller block count.
        // A current candidate/prefix must keep its original first samples.
        if (!instance.heardSpeech && !activity.voiced && !instance.awaitingPrefixConfirmation) instance.trimIdlePreroll()
        if(activity.voiced&&!instance.responseGrant&&availableResponse&&responseCanBuffer&&ownedCapture&&response){
          if(!availableResponse.claim(response.sessionId,response.childId,ownedCapture)){instance.rejectResponseUtterance();instance.clearCapturedAudio();return}
          instance.responseGrant=availableResponse
          instance.responseUnsubscribe=availableResponse.subscribeRevoked(()=>{
            instance.rejectResponseUtterance();instance.clearCapturedAudio()
          })
        }
        if (!instance.heardSpeech) {
          if (!activity.voiced) { instance.candidateStartedAt = 0; instance.lastCandidateAt = 0 }
          else if (!instance.candidateStartedAt) {
            instance.candidateStartedAt = now
            // Freeze local context at the first candidate frame, not after a
            // page/question change during VAD confirmation. This grants no ASR.
            instance.onUtteranceStarted?.()
          }
          if (activity.voiced) instance.lastCandidateAt = now
        }
        if (activity.speechDetected && !instance.heardSpeech) {
          instance.heardSpeech = true
          instance.awaitingPrefixConfirmation = false
          instance.speechStartedAt = instance.candidateStartedAt || now
          instance.lastSpeechAt = now
        }
        if (instance.heardSpeech && activity.voiced) {
          instance.lastSpeechAt = now
        }
        if (activity.speechDetected && activity.voiced) instance.setSpeechActivity(true)
      }
      source.connect(processor)
      processor.connect(context.destination)
      const settings = stream.getAudioTracks()[0]?.getSettings?.() ?? {}
      recordVoiceDiagnostic('voice.capture.ready', 'info', {
        device_label: stream.getAudioTracks()[0]?.label || '未命名麦克风',
        selection_method: opened.usedDefaultFallback ? 'system_default_fallback' : audioDeviceId ? 'resolved_device_id' : 'system_default',
        sample_rate: settings.sampleRate ?? context.sampleRate,
        pcm_block_samples: processor.bufferSize ?? pcmCaptureBlockSamples(context.sampleRate),
        speech_confirmation_ms: 96,
        channel_count: settings.channelCount ?? 1,
        echo_cancellation: settings.echoCancellation,
        noise_suppression: settings.noiseSuppression,
        auto_gain_control: settings.autoGainControl,
      })
      if (signal?.aborted) { instance.dispose(); throw new DOMException('Microphone opening cancelled.', 'AbortError') }
      instance.observePlayback()
      // Same owner reconnection inherits only refusal, never PCM, track, grant or quiet duration.
      if(instance.rejectedResponse){
        const response=protectedScope?.response?.isCurrent()?protectedScope.response:protectedScope?.ordinary
        if(response?.isCurrent())response.onUnsupported()
      }
      instance.syncResponseCaptureRegistration()
      return instance
    } catch (cause) {
      stream.getTracks().forEach((track) => track.stop())
      if (context) void context.close().catch(() => undefined)
      throw cause
    }
  }

  private rejectResponseUtterance(): void {
    if(this.disposed||this.rejectedResponse)return
    this.rejectedResponse=true;this.rejectedResponseQuietMs=0;this.rejectedResponseFrameAt=0;this.rejectedResponseActivity.reset()
    const response=this.responseScope?.response?.isCurrent()?this.responseScope.response:this.responseScope?.ordinary
    if(response?.isCurrent())response.onUnsupported()
  }

  private syncResponseCaptureRegistration(): void {
    const scope = this.responseScope
    const onboarding = scope?.response?.isCurrent() === true
    const policy = onboarding ? scope?.response : scope?.ordinary
    const track = this.audioTrack()
    const valid = !this.disposed && !!scope && !!policy && policy.isCurrent()
      && !!this.echoPolicy && getVoiceCaptureOwner() === policy.owner
      && track?.readyState === 'live' && track.enabled === true && this.context.state === 'running'
    const lane: 'onboarding' | 'ordinary' = onboarding ? 'onboarding' : 'ordinary'
    const mode = microphoneEchoMode(track)
    const previous = this.responseRegistration
    if (valid && previous?.policy === policy && previous.lane === lane && previous.track === track) {
      if (previous.mode !== mode) {
        const granted = this.responseGrant ?? (onboarding
          ? onboardingResponseGrant(scope.sessionId, scope.childId)
          : ordinaryResponseGrant(scope.sessionId, scope.childId))
        const cuePending = listenReadyPending(scope.sessionId, scope.childId, policy.owner,
          { track, epoch: this.protectedCaptureEpoch })
        // Changing AEC cannot turn a ready answer into an ordinary/cloud tail.
        // Keep the refusal across grant cleanup until actual quiet PCM arrives.
        if (granted || cuePending) {
          this.rejectResponseUtterance()
          granted?.revoke()
          cancelListenReady(scope.sessionId, policy.owner, { track, epoch: this.protectedCaptureEpoch })
          this.clearCapturedAudio()
        }
        // Before natural end this is still the same owned playback and capture.
        // Its response will be frozen from the actual mode at natural end.
        previous.mode = mode
      }
      return
    }
    // A policy/owner change revokes its old registration before registering current metadata.
    const release = this.releaseResponseCapture
    this.releaseResponseCapture = undefined; this.responseRegistration = null
    if (previous && scope) cancelListenReady(scope.sessionId, previous.policy.owner,
      { track: previous.track, epoch: this.protectedCaptureEpoch })
    release?.()
    if (previous) this.clearCapturedAudio()
    if (!valid || !scope || !policy) return
    const capture = this.responseCapture()
    if (!capture) return
    const registration = { policy, lane, track, mode }
    this.responseRegistration = registration
    const current = () => !this.disposed && policy.isCurrent() && getVoiceCaptureOwner() === policy.owner
      && this.audioTrack() === track && track.readyState === 'live' && track.enabled === true
      && this.context.state === 'running' && this.responseRegistration === registration
      && microphoneEchoMode(track) === registration.mode
    this.releaseResponseCapture = onboarding
      ? registerOnboardingCapture(scope.sessionId, scope.childId, capture, current)
      : registerOrdinaryCapture(scope.sessionId, scope.childId, capture, current)
  }

  private responseCapture() {
    const scope=this.responseScope
    const response=scope?.response?.isCurrent()?scope.response:scope?.ordinary
    if(!response||!response.isCurrent()||!this.echoPolicy)return null
    return {owner:response.owner,track:this.audioTrack(),epoch:this.protectedCaptureEpoch,echoPolicy:this.echoPolicy,
      rejectUtterance:()=>this.rejectResponseUtterance(),needsRepeat:()=>this.rejectedResponse,onRepeatGiven:()=>{if(!this.disposed&&response.isCurrent())response.onRepeatGiven?.()},onCueReady:()=>{if(!this.disposed&&response.isCurrent()&&!this.rejectedResponse)response.onCueReady?.()}}
  }
  private responseCurrent(): boolean {
    const scope=this.responseScope,capture=this.responseCapture()
    return !!scope&&!!capture&&this.responseGrant?.capturedCurrent(scope.sessionId,scope.childId,capture)===true
  }
  onboardingCapture(): {grant:OnboardingResponseGrant|OrdinaryResponseGrant;isCurrent:()=>boolean}|undefined {
    const scope=this.responseScope,capture=this.responseCapture(),grant=this.responseGrant
    return scope&&capture&&grant?{grant,isCurrent:()=>!this.disposed&&(scope.response?.isCurrent()?scope.response:scope.ordinary)?.isCurrent()===true
      &&grant.capturedCurrent(scope.sessionId,scope.childId,capture)}:undefined
  }
  completeResponse(grant:OnboardingResponseGrant|OrdinaryResponseGrant):void {if(this.responseGrant===grant)this.clearResponse(false)}
  private clearResponse(revoke=true): void {
    const grant=this.responseGrant;this.responseGrant=null;this.responseUnsubscribe?.();this.responseUnsubscribe=undefined
    if(revoke)grant?.revoke()
  }

  private trimIdlePreroll(): void {
    const maximumSamples = Math.ceil(this.context.sampleRate * 1.4)
    let samples = this.chunks.reduce((total, chunk) => total + chunk.length, 0)
    while (samples > maximumSamples && this.chunks.length > 1) {
      const overflow = samples - maximumSamples
      const first = this.chunks[0]
      if (overflow < first.length) { this.chunks[0] = first.slice(overflow); break }
      samples -= this.chunks.shift()!.length
    }
    if (this.chunks.length === 1 && this.chunks[0].length > maximumSamples) this.chunks[0] = this.chunks[0].slice(-maximumSamples)
  }

  private holdOriginalPrefix(chunk: Float32Array, rms: number, now: number): void {
    this.originalPrefix.push(chunk)
    this.originalPrefixSamples += chunk.length
    const maximumSamples = Math.ceil(this.context.sampleRate * companionPlaybackCaptureCooldownMs / 1000) + chunk.length
    while (this.originalPrefixSamples > maximumSamples && this.originalPrefix.length > 1) {
      this.originalPrefixSamples -= this.originalPrefix.shift()!.length
    }
    const candidate = this.prefixActivity.observe(rms, chunk.length / this.context.sampleRate * 1000)
    if (candidate.voiced && !this.originalPrefixOnset) {
      this.originalPrefixOnset = now
      this.candidateStartedAt = now
      this.onUtteranceStarted?.()
      recordVoiceDiagnostic('voice.capture.original_prefix_held')
    } else if (!candidate.voiced && this.originalPrefixOnset) {
      this.clearOriginalPrefix(true)
    }
    if (candidate.voiced) this.lastCandidateAt = now
  }

  private clearOriginalPrefix(discarded: boolean): void {
    const held = this.originalPrefixOnset > 0
    this.originalPrefix = []; this.originalPrefixSamples = 0; this.originalPrefixOnset = 0
    this.prefixActivity.reset()
    if (discarded && held) {
      if (!this.heardSpeech && !this.awaitingPrefixConfirmation) {
        this.candidateStartedAt = 0; this.lastCandidateAt = 0
      }
      this.originalPrefixPolicy?.discarded()
      recordVoiceDiagnostic('voice.capture.original_prefix_discarded')
    }
  }

  async stop(): Promise<Uint8Array> {
    if (this.disposed) return new Uint8Array()
    if(this.responseGrant||(this.responseScope&&listenReadyPending(this.responseScope.sessionId,this.responseScope.childId,
      this.responseScope.response?.owner??this.responseScope.ordinary?.owner??'',{track:this.audioTrack(),epoch:this.protectedCaptureEpoch})))this.rejectResponseUtterance()
    if(this.responseScope)cancelListenReady(this.responseScope.sessionId,this.responseScope.response?.owner??this.responseScope.ordinary?.owner,
      {track:this.audioTrack(),epoch:this.protectedCaptureEpoch})
    this.disposed = true
    this.releaseResponseCapture?.();this.releaseResponseCapture=undefined
    this.responseRegistration = null
    this.unsubscribePlayback?.();this.unsubscribePlayback=null
    this.protectedControl?.dispose();this.protectedControl=null
    this.attentionControl?.dispose();this.attentionControl=null
    this.bargeEscrow?.dispose();this.bargeEscrow=null
    this.resetSpeechActivity()
    const inputSampleRate = this.context.sampleRate
    this.processor.disconnect()
    this.source.disconnect()
    this.stream.getTracks().forEach((track) => track.stop())
    await this.context.close()
    return this.takeSegment(inputSampleRate)
  }

  capturedSpeechInterval(): CapturedSpeechInterval | undefined {
    return this.heardSpeech ? { startedAt: this.speechStartedAt, endedAt: this.lastSpeechAt } : undefined
  }

  private setSpeechActivity(active: boolean): void {
    if (this.speechActive === active) return
    this.speechActive = active
    this.onSpeechActivity?.(active)
  }

  resetSpeechActivity(): void {
    this.setSpeechActivity(false)
  }

  takeSegment(inputSampleRate = this.context.sampleRate): Uint8Array {
    this.resetExplicitTurn()
    if(this.rejectedResponse||this.normalInputBlocked()){this.clearCapturedAudio();return new Uint8Array()}
    if(this.responseGrant&&!this.responseCurrent()){this.clearCapturedAudio();return new Uint8Array()}
    const chunks = this.chunks
    this.clearResponse(false)
    this.chunks = []
    this.clearOriginalPrefix(true)
    this.awaitingPrefixConfirmation = false
    this.heardSpeech = false
    this.lastSpeechAt = 0
    this.speechStartedAt = 0
    this.candidateStartedAt = 0
    this.lastCandidateAt = 0
    this.activity.reset()
    this.resetSpeechActivity()
    const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
    const source = new Float32Array(total)
    let offset = 0
    chunks.forEach((chunk) => { source.set(chunk, offset); offset += chunk.length })
    return encodeWav(resample(source, inputSampleRate, 16000), 16000)
  }

  private clearCapturedAudio(): void {
    this.clearResponse()
    const candidateHeld = this.awaitingPrefixConfirmation
    this.clearOriginalPrefix(true)
    this.awaitingPrefixConfirmation = false
    if (candidateHeld) this.originalPrefixPolicy?.discarded()
    this.chunks = []
    this.heardSpeech = false
    this.lastSpeechAt = 0
    this.speechStartedAt = 0
    this.candidateStartedAt = 0
    this.lastCandidateAt = 0
    this.activity.reset()
    this.resetSpeechActivity()
  }

  discardSegment(): void {
    this.resetExplicitTurn()
    this.clearCapturedAudio()
  }

  setAutomaticEndpointing(enabled: boolean): void {
    if (this.automaticEndpointing === enabled) return
    this.automaticEndpointing = enabled
    this.discardSegment()
  }

  beginExplicitTurn(onReady?: () => void, onExpired?: () => void): boolean {
    if (this.disposed || this.explicitTurn !== 'idle') return false
    this.clearCapturedAudio()
    this.explicitTurn = 'held'
    this.explicitStartedAt = Date.now()
    this.explicitReady = onReady
    this.explicitExpired = onExpired
    return true
  }

  finishExplicitTurn(): void {
    if (this.explicitTurn === 'held') this.explicitTurn = 'released'
  }

  cancelExplicitTurn(): void {
    if (this.explicitTurn !== 'idle') this.discardSegment()
  }

  private resetExplicitTurn(): void {
    this.explicitTurn = 'idle'
    this.explicitStartedAt = 0
    this.explicitReady = undefined
    this.explicitExpired = undefined
  }

  async waitForUtterance(maximumMs = 12_000, silenceMs = 900, maximumUtteranceMs = 8_000): Promise<boolean> {
    const startedAt = Date.now()
    const timing = { maximumMs, silenceMs, maximumUtteranceMs }
    return new Promise((resolve) => {
      const timer = window.setInterval(() => {
        const now = Date.now()
        if (this.disposed) { window.clearInterval(timer); resolve(false); return }
        if (this.explicitTurn === 'held') {
          if (now-this.explicitStartedAt<30_000) return
          const expired=this.explicitExpired
          this.discardSegment()
          expired?.()
          window.clearInterval(timer);resolve(false);return
        }
        if (this.explicitTurn === 'released') {
          window.clearInterval(timer)
          resolve(this.heardSpeech)
          return
        }
        if (!this.automaticEndpointing) return
        const onset = this.speechStartedAt || this.candidateStartedAt
        const lastActivityAt = this.heardSpeech ? this.lastSpeechAt : this.lastCandidateAt || onset
        // A claimed reply is one local utterance. Its grant's fixed 12s capture
        // deadline rejects/wipes the whole sentence; the ordinary 8s split must
        // never submit its prefix and route the remaining words as a new answer.
        if(this.responseGrant&&!this.responseCurrent()){this.rejectResponseUtterance();this.clearCapturedAudio()}
        const finished=this.responseGrant
          ? onset>0&&now-lastActivityAt>=silenceMs
          : shouldFinishCapturedUtterance(now, startedAt, onset, lastActivityAt, timing)
        if (finished) {
          window.clearInterval(timer)
          if (this.heardSpeech) recordVoiceDiagnostic('voice.capture.utterance_detected', 'info', { capture_ms: now - startedAt })
          resolve(this.heardSpeech)
        }
      }, 100)
    })
  }

  audioTrack(): MediaStreamTrack {
    return this.stream.getAudioTracks()[0]
  }

  dispose(): void {
    if (this.disposed) return
    if(this.responseGrant||(this.responseScope&&listenReadyPending(this.responseScope.sessionId,this.responseScope.childId,
      this.responseScope.response?.owner??this.responseScope.ordinary?.owner??'',{track:this.audioTrack(),epoch:this.protectedCaptureEpoch})))this.rejectResponseUtterance()
    if(this.responseScope)cancelListenReady(this.responseScope.sessionId,this.responseScope.response?.owner??this.responseScope.ordinary?.owner,
      {track:this.audioTrack(),epoch:this.protectedCaptureEpoch})
    this.disposed = true
    this.releaseResponseCapture?.();this.releaseResponseCapture=undefined
    this.responseRegistration = null
    this.unsubscribePlayback?.();this.unsubscribePlayback=null
    this.protectedControl?.dispose();this.protectedControl=null
    this.attentionControl?.dispose();this.attentionControl=null
    this.bargeEscrow?.dispose();this.bargeEscrow=null
    this.resetExplicitTurn()
    this.clearCapturedAudio()
    this.resetSpeechActivity()
    this.processor.disconnect()
    this.source.disconnect()
    this.stream.getTracks().forEach((track) => track.stop())
    void this.context.close()
  }
}

/** Browser media requests cannot be force-aborted; an aborted late stream is stopped before use. */
async function getMicrophoneStream(audioDeviceId?: string, signal?: AbortSignal): Promise<{ stream: MediaStream; usedDefaultFallback: boolean; failureName?: string }> {
  const open = (deviceId?: string): Promise<MediaStream> => navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      ...(deviceId ? { deviceId: { exact: deviceId } } : {}) },
    video: false,
  })
  const requested = open(audioDeviceId).then(
    (stream) => ({ stream, usedDefaultFallback: false }),
    async (cause) => {
      const failureName = browserMediaErrorName(cause) || 'Error'
      if (!audioDeviceId || ['NotAllowedError', 'PermissionDeniedError', 'SecurityError'].includes(failureName)) throw cause
      return { stream: await open(), usedDefaultFallback: true, failureName }
    },
  )
  if (!signal) return requested
  return new Promise<{ stream: MediaStream; usedDefaultFallback: boolean; failureName?: string }>((resolve, reject) => {
    const abort = (): void => reject(new DOMException('Microphone opening cancelled.', 'AbortError'))
    if (signal.aborted) { void requested.then((opened) => opened.stream.getTracks().forEach((track) => track.stop())).catch(() => undefined); abort(); return }
    signal.addEventListener('abort', abort, { once: true })
    void requested.then((opened) => {
      signal.removeEventListener('abort', abort)
      if (signal.aborted) { opened.stream.getTracks().forEach((track) => track.stop()); abort(); return }
      resolve(opened)
    }, (cause) => { signal.removeEventListener('abort', abort); reject(cause) })
  })
}

export function noteLocalCompanionSpeech(text: string, startedAt = Date.now(), sessionId?: string): void {
  // Called only by the playback-start callback, not while TTS is being fetched.
  // The browser fallback promise resolves before playback ends; state changes
  // cover both that path and managed playback, including interruption/error.
  for (const previous of recentLocalSpeech) {
    if (previous.endedAt === null) previous.endedAt = startedAt
  }
  const playbackId = sessionId ? crypto.randomUUID() : undefined
  const speech: LocalSpeechEchoGuard = { text: text.slice(0, 4000), startedAt, endedAt: null, sessionId, playbackId }
  if (sessionId && playbackId) speech.registered = localApi.voicePlayback(sessionId, { playback_id: playbackId, text: speech.text, event: 'started' }).then(() => true, () => false)
  recentLocalSpeech = [...recentLocalSpeech.filter((item) => startedAt - item.startedAt <= 180_000), speech].slice(-32)
  const unsubscribe = subscribeCompanionVoice((state) => {
    if (state === 'speaking') return
    if (speech.endedAt === null) speech.endedAt = Date.now()
    if (sessionId && playbackId) void speech.registered?.then((registered) => {
      if (registered) void localApi.voicePlayback(sessionId, { playback_id: playbackId, text: speech.text, event: 'ended' }).catch(() => undefined)
    })
    unsubscribe()
  })
}

export async function localPlaybackOverlap(sessionId: string, transcript: string, interval?: CapturedSpeechInterval): Promise<PlaybackOverlap | undefined> {
  if (!interval || !Number.isFinite(interval.startedAt) || !Number.isFinite(interval.endedAt)
    || interval.startedAt >= interval.endedAt || interval.endedAt > Date.now()) return undefined
  // Preserve mathematical punctuation/signs and mixed new content. This is
  // provenance uncertainty, never a reason to discard a child's repetition.
  const normalize = (text: string): string => text.replace(/\s/g, '').replace(/[，。！？!?、；;：:]+$/g, '')
  const heard = normalize(transcript)
  if (heard.length < 16) return undefined
  for (const item of [...recentLocalSpeech].reverse()) {
    if (item.sessionId !== sessionId || !item.playbackId || Date.now() - item.startedAt > 180_000
      || interval.startedAt < item.startedAt || interval.endedAt > (item.endedAt ?? Date.now())) continue
    const spoken = normalize(item.text)
    if (!spoken.includes(heard)) continue
    if (await item.registered) return { playback_id: item.playbackId }
  }
  return undefined
}

export function isLikelyLocalSpeechEcho(transcript: string, capturedSpeech?: CapturedSpeechInterval): boolean {
  return stripLocalCompanionSpeechEcho(transcript, capturedSpeech).echoDetected
}

export function stripLocalCompanionSpeechEcho(transcript: string, capturedSpeech?: CapturedSpeechInterval): {
  transcript: string
  // Legacy field name retained for call-site compatibility. A true value is a
  // high-confidence playback echo and must not become a child turn.
  echoDetected: boolean
  removedCharacters: number
} {
  const retained = { transcript, echoDetected: false, removedCharacters: 0 }
  if (!capturedSpeech || !Number.isFinite(capturedSpeech.startedAt) || !Number.isFinite(capturedSpeech.endedAt)
    || capturedSpeech.startedAt >= capturedSpeech.endedAt || capturedSpeech.endedAt > Date.now()) return retained
  // Decimal points, brackets, powers, signs and case can change an answer.
  // Only ignore whitespace and terminal sentence punctuation.
  const normalize = (value: string): string => value.replace(/\s/g, '').replace(/[，。！？,.!?、；;：:]+$/g, '')
  const heard = normalize(transcript)
  if (heard.length < 16) return retained
  for (const item of [...recentLocalSpeech].reverse()) {
    if (capturedSpeech.startedAt < item.startedAt || capturedSpeech.endedAt > (item.endedAt ?? Date.now())) continue
    const spoken = normalize(item.text)
    // Only discard speech contained in an actual playback window and carrying
    // no new semantic material. Short answers and mixed barge-in speech remain
    // child turns; a recitation after playback ends remains valid as well.
    if (heard === spoken || (spoken.includes(heard) && heard.length / Math.max(1, spoken.length) >= 0.8)) {
      return { transcript: '', echoDetected: true, removedCharacters: transcript.length }
    }
  }
  return retained
}

/** Standby filtering is not a semantic decision about an active child's reply. */
export function isLowInformationVoiceTranscript(transcript: string, dialogue = false): boolean {
  const compact = transcript.trim().replace(/[\s，。！？,.!?~～·…]/g, '')
  if (!compact) return true
  return !dialogue && /^(?:啊|嗯|呃|额|哦|噢|哼|哈|哎|唉){1,3}$/.test(compact)
}

export function shouldUseStandbyTranscription(wakeWord: string | undefined, conversationActive: boolean): boolean {
  return Boolean(wakeWord && !conversationActive)
}

export function childUtteranceCaptureTiming(conversationActive: boolean): UtteranceCaptureTiming {
  return conversationActive
    ? { maximumMs: 12_000, silenceMs: 1_050, maximumUtteranceMs: 8_000 }
    : { maximumMs: 10_000, silenceMs: 800, maximumUtteranceMs: 6_000 }
}

/** Signal-quality policy, not a fabricated ASR-model confidence. */
export function voiceCaptureSignalAdvice(wav: Uint8Array): { allowTranscription: boolean; confidence: number; notice: string } {
  if (wav.length < 44) return { allowTranscription: false, confidence: 0, notice: '录音无效，请重新说一遍。' }
  const view = new DataView(wav.buffer, wav.byteOffset, wav.byteLength)
  const sampleRate = view.getUint32(24, true)
  const samples = Math.floor((wav.length - 44) / 2)
  if (sampleRate < 8_000 || samples < Math.max(1, Math.floor(sampleRate * 0.35))) return { allowTranscription: false, confidence: 0, notice: '这句话太短，请靠近麦克风完整说一遍。' }
  const frameSamples = Math.max(80, Math.floor(sampleRate * 0.02))
  const frames: Array<{ rms: number; clipped: number; count: number }> = []
  for (let start = 0; start < samples; start += frameSamples) {
    const end = Math.min(samples, start + frameSamples)
    let energy = 0; let clipped = 0
    for (let index = start; index < end; index += 1) {
      const value = view.getInt16(44 + index * 2, true) / 32768
      energy += value * value
      if (Math.abs(value) > 0.98) clipped += 1
    }
    frames.push({ rms: Math.sqrt(energy / Math.max(1, end - start)), clipped, count: end - start })
  }
  const sortedRms = frames.map((frame) => frame.rms).sort((left, right) => left - right)
  const noiseFloor = sortedRms[Math.min(sortedRms.length - 1, Math.floor(sortedRms.length * 0.2))] ?? 0
  // Far-field document-camera microphones often include several seconds of
  // silence before speech. Judge active frames instead of diluting speech into
  // one whole-segment average, while keeping steady low-level room noise out.
  const activeThreshold = Math.min(0.02, Math.max(0.004, noiseFloor * 2.2))
  const active = frames.filter((frame) => frame.rms >= activeThreshold)
  let longestActiveRun = 0; let currentActiveRun = 0
  for (const frame of frames) {
    if (frame.rms >= activeThreshold) {
      currentActiveRun += 1
      longestActiveRun = Math.max(longestActiveRun, currentActiveRun)
    } else currentActiveRun = 0
  }
  const activeDurationMs = active.length * 20
  const continuousActiveMs = longestActiveRun * 20
  const activeRms = Math.sqrt(active.reduce((sum, frame) => sum + frame.rms * frame.rms, 0) / Math.max(1, active.length))
  const activeSamples = active.reduce((sum, frame) => sum + frame.count, 0)
  const clipRatio = active.reduce((sum, frame) => sum + frame.clipped, 0) / Math.max(1, activeSamples)
  const confidence = Math.max(0, Math.min(1, 0.55 * Math.min(1, activeRms / 0.05) + 0.3 * Math.min(1, activeDurationMs / 900) + 0.15 * (1 - Math.min(1, clipRatio / 0.08))))
  // This is only a conservative near-silence/impulse gate. Ambiguous far-field
  // speech must reach cloud ASR; semantic filtering after transcription is much
  // safer than discarding a child's short or quiet utterance here.
  if (activeDurationMs < 140 || continuousActiveMs < 100 || activeRms < 0.0032) return { allowTranscription: false, confidence, notice: '声音太轻或只有短促环境声，没有发送识别；请靠近麦克风完整说一遍。' }
  if (clipRatio > 0.08) return { allowTranscription: false, confidence, notice: '录音有明显爆音，暂不发送识别；请稍微离麦克风远一点后重说。' }
  return { allowTranscription: true, confidence, notice: '录音信号质量可用于本地识别；识别文字仍请核对。' }
}

/** The streaming VAD has already required sustained activity before this runs.
 * Do not apply a second energy threshold here: far-field child speech can be
 * quieter than keyboard impulses. Cloud ASR and transcript filters decide
 * whether the detected segment contains language. */
export function handsFreeVoiceCaptureAdvice(wav: Uint8Array): { allowTranscription: boolean; confidence: number; notice: string } {
  if (wav.length < 44) return { allowTranscription: false, confidence: 0, notice: '录音无效，正在继续监听。' }
  const view = new DataView(wav.buffer, wav.byteOffset, wav.byteLength)
  const sampleRate = view.getUint32(24, true)
  const samples = Math.floor((wav.length - 44) / 2)
  if (sampleRate < 8_000 || samples < Math.max(1, Math.floor(sampleRate * 0.12))) {
    return { allowTranscription: false, confidence: 0, notice: '只听到很短的声音，正在继续监听。' }
  }
  let clipped = 0
  for (let index = 0; index < samples; index += 1) {
    if (Math.abs(view.getInt16(44 + index * 2, true)) >= 32_112) clipped += 1
  }
  const clipRatio = clipped / Math.max(1, samples)
  if (clipRatio > 0.2) return { allowTranscription: false, confidence: 0.2, notice: '声音严重爆音，正在继续监听。' }
  return { allowTranscription: true, confidence: 1 - clipRatio, notice: '已交给云端语音识别。' }
}

/** Calibration gate based only on waveform statistics, never an ASR confidence. */
export function voiceCalibrationQuality(wav: Uint8Array): { accepted: boolean; code: string; message: string; metrics: Record<string, number | boolean> } {
  if (wav.length < 44) return { accepted: false, code: 'INVALID_AUDIO', message: '没有得到有效录音，请重试。', metrics: {} }
  const view = new DataView(wav.buffer, wav.byteOffset, wav.byteLength)
  const rate = view.getUint32(24, true); const count = Math.floor((wav.length - 44) / 2)
  if (rate < 8_000 || count < rate) return { accepted: false, code: 'TOO_SHORT', message: '请完整说一句，至少约一秒。', metrics: { sample_rate: rate, duration_ms: count / Math.max(rate, 1) * 1000 } }
  const frame = Math.max(80, Math.floor(rate * 0.02)); let voiced = 0; let frames = 0; let noiseEnergy = 0; let noiseFrames = 0; let peak = 0; let clipped = 0; let totalEnergy = 0
  for (let start = 0; start < count; start += frame) {
    let energy = 0; const end = Math.min(count, start + frame)
    for (let index = start; index < end; index += 1) { const value = view.getInt16(44 + index * 2, true) / 32768; energy += value * value; peak = Math.max(peak, Math.abs(value)); if (Math.abs(value) > 0.98) clipped += 1 }
    const rms = Math.sqrt(energy / Math.max(1, end - start)); totalEnergy += energy; frames += 1
    if (rms >= 0.018) voiced += 1; else { noiseEnergy += energy; noiseFrames += 1 }
  }
  const rms = Math.sqrt(totalEnergy / Math.max(1, count)); const speechRatio = voiced / Math.max(1, frames); const noiseFloor = Math.sqrt(noiseEnergy / Math.max(1, noiseFrames * frame)); const clipRatio = clipped / Math.max(1, count)
  const echoRisk = getCompanionVoiceState() === 'speaking'
  const metrics = { speech_ratio: speechRatio, rms, peak, noise_floor: noiseFloor, clip_ratio: clipRatio, echo_risk: echoRisk }
  if (echoRisk) return { accepted: false, code: 'ECHO_RISK', message: '伙伴正在播报，可能混入回声。请先停止播报后重录。', metrics }
  if (speechRatio < 0.18) return { accepted: false, code: 'NO_SPEECH', message: '没有检测到足够清晰的人声，请靠近麦克风说完整一句。', metrics }
  if (rms < 0.012 || noiseFloor > 0.025) return { accepted: false, code: 'NOISY_OR_LOW_GAIN', message: '环境噪声较大或声音太轻，请换安静位置并靠近麦克风重录。', metrics }
  if (clipRatio > 0.03 || peak > 0.995) return { accepted: false, code: 'CLIPPED', message: '声音过大出现爆音，请稍离麦克风后重录。', metrics }
  return { accepted: true, code: 'PASSED', message: '本地语音质量通过；仅保存统计特征，不保存原始录音。', metrics }
}

export function voiceProfileCaptureAdvice(childId: string | undefined, wav: Uint8Array, handsFree: boolean): { allowAutomaticSubmit: boolean; notice: string | null } {
  if (!childId) return { allowAutomaticSubmit: true, notice: null }
  const speaker = verifyLocalVoiceProfile(childId, wav)
  if (speaker.status !== 'uncertain') return { allowAutomaticSubmit: true, notice: null }
  return { allowAutomaticSubmit: true, notice: handsFree ? null : `${speaker.message} 这句话仍会在本机识别；请确认内容或改用文字。` }
}

function resample(input: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate) return input
  const output = new Float32Array(Math.round(input.length * toRate / fromRate))
  for (let index = 0; index < output.length; index += 1) {
    const position = index * fromRate / toRate
    const left = Math.floor(position)
    const right = Math.min(left + 1, input.length - 1)
    const fraction = position - left
    output[index] = input[left] * (1 - fraction) + input[right] * fraction
  }
  return output
}

function encodeWav(samples: Float32Array, sampleRate: number): Uint8Array {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  const write = (offset: number, text: string): void => { for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i)) }
  write(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); write(8, 'WAVE'); write(12, 'fmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(36, 'data'); view.setUint32(40, samples.length * 2, true)
  samples.forEach((sample, index) => view.setInt16(44 + index * 2, Math.max(-1, Math.min(1, sample)) * 0x7fff, true))
  return new Uint8Array(buffer)
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = ''
  const chunkSize = 0x8000
  for (let offset = 0; offset < bytes.length; offset += chunkSize) binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize))
  return btoa(binary)
}
