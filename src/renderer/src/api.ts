import { collectMistakeLearningDrafts } from './mistakeLearningDraftExport'
import {nativeClientExecution} from './nativeClientExecution'
import { changeParentDraftAuthority,parentDraftAuthorityVersion } from './parentDraftAuthority'
import type { ParentDraftRecoveryPort } from '../../shared/parentDraftRecovery'
import type { ParentDraftRecoveryFactory } from './parentDraftRecoveryPort'
import { ObservationIntentFence } from './observationIntentFence'
import type { ExecutionTraceDetail, ExecutionTracePage } from './executionTrace'
import { acknowledgeAgentRequest, pendingAgentRequests, saveAgentRequest, AGENT_REQUEST_UNCONFIRMED, AGENT_REQUEST_SAVE_FAILED, isNonRepeatableAgentHandoff } from './agentRequestJournal'
import { agentExecutionSnapshot, pendingAgentExecutions, saveAgentExecution, settleAgentExecution } from './agentExecutionJournal'
import type { GoalRuleReview, GoalRuleConfirmation, GoalRuleConfirmationResult } from './goalRuleReviewTypes'
import { trackDiagnosticCapture, type VoiceCaptureReference } from './voiceCaptureContext'
import { DiagnosticTaskStartRequiredError } from './diagnosticTaskStart'
import { validateVoiceInteractionHandoff } from './voiceInteractionHandoff'
import type { LegacyOcrPreview, LegacyOcrCommand, LegacyOcrReceipt } from './legacyOcrDispositionTypes'
import type { PetMediaEnvelope, PetPlaybackCommand, PetPlaybackReceipt, PetPromptMedia, PetActionReceipt } from './petMedia'
import type {
  VoiceInteractionRequest, VoiceInteractionResponse, VoiceInteractionContext, VoiceInteractionRecovery, VoiceInteractionHandoff,
  VariantGenerationJob,
  BootstrapState,
  ClassroomRecall,
  DailyReport,
  ChildLearningSummary,
  ParentReportSnapshot,
  ReportDelivery,
  ReportDeliveryRecipient,
  DiagnosticAttempt,
  DiagnosticItem,
  DailySession,
  HomeworkItem,
  SessionWorkspaceState,
  TutorTurn, TutorFeedback,
  ParentStatus,
  LearningState, MasteryHistoryItem
  , PrivacyPreferences, SystemHealth, PeriodReport, BackupManifest
  , MistakeCaptureQueue, OcrArtifact, VoiceInput, StandbyVoiceInput
  , AgentTurn, ObservationQualification, ObservationState, FamilyRule, KnowledgeSource, CurriculumCorpus, CurriculumCoverage, CurriculumSearchResult, CurriculumStandardSource, CurriculumStandardAlignmentSummary, CurriculumInternalizationScope, CurriculumInternalizationJob, CurriculumCapability
  , ChildProfile, ParentToday, DeviceBinding, DeviceQualificationMode, DeviceQualificationRun, DeviceQualificationSummary, ObservationCalibrationRun, ObservationCalibrationSummary, ParentDashboard, ChildSafetyEvent
  , ParentReportFeedback
  , CaptureArtifactSummary
  , StrategyExperiment
  , ModelCallAudit, ModelPricing, ModelRuntimePolicyStatus, PetBaseline, SessionDraft, SessionDraftSection, SensorReleaseVerification
  , MistakeCaptureCandidate, MistakeDialogue, MistakeItem, MistakeKnowledgeCandidate, MistakeList, MistakeNextActivity, WeaknessCluster, OcrReviewItem
  , VariantCandidate, SpecializedSupplyCandidate, SpecializedSupplyGap, ExamBlueprintReadiness
  , ChildModelCorrection, ChildModelCorrectionTarget, GoalProgramCycle, LongitudinalChildModel, SpecializedRoadmap
  , BehaviorReplay, BehaviorBaseline, BehaviorPersonalization, HabitExperimentList, HabitExperiment, PendingBehaviorInterventionResponse, KnowledgeGraphLearningPath
  , DocumentMonitorStatus, DocumentMonitorState, DocumentPageRevision, DocumentOcrPreview, DocumentOcrPreviewPhase, DocumentOcrPreviewResolution, AgentRuntimeStatus
  , ControlledContentCandidate, ControlledContentPlayback, ControlledContentRule
  , ControlledGameApproval, ControlledGameRule, ControlledGameSession
  , PetVocabularyCard, PetVocabularyReview, PetVocabularySummary
} from './types'

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly code?: string,
    readonly retryable?: boolean,
    readonly recoveryAction?: string,
    readonly details?: Record<string, unknown>
  ) {
    super(message)
  }
}

function scopedVoiceInteractionHandoff(value: unknown, sessionId: string, childId: string): VoiceInteractionHandoff {
  try { return validateVoiceInteractionHandoff(value, { sessionId, childId }) }
  catch { throw new ApiError('原请求的交接回执无法核对，没有重新执行；请核对原请求。', undefined, 'VOICE_INTERACTION_HANDOFF_SCOPE_MISMATCH') }
}

export type RealtimeVoiceAdmission = {
  voice_input_id: string; client_action_id: string; session_id: string; child_id: string
  status: 'completed'; source_kind: 'realtime_transcript'; raw_audio_available: false; original_transcript: string
}

let configuredBaseUrl: string | null = null
let configuredSessionToken = ''
let parentToken = ''
let parentRecoveryUnlockAuthority:number|null=null
let runtimeEpoch = 0
let runtimeIdentityCheck: { epoch: number; promise: Promise<void> } | null = null
/** Main independently authorizes every read/prepare/discard; no PIN/token enters a snapshot. */
export const createParentDraftRecoveryPort:ParentDraftRecoveryFactory=(childId,scope)=>{
  const desktop=():ParentDraftRecoveryPort=>{
    const port=window.desktop as unknown as Partial<ParentDraftRecoveryPort>|undefined
    if(!port||typeof port.prepareParentDraftRecovery!=='function'||typeof port.readParentDraftRecovery!=='function'||typeof port.checkpointParentDraftRecovery!=='function'||typeof port.revokeParentDraftRecovery!=='function'||typeof port.discardParentDraftRecovery!=='function')throw Error('当前客户端尚不支持加密草稿保护，保护未确认。')
    return port as ParentDraftRecoveryPort
  }
  const authorized=async<T>(invoke:(port:ParentDraftRecoveryPort,token:string)=>Promise<T>):Promise<T>=>{
    const token=parentToken,authority=parentDraftAuthorityVersion(),epoch=runtimeEpoch
    const current=():boolean=>!!token&&token===parentToken&&authority===parentRecoveryUnlockAuthority&&authority===parentDraftAuthorityVersion()&&epoch===runtimeEpoch
    if(!current())throw Error('请先解锁家长 PIN，再核对本机草稿。')
    const result=await invoke(desktop(),token)
    if(!current())throw Error('家长身份或服务已变化，未接纳恢复结果。')
    return result
  }
  return{
    prepare:()=>authorized((port,token)=>port.prepareParentDraftRecovery(childId,scope,token)),
    read:revision=>authorized((port,token)=>port.readParentDraftRecovery(childId,scope,token,revision)),
    // A write-only lease may flush PROVIDED input synchronously during the lock transition.
    checkpoint:(lease,sequence,snapshot)=>desktop().checkpointParentDraftRecovery(lease,sequence,snapshot),
    revoke:()=>desktop().revokeParentDraftRecovery(),
    discard:(revision,explicit)=>authorized((port,token)=>port.discardParentDraftRecovery(childId,scope,token,revision,explicit))
  }
}
let runtimeRestartSequence = 0
const observationIntents = new ObservationIntentFence(<T>(path: string, init?: RequestInit) => request<T>(path, init), () => runtimeEpoch)
if (typeof window !== 'undefined') window.addEventListener('hsc-privacy-pause', () => observationIntents.cancelAll())

export async function requestPetAudioSettings(childId: string, command: import('../../shared/petAudioSettings').PetAudioSettingsCommand): Promise<import('../../shared/petAudioSettings').PetAudioSettingsSummary> {
  const epoch = runtimeEpoch, token = parentToken, session = configuredSessionToken
  if (!token || !window.desktop?.petAudioSettings) throw Error('请在桌面家长中心解锁后配置原音模型。')
  let result: import('../../shared/petAudioSettings').PetAudioSettingsSummary
  try {
    result = await window.desktop.petAudioSettings(childId, token, command)
  } finally {
    // The settings receipt can be lost after a durable write and service restart.
    // Reconcile only runtime identity here; the original operation stays journaled.
    if (runtimeEpoch === epoch && parentToken === token) {
      const current = await window.desktop.getRuntimeConfig().catch(() => null)
      if (runtimeEpoch === epoch && parentToken === token
        && (!current || current.status !== 'ready' || current.sessionToken !== session || current.localApiUrl !== configuredBaseUrl)) {
        resetRuntimeCache()
        window.dispatchEvent(new CustomEvent('hsc-runtime-restarted', { detail: {
          error: !current || current.status !== 'ready' ? '原音配置结果待核对，本地服务尚未就绪；请重启服务后重新解锁。' : undefined
        } }))
        throw Error('服务身份已变化，请重新解锁并读取原配置；不会重复保存。')
      }
    }
  }
  if (runtimeEpoch !== epoch || parentToken !== token) throw Error('身份或服务已变化，请重新读取原配置。')
  return result
}

/** Parent token remains inside this module and is checked again by the native host. */
export async function requestTutorExplanationSettings(command: import('../../main/tutor-explanation-handler').TutorExplanationCommand): Promise<import('../../main/tutor-explanation-handler').TutorExplanationResponse> {
  const epoch = runtimeEpoch, token = parentToken, session = configuredSessionToken, authority = parentDraftAuthorityVersion()
  if (!token || authority !== parentRecoveryUnlockAuthority || !window.desktop?.tutorExplanationSettings) throw Error('请先在桌面家长中心解锁，再配置讲题服务。')
  let result: import('../../main/tutor-explanation-handler').TutorExplanationResponse
  try { result = await window.desktop.tutorExplanationSettings(token, command) }
  catch (error) {
    if (command.action === 'save' && runtimeEpoch === epoch && parentToken === token) {
      const current = await window.desktop.getRuntimeConfig().catch(() => null)
      if (runtimeEpoch === epoch && parentToken === token && (!current || current.status !== 'ready' || current.sessionToken !== session || current.localApiUrl !== configuredBaseUrl)) {
        resetRuntimeCache()
        window.dispatchEvent(new CustomEvent('hsc-runtime-restarted', { detail: { error: '讲题配置结果待核对，请重新解锁家长中心查看。' } }))
      }
    }
    throw error
  }
  if (runtimeEpoch !== epoch || parentToken !== token || authority !== parentDraftAuthorityVersion()) throw Error('家长身份或服务已变化，请重新解锁并查看配置。')
  if (command.action === 'save') {
    const current = await window.desktop.getRuntimeConfig().catch(() => null)
    if (runtimeEpoch !== epoch || parentToken !== token || authority !== parentDraftAuthorityVersion()) throw Error('家长身份或服务已变化，请重新解锁并查看配置。')
    if (!current || current.status !== 'ready' || current.sessionToken !== session || current.localApiUrl !== configuredBaseUrl) {
      const confirmed = current?.status === 'ready' && result.runtimeApplied === true && typeof result.appliedRuntimeEpoch === 'number' && current.runtimeEpoch === result.appliedRuntimeEpoch
      resetRuntimeCache()
      window.dispatchEvent(new CustomEvent('hsc-runtime-restarted', { detail: confirmed
        ? { notice: '讲题服务配置已保存并应用，本地服务已重启；如需再次设置，请重新解锁家长中心。' }
        : { error: '讲题配置已保存，但旧服务未确认停止，已有请求可能仍在进行；请恢复并核对本地服务。' } }))
      if (!confirmed) throw Error('讲题配置已保存，旧服务未确认停止。')
    }
  }
  return result
}

export function resetRuntimeCache(reason: 'runtime' | 'rotation_complete' = 'runtime'): void {
  changeParentDraftAuthority(reason)
  runtimeEpoch += 1
  observationIntents.reset()
  configuredBaseUrl = null
  configuredSessionToken = ''
  parentToken = ''
}

async function reconcileFailedRuntime(epoch: number, base: string, session: string): Promise<void> {
  const desktop = window.desktop
  const currentRequest = (): boolean => runtimeEpoch === epoch
    && configuredBaseUrl === base && configuredSessionToken === session && window.desktop === desktop
  if (!desktop || !currentRequest()) return
  if (runtimeIdentityCheck?.epoch === epoch) return runtimeIdentityCheck.promise
  const check = { epoch, promise: Promise.resolve().then(async () => {
    let timeout: ReturnType<typeof setTimeout> | undefined
    const current = await Promise.race([
      Promise.resolve().then(() => desktop.getRuntimeConfig()).catch(() => null),
      new Promise<null>(resolve => { timeout = setTimeout(() => resolve(null), 2_000) })
    ]).finally(() => { if (timeout !== undefined) clearTimeout(timeout) })
    if (!current || !currentRequest() || !['ready', 'starting', 'error'].includes(current.status)) return
    if (current.status === 'ready' && current.localApiUrl === base && current.sessionToken === session) return
    // Retire the old requests and parent authority, but never replay an operation
    // whose response may have been lost. App restores the current workspace.
    resetRuntimeCache()
    window.dispatchEvent(new CustomEvent('hsc-runtime-restarted', { detail: {
      reason: 'automatic_identity_change',
      error: current.status === 'ready' ? undefined : '本地学习服务正在恢复，请稍后继续。'
    } }))
  }) }
  runtimeIdentityCheck = check
  try { await check.promise }
  finally { if (runtimeIdentityCheck === check) runtimeIdentityCheck = null }
}

async function baseUrl(): Promise<string> {
  if (configuredBaseUrl) return configuredBaseUrl
  if (window.desktop) {
    const epoch = runtimeEpoch
    const config = await window.desktop.getRuntimeConfig()
    if (runtimeEpoch !== epoch) throw new ApiError('本地学习服务已切换，请重新核对当前学习状态。', undefined, 'LOCAL_SERVICE_CHANGED')
    if (config.status !== 'ready') {
      throw new ApiError(config.error ?? '本地学习服务尚未就绪。', undefined, 'LOCAL_SERVICE_OFFLINE')
    }
    if (configuredBaseUrl && (configuredBaseUrl !== config.localApiUrl || configuredSessionToken !== config.sessionToken)) {
      throw new ApiError('本地学习服务已切换，请重新核对当前学习状态。', undefined, 'LOCAL_SERVICE_CHANGED')
    }
    configuredBaseUrl = config.localApiUrl
    configuredSessionToken = config.sessionToken
    return config.localApiUrl
  }
  configuredBaseUrl = 'http://127.0.0.1:8765'
  return configuredBaseUrl
}

export async function request<T>(path: string, init?: RequestInit, retryVoiceContention = false, stopOnPrivacy = false): Promise<T> {
  const epoch = runtimeEpoch, parentAuthority=parentDraftAuthorityVersion()
  // Initial startup already has App's bounded recovery loop. Only a previously
  // cached identity can become stale beneath an open renderer.
  const hadRuntimeIdentity = configuredBaseUrl !== null
  let voiceRetryCancelled = false
  const cancelVoiceRetry = (): void => { voiceRetryCancelled = true }
  const guardPrivacy = retryVoiceContention || stopOnPrivacy
  if (guardPrivacy) window.addEventListener('hsc-privacy-pause', cancelVoiceRetry)
  let base: string
  try { base = await baseUrl() }
  catch (error) {
    if (guardPrivacy) window.removeEventListener('hsc-privacy-pause', cancelVoiceRetry)
    throw error
  }
  const url = `${base}${path}`, session = configuredSessionToken
  const options: RequestInit = {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(session ? { 'X-HSC-Session': session } : {}),
      ...init?.headers
    }
  }
  const send = async (attempt: number): Promise<T> => {
    if (options.signal?.aborted) throw new DOMException('原请求已停止，不再发送。', 'AbortError')
    if (voiceRetryCancelled) throw new DOMException('原语音请求已停止，不再发送。', 'AbortError')
    if (runtimeEpoch !== epoch) throw new ApiError('本地学习服务已切换，请重新核对当前学习状态。', undefined, 'LOCAL_SERVICE_CHANGED')
    const parentHeader=new Headers(options.headers).get('X-HSC-Parent')
    if(parentHeader!==null&&(parentAuthority!==parentDraftAuthorityVersion()||parentHeader!==parentToken))
      throw new ApiError('家长身份已变化，请重新解锁后核对原操作。',401,'PARENT_SESSION_REQUIRED')
    const response = await fetch(url, options).catch(async (cause) => {
      if ((cause instanceof DOMException && cause.name === 'AbortError') || (cause instanceof Error && cause.name === 'AbortError')) throw cause
      if (hadRuntimeIdentity) await reconcileFailedRuntime(epoch, base, session)
      throw new ApiError('本地学习服务尚未启动，请稍后重试。', undefined, 'LOCAL_SERVICE_OFFLINE')
    })

    if(parentHeader!==null&&(parentAuthority!==parentDraftAuthorityVersion()||parentHeader!==parentToken)){
      await response.body?.cancel().catch(()=>undefined)
      if (options.signal?.aborted) throw new DOMException('原请求已停止，请核对原操作。', 'AbortError')
      if (guardPrivacy && voiceRetryCancelled) throw new DOMException('原语音请求已停止，请核对原操作。', 'AbortError')
      if (guardPrivacy && runtimeEpoch !== epoch) throw new ApiError('本地学习服务已切换，请核对原操作。', undefined, 'LOCAL_SERVICE_CHANGED')
      throw new ApiError('家长身份已变化，请重新解锁后核对原操作。',401,'PARENT_SESSION_REQUIRED')
    }
    if (runtimeEpoch !== epoch) {
      await response.body?.cancel().catch(() => undefined)
      if (options.signal?.aborted || (guardPrivacy && voiceRetryCancelled)) throw new DOMException('原请求已停止，请核对原操作。', 'AbortError')
      throw new ApiError('本地学习服务已切换，请核对原操作。', undefined, 'LOCAL_SERVICE_CHANGED')
    }
    if (!response.ok) {
      if(response.status===401&&path.startsWith('/v1/parent/')&&parentToken
        &&new Headers(options.headers).get('X-HSC-Parent')===parentToken
        &&parentAuthority===parentDraftAuthorityVersion()){
        parentToken='';changeParentDraftAuthority('expired')
      }
      const body = await response.json().catch(() => null)
      // FastAPI serializes HTTPException payloads under `detail`; the rest of
      // the local API returns the same fields at the top level.  Preserve both
      // shapes so UI recovery branches receive the actionable server message.
      const detail = body && typeof body.detail === 'object' && body.detail !== null ? body.detail as Record<string, unknown> : null
      const message = typeof body?.message === 'string' ? body.message
        : typeof detail?.message === 'string' ? detail.message
        : typeof body?.detail === 'string' ? body.detail
        : '请求失败'
      const code = typeof body?.code === 'string' ? body.code
        : typeof detail?.code === 'string' ? detail.code
        : undefined
      const retryable = typeof body?.retryable === 'boolean' ? body.retryable
        : typeof detail?.retryable === 'boolean' ? detail.retryable
        : undefined
      const recoveryAction = typeof body?.recovery_action === 'string' ? body.recovery_action
        : typeof detail?.recovery_action === 'string' ? detail.recovery_action
        : undefined
      const details = body?.details && typeof body.details === 'object' ? body.details as Record<string, unknown>
        : detail?.details && typeof detail.details === 'object' ? detail.details as Record<string, unknown>
        : undefined
      if (hadRuntimeIdentity && response.status === 401 && code === 'LOCAL_SESSION_REQUIRED') await reconcileFailedRuntime(epoch, base, session)
      // Only the original idempotent voice turn opts in. Never replay a network
      // error or a generic mutation whose result may already have been committed.
      if (retryVoiceContention && attempt === 0 && response.status === 503
        && code === 'CALIBRATION_WRITER_BUSY' && retryable === true && recoveryAction === 'retry_same_request') {
        await new Promise(resolve => window.setTimeout(resolve, 1000))
        if (voiceRetryCancelled) throw new DOMException('原语音请求已停止，不再重试。', 'AbortError')
        if (runtimeEpoch !== epoch) throw new ApiError('本地学习服务已切换，请重新核对当前学习状态。', undefined, 'LOCAL_SERVICE_CHANGED')
        return send(1)
      }
      throw new ApiError(message, response.status, code, retryable, recoveryAction, details)
    }
    const body = await response.json() as T
    if (options.signal?.aborted) throw new DOMException('原请求已停止，请核对原操作。', 'AbortError')
    if (guardPrivacy && voiceRetryCancelled) throw new DOMException('原语音请求已停止，请核对原操作。', 'AbortError')
    if (runtimeEpoch !== epoch) throw new ApiError('本地学习服务已切换，请核对原操作。', undefined, 'LOCAL_SERVICE_CHANGED')
    if(parentHeader!==null&&(parentAuthority!==parentDraftAuthorityVersion()||parentHeader!==parentToken))
      throw new ApiError('家长身份已变化，请重新解锁后核对原操作。',401,'PARENT_SESSION_REQUIRED')
    return body
  }
  try { return await send(0) }
  finally { if (guardPrivacy) window.removeEventListener('hsc-privacy-pause', cancelVoiceRetry) }
}

