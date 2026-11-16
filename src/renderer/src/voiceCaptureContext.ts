import type { DiagnosticPromptResult, DocumentPageRevision } from './types'
import { carryoverConfirmationAtArrival } from './pendingCarryoverReview'

// Local capture metadata, deliberately separate from the ASR wire reference.
export function voiceCarryoverConfirmationCapture(sessionId?: string, childId?: string): string | null {
  return sessionId && childId ? carryoverConfirmationAtArrival(sessionId, childId) : null
}

export type MistakeVoiceBinding = {
  session_id: string; child_id: string; phase: string; item_id: string; source_version: string
} & ({ mistake_id: string; candidate_id?: never; artifact_id?: never }
  | { mistake_id: null; candidate_id: string; artifact_id: string })
export type LearningVoiceBinding = {
  objective_id: string; plan_id: string; source_agent_turn_id: string
  capability_name: 'learning.explain' | 'learning.evaluate_response' | 'learning.request_response'; phase: string; source_version: string
}
export type PendingLearningCapture = { version: 'ordinary_learning_capture_v1';
  session_id: string; child_id: string; reference: { kind: 'learning_reply'; binding: LearningVoiceBinding } }
export type VoiceQuestionReference =
  | { kind: 'learning_reply'; binding: LearningVoiceBinding }
  | { kind: 'diagnostic'; binding: { prompt_id: string; item_version: string } }
  | { kind: 'diagnostic_help'; binding: { prompt_id: string; item_version: string } }
  | { kind: 'exam'; binding: { run_id: string; position: number; item_id: string; source_version: string } }
  | { kind: 'vocabulary'; binding: { prompt_session_id: string; card_version: string } }
  | { kind: 'mistake'; binding: MistakeVoiceBinding }
  | { kind: 'document'; binding: { revision_id: string; artifact_id: string; content_sha256: string; page_sha256: string } }
export type VoiceCaptureReference = {
  child_id: string
  mistake_learning?: import('./mistakeLearningVoice').MistakeLearningCapture
  question_reference?: VoiceQuestionReference | null
  activity_reference?: VoiceActivityReference
  task_choice_reference?: VoiceTaskChoiceReference
  pending_evaluation_turn_id?: string
  local_recovery_only?: true
}
export type VoiceActivityReference = { activity_id: string; version: string; last_turn_id: string }
export type VoiceTaskChoiceReference = { offer_turn_id: string; source_version: string }
export type TaskStartChoiceResult = { session_id: string; child_id: string; reference: VoiceTaskChoiceReference | null }
const taskChoices = new Map<string, { epoch: number; childId: string; reference: VoiceTaskChoiceReference | null }>()
const learningCaptures = new Map<string, PendingLearningCapture>()
const learningReads = new Map<string, symbol>()
const captureListeners = new Set<() => void>()
const originalAudioPrompts = new Map<string, { childId: string; promptId: string; itemVersion: string }>()
const notifyCaptureContext = (): void => { for (const listener of captureListeners) listener() }
export function subscribeVoiceCaptureContext(listener: () => void): () => void {
  captureListeners.add(listener)
  return () => { captureListeners.delete(listener) }
}

// A recorder choice only. The server still owns source, retention and scoring permission.
export function originalAudioCaptureRequired(sessionId: string, childId: string): boolean {
  const hint = originalAudioPrompts.get(sessionId), current = contexts.get(sessionId)?.reference
  const question = current?.question_reference
  return Boolean(hint?.childId === childId && current?.child_id === childId
    && question?.kind === 'diagnostic' && question.binding.prompt_id === hint.promptId
    && question.binding.item_version === hint.itemVersion && !current.activity_reference
    && pendingEvaluations.get(sessionId)?.childId !== childId && localRecoveries.get(sessionId) !== childId
    && !(taskChoices.get(sessionId)?.childId === childId && taskChoices.get(sessionId)?.reference))
}

