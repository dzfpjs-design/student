import { cancelExactLocalSpeech } from './LocalInputs'
import { Mic, Square } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { APP_SPACE_VOICE_PRIORITY, registerAppSpaceVoice } from './appSpaceVoice'
import { saveVoiceInputMode, useVoiceInputMode, type VoiceInputMode } from './voiceInputPreference'
import { QwenRealtimeVoiceSession, type QwenRealtimeToolResult, type QwenRealtimeInputCapture, type QwenRealtimeQueuedInput } from './qwenRealtimeVoice'
import { subscribeVoiceCaptureContext, voiceCaptureReference, voiceCaptureScopeStamp, voiceCarryoverConfirmationCapture } from './voiceCaptureContext'
import type { RealtimeVoiceStatus } from './realtimeVoiceRuntime'
import { localApi } from './api'
import { mediaDeviceRegistration } from './mediaDevices'
import {
  getCompanionVoiceState,
  getCompanionPlaybackLease,
  getCompanionPlaybackLocalOnly,
  LocalAgentStreamingTtsProvider,
  speakCompanion,
  subscribeCompanionVoice,
  stopCompanionSpeech,
} from './companionVoice'
import { claimVoiceCapture, releaseVoiceCapture, getVoiceCaptureOwner, subscribeVoiceCapture } from './voiceCaptureCoordinator'
import { matchWakeWord, shouldReturnToWakeStandby } from './wakeWordRuntime'
import type { QwenNativeRuntimeBridge,QwenNativeLocalSafetyControl, QwenCurriculumTutorDispatch } from './qwenNativeProtocol'
import { currentNativeRuntime } from './currentNativeRuntime'
import { nativeVoiceOnsetContext } from './nativeVoiceOnsetContext'
import { mistakeLearningOnset } from './mistakeLearningVoice'
import { privacyStopPending } from './deviceCommandReceipt'
import { PLAYBACK_TAIL_FAILURE, protectedPlaybackBarrier } from './protectedPlaybackBarrier'

// Older/unknown transport limits retain the conservative existing boundary.
// Qwen 3.8's documented session limit is 120 minutes; leave time for idle handoff.
export const QWEN_REALTIME_SESSION_REFRESH_MS = 275_000
export const QWEN_38_REALTIME_SESSION_REFRESH_MS = 110 * 60_000
export function realtimeSessionRefreshMs(model: string | undefined): number {
  return model === 'qwen3.8-omni-flash-realtime'
    ? QWEN_38_REALTIME_SESSION_REFRESH_MS : QWEN_REALTIME_SESSION_REFRESH_MS
}

export type QwenRealtimeUtteranceRoute =
  | { kind: 'conversation'; utterance: string; woke: boolean }
  | { kind: 'wake_only'; woke: true }
  | { kind: 'standby_ignored'; woke: false }

// Exact reviewed local failure sites. Provider bodies and near-matches are never promoted.
const nativeFailureReasons: Readonly<Record<string,string>> = Object.freeze({
  [PLAYBACK_TAIL_FAILURE]: 'QWEN_PLAYBACK_TAIL_UNCONFIRMED',
  "实时语音播放器异常。": "QWEN_PLAYBACK_ELEMENT_ERROR",
  "实时语音播放观测不可用，不能确认回复已送达。": "QWEN_PLAYBACK_OBSERVER_CREATE_FAILED",
  "实时语音播放观测暂时不可用，正在恢复连接。": "QWEN_PLAYBACK_OBSERVER_ACTIVITY_FAILED",
  "保守收音路径无法确认播放观测健康，正在恢复连接。": "QWEN_PLAYBACK_OBSERVER_QUIET_UNCONFIRMED",
  "保守收音路径缺少播放观测，正在恢复连接。": "QWEN_PLAYBACK_OBSERVER_MISSING",
  "保守收音路径的播放观测不可用，正在恢复连接。": "QWEN_PLAYBACK_OBSERVER_UNAVAILABLE",
  "实时语音播放观测不可用，未取得送达回执。": "QWEN_PLAYBACK_OBSERVER_UNAVAILABLE",
  "实时语音播放观测不可用，回复未取得送达回执。": "QWEN_PLAYBACK_OBSERVER_UNAVAILABLE",
  "实时语音播放被系统权限阻止。": "QWEN_PLAYBACK_PLAY_NOT_ALLOWED",
  "实时语音播放格式或设备不受支持。": "QWEN_PLAYBACK_PLAY_NOT_SUPPORTED",
  "实时语音播放被中断。": "QWEN_PLAYBACK_PLAY_ABORTED",
  "实时语音播放状态无效。": "QWEN_PLAYBACK_PLAY_INVALID_STATE",
  "实时语音播放器启动失败。": "QWEN_PLAYBACK_PLAY_OTHER",
  "这句话的云端转写未完成，没有执行请求；恢复后请重说。": "QWEN_REALTIME_INPUT_TRANSCRIPTION_FAILED",
  "自然语音服务没有确认会话配置。": "QWEN_NATIVE_SESSION_CONFIG_TIMEOUT",
  "云语音没有确认手动回合模式，未开放音频上传。": "QWEN_NATIVE_SESSION_CONFIG_UNCONFIRMED",
  "云语音会话身份发生变化，正在重新连接。": "QWEN_NATIVE_SESSION_IDENTITY_UNCONFIRMED",
  "云语音会话身份未能核对。": "QWEN_NATIVE_SESSION_IDENTITY_UNCONFIRMED",
  "首次语音原话的供应商身份尚未确认，没有重复操作。": "QWEN_NATIVE_HOUSEHOLD_INPUT_UNCONFIRMED",
  "首次原话的持久来源尚未确认，没有执行操作。": "QWEN_NATIVE_HOUSEHOLD_INPUT_UNCONFIRMED",
  "已保存原话的恢复身份尚未确认，没有重新执行。": "QWEN_NATIVE_RESTORED_INPUT_UNCONFIRMED",
  "上次保存的原话暂未取得恢复确认。": "QWEN_NATIVE_RESTORED_INPUT_UNCONFIRMED",
  "这句话的原始采集身份未能核对，没有提交新的操作。": "QWEN_NATIVE_INPUT_ADMISSION_UNCONFIRMED",
  "原话的保存回执尚未确认，没有执行请求。": "QWEN_NATIVE_INPUT_ADMISSION_UNCONFIRMED",
  "实时回复归属尚未确认，未执行新的操作。": "QWEN_NATIVE_RESPONSE_IDENTITY_UNCONFIRMED",
  "实时回复没有返回对应的回合身份，正在恢复连接；原话已保存，没有自动重做。": "QWEN_NATIVE_RESPONSE_IDENTITY_UNCONFIRMED",
  "这段采集来源仅限本机处理，没有交给云语音。请重新开始一段新的发言。": "QWEN_NATIVE_INPUT_SOURCE_RESTRICTED",
  "前面的语音仍在等待确认，已暂停收音。未确认的一句请稍后重说。": "QWEN_NATIVE_INPUT_BACKPRESSURE",
  "工具结果已保留，实时语音交接尚未确认，没有重新执行。": "QWEN_NATIVE_TOOL_HANDOFF_UNCONFIRMED",
  "云语音能力或上下文尚未核对，未开放收音。": "QWEN_NATIVE_RUNTIME_CONTEXT_UNCONFIRMED",
  "当前客户端缺少原生云语音连接。": "QWEN_NATIVE_RUNTIME_CONTEXT_UNCONFIRMED",
  "尚未允许云端语音。": "QWEN_NATIVE_AUDIO_PERMISSION_UNCONFIRMED",
  "自然语音输出连接暂时不可用，正在恢复。": "QWEN_NATIVE_AUDIO_OUTPUT_UNAVAILABLE",
  "自然语音输出音轨缺失。": "QWEN_NATIVE_AUDIO_OUTPUT_UNAVAILABLE",
  "刚才的对话记录尚未确认保存。": "QWEN_NATIVE_DIALOGUE_SAVE_UNCONFIRMED",
  "云语音初始化消息超过边界。": "QWEN_NATIVE_PROTOCOL_CAPACITY",
  "本轮工具身份容量已满，正在恢复连接。": "QWEN_NATIVE_PROTOCOL_CAPACITY",
  "云端对话长度超过本轮记录边界。": "QWEN_NATIVE_PROTOCOL_CAPACITY",
  "云语音连接身份没有取得。": "QWEN_NATIVE_SOCKET_UNCONFIRMED",
  "自然语音连接没有完成，正在恢复。": "QWEN_NATIVE_SOCKET_UNCONFIRMED",
  "自然语音连接已中断，原话仍按保存回执恢复。": "QWEN_NATIVE_SOCKET_UNCONFIRMED",
  "自然语音发送没有取得确认，正在恢复连接。": "QWEN_NATIVE_SOCKET_SEND_UNCONFIRMED"
})