async function requestDocumentMutation<T>(sessionId: string, operation: 'ocr' | 'revision', body: { client_action_id: string } & Record<string, unknown>, timeoutMs: number): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const stop = (): void => controller.abort()
  window.addEventListener('hsc-privacy-pause', stop, { once: true })
  const receiptPath = `/v1/sessions/${encodeURIComponent(sessionId)}/document-monitor/receipts/${operation}/${encodeURIComponent(body.client_action_id)}`
  try {
    const prepared = await request<{ session_id: string; client_action_id: string; operation: string; status: string }>(receiptPath + '/prepare', { method: 'POST', signal: controller.signal })
    if (prepared.session_id !== sessionId || prepared.client_action_id !== body.client_action_id || prepared.operation !== operation || prepared.status !== 'prepared') {
      throw new ApiError('页面操作的恢复记录没有确认保存，尚未提交图片。', undefined, 'DOCUMENT_RECOVERY_NOT_PREPARED')
    }
    if (controller.signal.aborted) throw new DOMException('Document submission cancelled', 'AbortError')
    const result = await request<T>(`/v1/sessions/${encodeURIComponent(sessionId)}/document-monitor/${operation === 'ocr' ? 'ocr' : 'revisions'}`, {
      method: 'POST', body: JSON.stringify(body), signal: controller.signal })
    // Losing this acknowledgement leaves a recoverable notice, not another OCR.
    await request(receiptPath + '/acknowledge', { method: 'POST', signal: AbortSignal.timeout(5_000) }).catch(() => undefined)
    return result
  } catch (error) {
    // A lost mutation response is not proof that OCR or its save did not run.
    if (controller.signal.aborted || error instanceof SyntaxError
      || (error instanceof ApiError && (error.code === 'LOCAL_SERVICE_OFFLINE'
        || error.code === 'DOCUMENT_OCR_RESULT_UNCONFIRMED' || (error.status !== undefined && error.status >= 500)))) {
      throw new ApiError('页面操作没有确认保存结果，不会自动重发。', undefined, 'DOCUMENT_OPERATION_RESULT_UNCONFIRMED')
    }
    throw error
  } finally { clearTimeout(timer); window.removeEventListener('hsc-privacy-pause', stop) }
}

export function requestParentLocal<T>(path: string, init?: RequestInit): Promise<T> {
  if (!path.startsWith('/api/parent/') && !path.startsWith('/v1/parent/')) {
    return Promise.reject(new ApiError('家长请求路径不受支持。', undefined, 'PARENT_PATH_INVALID'))
  }
  return request<T>(path, { ...init, headers: { ...init?.headers, 'X-HSC-Parent': parentToken } })
}

export function requestParentPetAudioPurpose<T>(path: string, init?: RequestInit): Promise<T> {
  if (!/^\/v1\/parent\/children\/[^/]+\/pet-audio(?:\/|$)/.test(path)) {
    return Promise.reject(new ApiError('原音用途请求路径不受支持。', undefined, 'PARENT_PATH_INVALID'))
  }
  return request<T>(path, { ...init, headers: { ...init?.headers, 'X-HSC-Parent': parentToken } }, false, true)
}

function nativePrivacyResult<T>(response:import('../../shared/nativePrivacy').NativePrivacyResponse):T {
  if(response.status>=200&&response.status<300)return response.body as T
  const body=response.body as Record<string,unknown>|null
  const detail=body?.detail&&typeof body.detail==='object'?body.detail as Record<string,unknown>:body
  throw new ApiError(typeof detail?.message==='string'?detail.message:typeof body?.detail==='string'?body.detail:'本机活动或原隐私操作尚未核对完成。',
    response.status,typeof detail?.code==='string'?detail.code:undefined,
    typeof detail?.retryable==='boolean'?detail.retryable:undefined,
    typeof detail?.recovery_action==='string'?detail.recovery_action:undefined,
    detail?.details&&typeof detail.details==='object'?detail.details as Record<string,unknown>:undefined)
}
async function destructiveParentRequest<T>(path:string,body:Record<string,unknown>):Promise<T>{
  if(window.desktop?.executeNativePrivacy){
    const response=await window.desktop.executeNativePrivacy({path,body},parentToken)
      .catch(()=>{throw new ApiError('本机活动关闭或原操作回执尚未核对完成。原请求已保留，请核对原操作，不要重新删除或恢复。',undefined,'NATIVE_PRIVACY_PENDING')})
    return nativePrivacyResult<T>(response)
  }
  return requestParentLocal<T>(path,{method:'POST',body:JSON.stringify(body)})
}
async function privacyReceipt(requestId:string,explicit=false):Promise<import('./privacyOperationRecovery').PrivacyOperationReceipt>{
  if(window.desktop?.recoverNativePrivacy)return nativePrivacyResult(await window.desktop.recoverNativePrivacy(requestId,parentToken,explicit))
  return requestParentLocal(`/v1/parent/privacy-operations/${encodeURIComponent(requestId)}`,{cache:'no-store'})
}

export async function requestParentPetLocal<T>(path: string, init?: RequestInit): Promise<T> {
  const segment = '[A-Za-z0-9_-]+'
  const endpoint = new RegExp(`^/v1/sessions/${segment}/pet-media/(?:prompt/${segment}(?:/materialize)?|listening/begin|${segment}/source-review)(?:\\?item_version=[A-Za-z0-9_%.-]+)?$`)
  if (!endpoint.test(path)) return Promise.reject(new ApiError('家长材料请求路径不受支持。', undefined, 'PARENT_PATH_INVALID'))
  const origin = new URL(await baseUrl())
  if (!['http:', 'https:'].includes(origin.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname)
    || origin.username || origin.password) throw new ApiError('听力材料只允许使用本机学习服务。')
  return request<T>(path, { ...init, redirect: 'error', cache: 'no-store', headers: { ...init?.headers, 'X-HSC-Parent': parentToken } })
}

export async function requestPetListeningAudio(sessionId: string, mediaId: string, sha256: string, signal: AbortSignal): Promise<ArrayBuffer> {
  return requestPetAudio(sessionId, mediaId, sha256, signal, false)
}

export async function requestParentPetAudio(sessionId: string, mediaId: string, sha256: string, signal: AbortSignal): Promise<ArrayBuffer> {
  return requestPetAudio(sessionId, mediaId, sha256, signal, true)
}

export async function requestParentRubricAudio(path: string, init?: RequestInit): Promise<ArrayBuffer> {
  const endpoint = /^\/v1\/parent\/children\/[A-Za-z0-9_-]+\/learning-rubrics\/[A-Za-z0-9_-]+\/audio\?media_id=[A-Za-z0-9_-]+&audio_sha256=[a-f0-9]{64}$/
  if (!endpoint.test(path) || init?.method && init.method !== 'GET') throw new ApiError('口语原音请求路径不受支持。', undefined, 'PARENT_PATH_INVALID')
  const origin = new URL(await baseUrl())
  if (!['http:', 'https:'].includes(origin.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname)
    || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) throw new ApiError('口语原音只允许从本机学习服务读取。')
  const response = await fetch(`${origin.origin}${path}`, {
    method: 'GET', signal: init?.signal, cache: 'no-store', redirect: 'error', headers: {
      ...(configuredSessionToken ? { 'X-HSC-Session': configuredSessionToken } : {}),
      'X-HSC-Parent': parentToken,
    },
  })
  return readLocalWav(response)
}

export async function requestParentTextbookSource(childId: string, captureRef: string, signal: AbortSignal): Promise<{ bytes: ArrayBuffer; mediaType: 'image/png' | 'image/jpeg' }> {
  const authority=parentDraftAuthorityVersion(),token=parentToken,epoch=runtimeEpoch
  const check=():void=>{if(signal.aborted||authority!==parentDraftAuthorityVersion()||token!==parentToken||epoch!==runtimeEpoch)throw new ApiError('家长身份已变化，未继续读取原页。',401,'PARENT_SESSION_REQUIRED')}
  const opaque = /^[A-Za-z0-9_-]+$/
  if (!opaque.test(childId) || !opaque.test(captureRef)) throw new ApiError('教材原页请求路径不受支持。', undefined, 'PARENT_PATH_INVALID')
  const origin = new URL(await baseUrl())
  if (!['http:', 'https:'].includes(origin.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname)
    || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) throw new ApiError('教材原页只允许从本机学习服务读取。')
  check()
  const path = `/v1/parent/knowledge-graph/textbook-capture/${childId}/artifacts/${captureRef}/source`
  const response = await fetch(`${origin.origin}${path}`, {
    method: 'GET', signal, cache: 'no-store', redirect: 'error', headers: {
      ...(configuredSessionToken ? { 'X-HSC-Session': configuredSessionToken } : {}),
      'X-HSC-Parent': parentToken,
    },
  })
  if(response.status===401&&authority===parentDraftAuthorityVersion()&&token===parentToken){parentToken='';changeParentDraftAuthority('expired')}
  try{check()}catch(error){await response.body?.cancel().catch(()=>undefined);throw error}
  const mediaType = response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase()
  if (response.status !== 200 || (mediaType !== 'image/png' && mediaType !== 'image/jpeg') || !response.body) {
    await response.body?.cancel().catch(() => undefined)
    throw new ApiError(response.status === 401 ? '家长身份已过期，请重新解锁。' : '教材原页暂时无法读取。', response.status)
  }
  const declaredLength = Number(response.headers.get('Content-Length') || 0)
  const maxLength = 18 * 1024 * 1024
  if (declaredLength > maxLength) {
    await response.body.cancel().catch(() => undefined)
    throw new ApiError('教材原页响应超过本机预览上限。')
  }
  const reader = response.body.getReader(), chunks: Uint8Array[] = []
  let length = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      check()
      if (done) break
      length += value.byteLength
      if (length > maxLength) throw new ApiError('教材原页响应超过本机预览上限。')
      chunks.push(value)
    }
  } catch (error) { await reader.cancel().catch(() => undefined); throw error }
  finally { reader.releaseLock() }
  const bytes = new Uint8Array(length)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
  return { bytes: bytes.buffer, mediaType }
}

export async function requestParentTrialImage(childId: string, artifactId: string, signal: AbortSignal): Promise<Blob> {
  if (![childId, artifactId].every(value => /^[A-Za-z0-9_-]+$/.test(value))) throw new ApiError('诊断图片引用无效。')
  const origin = new URL(await baseUrl())
  if (!['http:', 'https:'].includes(origin.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname)
      || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) throw new ApiError('诊断图片只能从本机读取。')
  const response = await fetch(`${origin.origin}/v1/parent/children/${childId}/document-trial/frames/${artifactId}/image`, {
    signal, cache: 'no-store', redirect: 'error', headers: {
      ...(configuredSessionToken ? { 'X-HSC-Session': configuredSessionToken } : {}), 'X-HSC-Parent': parentToken
    }
  })
  if (!response.ok || response.headers.get('Content-Type')?.split(';')[0] !== 'image/jpeg' || !response.body) {
    await response.body?.cancel()
    throw new ApiError(response.status === 401 ? '请重新解锁家长中心。' : '诊断图片已删除、损坏或暂时无法读取。', response.status)
  }
  const reader = response.body.getReader(), chunks: Uint8Array[] = []
  let size = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > 18 * 1024 * 1024) throw new ApiError('诊断图片超过预览上限。')
      chunks.push(value)
    }
  } catch (error) { await reader.cancel().catch(() => undefined); throw error }
  finally { reader.releaseLock() }
  return new Blob(chunks as BlobPart[], { type: 'image/jpeg' })
}

export async function requestDiagnosticVisual(path: string, signal: AbortSignal): Promise<unknown> {
  const route = /^\/v1\/sessions\/[A-Za-z0-9_-]+\/diagnostic-visual(?:\/image)?\?[^#]+$/
  if (!route.test(path)) throw new ApiError('当前题目图形路径无法核对。')
  return readChildVisual(path, signal)
}

export async function requestTutorVisual(path: string, signal: AbortSignal): Promise<unknown> {
  if (!/^\/v1\/sessions\/[A-Za-z0-9_-]+\/tutor-visual(?:\/image)?\?[^#]+$/.test(path)) throw new ApiError('本轮讲解图形路径无法核对。')
  return readChildVisual(path, signal)
}

async function readChildVisual(path: string, signal: AbortSignal): Promise<unknown> {
  const origin = new URL(await baseUrl())
  if (!['http:', 'https:'].includes(origin.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname)
    || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) throw new ApiError('原图只能从本机读取。')
  const response = await fetch(`${origin.origin}${path}`, { method: 'GET', signal, cache: 'no-store', redirect: 'error', headers: {
    ...(configuredSessionToken ? { 'X-HSC-Session': configuredSessionToken } : {}),
  } })
  const image = path.split('?')[0].endsWith('/image'), mime = response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase()
  if (response.status !== 200 || mime !== (image ? 'image/png' : 'application/json') || !response.body) {
    await response.body?.cancel().catch(() => undefined)
    throw new ApiError('原题图形暂时无法核对。', response.status)
  }
  const reader = response.body.getReader(), chunks: Uint8Array[] = []
  let length = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      length += value.byteLength
      if (length > (image ? 2 * 1024 * 1024 : 64 * 1024)) throw new ApiError('原题图形响应超过上限。')
      chunks.push(value)
    }
  } catch (error) { await reader.cancel().catch(() => undefined); throw error }
  finally { reader.releaseLock() }
  const bytes = new Uint8Array(length)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
  return image ? bytes.buffer : JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))
}

