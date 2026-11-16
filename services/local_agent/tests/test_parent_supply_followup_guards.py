from datetime import timedelta, timezone

from home_study_agent.parent_supply_followup import current_followups, _task
from home_study_agent.repository import StudyRepository
from test_pet_task_execution import generated, CHILD


def full_schema(db):
    from home_study_agent.curriculum_supply import CurriculumSupplyService
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    CurriculumSupplyService(db).ensure_schema()
    DiagnosticPromptService(db)


def test_missing_listening_asset_is_not_parent_ready(tmp_path):
    db, sid, pid, _, original = generated(tmp_path)
    full_schema(db)
    with db.connect() as c:
        c.execute('BEGIN')
        rows = current_followups(c, CHILD)
        row = next(r for r in rows if r['plan_item_id'] == pid)
        assert row['status'] == 'awaiting_resources'
        assert '原音' in row['next_action']
        assert c.execute('SELECT started_at FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] is None


def test_changed_original_version_cannot_be_ready_just_because_item_is_active(tmp_path):
    db, sid, pid, _, original = generated(tmp_path, skill='reading')
    full_schema(db)
    with db.connect() as c:
        c.execute('BEGIN')
        initial = next(r for r in current_followups(c, CHILD) if r['plan_item_id'] == pid)
        assert initial['status'] in {'scheduled', 'waiting_constraints'}
        c.execute("UPDATE diagnostic_item SET prompt=prompt||' Revised source text.' WHERE diagnostic_item_id=?", (original['diagnostic_item_id'],))
        changed = next(r for r in current_followups(c, CHILD) if r['plan_item_id'] == pid)
        assert changed['status'] == 'source_changed'
        c.rollback()
        c.execute('BEGIN')
        assert next(r for r in current_followups(c, CHILD) if r['plan_item_id'] == pid) == initial


def test_historical_lifecycle_not_overwritten_by_later_bookkeeping_and_today_expires(tmp_path, monkeypatch):
    db, sid, pid, _, original = generated(tmp_path, skill='reading')
    full_schema(db)
    from home_study_agent import parent_supply_followup as projection
    now = projection.datetime.now(timezone.utc)
    with db.connect() as c:
        c.execute('BEGIN')
        decision = dict(c.execute('''SELECT d.*,p.child_id FROM plan_constraint_decision d
            JOIN daily_plan p USING(plan_id) WHERE json_extract(d.metadata_json,'$.plan_item_id')=?''', (pid,)).fetchone())
        # A real reschedule event and a bookkeeping marker represent different facts.
        for kind, payload in [('rescheduled', {'reason': '家长确认今天时间不足'}), ('revision_replanned', {})]:
            StudyRepository._insert_plan_event(c, pid, kind, now.isoformat(), payload, 'history-'+kind)
        task, reason = _task(c, decision, now.isoformat())
        assert task['status'] == 'rescheduled' and reason == '家长确认今天时间不足'
        class Tomorrow(projection.datetime):
            @classmethod
            def now(cls, tz=None):
                value = now + timedelta(days=1)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        monkeypatch.setattr(projection, 'datetime', Tomorrow)
        row = next(r for r in current_followups(c, CHILD) if r['plan_item_id'] == pid)
        assert row['status'] == 'needs_plan' and row['current_session_id'] is None
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
