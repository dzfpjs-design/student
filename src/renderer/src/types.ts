export type ChildProfile = {
  child_id: string
  nickname: string
  grade: string
  curriculum_region: string
  status: string
}

export type DailySession = {
  session_id: string
  child_id: string
  status: string
  state: string
  started_at: string
  last_checkpoint_at: string
  observation_status: 'enabled' | 'paused'
  observation_revision: number
  observation_generation: string
  snooze_until: string | null
  recall_declared_empty: number
  homework_declared_empty: number
}

export type DeviceState = {
  device_type: 'camera' | 'document_camera' | 'microphone'
  status: 'unavailable' | 'ready' | 'active' | 'paused' | 'error'
  message: string
  historical_binding?: { label: string; verified_at: string } | null
  open_failure?: { error_code: string; message: string; occurred_at: string } | null
}

export type DeviceBinding = {
  device_type: 'camera' | 'document_camera'
  system_id_hash: string
  label: string
  capabilities: Record<string, unknown>
  verified_at: string
  updated_at: string
}

export type DeviceQualificationMode = 'quick_dual' | 'dual_concurrency' | 'camera_stability' | 'document_stability'

export type DeviceQualificationRun = {
  run_id: string
  completion_token?: string
  mode: DeviceQualificationMode
  status: 'active' | 'passed' | 'failed' | 'cancelled' | 'interrupted'
  expected_seconds: number
  metrics: Record<string, unknown>
  failure_code: string | null
  started_at: string
  completed_at: string | null
}

export type DeviceQualificationSummary = {
  bindings_ready: boolean
  profiles_ready: boolean
  profile_missing: Record<'camera' | 'document_camera', string[]>
  acceptance_status: 'pending' | 'passed'
  required: Array<{ mode: Exclude<DeviceQualificationMode, 'quick_dual'>; status: DeviceQualificationRun['status'] | 'not_run'; completed_at: string | null }>
  recent_runs: DeviceQualificationRun[]
}

export type ObservationCalibrationResult = {
  calibration_ready: boolean
  scene_quality: { status: 'ready' | 'too_dark' | 'too_bright' | 'blurred'; message: string }
  person_visible: boolean
  person_message: string
  metrics: { brightness: number; sharpness: number; width: number; height: number }
  model_version: string
  raw_frame_stored: false
}

export type ObservationCalibrationRun = {
  run_id: string
  child_id: string
  status: 'active' | 'passed' | 'failed' | 'cancelled' | 'interrupted'
  result: Partial<ObservationCalibrationResult>
  failure_code: string | null
  started_at: string
  completed_at: string | null
  current_binding: boolean
}

export type ObservationCalibrationSummary = {
  latest: ObservationCalibrationRun | null
  active: ObservationCalibrationRun | null
  recent_runs: ObservationCalibrationRun[]
}

export type BootstrapState = {
  children: ChildProfile[]
  active_session: DailySession | null
  devices: DeviceState[]
  privacy_paused: boolean
  cloud_permissions?: { cloud_model_text_allowed: boolean; cloud_audio_allowed: boolean }
  recovery?: {
    unclean_restart_detected: boolean
    sensors_forced_paused: boolean
  }
  model: {
    provider: string
    base_url?: string
    model: string
    voice_intent_model?: string
    configured: boolean
    fallback_provider: string | null
    fallback_base_url?: string | null
    fallback_model: string | null
    fallback_configured: boolean
  }
}

export type ClassroomRecall = {
  recall_id: string
  subject: string
  summary: string
  confidence: number
  confirmation_status: string
  record_kind: 'child_statement' | 'system_candidate'
  candidate_nodes: Array<{ title: string }>
  textbook_mapping: { status: 'course_index_only' | 'family_approved_source' | 'unmapped'; label: string } | null
  evidence_sources: string[]
  created_at: string
}

export type HomeworkItem = {
  homework_id: string
  subject: string
  task_type: string
  title: string
  range_text: string | null
  expected_minutes: number
  due_at: string | null
  special_requirements: string | null
  source_artifact_id: string | null
  ocr_confidence: number | null
  confirmation_status: string
  status: string
}

export type DeferredTaskCandidate = {
  source_plan_item_id: string
  source_version?: string
  source_session_id: string
  source_date: string
  subject: string
  title: string
  expected_minutes: number
  task_type: HomeworkItem['task_type']
  range_text: string | null
  due_at: string | null
  special_requirements: string | null
  reschedule_reason: string
}

export type DeferredTaskDecision = {
  review_id: string
  source_plan_item_id: string
  source_session_id: string
  decision: 'continue_today' | 'completed_elsewhere' | 'cancelled'
  reason: string
  target_homework_id: string | null
  reviewed_at: string
}

export type DailyPlan = {
  plan_id: string
  plan_date: string
  status: string
  total_minutes: number
  generator: string
  version: number
}

export type PlanningContext = {
  energy_level: 'low' | 'medium' | 'high'
  fixed_commitment_minutes: number
  fixed_commitment_note: string | null
  tomorrow_event_subject: string | null
  tomorrow_event_title: string | null
  tomorrow_event_minutes: number
  updated_at: string | null
}

export type PlanItem = {
  plan_item_id: string
  homework_id: string | null
  subject: string
  title: string
  expected_minutes: number
  actual_minutes: number | null
  priority: number
  status: string
  position: number
  started_at: string | null
  completed_at: string | null
  active_seconds: number
  active_segment_started_at: string | null
  source_type: 'school_homework' | 'tomorrow_event' | 'due_review' | 'mistake_review' | 'pet_daily' | 'competition_math' | 'junior_exam_goal' | 'agent_retention' | 'oral_question_supply' | 'oral_delivered_continuation' | 'oral_daily_check' | 'planned_break'
  source_ref: string | null
  completion_standard: string
  cognitive_load: 'low' | 'medium' | 'high'
  task_type: 'written' | 'memorization' | 'reading' | 'correction' | 'review' | 'preview' | 'test_prep' | 'reasoning' | 'speaking' | 'oral' | 'writing' | 'break' | 'goal_prerequisite'
  completion_evidence_type: 'child_confirmed' | 'child_checked' | 'needs_correction' | 'voice_confirmed' | 'planned_break' | 'review_evidence' | null
  completion_note: string | null
  completion_checklist: string[]
  completion_artifact_id: string | null
}

export type SessionWorkspaceState = {
  supply_obligations?: import('./SupplyObligationStatus').SupplyObligation[]
  session: DailySession
  family_rule: FamilyRule
  planning_context: PlanningContext
  recalls: ClassroomRecall[]
  homework: HomeworkItem[]
  carryover_candidates: DeferredTaskCandidate[]
  carryover_decisions: DeferredTaskDecision[]
  plan: DailyPlan | null
  plan_items: PlanItem[]
  constraint_decisions?: Array<{
    decision_id?: string
    obligation_type: string
    source_ref: string | null
    decision: 'scheduled' | 'deferred' | 'applied' | 'suppressed'
    reason: string
    requested_minutes: number
    allocated_minutes: number
    metadata: Record<string, unknown>
    created_at: string
  }>
  daily_report_ready: boolean
  latest_tutor_turn: TutorTurn | null
  drafts: Partial<Record<SessionDraftSection, SessionDraft>>
  sensor_release: SensorReleaseSummary
}

export type SensorReleaseSummary = {
  opened_leases: number
  active_leases: number
  released_leases: number
  application_release_confirmed: boolean
  evidence_level: 'not_opened' | 'application_record_only' | 'windows_session_verified'
  os_release_verified: boolean
  release_verification: SensorReleaseVerification | null
  by_device: Record<string, {
    opened: number
    active: number
    released: number
    last_release_reason: string | null
    last_released_at: string | null
  }>
}

export type SensorReleaseVerification = {
  result: 'passed' | 'failed'
  used_device_types: Array<'camera' | 'document_camera' | 'microphone'>
  reopened_device_types: Array<'camera' | 'document_camera' | 'microphone'>
  device_manager_healthy: boolean
  windows_privacy_indicator_clear: boolean
  physical_indicator_status: 'clear' | 'not_available' | 'still_on'
  platform_name: 'Windows'
  platform_version: string
  notes?: string | null
  verified_at: string
}

export type SensorReleaseReview = {
  session_id: string
  child_id: string
  child_nickname: string
  ended_at: string
  sensor_release: SensorReleaseSummary
}

export type SessionDraftSection = 'check_in' | 'classroom_recall' | 'homework_capture' | 'study' | 'reflection' | 'agent_command'

export type SessionDraft = {
  payload: Record<string, unknown>
  updated_at: string
}

export type ParentCoaching = {
  focus?: 'empty_day' | 'correction' | 'unfinished_task' | 'reported_difficulty' | 'reflection_skipped' | 'completed_day' | 'limited_evidence' | 'insufficient_evidence' | 'period_review' | 'verified_learning_check' | 'verified_retention_support'
  evidence_basis?: string[]
  scenario: string
  why_this_fits: string
  script: string[]
  script_phases?: Array<{ phase: '开场' | '倾听' | '共情' | '共同选择' | '收尾'; text: string }>
  evidence_refs?: string[]
  evidence_sources?: Array<{ evidence_ref: string; source: string; occurred_at?: string | null }>
  uncertainty?: string
  action: {
    what: string
    duration_minutes: number
    steps: string[]
    stop_condition: string
    learning_target?: {
      evidence_id: string
      knowledge_node_id: string
      knowledge_title: string
      question_identity: string
      mastery_claim_allowed: false
      evidence_kind: 'qualified_learning_attempt' | 'qualified_mistake_retention'
      next_use: 'optional_recheck_candidate' | 'optional_independence_support'
      retention_basis_refs?: string[]
    }
  } | null
  avoid: Array<{ do_not_say: string; say_instead: string }>
  adaptation?: {
    basis: 'cold_start' | 'confirmed_facts' | 'parent_feedback'
    feedback_count: number
    average_effectiveness?: number | null
    average_stress?: number | null
    note: string
  }
  personalization?: {
    child_stage: string
    goal_context: string | null
    recent_confirmed_facts: string[]
    feedback_considered: number
    previous_decision?: 'adopt' | 'custom' | 'skip' | null
  }
}

export type ParentReportFeedback = {
  feedback_id: string
  report_id: string | null
  period_report_id?: string | null
  source_kind?: 'daily_report' | 'period_report'
  source_id?: string
  child_id: string
  executed: boolean | null
  effectiveness: number | null
  stress_rating: number | null
  notes_summary: string | null
  created_at: string
  updated_at: string
  action_snapshot_id?: string
  client_action_id?: string
  binding_status?: 'bound' | 'unknown'
  revision?: number
  operation?: 'record' | 'retract'
  executed_at?: string | null
  reason?: string | null
}