export function classifyQwenRealtimeError(message: string): string {
  if (Object.prototype.hasOwnProperty.call(nativeFailureReasons,message)) return nativeFailureReasons[message]
  for (const code of ['QWEN_NATIVE_CREDENTIAL_REJECTED','QWEN_NATIVE_ACCESS_DENIED','QWEN_NATIVE_ACCOUNT_BALANCE']) if (message === code) return code
  for(const code of ['ASR_CLIENT_TIMEOUT','ASR_CLIENT_UNKNOWN','QWEN_NATIVE_SOCKET_UNCONFIRMED','QWEN_NATIVE_SOCKET_OWNER_CHANGED','QWEN_NATIVE_SOCKET_POLICY_UNCONFIRMED','QWEN_NATIVE_SOCKET_PROVIDER_ERROR','QWEN_NATIVE_SOCKET_PAYLOAD_INVALID','QWEN_NATIVE_SOCKET_BACKPRESSURE','QWEN_NATIVE_SOCKET_SEND_UNCONFIRMED'])if(message.includes(code))return code
  if (message === '刚才的语音没有取得完整转写和保存回执，正在恢复连接；未确认的原话请重说。') return 'QWEN_REALTIME_INPUT_ACTIVITY_UNCONFIRMED'
  if (message.includes('音频活动后') && message.includes('未取得完整转写')) return 'QWEN_REALTIME_INPUT_ACTIVITY_UNCONFIRMED'
  if (message.includes('这句话的语音转写失败')) return 'QWEN_REALTIME_INPUT_TRANSCRIPTION_FAILED'
  if (message.includes('输入身份容量已满')) return 'QWEN_REALTIME_INPUT_IDENTITY_CAPACITY'
  if (/播放|autoplay|audio player/i.test(message)) return 'QWEN_REALTIME_MEDIA_PLAYBACK_BLOCKED'
  if (/数据通道|data channel/i.test(message)) return 'QWEN_REALTIME_DATA_CHANNEL'
  if (/麦克风|microphone|audio track|audio source|NotReadableError|NotFoundError|OverconstrainedError|device not found|device.*(?:busy|unavailable)/i.test(message)) return 'QWEN_REALTIME_MICROPHONE'
  if (/绕过.*运行时|bypass/i.test(message)) return 'QWEN_REALTIME_RUNTIME_BYPASS'
  if (/千问实时语音返回错误|turn_detection|provider/i.test(message)) return 'QWEN_REALTIME_PROVIDER_ERROR'
  if (/会话.*(?:过期|时长|到期)|session.*(?:expired|duration|lifetime|timeout)/i.test(message)) return 'QWEN_REALTIME_SESSION_EXPIRED'
  if (/连接失败|连接已关闭|connection/i.test(message)) return 'QWEN_REALTIME_CONNECTION_FAILED'
  return 'QWEN_REALTIME_UNKNOWN'
}

const permanentProviderGuidance: Readonly<Record<string, string>> = Object.freeze({
  QWEN_NATIVE_CREDENTIAL_REJECTED: '云语音服务拒绝了密钥，请家长检查语音设置中的密钥，检查后点重试云语音。',
  QWEN_NATIVE_ACCESS_DENIED: '云语音服务拒绝访问，请家长检查模型权限，检查后点重试云语音。',
  QWEN_NATIVE_ACCOUNT_BALANCE: '云语音账户余额不足，请家长检查百炼账户，检查后点重试云语音。',
})
export const PLAYBACK_TAIL_RECOVERY_GUIDANCE = '麦克风已暂停，因为还未确认上一段声音结束。请等声音停止后，点“重试麦克风”。'
type ManualRecovery = { kind: 'provider' | 'playback_tail'; guidance: string }
type ExplicitCaptureState = 'idle' | 'waiting' | 'ready'
type ExplicitControlSource = 'keyboard' | 'screen'

// A component replacement must not silently retry an unchanged rejection or
// discard originals awaiting confirmation. This is process memory, not durable
// storage: restart recovery still depends on the local admission journal.
const permanentRecovery = new Map<string, ManualRecovery & { pending: QwenRealtimeQueuedInput[] }>()
export function resetPermanentQwenRecoveryForTests(): void { permanentRecovery.clear() }

export function routeQwenRealtimeUtterance(utterance: string, wakeWord: string | undefined,
  conversationActive: boolean): QwenRealtimeUtteranceRoute {
  const normalized = utterance.trim()
  if (!wakeWord || conversationActive) return { kind: 'conversation', utterance: normalized, woke: false }
  const match = matchWakeWord(normalized, wakeWord)
  if (!match.matched) return { kind: 'standby_ignored', woke: false }
  return match.command
    ? { kind: 'conversation', utterance: match.command, woke: true }
    : { kind: 'wake_only', woke: true }
}