export function publishOriginalAudioCaptureHint(sessionId: string, result: DiagnosticPromptResult): void {
  const prompt = result.prompt, current = contexts.get(sessionId)?.reference, question = current?.question_reference
  if (result.status === 'waiting_answer' && !result.reason && prompt?.origin_session_id === sessionId
    && current?.child_id === prompt.child_id && question?.kind === 'diagnostic'
    && question.binding.prompt_id === prompt.prompt_id && question.binding.item_version === prompt.item_version
    && prompt.item?.track === 'pet' && prompt.item.assessment_mode === 'pet_reference_rubric'
    && ['speaking', 'read_aloud'].includes(prompt.item.answer_type)) {
    originalAudioPrompts.set(sessionId, { childId: prompt.child_id, promptId: prompt.prompt_id, itemVersion: prompt.item_version })
  } else originalAudioPrompts.delete(sessionId)
  notifyCaptureContext()
}
export function setVoiceTaskChoiceCapture(sessionId: string, childId: string, reference: VoiceTaskChoiceReference | null): void {
  taskChoices.set(sessionId, { epoch: (taskChoices.get(sessionId)?.epoch ?? 0) + 1, childId,
    reference: reference ? structuredClone(reference) : null })
  notifyCaptureContext()
}
export async function trackTaskChoiceCapture(sessionId: string, childId: string, load: () => Promise<TaskStartChoiceResult>): Promise<TaskStartChoiceResult> {
  setVoiceTaskChoiceCapture(sessionId, childId, null)
  const epoch = taskChoices.get(sessionId)!.epoch
  const result = await load()
  if (taskChoices.get(sessionId)?.epoch === epoch && result.session_id === sessionId && result.child_id === childId) {
    setVoiceTaskChoiceCapture(sessionId, childId, result.reference)
    return result
  }
  return { ...result, reference: null }
}
const contexts = new Map<string, { epoch: number; activityEpoch: number; reference: VoiceCaptureReference | null }>()
const pendingEvaluations = new Map<string, { childId: string; turnId: string }>()
const localRecoveries = new Map<string, string>()
const documents = new Map<string, { childId: string; owner: symbol; question: Extract<VoiceQuestionReference, { kind: 'document' }> }>()

// A background page is a fallback, never the foreground answer/activity owner.
export function publishVoiceDocumentCapture(sessionId: string, childId: string, page: DocumentPageRevision): symbol | undefined {
  const source = page.page_source
  const digest = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
  if (page.session_id !== sessionId || page.child_id !== childId || !page.revision_id || !page.artifact_id
      || !digest(page.content_sha256) || !page.extracted_text?.trim() || !source
      || source.version !== 'document_page_source_v1' || source.status !== 'retained'
      || !digest(source.sha256) || !Number.isSafeInteger(source.byte_length) || source.byte_length <= 0
      || !['image/jpeg', 'image/png'].includes(source.media_type) || source.encrypted !== true
      || source.local_only !== true || source.cloud_transmission_allowed !== false
      || source.retention_reason !== 'accepted_document_page_revision') return undefined
  const owner = Symbol('document-voice-source')
  documents.set(sessionId, { childId, owner, question: { kind: 'document', binding: {
    revision_id: page.revision_id, artifact_id: page.artifact_id,
    content_sha256: page.content_sha256, page_sha256: source.sha256,
  } } })
  return owner
}

export function releaseVoiceDocumentCapture(sessionId: string, childId: string, owner: symbol): void {
  const current = documents.get(sessionId)
  if (current?.owner === owner && current.childId === childId) documents.delete(sessionId)
}
export function setVoiceLocalRecoveryCapture(sessionId: string, childId: string, enabled: boolean): void {
  if (enabled) localRecoveries.set(sessionId, childId)
  else if (localRecoveries.get(sessionId) === childId) localRecoveries.delete(sessionId)
  notifyCaptureContext()
}
export function setVoicePendingEvaluationCapture(sessionId: string, childId: string, turnId: string | null): void {
  if (turnId) pendingEvaluations.set(sessionId, { childId, turnId })
  else if (pendingEvaluations.get(sessionId)?.childId === childId) pendingEvaluations.delete(sessionId)
  notifyCaptureContext()
}
export function voiceActivityCaptureEpoch(sessionId: string): number { return contexts.get(sessionId)?.activityEpoch ?? 0 }