export type ParentActionSnapshot = {
  feedback_eligibility?: { record_allowed: boolean; approval_witness_valid: boolean; current_execution_granted: false; reason: 'invalid_approval_witness' | 'explicit_parent_withdrawal' | 'original_approval' | 'system_superseded_approval' }
  binding_status: 'bound'
  snapshot_id: string
  report_id: string | null
  period_report_id?: string | null
  source_kind?: 'daily_report' | 'period_report'
  source_id?: string
  child_id: string
  version: number
  fingerprint: string
  coaching: ParentCoaching
  timing: { report_date: string; when: string | null; duration_minutes: number | null; planned_at: string | null }
  feedback: ParentReportFeedback | null
  execution_allowed?: boolean
  review_binding?: { snapshot_id: string; source_version: string; approval_event_id: string; decision: 'adopt' | 'custom' | 'skip'; edit_fingerprint: string; base_action_fingerprint: string }
}

export type ParentActionFeedbackInput = {
  executed: boolean | null
  effectiveness: number | null
  stress_rating: number | null
  notes_summary: string
  action_snapshot_id?: string
  client_action_id?: string
  expected_revision?: number
  executed_at?: string | null
  operation?: 'record' | 'retract'
  reason?: string
}

export type ChildLearningSummary = {
  child_id: string
  session_id: string | null
  learning_date: string
  child_response: string
  local_only: true
  cloud_transmission_allowed: false
  mastery_update_allowed: false
  learning_content: Array<{ title: string; evidence_status: string }>
  homework: Array<{ title: string; status: string; status_label: string }>
  weak_points: Array<{ title: string; evidence_status: string }>
  needs_verification: Array<{ title: string; evidence_status: string }>
  remediation: Array<{ title: string; status: string; guidance: string; estimated_minutes?: number; due_at?: string }>
}

export type DailyReport = {
  report_id: string
  session_id: string
  child_id: string
  report_date: string
  source: string
  summary: {
    headline: string
    facts: string[]
    interpretation: string
    evidence_count: number
  }
  parent_coaching: ParentCoaching
  evidence_detail: {
    recalls: Array<{ subject: string; summary: string; confirmation_status: string; confidence: number; created_at: string }>
    tasks: Array<{ subject: string; title: string; status: string; expected_minutes: number; actual_minutes: number | null; task_type: PlanItem['task_type']; completion_evidence_type: PlanItem['completion_evidence_type']; completion_note: string | null; completion_checklist: string[]; completion_artifact_id: string | null; completion_artifact_status: string | null; completion_artifact_confidence: number | null; completion_artifact_raw_stored: boolean; completed_at: string | null; reschedule_reason?: string | null }>
    reflection: { learned_text: string; difficult_text: string; feeling: string; next_action: string; skipped: boolean } | null
    knowledge_evidence?: Array<{ subject: string; knowledge_title: string; evidence_type: string; correctness: number | null; hint_level: number; independent: boolean; confidence: number; score_status?: 'verified_local'; mastery_claim_allowed?: false; mastery_probability?: number | null; mastery_evidence_count?: number | null; status_confidence?: number | null }>
    mistakes?: Array<{ subject: string; title: string; status: string; confidence: number; updated_at: string }>
    pet_vocabulary_attempts?: Array<{ attempt_id: string; surface: string; focus_kind: 'word' | 'phrase'; review_mode: 'recognition' | 'recall'; correct: number; interval_days: number; next_due_at: string; occurred_at: string }>
    declarations: { no_recall: boolean; no_homework: boolean }
  }
  feedback: ParentReportFeedback | null
  parent_action?: ParentActionSnapshot | { binding_status: 'unknown'; reason: string }
  created_at: string
}

export type ReportFeedbackRevision = {
  event_id: string
  revision: number
  operation: 'record' | 'retract'
  created_at: string
  payload: { helpfulness?: number | null; stress_rating?: number | null; notes?: string | null; reason?: string }
}

export type ParentReportCorrectionStatus = {
  snapshot_id: string
  child_id: string
  status: 'draft' | 'approved' | 'revoked'
  source_current: boolean
  latest_replacement_id?: string | null
  latest_replacement_availability?: 'available' | 'none' | 'deleted' | 'ambiguous'
  latest_replacement_status?: 'draft' | 'approved' | 'revoked' | null
  replacements: Array<{ snapshot_id: string; replacement_snapshot_id: string; event_id: string; occurred_at: string }>
  prior_submissions: Array<{ delivery_id: string; snapshot_id?: string; status: ReportDelivery['status']; recipient_id: string }>
  replacement_submissions: Array<{
    delivery_id: string; snapshot_id: string; recipient_id: string; status: ReportDelivery['status']
    sent_at: string | null; provider_message_id: string | null; evidence_version: string; source_version: string
    source_current: boolean; channel_acceptance: 'accepted' | 'unknown' | 'not_confirmed'
    is_latest_replacement?: boolean
    human_delivery_verified: false; human_read_verified: false
  }>
  recipient_ids_requiring_correction: string[]
  recipient_ids_blocked_by_consent?: string[]
  correction_notification_required: boolean
  next_action: 'recheck_source' | 'review_replacement_and_choose_notification' | 'verify_correction_receipt'
  automatic_delivery: false
  human_delivery_verified: false
  local_only: true
  cloud_transmission_allowed: false
}

export type ParentReportSnapshot = {
  parent_edit_version?: string
  edit_transfer?: { source_snapshot_id: string; target_snapshot_id: string; source_edit_version: string; target_edit_version: string; client_action_id: string; saved_edit_version: string; saved_at: string; replayed: boolean }
  historical_parent_actions?: ParentActionSnapshot[]
  snapshot_id: string
  source_kind: 'daily_report' | 'period_report'
  parent_action?: ParentActionSnapshot | { binding_status: 'unknown'; reason: string }
  source_id: string
  report_id: string | null
  child_id: string
  report_type: 'daily' | 'weekly' | 'monthly'
  source_version: string
  template_version: string
  evidence_version: string
  facts: { facts?: string[]; evidence_manifest?: Record<string, unknown>; evidence_detail?: DailyReport['evidence_detail']; coverage?: Record<string, unknown>; daily_signals?: Array<Record<string, unknown>>; learning_summary?: Record<string, unknown> }
  inferences: { interpretation?: string; parent_coaching?: ParentCoaching; limitations?: string[]; next_action?: Record<string, unknown> | null }
  content: { headline: string; report_date: string | null; summary: DailyReport['summary'] | Record<string, unknown>; parent_coaching: ParentCoaching; evidence_detail: DailyReport['evidence_detail'] | Record<string, never> }
  confidence: { overall: 'high' | 'limited'; uncertainty: string; evidence_count?: number; coverage?: Record<string, unknown> }
  status: 'draft' | 'approved' | 'revoked'
  revoke_reason?: string | null
  parent_edit: {
    delivery_intro: string
    parent_note: string
    coaching_decision?: 'adopt' | 'custom' | 'skip'
    coaching_script?: string
    coaching_action?: string
    coaching_duration_minutes?: number
    coaching_observation?: string
  }
  corrections: Array<{ note: string; created_at: string }>
  correction_status?: ParentReportCorrectionStatus
  created_at: string
  approved_at: string | null
  feedback: Array<{ helpfulness: number | null; stress_rating: number | null; notes: string | null; created_at: string }>
  feedback_current?: { helpfulness: number | null; stress_rating: number | null; notes: string | null; revision: number } | null
  feedback_current_revision?: number | null
  feedback_current_status?: 'active' | 'retracted' | 'source_invalid' | 'legacy_unversioned' | 'none'
}

export type ReportDeliveryRecipient = {
  recipient_id: string
  child_id: string
  channel: 'email' | 'wechat_work_bot'
  address: string
  label: string
  consent_status: 'pending' | 'consented' | 'revoked'
  consented_at: string | null
}

export type ReportDelivery = {
  delivery_id: string
  snapshot_id: string
  recipient_id: string
  channel: 'email' | 'wechat_work_bot'
  status: 'scheduled' | 'processing' | 'sent' | 'failed' | 'cancelled' | 'unknown' | 'unknown_acknowledged' | 'superseded'
  scheduled_at: string
  attempt_count: number
  last_error_code: string | null
  last_error_message: string | null
  sent_at: string | null
  cancel_reason: string | null
  replacement_delivery_id?: string | null
  attempts: Array<{ attempt_number: number; status: string; error_code: string | null; error_message: string | null; created_at: string }>
}

export type ModelOutputEnvelope = {
  request_id: string
  output_type: 'daily_plan' | 'tutoring' | 'diagnosis' | 'report' | 'dialogue'
  result: Record<string, unknown>
  reason_summary: string
  confidence: number
  evidence_refs: string[]
  model_provider: string
  model_version: string
  prompt_version: string
  knowledge_version: string | null
  policy_version: string
  verification_status: 'verified' | 'partially_verified' | 'unverified' | 'rejected'
  safety_status: 'passed' | 'degraded' | 'blocked' | 'escalated'
  fallback_used: boolean
  example_quality?: { status: 'evidence_present' | 'not_needed' | 'missing' | 'unavailable' | 'not_assessed' }
}

export type TutorTurn = {
  turn_id: string
  session_id: string
  child_id: string
  plan_item_id: string | null
  subject: string
  question: string
  answer: string
  provider: string
  model: string
  status: string
  hint_level: number
  parent_turn_id: string | null
  response_kind: 'hint' | 'full_explanation'
  verification_status: string
  safety_status: string
  strategy_experiment_id: string | null
  strategy_version: string | null
  output_envelope: ModelOutputEnvelope | null
  created_at: string
}

export type TutorBlackboardTone = 'chalk' | 'yellow' | 'cyan' | 'coral' | 'green'

export type TutorBlackboardBlock = {
  kind: 'heading' | 'text' | 'formula' | 'steps' | 'table' | 'relation' | 'question'
  tone: TutorBlackboardTone
  text?: string
  latex?: string
  items?: string[]
  columns?: string[]
  rows?: string[][]
  nodes?: string[]
}

