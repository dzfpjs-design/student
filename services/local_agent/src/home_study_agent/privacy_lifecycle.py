from __future__ import annotations
from .backup_local_io import io_path, io_resolve, io_walk, io_glob, io_mkdtemp, io_temporary_directory

from datetime import datetime, timedelta, timezone
from contextlib import ExitStack
from functools import wraps
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
from uuid import uuid4
import zipfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .database import Database, ExternalEffectBusyError
from .repository import UnknownChildError, utc_now
from .rubric_calibration_authority import rows as calibration_authority_rows, delete_for_child as delete_calibration_authorities
from .visual_material_lifecycle import rows as visual_material_rows, count as visual_material_count


DEFAULT_PREFERENCES = {
    "cloud_model_text_allowed": True,
    "cloud_audio_allowed": False,
    "raw_media_storage_allowed": False,
    "model_improvement_allowed": False,
    "model_call_retention_days": 90,
    "tutor_turn_retention_days": 180,
    "report_retention_days": 730,
}

BACKUP_REASONS = {"manual", "session_completed", "daily_automatic", "restore_recovery_point", "post_deletion"}
PORTABLE_BACKUP_MAGIC = b"HSCB1\0"
PORTABLE_BACKUP_AAD = b"hsc-portable-backup-v1"
DELETION_SCOPES = {"learning", "reports", "tutor", "media", "knowledge", "all_history"}
INTERRUPTED_DELETION_STATUSES = {"requested", "scanning", "deleting", "verifying"}
DELETION_ADMISSION_TIMEOUT_SECONDS = 10.0
_COMBINED_PRIVACY_TOMBSTONE = 'COMBINED_PRIVACY_TOMBSTONE'


def _combined_pilot_identity(row):
    from datetime import date
    day, address = row['delivery_date'], row['recipient']
    try:
        if not isinstance(day,str) or date.fromisoformat(day).isoformat()!=day: raise ValueError()
    except ValueError:
        raise ValueError('COMBINED_PRIVACY_IDENTITY_UNCONFIRMED') from None
    if not isinstance(address,str) or not address.strip() or '@' not in address or len(address)>320:
        raise ValueError('COMBINED_PRIVACY_IDENTITY_UNCONFIRMED')
    return day,address.strip().casefold()


def _combined_privacy_batches(c, *, child_id=None):
    """Erase shared derived payloads, preserving an irreversible mail tombstone.

    Caller owns the guard and transaction. Malformed/partial ledgers stop the
    whole operation before committing rather than guessing batch ownership.
    """
    if not c.in_transaction: raise ValueError('COMBINED_PRIVACY_TRANSACTION_REQUIRED')
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'combined_diagnostic_batch' not in tables:
        if tables.intersection({'combined_diagnostic_member','combined_daily_snapshot','combined_daily_delivery'}):
            raise ValueError('COMBINED_PRIVACY_SCHEMA_UNCONFIRMED')
        return
    if not c.execute('SELECT 1 FROM combined_diagnostic_batch LIMIT 1').fetchone():
        for name in ('combined_diagnostic_member','combined_daily_snapshot','combined_daily_delivery'):
            if name in tables and c.execute(f'SELECT 1 FROM {name} LIMIT 1').fetchone():
                raise ValueError('COMBINED_PRIVACY_SCHEMA_UNCONFIRMED')
        return
    required={'combined_diagnostic_member','combined_daily_snapshot','combined_daily_delivery','pilot_test_digest_outbox','daily_observation_delivery_ledger','daily_observation_delivery_barrier'}
    if not required.issubset(tables): raise ValueError('COMBINED_PRIVACY_SCHEMA_UNCONFIRMED')
    from .pilot_test_telemetry import PilotTestTelemetryService
    from .daily_report_combined_snapshot import _schema as snapshot_schema
    from .daily_report_combined_delivery import schema_valid as delivery_schema
    PilotTestTelemetryService._require_combined_schema(c)
    PilotTestTelemetryService._joint_members_schema(c)
    snapshot_schema(c)
    if not delivery_schema(c): raise ValueError('COMBINED_PRIVACY_SCHEMA_UNCONFIRMED')
    if c.execute('''SELECT 1 FROM combined_diagnostic_batch b LEFT JOIN combined_diagnostic_member m ON m.batch_id=b.batch_id
        GROUP BY b.batch_id HAVING b.sealed!=1 OR COUNT(m.child_id)!=b.expected_count LIMIT 1''').fetchone():
        raise ValueError('COMBINED_PRIVACY_MEMBERS_UNCONFIRMED')
    if c.execute('''SELECT 1 FROM combined_daily_snapshot s LEFT JOIN combined_diagnostic_member m
        ON m.batch_id=s.batch_id AND m.child_id=s.child_id WHERE m.child_id IS NULL LIMIT 1''').fetchone():
        raise ValueError('COMBINED_PRIVACY_MEMBERS_UNCONFIRMED')
    sql='SELECT b.batch_id FROM combined_diagnostic_batch b'
    args=()
    if child_id is not None:
        sql+=' JOIN combined_diagnostic_member m ON m.batch_id=b.batch_id WHERE m.child_id=?'
        args=(child_id,)
    batches=[r[0] for r in c.execute(sql,args)]
    if not batches: return
    c.execute("UPDATE daily_observation_delivery_barrier SET blocked=1,reason='DELIVERY_HISTORY_ERASED' WHERE singleton_id=1")
    if c.execute('SELECT changes()').fetchone()[0]!=1: raise ValueError('COMBINED_PRIVACY_BARRIER_UNCONFIRMED')
    for batch in batches:
        pilot=c.execute('SELECT * FROM pilot_test_digest_outbox WHERE combined_batch_id=?',(batch,)).fetchone()
        if not pilot: raise ValueError('COMBINED_PRIVACY_IDENTITY_UNCONFIRMED')
        day,address=_combined_pilot_identity(pilot)
        aliases=[r for r in c.execute('SELECT delivery_date,recipient FROM pilot_test_digest_outbox WHERE delivery_date=?',(day,))
                 if isinstance(r['recipient'],str) and r['recipient'].strip().casefold()==address]
        if len(aliases)!=1: raise ValueError('COMBINED_PRIVACY_IDENTITY_UNCONFIRMED')
        c.execute('''UPDATE daily_observation_delivery_ledger SET status=CASE WHEN status='sent' THEN 'sent' ELSE 'unknown' END
            WHERE identity IN (SELECT identity FROM combined_daily_delivery WHERE batch_id=?)''',(batch,))
        c.execute('DELETE FROM combined_daily_delivery WHERE batch_id=?',(batch,))
        c.execute('DELETE FROM combined_daily_snapshot WHERE batch_id=?',(batch,))
        c.execute('DELETE FROM combined_diagnostic_member WHERE batch_id=?',(batch,))
        c.execute('''UPDATE pilot_test_digest_outbox SET status='unknown',error_code=?,combined_batch_id=NULL,
            subject=NULL,body=NULL,attachment_filename=NULL,attachment_content=NULL,
            attachment_manifest_json=NULL,window_policy_json=NULL,submission_token=NULL,claim_token=NULL
            WHERE combined_batch_id=?''',(_COMBINED_PRIVACY_TOMBSTONE,batch))
        c.execute('DELETE FROM combined_diagnostic_batch WHERE batch_id=?',(batch,))


def _preserve_combined_pilot_tombstones(live, staged):
    """Carry live unknown identities over restore without copying ZIP or text."""
    from .pilot_test_telemetry import PilotTestTelemetryService
    if not staged.in_transaction: raise ValueError('COMBINED_PRIVACY_TRANSACTION_REQUIRED')
    if not live.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pilot_test_digest_outbox'").fetchone(): return
    columns={r[1] for r in live.execute('PRAGMA table_info(pilot_test_digest_outbox)')}
    if 'combined_batch_id' not in columns: return
    if live.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_diagnostic_batch'").fetchone() and live.execute('SELECT 1 FROM combined_diagnostic_batch LIMIT 1').fetchone():
        from .daily_report_combined_snapshot import _schema as snapshot_schema
        from .daily_report_combined_delivery import schema_valid as delivery_schema
        PilotTestTelemetryService._require_combined_schema(live)
        PilotTestTelemetryService._joint_members_schema(live)
        snapshot_schema(live)
        if not delivery_schema(live) or live.execute('''SELECT 1 FROM combined_diagnostic_batch b LEFT JOIN combined_diagnostic_member m ON m.batch_id=b.batch_id
            GROUP BY b.batch_id HAVING b.sealed!=1 OR COUNT(m.child_id)!=b.expected_count LIMIT 1''').fetchone():
            raise ValueError('COMBINED_PRIVACY_MEMBERS_UNCONFIRMED')
    rows=live.execute('''SELECT * FROM pilot_test_digest_outbox WHERE
        (combined_batch_id IS NOT NULL AND status IN ('processing','unknown','sent')) OR error_code IN (?,?)''',
        (_COMBINED_PRIVACY_TOMBSTONE,PilotTestTelemetryService._COMBINED_RESERVATION)).fetchall()
    identities=[_combined_pilot_identity(row) for row in rows]
    if len(set(identities))!=len(identities): raise ValueError('COMBINED_PRIVACY_IDENTITY_UNCONFIRMED')
    for row,(day,address) in zip(rows,identities):
        reserved=row['error_code']==PilotTestTelemetryService._COMBINED_RESERVATION
        if reserved and not PilotTestTelemetryService._is_combined_reservation(row,address):
            raise ValueError('COMBINED_PRIVACY_IDENTITY_UNCONFIRMED')
        matches=[r for r in staged.execute('SELECT * FROM pilot_test_digest_outbox WHERE delivery_date=?',(day,))
                 if isinstance(r['recipient'],str) and r['recipient'].strip().casefold()==address]
        if len(matches)>1: raise ValueError('COMBINED_PRIVACY_IDENTITY_UNCONFIRMED')
        if matches:
            match=matches[0]
            if reserved and PilotTestTelemetryService._is_combined_reservation(match,address):
                staged.execute("UPDATE pilot_test_digest_outbox SET status='unknown',error_code=?,updated_at=? WHERE delivery_date=? AND recipient=?",
                               (_COMBINED_PRIVACY_TOMBSTONE,utc_now(),day,address))
            elif match['error_code']!=_COMBINED_PRIVACY_TOMBSTONE or match['combined_batch_id'] is not None:
                raise ValueError('COMBINED_PRIVACY_IDENTITY_UNCONFIRMED')
        else:
            now=utc_now()
            staged.execute('''INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,error_code,created_at,updated_at)
                VALUES(?,?,'unknown',0,?,?,?)''',(day,address,_COMBINED_PRIVACY_TOMBSTONE,now,now))


class DeletionAdmissionBusyError(Exception):
    """This invocation did not acquire the lock or begin a deletion."""


def _serialized_deletion(function):
    """Serialize admission with external submission, including action lookup.

    Catch only acquisition failures: errors after entry may have committed a
    deletion job, and must never be described as a fresh, unstarted request.
    """
    @wraps(function)
    def run(self, *args, **kwargs):
        with ExitStack() as stack:
            try:
                stack.enter_context(self.database.external_effect_guard(timeout=DELETION_ADMISSION_TIMEOUT_SECONDS))
            except ExternalEffectBusyError as error:
                raise DeletionAdmissionBusyError() from error
            return function(self, *args, **kwargs)
    return run