export async function trackDiagnosticCapture(
  sessionId: string, load: () => Promise<DiagnosticPromptResult>, replacing = false,
): Promise<DiagnosticPromptResult> {
  const old = contexts.get(sessionId)
  // A background read may inspect a prompt, but cannot take another activity's capture ownership.
  if (!replacing && old?.reference?.question_reference?.kind
    && !['diagnostic', 'diagnostic_help'].includes(old.reference.question_reference.kind)) return load()
  const epoch = (old?.epoch ?? 0) + 1
  contexts.set(sessionId, { epoch, activityEpoch: old?.activityEpoch ?? 0, reference: replacing && old?.reference
    ? { ...old.reference, question_reference: null } : old?.reference ?? null })
  notifyCaptureContext()
  const result = await load()
  const latest = contexts.get(sessionId)
  if (latest?.epoch !== epoch) return result
  if (result.status === 'awaiting_task_start') {
    originalAudioPrompts.delete(sessionId)
    const restored = old?.reference ? { ...old.reference } : null
    if (restored) {
      delete restored.activity_reference
      if (latest.reference?.child_id === restored.child_id && latest.reference.activity_reference) {
        restored.activity_reference = latest.reference.activity_reference
      }
    }
    contexts.set(sessionId, { epoch, activityEpoch: latest.activityEpoch,
      reference: restored ?? (latest.reference ? { ...latest.reference, question_reference: null } : null) })
    if (result.prompt) clearDiagnosticCaptureForPrompt(sessionId, result.prompt)
    notifyCaptureContext()
    return result
  }
  const scored = result.status === 'answered' && !result.reason && result.prompt
    && result.attempt && result.attempt.diagnostic_item_id === result.prompt.diagnostic_item_id
    && result.attempt.child_id === result.prompt.child_id && result.attempt.session_id === sessionId
  const answered = scored && result.prompt!.item?.track === 'pet'
    && result.prompt!.item.assessment_mode === 'pet_reference_rubric'
    && ['speaking', 'read_aloud', 'writing'].includes(result.prompt!.item.answer_type)
  // An answered oral/written prompt identifies a subsequent control's subject.
  // It is not a pending answer or permission to reuse an earlier original.
  const previous = old?.reference?.question_reference
  if (scored && !replacing && previous && ['diagnostic', 'diagnostic_help'].includes(previous.kind)
    && 'prompt_id' in previous.binding
    && previous.binding.prompt_id !== result.prompt?.prompt_id) return result
  const prompt = result.status === 'waiting_answer' || scored ? result.prompt : null
  if (prompt) setVoiceTaskChoiceCapture(sessionId, prompt.child_id, null)
  contexts.set(sessionId, { epoch, activityEpoch: latest.activityEpoch, reference: prompt?.origin_session_id === sessionId ? {
    child_id: prompt.child_id,
    question_reference: { kind: scored && !answered ? 'diagnostic_help' : 'diagnostic',
      binding: { prompt_id: prompt.prompt_id, item_version: prompt.item_version } },
    ...(latest.reference?.child_id === prompt.child_id && latest.reference.activity_reference
      ? { activity_reference: latest.reference.activity_reference } : {}),
  } : latest.reference ? { ...latest.reference, question_reference: null } : null })
  publishOriginalAudioCaptureHint(sessionId, result)
  return result
}

function learningCapture(value: unknown, sessionId: string, childId: string): PendingLearningCapture | null {
  if (!value || typeof value !== 'object') return null
  const v = value as PendingLearningCapture
  const keys = (o: object): string => Object.keys(o).sort().join(',')
  if (keys(v) !== 'child_id,reference,session_id,version' || v.version !== 'ordinary_learning_capture_v1'
    || v.session_id !== sessionId || v.child_id !== childId || !v.reference
    || keys(v.reference) !== 'binding,kind' || v.reference.kind !== 'learning_reply') return null
  const b = v.reference.binding
  if (!b || keys(b) !== 'capability_name,objective_id,phase,plan_id,source_agent_turn_id,source_version'
    || !['learning.explain', 'learning.evaluate_response', 'learning.request_response'].includes(b.capability_name)
    || Object.values(b).some(x => typeof x !== 'string' || !x.trim() || x.length > 200)
    || !/^[0-9a-f]{64}$/.test(b.source_version)) return null
  return structuredClone(v)
}

