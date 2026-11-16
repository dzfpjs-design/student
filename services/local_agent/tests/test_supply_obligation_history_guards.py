"""Exact inherited scope and borrowed transaction durability, using real plans."""
import json
import pytest

from test_supply_gap_resume_http import supplied, CHILD
from test_supply_obligation_cross_day import close_and_plan, resume_next_day
from home_study_agent import supply_gap_resume
from home_study_agent.supply_obligation_history import append_to_plan
from home_study_agent.task_start_binding import TaskStartBindingError


def test_inherited_scope_read_only_cas_and_rollback(supplied, monkeypatch):
    case = supplied
    sid, root, workspace, _ = close_and_plan(case, monkeypatch)
    with case['db'].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        plan = c.execute('SELECT * FROM daily_plan WHERE session_id=?', (sid,)).fetchone()
        targets = supply_gap_resume.candidates(c, sid, CHILD)
        target = next(t for t in targets if t['source_ref'] == case['published']['diagnostic_item_id'])
        unchanged = c.total_changes
        assert supply_gap_resume.candidates(c, sid, 'child-junior-3') == []
        with pytest.raises(TaskStartBindingError):
            supply_gap_resume.validate_target(c, sid, 'child-junior-3', target)
        assert c.total_changes == unchanged
        carry = c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=?', (target['obligation_id'],)).fetchone()
        # Recreate the original input from immutable roots, not a new learning fact.
        history = json.loads(carry['metadata_json'])['supply_carryover']
        entry = dict(root=dict(c.execute('''SELECT d.*,p.session_id FROM plan_constraint_decision d
            JOIN daily_plan p USING(plan_id) WHERE d.decision_id=?''', (root['decision_id'],)).fetchone()),
            latest=root, kind=history['kind'], fixed_item=None, task_origin=None)
        with pytest.raises(ValueError, match='SUPPLY_HISTORY_SCOPE_CHANGED'):
            append_to_plan(c, 'child-junior-3', sid, plan['plan_id'], [entry], now=plan['created_at'])
        c.execute('DELETE FROM plan_constraint_decision WHERE decision_id=?', (target['obligation_id'],))
        append_to_plan(c, CHILD, sid, plan['plan_id'], [entry], now=carry['created_at'])
        assert dict(c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=?', (target['obligation_id'],)).fetchone()) == dict(carry)
        c.rollback()
    with case['db'].connect() as c:
        assert dict(c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=?', (target['obligation_id'],)).fetchone()) == dict(carry)


@pytest.mark.parametrize('fault', ['withdraw', 'authorization', 'plan_version', 'next_date'])
def test_new_day_late_voice_cannot_reuse_stale_authority(supplied, monkeypatch, fault):
    from test_voice_runtime_binding import capture, interpret
    from datetime import datetime, timedelta
    case = supplied
    sid, root, workspace, attempts = close_and_plan(case, monkeypatch)
    case['post'](case['client'], f'/v1/sessions/{sid}/transition', {
        'expected_state': 'plan_review', 'target_state': 'study', 'reason': 'Accept plan',
        'client_action_id': 'guard-study'})
    quote = '昨天缺题的写作练习补好了，请继续原来的练习。'
    case['speech']['quote'] = quote
    voice = capture(case['client'], case['settings'], monkeypatch, sid, quote, key='guard-voice')
    turn = interpret(case['client'], sid, quote, 'guard-turn', voice)
    assert turn['runtime_capability'] == 'task.resume'
    if fault == 'withdraw':
        case['post'](case['client'], case['base'] + f"/{case['draft']['candidate_id']}/withdraw", {
            'expected_version': 4, 'action_id': 'guard-withdraw', 'note': 'Source withdrawn after original voice'})
    elif fault == 'authorization':
        for allowed in (False, True):
            response = case['client'].put('/v1/parent/privacy', json={'cloud_model_text_allowed': allowed})
            assert response.status_code == 200, response.text
    elif fault == 'plan_version':
        with case['db'].connect() as c:
            c.execute('UPDATE daily_plan SET version=version+1 WHERE session_id=?', (sid,))
            c.commit()
    else:
        with case['db'].connect() as c:
            date = c.execute('SELECT plan_date FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0]
        case['cross_day_clock'].current = datetime.fromisoformat(date + 'T12:00:00+08:00') + timedelta(days=1)
    response = case['client'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute", json={})
    assert response.status_code == 409, response.text
    with case['db'].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == attempts
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 0
        assert c.execute('''SELECT COUNT(*) FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            WHERE dp.session_id=? AND pi.source_type='pet_daily' AND pi.task_type='writing' ''', (sid,)).fetchone()[0] == 0
        assert dict(c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=?',
            (root['decision_id'],)).fetchone()) == root


def test_other_child_actual_next_day_plan_does_not_inherit_first_child_promise(supplied, monkeypatch):
    case = supplied
    sid, root, workspace, attempts = close_and_plan(case, monkeypatch, target_child='child-junior-3')
    assert root['decision_id'] not in json.dumps(workspace, ensure_ascii=False)
    with case['db'].connect() as c:
        assert c.execute('SELECT child_id FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0] == 'child-junior-3'
        assert c.execute('''SELECT COUNT(*) FROM plan_constraint_decision d JOIN daily_plan p USING(plan_id)
            WHERE p.session_id=? AND json_type(d.metadata_json,'$.supply_carryover')='object' ''', (sid,)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == attempts
        assert dict(c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=?',
            (root['decision_id'],)).fetchone()) == root


def test_actual_generate_carry_failure_rolls_back_and_same_key_can_recover(supplied, monkeypatch):
    import sqlite3
    case = supplied
    def fail_on_carry(case, sid):
        with case['db'].connect() as c:
            from home_study_agent.supply_obligation_history import pending_history
            c.execute('BEGIN IMMEDIATE')
            case['carry_root_ids'] = {entry['root']['decision_id'] for entry in pending_history(c, CHILD, sid)}
            c.execute('''CREATE TRIGGER fail_supply_carry BEFORE INSERT ON plan_constraint_decision
                WHEN json_type(NEW.metadata_json,'$.supply_carryover')='object'
                BEGIN SELECT RAISE(ABORT,'supply_carry_failed'); END''')
            c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='supply_carry_failed'):
        close_and_plan(case, monkeypatch, before_generate=fail_on_carry)
    sid = case['next_sid']
    with case['db'].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 0
        assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'homework_capture'
        c.execute('DROP TRIGGER fail_supply_carry')
        c.commit()
    result = case['post'](case['client'], f'/v1/sessions/{sid}/plan/generate', {'client_action_id': 'cross-day-next-plan'})
    assert result['supply_obligations']
    with case['db'].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0] == 1
        roots = [r[0] for r in c.execute('''SELECT json_extract(d.metadata_json,'$.supply_carryover.root_decision_id')
            FROM plan_constraint_decision d JOIN daily_plan p USING(plan_id)
            WHERE p.session_id=? AND json_type(d.metadata_json,'$.supply_carryover')='object' ''', (sid,))]
        assert set(roots) == case['carry_root_ids'] and len(roots) == len(set(roots))
