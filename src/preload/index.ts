import { contextBridge, ipcRenderer } from 'electron'
import type { ControlledLaunchScope,ControlledLaunchIntent,ControlledLaunchProof } from '../shared/controlledLaunch'

type ModelEndpoint = { provider: 'qwen' | 'deepseek'; apiKey: string; baseUrl: string; model: string }
type ModelConfig = ModelEndpoint & { voiceIntentModel: string; fallback?: ModelEndpoint }
type ModelSummary = { loadDiagnostic?: 'loaded' | 'not_configured' | 'read_failed' | 'parse_failed' | 'secure_storage_unavailable' | 'config_unreadable'; configured: boolean; provider: 'qwen' | 'deepseek'; baseUrl: string; model: string; voiceIntentModel: string; fallbackConfigured: boolean; fallbackProvider?: 'qwen' | 'deepseek'; fallbackBaseUrl?: string; fallbackModel?: string }
type VoiceProvider = 'relay_pcm' | 'aliyun_cosyvoice' | 'qwen_audio_tts' | 'qwen_voice_clone'
type VoiceConfig = { provider: VoiceProvider; relayUrl: string; apiKey: string; voiceId: string; model?: string; workspaceId?: string; region?: string }
type VoiceSummary = { configured: boolean; provider: VoiceProvider | 'disabled'; relayUrl?: string; voiceId?: string; model?: string; workspaceId?: string; region?: string }
type ReferenceVoiceSample = { id: string; name: string; sizeBytes: number; recommended: boolean }
type ReferenceVoiceEnrollmentResult = VoiceSummary & { sampleName: string; sampleFingerprint: string; firstFrameMs: number; totalMs: number; sampleRate: number; pcmBase64: string }
type VoiceTrial = { trialId: string; createdAt: string; provider: 'relay_pcm'; relayUrl: string; voiceId: string; firstFrameMs: number; totalMs: number; naturalness: number; childIntelligibility: number; permissionVerified: boolean }
type VoiceEvaluationCandidate = { id: string; name: string; trait: string; role: 'primary' | 'secondary' | 'offline_fallback'; allowedUse: string }
type VoiceListeningRating = { ratingId: string; candidateId: string; createdAt: string; evaluatorRole: 'parent' | 'child' | 'joint'; naturalness: number; chineseClarity: number; englishMath: number; lowPressureReminder: number; seriousCorrection: number; fatigue30Minutes: number; childAcceptance: number; notes?: string }
type WakeWordSettings = { childId: string; wakeWord: string; inactivitySeconds: number; enabled: boolean; updatedAt: string }
type QwenRealtimeConfig = { apiKey: string; endpoint: string; model: 'qwen3.8-omni-flash-realtime'; voice: string; enabled: boolean }
type QwenRealtimeSummary = Omit<QwenRealtimeConfig, 'apiKey'> & { configured: boolean }
type VoiceOnboarding = { childId: string; version: 1; step: 'wake_practice' | 'study_practice' | 'document_practice' | 'finish_practice' | 'completed' | 'deferred'; updatedAt: string }
type VideoHardwareInventory = { supported: boolean; source: string; items: Array<{ name: string; manufacturer: string | null; driver_version: string | null; driver_provider: string | null; driver_date: string | null; usb_vid: string | null; usb_pid: string | null; transport_hint: string; instance_id_hash: string }>; error_code?: string }
type CameraPermissionStatus = 'granted' | 'denied' | 'not_determined' | 'restricted' | 'unknown'
type ReportDeliverySecretInput = { smtpPassword?: string; wecomWebhook?: string }
type ReportDeliverySecretSummary = { smtpPasswordConfigured: boolean; wecomWebhookConfigured: boolean }
type ReportDeliveryChannelClearResult = ReportDeliverySecretSummary & { credentialsCleared: boolean; metadataCleared: boolean; runtimeRestarted: boolean; runtimeRestartFailed: boolean }
type ReportDeliverySecretStatus = ReportDeliverySecretSummary & { state: 'ready' | 'empty' | 'unavailable' | 'corrupt' }
type DiagnosticSummary = { rootPath: string; logPath: string; crashDumpPath: string; currentLogBytes: number; rotatedLogCount: number; maxFileBytes: number; maxRotatedFiles: number; rawMediaRecorded: false | null; existingLogsVerified: boolean; crashDumpsInspected: false; uploadEnabled: false }
type RendererDiagnosticEvent = { event: string; level?: 'debug' | 'info' | 'warn' | 'error'; message?: string; correlationId?: string; details?: unknown }