/** Existing runtime GET only. A hint is never rebound onto an already captured onset. */
export async function trackVoiceLearningCapture<T extends { session_id: string; pending_learning_capture?: unknown }>(
  sessionId: string, childId: string, load: () => Promise<T>, isCurrent: () => boolean,
): Promise<{ current: boolean; status: T | null }> {
  const request = Symbol('learning-read')
  learningReads.set(sessionId, request)
  let status = await load().catch(() => null)
  if (learningReads.get(sessionId) !== request || !isCurrent()) return { current: false, status }
  if (status?.session_id !== sessionId) status = null
  const capture = learningCapture(status?.pending_learning_capture, sessionId, childId)
  if (status?.pending_learning_capture && !capture) status = null
  const before = learningCaptures.get(sessionId)
  const same = capture === null ? before === undefined : !!before
    && before.session_id === capture.session_id && before.child_id === capture.child_id
    && Object.entries(capture.reference.binding).every(([key, value]) =>
      before.reference.binding[key as keyof LearningVoiceBinding] === value)
  if (!same) {
    if (capture) learningCaptures.set(sessionId, capture)
    else learningCaptures.delete(sessionId)
    notifyCaptureContext()
  }
  return { current: true, status }
}

// Copy at capture, not when an earlier ASR request finishes draining the FIFO.
export function voiceCaptureReference(sessionId: string, childId: string): VoiceCaptureReference {
  if (localRecoveries.get(sessionId) === childId) return { child_id: childId, local_recovery_only: true }
  const current = contexts.get(sessionId)?.reference
  const pending = pendingEvaluations.get(sessionId)
  const choice = taskChoices.get(sessionId)
  const document = documents.get(sessionId)
  const learning = learningCaptures.get(sessionId)
  if (choice?.childId === childId && choice.reference && pending?.childId !== childId) {
    return { child_id: childId, task_choice_reference: structuredClone(choice.reference) }
  }
  const question = current?.child_id === childId && current.question_reference ? current.question_reference
    : pending?.childId !== childId && !(current?.child_id === childId && current.activity_reference)
      ? learning?.child_id === childId ? learning.reference
        : document?.childId === childId ? document.question : null : null
  return { child_id: childId, question_reference: question ? structuredClone(question) : null,
    ...(current?.child_id === childId && current.activity_reference
      ? { activity_reference: structuredClone(current.activity_reference) } : {}),
    ...(pending?.childId === childId ? { pending_evaluation_turn_id: pending.turnId } : {}) }
}

export function setVoiceQuestionCapture(sessionId: string, childId: string, question: VoiceQuestionReference | null): void {
  if (question) setVoiceTaskChoiceCapture(sessionId, childId, null)
  const old = contexts.get(sessionId)
  contexts.set(sessionId, { epoch: (old?.epoch ?? 0) + 1, activityEpoch: old?.activityEpoch ?? 0,
    reference: { child_id: childId, question_reference: question ? structuredClone(question) : null,
      ...(old?.reference?.child_id === childId && old.reference.activity_reference
        ? { activity_reference: old.reference.activity_reference } : {}) } })
  const hint = originalAudioPrompts.get(sessionId)
  if (hint && !(hint.childId === childId && question?.kind === 'diagnostic'
    && question.binding.prompt_id === hint.promptId && question.binding.item_version === hint.itemVersion)) {
    originalAudioPrompts.delete(sessionId)
  }
  notifyCaptureContext()
}

