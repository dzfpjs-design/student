from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import errno
import os
import sqlite3
import threading
import time
from typing import Iterator
from uuid import uuid4


# 68 introduces the durable objective/plan backbone used by the child-facing
# agent runtime.  It deliberately lives in the family database so a restart
# cannot erase what the agent is trying to help the child accomplish.
LATEST_SCHEMA_VERSION = 99
UPGRADE_BACKUP_FORMAT = "hsc_pre_upgrade_backup_v1"


def _observation_v93_schema_valid(connection) -> bool:
    """Validate persisted v93 contracts before attempting in-place repair."""
    rows = connection.execute('PRAGMA table_info(document_observation_segment)').fetchall()
    expected = {
        'segment_id': ('TEXT', 0, 1), 'session_id': ('TEXT', 1, 0),
        'child_id': ('TEXT', 1, 0), 'monitor_id': ('TEXT', 1, 0),
        'revision_id': ('TEXT', 1, 0), 'lease_id': ('TEXT', 1, 0),
        'epoch': ('TEXT', 1, 0), 'generation': ('INTEGER', 1, 0),
        'seq': ('INTEGER', 1, 0), 'started_at': ('TEXT', 1, 0),
        'ended_at': ('TEXT', 1, 0), 'visible_ms': ('INTEGER', 1, 0),
        'payload_hash': ('TEXT', 1, 0), 'recorded_at': ('TEXT', 1, 0),
    }
    actual = {row[1]: (row[2].upper(), row[3], row[5]) for row in rows}
    if actual != expected:
        return False
    foreign = {(row[3], row[2], row[4], row[6].upper()) for row in
               connection.execute('PRAGMA foreign_key_list(document_observation_segment)')}
    if foreign != {('session_id', 'daily_session', 'session_id', 'CASCADE'),
                   ('child_id', 'child_profile', 'child_id', 'CASCADE'),
                   ('monitor_id', 'document_monitor_session', 'monitor_id', 'CASCADE'),
                   ('revision_id', 'document_page_revision', 'revision_id', 'CASCADE'),
                   ('lease_id', 'sensor_lease', 'lease_id', 'CASCADE')}:
        return False
    indexes = connection.execute('PRAGMA index_list(document_observation_segment)').fetchall()
    unique_fields = {tuple(info[2] for info in connection.execute(
        'PRAGMA index_info(' + '"' + row[1].replace('"', '""') + '"' + ')'))
        for row in indexes if row[2] and not row[4]}
    if ('monitor_id', 'epoch', 'generation', 'seq') not in unique_fields:
        return False
    monitor = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_monitor_session'").fetchone()
    revision = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_page_revision'").fetchone()
    if not monitor or not revision:
        return False
    monitor_columns = {row[1] for row in connection.execute('PRAGMA table_info(document_monitor_session)')}
    revision_columns = {row[1] for row in connection.execute('PRAGMA table_info(document_page_revision)')}
    required_monitor = {'monitor_id', 'child_id', 'session_id', 'status', 'current_state',
                        'started_at', 'updated_at', 'ended_at', 'generation', 'generation_started_at'}
    required_revision = {'revision_id', 'monitor_id', 'child_id', 'session_id', 'artifact_id',
                         'revision_index', 'content_signature', 'content_sha256', 'previous_revision_id',
                         'change_summary_json', 'ocr_confidence', 'review_required', 'study_signal_json',
                         'created_at', 'client_action_id'}
    if not required_monitor <= monitor_columns or not required_revision <= revision_columns:
        return False
    return True

# Planning and execution must agree on the next possible daily phase. These
# edges do not replace the repository's data and authorization preconditions.
SESSION_TRANSITION_TARGETS = {
    "check_in": ("classroom_recall",),
    "classroom_recall": ("homework_capture",),
    "homework_capture": ("plan_review",),
    "plan_review": ("study",),
    "study": ("break", "reflection"),
    "break": ("study",),
    "reflection": ("evolving",),
    "evolving": ("completed",),
}


class _ExternalEffectLock:
    def __init__(self):
        self.thread_lock = threading.RLock()
        self.owner = threading.local()


_EFFECT_LOCKS: dict[Path, _ExternalEffectLock] = {}
_EFFECT_LOCKS_LOCK = threading.Lock()


class UnsupportedDatabaseVersionError(RuntimeError):
    pass


class DatabaseUpgradeRecoveryError(RuntimeError):
    pass


