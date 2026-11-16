/// <reference types="vite/client" />

type DesktopModelEndpoint = { provider: 'qwen' | 'deepseek'; apiKey: string; baseUrl: string; model: string }
type DesktopModelConfig = DesktopModelEndpoint & { voiceIntentModel: string; fallback?: DesktopModelEndpoint }
type DesktopModelSummary = { loadDiagnostic?: 'loaded' | 'not_configured' | 'read_failed' | 'parse_failed' | 'secure_storage_unavailable' | 'config_unreadable'; configured: boolean; provider: 'qwen' | 'deepseek'; baseUrl: string; model: string; voiceIntentModel: string; fallbackConfigured: boolean; fallbackProvider?: 'qwen' | 'deepseek'; fallbackBaseUrl?: string; fallbackModel?: string }
type DesktopVoiceProvider = 'relay_pcm' | 'aliyun_cosyvoice' | 'qwen_audio_tts' | 'qwen_voice_clone'
type DesktopVoiceConfig = { provider: DesktopVoiceProvider; relayUrl: string; apiKey: string; voiceId: string; model?: string; workspaceId?: string; region?: string }
type DesktopVoiceSummary = { configured: boolean; provider: DesktopVoiceProvider | 'disabled'; relayUrl?: string; voiceId?: string; model?: string; workspaceId?: string; region?: string }
type DesktopReferenceVoiceSample = { id: string; name: string; sizeBytes: number; recommended: boolean }
type DesktopReferenceVoiceEnrollmentResult = DesktopVoiceSummary & { sampleName: string; sampleFingerprint: string; firstFrameMs: number; totalMs: number; sampleRate: number; pcmBase64: string }
type DesktopVoiceTrial = { trialId: string; createdAt: string; provider: 'relay_pcm'; relayUrl: string; voiceId: string; firstFrameMs: number; totalMs: number; naturalness: number; childIntelligibility: number; permissionVerified: boolean }
type DesktopVoiceEvaluationCandidate = { id: string; name: string; trait: string; role: 'primary' | 'secondary' | 'offline_fallback'; allowedUse: string }
type DesktopVoiceListeningRating = { ratingId: string; candidateId: string; createdAt: string; evaluatorRole: 'parent' | 'child' | 'joint'; naturalness: number; chineseClarity: number; englishMath: number; lowPressureReminder: number; seriousCorrection: number; fatigue30Minutes: number; childAcceptance: number; notes?: string }
type DesktopWakeWordSettings = { childId: string; wakeWord: string; inactivitySeconds: number; enabled: boolean; updatedAt: string }
type DesktopQwenRealtimeConfig = { apiKey: string; endpoint: string; model: 'qwen3.8-omni-flash-realtime'; voice: string; enabled: boolean }
type DesktopQwenRealtimeSummary = Omit<DesktopQwenRealtimeConfig, 'apiKey'> & { configured: boolean }
type DesktopReportDeliverySecretInput = { smtpPassword?: string; wecomWebhook?: string }
type DesktopReportDeliverySecretSummary = { smtpPasswordConfigured: boolean; wecomWebhookConfigured: boolean }
type DesktopReportDeliverySecretStatus = DesktopReportDeliverySecretSummary & { state: 'ready' | 'empty' | 'unavailable' | 'corrupt' }
type DesktopReportDeliveryChannelClearResult = DesktopReportDeliverySecretSummary & { credentialsCleared: boolean; metadataCleared: boolean; runtimeRestarted: boolean; runtimeRestartFailed: boolean }
type DesktopDiagnosticSummary = { rootPath: string; logPath: string; crashDumpPath: string; currentLogBytes: number; rotatedLogCount: number; maxFileBytes: number; maxRotatedFiles: number; rawMediaRecorded: false | null; existingLogsVerified: boolean; crashDumpsInspected: false; uploadEnabled: false }
type DesktopRendererDiagnosticEvent = { event: string; level?: 'debug' | 'info' | 'warn' | 'error'; message?: string; correlationId?: string; details?: unknown }
type DesktopVideoHardwareItem = { name: string; manufacturer: string | null; driver_version: string | null; driver_provider: string | null; driver_date: string | null; usb_vid: string | null; usb_pid: string | null; transport_hint: 'usb' | 'bluetooth' | 'software' | 'unknown'; instance_id_hash: string }
type DesktopVideoHardwareInventory = { supported: boolean; source: 'windows_pnp_cim_v1' | 'unsupported_platform' | 'windows_inventory_failed'; items: DesktopVideoHardwareItem[]; error_code?: 'WINDOWS_INVENTORY_FAILED' }
type DesktopCameraPermissionStatus = 'granted' | 'denied' | 'not_determined' | 'restricted' | 'unknown'