export type TutorBlackboardPresentation = {
  version: 'tutor_blackboard_v1'
  source: 'model_structured' | 'model_structured_repaired' | 'local_degraded' | 'model_plus_local_diagnostic' | 'visual_teaching_v1'
  repair_warnings?: string[]
  degraded_reason?: 'invalid_json' | 'invalid_schema' | 'safety_policy_rewrite' | 'verification_rewrite' | 'local_fallback'
  sync_mode: 'segment_estimate'
  subject: string
  title: string
  segments: Array<{
    segment_id: string
    narration: string
    duration_hint_ms: number
    blocks: TutorBlackboardBlock[]
  }>
}

export type StrategyExperiment = {
  experiment_id: string
  child_id: string
  strategy_key: 'tutor_hint_pacing'
  baseline_version: string
  candidate_version: string
  status: 'draft' | 'replay_passed' | 'shadow' | 'active' | 'rolled_back' | 'rejected'
  replay_metrics: {
    tutor_feedback_count?: number
    tutor_understood_rate?: number | null
    eligible_tutor_turns?: number
    allowlist_passed?: boolean
  }
  shadow_metrics: StrategyPhaseMetrics
  active_metrics: StrategyPhaseMetrics
  phase_metrics: StrategyPhaseMetrics
  rollback_reason: string | null
  frozen_boundaries: string[]
  transitions: Array<{
    from_status: string | null
    to_status: string
    actor: string
    reason: string
    occurred_at: string
    metrics: Record<string, unknown>
  }>
  created_at: string
  updated_at: string
  activated_at: string | null
  ended_at: string | null
}

export type StrategyPhaseMetrics = {
  eligible_turns?: number
  feedback_count?: number
  understood_rate?: number | null
  policy_replacements?: number
}

export type TutorFeedback = {
  feedback_id: string
  turn_id: string
  response_text: string
  outcome: 'understood' | 'still_stuck' | 'answer_attempt'
  error_hypothesis: string
  confidence: number
  evidence_basis: string
  created_at: string
}

export type ModelCallAudit = {
  purpose: string
  provider: string
  model: string
  status: 'started' | 'completed' | 'failed'
  latency_ms: number | null
  prompt_tokens: number | null
  completion_tokens: number | null
  input_price_cny_per_million: number | null
  output_price_cny_per_million: number | null
  estimated_cost_cny: number | null
  verification_status: string | null
  safety_status: string | null
  fallback_used: number
  error_code: string | null
  privacy_summary: {
    policy_version: string | null
    transmitted: boolean | null
    redaction_count: number
    redaction_types: string[]
    transmitted_recall_count: number | null
    omitted_sensitive_context_count: number
    blocked_categories: string[]
  }
  created_at: string
  completed_at: string | null
}

export type ModelPricing = {
  provider: 'qwen' | 'deepseek'
  model: string
  input_price_cny_per_million: number
  output_price_cny_per_million: number
  currency: 'CNY'
  source: 'parent_configured'
  updated_at: string
}

export type ModelRuntimePolicyStatus = {
  policy: {
    enforcement_mode: 'monitor' | 'enforce'
    daily_cost_limit_cny: number
    daily_token_limit: number
    failure_threshold: number
    cooldown_seconds: number
    updated_at: string
  }
  today: {
    calls: number
    tokens: number
    estimated_cost_cny: number
    failures: number
    fallbacks: number
    cost_limit_reached: boolean
    token_limit_reached: boolean
  }
  routes: Array<{
    provider: string
    model: string
    status: 'healthy' | 'degraded' | 'degraded_recoverable' | 'rate_limited' | 'quota_exhausted' | 'auth_failed'
    consecutive_failures: number
    last_error_code: string | null
    blocked_until: string | null
    last_success_at: string | null
    updated_at: string
  }>
  purpose_quality: Array<{
    purpose: string
    calls: number
    failures: number
    rejected: number
    fallbacks: number
    average_latency_ms: number
    estimated_cost_cny: number
    bad_result_rate: number
    state: 'healthy' | 'watch' | 'blocked'
  }>
}

export type ParentStatus = {
  configured: boolean
  locked_seconds: number
}

export type InstallationSetupStatus = {
  schema: 'hsc.installation-setup.v1'
  existing_family?: boolean
  complete: boolean
  parent_pin_configured: boolean
  smtp_configured: boolean
  smtp_secret_check_required: boolean
  active_child_count: number
  children_missing_email_recipient: number
  children_missing_email_test: number
  next_step: 'parent_pin' | 'smtp' | 'email_recipients' | 'email_test' | 'complete'
}

export type ParentToday = {
  generated_at: string
  items: Array<{
    child: { child_id: string; nickname: string; grade: string }
    session: { session_id: string; state: string; status: string; started_at: string; ended_at: string | null } | null
    plan: { total: number; completed: number; deferred: number; planned_minutes: number } | null
    current_task: { title: string; subject: string; status: string; expected_minutes: number } | null
    due_reviews: number
    latest_report: { report_id: string; report_date: string; headline: string } | null
    failures: { pending_reports: number; model_failures_last_7_days: number }
  }>
}

export type ParentDashboard = {
  today: ParentToday
  children: Array<{
    child_id: string
    reports: DailyReport[]
    learning_state: LearningState
    period_reports: PeriodReport[]
    family_rule: FamilyRule
    weekly_goals: WeeklyGoal[]
    strategy_experiments: StrategyExperiment[]
  }>
  privacy: PrivacyPreferences
  health: SystemHealth
  device_bindings: DeviceBinding[]
  device_qualification: DeviceQualificationSummary
  sensor_release_reviews: SensorReleaseReview[]
  sensor_release_platform_eligible: boolean
  safety_events: ChildSafetyEvent[]
}

export type ChildSafetyEvent = {
  safety_event_id: string
  child_id: string
  child_nickname: string
  risk_level: 'R1' | 'R2' | 'R3'
  category: 'emotional_distress' | 'fear_or_bullying' | 'immediate_danger'
  status: 'pending' | 'acknowledged'
  guidance: {
    summary: string
    script: string[]
    action: { what: string; duration_minutes: number; steps: string[]; stop_condition: string }
    avoid: Array<{ do_not_say: string; say_instead: string }>
  }
  created_at: string
  acknowledged_at: string | null
}

export type GoalLearningOutcome = {
  status: 'recheck_needed' | 'supported_only' | 'independent_observed' | 'retention_observed' | 'insufficient_evidence'
  next_action: string
  reason: string
  track: string
  evidence_version: string
  evidence_fingerprint: string
  window_days: number
  evidence_scope: 'current_valid_evidence' | 'as_of_week_end'
  as_of?: string | null
  goal_attainment: 'not_established'
  mastery_claim_allowed: false
  cloud_transmission_allowed?: false
  nodes: Array<{
    knowledge_node_id: string
    subject: string
    title: string
    track: string
    status: GoalLearningOutcome['status']
    next_action: string
    reason: string
    evidence_refs: string[]
    independent_correct: number
    attempt_count: number
    retention_success_count: number
    distinct_items: number
    independent_distinct_items?: number
  }>
}

export type GoalFocus = {
  subject?: string
  title?: string | null
  reason?: string
  evidence_level?: string
  status?: GoalLearningOutcome['status'] | 'needs_baseline'
  next_action?: string
  cloud_transmission_allowed?: false
}

export type GoalProgressExplanation = {
  participation_status?: 'not_started' | 'on_track' | 'behind' | 'quota_met'
  status_scope?: 'participation_only'
  learning_outcome?: GoalLearningOutcome | null
  budget_status?: 'available' | 'exhausted'
  scheduling_status?: 'ready' | 'quota_met' | 'budget_exhausted'
  scheduling_reason?: string
  focus?: GoalFocus | null
}

export type WeeklyGoal = GoalProgressExplanation & {
  weekly_goal_id: string
  child_id: string
  week_start: string
  week_end: string
  track: 'pet' | 'competition_math'
  target_minutes: number
  target_sessions: number
  completed_minutes: number
  completed_sessions: number
  remaining_minutes: number
  remaining_sessions: number
  status: 'not_started' | 'on_track' | 'behind' | 'completed'
  generator: string
  knowledge_version: string
  created_at: string
  updated_at: string
}

export type DiagnosticItem = {
  pet_audio_modality?: 'listening' | 'speaking' | null
  diagnostic_item_id: string
  knowledge_node_id: string
  track: string
  subject: string
  title: string
  prompt: string
  answer_type: string
  difficulty: number
  review_task_id?: string
  review_interval?: 'immediate' | 'next_day' | '48h' | '7d' | 'school_open_near' | 'school_open_far' | 'school_open_7d'
  due_at?: string
  stimulus_text?: string | null
  stimulus_asset?: string | null
  assessment_mode?: 'objective' | 'pet_reference_rubric' | 'open_response_rubric'
  response_mode?: 'written' | 'spoken_explanation' | null
}

export type DiagnosticAttempt = {
  attempt_id: string
  evidence_id?: string
  child_id?: string
  session_id?: string
  diagnostic_item_id: string
  submitted_answer: string
  correctness: number | null
  feedback: string
  score_status?: 'verified_local' | 'practice_only' | 'needs_review'
  mastery_update_allowed?: boolean
  review_task_id?: string | null
  pet_baseline?: PetBaseline | null
  specialized_assessment?: null | {
    run_id: string
    track: 'pet' | 'competition_math' | 'junior_exam'
    phase: 'baseline' | 'foundation' | 'strengthening' | 'transfer_and_simulation'
    status: 'active' | 'completed' | 'cancelled'
    total_items: number
    completed_items: number
    next_item_id: string | null
    domain_results: Record<string, number>
    qualified_items?: number
    unverified_items?: number
    learning_status?: 'observed_checks_not_mastery' | 'needs_verification'
  }
  next_specialized_item?: DiagnosticItem | null
  knowledge_graph?: {
    status: 'recorded' | 'manual_confirmation_required'
    node_id?: string
    exercise_id?: string
    mistake_recorded?: boolean
    reason?: string
    result_source?: string
  }
  created_at: string
}

export type WritingFeedbackNotice = {
  status: string; reason?: string | null; feedback_turn_id?: string; local_feedback_turn_id?: string;
  local_only?: boolean; cloud_transmission_allowed?: boolean;
}

