import { ApiError, localApi } from './api'
import { withReceiptRecoveryWindow } from './receiptRecoveryWindow'
import type { OcrArtifact, TutorTurn } from './types'

export const OCR_TUTOR_REVIEW_MAX_AGE_MS = 24 * 60 * 60 * 1000

export type OcrTutorReview = {
  artifactId: string
  text: string
  status: 'needs_confirmation' | 'confirmed' | 'uncertain' | 'unreadable'
  createdAt: string
  confirmationId?: string
  confirmationKind?: 'ocr_verified' | 'manual_correction'
  confirmedAt?: string
  expiresAt?: string
  source?: 'full_page' | 'marked_question' | 'cloud_page'
  candidateCount?: number
}

export type OcrTextCandidate = {
  text: string
  source: 'local' | 'marked_question' | 'cloud_page'
  requiresConfirmation: boolean
}

function cloudTextCandidate(artifact: OcrArtifact): string {
  const fallback = artifact.details?.cloud_vision_fallback
  if (!fallback || !['needs_review', 'needs_confirmation'].includes(fallback.status ?? '')) return ''
  const blocks = Array.isArray(fallback.blocks) ? fallback.blocks : []
  return blocks
    .map((block) => typeof block?.text === 'string' ? block.text.trim() : '')
    .filter(Boolean)
    .join('\n')
    .slice(0, 4000)
}

export function selectOcrTextCandidate(artifact: OcrArtifact): OcrTextCandidate {
  const markedCandidates = artifact.details?.local_pipeline?.wrong_question_candidates?.filter((candidate) => candidate.text.trim()) ?? []
  const marked = markedCandidates[0]
  const markedQuestionClear = Boolean(marked && marked.confidence >= 0.72 && marked.ocr_confidence >= 0.7)
  if (marked && markedQuestionClear) {
    return { text: marked.text.trim().slice(0, 4000), source: 'marked_question', requiresConfirmation: true }
  }
  const local = artifact.extracted_text.trim().slice(0, 4000)
  const confidence = artifact.details?.ocr_confidence ?? artifact.confidence
  const localReliable = artifact.status === 'completed' && artifact.details?.quality?.passed === true && confidence >= 0.75
  const cloud = cloudTextCandidate(artifact)
  if (cloud && !localReliable) return { text: cloud, source: 'cloud_page', requiresConfirmation: true }
  return { text: local, source: 'local', requiresConfirmation: !localReliable }
}

type OcrTutorConfirmation = {
  confirmation_id: string
  confirmed_text: string
  confirmation_kind: 'ocr_verified' | 'manual_correction'
  confirmed_at: string
  expires_at: string
}

export type DocumentGoalConfirmation = { rootTurnId: string; revision: number }

export async function readOcrDocumentGoal(sessionId: string, childId: string, goal: DocumentGoalConfirmation, signal?: AbortSignal): Promise<OcrTutorReview> {
  const owner = new AbortController()
  const stop = () => owner.abort()
  signal?.addEventListener('abort', stop, { once: true })
  window.addEventListener('hsc-privacy-pause', stop)
  if (signal?.aborted) owner.abort()
  try {
    return await withReceiptRecoveryWindow(() => !owner.signal.aborted, owner.signal, window => window.read(async readSignal => {
      const result = await localApi.documentGoalReview(sessionId, goal.rootTurnId, readSignal)
      if (!window.current()) throw new DOMException('Original document read stopped', 'AbortError')
      const review = parseOcrTutorReview(result?.review, true)
      if (!review || result.phase !== 'awaiting_confirmation' || result.root_turn_id !== goal.rootTurnId
        || result.session_id !== sessionId || result.child_id !== childId || result.revision !== goal.revision
        || result.local_only !== true || result.cloud_transmission_allowed !== false || result.mastery_update_allowed !== false
        || !['needs_confirmation', 'uncertain'].includes(review.status)) {
        throw new ApiError('原题核对信息已变化，原记录仍保留，没有重新拍摄或另起讲解。', 409, 'DOCUMENT_GOAL_REVIEW_CHANGED')
      }
      return review
    }))
  } finally {
    signal?.removeEventListener('abort', stop)
    window.removeEventListener('hsc-privacy-pause', stop)
  }
}