async function visualParentOrigin(path: string): Promise<string> {
  const route = /^\/v1\/parent\/children\/[^/?#]+\/visual-materials(?:\/[^/?#]+\/regions\/[^/?#]+(?:\/(?:image|storage|displayed|confirm|retract|revoke|commands\/[^/?#]+))?)?(?:\?[^#]*)?$/
  const retention = /^\/v1\/parent\/children\/[^/?#]+\/visual-retention$/
  if (!(route.test(path) || retention.test(path)) || path.split('?')[0].split('/').some(part => {
    try { return /[\\/?#\u0000-\u001f]/.test(decodeURIComponent(part)) || ['.', '..'].includes(decodeURIComponent(part)) } catch { return true }
  })) throw new ApiError('图形材料请求路径不受支持。', undefined, 'PARENT_PATH_INVALID')
  const origin = new URL(await baseUrl())
  if (!['http:', 'https:'].includes(origin.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname)
    || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) throw new ApiError('图形原件只允许从本机学习服务读取。')
  return origin.origin
}

export async function requestParentVisualLocal<T>(path: string, init?: RequestInit): Promise<T> {
  await visualParentOrigin(path)
  return requestParentLocal<T>(path, { ...init, cache: 'no-store', redirect: 'error' })
}

export async function requestParentVisualImage(path: string, signal: AbortSignal): Promise<{ bytes: ArrayBuffer; display_token: string }> {
  const origin = await visualParentOrigin(path)
  if (!path.split('?')[0].endsWith('/image')) throw new ApiError('图形原件请求路径不受支持。')
  const response = await fetch(`${origin}${path}`, { method: 'GET', signal, cache: 'no-store', redirect: 'error', headers: {
    ...(configuredSessionToken ? { 'X-HSC-Session': configuredSessionToken } : {}), 'X-HSC-Parent': parentToken,
  } })
  const display_token = response.headers.get('X-HSC-Visual-Display-Token')
  if (response.status !== 200 || response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'image/png'
    || !display_token || display_token.length > 6000 || !response.body) {
    await response.body?.cancel().catch(() => undefined)
    throw new ApiError('原图或显示凭证无法核对，未加载替代图片。', response.status, 'VISUAL_MATERIAL_UNAVAILABLE')
  }
  const reader = response.body.getReader(), chunks: Uint8Array[] = []
  let length = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      length += value.byteLength
      if (length > 2 * 1024 * 1024) throw new ApiError('图形材料超过本机读取上限。')
      chunks.push(value)
    }
  } catch (error) { await reader.cancel().catch(() => undefined); throw error }
  finally { reader.releaseLock() }
  const bytes = new Uint8Array(length)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
  return { bytes: bytes.buffer, display_token }
}

async function requestPetAudio(sessionId: string, mediaId: string, sha256: string, signal: AbortSignal, parentPreview: boolean): Promise<ArrayBuffer> {
  if (!/^[a-f0-9]{64}$/.test(sha256)) throw new ApiError('听力材料版本无效。')
  const origin = new URL(await baseUrl())
  if (!['http:', 'https:'].includes(origin.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname)
    || origin.username || origin.password) throw new ApiError('原音只允许从本机学习服务读取。')
  const endpoint = parentPreview ? 'review-audio' : 'audio'
  const response = await fetch(`${origin.href.replace(/\/$/, '')}/v1/sessions/${encodeURIComponent(sessionId)}/pet-media/${encodeURIComponent(mediaId)}/${endpoint}?audio_sha256=${sha256}`, {
    signal, cache: 'no-store', redirect: 'error', headers: {
      ...(configuredSessionToken ? { 'X-HSC-Session': configuredSessionToken } : {}),
      ...(parentPreview ? { 'X-HSC-Parent': parentToken } : {}),
    },
  })
  return readLocalWav(response)
}

async function readLocalWav(response: Response): Promise<ArrayBuffer> {
  if (!response.ok || response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'audio/wav' || !response.body) {
    await response.body?.cancel().catch(() => undefined)
    throw new ApiError('原题听力材料尚未核准或已经变化，没有播放替代内容。', response.status, 'PET_LISTENING_AUDIO_UNAVAILABLE')
  }
  const limit = 24 * 1024 * 1024, reader = response.body.getReader(), chunks: Uint8Array[] = []
  let size = 0
  try {
    for (;;) {
      const chunk = await reader.read()
      if (chunk.done) break
      size += chunk.value.byteLength
      if (size > limit) throw new ApiError('听力材料超过本机播放上限。')
      chunks.push(chunk.value)
    }
  } catch (error) { await reader.cancel().catch(() => undefined); throw error }
  finally { reader.releaseLock() }
  const data = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength }
  return data.buffer
}

export async function requestVoiceSynthesis(
  sessionId: string,
  text: string,
  clientActionId: string,
  signal: AbortSignal,
  context: { local_only?: boolean; source_agent_turn_id?: string; protocol?: 'pcm16_frame_v1' | 'pcm16_frame_v2' } = {}
): Promise<Response> {
  const url = `${await baseUrl()}/v1/sessions/${encodeURIComponent(sessionId)}/voice/synthesize/stream`
  return fetch(url, {
    method: 'POST',
    signal,
    headers: {
      'Content-Type': 'application/json',
      ...(configuredSessionToken ? { 'X-HSC-Session': configuredSessionToken } : {})
    },
    body: JSON.stringify({
      text, client_action_id: clientActionId,
      local_only: context.local_only,
      source_agent_turn_id: context.source_agent_turn_id,
      protocol: context.protocol,
    })
  }).catch(() => {
    throw new ApiError('语音服务暂时不可用。', undefined, 'VOICE_SYNTHESIS_OFFLINE', true)
  })
}

export async function clearReportDeliveryChannelWithDesktop(channel: 'email' | 'wechat_work_bot'): Promise<{ runtimeRestarted: boolean }> {
  const clearChannel = window.desktop?.clearReportDeliveryChannel
  if (!clearChannel) throw new Error('只能在桌面应用中清除外发通道凭据。')
  const result = await clearChannel(channel, parentToken)
  const restartFailure = result.runtimeRestartFailed ? '凭据和通道配置已清，但本地服务重启失败；请执行“重启本地服务”。' : undefined
  if (result.credentialsCleared) {
    resetRuntimeCache()
    window.dispatchEvent(new CustomEvent('hsc-runtime-restarted', { detail: { error: restartFailure } }))
  }
  if (restartFailure) throw new Error(restartFailure)
  if (!result.metadataCleared) throw new Error('凭据已清、配置元数据待重试；系统不会使用已清除的凭据外发。')
  return { runtimeRestarted: result.runtimeRestarted }
}

async function dispatchAgentRequest(sessionId: string, actionId: string, path: string,
  body: Record<string, unknown>, resume?: { childId: string; voiceInputId?: string; signal?: AbortSignal; handoffTranscript?: string }): Promise<AgentTurn> {
  let pending: ReturnType<typeof saveAgentRequest>
  let originalExecution: ReturnType<typeof pendingAgentExecutions>[number] | undefined
  try {
    if (resume) {
      const delivery = pendingAgentRequests(sessionId).find(value => value.actionId === actionId)
      const executions = pendingAgentExecutions(sessionId).filter(value => value.actionId === actionId)
      originalExecution = executions[0]
      if (executions.length > 1 || originalExecution && (originalExecution.childId !== resume.childId
        || delivery && delivery.createdAt !== originalExecution.createdAt)) throw Error('Conflicting recovery identity')
      pending = delivery ?? (originalExecution ? { version: originalExecution.version, sessionId, actionId,
        createdAt: originalExecution.createdAt } : saveAgentRequest(sessionId, actionId))
    } else pending = saveAgentRequest(sessionId, actionId)
  } catch {
    throw new ApiError(resume ? '原请求的恢复记录无法核对，没有重新操作；请先核对原请求。' : AGENT_REQUEST_SAVE_FAILED,
      undefined, resume ? 'AGENT_REQUEST_UNCONFIRMED' : 'AGENT_REQUEST_SAVE_FAILED')
  }
  const epoch = runtimeEpoch
  let cancelled = resume?.signal?.aborted ?? false
  const cancel = (): void => { cancelled = true }
  window.addEventListener('hsc-privacy-pause', cancel)
  resume?.signal?.addEventListener('abort', cancel, { once: true })
  try {
    if (cancelled) throw new DOMException('原语音请求已停止。', 'AbortError')
    const turn = await request<AgentTurn>(path, { method: 'POST', body: JSON.stringify(body), signal: resume?.signal }, !resume, Boolean(resume))
    if (cancelled) throw new DOMException('原语音请求已停止。', 'AbortError')
    if (runtimeEpoch !== epoch) throw new ApiError('本地学习服务已切换，请核对原请求。', undefined, 'LOCAL_SERVICE_CHANGED')
    if (!turn || typeof turn.agent_turn_id !== 'string' || !turn.agent_turn_id
      || resume && (turn.session_id !== sessionId || turn.child_id !== resume.childId
        || (resume.handoffTranscript === undefined || resume.voiceInputId !== undefined) && turn.action?.voice_input_id !== resume.voiceInputId
        || !turn.transcript?.trim()
        || resume.handoffTranscript !== undefined && turn.transcript.trim() !== resume.handoffTranscript.trim()
        || originalExecution && originalExecution.turnId !== turn.agent_turn_id)) {
      throw new ApiError(AGENT_REQUEST_UNCONFIRMED, undefined, 'AGENT_REQUEST_UNCONFIRMED')
    }
    if (turn.tool_status === 'pending') {
      if (turn.session_id !== sessionId || !turn.child_id) throw new ApiError(AGENT_REQUEST_UNCONFIRMED, undefined, 'AGENT_REQUEST_UNCONFIRMED')
      saveAgentExecution(pending, turn.child_id, turn.agent_turn_id)
    }
    try { acknowledgeAgentRequest(pending) } catch { /* Preserve delivery identity if storage is unavailable. */ }
    return turn
  } catch (cause) {
    if (cause instanceof ApiError && cause.status !== undefined && cause.status >= 400 && cause.status < 500
      && !isNonRepeatableAgentHandoff(cause.status, cause.code) && !resume) {
      try { acknowledgeAgentRequest(pending) } catch { /* Keep the original identity. */ }
    }
    if ((cause instanceof DOMException || cause instanceof Error) && cause.name === 'AbortError') throw cause
    if (!(cause instanceof ApiError) || cause.code === 'LOCAL_SERVICE_OFFLINE') {
      throw new ApiError(AGENT_REQUEST_UNCONFIRMED, undefined, 'AGENT_REQUEST_UNCONFIRMED')
    }
    throw cause
  } finally {
    window.removeEventListener('hsc-privacy-pause', cancel)
    resume?.signal?.removeEventListener('abort', cancel)
  }
}

export const localApi = {
  combinedDailyStatus: (childId: string, signal?: AbortSignal): Promise<{mode: 'legacy' | 'combined' | 'blocked'; sending_safety_ready: boolean; last_check: {status: string; reason?: string; message?: string}; next_day: string | null; result_unknown: boolean; last_sent_day: string | null}> =>
    requestParentLocal(`/v1/parent/children/${encodeURIComponent(childId)}/combined-daily/status`, {signal, cache:'no-store'}),
  activeExamSimulation: (sessionId: string): Promise<{ exam: import('./examListeningDelivery').ExamListeningState | null }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/exam-simulations/active`, { cache: 'no-store' }),
  nativeWritingReceipt: (sessionId:string, query:import('./nativeWritingFlow').NativeWritingReceiptQuery):Promise<import('./nativeWritingFlow').NativeWritingReceipt> => {
    const {session_id: _session, ...values}=query
    return request(`/v1/sessions/${encodeURIComponent(sessionId)}/native-writing-receipt?${new URLSearchParams(Object.entries(values).filter((entry):entry is [string,string]=>typeof entry[1]==='string'))}`, {cache:'no-store',redirect:'error'})
  },
  nativeWritingResume: (sessionId:string, values:import('./nativeWritingFlow').NativeWritingResumeRequest):Promise<import('./nativeWritingFlow').NativeWritingReceipt> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/native-writing-resume`, {method:'POST',body:JSON.stringify(values),redirect:'error'}),
  petListeningAgentControl: (sessionId: string, agentTurnId: string): Promise<PetMediaEnvelope & { control_action_id: string; replayed: boolean; replay_audio_allowed: false }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/pet-media/listening/agent-control`, { method: 'POST', body: JSON.stringify({ agent_turn_id: agentTurnId }) }),
  petPlaybackActionReceipt: (sessionId: string, mediaId: string, clientActionId: string): Promise<PetActionReceipt> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/pet-media/${encodeURIComponent(mediaId)}/action-receipt?client_action_id=${encodeURIComponent(clientActionId)}`),
  petPromptMedia: (sessionId: string, promptId: string, itemVersion: string): Promise<PetPromptMedia> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/pet-media/prompt/${encodeURIComponent(promptId)}?item_version=${encodeURIComponent(itemVersion)}`),
  petPcmControlReference: (sessionId: string, mediaId: string, audioSha: string, signal: AbortSignal): Promise<import('./petPlaybackReference').PetPcmControlReference> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/pet-media/${encodeURIComponent(mediaId)}/control-reference?audio_sha256=${encodeURIComponent(audioSha)}`,
      { signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]) }),
  petMedia: (sessionId: string, mediaId: string): Promise<PetMediaEnvelope> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/pet-media/${encodeURIComponent(mediaId)}`),
  petMediaControl: (sessionId: string, body: PetPlaybackCommand): Promise<PetMediaEnvelope> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/pet-media/listening/control`, { method: 'POST', body: JSON.stringify(body) }),
  petMediaPlayback: (sessionId: string, body: PetPlaybackReceipt): Promise<PetMediaEnvelope> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/pet-media/listening/playback`, { method: 'POST', body: JSON.stringify(body) }),
  restartRuntime: async (): Promise<void> => {
    resetRuntimeCache()
    const epoch = runtimeEpoch, sequence = ++runtimeRestartSequence
    let current: Awaited<ReturnType<NonNullable<Window['desktop']>['restartLocalService']>> | undefined
    try {
      if (window.desktop) current = await window.desktop.restartLocalService()
    } finally {
      // A later UI reset may still cache the old child. Preserve a later grant
      // only when its cached identity matches the actual ready IPC receipt.
      if (runtimeRestartSequence === sequence && (runtimeEpoch === epoch
        || current?.status !== 'ready' || configuredBaseUrl !== current.localApiUrl
        || configuredSessionToken !== current.sessionToken)) resetRuntimeCache()
    }
  },
  parentRestoreRecoveryHealth: (): Promise<{status:string;normal_business_ready?:boolean}> => request('/v1/health', {cache:'no-store'}),
  parentRestoreRecoveryStatus: (): Promise<import('./ParentRestoreRecovery').ParentRestoreStatus> => requestParentLocal('/v1/parent/recovery-status', {cache:'no-store'}),
  bootstrap: (): Promise<BootstrapState> => request('/v1/bootstrap'),
  startSession: (childId: string): Promise<DailySession> =>
    request('/v1/sessions', {
      method: 'POST',
      body: JSON.stringify({ child_id: childId, client_action_id: crypto.randomUUID() })
    }),
  switchSessionChild: (sessionId: string, targetChildId: string, clientActionId?: string): Promise<DailySession> =>
    request(`/v1/sessions/${sessionId}/switch-child`, {
      method: 'POST',
      body: JSON.stringify({ target_child_id: targetChildId, client_action_id: clientActionId ?? crypto.randomUUID() })
    }),
  sessionWorkspace: (sessionId: string): Promise<SessionWorkspaceState> =>
    observationIntents.workspace(sessionId, () => request<SessionWorkspaceState>(`/v1/sessions/${sessionId}`)),
  sessionVoiceCalibrationSummary: (sessionId: string): Promise<VoiceCalibrationSummary> =>
    request(`/v1/sessions/${sessionId}/voice-calibration`),
  saveSessionDraft: (sessionId: string, section: SessionDraftSection, payload: Record<string, unknown>): Promise<SessionDraft & { section: SessionDraftSection }> =>
    request(`/v1/sessions/${sessionId}/drafts/${section}`, {
      method: 'PUT',
      body: JSON.stringify({ payload })
    }),
  clearSessionDraft: (sessionId: string, section: SessionDraftSection): Promise<{ section: SessionDraftSection; deleted: boolean }> =>
    request(`/v1/sessions/${sessionId}/drafts/${section}`, { method: 'DELETE' }),
  transition: (
    sessionId: string,
    expectedState: string,
    targetState: string,
    reason: string
  ): Promise<DailySession> =>
    request(`/v1/sessions/${sessionId}/transition`, {
      method: 'POST',
      body: JSON.stringify({
        expected_state: expectedState,
        target_state: targetState,
        reason,
        client_action_id: crypto.randomUUID()
      })
    }),
  addRecall: (sessionId: string, subject: string, summary: string): Promise<ClassroomRecall> =>
    request(`/v1/sessions/${sessionId}/recalls`, {
      method: 'POST',
      body: JSON.stringify({ subject, summary, client_action_id: crypto.randomUUID() })
    }),
  declareNoRecall: (sessionId: string): Promise<DailySession> =>
    request(`/v1/sessions/${sessionId}/recalls/none`, {
      method: 'POST',
      body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  reviewRecall: (sessionId: string, recallId: string, decision: 'confirm' | 'reject'): Promise<ClassroomRecall> =>
    request(`/v1/sessions/${sessionId}/recalls/${recallId}/review`, {
      method: 'POST',
      body: JSON.stringify({ decision, client_action_id: crypto.randomUUID() })
    }),
  addHomework: (
    sessionId: string,
    subject: string,
    title: string,
    expectedMinutes: number,
    details: { taskType?: string; rangeText?: string; dueAt?: string; specialRequirements?: string; sourceArtifactId?: string; ocrConfidence?: number } = {}
  ): Promise<HomeworkItem> =>
    request(`/v1/sessions/${sessionId}/homework`, {
      method: 'POST',
      body: JSON.stringify({
        subject,
        title,
        expected_minutes: expectedMinutes,
        task_type: details.taskType ?? 'written',
        range_text: details.rangeText || null,
        due_at: details.dueAt || null,
        special_requirements: details.specialRequirements || null,
        source_artifact_id: details.sourceArtifactId || null,
        ocr_confidence: details.ocrConfidence ?? null,
        client_action_id: crypto.randomUUID()
      })
    }),
  declareNoHomework: (sessionId: string): Promise<DailySession> =>
    request(`/v1/sessions/${sessionId}/homework/none`, {
      method: 'POST',
      body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  updateHomework: (
    sessionId: string,
    homeworkId: string,
    values: { subject: string; title: string; expectedMinutes: number; taskType: string; rangeText: string; dueAt: string; specialRequirements: string; sourceArtifactId?: string; ocrConfidence?: number }
  ): Promise<HomeworkItem> =>
    request(`/v1/sessions/${sessionId}/homework/${homeworkId}`, {
      method: 'PUT',
      body: JSON.stringify({
        subject: values.subject,
        title: values.title,
        expected_minutes: values.expectedMinutes,
        task_type: values.taskType,
        range_text: values.rangeText || null,
        due_at: values.dueAt || null,
        special_requirements: values.specialRequirements || null,
        source_artifact_id: values.sourceArtifactId || null,
        ocr_confidence: values.ocrConfidence ?? null,
        client_action_id: crypto.randomUUID()
      })
    }),
  reviewDeferredTask: (
    sessionId: string,
    sourcePlanItemId: string,
    decision: 'continue_today' | 'completed_elsewhere' | 'cancelled',
    reason: string,
    requestId: string = crypto.randomUUID(),
    sourceVersion?: string,
  ): Promise<import('./types').DeferredTaskDecision & {child_id: string; target_session_id: string}> =>
    request(`/v1/sessions/${sessionId}/deferred-tasks/${sourcePlanItemId}/review`, {
      method: 'POST',
      body: JSON.stringify({ decision, reason, client_action_id: requestId, source_version: sourceVersion })
    }),
  savePlanningContext: (
    sessionId: string,
    values: {
      energyLevel: 'low' | 'medium' | 'high'
      fixedCommitmentMinutes: number
      fixedCommitmentNote: string
      tomorrowEventSubject: string
      tomorrowEventTitle: string
      tomorrowEventMinutes: number
    }
  ): Promise<void> =>
    request(`/v1/sessions/${sessionId}/planning-context`, {
      method: 'PUT',
      body: JSON.stringify({
        energy_level: values.energyLevel,
        fixed_commitment_minutes: values.fixedCommitmentMinutes,
        fixed_commitment_note: values.fixedCommitmentNote || null,
        tomorrow_event_subject: values.tomorrowEventTitle ? values.tomorrowEventSubject : null,
        tomorrow_event_title: values.tomorrowEventTitle || null,
        tomorrow_event_minutes: values.tomorrowEventTitle ? values.tomorrowEventMinutes : 0,
        client_action_id: crypto.randomUUID()
      })
    }),
  generatePlan: (sessionId: string): Promise<SessionWorkspaceState> =>
    request(`/v1/sessions/${sessionId}/plan/generate`, {
      method: 'POST',
      body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  reorderPlan: (sessionId: string, strategy: 'school_first' | 'short_school_first', reason: string): Promise<SessionWorkspaceState> =>
    request(`/v1/sessions/${sessionId}/plan/reorder`, {
      method: 'POST',
      body: JSON.stringify({ strategy, reason, client_action_id: crypto.randomUUID() })
    }),
  startPlanItem: (planItemId: string): Promise<void> =>
    request(`/v1/plan-items/${planItemId}/start`, {
      method: 'POST',
      body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  checkpointPlanItem: (planItemId: string): Promise<void> =>
    request(`/v1/plan-items/${planItemId}/checkpoint`, { method: 'POST' }),
  completePlanItem: (
    planItemId: string,
    actualMinutes: number,
    evidence: { type: 'child_confirmed' | 'child_checked' | 'needs_correction' | 'voice_confirmed'; note?: string; checklist?: string[]; artifactId?: string } = { type: 'child_confirmed' }
  ): Promise<void> =>
    request(`/v1/plan-items/${planItemId}/complete`, {
      method: 'POST',
      body: JSON.stringify({
        actual_minutes: actualMinutes,
        completion_evidence_type: evidence.type,
        completion_note: evidence.note || null,
        completion_checklist: evidence.checklist ?? [],
        completion_artifact_id: evidence.artifactId ?? null,
        client_action_id: crypto.randomUUID()
      })
    }),
  pausePlanItem: (planItemId: string): Promise<void> =>
    request(`/v1/plan-items/${planItemId}/pause`, {
      method: 'POST',
      body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  resumePlanItem: (planItemId: string): Promise<void> =>
    request(`/v1/plan-items/${planItemId}/resume`, {
      method: 'POST',
      body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  reschedulePlanItem: (planItemId: string, reason: string): Promise<void> =>
    request(`/v1/plan-items/${planItemId}/reschedule`, {
      method: 'POST',
      body: JSON.stringify({ reason, client_action_id: crypto.randomUUID() })
    }),
  beginBreak: (sessionId: string): Promise<SessionWorkspaceState> =>
    request(`/v1/sessions/${sessionId}/break`, {
      method: 'POST',
      body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  resumeSession: (sessionId: string): Promise<SessionWorkspaceState> =>
    request(`/v1/sessions/${sessionId}/resume`, {
      method: 'POST',
      body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  sessionLearningSummary: (sessionId: string): Promise<ChildLearningSummary> =>
    request(`/v1/sessions/${sessionId}/learning-summary`),
  dailyLearningSummary: (childId: string): Promise<ChildLearningSummary> =>
    request(`/v1/children/${childId}/daily-learning-summary`),
  completeReflection: async (
    sessionId: string,
    payload: { learnedText: string; difficultText: string; feeling: string; nextAction: string; skipped?: boolean }
  ): Promise<SessionWorkspaceState> => {
    try {
      return await request(`/v1/sessions/${sessionId}/reflection/complete`, {
        method: 'POST',
        body: JSON.stringify({
          learned_text: payload.learnedText,
          difficult_text: payload.difficultText,
          feeling: payload.feeling,
          next_action: payload.nextAction,
          skipped: payload.skipped ?? false,
          client_action_id: `reflection-close:${sessionId}`
        })
      })
    } catch (error) {
      // A lost HTTP response must not trap a durably completed session in reflection.
      try {
        const saved = await request<SessionWorkspaceState>(`/v1/sessions/${sessionId}`)
        if (saved.session.session_id === sessionId && saved.session.status === 'completed') return saved
      } catch { /* Keep the original error when the authoritative state is unavailable. */ }
      throw error
    }
  },
  askTutor: (sessionId: string, subject: string, question: string, hintLevel = 1, parentTurnId?: string, clientActionId: string = crypto.randomUUID(), confirmUnknownOutcomeRetry = false, questionReference?: { prompt_id: string; item_version: string }): Promise<TutorTurn> =>
    request(`/v1/sessions/${sessionId}/tutor`, {
      method: 'POST',
      body: JSON.stringify({ subject, question, hint_level: hintLevel, parent_turn_id: parentTurnId, client_action_id: clientActionId, confirm_unknown_outcome_retry: confirmUnknownOutcomeRetry,
        ...(questionReference ? { prompt_id: questionReference.prompt_id, item_version: questionReference.item_version } : {}) })
    }),
  askTutorFollowup: (sessionId: string, sourceTurnId: string, question: string, clientActionId: string, confirmUnknownOutcomeRetry = false): Promise<TutorTurn> =>
    request(`/v1/sessions/${sessionId}/tutor-followup`, {
      method: 'POST',
      body: JSON.stringify({ source_turn_id: sourceTurnId, question, client_action_id: clientActionId, confirm_unknown_outcome_retry: confirmUnknownOutcomeRetry })
    }),
  saveTutorFeedback: (turnId: string, responseText: string, outcome: 'understood' | 'still_stuck' | 'answer_attempt', clientActionId: string = crypto.randomUUID()): Promise<TutorFeedback> =>
    request(`/v1/tutor-turns/${turnId}/feedback`, {
      method: 'POST',
      body: JSON.stringify({ response_text: responseText, outcome, client_action_id: clientActionId })
    }),
  parentStatus: (): Promise<ParentStatus> => request('/v1/parent/status'),
  installationSetupStatus: (): Promise<import('./types').InstallationSetupStatus> =>
    request('/v1/install-setup/status', { cache: 'no-store' }),
  parentToday: (): Promise<ParentToday> =>
    request('/v1/parent/today', { headers: { 'X-HSC-Parent': parentToken } }),
  parentDashboard: (): Promise<ParentDashboard> =>
    request('/v1/parent/dashboard', { headers: { 'X-HSC-Parent': parentToken } }),
  companionMemories: (childId: string, query: string): Promise<{ items: import('./CompanionMemoryPanel').Memory[]; cloud_transmission_allowed: false }> =>
    request(`/v1/parent/children/${encodeURIComponent(childId)}/companion-memory?query=${encodeURIComponent(query)}`, { headers: { 'X-HSC-Parent': parentToken } }),
  reviewTutorOutcome: (childId: string, outcomeId: string, decision: 'revoke' | 'restore', reason: string): Promise<{ model: LongitudinalChildModel }> =>
    request(`/v1/parent/children/${encodeURIComponent(childId)}/tutor-outcomes/${encodeURIComponent(outcomeId)}/review`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ decision, reason, client_action_id: crypto.randomUUID() }) }),
  forgetCompanionMemory: async (childId: string, memoryId: string): Promise<void> => {
    await request(`/v1/parent/children/${encodeURIComponent(childId)}/companion-memory/${encodeURIComponent(memoryId)}`, {
      method: 'DELETE', headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    })
  },
  behaviorReplay: (childId: string): Promise<BehaviorReplay> =>
    request(`/v1/children/${childId}/behavior/replay`, { headers: { 'X-HSC-Parent': parentToken } }),
  behaviorBaseline: (childId: string): Promise<BehaviorBaseline> =>
    request(`/v1/children/${childId}/behavior/baseline`, { headers: { 'X-HSC-Parent': parentToken } }),
  behaviorPersonalization: (childId: string): Promise<BehaviorPersonalization> =>
    request(`/v1/children/${childId}/behavior/personalization`, { headers: { 'X-HSC-Parent': parentToken } }),
  behaviorHabitExperiments: (childId: string): Promise<HabitExperimentList> =>
    request(`/v1/children/${childId}/behavior/habit-experiments`, { headers: { 'X-HSC-Parent': parentToken } }),
  stopBehaviorHabitExperiment: (childId: string, experimentId: string): Promise<HabitExperiment> =>
    request(`/v1/children/${childId}/behavior/habit-experiments/${experimentId}/stop`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ client_action_id: crypto.randomUUID() }) }),
  resetBehaviorPersonalization: (childId: string): Promise<BehaviorPersonalization> =>
    request(`/v1/children/${childId}/behavior/personalization/reset`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ client_action_id: crypto.randomUUID() }) }),
  resetSessionBehaviorPersonalization: (sessionId: string): Promise<BehaviorPersonalization> =>
    request(`/v1/sessions/${sessionId}/behavior/personalization/reset`, { method: 'POST', body: JSON.stringify({ client_action_id: crypto.randomUUID() }) }),
  pendingSessionBehaviorIntervention: (sessionId: string): Promise<PendingBehaviorInterventionResponse> =>
    request(`/v1/sessions/${sessionId}/behavior/interventions/pending`),
  proactiveAgentPrompt: (sessionId: string): Promise<{
    status: string
    prompt: null | { prompt_id: string; source_type: string; message: string; expires_at: string }
  }> => request(`/v1/sessions/${sessionId}/agent/proactive-prompt`),
  proactiveAgentPromptCurrent: (sessionId: string, promptId: string, signal?: AbortSignal): Promise<{ prompt_id: string; current: boolean }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/agent/proactive-prompts/${encodeURIComponent(promptId)}/current`, { cache: 'no-store', signal }),
  acknowledgeProactiveAgentPrompt: (sessionId: string, promptId: string): Promise<{ prompt_id: string; status: string }> =>
    request(`/v1/sessions/${sessionId}/agent/proactive-prompts/${promptId}/acknowledge`, { method: 'POST' }),
  startCompanionActivity: (sessionId: string, activityType: 'interactive_story' | 'word_chain' | 'riddle' | 'movement' | 'tidy_challenge', topic: string, clientActionId: string): Promise<Record<string, unknown>> =>
    request(`/v1/sessions/${sessionId}/companion-activities/start`, {
      method: 'POST', body: JSON.stringify({ activity_type: activityType, topic, client_action_id: clientActionId })
    }),
  endCompanionActivity: (sessionId: string, reason = 'child_requested'): Promise<Record<string, unknown>> =>
    request(`/v1/sessions/${sessionId}/companion-activities/end`, {
      method: 'POST', body: JSON.stringify({ reason })
    }),
  startControlledContent: (sessionId: string, query: string, clientActionId: string, selectionBinding?: {resource_id:string;source_version:string}, nativeIntentId?:string,nativeEpoch?:string): Promise<ControlledContentPlayback> =>
    request(`/v1/sessions/${sessionId}/controlled-content/start`, {
      method: 'POST', body: JSON.stringify({ query, client_action_id: clientActionId, ...(selectionBinding ? {selection_binding:selectionBinding} : {}), ...(nativeIntentId?{native_intent_id:nativeIntentId}:{}),...(nativeEpoch?{native_epoch:nativeEpoch}:{}) })
    }),
  endControlledContent: (sessionId: string, reason = 'child_requested', actualSeconds = 0, contentSessionId?: string): Promise<ControlledContentPlayback | { status: 'no_active_content' }> =>
    request(`/v1/sessions/${sessionId}/controlled-content/end`, {
      method: 'POST', body: JSON.stringify({ reason, actual_seconds: actualSeconds, content_session_id: contentSessionId })
    }),
  activeControlledContent: (sessionId: string): Promise<{ playback: ControlledContentPlayback | null }> =>
    request(`/v1/sessions/${sessionId}/controlled-content/active`),
  controlledContentRule: (childId: string): Promise<ControlledContentRule> =>
    request(`/v1/parent/children/${childId}/controlled-content/rule`, { headers: { 'X-HSC-Parent': parentToken } }),
  updateControlledContentRule: (childId: string, values: Pick<ControlledContentRule, 'enabled' | 'max_daily_minutes' | 'max_item_minutes'>): Promise<ControlledContentRule> =>
    request(`/v1/parent/children/${childId}/controlled-content/rule`, { method: 'PUT', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values) }),
  controlledContentCandidates: (childId: string, query = ''): Promise<ControlledContentCandidate[]> =>
    request<{ items: ControlledContentCandidate[] }>(`/v1/parent/children/${childId}/controlled-content/candidates?q=${encodeURIComponent(query)}`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  approveControlledContent: (childId: string, rowId: number, maxMinutes: number, note = ''): Promise<ControlledContentCandidate> =>
    request(`/v1/parent/children/${childId}/controlled-content/approvals`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ curriculum_row_id: rowId, max_minutes: maxMinutes, note }) }),
  revokeControlledContent: (childId: string, rowId: number): Promise<{ row_id: number; status: 'revoked' }> =>
    request(`/v1/parent/children/${childId}/controlled-content/approvals/${rowId}`, { method: 'DELETE', headers: { 'X-HSC-Parent': parentToken } }),
  startControlledGame: (sessionId: string, query: string, clientActionId: string, selectionBinding?: {resource_id:string;source_version:string}, nativeIntentId?:string,nativeEpoch?:string): Promise<ControlledGameSession> =>
    request(`/v1/sessions/${sessionId}/controlled-games/start`, {
      method: 'POST', body: JSON.stringify({ query, client_action_id: clientActionId, ...(selectionBinding ? {selection_binding:selectionBinding} : {}), ...(nativeIntentId?{native_intent_id:nativeIntentId}:{}),...(nativeEpoch?{native_epoch:nativeEpoch}:{}) })
    }),
  bindControlledGameProcess: (sessionId: string, gameSessionId: string, osProcessId: number): Promise<ControlledGameSession> =>
    request(`/v1/sessions/${sessionId}/controlled-games/${gameSessionId}/process`, {
      method: 'POST', body: JSON.stringify({ os_process_id: osProcessId })
    }),
  endControlledGame: (sessionId: string, reason = 'child_requested', actualSeconds = 0, gameSessionId?: string): Promise<ControlledGameSession | { status: 'no_active_game' }> =>
    request(`/v1/sessions/${sessionId}/controlled-games/end`, {
      method: 'POST', body: JSON.stringify({ reason, actual_seconds: actualSeconds, game_session_id: gameSessionId })
    }),
  activeControlledGame: (sessionId: string): Promise<{ game: ControlledGameSession | null }> =>
    request(`/v1/sessions/${sessionId}/controlled-games/active`),
  controlledGameRule: (childId: string): Promise<ControlledGameRule> =>
    request(`/v1/parent/children/${childId}/controlled-games/rule`, { headers: { 'X-HSC-Parent': parentToken } }),
  updateControlledGameRule: (childId: string, values: Pick<ControlledGameRule, 'enabled' | 'max_daily_minutes' | 'max_item_minutes'>): Promise<ControlledGameRule> =>
    request(`/v1/parent/children/${childId}/controlled-games/rule`, { method: 'PUT', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values) }),
  controlledGameApprovals: (childId: string): Promise<ControlledGameApproval[]> =>
    request<{ items: ControlledGameApproval[] }>(`/v1/parent/children/${childId}/controlled-games/approvals`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  approveControlledGame: (childId: string, values: { title: string; executable_path: string; max_minutes: number; note?: string }): Promise<ControlledGameApproval> =>
    request(`/v1/parent/children/${childId}/controlled-games/approvals`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values) }),
  revokeControlledGame: (childId: string, approvalId: string): Promise<{ approval_id: string; status: 'revoked' }> =>
    request(`/v1/parent/children/${childId}/controlled-games/approvals/${approvalId}`, { method: 'DELETE', headers: { 'X-HSC-Parent': parentToken } }),
  respondToSessionBehaviorIntervention: (sessionId: string, interventionId: string, accepted: boolean, note: string | undefined, clientActionId: string): Promise<{ intervention_id: string; feedback: 'accepted' | 'declined' }> =>
    request(`/v1/sessions/${sessionId}/behavior/interventions/${interventionId}/feedback`, {
      method: 'POST', body: JSON.stringify({ accepted, note, client_action_id: clientActionId })
    }),
  retractBehaviorEvidence: (childId: string, stateId: string, note: string): Promise<{ state_id: string; status: string }> =>
    request(`/v1/children/${childId}/behavior/states/${stateId}/retract`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ note, client_action_id: crypto.randomUUID() }) }),
  ocrReviewQueue: (childId: string): Promise<OcrReviewItem[]> =>
    request<{ items: OcrReviewItem[] }>(`/v1/children/${childId}/ocr-review-queue`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  ocrReviewHistory: (childId: string): Promise<OcrReviewItem[]> =>
    request<{ items: OcrReviewItem[] }>(`/v1/children/${childId}/ocr-review-history`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  confirmOcrReview: (reviewId: string, childId: string, correctedText: string, clientActionId: string = crypto.randomUUID()): Promise<OcrReviewItem> =>
    request(`/v1/ocr-review-items/${reviewId}`, { method: 'PUT', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ child_id: childId, corrected_text: correctedText, client_action_id: clientActionId }) }),
  skipOcrReview: (reviewId: string, childId: string, clientActionId: string = crypto.randomUUID()): Promise<OcrReviewItem> =>
    request(`/v1/ocr-review-items/${reviewId}/skip`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ child_id: childId, client_action_id: clientActionId }) }),
  undoOcrReview: (reviewId: string, childId: string, clientActionId: string = crypto.randomUUID()): Promise<OcrReviewItem> =>
    request(`/v1/ocr-review-items/${reviewId}/undo`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ child_id: childId, client_action_id: clientActionId }) }),
  batchOcrReview: (childId: string, reviewIds: string[], decision: 'confirm_proposed' | 'skip', clientActionId: string = crypto.randomUUID()): Promise<{ items: OcrReviewItem[]; processed_count: number; remaining_count: number; decision: 'confirm_proposed' | 'skip' }> =>
    request('/v1/ocr-review-items/batch', { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ child_id: childId, review_ids: reviewIds, decision, client_action_id: clientActionId }) }),
  safetyEvent: (childId: string, safetyEventId: string): Promise<ChildSafetyEvent> =>
    request(`/v1/parent/children/${encodeURIComponent(childId)}/safety-events/${encodeURIComponent(safetyEventId)}`, { headers: { 'X-HSC-Parent': parentToken } }),
  acknowledgeSafetyEvent: (safetyEventId: string): Promise<ChildSafetyEvent> =>
    request(`/v1/parent/safety-events/${safetyEventId}/acknowledge`, {
      method: 'POST',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  modelCalls: (childId: string, limit = 20): Promise<ModelCallAudit[]> =>
    request<{ items: ModelCallAudit[] }>(`/v1/parent/children/${childId}/model-calls?limit=${limit}`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  executionTraces: (childId: string, before: string | null = null): Promise<ExecutionTracePage> =>
    request(`/v1/parent/children/${encodeURIComponent(childId)}/execution-traces?limit=20${before ? `&before=${encodeURIComponent(before)}` : ''}`, { headers: { 'X-HSC-Parent': parentToken } }),
  executionTrace: (childId: string, turnId: string): Promise<ExecutionTraceDetail> =>
    request(`/v1/parent/children/${encodeURIComponent(childId)}/execution-traces/${encodeURIComponent(turnId)}`, { headers: { 'X-HSC-Parent': parentToken } }),
  modelPricing: (): Promise<ModelPricing[]> =>
    request<{ items: ModelPricing[] }>('/v1/parent/model-pricing', { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  saveModelPricing: (values: Pick<ModelPricing, 'provider' | 'model' | 'input_price_cny_per_million' | 'output_price_cny_per_million'>): Promise<ModelPricing> =>
    request('/v1/parent/model-pricing', { method: 'PUT', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values) }),
  modelRuntimePolicy: (): Promise<ModelRuntimePolicyStatus> =>
    request('/v1/parent/model-runtime-policy', { headers: { 'X-HSC-Parent': parentToken } }),
  saveModelRuntimePolicy: (values: ModelRuntimePolicyStatus['policy']): Promise<ModelRuntimePolicyStatus> =>
    request('/v1/parent/model-runtime-policy', { method: 'PUT', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values) }),
  strategyExperiments: (childId: string): Promise<StrategyExperiment[]> =>
    request(`/v1/children/${childId}/strategy-experiments`, { headers: { 'X-HSC-Parent': parentToken } }),
  createStrategyCandidate: (childId: string): Promise<StrategyExperiment> =>
    request(`/v1/children/${childId}/strategy-experiments/candidate`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }),
  replayStrategy: (experimentId: string): Promise<StrategyExperiment> =>
    request(`/v1/strategy-experiments/${experimentId}/replay`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }),
  shadowStrategy: (experimentId: string): Promise<StrategyExperiment> =>
    request(`/v1/strategy-experiments/${experimentId}/shadow`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }),
  activateStrategy: (experimentId: string): Promise<StrategyExperiment> =>
    request(`/v1/strategy-experiments/${experimentId}/activate`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }),
  rollbackStrategy: (experimentId: string, reason: string): Promise<StrategyExperiment> =>
    request(`/v1/strategy-experiments/${experimentId}/rollback`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ reason }) }),
  parentUnlock: async (pin: string, setup: boolean, source?: symbol): Promise<void> => {
    const epoch = runtimeEpoch, authority=parentDraftAuthorityVersion()
    const result = await request<{ parent_token: string }>(setup ? '/v1/parent/setup' : '/v1/parent/unlock', {
      method: 'POST',
      body: JSON.stringify({ pin })
    })
    if (runtimeEpoch !== epoch||authority!==parentDraftAuthorityVersion()) throw new ApiError('本地学习服务已切换，请重新解锁家长中心。', undefined, 'LOCAL_SERVICE_CHANGED')
    parentToken = result.parent_token
    changeParentDraftAuthority('unlocked', source)
    parentRecoveryUnlockAuthority=parentDraftAuthorityVersion()
  },
  continueQwenRotationRecovery: async (): Promise<void> => {
    const token = parentToken, epoch = runtimeEpoch, authority = parentDraftAuthorityVersion()
    const current = (): boolean => !!token && token === parentToken && epoch === runtimeEpoch
      && authority === parentRecoveryUnlockAuthority && authority === parentDraftAuthorityVersion()
    if (!current() || !window.desktop?.rotationRecoveryContinue) throw new Error('ROTATION_PARENT_AUTH_REQUIRED')
    const runtime = await window.desktop.getRuntimeConfig()
    if (!current() || runtime.status !== 'ready' || runtime.rotationAuthOnly !== true) throw new Error('ROTATION_AUTH_CHANGED')
    // Main independently verifies parent authority, process epoch and the pending payload.
    const result = await window.desktop.rotationRecoveryContinue(token)
    if (!current()) throw new Error('ROTATION_AUTH_CHANGED')
    if (result?.status === 'restart_required') throw new Error('ROTATION_RECOVERY_RESTART_REQUIRED')
    if (result?.status !== 'completed') throw new Error('ROTATION_RECOVERY_UNKNOWN')
    resetRuntimeCache('rotation_complete')
    window.dispatchEvent(new Event('hsc-rotation-pending-changed'))
  },
  knowledgeGraphRequest: (path: string, init?: RequestInit): Promise<unknown> =>
    request(path, {
      ...init,
      headers: { ...(init?.headers ?? {}), 'X-HSC-Parent': parentToken }
    }),
  childReports: (childId: string): Promise<DailyReport[]> =>
    request<{ items: DailyReport[] }>(`/v1/children/${childId}/reports`, {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items),
  variantCandidates: (childId: string, status = 'needs_review'): Promise<VariantCandidate[]> =>
    request<{ items: VariantCandidate[] }>(`/v1/parent/children/${childId}/variant-candidates?status=${encodeURIComponent(status)}`, {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items),
  variantGenerationJobs: (childId: string): Promise<VariantGenerationJob[]> =>
    request<{ jobs: VariantGenerationJob[] }>(`/v1/parent/children/${childId}/variant-candidates?status=needs_review`, {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.jobs),
  retryVariantGeneration: (childId: string, jobId: string, actionId: string): Promise<VariantGenerationJob> =>
    request(`/v1/parent/children/${childId}/variant-jobs/${jobId}/retry`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ client_action_id: actionId })
    }),
  reviewVariantCandidate: (candidateId: string, decision: 'approve' | 'reject', note: string): Promise<VariantCandidate> =>
    request(`/v1/parent/variant-candidates/${candidateId}/review`, {
      method: 'POST',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ decision, note, client_action_id: crypto.randomUUID() })
    }),
  specializedSupplyGaps: (childId: string, track: 'pet' | 'competition_math' | 'junior_exam'): Promise<{ child_id: string; track: string; target_per_domain: number; domains: SpecializedSupplyGap[]; ready_domain_count: number; total_domain_count: number }> =>
    request(`/v1/parent/children/${childId}/specialized-supply/gaps?track=${encodeURIComponent(track)}`, { headers: { 'X-HSC-Parent': parentToken } }),
  prepareSpecializedSupply: (childId: string, track: 'pet' | 'competition_math' | 'junior_exam', targetPerDomain = 4): Promise<{ created_count: number }> =>
    request(`/v1/parent/children/${childId}/specialized-supply/prepare`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ track, target_per_domain: targetPerDomain, client_action_id: crypto.randomUUID() })
    }),
  examBlueprintReadiness: (childId: string, track: 'pet' | 'competition_math' | 'junior_exam', subject: string): Promise<ExamBlueprintReadiness> =>
    request(`/v1/parent/children/${childId}/exam-blueprint-readiness?track=${encodeURIComponent(track)}&subject=${encodeURIComponent(subject)}`, { headers: { 'X-HSC-Parent': parentToken } }),
  prepareExamBlueprint: (childId: string, track: 'pet' | 'competition_math' | 'junior_exam', subject: string, blueprintId?: string): Promise<{ created_count: number; skipped: Array<{ domain_id: string; reason: string }>; readiness: ExamBlueprintReadiness }> =>
    request(`/v1/parent/children/${childId}/exam-blueprint-readiness/prepare`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ track, subject, blueprint_id: blueprintId ?? null, client_action_id: crypto.randomUUID() })
    }),
  specializedSupplyCandidates: (childId: string): Promise<SpecializedSupplyCandidate[]> =>
    request<{ items: SpecializedSupplyCandidate[] }>(`/v1/parent/children/${childId}/specialized-supply/candidates`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  reviewSpecializedSupplyCandidate: (candidateId: string, decision: 'approve' | 'reject', note: string): Promise<SpecializedSupplyCandidate> =>
    request(`/v1/parent/specialized-supply/candidates/${candidateId}/review`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ decision, note, client_action_id: crypto.randomUUID() })
    }),
  longitudinalChildModel: (childId: string): Promise<LongitudinalChildModel> =>
    request(`/v1/parent/children/${childId}/longitudinal-model`, { headers: { 'X-HSC-Parent': parentToken } }),
  childModelCorrections: (childId: string, includeInactive = true): Promise<ChildModelCorrection[]> =>
    request<{ items: ChildModelCorrection[] }>(`/v1/parent/children/${childId}/longitudinal-model/corrections?include_inactive=${includeInactive}`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  createChildModelCorrection: (childId: string, values: {
    target_key: ChildModelCorrectionTarget
    value: string | number
    reason: string
    expires_at: string | null
  }): Promise<{ correction: ChildModelCorrection; model: LongitudinalChildModel }> =>
    request(`/v1/parent/children/${childId}/longitudinal-model/corrections`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ ...values, client_action_id: crypto.randomUUID() })
    }),
  revokeChildModelCorrection: (correctionId: string, reason: string): Promise<{ correction: ChildModelCorrection; model: LongitudinalChildModel }> =>
    request(`/v1/parent/longitudinal-model/corrections/${correctionId}/revoke`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ reason, client_action_id: crypto.randomUUID() })
    }),
  goalProgramCycles: (childId: string): Promise<{ items: GoalProgramCycle[]; next_daily_action: null | { subject: string; title: string; minutes: number; completion_standard: string } }> =>
    request(`/v1/parent/children/${childId}/goal-programs`, { headers: { 'X-HSC-Parent': parentToken } }),
  goalRuleReview: (childId: string, track: string): Promise<GoalRuleReview> =>
    request(`/v1/parent/children/${childId}/goal-programs/${encodeURIComponent(track)}/rule-review`, { headers: { 'X-HSC-Parent': parentToken } }),
  confirmGoalRules: (childId: string, track: string, values: GoalRuleConfirmation): Promise<GoalRuleConfirmationResult> =>
    request(`/v1/parent/children/${childId}/goal-programs/${encodeURIComponent(track)}/rule-review`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values)
    }),
  configureGoalProgram: (childId: string, track: string, values: {
    target_statement: string
    target_date: string | null
    target_score: number | null
    target_score_label: string | null
    event_name?: string | null
    event_rules_url?: string | null
    event_version?: string | null
    award_target?: string | null
    registration_deadline?: string | null
    weekly_minutes: number
    weekly_sessions: number
  }): Promise<{ items: GoalProgramCycle[]; next_daily_action: null | { subject: string; title: string; minutes: number; completion_standard: string } }> =>
    request(`/v1/parent/children/${childId}/goal-programs/${encodeURIComponent(track)}`, {
      method: 'PUT',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ ...values, client_action_id: crypto.randomUUID() })
    }),
  specializedRoadmaps: (childId: string): Promise<SpecializedRoadmap[]> =>
    request<{ items: SpecializedRoadmap[] }>(`/v1/parent/children/${childId}/specialized-roadmaps`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  retryChildReport: (childId: string): Promise<DailyReport> =>
    request<DailyReport>(`/v1/parent/children/${childId}/reports/retry`, {
      method: 'POST',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }).then((report) => {
      if (!report || report.child_id !== childId) {
        throw new ApiError('这次日报结果尚未确认，请重新打开家长中心核对。', undefined, 'REPORT_CHILD_SCOPE_CHANGED')
      }
      return report
    }),
  saveParentReportFeedback: (reportId: string, values: import('./types').ParentActionFeedbackInput): Promise<ParentReportFeedback> =>
    request(`/v1/parent/reports/${reportId}/feedback`, {
      method: 'PUT',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify(values)
    }),
  parentActions: (reportId: string): Promise<import('./types').ParentActionSnapshot[]> =>
    request<{ items: import('./types').ParentActionSnapshot[] }>(`/v1/parent/reports/${reportId}/actions`, {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items),
  parentAction: async (childId: string, snapshotId: string): Promise<import('./types').ParentActionSnapshot> => {
    const value = await request<import('./types').ParentActionSnapshot>(`/v1/parent/children/${encodeURIComponent(childId)}/parent-actions/${encodeURIComponent(snapshotId)}`, { headers: { 'X-HSC-Parent': parentToken } })
    if (value.child_id !== childId || value.snapshot_id !== snapshotId) throw new ApiError('行动响应与当前孩子或版本不一致。', 409, 'PARENT_ACTION_RESPONSE_MISMATCH')
    return value
  },
  saveParentActionFeedback: async (childId: string, snapshotId: string, values: import('./types').ParentActionFeedbackInput): Promise<ParentReportFeedback> => {
    const value = await request<ParentReportFeedback>(`/v1/parent/children/${encodeURIComponent(childId)}/parent-actions/${encodeURIComponent(snapshotId)}/feedback`, {
      method: 'PUT', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values),
    })
    if (value.child_id !== childId || value.action_snapshot_id !== snapshotId) throw new ApiError('反馈回执暂不能核对，请保留原请求重试。', 409, 'PARENT_ACTION_RESPONSE_MISMATCH')
    return value
  },
  reportSnapshots: (childId: string): Promise<ParentReportSnapshot[]> =>
    request<{ items: ParentReportSnapshot[] }>(`/v1/parent/children/${childId}/report-snapshots`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  editReportSnapshot: (snapshotId: string, values: { delivery_intro?: string; parent_note?: string; coaching_decision?: 'adopt' | 'custom' | 'skip'; coaching_script?: string; coaching_action?: string; coaching_duration_minutes?: number; coaching_observation?: string }): Promise<ParentReportSnapshot> =>
    request(`/v1/parent/report-snapshots/${snapshotId}`, { method: 'PUT', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values) }),
  carryReportSavedEdits: (snapshotId: string, values: { source_snapshot_id: string; source_edit_version: string; target_edit_version: string; client_action_id: string }): Promise<ParentReportSnapshot> =>
    request(`/v1/parent/report-snapshots/${encodeURIComponent(snapshotId)}/carry-saved-edits`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values) }),
  approveReportSnapshot: (snapshotId: string, values?: { delivery_intro?: string; parent_note?: string; coaching_decision?: 'adopt' | 'custom' | 'skip'; coaching_script?: string; coaching_action?: string; coaching_duration_minutes?: number; coaching_observation?: string }): Promise<ParentReportSnapshot> =>
    request(`/v1/parent/report-snapshots/${snapshotId}/approve`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: values ? JSON.stringify(values) : undefined }),
  revokeReportSnapshot: (snapshotId: string, reason: string): Promise<ParentReportSnapshot> =>
    request(`/v1/parent/report-snapshots/${snapshotId}/revoke`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ reason }) }),
  saveReportSnapshotFeedback: (snapshotId: string, values: { helpfulness: number | null; stress_rating: number | null; notes: string | null; client_action_id: string }): Promise<ParentReportSnapshot> =>
    request(`/v1/parent/report-snapshots/${snapshotId}/feedback`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values) }),
  reportFeedbackHistory: (childId: string, snapshotId: string): Promise<import('./types').ReportFeedbackRevision[]> =>
    request<{ items: import('./types').ReportFeedbackRevision[] }>(`/v1/parent/children/${childId}/report-snapshots/${snapshotId}/feedback-history`, { headers: { 'X-HSC-Parent': parentToken } }).then(result => result.items),
  reportPlanAdjustments: (childId: string, snapshotId: string): Promise<import('./reportPlanAdjustment').ReportPlanAdjustment[]> =>
    request<{ items: import('./reportPlanAdjustment').ReportPlanAdjustment[] }>(`/v1/parent/children/${childId}/report-snapshots/${snapshotId}/plan-adjustments`, { headers: { 'X-HSC-Parent': parentToken } }).then(result => result.items),
  proposeReportPlanAdjustment: (childId: string, snapshotId: string, values: import('./reportPlanAdjustment').ReportPlanProposal): Promise<import('./reportPlanAdjustment').ReportPlanAdjustment> =>
    request(`/v1/parent/children/${childId}/report-snapshots/${snapshotId}/plan-adjustments`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values) }),
  reviewReportPlanAdjustment: (childId: string, adjustmentId: string, revision: number, decision: import('./reportPlanAdjustment').ReportPlanDecision): Promise<import('./reportPlanAdjustment').ReportPlanAdjustment> =>
    request(`/v1/parent/children/${childId}/report-plan-adjustments/${adjustmentId}/review`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ expected_revision: revision, decision, client_action_id: `plan-review:${adjustmentId}:${revision}:${decision}` }) }),
  retractReportFeedback: (childId: string, snapshotId: string, revision: number): Promise<{ event_id: string }> =>
    request(`/v1/parent/children/${childId}/report-snapshots/${snapshotId}/feedback-retractions`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({
        expected_revision: revision, client_action_id: `report-feedback-retract:${snapshotId}:${revision}`, reason: '家长撤回本次评价'
      })
    }),
  requestReportSnapshotCorrection: (snapshotId: string, note: string): Promise<ParentReportSnapshot> =>
    request(`/v1/parent/report-snapshots/${snapshotId}/corrections`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ note }) }),
  retractPeriodEvidence: (childId: string, evidenceRef: string, note: string, clientActionId: string): Promise<{ updated_reports: number; failures: Array<{ report_type: string; error_code: string }> }> =>
    request(`/v1/parent/children/${childId}/period-evidence/${evidenceRef}/retract`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ note, client_action_id: clientActionId }) }),
  restorePeriodEvidence: (childId: string, evidenceRef: string, note: string, clientActionId: string): Promise<{ updated_reports: number; failures: Array<{ report_type: string; error_code: string }> }> =>
    request(`/v1/parent/children/${childId}/period-evidence/${evidenceRef}/restore`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ note, client_action_id: clientActionId }) }),
  retractedPeriodEvidence: (childId: string): Promise<Array<{ evidence_ref: string; subject: string; knowledge_title: string; summary: string; date: string; note: string }>> =>
    request<{ items: Array<{ evidence_ref: string; subject: string; knowledge_title: string; summary: string; date: string; note: string }> }>(`/v1/parent/children/${childId}/period-evidence/retracted`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  reportDeliveryChannels: (): Promise<Array<{ channel: 'email' | 'wechat_work_bot'; configured: boolean; credential_updated_at: string | null; requires_encryption_key: boolean }>> =>
    request<{ items: Array<{ channel: 'email' | 'wechat_work_bot'; configured: boolean; credential_updated_at: string | null; requires_encryption_key: boolean }> }>('/v1/parent/report-delivery/channels', { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  pilotDiagnosticStatus: (): Promise<{ recipient: string; consent: 'consented' | 'revoked' | 'not_authorized'; latest: { date: string; status: string } | null; scheduler_enabled: boolean; blocked_children: string[]; recipient_editable?: boolean; recipient_configuration_conflict?: boolean }> =>
    request('/v1/parent/pilot-diagnostic-consent', { headers: { 'X-HSC-Parent': parentToken } }),
  setPilotDiagnosticRecipient: (recipient: string, confirmation: string): Promise<{ recipient: string }> =>
    request('/v1/parent/pilot-diagnostic-recipient', { method: 'PUT', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ recipient, confirmation }) }),
  setPilotDiagnosticConsent: (recipient: string, consented: boolean): Promise<{ recipient: string; status: string }> =>
    request('/v1/parent/pilot-diagnostic-consent', { method: 'PUT', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ recipient, consented }) }),
  saveReportDeliveryChannel: (channel: 'email' | 'wechat_work_bot', config: Record<string, unknown>): Promise<void> =>
    request(`/v1/parent/report-delivery/channels/${channel}`, { method: 'PUT', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ config }) }).then(() => undefined),
  clearReportDeliveryChannel: clearReportDeliveryChannelWithDesktop,
  reportDeliveryChannelTests: (childId: string): Promise<Array<{ channel: 'email' | 'wechat_work_bot'; status: 'passed' | 'failed' | 'blocked' | 'processing' | 'unknown' | null; message: string; tested_at: string | null; client_action_id?: string; recipient_id?: string | null; verified_recipient_ids?: string[]; kept_without_resend?: boolean }>> =>
    request<{ items: Array<{ channel: 'email' | 'wechat_work_bot'; status: 'passed' | 'failed' | 'blocked' | 'processing' | 'unknown' | null; message: string; tested_at: string | null; client_action_id?: string; recipient_id?: string | null; verified_recipient_ids?: string[]; kept_without_resend?: boolean }> }>(`/v1/parent/children/${childId}/report-delivery/channel-tests`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  testReportDeliveryChannel: (childId: string, channel: 'email' | 'wechat_work_bot', clientActionId: string, recipientId?: string): Promise<{ channel: 'email' | 'wechat_work_bot'; status: 'passed' | 'failed' | 'blocked' | 'processing' | 'unknown'; message: string; tested_at: string | null; client_action_id?: string; recipient_id?: string | null; kept_without_resend?: boolean; historical_only?: boolean; current_verified?: boolean }> =>
    request(`/v1/parent/children/${childId}/report-delivery/channels/${channel}/test`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ client_action_id: clientActionId, ...(recipientId ? { recipient_id: recipientId } : {}) }) }),
  keepUnknownReportDeliveryChannelTest: (childId: string, channel: 'email' | 'wechat_work_bot', clientActionId: string): Promise<{ channel: 'email' | 'wechat_work_bot'; status: 'unknown'; message: string; tested_at: string; client_action_id: string; recipient_id?: string | null; kept_without_resend: boolean }> =>
    request(`/v1/parent/children/${childId}/report-delivery/channels/${channel}/test/keep`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ client_action_id: clientActionId }) }),
  reissueReportDeliveryChannelTest: (childId: string, channel: 'email' | 'wechat_work_bot', previousClientActionId: string, clientActionId: string, recipientId?: string): Promise<{ channel: 'email' | 'wechat_work_bot'; status: 'passed' | 'failed' | 'blocked' | 'unknown'; message: string; tested_at: string | null; client_action_id?: string; recipient_id?: string | null; historical_only?: boolean; current_verified?: boolean }> =>
    request(`/v1/parent/children/${childId}/report-delivery/channels/${channel}/test/reissue`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ previous_client_action_id: previousClientActionId, client_action_id: clientActionId, ...(recipientId ? { recipient_id: recipientId } : {}) }) }),
  reissueReauthorizedEmailTest: (childId: string, previousClientActionId: string, clientActionId: string, recipientId: string, confirmedAddress: string): Promise<{ channel: 'email'; status: 'passed' | 'failed' | 'blocked' | 'unknown'; message: string; tested_at: string | null; client_action_id?: string; recipient_id?: string | null; historical_only?: boolean; current_verified?: boolean }> =>
    request(`/v1/parent/children/${encodeURIComponent(childId)}/report-delivery/channels/email/test/reissue-reauthorized`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ previous_client_action_id: previousClientActionId, client_action_id: clientActionId, recipient_id: recipientId, confirmed_address: confirmedAddress }) }),
  reportRecipients: (childId: string): Promise<{ items: ReportDeliveryRecipient[]; preferences: { quiet_start: string; quiet_end: string; retention_days: number; redaction_level: 'standard' | 'minimal' } }> =>
    request(`/v1/parent/children/${childId}/report-delivery/recipients`, { headers: { 'X-HSC-Parent': parentToken } }),
  reportDeliveryStatus: (childId: string): Promise<{ scheduled: number; processing: number; failed: number; unknown: number; recovered: number }> =>
    request(`/v1/parent/children/${childId}/report-delivery/status`, { headers: { 'X-HSC-Parent': parentToken } }),
  addReportRecipient: (values: { child_id: string; channel: 'email' | 'wechat_work_bot'; address: string; label: string; consented: boolean }): Promise<ReportDeliveryRecipient> =>
    request('/v1/parent/report-delivery/recipients', { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values) }),
  revokeReportRecipient: (recipientId: string): Promise<ReportDeliveryRecipient> =>
    request(`/v1/parent/report-delivery/recipients/${recipientId}`, { method: 'DELETE', headers: { 'X-HSC-Parent': parentToken } }),
  saveReportDeliveryPreferences: (childId: string, values: { quiet_start: string; quiet_end: string; retention_days: number; redaction_level: 'standard' | 'minimal' }): Promise<{ quiet_start: string; quiet_end: string; retention_days: number; redaction_level: 'standard' | 'minimal' }> =>
    request(`/v1/parent/children/${childId}/report-delivery/preferences`, { method: 'PUT', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values) }),
  reportDeliveries: (snapshotId: string): Promise<ReportDelivery[]> =>
    request<{ items: ReportDelivery[] }>(`/v1/parent/report-snapshots/${snapshotId}/deliveries`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  scheduleReportDelivery: (snapshotId: string, recipientId: string, scheduledAt?: string): Promise<ReportDelivery> =>
    request(`/v1/parent/report-snapshots/${snapshotId}/deliveries`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ recipient_id: recipientId, scheduled_at: scheduledAt ?? null }) }),
  cancelReportDelivery: (deliveryId: string, reason: string): Promise<ReportDelivery> =>
    request(`/v1/parent/report-deliveries/${deliveryId}/cancel`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ reason }) }),
  retryReportDelivery: (deliveryId: string): Promise<ReportDelivery> =>
    request(`/v1/parent/report-deliveries/${deliveryId}/retry`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }),
  keepUnknownReportDelivery: (deliveryId: string): Promise<ReportDelivery> =>
    request(`/v1/parent/report-deliveries/${deliveryId}/unknown/keep`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }),
  reissueUnknownReportDelivery: (deliveryId: string, clientActionId: string): Promise<ReportDelivery> =>
    request(`/v1/parent/report-deliveries/${deliveryId}/unknown/reissue`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ client_action_id: clientActionId }) }),
  processReportDeliveries: (): Promise<ReportDelivery[]> =>
    request<{ items: ReportDelivery[] }>('/v1/parent/report-deliveries/process', { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  childLearningState: (childId: string): Promise<LearningState> =>
    request(`/v1/children/${childId}/learning-state`, {
      headers: { 'X-HSC-Parent': parentToken }
    }),
  masteryHistory: (childId: string, knowledgeNodeId: string): Promise<MasteryHistoryItem[]> =>
    request<{ items: MasteryHistoryItem[] }>(`/v1/children/${childId}/learning-state/${encodeURIComponent(knowledgeNodeId)}/history`, {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items),
  diagnosticPrompt: (sessionId: string, promptId?: string): Promise<import('./types').DiagnosticPromptResult> =>
    trackDiagnosticCapture(sessionId, () => request(`/v1/sessions/${sessionId}/diagnostic-prompt${promptId ? `?prompt_id=${encodeURIComponent(promptId)}` : ''}`)),
  activityVoiceFocus: (sessionId: string): Promise<import('./activityVoiceFocus').ActivityVoiceFocus> =>
    request(`/v1/sessions/${sessionId}/voice/activity-focus`, { cache: 'no-store' }),
  tutorVoicePlayback: (sessionId:string,operation:'reserve'|'start'|'natural-end'|'cancel',values:Record<string,unknown>,signal:AbortSignal):Promise<import('./ordinaryResponseGrant').TutorVoicePlaybackReceipt> =>
    request(`/v1/sessions/${sessionId}/tutor-voice-playback/${operation}`,{method:'POST',body:JSON.stringify(values),signal}),
  tutorPlayback: (sessionId: string): Promise<{ playback: import('./types').TutorPlaybackCheckpoint | null }> =>
    request(`/v1/sessions/${sessionId}/tutor-playback`),
  mutateTutorPlayback: (sessionId: string, operation: 'begin' | 'control' | 'segment', values: Record<string, unknown>): Promise<{ playback: import('./types').TutorPlaybackCheckpoint }> =>
    request(`/v1/sessions/${sessionId}/tutor-playback/${operation}`, { method: 'POST', body: JSON.stringify(values) }),
  prepareDiagnosticPrompt: (sessionId: string, values: { track: string; paper_authorized_turn_id?: string; preferred_item_id?: string; subject?: string; knowledge_node_id?: string; review_task_id?: string; require_unexposed?: boolean; client_action_id: string; numeric_clarification?: import('./types').DiagnosticPromptResult['numeric_clarification'] }): Promise<import('./types').DiagnosticPromptResult> =>
    trackDiagnosticCapture(sessionId, () => request(`/v1/sessions/${sessionId}/diagnostic-prompt/prepare`, { method: 'POST', body: JSON.stringify(values) }), true),
  answerDiagnosticPrompt: (sessionId: string, values: { prompt_id: string; item_version: string; answer: string; client_action_id: string; authorized_turn_id?: string; writing_original_artifact_id?: string; writing_original_revision?: string }): Promise<import('./types').DiagnosticPromptResult> =>
    trackDiagnosticCapture(sessionId, () => request(`/v1/sessions/${sessionId}/diagnostic-prompt/answer`, { method: 'POST', body: JSON.stringify(values) }), true),
  resumeDiagnosticPrompt: (sessionId: string, values: { prompt_id: string; item_version: string }): Promise<import('./types').DiagnosticPromptResult> =>
    trackDiagnosticCapture(sessionId, () => request(`/v1/sessions/${sessionId}/diagnostic-prompt/resume`, { method: 'POST', body: JSON.stringify(values) }), true),
  cancelDiagnosticPrompt: (sessionId: string, values: { prompt_id: string; item_version: string }): Promise<import('./types').DiagnosticPromptResult> =>
    trackDiagnosticCapture(sessionId, () => request(`/v1/sessions/${sessionId}/diagnostic-prompt/cancel`, { method: 'POST', body: JSON.stringify(values) }), true),
  nextDiagnostic: async (sessionId: string, track: string, preferredItemId?: string, subject?: string): Promise<{ item: DiagnosticItem | null; pet_baseline: PetBaseline | null; specialized_assessment?: DiagnosticAttempt['specialized_assessment'] }> => {
    const current = await localApi.diagnosticPrompt(sessionId)
    if (current.status === 'awaiting_task_start') throw new DiagnosticTaskStartRequiredError(current)
    let result = current.status === 'waiting_answer' || current.status === 'answer_saved' ? current
      : await localApi.prepareDiagnosticPrompt(sessionId, { track, preferred_item_id: preferredItemId, subject, client_action_id: crypto.randomUUID() })
    if (result.status === 'awaiting_task_start') throw new DiagnosticTaskStartRequiredError(result)
    if (result.prompt && ((preferredItemId && result.prompt.diagnostic_item_id !== preferredItemId) || result.prompt.item.track !== track || (subject && result.prompt.item.subject !== subject))) {
      throw new Error('还有一道尚未完成的题目，请先核对或取消当前题目。')
    }
    if (result.status === 'answer_saved' && result.prompt) {
      result = await localApi.resumeDiagnosticPrompt(sessionId, result.prompt)
      if (result.status === 'awaiting_task_start') throw new DiagnosticTaskStartRequiredError(result)
      if (result.status === 'answer_saved') throw new Error('原回答已经保存，评分尚未完成。请稍后核对结果，不需要再次作答。')
      if (result.attempt) throw new Error(`原回答已恢复评分：${result.attempt.feedback}。请核对这次结果后继续。`)
    }
    return { item: result.status === 'waiting_answer' ? result.prompt?.item ?? null : null, pet_baseline: result.pet_baseline ?? null, specialized_assessment: result.specialized_assessment }
  },
  answerDiagnostic: async (sessionId: string, itemId: string, answer: string, reviewTaskId?: string): Promise<DiagnosticAttempt> => {
    const current = await localApi.diagnosticPrompt(sessionId)
    if (current.status === 'awaiting_task_start') throw new DiagnosticTaskStartRequiredError(current)
    if (current.prompt?.diagnostic_item_id === itemId) {
      if (reviewTaskId && current.prompt.review_task_id !== reviewTaskId) throw new Error('当前题目不属于这次复测，不能把答案记录到另一项任务。')
      const result = await localApi.answerDiagnosticPrompt(sessionId, {
        prompt_id: current.prompt.prompt_id, item_version: current.prompt.item_version,
        answer, client_action_id: `diagnostic-answer:${current.prompt.prompt_id}`,
      })
      if (result.status === 'awaiting_task_start') throw new DiagnosticTaskStartRequiredError(result)
      if (!result.attempt) throw new Error(result.status === 'answer_saved' ? '回答已保存，评分尚未完成。请核对本题结果，不要重复作答。' : '题目已失效或取消，答案未计入学习记录。')
      return result.attempt
    }
    if (current.status === 'waiting_answer' || current.status === 'answer_saved') throw new Error('正在等待另一道题目的回答，请先核对当前题目。')
    // Legacy, explicitly bound review cards retain their original scoring path.
    return request(`/v1/sessions/${sessionId}/diagnostics/answer`, {
      method: 'POST', body: JSON.stringify({ diagnostic_item_id: itemId, answer, review_task_id: reviewTaskId, client_action_id: crypto.randomUUID() }),
    })
  },
  planReviewQuestion: (planItemId: string): Promise<DiagnosticItem & { plan_item_id: string; child_id: string; session_id: string; review_task_id: string; review_status: 'pending' | 'completed'; attempt: DiagnosticAttempt | null }> =>
    request(`/v1/plan-items/${planItemId}/review-question`),
  activeSpecializedAssessment: (sessionId: string): Promise<{ assessment: DiagnosticAttempt['specialized_assessment'] }> =>
    request(`/v1/sessions/${sessionId}/specialized-assessment`),
  nextPetVocabulary: (sessionId: string): Promise<{
    card: PetVocabularyCard | null
    summary: PetVocabularySummary
    sync: { available_confirmed: number; inserted: number; skipped_without_meaning: number; deck_size: number }
  }> => request(`/v1/sessions/${sessionId}/pet-vocabulary/next`),
  answerPetVocabulary: (sessionId: string, answer: string): Promise<PetVocabularyReview> =>
    request(`/v1/sessions/${sessionId}/pet-vocabulary/answer`, {
      method: 'POST',
      body: JSON.stringify({ answer, client_action_id: crypto.randomUUID() })
    }),
  answerBoundPetVocabulary: async (sessionId: string, answer: string, promptId: string, cardVersion: string, authorizedTurnId?: string): Promise<PetVocabularyReview> => {
    const result = await request<{ status: string; review: PetVocabularyReview | null }>(`/v1/sessions/${sessionId}/pet-vocabulary/prompt/answer`, {
      method: 'POST', body: JSON.stringify({ prompt_session_id: promptId, card_version: cardVersion, answer, client_action_id: `vocabulary-answer:${promptId}`, authorized_turn_id: authorizedTurnId }),
    })
    if (!result.review) throw new Error('原词汇卡已取消或来源失效，没有把回答记到其他卡片上。')
    return result.review
  },
  cancelBoundPetVocabulary: (sessionId: string, promptId: string, cardVersion: string): Promise<{ status: string }> =>
    request(`/v1/sessions/${sessionId}/pet-vocabulary/prompt/cancel`, {
      method: 'POST', body: JSON.stringify({ prompt_session_id: promptId, card_version: cardVersion }),
    }),
  cancelPetVocabulary: (sessionId: string): Promise<{ cancelled: boolean }> =>
    request(`/v1/sessions/${sessionId}/pet-vocabulary/cancel`, { method: 'POST' }),
  knowledgeGraphLearningPath: (childId: string, nodeId: string): Promise<KnowledgeGraphLearningPath> =>
    request(`/v1/children/${encodeURIComponent(childId)}/knowledge-graph/learning-path?node_id=${encodeURIComponent(nodeId)}`),
  privacyPreferences: (): Promise<PrivacyPreferences> =>
    request('/v1/parent/privacy', { headers: { 'X-HSC-Parent': parentToken } }),
  updatePrivacyPreferences: (values: Partial<PrivacyPreferences>): Promise<PrivacyPreferences> =>
    request('/v1/parent/privacy', {
      method: 'PUT',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify(values)
    }),
  familyRule: (childId: string): Promise<FamilyRule> =>
    request(`/v1/parent/children/${childId}/family-rule`, { headers: { 'X-HSC-Parent': parentToken } }),
  updateChildProfile: (childId: string, values: { nickname: string; curriculum_region: string }): Promise<ChildProfile> =>
    request(`/v1/parent/children/${childId}/profile`, {
      method: 'PUT',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify(values)
    }),
  updateFamilyRule: (childId: string, values: Omit<FamilyRule, 'child_id' | 'version' | 'updated_at'>): Promise<FamilyRule> =>
    request(`/v1/parent/children/${childId}/family-rule`, {
      method: 'PUT',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify(values)
    }),
  knowledgeSources: (childId: string): Promise<KnowledgeSource[]> =>
    request<{ items: KnowledgeSource[] }>(`/v1/parent/children/${childId}/knowledge-sources`, {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items),
  importKnowledgeSource: (childId: string, values: {
    source_type: KnowledgeSource['source_type']; title: string; subject: string; grade_band: string
    publisher?: string; version_label?: string; source_ref?: string; effective_from?: string; effective_to?: string
    license_status: KnowledgeSource['license_status']; parent_reviewed: boolean; content_text: string
  }): Promise<KnowledgeSource> =>
    request(`/v1/parent/children/${childId}/knowledge-sources`, {
      method: 'POST',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify(values)
    }),
  knowledgeCorpora: (): Promise<CurriculumCorpus[]> =>
    request<{ items: CurriculumCorpus[] }>('/v1/parent/knowledge-corpora', {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items),
  importKnowledgeCorpus: (values: {
    title: string; database_path: string; transcript_root: string; video_root: string; client_action_id: string
  }): Promise<CurriculumCorpus> =>
    request('/v1/parent/knowledge-corpora', {
      method: 'POST',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify(values)
    }),
  knowledgeCorpusCoverage: (corpusId: string): Promise<CurriculumCoverage[]> =>
    request<{ items: CurriculumCoverage[] }>(`/v1/parent/knowledge-corpora/${corpusId}/coverage`, {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items),
  searchKnowledgeCorpus: (childId: string, query: string, subject?: string): Promise<CurriculumSearchResult[]> => {
    const parameters = new URLSearchParams({ q: query })
    if (subject) parameters.set('subject', subject)
    return request<{ items: CurriculumSearchResult[] }>(`/v1/parent/children/${childId}/knowledge-corpus/search?${parameters}`, {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items)
  },
  deleteKnowledgeCorpus: (corpusId: string): Promise<{ status: 'deleted'; resource_count: number }> =>
    request(`/v1/parent/knowledge-corpora/${corpusId}`, {
      method: 'DELETE', headers: { 'X-HSC-Parent': parentToken }
    }),
  importCurriculumStandards: (rootPath: string): Promise<{ status: 'imported'; source_count: number; page_count: number }> =>
    request('/v1/parent/curriculum-standards/import', {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ root_path: rootPath })
    }),
  curriculumStandards: (): Promise<CurriculumStandardSource[]> =>
    request<{ items: CurriculumStandardSource[] }>('/v1/parent/curriculum-standards', {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items),
  curriculumStandardAlignment: (corpusId: string): Promise<CurriculumStandardAlignmentSummary> =>
    request(`/v1/parent/knowledge-corpora/${corpusId}/curriculum-standard-alignment`, {
      headers: { 'X-HSC-Parent': parentToken }
    }),
  curriculumInternalizationScopes: (corpusId: string, childId: string): Promise<CurriculumInternalizationScope[]> =>
    request<{ items: CurriculumInternalizationScope[] }>(`/v1/parent/knowledge-corpora/${corpusId}/internalization-scopes?child_id=${encodeURIComponent(childId)}`, {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items),
  curriculumInternalizationJobs: (corpusId: string, childId: string): Promise<CurriculumInternalizationJob[]> =>
    request<{ items: CurriculumInternalizationJob[] }>(`/v1/parent/knowledge-corpora/${corpusId}/internalization-jobs?child_id=${encodeURIComponent(childId)}`, {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items),
  startCurriculumInternalization: (corpusId: string, values: {
    child_id: string; subject_title: string; version_title?: string; semester: string; unit_title?: string; max_units: number; client_action_id: string
  }): Promise<CurriculumInternalizationJob> =>
    request(`/v1/parent/knowledge-corpora/${corpusId}/internalization-jobs`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(values)
    }),
  curriculumCapabilities: (childId: string): Promise<CurriculumCapability[]> =>
    request<{ items: CurriculumCapability[] }>(`/v1/parent/children/${childId}/curriculum-capabilities`, {
      headers: { 'X-HSC-Parent': parentToken }
    }).then((result) => result.items),
  reviewCurriculumCapability: (capabilityId: string, decision: 'verify' | 'reject', note?: string): Promise<CurriculumCapability> =>
    request(`/v1/parent/curriculum-capabilities/${capabilityId}/review`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ decision, note })
    }),
  systemHealth: (): Promise<SystemHealth> =>
    request('/v1/parent/system-health', { headers: { 'X-HSC-Parent': parentToken } }),
  runMaintenance: (): Promise<{ run_date: string; items: Array<{ job_type: string; status: string; error_code?: string }> }> =>
    request('/v1/parent/maintenance/run', { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }),
  exportChild: (childId: string): Promise<{ path: string; size_bytes: number; sha256: string }> =>
    request(`/v1/parent/children/${childId}/export`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ mistake_learning_renderer_drafts: collectMistakeLearningDrafts(childId) }) }),
  createBackup: (): Promise<{ path: string; size_bytes: number; integrity_status: string; media_file_count: number; missing_media_references: number }> =>
    request('/v1/parent/backups', { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }),
  listBackups: (): Promise<BackupManifest[]> =>
    request<{ items: BackupManifest[] }>('/v1/parent/backups', { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  privacyOperationGeneration: (): Promise<{ generation: string }> =>
    request('/v1/parent/privacy-operations/generation', { headers: { 'X-HSC-Parent': parentToken }, cache: 'no-store' }),
  privacyOperationReceipt: (requestId: string): Promise<import('./privacyOperationRecovery').PrivacyOperationReceipt> =>
    privacyReceipt(requestId),
  recoverNativePrivacyOperation: (requestId:string):Promise<import('./privacyOperationRecovery').PrivacyOperationReceipt> => privacyReceipt(requestId,true),
  finalizePrivacyOperation: (requestId: string): Promise<import('./privacyOperationRecovery').PrivacyOperationReceipt> =>
    request(`/v1/parent/privacy-operations/${encodeURIComponent(requestId)}/finalize`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }),
  restoreBackup: (backupId: string, confirmation: string, operation?: import('./privacyOperationRecovery').PrivacyOperationIdentity): Promise<{ status: string; integrity_status: string; recovery_point_backup_id: string | null; restore_protection?: 'internal_database_only' }> =>
    observationIntents.duringRestore(() => destructiveParentRequest('/v1/parent/backups/restore', { backup_id: backupId, confirmation, ...(operation ? { operation } : {}) })),
  createPortableBackup: (passphrase: string): Promise<{ portable_backup_id: string; path: string; size_bytes: number; sha256: string; media_file_count: number; format: string }> =>
    request('/v1/parent/backups/portable', {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify({ passphrase })
    }),
  restorePortableBackup: (path: string, passphrase: string, confirmation: string, operation?: import('./privacyOperationRecovery').PrivacyOperationIdentity): Promise<{ portable_backup_id: string; status: string; integrity_status: string; recovery_point_backup_id: string | null; restore_protection?: 'internal_database_only'; media_file_count: number }> =>
    observationIntents.duringRestore(() => destructiveParentRequest('/v1/parent/backups/portable/restore', { path, passphrase, confirmation, ...(operation ? { operation } : {}) })),
  legacyOcrPreview: (): Promise<LegacyOcrPreview> => request('/v1/parent/privacy/legacy-ocr/preview', { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }),
  legacyOcrDispose: (command: LegacyOcrCommand): Promise<LegacyOcrReceipt> => request('/v1/parent/privacy/legacy-ocr/dispose', { method: 'POST', headers: { 'X-HSC-Parent': parentToken }, body: JSON.stringify(command) }),
  legacyOcrReceipt: (receiptId: string): Promise<LegacyOcrReceipt> => request(`/v1/parent/privacy/legacy-ocr/receipts/${encodeURIComponent(receiptId)}`, { headers: { 'X-HSC-Parent': parentToken } }),
  deletionPreview: (childId: string): Promise<Record<string, number>> =>
    request(`/v1/parent/children/${childId}/deletion-preview`, { headers: { 'X-HSC-Parent': parentToken } }),
  captureArtifacts: (childId: string): Promise<CaptureArtifactSummary[]> =>
    request<{ items: CaptureArtifactSummary[] }>(`/v1/parent/children/${childId}/capture-artifacts`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  deleteCaptureArtifact: (artifactId: string, confirmation: string, actionId: string = crypto.randomUUID()): Promise<{ status: string; database_reference_deleted: boolean; raw_file_deleted: boolean; recovery_rotation: { status: 'completed'; invalidated_backup_count: number; removed_backup_bundle_files: number; removed_upgrade_snapshot_files: number; replacement_backup_id: string } }> =>
    request(`/v1/parent/capture-artifacts/${artifactId}`, {
      method: 'DELETE',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ confirmation, client_action_id: actionId })
    }),
  deleteChildData: (childId: string, scope: string, confirmation: string, operation?: import('./privacyOperationRecovery').PrivacyOperationIdentity): Promise<{ deleted_counts: Record<string, number>; recovery_rotation: { status: 'completed'; invalidated_backup_count: number; removed_backup_bundle_files: number; removed_upgrade_snapshot_files: number; replacement_backup_id: string } }> =>
    destructiveParentRequest(`/v1/parent/children/${childId}/delete`, { scope, confirmation, client_action_id: operation?.request_id ?? crypto.randomUUID(), ...(operation ? { operation } : {}) }),
  retryDeletion: (deletionId: string, confirmation = '确认删除'): Promise<{ status: string; deletion_id: string; remaining_file_count: number }> =>
    destructiveParentRequest(`/v1/parent/deletions/${deletionId}/retry`, { confirmation }),
  periodReports: (childId: string): Promise<PeriodReport[]> =>
    request<{ items: PeriodReport[] }>(`/v1/children/${childId}/period-reports`, { headers: { 'X-HSC-Parent': parentToken } }).then((result) => result.items),
  generatePeriodReport: (childId: string, reportType: 'weekly' | 'monthly'): Promise<PeriodReport> =>
    request(`/v1/children/${childId}/period-reports/generate?report_type=${reportType}`, { method: 'POST', headers: { 'X-HSC-Parent': parentToken } }),
  runOcr: (sessionId: string, imageBase64: string): Promise<OcrArtifact> =>
    request(`/v1/sessions/${sessionId}/ocr`, {
      method: 'POST',
      body: JSON.stringify({ image_base64: imageBase64, client_action_id: crypto.randomUUID() })
    }),
  retainTrialDocumentFrame: (sessionId: string, imageBase64: string, leaseId: string, generation: number, reason: 'periodic' | 'page_changed' | 'ocr_capture'): Promise<{ artifact_id: string; image_available: boolean }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/document-monitor/trial-frames`, {
      method: 'POST', signal: AbortSignal.timeout(10_000), body: JSON.stringify({
        image_base64: imageBase64, lease_id: leaseId, generation, reason,
        observed_at: new Date().toISOString(), client_action_id: crypto.randomUUID()
      })
    }),
  runDocumentMonitorOcr: (sessionId: string, imageBase64: string, trialFrameId?: string): Promise<OcrArtifact> =>
    requestDocumentMutation(sessionId, 'ocr', {
      image_base64: imageBase64, client_action_id: crypto.randomUUID(), ...(trialFrameId ? { trial_frame_id: trialFrameId } : {})
    }, 180_000),
  documentMonitorStatus: (sessionId: string): Promise<DocumentMonitorStatus> =>
    request(`/v1/sessions/${sessionId}/document-monitor`),
  documentMonitorPreviews: (sessionId: string, signal: AbortSignal): Promise<{ items: DocumentOcrPreview[] }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/document-monitor/previews`, {
      cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(10_000)])
    }, false, true),
  createDocumentMonitorPreview: (sessionId: string, imageBase64: string, generation: number, actionId: string, signal: AbortSignal, leaseId: string, frameSignature?: string, inspectionTurnId?: string, phase?: DocumentOcrPreviewPhase, pageToken?: string): Promise<DocumentOcrPreview> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/document-monitor/previews`, {
      method: 'POST',
      body: JSON.stringify({ image_base64: imageBase64, expected_generation: generation, expected_lease_id: leaseId,
        client_action_id: actionId, frame_signature: frameSignature, inspection_turn_id: inspectionTurnId, expected_phase: phase, page_token: pageToken }),
      signal: AbortSignal.any([signal, AbortSignal.timeout(180_000)])
    }, false, true),
  resolveDocumentMonitorPreview: (sessionId: string, actionId: string, generation: number, leaseId: string, phase: DocumentOcrPreviewPhase, pageToken: string, signal: AbortSignal, inspectionTurnId?: string): Promise<DocumentOcrPreviewResolution> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/document-monitor/previews/${encodeURIComponent(actionId)}/resolve`, {
      method: 'POST', body: JSON.stringify({ expected_generation: generation, expected_lease_id: leaseId,
        expected_phase: phase, page_token: pageToken, inspection_turn_id: inspectionTurnId }),
      signal: AbortSignal.any([signal, AbortSignal.timeout(10_000)])
    }, false, true),
  documentGoalReview: (sessionId: string, rootTurnId: string, signal: AbortSignal): Promise<Record<string, unknown>> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/document-goals/${encodeURIComponent(rootTurnId)}/review`, {
      method: 'GET', cache: 'no-store', signal,
    }, false, true),
  startDocumentMonitor: (sessionId: string): Promise<DocumentMonitorStatus> =>
    request(`/v1/sessions/${sessionId}/document-monitor/start`, {
      method: 'POST', body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  pauseDocumentMonitor: (sessionId: string): Promise<DocumentMonitorStatus> =>
    request(`/v1/sessions/${sessionId}/document-monitor/pause`, {
      method: 'POST', body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  resumeDocumentMonitor: (sessionId: string): Promise<DocumentMonitorStatus> =>
    request(`/v1/sessions/${sessionId}/document-monitor/resume`, {
      method: 'POST', body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  stopDocumentMonitor: (sessionId: string): Promise<DocumentMonitorStatus> =>
    request(`/v1/sessions/${sessionId}/document-monitor/stop`, {
      method: 'POST', body: JSON.stringify({ client_action_id: crypto.randomUUID() })
    }),
  recordDocumentMonitorEvent: (sessionId: string, eventType: DocumentMonitorState, confidence: number, metrics: Record<string, string | number | boolean>): Promise<unknown> =>
    request(`/v1/sessions/${sessionId}/document-monitor/events`, {
      method: 'POST', body: JSON.stringify({ event_type: eventType, confidence, metrics, observed_at: new Date().toISOString(), client_action_id: crypto.randomUUID() })
    }),
  documentObservationEpoch: (sessionId: string): Promise<{epoch: string; local_only: true}> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/document-observation-segments/epoch`, { signal: AbortSignal.timeout(5_000) }),
  recordDocumentObservationSegment: (sessionId: string, payload: import('./documentVisibilityChannel').DocumentVisibilityPayload): Promise<{segment_id: string; recorded: boolean}> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/document-observation-segments`, {
      method: 'POST', signal: AbortSignal.timeout(5_000), body: JSON.stringify(payload)
    }),
  recordDocumentMonitorRevision: (sessionId: string, artifactId: string, contentSignature: string): Promise<DocumentPageRevision> =>
    requestDocumentMutation(sessionId, 'revision', {
      artifact_id: artifactId, content_signature: contentSignature, client_action_id: crypto.randomUUID()
    }, 30_000),
  pendingDocumentReceipts: (sessionId: string): Promise<import('./documentOperationRecovery').DocumentDeliveryReceipt[]> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/document-monitor/receipts`, { signal: AbortSignal.timeout(5_000) }),
  originalDocumentVoiceSource: (sessionId: string, turnId: string, signal?: AbortSignal): Promise<DocumentPageRevision & {
    source_voice_input_id: string; local_only: true; cloud_transmission_allowed: false; mastery_update_allowed: false;
    page_status: 'text_available' | 'empty' | 'blocked'
  }> => request(`/v1/sessions/${encodeURIComponent(sessionId)}/agent-turns/${encodeURIComponent(turnId)}/document-source`, { signal }),
  acknowledgeDocumentReceipt: (sessionId: string, operation: 'ocr' | 'revision', actionId: string): Promise<import('./documentOperationRecovery').DocumentDeliveryReceipt> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/document-monitor/receipts/${operation}/${encodeURIComponent(actionId)}/acknowledge`, { method: 'POST', signal: AbortSignal.timeout(5_000) }),
  localPlaybackControl: (sessionId: string, payload: import('./protectedPlaybackControl').LocalPlaybackControlPayload, signal: AbortSignal): Promise<import('./protectedPlaybackControl').LocalPlaybackControlResult> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/voice/local-playback-control`, { method: 'POST', signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]), body: JSON.stringify(payload) }),
  transcribe: (sessionId: string, purpose: string, wavBase64: string, signal?: AbortSignal, clientActionId: string = crypto.randomUUID(), captureReference?: VoiceCaptureReference, localOnly = false, ordinaryResponse?: import('./ordinaryResponseGrant').OrdinaryResponseBinding): Promise<VoiceInput & { local_only?: boolean }> =>
    request(`/v1/sessions/${sessionId}/asr`, {
      method: 'POST',
      signal,
      body: JSON.stringify({ wav_base64: wavBase64, purpose, client_action_id: clientActionId, ...(captureReference ? { capture_reference: captureReference } : {}), ...(localOnly ? { local_only: true } : {}), ...(ordinaryResponse ? { ordinary_response_binding: ordinaryResponse } : {}) })
    }),
  admitRealtimeVoice: async (sessionId: string, inputId: string, transcript: string, capturedAt: number,
    reference: VoiceCaptureReference, signal?: AbortSignal): Promise<RealtimeVoiceAdmission> => {
    if (!Number.isSafeInteger(capturedAt) || capturedAt <= 0 || !transcript.trim() || transcript.length > 16000
      || !/^[A-Za-z0-9_-]{8,128}$/.test(inputId)) throw new ApiError('这句话的原始采集记录不完整，尚未提交。', undefined, 'REALTIME_CAPTURE_INVALID')
    if (signal?.aborted) throw new DOMException('原语音接纳已停止。', 'AbortError')
    const epoch = runtimeEpoch
    const root = `/v1/sessions/${encodeURIComponent(sessionId)}/voice/realtime`
    const controller = new AbortController()
    let cancel!: () => void
    let timer!: ReturnType<typeof setTimeout>
    const interrupted = new Promise<never>((_resolve, reject) => {
      cancel = () => { reject(new DOMException('原语音接纳已停止。', 'AbortError')); controller.abort() }
      timer = setTimeout(() => {
        reject(new ApiError('原话的保存结果尚未确认，不会自动重发或执行。', undefined, 'REALTIME_CAPTURE_UNCONFIRMED'))
        controller.abort()
      }, 10_000)
      signal?.addEventListener('abort', cancel, { once: true })
    })
    const admit = async (): Promise<RealtimeVoiceAdmission> => {
      await request(root + '/begin', { method: 'POST', signal: controller.signal, body: JSON.stringify({ client_action_id: inputId,
        model_label: 'qwen3-asr-flash-realtime', captured_at_ms: capturedAt, capture_reference: reference }) })
      if (epoch !== runtimeEpoch || controller.signal.aborted) throw new DOMException('原语音接纳已停止。', 'AbortError')
      const result = await request<RealtimeVoiceAdmission>(root + '/complete', { method: 'POST', signal: controller.signal,
        body: JSON.stringify({ client_action_id: inputId, transcript }) })
      if (epoch !== runtimeEpoch || controller.signal.aborted) throw new DOMException('原语音接纳已停止。', 'AbortError')
      if (!result || typeof result.voice_input_id !== 'string' || !/^[A-Za-z0-9_-]{8,128}$/.test(result.voice_input_id)
        || result.client_action_id !== inputId || result.session_id !== sessionId
        || result.child_id !== reference.child_id || result.status !== 'completed' || result.source_kind !== 'realtime_transcript'
        || result.raw_audio_available !== false || result.original_transcript !== transcript) {
        throw new ApiError('原话的保存回执无法核对，没有交给学习任务。', undefined, 'REALTIME_CAPTURE_UNCONFIRMED')
      }
      return result
    }
    try { return await Promise.race([admit(), interrupted]) }
    finally { clearTimeout(timer); signal?.removeEventListener('abort', cancel) }
  },
  transcribeHouseholdEntry:(wavBase64:string,signal?:AbortSignal,clientActionId=crypto.randomUUID()):Promise<StandbyVoiceInput>=>
    request('/v1/household/voice-entry/asr',{method:'POST',signal,body:JSON.stringify({wav_base64:wavBase64,purpose:'household_entry',client_action_id:clientActionId})}),
  transcribeStandby: (wavBase64: string, signal?: AbortSignal, clientActionId = crypto.randomUUID()): Promise<StandbyVoiceInput> =>
    request('/v1/standby/asr', {
      method: 'POST',
      signal,
      body: JSON.stringify({ wav_base64: wavBase64, purpose: 'household_wake', client_action_id: clientActionId })
    }),
  transcriptionAction: (sessionId: string, clientActionId: string): Promise<VoiceInput | { client_action_id: string; status: 'not_found' | 'cancelled' }> =>
    request(`/v1/sessions/${sessionId}/asr/actions/${encodeURIComponent(clientActionId)}`),
  realtimeAdmissionReceipt: (sessionId: string, inputId: string, signal?: AbortSignal): Promise<import('./realtimeAdmissionRecovery').RealtimeAdmissionReceipt> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/voice/realtime/actions/${encodeURIComponent(inputId)}`, { signal: signal ?? AbortSignal.timeout(5_000) }),
  realtimeRecoveryIndex: (sessionId: string, childId: string, signal?: AbortSignal): Promise<import('./realtimeAdmissionJournal').RealtimeRecoveryIndex> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/voice/realtime/recovery-index?${new URLSearchParams({ child_id: childId })}`, { signal: signal ?? AbortSignal.timeout(5_000), cache: 'no-store' }),
  cancelTranscriptionAction: (sessionId: string, clientActionId: string, signal?: AbortSignal): Promise<VoiceInput | { client_action_id: string; status: 'cancelled' }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/asr/actions/${encodeURIComponent(clientActionId)}/cancel`, { method: 'POST', signal: signal ?? AbortSignal.timeout(5_000) }),
  voiceInteraction: (sessionId: string, body: VoiceInteractionRequest, signal?: AbortSignal): Promise<VoiceInteractionResponse> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/voice-interaction`, { method: 'POST', body: JSON.stringify(body), signal }),
  voiceInteractionRecovery: (sessionId: string, childId: string, context: VoiceInteractionContext, signal?: AbortSignal): Promise<VoiceInteractionRecovery> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/voice-interaction/recovery/read`, {
      method: 'POST', body: JSON.stringify({ child_id: childId, context }), cache: 'no-store', signal,
    }),
  retryVoiceInteraction: (sessionId: string, childId: string, originalActionKey: string, clientActionId: string, context: VoiceInteractionContext, signal?: AbortSignal): Promise<VoiceInteractionRecovery> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/voice-interaction/recovery/retry`, {
      method: 'POST', body: JSON.stringify({ child_id: childId, original_action_key: originalActionKey, client_action_id: clientActionId, context }), signal,
    }),
  prepareVoiceInteractionHandoff: async (sessionId: string, childId: string, originalActionKey: string, signal?: AbortSignal): Promise<VoiceInteractionHandoff> => {
    const proof = await request<unknown>(`/v1/sessions/${encodeURIComponent(sessionId)}/voice-interaction/recovery/handoff/prepare`, {
      method: 'POST', body: JSON.stringify({ child_id: childId, original_action_key: originalActionKey }),
      cache: 'no-store', signal: signal ?? AbortSignal.timeout(5_000),
    }, false, true)
    return scopedVoiceInteractionHandoff(proof, sessionId, childId)
  },
  readVoiceInteractionHandoff: async (sessionId: string, childId: string, signal?: AbortSignal): Promise<VoiceInteractionHandoff> => {
    const proof = await request<unknown>(`/v1/sessions/${encodeURIComponent(sessionId)}/voice-interaction/recovery/handoff/read`, {
      method: 'POST', body: JSON.stringify({ child_id: childId }), cache: 'no-store', signal: signal ?? AbortSignal.timeout(5_000),
    }, false, true)
    return scopedVoiceInteractionHandoff(proof, sessionId, childId)
  },
  invalidateVoiceInteractionHandoff: async (sessionId: string, childId: string, signal?: AbortSignal): Promise<VoiceInteractionHandoff> => {
    const proof = await request<unknown>(`/v1/sessions/${encodeURIComponent(sessionId)}/voice-interaction/recovery/handoff/invalidate`, {
      method: 'POST', body: JSON.stringify({ child_id: childId }), cache: 'no-store', signal: signal ?? AbortSignal.timeout(5_000),
    }, false, true)
    return scopedVoiceInteractionHandoff(proof, sessionId, childId)
  },
  dispatchVoiceInteractionHandoff: async (sessionId: string, childId: string, handoffId: string, agentActionId: string, signal?: AbortSignal): Promise<AgentTurn> => {
    const epoch = runtimeEpoch
    let cancelled = signal?.aborted ?? false
    const cancel = (): void => { cancelled = true }
    window.addEventListener('hsc-privacy-pause', cancel)
    signal?.addEventListener('abort', cancel, { once: true })
    try {
      if (cancelled) throw new DOMException('原语音请求已停止。', 'AbortError')
      const proof = await localApi.readVoiceInteractionHandoff(sessionId, childId, signal)
      if (cancelled) throw new DOMException('原语音请求已停止。', 'AbortError')
      if (runtimeEpoch !== epoch) throw new ApiError('本地学习服务已切换，请核对原请求。', undefined, 'LOCAL_SERVICE_CHANGED')
      if (proof.status !== 'ready' || !proof.request) throw new ApiError('原请求尚未具备交接条件，请先核对或保存教程进度；没有重新执行。',
        undefined, 'VOICE_INTERACTION_HANDOFF_NOT_READY', false, 'check_original_request')
      try { validateVoiceInteractionHandoff(proof, { sessionId, childId, handoffId, agentActionId }) }
      catch { throw new ApiError('原请求的交接身份无法核对，没有重新执行。', undefined, 'VOICE_INTERACTION_HANDOFF_SCOPE_MISMATCH') }
      const original = proof.request
      return await dispatchAgentRequest(sessionId, agentActionId, `/v1/sessions/${encodeURIComponent(sessionId)}/agent-turn`, {
        transcript: original.transcript, client_action_id: agentActionId, voice_interaction_handoff_id: handoffId,
        ...(original.voice_input_id ? { voice_input_id: original.voice_input_id } : {}),
      }, { childId, voiceInputId: original.voice_input_id, handoffTranscript: original.transcript, signal })
    } finally {
      window.removeEventListener('hsc-privacy-pause', cancel)
      signal?.removeEventListener('abort', cancel)
    }
  },
  voicePlayback: (sessionId: string, body: { playback_id: string; text: string; event: 'started' | 'ended' }): Promise<{ playback_id: string }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/voice-playback`, { method: 'POST', body: JSON.stringify(body), signal: AbortSignal.timeout(2_000) }),
  agentTurn: async (sessionId: string, transcript: string, playbackOverlapId?: string, voiceInputId?: string, pendingEvaluationTurnId?: string, localRecoveryOnly?: boolean, clientActionId?: string): Promise<AgentTurn> => {
    if (localRecoveryOnly && (pendingEvaluationTurnId || playbackOverlapId)) throw new Error('LOCAL_RECOVERY_SCOPE_CONFLICT')
    const actionId = clientActionId ?? crypto.randomUUID()
    return dispatchAgentRequest(sessionId, actionId, `/v1/sessions/${sessionId}/agent-turn`, {
      transcript, client_action_id: actionId, ...(playbackOverlapId ? { playback_overlap_id: playbackOverlapId } : {}),
      ...(voiceInputId ? { voice_input_id: voiceInputId } : {}),
      ...(pendingEvaluationTurnId ? { pending_evaluation_turn_id: pendingEvaluationTurnId } : {}),
      ...(localRecoveryOnly ? { local_recovery_only: true } : {}),
    })
  },
  realtimeResumePreview: (sessionId: string, childId: string, inputId: string, signal?: AbortSignal): Promise<import('./realtimeUnsentResume').RealtimeResumePreview> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/voice/realtime/actions/${encodeURIComponent(inputId)}/resume?child_id=${encodeURIComponent(childId)}`,
      { cache: 'no-store', signal: signal ?? AbortSignal.timeout(5_000) }),
  resumeRealtimeVoice: (sessionId: string, childId: string, inputId: string, voiceInputId: string, signal?: AbortSignal): Promise<AgentTurn> =>
    dispatchAgentRequest(sessionId, 'realtime-' + voiceInputId,
      `/v1/sessions/${encodeURIComponent(sessionId)}/voice/realtime/actions/${encodeURIComponent(inputId)}/resume`,
      { child_id: childId, voice_input_id: voiceInputId }, { childId, voiceInputId, signal }),
  agentExecutionRecoveryIndex: (sessionId: string, childId: string, signal?: AbortSignal): Promise<import('./agentExecutionJournal').AgentExecutionRecoveryIndex> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/agent-execution-recovery-index?${new URLSearchParams({ child_id: childId })}`,
      { cache: 'no-store', signal: signal ?? AbortSignal.timeout(5_000) }, false, true),
  agentRequestReceipt: (sessionId: string, actionId: string, signal?: AbortSignal): Promise<import('./agentRequestRecovery').AgentRequestReceipt> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/agent-request-receipts/${encodeURIComponent(actionId)}`,
      { cache: 'no-store', signal: signal ?? AbortSignal.timeout(5_000) }, false, true),
  agentRuntimeStatus: (sessionId: string): Promise<AgentRuntimeStatus> =>
    request(`/v1/sessions/${sessionId}/agent-runtime`),
  taskStartChoice: (sessionId: string): Promise<import('./voiceCaptureContext').TaskStartChoiceResult> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/task-start-choice`, { cache: 'no-store' }),
  continueAgentTurn: (sessionId: string, parentTurnId: string): Promise<{ status: string; reason?: string; turn?: AgentTurn }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/agent-turns/${encodeURIComponent(parentTurnId)}/continue`, { method: 'POST' }),
  continuationStatus: (sessionId: string): Promise<{ status: string; parent_turn_id?: string; reason?: string;
    document_goal?: { phase: string; root_turn_id?: string; revision?: number; session_id?: string; child_id?: string; artifact_id?: string; message?: string } }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/agent-continuation`),
  admitContinuationExecution: (sessionId: string, turnId: string): Promise<{ status: string }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/agent-turns/${encodeURIComponent(turnId)}/continuation-admission`, { method: 'POST' }),
  admitVoiceControlExecution: (sessionId: string, turnId: string): Promise<{ status: string }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/agent-turns/${encodeURIComponent(turnId)}/control-admission`, { method: 'POST' }),
  admitOriginalClientExecution: (sessionId: string, turnId: string): Promise<{ status: string }> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/agent-turns/${encodeURIComponent(turnId)}/original-client-admission`, { method: 'POST' }),
  executeAgentTurn: async (turnId: string): Promise<{ turn: AgentTurn; execution: { capability: string; response_text: string; result: unknown; evidence: string[]; tutor_turn?: TutorTurn; teaching_error?: string; replay_audio_allowed?: boolean } }> => {
    const snapshot = agentExecutionSnapshot(turnId)
    const native=nativeClientExecution(turnId)
    const result = native ? await native.execute() : await request<{ turn: AgentTurn; execution: { capability: string; response_text: string; result: unknown; evidence: string[]; tutor_turn?: TutorTurn; teaching_error?: string; replay_audio_allowed?: boolean } }>(`/v1/agent-turns/${turnId}/execute`, { method: 'POST' }, false, true)
    settleAgentExecution(snapshot, result?.turn)
    return result
  },
  agentTurnExecutionStatus: (turnId: string): Promise<{ status: 'in_progress' | 'completed' | 'unknown'; original_audio_teaching?: { work_id: string; state: string; generation: number; error_code: string | null; delivery_status: string; source_verification: string; retry_after_seconds: number }; execution?: { turn: AgentTurn; execution: { capability: string; response_text: string; result: unknown; evidence: string[]; replay_audio_allowed?: boolean } } }> =>
    request(`/v1/agent-turns/${encodeURIComponent(turnId)}/execution-status`),
  completeAgentTurn: async (turnId: string, finalResponse: string, toolStatus: 'completed' | 'failed', toolErrorCode?: string, result?: Record<string, unknown>): Promise<AgentTurn> => {
    const snapshot = agentExecutionSnapshot(turnId)
    const native=nativeClientExecution(turnId)
    const turn = native?await native.complete({final_response:finalResponse,tool_status:toolStatus,...(toolErrorCode?{tool_error_code:toolErrorCode}:{}),...(result?{result}:{})}):await request<AgentTurn>(`/v1/agent-turns/${turnId}/outcome`, {
      method: 'PUT',
      body: JSON.stringify({ final_response: finalResponse, tool_status: toolStatus, tool_error_code: toolErrorCode, result })
    }, false, true)
    settleAgentExecution(snapshot, turn)
    return turn
  },
  beginObservationStream: (sessionId: string, epoch: string): Promise<{ status: string; event_id?: string }> =>
    request(`/v1/sessions/${sessionId}/observation/stream/start`, {
      method: 'POST', body: JSON.stringify({ stream_epoch: epoch, client_action_id: `coverage-start:${epoch}` }),
    }),
  endObservationStream: (sessionId: string, epoch: string, sequence: number, reason: 'paused' | 'unavailable' | 'ended' | 'disconnected' | 'context_changed', signal?: AbortSignal): Promise<{ status: string; event_id?: string }> =>
    request(`/v1/sessions/${sessionId}/observation/stream/stop`, {
      method: 'POST', body: JSON.stringify({ stream_epoch: epoch, sequence, reason, client_action_id: `coverage-stop:${epoch}` }), signal,
    }),
  analyzeObservation: (sessionId: string, frameBase64: string, captureSample?: import('./observationSampleClock').ObservationCaptureSample, signal?: AbortSignal): Promise<ObservationState> =>
    request(`/v1/sessions/${sessionId}/observation-frame`, {
      method: 'POST',
      body: JSON.stringify({ frame_base64: frameBase64, ...(captureSample ? { capture_sample: captureSample } : {}) }), signal
    }),
  startObservationQualification: (sessionId: string, signal?: AbortSignal): Promise<ObservationQualification> =>
    request(`/v1/sessions/${sessionId}/observation/qualification/start`, {
      method: 'POST', body: JSON.stringify({ client_action_id: crypto.randomUUID() }), signal
    }),
  observationQualification: (sessionId: string): Promise<ObservationQualification> =>
    request(`/v1/sessions/${sessionId}/observation/qualification`),
  recordRendererObservationQuality: (sessionId: string, sceneQuality: 'too_dark' | 'too_bright' | 'frozen' | 'unusable_frame', reason: string, captureSample?: import('./observationSampleClock').ObservationCaptureSample, signal?: AbortSignal): Promise<ObservationQualification> =>
    request(`/v1/sessions/${sessionId}/observation/qualification/unavailable`, {
      method: 'POST', body: JSON.stringify({ scene_quality: sceneQuality, reason, ...(captureSample ? { capture_sample: captureSample } : {}) }), signal
    }),
  deviceBindings: (): Promise<DeviceBinding[]> =>
    request<{ items: DeviceBinding[] }>('/v1/device-bindings').then((result) => result.items),
  saveDeviceBindings: (bindings: Array<Omit<DeviceBinding, 'updated_at'>>): Promise<DeviceBinding[]> =>
    request<{ items: DeviceBinding[] }>('/v1/parent/device-bindings', {
      method: 'PUT',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ bindings, client_action_id: crypto.randomUUID() })
    }).then((result) => result.items),
  deviceQualification: (): Promise<DeviceQualificationSummary> =>
    request('/v1/parent/device-qualification', { headers: { 'X-HSC-Parent': parentToken } }),
  startDeviceQualification: (mode: DeviceQualificationMode): Promise<DeviceQualificationRun> =>
    request('/v1/parent/device-qualification/runs', {
      method: 'POST',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ mode, client_action_id: crypto.randomUUID() })
    }),
  finishDeviceQualification: (runId: string, outcome: 'passed' | 'failed' | 'cancelled', metrics: Record<string, unknown>, failureCode?: string, completionToken?: string): Promise<DeviceQualificationRun> =>
    request(`/v1/parent/device-qualification/runs/${runId}/finish`, {
      method: 'POST',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ outcome, metrics, failure_code: failureCode ?? null, client_action_id: crypto.randomUUID(), completion_token: completionToken ?? null })
    }),
  observationCalibrationSummary: (childId: string): Promise<ObservationCalibrationSummary> =>
    request(`/v1/parent/children/${childId}/observation-calibrations`, { headers: { 'X-HSC-Parent': parentToken } }),
  voiceCalibrationSummary: (childId: string): Promise<VoiceCalibrationSummary> =>
    request(`/v1/parent/children/${childId}/voice-calibration`, { headers: { 'X-HSC-Parent': parentToken } }),
  saveVoiceCalibration: (childId: string, profile: Record<string, unknown>, vocabulary: string[], microphone: VoiceCalibrationMicrophone, clientActionId: string): Promise<VoiceCalibrationSummary & { profile: Record<string, unknown>; vocabulary: string[]; microphone: VoiceCalibrationMicrophone }> =>
    request(`/v1/parent/children/${childId}/voice-calibration`, { method: 'PUT', headers: { 'X-HSC-Parent': parentToken, 'Content-Type': 'application/json' }, body: JSON.stringify({ profile, vocabulary, microphone, client_action_id: clientActionId }) }),
  deleteVoiceCalibration: (childId: string, clientActionId: string): Promise<{ child_id: string; status: string; raw_audio_stored: false; profile: null }> =>
    request(`/v1/parent/children/${childId}/voice-calibration`, { method: 'DELETE', headers: { 'X-HSC-Parent': parentToken, 'Content-Type': 'application/json' }, body: JSON.stringify({ client_action_id: clientActionId }) }),
  startObservationCalibration: (childId: string): Promise<ObservationCalibrationRun> =>
    request('/v1/parent/observation-calibrations', {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ child_id: childId, client_action_id: crypto.randomUUID() })
    }),
  finishObservationCalibration: (runId: string, frameBase64: string): Promise<ObservationCalibrationRun> =>
    request(`/v1/parent/observation-calibrations/${runId}/frame`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ frame_base64: frameBase64 })
    }),
  cancelObservationCalibration: (runId: string, failureCode = 'PARENT_CANCELLED'): Promise<ObservationCalibrationRun> =>
    request(`/v1/parent/observation-calibrations/${runId}/cancel`, {
      method: 'POST', headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ failure_code: failureCode })
    }),
  recordSensorReleaseVerification: (
    sessionId: string,
    values: {
      reopened_device_types: Array<'camera' | 'document_camera' | 'microphone'>
      device_manager_healthy: boolean
      windows_privacy_indicator_clear: boolean
      physical_indicator_status: 'clear' | 'not_available' | 'still_on'
      notes: string
    }
  ): Promise<SensorReleaseVerification> =>
    request(`/v1/parent/sessions/${sessionId}/sensor-release-verifications`, {
      method: 'POST',
      headers: { 'X-HSC-Parent': parentToken },
      body: JSON.stringify({ ...values, notes: values.notes.trim() || null, client_action_id: crypto.randomUUID() })
    }),
  pauseObservation: (sessionId: string): Promise<DailySession> => observationIntents.pause<DailySession>(sessionId),
  resumeObservation: (sessionId: string): Promise<DailySession> => observationIntents.resume<DailySession>(sessionId),
  correctObservation: (sessionId: string, correctionText: string): Promise<{ correction_id: string; previous_state: string | null; effect: string }> =>
    request(`/v1/sessions/${sessionId}/observation/correct`, {
      method: 'POST',
      body: JSON.stringify({ correction_text: correctionText, client_action_id: crypto.randomUUID() })
    }),
  mistakes: (childId: string): Promise<MistakeList> =>
    request(`/v1/children/${childId}/mistakes`),
  preservedMistakeResponses: (childId: string, mistakeId: string): Promise<{ items: Array<{
    response_id: string; child_id: string; mistake_id: string; original_version: number;
    response_text: string; responded_at: string; source_erased: true; score_authority: 'none'; response_status: 'historical_only'
  }> }> => requestParentLocal(`/v1/parent/children/${encodeURIComponent(childId)}/mistakes/${encodeURIComponent(mistakeId)}/preserved-responses`, { cache: 'no-store' }),
  writingOcrConfirmation: (sessionId: string, confirmationId: string, bindingVersion: string): Promise<import('./writingReadback').WritingConfirmation> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/writing-ocr?confirmation_id=${encodeURIComponent(confirmationId)}&binding_version=${encodeURIComponent(bindingVersion)}`, { cache: 'no-store' }),
  recordWritingReadback: (sessionId: string, values: import('./writingReadback').WritingReadbackRequest): Promise<unknown> =>
    request(`/v1/sessions/${encodeURIComponent(sessionId)}/writing-ocr/readback`, { method: 'POST', body: JSON.stringify(values), cache: 'no-store' }),
  mistakeKnowledgeCandidates: (childId: string, query: string, subject?: string): Promise<MistakeKnowledgeCandidate[]> =>
    request<{ items: MistakeKnowledgeCandidate[] }>(`/v1/children/${childId}/mistake-knowledge-candidates?q=${encodeURIComponent(query)}${subject ? `&subject=${encodeURIComponent(subject)}` : ''}`).then((result) => result.items),
  captureMistake: (values: { childId: string; knowledgeNodeId: string; subject: string; title: string; promptSummary: string; submittedAnswer: string; sourceType: string; sourceRef?: string | null; confidence?: number }): Promise<MistakeItem> =>
    request('/v1/mistakes/capture', {
      method: 'POST',
      body: JSON.stringify({
        child_id: values.childId,
        knowledge_node_id: values.knowledgeNodeId,
        subject: values.subject,
        title: values.title,
        prompt_summary: values.promptSummary,
        submitted_answer: values.submittedAnswer,
        source_type: values.sourceType,
        source_ref: values.sourceRef ?? null,
        confidence: values.confidence ?? 1,
        client_action_id: crypto.randomUUID()
      })
    }),
  prepareMistakeCandidates: (artifactId: string, childId: string): Promise<MistakeCaptureCandidate[]> =>
    request<{ items: MistakeCaptureCandidate[] }>(`/v1/capture-artifacts/${artifactId}/mistake-candidates/prepare`, {
      method: 'POST', body: JSON.stringify({ child_id: childId, client_action_id: crypto.randomUUID() })
    }).then((result) => result.items),
  beginMistakeDialogue: (sessionId: string, artifactId: string, actionId: string, candidateId?: string): Promise<MistakeDialogue> =>
    request(`/v1/sessions/${sessionId}/mistake-dialogue`, {
      method: 'POST', body: JSON.stringify({ artifact_id: artifactId, candidate_id: candidateId, client_action_id: actionId })
    }),
  mistakeDialogue: (sessionId: string): Promise<{ dialogue: MistakeDialogue | null }> =>
    request(`/v1/sessions/${sessionId}/mistake-dialogue`),
  mistakeCaptureCandidates: (artifactId: string, childId: string): Promise<MistakeCaptureCandidate[]> =>
    request<{ items: MistakeCaptureCandidate[] }>(`/v1/capture-artifacts/${artifactId}/mistake-candidates?child_id=${encodeURIComponent(childId)}`).then((result) => result.items),
  mistakeCaptureQueues: (childId: string): Promise<MistakeCaptureQueue[]> =>
    request<{ items: MistakeCaptureQueue[] }>(`/v1/children/${childId}/mistake-capture-queues`).then((result) => result.items),
  editMistakeCaptureCandidate: (candidateId: string, childId: string, questionText: string): Promise<MistakeCaptureCandidate> =>
    request(`/v1/mistake-capture-candidates/${candidateId}`, {
      method: 'PUT', body: JSON.stringify({ child_id: childId, question_text: questionText, client_action_id: crypto.randomUUID() })
    }),
  skipMistakeCaptureCandidate: (candidateId: string, childId: string): Promise<MistakeCaptureCandidate> =>
    request(`/v1/mistake-capture-candidates/${candidateId}/skip`, {
      method: 'POST', body: JSON.stringify({ child_id: childId, client_action_id: crypto.randomUUID() })
    }),
  captureMistakeCandidate: (candidateId: string, values: { childId: string; knowledgeNodeId: string; subject: string; title: string; submittedAnswer: string }): Promise<{ candidate: MistakeCaptureCandidate; mistake: MistakeItem }> =>
    request(`/v1/mistake-capture-candidates/${candidateId}/capture`, {
      method: 'POST',
      body: JSON.stringify({
        child_id: values.childId,
        knowledge_node_id: values.knowledgeNodeId,
        subject: values.subject,
        title: values.title,
        submitted_answer: values.submittedAnswer,
        client_action_id: crypto.randomUUID()
      })
    }),
  weaknessClusters: (childId: string): Promise<WeaknessCluster[]> =>
    request<{ items: WeaknessCluster[] }>(`/v1/children/${childId}/weakness-clusters`).then((result) => result.items),
  nextMistakeActivity: (mistakeId: string, childId: string): Promise<MistakeNextActivity> =>
    request(`/v1/mistakes/${mistakeId}/next?child_id=${encodeURIComponent(childId)}`),
  confirmMistake: (mistakeId: string, childId: string, decision: 'confirm' | 'exclude'): Promise<MistakeItem> =>
    request(`/v1/mistakes/${mistakeId}/confirm`, {
      method: 'POST',
      body: JSON.stringify({ child_id: childId, decision, client_action_id: crypto.randomUUID() })
    }),
  diagnoseMistake: (mistakeId: string, childId: string, errorType: string, explanation: string, confidence: number): Promise<MistakeItem> =>
    request(`/v1/mistakes/${mistakeId}/diagnose`, {
      method: 'POST',
      body: JSON.stringify({ child_id: childId, error_type: errorType, explanation, confidence, client_action_id: crypto.randomUUID() })
    }),
  addMistakeRemediation: (mistakeId: string, childId: string, stepType: string, outcome: 'completed' | 'still_stuck' | 'skipped', notes: string): Promise<MistakeItem> =>
    request(`/v1/mistakes/${mistakeId}/remediation-steps`, {
      method: 'POST',
      body: JSON.stringify({ child_id: childId, step_type: stepType, outcome, notes: notes.trim() || null, client_action_id: crypto.randomUUID() })
    }),
  submitMistakeAttempt: (mistakeId: string, childId: string, stage: string, diagnosticItemId: string, answer: string, hintLevel = 0): Promise<MistakeItem> =>
    request(`/v1/mistakes/${mistakeId}/attempts`, {
      method: 'POST',
      body: JSON.stringify({ child_id: childId, stage, diagnostic_item_id: diagnosticItemId, answer, hint_level: hintLevel, client_action_id: crypto.randomUUID() })
    }),
  registerDevice: (sessionId: string, deviceType: 'camera' | 'document_camera' | 'microphone', systemIdHash: string, capabilities: Record<string, unknown>): Promise<{ lease_id: string; status: string; expires_at: string }> =>
    request(`/v1/sessions/${sessionId}/devices/register`, {
      method: 'POST',
      body: JSON.stringify({ device_type: deviceType, system_id_hash: systemIdHash, capabilities })
    }),
  recordDeviceIncident: (sessionId: string, deviceType: 'camera' | 'document_camera' | 'microphone', errorCode: string, message: string): Promise<void> =>
    request(`/v1/sessions/${sessionId}/device-incidents`, {
      method: 'POST',
      body: JSON.stringify({ device_type: deviceType, error_code: errorCode, message, client_action_id: crypto.randomUUID() })
    }),
  renewDeviceLease: (leaseId: string, signal?: AbortSignal): Promise<{ lease_id: string; status: string; expires_at: string }> =>
    request(`/v1/device-leases/${leaseId}/renew`, { method: 'POST', ...(signal ? { signal } : {}) }),
  confirmDeviceFrame: (leaseId: string, width: number, height: number): Promise<{ lease_id: string; status: 'frame_confirmed'; resolved_incidents: number }> =>
    request(`/v1/device-leases/${leaseId}/confirm-frame`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ width, height }),
    }),
  releaseDeviceLease: (leaseId: string, reason: string, signal?: AbortSignal): Promise<{ lease_id: string; status: string }> =>
    request(`/v1/device-leases/${leaseId}/release`, {
      method: 'POST',
      body: JSON.stringify({ reason }), signal
    })
}

export type VoiceCalibrationMicrophone = {
  system_id_hash: string
  label: string
  capabilities: {
    transport: 'browser-media-device'
    echo_cancellation_requested: boolean
    noise_suppression_requested: boolean
    auto_gain_control_requested: boolean
    sample_rate_hz?: number | null
    channel_count?: number | null
  }
}

export type VoiceCalibrationSummary = {
  child_id: string
  status: string
  raw_audio_stored: false
  profile: Record<string, unknown> | null
  vocabulary?: string[]
  microphone?: VoiceCalibrationMicrophone
  consented_at?: string
  updated_at?: string
  vocabulary_applied?: boolean
  vocabulary_status?: 'not_supported_by_current_decoder' | 'cloud_asr_context_enabled'
}