export type DiagnosticPromptResult = {
  foundation_remediation?: {
    origin: { prompt_id: string; item_version: string; attempt_id: string; evidence_id: string; plan_item_id: string }
    mistake_id: string; phase: string; response_text: string; local_only: true; cloud_transmission_allowed: false
  }
  numeric_clarification?: { parent_prompt_id: string; parent_item_version: string; parent_attempt_id: string }
  original_audio_teaching?: { work_id?: string; state: string; generation?: number; error_code?: string | null }
  deferred_answer?: {
    version: string; source_turn_id: string; source_voice_input_id: string | null;
    speech_retained: boolean; formal_answer_saved: boolean; automatic_submission_allowed: boolean;
    new_confirmation_required: boolean; delivery_complete: boolean | null;
    local_only: boolean; cloud_transmission_allowed: boolean; message: string | null;
  }
  local_learning_evaluation?: WritingFeedbackNotice
  status: 'waiting_answer' | 'no_item' | 'answer_saved' | 'answered' | 'invalidated' | 'cancelled' | 'awaiting_task_start'
  plan_item_id?: string
  prompt: { prompt_id: string; item_version: string; diagnostic_item_id: string; review_task_id: string | null;
    child_id: string; origin_session_id: string; item: DiagnosticItem; created_at: string } | null
  attempt: DiagnosticAttempt | null
  reason: string | null
  pet_baseline?: PetBaseline | null
  specialized_assessment?: DiagnosticAttempt['specialized_assessment']
}

export type KnowledgeGraphLearningPath = {
  child_id: string
  node_id: string
  mastery: { mastery_probability: number; evidence_count: number }
  diagnosis: {
    prerequisites: Array<{ title: string }>
    weaknesses: Array<{ title?: string }>
    retest_items: Array<{ title: string; description?: string | null; basis?: string }>
  }
  materials: Array<{
    title: string
    preview?: string
    license_boundary?: string
    review_status?: 'approved'
    media_status?: { status?: string }
  }>
  next_action: 'explain' | 'retest' | 'manual_material_required'
  material_availability: 'available' | 'material_unavailable'
  truthfulness: { mapping_status: string; unreviewed_candidates_hidden: boolean; media_is_local_only: boolean }
}

export type PetBaseline = {
  definition_version: string
  status: 'not_started' | 'in_progress' | 'completed'
  completed_tasks: number
  required_tasks: number
  evidence_count: number
  reference_only: true
  boundary: string
  dimensions: Array<{
    key: 'reading' | 'listening' | 'writing' | 'speaking'
    label: string
    score_kind: 'objective' | 'reference'
    completed_tasks: number
    required_tasks: number
    status: 'not_started' | 'in_progress' | 'completed'
    reference_score: number | null
    tasks: Array<{ task: string; completed: boolean; last_attempt_at: string | null }>
  }>
}

export type CurrentLearningEvidence = {
  status: 'needs_evidence' | 'recheck_needed' | 'supported_only' | 'independent_observed' | 'retention_observed'
  readiness: 'ready' | 'needs_evidence'
  attempt_count: number
  independent_attempt_count: number
  supported_attempt_count: number
  independent_correct: number
  distinct_items: number
  independent_distinct_items: number
  retention_success_count: number
  evidence_refs: string[]
  retention_basis_refs: string[]
  latest_evidence_at: string | null
  mastery_claim_allowed: false
}

export type LearningState = {
  child_id: string
  algorithm_version: string
  knowledge_version: string
  pet_baseline: PetBaseline | null
  items: Array<{
    knowledge_node_id: string
    track: string
    subject: string
    code: string
    title: string
    description: string
    current_evidence?: CurrentLearningEvidence
    mastery_probability: number | null
    evidence_count: number | null
    hint_dependency: number | null
    status_confidence: number | null
    last_practiced_at: string | null
    last_independent_correct_at: string | null
    forgetting_rate: number | null
    next_review_at: string | null
    prerequisites: Array<{ knowledge_node_id: string; title: string; track: string; mastery_probability: number | null; status_confidence: number | null; readiness: 'ready' | 'needs_evidence' }>
    next_paths: Array<{ knowledge_node_id: string; title: string; relation_type: 'prerequisite_of' | 'transfers_to' }>
  }>
}

export type MasteryHistoryItem = {
  algorithm_version: string
  created_at: string
  evidence_type: string
  correctness: number | null
  hint_level: number
  independent: number
  confidence: number
  evidence_summary: string
  occurred_at: string
  old_state: {
    mastery_probability: number
    evidence_count: number
    hint_dependency: number
    status_confidence: number
    last_practiced_at: string | null
    last_independent_correct_at: string | null
    forgetting_rate: number
    next_review_at: string | null
    algorithm_version: string | null
  }
  new_state: {
    mastery_probability: number
    evidence_count: number
    hint_dependency: number
    status_confidence: number
    last_practiced_at: string
    last_independent_correct_at: string | null
    forgetting_rate: number
    next_review_at: string | null
    algorithm_version: string
  }
}

export type PrivacyPreferences = {
  cloud_model_text_allowed: boolean
  cloud_audio_allowed: boolean
  raw_media_storage_allowed: boolean
  model_improvement_allowed: boolean
  model_call_retention_days: number
  tutor_turn_retention_days: number
  report_retention_days: number
  updated_at: string
}

export type FamilyRule = {
  child_id: string
  latest_end_time: string | null
  max_learning_minutes: number
  task_segment_minutes: number
  break_minutes: number
  pet_min_minutes: number
  competition_minutes: number
  observation_mode: 'shadow' | 'gentle'
  reminder_budget_per_10m: number
  misclassification_shadow_threshold: number
  version: number
  updated_at: string
}

export type KnowledgeSource = {
  source_id: string
  child_id: string
  source_type: 'curriculum' | 'textbook_index' | 'school_material' | 'family_note'
  title: string
  subject: string
  grade_band: string
  publisher: string | null
  version_label: string | null
  source_ref: string
  effective_from: string | null
  effective_to: string | null
  license_status: 'public' | 'family_owned_index' | 'private_note' | 'unknown'
  review_status: 'approved' | 'quarantined' | 'superseded'
  content_hash: string
  excerpt_count: number
  imported_at: string
}

export type CurriculumCorpus = {
  corpus_id: string
  title: string
  corpus_type: 'seyk'
  source_ref: string
  license_status: 'public_personal_use' | 'unknown'
  review_status: 'approved_baseline' | 'quarantined'
  status: 'importing' | 'ready' | 'failed'
  resource_count: number
  transcript_count: number
  local_video_count: number
  topic_count: number
  model_drafted_topic_count: number
  verified_topic_count: number
  last_error_code: string | null
  imported_at: string
  updated_at: string
  import_status: 'processing' | 'completed' | 'failed' | null
  total_count: number | null
  processed_count: number | null
  import_error_code: string | null
}

export type CurriculumCoverage = {
  grade_title: string
  subject_title: string
  version_title: string
  semester: '上册' | '下册' | '全册' | '未知学期'
  topic_count: number
  resource_count: number
  transcript_count: number
  model_drafted_topic_count: number
  verified_topic_count: number
}

export type CurriculumSearchResult = {
  corpus_id: string
  corpus_title: string
  external_resource_id: string
  title: string
  description: string
  teacher: string | null
  stage_title: string
  grade_title: string
  subject_title: string
  version_title: string | null
  volume_title: string | null
  unit_path: string
  video_duration: number | null
  source_ref: string
  excerpt: string
  video_available: boolean
  video_path: string | null
}

export type CurriculumStandardSource = {
  source_id: string
  source_key: string
  jurisdiction: 'CN' | 'JS'
  source_type: 'national_curriculum_plan' | 'national_standard' | 'jiangsu_implementation' | 'jiangsu_textbook_catalog' | 'jiangsu_local_requirement'
  subject_title: string
  title: string
  version_label: string
  official_url: string
  pdf_sha256: string | null
  text_sha256: string | null
  page_count: number
  extracted_page_count: number
  status: 'downloaded' | 'ocr_ready' | 'structured' | 'failed'
  drafted_requirement_count: number
  verified_requirement_count: number
  imported_at: string
  updated_at: string
}

export type CurriculumStandardAlignmentSummary = {
  total_topic_count: number
  candidate_topic_count: number
  unresolved_topic_count: number
  no_standard_topic_count: number
  method_version: string
}

export type CurriculumInternalizationScope = {
  grade_title: string
  subject_title: string
  version_title: string
  semester: '上册' | '下册' | '全册' | '未知学期'
  unit_title: string
  topic_count: number
}

export type CurriculumInternalizationJob = {
  job_id: string
  corpus_id: string
  child_id: string
  grade_title: string
  subject_title: string
  version_title: string | null
  semester: string
  unit_title: string | null
  status: 'processing' | 'completed' | 'failed' | 'interrupted'
  total_batches: number
  processed_batches: number
  capability_count: number
  error_code: string | null
  model_provider: string | null
  model_name: string | null
  started_at: string
  completed_at: string | null
}

export type CurriculumCapability = {
  capability_id: string
  grade_title: string
  subject_title: string
  version_title: string | null
  semester: string
  unit_title: string
  capability_type: string
  title: string
  learning_objective: string
  prerequisites: string[]
  misconceptions: string[]
  mastery_criteria: string[]
  diagnostic_blueprint: string[]
  explanation_strategies: string[]
  transfer_goals: string[]
  source_titles: string[]
  standard_refs: string[]
  confidence: number
  maturity: 'model_drafted' | 'verified' | 'conflicted' | 'retired'
  review_note: string | null
  updated_at: string
}

export type CaptureArtifactSummary = {
  artifact_id: string
  artifact_type: string
  confidence: number | null
  status: string
  created_at: string
  local_path: null
  raw_file_stored: boolean
  extracted_text_chars: number
}

