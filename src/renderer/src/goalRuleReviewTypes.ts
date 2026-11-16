import type { GoalRoadmap } from './GoalRoadmapPanel.types'
import type { ExamDurationVariant } from './examRuleDisplay'

export interface GoalRuleSnapshot {
  version: string
  snapshot_hash: string
  reference: { authority: string; url: string; verified_on: string }
  blueprint_snapshots: Array<{ blueprint_id: string; title: string; sections: Array<{
    section_id: string; title: string; duration_minutes: number; item_target?: number; item_target_unit?: string
    duration_variants?: ExamDurationVariant[]; operational_duration_basis?: string | null
  }> }>
}
export interface GoalRuleReview {
  child_id: string
  program_id: string
  track: string
  program_version: string
  can_confirm: boolean
  rules: Omit<NonNullable<GoalRoadmap['exam_rules']>, 'current_snapshot'> & {
    current_snapshot: GoalRuleSnapshot | null
    bound_snapshot: GoalRuleSnapshot | null
  }
  local_only: true
  cloud_transmission_allowed: false
}
export interface GoalRuleConfirmation {
  expected_program_version: string
  expected_rule_version: string
  expected_rule_hash: string
  client_action_id: string
}
export interface GoalRuleConfirmationResult {
  review: GoalRuleReview
  receipt: { revision_id: string; child_id: string; program_id: string; rule_hash: string; confirmed_at: string }
  replayed: boolean
}
