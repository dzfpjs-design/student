/** Native encrypted recovery only; never backend publication or parent-auth authority. */
export type ParentDraftRecoveryScope = 'capture' | 'language'
export type ParentDraftRecoveryPaperObjective = {
  question_number: string; full_question: string; official_answers: string[]; candidate_node_id: string | null
  paper: { capture_ref: string; source_version: string }; answer_key: { capture_ref: string; source_version: string }
  raw_editing?: { full_question: string; official_answers_text: string }
}
export type ParentDraftRecoveryEditingPayload = {
  page: Record<string, unknown>; capture_ref: string; capture_version: string
  review_drafts: Record<string, { text: string; source_version: string }>; action_ids: Record<string, string>
  objective_draft?: ParentDraftRecoveryPaperObjective | null
}
export type ParentDraftRecoveryDraftReceipt = {
  child_id: string; scope: string; storage_generation: string; revision: number
  payload: ParentDraftRecoveryEditingPayload | null; last_action_id: string | null
  last_expected_revision: number | null; updated_at: string | null; stale_refs: string[]
  cloud_transmission_allowed: false
}
export type ParentDraftRecoveryPending = {
  expected_revision: number; expected_generation: string; client_action_id: string
  payload: ParentDraftRecoveryEditingPayload | null
}
export type ParentDraftRecoverySnapshot = {
  schema: 1; childId: string; scope: ParentDraftRecoveryScope
  acknowledged: ParentDraftRecoveryDraftReceipt | null
  latest: ParentDraftRecoveryEditingPayload | null; pending: ParentDraftRecoveryPending | null
}
export type ParentDraftRecoveryPrepared = {
  leaseId: string; localRevision: number; hasRecovery: boolean; protection: 'native_encrypted'
}
export type ParentDraftRecoveryCheckpoint = { stored: true; localRevision: number; sequence: number }
export type ParentDraftRecoveryRecovered = ParentDraftRecoveryPrepared & { snapshot: ParentDraftRecoverySnapshot }
export type ParentDraftRecoveryPort = {
  prepareParentDraftRecovery(childId: string, scope: ParentDraftRecoveryScope, parentToken: string): Promise<ParentDraftRecoveryPrepared>
  readParentDraftRecovery(childId: string, scope: ParentDraftRecoveryScope, parentToken: string, expectedLocalRevision: number): Promise<ParentDraftRecoveryRecovered>
  checkpointParentDraftRecovery(leaseId: string, sequence: number, snapshot: ParentDraftRecoverySnapshot): ParentDraftRecoveryCheckpoint
  revokeParentDraftRecovery(): void
  discardParentDraftRecovery(childId: string, scope: ParentDraftRecoveryScope, parentToken: string, expectedLocalRevision: number, explicit: true): Promise<ParentDraftRecoveryPrepared>
}