export type SystemHealth = {
  database_integrity: string
  database_size_bytes: number
  database_upgrade?: { status: 'prepared' | 'completed' | 'restored_after_failure' | 'unknown'; source_schema_version: number | null; target_schema_version: number | null; created_at: string | null; completed_at: string | null; error_type: string | null; snapshot_available: boolean } | null
  disk_free_bytes: number
  pending_report_jobs: number
  pending_agent_turns: number
  pending_content_generation_jobs?: number
  pending_deletion_jobs: number
  model_failures_last_7_days: number
  active_sensor_leases: number
  last_backup: { backup_id: string; created_at: string; integrity_status: string; reason: 'manual' | 'session_completed' | 'daily_automatic' | 'restore_recovery_point' | 'post_deletion' } | null
  maintenance: Record<'daily_backup' | 'retention_cleanup', { job_type: string; run_date: string; status: 'processing' | 'completed' | 'failed'; started_at: string; completed_at: string | null; error_code: string | null; error_message: string | null } | null>
  recent_maintenance_failures: Array<{ job_type: 'daily_backup' | 'retention_cleanup'; run_date: string; status: 'failed'; started_at: string; completed_at: string | null; error_code: string | null; error_message: string | null }>
  raw_media_files_stored: number
  devices: Array<{ device_type: 'camera' | 'document_camera' | 'microphone'; status: string; last_seen_at: string | null; application_reading: number; last_release_reason: string | null; binding_label: string | null }>
  device_incidents: Array<{ device_type: 'camera' | 'document_camera' | 'microphone'; error_code: string; message: string; status: 'open' | 'resolved'; occurred_at: string; resolved_at: string | null; child_nickname: string }>
  observation_quality?: Array<{
    child_nickname: string
    session_id: string | null
    sample_count: number
    usable_sample_count: number
    usable_ratio: number
    latest_state: ObservationState['state']
    latest_reason: string
    latest_observed_at: string
    qualification_status?: 'qualifying' | 'qualified' | 'unqualified' | 'paused' | 'unavailable' | 'not_recorded'
    scene_quality?: 'not_evaluated' | 'ready' | 'too_dark' | 'too_bright' | 'blurred' | 'unusable_frame'
    person_baseline?: 'not_evaluated' | 'established' | 'awaiting_person'
    qualification_source?: 'engine' | 'renderer' | null
    qualification_attempt_count?: number
    correction_count_7d?: number
    auto_shadowed?: boolean
    observation_mode?: 'gentle' | 'shadow'
  }>
  recent_model_failures: Array<{ purpose: string; provider: string; model: string; error_code: string | null; created_at: string }>
  recent_report_failures: Array<{ report_type: string; last_error: string | null; attempts: number; updated_at: string; child_nickname?: string }>
  recent_agent_failures: Array<{ intent: string; tool_error_code: string | null; created_at: string; child_nickname: string }>
  recent_controlled_game_failures?: Array<{ title: string; status: 'failed' | 'interrupted'; end_reason: string | null; updated_at: string; child_nickname: string }>
  active_controlled_games?: Array<{ game_session_id: string; title: string; started_at: string; ends_at: string; os_process_id: number | null; child_nickname: string; overdue: number }>
  recent_content_generation_failures?: Array<{ source: '错题变式' | '专项诊断'; error_code: string | null; attempt_count: number; updated_at: string; child_id: string; reference: string }>
  recent_deletion_failures: Array<{ deletion_id: string; scope: string; target_type: 'child_scope' | 'capture_artifact'; error_code: string | null; error_message: string | null; remaining_file_count: number; updated_at: string; child_nickname: string; historical?: boolean; retry_allowed?: boolean }>
}

export type BackupManifest = {
  backup_id: string
  path: string
  database_version: number
  size_bytes: number
  sha256: string
  integrity_status: string
  reason: 'manual' | 'session_completed' | 'daily_automatic' | 'restore_recovery_point' | 'post_deletion'
  created_at: string
}

export type PeriodReport = {
  period_report_id: string
  child_id: string
  report_type: 'weekly' | 'monthly'
  period_start: string
  period_end: string
  source: string
  content: {
    headline: string
    as_of_date: string
    coverage: { recorded_days: number; recorded_sessions: number; expected_calendar_days: number; evidence_count: number; sufficient_for_trend: boolean }
    daily_signals: Array<{ date: string; headline: string; session_count: number }>
    goal_progress?: Array<{ track: string; title: string; target_statement: string; target_minutes: number; target_sessions: number; completed_minutes: number; completed_sessions: number; evidence_items: number }>
    interpretation: string
    next_action: { what: string; duration_minutes: number; steps: string[]; stop_condition: string } | null
    parent_script: string[]
  }
  created_at: string
  updated_at: string
}

export type OcrArtifact = {
  artifact_id: string
  extracted_text: string
  confidence: number
  local_path: string | null
  status: 'completed' | 'quality_warning'
  details: {
    lines: Array<{ text: string; confidence: number | null; box?: number[][] | null }>
    engine: string
    ocr_confidence?: number
    quality?: { passed: boolean; issues: string[]; messages: string[]; metrics: Record<string, number>; version: string }
    cloud_vision_fallback?: {
      status?: 'not_needed' | 'needs_review' | 'needs_confirmation' | 'blocked'
      state?: string
      error_code?: string
      reason?: string | null
      text?: string
      blocks?: Array<{ kind?: 'text' | 'formula'; text?: string; confidence?: number }>
      privacy?: { uploaded?: string; original_frame_uploaded?: boolean; raw_image_stored?: boolean }
      provenance?: { source?: string; evidence_type?: string }
    }
    local_pipeline?: {
      engine: string
      route: string
      quality: {
        passed: boolean
        issues: string[]
        metrics: Record<string, number>
        transforms?: { orientation_degrees?: number; orientation_auto_corrected?: boolean }
        orientation_evidence?: { ambiguous: boolean; review_reasons?: string[] }
      }
      question_blocks: Array<{ text: string; line_count: number; confidence: number; bbox_xywh?: number[] | null; needs_manual_boundary_check: boolean; block_kind?: string; boundary_review_reasons?: string[] }>
      wrong_question_candidates?: Array<{
        text: string
        confidence: number
        ocr_confidence: number
        bbox_xywh: number[]
        red_pixel_count: number
        red_mark_count: number
        evidence: 'red_correction_mark_near_question'
        requires_child_confirmation: boolean
      }>
      manual_review: { required: boolean; reasons: string[]; handwriting: string; math: string }
      queue: { task_id: string; behavior_fps_cap: number }
    }
  }
}

export type DocumentMonitorState = 'no_page' | 'stabilizing' | 'writing' | 'occluded' | 'page_changed' | 'stable' | 'capture_failed'

export type DocumentMonitorEvent = {
  event_id: string
  event_type: DocumentMonitorState
  confidence: number
  metrics: Record<string, string | number | boolean>
  observed_at: string
}

export type DocumentPageRevision = {
  child_id?: string
  session_id?: string
  content_sha256?: string
  revision_id: string
  artifact_id: string
  revision_index: number
  previous_revision_id: string | null
  extracted_text: string
  ocr_confidence: number
  review_required: boolean
  marked_question_count?: number
  observed_question_count?: number
  queued_mistake_candidate_count?: number
  auto_saved_mistakes?: Array<{ mistake_id: string; has_question_crop?: boolean }>
  change_summary: { similarity: number; added_lines: string[]; removed_lines: string[]; changed: boolean }
  study_signal?: {
    kind: 'baseline' | 'progress_observed' | 'stable_no_change' | 'possible_stall' | 'observation_incomplete' | 'needs_review'
    confidence: number
    may_prompt_child: boolean
    reason: string
    stagnant_seconds?: number
  }
  intervention?: PendingBehaviorIntervention
  raw_image_transmitted: false
  page_source?: {
    version: 'document_page_source_v1'
    status: 'retained' | 'deleted'
    sha256: string
    byte_length: number
    media_type: 'image/jpeg' | 'image/png'
    encrypted: true
    local_only: true
    cloud_transmission_allowed: false
    retention_reason: 'accepted_document_page_revision'
  }
  created_at: string
}

export type DocumentOcrPreviewPhase = 'homework_capture' | 'plan_review' | 'study'

export type DocumentPreviewPageDraft = {
  page_token: string
  anchor_preview_id: string
  frame_count: number
  merged: boolean
  reason: string
  lines: {
    text: string
    status: 'current' | 'historical'
    conflict: boolean
    frame_sha256: string
    preview_id: string
    confidence: number
    original_polygon: number[][]
    canonical_polygon: number[][]
  }[]
}

export type DocumentOcrPreview = {
  preview_id: string
  created_at: string
  text: string
  question_count: number
  generation?: number
  frame_signature?: string | null
  client_action_id?: string
  inspection_turn_id?: string
  inspection_response_text?: string
  phase?: DocumentOcrPreviewPhase | null
  page_draft?: DocumentPreviewPageDraft | null
}

export type DocumentMonitorStatus = {
  generation?: number
  trial_capture_policy?: { enabled: boolean; interval_seconds: number; maximum_bytes: number; local_only: true }
  monitor_id?: string
  session_id: string
  status: 'not_started' | 'active' | 'paused' | 'stopped'
  current_state?: DocumentMonitorState | null
  latest_event: DocumentMonitorEvent | null
  latest_revision: DocumentPageRevision | null
  raw_frames_stored?: false
  cloud_image_calls?: 0
}

export type MistakeCaptureQueue = OcrArtifact & {
  pending_count: number
  candidate_count: number
}

export type VoiceInput = {
  voice_input_id: string
  capture_binding?: {
    status: 'completed'
    local_recovery_only?: true
    ordinary_response_binding?: import('./ordinaryResponseGrant').OrdinaryResponseBinding
    task_choice_reference?: import('./voiceCaptureContext').VoiceTaskChoiceReference
    activity_validation?: 'valid' | 'stale'
    activity_reference?: import('./voiceCaptureContext').VoiceActivityReference
    pending_evaluation_turn_id?: string
    prompt_id: string | null
    item_version: string | null
    raw_audio_available: boolean
    speaking_media_status?: string
    question_reference?: import('./voiceCaptureContext').VoiceQuestionReference | null
    question_validation?: 'valid' | 'stale'
  }
  client_action_id?: string
  transcript: string
  duration_ms: number
  model_version: string
  cloud_used: boolean
  local_path: string | null
  status: 'completed' | 'ignored'
}

export type HouseholdEntryReceipt={version:1;origin_scope:'unassigned_household';storage_generation:string;receipt_id:string;client_action_id:string;issued_at_ms:number;transcript_sha256:string;model_version:string;source_kind:'household_local_transcript'}
export type HouseholdNativeEntry={entry_receipt:HouseholdEntryReceipt;transcript:string}
export type StandbyVoiceInput = {
  entry_receipt?:HouseholdEntryReceipt
  client_action_id: string
  transcript: string
  duration_ms: number
  model_version: string
  status: 'completed'
  persisted: false
  cloud_used: false
}

export type PetVocabularyCard = {
  prompt_session_id?: string
  card_version?: string
  card_id: string
  review_mode: 'recognition' | 'recall'
  prompt: string
  focus_kind: 'word' | 'phrase'
  due_at: string
  source_ref: string
}

export type PetVocabularySummary = {
  deck_size: number
  new_count: number
  learning_count: number
  due_count: number
  total_attempts: number
  lapse_count: number
}

export type PetVocabularyReview = {
  result: {
    attempt_id: string
    card_id: string
    review_mode: 'recognition' | 'recall'
    correct: boolean
    expected: string | string[]
    next_due_at: string
    interval_days: number
    feedback: string
  }
  next_card: PetVocabularyCard | null
  summary: PetVocabularySummary
}