async function runtimeConfig(): Promise<{ localApiUrl: string; sessionToken: string }> {
  const runtime = window.desktop ? await window.desktop.getRuntimeConfig() : { status: 'ready' as const, localApiUrl: 'http://127.0.0.1:8765', sessionToken: '' }
  if (runtime.status !== 'ready') throw new ApiError(runtime.error ?? '本地学习服务尚未就绪。', undefined, 'LOCAL_SERVICE_OFFLINE')
  return runtime
}

async function localRequest(path: string, body: Record<string, unknown>, signal?: AbortSignal, tutorSubmission = false): Promise<Response> {
  const runtime = await runtimeConfig()
  return fetch(`${runtime.localApiUrl}${path}`, {
    method: 'POST', signal,
    headers: { 'Content-Type': 'application/json', ...(runtime.sessionToken ? { 'X-HSC-Session': runtime.sessionToken } : {}) },
    body: JSON.stringify(body)
  }).catch((cause) => {
    if (signal?.aborted || (cause instanceof DOMException && cause.name === 'AbortError')) throw cause
    if (tutorSubmission) throw tutorResultUnconfirmed()
    throw new ApiError('本地学习服务尚未启动，请稍后重试。', undefined, 'LOCAL_SERVICE_OFFLINE')
  })
}

function tutorResultUnconfirmed(): ApiError {
  return new ApiError('讲解请求已发出，但结果尚未确认。原请求编号已保留，不会自动重新调用模型。', undefined, 'TUTOR_RESULT_UNCONFIRMED')
}

async function requireSuccess(response: Response): Promise<Response> {
  if (response.ok) return response
  const body = await response.json().catch(() => null)
  const detail = body?.detail && typeof body.detail === 'object' ? body.detail : body
  throw new ApiError(detail?.message ?? '讲解暂时不可用，请稍后重试。', response.status, detail?.code, detail?.retryable, detail?.recovery_action)
}

export function createOcrTutorReview(artifact: OcrArtifact): OcrTutorReview {
  const markedCandidates = artifact.details?.local_pipeline?.wrong_question_candidates?.filter((candidate) => candidate.text.trim()) ?? []
  const marked = markedCandidates[0]
  const candidate = selectOcrTextCandidate(artifact)
  const text = candidate.text
  const qualityPassed = artifact.status === 'completed' && artifact.details?.quality?.passed === true
  const confidence = artifact.details?.ocr_confidence ?? artifact.confidence
  const markedQuestionClear = Boolean(marked && marked.confidence >= 0.72 && marked.ocr_confidence >= 0.7)
  return {
    artifactId: artifact.artifact_id, text,
    status: !text ? 'unreadable' : candidate.source === 'cloud_page' ? 'uncertain'
      : markedQuestionClear || (qualityPassed && confidence >= 0.75) ? 'needs_confirmation' : 'uncertain',
    createdAt: new Date().toISOString(),
    source: candidate.source === 'cloud_page' ? 'cloud_page' : marked ? 'marked_question' : 'full_page',
    candidateCount: markedCandidates.length,
  }
}

function validTime(value: unknown): value is string {
  return typeof value === 'string' && Number.isFinite(Date.parse(value))
}

export function parseOcrTutorReview(value: unknown, authoritativePendingRead = false): OcrTutorReview | null {
  if (!value || typeof value !== 'object') return null
  const candidate = value as Partial<OcrTutorReview>
  if (typeof candidate.artifactId !== 'string' || !candidate.artifactId || typeof candidate.text !== 'string' || candidate.text.length > 4000) return null
  if (!['needs_confirmation', 'confirmed', 'uncertain', 'unreadable'].includes(candidate.status ?? '')) return null
  // A fresh server read validates the retained source; it is not a stale browser draft.
  if (!validTime(candidate.createdAt) || (!authoritativePendingRead && Date.now() - Date.parse(candidate.createdAt) > OCR_TUTOR_REVIEW_MAX_AGE_MS) || Date.parse(candidate.createdAt) - Date.now() > 5 * 60 * 1000) return null
  if (candidate.status === 'confirmed') {
    if (typeof candidate.confirmationId !== 'string' || !candidate.confirmationId || !validTime(candidate.confirmedAt) || !validTime(candidate.expiresAt) || Date.parse(candidate.expiresAt) <= Date.now()) return null
    if (candidate.confirmationKind !== 'ocr_verified' && candidate.confirmationKind !== 'manual_correction') return null
  }
  return candidate as OcrTutorReview
}

