import { NativeResponseDiagnostics, type NativeResponseCancelSource } from './qwenNativeResponseDiagnostic'
import { RealtimeCaptureMetadataObserver, classifyCaptureRoute, registerCurrentRealtimeMetadataReader, type CaptureScope, type CaptureState } from './currentRealtimeCaptureMetadata'
import { protectedPlaybackBarrier, PLAYBACK_TAIL_FAILURE } from './protectedPlaybackBarrier'
import { ProtectedPlaybackControl, type LocalPlaybackControlPayload, type LocalPlaybackControlResult } from './protectedPlaybackControl'
import type { CompanionPlaybackLease } from './companionVoice'
import type { RealtimeVoiceStatus } from './realtimeVoiceRuntime'
import { browserMediaErrorName } from './mediaDevices'
import type { VoiceCaptureReference } from './voiceCaptureContext'
import { microphoneEchoAllowsDuplex, negotiateMicrophoneEchoCancellation, type MicrophoneEchoPolicy } from './microphoneEchoCancellation'
import { RemotePlaybackObservation } from './remotePlaybackObservation'
import { RealtimeAudioActivityHealth } from './realtimeAudioActivityHealth'
import { LocalAttentionControl } from './localAttentionControl'
import { LocalStandbyWakeControl } from './localStandbyWakeControl'
import type { StandbyVoiceInput } from './types'
import { isImmediatePrivacyStop } from './deviceCommandReceipt'
import { matchWakeWord } from './wakeWordRuntime'
import { QwenNativeConversation, qwenNativePublicToolOutput } from './qwenNativeConversation'
import {NativeActivationAudioBuffer,registeredWakePrefix,isExactStandbyPrivacyStop} from './nativeActivationAudio'
import { QwenManualAudio } from './qwenManualAudio'
import { realtimeInputSourceStatus } from './realtimeInputCapture'
import { QwenNativePcmDownlink } from './qwenNativePcmDownlink'
import type { QwenNativeSocketEvent } from '../../shared/qwenNativeSocket'
import { qwenNativeFormatCorrectionExhausted, qwenNativeInstructions, qwenNativeContextInstructions, fitQwenNativeSession, recoverQwenNativeContext, qwenRuntimeTools, type QwenNativeRuntimeBridge, type QwenNativeRuntimeContext, type QwenRuntimeCapabilityCatalog, type QwenNativeToolReceipt, type QwenNativePersistedInput,type QwenNativeLocalSafetyControl, type QwenCurriculumTutorDispatch } from './qwenNativeProtocol'

export const QWEN_REALTIME_TOOL_NAME = 'handle_child_turn'
const NATIVE_KEEPALIVE_INTERVAL_MS = 180_000
const NATIVE_KEEPALIVE_RECHECK_MS = 5_000

export type QwenRealtimeInputContext = {
  sessionId: string
  reference: VoiceCaptureReference
  carryoverConfirmationId: string | null
}
export type QwenRealtimeInputCapture = QwenRealtimeInputContext & {
  localInputId: string
  capturedAt: number
  source: 'accepted_onset' | 'unbound'
}
export type QwenRealtimeQueuedInput = { utterance: string; capture?: QwenRealtimeInputCapture }
export type VoiceInputMode = 'manual' | 'hands-free'

export type QwenRealtimeToolResult = {
  responseText: string
  /** False for already-local delivery or an abandoned stale reply. */
  speak?: boolean
  /** The caller has already settled the abandoned delivery receipt. */
  playbackAbandoned?: boolean
  onPlaybackStart?: () => void
  onPlaybackEnd?: () => void
  /** Media was observed, but RTC exposes no per-reply rendered EOS. This
   * receipt must not advance teaching or be treated as complete delivery. */
  onPlaybackUnconfirmed?: () => void
  onPlaybackAbandoned?: () => void
}

export type QwenRealtimeSessionCallbacks = {
  onStatus: (status: RealtimeVoiceStatus) => void
  onMicrophoneOpen?: (track: MediaStreamTrack) => Promise<void>
  onMicrophoneFallback?: (failureName: string) => void
  onDuplexCapability?: (browserAec: boolean) => void
  onEchoCancellationPolicy?: (policy: MicrophoneEchoPolicy) => void
  onUserSpeechStart?: () => void
  onExplicitVoiceCapture?: (state: 'idle' | 'waiting' | 'ready', detail?: string) => void
  onExplicitVoiceInterrupt?: () => void
  captureInputContext?: () => QwenRealtimeInputContext
  onInputRejected?: (reason: 'microphone_closed' | 'input_item_denied' | 'input_item_invalid' | 'anonymous_input_overflow') => void
  onUserTranscript?: (text: string) => void
  /** Store accepted final input independently of supplier tool selection;
   * this notification never executes a semantic action. */
  onInputCompleted?: (utterance: string, capture?: QwenRealtimeInputCapture) => void
  onWorkspaceInput?: (utterance: string, capture: QwenRealtimeInputCapture, voiceInputId: string) => boolean
  onCurriculumTutor?: (utterance: string, capture: QwenRealtimeInputCapture, dispatch: QwenCurriculumTutorDispatch, isCurrent: () => boolean) => boolean
  onAssistantTranscript?: (text: string) => void
  onToolCall?: (utterance: string, capture?: QwenRealtimeInputCapture) => Promise<QwenRealtimeToolResult>
  onUntooledTurn?: (utterance: string, reason?: 'reconnect', capture?: QwenRealtimeInputCapture) => Promise<void>
  /** Deterministic production route: every completed ASR turn is submitted
   * exactly once, independent of whether the realtime model elects to call a
   * tool or produce an answer of its own. */
  onCommittedTurn?: (utterance: string, capture?: QwenRealtimeInputCapture) => Promise<void>
  onError?: (message: string) => void
  onLocalSafety?:(control:QwenNativeLocalSafetyControl,localRecoveryRetained:boolean)=>void
}

// Only fixed, reviewed browser error classes leave this boundary. Never put a
// device-specific DOM exception message (which may contain private data) in logs.
export function qwenPlayFailureMessage(error: unknown): string {
  let name: unknown
  try { name = typeof error === 'object' && error !== null ? (error as { name?: unknown }).name : undefined } catch { name = undefined }
  switch (name) {
    case 'NotAllowedError': return '实时语音播放被系统权限阻止。'
    case 'NotSupportedError': return '实时语音播放格式或设备不受支持。'
    case 'AbortError': return '实时语音播放被中断。'
    case 'InvalidStateError': return '实时语音播放状态无效。'
    default: return '实时语音播放器启动失败。'
  }
}

const realtimeAudioConstraints = (audioDeviceId?: string): MediaTrackConstraints => ({
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  channelCount: 1,
  ...(audioDeviceId ? { deviceId: { exact: audioDeviceId } } : {}),
})

export async function openQwenRealtimeMicrophone(
  audioDeviceId?: string,
  getUserMedia: (constraints: MediaStreamConstraints) => Promise<MediaStream> = (constraints) => navigator.mediaDevices.getUserMedia(constraints),
): Promise<{ stream: MediaStream; usedDefaultFallback: boolean; failureName?: string }> {
  try {
    return {
      stream: await getUserMedia({ audio: realtimeAudioConstraints(audioDeviceId), video: false }),
      usedDefaultFallback: false,
    }
  } catch (error) {
    const failureName = browserMediaErrorName(error) || 'Error'
    // Permission denial applies to every device and must remain visible. A
    // missing, disconnected, busy or stale bound device may still leave the
    // computer's default microphone usable.
    if (!audioDeviceId || ['NotAllowedError', 'SecurityError'].includes(failureName)) throw error
    const stream = await getUserMedia({ audio: realtimeAudioConstraints(), video: false })
    return { stream, usedDefaultFallback: true, failureName }
  }
}

type RealtimeServerEvent = {
  type?: string
  event_id?: string
  item_id?: string
  response_id?: string
  transcript?: string
  text?: string
  stash?: string
  delta?: string
  name?: string
  call_id?: string
  arguments?: string
  item?: {id?:string;type?:string;role?:string;content?:{type?:string;text?:string}[]}
  response?: { id?: string; status?: string; status_details?: unknown }
  session?: { id?: string; turn_detection?: unknown;instructions?:string;model?:string;tools?:{function?:{name?:string}}[] }
  error?: { message?: string; code?: string }
}

/** Process-local canonical ACK projection. Never log it or use it as execution authority. */
function nativeSessionAckConfiguration(session: NonNullable<RealtimeServerEvent['session']>, fields: readonly string[]): string | null {
  const raw = session as Record<string,unknown>
  const configuration = Object.fromEntries(fields.map(key => [key,key === 'turn_detection' ? raw[key] ?? null : raw[key]]))
  try {
    const serialized = JSON.stringify(configuration, (_key,value) => isEventObject(value)
      ? Object.fromEntries(Object.keys(value).sort().map(key => [key,value[key]])) : value)
    return typeof serialized === 'string' ? serialized : null
  } catch { return null }
}

function isEventObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function parseServerEvent(raw: string): RealtimeServerEvent | null {
  let value: unknown
  try { value = JSON.parse(raw) } catch { return null }
  if (!isEventObject(value) || typeof value.type !== 'string') return null
  for (const key of ['event_id', 'item_id', 'response_id', 'transcript', 'text', 'stash', 'delta', 'name', 'call_id', 'arguments']) {
    if (value[key] !== undefined && typeof value[key] !== 'string') return null
  }
  for (const [key, fields] of [['response', ['id', 'status']], ['error', ['message', 'code']], ['session', ['id']]] as const) {
    if (value[key] === undefined) continue
    const nested = value[key]
    if (!isEventObject(nested) || fields.some(field => nested[field] !== undefined && typeof nested[field] !== 'string')) return null
  }
  return value as RealtimeServerEvent
}

function validResponseId(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 256 && !/\s/.test(value)
}

type ToolCall = QwenRealtimeQueuedInput & { callId: string; reconnect?: true }

const nativeSocketCodes=['QWEN_NATIVE_CREDENTIAL_REJECTED','QWEN_NATIVE_ACCESS_DENIED','QWEN_NATIVE_ACCOUNT_BALANCE','QWEN_NATIVE_SOCKET_UNCONFIRMED','QWEN_NATIVE_SOCKET_OWNER_CHANGED','QWEN_NATIVE_SOCKET_POLICY_UNCONFIRMED','QWEN_NATIVE_SOCKET_PROVIDER_ERROR','QWEN_NATIVE_SOCKET_PAYLOAD_INVALID','QWEN_NATIVE_SOCKET_BACKPRESSURE','QWEN_NATIVE_SOCKET_SEND_UNCONFIRMED'] as const
const nativeSocketFailure=(error:unknown,fallback:string):Error=>new Error(nativeSocketCodes.find(code=>String(error instanceof Error?error.message:error).includes(code))??fallback)

const childTurnTool = {
  type: 'function',
  function: {
    name: QWEN_REALTIME_TOOL_NAME,
    description: '将孩子的完整话语交给家庭学习伙伴运行时。课程辅导、学习计划、错题、设备、记忆、提醒和陪伴活动都必须通过该工具执行。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      required: ['utterance'],
      properties: {
        utterance: {
          type: 'string',
          description: '保留原意的完整转写，不要改写、总结或只保留最后一句。',
        },
      },
    },
  },
} as const

export function qwenRealtimeInstructions(childName: string, deterministicFrontend = false): string {
  if (deterministicFrontend) return [
    `你是${childName}的实时语音转写前台。`,
    '只负责语音活动检测和完整转写，不回答问题、不解题、不播报、不调用工具。',
    '保留孩子连续说话的完整原意，不要只保留最后一句。',
    '忽略键盘声、扬声器回声、咳嗽、短促语气词和旁人背景话。',
  ].join('\n')
  return [
    `你是${childName}的家庭学习伙伴的实时语音前台。`,
    '你的任务是听懂孩子、自然表达，并调度后台已有的学习、记忆、设备和陪伴能力。',
    `对每一个有意义的孩子话语，必须且只能调用 ${QWEN_REALTIME_TOOL_NAME}；在工具返回前不得自己解题、编造记忆、声称已操作设备或自行执行任务。`,
    '工具返回 JSON 后，逐字播报 response_text；不要增删、改写、总结，也不要增加未经工具确认的事实。',
    '不要把键盘声、扬声器回声、咳嗽、短促语气词或旁人背景话当成孩子指令。无有效话语时保持安静。',
    '语音回复不说 Markdown、编号符号或系统术语，一次先说一到三句，需要时再继续。',
  ].join('\n')
}

export function qwenRealtimeSessionUpdate(childName: string, voice = 'Tina', deterministicFrontend = false): Record<string, unknown> {
  return {
    type: 'session.update',
    session: {
      modalities: ['text', 'audio'],
      instructions: qwenRealtimeInstructions(childName, deterministicFrontend),
      input_audio_transcription: { model: 'qwen3-asr-flash-realtime' },
      turn_detection: {
        // The transcription frontend delegates semantic admission to Agent Runtime.
        type: deterministicFrontend ? 'server_vad' : 'semantic_vad',
        threshold: 0.5,
        prefix_padding_ms: 500,
        silence_duration_ms: 800,
      },
      audio: {
        input: { format: { type: 'pcm', sample_rate: 16000 } },
        output: { voice, format: { type: 'pcm', sample_rate: 24000 } },
      },
      ...(deterministicFrontend ? {} : { tools: [childTurnTool] }),
      temperature: 0,
    },
  }
}

export function parseQwenToolCall(event: RealtimeServerEvent, fallbackTranscript = ''): ToolCall | null {
  if (event.type !== 'response.function_call_arguments.done' || event.name !== QWEN_REALTIME_TOOL_NAME
    || typeof event.call_id !== 'string' || !event.call_id.trim()) return null
  const committedTranscript = fallbackTranscript.trim()
  let utterance = ''
  try {
    const values = JSON.parse(event.arguments ?? '{}') as { utterance?: unknown }
    if (typeof values.utterance === 'string') utterance = values.utterance.trim()
  } catch {
    utterance = ''
  }
  // The ASR-completed transcript is the auditable child utterance. The
  // realtime model may summarize function arguments, so never let its rewrite
  // replace a completed transcript.
  if (committedTranscript) utterance = committedTranscript
  return utterance ? { callId: event.call_id, utterance } : null
}

function eventText(event: RealtimeServerEvent): string {
  if (typeof event.transcript === 'string') return event.transcript.trim()
  if (typeof event.text === 'string') return event.text.trim()
  return ''
}

export function normalizeQwenAnswerSdp(value: string): string {
  const normalized = String(value).trim().replace(/\r?\n/g, '\r\n')
  return normalized.endsWith('\r\n') ? normalized : `${normalized}\r\n`
}

function waitForIceGathering(peer: RTCPeerConnection, timeoutMs = 8_000): Promise<void> {
  if (peer.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      peer.removeEventListener('icegatheringstatechange', changed)
      reject(new Error('麦克风实时连接协商超时。'))
    }, timeoutMs)
    const changed = (): void => {
      if (peer.iceGatheringState !== 'complete') return
      window.clearTimeout(timer)
      peer.removeEventListener('icegatheringstatechange', changed)
      resolve()
    }
    peer.addEventListener('icegatheringstatechange', changed)
  })
}

