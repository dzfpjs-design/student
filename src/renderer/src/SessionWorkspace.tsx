import { AgentReplyText, createAgentReplyStore } from './AgentReplyText'
import {claimMistakeCoachingVoice,renderMistakeLearningResult} from './mistakeLearningVoice'
import { saveVoiceInputMode, useVoiceInputMode } from './voiceInputPreference'
import {requestMistakeLearningRefresh, type MistakeLearningLibrary, type MistakeLearningState, type OriginalMistakeModality} from './mistakeLearningTypes'
import {mistakeLearningApi} from './mistakeLearningApi'
import {MISTAKE_WORKSPACE_LISTEN, mistakeWorkspaceVoiceCommand, type MistakeVoiceInput} from './mistakeWorkspaceVoice'
import {MistakeCaptureComposer} from './MistakeCaptureComposer'
import {reflectionSavedReceipt,keepReflectionCompletion,clearReflectionCompletion,type ReflectionSavedReceipt} from './reflectionCompletionReceipt'
import {pauseHiddenLearningReceivers,nativeCapabilityNeedsLearningSurface} from './learningSurfaceForeground'
import {MistakeBook,type MistakeWorkspaceReceipt} from './MistakeBook'
import type {MistakeTopicVoiceRequest} from './MistakeBookTopics'
import {NativePreparedQueue,type NativeClientExecutionPort,type NativeClientOutcome} from './nativeClientExecution'
import type {QwenCurriculumTutorDispatch,QwenNativeClientDispatch} from './qwenNativeProtocol'
import { createNativeRuntimeBridge } from './nativeRuntimeBridge'
import { useVoiceRuntimePolicy } from './useVoiceRuntimePolicy'
import { isVoiceInteractionRetry, validatedVoiceInteractionRecovery, voiceInteractionContextKey, voiceInteractionRecoveryMessage } from './voiceInteractionRecovery'
import { validateVoiceInteractionHandoff } from './voiceInteractionHandoff'
import type { VoiceInteractionContext, VoiceInteractionRecovery, VoiceInteractionHandoff } from './types'

type OnboardingHandoffDelivery = { handoffId: string; agentActionId: string }
type TutorialConsumption = boolean | { transcript: string; voiceInputId?: string; handoff?: OnboardingHandoffDelivery }
import { resolvesKnownNameConfirmation } from './knownNameConfirmation'
import { useQuestionViewportFocus } from './useQuestionViewportFocus'
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { ArrowLeft, ArrowRight, BookPlus, BrainCircuit, CalendarClock, Camera, Check, Clock, Coffee, EyeOff, Maximize2, MessageCircle, MessageCircleQuestion, Minimize2, Pause, Play, Plus, RotateCcw, Send, Volume2, VolumeX, X } from 'lucide-react'
import { ApiError, localApi } from './api'
import { useLearningSessionAwake } from './learningSessionAwake'
import { isAgentRequestCheck, AGENT_REQUEST_UNCONFIRMED, pendingAgentRequests, isNonRepeatableAgentHandoff, AGENT_HANDOFF_UNCONFIRMED } from './agentRequestJournal'
import { pendingAgentExecutions, restoreAgentExecutionIdentities, AGENT_EXECUTION_UNCONFIRMED } from './agentExecutionJournal'
import { checkAgentRequests, restoredAgentRequestMessage } from './agentRequestRecovery'
import { acknowledgeRealtimeAdmission, pendingRealtimeAdmissions, prepareRealtimeAdmission, restoredRealtimeAdmissionMessage,
  updateRealtimeAdmission, restoreRealtimeAdmissionIdentities, type RealtimeAdmissionHandoff } from './realtimeAdmissionJournal'
import { checkRealtimeAdmissions, recoverRealtimeCancellations } from './realtimeAdmissionRecovery'
import { shouldResumeRealtimeInput, resumeOneUnsentInput, settleUnsentResumeJournal } from './realtimeUnsentResume'
import { withReceiptRecoveryWindow } from './receiptRecoveryWindow'
import { captureControlledSelection, isControlledSelectionCurrent, removeControlledSelection, clearControlledSelections, matchesControlledSelectionReceipt, offerFromSelectionError, readControlledSelection, resolveControlledSelection,
  saveControlledSelection, selectionPrompt, type ControlledSelectionBinding, type PendingControlledSelection } from './controlledSelection'
import { preparePendingControlledEnd, readPendingControlledEnds, markControlledEndClosed,
  finishPendingControlledEnd, runPendingControlledEnd, PendingControlledEndError, type PendingControlledEnd, type ControlledEndReceipt } from './pendingControlledEnd'
import { SupplyObligationStatus } from './SupplyObligationStatus'
import { AgentObjectiveStatus } from './AgentObjectiveStatus'
import { supplyResumeConflictMessage } from './supplyResumeConflict'
import { schoolSupplyPromptBinding } from './schoolSupplyPromptRecovery'
import { planItemTitle, reviewLabel } from './reviewPresentation'
import { DiagnosticVisual } from './DiagnosticVisual'
import { tutorRequiresLocalSpeech } from './tutorSpeechBoundary'
import { evaluationFeedbackPolicy } from './evaluationFeedbackPolicy'
import { playWritingReadback } from './writingReadback'
import { continueWritingFeedback, writingFeedbackHoldMessage } from './writingFeedbackContinuation'
import { NATIVE_WRITING_CONTROL, NATIVE_WRITING_FEEDBACK, checkNativeWritingSubmission, dispatchRetainedNativeWriting, retainedNativeWritingCurrent, presentNativeWritingFeedback, restoreTypedWritingDraft, isWrittenDiagnostic, isSavedNativeWritingPrompt, sameWritingScope, submitNativeWriting, writingScope, type NativeWritingControl, type NativeWritingFeedback, type TypedWritingDraft } from './nativeWritingFlow'
import { clearDiagnosticCaptureForPrompt, setVoiceQuestionCapture, setVoiceTaskChoiceCapture, trackTaskChoiceCapture, publishOriginalAudioCaptureHint, type VoiceQuestionReference } from './voiceCaptureContext'
import { originalAudioPromptPlayback, registerOriginalAudioCaptureReadiness } from './originalAudioPromptPlayback'
import { DIAGNOSTIC_TASK_START_MESSAGE, DiagnosticTaskStartRequiredError } from './diagnosticTaskStart'
import { createActivityFocusReader } from './activityVoiceFocus'
import type { VoiceOnboardingRestartOutput, TutorBlackboardPresentation } from './types'
import { localApi as commandApi } from './api'
import { createCaptureReceiptBridge, guardCommandApi, isImmediatePrivacyStop, privacyStopKey, privacyStopPending, SupersededDeviceCommand, type CaptureRequest } from './deviceCommandReceipt'
import { clearPendingEvaluation, EVALUATION_STATUS_POLL_DELAYS, isExplicitEvaluationTopicChange, readPendingEvaluation, savePendingEvaluation, type PendingEvaluationReceipt } from './pendingEvaluationReceipt'
import { beginOnboardingResponse } from './onboardingResponseGrant'
import { ordinaryPromptPlayback, ordinaryResponseMayOwnPrompt, registerOrdinarySession, ordinaryTutorPlayback, type OrdinaryResponseGrant } from './ordinaryResponseGrant'
import { cancelKeyedLocalSpeech, cancelLocalSpeech, HandsFreeVoiceInput, speakLocally as playLocalSpeech, VoiceInputButton, type PlaybackOverlap } from './LocalInputs'
import { DocumentCameraCapture } from './DocumentCameraCapture'
import { ContinuousDocumentMonitor } from './ContinuousDocumentMonitor'
import { reconcileObservationRegistration, releaseObservationLease, retainObservationRegistration, type RetainedObservationRegistration } from './observationCameraStartup'
import { reconcileObservationCoverage, retainObservationCoverage, type RetainedObservationCoverage } from './observationCoverageStartup'
import { VoiceMistakeDialogue } from './VoiceMistakeDialogue'
import { MistakePlanActivity } from './MistakePlanActivity'
import { PetBaselineProgress } from './PetBaselineProgress'
import type { AgentRuntimeStatus, AgentTurn, ControlledContentPlayback, ControlledGameSession, DiagnosticItem, DocumentPageRevision, MistakeCaptureQueue, OcrArtifact, PendingBehaviorIntervention, PetBaseline, SensorReleaseSummary, SessionDraftSection, SessionWorkspaceState, TutorTurn } from './types'
import petStudyRoom from './assets/pet-study-room.png'
import { mediaDeviceFailure, mediaDeviceRegistration, openObservationCameraStream, resolveBoundVideoDevice } from './mediaDevices'
import { FRAME_HEALTH_SAMPLE_INTERVAL_MS, FrameHealthMonitor, L523ContentFreshnessMonitor, isL523ObservationSource } from './frameHealth'
import { effectiveActiveMilliseconds, effectiveActiveMinutes } from './studyTime'
import { CompanionAvatar } from './CompanionAvatar'
import { VoiceCalibrationPanel } from './VoiceCalibrationPanel'
import { parentHelpMessage, parseTutorToolRecovery, tutorFailureMessage, tutorSourceBlock, tutorSourceBlockMessage, type TutorToolRecovery } from './agentToolRecovery'
import { acceptsPendingEvaluationControl, freezePendingEvaluation, type PendingEvaluationScope } from './pendingEvaluationControl'
import { prepareCarryoverReview, finishCarryoverReview, readCarryoverReview, invalidateCarryoverReview,
  markCarryoverReadback, carryoverConfirmationAtArrival, refreshCarryoverReadback } from './pendingCarryoverReview'
import { setVoiceLocalRecoveryCapture, setVoicePendingEvaluationCapture, type VoiceCaptureReference } from './voiceCaptureContext'
import type { QwenRealtimeInputCapture } from './qwenRealtimeVoice'
import { playPublicReply } from './publicReplyPlayback'
import { realtimeInputSourceStatus, realtimeInputSourceMessage, sameRealtimeInputCapture } from './realtimeInputCapture'
import { acceptsLocalRecoveryControl, LOCAL_RECOVERY_FAILURE, LOCAL_RECOVERY_GUIDANCE } from './localVoiceRecovery'
import { askTutorWithOcrEvidence, confirmOcrTutorEvidence, createOcrTutorReview, ocrTutorReviewMessage, parseOcrTutorReview, prepareOcrDocumentGoal, readOcrDocumentGoal, type DocumentGoalConfirmation, type OcrTutorReview } from './tutorOcrEvidence'
import { parseTutorRequestRecovery, type TutorRequestRecovery } from './tutorRequestRecovery'
import { KnowledgeGraphDiagnosticGuidance } from './KnowledgeGraphDiagnosticGuidance'
import { avatarModeForRealtimeVoice, wakeListenerMessage, type RealtimeVoiceStatus } from './realtimeVoiceRuntime'
import { useRealtimeVoiceStatus } from './useRealtimeVoiceStatus'
import { PLAYBACK_TAIL_RECOVERY_GUIDANCE, QwenRealtimeVoiceInput } from './QwenRealtimeVoiceInput'
import { selectVoiceCaptureMode, selectVoiceCaptureHandoff, type VoiceCaptureMode } from './voiceCaptureMode'
import { originalAudioCaptureRequired, subscribeVoiceCaptureContext } from './voiceCaptureContext'
import type { QwenRealtimeToolResult } from './qwenRealtimeVoice'
import { resolveVoiceInteraction, VoiceInputQueue, voiceOnboardingSpeechPolicy, VoiceOnboardingRuntime, type VoiceOnboardingBridge, type VoiceOnboardingSnapshot } from './voiceOnboardingRuntime'
import { TutorBlackboard, tutorBlackboardFromTurn, tutorExampleNotice } from './TutorBlackboard'
import { TutorVisual } from './TutorVisual'
import { getCompanionVoiceState, subscribeCompanionVoice } from './companionVoice'
import { useDailyReflectionSummary } from './useDailyReflectionSummary'
import { answerBoundReview, isSchoolOpenReview, loadBoundReview, savedSchoolReviewReply } from './boundReview'
import { requestLegacyVoiceEvent, requestOwnedVoiceEvent, tutorRecoveryCommand, VoiceActionTimeoutError, VoiceActionUnavailableError, VoiceRecoverySaveError, type TutorRecoveryCommand } from './voiceJourneyRuntime'
import { AgentContinuationDriver } from './agentContinuationDriver'
import { ObservationSampleClock } from './observationSampleClock'
import { runDiagnosticVoice } from './diagnosticVoiceFlow'
import { retainedListeningReply } from './listeningDeferredReply'
import { interruptPetListening, isPetListening, playPetListening, stopPetListening } from './petListeningPlayback'
import { consumeExamListeningExecution, type ExamListeningView } from './examListeningDelivery'
import { voiceCaptureReference, trackVoiceLearningCapture } from './voiceCaptureContext'
import { TutorPlaybackController } from './tutorPlaybackController'

export function documentRevisionArtifact(item: DocumentPageRevision): OcrArtifact {
  return {
    artifact_id: item.artifact_id,
    extracted_text: item.extracted_text,
    confidence: item.ocr_confidence,
    local_path: null,
    status: item.review_required ? 'quality_warning' : 'completed',
    details: {
      lines: [],
      engine: 'continuous_document_monitor',
      ocr_confidence: item.ocr_confidence,
      quality: {
        passed: !item.review_required,
        issues: item.review_required ? ['continuous_monitor_review_required'] : [],
        messages: [],
        metrics: {},
        version: 'document-revision-v1'
      }
    }
  }
}

export function hasMarkedWrongQuestion(artifact: OcrArtifact): boolean {
  return Boolean(artifact.details.local_pipeline?.wrong_question_candidates?.some((candidate) => candidate.text.trim().length >= 2))
}
import { appendLocalDiagnosticFollowUp, hasProtectedCourseContent } from './localAtomicTutorContext'
import { assertSupportedAgentAction } from './agentActionContract'
import { startObservationAnalysis, observationAnalysisRetryDelay, OBSERVATION_ANALYSIS_TIMEOUT, type ObservationAnalysisFlight } from './observationAnalysisFlight'
import { CAMERA_DEVICE_COMMAND, CAMERA_DEVICE_STATUS, CameraFrameClock, queryCameraDevices, requestCameraDevice, waitForCameraFrame, type CameraDeviceCommand, type CameraStatusRequest, type DocumentInspectionOutput } from './cameraDeviceRuntime'
import { connectRequestedVideoDevices, deviceConnectSpeech, deviceConnectSucceeded, OBSERVATION_CAMERA_REMOVED_MESSAGE, type DeviceConnectTarget } from './deviceConnectionRuntime'
import { agentContinuationRecoveryMessage, agentWakeResumePrompt } from './agentWakeResumePrompt'
import { acknowledgeProfileSwitch, prepareProfileSwitch, ProfileSwitchPendingError, recordProfileSwitchDestination } from './profileSwitchHandoff'
import { clearTutorCompletion, readTutorCompletions, saveTutorCompletion, tutorCompletionMessage, waitForTutorReceipt, type TutorCompletion } from './tutorCompletionJournal'

const STEPS = [
  ['check_in', '报到'],
  ['classroom_recall', '课堂复盘'],
  ['homework_capture', '作业'],
  ['plan_review', '计划'],
  ['study', '学习'],
  ['reflection', '复盘'],
  ['completed', '完成']
] as const

const DOCUMENT_REVIEW_REVISION_EVENT = 'hsc-document-review-revision'

const homeworkTypeLabels: Record<string, string> = {
  written: '书面作业',
  memorization: '背诵',
  reading: '阅读',
  correction: '错题订正',
  review: '复习',
  preview: '预习',
  test_prep: '测验准备'
}

const completionChecksByTaskType: Record<string, Array<{ id: string; label: string }>> = {
  written: [{ id: 'task_finished', label: '指定内容已完成' }, { id: 'missing_items_checked', label: '已检查漏题和空白' }],
  memorization: [{ id: 'recall_attempted', label: '已进行一次脱稿自测' }, { id: 'uncertain_parts_marked', label: '已标出还不熟的部分' }],
  reading: [{ id: 'range_completed', label: '已读完指定范围' }, { id: 'main_idea_recalled', label: '能说出主要内容' }],
  correction: [{ id: 'correction_finished', label: '订正已完成' }, { id: 'error_cause_explained', label: '能说清原来错在哪里' }],
  review: [{ id: 'review_completed', label: '已完成本次复习' }, { id: 'self_tested', label: '已用一道题或口述自测' }],
  preview: [{ id: 'preview_completed', label: '已完成指定预习' }, { id: 'question_recorded', label: '已记下一个不理解的问题' }],
  test_prep: [{ id: 'materials_reviewed', label: '已完成必要准备' }, { id: 'uncertainty_marked', label: '已标出仍不确定的地方' }],
  reasoning: [{ id: 'attempt_completed', label: '已完成一次独立尝试' }, { id: 'reasoning_explained', label: '能说出主要思路' }],
  speaking: [{ id: 'spoken_response_recorded', label: '已提交本次英语口语转写' }, { id: 'task_points_covered', label: '已核对要求的任务点' }],
  writing: [{ id: 'writing_submitted', label: '已提交本次英文写作' }, { id: 'task_points_covered', label: '已核对要求的任务点' }]
}

function boundedExecutionResult(value: unknown): unknown {
  if (value == null) return null
  try {
    const serialized = JSON.stringify(value)
    if (serialized.length <= 16_000) return value
    return { truncated: true, serialized_bytes: new TextEncoder().encode(serialized).length }
  } catch {
    return { serialization_failed: true }
  }
}

function useLocalPrivacyStop(sessionId: string): boolean {
  const [stopped, setStopped] = useState(() => privacyStopPending(sessionId))
  useEffect(() => {
    const pause = (): void => setStopped(true)
    window.addEventListener('hsc-privacy-pause', pause)
    return () => window.removeEventListener('hsc-privacy-pause', pause)
  }, [sessionId])
  return stopped
}

const tutorialSpeechOwners = new Map<string, symbol>()
const DIAGNOSTIC_READ_FAILURE_MESSAGE = '这道题暂时没能核对，题目和图片已暂时收起。请说“再读一遍这道题”，我会重新核对，不会另出新题。'
type DiagnosticReplayReadDetail = {
  sessionId: string; childId: string; promptId: string; itemVersion: string
  requestId: string; phase: 'begin' | 'failed'; handled?: boolean
}
function speakLocally(...args: Parameters<typeof playLocalSpeech>): void {
  if (args[1] && privacyStopPending(args[1])) return
  if (args[1] && tutorialSpeechOwners.has(args[1])) return
  playLocalSpeech(...args)
}

function speakOrdinaryPrompt(text:string,session:SessionWorkspaceState['session'],options:import('./companionVoice').CompanionSpeechOptions={}):void {
  const eligible=()=>ordinaryResponseMayOwnPrompt(session.session_id,session.child_id,options)
  if(!eligible()) {speakLocally(text,session.session_id,undefined,undefined,options);return}
  let permit:ReturnType<typeof ordinaryPromptPlayback>|undefined,admissionStarted=false
  const current=()=>!privacyStopPending(session.session_id)&&!tutorialSpeechOwners.has(session.session_id)&&(options.isCurrent?.()??true)
  const create=()=>{
    if(eligible())permit=ordinaryPromptPlayback({sessionId:session.session_id,childId:session.child_id,state:session.state,prompt:{},text,
      isCurrent:current},options)
  }
  if(!options.beforePlayback)create()
  // A real admission read can install a dedicated capture binding. Freeze an
  // ordinary response only after that read; never renew the earlier epoch.
  const beforePlayback=options.beforePlayback?async(signal:AbortSignal)=>{
    if(admissionStarted)return false
    admissionStarted=true
    if(!await options.beforePlayback!(signal)||signal.aborted||!current())return false
    create()
    const allowed=permit?permit.grant.playbackCurrent():true
    if(!allowed)permit?.grant.revoke()
    return allowed
  }:undefined
  speakLocally(text,session.session_id,result=>{if(result==='text_only')permit?.grant.revoke()},()=>permit?.started(),{
    ...options,...(beforePlayback?{beforePlayback}:{}),
    isCurrent:()=> current()&&(!permit||(permit.options.isCurrent?.()??false)),
    onPlaybackEnd:()=>{if(permit)permit.options.onPlaybackEnd?.();else options.onPlaybackEnd?.()},
    onNotDelivered:reason=>{if(permit)permit.options.onNotDelivered?.(reason);else options.onNotDelivered?.(reason)},
  })
}

type AgentReplySpeechPolicy = { skipPublicReplyProvider?:boolean;ordinaryResponseAllowed?:boolean;sourceTutorTurnId?:string;localOnly: boolean; sourceAgentTurnId?: string; onPlaybackStart?: () => void; onPlaybackAbandoned?: () => void } & import('./companionVoice').CompanionSpeechOptions
type RealtimeSpeechCapture = {
  messages: Array<{ text: string; onFinished?: () => void; policy?: AgentReplySpeechPolicy }>
  localPlaybackUsed: boolean
  tutorPlaybackOwned?: boolean
  inputSource?: QwenRealtimeInputCapture
}
type RealtimeAdmissionOutcome = { record?: Awaited<ReturnType<typeof commandApi.admitRealtimeVoice>>; failed?: true }
type RealtimeAdmissionAttempt = { utterance: string; capture: QwenRealtimeInputCapture; admission: Promise<RealtimeAdmissionOutcome>; handoff: RealtimeAdmissionHandoff }
function agentReplySpeechPolicy(turn: AgentTurn): AgentReplySpeechPolicy {
  const action = turn.action as AgentTurn['action'] & { local_only?: boolean; cloud_transmission_allowed?: boolean }
  return {
    localOnly: !turn.agent_turn_id || action.local_only === true || action.cloud_transmission_allowed === false
      || hasProtectedCourseContent(action.local_learning_context?.course_context)
      || turn.intent === 'memory_command' || action.source === 'local_companion_memory',
    ...(turn.agent_turn_id ? { sourceAgentTurnId: turn.agent_turn_id, ordinaryResponseAllowed:true } : {}),
  }
}

function tutorExecutionSummary(turn: TutorTurn): Record<string, unknown> {
  return {
    tutor_turn_id: turn.turn_id,
    provider: turn.provider,
    model: turn.model,
    response_kind: turn.response_kind,
    verification_status: turn.verification_status,
    safety_status: turn.safety_status,
    blackboard_present: Boolean(turn.output_envelope?.result?.blackboard),
  }
}

type StudyCaptureVoiceDetail = {
  sessionId: string
  childId: string
  signal?: AbortSignal
  goal?: DocumentGoalConfirmation
  correctedText?: string
  resolve: (handled: boolean) => void
}

function dispatchStudyCaptureVoiceAction(sessionId: string, childId: string, type: 'confirm' | 'retry' | 'cancel', correctedText?: string, signal?: AbortSignal, goal?: DocumentGoalConfirmation): Promise<boolean> {
  return requestOwnedVoiceEvent(`hsc-${type}-question-capture`, { sessionId, childId, signal, correctedText, ...(goal ? { goal } : {}) }, signal)
}

export function isCaptureReadinessAcknowledgement(value: string): boolean {
  const compact = value.trim().replace(/[\s，。！？,.!?]/g, '')
  return /^(?:好了|放好了|摆好了|准备好了|可以拍了|拍吧|来了|在这里|就是这题|就是这道题|这道题)$/.test(compact)
}

type DocumentMonitorVoiceDetail = {
  action: 'pause' | 'resume' | 'inspect' | 'review'
  resolve: (handled: boolean) => void
}

async function dispatchDocumentMonitorVoiceAction(sessionId: string, action: DocumentMonitorVoiceDetail['action'], signal?: AbortSignal, report?: (output: DocumentInspectionOutput) => void, deferReview = false): Promise<boolean> {
  const controller = new AbortController()
  const abort = (): void => controller.abort()
  if (signal?.aborted) abort()
  signal?.addEventListener('abort', abort, { once: true })
  try { return await requestOwnedVoiceEvent('hsc-document-monitor-voice-action', { sessionId, action, report, deferReview, signal: controller.signal }, controller.signal) }
  finally { signal?.removeEventListener('abort', abort); controller.abort() }
}

function dispatchMistakeRecoveryVoiceAction(action: 'reload' | 'sync', signal?: AbortSignal): Promise<boolean> {
  return requestLegacyVoiceEvent('hsc-mistake-recovery-voice-action', { action }, signal)
}

type MistakeCaptureVoiceAction = 'open' | 'set_answer' | 'correct_prompt' | 'save' | 'skip' | 'close'

function dispatchMistakeCaptureVoiceAction(sessionId: string, childId: string, action: MistakeCaptureVoiceAction, value?: string, signal?: AbortSignal,
  report?: (dialogue: import('./types').MistakeDialogue) => void): Promise<boolean> {
  return requestOwnedVoiceEvent('hsc-mistake-capture-voice-action', { sessionId, childId, action, value, signal, report }, signal)
}

type StudyLearningVoiceDetail = {
  action: 'start_diagnostic' | 'replay_diagnostic' | 'submit_diagnostic_answer' | 'record_tutor_feedback' | 'more_tutor_hint'
  track?: 'school' | 'pet' | 'competition_math'
  answer?: string
  outcome?: 'understood' | 'still_stuck'
  feedbackText?: string
  resolve: (handled: boolean) => void
}

type TutorPresentationVoiceDetail = {
  turn: TutorTurn
  speechPolicy?: AgentReplySpeechPolicy
  signal?: AbortSignal
  resolve: (handled: boolean) => void
}

function dispatchTutorPresentation(turn: TutorTurn, speechPolicy?: AgentReplySpeechPolicy, signal?: AbortSignal): Promise<boolean> {
  return requestOwnedVoiceEvent('hsc-runtime-tutor-presentation', { turn, speechPolicy, signal }, signal)
}

type ReflectionVoiceDetail = {
  action: 'update' | 'complete'
  field?: 'learned' | 'difficult' | 'next_action'
  value?: string
  skipped?: boolean
  afterSaved?: (saved: SessionWorkspaceState) => Promise<void>
  resolve: (handled: boolean) => void
}

function dispatchReflectionVoiceAction(detail: Omit<ReflectionVoiceDetail, 'resolve'>, signal?: AbortSignal): Promise<boolean> {
  return requestOwnedVoiceEvent('hsc-reflection-voice-action', detail, signal)
}

type MistakeVoiceDetail = {
  action: 'review' | 'diagnose' | 'remediate' | 'answer'
  decision?: 'confirm' | 'exclude'
  errorType?: string
  explanation?: string
  outcome?: 'completed' | 'still_stuck'
  notes?: string
  answer?: string
  resolve: (handled: boolean) => void
}

function dispatchMistakeVoiceAction(detail: Omit<MistakeVoiceDetail, 'resolve'>, signal?: AbortSignal): Promise<boolean> {
  return requestLegacyVoiceEvent('hsc-mistake-voice-action', detail, signal)
}

type DraftSaveState = 'idle' | 'saving' | 'saved' | 'error'

function useAutosavedDraft(
  sessionId: string,
  section: SessionDraftSection,
  payload: Record<string, unknown>,
  meaningful: boolean,
  restored: boolean
): { state: DraftSaveState; clear: () => Promise<void>; markCommitted: () => void; saveNow: (value: Record<string, unknown>) => Promise<void> } {
  const serialized = JSON.stringify(payload)
  const [state, setState] = useState<DraftSaveState>(restored ? 'saved' : 'idle')
  const initializedRef = useRef(false)
  const latestRef = useRef({ payload, serialized, meaningful })
  const lastSavedRef = useRef(restored ? serialized : '')
  const queueRef = useRef<Promise<unknown>>(Promise.resolve())
  const timerRef = useRef<number | null>(null)
  const criticalRef = useRef<{ payload: Record<string, unknown>; serialized: string } | null>(null)
  if (criticalRef.current?.serialized === serialized) criticalRef.current = null
  if (!criticalRef.current) latestRef.current = { payload, serialized, meaningful }

  useEffect(() => {
    if (!initializedRef.current) {
      initializedRef.current = true
      return
    }
    if (criticalRef.current || !meaningful || serialized === lastSavedRef.current) return
    setState('saving')
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      queueRef.current = queueRef.current
        .catch(() => undefined)
        .then(() => localApi.saveSessionDraft(sessionId, section, payload))
        .then(() => {
          lastSavedRef.current = serialized
          if (latestRef.current.serialized === serialized) setState('saved')
        })
        .catch(() => setState('error'))
    }, 600)
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [meaningful, section, serialized, sessionId])

  useEffect(() => () => {
    const latest = latestRef.current
    if (!latest.meaningful || latest.serialized === lastSavedRef.current) return
    void queueRef.current
      .catch(() => undefined)
      .then(() => localApi.saveSessionDraft(sessionId, section, latest.payload))
      .catch(() => undefined)
  }, [section, sessionId])

  const saveNow = async (value: Record<string, unknown>): Promise<void> => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = null
    const snapshot = { payload: value, serialized: JSON.stringify(value) }
    criticalRef.current = snapshot
    latestRef.current = { ...snapshot, meaningful: true }
    setState('saving')
    queueRef.current = queueRef.current.catch(() => undefined)
      .then(() => localApi.saveSessionDraft(sessionId, section, value))
    try {
      await queueRef.current
      lastSavedRef.current = snapshot.serialized
      if (latestRef.current.serialized === snapshot.serialized) setState('saved')
    } catch { setState('error'); throw new VoiceRecoverySaveError() }
  }

  const clear = async (): Promise<void> => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = null
    criticalRef.current = null
    latestRef.current.meaningful = false
    queueRef.current = queueRef.current
      .catch(() => undefined)
      .then(() => localApi.clearSessionDraft(sessionId, section))
    await queueRef.current
    lastSavedRef.current = ''
    setState('idle')
  }

  const markCommitted = (): void => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = null
    latestRef.current.meaningful = false
    lastSavedRef.current = latestRef.current.serialized
    setState('idle')
  }

  return { state, clear, markCommitted, saveNow }
}

function DraftSaveStatus({ state, restored }: { state: DraftSaveState; restored: boolean }): React.JSX.Element {
  const text = state === 'saving' ? '正在保存草稿' : state === 'saved' ? (restored ? '已恢复并保存草稿' : '草稿已保存') : state === 'error' ? '草稿保存失败，内容仍在当前页面' : '修改后会自动保存'
  return <span className={`draft-save-status is-${state}`}>{text}</span>
}

function SensorReleaseStatus({ summary }: { summary: SensorReleaseSummary }): React.JSX.Element {
  if (summary.active_leases > 0) {
    return <div className="sensor-release-status is-warning"><EyeOff size={18} /><span>本地服务仍记录{summary.active_leases}个活动设备租约。请先返回首页，再让家长从运行状态重试释放。</span></div>
  }
  if (summary.evidence_level === 'not_opened') {
    return <div className="sensor-release-status"><EyeOff size={18} /><span>本次会话没有登记设备租约。应用当前没有继续请求摄像头或麦克风；Windows系统级状态仍将在实机验收。</span></div>
  }
  if (summary.os_release_verified && summary.release_verification) {
    return <div className="sensor-release-status is-confirmed"><EyeOff size={18} /><span>本次{summary.released_leases}个设备租约已释放；家长已于{new Date(summary.release_verification.verified_at).toLocaleString()}完成 Windows 隐私指示和其他应用重开复核。</span></div>
  }
  if (summary.release_verification?.result === 'failed') {
    return <div className="sensor-release-status is-warning"><EyeOff size={18} /><span>应用已记录本次{summary.released_leases}个租约释放，但最近一次 Windows 系统复核未通过；请让家长在家长中心重新检查。</span></div>
  }
  return <div className="sensor-release-status is-confirmed"><EyeOff size={18} /><span>本地服务已记录本次{summary.released_leases}个设备租约全部释放。Windows系统级占用状态仍将在真实设备上复核。</span></div>
}

function draftString(payload: Record<string, unknown>, key: string, fallback = ''): string {
  return typeof payload[key] === 'string' ? payload[key] as string : fallback
}

function draftNumber(payload: Record<string, unknown>, key: string, fallback: number): number {
  return typeof payload[key] === 'number' && Number.isFinite(payload[key]) ? payload[key] as number : fallback
}

type ControlledEndTarget = {child_id: string; session_id: string; content_session_id?: string; game_session_id?: string}
function prepareControlledEnd(kind: 'video' | 'game', target: ControlledEndTarget, reason: string, seconds: number): PendingControlledEnd {
  return preparePendingControlledEnd({kind, childId: target.child_id, sessionId: target.session_id,
    activityId: (kind === 'video' ? target.content_session_id : target.game_session_id) ?? ''},
  {reason, actualSeconds: seconds})
}

async function confirmPendingClosure(pending: PendingControlledEnd): Promise<PendingControlledEnd> {
  if (pending.phase === 'closed') return pending
  const {kind, reason, activityId} = pending
  const close = kind === 'video' ? window.desktop?.closeControlledMedia : window.desktop?.closeControlledGame
  const receipt = close ? await close(reason, activityId).catch(() => null) : null
  if (!receipt?.closed || receipt.sessionId !== activityId) throw new VoiceActionUnavailableError(kind === 'video'
    ? '尚未确认视频窗口关闭，观看记录仍然保留。请再说一次停止视频；仍不成功时请家长检查播放器。'
    : '尚未确认游戏退出，游戏记录仍然保留。请再说一次停止游戏；仍不成功时请家长检查游戏进程。')
  return markControlledEndClosed(pending, receipt)
}

async function confirmControlledClosure(kind: 'video' | 'game', reason: string, target: ControlledEndTarget): Promise<void> {
  await confirmPendingClosure(prepareControlledEnd(kind, target, reason, 0))
}

async function settlePendingControlledActivity(pending: PendingControlledEnd): Promise<ControlledEndReceipt> {
  pending = markControlledEndClosed(pending, {closed: pending.phase === 'closed', sessionId: pending.activityId})
  const {kind, activityId: id, sessionId, childId, reason, actualSeconds: seconds} = pending
  const video = kind === 'video'
  if (pending.phase !== 'closed') throw new VoiceActionUnavailableError('原活动关闭仍待确认，没有提交结束记录。')
  const receipt = video ? await localApi.endControlledContent(sessionId, reason, seconds, id)
    : await localApi.endControlledGame(sessionId, reason, seconds, id)
  if (!('child_id' in receipt) || receipt.child_id !== childId || receipt.session_id !== sessionId
    || (video ? (receipt as ControlledContentPlayback).content_session_id : (receipt as ControlledGameSession).game_session_id) !== id
    || !['completed', 'stopped', 'expired', 'interrupted', 'failed'].includes(receipt.status)) {
    throw new VoiceActionUnavailableError('原活动结束回执尚未核对，记录仍然保留。')
  }
  const facts = receipt as typeof receipt & {task_resumed?: boolean}
  if (facts.task_resumed !== false) throw new VoiceActionUnavailableError('活动结束回执未能确认没有自动恢复学习，原结束记录仍然保留。')
  const result: ControlledEndReceipt = {child_id: childId, session_id: sessionId,
    ...(video ? {content_session_id: id} : {game_session_id: id}), status: receipt.status, device_closed: true,
    ...(typeof receipt.replayed === 'boolean' ? {replayed: receipt.replayed} : {}), task_resumed: false}
  await window.desktop?.finishControlledLaunch?.(kind,id)
  finishPendingControlledEnd(pending, result)
  return result
}

function resumeControlledEnd(pending: PendingControlledEnd, observedClosed = false,
  beforeSettle?: () => void): Promise<ControlledEndReceipt> {
  return runPendingControlledEnd(pending, async current => {
    const closed = observedClosed
      ? markControlledEndClosed(current, {closed: true, sessionId: current.activityId})
      : await confirmPendingClosure(current)
    beforeSettle?.()
    return settlePendingControlledActivity(closed)
  })
}

function settleControlledActivity(kind: 'video' | 'game', target: ControlledEndTarget,
  reason: string, seconds: number, observedClosed = false, beforeSettle?: () => void): Promise<ControlledEndReceipt> {
  // Persist synchronously before native close or HTTP, including callbacks and unmount cleanup.
  try { return resumeControlledEnd(prepareControlledEnd(kind, target, reason, seconds), observedClosed, beforeSettle) }
  catch (error) { return Promise.reject(error) }
}

async function retryControlledEnds(pending: PendingControlledEnd[], isCurrent: () => boolean,
  onCompleted: (receipt: ControlledEndReceipt) => void): Promise<ControlledEndReceipt | {
  completed_ends: ControlledEndReceipt[]; device_closed: true; task_resumed: false
}> {
  const completed: ControlledEndReceipt[] = []
  let failed = 0
  for (const entry of pending) {
    if (!isCurrent()) throw new SupersededDeviceCommand()
    try {
      const receipt = await resumeControlledEnd(entry, false,
        () => { if (!isCurrent()) throw new SupersededDeviceCommand() })
      completed.push(receipt)
      onCompleted(receipt)
    } catch (cause) {
      if (cause instanceof SupersededDeviceCommand) throw cause
      failed += 1
    }
  }
  if (failed) throw new VoiceActionUnavailableError(`原活动结束记录还没有全部确认，还有${failed}条保留待核对。恢复连接后再说“停止${pending[0].kind === 'video' ? '视频' : '游戏'}”，我会重试原记录，不会操作新活动。`)
  return completed.length === 1 ? completed[0] : {completed_ends: completed, device_closed: true, task_resumed: false}
}

async function recoverControlledEnds(kind: 'video' | 'game', sessionId: string, childId: string,
  isLive: () => boolean, report: (message: string, activityId: string, recovered?: boolean) => void): Promise<boolean> {
  let recoveredLaunch=false
  const launches=await window.desktop?.listControlledLaunches?.({kind,sessionId,childId})??[]
  for(const launch of launches){
    if(!isLive())return true
    if(launch.kind!==kind||launch.childId!==childId||launch.sessionId!==sessionId)throw new VoiceActionUnavailableError('启动恢复记录归属不一致，没有操作其它活动。')
    const proof=await window.desktop?.cancelControlledLaunch?.(launch.intentId)
    if(!isLive())return true
    if(proof?.closed&&proof.noActivity===true&&proof.proof==='cancelled_before_start'){
      recoveredLaunch=true
      report('原启动请求已取消，没有打开活动或自动恢复学习。','',true)
      continue
    }
    const binding=proof?.binding
    if(!proof?.closed||!binding||binding.intentId!==launch.intentId||binding.childId!==childId||binding.sessionId!==sessionId||binding.kind!==kind||binding.requestId!==launch.requestId||proof.sessionId!==binding.activityId){
      throw new VoiceActionUnavailableError('原启动是否执行仍待核对，没有重新播放或启动。请恢复连接后再说停止原活动。')
    }
    await settleControlledActivity(kind,{child_id:childId,session_id:sessionId,
      ...(kind==='video'?{content_session_id:binding.activityId}:{game_session_id:binding.activityId})},'launch_cancelled',0,true,
      ()=>{if(!isLive())throw new SupersededDeviceCommand()})
    recoveredLaunch=true
    if(isLive())report(['cancelled_before_dispatch','owner_exit_before_registration'].includes(proof.proof??'')
      ?'原启动已安全撤销，没有重新打开活动或自动恢复学习。'
      :'原活动结束记录已经核对，没有重新打开活动或自动恢复学习。',binding.activityId,true)
  }
  const pending = readPendingControlledEnds(sessionId, childId, kind)
  for (const entry of pending) {
    if (!isLive()) break
    try {
      await resumeControlledEnd(entry)
      if (isLive()) report(kind === 'video' ? '上次视频结束记录已经核对，没有重新播放或自动恢复学习。'
        : '上次游戏结束记录已经核对，没有重新启动游戏或自动恢复学习。', entry.activityId, true)
    }
    catch { if (isLive()) report('上次活动的结束记录尚未核对，原记录仍保留。请恢复连接后再说停止原活动。', entry.activityId) }
  }
  // A pending original end never authorizes closing a different active resource returned by GET.
  return pending.length > 0 || recoveredLaunch
}

class ControlledStartRecoveryError extends VoiceActionUnavailableError {}

async function rejectNativeStart(error:unknown,kind:'video'|'game',sessionId:string,childId:string,intentId:string):Promise<never>{
  const offer=offerFromSelectionError(error,kind,sessionId,childId)
  const denied=error instanceof ApiError && error.status===409
    && error.code===(kind==='video'?'CONTROLLED_CONTENT_UNAVAILABLE':'CONTROLLED_GAME_UNAVAILABLE')
  if(!offer&&!denied)throw error
  // An explicit negative HTTP response is not closure proof: fence and read
  // the original native request, then require confirmed backend no-start.
  const proof=await window.desktop?.cancelControlledLaunch?.(intentId).catch(()=>null)
  if(proof?.closed!==true||proof.noActivity!==true||proof.proof!=='cancelled_before_start'){
    throw new ControlledStartRecoveryError('原启动是否执行仍待核对，没有重新打开或进入选择。请恢复连接后说停止原活动，核对原请求。')
  }
  throw error
}

async function prepareNativeLaunch(kind:'video'|'game',sessionId:string,childId:string,requestId:string,isCurrent:()=>boolean):Promise<{intentId:string;nativeEpoch?:string}>{
  if(!window.desktop?.prepareControlledLaunch)throw new VoiceActionUnavailableError('本机启动恢复能力不可用，没有开始活动。请家长检查应用版本。')
  const intent=await window.desktop.prepareControlledLaunch({kind,sessionId,childId,requestId})
  if(!isCurrent())throw new SupersededDeviceCommand()
  if(intent.kind!==kind||intent.sessionId!==sessionId||intent.childId!==childId||intent.requestId!==requestId||!intent.intentId||intent.state!=='prepared'){
    throw new VoiceActionUnavailableError('原启动请求仍待核对，没有创建新活动。请先停止并核对原记录。')
  }
  return {intentId:intent.intentId,nativeEpoch:intent.nativeEpoch}
}

export function SessionWorkspace({
  initial,
  activeChild,
  childName,
  childGrade,
  initialVoiceCommand,
  initialHouseholdEntry,
  initialWakeOnly,
  onInitialVoiceCommandConsumed,
  onOpenMistakeBook,
  onSwitchChild,
  onBack
}: {
  initial: SessionWorkspaceState
  activeChild?:import('./types').ChildProfile
  childName: string
  childGrade: string
  initialVoiceCommand?: string
  initialHouseholdEntry?:import('./types').HouseholdNativeEntry
  initialWakeOnly?: boolean
  onInitialVoiceCommandConsumed?: () => void
  onOpenMistakeBook?: (destination?:'learning'|'mistake_book') => Promise<unknown>
  onSwitchChild?: VoiceProfileSwitch
  onBack: (sessionActive: boolean, saved?: ReflectionSavedReceipt) => void
}): React.JSX.Element {
  const [workspace, setWorkspace] = useState(initial)
  const nativeReflectionHandoff = useRef(false)
  const [mistakeWorkspaceOpen,setMistakeWorkspaceOpen]=useState(false)
  useLayoutEffect(()=>mistakeWorkspaceOpen?pauseHiddenLearningReceivers():undefined,[mistakeWorkspaceOpen])
  const [mistakeWorkspaceMounted,setMistakeWorkspaceMounted]=useState(false)
  const mistakeWorkspaceReady=useRef<MistakeWorkspaceReceipt|null>(null)
  const mistakeWorkspaceWaiter=useRef<{resolve:(receipt:MistakeWorkspaceReceipt)=>void;reject:(error:Error)=>void;timer:ReturnType<typeof setTimeout>}|null>(null)
  const learningWorkspaceWaiter=useRef<{resolve:(value:MistakeWorkspaceReceipt)=>void;reject:(error:Error)=>void;timer:ReturnType<typeof setTimeout>}|null>(null)
  useLayoutEffect(()=>{if(!mistakeWorkspaceOpen&&learningWorkspaceWaiter.current){const done=learningWorkspaceWaiter.current;learningWorkspaceWaiter.current=null;clearTimeout(done.timer);done.resolve({workspace_opened:true,workspace:'learning',child_id:workspace.session.child_id,session_id:workspace.session.session_id,content_status:'ready'})}},[mistakeWorkspaceOpen,workspace.session.child_id,workspace.session.session_id])
  const localMistakeWorkspace=async(destination:'learning'|'mistake_book'='mistake_book'):Promise<unknown>=>{
    if(!activeChild||activeChild.child_id!==workspace.session.child_id){
      if(destination==='learning')throw Error('当前学习工作区身份尚未读到。')
      if(onOpenMistakeBook)return onOpenMistakeBook(destination)
      throw Error('当前孩子的错题工作区尚未读到。')
    }
    if(destination==='learning'){
      if(!mistakeWorkspaceOpen)return {workspace_opened:true,workspace:'learning',child_id:workspace.session.child_id,session_id:workspace.session.session_id,content_status:'ready'}
      const ready=new Promise<MistakeWorkspaceReceipt>((resolve,reject)=>{
        const timer=setTimeout(()=>{if(learningWorkspaceWaiter.current?.timer===timer){learningWorkspaceWaiter.current=null;reject(Error('返回学习的界面尚未确认。'))}},15000)
        learningWorkspaceWaiter.current={resolve,reject,timer}
      })
      setMistakeWorkspaceOpen(false);mistakeWorkspaceReady.current=null
      return ready
    }
    if(mistakeWorkspaceOpen&&mistakeWorkspaceReady.current)return mistakeWorkspaceReady.current
    if(mistakeWorkspaceWaiter.current)throw Error('错题工作区仍在读取，原打开请求没有重做。')
    const ready=new Promise<MistakeWorkspaceReceipt>((resolve,reject)=>{
      const timer=setTimeout(()=>{if(mistakeWorkspaceWaiter.current?.timer===timer){mistakeWorkspaceWaiter.current=null;reject(Error('错题工作区读取尚未确认，学习会话与语音仍保留。'))}},15000)
      mistakeWorkspaceWaiter.current={resolve,reject,timer}
    })
    setMistakeWorkspaceMounted(true);setMistakeWorkspaceOpen(true);return ready
  }
  useEffect(()=>()=>{const returning=learningWorkspaceWaiter.current;learningWorkspaceWaiter.current=null;if(returning){clearTimeout(returning.timer);returning.reject(Error('原学习工作区已离开，没有确认返回。'))}const pending=mistakeWorkspaceWaiter.current;mistakeWorkspaceWaiter.current=null;if(pending){clearTimeout(pending.timer);pending.reject(Error('原学习工作区已离开，没有确认错题本已打开。'))}},[])
  const awakeWarning = useLearningSessionAwake(workspace.session)
  useLayoutEffect(()=>workspace.session.status==='active'?registerOrdinarySession({sessionId:workspace.session.session_id,childId:workspace.session.child_id,state:workspace.session.state}):undefined,
    [workspace.session.session_id,workspace.session.child_id,workspace.session.state,workspace.session.status])
  const privacyStopped = useLocalPrivacyStop(initial.session.session_id)
  const mistakeWorkspaceRef = useRef<HTMLDivElement>(null)
  const [topicVoiceRequest, setTopicVoiceRequest] = useState<MistakeTopicVoiceRequest | null>(null)
  const mistakeReviewCapture = useRef<((signal: AbortSignal) => Promise<string>) | null>(null)
  const waitingMistakeVoice = useRef<{at:number; mistakeId:string; sourceRevision:string; accept:(value:MistakeVoiceInput)=>void; cancel:()=>void} | null>(null)
  const scopedMistakeReview = useRef<AbortController | null>(null)
  const mistakeScope = {sessionId:workspace.session.session_id,childId:workspace.session.child_id,
    usable:workspace.session.status==='active'&&!privacyStopped,open:mistakeWorkspaceOpen}
  const latestMistakeScope = useRef(mistakeScope)
  latestMistakeScope.current = mistakeScope
  const [documentCameraStatus, setDocumentCameraStatus] = useState<'inactive'|'starting'|'active'|'paused'|'unavailable'>('inactive')
  const [mistakeIntake, setMistakeIntake] = useState<MistakeCaptureQueue|null>(null)
  const [mistakeIntakeNotice, setMistakeIntakeNotice] = useState('')
  const waitingMistakeIntake = useRef(false)
  const [mistakePendingCount, setMistakePendingCount] = useState<number|undefined>()
  const [mistakeIntakeBusy, setMistakeIntakeBusy] = useState(false)
  const mistakeIntakeFlight = useRef<Promise<void>|null>(null)
  const {sessionId:mistakeSessionId,childId:mistakeChildId} = mistakeScope
  useLayoutEffect(() => {
    if (!mistakeWorkspaceOpen || privacyStopped) {
      waitingMistakeIntake.current=false
      setTopicVoiceRequest(null); setMistakeIntake(null); setMistakeIntakeNotice(''); waitingMistakeVoice.current?.cancel(); scopedMistakeReview.current?.abort()
    }
  }, [mistakeWorkspaceOpen, privacyStopped])
  useEffect(() => () => { waitingMistakeVoice.current?.cancel(); scopedMistakeReview.current?.abort() }, [mistakeSessionId, mistakeChildId])
  useEffect(()=>{
    if(!mistakeWorkspaceOpen||privacyStopped)return
    let current=true
    void localApi.mistakeCaptureQueues(mistakeChildId).then(rows=>{
      if(current)setMistakePendingCount(rows.reduce((count,row)=>count+row.pending_count,0))
    }).catch(()=>undefined)
    return()=>{current=false}
  },[mistakeWorkspaceOpen,mistakeChildId,privacyStopped])

  const mistakeStillCurrent = (open = true):boolean => {
    const latest = latestMistakeScope.current
    return latest.sessionId===mistakeSessionId && latest.childId===mistakeChildId && latest.usable
      && (!open || latest.open) && !privacyStopPending(mistakeSessionId)
  }
  const speakMistakeWorkspaceReply = (text: string, scope: AbortSignal):void => {
    const permitted=()=>!scope.aborted&&mistakeStillCurrent()&&!document.hidden&&!tutorialSpeechOwners.has(mistakeSessionId)
    if(!permitted())return
    const speechKey=`mistake-book:${crypto.randomUUID()}`
    const stop=()=>cancelKeyedLocalSpeech(speechKey)
    const finished=()=>scope.removeEventListener('abort',stop)
    scope.addEventListener('abort',stop,{once:true})
    speakLocally(text,mistakeSessionId,undefined,undefined,{localOnly:true,isCurrent:permitted,speechKey,onPlaybackEnd:finished,onNotDelivered:finished})
  }
  const visibleOriginalMistake = ():string|null => {
    if(mistakeIntake || document.hidden)return null
    const node = mistakeWorkspaceRef.current?.querySelector<HTMLElement>('[data-mistake-review-id]')
    return node?.isConnected && !node.closest('[hidden]') ? node.dataset.mistakeReviewId || null : null
  }
  const sayMistakeWorkspace = (text:string,open=true):void => {
    if (mistakeStillCurrent(open)) speakOrdinaryPrompt(text,workspace.session,{localOnly:true,isCurrent:()=>mistakeStillCurrent(open)})
  }
  const waitForMistakeVoice = (record:MistakeLearningState,signal:AbortSignal):Promise<MistakeVoiceInput> => new Promise((resolve,reject) => {
    signal.throwIfAborted()
    const finish = (error:Error|null, value?:MistakeVoiceInput):void => {
      clearTimeout(timer); signal.removeEventListener('abort',cancel)
      if (waitingMistakeVoice.current?.cancel===cancel) waitingMistakeVoice.current=null
      if(error)reject(error);else resolve(value!)
    }
    const cancel = ():void => finish(new DOMException('已取消本次口述。','AbortError'))
    const timer = setTimeout(()=>finish(new Error('这次没有收到完整口述，请再试一次。')),45_000)
    waitingMistakeVoice.current={at:Date.now(),mistakeId:record.mistake_id,sourceRevision:record.source_revision,accept:value=>finish(null,value),cancel}
    signal.addEventListener('abort',cancel,{once:true})
    window.dispatchEvent(new CustomEvent(MISTAKE_WORKSPACE_LISTEN,{detail:{sessionId:mistakeSessionId,childId:mistakeChildId}}))
  })
  const reviewOriginalMistake = async ({record,method,signal}:{record:MistakeLearningState;method:OriginalMistakeModality;signal:AbortSignal}):Promise<MistakeLearningState> => {
    if (!mistakeStillCurrent() || record.child_id!==mistakeChildId || visibleOriginalMistake()!==record.mistake_id) {
      throw new Error('请先打开要复习的原题。')
    }
    if (scopedMistakeReview.current) throw new Error('正在核对这次复习，请等结果。')
    waitingMistakeIntake.current=false
    const owner = new AbortController()
    scopedMistakeReview.current=owner
    const combined=AbortSignal.any([owner.signal,signal,AbortSignal.timeout(100_000)])
    const check = ():void => {
      combined.throwIfAborted()
      if(!mistakeStillCurrent()||visibleOriginalMistake()!==record.mistake_id)throw new Error('当前题目已变更。请打开原题再重新作答。')
    }
    try {
      check()
      const capture = mistakeReviewCapture.current
      if(method==='paper'&&!capture)throw new Error('高拍仪尚未就绪。可以改用语音讲解。')
      const source = method==='paper' ? {artifact_id:await capture!(combined)} : await waitForMistakeVoice(record,combined)
        .then(value=>({voice_input_id:value.voiceInputId,utterance:value.transcript}))
      check()
      const clientActionId='original-review:'+('artifact_id'in source?source.artifact_id:source.voice_input_id)
      const body={...source,child_id:mistakeChildId,session_id:mistakeSessionId,client_action_id:clientActionId,
        expected_revision:record.revision,source_revision:record.source_revision,modality:method}
      let result: MistakeLearningState
      try { result=await mistakeLearningApi.mutate(record.mistake_id,'review',body,combined) }
      catch (error) {
        check()
        const recovered=await mistakeLearningApi.action(record.mistake_id,mistakeChildId,clientActionId).catch(()=>null)
        if(!recovered?.review_result || recovered.action_status!=='completed')throw error
        result=recovered
      }
      check()
      if(!result.review_result)throw new Error('本次复习结果暂未确认，请刷新错题后重试。')
      const {feedback,next_hint,judgement}=result.review_result
      sayMistakeWorkspace(judgement==='incorrect'&&next_hint ? `${feedback} ${next_hint}` : feedback)
      return result
    } finally { if(scopedMistakeReview.current===owner)scopedMistakeReview.current=null }
  }
  const openMistakeIntake = (kind:'voice'|'camera'):Promise<void> => {
    if(mistakeIntakeFlight.current)return mistakeIntakeFlight.current
    const work = (async ():Promise<void> => {
      if(!mistakeStillCurrent(false))throw new Error('先回到正在进行的学习会话。')
      setMistakeIntakeBusy(true)
      await localMistakeWorkspace()
      if(!mistakeStillCurrent())return
      if(kind==='voice') {
        window.dispatchEvent(new CustomEvent(MISTAKE_WORKSPACE_LISTEN,{detail:{sessionId:mistakeSessionId,childId:mistakeChildId}}))
        sayMistakeWorkspace('请说“录入这道错题”。')
        return
      }
      const queues=await localApi.mistakeCaptureQueues(mistakeChildId)
      if(!mistakeStillCurrent())return
      setMistakePendingCount(queues.reduce((count,row)=>count+row.pending_count,0))
      if(queues[0]) {
        waitingMistakeIntake.current=false
        setMistakeIntake(queues[0]);setMistakeIntakeNotice('');return
      }
      const camera=queryCameraDevices(mistakeSessionId,'document_camera').devices[0]
      waitingMistakeIntake.current=Boolean(camera?.monitor_active)
      const message=camera?.monitor_active?'把有批改的题目放在高拍仪下。识别出错题后会自动保存，需要核对的会留在这里。':'高拍仪尚未看到可用画面。请连接或恢复高拍仪，也可以手动添加题干。'
      setMistakeIntakeNotice(message);sayMistakeWorkspace(message)
    })().finally(()=>{mistakeIntakeFlight.current=null;setMistakeIntakeBusy(false)})
    mistakeIntakeFlight.current=work
    return work
  }
  const handleMistakeWorkspaceVoice = (command:ReturnType<typeof mistakeWorkspaceVoiceCommand>,input:MistakeVoiceInput):boolean => {
    if(!mistakeStillCurrent(false))return false
    const claim = (kind:NonNullable<ReturnType<typeof mistakeWorkspaceVoiceCommand>>|'answer', run:()=>void|Promise<void>, answer?:{after_ms:number;mistake_id:string;source_revision:string}):void=>{
      const requiresOpenBook = kind!=='capture'&&kind!=='topics'&&kind!=='topic_explain'
      void mistakeLearningApi.claimWorkspaceVoice(mistakeSessionId,mistakeChildId,input.voiceInputId,kind,answer)
        .then(async status=>{
          if(!mistakeStillCurrent(requiresOpenBook))return
          if(status==='safety'){waitingMistakeVoice.current?.cancel();input.onSafety?.();return}
          if(status==='accepted')await run()
        })
        .catch((error:unknown)=>{if(error instanceof DOMException&&error.name==='AbortError')return;sayMistakeWorkspace(error instanceof Error?error.message:'这次操作未能确认，请再试一次。',requiresOpenBook)})
    }
    if(command==='capture'){
      claim(command,async()=>{
        waitingMistakeVoice.current?.cancel()
        await openMistakeIntake('camera')
      })
      return true
    }
    if(command==='topics'||command==='topic_explain'){
      if(!activeChild||activeChild.child_id!==mistakeChildId)return false
      claim(command,async()=>{
        waitingMistakeVoice.current?.cancel();scopedMistakeReview.current?.abort();setMistakeIntake(null)
        await localMistakeWorkspace()
        if(mistakeStillCurrent())setTopicVoiceRequest({id:input.voiceInputId,childId:mistakeChildId,sessionId:mistakeSessionId,explain:command==='topic_explain'})
      })
      return true
    }
    if(!mistakeStillCurrent())return false
    const waiting=waitingMistakeVoice.current
    if(command==='cancel'&&scopedMistakeReview.current){
      const review=scopedMistakeReview.current
      claim(command,()=>{if(scopedMistakeReview.current!==review)return;review.abort();sayMistakeWorkspace('已取消本次作答。')})
      return true
    }
    if(waiting && !command && input.capturedAt>=waiting.at && visibleOriginalMistake()===waiting.mistakeId) {
      claim('answer',()=>{if(waitingMistakeVoice.current===waiting&&visibleOriginalMistake()===waiting.mistakeId)waiting.accept(input)},
        {after_ms:waiting.at,mistake_id:waiting.mistakeId,source_revision:waiting.sourceRevision})
      return true
    }
    if(command!=='paper'&&command!=='voice')return false
    const id=visibleOriginalMistake()
    if(!id)return false
    if(scopedMistakeReview.current){sayMistakeWorkspace('正在处理本次复习。');return true}
    claim(command,async()=>{
      if(visibleOriginalMistake()!==id)return
      const signal=AbortSignal.timeout(100_000)
      const record=await mistakeLearningApi.state(id,mistakeChildId,mistakeSessionId,signal)
      if(command==='voice')sayMistakeWorkspace('我在听，请说说这道题的做法和答案。')
      const result=await reviewOriginalMistake({record,method:command,signal})
      requestMistakeLearningRefresh({child_id:mistakeChildId,session_id:mistakeSessionId,mistake_id:id,review:true,
        ...(result.review_result?.judgement==='incorrect'?{panel:'coach'}:{})})
    })
    return true
  }
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const activityFocusReader = useRef(createActivityFocusReader())
  const activityScope = `${initial.session.child_id}:${initial.session.session_id}`
  const activityScopeRef = useRef(activityScope)
  activityScopeRef.current = activityScope
  const activityMounted = useRef(false)
  const refreshActivityFocus = async (requestIsCurrent: () => boolean = () => true): Promise<void> => {
    const scope = activityScope
    await activityFocusReader.current.refresh(initial.session.session_id, initial.session.child_id,
      () => localApi.activityVoiceFocus(initial.session.session_id),
      () => activityMounted.current && activityScopeRef.current === scope && requestIsCurrent())
  }
  useLayoutEffect(() => {
    activityMounted.current = true
    void refreshActivityFocus()
    return () => { activityMounted.current = false; activityFocusReader.current.invalidate() }
  }, [activityScope])

  // A child switch, navigation away, or session completion must not leave a
  // previous child's cloud stream or locally queued PCM audible.
  useEffect(() => () => cancelLocalSpeech(), [])

  // Electron preserves scroll position across some reload/navigation paths.
  // Start each learning workspace at the live-device area so a decoded stream
  // cannot be mistaken for a missing stream merely because it is above view.
  useLayoutEffect(() => {
    document.documentElement.scrollTop = 0
    document.body.scrollTop = 0
  }, [initial.session.session_id])

  const refresh = async (isCurrent: () => boolean = () => true): Promise<void> => {
    if (!isCurrent()) return
    const updated = await localApi.sessionWorkspace(workspace.session.session_id)
    await refreshActivityFocus()
    const current = activityMounted.current && updated.session.session_id === initial.session.session_id
      && updated.session.child_id === initial.session.child_id && activityScopeRef.current === activityScope && isCurrent()
    if (current) setWorkspace(updated)
  }

  const runOwner = useRef<symbol | null>(null)
  const run = async (action: () => Promise<unknown>, isCurrent: () => boolean = () => true): Promise<boolean> => {
    if (!isCurrent()) return false
    const owner = Symbol('workspace-operation')
    runOwner.current = owner
    setBusy(true)
    setError(null)
    try {
      await action()
      if (!isCurrent()) return false
      await refresh(isCurrent)
      return isCurrent()
    } catch (cause) {
      if (isCurrent()) setError(cause instanceof ApiError ? cause.message : '操作没有保存，请重试。')
      return false
    } finally {
      if (runOwner.current === owner) { runOwner.current = null; setBusy(false) }
    }
  }

  useEffect(() => {
    if (workspace.session.status !== 'completed' || workspace.session.state !== 'completed') return
    const timer = window.setTimeout(() => { if (!nativeReflectionHandoff.current && activityMounted.current && activityScopeRef.current === activityScope && !privacyStopPending(initial.session.session_id)) onBack(false) }, 8_000)
    return () => window.clearTimeout(timer)
  }, [onBack, workspace.session.state, workspace.session.status])

  const finishNativeReflection = async (saved: SessionWorkspaceState, acknowledge: () => Promise<void>): Promise<void> => {
    const receipt = reflectionSavedReceipt(saved, initial.session.session_id, initial.session.child_id)
    const current = (): boolean => activityMounted.current && !privacyStopPending(initial.session.session_id)
      && activityScopeRef.current === activityScope
    if (current()) {
      nativeReflectionHandoff.current = true
      setError(null)
      setWorkspace(saved)
    }
    // The saved business record is final. Only its original claimed metadata
    // may finish after AgentCommandPanel has unmounted.
    await acknowledge()
    if (current()) onBack(false, receipt)
  }

  return (
    <main className="session-shell child-voice-only" data-interaction-mode="voice-only">
      <header className="session-header">
        <button className="icon-button" title="返回首页" onClick={() => onBack(workspace.session.status === 'active' && workspace.session.state !== 'completed')}>
          <ArrowLeft size={20} />
        </button>
        <div>
          <span>{childName}</span>
          <h1>{stateTitle(workspace.session.state)}</h1>
        </div>
        <div className="save-indicator"><Check size={17} />会话已保存</div>
      </header>

      {awakeWarning && <p className="inline-notice" role="status">{awakeWarning}</p>}

      <nav className="step-rail" aria-label="今日学习进度">
        {STEPS.map(([state, label], index) => {
          const current = stepIndex(workspace.session.state)
          return (
            <div className={`step ${index === current ? 'is-current' : ''} ${index < current ? 'is-done' : ''}`} key={state}>
              <i>{index < current ? <Check size={13} /> : index + 1}</i>
              <span>{label}</span>
            </div>
          )
        })}
      </nav>

      {error && <div className="inline-error">{error}</div>}

      {workspace.session.status === 'active' && <AgentCommandPanel key={`${workspace.session.child_id}:${workspace.session.session_id}`} workspace={workspace} childName={childName} childGrade={childGrade} run={run} refreshActivityFocus={refreshActivityFocus} initialVoiceCommand={initialVoiceCommand} initialHouseholdEntry={initialHouseholdEntry} learningSurfaceVisible={!mistakeWorkspaceOpen} initialWakeOnly={initialWakeOnly} onInitialVoiceCommandConsumed={onInitialVoiceCommandConsumed} onOpenMistakeBook={localMistakeWorkspace} onMistakeWorkspaceVoice={handleMistakeWorkspaceVoice} onSwitchChild={onSwitchChild} onReflectionSaved={finishNativeReflection} />}
      {!mistakeWorkspaceOpen&&activeChild&&!privacyStopped&&workspace.session.status==='active'&&<div className="embedded-mistake-workspace mistake-workspace-entry">
        <button type="button" className="secondary-button" onClick={()=>{void localMistakeWorkspace().catch(error=>setError(error instanceof Error?error.message:'错题本暂时无法打开。'))}}><BookPlus size={17}/>打开错题本</button>
      </div>}
      {mistakeWorkspaceMounted&&activeChild&&<div ref={mistakeWorkspaceRef} className="embedded-mistake-workspace" hidden={!mistakeWorkspaceOpen} aria-label="错题工作区">
        {mistakeWorkspaceOpen&&!privacyStopped&&(mistakeIntakeBusy||mistakeIntakeNotice)&&<p role="status" className="inline-notice">{mistakeIntakeBusy?'正在读取高拍仪的错题候选…':mistakeIntakeNotice}</p>}
        {mistakeWorkspaceOpen&&!privacyStopped&&mistakeIntake&&<MistakeCaptureComposer key={mistakeIntake.artifact_id}
          childId={mistakeChildId} artifact={mistakeIntake} initialSubject="" onClose={()=>setMistakeIntake(null)}
          onCaptured={item=>{
            requestMistakeLearningRefresh({child_id:mistakeChildId,session_id:mistakeSessionId,mistake_id:item.mistake_id})
            void localApi.mistakeCaptureQueues(mistakeChildId).then(rows=>{if(mistakeStillCurrent())setMistakePendingCount(rows.reduce((sum,row)=>sum+row.pending_count,0))}).catch(()=>undefined)
          }}/>}
        <MistakeBook key={`${workspace.session.child_id}:${privacyStopped?'paused':'active'}`} child={activeChild} embedded active={mistakeWorkspaceOpen&&!privacyStopped} sessionId={workspace.session.session_id}
          documentMonitoring={{status: documentCameraStatus==='active'?'observing':documentCameraStatus==='paused'?'paused':'unavailable',pendingCount:mistakePendingCount}}
          onIntakeRequest={kind=>{void openMistakeIntake(kind).catch((error:unknown)=>{if(mistakeStillCurrent())setMistakeIntakeNotice(error instanceof Error?error.message:'这次没能打开错题录入。')})}}
          onOriginalReviewRequest={reviewOriginalMistake}
          topicVoiceRequest={topicVoiceRequest}
          onTopicVoiceHandled={id=>setTopicVoiceRequest(request=>request?.id===id?null:request)}
          onTopicSpeech={speakMistakeWorkspaceReply}
          onCoachSpeech={speakMistakeWorkspaceReply}
          onClose={()=>{void localMistakeWorkspace('learning')}}
          onReady={status=>{
            const receipt:MistakeWorkspaceReceipt={workspace_opened:true,workspace:'mistake_book',child_id:workspace.session.child_id,session_id:workspace.session.session_id,content_status:status}
            mistakeWorkspaceReady.current=receipt
            const pending=mistakeWorkspaceWaiter.current;mistakeWorkspaceWaiter.current=null;if(pending){clearTimeout(pending.timer);pending.resolve(receipt)}
          }} onUnavailable={message=>{const pending=mistakeWorkspaceWaiter.current;mistakeWorkspaceWaiter.current=null;if(pending){clearTimeout(pending.timer);pending.reject(Error(message))}}}/>
      </div>}
      <section className="session-content" hidden={mistakeWorkspaceOpen}>
        {workspace.session.state === 'check_in' && (
          <CheckInStep
            workspace={workspace}
            busy={busy}
            run={run}
          />
        )}
        {workspace.session.state === 'classroom_recall' && (
          <RecallStep workspace={workspace} busy={busy} run={run} />
        )}
        {workspace.session.state === 'homework_capture' && (
          <HomeworkStep workspace={workspace} busy={busy} run={run} />
        )}
        {workspace.session.state === 'plan_review' && (
          <PlanStep workspace={workspace} busy={busy} run={run} />
        )}
        {workspace.session.state === 'study' && (
          <StudyStep workspace={workspace} childGrade={childGrade} busy={busy} run={run} />
        )}
        {workspace.session.state === 'break' && (
          <BreakStep workspace={workspace} busy={busy} run={run} />
        )}
        {workspace.session.state === 'reflection' && (
          <ReflectionStep workspace={workspace} busy={busy} run={run} />
        )}
        {workspace.session.state === 'completed' && (
          <div className="step-panel completion-panel">
            <div className="completion-mark"><Check size={34} /></div>
            <span className="section-kicker">今天完成</span>
            <h2>辛苦了，记录已经保存。</h2>
            <p>{workspace.daily_report_ready ? '家长日报已经在本机生成并保存。' : '家长日报正在本地生成。'}</p>
            <SensorReleaseStatus summary={workspace.sensor_release} />
            <p role="status">即将自动回到家庭待机，之后仍可直接用唤醒词叫我。</p>
            <button className="primary-button" onClick={() => onBack(false)}>立即回到首页</button>
          </div>
        )}
      </section>
      <section className="device-live-workspace" aria-label="高拍仪工作区">
        <header>
          <h2>高拍仪</h2>
          <p>查看纸面，识别题目。</p>
        </header>
        <div className="device-live-grid">
          <ContinuousDocumentMonitor
            sessionId={workspace.session.session_id}
            childId={workspace.session.child_id}
            active={Boolean(!privacyStopped && workspace.session.status === 'active' && workspace.session.state !== 'completed')}
            analysisActive={['homework_capture', 'plan_review', 'study'].includes(workspace.session.state)}
            allowDiagnosticFrameCapture={workspace.session.state === 'study'}
            onStatus={setDocumentCameraStatus}
            reviewCapture={mistakeReviewCapture}
            onSavedRevision={item=>{
              if(!mistakeStillCurrent(false)||item.child_id!==mistakeChildId||item.session_id!==mistakeSessionId)return
              if(item.auto_saved_mistakes?.length) {
                waitingMistakeIntake.current=false
                requestMistakeLearningRefresh({child_id:mistakeChildId,session_id:mistakeSessionId})
                setMistakeIntakeNotice(`高拍仪已自动保存 ${item.auto_saved_mistakes.length} 道错题。`)
              }
              if(item.queued_mistake_candidate_count) {
                void localApi.mistakeCaptureQueues(mistakeChildId).then(rows=>{
                  if(!mistakeStillCurrent(false))return
                  setMistakePendingCount(rows.reduce((sum,row)=>sum+row.pending_count,0))
                  const matched=rows.find(row=>row.artifact_id===item.artifact_id)
                  if(matched&&waitingMistakeIntake.current&&mistakeStillCurrent()){
                    waitingMistakeIntake.current=false
                    setMistakeIntake(matched);setMistakeIntakeNotice('这道题需要核对题干和作答，再由小知判断。')
                  }
                }).catch(()=>undefined)
              }
            }}
            onReviewRevision={(item) => {
              const marked = Number.isInteger(item.marked_question_count) && (item.marked_question_count ?? 0) > 0
              window.dispatchEvent(new CustomEvent(DOCUMENT_REVIEW_REVISION_EVENT, {
                detail: { sessionId: workspace.session.session_id, revision: item },
              }))
              return marked ? 'mistake' : 'tutor'
            }}
          />
        </div>
      </section>
    </main>
  )
}

export type VoiceProfileSwitchReceipt = { profile_switched: true; target_child_id: string; target_session_id: string; previous_session_id: string }
export type VoiceProfileSwitch = (targetChildId: string, beforeActivate: (receipt: VoiceProfileSwitchReceipt) => Promise<void>, clientActionId?: string) => Promise<void>

type PreparedAgentTurn={turn:AgentTurn;current:()=>boolean;native?:{dispatch:QwenNativeClientDispatch;port:NativeClientExecutionPort;record:(outcome:NativeClientOutcome)=>void}}
const nativeAnswerCapabilities=new Set(['learning.evaluate_response','diagnostic.submit','exam.submit','pet_vocabulary.submit','mistake.submit_response','mistake.dialogue'])
function AgentCommandPanel({ workspace, childName, childGrade, run, refreshActivityFocus, initialVoiceCommand, initialHouseholdEntry, learningSurfaceVisible=true, initialWakeOnly, onInitialVoiceCommandConsumed, onOpenMistakeBook, onMistakeWorkspaceVoice, onSwitchChild, onReflectionSaved }: { workspace: SessionWorkspaceState; childName: string; childGrade: string; run: (action: () => Promise<unknown>) => Promise<boolean>; refreshActivityFocus: (isCurrent?: () => boolean) => Promise<void>; initialVoiceCommand?: string; initialHouseholdEntry?:import('./types').HouseholdNativeEntry; learningSurfaceVisible?:boolean; initialWakeOnly?: boolean; onInitialVoiceCommandConsumed?: () => void; onOpenMistakeBook?: (destination?:'learning'|'mistake_book') => Promise<unknown>; onMistakeWorkspaceVoice?: (command:ReturnType<typeof mistakeWorkspaceVoiceCommand>,input:MistakeVoiceInput)=>boolean; onSwitchChild?: VoiceProfileSwitch; onReflectionSaved?: (saved: SessionWorkspaceState, acknowledge: () => Promise<void>) => Promise<void> }): React.JSX.Element {
  const privacyStopped = useLocalPrivacyStop(workspace.session.session_id)
  const [privacySaveStatus, setPrivacySaveStatus] = useState<string | null>(null)
  const [captureSaveError, setCaptureSaveError] = useState<string | null>(null)
  const [controlledActivityError, setControlledActivityError] = useState<string | null>(null)
  const [evaluationStatus, setEvaluationStatus] = useState<string | null>(null)
  const [examListeningView, setExamListeningView] = useState<ExamListeningView | null>(null)
  const [evaluationRecovery] = useState(() => {
    try { return { receipt: readPendingEvaluation(workspace.session.session_id, workspace.session.child_id), invalid: false } }
    catch { return { receipt: null, invalid: true } }
  })
  const [pendingEvaluation, setPendingEvaluation] = useState(evaluationRecovery.receipt)
  const pendingEvaluationRef = useRef(pendingEvaluation)
  useLayoutEffect(() => {
    setVoiceLocalRecoveryCapture(workspace.session.session_id, workspace.session.child_id, evaluationRecovery.invalid)
    return () => setVoiceLocalRecoveryCapture(workspace.session.session_id, workspace.session.child_id, false)
  }, [workspace.session.session_id, workspace.session.child_id, evaluationRecovery.invalid])
  useLayoutEffect(() => {
    setVoicePendingEvaluationCapture(workspace.session.session_id, workspace.session.child_id, pendingEvaluation?.turnId ?? null)
    return () => setVoicePendingEvaluationCapture(workspace.session.session_id, workspace.session.child_id, null)
  }, [workspace.session.session_id, workspace.session.child_id, pendingEvaluation?.turnId])
  const [evaluationCheckSignal, setEvaluationCheckSignal] = useState(0)
  const [captureBridge] = useState(() => createCaptureReceiptBridge(workspace.session.session_id, setCaptureSaveError))
  useEffect(() => captureBridge.start(), [captureBridge])
  const commandGenerationRef = useRef(0)
  const ordinarySessionRef=useRef(workspace.session)
  ordinarySessionRef.current=workspace.session
  const continuationDriver = useRef(new AgentContinuationDriver()).current
  const continuationHandledRef = useRef(new Set<string>())
  const conversationActiveRef = useRef(false)
  const learningSurfaceVisibleRef=useRef(learningSurfaceVisible)
  learningSurfaceVisibleRef.current=learningSurfaceVisible
  const nativePreparedQueue=useRef(new NativePreparedQueue())
  const preparedTurnRef = useRef<(turn: AgentTurn, current: () => boolean) => Promise<void>>(async () => undefined)
  useEffect(() => () => continuationDriver.interrupt(), [continuationDriver])
  const voiceEventController = useRef(new AbortController())
  const submitLatest = useRef<(text: string, signal?: AbortSignal, overlap?: PlaybackOverlap, voiceInputId?: string, pendingScope?: PendingEvaluationScope | null, localRecoveryOnly?: boolean, carryoverArrival?: string | null, prepared?: PreparedAgentTurn, handoff?: OnboardingHandoffDelivery) => Promise<void>>(async () => undefined)
  const [voiceInputQueue] = useState(() => new VoiceInputQueue())
  const unprocessedVoiceInputs = useRef<string[]>([])
  const restoredDraft = workspace.drafts.agent_command?.payload ?? {}
  const [text, setText] = useState(draftString(restoredDraft, 'text'))
  const nativeDialogueActiveRef = useRef(false)
  const [tutorRecovery, setTutorRecovery] = useState<TutorToolRecovery | null>(() => parseTutorToolRecovery(restoredDraft.tutor_recovery))
  const tutorRecoveryRef = useRef(tutorRecovery)
  tutorRecoveryRef.current = tutorRecovery
  const currentTextRef = useRef(text)
  currentTextRef.current = text
  const [completionRestore] = useState(() => {
    try { return { pending: readTutorCompletions(workspace.session.session_id, workspace.session.child_id)[0] ?? null, invalid: false } }
    catch { return { pending: null, invalid: true } }
  })
  const [pendingTutorCompletion, setPendingTutorCompletion] = useState<TutorCompletion | null>(completionRestore.pending)
  const pendingTutorCompletionRef = useRef(pendingTutorCompletion)
  pendingTutorCompletionRef.current = pendingTutorCompletion
  const [tutorSyncStatus, setTutorSyncStatus] = useState('')
  const [replyStore] = useState(() => createAgentReplyStore(evaluationRecovery.invalid ? LOCAL_RECOVERY_GUIDANCE
    : restoredAgentRequestMessage(workspace.session.session_id)
      ?? restoredRealtimeAdmissionMessage(workspace.session.session_id, workspace.session.child_id)
      ?? (workspace.session.state === 'break' ? '先休息一会儿，我在这儿。' : '我在这儿。')))
  const setReply = replyStore.set
  const [busy, setBusy] = useState(false)
  const [voiceDelivery, setVoiceDelivery] = useState<'natural' | 'system_female_offline' | 'system_offline' | 'text_only' | 'interrupted' | 'unconfirmed' | 'pending' | 'idle'>('idle')
  const [boundAudioDeviceId, setBoundAudioDeviceId] = useState<string | undefined>()
  const [microphoneBindingReady, setMicrophoneBindingReady] = useState(false)
  const [queuedUtteranceCount, setQueuedUtteranceCount] = useState(0)
  const { status: realtimeVoiceStatus, current: realtimeVoiceRef, update: updateRealtimeVoiceStatus } = useRealtimeVoiceStatus()
  const [qwenRealtimeSettings, setQwenRealtimeSettings] = useState<DesktopQwenRealtimeSummary | null>(null)
  const [qwenRealtimeSettingsLoaded, setQwenRealtimeSettingsLoaded] = useState(false)
  const voiceRuntimePolicy = useVoiceRuntimePolicy(workspace.session.child_id, workspace.session.session_id, true)
  const [qwenRealtimeFailed, setQwenRealtimeFailed] = useState(false)
  const realtimeSpeechCaptureRef = useRef<RealtimeSpeechCapture | null>(null)
  const realtimeTurnTailRef = useRef<Promise<void>>(Promise.resolve())
  const realtimeAdmissionOwnersRef = useRef(new Map<string, string>())
  const realtimeAdmissionsRef = useRef(new Map<string, RealtimeAdmissionAttempt>())
  const terminalAdmissionFlights = useRef(new Map<string, Promise<boolean>>())
  const receiveRuntimeStatus = useCallback((status: RealtimeVoiceStatus) => {
    updateRealtimeVoiceStatus(status)
    // A transient transport error may reconnect while the child is still in
    // the same conversation. Only the explicit inactivity transition ends it.
    if (status.phase === 'wake_standby') conversationActiveRef.current = false
    if (status.phase === 'listening' || status.phase === 'wake_standby') {
      setWakeSettingsMessage(current => current?.startsWith('实时语音连接中断；')
        || current?.startsWith('云语音服务拒绝') || current?.startsWith('云语音账户余额不足') ? null : current)
    }
    continuationDriver.flush()
  }, [continuationDriver, realtimeVoiceRef, updateRealtimeVoiceStatus])
  const [wakeSettings, setWakeSettings] = useState<DesktopWakeWordSettings | null>(null)
  const [wakeSettingsLoaded, setWakeSettingsLoaded] = useState(false)
  const [wakeSettingsReadError, setWakeSettingsReadError] = useState(false)
  const [wakeSettingsReadAttempt, setWakeSettingsReadAttempt] = useState(0)
  const [editingWakeWord, updateEditingWakeWord] = useState(false)
  const [voiceWakeNameSetup, setVoiceWakeNameSetup] = useState(false)
  const editingWakeWordRef = useRef(false)
  const setEditingWakeWord = (editing: boolean): void => {
    editingWakeWordRef.current = editing
    updateEditingWakeWord(editing)
    if (!editing) setVoiceWakeNameSetup(false)
  }
  const [wakeWordDraft, setWakeWordDraft] = useState('')
  const [wakeInactivitySeconds, setWakeInactivitySeconds] = useState(60)
  const [wakeSettingsMessage, setWakeSettingsMessage] = useState<string | null>(null)
  const pendingNameRef = useRef<string | null>(null)
  const savedWakeSettingsRef = useRef<DesktopWakeWordSettings | null>(null)
  const [voiceActivationSignal, setVoiceActivationSignal] = useState(0)
  useEffect(()=>{
    const activate=(event:Event):void=>{
      const d=(event as CustomEvent<{sessionId?:string;childId?:string}>).detail
      if(d?.sessionId!==workspace.session.session_id||d.childId!==workspace.session.child_id||privacyStopPending(d.sessionId))return
      conversationActiveRef.current=true;setVoiceActivationSignal(value=>value+1)
    }
    window.addEventListener(MISTAKE_WORKSPACE_LISTEN,activate)
    return()=>window.removeEventListener(MISTAKE_WORKSPACE_LISTEN,activate)
  },[workspace.session.session_id,workspace.session.child_id])

  useEffect(() => {
    let current = true
    setQwenRealtimeFailed(false)
    setQwenRealtimeSettings(null)
    setQwenRealtimeSettingsLoaded(false)
    const readSettings = window.desktop?.getQwenRealtimeSettings
    if (!readSettings) {
      setQwenRealtimeSettingsLoaded(true)
      return () => { current = false }
    }
    void readSettings()
      .then((settings) => { if (current) setQwenRealtimeSettings(settings) })
      .catch((cause) => {
        if (!current) return
        setQwenRealtimeSettings(null)
        void window.desktop?.recordRendererDiagnostic?.({
          event: 'voice.realtime.settings_read_failed', level: 'error',
          message: cause instanceof Error ? cause.message : '实时语音配置无法读取。',
        })
      })
      .finally(() => { if (current) setQwenRealtimeSettingsLoaded(true) })
    return () => { current = false }
  }, [workspace.session.child_id, workspace.session.session_id])
  const onboardingDesktop = window.desktop as (NonNullable<typeof window.desktop> & Partial<VoiceOnboardingBridge>) | undefined
  const onboardingSupported = Boolean(onboardingDesktop?.getVoiceOnboarding && onboardingDesktop?.saveVoiceOnboarding)
  const onboardingRef = useRef<VoiceOnboardingRuntime | null>(null)
  const [onboarding, setOnboarding] = useState<VoiceOnboardingSnapshot | null>(onboardingSupported ? { phase: 'loading', step: null, prompt: '', silent: true } : null)
  const responsePrompt = useRef(onboarding)
  responsePrompt.current = onboarding
  const responseIdentity = useRef({ childId: workspace.session.child_id, sessionId: workspace.session.session_id })
  responseIdentity.current = { childId: workspace.session.child_id, sessionId: workspace.session.session_id }
  const [readyTutorialPrompt, setReadyTutorialPrompt] = useState<VoiceOnboardingSnapshot | null>(null)
  const [finishedTutorialPrompt, setFinishedTutorialPrompt] = useState<VoiceOnboardingSnapshot | null>(null)
  const tutorialSpeechGeneration = useRef(0)
  const savingWakeWord = useRef(false)
  const tutorialRecoveryRef = useRef<VoiceInteractionRecovery | null>(null)
  const tutorialRecoveryEpoch = useRef(0)
  const tutorialRecoveryRead = useRef<{ key: string; epoch: number; promise: Promise<VoiceInteractionRecovery> } | null>(null)
  const voiceHandoffRef = useRef<VoiceInteractionHandoff | null>(null)
  const voiceHandoffEpoch = useRef(0)
  const voiceHandoffReadUnconfirmed = useRef(true)
  const tutorialActive = onboardingSupported && onboarding?.phase !== 'idle'
  const wakePractice = tutorialActive && onboarding?.phase === 'active' && onboarding.step === 'wake_practice'
  const wakePracticeArmed = wakePractice && readyTutorialPrompt === onboarding
  const tutorialOwnsSpeech = tutorialActive || Boolean(onboarding?.phase === 'idle' && !onboarding.silent && finishedTutorialPrompt !== onboarding)
  const [tutorialCaptureHeld, setTutorialCaptureHeld] = useState(onboardingSupported)
  useEffect(() => {
    if (tutorialActive) setTutorialCaptureHeld(true)
    // Changing the wake configuration restarts the recorder. Drain already
    // captured speech and routed commands before returning to wake standby.
    else if (!voiceInputQueue.running && realtimeVoiceStatus.pendingUtterances === 0) setTutorialCaptureHeld(false)
  }, [tutorialActive, busy, realtimeVoiceStatus.pendingUtterances, voiceInputQueue])
  useLayoutEffect(() => {
    if (!tutorialOwnsSpeech) return
    const owner = Symbol('voice-tutorial')
    tutorialSpeechOwners.set(workspace.session.session_id, owner)
    return () => {
      if (tutorialSpeechOwners.get(workspace.session.session_id) !== owner) return
      tutorialSpeechOwners.delete(workspace.session.session_id)
      queueMicrotask(() => window.dispatchEvent(new CustomEvent('hsc-tutorial-speech-released', { detail: workspace.session.session_id })))
    }
  }, [tutorialOwnsSpeech, workspace.session.session_id])
  const [agentRuntime, setAgentRuntime] = useState<AgentRuntimeStatus | null>(null)
  const [globalTutorTurn, setGlobalTutorTurn] = useState<TutorTurn | null>(null)
  const [globalTutorSegments, setGlobalTutorSegments] = useState(0)
  const [localTutorPlayback, setLocalTutorPlayback] = useState(false)
  const [globalTutorBoard, setGlobalTutorBoard] = useState<TutorBlackboardPresentation | null>(null)
  const [globalTutorBoardTurnId, setGlobalTutorBoardTurnId] = useState<string | null>(null)
  const [globalTutorVisualTurnId, setGlobalTutorVisualTurnId] = useState<string | null>(null)
  const [globalCaptureVisible, setGlobalCaptureVisible] = useState(false)
  const [globalCaptureGeneration, setGlobalCaptureGeneration] = useState(0)
  const [globalCaptureCommandId, setGlobalCaptureCommandId] = useState<string>()
  const [globalCaptureReview, setGlobalCaptureReview] = useState<OcrTutorReview | null>(null)
  const currentGlobalOcrRef=useRef(globalCaptureReview)
  currentGlobalOcrRef.current=globalCaptureReview
  const [restoredDocumentGoal, setRestoredDocumentGoal] = useState<string | null>(null)
  const restoringDocumentGoalRef = useRef(false)
  const globalTutorPlayingRef = useRef(false)
  const [globalCaptureMistakeArtifact, setGlobalCaptureMistakeArtifact] = useState<OcrArtifact | null>(null)
  const [globalCaptureForMistake, setGlobalCaptureForMistake] = useState(false)
  const [globalCaptureObservationOnly, setGlobalCaptureObservationOnly] = useState(false)
  const [documentCaptureActive, setDocumentCaptureActive] = useState(false)
  const documentCaptureActiveRef = useRef(false)
  const captureForMistakeRef = useRef(false)
  const globalTutorTimersRef = useRef<number[]>([])
  const globalTutorGenerationRef = useRef(0)
  const busyRef = useRef(false)
  const pendingUtterancesRef = useRef<Array<{ text: string; overlap?: PlaybackOverlap; voiceInputId?: string; pendingScope: PendingEvaluationScope | null; localRecoveryOnly: boolean; carryoverArrival: string | null; handoff?: OnboardingHandoffDelivery }>>([])
  const mountedRef = useRef(true)
  const speechSuppressedRef = useRef(privacyStopped)
  const lastSpeechRequestedAtRef = useRef(0)
  const preserveInputRef = useRef(false)
  const initialNativeEntry=useRef(initialHouseholdEntry)
  const initialVoiceCommandHandled = useRef(false)
  const controlledContentRef = useRef<ControlledContentPlayback | null>(null)
  const controlledContentEndInFlightRef = useRef(new Set<string>())
  const controlledGameRef = useRef<ControlledGameSession | null>(null)
  const controlledGameEndInFlightRef = useRef(new Set<string>())
  const petVocabularyPromptRequestedRef = useRef<string | null>(null)
  const receiveBoundAudioDeviceId = useCallback((deviceId: string | undefined): void => {
    setBoundAudioDeviceId(deviceId)
  }, [workspace.session.session_id])

  useEffect(() => {
    let live = true
    const unsubscribe = window.desktop?.onControlledGameClosed?.((reason, gameSessionId) => {
      const game = controlledGameRef.current
      if (!game || game.game_session_id !== gameSessionId) return
      if (controlledGameEndInFlightRef.current.has(game.game_session_id)) return
      controlledGameEndInFlightRef.current.add(game.game_session_id)
      const actualSeconds = Math.max(0, Math.floor((Date.now() - Date.parse(game.started_at)) / 1000))
      void settleControlledActivity('game', game, reason || 'process_exited', actualSeconds, true)
        .then(() => {
          if (!live || controlledGameRef.current !== game) return
          controlledGameRef.current = null
          if (mountedRef.current) {
            const message = reason === 'time_limit'
              ? '约定的游戏时间到了，游戏已经关闭。感觉怎么样，准备好继续刚才的学习了吗？'
              : reason === 'process_error'
                ? '游戏意外结束了，刚才的学习进度还在。准备好继续时告诉我。'
                : '游戏已经结束。感觉怎么样，准备好继续刚才的学习了吗？'
            setReply(message)
            if (!speechSuppressedRef.current) speakReply(message,undefined,{localOnly:true,ordinaryResponseAllowed:true})
          }
        })
        .catch(() => { if (live && controlledGameRef.current === game) setControlledActivityError('游戏已退出，但结束记录尚未保存。请再说一次停止游戏来核对原记录。') })
        .finally(() => { controlledGameEndInFlightRef.current.delete(game.game_session_id) })
    })
    let restoringGame: ControlledGameSession | null = null
    void recoverControlledEnds('game', workspace.session.session_id, workspace.session.child_id, () => live, (message, id, recovered) => {
      if (controlledGameRef.current && controlledGameRef.current.game_session_id !== id) return
      if (recovered) { if (!busyRef.current) setReply(message) }
      else setControlledActivityError(message)
    })
      .then(async recovering => recovering ? {game: null} : localApi.activeControlledGame(workspace.session.session_id)).then(async ({ game }) => {
      if (!game || !live || controlledGameRef.current) return
      if (game.session_id !== workspace.session.session_id || game.child_id !== workspace.session.child_id || game.status !== 'active') {
        throw new VoiceActionUnavailableError('游戏恢复记录不属于当前孩子的进行中活动。')
      }
      restoringGame = game
      controlledGameRef.current = game
      controlledGameEndInFlightRef.current.add(game.game_session_id)
      try {
        await settleControlledActivity('game', game, 'renderer_reloaded', 0)
        if (controlledGameRef.current === game) controlledGameRef.current = null
      } finally { controlledGameEndInFlightRef.current.delete(game.game_session_id) }
    }).catch(() => {
      if (live && (restoringGame ? controlledGameRef.current === restoringGame : !controlledGameRef.current)) {
        setControlledActivityError('游戏恢复状态尚未确认，记录仍然保留。可以说停止游戏核对原记录。')
      }
    })
    return () => {
      live = false
      unsubscribe?.()
      if (controlledGameRef.current && !controlledGameEndInFlightRef.current.has(controlledGameRef.current.game_session_id)) {
        const game = controlledGameRef.current
        controlledGameEndInFlightRef.current.add(game.game_session_id)
        void settleControlledActivity('game', game, 'workspace_closed', 0)
          .catch(() => undefined)
          .finally(() => { controlledGameEndInFlightRef.current.delete(game.game_session_id) })
        controlledGameRef.current = null
      }
    }
  }, [workspace.session.session_id, workspace.session.child_id])

  const refreshAgentRuntime = useCallback(async (): Promise<void> => {
    const { current, status } = await trackVoiceLearningCapture(
      workspace.session.session_id, workspace.session.child_id,
      () => localApi.agentRuntimeStatus(workspace.session.session_id), () => mountedRef.current)
    if (current && mountedRef.current) setAgentRuntime(status)
  }, [])

  useEffect(() => {
    void refreshAgentRuntime()
  }, [refreshAgentRuntime])

  useEffect(() => {
    const { session_id: sessionId, child_id: childId, state } = workspace.session
    if (state === 'study') void trackTaskChoiceCapture(sessionId, childId, () => localApi.taskStartChoice(sessionId)).catch(() => undefined)
    return () => setVoiceTaskChoiceCapture(sessionId, childId, null)
  }, [workspace.session.session_id, workspace.session.child_id, workspace.session.state])

  useEffect(() => {
    let live = true
    const unsubscribe = window.desktop?.onControlledMediaClosed?.((reason, contentSessionId) => {
      const playback = controlledContentRef.current
      if (!playback || playback.content_session_id !== contentSessionId) return
      if (controlledContentEndInFlightRef.current.has(playback.content_session_id)) return
      controlledContentEndInFlightRef.current.add(playback.content_session_id)
      const actualSeconds = Math.max(0, Math.floor((Date.now() - Date.parse(playback.started_at)) / 1000))
      void settleControlledActivity('video', playback, reason || 'window_closed', actualSeconds, true)
        .then(() => {
          if (!live || controlledContentRef.current !== playback) return
          controlledContentRef.current = null
          if (mountedRef.current) {
            const message = reason === 'time_limit'
              ? '约定的观看时间到了。想说说刚才的内容，还是准备继续学习？'
              : reason === 'media_ended'
                ? '这段内容播放完了。有什么想聊的，还是准备继续学习？'
                : '视频已经关闭。准备好继续刚才的学习时告诉我。'
            setReply(message)
            if (!speechSuppressedRef.current) speakReply(message,undefined,{localOnly:true,ordinaryResponseAllowed:true})
          }
        })
        .catch(() => { if (live && controlledContentRef.current === playback) setControlledActivityError('视频已关闭，但结束记录尚未保存。请再说一次停止视频来核对原记录。') })
        .finally(() => { controlledContentEndInFlightRef.current.delete(playback.content_session_id) })
    })
    let restoringPlayback: ControlledContentPlayback | null = null
    void recoverControlledEnds('video', workspace.session.session_id, workspace.session.child_id, () => live, (message, id, recovered) => {
      if (controlledContentRef.current && controlledContentRef.current.content_session_id !== id) return
      if (recovered) { if (!busyRef.current) setReply(message) }
      else setControlledActivityError(message)
    })
      .then(async recovering => recovering ? {playback: null} : localApi.activeControlledContent(workspace.session.session_id)).then(async ({ playback }) => {
      if (!playback || !live || controlledContentRef.current) return
      if (playback.session_id !== workspace.session.session_id || playback.child_id !== workspace.session.child_id || playback.status !== 'active') {
        throw new VoiceActionUnavailableError('视频恢复记录不属于当前孩子的进行中活动。')
      }
      restoringPlayback = playback
      controlledContentRef.current = playback
      controlledContentEndInFlightRef.current.add(playback.content_session_id)
      try {
        await settleControlledActivity('video', playback, 'renderer_reloaded', 0)
        if (controlledContentRef.current === playback) controlledContentRef.current = null
      } finally { controlledContentEndInFlightRef.current.delete(playback.content_session_id) }
    }).catch(() => {
      if (live && (restoringPlayback ? controlledContentRef.current === restoringPlayback : !controlledContentRef.current)) {
        setControlledActivityError('视频恢复状态尚未确认，记录仍然保留。可以说停止视频核对原记录。')
      }
    })
    return () => {
      live = false
      unsubscribe?.()
      if (controlledContentRef.current && !controlledContentEndInFlightRef.current.has(controlledContentRef.current.content_session_id)) {
        const playback = controlledContentRef.current
        controlledContentEndInFlightRef.current.add(playback.content_session_id)
        void settleControlledActivity('video', playback, 'workspace_closed', 0)
          .catch(() => undefined)
          .finally(() => { controlledContentEndInFlightRef.current.delete(playback.content_session_id) })
        controlledContentRef.current = null
      }
    }
  }, [workspace.session.session_id, workspace.session.child_id])
  const draft = useAutosavedDraft(
    workspace.session.session_id,
    'agent_command',
    { text, tutor_recovery: tutorRecovery },
    Boolean(text.trim() || tutorRecovery) && !tutorialActive && !voiceHandoffRef.current && !voiceHandoffReadUnconfirmed.current,
    Boolean(workspace.drafts.agent_command)
  )

  const speakReply = (message: string, onFinished?: () => void, speechPolicy?: AgentReplySpeechPolicy, tutorialPrompt = false): void => {
    if (speechSuppressedRef.current || !mountedRef.current || (!learningSurfaceVisibleRef.current&&speechPolicy?.ordinaryResponseAllowed)) { speechPolicy?.onPlaybackAbandoned?.(); return }
    if (tutorialPrompt && onboardingRef.current && onboardingRef.current.snapshot.phase !== 'idle') {
      setOnboarding({ ...onboardingRef.current.snapshot, prompt: message, silent: false })
      return
    }
    if (!tutorialPrompt && !speechPolicy?.skipPublicReplyProvider && qwenRealtimeActive && !speechPolicy?.sourceTutorTurnId && speechPolicy?.sourceAgentTurnId && window.desktop?.openPublicReplySpeech) {
      const generation = commandGenerationRef.current
      const current = () => mountedRef.current && !speechSuppressedRef.current && generation === commandGenerationRef.current
        && (speechPolicy.isCurrent?.() ?? true)
      setVoiceDelivery('pending')
      const ordinary=speechPolicy.ordinaryResponseAllowed&&ordinaryResponseMayOwnPrompt(workspace.session.session_id,workspace.session.child_id,speechPolicy)?ordinaryPromptPlayback({sessionId:workspace.session.session_id,
        childId:workspace.session.child_id,state:ordinarySessionRef.current.state,prompt:{},text:message,
        sourceAgentTurnId:speechPolicy.sourceAgentTurnId,deliveryPending:true,isCurrent:current},speechPolicy):null
      void playPublicReply({ sessionId: workspace.session.session_id, childId: workspace.session.child_id,
        agentTurnId: speechPolicy.sourceAgentTurnId }, message, { ...speechPolicy, isCurrent:()=>current()&&(!ordinary||(ordinary.options.isCurrent?.()??false)),
        onPlaybackEnd: () => { if (current()) { ordinary?.grant.confirmDelivery();speechPolicy.onPlaybackEnd?.(); onFinished?.() } },
        onNotDelivered:reason=>{ordinary?.grant.revoke();speechPolicy.onNotDelivered?.(reason)},
      }, result => { if (current()) setVoiceDelivery(result === 'managed' ? 'natural' : result) }, ()=>{ordinary?.started();speechPolicy.onPlaybackStart?.()},()=>ordinary?.grant.naturalEnd())
        .then(result => {
          if (result === 'unavailable' && current()) {
            ordinary?.grant.revoke()
            // Definitively ineligible local material stays on the existing
            // offline boundary. An uncertain reservation never takes this path.
            speakReply(message, onFinished, { ...speechPolicy, skipPublicReplyProvider: true }, tutorialPrompt)
          }
        }).catch(() => {ordinary?.grant.revoke();if (current()) setVoiceDelivery('unconfirmed') })
      if (realtimeSpeechCaptureRef.current) realtimeSpeechCaptureRef.current.localPlaybackUsed = true
      return
    }
    const realtimeCapture = realtimeSpeechCaptureRef.current
    if (realtimeCapture && !tutorialPrompt) {
      if (speechPolicy?.localOnly) realtimeCapture.localPlaybackUsed = true
      else {
        realtimeCapture.messages.push({ text: message, onFinished, policy: speechPolicy })
        lastSpeechRequestedAtRef.current = Date.now()
        setVoiceDelivery('pending')
        return
      }
    }
    const generation = commandGenerationRef.current
    lastSpeechRequestedAtRef.current = Date.now()
    setVoiceActivationSignal((current) => current + 1)
    setVoiceDelivery('pending')
    const play = tutorialPrompt ? playLocalSpeech : speakLocally
    const responseScope={sessionId:workspace.session.session_id,childId:workspace.session.child_id,state:ordinarySessionRef.current.state,
      prompt:{},text:message,sourceAgentTurnId:speechPolicy?.sourceAgentTurnId,sourceTutorTurnId:speechPolicy?.sourceTutorTurnId,
      isCurrent:()=>mountedRef.current&&!speechSuppressedRef.current&&generation===commandGenerationRef.current
        &&!privacyStopped&&!tutorialOwnsSpeech&&(speechPolicy?.isCurrent?.()??true)}
    const ordinary=!tutorialPrompt&&speechPolicy?.ordinaryResponseAllowed
      &&ordinaryResponseMayOwnPrompt(workspace.session.session_id,workspace.session.child_id,speechPolicy)
      ?speechPolicy.sourceTutorTurnId?ordinaryTutorPlayback(responseScope,speechPolicy):ordinaryPromptPlayback(responseScope,speechPolicy):null
    let playbackFinished = false
    play(message, workspace.session.session_id, (result) => {
      if (!mountedRef.current || speechSuppressedRef.current || generation !== commandGenerationRef.current) return
      setVoiceDelivery(result === 'managed' ? 'natural' : result)
      // Text fallback can continue an already-authorized tool workflow, but
      // cannot acknowledge audible reminders or blackboard playback.
      if (result === 'text_only' && !playbackFinished) { ordinary?.grant.revoke();playbackFinished = true; onFinished?.() }
    }, ()=>{ordinary?.started();speechPolicy?.onPlaybackStart?.()}, { ...(ordinary?.options??speechPolicy),
      isCurrent: () => mountedRef.current && !speechSuppressedRef.current && generation === commandGenerationRef.current
        && (speechPolicy?.isCurrent?.() ?? true)&&(!ordinary||(ordinary.options.isCurrent?.()??false)),
      onPlaybackEnd: () => {
        if (playbackFinished) return
        if (!mountedRef.current || speechSuppressedRef.current || generation !== commandGenerationRef.current) {
          speechPolicy?.onPlaybackAbandoned?.(); return
        }
        playbackFinished = true
        if(ordinary)ordinary.options.onPlaybackEnd?.();else speechPolicy?.onPlaybackEnd?.()
        onFinished?.()
      },
      onNotDelivered: reason => {
        if (!mountedRef.current || generation !== commandGenerationRef.current || speechSuppressedRef.current) {
          speechPolicy?.onPlaybackAbandoned?.(); return
        }
        setVoiceDelivery(reason === 'unavailable' ? 'text_only' : 'interrupted')
        if(ordinary)ordinary.options.onNotDelivered?.(reason);else speechPolicy?.onNotDelivered?.(reason)
      },
    })
  }

  useDailyReflectionSummary({
    sessionId: workspace.session.session_id, childId: workspace.session.child_id,
    enabled: workspace.session.state === 'reflection' && !privacyStopped && !tutorialOwnsSpeech,
    busy: busy || queuedUtteranceCount > 0 || realtimeVoiceStatus.pendingUtterances > 0,
    onSummary: (message) => {
      if (speechSuppressedRef.current || tutorialSpeechOwners.has(workspace.session.session_id)) return
      setReply(message)
      setVoiceActivationSignal((value) => value + 1)
      speakReply(message, undefined, { localOnly: true,ordinaryResponseAllowed:true })
    },
  })

  useEffect(() => {
    if (!onboardingSupported || !wakeSettingsLoaded || wakeSettingsReadError || privacyStopped) return
    const bridge = window.desktop as unknown as VoiceOnboardingBridge
    const runtime = new VoiceOnboardingRuntime(workspace.session.child_id, wakeSettings?.wakeWord ?? '', bridge, setOnboarding, childGrade)
    onboardingRef.current = runtime
    setOnboarding(runtime.snapshot)
    void runtime.load()
    return () => {
      runtime.dispose()
      if (onboardingRef.current === runtime) onboardingRef.current = null
      tutorialSpeechGeneration.current += 1
    }
  }, [childGrade, onboardingSupported, privacyStopped, wakeSettingsLoaded, wakeSettingsReadError, workspace.session.child_id])

  useEffect(() => {
    if (!onboarding || privacyStopped || speechSuppressedRef.current) return
    const generation = ++tutorialSpeechGeneration.current
    setReadyTutorialPrompt(null)
    if (!onboarding.prompt || onboarding.silent) return
    const message = onboarding.prompt
    setReply(message)
    const speechKey = `onboarding-response:${workspace.session.session_id}:${generation}`
    const responseScope = ['naming', 'active'].includes(onboarding.phase) ? {
      sessionId: workspace.session.session_id, childId: workspace.session.child_id,
      phase: onboarding.phase as 'naming' | 'active', step: onboarding.step,
      prompt: onboarding, promptText: message, epoch: generation,
      isCurrent: () => mountedRef.current && !speechSuppressedRef.current
        && generation === tutorialSpeechGeneration.current && responsePrompt.current === onboarding
        && responseIdentity.current.childId === workspace.session.child_id
        && responseIdentity.current.sessionId === workspace.session.session_id,
    } : null
    let response = responseScope ? beginOnboardingResponse(responseScope) : null
    let delivered = false
    const arm = (): void => {
      if (generation !== tutorialSpeechGeneration.current || !mountedRef.current || speechSuppressedRef.current) return
      if (delivered && ['idle', 'error'].includes(getCompanionVoiceState())) {
        setReadyTutorialPrompt(onboarding)
      }
      if (delivered && onboarding.phase === 'idle' && ['idle', 'error'].includes(getCompanionVoiceState())) setFinishedTutorialPrompt(onboarding)
    }
    const unsubscribe = subscribeCompanionVoice(arm)
    playLocalSpeech(message, workspace.session.session_id, (result) => {
      if (generation !== tutorialSpeechGeneration.current || !mountedRef.current || speechSuppressedRef.current) return
      setVoiceDelivery(result === 'managed' ? 'natural' : result)
      delivered = true
      arm()
    }, () => {
      if (!responseScope?.isCurrent()) return
      // A managed prefix can fail before the same prompt is replayed in full
      // by the local voice. Each actual start owns its own completion proof.
      response?.revoke()
      response = beginOnboardingResponse(responseScope)
      response.started()
    }, {
      ...voiceOnboardingSpeechPolicy(message), speechKey,
      onPlaybackEnd: () => response?.naturalEnd(),
      onNotDelivered: () => response?.revoke(),
    })
    return () => { tutorialSpeechGeneration.current += 1; response?.revoke(); unsubscribe() }
  }, [onboarding, privacyStopped, workspace.session.child_id, workspace.session.session_id])

  const tutorialContext = (): VoiceInteractionContext => {
    const runtime = onboardingRef.current
    const pendingName = pendingNameRef.current
    const wakeWord = savedWakeSettingsRef.current?.wakeWord
    const naming = runtime?.snapshot.phase === 'naming' || (!onboardingSupported && !wakeWord && Boolean(window.desktop?.saveWakeWordSettings)) || editingWakeWordRef.current
    return {
      phase: pendingName ? 'confirm_name' : naming ? 'naming' : 'tutorial',
      ...(runtime?.snapshot.step ? { step: runtime.snapshot.step } : {}),
      ...(pendingName ? { pending_name: pendingName } : {}), ...(wakeWord ? { wake_word: wakeWord } : {}),
    }
  }

  const handoffMessage = (value: VoiceInteractionHandoff): string => {
    if (value.status === 'invalidated') return '原学习要求的交接已取消或已变化，我不会恢复旧要求。请重新说明现在想做什么。'
    if (value.status === 'needs_deferral' || value.status === 'ready') {
      return '刚才的学习要求已保留，还没有交接完成。说“再试一次”，我会接着处理原来的要求。'
    }
    return value.receipt?.response_text || '原学习要求已有交接记录，我不会重复提交。可以说“核对刚才请求”查看原记录。'
  }

  const readVoiceHandoff = async (signal: AbortSignal, current: () => boolean): Promise<VoiceInteractionHandoff | null> => {
    const epoch = ++voiceHandoffEpoch.current
    const value = validateVoiceInteractionHandoff(await resolveVoiceInteraction(requestSignal => localApi.readVoiceInteractionHandoff(
      workspace.session.session_id, workspace.session.child_id, requestSignal), signal), {
      sessionId: workspace.session.session_id, childId: workspace.session.child_id,
    })
    if (!current() || epoch !== voiceHandoffEpoch.current) return null
    voiceHandoffReadUnconfirmed.current = false
    voiceHandoffRef.current = ['none', 'invalidated'].includes(value.status) ? null : value
    return value
  }

  const invalidateVoiceHandoff = async (signal?: AbortSignal): Promise<void> => {
    const epoch = ++voiceHandoffEpoch.current
    await localApi.invalidateVoiceInteractionHandoff(workspace.session.session_id, workspace.session.child_id, signal)
    if (epoch !== voiceHandoffEpoch.current || responseIdentity.current.childId !== workspace.session.child_id
      || responseIdentity.current.sessionId !== workspace.session.session_id) return
    voiceHandoffRef.current = null
    voiceHandoffReadUnconfirmed.current = false
  }

  const discardHandoffTranscript = (transcript: string): void => {
    const original = transcript.trim()
    const escaped = original.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const remaining = currentTextRef.current.replace(new RegExp(`(^|\\n)[ \\t]*${escaped}[ \\t]*(?=\\n|$)`, 'g'), '$1').trim()
    unprocessedVoiceInputs.current = unprocessedVoiceInputs.current.filter(input => input.trim() !== original)
    currentTextRef.current = remaining
    setText(remaining)
  }

  const continueVoiceHandoff = async (initial: VoiceInteractionHandoff, signal: AbortSignal, current: () => boolean): Promise<TutorialConsumption> => {
    let value = initial
    if (value.status === 'needs_deferral' || value.status === 'ready' && onboardingRef.current?.snapshot.phase !== 'idle') {
      const runtime = onboardingRef.current
      if (!runtime || !value.deferred_progress || !await runtime.deferPrepared(value.deferred_progress)) throw new Error('ONBOARDING_DEFER_NOT_SAVED')
      if (!current() || runtime !== onboardingRef.current) return true
      const updated = await readVoiceHandoff(signal, current)
      if (!updated) return true
      value = updated
    }
    if (!current()) return true
    if (value.status !== 'ready' || !value.request || !value.handoff_id || !value.agent_action_id) {
      const message = handoffMessage(value)
      setReply(message); speakReply(message, undefined, { localOnly: true }, true)
      return true
    }
    discardHandoffTranscript(value.request.transcript)
    pendingNameRef.current = null
    setEditingWakeWord(false)
    return { transcript: value.request.transcript, voiceInputId: value.request.voice_input_id,
      handoff: { handoffId: value.handoff_id, agentActionId: value.agent_action_id } }
  }

  useEffect(() => {
    voiceHandoffRef.current = null
    voiceHandoffReadUnconfirmed.current = true
    if (!wakeSettingsLoaded || wakeSettingsReadError || privacyStopped) return
    const controller = new AbortController(), generation = commandGenerationRef.current
    const runtime = onboardingRef.current
    const current = (): boolean => mountedRef.current && !controller.signal.aborted && !speechSuppressedRef.current
      && generation === commandGenerationRef.current && runtime === onboardingRef.current
    void (async () => {
      if (runtime) await runtime.whenSettled()
      if (!current()) return
      const value = await readVoiceHandoff(controller.signal, current)
      if (!current() || !value || ['none', 'invalidated'].includes(value.status)) return
      const message = handoffMessage(value)
      setReply(message); speakReply(message, undefined, { localOnly: true }, true)
    })().catch(() => { /* An unread pointer never authorizes a replay. Explicit retry will read it again. */ })
    return () => { controller.abort(); voiceHandoffEpoch.current += 1 }
  }, [wakeSettingsLoaded, wakeSettingsReadError, privacyStopped, onboardingSupported, Boolean(onboarding),
    workspace.session.session_id, workspace.session.child_id])

  const readTutorialRecovery = async (context: VoiceInteractionContext, signal: AbortSignal, current: () => boolean): Promise<VoiceInteractionRecovery | null> => {
    const key = `${workspace.session.session_id}:${workspace.session.child_id}:${voiceInteractionContextKey(context)}`
    let read = tutorialRecoveryRead.current
    if (!read || read.key !== key || read.epoch !== tutorialRecoveryEpoch.current) {
      const epoch = ++tutorialRecoveryEpoch.current
      const promise = resolveVoiceInteraction(requestSignal => localApi.voiceInteractionRecovery(
        workspace.session.session_id, workspace.session.child_id, context, requestSignal), signal)
        .then(value => validatedVoiceInteractionRecovery(value, workspace.session.session_id, workspace.session.child_id, context))
      read = { key, epoch, promise }
      tutorialRecoveryRead.current = read
      void promise.finally(() => { if (tutorialRecoveryRead.current === read) tutorialRecoveryRead.current = null }).catch(() => undefined)
    }
    const epoch = read.epoch
    const value = await read.promise
    if (!current() || epoch !== tutorialRecoveryEpoch.current || voiceInteractionContextKey(tutorialContext()) !== voiceInteractionContextKey(context)) return null
    tutorialRecoveryRef.current = value.status === 'none' ? null : value
    if (value.request?.context.phase === 'confirm_name' && !pendingNameRef.current) {
      pendingNameRef.current = value.request.context.pending_name ?? null
      setWakeWordDraft(pendingNameRef.current ?? '')
    }
    return value
  }

  useEffect(() => {
    if (!wakeSettingsLoaded || wakeSettingsReadError || privacyStopped || !onboardingSupported
      || !onboarding || !['naming', 'active'].includes(onboarding.phase)) return
    const controller = new AbortController()
    const generation = commandGenerationRef.current
    const runtime = onboardingRef.current
    const current = (): boolean => mountedRef.current && !controller.signal.aborted && !speechSuppressedRef.current
      && generation === commandGenerationRef.current && runtime === onboardingRef.current
    tutorialRecoveryRef.current = null
    void (async () => {
      if (runtime) await runtime.whenSettled()
      if (!current()) return
      const recovered = await readTutorialRecovery(tutorialContext(), controller.signal, current)
      if (!current() || !recovered || recovered.status === 'none' || voiceHandoffRef.current) return
      const message = voiceInteractionRecoveryMessage(recovered.status)
      setReply(message)
      speakReply(message, undefined, { localOnly: true }, true)
    })().catch(() => {
      if (!current()) return
      setReply(voiceInteractionRecoveryMessage('unreadable'))
    })
    return () => { controller.abort(); tutorialRecoveryEpoch.current += 1; tutorialRecoveryRef.current = null }
  }, [wakeSettingsLoaded, wakeSettingsReadError, privacyStopped, onboardingSupported, onboarding?.phase, onboarding?.step,
    workspace.session.session_id, workspace.session.child_id])

  const consumeTutorial = async (value: string, signal: AbortSignal, voiceInputId?: string): Promise<TutorialConsumption> => {
    if (!mountedRef.current || speechSuppressedRef.current) return true
    const generation = commandGenerationRef.current
    const current = (): boolean => mountedRef.current && !speechSuppressedRef.current && !signal.aborted && generation === commandGenerationRef.current
    if (wakeSettingsReadError) {
      if (/^(重试|再试一次)[。！!]?$/u.test(value.trim())) setWakeSettingsReadAttempt((attempt) => attempt + 1)
      else { setText(value); setOnboarding({ phase: 'error', step: null, prompt: '名字设置还没能读到。刚才的话保留在输入框，尚未执行；请先恢复名字设置。', silent: false }) }
      return true
    }
    const runtime = onboardingRef.current
    let originalValue = value
    let originalContext: VoiceInteractionContext | null = null
    let recoveredVoiceInputId = voiceInputId
    let completedInteractionKey: string | null = null
    let preparedHandoff = false
    try {
      if (runtime) await resolveVoiceInteraction(() => runtime.whenSettled(), signal)
      if (!current()) return true
      if (isVoiceInteractionRetry(value)) {
        const handoff = await readVoiceHandoff(signal, current)
        if (!current()) return true
        if (handoff?.status === 'invalidated') {
          const message = handoffMessage(handoff)
          setReply(message); speakReply(message, undefined, { localOnly: true }, true)
          return true
        }
        if (handoff && !['none', 'invalidated'].includes(handoff.status)) {
          preparedHandoff = true
          return await continueVoiceHandoff(handoff, signal, current)
        }
      }
      let pendingName = pendingNameRef.current
      const wakeWord = savedWakeSettingsRef.current?.wakeWord
      const naming = runtime?.snapshot.phase === 'naming' || (!onboardingSupported && !wakeWord && Boolean(window.desktop?.saveWakeWordSettings)) || editingWakeWordRef.current
      if (!pendingName && !naming && (!onboardingSupported || runtime?.snapshot.phase === 'idle')) return false
      if (onboardingSupported && !runtime) throw new Error('ONBOARDING_NOT_READY')
      cancelLocalSpeech()
      // The model decision already succeeded when only the desktop progress
      // write failed. Retry that owned local write, without another model call
      // or a second wake-name save.
      if (isVoiceInteractionRetry(value) && runtime?.snapshot.phase === 'error') {
        if (!await runtime.applyAction('advance')) throw new Error('ONBOARDING_NOT_SAVED')
        return true
      }
      let result
      originalContext = tutorialContext()
      if (isVoiceInteractionRetry(value)) {
        const recovered = await readTutorialRecovery(originalContext, signal, current)
        if (!current() || !recovered) return true
        if (!['retryable', 'completed'].includes(recovered.status) || !recovered.request || !recovered.original_action_key) {
          const message = voiceInteractionRecoveryMessage(recovered.status)
          setReply(message); speakReply(message, undefined, { localOnly: true }, true)
          return true
        }
        originalValue = recovered.request.transcript
        originalContext = recovered.request.context
        recoveredVoiceInputId = recovered.request.voice_input_id
        pendingName = pendingNameRef.current
        const retried = validatedVoiceInteractionRecovery(await resolveVoiceInteraction(requestSignal => localApi.retryVoiceInteraction(
          workspace.session.session_id, workspace.session.child_id, recovered.original_action_key!, crypto.randomUUID(), originalContext!, requestSignal), signal),
          workspace.session.session_id, workspace.session.child_id, originalContext)
        if (!current() || voiceInteractionContextKey(tutorialContext()) !== voiceInteractionContextKey(originalContext)) return true
        tutorialRecoveryRef.current = retried.status === 'none' ? null : retried
        if (retried.status !== 'completed' || !retried.response) {
          const message = voiceInteractionRecoveryMessage(retried.status)
          setReply(message); speakReply(message, undefined, { localOnly: true }, true)
          return true
        }
        result = retried.response
        completedInteractionKey = retried.original_action_key
      } else {
        tutorialRecoveryEpoch.current += 1
        tutorialRecoveryRef.current = null
        const clientActionId = crypto.randomUUID()
        result = await resolveVoiceInteraction((requestSignal) => localApi.voiceInteraction(workspace.session.session_id, {
          transcript: value, client_action_id: clientActionId, context: originalContext!,
          ...(voiceInputId ? { voice_input_id: voiceInputId } : {}),
        }, requestSignal), signal)
        if (result?.route === 'agent' && result.source === 'cloud') {
          const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(clientActionId))
          completedInteractionKey = 'voice-interaction:' + Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
        }
      }
      if (!current()) return true
      if (!result || !['agent', 'onboarding', 'clarify'].includes(result.route) || typeof result.response_text !== 'string') throw new Error('INVALID_VOICE_INTERACTION')
      // Local privacy may mean a scoped device stop OR private clarification.
      // Preserve the original command for AgentRuntime; never infer "stop all".
      const localPrivacy = result.source === 'local_privacy' && result.action === 'none'
        && (result.route === 'agent' || result.route === 'clarify')
      if (result.source !== 'cloud' && !localPrivacy) throw new Error('VOICE_INTERACTION_UNAVAILABLE')
      if (result.route === 'agent') {
        if (!localPrivacy) {
          // Daily wake-name editing can yield directly to learning. Its
          // tutorial is already settled and must not be deferred again.
          if (!onboardingSupported || runtime?.snapshot.phase === 'idle') {
            pendingNameRef.current = null
            setEditingWakeWord(false)
            return originalValue === value ? false : { transcript: originalValue, voiceInputId: recoveredVoiceInputId }
          }
          if (!completedInteractionKey) throw new Error('ONBOARDING_HANDOFF_SOURCE_REQUIRED')
          preparedHandoff = true
          discardHandoffTranscript(originalValue)
          voiceHandoffEpoch.current += 1
          const handoff = validateVoiceInteractionHandoff(await resolveVoiceInteraction(requestSignal => localApi.prepareVoiceInteractionHandoff(
            workspace.session.session_id, workspace.session.child_id, completedInteractionKey!, requestSignal), signal), {
            sessionId: workspace.session.session_id, childId: workspace.session.child_id,
          })
          if (!current()) return true
          voiceHandoffRef.current = handoff
          voiceHandoffReadUnconfirmed.current = false
          return await continueVoiceHandoff(handoff, signal, current)
        }
        if (runtime && !await runtime.applyAction('skip', true)) throw new Error('ONBOARDING_DEFER_NOT_SAVED')
        if (!current()) return true
        pendingNameRef.current = null
        setEditingWakeWord(false)
        return originalValue === value ? false : { transcript: originalValue, voiceInputId: recoveredVoiceInputId }
      }
      if (result.route === 'onboarding') {
        if (result.action === 'name_candidate') {
          const candidate = result.candidate?.trim()
          if (!candidate || candidate.length < 2 || candidate.length > 12 || /[\r\n]/.test(candidate)) throw new Error('INVALID_NAME_CANDIDATE')
          if (pendingName && resolvesKnownNameConfirmation(originalValue, pendingName, candidate)) {
            if (pendingNameRef.current !== pendingName || onboardingRef.current !== runtime) throw new Error('NAME_CONFIRMATION_CONTEXT_CHANGED')
            const needsNamedProgress = runtime?.snapshot.phase === 'naming'
            const savedThisTime = await saveWakeWord(pendingName)
            if (!current()) return true
            if (!savedThisTime) throw new Error('KNOWN_NAME_CONFIRMATION_NOT_SAVED')
            const saved = savedWakeSettingsRef.current
            if (!saved?.enabled || saved.childId !== workspace.session.child_id || saved.wakeWord !== pendingName
              || (needsNamedProgress && (runtime?.snapshot.phase !== 'active' || runtime.snapshot.step !== 'wake_practice'))) throw new Error('KNOWN_NAME_CONFIRMATION_NOT_SAVED')
            return true
          }
          pendingNameRef.current = candidate
          setWakeWordDraft(candidate)
        } else if (result.action === 'confirm_name') {
          if (!pendingName || (result.candidate && result.candidate !== pendingName)) throw new Error('NAME_CONFIRMATION_MISMATCH')
          await saveWakeWord(pendingName)
          return true
        } else if (result.action === 'reject_name') {
          pendingNameRef.current = null
          setWakeWordDraft(savedWakeSettingsRef.current?.wakeWord ?? '')
        } else if (['skip', 'repeat', 'advance'].includes(result.action)) {
          if (!runtime) throw new Error('ONBOARDING_NOT_READY')
          if (!await runtime.applyAction(result.action as 'skip' | 'repeat' | 'advance')) throw new Error('ONBOARDING_NOT_SAVED')
          if (!current()) return true
          if (result.action === 'skip') { pendingNameRef.current = null; setEditingWakeWord(false) }
          if (!unprocessedVoiceInputs.current.length) setText('')
          return true
        } else if (result.action !== 'none') throw new Error('INVALID_VOICE_ACTION')
      }
      if (current()) {
        if (!unprocessedVoiceInputs.current.length) setText('')
        setReply(result.response_text)
        // No cloud TTS for arbitrary conversation, names, or private memory.
        speakReply(result.response_text, undefined, { localOnly: true }, true)
      }
      return true
    } catch {
      if (!current()) return true
      preserveInputRef.current = true
      if (!preparedHandoff && !voiceHandoffRef.current) {
        if (!unprocessedVoiceInputs.current.includes(originalValue)) unprocessedVoiceInputs.current.push(originalValue)
        setText(unprocessedVoiceInputs.current.join('\n'))
      }
      let message = voiceInteractionRecoveryMessage('unreadable')
      if (voiceHandoffRef.current) message = handoffMessage(voiceHandoffRef.current)
      else if (originalContext) {
        try {
          const recovered = await readTutorialRecovery(originalContext, signal, current)
          if (!current()) return true
          if (recovered) message = voiceInteractionRecoveryMessage(recovered.status)
        } catch { /* Keep the original text locally; never assert an unread durable save. */ }
      }
      setReply(message)
      speakReply(message, undefined, { localOnly: true }, true)
      return true
    }
  }

  const receiveWake = (announce = true): void => {
    if (!mountedRef.current || speechSuppressedRef.current || privacyStopped) return
    conversationActiveRef.current = true
    if (onboardingSupported && onboardingRef.current?.snapshot.phase !== 'idle') {
      if (wakePracticeArmed) void onboardingRef.current?.wakeDetected()
      return
    }
    if (nativeDialogueActiveRef.current) {
      setReply('我在。')
      if (announce) speakReply('我在。', undefined, { localOnly: true })
      return
    }
    const pendingRequestMessage = restoredAgentRequestMessage(workspace.session.session_id)
      ?? restoredRealtimeAdmissionMessage(workspace.session.session_id, workspace.session.child_id)
    const continuationMessage = agentRuntime?.session_id === workspace.session.session_id
      ? agentContinuationRecoveryMessage(agentRuntime, workspace.session.child_id) : null
    const message = evaluationRecovery.invalid ? LOCAL_RECOVERY_GUIDANCE
      : pendingRequestMessage ?? continuationMessage ?? agentWakeResumePrompt(workspace, agentRuntime, childName)
    setReply(message)
    if (announce) speakReply('我在。', undefined, { localOnly: true })
  }

  const keepPendingEvaluation = (receipt: PendingEvaluationReceipt): void => {
    savePendingEvaluation(receipt)
    pendingEvaluationRef.current = receipt
    setVoicePendingEvaluationCapture(receipt.sessionId, receipt.childId, receipt.turnId)
    setPendingEvaluation(receipt)
  }

  const finishPendingEvaluation = (turnId: string): void => {
    const receipt = pendingEvaluationRef.current
    if (!receipt || receipt.turnId !== turnId) return
    clearPendingEvaluation(receipt)
    pendingEvaluationRef.current = null
    setVoicePendingEvaluationCapture(receipt.sessionId, receipt.childId, null)
    setPendingEvaluation(null)
  }

  useEffect(() => {
    if (!pendingEvaluation || busy) return
    let cancelled = false
    let timer: number | undefined
    const turnId = pendingEvaluation.turnId
    let attempt = 0
    const check = async (): Promise<void> => {
      let receiptDelay = 0
      try {
        const result = await localApi.agentTurnExecutionStatus(turnId)
        const receipt = pendingEvaluationRef.current
        if (cancelled || !mountedRef.current || receipt?.turnId !== turnId) return
        if (result.status === 'completed') {
          const envelope = result.execution
          const recoveredTurn = envelope?.turn as (AgentTurn & { session_id?: string; child_id?: string }) | undefined
          if (!envelope || !recoveredTurn || recoveredTurn.agent_turn_id !== turnId
            || recoveredTurn.session_id !== workspace.session.session_id
            || (recoveredTurn.child_id !== undefined && recoveredTurn.child_id !== workspace.session.child_id)
            || envelope.execution.capability !== 'learning.evaluate_response') {
            keepPendingEvaluation({ ...receipt, status: 'unknown' })
            setEvaluationStatus('返回的评价记录与当前孩子或会话不符，未展示或播报。原回答与证据已保留，请家长核对。')
            return
          }
          const delivery = evaluationFeedbackPolicy(envelope.execution.result)
          const silent = receipt.silent || speechSuppressedRef.current || busyRef.current || !delivery.deliver
          finishPendingEvaluation(turnId)
          setEvaluationStatus(silent ? '原作答评价已保留为证据，不会恢复旧目标或继续旧追问。' : '已从原作答记录恢复评价结果，没有重新调用模型。')
          if (!silent) {
            // A timed-out POST can leave the turn's draft response unchanged.
            // Only the durable evaluator output is authoritative on this GET.
            setReply(envelope.execution.response_text)
            const policy = agentReplySpeechPolicy(envelope.turn)
            policy.localOnly ||= delivery.localOnly
            speakReply(envelope.execution.response_text, undefined, policy)
            void refreshAgentRuntime().catch(() => undefined)
          }
          return
        }
        if (result.status === 'unknown') {
          keepPendingEvaluation({ ...receipt, status: 'unknown' })
          setEvaluationStatus('这次作答评价的结果尚未确认，原回答与证据已保留；只能核对原记录状态，不会把重试作为新作答。')
          const work = result.original_audio_teaching
          if (work?.state !== 'response_saved' || work.delivery_status !== 'awaiting_receipt'
            || work.source_verification !== 'current' || work.error_code !== 'SAVED_DELIVERY_UNCONFIRMED') return
          // Query only the admitted turn's receipt; never repeat uncertain delivery.
          receiptDelay = Number.isFinite(work.retry_after_seconds)
            ? Math.min(60, Math.max(1, work.retry_after_seconds)) * 1000 : 5000
        } else {
          if (result.status !== 'in_progress') throw new Error('EVALUATION_STATUS_INVALID')
          keepPendingEvaluation({ ...receipt, status: 'in_progress' })
          setEvaluationStatus('这次作答评价仍在处理中，原回答与证据已保留；正在核对原记录，不会重复调用模型。')
        }
      } catch {
        if (cancelled || !mountedRef.current || pendingEvaluationRef.current?.turnId !== turnId) return
        setEvaluationStatus('暂时无法核对原作答的评价状态，原回答与证据已保留；不会重新提交作答或调用模型。')
      }
      attempt += 1
      if (attempt < EVALUATION_STATUS_POLL_DELAYS.length) timer = window.setTimeout(() => void check(), Math.max(receiptDelay, EVALUATION_STATUS_POLL_DELAYS[attempt]))
      else setEvaluationStatus('本轮状态核对已暂停，原作答评价仍未确认。原回答与证据已保留，可继续核对原记录状态。')
    }
    timer = window.setTimeout(() => void check(), EVALUATION_STATUS_POLL_DELAYS[0])
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [pendingEvaluation?.turnId, busy, evaluationCheckSignal])

  const activePetVocabularyItem = workspace.plan_items.find((item) =>
    item.status === 'active' && item.source_type === 'pet_daily' &&
    item.source_ref?.startsWith('pet-vocabulary-due:')
  )
  useEffect(() => {
    if (!activePetVocabularyItem) {
      petVocabularyPromptRequestedRef.current = null
      return
    }
    if (petVocabularyPromptRequestedRef.current === activePetVocabularyItem.plan_item_id) return
    petVocabularyPromptRequestedRef.current = activePetVocabularyItem.plan_item_id
    let cancelled = false
    void localApi.nextPetVocabulary(workspace.session.session_id)
      .then((review) => {
        if (cancelled || !mountedRef.current) return
        setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
          review.card?.prompt_session_id && review.card.card_version ? { kind: 'vocabulary', binding: {
            prompt_session_id: review.card.prompt_session_id, card_version: review.card.card_version } } : null)
        const message = review.card?.prompt
          ?? (review.summary.deck_size === 0
            ? '还没有通过教材证据确认的词汇卡，这项任务暂时不会凭空出题。'
            : '这项任务的到期词汇已经复习完了。')
        setReply(message)
        if (!speechSuppressedRef.current) speakReply(message,undefined,{localOnly:true,ordinaryResponseAllowed:!!review.card})
      })
      .catch(() => {
        if (cancelled || !mountedRef.current) return
        petVocabularyPromptRequestedRef.current = null
        const message = '词汇卡暂时没有载入，任务还在，请再说一次“继续背单词”。'
        setReply(message)
        if (!speechSuppressedRef.current) speakReply(message)
      })
    return () => { cancelled = true }
  }, [activePetVocabularyItem?.plan_item_id, workspace.session.session_id])

  const stopGlobalTutorTimeline = useCallback((): void => {
    globalTutorGenerationRef.current += 1
    globalTutorTimersRef.current.forEach((timer) => window.clearTimeout(timer))
    globalTutorTimersRef.current = []
  }, [])

  const tutorPlayback = useMemo(() => new TutorPlaybackController(workspace.session.session_id, workspace.session.child_id, {
    speak: (text, sessionId, delivery, started, policy) => {
      if (privacyStopPending(sessionId) || tutorialSpeechOwners.has(sessionId)) { policy.onNotDelivered?.('unavailable'); return }
      speakLocally(text, sessionId, delivery, started, policy)
    },
    cancel: cancelLocalSpeech,
    localNarration: setLocalTutorPlayback,
    allowed: () => mountedRef.current && !speechSuppressedRef.current,
    display: (state, revealed) => {
      globalTutorPlayingRef.current = state.status === 'playing'
      setGlobalTutorBoardTurnId(['cancelled', 'invalidated'].includes(state.status) ? null : state.turn_id)
      setGlobalTutorBoard(documentCaptureActiveRef.current || speechSuppressedRef.current || ['cancelled', 'invalidated'].includes(state.status) ? null : state.presentation)
      setGlobalTutorVisualTurnId(state.presentation?.source === 'visual_teaching_v1'
        && !['cancelled', 'invalidated'].includes(state.status) ? state.turn_id : null)
      setGlobalTutorSegments(revealed)
    },
    error: message => { setReply(message); setTutorSyncStatus(message) },
    delivery: result => setVoiceDelivery(result === 'managed' ? 'natural' : result),
    response: (state,text,current,sourceAgentTurnId)=>ordinaryResponseMayOwnPrompt(workspace.session.session_id,workspace.session.child_id)?ordinaryPromptPlayback({sessionId:workspace.session.session_id,childId:workspace.session.child_id,state:ordinarySessionRef.current.state,
      prompt:state,text,sourceAgentTurnId,sourceTutorTurnId:state.turn_id,deliveryPending:true,
      isCurrent:()=>current()&&mountedRef.current&&!speechSuppressedRef.current&&!privacyStopPending(workspace.session.session_id)}, {localOnly:true}):undefined,
  }), [workspace.session.session_id, workspace.session.child_id])

  const pauseUnavailableTutorVisual = useCallback(() => {
    tutorPlayback.interrupt()
    setTutorSyncStatus('原题图形暂时无法核对，讲解已暂停；核对材料后可以继续。')
  }, [tutorPlayback])

  useEffect(() => {
    void tutorPlayback.restore().catch(() => { if (mountedRef.current) setTutorSyncStatus('讲解进度暂时未读到；开始讲解前会再次核对。') })
    const forget = (event: Event): void => {
      if ((event as CustomEvent<{ childId: string }>).detail?.childId !== workspace.session.child_id) return
      tutorPlayback.forget()
      setGlobalTutorTurn(null); setGlobalTutorBoard(null); setGlobalTutorSegments(0)
    }
    window.addEventListener('hsc-tutor-playback-forget', forget)
    return () => { window.removeEventListener('hsc-tutor-playback-forget', forget); tutorPlayback.dispose() }
  }, [tutorPlayback, workspace.session.child_id])

  const tutorPresentationOwnerRef = useRef<symbol | null>(null)
  const presentGlobalTutorTurn = useCallback(async (turn: TutorTurn, speechPolicy?: AgentReplySpeechPolicy, signal?: AbortSignal): Promise<void> => {
    if (speechSuppressedRef.current || !mountedRef.current || signal?.aborted || speechPolicy?.isCurrent?.() === false) return
    const owner = Symbol('tutor-presentation')
    tutorPresentationOwnerRef.current = owner
    const abort = (): void => {
      if (tutorPresentationOwnerRef.current !== owner) return
      stopGlobalTutorTimeline()
      tutorPlayback.interrupt()
    }
    signal?.addEventListener('abort', abort, { once: true })
    try {
      cancelLocalSpeech()
      stopGlobalTutorTimeline()
      setGlobalTutorTurn(turn)
      const policy = { ...speechPolicy, localOnly: !speechPolicy?.sourceAgentTurnId || speechPolicy.localOnly || tutorRequiresLocalSpeech(turn),
        sourceTutorTurnId: turn.turn_id, ordinaryResponseAllowed: true }
      const presentation = tutorBlackboardFromTurn(turn)
      if (!presentation) {
        tutorPlayback.interrupt()
        setGlobalTutorBoard(null)
        setGlobalTutorSegments(0)
        speakReply(turn.answer, undefined, policy)
        return
      }
      // The durable Tutor controller owns both speech and board callbacks.
      // Returning a generic RTC fallback would cancel this independent player.
      if (realtimeSpeechCaptureRef.current) realtimeSpeechCaptureRef.current.tutorPlaybackOwned = true
      setGlobalTutorSegments(0)
      lastSpeechRequestedAtRef.current = Date.now()
      setVoiceActivationSignal((current) => current + 1)
      setVoiceDelivery('pending')
      await tutorPlayback.present(turn.turn_id, presentation, policy)
    } finally {
      signal?.removeEventListener('abort', abort)
      if (tutorPresentationOwnerRef.current === owner) tutorPresentationOwnerRef.current = null
    }
  }, [stopGlobalTutorTimeline, workspace.session.session_id, tutorPlayback])

  const curriculumTutorPendingRef = useRef<(() => boolean) | null>(null)
  const receiveCurriculumTutor = (utterance: string, dispatch: QwenCurriculumTutorDispatch, voiceCurrent: () => boolean): boolean => {
    const { session_id: sessionId, child_id: childId } = workspace.session
    const current = () => voiceCurrent() && mountedRef.current && !speechSuppressedRef.current && !privacyStopPending(sessionId)
    if (!current()) return false
    curriculumTutorPendingRef.current = current
    setTutorSyncStatus('正在准备这节课的讲解。')
    void (async () => {
      try {
        const taught = await localApi.askTutor(sessionId, dispatch.subject, utterance.trim(), 6, undefined, dispatch.action_id)
        if (!current() || taught.session_id !== sessionId || taught.child_id !== childId) return
        setTutorSyncStatus('')
        await presentGlobalTutorTurn(taught, { localOnly: true, isCurrent: current })
      } catch {
        if (!current()) return
        const message = '这次讲解还没有返回，你刚才的话已经保存。'
        setTutorSyncStatus(message)
        setReply(message)
        speakReply(message, undefined, { localOnly: true, skipPublicReplyProvider: true, isCurrent: current })
      } finally {
        if (curriculumTutorPendingRef.current === current) curriculumTutorPendingRef.current = null
      }
    })()
    return true
  }

  useEffect(() => {
    const present = (event: Event): void => {
      const detail = (event as CustomEvent<TutorPresentationVoiceDetail>).detail
      if (event.defaultPrevented || !detail?.turn || detail.turn.session_id !== workspace.session.session_id
        || detail.turn.child_id !== workspace.session.child_id || detail.signal?.aborted
        || !mountedRef.current || speechSuppressedRef.current) return
      event.preventDefault()
      void presentGlobalTutorTurn(detail.turn, detail.speechPolicy, detail.signal).then(() => detail.resolve(true), () => detail.resolve(false))
    }
    window.addEventListener('hsc-runtime-tutor-presentation', present)
    return () => window.removeEventListener('hsc-runtime-tutor-presentation', present)
  }, [presentGlobalTutorTurn, workspace.session.session_id, workspace.session.child_id])

  useEffect(() => {
    let live = true
    const receive = (event: Event): void => {
      const detail = (event as CustomEvent<NativeWritingFeedback>).detail
      if (!detail || detail.scope.child_id !== workspace.session.child_id || detail.scope.session_id !== workspace.session.session_id) return
      event.preventDefault()
      const isCurrent = (): boolean => live && mountedRef.current && detail.isCurrent()
        && !privacyStopPending(workspace.session.session_id)
      void (async () => {
        const saved = await localApi.diagnosticPrompt(workspace.session.session_id)
        if (!isCurrent() || !saved.prompt || !sameWritingScope(writingScope(saved.prompt),detail.scope)
          || saved.attempt?.attempt_id !== detail.result.attempt?.attempt_id) throw Error('原稿已保存，当前题目已变化；没有继续旧稿反馈。')
        const feedback = await continueWritingFeedback({notice:detail.result.local_learning_evaluation,
          childId:detail.scope.child_id,sessionId:detail.scope.session_id,isCurrent,
          execute:id=>localApi.executeAgentTurn(id),
          recover:async id=>{const value=await localApi.agentTurnExecutionStatus(id);return value.status==='completed'?value.execution??null:null},
          beforeEvaluation:id=>{
            if(pendingEvaluationRef.current&&pendingEvaluationRef.current.turnId!==id)throw Error('原作答评价尚未确认，未重复启动评价。')
            keepPendingEvaluation({version:1,childId:detail.scope.child_id,sessionId:detail.scope.session_id,turnId:id,status:'in_progress',silent:false})
          },finishEvaluation:finishPendingEvaluation})
        if (!isCurrent()) throw Error('原稿与反馈已保留，没有恢复旧话题。')
        const message=feedback?.execution.response_text ?? writingFeedbackHoldMessage(detail.result.local_learning_evaluation)
          ?? '这份文字已保存，但原稿资格尚未通过核对，不记作掌握。'
        setReply(message)
        if(feedback?.execution.tutor_turn)await presentGlobalTutorTurn(feedback.execution.tutor_turn,{localOnly:true,isCurrent,sourceAgentTurnId:feedback.turn.agent_turn_id})
        else speakReply(message,undefined,{localOnly:true,isCurrent,sourceAgentTurnId:feedback?.turn.agent_turn_id,ordinaryResponseAllowed:!!feedback?.turn.agent_turn_id})
        await refreshAgentRuntime()
        detail.resolve(message)
      })().catch(cause=>{
        const message=cause instanceof Error?cause.message:'原稿已保存，反馈未确认；无需重写或重复提交。'
        if(isCurrent())setReply(message)
        detail.resolve(message)
      })
    }
    window.addEventListener(NATIVE_WRITING_FEEDBACK,receive)
    return ()=>{live=false;window.removeEventListener(NATIVE_WRITING_FEEDBACK,receive)}
  },[workspace.session.session_id,workspace.session.child_id,presentGlobalTutorTurn])

  const startGlobalQuestionCapture = (forMistake: boolean): void => {
    stopGlobalTutorTimeline()
    setGlobalTutorTurn(null)
    setGlobalTutorBoard(null)
    setGlobalTutorSegments(0)
    setGlobalCaptureReview(null)
    setRestoredDocumentGoal(null)
    setGlobalCaptureMistakeArtifact(null)
    setGlobalCaptureForMistake(forMistake)
    setGlobalCaptureVisible(true)
    setGlobalCaptureGeneration((value) => value + 1)
  }

  const finishGlobalOcrConfirmation = async (correctedText?: string, goal?: DocumentGoalConfirmation): Promise<TutorTurn | 'goal_prepared' | null> => {
    const generation = commandGenerationRef.current
    const review = goal ? await readOcrDocumentGoal(workspace.session.session_id, workspace.session.child_id, goal,
      voiceEventController.current.signal) : globalCaptureReview
    if (generation !== commandGenerationRef.current || !mountedRef.current) throw new SupersededDeviceCommand()
    if (!review || review.status === 'unreadable' || (review.status === 'confirmed' && !goal)) return null
    const confirmedText = (correctedText ?? review.text).replace(/\s+/g, ' ').trim()
    if (confirmedText.length < 2) return null
    const confirmed = !goal && review.status === 'confirmed' && confirmedText === review.text
      ? review : await confirmOcrTutorEvidence(workspace.session.session_id, review, confirmedText)
    if (generation !== commandGenerationRef.current || !mountedRef.current) throw new SupersededDeviceCommand()
    setGlobalCaptureReview(confirmed)
    if (goal) {
      await prepareOcrDocumentGoal(workspace.session.session_id, confirmed, goal)
      if (generation !== commandGenerationRef.current || !mountedRef.current) throw new SupersededDeviceCommand()
      setGlobalCaptureVisible(false)
      setRestoredDocumentGoal(null)
      setGlobalCaptureReview(null)
      return 'goal_prepared'
    }
    const current = workspace.plan_items.find((item) => item.status === 'active')
      ?? workspace.plan_items.find((item) => item.status === 'pending' && item.source_type !== 'planned_break')
    const turn = await askTutorWithOcrEvidence(
      workspace.session.session_id,
      current?.subject ?? inferQuestionSubject(confirmed.text),
      confirmed.confirmationId!,
      crypto.randomUUID(),
    )
    if (generation !== commandGenerationRef.current || !mountedRef.current) throw new SupersededDeviceCommand()
    setGlobalCaptureVisible(false)
    setGlobalCaptureReview(null)
    await presentGlobalTutorTurn(turn)
    return turn
  }

  useEffect(() => {
    const unsubscribe = subscribeCompanionVoice((state) => {
      if (state === 'interrupted') { stopGlobalTutorTimeline(); tutorPlayback.interrupt() }
      else if (state === 'error') {
        globalTutorTimersRef.current.forEach((timer) => window.clearTimeout(timer))
        globalTutorTimersRef.current = []
      }
    })
    return () => {
      unsubscribe()
      stopGlobalTutorTimeline()
    }
  }, [stopGlobalTutorTimeline, tutorPlayback])

  useEffect(() => {
    let current = true
    setWakeSettingsLoaded(false)
    setWakeSettingsReadError(false)
    void (async () => {
      try {
        const settings = await window.desktop?.getWakeWordSettings?.(workspace.session.child_id) ?? null
        if (!current) return
        setWakeSettings(settings)
        savedWakeSettingsRef.current = settings
        setWakeWordDraft(settings?.wakeWord ?? '')
        setWakeInactivitySeconds(settings?.inactivitySeconds ?? 60)
      } catch (cause) {
        if (current) {
          setWakeSettingsReadError(true)
          setWakeSettingsMessage(cause instanceof Error ? cause.message : '唤醒词设置暂时无法读取。')
          if (onboardingSupported) setOnboarding({ phase: 'error', step: null, prompt: '名字设置还没能读到。请说“重试”，不会覆盖原来的名字。', silent: false })
        }
      } finally {
        if (current) setWakeSettingsLoaded(true)
      }
    })()
    return () => { current = false }
  }, [workspace.session.child_id, wakeSettingsReadAttempt, onboardingSupported])

  const saveWakeWord = async (spokenCandidate?: string): Promise<boolean> => {
    if (!mountedRef.current || savingWakeWord.current || speechSuppressedRef.current) return false
    if (!window.desktop?.saveWakeWordSettings) {
      setWakeSettingsMessage('当前桌面语音服务无法保存唤醒词。我会保留当前内容，请告诉家长检查本机服务。')
      return false
    }
    setWakeSettingsMessage(null)
    savingWakeWord.current = true
    const generation = commandGenerationRef.current
    try {
      const candidate = (spokenCandidate ?? wakeWordDraft).trim()
      const saved = await window.desktop.saveWakeWordSettings({
        childId: workspace.session.child_id,
        wakeWord: candidate,
        inactivitySeconds: wakeInactivitySeconds,
        enabled: true,
        updatedAt: new Date().toISOString(),
      })
      if (!mountedRef.current || speechSuppressedRef.current || generation !== commandGenerationRef.current) return false
      if (saved.childId !== workspace.session.child_id || saved.wakeWord !== candidate.replace(/[，。！？、,.!?]/g, '') || !saved.enabled) throw new Error('唤醒词保存回执不一致，请说“重试”。')
      setWakeSettings(saved)
      savedWakeSettingsRef.current = saved
      setWakeWordDraft(saved.wakeWord)
      setEditingWakeWord(false)
      pendingNameRef.current = null
      setWakeSettingsMessage(`已开始在本机监听“${saved.wakeWord}”；唤醒后${saved.inactivitySeconds}秒没有新对话会回到待机。`)
      const namingRuntime = onboardingRef.current
      const needsNamedProgress = namingRuntime?.snapshot.phase === 'naming'
      await namingRuntime?.named(saved.wakeWord)
      if (!mountedRef.current || speechSuppressedRef.current || generation !== commandGenerationRef.current || onboardingRef.current !== namingRuntime) return false
      if (needsNamedProgress && (namingRuntime?.snapshot.phase !== 'active' || namingRuntime.snapshot.step !== 'wake_practice')) return false
      if (onboardingRef.current?.snapshot.phase === 'idle' || !onboardingSupported) {
        const message = `唤醒名字已改为“${saved.wakeWord}”。我们继续聊，待机后再用这个名字叫我。`
        setReply(message)
        speakReply(message, undefined, { localOnly: true }, true)
      }
      return true
    } catch (cause) {
      if (!mountedRef.current || speechSuppressedRef.current || generation !== commandGenerationRef.current) return false
      if (onboardingSupported && onboardingRef.current?.snapshot.phase === 'idle' && !editingWakeWord) return false
      setWakeSettingsMessage(cause instanceof Error ? cause.message : '唤醒词没有保存。')
      const conflict = cause instanceof Error && cause.message.includes('这个唤醒名字已经有人使用了')
      if (conflict) { pendingNameRef.current = null; setWakeWordDraft('') }
      const message = conflict
        ? '这个唤醒名字已经有人使用了。请换一个名字，避免进入另一位孩子的学习记录。'
        : '名字还没有保存成功。请说“重试”，我会保留刚才确认的名字。'
      setReply(message)
      speakReply(message, undefined, { localOnly: true }, true)
      return false
    } finally { savingWakeWord.current = false }
  }

  const receiveHandsFreeTranscript = async (value: string, overlap?: PlaybackOverlap, voiceInputId?: string, pendingEvaluationTurnId?: string, localRecoveryOnly = false, capturedCarryoverConfirmationId?: string | null): Promise<void> => {
    await submit(value, overlap, voiceInputId, pendingEvaluationTurnId, localRecoveryOnly, capturedCarryoverConfirmationId)
  }

  useEffect(() => {
    const update = (event: Event): void => {
      const detail = (event as CustomEvent<{ sessionId?: string; active?: boolean }>).detail
      if (detail?.sessionId === workspace.session.session_id) {
        documentCaptureActiveRef.current = Boolean(detail.active)
        setDocumentCaptureActive(Boolean(detail.active))
      }
    }
    window.addEventListener('hsc-document-capture-presence', update)
    return () => window.removeEventListener('hsc-document-capture-presence', update)
  }, [workspace.session.session_id])

  useEffect(() => {
    if (onboardingSupported || !window.desktop?.saveWakeWordSettings || !wakeSettingsLoaded || wakeSettings || editingWakeWord || tutorRecovery) return
    const message = '先给我起一个唤醒名字吧。直接说一个两到十二个字的短名字，例如“小知”。'
    setReply(message)
    speakReply(message)
  }, [editingWakeWord, onboardingSupported, tutorRecovery, wakeSettings, wakeSettingsLoaded])

  useEffect(() => {
    if ((!tutorRecovery && !pendingTutorCompletion) || busyRef.current || pendingTutorCompletion?.presentationStarted || tutorialSpeechOwners.has(workspace.session.session_id)) return
    const message = pendingTutorCompletion ? tutorCompletionMessage(pendingTutorCompletion) : tutorRecovery!.sourceBlocked ? tutorSourceBlockMessage(tutorRecovery!.sourceBlocked) : tutorRecovery!.requiresExplicitRetryConfirmation
      ? '上次讲解结果没有确认保存。你可以说“确认再次请求”，或者说“保留问题重新问”。'
      : `${tutorFailureMessage(tutorRecovery!.attempts)} 你可以说“重试讲解”。`
    setReply(message)
    if (!speechSuppressedRef.current) speakReply(message)
  }, [tutorRecovery?.actionId, tutorRecovery?.attempts, tutorRecovery?.requiresExplicitRetryConfirmation, tutorRecovery?.sourceBlocked, pendingTutorCompletion?.turnId, tutorialOwnsSpeech])

  const makeTutorCompletion = (turnId: string, actionId: string, actionType: TutorCompletion['receipt']['action_type'], response: string,
    output: Record<string, unknown>, localOnly: boolean, tutor?: TutorTurn): TutorCompletion => ({
    version: 1, sessionId: workspace.session.session_id, childId: workspace.session.child_id, turnId, actionId, response, draftText: text, tutor,
    localOnly: localOnly || tutorRequiresLocalSpeech(tutor),
    acknowledged: false, presentationStarted: false,
    receipt: { capability: actionType === 'ask_tutor' ? 'learning.explain' : actionType === 'more_tutor_hint' ? 'tutor.more_hint' : 'tutor.record_feedback',
      execution_location: actionType === 'ask_tutor' ? 'local_model' : 'client_device_bridge', action_type: actionType, status: 'completed',
      completed_at: new Date().toISOString(), output },
  })

  const finishTutorCompletion = async (initial: TutorCompletion, isCurrent: () => boolean): Promise<void> => {
    let item = initial
    let stage: 'journal' | 'ack' | 'cleanup' = 'journal'
    const remember = (): void => {
      pendingTutorCompletionRef.current = item
      if (isCurrent()) setPendingTutorCompletion(item)
      saveTutorCompletion(item)
    }
    try {
      if (item.tutor && (item.tutor.session_id !== workspace.session.session_id || item.tutor.child_id !== workspace.session.child_id)) throw new Error('TUTOR_RESULT_OWNER_MISMATCH')
      remember()
      if (!isCurrent()) return
      if (!item.acknowledged) {
        stage = 'ack'
        const completed = await waitForTutorReceipt(commandApi.completeAgentTurn(item.turnId, item.response, 'completed', undefined, {
          execution_receipt: item.receipt,
          ...(item.receipt.execution_location === 'client_device_bridge' ? { client_bridge_receipt: item.receipt } : {}),
        }))
        if (completed.agent_turn_id !== item.turnId || completed.tool_status !== 'completed') throw new Error('TUTOR_RECEIPT_UNCONFIRMED')
        item = { ...item, acknowledged: true, localOnly: item.localOnly || Boolean(completed.action && agentReplySpeechPolicy(completed).localOnly) }
        stage = 'journal'
        remember()
      }
      if (!isCurrent()) return
      if (!item.presentationStarted && !speechSuppressedRef.current) {
        item = { ...item, presentationStarted: true }
        remember()
        const policy = { localOnly: item.localOnly, sourceAgentTurnId: item.turnId }
        setReply(item.response)
        if (item.tutor) {
          await presentGlobalTutorTurn(item.tutor, policy)
        } else speakReply(item.response, undefined, policy)
      }
      if (!isCurrent()) return
      stage = 'cleanup'
      const nextRecovery = tutorRecoveryRef.current?.turnId === item.turnId ? null : tutorRecoveryRef.current
      const keepInput = pendingUtterancesRef.current.length > 0 || voiceInputQueue.pendingCount > 0 || preserveInputRef.current || currentTextRef.current !== item.draftText
      if (keepInput || nextRecovery) await draft.saveNow({ text: currentTextRef.current, tutor_recovery: nextRecovery })
      else await draft.clear()
      if (!isCurrent()) return
      clearTutorCompletion(item)
      tutorRecoveryRef.current = nextRecovery
      setTutorRecovery(nextRecovery)
      if (!keepInput && !nextRecovery) setText('')
      const next = readTutorCompletions(workspace.session.session_id, workspace.session.child_id)[0] ?? null
      pendingTutorCompletionRef.current = next
      setPendingTutorCompletion(next)
      setTutorSyncStatus('')
      // Projection reads do not change the already-confirmed execution result.
      const refreshed = await run(async () => undefined)
      await refreshAgentRuntime().catch(() => { if (isCurrent()) setTutorSyncStatus('结果已保存，运行状态暂未刷新。') })
      if (isCurrent() && !refreshed) setTutorSyncStatus('结果已保存，页面状态暂未刷新。')
    } catch {
      if (!isCurrent()) return
      const message = stage === 'journal'
        ? '结果已经取得，但本机恢复记录还没保存成功。请保持页面，重试只保存已有结果，不会再次请求模型。'
        : tutorCompletionMessage(item)
      setTutorSyncStatus(message)
      if (!item.presentationStarted && !speechSuppressedRef.current) speakReply(message, undefined, { localOnly: true })
    }
  }

  const retryTutorCompletion = async (): Promise<void> => {
    const item = pendingTutorCompletionRef.current
    if (!item || busyRef.current) return
    const generation = commandGenerationRef.current
    const isCurrent = (): boolean => mountedRef.current && generation === commandGenerationRef.current
    busyRef.current = true
    setBusy(true)
    try { await finishTutorCompletion(item, isCurrent) }
    finally {
      if (isCurrent()) {
        busyRef.current = false
        const next = pendingUtterancesRef.current.shift()
        setQueuedUtteranceCount(pendingUtterancesRef.current.length)
        if (next) void submitLatest.current(next.text, undefined, next.overlap, next.voiceInputId, next.pendingScope, next.localRecoveryOnly, next.carryoverArrival, undefined, next.handoff)
        else setBusy(false)
      }
    }
  }

  const finishTutorRecovery = async (recovery: TutorToolRecovery, confirmUnknownOutcomeRetry = false): Promise<void> => {
    if (recovery.sourceBlocked) {
      const message = tutorSourceBlockMessage(recovery.sourceBlocked)
      setReply(message)
      if (!speechSuppressedRef.current) speakReply(message)
      return
    }
    if (completionRestore.invalid) { setTutorSyncStatus('本机讲解结果恢复记录未通过核对，暂不重复请求模型。'); return }
    if (pendingTutorCompletionRef.current?.turnId === recovery.turnId) { await retryTutorCompletion(); return }
    const generation = commandGenerationRef.current
    const isCurrent = (): boolean => mountedRef.current && generation === commandGenerationRef.current
    const localApi = guardCommandApi(commandApi, isCurrent)
    busyRef.current = true
    setBusy(true)
    try {
      await draft.saveNow({ text, tutor_recovery: { ...recovery, requiresExplicitRetryConfirmation: true } })
      if (!isCurrent()) return
      const rawTutor = recovery.followupSourceTurnId
        ? await commandApi.askTutorFollowup(workspace.session.session_id, recovery.followupSourceTurnId, recovery.question, recovery.actionId, confirmUnknownOutcomeRetry)
        : recovery.ocrConfirmationId
        ? await askTutorWithOcrEvidence(workspace.session.session_id, recovery.subject, recovery.ocrConfirmationId,
          recovery.actionId, recovery.hintLevel ?? 1, recovery.parentTurnId, undefined, confirmUnknownOutcomeRetry)
        : recovery.questionReference
        ? await commandApi.askTutor(workspace.session.session_id, recovery.subject, recovery.question, recovery.hintLevel ?? 1, recovery.parentTurnId, recovery.actionId, confirmUnknownOutcomeRetry, recovery.questionReference)
        : await commandApi.askTutor(workspace.session.session_id, recovery.subject, recovery.question, recovery.hintLevel ?? 1, recovery.parentTurnId, recovery.actionId, confirmUnknownOutcomeRetry)
      const tutor = appendLocalDiagnosticFollowUp(rawTutor, recovery.localDiagnosticPrompt || recovery.courseContext ? {
        status: 'matched', cloud_transmission_allowed: false, mastery_update_allowed: false,
        audit_event_id: recovery.atomicAuditEventId ?? null, matched_node_count: 0,
        approved_node_count: 0, hypothesis_count: 1,
        diagnostic_prompt: recovery.localDiagnosticPrompt ?? null,
        ...(recovery.courseContext ? { course_context: recovery.courseContext } : {}),
      } : undefined)
      const completion = makeTutorCompletion(recovery.turnId, recovery.actionId, recovery.actionType ?? 'ask_tutor', tutor.answer,
        tutorExecutionSummary(tutor), true, tutor)
      await finishTutorCompletion(completion, isCurrent)
    } catch (cause) {
      if (!isCurrent() || cause instanceof SupersededDeviceCommand) return
      const unknownOutcome = cause instanceof ApiError && cause.code === 'TUTOR_RESULT_UNCONFIRMED'
      const sourceBlocked = cause instanceof ApiError ? tutorSourceBlock(cause.code) : undefined
      const next = {
        ...recovery,
        attempts: Math.min(3, recovery.attempts + 1),
        ...(sourceBlocked ? { sourceBlocked } : {}),
        requiresExplicitRetryConfirmation: !sourceBlocked && (unknownOutcome || recovery.requiresExplicitRetryConfirmation),
      }
      tutorRecoveryRef.current = next
      setTutorRecovery(next)
      let message = sourceBlocked ? tutorSourceBlockMessage(sourceBlocked) : unknownOutcome
        ? '上次讲解结果没有确认保存。再次请求可能会再次调用模型；请先确认。'
        : tutorFailureMessage(next.attempts)
      try { await draft.saveNow({ text, tutor_recovery: next }) }
      catch { message = '恢复记录还没有保存成功，请不要关闭页面。可以说“重试讲解”先重试保存，不会跳过保存发送请求。' }
      if (!isCurrent()) return
      if (cause instanceof VoiceRecoverySaveError) message = '恢复记录还没有保存成功，这次没有请求讲解。请保持页面，可以说“重试讲解”。'
      await localApi.completeAgentTurn(recovery.turnId, message, 'failed', sourceBlocked && cause instanceof ApiError ? cause.code : unknownOutcome ? 'TUTOR_RESULT_UNCONFIRMED' : 'AGENT_TUTOR_UNAVAILABLE').catch(() => undefined)
      setReply(message)
      if (!speechSuppressedRef.current) speakReply(message)
    } finally {
      if (isCurrent()) {
        busyRef.current = false
        const next = pendingUtterancesRef.current.shift()
        setQueuedUtteranceCount(pendingUtterancesRef.current.length)
        if (next) void submitLatest.current(next.text, undefined, next.overlap, next.voiceInputId, next.pendingScope, next.localRecoveryOnly, next.carryoverArrival, undefined, next.handoff)
        else setBusy(false)
      }
    }
  }

  const abandonUnknownTutorRecovery = async (recovery: TutorToolRecovery): Promise<void> => {
    // A different request must have a new action ID.  Keep the child's words in
    // the input, but delete the old replay identity before it can be retried.
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    try {
      await draft.saveNow({ text: recovery.question, tutor_recovery: null })
      if (!mountedRef.current || speechSuppressedRef.current) return
      setTutorRecovery(null)
      setText(recovery.question)
      const message = '问题已保留，不会自动重放原请求。可以直接说新的问题。'
      setReply(message)
      speakReply(message, undefined, { localOnly: true })
    } catch {
      if (mountedRef.current && !speechSuppressedRef.current) {
        const message = '保留问题还没有保存成功，原请求仍保留。请不要关闭页面，可以再说“保留问题”。'
        setReply(message)
        speakReply(message, undefined, { localOnly: true })
      }
    } finally { busyRef.current = false; if (mountedRef.current) setBusy(false) }
  }

  useEffect(() => {
    mountedRef.current = true
    if (voiceEventController.current.signal.aborted && !speechSuppressedRef.current) voiceEventController.current = new AbortController()
    let ownsReminders = true
    const behaviorPrompts = new Set<string>()
    const intervene = (event: Event): void => {
      if (nativeDialogueActiveRef.current || tutorialSpeechOwners.has(workspace.session.session_id)) return
      const detail = (event as CustomEvent<{ message: string; promptId?: string; sourceType?: string;
        sessionId?: string; childId?: string; expiresAt?: string }>).detail
      const message = detail?.message
      if (!message) return
      if ((detail.sessionId && detail.sessionId !== workspace.session.session_id)
        || (detail.childId && detail.childId !== workspace.session.child_id)) return
      const behavior = ['behavior_intervention', 'oral_supply_ready', 'oral_daily_supply_ready', 'due_commitment', 'task_ready'].includes(detail.sourceType ?? '')
      if (behavior && (!detail.promptId || detail.sessionId !== workspace.session.session_id
        || detail.childId !== workspace.session.child_id || behaviorPrompts.has(detail.promptId))) return
      const generation = commandGenerationRef.current
      const currentOwner = (): boolean => !nativeDialogueActiveRef.current && ownsReminders && mountedRef.current && !speechSuppressedRef.current
        && generation === commandGenerationRef.current
      const release = (): void => { if (detail.promptId) behaviorPrompts.delete(detail.promptId) }
      if (!behavior) setReply(message)
      if (!speechSuppressedRef.current) {
        if (behavior) behaviorPrompts.add(detail.promptId!)
        speakReply(message, undefined, { localOnly: true, priority: 'reminder',
          speechKey: detail.promptId ?? 'observation-guidance',
          expiresAt: Math.min(Date.now() + 15_000, detail.expiresAt ? Date.parse(detail.expiresAt) : Infinity),
          isCurrent: currentOwner,
          beforePlayback: behavior ? async signal => {
            if (!currentOwner() || signal.aborted) return false
            try {
              const result = await localApi.proactiveAgentPromptCurrent(workspace.session.session_id, detail.promptId!, signal)
              if (!currentOwner() || signal.aborted) return false
              if (result.prompt_id !== detail.promptId || result.current !== true) return false
              setReply(message)
              return true
            } catch {
              return false
            }
          } : undefined,
          onPlaybackAbandoned: behavior ? release : undefined,
          onNotDelivered: behavior ? release : undefined,
          onPlaybackEnd: detail.promptId ? () => {
            if (!currentOwner()) return
            // Keep this delivered ID locally consumed even if its ACK is lost.
            void localApi.acknowledgeProactiveAgentPrompt(
              workspace.session.session_id, detail.promptId!
            ).catch(() => undefined)
          } : undefined,
        })
      }
    }
    const privacyPause = (): void => {
      voiceInputQueue.cancel()
      voiceEventController.current.abort()
      const cancellationOwners = new Map(realtimeAdmissionOwnersRef.current)
      try {
        for (const item of pendingRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id)) {
          const cancelled = updateRealtimeAdmission(item, 'cancel_requested')
          const attempt = realtimeAdmissionsRef.current.get(item.inputId)
          if (attempt) attempt.handoff = cancelled
          cancellationOwners.set(item.inputId, item.sessionId)
        }
      } catch {
        void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.cancellation_journal_unconfirmed', level: 'error',
          message: 'Local audio stopped; durable cancellation handoff was not confirmed.' })
      }
      for (const [inputId, sessionId] of cancellationOwners) {
        void commandApi.cancelTranscriptionAction(sessionId, inputId).catch(() => undefined)
      }
      onboardingRef.current?.suspend()
      tutorialSpeechGeneration.current += 1
      setReadyTutorialPrompt(null)
      commandGenerationRef.current += 1
      busyRef.current = false
      setBusy(false)
      if (pendingUtterancesRef.current.length > 0) preserveInputRef.current = true
      pendingUtterancesRef.current = []
      setQueuedUtteranceCount(0)
      speechSuppressedRef.current = true
      void invalidateVoiceHandoff().catch(() => undefined)
      stopGlobalTutorTimeline()
      setGlobalCaptureVisible(false)
      setGlobalCaptureReview(null)
      captureBridge.cancel()
      cancelLocalSpeech()
      const playback = controlledContentRef.current
      const game = controlledGameRef.current
      if (playback && !controlledContentEndInFlightRef.current.has(playback.content_session_id)) {
        controlledContentEndInFlightRef.current.add(playback.content_session_id)
        void confirmControlledClosure('video', 'privacy_pause', playback)
          .catch(() => { if (mountedRef.current) setControlledActivityError('视频关闭状态未确认，原观看记录仍保留。') })
          .finally(() => controlledContentEndInFlightRef.current.delete(playback.content_session_id))
      }
      if (game && !controlledGameEndInFlightRef.current.has(game.game_session_id)) {
        controlledGameEndInFlightRef.current.add(game.game_session_id)
        void confirmControlledClosure('game', 'privacy_pause', game)
          .catch(() => { if (mountedRef.current) setControlledActivityError('游戏关闭状态未确认，原游戏记录仍保留。') })
          .finally(() => controlledGameEndInFlightRef.current.delete(game.game_session_id))
      }
    }
    window.addEventListener('hsc-agent-intervention', intervene)
    window.addEventListener('hsc-privacy-pause', privacyPause)
    return () => {
      ownsReminders = false
      mountedRef.current = false
      voiceInputQueue.cancel()
      voiceEventController.current.abort()
      commandGenerationRef.current += 1
      pendingUtterancesRef.current = []
      window.removeEventListener('hsc-agent-intervention', intervene)
      window.removeEventListener('hsc-privacy-pause', privacyPause)
    }
  }, [workspace.session.session_id, workspace.session.child_id])

  useEffect(() => {
    const controller = new AbortController()
    const attempts = new Map<string, number>()
    let live = true, running = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const current = (): boolean => live && !controller.signal.aborted
    const discoveryGeneration = commandGenerationRef.current
    void Promise.resolve().then(() => commandApi.agentExecutionRecoveryIndex(workspace.session.session_id, workspace.session.child_id, controller.signal))
      .then(index => {
        if (!current() || speechSuppressedRef.current) return
        const restored = restoreAgentExecutionIdentities(workspace.session.session_id, workspace.session.child_id, index)
        if ((restored || index.has_more) && discoveryGeneration === commandGenerationRef.current) {
          preserveInputRef.current = true
          setReply(AGENT_EXECUTION_UNCONFIRMED + (index.has_more ? '还有原请求需要核对，请家长检查未完成记录。' : ''))
        }
        if (restored || index.has_more) void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.agent.identity_index_restored',
          level: index.has_more ? 'warn' : 'info', details: { restored, has_more: index.has_more, learning_tasks_replayed: 0, contains_transcript: false } })
      }).catch(() => {
        if (current()) void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.agent.identity_index_unconfirmed', level: 'warn',
          message: 'Canonical pending operation identities could not be read; no operation was replayed.' })
      })
    // Chromium's buffered localStorage can lose identity metadata on SIGKILL.
    // Discover canonical local identities only; never execute or replay here.
    void Promise.resolve().then(() => commandApi.realtimeRecoveryIndex(workspace.session.session_id, workspace.session.child_id, controller.signal))
      .then(index => {
        if (!current() || speechSuppressedRef.current) return
        const restored = restoreRealtimeAdmissionIdentities(workspace.session.session_id, workspace.session.child_id, index)
        if (restored) {
          preserveInputRef.current = true
          setReply('上次的采集和交接记录需要核对，不会自动重做。可以说“核对刚才请求”。')
        }
        if (restored || index.has_more) void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.identity_index_restored',
          level: index.has_more ? 'warn' : 'info', details: { restored, has_more: index.has_more, learning_tasks_replayed: 0, contains_transcript: false } })
      }).catch(() => {
        if (current()) void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.identity_index_unconfirmed', level: 'warn',
          message: 'Canonical recovery identities could not be read; no original was replayed.' })
      })
    const run = async (): Promise<void> => {
      if (!current() || running) return
      running = true
      try {
        const result = await recoverRealtimeCancellations(workspace.session.session_id, workspace.session.child_id,
          commandApi.cancelTranscriptionAction, commandApi.realtimeAdmissionReceipt, current, attempts, controller.signal)
        if (!current()) return
        if (result.confirmed) void window.desktop?.recordRendererDiagnostic?.({
          event: 'voice.realtime.cancellation_recovered', level: 'info',
          details: { confirmed: result.confirmed, scope: 'canonical_voice_only', learning_tasks_replayed: 0 },
        })
        if (result.failures) void window.desktop?.recordRendererDiagnostic?.({
          event: 'voice.realtime.cancellation_recovery_unconfirmed', level: 'warn',
          message: 'Durable cancellation remains pending; no speech or learning task was replayed.',
        })
        if (result.retryable) {
          clearTimeout(timer)
          timer = setTimeout(() => { void run() }, 30_000)
        }
      } catch {
        if (current()) void window.desktop?.recordRendererDiagnostic?.({
          event: 'voice.realtime.cancellation_recovery_invalid', level: 'error',
          message: 'Cancellation recovery metadata could not be read; no learning task was replayed.',
        })
      } finally { running = false }
    }
    const online = (): void => {
      if (running) return
      clearTimeout(timer)
      attempts.clear()
      void run()
    }
    const stopped = (): void => {
      clearTimeout(timer)
      timer = setTimeout(() => { void run() }, 30_000)
    }
    void run()
    window.addEventListener('online', online)
    window.addEventListener('hsc-privacy-pause', stopped)
    return () => {
      live = false
      controller.abort()
      clearTimeout(timer)
      window.removeEventListener('online', online)
      window.removeEventListener('hsc-privacy-pause', stopped)
    }
  }, [workspace.session.session_id, workspace.session.child_id])

  const persistPrivacyStop = async (turn?: AgentTurn, restartProtected = true): Promise<void> => {
    setPrivacySaveStatus('本机监听与播放已停止，正在保存隐私选择。')
    let saved = false
    try {
      await localApi.pauseObservation(workspace.session.session_id)
      saved = true
      if (mountedRef.current) setPrivacySaveStatus('隐私选择已保存；本机监听与播放保持停止。')
    } catch {
      if (mountedRef.current) setPrivacySaveStatus('本机监听与播放已停止，但隐私选择尚未保存到服务。重新打开仍保持停止，请重试保存。')
    }
    if (!restartProtected && mountedRef.current) setPrivacySaveStatus('本机监听已停止，但重启保护未能保存；请保持应用关闭并请家长检查存储。')
    if (turn) {
      const receipt = {
        capability: turn.runtime_capability ?? turn.action.capability ?? 'observation.pause',
        execution_location: 'client_device_bridge', action_type: 'privacy_pause',
        status: saved ? 'completed' : 'failed', completed_at: new Date().toISOString(),
        output: { local_stop_dispatched: true, privacy_choice_saved: saved, restart_protected: restartProtected },
      }
      await localApi.completeAgentTurn(turn.agent_turn_id, saved ? '隐私选择已保存，监听与播放保持停止。' : '本机监听已停止，但隐私选择未保存到服务。', saved ? 'completed' : 'failed', saved ? undefined : 'PRIVACY_SAVE_FAILED', { execution_receipt: receipt, client_bridge_receipt: receipt })
        .catch(() => { if (mountedRef.current) setPrivacySaveStatus('本机监听已停止，但隐私操作回执尚未保存，请重试保存。') })
    }
  }

  const preemptPrivacyStop = (turn?: AgentTurn): void => {
    continuationDriver.interrupt()
    conversationActiveRef.current = false
    // The broadcast releases tracks and invalidates work before any persistence.
    window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))
    setReply('已停止本机监听与播放。')
    let restartProtected = true
    try { localStorage.setItem(privacyStopKey(workspace.session.session_id), 'stopped') }
    catch { restartProtected = false }
    void persistPrivacyStop(turn, restartProtected)
  }

  useEffect(() => {
    if (privacyStopPending(workspace.session.session_id)) void persistPrivacyStop()
  }, [workspace.session.session_id])

  useEffect(() => {
    let cancelled = false
    const poll = async (): Promise<void> => {
      if (nativeDialogueActiveRef.current) return
      if (
        (onboardingRef.current && onboardingRef.current.snapshot.phase !== 'idle')
        ||
        busyRef.current
        || speechSuppressedRef.current
        || ['listening', 'transcribing', 'backpressure', 'connecting'].includes(realtimeVoiceStatus.phase)
        || realtimeVoiceStatus.pendingUtterances > 0
        || Date.now() - lastSpeechRequestedAtRef.current < 15_000
      ) return
      const result = await localApi.proactiveAgentPrompt(workspace.session.session_id).catch(() => null)
      if (cancelled || nativeDialogueActiveRef.current || !result?.prompt || speechSuppressedRef.current || Date.now() - lastSpeechRequestedAtRef.current < 15_000) return
      window.dispatchEvent(new CustomEvent('hsc-agent-intervention', {
        detail: {
          message: result.prompt.message,
          sourceType: result.prompt.source_type,
          promptId: result.prompt.prompt_id,
          sessionId: workspace.session.session_id,
          childId: workspace.session.child_id,
          expiresAt: result.prompt.expires_at,
        }
      }))
    }
    const initial = window.setTimeout(() => { void poll() }, 5_000)
    const timer = window.setInterval(() => { void poll() }, 30_000)
    return () => {
      cancelled = true
      window.clearTimeout(initial)
      window.clearInterval(timer)
    }
  }, [realtimeVoiceStatus.pendingUtterances, realtimeVoiceStatus.phase, workspace.session.session_id])

  const hasVoiceInteractionContext = (): boolean => Boolean(
    wakeSettingsReadError || voiceHandoffReadUnconfirmed.current || voiceHandoffRef.current || pendingNameRef.current || editingWakeWordRef.current
    || (onboardingSupported && onboardingRef.current?.snapshot.phase !== 'idle')
    || (!onboardingSupported && !savedWakeSettingsRef.current && window.desktop?.saveWakeWordSettings)
  )

  const isLocalPrivacyStop = (value: string): boolean => {
    if (isImmediatePrivacyStop(value)) return true
    const compact = (text: string): string => text.replace(/[\s，。！？,.!?]/g, '')
    const wakeWord = compact(savedWakeSettingsRef.current?.wakeWord ?? '')
    const heard = compact(value)
    // Tutorials listen without a wake gate, but a known wake-name prefix must
    // not turn an exact privacy stop into a cloud-dependent request.
    return Boolean(wakeWord && heard.startsWith(wakeWord) && isImmediatePrivacyStop(heard.slice(wakeWord.length)))
  }

  const controlledStartSelection = (action: AgentTurn['action'], kind: 'content' | 'games'): ControlledSelectionBinding | undefined => {
    const packet = action.controlled_voice_binding
    if (!packet && action.source !== 'content_free_voice_control') return undefined
    const selected = action.selection_binding
    if (!packet || packet.child_id !== workspace.session.child_id || packet.session_id !== workspace.session.session_id
      || packet.kind !== kind || !packet.source_version || !packet.query || packet.query !== action.query
      || Boolean(selected) !== Boolean(packet.selection_binding)
      || (selected && (selected.resource_id !== packet.selection_binding?.resource_id
        || selected.source_version !== packet.selection_binding?.source_version))) {
      throw new VoiceActionUnavailableError('刚才请求的原资源或会话已变化，没有打开其他内容。请重新说明要做什么。')
    }
    return selected
  }

  const launchSelectedVideo = async (query:string, requestId:string, isCurrent:()=>boolean,
    binding?:ControlledSelectionBinding):Promise<ControlledContentPlayback> => {
    const pending = !binding && readControlledSelection(workspace.session.session_id, workspace.session.child_id)
    if (pending) throw new ApiError('尚需明确选择，没有打开首项。',409,'CONTROLLED_CONTENT_SELECTION_REQUIRED',false,undefined,{selection:pending.offer})
    const localApi = guardCommandApi(commandApi, isCurrent)
    const {intentId:nativeIntentId,nativeEpoch}=await prepareNativeLaunch('video',workspace.session.session_id,workspace.session.child_id,requestId,isCurrent)
    let playback = await localApi.startControlledContent(workspace.session.session_id, query, requestId, binding,nativeIntentId,nativeEpoch)
      .catch(error=>rejectNativeStart(error,'video',workspace.session.session_id,workspace.session.child_id,nativeIntentId))
    if (playback.session_id !== workspace.session.session_id || playback.child_id !== workspace.session.child_id) {
      throw new VoiceActionUnavailableError('观看回执归属不一致，没有打开播放器。')
    }
    if (binding && !matchesControlledSelectionReceipt(playback, binding, 'video')) {
      throw new VoiceActionUnavailableError('返回的活动与刚才选择的原资源不一致，没有打开其他内容。')
    }
    if (playback.launch_allowed === false || playback.replayed || playback.status !== 'active') {
      return playback
    }
    controlledContentRef.current = playback
    try {
      if (!window.desktop?.openControlledMedia) throw new Error('CONTROLLED_MEDIA_BRIDGE_UNAVAILABLE')
      const launched = await window.desktop.openControlledMedia({
        path: playback.local_path,
        title: playback.title,
        maxSeconds: playback.remaining_seconds,
        contentSessionId: playback.content_session_id,
        nativeIntentId,
      })
      if (launched?.opened !== true) throw new Error('CONTROLLED_MEDIA_OPEN_UNCONFIRMED')
      if (!isCurrent()) {
        controlledContentEndInFlightRef.current.add(playback.content_session_id)
        try { await confirmControlledClosure('video', 'privacy_pause', playback) }
        finally { controlledContentEndInFlightRef.current.delete(playback.content_session_id) }
        throw new SupersededDeviceCommand()
      }
      controlledContentRef.current = playback
      return playback
    } catch (cause) {
      const ending = playback
      if (!controlledContentEndInFlightRef.current.has(ending.content_session_id)) {
        controlledContentEndInFlightRef.current.add(ending.content_session_id)
        try {
          await settleControlledActivity('video', ending, 'playback_failed', 0)
          if (controlledContentRef.current === ending) controlledContentRef.current = null
        } catch {
          if (controlledContentRef.current === ending) setControlledActivityError('播放状态尚未确认，原观看记录已保留。可以说停止视频核对播放器。')
        } finally { controlledContentEndInFlightRef.current.delete(ending.content_session_id) }
      }
      throw cause
    }
  }

  const launchSelectedGame = async (query:string, requestId:string, isCurrent:()=>boolean,
    binding?:ControlledSelectionBinding):Promise<ControlledGameSession> => {
    const pending = !binding && readControlledSelection(workspace.session.session_id, workspace.session.child_id)
    if (pending) throw new ApiError('尚需明确选择，没有打开首项。',409,'CONTROLLED_GAME_SELECTION_REQUIRED',false,undefined,{selection:pending.offer})
    const localApi = guardCommandApi(commandApi, isCurrent)
    const {intentId:nativeIntentId,nativeEpoch}=await prepareNativeLaunch('game',workspace.session.session_id,workspace.session.child_id,requestId,isCurrent)
    let game = await localApi.startControlledGame(workspace.session.session_id, query, requestId, binding,nativeIntentId,nativeEpoch)
      .catch(error=>rejectNativeStart(error,'game',workspace.session.session_id,workspace.session.child_id,nativeIntentId))
    if (game.session_id !== workspace.session.session_id || game.child_id !== workspace.session.child_id) {
      throw new VoiceActionUnavailableError('游戏回执归属不一致，没有启动游戏。')
    }
    if (binding && !matchesControlledSelectionReceipt(game, binding, 'game')) {
      throw new VoiceActionUnavailableError('返回的活动与刚才选择的原资源不一致，没有打开其他内容。')
    }
    if (game.launch_allowed === false || game.replayed || game.status !== 'active') {
      return game
    }
    controlledGameRef.current = game
    try {
      if (!window.desktop?.openControlledGame) throw new Error('CONTROLLED_GAME_BRIDGE_UNAVAILABLE')
      const launched = await window.desktop.openControlledGame({
        path: game.executable_path,
        title: game.title,
        maxSeconds: game.remaining_seconds,
        gameSessionId: game.game_session_id,
        nativeIntentId,
      })
      if (launched?.opened !== true || !Number.isSafeInteger(launched.processId) || launched.processId <= 0) {
        throw new Error('CONTROLLED_GAME_OPEN_UNCONFIRMED')
      }
      game = await localApi.bindControlledGameProcess(
        workspace.session.session_id, game.game_session_id, launched.processId
      )
      controlledGameRef.current = game
      return game
    } catch (cause) {
      const ending = game
      if (!controlledGameEndInFlightRef.current.has(ending.game_session_id)) {
        controlledGameEndInFlightRef.current.add(ending.game_session_id)
        try {
          await settleControlledActivity('game', ending, 'launch_failed', 0)
          if (controlledGameRef.current === ending) controlledGameRef.current = null
        } catch {
          if (controlledGameRef.current === ending) setControlledActivityError('游戏启动状态尚未确认，原记录已保留。可以说停止游戏核对原进程。')
        } finally { controlledGameEndInFlightRef.current.delete(ending.game_session_id) }
      }
      throw cause
    }
  }

  const submit = async (spokenText: string, overlap?: PlaybackOverlap, voiceInputId?: string, pendingEvaluationTurnId?: string, capturedLocalRecoveryOnly?: boolean, capturedCarryoverConfirmationId?: string | null, capturedRealtimeReference?: VoiceCaptureReference): Promise<void> => {
    const utterance = spokenText.trim()
    if (!utterance || !mountedRef.current) return
    const carryoverArrival = voiceInputId ? capturedCarryoverConfirmationId ?? null
      : capturedCarryoverConfirmationId !== undefined ? capturedCarryoverConfirmationId
      : carryoverConfirmationAtArrival(workspace.session.session_id, workspace.session.child_id)
    const localRecoveryOnly = capturedLocalRecoveryOnly !== undefined ? capturedLocalRecoveryOnly
      : voiceInputId ? false : evaluationRecovery.invalid
    if (localRecoveryOnly && (pendingEvaluationTurnId || overlap)) {
      speakReply(LOCAL_RECOVERY_FAILURE, undefined, { localOnly: true })
      return
    }
    const pendingScope = localRecoveryOnly ? null : voiceInputId || capturedRealtimeReference
      ? pendingEvaluationTurnId ? { sessionId: workspace.session.session_id, childId: workspace.session.child_id, turnId: pendingEvaluationTurnId } : null
      : freezePendingEvaluation(pendingEvaluationRef.current)
    // Yield original audio immediately; semantic routing still owns the action.
    interruptPetListening(workspace.session.session_id)
    conversationActiveRef.current = true
    continuationDriver.interrupt()
    if (isLocalPrivacyStop(utterance)) { preemptPrivacyStop(); return }
    if (speechSuppressedRef.current) return
    if (onboardingRef.current && onboardingRef.current.snapshot.phase !== 'idle' && /^(停止|停止教程|退出教程)[。！!]?$/u.test(utterance)) {
      voiceInputQueue.cancel()
      commandGenerationRef.current += 1
      pendingNameRef.current = null
      cancelLocalSpeech()
      await onboardingRef.current?.receive('停止')
      return
    }
    // Preserve ordinary Agent queue/recovery preemption. Only the onboarding
    // transition needs to wait for cloud routing and the desktop save receipt.
    const realtimeCaptureAtArrival = realtimeSpeechCaptureRef.current
    if (!voiceInputQueue.running && !hasVoiceInteractionContext()
        && (!busyRef.current || !realtimeCaptureAtArrival)) {
      await processVoiceInput(utterance, undefined, overlap, voiceInputId, pendingScope, localRecoveryOnly, carryoverArrival)
      return
    }
    const work = voiceInputQueue.enqueue(async (signal) => {
      // The realtime provider already preserves arrival order. Keep this job
      // attached to its own reply capture until an older Agent turn has really
      // finished; otherwise the job can fall into the legacy pending queue,
      // return early, and have its eventual reply escape through another TTS.
      while (realtimeCaptureAtArrival && busyRef.current && !signal.aborted && mountedRef.current) {
        await new Promise<void>((resolve) => window.setTimeout(resolve, 20))
      }
      if (signal.aborted || !mountedRef.current || speechSuppressedRef.current) return
      setQueuedUtteranceCount(voiceInputQueue.pendingCount)
      await submitLatest.current(utterance, signal, overlap, voiceInputId, pendingScope, localRecoveryOnly, carryoverArrival)
    })
    setBusy(true)
    setQueuedUtteranceCount(voiceInputQueue.pendingCount)
    await work
    if (mountedRef.current) {
      setQueuedUtteranceCount(voiceInputQueue.pendingCount)
      setBusy(voiceInputQueue.running || busyRef.current)
    }
    continuationDriver.flush()
  }

  const processVoiceInput = async (spokenText: string, signal = voiceEventController.current.signal, overlap?: PlaybackOverlap, voiceInputId?: string, pendingScope: PendingEvaluationScope | null = null, localRecoveryOnly = false, carryoverArrival: string | null = null, prepared?: PreparedAgentTurn, providedHandoff?: OnboardingHandoffDelivery, skipMistakeWorkspace = false): Promise<void> => {
    let utterance = spokenText.trim()
    const native=prepared?.native
    const unrelatedNative=!!native&&!nativeAnswerCapabilities.has(prepared.turn.runtime_capability??'')
    let onboardingHandoff = providedHandoff
    const submittedDraftText = currentTextRef.current
    const ownsSubmittedDraft = !submittedDraftText.trim() || submittedDraftText.trim() === utterance
    const receiptCheck = !prepared && isAgentRequestCheck(utterance)
    const resumeCheck = !prepared && shouldResumeRealtimeInput(utterance, workspace.session.session_id, workspace.session.child_id)
    if (!utterance && !prepared) return
    if (!prepared) continuationDriver.interrupt()
    if (prepared && !prepared.current()) return
    if (isLocalPrivacyStop(utterance)) { preemptPrivacyStop(); return }
    if (!mountedRef.current || speechSuppressedRef.current) return
    const realtimeInputSource = realtimeSpeechCaptureRef.current?.inputSource
    if (!prepared && realtimeInputSource) {
      const sourceStatus = realtimeInputSourceStatus(realtimeInputSource, workspace.session.session_id, workspace.session.child_id)
      if (sourceStatus !== 'current') {
        preserveInputRef.current = true
        speakReply(realtimeInputSourceMessage(sourceStatus), undefined, { localOnly: true })
        return
      }
    }
    if(!skipMistakeWorkspace&&!prepared&&!overlap&&!localRecoveryOnly&&!pendingScope&&voiceInputId&&onMistakeWorkspaceVoice?.(
      mistakeWorkspaceVoiceCommand(utterance,wakeSettings?.wakeWord),
      {transcript:utterance,voiceInputId,capturedAt:realtimeInputSource?.capturedAt??Date.now(),
        onSafety:()=>{void processVoiceInput(utterance,signal,overlap,voiceInputId,pendingScope,localRecoveryOnly,carryoverArrival,prepared,providedHandoff,true)}},
    ))return
    if (!receiptCheck && !resumeCheck && !localRecoveryOnly && !prepared && !overlap && !onboardingHandoff
      && !isVoiceInteractionRetry(utterance) && (voiceHandoffRef.current || voiceHandoffReadUnconfirmed.current)) {
      try { await invalidateVoiceHandoff(signal) }
      catch {
        if (signal.aborted || !mountedRef.current || speechSuppressedRef.current) return
        preserveInputRef.current = true
        if (!unprocessedVoiceInputs.current.includes(utterance)) unprocessedVoiceInputs.current.push(utterance)
        setText(unprocessedVoiceInputs.current.join('\n'))
        const message = '原学习要求的交接状态还没能核对，新要求尚未提交。请稍后再说一次。'
        setReply(message); speakReply(message, undefined, { localOnly: true })
        return
      }
      if (signal.aborted || !mountedRef.current || speechSuppressedRef.current) return
    }
    if (!receiptCheck && !resumeCheck && !localRecoveryOnly && !prepared && !overlap && !onboardingHandoff && hasVoiceInteractionContext()) {
      const consumed = await consumeTutorial(utterance, signal, voiceInputId)
      if (consumed === true) return
      if (consumed) { utterance = consumed.transcript; voiceInputId = consumed.voiceInputId; onboardingHandoff = consumed.handoff }
    }
    if (signal.aborted || !mountedRef.current || speechSuppressedRef.current) return
    if (evaluationRecovery.invalid && !unrelatedNative && !localRecoveryOnly && !receiptCheck && !resumeCheck) {
      const message = '上次作答评价的恢复记录无法读取，刚才的话没有提交。请家长核对本机记录，恢复后再说一次。'
      setEvaluationStatus(message)
      speakReply(message, undefined, { localOnly: true })
      return
    }
    if (pendingEvaluationRef.current && !unrelatedNative && !pendingScope && !localRecoveryOnly && !receiptCheck && !resumeCheck) {
      preserveInputRef.current = true
      setEvaluationStatus('这句话排队后出现了新的待确认评价，未把它绑定到另一份作答。原话已保留，请核对原评价后再继续。')
      return
    }
    if (pendingScope) {
      if (pendingScope.sessionId !== workspace.session.session_id || pendingScope.childId !== workspace.session.child_id) return
      if (/^(核对评价状态|核对原记录|查询作答状态)[。！!]?$/u.test(utterance)) {
        if (pendingEvaluationRef.current?.turnId === pendingScope.turnId) {
          setEvaluationCheckSignal((value) => value + 1)
          speakReply('我会核对原作答记录，不会重新提交答案或重复评分。', undefined, { localOnly: true })
          return
        }
      }
    }
    if(!native)cancelLocalSpeech()
    if (busyRef.current) {
      if (prepared) return
      pendingUtterancesRef.current.push({ text: utterance, overlap, voiceInputId, pendingScope, localRecoveryOnly, carryoverArrival, handoff: onboardingHandoff })
      const queued = pendingUtterancesRef.current.length
      if (mountedRef.current) {
        setQueuedUtteranceCount(queued)
        setReply(`听到了，已按顺序排队${queued}句。我会逐句处理，不会漏掉。`)
      }
      return
    }
    const recoveryCommand = native||localRecoveryOnly ? null : tutorRecoveryCommand(utterance)
    if (pendingTutorCompletionRef.current && recoveryCommand) { await retryTutorCompletion(); return }
    if (tutorRecovery) {
      if (recoveryCommand === 'keep') {
        await abandonUnknownTutorRecovery(tutorRecovery)
        return
      }
      if (recoveryCommand === 'confirm') {
        await finishTutorRecovery(tutorRecovery, true)
        return
      }
      if (recoveryCommand === 'retry') {
        if (tutorRecovery.requiresExplicitRetryConfirmation) {
          const message = '这次重试可能重复调用模型。请明确说“确认再次请求”，或者说“保留问题重新问”。'
          setReply(message)
          if (!speechSuppressedRef.current) speakReply(message)
        } else await finishTutorRecovery(tutorRecovery)
        return
      }
    }
    if (!tutorRecovery && recoveryCommand && workspace.session.state === 'study') {
      const signal = voiceEventController.current.signal
      try {
        if (await requestOwnedVoiceEvent('hsc-study-tutor-recovery', {
          sessionId: workspace.session.session_id, childId: workspace.session.child_id, command: recoveryCommand, signal,
        }, signal)) return
      } catch (cause) {
        if (signal.aborted || (cause instanceof DOMException && cause.name === 'AbortError')) return
        throw cause
      }
      if (!mountedRef.current || speechSuppressedRef.current) return
    }
    preserveInputRef.current = unprocessedVoiceInputs.current.length > 0
    busyRef.current = true
    setBusy(true)
    let persistedTurn: AgentTurn | null = null
    let resumedAdmission: RealtimeAdmissionHandoff | undefined
    let outcomeOwnedByRuntime = false
    let clientCompletionSubmitted = false
    let diagnosticReplayReadFailed = false
    let afterSpeech: (() => void) | undefined
    let documentGoalToContinue: string | undefined
    const generation = commandGenerationRef.current
    const isCurrent = (): boolean => mountedRef.current && generation === commandGenerationRef.current && (!prepared || prepared.current())
    const localApi = guardCommandApi(commandApi, isCurrent)
    try {
      if (receiptCheck) {
        let message: string | null
        try {
          message = await withReceiptRecoveryWindow(isCurrent, signal, async recovery => {
            const index = await recovery.read(signal => commandApi.agentExecutionRecoveryIndex(
              workspace.session.session_id, workspace.session.child_id, signal))
            if (!recovery.current()) return null
            restoreAgentExecutionIdentities(workspace.session.session_id, workspace.session.child_id, index)
            const realtime = await checkRealtimeAdmissions(workspace.session.session_id, workspace.session.child_id,
              commandApi.realtimeAdmissionReceipt, commandApi.agentRequestReceipt, recovery.current,
              realtimeSpeechCaptureRef.current?.inputSource?.localInputId, recovery)
            if (!recovery.current()) return null
            const agent = !realtime || restoredAgentRequestMessage(workspace.session.session_id)
              ? await checkAgentRequests(workspace.session.session_id, commandApi.agentRequestReceipt, recovery.current, recovery) : null
            return [realtime, agent, index.has_more ? '还有原操作未全部核对，请家长检查待完成记录。' : null].filter(Boolean).join(' ') || null
          })
        }
        catch (error) {
          message = '原请求核对没有全部完成，未确认的恢复编号仍保留在本机，没有重新提交。可以稍后继续核对。'
          if (isCurrent() && !signal.aborted) void window.desktop?.recordRendererDiagnostic?.({
            event: 'voice.recovery.receipt_check_incomplete', level: 'warn',
            message: 'Read-only recovery incomplete; unconfirmed identities retained.',
            details: { reason: error instanceof Error && 'code' in error && error.code === 'VOICE_RECEIPT_CHECK_TIMEOUT' ? 'deadline' : 'read_failed' },
          })
        }
        if (message && isCurrent() && !signal.aborted) {
          setReply(message)
          if (!speechSuppressedRef.current) speakReply(message, undefined, { localOnly: true })
        }
        return
      }
      let resumedTurn: AgentTurn | undefined
      if (resumeCheck) {
        const resumed = await resumeOneUnsentInput(workspace.session.session_id, workspace.session.child_id,
          localApi.realtimeResumePreview, localApi.resumeRealtimeVoice, isCurrent, signal, realtimeInputSource?.localInputId)
        if (!isCurrent() || signal.aborted) return
        if (!resumed.turn) {
          const message = resumed.message ?? '原话的续接已停止，原编号仍保留。'
          setReply(message)
          speakReply(message, undefined, { localOnly: true })
          return
        }
        resumedTurn = resumed.turn
        resumedAdmission = resumed.handoff
        pendingScope = resumed.pendingEvaluationTurnId ? { sessionId: workspace.session.session_id,
          childId: workspace.session.child_id, turnId: resumed.pendingEvaluationTurnId } : null
        utterance = resumedTurn.transcript
      }
      // A private, already-offered catalog selection never enters general
      // planning. It is a fresh exact start request, not a fabricated AgentTurn.
      if (!prepared && !resumedTurn && !localRecoveryOnly && !pendingScope && !pendingEvaluationRef.current) {
        const selection = readControlledSelection(workspace.session.session_id, workspace.session.child_id)
        if (selection) {
          const originalSelection = captureControlledSelection(workspace.session.session_id, workspace.session.child_id)
          const meaning = resolveControlledSelection(utterance, selection.offer)
          const genericSelection = /^(那|就|请|我想|先)?(视频|游戏|来一个|随便|任意|选一个|选哪个|那个|这个)[吧。！!？?]?$/u.test(utterance.trim())
          if (meaning.type !== 'other' || genericSelection) {
            let message = selectionPrompt(selection.offer)
            if (meaning.type === 'ordinal') {
              message = '刚才的编号可能对应另一份列表，我不会按现在的顺序打开。'+selectionPrompt(selection.offer)+'请说想选的完整名称。'
            } else if (selection.cancelled) {
              message = '上次选择已取消，不会重试打开。尚未确认的原活动可以说停止视频或停止游戏来核对。'
            } else if (meaning.type === 'cancel') {
              if (selection.request) {
                saveControlledSelection({...selection,cancelled:true},originalSelection)
                message = '已取消重试打开。上次打开的回执还未确认，可以先说停止视频或停止游戏，核对原活动。'
              }
              else { removeControlledSelection(originalSelection); message = '好，先不选。没有启动活动，学习进度保持原样。' }
            } else if (meaning.type === 'choose' || meaning.type === 'retry' && selection.request) {
              if (selection.request && meaning.type === 'choose'
                && selection.request.binding.resource_id !== meaning.choice.resource_id) {
                message = '上一次打开还没确认，先核对原记录，不会改开另一个。'
              } else {
                const request = selection.request ?? (meaning.type === 'choose' ? {
                  query:meaning.choice.title, clientActionId:crypto.randomUUID(),
                  binding:{resource_id:meaning.choice.resource_id,source_version:meaning.choice.source_version}
                } : undefined)
                if (request) {
                  const savedSelection=saveControlledSelection({...selection,request},originalSelection)
                  const selectionCurrent=():boolean=>isCurrent()&&isControlledSelectionCurrent(savedSelection)
                  try {
                    const result = selection.offer.kind === 'video'
                      ? await launchSelectedVideo(request.query, request.clientActionId, selectionCurrent, request.binding)
                      : await launchSelectedGame(request.query, request.clientActionId, selectionCurrent, request.binding)
                    if (!selectionCurrent()) throw new SupersededDeviceCommand()
                    removeControlledSelection(savedSelection)
                    await run(async () => undefined)
                    message = result.launch_allowed === false || result.replayed || result.status !== 'active'
                      ? '已核对上次打开的原记录，没有重新播放或启动。'
                      : `已打开“${result.title}”，最多${result.allowed_minutes}分钟。随时可以说停止。`
                  } catch (cause) {
                    if (!selectionCurrent() || cause instanceof SupersededDeviceCommand) throw cause
                    if (cause instanceof ApiError && cause.status === 409) {
                      removeControlledSelection(savedSelection)
                      message = '刚才选择的原资源、许可或可用时长发生了变化，没有改开其他内容。请先核对当前许可，再提出新的请求。'
                    } else message = cause instanceof ControlledStartRecoveryError ? cause.message
                      : '这次打开还没有确认，原请求已在本机保留。可以说“重试打开”核对原请求，不会另选一项。'
                  }
                }
              }
            }
            if (isCurrent()) {
              setReply(message); setText('')
              if (!speechSuppressedRef.current) speakReply(message, undefined, {localOnly:true})
            }
            return
          }
        }
      }
      // Keep the ID even when a preempted network request returns late, so it
      // can be marked cancelled without executing its suggested device action.
      const realtimeActionId = realtimeInputSource && voiceInputId ? 'realtime-' + voiceInputId : undefined
      const turn = resumedTurn ?? prepared?.turn ?? (onboardingHandoff ? await commandApi.dispatchVoiceInteractionHandoff(workspace.session.session_id, workspace.session.child_id, onboardingHandoff.handoffId, onboardingHandoff.agentActionId, signal)
        : realtimeActionId ? await commandApi.agentTurn(workspace.session.session_id, utterance,
          localRecoveryOnly ? undefined : overlap?.playback_id, voiceInputId,
          localRecoveryOnly ? undefined : pendingScope?.turnId, localRecoveryOnly ? true : undefined, realtimeActionId)
        : localRecoveryOnly ? await commandApi.agentTurn(workspace.session.session_id, utterance, undefined, voiceInputId, undefined, true)
        : pendingScope ? await commandApi.agentTurn(workspace.session.session_id, utterance, overlap?.playback_id, voiceInputId, pendingScope.turnId)
        : voiceInputId ? await commandApi.agentTurn(workspace.session.session_id, utterance, overlap?.playback_id, voiceInputId)
        : overlap ? await commandApi.agentTurn(workspace.session.session_id, utterance, overlap.playback_id)
        : await commandApi.agentTurn(workspace.session.session_id, utterance))
      persistedTurn = turn
      if (!isCurrent()) throw new SupersededDeviceCommand()
      if (onboardingHandoff) {
        voiceHandoffEpoch.current += 1
        voiceHandoffRef.current = null
        voiceHandoffReadUnconfirmed.current = false
      }
      setVoiceTaskChoiceCapture(workspace.session.session_id, workspace.session.child_id, null)
      if (localRecoveryOnly) {
        outcomeOwnedByRuntime = true
        if (!acceptsLocalRecoveryControl(turn, workspace.session.session_id, workspace.session.child_id)
          || turn.agent_turn_id === pendingEvaluationRef.current?.turnId) throw new VoiceActionUnavailableError(LOCAL_RECOVERY_FAILURE)
      } else if (pendingScope) {
        outcomeOwnedByRuntime = true
        if (!acceptsPendingEvaluationControl(turn, pendingScope)) {
          throw new VoiceActionUnavailableError('这次回复未通过原评价的非评分约束核对，没有执行新评分或覆盖原记录。请先核对原评价。')
        }
      } else if (pendingEvaluationRef.current&&!unrelatedNative) {
        outcomeOwnedByRuntime = true
        throw new VoiceActionUnavailableError('请求等待期间出现了待确认评价，没有执行新的作答动作。原记录已保留。')
      }
      if (turn.intent === 'companion_activity_reply') {
        await refreshActivityFocus(isCurrent)
        if (!isCurrent()) throw new SupersededDeviceCommand()
      }
      let response = turn.response_text
      const speechPolicy = agentReplySpeechPolicy(turn)
      let shouldSpeak = !native&&turn.action.suppress_speech !== true
      let completedByRuntime = false
      let followupOwnsContinuation = false
      let clientToolResult: unknown = null
      const action = turn.action
      assertSupportedAgentAction(action.type)
      if (pendingScope && action.pending_evaluation_query) {
        if (pendingEvaluationRef.current?.turnId === pendingScope.turnId) {
          setEvaluationCheckSignal(value => value + 1)
        } else {
          const status = await localApi.agentTurnExecutionStatus(pendingScope.turnId)
          if (!isCurrent()) throw new SupersededDeviceCommand()
          const saved = status.execution?.turn
          if (status.status === 'completed' && (!saved || saved.agent_turn_id !== pendingScope.turnId
            || saved.session_id !== pendingScope.sessionId || saved.child_id !== pendingScope.childId
            || status.execution?.execution.capability !== 'learning.evaluate_response')) {
            throw new VoiceActionUnavailableError('返回的原评价与当前孩子或会话不一致，未采纳，也未重复评价。')
          }
          response = status.status === 'completed' ? '原作答评价已保存，没有再次提交或重复评分。'
            : status.status === 'in_progress' ? '原作答评价仍在处理中，没有再次提交。' : '原作答结果仍未确认，原记录已保留，没有重复评价。'
        }
      }
      if (pendingScope && (action.type !== 'none' || isExplicitEvaluationTopicChange(utterance))
        && pendingEvaluationRef.current?.turnId === pendingScope.turnId) {
        try { keepPendingEvaluation({ ...pendingEvaluationRef.current, silent: true }) }
        catch {
          const localSafety = action.local_only === true && action.cloud_transmission_allowed === false
            && ['urgent_safety_support', 'high_risk_child_safety', 'emotional_distress_support'].includes(turn.intent)
            && ['none', 'find_parent', 'find_parent_urgent'].includes(action.type)
          if (!localSafety) throw new VoiceActionUnavailableError('原评价的恢复记录未能保存，未继续执行新的控制。')
          setEvaluationStatus('原评价恢复记录未能更新，原记录未删除；安全求助继续单独处理。')
        }
      }
      if(native){await native.port.claim();if(!isCurrent())throw new SupersededDeviceCommand()}
      if (!native&&turn.requires_control_admission) {
        const admission = await localApi.admitVoiceControlExecution(workspace.session.session_id, turn.agent_turn_id)
        if (admission.status !== 'admitted') {
          outcomeOwnedByRuntime = true
          throw new VoiceActionUnavailableError('刚才的控制请求已变化，或上次操作的结果还未确认。我没有重复操作设备。')
        }
      }
      if (prepared && !native && ['client_device_bridge', 'local_model'].includes(turn.runtime_execution_location ?? action.execution_location ?? '')) {
        const admission = await localApi.admitContinuationExecution(workspace.session.session_id, turn.agent_turn_id)
        if (admission.status !== 'admitted') {
          outcomeOwnedByRuntime = true
          throw new VoiceActionUnavailableError('这一步的执行资格已经失效，或上次结果还在核对；我没有再次操作设备或重新讲题。')
        }
      }
      if (completionRestore.invalid && ['ask_tutor', 'more_tutor_hint', 'record_tutor_feedback'].includes(action.type)) {
        throw new VoiceActionUnavailableError('本机讲解结果恢复记录未通过核对，暂不重复执行。请家长检查本机存储。')
      }
      if (['pause_observation', 'resume_observation', 'configure_wake_word', 'query_device_status', 'connect_devices'].includes(action.type)
        && (turn.runtime_execution_location ?? action.execution_location) !== 'client_device_bridge') {
        throw new VoiceActionUnavailableError('设备动作没有本机执行契约，尚未确认执行。')
      }
      if (action.type === 'resume_voice_tutorial'
        && ((turn.runtime_capability ?? action.capability) !== 'onboarding.resume'
          || (turn.runtime_execution_location ?? action.execution_location) !== 'client_device_bridge')) {
        throw new VoiceActionUnavailableError('教程重启动作的执行契约不一致，尚未修改教程。')
      }
      const runTool = async (tool: () => Promise<unknown>, recovery?: Omit<TutorToolRecovery, 'version' | 'createdAt' | 'attempts'>): Promise<boolean> => {
        let toolError: unknown
        const succeeded = await run(async () => {
          try { clientToolResult = await tool() }
          catch (cause) { toolError = cause; throw cause }
        })
        if (!isCurrent()) throw new SupersededDeviceCommand()
        if (succeeded) return true
        if (['start_controlled_content', 'start_controlled_game'].includes(action.type)) {
          const offer = offerFromSelectionError(toolError,
            action.type === 'start_controlled_content' ? 'video' : 'game', workspace.session.session_id, workspace.session.child_id)
          if (offer) {
            const existing = readControlledSelection(workspace.session.session_id,workspace.session.child_id)
            const selection:PendingControlledSelection = existing ?? {version:1,offer,rootTurnId:turn.agent_turn_id,
              local_only:true,cloud_transmission_allowed:false}
            saveControlledSelection(selection)
            // Only a title-free status goes into the old generic turn. The
            // source catalog and its next request stay in local UI recovery.
            await localApi.completeAgentTurn(turn.agent_turn_id, '尚未启动活动，需要明确选择。', 'failed', 'CONTROLLED_SELECTION_REQUIRED').catch(() => undefined)
            if (isCurrent()) {
              const message = selection.request
                ? '上次打开的回执还未确认，不会另开一项。可以说重试打开核对原请求，或停止视频、停止游戏。'
                : selectionPrompt(selection.offer)
              setReply(message); setText('')
              if (!speechSuppressedRef.current) speakReply(message, undefined, {localOnly:true})
            }
            return false
          }
        }
        if (recovery) {
          const savedRecovery: TutorToolRecovery = { ...recovery, version: 1, createdAt: new Date().toISOString(), attempts: 1 }
          setTutorRecovery(savedRecovery)
          await draft.saveNow({ text, tutor_recovery: savedRecovery })
        }
        const endFailure = ['stop_controlled_content', 'stop_controlled_game'].includes(action.type)
          && (toolError instanceof VoiceActionUnavailableError || toolError instanceof PendingControlledEndError) ? toolError.message : null
        const originalPageFailure = action.type === 'control_document_monitor' && action.command === 'review'
          && action.voice_input_binding?.question_reference?.kind === 'document'
          ? '原页交接没有确认保存，原话和原页来源仍保留。我没有改用当前页面，也没有开始讲解或评分。' : null
        const failureResponse = originalPageFailure ?? endFailure ?? (toolError instanceof ControlledStartRecoveryError ? toolError.message
          : recovery ? tutorFailureMessage(1) : '这一步没有确认保存，当前记录会保留。请再说一次刚才的请求；如果一直不行，请家长帮忙。')
        await localApi.completeAgentTurn(turn.agent_turn_id, failureResponse, 'failed',
          toolError instanceof ControlledStartRecoveryError && ['start_controlled_content','start_controlled_game'].includes(action.type)
            ? 'CONTROLLED_START_NATIVE_UNCONFIRMED' : 'CLIENT_TOOL_ACTION_FAILED').catch(() => undefined)
        if (pendingUtterancesRef.current.length === 0 && voiceInputQueue.pendingCount === 0 && mountedRef.current) {
          setReply(failureResponse)
          if (!speechSuppressedRef.current) speakReply(failureResponse, undefined, { localOnly: speechPolicy.localOnly })
        }
        return false
      }
      const followWritingFeedback = async (notice?: import('./types').WritingFeedbackNotice): Promise<void> => {
        const held = writingFeedbackHoldMessage(notice)
        if (held) {
          if (!isCurrent()) throw new VoiceActionUnavailableError('原回答已经保存，没有继续旧话题。')
          response = held
          speechPolicy.localOnly = true
          followupOwnsContinuation = true
          afterSpeech = undefined
          return
        }
        const feedback = await continueWritingFeedback({ notice, sourceTurnId: turn.agent_turn_id,
          childId: workspace.session.child_id, sessionId: workspace.session.session_id, isCurrent,
          execute: id => localApi.executeAgentTurn(id),
          recover: async id => {
            const recovered = await localApi.agentTurnExecutionStatus(id)
            return recovered.status === 'completed' ? recovered.execution ?? null : null
          },
          beforeEvaluation: id => {
            if (pendingEvaluationRef.current && pendingEvaluationRef.current.turnId !== id) {
              throw new VoiceActionUnavailableError('原作答评价尚未确认，未重复启动评价。')
            }
            keepPendingEvaluation({version:1, sessionId:workspace.session.session_id, childId:workspace.session.child_id,
              turnId:id, status:'in_progress', silent:false})
          },
          finishEvaluation: finishPendingEvaluation,
        })
        if (feedback) {
          followupOwnsContinuation = true
          const replayOnly = feedback.execution.replay_audio_allowed === false
          afterSpeech = !native && !replayOnly && feedback.turn.after_execution === 'replan' ? scheduleContinuation(feedback.turn.agent_turn_id) : undefined
          response = feedback.execution.response_text
          speechPolicy.localOnly = true
          speechPolicy.sourceAgentTurnId = feedback.turn.agent_turn_id
          if (replayOnly) shouldSpeak = false
          else if (feedback.execution.tutor_turn) {
            await presentGlobalTutorTurn(feedback.execution.tutor_turn, {localOnly:true,isCurrent,sourceAgentTurnId:feedback.turn.agent_turn_id})
            shouldSpeak = false
          }
        }
      }
      if ((turn.intent === 'companion_activity_reply' || pendingScope || localRecoveryOnly) && action.type === 'none'
        && turn.tool_status === 'completed' && turn.runtime_capability === 'conversation.respond'
        && turn.runtime_execution_location === 'server') {
        // This reply and its runtime receipt were committed by POST agent-turn.
        outcomeOwnedByRuntime = true
        completedByRuntime = true
        await refreshAgentRuntime()
      } else if (
        (turn.runtime_execution_location === 'server'
          || (!!native&&turn.runtime_capability==='learning.evaluate_response')
          || (action.source?.startsWith('agent_runtime_') && (action.execution_location === 'server' || action.capability === 'learning.evaluate_response')))
      ) {
        outcomeOwnedByRuntime = true
        if (action.capability === 'learning.evaluate_response' || turn.runtime_capability === 'learning.evaluate_response') {
          if (pendingEvaluationRef.current && pendingEvaluationRef.current.turnId !== turn.agent_turn_id) {
            setEvaluationStatus('原作答评价尚未确认，已保留原记录，不会启动第二次评价。')
            return
          }
          keepPendingEvaluation({ version: 1, sessionId: workspace.session.session_id, childId: workspace.session.child_id, turnId: turn.agent_turn_id, status: 'in_progress', silent: false })
        }
        const execution: { value: Awaited<ReturnType<typeof localApi.executeAgentTurn>> | null } = { value: null }
        execution.value = await localApi.executeAgentTurn(turn.agent_turn_id)
        if (!isCurrent()) throw new SupersededDeviceCommand()
        if (!execution.value) return
        clientToolResult=execution.value.execution.result
        if (execution.value.execution.replay_audio_allowed === false) shouldSpeak = false
        const resourceReceipt = execution.value.execution.result as { status?: string; task_resource_gap?: unknown } | null
        const unavailableOriginal = ['task.start', 'task.resume'].includes(execution.value.execution.capability)
          && resourceReceipt?.status === 'awaiting_resources' && Boolean(resourceReceipt.task_resource_gap)
        if (action.source === 'task_navigation_clarification' || unavailableOriginal) {
          const offer = await trackTaskChoiceCapture(workspace.session.session_id, workspace.session.child_id,
            () => localApi.taskStartChoice(workspace.session.session_id))
          if (!isCurrent()) throw new SupersededDeviceCommand()
          const recoveryOffer = action as unknown as { task_start_offer?: unknown; task_source_gap?: unknown }
          if ((recoveryOffer.task_start_offer || recoveryOffer.task_source_gap || unavailableOriginal)
              && offer.reference?.offer_turn_id !== turn.agent_turn_id) {
            setVoiceTaskChoiceCapture(workspace.session.session_id, workspace.session.child_id, null)
            throw new VoiceActionUnavailableError('刚才的任务或原题状态已变化，我没有按旧提示操作；请重新说明想处理哪项任务。')
          }
          speechPolicy.localOnly = true
        }
        if (execution.value.execution.capability === 'learning.review_schedule') {
          const receipt = execution.value.execution.result as { root_turn_id?: string; revision?: number }
          if (receipt.root_turn_id && Number.isInteger(receipt.revision)) {
            continuationHandledRef.current.add(`document-review:${receipt.root_turn_id}:${receipt.revision}`)
          }
          speechPolicy.localOnly = true
        }
        let examResponse: string | undefined
        if (['exam.start', 'exam.submit', 'exam.resume', 'exam.pause', 'exam.cancel'].includes(execution.value.execution.capability)) {
          try {
            const exam = await consumeExamListeningExecution({
              sessionId: workspace.session.session_id, childId: workspace.session.child_id, turnId: turn.agent_turn_id,
              execution: execution.value.execution, isCurrent, signal: voiceEventController.current.signal,
              currentQuestion: voiceCaptureReference(workspace.session.session_id, workspace.session.child_id).question_reference,
              currentViewRunId: examListeningView?.runId,
              capture: binding => setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
                binding ? { kind: 'exam', binding } : null),
              present: view => setExamListeningView(view),
            })
            if (!isCurrent()) throw new SupersededDeviceCommand()
            examResponse = exam.response
          } catch (cause) {
            if (cause instanceof SupersededDeviceCommand) throw cause
            throw new VoiceActionUnavailableError('考试操作已保存，但当前原音和题目归属尚未核对一致。没有重复播放或提交答案；请先确认当前考试进度，再说“继续模拟卷”。')
          }
          speechPolicy.localOnly = true
        }
        const writingFollowup = ['writing.revise_original', 'writing.independent_check'].includes(execution.value.execution.capability)
        const oralFollowup = ['oral.retry', 'oral.independent_check'].includes(execution.value.execution.capability)
        const oralReceipt = execution.value.execution as { replayed?: boolean; recorded_receipt?: boolean; replay_audio_allowed?: boolean }
        const oralReplay = oralFollowup && (oralReceipt.replayed === true || oralReceipt.recorded_receipt === true
          || oralReceipt.replay_audio_allowed === false)
        const taskExecution = ['task.start', 'task.resume'].includes(execution.value.execution.capability)
        const taskReceipt = execution.value.execution.result as { status?: string; source_type?: string; replayed?: boolean; recorded_receipt?: unknown } | null
        const taskReplay = taskExecution && Boolean(taskReceipt?.replayed || taskReceipt?.recorded_receipt
          || oralReceipt.replayed || oralReceipt.recorded_receipt || oralReceipt.replay_audio_allowed === false)
        const taskMistake = taskExecution && !taskReplay
          ? (execution.value.execution.result as { mistake_dialogue?: import('./types').MistakeDialogue } | null)?.mistake_dialogue
          : undefined
        const reviewOwnsTaskPlayback = taskExecution && taskReceipt?.source_type === 'due_review' && taskReceipt.status === 'active'
        const resumeReplay = execution.value.execution.capability === 'task.resume'
          && Boolean(taskReceipt?.replayed || taskReceipt?.recorded_receipt)
        const taskPrompt = taskExecution && !resumeReplay
          ? (execution.value.execution.result as { diagnostic_prompt?: import('./types').DiagnosticPromptResult } | null)?.diagnostic_prompt
          : undefined
        const documentReplay = execution.value.execution.capability === 'learning.review_revisit'
          && Boolean(taskReceipt?.replayed || taskReceipt?.recorded_receipt
            || oralReceipt.replayed || oralReceipt.recorded_receipt || oralReceipt.replay_audio_allowed === false)
        const documentPrompt = execution.value.execution.capability === 'learning.review_revisit' && !documentReplay
          ? (execution.value.execution.result as { diagnostic_prompt?: import('./types').DiagnosticPromptResult } | null)?.diagnostic_prompt
          : undefined
        let taskPromptResponse: string | undefined
        if (writingFollowup || (oralFollowup && !oralReplay) || taskPrompt || documentPrompt) {
          const revised = taskPrompt ?? documentPrompt ?? execution.value.execution.result as import('./types').DiagnosticPromptResult | null
          if (revised?.status === 'awaiting_task_start') {
            if (!revised.prompt || revised.prompt.child_id !== workspace.session.child_id || revised.prompt.origin_session_id !== workspace.session.session_id) {
              throw new VoiceActionUnavailableError('原任务题目与当前孩子或会话不一致，未更改其他题目。')
            }
            clearDiagnosticCaptureForPrompt(workspace.session.session_id, revised.prompt)
            window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated',
              { detail: { sessionId: workspace.session.session_id, result: revised } }))
            taskPromptResponse = taskReceipt?.status === 'awaiting_resources'
              ? execution.value.execution.response_text || '原题所需材料尚未齐备，暂未开始计时。请家长先核对材料。'
              : DIAGNOSTIC_TASK_START_MESSAGE
          }
          if (revised?.status === 'waiting_answer' && revised.prompt) {
            if (revised.prompt.child_id !== workspace.session.child_id || revised.prompt.origin_session_id !== workspace.session.session_id) {
              throw new VoiceActionUnavailableError('计划题目与当前孩子或会话不一致，没有切换作答目标。')
            }
            setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
              {kind:'diagnostic',binding:{prompt_id:revised.prompt.prompt_id,item_version:revised.prompt.item_version}})
            publishOriginalAudioCaptureHint(workspace.session.session_id, revised)
            Object.assign(speechPolicy, originalAudioPromptPlayback(workspace.session.session_id, revised, speechPolicy))
            window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated',
              {detail:{sessionId:workspace.session.session_id,result:revised}}))
          }
          if (documentPrompt?.status === 'answered' && documentPrompt.prompt) {
            if (documentPrompt.prompt.child_id !== workspace.session.child_id || documentPrompt.prompt.origin_session_id !== workspace.session.session_id) {
              throw new VoiceActionUnavailableError('补充作答与当前孩子或会话不一致，没有更改当前题目。')
            }
            setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
              { kind: 'diagnostic', binding: { prompt_id: documentPrompt.prompt.prompt_id, item_version: documentPrompt.prompt.item_version } })
            window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated',
              { detail: { sessionId: workspace.session.session_id, result: documentPrompt } }))
          }
          speechPolicy.localOnly = true
          // The active review panel owns its original prompt and playback across
          // refresh. Starting it here too would consume the same recording twice.
          if (!reviewOwnsTaskPlayback && taskPrompt?.status !== 'awaiting_task_start' && taskPrompt?.prompt && isPetListening(taskPrompt.prompt)) {
            // The task receipt already owns this prompt. Never prepare another
            // question or use the server's text response as a listening stimulus.
            taskPromptResponse = '这道原题已不在等待作答，先核对原记录，不重复播放或计算听力成绩。'
            if (taskPrompt.status === 'waiting_answer') {
              try {
                const playback = await playPetListening(workspace.session.session_id, taskPrompt.prompt,
                  turn.agent_turn_id, isCurrent, voiceEventController.current.signal)
                taskPromptResponse = playback.response
              } catch {
                taskPromptResponse = '任务启动记录已保留，但原音播放尚未确认，暂时不计听力成绩。可以说“继续听题”核对原播放；不会用朗读原文代替，也没有重新启动任务。'
              }
              if (!isCurrent()) throw new SupersededDeviceCommand()
            }
          }
        }
        const delivery = evaluationFeedbackPolicy(execution.value.execution.result)
        if (execution.value.execution.capability === 'learning.evaluate_response' && !delivery.deliver) {
          finishPendingEvaluation(turn.agent_turn_id)
          preserveInputRef.current = true
          setEvaluationStatus('旧话题的评价结果已保留为证据，不会恢复旧目标或继续旧追问。')
          return
        }
        finishPendingEvaluation(turn.agent_turn_id)
        if (!await run(async () => undefined)) return
        if (!isCurrent()) throw new SupersededDeviceCommand()
        setEvaluationStatus(null)
        if(native&&!taskReplay&&!resumeReplay&&!documentReplay&&(!!taskPromptResponse||!!examResponse||documentPrompt||['learning.explain','learning.evaluate_response'].includes(execution.value.execution.capability)))shouldSpeak=turn.action.suppress_speech!==true
        response = examResponse ?? taskPromptResponse ?? (documentReplay || taskReplay || taskMistake ? execution.value.execution.response_text : execution.value.turn.response_text)
        speechPolicy.localOnly ||= agentReplySpeechPolicy(execution.value.turn).localOnly
        if (execution.value.execution.capability === 'learning.evaluate_response') speechPolicy.localOnly ||= delivery.localOnly
        completedByRuntime = true
        // Server execution owns persistence. Refresh its projection without
        // issuing the action again, even if this read fails.
        await run(async () => undefined)
        if (taskReplay || resumeReplay || reviewOwnsTaskPlayback || oralReplay || documentReplay) shouldSpeak = false
        let mistakeTeachingCurrent = true
        if (execution.value.execution.capability.startsWith('mistake.') || taskMistake) {
          const focus = await localApi.mistakeDialogue(workspace.session.session_id).catch(() => ({ dialogue: null }))
          if (!isCurrent()) throw new SupersededDeviceCommand()
          const result = execution.value.execution.result as import('./types').MistakeDialogue | undefined
          const repair = result?.state?.question_confirmation
          if (repair?.mode === 'source_repair') {
            const current = focus.dialogue
            const pending = current?.state.question_confirmation
            speechPolicy.localOnly = true
            if (!current || current.session_id !== workspace.session.session_id || current.child_id !== workspace.session.child_id
              || current.mistake_id !== result?.mistake_id || ['paused', 'closed'].includes(current.phase)
              || current.state.foundation_hold || pending?.mode !== 'source_repair'
              || pending.review_id !== repair.review_id || pending.version !== repair.version
              || pending.question !== repair.question || pending.original_answer !== repair.original_answer
              || pending.replacement_answer !== repair.replacement_answer) {
              throw new VoiceActionUnavailableError('刚才的题干核对已变化，旧回复保留，不再读回旧版本。请告诉我当前要更正的内容。')
            }
          }
          if (taskMistake) {
            const current = focus.dialogue
            const actual = current?.state.next_activity
            const expected = taskMistake.state.next_activity
            if (!current || current.session_id !== workspace.session.session_id || current.child_id !== workspace.session.child_id
              || taskMistake.session_id !== current.session_id || taskMistake.child_id !== current.child_id
              || current.mistake_id !== taskMistake.mistake_id || current.phase !== taskMistake.phase
              || ['closed', 'paused', 'scheduled'].includes(current.phase)
              || current.state.question_text !== taskMistake.state.question_text
              || current.state.submitted_answer !== taskMistake.state.submitted_answer
              || actual?.kind !== expected?.kind || actual?.stage !== expected?.stage
              || actual?.item?.diagnostic_item_id !== expected?.item?.diagnostic_item_id
              || actual?.item?.prompt !== expected?.item?.prompt) {
              throw new VoiceActionUnavailableError('复习记录已保存，但当前原题还没有核对一致，先不播放旧提问，也不重复启动任务。')
            }
            speechPolicy.localOnly = true
          }
          const binding = focus.dialogue?.answer_binding ?? focus.dialogue?.dialogue_binding
          setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id, binding ? { kind: 'mistake', binding } : null)
          if (focus.dialogue) window.dispatchEvent(new CustomEvent('hsc-mistake-dialogue-updated', { detail: focus.dialogue }))
          if (execution.value.execution.tutor_turn && execution.value.execution.replay_audio_allowed !== false) {
            const taught = execution.value.execution.tutor_turn
            const current = focus.dialogue
            const receipt = execution.value.execution.result
            const expectedMistake = receipt && typeof receipt === 'object' && 'mistake_id' in receipt ? receipt.mistake_id : null
            mistakeTeachingCurrent = !!current && current.session_id === taught.session_id
              && current.child_id === taught.child_id && typeof expectedMistake === 'string' && current.mistake_id === expectedMistake
              && current.state.teaching_turn_id === taught.turn_id && !current.state.foundation_hold
              && !['closed', 'paused'].includes(current.phase)
          }
        }
        const learningReplyExecution = ['learning.explain', 'learning.evaluate_response'].includes(execution.value.execution.capability)
        if (learningReplyExecution) {
          await refreshAgentRuntime()
          if (!isCurrent()) throw new SupersededDeviceCommand()
        }
        if (execution.value.execution.tutor_turn && execution.value.execution.replay_audio_allowed !== false) {
          const taught = execution.value.execution.tutor_turn
          if (mistakeTeachingCurrent) await presentGlobalTutorTurn(taught, { ...speechPolicy, isCurrent })
          else setEvaluationStatus('旧讲解已保留，但当前题目或任务已变化，不再播报。')
          shouldSpeak = false
        }
        if (!learningReplyExecution) await refreshAgentRuntime()
        if (execution.value.execution.capability === 'document.set_occlusion_reminders') {
          window.dispatchEvent(new CustomEvent('hsc-document-guidance-preferences-changed', { detail: { sessionId: workspace.session.session_id } }))
        }
        await followWritingFeedback(execution.value.turn.local_evaluation)
        if(native&&execution.value.execution.capability==='mistake.open'){
          if(!onOpenMistakeBook)throw new VoiceActionUnavailableError('错题工作区尚未连接。')
          clientToolResult=await onOpenMistakeBook('mistake_book')
        }
      } else if (action.type === 'learn_mistake') {
        if(!native||!onOpenMistakeBook||turn.runtime_capability!=='mistake.learn')
          throw new VoiceActionUnavailableError('这次错题请求尚未连接到学习服务，原话已保留。')
        outcomeOwnedByRuntime=true
        const executed=await native.port.execute()
        if(!isCurrent())throw new SupersededDeviceCommand()
        const result=executed.execution.result as {state?:MistakeLearningState;library?:MistakeLearningLibrary;mistake_learning_receipt?:{mistake_learning_effect_id?:string;view_rendered?:boolean}}|null
        if(executed.execution.capability!=='mistake.learn'||!result||(!result.state&&!result.library)
          ||typeof result.mistake_learning_receipt?.mistake_learning_effect_id!=='string')
          throw new VoiceActionUnavailableError('这次错题结果尚未确认，已保留原请求，不会重复出题。')
        await renderMistakeLearningResult({childId:workspace.session.child_id,sessionId:workspace.session.session_id,
          state:result.state,library:result.library,open:()=>onOpenMistakeBook('mistake_book'),current:isCurrent,signal})
        if(!isCurrent())throw new SupersededDeviceCommand()
        clientToolResult={mistake_learning_effect_id:result.mistake_learning_receipt.mistake_learning_effect_id,view_rendered:true}
        response=executed.execution.response_text
        shouldSpeak=action.suppress_speech!==true
        speechPolicy.localOnly=true
        speechPolicy.skipPublicReplyProvider=true
        speechPolicy.ordinaryResponseAllowed=false
        completedByRuntime=true
      } else if (action.type === 'navigate_workspace'&&action.destination) {
        if(!onOpenMistakeBook)throw new VoiceActionUnavailableError('错题工作区尚未连接。')
        clientToolResult=await onOpenMistakeBook(action.destination)
      } else if (action.type === 'check_native_writing_submission' || action.type === 'submit_retained_native_writing') {
        const target=action.native_writing_target
        const capability=action.type==='check_native_writing_submission'?'native_writing.submission_status':'native_writing.submit_retained'
        if(!target || target.child_id!==workspace.session.child_id || target.session_id!==workspace.session.session_id
          || (!native&&(action.source!=='content_free_voice_control'||!turn.requires_control_admission))
          || turn.runtime_capability!==capability || turn.runtime_execution_location!=='client_device_bridge')
          throw new VoiceActionUnavailableError('本次请求没有绑定原稿，未提交任何文字。')
        speechPolicy.localOnly=true
        response=action.type==='check_native_writing_submission'
          ? (await checkNativeWritingSubmission(target.child_id,target.session_id,isCurrent,
            query=>localApi.nativeWritingReceipt(target.session_id,query),target))!
          : await dispatchRetainedNativeWriting(target,isCurrent,turn.agent_turn_id)
        clientToolResult={native_writing_target:target,receipt_checked:true}
      } else if (action.type === 'read_writing_original') {
        const reference = action.writing_ocr_confirmation
        if (!reference || (turn.runtime_capability ?? action.capability) !== 'writing.read_original'
          || (turn.runtime_execution_location ?? action.execution_location) !== 'client_device_bridge') {
          throw new VoiceActionUnavailableError('本次原稿读回没有绑定题目，我没有提交任何作答。')
        }
        shouldSpeak = false
        if (!await runTool(() => playWritingReadback({
          childId: workspace.session.child_id, sessionId: workspace.session.session_id,
          confirmationId: reference.confirmation_id, bindingVersion: reference.binding_version, isCurrent,
          read: () => localApi.writingOcrConfirmation(workspace.session.session_id, reference.confirmation_id, reference.binding_version),
          record: event => localApi.recordWritingReadback(workspace.session.session_id, event),
          play: (message, callbacks) => {
            setReply(message)
            speakReply(message, undefined, { localOnly: true, isCurrent,
              onPlaybackStart: callbacks.started, onPlaybackEnd: callbacks.ended,
              onNotDelivered: callbacks.unavailable, onPlaybackAbandoned: callbacks.unavailable })
          },
        }))) return
        response = '原稿已完整读回，等待你确认识别内容与本题作答。'
      } else if (action.type === 'connect_devices') {
        if (!['camera', 'document_camera', 'all'].includes(action.device ?? '')) {
          throw new VoiceActionUnavailableError('没有听清要连接哪台设备。可以说“连接高拍仪”。')
        }
        const connection = await connectRequestedVideoDevices(
          workspace.session.session_id,
          action.device as DeviceConnectTarget,
          voiceEventController.current.signal,
        )
        clientToolResult = connection
        response = deviceConnectSpeech(connection)
        speechPolicy.localOnly = true
        for (const device of connection.devices) {
          void window.desktop?.recordRendererDiagnostic?.({
            event: device.status === 'connected' ? 'device.connect.completed' : 'device.connect.failed',
            level: device.status === 'connected' ? 'info' : 'warn',
            correlationId: turn.agent_turn_id,
            details: {
              device_type: device.device_type,
              status: device.status,
              ...(device.status === 'failed' ? { error_code: device.error_code } : {}),
            },
          })
        }
        if (!deviceConnectSucceeded(connection)) {
          const errorCode = connection.devices.find((item) => item.status === 'failed')?.error_code ?? 'DEVICE_OPEN_FAILED'
          await localApi.completeAgentTurn(turn.agent_turn_id, response, 'failed', errorCode, {
            device_connection_attempt: boundedExecutionResult(connection),
          })
          await refreshAgentRuntime()
          if (isCurrent() && pendingUtterancesRef.current.length === 0 && voiceInputQueue.pendingCount === 0) {
            setReply(response)
            if (!speechSuppressedRef.current) speakReply(response, undefined, { localOnly: true })
          }
          return
        }
      } else if (action.type === 'query_device_status') {
        if (!['camera', 'document_camera', 'all'].includes(action.device ?? '')) throw new VoiceActionUnavailableError('设备查询对象不明确，没有操作设备。')
        speechPolicy.localOnly = true
        if (action.device === 'camera') {
          clientToolResult = { device_type: 'camera', status: 'removed' }
          response = OBSERVATION_CAMERA_REMOVED_MESSAGE
        } else {
          const result = queryCameraDevices(workspace.session.session_id, 'document_camera')
          clientToolResult = result
          const device = result.devices[0]
          response = `高拍仪${device.frame_confirmed ? '当前有画面，题目还要等识别后确认' : device.track_live ? '已连接，正在等待新画面' : '当前没有读取画面'}。物理电源状态无法判断。`
          if (action.device === 'all') response = `${OBSERVATION_CAMERA_REMOVED_MESSAGE}${response}`
        }
      } else if (action.type === 'configure_wake_word') {
        if ((turn.runtime_capability ?? action.capability) !== 'voice.configure_wake_word'
          || (turn.runtime_execution_location ?? action.execution_location) !== 'client_device_bridge'
          || !window.desktop?.saveWakeWordSettings || !wakeSettingsLoaded || wakeSettingsReadError) {
          throw new VoiceActionUnavailableError('本机名字设置尚未就绪，没有修改唤醒词。')
        }
        const candidate = action.candidate?.trim()
        if (candidate !== undefined && (!candidate || candidate.length < 2 || candidate.length > 12 || /[\r\n]/.test(candidate))) {
          throw new VoiceActionUnavailableError('名字候选不符合本机保存要求，尚未修改。')
        }
        pendingNameRef.current = candidate ?? null
        setVoiceWakeNameSetup(true)
        setEditingWakeWord(true)
        setWakeWordDraft(candidate ?? '')
        setWakeSettingsMessage(null)
        response = candidate ? `你想把唤醒名字改为“${candidate}”，对吗？确认后我才保存。` : '你想给我换什么名字？你确认后我才保存。'
        const receipt = { capability: 'voice.configure_wake_word', execution_location: 'client_device_bridge', action_type: action.type,
          status: 'completed', completed_at: new Date().toISOString(),
          output: { configuration_started: true, phase: candidate ? 'confirm_name' : 'naming', wake_word_saved: false } }
        // This acknowledges entry into configuration, never a saved name.
        try {
          await localApi.completeAgentTurn(turn.agent_turn_id, response, 'completed', undefined, { execution_receipt: receipt, client_bridge_receipt: receipt })
        } catch {
          if (isCurrent()) setWakeSettingsMessage('取名流程已打开，但回执未确认；原名字未修改，不会自动重复提交。')
        }
        if (isCurrent()) { setReply(response); speakReply(response, undefined, { localOnly: true }, true) }
        return
      } else if (action.type === 'resume_voice_tutorial') {
        const runtime = onboardingRef.current
        if (!runtime || wakeSettingsReadError) throw new VoiceActionUnavailableError('教程或名字设置还没有读到，尚未重启教程。')
        setWakeSettingsMessage(null)
        const restarted = await runtime.restart()
        if (!isCurrent()) throw new SupersededDeviceCommand()
        const snapshot = runtime.snapshot
        if (!restarted) {
          await localApi.completeAgentTurn(turn.agent_turn_id, '教程重启进度没有确认保存。', 'failed', 'VOICE_ONBOARDING_RESTART_NOT_SAVED')
            .catch(() => { if (isCurrent()) setWakeSettingsMessage('教程重启未确认，失败回执也未保存；不会自动重试。') })
          return
        }
        const output: VoiceOnboardingRestartOutput = snapshot.phase === 'naming'
          ? { onboarding_restarted: true, onboarding_phase: 'naming', onboarding_step: null }
          : { onboarding_restarted: true, onboarding_phase: 'active', onboarding_step: 'wake_practice' }
        const receipt = {
          capability: 'onboarding.resume', execution_location: 'client_device_bridge', action_type: action.type,
          status: 'completed', completed_at: new Date().toISOString(), output,
        }
        try {
          await localApi.completeAgentTurn(turn.agent_turn_id, snapshot.prompt, 'completed', undefined, { execution_receipt: receipt, client_bridge_receipt: receipt })
        } catch {
          // A lost outcome acknowledgement is not permission to repeat the
          // restart or overwrite a possibly completed server outcome as failed.
          if (isCurrent()) setWakeSettingsMessage('教程进度已保存，但执行回执尚未确认；不会自动重启或重复提交。')
          return
        }
        await refreshAgentRuntime()
        // The runtime owns narration. Leave learning drafts/recovery intact.
        return
      } else if (action.type === 'transition' && action.target) {
        if (!await runTool(() => localApi.transition(workspace.session.session_id, workspace.session.state, action.target!, `voice_${turn.intent}`))) return
      } else if (action.type === 'update_planning_context') {
        const context = workspace.planning_context
        if (!await runTool(() => localApi.savePlanningContext(workspace.session.session_id, {
          energyLevel: action.energy_level ?? context.energy_level,
          fixedCommitmentMinutes: action.fixed_commitment_minutes ?? context.fixed_commitment_minutes,
          fixedCommitmentNote: action.fixed_commitment_note ?? context.fixed_commitment_note ?? '',
          tomorrowEventSubject: action.tomorrow_event_subject ?? context.tomorrow_event_subject ?? '其他',
          tomorrowEventTitle: action.tomorrow_event_title ?? context.tomorrow_event_title ?? '',
          tomorrowEventMinutes: action.tomorrow_event_minutes ?? context.tomorrow_event_minutes
        }))) return
      } else if (action.type === 'add_recall' && action.subject && action.summary) {
        if (!await runTool(() => localApi.addRecall(workspace.session.session_id, action.subject!, action.summary!))) return
      } else if (action.type === 'review_pending_recall' && action.decision) {
        const recallDecision = action.decision === 'confirm' ? 'confirm' : 'reject'
        const candidate = workspace.recalls.find((item) => item.confirmation_status === 'inferred')
          ?? workspace.recalls.find((item) => item.confirmation_status === 'unknown')
        if (!candidate) throw new VoiceActionUnavailableError('现在没有等待确认的课堂候选。你可以继续说今天学了什么。')
        else if (recallDecision === 'confirm' && candidate.confirmation_status === 'unknown') throw new VoiceActionUnavailableError('这条证据还不够，不能直接确认。你可以把教材页或作业放到高拍仪下再核对。')
        else if (!await runTool(() => localApi.reviewRecall(workspace.session.session_id, candidate.recall_id, recallDecision))) return
      } else if (action.type === 'declare_no_recall') {
        if (!await runTool(() => localApi.declareNoRecall(workspace.session.session_id))) return
      } else if (action.type === 'add_homework' && action.subject && action.title) {
        if (!await runTool(() => localApi.addHomework(
          workspace.session.session_id,
          action.subject!,
          action.title!,
          action.expected_minutes ?? 20,
          {
            taskType: action.task_type,
            rangeText: action.range_text ?? undefined,
            dueAt: action.due_at ?? undefined,
            specialRequirements: action.special_requirements ?? undefined,
          }
        ))) return
      } else if (action.type === 'update_last_homework_minutes' && action.expected_minutes) {
        const item = workspace.homework.at(-1)
        if (!item) response = '还没有可以修改的作业。请先说一项作业。'
        else if (!await runTool(() => localApi.updateHomework(workspace.session.session_id, item.homework_id, {
          subject: item.subject, title: item.title, expectedMinutes: action.expected_minutes!, taskType: item.task_type,
          rangeText: item.range_text ?? '', dueAt: item.due_at ?? '', specialRequirements: item.special_requirements ?? '',
          sourceArtifactId: item.source_artifact_id ?? undefined, ocrConfidence: item.ocr_confidence ?? undefined,
        }))) return
      } else if (action.type === 'review_first_carryover' && ['continue_today', 'completed_elsewhere', 'cancelled'].includes(action.decision ?? '')) {
        const decision = action.decision as 'continue_today' | 'completed_elsewhere' | 'cancelled'
        const reason = decision === 'continue_today' ? '孩子语音确认今天继续完成' : decision === 'completed_elsewhere' ? '孩子语音确认已在其他时间完成' : '孩子语音确认老师已取消或不再要求'
        const readBack = async (candidate: import('./types').DeferredTaskCandidate, pending: ReturnType<typeof prepareCarryoverReview>): Promise<void> => {
          await localApi.completeAgentTurn(turn.agent_turn_id, '原作业内容已变化，本轮只读回新内容，等待新确认。', 'failed', 'DEFERRED_TASK_SOURCE_CHANGED')
          if (!isCurrent()) return
          const description = [candidate.subject, candidate.title, candidate.range_text,
            `预计${candidate.expected_minutes}分钟`, candidate.due_at, candidate.special_requirements].filter(Boolean).join('，')
          const message = `原作业内容已经变化，刚才的决定没有执行。我重新核对的是：${description}。请听完后重新告诉我，是今天继续、已经做完，还是取消这项。`
          setReply(message)
          speakReply(message, undefined, {localOnly:true, onPlaybackEnd:() => {
            try { markCarryoverReadback(pending) }
            catch { setControlledActivityError('作业读回确认状态未保存，请重新核对原作业。') }
          }})
        }
        let pending: ReturnType<typeof prepareCarryoverReview>
        try {
          const saved = readCarryoverReview(workspace.session.session_id, workspace.session.child_id)
          const currentWorkspace = saved?.confirmationId ? await localApi.sessionWorkspace(saved.sessionId) : workspace
          if (!isCurrent()) throw new SupersededDeviceCommand()
          if (currentWorkspace.session.session_id !== workspace.session.session_id
            || currentWorkspace.session.child_id !== workspace.session.child_id) throw new VoiceActionUnavailableError('原作业的会话归属未通过核对。')
          const candidate = saved ? currentWorkspace.carryover_candidates.find(item => item.source_plan_item_id === saved.sourcePlanItemId)
            : currentWorkspace.carryover_candidates[0]
          if (saved?.confirmationId && (carryoverArrival !== saved.confirmationId || !saved.readbackReady
            || candidate?.source_version !== saved.sourceVersion)) {
            const current = refreshCarryoverReadback(saved, candidate)
            await readBack(candidate!, current)
            return
          }
          pending = prepareCarryoverReview({sessionId:workspace.session.session_id, childId:workspace.session.child_id},
            candidate, decision, reason, turn.agent_turn_id, carryoverArrival)
        } catch (cause) {
          throw new VoiceActionUnavailableError(cause instanceof Error ? cause.message : '延期任务恢复信息未保存，暂未执行。')
        }
        const changed: {candidate?: import('./types').DeferredTaskCandidate} = {}
        if (!await runTool(async () => {
          try {
            const receipt = await localApi.reviewDeferredTask(pending.sessionId, pending.sourcePlanItemId,
              pending.decision, pending.reason, pending.requestId, pending.sourceVersion)
            finishCarryoverReview(pending, receipt)
          } catch (cause) {
            if (!(cause instanceof ApiError) || cause.status !== 409 || cause.code !== 'DEFERRED_TASK_SOURCE_CHANGED' || !cause.details) throw cause
            changed.candidate = invalidateCarryoverReview(pending, cause.details)
          }
        })) return
        if (changed.candidate) {
          const current = readCarryoverReview(pending.sessionId, pending.childId)
          if (!current) throw new VoiceActionUnavailableError('原作业重新确认状态丢失，未提交新决定。')
          await readBack(changed.candidate, current)
          return
        }
      } else if (action.type === 'declare_no_homework') {
        if (!await runTool(() => localApi.declareNoHomework(workspace.session.session_id))) return
      } else if (action.type === 'generate_plan') {
        const generated: { value: SessionWorkspaceState | null } = { value: null }
        if (!await runTool(async () => { generated.value = await localApi.generatePlan(workspace.session.session_id) })) return
        const learningItems = (generated.value?.plan_items ?? []).filter((item) => item.source_type !== 'planned_break' && item.status !== 'rescheduled')
        const sequence = learningItems.slice(0, 4).map((item, index) => `${index + 1}，${planItemTitle(item)}，${item.expected_minutes}分钟`).join('；')
        response = learningItems.length > 0
          ? `今天的计划已经排好，共${learningItems.length}项。${sequence}${learningItems.length > 4 ? `；还有${learningItems.length - 4}项我会按顺序提醒。` : '。'}你可以说“按这个开始”，或者说“先做短任务”。`
          : '今天没有需要硬加的学习任务。你确认后就可以进入今日回顾。'
      } else if (action.type === 'reorder_plan' && action.strategy) {
        if (!await runTool(() => localApi.reorderPlan(workspace.session.session_id, action.strategy!, '孩子通过语音调整计划顺序'))) return
      } else if (action.type === 'begin_break') {
        if (!await runTool(() => localApi.beginBreak(workspace.session.session_id))) return
      } else if (action.type === 'pause_active_task') {
        const active = workspace.plan_items.find((item) => item.status === 'active')
        if (active) {
          if (!await runTool(() => localApi.pausePlanItem(active.plan_item_id))) return
        } else response = '现在没有正在计时的任务。你可以说“开始任务”，或者问我接下来做什么。'
      } else if (action.type === 'reschedule_current_task') {
        const current = workspace.plan_items.find((item) => ['active', 'paused'].includes(item.status))
          ?? workspace.plan_items.find((item) => item.status === 'pending' && item.source_type !== 'planned_break')
        if (current) {
          if (!await runTool(() => localApi.reschedulePlanItem(current.plan_item_id, action.reason ?? '孩子通过语音决定延期'))) return
        } else response = '现在没有可以延期的学习任务。'
      } else if (action.type === 'resume_study') {
        if (!await runTool(() => localApi.resumeSession(workspace.session.session_id))) return
      } else if (action.type === 'resume_paused_task') {
        throw new VoiceActionUnavailableError('恢复原任务需要核对已保存的暂停记录，请重新说出要继续的任务。')
      } else if (action.type === 'start_next_task') {
        const next = workspace.plan_items.find((item) => item.status === 'paused')
          ?? workspace.plan_items.find((item) => item.status === 'pending')
        if (next) {
          const startsVocabulary = next.source_type === 'pet_daily' && next.source_ref?.startsWith('pet-vocabulary-due:')
          if (startsVocabulary) petVocabularyPromptRequestedRef.current = next.plan_item_id
          if (!await runTool(() => next.status === 'paused' ? localApi.resumePlanItem(next.plan_item_id) : next.source_type === 'planned_break' ? localApi.beginBreak(workspace.session.session_id) : localApi.startPlanItem(next.plan_item_id))) {
            if (startsVocabulary) petVocabularyPromptRequestedRef.current = null
            return
          }
          if (startsVocabulary) {
            const review = await localApi.nextPetVocabulary(workspace.session.session_id)
            setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
              review.card?.prompt_session_id && review.card.card_version ? { kind: 'vocabulary', binding: {
                prompt_session_id: review.card.prompt_session_id, card_version: review.card.card_version } } : null)
            response = review.card?.prompt
              ?? (review.summary.deck_size === 0
                ? '还没有通过教材证据确认的词汇卡，这项任务暂时不会凭空出题。'
                : '这项任务的到期词汇已经复习完了。')
          }
          if (next.source_type === 'due_review') shouldSpeak = false
        } else response = '今天没有等待开始的任务。'
      } else if (action.type === 'complete_active_task') {
        throw new VoiceActionUnavailableError('任务完成需要云端运行时逐项核对真实确认；当前没有保存完成记录。')
      } else if (action.type === 'ask_tutor') {
        const current = workspace.plan_items.find((item) => item.status === 'active') ?? workspace.plan_items.find((item) => item.status === 'pending')
        const question = action.question ?? utterance
        const subject = action.subject ?? current?.subject ?? inferQuestionSubject(question)
        const hintLevel = Math.min(6, Math.max(1, action.hint_level ?? 1))
        const ocrConfirmationId = action.ocr_confirmation_id
        if (action.document_goal_root_turn_id && (!ocrConfirmationId || !action.tutor_request_action_id)) {
          throw new VoiceActionUnavailableError('原题确认或讲解请求编号缺失，没有另起请求。原进度仍保留。')
        }
        const followup = action.question_relation === 'followup'
        const followupSource = followup ? action.tutor_followup_binding?.turn_id : undefined
        if (followup && !followupSource) throw new VoiceActionUnavailableError('刚才的讲解来源暂时无法核对，请把这次想问的内容重新说清楚。')
        const actionId = action.tutor_request_action_id ?? (followup ? `${turn.agent_turn_id}-followup` : crypto.randomUUID())
        const questionReference = action.diagnostic_prompt_id && action.diagnostic_item_version
          ? { prompt_id: action.diagnostic_prompt_id, item_version: action.diagnostic_item_version } : undefined
        if (followup && (ocrConfirmationId || questionReference)) throw new VoiceActionUnavailableError('刚才的题目来源需要重新核对，请重新说出要问的问题。')
        if (ocrConfirmationId && questionReference) throw new VoiceActionUnavailableError('讲解对应了不同题目来源，先核对原题再继续。')
        const initialRecovery: TutorToolRecovery = {
          turnId: turn.agent_turn_id, actionId, subject, question, hintLevel,
          version: 1, createdAt: new Date().toISOString(), attempts: 1,
          ...(followupSource ? { followupSourceTurnId: followupSource } : {}),
          ...(questionReference ? { questionReference } : {}),
          ...(ocrConfirmationId ? { ocrConfirmationId } : {}),
          ...(action.local_learning_context?.diagnostic_prompt ? {
            localDiagnosticPrompt: action.local_learning_context.diagnostic_prompt,
            atomicAuditEventId: action.local_learning_context.audit_event_id ?? undefined,
          } : {}),
          ...(action.local_learning_context?.course_context ? { courseContext: action.local_learning_context.course_context } : {}),
        }
        try {
          await draft.saveNow({ text, tutor_recovery: { ...initialRecovery, requiresExplicitRetryConfirmation: true } })
          tutorRecoveryRef.current = initialRecovery
          if (isCurrent()) setTutorRecovery(initialRecovery)
          if (!isCurrent()) throw new SupersededDeviceCommand()
          outcomeOwnedByRuntime = true
          const rawTutorTurn = followupSource
            ? await commandApi.askTutorFollowup(workspace.session.session_id, followupSource, question, actionId)
            : ocrConfirmationId
            ? await askTutorWithOcrEvidence(workspace.session.session_id, subject, ocrConfirmationId, actionId, hintLevel)
            : questionReference
            ? await commandApi.askTutor(workspace.session.session_id, subject, question, hintLevel, undefined, actionId, false, questionReference)
            : await commandApi.askTutor(workspace.session.session_id, subject, question, hintLevel, undefined, actionId)
          const tutorTurn = appendLocalDiagnosticFollowUp(rawTutorTurn, action.local_learning_context)
          await finishTutorCompletion(makeTutorCompletion(turn.agent_turn_id, actionId, 'ask_tutor', tutorTurn.answer,
            tutorExecutionSummary(tutorTurn), speechPolicy.localOnly || tutorTurn.answer !== rawTutorTurn.answer, tutorTurn), isCurrent)
          return
        } catch (cause) {
          if (!isCurrent() || cause instanceof SupersededDeviceCommand) throw new SupersededDeviceCommand()
          const unknownOutcome = cause instanceof ApiError && cause.code === 'TUTOR_RESULT_UNCONFIRMED'
          const sourceBlocked = cause instanceof ApiError ? tutorSourceBlock(cause.code) : undefined
          const recovery: TutorToolRecovery = {
            turnId: turn.agent_turn_id, actionId, subject, question, hintLevel,
            version: 1, createdAt: new Date().toISOString(), attempts: 1,
            ...(followupSource ? { followupSourceTurnId: followupSource } : {}),
            ...(questionReference ? { questionReference } : {}),
            ...(ocrConfirmationId ? { ocrConfirmationId } : {}),
            ...(sourceBlocked ? { sourceBlocked } : {}),
            ...(action.local_learning_context?.diagnostic_prompt
              ? {
                  localDiagnosticPrompt: action.local_learning_context.diagnostic_prompt,
                  atomicAuditEventId: action.local_learning_context.audit_event_id ?? undefined,
                }
              : {}),
            ...(action.local_learning_context?.course_context ? { courseContext: action.local_learning_context.course_context } : {}),
            ...(unknownOutcome ? { requiresExplicitRetryConfirmation: true } : {}),
          }
          tutorRecoveryRef.current = recovery
          setTutorRecovery(recovery)
          let failureResponse = sourceBlocked ? tutorSourceBlockMessage(sourceBlocked) : unknownOutcome
            ? '上次讲解结果没有确认保存。再次请求可能会再次调用模型；请先确认。'
            : tutorFailureMessage(1)
          try { await draft.saveNow({ text, tutor_recovery: recovery }) }
          catch { failureResponse = '恢复记录还没有保存成功，请不要关闭页面。可以说“重试讲解”先重试保存，不会跳过保存发送请求。' }
          if (!isCurrent()) throw new SupersededDeviceCommand()
          if (cause instanceof VoiceRecoverySaveError) failureResponse = '恢复记录还没有保存成功，这次没有请求讲解。请保持页面，可以说“重试讲解”。'
          await localApi.completeAgentTurn(turn.agent_turn_id, failureResponse, 'failed', sourceBlocked && cause instanceof ApiError ? cause.code : unknownOutcome ? 'TUTOR_RESULT_UNCONFIRMED' : 'CLIENT_TOOL_ACTION_FAILED').catch(() => undefined)
          if (pendingUtterancesRef.current.length === 0 && voiceInputQueue.pendingCount === 0 && mountedRef.current) {
            setReply(failureResponse)
            if (!speechSuppressedRef.current) speakReply(failureResponse)
          }
          return
        }
      } else if (action.type === 'start_companion_activity' && action.activity_type) {
        if (!await runTool(() => localApi.startCompanionActivity(
          workspace.session.session_id,
          action.activity_type as 'interactive_story' | 'word_chain' | 'riddle' | 'movement' | 'tidy_challenge',
          action.topic ?? utterance,
          turn.agent_turn_id,
        ))) return
      } else if (action.type === 'end_companion_activity') {
        if (!await runTool(() => localApi.endCompanionActivity(workspace.session.session_id))) return
        response = '这轮活动先停在这里。刚才感觉怎么样？准备好继续学习时告诉我。'
      } else if (action.type === 'start_controlled_content') {
        const playback: { value: ControlledContentPlayback | null } = { value: null }
        if (!await runTool(async () => {
          playback.value = await launchSelectedVideo(action.query ?? utterance, turn.agent_turn_id, isCurrent,
            controlledStartSelection(action, 'content'))
          return {content_session_id:playback.value.content_session_id, status:playback.value.status,
            launch_performed:playback.value.launch_allowed !== false && !playback.value.replayed && playback.value.status === 'active',
            replayed:Boolean(playback.value.replayed)}
        })) return
        if (playback.value) response = playback.value.launch_allowed === false || playback.value.replayed || playback.value.status !== 'active'
          ? '找到了上次观看的记录，没有重新播放。要查看观看状态或停止视频，可以直接告诉我。'
          : `开始播放“${playback.value.title}”，最多${playback.value.allowed_minutes}分钟。随时说“停止视频”就会关闭，学习进度会保留。`
      } else if (['stop_controlled_content', 'stop_controlled_game'].includes(action.type) && action.controlled_launch_cancel_binding) {
        const target=action.controlled_launch_cancel_binding
        const kind=action.type==='stop_controlled_content'?'video':'game'
        if(target.child_id!==workspace.session.child_id||target.session_id!==workspace.session.session_id
          ||target.kind!==kind||target.request_id!==target.source_turn_id||!target.request_id
          ||!/^[a-f0-9]{64}$/.test(target.source_action_sha256)||!action.controlled_voice_binding) {
          throw new VoiceActionUnavailableError('原启动核对请求归属不一致，尚未取消。')
        }
        if(!await runTool(async()=>{
          const launches=await window.desktop?.listControlledLaunches?.({kind,childId:target.child_id,
            sessionId:target.session_id,requestId:target.request_id,includeSettled:true})
          if(!isCurrent())throw new SupersededDeviceCommand()
          if(!launches||launches.length!==1)throw new ControlledStartRecoveryError('原启动记录还无法精确核对，没有认定已经结束。恢复连接后再说停止原活动。')
          const launch=launches[0]
          if(launch.childId!==target.child_id||launch.sessionId!==target.session_id||launch.kind!==kind
            ||launch.requestId!==target.request_id||launch.activityId!==null)throw new ControlledStartRecoveryError('原启动记录发生变化，未关闭其他活动。')
          const proof=await window.desktop?.cancelControlledLaunch?.(launch.intentId)
          if(!isCurrent())throw new SupersededDeviceCommand()
          if(proof?.closed!==true||proof.noActivity!==true||proof.proof!=='cancelled_before_start')
            throw new ControlledStartRecoveryError('原启动是否执行仍待核对，没有认定已经结束。恢复连接后再说停止原活动。')
          return {request_id:target.request_id,native_intent_id:launch.intentId,status:'cancelled_before_start',
            device_closed:true,no_activity:true,task_resumed:false}
        }))return
        response='原启动请求已取消，没有打开活动，也没有自动恢复学习。'
        setControlledActivityError(null)
      } else if (['stop_controlled_content', 'stop_controlled_game'].includes(action.type)
        && (action.controlled_stop_binding !== undefined || action.source === 'local_controlled_stop_v1')) {
        const video = action.type === 'stop_controlled_content'
        const kind = video ? 'video' : 'game'
        const binding = action.controlled_stop_binding
        const sid = workspace.session.session_id, child = workspace.session.child_id
        if (!binding || binding.child_id !== child || binding.session_id !== sid
          || binding.kind !== (video ? 'content' : 'games') || typeof binding.activity_id !== 'string'
          || !binding.activity_id || binding.activity_id !== (video ? action.content_session_id : action.game_session_id)) {
          throw new VoiceActionUnavailableError('原停止请求的活动归属已变化，没有操作当前活动。')
        }
        const id = binding.activity_id
        const active = video
          ? controlledContentRef.current ?? (await localApi.activeControlledContent(sid)).playback
          : controlledGameRef.current ?? (await localApi.activeControlledGame(sid)).game
        if (!isCurrent()) throw new SupersededDeviceCommand()
        const activeId = active && (video ? (active as ControlledContentPlayback).content_session_id : (active as ControlledGameSession).game_session_id)
        if (active && (active.child_id !== child || active.session_id !== sid || activeId !== id)) {
          throw new VoiceActionUnavailableError('当前活动不是原停止请求的对象，没有关闭或结算新活动。')
        }
        const pending = readPendingControlledEnds(sid, child, kind).filter(entry => entry.activityId === id)
        if (!pending.length) {
          const readback = action.controlled_stop_readback
          if (!readback || readback.activity_id !== id || readback.child_id !== child || readback.session_id !== sid
            || readback.kind !== binding.kind || !['active', 'completed', 'stopped', 'expired', 'interrupted', 'failed'].includes(readback.status)
            || !Number.isFinite(Date.parse(readback.started_at))) {
            throw new VoiceActionUnavailableError('原活动的只读记录尚未核对，没有凭空确认设备已关闭。')
          }
          const seconds = active && active.status === 'active'
            ? Math.max(0, Math.floor((Date.now() - Date.parse(active.started_at)) / 1000)) : 0
          pending.push(preparePendingControlledEnd({childId: child, sessionId: sid, kind, activityId: id},
            {reason: 'child_requested', actualSeconds: seconds}))
        }
        const inFlight = video ? controlledContentEndInFlightRef.current : controlledGameEndInFlightRef.current
        if (inFlight.has(id)) throw new VoiceActionUnavailableError('原活动结束回执正在核对，没有重复提交。')
        inFlight.add(id)
        try {
          if (!await runTool(() => retryControlledEnds(pending, isCurrent, () => {
            if (video && controlledContentRef.current?.content_session_id === id) controlledContentRef.current = null
            if (!video && controlledGameRef.current?.game_session_id === id) controlledGameRef.current = null
          }))) return
        } finally { inFlight.delete(id) }
        response = '原活动的关闭和结束记录已经核对。没有自动恢复学习，准备好后告诉我。'
        setControlledActivityError(null)
      } else if (action.type === 'stop_controlled_content') {
        const pending = controlledContentRef.current ? [] : readPendingControlledEnds(workspace.session.session_id, workspace.session.child_id, 'video')
        if (pending.length > 0) {
          if (pending.some(entry => controlledContentEndInFlightRef.current.has(entry.activityId))) throw new VoiceActionUnavailableError('原视频结束回执正在核对，没有重复操作。')
          if (!await runTool(() => retryControlledEnds(pending, isCurrent, receipt => {
            if (controlledContentRef.current?.content_session_id === receipt.content_session_id) controlledContentRef.current = null
          }))) return
        } else {
        const playback = controlledContentRef.current ?? (await localApi.activeControlledContent(workspace.session.session_id)).playback
        if (playback && (playback.session_id !== workspace.session.session_id || playback.child_id !== workspace.session.child_id)) {
          throw new VoiceActionUnavailableError('原观看记录归属不一致，未结算其他孩子的活动。')
        }
        if (!isCurrent()) throw new SupersededDeviceCommand()
        if (!playback || controlledContentEndInFlightRef.current.has(playback.content_session_id)) {
          throw new VoiceActionUnavailableError('尚未确认原视频结束记录，暂未重复结算其他活动。')
        }
        controlledContentRef.current = playback
        controlledContentEndInFlightRef.current.add(playback.content_session_id)
        try {
          if (playback && !await runTool(() => settleControlledActivity('video', playback, 'child_requested',
            Math.max(0, Math.floor((Date.now() - Date.parse(playback.started_at)) / 1000)), false,
            () => { if (!isCurrent()) throw new SupersededDeviceCommand() }))) return
          if (controlledContentRef.current === playback) controlledContentRef.current = null
        } finally {
          controlledContentEndInFlightRef.current.delete(playback.content_session_id)
        }
        }
        response = pending.length > 0
          ? (controlledContentRef.current ? '原来的视频结束记录已经核对。新的视频还在播放，没有操作它；要停止这段视频，可以再告诉我。'
            : '原来的视频结束记录已经全部核对。准备好继续刚才的学习时告诉我。')
          : '视频已经关闭。准备好继续刚才的学习时告诉我。'
        setControlledActivityError(null)
      } else if (action.type === 'start_controlled_game') {
        const game: { value: ControlledGameSession | null } = { value: null }
        if (!await runTool(async () => {
          game.value = await launchSelectedGame(action.query ?? utterance, turn.agent_turn_id, isCurrent,
            controlledStartSelection(action, 'games'))
          return {game_session_id:game.value.game_session_id, status:game.value.status,
            launch_performed:game.value.launch_allowed !== false && !game.value.replayed && game.value.status === 'active',
            replayed:Boolean(game.value.replayed)}
        })) return
        if (game.value) response = game.value.launch_allowed === false || game.value.replayed || game.value.status !== 'active'
          ? '找到了上次游戏的记录，没有重新启动。要查看游戏状态或停止游戏，可以直接告诉我。'
          : `开始“${game.value.title}”，最多${game.value.allowed_minutes}分钟。时间到会自动关闭，随时说“停止游戏”也可以结束。`
      } else if (action.type === 'stop_controlled_game') {
        const pending = controlledGameRef.current ? [] : readPendingControlledEnds(workspace.session.session_id, workspace.session.child_id, 'game')
        if (pending.length > 0) {
          if (pending.some(entry => controlledGameEndInFlightRef.current.has(entry.activityId))) throw new VoiceActionUnavailableError('原游戏结束回执正在核对，没有重复操作。')
          if (!await runTool(() => retryControlledEnds(pending, isCurrent, receipt => {
            if (controlledGameRef.current?.game_session_id === receipt.game_session_id) controlledGameRef.current = null
          }))) return
        } else {
        const game = controlledGameRef.current ?? (await localApi.activeControlledGame(workspace.session.session_id)).game
        if (game && (game.session_id !== workspace.session.session_id || game.child_id !== workspace.session.child_id)) {
          throw new VoiceActionUnavailableError('原游戏记录归属不一致，未结算其他孩子的活动。')
        }
        if (!isCurrent()) throw new SupersededDeviceCommand()
        if (!game || controlledGameEndInFlightRef.current.has(game.game_session_id)) {
          throw new VoiceActionUnavailableError('尚未确认原游戏结束记录，暂未重复结算其他活动。')
        }
        controlledGameRef.current = game
        controlledGameEndInFlightRef.current.add(game.game_session_id)
        try {
          if (game && !await runTool(() => settleControlledActivity('game', game, 'child_requested',
            Math.max(0, Math.floor((Date.now() - Date.parse(game.started_at)) / 1000)), false,
            () => { if (!isCurrent()) throw new SupersededDeviceCommand() }))) return
          if (controlledGameRef.current === game) controlledGameRef.current = null
        } finally {
          controlledGameEndInFlightRef.current.delete(game.game_session_id)
        }
        }
        response = pending.length > 0
          ? (controlledGameRef.current ? '原来的游戏结束记录已经核对。新的游戏还在进行，没有操作它；要停止这次游戏，可以再告诉我。'
            : '原来的游戏结束记录已经全部核对。感觉怎么样，准备好继续刚才的学习了吗？')
          : '游戏已经关闭。感觉怎么样，准备好继续刚才的学习了吗？'
        setControlledActivityError(null)
      } else if (action.type === 'open_mistake_book') {
        if(native){if(!onOpenMistakeBook)throw new VoiceActionUnavailableError('错题工作区尚未连接。');clientToolResult=await onOpenMistakeBook('mistake_book')}
        else if (onOpenMistakeBook) afterSpeech = () => {
          void onOpenMistakeBook().catch(() => {
            const message = '错题本暂时没有打开，当前学习记录仍然保留。你可以再说一次。'
            setReply(message)
            speakReply(message)
          })
        }
        else response = '错题本入口暂时没有连接成功，请告诉家长检查应用。'
      } else if (action.type === 'switch_child_profile' && action.target_child_id) {
        if (!onSwitchChild) throw new Error('Child profile switch is not connected')
        const handoff = prepareProfileSwitch({ turnId: turn.agent_turn_id, sourceSessionId: workspace.session.session_id,
          sourceChildId: workspace.session.child_id, targetChildId: action.target_child_id, clientActionId: `${turn.agent_turn_id}-switch` })
        outcomeOwnedByRuntime = true
        let recorded = false
        try {
          await onSwitchChild(action.target_child_id, async output => {
            const saved = recordProfileSwitchDestination(handoff, output)
            recorded = true
            // This acknowledgement survives the source panel's unmount. An
            // unknown response is retried by App, never converted to failure.
            void acknowledgeProfileSwitch(saved)
          }, handoff.clientActionId)
          if (!recorded) throw new ProfileSwitchPendingError()
        } catch { throw new ProfileSwitchPendingError() }
        return
      } else if (action.type === 'find_parent' || action.type === 'find_parent_urgent') {
        clientToolResult = { parent_contact_prompted: true }
      } else if (action.type === 'describe_plan') {
        const studyItems = workspace.plan_items.filter((item) => item.source_type !== 'planned_break')
        const completed = studyItems.filter((item) => item.status === 'completed').length
        response = `今天共有${studyItems.length}项学习任务，已经完成${completed}项。`
      } else if (action.type === 'request_capture' || action.type === 'retry_capture') {
        stopGlobalTutorTimeline()
        setGlobalTutorTurn(null)
        setGlobalTutorBoard(null)
        setGlobalTutorSegments(0)
        documentCaptureActiveRef.current = true
        setDocumentCaptureActive(true)
        const forMistake = action.observation_only ? false : action.mistake_recapture_target ? true : action.type === 'retry_capture' ? captureForMistakeRef.current : /错题|订正/.test(utterance)
        captureForMistakeRef.current = forMistake
        void window.desktop?.recordRendererDiagnostic?.({
          event: 'document.capture.agent_dispatched',
          level: 'info',
          details: { session_state: workspace.session.state, route: workspace.session.state === 'study' ? 'study' : 'global', for_mistake: forMistake }
        }).catch(() => undefined)
        if (speechSuppressedRef.current) throw new SupersededDeviceCommand()
        captureBridge.cancel()
        setCaptureSaveError(null)
        captureBridge.request(turn, forMistake, (request) => {
          if (workspace.session.state === 'study' && !request.observationOnly) {
            window.dispatchEvent(new CustomEvent('hsc-request-question-capture', { detail: request }))
          } else {
            setGlobalCaptureCommandId(request.commandId)
            setGlobalCaptureObservationOnly(request.observationOnly ?? false)
            startGlobalQuestionCapture(forMistake)
            request.accept()
          }
        })
        completedByRuntime = true // Final completion belongs to the device receipt, not dispatch.
        response = '拍题请求已发出，正在等待设备拍摄和识别；你可以继续说话。'
      } else if (action.type === 'confirm_ocr_text' || action.type === 'correct_ocr_text') {
        if (action.document_goal_confirmation_blocked) {
          throw new VoiceActionUnavailableError('原拍题目标已有后续记录或来源发生变化，需要先核对原请求结果。我没有另起讲解，原记录仍保留。')
        }
        const correctedText = action.type === 'correct_ocr_text' ? action.corrected_text : undefined
        const goal = action.document_goal_confirmation_root ? {
          rootTurnId: action.document_goal_confirmation_root, revision: action.document_goal_confirmation_revision ?? 0,
        } : undefined
        if (goal) outcomeOwnedByRuntime = true
        const handled = !goal && workspace.session.state === 'study' && await dispatchStudyCaptureVoiceAction(workspace.session.session_id, workspace.session.child_id, 'confirm', correctedText, voiceEventController.current.signal)
        if (handled) shouldSpeak = false
        if (!handled) {
          const tutor = await finishGlobalOcrConfirmation(correctedText, goal)
          if (tutor === 'goal_prepared' && goal) {
            if (workspace.session.state === 'study') await dispatchStudyCaptureVoiceAction(workspace.session.session_id, workspace.session.child_id, 'cancel', undefined, voiceEventController.current.signal)
            documentGoalToContinue = goal.rootTurnId; shouldSpeak = false
          }
          else if (tutor && tutor !== 'goal_prepared') { response = tutor.answer; shouldSpeak = false }
          else response = '现在还没有可以确认的题干。你可以说“帮我看这题”重新拍题。'
        }
      } else if (action.type === 'cancel_capture') {
        setRestoredDocumentGoal(null)
        captureBridge.cancel()
        documentCaptureActiveRef.current = false
        setDocumentCaptureActive(false)
        const handled = workspace.session.state === 'study' && await dispatchStudyCaptureVoiceAction(workspace.session.session_id, workspace.session.child_id, 'cancel', undefined, voiceEventController.current.signal)
        if (!handled) {
          setGlobalCaptureVisible(false)
          setGlobalCaptureReview(null)
          setGlobalCaptureMistakeArtifact(null)
        }
      } else if (action.type === 'start_diagnostic') {
        const diagnostic = await runDiagnosticVoice(workspace, {
          type: 'start_diagnostic', track: action.track, subject: action.subject,
          knowledge_node_id: action.knowledge_node_id, require_unexposed: action.require_unexposed,
        }, turn.agent_turn_id, isCurrent, !prepared && !turn.requires_control_admission)
        response = diagnostic.response
        clientToolResult = diagnostic.result
        const priorTeaching = globalTutorTurn ?? workspace.latest_tutor_turn
        if (diagnostic.result.prompt && priorTeaching
          && diagnostic.result.prompt.created_at > priorTeaching.created_at) {
          tutorPlayback.interrupt()
          stopGlobalTutorTimeline()
          setGlobalTutorTurn(null); setGlobalTutorBoard(null); setGlobalTutorSegments(0)
        }
        await followWritingFeedback(diagnostic.result.local_learning_evaluation)
        if (diagnostic.result.status === 'waiting_answer' && diagnostic.result.prompt && isPetListening(diagnostic.result.prompt)) {
          const playback = await playPetListening(workspace.session.session_id, diagnostic.result.prompt, turn.agent_turn_id, isCurrent, voiceEventController.current.signal)
          response = playback.response
          clientToolResult = { diagnostic: diagnostic.result, playback }
        }
        speechPolicy.localOnly = true
        Object.assign(speechPolicy, originalAudioPromptPlayback(workspace.session.session_id, diagnostic.result, speechPolicy))
        window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated', { detail: { sessionId: workspace.session.session_id, result: diagnostic.result } }))
        await run(async () => undefined)
      } else if (action.type === 'start_pet_vocabulary_review') {
        const review = await localApi.nextPetVocabulary(workspace.session.session_id)
        setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
          review.card?.prompt_session_id && review.card.card_version ? { kind: 'vocabulary', binding: {
            prompt_session_id: review.card.prompt_session_id, card_version: review.card.card_version } } : null)
        clientToolResult = review
        response = review.card?.prompt
          ?? (review.summary.deck_size === 0
            ? '还没有通过教材证据确认的词汇卡，我不会凭空出题。'
            : '今天的到期词汇已经复习完了。')
      } else if (action.type === 'submit_pet_vocabulary_answer' && action.answer) {
        if (!action.vocabulary_prompt_id || !action.vocabulary_card_version) throw new VoiceActionUnavailableError('这句话没有绑定原词汇卡，未提交评分。请先核对当前词卡。')
        const review = await localApi.answerBoundPetVocabulary(workspace.session.session_id, action.answer, action.vocabulary_prompt_id, action.vocabulary_card_version, turn.agent_turn_id)
        setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
          review.next_card?.prompt_session_id && review.next_card.card_version ? { kind: 'vocabulary', binding: {
            prompt_session_id: review.next_card.prompt_session_id, card_version: review.next_card.card_version } } : null)
        clientToolResult = review
        const expected = Array.isArray(review.result.expected)
          ? review.result.expected.join('、')
          : review.result.expected
        response = review.result.correct
          ? `${review.result.feedback}${review.next_card ? ` ${review.next_card.prompt}` : ' 今天的到期卡片已经复习完了。'}`
          : `${review.result.feedback} 这张卡的参考是“${expected}”。${review.next_card ? ` 我们先换一张：${review.next_card.prompt}` : ''}`
        if (!review.next_card) {
          const activeVocabulary = workspace.plan_items.find((item) =>
            item.status === 'active' && item.source_type === 'pet_daily' &&
            item.source_ref?.startsWith('pet-vocabulary-due:')
          )
          if (activeVocabulary) {
            const actualMinutes = effectiveActiveMinutes(activeVocabulary)
            if (!await runTool(() => localApi.completePlanItem(activeVocabulary.plan_item_id, actualMinutes, {
              type: 'child_checked',
              note: '到期PET词汇卡已通过语音逐张作答并保存',
              checklist: ['recall_attempted', 'uncertain_parts_marked'],
            }))) return
            const following = workspace.plan_items.find((item) =>
              item.plan_item_id !== activeVocabulary.plan_item_id && item.status === 'pending'
            )
            response += following
              ? ` 这项已经自动记录完成。下一项是“${planItemTitle(following)}”，准备好就说“开始任务”。`
              : ' 这项已经自动记录完成。'
          }
        }
      } else if (action.type === 'cancel_pet_vocabulary_review') {
        if (!action.vocabulary_prompt_id || !action.vocabulary_card_version) throw new VoiceActionUnavailableError('没有对应这句话的词卡记录，未取消其他词卡。')
        const cancelled = await localApi.cancelBoundPetVocabulary(workspace.session.session_id, action.vocabulary_prompt_id, action.vocabulary_card_version)
        setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id, null)
        clientToolResult = cancelled
        if (cancelled.status !== 'cancelled') throw new VoiceActionUnavailableError('原词卡状态已改变，未确认取消。')
      } else if (action.type === 'replay_diagnostic' || action.type === 'submit_diagnostic_answer' || action.type === 'cancel_diagnostic') {
        const replayRead: DiagnosticReplayReadDetail | null = action.type === 'replay_diagnostic'
          && action.diagnostic_prompt_id && action.diagnostic_item_version ? {
            sessionId: workspace.session.session_id, childId: workspace.session.child_id,
            promptId: action.diagnostic_prompt_id, itemVersion: action.diagnostic_item_version,
            requestId: turn.agent_turn_id, phase: 'begin',
          } : null
        if (replayRead && isCurrent()) window.dispatchEvent(new CustomEvent('hsc-diagnostic-replay-read', { detail: replayRead }))
        let diagnostic: Awaited<ReturnType<typeof runDiagnosticVoice>>
        try {
          diagnostic = await runDiagnosticVoice(workspace, { type: action.type, answer: action.answer,
            diagnostic_prompt_id: action.diagnostic_prompt_id, diagnostic_item_version: action.diagnostic_item_version }, turn.agent_turn_id, isCurrent)
        } catch (cause) {
          if (replayRead && isCurrent()) {
            const failed: DiagnosticReplayReadDetail = { ...replayRead, phase: 'failed', handled: false }
            window.dispatchEvent(new CustomEvent('hsc-diagnostic-replay-read', { detail: failed }))
            diagnosticReplayReadFailed = failed.handled === true
          }
          throw cause
        }
        response = diagnostic.response
        clientToolResult = diagnostic.result
        await followWritingFeedback(diagnostic.result.local_learning_evaluation)
        if (action.type !== 'cancel_diagnostic' && diagnostic.playbackAllowed !== false
          && diagnostic.result.status === 'waiting_answer' && diagnostic.result.prompt && isPetListening(diagnostic.result.prompt)) {
          const playback = await playPetListening(workspace.session.session_id, diagnostic.result.prompt, turn.agent_turn_id, isCurrent, voiceEventController.current.signal)
          response = playback.response
          clientToolResult = { diagnostic: diagnostic.result, playback }
        }
        speechPolicy.localOnly = true
        Object.assign(speechPolicy, originalAudioPromptPlayback(workspace.session.session_id, diagnostic.result, speechPolicy))
        window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated', { detail: { sessionId: workspace.session.session_id, result: diagnostic.result } }))
        await run(async () => undefined)
      } else if (action.type === 'control_pet_listening') {
        const binding = action.pet_listening_binding
        if (!binding || binding.child_id !== workspace.session.child_id || binding.session_id !== workspace.session.session_id
          || !['pause', 'resume', 'restart', 'cancel'].includes(action.command ?? '')
          || (turn.runtime_capability ?? action.capability) !== 'pet.listening_control') throw new VoiceActionUnavailableError('这句话没有绑定当前原音，没有改变其他播放。')
        if (action.command === 'pause' || action.command === 'cancel') stopPetListening(binding.session_id, binding.media_id)
        const controlled = await localApi.petListeningAgentControl(binding.session_id, turn.agent_turn_id)
        const media = controlled.media
        if (media.media_id !== binding.media_id || media.child_id !== binding.child_id || media.session_id !== binding.session_id
          || media.prompt_id !== binding.prompt_id || media.item_version !== binding.item_version) throw new VoiceActionUnavailableError('原音控制回执不匹配，未确认完成。')
        clientToolResult = controlled
        speechPolicy.localOnly = true
        if (controlled.replayed) response = '这次原音控制已有记录，未重复播放。需要再次听时请告诉我。'
        else if (action.command === 'resume' || action.command === 'restart') {
          const original = await localApi.diagnosticPrompt(binding.session_id, binding.prompt_id)
          if (original.status !== 'waiting_answer' || !original.prompt || original.prompt.item_version !== binding.item_version) throw new VoiceActionUnavailableError('原听力题已变化，没有播放另一道题。')
          const played = await playPetListening(binding.session_id, original.prompt, turn.agent_turn_id, isCurrent, voiceEventController.current.signal, 4, media)
          response = played.response
          clientToolResult = { control: controlled, playback: played }
        } else response = action.command === 'cancel' ? '本次原音已取消，已保存的学习记录保留。' : '原音已暂停。继续时会从这段原音开头播放，不把中断的一遍算作听完。'
      } else if (action.type === 'control_tutor_playback') {
        if (!action.tutor_playback_id || !action.tutor_source_version || !['pause', 'resume', 'previous', 'restart', 'cancel'].includes(action.command ?? '')) throw new VoiceActionUnavailableError('没有对应这句话的讲解进度，没有改变其他讲解。')
        const result = await tutorPlayback.command(action.command as 'pause' | 'resume' | 'previous' | 'restart' | 'cancel', {
          playbackId: action.tutor_playback_id, sourceVersion: action.tutor_source_version,
        })
        if (!result) throw new VoiceActionUnavailableError('目前没有可以恢复的讲解。')
        clientToolResult = { playback_id: result.playback_id, status: result.status, segment_index: result.segment_index, evidence_kind: result.evidence_kind }
        response = action.command === 'resume' ? '从保存的位置继续讲解。' : action.command === 'previous' ? '已经退回上一段，说继续讲解就从这里讲。'
          : action.command === 'restart' ? '已经回到开头，说继续讲解就重新开始。' : action.command === 'cancel' ? '本次讲解已取消，学习记录仍然保留。' : '讲解已暂停，进度已经保存。'
        if (action.command === 'resume') shouldSpeak = false
      } else if (action.type === 'record_tutor_feedback') {
        const previous = globalTutorTurn ?? workspace.latest_tutor_turn
        if (previous && previous.session_id === workspace.session.session_id && previous.child_id === workspace.session.child_id
          && Date.now() - Date.parse(previous.created_at) < 30 * 60_000) {
          if (!action.outcome) throw new VoiceActionUnavailableError('还需要确认你是理解了，还是卡在某一步。')
          const actionId = `${turn.agent_turn_id}-feedback`
          outcomeOwnedByRuntime = true
          const feedback = await commandApi.saveTutorFeedback(previous.turn_id, action.feedback_text || utterance, action.outcome!, actionId)
          response = action.outcome === 'understood' ? '我记下了你说已经理解，接下来用一道独立练习核对，不会仅凭这句话判定掌握。' : '我记下了你卡住的地方，我们可以换一种解释或再看一个提示。'
          await finishTutorCompletion(makeTutorCompletion(turn.agent_turn_id, actionId, 'record_tutor_feedback', response, { ...feedback }, speechPolicy.localOnly), isCurrent)
          return
        } else throw new VoiceActionUnavailableError('当前没有刚才的讲解可以评价。你可以直接告诉我哪里不会。')
      } else if (action.type === 'more_tutor_hint') {
        const previous = globalTutorTurn ?? workspace.latest_tutor_turn
        if (previous && previous.session_id === workspace.session.session_id && previous.child_id === workspace.session.child_id
          && Date.now() - Date.parse(previous.created_at) < 30 * 60_000 && previous.hint_level < 6) {
          const recovery: TutorToolRecovery = { version: 1, turnId: turn.agent_turn_id, actionId: `${turn.agent_turn_id}-hint`,
            actionType: 'more_tutor_hint', subject: previous.subject, question: previous.question, hintLevel: previous.hint_level + 1,
            parentTurnId: previous.turn_id, createdAt: new Date().toISOString(), attempts: 1, requiresExplicitRetryConfirmation: true }
          await draft.saveNow({ text, tutor_recovery: recovery })
          tutorRecoveryRef.current = recovery
          if (isCurrent()) setTutorRecovery(recovery)
          if (!isCurrent()) throw new SupersededDeviceCommand()
          outcomeOwnedByRuntime = true
          try {
            const taught = await commandApi.askTutor(workspace.session.session_id, previous.subject, previous.question, recovery.hintLevel, previous.turn_id, recovery.actionId)
            await finishTutorCompletion(makeTutorCompletion(turn.agent_turn_id, recovery.actionId, 'more_tutor_hint', taught.answer,
              tutorExecutionSummary(taught), speechPolicy.localOnly, taught), isCurrent)
          } catch (cause) {
            if (isCurrent()) {
              const sourceBlocked = cause instanceof ApiError ? tutorSourceBlock(cause.code) : undefined
              const next = sourceBlocked ? { ...recovery, sourceBlocked, requiresExplicitRetryConfirmation: false } : recovery
              tutorRecoveryRef.current = next
              setTutorRecovery(next)
              await draft.saveNow({ text, tutor_recovery: next })
              if (sourceBlocked) throw new VoiceActionUnavailableError(tutorSourceBlockMessage(sourceBlocked))
            }
            throw cause
          }
          return
        } else throw new VoiceActionUnavailableError('当前没有可以继续展开的提示。你可以先把问题告诉我。')
      } else if (action.type === 'update_reflection') {
        if (!await dispatchReflectionVoiceAction({ action: 'update', field: action.field, value: action.value }, voiceEventController.current.signal)) throw new VoiceActionUnavailableError('这项复盘还没有确认保存。原话和已填内容会保留；请稍后重试。')
      } else if (action.type === 'complete_reflection') {
        const afterSaved = native && onReflectionSaved ? async (saved: SessionWorkspaceState): Promise<void> => {
          const receipt = reflectionSavedReceipt(saved, workspace.session.session_id, workspace.session.child_id)
          // This is the actual save receipt, not another request to end learning.
          // Keep the exact same completion body for the one bounded ACK retry.
          const outcome: NativeClientOutcome = { final_response: '今天的复盘已保存。', tool_status: 'completed', result: {
            execution_receipt: { capability: turn.runtime_capability, execution_location: 'client_device_bridge',
              action_type: action.type, status: 'completed', completed_at: new Date().toISOString(), output: receipt }
          } }
          try { keepReflectionCompletion(turn.agent_turn_id, receipt) } catch { /* The original business save remains durable. */ }
          await onReflectionSaved(saved, async () => {
            // React retires the real voice owner when the saved session becomes
            // completed. Never manufacture a current owner to submit the ACK.
            const deadline = performance.now() + 1500
            while (native.port.current() && performance.now() < deadline) await new Promise<void>(resolve => setTimeout(resolve, 25))
            for (let attempt = 0; attempt < 2; attempt++) {
              try {
                await native.port.complete(outcome)
                clearReflectionCompletion(turn.agent_turn_id)
                return
              } catch { /* At most one identical metadata retry, never save again. */ }
            }
            void window.desktop?.recordRendererDiagnostic?.({event:'voice.realtime.native_operation',level:'error',
              message:'复盘已经保存，原完成回执尚未同步；没有重复结束学习。',details:{native_operation:'client',native_phase:'complete',native_capability:'reflection.complete',status:'failed'}})
          })
        } : undefined
        // This native action owns its save/ACK boundary. Real session retirement
        // must never send the generic opposing CLIENT_COMMAND_CANCELLED result.
        if (afterSaved) outcomeOwnedByRuntime = true
        if (!await dispatchReflectionVoiceAction({ action: 'complete', skipped: action.skipped, afterSaved }, voiceEventController.current.signal)) throw new VoiceActionUnavailableError('复盘还没有确认保存。请再说“保存复盘”；原有内容会保留。')
      } else if (action.type === 'review_current_mistake') {
        if (action.decision !== 'exclude') throw new VoiceActionUnavailableError('孩子不需要确认题目对错；系统会按老师批改、标准答案或出题时冻结的评分规则核对。')
        if (!await dispatchMistakeVoiceAction({ action: 'review', decision: 'exclude' }, voiceEventController.current.signal)
          && !await dispatchMistakeCaptureVoiceAction(workspace.session.session_id, workspace.session.child_id, 'skip', undefined, voiceEventController.current.signal)) throw new VoiceActionUnavailableError('当前没有可排除的识别记录。可以继续说明题干或实际作答哪里识别错了。')
      } else if (action.type === 'diagnose_current_mistake') {
        if (!await dispatchMistakeVoiceAction({ action: 'diagnose', errorType: action.error_type, explanation: action.explanation }, voiceEventController.current.signal)) throw new VoiceActionUnavailableError('当前错题还没到分析错因这一步。可以说“重试加载错题”核对原记录。')
      } else if (action.type === 'complete_mistake_remediation') {
        if (!await dispatchMistakeVoiceAction({ action: 'remediate', outcome: action.outcome === 'still_stuck' ? 'still_stuck' : 'completed', notes: action.notes }, voiceEventController.current.signal)) throw new VoiceActionUnavailableError('当前错题还没到补救确认这一步。可以说“重试加载错题”核对原记录。')
      } else if (action.type === 'submit_mistake_answer') {
        if (!await dispatchMistakeVoiceAction({ action: 'answer', answer: action.answer }, voiceEventController.current.signal)) throw new VoiceActionUnavailableError('当前没有等待作答的错题强化题。可以说“重试加载错题”核对原记录，请不要重复提交答案。')
      } else if (action.type === 'privacy_pause') {
        preemptPrivacyStop(turn)
        return
      } else if (action.type === 'pause_observation') {
        clientToolResult = { device_type: 'camera', status: 'removed' }
        response = OBSERVATION_CAMERA_REMOVED_MESSAGE
        speechPolicy.localOnly = true
      } else if (action.type === 'resume_observation') {
        throw new VoiceActionUnavailableError(OBSERVATION_CAMERA_REMOVED_MESSAGE)
      } else if (action.type === 'reset_behavior_personalization') {
        if (!await runTool(() => localApi.resetSessionBehaviorPersonalization(workspace.session.session_id))) return
      } else if (action.type === 'control_document_monitor' && ['pause', 'resume', 'inspect', 'review'].includes(action.command ?? '')) {
        const command = action.command as DocumentMonitorVoiceDetail['action']
        const capture = action.voice_input_binding
        const original = capture?.question_reference
        let originalHandled = false
        if (command === 'review' && original?.kind === 'document') {
          if (capture?.child_id !== workspace.session.child_id || capture.session_id !== workspace.session.session_id
              || capture.question_validation !== 'valid' || !action.voice_input_id) {
            throw new VoiceActionUnavailableError('开口时的原页已经变化或不可用；我没有换到另一页。请重新说明你想看哪一页。')
          }
          if (!await runTool(async () => {
            const page = await localApi.originalDocumentVoiceSource(workspace.session.session_id, turn.agent_turn_id, voiceEventController.current.signal)
            if (!isCurrent()) throw new SupersededDeviceCommand()
            if (page.child_id !== capture.child_id || page.session_id !== capture.session_id
                || page.revision_id !== original.binding.revision_id || page.artifact_id !== original.binding.artifact_id
                || page.content_sha256 !== original.binding.content_sha256 || page.page_source?.sha256 !== original.binding.page_sha256
                || page.source_voice_input_id !== action.voice_input_id || page.local_only !== true
                || page.cloud_transmission_allowed !== false || page.mastery_update_allowed !== false) {
              throw new VoiceActionUnavailableError('原页读取回执不一致，没有改用当前页面。')
            }
            const preview = page.extracted_text.replace(/\s+/g, ' ').trim().slice(0, 120)
            response = preview ? `我读回你开口时那张原页：“${preview}”。这还是识别候选；你可以说哪里识别错了，或继续说你卡在哪一步。`
              : '开口时的原页没有可靠文字，不能猜题目。请重新说明你想看哪一页。'
            if (page.page_status === 'blocked') response = '开口时的原页已保留，但解析仍受阻，不能判断题目或作业情况。识别候选留在本机，未开始讲解。'
            speechPolicy.localOnly = true
            const saved = await requestOwnedVoiceEvent(DOCUMENT_REVIEW_REVISION_EVENT,
              { sessionId: capture.session_id, childId: capture.child_id, revision: page,
                signal: voiceEventController.current.signal }, voiceEventController.current.signal)
            if (!isCurrent()) throw new SupersededDeviceCommand()
            if (!saved) throw new VoiceActionUnavailableError('当前学习工作区还没有确认保存原页，未开始讲解。')
            return { artifact_id: page.artifact_id, revision_id: page.revision_id,
              page_status: page.page_status, response_text: response }
          })) return
          originalHandled = true
        }
        const handled = originalHandled || (command === 'pause' || command === 'resume'
          ? await runTool(() => requestCameraDevice(workspace.session.session_id, 'document_camera', command, voiceEventController.current.signal))
          : await dispatchDocumentMonitorVoiceAction(workspace.session.session_id, command, voiceEventController.current.signal, (output) => { clientToolResult = output; response = output.response_text; speechPolicy.localOnly = true }, action.defer_document_review === true))
        if (!handled && (command === 'pause' || command === 'resume')) return
        if (command === 'pause') response = '高拍仪已停止读取并确认释放，可以继续语音对话。'
        if (command === 'resume') response = '高拍仪已收到画面，等页面清晰稳定后再识别。'
        if (action.feedback) {
          window.dispatchEvent(new CustomEvent('hsc-behavior-intervention-feedback', {
            detail: { sessionId: workspace.session.session_id, feedback: action.feedback, responseKind: action.response_kind }
          }))
        }
        if (!handled) {
          if (workspace.session.state === 'check_in') {
            throw new VoiceActionUnavailableError('签到时，高拍仪只显示预览。进入作业登记或开始学习后，再说“检查当前页”。')
          }
          response = command === 'review'
            ? '现在还没有可核对的页面。请保持页面稳定，等我完成一次本地识别后再说“核对这一页”。'
            : '这次页面检查没有完成，我没有新的页面结果。请保持页面稳定，稍后再说“检查当前页”；如果仍没有响应，请检查高拍仪连接。'
          throw new VoiceActionUnavailableError(response)
        }
      } else if (action.type === 'control_mistake_activity' && (action.command === 'reload' || action.command === 'sync')) {
        if (!await dispatchMistakeRecoveryVoiceAction(action.command, voiceEventController.current.signal)) {
          response = action.command === 'sync'
            ? '当前没有等待同步的错题进度。'
            : '当前任务不是错题强化任务。'
          throw new VoiceActionUnavailableError(`${response}可以说“继续当前任务”。`)
        }
      } else if (action.type === 'control_mistake_capture' && ['open', 'set_answer', 'correct_prompt', 'save', 'skip', 'close'].includes(action.command ?? '')) {
        const captureAction = action.command as MistakeCaptureVoiceAction
        const value = captureAction === 'set_answer' ? action.answer : captureAction === 'correct_prompt' ? action.corrected_text : undefined
        if (!await dispatchMistakeCaptureVoiceAction(workspace.session.session_id, workspace.session.child_id, captureAction, value,
          voiceEventController.current.signal, dialogue => { response = dialogue.response_text; clientToolResult = dialogue })) {
          response = captureAction === 'open'
            ? '现在没有刚拍摄或等待整理的题目。先说“帮我看这题”。'
            : '当前没有正在核对的作答记录。先说“帮我看这题”。'
          throw new VoiceActionUnavailableError(response)
        }
        if (captureAction === 'open') shouldSpeak = false // The verified dialogue owns its question readback.
      } else if (action.type === 'correct_observation') {
        if (!await runTool(() => localApi.correctObservation(workspace.session.session_id, action.correction ?? utterance))) return
      } else if (action.type === 'stop_speech') {
        cancelLocalSpeech()
        shouldSpeak = false
      } else if (action.type === 'behavior_intervention_feedback') {
        window.dispatchEvent(new CustomEvent('hsc-behavior-intervention-feedback', {
          detail: { sessionId: workspace.session.session_id, feedback: action.feedback, responseKind: action.response_kind }
        }))
      } else if (action.type === 'snooze') {
        cancelLocalSpeech()
        shouldSpeak = false
        if (!await runTool(() => Promise.resolve())) return
      } else if (action.type === 'request_end') {
        const unsettled = workspace.plan_items.filter((item) => !['completed', 'rescheduled', 'skipped'].includes(item.status))
        if (workspace.session.state === 'study' && unsettled.length === 0) {
          if (!await runTool(() => localApi.transition(workspace.session.session_id, 'study', 'reflection', 'voice_end_requested'))) return
        } else if (unsettled.length > 0) {
          response = `还有${unsettled.length}项任务没有完成或延期。请直接说“完成了”“暂停这项”或“这项延期”，我会保存你的决定。`
        }
      }
      if (!isCurrent()) throw new SupersededDeviceCommand()
      let nativeFormalDelivery:{status:'ended'|'not_delivered'|'unconfirmed';kind:'protected_runtime_speech'}|undefined
      if(native&&completedByRuntime&&shouldSpeak&&!speechSuppressedRef.current){
        speechPolicy.localOnly=true;speechPolicy.skipPublicReplyProvider=true
        nativeFormalDelivery=await new Promise(resolve=>{
          let finished=false
          const finish=(status:'ended'|'not_delivered'|'unconfirmed'):void=>{if(finished)return;finished=true;clearTimeout(timer);resolve({status,kind:'protected_runtime_speech'})}
          const timer=setTimeout(()=>finish('unconfirmed'),90000)
          speakReply(response,undefined,{...speechPolicy,onPlaybackEnd:()=>{speechPolicy.onPlaybackEnd?.();finish('ended')},
            onNotDelivered:reason=>{speechPolicy.onNotDelivered?.(reason);finish('not_delivered')},
            onPlaybackAbandoned:()=>{speechPolicy.onPlaybackAbandoned?.();finish('unconfirmed')}})
        })
        shouldSpeak=false
      }
      if(native&&completedByRuntime&&((turn.runtime_execution_location??action.execution_location)==='server'||turn.runtime_capability==='learning.evaluate_response'||turn.runtime_capability==='mistake.learn')){
        native.record({final_response:response,tool_status:'completed',result:{...(nativeFormalDelivery?{local_delivery:nativeFormalDelivery}:{}),client_bridge_receipt:{capability:turn.runtime_capability??action.capability,execution_location:'client_device_bridge',action_type:action.type,status:'completed',completed_at:new Date().toISOString(),output:boundedExecutionResult(clientToolResult)}}})
      }
      if (!completedByRuntime) {
        const executionLocation = turn.runtime_execution_location ?? action.execution_location
        const requiresExecutionReceipt = executionLocation === 'client_device_bridge' || executionLocation === 'local_model'
        if (requiresExecutionReceipt) {
          const receipt = {
            capability: turn.runtime_capability ?? action.capability ?? 'unknown',
            execution_location: executionLocation,
            action_type: action.type,
            status: 'completed',
            completed_at: new Date().toISOString(),
            output: boundedExecutionResult(clientToolResult),
          }
          clientCompletionSubmitted = true
          await commandApi.completeAgentTurn(turn.agent_turn_id, response, 'completed', undefined, {
            execution_receipt: receipt,
            ...(executionLocation === 'client_device_bridge' ? { client_bridge_receipt: receipt } : {}),
          })
        } else {
          clientCompletionSubmitted = true
          await commandApi.completeAgentTurn(turn.agent_turn_id, response, 'completed')
        }
        await refreshAgentRuntime()
      }
      if (!isCurrent()) throw new SupersededDeviceCommand()
      setTutorRecovery(null)
      if (pendingUtterancesRef.current.length === 0 && voiceInputQueue.pendingCount === 0 && mountedRef.current) {
        if (!native&&documentGoalToContinue) {
          continuationHandledRef.current.delete(documentGoalToContinue)
          afterSpeech = scheduleContinuation(documentGoalToContinue)
        }
        if (!native&&!followupOwnsContinuation && turn.after_execution === 'replan' && !afterSpeech && !['request_capture', 'retry_capture'].includes(action.type)
            && !(action.type === 'control_document_monitor' && action.defer_document_review === true && ['inspect', 'review'].includes(action.command ?? ''))) {
          afterSpeech = scheduleContinuation(turn.agent_turn_id)
        }
        setReply(response)
        if (shouldSpeak && !speechSuppressedRef.current&&(!native||speechPolicy.localOnly)) speakReply(response, afterSpeech, speechPolicy)
        else afterSpeech?.()
        if (!native&&!preserveInputRef.current) {
          if (!ownsSubmittedDraft || currentTextRef.current !== submittedDraftText) {
            await draft.saveNow({ text: currentTextRef.current, tutor_recovery: null })
          } else {
            await draft.clear().catch(() => undefined)
            if (currentTextRef.current === submittedDraftText) setText('')
            else await draft.saveNow({ text: currentTextRef.current, tutor_recovery: null })
          }
        }
      }
    } catch (cause) {
      // Once sent, a terminal receipt may already be committed even if its
      // response is lost or the UI owner leaves. Never send an opposing result.
      if (clientCompletionSubmitted) {
        if (isCurrent()) {
          preserveInputRef.current = true
          const message = '原操作的结果回执尚未核对，原话仍保留，没有重复执行。可以说“核对刚才请求”，不用重复提交。'
          setReply(message)
          if (!speechSuppressedRef.current) speakReply(message, undefined, { localOnly: true })
        }
        return
      }
      if (localRecoveryOnly) {
        if (isCurrent()) {
          preserveInputRef.current = true
          setReply(LOCAL_RECOVERY_FAILURE)
          speakReply(LOCAL_RECOVERY_FAILURE, undefined, { localOnly: true })
        }
        return
      }
      if (isCurrent() && cause instanceof ApiError && cause.status === 409
        && cause.code?.startsWith('PENDING_EVALUATION_CONTROL_')) {
        preserveInputRef.current = true
        const message = '原评价记录或当前会话已变化，没有提交新答案或重复评价。请先核对原记录。'
        setEvaluationStatus(message)
        setReply(message)
        if (!speechSuppressedRef.current) speakReply(message, undefined, { localOnly: true })
        return
      }
      if (isCurrent() && cause instanceof ApiError && cause.status === 409
        && ['LEARNING_EVALUATION_SOURCE_CHANGED', 'TUTOR_SOURCE_AUTHORIZATION_CHANGED'].includes(cause.code ?? '')) {
        preserveInputRef.current = true
        try {
          if (persistedTurn) finishPendingEvaluation(persistedTurn.agent_turn_id)
        } catch {
          setEvaluationStatus('评价已停止，但本机恢复记录尚未清理成功。原回答已保留，请家长检查本机存储。')
          return
        }
        const message = cause.code === 'TUTOR_SOURCE_AUTHORIZATION_CHANGED'
          ? '这次题目或云端许可发生了变化，我已停止后续讲解请求，原问题仍然保留。需要先核对当前任务，不用重复提交答案。'
          : '这次题目、原稿或评价许可发生了变化，我已停止评价，原回答仍然保留。需要家长核对后再继续，不用重复提交答案。'
        setEvaluationStatus(message)
        if (pendingUtterancesRef.current.length === 0 && voiceInputQueue.pendingCount === 0) {
          setReply(message)
          if (!speechSuppressedRef.current) speakReply(message, undefined, { localOnly: true })
        }
        return
      }
      if (cause instanceof ApiError && cause.status === 409 && ['LEARNING_EVALUATION_IN_PROGRESS', 'LEARNING_EVALUATION_RESULT_UNKNOWN'].includes(cause.code ?? '')) {
        preserveInputRef.current = true
        const receipt = pendingEvaluationRef.current
        if (receipt && receipt.turnId === persistedTurn?.agent_turn_id) {
          try { keepPendingEvaluation({ ...receipt, status: cause.code === 'LEARNING_EVALUATION_RESULT_UNKNOWN' ? 'unknown' : 'in_progress' }) }
          catch { if (isCurrent()) setEvaluationStatus('原作答评价恢复状态未能保存，请保持页面并请家长检查本机存储。') }
        }
        if (isCurrent()) setEvaluationStatus(cause.code === 'LEARNING_EVALUATION_IN_PROGRESS'
          ? '这次作答评价仍在处理中，原回答与证据已保留；不会自动重复调用。'
          : '这次作答评价的结果尚未确认，原回答与证据已保留；不会自动重试或覆盖状态。')
        return
      }
      if (!isCurrent() || cause instanceof SupersededDeviceCommand) {
        if (persistedTurn && !outcomeOwnedByRuntime) void commandApi.completeAgentTurn(persistedTurn.agent_turn_id, '操作因隐私停止而取消，未继续执行。', 'failed', 'CLIENT_COMMAND_CANCELLED').catch(() => undefined)
        return
      }
      if (cause instanceof ApiError && isNonRepeatableAgentHandoff(cause.status, cause.code)) {
        preserveInputRef.current = true
        if (pendingUtterancesRef.current.length === 0 && voiceInputQueue.pendingCount === 0 && mountedRef.current) {
          setReply(AGENT_HANDOFF_UNCONFIRMED)
          if (!speechSuppressedRef.current) speakReply(AGENT_HANDOFF_UNCONFIRMED, undefined, { localOnly: true })
        }
        return
      }
      const response = resumeCheck && !persistedTurn
        ? '原话的续接结果还未确认，原编号和原话仍保留，没有自动重做。可以先核对刚才请求，不用重复提交答案。'
        : diagnosticReplayReadFailed ? DIAGNOSTIC_READ_FAILURE_MESSAGE : cause instanceof ProfileSwitchPendingError ? cause.message : cause instanceof VoiceActionTimeoutError
        ? '这一步没有收到处理回执，不能确认是否保存，也没有自动重试。可以说“重试加载错题”核对原记录，或“继续当前任务”；高拍仪可以再说“查看当前页面”。请不要直接重复提交刚才的答案。'
        : cause instanceof VoiceActionUnavailableError ? cause.message : cause instanceof VoiceRecoverySaveError
          ? '恢复记录没有保存成功，暂不继续请求。请保持页面，可以说“重试讲解”。'
          : cause instanceof ApiError && ['AGENT_REQUEST_UNCONFIRMED', 'AGENT_REQUEST_SAVE_FAILED'].includes(cause.code ?? '') ? cause.message
          : !persistedTurn && cause instanceof ApiError && (cause.status ?? 0) >= 500 ? AGENT_REQUEST_UNCONFIRMED
          : cause instanceof ApiError ? supplyResumeConflictMessage(cause) ?? '这一步暂时没有完成。你可以再试一次；如果还是不行，请家长帮忙。' : '我刚才没有处理成功，请再说一次。'
      if (persistedTurn && !outcomeOwnedByRuntime) {
        await localApi.completeAgentTurn(persistedTurn.agent_turn_id, response, 'failed', cause instanceof VoiceActionTimeoutError ? 'CLIENT_VOICE_RESULT_UNCONFIRMED' : cause instanceof VoiceActionUnavailableError ? 'CLIENT_VOICE_ACTION_UNAVAILABLE' : cause instanceof ApiError ? cause.code : 'CLIENT_TOOL_ERROR').catch(() => undefined)
      }
      if (pendingUtterancesRef.current.length === 0 && voiceInputQueue.pendingCount === 0 && mountedRef.current) {
        setReply(response)
        if (!speechSuppressedRef.current) speakReply(response, undefined, { localOnly: true })
      }
    } finally {
      if (!mountedRef.current || generation !== commandGenerationRef.current) return
      if (resumedAdmission && persistedTurn) {
        if (!settleUnsentResumeJournal(resumedAdmission)) void window.desktop?.recordRendererDiagnostic?.({
          event: 'voice.realtime.resume_receipt_retained', level: 'warn',
          message: 'Original resume identities retained for readonly reconciliation.',
          details: { input_id: resumedAdmission.inputId, voice_input_id: resumedAdmission.voiceInputId },
        })
      }
      busyRef.current = false
      const next = pendingUtterancesRef.current.shift()
      if (mountedRef.current) setQueuedUtteranceCount(pendingUtterancesRef.current.length)
      if (next && mountedRef.current) void submitLatest.current(next.text, undefined, next.overlap, next.voiceInputId, next.pendingScope, next.localRecoveryOnly, next.carryoverArrival, undefined, next.handoff)
      else if (mountedRef.current) setBusy(false)
      continuationDriver.flush()
    }
  }
  submitLatest.current = processVoiceInput
  preparedTurnRef.current = (turn, current) => processVoiceInput('', voiceEventController.current.signal, undefined, undefined, null, false, null, {
    turn, current: () => current() && realtimeVoiceRef.current.speechActive === false && realtimeVoiceRef.current.pendingUtterances === 0,
  })

  const runQwenRealtimeTurn = async (utterance: string, inputSource?: QwenRealtimeInputCapture,
    admission?: Promise<RealtimeAdmissionOutcome>): Promise<QwenRealtimeToolResult> => {
    if (realtimeSpeechCaptureRef.current) throw new Error('回复采集入口仍被占用，这句话尚未接纳。')
    const capture: RealtimeSpeechCapture = { messages: [], localPlaybackUsed: false, inputSource }
    realtimeSpeechCaptureRef.current = capture
    try {
      const sourceStatus = realtimeInputSourceStatus(inputSource, workspace.session.session_id, workspace.session.child_id)
      void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.input_source_checked', level: 'info',
        details: { input_stage: 'dispatch', source_status: sourceStatus } })
      if (sourceStatus !== 'current' && !isLocalPrivacyStop(utterance)) {
        preserveInputRef.current = true
        speakReply(realtimeInputSourceMessage(sourceStatus), undefined, { localOnly: true })
      } else if ((isAgentRequestCheck(utterance) || shouldResumeRealtimeInput(utterance, workspace.session.session_id, workspace.session.child_id)) && inputSource) {
        // Recovery is read-only and must remain reachable when new handoff
        // storage is full or unavailable. No old task is replayed here.
        await submit(utterance, undefined, undefined, undefined, isAgentRequestCheck(utterance), null, inputSource.reference)
      } else if (inputSource) {
        let admitted: Awaited<ReturnType<typeof commandApi.admitRealtimeVoice>>
        try {
          const outcome = await admission
          if (!outcome?.record) throw new Error('Realtime admission not confirmed')
          admitted = outcome.record
        } catch {
          if (!mountedRef.current || speechSuppressedRef.current) return { responseText: 'input_cancelled', speak: false }
          preserveInputRef.current = true
          speakReply('刚才原话的保存回执还没有确认，这句话没有交给学习任务。不会自动重做，请先核对本机记录。', undefined, { localOnly: true })
          return { responseText: 'input_admission_unconfirmed', speak: false }
        }
        void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.input_admitted', level: 'info',
          details: { input_id: inputSource.localInputId, voice_input_id: admitted.voice_input_id, source_kind: 'realtime_transcript', raw_audio_available: false } })
        if (!mountedRef.current || speechSuppressedRef.current) return { responseText: 'input_cancelled', speak: false }
        const afterAdmission = realtimeInputSourceStatus(inputSource, workspace.session.session_id, workspace.session.child_id)
        if (afterAdmission !== 'current') {
          preserveInputRef.current = true
          speakReply(realtimeInputSourceMessage(afterAdmission), undefined, { localOnly: true })
        } else {
          try {
            const attempt = realtimeAdmissionsRef.current.get(inputSource.localInputId)
            if (!attempt) throw new Error('Missing original handoff')
            attempt.handoff = updateRealtimeAdmission(attempt.handoff, 'dispatching')
          } catch {
            preserveInputRef.current = true
            speakReply('原话已保存，但学习任务的交接记录还未确认，没有执行。请先核对刚才请求。', undefined, { localOnly: true })
            return { responseText: 'input_handoff_unconfirmed', speak: false }
          }
          await submit(utterance, undefined, admitted.voice_input_id, inputSource.reference.pending_evaluation_turn_id,
            inputSource.reference.local_recovery_only === true, inputSource.carryoverConfirmationId, inputSource.reference)
        }
      }
    } finally {
      if (realtimeSpeechCaptureRef.current === capture) realtimeSpeechCaptureRef.current = null
    }
    if (!mountedRef.current || speechSuppressedRef.current) return { responseText: 'reply_cancelled', speak: false }
    if (capture.tutorPlaybackOwned) return { responseText: 'tutor_playback_owned', speak: false }
    const selected = capture.messages.at(-1)
    if (!selected) {
      // Local-only content has already gone through the existing local speech
      // boundary. Return no private text to Qwen and ask it to stay silent.
      if (capture.localPlaybackUsed) return { responseText: 'local_reply_already_delivered', speak: false }
      return { responseText: '我没有得到可播报的处理结果，请再说一遍。' }
    }
    const policy = selected.policy
    if (policy?.beforePlayback) {
      const allowed = await policy.beforePlayback(new AbortController().signal)
      if (!allowed) {
        policy.onPlaybackAbandoned?.()
        return { responseText: 'expired_reply_not_delivered', speak: false }
      }
    }
    const generation = commandGenerationRef.current
    let deliverySettled = false
    const ownsReply = (): boolean => mountedRef.current && !speechSuppressedRef.current
      && generation === commandGenerationRef.current && (policy?.isCurrent?.() ?? true)
    const undelivered = (unconfirmed: boolean): void => {
      if (deliverySettled) return
      deliverySettled = true
      if (ownsReply()) setVoiceDelivery(unconfirmed ? 'unconfirmed' : 'interrupted')
      policy?.onPlaybackAbandoned?.()
      policy?.onNotDelivered?.(unconfirmed ? 'unavailable' : 'cancelled')
    }
    return {
      responseText: selected.text,
      onPlaybackStart: () => {
        if (deliverySettled || !ownsReply()) return
        setVoiceDelivery('natural')
        policy?.onPlaybackStart?.()
      },
      onPlaybackEnd: () => {
        if (deliverySettled) return
        if (!ownsReply()) { undelivered(false); return }
        deliverySettled = true
        policy?.onPlaybackEnd?.()
        selected.onFinished?.()
      },
      onPlaybackAbandoned: () => undelivered(false),
      onPlaybackUnconfirmed: () => undelivered(true),
    }
  }

  const admitQwenRealtimeInput = (utterance: string, inputSource?: QwenRealtimeInputCapture): Promise<RealtimeAdmissionOutcome> | undefined => {
    if (isAgentRequestCheck(utterance) || shouldResumeRealtimeInput(utterance, workspace.session.session_id, workspace.session.child_id)) return undefined
    if (!inputSource || !mountedRef.current || speechSuppressedRef.current) return undefined
    const existing = realtimeAdmissionsRef.current.get(inputSource.localInputId)
    if (existing) return existing.utterance === utterance.trim() && sameRealtimeInputCapture(existing.capture, inputSource)
      ? existing.admission : Promise.resolve({ failed: true })
    const source = realtimeInputSourceStatus(inputSource, workspace.session.session_id, workspace.session.child_id)
    // A changed original question can be saved for clarification, never rebound
    // to the page visible at dispatch. Missing onset/owner remains inadmissible.
    if (source !== 'current' && source !== 'source_changed') return undefined
    const capture = structuredClone(inputSource)
    let handoff: RealtimeAdmissionHandoff
    try { handoff = prepareRealtimeAdmission(capture.sessionId, capture.reference.child_id, capture.localInputId) }
    catch { return Promise.resolve({ failed: true }) }
    realtimeAdmissionOwnersRef.current.set(capture.localInputId, capture.sessionId)
    let attempt!: RealtimeAdmissionAttempt
    const admission = commandApi.admitRealtimeVoice(capture.sessionId, capture.localInputId, utterance.trim(),
      capture.capturedAt, capture.reference, voiceEventController.current.signal)
      .then(record => {
        attempt.handoff = updateRealtimeAdmission(attempt.handoff, 'stored', record.voice_input_id)
        return { record }
      }).catch(() => ({ failed: true as const }))
    attempt = { utterance: utterance.trim(), capture, admission, handoff }
    realtimeAdmissionsRef.current.set(capture.localInputId, attempt)
    return admission
  }

  const handleQwenRealtimeInputCompleted = (utterance: string, capture?: QwenRealtimeInputCapture): void => {
    if (isLocalPrivacyStop(utterance)) {
      if (mountedRef.current) preemptPrivacyStop()
      return
    }
    void window.desktop?.recordRendererDiagnostic?.({ event: 'voice.realtime.input_source_checked', level: 'info',
      details: { input_stage: 'admission', source_status: realtimeInputSourceStatus(capture,
        workspace.session.session_id, workspace.session.child_id) } })
    void admitQwenRealtimeInput(utterance, capture)
  }

  const preserveTerminalRealtimeInputs = async (inputs: Array<{ utterance: string; capture?: QwenRealtimeInputCapture }>, signal: AbortSignal, retry = false): Promise<boolean> => {
    if (!inputs.length || signal.aborted || !mountedRef.current || speechSuppressedRef.current) return false
    const sessionId = workspace.session.session_id, childId = workspace.session.child_id
    const generation = commandGenerationRef.current, ownerSignal = voiceEventController.current.signal
    const current = () => !signal.aborted && !ownerSignal.aborted && mountedRef.current && !speechSuppressedRef.current
      && !privacyStopPending(sessionId) && commandGenerationRef.current === generation
      && ordinarySessionRef.current.session_id === sessionId && ordinarySessionRef.current.child_id === childId
    const outcomes = await Promise.all(inputs.map(async input => {
      const capture = input.capture
      if (!current() || !capture || !['current', 'source_changed'].includes(realtimeInputSourceStatus(capture, sessionId, childId))) return false
      const existingFlight = terminalAdmissionFlights.current.get(capture.localInputId)
      if (existingFlight) {
        const owner = realtimeAdmissionsRef.current.get(capture.localInputId)
        return owner?.utterance === input.utterance.trim() && sameRealtimeInputCapture(owner.capture, capture) ? existingFlight : false
      }
      const flight = (async () => {
        let owner = realtimeAdmissionsRef.current.get(capture.localInputId)
        if (!owner && retry) {
          // Component remount keeps the original only in the recorder's
          // protected memory; restore ownership only from its scoped journal.
          let journal: RealtimeAdmissionHandoff | undefined
          try { journal = pendingRealtimeAdmissions(sessionId, childId).find(item => item.inputId === capture.localInputId) }
          catch { return false }
          if (journal && journal.phase !== 'cancel_requested' && journal.phase !== 'dispatching' && current()) {
            owner = { utterance: input.utterance.trim(), capture: structuredClone(capture),
              admission: Promise.resolve({ failed: true }), handoff: journal }
            realtimeAdmissionsRef.current.set(capture.localInputId, owner)
            realtimeAdmissionOwnersRef.current.set(capture.localInputId, sessionId)
          }
          // A missing or cancelled journal is never authority to recreate an
          // old original after a component or privacy lifecycle change.
          if (!owner) return false
        }
        if (!owner) {
          const outcome = await admitQwenRealtimeInput(input.utterance, capture)
          const created = realtimeAdmissionsRef.current.get(capture.localInputId)
          return Boolean(current() && outcome?.record && created?.handoff.phase === 'stored'
            && created.handoff.voiceInputId === outcome.record.voice_input_id)
        }
        if (owner.utterance !== input.utterance.trim() || !sameRealtimeInputCapture(owner.capture, capture)) return false
        if (!retry) {
          const outcome = await owner.admission
          return Boolean(current() && outcome?.record && owner.handoff.phase === 'stored'
            && owner.handoff.voiceInputId === outcome.record.voice_input_id)
        }
        // The journal is an identity-only CAS; never replace a cancellation or
        // another owner's update, even when an old network reply arrives late.
        const unchanged = (expected: RealtimeAdmissionHandoff) => current()
          && realtimeAdmissionsRef.current.get(capture.localInputId) === owner
          && JSON.stringify(owner.handoff) === JSON.stringify(expected)
          && pendingRealtimeAdmissions(sessionId, childId).some(item => JSON.stringify(item) === JSON.stringify(expected))
        const expected = structuredClone(owner.handoff)
        if (!unchanged(expected) || expected.phase === 'cancel_requested' || expected.phase === 'dispatching') return false
        if (expected.phase === 'stored') return true
        const controller = new AbortController()
        const stop = () => controller.abort()
        signal.addEventListener('abort', stop, { once: true })
        ownerSignal.addEventListener('abort', stop, { once: true })
        try {
          if (!unchanged(expected)) return false
          const receipt = await commandApi.realtimeAdmissionReceipt(sessionId, capture.localInputId, controller.signal)
          if (!unchanged(expected) || controller.signal.aborted || receipt.session_id !== sessionId
            || receipt.child_id !== childId || receipt.client_action_id !== capture.localInputId
            || receipt.local_only !== true || receipt.cloud_transmission_allowed !== false
            || receipt.replay_allowed !== false || receipt.mastery_update_allowed !== false) return false
          let voiceId: string
          if (receipt.status === 'completed' && typeof receipt.voice_input_id === 'string'
            && /^[A-Za-z0-9_-]{8,128}$/.test(receipt.voice_input_id)) voiceId = receipt.voice_input_id
          else if (['not_found', 'prepared'].includes(receipt.status) && receipt.voice_input_id === null) {
            // A read is only a snapshot: repeat the same immutable identity and
            // original payload; the local service checks both transactionally.
            const record = await commandApi.admitRealtimeVoice(sessionId, capture.localInputId, owner.utterance,
              capture.capturedAt, capture.reference, controller.signal)
            if (!unchanged(expected) || controller.signal.aborted || record.session_id !== sessionId
              || record.child_id !== childId || record.client_action_id !== capture.localInputId
              || record.status !== 'completed' || record.source_kind !== 'realtime_transcript'
              || record.raw_audio_available !== false || record.original_transcript !== owner.utterance
              || !/^[A-Za-z0-9_-]{8,128}$/.test(record.voice_input_id)) return false
            voiceId = record.voice_input_id
          } else return false
          if (!unchanged(expected)) return false
          owner.handoff = updateRealtimeAdmission(expected, 'stored', voiceId)
          return current()
        } catch { return false }
        finally { signal.removeEventListener('abort', stop); ownerSignal.removeEventListener('abort', stop) }
      })()
      terminalAdmissionFlights.current.set(capture.localInputId, flight)
      try { return await flight }
      finally { if (terminalAdmissionFlights.current.get(capture.localInputId) === flight) terminalAdmissionFlights.current.delete(capture.localInputId) }
    }))
    if (!current() || !outcomes.every(Boolean)) return false
    setWakeSettingsMessage(previous => previous && (previous.startsWith('云语音服务拒绝') || previous.startsWith('云语音账户余额不足'))
      ? `${previous} 待处理原话已确认保存，未自动重复执行。` : '待处理原话已确认保存，未自动重复执行。')
    return true
  }

  const handleQwenRealtimeTurn = (utterance: string, inputSource?: QwenRealtimeInputCapture): Promise<QwenRealtimeToolResult> => {
    if (isLocalPrivacyStop(utterance)) {
      if (mountedRef.current) preemptPrivacyStop()
      return Promise.resolve({ responseText: 'local_privacy_stop', speak: false })
    }
    const frozenInput = inputSource ? structuredClone(inputSource) : undefined
    // Save complete inputs while an older Runtime action is still running.
    // Only execution is serialized; waiting speech must not remain memory-only.
    const admission = admitQwenRealtimeInput(utterance, frozenInput)
    // Connection replacement does not replace this workspace's Runtime owner.
    const work = realtimeTurnTailRef.current.then(() => {
      if (!mountedRef.current || speechSuppressedRef.current) return { responseText: 'input_cancelled', speak: false }
      return runQwenRealtimeTurn(utterance, frozenInput, admission)
    }).then(result => {
      if (frozenInput) {
        const attempt = realtimeAdmissionsRef.current.get(frozenInput.localInputId)
        if (attempt && attempt.admission === admission && attempt.handoff.phase === 'dispatching') {
          const actionId = 'realtime-' + attempt.handoff.voiceInputId
          const unknown = [...pendingAgentRequests(frozenInput.sessionId), ...pendingAgentExecutions(frozenInput.sessionId)]
            .some(item => item.actionId === actionId)
          if (!unknown) acknowledgeRealtimeAdmission(attempt.handoff)
        }
      }
      return result
    }).finally(async () => {
      await admission
      if (frozenInput && realtimeAdmissionsRef.current.get(frozenInput.localInputId)?.admission === admission) {
        realtimeAdmissionOwnersRef.current.delete(frozenInput.localInputId)
        realtimeAdmissionsRef.current.delete(frozenInput.localInputId)
      }
    })
    realtimeTurnTailRef.current = work.then(() => undefined, () => undefined)
    return work
  }

  const continuationIdle = (): boolean => !nativeDialogueActiveRef.current && mountedRef.current && conversationActiveRef.current
    && !speechSuppressedRef.current && !busyRef.current && !documentCaptureActiveRef.current
    && !restoredAgentRequestMessage(workspace.session.session_id)
    && !restoredRealtimeAdmissionMessage(workspace.session.session_id, workspace.session.child_id)
    && !evaluationRecovery.invalid && !pendingEvaluationRef.current && !pendingTutorCompletionRef.current
    && pendingUtterancesRef.current.length === 0 && unprocessedVoiceInputs.current.length === 0
    && voiceInputQueue.pendingCount === 0 && !voiceInputQueue.running
    && realtimeVoiceRef.current.pendingUtterances === 0
    && realtimeVoiceRef.current.speechActive === false
    && !['transcribing', 'backpressure', 'connecting', 'wake_standby', 'error'].includes(realtimeVoiceRef.current.phase)
    && getCompanionVoiceState() === 'idle'

  function scheduleContinuation(parentTurnId: string): (() => void) | undefined {
    if (continuationHandledRef.current.has(parentTurnId)) return
    continuationHandledRef.current.add(parentTurnId)
    return continuationDriver.offer({
      idle: continuationIdle,
      request: () => commandApi.continueAgentTurn(workspace.session.session_id, parentTurnId),
      execute: (next, current) => preparedTurnRef.current(next, current),
      failed: () => {
        const message = '刚才这一步已经处理，后续步骤暂未确认。我保留了进度，不会假装全部完成。'
        setReply(message)
        speakReply(message, undefined, { localOnly: true })
      },
    })
  }

  const continuationPollRef = useRef(async () => undefined)
  continuationPollRef.current = async () => {
    continuationDriver.flush()
    if (!continuationIdle()) return
    const status = await continuationDriver.readCurrent(
      () => commandApi.continuationStatus(workspace.session.session_id)).catch(() => null)
    if (!status?.parent_turn_id || !continuationIdle()) return
    const documentGoal = status.document_goal
    if (documentGoal && ['awaiting_time', 'time_proposal'].includes(documentGoal.phase)
      && documentGoal.root_turn_id === status.parent_turn_id
      && documentGoal.session_id === workspace.session.session_id && documentGoal.child_id === workspace.session.child_id
      && Number.isInteger(documentGoal.revision) && (documentGoal.revision ?? 0) > 0
      && typeof documentGoal.message === 'string' && documentGoal.message.trim()
      && !globalCaptureReview && !globalTutorPlayingRef.current) {
      const key = `document-review:${status.parent_turn_id}:${documentGoal.revision}`
      if (!continuationHandledRef.current.has(key)) {
        continuationHandledRef.current.add(key)
        setReply(documentGoal.message)
        speakReply(documentGoal.message, undefined, { localOnly: true,ordinaryResponseAllowed:true,sourceAgentTurnId:status.parent_turn_id })
      }
      return
    }
    if (documentGoal?.phase === 'awaiting_confirmation' && documentGoal.root_turn_id === status.parent_turn_id
      && documentGoal.session_id === workspace.session.session_id && documentGoal.child_id === workspace.session.child_id
      && Number.isInteger(documentGoal.revision) && (documentGoal.revision ?? 0) > 0
      && !globalCaptureReview && !restoringDocumentGoalRef.current && !globalTutorPlayingRef.current
      && !continuationHandledRef.current.has(status.parent_turn_id)) {
      restoringDocumentGoalRef.current = true
      const generation = commandGenerationRef.current
      try {
        const review = await continuationDriver.readCurrent(() => readOcrDocumentGoal(workspace.session.session_id,
          workspace.session.child_id, { rootTurnId: status.parent_turn_id!, revision: documentGoal.revision! },
          voiceEventController.current.signal))
        if (!review || !continuationIdle() || globalTutorPlayingRef.current || generation !== commandGenerationRef.current
          || review.artifactId !== documentGoal.artifact_id) return
        continuationHandledRef.current.add(status.parent_turn_id)
        setGlobalCaptureReview(review)
        setRestoredDocumentGoal(status.parent_turn_id)
        const message = `上次拍下的原题还在，没有重新拍摄。题干是：${review.text}。请核对，有识别错误可以直接告诉我。`
        setReply(message)
        speakReply(message, undefined, { localOnly: true })
      } catch {
        if (continuationIdle() && generation === commandGenerationRef.current) {
          continuationHandledRef.current.add(status.parent_turn_id)
          const message = '上次的拍题记录还在，但暂时未能恢复核对。没有重新拍摄，你可以稍后让我核对原题。'
          setReply(message); speakReply(message, undefined, { localOnly: true })
        }
      } finally { restoringDocumentGoalRef.current = false }
      return
    }
    if (status.status === 'awaiting_receipt' && !continuationHandledRef.current.has(status.parent_turn_id)) {
      continuationHandledRef.current.add(status.parent_turn_id)
      const message = '刚才的后续操作还没有确认结果，不能据此判断是否已经执行。我不会重复操作，请先核对当前设备或学习任务的状态。'
      setReply(message)
      speakReply(message, undefined, { localOnly: true })
      return
    }
    if (!['pending', 'ready'].includes(status.status)) return
    scheduleContinuation(status.parent_turn_id)?.()
  }
  useEffect(() => {
    const timer = window.setInterval(() => { void continuationPollRef.current() }, 2_000)
    return () => window.clearInterval(timer)
  }, [workspace.session.session_id])

  useEffect(() => {
    if ((!initialVoiceCommand && !initialWakeOnly) || initialVoiceCommandHandled.current || !wakeSettingsLoaded) return
    if (onboardingSupported && (!onboardingRef.current || ['loading', 'saving'].includes(onboardingRef.current.snapshot.phase))) return
    initialVoiceCommandHandled.current = true
    onInitialVoiceCommandConsumed?.()
    if(initialNativeEntry.current){
      conversationActiveRef.current=true;setVoiceActivationSignal(value=>value+1)
      return
    }else if (initialVoiceCommand) {
      setReply('首次语音的原始识别凭据尚未核对，没有执行操作。请直接说出你的新要求。')
      conversationActiveRef.current=true;setVoiceActivationSignal(value=>value+1)
      return
    }
    if (onboardingSupported && onboardingRef.current?.snapshot.phase !== 'idle') return
    receiveWake()
  }, [agentRuntime, childName, initialVoiceCommand, initialWakeOnly, onInitialVoiceCommandConsumed, wakeSettingsLoaded, onboardingSupported, onboarding])

  const originalAudioRequired = useSyncExternalStore(subscribeVoiceCaptureContext,
    () => originalAudioCaptureRequired(workspace.session.session_id, workspace.session.child_id))
  const desiredVoiceCaptureMode = selectVoiceCaptureMode({
    runtimePolicy: voiceRuntimePolicy,
    runtimeEpoch: voiceRuntimePolicy?.runtimeEpoch ?? null,
    realtimeSettingsLoaded: qwenRealtimeSettingsLoaded,
    realtimeConfigured: Boolean(qwenRealtimeSettings?.configured),
    realtimeEnabled: Boolean(qwenRealtimeSettings?.enabled),
    realtimeFailed: qwenRealtimeFailed,
    onboardingSupported,
    onboardingPhase: onboarding?.phase ?? null,
    privacyStopped,
    microphoneReady: microphoneBindingReady,
    wakeSettingsLoaded,
    editingWakeWord,
    originalAudioRequired:learningSurfaceVisible&&originalAudioRequired,
    nativeDialogueRequired: true,
  })
  const [voiceCaptureMode, setVoiceCaptureMode] = useState<VoiceCaptureMode>('blocked')
  useLayoutEffect(() => {
    setVoiceCaptureMode(current => selectVoiceCaptureHandoff(current, desiredVoiceCaptureMode,
      realtimeVoiceStatus, busy || queuedUtteranceCount > 0,
      privacyStopped || editingWakeWord || desiredVoiceCaptureMode === 'blocked'))
  }, [desiredVoiceCaptureMode, realtimeVoiceStatus, busy, queuedUtteranceCount, privacyStopped, editingWakeWord])
  const qwenRealtimeActive = voiceCaptureMode === 'qwen_realtime'
  nativeDialogueActiveRef.current = qwenRealtimeActive
  const nativeRuntime = createNativeRuntimeBridge({
    sessionId: workspace.session.session_id, childId: workspace.session.child_id,
    current: () => mountedRef.current && !speechSuppressedRef.current && !privacyStopPending(workspace.session.session_id),
    forward: request => {
      if (!window.desktop?.forwardQwenNativeRuntime) throw Error('QWEN_NATIVE_RUNTIME_UNAVAILABLE')
      return window.desktop.forwardQwenNativeRuntime(request)
    },
    onReceipt: async () => { await run(async () => undefined) },
    executePrepared:(dispatch,port)=>{
      const generation=commandGenerationRef.current
      const current=()=>mountedRef.current&&!speechSuppressedRef.current&&generation===commandGenerationRef.current&&port.current()
      return nativePreparedQueue.current.run(current,()=>!busyRef.current,async()=>{
        if(!learningSurfaceVisibleRef.current&&nativeCapabilityNeedsLearningSurface(dispatch.turn.runtime_capability??'',nativeAnswerCapabilities.has(dispatch.turn.runtime_capability??''))){
          if(!onOpenMistakeBook)throw Error('NATIVE_LEARNING_SURFACE_UNAVAILABLE')
          await port.claim()
          await onOpenMistakeBook('learning')
          if(!current())throw Error('NATIVE_LEARNING_SURFACE_OWNER_CHANGED')
        }
        let outcome:NativeClientOutcome|undefined
        await processVoiceInput(dispatch.turn.transcript,voiceEventController.current.signal,undefined,dispatch.turn.action.voice_input_id, null,false,null,
          {turn:dispatch.turn,current,native:{dispatch,port,record:value=>{outcome=value}}})
        if(outcome)await port.complete(outcome)
      })
    },
    trace: (operation, status, durationMs,meta) => {
      void window.desktop?.recordRendererDiagnostic?.({event: 'voice.realtime.native_operation',
        details: {native_operation: operation, status, durationMs,...(meta?.phase?{native_phase:meta.phase}:{}),...(meta?.capability?{native_capability:meta.capability}:{})}})
    },
  })
  const segmentedVoiceActive = voiceCaptureMode === 'segmented'
  const originalCaptureReadiness = useRef<ReturnType<typeof registerOriginalAudioCaptureReadiness> | null>(null)
  useLayoutEffect(() => {
    const owner = registerOriginalAudioCaptureReadiness(workspace.session.session_id, workspace.session.child_id, () => {
      if (!mountedRef.current || speechSuppressedRef.current) return
      conversationActiveRef.current = true
      setVoiceActivationSignal(value => value + 1)
    })
    originalCaptureReadiness.current = owner
    return () => { owner.dispose(); originalCaptureReadiness.current = null }
  }, [workspace.session.session_id, workspace.session.child_id])
  useLayoutEffect(() => {
    originalCaptureReadiness.current?.update(realtimeVoiceStatus,
      !segmentedVoiceActive || busy || queuedUtteranceCount > 0 || privacyStopped || editingWakeWord)
  }, [realtimeVoiceStatus, segmentedVoiceActive, busy, queuedUtteranceCount, privacyStopped, editingWakeWord])

  const [collapsed, setCollapsed] = useState(false)
  const voiceInputMode = useVoiceInputMode(workspace.session.child_id)
  const [voicePreferenceError, setVoicePreferenceError] = useState('')
  const companionPanelId = useId()
  const collapseButton = useRef<HTMLButtonElement>(null)
  const expandButton = useRef<HTMLButtonElement>(null)
  const moveCompanionFocus = useRef(false)
  useLayoutEffect(() => {
    if (!moveCompanionFocus.current) return
    moveCompanionFocus.current = false
    if (collapsed) expandButton.current?.focus({ preventScroll: true })
    else collapseButton.current?.focus()
  }, [collapsed])
  function setCompanionCollapsed(value: boolean, moveFocus: boolean): void {
    moveCompanionFocus.current = moveFocus
    setCollapsed(value)
  }

  const showLesson = learningSurfaceVisible && (
    (!privacyStopped && examListeningView) || globalTutorBoard || globalCaptureVisible || (restoredDocumentGoal && globalCaptureReview)
  )

  return <>
    {collapsed && <button
      type="button" className="agent-edge-toggle" ref={expandButton}
      aria-label="展开学习伙伴" title="展开学习伙伴" aria-controls={companionPanelId} aria-expanded={false}
      onClick={event => { if (event.detail > 0) event.currentTarget.blur(); setCompanionCollapsed(false, event.detail === 0) }}
    ><MessageCircle size={20} aria-hidden="true" /></button>}
    <aside id={companionPanelId} className={`agent-command-panel${collapsed ? ' is-collapsed' : ''}`} aria-label="语音学习伙伴" hidden={collapsed && !showLesson}>
      <button
        type="button" className="agent-panel-collapse" ref={collapseButton}
        aria-label="收起学习伙伴" title="收起到窗口右侧" aria-controls={companionPanelId} aria-expanded={true}
        onClick={event => { if (event.detail > 0) event.currentTarget.blur(); setCompanionCollapsed(true, event.detail === 0) }}
      ><X size={18} aria-hidden="true" /></button>
      <CompanionAvatar compact mode={avatarModeForRealtimeVoice(realtimeVoiceStatus, busy)} />
      <div className="agent-dialogue"><strong>学习伙伴</strong><AgentReplyText store={replyStore} />{voiceDelivery !== 'idle' && <small className="voice-fallback-note">{voiceDelivery === 'natural' ? '声音已播放' : voiceDelivery === 'system_female_offline' ? '本机离线女声' : voiceDelivery === 'system_offline' ? '本机系统声音' : voiceDelivery === 'text_only' ? '声音暂不可用，回复保留在这里。' : voiceDelivery === 'interrupted' ? '播报已停止' : voiceDelivery === 'unconfirmed' ? '完整播报尚未核实，回复保留。' : '正在准备声音'}</small>}</div>
      {(!voiceRuntimePolicy || voiceRuntimePolicy.capture === 'blocked') && <div className="agent-input-status" role="status" aria-live="polite">
        {voiceRuntimePolicy ? '语音服务尚未通过核对，暂不采集声音。' : '正在核对语音服务，暂不采集声音。'}
      </div>}
      {realtimeVoiceStatus.detail && <div className="agent-input-status" role="status" aria-live="polite">
        {realtimeVoiceStatus.detail}
      </div>}
      <div className="agent-learning-content" hidden={!learningSurfaceVisible}>
      {!privacyStopped && examListeningView && <section className="exam-listening-question" aria-label="当前考试题">
        <strong>{examListeningView.status === 'active' ? `第${examListeningView.position}题` : '考试进度'}</strong>
        {examListeningView.item && <>
          {examListeningView.item.stimulus_text && <p className="exam-stimulus">{examListeningView.item.stimulus_text}</p>}
          {examListeningView.item.stimulus_asset === 'pet-study-room' && <img className="pet-stimulus" src={petStudyRoom} alt="两名孩子在书房学习，一名阅读，一名书写，背景中家长正在放下一杯水" />}
          <p>{examListeningView.item.prompt}</p>
        </>}
        <span role="status">{examListeningView.message}</span>
      </section>}
      {globalTutorBoard && <>
        {globalTutorBoard.source === 'visual_teaching_v1' && globalTutorVisualTurnId && <TutorVisual
          onUnavailable={pauseUnavailableTutorVisual}
          binding={{ child_id: workspace.session.child_id, session_id: workspace.session.session_id, turn_id: globalTutorVisualTurnId }} />}
        <TutorBlackboard presentation={globalTutorBoard} revealedSegments={globalTutorSegments} />
        {(() => {
          const source = globalTutorTurn?.turn_id === globalTutorBoardTurnId ? globalTutorTurn
            : workspace.latest_tutor_turn?.turn_id === globalTutorBoardTurnId ? workspace.latest_tutor_turn : null
          const notice = tutorExampleNotice(source, globalTutorBoardTurnId)
          return notice ? <p className="inline-notice" role="status">{notice}</p> : null
        })()}
      </>}
      {restoredDocumentGoal && globalCaptureReview && !globalCaptureVisible && <div className="tutor-ocr-review" aria-live="polite">
        <strong>待核对的原题</strong><span>{globalCaptureReview.text}</span>
      </div>}
      {globalCaptureVisible && <div className="agent-document-capture" aria-live="polite">
        <DocumentCameraCapture
          key={globalCaptureGeneration}
          automation="capture_and_recognize"
          sessionId={workspace.session.session_id}
          commandId={globalCaptureCommandId}
          onGuidance={(message) => { setReply(message); speakReply(message, undefined, { localOnly: globalCaptureObservationOnly }) }}
          onArtifact={(artifact) => {
            if (globalCaptureObservationOnly) {
              const preview = artifact.extracted_text.trim().slice(0, 500)
              const message = preview ? `刚取得的页面文字是：“${preview}”。这是待核对的识别结果，还不能判断你是否做对。` : '已取得页面，但没有识别到可靠文字，暂时不能判断你在写什么。'
              setReply(message)
              speakReply(message, undefined, { localOnly: true }, true)
              return
            }
            const review = createOcrTutorReview(artifact)
            setGlobalCaptureReview(review)
            if (hasMarkedWrongQuestion(artifact) || globalCaptureForMistake) setGlobalCaptureMistakeArtifact(artifact)
            const message = ocrTutorReviewMessage(review)
            setReply(message)
            currentGlobalOcrRef.current=review
            speakReply(message,undefined,{localOnly:true,ordinaryResponseAllowed:true,isCurrent:()=>currentGlobalOcrRef.current===review})
          }}
        />
        {globalCaptureReview && !globalCaptureMistakeArtifact && <div className={`tutor-ocr-review is-${globalCaptureReview.status}`}><strong>高拍仪已联动</strong><span>{ocrTutorReviewMessage(globalCaptureReview)}</span></div>}
        {globalCaptureMistakeArtifact && <VoiceMistakeDialogue key={globalCaptureMistakeArtifact.artifact_id} sessionId={workspace.session.session_id} childId={workspace.session.child_id} artifactId={globalCaptureMistakeArtifact.artifact_id} />}
      </div>}
      <div className="agent-learning-content" hidden={collapsed}>
      {!evaluationRecovery.invalid && !pendingEvaluation && agentRuntime && <AgentObjectiveStatus runtime={agentRuntime} />}
      <div className="agent-command-entry"><div className="agent-command-input"><input aria-label="对学习伙伴说" value={text} onChange={(event) => setText(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void submit(text) }} placeholder="也可以在这里输入" /><button className="secondary-button" disabled={!text.trim()} onClick={() => void submit(text)}>{busy ? (queuedUtteranceCount > 0 ? `已排队 ${queuedUtteranceCount} 句` : '加入队列') : '发送'}</button></div><DraftSaveStatus state={draft.state} restored={Boolean(workspace.drafts.agent_command)} /></div>
      {captureSaveError && <div role="alert">{captureSaveError}</div>}
      {controlledActivityError && <div role="alert">{controlledActivityError}</div>}
      {tutorSyncStatus && <div role="status">{tutorSyncStatus}</div>}
      {completionRestore.invalid && <div role="alert">讲解结果恢复记录未通过核对，已阻止重复执行，请家长检查本机存储。</div>}
      {pendingTutorCompletion && <div className="agent-tool-recovery" role="alert"><span>{tutorCompletionMessage(pendingTutorCompletion)}</span><button className="secondary-button" disabled={busy} onClick={() => void retryTutorCompletion()}>核对原回执</button></div>}
      {(evaluationStatus || pendingEvaluation || evaluationRecovery.invalid) && <div className="agent-tool-recovery" role="status"><span>{evaluationStatus ?? (evaluationRecovery.invalid ? '上次作答评价的恢复记录无法读取。为避免重复评价，已停止提交，请家长核对本机记录。' : '正在恢复原作答评价状态，原回答与证据已保留，不会重新提交作答。')}</span>{pendingEvaluation && <button className="secondary-button" onClick={() => setEvaluationCheckSignal((value) => value + 1)}>核对评价状态</button>}</div>}
      {privacySaveStatus && <div role="status">{privacySaveStatus}<button className="secondary-button" onClick={() => void persistPrivacyStop()}>重试保存隐私选择</button></div>}
      {!pendingTutorCompletion && tutorRecovery && <div className="agent-tool-recovery" role="alert" aria-live="assertive">
        <span>{tutorRecovery.sourceBlocked ? tutorSourceBlockMessage(tutorRecovery.sourceBlocked) : tutorRecovery.requiresExplicitRetryConfirmation ? '上次讲解结果没有确认保存。再次请求可能会再次调用模型；请先确认。' : tutorFailureMessage(tutorRecovery.attempts)}</span>
        <div>{tutorRecovery.sourceBlocked ? null : tutorRecovery.requiresExplicitRetryConfirmation ? <><button className="secondary-button" disabled={busy} onClick={() => void finishTutorRecovery(tutorRecovery, true)}>确认再次请求</button><button className="secondary-button" onClick={() => void abandonUnknownTutorRecovery(tutorRecovery)}>保留问题，改为新请求</button></> : <button className="secondary-button" disabled={busy} onClick={() => void finishTutorRecovery(tutorRecovery)}>重试讲解</button>}
          <button className="secondary-button" onClick={() => { const message = parentHelpMessage(); setReply(message); cancelLocalSpeech() }}>请家长帮忙</button>
        </div>
      </div>}
      </div>
      </div>
      <div className="agent-voice-controls">
        {qwenRealtimeActive ? <QwenRealtimeVoiceInput
          explicitControlsVisible={!collapsed}
          onKeyboardCaptureStart={() => setCollapsed(false)}
          explicitReplyBusy={() => !!tutorPresentationOwnerRef.current || curriculumTutorPendingRef.current?.() === true}
          onExplicitPlaybackInterrupt={() => {
            if (!globalTutorPlayingRef.current) return false
            stopGlobalTutorTimeline()
            tutorPlayback.interrupt()
            return true
          }}
          onLocalSafety={control => {
            if (!mountedRef.current || speechSuppressedRef.current || privacyStopPending(workspace.session.session_id)) return
            continuationDriver.interrupt()
            conversationActiveRef.current = false
            setReply(control.response_text)
            setWakeSettingsMessage('现在先在本机陪伴。可以先找身边可信任的大人；需要继续时，再叫我的名字。')
            speakReply(control.response_text, undefined, {localOnly: true, skipPublicReplyProvider: true, priority: 'urgent',
              isCurrent: () => mountedRef.current && !speechSuppressedRef.current && !privacyStopPending(workspace.session.session_id)})
          }}
          onPrivacyStop={() => preemptPrivacyStop()}
          nativeRuntime={nativeRuntime}
          visibleLearningSource={learningSurfaceVisible}
          householdEntry={initialNativeEntry.current}
          onHouseholdEntryConsumed={()=>{initialNativeEntry.current=undefined}}
          audioAllowed={voiceRuntimePolicy?.capture === 'configured' && !privacyStopped}
          active sessionId={workspace.session.session_id} childId={workspace.session.child_id}
          childName={childName} audioDeviceId={boundAudioDeviceId}
          wakeWord={wakeSettings?.enabled ? wakeSettings.wakeWord : undefined}
          inactivitySeconds={wakeSettings?.inactivitySeconds ?? 60}
          interactionBusy={busy}
          localTutorPlayback={localTutorPlayback}
          externalActivationSignal={voiceActivationSignal}
          initialConversationActive={conversationActiveRef.current}
          onConversationActiveChange={(active) => { conversationActiveRef.current = active }}
          onWake={receiveWake}
          onTurn={handleQwenRealtimeTurn} onInputCompleted={handleQwenRealtimeInputCompleted} onStatus={receiveRuntimeStatus}
          onWorkspaceInput={(transcript,capture,voiceInputId)=>{
            if(speechSuppressedRef.current||!mountedRef.current)return false
            if(onMistakeWorkspaceVoice?.(mistakeWorkspaceVoiceCommand(transcript,wakeSettings?.wakeWord),
                {transcript,voiceInputId,capturedAt:capture.capturedAt})===true)return true
            return claimMistakeCoachingVoice(capture.reference.mistake_learning,voiceInputId,transcript)
          }}
          onCurriculumTutor={receiveCurriculumTutor}
          onPendingInputHandoff={preserveTerminalRealtimeInputs}
          onRepeatRequired={(scope) => {
            if (!mountedRef.current || speechSuppressedRef.current || privacyStopped
              || scope.sessionId !== workspace.session.session_id || scope.childId !== workspace.session.child_id) return
            const message = '刚才有一段声音没有识别完成。现在可以再说一次。'
            setReply(message)
            speakReply(message, undefined, { localOnly: true,
              isCurrent: () => mountedRef.current && !speechSuppressedRef.current
                && !privacyStopPending(scope.sessionId) && !privacyStopped }, true)
          }}
          onAssistantTranscript={(value) => setReply(value)}
          onPublicSpeechInterrupt={cancelLocalSpeech}
          onPermanentFailure={(message) => {
            if (message.startsWith(PLAYBACK_TAIL_RECOVERY_GUIDANCE)) {
              // The voice status already displays the specific recovery next to its action.
              setWakeSettingsMessage('')
              return
            }
            setWakeSettingsMessage(message.startsWith('云语音服务拒绝') || message.startsWith('云语音账户余额不足') ? message : realtimeVoiceRef.current.phase === 'error'
              ? (realtimeVoiceRef.current.pendingUtterances > 0
                ? '实时语音连接中断；未处理原话的保存结果正在核对，条件允许时将自动尝试恢复。'
                : '实时语音连接中断；未确认的原话会保留，条件允许时将自动尝试恢复。')
              : `云端自然语音暂时无法连接：${message}`)
          }}
        /> : segmentedVoiceActive ? <>
          <HandsFreeVoiceInput
            sessionId={workspace.session.session_id}
            purpose={tutorialActive || voiceWakeNameSetup || (!onboardingSupported && !wakeSettings) ? 'wake_word_setup' : 'agent_command'}
            childId={workspace.session.child_id} audioDeviceId={boundAudioDeviceId}
            onTranscript={receiveHandsFreeTranscript} onRuntimeStatusChange={receiveRuntimeStatus}
            wakeWord={!editingWakeWord && wakeSettings?.enabled
              && ((!tutorialCaptureHeld && !tutorialActive) || wakePracticeArmed)
              ? wakeSettings.wakeWord : undefined}
            inactivitySeconds={wakeSettings?.inactivitySeconds ?? 60}
            initialConversationActive={conversationActiveRef.current}
            onConversationActiveChange={(active) => { conversationActiveRef.current = active }}
            standbyOnWakeWordEnable={wakePracticeArmed}
            interactionBusy={busy}
            inputMode={voiceInputMode}
            visibleLearningSource={learningSurfaceVisible}
            spaceEnabled={!privacyStopped && microphoneBindingReady && (!editingWakeWord || voiceWakeNameSetup)}
            onInputModeChange={mode => setVoicePreferenceError(saveVoiceInputMode(workspace.session.child_id, mode) ? '' : '已切换，本次语音设置未能保存。')}
            onKeyboardCaptureStart={() => setCollapsed(false)}
            autoStart={!privacyStopped && Boolean(window.desktop?.saveWakeWordSettings) && microphoneBindingReady && (!editingWakeWord || voiceWakeNameSetup) && wakeSettingsLoaded && (!tutorRecovery || onboardingSupported)}
            externalActivationSignal={tutorialActive ? 0 : voiceActivationSignal} onWake={receiveWake}
          />
          {voicePreferenceError && <span role="status">{voicePreferenceError}</span>}
        </> : null}
        <button className="icon-button" title="停止伙伴播报" onClick={() => {
          window.dispatchEvent(new Event('hsc-stop-realtime-speech'))
          cancelLocalSpeech()
          setVoiceDelivery('interrupted')
        }}><VolumeX size={18} /></button>
      </div>
      {wakeSettingsLoaded && (!wakeSettings || editingWakeWord) && <div className="wake-word-setup" aria-label="初始化唤醒词"><strong>{wakeSettings ? '修改唤醒词' : onboarding?.phase === 'idle' ? '暂未设置唤醒词' : '先给学习伙伴起一个唤醒词'}</strong><span>{wakeSettings ? '家长可以在这里维护设置。' : onboarding?.phase === 'idle' ? '可以直接说学习请求。' : '请说一个短名字，确认后才会保存。'}</span>{editingWakeWord && <div className="wake-word-entry"><input aria-label="唤醒词" value={wakeWordDraft} maxLength={12} placeholder="例如：小知" onChange={(event) => setWakeWordDraft(event.target.value)} /><select aria-label="连续对话静默时间" value={wakeInactivitySeconds} onChange={(event) => setWakeInactivitySeconds(Number(event.target.value))}><option value={30}>30秒</option><option value={45}>45秒</option><option value={60}>60秒</option><option value={90}>90秒</option><option value={120}>2分钟</option></select><button className="primary-button" disabled={!wakeWordDraft.trim() || savingWakeWord.current} onClick={() => void saveWakeWord()}>保存并开始监听</button>{wakeSettings && <button className="secondary-button" onClick={() => { setEditingWakeWord(false); setWakeWordDraft(wakeSettings.wakeWord) }}>取消</button>}</div>}</div>}
      {wakeSettingsLoaded && wakeSettings && !editingWakeWord && <div className="wake-word-status">{!qwenRealtimeActive && <span>{wakeListenerMessage(realtimeVoiceStatus, wakeSettings, privacyStopped)}</span>}<button className="secondary-button" onClick={() => setEditingWakeWord(true)}>修改唤醒词</button></div>}
      {wakeSettingsMessage && <small className="wake-word-message">{wakeSettingsMessage}</small>}
      <div className="agent-calibration"><VoiceCalibrationPanel childId={workspace.session.child_id} sessionId={workspace.session.session_id} sessionScoped onBoundAudioDeviceIdChange={receiveBoundAudioDeviceId} onMicrophoneBindingReadyChange={setMicrophoneBindingReady} /></div>
    </aside>
  </>
}

export function LocalObservation({ sessionId, active, enabled, snoozeUntil, onAuthorityRefresh }: { sessionId: string; active: boolean; enabled: boolean; snoozeUntil: string | null; onAuthorityRefresh?: (isCurrent: () => boolean) => Promise<void> }): React.JSX.Element {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const observationSampleRef = useRef<ObservationSampleClock | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const leaseRefs = useRef<string[]>([])
  const releasePendingRef = useRef<Promise<boolean>>(Promise.resolve(true))
  const registrationRef = useRef<RetainedObservationRegistration | null>(null)
  const coverageRef = useRef<RetainedObservationCoverage | null>(null)
  const coverageBindingRef = useRef<string | null>(null)
  const startupApiRef = useRef<AbortController | null>(null)
  const frameWaitTimerRef = useRef<number | null>(null)
  const framePollTimerRef = useRef<number | null>(null)
  const firstVideoFrameRef = useRef<number | null>(null)
  const frameHealthRef = useRef(new FrameHealthMonitor())
  const contentFreshnessRef = useRef(new L523ContentFreshnessMonitor())
  const lastFrameGuidanceRef = useRef<string | null>(null)
  const deviceFrameClock = useRef(new CameraFrameClock())
  const attachGenerationRef = useRef(0)
  const attachmentOwnerRef = useRef({ sessionId, active })
  if (attachmentOwnerRef.current.sessionId !== sessionId || attachmentOwnerRef.current.active !== active) attachmentOwnerRef.current = { sessionId, active }
  const openingRef = useRef<{ generation: number; owner: object; promise: Promise<void> } | null>(null)
  const deviceCommandSequenceRef = useRef(0)
  const authorityRefreshRef = useRef(onAuthorityRefresh)
  authorityRefreshRef.current = onAuthorityRefresh
  const attachRef = useRef<() => Promise<void>>(async () => undefined)
  const analysisFlightRef = useRef<ObservationAnalysisFlight<unknown> | null>(null)
  const analysisFailuresRef = useRef(0)
  const analysisRetryAtRef = useRef(0)
  const [status, setStatus] = useState<'starting' | 'active' | 'paused' | 'unavailable' | 'inactive'>(active ? 'starting' : 'inactive')
  const [message, setMessage] = useState(active ? '正在启动本地摄像头' : '学习会话已经结束，摄像头未由应用读取')
  const [observedState, setObservedState] = useState<string | null>(null)
  const [observationReady, setObservationReady] = useState<boolean | null>(null)
  const [qualificationGate, setQualificationGate] = useState<'idle' | 'starting' | 'ready' | 'failed'>('idle')
  const [qualificationDiagnostic, setQualificationDiagnostic] = useState({ scene: 'not_evaluated', person: 'not_evaluated' })
  const [manualRecheck, setManualRecheck] = useState(0)
  const [rechecking, setRechecking] = useState(false)
  const [previewExpanded, setPreviewExpanded] = useState(false)
  const [deviceLabel, setDeviceLabel] = useState('尚未识别实际设备')
  const [frameDimensions, setFrameDimensions] = useState<string | null>(null)
  const [resettingPreference, setResettingPreference] = useState(false)
  const pendingOwner = useRef({ sessionId, active })
  if (pendingOwner.current.sessionId !== sessionId || pendingOwner.current.active !== active) pendingOwner.current = { sessionId, active }
  const [pendingState, setPendingState] = useState<{ owner: object; value: PendingBehaviorIntervention | null } | null>(null)
  const pendingIntervention = pendingState?.owner === pendingOwner.current ? pendingState.value : null
  const setPendingIntervention = useCallback((value: PendingBehaviorIntervention | null): void => {
    if (pendingOwner.current.sessionId === sessionId && pendingOwner.current.active === active) {
      setPendingState({ owner: pendingOwner.current, value })
    }
  }, [sessionId, active])
  const [feedbackBusy, setFeedbackBusy] = useState(false)
  const [pendingRecoveryError, setPendingRecoveryError] = useState<string | null>(null)
  const [pendingChecking, setPendingChecking] = useState(false)
  const pendingRead = useRef(0)
  const pendingReadBusy = useRef(false)
  const feedbackFlight = useRef<object | null>(null)
  const [feedbackRecovery, setFeedbackRecovery] = useState<{ owner: object; intervention: PendingBehaviorIntervention; accepted: boolean; note?: string; clientActionId: string } | null>(null)
  const unresolvedFeedback = feedbackRecovery?.owner === pendingOwner.current ? feedbackRecovery : null
  const [reconnectAttempt, setReconnectAttempt] = useState(0)
  const [autoReconnect, setAutoReconnect] = useState(false)
  const feedbackActionIdsRef = useRef<Record<string, string>>({})
  const [clock, setClock] = useState(Date.now())
  const snoozed = Boolean(snoozeUntil && new Date(snoozeUntil).getTime() > clock)

  useEffect(() => {
    if (!snoozeUntil) return
    setClock(Date.now())
    const timer = window.setInterval(() => setClock(Date.now()), 15_000)
    return () => window.clearInterval(timer)
  }, [snoozeUntil])

  const recoverPendingIntervention = useCallback(async (): Promise<PendingBehaviorIntervention | null | undefined> => {
    const owner = pendingOwner.current
    const sequence = ++pendingRead.current
    const current = (): boolean => pendingOwner.current === owner && pendingRead.current === sequence
    if (!active) { setPendingIntervention(null); return null }
    pendingReadBusy.current = true
    setPendingChecking(true)
    try {
      const result = await localApi.pendingSessionBehaviorIntervention(sessionId)
      if (!current()) return undefined
      setPendingRecoveryError(null)
      const invitation = result.status === 'pending' && result.intervention ? result.intervention : null
      setPendingIntervention(invitation)
      if (result.status === 'expired') setMessage('上次提醒已过期，不需要再回答。')
      return invitation
    } catch (cause) {
      if (!current()) return undefined
      setPendingRecoveryError(cause instanceof ApiError ? `提醒恢复暂不可用：${cause.message}` : '提醒恢复暂不可用；请重试。')
      return undefined
    } finally { if (current()) { pendingReadBusy.current = false; setPendingChecking(false) } }
  }, [active, sessionId, setPendingIntervention])

  useEffect(() => {
    setFeedbackBusy(false); feedbackFlight.current = null; setPendingRecoveryError(null)
    void recoverPendingIntervention()
    return () => { pendingRead.current += 1; pendingReadBusy.current = false; feedbackFlight.current = null }
  }, [recoverPendingIntervention])

  useEffect(() => {
    if (!active || !pendingIntervention || feedbackBusy) return
    const refresh = (): void => { if (document.visibilityState !== 'hidden' && !pendingReadBusy.current) void recoverPendingIntervention() }
    const timer = window.setInterval(refresh, 5000)
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [active, pendingIntervention?.intervention_id, feedbackBusy, recoverPendingIntervention])

  useEffect(() => {
    const feedbackSaved = (event: Event): void => {
      const detail = (event as CustomEvent<{ sessionId?: string; feedback?: string; responseKind?: string }>).detail
      if (detail?.sessionId !== sessionId) return
      setPendingIntervention(null)
      setMessage(detail.feedback === 'accepted'
        ? '已记下：你愿意试一试。'
        : detail.responseKind === 'less_like_this'
          ? '已记下：这类提醒会更少、更先询问。'
          : '已记下：你想稍后再说。')
      void recoverPendingIntervention()
    }
    window.addEventListener('hsc-behavior-intervention-feedback', feedbackSaved)
    return () => window.removeEventListener('hsc-behavior-intervention-feedback', feedbackSaved)
  }, [recoverPendingIntervention, sessionId])

  const release = async (reason = 'renderer_closed'): Promise<boolean> => {
    attachGenerationRef.current += 1
    const analysisFlight = analysisFlightRef.current
    analysisFlight?.retire()
    if (analysisFlightRef.current === analysisFlight) analysisFlightRef.current = null
    setRechecking(false)
    startupApiRef.current?.abort()
    const registration = registrationRef.current
    const registrationClosed = registration?.retire(reason)
    if (frameWaitTimerRef.current !== null) {
      window.clearTimeout(frameWaitTimerRef.current)
      frameWaitTimerRef.current = null
    }
    if (framePollTimerRef.current !== null) {
      window.clearInterval(framePollTimerRef.current)
      framePollTimerRef.current = null
    }
    const video = videoRef.current
    if (firstVideoFrameRef.current !== null && video?.cancelVideoFrameCallback) {
      video.cancelVideoFrameCallback(firstVideoFrameRef.current)
      firstVideoFrameRef.current = null
    }
    const stream = streamRef.current
    const sampler = observationSampleRef.current
    observationSampleRef.current = null
    const boundary = reason.includes('pause') ? 'paused' : reason.includes('disconnect') ? 'disconnected'
      : reason.includes('closed') ? 'ended' : 'unavailable'
    const coverage = coverageRef.current
    const coverageClosed = coverage?.retire(sampler?.sequence ?? 0, boundary)
    streamRef.current = null
    coverageBindingRef.current = null
    frameHealthRef.current.reset()
    if (['component_unmounted', 'renderer_closed', 'session_inactive', 'persisted_privacy_pause', 'voice_privacy_pause', 'voice_camera_pause'].includes(reason)) contentFreshnessRef.current.reset()
    else if (reason !== 'camera_content_unconfirmed') contentFreshnessRef.current.clearContent()
    stream?.getTracks().forEach((track) => track.stop())
    const leases = leaseRefs.current.filter(leaseId => leaseId !== registration?.leaseId)
    leaseRefs.current = []
    if (videoRef.current) videoRef.current.srcObject = null
    if (leases.length) {
      const previous = releasePendingRef.current
      releasePendingRef.current = Promise.all(leases.map(async (leaseId) => {
        try { const saved = await releaseObservationLease(signal => localApi.releaseDeviceLease(leaseId, reason, signal)); if (saved.lease_id !== leaseId || saved.status !== 'released') throw new Error('LEASE_NOT_RELEASED'); return true }
        catch { leaseRefs.current.push(leaseId); return false }
      })).then(async (results) => { await previous; return results.every(Boolean) })
    }
    const [released, registrationReleased, coverageReleased] = await Promise.all([releasePendingRef.current, registrationClosed ?? true, coverageClosed ?? true])
    if (registrationReleased && registrationRef.current === registration) registrationRef.current = null
    if (coverageReleased && coverageRef.current === coverage) coverageRef.current = null
    return released && registrationReleased && coverageReleased
  }

  const retireForReconnect = async (reason: string, message: string, retry = true): Promise<void> => {
    // Repeating content grants one source check, not indefinite retries.
    const retryAllowed = retry && (reason === 'camera_content_unconfirmed' || !contentFreshnessRef.current.unconfirmed)
    setObservationReady(false)
    setObservedState(null)
    setStatus('unavailable')
    setAutoReconnect(false)
    setMessage(message)
    const pending = release(reason)
    const retirement = attachGenerationRef.current
    const released = await pending
    // A pause, session change or newer attachment owns subsequent decisions.
    if (attachGenerationRef.current !== retirement || streamRef.current !== null) return
    setAutoReconnect(released && retryAllowed)
    if (released && retryAllowed) setReconnectAttempt((value) => value + 1)
    if (!released) setMessage(`${message} 旧连接的关闭状态还未确认，摄像头会保持关闭；可以稍后重试连接。`)
  }

  const startupRequest = async <T,>(work: (signal: AbortSignal) => Promise<T>): Promise<T> => {
    const controller = new AbortController()
    startupApiRef.current = controller
    const timer = window.setTimeout(() => controller.abort(new Error('OBSERVATION_STARTUP_TIMEOUT')), 12_000)
    const cancelled = new Promise<never>((_resolve, reject) => {
      controller.signal.addEventListener('abort', () => reject(controller.signal.reason), { once: true })
    })
    try { return await Promise.race([Promise.resolve().then(() => work(controller.signal)), cancelled]) }
    finally {
      window.clearTimeout(timer)
      if (startupApiRef.current === controller) startupApiRef.current = null
    }
  }

  const startQualification = async (isCurrent: () => boolean = () => true): Promise<boolean> => {
    setQualificationGate('starting')
    setObservationReady(false)
    setObservedState(null)
    try {
      await startupRequest(signal => localApi.startObservationQualification(sessionId, signal))
      if (!isCurrent()) return false
      setQualificationGate('ready')
      return true
    } catch (cause) {
      if (!isCurrent()) return false
      if (cause instanceof Error && cause.message === 'OBSERVATION_STARTUP_TIMEOUT') throw cause
      setQualificationGate('failed')
      setMessage(cause instanceof ApiError
        ? `取景确认状态暂时没有保存：${cause.message}；预览仍在本机，确认成功前不会分析或记录行为。`
        : '取景确认状态暂时没有保存；预览仍在本机，确认成功前不会分析或记录行为。')
      return false
    }
  }

  const openAttachment = async (generation: number, owner: object): Promise<void> => {
    const isCurrent = (): boolean => attachGenerationRef.current === generation && attachmentOwnerRef.current === owner
    if (!active) {
      setStatus('unavailable')
      setMessage('学习会话未激活，应用未读取摄像头')
      return
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setAutoReconnect(false)
      setStatus('unavailable')
      setMessage('当前运行环境没有可用的媒体设备接口')
      void localApi.recordDeviceIncident(sessionId, 'camera', 'MEDIA_API_UNAVAILABLE', '当前运行环境没有可用的媒体设备接口。').catch(() => undefined)
      return
    }
    try {
      const binding = (await localApi.deviceBindings()).find((item) => item.device_type === 'camera')
      if (!isCurrent()) return
      if (!binding) {
        setAutoReconnect(false)
        setStatus('unavailable')
        setMessage('家长尚未绑定观察摄像头；应用没有打开任何视频设备')
        void localApi.recordDeviceIncident(sessionId, 'camera', 'DEVICE_NOT_BOUND', '家长尚未绑定观察摄像头。').catch(() => undefined)
        return
      }
      const selected = await resolveBoundVideoDevice(await navigator.mediaDevices.enumerateDevices(), binding.system_id_hash, binding.label, 'camera')
      if (!isCurrent()) return
      if (!selected) {
        setAutoReconnect(!contentFreshnessRef.current.unconfirmed)
        setReconnectAttempt((value) => value + 1)
        setStatus('unavailable')
        setMessage(`未找到已绑定的观察摄像头“${binding.label}”；应用没有改用其他摄像头`)
        void localApi.recordDeviceIncident(sessionId, 'camera', 'BOUND_DEVICE_NOT_FOUND', `未找到已绑定的观察摄像头“${binding.label}”。`).catch(() => undefined)
        return
      }
      const retainedRegistration = reconcileObservationRegistration(binding.system_id_hash) ?? reconcileObservationCoverage(sessionId, binding.system_id_hash)
      if (retainedRegistration) {
        setStatus('unavailable')
        setAutoReconnect(false)
        setMessage('摄像头正在安全关闭，已停止画面读取；确认旧连接关闭后会自动重连。')
        const released = await retainedRegistration
        if (!isCurrent()) return
        if (released) { setAutoReconnect(true); setReconnectAttempt(value => value + 1) }
        return
      }
      if (leaseRefs.current.length) {
        setStatus('unavailable')
        setAutoReconnect(false)
        setMessage('摄像头正在安全关闭，已停止画面读取；确认旧连接关闭后再重新连接。')
        const pending = release('observation_resume_cleanup')
        const retirement = attachGenerationRef.current
        const released = await pending
        if (attachGenerationRef.current !== retirement || streamRef.current !== null) return
        if (released) { setAutoReconnect(true); setReconnectAttempt(value => value + 1) }
        return
      }
      setDeviceLabel(selected.device.label || binding.label)
      if (!isL523ObservationSource(selected.device.label || binding.label)) contentFreshnessRef.current.clearContent()
      setFrameDimensions(null)
      const stream = await openObservationCameraStream(selected.device.deviceId, selected.device.label || binding.label, undefined, binding.system_id_hash)
      if (!isCurrent()) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      streamRef.current = stream
      frameHealthRef.current.reset()
      const track = stream.getVideoTracks()[0]
      const videoRegistration = await mediaDeviceRegistration(track)
      if (!isCurrent()) {
        stream.getTracks().forEach((item) => item.stop())
        return
      }
      const registration = retainObservationRegistration(binding.system_id_hash,
        () => localApi.registerDevice(sessionId, 'camera', selected.method === 'system_id_hash' ? videoRegistration.systemIdHash : binding.system_id_hash, videoRegistration.capabilities),
        (leaseId, reason) => releaseObservationLease(signal => localApi.releaseDeviceLease(leaseId, reason, signal)))
      registrationRef.current = registration
      const cameraLease = await startupRequest(() => registration.result)
      if (!isCurrent()) {
        stream.getTracks().forEach((item) => item.stop())
        void registration.retire()
        return
      }
      leaseRefs.current.push(cameraLease.lease_id)
      registration.accept()
      const sampler = new ObservationSampleClock()
      const coverageOwner = retainObservationCoverage(sessionId, sampler.epoch,
        () => localApi.beginObservationStream(sessionId, sampler.epoch),
        (sequence, reason, signal) => localApi.endObservationStream(sessionId, sampler.epoch, sequence, reason, signal), binding.system_id_hash)
      coverageRef.current = coverageOwner
      coverageBindingRef.current = binding.system_id_hash
      await startupRequest(() => coverageOwner.result)
      if (!isCurrent()) {
        void coverageOwner.retire(0, 'context_changed')
        return
      }
      coverageOwner.accept()
      observationSampleRef.current = sampler
      setStatus('starting')
      setObservationReady(false)
      setObservedState(null)
      setQualificationDiagnostic({ scene: 'not_evaluated', person: 'not_evaluated' })
      setMessage('摄像头已打开，正在本机确认取景和常坐位置；通过前不会记录行为状态或发出提醒。')
      track.addEventListener('ended', () => {
        if (streamRef.current !== stream) return
        const message = `已绑定的观察摄像头“${binding.label}”视频流已结束，正在确认释放后重新连接`
        void retireForReconnect('device_disconnected', message)
        void localApi.recordDeviceIncident(sessionId, 'camera', 'DEVICE_DISCONNECTED', message).catch(() => undefined)
      }, { once: true })
      if (videoRef.current) {
        videoRef.current.srcObject = stream
        // A live UVC track can leave play() pending without delivering a frame.
        // The independent first-frame deadline must still be installed.
        void videoRef.current.play().catch((cause) => {
          if (!isCurrent() || streamRef.current !== stream) return
          const failure = mediaDeviceFailure(cause, '观察摄像头')
          void retireForReconnect('video_play_failed', failure.message, failure.code !== 'MEDIA_PERMISSION_DENIED')
          void localApi.recordDeviceIncident(sessionId, 'camera', failure.code, failure.message).catch(() => undefined)
        })
      }
      if (!isCurrent()) return
      // This awaits the durable start marker before status becomes active.
      // The preview may already be visible, but no observation-frame request
      // can race ahead and be overwritten by a delayed qualification start.
      if (!await startQualification(isCurrent)) {
        if (!isCurrent()) return
        void release('qualification_start_failed').then((released) => {
          if (streamRef.current !== null) return
          setStatus('unavailable')
          setMessage(released
            ? '取景确认没有保存，应用已停止读取摄像头；请调整后重新打开。'
            : '取景确认没有保存，浏览器已停止读取，但本地服务未确认租约释放；请重试。')
        })
        return
      }
      let firstFrameConfirmed = false
      let contentFrameCheckPending = false
      const publishFirstFrame = (): void => {
        const video = videoRef.current
        if (firstFrameConfirmed || !isCurrent() || streamRef.current !== stream || !video) return
        const hasUsableFrame = !video.paused && !video.ended
          && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
          && video.videoWidth > 0 && video.videoHeight > 0
        if (!hasUsableFrame) return
        firstFrameConfirmed = true
        if (frameWaitTimerRef.current !== null) {
          window.clearTimeout(frameWaitTimerRef.current)
          frameWaitTimerRef.current = null
        }
        if (framePollTimerRef.current !== null) {
          window.clearInterval(framePollTimerRef.current)
          framePollTimerRef.current = null
        }
        if (firstVideoFrameRef.current !== null && video.cancelVideoFrameCallback) {
          video.cancelVideoFrameCallback(firstVideoFrameRef.current)
          firstVideoFrameRef.current = null
        }
        setStatus('active')
        deviceFrameClock.current.read('camera', video, stream, true, null)
        setFrameDimensions(`${video.videoWidth} × ${video.videoHeight}`)
        setAutoReconnect(false)
        setReconnectAttempt(0)
        setMessage('已确认本地首帧，正在本机分析画面，不保存观察帧')
        void localApi.confirmDeviceFrame(cameraLease.lease_id, video.videoWidth, video.videoHeight).catch(() => undefined)
      }
      const confirmFirstFrame = (): void => {
        if (firstFrameConfirmed || !isCurrent() || streamRef.current !== stream) return
        const video = videoRef.current
        if (!video || video.paused || video.ended || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || video.videoWidth <= 0 || video.videoHeight <= 0) return
        if (!contentFreshnessRef.current.unconfirmed) { publishFirstFrame(); return }
        if (contentFrameCheckPending) return
        contentFrameCheckPending = true
        // Retain the original end-ACK/new-coverage ownership chain, then require
        // strictly newer native frames on this replacement before publishing it.
        void startupRequest(signal => waitForCameraFrame(() => ({ video: videoRef.current, stream: streamRef.current,
          leased: leaseRefs.current.length > 0, generation: attachGenerationRef.current }), signal)).then(() => {
          if (isCurrent() && streamRef.current === stream) publishFirstFrame()
        }).catch(() => {
          if (!isCurrent() || streamRef.current !== stream) return
          void retireForReconnect('content_recheck_frame_unconfirmed', '摄像头新连接尚未确认到新帧，观察保持关闭；语音陪伴继续，可以明确重试摄像头。', false)
        })
      }
      videoRef.current?.addEventListener('loadeddata', confirmFirstFrame, { once: true })
      videoRef.current?.addEventListener('resize', confirmFirstFrame, { once: true })
      videoRef.current?.addEventListener('timeupdate', confirmFirstFrame, { once: true })
      const preview = videoRef.current
      if (preview?.requestVideoFrameCallback) {
        firstVideoFrameRef.current = preview.requestVideoFrameCallback(() => confirmFirstFrame())
      }
      framePollTimerRef.current = window.setInterval(confirmFirstFrame, 100)
      confirmFirstFrame()
      if (!firstFrameConfirmed) {
        frameWaitTimerRef.current = window.setTimeout(() => {
          if (firstFrameConfirmed || !isCurrent() || streamRef.current !== stream) return
          setQualificationDiagnostic({ scene: 'unusable_frame', person: 'not_evaluated' })
          void retireForReconnect('first_frame_timeout', '摄像头已打开但 12 秒内没有可用画面，已停止本次读取，正在确认释放后自动重连。')
          void localApi.recordDeviceIncident(
            sessionId, 'camera', 'NO_USABLE_VIDEO_FRAME',
            '观察摄像头已打开但 12 秒内没有返回可用画面，应用已停止本次读取。'
          ).catch(() => undefined)
        }, 12_000)
      }
    } catch (cause) {
      if (!isCurrent()) return
      if (cause instanceof Error && cause.message === 'OBSERVATION_STARTUP_TIMEOUT') {
        void retireForReconnect('camera_startup_api_timeout', '摄像头连接超过 12 秒仍未完成，已停止读取画面；确认旧连接安全关闭后会自动重连。')
        void localApi.recordDeviceIncident(sessionId, 'camera', 'CAMERA_STARTUP_TIMEOUT', '摄像头注册、观察时段启动或取景确认请求超过 12 秒未完成，已停止读取并等待原连接关闭确认。').catch(() => undefined)
        return
      }
      const failure = mediaDeviceFailure(cause, '观察摄像头')
      void retireForReconnect('open_failed', failure.message, failure.code !== 'MEDIA_PERMISSION_DENIED')
      // Waiting on the same unresolved browser request is not a new failure.
      if (failure.code !== 'DEVICE_START_PENDING') {
        void localApi.recordDeviceIncident(sessionId, 'camera', failure.code, failure.message).catch(() => undefined)
      }
    }
  }
  const attach = (): Promise<void> => {
    const owner = attachmentOwnerRef.current
    const pending = openingRef.current
    if (pending?.owner === owner && pending.generation === attachGenerationRef.current) return pending.promise
    const generation = ++attachGenerationRef.current
    const promise = openAttachment(generation, owner)
    const opening = { generation, owner, promise }
    openingRef.current = opening
    const clear = (): void => { if (openingRef.current === opening) openingRef.current = null }
    void promise.then(clear, clear)
    return promise
  }
  attachRef.current = attach

  useLayoutEffect(() => {
    if (!active || !enabled || !autoReconnect || status !== 'unavailable') return
    const media = navigator.mediaDevices
    if (!media?.enumerateDevices) return
    const retry = (): void => { void attachRef.current() }
    const delay = Math.min(30_000, 1_000 * (2 ** Math.min(reconnectAttempt, 5)))
    const timer = window.setTimeout(retry, delay)
    media.addEventListener?.('devicechange', retry)
    return () => {
      window.clearTimeout(timer)
      media.removeEventListener?.('devicechange', retry)
    }
  }, [active, autoReconnect, enabled, reconnectAttempt, status])

  useEffect(() => {
    if (!active) {
      void release('session_inactive')
      setStatus('inactive')
      setMessage('学习会话已经结束，摄像头未由应用读取')
    } else if (enabled) { if (!streamRef.current) void attach() }
    else {
      void release('persisted_privacy_pause')
      setStatus('paused')
      setMessage('观察当前保持暂停；获得新的明确同意后才会重新开启摄像头')
    }
  }, [active, enabled])

  useEffect(() => () => { void release('component_unmounted') }, [sessionId])

  useEffect(() => {
    const read = (): ReturnType<CameraFrameClock['read']> => {
      const frame = deviceFrameClock.current.read('camera', videoRef.current, streamRef.current, active && status === 'active', status === 'unavailable' ? 'CAMERA_UNAVAILABLE' : null)
      return contentFreshnessRef.current.unconfirmed ? { ...frame, frame_confirmed: false, monitor_active: false } : frame
    }
    const query = (event: Event): void => {
      const detail = (event as CustomEvent<CameraStatusRequest>).detail
      if (detail?.sessionId === sessionId && detail.device === 'camera') detail.resolve(read())
    }
    const timer = window.setInterval(() => {
      const frame = read()
      const frameAge = deviceFrameClock.current.frameAgeMs()
      if (!active || status !== 'active' || !streamRef.current || frame.frame_confirmed
        || frameAge === null || frameAge < 5_000) return
      setQualificationDiagnostic({ scene: 'unusable_frame', person: 'not_evaluated' })
      void retireForReconnect('camera_frame_stalled', '观察摄像头持续 5 秒没有新画面，正在确认释放旧连接后自动重连；收到新画面并完成取景确认前暂停行为分析。')
      void localApi.recordDeviceIncident(sessionId, 'camera', 'CAMERA_FRAME_STALLED',
        '观察摄像头持续 5 秒没有新画面，旧连接已停止读取，等待释放确认后自动重连。').catch(() => undefined)
    }, 1_000)
    window.addEventListener(CAMERA_DEVICE_STATUS, query)
    return () => { window.clearInterval(timer); window.removeEventListener(CAMERA_DEVICE_STATUS, query) }
  }, [active, sessionId, status])

  useEffect(() => {
    let disposed = false
    const control = (event: Event): void => {
      const detail = (event as CustomEvent<CameraDeviceCommand>).detail
      if (detail?.sessionId !== sessionId || detail.device !== 'camera' || !active) return
      event.preventDefault()
      const sequence = ++deviceCommandSequenceRef.current
      let generation = attachGenerationRef.current
      const sameIntent = (): boolean => !disposed && deviceCommandSequenceRef.current === sequence
      const ownsStream = (): boolean => sameIntent() && (detail.command === 'pause' || attachGenerationRef.current === generation)
      const current = (): boolean => ownsStream() && !detail.signal.aborted
      const abort = (): void => { if (!ownsStream()) return; setAutoReconnect(false); void release('voice_command_cancelled'); setStatus('paused') }
      detail.signal.addEventListener('abort', abort, { once: true })
      void (async () => {
        try {
          if (!current()) throw new DOMException('Cancelled', 'AbortError')
          if (detail.command === 'pause') {
            setAutoReconnect(false)
            const released = release('voice_camera_pause')
            generation = attachGenerationRef.current
            setStatus('paused')
            const saved = await localApi.pauseObservation(sessionId)
            if (!await released || saved.observation_status !== 'paused') throw new Error('CAMERA_PAUSE_NOT_CONFIRMED')
          } else {
            const pendingOpening = openingRef.current
            if (pendingOpening?.owner === attachmentOwnerRef.current && pendingOpening.generation === generation) {
              await pendingOpening.promise
              if (!current()) throw new DOMException('Cancelled', 'AbortError')
            }
            const existingStream = streamRef.current
            const previousCoverage = coverageRef.current
            const previousSampler = observationSampleRef.current
            const bindingKey = coverageBindingRef.current
            let previousClosed: Promise<boolean> | null = null
            let pendingSampler: ObservationSampleClock | null = null
            if (existingStream) {
              if (!previousCoverage || !bindingKey) throw new Error('OBSERVATION_COVERAGE_PENDING')
              // A fresh resume changes the server revision. Stop publishing old
              // captures synchronously, before that permission can be replaced.
              observationSampleRef.current = null
              const analysis = analysisFlightRef.current
              if (analysisFlightRef.current === analysis) analysisFlightRef.current = null
              analysis?.retire()
              setObservationReady(false)
              setObservedState(null)
              previousClosed = previousCoverage.retire(previousSampler?.sequence ?? 0, 'context_changed')
            }
            const saved = await localApi.resumeObservation(sessionId)
            if (!current()) throw new DOMException('Cancelled', 'AbortError')
            if (saved.observation_status !== 'enabled') throw new Error('CAMERA_RESUME_NOT_SAVED')
            if (existingStream) {
              if (!await previousClosed) throw new Error('OBSERVATION_COVERAGE_PENDING')
              if (!current() || streamRef.current !== existingStream) throw new DOMException('Cancelled', 'AbortError')
              if (coverageRef.current === previousCoverage) coverageRef.current = null
              const sampler = new ObservationSampleClock()
              const coverage = retainObservationCoverage(sessionId, sampler.epoch,
                () => localApi.beginObservationStream(sessionId, sampler.epoch),
                (sequence, reason, signal) => localApi.endObservationStream(sessionId, sampler.epoch, sequence, reason, signal), bindingKey!)
              coverageRef.current = coverage
              await startupRequest(() => coverage.result)
              if (!current() || streamRef.current !== existingStream) {
                void coverage.retire(0, 'context_changed')
                throw new DOMException('Cancelled', 'AbortError')
              }
              coverage.accept()
              pendingSampler = sampler
            } else if (!streamRef.current) {
              const opening = attachRef.current()
              generation = attachGenerationRef.current
              await opening
            }
            if (!current()) throw new DOMException('Cancelled', 'AbortError')
            await waitForCameraFrame(() => ({ video: videoRef.current, stream: streamRef.current, leased: leaseRefs.current.length > 0, generation: attachGenerationRef.current }), detail.signal)
            if (!current() || (existingStream && streamRef.current !== existingStream)) throw new DOMException('Cancelled', 'AbortError')
            if (pendingSampler) observationSampleRef.current = pendingSampler
            const stream = streamRef.current
            // Refresh only after this attachment has a frame. Publishing enabled
            // earlier can make the parent's effect race the explicit attachment.
            await authorityRefreshRef.current?.(() => current() && streamRef.current === stream)
          }
          if (!current()) throw new DOMException('Cancelled', 'AbortError')
          if (detail.command === 'resume') contentFreshnessRef.current.reset()
          detail.resolve({ device_type: 'camera', device_status: detail.command === 'resume' ? 'active' : 'paused',
            frame_confirmed: detail.command === 'resume', lease_confirmed: true,
            observation_status: detail.command === 'resume' ? 'enabled' : 'paused' })
        } catch (error) {
          if (ownsStream() && !detail.signal.aborted) {
            setAutoReconnect(false)
            await release('voice_camera_failed')
            if (sameIntent()) {
              setStatus('unavailable')
              setMessage('摄像头操作尚未确认成功，当前未读取画面。')
            }
          }
          detail.reject(error instanceof Error ? error : new Error('CAMERA_ACTION_FAILED'))
        } finally { detail.signal.removeEventListener('abort', abort) }
      })()
    }
    window.addEventListener(CAMERA_DEVICE_COMMAND, control)
    return () => { disposed = true; window.removeEventListener(CAMERA_DEVICE_COMMAND, control) }
  }, [active, sessionId])

  useEffect(() => {
    const pause = (): void => {
      void release('voice_privacy_pause').then((released) => {
        setMessage(released
          ? '观察已由孩子暂停，应用已确认释放摄像头；语音输入也已停止'
          : '观察已由孩子暂停，浏览器已停止读取，但本地服务未确认设备租约释放；语音输入也已停止。')
      })
      setStatus('paused')
      setMessage('观察已由孩子暂停，正在确认本机摄像头释放；语音输入也已停止')
    }
    window.addEventListener('hsc-privacy-pause', pause)
    return () => window.removeEventListener('hsc-privacy-pause', pause)
  }, [])

  useEffect(() => {
    const pauseCamera = (): void => {
      void release('voice_camera_pause').then((released) => setMessage(released
        ? '摄像头观察已暂停并确认释放；语音陪伴仍然可用。'
        : '摄像头画面已停止读取，但本地服务尚未确认租约释放。'))
      setStatus('paused')
    }
    window.addEventListener('hsc-camera-observation-pause', pauseCamera)
    return () => window.removeEventListener('hsc-camera-observation-pause', pauseCamera)
  }, [])

  useEffect(() => {
    const resumeCamera = (): void => {
      if (!active) return
      if (streamRef.current && qualificationGate === 'failed') void retryQualification()
      else if (!streamRef.current) void toggle('resume')
      else recheckQualification()
    }
    window.addEventListener('hsc-camera-observation-resume', resumeCamera)
    return () => window.removeEventListener('hsc-camera-observation-resume', resumeCamera)
  }, [active, qualificationGate, status])

  useEffect(() => {
    if (status !== 'active' || qualificationGate !== 'ready') return
    const timer = window.setInterval(() => {
      const stream = streamRef.current
      const generation = attachGenerationRef.current
      leaseRefs.current.forEach((leaseId) => { void localApi.renewDeviceLease(leaseId).catch(() => {
        if (!stream || streamRef.current !== stream || attachGenerationRef.current !== generation) return
        void retireForReconnect('camera_lease_failed', '观察摄像头连接续期失败，正在确认释放旧连接后自动重连。')
      }) })
    }, 60_000)
    return () => window.clearInterval(timer)
  }, [status])

  useEffect(() => {
    if (!active || !enabled || status !== 'active') return
    const analyze = async (): Promise<void> => {
      const video = videoRef.current
      if (!video || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return
      const checkContent = isL523ObservationSource(deviceLabel)
      // L523 local health must keep its original two-second sampling cadence
      // even while analysis is in flight or backing off. Other devices retain
      // their original backpressure; no second analysis request is admitted.
      if (!checkContent && (analysisFlightRef.current || Date.now() < analysisRetryAtRef.current)) return
      const sourceStream = streamRef.current
      const sampler = observationSampleRef.current
      if (!sourceStream || !sampler || video.srcObject !== sourceStream) return
      const generation = attachGenerationRef.current
      const owner = pendingOwner.current
      const current = (): boolean => active && pendingOwner.current === owner && attachGenerationRef.current === generation
        && streamRef.current === sourceStream && observationSampleRef.current === sampler
      let flight: ObservationAnalysisFlight<unknown> | null = null
      try {
        const canvas = canvasRef.current ?? document.createElement('canvas')
        canvasRef.current = canvas
        canvas.width = 320
        canvas.height = Math.max(180, Math.round(320 * video.videoHeight / Math.max(video.videoWidth, 1)))
        const context = canvas.getContext('2d')
        if (!context) throw new Error('CANVAS_CONTEXT_UNAVAILABLE')
        context.drawImage(video, 0, 0, canvas.width, canvas.height)
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height)
        const health = frameHealthRef.current.inspect(pixels)
        if (health.status === 'healthy' && checkContent) {
          const content = contentFreshnessRef.current.inspect(pixels, sourceStream)
          if (content.unconfirmed) {
            setObservationReady(false)
            setObservedState(null)
            setQualificationDiagnostic({ scene: 'unusable_frame', person: 'not_evaluated' })
            if (content.recheckSource) {
              void retireForReconnect('camera_content_unconfirmed', '观察画面内容持续未变化，尚不能确认是否更新；正在自动核对一次摄像头连接，语音陪伴继续。')
            } else {
              setMessage('画面内容仍未变化，暂不据此分析行为；画面变化后会自动继续，也可以正常明确重试摄像头。语音陪伴继续。')
            }
            return
          }
        }
        // Read-only content checks above cannot bypass analysis single-flight
        // or retry backpressure for uploads, quality reports or result work.
        if (analysisFlightRef.current || Date.now() < analysisRetryAtRef.current) return
        if (health.status !== 'healthy') {
          setObservationReady(false)
          setObservedState(null)
          setQualificationDiagnostic({ scene: health.status, person: 'not_evaluated' })
          setMessage(`${health.message} 实时预览继续；画面恢复前暂停行为分析。原始画面不保存。`)
          if (lastFrameGuidanceRef.current !== health.status) {
            lastFrameGuidanceRef.current = health.status
            const guidance = health.status === 'low_contrast'
              ? '观察摄像头已经连接，但现在只看到近乎纯色的占位画面。请打开摄像头隐私挡板，移开镜头遮挡；如果仍没有真实画面，请重新插拔观察摄像头。'
              : `${health.message} 请调整观察摄像头后，我会自动继续确认画面。`
            window.dispatchEvent(new CustomEvent('hsc-agent-intervention', {
              detail: { message: guidance, sourceType: 'observation_frame_health', sessionId }
            }))
          }
          if (health.status === 'too_dark' || health.status === 'too_bright' || health.status === 'low_contrast') {
            const persistedStatus = health.status === 'low_contrast' ? 'unusable_frame' : health.status
            const sample = sampler.sample(video.currentTime)
            if (!sample) return
            const quality = startObservationAnalysis(signal => localApi.recordRendererObservationQuality(sessionId, persistedStatus, health.message, sample, signal))
            flight = quality
            analysisFlightRef.current = quality
            await quality.result
            if (!current() || analysisFlightRef.current !== quality) return
            analysisFailuresRef.current = 0
            analysisRetryAtRef.current = 0
          }
          return
        }
        lastFrameGuidanceRef.current = null
        const frameBase64 = canvas.toDataURL('image/jpeg', 0.65).split(',', 2)[1]
        const sample = sampler?.sample(video.currentTime)
        if (sampler && !sample) return
        const analysis = startObservationAnalysis(signal => localApi.analyzeObservation(sessionId, frameBase64, sample, signal))
        flight = analysis
        analysisFlightRef.current = analysis
        const result = await analysis.result
        if (!current() || analysisFlightRef.current !== flight || contentFreshnessRef.current.unconfirmed) return
        analysisFailuresRef.current = 0
        analysisRetryAtRef.current = 0
        setObservedState(result.observation_ready ? observationLabel(result.state) : null)
        setObservationReady(result.observation_ready)
        setQualificationDiagnostic({ scene: result.scene_quality.status, person: result.person_baseline.status })
        const readinessMessage = result.scene_quality.status !== 'ready'
          ? result.scene_quality.message
          : result.person_baseline.status !== 'established'
            ? result.person_baseline.message
            : result.reason
        setMessage(result.observation_ready
          ? `${readinessMessage} 原始画面不保存。`
          : `${readinessMessage} 请调整照明或坐到常用位置；通过前只做本机取景确认，不记录行为状态或发出提醒。原始画面不保存。`)
        if (result.intervention && result.observation_ready) {
          setPendingIntervention(result.intervention)
        } else {
          // If a voice response was persisted while the renderer missed its local
          // event, the next regular local analysis converges to the durable record.
          void recoverPendingIntervention()
        }
      } catch (cause) {
        if (!current()) return
        analysisFailuresRef.current = Math.min(8, analysisFailuresRef.current + 1)
        const delay = observationAnalysisRetryDelay(analysisFailuresRef.current)
        analysisRetryAtRef.current = Date.now() + delay
        setObservationReady(false)
        setObservedState(null)
        if (cause instanceof ApiError && (cause.status === 403 || cause.code?.startsWith('MONEY_'))) {
          void retireForReconnect('observation_analysis_denied', '当前画面分析未获准，已停止读取；请核对原状态后再明确恢复观察。', false)
          return
        }
        if ((cause instanceof Error && cause.message === OBSERVATION_ANALYSIS_TIMEOUT)
          || (cause instanceof ApiError && cause.status === 409 && cause.code === 'OBSERVATION_CAPTURE_STALE')) {
          void retireForReconnect('observation_analysis_unconfirmed', '本次画面分析未能及时核对，已暂停分析；确认原观察时段和连接关闭后会自动重新取景。')
          return
        }
        setMessage(cause instanceof ApiError ? `本地状态分析暂不可用：${cause.message}；稍后会自动核对新画面。` : '本地状态分析暂不可用，摄像头预览仍在继续；稍后会自动核对新画面。')
      } finally {
        if (analysisFlightRef.current === flight) analysisFlightRef.current = null
        if (current()) setRechecking(false)
      }
    }
    void analyze()
    const timer = window.setInterval(() => { void analyze() }, FRAME_HEALTH_SAMPLE_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [active, deviceLabel, enabled, manualRecheck, qualificationGate, recoverPendingIntervention, sessionId, status])

  const toggle = async (command: 'pause' | 'resume' = streamRef.current || status === 'active' || status === 'starting' ? 'pause' : 'resume'): Promise<void> => {
    if (!active) return
    if (command === 'resume') setStatus('starting')
    const requested = requestCameraDevice(sessionId, 'camera', command)
    const sequence = deviceCommandSequenceRef.current
    try {
      await requested
      if (deviceCommandSequenceRef.current !== sequence) return
      if (command === 'pause') setMessage('观察已由用户暂停，应用已确认释放摄像头；物理电源状态未知')
    } catch {
      // The shared receiver owns current-intent errors and retired-stream cleanup.
      // A late UI promise must not change the state of a newer command.
    }
  }

  const resetReminderPreference = async (): Promise<void> => {
    setResettingPreference(true)
    try {
      await localApi.resetSessionBehaviorPersonalization(sessionId)
      setMessage('已重置提示偏好；之后会先使用默认温和、先询问的提醒。')
    } catch (cause) {
      setMessage(cause instanceof ApiError ? cause.message : '提示偏好没有重置，请重试。')
    } finally { setResettingPreference(false) }
  }

  const retryQualification = async (): Promise<void> => {
    const generation = attachGenerationRef.current
    const stream = streamRef.current
    if (!stream || !active) return
    const current = (): boolean => active && streamRef.current === stream && attachGenerationRef.current === generation
    try {
      if (await startQualification(current) && current()) {
        setStatus('active')
        setMessage('正在重新确认取景和常坐位置；通过前不会记录行为状态或发出提醒。')
      }
    } catch (cause) {
      if (!current()) return
      if (cause instanceof Error && cause.message === 'OBSERVATION_STARTUP_TIMEOUT') {
        void retireForReconnect('camera_startup_api_timeout', '取景确认超过 12 秒仍未完成，已停止画面读取；确认旧连接安全关闭后会自动重连。')
      }
    }
  }

  const recheckQualification = (): void => {
    if (status === 'unavailable') {
      void toggle()
      return
    }
    if (status !== 'active' || qualificationGate !== 'ready') return
    contentFreshnessRef.current.reset()
    setRechecking(true)
    setMessage('正在重新检测本机取景；原始画面不保存。')
    setManualRecheck((current) => current + 1)
  }

  const respondToIntervention = async (accepted: boolean, note?: string, retry = false): Promise<void> => {
    const retained = retry ? unresolvedFeedback : null
    const intervention = retained?.intervention ?? pendingIntervention
    if (retry && !retained) return
    if (!intervention || feedbackFlight.current) return
    const owner = pendingOwner.current
    const flight = {}
    feedbackFlight.current = flight
    const current = (): boolean => pendingOwner.current === owner && feedbackFlight.current === flight
    const actionKey = `${intervention.intervention_id}:${accepted ? 'accepted' : note ?? 'declined'}`
    const clientActionId = retained?.clientActionId ?? feedbackActionIdsRef.current[actionKey] ?? crypto.randomUUID()
    setFeedbackBusy(true)
    try {
      if (!retained) {
        const latest = await recoverPendingIntervention()
        if (!current() || latest === undefined) return
        if (latest?.intervention_id !== intervention.intervention_id) {
          setMessage('原提醒已不再等待回答，没有提交这次回答。')
          return
        }
      }
      feedbackActionIdsRef.current[actionKey] = clientActionId
      // Retain only the exact attempted command; a lost response is not a new invitation.
      setFeedbackRecovery({ owner, intervention, accepted, note, clientActionId })
      await localApi.respondToSessionBehaviorIntervention(sessionId, intervention.intervention_id, accepted, note, clientActionId)
      if (!current()) return
      setFeedbackRecovery(null)
      delete feedbackActionIdsRef.current[actionKey]
      setPendingIntervention(null)
      setMessage(accepted ? '已记下：你愿意试一试。' : note === 'less_like_this' ? '已记下：这类提醒会更少、更先询问。' : '已记下：你想稍后再说。')
    } catch (cause) {
      if (!current()) return
      setMessage(cause instanceof ApiError ? `这次回答没有保存：${cause.message}` : '这次回答没有保存，请重试。')
      if (cause instanceof ApiError && (cause.status === 404 || cause.status === 409)) {
        setFeedbackRecovery(null)
        setPendingIntervention(null)
        await recoverPendingIntervention()
      }
    } finally { if (current()) { feedbackFlight.current = null; setFeedbackBusy(false) } }
  }

  const showQualificationGuide = active && observationReady === false && qualificationGate === 'ready' && status !== 'paused' && status !== 'inactive'
  const sceneUnusable = observationReady === false
    && !['not_evaluated', 'ready'].includes(qualificationDiagnostic.scene)

  return (
    <aside className={`observation-strip is-${status}${status === 'active' && observationReady === false ? ' is-scene-unready' : ''}${previewExpanded ? ' is-preview-expanded' : ''}`} aria-label="本地观察状态">
      <div className="observation-preview-button device-live-video-frame" title="观察摄像头实时预览">
        <video ref={(element) => { videoRef.current = element }} autoPlay muted playsInline aria-label="观察摄像头实时画面" />
        <span className={`device-frame-badge is-${sceneUnusable ? 'unavailable' : status}`}>{status === 'active' ? sceneUnusable ? '画面不可用' : '实时画面' : status === 'starting' ? '等待首帧' : status === 'unavailable' ? '画面不可用' : status === 'paused' ? '已暂停' : '未读取'}</span>
      </div>
      <div className="observation-copy"><strong>{status === 'active' ? sceneUnusable ? '观察摄像头已连接 · 画面不可用' : observationReady === false ? '观察摄像头已连接 · 正在确认取景' : `观察摄像头画面可用${observedState ? ` · ${observedState}` : ''}` : status === 'paused' ? '本地观察已暂停' : status === 'starting' && qualificationGate === 'failed' ? '取景确认暂不可用 · 预览未上传' : status === 'starting' ? '正在连接并验证画面' : status === 'inactive' ? '本地观察已关闭' : '本地观察不可用'}</strong><div className="device-live-facts" aria-label="观察摄像头连接详情"><span title={deviceLabel}>设备：{deviceLabel}</span><span>画面：{frameDimensions ?? '尚未确认'}</span><span>重连：{reconnectAttempt > 0 ? `第 ${reconnectAttempt} 次` : '无需重连'}</span></div><span>{snoozed ? `主动提醒已暂停到${new Date(snoozeUntil!).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}。` : message}</span></div>
      {showQualificationGuide && (
        <ObservationQualificationGuide
          scene={qualificationDiagnostic.scene}
          person={qualificationDiagnostic.person}
          unavailable={status === 'unavailable'}
          rechecking={rechecking}
          onRecheck={recheckQualification}
        />
      )}
      {qualificationGate === 'failed' && streamRef.current && <div className="observation-recovery" role="status"><span>取景确认没有保存，当前预览不会进入行为模型。</span><button className="secondary-button" onClick={() => void retryQualification()}>重试取景确认</button></div>}
      {status === 'unavailable' && active && !showQualificationGuide && <div className="observation-recovery" role="status"><span>本次设备或取景没有通过，不会形成行为结论或长期记录。调整镜头、照明或设备占用后可重试。</span><button className="secondary-button" onClick={() => void toggle()}>重新尝试本地观察</button></div>}
      {pendingIntervention && !unresolvedFeedback && <div className="observation-feedback" role="group" aria-label="回答学习提醒"><span>{pendingIntervention.message}</span>{!pendingRecoveryError && !pendingChecking && <small>直接说“愿意”“稍后”或“这类提醒少一点”。</small>}<div><button className="secondary-button" disabled={feedbackBusy || pendingChecking || Boolean(pendingRecoveryError)} onClick={() => void respondToIntervention(true)}>愿意</button><button className="secondary-button" disabled={feedbackBusy || pendingChecking || Boolean(pendingRecoveryError)} onClick={() => void respondToIntervention(false, 'later')}>稍后</button><button className="secondary-button" disabled={feedbackBusy || pendingChecking || Boolean(pendingRecoveryError)} onClick={() => void respondToIntervention(false, 'less_like_this')}>这类提醒少一点</button></div></div>}
      {unresolvedFeedback && !feedbackBusy && <div className="observation-recovery" role="status"><span>原回答的回执尚未确认。核对只重试原操作，不回答其他提醒。</span><button className="secondary-button" onClick={() => void respondToIntervention(unresolvedFeedback.accepted, unresolvedFeedback.note, true)}>核对原回答</button></div>}
      {pendingRecoveryError && <div className="observation-recovery" role="status"><span>{pendingRecoveryError}</span><button className="secondary-button" disabled={pendingChecking} onClick={() => void recoverPendingIntervention()}>重试恢复</button></div>}
      <div className="observation-actions">
        <button className="icon-button" title={previewExpanded ? '收起摄像头预览' : '放大摄像头预览'} onClick={() => setPreviewExpanded((current) => !current)}>{previewExpanded ? <Minimize2 size={18} /> : <Maximize2 size={18} />}</button>
        {active && <button className="icon-button" aria-label="重置提示偏好" title="重置提示偏好" disabled={resettingPreference} onClick={() => void resetReminderPreference()}><RotateCcw size={18} /></button>}
        {active && <button className="icon-button" aria-label={status === 'active' || status === 'starting' ? '暂停本地观察' : '恢复本地观察'} title={status === 'active' || status === 'starting' ? '暂停本地观察' : '恢复本地观察'} onClick={() => void toggle()}>
          {status === 'active' || status === 'starting' ? <EyeOff size={19} /> : <Camera size={19} />}
        </button>}
      </div>
    </aside>
  )
}

function ObservationQualificationGuide({
  scene,
  person,
  unavailable,
  rechecking,
  onRecheck,
}: {
  scene: string
  person: string
  unavailable: boolean
  rechecking: boolean
  onRecheck: () => void
}): React.JSX.Element {
  const guide = observationQualificationGuide(scene, person)
  return (
    <div className="observation-qualification-guide" role="status" aria-live="polite">
      <div><strong>{guide.title}</strong><span>{guide.detail}</span></div>
      <ol>{guide.steps.map((step) => <li key={step}>{step}</li>)}</ol>
      <small>只在本机即时检查取景；不会保存、上传或展示原始画面。通过前不记录行为状态或长期记录，也不发出提醒。</small>
      <button className="secondary-button" disabled={rechecking} onClick={onRecheck}>{rechecking ? '正在重新检测' : unavailable ? '重新打开并检测' : '立即重新检测'}</button>
    </div>
  )
}

function observationQualificationGuide(scene: string, person: string): { title: string; detail: string; steps: string[] } {
  if (scene === 'too_dark') return {
    title: '先补光，再重新检测', detail: '当前画面太暗或对比度不足，暂不推断行为。',
    steps: ['打开正面照明，避免背光或镜头直对灯光。', '将摄像头对准常坐位置，让画面保持清楚。', '完成后点“立即重新检测”。'],
  }
  if (scene === 'too_bright') return {
    title: '先避开强光，再重新检测', detail: '当前画面过亮，暂不推断行为。',
    steps: ['避免镜头正对灯光或窗户。', '将摄像头对准常坐位置，让画面保持清楚。', '完成后点“立即重新检测”。'],
  }
  if (scene === 'blurred' || scene === 'frozen' || scene === 'unusable_frame' || scene === 'low_contrast') return {
    title: '先恢复清晰画面，再重新检测', detail: '当前画面无法可靠用于本地观察。',
    steps: ['打开摄像头隐私挡板，确认镜头没有被遮住。', '擦拭镜头并重新对准常坐位置。', '仍是纯色画面时，关闭占用摄像头的程序并重新插拔设备。'],
  }
  if (person === 'awaiting_person') return {
    title: '请入镜并正对镜头', detail: '画面已经基本可用，正在确认本次会话的人物基线。',
    steps: ['坐到常坐位置，保持上半身在画面内。', '正对镜头几秒，不需要做表情或动作。', '完成后点“立即重新检测”。'],
  }
  return {
    title: '正在确认本机取景', detail: '通过前不会记录行为状态、形成长期结论或发出提醒。',
    steps: ['确认摄像头对准常坐位置。', '让孩子自然坐在画面内并正对镜头几秒。', '需要时点“立即重新检测”。'],
  }
}

function observationLabel(state: string): string {
  return ({ unknown: '等待更多画面', away: '暂时离开候选', writing_like: '书写活动候选', paused_like: '低活动候选', present: '在镜头前' } as Record<string, string>)[state] ?? '状态待确认'
}

function sourceLabel(sourceType: string): string {
  return ({
    school_homework: '校内作业',
    tomorrow_event: '明日重点',
    due_review: '到期复习',
    mistake_review: '错题强化',
    pet_daily: 'PET长期目标',
    competition_math: '数学思维',
    planned_break: '计划休息'
  } as Record<string, string>)[sourceType] ?? '今日任务'
}

function completionEvidenceLabel(evidenceType: string | null): string {
  return ({
    child_checked: '已检查',
    needs_correction: '待订正',
    voice_confirmed: '语音确认',
    planned_break: '休息完成',
    review_evidence: '复测已记录',
    child_confirmed: '已完成'
  } as Record<string, string>)[evidenceType ?? 'child_confirmed'] ?? '已完成'
}

function recallStatusLabel(status: string): string {
  return ({ child_confirmed: '孩子确认', inferred: '待确认候选', unknown: '证据不足' } as Record<string, string>)[status] ?? '待处理'
}

function CheckInStep({
  workspace,
  busy,
  run
}: {
  workspace: SessionWorkspaceState
  busy: boolean
  run: (action: () => Promise<unknown>) => Promise<boolean>
}): React.JSX.Element {
  const context = workspace.planning_context
  const restoredDraft = workspace.drafts.check_in?.payload ?? {}
  const restoredEnergy = draftString(restoredDraft, 'energy_level', context.energy_level)
  const [energyLevel, setEnergyLevel] = useState<'low' | 'medium' | 'high'>(['low', 'medium', 'high'].includes(restoredEnergy) ? restoredEnergy as 'low' | 'medium' | 'high' : context.energy_level)
  const [fixedMinutes, setFixedMinutes] = useState(draftNumber(restoredDraft, 'fixed_minutes', context.fixed_commitment_minutes))
  const [fixedNote, setFixedNote] = useState(draftString(restoredDraft, 'fixed_note', context.fixed_commitment_note ?? ''))
  const [tomorrowSubject, setTomorrowSubject] = useState(draftString(restoredDraft, 'tomorrow_subject', context.tomorrow_event_subject ?? '数学'))
  const [tomorrowTitle, setTomorrowTitle] = useState(draftString(restoredDraft, 'tomorrow_title', context.tomorrow_event_title ?? ''))
  const [tomorrowMinutes, setTomorrowMinutes] = useState(draftNumber(restoredDraft, 'tomorrow_minutes', context.tomorrow_event_minutes || 10))
  const draft = useAutosavedDraft(workspace.session.session_id, 'check_in', {
    energy_level: energyLevel,
    fixed_minutes: fixedMinutes,
    fixed_note: fixedNote,
    tomorrow_subject: tomorrowSubject,
    tomorrow_title: tomorrowTitle,
    tomorrow_minutes: tomorrowMinutes
  }, true, Boolean(workspace.drafts.check_in))

  const continueToRecall = async (): Promise<void> => {
    const completed = await run(async () => {
      await localApi.savePlanningContext(workspace.session.session_id, {
        energyLevel,
        fixedCommitmentMinutes: fixedMinutes,
        fixedCommitmentNote: fixedNote.trim(),
        tomorrowEventSubject: tomorrowSubject,
        tomorrowEventTitle: tomorrowTitle.trim(),
        tomorrowEventMinutes: tomorrowMinutes
      })
      await localApi.transition(workspace.session.session_id, 'check_in', 'classroom_recall', 'check_in_complete')
    })
    if (completed) draft.markCommitted()
  }

  useEffect(() => { speakOrdinaryPrompt('欢迎回家。先喝口水，整理一下桌面。准备好后，我们一起回想今天的课堂。', workspace.session) }, [])
  return (
    <div className="step-panel check-in-panel">
      <span className="section-kicker">开始前</span>
      <h2>今天感觉怎么样？</h2>
      <div className="check-in-grid">
        <section>
          <strong>现在的精力</strong>
          <div className="track-control" role="group" aria-label="现在的精力">
            {([['low', '有点累'], ['medium', '还可以'], ['high', '状态好']] as const).map(([value, label]) => (
              <button key={value} type="button" aria-pressed={energyLevel === value} onClick={() => setEnergyLevel(value)}>{label}</button>
            ))}
          </div>
        </section>
        <label>今晚还有多少分钟固定安排
          <input type="number" min="0" max="180" step="5" value={fixedMinutes} onChange={(event) => setFixedMinutes(Math.max(0, Math.min(180, Number(event.target.value))))} />
        </label>
        <label>固定安排是什么（可跳过）
          <input value={fixedNote} maxLength={200} onChange={(event) => setFixedNote(event.target.value)} placeholder="例如：晚饭、洗澡、训练" />
        </label>
        <section className="tomorrow-event-fields">
          <strong>明天有检查或考试吗？（可跳过）</strong>
          <label>学科<select value={tomorrowSubject} onChange={(event) => setTomorrowSubject(event.target.value)}>{['语文', '数学', '英语', '物理', '化学', '其他'].map((item) => <option key={item}>{item}</option>)}</select></label>
          <label className="grow-field">内容<input value={tomorrowTitle} maxLength={200} onChange={(event) => setTomorrowTitle(event.target.value)} placeholder="例如：英语单词听写" /></label>
          <label>准备分钟<input type="number" min="0" max="60" step="5" disabled={!tomorrowTitle.trim()} value={tomorrowMinutes} onChange={(event) => setTomorrowMinutes(Math.max(0, Math.min(60, Number(event.target.value))))} /></label>
        </section>
      </div>
      <DraftSaveStatus state={draft.state} restored={Boolean(workspace.drafts.check_in)} />
      <p>我会把这些算进今天的时间表，不会把时间排满。</p>
      <button className="primary-button" disabled={busy || (Boolean(tomorrowTitle.trim()) && tomorrowMinutes < 5)} onClick={() => void continueToRecall()}>
        我准备好了<ArrowRight size={18} />
      </button>
    </div>
  )
}

function RecallStep({
  workspace,
  busy,
  run
}: {
  workspace: SessionWorkspaceState
  busy: boolean
  run: (action: () => Promise<unknown>) => Promise<boolean>
}): React.JSX.Element {
  const restoredDraft = workspace.drafts.classroom_recall?.payload ?? {}
  const [subject, setSubject] = useState(draftString(restoredDraft, 'subject', '数学'))
  const [summary, setSummary] = useState(draftString(restoredDraft, 'summary'))
  const canAdd = summary.trim().length >= 2
  const draft = useAutosavedDraft(workspace.session.session_id, 'classroom_recall', { subject, summary }, Boolean(summary.trim()), Boolean(workspace.drafts.classroom_recall))

  const add = async (): Promise<void> => {
    const saved = await run(() => localApi.addRecall(workspace.session.session_id, subject, summary.trim()))
    if (saved) {
      await draft.clear()
      setSummary('')
    }
  }

  const continueToHomework = async (): Promise<void> => {
    const moved = await run(() => localApi.transition(
      workspace.session.session_id,
      'classroom_recall',
      'homework_capture',
      'recall_confirmed'
    ))
    if (moved) draft.markCommitted()
  }

  return (
    <div className="form-layout">
      <div className="form-main">
        <span className="section-kicker">课堂复盘</span>
        <h2>今天老师主要讲了什么？</h2>
        <div className="field-row">
          <label>
            学科
            <select value={subject} onChange={(event) => setSubject(event.target.value)}>
              {['语文', '数学', '英语', '物理', '化学', '其他'].map((item) => <option key={item}>{item}</option>)}
            </select>
          </label>
          <label className="grow-field">
            我记得
            <textarea
              value={summary}
              onChange={(event) => setSummary(event.target.value)}
              placeholder="不用说得很完整，想到什么就写什么。"
            />
            <VoiceInputButton sessionId={workspace.session.session_id} purpose="classroom_recall" childId={workspace.session.child_id} onTranscript={(text) => setSummary((current) => current ? `${current} ${text}` : text)} />
          </label>
        </div>
        <button className="secondary-button" disabled={busy || !canAdd} onClick={() => void add()}>
          <Plus size={18} />记下来
        </button>
        <DraftSaveStatus state={draft.state} restored={Boolean(workspace.drafts.classroom_recall)} />
      </div>
      <section className="saved-section recall-review-list">
        <h3>已经记下</h3>
        {workspace.recalls.length === 0 ? <p>{workspace.session.recall_declared_empty ? '已记录：现在暂时想不起来，不会据此补写课堂内容。' : '还没有课堂内容'}</p> : workspace.recalls.filter((item) => item.confirmation_status !== 'rejected').map((item) => (
          <div className={`recall-review-row status-${item.confirmation_status}`} key={item.recall_id}>
            <div><strong>{item.subject} · {recallStatusLabel(item.confirmation_status)}</strong><span>{item.summary}</span>{item.record_kind === 'system_candidate' && <small className="recall-evidence">{item.candidate_nodes.length > 0 ? `候选知识点：${item.candidate_nodes.map((node) => node.title).join('、')}` : '候选知识点：尚不确定'} · {item.textbook_mapping?.label ?? '尚未映射'} · 依据：{item.evidence_sources.join('、')}</small>}</div>
            {item.confirmation_status === 'inferred' && <div className="recall-actions"><button className="secondary-button" disabled={busy} onClick={() => void run(() => localApi.reviewRecall(workspace.session.session_id, item.recall_id, 'confirm'))}>确实讲了</button><button className="secondary-button" disabled={busy} onClick={() => void run(() => localApi.reviewRecall(workspace.session.session_id, item.recall_id, 'reject'))}>不是这个</button></div>}
            {item.confirmation_status === 'unknown' && <button className="secondary-button" disabled={busy} onClick={() => void run(() => localApi.reviewRecall(workspace.session.session_id, item.recall_id, 'reject'))}>先忽略</button>}
          </div>
        ))}
        {workspace.recalls.length === 0 && <button className="secondary-button" disabled={busy || Boolean(workspace.session.recall_declared_empty)} onClick={() => void run(() => localApi.declareNoRecall(workspace.session.session_id))}>今天暂时想不起来</button>}
      </section>
      <div className="step-actions">
        <button
          className="primary-button"
          disabled={busy || (workspace.recalls.length === 0 && !workspace.session.recall_declared_empty)}
          onClick={() => void continueToHomework()}
        >
          接着看作业<ArrowRight size={18} />
        </button>
      </div>
    </div>
  )
}

function HomeworkStep({
  workspace,
  busy,
  run
}: {
  workspace: SessionWorkspaceState
  busy: boolean
  run: (action: () => Promise<unknown>) => Promise<boolean>
}): React.JSX.Element {
  const restoredDraft = workspace.drafts.homework_capture?.payload ?? {}
  const [subject, setSubject] = useState(draftString(restoredDraft, 'subject', '数学'))
  const [title, setTitle] = useState(draftString(restoredDraft, 'title'))
  const [minutes, setMinutes] = useState(draftNumber(restoredDraft, 'minutes', 25))
  const [taskType, setTaskType] = useState(draftString(restoredDraft, 'task_type', 'written'))
  const [rangeText, setRangeText] = useState(draftString(restoredDraft, 'range_text'))
  const [dueAt, setDueAt] = useState(draftString(restoredDraft, 'due_at'))
  const [specialRequirements, setSpecialRequirements] = useState(draftString(restoredDraft, 'special_requirements'))
  const [sourceArtifactId, setSourceArtifactId] = useState<string | undefined>(draftString(restoredDraft, 'source_artifact_id') || undefined)
  const [ocrConfidence, setOcrConfidence] = useState<number | undefined>(typeof restoredDraft.ocr_confidence === 'number' ? restoredDraft.ocr_confidence : undefined)
  const restoredEditingId = draftString(restoredDraft, 'editing_id')
  const [editingId, setEditingId] = useState<string | null>(workspace.homework.some((item) => item.homework_id === restoredEditingId) ? restoredEditingId : null)
  const [carryoverOutcomes, setCarryoverOutcomes] = useState<Record<string, 'completed_elsewhere' | 'cancelled'>>({})
  const hasDraftContent = Boolean(title.trim() || rangeText.trim() || dueAt || specialRequirements.trim() || sourceArtifactId || editingId)
  const draft = useAutosavedDraft(workspace.session.session_id, 'homework_capture', {
    subject,
    title,
    minutes,
    task_type: taskType,
    range_text: rangeText,
    due_at: dueAt,
    special_requirements: specialRequirements,
    source_artifact_id: sourceArtifactId ?? null,
    ocr_confidence: ocrConfidence ?? null,
    editing_id: editingId
  }, hasDraftContent, Boolean(workspace.drafts.homework_capture))

  const clearForm = (): void => {
    setTitle('')
    setRangeText('')
    setDueAt('')
    setSpecialRequirements('')
    setSourceArtifactId(undefined)
    setOcrConfidence(undefined)
    setEditingId(null)
  }

  const save = async (): Promise<void> => {
    const values = {
      subject,
      title: title.trim(),
      expectedMinutes: minutes,
      taskType,
      rangeText: rangeText.trim(),
      dueAt,
      specialRequirements: specialRequirements.trim(),
      sourceArtifactId,
      ocrConfidence
    }
    const saved = editingId
      ? await run(() => localApi.updateHomework(workspace.session.session_id, editingId, values))
      : await run(() => localApi.addHomework(workspace.session.session_id, subject, title.trim(), minutes, {
        taskType,
        rangeText: rangeText.trim(),
        dueAt,
        specialRequirements: specialRequirements.trim(),
        sourceArtifactId,
        ocrConfidence
      }))
    if (saved) {
      await draft.clear()
      clearForm()
    }
  }

  const edit = (item: SessionWorkspaceState['homework'][number]): void => {
    setEditingId(item.homework_id)
    setSubject(item.subject)
    setTitle(item.title)
    setMinutes(item.expected_minutes)
    setTaskType(item.task_type || 'written')
    setRangeText(item.range_text ?? '')
    setDueAt(item.due_at ?? '')
    setSpecialRequirements(item.special_requirements ?? '')
    setSourceArtifactId(item.source_artifact_id ?? undefined)
    setOcrConfidence(item.ocr_confidence ?? undefined)
  }

  const cancelEdit = async (): Promise<void> => {
    await draft.clear()
    clearForm()
  }

  const generatePlan = async (): Promise<void> => {
    const generated = await run(() => localApi.generatePlan(workspace.session.session_id))
    if (generated) draft.markCommitted()
  }

  const resolveCarryover = async (
    sourcePlanItemId: string,
    decision: 'continue_today' | 'completed_elsewhere' | 'cancelled'
  ): Promise<void> => {
    const reason = decision === 'continue_today'
      ? '孩子确认今天继续完成'
      : decision === 'completed_elsewhere'
        ? '孩子确认已在其他时间完成'
        : '孩子确认老师已取消或不再要求'
    await run(() => localApi.reviewDeferredTask(
      workspace.session.session_id,
      sourcePlanItemId,
      decision,
      reason
    ))
  }

  return (
    <div className="form-layout">
      <div className="form-main">
        <span className="section-kicker">今日作业</span>
        <h2>把要完成的事情列清楚</h2>
        {workspace.carryover_candidates.length > 0 && (
          <section className="carryover-review" aria-label="处理上次延期任务">
            <div>
              <strong>先处理上次延期的任务</strong>
              <span>逐项确认，不会自动把它们全部放进今天。</span>
            </div>
            {workspace.carryover_candidates.map((item) => {
              const outcome = carryoverOutcomes[item.source_plan_item_id] ?? 'completed_elsewhere'
              return (
                <div className="carryover-row" key={item.source_plan_item_id}>
                  <div>
                    <strong>{item.subject} · {item.title} · {item.expected_minutes}分钟</strong>
                    <span>{item.source_date}延期：{item.reschedule_reason}</span>
                  </div>
                  <button className="secondary-button" disabled={busy} onClick={() => void resolveCarryover(item.source_plan_item_id, 'continue_today')}>
                    <CalendarClock size={17} />今天继续
                  </button>
                  <label>不再安排的原因
                    <select
                      aria-label={`${item.title}不再安排的原因`}
                      value={outcome}
                      onChange={(event) => setCarryoverOutcomes((current) => ({
                        ...current,
                        [item.source_plan_item_id]: event.target.value as 'completed_elsewhere' | 'cancelled'
                      }))}
                    >
                      <option value="completed_elsewhere">已在其他时间完成</option>
                      <option value="cancelled">老师已取消或不再要求</option>
                    </select>
                  </label>
                  <button className="secondary-button" disabled={busy} onClick={() => void resolveCarryover(item.source_plan_item_id, outcome)}>
                    <Check size={17} />确认不再安排
                  </button>
                </div>
              )
            })}
          </section>
        )}
        <div className="homework-fields">
          <label>学科<select value={subject} onChange={(event) => setSubject(event.target.value)}>{['语文', '数学', '英语', '物理', '化学', '其他'].map((item) => <option key={item}>{item}</option>)}</select></label>
          <label>任务类型<select value={taskType} onChange={(event) => setTaskType(event.target.value)}>{Object.entries(homeworkTypeLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <label className="grow-field">作业内容<input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：练习册第32页" /></label>
          <label>预计分钟<input type="number" min="5" max="180" step="5" value={minutes} onChange={(event) => setMinutes(Number(event.target.value))} /></label>
          <label>页码/范围<input value={rangeText} maxLength={200} onChange={(event) => setRangeText(event.target.value)} placeholder="例如：32-34页" /></label>
          <label>截止时间<input type="datetime-local" value={dueAt} onChange={(event) => setDueAt(event.target.value)} /></label>
          <label className="grow-field">老师特别要求<input value={specialRequirements} maxLength={500} onChange={(event) => setSpecialRequirements(event.target.value)} placeholder="例如：要求用完整句回答" /></label>
        </div>
        <div className="homework-form-actions">
          <button className="secondary-button" disabled={busy || title.trim().length < 2} onClick={() => void save()}>
            {editingId ? <Check size={18} /> : <Plus size={18} />}{editingId ? '保存修改' : '加入作业'}
          </button>
          {editingId && <button className="secondary-button" disabled={busy} onClick={() => void cancelEdit()}>取消修改</button>}
          <DraftSaveStatus state={draft.state} restored={Boolean(workspace.drafts.homework_capture)} />
        </div>
        {sourceArtifactId && (
          <p className={`homework-source-note ${(ocrConfidence ?? 0) < 0.5 ? 'is-warning' : ''}`}>
            已关联本次高拍仪证据，OCR置信度{Math.round((ocrConfidence ?? 0) * 100)}%。作业只在你确认后加入计划。
          </p>
        )}
        <DocumentCameraCapture
          sessionId={workspace.session.session_id}
          onText={(text, artifact) => {
            setTitle(text.replace(/\s+/g, ' ').slice(0, 500))
            setSourceArtifactId(artifact.artifact_id)
            setOcrConfidence(artifact.details.ocr_confidence ?? artifact.confidence)
          }}
        />
      </div>
      <section className="saved-section homework-review-list">
        <h3>今日任务</h3>
        {workspace.homework.length === 0 ? <p>{workspace.session.homework_declared_empty ? '已记录：今天没有校内作业。' : '还没有确认作业'}</p> : workspace.homework.map((item) => (
          <div className={editingId === item.homework_id ? 'is-editing' : ''} key={item.homework_id}>
            <div>
              <strong>{item.subject} · {homeworkTypeLabels[item.task_type] ?? '作业'} · {item.expected_minutes}分钟</strong>
              <span>{item.title}{item.range_text ? ` · ${item.range_text}` : ''}{item.due_at ? ` · 截止${item.due_at.replace('T', ' ')}` : ''}</span>
              {item.special_requirements && <small>老师要求：{item.special_requirements}</small>}
            </div>
            <button className="secondary-button" disabled={busy} onClick={() => edit(item)}>修改</button>
          </div>
        ))}
        {workspace.homework.length === 0 && <button className="secondary-button" disabled={busy || Boolean(workspace.session.homework_declared_empty) || workspace.carryover_candidates.length > 0} onClick={() => void run(() => localApi.declareNoHomework(workspace.session.session_id))}>今天没有作业</button>}
      </section>
      <div className="step-actions">
        <button className="primary-button" disabled={busy || workspace.carryover_candidates.length > 0 || (workspace.homework.length === 0 && !workspace.session.homework_declared_empty)} onClick={() => void generatePlan()}>
          制定今天的计划<ArrowRight size={18} />
        </button>
      </div>
    </div>
  )
}

function PlanStep({ workspace, busy, run }: { workspace: SessionWorkspaceState; busy: boolean; run: (action: () => Promise<unknown>) => Promise<boolean> }): React.JSX.Element {
  const total = useMemo(() => workspace.plan_items.filter((item) => item.status !== 'rescheduled').reduce((sum, item) => sum + item.expected_minutes, 0), [workspace.plan_items])
  return (
    <div className="form-layout">
      <div className="plan-heading">
        <div><span className="section-kicker">今日计划</span><h2>{workspace.plan_items.length ? '先完成校内任务' : '今天没有必须完成的任务'}</h2></div>
        <div className="time-total"><Clock size={18} /><strong>{total}</strong><span>分钟</span></div>
      </div>
      <div className="plan-list">
        <SupplyObligationStatus obligations={workspace.supply_obligations} />
        {workspace.plan_items.length === 0 && <p>没有校内作业、明日重点、到期复测或已配置的长期任务。可以直接进入简短复盘。</p>}
        {workspace.plan_items.map((item, index) => (
          <div className="plan-row" key={item.plan_item_id}>
            <i>{index + 1}</i><div><strong>{planItemTitle(item)}</strong><span>{item.subject} · {sourceLabel(item.source_type)} · {item.completion_standard}</span></div><b>{item.status === 'rescheduled' ? '建议延期' : `${item.expected_minutes}分钟`}</b>
          </div>
        ))}
      </div>
      {workspace.plan_items.length > 1 && <div className="plan-order-control">
        <div><strong>你想怎么开始？</strong><span>只调整顺序，任务不会丢失。</span></div>
        <div className="track-control" role="group" aria-label="计划顺序">
          <button disabled={busy} onClick={() => void run(() => localApi.reorderPlan(workspace.session.session_id, 'school_first', '孩子选择按校内任务顺序开始'))}>按作业顺序</button>
          <button disabled={busy} onClick={() => void run(() => localApi.reorderPlan(workspace.session.session_id, 'short_school_first', '孩子选择先完成较短的校内任务'))}>先做短任务</button>
        </div>
      </div>}
      <p className="plan-source">本地硬约束计划：校内任务优先且不静默丢失，超量部分明确建议延期；四年级保留 PET 最小任务，有余量再安排数学思维。</p>
      <div className="step-actions">
        <button className="primary-button" disabled={busy} onClick={() => void run(() => localApi.transition(workspace.session.session_id, 'plan_review', 'study', 'plan_confirmed'))}>
          按这个计划开始<ArrowRight size={18} />
        </button>
      </div>
    </div>
  )
}

function StudyStep({ workspace, childGrade, busy, run }: { workspace: SessionWorkspaceState; childGrade: string; busy: boolean; run: (action: () => Promise<unknown>, isCurrent?: () => boolean) => Promise<boolean> }): React.JSX.Element {
  const privacyStopped = useLocalPrivacyStop(workspace.session.session_id)
  const [captureCommandId, setCaptureCommandId] = useState<string>()
  const restoredDraft = workspace.drafts.study?.payload ?? {}
  const restoredTutorRequestRecovery = parseTutorRequestRecovery(restoredDraft.tutor_request_recovery, workspace.session.session_id, workspace.session.child_id)
  const [now, setNow] = useState(Date.now())
  const [question, setQuestion] = useState(draftString(restoredDraft, 'question') || restoredTutorRequestRecovery?.question || '')
  const [tutorTurn, setTutorTurn] = useState<TutorTurn | null>(workspace.latest_tutor_turn)
  const [tutorFeedbackText, setTutorFeedbackText] = useState(draftString(restoredDraft, 'tutor_feedback_text'))
  const [tutorFeedbackMessage, setTutorFeedbackMessage] = useState<string | null>(null)
  const [tutorBusy, setTutorBusy] = useState(false)
  const [tutorError, setTutorError] = useState<string | null>(null)
  const [tutorSafetyBlocked, setTutorSafetyBlocked] = useState(false)
  const [tutorUnknownOutcome, setTutorUnknownOutcome] = useState(false)
  const [diagnosticTrack, setDiagnosticTrack] = useState(draftString(restoredDraft, 'diagnostic_track', 'school'))
  const [diagnosticItem, setDiagnosticItem] = useState<DiagnosticItem | null>(null)
  const diagnosticPromptBinding = useRef<NonNullable<import('./types').DiagnosticPromptResult['prompt']> | null>(null)
  const restoredQuestionViewport = useRef<import('./types').DiagnosticPromptResult['prompt']>(null)
  const [savedNativeWritingDraft, setSavedNativeWritingDraft] = useState<TypedWritingDraft | null>(() =>
    restoreTypedWritingDraft(restoredDraft.native_writing_draft, workspace.session.child_id, workspace.session.session_id))
  const nativeWritingDraft = useRef<TypedWritingDraft | null>(savedNativeWritingDraft)
  const writingDraftRestored = useRef(false)
  const nativeWritingSubmitting = useRef(false)
  const [petBaseline, setPetBaseline] = useState<PetBaseline | null>(null)
  const [diagnosticAnswer, setDiagnosticAnswer] = useState('')
  const [diagnosticFeedback, setDiagnosticFeedback] = useState<string | null>(null)
  const [standaloneReadError, setStandaloneReadError] = useState<string | null>(null)
  const standaloneReadSpeechOwner = useRef<'study' | 'agent'>('study')
  const standaloneReplayReadRef = useRef<(Omit<DiagnosticReplayReadDetail, 'phase' | 'handled'> & { generation: number }) | null>(null)
  const [diagnosticKnowledgeNodeId, setDiagnosticKnowledgeNodeId] = useState<string | null>(null)
  const [diagnosticBusy, setDiagnosticBusy] = useState(false)
  const diagnosticPlayback = useRef(new AbortController())
  useEffect(() => {
    diagnosticPlayback.current = new AbortController()
    return () => diagnosticPlayback.current.abort()
  }, [workspace.session.session_id])
  useEffect(() => { if (privacyStopped) diagnosticPlayback.current.abort() }, [privacyStopped])
  const [captureVisible, setCaptureVisible] = useState(false)
  const restoredOcrTutorReview = parseOcrTutorReview(restoredDraft.tutor_ocr_review)
  const [ocrTutorReview, setOcrTutorReview] = useState<OcrTutorReview | null>(restoredOcrTutorReview)
  const currentOcrReviewRef=useRef(ocrTutorReview)
  currentOcrReviewRef.current=ocrTutorReview
  const [ocrTutorReviewText, setOcrTutorReviewText] = useState(restoredOcrTutorReview?.text ?? '')
  const [tutorRequestRecovery, setTutorRequestRecovery] = useState<TutorRequestRecovery | null>(() => {
    if (!restoredTutorRequestRecovery) return null
    if (restoredTutorRequestRecovery.ocrConfirmationId !== undefined && restoredOcrTutorReview?.confirmationId !== restoredTutorRequestRecovery.ocrConfirmationId) return null
    return restoredTutorRequestRecovery
  })
  const [ocrTutorConfirmationBusy, setOcrTutorConfirmationBusy] = useState(false)
  const autoTutorAfterConfirmRef = useRef<symbol | false>(false)
  const [captureGeneration, setCaptureGeneration] = useState(0)
  const ocrTutorAbortRef = useRef<AbortController | null>(null)
  const [mistakeCaptureArtifact, setMistakeCaptureArtifact] = useState<OcrArtifact | null>(null)
  const pendingMistakeOpenRef = useRef<{ sessionId: string; childId: string; artifactId: string;
    signal?: AbortSignal; finish: (handled: boolean, dialogue?: import('./types').MistakeDialogue) => void } | null>(null)
  const [mistakeComposerOpen, setMistakeComposerOpen] = useState(false)
  const [savedMistakeReviewId, setSavedMistakeReviewId] = useState<string | null>(null)
  const mistakePhotoRequestedRef = useRef(false)
  const [pendingMistakeQueues, setPendingMistakeQueues] = useState<MistakeCaptureQueue[]>([])
  const [completionArtifact, setCompletionArtifact] = useState<OcrArtifact | null>(null)
  const [completionCaptureOpen, setCompletionCaptureOpen] = useState(false)
  const active = workspace.plan_items.find((item) => item.status === 'active')
  const paused = workspace.plan_items.find((item) => item.status === 'paused')
  const next = active ?? paused ?? workspace.plan_items.find((item) => item.status === 'pending')
  const studyMountedRef = useRef(false)
  const studyContext = `${workspace.session.session_id}:${workspace.session.child_id}:${next?.plan_item_id}:${next?.status}:${privacyStopped}`
  const studyContextRef = useRef(studyContext)
  studyContextRef.current = studyContext
  const tutorOperationRef = useRef<symbol | null>(null)
  const ocrConfirmationRef = useRef<symbol | null>(null)
  useEffect(() => {
    studyMountedRef.current = true
    setTutorBusy(false)
    setOcrTutorConfirmationBusy(false)
    return () => {
      studyMountedRef.current = false
      tutorOperationRef.current = null
      ocrConfirmationRef.current = null
      autoTutorAfterConfirmRef.current = false
      ocrTutorAbortRef.current?.abort()
    }
  }, [studyContext])
  const isMistakeReview = next?.source_type === 'mistake_review'
  const boundPetActivityId = next?.source_type === 'pet_daily' ? next.source_ref : null
  const petOriginalReadGeneration = useRef(0)
  const specializedActivityLoadedRef = useRef<string | null>(null)
  const dueReviewLoadedRef = useRef<string | null>(null)
  const dueReviewAnswerBusyRef = useRef(false)
  const reviewSessionMountedRef = useRef(false)
  const pendingReviewSpeechRef = useRef<{ itemId: string; text: string; hasDeferredReply?: boolean;
    originalPrompt?: import('./types').DiagnosticPromptResult;
    listeningPrompt?: NonNullable<import('./types').DiagnosticPromptResult['prompt']> } | null>(null)
  const currentReviewRef = useRef<string | null>(null)
  currentReviewRef.current = !privacyStopped && active?.source_type === 'due_review' ? active.plan_item_id : null
  useEffect(() => {
    reviewSessionMountedRef.current = true
    return () => { currentReviewRef.current = null; reviewSessionMountedRef.current = false; pendingReviewSpeechRef.current = null }
  }, [workspace.session.session_id])

  const flushReviewPrompt = (): void => {
    const pending = pendingReviewSpeechRef.current
    const sid = workspace.session.session_id
    if (!pending || !reviewSessionMountedRef.current || currentReviewRef.current !== pending.itemId || privacyStopPending(sid) || tutorialSpeechOwners.has(sid)) return
    pendingReviewSpeechRef.current = null
    if (pending.listeningPrompt) {
      diagnosticPlayback.current.abort()
      const controller = new AbortController()
      diagnosticPlayback.current = controller
      const current = (): boolean => !controller.signal.aborted && reviewSessionMountedRef.current
        && currentReviewRef.current === pending.itemId && !privacyStopPending(sid)
      const prompt = pending.listeningPrompt
      // Remounting is a read/recovery, not a new request to hear the recording.
      const playbackKey = `review-listen:${sid}:${pending.itemId}:${prompt.prompt_id}:${prompt.item_version}`
      void playPetListening(sid, prompt, playbackKey, current, controller.signal)
        .then(async result => {
          if (!current()) return
          let message = result.response
          if (pending.hasDeferredReply) {
            // Re-read after playback: the original speech may have been revoked
            // while audio was playing. Retention never means answer submission.
            const restored = await localApi.diagnosticPrompt(sid, prompt.prompt_id).catch(() => null)
            if (!current()) return
            message = retainedListeningReply(restored, prompt) ?? message
          }
          if (current()) { setDiagnosticFeedback(message); speakLocally(message, sid, undefined, undefined, { localOnly: true }) }
        })
        .catch(() => { if (current()) setDiagnosticFeedback('原音播放未确认，原复测和进度仍然保留。') })
      return
    }
    const options: import('./companionVoice').CompanionSpeechOptions = { localOnly: true, isCurrent: () => reviewSessionMountedRef.current
      && currentReviewRef.current === pending.itemId && !privacyStopPending(sid) }
    const gated = pending.originalPrompt ? originalAudioPromptPlayback(sid, pending.originalPrompt, options) : options
    speakOrdinaryPrompt(pending.text,workspace.session,gated.beforePlayback?gated:options)
  }
  useEffect(() => {
    const released = (event: Event): void => {
      if ((event as CustomEvent<string>).detail === workspace.session.session_id) flushReviewPrompt()
    }
    window.addEventListener('hsc-tutorial-speech-released', released)
    return () => window.removeEventListener('hsc-tutorial-speech-released', released)
  }, [workspace.session.session_id])

  const loadCurrentPlanReview = async (): Promise<void> => {
    if (!active || active.source_type !== 'due_review' || privacyStopped) return
    const item = active
    const isCurrent = (): boolean => currentReviewRef.current === item.plan_item_id && !privacyStopPending(workspace.session.session_id)
    dueReviewLoadedRef.current = item.plan_item_id
    setDiagnosticBusy(true)
    try {
      const review = await loadBoundReview(item, workspace.session.session_id, workspace.session.child_id)
      if (!isCurrent()) return
      if (review.review_status === 'completed' && review.attempt) {
        const saved = await run(() => localApi.completePlanItem(item.plan_item_id, effectiveActiveMinutes(item), {
          type: 'child_checked', checklist: ['review_completed', 'self_tested'], note: '已恢复该计划绑定复测的真实作答回执',
        }))
        if (saved) { setDiagnosticItem(null); setDiagnosticFeedback('上次的回答已经保存，这次复测任务已恢复收尾。') }
        return
      }
      const pending = await localApi.diagnosticPrompt(workspace.session.session_id)
      if (!isCurrent()) return
      if (pending.status === 'awaiting_task_start') throw new DiagnosticTaskStartRequiredError(pending)
      const retained = savedSchoolReviewReply(review, pending)
      if (retained && pending.prompt) {
        diagnosticPromptBinding.current = null
        clearDiagnosticCaptureForPrompt(workspace.session.session_id, pending.prompt)
        setDiagnosticItem(review); setDiagnosticAnswer(''); setDiagnosticTrack(review.track)
        setDiagnosticFeedback(retained)
        return
      }
      const prepared = ['waiting_answer', 'answer_saved'].includes(pending.status) ? pending
        : await localApi.prepareDiagnosticPrompt(workspace.session.session_id, {
          track: review.track, preferred_item_id: review.diagnostic_item_id, review_task_id: review.review_task_id,
          client_action_id: `plan-review-prompt:${workspace.session.session_id}:${item.plan_item_id}`,
        })
      if (!isCurrent()) return
      if (prepared.status === 'awaiting_task_start') throw new DiagnosticTaskStartRequiredError(prepared)
      if (prepared.prompt?.review_task_id !== review.review_task_id || prepared.prompt.diagnostic_item_id !== review.diagnostic_item_id
        || prepared.prompt.child_id !== workspace.session.child_id || prepared.prompt.origin_session_id !== workspace.session.session_id) {
        throw new Error('当前待答题与这项复测不同，请先核对或取消之前的题目。')
      }
      if (prepared.status !== 'waiting_answer') throw new Error(prepared.status === 'answer_saved' ? '上次回答已保存，评分还需恢复。' : '这道题已取消或失效，未重新开始作答。')
      setVoiceQuestionCapture(workspace.session.session_id, workspace.session.child_id,
        { kind: 'diagnostic', binding: { prompt_id: prepared.prompt.prompt_id, item_version: prepared.prompt.item_version } })
      diagnosticPromptBinding.current = prepared.prompt
      setDiagnosticItem(review)
      setDiagnosticAnswer('')
      setDiagnosticTrack(review.track)
      setDiagnosticFeedback(null)
      pendingReviewSpeechRef.current = { itemId: item.plan_item_id,
        originalPrompt: prepared,
        hasDeferredReply: Boolean(prepared.deferred_answer),
        listeningPrompt: isPetListening(prepared.prompt) ? prepared.prompt : undefined,
        text: `我们复习“${review.title}”。先自己试一试，不会也可以直接告诉我。${isPetListening(prepared.prompt) ? review.prompt : [...new Set([review.stimulus_text, review.prompt].filter(Boolean))].join('。')}` }
      flushReviewPrompt()
    } catch (cause) {
      if (!isCurrent()) return
      dueReviewLoadedRef.current = null
      if (cause instanceof DiagnosticTaskStartRequiredError) {
        showTaskStartRecovery(cause.result)
        speakLocally(cause.message, workspace.session.session_id, undefined, undefined, { localOnly: true })
        return
      }
      const message = cause instanceof Error ? cause.message : '这道复测题暂时没有读到。'
      setDiagnosticFeedback(message)
      speakLocally(`${message}说“重读题目”可以重试，未完成记录会保留。`, workspace.session.session_id, undefined, undefined, { localOnly: true })
    } finally { if (isCurrent()) setDiagnosticBusy(false) }
  }

  useEffect(() => {
    if (!currentReviewRef.current) {
      pendingReviewSpeechRef.current = null
      if (dueReviewLoadedRef.current) { dueReviewLoadedRef.current = null; setDiagnosticItem(null); setDiagnosticBusy(false) }
      return
    }
    if (dueReviewLoadedRef.current !== currentReviewRef.current) void loadCurrentPlanReview()
  }, [active?.plan_item_id, active?.source_type, privacyStopped])

  useEffect(() => {
    if (next?.source_type !== 'due_review' || !next.source_ref?.startsWith('school-review:')
      || next.status !== 'paused' || privacyStopped) return
    let cancelled = false
    const context = studyContextRef.current
    const current = (): boolean => !cancelled && studyMountedRef.current && studyContextRef.current === context
      && !privacyStopPending(workspace.session.session_id)
    // A backend restart pauses the task. Restore its original without starting
    // a timer, arming answer capture, or replaying the question automatically.
    void loadBoundReview(next, workspace.session.session_id, workspace.session.child_id).then(async review => {
      if (!current() || !isSchoolOpenReview(review)) return
      diagnosticPromptBinding.current = null
      setDiagnosticItem(null)
      const original = await localApi.diagnosticPrompt(workspace.session.session_id)
      if (!current()) return
      const prompt = original.prompt
      if (!prompt || prompt.child_id !== review.child_id || prompt.origin_session_id !== review.session_id
        || prompt.review_task_id !== review.review_task_id || prompt.diagnostic_item_id !== review.diagnostic_item_id
        || prompt.item.diagnostic_item_id !== review.diagnostic_item_id) {
        throw new Error('暂停复习的原题暂未核对成功，请说“继续原任务”；不会另出新题。')
      }
      const retained = savedSchoolReviewReply(review, original)
      if (review.review_status === 'completed' && (original.status !== 'answered'
        || original.attempt?.attempt_id !== review.attempt?.attempt_id
        || original.attempt?.evidence_id !== review.attempt?.evidence_id)) {
        throw new Error('暂停复习的评分与原回答尚未核对一致，原记录保留，未确认任务完成。')
      }
      if (!retained && original.status !== 'waiting_answer' && review.review_status !== 'completed') {
        throw new Error('暂停复习的原回答状态需要核对，请说“继续原任务”；不会重复作答。')
      }
      clearDiagnosticCaptureForPrompt(workspace.session.session_id, prompt)
      // A verified paused original can be shown without arming answer capture.
      restoredQuestionViewport.current = prompt
      setDiagnosticItem(review); setDiagnosticAnswer(''); setDiagnosticTrack(review.track)
      setDiagnosticFeedback(retained ?? (review.review_status === 'completed'
        ? '这次复习的原稿和评分已保存。任务仍暂停，可以说“继续原任务”。'
        : '原来的复习题已保留，任务仍暂停。准备好后说“继续原任务”。'))
    }).catch(cause => {
      if (!current()) return
      diagnosticPromptBinding.current = null
      setDiagnosticItem(null)
      setDiagnosticFeedback(cause instanceof Error ? cause.message : '暂停复习暂未读回，请说“继续原任务”；原记录仍保留。')
    })
    return () => { cancelled = true }
  }, [workspace.session.session_id, workspace.session.child_id, next?.plan_item_id, next?.status, next?.source_type, privacyStopped])

  const presentTutorTurn = useCallback(async (turn: TutorTurn, speechPolicy?: AgentReplySpeechPolicy, signal?: AbortSignal): Promise<void> => {
    if (signal?.aborted || privacyStopPending(workspace.session.session_id)) return
    setTutorTurn(turn)
    if (!await dispatchTutorPresentation(turn, speechPolicy, signal)) throw new VoiceActionUnavailableError('讲解进度尚未确认保存，未启动播放。')
  }, [workspace.session.session_id])

  useEffect(() => {
    const openMistakeCapture = (event: Event): void => {
      const detail = (event as CustomEvent<{ action?: MistakeCaptureVoiceAction; sessionId?: string; childId?: string; signal?: AbortSignal;
        report?: (dialogue: import('./types').MistakeDialogue) => void; resolve?: (handled: boolean) => void }>).detail
      if (event.defaultPrevented || detail?.action !== 'open' || detail.sessionId !== workspace.session.session_id
        || detail.childId !== workspace.session.child_id || detail.signal?.aborted) return
      const artifact = mistakeCaptureArtifact ?? pendingMistakeQueues[0] ?? null
      if (!artifact || pendingMistakeOpenRef.current) return
      event.preventDefault()
      let settled = false
      const finish = (handled: boolean, dialogue?: import('./types').MistakeDialogue): void => {
        if (settled) return
        settled = true
        detail.signal?.removeEventListener('abort', abort)
        if (pendingMistakeOpenRef.current?.finish === finish) pendingMistakeOpenRef.current = null
        if (handled && dialogue) detail.report?.(dialogue)
        detail.resolve?.(handled)
      }
      const abort = (): void => finish(false)
      pendingMistakeOpenRef.current = { sessionId: workspace.session.session_id, childId: workspace.session.child_id,
        artifactId: artifact.artifact_id, signal: detail.signal, finish }
      detail.signal?.addEventListener('abort', abort, { once: true })
      setMistakeCaptureArtifact(artifact)
      setMistakeComposerOpen(true)
    }
    window.addEventListener('hsc-mistake-capture-voice-action', openMistakeCapture)
    return () => window.removeEventListener('hsc-mistake-capture-voice-action', openMistakeCapture)
  }, [mistakeCaptureArtifact, pendingMistakeQueues, workspace.session.session_id, workspace.session.child_id])

  useEffect(() => () => { pendingMistakeOpenRef.current?.finish(false) }, [workspace.session.session_id, workspace.session.child_id])

  const completionDraftMatches = draftString(restoredDraft, 'completion_plan_item_id') === next?.plan_item_id
  const restoredEvidence = completionDraftMatches ? draftString(restoredDraft, 'completion_evidence', 'child_checked') : 'child_checked'
  const [completionReviewOpen, setCompletionReviewOpen] = useState(completionDraftMatches && Boolean(restoredDraft.completion_review_open))
  const [completionEvidence, setCompletionEvidence] = useState<'child_checked' | 'child_confirmed' | 'needs_correction'>(['child_checked', 'child_confirmed', 'needs_correction'].includes(restoredEvidence) ? restoredEvidence as 'child_checked' | 'child_confirmed' | 'needs_correction' : 'child_checked')
  const [completionNote, setCompletionNote] = useState(completionDraftMatches ? draftString(restoredDraft, 'completion_note') : '')
  const [completionChecklist, setCompletionChecklist] = useState<string[]>(completionDraftMatches && Array.isArray(restoredDraft.completion_checklist) ? restoredDraft.completion_checklist.filter((item): item is string => typeof item === 'string') : [])
  const completedCount = workspace.plan_items.filter((item) => item.status === 'completed' && item.source_type !== 'planned_break').length
  const deferredCount = workspace.plan_items.filter((item) => item.status === 'rescheduled').length
  const allSettled = workspace.plan_items.every((item) => ['completed', 'rescheduled', 'skipped'].includes(item.status))
  const studyDraftPayload = {
    question,
    tutor_ocr_review: ocrTutorReview,
    tutor_ocr_review_text: ocrTutorReviewText,
    tutor_request_recovery: tutorRequestRecovery,
    tutor_feedback_text: tutorFeedbackText,
    diagnostic_track: diagnosticTrack,
    native_writing_draft: savedNativeWritingDraft,
    completion_plan_item_id: next?.plan_item_id ?? null,
    completion_review_open: completionReviewOpen,
    completion_evidence: completionEvidence,
    completion_note: completionNote,
    completion_checklist: completionChecklist
  }
  const draft = useAutosavedDraft(workspace.session.session_id, 'study', studyDraftPayload,
    Boolean(savedNativeWritingDraft || question.trim() || ocrTutorReview || tutorFeedbackText.trim() || completionReviewOpen || completionNote.trim() || completionChecklist.length), Boolean(workspace.drafts.study))
  const documentDraftRef = useRef({ payload: studyDraftPayload, saveNow: draft.saveNow })
  documentDraftRef.current = { payload: studyDraftPayload, saveNow: draft.saveNow }
  useEffect(() => {
    let pending: { finish: (saved: boolean) => void } | null = null
    const receive = (event: Event): void => {
      const detail = (event as CustomEvent<{ sessionId?: string; childId?: string; revision?: DocumentPageRevision;
        signal?: AbortSignal; resolve?: (saved: boolean) => void }>).detail
      const item = detail?.revision
      if (event.defaultPrevented || detail?.signal?.aborted || detail?.sessionId !== workspace.session.session_id || !item
        || detail.childId !== undefined && detail.childId !== workspace.session.child_id
        || !studyMountedRef.current || studyContextRef.current !== studyContext
        || privacyStopPending(workspace.session.session_id)) return
      event.preventDefault()
      pending?.finish(false)
      let settled = false
      const owner = { finish: (saved: boolean): void => {
        if (settled) return
        settled = true
        detail.signal?.removeEventListener('abort', abort)
        if (pending === owner) pending = null
        detail.resolve?.(saved)
      } }
      const abort = (): void => owner.finish(false)
      pending = owner
      detail.signal?.addEventListener('abort', abort, { once: true })
      const review: OcrTutorReview = { artifactId: item.artifact_id, text: item.extracted_text,
        status: item.review_required ? 'uncertain' : 'needs_confirmation', createdAt: item.created_at }
      const savedDraft = { ...documentDraftRef.current.payload, question: '', tutor_ocr_review: review, tutor_ocr_review_text: review.text }
      // Keep the pending draft visible for recovery; only its durable receipt
      // authorizes readback. A late save never changes the current page.
      setOcrTutorReview(review)
      setOcrTutorReviewText(review.text)
      setQuestion('')
      setMistakeCaptureArtifact(documentRevisionArtifact(item))
      setMistakeComposerOpen(Number.isInteger(item.marked_question_count) && (item.marked_question_count ?? 0) > 0)
      void documentDraftRef.current.saveNow(savedDraft).then(() => {
        if (settled || pending !== owner || !studyMountedRef.current || studyContextRef.current !== studyContext
          || detail.signal?.aborted || privacyStopPending(workspace.session.session_id)) { owner.finish(false); return }
        owner.finish(true)
      }).catch(() => owner.finish(false))
    }
    window.addEventListener(DOCUMENT_REVIEW_REVISION_EVENT, receive)
    return () => { window.removeEventListener(DOCUMENT_REVIEW_REVISION_EVENT, receive); pending?.finish(false) }
  }, [studyContext, workspace.session.child_id, workspace.session.session_id])

  useEffect(() => {
    const saved = nativeWritingDraft.current
    const shown = diagnosticPromptBinding.current
    if (writingDraftRestored.current || !saved || !shown || !isWrittenDiagnostic(diagnosticItem)
      || privacyStopped || !sameWritingScope(saved.scope, writingScope(shown))) return
    let cancelled = false
    const context = studyContext
    void localApi.diagnosticPrompt(workspace.session.session_id).then(result => {
      const current = diagnosticPromptBinding.current
      if (cancelled || !studyMountedRef.current || studyContextRef.current !== context
        || nativeWritingDraft.current !== saved || privacyStopPending(workspace.session.session_id)
        || !current || !sameWritingScope(saved.scope, writingScope(current))
        || (result.status !== 'waiting_answer' && !isSavedNativeWritingPrompt(result,saved)) || !result.prompt || !isWrittenDiagnostic(result.prompt.item)
        || !sameWritingScope(saved.scope, writingScope(result.prompt))) return
      writingDraftRestored.current = true
      setDiagnosticAnswer(saved.text)
    }).catch(() => undefined)
    return () => { cancelled = true }
  }, [diagnosticItem, studyContext, privacyStopped, workspace.session.session_id])

  useEffect(() => {
    if (tutorRequestRecovery) {
      setTutorError('上次讲解的结果还没有收到。题干已保留，说“重试本题”会核对原请求，不会自动创建新请求。')
    }
  }, [])

  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])

  useEffect(() => {
    if (!active) return
    const checkpoint = (): void => { void localApi.checkpointPlanItem(active.plan_item_id).catch(() => undefined) }
    const timer = window.setInterval(checkpoint, 15_000)
    return () => {
      window.clearInterval(timer)
      checkpoint()
    }
  }, [active?.plan_item_id])

  useEffect(() => {
    const showCapture = (event: Event): void => {
      const request = (event as CustomEvent<Partial<CaptureRequest>>).detail
      if (request?.sessionId && request.sessionId !== workspace.session.session_id) return
      if (privacyStopPending(workspace.session.session_id)) return
      mistakePhotoRequestedRef.current = Boolean(request?.forMistake)
      tutorOperationRef.current = null
      ocrConfirmationRef.current = null
      autoTutorAfterConfirmRef.current = false
      setTutorBusy(false)
      setCaptureCommandId(request?.commandId)
      ocrTutorAbortRef.current?.abort()
      cancelLocalSpeech()
      setTutorTurn(null)
      setTutorFeedbackText('')
      setTutorFeedbackMessage(null)
      setOcrTutorReview(null)
      setOcrTutorReviewText('')
      setTutorRequestRecovery(null)
      setTutorError(null)
      setQuestion('')
      setMistakeCaptureArtifact(null)
      setMistakeComposerOpen(false)
      setCaptureVisible(true)
      setCaptureGeneration((value) => value + 1)
      request?.accept?.()
    }
    window.addEventListener('hsc-request-question-capture', showCapture)
    return () => window.removeEventListener('hsc-request-question-capture', showCapture)
  }, [workspace.session.session_id])

  useEffect(() => {
    const cancelOcrTutor = (): void => {
      tutorOperationRef.current = null
      ocrConfirmationRef.current = null
      autoTutorAfterConfirmRef.current = false
      ocrTutorAbortRef.current?.abort()
      cancelLocalSpeech()
      setCaptureVisible(false)
      setCompletionCaptureOpen(false)
    }
    window.addEventListener('hsc-privacy-pause', cancelOcrTutor)
    return () => window.removeEventListener('hsc-privacy-pause', cancelOcrTutor)
  }, [])

  useEffect(() => {
    let cancelled = false
    setMistakeCaptureArtifact(null); setMistakeComposerOpen(false); setPendingMistakeQueues([]); setSavedMistakeReviewId(null)
    const refreshMistakeQueues = (): void => {
      if (cancelled) return
      void localApi.mistakeCaptureQueues(workspace.session.child_id).then(queues => {
        if (cancelled) return
        setPendingMistakeQueues(queues)
        if (queues[0]) { setMistakeCaptureArtifact(queues[0]); setMistakeComposerOpen(true) }
      }).catch(() => undefined)
    }
    void localApi.mistakeDialogue(workspace.session.session_id).then(({ dialogue }) => {
      if (cancelled) return
      if (dialogue?.session_id === workspace.session.session_id && dialogue.child_id === workspace.session.child_id
        && dialogue.state.artifact_id && !['closed', 'paused'].includes(dialogue.phase)) {
        setMistakeCaptureArtifact({ artifact_id: dialogue.state.artifact_id,
          extracted_text: dialogue.state.question_text ?? '', confidence: 0,
          local_path: null, status: 'completed', details: { lines: [], engine: 'persisted_mistake_dialogue' } })
        setMistakeComposerOpen(true)
      } else if (dialogue?.session_id === workspace.session.session_id && dialogue.child_id === workspace.session.child_id
        && dialogue.mistake_id && !dialogue.state.artifact_id && dialogue.dialogue_binding
        && !['closed', 'paused'].includes(dialogue.phase)) {
        setSavedMistakeReviewId(dialogue.mistake_id)
      } else refreshMistakeQueues()
    }).catch(() => { if (!cancelled) refreshMistakeQueues() })
    return () => { cancelled = true }
  }, [workspace.session.session_id, workspace.session.child_id])

  const elapsedMilliseconds = next ? effectiveActiveMilliseconds(next, now) : 0
  const elapsedMinutes = Math.max(1, Math.ceil(elapsedMilliseconds / 60000))
  const requiredCompletionChecks = next ? completionChecksByTaskType[next.task_type] ?? [] : []
  const checklistComplete = requiredCompletionChecks.every((item) => completionChecklist.includes(item.id))

  const completionResetReadyRef = useRef(false)
  useEffect(() => {
    if (!completionResetReadyRef.current) {
      completionResetReadyRef.current = true
      return
    }
    setCompletionReviewOpen(false)
    setCompletionEvidence('child_checked')
    setCompletionNote('')
    setCompletionChecklist([])
    setCompletionArtifact(null)
    setCompletionCaptureOpen(false)
  }, [next?.plan_item_id])

  const completeTask = async (): Promise<void> => {
    if (!next || !['active', 'paused'].includes(next.status)) return
    const completed = await run(() => localApi.completePlanItem(next.plan_item_id, elapsedMinutes, {
      type: completionEvidence,
      note: completionNote.trim(),
      checklist: completionEvidence === 'child_checked' ? completionChecklist : [],
      artifactId: completionArtifact?.artifact_id
    }))
    if (completed) {
      setCompletionReviewOpen(false)
      setCompletionEvidence('child_checked')
      setCompletionNote('')
      setCompletionChecklist([])
      setCompletionArtifact(null)
      setCompletionCaptureOpen(false)
    }
  }

  const askTutor = async (
    hintLevel = 1,
    previous?: TutorTurn,
    confirmUnknownOutcomeRetry = false,
    recoveryOverride?: TutorRequestRecovery,
    signal?: AbortSignal,
  ): Promise<void> => {
    // A confirmed unknown-outcome retry must retain every part of the original
    // request identity (including a parent turn and OCR confirmation).  Do not
    // reconstruct it from the current screen, which may have changed while the
    // child was deciding whether to authorize a potentially billable retry.
    const tutorQuestion = previous?.question ?? recoveryOverride?.question ?? question.trim()
    const parentTurnId = previous?.turn_id ?? recoveryOverride?.parentTurnId
    if (tutorQuestion.length < 2 || !next || !studyMountedRef.current || signal?.aborted || privacyStopPending(workspace.session.session_id)) return
    if (tutorOperationRef.current) return
    if (!previous && ocrTutorReview?.status !== undefined && (ocrTutorReview.status !== 'confirmed' || !ocrTutorReview.confirmationId)) {
      setTutorError(ocrTutorReviewMessage(ocrTutorReview))
      setTutorSafetyBlocked(false)
      speakOrdinaryPrompt(ocrTutorReviewMessage(ocrTutorReview),workspace.session,{localOnly:true,isCurrent:()=>studyMountedRef.current&&currentOcrReviewRef.current===ocrTutorReview})
      return
    }
    const operation = Symbol('study-tutor-request')
    tutorOperationRef.current = operation
    const context = studyContextRef.current
    const isCurrent = (): boolean => studyMountedRef.current && tutorOperationRef.current === operation
      && studyContextRef.current === context && !signal?.aborted && !privacyStopPending(workspace.session.session_id)
    setTutorBusy(true)
    setTutorError(null)
    setTutorSafetyBlocked(false)
    setTutorUnknownOutcome(false)
    try {
      const ocrConfirmationId = !previous
        ? recoveryOverride?.ocrConfirmationId ?? (ocrTutorReview?.status === 'confirmed' ? ocrTutorReview.confirmationId : undefined)
        : undefined
      const reusable = tutorRequestRecovery &&
        tutorRequestRecovery.subject === next.subject && tutorRequestRecovery.question === tutorQuestion &&
        tutorRequestRecovery.hintLevel === hintLevel && tutorRequestRecovery.parentTurnId === parentTurnId &&
        tutorRequestRecovery.ocrConfirmationId === ocrConfirmationId
        ? tutorRequestRecovery : null
      const recovery: TutorRequestRecovery = reusable ?? {
        version: 1, sessionId: workspace.session.session_id, childId: workspace.session.child_id,
        actionId: crypto.randomUUID(), subject: next.subject, question: tutorQuestion, hintLevel,
        ...(parentTurnId ? { parentTurnId } : {}),
        ...(ocrConfirmationId ? { ocrConfirmationId } : {}), createdAt: new Date().toISOString()
      }
      if (!reusable) {
        // Persist the action identity before sending: a lost HTTP response must
        // replay this exact request rather than create another model turn.
        await draft.saveNow({
          question: tutorQuestion, tutor_ocr_review: ocrTutorReview,
          tutor_ocr_review_text: ocrTutorReviewText, tutor_request_recovery: recovery,
          tutor_feedback_text: tutorFeedbackText, diagnostic_track: diagnosticTrack,
          completion_plan_item_id: next?.plan_item_id ?? null,
          completion_review_open: completionReviewOpen, completion_evidence: completionEvidence,
          completion_note: completionNote, completion_checklist: completionChecklist
        })
        if (!isCurrent()) return
        setTutorRequestRecovery(recovery)
      }
      const ocrAbort = ocrConfirmationId ? new AbortController() : null
      ocrTutorAbortRef.current = ocrAbort
      const turn = ocrConfirmationId
        ? await askTutorWithOcrEvidence(workspace.session.session_id, next.subject, ocrConfirmationId, recovery.actionId, hintLevel, parentTurnId, ocrAbort?.signal, confirmUnknownOutcomeRetry)
        : await localApi.askTutor(workspace.session.session_id, next.subject, tutorQuestion, hintLevel, parentTurnId, recovery.actionId, confirmUnknownOutcomeRetry)
      if (!isCurrent()) return
      await presentTutorTurn(turn, undefined, signal)
      if (!isCurrent()) return
      setQuestion('')
      setOcrTutorReview(null)
      setOcrTutorReviewText('')
      setTutorRequestRecovery(null)
      // Do not leave an already-completed action in the 24-hour recovery
      // draft.  Keep the rest of the study draft intact.
      await draft.saveNow({
        question: '', tutor_ocr_review: null, tutor_ocr_review_text: '', tutor_request_recovery: null,
        tutor_feedback_text: tutorFeedbackText, diagnostic_track: diagnosticTrack,
        completion_plan_item_id: next?.plan_item_id ?? null,
        completion_review_open: completionReviewOpen, completion_evidence: completionEvidence,
        completion_note: completionNote, completion_checklist: completionChecklist
      })
    } catch (cause) {
      if (!isCurrent()) return
      const cancelled = cause instanceof DOMException && cause.name === 'AbortError'
      const message = cancelled ? '已取消本次讲解，题干仍保留。你可以继续核对或稍后再问。' : cause instanceof ApiError ? cause.message : '这个问题暂时没有回答成功，请稍后重试。'
      const sourceBlocked = cause instanceof ApiError ? tutorSourceBlock(cause.code) : undefined
      const safetyBlocked = Boolean(sourceBlocked)
      const unknownOutcome = cause instanceof ApiError && cause.code === 'TUTOR_RESULT_UNCONFIRMED'
      setTutorError(message)
      setTutorSafetyBlocked(safetyBlocked)
      setTutorUnknownOutcome(unknownOutcome)
      const guidance = sourceBlocked ? tutorSourceBlockMessage(sourceBlocked)
        : unknownOutcome ? '上次请求的结果还没有确认。说“确认再次请求”授权核对，或说“保留题干”暂不重试。'
          : '这次讲解没有完成，题干仍然保留。可以说“重试本题”，或“继续当前任务”。'
      speakLocally(guidance, workspace.session.session_id, undefined, undefined, { localOnly: true })
    } finally {
      if (tutorOperationRef.current === operation) {
        tutorOperationRef.current = null
        ocrTutorAbortRef.current = null
        if (studyMountedRef.current) setTutorBusy(false)
      }
    }
  }

  const cancelUnknownTutorRetry = async (signal?: AbortSignal): Promise<void> => {
    const context = studyContextRef.current
    const isCurrent = (): boolean => studyMountedRef.current && studyContextRef.current === context
      && !signal?.aborted && !privacyStopPending(workspace.session.session_id)
    if (!isCurrent()) return
    try {
      await draft.saveNow({
      question, tutor_ocr_review: ocrTutorReview, tutor_ocr_review_text: ocrTutorReviewText, tutor_request_recovery: null,
      tutor_feedback_text: tutorFeedbackText, diagnostic_track: diagnosticTrack,
      completion_plan_item_id: next?.plan_item_id ?? null,
      completion_review_open: completionReviewOpen, completion_evidence: completionEvidence,
      completion_note: completionNote, completion_checklist: completionChecklist
      })
    } catch {
      if (isCurrent()) {
        const message = '这一步没有保存好，原记录仍保留。请再说一次“保留题干”。'
        setTutorError(message)
        speakLocally(message, workspace.session.session_id, undefined, undefined, { localOnly: true })
      }
      return
    }
    if (!isCurrent()) return
    setTutorRequestRecovery(null)
    setTutorUnknownOutcome(false)
    const message = '已保留题干，不会自动重放原请求。想问新的问题，可以直接说出来。'
    setTutorError(message)
    speakLocally(message, workspace.session.session_id, undefined, undefined, { localOnly: true })
  }

  useEffect(() => {
    const recover = (event: Event): void => {
      const detail = (event as CustomEvent<{ sessionId?: string; childId?: string; command: TutorRecoveryCommand;
        signal?: AbortSignal; resolve: (handled: boolean) => void }>).detail
      if (event.defaultPrevented || detail?.sessionId !== workspace.session.session_id || detail.childId !== workspace.session.child_id
        || detail.signal?.aborted || !studyMountedRef.current || privacyStopPending(workspace.session.session_id)
        || !['continue', 'keep', 'retry', 'confirm'].includes(detail.command)) return
      if (!tutorRequestRecovery && !tutorError && !tutorOperationRef.current) return
      event.preventDefault()
      const context = studyContextRef.current
      const isCurrent = (): boolean => studyMountedRef.current && studyContextRef.current === context
        && !detail.signal?.aborted && !privacyStopPending(workspace.session.session_id)
      const execute = async (): Promise<void> => {
        if (!isCurrent()) return
        if (detail.command === 'continue') {
          tutorOperationRef.current = null
          ocrTutorAbortRef.current?.abort()
          setTutorBusy(false)
          setTutorError(null)
          speakLocally('好，先继续当前任务。题干和未确认的请求仍保留，不会自动重试。', workspace.session.session_id, undefined, undefined, { localOnly: true })
        } else if (detail.command === 'keep') {
          await cancelUnknownTutorRetry(detail.signal)
        } else if (tutorSafetyBlocked) {
          speakLocally('这段内容只能在本地处理，暂时不能重试云端讲解。可以说“继续当前任务”。', workspace.session.session_id, undefined, undefined, { localOnly: true })
        } else if (tutorUnknownOutcome && detail.command !== 'confirm') {
          speakLocally('原请求结果还没有确认。要重试请说“确认再次请求”；也可以说“保留题干”。', workspace.session.session_id, undefined, undefined, { localOnly: true })
        } else if (tutorRequestRecovery && next?.subject !== tutorRequestRecovery.subject) {
          speakLocally('原讲解属于之前的学科，当前没有重新提交。可以说“保留题干”，再说现在的问题。', workspace.session.session_id, undefined, undefined, { localOnly: true })
        } else {
          await askTutor(tutorRequestRecovery?.hintLevel ?? 1, undefined, detail.command === 'confirm', tutorRequestRecovery ?? undefined, detail.signal)
        }
      }
      void execute().catch(() => {
        if (isCurrent()) speakLocally('这一步没有保存好，原记录仍保留。请再说一次刚才的恢复指令。', workspace.session.session_id, undefined, undefined, { localOnly: true })
      }).finally(() => detail.resolve(true))
    }
    window.addEventListener('hsc-study-tutor-recovery', recover)
    return () => window.removeEventListener('hsc-study-tutor-recovery', recover)
  }, [tutorRequestRecovery, tutorError, tutorUnknownOutcome, tutorSafetyBlocked, question, ocrTutorReview, next?.plan_item_id, next?.subject, workspace.session.session_id, workspace.session.child_id])

  const confirmOcrTutorReview = async (correctedText?: string, goal?: DocumentGoalConfirmation, signal?: AbortSignal): Promise<boolean> => {
    if (!ocrTutorReview || ocrTutorReview.status === 'unreadable' || (ocrTutorReview.status === 'confirmed' && !goal)) return false
    if (ocrConfirmationRef.current || !studyMountedRef.current || signal?.aborted || privacyStopPending(workspace.session.session_id)) return false
    const text = (correctedText ?? ocrTutorReviewText).replace(/\s+/g, ' ').trim()
    if (text.length < 2) return false
    const operation = Symbol('ocr-confirmation')
    ocrConfirmationRef.current = operation
    const context = studyContextRef.current
    const isCurrent = (): boolean => studyMountedRef.current && ocrConfirmationRef.current === operation
      && studyContextRef.current === context && !signal?.aborted && !privacyStopPending(workspace.session.session_id)
    setOcrTutorConfirmationBusy(true)
    setTutorError(null)
    try {
      const confirmed = !goal && ocrTutorReview.status === 'confirmed' && text === ocrTutorReview.text
        ? ocrTutorReview : await confirmOcrTutorEvidence(workspace.session.session_id, ocrTutorReview, text)
      if (!isCurrent()) return false
      setOcrTutorReview(confirmed)
      setOcrTutorReviewText(confirmed.text)
      setQuestion(confirmed.text)
      if (goal) {
        await prepareOcrDocumentGoal(workspace.session.session_id, confirmed, goal)
        if (!isCurrent()) return false
        setCaptureVisible(false)
      }
      return true
    } catch (cause) {
      if (!isCurrent()) return false
      const message = '题干确认还没有保存成功。可以说“确认题干”重试，未核对文字不会作为题目提交。'
      setTutorError(message)
      speakLocally(message, workspace.session.session_id, undefined, undefined, { localOnly: true })
      return false
    } finally {
      if (ocrConfirmationRef.current === operation) {
        ocrConfirmationRef.current = null
        if (studyMountedRef.current) setOcrTutorConfirmationBusy(false)
      }
    }
  }

  useEffect(() => {
    const claim = (event: Event): StudyCaptureVoiceDetail | null => {
      const detail = (event as CustomEvent<StudyCaptureVoiceDetail>).detail
      if (event.defaultPrevented || detail?.sessionId !== workspace.session.session_id
          || detail.childId !== workspace.session.child_id || detail.signal?.aborted
          || !studyMountedRef.current || privacyStopPending(workspace.session.session_id)) return null
      event.preventDefault()
      return detail
    }
    const confirm = (event: Event): void => {
      const detail = claim(event)
      if (!detail) return
      if (ocrConfirmationRef.current || !ocrTutorReview || ocrTutorReview.status === 'unreadable' || (ocrTutorReview.status === 'confirmed' && !detail.goal)) {
        detail.resolve(false)
        return
      }
      const speechOwner = detail.goal ? false : Symbol('confirmed-page-explanation')
      autoTutorAfterConfirmRef.current = speechOwner
      void confirmOcrTutorReview(detail.correctedText, detail.goal, detail.signal).then((handled) => {
        if (!handled && autoTutorAfterConfirmRef.current === speechOwner) autoTutorAfterConfirmRef.current = false
        detail.resolve(handled)
      })
    }
    const retry = (event: Event): void => {
      const detail = claim(event)
      if (!detail) return
      ocrConfirmationRef.current = null
      autoTutorAfterConfirmRef.current = false
      setOcrTutorConfirmationBusy(false)
      setOcrTutorReview(null); setOcrTutorReviewText(''); setQuestion('')
      setCaptureVisible(true); setCaptureGeneration((value) => value + 1)
      detail.resolve(true)
    }
    const cancel = (event: Event): void => {
      const detail = claim(event)
      if (!detail) return
      ocrConfirmationRef.current = null
      autoTutorAfterConfirmRef.current = false
      setOcrTutorConfirmationBusy(false)
      setOcrTutorReview(null); setOcrTutorReviewText(''); setQuestion(''); setCaptureVisible(false)
      detail.resolve(true)
    }
    window.addEventListener('hsc-confirm-question-capture', confirm)
    window.addEventListener('hsc-retry-question-capture', retry)
    window.addEventListener('hsc-cancel-question-capture', cancel)
    return () => {
      window.removeEventListener('hsc-confirm-question-capture', confirm)
      window.removeEventListener('hsc-retry-question-capture', retry)
      window.removeEventListener('hsc-cancel-question-capture', cancel)
    }
  }, [ocrTutorReview, ocrTutorReviewText, workspace.session.session_id, workspace.session.child_id])

  useEffect(() => {
    if (ocrTutorReview?.status !== 'confirmed' || !autoTutorAfterConfirmRef.current) return
    autoTutorAfterConfirmRef.current = false
    void askTutor()
  }, [ocrTutorReview?.status, ocrTutorReview?.confirmationId])

  function showTaskStartRecovery(result: import('./types').DiagnosticPromptResult): void {
    const prompt = result.prompt
    if (!prompt || prompt.child_id !== workspace.session.child_id || prompt.origin_session_id !== workspace.session.session_id) return
    clearDiagnosticCaptureForPrompt(workspace.session.session_id, prompt)
    const shown = diagnosticPromptBinding.current
    const exact = shown?.prompt_id === prompt.prompt_id && shown.item_version === prompt.item_version
    setDiagnosticItem(current => exact || (!shown && current?.diagnostic_item_id === prompt.diagnostic_item_id) ? null : current)
    if (exact) { diagnosticPromptBinding.current = null; setDiagnosticAnswer('') }
    setDiagnosticFeedback(DIAGNOSTIC_TASK_START_MESSAGE)
  }

  const presentDiagnosticItem = async (item: DiagnosticItem): Promise<void> => {
    if (!isPetListening({ item })) {
      speakOrdinaryPrompt(item.stimulus_text ?? item.prompt,workspace.session)
      return
    }
    if (privacyStopped) return
    diagnosticPlayback.current.abort()
    const controller = new AbortController()
    diagnosticPlayback.current = controller
    const isCurrent = (): boolean => !controller.signal.aborted && diagnosticPlayback.current === controller
    const prepared = await localApi.prepareDiagnosticPrompt(workspace.session.session_id, {
      track: item.track, preferred_item_id: item.diagnostic_item_id, review_task_id: item.review_task_id,
      client_action_id: `listening-source:${crypto.randomUUID()}`,
    })
    if (!isCurrent()) return
    if (prepared.status === 'awaiting_task_start') {
      showTaskStartRecovery(prepared)
      speakLocally(DIAGNOSTIC_TASK_START_MESSAGE, workspace.session.session_id, undefined, undefined, { localOnly: true })
      return
    }
    if (prepared.status !== 'waiting_answer' || !prepared.prompt || prepared.prompt.diagnostic_item_id !== item.diagnostic_item_id) {
      setDiagnosticFeedback('原题状态已变化，未播放其他题目的材料。')
      return
    }
    diagnosticPromptBinding.current = prepared.prompt
    const played = await playPetListening(workspace.session.session_id, prepared.prompt,
      `listening-play:${crypto.randomUUID()}`, isCurrent, controller.signal)
    if (isCurrent()) {
      setDiagnosticFeedback(played.response)
      speakLocally(played.response, workspace.session.session_id)
    }
  }

  const loadDiagnostic = async (track = diagnosticTrack, preferredItemId?: string, subject?: string): Promise<void> => {
    setDiagnosticBusy(true)
    setDiagnosticFeedback(null)
    setDiagnosticKnowledgeNodeId(null)
    try {
      const selection = subject
        ? await localApi.nextDiagnostic(workspace.session.session_id, track, preferredItemId, subject)
        : await localApi.nextDiagnostic(workspace.session.session_id, track, preferredItemId)
      setDiagnosticItem(selection.item)
      setPetBaseline(selection.pet_baseline)
      setDiagnosticAnswer('')
      if (selection.item) await presentDiagnosticItem(selection.item)
      else setDiagnosticFeedback('当前暂时没有合适的挑战题，我会保留你的学习进度。')
    } catch (cause) {
      if (cause instanceof DiagnosticTaskStartRequiredError) showTaskStartRecovery(cause.result)
      else setDiagnosticFeedback(cause instanceof ApiError ? cause.message : '暂时没有取到挑战题。')
    } finally {
      setDiagnosticBusy(false)
    }
  }

  useEffect(() => {
    const generation = ++petOriginalReadGeneration.current
    if (!boundPetActivityId || !['active', 'paused'].includes(next?.status ?? '') || privacyStopped) return
    let cancelled = false
    const context = studyContextRef.current
    const isCurrent = (): boolean => !cancelled && generation === petOriginalReadGeneration.current
      && studyContextRef.current === context && !privacyStopPending(workspace.session.session_id)
    setDiagnosticTrack('pet')
    // task.start owns preparation and playback. Mount/reload only restores the
    // existing prompt, never a second prepare or an unrequested audio replay.
    void localApi.diagnosticPrompt(workspace.session.session_id).then(result => {
      if (!isCurrent()) return
      const prompt = result.prompt
      if (!prompt || prompt.origin_session_id !== workspace.session.session_id
        || prompt.child_id !== workspace.session.child_id || prompt.diagnostic_item_id !== boundPetActivityId
        || prompt.item.track !== 'pet') {
        setDiagnosticFeedback('原任务的题目暂未核对成功，请说“继续原任务”；不会另出新题。')
        return
      }
      if (result.status === 'awaiting_task_start') { showTaskStartRecovery(result); return }
      if (result.status !== 'waiting_answer' && !isSavedNativeWritingPrompt(result,nativeWritingDraft.current)) {
        setDiagnosticFeedback(result.attempt?.feedback ?? '原回答状态需要核对，不重复播题或作答。')
        return
      }
      diagnosticPromptBinding.current = prompt
      setDiagnosticItem(prompt.item)
      setPetBaseline(result.pet_baseline ?? null)
    }).catch(cause => {
      if (isCurrent()) setDiagnosticFeedback(cause instanceof ApiError ? cause.message : '原题暂未读回，请稍后说“继续原任务”；不会另出新题。')
    })
    return () => { cancelled = true }
  }, [workspace.session.session_id, workspace.session.child_id, next?.plan_item_id, next?.status, boundPetActivityId, privacyStopped])

  useEffect(() => {
    const track = next?.source_type === 'competition_math'
      ? 'competition_math'
      : next?.source_type === 'junior_exam_goal'
        ? 'school'
        : null
    if (!track || next?.status !== 'active') {
      if (!track) specializedActivityLoadedRef.current = null
      return
    }
    if (specializedActivityLoadedRef.current === next.plan_item_id) return
    specializedActivityLoadedRef.current = next.plan_item_id
    setDiagnosticTrack(track)
    void loadDiagnostic(
      track,
      undefined,
      next.source_type === 'junior_exam_goal' ? next.subject : undefined
    )
  }, [next?.plan_item_id, next?.status, next?.source_type])

  const schoolSupplyPrompt = schoolSupplyPromptBinding(workspace, next)
  useEffect(() => {
    if (!schoolSupplyPrompt || next?.status !== 'paused' || privacyStopped) return
    let cancelled = false
    const context = studyContextRef.current
    const generation = ++petOriginalReadGeneration.current
    const isCurrent = (): boolean => !cancelled && studyMountedRef.current && studyContextRef.current === context
      && generation === petOriginalReadGeneration.current && !privacyStopPending(workspace.session.session_id)
    // Paused supply recovery is a read of the exact persisted item, never a
    // selection/prepare or permission to start, speak, or score it again.
    void localApi.diagnosticPrompt(workspace.session.session_id).then(result => {
      if (!isCurrent()) return
      const prompt = result.prompt
      if (!prompt || prompt.child_id !== workspace.session.child_id || prompt.origin_session_id !== workspace.session.session_id
        || prompt.diagnostic_item_id !== schoolSupplyPrompt.diagnosticItemId
        || prompt.item.diagnostic_item_id !== schoolSupplyPrompt.diagnosticItemId
        || prompt.item_version !== schoolSupplyPrompt.itemVersion || prompt.item.track !== 'school') {
        setDiagnosticFeedback('原练习的题目暂未核对成功，请说“继续原任务”；不会另出新题。')
        return
      }
      if (result.status === 'awaiting_task_start') { showTaskStartRecovery(result); return }
      if (result.status !== 'waiting_answer') {
        setDiagnosticFeedback(result.attempt?.feedback ?? '原题状态已变化，先核对原练习，不会重复作答。')
        return
      }
      diagnosticPromptBinding.current = prompt
      setDiagnosticItem(prompt.item)
      setDiagnosticTrack('school')
    }).catch(() => { if (isCurrent()) setDiagnosticFeedback('原题暂未读回，请稍后说“继续原任务”；不会另出新题。') })
    return () => { cancelled = true }
  }, [workspace.session.session_id, workspace.session.child_id, next?.plan_item_id, next?.status,
    schoolSupplyPrompt?.diagnosticItemId, schoolSupplyPrompt?.itemVersion, privacyStopped])

  const saveTutorFeedback = async (outcome: 'understood' | 'still_stuck', responseOverride?: string): Promise<boolean> => {
    if (!tutorTurn) return false
    const responseText = outcome === 'understood' ? '我现在能继续做了' : (responseOverride ?? tutorFeedbackText).trim()
    if (responseText.length < 2) {
      setTutorFeedbackMessage('请用一句话说说卡在哪里，例如“我看不懂题目问什么”。')
      return false
    }
    try {
      const feedback = await localApi.saveTutorFeedback(tutorTurn.turn_id, responseText, outcome)
      setTutorFeedbackMessage(`${feedback.error_hypothesis}。这只是本轮候选，还要用变式题验证。`)
      return true
    } catch (cause) {
      setTutorFeedbackMessage(cause instanceof ApiError ? cause.message : '这条反馈没有保存，请重试。')
      return false
    }
  }

  const submitDiagnostic = async (answerOverride?: string, nativeControl?: NativeWritingControl): Promise<boolean> => {
    const answer = (answerOverride ?? diagnosticAnswer).trim()
    if (active?.source_type === 'due_review') {
      if (dueReviewAnswerBusyRef.current) return true
      dueReviewAnswerBusyRef.current = true
      const item = active
      const isCurrent = (): boolean => currentReviewRef.current === item.plan_item_id && !privacyStopPending(workspace.session.session_id)
      setDiagnosticBusy(true)
      try {
        const attempt = await answerBoundReview(item, workspace.session.session_id, workspace.session.child_id, answer, isCurrent)
        if (!isCurrent()) return false
        const saved = await run(() => localApi.completePlanItem(item.plan_item_id, effectiveActiveMinutes(item), {
          type: 'child_checked', checklist: ['review_completed', 'self_tested'], note: '该复测题已通过语音作答并核对真实回执',
        }))
        if (!reviewSessionMountedRef.current || privacyStopPending(workspace.session.session_id)) return true
        const message = saved
          ? `${attempt.feedback}。这次回答已保存。后续复测还会按你的表现安排，今天不需要反复刷到答对。`
          : `${attempt.feedback}。回答已保存，但任务收尾还没确认。说“完成了”可以重新核对。`
        setDiagnosticFeedback(message)
        setDiagnosticAnswer('')
        if (saved) setDiagnosticItem(null)
        speakLocally(message, workspace.session.session_id)
        return true
      } catch (cause) {
        if (isCurrent()) {
          const message = cause instanceof Error ? cause.message : '这次回答还没有确认保存，请重试。'
          setDiagnosticFeedback(message)
          speakLocally(message, workspace.session.session_id, undefined, undefined, { localOnly: true })
        }
        return false
      } finally { dueReviewAnswerBusyRef.current = false; setDiagnosticBusy(false) }
    }
    if (!diagnosticItem || answer.length < 1) return false
    if (isWrittenDiagnostic(diagnosticItem)) {
      if(answer.length>4000){setDiagnosticFeedback('超出本次提交的 4000 字上限。原文未截断。');return false}
      const prompt=diagnosticPromptBinding.current,draft=nativeWritingDraft.current
      if(answerOverride !== undefined || !prompt || !draft || !sameWritingScope(draft.scope,writingScope(prompt))
        || draft.text.trim()!==answer || !window.desktop?.captureNativeWritingOriginal) {
        setDiagnosticFeedback('这句话没有作为书面原稿提交。请在本题原稿中核对文字；口述和控制话不会冒充原稿。')
        return false
      }
      if(nativeWritingSubmitting.current)return false
      nativeWritingSubmitting.current=true
      const scope=writingScope(prompt)
      const context=studyContextRef.current
      const isCurrent=():boolean=>Boolean(diagnosticPromptBinding.current
        && studyContextRef.current===context && studyMountedRef.current
        && nativeWritingDraft.current===draft && draft.text.trim()===answer
        && sameWritingScope(scope,writingScope(diagnosticPromptBinding.current))
        && (!nativeControl || nativeControl.isCurrent())
        && scope.child_id===workspace.session.child_id && scope.session_id===workspace.session.session_id
        && !privacyStopPending(scope.session_id) && reviewSessionMountedRef.current)
      setDiagnosticBusy(true)
      try {
        const result=await submitNativeWriting({scope,text:answer,isCurrent,
          ...(nativeControl?{resumeControl:{target:nativeControl.target,turnId:nativeControl.turnId}}:{}),
          capture:input=>window.desktop!.captureNativeWritingOriginal!(input),
          answer:values=>localApi.answerDiagnosticPrompt(scope.session_id,{...values,
            ...(nativeControl?{authorized_turn_id:nativeControl.turnId}:{})})})
        if(!result.attempt)throw Error('原稿已保存，但本题提交回执尚未确认；请保留原文重试。')
        if(!isCurrent())return true
        const message=result.native_writing_recovered
          ? '原稿和本题作答已核对保存；没有重新提交或启动评价，也不记作掌握。'
          : await presentNativeWritingFeedback(scope,result,isCurrent)
        if(isCurrent()){setDiagnosticFeedback(message);setDiagnosticAnswer('');nativeWritingDraft.current=null;setSavedNativeWritingDraft(null)}
        return true
      } catch(cause) {
        if(isCurrent())setDiagnosticFeedback(cause instanceof Error?cause.message:'原稿尚未确认提交，请保留原文重试。')
        return false
      } finally {nativeWritingSubmitting.current=false;setDiagnosticBusy(false)}
    }
    if (answerOverride) setDiagnosticAnswer(answer)
    setDiagnosticBusy(true)
    try {
      const attempt = await localApi.answerDiagnostic(workspace.session.session_id, diagnosticItem.diagnostic_item_id, answer, diagnosticItem.review_task_id)
      setDiagnosticFeedback(attempt.feedback)
      setDiagnosticKnowledgeNodeId(attempt.knowledge_graph?.status === 'recorded' && typeof attempt.knowledge_graph.node_id === 'string'
        ? attempt.knowledge_graph.node_id : null)
      if (attempt.pet_baseline) setPetBaseline(attempt.pet_baseline)
      if (attempt.next_specialized_item) {
        const prepared = await localApi.nextDiagnostic(workspace.session.session_id, attempt.next_specialized_item.track, attempt.next_specialized_item.diagnostic_item_id)
        setDiagnosticItem(prepared.item)
        setDiagnosticAnswer('')
        const progress = attempt.specialized_assessment
        if (prepared.item) await presentDiagnosticItem(prepared.item)
        else speakLocally(`${attempt.feedback}。后续题目尚未准备好，不需要继续作答。`, workspace.session.session_id)
      } else if (attempt.specialized_assessment?.status === 'completed') {
        setDiagnosticItem(null)
        const scores = attempt.specialized_assessment.learning_status === 'observed_checks_not_mastery'
          ? Object.values(attempt.specialized_assessment.domain_results) : []
        const average = scores.length ? Math.round(scores.reduce((sum, value) => sum + value, 0) / scores.length * 100) : null
        const pending = attempt.specialized_assessment.unverified_items ?? (average == null ? attempt.specialized_assessment.completed_items : 0)
        const message = `${attempt.feedback}。这组${attempt.specialized_assessment.completed_items}题的作答已经完成并保存${average == null ? '，评分仍需核验' : `，有效作答的本次参考值为${average}分`}${pending > 0 ? `，其中${pending}题尚不能作为独立能力证据` : ''}。这不代表已经掌握，我会用后续不同题目和跨天复测继续核对。`
        setDiagnosticFeedback(message)
        speakLocally(message, workspace.session.session_id)
      } else {
        speakLocally(attempt.feedback, workspace.session.session_id)
      }
      return true
    } catch (cause) {
      setDiagnosticKnowledgeNodeId(null)
      if (cause instanceof DiagnosticTaskStartRequiredError) showTaskStartRecovery(cause.result)
      else setDiagnosticFeedback(cause instanceof ApiError ? cause.message : '答案没有保存，请重试。')
      return false
    } finally {
      setDiagnosticBusy(false)
    }
  }

  useEffect(() => {
    const submit=(event:Event):void=>{
      const detail=(event as CustomEvent<NativeWritingControl>).detail
      if(!detail?.target || detail.target.child_id!==workspace.session.child_id || detail.target.session_id!==workspace.session.session_id)return
      event.preventDefault()
      const saved=nativeWritingDraft.current, bound=diagnosticPromptBinding.current, context=studyContextRef.current
      const current=():boolean=>Boolean(detail.isCurrent() && studyMountedRef.current && studyContextRef.current===context
        && !privacyStopPending(workspace.session.session_id) && nativeWritingDraft.current===saved
        && bound && diagnosticPromptBinding.current && sameWritingScope(writingScope(diagnosticPromptBinding.current),detail.target))
      void (async()=>{
        if(!current())throw Error('原题或当前孩子已变化，未提交原稿。')
        if(!await retainedNativeWritingCurrent(saved,detail.target,current)) {
          // An unknown operation is read only, even when the child asks to continue.
          const message=await checkNativeWritingSubmission(detail.target.child_id,detail.target.session_id,current,
            undefined,detail.target)
          detail.resolve(message??'原稿尚未核对，未重新提交。')
          return
        }
        if(!current())throw Error('原稿范围已经变化，未提交。')
        const savedAnswer=await submitDiagnostic(undefined,detail)
        detail.resolve(savedAnswer?'本题保留的原稿已收到作答回执；这不表示已经掌握。':'原稿的提交回执尚未确认，原文和编号保留，没有自动重发。')
      })().catch(error=>detail.resolve(error instanceof Error?error.message:'原稿暂时无法核对，未重新提交。'))
    }
    window.addEventListener(NATIVE_WRITING_CONTROL,submit)
    return ()=>window.removeEventListener(NATIVE_WRITING_CONTROL,submit)
  })

  const visiblePrompt = diagnosticPromptBinding.current ?? (next?.status === 'paused'
    && next.source_type === 'due_review' ? restoredQuestionViewport.current : null)
  const diagnosticQuestionRef = useQuestionViewportFocus<HTMLDivElement>(
    !privacyStopped && diagnosticItem && visiblePrompt
      && visiblePrompt.child_id === workspace.session.child_id
      && visiblePrompt.origin_session_id === workspace.session.session_id
      && visiblePrompt.diagnostic_item_id === diagnosticItem.diagnostic_item_id
      ? `${workspace.session.child_id}:${workspace.session.session_id}:${visiblePrompt.prompt_id}:${visiblePrompt.item_version}:${next?.status ?? 'standalone'}`
      : null,
    () => studyMountedRef.current && studyContextRef.current === studyContext
      && !privacyStopPending(workspace.session.session_id) && (diagnosticPromptBinding.current === visiblePrompt || restoredQuestionViewport.current === visiblePrompt))


  function failStandaloneRead(generation: number, speechOwner: 'study' | 'agent' = 'study'): void {
    if (!studyMountedRef.current || generation !== petOriginalReadGeneration.current
      || studyContextRef.current !== studyContext
      || privacyStopPending(workspace.session.session_id)) return
    diagnosticPlayback.current.abort()
    setDiagnosticItem(null)
    setDiagnosticKnowledgeNodeId(null)
    standaloneReadSpeechOwner.current = speechOwner
    setStandaloneReadError(DIAGNOSTIC_READ_FAILURE_MESSAGE)
    // Keep the original identity and unsent text for recovery, not as permission
    // to answer. The real voice replay must read the server prompt again.
  }

  useEffect(() => {
    if (!standaloneReadError || standaloneReadSpeechOwner.current === 'agent' || privacyStopped || (next && !allSettled)) return
    let announced = false
    const announce = (): void => {
      if (announced || !studyMountedRef.current || privacyStopPending(workspace.session.session_id)
        || tutorialSpeechOwners.has(workspace.session.session_id)) return
      announced = true
      speakLocally(standaloneReadError, workspace.session.session_id, undefined, undefined, { localOnly: true })
    }
    const released = (event: Event): void => {
      if ((event as CustomEvent<string>).detail === workspace.session.session_id) announce()
    }
    window.addEventListener('hsc-tutorial-speech-released', released)
    announce()
    return () => window.removeEventListener('hsc-tutorial-speech-released', released)
  }, [standaloneReadError, workspace.session.session_id, workspace.session.child_id, privacyStopped, next?.plan_item_id, allSettled])

  useEffect(() => {
    standaloneReplayReadRef.current = null
    return () => { standaloneReplayReadRef.current = null }
  }, [studyContext, workspace.session.child_id])

  useEffect(() => {
    if (boundPetActivityId || privacyStopped || (next && !allSettled)) return
    const read = (event: Event): void => {
      const detail = (event as CustomEvent<DiagnosticReplayReadDetail>).detail
      const original = diagnosticPromptBinding.current
      if (!studyMountedRef.current || studyContextRef.current !== studyContext
        || privacyStopPending(workspace.session.session_id) || !original
        || detail.sessionId !== workspace.session.session_id || detail.childId !== workspace.session.child_id
        || detail.promptId !== original.prompt_id || detail.itemVersion !== original.item_version
        || !detail.requestId) return
      if (original.review_task_id && workspace.plan_items.some(item => item.source_type === 'due_review'
        && item.source_ref === original.review_task_id && ['completed', 'cancelled'].includes(item.status))) return
      const pending = standaloneReplayReadRef.current
      if (detail.phase === 'begin') standaloneReplayReadRef.current = {
        sessionId: detail.sessionId, childId: detail.childId, promptId: detail.promptId,
        itemVersion: detail.itemVersion, requestId: detail.requestId, generation: ++petOriginalReadGeneration.current,
      }
      else if (detail.phase === 'failed' && pending?.requestId === detail.requestId
        && pending.sessionId === detail.sessionId && pending.childId === detail.childId
        && pending.promptId === detail.promptId && pending.itemVersion === detail.itemVersion
        && pending.generation === petOriginalReadGeneration.current) {
        failStandaloneRead(pending.generation, 'agent')
        standaloneReplayReadRef.current = null
        detail.handled = true
      }
    }
    window.addEventListener('hsc-diagnostic-replay-read', read)
    return () => window.removeEventListener('hsc-diagnostic-replay-read', read)
  }, [workspace.session.session_id, workspace.session.child_id, workspace.plan_items, boundPetActivityId, privacyStopped, next?.plan_item_id, allSettled])

  useEffect(() => {
    setStandaloneReadError(null)
    // Ordinary homework may retain a scored question after an app restart.
    // Reading it restores help identity only; it does not reopen or rescore it.
    const restoreHomeworkHelp = active?.source_type === 'school_homework' && !schoolSupplyPrompt
    if (boundPetActivityId || privacyStopped || (next && !allSettled && !restoreHomeworkHelp)) return
    let cancelled = false
    const generation = ++petOriginalReadGeneration.current
    diagnosticPromptBinding.current = null
    setDiagnosticItem(null)
    void localApi.diagnosticPrompt(workspace.session.session_id).then(result => {
      if (cancelled || !studyMountedRef.current || generation !== petOriginalReadGeneration.current
        || privacyStopPending(workspace.session.session_id)) return
      const prompt = result.prompt
      if (!prompt) return
      if (prompt.child_id !== workspace.session.child_id
        || prompt.origin_session_id !== workspace.session.session_id
        || prompt.item.diagnostic_item_id !== prompt.diagnostic_item_id) { failStandaloneRead(generation); return }
      if (prompt.review_task_id && workspace.plan_items.some(item => item.source_type === 'due_review'
        && item.source_ref === prompt.review_task_id && ['completed', 'cancelled'].includes(item.status))) return
      if (result.status === 'awaiting_task_start') { showTaskStartRecovery(result); return }
      if (result.status !== 'waiting_answer' && !isSavedNativeWritingPrompt(result,nativeWritingDraft.current)) return
      diagnosticPromptBinding.current = prompt
      setDiagnosticItem(prompt.item)
      setDiagnosticTrack(prompt.item.track)
    }).catch(() => { if (!cancelled) failStandaloneRead(generation) })
    return () => { cancelled = true }
  }, [workspace.session.session_id, workspace.session.child_id, boundPetActivityId, privacyStopped,
    next?.plan_item_id, active?.source_type, schoolSupplyPrompt?.diagnosticItemId, allSettled])

  useEffect(() => {
    const sync = (event: Event): void => {
      const detail = (event as CustomEvent<{ sessionId: string; result: import('./types').DiagnosticPromptResult }>).detail
      if (detail.sessionId !== workspace.session.session_id) return
      if (detail.result.prompt && (detail.result.prompt.child_id !== workspace.session.child_id
        || detail.result.prompt.origin_session_id !== workspace.session.session_id)) return
      const generation = ++petOriginalReadGeneration.current
      const apply = (result: import('./types').DiagnosticPromptResult): void => {
        if (!studyMountedRef.current || studyContextRef.current !== studyContext
          || generation !== petOriginalReadGeneration.current || privacyStopPending(workspace.session.session_id)) return
        setStandaloneReadError(null)
        if (result.prompt && tutorTurn && result.prompt.created_at > tutorTurn.created_at) setTutorTurn(null)
        if (result.prompt?.review_task_id && workspace.plan_items.some(item => item.source_type === 'due_review'
          && item.source_ref === result.prompt?.review_task_id && ['completed', 'cancelled'].includes(item.status))) {
          diagnosticPromptBinding.current = null
          setDiagnosticItem(null)
          return
        }
        if (result.status === 'awaiting_task_start') { showTaskStartRecovery(result); return }
        const samePrompt = diagnosticPromptBinding.current?.prompt_id === result.prompt?.prompt_id
          && diagnosticPromptBinding.current?.item_version === result.prompt?.item_version
        const retained = result.status === 'waiting_answer' || isSavedNativeWritingPrompt(result,nativeWritingDraft.current)
        diagnosticPromptBinding.current = retained ? result.prompt : null
        setDiagnosticItem(retained ? result.prompt?.item ?? null : null)
        const typed = nativeWritingDraft.current
        const sameTypedDraft = samePrompt && retained && result.prompt
          && isWrittenDiagnostic(result.prompt.item) && typed
          && sameWritingScope(typed.scope, writingScope(result.prompt))
        if (!sameTypedDraft && ((next && !allSettled) || !samePrompt || !retained)) setDiagnosticAnswer('')
        setDiagnosticFeedback(result.attempt?.feedback ?? '')
      }
      if (!next || allSettled) {
        // A late receipt for a previous standalone question cannot become the
        // visible/current question after another prompt has been prepared.
        void localApi.diagnosticPrompt(workspace.session.session_id).then(current => {
          if (generation !== petOriginalReadGeneration.current || !studyMountedRef.current
            || studyContextRef.current !== studyContext || privacyStopPending(workspace.session.session_id)) return
          const prompt = current.prompt
          if (prompt && (prompt.child_id !== workspace.session.child_id
            || prompt.origin_session_id !== workspace.session.session_id
            || prompt.item.diagnostic_item_id !== prompt.diagnostic_item_id)) { failStandaloneRead(generation); return }
          if (current.prompt?.prompt_id !== detail.result.prompt?.prompt_id
            || current.prompt?.item_version !== detail.result.prompt?.item_version
            || current.prompt?.child_id !== workspace.session.child_id
            || current.prompt?.origin_session_id !== workspace.session.session_id) {
            const shown = diagnosticPromptBinding.current
            if (current.status === 'waiting_answer' && shown && prompt?.prompt_id === shown.prompt_id
              && prompt.item_version === shown.item_version) return
            failStandaloneRead(generation)
            return
          }
          apply(current)
        }).catch(() => failStandaloneRead(generation))
      } else apply(detail.result)
    }
    window.addEventListener('hsc-diagnostic-prompt-updated', sync)
    return () => window.removeEventListener('hsc-diagnostic-prompt-updated', sync)
  }, [workspace.session.session_id, workspace.session.child_id, studyContext, allSettled])

  useEffect(() => {
    const handle = (event: Event): void => {
      const detail = (event as CustomEvent<StudyLearningVoiceDetail>).detail
      event.preventDefault()
      const execute = async (): Promise<boolean> => {
        if (detail.action === 'start_diagnostic') {
          if (active?.source_type === 'due_review') { await loadCurrentPlanReview(); return true }
          await loadDiagnostic(detail.track ?? diagnosticTrack)
          return true
        }
        if (detail.action === 'replay_diagnostic') {
          if (!diagnosticItem && active?.source_type === 'due_review') { await loadCurrentPlanReview(); return true }
          if (!diagnosticItem) return false
          await presentDiagnosticItem(diagnosticItem)
          return true
        }
        if (detail.action === 'submit_diagnostic_answer') return submitDiagnostic(detail.answer)
        if (detail.action === 'record_tutor_feedback') return detail.outcome ? saveTutorFeedback(detail.outcome, detail.feedbackText) : false
        if (detail.action === 'more_tutor_hint') {
          if (!tutorTurn || tutorTurn.hint_level >= 6) return false
          await askTutor(tutorTurn.hint_level + 1, tutorTurn)
          return true
        }
        return false
      }
      void execute().then(detail.resolve).catch(() => detail.resolve(false))
    }
    window.addEventListener('hsc-study-learning-voice-action', handle)
    return () => window.removeEventListener('hsc-study-learning-voice-action', handle)
  }, [diagnosticItem, diagnosticTrack, diagnosticAnswer, tutorTurn, tutorFeedbackText, next?.plan_item_id, active?.plan_item_id, active?.source_type, privacyStopped])

  const goToReflection = async (): Promise<void> => {
    const moved = await run(() => localApi.transition(workspace.session.session_id, 'study', 'reflection', 'tasks_completed'))
    if (moved) draft.markCommitted()
  }

  const studyHeading = !allSettled ? (next ? planItemTitle(next) : '没有待执行任务')
    : !privacyStopped && standaloneReadError ? '正在核对当前练习'
    : !privacyStopped && diagnosticItem ? '继续眼前这道题'
    : '今天的计划已经收好'

  return (
    <div className="study-layout">
      {savedMistakeReviewId && !privacyStopped && <VoiceMistakeDialogue sessionId={workspace.session.session_id}
        childId={workspace.session.child_id} mistakeId={savedMistakeReviewId} />}
      <div className="study-summary">
        <span className="section-kicker">执行计划</span>
        <h2>{studyHeading}</h2>
        <p>{completedCount}项完成{deferredCount > 0 ? ` · ${deferredCount}项已延期并记录原因` : ''}</p>
      </div>
      {!allSettled && next && (
        <div className="focus-band">
          <div className="focus-time">
            <Clock size={20} />
            <strong>{next.status === 'active' || next.status === 'paused' ? formatElapsed(elapsedMilliseconds) : `${next.expected_minutes}:00`}</strong>
            <span>{next.status === 'active' ? '已专注' : next.status === 'paused' ? '已暂停，计时已停止' : '预计用时'}</span>
          </div>
          <div className="focus-actions">
            {next.status === 'pending' && next.source_type === 'planned_break' ? (
              <button className="primary-button" disabled={busy} onClick={() => void run(() => localApi.beginBreak(workspace.session.session_id))}><Coffee size={17} />开始休息</button>
            ) : next.status === 'pending' ? (
              <><button className="primary-button" disabled={busy} onClick={() => void run(() => localApi.startPlanItem(next.plan_item_id))}><Play size={18} />开始这项</button><button className="secondary-button inverse-button" disabled={busy} onClick={() => void run(() => localApi.reschedulePlanItem(next.plan_item_id, '孩子选择今天延期'))}><CalendarClock size={17} />延期</button></>
            ) : next.status === 'paused' ? (
              <><button className="primary-button" disabled={busy} onClick={() => void run(() => localApi.resumePlanItem(next.plan_item_id))}><RotateCcw size={18} />继续这项</button><button className="secondary-button inverse-button" disabled={busy} onClick={() => void run(() => localApi.reschedulePlanItem(next.plan_item_id, '暂停后决定延期'))}><CalendarClock size={17} />延期</button></>
            ) : (
              <><button className="secondary-button inverse-button" disabled={busy} onClick={() => void run(() => localApi.pausePlanItem(next.plan_item_id))}><Pause size={17} />暂停任务</button><button className="secondary-button inverse-button" disabled={busy} onClick={() => void run(() => localApi.beginBreak(workspace.session.session_id))}><Coffee size={17} />休息</button>{!isMistakeReview && <button className="primary-button" disabled={busy} onClick={() => setCompletionReviewOpen(true)}><Check size={18} />完成这项</button>}</>
            )}
          </div>
        </div>
      )}
      {!privacyStopped && isMistakeReview && next?.source_ref && <MistakePlanActivity sessionId={workspace.session.session_id} childId={workspace.session.child_id} mistakeId={next.source_ref} active={next.status === 'active'} onProgress={async (updated, owned = () => true) => { const current = () => owned() && studyMountedRef.current && studyContextRef.current === studyContext && !privacyStopPending(workspace.session.session_id); return run(() => localApi.completePlanItem(next.plan_item_id, elapsedMinutes, { type: 'child_confirmed', note: `错题步骤已保存：${updated.status}` }), current) }} />}
      {completionReviewOpen && next && !isMistakeReview && ['active', 'paused'].includes(next.status) && (
        <section className="completion-review" aria-label="完成核对">
          <div><strong>完成前核对一下</strong><span>{next.completion_standard}</span></div>
          <div className="track-control" role="group" aria-label="完成情况">
            <button className={completionEvidence === 'child_checked' ? 'is-active' : ''} onClick={() => setCompletionEvidence('child_checked')}>已完成并检查</button>
            <button className={completionEvidence === 'child_confirmed' ? 'is-active' : ''} onClick={() => setCompletionEvidence('child_confirmed')}>已完成</button>
            <button className={completionEvidence === 'needs_correction' ? 'is-active' : ''} onClick={() => setCompletionEvidence('needs_correction')}>还需订正</button>
          </div>
          {completionEvidence === 'child_checked' && requiredCompletionChecks.length > 0 && <fieldset className="completion-checklist"><legend>逐项核对</legend>{requiredCompletionChecks.map((item) => <label key={item.id}><input type="checkbox" checked={completionChecklist.includes(item.id)} onChange={(event) => setCompletionChecklist((current) => event.target.checked ? [...current, item.id] : current.filter((value) => value !== item.id))} /><span>{item.label}</span></label>)}</fieldset>}
          <input aria-label="完成备注" value={completionNote} maxLength={500} onChange={(event) => setCompletionNote(event.target.value)} placeholder="可选：还有哪里需要处理" />
          {['written', 'correction', 'reasoning', 'test_prep'].includes(next.task_type) && <div className="completion-artifact-control">
            <button className="secondary-button" onClick={() => setCompletionCaptureOpen((current) => !current)}><Camera size={16} />{completionCaptureOpen ? '收起完成页采集' : completionArtifact ? '重新采集完成页' : '附上完成页'}</button>
            {completionArtifact && <span>{completionArtifact.local_path ? '完成页原图已加密保存' : '已完成页面检查，原图未按隐私设置保留'} · {completionArtifact.details.quality?.passed ? '画面质量通过' : '画面有质量提醒'}</span>}
          </div>}
          {completionCaptureOpen && <DocumentCameraCapture sessionId={workspace.session.session_id} onArtifact={(artifact) => { setCompletionArtifact(artifact); setCompletionCaptureOpen(false) }} />}
          <div><button className="secondary-button" disabled={busy} onClick={() => setCompletionReviewOpen(false)}>继续学习</button><button className="primary-button" disabled={busy || (completionEvidence === 'child_checked' && !checklistComplete)} onClick={() => void completeTask()}><Check size={17} />确认记录</button></div>
        </section>
      )}
      {!privacyStopped && standaloneReadError && <div role="alert" className="diagnostic-feedback">{standaloneReadError}</div>}
      {!privacyStopped && (diagnosticItem || (!allSettled && next && next.source_type !== 'planned_break' && !isMistakeReview)) && (
        <section className="diagnostic-panel" aria-label="能力挑战">
          <div className="tutor-heading"><BrainCircuit size={22} /><div><strong>能力挑战</strong><span>一次只做一道，用结果安排下次复测。</span></div></div>
          {boundPetActivityId ? (
            <div className="pet-bound-activity"><strong>今日PET活动</strong><span>这项任务已绑定具体练习；提交练习证据后才能记录完成。</span></div>
          ) : (
            <div className="track-control">
              {(childGrade === '四年级'
                ? [['school', '校内'], ['pet', 'PET'], ['competition_math', '数学思维']]
                : [['school', '校内']]
              ).map(([value, label]) => (
                <button className={diagnosticTrack === value ? 'is-active' : ''} key={value} onClick={() => { setDiagnosticTrack(value); setDiagnosticItem(null); setPetBaseline(null); setDiagnosticFeedback(null) }}>{label}</button>
              ))}
            </div>
          )}
          {diagnosticTrack === 'pet' && petBaseline && <PetBaselineProgress baseline={petBaseline} compact />}
          {!diagnosticItem ? (
            <button className="secondary-button" disabled={diagnosticBusy} onClick={() => void loadDiagnostic(boundPetActivityId ? 'pet' : diagnosticTrack, boundPetActivityId ?? undefined)}>{diagnosticBusy ? '正在准备' : boundPetActivityId ? '加载今日活动' : '来一道题'}</button>
          ) : (
            <div ref={diagnosticQuestionRef} className="diagnostic-question" data-question-viewport="diagnostic">
              <span>{diagnosticItem.review_interval ? reviewLabel(diagnosticItem.review_interval) : diagnosticItem.title}{diagnosticItem.assessment_mode === 'pet_reference_rubric' ? ' · 参考评估' : ''}</span><p>{diagnosticItem.prompt}</p>
              {diagnosticItem.stimulus_asset === 'pet-study-room' && <img className="pet-stimulus" src={petStudyRoom} alt="两名孩子在书房学习，一名阅读，一名书写，背景中家长正在放下一杯水" />}
              {!privacyStopped && diagnosticPromptBinding.current && diagnosticItem.assessment_mode === 'open_response_rubric'
                && diagnosticPromptBinding.current.diagnostic_item_id === diagnosticItem.diagnostic_item_id
                && <DiagnosticVisual binding={{ child_id: workspace.session.child_id, session_id: workspace.session.session_id,
                  prompt_id: diagnosticPromptBinding.current.prompt_id, item_version: diagnosticPromptBinding.current.item_version }} />}
              {(diagnosticItem.answer_type === 'listening_text' || diagnosticItem.pet_audio_modality === 'listening') && <button className="secondary-button" onClick={() => { void presentDiagnosticItem(diagnosticItem).catch(() => setDiagnosticFeedback('原音播放未确认，记录已保留，请稍后重试。')) }}><Volume2 size={17} />播放原音</button>}
              {diagnosticItem.answer_type === 'read_aloud' && diagnosticItem.stimulus_text && <blockquote className="read-aloud-passage">{diagnosticItem.stimulus_text}</blockquote>}
              {diagnosticItem.answer_type === 'speaking' && diagnosticItem.stimulus_text && <blockquote className="read-aloud-passage">{diagnosticItem.stimulus_text}</blockquote>}
              {diagnosticItem.answer_type === 'speaking' && <button className="secondary-button" onClick={() => speakLocally(diagnosticItem.stimulus_text ?? diagnosticItem.prompt, workspace.session.session_id)}><Volume2 size={17} />{diagnosticItem.stimulus_text ? '播放故事' : '播放对话题'}</button>}
              <textarea aria-label="输入挑战题答案" value={diagnosticAnswer} onChange={(event) => {
                setDiagnosticAnswer(event.target.value)
                writingDraftRestored.current=true
                nativeWritingDraft.current=isWrittenDiagnostic(diagnosticItem)&&diagnosticPromptBinding.current
                  ? {input_kind:'typed',scope:writingScope(diagnosticPromptBinding.current),text:event.target.value}:null
                if(nativeWritingDraft.current)setSavedNativeWritingDraft(nativeWritingDraft.current)
              }} placeholder={diagnosticItem.answer_type === 'read_aloud' ? '直接朗读；这里会显示本地转写，提交前可以核对。' : undefined} />
              {isWrittenDiagnostic(diagnosticItem) && diagnosticAnswer.trim().length > 4000 && <p role="status">超出本次提交的 4000 字上限。原文未截断。</p>}
              {['speaking', 'read_aloud'].includes(diagnosticItem.answer_type) && <VoiceInputButton sessionId={workspace.session.session_id} purpose={diagnosticItem.answer_type === 'read_aloud' ? 'pet_read_aloud' : 'pet_speaking'} childId={workspace.session.child_id} onTranscript={(text) => setDiagnosticAnswer((current) => current ? `${current} ${text}` : text)} />}
              {boundPetActivityId && next?.status === 'pending' && <span className="diagnostic-start-note">先说“开始这项”，再提交本次活动。</span>}
              <button className="primary-button" disabled={diagnosticBusy || diagnosticAnswer.trim().length < 1 || (isWrittenDiagnostic(diagnosticItem) && diagnosticAnswer.trim().length > 4000) || Boolean(boundPetActivityId && next?.status === 'pending')} onClick={() => void submitDiagnostic()}>提交答案</button>
            </div>
          )}
          {diagnosticFeedback && <div className="diagnostic-feedback">{diagnosticFeedback}</div>}
          {diagnosticKnowledgeNodeId && <KnowledgeGraphDiagnosticGuidance childId={workspace.session.child_id} nodeId={diagnosticKnowledgeNodeId} />}
        </section>
      )}
      <div className="execution-list">
        <SupplyObligationStatus obligations={workspace.supply_obligations} />
        {workspace.plan_items.map((item, index) => (
          <div className={`execution-row status-${item.status}`} key={item.plan_item_id}>
            <i>{item.status === 'completed' ? <Check size={15} /> : index + 1}</i>
            <div><strong>{planItemTitle(item)}</strong><span>{item.subject} · {sourceLabel(item.source_type)} · 预计{item.expected_minutes}分钟</span></div>
            <b>{item.status === 'completed' ? `${completionEvidenceLabel(item.completion_evidence_type)} · ${item.actual_minutes ?? item.expected_minutes}分钟` : item.status === 'active' ? '进行中' : item.status === 'paused' ? '已暂停' : item.status === 'rescheduled' ? '已延期' : '等待'}</b>
          </div>
        ))}
      </div>
      <DraftSaveStatus state={draft.state} restored={Boolean(workspace.drafts.study)} />
      {!allSettled && next && next.source_type !== 'planned_break' && (
        <section className="tutor-panel" aria-label="向学习伙伴提问">
          <div className="tutor-heading">
            <MessageCircleQuestion size={22} />
            <div><strong>哪里卡住了？</strong><span>我会先给一个小提示，再和你一起想。</span></div>
          </div>
          {tutorTurn && <div className="tutor-answer">
            <strong>学习伙伴 · 第{tutorTurn.hint_level}级提示</strong>
            {!tutorBlackboardFromTurn(tutorTurn) && <p>{tutorTurn.answer}</p>}
            <div className="tutor-response">
              <input aria-label="告诉学习伙伴卡在哪里" value={tutorFeedbackText} onChange={(event) => setTutorFeedbackText(event.target.value)} placeholder="例如：我看不懂题目问什么" />
              <button className="secondary-button" onClick={() => void saveTutorFeedback('understood')}>我懂了</button>
              <button className="secondary-button" onClick={() => void saveTutorFeedback('still_stuck')}>还卡着</button>
              {tutorTurn.hint_level < 6 && <button className="secondary-button" disabled={tutorBusy} onClick={() => void askTutor(tutorTurn.hint_level + 1, tutorTurn)}>再给一点提示</button>}
            </div>
            {tutorFeedbackMessage && <span>{tutorFeedbackMessage}</span>}
            {tutorTurn.output_envelope ? <span>{tutorTurn.output_envelope.reason_summary}</span> : <>
              {tutorTurn.hint_level === 6 && tutorTurn.verification_status === 'cross_provider_agreed' && <span>结论已由另一家模型交叉核对，仍建议用课本方法或代入再检查一次。</span>}
              {tutorTurn.hint_level === 6 && tutorTurn.verification_status !== 'cross_provider_agreed' && <span>最终结论未通过独立核验，当前只提供方法，不把候选答案作为正确答案。</span>}
            </>}
          </div>}
          {tutorError && <div className={`tutor-error tutor-recovery${tutorSafetyBlocked ? ' is-safety' : ''}`} role="alert" aria-live="assertive"><span>{tutorError}</span><div>{tutorUnknownOutcome ? <><button className="secondary-button" disabled={tutorBusy || !tutorRequestRecovery} onClick={() => tutorRequestRecovery && void askTutor(tutorRequestRecovery.hintLevel, undefined, true, tutorRequestRecovery)}>确认再次请求</button><button className="secondary-button" onClick={() => void cancelUnknownTutorRetry()}>保留题干，改为新请求</button></> : !tutorSafetyBlocked && <button className="secondary-button" disabled={tutorBusy || question.trim().length < 2} onClick={() => void askTutor()}>重试本题</button>}<button className="secondary-button" onClick={() => { setTutorError(null); setTutorSafetyBlocked(false); setTutorUnknownOutcome(false) }}>{tutorSafetyBlocked ? '我知道了，继续当前任务' : '继续当前任务'}</button></div></div>}
          <button className="secondary-button" onClick={() => setCaptureVisible((current) => !current)}><Camera size={17} />{captureVisible ? '收起拍题' : '用高拍仪拍题'}</button>
          {captureVisible && <DocumentCameraCapture key={captureGeneration} commandId={captureCommandId} automation="capture_and_recognize" sessionId={workspace.session.session_id} onGuidance={(message) => { if (!privacyStopPending(workspace.session.session_id)) speakLocally(message, workspace.session.session_id) }} onArtifact={(artifact) => {
            const review = createOcrTutorReview(artifact)
            setOcrTutorReview(review); setOcrTutorReviewText(review.text)
          }} onText={(text, artifact) => {
            const review = createOcrTutorReview({ ...artifact, extracted_text: text })
            setOcrTutorReview(review); setOcrTutorReviewText(review.text)
            const markedWrongQuestion = hasMarkedWrongQuestion(artifact)
            const openMistakeDialogue = markedWrongQuestion || mistakePhotoRequestedRef.current
            if (!openMistakeDialogue) { currentOcrReviewRef.current=review;const owner=studyContextRef.current;speakOrdinaryPrompt(ocrTutorReviewMessage(review),workspace.session,{localOnly:true,isCurrent:()=>studyMountedRef.current&&studyContextRef.current===owner&&currentOcrReviewRef.current===review}) }
            setMistakeCaptureArtifact(artifact); setMistakeComposerOpen(openMistakeDialogue)
          }} />}
          {ocrTutorReview && !mistakeComposerOpen && <section className={`tutor-ocr-review is-${ocrTutorReview.status}`} aria-label="题干核对">
            <strong>{ocrTutorReview.status === 'confirmed' ? '题干已确认' : ocrTutorReview.status === 'unreadable' ? '请重新拍题' : ocrTutorReview.status === 'uncertain' ? '题干需要人工修正' : '先核对题干'}</strong>
            <span>{ocrTutorReviewMessage(ocrTutorReview)}</span>
            {!['confirmed', 'unreadable'].includes(ocrTutorReview.status) && <textarea aria-label="校对后的题干" value={ocrTutorReviewText} onChange={(event) => setOcrTutorReviewText(event.target.value.slice(0, 4000))} placeholder="逐字核对题干、数字和选项；不清楚的地方先补全" />}
            <div>{!['confirmed', 'unreadable'].includes(ocrTutorReview.status) && <button className="secondary-button" disabled={ocrTutorConfirmationBusy || ocrTutorReviewText.trim().length < 2} onClick={() => void confirmOcrTutorReview()}>{ocrTutorConfirmationBusy ? '正在保存确认' : ocrTutorReview.status === 'uncertain' ? '我已逐字人工修正' : '我已核对题干'}</button>}<button className="secondary-button" onClick={() => { setOcrTutorReview(null); setOcrTutorReviewText(''); setQuestion(''); setCaptureVisible(true); setCaptureGeneration((value) => value + 1) }}>重新拍题</button><button className="secondary-button" onClick={() => setCaptureVisible(false)}>取消拍题</button></div>
          </section>}
          {!mistakeCaptureArtifact && pendingMistakeQueues.length > 0 && <button className="secondary-button mistake-capture-entry" onClick={() => { setMistakeCaptureArtifact(pendingMistakeQueues[0]); setMistakeComposerOpen(true) }}><BookPlus size={17} />继续整理拍题（{pendingMistakeQueues.reduce((sum, item) => sum + item.pending_count, 0)} 道）</button>}
          {mistakeCaptureArtifact && !mistakeComposerOpen && <button className="secondary-button mistake-capture-entry" onClick={() => setMistakeComposerOpen(true)}><BookPlus size={17} />这是一道错题，整理到错题本</button>}
          {mistakeCaptureArtifact && mistakeComposerOpen && <VoiceMistakeDialogue key={mistakeCaptureArtifact.artifact_id}
            sessionId={workspace.session.session_id} childId={workspace.session.child_id} artifactId={mistakeCaptureArtifact.artifact_id}
            openSignal={pendingMistakeOpenRef.current?.signal} onOpened={(handled, dialogue) => {
              const pending = pendingMistakeOpenRef.current
              if (pending?.sessionId === workspace.session.session_id && pending.childId === workspace.session.child_id
                && pending.artifactId === mistakeCaptureArtifact.artifact_id) pending.finish(handled, dialogue)
            }} />}
          <div className="tutor-input-row">
            <textarea
              aria-label="输入学习问题"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="这道题卡在哪一步？"
            />
            <VoiceInputButton sessionId={workspace.session.session_id} purpose="tutor_question" childId={workspace.session.child_id} onTranscript={(text) => setQuestion((current) => current ? `${current} ${text}` : text)} />
            <button className="primary-button" title="发送问题" disabled={tutorBusy || question.trim().length < 2} onClick={() => void askTutor()}>
              <Send size={18} />{tutorBusy ? '思考中' : '提问'}
            </button>
          </div>
          {tutorBusy && ocrTutorReview?.status === 'confirmed' && <button className="secondary-button" onClick={() => ocrTutorAbortRef.current?.abort()}>取消本次讲解</button>}
        </section>
      )}
      {allSettled && (
        <div className="step-actions"><button className="primary-button" disabled={busy} onClick={() => void goToReflection()}>回顾一下今天<ArrowRight size={18} /></button></div>
      )}
    </div>
  )
}

function BreakStep({ workspace, busy, run }: { workspace: SessionWorkspaceState; busy: boolean; run: (action: () => Promise<unknown>) => Promise<boolean> }): React.JSX.Element {
  const [now, setNow] = useState(Date.now())
  const isPlannedBreak = workspace.plan_items.some((item) => item.source_type === 'planned_break' && item.status === 'active')
  const breakEndsAt = new Date(workspace.session.last_checkpoint_at).getTime() + workspace.family_rule.break_minutes * 60 * 1000
  const remaining = Math.max(0, breakEndsAt - now)
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  return (
    <div className="step-panel break-panel">
      <Coffee size={32} />
      <span className="section-kicker">休息时间</span>
      <h2>{remaining > 0 ? formatElapsed(remaining) : '可以继续了'}</h2>
      <p>这次休息{workspace.family_rule.break_minutes}分钟。喝点水、看看远处，活动一下肩颈。任务计时已经暂停。</p>
      <button className="primary-button" disabled={busy} onClick={() => void run(() => localApi.resumeSession(workspace.session.session_id))}>
        <Play size={18} />{isPlannedBreak ? '结束休息，继续计划' : '继续刚才的任务'}
      </button>
    </div>
  )
}

function ReflectionStep({ workspace, busy, run }: { workspace: SessionWorkspaceState; busy: boolean; run: (action: () => Promise<unknown>) => Promise<boolean> }): React.JSX.Element {
  const restoredDraft = workspace.drafts.reflection?.payload ?? {}
  const [learnedText, setLearnedText] = useState(draftString(restoredDraft, 'learned_text'))
  const [difficultText, setDifficultText] = useState(draftString(restoredDraft, 'difficult_text'))
  const [nextAction, setNextAction] = useState(draftString(restoredDraft, 'next_action'))
  const hasReflection = Boolean(learnedText.trim() || difficultText.trim() || nextAction.trim())
  const draft = useAutosavedDraft(workspace.session.session_id, 'reflection', {
    learned_text: learnedText,
    difficult_text: difficultText,
    next_action: nextAction
  }, hasReflection, Boolean(workspace.drafts.reflection))
  const finish = async (skipped: boolean, afterSaved?: ReflectionVoiceDetail['afterSaved']): Promise<boolean> => {
    let saved: SessionWorkspaceState | undefined
    const completed = await run(async () => {
      saved = await localApi.completeReflection(workspace.session.session_id, {
        learnedText: skipped ? '' : learnedText.trim(), difficultText: skipped ? '' : difficultText.trim(),
        feeling: '', nextAction: skipped ? '' : nextAction.trim(), skipped
      })
      return saved
    })
    if (saved) {
      reflectionSavedReceipt(saved, workspace.session.session_id, workspace.session.child_id)
      draft.markCommitted()
      // A failed metadata ACK or follow-up refresh must not turn an actual
      // successful save into a request to perform the reflection again.
      if (afterSaved) await afterSaved(saved)
      return true
    }
    if (completed) draft.markCommitted()
    return completed
  }
  useEffect(() => {
    speakOrdinaryPrompt('收尾前说三件事：今天学会了什么、哪里还不确定、明天先做什么。你可以一项一项告诉我。', workspace.session)
  }, [])
  useEffect(() => {
    const handle = (event: Event): void => {
      const detail = (event as CustomEvent<ReflectionVoiceDetail>).detail
      event.preventDefault()
      if (detail.action === 'update' && detail.field && detail.value?.trim()) {
        const field = detail.field === 'learned' ? 'learned_text'
          : detail.field === 'difficult' ? 'difficult_text'
          : detail.field === 'next_action' ? 'next_action' : null
        if (!field) { detail.resolve(false); return }
        const value = detail.value.trim()
        if (detail.field === 'learned') setLearnedText(value)
        if (detail.field === 'difficult') setDifficultText(value)
        if (detail.field === 'next_action') setNextAction(value)
        // The voice action is completed only when its complete draft is durable.
        void draft.saveNow({ learned_text: learnedText, difficult_text: difficultText,
          next_action: nextAction, [field]: value }).then(() => detail.resolve(true)).catch(() => detail.resolve(false))
        return
      }
      if (detail.action === 'complete') {
        void finish(Boolean(detail.skipped), detail.afterSaved).then(detail.resolve).catch(() => detail.resolve(false))
        return
      }
      detail.resolve(false)
    }
    window.addEventListener('hsc-reflection-voice-action', handle)
    return () => window.removeEventListener('hsc-reflection-voice-action', handle)
  }, [learnedText, difficultText, nextAction])
  return (
    <div className="reflection-layout">
      <div><span className="section-kicker">今日复盘</span><h2>用自己的话收个尾</h2></div>
      <div className="reflection-fields">
        <label>今天真正学会了什么？<textarea value={learnedText} onChange={(event) => setLearnedText(event.target.value)} /></label>
        <label>哪里还不太确定？<textarea value={difficultText} onChange={(event) => setDifficultText(event.target.value)} /></label>
        <label>明天第一件要做什么？<input value={nextAction} onChange={(event) => setNextAction(event.target.value)} /></label>
      </div>
      <DraftSaveStatus state={draft.state} restored={Boolean(workspace.drafts.reflection)} />
      <div className="step-actions"><button className="secondary-button" disabled={busy} onClick={() => void finish(true)}>跳过，直接结束</button><button className="primary-button" disabled={busy || !hasReflection} onClick={() => void finish(false)}>保存复盘并结束<Check size={18} /></button></div>
    </div>
  )
}

function SavedList({ title, empty, items }: { title: string; empty: string; items: { id: string; title: string; detail: string }[] }): React.JSX.Element {
  return (
    <section className="saved-section">
      <h3>{title}</h3>
      {items.length === 0 ? <p>{empty}</p> : items.map((item) => <div className="saved-row" key={item.id}><Check size={17} /><div><strong>{item.title}</strong><span>{item.detail}</span></div></div>)}
    </section>
  )
}

function stepIndex(state: string): number {
  const normalized = state === 'break' ? 'study' : state === 'evolving' ? 'reflection' : state
  const index = STEPS.findIndex(([candidate]) => candidate === normalized)
  return index === -1 ? 0 : index
}

function stateTitle(state: string): string {
  const labels: Record<string, string> = { check_in: '开始报到', classroom_recall: '回想课堂', homework_capture: '确认作业', plan_review: '确认计划', study: '执行计划', break: '休息一下', reflection: '今日复盘', evolving: '整理学习记录', completed: '今天完成' }
  return labels[state] ?? '今日学习'
}

function inferQuestionSubject(question: string): string {
  for (const subject of ['语文', '数学', '英语', '物理', '化学']) {
    if (question.includes(subject)) return subject
  }
  if (/(?:sin|cos|tan|三角函数|方程|几何|函数)/i.test(question)) return '数学'
  if (/(?:word|sentence|grammar|english|单词|语法|英语)/i.test(question)) return '英语'
  return '其他'
}

function formatElapsed(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}
