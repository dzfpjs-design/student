import { configureMoneyBackend, assertMoneyLocalBackend } from './money-egress'
import { app } from 'electron'
import { spawn, type ChildProcess } from 'node:child_process'
import { createHmac, randomBytes } from 'node:crypto'
import { StringDecoder } from 'node:string_decoder'
import {signNativeAbsence,type NativeAbsenceVersion} from './native-launch-attestation'
import {signNativePrivacy} from './native-privacy'
import {signNativeWriting} from './native-writing'
import {signNativeRealtimeOutput} from './native-realtime-output'
import {signNativeRealtimeInputSource} from './native-realtime-input-source'
import {signNativeRealtimeCapability} from './native-realtime-capabilities'
import type {NativePrivacyMetadata} from '../shared/nativePrivacy'
import type {ControlledLaunchReceipt} from '../shared/controlledLaunch'
import { existsSync } from 'node:fs'
import { createServer } from 'node:net'
import { join } from 'node:path'
import { resolveFamilyDataPaths } from './data-paths'
import type { ModelConfig } from './model-settings'
import type { TutorExplanationConfig } from './tutor-explanation-settings'
import { validateTutorEndpoint, validateTutorModel } from '../shared/tutorExplanation'
import type { CloudProviderSnapshot } from './runtime-provider-refresh'
import type { PetAudioRuntimeConfig } from '../shared/petAudioSettings'
import { currentPetAudioTariff } from '../shared/petAudioTariffs'
import { readPetCatalog, readPetCatalogControl } from './pet-audio-catalog'
import type { CloudAsrConfig } from './asr-settings'
import type { VoiceConfig } from './voice-settings'
import type { ReportDeliverySecrets } from './report-delivery-secrets'

export const LOCAL_SERVICE_HEALTH_POLL_INTERVAL_MS = 250
export const LOCAL_SERVICE_HEALTH_TIMEOUT_MS = 60_000
const WINDOWS_PACKAGED_HEALTH_TIMEOUT_MS = 240_000
const LOCAL_SERVICE_HEALTH_REQUEST_TIMEOUT_MS = 5_000
export const LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS = 15_000
const LOCAL_SERVICE_CLOSE_ACK_TIMEOUT_MS = 1_000
export const LOCAL_SERVICE_RECOVERY_BASE_DELAY_MS = 1_000
export const LOCAL_SERVICE_RECOVERY_MAX_DELAY_MS = 30_000
export const LOCAL_SERVICE_RECOVERY_WINDOW_MS = 5 * 60_000
export const LOCAL_SERVICE_RECOVERY_MAX_ATTEMPTS = 5

export function localServiceHealthTimeout(options: {
  platform: NodeJS.Platform
  packaged: boolean
  managed: boolean
  rotationAuthOnly: boolean
}): number {
  // The installed Windows worker can spend the initial minute unpacking its
  // executable and backing up an older family database before binding HTTP.
  return options.platform === 'win32' && options.packaged && options.managed && !options.rotationAuthOnly
    ? WINDOWS_PACKAGED_HEALTH_TIMEOUT_MS
    : LOCAL_SERVICE_HEALTH_TIMEOUT_MS
}

export type LocalServiceRuntime = {
  runtimeEpoch?: number
  dataScope?: 'trial' | 'formal'
  rotationAuthOnly?: boolean
  rotationKeyUpdatePending?: boolean
  localApiUrl: string
  sessionToken: string
  status: 'starting' | 'ready' | 'error' | 'stopped'
  error?: string
  recoveryAttempt?: number
  nextRetryAt?: string
}

export type LocalServiceDiagnosticEvent = {
  event: string
  level?: 'debug' | 'info' | 'warn' | 'error'
  message?: string
  correlationId?: string
  details?: unknown
}

