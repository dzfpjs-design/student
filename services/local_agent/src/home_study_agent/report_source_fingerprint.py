"""Local report-input invalidation, not learning qualification or cloud consent.

Only SELECTs on the borrowed connection. Callers own a consistent read/write
transaction and must compare the same token before/after gathering evidence.
Keep existing reliable-learning, mistake and specialized qualification guards.
External/custom behavior providers are not covered by this SQLite token. Habit
outcome caches are deliberately excluded: their window_end changes on each read;
the underlying observations, retractions and experiment boundaries are covered.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3


def report_source_fingerprint(c: sqlite3.Connection, session_id: str) -> str:
    """Return an opaque digest; never initialize schema, commit or open a DB."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError('REPORT_SOURCE_SESSION_NOT_FOUND')
    session = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
    if session is None:
        raise ValueError('REPORT_SOURCE_SESSION_NOT_FOUND')
    child_id = session[0]
    payload = {'version': 'report_source_v1', 'session_id': session_id, 'child_id': child_id}
    from .pet_audio_memory import report_memory
    oral = report_memory(c, child_id, session_id)['items']
    if oral:
        payload['pet_audio_practice'] = oral
    from .document_revisit_memory import report_memory as revisit_memory
    revisits = revisit_memory(c, child_id, session_id)['items']
    if revisits:
        payload['document_revisit_practice'] = revisits
    from .behavior_model.service import DAILY_PROJECTION_VERSION
    payload['behavior_calendar'] = DAILY_PROJECTION_VERSION
    presence = {}

    def exists(table):
        if table not in presence:
            presence[table] = bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
        return presence[table]

    def rows(key, tables, sql, args=(child_id,)):
        # A missing optional module and its initialized, empty schema are equal.
        payload[key] = [tuple(row) for row in c.execute(sql, args)] if all(exists(t) for t in tables) else []

    rows('session', ('daily_session',), '''SELECT session_id,child_id,status,state,started_at,ended_at,
        recall_declared_empty,homework_declared_empty FROM daily_session WHERE session_id=?''', (session_id,))
    from .report_calendar import study_day
    report_day = study_day(c, session_id)
    report_end = payload['session'][0][5]
    payload['study_day'] = report_day
    payload['supply_as_of'] = []
    if payload['session'][0][2] == 'completed' and all(exists(t) for t in (
            'plan_constraint_decision', 'daily_plan', 'plan_item', 'plan_item_event')):
        from .parent_supply_followup import report_supply_facts
        class ReadRows:
            # Use cursor-local row names without changing the caller's row factory.
            def execute(self, sql, parameters=()):
                cursor = c.cursor()
                cursor.row_factory = sqlite3.Row
                return cursor.execute(sql, parameters)
        try:
            payload['supply_as_of'] = report_supply_facts(ReadRows(), child_id, session_id)
        except (ValueError, KeyError, TypeError):
            # Invalidated source must differ from a captured valid source. The
            # report builder rejects it; this is not an empty-evidence fallback.
            payload['supply_as_of'] = {'status': 'source_unavailable'}
    rows('child', ('child_profile',), 'SELECT child_id,nickname,grade,curriculum_region,status FROM child_profile WHERE child_id=?')
    rows('reflection', ('daily_reflection',), '''SELECT reflection_id,child_id,learned_text,difficult_text,feeling,next_action,skipped,created_at
        FROM daily_reflection WHERE session_id=? ORDER BY reflection_id''', (session_id,))
    rows('recall', ('classroom_recall',), '''SELECT recall_id,subject,summary,confidence,created_at FROM classroom_recall
        WHERE session_id=? AND confirmation_status='child_confirmed' ORDER BY created_at,recall_id''', (session_id,))
    if c.execute('SELECT 1 FROM daily_plan WHERE session_id=? AND child_id!=? LIMIT 1', (session_id,child_id)).fetchone():
        payload['foreign_plan_conflict'] = True
    rows('tasks', ('daily_plan', 'plan_item'), '''SELECT pi.plan_item_id,pi.plan_id,pi.subject,pi.title,pi.expected_minutes,
        pi.actual_minutes,pi.status,pi.position,pi.completion_evidence_type,pi.completion_note,pi.task_type,pi.completion_checklist_json
        FROM plan_item pi JOIN daily_plan dp ON dp.plan_id=pi.plan_id
        WHERE dp.session_id=? AND dp.child_id=? AND pi.source_type!='planned_break' ORDER BY pi.position,pi.plan_item_id''', (session_id, child_id))
    # Bind the type, ownership and status of referenced artifacts; raw OCR text
    # is not needed here. A transition to unconfirmed must invalidate a report.
    rows('task_sources', ('daily_plan', 'plan_item', 'homework_item', 'capture_artifact'),
         """SELECT pi.plan_item_id,pi.homework_id,pi.completion_artifact_id,pi.source_type,pi.source_ref,
           ca.child_id,ca.session_id,ca.artifact_type,ca.status,
           h.child_id,h.session_id,h.source_artifact_id,
           ha.child_id,ha.session_id,ha.artifact_type,ha.status
         FROM plan_item pi JOIN daily_plan dp ON dp.plan_id=pi.plan_id
         LEFT JOIN homework_item h ON h.homework_id=pi.homework_id
         LEFT JOIN capture_artifact ca ON ca.artifact_id=pi.completion_artifact_id
         LEFT JOIN capture_artifact ha ON ha.artifact_id=h.source_artifact_id
         WHERE dp.child_id=? AND dp.session_id=? AND pi.source_type!='planned_break'
           AND (pi.completion_artifact_id IS NOT NULL OR pi.homework_id IS NOT NULL OR (pi.source_type='school_homework' AND pi.source_ref IS NOT NULL))
         ORDER BY pi.plan_item_id""", (child_id,session_id))
    # Weekly goal/cycle projections may draw completed tasks from older sessions.
    # Bind their referenced artifacts too, so later source reclassification makes
    # a previously generated report stale without exposing OCR text.
    from .report_task_provenance import aggregate_task_ids
    for item_id in aggregate_task_ids(c, child_id, report_day, report_end):
        source = c.execute("""SELECT pi.plan_item_id,pi.homework_id,pi.completion_artifact_id,pi.source_type,pi.source_ref,
           ca.child_id,ca.session_id,ca.artifact_type,ca.status,
           h.child_id,h.session_id,h.source_artifact_id,
           ha.child_id,ha.session_id,ha.artifact_type,ha.status
         FROM plan_item pi JOIN daily_plan dp ON dp.plan_id=pi.plan_id
         LEFT JOIN homework_item h ON h.homework_id=pi.homework_id
         LEFT JOIN capture_artifact ca ON ca.artifact_id=pi.completion_artifact_id
         LEFT JOIN capture_artifact ha ON ha.artifact_id=h.source_artifact_id
         WHERE dp.child_id=? AND pi.plan_item_id=? AND dp.session_id!=?
           AND (pi.completion_artifact_id IS NOT NULL OR pi.homework_id IS NOT NULL OR (pi.source_type='school_homework' AND pi.source_ref IS NOT NULL))""", (child_id,item_id,session_id)).fetchone()
        if source is not None:
            payload['task_sources'].append(tuple(source))
    payload['task_sources'].sort(key=lambda row: row[0])
    # Bind every upstream carryover receipt and source homework, including
    # multi-step chains. Invalid lineages remain a stable invalid marker so a
    # formerly valid frozen report becomes stale without exposing source text.
    from .report_task_provenance import task_source_state
    qualification = []
    for row in payload['task_sources']:
        try:
            value = task_source_state(c, child_id, row[0])
        except (ValueError, sqlite3.Error, TypeError):
            value = [('source_unconfirmed',)]
        if value:
            qualification.append((row[0], value))
    if qualification:
        payload['task_source_qualification'] = qualification
    if not payload['task_sources']:
        del payload['task_sources']
    rows('reschedules', ('daily_plan', 'plan_item', 'plan_item_event'), '''SELECT e.event_id,e.plan_item_id,e.occurred_at,e.payload_json
        FROM plan_item_event e JOIN plan_item pi ON pi.plan_item_id=e.plan_item_id JOIN daily_plan dp ON dp.plan_id=pi.plan_id
        WHERE dp.session_id=? AND dp.child_id=? AND pi.source_type!='planned_break' AND e.event_type='rescheduled'
        ORDER BY e.plan_item_id,e.occurred_at,e.event_id''', (session_id, child_id))
    rows('planning', ('session_planning_context',), '''SELECT energy_level,fixed_commitment_minutes,fixed_commitment_note,
        tomorrow_event_subject,tomorrow_event_title,tomorrow_event_minutes FROM session_planning_context WHERE session_id=?''', (session_id,))
    rows('family', ('family_rule',), 'SELECT max_learning_minutes,pet_min_minutes,competition_minutes FROM family_rule WHERE child_id=?')
    rows('weekly', ('weekly_goal', 'daily_plan', 'plan_item'), '''SELECT wg.weekly_goal_id,wg.track,wg.target_minutes,wg.status,
        COALESCE(SUM(CASE WHEN pi.status='completed' THEN COALESCE(pi.actual_minutes,pi.expected_minutes) ELSE 0 END),0)
        FROM weekly_goal wg LEFT JOIN daily_plan dp ON dp.child_id=wg.child_id AND dp.plan_date BETWEEN wg.week_start AND wg.week_end AND dp.plan_date<=?
        LEFT JOIN plan_item pi ON pi.plan_id=dp.plan_id AND pi.source_type=CASE wg.track WHEN 'pet' THEN 'pet_daily' ELSE 'competition_math' END
        AND julianday(COALESCE(pi.completed_at,(SELECT ended_at FROM daily_session
          WHERE session_id=dp.session_id AND child_id=wg.child_id)))<=julianday(?)
        WHERE wg.child_id=? AND ? BETWEEN wg.week_start AND wg.week_end
        GROUP BY wg.weekly_goal_id ORDER BY wg.weekly_goal_id''', (report_day, report_end, child_id, report_day))
    rows('goals', ('goal_program',), '''SELECT program_id,track,title,target_statement,target_horizon_json,priority,
        default_weekly_minutes,default_weekly_sessions FROM goal_program WHERE child_id=? AND status='active' ORDER BY program_id''')
    rows('cycles', ('goal_cycle', 'goal_program', 'daily_plan', 'plan_item'), '''SELECT gc.cycle_id,gc.program_id,gc.week_start,gc.week_end,
        gc.target_minutes,gc.target_sessions,
        COALESCE(SUM(CASE WHEN pi.status='completed' THEN COALESCE(pi.actual_minutes,pi.expected_minutes) ELSE 0 END),0),
        COUNT(DISTINCT CASE WHEN pi.status='completed' THEN dp.session_id END)
        FROM goal_cycle gc JOIN goal_program gp ON gp.program_id=gc.program_id AND gp.child_id=gc.child_id
        LEFT JOIN daily_plan dp ON dp.child_id=gc.child_id AND dp.plan_date BETWEEN gc.week_start AND gc.week_end AND dp.plan_date<=?
        LEFT JOIN plan_item pi ON pi.plan_id=dp.plan_id AND ((gp.track='pet_path' AND pi.source_type='pet_daily')
          OR (gp.track='competition_math_path' AND pi.source_type='competition_math')
          OR (gp.track='junior_exam_readiness' AND pi.source_type IN ('junior_exam_goal','mistake_review','due_review')))
        AND julianday(COALESCE(pi.completed_at,(SELECT ended_at FROM daily_session
          WHERE session_id=dp.session_id AND child_id=gc.child_id)))<=julianday(?)
        WHERE gc.child_id=? AND gp.status='active' AND ? BETWEEN gc.week_start AND gc.week_end
        GROUP BY gc.cycle_id ORDER BY gc.cycle_id''', (report_day, report_end, child_id, report_day))
    rows('legacy_parent_feedback', ('parent_report_feedback', 'daily_report'), '''SELECT f.feedback_id,f.report_id,f.executed,
        f.effectiveness,f.stress_rating,f.notes_summary,f.updated_at FROM parent_report_feedback f
        JOIN daily_report r ON r.report_id=f.report_id AND r.child_id=f.child_id WHERE f.child_id=? ORDER BY f.feedback_id''')
    rows('snapshots', ('parent_report_snapshot',), '''SELECT snapshot_id,source_kind,source_id,source_version,evidence_version,
        status,parent_edit_json,approved_at,approved_by,content_json FROM parent_report_snapshot WHERE child_id=? ORDER BY snapshot_id''')
    rows('snapshot_feedback', ('parent_report_snapshot', 'parent_report_snapshot_feedback'), '''SELECT f.feedback_id,f.snapshot_id,
        f.helpfulness,f.stress_rating,f.notes,f.created_at FROM parent_report_snapshot_feedback f
        JOIN parent_report_snapshot s ON s.snapshot_id=f.snapshot_id WHERE s.child_id=? ORDER BY f.feedback_id''')
    rows('feedback_revision', ('report_feedback_revision',), '''SELECT event_id,snapshot_id,revision,operation,feedback_id,payload_json,created_at
        FROM report_feedback_revision e WHERE child_id=? AND revision=(SELECT MAX(revision) FROM report_feedback_revision WHERE snapshot_id=e.snapshot_id)
        ORDER BY snapshot_id''')
    rows('actions', ('parent_action_snapshot',), '''SELECT snapshot_id,report_id,period_report_id,version,fingerprint,payload_json,created_at
        FROM parent_action_snapshot WHERE child_id=? ORDER BY snapshot_id''')
    rows('action_feedback', ('parent_action_feedback_event', 'parent_action_snapshot'), '''SELECT e.event_id,e.snapshot_id,e.revision,e.operation,e.payload_json,e.created_at
        FROM parent_action_feedback_event e JOIN parent_action_snapshot s ON s.snapshot_id=e.snapshot_id AND s.child_id=e.child_id
        WHERE e.child_id=? AND e.revision=(SELECT MAX(revision) FROM parent_action_feedback_event WHERE snapshot_id=e.snapshot_id)
        ORDER BY e.snapshot_id''')
    # Approval audits are evidence for reviewed actions; arbitrary audit/model logs are not.
    rows('approval_refs', ('audit_event', 'parent_report_snapshot'), '''SELECT a.audit_id,a.summary_json,a.occurred_at FROM audit_event a
        JOIN parent_report_snapshot s ON s.snapshot_id=json_extract(a.summary_json,'$.snapshot_id') AND s.child_id=a.child_id
        WHERE a.child_id=? AND a.action='parent_report_snapshot_approved' ORDER BY a.rowid''')
    rows('reports', ('daily_report', 'daily_session'), '''SELECT r.report_id,r.session_id,r.report_date,r.source,r.evidence_json,
        r.summary_json,r.parent_coaching_json,r.created_at,ds.ended_at,ds.started_at FROM daily_report r
        JOIN daily_session ds ON ds.session_id=r.session_id AND ds.child_id=r.child_id
        WHERE r.child_id=? AND r.session_id!=? ORDER BY r.report_id''', (child_id, session_id))
    rows('referenced_daily', ('daily_report', 'parent_report_snapshot'), '''SELECT DISTINCT r.report_id,r.source,r.evidence_json,r.summary_json,r.parent_coaching_json
        FROM daily_report r JOIN parent_report_snapshot s ON s.source_kind='daily_report' AND s.source_id=r.report_id AND s.child_id=r.child_id
        WHERE s.child_id=? ORDER BY r.report_id''')
    rows('referenced_period', ('period_report', 'parent_report_snapshot'), '''SELECT DISTINCT r.period_report_id,r.source,r.content_json,r.period_start,r.period_end
        FROM period_report r JOIN parent_report_snapshot s ON s.source_kind='period_report' AND s.source_id=r.period_report_id AND s.child_id=r.child_id
        WHERE s.child_id=? ORDER BY r.period_report_id''')
    rows('legacy_behavior', ('intervention_record',), '''SELECT intervention_id,trigger_state,child_response,confidence,COALESCE(responded_at,created_at)
        FROM intervention_record WHERE child_id=? AND child_response IS NOT NULL ORDER BY COALESCE(responded_at,created_at) DESC,intervention_id LIMIT 3''')
    rows('habits', ('habit_experiment',), '''SELECT experiment_id,target_state,title,child_action,success_signal,duration_days,baseline_json,
        status,proposed_at,started_at,ends_at,closed_at,
        CASE WHEN status='active' AND julianday(ends_at)<=julianday('now') THEN 1 ELSE 0 END
        FROM habit_experiment WHERE child_id=? ORDER BY proposed_at DESC,experiment_id''')
    # Active/completed/stopped experiments derive from child-wide observations,
    # including coverage payloads and their referenced local-vision observations.
    habit_inputs = any(row[8] in ('active', 'completed', 'stopped') for row in payload['habits'])
    observation_scope = '' if habit_inputs else ' AND session_id=?'
    args = (child_id,) if habit_inputs else (child_id, session_id)
    rows('observations', ('observable_event',), '''SELECT event_id,session_id,occurred_at,event_type,source,confidence,
        source_refs_json,missing_sources_json,payload_json,alternatives_json FROM observable_event
        WHERE child_id=?''' + observation_scope + ' ORDER BY event_id', args)
    rows('states', ('probabilistic_state', 'observable_event'), '''SELECT ps.state_id,ps.event_id,ps.state,ps.probability,ps.occurred_at
        FROM probabilistic_state ps JOIN observable_event oe ON oe.event_id=ps.event_id
        WHERE oe.child_id=? AND oe.session_id=? AND ps.state IN ('blocked_candidate','distraction_candidate','fatigue_candidate','frequent_switching')
        ORDER BY ps.state_id''', (child_id, session_id))
    rows('behavior_retractions', ('behavior_retraction', 'observable_event'), '''SELECT br.event_id,br.correction_id,br.created_at
        FROM behavior_retraction br JOIN observable_event oe ON oe.event_id=br.event_id
        WHERE oe.child_id=?''' + ('' if habit_inputs else ' AND oe.session_id=?') + ' ORDER BY br.event_id', args)
    rows('interventions', ('intervention', 'observable_event'), '''SELECT i.intervention_id,i.source_event_id,i.status,i.feedback,i.feedback_at,oe.source
        FROM intervention i LEFT JOIN observable_event oe ON oe.event_id=i.source_event_id
        WHERE i.child_id=? AND (i.session_id=? OR oe.session_id=?) ORDER BY i.intervention_id''', (child_id, session_id, session_id))
    rows('vocabulary', ('pet_vocabulary_attempt', 'pet_vocabulary_card'), '''SELECT a.attempt_id,v.surface,v.focus_kind,a.review_mode,a.correct,
        a.interval_days,a.next_due_at,a.occurred_at FROM pet_vocabulary_attempt a JOIN pet_vocabulary_card v ON v.card_id=a.card_id
        WHERE a.session_id=? ORDER BY a.occurred_at,a.attempt_id''', (session_id,))
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return 'report_source_v1:' + hashlib.sha256(encoded.encode('utf-8')).hexdigest()