export function QwenRealtimeVoiceInput({
  active,
  sessionId,
  childId,
  childName,
  audioDeviceId,
  wakeWord,
  inactivitySeconds = 60,
  interactionBusy = false,
  localTutorPlayback = false,
  externalActivationSignal = 0,
  initialConversationActive = false,
  onWake,
  onConversationActiveChange,
  onTurn,
  onInputCompleted,
  onWorkspaceInput,
  onCurriculumTutor,
  onStatus,
  onUserTranscript,
  onAssistantTranscript,
  onPublicSpeechInterrupt,
  onExplicitPlaybackInterrupt,
  explicitReplyBusy,
  explicitControlsVisible = true,
  onKeyboardCaptureStart,
  onPermanentFailure,
  onPendingInputHandoff,
  onRepeatRequired,
  nativeRuntime,
  visibleLearningSource=true,
  householdEntry,
  onHouseholdEntryConsumed,
  audioAllowed = false,
  onPrivacyStop,
  onLocalSafety,
}: {
  active: boolean
  sessionId: string
  childId: string
  childName: string
  audioDeviceId?: string
  wakeWord?: string
  inactivitySeconds?: number
  interactionBusy?: boolean
  externalActivationSignal?: number
  initialConversationActive?: boolean
  onWake?: (announce?:boolean) => void
  onConversationActiveChange?: (active: boolean) => void
  nativeRuntime?: QwenNativeRuntimeBridge
  visibleLearningSource?:boolean
  householdEntry?:import('./types').HouseholdNativeEntry
  onHouseholdEntryConsumed?:()=>void
  audioAllowed?: boolean
  onPrivacyStop?: () => void
  onLocalSafety?:(control:QwenNativeLocalSafetyControl)=>void
  onTurn: (utterance: string, capture?: QwenRealtimeInputCapture) => Promise<QwenRealtimeToolResult>
  onInputCompleted?: (utterance: string, capture?: QwenRealtimeInputCapture) => void
  onWorkspaceInput?: (utterance: string, capture: QwenRealtimeInputCapture, voiceInputId: string) => boolean
  onCurriculumTutor?: (utterance: string, dispatch: QwenCurriculumTutorDispatch, isCurrent: () => boolean) => boolean
  localTutorPlayback?: boolean
  onStatus: (status: RealtimeVoiceStatus) => void
  onUserTranscript?: (text: string) => void
  onAssistantTranscript?: (text: string) => void
  onPublicSpeechInterrupt?: () => void
  onExplicitPlaybackInterrupt?: () => boolean
  explicitReplyBusy?: () => boolean
  explicitControlsVisible?: boolean
  onKeyboardCaptureStart?: () => void
  onPermanentFailure?: (message: string) => void
  /** Only true confirms durable ownership of every original; never authorizes execution. */
  onPendingInputHandoff?: (inputs: QwenRealtimeQueuedInput[], signal: AbortSignal, retry?: boolean) => Promise<boolean>
  /** Fixed local guidance for an unrecognized segment; never an input/replay. */
  onRepeatRequired?: (scope: { sessionId: string; childId: string }) => void
}) {
  const [manualRecoveryKind, setManualRecoveryKind] = useState<ManualRecovery['kind'] | null>(null)
  const inputMode = useVoiceInputMode(childId)
  const inputModeRef = useRef(inputMode)
  inputModeRef.current = inputMode
  const [preferenceError, setPreferenceError] = useState('')
  const [explicitCapture, setExplicitCapture] = useState<{ scope: string; state: ExplicitCaptureState; source?: ExplicitControlSource; detail?: string }>({ scope: '', state: 'idle' })
  const [controlPhase, setControlPhase] = useState<RealtimeVoiceStatus['phase']>('connecting')
  const explicitControls = useRef<{ begin: (source: ExplicitControlSource) => boolean; finish: (source: ExplicitControlSource) => void;
    cancel: () => void; setMode: (mode: VoiceInputMode) => void } | null>(null)
  const controlsVisible = useRef(explicitControlsVisible)
  controlsVisible.current = explicitControlsVisible
  const revealKeyboardControls = useRef(onKeyboardCaptureStart)
  revealKeyboardControls.current = onKeyboardCaptureStart
  const refreshKeyboardScope = useRef<(() => void) | null>(null)
  useLayoutEffect(() => { refreshKeyboardScope.current?.() }, [visibleLearningSource])
  useLayoutEffect(() => { explicitControls.current?.setMode(inputMode) }, [inputMode])
  useLayoutEffect(() => { if (!explicitControlsVisible) explicitControls.current?.cancel() }, [explicitControlsVisible])
  const manualRetry = useRef<(() => void) | null>(null)
  const handlers = useRef({ active, sessionId, childId, audioDeviceId, onTurn, onInputCompleted, onWorkspaceInput, onCurriculumTutor, onStatus, onUserTranscript, onAssistantTranscript, onPermanentFailure, onPendingInputHandoff, onRepeatRequired, onWake, onConversationActiveChange, interactionBusy, localTutorPlayback, onPublicSpeechInterrupt, onExplicitPlaybackInterrupt, explicitReplyBusy, nativeRuntime, visibleLearningSource,audioAllowed, onPrivacyStop,onLocalSafety })
  handlers.current = { active, sessionId, childId, audioDeviceId, onTurn, onInputCompleted, onWorkspaceInput, onCurriculumTutor, onStatus, onUserTranscript, onAssistantTranscript, onPermanentFailure, onPendingInputHandoff, onRepeatRequired, onWake, onConversationActiveChange, interactionBusy, localTutorPlayback, onPublicSpeechInterrupt, onExplicitPlaybackInterrupt, explicitReplyBusy, nativeRuntime, visibleLearningSource,audioAllowed, onPrivacyStop,onLocalSafety }
  const refreshPlaybackGate = useRef<(() => void) | null>(null)
  useEffect(() => { refreshPlaybackGate.current?.() }, [localTutorPlayback])
  const householdEntryRef=useRef(householdEntry)
  const householdConsumed=useRef(false)
  const activation = useRef<{ activate: () => void } | null>(null)
  const authority = useRef<(() => void) | null>(null)
  useLayoutEffect(()=>{authority.current?.()},[audioAllowed])
  const conversation = useRef<{ key: string; active: boolean; lastActivityAt: number }>({
    key: '', active: initialConversationActive || !wakeWord,
    lastActivityAt: initialConversationActive || !wakeWord ? Date.now() : 0,
  })
  const conversationKey = `${sessionId}:${childId}`
  if (conversation.current.key !== conversationKey) {
    const activeConversation = initialConversationActive || !wakeWord
    conversation.current = {
      key: conversationKey,
      active: activeConversation,
      lastActivityAt: activeConversation ? Date.now() : 0,
    }
  } else if (!wakeWord && !conversation.current.active) {
    conversation.current.active = true
    conversation.current.lastActivityAt = Date.now()
  }
  const owner = useRef('')
  if (!owner.current) owner.current = `qwen-realtime-${crypto.randomUUID()}`

  useEffect(() => {
    if (externalActivationSignal > 0) activation.current?.activate()
  }, [externalActivationSignal])

  useEffect(() => {
    if (privacyStopPending(sessionId)) {
      const retained = permanentRecovery.get(JSON.stringify([sessionId, childId]))
      if (retained) retained.pending.length = 0
      setManualRecoveryKind(null)
      return
    }
    if(!handlers.current.audioAllowed){
      const retained = permanentRecovery.get(JSON.stringify([sessionId, childId]))
      if (retained) retained.pending.length = 0
      setManualRecoveryKind(null)
      if (active) handlers.current.onStatus({runtime:'qwen_omni_realtime',phase:'idle',speechActive:false,pendingUtterances:0,recoverable:true,
        detail:'云音频许可尚未开放，自然语音已暂停。'})
      return
    }
    if (!active) return
    if(!handlers.current.nativeRuntime){
      const message='云语音运行时尚未配置完成，暂时不能开始自然对话。'
      handlers.current.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:false,detail:message})
      handlers.current.onPermanentFailure?.(message)
      return
    }
    let stopped = false
    let retryTimer: number | null = null
    let refreshTimer: number | null = null
    let renewalTimer: number | null = null
    let inactivityTimer: number | null = null
    let pendingHandoffTimer: number | null = null
    let pendingHandoffController: AbortController | null = null
    let unsubscribeCaptureWait: (() => void) | null = null
    const stopWaitingForCapture = (): void => {
      unsubscribeCaptureWait?.()
      unsubscribeCaptureWait = null
    }
    const recoveryKey = JSON.stringify([sessionId, childId])
    const playbackScope = { sessionId, childId }
    const priorPermanent = permanentRecovery.get(recoveryKey)
    let attempt = 0
    let retryExhausted = !!priorPermanent
    let manualRecovery = priorPermanent?.kind ?? null
    let terminalGuidance: string | null = priorPermanent?.guidance ?? null
    let repeatRequired = false
    let replyGeneration = 0
    let connectionGeneration = 0
    const recoveryCancellations = new Set<() => void>()
    const invalidateReplies = (): void => {
      replyGeneration += 1
      for (const cancel of [...recoveryCancellations]) cancel()
    }
    const interruptExplicitPlayback = (): boolean => {
      const lease = getCompanionPlaybackLease(true)
      let stoppedLocalPlayback = false
      if (lease?.sessionId === sessionId) {
        // Windows may already have queued local SAPI before the browser reports its start.
        const needsTail = lease.kind === 'local_sapi' || lease.kind === 'preparing' && getCompanionPlaybackLocalOnly()
        const tail = needsTail
          ? protectedPlaybackBarrier.begin(playbackScope, lease.id, lease.generation, performance.now()) : null
        stoppedLocalPlayback = cancelExactLocalSpeech(lease.id, lease.generation, true)
        if (!stoppedLocalPlayback && tail) protectedPlaybackBarrier.rollback(tail)
      }
      const stoppedTutorPresentation = handlers.current.onExplicitPlaybackInterrupt?.() === true
      if (stoppedLocalPlayback || stoppedTutorPresentation) invalidateReplies()
      return stoppedLocalPlayback || stoppedTutorPresentation
    }
    let session: QwenRealtimeVoiceSession | null = null
    let explicitSource: ExplicitControlSource | null = null
    let explicitFinishing = false
    const updateExplicitCapture = (state: ExplicitCaptureState, detail?: string): void => {
      if (state === 'idle') { explicitSource = null; explicitFinishing = false }
      setExplicitCapture({ scope: conversationKey, state, source: explicitSource ?? undefined, detail })
    }
    authority.current=()=>session?.syncNativeAuthority()
    const pendingReconnectInputs: QwenRealtimeQueuedInput[] = priorPermanent?.pending ?? []
    // Preserve the input's route when activation changes before tool dispatch.
    const completedInputRoutes = new Map<string, { utterance: string; route: QwenRealtimeUtteranceRoute }>()
    const retainWaitingInput = (): void => {
      if (session) for (const utterance of session.takePendingInputsForReconnect()) pendingReconnectInputs.push(utterance)
    }
    let leaseId: string | null = null
    const conversationState = conversation.current
    let lastStatus: RealtimeVoiceStatus = {
      runtime: 'qwen_omni_realtime', phase: conversationState.active ? 'connecting' : 'wake_standby',
      speechActive: false, pendingUtterances: 0, recoverable: true,
    }
    const retryDelays = [1_000, 3_000, 8_000]
    let companionSpeaking = getCompanionVoiceState() === 'speaking'
    let companionPreparing = getCompanionVoiceState() === 'preparing'
    let lastCompanionActivityAt = companionSpeaking || companionPreparing ? Date.now() : 0
    const applyPlaybackGate = (target = session): void => {
      const localTutor = handlers.current.localTutorPlayback
      target?.setExternalPlaybackActive(localTutor || companionSpeaking || companionPreparing,
        localTutor || getCompanionPlaybackLocalOnly())
    }
    const refreshGate = () => applyPlaybackGate()
    refreshPlaybackGate.current = refreshGate
    const unsubscribeCompanionVoice = subscribeCompanionVoice((state) => {
      if (stopped) return
      const wasSpeaking = companionSpeaking
      const wasPreparing = companionPreparing
      companionSpeaking = state === 'speaking'
      companionPreparing = state === 'preparing'
      if (companionSpeaking || wasSpeaking || companionPreparing || wasPreparing) lastCompanionActivityAt = Date.now()
      if (conversationState.active && (companionSpeaking || wasSpeaking || companionPreparing || wasPreparing)) conversationState.lastActivityAt = Date.now()
      applyPlaybackGate()
      announceRepeatIfReady()
    })

    const publish = (status: RealtimeVoiceStatus): void => {
      const displayed = inputModeRef.current === 'hands-free' && wakeWord && !conversationState.active && status.phase === 'listening'
        ? { ...status, phase: 'wake_standby' as const, speechActive: false }
        : status
      lastStatus = { ...displayed, pendingUtterances: displayed.pendingUtterances + pendingReconnectInputs.length }
      setControlPhase(displayed.phase)
      handlers.current.onStatus(lastStatus)
    }

    function announceRepeatIfReady(): void {
      if (!repeatRequired || stopped || retryExhausted) return
      if (!conversationState.active) { repeatRequired = false; return }
      if (lastStatus.phase !== 'listening' || lastStatus.speechActive || lastStatus.pendingUtterances
        || handlers.current.interactionBusy || companionSpeaking || companionPreparing) return
      repeatRequired = false
      try { handlers.current.onRepeatRequired?.({ sessionId, childId }) }
      catch { /* A guidance consumer cannot break or revive transport ownership. */ }
    }

    const releaseLease = (reason: string): void => {
      if (renewalTimer !== null) window.clearInterval(renewalTimer)
      renewalTimer = null
      const current = leaseId
      leaseId = null
      if (current) void localApi.releaseDeviceLease(current, reason).catch(() => undefined)
    }

    const clearRefresh = (): void => {
      if (refreshTimer !== null) window.clearTimeout(refreshTimer)
      refreshTimer = null
    }

    const scheduleRefresh = (): void => {
      clearRefresh()
      const scheduledSession = session, scheduledGeneration = connectionGeneration
      const owned = (): boolean => !stopped && !retryExhausted && session === scheduledSession
        && connectionGeneration === scheduledGeneration
      const busy = (): boolean => handlers.current.interactionBusy || companionSpeaking || companionPreparing
        || lastStatus.speechActive || lastStatus.pendingUtterances > 0 || pendingReconnectInputs.length > 0
        || ['tool_running', 'speaking', 'transcribing', 'recovering'].includes(lastStatus.phase)
        || Date.now() - Math.max(conversationState.lastActivityAt, lastCompanionActivityAt) < 10_000
      const defer = (): void => { refreshTimer = window.setTimeout(() => { void refreshWhenIdle() }, 5_000) }
      const refreshWhenIdle = async (): Promise<void> => {
        if (!owned()) return
        if (busy()) { defer(); return }
        let health: Awaited<ReturnType<QwenRealtimeVoiceSession['readPlannedRefreshState']>> | undefined
        try { health = await scheduledSession?.readPlannedRefreshState() } catch { /* Unknown maintenance state defers. */ }
        if (!owned()) return
        if (busy() || !health?.current || !health.known || !health.fresh || health.pending) {
          defer()
          return
        }
        void window.desktop?.recordRendererDiagnostic?.({
          event: 'voice.realtime.session_rotated', level: 'info',
          message: 'The realtime transport was refreshed before the provider session lifetime expired.',
          details: { runtime: 'qwen_omni_realtime', planned: true },
        })
        refreshTimer = null
        void connect()
      }
      refreshTimer = window.setTimeout(() => { void refreshWhenIdle() }, realtimeSessionRefreshMs(scheduledSession?.getConnectedModel?.()))
    }

    let nativeRecoveryWitness=false
    // A background transport probe must never restore prior speech or side effects.
    let backgroundRecoveryNoReplay = false
    let slowRetryStep = 0
    const slowRetryDelays = [30_000, 60_000, 120_000, 300_000]
    const scheduleSlowRecovery = (): void => {
      if (stopped || !retryExhausted || retryTimer !== null) return
      const delay = slowRetryDelays[Math.min(slowRetryStep++, slowRetryDelays.length - 1)]
      retryTimer = window.setTimeout(() => {
        retryTimer = null
        if (stopped || !retryExhausted || !handlers.current.active || !handlers.current.audioAllowed
          || !handlers.current.nativeRuntime || handlers.current.sessionId !== sessionId
          || handlers.current.childId !== childId || handlers.current.audioDeviceId !== audioDeviceId) return
        // Original entries remain with their current owner until explicitly resolved.
        if (pendingReconnectInputs.length || pendingHandoffController || handlers.current.interactionBusy
          || companionSpeaking || companionPreparing
          || (householdEntryRef.current && !householdConsumed.current) || !claimVoiceCapture(owner.current)) {
          scheduleSlowRecovery(); return
        }
        try {
          void Promise.resolve(window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.retry_scheduled', level: 'info',
            message: 'A background recovery connection is being attempted without replaying prior inputs.',
            details: { runtime: 'qwen_omni_realtime', planned: true } })).catch(() => undefined)
        } catch { /* Diagnostics never own recovery. */ }
        retryExhausted = false
        backgroundRecoveryNoReplay = true
        nativeRecoveryWitness = false
        conversationState.active = false
        conversationState.lastActivityAt = 0
        handlers.current.onConversationActiveChange?.(false)
        publish({ runtime: 'qwen_omni_realtime', phase: 'recovering', speechActive: false,
          pendingUtterances: 0, recoverable: true, detail: '正在后台恢复云语音连接。' })
        void connect()
      }, delay)
    }
    const activateConversation = (announce: boolean,notify=false): void => {
      if(announce)nativeRecoveryWitness=true
      conversationState.active = true
      conversationState.lastActivityAt = Date.now()
      handlers.current.onConversationActiveChange?.(true)
      if(announce||notify)handlers.current.onWake?.(announce)
      session?.syncConversationActivation()
      publish({ ...lastStatus, phase: 'listening', speechActive: false, detail: undefined })
    }
    activation.current = { activate: () => activateConversation(false) }

    const savePendingHandoff = async (retry: boolean): Promise<boolean> => {
      const handoff = handlers.current.onPendingInputHandoff
      const scopeCurrent = (): boolean => !stopped && !privacyStopPending(sessionId) && handlers.current.active && handlers.current.audioAllowed
        && !!handlers.current.nativeRuntime && handlers.current.sessionId === sessionId && handlers.current.childId === childId
        && handlers.current.audioDeviceId === audioDeviceId
      if (!scopeCurrent() || !retryExhausted || !pendingReconnectInputs.length || !handoff || pendingHandoffController) return false
      const controller = new AbortController()
      pendingHandoffController = controller
      let timer: number | null = null
      try {
        const deadline = new Promise<false>(resolve => {
          controller.signal.addEventListener('abort', () => resolve(false), { once: true })
          timer = window.setTimeout(() => controller.abort(), 20_000)
          pendingHandoffTimer = timer
        })
        const stored = await Promise.race([Promise.resolve().then(() => handoff(structuredClone(pendingReconnectInputs), controller.signal, retry)), deadline])
        if (!scopeCurrent() || !retryExhausted || controller.signal.aborted || stored !== true || pendingHandoffController !== controller) return false
        pendingReconnectInputs.length = 0
        const saved = '待处理原话已保存，未自动重复执行。'
        const detail = terminalGuidance ? `${terminalGuidance} ${saved}` : saved
        publish({ runtime: 'qwen_omni_realtime', phase: 'error', speechActive: false,
          pendingUtterances: 0, recoverable: false, detail })
        handlers.current.onPermanentFailure?.(detail)
        return true
      } catch { return false }
      finally {
        if (timer !== null) window.clearTimeout(timer)
        if (pendingHandoffTimer === timer) pendingHandoffTimer = null
        controller.abort()
        if (pendingHandoffController === controller) pendingHandoffController = null
      }
    }

    const scheduleRetry = (message: string, trigger: 'status_error' | 'lease_renewal' | 'connect_failed'): void => {
      if (stopped || retryExhausted || retryTimer !== null) return
      const reason = classifyQwenRealtimeError(message)
      const providerGuidance = permanentProviderGuidance[reason]
      const playbackTailBlocked = reason === 'QWEN_PLAYBACK_TAIL_UNCONFIRMED'
        && (attempt >= retryDelays.length || !protectedPlaybackBarrier.mayOpen(playbackScope, performance.now()))
      const recovery: ManualRecovery | null = playbackTailBlocked ? { kind: 'playback_tail', guidance: PLAYBACK_TAIL_RECOVERY_GUIDANCE }
        : providerGuidance ? { kind: 'provider', guidance: providerGuidance } : null
      manualRecovery = recovery?.kind ?? null
      terminalGuidance = recovery?.guidance ?? null
      if (providerGuidance) attempt = retryDelays.length
      const retryGeneration = connectionGeneration
      if (conversationState.active && ['QWEN_REALTIME_INPUT_ACTIVITY_UNCONFIRMED',
        'QWEN_REALTIME_INPUT_TRANSCRIPTION_FAILED'].includes(reason)) repeatRequired = true
      retainWaitingInput()
      if (recovery) permanentRecovery.set(recoveryKey, { ...recovery, pending: pendingReconnectInputs })
      invalidateReplies()
      if (recovery || attempt >= retryDelays.length) {
        retryExhausted = true
        clearRefresh()
        const retainedLocalWake=!recovery && session?.enterLocalWakeRecovery()===true
        if(!retainedLocalWake){connectionGeneration+=1;session?.close('retry_exhausted');releaseLease('qwen_realtime_retry_exhausted')}
        if(!retainedLocalWake)releaseVoiceCapture(owner.current)
        void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.retry_exhausted', level: 'warn',
          details: { reason_code: reason, attempts: attempt,
            count: pendingReconnectInputs.length, raw_audio_or_video: false } })
        const displayMessage = recovery?.guidance ?? message
        const failure = pendingReconnectInputs.length
          ? `${displayMessage} 还有${pendingReconnectInputs.length}句已识别的话尚未交给运行时，不能当作已处理。点击重试会核对并保存原话。` : displayMessage
        if (recovery) setManualRecoveryKind(recovery.kind)
        if (!recovery) scheduleSlowRecovery()
        try {
          publish({ runtime: 'qwen_omni_realtime', phase: 'error', speechActive: false,
            pendingUtterances: 0, recoverable: false, detail: failure })
        } finally {
          try { handlers.current.onPermanentFailure?.(failure) }
          finally {
            if (pendingReconnectInputs.length) void savePendingHandoff(false)
          }
        }
        return
      }
      connectionGeneration+=1
      session?.close('retry_scheduled')
      releaseLease('qwen_realtime_retry_scheduled')
      const delay = retryDelays[attempt++]
      // Arm internal recovery before notifying the UI; its consumer can fail.
      retryTimer = window.setTimeout(() => { retryTimer = null; void connect() }, delay)
      try {
        void Promise.resolve(window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.retry_scheduled', level: 'warn',
          details: { trigger, reason_code: reason, attempt,
            retry_delay_ms: delay, connection_generation: retryGeneration } })).catch(() => undefined)
      } catch { /* Diagnostics cannot prevent the already scheduled recovery. */ }
      publish({
        runtime: 'qwen_omni_realtime', phase: 'recovering', speechActive: false,
        pendingUtterances: 0, recoverable: true, detail: `${message} ${Math.ceil(delay / 1000)}秒后重连。`,
      })
    }

    manualRetry.current = () => {
      if (stopped || privacyStopPending(sessionId) || !retryExhausted || !handlers.current.active || !handlers.current.audioAllowed
        || !handlers.current.nativeRuntime || handlers.current.sessionId !== sessionId
        || handlers.current.childId !== childId || handlers.current.audioDeviceId !== audioDeviceId) return
      if (pendingReconnectInputs.length && !pendingHandoffController) {
        publish({...lastStatus,phase:'error',speechActive:false,recoverable:false,
          detail: `${terminalGuidance ?? ''} 正在核对原话保存结果。`.trim()})
        void savePendingHandoff(true).then(stored => {
          if (stored && !stopped && retryExhausted) manualRetry.current?.()
          else if (!stored && !stopped && retryExhausted) publish({...lastStatus,phase:'error',speechActive:false,recoverable:false,
            detail:`${terminalGuidance ?? ''} 原话仍在核对，请稍后重试。`.trim()})
        })
        return
      }
      if (pendingReconnectInputs.length || pendingHandoffController || handlers.current.interactionBusy
        || companionSpeaking || companionPreparing || (householdEntryRef.current && !householdConsumed.current)) {
        publish({...lastStatus,phase:'error',speechActive:false,recoverable:false,
          detail:`${terminalGuidance ?? ''} 原话或当前任务仍在核对，请稍后重试。`.trim()})
        return
      }
      if (!claimVoiceCapture(owner.current)) {
        publish({...lastStatus,phase:'error',speechActive:false,recoverable:false,detail:'麦克风正被其他语音任务使用，请稍后重试。'})
        return
      }
      if (stopped || !handlers.current.active || !handlers.current.audioAllowed || !handlers.current.nativeRuntime
        || handlers.current.sessionId !== sessionId || handlers.current.childId !== childId
        || handlers.current.audioDeviceId !== audioDeviceId || getVoiceCaptureOwner() !== owner.current) {
        releaseVoiceCapture(owner.current)
        return
      }
      const retryPlaybackTail = manualRecovery === 'playback_tail'
      if (retryPlaybackTail) protectedPlaybackBarrier.explicitRetry(playbackScope, performance.now())
      retryExhausted = false
      permanentRecovery.delete(recoveryKey)
      manualRecovery = null
      terminalGuidance = null
      attempt = 0
      backgroundRecoveryNoReplay = true
      nativeRecoveryWitness = false
      conversationState.active = false
      conversationState.lastActivityAt = 0
      handlers.current.onConversationActiveChange?.(false)
      setManualRecoveryKind(null)
      publish({runtime:'qwen_omni_realtime',phase:'recovering',speechActive:false,pendingUtterances:0,recoverable:true,
        detail: retryPlaybackTail ? '正在重新确认播放尾音，确认后会恢复收音。' : '正在重试云语音连接。'})
      void connect()
    }

    const connect = async (): Promise<void> => {
      if (stopped || retryExhausted) return
      const generation = ++connectionGeneration
      let metadataDeviceHash: string | null = null
      const ownsConnection = (): boolean => !stopped && generation === connectionGeneration
      const nativeRuntimeCurrent = (): boolean => ownsConnection()
        &&handlers.current.active&&handlers.current.audioAllowed===true&&!!handlers.current.nativeRuntime
        &&handlers.current.sessionId===sessionId&&handlers.current.childId===childId
        &&handlers.current.audioDeviceId===audioDeviceId&&getVoiceCaptureOwner()===owner.current
      retainWaitingInput()
      invalidateReplies()
      clearRefresh()
      if (explicitSource) updateExplicitCapture('idle', '语音连接已更换，请重新说一次。')
      session?.close('reconnecting')
      releaseLease('qwen_realtime_reconnecting')
      const dispatchRuntimeTurn = async (
        utterance: string,
        deliveryRoute: 'realtime_tool' | 'runtime_recovery' | 'reconnect_recovery' | 'committed_input',
        capture?: QwenRealtimeInputCapture,
      ): Promise<QwenRealtimeToolResult> => {
        if (!ownsConnection()) return { responseText: 'obsolete_connection_ignored', speak: false }
        const completed = capture ? completedInputRoutes.get(capture.localInputId) : undefined
        if (completed && completed.utterance !== utterance.trim()) return { responseText: 'input_body_changed', speak: false }
        const route = completed?.route ?? routeQwenRealtimeUtterance(utterance, wakeWord, conversationState.active)
        if (capture) completedInputRoutes.delete(capture.localInputId)
        if (route.kind === 'standby_ignored') {
          return { responseText: 'standby_utterance_ignored', speak: false }
        }
        if (route.woke && !completed) activateConversation(route.kind === 'wake_only')
        if (route.kind === 'wake_only') return { responseText: 'wake_word_acknowledged', speak: false }
        if (route.woke) handlers.current.onUserTranscript?.(route.utterance)
        conversationState.lastActivityAt = Date.now()
        const dispatchedAt = Date.now()
        const generation = replyGeneration
        void window.desktop?.recordRendererDiagnostic?.({
          event: 'voice.realtime.turn_dispatched', level: 'info',
          message: 'A completed voice turn requested the Runtime entry; durable admission is separately verified.',
          details: { runtime: 'qwen_omni_realtime', route: deliveryRoute, woke: route.woke, admission_confirmed: false },
        })
        // Wake matching is activation only. The Runtime and canonical voice
        // record receive the same complete ASR, not a rewritten answer.
        const originalUtterance = utterance.trim()
        const result = capture ? await handlers.current.onTurn(originalUtterance, capture)
          : await handlers.current.onTurn(originalUtterance)
        const runtimeCompletedAt = Date.now()
        if (result.speak !== false && (stopped || generation !== replyGeneration)) {
          result.onPlaybackAbandoned?.()
          void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.reply_delivery', level: 'info',
            message: 'The runtime result was preserved without reviving an interrupted or disconnected reply.',
            details: { runtime: 'qwen_omni_realtime', route: deliveryRoute, delivered: false,
              reason_code: stopped ? 'REPLY_OWNER_CLOSED' : 'REPLY_OWNER_SUPERSEDED' } })
          return { responseText: result.responseText, speak: false, playbackAbandoned: true }
        }
        if (result.speak === false) {
          void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.local_reply_boundary', level: 'info',
            details: { runtime: 'qwen_omni_realtime', route: deliveryRoute, local_only: true } })
          return result
        }
        let deliverySettled = false, firstMediaObserved = false
        const ownsReply = (): boolean => ownsConnection() && generation === replyGeneration
        const claimSettlement = (): boolean => {
          if (deliverySettled) return false
          deliverySettled = true
          if (ownsReply()) return true
          result.onPlaybackAbandoned?.()
          return false
        }
        return {
          ...result,
          onPlaybackStart: () => {
            if (deliverySettled || firstMediaObserved || !ownsReply()) return
            firstMediaObserved = true
            void window.desktop?.recordRendererDiagnostic?.({
              event: 'voice.realtime.reply_first_audio', level: 'info',
              message: 'Fresh authorized remote PCM was observed while the media player was active; physical speaker audibility is not verified.',
              details: {
                runtime: 'qwen_omni_realtime', route: deliveryRoute,
                agent_runtime_ms: runtimeCompletedAt - dispatchedAt,
                first_audio_ms: Date.now() - dispatchedAt,
                observation_kind: deliveryRoute === 'realtime_tool' ? 'remote_pcm_with_active_player' : 'local_pcm_scheduled',
                physical_audibility_verified: false,
              },
            })
            result.onPlaybackStart?.()
          },
          onPlaybackEnd: () => {
            if (!claimSettlement()) return
            conversationState.lastActivityAt = Date.now()
            void window.desktop?.recordRendererDiagnostic?.({
              event: 'voice.realtime.reply_delivery', level: 'info',
              message: 'The Agent Runtime reply finished through the authorized realtime voice.',
              details: {
                runtime: 'qwen_omni_realtime', route: deliveryRoute,
                agent_runtime_ms: runtimeCompletedAt - dispatchedAt,
                total_turn_ms: Date.now() - dispatchedAt,
                delivered: true,
              },
            })
            result.onPlaybackEnd?.()
          },
          onPlaybackAbandoned: () => {
            if (!claimSettlement()) return
            void window.desktop?.recordRendererDiagnostic?.({
              event: 'voice.realtime.reply_delivery', level: 'error',
              message: 'The authorized Agent Runtime reply was not fully delivered.',
              details: {
                delivered: false,
                runtime: 'qwen_omni_realtime', route: deliveryRoute,
                agent_runtime_ms: runtimeCompletedAt - dispatchedAt,
                total_turn_ms: Date.now() - dispatchedAt,
              },
            })
            result.onPlaybackAbandoned?.()
          },
          onPlaybackUnconfirmed: () => {
            if (!claimSettlement()) return
            conversationState.lastActivityAt = Date.now()
            void window.desktop?.recordRendererDiagnostic?.({
              event: 'voice.realtime.reply_delivery', level: 'warn',
              message: 'Remote media was observed, but complete playback is unverified; no teaching completion was acknowledged.',
              details: {
                delivered: false, completion_verified: false,
                reason_code: 'QWEN_REALTIME_PLAYBACK_COMPLETION_UNVERIFIED',
                runtime: 'qwen_omni_realtime', route: deliveryRoute,
                agent_runtime_ms: runtimeCompletedAt - dispatchedAt,
                total_turn_ms: Date.now() - dispatchedAt,
              },
            })
            if (result.onPlaybackUnconfirmed) result.onPlaybackUnconfirmed()
            else result.onPlaybackAbandoned?.()
          },
        }
      }
      const dispatchLocalRuntimeInput = async (utterance: string, reason: 'reconnect' | 'committed' | undefined,
        capture?: QwenRealtimeInputCapture): Promise<void> => {
        if (!ownsConnection()) return
        void window.desktop?.recordRendererDiagnostic?.({
          event: reason === 'committed' ? 'voice.realtime.committed_input_dispatch'
            : reason === 'reconnect' ? 'voice.realtime.pending_input_recovery' : 'voice.realtime.semantic_recovery',
          level: reason === undefined ? 'warn' : 'info',
          message: reason === 'committed' ? 'Dispatching the original completed ASR without supplier tool selection; durable admission is separately verified.'
            : reason === 'reconnect' ? 'Recovering completed input which was not dispatched before transport loss.'
            : 'Realtime model did not call the runtime; recovering the committed utterance without playing its direct answer.' })
        const result = await dispatchRuntimeTurn(utterance, reason === 'committed' ? 'committed_input' : reason === 'reconnect' ? 'reconnect_recovery' : 'runtime_recovery', capture)
        if (result.speak === false) return
        const speechKey = `realtime-recovery-${crypto.randomUUID()}`
        let delivered = false, abandoned = false
        const abandon = (): void => {
          if (delivered || abandoned) return
          abandoned = true
          result.onPlaybackAbandoned?.()
        }
        const cancel = (): void => {
          if (delivered || abandoned) return
          abandon()
          stopCompanionSpeech(speechKey)
        }
        recoveryCancellations.add(cancel)
        try {
          const voiceResult = await speakCompanion(
            result.responseText,
            new LocalAgentStreamingTtsProvider(sessionId),
            undefined,
            () => { if (!delivered && !abandoned) result.onPlaybackStart?.() },
            {
              managedVoiceRequired: true,
              speechKey,
              onPlaybackEnd: () => {
                if (delivered || abandoned) return
                delivered = true
                result.onPlaybackEnd?.()
              },
              onNotDelivered: abandon,
            },
          )
          if (voiceResult === 'text_only') abandon()
        } finally { recoveryCancellations.delete(cancel) }
      }
      const nextSession = new QwenRealtimeVoiceSession({
        childName,
        audioDeviceId,
        requireCommittedTranscript: false,
        fullDuplex: true,
        nativeRuntime:currentNativeRuntime(()=>handlers.current.nativeRuntime,nativeRuntimeCurrent),
        inputMode: inputModeRef.current,
        explicitReplyBusy: () => handlers.current.interactionBusy || handlers.current.localTutorPlayback
          || handlers.current.explicitReplyBusy?.() === true,
        householdEntry:householdConsumed.current?undefined:householdEntryRef.current,
        onHouseholdEntryConsumed:recoveryEligible=>{
          if(!ownsConnection()||getVoiceCaptureOwner()!==owner.current||handlers.current.audioAllowed!==true)return
          householdConsumed.current=true
          if(recoveryEligible)nativeRecoveryWitness=true
          onHouseholdEntryConsumed?.()
        },
        nativeAudioAllowed:nativeRuntimeCurrent,
        nativeActivation:handlers.current.nativeRuntime?{canRecoverPersisted:()=>!backgroundRecoveryNoReplay&&nativeRecoveryWitness,isActive:()=>conversationState.active,word:()=>wakeWord,
          onExplicit:()=>activateConversation(false),
          onWake:(announce=true)=>activateConversation(announce,true),onPrivacyStop:()=>{privacyPause();handlers.current.onPrivacyStop?.()},onRecoveryWake:()=>{
            if(!ownsConnection()||!retryExhausted||handlers.current.audioAllowed!==true)return
            if(pendingReconnectInputs.length || pendingHandoffController || (householdEntryRef.current && !householdConsumed.current)) {
              publish({...lastStatus,phase:'error',speechActive:false,recoverable:false,
                detail:'原话仍在保存核对，暂不重复执行；请稍后重试。'})
              return
            }
            if(retryTimer!==null)window.clearTimeout(retryTimer);retryTimer=null
            retryExhausted=false;attempt=0;activateConversation(true);void connect()
          },recognizeStandby:(wav,signal,key)=>localApi.transcribeStandby(wav,signal,key)}:undefined,
        protectedPlaybackControl: { sessionId, childId, isCurrent: ()=>ownsConnection()&&getVoiceCaptureOwner()===owner.current, getLease: () => ownsConnection() ? getCompanionPlaybackLease() : null,
          recognize: (session, payload, signal) => localApi.localPlaybackControl(session, payload, signal),
          cancelExact: (id, generation) => ownsConnection() && cancelExactLocalSpeech(id, generation) },
        connectionGeneration: generation,
        captureMetadata: {binding:()=>({owner:owner.current,sessionId,childId,generation,current:ownsConnection(),captureOwner:getVoiceCaptureOwner(),leaseId,deviceHash:metadataDeviceHash})},
        callbacks: {
          onExplicitVoiceCapture: (state, detail) => {
            if (ownsConnection()) updateExplicitCapture(state, detail)
          },
          onExplicitVoiceInterrupt: () => {
            if (ownsConnection()) interruptExplicitPlayback()
          },
          onWorkspaceInput: (text, capture, voiceId) => ownsConnection() && capture.source === 'accepted_onset'
            && capture.sessionId === sessionId && capture.reference.child_id === childId
            && handlers.current.onWorkspaceInput?.(text, capture, voiceId) === true,
          onCurriculumTutor: (text, capture, dispatch, current) => ownsConnection() && capture.source === 'accepted_onset'
            && capture.sessionId === sessionId && capture.reference.child_id === childId
            && handlers.current.onCurriculumTutor?.(text, dispatch, () => ownsConnection() && current()) === true,
          captureInputContext: () => {
            const mistake = mistakeLearningOnset(sessionId,childId)
            return mistake ? {sessionId,reference:{child_id:childId,mistake_learning:mistake},carryoverConfirmationId:null}
              : handlers.current.visibleLearningSource?nativeVoiceOnsetContext(sessionId,childId):{sessionId,reference:{child_id:childId},carryoverConfirmationId:null}
          },
          onInputCompleted: (utterance, capture) => {
            const owned = ownsConnection()
            try {
              void Promise.resolve(window.desktop?.recordRendererDiagnostic?.({
                event: 'voice.realtime.input_progress', details: { input_stage: 'callback_owner_checked',
                  source_status: owned ? 'current' : 'owner_changed', runtime: 'qwen_omni_realtime' },
              })).catch(() => undefined)
            } catch { /* Preserve the original ownership decision when diagnostics fail. */ }
            if (!owned) return
            if(handlers.current.nativeRuntime){
              // Canonical storage already completed in nativeRuntime.admit.
              // Do not open the legacy dispatch/recovery journal a second time.
              if(conversationState.active)conversationState.lastActivityAt=Date.now()
              if(capture?.source==='accepted_onset'&&capture.sessionId===sessionId&&capture.reference.child_id===childId){nativeRecoveryWitness=true;attempt=0;slowRetryStep=0;repeatRequired=false}
              return
            }
            const original = utterance.trim()
            const route = routeQwenRealtimeUtterance(original, wakeWord, conversationState.active)
            if (capture) completedInputRoutes.set(capture.localInputId, { utterance: original, route })
            if (route.woke) activateConversation(route.kind === 'wake_only')
            if (route.kind !== 'conversation') return
            conversationState.lastActivityAt = Date.now()
            handlers.current.onInputCompleted?.(original, capture)
            // A handshake alone does not prove usable speech. Repeated
            // transcription failures must eventually leave this transport.
            if (capture?.source === 'accepted_onset' && capture.sessionId === sessionId
              && capture.reference.child_id === childId) { attempt = 0; repeatRequired = false }
          },
          onLocalSafety:(control,retained)=>{
            if(!ownsConnection())return
            retryExhausted=true;attempt=0;clearRefresh()
            if(retryTimer!==null)window.clearTimeout(retryTimer);retryTimer=null
            if(!retained){releaseLease('child_safety_recovery');releaseVoiceCapture(owner.current)}
            publish({...lastStatus,phase:'recovering',speechActive:false,pendingUtterances:0,detail:retained?'正在本机回应；云语音已暂停。想继续说话时，请重新叫我。':'正在本机回应；语音采集已停止，请家长重新开启。'})
            handlers.current.onLocalSafety?.(control)
          },
          onStatus: (status) => {
            if (!ownsConnection()) return
            if (conversationState.active && (status.speechActive || ['tool_running', 'speaking'].includes(status.phase))) {
              conversationState.lastActivityAt = Date.now()
            }
            const permanentGuidance = status.detail ? permanentProviderGuidance[classifyQwenRealtimeError(status.detail)] : undefined
            try { publish(permanentGuidance ? {...status, detail: permanentGuidance} : status) }
            finally {
              if (status.phase === 'error') scheduleRetry(status.detail ?? '实时语音连接异常。', 'status_error')
              else announceRepeatIfReady()
            }
          },
          onMicrophoneOpen: async (track) => {
            if (!ownsConnection()) throw new Error('实时语音连接已更换。')
            const registration = await mediaDeviceRegistration(track)
            if (!ownsConnection()) throw new Error('实时语音连接已更换。')
            const lease = await localApi.registerDevice(sessionId, 'microphone', registration.systemIdHash, registration.capabilities)
            if (!ownsConnection()) {
              await localApi.releaseDeviceLease(lease.lease_id, 'qwen_realtime_open_cancelled').catch(() => undefined)
              throw new Error('实时语音已停止。')
            }
            leaseId = lease.lease_id
            metadataDeviceHash = registration.systemIdHash
            renewalTimer = window.setInterval(() => {
              if (!ownsConnection() || leaseId !== lease.lease_id) return
              void localApi.renewDeviceLease(lease.lease_id).catch(() => {
                if (ownsConnection() && leaseId === lease.lease_id) scheduleRetry('麦克风状态续期失败。', 'lease_renewal')
              })
            }, 30_000)
          },
          onMicrophoneFallback: (failureName) => {
            if (!ownsConnection()) return
            void window.desktop?.recordRendererDiagnostic?.({
              event: 'voice.realtime.microphone_fallback', level: 'warn',
              message: '绑定麦克风不可用，已切换到电脑默认麦克风。',
              details: { name: failureName },
            })
          },
          onDuplexCapability: (browserAec) => {
            if (!ownsConnection()) return
            void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.duplex_capability',
              level: browserAec ? 'info' : 'warn', details: { browser_aec: browserAec, full_duplex: browserAec, acoustic_quality_verified: false } })
          },
          onEchoCancellationPolicy: (policy) => {
            if (!ownsConnection()) return
            void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.echo_cancellation_policy',
              level: policy.negotiation === 'rejected' || policy.negotiation === 'timed_out' ? 'warn' : 'info',
              details: {echo_mode:policy.mode,echo_negotiation:policy.negotiation,acoustic_quality_verified:false} })
          },
          onUserSpeechStart: () => {
            if (!ownsConnection()) return
            const publicSpeechWasActive = getCompanionVoiceState() === 'speaking' && !getCompanionPlaybackLocalOnly()
            invalidateReplies()
            if (!publicSpeechWasActive) return
            handlers.current.onPublicSpeechInterrupt?.()
            stopCompanionSpeech()
          },
          onInputRejected: (reason) => {
            if (!ownsConnection()) return
            void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.input_rejected', level: 'info',
              message: 'A realtime input without current capture authorization was ignored.',
              details: { runtime: 'qwen_omni_realtime', reason_code: reason, contains_transcript: false } })
          },
          onUserTranscript: (text) => {
            if (!ownsConnection()) return
            if (conversationState.active) handlers.current.onUserTranscript?.(text)
          },
          onAssistantTranscript: (text) => { if (ownsConnection()) handlers.current.onAssistantTranscript?.(text) },
          onToolCall: undefined,
          onCommittedTurn: undefined,
          onUntooledTurn: undefined,
          onError: (message) => {
            if (!ownsConnection()) return
            void window.desktop?.recordRendererDiagnostic?.({
              event: 'voice.realtime.runtime_error', level: 'error', message,
              details: { runtime: 'qwen_omni_realtime', attempt, reason_code: classifyQwenRealtimeError(message) },
            })
          },
        },
      })
      session = nextSession
      if (pendingReconnectInputs.length) nextSession.resumePendingInputs(pendingReconnectInputs.splice(0))
      applyPlaybackGate(nextSession)
      try {
        await nextSession.connect()
        if (ownsConnection()) scheduleRefresh()
      } catch (error) {
        nextSession.close('connect_failed')
        if (!ownsConnection()) return
        releaseLease('qwen_realtime_connect_failed')
        const message = error instanceof Error ? error.message : '实时语音连接失败。'
        scheduleRetry(message, 'connect_failed')
      }
    }

    inactivityTimer = window.setInterval(() => {
      announceRepeatIfReady()
      if (explicitSource || !wakeWord || !conversationState.active || handlers.current.interactionBusy || companionSpeaking || companionPreparing) return
      if (lastStatus.speechActive || ['tool_running', 'speaking', 'transcribing', 'recovering'].includes(lastStatus.phase)) return
      if (!shouldReturnToWakeStandby(true, conversationState.lastActivityAt, Date.now(), inactivitySeconds)) return
      conversationState.active = false
      conversationState.lastActivityAt = 0
      session?.syncConversationActivation()
      handlers.current.onConversationActiveChange?.(false)
      void window.desktop?.recordRendererDiagnostic?.({
        event: 'voice.realtime.inactivity_standby', level: 'info',
        message: 'Continuous conversation ended only after the configured inactivity interval.',
        details: { runtime: 'qwen_omni_realtime', inactivity_seconds: inactivitySeconds },
      })
      publish({ ...lastStatus, phase: 'wake_standby', speechActive: false, detail: undefined })
    }, 1_000)

    const interrupt = (): void => { invalidateReplies(); session?.interrupt() }
    const privacyPause = (): void => {
      stopped = true
      stopWaitingForCapture()
      connectionGeneration += 1
      invalidateReplies()
      if (retryTimer !== null) window.clearTimeout(retryTimer)
      retryTimer = null
      clearRefresh()
      pendingHandoffController?.abort()
      if (pendingHandoffTimer !== null) window.clearTimeout(pendingHandoffTimer)
      // Explicit privacy stop cancels the unsaved local speech as well as any
      // in-flight handoff. Keep only the non-sensitive rejection guidance.
      pendingReconnectInputs.length = 0
      if (explicitSource) updateExplicitCapture('idle')
      if (manualRecovery === 'playback_tail') setManualRecoveryKind(null)
      releaseLease('qwen_realtime_privacy_pause')
      session?.close('privacy_pause')
      releaseVoiceCapture(owner.current)
    }
    window.addEventListener('hsc-stop-realtime-speech', interrupt)
    window.addEventListener('hsc-privacy-pause', privacyPause)
    let attemptingCapture = false
    const stillAuthorized = (): boolean => !stopped && !privacyStopPending(sessionId) && handlers.current.active && handlers.current.audioAllowed
      && !!handlers.current.nativeRuntime && handlers.current.sessionId === sessionId
      && handlers.current.childId === childId && handlers.current.audioDeviceId === audioDeviceId
    const controls = {
      begin(source: ExplicitControlSource): boolean {
        if (explicitSource || source === 'screen' && !controlsVisible.current || !stillAuthorized()) return false
        const interruptWhileConnecting = (detail: string): boolean => {
          const interrupted = interruptExplicitPlayback()
          updateExplicitCapture('idle', detail)
          if (interrupted && source === 'keyboard') revealKeyboardControls.current?.()
          return interrupted
        }
        if (!session || retryExhausted) {
          return interruptWhileConnecting('语音还在连接，请稍后再试。')
        }
        explicitSource = source
        updateExplicitCapture('waiting')
        if (session.beginExplicitVoiceTurn()) {
          if (source === 'keyboard') revealKeyboardControls.current?.()
          return true
        }
        return interruptWhileConnecting('麦克风暂未就绪，请稍后再试。')
      },
      finish(source: ExplicitControlSource): void {
        if (source !== explicitSource || explicitFinishing) return
        explicitFinishing = true
        if (stillAuthorized()) session?.finishExplicitVoiceTurn()
        else controls.cancel()
      },
      cancel(): void {
        if (!explicitSource) return
        session?.cancelExplicitVoiceTurn()
        updateExplicitCapture('idle', '录音已取消，请重新说一次。')
      },
      setMode(mode: VoiceInputMode): void {
        session?.setVoiceInputMode(mode)
        updateExplicitCapture('idle')
      },
    }
    explicitControls.current = controls
    const keyboardScope = () => ({ ...voiceCaptureScopeStamp(sessionId, childId), learningVisible: handlers.current.visibleLearningSource,
      mistake: JSON.stringify(mistakeLearningOnset(sessionId, childId)) })
    let pressedScope: ReturnType<typeof keyboardScope> | null = null
    const sameKeyboardScope = (): boolean => {
      if (!pressedScope) return false
      const now = keyboardScope()
      return pressedScope.value===now.value && pressedScope.documentOwner===now.documentOwner
        && pressedScope.learningVisible===now.learningVisible && pressedScope.mistake===now.mistake
    }
    const spaceBinding = registerAppSpaceVoice({ priority: APP_SPACE_VOICE_PRIORITY.focused,
      eligible: stillAuthorized,
      begin: () => {
        pressedScope = keyboardScope()
        if (controls.begin('keyboard')) return true
        pressedScope = null
        return false
      },
      finish: () => {
        if (sameKeyboardScope()) controls.finish('keyboard')
        else if (explicitSource === 'keyboard') controls.cancel()
        pressedScope = null
      },
      cancel: () => { pressedScope = null; if (explicitSource === 'keyboard') controls.cancel() } })
    const verifyKeyboardScope = (): void => { if (pressedScope && !sameKeyboardScope()) spaceBinding.cancel() }
    refreshKeyboardScope.current = verifyKeyboardScope
    const unsubscribeKeyboardScope = subscribeVoiceCaptureContext(verifyKeyboardScope)
    window.addEventListener('hsc-mistake-learning-surface', verifyKeyboardScope)
    const cancelScreen = (): void => { if (explicitSource === 'screen') controls.cancel() }
    const visibilityChange = (): void => { if (document.hidden) cancelScreen() }
    window.addEventListener('blur', cancelScreen)
    document.addEventListener('visibilitychange', visibilityChange)
    const tryStartCapture = (): void => {
      if (retryExhausted || attemptingCapture || !stillAuthorized()) return
      attemptingCapture = true
      try {
        if (!claimVoiceCapture(owner.current)) return
        // Claiming notifies other listeners synchronously. A privacy stop or an
        // ownership change during that notification must win over startup.
        if (!stillAuthorized() || getVoiceCaptureOwner() !== owner.current) {
          releaseVoiceCapture(owner.current)
          return
        }
        stopWaitingForCapture()
        void connect()
      } finally {
        attemptingCapture = false
      }
    }
    unsubscribeCaptureWait = subscribeVoiceCapture(tryStartCapture)
    if (priorPermanent) {
      setManualRecoveryKind(priorPermanent.kind)
      const detail = priorPermanent.pending.length ? `${priorPermanent.guidance} 原话的保存仍需核对，点击重试会核对并保存原话。` : priorPermanent.guidance
      publish({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:false,detail})
      handlers.current.onPermanentFailure?.(detail)
    }
    tryStartCapture()
    if (!retryExhausted && getVoiceCaptureOwner() !== owner.current && stillAuthorized()) {
      publish({ ...lastStatus, phase: 'recovering', recoverable: true,
        detail: '正在等待其他语音任务释放麦克风，释放后会自动恢复。' })
    }
    return () => {
      if (refreshPlaybackGate.current === refreshGate) refreshPlaybackGate.current = null
      if (explicitControls.current === controls) explicitControls.current = null
      controls.cancel()
      stopped = true
      stopWaitingForCapture()
      connectionGeneration += 1
      invalidateReplies()
      activation.current = null
      manualRetry.current = null
      setManualRecoveryKind(null)
      window.removeEventListener('hsc-stop-realtime-speech', interrupt)
      window.removeEventListener('hsc-privacy-pause', privacyPause)
      spaceBinding.unregister()
      if (refreshKeyboardScope.current === verifyKeyboardScope) refreshKeyboardScope.current = null
      unsubscribeKeyboardScope()
      window.removeEventListener('hsc-mistake-learning-surface', verifyKeyboardScope)
      window.removeEventListener('blur', cancelScreen)
      document.removeEventListener('visibilitychange', visibilityChange)
      if (retryTimer !== null) window.clearTimeout(retryTimer)
      clearRefresh()
      pendingHandoffController?.abort()
      if (pendingHandoffTimer !== null) window.clearTimeout(pendingHandoffTimer)
      if (!handlers.current.audioAllowed) pendingReconnectInputs.length = 0
      if (inactivityTimer !== null) window.clearInterval(inactivityTimer)
      releaseLease('qwen_realtime_unmounted')
      session?.close('component_unmounted')
      unsubscribeCompanionVoice()
      releaseVoiceCapture(owner.current)
    }
  }, [active, audioDeviceId, childId, childName, inactivitySeconds, sessionId, wakeWord, audioAllowed, !!nativeRuntime])

  if (!active || !audioAllowed || !nativeRuntime || !explicitControlsVisible) return null
  const capture: typeof explicitCapture = explicitCapture.scope === conversationKey ? explicitCapture : { scope: conversationKey, state: 'idle' }
  const manualRecording = capture.state !== 'idle'
  const modeHint = inputMode === 'hands-free' && controlPhase === 'wake_standby' && wakeWord
    ? `说“${wakeWord}”唤醒，也可以按住空格说话` : inputMode === 'hands-free' ? '免按监听中，也可以按住空格说话' : '按住空格说话，松开发送'
  const hint = capture.state === 'waiting' ? '正在准备麦克风，等提示后再说'
    : capture.state === 'ready' ? capture.source === 'keyboard' ? '正在听，松开空格发送' : '正在听，说完点“结束并发送”'
    : capture.detail ?? (controlPhase === 'connecting' || controlPhase === 'recovering' ? '语音正在连接'
      : controlPhase === 'error' ? '语音暂不可用' : controlPhase === 'speaking' || localTutorPlayback ? '小知正在回答，可以按住空格打断'
        : controlPhase === 'transcribing' || controlPhase === 'tool_running' ? '正在处理你刚才说的话' : modeHint)

  const toggleHandsFree = (): void => {
    const mode = inputMode === 'manual' ? 'hands-free' : 'manual'
    inputModeRef.current = mode
    explicitControls.current?.setMode(mode)
    setPreferenceError(saveVoiceInputMode(childId, mode) ? '' : '已切换，本次语音设置未能保存。')
  }

  return <>
    <div className="agent-realtime-voice" data-testid="realtime-voice-controls" data-input-mode={inputMode} data-capture-state={capture.state}>
      <div className="agent-realtime-actions">
        <button type="button" className="primary-button agent-realtime-talk" data-testid="realtime-talk"
          aria-keyshortcuts="Space" disabled={capture.source === 'keyboard' || Boolean(manualRecoveryKind)}
          onClick={event => {
            if (event.detail > 0) event.currentTarget.blur()
            if (manualRecording) explicitControls.current?.finish('screen')
            else explicitControls.current?.begin('screen')
          }}>
          {manualRecording ? <Square size={16} aria-hidden="true" /> : <Mic size={16} aria-hidden="true" />}
          {manualRecording ? '结束并发送' : '开始说话'}
        </button>
        <button type="button" className="secondary-button agent-realtime-mode" role="switch" data-testid="realtime-hands-free"
          aria-checked={inputMode === 'hands-free'} disabled={manualRecording} onClick={event => {
            if (event.detail > 0) event.currentTarget.blur()
            toggleHandsFree()
          }}>免按监听</button>
      </div>
      <span className="agent-realtime-hint" role="status">{preferenceError || hint}</span>
    </div>
    {manualRecoveryKind && <button className="secondary-button agent-playback-retry" type="button"
      onClick={() => manualRetry.current?.()}>{manualRecoveryKind === 'playback_tail' ? '重试麦克风' : '已检查设置，重试云语音'}</button>}
  </>
}