export class QwenRealtimeVoiceSession {
  private peer: RTCPeerConnection | null = null
  private stream: MediaStream | null = null
  private sender: RTCRtpSender | null = null
  private audioTrack: MediaStreamTrack | null = null
  private localSourceTrack: MediaStreamTrack | null = null
  private protectedControl: ProtectedPlaybackControl | null = null
  private protectedControlHold = false
  private nativeHostConnectionId: string | null = null
  private nativeVendorGeneratingResponseId:string|null=null
  private nativeActivationBuffer:NativeActivationAudioBuffer|null=null
  private nativeActivationEpoch=0
  private nativeAudioDeltaSeen=false
  private nativePlaybackObserverUnavailable=false
  private nativeSocketOpen = false
  private nativeSocketReady = false
  private nativeSocketUnsubscribe: (()=>void)|null = null
  private nativeEarlyPackets: QwenNativeSocketEvent[] = []
  private nativeEarlyBytes = 0
  private nativeSendTail: Promise<void> = Promise.resolve()
  private nativePendingSendBytes = 0
  private nativeSendEpoch = 0
  private nativeWasAppendAllowed = false
  private nativeVoice: string | undefined
  private nativeModel:string|undefined
  private nativeExpectedInstructions:string|undefined
  private nativeDownlink: QwenNativePcmDownlink | null = null
  private nativeLiveInputObserved=false
  private householdEntryConsumed=false
  private householdEntryLoading=false
  private householdPending:{entry:import('./types').HouseholdNativeEntry;voiceInputId:string;timer:ReturnType<typeof setTimeout>}|null=null
  private nativeRestoreTried=false
  private nativeRestoreLoading=false
  private nativeRestore:{original:QwenNativePersistedInput;timer:ReturnType<typeof setTimeout>}|null=null
  private nativeSessionDeadline: ReturnType<typeof setTimeout>|null = null
  private nativeSupplierSessionId: string | null = null
  private nativeCatalog: QwenRuntimeCapabilityCatalog | null = null
  private nativeContext: QwenNativeRuntimeContext | null = null
  private nativeConversation: QwenNativeConversation | null = null
  private nativeSessionAckConfiguration: string | null = null
  private nativeSessionAckFields: readonly string[] = []
  private nativeKeepaliveTimer: ReturnType<typeof setTimeout> | null = null
  private nativeKeepaliveSentAt: number | null = null
  private readonly nativeResponseDiagnostics = new NativeResponseDiagnostics()
  private manualAudio: QwenManualAudio | null = null
  private voiceInputMode: VoiceInputMode
  private explicitCapture: 'idle' | 'waiting' | 'ready' = 'idle'
  private explicitReplyPending = false
  private explicitQuietUntil = 0
  private nativeTools = new Map<string, { done: boolean; sending?:boolean; calls: { id: string; result?: QwenNativeToolReceipt; sent?:boolean }[] }>()
  private nativeActivityTimer: ReturnType<typeof setTimeout> | null = null
  private nativeQuietTimer: ReturnType<typeof setTimeout> | null = null
  private nativeRemoteMediaActive = false
  private nativeLastFrameAt = 0
  private nativeFrameCount = 0
  private nativeReleasedAt = 0
  private localWakeRecovery = false
  private privateSapiTail: { sessionId: string; childId: string; root: MediaStreamTrack; cloud: MediaStreamTrack; id: string; generation: number } | null = null
  private localAttention: LocalAttentionControl | null = null
  private standbyWake: LocalStandbyWakeControl | null = null
  private metadataObserver: RealtimeCaptureMetadataObserver | null = null
  private retireMetadataReader: (() => void) | null = null
  private protectedTailTimer: ReturnType<typeof setTimeout> | null = null
  private channel: RTCDataChannel | null = null
  private readonly retiredChannels = new WeakSet<RTCDataChannel>()
  private remoteAudio: HTMLAudioElement | null = null
  private remotePlaybackObservation: RemotePlaybackObservation | null = null
  private closed = false
  private sessionCreated = false
  private sessionReady = false
  private sessionUpdateSent = false
  private microphoneReleased = false
  private microphoneEnded = false
  private readonly audioActivityHealth = new RealtimeAudioActivityHealth()
  private audioHealthTimer: number | null = null
  private audioHealthReadAbort: AbortController | null = null
  private audioHealthEpoch = 0
  private audioHealthReading = false
  private audioHealthReadDone: Promise<void> | null = null
  private inputDeliveryFailed = false
  private lastTranscript = ''
  private assistantTranscript = ''
  private toolRunning = false
  private pendingPlayback: QwenRealtimeToolResult | null = null
  private drainingPlayback: QwenRealtimeToolResult | null = null
  private playbackDrainTimer: number | null = null
  private playbackDeadline: number | null = null
  private playbackSettled: (() => void) | null = null
  private unboundToolCalls: RealtimeServerEvent[] = []
  private transcriptRecoveryTimer: number | null = null
  private playbackStarted = false
  private playbackAuthorized = false
  private responseContainsToolCall = false
  private responseActive = false
  private activeResponseId: string | null = null
  private readonly seenResponseIds = new Set<string>()
  private responseIdentityExhausted = false
  private protocolRejections = new Set<string>()
  private failureReported = false
  private cancelledResponseIds = new Set<string>()
  private committedAudioItemIds = new Set<string>()
  private unauthorizedOutputObserved = false
  private recoveringUntooledTurn = false
  private committedTurnQueue: QwenRealtimeQueuedInput[] = []
  private drainingCommittedTurns = false
  private pendingToolCalls: ToolCall[] = []
  private drainingToolCalls = false
  private seenToolCallIds = new Set<string>()
  private completedTranscriptQueue: QwenRealtimeQueuedInput[] = []
  private responseTranscript: string | null = null
  private responseInput: QwenRealtimeQueuedInput | null = null
  private lastInputCapture: QwenRealtimeInputCapture | undefined
  private externalPlaybackActive = false
  private externalPlaybackLocalOnly = true
  private replyGeneration = 0
  private inputItemPermissions = new Map<string, boolean>()
  private inputItemCaptures = new Map<string, QwenRealtimeInputCapture>()
  private matchedInputCapture: QwenRealtimeInputCapture | undefined
  private pendingInputIds = new Set<string>()
  private inputStartEventIds = new Set<string>()
  private inputOnsetObserved = false
  private anonymousInputPermissions: boolean[] = []
  private anonymousInputCaptures: (QwenRealtimeInputCapture | undefined)[] = []
  private anonymousInputOverflow = false

  constructor(
    private readonly options: {
      childName: string
      audioDeviceId?: string
      voice?: string
      /** Product mode forbids model-rewritten arguments without committed ASR. */
      requireCommittedTranscript?: boolean
      /** Enable duplex only when Chromium actually reports AEC on the track.
       * This is a capability gate, not proof of the room's acoustic quality. */
      fullDuplex?: boolean
      protectedPlaybackControl?: { sessionId: string; childId: string; isCurrent: () => boolean; getLease: () => CompanionPlaybackLease | null;
        recognize: (session: string, payload: LocalPlaybackControlPayload, signal: AbortSignal) => Promise<LocalPlaybackControlResult>;
        cancelExact: (id: string, generation: number) => boolean }
      nativeRuntime?: QwenNativeRuntimeBridge
      inputMode?: VoiceInputMode
      explicitReplyBusy?: () => boolean
      householdEntry?:import('./types').HouseholdNativeEntry
      onHouseholdEntryConsumed?:(recoveryEligible:boolean)=>void
      nativeAudioAllowed?: () => boolean
      nativeActivation?: { canRecoverPersisted?:()=>boolean; isActive: () => boolean; word: () => string | undefined; onWake: (announce?:boolean) => void; onExplicit?: () => void; onRecoveryWake?: () => void; onPrivacyStop?: () => void;
        recognizeStandby: (wav: string, signal: AbortSignal, actionId: ReturnType<Crypto['randomUUID']>) => Promise<StandbyVoiceInput> }
      captureMetadata?: CaptureScope
      connectionGeneration?: number
      callbacks: QwenRealtimeSessionCallbacks
    },
  ) {
    this.voiceInputMode = this.options.inputMode ?? 'hands-free'
    if(this.options.captureMetadata){
      this.metadataObserver=new RealtimeCaptureMetadataObserver(this.options.captureMetadata)
      this.retireMetadataReader=registerCurrentRealtimeMetadataReader(this.options.captureMetadata,()=>this.readCaptureMetadata())
    }
  }

  /** Model bound to this native transport by the main-process open result. */
  getConnectedModel(): string | undefined {
    return !this.closed && this.nativeSocketOpen && this.nativeHostConnectionId ? this.nativeModel : undefined
  }

  async connect(): Promise<void> {
    if(this.options.nativeRuntime)return this.connectNativeSocket()
    if (this.peer || this.closed || this.inputDeliveryFailed) return
    const negotiate = window.desktop?.negotiateQwenRealtime
    if (!negotiate) throw new Error('当前客户端不支持千问实时语音。')
    this.publish('connecting', false, '正在建立端到端实时语音连接')
    try {
      const protectedScope=this.options.protectedPlaybackControl
      if(protectedPlaybackBarrier.held()&&(!protectedScope||!protectedPlaybackBarrier.reserveOpen(protectedScope,performance.now())))throw new Error(PLAYBACK_TAIL_FAILURE)
      this.watchProtectedTailDeadline()
      const captureRoute=classifyCaptureRoute(navigator.mediaDevices.getUserMedia)
      const opened = await openQwenRealtimeMicrophone(this.options.audioDeviceId)
      const stream = opened.stream
      if (opened.usedDefaultFallback) this.options.callbacks.onMicrophoneFallback?.(opened.failureName ?? 'Error')
      if (this.closed) { stream.getTracks().forEach((track) => track.stop()); return }
      this.stream = stream
      const track = stream.getAudioTracks()[0] ?? null
      if (!track) throw new Error('未获得可用的麦克风音轨。')
      if (track.readyState === 'ended') throw new Error('麦克风音轨已断开，正在重新连接。')
      this.localSourceTrack = track
      this.metadataObserver?.opened(stream,track,captureRoute)
      track.addEventListener('ended', () => {
        if (this.closed || this.localSourceTrack !== track) return
        this.microphoneEnded = true
        if (this.audioTrack) this.audioTrack.enabled = false
        this.fail(new Error('麦克风音轨已断开，正在重新连接。'))
      }, { once: true })
      const echoPolicy = await negotiateMicrophoneEchoCancellation(track)
      if (this.closed || this.microphoneEnded) return
      const cloudTrack = track.clone()
      cloudTrack.enabled = false
      this.audioTrack = cloudTrack
      this.metadataObserver?.cloned(track,cloudTrack)
      cloudTrack.addEventListener('ended', () => {
        if (this.closed || this.audioTrack !== cloudTrack) return
        this.microphoneEnded = true
        cloudTrack.enabled = false
        this.fail(new Error('麦克风传输音轨已断开，正在重新连接。'))
      }, { once: true })
      this.options.callbacks.onEchoCancellationPolicy?.(echoPolicy)
      this.options.callbacks.onDuplexCapability?.(this.options.fullDuplex === true && microphoneEchoAllowsDuplex(cloudTrack))
      await this.options.callbacks.onMicrophoneOpen?.(track)
      if (this.closed || this.microphoneEnded) return
      await this.attachOwnedControlFanout(stream,track,cloudTrack)

      const peer = new RTCPeerConnection({ iceServers: [] })
      this.peer = peer
      this.sender = peer.addTrack(this.audioTrack, stream)
      // Qwen 3.8 requires media to stay gated until session.created. Keeping
      // the transceiver while removing its track preserves m=audio in SDP.
      await this.sender.replaceTrack(null)
      this.bindChannel(peer.createDataChannel('oai-events'))
      peer.addEventListener('datachannel', (event) => this.bindChannel(event.channel))
      peer.addEventListener('track', (event) => this.attachRemoteAudio(event))
      peer.addEventListener('connectionstatechange', () => this.handleConnectionState())

      const offer = await peer.createOffer()
      await peer.setLocalDescription(offer)
      await waitForIceGathering(peer)
      const offerSdp = peer.localDescription?.sdp
      if (!offerSdp) throw new Error('未能生成实时语音协商信息。')
      const answer = await negotiate(offerSdp)
      if(this.closed)return
      await peer.setRemoteDescription({ type: 'answer', sdp: normalizeQwenAnswerSdp(answer.answerSdp) })
    } catch (error) {
      this.fail(error)
      if(protectedPlaybackBarrier.held())this.close('protected_tail_setup_failed')
      throw error
    }
  }

  private async connectNativeSocket(): Promise<void> {
    if(this.closed||this.nativeSocketOpen||this.inputDeliveryFailed)return
    let stage: 'prerequisites'|'subscribe'|'playback_barrier'|'microphone'|'echo_policy'|'lease'|'fanout'|'downlink'|'socket_open'|'context' = 'prerequisites'
    const desktop=window.desktop,runtime=this.options.nativeRuntime,scope=this.options.protectedPlaybackControl
    try {
    if(!desktop||!runtime||!scope||typeof desktop.openQwenNativeSocket!=='function'
      ||typeof desktop.sendQwenNativeSocket!=='function'||typeof desktop.closeQwenNativeSocket!=='function'
      ||typeof desktop.onQwenNativeSocketEvent!=='function')throw Error('当前客户端缺少原生云语音连接。')
    if(this.options.nativeAudioAllowed?.()!==true)throw Error('尚未允许云端语音。')
    this.publish('connecting',false,'正在连接自然语音服务')
    stage='subscribe'
    this.nativeSocketUnsubscribe=desktop.onQwenNativeSocketEvent(packet=>{
      if(this.closed)return
      if(!this.nativeHostConnectionId){
        const bytes=packet.data?.length??0
        if(this.nativeEarlyPackets.length>=32||this.nativeEarlyBytes+bytes>1048576){this.fail(new Error('云语音初始化消息超过边界。'));return}
        this.nativeEarlyPackets.push(packet);this.nativeEarlyBytes+=bytes;return
      }
      this.receiveNativePacket(packet)
    })
      stage='playback_barrier'
      const protectedScope=this.options.protectedPlaybackControl
      if(protectedPlaybackBarrier.held()&&(!protectedScope||!protectedPlaybackBarrier.reserveOpen(protectedScope,performance.now())))throw new Error(PLAYBACK_TAIL_FAILURE)
      this.watchProtectedTailDeadline()
      stage='microphone'
      const captureRoute=classifyCaptureRoute(navigator.mediaDevices.getUserMedia)
      const opened = await openQwenRealtimeMicrophone(this.options.audioDeviceId)
      const stream = opened.stream
      if (opened.usedDefaultFallback) this.options.callbacks.onMicrophoneFallback?.(opened.failureName ?? 'Error')
      if (this.closed) { stream.getTracks().forEach((track) => track.stop()); return }
      this.stream = stream
      const track = stream.getAudioTracks()[0] ?? null
      if (!track) throw new Error('未获得可用的麦克风音轨。')
      if (track.readyState === 'ended') throw new Error('麦克风音轨已断开，正在重新连接。')
      this.localSourceTrack = track
      this.metadataObserver?.opened(stream,track,captureRoute)
      track.addEventListener('ended', () => {
        if (this.closed || this.localSourceTrack !== track) return
        this.microphoneEnded = true
        if (this.audioTrack) this.audioTrack.enabled = false
        this.fail(new Error('麦克风音轨已断开，正在重新连接。'))
      }, { once: true })
      stage='echo_policy'
      const echoPolicy = await negotiateMicrophoneEchoCancellation(track)
      if (this.closed || this.microphoneEnded) return
      const cloudTrack = track.clone()
      cloudTrack.enabled = false
      this.audioTrack = cloudTrack
      this.metadataObserver?.cloned(track,cloudTrack)
      cloudTrack.addEventListener('ended', () => {
        if (this.closed || this.audioTrack !== cloudTrack) return
        this.microphoneEnded = true
        cloudTrack.enabled = false
        this.fail(new Error('麦克风传输音轨已断开，正在重新连接。'))
      }, { once: true })
      this.options.callbacks.onEchoCancellationPolicy?.(echoPolicy)
      this.options.callbacks.onDuplexCapability?.(this.options.fullDuplex === true && microphoneEchoAllowsDuplex(cloudTrack))
      stage='lease'
      await this.options.callbacks.onMicrophoneOpen?.(track)
      if (this.closed || this.microphoneEnded) return
      stage='fanout'
      await this.attachOwnedControlFanout(stream,track,cloudTrack)

      stage='downlink'
      const downlink=new QwenNativePcmDownlink({current:()=>this.nativeOwnerCurrent(),
        failed:()=>this.fail(new Error('自然语音输出连接暂时不可用，正在恢复。'))})
      this.nativeDownlink=downlink
      const output=await downlink.start()
      if(!this.nativeOwnerCurrent())return
      const outputTrack=output.getAudioTracks()[0]
      if(!outputTrack)throw Error('自然语音输出音轨缺失。')
      this.attachRemoteStream(outputTrack,output)
      stage='socket_open'
      const result=await desktop.openQwenNativeSocket(scope.sessionId)
      if(this.closed||this.inputDeliveryFailed||!scope.isCurrent()||this.options.nativeAudioAllowed?.()!==true){
        await desktop.closeQwenNativeSocket(result.nativeHostConnectionId);return
      }
      const hostId=result.nativeHostConnectionId
      if(!/^[\x21-\x7e]{1,160}$/.test(hostId))throw Error('云语音连接身份没有取得。')
      this.nativeHostConnectionId=hostId;this.nativeSocketOpen=true;this.nativeVoice=result.voice;this.nativeModel=result.model
      stage='context'
      const [catalog,context]=await Promise.all([runtime.loadCatalog(hostId),runtime.loadContext(hostId)])
      if(!this.nativeOwnerCurrent()||this.nativeHostConnectionId!==hostId)return
      qwenRuntimeTools(catalog);qwenNativeContextInstructions(context);this.nativeCatalog=catalog
      this.nativeContext=await recoverQwenNativeContext(runtime,hostId,context,()=>this.nativeOwnerCurrent()&&this.nativeHostConnectionId===hostId)
      if(!this.nativeOwnerCurrent())return
      this.nativeSocketReady=true
      const early=this.nativeEarlyPackets.splice(0);this.nativeEarlyBytes=0
      for(const packet of early)this.receiveNativePacket(packet)
      this.nativeSessionDeadline=setTimeout(()=>{if(!this.closed&&!this.sessionReady)this.fail(new Error('自然语音服务没有确认会话配置。'))},12000)
    }catch(error){
      const names: Record<string,string> = {NotAllowedError:'permission_denied',SecurityError:'permission_denied',NotFoundError:'device_missing',NotReadableError:'device_busy',OverconstrainedError:'constraints',AbortError:'aborted',InvalidStateError:'invalid_state',TypeError:'type_error'}
      const name = error instanceof Error ? error.name : ''
      const generation = this.options.connectionGeneration
      const playbackTailError = (stage === 'playback_barrier' || stage === 'fanout')
        && error instanceof Error && error.message === PLAYBACK_TAIL_FAILURE ? error : null
      const failure = playbackTailError ? 'playback_tail_unconfirmed' : Object.prototype.hasOwnProperty.call(names,name) ? names[name] : 'other'
      try { void Promise.resolve(window.desktop?.recordRendererDiagnostic?.({event:'voice.realtime.native_connect_failed',level:'warn',details:{native_connect_stage:stage,native_connect_failure:failure,...(Number.isSafeInteger(generation) && generation! > 0 ? {connection_generation:generation} : {})}})).catch(()=>undefined) } catch { /* Diagnostics do not affect recovery. */ }
      if(stage==='prerequisites' && error instanceof Error && ['当前客户端缺少原生云语音连接。','尚未允许云端语音。'].includes(error.message)) throw error
      const safe = playbackTailError ?? nativeSocketFailure(error,'自然语音连接没有完成，正在恢复。')
      this.fail(safe)
      throw safe
    }
  }
  private receiveNativePacket(packet:QwenNativeSocketEvent):void{
    if(this.closed||packet.hostConnectionId!==this.nativeHostConnectionId)return
    if(!this.nativeSocketReady){
      const bytes=packet.data?.length??0
      if(this.nativeEarlyPackets.length>=32||this.nativeEarlyBytes+bytes>1048576){this.fail(new Error('云语音初始化消息超过边界。'));return}
      this.nativeEarlyPackets.push(packet);this.nativeEarlyBytes+=bytes;return
    }
    if(packet.closed||packet.error){this.nativeSocketOpen=false;this.fail(nativeSocketFailure(packet.error,'自然语音连接已中断，原话仍按保存回执恢复。'));return}
    if(typeof packet.data==='string')this.receive(packet.data)
  }

