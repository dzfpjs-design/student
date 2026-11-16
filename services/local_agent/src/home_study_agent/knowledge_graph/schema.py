from __future__ import annotations

import sqlite3


KG_SCHEMA_VERSION = 36
_OCCURRENCE_INCREMENTAL_VERSION = 31

# Test-only fault point used to prove the SQLite savepoint migration can restart
# after any DDL/copy interruption.  It is intentionally unset in production.
_CANDIDATE_MIGRATION_FAULT_AFTER: str | None = None
_PARENT_SOURCE_MIGRATION_FAULT_AFTER: str | None = None

# This schema is deliberately namespaced.  It can be installed in the application's
# existing SQLite database without changing the shared bootstrap migration contract.
SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_schema_migration (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kg_parent_textbook_form_draft (
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  scope TEXT NOT NULL CHECK(scope IN ('capture','language')),
  revision INTEGER NOT NULL CHECK(revision >= 1),
  encrypted_payload BLOB, payload_sha256 TEXT NOT NULL,
  last_action_id TEXT NOT NULL, last_expected_revision INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(child_id,scope)
);
CREATE TABLE IF NOT EXISTS kg_corpus (
  corpus_id TEXT PRIMARY KEY, title TEXT NOT NULL, source_kind TEXT NOT NULL,
  transcript_root TEXT NOT NULL, video_root TEXT, metadata_db_path TEXT,
  source_ref TEXT NOT NULL, license_boundary TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','restricted','rejected')),
  version_label TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  pdf_root TEXT, image_root TEXT,
  UNIQUE(source_kind, transcript_root)
);
CREATE TABLE IF NOT EXISTS kg_ingest_job (
  job_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES kg_corpus(corpus_id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','failed','interrupted')),
  options_json TEXT NOT NULL, checkpoint_cursor TEXT, total_count INTEGER NOT NULL DEFAULT 0,
  processed_count INTEGER NOT NULL DEFAULT 0, skipped_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
  run_token TEXT, run_lease_expires_at TEXT
);
CREATE TABLE IF NOT EXISTS kg_ingest_failure (
  failure_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES kg_ingest_job(job_id) ON DELETE CASCADE,
  external_id TEXT NOT NULL, relative_path TEXT NOT NULL, error_code TEXT NOT NULL, detail TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL CHECK(status IN ('open','resolved','ignored')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(job_id, external_id)
);
CREATE TABLE IF NOT EXISTS kg_resource (
  resource_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES kg_corpus(corpus_id) ON DELETE CASCADE,
  external_id TEXT NOT NULL, title TEXT NOT NULL, grade_scope TEXT, subject_scope TEXT, semester_scope TEXT,
  transcript_relative_path TEXT NOT NULL, video_relative_path TEXT, transcript_sha256 TEXT NOT NULL,
  video_sha256 TEXT, video_fingerprint TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
  source_ref TEXT NOT NULL, license_boundary TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','restricted','rejected')),
  version_label TEXT NOT NULL, ingested_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(corpus_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_resource_hash ON kg_resource(transcript_sha256);
CREATE TABLE IF NOT EXISTS kg_document_page (
  page_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL UNIQUE REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  document_kind TEXT NOT NULL CHECK(document_kind IN ('textbook','school_official_practice','purchased_practice_book','other_user_document')),
  page_number INTEGER NOT NULL CHECK(page_number >= 1), image_relative_path TEXT,
  image_sha256 TEXT, image_fingerprint TEXT, ocr_engine TEXT NOT NULL,
  ocr_confidence REAL CHECK(ocr_confidence IS NULL OR (ocr_confidence BETWEEN 0 AND 1)),
  source_ref TEXT NOT NULL, license_boundary TEXT NOT NULL, version_label TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kg_document_page_kind ON kg_document_page(document_kind, page_number);
CREATE TABLE IF NOT EXISTS kg_segment (
  segment_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, start_char INTEGER NOT NULL, end_char INTEGER NOT NULL, text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL, source_ref TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','restricted','rejected')),
  created_at TEXT NOT NULL, UNIQUE(resource_id, ordinal), UNIQUE(resource_id, text_sha256)
);
CREATE INDEX IF NOT EXISTS idx_kg_segment_hash ON kg_segment(text_sha256);
CREATE TABLE IF NOT EXISTS kg_segment_occurrence (
  resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  segment_id TEXT NOT NULL REFERENCES kg_segment(segment_id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
  PRIMARY KEY(resource_id,segment_id)
);
CREATE TABLE IF NOT EXISTS kg_segment_occurrence_pending (
  segment_id TEXT PRIMARY KEY REFERENCES kg_segment(segment_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE TRIGGER IF NOT EXISTS kg_segment_occurrence_pending_insert AFTER INSERT ON kg_segment
BEGIN
  INSERT OR IGNORE INTO kg_segment_occurrence_pending(segment_id) VALUES(NEW.segment_id);
END;
CREATE TRIGGER IF NOT EXISTS kg_segment_occurrence_pending_update
AFTER UPDATE OF segment_id,resource_id,ordinal,start_char,end_char ON kg_segment
BEGIN
  INSERT OR IGNORE INTO kg_segment_occurrence_pending(segment_id) VALUES(NEW.segment_id);
END;
CREATE VIRTUAL TABLE IF NOT EXISTS kg_segment_fts USING fts5(
  segment_id UNINDEXED, text, title, subject_scope, grade_scope, tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS kg_segment_embedding (
  segment_id TEXT PRIMARY KEY REFERENCES kg_segment(segment_id) ON DELETE CASCADE,
  algorithm TEXT NOT NULL, dimensions INTEGER NOT NULL, vector BLOB NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kg_node (
  node_id TEXT PRIMARY KEY, node_type TEXT NOT NULL CHECK(node_type IN ('standard_capability','stage','grade','semester','subject','unit','knowledge_point','misconception','explanation','exercise','mistake','diagnostic_evidence','mastery_state')),
  canonical_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, description TEXT NOT NULL,
  scope_json TEXT NOT NULL DEFAULT '{}', source_ref TEXT NOT NULL, source_time TEXT, version_label TEXT NOT NULL,
  license_boundary TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','restricted','rejected')),
  status TEXT NOT NULL CHECK(status IN ('active','deprecated','conflicted')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kg_edge (
  edge_id TEXT PRIMARY KEY, source_node_id TEXT NOT NULL REFERENCES kg_node(node_id) ON DELETE CASCADE,
  target_node_id TEXT NOT NULL REFERENCES kg_node(node_id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL CHECK(relation_type IN ('contains','prerequisite_of','related_to','misconception_of','aligned_to','explained_by','assessed_by','evidenced_by','maps_to','generated_from')),
  rationale TEXT NOT NULL, source_ref TEXT NOT NULL, version_label TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','approved','restricted','rejected')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(source_node_id,target_node_id,relation_type,version_label)
);
CREATE INDEX IF NOT EXISTS idx_kg_edge_target_relation ON kg_edge(target_node_id, relation_type);
CREATE TABLE IF NOT EXISTS kg_resource_node (
  resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  segment_id TEXT REFERENCES kg_segment(segment_id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES kg_node(node_id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL CHECK(relation_type IN ('covers','explains','exemplifies','assesses')),
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), review_status TEXT NOT NULL,
  source_ref TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(resource_id, segment_id, node_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_kg_resource_node_node ON kg_resource_node(node_id, relation_type);
CREATE TABLE IF NOT EXISTS kg_candidate_mapping_job (
  mapping_job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','failed','interrupted')),
  source_ref TEXT NOT NULL, checkpoint_resource_id TEXT, total_resources INTEGER NOT NULL DEFAULT 0,
  processed_resources INTEGER NOT NULL DEFAULT 0, mapped_rows INTEGER NOT NULL DEFAULT 0,
  failed_resources INTEGER NOT NULL DEFAULT 0, options_json TEXT NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, completed_at TEXT, run_token TEXT, run_lease_expires_at TEXT
);
CREATE TABLE IF NOT EXISTS kg_candidate_mapping_failure (
  failure_id TEXT PRIMARY KEY, mapping_job_id TEXT NOT NULL REFERENCES kg_candidate_mapping_job(mapping_job_id) ON DELETE CASCADE,
  resource_id TEXT NOT NULL, error_code TEXT NOT NULL, detail TEXT, retry_count INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK(status IN ('open','resolved','ignored')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(mapping_job_id,resource_id)
);
CREATE TABLE IF NOT EXISTS kg_mapping_audit_pack (
  audit_pack_id TEXT PRIMARY KEY, snapshot_sha256 TEXT NOT NULL, criteria_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open','completed','superseded')), item_count INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kg_mapping_audit_item (
  audit_item_id TEXT PRIMARY KEY, audit_pack_id TEXT NOT NULL REFERENCES kg_mapping_audit_pack(audit_pack_id) ON DELETE CASCADE,
  resource_id TEXT NOT NULL, segment_id TEXT NOT NULL, node_id TEXT NOT NULL, relation_type TEXT NOT NULL,
  grade_scope TEXT, subject_scope TEXT, track TEXT, lexical_evidence TEXT NOT NULL,
  scope_match TEXT NOT NULL, mapping_source_ref TEXT NOT NULL, precision_proxy REAL NOT NULL,
  human_verdict TEXT CHECK(human_verdict IN ('accept','reject','needs_split')), error_category TEXT,
  reviewer TEXT, note TEXT, reviewed_at TEXT, created_at TEXT NOT NULL,
  UNIQUE(audit_pack_id,resource_id,segment_id,node_id,relation_type)
);
CREATE INDEX IF NOT EXISTS idx_kg_mapping_audit_item_pack ON kg_mapping_audit_item(audit_pack_id,grade_scope,subject_scope,track);
CREATE TABLE IF NOT EXISTS kg_language_lexeme (
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
CREATE TABLE IF NOT EXISTS kg_language_lexeme_page (
  lexeme_id TEXT NOT NULL REFERENCES kg_language_lexeme(lexeme_id) ON DELETE CASCADE,
  resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE, page_number INTEGER NOT NULL,
  evidence_text TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lexeme_id,resource_id)
);
CREATE TABLE IF NOT EXISTS kg_language_focus_candidate (
  candidate_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  language TEXT NOT NULL CHECK(language IN ('english','chinese')), term TEXT NOT NULL, normalized TEXT NOT NULL,
  focus_kind TEXT NOT NULL CHECK(focus_kind IN ('word','character','phrase')), proposed_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK(status IN ('pending','projecting','confirmed','deleted','stale','conflicted','withdrawn')), source_start INTEGER, source_end INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(resource_id,language,normalized,focus_kind)
);
CREATE TABLE IF NOT EXISTS kg_language_focus_candidate_action (
  client_action_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES kg_language_focus_candidate(candidate_id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK(action IN ('save','confirm','withdraw')),
  payload_sha256 TEXT NOT NULL,
  result_status TEXT NOT NULL,
  projection_claim TEXT, projection_lease_expires_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kg_language_focus_candidate_action_candidate
ON kg_language_focus_candidate_action(candidate_id, created_at DESC);
CREATE TABLE IF NOT EXISTS kg_parent_textbook_capture_lease (
  lease_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id), system_id_hash TEXT NOT NULL,
  capabilities_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','released')),
  opened_at TEXT NOT NULL,last_renewed_at TEXT NOT NULL,expires_at TEXT NOT NULL,released_at TEXT,release_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_kg_parent_textbook_capture_lease_active ON kg_parent_textbook_capture_lease(system_id_hash,status,expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_kg_parent_textbook_capture_lease_active_device ON kg_parent_textbook_capture_lease(system_id_hash) WHERE status='active';
CREATE TABLE IF NOT EXISTS kg_parent_textbook_file_source (
  file_source_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  image_sha256 TEXT NOT NULL,page_context_sha256 TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE,
  request_sha256 TEXT NOT NULL,created_at TEXT NOT NULL,
  UNIQUE(file_source_id,child_id,image_sha256,page_context_sha256,client_action_id)
);
CREATE TRIGGER IF NOT EXISTS kg_parent_textbook_file_source_immutable
BEFORE UPDATE ON kg_parent_textbook_file_source BEGIN SELECT RAISE(ABORT,'file source is immutable'); END;
CREATE TABLE IF NOT EXISTS kg_parent_textbook_ocr_artifact (
  artifact_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id),lease_id TEXT REFERENCES kg_parent_textbook_capture_lease(lease_id),
  source_kind TEXT NOT NULL DEFAULT 'camera' CHECK(source_kind IN ('camera','file')),file_source_id TEXT,
  page_context_json TEXT NOT NULL,extracted_text TEXT NOT NULL,confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),details_json TEXT NOT NULL,
  image_sha256 TEXT NOT NULL DEFAULT '',page_context_sha256 TEXT NOT NULL DEFAULT '',
  raw_image_path TEXT,raw_image_media_type TEXT,raw_image_byte_length INTEGER NOT NULL DEFAULT 0,
  raw_image_encryption_version TEXT,
  status TEXT NOT NULL CHECK(status IN ('processing','needs_review','ready','importing','imported','skipped','failed')),
  import_action_id TEXT,import_payload_sha256 TEXT,import_request_json TEXT,import_claimed_at TEXT,
  created_at TEXT NOT NULL,updated_at TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE,
  CHECK((source_kind='camera' AND lease_id IS NOT NULL AND file_source_id IS NULL)
     OR (source_kind='file' AND lease_id IS NULL AND file_source_id IS NOT NULL)),
  FOREIGN KEY(file_source_id,child_id,image_sha256,page_context_sha256,client_action_id)
    REFERENCES kg_parent_textbook_file_source(file_source_id,child_id,image_sha256,page_context_sha256,client_action_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_parent_textbook_ocr_artifact_child ON kg_parent_textbook_ocr_artifact(child_id,status,updated_at DESC);
CREATE TABLE IF NOT EXISTS kg_parent_textbook_ocr_review_event (
  event_id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL REFERENCES kg_parent_textbook_ocr_artifact(artifact_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK(event_type IN ('created','reviewed','review_undone','skipped','restored','retry_requested','failed','recovery_required','import_recovered','imported','raw_source_missing')),
  payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kg_parent_textbook_ocr_review_event_artifact ON kg_parent_textbook_ocr_review_event(artifact_id,created_at DESC);
CREATE TABLE IF NOT EXISTS kg_mastery_evidence (
  evidence_id TEXT PRIMARY KEY, child_id TEXT NOT NULL, node_id TEXT NOT NULL REFERENCES kg_node(node_id),
  origin_type TEXT NOT NULL CHECK(origin_type IN ('diagnostic','exercise','mistake','review','teacher_observation')),
  origin_id TEXT, correctness REAL NOT NULL CHECK(correctness BETWEEN 0 AND 1), independent INTEGER NOT NULL CHECK(independent IN (0,1)),
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), note TEXT NOT NULL, occurred_at TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS kg_mastery_state (
  child_id TEXT NOT NULL, node_id TEXT NOT NULL REFERENCES kg_node(node_id), mastery_probability REAL NOT NULL CHECK(mastery_probability BETWEEN 0 AND 1),
  evidence_count INTEGER NOT NULL, weakness_signal REAL NOT NULL CHECK(weakness_signal BETWEEN 0 AND 1), algorithm_version TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY(child_id,node_id)
);
CREATE TABLE IF NOT EXISTS kg_review_plan (
  review_plan_id TEXT PRIMARY KEY, child_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','superseded','completed')),
  algorithm_version TEXT NOT NULL, item_count INTEGER NOT NULL,
  source_evidence_count INTEGER NOT NULL, plan_json TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE, generated_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kg_review_plan_child_status
ON kg_review_plan(child_id, status, generated_at DESC);
CREATE TABLE IF NOT EXISTS kg_mapping_review (
  review_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
  segment_id TEXT REFERENCES kg_segment(segment_id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES kg_node(node_id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','needs_split')),
  reviewer TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,
  UNIQUE(resource_id,segment_id,node_id,relation_type,reviewer)
);
CREATE INDEX IF NOT EXISTS idx_kg_mapping_review_target ON kg_mapping_review(node_id,decision);
CREATE TABLE IF NOT EXISTS kg_mapping_review_event (
  event_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL,
  segment_id TEXT, node_id TEXT NOT NULL, relation_type TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('decision','undo')), decision TEXT,
  reviewer TEXT NOT NULL, note TEXT, previous_decision TEXT,
  client_action_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kg_mapping_review_event_mapping
ON kg_mapping_review_event(resource_id, segment_id, node_id, relation_type, created_at DESC);
CREATE TABLE IF NOT EXISTS kg_atomic_candidate (
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
CREATE INDEX IF NOT EXISTS idx_kg_atomic_candidate_signature
ON kg_atomic_candidate(canonical_signature,status);
CREATE INDEX IF NOT EXISTS idx_kg_atomic_candidate_scope
ON kg_atomic_candidate(status,source_kind);
CREATE TABLE IF NOT EXISTS kg_atomic_profile (
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
CREATE TABLE IF NOT EXISTS kg_atomic_relation_candidate (
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
CREATE INDEX IF NOT EXISTS idx_kg_atomic_relation_target
ON kg_atomic_relation_candidate(target_key,relation_type,review_status);
CREATE TABLE IF NOT EXISTS kg_atomic_extraction_job (
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
CREATE TABLE IF NOT EXISTS kg_atomic_extraction_batch (
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
CREATE INDEX IF NOT EXISTS idx_kg_atomic_extraction_batch_status
ON kg_atomic_extraction_batch(job_id,status,batch_index);
CREATE TABLE IF NOT EXISTS kg_atomic_resource_disposition (
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
CREATE INDEX IF NOT EXISTS idx_kg_atomic_resource_disposition_job
ON kg_atomic_resource_disposition(job_id,disposition,reason_code,resource_id);
CREATE TABLE IF NOT EXISTS kg_atomic_resource_gap (
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
CREATE INDEX IF NOT EXISTS idx_kg_atomic_resource_gap_status
ON kg_atomic_resource_gap(job_id,status,reason_code,resource_id);
CREATE TABLE IF NOT EXISTS kg_atomic_definition_draft (
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
CREATE INDEX IF NOT EXISTS idx_kg_atomic_definition_review
ON kg_atomic_definition_draft(review_status,grade_band,subject);
CREATE TABLE IF NOT EXISTS kg_atomic_draft_batch (
  draft_id TEXT NOT NULL REFERENCES kg_atomic_definition_draft(draft_id) ON DELETE CASCADE,
  batch_id TEXT NOT NULL REFERENCES kg_atomic_extraction_batch(batch_id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES kg_atomic_extraction_job(job_id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  PRIMARY KEY(draft_id,batch_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_atomic_draft_batch_job
ON kg_atomic_draft_batch(job_id,batch_id,draft_id);
CREATE TABLE IF NOT EXISTS kg_atomic_draft_evidence (
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
CREATE INDEX IF NOT EXISTS idx_kg_atomic_draft_evidence_resource
ON kg_atomic_draft_evidence(resource_id,segment_id);
CREATE TABLE IF NOT EXISTS kg_atomic_draft_standard_alignment (
  alignment_id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES kg_atomic_definition_draft(draft_id) ON DELETE CASCADE,
  source_ref TEXT NOT NULL,
  exact_quote TEXT NOT NULL,
  quote_sha256 TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
  created_at TEXT NOT NULL,
  UNIQUE(draft_id,source_ref,quote_sha256)
);
CREATE INDEX IF NOT EXISTS idx_kg_atomic_draft_standard_ref
ON kg_atomic_draft_standard_alignment(source_ref,draft_id);
CREATE TABLE IF NOT EXISTS kg_atomic_textbook_alignment (
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
CREATE INDEX IF NOT EXISTS idx_kg_atomic_textbook_alignment_review
ON kg_atomic_textbook_alignment(source_job_id,review_status,lexical_score DESC);
CREATE INDEX IF NOT EXISTS idx_kg_atomic_textbook_alignment_page
ON kg_atomic_textbook_alignment(textbook_resource_id,textbook_segment_id);
CREATE TABLE IF NOT EXISTS kg_atomic_merge_candidate (
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
CREATE INDEX IF NOT EXISTS idx_kg_atomic_merge_review
ON kg_atomic_merge_candidate(review_status,lexical_similarity DESC);
"""


def ensure_schema(connection: sqlite3.Connection, applied_at: str) -> None:
    _migrate_parent_capture_sources(connection)
    lease_table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_parent_textbook_capture_lease'").fetchone()
    if lease_table:
        # Normalize pre-v13 duplicate/expired active leases before installing the
        # partial unique index.  Keep the newest lease per device as the only
        # possible active owner; older records remain auditable as released.
        connection.execute("UPDATE kg_parent_textbook_capture_lease SET status='released',released_at=?,release_reason='expired_before_unique_lease_migration' WHERE status='active' AND expires_at<=datetime('now')", (applied_at,))
        connection.execute("""UPDATE kg_parent_textbook_capture_lease SET status='released',released_at=?,release_reason='duplicate_before_unique_lease_migration'
            WHERE status='active' AND rowid NOT IN (SELECT MAX(rowid) FROM kg_parent_textbook_capture_lease WHERE status='active' GROUP BY system_id_hash)""", (applied_at,))
    # Do this before SCHEMA creates the v17 command ledger: SQLite would retarget
    # that new foreign key during a candidate-table rename.  An interrupted prior
    # run can already have the renamed ledger; it is copied back after the new
    # parent table exists.
    candidate_sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='kg_language_focus_candidate'").fetchone()
    candidate_action = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_language_focus_candidate_action'").fetchone()
    candidate_action_legacy = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_language_focus_candidate_action_upgrade_legacy'").fetchone()
    if candidate_sql and not _candidate_schema_current(candidate_sql[0]) and candidate_action and not candidate_action_legacy:
        connection.execute("ALTER TABLE kg_language_focus_candidate_action RENAME TO kg_language_focus_candidate_action_upgrade_legacy")
        candidate_action_legacy = True
    _migrate_language_focus_candidates(connection)
    connection.executescript(SCHEMA)
    # Persist only caller-selected document roots on the original registry.
    # An old row stays NULL until explicitly associated; never infer a parent.
    connection.execute("SAVEPOINT kg_registered_document_roots")
    try:
        corpus_columns = {row[1] for row in connection.execute("PRAGMA table_info(kg_corpus)")}
        for column in ("pdf_root", "image_root"):
            if column not in corpus_columns:
                connection.execute(f"ALTER TABLE kg_corpus ADD COLUMN {column} TEXT")
        connection.execute("RELEASE SAVEPOINT kg_registered_document_roots")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT kg_registered_document_roots")
        connection.execute("RELEASE SAVEPOINT kg_registered_document_roots")
        raise
    sync_segment_occurrences(connection, applied_at)
    if candidate_action_legacy:
        connection.execute("""INSERT OR IGNORE INTO kg_language_focus_candidate_action(client_action_id,candidate_id,action,payload_sha256,result_status,created_at,updated_at)
            SELECT client_action_id,candidate_id,action,payload_sha256,result_status,created_at,updated_at
            FROM kg_language_focus_candidate_action_upgrade_legacy""")
        connection.execute("DROP TABLE kg_language_focus_candidate_action_upgrade_legacy")
    # v14 adds durable run ownership.  The import and mapping workers can be
    # started again after a shell/network timeout, so a plain status flag is not
    # enough: two processes must never advance one checkpoint concurrently.
    for table in ("kg_ingest_job", "kg_candidate_mapping_job"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "run_token" not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN run_token TEXT")
        if "run_lease_expires_at" not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN run_lease_expires_at TEXT")
    artifact_columns = {row[1] for row in connection.execute("PRAGMA table_info(kg_parent_textbook_ocr_artifact)")}
    if "import_owner_boot_id" not in artifact_columns:
        connection.execute("ALTER TABLE kg_parent_textbook_ocr_artifact ADD COLUMN import_owner_boot_id TEXT")
    for column, definition in (
        ("raw_image_path", "TEXT"),
        ("raw_image_media_type", "TEXT"),
        ("raw_image_byte_length", "INTEGER NOT NULL DEFAULT 0"),
        ("raw_image_encryption_version", "TEXT"),
    ):
        if column not in artifact_columns:
            connection.execute(
                f"ALTER TABLE kg_parent_textbook_ocr_artifact ADD COLUMN {column} {definition}"
            )
    candidate_action_columns = {row[1] for row in connection.execute("PRAGMA table_info(kg_language_focus_candidate_action)")}
    if "projection_claim" not in candidate_action_columns:
        connection.execute("ALTER TABLE kg_language_focus_candidate_action ADD COLUMN projection_claim TEXT")
    if "projection_lease_expires_at" not in candidate_action_columns:
        connection.execute("ALTER TABLE kg_language_focus_candidate_action ADD COLUMN projection_lease_expires_at TEXT")
    atomic_batch_columns = {row[1] for row in connection.execute("PRAGMA table_info(kg_atomic_extraction_batch)")}
    if "error_detail" not in atomic_batch_columns:
        connection.execute("ALTER TABLE kg_atomic_extraction_batch ADD COLUMN error_detail TEXT")
    if "rejected_output_json" not in atomic_batch_columns:
        connection.execute("ALTER TABLE kg_atomic_extraction_batch ADD COLUMN rejected_output_json TEXT")
    textbook_alignment_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(kg_atomic_textbook_alignment)")
    }
    if "evidence_role" not in textbook_alignment_columns:
        connection.execute(
            "ALTER TABLE kg_atomic_textbook_alignment ADD COLUMN evidence_role TEXT NOT NULL DEFAULT 'mention'"
        )
    connection.execute(
        "INSERT OR IGNORE INTO kg_schema_migration(version, applied_at) VALUES (?, ?)",
        (KG_SCHEMA_VERSION, applied_at),
    )


def _migrate_parent_capture_sources(connection: sqlite3.Connection) -> None:
    """Keep both original and event tables intact across any rebuild failure."""
    names = ('kg_parent_textbook_ocr_artifact', 'kg_parent_textbook_ocr_review_event')
    sql = {name: connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() for name in names}
    rebuild = bool(sql[names[0]] and ('file_source_id' not in sql[names[0]][0] or "'importing'" not in sql[names[0]][0]))
    events = bool(sql[names[1]] and (rebuild or any(value not in sql[names[1]][0] for value in ('client_action_id', "'import_recovered'", "'raw_source_missing'"))))
    if not rebuild and not events:
        return
    connection.execute('SAVEPOINT kg_parent_source_upgrade')
    def checkpoint(step):
        if _PARENT_SOURCE_MIGRATION_FAULT_AFTER == step:
            raise sqlite3.OperationalError('injected parent source upgrade: ' + step)
    try:
        affected = names if rebuild else (names[1],)
        for name in affected:
            if sql[name]:
                for index in connection.execute('SELECT name FROM sqlite_master WHERE type=\'index\' AND tbl_name=? AND sql IS NOT NULL', (name,)).fetchall():
                    connection.execute('DROP INDEX "' + index[0].replace('"','""') + '"')
                connection.execute(f'ALTER TABLE {name} RENAME TO {name}_source_upgrade')
        checkpoint('rename')
        for statement in SCHEMA.split(';'):
            statement = statement.strip()
            if any(statement.startswith('CREATE TABLE IF NOT EXISTS '+name+' (') for name in (*names,'kg_parent_textbook_file_source')):
                connection.execute(statement)
        # This historical field was added outside CREATE TABLE; retain its owner.
        if 'import_owner_boot_id' not in {r[1] for r in connection.execute('PRAGMA table_info('+names[0]+')')}:
            connection.execute('ALTER TABLE '+names[0]+' ADD COLUMN import_owner_boot_id TEXT')
        checkpoint('create')
        for name in affected:
            if not sql[name]:
                continue
            old = {r[1] for r in connection.execute(f'PRAGMA table_info({name}_source_upgrade)')}
            new = {r[1] for r in connection.execute(f'PRAGMA table_info({name})')}
            if old - new:
                raise sqlite3.DatabaseError('unrecognized parent source columns; upgrade refused')
            columns = sorted(old)
            values = list(columns)
            if name == names[1]:
                for column, default in (('payload_sha256', "'legacy'"), ('client_action_id', "'legacy:' || event_id")):
                    if column not in old:
                        columns.append(column); values.append(default)
            connection.execute(f'INSERT INTO {name} ({",".join(columns)}) SELECT {",".join(values)} FROM {name}_source_upgrade')
        checkpoint('copy')
        for name in reversed(affected):
            if sql[name]:
                connection.execute(f'DROP TABLE {name}_source_upgrade')
        checkpoint('drop')
        if connection.execute('PRAGMA foreign_key_check').fetchall():
            raise sqlite3.DatabaseError('parent source upgrade foreign key failure')
        connection.execute('RELEASE SAVEPOINT kg_parent_source_upgrade')
    except BaseException:
        connection.execute('ROLLBACK TO SAVEPOINT kg_parent_source_upgrade')
        connection.execute('RELEASE SAVEPOINT kg_parent_source_upgrade')
        raise


def sync_segment_occurrences(connection: sqlite3.Connection, applied_at: str) -> None:
    """One legacy backfill; thereafter reconcile only persistently queued writes.

    A queue avoids conflicting with modern writers' plain occurrence INSERTs.
    The savepoint keeps the backfill, migration receipt and queue drain atomic,
    including when called inside a content-import transaction.
    """
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    connection.execute("SAVEPOINT kg_occurrence_sync")
    try:
        migrated = connection.execute("SELECT 1 FROM kg_schema_migration WHERE version=?",
                                      (_OCCURRENCE_INCREMENTAL_VERSION,)).fetchone()
        if not migrated:
            connection.execute("""INSERT OR IGNORE INTO kg_segment_occurrence(resource_id,segment_id,ordinal,start_char,end_char)
                SELECT resource_id,segment_id,ordinal,start_char,end_char FROM kg_segment""")
            connection.execute("INSERT INTO kg_schema_migration(version,applied_at) VALUES(?,?)",
                               (_OCCURRENCE_INCREMENTAL_VERSION, applied_at))
        else:
            # Keep the small queue as the outer loop even before ANALYZE has
            # collected statistics for a freshly imported large corpus.
            connection.execute("""INSERT OR IGNORE INTO kg_segment_occurrence(resource_id,segment_id,ordinal,start_char,end_char)
                SELECT s.resource_id,s.segment_id,s.ordinal,s.start_char,s.end_char
                FROM kg_segment_occurrence_pending p CROSS JOIN kg_segment s
                WHERE s.segment_id=p.segment_id""")
        connection.execute("DELETE FROM kg_segment_occurrence_pending")
        connection.execute("RELEASE SAVEPOINT kg_occurrence_sync")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT kg_occurrence_sync")
        connection.execute("RELEASE SAVEPOINT kg_occurrence_sync")
        raise


def _candidate_schema_current(sql: str | None) -> bool:
    return bool(sql and "'withdrawn'" in sql and "'projecting'" in sql)


def _create_language_candidate_table(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE kg_language_focus_candidate (
      candidate_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
      language TEXT NOT NULL CHECK(language IN ('english','chinese')), term TEXT NOT NULL, normalized TEXT NOT NULL,
      focus_kind TEXT NOT NULL CHECK(focus_kind IN ('word','character','phrase')), proposed_json TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL CHECK(status IN ('pending','projecting','confirmed','deleted','stale','conflicted','withdrawn')), source_start INTEGER, source_end INTEGER,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(resource_id,language,normalized,focus_kind)
    )""")


def _candidate_migration_checkpoint(step: str) -> None:
    if _CANDIDATE_MIGRATION_FAULT_AFTER == step:
        raise sqlite3.OperationalError(f'injected candidate migration interruption: {step}')


def _migrate_language_focus_candidates(connection: sqlite3.Connection) -> None:
    """Rebuild the candidate table atomically and also heal old migration residue.

    SQLite DDL participates in a savepoint.  Therefore an injected/process
    failure leaves either the pre-upgrade layout or the fully copied new layout,
    never a half-renamed family database that needs an external snapshot.
    """
    active = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='kg_language_focus_candidate'").fetchone()
    legacy = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_language_focus_candidate_legacy'").fetchone()
    # Fresh installations are created by SCHEMA's CREATE IF NOT EXISTS below.
    # Avoid a competing non-idempotent CREATE during concurrent first requests.
    if not active and not legacy:
        return
    if active and _candidate_schema_current(active[0]) and not legacy:
        return
    connection.execute('SAVEPOINT kg_language_candidate_upgrade')
    try:
        active = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='kg_language_focus_candidate'").fetchone()
        legacy = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_language_focus_candidate_legacy'").fetchone()
        if active and _candidate_schema_current(active[0]):
            # Previous process completed the new table but died before cleanup.
            # Keep its rows authoritative; copy only rows absent by primary key.
            if legacy:
                connection.execute("INSERT OR IGNORE INTO kg_language_focus_candidate SELECT * FROM kg_language_focus_candidate_legacy")
                _candidate_migration_checkpoint('copy_legacy_into_current')
                connection.execute("DROP TABLE kg_language_focus_candidate_legacy")
                _candidate_migration_checkpoint('drop_legacy_after_current')
        else:
            if active:
                connection.execute("ALTER TABLE kg_language_focus_candidate RENAME TO kg_language_focus_candidate_upgrade_active")
                _candidate_migration_checkpoint('rename_active')
            _create_language_candidate_table(connection)
            _candidate_migration_checkpoint('create_current')
            # The active table represents the last committed primary table and
            # wins duplicate ids over stale legacy residue.
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_language_focus_candidate_upgrade_active'").fetchone()
            if exists:
                connection.execute("INSERT OR IGNORE INTO kg_language_focus_candidate SELECT * FROM kg_language_focus_candidate_upgrade_active")
                _candidate_migration_checkpoint('copy_active')
            if legacy:
                connection.execute("INSERT OR IGNORE INTO kg_language_focus_candidate SELECT * FROM kg_language_focus_candidate_legacy")
                _candidate_migration_checkpoint('copy_legacy')
            if exists:
                connection.execute("DROP TABLE kg_language_focus_candidate_upgrade_active")
                _candidate_migration_checkpoint('drop_active')
            if legacy:
                connection.execute("DROP TABLE kg_language_focus_candidate_legacy")
                _candidate_migration_checkpoint('drop_legacy')
        connection.execute('RELEASE SAVEPOINT kg_language_candidate_upgrade')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT kg_language_candidate_upgrade')
        connection.execute('RELEASE SAVEPOINT kg_language_candidate_upgrade')
        raise