class ExternalEffectBusyError(TimeoutError):
    pass


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migration (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state (
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
  clean_shutdown INTEGER NOT NULL CHECK(clean_shutdown IN (0, 1)),
  run_id TEXT,
  started_at TEXT,
  ended_at TEXT
);

CREATE TABLE IF NOT EXISTS child_profile (
  child_id TEXT PRIMARY KEY,
  nickname TEXT NOT NULL,
  grade TEXT NOT NULL,
  curriculum_region TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_session (
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

CREATE TABLE IF NOT EXISTS observation_control_action (
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_session
ON daily_session(status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS session_draft (
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  section TEXT NOT NULL CHECK(section IN (
    'check_in', 'classroom_recall', 'homework_capture',
    'study', 'reflection', 'agent_command'
  )),
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, section)
);

CREATE TABLE IF NOT EXISTS session_planning_context (
  session_id TEXT PRIMARY KEY REFERENCES daily_session(session_id),
  energy_level TEXT NOT NULL CHECK(energy_level IN ('low', 'medium', 'high')),
  fixed_commitment_minutes INTEGER NOT NULL CHECK(fixed_commitment_minutes BETWEEN 0 AND 180),
  fixed_commitment_note TEXT,
  tomorrow_event_subject TEXT,
  tomorrow_event_title TEXT,
  tomorrow_event_minutes INTEGER NOT NULL CHECK(tomorrow_event_minutes BETWEEN 0 AND 60),
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS planning_context_event (
  event_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS session_transition (
  transition_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  from_state TEXT,
  to_state TEXT NOT NULL,
  reason TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  client_action_id TEXT
);

CREATE TABLE IF NOT EXISTS session_declaration (
  declaration_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  declaration_type TEXT NOT NULL CHECK(declaration_type IN ('no_recall', 'no_homework')),
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS device_profile (
  device_id TEXT PRIMARY KEY,
  device_type TEXT NOT NULL,
  system_id_hash TEXT NOT NULL,
  capabilities_json TEXT NOT NULL,
  last_seen_at TEXT,
  status TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_device_profile_type_hash
ON device_profile(device_type, system_id_hash);

CREATE TABLE IF NOT EXISTS device_binding (
  device_type TEXT PRIMARY KEY CHECK(device_type IN ('camera', 'document_camera')),
  system_id_hash TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  capabilities_json TEXT NOT NULL,
  verified_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_binding_event (
  event_id TEXT PRIMARY KEY,
  bindings_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS device_qualification_run (
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

CREATE TABLE IF NOT EXISTS sensor_lease (
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

CREATE INDEX IF NOT EXISTS idx_sensor_lease_session_status
ON sensor_lease(session_id, status);

CREATE TABLE IF NOT EXISTS sensor_release_verification (
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

CREATE INDEX IF NOT EXISTS idx_sensor_release_verification_session_time
ON sensor_release_verification(session_id, verified_at DESC);

CREATE TABLE IF NOT EXISTS device_incident (
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

CREATE INDEX IF NOT EXISTS idx_device_incident_child_status
ON device_incident(child_id, status, occurred_at);

CREATE TABLE IF NOT EXISTS device_incident_report (
  report_id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL REFERENCES device_incident(incident_id),
  client_action_id TEXT NOT NULL UNIQUE,
  reported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classroom_recall (
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

CREATE TABLE IF NOT EXISTS recall_review (
  review_id TEXT PRIMARY KEY,
  recall_id TEXT NOT NULL REFERENCES classroom_recall(recall_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  decision TEXT NOT NULL,
  previous_status TEXT NOT NULL,
  resulting_status TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS homework_item (
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

CREATE TABLE IF NOT EXISTS homework_revision (
  revision_id TEXT PRIMARY KEY,
  homework_id TEXT NOT NULL REFERENCES homework_item(homework_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS daily_plan (
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

CREATE TABLE IF NOT EXISTS plan_constraint_decision (
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
CREATE INDEX IF NOT EXISTS idx_plan_constraint_decision_plan
ON plan_constraint_decision(plan_id, decision, obligation_type);

CREATE TABLE IF NOT EXISTS family_rule (
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

CREATE TABLE IF NOT EXISTS plan_revision (
  revision_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES daily_plan(plan_id),
  strategy TEXT NOT NULL,
  reason TEXT NOT NULL,
  previous_ordered_item_ids_json TEXT NOT NULL DEFAULT '[]',
  ordered_item_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_active_plan_session
ON daily_plan(session_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS weekly_goal (
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

CREATE INDEX IF NOT EXISTS idx_weekly_goal_child_week
ON weekly_goal(child_id, week_start, track);

CREATE TABLE IF NOT EXISTS plan_item (
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

CREATE TABLE IF NOT EXISTS plan_item_event (
  event_id TEXT PRIMARY KEY,
  plan_item_id TEXT NOT NULL REFERENCES plan_item(plan_item_id),
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS deferred_task_review (
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

CREATE INDEX IF NOT EXISTS idx_deferred_task_review_child_target
ON deferred_task_review(child_id, target_session_id, reviewed_at);

CREATE TABLE IF NOT EXISTS daily_reflection (
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

CREATE TABLE IF NOT EXISTS report_job (
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

CREATE TABLE IF NOT EXISTS daily_report (
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

CREATE TABLE IF NOT EXISTS period_report_evidence_reference (
  evidence_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  evidence_ref TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS period_report_evidence_state (
  evidence_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  is_retracted INTEGER NOT NULL CHECK(is_retracted IN (0, 1)),
  note TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS period_report_evidence_action (
  client_action_id TEXT PRIMARY KEY,
  evidence_id TEXT NOT NULL,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  action_type TEXT NOT NULL CHECK(action_type IN ('retract', 'restore')),
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_retry_request (
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

CREATE TABLE IF NOT EXISTS parent_report_feedback (
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

CREATE TABLE IF NOT EXISTS model_call (
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

CREATE TABLE IF NOT EXISTS model_pricing (
  provider TEXT NOT NULL CHECK(provider IN ('qwen', 'deepseek')),
  model TEXT NOT NULL,
  input_price_cny_per_million REAL NOT NULL CHECK(input_price_cny_per_million >= 0),
  output_price_cny_per_million REAL NOT NULL CHECK(output_price_cny_per_million >= 0),
  currency TEXT NOT NULL DEFAULT 'CNY' CHECK(currency = 'CNY'),
  source TEXT NOT NULL DEFAULT 'parent_configured' CHECK(source = 'parent_configured'),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(provider, model)
);

CREATE TABLE IF NOT EXISTS tutor_turn (
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

CREATE TABLE IF NOT EXISTS strategy_experiment (
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_one_open
ON strategy_experiment(child_id, strategy_key)
WHERE status IN ('draft', 'replay_passed', 'shadow', 'active');

CREATE TABLE IF NOT EXISTS strategy_evaluation_event (
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

CREATE INDEX IF NOT EXISTS idx_strategy_evaluation_experiment
ON strategy_evaluation_event(experiment_id, phase, occurred_at);

CREATE TABLE IF NOT EXISTS strategy_transition_event (
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

CREATE TABLE IF NOT EXISTS tutor_feedback (
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

CREATE TABLE IF NOT EXISTS parent_security (
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
  pin_salt BLOB NOT NULL,
  pin_hash BLOB NOT NULL,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_node (
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

CREATE TABLE IF NOT EXISTS knowledge_source (
  source_id TEXT PRIMARY KEY,
  child_id TEXT REFERENCES child_profile(child_id),
  source_type TEXT NOT NULL,
  title TEXT NOT NULL,
  subject TEXT NOT NULL,
  grade_band TEXT NOT NULL,
  publisher TEXT,
  version_label TEXT,
  source_ref TEXT NOT NULL,
  effective_from TEXT,
  effective_to TEXT,
  license_status TEXT NOT NULL,
  review_status TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  UNIQUE(child_id, content_hash)
);

CREATE TABLE IF NOT EXISTS knowledge_edge (
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

CREATE INDEX IF NOT EXISTS idx_knowledge_edge_target
ON knowledge_edge(target_node_id, relation_type, status);

CREATE TABLE IF NOT EXISTS knowledge_excerpt (
  excerpt_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES knowledge_source(source_id),
  sequence INTEGER NOT NULL,
  heading TEXT,
  content_text TEXT NOT NULL,
  content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS curriculum_corpus (
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

CREATE TABLE IF NOT EXISTS curriculum_corpus_import_job (
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

CREATE TABLE IF NOT EXISTS curriculum_resource (
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

CREATE INDEX IF NOT EXISTS idx_curriculum_resource_grade_subject
ON curriculum_resource(grade_title, subject_title, corpus_id);

CREATE TABLE IF NOT EXISTS curriculum_topic (
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

CREATE INDEX IF NOT EXISTS idx_curriculum_topic_scope
ON curriculum_topic(grade_title, subject_title, semester, maturity);

CREATE TABLE IF NOT EXISTS curriculum_resource_topic (
  row_id INTEGER NOT NULL REFERENCES curriculum_resource(row_id) ON DELETE CASCADE,
  topic_id TEXT NOT NULL REFERENCES curriculum_topic(topic_id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL DEFAULT 'evidence_for' CHECK(relation_type = 'evidence_for'),
  PRIMARY KEY(row_id, topic_id)
);

CREATE TABLE IF NOT EXISTS curriculum_internalization_job (
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

CREATE TABLE IF NOT EXISTS curriculum_internalization_batch (
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

CREATE TABLE IF NOT EXISTS curriculum_capability (
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

CREATE INDEX IF NOT EXISTS idx_curriculum_capability_scope
ON curriculum_capability(grade_title, subject_title, semester, maturity);

CREATE TABLE IF NOT EXISTS curriculum_capability_source (
  capability_id TEXT NOT NULL REFERENCES curriculum_capability(capability_id) ON DELETE CASCADE,
  topic_id TEXT NOT NULL REFERENCES curriculum_topic(topic_id) ON DELETE CASCADE,
  PRIMARY KEY(capability_id, topic_id)
);

CREATE TABLE IF NOT EXISTS curriculum_standard_source (
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

CREATE TABLE IF NOT EXISTS curriculum_standard_page (
  source_id TEXT NOT NULL REFERENCES curriculum_standard_source(source_id) ON DELETE CASCADE,
  page_number INTEGER NOT NULL,
  content_text TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  char_count INTEGER NOT NULL,
  PRIMARY KEY(source_id, page_number)
);

CREATE VIRTUAL TABLE IF NOT EXISTS curriculum_standard_page_fts USING fts5(
  source_id UNINDEXED, page_number UNINDEXED, content_text, tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS curriculum_standard_requirement (
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

CREATE INDEX IF NOT EXISTS idx_curriculum_standard_requirement_scope
ON curriculum_standard_requirement(subject_title, stage_scope, requirement_type, maturity);

CREATE TABLE IF NOT EXISTS curriculum_standard_topic_alignment (
  requirement_id TEXT NOT NULL REFERENCES curriculum_standard_requirement(requirement_id) ON DELETE CASCADE,
  topic_id TEXT NOT NULL REFERENCES curriculum_topic(topic_id) ON DELETE CASCADE,
  coverage_type TEXT NOT NULL CHECK(coverage_type IN ('full', 'partial', 'context_only')),
  rationale TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  maturity TEXT NOT NULL CHECK(maturity IN ('model_drafted', 'verified', 'conflicted', 'retired')),
  PRIMARY KEY(requirement_id, topic_id)
);

CREATE TABLE IF NOT EXISTS curriculum_capability_standard (
  capability_id TEXT NOT NULL REFERENCES curriculum_capability(capability_id) ON DELETE CASCADE,
  requirement_id TEXT NOT NULL REFERENCES curriculum_standard_requirement(requirement_id) ON DELETE CASCADE,
  PRIMARY KEY(capability_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS curriculum_topic_standard_candidate (
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

CREATE INDEX IF NOT EXISTS idx_curriculum_topic_standard_candidate_status
ON curriculum_topic_standard_candidate(alignment_status);

CREATE VIRTUAL TABLE IF NOT EXISTS curriculum_resource_fts USING fts5(
  title, description, transcript_text, unit_path, grade_title, subject_title,
  content='curriculum_resource', content_rowid='row_id', tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS curriculum_resource_ai AFTER INSERT ON curriculum_resource BEGIN
  INSERT INTO curriculum_resource_fts(rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
  VALUES (new.row_id, new.title, new.description, new.transcript_text, new.unit_path, new.grade_title, new.subject_title);
END;

CREATE TRIGGER IF NOT EXISTS curriculum_resource_ad AFTER DELETE ON curriculum_resource BEGIN
  INSERT INTO curriculum_resource_fts(curriculum_resource_fts, rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
  VALUES ('delete', old.row_id, old.title, old.description, old.transcript_text, old.unit_path, old.grade_title, old.subject_title);
END;

CREATE TRIGGER IF NOT EXISTS curriculum_resource_au AFTER UPDATE ON curriculum_resource BEGIN
  INSERT INTO curriculum_resource_fts(curriculum_resource_fts, rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
  VALUES ('delete', old.row_id, old.title, old.description, old.transcript_text, old.unit_path, old.grade_title, old.subject_title);
  INSERT INTO curriculum_resource_fts(rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
  VALUES (new.row_id, new.title, new.description, new.transcript_text, new.unit_path, new.grade_title, new.subject_title);
END;

CREATE TABLE IF NOT EXISTS diagnostic_item (
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

CREATE TABLE IF NOT EXISTS learning_evidence (
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

CREATE TABLE IF NOT EXISTS learning_evidence_withdrawal (
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

CREATE TRIGGER IF NOT EXISTS learning_evidence_withdrawal_no_update
BEFORE UPDATE ON learning_evidence_withdrawal
BEGIN SELECT RAISE(ABORT, 'immutable learning evidence withdrawal'); END;

CREATE TABLE IF NOT EXISTS mastery_state (
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

CREATE TABLE IF NOT EXISTS mastery_update_event (
  event_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
  evidence_id TEXT NOT NULL UNIQUE REFERENCES learning_evidence(evidence_id),
  old_state_json TEXT NOT NULL,
  new_state_json TEXT NOT NULL,
  algorithm_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mastery_update_child_node
ON mastery_update_event(child_id, knowledge_node_id, created_at);

CREATE TABLE IF NOT EXISTS review_task (
  review_task_id TEXT PRIMARY KEY,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
  diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
  source_evidence_id TEXT NOT NULL REFERENCES learning_evidence(evidence_id),
  interval_label TEXT NOT NULL,
  due_at TEXT NOT NULL,
  status TEXT NOT NULL,
  completed_at TEXT,
  result_evidence_id TEXT REFERENCES learning_evidence(evidence_id),
  created_at TEXT NOT NULL,
  UNIQUE(source_evidence_id, interval_label)
);

CREATE INDEX IF NOT EXISTS idx_review_task_due
ON review_task(child_id, status, due_at);

CREATE TABLE IF NOT EXISTS diagnostic_attempt (
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

CREATE TABLE IF NOT EXISTS mistake_cluster (
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

CREATE TABLE IF NOT EXISTS mistake_case (
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

CREATE INDEX IF NOT EXISTS idx_mistake_case_child_status
ON mistake_case(child_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS mistake_occurrence (
  occurrence_id TEXT PRIMARY KEY,
  mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  evidence_id TEXT REFERENCES learning_evidence(evidence_id) ON DELETE SET NULL,
  submitted_answer TEXT NOT NULL,
  source_ref TEXT,
  occurred_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS mistake_diagnosis (
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

CREATE TABLE IF NOT EXISTS remediation_step (
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

CREATE TABLE IF NOT EXISTS mistake_attempt (
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

CREATE INDEX IF NOT EXISTS idx_mistake_attempt_case_stage
ON mistake_attempt(mistake_id, stage, created_at);

CREATE TABLE IF NOT EXISTS mistake_mastery_gate (
  mistake_id TEXT PRIMARY KEY REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
  error_type TEXT NOT NULL,
  requirements_json TEXT NOT NULL,
  delayed_48h_due_at TEXT,
  delayed_7d_due_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mistake_status_event (
  event_id TEXT PRIMARY KEY,
  mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  from_status TEXT,
  to_status TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS privacy_preference (
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

CREATE TABLE IF NOT EXISTS consent_record (
  consent_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  allowed INTEGER NOT NULL,
  policy_version TEXT NOT NULL,
  source TEXT NOT NULL,
  occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_event (
  audit_id TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  child_id TEXT,
  summary_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_manifest (
  backup_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  database_version INTEGER NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  integrity_status TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_run (
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

CREATE TABLE IF NOT EXISTS deletion_job (
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

CREATE TABLE IF NOT EXISTS period_report (
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

CREATE TABLE IF NOT EXISTS period_report_refresh_state (
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

CREATE TABLE IF NOT EXISTS observation_calibration_run (
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

CREATE TABLE IF NOT EXISTS capture_artifact (
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

CREATE TABLE IF NOT EXISTS mistake_capture_candidate (
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

CREATE INDEX IF NOT EXISTS idx_mistake_capture_candidate_artifact
ON mistake_capture_candidate(artifact_id, question_index);

CREATE TABLE IF NOT EXISTS mistake_capture_candidate_event (
  event_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES mistake_capture_candidate(candidate_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK(event_type IN ('prepared', 'edited', 'skipped', 'captured')),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS mistake_dialogue (
  session_id TEXT PRIMARY KEY REFERENCES daily_session(session_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  candidate_id TEXT REFERENCES mistake_capture_candidate(candidate_id) ON DELETE SET NULL,
  mistake_id TEXT REFERENCES mistake_case(mistake_id) ON DELETE SET NULL,
  phase TEXT NOT NULL,
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mistake_dialogue_event (
  client_action_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  request_hash TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voice_input (
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

CREATE TABLE IF NOT EXISTS agent_turn (
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

CREATE TABLE IF NOT EXISTS agent_objective (
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_one_active_objective
ON agent_objective(child_id) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_agent_objective_session_time
ON agent_objective(session_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_plan (
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

CREATE TABLE IF NOT EXISTS agent_step (
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

CREATE INDEX IF NOT EXISTS idx_agent_step_plan_status
ON agent_step(plan_id, status, step_index);

CREATE TABLE IF NOT EXISTS agent_outcome (
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

CREATE TABLE IF NOT EXISTS agent_commitment (
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

CREATE INDEX IF NOT EXISTS idx_agent_commitment_child_due
ON agent_commitment(child_id, status, due_at);

CREATE TABLE IF NOT EXISTS child_safety_event (
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

CREATE TABLE IF NOT EXISTS child_state_event (
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

CREATE TABLE IF NOT EXISTS observation_correction (
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

-- This is deliberately a small session-level record, not a media artifact.
-- It lets a resumed renderer tell the child why observation is still shadowed
-- without retaining a frame, a face template, or pixel-derived metrics.
CREATE TABLE IF NOT EXISTS observation_qualification (
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
CREATE INDEX IF NOT EXISTS idx_observation_qualification_child_time
ON observation_qualification(child_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS intervention_record (
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

-- Local-only, reversible observable evidence. These tables intentionally live
-- in the same database as the family record so backup/restore/export/deletion
-- cannot silently omit longitudinal behavior history.
CREATE TABLE IF NOT EXISTS observable_event (
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
CREATE INDEX IF NOT EXISTS idx_behavior_event_child_time ON observable_event(child_id, occurred_at);
CREATE TABLE IF NOT EXISTS probabilistic_state (
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
CREATE INDEX IF NOT EXISTS idx_behavior_state_child_time ON probabilistic_state(child_id, occurred_at);
CREATE TABLE IF NOT EXISTS intervention (
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_feedback_action ON intervention(feedback_action_id) WHERE feedback_action_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_source_event ON intervention(source_event_id) WHERE source_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_effect_action ON intervention(effect_action_id) WHERE effect_action_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS intervention_policy_reset (child_id TEXT PRIMARY KEY, reset_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS behavior_retraction (
  event_id TEXT PRIMARY KEY REFERENCES observable_event(event_id),
  child_id TEXT NOT NULL,
  correction_id TEXT NOT NULL UNIQUE,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS behavior_stop_condition (
  child_id TEXT NOT NULL,
  session_id TEXT,
  candidate_state TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  correction_id TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY(child_id, session_id, candidate_state, source_event_id)
);
CREATE TABLE IF NOT EXISTS daily_feature (
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
CREATE TABLE IF NOT EXISTS habit_experiment (
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_habit_one_open
ON habit_experiment(child_id) WHERE status IN ('proposed','active');
CREATE TABLE IF NOT EXISTS habit_experiment_event (
  event_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL REFERENCES habit_experiment(experiment_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS habit_formation_state (
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
CREATE INDEX IF NOT EXISTS idx_habit_formation_child_stage
ON habit_formation_state(child_id, stage, updated_at DESC);
CREATE TABLE IF NOT EXISTS habit_formation_event (
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
CREATE TRIGGER IF NOT EXISTS trg_habit_formation_event_no_update
BEFORE UPDATE ON habit_formation_event BEGIN
  SELECT RAISE(ABORT, 'habit_formation_event is immutable');
END;
CREATE TABLE IF NOT EXISTS behavior_legacy_migration (
  source_path TEXT PRIMARY KEY,
  completed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_child_state_session_time
ON child_state_event(session_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_learning_evidence_child_node
ON learning_evidence(child_id, knowledge_node_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_mastery_review
ON mastery_state(child_id, next_review_at);

CREATE INDEX IF NOT EXISTS idx_daily_report_child_date
ON daily_report(child_id, report_date DESC);

CREATE INDEX IF NOT EXISTS idx_model_call_child_created
ON model_call(child_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tutor_turn_session_created
ON tutor_turn(session_id, created_at);
"""


@dataclass(frozen=True)
class LocalMediaRuntime:
    media_root: Path = field(repr=False)
    data_encryption_key: str | None = field(default=None, repr=False)


class StudyCursor(sqlite3.Cursor):
    def execute(self,*args,**kwargs):
        from .realtime_native_write_scope import verify
        verify(self.connection)
        result=super().execute(*args,**kwargs)
        verify(self.connection)
        return result

    def executemany(self,*args,**kwargs):
        from .realtime_native_write_scope import verify
        def checked(values):
            for value in values:
                verify(self.connection)
                yield value
        values=args[1] if len(args)>1 else kwargs.pop('seq_of_parameters')
        verify(self.connection)
        result=super().executemany(args[0],checked(values))
        verify(self.connection)
        return result

    def executescript(self,script):
        from .realtime_native_write_scope import script_allowed,verify
        if script_allowed():return super().executescript(script)
        # Original lazy schema initialization can legitimately run inside a
        # native effect. Execute complete SQLite statements through the same
        # fence; do not use the C shortcut with an unchecked internal cursor.
        pending=''
        for character in script:
            pending+=character
            if character==';' and sqlite3.complete_statement(pending):
                self.execute(pending);pending=''
        if pending.strip():self.execute(pending)
        verify(self.connection)
        return self



class StudyConnection(sqlite3.Connection):
    def cursor(self,factory=None):
        from .realtime_native_write_scope import script_allowed
        if factory is not None and factory is not StudyCursor and not script_allowed():
            raise ValueError('NATIVE_EXECUTION_CURSOR_NOT_ALLOWED')
        return super().cursor(factory or StudyCursor)

    def execute(self,*args,**kwargs):return self.cursor().execute(*args,**kwargs)
    def executemany(self,*args,**kwargs):return self.cursor().executemany(*args,**kwargs)
    def executescript(self,*args,**kwargs):return self.cursor().executescript(*args,**kwargs)

    def commit(self):
        from .realtime_native_write_scope import verify
        try:verify(self)
        except BaseException:
            super().rollback()
            raise
        return super().commit()

    def __exit__(self,exc_type,exc_value,traceback):
        if exc_type is None:
            self.commit()
            return False
        self.rollback()
        return False

    local_media_runtime: LocalMediaRuntime | None = None
    local_semantic_matcher = None


def local_media_scope(connection) -> dict:
    context = getattr(connection, 'local_media_runtime', None)
    if not isinstance(context, LocalMediaRuntime):
        return {}
    return {'media_root': context.media_root, 'data_encryption_key': context.data_encryption_key}


class Database:
    def __init__(self, path: Path, *, media_root: Path | None = None, data_encryption_key: str | None = None, runtime_mode: str = 'formal'):
        if runtime_mode not in {'formal', 'trial'}:
            raise ValueError('RUNTIME_PROFILE_MODE_INVALID')
        self.path = path
        self.runtime_mode = runtime_mode
        self.local_media_runtime = LocalMediaRuntime(Path(media_root), data_encryption_key) if media_root is not None else None
        self.local_semantic_matcher = None
        self._original_connections = {}

    @contextmanager
    def external_effect_guard(self, timeout: float = 120.0, *, deadline: float | None = None) -> Iterator[None]:
        """Keep irreversible network submissions outside a restore's lifetime.

        A path-shared reentrant lock coordinates service instances; the adjacent
        OS lock also excludes another local worker process. Never unlink it:
        replacing a locked inode would create two independent owners.
        """
        path = self.path.resolve()
        with _EFFECT_LOCKS_LOCK:
            state = _EFFECT_LOCKS.setdefault(path, _ExternalEffectLock())
        if deadline is None:
            deadline = time.monotonic() + max(0.0, timeout)
            thread_wait = max(0.0, timeout)
        else:
            thread_wait = max(0.0, deadline - time.monotonic())
        if not state.thread_lock.acquire(timeout=thread_wait):
            raise ExternalEffectBusyError('A database restore or external submission is still running.')
        handle = None
        acquired = False
        try:
            if getattr(state.owner, 'active', False):
                yield
                return
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle = path.with_name(f'.{path.name}.external-effects.lock').open('a+b')
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b'0')
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise ExternalEffectBusyError('A database restore or external submission is still running.') from error
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            from .smtp_owned_journal import check_and_reconcile
            try:
                check_and_reconcile(path)
            except ValueError as error:
                raise ExternalEffectBusyError(str(error)) from error
            state.owner.active = True
            try:
                yield
            finally:
                state.owner.active = False
        finally:
            try:
                if acquired:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                if handle is not None:
                    handle.close()
                state.thread_lock.release()

    def _external_effect_guard_owned(self) -> bool:
        with _EFFECT_LOCKS_LOCK:
            state = _EFFECT_LOCKS.get(self.path.resolve())
        return bool(state and getattr(state.owner, 'active', False))

    def initialize(self, *, startup_wait_seconds: float = 0) -> None:
        from .restore_publication import publication_owner, recover_before_initialize, _database_identity, RestorePublicationError, initialization_preflight
        if type(startup_wait_seconds) not in (int, float) or not 0 <= startup_wait_seconds <= 120:
            raise ValueError('INITIALIZATION_WAIT_BUDGET_REQUIRED')
        startup_deadline = time.monotonic() + startup_wait_seconds if startup_wait_seconds else None
        # Rejection only: this original readonly view grants no writer/cleanup
        # authority. Recovery and all admission checks repeat under the owner.
        preflight = initialization_preflight(self) if startup_deadline is None else initialization_preflight(self, deadline=startup_deadline)
        with preflight as connection:
            if connection is not None:
                from .parent_restore_ownership import _anchor
                from .runtime_data_scope import assert_scope
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if _anchor(connection) is None and 'schema_migration' in tables:
                    assert_scope(connection, self.runtime_mode)
                    source_version = int(connection.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] or 0)
                    if source_version > LATEST_SCHEMA_VERSION:
                        raise UnsupportedDatabaseVersionError(
                            f"数据库版本 {source_version} 高于当前应用支持的 {LATEST_SCHEMA_VERSION}，已拒绝降级打开。"
                        )
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        owner_context = publication_owner(self) if startup_deadline is None else publication_owner(self, deadline=startup_deadline)
        try:
            with owner_context as owner:
                # Same exclusive owner: recovery cannot race admission, and owner SQL
                # bypass must never bypass the active-journal startup decision.
                recover_before_initialize(self)
                result = self._initialize_admitted()
                identity = _database_identity(self.path, owner.parent, owner.gate)
                expected = owner.gate.database_identity
                if identity is None or (expected is not None and identity != expected):
                    raise RestorePublicationError('RESTORE_PUBLICATION_DATABASE_ALIAS')
                if expected is None:  # First owned creation only; never reset a pin.
                    owner.gate.database_identity = identity
                return result
        except RestorePublicationError as error:
            # ExitStack closes a poisoned owner without the active exception.
            # Preserve the original failed rollback and its migration exception
            # as the cause while leaving the poisoned gate closed to all writers.
            if str(error) == 'RESTORE_PUBLICATION_CLOSE_UNCONFIRMED':
                from .restore_publication import _gate
                for retained in reversed(_gate(self.path).retained):
                    if isinstance(retained, tuple) and len(retained) == 4 and retained[0] == 'migration_rollback':
                        raise error from retained[3]
            raise

    def _initialize_admitted(self) -> None:
        from .runtime_data_scope import assert_scope
        from .restore_publication import _cold
        if self.path.exists() and self.path.stat().st_size:
            with _cold(self.path, owner=self._restore_sql_owner()) as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migration'").fetchone():
                    assert_scope(connection, self.runtime_mode)
        snapshot = self._prepare_upgrade_snapshot()
        try:
            if self._existing_schema_version() in (93, 94, 95, 96, 97, 98, 99):
                with self.connect() as connection:
                    if not _observation_v93_schema_valid(connection):
                        raise sqlite3.DatabaseError('OBSERVATION_V93_SCHEMA_INVALID')
                    if self._existing_schema_version() in (94, 95, 96, 97, 98, 99):
                        from .daily_report_observation import schema_valid
                        if not schema_valid(connection):
                            raise sqlite3.DatabaseError('DAILY_OBSERVATION_V94_SCHEMA_INVALID')
                    if self._existing_schema_version() in (95, 96, 97, 98, 99):
                        from .daily_report_frozen_snapshot import schema_valid
                        if not schema_valid(connection):
                            raise sqlite3.DatabaseError('DAILY_OBSERVATION_V95_SCHEMA_INVALID')
                        from .daily_report_email_verification import schema_valid as email_verification_schema_valid
                        if not email_verification_schema_valid(connection):
                            raise sqlite3.DatabaseError('DAILY_EMAIL_VERIFICATION_V95_SCHEMA_INVALID')
                        from .daily_report_delivery_state import schema_valid as daily_delivery_state_schema_valid
                        if not daily_delivery_state_schema_valid(connection):
                            raise sqlite3.DatabaseError('DAILY_DELIVERY_STATE_V95_SCHEMA_INVALID')
                    if self._existing_schema_version() in (96, 97, 98, 99):
                        from .daily_report_visual_schedule import _valid_schema
                        if not _valid_schema(connection):
                            raise sqlite3.DatabaseError('DAILY_VISUAL_SCHEDULE_V96_SCHEMA_INVALID')
                        from .daily_report_combined_schema import schema_valid as combined_schema_valid
                        if not combined_schema_valid(connection):
                            raise sqlite3.DatabaseError('COMBINED_REPORT_V96_SCHEMA_INVALID')
                    if self._existing_schema_version() in (97, 98, 99):
                        from .daily_report_combined_mode import schema_valid as combined_mode_schema_valid
                        if not combined_mode_schema_valid(connection):
                            raise sqlite3.DatabaseError('COMBINED_MODE_V97_SCHEMA_INVALID')
                    if self._existing_schema_version() in (98, 99):
                        from .smtp_owned_journal import schema_valid as smtp_journal_schema_valid
                        if not smtp_journal_schema_valid(connection):
                            raise sqlite3.DatabaseError('SMTP_WORKER_V98_SCHEMA_INVALID')
                    if self._existing_schema_version() == 99:
                        from .smtp_owned_journal import schema_valid_v99
                        if not schema_valid_v99(connection):
                            raise sqlite3.DatabaseError('SMTP_WORKER_V99_SCHEMA_INVALID')
            self._initialize_schema()
            integrity = self._integrity_check(self.path)
            if integrity != "ok":
                raise sqlite3.DatabaseError(f"database integrity check failed: {integrity}")
        except Exception as error:
            if snapshot:
                try:
                    self._restore_upgrade_snapshot(snapshot, error)
                except Exception as recovery_error:
                    raise DatabaseUpgradeRecoveryError(
                        "数据库升级失败，升级前恢复点也未能自动还原。"
                    ) from recovery_error
            raise
        if snapshot:
            self._complete_upgrade_snapshot(snapshot)
            self._prune_upgrade_snapshots()

    def _initialize_schema(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect(rollback_on_error=True) as connection:
            connection.executescript(SCHEMA)
            # Immutable decision and outcome provenance tables must exist before
            # any request transaction. Creating them lazily would make SQLite's
            # executescript commit a partially written child interaction.
            from .decision_record import ensure_schema as ensure_decision_record_schema
            ensure_decision_record_schema(connection)
            from .outcome_attribution import ensure_schema as ensure_outcome_attribution_schema
            ensure_outcome_attribution_schema(connection)
            from .school_review_versions import migrate as migrate_school_reviews
            migrate_school_reviews(connection)
            from .knowledge_source_revision import migrate as migrate_source_revisions
            migrate_source_revisions(connection)
            from .mistake_source_revision import migrate as migrate_mistake_source_revisions
            migrate_mistake_source_revisions(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (72,datetime('now'))")
            from .learning_evidence_withdrawal import ensure_schema as ensure_evidence_withdrawal
            ensure_evidence_withdrawal(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (74,datetime('now'))")
            from .behavior_model.habit_formation import ensure_habit_formation_schema
            ensure_habit_formation_schema(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (75,datetime('now'))")
            from .bounded_experiment_protocol import ensure_schema as ensure_bounded_experiment_schema
            ensure_bounded_experiment_schema(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (76,datetime('now'))")
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (77,datetime('now'))")
            from .paper_scoring import ensure_paper_scoring_schema
            ensure_paper_scoring_schema(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (78,datetime('now'))")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (1, datetime('now'))"
            )
            transition_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(session_transition)").fetchall()
            }
            if "client_action_id" not in transition_columns:
                connection.execute(
                    "ALTER TABLE session_transition ADD COLUMN client_action_id TEXT"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_transition_client_action
                ON session_transition(client_action_id) WHERE client_action_id IS NOT NULL
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (2, datetime('now'))"
            )
            plan_item_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(plan_item)").fetchall()
            }
            if "started_at" not in plan_item_columns:
                connection.execute("ALTER TABLE plan_item ADD COLUMN started_at TEXT")
            if "completed_at" not in plan_item_columns:
                connection.execute("ALTER TABLE plan_item ADD COLUMN completed_at TEXT")
            if "active_seconds" not in plan_item_columns:
                connection.execute(
                    "ALTER TABLE plan_item ADD COLUMN active_seconds INTEGER NOT NULL DEFAULT 0"
                )
            if "active_segment_started_at" not in plan_item_columns:
                connection.execute(
                    "ALTER TABLE plan_item ADD COLUMN active_segment_started_at TEXT"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (3, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (4, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (5, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (6, datetime('now'))"
            )
            diagnostic_attempt_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(diagnostic_attempt)").fetchall()
            }
            if "review_task_id" not in diagnostic_attempt_columns:
                connection.execute("ALTER TABLE diagnostic_attempt ADD COLUMN review_task_id TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (7, datetime('now'))"
            )
            tutor_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tutor_turn)").fetchall()
            }
            for column, definition in (
                ("hint_level", "INTEGER NOT NULL DEFAULT 1"),
                ("parent_turn_id", "TEXT"),
                ("response_kind", "TEXT NOT NULL DEFAULT 'hint'"),
                ("verification_status", "TEXT NOT NULL DEFAULT 'not_applicable_hint'"),
                ("safety_status", "TEXT NOT NULL DEFAULT 'passed'"),
            ):
                if column not in tutor_columns:
                    connection.execute(f"ALTER TABLE tutor_turn ADD COLUMN {column} {definition}")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (8, datetime('now'))"
            )
            diagnostic_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(diagnostic_item)").fetchall()
            }
            for column, definition in (
                ("stimulus_text", "TEXT"),
                ("stimulus_asset", "TEXT"),
                ("assessment_mode", "TEXT NOT NULL DEFAULT 'objective'"),
            ):
                if column not in diagnostic_columns:
                    connection.execute(f"ALTER TABLE diagnostic_item ADD COLUMN {column} {definition}")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (9, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (10, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (11, datetime('now'))"
            )
            plan_item_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(plan_item)").fetchall()
            }
            for column, definition in (
                ("source_type", "TEXT NOT NULL DEFAULT 'school_homework'"),
                ("source_ref", "TEXT"),
                ("completion_standard", "TEXT NOT NULL DEFAULT '孩子确认完成'"),
                ("cognitive_load", "TEXT NOT NULL DEFAULT 'medium'"),
            ):
                if column not in plan_item_columns:
                    connection.execute(f"ALTER TABLE plan_item ADD COLUMN {column} {definition}")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (12, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (13, datetime('now'))"
            )
            session_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(daily_session)").fetchall()
            }
            if "observation_status" not in session_columns:
                connection.execute(
                    "ALTER TABLE daily_session ADD COLUMN observation_status TEXT NOT NULL DEFAULT 'enabled'"
                )
            if "observation_revision" not in session_columns:
                connection.execute("ALTER TABLE daily_session ADD COLUMN observation_revision INTEGER NOT NULL DEFAULT 0 CHECK(observation_revision >= 0)")
            connection.execute("INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (84, datetime('now'))")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (14, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (15, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (16, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO runtime_state(singleton_id, clean_shutdown) VALUES (1, 1)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (17, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (18, datetime('now'))"
            )
            family_rule_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(family_rule)").fetchall()
            }
            if "observation_mode" not in family_rule_columns:
                connection.execute(
                    "ALTER TABLE family_rule ADD COLUMN observation_mode TEXT NOT NULL DEFAULT 'shadow'"
                )
            if "reminder_budget_per_10m" not in family_rule_columns:
                connection.execute(
                    "ALTER TABLE family_rule ADD COLUMN reminder_budget_per_10m INTEGER NOT NULL DEFAULT 2"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (19, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (20, datetime('now'))"
            )
            model_call_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(model_call)").fetchall()
            }
            for column, definition in (
                ("request_id", "TEXT NOT NULL DEFAULT ''"),
                ("risk_level", "TEXT NOT NULL DEFAULT 'medium'"),
                ("provider_preference", "TEXT NOT NULL DEFAULT 'configured_primary'"),
                ("policy_version", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("knowledge_version", "TEXT"),
                ("verification_status", "TEXT"),
                ("safety_status", "TEXT"),
                ("fallback_used", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in model_call_columns:
                    connection.execute(f"ALTER TABLE model_call ADD COLUMN {column} {definition}")
            connection.execute(
                "UPDATE model_call SET request_id = call_id WHERE request_id = ''"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (21, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (22, datetime('now'))"
            )
            homework_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(homework_item)").fetchall()
            }
            for column, definition in (
                ("special_requirements", "TEXT"),
                ("source_artifact_id", "TEXT"),
                ("ocr_confidence", "REAL"),
            ):
                if column not in homework_columns:
                    connection.execute(f"ALTER TABLE homework_item ADD COLUMN {column} {definition}")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (23, datetime('now'))"
            )
            plan_item_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(plan_item)").fetchall()
            }
            for column in ("completion_evidence_type", "completion_note"):
                if column not in plan_item_columns:
                    connection.execute(f"ALTER TABLE plan_item ADD COLUMN {column} TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (24, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (25, datetime('now'))"
            )
            reflection_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(daily_reflection)").fetchall()
            }
            if "skipped" not in reflection_columns:
                connection.execute(
                    "ALTER TABLE daily_reflection ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0 CHECK(skipped IN (0, 1))"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (26, datetime('now'))"
            )
            plan_item_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(plan_item)").fetchall()
            }
            for column, definition in (
                ("task_type", "TEXT NOT NULL DEFAULT 'written'"),
                ("completion_checklist_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if column not in plan_item_columns:
                    connection.execute(f"ALTER TABLE plan_item ADD COLUMN {column} {definition}")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (27, datetime('now'))"
            )
            session_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(daily_session)").fetchall()
            }
            if "snooze_until" not in session_columns:
                connection.execute("ALTER TABLE daily_session ADD COLUMN snooze_until TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (28, datetime('now'))"
            )
            family_rule_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(family_rule)").fetchall()
            }
            if "misclassification_shadow_threshold" not in family_rule_columns:
                connection.execute(
                    "ALTER TABLE family_rule ADD COLUMN misclassification_shadow_threshold INTEGER NOT NULL DEFAULT 3 CHECK(misclassification_shadow_threshold BETWEEN 1 AND 10)"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (29, datetime('now'))"
            )
            agent_turn_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(agent_turn)").fetchall()
            }
            for column, definition in (
                ("tool_status", "TEXT NOT NULL DEFAULT 'pending' CHECK(tool_status IN ('pending', 'completed', 'failed'))"),
                ("tool_error_code", "TEXT"),
                ("completed_at", "TEXT"),
            ):
                if column not in agent_turn_columns:
                    connection.execute(f"ALTER TABLE agent_turn ADD COLUMN {column} {definition}")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (30, datetime('now'))"
            )
            plan_revision_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(plan_revision)").fetchall()
            }
            if "previous_ordered_item_ids_json" not in plan_revision_columns:
                connection.execute(
                    "ALTER TABLE plan_revision ADD COLUMN previous_ordered_item_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (31, datetime('now'))"
            )
            session_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(daily_session)").fetchall()
            }
            for column, definition in (
                ("recall_declared_empty", "INTEGER NOT NULL DEFAULT 0 CHECK(recall_declared_empty IN (0, 1))"),
                ("homework_declared_empty", "INTEGER NOT NULL DEFAULT 0 CHECK(homework_declared_empty IN (0, 1))"),
            ):
                if column not in session_columns:
                    connection.execute(f"ALTER TABLE daily_session ADD COLUMN {column} {definition}")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (32, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (33, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (34, datetime('now'))"
            )
            backup_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(backup_manifest)").fetchall()
            }
            if "reason" not in backup_columns:
                connection.execute(
                    "ALTER TABLE backup_manifest ADD COLUMN reason TEXT NOT NULL DEFAULT 'manual'"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (35, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (36, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (37, datetime('now'))"
            )
            plan_item_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(plan_item)").fetchall()
            }
            if "completion_artifact_id" not in plan_item_columns:
                connection.execute("ALTER TABLE plan_item ADD COLUMN completion_artifact_id TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (38, datetime('now'))"
            )
            knowledge_node_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(knowledge_node)").fetchall()
            }
            if "node_type" not in knowledge_node_columns:
                connection.execute(
                    "ALTER TABLE knowledge_node ADD COLUMN node_type TEXT NOT NULL DEFAULT 'knowledge_point'"
                )
            knowledge_source_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(knowledge_source)").fetchall()
            }
            for column in ("effective_from", "effective_to"):
                if column not in knowledge_source_columns:
                    connection.execute(f"ALTER TABLE knowledge_source ADD COLUMN {column} TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (39, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (40, datetime('now'))"
            )
            mastery_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(mastery_state)").fetchall()
            }
            if "last_independent_correct_at" not in mastery_columns:
                connection.execute("ALTER TABLE mastery_state ADD COLUMN last_independent_correct_at TEXT")
            if "forgetting_rate" not in mastery_columns:
                connection.execute(
                    "ALTER TABLE mastery_state ADD COLUMN forgetting_rate REAL NOT NULL DEFAULT 0.2"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (41, datetime('now'))"
            )
            tutor_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tutor_turn)").fetchall()
            }
            for column in ("strategy_experiment_id", "strategy_version"):
                if column not in tutor_columns:
                    connection.execute(f"ALTER TABLE tutor_turn ADD COLUMN {column} TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (42, datetime('now'))"
            )
            model_call_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(model_call)").fetchall()
            }
            for column in (
                "input_price_cny_per_million",
                "output_price_cny_per_million",
                "estimated_cost_cny",
            ):
                if column not in model_call_columns:
                    connection.execute(f"ALTER TABLE model_call ADD COLUMN {column} REAL")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (43, datetime('now'))"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_draft (
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
                  section TEXT NOT NULL CHECK(section IN (
                    'check_in', 'classroom_recall', 'homework_capture',
                    'study', 'reflection', 'agent_command'
                  )),
                  payload_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(session_id, section)
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (44, datetime('now'))"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sensor_release_verification (
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
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sensor_release_verification_session_time
                ON sensor_release_verification(session_id, verified_at DESC)
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (45, datetime('now'))"
            )
            tutor_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tutor_turn)").fetchall()
            }
            if "output_envelope_json" not in tutor_columns:
                connection.execute("ALTER TABLE tutor_turn ADD COLUMN output_envelope_json TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (46, datetime('now'))"
            )
            recall_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(classroom_recall)").fetchall()
            }
            for column, definition in (
                ("record_kind", "TEXT NOT NULL DEFAULT 'child_statement'"),
                ("candidate_nodes_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("textbook_mapping_json", "TEXT"),
                ("evidence_sources_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if column not in recall_columns:
                    connection.execute(f"ALTER TABLE classroom_recall ADD COLUMN {column} {definition}")
            connection.execute(
                """
                UPDATE classroom_recall
                SET record_kind = CASE
                      WHEN TRIM(COALESCE(source_refs_json, '[]')) != '[]' THEN 'system_candidate'
                      ELSE 'child_statement'
                    END,
                    evidence_sources_json = CASE
                      WHEN TRIM(COALESCE(source_refs_json, '[]')) != '[]'
                        THEN '["孩子口述","历史候选来源（待复核）"]'
                      ELSE '["孩子口述"]'
                    END
                WHERE evidence_sources_json = '[]'
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (47, datetime('now'))"
            )
            deletion_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(deletion_job)").fetchall()
            }
            for column, definition in (
                ("target_type", "TEXT NOT NULL DEFAULT 'child_scope'"),
                ("target_id", "TEXT"),
                ("residual_paths_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("verification_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("error_code", "TEXT"),
                ("error_message", "TEXT"),
                ("client_action_id", "TEXT"),
                ("updated_at", "TEXT"),
            ):
                if column not in deletion_columns:
                    connection.execute(f"ALTER TABLE deletion_job ADD COLUMN {column} {definition}")
            connection.execute(
                "UPDATE deletion_job SET updated_at = COALESCE(updated_at, completed_at, created_at) WHERE updated_at IS NULL"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_deletion_job_client_action ON deletion_job(client_action_id) WHERE client_action_id IS NOT NULL"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (48, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (49, datetime('now'))"
            )
            connection.execute(
                """
                UPDATE homework_item
                SET status = 'completed'
                WHERE status != 'completed'
                  AND EXISTS (
                    SELECT 1 FROM plan_item
                    WHERE plan_item.homework_id = homework_item.homework_id
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM plan_item
                    WHERE plan_item.homework_id = homework_item.homework_id
                      AND plan_item.status != 'completed'
                  )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (50, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (51, datetime('now'))"
            )
            period_report_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(period_report)").fetchall()
            }
            if "updated_at" not in period_report_columns:
                connection.execute("ALTER TABLE period_report ADD COLUMN updated_at TEXT")
                connection.execute(
                    "UPDATE period_report SET updated_at = created_at WHERE updated_at IS NULL"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS period_report_refresh_state (
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
                  report_type TEXT NOT NULL CHECK(report_type IN ('weekly', 'monthly')),
                  period_start TEXT NOT NULL,
                  period_end TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'failed')),
                  attempts INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(child_id, report_type)
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (52, datetime('now'))"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS observation_calibration_run (
                  run_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
                  camera_system_id_hash TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('active', 'passed', 'failed', 'cancelled', 'interrupted')),
                  result_json TEXT NOT NULL DEFAULT '{}',
                  failure_code TEXT,
                  started_at TEXT NOT NULL,
                  completed_at TEXT,
                  client_action_id TEXT NOT NULL UNIQUE
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (53, datetime('now'))"
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS curriculum_corpus (
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
                CREATE TABLE IF NOT EXISTS curriculum_corpus_import_job (
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
                CREATE TABLE IF NOT EXISTS curriculum_resource (
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
                CREATE INDEX IF NOT EXISTS idx_curriculum_resource_grade_subject
                ON curriculum_resource(grade_title, subject_title, corpus_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS curriculum_resource_fts USING fts5(
                  title, description, transcript_text, unit_path, grade_title, subject_title,
                  content='curriculum_resource', content_rowid='row_id', tokenize='trigram'
                );
                CREATE TRIGGER IF NOT EXISTS curriculum_resource_ai AFTER INSERT ON curriculum_resource BEGIN
                  INSERT INTO curriculum_resource_fts(rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
                  VALUES (new.row_id, new.title, new.description, new.transcript_text, new.unit_path, new.grade_title, new.subject_title);
                END;
                CREATE TRIGGER IF NOT EXISTS curriculum_resource_ad AFTER DELETE ON curriculum_resource BEGIN
                  INSERT INTO curriculum_resource_fts(curriculum_resource_fts, rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
                  VALUES ('delete', old.row_id, old.title, old.description, old.transcript_text, old.unit_path, old.grade_title, old.subject_title);
                END;
                CREATE TRIGGER IF NOT EXISTS curriculum_resource_au AFTER UPDATE ON curriculum_resource BEGIN
                  INSERT INTO curriculum_resource_fts(curriculum_resource_fts, rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
                  VALUES ('delete', old.row_id, old.title, old.description, old.transcript_text, old.unit_path, old.grade_title, old.subject_title);
                  INSERT INTO curriculum_resource_fts(rowid, title, description, transcript_text, unit_path, grade_title, subject_title)
                  VALUES (new.row_id, new.title, new.description, new.transcript_text, new.unit_path, new.grade_title, new.subject_title);
                END;
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (54, datetime('now'))"
            )
            curriculum_resource_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(curriculum_resource)").fetchall()
            }
            if "semester" not in curriculum_resource_columns:
                connection.execute(
                    "ALTER TABLE curriculum_resource ADD COLUMN semester TEXT NOT NULL DEFAULT '未知学期'"
                )
            connection.execute(
                """
                UPDATE curriculum_resource
                SET semester = CASE
                  WHEN unit_path LIKE '上册/%' OR unit_path = '上册' THEN '上册'
                  WHEN unit_path LIKE '下册/%' OR unit_path = '下册' THEN '下册'
                  WHEN volume_title IN ('上册', '下册', '全册') THEN volume_title
                  ELSE '未知学期'
                END
                WHERE semester = '未知学期'
                """
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS curriculum_topic (
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
                CREATE INDEX IF NOT EXISTS idx_curriculum_topic_scope
                ON curriculum_topic(grade_title, subject_title, semester, maturity);
                CREATE TABLE IF NOT EXISTS curriculum_resource_topic (
                  row_id INTEGER NOT NULL REFERENCES curriculum_resource(row_id) ON DELETE CASCADE,
                  topic_id TEXT NOT NULL REFERENCES curriculum_topic(topic_id) ON DELETE CASCADE,
                  relation_type TEXT NOT NULL DEFAULT 'evidence_for' CHECK(relation_type = 'evidence_for'),
                  PRIMARY KEY(row_id, topic_id)
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (55, datetime('now'))"
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS curriculum_internalization_job (
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
                CREATE TABLE IF NOT EXISTS curriculum_internalization_batch (
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
                CREATE TABLE IF NOT EXISTS curriculum_capability (
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
                CREATE INDEX IF NOT EXISTS idx_curriculum_capability_scope
                ON curriculum_capability(grade_title, subject_title, semester, maturity);
                CREATE TABLE IF NOT EXISTS curriculum_capability_source (
                  capability_id TEXT NOT NULL REFERENCES curriculum_capability(capability_id) ON DELETE CASCADE,
                  topic_id TEXT NOT NULL REFERENCES curriculum_topic(topic_id) ON DELETE CASCADE,
                  PRIMARY KEY(capability_id, topic_id)
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (56, datetime('now'))"
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS curriculum_standard_source (
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
                CREATE TABLE IF NOT EXISTS curriculum_standard_page (
                  source_id TEXT NOT NULL REFERENCES curriculum_standard_source(source_id) ON DELETE CASCADE,
                  page_number INTEGER NOT NULL,
                  content_text TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  char_count INTEGER NOT NULL,
                  PRIMARY KEY(source_id, page_number)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS curriculum_standard_page_fts USING fts5(
                  source_id UNINDEXED, page_number UNINDEXED, content_text, tokenize='trigram'
                );
                CREATE TABLE IF NOT EXISTS curriculum_standard_requirement (
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
                CREATE INDEX IF NOT EXISTS idx_curriculum_standard_requirement_scope
                ON curriculum_standard_requirement(subject_title, stage_scope, requirement_type, maturity);
                CREATE TABLE IF NOT EXISTS curriculum_standard_topic_alignment (
                  requirement_id TEXT NOT NULL REFERENCES curriculum_standard_requirement(requirement_id) ON DELETE CASCADE,
                  topic_id TEXT NOT NULL REFERENCES curriculum_topic(topic_id) ON DELETE CASCADE,
                  coverage_type TEXT NOT NULL CHECK(coverage_type IN ('full', 'partial', 'context_only')),
                  rationale TEXT NOT NULL,
                  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                  maturity TEXT NOT NULL CHECK(maturity IN ('model_drafted', 'verified', 'conflicted', 'retired')),
                  PRIMARY KEY(requirement_id, topic_id)
                );
                CREATE TABLE IF NOT EXISTS curriculum_capability_standard (
                  capability_id TEXT NOT NULL REFERENCES curriculum_capability(capability_id) ON DELETE CASCADE,
                  requirement_id TEXT NOT NULL REFERENCES curriculum_standard_requirement(requirement_id) ON DELETE CASCADE,
                  PRIMARY KEY(capability_id, requirement_id)
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (57, datetime('now'))"
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS curriculum_topic_standard_candidate (
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
                CREATE INDEX IF NOT EXISTS idx_curriculum_topic_standard_candidate_status
                ON curriculum_topic_standard_candidate(alignment_status);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (58, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (59, datetime('now'))"
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS mistake_capture_candidate (
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
                CREATE INDEX IF NOT EXISTS idx_mistake_capture_candidate_artifact
                ON mistake_capture_candidate(artifact_id, question_index);
                CREATE TABLE IF NOT EXISTS mistake_capture_candidate_event (
                  event_id TEXT PRIMARY KEY,
                  candidate_id TEXT NOT NULL REFERENCES mistake_capture_candidate(candidate_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  event_type TEXT NOT NULL CHECK(event_type IN ('prepared', 'edited', 'skipped', 'captured')),
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (60, datetime('now'))"
            )
            tutor_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tutor_turn)").fetchall()
            }
            for column in ("tutor_request_hash", "ocr_confirmation_id"):
                if column not in tutor_columns:
                    connection.execute(f"ALTER TABLE tutor_turn ADD COLUMN {column} TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (61, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (62, datetime('now'))"
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS child_voice_calibration (
                  child_id TEXT PRIMARY KEY REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  consented_at TEXT NOT NULL,
                  profile_json TEXT NOT NULL,
                  vocabulary_json TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('ready')),
                  updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (63, datetime('now'))"
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(child_voice_calibration)").fetchall()}
            if "microphone_json" not in columns:
                connection.execute("ALTER TABLE child_voice_calibration ADD COLUMN microphone_json TEXT")
            connection.execute("INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (64, datetime('now'))")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS voice_calibration_save_action (
                  client_action_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  payload_hash TEXT NOT NULL,
                  saved_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_voice_calibration_save_action_child
                ON voice_calibration_save_action(child_id, saved_at);
                CREATE TABLE IF NOT EXISTS voice_transcription_action (
                  client_action_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  status TEXT NOT NULL CHECK(status IN ('cancelled', 'completed', 'ignored')),
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_voice_transcription_action_session
                ON voice_transcription_action(session_id, updated_at);
                """
            )
            connection.execute("INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (65, datetime('now'))")
            # Knowledge-graph tables are part of the same durable family
            # database.  Install/upgrade them while Database.initialize still
            # has its pre-upgrade backup and recovery contract; do not defer a
            # potentially destructive candidate-table migration until the first
            # request reaches a KG route.
            from .knowledge_graph.schema import ensure_schema as ensure_knowledge_graph_schema

            ensure_knowledge_graph_schema(connection, connection.execute("SELECT datetime('now')").fetchone()[0])
            # Reserved main-schema migration for the KG bootstrap.  Versions
            # 62-65 belong to the behavior/voice workstreams, so do not reuse
            # them even though kg_schema_migration has its own internal version.
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (66, datetime('now'))"
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS observation_qualification (
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
                CREATE INDEX IF NOT EXISTS idx_observation_qualification_child_time
                ON observation_qualification(child_id, updated_at DESC);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (67, datetime('now'))"
            )
            # Durable agent runtime backbone.  The same DDL is also present in
            # SCHEMA for new databases; this block upgrades existing families
            # under the normal pre-upgrade backup and integrity contract.
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_objective (
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
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_one_active_objective
                ON agent_objective(child_id) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_agent_objective_session_time
                ON agent_objective(session_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS agent_plan (
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
                CREATE TABLE IF NOT EXISTS agent_step (
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
                CREATE INDEX IF NOT EXISTS idx_agent_step_plan_status
                ON agent_step(plan_id, status, step_index);
                CREATE TABLE IF NOT EXISTS agent_outcome (
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
                CREATE TABLE IF NOT EXISTS agent_commitment (
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
                CREATE INDEX IF NOT EXISTS idx_agent_commitment_child_due
                ON agent_commitment(child_id, status, due_at);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (68, datetime('now'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (69, datetime('now'))"
            )
            from .diagnostic_history_identity import ensure_schema as ensure_diagnostic_history
            ensure_diagnostic_history(connection)
            from .runtime_data_scope import initialize_scope
            initialize_scope(connection, self.runtime_mode)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (79,datetime('now'))")
            from .voice_capture_schema import ensure_schema as ensure_voice_capture_schema
            ensure_voice_capture_schema(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (80,datetime('now'))")
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (81,datetime('now'))")
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (82,datetime('now'))")
            from .managed_local_schema import ensure_schema as ensure_managed_local_schema
            ensure_managed_local_schema(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (83,datetime('now'))")
            from .parent_restore_assets import ensure_schema as ensure_restore_asset_schema
            ensure_restore_asset_schema(self, connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (85,datetime('now'))")
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (86,datetime('now'))")
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (87,datetime('now'))")
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (88,datetime('now'))")
            from .pet_speaking_candidate_store import ensure_schema as ensure_speaking_model_candidates
            ensure_speaking_model_candidates(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (89,datetime('now'))")
            from .pet_speaking_candidate_jobs import ensure_schema as ensure_speaking_candidate_jobs
            ensure_speaking_candidate_jobs(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (90,datetime('now'))")
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (91,datetime('now'))")
            from .pet_multipa_execution_receipt import ensure_schema as ensure_cpu_execution_receipts
            ensure_cpu_execution_receipts(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (92,datetime('now'))")
            from .document_observation_segments import SEGMENT_SCHEMA_SQL
            from .document_monitoring_schema import ensure_document_monitor_parents
            ensure_document_monitor_parents(connection)
            connection.execute(SEGMENT_SCHEMA_SQL)
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_monitor_session'").fetchone():
                columns = {row['name'] for row in connection.execute('PRAGMA table_info(document_monitor_session)')}
                if 'generation' not in columns:
                    connection.execute('ALTER TABLE document_monitor_session ADD COLUMN generation INTEGER NOT NULL DEFAULT 0')
                if 'generation_started_at' not in columns:
                    # An old active generation has no trustworthy start; leave
                    # it unknown until start/resume creates a new generation.
                    connection.execute('ALTER TABLE document_monitor_session ADD COLUMN generation_started_at TEXT')
            revision_columns = {row['name'] for row in connection.execute('PRAGMA table_info(document_page_revision)')}
            if 'study_signal_json' not in revision_columns:
                connection.execute("ALTER TABLE document_page_revision ADD COLUMN study_signal_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (93,datetime('now'))")
            from .daily_report_observation import SOURCE_SCHEMA, OUTBOX_SCHEMA
            connection.execute(SOURCE_SCHEMA)
            connection.execute(OUTBOX_SCHEMA)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (94,datetime('now'))")
            from .daily_report_frozen_snapshot import SNAPSHOT_SCHEMA
            connection.execute(SNAPSHOT_SCHEMA)
            from .daily_report_email_verification import EMAIL_VERIFICATION_SCHEMA
            connection.execute(EMAIL_VERIFICATION_SCHEMA)
            from .daily_report_delivery_state import LEDGER_SCHEMA, BARRIER_SCHEMA, initialize_barrier
            connection.execute(LEDGER_SCHEMA)
            connection.execute(BARRIER_SCHEMA)
            initialize_barrier(connection)
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (95,datetime('now'))")
            from .daily_report_visual_schedule import SCHEDULE_SCHEMA
            connection.execute(SCHEDULE_SCHEMA)
            from .daily_report_combined_schema import PILOT_BASE_SCHEMA, PILOT_CONSENT_SCHEMA, BATCH_SCHEMA, INDEX_SCHEMA, DELIVERY_SCHEMA, schema_valid as combined_schema_valid
            from .pilot_test_telemetry import JOINT_TEST_MEMBER_SCHEMA
            from .daily_report_combined_snapshot import CANDIDATE_SCHEMA
            connection.execute(PILOT_BASE_SCHEMA)
            connection.execute(PILOT_CONSENT_SCHEMA)
            connection.execute(BATCH_SCHEMA)
            pilot_columns = {row['name'] for row in connection.execute('PRAGMA table_info(pilot_test_digest_outbox)')}
            if 'combined_batch_id' not in pilot_columns:
                connection.execute('ALTER TABLE pilot_test_digest_outbox ADD COLUMN combined_batch_id TEXT REFERENCES combined_diagnostic_batch(batch_id)')
            consent_columns = {row['name'] for row in connection.execute('PRAGMA table_info(pilot_diagnostic_recipient_consent)')}
            if 'authority_generation' not in consent_columns:
                connection.execute('ALTER TABLE pilot_diagnostic_recipient_consent ADD COLUMN authority_generation TEXT')
                # Preserve explicit existing consent/revocation; never infer it
                # from historic mail receipts. Each existing row gets an epoch.
                connection.execute("UPDATE pilot_diagnostic_recipient_consent SET authority_generation=lower(hex(randomblob(16)))")
            connection.execute(INDEX_SCHEMA)
            connection.execute(JOINT_TEST_MEMBER_SCHEMA.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1))
            connection.execute(CANDIDATE_SCHEMA.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1))
            connection.execute(DELIVERY_SCHEMA)
            from .daily_report_visual_schedule import _valid_schema as visual_schedule_schema_valid
            if not visual_schedule_schema_valid(connection) or not combined_schema_valid(connection):
                raise sqlite3.DatabaseError('COMBINED_REPORT_V96_SCHEMA_INVALID')
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (96,datetime('now'))")
            from .daily_report_combined_mode import SCHEMA as COMBINED_MODE_SCHEMA, schema_valid as combined_mode_schema_valid
            connection.execute(COMBINED_MODE_SCHEMA)
            connection.execute("INSERT OR IGNORE INTO combined_daily_mode VALUES(1,'legacy','',datetime('now'))")
            if not combined_mode_schema_valid(connection):
                raise sqlite3.DatabaseError('COMBINED_MODE_V97_SCHEMA_INVALID')
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (97,datetime('now'))")
            from .smtp_owned_journal import SCHEMA as SMTP_WORKER_SCHEMA, schema_valid as smtp_journal_schema_valid
            connection.execute(SMTP_WORKER_SCHEMA)
            if not smtp_journal_schema_valid(connection):
                raise sqlite3.DatabaseError('SMTP_WORKER_V98_SCHEMA_INVALID')
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (98,datetime('now'))")
            from .smtp_owned_journal import BINDING_SCHEMA, MEMBER_SCHEMA, schema_valid_v99
            connection.execute(BINDING_SCHEMA)
            connection.execute(MEMBER_SCHEMA)
            if not schema_valid_v99(connection):
                raise sqlite3.DatabaseError('SMTP_WORKER_V99_SCHEMA_INVALID')
            connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (99,datetime('now'))")
            connection.commit()
            from .diagnostic_score_migration import migrate_unknown_diagnostic_scores
            migrate_unknown_diagnostic_scores(connection)
            from .mistake_score_migration import migrate_unknown_mistake_scores
            migrate_unknown_mistake_scores(connection)

    def latest_upgrade_status(self) -> dict | None:
        backup_dir = self.path.parent / "upgrade-backups"
        metadata_paths = sorted(
            backup_dir.glob("pre-upgrade-*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ) if backup_dir.is_dir() else []
        for metadata_path in metadata_paths:
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if metadata.get("format") != UPGRADE_BACKUP_FORMAT:
                continue
            backup_file = metadata.get("backup_file")
            return {
                "status": metadata.get("status", "unknown"),
                "source_schema_version": metadata.get("source_schema_version"),
                "target_schema_version": metadata.get("target_schema_version"),
                "created_at": metadata.get("created_at"),
                "completed_at": metadata.get("completed_at"),
                "error_type": metadata.get("error_type"),
                "snapshot_available": bool(
                    isinstance(backup_file, str) and (backup_dir / backup_file).is_file()
                ),
            }
        return None

    def purge_upgrade_snapshots(self) -> int:
        backup_dir = self.path.parent / "upgrade-backups"
        if not backup_dir.is_dir():
            return 0
        removed = 0
        for path in backup_dir.glob("pre-upgrade-*"):
            if not path.is_file():
                continue
            path.unlink()
            removed += 1
        try:
            backup_dir.rmdir()
        except OSError:
            pass
        return removed

    def _prepare_upgrade_snapshot(self) -> dict | None:
        source_version = self._existing_schema_version()
        if source_version is None:
            return None
        if source_version > LATEST_SCHEMA_VERSION:
            raise UnsupportedDatabaseVersionError(
                f"数据库版本 {source_version} 高于当前应用支持的 {LATEST_SCHEMA_VERSION}，已拒绝降级打开。"
            )
        if source_version == LATEST_SCHEMA_VERSION:
            from .voice_capture_schema import needs_upgrade as capture_needs_upgrade
            from .managed_local_schema import needs_upgrade as local_needs_upgrade
            from .daily_report_observation import schema_valid as daily_observation_schema_valid
            from .daily_report_frozen_snapshot import schema_valid as frozen_observation_schema_valid
            from .daily_report_email_verification import schema_valid as email_verification_schema_valid
            from .daily_report_delivery_state import schema_valid as daily_delivery_state_schema_valid
            from .daily_report_visual_schedule import _valid_schema as daily_visual_schedule_schema_valid
            from .daily_report_combined_schema import schema_valid as combined_schema_valid
            from .daily_report_combined_mode import schema_valid as combined_mode_schema_valid
            from .smtp_owned_journal import schema_valid as smtp_journal_schema_valid
            from .smtp_owned_journal import schema_valid_v99
            with self.connect() as connection:
                if (not capture_needs_upgrade(connection) and not local_needs_upgrade(connection)
                        and _observation_v93_schema_valid(connection)
                        and daily_observation_schema_valid(connection)
                        and frozen_observation_schema_valid(connection)
                        and email_verification_schema_valid(connection)
                        and daily_delivery_state_schema_valid(connection)
                        and daily_visual_schedule_schema_valid(connection)
                        and combined_schema_valid(connection)
                        and combined_mode_schema_valid(connection)
                        and smtp_journal_schema_valid(connection)
                        and schema_valid_v99(connection)):
                    return None

        backup_dir = self.path.parent / "upgrade-backups"
        from .parent_restore_ownership import staged_capability
        work = staged_capability(self)
        if work is not None:work.directory(backup_dir)
        else:backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        stem = f"pre-upgrade-v{source_version}-to-v{LATEST_SCHEMA_VERSION}-{stamp}-{str(uuid4())[:8]}"
        snapshot_path = backup_dir / f"{stem}.db"
        metadata_path = backup_dir / f"{stem}.json"
        if work is not None:work.upgrade([snapshot_path,metadata_path])
        from .restore_publication import _sql,_gate
        original_owner=self._restore_sql_owner()
        try:
            with _sql(self.path,owner=original_owner,timeout=5)as source, _sql(snapshot_path,owner=original_owner,timeout=5)as target:
                source.backup(target)
                integrity=target.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity!="ok":raise sqlite3.DatabaseError('pre-upgrade snapshot integrity failed')
        except Exception:
            if work is None and not _gate(self.path).poisoned:snapshot_path.unlink(missing_ok=True)
            raise
        metadata = {
            "format": UPGRADE_BACKUP_FORMAT,
            "status": "prepared",
            "source_schema_version": source_version,
            "target_schema_version": LATEST_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "backup_file": snapshot_path.name,
            "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            "size_bytes": snapshot_path.stat().st_size,
            "error_type": None,
        }
        self._write_upgrade_metadata(metadata_path, metadata)
        return {"snapshot_path": snapshot_path, "metadata_path": metadata_path, "metadata": metadata}

    def _restore_upgrade_snapshot(self, snapshot: dict, error: Exception) -> None:
        from .restore_publication import publication_owner, recover_before_initialize, connection_admission, _database_identity, RestorePublicationError, _sql, _gate, _close_sql
        snapshot_path = Path(snapshot["snapshot_path"])
        restore_path = self.path.with_name(f".{self.path.name}.upgrade-restore-{uuid4()}.tmp")
        from .parent_restore_ownership import staged_capability
        work = staged_capability(self)
        if work is not None:work.upgrade([restore_path])
        with publication_owner(self) as owner:
            recover_before_initialize(self)
            expected = owner.gate.database_identity
            if expected is None or _database_identity(self.path, owner.parent, owner.gate) != expected:
                raise RestorePublicationError('RESTORE_PUBLICATION_DATABASE_ALIAS')
            try:
                # Validate a private offline copy before touching the original
                # live SQLite file. Missing snapshots must never be recreated.
                with _sql(snapshot_path,owner=self._restore_sql_owner(),readonly=True,timeout=5) as source:
                    with _sql(restore_path,owner=self._restore_sql_owner(),timeout=5) as target:
                        source.backup(target)
                        if target.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                            raise sqlite3.DatabaseError('private rollback journal mode unconfirmed')
                        integrity = target.execute('PRAGMA integrity_check').fetchone()[0]
                        if integrity != 'ok':
                            raise sqlite3.DatabaseError(f'restored snapshot integrity failed: {integrity}')
                with _sql(restore_path,owner=self._restore_sql_owner(),readonly=True,timeout=5) as source:
                    source.execute('PRAGMA query_only=ON')
                    with connection_admission(self.path) as admission:
                        # Keep the live file/inode and SQLite-owned WAL intact.
                        original_owner=self._restore_sql_owner()
                        target = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=0)
                        try:
                            target.execute('PRAGMA synchronous=FULL')
                            def progress(status, remaining, total):
                                if status in (5, 6):  # SQLITE_BUSY/LOCKED, including Python3.9.
                                    raise sqlite3.OperationalError('upgrade rollback target busy')
                            source.backup(target, pages=256, progress=progress)
                            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                                raise sqlite3.DatabaseError('restored live database integrity failed')
                            if _database_identity(self.path, owner.parent, owner.gate) != expected:
                                raise RestorePublicationError('RESTORE_PUBLICATION_DATABASE_ALIAS')
                        finally:
                            try:
                                _close_sql(target,path=self.path,owner=original_owner)
                            except BaseException:
                                admission.poison(target)
                                raise
            finally:
                # Only this UUID-named private workspace, after its SQL handles
                # closed. Never unlink the live database's WAL/SHM.
                if work is None and not _gate(self.path).poisoned:
                    for temporary in [restore_path, *[Path(str(restore_path) + suffix) for suffix in ('-wal', '-shm', '-journal')]]:
                        temporary.unlink(missing_ok=True)
            metadata = dict(snapshot['metadata'])
            metadata.update({
                'status': 'restored_after_failure',
                'completed_at': datetime.now(timezone.utc).isoformat(),
                'error_type': type(error).__name__,
            })
            self._write_upgrade_metadata(Path(snapshot['metadata_path']), metadata)

    def _complete_upgrade_snapshot(self, snapshot: dict) -> None:
        metadata = dict(snapshot["metadata"])
        metadata.update({
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "error_type": None,
        })
        self._write_upgrade_metadata(Path(snapshot["metadata_path"]), metadata)

    def _prune_upgrade_snapshots(self, keep: int = 3) -> None:
        backup_dir = self.path.parent / "upgrade-backups"
        if not backup_dir.is_dir():
            return
        metadata_paths = sorted(
            backup_dir.glob("pre-upgrade-*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for metadata_path in metadata_paths[max(1, keep):]:
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                backup_file = metadata.get("backup_file")
                if isinstance(backup_file, str):
                    (backup_dir / backup_file).unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                pass
            metadata_path.unlink(missing_ok=True)

    def _existing_schema_version(self) -> int | None:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return None
        from .restore_publication import _cold
        original_owner=self._restore_sql_owner()
        with _cold(self.path, owner=original_owner) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not tables:
                return None
            if "schema_migration" not in tables:
                return 0
            row = connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()
            return int(row[0] or 0)

    def _integrity_check(self, path: Path) -> str:
        from .restore_publication import _close_sql
        original_owner=self._restore_sql_owner()
        connection = sqlite3.connect(path)
        try:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            _close_sql(connection,path=path,owner=original_owner)

    def _write_upgrade_metadata(self, path: Path, metadata: dict) -> None:
        from .parent_restore_ownership import staged_capability
        work = staged_capability(self)
        if work is not None:
            work.metadata(path, metadata);return
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)

    def begin_runtime(self) -> tuple[str, bool]:
        run_id = str(uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT clean_shutdown, run_id, started_at FROM runtime_state WHERE singleton_id = 1"
            ).fetchone()
            unclean_restart = bool(row and row["clean_shutdown"] == 0)
            if unclean_restart:
                # Record the recovery transition, not an invented user pause or
                # crash time. Only previously checkpointed learning time survives.
                # Camera leases are expired during application recovery, but the
                # child's saved observation preference remains authoritative so
                # an enabled camera can be reopened automatically after startup.
                connection.execute(
                    """
                    INSERT INTO plan_item_event(
                        event_id, plan_item_id, event_type, occurred_at, payload_json, client_action_id
                    )
                    SELECT lower(hex(randomblob(16))), plan_item_id, 'paused', ?,
                           json_object('reason', 'unclean_restart', 'source', 'system',
                                       'runtime_run_id', ?, 'recovery_run_id', ?,
                                       'run_started_at', ?, 'active_segment_started_at', active_segment_started_at,
                                       'uncheckpointed_duration_unknown', json('true')),
                           'runtime-recovery:' || ? || ':' || plan_item_id
                    FROM plan_item WHERE status = 'active'
                    """,
                    (datetime.now(timezone.utc).isoformat(), row['run_id'], run_id, row['started_at'], run_id),
                )
                connection.execute(
                    """
                    UPDATE plan_item SET status = 'paused', active_segment_started_at = NULL
                    WHERE status = 'active'
                    """
                )
                connection.execute(
                    """
                    UPDATE sensor_lease
                    SET status = 'released', released_at = datetime('now'),
                        release_reason = 'unclean_restart'
                    WHERE status = 'active'
                    """
                )
            connection.execute(
                """
                UPDATE runtime_state
                SET clean_shutdown = 0, run_id = ?, started_at = datetime('now'), ended_at = NULL
                WHERE singleton_id = 1
                """,
                (run_id,),
            )
            connection.commit()
        return run_id, unclean_restart

    def end_runtime(self, run_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT run_id, started_at FROM runtime_state WHERE singleton_id = 1"
            ).fetchone()
            if not current or current["run_id"] != run_id:
                connection.rollback()
                return
            connection.execute(
                """
                UPDATE sensor_lease
                SET status = 'released', released_at = datetime('now'),
                    release_reason = 'app_shutdown'
                WHERE status = 'active'
                """
            )
            connection.execute(
                """
                INSERT INTO plan_item_event(
                    event_id, plan_item_id, event_type, occurred_at, payload_json, client_action_id
                )
                SELECT lower(hex(randomblob(16))), plan_item_id, 'paused', ?,
                       json_object('reason', 'runtime_shutdown', 'runtime_run_id', ?, 'source', 'system',
                                   'run_started_at', ?, 'active_segment_started_at', active_segment_started_at),
                       'runtime-shutdown:' || ? || ':' || plan_item_id
                FROM plan_item
                WHERE status = 'active' AND active_segment_started_at IS NOT NULL
                """,
                (datetime.now(timezone.utc).isoformat(), run_id, current['started_at'], run_id),
            )
            connection.execute(
                """
                UPDATE plan_item
                SET active_seconds = active_seconds + MAX(
                      0,
                      CAST(strftime('%s', 'now') AS INTEGER) -
                      CAST(strftime('%s', active_segment_started_at) AS INTEGER)
                    ),
                    status = 'paused', active_segment_started_at = NULL
                WHERE status = 'active' AND active_segment_started_at IS NOT NULL
                """
            )
            connection.execute(
                """
                UPDATE runtime_state
                SET clean_shutdown = 1, ended_at = datetime('now')
                WHERE singleton_id = 1 AND run_id = ?
                """,
                (run_id,),
            )
            connection.commit()

    def _restore_sql_owner(self):
        from . import restore_publication as pub
        from .parent_restore_ownership import WorkCapability
        work=getattr(self,'_restore_work_capability',None)
        if work is not None:
            pub._need(type(work)is WorkCapability and self.path==work.work/'database.db','RESTORE_PUBLICATION_OWNER_REQUIRED')
            return pub._owner(work.owner)
        owner=pub._gate(self.path).owner
        return pub._owner(owner)if pub._same_actor(owner,pub._actor())else None

    @contextmanager
    def connect(self, *, rollback_on_error: bool = False) -> Iterator[sqlite3.Connection]:
        from .restore_publication import connection_admission
        original_owner=self._restore_sql_owner()
        with connection_admission(self.path) as admission:
            connection = sqlite3.connect(self.path, timeout=10, factory=StudyConnection)
            connection.native_database_path = str(self.path.resolve())
            connection.local_media_runtime = self.local_media_runtime
            connection.local_semantic_matcher = self.local_semantic_matcher
            connection.row_factory = sqlite3.Row
            self._original_connections[id(connection)] = connection
            try:
                from .runtime_data_scope import assert_scope
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name IN ('schema_migration','runtime_data_scope') LIMIT 1").fetchone():
                    assert_scope(connection, self.runtime_mode)
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = WAL")
                from .original_connection_read import _enroll, _retire
                read_ticket = _enroll(self, connection, admission)
                try:
                    try:
                        yield connection
                    except BaseException as migration_error:
                        # A failed migration can leave an active write
                        # transaction while its traceback still owns a cursor.
                        # SQLite close_v2 may then defer releasing Windows locks;
                        # roll back the original transaction before close so
                        # restoring the pre-upgrade snapshot does not self-block.
                        if rollback_on_error and connection.in_transaction:
                            try:
                                connection.rollback()
                            except BaseException as rollback_error:
                                admission.poison(connection)
                                if original_owner is not None:
                                    original_owner.gate.poisoned = True
                                    original_owner.gate.retained.append(('migration_rollback', connection, migration_error, rollback_error))
                                raise
                        raise
                finally:
                    _retire(read_ticket, connection)
            finally:
                try:
                    connection.close()
                except BaseException:
                    admission.poison(connection)
                    if original_owner is not None:
                        original_owner.gate.poisoned=True;original_owner.gate.retained.append(('original_sql_connection',connection))
                    raise
                else:
                    del self._original_connections[id(connection)]