  private async attachOwnedControlFanout(stream:MediaStream,track:MediaStreamTrack,cloudTrack:MediaStreamTrack):Promise<void>{
  const control = this.options.protectedPlaybackControl
    if (control) {
      const capture = () => !this.closed&&!this.microphoneEnded&&this.microphoneReleased&&(this.localWakeRecovery||(this.options.nativeRuntime?this.nativeSocketOpen:this.peer?.connectionState==='connected'))
        &&(this.localWakeRecovery||(this.options.nativeRuntime?!this.nativeAppendAllowed():this.sender?.track===cloudTrack))
        &&this.localSourceTrack===track&&control.isCurrent()
        ? {sessionId:control.sessionId,childId:control.childId,inputKind:'realtime' as const,localTrack:track,cloudTrack} : null
      const currentControl = () => {
        const owned=capture(),lease=control.getLease();return owned&&lease?{...owned,lease}:null
      }
      const hold = (held: boolean): void => {
        const changed=this.protectedControlHold!==held;if(changed){this.nativeActivationEpoch++;this.nativeActivationBuffer?.invalidate()}this.protectedControlHold=held;this.syncMicrophoneGate()
        if(changed&&this.sessionReady)this.publish('listening',false)
      }
      const activation=this.options.nativeActivation
      const wake = (announce=true): void => {
        if(!capture()||!activation)return
        if(this.localWakeRecovery){activation.onRecoveryWake?.();return}
        if(!this.automaticVoiceAllowed())return
        activation.onWake(announce);this.syncConversationActivation()
      }
      const tap = new ProtectedPlaybackControl({ capture, current: currentControl,
        recognize: control.recognize, cancelExact: control.cancelExact,
        attentionWakeWord:()=>this.automaticVoiceAllowed()?activation?.word():undefined,onAttentionWake:activation?wake:undefined,
        holdCloud:hold,failClosed: () => this.failProtectedTail() })
      if(activation){
        this.nativeActivationBuffer=new NativeActivationAudioBuffer({
          current:()=>this.automaticVoiceAllowed()&&this.nativeOwnerCurrent()&&this.options.nativeAudioAllowed?.()===true&&this.nativeSocketOpen&&this.sessionReady
            &&!this.localWakeRecovery&&!this.externalPlaybackActive&&!this.playbackAuthorized&&!this.protectedControlHold&&!protectedPlaybackBarrier.held()
            &&this.localSourceTrack===track&&activation.word()?{sessionId:control.sessionId,childId:control.childId,root:track,word:activation.word()!,epoch:this.nativeActivationEpoch}:null,
          standby:()=>!activation.isActive(),capture:(owner,capturedAt)=>({sessionId:owner.sessionId,reference:{child_id:owner.childId},carryoverConfirmationId:null,localInputId:crypto.randomUUID(),capturedAt,source:'accepted_onset'})})
        this.localAttention=new LocalAttentionControl({capture,current:currentControl,word:()=>this.automaticVoiceAllowed()?activation.word():undefined,
          recognize:control.recognize,cancelExact:control.cancelExact,holdInput:hold,onWake:wake})
        this.standbyWake=new LocalStandbyWakeControl({capture,word:activation.word,
          eligible:()=>(this.localWakeRecovery||this.automaticVoiceAllowed()&&!activation.isActive())&&!this.externalPlaybackActive&&!this.playbackAuthorized
            &&!this.protectedControlHold&&!protectedPlaybackBarrier.held(),
          recognize:activation.recognizeStandby,onWake:wake,
          activationToken:()=>this.nativeActivationBuffer?.token(),onWakeAudio:token=>{
            const buffer=this.nativeActivationBuffer,audio=buffer?.take(token)
            if(!audio||audio.overflow){if(audio)for(const frame of audio.frames)frame.fill(0);wake();this.options.callbacks.onError?.('唤醒前这句话没有保留完整，请再说一次。');return}
            wake(false)
            if(!this.manualAudio?.acceptActivationAudio(audio,()=>buffer?.owns(audio)===true)){
              for(const frame of audio.frames)frame.fill(0);this.options.callbacks.onError?.('这句话没有取得完整采集来源，请再说一次。')
            }
          },
          onPrivacyStop:()=>{activation.onPrivacyStop?.();this.close('local_privacy_stop')},
          onUnavailable:reason=>this.fail(new Error(reason==='timeout'
            ? 'ASR_CLIENT_TIMEOUT：本地唤醒识别未按时返回，正在恢复；刚才的呼叫尚未确认。'
            : 'ASR_CLIENT_UNKNOWN：本地唤醒识别暂时不可用，正在恢复；刚才的呼叫尚未确认。'))})
      }
      this.protectedControl = tap
      try { await tap.attach(stream,(frame,rate)=>{
        this.nativeLastFrameAt=performance.now();this.nativeFrameCount++
        this.rememberPrivateSapiLease()
        this.advanceExplicitCapture()
        this.nativeActivationBuffer?.processFrame(frame,rate)
        this.localAttention?.processFrame(frame,rate);this.standbyWake?.processFrame(frame,rate)
        if(this.explicitCapture==='ready')this.manualAudio?.processFrame(frame,rate,'explicit')
        else if(this.automaticVoiceAllowed())this.manualAudio?.processFrame(frame,rate)
        if(this.nativeTools.size&&this.nativeResponseAllowed())for(const response of [...this.nativeTools.keys()])this.flushNativeTools(response)
        if(this.options.householdEntry&&!this.householdEntryConsumed)void this.maybeStartHouseholdInput()
        else if(this.explicitCapture==='idle'&&!this.explicitReplyPending)void this.maybeRestorePersistedInput()
      }) } catch {
        tap.dispose();this.protectedControl = null
        if(protectedPlaybackBarrier.held())throw new Error(PLAYBACK_TAIL_FAILURE)
      }
      if (this.closed || this.microphoneEnded) return
    }
  }

  private rememberPrivateSapiLease(): void {
    if(!this.externalPlaybackActive||!this.externalPlaybackLocalOnly)return
    const scope=this.options.protectedPlaybackControl,lease=scope?.getLease(),root=this.localSourceTrack,cloud=this.audioTrack
    if(!scope||!scope.isCurrent()||!root||!cloud||root.readyState!=='live'||!root.enabled
      ||lease?.kind!=='local_sapi'||lease.sessionId!==scope.sessionId)return
    this.privateSapiTail={sessionId:scope.sessionId,childId:scope.childId,root,cloud,id:lease.id,generation:lease.generation}
  }
  private beginPrivateSapiTail(): void {
    const saved=this.privateSapiTail;this.privateSapiTail=null
    const scope=this.options.protectedPlaybackControl
    if(!saved||this.closed||!scope?.isCurrent()||scope.sessionId!==saved.sessionId||scope.childId!==saved.childId
      ||this.localSourceTrack!==saved.root||this.audioTrack!==saved.cloud||saved.root.readyState!=='live'||!saved.root.enabled)return
    if(!protectedPlaybackBarrier.held())protectedPlaybackBarrier.begin(saved,saved.id,saved.generation,performance.now())
    if(protectedPlaybackBarrier.held()){this.protectedControlHold=true;this.watchProtectedTailDeadline()}
  }
  private stopNativeActivityWatch(): void {
    if(this.nativeActivityTimer!==null)clearTimeout(this.nativeActivityTimer)
    if(this.nativeQuietTimer!==null)clearTimeout(this.nativeQuietTimer)
    this.nativeActivityTimer=null;this.nativeQuietTimer=null
    this.remotePlaybackObservation?.disarm()
  }
  private scheduleNativeQuiet(responseId:string):void{
    if(this.nativeQuietTimer!==null)clearTimeout(this.nativeQuietTimer)
    this.nativeQuietTimer=setTimeout(()=>{
      this.nativeQuietTimer=null
      if(this.closed||this.activeResponseId!==responseId||!this.playbackAuthorized||this.responseActive)return
      // Quiet is an input/UI observation only. It never mutes buffered RTP,
      // acknowledges teaching delivery, scores an answer or proves mastery.
      if(!this.nativeCanDuplex()&&(!this.remotePlaybackObservation||this.nativePlaybackObserverUnavailable||this.nativeAudioDeltaSeen&&!this.playbackStarted)){
        this.fail(new Error('保守收音路径无法确认播放观测健康，正在恢复连接。'));return
      }
      this.nativeRemoteMediaActive=false;this.syncMicrophoneGate();this.publish('listening',false)
    },1200)
  }
  private watchNativeActivity():void{
    const responseId=this.activeResponseId,observer=this.remotePlaybackObservation
    if(!responseId||!observer)return
    const owns=()=>!this.closed&&this.activeResponseId===responseId&&this.playbackAuthorized
      &&this.options.nativeAudioAllowed?.()===true&&!!this.nativeConversation?.ownsResponse(responseId)
    const observed=():void=>{
      if(!owns())return
      this.nativeRemoteMediaActive=true;this.syncMicrophoneGate();this.publish('speaking',false)
      this.scheduleNativeQuiet(responseId)
      this.nativeActivityTimer=setTimeout(()=>{
        this.nativeActivityTimer=null
        if(owns())observer.arm(observed,()=>{if(owns())this.fail(new Error('实时语音播放观测暂时不可用，正在恢复连接。'))})
      },200)
    }
    observed()
  }
  private nativeCanDuplex():boolean{return this.options.fullDuplex===true&&microphoneEchoAllowsDuplex(this.localSourceTrack)}
  private nativeOwnerCurrent(): boolean {
    return !this.closed&&!this.failureReported&&!this.inputDeliveryFailed&&!this.microphoneEnded
      &&this.localSourceTrack?.readyState==='live'&&this.localSourceTrack.enabled===true
      &&(!this.options.protectedPlaybackControl||this.options.protectedPlaybackControl.isCurrent())
  }
  private nativeResponseAllowed(): boolean {
    return !!this.options.nativeRuntime&&!this.localWakeRecovery&&this.options.nativeAudioAllowed?.()===true&&this.nativeOwnerCurrent()&&this.microphoneReleased&&this.sessionReady
      &&this.nativeSocketOpen&&this.nativeSocketReady&&!!this.nativeHostConnectionId
      &&(!this.options.nativeActivation||this.options.nativeActivation.isActive())
      &&!this.protectedControlHold&&!protectedPlaybackBarrier.held()
      &&(!this.externalPlaybackActive||!this.externalPlaybackLocalOnly
        &&this.options.fullDuplex===true&&microphoneEchoAllowsDuplex(this.localSourceTrack,true))
  }
  private nativeAppendAllowed(): boolean {
    return (!this.options.householdEntry||this.householdEntryConsumed)&&this.nativeResponseAllowed()&&(!this.playbackAuthorized||!this.nativeRemoteMediaActive
      ||this.options.fullDuplex===true&&microphoneEchoAllowsDuplex(this.localSourceTrack))
  }
  private automaticVoiceAllowed(): boolean {
    return this.voiceInputMode==='hands-free' && this.explicitCapture==='idle' && !this.explicitReplyPending
      && performance.now()>=this.explicitQuietUntil
  }

  setVoiceInputMode(mode: VoiceInputMode): void {
    if (this.voiceInputMode===mode) return
    this.voiceInputMode=mode
    this.cancelExplicitVoiceTurn()
    this.manualAudio?.discardAutomaticCapture()
    this.nativeActivationBuffer?.invalidate()
  }

  private reportExplicitCapture(state: 'idle'|'waiting'|'ready', detail?: string): void {
    this.explicitCapture=state
    this.options.callbacks.onExplicitVoiceCapture?.(state,detail)
  }

  beginExplicitVoiceTurn(): boolean {
    if (!this.manualAudio || this.explicitCapture!=='idle' || !this.nativeAudioSessionCurrent()) return false
    const playingRemote=this.playbackAuthorized && this.nativeRemoteMediaActive && this.nativeAudioDeltaSeen
    this.reportExplicitCapture('waiting')
    this.manualAudio.discardAutomaticCapture()
    this.nativeActivationBuffer?.invalidate()
    this.options.nativeActivation?.onExplicit?.()
    if (playingRemote) {
      this.explicitQuietUntil=performance.now()+240
      this.manualAudio.interruptPlayingReply()
    }
    this.options.callbacks.onExplicitVoiceInterrupt?.()
    this.advanceExplicitCapture()
    return true
  }

  finishExplicitVoiceTurn(): void {
    if (this.explicitCapture==='idle') return
    if (this.explicitCapture==='waiting') {
      this.reportExplicitCapture('idle','麦克风还没准备好，请再说一次。')
      return
    }
    const submitted=this.manualAudio?.finishExplicitTurn()===true
    this.explicitReplyPending ||= submitted
    this.reportExplicitCapture('idle',submitted?undefined:'没有收到清楚的语音，请再说一次。')
  }

  cancelExplicitVoiceTurn(): void {
    if (this.explicitCapture==='idle') return
    this.manualAudio?.cancelExplicitTurn()
    this.reportExplicitCapture('idle')
  }