export type MistakeDialogue = {
  answer_binding?: { session_id: string; child_id: string; mistake_id: string; phase: string; item_id: string; source_version: string } | null
  dialogue_binding?: import('./voiceCaptureContext').MistakeVoiceBinding | null
  session_id: string
  child_id: string
  candidate_id: string | null
  mistake_id: string | null
  phase: string
  response_text: string
  state: {
    artifact_id?: string
    question_text?: string
    submitted_answer?: string
    subject?: string
    knowledge_title?: string
    teaching_turn_id?: string
    question_confirmation?: { mode?: string; review_id: string; version: string; question: string; original_answer?: string; replacement_answer?: string }
    foundation_hold?: { reason: string; message?: string; utterance?: string }
    next_activity?: MistakeNextActivity
  }
}

export type AgentTurn = {
  session_id?: string
  child_id?: string
  local_evaluation?: WritingFeedbackNotice
  requires_control_admission?: boolean
  after_execution?: 'stop' | 'replan'
  agent_turn_id: string
  transcript: string
  intent: string
  response_text: string
  action: {
    type: string
    operation?: 'capture'|'analyze'|'practice'|'coach'|'answer'|'review_today'|'select'
    selector?: string|number
    count?: number
    style?: 'direct'|'socratic'
    utterance?: string
    mistake_learning_action_id?: string
    mistake_learning_source?: {mistake_id:string;child_id:string;session_id:string;source_revision:string;
      focus_generation:number|null;learning_session_id:string|null;revision:number;
      practice_item_id:string|null;focus_created_at:string;practice_created_at:string|null}|null
    destination?:'learning'|'mistake_book'
    voice_input_id?: string
    source_voice_input_id?: string | null
    voice_input_binding?: { child_id: string; session_id: string; question_validation: 'valid' | 'stale';
      question_reference?: import('./voiceCaptureContext').VoiceQuestionReference | null }
    pending_evaluation_control?: { version: 'pending_evaluation_control_v1'; evaluation_turn_id: string; mode: 'control_only' }
    controlled_stop_binding?: { child_id: string; session_id: string; kind: 'content' | 'games'; activity_id: string }
    controlled_launch_cancel_binding?: {child_id:string;session_id:string;kind:'video'|'game';request_id:string;
      source_turn_id:string;source_action_sha256:string;created_at:string;failed_at:string}
    controlled_voice_binding?: { child_id: string; session_id: string; kind: 'content' | 'games'; source_version: string;
      query?: string; selection_binding?: { resource_id: string; source_version: string } }
    selection_binding?: { resource_id: string; source_version: string }
    controlled_stop_readback?: { child_id: string; session_id: string; kind: 'content' | 'games'; activity_id: string; status: string; started_at: string }
    content_session_id?: string
    game_session_id?: string
    local_recovery_control?: { version: 'local_voice_recovery_v1'; mode: 'safety_only' }
    pending_evaluation_query?: { turn_id: string }
    writing_ocr_confirmation?: { confirmation_id: string; binding_version: string }
    native_writing_target?: import('./nativeWritingFlow').NativeWritingControlTarget
    capability?: string
    diagnostic_prompt_id?: string | null
    require_unexposed?: boolean
    knowledge_node_id?: string
    diagnostic_item_version?: string | null
    vocabulary_prompt_id?: string | null
    vocabulary_card_version?: string | null
    tutor_playback_id?: string | null
    tutor_source_version?: string | null
    question_relation?: 'followup'
    tutor_followup_binding?: { turn_id: string; source_sha256: string } | null
    candidate?: string
    observation_only?: boolean
    mistake_recapture_target?: { mistake_id: string; source_version: string }
    defer_document_review?: boolean
    local_only?: boolean
    cloud_transmission_allowed?: boolean
    device?: 'camera' | 'document_camera' | 'all'
    source?: string
    suppress_speech?: boolean
    speech_echo_filter?: {
      removed_characters: number
      raw_transcript_sha256: string
    }
    execution_location?: 'server' | 'local_model' | 'client_device_bridge'
    local_learning_context?: {
      status: 'matched' | 'no_match' | 'unavailable'
      cloud_transmission_allowed: false
      mastery_update_allowed: false
      audit_event_id: string | null
      matched_node_count: number
      approved_node_count: number
      hypothesis_count: number
      diagnostic_prompt: string | null
      course_context?: {
        status: 'matched' | 'no_match' | 'unavailable'
        cloud_transmission_allowed: false
        mastery_update_allowed: false
        audit_event_id?: string
        diagnostic_prompt?: string | null
        evidence: Array<{
          segment_id: string
          resource_id: string
          title: string
          subject: string
          grade: string
          page_number: number | null
          source_ref: string
          excerpt: string
          excerpt_start_char: number
          review_status: string
          usage: 'source_comparison_not_verified_curriculum_alignment'
        }>
      }
    }
    target?: string
    target_child_id?: string
    target_child_name?: string
    question?: string
    ocr_confirmation_id?: string
    tutor_request_action_id?: string
    document_goal_root_turn_id?: string
    document_goal_confirmation_root?: string
    document_goal_confirmation_revision?: number
    document_goal_confirmation_blocked?: boolean
    hint_level?: number
    correction?: string
    corrected_text?: string
    answer?: string
    track?: 'school' | 'pet' | 'competition_math'
    outcome?: 'understood' | 'still_stuck'
    feedback_text?: string
    field?: 'learned' | 'difficult' | 'next_action'
    value?: string
    skipped?: boolean
    error_type?: string
    explanation?: string
    notes?: string
    scope?: string
    subject?: string
    summary?: string
    title?: string
    reason?: string
    expected_minutes?: number
    task_type?: 'written' | 'memorization' | 'reading' | 'correction' | 'review' | 'preview' | 'test_prep'
    range_text?: string | null
    due_at?: string | null
    special_requirements?: string | null
    decision?: 'confirm' | 'reject' | 'exclude' | 'continue_today' | 'completed_elsewhere' | 'cancelled'
    strategy?: 'school_first' | 'short_school_first'
    completion_evidence_type?: 'child_checked' | 'voice_confirmed' | 'needs_correction'
    energy_level?: 'low' | 'medium' | 'high'
    fixed_commitment_minutes?: number
    fixed_commitment_note?: string
    tomorrow_event_subject?: string
    tomorrow_event_title?: string
    tomorrow_event_minutes?: number
    feedback?: 'accepted' | 'declined'
    response_kind?: 'later' | 'less_like_this' | 'accepted'
    command?: 'pause' | 'resume' | 'previous' | 'restart' | 'cancel' | 'inspect' | 'review' | 'reload' | 'sync' | 'open' | 'set_answer' | 'correct_prompt' | 'save' | 'skip' | 'close'
    pet_listening_binding?: { version: string; child_id: string; session_id: string; media_id: string;
      prompt_id: string; item_version: string; source_version: string; generation: number; expected_revision: number }
    until?: string
    activity_type?: 'interactive_story' | 'word_chain' | 'riddle' | 'movement' | 'tidy_challenge'
    topic?: string
    query?: string
  }
  orchestrator_version: string
  runtime_execution_location?: 'server' | 'local_model' | 'client_device_bridge'
  runtime_capability?: string
  tool_status: 'pending' | 'completed' | 'failed'
  tool_error_code: string | null
  completed_at: string | null
  created_at: string
}

export type AgentRuntimeStatus = {
  pending_learning_capture?: import('./voiceCaptureContext').PendingLearningCapture | null
  continuation?: {
    status: string
    session_id?: string
    child_id?: string
    next_turn_id?: string
    replay_allowed?: boolean
    local_only?: boolean
    cloud_transmission_allowed?: boolean
  }
  document_guidance_preferences?: { child_id: string; occlusion_reminders_enabled: boolean; settings_saved: boolean; source: 'default' | 'saved' }
  session_id: string
  active_objective: null | {
    objective_id: string
    objective_type: 'learning' | 'execution' | 'habit' | 'companion'
    summary: string
    success_criteria: string[]
    status: string
    phase?: string
  }
  current_step: null | {
    step_id: string
    capability_name: string
    status: string
    plan_status?: string
    expected_evidence: string[]
  }
}

export type ControlledContentRule = {
  child_id: string
  enabled: boolean
  max_daily_minutes: number
  max_item_minutes: number
  updated_at: string
}

export type ControlledContentCandidate = {
  row_id: number
  title: string
  subject_title: string
  grade_title: string
  unit_path: string
  video_duration: number | null
  local_video_available: 0 | 1
  approval_status: 'approved' | 'revoked' | 'not_approved'
  max_minutes: number | null
  note: string | null
}

export type ControlledContentPlayback = {
  replayed?: boolean
  launch_allowed?: boolean
  content_session_id: string
  child_id: string
  session_id: string
  curriculum_row_id: number
  title: string
  local_path: string
  status: 'active' | 'completed' | 'stopped' | 'expired' | 'interrupted' | 'failed'
  allowed_minutes: number
  actual_seconds: number
  paused_plan_item_id: string | null
  started_at: string
  ends_at: string
  ended_at: string | null
  end_reason: string | null
  remaining_seconds: number
  task_resumed?: boolean
}

export type ControlledGameRule = {
  child_id: string
  enabled: boolean
  max_daily_minutes: number
  max_item_minutes: number
  updated_at: string
}

export type ControlledGameApproval = {
  approval_id: string
  child_id: string
  title: string
  executable_path: string
  status: 'approved' | 'revoked'
  max_minutes: number
  note: string | null
  file_size: number
  file_mtime_ns: number
  created_at: string
  updated_at: string
}

export type ControlledGameSession = {
  replayed?: boolean
  launch_allowed?: boolean
  game_session_id: string
  child_id: string
  session_id: string
  approval_id: string
  title: string
  executable_path: string
  status: 'active' | 'completed' | 'stopped' | 'expired' | 'interrupted' | 'failed'
  allowed_minutes: number
  actual_seconds: number
  paused_plan_item_id: string | null
  os_process_id: number | null
  started_at: string
  ends_at: string
  ended_at: string | null
  end_reason: string | null
  remaining_seconds: number
  task_resumed?: boolean
}

