export type GoalBaselineStatus = 'needs_baseline' | 'stale_recheck' | 'recheck_needed' | 'supported_only' | 'independent_observed' | 'retention_observed'
export type GoalRoadmapActionKind = 'establish_baseline' | 'refresh_baseline' | 'teach_then_check' | 'independent_check' | 'delayed_check' | 'transfer_check' | 'clarify_then_recheck' | 'fade_hint_then_check' | 'different_item_check'

export interface GoalRoadmapBaseline {
  knowledge_node_id: string
  baseline_status: GoalBaselineStatus
  feedback_status: string | null
  evidence_refs: string[]
  latest_evidence_at: string | null
  prerequisite_observed: boolean
  mastery_claim_allowed: false
}

export interface GoalRoadmapStep {
  work_id: string
  action: GoalRoadmapActionKind
  estimated_minutes: { min: number; max: number }
  status: 'planned'
  estimate_only: true
  minimum_gap_hours: number
  completion_basis: string
}

export interface GoalRoadmapNode extends GoalRoadmapBaseline {
  title: string
  subject: string
  prerequisite_node_ids: string[]
  blocked_by: string[]
  source_ref: string
  knowledge_version: string
  steps: GoalRoadmapStep[]
}

export interface GoalRoadmapAction {
  action: GoalRoadmapActionKind
  knowledge_node_id: string
  child_id: string
  program_id: string
  subject: string
  title: string
  minutes: number
  source_type: string
  source_ref: string
  evidence_refs: string[]
  requires_source_selection: true
  dispatchable: boolean
  status: 'suggested' | 'qualified_source_needed'
  completion_standard: string
  mastery_claim_allowed: false
  cloud_transmission_allowed: false
  roadmap_source_version: string
}

export interface GoalRoadmapMilestone {
  target_date: string
  work_ids: string[]
  estimated_minutes: number
  status: 'planned'
  estimate_only: true
}

export interface GoalRoadmap {
  section_evidence?: import('./GoalSectionEvidence.types').GoalSectionEvidence
  exam_rules?: {
    version: 'official_goal_rules_v1'
    public_rule_status: string
    binding_status: string
    target_year: number | null
    reference_year: number | null
    registration_status: 'not_verified'
    coverage_status: string
    current_snapshot: { snapshot_hash?: string; reference: { authority: string; url: string; verified_on: string } } | null
    required_sections: Array<{
      section_id: string; title: string; blueprint_id: string; duration_minutes: number
      item_target?: number; item_target_unit?: string
      duration_variants?: import('./examRuleDisplay').ExamDurationVariant[]
      operational_duration_basis?: string | null
    }>
    exam_readiness_claim_allowed: false
  }
  version: 'goal_roadmap_v1'
  source_version: string
  program_id: string
  child_id: string
  track: 'pet_path' | 'competition_math_path' | 'junior_exam_readiness'
  title: string
  as_of_date: string
  reference_date: string
  target_date: string | null
  status: 'paused' | 'needs_clarification' | 'blocked' | 'needs_negotiation' | 'estimated'
  baseline: GoalRoadmapBaseline[]
  prerequisite_path: GoalRoadmapNode[]
  cycles: Array<{
    week_start: string; week_end: string; budget_minutes: number; planned_minutes: number
    allocations: Array<{ work_id: string; minutes: number }>; status: 'planned'; estimate_only: true
  }>
  milestones: {
    monthly: Array<GoalRoadmapMilestone & { month: string }>
    weekly: Array<GoalRoadmapMilestone & { period_start: string; basis: string }>
  }
  burden: {
    weekly_minutes: number; weekly_sessions: number; committed_weekly_minutes: number
    remaining_weekly_minutes: number; auto_increase_budget: false; configured_budget_valid: boolean
    capacity_basis: string; estimated_minutes: { min: number; max: number }
    available_minutes: number | null; unallocated_minutes: number
    feasibility: 'unknown' | 'deadline_passed' | 'budget_unavailable' | 'horizon_unverified' | 'estimated_over_capacity' | 'estimated_at_risk' | 'provisional_not_guaranteed'
    estimate_complete: boolean; estimated_weeks: number | null
  }
  risks: Array<{ code: string; message: string }>
  clarifications: Array<{ field: string; reason: string }>
  next_daily_action: GoalRoadmapAction | null
  negotiation_options: string[]
  assumptions: {
    effort: string; freshness_days: number; evidence_window_days: number; calendar: string
    retention: string; backward_windows: string; coverage: string
  }
  evidence_fingerprint: string
  goal_attainment: 'not_established'
  mastery_claim_allowed: false
  local_only: true
  cloud_transmission_allowed: false
}

export interface GoalRoadmapPanelProps {
  childId: string
  roadmap: GoalRoadmap | null | undefined
  loading?: boolean
  error?: string | null
  onRetry?: () => void
}