class PrivacyLifecycleService:
    def __init__(self, database: Database, data_dir: Path, data_encryption_key: bytes | None = None):
        self.database = database
        self.data_dir = data_dir
        self.data_encryption_key = data_encryption_key
        self.reconcile_source_deletion = None
        self.replay_source_mastery = None
        self.clear_ephemeral_child = None
        self._deletion_trace: dict[str, dict[str, str]] = {}

    def initialize(self) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            from .privacy_operation_receipt import initialize as initialize_operations
            initialize_operations(connection)
            from .privacy_source_withdrawal import ensure_schema as ensure_source_withdrawal_schema
            ensure_source_withdrawal_schema(connection)
            connection.execute(
                """
                INSERT OR IGNORE INTO privacy_preference(
                  singleton_id, cloud_model_text_allowed, cloud_audio_allowed,
                  raw_media_storage_allowed, model_improvement_allowed,
                  model_call_retention_days, tutor_turn_retention_days,
                  report_retention_days, updated_at
                ) VALUES (1, 1, 0, 0, 0, 90, 180, 730, ?)
                """,
                (now,),
            )
            connection.commit()

    def preferences(self) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM privacy_preference WHERE singleton_id = 1").fetchone()
        result = dict(row)
        result.pop("singleton_id", None)
        for key in (
            "cloud_model_text_allowed",
            "cloud_audio_allowed",
            "raw_media_storage_allowed",
            "model_improvement_allowed",
        ):
            result[key] = bool(result[key])
        return result

    def update_preferences(self, values: dict) -> dict:
        from .agent_continuation import ContinuationStore
        merged = {**self.preferences(), **values}
        for key in ("model_call_retention_days", "tutor_turn_retention_days", "report_retention_days"):
            if not 1 <= int(merged[key]) <= 3650:
                raise InvalidPrivacyPreferenceError(f"{key}必须在1到3650天之间。")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE privacy_preference SET cloud_model_text_allowed = ?, cloud_audio_allowed = ?,
                  raw_media_storage_allowed = ?, model_improvement_allowed = ?,
                  model_call_retention_days = ?, tutor_turn_retention_days = ?,
                  report_retention_days = ?, updated_at = ? WHERE singleton_id = 1
                """,
                (
                    int(bool(merged["cloud_model_text_allowed"])),
                    int(bool(merged["cloud_audio_allowed"])),
                    int(bool(merged["raw_media_storage_allowed"])),
                    int(bool(merged["model_improvement_allowed"])),
                    int(merged["model_call_retention_days"]),
                    int(merged["tutor_turn_retention_days"]),
                    int(merged["report_retention_days"]),
                    now,
                ),
            )
            for scope in (
                "cloud_model_text_allowed",
                "cloud_audio_allowed",
                "raw_media_storage_allowed",
                "model_improvement_allowed",
            ):
                if values.get(scope) is not None:
                    connection.execute(
                        "INSERT INTO consent_record(consent_id, scope, allowed, policy_version, source, occurred_at) VALUES (?, ?, ?, 'privacy_v1', 'parent_ui', ?)",
                        (str(uuid4()), scope, int(bool(merged[scope])), now),
                    )
            self._audit(connection, "parent", "privacy_preferences_updated", None, {"changed_fields": sorted(values)}, now)
            if values.get("cloud_model_text_allowed") is False and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='agent_continuation'"
            ).fetchone():
                sessions = connection.execute("SELECT DISTINCT session_id FROM agent_continuation").fetchall()
                for session in sessions:
                    ContinuationStore.invalidate_in(connection, session[0])
            connection.commit()
        return self.preferences()

    def export_child(self, child_id: str, mistake_learning_renderer_drafts=None) -> dict:
        # Publish the file and its audit before a deletion scans managed copies.
        with self.database.external_effect_guard():
            return self._export_child_locked(child_id, mistake_learning_renderer_drafts)

    def _export_child_locked(self, child_id: str, mistake_learning_renderer_drafts=None) -> dict:
        from .mistake_learning_privacy import validate_renderer_drafts
        renderer_drafts = validate_renderer_drafts(child_id, mistake_learning_renderer_drafts)
        from .curriculum_authority_lifecycle import authority_rows
        export_dir = self.data_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        export_id = str(uuid4())
        destination = export_dir / f"child-export-{child_id}-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            payload = {
                "export_version": "hsc_child_export_v1",
                "exported_at": now.isoformat(),
                "child": self._rows(connection, "SELECT child_id, nickname, grade, curriculum_region, status FROM child_profile WHERE child_id = ?", (child_id,)),
                "family_rule": self._rows(connection, "SELECT * FROM family_rule WHERE child_id = ?", (child_id,)),
                "sessions": self._rows(connection, "SELECT * FROM daily_session WHERE child_id = ?", (child_id,)),
                "session_drafts": self._rows(connection, "SELECT sd.* FROM session_draft sd JOIN daily_session ds ON ds.session_id = sd.session_id WHERE ds.child_id = ?", (child_id,)),
                "session_declarations": self._rows(connection, "SELECT * FROM session_declaration WHERE child_id = ?", (child_id,)),
                "planning_contexts": self._rows(connection, "SELECT spc.* FROM session_planning_context spc JOIN daily_session ds ON ds.session_id = spc.session_id WHERE ds.child_id = ?", (child_id,)),
                "planning_context_events": self._rows(connection, "SELECT pce.* FROM planning_context_event pce JOIN daily_session ds ON ds.session_id = pce.session_id WHERE ds.child_id = ?", (child_id,)),
                "recalls": self._rows(connection, "SELECT cr.* FROM classroom_recall cr JOIN daily_session ds ON ds.session_id = cr.session_id WHERE ds.child_id = ?", (child_id,)),
                "recall_reviews": self._rows(connection, "SELECT rr.* FROM recall_review rr JOIN daily_session ds ON ds.session_id = rr.session_id WHERE ds.child_id = ?", (child_id,)),
                "homework": self._rows(connection, "SELECT * FROM homework_item WHERE child_id = ?", (child_id,)),
                "homework_revisions": self._rows(connection, "SELECT hr.* FROM homework_revision hr JOIN homework_item hi ON hi.homework_id = hr.homework_id WHERE hi.child_id = ?", (child_id,)),
                "deferred_task_reviews": self._rows(connection, "SELECT * FROM deferred_task_review WHERE child_id = ?", (child_id,)),
                "plans": self._rows(connection, "SELECT * FROM daily_plan WHERE child_id = ?", (child_id,)),
                "plan_constraint_decisions": self._rows(connection, """SELECT pcd.* FROM plan_constraint_decision pcd
                    JOIN daily_plan dp ON dp.plan_id=pcd.plan_id WHERE dp.child_id=?""", (child_id,))
                    if self._table_exists(connection, "plan_constraint_decision") else [],
                "weekly_goals": self._rows(connection, "SELECT * FROM weekly_goal WHERE child_id = ?", (child_id,)),
                "goal_programs": self._optional_rows(connection, "goal_program", child_id),
                "goal_program_revisions": self._optional_rows(connection, "goal_program_revision", child_id),
                "goal_cycles": self._optional_rows(connection, "goal_cycle", child_id),
                "specialized_assessments": self._optional_rows(connection, "specialized_assessment_event", child_id),
                "specialized_program_snapshots": self._optional_rows(connection, "specialized_program_snapshot", child_id),
                "specialized_assessment_runs": self._optional_rows(connection, "specialized_assessment_run", child_id),
                "specialized_assessment_run_attempts": self._rows(connection, """SELECT attempt.* FROM specialized_assessment_run_attempt attempt
                    JOIN specialized_assessment_run run ON run.run_id=attempt.run_id WHERE run.child_id=?""", (child_id,))
                    if self._table_exists(connection, "specialized_assessment_run_attempt") else [],
                "specialized_exam_runs": self._optional_rows(connection, "specialized_exam_run", child_id),
                "specialized_exam_items": self._rows(connection, """SELECT item.* FROM specialized_exam_item item
                    JOIN specialized_exam_run run ON run.run_id=item.run_id WHERE run.child_id=?""", (child_id,))
                    if self._table_exists(connection, "specialized_exam_item") else [],
                "specialized_exam_sections": self._rows(connection, """SELECT state.* FROM specialized_exam_section_state state
                    JOIN specialized_exam_run run ON run.run_id=state.run_id WHERE run.child_id=?""", (child_id,))
                    if self._table_exists(connection, "specialized_exam_section_state") else [],
                "child_model_snapshots": self._optional_rows(connection, "child_model_snapshot", child_id),
                "analysis_candidate_ingest_attempts": self._optional_rows(connection, "analysis_candidate_ingest_attempt", child_id),
                "analysis_candidates": self._optional_rows(connection, "analysis_candidate", child_id),
                "analysis_candidate_events": self._optional_rows(connection, "analysis_candidate_event", child_id),
                "child_analysis_review_jobs": self._optional_rows(connection, "child_analysis_review_job", child_id),
                "child_model_corrections": self._optional_rows(connection, "child_model_correction", child_id),
                "child_model_correction_events": self._optional_rows(connection, "child_model_correction_event", child_id),
                "plan_revisions": self._rows(connection, "SELECT pr.* FROM plan_revision pr JOIN daily_plan dp ON dp.plan_id = pr.plan_id WHERE dp.child_id = ?", (child_id,)),
                "reflections": self._rows(connection, "SELECT * FROM daily_reflection WHERE child_id = ?", (child_id,)),
                "reports": self._rows(connection, "SELECT * FROM daily_report WHERE child_id = ?", (child_id,)),
                "parent_action_snapshots": self._optional_rows(connection, "parent_action_snapshot", child_id),
                "parent_action_feedback_events": self._optional_rows(connection, "parent_action_feedback_event", child_id),
                "report_retry_requests": self._rows(connection, "SELECT * FROM report_retry_request WHERE child_id = ?", (child_id,)),
                "parent_report_feedback": self._rows(connection, "SELECT * FROM parent_report_feedback WHERE child_id = ?", (child_id,)),
                "report_feedback_revisions": self._optional_rows(connection, "report_feedback_revision", child_id),
                "report_plan_feedback_receipts": self._optional_rows(connection, "report_plan_feedback_receipt", child_id),
                "report_plan_adjustment_events": self._optional_rows(connection, "report_plan_adjustment_event", child_id),
                "report_adjustment_bindings": self._optional_rows(connection, "report_adjustment_binding", child_id),
                "report_rebinding_commands": self._optional_rows(connection, "report_rebinding_command", child_id),
                "report_rebinding_cancelled_commands": self._optional_rows(connection, "report_rebinding_cancelled_command", child_id),
                "period_reports": self._rows(connection, "SELECT * FROM period_report WHERE child_id = ?", (child_id,)),
                "period_report_refresh_states": self._rows(connection, "SELECT * FROM period_report_refresh_state WHERE child_id = ?", (child_id,)),
                "tutor_turns": self._rows(connection, "SELECT * FROM tutor_turn WHERE child_id = ?", (child_id,)),
                "tutor_playback_checkpoints": self._optional_rows(connection, "tutor_playback_checkpoint", child_id),
                "rubric_assessments": self._optional_rows(connection, "rubric_assessment", child_id),
                "rubric_review_events": self._optional_rows(connection, "rubric_review_event", child_id),
                "rubric_calibration_protocols": self._optional_rows(connection, "rubric_calibration_protocol", child_id),
                "rubric_calibration_events": self._optional_rows(connection, "rubric_calibration_event", child_id),
                "rubric_calibration_authorities": calibration_authority_rows(connection, child_id),
                "tutor_playback_events": self._rows(connection, """SELECT e.* FROM tutor_playback_event e
                    JOIN tutor_playback_checkpoint p USING(playback_id) WHERE p.child_id=?""", (child_id,))
                    if self._table_exists(connection, "tutor_playback_event") else [],
                "tutor_feedback": self._rows(connection, "SELECT * FROM tutor_feedback WHERE child_id = ?", (child_id,)),
                "model_calls": self._rows(connection, "SELECT * FROM model_call WHERE child_id = ?", (child_id,)),
                "strategy_experiments": self._rows(connection, "SELECT * FROM strategy_experiment WHERE child_id = ?", (child_id,)),
                "strategy_evaluation_events": self._rows(connection, "SELECT * FROM strategy_evaluation_event WHERE child_id = ?", (child_id,)),
                "strategy_transition_events": self._rows(connection, "SELECT * FROM strategy_transition_event WHERE child_id = ?", (child_id,)),
                "bounded_experiment_protocols": self._optional_rows(connection, "bounded_experiment_protocol", child_id),
                "bounded_experiment_events": self._optional_rows(connection, "bounded_experiment_event", child_id),
                "agent_turns": self._rows(connection, "SELECT * FROM agent_turn WHERE child_id = ?", (child_id,)),
                "execution_trace_events": self._optional_rows(connection, "execution_trace_event", child_id),
                "agent_objectives": self._optional_rows(connection, "agent_objective", child_id),
                "agent_outcomes": self._optional_rows(connection, "agent_outcome", child_id),
                "agent_commitments": self._optional_rows(connection, "agent_commitment", child_id),
                "companion_memories": self._optional_rows(connection, "companion_memory", child_id),
                "companion_activities": self._optional_rows(connection, "companion_activity", child_id),
                "companion_activity_outcomes": self._optional_rows(connection, "companion_activity_outcome", child_id),
                "companion_activity_outcome_events": self._optional_rows(connection, "companion_activity_outcome_event", child_id),
                "companion_activity_voice_bindings": self._optional_rows(connection, "companion_activity_voice_binding", child_id),
                "learning_input_arrivals": self._optional_rows(connection, "learning_input_arrival", child_id),
                "learning_input_proposals": self._optional_rows(connection, "learning_input_proposal", child_id),
                "learning_input_revisions": self._optional_rows(connection, "learning_input_revision_event", child_id),
                "learning_input_commands": self._optional_rows(connection, "learning_input_command", child_id),
                "learning_input_replan_receipts": self._optional_rows(connection, "learning_input_replan_receipt", child_id),
                "learning_input_replan_commands": self._optional_rows(connection, "learning_input_replan_command", child_id),
                "learning_input_replan_items": self._optional_rows(connection, "learning_input_replan_item", child_id),
                "learning_input_replan_unplaced": self._optional_rows(connection, "learning_input_replan_unplaced", child_id),
                "learning_input_carryover_events": self._optional_rows(connection, "learning_input_carryover_event", child_id),
                "learning_input_carryover_plan_receipts": self._optional_rows(connection, "learning_input_carryover_plan_receipt", child_id),
                "companion_memory_sources": self._optional_rows(connection, "companion_memory_source", child_id),
                "companion_memory_audit": self._optional_rows(connection, "companion_memory_audit", child_id),
                "companion_memory_suppressions": self._optional_rows(connection, "companion_memory_suppression", child_id),
                "companion_memory_document_sources": self._optional_rows(connection, "companion_memory_document_source", child_id),
                "companion_memory_document_checkpoints": self._optional_rows(connection, "companion_memory_document_checkpoint", child_id),
                "local_course_context_events": self._optional_rows(connection, "kg_local_course_context_event", child_id),
                "learning_session_closures": self._optional_rows(connection, "learning_session_closure", child_id),
                "learning_outcome_reviews": self._optional_rows(connection, "learning_outcome_review_event", child_id),
                "learning_evaluation_executions": self._rows(connection, "SELECT e.* FROM learning_evaluation_execution e JOIN agent_step s USING(step_id) JOIN agent_plan p USING(plan_id) WHERE p.child_id=?", (child_id,)) if self._table_exists(connection, "learning_evaluation_execution") else [],
                "companion_memory_evidence": self._rows(connection, "SELECT e.* FROM companion_memory_evidence e JOIN companion_memory m USING(memory_id) WHERE m.child_id = ?", (child_id,)) if self._table_exists(connection, "companion_memory_evidence") else [],
                "agent_proactive_prompts": self._optional_rows(connection, "agent_proactive_prompt", child_id),
                "controlled_content_rules": self._optional_rows(connection, "controlled_content_rule", child_id),
                "controlled_content_approvals": self._optional_rows(connection, "controlled_content_approval", child_id),
                "controlled_content_sessions": self._optional_rows(connection, "controlled_content_session", child_id),
                "controlled_game_rules": self._optional_rows(connection, "controlled_game_rule", child_id),
                "controlled_game_approvals": self._optional_rows(connection, "controlled_game_approval", child_id),
                "controlled_game_sessions": self._optional_rows(connection, "controlled_game_session", child_id),
                "safety_events": self._rows(connection, "SELECT * FROM child_safety_event WHERE child_id = ?", (child_id,)),
                "voice_inputs": self._rows(connection, "SELECT * FROM voice_input WHERE child_id = ?", (child_id,)),
                "voice_capture_bindings": self._optional_rows(connection, 'voice_capture_binding', child_id),
                "voice_control_intent_requests": self._optional_rows(connection, 'voice_control_intent_request', child_id),
                "break_control_receipts": self._optional_rows(connection, 'break_control_receipt', child_id),
                "writing_followups": self._optional_rows(connection, 'writing_followup', child_id),
                "writing_reference_revisions": self._optional_rows(connection, 'writing_reference_revision', child_id),
                "writing_ocr_confirmations": self._optional_rows(connection, 'writing_ocr_confirmation', child_id),
                "writing_ocr_reviews": self._rows(connection, '''SELECT r.* FROM writing_ocr_review r
                    JOIN writing_ocr_confirmation w USING(confirmation_id) WHERE w.child_id=?''', (child_id,))
                    if self._table_exists(connection, 'writing_ocr_review') else [],
                "writing_ocr_events": self._optional_rows(connection, 'writing_ocr_event', child_id),
                "preserved_child_responses": self._optional_rows(connection, 'privacy_preserved_child_response', child_id),
                "source_erasures": self._optional_rows(connection, 'privacy_source_erasure', child_id),
                "source_evidence_withdrawals": self._optional_rows(connection, 'privacy_source_evidence_withdrawal', child_id),
                "cloud_voice_interactions": self._optional_rows(connection, "cloud_voice_interaction", child_id),
                "capture_artifacts": self._rows(connection, "SELECT * FROM capture_artifact WHERE child_id = ?", (child_id,)),
                "ocr_review_items": self._optional_rows(connection, "ocr_review_item", child_id),
                "ocr_review_batches": self._optional_rows(connection, "ocr_review_batch", child_id),
                "ocr_review_events": self._rows(connection, """SELECT e.* FROM ocr_review_event e
                    WHERE e.artifact_id IN (SELECT artifact_id FROM ocr_review_item WHERE child_id=?
                      UNION SELECT artifact_id FROM capture_artifact WHERE child_id=?)""", (child_id, child_id))
                    if self._table_exists(connection, "ocr_review_event") else [],
                "document_monitor_sessions": self._optional_rows(connection, "document_monitor_session", child_id),
                "document_monitor_events": self._optional_rows(connection, "document_monitor_event", child_id),
                "document_observation_segments": self._optional_rows(connection, "document_observation_segment", child_id),
                "daily_observation_report_sources": self._optional_rows(connection, "daily_observation_report_source", child_id),
                "daily_observation_report_outbox": self._optional_rows(connection, "daily_observation_report_outbox", child_id),
                "daily_observation_report_snapshots": self._rows(connection, "SELECT snapshot_id,source_id,child_id,day,recipient_id,generated_at,payload_sha256,status FROM daily_observation_report_snapshot WHERE child_id=?", (child_id,)) if self._table_exists(connection, 'daily_observation_report_snapshot') else [],
                "daily_report_email_verifications": self._optional_rows(connection, 'daily_report_email_verification', child_id),
                "report_delivery_recipient_authority_events": self._rows(connection, "SELECT audit_id,action,child_id,summary_json,occurred_at FROM audit_event WHERE child_id=? AND action IN ('report_delivery_test_recipient_selected','report_delivery_recipient_authority_changed','report_delivery_auto_verification_blocked')", (child_id,)),
                "daily_observation_delivery_ledger": self._optional_rows(connection, 'daily_observation_delivery_ledger', child_id),
                "document_monitor_requests": self._optional_rows(connection, "document_monitor_request", child_id),
                "document_operation_receipts": self._optional_rows(connection, "document_operation_receipt", child_id),
                "document_operation_deliveries": self._optional_rows(connection, "document_operation_delivery", child_id),
                "document_page_revisions": self._optional_rows(connection, "document_page_revision", child_id),
                "observed_questions": self._optional_rows(connection, "observed_question", child_id),
                "observed_question_occurrences": self._optional_rows(connection, "observed_question_occurrence", child_id),
                "observed_answer_evidence": self._optional_rows(connection, "observed_answer_evidence", child_id),
                "paper_answer_authorities": self._optional_rows(connection, "paper_answer_authority", child_id),
                "paper_scoring_requests": self._optional_rows(connection, "paper_scoring_request", child_id),
                "paper_scoring_decisions": self._optional_rows(connection, "paper_scoring_decision", child_id),
                "paper_scoring_decision_revisions": self._optional_rows(connection, "paper_scoring_decision_revision", child_id),
                "paper_scoring_revision_status_events": self._optional_rows(connection, "paper_scoring_revision_status_event", child_id),
                "paper_subjective_score_bindings": self._optional_rows(connection, "paper_subjective_score_binding", child_id),
                "paper_scoring_supply_obligations": self._optional_rows(connection, "paper_scoring_supply_obligation", child_id),
                "paper_scoring_supply_events": self._optional_rows(connection, "paper_scoring_supply_event", child_id),
                "paper_scoring_supply_work": self._optional_rows(connection, "paper_scoring_supply_work", child_id),
                "paper_scoring_source_lookups": self._optional_rows(connection, "paper_scoring_source_lookup", child_id),
                "paper_scoring_source_candidates": self._optional_rows(connection, "paper_scoring_source_candidate", child_id),
                "paper_scoring_source_qualifications": self._optional_rows(connection, "paper_scoring_source_qualification", child_id),
                "paper_scoring_dialogue_reconciliations": self._optional_rows(connection, "paper_scoring_dialogue_reconciliation", child_id),
                "paper_scoring_dialogue_revision_reconciliations": self._optional_rows(connection, "paper_scoring_dialogue_revision_reconciliation", child_id),
                "paper_scoring_revision_mistake_links": self._optional_rows(connection, "paper_scoring_revision_mistake_link", child_id),
                "paper_scoring_decision_status_events": self._optional_rows(connection, "paper_scoring_decision_status_event", child_id),
                "paper_correct_verification_obligations": self._optional_rows(connection, "paper_correct_verification_obligation", child_id),
                "paper_correct_verification_targets": self._rows(connection, """SELECT target.* FROM paper_correct_verification_target target
                    JOIN paper_correct_verification_obligation obligation USING(obligation_id)
                    WHERE obligation.child_id=?""", (child_id,))
                    if self._table_exists(connection, "paper_correct_verification_target") else [],
                "paper_correct_verification_events": self._optional_rows(connection, "paper_correct_verification_event", child_id),
                "paper_correct_revision_obligations": self._optional_rows(connection, "paper_correct_revision_obligation", child_id),
                "paper_correct_revision_targets": self._rows(connection, """SELECT target.* FROM paper_correct_revision_target target
                    JOIN paper_correct_revision_obligation obligation USING(obligation_id)
                    WHERE obligation.child_id=?""", (child_id,))
                    if self._table_exists(connection, "paper_correct_revision_target") else [],
                "paper_correct_revision_events": self._optional_rows(connection, "paper_correct_revision_event", child_id),
                "visual_answer_evidence_promotions": self._optional_rows(
                    connection, "visual_answer_evidence_promotion", child_id
                ),
                "document_learning_analyses": self._optional_rows(connection, "document_learning_analysis", child_id),
                "document_vision_attempts": self._optional_rows(connection, "document_vision_attempt", child_id),
                "document_vision_requests": self._optional_rows(connection, "document_vision_request", child_id),
                "state_events": self._rows(connection, "SELECT * FROM child_state_event WHERE child_id = ?", (child_id,)),
                "observation_corrections": self._rows(connection, "SELECT * FROM observation_correction WHERE child_id = ?", (child_id,)),
                "observation_calibrations": self._rows(connection, "SELECT * FROM observation_calibration_run WHERE child_id = ?", (child_id,)),
                "interventions": self._rows(connection, "SELECT * FROM intervention_record WHERE child_id = ?", (child_id,)),
                "behavior_events": self._rows(connection, "SELECT * FROM observable_event WHERE child_id = ?", (child_id,)),
                "behavior_states": self._rows(connection, "SELECT * FROM probabilistic_state WHERE child_id = ?", (child_id,)),
                "behavior_interventions": self._rows(connection, "SELECT * FROM intervention WHERE child_id = ?", (child_id,)),
                "behavior_retractions": self._rows(connection, "SELECT * FROM behavior_retraction WHERE child_id = ?", (child_id,)),
                "behavior_daily_features": self._rows(connection, "SELECT * FROM daily_feature WHERE child_id = ?", (child_id,)),
                "habit_experiments": self._optional_rows(connection, "habit_experiment", child_id),
                "habit_experiment_events": self._optional_rows(connection, "habit_experiment_event", child_id),
                "habit_formation_states": self._optional_rows(connection, "habit_formation_state", child_id),
                "habit_formation_events": self._optional_rows(connection, "habit_formation_event", child_id),
                "sensor_leases": self._rows(connection, "SELECT sl.* FROM sensor_lease sl JOIN daily_session ds ON ds.session_id = sl.session_id WHERE ds.child_id = ?", (child_id,)),
                "sensor_release_verifications": self._rows(connection, "SELECT * FROM sensor_release_verification WHERE child_id = ?", (child_id,)),
                "device_incidents": self._rows(connection, "SELECT * FROM device_incident WHERE child_id = ?", (child_id,)),
                "device_incident_reports": self._rows(connection, "SELECT dir.* FROM device_incident_report dir JOIN device_incident di ON di.incident_id = dir.incident_id WHERE di.child_id = ?", (child_id,)),
                "learning_evidence": self._rows(connection, "SELECT * FROM learning_evidence WHERE child_id = ?", (child_id,)),
                "diagnostic_prompts": self._optional_rows(connection, "diagnostic_prompt", child_id),
                "paper_learning_selections": self._optional_rows(connection, "paper_learning_selection", child_id),
                "paper_learning_selection_events": self._optional_rows(connection, "paper_learning_selection_event", child_id),
                "pet_media_bindings": self._optional_rows(connection, "pet_media_binding", child_id),
                "pet_multipa_execution_receipts": self._optional_rows(connection,"pet_multipa_execution_receipt",child_id),
                "pet_multipa_execution_receipt_events": self._optional_rows(connection,"pet_multipa_execution_receipt_event",child_id),
                "pet_speaking_candidate_jobs": self._optional_rows(connection,"pet_speaking_candidate_job",child_id),
                "pet_speaking_model_candidates": self._optional_rows(connection, "pet_speaking_model_candidate", child_id),
                "pet_speaking_model_candidate_events": self._optional_rows(connection, "pet_speaking_model_candidate_event", child_id),
                "pet_audio_work": self._optional_rows(connection, "pet_audio_work", child_id),
                "pet_audio_grants": self._optional_rows(connection, "pet_audio_grant", child_id),
                "pet_audio_retries": self._optional_rows(connection, "pet_audio_retry", child_id),
                "pet_audio_dispatches": self._optional_rows(connection, "pet_audio_dispatch", child_id),
                "pet_media_events": self._rows(connection, "SELECT e.* FROM pet_media_event e JOIN pet_media_binding b USING(media_id) WHERE b.child_id=?", (child_id,))
                    if self._table_exists(connection, 'pet_media_event') else [],
                "mastery_state": self._rows(connection, "SELECT * FROM mastery_state WHERE child_id = ?", (child_id,)),
                "mastery_update_events": self._rows(connection, "SELECT * FROM mastery_update_event WHERE child_id = ?", (child_id,)),
                "review_tasks": self._rows(connection, "SELECT * FROM review_task WHERE child_id = ?", (child_id,)),
                "pet_vocabulary_cards": self._optional_rows(connection, "pet_vocabulary_card", child_id),
                "pet_vocabulary_attempts": self._optional_rows(connection, "pet_vocabulary_attempt", child_id),
                "pet_vocabulary_prompt_sessions": self._optional_rows(connection, "pet_vocabulary_prompt_session", child_id),
                "mistake_clusters": self._rows(connection, "SELECT * FROM mistake_cluster WHERE child_id = ?", (child_id,)),
                "mistake_cases": self._rows(connection, "SELECT * FROM mistake_case WHERE child_id = ?", (child_id,)),
                "mistake_occurrences": self._rows(connection, "SELECT * FROM mistake_occurrence WHERE child_id = ?", (child_id,)),
                "mistake_diagnoses": self._rows(connection, "SELECT * FROM mistake_diagnosis WHERE child_id = ?", (child_id,)),
                "mistake_diagnosis_events": self._optional_rows(connection, "mistake_diagnosis_event", child_id),
                "mistake_source_revisions": self._optional_rows(connection, "mistake_source_revision", child_id),
                "mistake_remediation_steps": self._rows(connection, "SELECT * FROM remediation_step WHERE child_id = ?", (child_id,)),
                "mistake_attempts": self._rows(connection, "SELECT * FROM mistake_attempt WHERE child_id = ?", (child_id,)),
                "mistake_mastery_gates": self._rows(connection, "SELECT mmg.* FROM mistake_mastery_gate mmg JOIN mistake_case mc ON mc.mistake_id = mmg.mistake_id WHERE mc.child_id = ?", (child_id,)),
                "mistake_status_events": self._rows(connection, "SELECT * FROM mistake_status_event WHERE child_id = ?", (child_id,)),
                "mistake_capture_candidates": self._rows(connection, "SELECT * FROM mistake_capture_candidate WHERE child_id = ?", (child_id,)),
                "mistake_capture_candidate_events": self._rows(connection, "SELECT * FROM mistake_capture_candidate_event WHERE child_id = ?", (child_id,)),
                "mistake_dialogues": self._rows(connection, "SELECT * FROM mistake_dialogue WHERE child_id = ?", (child_id,)),
                "mistake_dialogue_events": self._rows(connection, "SELECT * FROM mistake_dialogue_event WHERE child_id = ?", (child_id,)),
                "variant_generation_jobs": self._optional_rows(connection, "variant_generation_job", child_id),
                "variant_candidates": self._optional_rows(connection, "variant_candidate", child_id),
                "specialized_supply_jobs": self._optional_rows(connection, "specialized_supply_job", child_id),
                "specialized_supply_candidates": self._optional_rows(connection, "specialized_supply_candidate", child_id),
                "curriculum_supply_selections": self._optional_rows(connection, "curriculum_supply_selection", child_id),
                "curriculum_supply_packages": self._optional_rows(connection, "curriculum_supply_package", child_id),
                "curriculum_supply_events": self._optional_rows(connection, "curriculum_supply_event", child_id),
                "curriculum_first_sources": self._optional_rows(connection, "curriculum_first_source", child_id),
                "curriculum_first_source_events": self._optional_rows(connection, "curriculum_first_source_event", child_id),
                "writing_question_authorizations": self._optional_rows(connection, "writing_question_authorization_event", child_id),
                "paper_variant_authorizations": self._optional_rows(connection, "paper_variant_authorization_event", child_id),
                "paper_variant_job_authorizations": self._optional_rows(connection, "paper_variant_job_authorization", child_id),
                "curriculum_internalization_authorities": authority_rows(connection, child_id),
                "parent_report_source_issues": self._rows(connection,
                    "SELECT * FROM audit_event WHERE child_id=? AND action='parent_report_source_issue' ORDER BY rowid", (child_id,)),
                "visual_material_events": visual_material_rows(connection, child_id),
                "knowledge_sources": self._rows(connection, "SELECT * FROM knowledge_source WHERE child_id = ?", (child_id,)),
                "knowledge_source_revisions": self._optional_rows(connection, "knowledge_source_revision_event", child_id),
                "knowledge_excerpts": self._rows(connection, "SELECT ke.* FROM knowledge_excerpt ke JOIN knowledge_source ks ON ks.source_id = ke.source_id WHERE ks.child_id = ?", (child_id,)),
            }
            payload["cloud_transmission_allowed"] = False
            from .mistake_learning_privacy import export_learning
            payload['mistake_learning'] = export_learning(connection, child_id)
            if self._table_exists(connection, 'kg_parent_textbook_form_draft'):
                from .knowledge_graph.parent_textbook_drafts import ParentTextbookDraftService
                payload['parent_textbook_drafts'] = ParentTextbookDraftService(self.database).export(connection, child_id)
            payload["scope"] = "local_only"
            from .ordinary_learning_text_projection import public_export_copy
            payload = public_export_copy(payload)
            payload['mistake_learning_renderer_drafts'] = renderer_drafts
            destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
            self._audit(connection, "parent", "child_data_exported", child_id, {"export_id": export_id, "sha256": sha256}, utc_now())
            connection.commit()
        return {"export_id": export_id, "path": str(destination), "sha256": sha256, "size_bytes": destination.stat().st_size}

    def deletion_preview(self, child_id: str) -> dict:
        from .curriculum_authority_lifecycle import authority_count
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            return {
                "learning": self._count(connection, "learning_evidence", child_id),
                "ocr_reviews": self._optional_count(connection, "ocr_review_item", child_id) + self._optional_count(connection, "ocr_review_batch", child_id),
                "execution_traces": self._optional_count(connection, "execution_trace_event", child_id),
                "companion_activity_outcomes": sum(self._optional_count(connection, table, child_id) for table in
                    ("companion_activity_outcome", "companion_activity_outcome_event", "companion_activity_voice_binding")),
                "companion_activities": self._optional_count(connection, "companion_activity", child_id),
                "learning_input_revisions": sum(self._optional_count(connection, table, child_id) for table in
                    ("learning_input_arrival", "learning_input_proposal", "learning_input_revision_event", "learning_input_command",
                     "learning_input_replan_receipt", "learning_input_replan_command", "learning_input_replan_item", "learning_input_replan_unplaced",
                     "learning_input_carryover_event", "learning_input_carryover_plan_receipt")),
                "rubrics": self._optional_count(connection, "rubric_assessment", child_id) + self._optional_count(connection, "rubric_review_event", child_id),
                "rubric_calibrations": self._optional_count(connection, "rubric_calibration_protocol", child_id) + self._optional_count(connection, "rubric_calibration_event", child_id) + len(calibration_authority_rows(connection, child_id)),
                "diagnostic_prompts": self._optional_count(connection, "diagnostic_prompt", child_id),
                "paper_learning_selections": self._optional_count(connection, "paper_learning_selection", child_id) + self._optional_count(connection, "paper_learning_selection_event", child_id),
                "pet_media_bindings": self._optional_count(connection, "pet_media_binding", child_id),
                "pet_multipa_execution_receipts": sum(self._optional_count(connection,t,child_id)for t in("pet_multipa_execution_receipt","pet_multipa_execution_receipt_event")),
                "pet_speaking_model_candidates": sum(self._optional_count(connection, table, child_id) for table in
                    ("pet_speaking_model_candidate", "pet_speaking_model_candidate_event","pet_speaking_candidate_job")),
                "pet_audio_work": self._optional_count(connection, "pet_audio_work", child_id),
                "pet_audio_grants": self._optional_count(connection, "pet_audio_grant", child_id),
                "weekly_goals": self._count(connection, "weekly_goal", child_id),
                "goal_programs": self._optional_count(connection, "goal_program", child_id) + self._optional_count(connection, "goal_cycle", child_id),
                "specialized_programs": self._optional_count(connection, "specialized_assessment_event", child_id) + self._optional_count(connection, "specialized_program_snapshot", child_id) + self._optional_count(connection, "specialized_assessment_run", child_id),
                "specialized_exams": self._optional_count(connection, "specialized_exam_run", child_id),
                "child_model_snapshots": self._optional_count(connection, "child_model_snapshot", child_id),
                "analysis_candidates": sum(self._optional_count(connection, table, child_id) for table in
                    ("analysis_candidate_ingest_attempt", "analysis_candidate", "analysis_candidate_event", "child_analysis_review_job")),
                "child_model_corrections": self._optional_count(connection, "child_model_correction", child_id) + self._optional_count(connection, "child_model_correction_event", child_id),
                "mastery_updates": self._count(connection, "mastery_update_event", child_id),
                "reviews": self._count(connection, "review_task", child_id),
                "pet_vocabulary": self._optional_count(connection, "pet_vocabulary_card", child_id) + self._optional_count(connection, "pet_vocabulary_attempt", child_id) + self._optional_count(connection, "pet_vocabulary_prompt_session", child_id),
                "mistake_diagnosis_events": self._optional_count(connection, "mistake_diagnosis_event", child_id),
                "mistake_source_revisions": self._optional_count(connection, "mistake_source_revision", child_id),
                "mistakes": self._count(connection, "mistake_cluster", child_id) + self._count(connection, "mistake_case", child_id) + self._count(connection, "mistake_occurrence", child_id) + self._count(connection, "mistake_diagnosis", child_id) + self._count(connection, "remediation_step", child_id) + self._count(connection, "mistake_attempt", child_id) + self._count(connection, "mistake_status_event", child_id) + connection.execute("SELECT COUNT(*) FROM mistake_mastery_gate mmg JOIN mistake_case mc ON mc.mistake_id = mmg.mistake_id WHERE mc.child_id = ?", (child_id,)).fetchone()[0],
                "mistake_capture_candidates": self._count(connection, "mistake_capture_candidate", child_id) + self._count(connection, "mistake_capture_candidate_event", child_id),
                "variant_supply": self._optional_count(connection, "variant_generation_job", child_id) + self._optional_count(connection, "variant_candidate", child_id),
                "specialized_supply": self._optional_count(connection, "specialized_supply_job", child_id) + self._optional_count(connection, "specialized_supply_candidate", child_id),
                "curriculum_supply": sum(self._optional_count(connection, table, child_id) for table in
                    ("curriculum_supply_selection", "curriculum_supply_package", "curriculum_supply_event", "curriculum_first_source", "curriculum_first_source_event")),
                "writing_question_authorizations": self._optional_count(connection, "writing_question_authorization_event", child_id),
                "paper_variant_authorizations": self._optional_count(connection, "paper_variant_authorization_event", child_id),
                "paper_variant_job_authorizations": self._optional_count(connection, "paper_variant_job_authorization", child_id),
                "curriculum_internalization_authorities": authority_count(connection, child_id),
                "visual_material_events": visual_material_count(connection, child_id),
                "parent_report_source_issues": connection.execute(
                    "SELECT COUNT(*) FROM audit_event WHERE child_id=? AND action='parent_report_source_issue'", (child_id,)).fetchone()[0],
                "reports": self._count(connection, "daily_report", child_id) + self._count(connection, "period_report", child_id) + self._count(connection, "period_report_refresh_state", child_id) + self._count(connection, "parent_report_feedback", child_id) + self._count(connection, "report_retry_request", child_id)
                    + sum(self._count(connection, table, child_id) for table in ('report_feedback_revision', 'report_plan_feedback_receipt', 'report_plan_adjustment_event', 'report_adjustment_binding', 'report_rebinding_command', 'report_rebinding_cancelled_command') if self._table_exists(connection, table)),
                "daily_observation_report_sources": self._optional_count(connection, 'daily_observation_report_source', child_id) + self._optional_count(connection, 'daily_observation_report_outbox', child_id) + self._optional_count(connection, 'daily_observation_report_snapshot', child_id) + self._optional_count(connection, 'daily_visual_schedule', child_id),
                "daily_report_email_verifications": self._optional_count(connection, 'daily_report_email_verification', child_id),
                "report_delivery_recipient_authority_events": connection.execute("SELECT COUNT(*) FROM audit_event WHERE child_id=? AND action IN ('report_delivery_test_recipient_selected','report_delivery_recipient_authority_changed','report_delivery_auto_verification_blocked')", (child_id,)).fetchone()[0],
                "daily_observation_delivery_history": self._optional_count(connection, 'daily_observation_delivery_ledger', child_id),
                "parent_actions": self._optional_count(connection, "parent_action_snapshot", child_id) + self._optional_count(connection, "parent_action_feedback_event", child_id),
                "tutor": self._count(connection, "tutor_turn", child_id) + self._count(connection, "tutor_feedback", child_id) + self._count(connection, "model_call", child_id) + self._optional_count(connection, "tutor_playback_checkpoint", child_id),
                "strategy_evolution": self._count(connection, "strategy_experiment", child_id) + self._count(connection, "strategy_evaluation_event", child_id) + self._count(connection, "strategy_transition_event", child_id)
                    + self._optional_count(connection, "bounded_experiment_protocol", child_id) + self._optional_count(connection, "bounded_experiment_event", child_id),
                "media": self._count(connection, "voice_input", child_id) + self._count(connection, "capture_artifact", child_id),
                "cloud_voice_interactions": self._optional_count(connection, "cloud_voice_interaction", child_id),
                "controlled_content_sessions": self._optional_count(connection, "controlled_content_session", child_id),
                "controlled_content_configuration": self._optional_count(connection, "controlled_content_rule", child_id) + self._optional_count(connection, "controlled_content_approval", child_id),
                "controlled_game_sessions": self._optional_count(connection, "controlled_game_session", child_id),
                "controlled_game_configuration": self._optional_count(connection, "controlled_game_rule", child_id) + self._optional_count(connection, "controlled_game_approval", child_id),
                "document_monitoring": self._optional_count(connection, "document_monitor_session", child_id) + self._optional_count(connection, "document_monitor_event", child_id) + self._optional_count(connection, "document_page_revision", child_id) + self._optional_count(connection, "document_observation_segment", child_id),
                "agent_turns": self._count(connection, "agent_turn", child_id),
                "companion_memory": sum(self._optional_count(connection, table, child_id) for table in ("companion_memory", "companion_memory_source", "companion_memory_audit", "companion_memory_checkpoint", "companion_memory_suppression", "companion_memory_document_source", "companion_memory_document_checkpoint")),
                "document_learning_analysis": self._optional_count(connection, "document_learning_analysis", child_id),
                "document_vision_attempt": self._optional_count(connection, "document_vision_attempt", child_id),
                "document_vision_request": self._optional_count(connection, "document_vision_request", child_id),
                "document_monitor_request": self._optional_count(connection, "document_monitor_request", child_id),
                "document_operation_receipt": self._optional_count(connection, "document_operation_receipt", child_id),
                "document_operation_delivery": self._optional_count(connection, "document_operation_delivery", child_id),
                "learning_session_closure": self._optional_count(connection, "learning_session_closure", child_id),
                "agent_proactive_prompts": self._optional_count(connection, "agent_proactive_prompt", child_id),
                "safety_events": self._count(connection, "child_safety_event", child_id),
                "state_events": self._count(connection, "child_state_event", child_id),
                "observation_corrections": self._count(connection, "observation_correction", child_id),
                "observation_calibrations": self._count(connection, "observation_calibration_run", child_id),
                "interventions": self._count(connection, "intervention_record", child_id),
                "behavior_records": self._count(connection, "observable_event", child_id) + self._count(connection, "probabilistic_state", child_id) + self._count(connection, "intervention", child_id) + self._count(connection, "behavior_retraction", child_id) + self._optional_count(connection, "habit_experiment", child_id) + self._optional_count(connection, "habit_experiment_event", child_id) + self._optional_count(connection, "habit_formation_state", child_id) + self._optional_count(connection, "habit_formation_event", child_id),
                "device_incidents": self._count(connection, "device_incident", child_id),
                "sessions": self._count(connection, "daily_session", child_id),
                "session_drafts": connection.execute("SELECT COUNT(*) FROM session_draft sd JOIN daily_session ds ON ds.session_id = sd.session_id WHERE ds.child_id = ?", (child_id,)).fetchone()[0],
                "session_declarations": self._count(connection, "session_declaration", child_id),
                "deferred_task_reviews": self._count(connection, "deferred_task_review", child_id),
                "sensor_release_verifications": self._count(connection, "sensor_release_verification", child_id),
                "knowledge_sources": self._count(connection, "knowledge_source", child_id),
                "knowledge_source_revisions": self._optional_count(connection, "knowledge_source_revision_event", child_id),
                "parent_textbook_drafts": connection.execute("SELECT COUNT(*) FROM kg_parent_textbook_form_draft WHERE child_id=? AND encrypted_payload IS NOT NULL", (child_id,)).fetchone()[0]
                    if self._table_exists(connection, 'kg_parent_textbook_form_draft') else 0,
            }

    def list_capture_artifacts(self, child_id: str, limit: int = 30) -> list[dict]:
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            rows = connection.execute(
                """
                SELECT artifact_id, artifact_type, confidence, status, created_at,
                       local_path, LENGTH(COALESCE(extracted_text, '')) AS extracted_text_chars
                FROM capture_artifact WHERE child_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (child_id, max(1, min(limit, 100))),
            ).fetchall()
        return [
            {
                **dict(row),
                "raw_file_stored": bool(row["local_path"] and Path(row["local_path"]).is_file()),
                "local_path": None,
            }
            for row in rows
        ]

    @_serialized_deletion
    def delete_capture_artifact(
        self, artifact_id: str, confirmation: str, client_action_id: str
    ) -> dict:
        if confirmation != "确认删除":
            raise DeletionConfirmationError()
        existing = self._deletion_job_by_action(client_action_id)
        if existing:
            self._require_matching_deletion(existing, "capture_artifact", artifact_id)
            return self._resume_deletion_job(existing["deletion_id"])
        with self.database.connect() as connection:
            artifact = connection.execute(
                "SELECT child_id, local_path FROM capture_artifact WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if not artifact:
            raise UnknownCaptureArtifactError(artifact_id)
        deletion_id = self._create_deletion_job(
            artifact["child_id"], "capture_artifact", "capture_artifact",
            artifact_id, client_action_id,
        )
        return self._execute_capture_deletion(deletion_id)

    def _commit_original_deletion(self,connection,job,*,native_owned=False):
        from .parent_restore_privacy import persist_missing_references
        parent_owned=persist_missing_references(self.database,connection,job)
        try:connection.commit()
        except BaseException:
            if parent_owned or native_owned:
                from . import restore_publication as publication
                publication._gate(publication._path(self.database.path)).poisoned=True
            raise

    @_serialized_deletion
    def delete_capture_artifact_raw_media(
        self, artifact_id: str, confirmation: str, client_action_id: str
    ) -> dict:
        """Erase only a capture's encrypted page, keeping derived local records."""
        if confirmation != "确认删除":
            raise DeletionConfirmationError()
        existing = self._deletion_job_by_action(client_action_id)
        if existing:
            self._require_matching_deletion(existing, "capture_artifact_raw_media", artifact_id, "raw_media_only")
            return self._resume_deletion_job(existing["deletion_id"])
        with self.database.connect() as connection:
            artifact = connection.execute(
                "SELECT child_id FROM capture_artifact WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        if not artifact:
            raise UnknownCaptureArtifactError(artifact_id)
        deletion_id = self._create_deletion_job(
            artifact["child_id"], "raw_media_only", "capture_artifact_raw_media",
            artifact_id, client_action_id,
        )
        return self._execute_capture_raw_media_deletion(deletion_id)

    def _execute_capture_raw_media_deletion(self, deletion_id: str) -> dict:
        with self.database.connect() as connection:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute("SELECT * FROM deletion_job WHERE deletion_id = ?", (deletion_id,)).fetchone()
            artifact = connection.execute(
                "SELECT child_id, local_path FROM capture_artifact WHERE artifact_id = ?", (job["target_id"],)
            ).fetchone()
            if not artifact:
                connection.rollback()
                self._mark_deletion_failed(deletion_id, "TARGET_NOT_FOUND", "原图删除目标已不存在，请刷新后重试。")
                raise DeletionIncompleteError(deletion_id, "TARGET_NOT_FOUND", 0)
            raw_path = artifact["local_path"]
            connection.execute("UPDATE capture_artifact SET local_path = NULL WHERE artifact_id = ?", (job["target_id"],))
            crop_count = (self._delete(connection, "DELETE FROM mistake_question_crop WHERE artifact_id=?", job["target_id"])
                          if self._table_exists(connection, 'mistake_question_crop') else 0)
            verification = {
                "database_delete_committed": True,
                "derived_artifact_retained": True,
                "raw_file_expected": bool(raw_path),
            }
            connection.execute(
                """UPDATE deletion_job SET status = 'deleting', deleted_counts_json = ?, residual_paths_json = ?,
                   verification_json = ?, error_code = NULL, error_message = NULL, updated_at = ? WHERE deletion_id = ?""",
                (
                    json.dumps({"capture_artifact_retained": 1, "mistake_question_crop": crop_count}),
                    json.dumps([raw_path] if raw_path else []), json.dumps(verification), utc_now(), deletion_id,
                ),
            )
            self._audit(
                connection, "parent", "capture_artifact_raw_media_deleted", artifact["child_id"],
                {"artifact_id": job["target_id"], "deletion_id":deletion_id,"client_action_id":job["client_action_id"],"derived_artifact_retained": True, "raw_file_present": bool(raw_path)},
                utc_now(),
            )
            self._commit_original_deletion(connection,job)
        return self._finish_deletion_job(deletion_id)

    def _execute_capture_deletion(self, deletion_id: str) -> dict:
        with self.database.external_effect_guard():
            return self._execute_capture_deletion_locked(deletion_id)

    def _execute_capture_deletion_locked(self, deletion_id: str) -> dict:
        with self.database.connect() as connection:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM deletion_job WHERE deletion_id = ?", (deletion_id,)
            ).fetchone()
            artifact_id = job["target_id"]
            artifact = connection.execute(
                "SELECT child_id, local_path FROM capture_artifact WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if not artifact:
                connection.rollback()
                self._mark_deletion_failed(
                    deletion_id, "TARGET_NOT_FOUND",
                    "删除目标在数据库清理提交前已不存在，请重新扫描。",
                )
                raise DeletionIncompleteError(deletion_id, "TARGET_NOT_FOUND", 0)
            # A managed export is a composite copy of the child's OCR and
            # report-source IDs; removing one capture invalidates that copy.
            from .knowledge_source_deletion import export_copies
            export_hashes = export_copies(self.data_dir, artifact['child_id'], connection=connection, include_hashes=True)
            linked_homework = connection.execute(
                "SELECT COUNT(*) FROM homework_item WHERE source_artifact_id = ?",
                (artifact_id,),
            ).fetchone()[0]
            linked_plan_items = connection.execute(
                "SELECT COUNT(*) FROM plan_item WHERE completion_artifact_id = ?",
                (artifact_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE homework_item SET source_artifact_id = NULL, ocr_confidence = NULL WHERE source_artifact_id = ?",
                (artifact_id,),
            )
            connection.execute(
                "UPDATE plan_item SET completion_artifact_id = NULL WHERE completion_artifact_id = ?",
                (artifact_id,),
            )
            from .local_ocr.review_lifecycle import delete_review_scope
            review_counts = delete_review_scope(connection, artifact['child_id'], artifact_id)
            from .local_ocr.cache_lifecycle import delete_scope as delete_ocr_cache_scope
            review_counts.update(delete_ocr_cache_scope(connection, artifact['child_id'], artifact_id))
            from .visual_material_lifecycle import delete_scope as delete_visual_scope
            visual_counts, visual_paths = delete_visual_scope(connection, artifact['child_id'], artifact_ids={artifact_id})
            review_counts.update(visual_counts)
            # Frozen report sources can contain IDs of this capture. Invalidate
            # every prepared source for the child in the same deletion transaction.
            if self._table_exists(connection, 'daily_observation_report_source'):
                if self._table_exists(connection, 'daily_observation_delivery_ledger'):
                    review_counts['daily_observation_delivery_ready'] = connection.execute(
                        "DELETE FROM daily_observation_delivery_ledger WHERE child_id=? AND status='ready'", (artifact['child_id'],)).rowcount
                if self._table_exists(connection, 'daily_observation_report_snapshot'):
                    review_counts['daily_observation_report_snapshot'] = connection.execute(
                        'DELETE FROM daily_observation_report_snapshot WHERE child_id=?', (artifact['child_id'],)).rowcount
                review_counts['daily_observation_report_source'] = connection.execute(
                    'DELETE FROM daily_observation_report_source WHERE child_id=?', (artifact['child_id'],)).rowcount
            if self._table_exists(connection, 'mistake_source_revision'):
                review_counts['mistake_source_revision'] = connection.execute(
                    'DELETE FROM mistake_source_revision WHERE artifact_id=? AND child_id=?',
                    (artifact_id, artifact['child_id'])).rowcount
            connection.execute("DELETE FROM capture_artifact WHERE artifact_id = ?", (artifact_id,))
            counts = {
                **review_counts,
                "capture_artifact": 1,
                "homework_references_cleared": linked_homework,
                "plan_item_references_cleared": linked_plan_items,
            }
            verification = {
                "database_delete_committed": True,
                "raw_file_expected": bool(artifact["local_path"]),
                "managed_export_hashes": export_hashes,
            }
            connection.execute(
                """
                UPDATE deletion_job SET status = 'deleting', deleted_counts_json = ?,
                  residual_paths_json = ?, verification_json = ?, updated_at = ?,
                  error_code = NULL, error_message = NULL
                WHERE deletion_id = ?
                """,
                (
                    json.dumps(counts),
                    json.dumps(visual_paths + ([artifact["local_path"]] if artifact["local_path"] else []) + list(export_hashes)),
                    json.dumps(verification), utc_now(), deletion_id,
                ),
            )
            self._audit(
                connection,
                "parent",
                "capture_artifact_deleted",
                artifact["child_id"],
                {
                    "artifact_id": artifact_id,"deletion_id":deletion_id,"client_action_id":job["client_action_id"],
                    "raw_file_present": bool(artifact["local_path"]),
                    "homework_references_cleared": linked_homework,
                    "plan_item_references_cleared": linked_plan_items,
                },
                utc_now(),
            )
            self._commit_original_deletion(connection,job)
        return self._finish_deletion_job(deletion_id)

    @_serialized_deletion
    def delete_knowledge_source(self, child_id: str, source_id: str, confirmation: str, client_action_id: str) -> dict:
        if confirmation != '确认删除':
            raise DeletionConfirmationError()
        if not all(isinstance(value, str) and value.strip() and len(value) <= 200
                   for value in (child_id, source_id, client_action_id)):
            raise InvalidDeletionActionError()
        with self.database.external_effect_guard():
            existing = self._deletion_job_by_action(client_action_id)
            if existing:
                if existing['child_id'] != child_id:
                    raise InvalidDeletionActionError()
                self._require_matching_deletion(existing, 'knowledge_source', source_id, 'knowledge')
                return self._resume_deletion_job(existing['deletion_id'])
            with self.database.connect() as connection:
                self._require_child(connection, child_id)
                source = connection.execute('SELECT child_id FROM knowledge_source WHERE source_id=?', (source_id,)).fetchone()
                erased = connection.execute('SELECT 1 FROM privacy_source_erasure WHERE source_id=? AND child_id=?', (source_id, child_id)).fetchone()
                if (source and source['child_id'] != child_id) or (not source and not erased):
                    raise KeyError('KNOWLEDGE_SOURCE_NOT_FOUND')
            deletion_id = self._create_deletion_job(child_id, 'knowledge', 'knowledge_source', source_id, client_action_id)
            return self._execute_knowledge_source_deletion(deletion_id)

    def _execute_knowledge_source_deletion(self, deletion_id: str) -> dict:
        from .privacy_evidence_cleanup import PrivacyEvidenceCleanupError
        from .privacy_native_sources import purge_native_sources
        from .knowledge_source_deletion import export_copies
        with self.database.external_effect_guard():
            try:
                with self.database.connect() as connection:
                    connection.execute('PRAGMA synchronous=FULL')
                    connection.execute('BEGIN IMMEDIATE')
                    job = connection.execute('SELECT * FROM deletion_job WHERE deletion_id=?', (deletion_id,)).fetchone()
                    if not job or job['target_type'] != 'knowledge_source' or job['scope'] != 'knowledge':
                        raise InvalidDeletionActionError()
                    if json.loads(job['verification_json'] or '{}').get('database_delete_committed'):
                        connection.rollback()
                    else:
                        child_id, source_id = job['child_id'], job['target_id']
                        self._require_child(connection, child_id)
                        export_hashes = export_copies(self.data_dir, child_id, source_id, connection=connection, include_hashes=True)
                        paths = list(export_hashes)
                        scopes, manifest = {}, {}
                        from .visual_material_lifecycle import delete_sources as delete_visual_sources
                        visual_counts, visual_paths = delete_visual_sources(connection, child_id, {source_id})
                        paths.extend(visual_paths)
                        from .curriculum_first_source import purge_sources as purge_first_sources
                        counts = purge_first_sources(connection, child_id, source_ids={source_id}, affected_scopes=scopes)
                        for table, count in purge_native_sources(connection, child_id, owner_source_ids={source_id},
                            affected_scopes=scopes, erasure_manifest=manifest).items():
                            counts[table] = counts.get(table, 0) + count
                        counts.update(visual_counts)
                        from .rubric_calibration import invalidate_sources
                        counts['rubric_calibration_invalidations'] = invalidate_sources(
                            connection, child_id, {source_id}, erasure_id=deletion_id)
                        try:
                            if not self.replay_source_mastery or not self.reconcile_source_deletion:
                                raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_RECONCILE_REQUIRED')
                            for affected_child, nodes in sorted(scopes.items()):
                                for node_id in sorted(nodes):
                                    self.replay_source_mastery(affected_child, node_id, connection=connection, reason='source_deleted')
                            for affected_child in sorted(set(scopes) | {child_id}):
                                self.reconcile_source_deletion(affected_child, connection=connection)
                        except (ValueError, KeyError, sqlite3.Error, OSError) as error:
                            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_RECONCILE_FAILED') from error
                        connection.execute("""UPDATE deletion_job SET status='deleting',deleted_counts_json=?,
                            residual_paths_json=?,verification_json=?,error_code=NULL,error_message=NULL,updated_at=?
                            WHERE deletion_id=?""", (json.dumps(counts), json.dumps(paths),
                                json.dumps({'database_delete_committed':True, 'native_manifest':manifest,
                                            'managed_export_hashes':export_hashes}), utc_now(), deletion_id))
                        self._audit(connection, 'parent', 'knowledge_source_deleted', child_id,
                            {'deletion_id':deletion_id, 'source_id':source_id, 'counts':counts}, utc_now())
                        self._commit_original_deletion(connection,job)
                return self._finish_deletion_job(deletion_id)
            except (PrivacyEvidenceCleanupError, sqlite3.Error, OSError) as error:
                waiting = str(error) == 'PRIVACY_NATIVE_EVALUATION_IN_FLIGHT'
                code = 'DELETION_AWAITING_EVALUATION' if waiting else 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE'
                self._mark_deletion_failed(deletion_id, code, '单份资料删除尚未完成，原任务已保存，可重试。')
                raise DeletionIncompleteError(deletion_id, code, 0) from error

    @_serialized_deletion
    def delete_child_scope(
        self, child_id: str, scope: str, confirmation: str, client_action_id: str
    ) -> dict:
        if confirmation != "确认删除":
            raise DeletionConfirmationError()
        if scope not in DELETION_SCOPES:
            raise InvalidDeletionScopeError(scope)
        existing = self._deletion_job_by_action(client_action_id)
        if existing:
            self._require_matching_deletion(existing, "child_scope", child_id, scope)
            return self._resume_deletion_job(existing["deletion_id"])
        deletion_id = self._create_deletion_job(
            child_id, scope, "child_scope", child_id, client_action_id
        )
        return self._execute_child_scope_deletion(deletion_id)

    def _execute_child_scope_deletion(self, deletion_id: str) -> dict:
        with self.database.external_effect_guard():
            from .privacy_evidence_cleanup import PrivacyEvidenceCleanupError
            try:
                result = self._execute_child_scope_deletion_locked(deletion_id)
            except PrivacyEvidenceCleanupError as error:
                self._deletion_trace.pop(deletion_id, None)
                if str(error) == 'PRIVACY_NATIVE_EVALUATION_IN_FLIGHT':
                    self._mark_deletion_failed(deletion_id, 'DELETION_AWAITING_EVALUATION',
                        '删除请求已保留并阻止后续评价外发；当前请求结束后可重试原清理任务。')
                    raise DeletionIncompleteError(deletion_id, 'DELETION_AWAITING_EVALUATION', 0) from error
                self._mark_deletion_failed(deletion_id, 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE',
                    '关联的审核证据未能完整核对，删除事务已回滚。修复来源后可重试原任务。')
                raise DeletionIncompleteError(deletion_id, 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE', 0) from error
            except sqlite3.Error as error:
                # The borrowed transaction has rolled back. Keep the original job
                # retryable without exposing SQL text or fabricating completion.
                trace = self._deletion_trace.pop(deletion_id, {})
                print("HSC_DIAGNOSTIC " + json.dumps({
                    "event": "privacy_database_delete_failed",
                    "level": "error",
                    "details": {
                        "deletionId": deletion_id,
                        "sqliteErrorName": getattr(error, "sqlite_errorname", type(error).__name__),
                        "sqliteErrorCode": getattr(error, "sqlite_errorcode", None),
                        "sqliteErrorMessage": str(error)[:160],
                        "lastOperation": trace.get("operation", "unknown"),
                        "lastTable": trace.get("table", "unknown"),
                        "recentOperations": trace.get("history", []),
                    },
                }, ensure_ascii=False), flush=True)
                self._mark_deletion_failed(deletion_id, 'PRIVACY_DATABASE_DELETE_FAILED',
                    '数据库清理未提交，原记录已保留。修复存储错误后可重试原清理任务。')
                raise DeletionIncompleteError(deletion_id, 'PRIVACY_DATABASE_DELETE_FAILED', 0) from error
            self._deletion_trace.pop(deletion_id, None)
            return result

    def _execute_child_scope_deletion_locked(self, deletion_id: str) -> dict:
        owned_privacy_preimages=False
        with self.database.connect() as connection:
            connection.set_trace_callback(lambda statement: self._record_deletion_trace(deletion_id, statement))
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM deletion_job WHERE deletion_id = ?", (deletion_id,)
            ).fetchone()
            child_id, scope = job["child_id"], job["scope"]
            self._require_child(connection, child_id)
            if scope in {'media', 'all_history'}:
                from .native_privacy import assert_resource_permit, rotate_child
                assert_resource_permit(connection, child_id, job['client_action_id'])
                from .parent_restore_assets import prepare_original_privacy_erasures
                owned_privacy_preimages=prepare_original_privacy_erasures(self.database,connection,job)
                rotate_child(connection, child_id)
            counts: dict[str, int] = {}
            if scope in {'media', 'all_history'} and self._table_exists(connection, 'mistake_question_crop'):
                counts['mistake_question_crop'] = self._delete(
                    connection, 'DELETE FROM mistake_question_crop WHERE child_id=?', child_id)
            affected_scopes: dict[str, set[str]] = {}
            managed_export_hashes = {}
            raw_paths: list[Path] = []
            if scope == 'reports':
                from .knowledge_source_deletion import export_copies
                managed_export_hashes = export_copies(self.data_dir, child_id, connection=connection, include_hashes=True)
            if scope in {'learning', 'knowledge', 'media', 'tutor', 'all_history'}:
                from .knowledge_source_deletion import export_copies
                managed_export_hashes = export_copies(self.data_dir, child_id, connection=connection, include_hashes=True)
                if self._table_exists(connection, 'agent_commitment'):
                    counts['original_question_review_payloads'] = connection.execute("""UPDATE agent_commitment
                        SET payload_json=json_remove(payload_json,'$.independent_mapping','$.independent_target_hypothesis','$.practice.independent_request',
                            '$.practice.independent_results','$.practice.independent_prompt_id')
                        WHERE child_id=? AND CASE WHEN json_valid(payload_json) THEN
                          json_type(payload_json,'$.independent_mapping') IS NOT NULL
                          OR json_type(payload_json,'$.independent_target_hypothesis') IS NOT NULL
                          OR json_type(payload_json,'$.practice.independent_request') IS NOT NULL
                          OR json_type(payload_json,'$.practice.independent_results') IS NOT NULL ELSE 0 END""", (child_id,)).rowcount
            if scope in {'learning', 'knowledge', 'all_history'}:
                from .curriculum_first_source import purge_sources as purge_first_sources
                counts.update(purge_first_sources(connection, child_id, affected_scopes=affected_scopes))
                from .manual_standard_scope import purge_manual_requirements
                counts.update(purge_manual_requirements(connection, child_id))
            if scope in {'learning', 'knowledge', 'tutor', 'all_history'}:
                from .mistake_learning_privacy import purge_learning
                counts.update(purge_learning(connection, child_id))
            if scope in {'learning', 'knowledge', 'media', 'all_history'}:
                for table in ('rubric_calibration_event', 'rubric_calibration_protocol'):
                    if self._table_exists(connection, table):
                        counts[table] = self._delete(connection, f'DELETE FROM {table} WHERE child_id=?', child_id)
                counts['rubric_calibration_authorities'] = delete_calibration_authorities(connection, child_id)
                from .curriculum_authority_lifecycle import delete_authorities
                counts['curriculum_internalization_authorities'] = delete_authorities(connection, child_id)
            if scope in {'knowledge', 'all_history'}:
                if self._table_exists(connection, 'kg_parent_textbook_form_draft'):
                    from .knowledge_graph.parent_textbook_drafts import ParentTextbookDraftService
                    counts['parent_textbook_drafts'] = ParentTextbookDraftService.purge(connection, child_id)
                if self._table_exists(connection, 'knowledge_source_revision_event'):
                    counts['knowledge_source_revision_event'] = self._delete(connection,
                        'DELETE FROM knowledge_source_revision_event WHERE child_id=?', child_id)
                from functools import partial
                from .privacy_evidence_cleanup import purge_parent_textbook_sources
                from .knowledge_graph.privacy_cleanup import purge_imported
                from .privacy_native_sources import purge_native_sources
                if self._table_exists(connection, 'kg_parent_textbook_ocr_artifact'):
                    columns = {column[1] for column in connection.execute('PRAGMA table_info(kg_parent_textbook_ocr_artifact)')}
                    if 'raw_image_path' in columns:
                        raw_paths.extend(Path(row[0]) for row in connection.execute(
                            'SELECT raw_image_path FROM kg_parent_textbook_ocr_artifact WHERE child_id=? AND raw_image_path IS NOT NULL',
                            (child_id,)).fetchall())
                source_ids = {row['source_id'] for row in connection.execute(
                    'SELECT source_id FROM knowledge_source WHERE child_id=?', (child_id,))}
                if source_ids:
                    for table, count in purge_native_sources(connection, child_id, owner_source_ids=source_ids,
                        affected_scopes=affected_scopes).items():
                        counts[table] = counts.get(table, 0) + count
                for table, count in purge_parent_textbook_sources(connection, child_id,
                    purge_imported=partial(purge_imported, affected_scopes=affected_scopes)).items():
                    counts[table] = counts.get(table, 0) + count
            if scope == 'media' and self._table_exists(connection, 'kg_parent_textbook_ocr_artifact'):
                columns = {column[1] for column in connection.execute('PRAGMA table_info(kg_parent_textbook_ocr_artifact)')}
                if 'raw_image_path' in columns:
                    raw_paths.extend(Path(row[0]) for row in connection.execute(
                        'SELECT raw_image_path FROM kg_parent_textbook_ocr_artifact WHERE child_id=? AND raw_image_path IS NOT NULL',
                        (child_id,)).fetchall())
                    counts['kg_parent_textbook_raw_image'] = connection.execute(
                        """UPDATE kg_parent_textbook_ocr_artifact
                           SET raw_image_path=NULL,raw_image_media_type=NULL,raw_image_byte_length=0,
                               raw_image_encryption_version=NULL,updated_at=?
                           WHERE child_id=? AND raw_image_path IS NOT NULL""",
                        (utc_now(),child_id)).rowcount
            if scope in {"learning", "media", "all_history"}:
                if scope in {'learning', 'all_history'}:
                    for table in ('privacy_source_evidence_withdrawal', 'privacy_source_erasure'):
                        if self._table_exists(connection, table):
                            counts[table] = self._delete(connection, f'DELETE FROM {table} WHERE child_id=?', child_id)
                for table in ('writing_ocr_event', 'writing_ocr_confirmation'):
                    if self._table_exists(connection, table):
                        counts[table] = self._delete(connection, f'DELETE FROM {table} WHERE child_id=?', child_id)
                if scope == 'learning':
                    counts['writing_originals'] = self._delete(connection,
                        "DELETE FROM capture_artifact WHERE child_id=? AND artifact_type='writing_original'", child_id)
                if self._table_exists(connection, 'writing_reference_revision'):
                    counts['writing_reference_revision'] = self._delete(connection,
                        'DELETE FROM writing_reference_revision WHERE child_id=?', child_id)
                if self._table_exists(connection, 'writing_followup'):
                    counts['writing_followup'] = self._delete(connection,
                        'DELETE FROM writing_followup WHERE child_id=?', child_id)
                if scope in {'learning', 'all_history'} and self._table_exists(connection, 'privacy_preserved_child_response'):
                    counts['privacy_preserved_child_response'] = self._delete(connection,
                        'DELETE FROM privacy_preserved_child_response WHERE child_id=?', child_id)
                if self._table_exists(connection, 'voice_control_intent_request'):
                    counts['voice_control_intent_request'] = self._delete(connection,
                        'DELETE FROM voice_control_intent_request WHERE child_id=?', child_id)
                if self._table_exists(connection, 'break_control_receipt'):
                    counts['break_control_receipt'] = self._delete(connection,
                        'DELETE FROM break_control_receipt WHERE child_id=?', child_id)
                from .local_ocr.review_lifecycle import delete_review_scope
                counts.update(delete_review_scope(connection, child_id))
                from .local_ocr.cache_lifecycle import delete_scope as delete_ocr_cache_scope
                counts.update(delete_ocr_cache_scope(connection, child_id))
                if self._table_exists(connection, 'voice_capture_binding'):
                    capture_columns = {column[1] for column in connection.execute('PRAGMA table_info(voice_capture_binding)')}
                    reference_reset = ''.join(column + '=NULL,' for column in
                        ('question_reference_json', 'activity_reference_json', 'pending_evaluation_turn_id')
                        if column in capture_columns)
                    counts['voice_capture_cancelled'] = connection.execute(
                        f"UPDATE voice_capture_binding SET status='cancelled',prompt_id=NULL,item_version=NULL,{reference_reset}updated_at=? WHERE child_id=?",
                        (utc_now(), child_id)).rowcount
            if scope in {'learning', 'knowledge', 'media', 'all_history'}:
                from .visual_material_lifecycle import delete_scope as delete_visual_scope
                visual_counts, visual_paths = delete_visual_scope(connection, child_id)
                counts.update(visual_counts)
                raw_paths.extend(Path(path) for path in visual_paths)
            if self._table_exists(connection, 'execution_trace_event'):
                if scope in {'media', 'all_history'}:
                    counts['execution_trace_event'] = self._delete(connection, 'DELETE FROM execution_trace_event WHERE child_id=?', child_id)
                elif scope == 'reports':
                    counts['execution_trace_event'] = self._delete(connection,
                        "DELETE FROM execution_trace_event WHERE child_id=? AND source_kind='daily_report'", child_id)
                elif scope in {'learning', 'knowledge'}:
                    counts['execution_trace_event'] = self._delete(connection,
                        "DELETE FROM execution_trace_event WHERE child_id=? AND source_kind IN ('diagnostic_attempt','agent_outcome')", child_id)
            if scope in {"learning", "media", "all_history"}:
                # Keep the non-content origin marker so erased bindings cannot
                # turn an old replacement into an unrestricted new task.
                counts['replan_source_payloads'] = connection.execute("""UPDATE plan_item_event SET payload_json='{"source_deleted":true}'
                    WHERE event_type='revision_replanned' AND plan_item_id IN
                    (SELECT pi.plan_item_id FROM plan_item pi JOIN daily_plan dp USING(plan_id) WHERE dp.child_id=?)""", (child_id,)).rowcount
                for table in ("learning_input_carryover_plan_receipt", "learning_input_carryover_event",
                              "learning_input_replan_item", "learning_input_replan_unplaced", "learning_input_replan_command", "learning_input_replan_receipt",
                              "learning_input_command", "learning_input_revision_event", "learning_input_proposal", "learning_input_arrival"):
                    if self._table_exists(connection, table):
                        counts[table] = self._delete(connection, f"DELETE FROM {table} WHERE child_id=?", child_id)
            if scope in {"learning", "media", "all_history"} and self._table_exists(connection, "companion_activity_outcome"):
                # Events and frozen voice bindings cascade with their owner;
                # no self-report survives deletion of the learning/media source.
                counts["companion_activity_outcome"] = self._delete(
                    connection, "DELETE FROM companion_activity_outcome WHERE child_id=?", child_id)
            if scope in {'media', 'knowledge'}:
                from .privacy_evidence_cleanup import purge_rubric_sources
                for table, count in purge_rubric_sources(connection, child_id, all_child_audio=True,
                        reconcile=self.reconcile_source_deletion).items():
                    counts[table] = counts.get(table, 0) + count
            if scope in {'learning', 'knowledge', 'media', 'all_history'} and self._table_exists(connection, 'writing_question_authorization_event'):
                counts['writing_question_authorization_event'] = self._delete(
                    connection, 'DELETE FROM writing_question_authorization_event WHERE child_id=?', child_id)
            if scope in {'learning', 'knowledge', 'media', 'all_history'} and self._table_exists(connection, 'paper_variant_authorization_event'):
                from .paper_variant_authorization import delete_child_scope as delete_paper_variant_authority
                counts.update(delete_paper_variant_authority(connection, child_id, scope))
            if scope in {'learning','media','tutor','knowledge','all_history'} and self._table_exists(connection,'pet_multipa_execution_receipt'):
                from .pet_multipa_execution_receipt import delete_child_scope as delete_cpu_receipts
                counts.update(delete_cpu_receipts(connection,child_id,scope))
            if scope in {"learning","media","tutor","knowledge","all_history"} and self._table_exists(connection,'pet_speaking_candidate_job'):
                counts['pet_speaking_candidate_job']=self._delete(connection,'DELETE FROM pet_speaking_candidate_job WHERE child_id=?',child_id)
            if scope in {"learning", "media", "tutor", "knowledge", "all_history"} and self._table_exists(connection, 'pet_speaking_model_candidate'):
                from .pet_speaking_candidate_store import delete_child_scope as delete_speaking_model_candidates
                counts.update(delete_speaking_model_candidates(connection, child_id))
            if scope in {"learning", "media", "tutor", "knowledge", "all_history"} and self._table_exists(connection, 'pet_media_binding'):
                for table in ('pet_audio_work', 'pet_audio_grant'):
                    if self._table_exists(connection, table):
                        counts[table] = self._delete(connection, f'DELETE FROM {table} WHERE child_id=?', child_id)
                counts['pet_media_binding'] = self._delete(connection, 'DELETE FROM pet_media_binding WHERE child_id=?', child_id)
            if scope in {"learning", "tutor", "all_history"}:
                for table in ("rubric_review_event", "rubric_assessment"):
                    if self._table_exists(connection, table):
                        counts[table] = counts.get(table, 0) + self._delete(connection,
                            f"DELETE FROM {table} WHERE child_id=?", child_id)
            if scope in {"learning", "media", "tutor", "reports", "all_history"} and self._table_exists(connection, "learning_session_closure"):
                counts["learning_session_closure"] = self._delete(connection, "DELETE FROM learning_session_closure WHERE child_id=?", child_id)
            if scope in {"tutor", "all_history"}:
                if self._table_exists(connection, "bounded_experiment_event"):
                    counts["bounded_experiment_event"] = counts.get("bounded_experiment_event", 0) + self._delete(
                        connection,
                        "DELETE FROM bounded_experiment_event WHERE child_id=? AND protocol_id IN (SELECT protocol_id FROM bounded_experiment_protocol WHERE domain='teaching_strategy')",
                        child_id,
                    )
                if self._table_exists(connection, "bounded_experiment_protocol"):
                    counts["bounded_experiment_protocol"] = counts.get("bounded_experiment_protocol", 0) + self._delete(
                        connection,
                        "DELETE FROM bounded_experiment_protocol WHERE child_id=? AND domain='teaching_strategy'",
                        child_id,
                    )
                if self._table_exists(connection, "agent_step"):
                    tutoring = connection.execute("""SELECT DISTINCT p.agent_turn_id, p.objective_id
                        FROM agent_plan p JOIN agent_step s USING(plan_id)
                        WHERE p.child_id=? AND s.capability_name LIKE 'learning.%'""", (child_id,)).fetchall()
                    for turn in tutoring:
                        if self._table_exists(connection, "companion_memory_source"):
                            connection.execute("""DELETE FROM companion_memory WHERE child_id=? AND memory_id IN
                                (SELECT e.memory_id FROM companion_memory_evidence e JOIN companion_memory_source s USING(source_id) WHERE s.turn_id=?)""", (child_id, turn[0]))
                            connection.execute("DELETE FROM companion_memory_source WHERE child_id=? AND turn_id=?", (child_id, turn[0]))
                        if turn[1]:
                            connection.execute("DELETE FROM agent_objective WHERE child_id=? AND objective_id=?", (child_id, turn[1]))
                        connection.execute("DELETE FROM agent_turn WHERE child_id=? AND agent_turn_id=?", (child_id, turn[0]))
                    # Child-model snapshots are shared by planning, reports and
                    # bounded analysis. Tutor-only deletion must preserve them;
                    # learning/all_history deletes them later, after dependants.
                counts["tutor_feedback"] = self._delete(connection, "DELETE FROM tutor_feedback WHERE child_id = ?", child_id)
                if self._table_exists(connection, "tutor_playback_checkpoint"):
                    counts["tutor_playback_checkpoint"] = self._delete(connection, "DELETE FROM tutor_playback_checkpoint WHERE child_id = ?", child_id)
                if self._table_exists(connection, 'tutor_request_reservation'):
                    if self._table_exists(connection, 'tutor_request_attempt'):
                        counts['tutor_request_attempt'] = self._delete(connection,
                            'DELETE FROM tutor_request_attempt WHERE action_id IN (SELECT action_id FROM tutor_request_reservation WHERE child_id=?)', child_id)
                    counts['tutor_request_reservation'] = self._delete(connection,
                        'DELETE FROM tutor_request_reservation WHERE child_id=?', child_id)
                counts["tutor_turn"] = self._delete(connection, "DELETE FROM tutor_turn WHERE child_id = ?", child_id)
                counts["model_call"] = self._delete(connection, "DELETE FROM model_call WHERE child_id = ?", child_id)
                counts["strategy_evaluation_event"] = self._delete(connection, "DELETE FROM strategy_evaluation_event WHERE child_id = ?", child_id)
                counts["strategy_transition_event"] = self._delete(connection, "DELETE FROM strategy_transition_event WHERE child_id = ?", child_id)
                counts["strategy_experiment"] = self._delete(connection, "DELETE FROM strategy_experiment WHERE child_id = ?", child_id)
            if scope in {"learning", "media", "all_history"} and self._table_exists(connection, "document_observation_segment"):
                counts["document_observation_segment"] = self._delete(connection, "DELETE FROM document_observation_segment WHERE child_id = ?", child_id)
            if scope in {"learning", "media", "reports", "all_history"} and self._table_exists(connection, "daily_observation_report_source"):
                if self._table_exists(connection, 'daily_visual_schedule'):
                    counts['daily_visual_schedule'] = self._delete(connection, 'DELETE FROM daily_visual_schedule WHERE child_id=?', child_id)
                if self._table_exists(connection, 'daily_observation_delivery_ledger'):
                    if scope in {'reports','all_history'}:
                        unresolved = connection.execute("SELECT 1 FROM daily_observation_delivery_ledger WHERE child_id=? AND status!='ready' LIMIT 1", (child_id,)).fetchone()
                        if unresolved:
                            connection.execute("""INSERT INTO daily_observation_delivery_barrier VALUES(1,1,'DELIVERY_HISTORY_ERASED')
                                ON CONFLICT(singleton_id) DO UPDATE SET blocked=1,reason='DELIVERY_HISTORY_ERASED'""")
                        counts['daily_observation_delivery_ledger'] = self._delete(connection, 'DELETE FROM daily_observation_delivery_ledger WHERE child_id=?', child_id)
                    else:
                        counts['daily_observation_delivery_ready'] = self._delete(connection, "DELETE FROM daily_observation_delivery_ledger WHERE child_id=? AND status='ready'", child_id)
                if self._table_exists(connection, 'daily_observation_report_snapshot'):
                    counts['daily_observation_report_snapshot'] = self._delete(connection, 'DELETE FROM daily_observation_report_snapshot WHERE child_id = ?', child_id)
                counts["daily_observation_report_source"] = self._delete(connection, "DELETE FROM daily_observation_report_source WHERE child_id = ?", child_id)
            if scope in {'reports', 'all_history'} and self._table_exists(connection, 'daily_report_email_verification'):
                counts['daily_report_email_verification'] = self._delete(connection, 'DELETE FROM daily_report_email_verification WHERE child_id = ?', child_id)
            if scope in {"media", "all_history"}:
                if self._table_exists(connection, "bounded_experiment_event"):
                    counts["bounded_experiment_event"] = counts.get("bounded_experiment_event", 0) + self._delete(
                        connection,
                        "DELETE FROM bounded_experiment_event WHERE child_id=? AND protocol_id IN (SELECT protocol_id FROM bounded_experiment_protocol WHERE domain='behavior_intervention')",
                        child_id,
                    )
                if self._table_exists(connection, "bounded_experiment_protocol"):
                    counts["bounded_experiment_protocol"] = counts.get("bounded_experiment_protocol", 0) + self._delete(
                        connection,
                        "DELETE FROM bounded_experiment_protocol WHERE child_id=? AND domain='behavior_intervention'",
                        child_id,
                    )
                if self._table_exists(connection, "companion_activity"):
                    counts["companion_activity"] = self._delete(
                        connection, "DELETE FROM companion_activity WHERE child_id=?", child_id)
                if self._table_exists(connection, "controlled_content_session"):
                    counts["controlled_content_session"] = self._delete(
                        connection, "DELETE FROM controlled_content_session WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "controlled_game_session"):
                    counts["controlled_game_session"] = self._delete(
                        connection, "DELETE FROM controlled_game_session WHERE child_id = ?", child_id
                    )
                raw_paths += [
                    Path(row[0])
                    for table in ("voice_input", "capture_artifact")
                    for row in connection.execute(f"SELECT local_path FROM {table} WHERE child_id = ? AND local_path IS NOT NULL", (child_id,)).fetchall()
                ]
                connection.execute(
                    "UPDATE plan_item SET completion_artifact_id = NULL WHERE completion_artifact_id IN (SELECT artifact_id FROM capture_artifact WHERE child_id = ?)",
                    (child_id,),
                )
                connection.execute(
                    "UPDATE homework_item SET source_artifact_id = NULL, ocr_confidence = NULL WHERE source_artifact_id IN (SELECT artifact_id FROM capture_artifact WHERE child_id = ?)",
                    (child_id,),
                )
                # Analysis/vision attempts may restrict deletion of revision or
                # capture sources. Delete dependants first, without needing the
                # optional feature schemas to be installed on older databases.
                for document_table in ("document_operation_receipt", "document_operation_delivery", "document_vision_attempt", "document_learning_analysis", "document_vision_request", "document_monitor_request"):
                    if self._table_exists(connection, document_table):
                        counts[document_table] = self._delete(connection, f"DELETE FROM {document_table} WHERE child_id = ?", child_id)
                if self._table_exists(connection, "document_page_revision"):
                    counts["document_page_revision"] = self._delete(connection, "DELETE FROM document_page_revision WHERE child_id = ?", child_id)
                if self._table_exists(connection, "document_monitor_event"):
                    counts["document_monitor_event"] = self._delete(connection, "DELETE FROM document_monitor_event WHERE child_id = ?", child_id)
                if self._table_exists(connection, "document_monitor_session"):
                    counts["document_monitor_session"] = self._delete(connection, "DELETE FROM document_monitor_session WHERE child_id = ?", child_id)
                counts["voice_input"] = self._delete(connection, "DELETE FROM voice_input WHERE child_id = ?", child_id)
                if self._table_exists(connection, "cloud_voice_interaction"):
                    counts["cloud_voice_interaction"] = self._delete(
                        connection, "DELETE FROM cloud_voice_interaction WHERE child_id = ?", child_id
                    )
                counts["capture_artifact"] = self._delete(connection, "DELETE FROM capture_artifact WHERE child_id = ?", child_id)
                counts["child_safety_event"] = self._delete(connection, "DELETE FROM child_safety_event WHERE child_id = ?", child_id)
                counts["agent_turn"] = self._delete(connection, "DELETE FROM agent_turn WHERE child_id = ?", child_id)
                if self._table_exists(connection, "kg_local_course_context_event"):
                    counts["kg_local_course_context_event"] = self._delete(connection, "DELETE FROM kg_local_course_context_event WHERE child_id = ?", child_id)
                for agent_table in ("agent_commitment", "agent_objective"):
                    if self._table_exists(connection, agent_table):
                        counts[agent_table] = self._delete(connection, f"DELETE FROM {agent_table} WHERE child_id = ?", child_id)
                for memory_table in ("companion_memory", "companion_memory_source", "companion_memory_audit", "companion_memory_checkpoint", "companion_memory_suppression", "companion_memory_document_source", "companion_memory_document_checkpoint"):
                    if self._table_exists(connection, memory_table):
                        counts[memory_table] = self._delete(connection, f"DELETE FROM {memory_table} WHERE child_id = ?", child_id)
                counts["intervention_record"] = self._delete(connection, "DELETE FROM intervention_record WHERE child_id = ?", child_id)
                counts["behavior_stop_condition"] = self._delete(connection, "DELETE FROM behavior_stop_condition WHERE child_id = ?", child_id)
                if self._table_exists(connection, "habit_formation_event"):
                    counts["habit_formation_event"] = self._delete(connection, "DELETE FROM habit_formation_event WHERE child_id = ?", child_id)
                if self._table_exists(connection, "habit_formation_state"):
                    counts["habit_formation_state"] = self._delete(connection, "DELETE FROM habit_formation_state WHERE child_id = ?", child_id)
                if self._table_exists(connection, "habit_experiment_event"):
                    counts["habit_experiment_event"] = self._delete(connection, "DELETE FROM habit_experiment_event WHERE child_id = ?", child_id)
                if self._table_exists(connection, "habit_experiment"):
                    counts["habit_experiment"] = self._delete(connection, "DELETE FROM habit_experiment WHERE child_id = ?", child_id)
                counts["behavior_intervention"] = self._delete(connection, "DELETE FROM intervention WHERE child_id = ?", child_id)
                counts["behavior_retraction"] = self._delete(connection, "DELETE FROM behavior_retraction WHERE child_id = ?", child_id)
                counts["probabilistic_state"] = self._delete(connection, "DELETE FROM probabilistic_state WHERE child_id = ?", child_id)
                counts["observable_event"] = self._delete(connection, "DELETE FROM observable_event WHERE child_id = ?", child_id)
                counts["daily_feature"] = self._delete(connection, "DELETE FROM daily_feature WHERE child_id = ?", child_id)
                counts["intervention_policy_reset"] = self._delete(connection, "DELETE FROM intervention_policy_reset WHERE child_id = ?", child_id)
                counts["observation_correction"] = self._delete(connection, "DELETE FROM observation_correction WHERE child_id = ?", child_id)
                counts["observation_qualification"] = self._delete(connection, "DELETE FROM observation_qualification WHERE child_id = ?", child_id)
                counts["child_state_event"] = self._delete(connection, "DELETE FROM child_state_event WHERE child_id = ?", child_id)
                counts["observation_calibration_run"] = self._delete(connection, "DELETE FROM observation_calibration_run WHERE child_id = ?", child_id)
            if scope in {"reports", "all_history"}:
                counts['report_delivery_recipient_authority_events'] = self._delete(connection,
                    "DELETE FROM audit_event WHERE child_id=? AND action IN ('report_delivery_test_recipient_selected','report_delivery_recipient_authority_changed','report_delivery_auto_verification_blocked')", child_id)
                counts['parent_report_source_issues'] = self._delete(connection,
                    "DELETE FROM audit_event WHERE child_id=? AND action='parent_report_source_issue'", child_id)
                for table in ('report_rebinding_command', 'report_adjustment_binding', 'report_rebinding_cancelled_command', 'report_plan_feedback_receipt', 'report_plan_adjustment_event', 'report_feedback_revision'):
                    if self._table_exists(connection, table):
                        counts[table] = self._delete(connection, f'DELETE FROM {table} WHERE child_id = ?', child_id)
                # Report snapshots are a delivery-time copy of report evidence. They
                # must be erased before the live source report, otherwise their
                # immutable-history FK would keep deleted learning data on device.
                delivery_tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('parent_report_snapshot', 'parent_report_snapshot_correction', 'report_delivery_outbox', 'report_delivery_recipient', 'report_delivery_preference')"
                    ).fetchall()
                }
                if "parent_report_snapshot" in delivery_tables:
                    if "parent_report_snapshot_correction" in delivery_tables:
                        counts["parent_report_snapshot_correction"] = connection.execute("""
                            SELECT COUNT(*) FROM parent_report_snapshot_correction c
                            JOIN parent_report_snapshot s ON s.snapshot_id = c.snapshot_id
                            WHERE s.child_id = ?
                        """, (child_id,)).fetchone()[0]
                    if "report_delivery_outbox" in delivery_tables:
                        # Erasing a legacy receipt must not erase our knowledge
                        # that a daily report may already have been submitted.
                        if self._table_exists(connection, 'daily_observation_delivery_barrier') and connection.execute('''
                            SELECT 1 FROM report_delivery_outbox o
                            JOIN parent_report_snapshot s ON s.snapshot_id=o.snapshot_id
                            WHERE s.child_id=? AND s.report_type='daily'
                              AND (o.status IN ('sent','processing','unknown','unknown_acknowledged','superseded')
                                   OR o.attempt_count>0) LIMIT 1''', (child_id,)).fetchone():
                            connection.execute("""INSERT INTO daily_observation_delivery_barrier VALUES(1,1,'DELIVERY_HISTORY_ERASED')
                                ON CONFLICT(singleton_id) DO UPDATE SET blocked=1,reason='DELIVERY_HISTORY_ERASED'""")
                        counts["report_delivery_outbox"] = connection.execute(
                            "DELETE FROM report_delivery_outbox WHERE snapshot_id IN (SELECT snapshot_id FROM parent_report_snapshot WHERE child_id = ?)",
                            (child_id,),
                        ).rowcount
                    counts["parent_report_snapshot"] = self._delete(
                        connection, "DELETE FROM parent_report_snapshot WHERE child_id = ?", child_id
                    )
                    if "report_delivery_recipient" in delivery_tables:
                        counts["report_delivery_recipient"] = self._delete(
                            connection, "DELETE FROM report_delivery_recipient WHERE child_id = ?", child_id
                        )
                    if "report_delivery_preference" in delivery_tables:
                        counts["report_delivery_preference"] = self._delete(
                            connection, "DELETE FROM report_delivery_preference WHERE child_id = ?", child_id
                        )
                counts["period_report_refresh_state"] = self._delete(connection, "DELETE FROM period_report_refresh_state WHERE child_id = ?", child_id)
                counts["report_retry_request"] = self._delete(connection, "DELETE FROM report_retry_request WHERE child_id = ?", child_id)
                counts["parent_report_feedback"] = self._delete(connection, "DELETE FROM parent_report_feedback WHERE child_id = ?", child_id)
                for table in ("parent_action_feedback_event", "parent_action_snapshot"):
                    if self._table_exists(connection, table):
                        counts[table] = self._delete(connection, f"DELETE FROM {table} WHERE child_id = ?", child_id)
                counts["daily_report"] = self._delete(connection, "DELETE FROM daily_report WHERE child_id = ?", child_id)
                counts["period_report"] = self._delete(connection, "DELETE FROM period_report WHERE child_id = ?", child_id)
                counts["report_job"] = self._delete(connection, "DELETE FROM report_job WHERE child_id = ?", child_id)
            if scope in {"learning", "knowledge", "all_history"} and self._table_exists(connection, "curriculum_supply_package"):
                connection.execute("""UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id IN
                    (SELECT sc.diagnostic_item_id FROM specialized_supply_candidate sc
                     JOIN curriculum_supply_package cp USING(candidate_id) WHERE cp.child_id=?)""", (child_id,))
                for table in ("curriculum_supply_event", "curriculum_supply_package", "curriculum_supply_selection"):
                    counts[table] = self._delete(connection, f"DELETE FROM {table} WHERE child_id = ?", child_id)
            if scope in {"learning", "all_history"}:
                if self._table_exists(connection, "specialized_exam_section_state"):
                    counts["specialized_exam_section_state"] = connection.execute(
                        "DELETE FROM specialized_exam_section_state WHERE run_id IN (SELECT run_id FROM specialized_exam_run WHERE child_id = ?)",
                        (child_id,),
                    ).rowcount
                if self._table_exists(connection, "specialized_exam_item"):
                    counts["specialized_exam_item"] = connection.execute(
                        "DELETE FROM specialized_exam_item WHERE run_id IN (SELECT run_id FROM specialized_exam_run WHERE child_id = ?)",
                        (child_id,),
                    ).rowcount
                if self._table_exists(connection, "specialized_exam_run"):
                    counts["specialized_exam_run"] = self._delete(
                        connection, "DELETE FROM specialized_exam_run WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "pet_vocabulary_prompt_session"):
                    counts["pet_vocabulary_prompt_session"] = self._delete(
                        connection, "DELETE FROM pet_vocabulary_prompt_session WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "pet_vocabulary_attempt"):
                    counts["pet_vocabulary_attempt"] = self._delete(
                        connection, "DELETE FROM pet_vocabulary_attempt WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "pet_vocabulary_card"):
                    counts["pet_vocabulary_card"] = self._delete(
                        connection, "DELETE FROM pet_vocabulary_card WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "specialized_program_snapshot"):
                    counts["specialized_program_snapshot"] = self._delete(
                        connection, "DELETE FROM specialized_program_snapshot WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "specialized_assessment_run"):
                    counts["specialized_assessment_run"] = self._delete(
                        connection, "DELETE FROM specialized_assessment_run WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "specialized_assessment_event"):
                    counts["specialized_assessment_event"] = self._delete(
                        connection, "DELETE FROM specialized_assessment_event WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "analysis_candidate"):
                    from .analysis_candidate import delete_child_scope as delete_analysis_candidates
                    counts.update(delete_analysis_candidates(connection, child_id))
                    residual_analysis = {
                        table: connection.execute(f"SELECT COUNT(*) FROM {table} WHERE child_id=?", (child_id,)).fetchone()[0]
                        for table in ("analysis_candidate_ingest_attempt", "analysis_candidate",
                                      "analysis_candidate_event", "child_analysis_review_job")
                        if self._table_exists(connection, table)
                    }
                    if any(residual_analysis.values()):
                        print("HSC_DIAGNOSTIC " + json.dumps({
                            "event": "privacy_analysis_candidate_residual",
                            "level": "error",
                            "details": residual_analysis,
                        }, ensure_ascii=False), flush=True)
                if self._table_exists(connection, "child_model_snapshot"):
                    counts["child_model_snapshot"] = self._delete(connection, "DELETE FROM child_model_snapshot WHERE child_id = ?", child_id)
                if self._table_exists(connection, "child_model_correction_event"):
                    counts["child_model_correction_event"] = self._delete(connection, "DELETE FROM child_model_correction_event WHERE child_id = ?", child_id)
                if self._table_exists(connection, "child_model_correction"):
                    counts["child_model_correction"] = self._delete(connection, "DELETE FROM child_model_correction WHERE child_id = ?", child_id)
                if self._table_exists(connection, "goal_cycle"):
                    counts["goal_cycle"] = self._delete(connection, "DELETE FROM goal_cycle WHERE child_id = ?", child_id)
                if self._table_exists(connection, "goal_program"):
                    counts["goal_program"] = self._delete(connection, "DELETE FROM goal_program WHERE child_id = ?", child_id)
                if self._table_exists(connection, "variant_candidate"):
                    counts["variant_candidate"] = self._delete(connection, "DELETE FROM variant_candidate WHERE child_id = ?", child_id)
                if self._table_exists(connection, "variant_generation_job"):
                    counts["variant_generation_job"] = self._delete(connection, "DELETE FROM variant_generation_job WHERE child_id = ?", child_id)
                if self._table_exists(connection, "specialized_supply_candidate"):
                    # Preserve referential integrity while erasing owned original-check publications.
                    if self._table_exists(connection, "specialized_supply_job"):
                        counts["document_check_publication"] = connection.execute("""UPDATE diagnostic_item
                            SET status='retired',prompt='',expected_answer_json='{}',explanation='',
                                stimulus_text=NULL,stimulus_asset=NULL,source_ref='deleted_local_supply'
                            WHERE diagnostic_item_id IN
                                (SELECT sc.diagnostic_item_id FROM specialized_supply_candidate sc
                                 JOIN specialized_supply_job sj ON sj.job_id=sc.job_id
                                 WHERE sc.child_id=? AND sj.child_id=?
                                   AND diagnostic_item.source_ref='local_verified_specialized:'||sc.candidate_id
                                   AND sj.domain_id LIKE 'document-check:%')""", (child_id, child_id)).rowcount
                    counts["specialized_supply_candidate"] = self._delete(
                        connection, "DELETE FROM specialized_supply_candidate WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "specialized_supply_job"):
                    counts["specialized_supply_job"] = self._delete(
                        connection, "DELETE FROM specialized_supply_job WHERE child_id = ?", child_id
                    )
                counts["weekly_goal"] = self._delete(connection, "DELETE FROM weekly_goal WHERE child_id = ?", child_id)
                for table in ('mistake_capture_candidate_event', 'mistake_capture_candidate'):
                    counts[table] = self._delete(connection, f'DELETE FROM {table} WHERE child_id = ?', child_id)
                counts["mistake_status_event"] = self._delete(connection, "DELETE FROM mistake_status_event WHERE child_id = ?", child_id)
                counts["mistake_dialogue"] = self._delete(connection, "DELETE FROM mistake_dialogue WHERE child_id = ?", child_id)
                counts["mistake_dialogue_event"] = self._delete(connection, "DELETE FROM mistake_dialogue_event WHERE child_id = ?", child_id)
                if self._table_exists(connection, 'mistake_source_revision'):
                    counts['mistake_source_revision'] = self._delete(connection, 'DELETE FROM mistake_source_revision WHERE child_id=?', child_id)
                counts["mistake_attempt"] = self._delete(connection, "DELETE FROM mistake_attempt WHERE child_id = ?", child_id)
                if self._table_exists(connection, "mistake_diagnosis_event"):
                    from .mistake_diagnosis import MistakeDiagnosisService
                    MistakeDiagnosisService(self.database).ensure_schema(connection)
                    counts["mistake_diagnosis_event"] = self._delete(
                        connection, "DELETE FROM mistake_diagnosis_event WHERE child_id = ?", child_id
                    )
                counts["remediation_step"] = self._delete(connection, "DELETE FROM remediation_step WHERE child_id = ?", child_id)
                counts["mistake_diagnosis"] = self._delete(connection, "DELETE FROM mistake_diagnosis WHERE child_id = ?", child_id)
                counts["mistake_occurrence"] = self._delete(connection, "DELETE FROM mistake_occurrence WHERE child_id = ?", child_id)
                counts["mistake_mastery_gate"] = connection.execute(
                    "DELETE FROM mistake_mastery_gate WHERE mistake_id IN (SELECT mistake_id FROM mistake_case WHERE child_id = ?)",
                    (child_id,),
                ).rowcount
                counts["mistake_case"] = self._delete(connection, "DELETE FROM mistake_case WHERE child_id = ?", child_id)
                counts["mistake_cluster"] = self._delete(connection, "DELETE FROM mistake_cluster WHERE child_id = ?", child_id)
                for table in ("paper_learning_selection_event", "paper_learning_selection"):
                    if self._table_exists(connection, table):
                        counts[table] = self._delete(connection, f"DELETE FROM {table} WHERE child_id = ?", child_id)
                if self._table_exists(connection, "diagnostic_prompt"):
                    counts["diagnostic_prompt"] = self._delete(connection, "DELETE FROM diagnostic_prompt WHERE child_id = ?", child_id)
                counts["diagnostic_attempt"] = self._delete(connection, "DELETE FROM diagnostic_attempt WHERE child_id = ?", child_id)
                counts["review_task"] = self._delete(connection, "DELETE FROM review_task WHERE child_id = ?", child_id)
                counts["mastery_update_event"] = self._delete(connection, "DELETE FROM mastery_update_event WHERE child_id = ?", child_id)
                counts["learning_evidence"] = self._delete(connection, "DELETE FROM learning_evidence WHERE child_id = ?", child_id)
                counts["mastery_state"] = self._delete(connection, "DELETE FROM mastery_state WHERE child_id = ?", child_id)
            if scope in {"knowledge", "all_history"}:
                counts["knowledge_excerpt"] = counts.get("knowledge_excerpt", 0) + connection.execute(
                    "DELETE FROM knowledge_excerpt WHERE source_id IN (SELECT source_id FROM knowledge_source WHERE child_id = ?)",
                    (child_id,),
                ).rowcount
                counts["knowledge_source"] = counts.get("knowledge_source", 0) + self._delete(connection, "DELETE FROM knowledge_source WHERE child_id = ?", child_id)
            if scope == "all_history":
                if self._table_exists(connection, "controlled_game_approval"):
                    counts["controlled_game_approval"] = self._delete(
                        connection, "DELETE FROM controlled_game_approval WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "controlled_game_rule"):
                    counts["controlled_game_rule"] = self._delete(
                        connection, "DELETE FROM controlled_game_rule WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "controlled_content_approval"):
                    counts["controlled_content_approval"] = self._delete(
                        connection, "DELETE FROM controlled_content_approval WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "controlled_content_rule"):
                    counts["controlled_content_rule"] = self._delete(
                        connection, "DELETE FROM controlled_content_rule WHERE child_id = ?", child_id
                    )
                if self._table_exists(connection, "agent_proactive_prompt"):
                    counts["agent_proactive_prompt"] = self._delete(connection, "DELETE FROM agent_proactive_prompt WHERE child_id = ?", child_id)
                counts["deferred_task_review"] = self._delete(
                    connection, "DELETE FROM deferred_task_review WHERE child_id = ?", child_id
                )
                session_ids = [row[0] for row in connection.execute("SELECT session_id FROM daily_session WHERE child_id = ?", (child_id,)).fetchall()]
                for session_id in session_ids:
                    counts["sensor_release_verification"] = counts.get("sensor_release_verification", 0) + connection.execute("DELETE FROM sensor_release_verification WHERE session_id = ?", (session_id,)).rowcount
                    counts["session_draft"] = counts.get("session_draft", 0) + connection.execute("DELETE FROM session_draft WHERE session_id = ?", (session_id,)).rowcount
                    counts["device_incident_report"] = counts.get("device_incident_report", 0) + connection.execute("DELETE FROM device_incident_report WHERE incident_id IN (SELECT incident_id FROM device_incident WHERE session_id = ?)", (session_id,)).rowcount
                    counts["device_incident"] = counts.get("device_incident", 0) + connection.execute("DELETE FROM device_incident WHERE session_id = ?", (session_id,)).rowcount
                    counts["sensor_lease"] = counts.get("sensor_lease", 0) + connection.execute("DELETE FROM sensor_lease WHERE session_id = ?", (session_id,)).rowcount
                    counts["planning_context_event"] = counts.get("planning_context_event", 0) + connection.execute("DELETE FROM planning_context_event WHERE session_id = ?", (session_id,)).rowcount
                    counts["session_planning_context"] = counts.get("session_planning_context", 0) + connection.execute("DELETE FROM session_planning_context WHERE session_id = ?", (session_id,)).rowcount
                    counts["homework_revision"] = counts.get("homework_revision", 0) + connection.execute("DELETE FROM homework_revision WHERE session_id = ?", (session_id,)).rowcount
                    plan_ids = [row[0] for row in connection.execute("SELECT plan_id FROM daily_plan WHERE session_id = ?", (session_id,)).fetchall()]
                    for plan_id in plan_ids:
                        if self._table_exists(connection, "plan_constraint_decision"):
                            counts["plan_constraint_decision"] = counts.get("plan_constraint_decision", 0) + connection.execute(
                                "DELETE FROM plan_constraint_decision WHERE plan_id=?", (plan_id,)
                            ).rowcount
                        counts["plan_revision"] = counts.get("plan_revision", 0) + connection.execute("DELETE FROM plan_revision WHERE plan_id = ?", (plan_id,)).rowcount
                        counts["plan_item_event"] = counts.get("plan_item_event", 0) + connection.execute("DELETE FROM plan_item_event WHERE plan_item_id IN (SELECT plan_item_id FROM plan_item WHERE plan_id = ?)", (plan_id,)).rowcount
                        counts["plan_item"] = counts.get("plan_item", 0) + connection.execute("DELETE FROM plan_item WHERE plan_id = ?", (plan_id,)).rowcount
                    counts["recall_review"] = counts.get("recall_review", 0) + connection.execute("DELETE FROM recall_review WHERE session_id = ?", (session_id,)).rowcount
                    for table in ("daily_plan", "homework_item", "classroom_recall", "daily_reflection", "session_declaration", "session_transition"):
                        counts[table] = counts.get(table, 0) + connection.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,)).rowcount
                counts["daily_session"] = self._delete(connection, "DELETE FROM daily_session WHERE child_id = ?", child_id)
            # Every source copy has been validated/erased before state consumers run.
            reconcile_children = set()
            try:
                for affected_child, node_ids in sorted(affected_scopes.items()):
                    if scope == 'all_history' and affected_child == child_id:
                        continue
                    if not self.replay_source_mastery or not self.reconcile_source_deletion:
                        from .privacy_evidence_cleanup import PrivacyEvidenceCleanupError
                        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_RECONCILE_REQUIRED')
                    for node_id in sorted(node_ids):
                        self.replay_source_mastery(affected_child, node_id,
                            connection=connection, reason='source_deleted')
                    reconcile_children.add(affected_child)
                if scope == 'knowledge' and self.reconcile_source_deletion:
                    reconcile_children.add(child_id)
                for affected_child in sorted(reconcile_children):
                    self.reconcile_source_deletion(affected_child, connection=connection)
            except (ValueError, KeyError, sqlite3.Error, OSError) as error:
                from .privacy_evidence_cleanup import PrivacyEvidenceCleanupError
                raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_RECONCILE_FAILED') from error
            connection.execute(
                """
                UPDATE deletion_job SET status = 'deleting', deleted_counts_json = ?,
                  residual_paths_json = ?, verification_json = ?, updated_at = ?,
                  error_code = NULL, error_message = NULL
                WHERE deletion_id = ?
                """,
                (
                    json.dumps(counts), json.dumps([str(path) for path in raw_paths] + list(managed_export_hashes)),
                    json.dumps({"database_delete_committed": True, 'managed_export_hashes':managed_export_hashes}), utc_now(), deletion_id,
                ),
            )
            self._audit(connection, "parent", "child_data_deleted", child_id, {"deletion_id": deletion_id, "scope": scope, "counts": counts}, utc_now())
            self._commit_original_deletion(connection,job,native_owned=owned_privacy_preimages)
        return self._finish_deletion_job(deletion_id)

    def _deletion_generation_current(self, connection, job) -> bool:
        if not self._table_exists(connection, 'privacy_operation_intent'):
            return True
        operation = connection.execute('SELECT * FROM privacy_operation_intent WHERE request_id=?',
                                       (job['client_action_id'],)).fetchone()
        if operation is None:
            return True  # Legacy jobs without an operation receipt keep their existing contract.
        current = connection.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone()
        return bool(current and operation['kind'] == 'delete'
                    and operation['child_id'] == job['child_id'] and operation['scope'] == job['scope']
                    and operation['origin_generation'] == current['generation'])

    def require_current_deletion(self, deletion_id: str) -> None:
        from .privacy_operation_receipt import PrivacyOperationError
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            job = connection.execute('SELECT * FROM deletion_job WHERE deletion_id=?', (deletion_id,)).fetchone()
            if job is None:
                raise UnknownDeletionJobError(deletion_id)
            if not self._deletion_generation_current(connection, job):
                raise PrivacyOperationError('PRIVACY_OPERATION_GENERATION_CHANGED')

    @_serialized_deletion
    def retry_deletion_job(self, deletion_id: str, confirmation: str) -> dict:
        if confirmation != "确认删除":
            raise DeletionConfirmationError()
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT * FROM deletion_job WHERE deletion_id = ?", (deletion_id,)
            ).fetchone()
        if not job:
            raise UnknownDeletionJobError(deletion_id)
        return self._resume_deletion_job(deletion_id)

    def recover_interrupted_deletions(self) -> int:
        now = utc_now()
        placeholders = ",".join("?" for _ in INTERRUPTED_DELETION_STATUSES)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT deletion_id FROM deletion_job WHERE status IN ({placeholders})",
                tuple(INTERRUPTED_DELETION_STATUSES),
            ).fetchall()
            if rows:
                connection.execute(
                    f"""
                    UPDATE deletion_job SET status = 'failed', error_code = 'APP_RESTART',
                      error_message = '删除清理在应用退出前未完成，请重试。', updated_at = ?
                    WHERE status IN ({placeholders})
                    """,
                    (now, *tuple(INTERRUPTED_DELETION_STATUSES)),
                )
                self._audit(
                    connection, "system", "deletion_jobs_interrupted", None,
                    {"count": len(rows)}, now,
                )
            connection.commit()
        return len(rows)

    def _create_deletion_job(
        self, child_id: str, scope: str, target_type: str,
        target_id: str, client_action_id: str,
    ) -> str:
        deletion_id, now = str(uuid4()), utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_child(connection, child_id)
            connection.execute(
                """
                INSERT INTO deletion_job(
                  deletion_id, child_id, scope, target_type, target_id, status,
                  deleted_counts_json, residual_paths_json, verification_json,
                  client_action_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'requested', '{}', '[]', '{}', ?, ?, ?)
                """,
                (deletion_id, child_id, scope, target_type, target_id, client_action_id, now, now),
            )
            connection.execute(
                "UPDATE deletion_job SET status = 'scanning', updated_at = ? WHERE deletion_id = ?",
                (now, deletion_id),
            )
            _combined_privacy_batches(connection,child_id=child_id)
            connection.commit()
        return deletion_id

    def _resume_deletion_job(self, deletion_id: str) -> dict:
        # Restore and explicit retry share the guard; a generation cannot change
        # between validating the original authority and finishing its filesystem work.
        with self.database.external_effect_guard():
            self.require_current_deletion(deletion_id)
            return self._resume_current_deletion_job(deletion_id)

    def _resume_current_deletion_job(self, deletion_id: str) -> dict:
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT * FROM deletion_job WHERE deletion_id = ?", (deletion_id,)
            ).fetchone()
            if job and job['status'] != 'completed':
                # Jobs admitted by an older version may predate the combined
                # erasure hook.  Preserve the same guard/transaction boundary
                # before resuming their original destructive work.
                connection.execute('BEGIN IMMEDIATE')
                _combined_privacy_batches(connection,child_id=job['child_id'])
                connection.commit()
        if not job:
            raise UnknownDeletionJobError(deletion_id)
        if job["status"] == "completed":
            return self._serialize_deletion_job(job)
        if job['target_type'] == 'knowledge_source':
            return self._execute_knowledge_source_deletion(deletion_id)
        verification = json.loads(job["verification_json"] or "{}")
        if not verification.get("database_delete_committed"):
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE deletion_job SET status = 'scanning', error_code = NULL, error_message = NULL, updated_at = ? WHERE deletion_id = ?",
                    (utc_now(), deletion_id),
                )
                connection.commit()
            return (
                self._execute_capture_deletion(deletion_id)
                if job["target_type"] == "capture_artifact"
                else self._execute_capture_raw_media_deletion(deletion_id)
                if job["target_type"] == "capture_artifact_raw_media"
                else self._execute_child_scope_deletion(deletion_id)
            )
        return self._finish_deletion_job(deletion_id)

    def _finish_deletion_job(self, deletion_id: str) -> dict:
        from .privacy_evidence_cleanup import PrivacyEvidenceCleanupError
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT * FROM deletion_job WHERE deletion_id = ?", (deletion_id,)
            ).fetchone()
        if (self.clear_ephemeral_child and json.loads(job["verification_json"] or "{}").get("database_delete_committed")
                and (job['target_type'] in {'capture_artifact', 'knowledge_source'} or job['target_type'] == 'child_scope'
                     and job['scope'] in {'knowledge', 'learning', 'media', 'all_history'})):
            # Database rollback must not erase live playback provenance. Once
            # committed, clear it even if a later media-file cleanup must retry.
            self.clear_ephemeral_child(job['child_id'])
        residuals = [str(value) for value in json.loads(job["residual_paths_json"] or "[]")]
        remaining_paths: list[str] = []
        deleted_files = 0
        deleted_exports = 0
        media_root = io_resolve(self.data_dir / "media",)
        export_root = io_resolve(self.data_dir / 'exports',)
        verification = json.loads(job["verification_json"] or "{}")
        export_hashes = verification.get('managed_export_hashes', {})
        for raw_path in residuals:
            try:
                original_path = Path(raw_path)
                from .parent_restore_assets import erase_original_privacy_member
                original_erased=erase_original_privacy_member(self.database,job,original_path)
                if original_erased is not None:
                    deleted_files+=int(original_erased)
                    continue
                path = io_resolve(original_path,)
                export_copy = (raw_path in export_hashes or job['target_type'] == 'knowledge_source'
                               and original_path.parent == export_root
                               and original_path.name.startswith('child-export-' + job['child_id'] + '-')
                               and original_path.suffix == '.json')
                if export_copy:
                    from .knowledge_source_deletion import export_copies, verify_export_copy
                    expected_hash = export_hashes.get(raw_path)
                    if not expected_hash:
                        with self.database.connect() as connection:
                            registered = export_copies(self.data_dir, job['child_id'], job['target_id'],
                                                       connection=connection, include_hashes=True)
                        expected_hash = registered.get(raw_path)
                    verify_export_copy(self.data_dir, job['child_id'], original_path, expected_hash)
                if not export_copy and path != media_root and media_root not in path.parents:
                    raise OSError("path outside managed media directory")
                existed = io_path(path).is_file()
                io_path(path).unlink(missing_ok=True)
                if export_copy:
                    deleted_exports += int(existed)
                else:
                    deleted_files += int(existed)
            except (OSError, PrivacyEvidenceCleanupError):
                remaining_paths.append(raw_path)
        counts = json.loads(job["deleted_counts_json"] or "{}")
        if deleted_files:
            counts['raw_media_files'] = counts.get('raw_media_files', 0) + deleted_files
        if deleted_exports:
            counts['local_export_files'] = counts.get('local_export_files', 0) + deleted_exports
        if job["target_type"] in {"capture_artifact", "capture_artifact_raw_media"}:
            verification["raw_file_deleted"] = not remaining_paths
        if remaining_paths:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE deletion_job SET status = 'failed', deleted_counts_json = ?,
                      residual_paths_json = ?, verification_json = ?,
                      error_code = 'FILE_DELETE_FAILED',
                      error_message = '仍有受管理媒体文件未能删除，请关闭占用程序后重试。',
                      updated_at = ? WHERE deletion_id = ?
                    """,
                    (
                        json.dumps(counts), json.dumps(remaining_paths),
                        json.dumps(verification), utc_now(), deletion_id,
                    ),
                )
                connection.commit()
            raise DeletionIncompleteError(
                deletion_id, "FILE_DELETE_FAILED", len(remaining_paths)
            )

        if (job['target_type'] == 'capture_artifact' or job['target_type'] == 'child_scope'
                and job['scope'] in {'learning', 'media', 'all_history'}):
            from .local_ocr.review_lifecycle import purge_legacy_review_scope
            try:
                if io_resolve(self.data_dir / 'ocr-review-queue.db',) != io_resolve(self.database.path,):
                    legacy_counts = purge_legacy_review_scope(self.data_dir, job['child_id'],
                        job['target_id'] if job['target_type'] == 'capture_artifact' else None)
                    counts['legacy_ocr_review_rows'] = counts.get('legacy_ocr_review_rows', 0) + sum(legacy_counts.values())
                verification['legacy_ocr_review_purged'] = True
            except (OSError, sqlite3.Error, ValueError) as error:
                self._mark_deletion_failed(deletion_id, 'OCR_LEGACY_DELETE_FAILED', '旧版OCR校对副本尚未清理，关闭占用后可重试。')
                raise DeletionIncompleteError(deletion_id, 'OCR_LEGACY_DELETE_FAILED', 0) from error

            from .local_ocr.cache_lifecycle import purge_legacy_scope
            try:
                legacy_cache = purge_legacy_scope(self.database, self.data_dir / 'local-ocr.db', job['child_id'],
                    job['target_id'] if job['target_type'] == 'capture_artifact' else None)
                counts.update(legacy_cache)
                unresolved = legacy_cache['legacy_ocr_unattributed_jobs'] + legacy_cache['legacy_ocr_unattributed_cache']
                verification['legacy_ocr_cache_purged'] = unresolved == 0
                if unresolved:
                    with self.database.connect() as connection:
                        connection.execute('UPDATE deletion_job SET deleted_counts_json=?,verification_json=? WHERE deletion_id=?',
                            (json.dumps(counts), json.dumps(verification), deletion_id))
                        connection.commit()
                    self._mark_deletion_failed(deletion_id, 'OCR_LEGACY_OWNER_UNKNOWN',
                        '仍有无法确认属于哪位孩子的旧OCR缓存，已禁用重放；需家长明确选择全家旧缓存清理，不能按当前孩子猜测删除。')
                    raise DeletionIncompleteError(deletion_id, 'OCR_LEGACY_OWNER_UNKNOWN', unresolved)
            except (OSError, sqlite3.Error, ValueError) as error:
                self._mark_deletion_failed(deletion_id, 'OCR_LEGACY_CACHE_DELETE_FAILED', '旧OCR衍生缓存尚未清理，关闭占用后可重试。')
                raise DeletionIncompleteError(deletion_id, 'OCR_LEGACY_CACHE_DELETE_FAILED', 0) from error

        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE deletion_job SET status = 'verifying', deleted_counts_json = ?,
                  residual_paths_json = '[]', verification_json = ?, updated_at = ?
                WHERE deletion_id = ?
                """,
                (json.dumps(counts), json.dumps(verification), utc_now(), deletion_id),
            )
            connection.commit()
        remaining_counts = self._remaining_deletion_counts(job)
        if any(remaining_counts.values()):
            self._mark_deletion_failed(
                deletion_id, "DATABASE_REFERENCES_REMAIN",
                "数据库中仍有属于该删除范围的引用，请重试。",
            )
            raise DeletionIncompleteError(
                deletion_id, "DATABASE_REFERENCES_REMAIN", sum(remaining_counts.values())
            )
        try:
            recovery_rotation = self._rotate_recovery_points_after_deletion(
                job["child_id"], job["scope"]
            )
        except RecoveryPointRotationError as error:
            self._mark_deletion_failed(
                deletion_id, "RECOVERY_POINT_ROTATION_FAILED",
                "含旧数据的恢复点没有全部完成轮换，请重试。",
            )
            raise DeletionIncompleteError(
                deletion_id, "RECOVERY_POINT_ROTATION_FAILED", 0
            ) from error

        verification.update({
            "database_delete_committed": True,
            "remaining_counts": remaining_counts,
            "remaining_file_count": 0,
            "recovery_rotation": recovery_rotation,
        })
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE deletion_job SET status = 'completed', deleted_counts_json = ?,
                  verification_json = ?, residual_paths_json = '[]', error_code = NULL,
                  error_message = NULL, updated_at = ?, completed_at = ?
                WHERE deletion_id = ?
                """,
                (json.dumps(counts), json.dumps(verification), now, now, deletion_id),
            )
            connection.commit()
            completed = connection.execute(
                "SELECT * FROM deletion_job WHERE deletion_id = ?", (deletion_id,)
            ).fetchone()
        return self._serialize_deletion_job(completed)

    def _remaining_deletion_counts(self, job: sqlite3.Row) -> dict[str, int]:
        if job['target_type'] == 'knowledge_source':
            from .knowledge_source_deletion import remaining
            with self.database.connect() as connection:
                return remaining(connection, job['child_id'], job['target_id'],
                    json.loads(job['verification_json'] or '{}').get('native_manifest', {}))
        if job["target_type"] == "capture_artifact":
            artifact_id = job["target_id"]
            with self.database.connect() as connection:
                return {
                    "capture_artifact": connection.execute(
                        "SELECT COUNT(*) FROM capture_artifact WHERE artifact_id = ?", (artifact_id,)
                    ).fetchone()[0],
                    "homework_reference": connection.execute(
                        "SELECT COUNT(*) FROM homework_item WHERE source_artifact_id = ?", (artifact_id,)
                    ).fetchone()[0],
                    "plan_reference": connection.execute(
                        "SELECT COUNT(*) FROM plan_item WHERE completion_artifact_id = ?", (artifact_id,)
                    ).fetchone()[0],
                    "daily_observation_report_source": self._optional_count(connection, 'daily_observation_report_source', job['child_id']) + self._optional_count(connection, 'daily_observation_report_snapshot', job['child_id']),
                }
        if job["target_type"] == "capture_artifact_raw_media":
            artifact_id = job["target_id"]
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT local_path FROM capture_artifact WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()
            return {
                "derived_artifact": 0 if row else 1,
                "raw_media_reference": int(bool(row and row["local_path"])),
            }
        preview = self.deletion_preview(job["child_id"])
        keys = {
            "learning": ("pet_multipa_execution_receipts", "pet_speaking_candidate_jobs", "pet_speaking_model_candidates", "learning", "rubrics", "diagnostic_prompts", "weekly_goals", "goal_programs", "specialized_programs", "specialized_exams", "child_model_snapshots", "analysis_candidates", "child_model_corrections", "mastery_updates", "reviews", "pet_vocabulary", "mistakes", "mistake_diagnosis_events", "mistake_source_revisions", "mistake_capture_candidates", "variant_supply", "specialized_supply", "curriculum_supply"),
            "reports": ("reports", "parent_actions", "parent_report_source_issues"),
            "tutor": ("pet_multipa_execution_receipts", "pet_speaking_candidate_jobs", "pet_speaking_model_candidates", "tutor", "strategy_evolution", "rubrics"),
            "media": ("pet_multipa_execution_receipts", "pet_speaking_candidate_jobs", "pet_speaking_model_candidates", "media", "cloud_voice_interactions", "controlled_content_sessions", "agent_turns", "companion_memory", "safety_events", "state_events", "observation_corrections", "observation_calibrations", "interventions"),
            "knowledge": ("pet_multipa_execution_receipts", "pet_speaking_candidate_jobs", "pet_speaking_model_candidates", "knowledge_sources", "knowledge_source_revisions", "curriculum_supply"),
            "all_history": tuple(preview.keys()),
        }[job["scope"]]
        if job["scope"] in {"learning", "media"}:
            keys += ("companion_activity_outcomes", "learning_input_revisions", "ocr_reviews")
        if job["scope"] in {"learning", "media", "reports"}:
            keys += ("daily_observation_report_sources",)
        if job['scope'] == 'reports':
            keys += ('daily_report_email_verifications', 'daily_observation_delivery_history', 'report_delivery_recipient_authority_events')
        if job["scope"] == "media":
            keys += ("companion_activities",)
        if job['scope'] in {'learning', 'knowledge', 'media'}:
            keys += ('writing_question_authorizations', 'paper_variant_authorizations', 'paper_variant_job_authorizations', 'curriculum_internalization_authorities', 'rubric_calibrations', 'visual_material_events')
        return {key: int(preview.get(key, 0)) for key in keys}

    def _mark_deletion_failed(
        self, deletion_id: str, error_code: str, error_message: str,
        residual_paths: list[str] | None = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if residual_paths is None:
                connection.execute(
                    "UPDATE deletion_job SET status = 'failed', error_code = ?, error_message = ?, updated_at = ? WHERE deletion_id = ?",
                    (error_code, error_message, utc_now(), deletion_id),
                )
            else:
                connection.execute(
                    "UPDATE deletion_job SET status = 'failed', residual_paths_json = ?, error_code = ?, error_message = ?, updated_at = ? WHERE deletion_id = ?",
                    (json.dumps(residual_paths), error_code, error_message, utc_now(), deletion_id),
                )
            connection.commit()

    def _deletion_job_by_action(self, client_action_id: str) -> sqlite3.Row | None:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT * FROM deletion_job WHERE client_action_id = ?", (client_action_id,)
            ).fetchone()

    @staticmethod
    def _require_matching_deletion(
        job: sqlite3.Row, target_type: str, target_id: str, scope: str | None = None
    ) -> None:
        if (
            job["target_type"] != target_type
            or job["target_id"] != target_id
            or (scope is not None and job["scope"] != scope)
        ):
            raise InvalidDeletionActionError()

    @staticmethod
    def _serialize_deletion_job(job: sqlite3.Row) -> dict:
        counts = json.loads(job["deleted_counts_json"] or "{}")
        verification = json.loads(job["verification_json"] or "{}")
        result = {
            "deletion_id": job["deletion_id"],
            "child_id": job["child_id"],
            "scope": job["scope"],
            "target_type": job["target_type"],
            "status": job["status"],
            "deleted_counts": counts,
            "remaining_file_count": len(json.loads(job["residual_paths_json"] or "[]")),
            "error_code": job["error_code"],
            "error_message": job["error_message"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "completed_at": job["completed_at"],
            "recovery_rotation": verification.get("recovery_rotation"),
        }
        if job["target_type"] == "capture_artifact":
            result.update({
                "artifact_id": job["target_id"],
                "database_reference_deleted": bool(verification.get("database_delete_committed")),
                "raw_file_deleted": bool(verification.get("raw_file_deleted")),
                "homework_references_cleared": counts.get("homework_references_cleared", 0),
                "plan_item_references_cleared": counts.get("plan_item_references_cleared", 0),
            })
        if job['target_type'] == 'knowledge_source':
            result.update(source_id=job['target_id'], database_reference_deleted=bool(verification.get('database_delete_committed')))
        if job["target_type"] == "capture_artifact_raw_media":
            result.update({
                "artifact_id": job["target_id"],
                "derived_artifact_retained": bool(verification.get("derived_artifact_retained")),
                "raw_file_deleted": bool(verification.get("raw_file_deleted")),
            })
        return result

    @_serialized_deletion
    def run_retention(self) -> dict:
        preferences = self.preferences()
        now = datetime.now(timezone.utc)
        thresholds = {
            "model_call": (now - timedelta(days=preferences["model_call_retention_days"])).isoformat(),
            "tutor_turn": (now - timedelta(days=preferences["tutor_turn_retention_days"])).isoformat(),
            "daily_report": (now - timedelta(days=preferences["report_retention_days"])).date().isoformat(),
        }
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            counts = {
                "model_call": connection.execute("DELETE FROM model_call WHERE created_at < ?", (thresholds["model_call"],)).rowcount,
                "tutor_feedback": connection.execute("DELETE FROM tutor_feedback WHERE created_at < ?", (thresholds["tutor_turn"],)).rowcount,
                "tutor_turn": connection.execute("DELETE FROM tutor_turn WHERE created_at < ?", (thresholds["tutor_turn"],)).rowcount,
                "daily_report": connection.execute("DELETE FROM daily_report WHERE report_date < ?", (thresholds["daily_report"],)).rowcount,
            }
            if self._table_exists(connection, 'execution_trace_event'):
                counts['execution_trace_event'] = connection.execute("""DELETE FROM execution_trace_event
                    WHERE source_kind='daily_report' AND NOT EXISTS
                    (SELECT 1 FROM daily_report d WHERE d.report_id=execution_trace_event.source_id
                     AND d.child_id=execution_trace_event.child_id)""").rowcount
            self._audit(connection, "system", "retention_cleanup_completed", None, counts, utc_now())
            connection.commit()
        return {"deleted_counts": counts, "ran_at": utc_now()}

    def run_due_maintenance(self, reference: datetime | None = None) -> dict:
        reference = reference or datetime.now(timezone.utc)
        run_date = reference.astimezone().date().isoformat()
        results = []
        for job_type in ("retention_cleanup", "daily_backup"):
            run_id, existing_status = self._claim_maintenance(job_type, run_date)
            if not run_id:
                results.append({"job_type": job_type, "status": existing_status, "skipped": True})
                continue
            try:
                if job_type == "retention_cleanup":
                    details = self.run_retention()
                else:
                    existing_backup = self._backup_for_local_date(run_date)
                    details = (
                        {"backup_id": existing_backup["backup_id"], "reused_existing_backup": True}
                        if existing_backup
                        else {**self.create_backup(reason="daily_automatic"), "reused_existing_backup": False}
                    )
                self._finish_maintenance(run_id, "completed", details=details)
                results.append({"job_type": job_type, "status": "completed", **details})
            except Exception as error:
                public_message = (
                    "自动备份失败，请检查可用空间和家庭数据目录权限。"
                    if job_type == "daily_backup"
                    else "数据保留清理失败，系统会稍后重试。"
                )
                self._finish_maintenance(
                    run_id,
                    "failed",
                    error_code=type(error).__name__,
                    error_message=public_message,
                )
                results.append(
                    {
                        "job_type": job_type,
                        "status": "failed",
                        "error_code": type(error).__name__,
                    }
                )
        return {"run_date": run_date, "items": results}

    def recover_interrupted_maintenance(self) -> int:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            interrupted = connection.execute(
                "SELECT run_id, job_type, run_date FROM maintenance_run WHERE status = 'processing'"
            ).fetchall()
            if interrupted:
                connection.execute(
                    """
                    UPDATE maintenance_run SET status = 'failed', completed_at = ?,
                      error_code = 'MAINTENANCE_INTERRUPTED',
                      error_message = '上次本地维护在应用退出前未完成，系统将重新执行。'
                    WHERE status = 'processing'
                    """,
                    (now,),
                )
                self._audit(
                    connection,
                    "system",
                    "maintenance_interrupted",
                    None,
                    {"runs": [{"run_id": row["run_id"], "job_type": row["job_type"], "run_date": row["run_date"]} for row in interrupted]},
                    now,
                )
            connection.commit()
        return len(interrupted)

    def create_backup(self, prune: bool = True, reason: str = "manual") -> dict:
        if reason not in BACKUP_REASONS:
            raise InvalidBackupError("不支持的备份来源。")
        backup_dir = self.data_dir / "backups"
        io_path(backup_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        backup_id = str(uuid4())
        destination = backup_dir / f"home-study-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{backup_id[:8]}.db"
        published = False
        owned_identity=None
        try:
            from .registered_asset_snapshot import _check_directory, Policy
            from . import registered_asset_backup as registered
            _check_directory(backup_dir, Policy(100000,64,1024,64*1024*1024,2**53-1,2**53-1))
            if any(os.path.lexists(io_path(p))for p in [destination,self._backup_media_dir(destination),self._backup_metadata_path(destination),registered.capsule_path(destination),*[Path(str(destination)+s)for s in ['-wal','-shm','-journal']]]):
                raise InvalidBackupError('BACKUP_DESTINATION_EXISTS')
            with io_path(destination).open('xb')as reserved:
                opened=os.fstat(reserved.fileno());owned_identity=(opened.st_dev,opened.st_ino)
                os.chmod(io_path(destination),0o600)
            with self.database.connect() as source:
                # The original business scope owns only its fresh read txn.
                # Its private nested marker must survive capsule completion
                # before any backup_manifest/success audit can be published.
                from .standard_relocation_provenance import export_scope,business_snapshot
                if source.in_transaction:raise InvalidBackupError('BACKUP_SOURCE_SNAPSHOT_REQUIRED')
                with business_snapshot(source):
                    source.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
                    target = sqlite3.connect(destination)
                    try:
                        source.backup(target)
                        if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != 'delete':
                            raise InvalidBackupError('BACKUP_SQLITE_SINGLE_FILE_REQUIRED')
                        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
                        if integrity != 'ok':
                            raise InvalidBackupError('BACKUP_DATABASE_INTEGRITY_REQUIRED')
                    finally:
                        target.close()
                    registered.normalize_owned_snapshot(destination)
                    media_files, missing_media = self._snapshot_backup_media(destination)
                    with export_scope(source,self.database,destination)as provenance_ticket:
                        roots = registered.create_capsule(destination, self.data_encryption_key, self._encrypt_file,
                                                          provenance_ticket=provenance_ticket)
            metadata_path = self._backup_metadata_path(destination)
            metadata = registered.read_json(metadata_path)
            metadata.update(format=registered.FULL, database=registered.record(destination), registered=roots)
            with io_path(metadata_path).open('w', encoding='utf-8') as output:
                output.write(registered.canonical(metadata).decode('utf-8'))
                output.flush(); os.fsync(output.fileno())
            sha256, total_size = self._backup_checksum(destination)
            with self.database.connect() as connection:
                version = connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] or 0
                connection.execute(
                    "INSERT INTO backup_manifest(backup_id, path, database_version, size_bytes, sha256, integrity_status, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (backup_id, str(destination), version, total_size, sha256, integrity, reason, utc_now()),
                )
                self._audit(connection, "parent" if reason == "manual" else "system", "backup_created", None, {
                    "backup_id": backup_id,
                    "reason": reason,
                    "sha256": sha256,
                    "media_files": media_files,
                    "missing_media_references": missing_media,
                }, utc_now())
                connection.commit()
            published = True
            if prune:
                self._prune_backups()
            return {
                "backup_id": backup_id,
                "path": str(destination),
                "size_bytes": total_size,
                "sha256": sha256,
                "integrity_status": integrity,
                "reason": reason,
                "media_file_count": media_files,
                "missing_media_references": missing_media,
                "registered_asset_count": roots["asset_count"],
                "registered_coverage": roots["coverage"],
            }
        except Exception as error:
            if not published and owned_identity is not None:
                if os.path.lexists(io_path(destination)):
                    current=io_path(destination).lstat()
                    if io_path(destination).is_symlink()or(current.st_dev,current.st_ino)!=owned_identity:
                        raise InvalidBackupError('BACKUP_MEMBER_ALIAS')from error
                self._remove_backup_bundle(destination)
            if isinstance(error, InvalidBackupError):
                raise
            code = str(error) if isinstance(error, ValueError) and str(error).isupper() and len(str(error)) <= 100 else 'REGISTERED_ASSET_BACKUP_FAILED'
            raise InvalidBackupError(code) from error

    def _rotate_recovery_points_after_deletion(self, child_id: str, scope: str) -> dict:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT path FROM backup_manifest").fetchall()
            connection.execute("BEGIN IMMEDIATE")
            removed_manifests = connection.execute("DELETE FROM backup_manifest").rowcount
            self._audit(
                connection,
                "system",
                "recovery_points_invalidated_after_deletion",
                child_id,
                {"scope": scope, "manifest_count": removed_manifests},
                utc_now(),
            )
            connection.commit()

        backup_root = (self.data_dir / "backups").absolute()
        portable_root = (self.data_dir / "portable-backups").absolute()
        from . import registered_asset_backup as registered
        from .registered_asset_snapshot import _check_directory
        removed_files = 0
        try:
            for owned_root in [backup_root,portable_root]:
                if os.path.lexists(io_path(owned_root)):
                    _check_directory(owned_root,registered.Policy(100000,64,1024,registered.M,registered.SAFE,registered.SAFE))
            candidates = {Path(row["path"]).absolute() for row in rows}
            if io_path(backup_root).is_dir():
                candidates.update(path.absolute() for path in io_glob(backup_root,"home-study-*.db"))
            for backup_path in candidates:
                if backup_path.parent != backup_root:
                    raise OSError("backup path outside managed directory")
                removed_files += self._remove_backup_bundle(backup_path)
            if io_path(portable_root).is_dir():
                for portable_path in io_glob(portable_root,"home-study-portable-*.hscbackup"):
                    if io_path(portable_path).is_symlink() or portable_path.parent != portable_root:
                        raise OSError("portable backup path outside managed directory")
                    io_path(portable_path).unlink()
                    removed_files += 1
            removed_upgrade_files = self.database.purge_upgrade_snapshots()
            replacement = self.create_backup(prune=False, reason="post_deletion")
        except Exception as error:
            with self.database.connect() as connection:
                self._audit(
                    connection,
                    "system",
                    "recovery_point_rotation_failed",
                    child_id,
                    {"scope": scope, "error_type": type(error).__name__},
                    utc_now(),
                )
                connection.commit()
            raise RecoveryPointRotationError() from error
        return {
            "status": "completed",
            "invalidated_backup_count": removed_manifests,
            "removed_backup_bundle_files": removed_files,
            "removed_upgrade_snapshot_files": removed_upgrade_files,
            "replacement_backup_id": replacement["backup_id"],
        }

    def list_backups(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT backup_id, path, database_version, size_bytes, sha256,
                       integrity_status, reason, created_at FROM backup_manifest
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def restore_backup(self, backup_id: str, confirmation: str, *, operation_request_id: str | None = None) -> dict:
        from .restore_publication import publication_owner
        with publication_owner(self.database):
            return self._restore_backup_guarded(backup_id, confirmation, operation_request_id=operation_request_id)

    def _restore_backup_guarded(self, backup_id: str, confirmation: str, *, operation_request_id: str | None = None) -> dict:
        if confirmation != "确认恢复":
            raise RestoreConfirmationError()
        with self.database.connect() as connection:
            from .native_privacy import assert_resource_permit
            assert_resource_permit(connection, None, operation_request_id)
            if connection.execute("SELECT 1 FROM daily_session WHERE status = 'active' LIMIT 1").fetchone():
                raise RestoreWhileSessionActiveError()
            runtime_state = connection.execute(
                "SELECT clean_shutdown, run_id, started_at, ended_at FROM runtime_state WHERE singleton_id = 1"
            ).fetchone()
            manifest = connection.execute(
                "SELECT * FROM backup_manifest WHERE backup_id = ?", (backup_id,)
            ).fetchone()
            current_version = connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] or 0
        if not manifest:
            raise UnknownBackupError(backup_id)
        source_path = Path(manifest["path"])
        if not io_path(source_path).is_file():
            raise InvalidBackupError("备份文件缺失或校验和不一致。")
        try:
            actual_sha256, _ = self._backup_checksum(source_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise InvalidBackupError("备份媒体清单缺失或校验失败。") from error
        if actual_sha256 != manifest["sha256"]:
            raise InvalidBackupError("备份文件缺失或校验和不一致。")
        if operation_request_id:
            from .privacy_operation_receipt import PrivacyOperationReceipts
            PrivacyOperationReceipts(self.database).bind_source(operation_request_id, actual_sha256)
        if manifest["database_version"] > current_version:
            raise InvalidBackupError("备份来自更高版本应用，当前版本不能恢复。")
        from .parent_restore_workflow import publish_local_restore
        return publish_local_restore(self, source_path, backup_id, operation_request_id,
                                     actual_sha256, runtime_state, current_version)

    def create_portable_backup(self, passphrase: str) -> dict:
        """Create one password-encrypted file that can be restored on another PC."""
        self._validate_portable_passphrase(passphrase)
        if not self.data_encryption_key or len(self.data_encryption_key) != 32:
            raise InvalidBackupError("本地数据加密密钥不可用，无法创建可迁移备份。")

        from . import registered_asset_backup as registered
        from .registered_asset_snapshot import _check_directory
        portable_dir = self.data_dir / "portable-backups"
        io_path(portable_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_directory(portable_dir,registered.Policy(100000,64,1024,registered.M,registered.SAFE,registered.SAFE))
        portable_id = str(uuid4())
        destination = portable_dir / (
            f"home-study-portable-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{portable_id[:8]}.hscbackup"
        )
        snapshot = self.create_backup(prune=False, reason="manual")
        snapshot_path = Path(snapshot["path"])

        owned_identity=[]
        try:
            return self._create_portable_bundle(passphrase,portable_id,destination,snapshot_path,snapshot,owned_identity)
        except Exception as error:
            # Only this fresh, fixed owned destination; the valid local recovery
            # point remains available. Never leave a partial portable publication.
            if owned_identity and os.path.lexists(io_path(destination)):
                current=io_path(destination).lstat()
                if io_path(destination).is_symlink() or (current.st_dev,current.st_ino)!=owned_identity[0]:
                    raise InvalidBackupError('BACKUP_MEMBER_ALIAS') from error
                io_path(destination).unlink()
                if os.path.lexists(io_path(destination)):
                    raise InvalidBackupError('BACKUP_CLEANUP_UNCONFIRMED') from error
            if isinstance(error,InvalidBackupError):
                raise
            raise InvalidBackupError('PORTABLE_BACKUP_CREATE_FAILED') from error

    def _create_portable_bundle(self,passphrase,portable_id,destination,snapshot_path,snapshot,owned_identity):
        from . import registered_asset_backup as registered
        with registered.owned_work(destination.parent) as temporary_root:
            archive_path = temporary_root / "payload.zip"
            encrypted_path = temporary_root / "payload.enc"
            manifest = self._portable_archive_manifest(snapshot_path)
            manifest["portable_backup_id"] = portable_id
            manifest["created_at"] = utc_now()
            manifest["source_data_key"] = base64.b64encode(self.data_encryption_key).decode("ascii")
            manifest_bytes=registered.canonical(manifest)
            if len(manifest_bytes)>registered.M:
                raise InvalidBackupError('BACKUP_MANIFEST_BOUND')
            archive_budget=sum(x['size_bytes']for x in manifest['files'])+len(manifest_bytes)+1024+sum(
                2*len(n.encode('utf-8'))+256 for n in ['portable-manifest.json']+[x['archive_path']for x in manifest['files']])
            registered.space(temporary_root,archive_budget)
            self._write_portable_archive(snapshot_path, archive_path, manifest)
            if io_path(archive_path).stat().st_size>archive_budget:
                raise InvalidBackupError('BACKUP_CONTAINER_BOUND')

            salt, nonce = os.urandom(16), os.urandom(12)
            key = self._derive_portable_key(passphrase, salt)
            registered.space(temporary_root,io_path(archive_path).stat().st_size)
            tag = self._encrypt_file(archive_path, encrypted_path, key, nonce)
            io_path(archive_path).unlink()
            header = {
                "format": "hsc_portable_backup_v1",
                "kdf": "scrypt",
                "n": 32768,
                "r": 8,
                "p": 1,
                "salt": base64.b64encode(salt).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "tag": base64.b64encode(tag).decode("ascii"),
                "ciphertext_size": io_path(encrypted_path).stat().st_size,
            }
            header_payload = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
            registered.space(destination.parent,len(PORTABLE_BACKUP_MAGIC)+4+len(header_payload)+header['ciphertext_size'])
            with io_path(destination).open("xb") as output:
                opened=os.fstat(output.fileno());owned_identity.append((opened.st_dev,opened.st_ino))
                os.chmod(io_path(destination),0o600)
                output.write(PORTABLE_BACKUP_MAGIC)
                output.write(struct.pack(">I", len(header_payload)))
                output.write(header_payload)
                with io_path(encrypted_path).open("rb") as encrypted:
                    shutil.copyfileobj(encrypted, output, length=1024 * 1024)
                output.flush();os.fsync(output.fileno())

        digest = self._sha256_file(destination)
        with self.database.connect() as connection:
            self._audit(connection, "parent", "portable_backup_created", None, {
                "portable_backup_id": portable_id,
                "source_backup_id": snapshot["backup_id"],
                "sha256": digest,
                "size_bytes": io_path(destination).stat().st_size,
                "media_file_count": snapshot["media_file_count"],
            }, utc_now())
            connection.commit()
        return {
            "portable_backup_id": portable_id,
            "path": str(destination),
            "size_bytes": io_path(destination).stat().st_size,
            "sha256": digest,
            "media_file_count": snapshot["media_file_count"],
            "format": "hsc_portable_backup_v1",
        }

    def restore_portable_backup(self, path: str, passphrase: str, confirmation: str, *, operation_request_id: str | None = None) -> dict:
        from .restore_publication import publication_owner
        with publication_owner(self.database):
            return self._restore_portable_backup_guarded(path, passphrase, confirmation, operation_request_id=operation_request_id)

    def _restore_portable_backup_guarded(self, path: str, passphrase: str, confirmation: str, *, operation_request_id: str | None = None) -> dict:
        if confirmation != "确认迁移恢复":
            raise RestoreConfirmationError("确认迁移恢复")
        self._validate_portable_passphrase(passphrase)
        from .parent_restore_parent import current as parent_restore_current
        parent_restore=parent_restore_current(self.database)
        if parent_restore is not None:
            if not self.data_encryption_key or len(self.data_encryption_key)!=32:
                raise InvalidBackupError("新设备数据加密密钥不可用，已拒绝恢复。")
            from .parent_restore_portable import publish_parent_portable
            return publish_parent_portable(self,path,passphrase,parent_restore.body['operation'])
        with self.database.connect() as connection:
            from .native_privacy import assert_resource_permit
            assert_resource_permit(connection, None, operation_request_id)
        if not self.data_encryption_key or len(self.data_encryption_key) != 32:
            raise InvalidBackupError("新设备数据加密密钥不可用，已拒绝恢复。")
        from .parent_restore_portable import has_original_native_claim,publish_native_portable
        with self.database.connect()as connection:
            original_native=has_original_native_claim(connection,operation_request_id)
            from .parent_restore_assets import has_asset_references
            owned=has_asset_references(connection)
        if original_native:
            return publish_native_portable(self,path,passphrase,operation_request_id)
        if owned:
            raise InvalidBackupError('PORTABLE_PARENT_OWNED_AUTHORITY_REQUIRED')
        source = io_resolve(Path(path).expanduser(),)
        if not io_path(source).is_file():
            raise InvalidBackupError("可迁移备份文件不存在。")
        source_sha256 = self._sha256_file(source)
        if operation_request_id:
            from .privacy_operation_receipt import PrivacyOperationReceipts
            PrivacyOperationReceipts(self.database).bind_source(operation_request_id, source_sha256)
        with self.database.connect() as connection:
            if connection.execute("SELECT 1 FROM daily_session WHERE status = 'active' LIMIT 1").fetchone():
                raise RestoreWhileSessionActiveError()
            runtime_state = connection.execute(
                "SELECT clean_shutdown, run_id, started_at, ended_at FROM runtime_state WHERE singleton_id = 1"
            ).fetchone()
            current_version = connection.execute(
                "SELECT MAX(version) FROM schema_migration"
            ).fetchone()[0] or 0

        recovery_point = self.create_backup(prune=False, reason="restore_recovery_point")
        with io_temporary_directory(prefix="hsc-portable-restore-", dir=self.data_dir) as temporary:
            temporary_root = Path(temporary)
            archive_path = temporary_root / "payload.zip"
            extracted = temporary_root / "archive"
            header, ciphertext_offset = self._read_portable_header(source)
            try:
                salt = base64.b64decode(header["salt"], validate=True)
                nonce = base64.b64decode(header["nonce"], validate=True)
                tag = base64.b64decode(header["tag"], validate=True)
                key = self._derive_portable_key(passphrase, salt)
                self._decrypt_file(source, archive_path, key, nonce, tag, ciphertext_offset)
            except Exception as error:
                raise InvalidBackupError("备份口令错误或文件已损坏。") from error
            manifest = self._extract_and_verify_portable_archive(archive_path, extracted)
            database_snapshot = extracted / "database.db"
            self._require_registered_restore_migration(database_snapshot)
            try:
                old_key = base64.b64decode(manifest["source_data_key"], validate=True)
            except Exception as error:
                raise InvalidBackupError("备份内的媒体密钥封装无效。") from error
            if len(old_key) != 32:
                raise InvalidBackupError("备份内的媒体密钥长度无效。")
            self._prepare_portable_snapshot(database_snapshot, old_key, self.data_encryption_key)
            source_check = sqlite3.connect(database_snapshot)
            try:
                self._validate_restore_scope(source_check)
                if source_check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise InvalidBackupError("迁移备份数据库完整性检查失败。")
                if source_check.execute('PRAGMA foreign_key_check').fetchone():
                    raise InvalidBackupError('迁移备份中的数据关联不完整，已拒绝替换当前数据。')
                source_version = source_check.execute(
                    "SELECT MAX(version) FROM schema_migration"
                ).fetchone()[0] or 0
                if source_version > current_version:
                    raise InvalidBackupError("迁移备份来自更高版本应用，当前版本不能恢复。")
            finally:
                source_check.close()

            with self.database.connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            try:
                if self._sha256_file(source) != source_sha256:
                    raise InvalidBackupError('迁移备份在恢复前发生变化。')
                integrity = self._restore_database_snapshot(database_snapshot, pause_restored_observation=True)
                self._restore_backup_media(database_snapshot)
                self.database.initialize()
                with self.database.connect() as connection:
                    if connection.execute('PRAGMA foreign_key_check').fetchone():
                        raise InvalidBackupError('迁移升级后的数据关联不完整。')
                    if runtime_state:
                        connection.execute(
                            """UPDATE runtime_state SET clean_shutdown = ?, run_id = ?, started_at = ?, ended_at = ?
                               WHERE singleton_id = 1""",
                            tuple(runtime_state),
                        )
                    connection.execute(
                        """INSERT OR IGNORE INTO backup_manifest(
                          backup_id, path, database_version, size_bytes, sha256,
                          integrity_status, reason, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            recovery_point["backup_id"], recovery_point["path"], current_version,
                            recovery_point["size_bytes"], recovery_point["sha256"],
                            recovery_point["integrity_status"], recovery_point["reason"], utc_now(),
                        ),
                    )
                    summary = {
                        "portable_backup_id": manifest["portable_backup_id"],
                        "source_sha256": source_sha256,
                        "recovery_point_backup_id": recovery_point["backup_id"],
                        "media_file_count": len(manifest.get("media_files", [])),
                    }
                    if operation_request_id:
                        summary['privacy_operation_request_id'] = operation_request_id
                    audit_id = self._audit(connection, "parent", "portable_backup_restored", None, summary, utc_now())
                    from .privacy_operation_receipt import record_restore
                    record_restore(connection, operation_request_id, audit_id)
                    connection.commit()
            except Exception as error:
                try:
                    self._restore_database_snapshot(Path(recovery_point["path"]))
                    self._restore_backup_media(Path(recovery_point["path"]))
                    self.database.initialize()
                except Exception as rollback_error:
                    raise InvalidBackupError("迁移恢复失败，且自动回滚未完成；请停止使用并保留备份。") from rollback_error
                if isinstance(error, InvalidBackupError):
                    raise
                raise InvalidBackupError("迁移恢复失败，当前数据已自动回滚。") from error
        self._prune_backups()
        return {
            "portable_backup_id": manifest["portable_backup_id"],
            "status": "restored",
            "integrity_status": integrity,
            "recovery_point_backup_id": recovery_point["backup_id"],
            "media_file_count": len(manifest.get("media_files", [])),
        }

    @staticmethod
    def _validate_portable_passphrase(passphrase: str) -> None:
        if len(passphrase) < 12 or len(passphrase) > 200:
            raise InvalidBackupError("备份口令必须为12至200个字符。")

    @staticmethod
    def _derive_portable_key(passphrase: str, salt: bytes) -> bytes:
        return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(passphrase.encode("utf-8"))

    @staticmethod
    def _encrypt_file(source: Path, destination: Path, key: bytes, nonce: bytes, aad: bytes | None = None) -> bytes:
        encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(PORTABLE_BACKUP_AAD if aad is None else aad)
        with io_path(source).open("rb") as input_file, io_path(destination).open("wb") as output_file:
            while chunk := input_file.read(1024 * 1024):
                output_file.write(encryptor.update(chunk))
            output_file.write(encryptor.finalize())
        return encryptor.tag

    @staticmethod
    def _decrypt_file(source: Path, destination: Path, key: bytes, nonce: bytes, tag: bytes, offset: int, aad: bytes | None = None) -> None:
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(PORTABLE_BACKUP_AAD if aad is None else aad)
        with io_path(source).open("rb") as input_file, io_path(destination).open("wb") as output_file:
            input_file.seek(offset)
            while chunk := input_file.read(1024 * 1024):
                output_file.write(decryptor.update(chunk))
            output_file.write(decryptor.finalize())

    @staticmethod
    def _read_portable_header(source: Path) -> tuple[dict, int]:
        with io_path(source).open("rb") as input_file:
            if input_file.read(len(PORTABLE_BACKUP_MAGIC)) != PORTABLE_BACKUP_MAGIC:
                raise InvalidBackupError("不是可识别的家庭学习伙伴备份。")
            length_payload = input_file.read(4)
            if len(length_payload) != 4:
                raise InvalidBackupError("备份头不完整。")
            header_length = struct.unpack(">I", length_payload)[0]
            if header_length <= 0 or header_length > 16_384:
                raise InvalidBackupError("备份头长度无效。")
            header = json.loads(input_file.read(header_length).decode("utf-8"))
            if header.get("format") != "hsc_portable_backup_v1":
                raise InvalidBackupError("备份版本不受支持。")
            if io_path(source).stat().st_size - input_file.tell() != int(header.get("ciphertext_size", -1)):
                raise InvalidBackupError("备份密文长度不一致。")
            return header, input_file.tell()

    def _validate_restore_scope(self, connection) -> None:
        from .runtime_data_scope import assert_scope, RuntimeDataScopeError
        try:
            assert_scope(connection, self.database.runtime_mode)
        except (RuntimeDataScopeError, sqlite3.DatabaseError) as error:
            raise InvalidBackupError('备份的正式/试用数据作用域不匹配；不能整库迁移试用掌握结果。') from error

    def _portable_archive_manifest(self, database_snapshot: Path) -> dict:
        from . import registered_asset_backup as registered
        self._backup_checksum(database_snapshot)
        metadata_path = self._backup_metadata_path(database_snapshot)
        contract = registered.record(metadata_path)
        metadata = registered.read_json(metadata_path)
        if metadata.get('format') == registered.FULL:
            files = registered.full_files(database_snapshot, metadata)
        else:
            files = [{'archive_path':'database.db', **registered.record(database_snapshot)}]
            for item in metadata['files']:
                files.append({'archive_path':'database.media/'+item['relative_path'],
                              **registered.record(self._backup_media_dir(database_snapshot)/item['relative_path'])})
        if registered.record(metadata_path)!=contract:
            raise InvalidBackupError('BACKUP_METADATA_CHANGED')
        files.append({'archive_path':'database.media.json', **contract})
        return {'format':'hsc_portable_archive_v1','files':files,'media_files':metadata['files']}

    def _write_portable_archive(self, database_snapshot: Path, destination: Path, manifest: dict) -> None:
        from . import registered_asset_backup as registered
        expected = self._portable_archive_manifest(database_snapshot)
        if sorted(manifest['files'],key=lambda x:x['archive_path']) != sorted(expected['files'],key=lambda x:x['archive_path']) or manifest['media_files'] != expected['media_files']:
            raise InvalidBackupError('BACKUP_FULL_CONTRACT_CHANGED')
        with zipfile.ZipFile(io_path(destination), 'x', compression=zipfile.ZIP_STORED) as archive:
            for item in manifest['files']:
                name = item['archive_path']
                if name == 'database.db':
                    source = database_snapshot
                elif name == 'database.media.json':
                    source = self._backup_metadata_path(database_snapshot)
                elif name == 'database.registered.enc':
                    source = registered.capsule_path(database_snapshot)
                else:
                    source = self._backup_media_dir(database_snapshot)/name.removeprefix('database.media/')
                policy = registered.Policy(100000,64,1024,registered.M,max(1,item['size_bytes']),max(1,item['size_bytes']))
                with registered.HeldFile(source.parent,source.name,policy) as held, archive.open(name,'w',force_zip64=True) as output:
                    count = 0; digest = hashlib.sha256()
                    for chunk in held.chunks():
                        count += len(chunk); digest.update(chunk); output.write(chunk)
                    if count != item['size_bytes'] or digest.hexdigest() != item['sha256']:
                        raise InvalidBackupError('BACKUP_SOURCE_CHANGED')
            archive.writestr('portable-manifest.json', registered.canonical(manifest))

    def _extract_and_verify_portable_archive(self, source: Path, destination: Path) -> dict:
        from . import registered_asset_backup as registered
        try:
            with registered.finite_archive(source) as archive:
                info = archive.getinfo('portable-manifest.json')
                if info.file_size > registered.M:
                    raise InvalidBackupError('BACKUP_MANIFEST_BOUND')
                with archive.open(info) as input_file:
                    raw = input_file.read(registered.M+1)
                if len(raw) > registered.M:
                    raise InvalidBackupError('BACKUP_MANIFEST_BOUND')
                manifest = registered.unique_json(raw)
            if manifest.get('format') != 'hsc_portable_archive_v1':
                raise InvalidBackupError('BACKUP_FORMAT_UNSUPPORTED')
            if set(manifest) != {'format','files','media_files','portable_backup_id','created_at','source_data_key'}:
                raise InvalidBackupError('BACKUP_FULL_CONTRACT')
            for item in manifest['files']:
                name = item['archive_path']
                if name not in {'database.db','database.media.json','database.registered.enc'} and not name.startswith('database.media/'):
                    raise InvalidBackupError('BACKUP_MEMBER_NOT_ALLOWED')
            registered.extract_exact(source,destination,manifest['files'],manifest_name='portable-manifest.json')
            expected = self._portable_archive_manifest(destination/'database.db')
            if sorted(manifest['files'],key=lambda x:x['archive_path']) != sorted(expected['files'],key=lambda x:x['archive_path']) or manifest['media_files'] != expected['media_files']:
                raise InvalidBackupError('BACKUP_FULL_CONTRACT_CHANGED')
            return manifest
        except InvalidBackupError:
            raise
        except Exception as error:
            raise InvalidBackupError('BACKUP_ARCHIVE_INVALID') from error

    def _prepare_portable_snapshot(self, database_snapshot: Path, old_key: bytes, new_key: bytes) -> None:
        media_root = self._backup_media_dir(database_snapshot)
        metadata_path = self._backup_metadata_path(database_snapshot)
        metadata = json.loads(io_path(metadata_path).read_text(encoding="utf-8"))
        for item in metadata.get("files", []):
            path = media_root / item["relative_path"]
            payload = io_path(path).read_bytes()
            if not payload.startswith(b"HSC1") or len(payload) < 32:
                raise InvalidBackupError("备份中的媒体文件格式无效。")
            plaintext = AESGCM(old_key).decrypt(payload[4:16], payload[16:], b"hsc-raw-media-v1")
            nonce = os.urandom(12)
            io_path(path).write_bytes(b"HSC1" + nonce + AESGCM(new_key).encrypt(nonce, plaintext, b"hsc-raw-media-v1"))
            item["size_bytes"] = io_path(path).stat().st_size
            item["sha256"] = self._sha256_file(path)
        io_path(metadata_path).write_text(json.dumps(metadata, sort_keys=True, separators=(",", ":")), encoding="utf-8")

        connection = sqlite3.connect(database_snapshot)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table in ("voice_input", "capture_artifact"):
                rows = connection.execute(f"SELECT rowid, local_path FROM {table} WHERE local_path IS NOT NULL").fetchall()
                for row in rows:
                    relative = self._portable_media_relative_path(row["local_path"])
                    connection.execute(
                        f"UPDATE {table} SET local_path = ? WHERE rowid = ?",
                        (str(self.data_dir / "media" / relative), row["rowid"]),
                    )
            if self._table_exists(connection, "kg_parent_textbook_ocr_artifact"):
                columns = {column[1] for column in connection.execute("PRAGMA table_info(kg_parent_textbook_ocr_artifact)")}
                if "raw_image_path" in columns:
                    rows = connection.execute(
                        "SELECT rowid,raw_image_path FROM kg_parent_textbook_ocr_artifact WHERE raw_image_path IS NOT NULL"
                    ).fetchall()
                    for row in rows:
                        relative = self._portable_media_relative_path(row["raw_image_path"])
                        connection.execute(
                            "UPDATE kg_parent_textbook_ocr_artifact SET raw_image_path=? WHERE rowid=?",
                            (str(self.data_dir / "media" / relative),row["rowid"]),
                        )
            self._rekey_portable_credentials(connection,old_key,new_key)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _rekey_portable_credentials(self,connection,old_key,new_key):
        if self._table_exists(connection, "kg_parent_textbook_form_draft"):
            from .knowledge_graph.parent_textbook_drafts import ParentTextbookDraftService
            rows = connection.execute("""SELECT child_id, scope, revision, encrypted_payload, payload_sha256
                    FROM kg_parent_textbook_form_draft WHERE encrypted_payload IS NOT NULL""").fetchall()
            for row in rows:
                try:
                    raw = bytes(row["encrypted_payload"])
                    if len(raw) < 32 or not raw.startswith(b"HSD1"):
                        raise ValueError("invalid draft encrypted header")
                    aad = ParentTextbookDraftService._aad(row["child_id"], row["scope"], row["revision"])
                    plaintext = AESGCM(old_key).decrypt(raw[4:16], raw[16:], aad)
                    if hashlib.sha256(plaintext).hexdigest() != row["payload_sha256"]:
                        raise ValueError("draft checksum mismatch")
                    nonce = os.urandom(12)
                    encrypted = b"HSD1" + nonce + AESGCM(new_key).encrypt(nonce, plaintext, aad)
                except Exception as error:
                    raise InvalidBackupError("备份中的教材编辑草稿无法解密或校验，已拒绝恢复。") from error
                connection.execute("""UPDATE kg_parent_textbook_form_draft SET encrypted_payload=?
                        WHERE child_id=? AND scope=?""", (encrypted, row["child_id"], row["scope"]))
        if self._table_exists(connection, "notification_credential"):
            rows = connection.execute("SELECT credential_id, encrypted_json FROM notification_credential").fetchall()
            for row in rows:
                raw = base64.b64decode(row["encrypted_json"])
                plaintext = AESGCM(old_key).decrypt(raw[:12], raw[12:], b"hsc-notification-credential-v1")
                nonce = os.urandom(12)
                encrypted = AESGCM(new_key).encrypt(nonce, plaintext, b"hsc-notification-credential-v1")
                connection.execute(
                    "UPDATE notification_credential SET encrypted_json = ? WHERE credential_id = ?",
                    (base64.b64encode(nonce + encrypted).decode("ascii"), row["credential_id"]),
                )

    def _portable_media_relative_path(self, stored_path: str) -> Path:
        source = Path(stored_path)
        media_root = io_resolve(self.data_dir / "media",)
        try:
            return io_resolve(source,).relative_to(media_root)
        except ValueError:
            parts = source.parts
            try:
                media_index = max(index for index, part in enumerate(parts) if part == "media")
            except ValueError as error:
                raise InvalidBackupError("备份媒体引用无法安全重映射。") from error
            relative = Path(*parts[media_index + 1:])
            if not relative.parts or ".." in relative.parts:
                raise InvalidBackupError("备份媒体引用无法安全重映射。")
            return relative

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with io_path(path).open("rb") as input_file:
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _claim_maintenance(self, job_type: str, run_date: str) -> tuple[str | None, str]:
        now = utc_now()
        run_id = str(uuid4())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT run_id, status FROM maintenance_run WHERE job_type = ? AND run_date = ?",
                (job_type, run_date),
            ).fetchone()
            if existing and existing["status"] in {"completed", "processing"}:
                connection.rollback()
                return None, existing["status"]
            if existing:
                run_id = existing["run_id"]
                connection.execute(
                    """
                    UPDATE maintenance_run SET status = 'processing', started_at = ?, completed_at = NULL,
                      error_code = NULL, error_message = NULL, details_json = '{}'
                    WHERE run_id = ?
                    """,
                    (now, run_id),
                )
            else:
                connection.execute(
                    "INSERT INTO maintenance_run(run_id, job_type, run_date, status, started_at) VALUES (?, ?, ?, 'processing', ?)",
                    (run_id, job_type, run_date, now),
                )
            connection.commit()
        return run_id, "processing"

    def _finish_maintenance(
        self,
        run_id: str,
        status: str,
        details: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE maintenance_run SET status = ?, completed_at = ?, error_code = ?,
                  error_message = ?, details_json = ? WHERE run_id = ?
                """,
                (
                    status,
                    utc_now(),
                    error_code,
                    error_message,
                    json.dumps(details or {}, ensure_ascii=False),
                    run_id,
                ),
            )
            if status == "failed":
                self._audit(
                    connection,
                    "system",
                    "maintenance_failed",
                    None,
                    {"run_id": run_id, "error_code": error_code},
                    utc_now(),
                )
            connection.commit()

    def _backup_for_local_date(self, run_date: str) -> dict | None:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT backup_id, created_at FROM backup_manifest WHERE integrity_status = 'ok' ORDER BY created_at DESC"
            ).fetchall()
        for row in rows:
            created_at = datetime.fromisoformat(row["created_at"])
            if created_at.astimezone().date().isoformat() == run_date:
                return dict(row)
        return None

    def _prune_backups(self) -> None:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT backup_id, path, created_at FROM backup_manifest ORDER BY created_at DESC"
            ).fetchall()
            keep = {row["backup_id"] for row in rows[:7]}
            daily = set()
            for row in rows:
                created = datetime.fromisoformat(row["created_at"]).astimezone()
                day = created.date().isoformat()
                if day not in daily and len(daily) < 7:
                    keep.add(row["backup_id"])
                    daily.add(day)
            weekly = set()
            for row in rows:
                created = datetime.fromisoformat(row["created_at"]).astimezone()
                week = created.isocalendar()[:2]
                if week not in weekly and len(weekly) < 4:
                    keep.add(row["backup_id"])
                    weekly.add(week)
            root=(self.data_dir/'backups').absolute()
            # Restored historical rows can name another machine. Such metadata
            # never grants this process authority to delete outside its root.
            removed = [row for row in rows if row['backup_id'] not in keep
                       and Path(row['path']).absolute().parent==root
                       and Path(row['path']).name.startswith('home-study-')
                       and Path(row['path']).suffix=='.db']
            for row in removed:
                self._remove_backup_bundle(Path(row['path']))
                connection.execute("DELETE FROM backup_manifest WHERE backup_id = ?", (row["backup_id"],))
                # Later I/O failure must not roll back a prior completed bundle
                # deletion and advertise its missing file as a recovery point.
                connection.commit()
            connection.commit()

    def _snapshot_backup_media(self, database_snapshot: Path) -> tuple[int, int]:
        media_root = io_resolve(self.data_dir / "media",)
        backup_media = self._backup_media_dir(database_snapshot)
        metadata_path = self._backup_metadata_path(database_snapshot)
        io_path(backup_media).mkdir(mode=0o700, parents=False, exist_ok=False)
        files: list[dict] = []
        missing = 0
        connection = sqlite3.connect(database_snapshot)
        try:
            paths = [
                row[0]
                for table in ("voice_input", "capture_artifact")
                for row in connection.execute(
                    f"SELECT local_path FROM {table} WHERE local_path IS NOT NULL"
                ).fetchall()
            ]
            if self._table_exists(connection,"kg_parent_textbook_ocr_artifact"):
                columns = {column[1] for column in connection.execute("PRAGMA table_info(kg_parent_textbook_ocr_artifact)")}
                if "raw_image_path" in columns:
                    paths.extend(row[0] for row in connection.execute(
                        "SELECT raw_image_path FROM kg_parent_textbook_ocr_artifact WHERE raw_image_path IS NOT NULL"
                    ).fetchall())
        finally:
            connection.close()
        for stored_path in sorted(set(paths)):
            source = Path(stored_path)
            if not io_path(source).is_file():
                missing += 1
                continue
            try:
                relative = io_resolve(source,).relative_to(media_root)
            except ValueError as error:
                raise InvalidBackupError("原始媒体路径超出受管理目录，已拒绝创建不完整备份。") from error
            destination = backup_media / relative
            io_path(destination.parent).mkdir(parents=True, exist_ok=True)
            shutil.copy2(io_path(source), io_path(destination))
            files.append({
                "relative_path": relative.as_posix(),
                "size_bytes": io_path(destination).stat().st_size,
                "sha256": self._sha256_file(destination),
            })
        metadata = {
            "format": "hsc_backup_media_v1",
            "files": files,
            "missing_media_references": missing,
            "requires_existing_media_key": bool(files),
        }
        with io_path(metadata_path).open('x',encoding='utf-8')as out:
            out.write(json.dumps(metadata,sort_keys=True,separators=(',',':')))
        return len(files), missing

    def _backup_checksum(self, database_snapshot: Path) -> tuple[str, int]:
        from . import registered_asset_backup as registered
        database = registered.record(database_snapshot)
        metadata_path = self._backup_metadata_path(database_snapshot)
        if not io_path(metadata_path).is_file():
            return database['sha256'], database['size_bytes']
        contract = registered.record(metadata_path)
        metadata = registered.read_json(metadata_path)
        if metadata.get('format') == registered.FULL:
            files = registered.full_files(database_snapshot,metadata)
            if registered.record(metadata_path)!=contract:
                raise InvalidBackupError('BACKUP_METADATA_CHANGED')
            bundle = {'format':registered.FULL,'contract':contract,'files':files}
            return registered.sha(registered.canonical(bundle)), sum(x['size_bytes'] for x in files)+contract['size_bytes']
        if metadata.get('format') != 'hsc_backup_media_v1' or not isinstance(metadata.get('files'),list):
            raise ValueError('BACKUP_FULL_CONTRACT')
        verified_files=[];total_size=database['size_bytes']+io_path(metadata_path).stat().st_size
        names=set();folded=set()
        for item in metadata['files']:
            relative=item['relative_path'];registered.safe_relative(relative,registered.Policy(100000,64,1024,registered.M,registered.SAFE,registered.SAFE))
            if relative in names or relative.casefold() in folded:
                raise ValueError('BACKUP_CASE_COLLISION')
            names.add(relative);folded.add(relative.casefold())
            value=registered.record(self._backup_media_dir(database_snapshot)/relative)
            if value != {k:item[k]for k in ['size_bytes','sha256']}:
                raise ValueError('BACKUP_MEDIA_CHANGED')
            verified_files.append({'relative_path':relative,**value});total_size+=value['size_bytes']
        bundle={'database_sha256':database['sha256'],'files':verified_files,
                'missing_media_references':int(metadata.get('missing_media_references',0))}
        if registered.record(metadata_path)!=contract:
            raise InvalidBackupError('BACKUP_METADATA_CHANGED')
        return registered.sha(registered.canonical(bundle)),total_size

    def _remove_backup_bundle(self, snapshot: Path) -> int:
        from . import registered_asset_backup as registered
        from .registered_asset_snapshot import _check_directory
        snapshot=Path(snapshot)
        root=self.data_dir/'backups'
        if snapshot.parent.absolute() != root.absolute() or not snapshot.name.startswith('home-study-') or snapshot.suffix != '.db':
            raise InvalidBackupError('BACKUP_OWNED_SCOPE_REQUIRED')
        policy=registered.Policy(100000,64,1024,registered.M,registered.SAFE,registered.SAFE)
        registered.safe_relative(snapshot.name,policy)
        _check_directory(root,policy)
        removed=0
        for path in [snapshot,self._backup_metadata_path(snapshot),registered.capsule_path(snapshot),*[Path(str(snapshot)+s) for s in ['-wal','-shm','-journal']]]:
            if io_path(path).is_symlink():
                raise InvalidBackupError('BACKUP_MEMBER_ALIAS')
            if os.path.lexists(io_path(path)):
                io_path(path).unlink();removed+=1
        media=self._backup_media_dir(snapshot)
        if os.path.lexists(io_path(media)):
            if io_path(media).is_symlink():raise InvalidBackupError('BACKUP_MEMBER_ALIAS')
            shutil.rmtree(io_path(media));removed+=1
        if any(os.path.lexists(io_path(p))for p in [snapshot,self._backup_metadata_path(snapshot),registered.capsule_path(snapshot),media]):
            raise InvalidBackupError('BACKUP_CLEANUP_UNCONFIRMED')
        return removed

    def _require_registered_restore_migration(self, snapshot: Path, *, owner=None) -> None:
        # This slice creates complete encrypted backups; publishing a new owned
        # generation and remapping fixed registry columns belongs to the next slice.
        from . import registered_asset_backup as registered
        metadata_path=self._backup_metadata_path(snapshot)
        if metadata_path.is_file():
            metadata=registered.read_json(metadata_path)
            if metadata.get('registered',{}).get('asset_count',0):
                raise InvalidBackupError('REGISTERED_ASSET_RESTORE_MIGRATION_REQUIRED')
        # Old partial bundles with external registry authority are never promoted
        # to a full restore. Truly DB-only/managed-media legacy remains supported.
        # Both callers already verified the closed single-file snapshot checksum.
        # Legacy backup() can retain a WAL-mode header without any WAL payload.
        # immutable permits that cold file to be inspected without creating SHM
        # or changing its checksum; never ignore an actual unaccounted sidecar.
        for suffix in ['-wal','-shm','-journal']:
            if os.path.lexists(Path(str(snapshot)+suffix)):
                raise InvalidBackupError('BACKUP_UNACCOUNTED_SIDECAR')
        from .restore_publication import _sql
        with _sql(snapshot,owner=owner,readonly=True,immutable=True,timeout=5) as con:
            queries=[('curriculum_corpus','SELECT source_database_path,transcript_root_path,video_root_path FROM curriculum_corpus'),
                     ('curriculum_standard_source','SELECT local_root_path FROM curriculum_standard_source')]
            for table,query in queries:
                if self._table_exists(con,table) and any(any(row)for row in con.execute(query)):
                    raise InvalidBackupError('REGISTERED_ASSET_RESTORE_MIGRATION_REQUIRED')
            if self._table_exists(con,'kg_corpus'):
                for key,root in con.execute('SELECT corpus_id,transcript_root FROM kg_corpus'):
                    if root not in {'bundle:'+key,'local://family-textbook-ocr-pages'}:
                        raise InvalidBackupError('REGISTERED_ASSET_RESTORE_MIGRATION_REQUIRED')

    def _restore_backup_media(self, database_snapshot: Path) -> None:
        metadata_path = self._backup_metadata_path(database_snapshot)
        if not io_path(metadata_path).is_file():
            return
        metadata = json.loads(io_path(metadata_path).read_text(encoding="utf-8"))
        source_root = self._backup_media_dir(database_snapshot)
        target_root = self.data_dir / "media"
        previous_root = self.data_dir / f".media-before-restore-{uuid4()}"
        if io_path(target_root).exists():
            io_path(target_root).rename(io_path(previous_root))
        try:
            io_path(target_root).mkdir(parents=True, exist_ok=True)
            for item in metadata["files"]:
                relative = Path(item["relative_path"])
                destination = target_root / relative
                io_path(destination.parent).mkdir(parents=True, exist_ok=True)
                shutil.copy2(io_path(source_root / relative), io_path(destination))
        except Exception:
            shutil.rmtree(io_path(target_root), ignore_errors=True)
            if io_path(previous_root).exists():
                io_path(previous_root).rename(io_path(target_root))
            raise
        shutil.rmtree(io_path(previous_root), ignore_errors=True)

    def _restore_database_snapshot(self, source_path: Path, *, pause_restored_observation: bool = False) -> str:
        from .report_delivery import ReportDeliveryService

        # Do not expose a pre-send queue even briefly. Upgrade and quarantine an
        # isolated copy before the one live replacement; the original stays intact.
        # Canonicalize only the stdlib-selected scratch root before allocating
        # our new private directory. Live, backup and caller paths retain all
        # publication alias checks (macOS /var and /tmp are system symlinks).
        scratch_root = io_resolve(Path(tempfile.gettempdir()),strict=True)
        with io_temporary_directory(prefix="hsc-restore-staging-", dir=scratch_root) as temporary:
            staged = Database(Path(temporary) / "database.db", runtime_mode=self.database.runtime_mode)
            self._prepare_database_snapshot(source_path, staged, pause_restored_observation=pause_restored_observation)
            with staged.connect() as source:
                target = sqlite3.connect(self.database.path)
                try:
                    source.backup(target)
                    integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
                    if integrity != "ok":
                        raise InvalidBackupError("恢复后的数据库完整性检查失败。")
                    return integrity
                finally:
                    target.close()

    def _prepare_database_snapshot(self, source_path: Path, staged: Database, *, pause_restored_observation: bool = False) -> None:
        from .report_delivery import ReportDeliveryService

        from .restore_publication import _sql
        original_owner=staged._restore_sql_owner()
        with _sql(source_path,owner=original_owner,timeout=5)as source, _sql(staged.path,owner=original_owner,timeout=5)as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
        from .parent_restore_ownership import discard_imported
        discard_imported(staged)
        staged.initialize()
        from .behavior_model import BehaviorModelService
        BehaviorModelService(staged).initialize(force_daily_rebuild=True)
        from .voice_capture_binding import VoiceCaptureBindingService
        VoiceCaptureBindingService(staged)
        from .writing_teaching import WritingTeachingService
        WritingTeachingService(staged).initialize()
        from .writing_ocr_authorization import WritingOcrAuthorizationService
        WritingOcrAuthorizationService(staged).initialize()
        from .writing_question_authorization import WritingQuestionAuthorizationService, quarantine_after_restore as quarantine_writing_question
        WritingQuestionAuthorizationService(staged).initialize()
        from .paper_variant_authorization import PaperVariantAuthorizationService, quarantine_after_restore as quarantine_paper_variant
        PaperVariantAuthorizationService(staged).initialize()
        from .privacy_preserved_response import ensure_schema as ensure_preserved_response_schema
        from .privacy_source_withdrawal import ensure_schema as ensure_source_withdrawal_schema
        with staged.connect() as connection:
            ensure_preserved_response_schema(connection)
            ensure_source_withdrawal_schema(connection)
            connection.commit()
        from .local_ocr.review_queue import OcrReviewQueue
        OcrReviewQueue(staged)
        delivery = ReportDeliveryService(staged, self.data_encryption_key)
        delivery.initialize(recover_processing=False)
        with staged.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            from .privacy_operation_receipt import preserve_live_identities
            with self.database.connect() as live:
                # The incoming snapshot and live state are both examined while
                # the restore owner holds the external-effect guard.
                _combined_privacy_batches(connection)
                _preserve_combined_pilot_tombstones(live,connection)
                from .daily_report_combined_mode import preserve_on_restore as preserve_combined_mode
                preserve_combined_mode(live,connection)
                preserve_live_identities(live, connection)
                from .agent_request_ownership import preserve_live_claims
                preserve_live_claims(live, connection)
                from .voice_interaction_handoff import quarantine_after_restore as quarantine_voice_interaction_handoffs
                quarantine_voice_interaction_handoffs(live, connection)
                from .native_privacy import preserve_live_authority
                preserve_live_authority(live, connection)
                from .parent_restore_ownership import preserve_live as preserve_live_restore_ownership
                preserve_live_restore_ownership(live, connection,self.database,staged)
            if pause_restored_observation:
                # Restoring learning data does not authorize reopening a camera.
                # Keep rollback snapshots' original observation state unchanged.
                connection.execute("UPDATE daily_session SET observation_status='paused'")
            delivery.quarantine_after_restore(connection, str(uuid4()))
            if self._table_exists(connection, 'daily_observation_delivery_barrier'):
                from .daily_report_delivery_state import block_after_restore
                block_after_restore(connection)
            if self._table_exists(connection, 'daily_visual_schedule'):
                # A restored schedule may contain an earlier cursor or revoked
                # consent. Require explicit re-enablement after reconciliation.
                connection.execute('UPDATE daily_visual_schedule SET enabled=0')
            if self._table_exists(connection, 'daily_report_email_verification'):
                connection.execute('DELETE FROM daily_report_email_verification')
            # A restored local draft has not been revalidated against current
            # sources, deletion history or recipient authority. Drop it.
            if self._table_exists(connection, 'daily_observation_report_source'):
                if self._table_exists(connection, 'daily_observation_report_snapshot'):
                    connection.execute('DELETE FROM daily_observation_report_snapshot')
                connection.execute('DELETE FROM daily_observation_report_source')
            quarantine_writing_question(connection, str(uuid4()))
            quarantine_paper_variant(connection, str(uuid4()))
            from .pet_audio_work import quarantine_after_restore as quarantine_pet_audio
            quarantine_pet_audio(connection)
            from .pet_speaking_candidate_jobs import quarantine_after_restore as quarantine_speaking_candidates
            quarantine_speaking_candidates(connection)
            from .pet_multipa_execution_receipt import quarantine_after_restore as quarantine_cpu_receipts
            quarantine_cpu_receipts(connection,str(uuid4()))
            from .visual_material_lifecycle import quarantine_after_restore as quarantine_visual_materials
            quarantine_visual_materials(connection, str(uuid4()))
            from .curriculum_first_source import quarantine_after_restore as quarantine_first_sources
            quarantine_first_sources(connection, str(uuid4()))
            from .curriculum_authority_lifecycle import quarantine_after_restore as quarantine_curriculum_authorities
            quarantine_curriculum_authorities(connection)
            from .rubric_calibration import initialize as initialize_calibration, quarantine_after_restore as quarantine_calibration
            initialize_calibration(connection)
            quarantine_calibration(connection, str(uuid4()))
            from .local_ocr.cache_lifecycle import ensure_schema as ensure_ocr_cache_schema, quarantine_after_restore as quarantine_ocr_cache
            ensure_ocr_cache_schema(connection)
            quarantine_ocr_cache(connection, str(uuid4()))
            if self._table_exists(connection, 'voice_capture_binding'):
                connection.execute("UPDATE voice_capture_binding SET status='cancelled',updated_at=?", (utc_now(),))
            if self._table_exists(connection, 'voice_control_intent_request'):
                connection.execute("UPDATE voice_control_intent_request SET status='cancelled',completed_at=?", (utc_now(),))
            if getattr(staged, '_restore_work_capability', None) is not None:
                from .parent_restore_privacy import preserve_original_jobs
                with self.database.connect() as live:
                    preserve_original_jobs(self.database, staged, live, connection)
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise InvalidBackupError("隔离恢复副本的数据关联不完整。")
            connection.commit()

    @staticmethod
    def _backup_media_dir(database_snapshot: Path) -> Path:
        return database_snapshot.with_name(f"{database_snapshot.stem}.media")

    @staticmethod
    def _backup_metadata_path(database_snapshot: Path) -> Path:
        return database_snapshot.with_name(f"{database_snapshot.stem}.media.json")

    def system_health(self) -> dict:
        usage = shutil.disk_usage(self.data_dir)
        with self.database.connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            pending_reports = connection.execute("SELECT COUNT(*) FROM report_job WHERE status = 'pending'").fetchone()[0]
            pending_reports += connection.execute(
                "SELECT COUNT(*) FROM period_report_refresh_state WHERE status IN ('processing', 'failed')"
            ).fetchone()[0]
            pending_agent_turns = connection.execute("SELECT COUNT(*) FROM agent_turn WHERE tool_status = 'pending'").fetchone()[0]
            recent_model_failures = connection.execute("SELECT COUNT(*) FROM model_call WHERE status = 'failed' AND created_at >= datetime('now', '-7 days')").fetchone()[0]
            pending_content_generation = sum(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE status IN ('pending','processing')"
                ).fetchone()[0]
                for table in ("variant_generation_job", "specialized_supply_job")
                if self._table_exists(connection, table)
            )
            active_sensor_leases = connection.execute("SELECT COUNT(*) FROM sensor_lease WHERE status = 'active'").fetchone()[0]
            last_backup = connection.execute("SELECT backup_id, created_at, integrity_status, reason FROM backup_manifest ORDER BY created_at DESC LIMIT 1").fetchone()
            maintenance_runs = connection.execute(
                """
                SELECT job_type, run_date, status, started_at, completed_at,
                       error_code, error_message
                FROM maintenance_run ORDER BY started_at DESC LIMIT 10
                """
            ).fetchall()
            device_profiles = connection.execute(
                """
                SELECT dp.device_type, dp.system_id_hash, dp.status, dp.last_seen_at,
                       EXISTS(SELECT 1 FROM sensor_lease sl WHERE sl.device_id = dp.device_id AND sl.status = 'active') AS application_reading,
                       (SELECT sl.release_reason FROM sensor_lease sl WHERE sl.device_id = dp.device_id AND sl.status != 'active' ORDER BY sl.released_at DESC LIMIT 1) AS last_release_reason
                FROM device_profile dp ORDER BY dp.last_seen_at DESC
                """
            ).fetchall()
            device_bindings = connection.execute(
                """
                SELECT device_type, system_id_hash, label, verified_at
                FROM device_binding ORDER BY device_type
                """
            ).fetchall()
            device_incidents = connection.execute(
                """
                SELECT di.device_type, di.error_code, di.message, di.status,
                       di.occurred_at, di.resolved_at, cp.nickname AS child_nickname
                FROM device_incident di JOIN child_profile cp ON cp.child_id = di.child_id
                ORDER BY di.occurred_at DESC LIMIT 10
                """
            ).fetchall()
            observation_rows = connection.execute(
                """
                SELECT cse.child_id, cp.nickname AS child_nickname, cse.session_id,
                       cse.state, cse.confidence, cse.reason, cse.observed_at
                FROM child_state_event cse
                JOIN child_profile cp ON cp.child_id = cse.child_id
                WHERE datetime(cse.observed_at) >= datetime('now', '-24 hours')
                ORDER BY cse.observed_at DESC
                """
            ).fetchall()
            qualification_rows = connection.execute(
                """
                WITH latest_qualification AS (
                  SELECT oq.*, ROW_NUMBER() OVER (
                    PARTITION BY oq.child_id ORDER BY oq.updated_at DESC, oq.session_id DESC
                  ) AS row_rank
                  FROM observation_qualification oq
                )
                SELECT oq.child_id, cp.nickname AS child_nickname, oq.status,
                       oq.scene_quality, oq.person_baseline, oq.reason, oq.source,
                       oq.attempt_count, oq.updated_at,
                       (SELECT COUNT(*) FROM observation_correction oc
                        WHERE oc.child_id = oq.child_id
                          AND datetime(oc.occurred_at) >= datetime('now', '-7 days')) AS correction_count_7d,
                       (
                         COALESCE(fr.observation_mode, 'shadow') = 'shadow'
                         AND EXISTS(
                         SELECT 1 FROM audit_event ae
                         WHERE ae.child_id = oq.child_id
                           AND ae.action = 'observation_auto_shadowed'
                           -- The automatic switch and the current rule update
                           -- share one timestamp.  A later parent change back
                           -- to gentle must not leave a stale warning visible.
                           AND ae.occurred_at = fr.updated_at
                         )
                       ) AS auto_shadowed,
                       COALESCE(fr.observation_mode, 'shadow') AS observation_mode
                FROM latest_qualification oq
                JOIN child_profile cp ON cp.child_id = oq.child_id
                LEFT JOIN family_rule fr ON fr.child_id = oq.child_id
                WHERE oq.row_rank = 1
                ORDER BY oq.updated_at DESC
                """
            ).fetchall()
            model_failure_items = connection.execute(
                """
                SELECT purpose, provider, model, error_code, created_at
                FROM model_call WHERE status = 'failed'
                ORDER BY created_at DESC LIMIT 10
                """
            ).fetchall()
            report_failure_items = connection.execute(
                """
                SELECT report_type, last_error, attempts, updated_at
                FROM report_job WHERE status = 'pending' AND last_error IS NOT NULL
                ORDER BY updated_at DESC LIMIT 10
                """
            ).fetchall()
            period_report_failure_items = connection.execute(
                """
                SELECT prs.report_type, prs.last_error, prs.attempts, prs.updated_at,
                       cp.nickname AS child_nickname
                FROM period_report_refresh_state prs
                JOIN child_profile cp ON cp.child_id = prs.child_id
                WHERE prs.status = 'failed'
                ORDER BY prs.updated_at DESC LIMIT 10
                """
            ).fetchall()
            agent_failure_items = connection.execute(
                """
                SELECT at.intent, at.tool_error_code, at.created_at, cp.nickname AS child_nickname
                FROM agent_turn at JOIN child_profile cp ON cp.child_id = at.child_id
                WHERE at.tool_status = 'failed'
                ORDER BY at.completed_at DESC, at.created_at DESC LIMIT 10
                """
            ).fetchall()
            controlled_game_failure_items = connection.execute(
                """
                SELECT cgs.title, cgs.status, cgs.end_reason, cgs.updated_at,
                       cp.nickname AS child_nickname
                FROM controlled_game_session cgs
                JOIN child_profile cp ON cp.child_id = cgs.child_id
                WHERE cgs.status IN ('failed', 'interrupted')
                  AND datetime(cgs.updated_at) >= datetime('now', '-7 days')
                ORDER BY cgs.updated_at DESC LIMIT 10
                """
            ).fetchall()
            active_controlled_game_items = connection.execute(
                """
                SELECT cgs.game_session_id, cgs.title, cgs.started_at, cgs.ends_at,
                       cgs.os_process_id, cp.nickname AS child_nickname,
                       CASE WHEN datetime(cgs.ends_at) <= datetime('now') THEN 1 ELSE 0 END AS overdue
                FROM controlled_game_session cgs
                JOIN child_profile cp ON cp.child_id = cgs.child_id
                WHERE cgs.status = 'active'
                ORDER BY cgs.started_at DESC LIMIT 10
                """
            ).fetchall()
            content_generation_failure_items = []
            if self._table_exists(connection, "variant_generation_job"):
                content_generation_failure_items.extend(connection.execute(
                    """SELECT '错题变式' AS source,error_code,attempt_count,updated_at,
                              child_id,mistake_id AS reference
                       FROM variant_generation_job WHERE status IN ('failed','blocked')"""
                ).fetchall())
            if self._table_exists(connection, "specialized_supply_job"):
                content_generation_failure_items.extend(connection.execute(
                    """SELECT '专项诊断' AS source,error_code,attempt_count,updated_at,
                              child_id,domain_id AS reference
                       FROM specialized_supply_job WHERE status IN ('failed','blocked')"""
                ).fetchall())
            content_generation_failure_items.sort(key=lambda row: row["updated_at"], reverse=True)
            content_generation_failure_items = content_generation_failure_items[:10]
            pending_deletions = connection.execute(
                "SELECT COUNT(*) FROM deletion_job WHERE status IN ('requested', 'scanning', 'deleting', 'verifying')"
            ).fetchone()[0]
            deletion_failure_rows = connection.execute(
                """
                SELECT dj.deletion_id, dj.client_action_id, dj.child_id, dj.scope, dj.target_type, dj.error_code,
                       dj.error_message, dj.residual_paths_json, dj.updated_at,
                       cp.nickname AS child_nickname
                FROM deletion_job dj
                JOIN child_profile cp ON cp.child_id = dj.child_id
                WHERE dj.status = 'failed'
                ORDER BY dj.updated_at DESC LIMIT 10
                """
            ).fetchall()
            historical_deletions = {row['deletion_id'] for row in deletion_failure_rows
                                    if not self._deletion_generation_current(connection, row)}
            raw_paths = [
                row[0]
                for table in ("voice_input", "capture_artifact")
                for row in connection.execute(f"SELECT local_path FROM {table} WHERE local_path IS NOT NULL").fetchall()
            ]
        profiles = [dict(row) for row in device_profiles]
        bindings = {row["device_type"]: dict(row) for row in device_bindings}
        maintenance_items = [dict(row) for row in maintenance_runs]
        latest_maintenance = {
            job_type: next((item for item in maintenance_items if item["job_type"] == job_type), None)
            for job_type in ("daily_backup", "retention_cleanup")
        }
        deletion_failures = [
            {
                "deletion_id": row["deletion_id"],
                "scope": row["scope"],
                "target_type": row["target_type"],
                "error_code": row["error_code"],
                "error_message": ('恢复前的历史删除记录，不适用于当前数据，不会重复执行。若需删除当前数据，请重新核对范围。'
                                  if row['deletion_id'] in historical_deletions else row["error_message"]),
                "historical": row['deletion_id'] in historical_deletions,
                "retry_allowed": row['deletion_id'] not in historical_deletions,
                "remaining_file_count": len(json.loads(row["residual_paths_json"] or "[]")),
                "updated_at": row["updated_at"],
                "child_nickname": row["child_nickname"],
            }
            for row in deletion_failure_rows
        ]
        observation_quality: list[dict] = []
        qualification_by_child = {row["child_id"]: row for row in qualification_rows}
        observation_by_child: dict[str, list] = {}
        for row in observation_rows:
            observation_by_child.setdefault(row["child_id"], []).append(row)
        for rows in observation_by_child.values():
            latest = rows[0]
            usable = sum(1 for row in rows if row["state"] != "unknown")
            qualification = qualification_by_child.pop(latest["child_id"], None)
            observation_quality.append(
                {
                    "child_nickname": latest["child_nickname"],
                    "session_id": latest["session_id"],
                    "sample_count": len(rows),
                    "usable_sample_count": usable,
                    "usable_ratio": round(usable / len(rows), 3),
                    "latest_state": latest["state"],
                    "latest_reason": latest["reason"],
                    "latest_observed_at": latest["observed_at"],
                    **self._observation_qualification_summary(qualification),
                }
            )
        # A dark, unavailable, or freshly restarted camera can have no
        # state-event at all.  It is still useful parent-facing quality
        # information, and remains frame-free.
        for qualification in qualification_by_child.values():
            observation_quality.append(
                {
                    "child_nickname": qualification["child_nickname"],
                    "session_id": None,
                    "sample_count": 0,
                    "usable_sample_count": 0,
                    "usable_ratio": 0.0,
                    "latest_state": "unknown",
                    "latest_reason": qualification["reason"],
                    "latest_observed_at": qualification["updated_at"],
                    **self._observation_qualification_summary(qualification),
                }
            )
        devices = []
        for device_type in ("camera", "document_camera", "microphone"):
            binding = bindings.get(device_type)
            profile = next(
                (
                    item
                    for item in profiles
                    if item["device_type"] == device_type
                    and (not binding or item["system_id_hash"] == binding["system_id_hash"])
                ),
                None,
            )
            if profile:
                devices.append({**profile, "binding_label": binding["label"] if binding else None})
            elif binding:
                devices.append(
                    {
                        "device_type": device_type,
                        "system_id_hash": binding["system_id_hash"],
                        "status": "ready",
                        "last_seen_at": binding["verified_at"],
                        "application_reading": 0,
                        "last_release_reason": None,
                        "binding_label": binding["label"],
                    }
                )
        return {
            "database_integrity": integrity,
            "database_size_bytes": self.database.path.stat().st_size if self.database.path.exists() else 0,
            "database_upgrade": self.database.latest_upgrade_status(),
            "disk_free_bytes": usage.free,
            "pending_report_jobs": pending_reports,
            "pending_agent_turns": pending_agent_turns,
            "pending_content_generation_jobs": pending_content_generation,
            "pending_deletion_jobs": pending_deletions,
            "model_failures_last_7_days": recent_model_failures,
            "active_sensor_leases": active_sensor_leases,
            "last_backup": dict(last_backup) if last_backup else None,
            "maintenance": latest_maintenance,
            "recent_maintenance_failures": [
                item for item in maintenance_items if item["status"] == "failed"
            ],
            "raw_media_files_stored": sum(1 for path in raw_paths if Path(path).is_file()),
            "devices": devices,
            "device_incidents": [dict(row) for row in device_incidents],
            "observation_quality": observation_quality,
            "recent_model_failures": [dict(row) for row in model_failure_items],
            "recent_report_failures": [dict(row) for row in report_failure_items] + [dict(row) for row in period_report_failure_items],
            "recent_agent_failures": [dict(row) for row in agent_failure_items],
            "recent_controlled_game_failures": [dict(row) for row in controlled_game_failure_items],
            "active_controlled_games": [dict(row) for row in active_controlled_game_items],
            "recent_content_generation_failures": [dict(row) for row in content_generation_failure_items],
            "recent_deletion_failures": deletion_failures,
        }

    @staticmethod
    def _rows(connection, sql: str, params: tuple) -> list[dict]:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]

    @staticmethod
    def _table_exists(connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    @classmethod
    def _optional_rows(cls, connection, table: str, child_id: str) -> list[dict]:
        if not cls._table_exists(connection, table):
            return []
        return cls._rows(connection, f"SELECT * FROM {table} WHERE child_id = ?", (child_id,))

    @classmethod
    def _optional_count(cls, connection, table: str, child_id: str) -> int:
        return cls._count(connection, table, child_id) if cls._table_exists(connection, table) else 0

    @staticmethod
    def _observation_qualification_summary(row) -> dict:
        if row is None:
            return {
                "qualification_status": "not_recorded",
                "scene_quality": "not_evaluated",
                "person_baseline": "not_evaluated",
                "qualification_source": None,
                "qualification_attempt_count": 0,
                "correction_count_7d": 0,
                "auto_shadowed": False,
                "observation_mode": "shadow",
            }
        return {
            "qualification_status": row["status"],
            "scene_quality": row["scene_quality"],
            "person_baseline": row["person_baseline"],
            "qualification_source": row["source"],
            "qualification_attempt_count": row["attempt_count"],
            "correction_count_7d": row["correction_count_7d"],
            "auto_shadowed": bool(row["auto_shadowed"]),
            "observation_mode": row["observation_mode"],
        }

    @staticmethod
    def _count(connection, table: str, child_id: str) -> int:
        return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE child_id = ?", (child_id,)).fetchone()[0]

    @staticmethod
    def _delete(connection, sql: str, child_id: str) -> int:
        try:
            return connection.execute(sql, (child_id,)).rowcount
        except sqlite3.IntegrityError:
            words = sql.replace("\n", " ").split()
            table = words[words.index("FROM") + 1] if "FROM" in words else "unknown"
            print("HSC_DIAGNOSTIC " + json.dumps({
                "event": "privacy_delete_dependency_blocked",
                "level": "error",
                "details": {"table": table},
            }, ensure_ascii=False), flush=True)
            raise

    def _record_deletion_trace(self, deletion_id: str, statement: str) -> None:
        words = statement.replace("\n", " ").split()
        if not words:
            return
        operation = words[0].upper()
        table = None
        if operation == "DELETE" and len(words) > 2 and words[1].upper() == "FROM":
            table = words[2]
        elif operation == "UPDATE" and len(words) > 1:
            table = words[1]
        elif operation == "INSERT" and len(words) > 2 and words[1].upper() == "INTO":
            table = words[2]
        if table:
            trace = self._deletion_trace.setdefault(deletion_id, {"history": []})
            trace.update({
                "operation": operation,
                "table": table.strip('"`[]'),
            })
            trace["history"] = (trace["history"] + [
                {"operation": trace["operation"], "table": trace["table"]}
            ])[-12:]

    @staticmethod
    def _require_child(connection, child_id: str) -> None:
        if not connection.execute("SELECT 1 FROM child_profile WHERE child_id = ?", (child_id,)).fetchone():
            raise UnknownChildError(child_id)

    @staticmethod
    def _audit(connection, actor: str, action: str, child_id: str | None, summary: dict, occurred_at: str) -> str:
        audit_id = str(uuid4())
        connection.execute(
            "INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?)",
            (audit_id, actor, action, child_id, json.dumps(summary, ensure_ascii=False), occurred_at),
        )
        return audit_id


class InvalidPrivacyPreferenceError(Exception):
    pass


class UnknownCaptureArtifactError(Exception):
    pass


class DeletionConfirmationError(Exception):
    pass


class InvalidDeletionScopeError(Exception):
    pass


class CloudProcessingDisabledError(Exception):
    pass


class RestoreConfirmationError(Exception):
    def __init__(self, required_confirmation="确认恢复"):
        self.required_confirmation = required_confirmation
        super().__init__(f"请输入“{required_confirmation}”。")


class RestoreWhileSessionActiveError(Exception):
    pass


class UnknownBackupError(Exception):
    pass


class InvalidBackupError(Exception):
    pass


class RecoveryPointRotationError(Exception):
    pass


class DeletionIncompleteError(Exception):
    def __init__(self, deletion_id: str, error_code: str, remaining_count: int):
        super().__init__(error_code)
        self.deletion_id = deletion_id
        self.error_code = error_code
        self.remaining_count = remaining_count


class UnknownDeletionJobError(Exception):
    pass


class InvalidDeletionActionError(Exception):
    pass

# Capture the original trusted business producer, never a caller-supplied frame name.
_SOURCE_EXPORT_PRODUCER_CODE=PrivacyLifecycleService.create_backup.__code__
