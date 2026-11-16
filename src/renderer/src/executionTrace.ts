export type ExecutionTraceTurn = {
  turn_id: string; created_at: string; status: string; error_code: string | null
  capability_name: string | null; trace_event_count: number
}
export type ExecutionTracePage = { items: ExecutionTraceTurn[]; next_cursor: string | null }
export type ExecutionTraceSpan = {
  event_id: string; event_type: string; source_status: string; error_code: string | null
  elapsed_ms: number; source_current: boolean; parent_link_current: boolean; recovery_action: string
}
export type ExecutionTraceDetail = {
  child_id: string; turn_id: string; spans: ExecutionTraceSpan[]
  existing_steps: { step_id: string; status: string; error_code: string | null }[]
  missing_stages: string[]; local_only: true; cloud_transmission_allowed: false
  outcome_evidence_created: false
}
