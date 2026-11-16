import { requestParentLocal } from './api'
import type { ParentReportSnapshot } from './types'

type Stamp = { snapshot_id: string; source_version: string; evidence_version: string; frozen_hash: string }
export type RebindingProof = { version: string; child_id: string; feedback_event_id: string; feedback_revision: number; experience_hash: string; historical: Stamp; target: Stamp }
export type RebindingContext = {
  binding: RebindingProof
  feedback: { snapshot_id: string; notes: string | null; helpfulness: number | null; stress_rating: number | null; created_at: string; revision: number }
  historical_report: { snapshot_id: string; content: ParentReportSnapshot['content']; parent_edit: ParentReportSnapshot['parent_edit'] }
  report: { snapshot_id: string; content: ParentReportSnapshot['content']; parent_edit: ParentReportSnapshot['parent_edit'] }
}
export type RebindingTerms = { constraint_type: 'daily_total_minutes_cap' | 'task_segment_minutes_cap'; value_minutes: number; start_date: string; end_date: string }
export type RebindingAdjustment = { adjustment_id: string; snapshot_id: string; revision: number; operation: 'propose' | 'approve' | 'reject' | 'retract'; review_hash: string; source_current: boolean; requires_review: boolean; effective_as_guard: boolean; release_reason: string | null; binding: RebindingProof; terms: RebindingTerms; evidence: RebindingContext }
export type RebindingCommand = { receipt: RebindingAdjustment; current: RebindingAdjustment } | { status: 'cancelled_not_committed' }
const root = (child: string): string => `/v1/parent/children/${encodeURIComponent(child)}/report-rebindings`
const post = <T,>(path: string, body?: unknown): Promise<T> => requestParentLocal(path, { method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
export const rebindingApi = {
  contexts: (child: string, snapshot: string): Promise<{ items: RebindingContext[] }> => requestParentLocal(`${root(child)}/contexts/${encodeURIComponent(snapshot)}`, { cache: 'no-store' }),
  list: (child: string, snapshot: string): Promise<{ items: RebindingAdjustment[] }> => requestParentLocal(`${root(child)}/snapshots/${encodeURIComponent(snapshot)}`, { cache: 'no-store' }),
  propose: (child: string, binding: RebindingProof, terms: RebindingTerms, key: string): Promise<RebindingAdjustment> => post(`${root(child)}/proposals`, { binding, terms, client_action_id: key }),
  review: (child: string, row: RebindingAdjustment, decision: 'approve' | 'reject' | 'retract', seen: boolean, key: string): Promise<RebindingAdjustment> => post(`${root(child)}/${encodeURIComponent(row.adjustment_id)}/review`, { expected_revision: row.revision, review_hash: row.review_hash, decision, evidence_seen: seen, client_action_id: key }),
  command: (child: string, key: string): Promise<RebindingCommand> => requestParentLocal(`${root(child)}/commands/${encodeURIComponent(key)}`, { cache: 'no-store' }),
  cancel: (child: string, key: string, snapshot: string): Promise<RebindingCommand> => post(`${root(child)}/commands/${encodeURIComponent(key)}/cancel`, { target_snapshot_id: snapshot }),
}

export type LocalRebinding = { version: 1; child: string; snapshot: string; terms: RebindingTerms; pending: { key: string; snapshot: string } | null }
const prefix = 'hsc:period-rebinding:'
const storageKey = (child: string): string => prefix + encodeURIComponent(child)
export function clearPeriodRebindingLocalState(child?: string): void {
  if (child !== undefined) window.localStorage.removeItem(storageKey(child))
  else for (const key of Object.keys(window.localStorage)) if (key.startsWith(prefix)) window.localStorage.removeItem(key)
}
export function readRebindingLocal(child: string): LocalRebinding | null {
  const raw = window.localStorage.getItem(storageKey(child))
  if (raw === null) return null
  const value = JSON.parse(raw) as LocalRebinding
  if (!value || value.version !== 1 || value.child !== child || typeof value.snapshot !== 'string'
      || !value.terms || !['daily_total_minutes_cap', 'task_segment_minutes_cap'].includes(value.terms.constraint_type)
      || typeof value.terms.value_minutes !== 'number' || !Number.isFinite(value.terms.value_minutes)
      || typeof value.terms.start_date !== 'string' || typeof value.terms.end_date !== 'string'
      || (value.pending !== null && (!value.pending || typeof value.pending.key !== 'string' || !value.pending.key || typeof value.pending.snapshot !== 'string'))) throw new Error('LOCAL_RECOVERY_INVALID')
  return value
}
export function writeRebindingLocal(value: LocalRebinding): void {
  const encoded = JSON.stringify(value)
  window.localStorage.setItem(storageKey(value.child), encoded)
  if (window.localStorage.getItem(storageKey(value.child)) !== encoded) throw new Error('LOCAL_RECOVERY_NOT_DURABLE')
}
