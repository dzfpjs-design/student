import { parseLocalCourseRecoveryContext, type LocalCourseRecoveryContext } from './voiceJourneyRuntime'

/**
 * Durable, child-facing recovery metadata for a *read-only* tutor request.
 *
 * A failed request must never be replayed as a generic mutation: the original
 * HTTP result may have been lost after the server applied it.  Tutor retries
 * are kept separate because they only request another explanation; all plan,
 * homework and completion changes require a fresh, visible choice in their
 * owning screen.
 */
export type TutorToolRecovery = {
  version: 1
  turnId: string
  actionId: string
  subject: string
  question: string
  hintLevel?: number
  parentTurnId?: string
  followupSourceTurnId?: string
  ocrConfirmationId?: string
  questionReference?: { prompt_id: string; item_version: string }
  sourceBlocked?: 'changed' | 'local_only' | 'foreground_reply'
  actionType?: 'ask_tutor' | 'more_tutor_hint'
  createdAt: string
  attempts: number
  localDiagnosticPrompt?: string
  atomicAuditEventId?: string
  courseContext?: LocalCourseRecoveryContext
  /** Only the explicit recovery card may set this retry acknowledgement. */
  requiresExplicitRetryConfirmation?: boolean
}

/** A short-lived service outage should be recoverable, not become a permanent prompt. */
export const TUTOR_RECOVERY_MAX_AGE_MS = 24 * 60 * 60 * 1000
const MAX_FUTURE_SKEW_MS = 5 * 60 * 1000

export function parseTutorToolRecovery(value: unknown): TutorToolRecovery | null {
  if (!value || typeof value !== 'object') return null
  const candidate = value as Partial<TutorToolRecovery>
  if (candidate.version !== 1 || typeof candidate.turnId !== 'string' || typeof candidate.actionId !== 'string' || typeof candidate.subject !== 'string' || typeof candidate.question !== 'string') return null
  if (!candidate.turnId || candidate.actionId.length < 8 || !candidate.subject.trim() || candidate.subject.length > 40 || !candidate.question.trim() || candidate.question.length > 4000) return null
  if (typeof candidate.createdAt !== 'string' || typeof candidate.attempts !== 'number' || !Number.isInteger(candidate.attempts) || candidate.attempts < 1 || candidate.attempts > 3) return null
  if (candidate.localDiagnosticPrompt !== undefined && (typeof candidate.localDiagnosticPrompt !== 'string' || candidate.localDiagnosticPrompt.length > 240)) return null
  if (candidate.parentTurnId !== undefined && (typeof candidate.parentTurnId !== 'string' || !candidate.parentTurnId || candidate.parentTurnId.length > 120)) return null
  if (candidate.ocrConfirmationId !== undefined && (typeof candidate.ocrConfirmationId !== 'string'
    || !candidate.ocrConfirmationId.trim() || candidate.ocrConfirmationId.length > 120
    || candidate.questionReference !== undefined)) return null
  if (candidate.questionReference !== undefined && (!candidate.questionReference ||
      typeof candidate.questionReference !== 'object' || ['prompt_id', 'item_version'].some(key => {
        const value = candidate.questionReference?.[key as 'prompt_id' | 'item_version']
        return typeof value !== 'string' || !value.trim() || value.length > 120
      }))) return null
  if (candidate.actionType !== undefined && !['ask_tutor', 'more_tutor_hint'].includes(candidate.actionType)) return null
  if (candidate.sourceBlocked !== undefined && !['changed', 'local_only', 'foreground_reply'].includes(candidate.sourceBlocked)) return null
  if (candidate.actionType === 'more_tutor_hint' && !candidate.parentTurnId) return null
  if (candidate.followupSourceTurnId !== undefined && (typeof candidate.followupSourceTurnId !== 'string' || !candidate.followupSourceTurnId.trim() || candidate.followupSourceTurnId.length > 120 || candidate.parentTurnId || candidate.ocrConfirmationId || candidate.questionReference)) return null
  if (candidate.atomicAuditEventId !== undefined && (typeof candidate.atomicAuditEventId !== 'string' || !candidate.atomicAuditEventId.startsWith('atomic-context-') || candidate.atomicAuditEventId.length > 120)) return null
  const courseContext = candidate.courseContext === undefined ? undefined : parseLocalCourseRecoveryContext(candidate.courseContext)
  if (courseContext === null) return null
  const createdAtMs = Date.parse(candidate.createdAt)
  if (!Number.isFinite(createdAtMs) || createdAtMs < 0 || createdAtMs < Date.now() - TUTOR_RECOVERY_MAX_AGE_MS || createdAtMs > Date.now() + MAX_FUTURE_SKEW_MS) return null
  return {
    version: 1,
    turnId: candidate.turnId,
    actionId: candidate.actionId,
    subject: candidate.subject,
    question: candidate.question,
    ...(candidate.parentTurnId ? { parentTurnId: candidate.parentTurnId } : {}),
    ...(candidate.followupSourceTurnId ? { followupSourceTurnId: candidate.followupSourceTurnId } : {}),
    ...(candidate.ocrConfirmationId ? { ocrConfirmationId: candidate.ocrConfirmationId } : {}),
    ...(candidate.questionReference ? { questionReference: {
      prompt_id: candidate.questionReference.prompt_id, item_version: candidate.questionReference.item_version
    } } : {}),
    ...(candidate.actionType ? { actionType: candidate.actionType } : {}),
    ...(candidate.sourceBlocked ? { sourceBlocked: candidate.sourceBlocked } : {}),
    hintLevel: Number.isInteger(candidate.hintLevel) && candidate.hintLevel! >= 1 && candidate.hintLevel! <= 6 ? candidate.hintLevel : 1,
    createdAt: candidate.createdAt,
    attempts: candidate.attempts,
    ...(candidate.localDiagnosticPrompt ? { localDiagnosticPrompt: candidate.localDiagnosticPrompt } : {}),
    ...(candidate.atomicAuditEventId ? { atomicAuditEventId: candidate.atomicAuditEventId } : {}),
    ...(courseContext ? { courseContext } : {}),
    requiresExplicitRetryConfirmation: !candidate.sourceBlocked && candidate.requiresExplicitRetryConfirmation === true
  }
}