export function classifyLocalServiceOutputLevel(
  line: string,
  fallbackLevel: LocalServiceDiagnosticEvent['level']
): LocalServiceDiagnosticEvent['level'] {
  const match = /^(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s/.exec(line.trimStart())
  if (!match) return fallbackLevel
  if (match[1] === 'DEBUG') return 'debug'
  if (match[1] === 'INFO') return 'info'
  if (match[1] === 'WARNING') return 'warn'
  return 'error'
}

/**
 * The desktop process can be launched from a developer shell or an installer
 * environment.  Neither is an authority to pass provider credentials into the
 * local Python agent: credentials must originate from the encrypted desktop
 * settings currently loaded by this manager.
 */
const LOCAL_AGENT_SECRET_PREFIXES = ['HSC_PET_AUDIO_', 'HSC_MODEL_', 'HSC_TUTOR_EXPLANATION_', 'HSC_FALLBACK_', 'HSC_ASR_', 'HSC_TTS_', 'HSC_REPORT_DELIVERY_', 'HSC_DOCUMENT_VISION_', 'HSC_NATIVE_']
const LOCAL_AGENT_PROVIDER_SECRET_NAMES = new Set([
  'DASHSCOPE_API_KEY', 'QWEN_API_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY',
  'ANTHROPIC_API_KEY', 'AZURE_OPENAI_API_KEY', 'GOOGLE_API_KEY', 'GEMINI_API_KEY',
  'MISTRAL_API_KEY', 'COHERE_API_KEY', 'GROQ_API_KEY', 'TOGETHER_API_KEY',
  'SILICONFLOW_API_KEY', 'MOONSHOT_API_KEY', 'ZHIPUAI_API_KEY', 'BAIDU_API_KEY',
  'VOLCENGINE_API_KEY', 'ARK_API_KEY', 'MINIMAX_API_KEY', 'XAI_API_KEY',
  'PERPLEXITY_API_KEY', 'OPENROUTER_API_KEY', 'REPLICATE_API_TOKEN',
  'FIREWORKS_API_KEY', 'NVIDIA_API_KEY', 'HF_TOKEN', 'HUGGINGFACEHUB_API_TOKEN',
  'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
  'AZURE_CLIENT_SECRET', 'GOOGLE_APPLICATION_CREDENTIALS',
  'TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY',
  'ALIYUN_ACCESS_KEY_ID', 'ALIYUN_ACCESS_KEY_SECRET', 'COSYVOICE_API_KEY'
])

/**
 * This stays structural so the local-service boundary can be shared with the
 * parent-report safe-storage integration without importing its Electron-only
 * store into builds where that feature is not present yet.
 */
export type LocalServiceChildEnvironmentValues = {
  sessionToken: string
  nativeLaunchAttestationKey?:string
  dataEncryptionKey: string
  dataDir: string
  onboardingProfileDir?: string
  databasePath: string
  localModelDir: string
  atomicGraphDbPath?: string
  modelConfig: ModelConfig | null
  tutorExplanationConfig?: TutorExplanationConfig | null
  petAudioConfig?: PetAudioRuntimeConfig | null
  asrConfig?: CloudAsrConfig | null
  documentVisionConfig?: DocumentVisionConfig
  voiceConfig: VoiceConfig | null
  reportDeliverySecrets?: {
    smtpPassword?: string
    wecomWebhook?: string
  }
}

export type DocumentVisionConfig = {
  enabled?: boolean
  model?: 'qwen3-vl-plus' | 'qwen3-vl-flash'
  timeoutSeconds?: number
  maxOutputTokens?: number
  dailyRequestLimit?: number
  dailyTokenBudget?: number
  minIntervalSeconds?: number
}

export function deriveDocumentVisionEnvironment(
  asr: CloudAsrConfig | null | undefined,
  config: DocumentVisionConfig = {}
): NodeJS.ProcessEnv {
  const baseUrl = asr?.baseUrl.trim().replace(/\/+$/, '')
  const official = /^https:\/\/(?:dashscope\.aliyuncs\.com|dashscope-intl\.aliyuncs\.com|[a-z0-9][a-z0-9-]{0,62}\.(?:cn-beijing|ap-southeast-1|ap-northeast-1|us-east-1)\.maas\.aliyuncs\.com)\/compatible-mode\/v1$/
  if (config.enabled === false || asr?.provider !== 'dashscope' || !asr.apiKey.trim() || !baseUrl || !official.test(baseUrl)) {
    return { HSC_DOCUMENT_VISION_ENABLED: '0' }
  }
  // This family has authorized insufficient-OCR fallback, never full-frame upload.
  // No new key, endpoint override, region migration, or text-model fallback.
  return {
    HSC_DOCUMENT_VISION_ENABLED: '1',
    HSC_DOCUMENT_VISION_CREDENTIAL_SOURCE: 'asr_same_origin',
    ...(asr.documentVisionApiKey ? { HSC_DOCUMENT_VISION_API_KEY: asr.documentVisionApiKey } : {}),
    HSC_DOCUMENT_VISION_BASE_URL: baseUrl,
    HSC_DOCUMENT_VISION_MODEL: config.model ?? 'qwen3-vl-flash',
    HSC_DOCUMENT_VISION_TIMEOUT_SECONDS: String(config.timeoutSeconds ?? 60),
    HSC_DOCUMENT_VISION_MAX_OUTPUT_TOKENS: String(config.maxOutputTokens ?? 4096),
    HSC_DOCUMENT_VISION_DAILY_REQUEST_LIMIT: String(config.dailyRequestLimit ?? 0),
    HSC_DOCUMENT_VISION_DAILY_TOKEN_BUDGET: String(config.dailyTokenBudget ?? 0),
    HSC_DOCUMENT_VISION_MIN_INTERVAL_SECONDS: String(config.minIntervalSeconds ?? 2)
  }
}

export function sanitizeLocalServiceEnvironment(environment: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const sanitized = { ...environment }
  for (const name of Object.keys(sanitized)) {
    // Windows environment names are case-insensitive; a differently cased
    // inherited alias must not win over the desktop-owned configuration.
    const upper = name.toUpperCase()
    if (['HSC_VOICE_ONBOARDING_PROFILE_DIR','HSC_VOICE_INTENT_MODEL_NAME','HSC_VOICE_INTENT_API_KEY','HSC_SESSION_TOKEN','HSC_DATA_ENCRYPTION_KEY'].includes(upper)
      || LOCAL_AGENT_SECRET_PREFIXES.some((prefix) => upper.startsWith(prefix)) || LOCAL_AGENT_PROVIDER_SECRET_NAMES.has(upper)) delete sanitized[name]
  }
  return sanitized
}

export function derivePetAudioEnvironment(config: PetAudioRuntimeConfig | null | undefined): NodeJS.ProcessEnv {
  if (!config?.enabled) return {}
  let selected
  try {
    if (!config.control || JSON.stringify(readPetCatalogControl(config.control.file)) !== JSON.stringify(config.control)) return {}
    if (config.catalog) selected = readPetCatalog(config.catalog.file, config.catalog.sha256).tariff
  } catch { return {} }
  const tariff = currentPetAudioTariff(config.tariffId, config.baseUrl, config.model, Date.now(), selected)
  if (!tariff || config.purpose !== 'pet_original_audio_formative_v1' || !config.apiKey
    || !/^[A-Za-z0-9-]{8,100}$/.test(config.actionId)) return {}
  return { HSC_PET_AUDIO_ENABLED: 'true', HSC_PET_AUDIO_API_KEY: config.apiKey,
    HSC_PET_AUDIO_BASE_URL: config.baseUrl, HSC_PET_AUDIO_MODEL: tariff.model,
    HSC_PET_AUDIO_CATALOG_ID: tariff.id, HSC_PET_AUDIO_TARIFF_VERSION: tariff.id,
    HSC_PET_AUDIO_CONFIGURATION_ID: config.actionId,
    HSC_PET_AUDIO_CONTROL_FILE: config.control.file, HSC_PET_AUDIO_CONTROL_STATE: config.control.state,
    HSC_PET_AUDIO_INPUT_SECOND_MICROS: String(tariff.inputSecondMicros),
    HSC_PET_AUDIO_INPUT_TEXT_TOKEN_MICROS: String(tariff.inputTextTokenMicros),
    HSC_PET_AUDIO_OUTPUT_TOKEN_MICROS: String(tariff.outputTokenMicros),
    ...(config.catalog ? { HSC_PET_AUDIO_CATALOG_FILE: config.catalog.file, HSC_PET_AUDIO_CATALOG_SHA256: config.catalog.sha256 } : {}) }
}

export function buildLocalServiceChildEnvironment(
  environment: NodeJS.ProcessEnv,
  values: LocalServiceChildEnvironmentValues
): NodeJS.ProcessEnv {
  let explanation: { apiKey: string; baseUrl: string; model: string } | null = null
  try {
    const config = values.tutorExplanationConfig
    if (config?.apiKey && /^[!-~]{16,512}$/.test(config.apiKey) && config.baseUrl && config.model) explanation = { apiKey: config.apiKey, baseUrl: validateTutorEndpoint(config.baseUrl), model: validateTutorModel(config.model) }
  } catch { /* Invalid/missing v1 route never sends a key or falls back. */ }
  return {
    ...sanitizeLocalServiceEnvironment(environment),
    ...derivePetAudioEnvironment(values.petAudioConfig),
    HSC_SESSION_TOKEN: values.sessionToken,
    ...(values.nativeLaunchAttestationKey?{HSC_NATIVE_LAUNCH_ATTESTATION_KEY:values.nativeLaunchAttestationKey}:{}),
    HSC_DATA_ENCRYPTION_KEY: values.dataEncryptionKey,
    HSC_DATA_DIR: values.dataDir,
    ...(values.onboardingProfileDir ? { HSC_VOICE_ONBOARDING_PROFILE_DIR: values.onboardingProfileDir } : {}),
    HSC_DATABASE_PATH: values.databasePath,
    ...(explanation ? { HSC_TUTOR_EXPLANATION_API_KEY: explanation.apiKey, HSC_TUTOR_EXPLANATION_BASE_URL: explanation.baseUrl, HSC_TUTOR_EXPLANATION_MODEL: explanation.model } : {}),
    ...(values.modelConfig ? {
      HSC_MODEL_PROVIDER: values.modelConfig.provider,
      HSC_MODEL_API_KEY: values.modelConfig.apiKey,
      HSC_MODEL_BASE_URL: values.modelConfig.baseUrl,
      HSC_MODEL_NAME: values.modelConfig.model,
      HSC_VOICE_INTENT_MODEL_NAME: values.modelConfig.voiceIntentModel,
      ...(values.modelConfig.voiceIntentApiKey ? { HSC_VOICE_INTENT_API_KEY: values.modelConfig.voiceIntentApiKey } : {}),
      ...(values.modelConfig.fallback ? {
        HSC_FALLBACK_MODEL_PROVIDER: values.modelConfig.fallback.provider,
        HSC_FALLBACK_MODEL_API_KEY: values.modelConfig.fallback.apiKey,
        HSC_FALLBACK_MODEL_BASE_URL: values.modelConfig.fallback.baseUrl,
        HSC_FALLBACK_MODEL_NAME: values.modelConfig.fallback.model
      } : {})
    } : {}),
    ...(values.asrConfig ? {
      HSC_ASR_PROVIDER: values.asrConfig.provider,
      HSC_ASR_API_KEY: values.asrConfig.apiKey,
      HSC_ASR_BASE_URL: values.asrConfig.baseUrl,
      HSC_ASR_MODEL: values.asrConfig.model,
    } : {}),
    ...deriveDocumentVisionEnvironment(values.asrConfig, values.documentVisionConfig),
    ...(values.voiceConfig ? {
      HSC_TTS_PROVIDER: values.voiceConfig.provider,
      HSC_TTS_RELAY_URL: values.voiceConfig.relayUrl,
      HSC_TTS_API_KEY: values.voiceConfig.apiKey,
      HSC_TTS_VOICE_ID: values.voiceConfig.voiceId,
      HSC_TTS_MODEL: values.voiceConfig.model ?? '',
      HSC_TTS_WORKSPACE_ID: values.voiceConfig.workspaceId ?? '',
      HSC_TTS_REGION: values.voiceConfig.region ?? ''
    } : {}),
    // Report-delivery credentials are deliberately never inherited.  The
    // parent-report integration may supply only the values decrypted from its
    // current OS safe-storage record through this explicit boundary.
    ...(values.reportDeliverySecrets?.smtpPassword ? {
      HSC_REPORT_DELIVERY_EMAIL_PASSWORD: values.reportDeliverySecrets.smtpPassword
    } : {}),
    ...(values.reportDeliverySecrets?.wecomWebhook ? {
      HSC_REPORT_DELIVERY_WECOM_WEBHOOK: values.reportDeliverySecrets.wecomWebhook
    } : {}),
    HSC_LOCAL_MODEL_DIR: values.localModelDir,
    ...(values.atomicGraphDbPath ? { HSC_ATOMIC_GRAPH_DB: values.atomicGraphDbPath } : {})
  }
}

type CloudProviderIdentity = Readonly<{ child: ChildProcess | null; epoch: number; revision: number }>

type ChildExitFact = { code: number | null; signal: NodeJS.Signals | null }
type ChildClosure = {
  child: ChildProcess
  exit: ChildExitFact | null
  close: ChildExitFact | null
  shutdownRequested: boolean
  forceRequested: boolean
  recoverOnClose: boolean
  listeners: Set<() => void>
}

export class LocalServiceManager {
  // Main/backend only; never included in RuntimeConfig or any IPC getter.
  private nativeLaunchAttestationKey:string|undefined
  signPilotProvision(bootstrapId: string, recipient: string): string {
    if (!this.nativeLaunchAttestationKey) throw new Error('PILOT_PROVISION_KEY_UNAVAILABLE')
    return createHmac('sha256', this.nativeLaunchAttestationKey).update(`hsc-pilot-diagnostic-consent-v1\n${bootstrapId}\n${recipient}`).digest('hex')
  }
  signNativeLaunchAbsence(binding:ControlledLaunchReceipt,proof:string,version:NativeAbsenceVersion):string{
    return signNativeAbsence(this.nativeLaunchAttestationKey,binding,proof,version)
  }
  signNativePrivacyPermit(metadata:NativePrivacyMetadata):Record<string,unknown>{return signNativePrivacy(this.nativeLaunchAttestationKey,metadata)}
  signNativeWriting(input:import('../shared/nativeWriting').NativeWritingInput,issuedAt:string):string{return signNativeWriting(this.nativeLaunchAttestationKey,input,issuedAt)}
  signNativeRealtimeOutput(command:import('../shared/nativeRealtimeOutput').NativeRealtimeOutputCommand):string{return signNativeRealtimeOutput(this.nativeLaunchAttestationKey,command)}
  signNativeRealtimeInputSource(command:import('../shared/nativeRealtimeInputSource').NativeRealtimeInputSourceCommand):string{return signNativeRealtimeInputSource(this.nativeLaunchAttestationKey,command)}
  signNativeRealtimeCapability(proof:import('../shared/nativeRealtimeCapability').NativeRealtimeCapabilityProof):string{return signNativeRealtimeCapability(this.nativeLaunchAttestationKey,proof)}
  private child: ChildProcess | null = null
  private childClosure: ChildClosure | null = null
  private stopPromise: Promise<void> | null = null
  private runtime: LocalServiceRuntime = {
    localApiUrl: '',
    sessionToken: '',
    status: 'stopped'
  }
  private stopping = false
  private modelConfig: ModelConfig | null = null
  private tutorExplanationConfig: TutorExplanationConfig | null = null
  private petAudioConfig: PetAudioRuntimeConfig | null = null
  private activePetAudioActionId: string | null | undefined
  private asrConfig: CloudAsrConfig | null = null
  private documentVisionConfig: DocumentVisionConfig = {}
  private voiceConfig: VoiceConfig | null = null
  private reportDeliverySecrets: ReportDeliverySecrets = {}
  private dataEncryptionKey: string | null = null
  private restartPromise: Promise<void> | null = null
  private startPromise: Promise<void> | null = null
  private desiredRunning = false
  private protectedStartupFailure = false
  private rotationAuthOnly = false
  private rotationKeyUpdatePending = false
  private rotationRecoveryGuard = false
  private recoveryTimer: ReturnType<typeof setTimeout> | null = null
  private recoveryFailures: number[] = []
  private startupEpoch = 0
  private healthAbort: AbortController | null = null
  private cloudProviderRevision = 0
  private cloudProviderStops = 0
  private appliedCloudProvider: CloudProviderIdentity | null = null

  private cloudProviderRefreshBusy(): boolean {
    return Boolean(this.restartPromise || this.startPromise || this.recoveryTimer || this.healthAbort
      || this.cloudProviderStops || this.runtime.status === 'starting')
  }

  admitCloudProviderRefresh(): CloudProviderIdentity {
    if (process.env.HSC_LOCAL_API_URL || this.rotationRecoveryGuard || this.rotationAuthOnly || this.protectedStartupFailure || this.cloudProviderRefreshBusy()) {
      throw new Error('CLOUD_PROVIDER_REFRESH_BUSY')
    }
    return Object.freeze({ child: this.child, epoch: this.startupEpoch, revision: this.cloudProviderRevision })
  }

  async restartCloudProviderSnapshot(snapshot: CloudProviderSnapshot, admission: CloudProviderIdentity): Promise<CloudProviderIdentity> {
    const current = this.admitCloudProviderRefresh()
    if (current.child !== admission.child || current.epoch !== admission.epoch || current.revision !== admission.revision) {
      throw new Error('CLOUD_PROVIDER_RUNTIME_CHANGED')
    }
    // Commit all three caches and start a fresh restart synchronously. No old
    // in-flight start/restart/recovery can be credited with these values.
    this.modelConfig = snapshot.modelConfig ? { ...snapshot.modelConfig, fallback: snapshot.modelConfig.fallback ? { ...snapshot.modelConfig.fallback } : undefined } : null
    this.asrConfig = snapshot.asrConfig ? { ...snapshot.asrConfig } : null
    this.voiceConfig = snapshot.voiceConfig ? { ...snapshot.voiceConfig } : null
    if ('tutorExplanationConfig' in snapshot) this.tutorExplanationConfig = snapshot.tutorExplanationConfig ? { ...snapshot.tutorExplanationConfig } : null
    const revision = ++this.cloudProviderRevision
    await this.restart()
    const applied = this.appliedCloudProvider
    if (!applied || applied.revision !== revision || !this.cloudProviderSnapshotApplied(applied)) {
      throw new Error('CLOUD_PROVIDER_REFRESH_NOT_APPLIED')
    }
    return applied
  }

  cloudProviderSnapshotApplied(token: CloudProviderIdentity): boolean {
    return token === this.appliedCloudProvider && token.child !== null && token.child === this.child
      && token.child.exitCode === null && token.epoch === this.startupEpoch
      && token.revision === this.cloudProviderRevision && this.runtime.status === 'ready'
      && !process.env.HSC_LOCAL_API_URL && !this.protectedStartupFailure && !this.cloudProviderRefreshBusy()
  }

  getCloudProviderRevision(): number { return this.cloudProviderRevision }

  cloudProviderRevisionApplied(revision: number): boolean {
    return Boolean(this.appliedCloudProvider && this.appliedCloudProvider.revision === revision
      && this.cloudProviderSnapshotApplied(this.appliedCloudProvider))
  }

  constructor(private readonly diagnostic?: (event: LocalServiceDiagnosticEvent) => void) {
    configureMoneyBackend({ runtime: () => this.getRuntime(), seed: () => this.nativeLaunchAttestationKey })
  }

  setModelConfig(config: ModelConfig | null): void {
    this.modelConfig = config ? { ...config, fallback: config.fallback ? { ...config.fallback } : undefined } : null
    this.cloudProviderRevision += 1
  }
  setTutorExplanationConfig(config: TutorExplanationConfig | null): void { this.tutorExplanationConfig = config ? { ...config } : null; this.cloudProviderRevision += 1 }

  /** Apply only on a quiescent owned runtime; never credit a prior launch. */
  async applyTutorExplanationConfig(config: TutorExplanationConfig): Promise<void> {
    try {
      this.admitCloudProviderRefresh()
    } catch (error) {
      // The durable setting has already changed. Cancel any old launch and
      // close its process before reporting failure; an old endpoint must not
      // remain silently active while the parent believes a switch was made.
      this.setTutorExplanationConfig(config)
      const prior = this.restartPromise
      await this.stop()
      await prior?.catch(() => undefined)
      await this.stop()
      throw error
    }
    this.setTutorExplanationConfig(config)
    const revision = this.cloudProviderRevision
    try {
      await this.restart()
      if (!this.cloudProviderRevisionApplied(revision)) throw new Error('CLOUD_PROVIDER_REFRESH_NOT_APPLIED')
    } catch (error) {
      // A pending closure is already tracked; a second wait cannot prove that
      // it stopped and needlessly blocks the parent's failure receipt.
      if ((error as { code?: unknown })?.code !== 'LOCAL_SERVICE_SHUTDOWN_PENDING') await this.stop()
      throw error
    }
  }
  setAsrConfig(config: CloudAsrConfig | null): void { this.asrConfig = config ? { ...config } : null; this.cloudProviderRevision += 1 }
  setPetAudioConfig(config: PetAudioRuntimeConfig | null): void { this.petAudioConfig = config ? { ...config } : null }
  revokePetAudioConfig(): void { this.petAudioConfig = null; this.activePetAudioActionId = null }
  petAudioConfigApplied(actionId: string | null): boolean {
    return this.runtime.status === 'ready' && this.activePetAudioActionId === actionId
  }
  setDocumentVisionConfig(config: DocumentVisionConfig): void { this.documentVisionConfig = { ...config } }
  setVoiceConfig(config: VoiceConfig | null): void { this.voiceConfig = config ? { ...config } : null; this.cloudProviderRevision += 1 }
  setReportDeliverySecrets(secrets: ReportDeliverySecrets): void { this.reportDeliverySecrets = { ...secrets } }

  setDataEncryptionKey(encodedKey: string): void {
    this.dataEncryptionKey = encodedKey
  }

  reportProtectedStartupFailure(reason: 'storage' | 'media_key_recovery' | 'rotation_model_unclassified' | 'tutor_recovery_marker' = 'storage'): void {
    if (this.runtime.status === 'ready' || this.runtime.status === 'error') return
    this.desiredRunning = false
    this.invalidateStartup()
    this.protectedStartupFailure = true
    this.cancelRecoveryTimer()
    this.runtime = { ...this.runtime, status: 'error', nextRetryAt: undefined, recoveryAttempt: undefined,
      error: reason === 'tutor_recovery_marker'
        ? `讲题配置恢复正在占用或上次恢复未完成，学习服务未启动。请先关闭所有学习伙伴及相关后台进程，再打开本机数据目录 ${app.getPath('userData')}，仅删除其中的 tutor-explanation-settings.lock.recovery 空目录，然后重启。请保留其他文件，尤其是 .pending 文件。`
        : reason === 'rotation_model_unclassified'
        ? '无法确认现有模型是否属于千问，密钥更新已暂停，学习服务未启动。原有配置和资料已保留；请联系维护者核实模型，处理后重新启动。'
        : reason === 'media_key_recovery'
        ? '本地媒体密钥缺失、损坏或无法安全保存，学习服务未启动。请保留整个档案并恢复原有受保护密钥后重启；可移植备份应在新的隔离档案中恢复。系统不会自动更换密钥，也不会删除原资料。'
        : '受保护的本地配置无法读取，学习服务未启动。请保留现有资料并联系维护者，系统不会自动更换密钥。' }
    this.emitDiagnostic('protected_startup_failed', undefined, 'error', this.runtime.error)
  }

  getRuntime(): LocalServiceRuntime {
    return { ...this.runtime, runtimeEpoch: this.startupEpoch, dataScope: process.env.HSC_RUNTIME_MODE === 'trial' ? 'trial' : 'formal', ...(this.rotationAuthOnly ? {rotationAuthOnly:true} : {}), ...(this.rotationKeyUpdatePending ? {rotationKeyUpdatePending:true} : {}) }
  }

  markRotationKeyUpdatePending(value = true): void { this.rotationKeyUpdatePending = value }
  setRotationRecoveryGuard(value: boolean): void { this.rotationRecoveryGuard = value }

  async startRotationAuthOnly(): Promise<void> {
    if (this.child || this.startPromise || this.desiredRunning || this.protectedStartupFailure || process.env.HSC_LOCAL_API_URL) throw new Error('ROTATION_AUTH_UNAVAILABLE')
    this.rotationAuthOnly = true
    await this.start()
  }

  async stopRotationAuthOnly(): Promise<void> {
    if (!this.rotationAuthOnly) throw new Error('ROTATION_AUTH_UNAVAILABLE')
    await this.stop()
    if (this.childClosure && !this.childClosureConfirmed(this.childClosure)) throw this.childClosureError()
    this.rotationAuthOnly = false
  }

  async restart(): Promise<void> {
    if (this.protectedStartupFailure) throw new Error(this.runtime.error)
    if (this.restartPromise) return this.restartPromise
    this.emitDiagnostic('restart_requested')
    this.recoveryFailures = []
    this.restartPromise = this.stop()
      .catch(error => {
        const acknowledge = this.captureAbnormalShutdownAcknowledgement()
        if ((error as { code?: unknown })?.code !== 'LOCAL_SERVICE_SHUTDOWN_ABNORMAL'
          || !acknowledge?.()) throw error
        // An explicit restart may recover an already closed abnormal owner.
        // No normal-save receipt is issued and the DB's unclean marker stays.
        this.emitDiagnostic('restart_after_abnormal_shutdown', { error_code: 'LOCAL_SERVICE_SHUTDOWN_ABNORMAL' }, 'warn')
      })
      .then(() => this.start())
      .finally(() => { this.restartPromise = null })
    return this.restartPromise
  }

  async start(): Promise<void> {
    if (this.protectedStartupFailure) throw new Error(this.runtime.error)
    if (this.startPromise) return this.startPromise
    if (this.childClosure && !this.childClosureConfirmed(this.childClosure)) {
      throw this.childClosureError()
    }
    this.desiredRunning = true
    this.cancelRecoveryTimer()
    this.emitDiagnostic('start_requested')
    const epoch = ++this.startupEpoch
    this.startPromise = this.startInternal(epoch).finally(() => {
      this.startPromise = null
      this.scheduleClosedChildRecovery()
    })
    return this.startPromise
  }

  private async startInternal(epoch: number): Promise<void> {
    const externalUrl = process.env.HSC_LOCAL_API_URL
    assertMoneyLocalBackend(externalUrl)
    if (externalUrl) {
      if (process.env.HSC_RUNTIME_MODE === 'trial') throw new Error('TRIAL_EXTERNAL_RUNTIME_FORBIDDEN')
      this.nativeLaunchAttestationKey=!app.isPackaged?process.env.HSC_NATIVE_LAUNCH_ATTESTATION_KEY:undefined
      this.runtime = {
        localApiUrl: externalUrl,
        sessionToken: process.env.HSC_SESSION_TOKEN ?? '',
        status: 'starting'
      }
      await this.waitUntilHealthy(epoch)
      this.assertCurrentStartup(epoch)
      this.markReady()
      this.emitDiagnostic('external_service_ready', { localApiUrl: externalUrl })
      return
    }

    const port = await findAvailablePort()
    this.assertCurrentStartup(epoch)
    const sessionToken = randomBytes(32).toString('hex')
    this.nativeLaunchAttestationKey=randomBytes(32).toString('hex')
    this.runtime = {
      localApiUrl: `http://127.0.0.1:${port}`,
      sessionToken,
      status: 'starting'
    }

    const command = resolveServiceCommand(port)
    if (!existsSync(command.executable)) {
      this.runtime.status = 'error'
      this.runtime.error = `找不到本地学习服务运行时：${command.executable}`
      this.emitDiagnostic('runtime_missing', { executable: command.executable }, 'error', this.runtime.error)
      throw new Error(this.runtime.error)
    }
    if (!this.dataEncryptionKey) {
      this.runtime.status = 'error'
      this.runtime.error = '本地媒体加密密钥尚未就绪'
      this.emitDiagnostic('data_key_unavailable', undefined, 'error', this.runtime.error)
      throw new Error(this.runtime.error)
    }

    this.stopping = false
    this.activePetAudioActionId = this.petAudioConfig?.actionId ?? null
    // A child can outlive a configuration mutation; retain its launch-time
    // secrets alongside current ones until this child's output is closed.
    const launchedSecrets = this.localServiceOutputSecrets()
    const redactPetStdout = this.createPetAudioLogRedactor(launchedSecrets)
    const redactPetStderr = this.createPetAudioLogRedactor(launchedSecrets)
    const { dataDir, databasePath } = resolveFamilyDataPaths(app.getPath('userData'))
    const launchedCloudProviderRevision = this.cloudProviderRevision
    const child = spawn(command.executable, this.rotationAuthOnly ? [...command.args, '--rotation-auth-only'] : command.args, {
      cwd: resolveServiceWorkingDirectory(app.isPackaged, app.getAppPath(), process.resourcesPath),
      env: buildLocalServiceChildEnvironment(process.env, {
        sessionToken,
        nativeLaunchAttestationKey:this.nativeLaunchAttestationKey,
        dataEncryptionKey: this.dataEncryptionKey,
        dataDir,
        onboardingProfileDir: app.getPath('userData'),
        databasePath,
        modelConfig: this.rotationAuthOnly ? null : this.modelConfig,
        tutorExplanationConfig: this.rotationAuthOnly ? null : this.tutorExplanationConfig,
        petAudioConfig: this.rotationAuthOnly ? null : this.petAudioConfig,
        asrConfig: this.rotationAuthOnly ? null : this.asrConfig,
        documentVisionConfig: this.rotationAuthOnly ? undefined : this.documentVisionConfig,
        voiceConfig: this.rotationAuthOnly ? null : this.voiceConfig,
        reportDeliverySecrets: this.rotationAuthOnly ? {} : this.reportDeliverySecrets,
        localModelDir: app.isPackaged
          ? join(process.resourcesPath, 'local-models')
          // Development acceptance may point at an already approved read-only
          // model directory. Packaged builds always use their bundled assets.
          : process.env.HSC_LOCAL_MODEL_DIR ?? join(app.getAppPath(), 'services', 'local_agent', 'models'),
        atomicGraphDbPath: app.isPackaged
          ? join(process.resourcesPath, 'knowledge', 'atomic-knowledge.db')
          : process.env.HSC_ATOMIC_GRAPH_DB
      }),
      stdio: ['ignore', 'pipe', 'pipe']
    })

    this.childClosure = this.observeChildClosure(child)
    this.child = child
    this.emitDiagnostic('process_spawned', { processId: child.pid, executable: command.executable, argumentCount: command.args.length })
    const forwardStdout = (value: string): void => {
      const message = value.trim()
      if (!message) return
      console.info(`[local-agent] ${message}`)
      this.recordChildOutput(message, 'info')
    }
    const forwardStderr = (value: string): void => {
      const message = value.trim()
      if (!message) return
      console.error(`[local-agent] ${message}`)
      this.recordChildOutput(message, 'error')
    }
    child.stdout?.on('data', (data: Buffer) => forwardStdout(redactPetStdout.write(data)))
    child.stdout?.once('end', () => forwardStdout(redactPetStdout.end()))
    child.stdout?.once('close', () => forwardStdout(redactPetStdout.end()))
    child.stderr?.on('data', (data: Buffer) => forwardStderr(redactPetStderr.write(data)))
    child.stderr?.once('end', () => forwardStderr(redactPetStderr.end()))
    child.stderr?.once('close', () => forwardStderr(redactPetStderr.end()))
    child.on('exit', (code, signal) => {
      const current = this.child === child
      if (current) this.child = null
      this.emitDiagnostic('process_exited', { exitCode: code, exitSignal: signal, expected: this.stopping || !current }, this.stopping || !current ? 'info' : 'error')
      if (current && !this.stopping) {
        this.invalidateStartup()
        this.scheduleRecovery(`本地学习服务意外退出（代码 ${code ?? 'unknown'}）`)
      }
    })

    try {
      await this.waitUntilHealthy(epoch, child)
      this.assertCurrentStartup(epoch, child)
      this.appliedCloudProvider = Object.freeze({ child, epoch, revision: launchedCloudProviderRevision })
      this.markReady()
    } catch (error) {
      if (epoch !== this.startupEpoch || !this.desiredRunning) throw error
      this.runtime.status = 'error'
      this.runtime.error = error instanceof Error ? error.message : '本地学习服务启动失败'
      this.emitDiagnostic('startup_failed', { error }, 'error', this.runtime.error)
      await this.stopChild(true)
      if (epoch === this.startupEpoch && this.desiredRunning) this.scheduleRecovery(this.runtime.error)
      throw error
    }
  }

  async stop(): Promise<void> {
    if (this.stopPromise) return this.stopPromise
    const request = this.stopInternal()
    this.stopPromise = request
    try { await request }
    finally { if (this.stopPromise === request) this.stopPromise = null }
  }

  private async stopInternal(): Promise<void> {
    this.cloudProviderStops += 1
    try {
      const starting = this.healthAbort ? this.startPromise : null
      this.desiredRunning = false
      this.invalidateStartup()
      this.cancelRecoveryTimer()
      this.emitDiagnostic('stop_requested')
      await this.stopChild(false)
      await starting?.catch(() => undefined)
    } finally { this.cloudProviderStops -= 1 }
  }

  private observeChildClosure(child: ChildProcess): ChildClosure {
    const state: ChildClosure = { child, exit: null, close: null,
      shutdownRequested: false, forceRequested: false, recoverOnClose: false, listeners: new Set() }
    const changed = (): void => { for (const notify of state.listeners) notify() }
    child.once('exit', (code, signal) => {
      state.exit = { code, signal }
      if (this.desiredRunning && !this.stopping) state.recoverOnClose = true
      changed()
    })
    child.once('close', (code, signal) => {
      state.close = { code, signal }
      this.emitDiagnostic('process_closed', { exitCode: code, exitSignal: signal,
        expected: state.shutdownRequested || state.forceRequested || !this.desiredRunning })
      changed()
      this.scheduleClosedChildRecovery()
    })
    return state
  }

  private scheduleClosedChildRecovery(): void {
    const state = this.childClosure
    if (!state || !state.recoverOnClose || !this.childClosureConfirmed(state)
      || !this.desiredRunning || this.recoveryTimer || this.startPromise) return
    state.recoverOnClose = false
    this.scheduleRecovery('本地学习服务原进程已确认关闭，继续自动恢复。')
  }

  private childClosureConfirmed(state: ChildClosure): boolean {
    const exited = state.exit, closed = state.close
    return Boolean(exited && closed && exited.code === closed.code && exited.signal === closed.signal
      && ((typeof exited.code === 'number' && Number.isInteger(exited.code) && exited.signal === null)
        || (exited.code === null && typeof exited.signal === 'string')))
  }

  private waitForChildClosure(state: ChildClosure, timeout: number): Promise<boolean> {
    if (this.childClosureConfirmed(state)) return Promise.resolve(true)
    return new Promise((resolve) => {
      let timer: ReturnType<typeof setTimeout> | undefined
      const done = (confirmed: boolean): void => {
        if (timer) clearTimeout(timer)
        state.listeners.delete(check)
        resolve(confirmed)
      }
      const check = (): void => { if (this.childClosureConfirmed(state)) done(true) }
      state.listeners.add(check)
      timer = setTimeout(() => done(this.childClosureConfirmed(state)), timeout)
      check()
    })
  }

  private childClosureError(kind: 'pending' | 'abnormal' = 'pending'): Error {
    const message = kind === 'abnormal'
      ? '后台异常退出，本次保存尚未确认；下次启动需要检查恢复。'
      : '尚未确认后台保存并退出，请稍后重试。'
    this.runtime = { ...this.runtime, status: 'error', error: message }
    return Object.assign(new Error(message), { code: kind === 'abnormal'
      ? 'LOCAL_SERVICE_SHUTDOWN_ABNORMAL' : 'LOCAL_SERVICE_SHUTDOWN_PENDING' })
  }

  private childGracefulClosureConfirmed(state: ChildClosure): boolean {
    return this.childClosureConfirmed(state) && !state.forceRequested
      && state.exit?.code === 0 && state.exit.signal === null
  }

  captureAbnormalShutdownAcknowledgement(): (() => boolean) | null {
    const state = this.childClosure
    if (!state || this.desiredRunning || !this.childClosureConfirmed(state)
      || this.childGracefulClosureConfirmed(state)) return null
    // Only Main retains this capability. It is never sent to a renderer and
    // cannot acknowledge a replacement process or an unclosed original.
    return () => this.childClosure === state && !this.desiredRunning
      && this.childClosureConfirmed(state) && !this.childGracefulClosureConfirmed(state)
  }

  observeShutdownClosure(onClosed: (receipt: { graceful: boolean; isCurrent: () => boolean }) => void): (() => void) | null {
    const state = this.childClosure
    if (!state || this.desiredRunning || !state.shutdownRequested) return null
    let active = true
    const isCurrent = (): boolean => this.childClosure === state && !this.desiredRunning
      && this.childClosureConfirmed(state)
    const cancel = (): void => { active = false; state.listeners.delete(changed) }
    const changed = (): void => {
      if (!active) return
      if (this.childClosure !== state || this.desiredRunning) { cancel(); return }
      if (!this.childClosureConfirmed(state)) return
      cancel()
      onClosed({ graceful: this.childGracefulClosureConfirmed(state), isCurrent })
    }
    state.listeners.add(changed)
    changed()
    return cancel
  }

  private async stopChild(preserveDesiredState: boolean): Promise<void> {
    this.stopping = true
    const state = this.childClosure
    const epoch = this.startupEpoch
    if (state && !this.childClosureConfirmed(state)) {
      if (preserveDesiredState && this.desiredRunning) state.recoverOnClose = true
      // Retain the same process and its original listeners through unknown closure.
      // A retry waits for these facts; it cannot re-send or kill a replacement.
      this.child = state.child
      if (!state.shutdownRequested && !state.exit && !state.forceRequested) {
        state.shutdownRequested = true
        try {
          await fetch(`${this.runtime.localApiUrl}/v1/runtime/shutdown`, {
            method: 'POST',
            headers: this.runtime.sessionToken ? { 'X-HSC-Session': this.runtime.sessionToken } : {},
            signal: AbortSignal.timeout(1_000)
          })
        } catch {
          this.emitDiagnostic('graceful_shutdown_failed', undefined, 'warn')
        }
      }
      let confirmed = await this.waitForChildClosure(state, state.forceRequested ? LOCAL_SERVICE_CLOSE_ACK_TIMEOUT_MS : LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS)
      // A normal stop cancels startup recovery while both wait on this owner.
      // Recheck authority after the await; captured policy alone cannot kill it.
      if (!confirmed && preserveDesiredState && this.desiredRunning
        && epoch === this.startupEpoch && this.childClosure === state && this.child === state.child
        && !state.forceRequested && !state.exit) {
        state.forceRequested = true
        this.emitDiagnostic('force_stop', { processId: state.child.pid }, 'warn')
        try { state.child.kill('SIGKILL') } catch { /* Original closure facts still decide. */ }
        confirmed = await this.waitForChildClosure(state, LOCAL_SERVICE_CLOSE_ACK_TIMEOUT_MS)
      }
      if (!confirmed) { this.child = state.child; throw this.childClosureError() }
    }
    if (state && !preserveDesiredState && !this.childGracefulClosureConfirmed(state)) {
      throw this.childClosureError('abnormal')
    }
    if (state && this.child === state.child) this.child = null
    if (!preserveDesiredState) {
      this.runtime = { ...this.runtime, status: 'stopped', error: undefined,
        recoveryAttempt: undefined, nextRetryAt: undefined }
    }
  }

  private markReady(): void {
    this.runtime = {
      ...this.runtime,
      status: 'ready',
      error: undefined,
      recoveryAttempt: undefined,
      nextRetryAt: undefined
    }
    this.emitDiagnostic('service_ready', { localApiUrl: this.runtime.localApiUrl })
  }

  private scheduleRecovery(error: string): void {
    if (!this.desiredRunning || this.recoveryTimer) return
    const now = Date.now()
    this.recoveryFailures = this.recoveryFailures.filter((at) => now - at <= LOCAL_SERVICE_RECOVERY_WINDOW_MS)
    this.recoveryFailures.push(now)
    const attempt = this.recoveryFailures.length
    if (attempt > LOCAL_SERVICE_RECOVERY_MAX_ATTEMPTS) {
      this.runtime = {
        ...this.runtime,
        status: 'error',
        error: `${error}；自动恢复已连续失败 ${LOCAL_SERVICE_RECOVERY_MAX_ATTEMPTS} 次，请在家长中心检查运行环境后重试。`,
        recoveryAttempt: LOCAL_SERVICE_RECOVERY_MAX_ATTEMPTS,
        nextRetryAt: undefined
      }
      this.emitDiagnostic('recovery_exhausted', { attempts: LOCAL_SERVICE_RECOVERY_MAX_ATTEMPTS }, 'error', this.runtime.error)
      return
    }
    const delay = Math.min(
      LOCAL_SERVICE_RECOVERY_BASE_DELAY_MS * (2 ** (attempt - 1)),
      LOCAL_SERVICE_RECOVERY_MAX_DELAY_MS
    )
    const nextRetryAt = new Date(now + delay).toISOString()
    this.runtime = {
      ...this.runtime,
      status: 'starting',
      error: `${error}；正在自动恢复（第 ${attempt} 次）。`,
      recoveryAttempt: attempt,
      nextRetryAt
    }
    this.emitDiagnostic('recovery_scheduled', { attempt, delayMs: delay, nextRetryAt }, 'warn', error)
    this.recoveryTimer = setTimeout(() => {
      this.recoveryTimer = null
      if (!this.desiredRunning) return
      void this.start().catch(() => undefined)
    }, delay)
  }

  private cancelRecoveryTimer(): void {
    if (this.recoveryTimer) clearTimeout(this.recoveryTimer)
    this.recoveryTimer = null
  }

  private invalidateStartup(): void {
    this.appliedCloudProvider = null
    this.startupEpoch += 1
    this.healthAbort?.abort()
  }

  private assertCurrentStartup(epoch: number, child?: ChildProcess): void {
    if (!this.desiredRunning || epoch !== this.startupEpoch || child && (this.child !== child || child.exitCode !== null)) {
      throw new Error('本地学习服务启动已取消')
    }
  }

  private async waitUntilHealthy(epoch: number, child?: ChildProcess): Promise<void> {
    const timeout = localServiceHealthTimeout({
      platform: process.platform, packaged: app.isPackaged, managed: child !== undefined, rotationAuthOnly: this.rotationAuthOnly
    })
    const deadline = performance.now() + timeout
    const scope = new AbortController()
    this.healthAbort = scope
    const current = (): void => {
      this.assertCurrentStartup(epoch, child)
      if (scope.signal.aborted) throw new Error('本地学习服务启动已取消')
    }
    try {
      while (performance.now() < deadline) {
        current()
        const request = new AbortController()
        let timer: ReturnType<typeof setTimeout> | undefined
        let cancel = (): void => undefined
        try {
          const response = await Promise.race([
            Promise.resolve().then(() => {
              current()
              return fetch(`${this.runtime.localApiUrl}/v1/health`, {
                headers: this.runtime.sessionToken ? { 'X-HSC-Session': this.runtime.sessionToken } : {},
                signal: request.signal
              }).then(async response => {
                if (!response.ok) return undefined
                // Body reading is inside the existing bounded race: receiving
                // headers must not let a stalled service bypass the deadline.
                try { return { body: await response.json() as unknown } }
                catch { return { body: null } }
              })
            }).catch(() => undefined),
            new Promise<undefined>((resolve, reject) => {
              cancel = (): void => { request.abort(); reject(new Error('本地学习服务启动已取消')) }
              scope.signal.addEventListener('abort', cancel, { once: true })
              timer = setTimeout(() => { request.abort(); resolve(undefined) },
                Math.min(LOCAL_SERVICE_HEALTH_REQUEST_TIMEOUT_MS, Math.max(0, deadline - performance.now())))
            })
          ])
          current()
          if (performance.now() >= deadline) break
          if (response) {
            const body = response.body
            if (!body || typeof body !== 'object' || Array.isArray(body)
              || (body as Record<string, unknown>).build_component !== 'home-study-local-agent'
              || (body as Record<string, unknown>).build_version !== app.getVersion()) {
              throw new Error('本地学习服务与客户端版本不一致，请重新安装完整版本。')
            }
            if (((body as Record<string, unknown>).status === 'rotation_auth_only') !== this.rotationAuthOnly) throw new Error('ROTATION_AUTH_MODE_MISMATCH')
            if (this.rotationAuthOnly && (body as Record<string, unknown>).normal_business_ready !== false) throw new Error('ROTATION_AUTH_MODE_MISMATCH')
            return
          }
        } finally {
          if (timer) clearTimeout(timer)
          scope.signal.removeEventListener('abort', cancel)
          request.abort()
        }
        current()
        await new Promise<void>((resolve, reject) => {
          const cancelWait = (): void => { clearTimeout(timer); reject(new Error('本地学习服务启动已取消')) }
          const timer = setTimeout(() => { scope.signal.removeEventListener('abort', cancelWait); resolve() },
            Math.min(LOCAL_SERVICE_HEALTH_POLL_INTERVAL_MS, Math.max(0, deadline - performance.now())))
          scope.signal.addEventListener('abort', cancelWait, { once: true })
        })
      }
      current()
      throw new Error(`本地学习服务在${timeout / 1000}秒内没有就绪`)
    } finally {
      scope.abort()
      if (this.healthAbort === scope) this.healthAbort = null
    }
  }

  private localServiceOutputSecrets(): string[] {
    return [this.modelConfig?.apiKey, this.modelConfig?.voiceIntentApiKey, this.modelConfig?.fallback?.apiKey, this.tutorExplanationConfig?.apiKey, this.asrConfig?.apiKey, this.asrConfig?.documentVisionApiKey,
      this.voiceConfig?.apiKey, this.reportDeliverySecrets.smtpPassword, this.reportDeliverySecrets.wecomWebhook,
      this.petAudioConfig?.apiKey, this.dataEncryptionKey, this.runtime.sessionToken, this.nativeLaunchAttestationKey]
      .filter((secret): secret is string => typeof secret === 'string' && secret.length > 0)
  }

  private createPetAudioLogRedactor(launchedSecrets: string[]): { write: (data: Buffer) => string; end: () => string } {
    const decoder = new StringDecoder('utf8')
    let pending = '', ended = false, suppressedChunks = 0
    const process = (fragment: string, final: boolean): string => {
      const keys = [...new Set([...launchedSecrets, ...this.localServiceOutputSecrets()])].sort((a,b) => b.length - a.length)
      // A placeholder must not itself contain a secret or connect partial
      // secrets across a replacement boundary.
      const safeMarker = (candidates: string[]): string | undefined => candidates.find(marker => keys.every(key =>
        !marker.includes(key) && !key.includes(marker[0]) && !key.includes(marker[marker.length-1])))
      const marker = safeMarker(['[REDACTED]', '[PRIVATE]', '***', '###', '~~~'])
      const tailMarker = safeMarker(['[REDACTED_PARTIAL]', '[PRIVATE_TAIL]', '***', '###', '~~~'])
      if (!marker || !tailMarker) {
        if (pending || fragment) suppressedChunks += 1
        pending = ''
        return ''
      }
      let value = pending + fragment
      // Replace complete secrets first; this also protects overlaps where one
      // secret is a prefix or suffix of another secret.
      for (const key of keys) value = value.split(key).join(marker)
      let held = 0
      // Never forward a suffix that could be the beginning of a secret whose
      // remaining bytes may arrive in a later stdout/stderr chunk.
      for (const key of keys) for (let length = 1; length < key.length && length <= value.length; length++) {
        if (value.endsWith(key.slice(0, length))) held = Math.max(held, length)
      }
      pending = held ? value.slice(-held) : ''
      const output = held ? value.slice(0, -held) : value
      if (final && pending) { pending = ''; return output + tailMarker }
      return output
    }
    return {
      write: data => ended ? '' : process(decoder.write(data), false),
      end: () => {
        if (ended) return ''
        ended = true
        const tail = process(decoder.end(), true)
        if (suppressedChunks) {
          // The event contains only a count. If an unusual credential equals
          // a substring of the event name, use a short existing event instead.
          const keys = [...launchedSecrets, ...this.localServiceOutputSecrets()]
          const event = ['local_service.output_redaction_suppressed', 'stderr', 'stdout']
            .find(candidate => keys.every(key => !candidate.includes(key)))
          if (event) try { this.emitDiagnostic(event, {count:suppressedChunks}, 'warn') } catch { /* Output cleanup must finish. */ }
        }
        return tail
      },
    }
  }

  private emitDiagnostic(
    event: string,
    details?: unknown,
    level: LocalServiceDiagnosticEvent['level'] = 'info',
    message?: string
  ): void {
    this.diagnostic?.({ event, level, message, details })
  }

  private recordChildOutput(message: string, fallbackLevel: LocalServiceDiagnosticEvent['level']): void {
    for (const line of message.split(/\r?\n/).filter(Boolean)) {
      if (!line.startsWith('HSC_DIAGNOSTIC ')) {
        this.emitDiagnostic(
          fallbackLevel === 'error' ? 'stderr' : 'stdout',
          undefined,
          classifyLocalServiceOutputLevel(line, fallbackLevel),
          line
        )
        continue
      }
      try {
        const parsed = JSON.parse(line.slice('HSC_DIAGNOSTIC '.length)) as LocalServiceDiagnosticEvent
        this.diagnostic?.({
          event: typeof parsed.event === 'string' ? parsed.event : 'structured_event',
          level: parsed.level ?? fallbackLevel,
          message: typeof parsed.message === 'string' ? parsed.message : undefined,
          correlationId: typeof parsed.correlationId === 'string' ? parsed.correlationId : undefined,
          details: parsed.details
        })
      } catch {
        this.emitDiagnostic('structured_output_invalid', undefined, 'warn', line)
      }
    }
  }
}

export function resolveServiceWorkingDirectory(isPackaged: boolean, appPath: string, resourcesPath: string): string {
  return isPackaged ? resourcesPath : appPath
}

async function findAvailablePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      if (!address || typeof address === 'string') {
        server.close()
        reject(new Error('无法分配本地服务端口'))
        return
      }
      server.close((error) => (error ? reject(error) : resolve(address.port)))
    })
  })
}

function resolveServiceCommand(port: number): { executable: string; args: string[] } {
  if (app.isPackaged) {
    return {
      executable: join(process.resourcesPath, 'local-agent', 'home-study-local-agent.exe'),
      args: ['--host', '127.0.0.1', '--port', String(port)]
    }
  }

  // This is intentionally development-only. It lets an isolated worktree run
  // the real local agent from an explicit existing interpreter without adding
  // a virtualenv symlink or changing the packaged runtime contract.
  const executable = process.env.HSC_LOCAL_AGENT_PYTHON ?? (
    process.platform === 'win32'
      ? join(app.getAppPath(), '.venv', 'Scripts', 'python.exe')
      : join(app.getAppPath(), '.venv', 'bin', 'python')
  )
  return {
    executable,
    args: [
      '-m',
      'home_study_agent',
      '--host',
      '127.0.0.1',
      '--port',
      String(port)
    ]
  }
}
