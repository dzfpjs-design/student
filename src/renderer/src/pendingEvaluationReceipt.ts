export type PendingEvaluationReceipt = {
  version: 1
  sessionId: string
  childId: string
  turnId: string
  status: 'in_progress' | 'unknown'
  silent: boolean
}

const key = (sessionId: string): string => `hsc-pending-evaluation-v1:${sessionId}`

export function readPendingEvaluation(sessionId: string, childId: string): PendingEvaluationReceipt | null {
  const serialized = localStorage.getItem(key(sessionId))
  if (!serialized) return null
  const value = JSON.parse(serialized) as PendingEvaluationReceipt
  if (value.version !== 1 || value.sessionId !== sessionId || value.childId !== childId
    || typeof value.turnId !== 'string' || !value.turnId
    || !['in_progress', 'unknown'].includes(value.status) || typeof value.silent !== 'boolean') {
    throw new Error('PENDING_EVALUATION_RECEIPT_INVALID')
  }
  return value
}

export function savePendingEvaluation(receipt: PendingEvaluationReceipt): void {
  // The original answer and evaluation evidence stay in SQLite, not this
  // renderer journal. This identity prevents a reload from creating a new run.
  localStorage.setItem(key(receipt.sessionId), JSON.stringify(receipt))
}

export function clearPendingEvaluation(receipt: PendingEvaluationReceipt): void {
  const current = readPendingEvaluation(receipt.sessionId, receipt.childId)
  if (current?.turnId === receipt.turnId) localStorage.removeItem(key(receipt.sessionId))
}

export function isExplicitEvaluationTopicChange(utterance: string): boolean {
  return /(?:换个?|换一?个|更换|切换|不聊这[个件道]?了|先不聊).*(?:话题|题目|事情)|^(?:换题|聊点别的|先做别的)/u.test(utterance.replace(/\s/g, ''))
}

export const EVALUATION_STATUS_POLL_DELAYS = [1000, 2000, 4000, 8000] as const