export function tutorSourceBlock(code?: string): TutorToolRecovery['sourceBlocked'] {
  if (code === 'TUTOR_FOLLOWUP_SOURCE_UNAVAILABLE') return 'changed'
  if (code === 'FOREGROUND_TUTOR_REPLY_SCOPE_CHANGED') return 'foreground_reply'
  return ['TUTOR_QUESTION_REFERENCE_CHANGED', 'OCR_SOURCE_INVALIDATED', 'OCR_CONFIRMATION_NOT_FOUND',
    'OCR_CONFIRMATION_SESSION_MISMATCH', 'OCR_CONFIRMATION_EXPIRED', 'OCR_CONFIRMATION_ARTIFACT_INVALID',
    'OCR_CONFIRMATION_CHILD_MISMATCH'].includes(code ?? '') ? 'changed'
    : code === 'TUTOR_QUESTION_LOCAL_ONLY' || code === 'SENSITIVE_TEXT_LOCAL_ONLY' ? 'local_only' : undefined
}

export function tutorSourceBlockMessage(reason: TutorToolRecovery['sourceBlocked']): string {
  if (reason === 'foreground_reply') return '这次独立讲解试图发起另一道作答，已经停止。原题和问题都保留着，可以继续原题，或另问一个问题。'
  return reason === 'changed' ? '刚才那道题的内容或来源已经变化，我不能沿用旧题继续讲。请把现在要问的题重新告诉我，或继续当前任务。'
    : '这道题的来源限定在本机使用，这次没有送给云端讲解。可以继续当前任务，或请家长核对题目来源。'
}

export function tutorFailureMessage(attempts: number): string {
  return attempts >= 2
    ? '这次讲解还是没有连上。可以请家长到首页的家长中心查看本机服务；你的问题已经保留在这里。'
    : '这次讲解暂时没有完成。你可以说“重试讲解”；如果还是不行，请家长帮忙。'
}

export function parentHelpMessage(): string {
  return '请叫家长帮忙：先回到首页，再由家长打开“家长中心”查看本机服务。你的学习记录和问题会保留，不需要重新输入。'
}