// A transient source stamp; never replace the complete server reference with this value.
export function voiceCaptureScopeStamp(sessionId:string,childId:string):{value:string;documentOwner:symbol|undefined} {
  const context=contexts.get(sessionId),choice=taskChoices.get(sessionId),page=documents.get(sessionId)
  return {value:JSON.stringify({childId,question:context?.epoch??0,activity:context?.activityEpoch??0,
    choice:choice?.childId===childId?choice.epoch:0,reference:voiceCaptureReference(sessionId,childId)}),
    documentOwner:page?.childId===childId?page.owner:undefined}
}
export function voiceQuestionCaptureEpoch(sessionId: string): number { return contexts.get(sessionId)?.epoch ?? 0 }

// Release a view's binding only while it still owns this capture generation.
export function releaseVoiceQuestionCapture(sessionId: string, childId: string, epoch: number): void {
  const current = contexts.get(sessionId)
  if (current?.epoch === epoch && current.reference?.child_id === childId) setVoiceQuestionCapture(sessionId, childId, null)
}

export function setVoiceActivityCapture(sessionId: string, childId: string, activity: VoiceActivityReference | null): void {
  const old = contexts.get(sessionId)
  contexts.set(sessionId, { epoch: old?.epoch ?? 0, activityEpoch: (old?.activityEpoch ?? 0) + 1, reference: {
    child_id: childId,
    question_reference: old?.reference?.child_id === childId ? old.reference.question_reference ?? null : null,
    ...(activity ? { activity_reference: structuredClone(activity) } : {}),
  } })
  notifyCaptureContext()
}

/** A successful focus refresh may preserve only its own unchanged registration.
 * Explicit setVoiceActivityCapture always remains a new capture generation. */
export function synchronizeVoiceActivityCapture(sessionId: string, childId: string,
  activity: VoiceActivityReference | null, expectedActivityEpoch: number | null): void {
  const old = contexts.get(sessionId), previous = old?.reference?.activity_reference
  const sameFocus = activity === null ? previous === undefined : !!previous
    && Object.keys(previous).sort().join(',') === 'activity_id,last_turn_id,version'
    && previous.activity_id === activity.activity_id && previous.version === activity.version
    && previous.last_turn_id === activity.last_turn_id
  if (expectedActivityEpoch !== null && old?.activityEpoch === expectedActivityEpoch
    && old.reference?.child_id === childId && sameFocus) return
  setVoiceActivityCapture(sessionId, childId, activity)
}

export function clearDiagnosticCaptureForPrompt(sessionId: string, prompt: NonNullable<DiagnosticPromptResult['prompt']>): void {
  const reference = contexts.get(sessionId)?.reference
  const question = reference?.question_reference
  if (prompt.origin_session_id === sessionId && reference?.child_id === prompt.child_id
    && question?.kind === 'diagnostic' && question.binding.prompt_id === prompt.prompt_id
    && question.binding.item_version === prompt.item_version) {
    setVoiceQuestionCapture(sessionId, prompt.child_id, null)
  }
}

export function clearVoiceCaptureContext(sessionId?: string): void {
  if (sessionId) {
    learningReads.delete(sessionId)
    learningCaptures.delete(sessionId)
    originalAudioPrompts.delete(sessionId)
    documents.delete(sessionId)
    setVoiceTaskChoiceCapture(sessionId, taskChoices.get(sessionId)?.childId ?? '', null)
    localRecoveries.delete(sessionId)
    pendingEvaluations.delete(sessionId)
    const old = contexts.get(sessionId)
    contexts.set(sessionId, { epoch: (old?.epoch ?? 0) + 1, activityEpoch: (old?.activityEpoch ?? 0) + 1, reference: null })
    notifyCaptureContext()
  } else {
    const sessions = new Set([...contexts.keys(), ...learningCaptures.keys(), ...learningReads.keys()])
    learningReads.clear()
    learningCaptures.clear()
    originalAudioPrompts.clear()
    documents.clear()
    for (const key of taskChoices.keys()) setVoiceTaskChoiceCapture(key, '', null)
    localRecoveries.clear()
    pendingEvaluations.clear()
    for (const key of sessions) clearVoiceCaptureContext(key)
  }
}