export type ObservationState = {
  state: 'unknown' | 'away' | 'writing_like' | 'paused_like' | 'present'
  confidence: number
  reason: string
  duration_ms: number
  model_version: string
  shadow: boolean
  raw_frame_stored: boolean
  state_event_id?: string | null
  observed_at?: string
  longitudinal_recorded?: boolean
  behavior_analysis_fps_cap?: number
  analysis_skipped?: 'fps_budget'
  observation_ready: boolean
  scene_quality: {
    status: 'ready' | 'too_dark' | 'too_bright' | 'blurred'
    message: string
  }
  person_baseline: {
    status: 'not_evaluated' | 'established' | 'awaiting_person'
    message: string
  }
  metrics: { brightness?: number; sharpness?: number; motion?: number; input_width?: number; input_height?: number; source_width?: number; source_height?: number; input_fps_cap?: number }
  intervention: null | {
    intervention_id: string
    message: string
    expires_at: string
    requires_child_feedback: boolean
  }
  analysis_scope?: 'framing_only'
  qualification?: ObservationQualification | ObservationFramingQualification
}

/** An unbound frame is preview telemetry, never a persisted qualification. */
export type ObservationFramingQualification = {
  status: 'framing_only'
  scene_quality: ObservationQualification['scene_quality']
  person_baseline: 'not_evaluated'
  longitudinal_inference_enabled: false
  raw_frame_stored: false
}

export type ObservationQualification = {
  status: 'qualifying' | 'qualified' | 'unqualified' | 'paused' | 'unavailable'
  scene_quality: 'not_evaluated' | 'ready' | 'too_dark' | 'too_bright' | 'blurred' | 'unusable_frame'
  person_baseline: 'not_evaluated' | 'established' | 'awaiting_person'
  reason: string
  source: 'engine' | 'renderer'
  attempt_count: number
  qualified_at: string | null
  updated_at: string | null
  raw_frame_stored: false
  longitudinal_inference_enabled: boolean
}

export type PendingBehaviorIntervention = {
  intervention_id: string
  message: string
  expires_at: string
  requires_child_feedback: boolean
  candidate_state?: string
  trigger_source?: 'document_monitor' | 'local_observation'
}

export type PendingBehaviorInterventionResponse = {
  status: 'pending' | 'no_pending' | 'expired'
  intervention?: PendingBehaviorIntervention
  expires_at?: string
}

export type BehaviorReplay = {
  child_id: string
  session_id: string | null
  events: Array<{ event_id: string; occurred_at: string; event_type: string; source: string; confidence: number; missing_sources: string[]; alternative_explanations: string[]; retracted?: { note: string } }>
  states: Array<{ state_id: string; event_id: string; occurred_at: string; state: string; probability: number; effective_probability?: number; alternatives: string[]; evidence: { event_type: string; source: string; missing_sources: string[] }; retracted?: { note: string } }>
  interventions: Array<{ intervention_id: string; decided_at: string; candidate_state: string; message: string; feedback: 'accepted' | 'declined' | null; feedback_note?: string | null; outcome: null | { state: string; probability: number; observed_at: string }; status?: 'active' | 'withdrawn' | 'expired'; withdrawal_reason?: string | null; withdrawal_pending?: boolean }>
}

export type BehaviorBaseline = {
  status: 'ready' | 'insufficient_history'
  days_observed: number
  minimum_days?: number
  switch_count_baseline?: { mean: number; stddev: number }
  feature_baselines?: Record<string, { mean: number; stddev: number }>
  habit_trends?: Record<string, { status: 'ready' | 'insufficient_history'; minimum_days?: number; prior_daily_mean?: number; recent_daily_mean?: number; delta?: number; direction?: 'increasing' | 'decreasing' | 'stable' }>
  interpretation_boundary?: string
}

export type BehaviorPersonalization = {
  minimum_feedback_samples: number
  boundary: string
  items: Array<{ candidate_state: string; sample_size: number; accepted: number; declined: number; decline_rate: number | null; effect_samples: number; helpful_effects: number; strategy: 'default_ask_first' | 'low_frequency_ask_first'; cooldown_minutes: number; daily_cap: number; adjusted: boolean; explanation: string[] }>
}

export type HabitExperiment = {
  experiment_id: string
  child_id: string
  target_state: string
  title: string
  child_action: string
  success_signal: string
  duration_days: number
  baseline: { feature?: string; average?: number | null; observed_days?: number }
  status: 'proposed' | 'active' | 'completed' | 'declined' | 'stopped'
  stop_pending?: boolean
  stop_retrying?: boolean
  proposed_at: string
  started_at: string | null
  ends_at: string | null
  closed_at: string | null
  outcome: null | { observed_days: number; baseline_average: number | null; current_average: number | null; direction: 'improving' | 'not_yet_clear' }
}

export type HabitExperimentList = {
  child_id: string
  items: HabitExperiment[]
  boundary: string
}

export type MistakeStatus =
  | 'captured'
  | 'awaiting_confirmation'
  | 'diagnosing'
  | 'remediating'
  | 'near_transfer'
  | 'mixed_transfer'
  | 'delayed_verification'
  | 'fragile'
  | 'mastered'
  | 'relapsed'
  | 'excluded'

export type MistakeMasteryGate = {
  error_type: string
  requirements: Record<string, number>
  satisfied: Record<string, number>
  missing: string[]
  delayed_48h_due_at: string | null
  delayed_7d_due_at: string | null
}

export type MistakeItem = {
  mistake_id: string
  child_id: string
  cluster_id: string
  diagnostic_item_id: string | null
  source_evidence_id: string | null
  subject: string
  title: string
  prompt_summary: string
  submitted_answer: string
  source_type: string
  source_ref: string | null
  confidence: number
  status: MistakeStatus
  diagnosis_status: 'suspected' | 'emerging_pattern' | 'confirmed_gap' | 'improving' | 'stable'
  occurrence_count: number
  relapse_count: number
  confirmed_at: string | null
  created_at: string
  updated_at: string
  knowledge_node: { knowledge_node_id: string; title: string; code: string; track: string }
  diagnosis: null | { error_type: string; explanation: string; confidence: number; status: string; created_at: string }
  mastery_gate: MistakeMasteryGate | null
  next_action?: string
  attempt_result?: { correctness: number | null; feedback: string; independent: boolean }
}

export type MistakeList = {
  items: MistakeItem[]
  counts: { awaiting_confirmation: number; today_strengthening: number; due_review: number; mastered: number }
}

export type MistakeNextActivity = {
  kind: 'confirmation' | 'diagnosis' | 'remediation' | 'attempt' | 'scheduled' | 'preparing_variant' | 'awaiting_parent_review' | 'blocked' | 'mastered' | 'complete'
  stage: string | null
  due_at?: string | null
  reason?: string
  candidate_count?: number
  item: null | { diagnostic_item_id: string; prompt: string; answer_type: string; difficulty: number; assessment_mode: string }
}

export type VariantCandidate = {
  candidate_id: string
  job_id: string
  mistake_id: string
  child_id: string
  knowledge_node_id: string
  stage: 'near_transfer' | 'representation_transfer' | 'discrimination' | 'mixed_transfer' | string
  prompt: string
  answer_type: 'text'
  expected_answer: { keywords: string[]; exact_answers?: string[]; local_rule?: string; answer_format?: string }
  source_context?: { prompt: string | null; kind: string; valid: boolean; cloud_transmission_allowed: false }
  explanation: string
  difficulty: number
  validation: { passed: boolean; errors: string[]; ruleset: string }
  status: 'needs_review' | 'promoted' | 'rejected'
  diagnostic_item_id: string | null
  reviewed_at: string | null
  review_note: string | null
  created_at: string
}

export type VariantGenerationJob = {
  job_id: string
  status: 'pending' | 'processing' | 'completed' | 'blocked' | 'failed'
  error_code: string | null
  generation_mode: 'local' | 'model'
  candidate_count: number
}

export type SpecializedSupplyGap = {
  domain_id: string
  subject: string
  label: string
  active_count: number
  pending_review_count: number
  target_count: number
  missing_count: number
  missing_stages: Array<'parallel' | 'discrimination' | 'transfer'>
  promoted_stages: Array<'parallel' | 'discrimination' | 'transfer'>
  ready: boolean
  seed_available: boolean
}

export type SpecializedSupplyCandidate = {
  candidate_id: string
  job_id: string
  child_id: string
  track: 'pet' | 'competition_math' | 'junior_exam'
  subject: string
  domain_id: string
  seed_item_id: string
  stage: 'parallel' | 'discrimination' | 'transfer'
  prompt: string
  expected_answer: { keywords?: string[]; exact_answers?: string[]; task_points?: string[]; [key: string]: unknown } | null
  review_workflow?: string
  explanation: string
  difficulty: number
  validation: { passed: boolean; errors: string[]; ruleset: string }
  status: 'needs_review' | 'promoted' | 'rejected'
  diagnostic_item_id: string | null
  reviewed_at: string | null
  review_note: string | null
  created_at: string
}

export type ExamBlueprintSection = {
  section_id: string
  title: string
  duration_minutes: number
  part_count: number | null
  item_target: number | null
  max_score: number | null
  weight_percent: number | null
  delivery_mode: string
}

export type ExamBlueprintReadiness = {
  child_id: string
  track: 'pet' | 'competition_math' | 'junior_exam'
  subject: string
  blueprint: {
    blueprint_id: string
    title: string
    version: string
    effective_year: number
    authority: string
    fidelity: string
    score_boundary: string
    source_urls: string[]
    sections: ExamBlueprintSection[]
  }
  sections: Array<{
    section_id: string
    title: string
    item_target: number
    active_count: number
    pending_review_count: number
    deficit: number
    pending_generation_count: number
    ready: boolean
  }>
  ready: boolean
  active_item_count: number
  required_item_count: number
  pending_review_count: number
}

export type GoalProgramCycle = GoalProgressExplanation & {
  roadmap?: import('./GoalRoadmapPanel.types').GoalRoadmap
  cycle_id: string
  program_id: string
  child_id: string
  track: string
  title: string
  target_statement: string
  target_horizon: Record<string, unknown>
  deadline: null | {
    status: 'active' | 'due_today' | 'overdue' | 'invalid'
    target_date: string
    days_remaining?: number
    weeks_remaining?: number
    target_score?: number | null
    target_score_label?: string | null
  }
  source_type: string
  week_start: string
  week_end: string
  target_minutes: number
  target_sessions: number
  completed_minutes: number
  completed_sessions: number
  remaining_minutes: number
  remaining_sessions: number
  status: 'not_started' | 'on_track' | 'behind' | 'completed'
  focus: GoalFocus
  evidence_watermark: { evidence_items: number; latest: string | null }
}

