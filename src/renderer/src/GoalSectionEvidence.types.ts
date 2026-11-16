export interface GoalSectionSource {
  source_qualification: 'parent_reviewed_source' | 'deterministically_validated_source' | 'shipped_reference_source'
  answer_type: string
  task_family?: string | null
}

export interface GoalSection {
  section_id: string
  title: string
  official_part_mapping: 'missing'
  coverage_status: 'missing_reviewed_part_mapping'
  scope_status: 'practice_scope_available' | 'missing'
  mastery_claim_allowed: false
  sources: GoalSectionSource[]
  opportunities: unknown[]
  qualified_evidence: unknown[]
  available_source_count: number
  reviewed_source_count: number
  opportunity_count: number
  qualified_evidence_count: number
  independent_correct_count: number
  distinct_qualified_questions: number
  distinct_independent_correct_questions: number
}

export interface GoalSectionEvidence {
  version: 'goal_section_evidence_v1'
  child_id: string
  context_version: string
  rule_snapshot_hash: string | null
  rule_binding_status: string
  public_rule_status: string
  as_of_date: string
  time_basis: 'current_source_and_receipt_at_read_not_end_of_day'
  status: 'scope_inventory_not_official_coverage' | 'rule_applicability_unverified'
  sections: GoalSection[]
  unavailable_item_ids: string[]
  unmapped_item_ids: string[]
  unmapped_or_unavailable_opportunity_count: number
  exam_readiness_claim_allowed: false
  local_only: true
  cloud_transmission_allowed: false
}
