export type ReportPlanConstraint = 'daily_total_minutes_cap' | 'task_segment_minutes_cap'
export type ReportPlanDecision = 'approve' | 'reject' | 'retract'
export type ReportPlanProposal = {
  expected_feedback_revision: number
  constraint_type: ReportPlanConstraint
  value_minutes: number
  start_date: string
  end_date: string
  client_action_id: string
}
export type ReportPlanAdjustment = ReportPlanProposal & {
  adjustment_id: string
  revision: number
  operation: 'propose' | ReportPlanDecision
  source_current: boolean
  authority?: 'explicit_parent_review' | 'retained_parent_safety_guard' | 'unapproved_parent_proposal' | 'released'
  requires_review?: boolean
  feedback_source_current?: boolean
  effective_as_guard?: boolean
  release_reason?: null | 'feedback_retracted' | 'adjustment_retracted' | 'rejected' | 'expired'
  learning_effect_verified: false
}
