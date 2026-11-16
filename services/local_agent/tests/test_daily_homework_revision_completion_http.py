"""A child's revised assignment must not stay pending or finish on old-scope work."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.repository import StudyRepository
from test_agent_continuation import Gateway
from test_learning_input_replan import schedule, inputs, flow, CHILD


def revise(client, sid, words, changes, quotes):
    turn = client.post('/v1/sessions/' + sid + '/agent-turn', json={
        'transcript': words, 'client_action_id': 'real-homework-revision'})
    assert turn.status_code == 200, turn.text
    proposed = client.post('/v1/agent-turns/' + turn.json()['agent_turn_id'] + '/execute')
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()['execution']['result'].get('status') == 'awaiting_confirmation', {
        'result': proposed.json()['execution']['result'], 'said': proposed.json()['turn']['response_text']}
    turn = client.post('/v1/sessions/' + sid + '/agent-turn', json={
        'transcript': '确认，就这样改', 'client_action_id': 'real-homework-confirm'})
    assert turn.status_code == 200, turn.text
    result = client.post('/v1/agent-turns/' + turn.json()['agent_turn_id'] + '/execute')
    assert result.status_code == 200, result.text
    assert 'replan' in result.json()['execution']['result'], result.json()['execution']['result']
    return result.json()['execution']['result']['replan']


def gateway_for(words, changes, quotes):
    return Gateway({'objective': {'mode': 'none'}, 'utterance_binding': {'role': 'correction', 'quote': words},
        'next_step': {'capability': 'learning_inputs.propose_revision', 'arguments': {
            'kind': 'homework', 'operation': 'update', 'target_quote': '口算', 'request_quote': words,
            'changes': changes, 'change_quotes': quotes}}, 'spoken_transition': '我先核对这项作业。',
        'requires_child_reply': False, 'confidence': .99})


def post(client, path, key, **payload):
    result = client.post(path, json={'client_action_id': 'daily-proof-' + key, **payload})
    assert result.status_code == 200, result.text
    return result.json()


def sibling_snapshot(connection):
    return {
        table: [tuple(row) for row in connection.execute(query)]
        for table, query in {
            'session': "SELECT * FROM daily_session WHERE session_id='sibling-session'",
            'homework': "SELECT * FROM homework_item WHERE homework_id='sibling-math'",
            'plan': "SELECT * FROM daily_plan WHERE plan_id='sibling-plan'",
            'item': "SELECT * FROM plan_item WHERE plan_item_id='sibling-item'",
        }.items()
    }


@pytest.mark.parametrize('capacity', [120, 30])
def test_all_replacement_segments_complete_current_homework_despite_cancelled_history(schedule, tmp_path, monkeypatch, capacity):
    (db, sid, _), _, clock = schedule
    from home_study_agent import repository
    monkeypatch.setattr(repository, 'utc_now', lambda: clock[0].isoformat())
    with db.connect() as c:
        c.execute('UPDATE family_rule SET max_learning_minutes=? WHERE child_id=?', (capacity, CHILD))
        journal = [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
        other = [tuple(row) for row in c.execute('SELECT * FROM child_profile WHERE child_id!=?', (CHILD,))]
        sibling = c.execute('SELECT child_id FROM child_profile WHERE child_id!=? LIMIT 1', (CHILD,)).fetchone()[0]
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,
            ended_at,last_checkpoint_at,client_action_id) VALUES ('sibling-session',?,'completed',
            'reflection',?, ?,?,'sibling-session')""", (sibling, *([clock[0].isoformat()] * 3)))
        c.execute("""INSERT INTO homework_item(homework_id,child_id,session_id,subject,task_type,title,
            expected_minutes,confirmation_status,status,created_at,client_action_id)
            VALUES ('sibling-math',?,'sibling-session','数学','written','口算',20,
            'child_confirmed','pending',?,'sibling-homework')""", (sibling, clock[0].isoformat()))
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,
            generator,version,created_at,client_action_id) VALUES ('sibling-plan',?,'sibling-session',
            '2026-09-07','active',20,'test',1,?,'sibling-plan')""", (sibling, clock[0].isoformat()))
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,
            priority,status,position,source_type,source_ref) VALUES ('sibling-item','sibling-plan',
            'sibling-math','数学','口算',20,1,'pending',1,'school_homework','sibling-math')""")
        sibling_before = sibling_snapshot(c)
        c.commit()
    words, changes, quotes = '口算改成30分钟', {'expected_minutes': 30}, {'expected_minutes': '30分钟'}
    settings = Settings(data_dir=tmp_path / 'app', database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=gateway_for(words, changes, quotes))) as client:
        result = revise(client, sid, words, changes, quotes)
        allocated = result['receipt']['allocated_minutes']
        assert allocated == (30 if capacity == 120 else 15)
        assert sum(row['minutes'] for row in result['unplaced']) == 30 - allocated
        for i, item in enumerate(result['newitems']):
            if item['source_type'] == 'planned_break':
                post(client, '/v1/sessions/' + sid + '/break', 'break-' + str(i))
                clock[0] += timedelta(minutes=item['expected_minutes'])
                post(client, '/v1/sessions/' + sid + '/resume', 'break-end-' + str(i))
            else:
                path = '/v1/plan-items/' + item['plan_item_id']
                post(client, path + '/start', 'start-' + str(i))
                clock[0] += timedelta(minutes=1)
                post(client, path + '/pause', 'pause-' + str(i))
                post(client, path + '/resume', 'resume-' + str(i))
                clock[0] += timedelta(minutes=item['expected_minutes'] - 1)
                post(client, path + '/complete', 'complete-' + str(i), actual_minutes=item['expected_minutes'])
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='old'").fetchone()[0] == 'cancelled'
            assert c.execute("SELECT status FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == ('completed' if capacity == 120 else 'pending')
        summary = client.get('/v1/sessions/' + sid + '/learning-summary')
        assert summary.status_code == 200, summary.text
        assert not any(row['source']['source_id'] == 'sibling-math' for row in summary.json()['homework'])
        assert next(row for row in summary.json()['homework'] if row['source']['source_id'] == 'math-a')['status'] == ('completed' if capacity == 120 else 'unfinished')
        with db.connect() as c:
            event_count = c.execute('SELECT COUNT(*) FROM plan_item_event').fetchone()[0]
    with TestClient(create_app(settings, model_gateway=gateway_for(words, changes, quotes))) as reopened:
        # Lost completion response: replay the original key, never finish a sibling.
        post(reopened, path + '/complete', 'complete-' + str(i), actual_minutes=item['expected_minutes'])
        persisted = reopened.get('/v1/sessions/' + sid + '/learning-summary').json()
        assert persisted['homework'] == summary.json()['homework']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM plan_item_event').fetchone()[0] == event_count
            assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == journal
            assert [tuple(row) for row in c.execute('SELECT * FROM child_profile WHERE child_id!=?', (CHILD,))] == other
            assert sibling_snapshot(c) == sibling_before
        sibling_summary = reopened.get('/v1/sessions/sibling-session/learning-summary')
        assert sibling_summary.status_code == 200, sibling_summary.text
        assert [(row['source']['source_id'], row['status']) for row in sibling_summary.json()['homework']] == [('sibling-math', 'unfinished')]
        post(reopened, '/v1/plan-items/protected/reschedule', 'keep-protected-task', reason='Keep the other unfinished task')
        post(reopened, '/v1/sessions/' + sid + '/transition', 'finish-day',
            expected_state='study', target_state='reflection', reason='Child explicitly ends today')
        closed = post(reopened, '/v1/sessions/' + sid + '/reflection/complete', 'day-reflection',
            learned_text='', difficult_text='', feeling='okay', next_action='', skipped=False)
        assert closed['session']['status'] == 'completed'
        end = reopened.get('/v1/sessions/' + sid + '/learning-summary').json()
        assert end['homework'] == persisted['homework']
        clock[0] += timedelta(days=1)
        next_sid = StudyRepository(db).start_session(CHILD, 'following-day')['session_id']
        context = reopened.put('/v1/sessions/' + next_sid + '/planning-context', json={
            'energy_level': 'medium', 'fixed_commitment_minutes': 0, 'tomorrow_event_minutes': 0,
            'client_action_id': 'following-day-context'})
        assert context.status_code == 200, context.text
        post(reopened, '/v1/sessions/' + next_sid + '/transition', 'next-recall',
            expected_state='check_in', target_state='classroom_recall', reason='Next-day intake')
        post(reopened, '/v1/sessions/' + next_sid + '/recalls/none', 'no-next-recall')
        post(reopened, '/v1/sessions/' + next_sid + '/transition', 'next-homework',
            expected_state='classroom_recall', target_state='homework_capture', reason='Review next-day obligations')
        following = reopened.get('/v1/sessions/' + next_sid).json()
        assert not any(row['source_plan_item_id'] == 'old' for row in following['carryover_candidates'])
        assert not any(row['source_plan_item_id'] == 'sibling-item' for row in following['carryover_candidates'])
        if capacity == 30:
            assert following['learning_input_unplaced']['status'] == 'ready', following['learning_input_unplaced']
            obligation = next(row for row in following['learning_input_unplaced']['items']
                if row['obligation_id'] == result['unplaced'][0]['obligation_id'])
            assert obligation['source_current'] and obligation['remaining_minutes'] == 15
            assert 'continue_today' in obligation['allowed_actions']
        with db.connect() as c:
            assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == journal
            assert sibling_snapshot(c) == sibling_before


def test_old_paused_scope_completion_does_not_complete_revised_assignment(schedule, tmp_path, monkeypatch):
    (db, sid, _), _, clock = schedule
    from home_study_agent import repository
    monkeypatch.setattr(repository, 'utc_now', lambda: clock[0].isoformat())
    words, changes, quotes = '口算范围改成第十一页', {'range_text': '第十一页'}, {'range_text': '第十一页'}
    settings = Settings(data_dir=tmp_path / 'app', database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=gateway_for(words, changes, quotes))) as client:
        post(client, '/v1/plan-items/old/start', 'original-start')
        clock[0] += timedelta(minutes=3)
        post(client, '/v1/plan-items/old/pause', 'original-pause')
        # Exercise completion independently of paused voice-control admission.
        # These helpers persist and validate the real revision authority/confirmation.
        from test_learning_input_revision_voice import propose, confirm
        propose(schedule[0], utterance=words, changes=changes, change_quotes=quotes)
        event = confirm(schedule[0])[0]['event_id']
        result = schedule[1].execute_replan(CHILD, event, 'paused-scope-replan')
        assert result['unplaced'][0]['reason'] == 'executed_scope_requires_review'
        post(client, '/v1/plan-items/old/complete', 'original-scope-complete', actual_minutes=3)
        with db.connect() as c:
            assignment = dict(c.execute("SELECT * FROM homework_item WHERE homework_id='math-a'").fetchone())
            assert assignment['range_text'] == '第十一页'
            assert assignment['status'] == 'pending', 'Old execution must not claim the newly revised scope was done'
        summary = client.get('/v1/sessions/' + sid + '/learning-summary')
        assert summary.status_code == 200, summary.text
        assert next(row for row in summary.json()['homework'] if row['source']['source_id'] == 'math-a')['status'] == 'unfinished'
