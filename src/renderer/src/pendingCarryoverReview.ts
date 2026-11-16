import type { DeferredTaskCandidate, DeferredTaskDecision } from './types'

export type PendingCarryoverReview = {
  version: 1
  childId: string
  sessionId: string
  sourcePlanItemId: string
  sourceVersion: string
  decision: DeferredTaskDecision['decision']
  reason: string
  requestId: string
  confirmationId?: string
  readbackReady?: boolean
}

const key = (sessionId: string): string => `hsc-pending-carryover-v1:${sessionId}`

export function readCarryoverReview(sessionId: string, childId: string): PendingCarryoverReview | null {
  const raw = localStorage.getItem(key(sessionId))
  if (!raw) return null
  const value = JSON.parse(raw) as PendingCarryoverReview
  if (!value || value.version !== 1 || value.sessionId !== sessionId || value.childId !== childId
    || !['continue_today', 'completed_elsewhere', 'cancelled'].includes(value.decision)
    || typeof value.sourcePlanItemId !== 'string' || !value.sourcePlanItemId
    || typeof value.sourceVersion !== 'string' || !/^[0-9a-f]{64}$/.test(value.sourceVersion)
    || typeof value.requestId !== 'string' || !value.requestId
    || typeof value.reason !== 'string' || !value.reason) throw new Error('延期任务恢复记录无法核对，未处理其他任务。请家长检查本机记录。')
  if (value.confirmationId !== undefined && (typeof value.confirmationId !== 'string' || !value.confirmationId
    || typeof value.readbackReady !== 'boolean')) throw new Error('延期任务重新确认状态无法核对。')
  return value
}

export function prepareCarryoverReview(scope: {sessionId: string; childId: string},
  candidate: DeferredTaskCandidate | undefined, decision: DeferredTaskDecision['decision'],
  reason: string, turnId: string, arrivalConfirmationId: string | null = null): PendingCarryoverReview {
  const pending = readCarryoverReview(scope.sessionId, scope.childId)
  if (pending) {
    if (!pending.confirmationId) {
      if (pending.decision !== decision) throw new Error('上一项延期任务的处理结果还没确认，请先重试原决定；没有处理下一项。')
      return pending
    }
    if (candidate?.source_plan_item_id !== pending.sourcePlanItemId || candidate.source_version !== pending.sourceVersion
      || arrivalConfirmationId !== pending.confirmationId || !pending.readbackReady || `${turnId}:carryover` === pending.requestId) {
      throw new Error('需要先核对原作业的新内容，再用新的一句话确认；没有处理其他作业。')
    }
  }
  if (!candidate?.source_version || !/^[0-9a-f]{64}$/.test(candidate.source_version)) {
    throw new Error('延期任务的原内容版本尚未核对，请刷新原记录后再确认。')
  }
  const value: PendingCarryoverReview = {version: 1, ...scope, sourcePlanItemId: candidate.source_plan_item_id,
    sourceVersion: candidate.source_version, decision, reason, requestId: `${turnId}:carryover`}
  // Persist only request identity, never the child's task text. Save before POST.
  localStorage.setItem(key(scope.sessionId), JSON.stringify(value))
  return value
}

export function invalidateCarryoverReview(pending: PendingCarryoverReview, details: Record<string, unknown>): DeferredTaskCandidate {
  const candidate = details.candidate as DeferredTaskCandidate | undefined
  if (details.not_applied !== true || details.session_id !== pending.sessionId || details.child_id !== pending.childId
    || details.client_action_id !== pending.requestId || candidate?.source_plan_item_id !== pending.sourcePlanItemId
    || typeof candidate.source_version !== 'string' || !/^[0-9a-f]{64}$/.test(candidate.source_version)
    || candidate.source_version === pending.sourceVersion || typeof candidate.title !== 'string'
    || typeof candidate.subject !== 'string') throw new Error('未能核对原请求未执行的回执，恢复记录仍然保留。')
  const current = readCarryoverReview(pending.sessionId, pending.childId)
  if (current?.requestId !== pending.requestId || current.sourceVersion !== pending.sourceVersion) throw new Error('原请求已经变化，未采用迟到的内容。')
  localStorage.setItem(`${key(pending.sessionId)}:invalidated:${pending.requestId}`, JSON.stringify(pending))
  localStorage.setItem(key(pending.sessionId), JSON.stringify({...pending, sourceVersion:candidate.source_version,
    confirmationId:crypto.randomUUID(), readbackReady:false}))
  return candidate
}

export function markCarryoverReadback(pending: PendingCarryoverReview): void {
  const current = readCarryoverReview(pending.sessionId, pending.childId)
  if (current?.requestId === pending.requestId && current.confirmationId === pending.confirmationId
    && current.sourceVersion === pending.sourceVersion) localStorage.setItem(key(pending.sessionId), JSON.stringify({...current, readbackReady:true}))
}

export function refreshCarryoverReadback(pending: PendingCarryoverReview, candidate: DeferredTaskCandidate | undefined): PendingCarryoverReview {
  if (!pending.confirmationId || candidate?.source_plan_item_id !== pending.sourcePlanItemId
    || typeof candidate.source_version !== 'string' || !/^[0-9a-f]{64}$/.test(candidate.source_version)) {
    throw new Error('找不到原延期作业的新内容，没有改选另一项。请先核对原记录。')
  }
  const current = readCarryoverReview(pending.sessionId, pending.childId)
  if (current?.requestId !== pending.requestId || current.confirmationId !== pending.confirmationId) throw new Error('延期任务确认状态已变化。')
  if (current.sourceVersion === candidate.source_version) return current
  const revised = {...current, sourceVersion:candidate.source_version, confirmationId:crypto.randomUUID(), readbackReady:false}
  localStorage.setItem(key(current.sessionId), JSON.stringify(revised))
  return revised
}

export function carryoverConfirmationAtArrival(sessionId: string, childId: string): string | null {
  try {
    const pending = readCarryoverReview(sessionId, childId)
    return pending?.readbackReady ? pending.confirmationId ?? null : null
  } catch { return null }
}

export function finishCarryoverReview(pending: PendingCarryoverReview,
  receipt: DeferredTaskDecision & {child_id: string; target_session_id: string}): void {
  if (!receipt || receipt.source_plan_item_id !== pending.sourcePlanItemId
    || receipt.child_id !== pending.childId || receipt.target_session_id !== pending.sessionId
    || receipt.decision !== pending.decision || receipt.reason !== pending.reason) {
    throw new Error('尚未收到原延期任务的对应回执，未处理下一项。')
  }
  const current = readCarryoverReview(pending.sessionId, pending.childId)
  if (current && (current.requestId !== pending.requestId || current.sourceVersion !== pending.sourceVersion
    || current.confirmationId)) throw new Error('这份回执不属于当前确认版本，原恢复记录仍然保留。')
  if (current?.requestId === pending.requestId && current.sourceVersion === pending.sourceVersion
    && !current.confirmationId) localStorage.removeItem(key(pending.sessionId))
}
