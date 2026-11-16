import type { OralSource } from './OralTaskPreview'
export type CurriculumParentRequest = (path: string, init?: RequestInit) => Promise<unknown>
export type CurriculumChild = { child_id: string; nickname: string; grade: string }
export type CurriculumScope = {
  grade: string; subject: string; edition: string; semester: string
  standard_scope: string; textbook_source_id: string
}
export type LocalSupplyResponse = { local_only: true; cloud_transmission_allowed: false }
export type CurriculumSource = {
  source_id: string; title: string; subject: string; edition: string | null
  available: boolean; blockers: string[]; excerpt_count: number
}
export type CurriculumOptions = LocalSupplyResponse & {
  child_id: string; grade: string; semesters: string[]; sources: CurriculumSource[]
}
export type ParentCurrentMaterials = {
  status: 'ready' | 'blocked'; reason: string | null
  materials: { material_key: string; title: string; version_label: string; heading: string | null; text: string }[]
  local_only: true; cloud_transmission_allowed: false; mastery_claim_allowed: false
}
export type CurriculumSourceOptions = LocalSupplyResponse & {
  child_id: string; source_id: string
  excerpts: { excerpt_id: string; sequence: number; text: string; current_materials?: ParentCurrentMaterials | null }[]
  standards: { requirement_id: string; title: string; stage_scope: string; requirement_text: string
    source_title: string; page_number: number; quote: string; available: boolean; blockers: string[] }[]
  seeds: { seed_item_id: string; title: string; prompt: string; validator: string; stages: string[] }[]
  first_sources?: { knowledge_node_id: string; title: string; excerpt_id: string; prompt: string }[]
}
export type CurriculumState = 'draft' | 'blocked' | 'validated' | 'reviewed' | 'published' | 'withdrawn' | 'rejected'
export type CurriculumStageReview = {
  schema: 'specialized_stage_review_v1'; stage: 'near_transfer' | 'far_transfer'; binding_version: string
  source_quote: string; candidate_quote: string; same_skill_rationale: string; changed_context_rationale: string
}
export type CurriculumStageContext = LocalSupplyResponse & {
  schema: 'specialized_stage_review_v1'; child_id: string; candidate_id: string; binding_version: string
  source_root_item_id: string; knowledge_node_id: string; source_prompt: string; candidate_prompt: string
  candidate_explanation: string; status: string; allowed_stages: CurriculumStageReview['stage'][]
  qualification_scope: 'explicit_human_source_stage_review_not_child_mastery'
}
export type CurriculumPackageSummary = {
  candidate_id: string; state: CurriculumState; version: number; eligible: boolean; blockers: string[]; next_action: string
}
export type CurriculumAudit = LocalSupplyResponse & {
  child_id: string; current_curriculum: { version: number; scope: CurriculumScope }[]
  blocked_job_counts: Record<string, number>; eligible_curriculum_items: number
  qualified_stages: string[]; packages: (CurriculumPackageSummary & { prompt: string })[]; thinking_or_transfer_coverage_claim_allowed: false
}
export type CurriculumSourceSpec = {
  seed_item_id: string; stage: string; excerpt_id: string; requirement_id: string
  standard_page: number; standard_quote: string; validator: string; explanation: string; difficulty: number
}
export type CurriculumFirstSourceSpec = Omit<CurriculumSourceSpec, 'seed_item_id' | 'stage' | 'validator'> & { knowledge_node_id: string }
export type CurriculumPackage = CurriculumPackageSummary & LocalSupplyResponse & {
  child_id: string; supersedes: string | null; diagnostic_item_id: string | null
  payload: (CurriculumSourceSpec | (CurriculumFirstSourceSpec & { seed_item_id: null; stage: 'parallel'; validator: 'curriculum_objective_exact_source_v1' })) & { prompt: string; expected_answer: string | null; pet_oral?: OralSource | null }
  stage_review_context?: CurriculumStageContext | null
  review_packet: null | {
    fingerprint: string; scope: CurriculumScope
    textbook: { source_id: string; title: string; source_ref: string; version_label: string; review_status: string; excerpt_id: string; content_text: string }
    standard: { source_id: string; official_url: string; page_number: number; requirement_text: string; quote: string }
    review_scope: string; pet_oral?: OralSource | null; current_materials?: ParentCurrentMaterials | null
  }
  events: { version: number; kind: string; actor: string; created_at: string
    result: { state: string; blockers: string[]; note?: string | null; decision?: string | null
      stage_review?: CurriculumStageReview | null
      check?: { validator: string; computed_answer: string; question_hash: string; payload_hash: string;
        expected_answer?: { answer_format?: string; numeric_kind?: 'integer' | 'rational'; tuple_size?: number } } | null } }[]
}
export type CurriculumCommand = { action_id: string; expected_version: number }
export type CurriculumPending = { child_id: string } & (
  | { kind: 'select'; body: CurriculumCommand & { scope: CurriculumScope } }
  | { kind: 'generate'; body: CurriculumCommand & { expected_scope_version: number; source: CurriculumSourceSpec; supersedes?: string; supersedes_version?: number } }
  | { kind: 'first-sources'; body: CurriculumCommand & { expected_scope_version: number; source: CurriculumFirstSourceSpec } }
  | { kind: 'validate' | 'publish'; candidate_id: string; body: CurriculumCommand }
  | { kind: 'review'; candidate_id: string; body: CurriculumCommand & { decision: 'approve' | 'reject'; note: string; stage_review?: CurriculumStageReview } }
  | { kind: 'withdraw'; candidate_id: string; body: CurriculumCommand & { note: string } }
)
export type CurriculumSelection = LocalSupplyResponse & { version: number; scope: CurriculumScope }
export type CurriculumDraftState = {
  sourceId: string; semester: string; standardKey: string; excerptId: string; seedId: string
  explanation: string; note: string; focusedId: string; pending: CurriculumPending | null
  difficulty?: string
}
