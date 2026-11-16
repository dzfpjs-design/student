/** Durable identity for a tutor request whose HTTP response may have been lost. */
export type TutorRequestRecovery = {
  version: 1
  sessionId: string
  childId: string
  actionId: string
  subject: string
  question: string
  hintLevel: number
  parentTurnId?: string
  ocrConfirmationId?: string
  createdAt: string
}

export const TUTOR_REQUEST_RECOVERY_MAX_AGE_MS = 24 * 60 * 60 * 1000
const FUTURE_SKEW_MS = 5 * 60 * 1000

export function parseTutorRequestRecovery(value: unknown, sessionId: string, childId: string): TutorRequestRecovery | null {
  if (!value || typeof value !== 'object') return null
  const candidate = value as Partial<TutorRequestRecovery>
  if (candidate.version !== 1 || candidate.sessionId !== sessionId || candidate.childId !== childId) return null
  if (typeof candidate.actionId !== 'string' || candidate.actionId.length < 8 || typeof candidate.subject !== 'string' || !candidate.subject.trim() || candidate.subject.length > 40) return null
  if (typeof candidate.question !== 'string' || candidate.question.trim().length < 2 || candidate.question.length > 4000) return null
  if (!Number.isInteger(candidate.hintLevel) || candidate.hintLevel! < 1 || candidate.hintLevel! > 6) return null
  if (candidate.parentTurnId !== undefined && (typeof candidate.parentTurnId !== 'string' || !candidate.parentTurnId || candidate.parentTurnId.length > 120)) return null
  if (candidate.ocrConfirmationId !== undefined && (typeof candidate.ocrConfirmationId !== 'string' || !candidate.ocrConfirmationId || candidate.ocrConfirmationId.length > 120)) return null
  if (typeof candidate.createdAt !== 'string') return null
  const createdAtMs = Date.parse(candidate.createdAt)
  if (!Number.isFinite(createdAtMs) || createdAtMs < Date.now() - TUTOR_REQUEST_RECOVERY_MAX_AGE_MS || createdAtMs > Date.now() + FUTURE_SKEW_MS) return null
  return candidate as TutorRequestRecovery
}
