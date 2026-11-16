BEGIN TRANSACTION;
CREATE TABLE agent_commitment (
  commitment_id TEXT PRIMARY KEY,
  objective_id TEXT REFERENCES agent_objective(objective_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  commitment_type TEXT NOT NULL CHECK(commitment_type IN ('review', 'reminder', 'follow_up', 'natural_end')),
  due_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'due', 'completed', 'cancelled')),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE agent_decision_record (
          decision_record_id TEXT PRIMARY KEY,
          plan_id TEXT NOT NULL UNIQUE REFERENCES agent_plan(plan_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          agent_turn_id TEXT NOT NULL REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
          snapshot_id TEXT,
          evidence_fingerprint TEXT,
          binding_status TEXT NOT NULL CHECK(binding_status IN ('bound_current','legacy_unbound')),
          strategy_version TEXT NOT NULL,
          strategy_experiment_id TEXT,
          selected_capability TEXT NOT NULL,
          selected_capability_version TEXT NOT NULL,
          candidate_set_json TEXT NOT NULL,
          constraints_json TEXT NOT NULL,
          adoption_reason TEXT NOT NULL,
          rejected_candidates_json TEXT NOT NULL,
          expected_outcomes_json TEXT NOT NULL,
          stop_conditions_json TEXT NOT NULL,
          rollback_action TEXT NOT NULL,
          planner_version TEXT NOT NULL,
          contract_version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
CREATE TABLE agent_objective (
  objective_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
  objective_type TEXT NOT NULL CHECK(objective_type IN ('learning', 'execution', 'habit', 'companion')),
  summary TEXT NOT NULL,
  success_criteria_json TEXT NOT NULL,
  phase TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'completed', 'cancelled', 'failed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  last_agent_turn_id TEXT REFERENCES agent_turn(agent_turn_id) ON DELETE SET NULL
);
CREATE TABLE agent_outcome (
  outcome_id TEXT PRIMARY KEY,
  objective_id TEXT NOT NULL REFERENCES agent_objective(objective_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  outcome_level TEXT NOT NULL CHECK(outcome_level IN ('exposed', 'understood_now', 'independent', 'transferred', 'retained', 'executed', 'recovered', 'completed')),
  evidence_refs_json TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE agent_outcome_attribution (
          attribution_id TEXT PRIMARY KEY,
          outcome_id TEXT NOT NULL UNIQUE REFERENCES agent_outcome(outcome_id) ON DELETE CASCADE,
          objective_id TEXT NOT NULL REFERENCES agent_objective(objective_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          agent_turn_id TEXT NOT NULL REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
          plan_id TEXT NOT NULL REFERENCES agent_plan(plan_id) ON DELETE CASCADE,
          step_id TEXT NOT NULL REFERENCES agent_step(step_id) ON DELETE CASCADE,
          decision_record_id TEXT,
          snapshot_id TEXT,
          evidence_fingerprint TEXT,
          binding_status TEXT NOT NULL CHECK(binding_status IN ('bound_decision','legacy_unbound')),
          strategy_version TEXT NOT NULL,
          strategy_experiment_id TEXT,
          capability_name TEXT NOT NULL,
          capability_version TEXT NOT NULL,
          task_binding_json TEXT NOT NULL,
          expected_outcomes_json TEXT NOT NULL,
          observed_outcome_json TEXT NOT NULL,
          outcome_evidence_digest TEXT NOT NULL,
          attribution_strength TEXT NOT NULL CHECK(attribution_strength='association_only'),
          contract_version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
CREATE TABLE agent_plan (
  plan_id TEXT PRIMARY KEY,
  objective_id TEXT REFERENCES agent_objective(objective_id) ON DELETE SET NULL,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
  agent_turn_id TEXT NOT NULL REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
  planner_version TEXT NOT NULL,
  context_manifest_json TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('planned', 'running', 'waiting_child', 'completed', 'failed', 'cancelled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(agent_turn_id)
);
CREATE TABLE agent_step (
  step_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES agent_plan(plan_id) ON DELETE CASCADE,
  step_index INTEGER NOT NULL CHECK(step_index >= 0),
  capability_name TEXT NOT NULL,
  capability_version TEXT NOT NULL,
  execution_location TEXT NOT NULL CHECK(execution_location IN ('server', 'local_model', 'client_device_bridge')),
  risk_level TEXT NOT NULL CHECK(risk_level IN ('low', 'medium', 'high')),
  arguments_json TEXT NOT NULL,
  expected_evidence_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('planned', 'running', 'waiting_child', 'completed', 'failed', 'unknown', 'cancelled')),
  idempotency_key TEXT NOT NULL UNIQUE,
  result_json TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(plan_id, step_index)
);
CREATE TABLE agent_turn (
  agent_turn_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  transcript TEXT NOT NULL,
  intent TEXT NOT NULL,
  response_text TEXT NOT NULL,
  action_json TEXT NOT NULL,
  orchestrator_version TEXT NOT NULL,
  tool_status TEXT NOT NULL DEFAULT 'pending' CHECK(tool_status IN ('pending', 'completed', 'failed')),
  tool_error_code TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE audit_event (
  audit_id TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  child_id TEXT,
  summary_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL
);
CREATE TABLE backup_manifest (
  backup_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  database_version INTEGER NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  integrity_status TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL
);
CREATE TABLE behavior_legacy_migration (
  source_path TEXT PRIMARY KEY,
  completed_at TEXT NOT NULL
);
CREATE TABLE behavior_retraction (
  event_id TEXT PRIMARY KEY REFERENCES observable_event(event_id),
  child_id TEXT NOT NULL,
  correction_id TEXT NOT NULL UNIQUE,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE behavior_stop_condition (
  child_id TEXT NOT NULL,
  session_id TEXT,
  candidate_state TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  correction_id TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY(child_id, session_id, candidate_state, source_event_id)
);
CREATE TABLE bounded_experiment_event (
          event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id TEXT NOT NULL UNIQUE,
          protocol_id TEXT NOT NULL REFERENCES bounded_experiment_protocol(protocol_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          from_status TEXT,
          to_status TEXT NOT NULL,
          outcome_status TEXT NOT NULL,
          actor TEXT NOT NULL,
          reason TEXT NOT NULL,
          metrics_json TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE bounded_experiment_protocol (
          protocol_id TEXT PRIMARY KEY,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          domain TEXT NOT NULL CHECK(domain IN ('teaching_strategy','behavior_intervention')),
          source_kind TEXT NOT NULL CHECK(source_kind IN ('strategy_experiment','habit_experiment')),
          source_id TEXT NOT NULL,
          primary_variable TEXT NOT NULL,
          baseline_version TEXT NOT NULL,
          candidate_version TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN (
            'draft','replay_passed','shadow','active','completed','stopped','rolled_back'
          )),
          outcome_status TEXT NOT NULL CHECK(outcome_status IN (
            'unknown','improving','stable','worsening'
          )),
          metrics_json TEXT NOT NULL,
          thresholds_json TEXT NOT NULL,
          rollback_reason TEXT,
          protocol_version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          ended_at TEXT,
          UNIQUE(source_kind, source_id)
        );
CREATE TABLE capture_artifact (
  artifact_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  artifact_type TEXT NOT NULL,
  local_path TEXT,
  extracted_text TEXT,
  confidence REAL,
  details_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE child_profile (
  child_id TEXT PRIMARY KEY,
  nickname TEXT NOT NULL,
  grade TEXT NOT NULL,
  curriculum_region TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE child_safety_event (
  safety_event_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  agent_turn_id TEXT NOT NULL REFERENCES agent_turn(agent_turn_id),
  risk_level TEXT NOT NULL CHECK(risk_level IN ('R1', 'R2', 'R3')),
  category TEXT NOT NULL CHECK(category IN ('emotional_distress', 'fear_or_bullying', 'immediate_danger')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'acknowledged')),
  guidance_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  acknowledged_at TEXT,
  acknowledgement_action_id TEXT UNIQUE,
  UNIQUE(agent_turn_id)
);
CREATE TABLE child_state_event (
  state_event_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  state TEXT NOT NULL,
  confidence REAL NOT NULL,
  reason TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
  model_version TEXT NOT NULL,
  shadow INTEGER NOT NULL,
  observed_at TEXT NOT NULL
);
CREATE TABLE child_voice_calibration (
                  child_id TEXT PRIMARY KEY REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  consented_at TEXT NOT NULL,
                  profile_json TEXT NOT NULL,
                  vocabulary_json TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('ready')),
                  updated_at TEXT NOT NULL
                , microphone_json TEXT);
CREATE TABLE classroom_recall (
  recall_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  subject TEXT NOT NULL,
  summary TEXT NOT NULL,
  confidence REAL NOT NULL,
  confirmation_status TEXT NOT NULL,
  source_refs_json TEXT NOT NULL,
  record_kind TEXT NOT NULL DEFAULT 'child_statement' CHECK(record_kind IN ('child_statement', 'system_candidate')),
  candidate_nodes_json TEXT NOT NULL DEFAULT '[]',
  textbook_mapping_json TEXT,
  evidence_sources_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE consent_record (
  consent_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  allowed INTEGER NOT NULL,
  policy_version TEXT NOT NULL,
  source TEXT NOT NULL,
  occurred_at TEXT NOT NULL
);
CREATE TABLE curriculum_capability (
  capability_id TEXT PRIMARY KEY,
  corpus_id TEXT NOT NULL REFERENCES curriculum_corpus(corpus_id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES curriculum_internalization_job(job_id) ON DELETE CASCADE,
  grade_title TEXT NOT NULL,
  subject_title TEXT NOT NULL,
  version_title TEXT,
  semester TEXT NOT NULL,
  unit_title TEXT NOT NULL,
  capability_type TEXT NOT NULL CHECK(capability_type IN (
    'concept', 'procedure', 'reasoning', 'representation', 'language_function',
    'experiment', 'reading_strategy', 'writing_skill', 'physical_skill', 'value_judgement'
  )),
  title TEXT NOT NULL,
  learning_objective TEXT NOT NULL,
  prerequisites_json TEXT NOT NULL,
  misconceptions_json TEXT NOT NULL,
  mastery_criteria_json TEXT NOT NULL,
  diagnostic_blueprint_json TEXT NOT NULL,
  explanation_strategies_json TEXT NOT NULL,
  transfer_goals_json TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  maturity TEXT NOT NULL CHECK(maturity IN ('model_drafted', 'verified', 'conflicted', 'retired')),
  review_note TEXT,
  model_provider TEXT NOT NULL,
  model_name TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(corpus_id, grade_title, subject_title, version_title, semester, unit_title, fingerprint)
);
CREATE TABLE curriculum_capability_source (
  capability_id TEXT NOT NULL REFERENCES curriculum_capability(capability_id) ON DELETE CASCADE,
  topic_id TEXT NOT NULL REFERENCES curriculum_topic(topic_id) ON DELETE CASCADE,
  PRIMARY KEY(capability_id, topic_id)
);
CREATE TABLE curriculum_capability_standard (
  capability_id TEXT NOT NULL REFERENCES curriculum_capability(capability_id) ON DELETE CASCADE,
  requirement_id TEXT NOT NULL REFERENCES curriculum_standard_requirement(requirement_id) ON DELETE CASCADE,
  PRIMARY KEY(capability_id, requirement_id)
);
CREATE TABLE curriculum_corpus (
  corpus_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  corpus_type TEXT NOT NULL CHECK(corpus_type = 'seyk'),
  source_database_path TEXT NOT NULL UNIQUE,
  transcript_root_path TEXT NOT NULL,
  video_root_path TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  license_status TEXT NOT NULL CHECK(license_status IN ('public_personal_use', 'unknown')),
  review_status TEXT NOT NULL CHECK(review_status IN ('approved_baseline', 'quarantined')),
  status TEXT NOT NULL CHECK(status IN ('importing', 'ready', 'failed')),
  source_hash TEXT,
  resource_count INTEGER NOT NULL DEFAULT 0,
  transcript_count INTEGER NOT NULL DEFAULT 0,
  local_video_count INTEGER NOT NULL DEFAULT 0,
  last_error_code TEXT,
  imported_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE curriculum_corpus_import_job (
  job_id TEXT PRIMARY KEY,
  corpus_id TEXT NOT NULL REFERENCES curriculum_corpus(corpus_id) ON DELETE CASCADE,
  client_action_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'failed')),
  total_count INTEGER NOT NULL DEFAULT 0,
  processed_count INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE curriculum_internalization_batch (
  batch_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES curriculum_internalization_job(job_id) ON DELETE CASCADE,
  batch_index INTEGER NOT NULL,
  unit_title TEXT NOT NULL,
  source_topic_ids_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'processing', 'completed', 'failed', 'interrupted')),
  error_code TEXT,
  output_hash TEXT,
  started_at TEXT,
  completed_at TEXT,
  UNIQUE(job_id, batch_index)
);
CREATE TABLE curriculum_internalization_job (
  job_id TEXT PRIMARY KEY,
  corpus_id TEXT NOT NULL REFERENCES curriculum_corpus(corpus_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  grade_title TEXT NOT NULL,
  subject_title TEXT NOT NULL,
  version_title TEXT,
  semester TEXT NOT NULL,
  unit_title TEXT,
  status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'failed', 'interrupted')),
  total_batches INTEGER NOT NULL DEFAULT 0,
  processed_batches INTEGER NOT NULL DEFAULT 0,
  capability_count INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  model_provider TEXT,
  model_name TEXT,
  client_action_id TEXT NOT NULL UNIQUE,
  started_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE curriculum_resource (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  corpus_id TEXT NOT NULL REFERENCES curriculum_corpus(corpus_id) ON DELETE CASCADE,
  external_resource_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  teacher TEXT,
  stage_title TEXT NOT NULL,
  grade_title TEXT NOT NULL,
  subject_title TEXT NOT NULL,
  version_title TEXT,
  volume_title TEXT,
  semester TEXT NOT NULL DEFAULT '未知学期',
  unit_path TEXT NOT NULL,
  transcript_text TEXT NOT NULL,
  transcript_hash TEXT,
  transcript_relative_path TEXT,
  video_relative_path TEXT,
  video_duration INTEGER,
  local_video_available INTEGER NOT NULL DEFAULT 0 CHECK(local_video_available IN (0, 1)),
  import_job_id TEXT NOT NULL,
  UNIQUE(corpus_id, external_resource_id)
);
CREATE VIRTUAL TABLE curriculum_resource_fts USING fts5(
  title, description, transcript_text, unit_path, grade_title, subject_title,
  content='curriculum_resource', content_rowid='row_id', tokenize='trigram'
);
CREATE TABLE curriculum_resource_topic (
  row_id INTEGER NOT NULL REFERENCES curriculum_resource(row_id) ON DELETE CASCADE,
  topic_id TEXT NOT NULL REFERENCES curriculum_topic(topic_id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL DEFAULT 'evidence_for' CHECK(relation_type = 'evidence_for'),
  PRIMARY KEY(row_id, topic_id)
);
CREATE TABLE curriculum_standard_page (
  source_id TEXT NOT NULL REFERENCES curriculum_standard_source(source_id) ON DELETE CASCADE,
  page_number INTEGER NOT NULL,
  content_text TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  char_count INTEGER NOT NULL,
  PRIMARY KEY(source_id, page_number)
);
CREATE VIRTUAL TABLE curriculum_standard_page_fts USING fts5(
  source_id UNINDEXED, page_number UNINDEXED, content_text, tokenize='trigram'
);
CREATE TABLE curriculum_standard_requirement (
  requirement_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES curriculum_standard_source(source_id) ON DELETE CASCADE,
  subject_title TEXT NOT NULL,
  stage_scope TEXT NOT NULL,
  requirement_type TEXT NOT NULL CHECK(requirement_type IN (
    'course_goal', 'content_requirement', 'academic_requirement', 'academic_quality',
    'implementation_requirement', 'local_supplement'
  )),
  title TEXT NOT NULL,
  requirement_text TEXT NOT NULL,
  observable_outcome TEXT NOT NULL,
  page_start INTEGER NOT NULL,
  page_end INTEGER NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  maturity TEXT NOT NULL CHECK(maturity IN ('model_drafted', 'verified', 'conflicted', 'retired')),
  model_provider TEXT,
  model_name TEXT,
  prompt_version TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE curriculum_standard_source (
  source_id TEXT PRIMARY KEY,
  source_key TEXT NOT NULL UNIQUE,
  jurisdiction TEXT NOT NULL CHECK(jurisdiction IN ('CN', 'JS')),
  source_type TEXT NOT NULL CHECK(source_type IN (
    'national_curriculum_plan', 'national_standard', 'jiangsu_implementation',
    'jiangsu_textbook_catalog', 'jiangsu_local_requirement'
  )),
  subject_title TEXT NOT NULL,
  title TEXT NOT NULL,
  version_label TEXT NOT NULL,
  official_url TEXT NOT NULL,
  local_root_path TEXT NOT NULL,
  pdf_filename TEXT,
  pages_filename TEXT,
  pdf_sha256 TEXT,
  text_sha256 TEXT,
  page_count INTEGER NOT NULL DEFAULT 0,
  extracted_page_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK(status IN ('downloaded', 'ocr_ready', 'structured', 'failed')),
  imported_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE curriculum_standard_topic_alignment (
  requirement_id TEXT NOT NULL REFERENCES curriculum_standard_requirement(requirement_id) ON DELETE CASCADE,
  topic_id TEXT NOT NULL REFERENCES curriculum_topic(topic_id) ON DELETE CASCADE,
  coverage_type TEXT NOT NULL CHECK(coverage_type IN ('full', 'partial', 'context_only')),
  rationale TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  maturity TEXT NOT NULL CHECK(maturity IN ('model_drafted', 'verified', 'conflicted', 'retired')),
  PRIMARY KEY(requirement_id, topic_id)
);
CREATE TABLE curriculum_topic (
  topic_id TEXT PRIMARY KEY,
  corpus_id TEXT NOT NULL REFERENCES curriculum_corpus(corpus_id) ON DELETE CASCADE,
  grade_title TEXT NOT NULL,
  subject_title TEXT NOT NULL,
  version_title TEXT,
  semester TEXT NOT NULL,
  unit_title TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  title TEXT NOT NULL,
  lesson_kind TEXT NOT NULL CHECK(lesson_kind IN ('instruction', 'practice', 'review', 'experiment', 'activity')),
  learning_objective TEXT NOT NULL,
  maturity TEXT NOT NULL CHECK(maturity IN ('source_structured', 'model_drafted', 'verified')),
  knowledge_json TEXT NOT NULL DEFAULT '{}',
  source_count INTEGER NOT NULL DEFAULT 0,
  transcript_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(corpus_id, grade_title, subject_title, version_title, semester, unit_title, normalized_title, lesson_kind)
);
CREATE TABLE curriculum_topic_standard_candidate (
  topic_id TEXT PRIMARY KEY REFERENCES curriculum_topic(topic_id) ON DELETE CASCADE,
  source_id TEXT REFERENCES curriculum_standard_source(source_id) ON DELETE SET NULL,
  page_number INTEGER,
  matched_term TEXT,
  alignment_status TEXT NOT NULL CHECK(alignment_status IN (
    'candidate', 'unresolved', 'no_subject_standard'
  )),
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  method_version TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE daily_feature (
  child_id TEXT NOT NULL,
  day TEXT NOT NULL,
  seated_count INTEGER NOT NULL DEFAULT 0,
  away_count INTEGER NOT NULL DEFAULT 0,
  writing_count INTEGER NOT NULL DEFAULT 0,
  reading_count INTEGER NOT NULL DEFAULT 0,
  switch_count INTEGER NOT NULL DEFAULT 0,
  help_count INTEGER NOT NULL DEFAULT 0,
  stall_count INTEGER NOT NULL DEFAULT 0,
  distraction_candidate_count INTEGER NOT NULL DEFAULT 0,
  fatigue_candidate_count INTEGER NOT NULL DEFAULT 0,
  plan_deviation_count INTEGER NOT NULL DEFAULT 0,
  child_check_in_count INTEGER NOT NULL DEFAULT 0,
  missing_observation_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(child_id, day)
);
CREATE TABLE daily_plan (
  plan_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  plan_date TEXT NOT NULL,
  status TEXT NOT NULL,
  total_minutes INTEGER NOT NULL,
  generator TEXT NOT NULL,
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE daily_reflection (
  reflection_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL UNIQUE REFERENCES daily_session(session_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  learned_text TEXT NOT NULL,
  difficult_text TEXT NOT NULL,
  feeling TEXT NOT NULL,
  next_action TEXT NOT NULL,
  skipped INTEGER NOT NULL DEFAULT 0 CHECK(skipped IN (0, 1)),
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE daily_report (
  report_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL UNIQUE REFERENCES daily_session(session_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  report_date TEXT NOT NULL,
  source TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  parent_coaching_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE daily_session (
  session_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  status TEXT NOT NULL CHECK(status IN ('active', 'completed', 'cancelled')),
  state TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  last_checkpoint_at TEXT NOT NULL,
  observation_status TEXT NOT NULL DEFAULT 'enabled',
  observation_revision INTEGER NOT NULL DEFAULT 0 CHECK(observation_revision >= 0),
  snooze_until TEXT,
  recall_declared_empty INTEGER NOT NULL DEFAULT 0 CHECK(recall_declared_empty IN (0, 1)),
  homework_declared_empty INTEGER NOT NULL DEFAULT 0 CHECK(homework_declared_empty IN (0, 1)),
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE deferred_task_review (
  review_id TEXT PRIMARY KEY,
  source_plan_item_id TEXT NOT NULL UNIQUE REFERENCES plan_item(plan_item_id),
  source_session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  target_session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  decision TEXT NOT NULL CHECK(decision IN ('continue_today', 'completed_elsewhere', 'cancelled')),
  reason TEXT NOT NULL,
  target_homework_id TEXT REFERENCES homework_item(homework_id),
  reviewed_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE deletion_job (
  deletion_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  target_type TEXT NOT NULL DEFAULT 'child_scope',
  target_id TEXT,
  status TEXT NOT NULL,
  deleted_counts_json TEXT NOT NULL DEFAULT '{}',
  residual_paths_json TEXT NOT NULL DEFAULT '[]',
  verification_json TEXT NOT NULL DEFAULT '{}',
  error_code TEXT,
  error_message TEXT,
  client_action_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE device_binding (
  device_type TEXT PRIMARY KEY CHECK(device_type IN ('camera', 'document_camera')),
  system_id_hash TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  capabilities_json TEXT NOT NULL,
  verified_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE device_binding_event (
  event_id TEXT PRIMARY KEY,
  bindings_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE device_incident (
  incident_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  device_type TEXT NOT NULL,
  error_code TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
  occurred_at TEXT NOT NULL,
  resolved_at TEXT,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE device_incident_report (
  report_id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL REFERENCES device_incident(incident_id),
  client_action_id TEXT NOT NULL UNIQUE,
  reported_at TEXT NOT NULL
);
CREATE TABLE device_profile (
  device_id TEXT PRIMARY KEY,
  device_type TEXT NOT NULL,
  system_id_hash TEXT NOT NULL,
  capabilities_json TEXT NOT NULL,
  last_seen_at TEXT,
  status TEXT NOT NULL
);
CREATE TABLE device_qualification_run (
  run_id TEXT PRIMARY KEY,
  mode TEXT NOT NULL CHECK(mode IN ('quick_dual', 'dual_concurrency', 'camera_stability', 'document_stability')),
  status TEXT NOT NULL CHECK(status IN ('active', 'passed', 'failed', 'cancelled', 'interrupted')),
  expected_seconds INTEGER NOT NULL,
  camera_system_id_hash TEXT NOT NULL,
  document_system_id_hash TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  failure_code TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  client_action_id TEXT NOT NULL UNIQUE,
  finish_client_action_id TEXT UNIQUE
);
CREATE TABLE diagnostic_attempt (
  attempt_id TEXT PRIMARY KEY,
  diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
  evidence_id TEXT NOT NULL REFERENCES learning_evidence(evidence_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  submitted_answer TEXT NOT NULL,
  correctness REAL,
  feedback TEXT NOT NULL,
  review_task_id TEXT REFERENCES review_task(review_task_id),
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE diagnostic_item (
  diagnostic_item_id TEXT PRIMARY KEY,
  knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
  prompt TEXT NOT NULL,
  answer_type TEXT NOT NULL,
  expected_answer_json TEXT NOT NULL,
  explanation TEXT NOT NULL,
  stimulus_text TEXT,
  stimulus_asset TEXT,
  assessment_mode TEXT NOT NULL DEFAULT 'objective',
  difficulty INTEGER NOT NULL,
  source_ref TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE family_rule (
  child_id TEXT PRIMARY KEY REFERENCES child_profile(child_id),
  latest_end_time TEXT,
  max_learning_minutes INTEGER NOT NULL,
  task_segment_minutes INTEGER NOT NULL,
  break_minutes INTEGER NOT NULL,
  pet_min_minutes INTEGER NOT NULL,
  competition_minutes INTEGER NOT NULL,
  observation_mode TEXT NOT NULL DEFAULT 'shadow' CHECK(observation_mode IN ('shadow', 'gentle')),
  reminder_budget_per_10m INTEGER NOT NULL DEFAULT 2 CHECK(reminder_budget_per_10m BETWEEN 0 AND 3),
  misclassification_shadow_threshold INTEGER NOT NULL DEFAULT 3 CHECK(misclassification_shadow_threshold BETWEEN 1 AND 10),
  version INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE habit_experiment (
  experiment_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  target_state TEXT NOT NULL,
  title TEXT NOT NULL,
  child_action TEXT NOT NULL,
  success_signal TEXT NOT NULL,
  duration_days INTEGER NOT NULL,
  baseline_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('proposed','active','completed','declined','stopped')),
  proposed_at TEXT NOT NULL,
  started_at TEXT,
  ends_at TEXT,
  closed_at TEXT,
  outcome_json TEXT
);
CREATE TABLE habit_experiment_event (
  event_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL REFERENCES habit_experiment(experiment_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE habit_formation_event (
  event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  experiment_id TEXT NOT NULL REFERENCES habit_experiment(experiment_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  from_stage TEXT,
  to_stage TEXT NOT NULL,
  from_cue_level TEXT,
  to_cue_level TEXT NOT NULL,
  assessment_fingerprint TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  reason TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE habit_formation_state (
  experiment_id TEXT PRIMARY KEY REFERENCES habit_experiment(experiment_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  target_state TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN (
    'negotiate','baseline','cue','practice','reinforce','fade','transfer',
    'maintain','recover','declined','stopped','completed'
  )),
  cue_level TEXT NOT NULL CHECK(cue_level IN ('L3','L2','L1','L0')),
  revision INTEGER NOT NULL,
  assessment_fingerprint TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  transition_reason TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE homework_item (
  homework_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  subject TEXT NOT NULL,
  task_type TEXT NOT NULL,
  title TEXT NOT NULL,
  range_text TEXT,
  expected_minutes INTEGER NOT NULL CHECK(expected_minutes > 0),
  due_at TEXT,
  special_requirements TEXT,
  source_artifact_id TEXT,
  ocr_confidence REAL,
  confirmation_status TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE homework_revision (
  revision_id TEXT PRIMARY KEY,
  homework_id TEXT NOT NULL REFERENCES homework_item(homework_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE intervention (
  intervention_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL,
  session_id TEXT,
  source_event_id TEXT,
  decided_at TEXT NOT NULL,
  candidate_state TEXT NOT NULL,
  message TEXT NOT NULL,
  feedback TEXT,
  feedback_at TEXT,
  feedback_note TEXT,
  feedback_action_id TEXT,
  feedback_payload_hash TEXT,
  outcome_json TEXT,
  effect_action_id TEXT,
  effect_payload_hash TEXT,
  intervention_type TEXT NOT NULL DEFAULT 'default_ask_first',
  status TEXT NOT NULL DEFAULT 'active',
  withdrawn_at TEXT,
  withdrawal_reason TEXT
);
CREATE TABLE intervention_policy_reset (child_id TEXT PRIMARY KEY, reset_at TEXT NOT NULL);
CREATE TABLE intervention_record (
  intervention_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  plan_item_id TEXT REFERENCES plan_item(plan_item_id),
  trigger_state TEXT NOT NULL,
  confidence REAL NOT NULL,
  stage TEXT NOT NULL,
  message_text TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  child_response TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  responded_at TEXT
);
CREATE TABLE kg_atomic_candidate (
  candidate_id TEXT PRIMARY KEY,
  resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  segment_id TEXT REFERENCES kg_segment(segment_id) ON DELETE CASCADE,
  canonical_signature TEXT NOT NULL,
  title TEXT NOT NULL,
  atom_kind TEXT NOT NULL CHECK(atom_kind IN (
    'fact_recall','concept_identification','concept_discrimination','procedure_execution',
    'representation_translation','evidence_reasoning','strategy_selection','language_form',
    'language_meaning','language_use','motor_skill'
  )),
  action_verb TEXT NOT NULL,
  object_text TEXT NOT NULL,
  condition_text TEXT NOT NULL DEFAULT '',
  representation TEXT NOT NULL DEFAULT '',
  mastery_criterion TEXT NOT NULL,
  evidence_text TEXT NOT NULL,
  evidence_start INTEGER,
  evidence_end INTEGER,
  scope_json TEXT NOT NULL DEFAULT '{}',
  unit_path TEXT NOT NULL DEFAULT '',
  source_kind TEXT NOT NULL CHECK(source_kind IN (
    'explicit_objective','assessment_prompt','lesson_summary','lesson_title','teacher_review','model_refinement'
  )),
  source_ref TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  atomicity_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','confirmed','rejected','merged','superseded')),
  version_label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(resource_id,canonical_signature,source_kind,evidence_start)
);
CREATE TABLE kg_atomic_definition_draft (
  draft_id TEXT PRIMARY KEY,
  canonical_signature TEXT NOT NULL,
  title TEXT NOT NULL,
  grade_band TEXT NOT NULL,
  subject TEXT NOT NULL,
  atom_kind TEXT NOT NULL CHECK(atom_kind IN (
    'fact_recall','concept_identification','concept_discrimination','procedure_execution',
    'representation_translation','evidence_reasoning','strategy_selection','language_form',
    'language_meaning','language_use','experiment_skill','motor_skill'
  )),
  action_verb TEXT NOT NULL,
  knowledge_component TEXT NOT NULL,
  boundary_json TEXT NOT NULL,
  observable_outcome TEXT NOT NULL,
  mastery_criteria_json TEXT NOT NULL,
  diagnostic_blueprint_json TEXT NOT NULL,
  misconception_hypotheses_json TEXT NOT NULL,
  concept_tags_json TEXT NOT NULL,
  standard_alignments_json TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','rejected','needs_split','merged')),
  source_method TEXT NOT NULL CHECK(source_method IN ('model','teacher','curated')),
  model_provider TEXT,
  model_name TEXT,
  prompt_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(canonical_signature,prompt_version)
);
CREATE TABLE kg_atomic_draft_batch (
  draft_id TEXT NOT NULL REFERENCES kg_atomic_definition_draft(draft_id) ON DELETE CASCADE,
  batch_id TEXT NOT NULL REFERENCES kg_atomic_extraction_batch(batch_id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES kg_atomic_extraction_job(job_id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  PRIMARY KEY(draft_id,batch_id)
);
CREATE TABLE kg_atomic_draft_evidence (
  evidence_id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES kg_atomic_definition_draft(draft_id) ON DELETE CASCADE,
  resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  segment_id TEXT NOT NULL REFERENCES kg_segment(segment_id) ON DELETE CASCADE,
  exact_quote TEXT NOT NULL,
  quote_sha256 TEXT NOT NULL,
  evidence_role TEXT NOT NULL CHECK(evidence_role IN ('objective','explanation','worked_example','assessment','summary')),
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  created_at TEXT NOT NULL,
  UNIQUE(draft_id,resource_id,segment_id,quote_sha256)
);
CREATE TABLE kg_atomic_draft_standard_alignment (
  alignment_id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES kg_atomic_definition_draft(draft_id) ON DELETE CASCADE,
  source_ref TEXT NOT NULL,
  exact_quote TEXT NOT NULL,
  quote_sha256 TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  created_at TEXT NOT NULL,
  UNIQUE(draft_id,source_ref,quote_sha256)
);
CREATE TABLE kg_atomic_extraction_batch (
  batch_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES kg_atomic_extraction_job(job_id) ON DELETE CASCADE,
  batch_index INTEGER NOT NULL,
  resource_ids_json TEXT NOT NULL,
  input_json TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','failed','interrupted')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  output_sha256 TEXT,
  model_provider TEXT,
  model_name TEXT,
  error_code TEXT,
  error_detail TEXT,
  rejected_output_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(job_id,batch_index)
);
CREATE TABLE kg_atomic_extraction_job (
  job_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','failed','interrupted')),
  scope_json TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  total_batches INTEGER NOT NULL DEFAULT 0,
  completed_batches INTEGER NOT NULL DEFAULT 0,
  failed_batches INTEGER NOT NULL DEFAULT 0,
  draft_count INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE kg_atomic_merge_candidate (
  merge_id TEXT PRIMARY KEY,
  left_draft_id TEXT NOT NULL REFERENCES kg_atomic_definition_draft(draft_id) ON DELETE CASCADE,
  right_draft_id TEXT NOT NULL REFERENCES kg_atomic_definition_draft(draft_id) ON DELETE CASCADE,
  lexical_similarity REAL NOT NULL CHECK(lexical_similarity BETWEEN 0 AND 1),
  shared_concepts_json TEXT NOT NULL,
  rationale TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','rejected')),
  algorithm_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(left_draft_id < right_draft_id),
  UNIQUE(left_draft_id,right_draft_id,algorithm_version)
);
CREATE TABLE kg_atomic_profile (
  node_id TEXT PRIMARY KEY REFERENCES kg_node(node_id) ON DELETE CASCADE,
  canonical_signature TEXT NOT NULL UNIQUE,
  atom_kind TEXT NOT NULL,
  action_verb TEXT NOT NULL,
  object_text TEXT NOT NULL,
  condition_text TEXT NOT NULL DEFAULT '',
  representation TEXT NOT NULL DEFAULT '',
  mastery_criteria_json TEXT NOT NULL,
  diagnostic_blueprint_json TEXT NOT NULL,
  granularity_version TEXT NOT NULL,
  approved_evidence_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE kg_atomic_relation_candidate (
  relation_id TEXT PRIMARY KEY,
  source_key TEXT NOT NULL,
  target_key TEXT NOT NULL,
  relation_type TEXT NOT NULL CHECK(relation_type IN (
    'part_of','prerequisite_of','enables','representation_of','contrasts_with',
    'misconception_of','assessed_by','transfers_to','remediated_by','evidenced_by','co_taught_with'
  )),
  rationale TEXT NOT NULL,
  evidence_ref TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','rejected')),
  version_label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(source_key <> target_key),
  UNIQUE(source_key,target_key,relation_type,version_label)
);
CREATE TABLE kg_atomic_resource_disposition (
  disposition_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES kg_atomic_extraction_job(job_id) ON DELETE CASCADE,
  batch_id TEXT NOT NULL REFERENCES kg_atomic_extraction_batch(batch_id) ON DELETE CASCADE,
  resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  disposition TEXT NOT NULL CHECK(disposition IN ('noninstructional')),
  reason_code TEXT NOT NULL CHECK(reason_code IN ('no_usable_transcript','repeated_transcript_noise')),
  detector_version TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(job_id,resource_id)
);
CREATE TABLE kg_atomic_resource_gap (
  gap_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES kg_atomic_extraction_job(job_id) ON DELETE CASCADE,
  batch_id TEXT NOT NULL REFERENCES kg_atomic_extraction_batch(batch_id) ON DELETE CASCADE,
  resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('open','resolved')),
  reason_code TEXT NOT NULL CHECK(reason_code IN (
    'terminal_model_validation_failure','scope_mismatch','specialized_extraction_required'
  )),
  attempt_count INTEGER NOT NULL,
  error_code TEXT,
  error_detail TEXT,
  resolution_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  UNIQUE(job_id,resource_id)
);
CREATE TABLE kg_atomic_textbook_alignment (
  alignment_id TEXT PRIMARY KEY,
  source_job_id TEXT NOT NULL REFERENCES kg_atomic_extraction_job(job_id) ON DELETE CASCADE,
  draft_id TEXT NOT NULL REFERENCES kg_atomic_definition_draft(draft_id) ON DELETE CASCADE,
  textbook_resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  textbook_segment_id TEXT NOT NULL REFERENCES kg_segment(segment_id) ON DELETE CASCADE,
  exact_quote TEXT NOT NULL,
  quote_sha256 TEXT NOT NULL,
  lexical_score REAL NOT NULL CHECK(lexical_score BETWEEN 0 AND 1),
  scope_score REAL NOT NULL CHECK(scope_score BETWEEN 0 AND 1),
  match_method TEXT NOT NULL CHECK(match_method IN ('local_lexical','model_verified','human_verified')),
  evidence_role TEXT NOT NULL CHECK(evidence_role IN ('mention','definition','worked_example','exercise','summary')),
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','rejected')),
  algorithm_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(source_job_id,draft_id,textbook_resource_id,textbook_segment_id,algorithm_version)
);
CREATE TABLE kg_candidate_mapping_failure (
  failure_id TEXT PRIMARY KEY, mapping_job_id TEXT NOT NULL REFERENCES kg_candidate_mapping_job(mapping_job_id) ON DELETE CASCADE,
  resource_id TEXT NOT NULL, error_code TEXT NOT NULL, detail TEXT, retry_count INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK(status IN ('open','resolved','ignored')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(mapping_job_id,resource_id)
);
CREATE TABLE kg_candidate_mapping_job (
  mapping_job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','failed','interrupted')),
  source_ref TEXT NOT NULL, checkpoint_resource_id TEXT, total_resources INTEGER NOT NULL DEFAULT 0,
  processed_resources INTEGER NOT NULL DEFAULT 0, mapped_rows INTEGER NOT NULL DEFAULT 0,
  failed_resources INTEGER NOT NULL DEFAULT 0, options_json TEXT NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, completed_at TEXT, run_token TEXT, run_lease_expires_at TEXT
);
CREATE TABLE kg_companion_graph_context_event(
        event_id TEXT PRIMARY KEY,child_fingerprint TEXT NOT NULL,query_fingerprint TEXT NOT NULL,
        grade_scope_json TEXT NOT NULL,subject_scope TEXT,result_count INTEGER NOT NULL,
        approved_count INTEGER NOT NULL,hypothesis_count INTEGER NOT NULL,
        selected_keys_json TEXT NOT NULL,build_label TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE kg_companion_graph_meta(
        key TEXT PRIMARY KEY,value TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE kg_corpus (
  corpus_id TEXT PRIMARY KEY, title TEXT NOT NULL, source_kind TEXT NOT NULL,
  transcript_root TEXT NOT NULL, video_root TEXT, metadata_db_path TEXT,
  source_ref TEXT NOT NULL, license_boundary TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','restricted','rejected')),
  version_label TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  pdf_root TEXT, image_root TEXT,
  UNIQUE(source_kind, transcript_root)
);
CREATE TABLE kg_document_page (
  page_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL UNIQUE REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  document_kind TEXT NOT NULL CHECK(document_kind IN ('textbook','school_official_practice','purchased_practice_book','other_user_document')),
  page_number INTEGER NOT NULL CHECK(page_number >= 1), image_relative_path TEXT,
  image_sha256 TEXT, image_fingerprint TEXT, ocr_engine TEXT NOT NULL,
  ocr_confidence REAL CHECK(ocr_confidence IS NULL OR (ocr_confidence BETWEEN 0 AND 1)),
  source_ref TEXT NOT NULL, license_boundary TEXT NOT NULL, version_label TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE kg_edge (
  edge_id TEXT PRIMARY KEY, source_node_id TEXT NOT NULL REFERENCES kg_node(node_id) ON DELETE CASCADE,
  target_node_id TEXT NOT NULL REFERENCES kg_node(node_id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL CHECK(relation_type IN ('contains','prerequisite_of','related_to','misconception_of','aligned_to','explained_by','assessed_by','evidenced_by','maps_to','generated_from')),
  rationale TEXT NOT NULL, source_ref TEXT NOT NULL, version_label TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','restricted','rejected')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(source_node_id,target_node_id,relation_type,version_label)
);
CREATE TABLE kg_ingest_failure (
  failure_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES kg_ingest_job(job_id) ON DELETE CASCADE,
  external_id TEXT NOT NULL, relative_path TEXT NOT NULL, error_code TEXT NOT NULL, detail TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL CHECK(status IN ('open','resolved','ignored')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(job_id, external_id)
);
CREATE TABLE kg_ingest_job (
  job_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES kg_corpus(corpus_id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','failed','interrupted')),
  options_json TEXT NOT NULL, checkpoint_cursor TEXT, total_count INTEGER NOT NULL DEFAULT 0,
  processed_count INTEGER NOT NULL DEFAULT 0, skipped_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
  run_token TEXT, run_lease_expires_at TEXT
);
CREATE TABLE kg_language_focus_candidate (
  candidate_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  language TEXT NOT NULL CHECK(language IN ('english','chinese')), term TEXT NOT NULL, normalized TEXT NOT NULL,
  focus_kind TEXT NOT NULL CHECK(focus_kind IN ('word','character','phrase')), proposed_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK(status IN ('pending','projecting','confirmed','deleted','stale','conflicted','withdrawn')), source_start INTEGER, source_end INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(resource_id,language,normalized,focus_kind)
);
CREATE TABLE kg_language_focus_candidate_action (
  client_action_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES kg_language_focus_candidate(candidate_id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK(action IN ('save','confirm','withdraw')),
  payload_sha256 TEXT NOT NULL,
  result_status TEXT NOT NULL,
  projection_claim TEXT, projection_lease_expires_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE kg_language_lexeme (
  lexeme_id TEXT PRIMARY KEY, language TEXT NOT NULL CHECK(language IN ('english','chinese')),
  surface TEXT NOT NULL, focus_kind TEXT NOT NULL CHECK(focus_kind IN ('word','character','phrase')),
  normalized TEXT NOT NULL, part_of_speech TEXT, senses_json TEXT NOT NULL DEFAULT '[]',
  collocations_json TEXT NOT NULL DEFAULT '[]', confusables_json TEXT NOT NULL DEFAULT '[]',
  phonics_or_ipa TEXT, pronunciations_json TEXT NOT NULL DEFAULT '[]', radical TEXT,
  structure TEXT, stroke_order_ref TEXT, compounds_json TEXT NOT NULL DEFAULT '[]',
  common_errors_json TEXT NOT NULL DEFAULT '[]', source_ref TEXT NOT NULL, version_label TEXT NOT NULL,
  license_boundary TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(language,normalized,focus_kind,version_label)
);
CREATE TABLE kg_language_lexeme_page (
  lexeme_id TEXT NOT NULL REFERENCES kg_language_lexeme(lexeme_id) ON DELETE CASCADE,
  resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE, page_number INTEGER NOT NULL,
  evidence_text TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lexeme_id,resource_id)
);
CREATE TABLE kg_local_course_context_event(
        event_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        query_sha256 TEXT NOT NULL,selected_segments_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE kg_mapping_audit_item (
  audit_item_id TEXT PRIMARY KEY, audit_pack_id TEXT NOT NULL REFERENCES kg_mapping_audit_pack(audit_pack_id) ON DELETE CASCADE,
  resource_id TEXT NOT NULL, segment_id TEXT NOT NULL, node_id TEXT NOT NULL, relation_type TEXT NOT NULL,
  grade_scope TEXT, subject_scope TEXT, track TEXT, lexical_evidence TEXT NOT NULL,
  scope_match TEXT NOT NULL, mapping_source_ref TEXT NOT NULL, precision_proxy REAL NOT NULL,
  human_verdict TEXT CHECK(human_verdict IN ('accept','reject','needs_split')), error_category TEXT,
  reviewer TEXT, note TEXT, reviewed_at TEXT, created_at TEXT NOT NULL,
  UNIQUE(audit_pack_id,resource_id,segment_id,node_id,relation_type)
);
CREATE TABLE kg_mapping_audit_pack (
  audit_pack_id TEXT PRIMARY KEY, snapshot_sha256 TEXT NOT NULL, criteria_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open','completed','superseded')), item_count INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE kg_mapping_review (
  review_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  segment_id TEXT REFERENCES kg_segment(segment_id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES kg_node(node_id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','needs_split')),
  reviewer TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,
  UNIQUE(resource_id,segment_id,node_id,relation_type,reviewer)
);
CREATE TABLE kg_mapping_review_event (
  event_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL,
  segment_id TEXT, node_id TEXT NOT NULL, relation_type TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('decision','undo')), decision TEXT,
  reviewer TEXT NOT NULL, note TEXT, previous_decision TEXT,
  client_action_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE kg_mastery_evidence (
  evidence_id TEXT PRIMARY KEY, child_id TEXT NOT NULL, node_id TEXT NOT NULL REFERENCES kg_node(node_id),
  origin_type TEXT NOT NULL CHECK(origin_type IN ('diagnostic','exercise','mistake','review','teacher_observation')),
  origin_id TEXT, correctness REAL NOT NULL CHECK(correctness BETWEEN 0 AND 1), independent INTEGER NOT NULL CHECK(independent IN (0,1)),
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), note TEXT NOT NULL, occurred_at TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE kg_mastery_state (
  child_id TEXT NOT NULL, node_id TEXT NOT NULL REFERENCES kg_node(node_id), mastery_probability REAL NOT NULL CHECK(mastery_probability BETWEEN 0 AND 1),
  evidence_count INTEGER NOT NULL, weakness_signal REAL NOT NULL CHECK(weakness_signal BETWEEN 0 AND 1), algorithm_version TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY(child_id,node_id)
);
CREATE TABLE kg_node (
  node_id TEXT PRIMARY KEY, node_type TEXT NOT NULL CHECK(node_type IN ('standard_capability','stage','grade','semester','subject','unit','knowledge_point','misconception','explanation','exercise','mistake','diagnostic_evidence','mastery_state')),
  canonical_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, description TEXT NOT NULL,
  scope_json TEXT NOT NULL DEFAULT '{}', source_ref TEXT NOT NULL, source_time TEXT, version_label TEXT NOT NULL,
  license_boundary TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','restricted','rejected')),
  status TEXT NOT NULL CHECK(status IN ('active','deprecated','conflicted')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE kg_parent_textbook_capture_lease (
  lease_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id), system_id_hash TEXT NOT NULL,
  capabilities_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','released')),
  opened_at TEXT NOT NULL,last_renewed_at TEXT NOT NULL,expires_at TEXT NOT NULL,released_at TEXT,release_reason TEXT
);
CREATE TABLE kg_parent_textbook_file_source (
  file_source_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  image_sha256 TEXT NOT NULL,page_context_sha256 TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE,
  request_sha256 TEXT NOT NULL,created_at TEXT NOT NULL,
  UNIQUE(file_source_id,child_id,image_sha256,page_context_sha256,client_action_id)
);
CREATE TABLE kg_parent_textbook_form_draft (
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  scope TEXT NOT NULL CHECK(scope IN ('capture','language')),
  revision INTEGER NOT NULL CHECK(revision >= 1),
  encrypted_payload BLOB, payload_sha256 TEXT NOT NULL,
  last_action_id TEXT NOT NULL, last_expected_revision INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(child_id,scope)
);
CREATE TABLE kg_parent_textbook_ocr_artifact (
  artifact_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id),lease_id TEXT REFERENCES kg_parent_textbook_capture_lease(lease_id),
  source_kind TEXT NOT NULL DEFAULT 'camera' CHECK(source_kind IN ('camera','file')),file_source_id TEXT,
  page_context_json TEXT NOT NULL,extracted_text TEXT NOT NULL,confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),details_json TEXT NOT NULL,
  image_sha256 TEXT NOT NULL DEFAULT '',page_context_sha256 TEXT NOT NULL DEFAULT '',
  raw_image_path TEXT,raw_image_media_type TEXT,raw_image_byte_length INTEGER NOT NULL DEFAULT 0,
  raw_image_encryption_version TEXT,
  status TEXT NOT NULL CHECK(status IN ('processing','needs_review','ready','importing','imported','skipped','failed')),
  import_action_id TEXT,import_payload_sha256 TEXT,import_request_json TEXT,import_claimed_at TEXT,
  created_at TEXT NOT NULL,updated_at TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE, import_owner_boot_id TEXT,
  CHECK((source_kind='camera' AND lease_id IS NOT NULL AND file_source_id IS NULL)
     OR (source_kind='file' AND lease_id IS NULL AND file_source_id IS NOT NULL)),
  FOREIGN KEY(file_source_id,child_id,image_sha256,page_context_sha256,client_action_id)
    REFERENCES kg_parent_textbook_file_source(file_source_id,child_id,image_sha256,page_context_sha256,client_action_id)
);
CREATE TABLE kg_parent_textbook_ocr_review_event (
  event_id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL REFERENCES kg_parent_textbook_ocr_artifact(artifact_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK(event_type IN ('created','reviewed','review_undone','skipped','restored','retry_requested','failed','recovery_required','import_recovered','imported','raw_source_missing')),
  payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL
);
CREATE TABLE kg_resource (
  resource_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES kg_corpus(corpus_id) ON DELETE CASCADE,
  external_id TEXT NOT NULL, title TEXT NOT NULL, grade_scope TEXT, subject_scope TEXT, semester_scope TEXT,
  transcript_relative_path TEXT NOT NULL, video_relative_path TEXT, transcript_sha256 TEXT NOT NULL,
  video_sha256 TEXT, video_fingerprint TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
  source_ref TEXT NOT NULL, license_boundary TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','restricted','rejected')),
  version_label TEXT NOT NULL, ingested_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(corpus_id, external_id)
);
CREATE TABLE kg_resource_node (
  resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  segment_id TEXT REFERENCES kg_segment(segment_id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES kg_node(node_id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL CHECK(relation_type IN ('covers','explains','exemplifies','assesses')),
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), review_status TEXT NOT NULL,
  source_ref TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(resource_id, segment_id, node_id, relation_type)
);
CREATE TABLE kg_review_plan (
  review_plan_id TEXT PRIMARY KEY, child_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','superseded','completed')),
  algorithm_version TEXT NOT NULL, item_count INTEGER NOT NULL,
  source_evidence_count INTEGER NOT NULL, plan_json TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE, generated_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE kg_schema_migration (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE kg_segment (
  segment_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, start_char INTEGER NOT NULL, end_char INTEGER NOT NULL, text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL, source_ref TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','restricted','rejected')),
  created_at TEXT NOT NULL, UNIQUE(resource_id, ordinal), UNIQUE(resource_id, text_sha256)
);
CREATE TABLE kg_segment_embedding (
  segment_id TEXT PRIMARY KEY REFERENCES kg_segment(segment_id) ON DELETE CASCADE,
  algorithm TEXT NOT NULL, dimensions INTEGER NOT NULL, vector BLOB NOT NULL, updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE kg_segment_fts USING fts5(
  segment_id UNINDEXED, text, title, subject_scope, grade_scope, tokenize='trigram'
);
CREATE TABLE kg_segment_occurrence (
  resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  segment_id TEXT NOT NULL REFERENCES kg_segment(segment_id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
  PRIMARY KEY(resource_id,segment_id)
);
CREATE TABLE kg_segment_occurrence_pending (
  segment_id TEXT PRIMARY KEY REFERENCES kg_segment(segment_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE TABLE knowledge_edge (
  edge_id TEXT PRIMARY KEY,
  source_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
  target_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
  relation_type TEXT NOT NULL CHECK(relation_type IN (
    'prerequisite_of', 'part_of', 'taught_in', 'assessed_by',
    'misconception_of', 'remediated_by', 'transfers_to'
  )),
  rationale TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  knowledge_version TEXT NOT NULL,
  status TEXT NOT NULL,
  UNIQUE(source_node_id, target_node_id, relation_type, knowledge_version)
);
CREATE TABLE knowledge_excerpt (
  excerpt_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES knowledge_source(source_id),
  sequence INTEGER NOT NULL,
  heading TEXT,
  content_text TEXT NOT NULL,
  content_hash TEXT NOT NULL
);
CREATE TABLE knowledge_node (
  knowledge_node_id TEXT PRIMARY KEY,
  node_type TEXT NOT NULL DEFAULT 'knowledge_point',
  track TEXT NOT NULL,
  subject TEXT NOT NULL,
  grade_band TEXT NOT NULL,
  code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  knowledge_version TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE "knowledge_source" ("source_id" TEXT,"child_id" TEXT,"source_type" TEXT NOT NULL,"title" TEXT NOT NULL,"subject" TEXT NOT NULL,"grade_band" TEXT NOT NULL,"publisher" TEXT,"version_label" TEXT,"source_ref" TEXT NOT NULL,"effective_from" TEXT,"effective_to" TEXT,"license_status" TEXT NOT NULL,"review_status" TEXT NOT NULL,"content_hash" TEXT NOT NULL,"imported_at" TEXT NOT NULL,current_version INTEGER NOT NULL DEFAULT 1 CHECK(current_version IN (0,1)),predecessor_id TEXT,PRIMARY KEY ("source_id"),FOREIGN KEY ("child_id") REFERENCES "child_profile"("child_id") ON DELETE NO ACTION ON UPDATE NO ACTION);
CREATE TABLE knowledge_source_revision_event(
        event_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id),
        old_source_id TEXT REFERENCES knowledge_source(source_id) ON DELETE SET NULL,
        new_source_id TEXT NOT NULL REFERENCES knowledge_source(source_id) ON DELETE CASCADE,
        action TEXT NOT NULL CHECK(action IN ('revise','review')),
        client_action_id TEXT NOT NULL, request_hash TEXT NOT NULL,
        previous_version TEXT NOT NULL, actor TEXT NOT NULL, occurred_at TEXT NOT NULL, previous_review_status TEXT, new_excerpts_hash TEXT,
        UNIQUE(child_id,client_action_id));
CREATE TABLE learning_evidence (
  evidence_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT REFERENCES daily_session(session_id),
  knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
  evidence_type TEXT NOT NULL,
  correctness REAL,
  hint_level INTEGER NOT NULL,
  independent INTEGER NOT NULL,
  confidence REAL NOT NULL,
  evidence_summary TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE learning_evidence_withdrawal (
  withdrawal_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL UNIQUE REFERENCES learning_evidence(evidence_id) ON DELETE CASCADE,
  reason TEXT NOT NULL CHECK(reason IN ('answer_corrected', 'answer_withdrawn')),
  replacement_evidence_id TEXT REFERENCES learning_evidence(evidence_id) ON DELETE RESTRICT,
  session_id TEXT REFERENCES daily_session(session_id) ON DELETE SET NULL,
  client_action_id TEXT NOT NULL UNIQUE,
  request_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK(replacement_evidence_id IS NULL OR replacement_evidence_id != evidence_id)
);
CREATE TABLE maintenance_run (
  run_id TEXT PRIMARY KEY,
  job_type TEXT NOT NULL CHECK(job_type IN ('retention_cleanup', 'daily_backup')),
  run_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'failed')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  error_code TEXT,
  error_message TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(job_type, run_date)
);
CREATE TABLE mastery_state (
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
  mastery_probability REAL NOT NULL,
  evidence_count INTEGER NOT NULL,
  hint_dependency REAL NOT NULL,
  status_confidence REAL NOT NULL,
  last_practiced_at TEXT,
  last_independent_correct_at TEXT,
  forgetting_rate REAL NOT NULL DEFAULT 0.2,
  next_review_at TEXT,
  algorithm_version TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(child_id, knowledge_node_id)
);
CREATE TABLE mastery_update_event (
  event_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
  evidence_id TEXT NOT NULL UNIQUE REFERENCES learning_evidence(evidence_id),
  old_state_json TEXT NOT NULL,
  new_state_json TEXT NOT NULL,
  algorithm_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE mistake_attempt (
  attempt_id TEXT PRIMARY KEY,
  mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
  evidence_id TEXT NOT NULL UNIQUE REFERENCES learning_evidence(evidence_id) ON DELETE CASCADE,
  stage TEXT NOT NULL CHECK(stage IN (
    'repair_check', 'near_transfer', 'representation_transfer',
    'discrimination', 'far_transfer', 'delayed_48h', 'delayed_7d'
  )),
  submitted_answer TEXT NOT NULL,
  correctness REAL CHECK(correctness >= 0 AND correctness <= 1),
  hint_level INTEGER NOT NULL CHECK(hint_level BETWEEN 0 AND 5),
  independent INTEGER NOT NULL CHECK(independent IN (0, 1)),
  feedback TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE mistake_capture_candidate (
  candidate_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL REFERENCES capture_artifact(artifact_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  question_index INTEGER NOT NULL CHECK(question_index >= 0),
  question_text TEXT NOT NULL,
  region_json TEXT,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  status TEXT NOT NULL CHECK(status IN ('pending', 'saved', 'skipped')),
  mistake_id TEXT REFERENCES mistake_case(mistake_id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(artifact_id, question_index)
);
CREATE TABLE mistake_capture_candidate_event (
  event_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES mistake_capture_candidate(candidate_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK(event_type IN ('prepared', 'edited', 'skipped', 'captured')),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE mistake_case (
  mistake_id TEXT PRIMARY KEY,
  cluster_id TEXT NOT NULL REFERENCES mistake_cluster(cluster_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
  diagnostic_item_id TEXT REFERENCES diagnostic_item(diagnostic_item_id),
  source_evidence_id TEXT REFERENCES learning_evidence(evidence_id) ON DELETE SET NULL,
  subject TEXT NOT NULL,
  title TEXT NOT NULL,
  prompt_summary TEXT NOT NULL,
  submitted_answer TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_ref TEXT,
  fingerprint TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  status TEXT NOT NULL CHECK(status IN (
    'captured', 'awaiting_confirmation', 'diagnosing', 'remediating',
    'near_transfer', 'mixed_transfer', 'delayed_verification', 'fragile',
    'mastered', 'relapsed', 'excluded'
  )),
  confirmed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE,
  UNIQUE(child_id, fingerprint)
);
CREATE TABLE mistake_cluster (
  cluster_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
  subject TEXT NOT NULL,
  title TEXT NOT NULL,
  diagnosis_status TEXT NOT NULL CHECK(diagnosis_status IN (
    'suspected', 'emerging_pattern', 'confirmed_gap', 'improving', 'stable'
  )),
  occurrence_count INTEGER NOT NULL DEFAULT 0,
  relapse_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(child_id, knowledge_node_id)
);
CREATE TABLE mistake_diagnosis (
  diagnosis_id TEXT PRIMARY KEY,
  mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  error_type TEXT NOT NULL CHECK(error_type IN (
    'concept_gap', 'prerequisite_gap', 'reading_gap', 'method_gap',
    'execution_error', 'retrieval_failure', 'transfer_gap', 'incidental', 'unknown'
  )),
  explanation TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  status TEXT NOT NULL CHECK(status IN ('candidate', 'confirmed', 'rejected')),
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE mistake_dialogue (
  session_id TEXT PRIMARY KEY REFERENCES daily_session(session_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  candidate_id TEXT REFERENCES mistake_capture_candidate(candidate_id) ON DELETE SET NULL,
  mistake_id TEXT REFERENCES mistake_case(mistake_id) ON DELETE SET NULL,
  phase TEXT NOT NULL,
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE mistake_dialogue_event (
  client_action_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  request_hash TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE mistake_mastery_gate (
  mistake_id TEXT PRIMARY KEY REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
  error_type TEXT NOT NULL,
  requirements_json TEXT NOT NULL,
  delayed_48h_due_at TEXT,
  delayed_7d_due_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE mistake_occurrence (
  occurrence_id TEXT PRIMARY KEY,
  mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  evidence_id TEXT REFERENCES learning_evidence(evidence_id) ON DELETE SET NULL,
  submitted_answer TEXT NOT NULL,
  source_ref TEXT,
  occurred_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE mistake_source_revision (
        revision_id TEXT PRIMARY KEY,
        mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
        version INTEGER NOT NULL CHECK(version > 0),
        question TEXT NOT NULL, artifact_id TEXT NOT NULL,
        before_json TEXT NOT NULL, after_hash TEXT NOT NULL,
        offer_json TEXT NOT NULL, confirmation_turn_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL, UNIQUE(mistake_id,version));
CREATE TABLE mistake_status_event (
  event_id TEXT PRIMARY KEY,
  mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  from_status TEXT,
  to_status TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE model_call (
  call_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  session_id TEXT REFERENCES daily_session(session_id),
  child_id TEXT REFERENCES child_profile(child_id),
  purpose TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  latency_ms INTEGER,
  input_chars INTEGER NOT NULL,
  output_chars INTEGER,
  prompt_version TEXT NOT NULL,
  context_manifest_json TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  provider_preference TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  knowledge_version TEXT,
  verification_status TEXT,
  safety_status TEXT,
  fallback_used INTEGER NOT NULL DEFAULT 0 CHECK(fallback_used IN (0, 1)),
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  input_price_cny_per_million REAL,
  output_price_cny_per_million REAL,
  estimated_cost_cny REAL,
  error_code TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE model_pricing (
  provider TEXT NOT NULL CHECK(provider IN ('qwen', 'deepseek')),
  model TEXT NOT NULL,
  input_price_cny_per_million REAL NOT NULL CHECK(input_price_cny_per_million >= 0),
  output_price_cny_per_million REAL NOT NULL CHECK(output_price_cny_per_million >= 0),
  currency TEXT NOT NULL DEFAULT 'CNY' CHECK(currency = 'CNY'),
  source TEXT NOT NULL DEFAULT 'parent_configured' CHECK(source = 'parent_configured'),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(provider, model)
);
CREATE TABLE observable_event (
  event_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL,
  session_id TEXT,
  occurred_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence REAL NOT NULL,
  source_refs_json TEXT NOT NULL,
  missing_sources_json TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  alternatives_json TEXT NOT NULL
);
CREATE TABLE observation_calibration_run (
  run_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  camera_system_id_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active', 'passed', 'failed', 'cancelled', 'interrupted')),
  result_json TEXT NOT NULL DEFAULT '{}',
  failure_code TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE observation_control_action (
  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
  client_action_id TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('paused', 'enabled')),
  expected_revision INTEGER,
  expected_generation TEXT,
  effects_complete INTEGER NOT NULL DEFAULT 0 CHECK(effects_complete IN (0,1)),
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(session_id, client_action_id)
);
CREATE TABLE observation_correction (
  correction_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  state_event_id TEXT REFERENCES child_state_event(state_event_id),
  previous_state TEXT,
  correction_text TEXT NOT NULL,
  effect TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE observation_qualification (
  session_id TEXT PRIMARY KEY REFERENCES daily_session(session_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('qualifying', 'qualified', 'unqualified', 'paused', 'unavailable')),
  scene_quality TEXT NOT NULL CHECK(scene_quality IN ('not_evaluated', 'ready', 'too_dark', 'too_bright', 'blurred', 'unusable_frame')),
  person_baseline TEXT NOT NULL CHECK(person_baseline IN ('not_evaluated', 'established', 'awaiting_person')),
  reason TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('engine', 'renderer')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  qualified_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE ocr_cache(
        owner_key TEXT NOT NULL,image_hash TEXT NOT NULL,result_json TEXT NOT NULL,
        created_at TEXT NOT NULL,PRIMARY KEY(owner_key,image_hash));
CREATE TABLE ocr_cache_epoch(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),epoch TEXT NOT NULL);
CREATE TABLE ocr_job(
        job_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, image_hash TEXT NOT NULL,
        preferred_route TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,
        started_at TEXT,completed_at TEXT,result_json TEXT,error_code TEXT,
        child_id TEXT,session_id TEXT,parent_artifact_id TEXT,artifact_id TEXT,
        owner_key TEXT NOT NULL DEFAULT 'isolated',owner_epoch TEXT,
        monitor_generation INTEGER,pipeline_version INTEGER,run_token TEXT,
        publication_started INTEGER NOT NULL DEFAULT 0);
CREATE TABLE ocr_legacy_reference(
        store_id TEXT NOT NULL,job_id TEXT NOT NULL,child_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,source_kind TEXT NOT NULL,image_hash TEXT NOT NULL,
        PRIMARY KEY(store_id,job_id,child_id,artifact_id,source_kind));
CREATE TABLE ocr_request_tombstone(
        request_digest TEXT PRIMARY KEY,reason TEXT NOT NULL);
CREATE TABLE ocr_restore_fence(
        restore_id TEXT PRIMARY KEY);
CREATE TABLE ocr_review_batch(
        client_action_id TEXT PRIMARY KEY, child_id TEXT NOT NULL,
        request_json TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE ocr_review_event(
        event_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, review_id TEXT,
        event_type TEXT NOT NULL, client_action_id TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE ocr_review_item(
        review_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, child_id TEXT NOT NULL,
        session_id TEXT, review_kind TEXT NOT NULL, region_index INTEGER NOT NULL,
        region_json TEXT, proposed_text TEXT NOT NULL, confidence REAL NOT NULL,
        status TEXT NOT NULL, corrected_text TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE ocr_review_legacy_import(
        source_path TEXT PRIMARY KEY);
CREATE TABLE paper_answer_authority (
          authority_id TEXT PRIMARY KEY,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT REFERENCES daily_session(session_id) ON DELETE CASCADE,
          question_text TEXT NOT NULL,
          question_sha256 TEXT NOT NULL,
          authority_type TEXT NOT NULL CHECK(authority_type IN (
            'published_answer_key','agent_generated_rubric'
          )),
          authority_ref TEXT NOT NULL,
          expected_answers_json TEXT NOT NULL,
          rubric_version TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status='qualified'),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE,
          UNIQUE(child_id, question_sha256, authority_type, authority_ref)
        );
CREATE TABLE paper_correct_revision_event (
          event_id TEXT PRIMARY KEY,
          obligation_id TEXT NOT NULL
            REFERENCES paper_correct_revision_obligation(obligation_id) ON DELETE CASCADE,
          target_binding_id TEXT
            REFERENCES paper_correct_revision_target(target_binding_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN (
            'pending_supply','scheduled','target_invalid','withdrawn'
          )),
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_correct_revision_obligation (
          obligation_id TEXT PRIMARY KEY,
          revision_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_decision_revision(revision_id) ON DELETE CASCADE,
          request_id TEXT NOT NULL
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
          source_evidence_id TEXT NOT NULL UNIQUE REFERENCES learning_evidence(evidence_id),
          source_question_identity TEXT NOT NULL,
          due_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_correct_revision_target (
          target_binding_id TEXT PRIMARY KEY,
          obligation_id TEXT NOT NULL
            REFERENCES paper_correct_revision_obligation(obligation_id) ON DELETE CASCADE,
          diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
          target_question_identity TEXT NOT NULL,
          review_task_id TEXT NOT NULL UNIQUE REFERENCES review_task(review_task_id),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE,
          UNIQUE(obligation_id,target_question_identity)
        );
CREATE TABLE paper_correct_verification_event (
          event_id TEXT PRIMARY KEY,
          obligation_id TEXT NOT NULL
            REFERENCES paper_correct_verification_obligation(obligation_id) ON DELETE CASCADE,
          target_binding_id TEXT
            REFERENCES paper_correct_verification_target(target_binding_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN (
            'pending_supply','scheduled','target_invalid','withdrawn'
          )),
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_correct_verification_obligation (
          obligation_id TEXT PRIMARY KEY,
          decision_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_decision(decision_id) ON DELETE CASCADE,
          request_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
          source_evidence_id TEXT NOT NULL UNIQUE REFERENCES learning_evidence(evidence_id),
          source_question_identity TEXT NOT NULL,
          due_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_correct_verification_target (
          target_binding_id TEXT PRIMARY KEY,
          obligation_id TEXT NOT NULL
            REFERENCES paper_correct_verification_obligation(obligation_id) ON DELETE CASCADE,
          diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
          target_question_identity TEXT NOT NULL,
          review_task_id TEXT NOT NULL UNIQUE REFERENCES review_task(review_task_id),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE,
          UNIQUE(obligation_id, target_question_identity)
        );
CREATE TABLE paper_scoring_decision (
          decision_id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          authority_type TEXT NOT NULL CHECK(authority_type IN (
            'teacher_mark','published_answer_key','agent_generated_rubric'
          )),
          authority_ref TEXT NOT NULL,
          authority_snapshot_json TEXT NOT NULL,
          correctness REAL NOT NULL CHECK(correctness IN (0,1)),
          confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
          status TEXT NOT NULL CHECK(status='qualified'),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_scoring_decision_revision (
          revision_id TEXT PRIMARY KEY,
          decision_id TEXT NOT NULL
            REFERENCES paper_scoring_decision(decision_id) ON DELETE CASCADE,
          request_id TEXT NOT NULL
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          revision_sequence INTEGER NOT NULL CHECK(revision_sequence >= 1),
          previous_revision_id TEXT
            REFERENCES paper_scoring_decision_revision(revision_id),
          authority_type TEXT NOT NULL CHECK(authority_type IN (
            'teacher_mark','published_answer_key','agent_generated_rubric'
          )),
          authority_ref TEXT NOT NULL,
          authority_snapshot_json TEXT NOT NULL,
          correctness REAL NOT NULL CHECK(correctness IN (0,1)),
          confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE,
          UNIQUE(decision_id,revision_sequence)
        );
CREATE TABLE paper_scoring_decision_status_event (
          event_id TEXT PRIMARY KEY,
          decision_id TEXT NOT NULL
            REFERENCES paper_scoring_decision(decision_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN ('qualified','withdrawn')),
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_scoring_dialogue_revision_reconciliation (
          revision_id TEXT PRIMARY KEY
            REFERENCES paper_scoring_decision_revision(revision_id) ON DELETE CASCADE,
          request_id TEXT NOT NULL
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          outcome TEXT NOT NULL CHECK(outcome IN (
            'mistake_created','correct_verification_scheduled','incorrect_unmapped'
          )),
          mistake_id TEXT REFERENCES mistake_case(mistake_id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_scoring_request (
          request_id TEXT PRIMARY KEY,
          -- Optional provenance only. Candidate/session ownership provides the
          -- cascade boundary, so voice-only installs need not initialize the
          -- document-monitoring schema merely to present a learning item.
          visual_answer_evidence_id TEXT,
          candidate_id TEXT NOT NULL UNIQUE
            REFERENCES mistake_capture_candidate(candidate_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          question_text TEXT NOT NULL,
          answer_text TEXT NOT NULL,
          answer_source TEXT NOT NULL
            CHECK(answer_source IN ('visual_unverified','child_spoken')),
          question_sha256 TEXT NOT NULL,
          answer_sha256 TEXT NOT NULL,
          confirmation_source TEXT NOT NULL
            CHECK(confirmation_source='child_spoken_readback'),
          fact_source TEXT CHECK(fact_source IN (
            'child_spoken_readback','qualified_visual_observation','system_presented_attempt'
          )),
          fact_confidence REAL CHECK(fact_confidence IS NULL OR
            (fact_confidence >= 0 AND fact_confidence <= 1)),
          status TEXT NOT NULL CHECK(status='pending_authority'),
          correctness REAL CHECK(correctness IS NULL),
          mastery_update_allowed INTEGER NOT NULL DEFAULT 0
            CHECK(mastery_update_allowed=0),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_scoring_revision_mistake_link (
          revision_id TEXT PRIMARY KEY
            REFERENCES paper_scoring_decision_revision(revision_id) ON DELETE CASCADE,
          mistake_id TEXT NOT NULL UNIQUE REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_scoring_revision_status_event (
          event_id TEXT PRIMARY KEY,
          revision_id TEXT NOT NULL
            REFERENCES paper_scoring_decision_revision(revision_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN ('qualified','withdrawn')),
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_scoring_supply_event (
          event_id TEXT PRIMARY KEY,
          obligation_id TEXT NOT NULL
            REFERENCES paper_scoring_supply_obligation(obligation_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN (
            'pending_authority','source_candidate','rubric_candidate',
            'calibration_pending','failed_retryable','blocked_source',
            'qualified','withdrawn'
          )),
          reason TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_scoring_supply_obligation (
          obligation_id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          question_sha256 TEXT NOT NULL,
          answer_sha256 TEXT NOT NULL,
          authority_requirement_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE paper_scoring_supply_work (
          obligation_id TEXT PRIMARY KEY
            REFERENCES paper_scoring_supply_obligation(obligation_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','blocked')),
          attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
          owner_token TEXT,
          claimed_at TEXT,
          error_code TEXT,
          provider TEXT,
          model TEXT,
          prompt_tokens INTEGER,
          completion_tokens INTEGER,
          updated_at TEXT NOT NULL
        );
CREATE TABLE paper_subjective_score_binding (
          binding_id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          decision_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_decision(decision_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          assessment_id TEXT NOT NULL,
          source_evidence_id TEXT NOT NULL,
          assessment_fingerprint TEXT NOT NULL,
          authority_snapshot_sha256 TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
CREATE TABLE parent_report_feedback (
  feedback_id TEXT PRIMARY KEY,
  report_id TEXT NOT NULL UNIQUE REFERENCES daily_report(report_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  executed INTEGER NOT NULL CHECK(executed IN (0, 1)),
  effectiveness INTEGER CHECK(effectiveness BETWEEN 1 AND 5),
  stress_rating INTEGER CHECK(stress_rating BETWEEN 1 AND 5),
  notes_summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE parent_security (
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
  pin_salt BLOB NOT NULL,
  pin_hash BLOB NOT NULL,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE period_report (
  period_report_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  report_type TEXT NOT NULL CHECK(report_type IN ('weekly', 'monthly')),
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  source TEXT NOT NULL,
  content_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(child_id, report_type, period_start, period_end)
);
CREATE TABLE period_report_evidence_action (
  client_action_id TEXT PRIMARY KEY,
  evidence_id TEXT NOT NULL,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  action_type TEXT NOT NULL CHECK(action_type IN ('retract', 'restore')),
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE period_report_evidence_reference (
  evidence_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  evidence_ref TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE period_report_evidence_state (
  evidence_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  is_retracted INTEGER NOT NULL CHECK(is_retracted IN (0, 1)),
  note TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE period_report_refresh_state (
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  report_type TEXT NOT NULL CHECK(report_type IN ('weekly', 'monthly')),
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(child_id, report_type)
);
CREATE TABLE plan_constraint_decision (
  decision_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES daily_plan(plan_id) ON DELETE CASCADE,
  obligation_type TEXT NOT NULL,
  source_ref TEXT,
  decision TEXT NOT NULL CHECK(decision IN ('scheduled','deferred','applied','suppressed')),
  reason TEXT NOT NULL,
  requested_minutes INTEGER NOT NULL DEFAULT 0,
  allocated_minutes INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE plan_item (
  plan_item_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES daily_plan(plan_id),
  homework_id TEXT REFERENCES homework_item(homework_id),
  subject TEXT NOT NULL,
  title TEXT NOT NULL,
  expected_minutes INTEGER NOT NULL,
  actual_minutes INTEGER,
  priority INTEGER NOT NULL,
  status TEXT NOT NULL,
  position INTEGER NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  active_seconds INTEGER NOT NULL DEFAULT 0,
  active_segment_started_at TEXT,
  source_type TEXT NOT NULL DEFAULT 'school_homework',
  source_ref TEXT,
  completion_standard TEXT NOT NULL DEFAULT '孩子确认完成',
  cognitive_load TEXT NOT NULL DEFAULT 'medium',
  task_type TEXT NOT NULL DEFAULT 'written',
  completion_evidence_type TEXT,
  completion_note TEXT,
  completion_checklist_json TEXT NOT NULL DEFAULT '[]',
  completion_artifact_id TEXT REFERENCES capture_artifact(artifact_id)
);
CREATE TABLE plan_item_event (
  event_id TEXT PRIMARY KEY,
  plan_item_id TEXT NOT NULL REFERENCES plan_item(plan_item_id),
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE plan_revision (
  revision_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES daily_plan(plan_id),
  strategy TEXT NOT NULL,
  reason TEXT NOT NULL,
  previous_ordered_item_ids_json TEXT NOT NULL DEFAULT '[]',
  ordered_item_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE planning_context_event (
  event_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE privacy_preference (
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
  cloud_model_text_allowed INTEGER NOT NULL,
  cloud_audio_allowed INTEGER NOT NULL,
  raw_media_storage_allowed INTEGER NOT NULL,
  model_improvement_allowed INTEGER NOT NULL,
  model_call_retention_days INTEGER NOT NULL,
  tutor_turn_retention_days INTEGER NOT NULL,
  report_retention_days INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE probabilistic_state (
  state_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES observable_event(event_id),
  child_id TEXT NOT NULL,
  session_id TEXT,
  occurred_at TEXT NOT NULL,
  state TEXT NOT NULL,
  probability REAL NOT NULL,
  evidence_json TEXT NOT NULL,
  alternatives_json TEXT NOT NULL
);
CREATE TABLE recall_review (
  review_id TEXT PRIMARY KEY,
  recall_id TEXT NOT NULL REFERENCES classroom_recall(recall_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  decision TEXT NOT NULL,
  previous_status TEXT NOT NULL,
  resulting_status TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE remediation_step (
  step_id TEXT PRIMARY KEY,
  mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  step_type TEXT NOT NULL CHECK(step_type IN (
    'concept_explanation', 'prerequisite_review', 'problem_restatement',
    'method_comparison', 'execution_check', 'active_recall', 'representation_change'
  )),
  outcome TEXT NOT NULL CHECK(outcome IN ('completed', 'still_stuck', 'skipped')),
  notes TEXT,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE report_job (
  job_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  report_type TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE report_retry_request (
  retry_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES report_job(job_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  status TEXT NOT NULL CHECK(status IN ('started', 'completed', 'failed')),
  result_report_id TEXT REFERENCES daily_report(report_id),
  error_code TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE "review_task" ("review_task_id" TEXT,"child_id" TEXT NOT NULL,"knowledge_node_id" TEXT NOT NULL,"diagnostic_item_id" TEXT NOT NULL,"source_evidence_id" TEXT NOT NULL,"interval_label" TEXT NOT NULL,"due_at" TEXT NOT NULL,"status" TEXT NOT NULL,"completed_at" TEXT,"result_evidence_id" TEXT,"created_at" TEXT NOT NULL,"score_authority_id" TEXT,"score_status" TEXT,"predecessor_review_task_id" TEXT,PRIMARY KEY (review_task_id),FOREIGN KEY ("result_evidence_id") REFERENCES "learning_evidence"("evidence_id") ON DELETE NO ACTION ON UPDATE NO ACTION,FOREIGN KEY ("source_evidence_id") REFERENCES "learning_evidence"("evidence_id") ON DELETE NO ACTION ON UPDATE NO ACTION,FOREIGN KEY ("diagnostic_item_id") REFERENCES "diagnostic_item"("diagnostic_item_id") ON DELETE NO ACTION ON UPDATE NO ACTION,FOREIGN KEY ("knowledge_node_id") REFERENCES "knowledge_node"("knowledge_node_id") ON DELETE NO ACTION ON UPDATE NO ACTION,FOREIGN KEY ("child_id") REFERENCES "child_profile"("child_id") ON DELETE NO ACTION ON UPDATE NO ACTION);
CREATE TABLE runtime_data_scope (
      singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
      mode TEXT NOT NULL CHECK(mode IN ('formal','trial')),
      created_at TEXT NOT NULL);
CREATE TABLE runtime_state (
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
  clean_shutdown INTEGER NOT NULL CHECK(clean_shutdown IN (0, 1)),
  run_id TEXT,
  started_at TEXT,
  ended_at TEXT
);
CREATE TABLE schema_migration (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
INSERT INTO "schema_migration" VALUES(1,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(2,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(3,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(4,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(5,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(6,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(7,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(8,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(9,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(10,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(11,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(12,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(13,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(14,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(15,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(16,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(17,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(18,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(19,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(20,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(21,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(22,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(23,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(24,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(25,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(26,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(27,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(28,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(29,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(30,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(31,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(32,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(33,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(34,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(35,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(36,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(37,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(38,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(39,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(40,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(41,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(42,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(43,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(44,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(45,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(46,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(47,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(48,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(49,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(50,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(51,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(52,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(53,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(54,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(55,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(56,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(57,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(58,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(59,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(60,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(61,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(62,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(63,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(64,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(65,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(66,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(67,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(68,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(69,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(70,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(71,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(72,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(73,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(74,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(75,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(76,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(77,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(78,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(79,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(80,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(81,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(82,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(83,'2026-10-20 20:09:21');
INSERT INTO "schema_migration" VALUES(84,'2026-10-20 20:09:21');
CREATE TABLE sensor_lease (
  lease_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  device_id TEXT NOT NULL REFERENCES device_profile(device_id),
  device_type TEXT NOT NULL,
  status TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  last_renewed_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT,
  release_reason TEXT
);
CREATE TABLE sensor_release_verification (
  verification_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  result TEXT NOT NULL CHECK(result IN ('passed', 'failed')),
  used_device_types_json TEXT NOT NULL,
  reopened_device_types_json TEXT NOT NULL,
  device_manager_healthy INTEGER NOT NULL CHECK(device_manager_healthy IN (0, 1)),
  windows_privacy_indicator_clear INTEGER NOT NULL CHECK(windows_privacy_indicator_clear IN (0, 1)),
  physical_indicator_status TEXT NOT NULL CHECK(physical_indicator_status IN ('clear', 'not_available', 'still_on')),
  platform_name TEXT NOT NULL CHECK(platform_name = 'Windows'),
  platform_version TEXT NOT NULL,
  notes TEXT,
  verified_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE session_declaration (
  declaration_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  declaration_type TEXT NOT NULL CHECK(declaration_type IN ('no_recall', 'no_homework')),
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE session_draft (
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  section TEXT NOT NULL CHECK(section IN (
    'check_in', 'classroom_recall', 'homework_capture',
    'study', 'reflection', 'agent_command'
  )),
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, section)
);
CREATE TABLE session_planning_context (
  session_id TEXT PRIMARY KEY REFERENCES daily_session(session_id),
  energy_level TEXT NOT NULL CHECK(energy_level IN ('low', 'medium', 'high')),
  fixed_commitment_minutes INTEGER NOT NULL CHECK(fixed_commitment_minutes BETWEEN 0 AND 180),
  fixed_commitment_note TEXT,
  tomorrow_event_subject TEXT,
  tomorrow_event_title TEXT,
  tomorrow_event_minutes INTEGER NOT NULL CHECK(tomorrow_event_minutes BETWEEN 0 AND 60),
  updated_at TEXT NOT NULL
);
CREATE TABLE session_transition (
  transition_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  from_state TEXT,
  to_state TEXT NOT NULL,
  reason TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  client_action_id TEXT
);
DELETE FROM "sqlite_sequence";
CREATE TABLE strategy_evaluation_event (
  event_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL REFERENCES strategy_experiment(experiment_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  phase TEXT NOT NULL CHECK(phase IN ('replay', 'shadow', 'active')),
  event_type TEXT NOT NULL,
  value REAL,
  source_ref TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  UNIQUE(experiment_id, phase, event_type, source_ref)
);
CREATE TABLE strategy_experiment (
  experiment_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  strategy_key TEXT NOT NULL CHECK(strategy_key = 'tutor_hint_pacing'),
  baseline_version TEXT NOT NULL,
  candidate_version TEXT NOT NULL,
  candidate_config_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'draft', 'replay_passed', 'shadow', 'active', 'rolled_back', 'rejected'
  )),
  replay_metrics_json TEXT,
  shadow_metrics_json TEXT,
  active_metrics_json TEXT,
  rollback_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  activated_at TEXT,
  ended_at TEXT
);
CREATE TABLE strategy_transition_event (
  transition_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL REFERENCES strategy_experiment(experiment_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  from_status TEXT,
  to_status TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL
);
CREATE TABLE tutor_feedback (
  feedback_id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL REFERENCES tutor_turn(turn_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  response_text TEXT NOT NULL,
  outcome TEXT NOT NULL,
  error_hypothesis TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_basis TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE tutor_turn (
  turn_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  plan_item_id TEXT REFERENCES plan_item(plan_item_id),
  subject TEXT NOT NULL,
  question TEXT NOT NULL,
  answer TEXT,
  provider TEXT,
  model TEXT,
  status TEXT NOT NULL,
  hint_level INTEGER NOT NULL DEFAULT 1,
  parent_turn_id TEXT REFERENCES tutor_turn(turn_id),
  response_kind TEXT NOT NULL DEFAULT 'hint',
  verification_status TEXT NOT NULL DEFAULT 'not_applicable_hint',
  safety_status TEXT NOT NULL DEFAULT 'passed',
  strategy_experiment_id TEXT,
  strategy_version TEXT,
  output_envelope_json TEXT,
  tutor_request_hash TEXT,
  ocr_confirmation_id TEXT,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE tutor_voice_playback (
 playback_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES tutor_turn(turn_id) ON DELETE CASCADE,
 session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
 child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
 source_version TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=1),
 lease_id TEXT, lease_generation INTEGER, lease_kind TEXT,
 status TEXT NOT NULL CHECK(status IN ('reserved','started','finished','cancelled')),
 client_action_id TEXT NOT NULL UNIQUE, event_source TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(session_id,child_id,generation));
CREATE TABLE voice_calibration_save_action (
                  client_action_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  payload_hash TEXT NOT NULL,
                  saved_at TEXT NOT NULL
                );
CREATE TABLE voice_capture_binding (
    client_action_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
    wav_sha256 TEXT, purpose TEXT NOT NULL,
    prompt_id TEXT, item_version TEXT,
    voice_input_id TEXT UNIQUE REFERENCES voice_input(voice_input_id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared','completed','cancelled')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    question_reference_json TEXT,
    question_validation TEXT NOT NULL DEFAULT 'valid' CHECK(question_validation IN ('valid','stale')),
    activity_reference_json TEXT,
    activity_validation TEXT NOT NULL DEFAULT 'valid' CHECK(activity_validation IN ('valid','stale')),
    pending_evaluation_turn_id TEXT,
    local_recovery_only INTEGER NOT NULL DEFAULT 0 CHECK(local_recovery_only IN (0,1)),
    task_choice_reference_json TEXT,
    source_kind TEXT NOT NULL DEFAULT 'local_wav' CHECK(source_kind IN ('local_wav','realtime_transcript')),
    source_model TEXT,
    speech_started_at TEXT,
    ordinary_response_binding_json TEXT,
    CHECK((source_kind='local_wav' AND wav_sha256 IS NOT NULL)
       OR (source_kind='realtime_transcript' AND wav_sha256 IS NULL AND source_model IS NOT NULL)));
CREATE TABLE voice_input (
  voice_input_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  purpose TEXT NOT NULL,
  transcript TEXT NOT NULL,
  local_path TEXT,
  model_version TEXT NOT NULL,
  duration_ms INTEGER,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE voice_transcription_action (
                  client_action_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  status TEXT NOT NULL CHECK(status IN ('cancelled', 'completed', 'ignored')),
                  updated_at TEXT NOT NULL
                );
CREATE TABLE weekly_goal (
  weekly_goal_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  week_start TEXT NOT NULL,
  week_end TEXT NOT NULL,
  track TEXT NOT NULL CHECK(track IN ('pet', 'competition_math')),
  target_minutes INTEGER NOT NULL CHECK(target_minutes >= 0),
  target_sessions INTEGER NOT NULL CHECK(target_sessions >= 0),
  status TEXT NOT NULL CHECK(status IN ('not_started', 'on_track', 'behind', 'completed')),
  generator TEXT NOT NULL,
  knowledge_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(child_id, week_start, track)
);
CREATE UNIQUE INDEX idx_one_active_session
ON daily_session(status) WHERE status = 'active';
CREATE UNIQUE INDEX idx_device_profile_type_hash
ON device_profile(device_type, system_id_hash);
CREATE INDEX idx_sensor_lease_session_status
ON sensor_lease(session_id, status);
CREATE INDEX idx_sensor_release_verification_session_time
ON sensor_release_verification(session_id, verified_at DESC);
CREATE INDEX idx_device_incident_child_status
ON device_incident(child_id, status, occurred_at);
CREATE INDEX idx_plan_constraint_decision_plan
ON plan_constraint_decision(plan_id, decision, obligation_type);
CREATE UNIQUE INDEX idx_active_plan_session
ON daily_plan(session_id) WHERE status = 'active';
CREATE INDEX idx_weekly_goal_child_week
ON weekly_goal(child_id, week_start, track);
CREATE INDEX idx_deferred_task_review_child_target
ON deferred_task_review(child_id, target_session_id, reviewed_at);
CREATE UNIQUE INDEX idx_strategy_one_open
ON strategy_experiment(child_id, strategy_key)
WHERE status IN ('draft', 'replay_passed', 'shadow', 'active');
CREATE INDEX idx_strategy_evaluation_experiment
ON strategy_evaluation_event(experiment_id, phase, occurred_at);
CREATE INDEX idx_knowledge_edge_target
ON knowledge_edge(target_node_id, relation_type, status);
CREATE INDEX idx_curriculum_resource_grade_subject
ON curriculum_resource(grade_title, subject_title, corpus_id);
CREATE INDEX idx_curriculum_topic_scope
ON curriculum_topic(grade_title, subject_title, semester, maturity);
CREATE INDEX idx_curriculum_capability_scope
ON curriculum_capability(grade_title, subject_title, semester, maturity);
CREATE INDEX idx_curriculum_standard_requirement_scope
ON curriculum_standard_requirement(subject_title, stage_scope, requirement_type, maturity);
CREATE INDEX idx_curriculum_topic_standard_candidate_status
ON curriculum_topic_standard_candidate(alignment_status);
CREATE TRIGGER curriculum_resource_ai AFTER INSERT ON curriculum_resource BEGIN
  INSERT INTO curriculum_resource_fts(rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
  VALUES (new.row_id, new.title, new.description, new.transcript_text, new.unit_path, new.grade_title, new.subject_title);
END;
CREATE TRIGGER curriculum_resource_ad AFTER DELETE ON curriculum_resource BEGIN
  INSERT INTO curriculum_resource_fts(curriculum_resource_fts, rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
  VALUES ('delete', old.row_id, old.title, old.description, old.transcript_text, old.unit_path, old.grade_title, old.subject_title);
END;
CREATE TRIGGER curriculum_resource_au AFTER UPDATE ON curriculum_resource BEGIN
  INSERT INTO curriculum_resource_fts(curriculum_resource_fts, rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
  VALUES ('delete', old.row_id, old.title, old.description, old.transcript_text, old.unit_path, old.grade_title, old.subject_title);
  INSERT INTO curriculum_resource_fts(rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
  VALUES (new.row_id, new.title, new.description, new.transcript_text, new.unit_path, new.grade_title, new.subject_title);
END;
CREATE TRIGGER learning_evidence_withdrawal_no_update
BEFORE UPDATE ON learning_evidence_withdrawal
BEGIN SELECT RAISE(ABORT, 'immutable learning evidence withdrawal'); END;
CREATE INDEX idx_mastery_update_child_node
ON mastery_update_event(child_id, knowledge_node_id, created_at);
CREATE INDEX idx_mistake_case_child_status
ON mistake_case(child_id, status, updated_at DESC);
CREATE INDEX idx_mistake_attempt_case_stage
ON mistake_attempt(mistake_id, stage, created_at);
CREATE INDEX idx_mistake_capture_candidate_artifact
ON mistake_capture_candidate(artifact_id, question_index);
CREATE UNIQUE INDEX idx_agent_one_active_objective
ON agent_objective(child_id) WHERE status = 'active';
CREATE INDEX idx_agent_objective_session_time
ON agent_objective(session_id, updated_at DESC);
CREATE INDEX idx_agent_step_plan_status
ON agent_step(plan_id, status, step_index);
CREATE INDEX idx_agent_commitment_child_due
ON agent_commitment(child_id, status, due_at);
CREATE INDEX idx_observation_qualification_child_time
ON observation_qualification(child_id, updated_at DESC);
CREATE INDEX idx_behavior_event_child_time ON observable_event(child_id, occurred_at);
CREATE INDEX idx_behavior_state_child_time ON probabilistic_state(child_id, occurred_at);
CREATE UNIQUE INDEX idx_intervention_feedback_action ON intervention(feedback_action_id) WHERE feedback_action_id IS NOT NULL;
CREATE UNIQUE INDEX idx_intervention_source_event ON intervention(source_event_id) WHERE source_event_id IS NOT NULL;
CREATE UNIQUE INDEX idx_intervention_effect_action ON intervention(effect_action_id) WHERE effect_action_id IS NOT NULL;
CREATE UNIQUE INDEX idx_habit_one_open
ON habit_experiment(child_id) WHERE status IN ('proposed','active');
CREATE INDEX idx_habit_formation_child_stage
ON habit_formation_state(child_id, stage, updated_at DESC);
CREATE TRIGGER trg_habit_formation_event_no_update
BEFORE UPDATE ON habit_formation_event BEGIN
  SELECT RAISE(ABORT, 'habit_formation_event is immutable');
END;
CREATE INDEX idx_child_state_session_time
ON child_state_event(session_id, observed_at);
CREATE INDEX idx_learning_evidence_child_node
ON learning_evidence(child_id, knowledge_node_id, occurred_at DESC);
CREATE INDEX idx_mastery_review
ON mastery_state(child_id, next_review_at);
CREATE INDEX idx_daily_report_child_date
ON daily_report(child_id, report_date DESC);
CREATE INDEX idx_model_call_child_created
ON model_call(child_id, created_at DESC);
CREATE INDEX idx_tutor_turn_session_created
ON tutor_turn(session_id, created_at);
CREATE INDEX idx_agent_decision_child_time
        ON agent_decision_record(child_id, created_at DESC);
CREATE TRIGGER trg_agent_decision_record_no_update
        BEFORE UPDATE ON agent_decision_record BEGIN
          SELECT RAISE(ABORT, 'agent_decision_record is immutable');
        END;
CREATE INDEX idx_agent_outcome_attribution_child_time
        ON agent_outcome_attribution(child_id, created_at DESC);
CREATE TRIGGER trg_agent_outcome_attribution_no_update
        BEFORE UPDATE ON agent_outcome_attribution BEGIN
          SELECT RAISE(ABORT, 'agent_outcome_attribution is immutable');
        END;
CREATE INDEX idx_review_task_due
ON review_task(child_id, status, due_at);
CREATE UNIQUE INDEX review_task_other_interval
        ON review_task(source_evidence_id,interval_label) WHERE interval_label NOT IN ('school_open_near','school_open_far','school_open_7d','writing_stage_7d');
CREATE UNIQUE INDEX review_task_school_current
        ON review_task(source_evidence_id,interval_label)
        WHERE interval_label IN ('school_open_near','school_open_far','school_open_7d','writing_stage_7d') AND status!='cancelled' ;
CREATE UNIQUE INDEX review_task_single_successor
        ON review_task(predecessor_review_task_id) WHERE predecessor_review_task_id IS NOT NULL;
CREATE UNIQUE INDEX knowledge_source_current_hash
                 ON knowledge_source(child_id,content_hash) WHERE current_version=1;
CREATE INDEX idx_mistake_source_revision_child ON mistake_source_revision(child_id,mistake_id,version);
CREATE TRIGGER mistake_source_revision_no_update
        BEFORE UPDATE ON mistake_source_revision BEGIN SELECT RAISE(ABORT,'question revisions are append-only'); END;
CREATE TRIGGER mistake_source_revision_owner
        BEFORE INSERT ON mistake_source_revision BEGIN
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM mistake_case m JOIN daily_session s ON s.child_id=m.child_id
            WHERE m.mistake_id=NEW.mistake_id AND m.child_id=NEW.child_id AND s.session_id=NEW.session_id)
            THEN RAISE(ABORT,'question revision owner mismatch') END; END;
CREATE UNIQUE INDEX idx_bounded_experiment_one_open_variable
        ON bounded_experiment_protocol(child_id, domain, primary_variable)
        WHERE status IN ('draft','replay_passed','shadow','active');
CREATE INDEX idx_bounded_experiment_child_status
        ON bounded_experiment_protocol(child_id, status, updated_at DESC);
CREATE TRIGGER trg_bounded_experiment_event_no_update
        BEFORE UPDATE ON bounded_experiment_event BEGIN
          SELECT RAISE(ABORT, 'bounded_experiment_event is immutable');
        END;
CREATE INDEX idx_paper_scoring_request_child
          ON paper_scoring_request(child_id, created_at DESC);
CREATE TRIGGER paper_scoring_request_no_update
        BEFORE UPDATE ON paper_scoring_request
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring request'); END;
CREATE INDEX idx_paper_answer_authority_lookup
          ON paper_answer_authority(child_id, question_sha256, created_at DESC);
CREATE TRIGGER paper_answer_authority_no_update
        BEFORE UPDATE ON paper_answer_authority
        BEGIN SELECT RAISE(ABORT, 'immutable paper answer authority'); END;
CREATE INDEX idx_paper_scoring_decision_child
          ON paper_scoring_decision(child_id, created_at DESC);
CREATE TRIGGER paper_scoring_decision_no_update
        BEFORE UPDATE ON paper_scoring_decision
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring decision'); END;
CREATE INDEX idx_paper_subjective_binding_child
          ON paper_subjective_score_binding(child_id, created_at DESC);
CREATE TRIGGER paper_subjective_score_binding_no_update
        BEFORE UPDATE ON paper_subjective_score_binding
        BEGIN SELECT RAISE(ABORT, 'immutable paper subjective score binding'); END;
CREATE INDEX idx_paper_scoring_status_decision
          ON paper_scoring_decision_status_event(decision_id, created_at);
CREATE TRIGGER paper_scoring_decision_status_no_update
        BEFORE UPDATE ON paper_scoring_decision_status_event
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring status event'); END;
CREATE INDEX idx_paper_scoring_revision_request
          ON paper_scoring_decision_revision(request_id,created_at);
CREATE TRIGGER paper_scoring_revision_no_update
        BEFORE UPDATE ON paper_scoring_decision_revision
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring revision'); END;
CREATE TRIGGER paper_scoring_revision_status_no_update
        BEFORE UPDATE ON paper_scoring_revision_status_event
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring revision status'); END;
CREATE TRIGGER paper_scoring_revision_mistake_no_update
        BEFORE UPDATE ON paper_scoring_revision_mistake_link
        BEGIN SELECT RAISE(ABORT,'immutable paper revision mistake link'); END;
CREATE VIEW paper_scoring_effective_decision AS
        SELECT root.request_id,root.child_id,root.decision_id AS root_decision_id,
               COALESCE(revision.revision_id,root.decision_id) AS decision_id,
               COALESCE(revision.authority_type,root.authority_type) AS authority_type,
               COALESCE(revision.authority_ref,root.authority_ref) AS authority_ref,
               COALESCE(revision.authority_snapshot_json,root.authority_snapshot_json)
                 AS authority_snapshot_json,
               COALESCE(revision.correctness,root.correctness) AS correctness,
               COALESCE(revision.confidence,root.confidence) AS confidence,
               COALESCE(revision.created_at,root.created_at) AS created_at,
               CASE WHEN revision.revision_id IS NOT NULL THEN
                 COALESCE((SELECT event.status FROM paper_scoring_revision_status_event AS event
                           WHERE event.revision_id=revision.revision_id
                           ORDER BY event.rowid DESC LIMIT 1),'qualified')
               ELSE COALESCE((SELECT event.status FROM paper_scoring_decision_status_event AS event
                              WHERE event.decision_id=root.decision_id
                              ORDER BY event.rowid DESC LIMIT 1),'qualified') END AS effective_status
        FROM paper_scoring_decision AS root
        LEFT JOIN paper_scoring_decision_revision AS revision
          ON revision.revision_id=(
            SELECT latest.revision_id FROM paper_scoring_decision_revision AS latest
            WHERE latest.decision_id=root.decision_id
            ORDER BY latest.revision_sequence DESC LIMIT 1
          );
CREATE VIEW paper_scoring_visible_decision AS
        SELECT effective.* FROM paper_scoring_effective_decision AS effective
        WHERE effective.decision_id=effective.root_decision_id
           OR NOT EXISTS(
             SELECT 1 FROM paper_scoring_request AS request
             JOIN mistake_dialogue AS dialogue
               ON dialogue.candidate_id=request.candidate_id
              AND dialogue.child_id=request.child_id
             WHERE request.request_id=effective.request_id
           )
           OR EXISTS(
             SELECT 1 FROM paper_scoring_dialogue_revision_reconciliation AS receipt
             WHERE receipt.revision_id=effective.decision_id
               AND receipt.child_id=effective.child_id
           );
CREATE INDEX idx_paper_correct_obligation_child
          ON paper_correct_verification_obligation(child_id, due_at);
CREATE TRIGGER paper_correct_obligation_no_update
        BEFORE UPDATE ON paper_correct_verification_obligation
        BEGIN SELECT RAISE(ABORT, 'immutable paper correct verification obligation'); END;
CREATE INDEX idx_paper_correct_target_obligation
          ON paper_correct_verification_target(obligation_id, created_at);
CREATE TRIGGER paper_correct_target_no_update
        BEFORE UPDATE ON paper_correct_verification_target
        BEGIN SELECT RAISE(ABORT, 'immutable paper correct verification target'); END;
CREATE INDEX idx_paper_correct_event_obligation
          ON paper_correct_verification_event(obligation_id, created_at);
CREATE TRIGGER paper_correct_event_no_update
        BEFORE UPDATE ON paper_correct_verification_event
        BEGIN SELECT RAISE(ABORT, 'immutable paper correct verification event'); END;
CREATE TRIGGER paper_correct_decision_delete_withdrawal
        BEFORE DELETE ON paper_scoring_decision
        BEGIN
          INSERT OR IGNORE INTO learning_evidence_withdrawal(
            withdrawal_id,child_id,evidence_id,reason,replacement_evidence_id,
            session_id,client_action_id,request_hash,created_at
          )
          SELECT lower(hex(randomblob(16))),o.child_id,o.source_evidence_id,
                 'answer_withdrawn',NULL,o.session_id,
                 'paper-decision-delete:' || OLD.decision_id,
                 'paper-decision-delete:' || OLD.decision_id,datetime('now')
            FROM paper_correct_verification_obligation AS o
           WHERE o.decision_id=OLD.decision_id;
          UPDATE review_task SET status='cancelled'
           WHERE status='pending' AND source_evidence_id IN (
             SELECT source_evidence_id FROM paper_correct_verification_obligation
              WHERE decision_id=OLD.decision_id
           );
          UPDATE plan_item SET status='cancelled'
           WHERE source_type='due_review' AND status IN ('pending','rescheduled')
             AND started_at IS NULL AND source_ref IN (
               SELECT t.review_task_id
                 FROM paper_correct_verification_target AS t
                 JOIN paper_correct_verification_obligation AS o USING(obligation_id)
                WHERE o.decision_id=OLD.decision_id
             );
        END;
CREATE TRIGGER paper_correct_request_delete_withdrawal
        BEFORE DELETE ON paper_scoring_request
        BEGIN
          INSERT OR IGNORE INTO learning_evidence_withdrawal(
            withdrawal_id,child_id,evidence_id,reason,replacement_evidence_id,
            session_id,client_action_id,request_hash,created_at
          )
          SELECT lower(hex(randomblob(16))),o.child_id,o.source_evidence_id,
                 'answer_withdrawn',NULL,o.session_id,
                 'paper-request-delete:' || OLD.request_id,
                 'paper-request-delete:' || OLD.request_id,datetime('now')
            FROM paper_correct_verification_obligation AS o
           WHERE o.request_id=OLD.request_id;
          UPDATE review_task SET status='cancelled'
           WHERE status='pending' AND source_evidence_id IN (
             SELECT source_evidence_id FROM paper_correct_verification_obligation
              WHERE request_id=OLD.request_id
           );
          UPDATE plan_item SET status='cancelled'
           WHERE source_type='due_review' AND status IN ('pending','rescheduled')
             AND started_at IS NULL AND source_ref IN (
               SELECT t.review_task_id
                 FROM paper_correct_verification_target AS t
                 JOIN paper_correct_verification_obligation AS o USING(obligation_id)
                WHERE o.request_id=OLD.request_id
             );
        END;
CREATE INDEX idx_paper_correct_revision_child
          ON paper_correct_revision_obligation(child_id,due_at);
CREATE TRIGGER paper_correct_revision_obligation_no_update
        BEFORE UPDATE ON paper_correct_revision_obligation
        BEGIN SELECT RAISE(ABORT,'immutable paper revision obligation'); END;
CREATE TRIGGER paper_correct_revision_target_no_update
        BEFORE UPDATE ON paper_correct_revision_target
        BEGIN SELECT RAISE(ABORT,'immutable paper revision target'); END;
CREATE TRIGGER paper_correct_revision_event_no_update
        BEFORE UPDATE ON paper_correct_revision_event
        BEGIN SELECT RAISE(ABORT,'immutable paper revision event'); END;
CREATE INDEX idx_paper_supply_obligation_child
          ON paper_scoring_supply_obligation(child_id, created_at DESC);
CREATE TRIGGER paper_scoring_supply_obligation_no_update
        BEFORE UPDATE ON paper_scoring_supply_obligation
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring supply obligation'); END;
CREATE INDEX idx_paper_supply_event_obligation
          ON paper_scoring_supply_event(obligation_id, created_at);
CREATE TRIGGER paper_scoring_supply_event_no_update
        BEFORE UPDATE ON paper_scoring_supply_event
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring supply event'); END;
CREATE INDEX idx_paper_scoring_supply_work_status
          ON paper_scoring_supply_work(status, updated_at);
CREATE UNIQUE INDEX idx_transition_client_action
                ON session_transition(client_action_id) WHERE client_action_id IS NOT NULL
                ;
CREATE UNIQUE INDEX idx_deletion_job_client_action ON deletion_job(client_action_id) WHERE client_action_id IS NOT NULL;
CREATE INDEX idx_voice_calibration_save_action_child
                ON voice_calibration_save_action(child_id, saved_at);
CREATE INDEX idx_voice_transcription_action_session
                ON voice_transcription_action(session_id, updated_at);
CREATE INDEX idx_kg_resource_hash ON kg_resource(transcript_sha256);
CREATE INDEX idx_kg_document_page_kind ON kg_document_page(document_kind, page_number);
CREATE INDEX idx_kg_segment_hash ON kg_segment(text_sha256);
CREATE TRIGGER kg_segment_occurrence_pending_insert AFTER INSERT ON kg_segment
BEGIN
  INSERT OR IGNORE INTO kg_segment_occurrence_pending(segment_id) VALUES(NEW.segment_id);
END;
CREATE TRIGGER kg_segment_occurrence_pending_update
AFTER UPDATE OF segment_id,resource_id,ordinal,start_char,end_char ON kg_segment
BEGIN
  INSERT OR IGNORE INTO kg_segment_occurrence_pending(segment_id) VALUES(NEW.segment_id);
END;
CREATE INDEX idx_kg_edge_target_relation ON kg_edge(target_node_id, relation_type);
CREATE INDEX idx_kg_resource_node_node ON kg_resource_node(node_id, relation_type);
CREATE INDEX idx_kg_mapping_audit_item_pack ON kg_mapping_audit_item(audit_pack_id,grade_scope,subject_scope,track);
CREATE INDEX idx_kg_language_focus_candidate_action_candidate
ON kg_language_focus_candidate_action(candidate_id, created_at DESC);
CREATE INDEX idx_kg_parent_textbook_capture_lease_active ON kg_parent_textbook_capture_lease(system_id_hash,status,expires_at);
CREATE UNIQUE INDEX uq_kg_parent_textbook_capture_lease_active_device ON kg_parent_textbook_capture_lease(system_id_hash) WHERE status='active';
CREATE TRIGGER kg_parent_textbook_file_source_immutable
BEFORE UPDATE ON kg_parent_textbook_file_source BEGIN SELECT RAISE(ABORT,'file source is immutable'); END;
CREATE INDEX idx_kg_parent_textbook_ocr_artifact_child ON kg_parent_textbook_ocr_artifact(child_id,status,updated_at DESC);
CREATE INDEX idx_kg_parent_textbook_ocr_review_event_artifact ON kg_parent_textbook_ocr_review_event(artifact_id,created_at DESC);
CREATE INDEX idx_kg_review_plan_child_status
ON kg_review_plan(child_id, status, generated_at DESC);
CREATE INDEX idx_kg_mapping_review_target ON kg_mapping_review(node_id,decision);
CREATE INDEX idx_kg_mapping_review_event_mapping
ON kg_mapping_review_event(resource_id, segment_id, node_id, relation_type, created_at DESC);
CREATE INDEX idx_kg_atomic_candidate_signature
ON kg_atomic_candidate(canonical_signature,status);
CREATE INDEX idx_kg_atomic_candidate_scope
ON kg_atomic_candidate(status,source_kind);
CREATE INDEX idx_kg_atomic_relation_target
ON kg_atomic_relation_candidate(target_key,relation_type,review_status);
CREATE INDEX idx_kg_atomic_extraction_batch_status
ON kg_atomic_extraction_batch(job_id,status,batch_index);
CREATE INDEX idx_kg_atomic_resource_disposition_job
ON kg_atomic_resource_disposition(job_id,disposition,reason_code,resource_id);
CREATE INDEX idx_kg_atomic_resource_gap_status
ON kg_atomic_resource_gap(job_id,status,reason_code,resource_id);
CREATE INDEX idx_kg_atomic_definition_review
ON kg_atomic_definition_draft(review_status,grade_band,subject);
CREATE INDEX idx_kg_atomic_draft_batch_job
ON kg_atomic_draft_batch(job_id,batch_id,draft_id);
CREATE INDEX idx_kg_atomic_draft_evidence_resource
ON kg_atomic_draft_evidence(resource_id,segment_id);
CREATE INDEX idx_kg_atomic_draft_standard_ref
ON kg_atomic_draft_standard_alignment(source_ref,draft_id);
CREATE INDEX idx_kg_atomic_textbook_alignment_review
ON kg_atomic_textbook_alignment(source_job_id,review_status,lexical_score DESC);
CREATE INDEX idx_kg_atomic_textbook_alignment_page
ON kg_atomic_textbook_alignment(textbook_resource_id,textbook_segment_id);
CREATE INDEX idx_kg_atomic_merge_review
ON kg_atomic_merge_candidate(review_status,lexical_similarity DESC);
CREATE TRIGGER runtime_data_scope_no_replace
      BEFORE INSERT ON runtime_data_scope WHEN EXISTS(SELECT 1 FROM runtime_data_scope)
      BEGIN SELECT RAISE(ABORT,'runtime scope is immutable'); END;
CREATE TRIGGER runtime_data_scope_no_update
      BEFORE UPDATE ON runtime_data_scope BEGIN SELECT RAISE(ABORT,'runtime scope is immutable'); END;
CREATE TRIGGER runtime_data_scope_no_delete
      BEFORE DELETE ON runtime_data_scope BEGIN SELECT RAISE(ABORT,'runtime scope is immutable'); END;
CREATE INDEX idx_ocr_owned_child ON ocr_job(child_id,artifact_id);
CREATE INDEX idx_ocr_review_child ON ocr_review_item(child_id,status,created_at);
CREATE UNIQUE INDEX idx_ocr_review_event_action
        ON ocr_review_event(review_id,client_action_id) WHERE review_id IS NOT NULL;
CREATE INDEX idx_kg_companion_context_created
        ON kg_companion_graph_context_event(created_at DESC);
COMMIT;
