"""Ordinary full-form edits never acknowledge changed external rules."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from home_study_agent import exam_blueprints
from home_study_agent.database import Database
from home_study_agent.family_clock import family_today
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.repository import StudyRepository
from test_goal_program import goal_db
from test_goal_roadmap import configure

CHILD = 'child-primary-4'


@pytest.mark.parametrize('edit', ['same_date_budget', 'budget_only', 'new_date', 'legacy_unbound'])
def test_changed_rules_stay_pending_after_real_plan_edit_and_restart(goal_db, monkeypatch, edit):
    service = GoalProgramService(goal_db)
    target = (family_today()+timedelta(days=120)).isoformat()
    configure(service, child=CHILD, track='pet_path', target_date=target, action='initial-target')
    before = service.roadmap(CHILD, 'pet_path')['exam_rules']['bound_snapshot']
    repo = StudyRepository(goal_db)
    sid = repo.start_session(CHILD, 'rule-plan-session')['session_id']
    with goal_db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1,homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute("UPDATE family_rule SET latest_end_time=NULL WHERE child_id=?", (CHILD,))
        if edit == 'legacy_unbound':
            row = c.execute("SELECT program_id,target_horizon_json FROM goal_program WHERE child_id=? AND track='pet_path'", (CHILD,)).fetchone()
            old = json.loads(row['target_horizon_json'])
            old.pop('rule_binding')
            c.execute('UPDATE goal_program SET target_horizon_json=? WHERE program_id=?', (json.dumps(old), row['program_id']))
        c.commit()
    workspace = repo.generate_plan(sid, 'rule-plan')
    with goal_db.connect() as c:
        original_decisions = [tuple(row) for row in c.execute('SELECT * FROM plan_constraint_decision WHERE plan_id=?', (workspace['plan']['plan_id'],))]
    changed = deepcopy(exam_blueprints.EXAM_BLUEPRINTS)
    changed['cambridge-b1-preliminary-for-schools-current-v2']['sections'][0]['item_target'] += 1
    monkeypatch.setattr(exam_blueprints, 'EXAM_BLUEPRINTS', changed)
    date_arg = None if edit=='budget_only' else ((family_today()+timedelta(days=150)).isoformat() if edit=='new_date' else target)
    kwargs = dict(child=CHILD, track='pet_path', target_date=date_arg, weekly_minutes=70, action='ordinary-form')
    first = configure(service, **kwargs)
    restarted = GoalProgramService(Database(goal_db.path))
    assert configure(restarted, **kwargs) == first
    current = restarted.roadmap(CHILD, 'pet_path')['exam_rules']
    assert current['binding_status'] == ('unbound' if edit=='legacy_unbound' else 'source_changed')
    assert current['bound_snapshot'] == (None if edit=='legacy_unbound' else before)
    assert not current['exam_readiness_claim_allowed']
    with goal_db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM plan_constraint_decision WHERE plan_id=?', (workspace['plan']['plan_id'],))] == original_decisions
        assert c.execute("SELECT COUNT(*) FROM goal_program_revision WHERE client_action_id='ordinary-form'").fetchone()[0] == 1