export function ocrTutorReviewMessage(review: OcrTutorReview): string {
  if (review.status === 'unreadable') return '没有识别到可核对的文字，请重拍。我不会猜题目。'
  if (review.source === 'cloud_page') return '本地文字不够可靠，已取得云端页内文字候选。请逐字核对题干、数字和选项；确认前不会讲解、判错或更新掌握度。'
  if (review.status === 'uncertain') return '题干需要人工修正：请逐字核对题干、数字和选项；确认后我只按你修正的文字讲解。'
  if (review.source === 'marked_question') return `我发现${review.candidateCount && review.candidateCount > 1 ? `${review.candidateCount}处` : '一处'}红色批改痕迹，先定位到这道疑似错题。请确认是不是这道题。`
  if (review.status === 'needs_confirmation') return '请先核对题干、数字和选项。确认完整后，我再结合当前任务一步一步提示。'
  return '题干已确认。我会只使用这份本地确认的题干，先给一个小提示。'
}

export async function confirmOcrTutorEvidence(sessionId: string, review: OcrTutorReview, confirmedText: string): Promise<OcrTutorReview> {
  const normalized = confirmedText.replace(/\s+/g, ' ').trim()
  const confirmationKind = review.status === 'uncertain' || normalized !== review.text.replace(/\s+/g, ' ').trim()
    ? 'manual_correction' : 'ocr_verified'
  const response = await requireSuccess(await localRequest(`/v1/sessions/${sessionId}/tutor-ocr-confirmations`, {
    artifact_id: review.artifactId, confirmed_text: normalized, confirmation_kind: confirmationKind, client_action_id: crypto.randomUUID()
  }))
  const confirmation = await response.json() as OcrTutorConfirmation
  return {
    ...review, text: confirmation.confirmed_text, status: 'confirmed', confirmationId: confirmation.confirmation_id,
    confirmationKind: confirmation.confirmation_kind, confirmedAt: confirmation.confirmed_at, expiresAt: confirmation.expires_at
  }
}

/** Prepare the original goal only; execution and speech remain owned by Agent Runtime. */
export async function prepareOcrDocumentGoal(sessionId: string, review: OcrTutorReview, goal: DocumentGoalConfirmation): Promise<void> {
  if (review.status !== 'confirmed' || !review.confirmationId || !goal.rootTurnId
    || !Number.isInteger(goal.revision) || goal.revision < 1) {
    throw new ApiError('原题确认信息不完整，没有另起讲解。', 409, 'DOCUMENT_GOAL_BINDING_MISSING')
  }
  const response = await requireSuccess(await localRequest(`/v1/sessions/${encodeURIComponent(sessionId)}/document-goals/${encodeURIComponent(goal.rootTurnId)}/confirm`, {
    confirmation_id: review.confirmationId, revision: goal.revision,
  }))
  const result = await response.json()
  if (result.status !== 'ready' || result.document_goal?.root_turn_id !== goal.rootTurnId
    || result.document_goal?.session_id !== sessionId || result.document_goal?.artifact_id !== review.artifactId
    || result.document_goal?.confirmation_id !== review.confirmationId) {
    throw new ApiError('原题讲解的接续结果尚未确认，进度仍保留，不会重复讲解。', 409, 'DOCUMENT_GOAL_RESULT_UNCONFIRMED')
  }
}

/** Sends no image and no editable OCR question: the backend resolves the durable confirmation. */
export async function askTutorWithOcrEvidence(sessionId: string, subject: string, confirmationId: string, clientActionId: string, hintLevel = 1, parentTurnId?: string, signal?: AbortSignal, confirmUnknownOutcomeRetry = false): Promise<TutorTurn> {
  const response = await requireSuccess(await localRequest(`/v1/sessions/${sessionId}/tutor`, {
    subject, question: '已确认的本地题干', hint_level: hintLevel, parent_turn_id: parentTurnId, client_action_id: clientActionId, ocr_confirmation_id: confirmationId, confirm_unknown_outcome_retry: confirmUnknownOutcomeRetry
  }, signal, true))
  const turn = await response.json().catch(() => { throw tutorResultUnconfirmed() })
  if (!turn || typeof turn.turn_id !== 'string' || !turn.turn_id || typeof turn.answer !== 'string'
    || turn.session_id !== sessionId) throw tutorResultUnconfirmed()
  return turn as TutorTurn
}