function recoverySync<T>(channel: 'parent-draft-recovery:checkpoint' | 'parent-draft-recovery:revoke', ...args: unknown[]): T {
  const result = ipcRenderer.sendSync(channel, ...args) as { ok?: boolean; value?: T } | null
  if (!result || result.ok !== true) throw Error('PARENT_DRAFT_RECOVERY_UNCONFIRMED')
  return result.value as T
}

function awakeSync(kind: 'learning' | 'qualification', leaseId: string, sessionId: string | null, enabled: boolean): { active: boolean; held: boolean } {
  const result = ipcRenderer.sendSync('runtime:set-awake-owner', { kind, leaseId, sessionId, enabled }) as { ok?: boolean; value?: { active?: boolean; held?: boolean } } | null
  if (!result || result.ok !== true || typeof result.value?.active !== 'boolean' || typeof result.value?.held !== 'boolean' || result.value.held !== enabled || (enabled && !result.value.active)) throw Error('RUNTIME_AWAKE_UNCONFIRMED')
  return { active: result.value.active, held: result.value.held }
}

const api = {
  printMistakeBook: (input: import('../shared/mistakeBookPrint').MistakeBookPrintRequest): Promise<import('../shared/mistakeBookPrint').MistakeBookPrintResult> => ipcRenderer.invoke('mistake-book:print', input),
  prepareParentDraftRecovery: (childId: string, scope: import('../shared/parentDraftRecovery').ParentDraftRecoveryScope, parentToken: string): Promise<import('../shared/parentDraftRecovery').ParentDraftRecoveryPrepared> => ipcRenderer.invoke('parent-draft-recovery:prepare', childId, scope, parentToken),
  readParentDraftRecovery: (childId: string, scope: import('../shared/parentDraftRecovery').ParentDraftRecoveryScope, parentToken: string, revision: number): Promise<import('../shared/parentDraftRecovery').ParentDraftRecoveryRecovered> => ipcRenderer.invoke('parent-draft-recovery:read', childId, scope, parentToken, revision),
  checkpointParentDraftRecovery: (leaseId: string, sequence: number, snapshot: import('../shared/parentDraftRecovery').ParentDraftRecoverySnapshot): import('../shared/parentDraftRecovery').ParentDraftRecoveryCheckpoint => recoverySync('parent-draft-recovery:checkpoint', leaseId, sequence, snapshot),
  revokeParentDraftRecovery: (): void => recoverySync('parent-draft-recovery:revoke'),
  discardParentDraftRecovery: (childId: string, scope: import('../shared/parentDraftRecovery').ParentDraftRecoveryScope, parentToken: string, revision: number, explicit: true): Promise<import('../shared/parentDraftRecovery').ParentDraftRecoveryPrepared> => ipcRenderer.invoke('parent-draft-recovery:discard', childId, scope, parentToken, revision, explicit),

  captureNativeWritingOriginal: (input:import('../shared/nativeWriting').NativeWritingInput):Promise<import('../shared/nativeWriting').NativeWritingOriginal> => ipcRenderer.invoke('writing:capture-original',input),
  onOpenParentCenter: (callback: () => void): (() => void) => {
    const listener = (): void => callback()
    ipcRenderer.on('parent-center:open', listener)
    ipcRenderer.send('parent-center:ready')
    return () => ipcRenderer.removeListener('parent-center:open', listener)
  },
  getRuntimeConfig: (): Promise<{
    runtimeEpoch?: number
    dataScope?: 'trial' | 'formal'
    localApiUrl: string
    sessionToken: string
    status: 'starting' | 'ready' | 'error' | 'stopped'
    rotationAuthOnly?: boolean
    rotationKeyUpdatePending?: boolean
    error?: string
    recoveryAttempt?: number
    nextRetryAt?: string
  }> =>
    ipcRenderer.invoke('runtime:get-config'),
  rotationRecoveryContinue: (parentToken: string): Promise<{ status: 'completed' | 'restart_required' }> => ipcRenderer.invoke('rotation-recovery:continue', parentToken),
  rotationRecoveryCancel: (): Promise<void> => ipcRenderer.invoke('rotation-recovery:cancel'),
  restartLocalService: (): Promise<{
    runtimeEpoch?: number
    dataScope?: 'trial' | 'formal'
    localApiUrl: string
    sessionToken: string
    status: 'starting' | 'ready' | 'error' | 'stopped'
    error?: string
    recoveryAttempt?: number
    nextRetryAt?: string
  }> => ipcRenderer.invoke('runtime:restart'),
  getDiagnosticSummary: (): Promise<DiagnosticSummary> =>
    ipcRenderer.invoke('diagnostics:get-summary'),
  revealDiagnosticFolder: (): Promise<{ opened: true }> =>
    ipcRenderer.invoke('diagnostics:reveal'),
  recordRendererDiagnostic: (event: RendererDiagnosticEvent): Promise<{ recorded: boolean }> =>
    ipcRenderer.invoke('diagnostics:renderer-event', event),
  setAutoLaunch: (enabled: boolean): Promise<{ openAtLogin: boolean }> =>
    ipcRenderer.invoke('runtime:set-auto-launch', enabled),
  getAutoLaunch: (): Promise<{ openAtLogin: boolean }> =>
    ipcRenderer.invoke('runtime:get-auto-launch'),
  setDeviceTestAwake: async (enabled: boolean, leaseId: string): Promise<{ active: boolean; held: boolean }> =>
    awakeSync('qualification', leaseId, null, enabled),
  setLearningSessionAwake: (sessionId: string, leaseId: string, enabled: boolean): { active: boolean; held: boolean } =>
    awakeSync('learning', leaseId, sessionId, enabled),
  getVideoHardwareInventory: (): Promise<VideoHardwareInventory> =>
    ipcRenderer.invoke('hardware:list-video-devices'),
  getCameraPermissionStatus: (): Promise<CameraPermissionStatus> =>
    ipcRenderer.invoke('hardware:get-camera-permission-status'),
  chooseSeykKnowledgeRoot: (): Promise<{ rootPath: string; databasePath: string; transcriptRoot: string; videoRoot: string; valid: boolean } | null> =>
    ipcRenderer.invoke('knowledge:choose-seyk-root'),
  chooseCurriculumStandardsRoot: (): Promise<{ rootPath: string; valid: boolean } | null> =>
    ipcRenderer.invoke('knowledge:choose-standards-root'),
  openLocalPath: (path: string): Promise<{ opened: boolean }> =>
    ipcRenderer.invoke('knowledge:open-local-path', path),
  prepareControlledLaunch: (scope:ControlledLaunchScope):Promise<ControlledLaunchIntent> => ipcRenderer.invoke('controlled-launch:prepare',scope),
  executeNativePrivacy: (command:import('../shared/nativePrivacy').NativePrivacyCommand,parentToken:string):Promise<import('../shared/nativePrivacy').NativePrivacyResponse> => ipcRenderer.invoke('native-privacy:execute',command,parentToken),
  recoverNativePrivacy: (requestId:string,parentToken:string,explicit=false):Promise<import('../shared/nativePrivacy').NativePrivacyResponse> => ipcRenderer.invoke('native-privacy:recover',requestId,parentToken,explicit),
  listControlledLaunches: (scope:Pick<ControlledLaunchScope,'childId'|'sessionId'|'kind'>&{requestId?:string;includeSettled?:true}):Promise<ControlledLaunchIntent[]> => ipcRenderer.invoke('controlled-launch:list',scope),
  cancelControlledLaunch: (intentId:string):Promise<ControlledLaunchProof> => ipcRenderer.invoke('controlled-launch:cancel',intentId),
  finishControlledLaunch: (kind:'video'|'game',activityId:string):Promise<void> => ipcRenderer.invoke('controlled-launch:finish',kind,activityId),
  openControlledMedia: (values: { path: string; title: string; maxSeconds: number; contentSessionId?: string; nativeIntentId?:string }): Promise<{ opened: true }> =>
    ipcRenderer.invoke('controlled-media:open', values),
  closeControlledMedia: (reason = 'child_requested', expectedSessionId?: string): Promise<{ closed: boolean; sessionId?: string; proof?: string }> =>
    ipcRenderer.invoke('controlled-media:close', reason, expectedSessionId),
  onControlledMediaClosed: (callback: (reason: string, contentSessionId: string) => void): (() => void) => {
    const listener = (_event: Electron.IpcRendererEvent, reason: string, contentSessionId: string): void => callback(reason, contentSessionId)
    ipcRenderer.on('controlled-media:closed', listener)
    return () => ipcRenderer.removeListener('controlled-media:closed', listener)
  },
  chooseControlledGameExecutable: (): Promise<{ path: string } | null> =>
    ipcRenderer.invoke('controlled-game:choose-executable'),
  choosePortableBackup: (): Promise<{ path: string } | null> =>
    ipcRenderer.invoke('privacy:choose-portable-backup'),
  revealLocalFile: (path: string): Promise<{ revealed: true }> =>
    ipcRenderer.invoke('privacy:reveal-local-file', path),
  openControlledGame: (values: { path: string; title: string; maxSeconds: number; gameSessionId: string; nativeIntentId?:string }): Promise<{ opened: true; processId: number }> =>
    ipcRenderer.invoke('controlled-game:open', values),
  closeControlledGame: (reason = 'child_requested', expectedSessionId?: string): Promise<{ closed: boolean; sessionId?: string; proof?: string }> =>
    ipcRenderer.invoke('controlled-game:close', reason, expectedSessionId),
  onControlledGameClosed: (callback: (reason: string, gameSessionId: string) => void): (() => void) => {
    const listener = (_event: Electron.IpcRendererEvent, reason: string, gameSessionId: string): void => callback(reason, gameSessionId)
    ipcRenderer.on('controlled-game:closed', listener)
    return () => ipcRenderer.removeListener('controlled-game:closed', listener)
  },
  petAudioSettings: (childId: string, parentToken: string, command: import('../shared/petAudioSettings').PetAudioSettingsCommand): Promise<import('../shared/petAudioSettings').PetAudioSettingsSummary> =>
    ipcRenderer.invoke('pet-audio:settings', childId, parentToken, command),
  tutorExplanationSettings: (parentToken: string, command: import('../main/tutor-explanation-handler').TutorExplanationCommand): Promise<import('../main/tutor-explanation-handler').TutorExplanationResponse> => ipcRenderer.invoke('tutor-explanation:settings', parentToken, command),
  getModelSettings: (): Promise<ModelSummary> =>
    ipcRenderer.invoke('model:get-settings'),
  saveModelSettings: (config: ModelConfig): Promise<ModelSummary> =>
    ipcRenderer.invoke('model:save-settings', config),
  clearModelSettings: (): Promise<ModelSummary> =>
    ipcRenderer.invoke('model:clear-settings'),
  testModelSettings: (config: ModelConfig): Promise<{ ok: boolean; latencyMs: number; model: string }> =>
    ipcRenderer.invoke('model:test-settings', config),
  getVoiceSettings: (): Promise<VoiceSummary> => ipcRenderer.invoke('voice:get-settings'),
  getQwenRealtimeSettings: (): Promise<QwenRealtimeSummary> => ipcRenderer.invoke('voice:realtime-get-settings'),
  getVoiceRuntimePolicy: (): Promise<import('../shared/voiceRuntimePolicy').VoiceRuntimePolicy> => ipcRenderer.invoke('voice:get-runtime-policy'),
  openPublicReplySpeech: (source: import('../shared/publicReplySpeech').PublicReplySpeechSource): Promise<import('../shared/publicReplySpeech').PublicReplySpeechOpen> => ipcRenderer.invoke('voice:public-reply-open', source),
  generatePublicReplySpeech: (leaseId: string): Promise<import('../shared/publicReplySpeech').PublicReplyAudio> => ipcRenderer.invoke('voice:public-reply-generate', leaseId),
  settlePublicReplySpeech: (leaseId: string, event: import('../shared/publicReplySpeech').PublicReplySettlement): Promise<void> => ipcRenderer.invoke('voice:public-reply-settle', leaseId, event),
  cancelPublicReplySpeech: (leaseId: string): Promise<void> => ipcRenderer.invoke('voice:public-reply-cancel', leaseId),
  onPublicReplySpeechRetired: (callback: (leaseId: string) => void): (() => void) => {
    const listener = (_: Electron.IpcRendererEvent, leaseId: string) => callback(leaseId)
    ipcRenderer.on('voice:public-reply-retired', listener)
    return () => ipcRenderer.removeListener('voice:public-reply-retired', listener)
  },
  saveQwenRealtimeSettings: (config: QwenRealtimeConfig): Promise<QwenRealtimeSummary> => ipcRenderer.invoke('voice:realtime-save-settings', config),
  testQwenRealtimeSettings: (config: QwenRealtimeConfig): Promise<{ ok: true; latencyMs: number; model: string }> => ipcRenderer.invoke('voice:realtime-test-settings', config),
  clearQwenRealtimeSettings: (): Promise<QwenRealtimeSummary> => ipcRenderer.invoke('voice:realtime-clear-settings'),
  negotiateQwenRealtime: (offerSdp: string): Promise<{ answerSdp: string; model: string; voice: string; nativeHostConnectionId?: string }> => ipcRenderer.invoke('voice:realtime-negotiate', offerSdp),
  forwardQwenNativeRuntime: (request: import('../shared/nativeRealtimeCapability').NativeRealtimeForward): Promise<unknown> => ipcRenderer.invoke('voice:realtime-native-runtime', request),
  closeQwenNativeConnection: (connectionId: string): Promise<void> => ipcRenderer.invoke('voice:realtime-native-close', connectionId),
  openQwenNativeSocket: (sessionId: string): Promise<import('../shared/qwenNativeSocket').QwenNativeSocketOpen> => ipcRenderer.invoke('voice:native-socket-open', sessionId),
  sendQwenNativeSocket: (id: string, raw: string): Promise<void> => ipcRenderer.invoke('voice:native-socket-send', id, raw),
  closeQwenNativeSocket: (id: string): Promise<void> => ipcRenderer.invoke('voice:native-socket-close', id),
  onQwenNativeSocketEvent: (callback: (packet: import('../shared/qwenNativeSocket').QwenNativeSocketEvent) => void): (() => void) => {
    const listener = (_: Electron.IpcRendererEvent, packet: import('../shared/qwenNativeSocket').QwenNativeSocketEvent) => callback(packet)
    ipcRenderer.on('voice:native-socket-event', listener)
    return () => ipcRenderer.removeListener('voice:native-socket-event', listener)
  },
  saveVoiceSettings: (config: VoiceConfig): Promise<VoiceSummary> => ipcRenderer.invoke('voice:save-settings', config),
  clearVoiceSettings: (): Promise<VoiceSummary> => ipcRenderer.invoke('voice:clear-settings'),
  getVoiceTrials: (): Promise<VoiceTrial[]> => ipcRenderer.invoke('voice:get-trials'),
  recordVoiceTrial: (trial: VoiceTrial): Promise<VoiceTrial[]> => ipcRenderer.invoke('voice:record-trial', trial),
  getVoiceListeningRatings: (): Promise<VoiceListeningRating[]> => ipcRenderer.invoke('voice:get-listening-ratings'),
  recordVoiceListeningRating: (rating: VoiceListeningRating): Promise<VoiceListeningRating[]> => ipcRenderer.invoke('voice:record-listening-rating', rating),
  getWakeWordSettings: (childId: string): Promise<WakeWordSettings | null> => ipcRenderer.invoke('voice:get-wake-word', childId),
  getVoiceOnboarding: (childId: string): Promise<VoiceOnboarding | null> => ipcRenderer.invoke('voice:get-onboarding', childId),
  saveVoiceOnboarding: (progress: VoiceOnboarding): Promise<VoiceOnboarding> => ipcRenderer.invoke('voice:save-onboarding', progress),
  saveWakeWordSettings: (settings: WakeWordSettings): Promise<WakeWordSettings> => ipcRenderer.invoke('voice:save-wake-word', settings),
  listVoiceEvaluationCandidates: (): Promise<VoiceEvaluationCandidate[]> => ipcRenderer.invoke('voice:list-evaluation-candidates'),
  readVoiceEvaluationAudio: (candidateId: string): Promise<{ mimeType: 'audio/mpeg' | 'audio/wav'; base64: string }> => ipcRenderer.invoke('voice:read-evaluation-audio', candidateId),
  listReferenceVoiceSamples: (): Promise<ReferenceVoiceSample[]> => ipcRenderer.invoke('voice:list-reference-samples'),
  enrollReferenceVoice: (values: { sampleId: string; apiKey?: string; rightsConfirmed: boolean }): Promise<ReferenceVoiceEnrollmentResult> => ipcRenderer.invoke('voice:enroll-reference', values),
  previewVoiceSettings: (config: VoiceConfig): Promise<{ ok: boolean; firstFrameMs: number; totalMs: number; sampleRate: number; pcmBase64: string }> => ipcRenderer.invoke('voice:preview-settings', config)
  ,saveReportDeliverySecrets: (values: ReportDeliverySecretInput): Promise<ReportDeliverySecretSummary> => ipcRenderer.invoke('report-delivery:save-secrets', values)
  ,clearReportDeliverySecrets: (names?: Array<keyof ReportDeliverySecretInput>): Promise<ReportDeliverySecretSummary> => ipcRenderer.invoke('report-delivery:clear-secrets', names)
  ,clearReportDeliveryChannel: (channel: 'email' | 'wechat_work_bot', parentToken: string): Promise<ReportDeliveryChannelClearResult> => ipcRenderer.invoke('report-delivery:clear-channel', channel, parentToken)
  ,getReportDeliverySecretStatus: (): Promise<ReportDeliverySecretStatus> => ipcRenderer.invoke('report-delivery:get-secret-status')
  ,getHouseholdSetupDefaults: (): Promise<import('../main/household-setup-defaults').HouseholdSetupDefaults | null> => ipcRenderer.invoke('household:get-setup-defaults')
}

contextBridge.exposeInMainWorld('desktop', api)