  private advanceExplicitCapture(): void {
    if (this.explicitCapture==='waiting' && this.nativeAppendAllowed() && !this.externalPlaybackActive
      && (this.nativeCanDuplex() || !this.manualAudio?.hasPendingWork() && !this.responseActive && !this.nativeRemoteMediaActive)
      && performance.now()>=this.explicitQuietUntil && this.manualAudio?.beginExplicitTurn()) {
      this.reportExplicitCapture('ready')
    }
    if (this.explicitReplyPending && !this.manualAudio?.hasPendingWork() && !this.responseActive && !this.nativeRemoteMediaActive
      && this.nativeQuietTimer===null && !this.nativeTools.size && !this.externalPlaybackActive && !this.protectedControlHold
      && !protectedPlaybackBarrier.held() && !this.options.explicitReplyBusy?.()) this.explicitReplyPending=false
  }
  private nativeAudioSessionCurrent(): boolean {
    return this.nativeOwnerCurrent()
      && this.options.nativeAudioAllowed?.() === true && this.nativeSocketOpen && this.nativeSocketReady
      && !!this.nativeHostConnectionId && this.sessionReady && this.microphoneReleased && !this.localWakeRecovery
  }
  private nativeKeepaliveCurrent(): boolean {
    return this.nativeModel === 'qwen3.8-omni-flash-realtime' && this.nativeAudioSessionCurrent()
  }
  private nativeKeepaliveIdle(): boolean {
    return this.nativeKeepaliveCurrent() && this.nativeKeepaliveSentAt === null
      && this.explicitCapture==='idle'
      && this.manualAudio?.hasPendingWork() === false && !this.nativeTools.size && !this.responseActive
      && !this.nativeRemoteMediaActive && this.nativeQuietTimer === null && !this.externalPlaybackActive
      && !this.protectedControlHold && !protectedPlaybackBarrier.held()
      && !this.householdEntryLoading && !this.householdPending && !this.nativeRestoreLoading && !this.nativeRestore
  }
  private scheduleNativeKeepalive(waitMs = NATIVE_KEEPALIVE_INTERVAL_MS): void {
    if (!this.nativeKeepaliveCurrent() || this.nativeKeepaliveTimer !== null || this.nativeKeepaliveSentAt !== null) return
    this.nativeKeepaliveTimer = setTimeout(() => {
      this.nativeKeepaliveTimer = null
      if (!this.nativeKeepaliveIdle() || this.nativePendingSendBytes > 0) {
        this.scheduleNativeKeepalive(NATIVE_KEEPALIVE_RECHECK_MS)
        return
      }
      // A duplicate manual configuration ACK is validated exactly as on startup.
      // Do not send PCM, context, response requests or changes to turn detection.
      try {
        void this.send({ type: 'session.update', session: {} }, true)?.then(sent => {
          // Busy before dispatch can be rechecked. Once sent, wait for its ACK.
          if (!sent && this.nativeKeepaliveSentAt === null) this.scheduleNativeKeepalive(NATIVE_KEEPALIVE_RECHECK_MS)
        })
      } catch { /* Missing control transport does not fail a live input turn. */ }
    }, waitMs)
  }
  private stopNativeKeepalive(): void {
    if (this.nativeKeepaliveTimer !== null) clearTimeout(this.nativeKeepaliveTimer)
    this.nativeKeepaliveTimer = null
    this.nativeKeepaliveSentAt = null
  }
  private prepareNativeSession(supplierSessionId: unknown): void {
    const runtime=this.options.nativeRuntime,scope=this.options.protectedPlaybackControl
    if(!runtime||!this.nativeCatalog||!this.nativeContext||!this.nativeHostConnectionId
      ||typeof supplierSessionId!=='string'||!supplierSessionId.length||supplierSessionId.length>160
      ||!/^[\x21-\x7e]+$/.test(supplierSessionId))throw Error('云语音会话身份未能核对。')
    this.nativeSupplierSessionId=supplierSessionId
    this.nativeConversation=new QwenNativeConversation(supplierSessionId,{...runtime,catalog:this.nativeCatalog},capture=>
      this.nativeOwnerCurrent()&&capture.source==='accepted_onset'
        &&(!scope||capture.sessionId===scope.sessionId&&capture.reference.child_id===scope.childId),this.nativeHostConnectionId)
    this.manualAudio=new QwenManualAudio({canAppend:()=>this.nativeAppendAllowed(),canRespond:()=>this.nativeResponseAllowed(),current:()=>this.nativeOwnerCurrent(),
      capture:()=>this.captureInputSource('accepted_onset'),ledger:()=>this.nativeConversation,
      explicitCaptureLimit:()=>this.reportExplicitCapture('idle','录音已到时限，未发送。请分段再说。'),
      explicitCaptureDiscarded:()=>this.reportExplicitCapture('idle','录音已中断，未发送。请再说一次。'),
      send:event=>this.send(event),speechStarted:()=>{
        this.nativeLiveInputObserved=true;this.clearNativeRestore()
        if(this.activeResponseId)this.nativeResponseDiagnostics.inputStarted(this.activeResponseId)
      },replyAudioScheduled:()=>this.playbackAuthorized&&this.nativeRemoteMediaActive&&this.nativeAudioDeltaSeen
        ||this.externalPlaybackActive&&!this.externalPlaybackLocalOnly,
      interruptReply:reason=>{
        const previous=this.activeResponseId
        if(previous){void this.nativeConversation?.finishDialogue(previous,'interrupted').catch(()=>this.options.callbacks.onError?.('刚才的对话记录尚未确认保存。'));this.nativeConversation?.retireResponse(previous)}
        this.stopNativeActivityWatch()
        this.replyGeneration++;this.cancelActiveInference(reason);this.setRemotePlaybackAuthorized(false)
        this.assistantTranscript='';this.options.callbacks.onUserSpeechStart?.()
      },inputCompleted:(text,capture)=>{
        this.options.callbacks.onInputCompleted?.(text,capture);this.options.callbacks.onUserTranscript?.(text)
      },onWorkspaceInput:(text,capture,voiceId)=>{
        return this.options.callbacks.onWorkspaceInput?.(text,capture,voiceId) === true
      },currentDiagnosticAnswerTool:capture=>{
        const { sessionId, reference } = capture
        if (reference.activity_reference || realtimeInputSourceStatus(capture, sessionId, reference.child_id) !== 'current') return undefined
        return this.nativeCatalog?.capabilities.find(item=>item.name==='diagnostic.submit')?.tool_name
      },onCurriculumTutor:(text,capture,dispatch)=>{
        const generation = this.replyGeneration
        const current = () => generation === this.replyGeneration && this.nativeOwnerCurrent()
          && this.options.nativeAudioAllowed?.() === true && (this.options.nativeActivation?.isActive() ?? true)
        return current() && this.options.callbacks.onCurriculumTutor?.(text,capture,dispatch,current) === true
      },responseBound:(_id,allowed)=>{
        if(!allowed){this.cancelActiveInference('response_binding');this.setRemotePlaybackAuthorized(false);return}
        this.setRemotePlaybackAuthorized(true)
      },status:(phase,speech)=>{
        const playing = this.playbackStarted && this.nativeRemoteMediaActive
        this.publish(phase === 'listening' && !speech && playing ? 'speaking' : phase, speech)
      },
      inputDiscarded:()=>this.options.callbacks.onError?.('播放保护期间的未确认语音没有补发，请在安静后重说。'),
      localSafety:control=>{
        const retained=this.enterLocalWakeRecovery()
        if(!retained)this.close('child_safety_recovery')
        this.options.callbacks.onLocalSafety?.(control,retained)
      },failed:message=>this.fail(new Error(message))})
  }
  private receiveNative(event: RealtimeServerEvent,responseId: string|undefined): void {
    switch(event.type){
      case 'session.created':
        if(this.sessionCreated){this.fail(new Error('云语音会话身份发生变化，正在重新连接。'));return}
        this.sessionCreated=true
        try{this.prepareNativeSession(event.session?.id);void this.beginConfiguredSession()}catch(error){this.fail(error)}
        return
      case 'session.updated':
        if(!this.sessionUpdateSent||event.session?.id!==this.nativeSupplierSessionId
          ||event.session.turn_detection!==undefined&&event.session.turn_detection!==null
          ||event.session.instructions!==this.nativeExpectedInstructions||event.session.model!==this.nativeModel
          ||!Array.isArray(event.session.tools)||event.session.tools.length!==this.nativeCatalog?.capabilities.length
          ||this.nativeCatalog.capabilities.some(cap=>!event.session!.tools!.some(tool=>tool?.function?.name===cap.tool_name))){
          this.fail(new Error('云语音没有确认手动回合模式，未开放音频上传。'));return
        }
        const acknowledged = nativeSessionAckConfiguration(event.session,this.nativeSessionAckFields)
        if(acknowledged===null||(this.sessionReady&&acknowledged!==this.nativeSessionAckConfiguration)){
          this.fail(new Error('云语音没有确认手动回合模式，未开放音频上传。'));return
        }
        // Repeated identical ACKs are idempotent, not a second microphone release.
        if(this.sessionReady){
          const sentAt = this.nativeKeepaliveSentAt
          if (sentAt !== null && this.nativeKeepaliveCurrent()) {
            const now = performance.now()
            this.nativeKeepaliveSentAt = null
            this.recordNativeToolDiagnostic({ event: 'voice.realtime.keepalive_acknowledged', level: 'info',
              details: { runtime: 'qwen_omni_realtime', elapsed_ms: Math.max(0, Math.round(now - sentAt)) } })
            this.scheduleNativeKeepalive()
          }
          return
        }
        this.nativeSessionAckConfiguration=acknowledged
        if(this.nativeSessionDeadline!==null)clearTimeout(this.nativeSessionDeadline);this.nativeSessionDeadline=null
        this.sessionReady=true;void this.releaseMicrophoneGate()
        this.scheduleNativeKeepalive()
        return
      case 'conversation.item.created':this.acceptRestoredInput(event.item);return
      case 'input_audio_buffer.committed':
        if(event.item_id&&!this.manualAudio?.inputCommitted(event.item_id))this.recordInputProgress('input_not_admitted')
        return
      case 'conversation.item.input_audio_transcription.completed':{
        this.recordInputProgress('provider_completed')
        const text=eventText(event),word=this.options.nativeActivation?.word()
        const wake=word?registeredWakePrefix(text,word):null
        if(this.manualAudio?.ownsCommittedInput(event.item_id)&&(isImmediatePrivacyStop(text)||isExactStandbyPrivacyStop(text)||wake?.matched&&isExactStandbyPrivacyStop(wake.command))){
          this.options.nativeActivation?.onPrivacyStop?.();this.close('native_privacy_stop');return
        }
        if(event.item_id){this.recordInputProgress('callback_started');void this.manualAudio?.inputCompleted(event.item_id,eventText(event))
          .then(accepted=>this.recordInputProgress(accepted?'callback_returned':'input_not_admitted'))
          .catch(()=>this.recordInputProgress('callback_failed'))}
        return
      }
      case 'conversation.item.input_audio_transcription.failed':
        this.fail(new Error('这句话的云端转写未完成，没有执行请求；恢复后请重说。'));return
      case 'response.created':
        if(!responseId||this.manualAudio?.hasSeenResponse(responseId))return
        this.nativeVendorGeneratingResponseId=responseId
        this.stopNativeActivityWatch();this.playbackStarted=false
        this.activeResponseId=responseId;this.responseActive=true;this.assistantTranscript=''
        this.responseContainsToolCall=false
        if(!this.manualAudio?.responseCreated(responseId))this.setRemotePlaybackAuthorized(false)
        else{const continuation=this.manualAudio?.isContinuation(responseId)===true;if(!continuation)this.nativeAudioDeltaSeen=false;this.nativeDownlink?.begin(responseId,continuation);this.publish('transcribing',false)}
        return
      case 'response.function_call_arguments.done':
        if(responseId)void this.runNativeTool(responseId,event)
        return
      case 'response.audio.delta':
        if(responseId&&this.playbackAuthorized&&this.nativeResponseAllowed()&&this.nativeConversation?.ownsResponse(responseId)&&typeof event.delta==='string'&&this.nativeDownlink?.append(responseId,event.delta)){this.nativeAudioDeltaSeen=true;this.nativeResponseDiagnostics.scheduledPcm(responseId)}
        return
      case 'response.audio_transcript.delta':
      case 'response.text.delta':
        if(!responseId||!this.playbackAuthorized||!this.nativeConversation?.hasResponse(responseId)||typeof event.delta!=='string')return
        if(!this.nativeConversation.appendDialogue(responseId,event.delta)){this.fail(new Error('云端对话长度超过本轮记录边界。'));return}
        this.assistantTranscript+=event.delta;this.options.callbacks.onAssistantTranscript?.(this.assistantTranscript);return
      case 'response.audio_transcript.done':
      case 'response.text.done':
        if(!responseId||!this.playbackAuthorized||!this.nativeConversation?.hasResponse(responseId))return
        if(eventText(event)){this.assistantTranscript=eventText(event);this.nativeConversation.replaceDialogue(responseId,this.assistantTranscript)
          this.options.callbacks.onAssistantTranscript?.(this.assistantTranscript)}
        return
      case 'response.audio.done':return // Generation EOS is not physical playback completion.
      case 'response.done':{
        if(!responseId)return
        this.responseActive=false
        const terminal=event.response?.status==='completed'?'completed':event.response?.status==='cancelled'?'interrupted':'failed'
        void this.nativeConversation?.finishDialogue(responseId,terminal).catch(()=>this.options.callbacks.onError?.('这轮自然对话尚未确认保存，不能当作学习记录。'))
        const group=this.nativeTools.get(responseId)
        if(group){group.done=true;this.flushNativeTools(responseId)}
        if(terminal!=='completed'){this.setRemotePlaybackAuthorized(false);this.publish('listening',false);return}
        // Generation EOS is not rendered EOS. Keep this owned remote stream
        // authorized; interruption, SAPI, standby, revocation or retirement mute it.
        if(this.nativeQuietTimer===null)this.scheduleNativeQuiet(responseId)
        return
      }
      case 'error':this.fail(new Error('千问实时语音返回错误，正在恢复连接。'));return
      default:return
    }
  }
  private clearNativeRestore():void{
    if(this.nativeRestore)clearTimeout(this.nativeRestore.timer);this.nativeRestore=null
    if(this.householdPending)clearTimeout(this.householdPending.timer);this.householdPending=null
  }
  private async maybeStartHouseholdInput():Promise<void>{
    const entry=this.options.householdEntry,runtime=this.options.nativeRuntime,host=this.nativeHostConnectionId
    if(!entry||this.householdEntryConsumed||this.householdEntryLoading||this.householdPending||!runtime?.admitHousehold||!host
      ||!this.sessionReady||!this.nativeSocketOpen||!this.nativeOwnerCurrent()||this.options.nativeAudioAllowed?.()!==true
      ||!this.options.nativeActivation?.isActive()||this.externalPlaybackActive||protectedPlaybackBarrier.held()||this.responseActive||this.manualAudio?.pendingCount())return
    this.householdEntryLoading=true
    try{
      const admitted=await runtime.admitHousehold(entry,host)
      if(this.closed||this.nativeHostConnectionId!==host||!this.nativeOwnerCurrent()||this.options.nativeAudioAllowed?.()!==true)return
      if(admitted.local_control){this.householdEntryConsumed=true;this.options.onHouseholdEntryConsumed?.(false);const retained=this.enterLocalWakeRecovery();if(!retained)this.close('child_safety_recovery');this.options.callbacks.onLocalSafety?.(admitted.local_control,retained);return}
      if(admitted.entry_state!=='unprocessed'||this.nativeContext?.pending_inputs?.some(input=>input.voice_input_id===admitted.voice_input_id)){
        this.householdEntryConsumed=true;this.options.onHouseholdEntryConsumed?.(admitted.entry_state==='unprocessed');return
      }
      const timer=setTimeout(()=>{if(this.householdPending?.entry===entry){this.householdPending=null;this.fail(Error('首次语音原话的供应商身份尚未确认，没有重复操作。'))}},12000)
      this.householdPending={entry,voiceInputId:admitted.voice_input_id,timer}
      this.send({type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:entry.transcript}]}})
    }catch(cause){this.fail(cause)}finally{this.householdEntryLoading=false}
  }
  private async maybeRestorePersistedInput():Promise<void>{
    const activation=this.options.nativeActivation,runtime=this.options.nativeRuntime,hostId=this.nativeHostConnectionId
    if(!activation?.canRecoverPersisted?.()||!runtime||!hostId||!this.nativeContext?.pending_inputs?.length
      ||this.nativeRestoreTried||this.nativeRestoreLoading||this.nativeLiveInputObserved||!this.nativeAppendAllowed()
      ||this.responseActive||this.nativeTools.size||this.manualAudio?.pendingCount())return
    this.nativeRestoreLoading=true
    try{
      const fresh=await runtime.loadContext(hostId)
      if(this.closed||hostId!==this.nativeHostConnectionId||this.nativeLiveInputObserved||!this.nativeAppendAllowed()
        ||this.manualAudio?.pendingCount()||this.responseActive||!activation.canRecoverPersisted?.())return
      qwenNativeContextInstructions(fresh)
      const original=fresh.pending_inputs?.[0]
      if(!original)return
      this.nativeRestoreTried=true
      const timer=setTimeout(()=>{if(this.nativeRestore?.original===original){this.clearNativeRestore();this.fail(new Error('已保存原话的恢复身份尚未确认，没有重新执行。'))}},12000)
      this.nativeRestore={original,timer}
      this.send({type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:original.text}]}})
    }catch{this.options.callbacks.onError?.('上次保存的原话暂未取得恢复确认。')}
    finally{this.nativeRestoreLoading=false}
  }
  private acceptRestoredInput(item:RealtimeServerEvent['item']):void{
    const entry=this.householdPending,host=this.nativeHostConnectionId
    if(entry&&item?.type==='message'&&item.role==='user'&&Array.isArray(item.content)&&item.content.length===1
      &&item.content[0]?.type==='input_text'&&item.content[0].text===entry.entry.transcript&&validResponseId(item.id)){
      clearTimeout(entry.timer);this.householdPending=null
      const current=()=>!this.closed&&this.nativeHostConnectionId===host&&this.nativeOwnerCurrent()&&this.options.nativeAudioAllowed?.()===true&&this.options.nativeActivation?.isActive()===true
      if(!current()||this.responseActive||this.manualAudio?.pendingCount())return
      if(this.nativeConversation?.startHouseholdInput(item.id,entry.voiceInputId,entry.entry.transcript,current,entry.entry.entry_receipt.issued_at_ms)){
        this.householdEntryConsumed=true;this.options.onHouseholdEntryConsumed?.(true);this.manualAudio?.requestRestoredResponse(item.id)
      }else this.fail(Error('首次原话的持久来源尚未确认，没有执行操作。'))
      return
    }
    const pending=this.nativeRestore,hostId=this.nativeHostConnectionId
    if(!pending||!item||item.type!=='message'||item.role!=='user'||!Array.isArray(item.content)||item.content.length!==1
      ||item.content[0]?.type!=='input_text'||item.content[0].text!==pending.original.text||!validResponseId(item.id))return
    this.clearNativeRestore()
    const current=()=>!this.closed&&this.nativeHostConnectionId===hostId&&this.nativeOwnerCurrent()
      &&this.options.nativeAudioAllowed?.()===true&&this.options.nativeActivation?.isActive()===true
    if(this.nativeLiveInputObserved||!this.nativeAppendAllowed()||this.manualAudio?.pendingCount()||!current())return
    if(this.nativeConversation?.restoreInput(item.id,pending.original,current))this.manualAudio?.requestRestoredResponse(item.id)
  }
  private async runNativeTool(responseId:string,event:RealtimeServerEvent):Promise<void>{
    const ledger=this.nativeConversation
    if(!ledger?.hasResponse(responseId)||!event.name||!event.call_id)return
    const callId=event.call_id,rawArguments=typeof event.arguments==='string'?event.arguments:'' 
    if(this.seenToolCallIds.has(callId))return
    if(this.seenToolCallIds.size>=1024){this.fail(new Error('本轮工具身份容量已满，正在恢复连接。'));return}
    this.seenToolCallIds.add(callId)
    const group=this.nativeTools.get(responseId)??{done:false,calls:[]}
    this.nativeTools.set(responseId,group)
    const call:{id:string;result?:QwenNativeToolReceipt}={id:callId};group.calls.push(call)
    this.toolRunning=true;this.publish('tool_running',false)
    this.recordNativeToolDiagnostic({event:'voice.realtime.turn_dispatched',level:'info',
      details:{runtime:'qwen_omni_realtime',route:'realtime_tool',input_stage:'dispatch',status:'pending'}})
    let result=await ledger.propose(responseId,callId,event.name,rawArguments)
    if(['pending','unknown'].includes(result.execution.status)&&ledger.ownsResponse(responseId)){
      result=await ledger.propose(responseId,callId,event.name,rawArguments) // Same identity: receipt only.
    }
    const capability=this.nativeCatalog?.capabilities.find(item=>item.tool_name===event.name)?.name
    const denied=result.not_admitted===true||result.proposal_status==='conflict'||result.proposal_status==='rejected'
    this.recordNativeToolDiagnostic({event:'voice.realtime.native_operation',level:'info',details:{
      runtime:'qwen_omni_realtime',native_operation:'proposal',...(capability?{native_capability:capability}:{}),
      status:result.execution.status,input_stage:denied?'input_not_admitted':'dispatch',chars:rawArguments.length,
      ...(result.public_result.status==='tool_arguments_invalid'?{error_code:'INPUT_INVALID'}:{})
    }})
    call.result=result;this.flushNativeTools(responseId)
  }
  private recordNativeToolDiagnostic(event:{event:string;level:'info'|'warn';details:Record<string,unknown>}):void{
    // Diagnostics are finite metadata only and cannot change Runtime execution.
    try{void Promise.resolve(window.desktop?.recordRendererDiagnostic?.(event)).catch(()=>undefined)}catch{/* Keep the actual tool outcome. */}
  }
  private flushNativeTools(responseId:string):void{
    const group=this.nativeTools.get(responseId)
    if(!group||!group.done||group.calls.some(call=>!call.result))return
    if(this.closed||!this.nativeConversation?.ownsResponse(responseId)){this.nativeTools.delete(responseId);this.toolRunning=this.nativeTools.size>0;return}
    // The finite public output is retained, never dropped or released with a
    // stale owner while protected local playback or its actual quiet tail holds.
    if(!this.nativeResponseAllowed()||group.sending)return
    group.sending=true
    void (async()=>{
      for(const call of group.calls){
        if(this.nativeTools.get(responseId)!==group||!this.nativeConversation?.ownsResponse(responseId)||!this.nativeResponseAllowed())return
        if(!call.sent){
          const sent=await this.send({type:'conversation.item.create',item:{type:'function_call_output',call_id:call.id,output:qwenNativePublicToolOutput(call.result!)}})
          if(sent!==true)return
          call.sent=true
        }
      }
      if(this.nativeTools.get(responseId)!==group||!this.nativeConversation?.ownsResponse(responseId)||!this.nativeResponseAllowed())return
      this.nativeTools.delete(responseId);this.toolRunning=this.nativeTools.size>0
      if(group.calls.some(call=>call.result&&qwenNativeFormatCorrectionExhausted(call.result))){
        this.publish('listening',false,'这项操作的参数仍未完整生成，原请求已保留，没有重复执行。')
        return
      }
      this.manualAudio?.requestContinuation(responseId)
    })().catch(()=>this.fail(new Error('工具结果已保留，实时语音交接尚未确认，没有重新执行。'))).finally(()=>{group.sending=false})
  }

  private captureMetadataState(): CaptureState {
    const root=this.localSourceTrack,cloud=this.audioTrack,stream=this.stream,now=performance.now()
    const captureCurrent=!this.closed&&!this.failureReported&&!this.inputDeliveryFailed&&!this.microphoneEnded&&this.microphoneReleased
      &&!!root&&!!cloud&&!!stream&&this.sender?.track===cloud&&this.peer?.connectionState==='connected'
      &&(!this.options.protectedPlaybackControl||this.options.protectedPlaybackControl.isCurrent())
    return {stream,root,cloud,peer:this.peer,sender:this.sender,captureCurrent,epoch:this.audioHealthEpoch,
      phase:!captureCurrent?'blocked':cloud?.enabled?'listening':this.protectedControlHold||protectedPlaybackBarrier.held()||this.externalPlaybackActive||this.playbackAuthorized?'protected':'blocked',
      frame:stream&&root?this.protectedControl?.readCaptureFrameMetadata(stream,root,now)??null:null,documentTimeOrigin:performance.timeOrigin,now}
  }
  async readCaptureMetadata(): Promise<Record<string,unknown>> {
    return this.metadataObserver?.read(()=>this.captureMetadataState())??{schema:'current_app_mic_rtc_metadata_v1',status:'unavailable',reason:'APP_METADATA_BINDING_ABSENT',native_origin_verified:false,joint8h_admitted:false}
  }

  interrupt(): void {
    if (this.closed || this.inputDeliveryFailed) return
    this.replyGeneration += 1
    this.cancelActiveInference()
    this.settlePlayback(false)
    this.playbackStarted = false
    this.setRemotePlaybackAuthorized(false)
    this.assistantTranscript = ''
    if (this.remoteAudio) {
      this.remoteAudio.pause()
      void this.remoteAudio.play().catch(() => undefined)
    }
    if (this.sessionReady) this.publish('listening', false, '已停止伙伴播报')
  }

  /** A failed cloud transport may keep only its already owned local wake/stop tap. */
  enterLocalWakeRecovery(): boolean {
    const scope=this.options.protectedPlaybackControl,word=this.options.nativeActivation?.word()
    if(!this.options.nativeRuntime||this.options.nativeAudioAllowed?.()!==true||this.closed||!scope?.isCurrent()
      ||!word||!this.protectedControl||!this.standbyWake||this.localSourceTrack?.readyState!=='live'||!this.localSourceTrack.enabled
      ||this.audioTrack?.readyState!=='live')return false
    this.retireNativeSocket()
    this.localWakeRecovery=true;this.microphoneEnded=false;this.microphoneReleased=true
    this.inputDeliveryFailed=true;this.failureReported=true;this.audioTrack.enabled=false
    this.manualAudio?.dispose();this.manualAudio=null;this.nativeConversation?.close();this.nativeConversation=null
    this.nativeTools.clear();this.toolRunning=false;this.setRemotePlaybackAuthorized(false);this.stopAudioHealthPolling()
    const hostId=this.nativeHostConnectionId;this.nativeHostConnectionId=null
    if(hostId)this.closeNativeSocketHost(hostId)
    this.channel?.close();this.peer?.close();this.channel=null;this.peer=null;this.sender=null
    if(!this.standbyWake.resumeAfterTransportRetired()){this.localWakeRecovery=false;return false}
    return true
  }

  syncNativeAuthority(): void {
    if(this.options.nativeRuntime&&this.options.nativeAudioAllowed?.()!==true){
      this.manualAudio?.gateChanged();this.cancelActiveInference();this.setRemotePlaybackAuthorized(false)
      this.close('native_audio_permission_changed')
    }
  }

  syncConversationActivation(): void {
    if(this.options.nativeActivation&&!this.options.nativeActivation.isActive()){
      this.manualAudio?.retireResponseEligibility();this.clearNativeRestore()
      if(this.activeResponseId)this.nativeConversation?.retireResponse(this.activeResponseId)
      if(this.options.nativeRuntime)this.cancelActiveInference();else this.cancelAssistantResponse();this.setRemotePlaybackAuthorized(false)
    }
    this.syncMicrophoneGate()
  }

  setExternalPlaybackActive(active: boolean, localOnly = true): void {
    if(active!==this.externalPlaybackActive||localOnly!==this.externalPlaybackLocalOnly){this.nativeActivationEpoch++;this.nativeActivationBuffer?.invalidate()}
    if(!active&&this.externalPlaybackActive&&this.externalPlaybackLocalOnly)this.beginPrivateSapiTail()
    this.externalPlaybackActive = active
    this.externalPlaybackLocalOnly = localOnly
    if(active&&localOnly){this.rememberPrivateSapiLease();if(this.options.nativeRuntime)this.setRemotePlaybackAuthorized(false)}
    this.syncMicrophoneGate()
  }

  /** Maintenance may wait on anonymous local activity; it cannot admit a turn. */
  async readPlannedRefreshState(): Promise<{ current: boolean; known: boolean; fresh: boolean; pending: boolean }> {
    const peer = this.peer, track = this.audioTrack, root = this.localSourceTrack, epoch = this.audioHealthEpoch
    if(this.options.nativeRuntime){
      const current=this.nativeOwnerCurrent()&&this.microphoneReleased&&this.nativeSocketOpen
      const fresh=current&&this.nativeFrameCount>0&&performance.now()-this.nativeLastFrameAt<=1000
      return {current,known:fresh,fresh,pending:!fresh||!!this.manualAudio?.pendingCount()||this.nativeTools.size>0
        ||this.explicitCapture!=='idle'||this.explicitReplyPending||this.responseActive||this.externalPlaybackActive||protectedPlaybackBarrier.held()}
    }
    const current = (): boolean => !this.closed && !this.failureReported && !this.inputDeliveryFailed
      && this.peer === peer && this.audioTrack === track && this.localSourceTrack === root
      && this.sender?.track === track && track?.readyState === 'live' && track.enabled
      && root?.readyState === 'live' && root.enabled && root.muted === false
      && this.microphoneReleased && !this.microphoneEnded
      && peer?.connectionState === 'connected' && !this.externalPlaybackActive && !this.playbackAuthorized
    const unknown = () => ({ current: current(), known: false, fresh: false, pending: true })
    if (!current()) return unknown()
    // Join the existing bounded poll; maintenance must not race a second sampler.
    if (this.audioHealthReading) {
      const reading = this.audioHealthReadDone
      if (!reading) return unknown()
      await reading
    } else if (!this.audioActivityHealth.snapshot(performance.now()).fresh) await this.pollAudioHealth()
    if (!current() || epoch !== this.audioHealthEpoch) return unknown()
    const health = this.audioActivityHealth.snapshot(performance.now())
    const unfinished = [...this.pendingInputIds].some(id => this.inputItemPermissions.get(id) === true)
      || this.anonymousInputPermissions.some(allowed => allowed)
      || this.committedTurnQueue.length > 0 || this.completedTranscriptQueue.length > 0
      || this.pendingToolCalls.length > 0 || this.toolRunning || this.drainingCommittedTurns
      || this.drainingToolCalls || this.recoveringUntooledTurn || this.pendingPlayback !== null || this.drainingPlayback !== null
    return { current: true, ...health, pending: health.pending || unfinished }
  }

  takePendingInputsForReconnect(): QwenRealtimeQueuedInput[] {
    if (this.transcriptRecoveryTimer !== null) window.clearTimeout(this.transcriptRecoveryTimer)
    this.transcriptRecoveryTimer = null
    // Only waiting callbacks transfer here; durable Runtime admission is separate.
    return structuredClone([
      ...this.pendingToolCalls.splice(0).map(({ utterance, capture }) => ({ utterance, ...(capture ? { capture } : {}) })),
      ...this.completedTranscriptQueue.splice(0),
      ...this.committedTurnQueue.splice(0),
    ])
  }

  resumePendingInputs(utterances: (string | QwenRealtimeQueuedInput)[]): void {
    if (this.closed) throw new Error('不能向已关闭的实时会话恢复话语。')
    if (this.inputDeliveryFailed) throw new Error('原话交接失败，需在新连接恢复。')
    if (!this.options.callbacks.onUntooledTurn && !this.options.callbacks.onCommittedTurn) {
      throw new Error('当前实时会话缺少运行时恢复入口。')
    }
    this.pendingToolCalls = utterances.map<ToolCall>(input => ({
      callId: `reconnect-local-${crypto.randomUUID()}`,
      ...(typeof input === 'string' ? { utterance: input } : structuredClone(input)), reconnect: true as const,
    })).concat(this.pendingToolCalls)
    // Wait for actual track release; never fabricate supplier tool outputs.
  }

  private closeNativeSocketHost(hostId:string):void{
    const close=window.desktop?.closeQwenNativeSocket
    if(!close){this.options.callbacks.onError?.('客户端缺少云连接关闭接口。');return}
    void close(hostId).catch(()=>this.options.callbacks.onError?.('云连接关闭未返回确认。'))
  }
  private retireNativeSocket():void{
    this.stopNativeKeepalive()
    this.nativeVendorGeneratingResponseId=null;this.clearNativeRestore();this.nativeSendEpoch++;this.nativeSocketOpen=false;this.nativeSocketReady=false
    this.explicitReplyPending=false;this.explicitQuietUntil=0
    if(this.explicitCapture!=='idle')this.reportExplicitCapture('idle','语音连接已中断，这段录音未提交。')
    this.nativeSocketUnsubscribe?.();this.nativeSocketUnsubscribe=null
    this.nativeEarlyPackets=[];this.nativeEarlyBytes=0
    if(this.nativeSessionDeadline!==null)clearTimeout(this.nativeSessionDeadline);this.nativeSessionDeadline=null
    this.nativeDownlink?.dispose();this.nativeDownlink=null
  }
  close(reason = 'session_closed'): void {
    if (this.closed) return
    this.closed = true
    this.retireMetadataReader?.();this.retireMetadataReader=null
    this.metadataObserver?.invalidateStats()
    if(this.protectedTailTimer!==null)clearTimeout(this.protectedTailTimer)
    this.protectedTailTimer=null
    this.nativeResponseDiagnostics.clear()
    this.nativeSessionAckConfiguration=null;this.nativeSessionAckFields=[]
    this.manualAudio?.dispose();this.manualAudio=null
    this.nativeConversation?.close();this.nativeConversation=null
    this.stopNativeActivityWatch()
    this.retireNativeSocket()
    const hostConnectionId=this.nativeHostConnectionId;this.nativeHostConnectionId=null
    if(hostConnectionId)this.closeNativeSocketHost(hostConnectionId)
    this.protectedControl?.dispose()
    this.protectedControl = null
    this.nativeActivationBuffer?.dispose();this.nativeActivationBuffer=null
    this.localAttention?.dispose();this.localAttention=null
    this.standbyWake?.dispose();this.standbyWake=null
    this.audioTrack?.stop?.()
    this.localSourceTrack = null
    this.stopAudioHealthPolling()
    this.settlePlayback(false)
    if (this.transcriptRecoveryTimer !== null) window.clearTimeout(this.transcriptRecoveryTimer)
    this.transcriptRecoveryTimer = null
    this.unboundToolCalls = []
    this.setRemotePlaybackAuthorized(false)
    try { this.send({ type: 'session.finish', reason }) } catch { /* channel may already be gone */ }
    this.channel?.close()
    this.peer?.close()
    this.stream?.getTracks().forEach((track) => track.stop())
    this.remotePlaybackObservation?.dispose()
    this.remotePlaybackObservation = null
    if (this.remoteAudio) {
      this.remoteAudio.pause()
      this.remoteAudio.srcObject = null
      this.remoteAudio.remove()
    }
    this.channel = null
    this.peer = null
    this.stream = null
    this.committedTurnQueue = []
    this.drainingCommittedTurns = false
    this.pendingToolCalls = []
    this.drainingToolCalls = false
    this.seenToolCallIds.clear()
    this.cancelledResponseIds.clear()
    this.seenResponseIds.clear()
    this.protocolRejections.clear()
    this.committedAudioItemIds.clear()
    this.inputItemPermissions.clear()
    this.inputItemCaptures.clear()
    this.matchedInputCapture = undefined
    this.lastInputCapture = undefined
    this.responseInput = null
    this.pendingInputIds.clear()
    this.inputStartEventIds.clear()
    this.inputOnsetObserved = false
    this.anonymousInputPermissions = []
    this.anonymousInputCaptures = []
    this.anonymousInputOverflow = false
    this.activeResponseId = null
    this.completedTranscriptQueue = []
    this.responseTranscript = null
    this.sender = null
    this.audioTrack = null
    this.remoteAudio = null
    // Tail failure was already published; cleanup must not replace it with idle.
    if(reason!=='protected_tail_deadline'&&reason!=='protected_tail_setup_failed')this.publish('idle', false, reason)
  }

  private bindChannel(channel: RTCDataChannel): void {
    const activate = (): void => {
      if (this.closed || this.inputDeliveryFailed || this.retiredChannels.has(channel)) return
      const previous = this.channel
      this.channel = channel
      if (previous && previous !== channel) {
        this.retiredChannels.add(previous)
        previous.close()
      }
    }
    channel.addEventListener('open', () => {
      activate()
      if (this.closed || this.inputDeliveryFailed || this.channel !== channel) return
      if (this.sessionCreated) void this.beginConfiguredSession()
    })
    channel.addEventListener('message', (message) => {
      if (!this.closed && this.channel === channel) this.receive(message.data)
    })
    channel.addEventListener('error', () => {
      if (!this.retiredChannels.has(channel) && (!this.channel || this.channel === channel)) {
        this.fail(new Error('实时语音数据通道异常。'))
      }
    })
    channel.addEventListener('close', () => {
      if (!this.closed && !this.retiredChannels.has(channel) && this.channel === channel) {
        this.fail(new Error('实时语音数据通道已关闭，待处理原话保留供连接恢复。'))
      }
    })
    if (channel.readyState === 'open') activate()
  }

  private attachRemoteAudio(event: RTCTrackEvent): void {
    this.attachRemoteStream(event.track,event.streams[0]??new MediaStream([event.track]))
  }
  private attachRemoteStream(track:MediaStreamTrack,stream:MediaStream):void{
    if (track.kind !== 'audio' || this.closed || this.inputDeliveryFailed) return
    const audio = document.createElement('audio')
    audio.autoplay = true
    // Remote audio is denied until this activated connection explicitly owns
    // the response. Native dialogue remains unverified and never grants scoring.
    audio.muted = !this.playbackAuthorized
    audio.setAttribute('data-hsc-qwen-realtime-audio', 'true')
    audio.srcObject = stream
    audio.addEventListener('error', () => {
      if (!this.closed && this.remoteAudio === audio) this.fail(new Error('实时语音播放器异常。'))
    })
    const previous = this.remoteAudio
    this.remotePlaybackObservation?.dispose()
    this.remotePlaybackObservation = null
    if (previous) {
      previous.muted = true
      previous.pause()
      previous.srcObject = null
      previous.remove()
    }
    document.body.appendChild(audio)
    this.remoteAudio = audio
    try { this.remotePlaybackObservation = new RemotePlaybackObservation(audio, audio.srcObject as MediaStream) }
    catch {
      this.nativePlaybackObserverUnavailable=true
      this.options.callbacks.onError?.('实时语音播放观测不可用，不能确认回复已送达。')
    }
    if (this.playbackAuthorized) this.observeRemotePlayback()
    // Prime the continuous WebRTC stream while it is muted. Some Chromium
    // builds still require another explicit play() after speech is authorized.
    void audio.play().catch(() => undefined)
  }

  private handleConnectionState(): void {
    if (this.closed || this.inputDeliveryFailed) return
    const state = this.peer?.connectionState
    if (state === 'connected' && !this.sessionReady) this.publish('connecting', false, '媒体通道已连接，等待会话配置')
    else if (state === 'disconnected') this.publish('recovering', false, '实时语音连接暂时中断')
    else if (state === 'failed') this.fail(new Error('实时语音连接失败。'))
    else if (state === 'closed' && !this.closed) this.fail(new Error('实时语音连接已关闭。'))
  }

  private receive(raw: unknown): void {
    if (this.closed || this.inputDeliveryFailed || typeof raw !== 'string') return
    const event = parseServerEvent(raw)
    if (!event) { this.rejectProtocol('invalid_event'); return }
    const responseId = event.response_id ?? event.response?.id
    if (event.type?.startsWith('response.')) {
      if (this.responseIdentityExhausted) return
      // Missing/conflicting identities cannot borrow the current playback or original input.
      if (!validResponseId(responseId)
          || (event.response_id !== undefined && !validResponseId(event.response_id))
          || (event.response?.id !== undefined && !validResponseId(event.response.id))
          || (event.response_id !== undefined && event.response?.id !== undefined && event.response_id !== event.response.id)
          || (['response.created', 'response.done'].includes(event.type) && !validResponseId(event.response?.id))
          || (event.type === 'response.done' && !['completed', 'cancelled', 'failed', 'incomplete'].includes(event.response?.status ?? ''))) {
        this.rejectProtocol('response_identity_invalid'); return
      }
      // Observe only strictly identity-validated packets. Late/cancelled packets
      // still reach the original execution rejection below; metrics never admit them.
      if(this.options.nativeRuntime){
        const details=this.nativeResponseDiagnostics.observe(responseId!,event,
          this.responseActive&&responseId===this.activeResponseId&&!this.cancelledResponseIds.has(responseId!))
        if(details)this.recordNativeToolDiagnostic({event:'voice.realtime.native_response',
          level:details.response_status==='completed'?'info':'warn',details:{runtime:'qwen_omni_realtime',...details}})
      }
      if(this.options.nativeRuntime&&event.type==='response.done'&&this.nativeVendorGeneratingResponseId===responseId)this.nativeVendorGeneratingResponseId=null
      if (event.type !== 'response.created'
          && (!this.responseActive || responseId !== this.activeResponseId || this.cancelledResponseIds.has(responseId))) {
        this.rejectProtocol('response_not_current'); return
      }
    }
    if(this.options.nativeRuntime){this.receiveNative(event,responseId);return}
    switch (event.type) {
      case 'session.created':
        this.sessionCreated = true
        void this.beginConfiguredSession()
        return
      case 'session.updated':
        this.sessionReady = true
        void this.releaseMicrophoneGate()
        return
      case 'input_audio_buffer.speech_started': {
        const microphoneWasOpen = this.audioTrack?.enabled === true
        if (!this.recordInputStart(event, microphoneWasOpen)) return
        this.replyGeneration += 1
        this.cancelAssistantResponse()
        this.options.callbacks.onUserSpeechStart?.()
        this.publish('listening', true)
        return
      }
      case 'input_audio_buffer.speech_stopped':
        if (!this.inputAllowed(event)) return
        this.publish('transcribing', false)
        return
      case 'conversation.item.input_audio_transcription.delta': {
        if (!this.inputAllowed(event)) return
        const preview = `${event.text ?? ''}${event.stash ?? ''}`.trim()
        if (preview) this.options.callbacks.onUserTranscript?.(preview)
        return
      }
      case 'conversation.item.input_audio_transcription.failed': {
        const itemId = this.inputId(event)
        if (!itemId) { this.rejectProtocol('transcription_failure_identity_invalid'); return }
        if (this.committedAudioItemIds.has(itemId)) return
        if (this.pendingInputIds.has(itemId) && this.inputItemPermissions.get(itemId) === false) {
          this.pendingInputIds.delete(itemId)
          return
        }
        // A stale/denied failure cannot borrow an anonymous or newer onset.
        if (!this.pendingInputIds.has(itemId) || this.inputItemPermissions.get(itemId) !== true) {
          this.rejectProtocol('transcription_failure_not_pending')
          return
        }
        this.fail(new Error('这句话的语音转写失败，已停止本连接并保留其他已接纳原话；连接恢复后请重说未识别的一句。'))
        return
      }
      case 'conversation.item.input_audio_transcription.completed': {
        this.recordInputProgress('provider_completed')
        // A transport retry is not another utterance. Identical words spoken
        // in a new audio item still represent a distinct child turn.
        const itemId = event.item_id ?? event.event_id
        if (itemId && this.committedAudioItemIds.has(itemId)) { this.recordInputProgress('duplicate_discarded'); return }
        if (!this.inputAllowed(event, true)) { this.recordInputProgress('input_not_admitted'); return }
        const transcript = eventText(event)
        if (transcript) {
          if (itemId && !this.rememberInputId(this.committedAudioItemIds, itemId)) return
          this.audioActivityHealth.reset()
          this.audioHealthEpoch += 1
          this.lastTranscript = transcript
          this.lastInputCapture = this.matchedInputCapture ?? this.captureInputSource('unbound')
          const input = { utterance: transcript, ...(this.lastInputCapture ? { capture: this.lastInputCapture } : {}) }
          // Retain the frozen original before notifying consumers. A callback
          // can fail after starting durable admission; that is not a receipt.
          if (this.options.callbacks.onCommittedTurn) this.committedTurnQueue.push(input)
          else this.completedTranscriptQueue.push(input)
          try {
            this.recordInputProgress('callback_started')
            this.options.callbacks.onInputCompleted?.(transcript, input.capture ? structuredClone(input.capture) : undefined)
            this.recordInputProgress('callback_returned')
            if (this.closed) return
            this.options.callbacks.onUserTranscript?.(transcript)
          } catch {
            this.recordInputProgress('callback_failed')
            if (this.closed) return
            this.inputDeliveryFailed = true
            this.microphoneEnded = true
            this.replyGeneration += 1
            this.syncMicrophoneGate()
            this.cancelActiveInference()
            if (this.transcriptRecoveryTimer !== null) window.clearTimeout(this.transcriptRecoveryTimer)
            this.transcriptRecoveryTimer = null
            this.fail(new Error('原话交接未确认，已停止本连接处理并保留待恢复输入。'))
            return
          }
          if (this.closed) return
          if (this.options.callbacks.onCommittedTurn) {
            if (this.drainingCommittedTurns) this.publish('tool_running', false, `已按顺序保留${this.committedTurnQueue.length}句待处理语音`)
            void this.drainCommittedTurns()
          }
          else {
            const waiting = this.unboundToolCalls.shift()
            if (waiting) this.receive(JSON.stringify(waiting))
            if (!this.recoverObservedBypass()) this.scheduleTranscriptRecovery()
          }
        } else this.recordInputProgress('empty_transcript')
        return
      }
      case 'response.created':
        if (responseId && this.cancelledResponseIds.has(responseId)) return
        // A retried creation cannot replace a newer input/response binding.
        // Do not evict old identities and make them reusable on a long call.
        if (responseId && this.seenResponseIds.has(responseId)) return
        if (responseId && this.seenResponseIds.size >= 2048) {
          this.responseIdentityExhausted = true
          this.microphoneEnded = true
          this.syncMicrophoneGate()
          this.fail(new Error('实时会话已达到回合身份上限，需重新建立连接；待处理原话已保留。'))
          return
        }
        if (responseId) this.seenResponseIds.add(responseId)
        this.activeResponseId = responseId ?? null
        this.responseActive = true
        this.responseTranscript = null
        this.responseInput = null
        this.assistantTranscript = ''
        this.responseContainsToolCall = false
        this.unauthorizedOutputObserved = false
        this.publish('transcribing', false)
        return
      case 'response.function_call_arguments.done': {
        if (this.options.callbacks.onCommittedTurn) return
        if (event.name !== QWEN_REALTIME_TOOL_NAME || !event.call_id?.trim()) return
        if (this.seenToolCallIds.has(event.call_id ?? '')) return
        if (this.options.requireCommittedTranscript && this.responseContainsToolCall) return
        if (this.options.requireCommittedTranscript && !this.completedTranscriptQueue.length) {
          if (this.responseActive && !this.toolRunning && !this.pendingPlayback && !this.drainingPlayback && !this.responseContainsToolCall
              && this.unboundToolCalls.length < 16) this.unboundToolCalls.push(event)
          return
        }
        const input = (this.options.requireCommittedTranscript ? this.completedTranscriptQueue.shift() : this.responseInput)
          ?? this.completedTranscriptQueue.shift()
          ?? { utterance: this.lastTranscript, ...(this.lastInputCapture ? { capture: this.lastInputCapture } : {}) }
        const transcript = input.utterance
        this.responseInput = input
        this.responseTranscript = transcript || null
        const call = parseQwenToolCall(event, transcript)
        if (!call) return
        this.responseContainsToolCall = true
        this.enqueueToolCall({ ...call, ...(input.capture ? { capture: input.capture } : {}) })
        this.scheduleTranscriptRecovery()
        return
      }
      case 'response.audio_transcript.delta':
      case 'response.text.delta':
        if (typeof event.delta === 'string' && event.delta.trim()) {
          if (!this.playbackAuthorized) {
            this.unauthorizedOutputObserved = true
            this.recoverObservedBypass()
            return
          }
          this.assistantTranscript += event.delta
          this.options.callbacks.onAssistantTranscript?.(this.assistantTranscript)
        }
        return
      case 'response.audio_transcript.done':
      case 'response.text.done': {
        if (!this.playbackAuthorized) {
          if (eventText(event)) this.unauthorizedOutputObserved = true
          this.recoverObservedBypass()
          return
        }
        const transcript = eventText(event) || this.assistantTranscript.trim()
        if (transcript) this.options.callbacks.onAssistantTranscript?.(transcript)
        return
      }
      case 'response.audio.done':
        this.finishPlaybackAfterDrain()
        return
      case 'response.done':
        this.responseActive = false
        if (['failed', 'cancelled'].includes(event.response?.status ?? '')) this.settlePlayback(false)
        if (this.options.callbacks.onCommittedTurn) {
          this.assistantTranscript = ''
          this.unauthorizedOutputObserved = false
          if (!this.toolRunning) this.publish('listening', false)
          return
        }
        if (!this.responseContainsToolCall && !this.pendingPlayback && !this.drainingPlayback
            && (this.unauthorizedOutputObserved || (this.options.requireCommittedTranscript && this.completedTranscriptQueue.length))) {
          // A model-only response cannot authorize replay of a consumed turn.
          const input = this.options.requireCommittedTranscript
            ? this.completedTranscriptQueue.shift()
            : (this.responseInput ?? this.completedTranscriptQueue.shift()
              ?? { utterance: this.lastTranscript, ...(this.lastInputCapture ? { capture: this.lastInputCapture } : {}) })
          const transcript = input?.utterance.trim()
          this.lastTranscript = ''
          this.unauthorizedOutputObserved = false
          if (input && transcript && this.options.callbacks.onUntooledTurn) {
            this.responseTranscript = null
            this.responseInput = null
            void this.recoverUntooledTurn({ ...input, utterance: transcript }).finally(() => this.scheduleTranscriptRecovery())
            return
          }
          // The realtime model is a voice front end. Direct answers bypass the
          // durable Agent Runtime and must never reach the child unchanged.
          this.options.callbacks.onError?.('实时模型绕过了学习运行时，本轮没有可恢复的孩子转写。')
        }
        this.responseTranscript = null
        this.responseInput = null
        this.assistantTranscript = ''
        if (!this.pendingPlayback && !this.drainingPlayback && !this.toolRunning && !this.pendingToolCalls.length) this.publish('listening', false)
        return
      case 'error':
        this.responseActive = false
        this.fail(new Error(`千问实时语音返回错误。${event.error?.message ?? ''}`))
        return
      default:
        return
    }
  }

  private sendSessionUpdate(): void {
    if ((!this.options.nativeRuntime&&(!this.channel||this.channel.readyState!=='open')||this.options.nativeRuntime&&!this.nativeSocketOpen)||!this.sessionCreated||this.sessionUpdateSent)return
    this.sessionUpdateSent = true
    if(this.options.nativeRuntime){
      if(!this.nativeCatalog||!this.nativeContext||!this.nativeConversation)throw Error('云语音能力或上下文尚未核对，未开放收音。')
      const base=qwenRealtimeSessionUpdate(this.options.childName,this.nativeVoice??this.options.voice,false)
      ;(base.session as Record<string,unknown>).turn_detection=null
      const update=fitQwenNativeSession(base,this.nativeCatalog,this.nativeContext)
      this.nativeExpectedInstructions=(update.session as Record<string,unknown>).instructions as string
      this.nativeSessionAckFields=[...new Set(['id','model',...Object.keys(update.session as Record<string,unknown>),
        'modalities','voice','audio','input_audio_format','output_audio_format','input_audio_transcription','max_response_output_tokens'])]
      this.send(update)
    }else this.send(qwenRealtimeSessionUpdate(
      this.options.childName,
      this.options.voice,
      Boolean(this.options.callbacks.onCommittedTurn),
    ))
  }

  private async beginConfiguredSession(): Promise<void> {
    if (this.sessionUpdateSent || this.closed || this.inputDeliveryFailed) return
    try {
      // Qwen rejects VAD changes once an input track is pending. Send the full
      // session configuration while the transceiver is still gated.
      this.sendSessionUpdate()
      this.publish('connecting', false, '实时会话配置已发送，等待服务确认')
    } catch (error) {
      this.fail(error)
    }
  }

  private async releaseMicrophoneGate(): Promise<void> {
    if (!this.sessionReady || this.microphoneReleased || this.microphoneEnded || this.closed || !this.audioTrack) return
    if(this.options.nativeRuntime){
      this.microphoneReleased=true;this.nativeReleasedAt=performance.now();this.syncMicrophoneGate();this.startAudioHealthPolling();this.publish('listening',false);return
    }
    if(!this.sender)return
    try {
      await this.sender.replaceTrack(this.audioTrack)
      if (this.closed || this.microphoneEnded) return
      this.microphoneReleased = true
      if(this.options.nativeRuntime)this.nativeReleasedAt=performance.now()
      this.syncMicrophoneGate()
      this.startAudioHealthPolling()
      this.publish('listening', false)
      if (!this.closed && this.pendingToolCalls.length) void this.drainToolCalls()
    } catch (error) {
      this.fail(error)
    }
  }

  private async runTool(call: ToolCall): Promise<void> {
    if (call.reconnect) {
      this.toolRunning = true
      this.syncMicrophoneGate()
      this.publish('tool_running', false, '正在接续断线前已听完整的话语')
      try {
        if (this.options.callbacks.onUntooledTurn) await this.dispatchUntooledInput(call, 'reconnect')
        else if (call.capture) await this.options.callbacks.onCommittedTurn?.(call.utterance, structuredClone(call.capture))
        else await this.options.callbacks.onCommittedTurn?.(call.utterance)
      } catch (error) {
        this.options.callbacks.onError?.(error instanceof Error ? error.message : '这句话尚未取得运行时结果。')
      } finally {
        this.toolRunning = false
        this.syncMicrophoneGate()
      }
      return
    }
    if (!this.options.callbacks.onToolCall) return
    const generation = this.replyGeneration
    this.toolRunning = true
    this.publish('tool_running', false)
    try {
      const result = call.capture
        ? await this.options.callbacks.onToolCall(call.utterance, structuredClone(call.capture))
        : await this.options.callbacks.onToolCall(call.utterance)
      if (this.closed || this.inputDeliveryFailed) {
        if (!result.playbackAbandoned) result.onPlaybackAbandoned?.()
        return
      }
      const abandoned = generation !== this.replyGeneration || result.playbackAbandoned === true
      if (abandoned && !result.playbackAbandoned && result.speak !== false) result.onPlaybackAbandoned?.()
      this.settlePlayback(false)
      const shouldSpeak = result.speak !== false && !abandoned
      // Muting is not an egress boundary: a silent/abandoned tool result must
      // also withhold its original body from the supplier conversation.
      const responseText = shouldSpeak ? (result.responseText.trim() || '这一步没有得到可播报的结果，请再说一次。')
        : abandoned ? 'reply_abandoned_no_remote_speech' : 'local_reply_no_remote_speech'
      this.pendingPlayback = shouldSpeak ? result : null
      this.playbackStarted = false
      this.send({
        type: 'conversation.item.create',
        item: {
          type: 'function_call_output',
          call_id: call.callId,
          output: JSON.stringify({ response_text: responseText, ...(abandoned ? { playback_abandoned: true } : {}) }),
        },
      })
      if (!shouldSpeak) {
        this.setRemotePlaybackAuthorized(false)
        this.publish('listening', false)
        return
      }
      this.setRemotePlaybackAuthorized(true)
      // Qwen 3.8 realtime accepts only the event envelope here. Output
      // modalities are session-level configuration; sending an OpenAI-style
      // response.modalities object makes Bailian reject the tool result turn.
      this.send({ type: 'response.create' })
      this.publish('tool_running', false, '正在准备已授权的语音回复')
      if (this.options.requireCommittedTranscript) {
        await new Promise<void>(resolve => {
          this.playbackSettled = resolve
          this.playbackDeadline = window.setTimeout(() => {
            this.settlePlayback(false)
            this.options.callbacks.onError?.('实时回复未取得播放完成回执，已停止本轮播报。')
          }, 90_000)
        })
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '后台学习能力暂时没有完成请求。'
      this.send({
        type: 'conversation.item.create',
        item: { type: 'function_call_output', call_id: call.callId, output: JSON.stringify({ response_text: 'tool_failed_no_remote_speech' }) },
      })
      this.setRemotePlaybackAuthorized(false)
      this.options.callbacks.onError?.(message)
    } finally {
      this.toolRunning = false
    }
  }

  private enqueueToolCall(call: ToolCall): void {
    if (this.closed || this.seenToolCallIds.has(call.callId)) return
    this.seenToolCallIds.add(call.callId)
    this.pendingToolCalls.push(call)
    if (this.drainingToolCalls) {
      this.publish('tool_running', false, `已按顺序保留${this.pendingToolCalls.length}句待处理语音`)
    }
    void this.drainToolCalls()
  }

  private async drainToolCalls(): Promise<void> {
    if (this.drainingToolCalls || this.closed || this.inputDeliveryFailed) return
    this.drainingToolCalls = true
    try {
      while (!this.closed && !this.inputDeliveryFailed && this.pendingToolCalls.length) {
        const call = this.pendingToolCalls.shift()
        if (!call) continue
        await this.runTool(call)
      }
    } finally {
      this.drainingToolCalls = false
      if (!this.closed && !this.inputDeliveryFailed && !this.pendingPlayback && !this.toolRunning) this.publish('listening', false)
      if (!this.closed && !this.inputDeliveryFailed && this.pendingToolCalls.length) void this.drainToolCalls()
    }
  }

  private async drainCommittedTurns(): Promise<void> {
    if (this.drainingCommittedTurns || this.closed || this.inputDeliveryFailed || !this.options.callbacks.onCommittedTurn) return
    this.drainingCommittedTurns = true
    this.recoveringUntooledTurn = true
    try {
      while (!this.closed && !this.inputDeliveryFailed && this.committedTurnQueue.length) {
        const input = this.committedTurnQueue.shift()
        if (!input) continue
        this.toolRunning = true
        // Keep collecting human speech while the Agent Runtime is thinking.
        // Local/remote assistant playback is still separately microphone-gated
        // through externalPlaybackActive/playbackAuthorized to prevent echo.
        this.syncMicrophoneGate()
        this.publish('tool_running', false, '正在通过学习运行时处理这句话')
        try {
          if (input.capture) await this.options.callbacks.onCommittedTurn(input.utterance, structuredClone(input.capture))
          else await this.options.callbacks.onCommittedTurn(input.utterance)
        } catch (error) {
          this.options.callbacks.onError?.(error instanceof Error ? error.message : '后台学习能力暂时没有完成请求。')
        } finally {
          this.lastTranscript = ''
          this.toolRunning = false
          this.syncMicrophoneGate()
        }
      }
    } finally {
      this.recoveringUntooledTurn = false
      this.drainingCommittedTurns = false
      this.syncMicrophoneGate()
      if (!this.closed && !this.inputDeliveryFailed) this.publish('listening', false)
      // A final transcript can arrive between the empty check and cleanup.
      if (!this.closed && !this.inputDeliveryFailed && this.committedTurnQueue.length) void this.drainCommittedTurns()
    }
  }

  private recoverObservedBypass(): boolean {
    if (!this.options.requireCommittedTranscript || !this.options.callbacks.onUntooledTurn
        || this.closed || this.inputDeliveryFailed || !this.unauthorizedOutputObserved || this.responseContainsToolCall
        || this.toolRunning || this.recoveringUntooledTurn || this.pendingToolCalls.length
        || this.pendingPlayback || this.drainingPlayback) return false
    const utterance = this.completedTranscriptQueue.shift()
    if (!utterance) return false
    if (this.transcriptRecoveryTimer !== null) window.clearTimeout(this.transcriptRecoveryTimer)
    this.transcriptRecoveryTimer = null
    this.lastTranscript = ''; this.responseTranscript = null
    this.unauthorizedOutputObserved = false; this.unboundToolCalls = []
    this.cancelActiveInference()
    void this.recoverUntooledTurn(utterance).finally(() => this.scheduleTranscriptRecovery())
    return true
  }

  private async recoverUntooledTurn(input: QwenRealtimeQueuedInput): Promise<void> {
    if (this.recoveringUntooledTurn || this.closed || this.inputDeliveryFailed || !this.options.callbacks.onUntooledTurn) return
    this.recoveringUntooledTurn = true
    this.toolRunning = true
    this.syncMicrophoneGate()
    this.publish('tool_running', false, '正在通过学习运行时恢复这句话')
    try {
      await this.dispatchUntooledInput(input)
    } catch (error) {
      this.options.callbacks.onError?.(error instanceof Error ? error.message : '后台学习能力暂时没有完成请求。')
    } finally {
      this.recoveringUntooledTurn = false
      this.toolRunning = false
      this.syncMicrophoneGate()
      if (!this.closed && !this.inputDeliveryFailed) this.publish('listening', false)
    }
  }

  private async dispatchUntooledInput(input: QwenRealtimeQueuedInput, reason?: 'reconnect'): Promise<void> {
    const callback = this.options.callbacks.onUntooledTurn
    if (!callback) return
    if (input.capture) await callback(input.utterance, reason, structuredClone(input.capture))
    else if (reason) await callback(input.utterance, reason)
    else await callback(input.utterance)
  }

  private scheduleTranscriptRecovery(): void {
    if (this.transcriptRecoveryTimer !== null) window.clearTimeout(this.transcriptRecoveryTimer)
    this.transcriptRecoveryTimer = null
    if (!this.options.requireCommittedTranscript || this.closed || this.inputDeliveryFailed || !this.completedTranscriptQueue.length) return
    this.transcriptRecoveryTimer = window.setTimeout(() => {
      this.transcriptRecoveryTimer = null
      if (this.closed || this.inputDeliveryFailed) return
      if (this.toolRunning || this.pendingPlayback || this.drainingPlayback || this.recoveringUntooledTurn) {
        this.scheduleTranscriptRecovery(); return
      }
      const utterance = this.completedTranscriptQueue.shift()
      if (!utterance) return
      this.lastTranscript = ''; this.responseTranscript = null; this.unboundToolCalls = []
      this.cancelActiveInference()
      void this.recoverUntooledTurn(utterance).finally(() => this.scheduleTranscriptRecovery())
    }, 8_000)
  }

  private settlePlayback(outcome: false | 'unconfirmed'): void {
    const completion = this.pendingPlayback ?? this.drainingPlayback
    this.pendingPlayback = null; this.drainingPlayback = null
    if (this.playbackDrainTimer !== null) window.clearTimeout(this.playbackDrainTimer)
    if (this.playbackDeadline !== null) window.clearTimeout(this.playbackDeadline)
    this.playbackDrainTimer = null; this.playbackDeadline = null
    this.setRemotePlaybackAuthorized(false)
    const observed = this.playbackStarted
    this.playbackStarted = false
    const resolve = this.playbackSettled; this.playbackSettled = null
    try {
      if (outcome === 'unconfirmed' && observed && completion?.onPlaybackUnconfirmed) completion.onPlaybackUnconfirmed()
      else completion?.onPlaybackAbandoned?.()
    } catch {
      this.options.callbacks.onError?.('播放回执接收失败，原任务结果保留，未记为送达。')
    } finally { resolve?.() }
  }

  private cancelAssistantResponse(): void {
    if (this.pendingPlayback || this.drainingPlayback || this.assistantTranscript) {
      this.cancelActiveInference()
      this.settlePlayback(false)
      this.playbackStarted = false
      this.setRemotePlaybackAuthorized(false)
      this.assistantTranscript = ''
    }
  }

  private cancelActiveInference(source: NativeResponseCancelSource = 'other_local'): void {
    if (this.activeResponseId) this.rememberId(this.cancelledResponseIds, this.activeResponseId)
    const wasActive = this.responseActive
    // Cancellation receipts may arrive after a new response. Settle local
    // ownership now; stale receipts must never clear the new response's state.
    this.responseActive = false
    if (wasActive) {
      if (this.options.nativeRuntime && this.activeResponseId) this.nativeResponseDiagnostics.cancelRequested(this.activeResponseId, source)
      try { this.send({ type: 'response.cancel' }) } catch { /* transport may already be closed */ }
    }
  }

  private finishPlaybackAfterDrain(): void {
    const completion = this.pendingPlayback
    if (!completion) return
    this.pendingPlayback = null
    this.drainingPlayback = completion
    this.playbackDrainTimer = window.setTimeout(() => {
      if (this.closed || this.drainingPlayback !== completion) return
      // Generation EOS and a compatibility drain window cannot prove that
      // the current reply's last RTP sample reached the player.
      this.settlePlayback('unconfirmed')
      this.publish('listening', false)
    }, 350)
  }

  private setRemotePlaybackAuthorized(authorized: boolean): void {
    if(authorized&&this.options.nativeRuntime&&(this.options.nativeAudioAllowed?.()!==true
      ||this.options.nativeActivation&&!this.options.nativeActivation.isActive()))authorized=false
    this.playbackAuthorized = authorized
    if(this.options.nativeRuntime){
      if(authorized)this.nativeRemoteMediaActive=true
      else{this.nativeRemoteMediaActive=false;this.nativeAudioDeltaSeen=false;this.stopNativeActivityWatch();this.nativeDownlink?.cancel()}
    }
    // Duplex uses the browser's real AEC capability; unknown devices retain
    // the conservative speaker gate. Room echo quality still needs measurement.
    this.syncMicrophoneGate()
    const audio = this.remoteAudio
    if (!audio) return
    audio.muted = !authorized
    if (authorized) this.observeRemotePlayback()
    else this.remotePlaybackObservation?.disarm()
    if (authorized) {
      void audio.play().catch((error: unknown) => {
        if (this.remoteAudio === audio && this.playbackAuthorized) {
          this.fail(new Error(qwenPlayFailureMessage(error)))
        }
      })
    }
  }

  private observeRemotePlayback(): void {
    const observation = this.remotePlaybackObservation
    if(!observation){
      if(this.options.nativeRuntime&&!this.nativeCanDuplex())this.fail(new Error('保守收音路径缺少播放观测，正在恢复连接。'))
      return
    }
    if(this.playbackStarted)return
    observation.arm(() => {
      if (!this.closed && this.playbackAuthorized && this.remotePlaybackObservation === observation) this.markPlaybackStarted()
    }, () => {
      if (this.closed || this.remotePlaybackObservation !== observation || !this.playbackAuthorized) return
      if(this.options.nativeRuntime){
        this.nativePlaybackObserverUnavailable=true
        if(!this.nativeCanDuplex()){this.fail(new Error('保守收音路径的播放观测不可用，正在恢复连接。'));return}
        this.options.callbacks.onError?.('实时语音播放观测不可用，未取得送达回执。');return
      }
      this.settlePlayback(false)
      this.options.callbacks.onError?.('实时语音播放观测不可用，回复未取得送达回执。')
    })
  }

  private syncMicrophoneGate(): void {
    if (!this.audioTrack) return
    const aecDuplex = this.options.fullDuplex === true && microphoneEchoAllowsDuplex(this.audioTrack)
    const externalAecDuplex = this.options.fullDuplex === true && microphoneEchoAllowsDuplex(this.audioTrack, true)
    const wasEnabled = this.audioTrack.enabled
    if(this.options.nativeRuntime){
      this.audioTrack.enabled=false
      const allowed=this.nativeAppendAllowed()
      if(this.nativeWasAppendAllowed&&!allowed)this.nativeSendEpoch++
      this.nativeWasAppendAllowed=allowed;this.manualAudio?.gateChanged();return
    }
    this.audioTrack.enabled = this.microphoneReleased && !this.microphoneEnded && !this.closed
      && (!this.options.nativeActivation || this.options.nativeActivation.isActive())
      && !this.protectedControlHold && !protectedPlaybackBarrier.held()
      && (!this.options.protectedPlaybackControl || this.options.protectedPlaybackControl.isCurrent())
      && (!this.playbackAuthorized || aecDuplex)
      && (!this.externalPlaybackActive || (!this.externalPlaybackLocalOnly && externalAecDuplex))
    if (wasEnabled !== this.audioTrack.enabled) {
      this.audioActivityHealth.reset()
      this.audioHealthEpoch += 1
    }
  }

  private startAudioHealthPolling(): void {
    if (this.audioHealthTimer !== null || (!this.options.nativeRuntime&&typeof this.peer?.getStats !== 'function')) return
    this.audioHealthTimer = window.setInterval(() => { void this.pollAudioHealth() }, 500)
  }

  private stopAudioHealthPolling(): void {
    if (this.audioHealthTimer !== null) window.clearInterval(this.audioHealthTimer)
    this.audioHealthTimer = null
    this.audioHealthReadAbort?.abort()
    this.audioActivityHealth.reset()
    this.audioHealthEpoch += 1
    this.metadataObserver?.invalidateStats()
  }

  private async pollAudioHealth(): Promise<void> {
    const peer = this.peer, track = this.audioTrack, root = this.localSourceTrack, epoch = this.audioHealthEpoch
    if(this.options.nativeRuntime){
      if(this.closed||this.failureReported||!this.microphoneReleased)return
      if(root?.readyState==='ended'||track?.readyState==='ended')this.fail(new Error('麦克风音轨已断开，正在重新连接。'))
      else if(performance.now()-this.nativeReleasedAt>5000&&(!this.nativeFrameCount||performance.now()-this.nativeLastFrameAt>5000))
        this.fail(new Error('麦克风没有继续提供音频帧，正在重新连接。'))
      return
    }
    // stop() ends a track without firing its ended event. A paused live track
    // remains valid, but an attached ended source must enter normal recovery.
    if ((root?.readyState === 'ended' || track?.readyState === 'ended')
      && (!this.options.protectedPlaybackControl || this.options.protectedPlaybackControl.isCurrent())
      && !this.closed && !this.failureReported && !this.microphoneEnded && this.microphoneReleased
      && peer?.connectionState === 'connected' && this.peer === peer
      && track && this.audioTrack === track && this.localSourceTrack === root && this.sender?.track === track) {
      this.fail(new Error(root?.readyState === 'ended'
        ? '麦克风音轨已断开，正在重新连接。' : '麦克风传输音轨已断开，正在重新连接。'))
      return
    }
    if (this.audioHealthReading) return
    const ownsOpenSource = (): boolean => !this.closed && !this.failureReported && this.peer === peer
      && this.audioTrack === track && this.sender?.track === track && track?.readyState === 'live'
      && track.enabled && this.microphoneReleased && !this.microphoneEnded
      && peer?.connectionState === 'connected' && !this.externalPlaybackActive && !this.playbackAuthorized
    if (!peer || !track || !ownsOpenSource()) { this.metadataObserver?.invalidateStats();this.audioActivityHealth.reset(); return }
    this.audioHealthReading = true
    let finishRead!: () => void
    const readDone = new Promise<void>(resolve => { finishRead = resolve })
    this.audioHealthReadDone = readDone
    const controller = new AbortController()
    this.audioHealthReadAbort = controller
    let deadline: number | null = null
    try {
      const cancelled = new Promise<null>(resolve => {
        controller.signal.addEventListener('abort', () => resolve(null), { once: true })
        deadline = window.setTimeout(() => controller.abort(), 2_000)
      })
      const stats = await Promise.race([peer.getStats(), cancelled])
      if (!ownsOpenSource() || epoch !== this.audioHealthEpoch) return
      if (!stats) { this.metadataObserver?.invalidateStats();this.audioActivityHealth.reset(); return }
      const sources: Array<{ id: string; trackIdentifier?: string; totalAudioEnergy?: number; totalSamplesDuration?: number }> = []
      stats.forEach(row => { if (row.type === 'media-source' && row.kind === 'audio') sources.push(row) })
      this.metadataObserver?.observeStats(peer,track,epoch,sources,performance.now())
      const source = sources.find(row => row.trackIdentifier === track.id)
        ?? (sources.length === 1 && !sources[0].trackIdentifier ? sources[0] : undefined)
      const timedOut = this.audioActivityHealth.observe(source && typeof source.totalAudioEnergy === 'number'
        && typeof source.totalSamplesDuration === 'number'
        ? { sourceId: source.id, energy: source.totalAudioEnergy, duration: source.totalSamplesDuration } : null, performance.now())
      if (timedOut) this.fail(new Error('音频活动后30秒仍未取得完整转写。已停止当前连接，恢复后请重说；未识别内容没有交给学习任务。'))
    } catch {
      if (ownsOpenSource() && epoch === this.audioHealthEpoch) {this.metadataObserver?.invalidateStats();this.audioActivityHealth.reset()}
    } finally {
      if (deadline !== null) window.clearTimeout(deadline)
      if (this.audioHealthReadAbort === controller) this.audioHealthReadAbort = null
      this.audioHealthReading = false
      finishRead()
      if (this.audioHealthReadDone === readDone) this.audioHealthReadDone = null
    }
  }

  private inputId(event: RealtimeServerEvent): string | null {
    return typeof event.item_id === 'string' && event.item_id.length > 0 && event.item_id.length <= 256
      ? event.item_id : null
  }

  private rememberInputPermission(itemId: string, allowed: boolean): boolean {
    if (!this.inputItemPermissions.has(itemId) && this.inputItemPermissions.size >= 2048) {
      this.retireInputCapacity()
      return false
    }
    this.inputItemPermissions.set(itemId, allowed)
    return true
  }

  private retireInputCapacity(): void {
    this.fail(new Error('实时会话的输入身份容量已满，已停止本连接；需重新建立连接。'))
  }

  private rememberInputId(ids: Set<string>, id: string): boolean {
    if (ids.has(id)) return true
    // Identity history is a permission boundary, not an evictable cache.
    if (ids.size >= 2048) { this.retireInputCapacity(); return false }
    ids.add(id)
    return true
  }

  private captureInputSource(source: QwenRealtimeInputCapture['source']): QwenRealtimeInputCapture | undefined {
    if (!this.options.callbacks.captureInputContext) return undefined
    try {
      return structuredClone({ ...this.options.callbacks.captureInputContext(),
        localInputId: crypto.randomUUID(), capturedAt: Date.now(), source })
    } catch {
      this.options.callbacks.onError?.('这句话的采集来源没有取得，不能用于题目作答。')
      return undefined
    }
  }

  private recordInputStart(event: RealtimeServerEvent, allowed: boolean): boolean {
    const itemId = this.inputId(event)
    if (event.item_id !== undefined && itemId === null) {
      this.options.callbacks.onInputRejected?.('input_item_invalid')
      return false
    }
    if (typeof event.event_id === 'string' && event.event_id.length > 0 && event.event_id.length <= 256) {
      if (this.inputStartEventIds.has(event.event_id)) return false
      if (!this.rememberInputId(this.inputStartEventIds, event.event_id)) return false
    }
    this.inputOnsetObserved = true
    if (itemId) {
      // Transport replay must not reauthorize a denied item or interrupt twice.
      if (this.inputItemPermissions.has(itemId) || this.committedAudioItemIds.has(itemId)) return false
      if (this.pendingInputIds.size >= 64) {
        if (!this.rememberInputPermission(itemId, false)) return false
        this.options.callbacks.onInputRejected?.('anonymous_input_overflow')
        return false
      }
      if (!this.rememberInputPermission(itemId, allowed)) return false
      this.pendingInputIds.add(itemId)
      const capture = allowed ? this.captureInputSource('accepted_onset') : undefined
      if (capture) this.inputItemCaptures.set(itemId, capture)
    } else {
      if (this.anonymousInputPermissions.length >= 64) this.anonymousInputOverflow = true
      if (this.anonymousInputOverflow) {
        this.options.callbacks.onInputRejected?.('anonymous_input_overflow')
        return false
      }
      this.anonymousInputPermissions.push(allowed)
      this.anonymousInputCaptures.push(allowed ? this.captureInputSource('accepted_onset') : undefined)
    }
    if (!allowed) this.options.callbacks.onInputRejected?.('microphone_closed')
    return allowed
  }

  private inputAllowed(event: RealtimeServerEvent, completed = false): boolean {
    this.matchedInputCapture = undefined
    const itemId = this.inputId(event)
    if (event.item_id !== undefined && itemId === null) {
      this.options.callbacks.onInputRejected?.('input_item_invalid')
      return false
    }
    if (itemId && this.inputItemPermissions.has(itemId)) {
      const allowed = this.inputItemPermissions.get(itemId) === true
      if (allowed) this.matchedInputCapture = this.inputItemCaptures.get(itemId)
      if (completed) this.pendingInputIds.delete(itemId)
      if (!allowed) this.options.callbacks.onInputRejected?.('input_item_denied')
      return allowed
    }
    if (this.anonymousInputOverflow) {
      this.options.callbacks.onInputRejected?.('anonymous_input_overflow')
      return false
    }
    // Providers which omit an onset ID may still attach one to the final ASR.
    // Never infer permission from a microphone that reopened after that onset.
    let allowed: boolean
    if (this.anonymousInputPermissions.length === 1 && (itemId || this.pendingInputIds.size === 0)) {
      allowed = this.anonymousInputPermissions[0]
      if (allowed) this.matchedInputCapture = this.anonymousInputCaptures[0]
      // Binding a later ID consumes the anonymous onset now, not at completion.
      if (itemId || completed) {
        this.anonymousInputPermissions.shift()
        this.anonymousInputCaptures.shift()
      }
    } else if (!itemId && this.anonymousInputPermissions.length === 0 && this.pendingInputIds.size === 1) {
      const pendingId = this.pendingInputIds.values().next().value!
      allowed = this.inputItemPermissions.get(pendingId) === true
      if (allowed) this.matchedInputCapture = this.inputItemCaptures.get(pendingId)
      if (completed) {
        this.pendingInputIds.delete(pendingId)
        if (!this.rememberInputId(this.committedAudioItemIds, pendingId)) return false
      }
    } else if (this.inputOnsetObserved) {
      // Ambiguous or unmatched input cannot borrow another utterance's grant.
      this.options.callbacks.onInputRejected?.('input_item_denied')
      return false
    } else {
      // Qwen may send ASR deltas before speech_started. A preview cannot
      // reserve this item's grant: doing so skips the later real onset and
      // loses its frozen source (or borrows a microphone state from earlier).
      // Final-only transports remain explicitly unbound at the Runtime gate.
      if (!completed) return false
      allowed = this.audioTrack?.enabled === true
    }
    if (itemId) {
      if (!this.rememberInputPermission(itemId, allowed)) return false
      if (allowed && this.matchedInputCapture) this.inputItemCaptures.set(itemId, this.matchedInputCapture)
      if (!completed) this.pendingInputIds.add(itemId)
    }
    if (!allowed) this.options.callbacks.onInputRejected?.('microphone_closed')
    return allowed
  }

  private markPlaybackStarted(): void {
    if (!this.playbackAuthorized || this.playbackStarted) return
    this.playbackStarted = true
    if(this.options.nativeRuntime)this.watchNativeActivity()
    if(this.options.nativeRuntime)void window.desktop?.recordRendererDiagnostic?.({event:'voice.realtime.reply_first_audio',level:'info',
      details:{runtime:'qwen_omni_realtime',observation_kind:'remote_pcm_with_active_player',physical_audibility_verified:false,...this.manualAudio?.audioLatency(this.activeResponseId??'',performance.now())}}).catch(()=>undefined)
    ;(this.pendingPlayback ?? this.drainingPlayback)?.onPlaybackStart?.()
    this.publish('speaking', false)
  }

  private send(event: Record<string, unknown>, keepalive = false): void|Promise<boolean> {
    if(this.options.nativeRuntime){
      const hostId=this.nativeHostConnectionId,desktop=window.desktop,raw=JSON.stringify(event),epoch=this.nativeSendEpoch
      const audio=event.type==='input_audio_buffer.append'||event.type==='input_audio_buffer.commit'
      const cancelResponseId=event.type==='response.cancel'?this.nativeVendorGeneratingResponseId:null
      if(!hostId||!this.nativeSocketOpen||!desktop?.sendQwenNativeSocket)throw Error('自然语音连接未就绪。')
      if(this.nativePendingSendBytes+raw.length>2097152)throw Error('自然语音发送队列已满。')
      const sendSocket=desktop.sendQwenNativeSocket
      this.nativePendingSendBytes+=raw.length
      const delivery=this.nativeSendTail.then(async()=>{
        if(this.closed||this.inputDeliveryFailed||hostId!==this.nativeHostConnectionId||!this.nativeSocketOpen)return false
        if (keepalive) {
          if (!this.nativeKeepaliveIdle() || this.nativePendingSendBytes !== raw.length) return false
          this.nativeKeepaliveSentAt = performance.now()
        }
        if(event.type==='response.cancel'&&(!cancelResponseId||this.nativeVendorGeneratingResponseId!==cancelResponseId))return false
        if(audio&&(epoch!==this.nativeSendEpoch||!this.nativeAppendAllowed())){
          if(event.type==='input_audio_buffer.commit')this.manualAudio?.discardUnsentCommit()
          return false
        }
        if((event.type==='response.create'||event.type==='conversation.item.create')&&(epoch!==this.nativeSendEpoch||!this.nativeResponseAllowed()))return false
        await sendSocket(hostId,raw);return true
      }).catch(error=>{
        if (!keepalive) this.fail(nativeSocketFailure(error,'自然语音发送没有取得确认，正在恢复连接。'))
        return false
      }).finally(()=>{this.nativePendingSendBytes-=raw.length})
      this.nativeSendTail=delivery.then(()=>undefined)
      return delivery
    }
    if (!this.channel || this.channel.readyState !== 'open') throw new Error('实时语音数据通道未就绪。')
    if(this.options.nativeRuntime&&this.channel.bufferedAmount>262144)throw Error('实时语音发送队列暂时不可用。')
    this.channel.send(JSON.stringify(event))
  }

  private rememberId(ids: Set<string>, id: string): void {
    ids.add(id)
    if (ids.size > 2048) ids.delete(ids.values().next().value!)
  }

  private recordInputProgress(input_stage: 'provider_completed' | 'duplicate_discarded' | 'input_not_admitted'
    | 'callback_started' | 'callback_returned' | 'callback_failed' | 'empty_transcript'): void {
    try {
      void Promise.resolve(window.desktop?.recordRendererDiagnostic?.({
        event: 'voice.realtime.input_progress', details: { input_stage, runtime: 'qwen_omni_realtime' },
      })).catch(() => undefined)
    } catch { /* Diagnostics cannot alter input admission or callback recovery. */ }
  }

  private rejectProtocol(code: string): void {
    if (this.protocolRejections.has(code)) return
    this.protocolRejections.add(code)
    // Fixed, bounded diagnostics only: never log provider bodies, originals or identities.
    console.warn(`qwen_realtime_protocol_rejected:${code}`)
  }

  private publish(phase: RealtimeVoiceStatus['phase'], speechActive: boolean, detail?: string): void {
    if(phase==='listening'&&this.options.nativeRuntime&&this.externalPlaybackActive&&this.externalPlaybackLocalOnly){phase='speaking';speechActive=false;detail='正在播放本机受保护内容，云语音输入已暂停。'}
    if(phase==='listening'&&protectedPlaybackBarrier.held()){phase='recovering';speechActive=false;detail='正在确认播放尾部静音，尚未开放语音输入。'}
    this.options.callbacks.onStatus({
      runtime: 'qwen_omni_realtime',
      phase,
      speechActive,
      pendingUtterances: (this.manualAudio?.pendingCount()??0)+this.committedTurnQueue.length + this.completedTranscriptQueue.length + this.pendingToolCalls.length + (this.toolRunning ? 1 : 0),
      recoverable: phase !== 'error',
      detail,
    })
  }

  private failProtectedTail(): void {
    this.fail(new Error(PLAYBACK_TAIL_FAILURE))
    this.close('protected_tail_deadline')
  }

  private watchProtectedTailDeadline(): void {
    const token=protectedPlaybackBarrier.token()
    if(this.closed||!token)return
    if(this.protectedTailTimer!==null)clearTimeout(this.protectedTailTimer)
    // Arm before GUM/registration/attach. No frame or observer claim is required.
    this.protectedTailTimer=setTimeout(()=>{
      this.protectedTailTimer=null
      if(this.closed||protectedPlaybackBarrier.token()!==token)return
      if(protectedPlaybackBarrier.tick(performance.now()))this.failProtectedTail()
      else this.watchProtectedTailDeadline()
    },protectedPlaybackBarrier.remainingMs(performance.now()))
  }

  private fail(error: unknown): void {
    if (this.closed || this.failureReported) return
    this.failureReported = true
    this.stopNativeKeepalive()
    this.stopAudioHealthPolling()
    // Retire capture/dispatch before consumers run. Already-dispatched work
    // belongs to Runtime receipt recovery; only untouched originals transfer.
    this.inputDeliveryFailed = true
    this.microphoneEnded = true
    this.replyGeneration += 1
    this.syncMicrophoneGate()
    if (this.transcriptRecoveryTimer !== null) window.clearTimeout(this.transcriptRecoveryTimer)
    this.transcriptRecoveryTimer = null
    this.cancelActiveInference()
    const message = error instanceof Error ? error.message : '实时语音发生未知错误。'
    // A broken receipt/UI consumer cannot prevent gate cleanup or notifying
    // the independent connection-recovery consumer. Never expose its error.
    try { this.settlePlayback(false) } catch { /* Original task remains unconfirmed. */ }
    this.setRemotePlaybackAuthorized(false)
    try { this.options.callbacks.onError?.(message) } catch { /* Recovery status is independent. */ }
    if (!this.closed) {
      try { this.publish('error', false, message) } catch { /* Pending originals remain transferable. */ }
    }
  }
}