interface Window {
    desktop?: {
    printMistakeBook?: (input: import('../../shared/mistakeBookPrint').MistakeBookPrintRequest) => Promise<import('../../shared/mistakeBookPrint').MistakeBookPrintResult>
      openPublicReplySpeech?: (source: import('../../shared/publicReplySpeech').PublicReplySpeechSource) => Promise<import('../../shared/publicReplySpeech').PublicReplySpeechOpen>
      generatePublicReplySpeech?: (leaseId: string) => Promise<import('../../shared/publicReplySpeech').PublicReplyAudio>
      settlePublicReplySpeech?: (leaseId: string, event: import('../../shared/publicReplySpeech').PublicReplySettlement) => Promise<void>
      cancelPublicReplySpeech?: (leaseId: string) => Promise<void>
      onPublicReplySpeechRetired?: (callback: (leaseId: string) => void) => (() => void)
    captureNativeWritingOriginal?: (input:import('../../shared/nativeWriting').NativeWritingInput)=>Promise<import('../../shared/nativeWriting').NativeWritingOriginal>
    onOpenParentCenter?: (callback: () => void) => (() => void)
    getRuntimeConfig: () => Promise<{
      runtimeEpoch?: number
      rotationAuthOnly?: boolean
      rotationKeyUpdatePending?: boolean
      dataScope?: 'trial' | 'formal'
      localApiUrl: string
      sessionToken: string
      status: 'starting' | 'ready' | 'error' | 'stopped'
      error?: string
      recoveryAttempt?: number
      nextRetryAt?: string
    }>
    rotationRecoveryContinue?: (parentToken: string) => Promise<{ status: 'completed' | 'restart_required' }>
    rotationRecoveryCancel?: () => Promise<void>
    restartLocalService: () => Promise<{
      runtimeEpoch?: number
      dataScope?: 'trial' | 'formal'
      localApiUrl: string
      sessionToken: string
      status: 'starting' | 'ready' | 'error' | 'stopped'
      error?: string
      recoveryAttempt?: number
      nextRetryAt?: string
    }>
    getDiagnosticSummary?: () => Promise<DesktopDiagnosticSummary>
    revealDiagnosticFolder?: () => Promise<{ opened: true }>
    recordRendererDiagnostic?: (event: DesktopRendererDiagnosticEvent) => Promise<{ recorded: boolean }>
    setAutoLaunch: (enabled: boolean) => Promise<{ openAtLogin: boolean }>
    getAutoLaunch: () => Promise<{ openAtLogin: boolean }>
    setDeviceTestAwake: (enabled: boolean, leaseId: string) => Promise<{ active: boolean; held: boolean }>
    setLearningSessionAwake: (sessionId: string, leaseId: string, enabled: boolean) => { active: boolean; held: boolean }
    getVideoHardwareInventory: () => Promise<DesktopVideoHardwareInventory>
    getCameraPermissionStatus?: () => Promise<DesktopCameraPermissionStatus>
    chooseSeykKnowledgeRoot: () => Promise<{ rootPath: string; databasePath: string; transcriptRoot: string; videoRoot: string; valid: boolean } | null>
    chooseCurriculumStandardsRoot: () => Promise<{ rootPath: string; valid: boolean } | null>
    openLocalPath: (path: string) => Promise<{ opened: boolean }>
    prepareControlledLaunch?: (scope:import('../../shared/controlledLaunch').ControlledLaunchScope)=>Promise<import('../../shared/controlledLaunch').ControlledLaunchIntent>
    executeNativePrivacy?: (command:import('../../shared/nativePrivacy').NativePrivacyCommand,parentToken:string)=>Promise<import('../../shared/nativePrivacy').NativePrivacyResponse>
    recoverNativePrivacy?: (requestId:string,parentToken:string,explicit?:boolean)=>Promise<import('../../shared/nativePrivacy').NativePrivacyResponse>
    listControlledLaunches?: (scope:Pick<import('../../shared/controlledLaunch').ControlledLaunchScope,'childId'|'sessionId'|'kind'>&{requestId?:string;includeSettled?:true})=>Promise<import('../../shared/controlledLaunch').ControlledLaunchIntent[]>
    cancelControlledLaunch?: (intentId:string)=>Promise<import('../../shared/controlledLaunch').ControlledLaunchProof>
    finishControlledLaunch?: (kind:'video'|'game',activityId:string)=>Promise<void>
    openControlledMedia?: (values: { path: string; title: string; maxSeconds: number; contentSessionId?: string; nativeIntentId?:string }) => Promise<{ opened: true }>
    chooseControlledGameExecutable?: () => Promise<{ path: string } | null>
    choosePortableBackup?: () => Promise<{ path: string } | null>
    revealLocalFile?: (path: string) => Promise<{ revealed: true }>
    openControlledGame?: (values: { path: string; title: string; maxSeconds: number; gameSessionId: string; nativeIntentId?:string }) => Promise<{ opened: true; processId: number }>
    closeControlledGame?: (reason?: string, expectedSessionId?: string) => Promise<{ closed: boolean; sessionId?: string; proof?: string }>
    onControlledGameClosed?: (callback: (reason: string, gameSessionId: string) => void) => () => void
    closeControlledMedia?: (reason?: string, expectedSessionId?: string) => Promise<{ closed: boolean; sessionId?: string; proof?: string }>
    onControlledMediaClosed?: (callback: (reason: string, contentSessionId: string) => void) => () => void
    tutorExplanationSettings?: (parentToken: string, command: import('../../main/tutor-explanation-handler').TutorExplanationCommand) => Promise<import('../../main/tutor-explanation-handler').TutorExplanationResponse>
    petAudioSettings?: (childId: string, parentToken: string, command: import('../../shared/petAudioSettings').PetAudioSettingsCommand) => Promise<import('../../shared/petAudioSettings').PetAudioSettingsSummary>
    getModelSettings: () => Promise<DesktopModelSummary>
    saveModelSettings: (config: DesktopModelConfig) => Promise<DesktopModelSummary>
    clearModelSettings: () => Promise<DesktopModelSummary>
    testModelSettings: (config: DesktopModelConfig) => Promise<{ ok: boolean; latencyMs: number; model: string }>
    getVoiceSettings?: () => Promise<DesktopVoiceSummary>
    getQwenRealtimeSettings?: () => Promise<DesktopQwenRealtimeSummary>
    getVoiceRuntimePolicy?: () => Promise<import('../../shared/voiceRuntimePolicy').VoiceRuntimePolicy>
    saveQwenRealtimeSettings?: (config: DesktopQwenRealtimeConfig) => Promise<DesktopQwenRealtimeSummary>
    testQwenRealtimeSettings?: (config: DesktopQwenRealtimeConfig) => Promise<{ ok: true; latencyMs: number; model: string }>
    clearQwenRealtimeSettings?: () => Promise<DesktopQwenRealtimeSummary>
    negotiateQwenRealtime?: (offerSdp: string) => Promise<{ answerSdp: string; model: string; voice: string; nativeHostConnectionId?: string }>
    forwardQwenNativeRuntime?: (request: import('../../shared/nativeRealtimeCapability').NativeRealtimeForward) => Promise<unknown>
    closeQwenNativeConnection?: (id: string) => Promise<void>
    openQwenNativeSocket?: (sessionId: string) => Promise<import('../../shared/qwenNativeSocket').QwenNativeSocketOpen>
    sendQwenNativeSocket?: (id: string, raw: string) => Promise<void>
    closeQwenNativeSocket?: (id: string) => Promise<void>
    onQwenNativeSocketEvent?: (callback: (packet: import('../../shared/qwenNativeSocket').QwenNativeSocketEvent) => void) => (() => void)
    saveVoiceSettings?: (config: DesktopVoiceConfig) => Promise<DesktopVoiceSummary>
    clearVoiceSettings?: () => Promise<DesktopVoiceSummary>
    getVoiceTrials?: () => Promise<DesktopVoiceTrial[]>
    recordVoiceTrial?: (trial: DesktopVoiceTrial) => Promise<DesktopVoiceTrial[]>
    getVoiceListeningRatings?: () => Promise<DesktopVoiceListeningRating[]>
    recordVoiceListeningRating?: (rating: DesktopVoiceListeningRating) => Promise<DesktopVoiceListeningRating[]>
    getWakeWordSettings?: (childId: string) => Promise<DesktopWakeWordSettings | null>
    saveWakeWordSettings?: (settings: DesktopWakeWordSettings) => Promise<DesktopWakeWordSettings>
    listVoiceEvaluationCandidates?: () => Promise<DesktopVoiceEvaluationCandidate[]>
    readVoiceEvaluationAudio?: (candidateId: string) => Promise<{ mimeType: 'audio/mpeg' | 'audio/wav'; base64: string }>
    listReferenceVoiceSamples?: () => Promise<DesktopReferenceVoiceSample[]>
    enrollReferenceVoice?: (values: { sampleId: string; apiKey?: string; rightsConfirmed: boolean }) => Promise<DesktopReferenceVoiceEnrollmentResult>
    previewVoiceSettings?: (config: DesktopVoiceConfig) => Promise<{ ok: boolean; firstFrameMs: number; totalMs: number; sampleRate: number; pcmBase64: string }>
    saveReportDeliverySecrets?: (values: DesktopReportDeliverySecretInput) => Promise<DesktopReportDeliverySecretSummary>
    clearReportDeliverySecrets?: (names?: Array<keyof DesktopReportDeliverySecretInput>) => Promise<DesktopReportDeliverySecretSummary>
    clearReportDeliveryChannel?: (channel: 'email' | 'wechat_work_bot', parentToken: string) => Promise<DesktopReportDeliveryChannelClearResult>
    getReportDeliverySecretStatus?: () => Promise<DesktopReportDeliverySecretStatus>
    getHouseholdSetupDefaults?: () => Promise<import('../../main/household-setup-defaults').HouseholdSetupDefaults | null>
  }
}