export type SpecializedRoadmap = {
  child_id: string
  track: 'pet' | 'competition_math' | 'junior_exam'
  phase: 'baseline' | 'foundation' | 'strengthening' | 'transfer_and_simulation'
  reference_readiness: number | null
  evidence_coverage: number
  evidence_domain_count: number
  total_domain_count: number
  catalog_domain_count?: number
  catalog_coverage?: number
  catalog_gap_domains?: string[]
  target?: null | { statement: string; target_date: string | null; days_remaining: number | null; target_score: number | null; target_score_label: string | null; weekly_minutes: number; weekly_sessions: number }
  stage_plan?: { stage: string; focus: string; reassessment_rule: string }
  stage_requirements?: { independent_check_required: boolean; distinct_transfer_required: boolean;
    delayed_retention_required: boolean; certified_stage: null; reason: string }
  assessment_history?: Array<{ assessment_id: string; assessment_type: string; subject: string; occurred_at: string; score: number | null; max_score: number | null; percentage: number | null; change_from_previous?: number | null }>
  whole_exam_analysis?: { latest_exam_review?: { score_status: string; verified_items: number; answered_items: number; total_items: number; current_evidence_status: string }; exam_reference_weak_domains?: Array<{ domain_id: string; subject?: string; title?: string; reference_score?: number }>; exam_reference_developing_domains?: Array<{ domain_id: string; subject?: string; title?: string; reference_score?: number }>; latest_exam: null | { score_scope?: string; score_boundary?: string; mastery_claim_allowed?: boolean; assessment_type: string; subject: string; occurred_at: string; score: number; max_score: number; percentage: number }; weak_domains: Array<{ domain_id: string; subject: string; label: string; reference_score: number }>; developing_domains: Array<{ domain_id: string; subject: string; label: string; reference_score: number }>; interpretation: string }
  forecast: { status: 'insufficient_evidence' | 'reference_only'; statement: string }
  domains: Array<{
    domain_id: string
    subject: string
    label: string
    reference_score: number | null
    status: 'strong' | 'observed' | 'developing' | 'weak' | 'baseline_needed'
    evidence_type: 'none' | 'assessment' | 'learning' | 'assessment_and_learning' | 'parent_reported_assessment' | 'qualified_guided_assessment' | 'qualified_independent_observations'
    priority_status?: string
    reference_score_basis?: string
    priority_reason?: string
    evidence_conflict?: boolean
    assessment_reference_score?: number | null
    assessment_age_days?: number | null
    assessment_planning_status?: 'current_reference' | 'expired_reference' | 'none'
    assessment_at: string | null
    learning_evidence_count: number
    diagnostic_item_count?: number
    actionable?: boolean
  }>
  next_actions: Array<{
    priority: number
    domain_id: string
    subject: string
    title: string
    minutes: number
    action_type: 'baseline' | 'remediation' | 'transfer' | 'independence_check'
    selection_reason?: string | null
    completion_standard: string
  }>
  generated_at: string
}

export type ChildModelCorrectionTarget = 'explanation_pacing' | 'task_segment_minutes' | 'interaction_tone' | 'mistake_priority'

export type ChildModelCorrection = {
  correction_id: string
  child_id: string
  target_key: ChildModelCorrectionTarget
  corrected_value: string | number
  reason: string
  status: 'active' | 'revoked' | 'expired'
  expires_at: string | null
  created_at: string
  revoked_at: string | null
  revoked_reason: string | null
  client_action_id: string
  idempotent?: boolean
}

export type LongitudinalChildModel = {
  snapshot_id: string
  child_id: string
  model_version: string
  evidence_fingerprint: string
  is_current: boolean
  created_at: string
  payload: {
    as_of: string
    evidence_level: 'cold_start' | 'emerging' | 'established'
    learning: {
      evidenced_node_count: number
      subjects: Array<{ subject: string; node_count: number; weak_nodes: number; stable_nodes: number; due_reviews: number; average_mastery: number; average_hint_dependency: number; average_confidence: number }>
      reliable_feedback?: {
        version: string
        evidence_fingerprint: string
        window_days: number
        status: 'needs_baseline' | 'evidence_available'
        nodes: GoalLearningOutcome['nodes']
        cloud_transmission_allowed?: false
        mastery_claim_allowed?: false
      } | null
    }
    mistakes: { status_counts: Record<string, number>; top_clusters: Array<{ subject: string; title: string; diagnosis_status: string; occurrence_count: number; relapse_count: number }> }
    execution: { window_days: number; sessions: number; planned_tasks: number; completed_tasks: number; deferred_tasks: number; actual_minutes: number; completion_rate: number | null }
    behavior: { observed_days: number; signals: Record<string, number>; uncertainty?: string; habit_experiments?: { open: { experiment_id: string; title: string; target_state: string; status: string; started_at: string | null } | null; completed_count: number; improving_count: number } }
    specialized_progress?: Array<{ track: string; subject: string; assessment_type: string; score: number | null; max_score: number | null; domain_count: number; weak_domain_count: number; occurred_at: string }>
    companion_balance?: {
      version?: string
      window_days: number; activity_sessions: number; video_sessions: number; game_sessions: number
      activity_minutes: number; video_minutes: number; game_minutes: number; total_minutes: number
      total_sessions: number; average_daily_minutes: number; child_ended_sessions: number; bounded_end_sessions: number
      not_dispatched_launches?: number
      not_dispatched_evidence_fingerprint?: string
      return_to_learning_sessions: number
      self_report_sessions?: number; parent_observed_sessions?: number; task_resume_receipt_sessions?: number
      verified_child_return_sessions?: number; habit_success_sessions?: number; legacy_unverified_sessions?: number
      boundary_status: 'within_bounds' | 'review_needed' | 'return_evidence_missing' | 'no_evidence' | 'return_observed' | 'return_evidence_partial'
      guidance: string
    }
    parent_feedback?: { window_days: number; feedback_count: number; executed_count: number; average_effectiveness: number | null; high_stress_count: number }
    tutor_feedback: { total: number; understood: number; still_stuck: number }
    tutor_outcomes?: {
      outcome_count: number; mapped_count: number; unmapped_count: number
      recent_outcomes: Array<{ outcome_id: string; summary: string; created_at: string; outcome_level: string; mapping_status: string; evidence_status: string }>
    }
    support_strategy: { explanation_pacing: string; task_segment_minutes: number; interaction_tone?: string; mistake_priority: string; require_independent_check: boolean; reason: string; parent_correction_count?: number; correction_sources?: Array<{ correction_id: string; target_key: ChildModelCorrectionTarget; reason: string; expires_at: string | null }>; strategy_evolution?: { status: string; candidate_version: string | null; feedback_count: number; understood_rate?: number | null; rollback_reason?: string | null } }
    parent_corrections?: ChildModelCorrection[]
    limitations: string[]
  }
}

export type WeaknessCluster = {
  cluster_id: string
  knowledge_node_id: string
  subject: string
  title: string
  diagnosis_status: 'suspected' | 'emerging_pattern' | 'confirmed_gap' | 'improving' | 'stable'
  occurrence_count: number
  relapse_count: number
  updated_at: string
  current_evidence?: CurrentLearningEvidence
  mastery_probability: number | null
  hint_dependency: number | null
  status_confidence: number | null
  next_review_at: string | null
}

export type MistakeKnowledgeCandidate = {
  knowledge_node_id: string
  node_type: string
  track: string
  subject: string
  grade_band: string
  code: string
  title: string
  description: string
  match_score: number
}

export type MistakeCaptureCandidate = {
  candidate_id: string
  artifact_id: string
  child_id: string
  question_index: number
  question_text: string
  region: { x: number; y: number; width: number; height: number } | null
  confidence: number
  status: 'pending' | 'saved' | 'skipped'
  mistake_id: string | null
  created_at: string
  updated_at: string
}

export type OcrReviewItem = {
  review_id: string
  artifact_id: string
  child_id: string
  review_kind: 'text' | 'handwriting' | 'formula' | 'formula_question' | 'marking'
  region_index: number
  region: number[][] | null
  proposed_text: string
  confidence: number
  status: 'pending' | 'confirmed' | 'skipped'
  corrected_text: string | null
  created_at: string
  updated_at: string
  raw_media_retained: false
}
export type VoiceInteractionContext = {
  phase: 'naming' | 'confirm_name' | 'tutorial'
  step?: string
  pending_name?: string
  wake_word?: string
}
export type VoiceOnboardingRestartOutput = {
  onboarding_restarted: true
} & ({ onboarding_phase: 'naming'; onboarding_step: null }
  | { onboarding_phase: 'active'; onboarding_step: 'wake_practice' | 'study_practice' | 'document_practice' | 'finish_practice' })
export type VoiceInteractionRequest = { transcript: string; client_action_id: string; context: VoiceInteractionContext; voice_input_id?: string }
export type VoiceInteractionRecovery = {
  session_id: string; child_id: string
  status: 'none' | 'retryable' | 'pending' | 'outcome_unknown' | 'completed'
  original_action_key: string | null; request: VoiceInteractionRequest | null
  response: VoiceInteractionResponse | null; reason: string | null
}
export type VoiceInteractionDeferredProgress = { childId: string; version: 1; step: 'deferred'; updatedAt: string }
export type VoiceInteractionHandoffStatus = 'none' | 'needs_deferral' | 'ready' | 'received' | 'interpreted' | 'completed' | 'failed' | 'outcome_unknown' | 'invalidated'
export type VoiceInteractionHandoff = {
  session_id: string; child_id: string; status: VoiceInteractionHandoffStatus
  handoff_id: string | null; agent_action_id: string | null
  deferred_progress: VoiceInteractionDeferredProgress | null
  request: VoiceInteractionRequest | null
  receipt: import('./agentRequestRecovery').AgentRequestReceipt | null
  reason: string | null
}
export type VoiceInteractionResponse = {
  route: 'agent' | 'onboarding' | 'clarify'
  action: 'name_candidate' | 'confirm_name' | 'reject_name' | 'skip' | 'repeat' | 'advance' | 'none'
  candidate?: string
  response_text: string
  source: 'cloud' | 'unavailable' | 'local_privacy'
}

export type TutorPlaybackCheckpoint = {
  playback_id: string; turn_id: string; child_id: string; session_id: string;
  source_version: string; presentation_version: string; generation: number; revision: number;
  status: 'paused' | 'playing' | 'playback_finished' | 'cancelled' | 'invalidated';
  segment_index: number; segment_started: boolean; reason: string | null;
  presentation: TutorBlackboardPresentation | null; evidence_kind: 'playback_only'; replayed: boolean;
}
