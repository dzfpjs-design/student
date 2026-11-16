import type { PlanItem, SessionWorkspaceState } from './types'

export function schoolSupplyPromptBinding(workspace: SessionWorkspaceState, task: PlanItem | undefined): {
  diagnosticItemId: string; itemVersion: string
} | null {
  if (!task || task.source_type !== 'junior_exam_goal' || !workspace.plan) return null
  const receipts = (workspace.constraint_decisions ?? []).filter(row => row.decision === 'scheduled'
    && row.obligation_type === task.source_type && row.source_ref === task.source_ref)
    .map(row => row.metadata.supply_resume).filter(value => value && typeof value === 'object' && !Array.isArray(value))
    .map(value => value as Record<string, unknown>).filter(value => value.plan_item_id === task.plan_item_id)
  if (receipts.length !== 1) return null
  const value = receipts[0]
  if (value.version !== 'supply_gap_resume_v1' || value.operation !== 'supply'
    || value.child_id !== workspace.session.child_id || value.session_id !== workspace.session.session_id
    || value.plan_id !== workspace.plan.plan_id || value.source_type !== task.source_type || value.source_ref !== task.source_ref
    || typeof value.diagnostic_item_id !== 'string' || !value.diagnostic_item_id
    || typeof value.item_version !== 'string' || !value.item_version) return null
  return { diagnosticItemId: value.diagnostic_item_id, itemVersion: value.item_version }
}
