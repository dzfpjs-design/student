import { requestParentLocal } from './api'

export type RevisionSource = {
  source_id: string; child_id: string; title: string; review_status: string;
  content_hash: string; current_version: number; predecessor_id: string | null
}
export type SourceRevisionView = {
  current: RevisionSource; source_version: string;
  stored_excerpts: Array<{ text: string; sequence: number }>;
  history: Array<{ event_id: string; action: string; occurred_at: string; new_source_id: string }>;
  purpose_permission_inherited: false
}
export type SourceRevisionPreview = {
  source: RevisionSource; source_version: string; status: 'complete' | 'incomplete';
  stored_excerpts: Array<{ text: string; sequence: number }>;
  replacement_text: string; replacement_length: number; stored_length: number
}
export type SourceRevisionCommand = {
  sourceId: string; operation: 'revise' | 'review';
  body: { expected_version: string; client_action_id: string; confirmation: string; content_text?: string }
}
export type SourceRevisionJournal = {
  schema: 1; childId: string; sourceId: string; text: string; pending: SourceRevisionCommand | null
}
const prefix = 'hsc:source-revision:'
export const sourceRevisionCleared = 'hsc:source-revision-cleared'
export function clearSourceRevisionLocalState(childId?: string): void {
  for (const key of Object.keys(localStorage)) {
    if (childId ? key === prefix + childId : key.startsWith(prefix)) localStorage.removeItem(key)
  }
  window.dispatchEvent(new CustomEvent(sourceRevisionCleared, { detail: { childId } }))
}
export function readSourceRevisionJournal(childId: string): SourceRevisionJournal | null {
  const raw = localStorage.getItem(prefix + childId)
  if (!raw) return null
  const data = JSON.parse(raw) as SourceRevisionJournal
  if (data.schema !== 1 || data.childId !== childId || typeof data.sourceId !== 'string'
    || typeof data.text !== 'string' || data.text.length > 100000
    || (data.pending !== null && (typeof data.pending !== 'object'
      || !['revise', 'review'].includes(data.pending.operation)
      || typeof data.pending.sourceId !== 'string' || typeof data.pending.body !== 'object'
      || typeof data.pending.body.client_action_id !== 'string'
      || !/^[a-f0-9]{64}$/.test(data.pending.body.expected_version)
      || typeof data.pending.body.confirmation !== 'string'
      || (data.pending.operation === 'revise' && typeof data.pending.body.content_text !== 'string')))) {
    throw new Error('修订恢复记录损坏，未发送任何操作。请核对服务端资料后再放弃本机记录。')
  }
  return data
}
export function saveSourceRevisionJournal(value: SourceRevisionJournal): void {
  localStorage.setItem(prefix + value.childId, JSON.stringify(value))
}
export const sourceRevisionPath = (child: string, source: string): string =>
  `/v1/parent/children/${encodeURIComponent(child)}/knowledge-sources/${encodeURIComponent(source)}/revision`
export const sourceRevisionRequest = requestParentLocal
