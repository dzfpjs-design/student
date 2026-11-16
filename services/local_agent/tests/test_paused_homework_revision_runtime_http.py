"""Paused assignment revision enters through the actual semantic runtime/HTTP lane."""
import json
from datetime import timedelta
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelResult
from test_agent_continuation import Gateway
from test_learning_input_replan import schedule, inputs, flow, CHILD
from test_daily_homework_revision_completion_http import post

WORDS = '口算范围改成第十一页'
CONTROL = 'learning_inputs.propose_revision'
REVISION = {'kind': 'homework', 'operation': 'update', 'target_quote': '口算',
            'changes': {'range_text': '第十一页'}, 'change_quotes': {'range_text': '第十一页'}}


class SemanticGateway(Gateway):
    def __init__(self, role='control', confidence=.99, revision=None):
        super().__init__({})
        self.role, self.confidence = role, confidence
        self.revision = REVISION if revision is None else revision

    async def complete(self, request):
        self.requests.append(request)
        assert request.request_type == 'voice_interaction', 'No broader planner fallback'
        payload = json.loads(request.messages[-1]['content'])
        if 'pending_input_field' in payload:
            assert set(payload) == {'transcript', 'pending_input_field'}, 'No historical content in field classification'
            field, words = payload['pending_input_field'], payload['transcript']
            result = {'field': field, 'meaning': 'unclear', 'quote': words,
                      'literal_quote': None, 'minutes': None, 'confidence': self.confidence}
            if field == 'ownership':
                if words in {'这是我的作业', '这是我要改的作业', '对，是老师给我们班布置的，我也得写', '这就是我今晚要交的'}:
                    result['meaning'] = 'own'
                elif words in {'不是我的作业', '不是我的，是同桌的'}:
                    result['meaning'] = 'not_mine'
            elif words in {'大概十分钟', '我看大概得花十分钟左右吧'}:
                result.update(meaning='estimate', literal_quote='大概十分钟' if words == '大概十分钟' else '十分钟', minutes=10)
            return ModelResult(content=json.dumps(result, ensure_ascii=False), provider='test', model='test', latency_ms=1)
        if '延期' in payload['transcript']:
            available = 'task.reschedule' in request.messages[0]['content']
            role = self.role if available else 'unknown'
            result = {'role': role,
                      'control': 'task.reschedule' if role == 'control' else None,
                      'quote': payload['transcript'], 'confidence': self.confidence}
            return ModelResult(content=json.dumps(result), provider='test', model='test', latency_ms=1)
        advertised = CONTROL in request.messages[0]['content']
        role = self.role if advertised else 'unknown'
        result = {'role': role, 'control': CONTROL if role == 'control' else None,
                  'quote': payload['transcript'], 'confidence': self.confidence}
        if role == 'control':
            result['revision'] = self.revision
        return ModelResult(content=json.dumps(result, ensure_ascii=False), provider='test', model='test', latency_ms=1)


def say(client, sid, words, key):
    turn = post(client, '/v1/sessions/' + sid + '/agent-turn', key, transcript=words)
    response = client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
    assert response.status_code == 200, response.text
    return turn, response.json()


def process_http(db, data_dir, sid, now, operations, *, revision=None):
    import home_study_agent
    result = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('daily_revision_process.py'))],
        input=json.dumps({'database_path': str(db.path), 'data_dir': str(data_dir), 'session_id': sid,
                          'source_dir': str(Path(home_study_agent.__file__).parents[1]),
                          'now': now.isoformat(), 'operations': operations, 'revision': revision}), text=True, capture_output=True, timeout=40)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-3000:]
    record = next(line for line in result.stdout.splitlines() if line.startswith('HSC_PROCESS_RESULT '))
    return json.loads(record.split(' ', 1)[1])


def test_paused_spoken_revision_proposes_then_confirms_without_resuming_old_scope(schedule, tmp_path):
    (db, sid, _), _, _ = schedule
    gateway = SemanticGateway()
    settings = Settings(data_dir=tmp_path / 'app', database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        post(client, '/v1/plan-items/old/start', 'old-start')
        post(client, '/v1/plan-items/old/pause', 'old-pause')
        turn, proposal = say(client, sid, WORDS, 'revision-proposal')
        assert proposal['execution']['result'].get('status') == 'awaiting_confirmation', proposal
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='old'").fetchone()[0] == 'paused'
            assert c.execute("SELECT range_text FROM homework_item WHERE homework_id='math-a'").fetchone()[0] is None
            assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0
        _, confirmed = say(client, sid, '确认，就这样改', 'revision-confirm')
        assert confirmed['execution']['result']['replan']['unplaced'][0]['reason'] == 'executed_scope_requires_review'
        post(client, '/v1/plan-items/old/complete', 'old-history-complete', actual_minutes=1)
        with db.connect() as c:
            homework = c.execute("SELECT range_text,status FROM homework_item WHERE homework_id='math-a'").fetchone()
            assert tuple(homework) == ('第十一页', 'pending')
        summary = client.get('/v1/sessions/' + sid + '/learning-summary').json()
        assert next(row for row in summary['homework'] if row['source']['source_id'] == 'math-a')['status'] == 'unfinished'
        assert len(gateway.requests) == 1


@pytest.mark.parametrize('role,confidence', [('unknown', .99), ('mixed', .99), ('control', .4)])
def test_unknown_mixed_or_uncertain_revision_never_falls_back_to_planner(schedule, tmp_path, role, confidence):
    (db, sid, _), _, _ = schedule
    gateway = SemanticGateway(role, confidence)
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=gateway)) as client:
        post(client, '/v1/plan-items/old/start', 'old-start')
        post(client, '/v1/plan-items/old/pause', 'old-pause')
        say(client, sid, WORDS, 'uncertain-revision')
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM learning_input_proposal').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0
        assert len(gateway.requests) == 1


def test_confirmed_new_scope_remains_visible_and_can_be_explicitly_replanned(schedule, tmp_path, monkeypatch):
    (db, sid, _), _, clock = schedule
    from home_study_agent import repository
    monkeypatch.setattr(repository, 'utc_now', lambda: clock[0].isoformat())
    with db.connect() as c:
        c.execute('UPDATE daily_session SET started_at=? WHERE session_id=?', (clock[0].isoformat(), sid))
        before_evidence = [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
        c.commit()
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway())) as client:
        post(client, '/v1/plan-items/old/start', 'old-start')
        clock[0] += timedelta(minutes=3)
        post(client, '/v1/plan-items/old/pause', 'old-pause')
        say(client, sid, WORDS, 'revision-proposal')
        _, confirmed = say(client, sid, '确认，就这样改', 'revision-confirm')
        oid = confirmed['execution']['result']['replan']['unplaced'][0]['obligation_id']
        post(client, '/v1/plan-items/old/complete', 'old-history-complete', actual_minutes=3)
        workspace = client.get('/v1/sessions/' + sid).json()
        source = next(row for row in workspace['learning_input_unplaced']['items'] if row['obligation_id'] == oid)
        assert source['scope_review_required'], 'Zero computed remainder must not hide changed, unreviewed content'
        say(client, sid, '没排完的作业有哪些', 'list-remaining')
        say(client, sid, '第一项', 'select-remaining')
        say(client, sid, '还剩第十一页没做', 'scope-content')
        say(client, sid, '还要十分钟', 'scope-estimate')
        _, scope = say(client, sid, '确认', 'scope-confirm')
        assert scope['execution']['result']['status'] == 'scope_confirmed', scope
        _, allocated = say(client, sid, '今天继续', 'scope-allocate')
        result = allocated['execution']['result']
        assert result['allocated_minutes'] == 10 and result['remaining_minutes'] == 0, result
        assert result['target_homework_id'] != 'math-a'
        with db.connect() as c:
            assert c.execute("SELECT status FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == 'pending'
            assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == before_evidence


def test_proposal_confirmation_scope_and_original_keys_survive_distinct_processes(schedule, tmp_path, monkeypatch):
    (db, sid, _), _, clock = schedule
    from home_study_agent import repository
    monkeypatch.setattr(repository, 'utc_now', lambda: clock[0].isoformat())
    with db.connect() as c:
        c.execute('UPDATE daily_session SET started_at=? WHERE session_id=?', (clock[0].isoformat(), sid))
        c.commit()
    first = process_http(db, tmp_path / 'process-data', sid, clock[0], [
        {'kind': 'http', 'method': 'POST', 'path': '/v1/plan-items/old/start', 'json': {'client_action_id': 'process-original-start'}},
        {'kind': 'http', 'method': 'POST', 'path': '/v1/plan-items/old/pause', 'json': {'client_action_id': 'process-original-pause'}},
        {'kind': 'say', 'text': WORDS, 'key': 'process-proposal'}])
    assert first['results'][-1]['result']['execution']['result']['status'] == 'awaiting_confirmation'
    second = process_http(db, tmp_path / 'process-data', sid, clock[0], [
        {'kind': 'http', 'path': '/v1/sessions/' + sid + '/agent-request-receipts/daily-proof-process-proposal'},
        {'kind': 'say', 'text': '确认，就这样改', 'key': 'process-confirm'},
        {'kind': 'say', 'text': '这项延期', 'key': 'process-old-reschedule'},
        {'kind': 'say', 'text': '没排完的作业有哪些', 'key': 'process-list'},
        {'kind': 'say', 'text': '第一项', 'key': 'process-select'},
        {'kind': 'say', 'text': '还剩第十一页没做', 'key': 'process-scope'},
        {'kind': 'say', 'text': '还要十分钟', 'key': 'process-estimate'}])
    third = process_http(db, tmp_path / 'process-data', sid, clock[0], [
        {'kind': 'say', 'text': '确认', 'key': 'process-scope-confirm'},
        {'kind': 'say', 'text': '今天继续', 'key': 'process-allocate'}])
    assert third['results'][0]['result']['execution']['result']['status'] == 'scope_confirmed'
    allocation = third['results'][1]['result']['execution']['result']
    assert allocation['allocated_minutes'] == 10
    with db.connect() as c:
        before = [tuple(row) for row in c.execute('SELECT * FROM learning_input_carryover_event ORDER BY rowid')]
        items = [tuple(row) for row in c.execute('SELECT * FROM plan_item ORDER BY rowid')]
    fourth = process_http(db, tmp_path / 'process-data', sid, clock[0], [
        {'kind': 'http', 'path': '/v1/sessions/' + sid + '/agent-request-receipts/daily-proof-process-allocate'},
        {'kind': 'say', 'text': '今天继续', 'key': 'process-allocate'},
        {'kind': 'say', 'text': '这项延期', 'key': 'process-old-reschedule'}])
    assert fourth['results'][1]['result']['execution']['result'] == allocation
    assert len({record['pid'] for record in (first, second, third, fourth)}) == 4
    assert first['model_requests'] == 1
    assert second['model_requests'] == 1
    assert third['model_requests'] == fourth['model_requests'] == 0
    with db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM learning_input_carryover_event ORDER BY rowid')] == before
        assert [tuple(row) for row in c.execute('SELECT * FROM plan_item ORDER BY rowid')] == items
        assert c.execute("SELECT status FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == 'pending'


@pytest.mark.parametrize('words,revision', [
    ('不要把口算范围改成第十一页', REVISION),
    ('如果口算范围改成第十一页呢', REVISION),
    (WORDS, {**REVISION, 'changes': {'range_text': '第十二页'}}),
    (WORDS, {**REVISION, 'target_quote': '另一项作业'}),
    (WORDS, {**REVISION, 'target_id': 'math-b'}),
])
def test_revision_semantics_cannot_invent_values_ids_or_ignore_negation(schedule, tmp_path, words, revision):
    (db, sid, _), _, _ = schedule
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway(revision=revision))) as client:
        post(client, '/v1/plan-items/old/start', 'old-start')
        post(client, '/v1/plan-items/old/pause', 'old-pause')
        say(client, sid, words, 'invalid-revision')
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM learning_input_proposal').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0


def test_revision_dispatch_rechecks_exact_semantic_arguments(schedule, tmp_path):
    (db, sid, _), _, _ = schedule
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway())) as client:
        post(client, '/v1/plan-items/old/start', 'old-start')
        post(client, '/v1/plan-items/old/pause', 'old-pause')
        turn = post(client, '/v1/sessions/' + sid + '/agent-turn', 'tampered-proposal', transcript=WORDS)
        with db.connect() as c:
            c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.changes.range_text','第十二页') WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn['agent_turn_id'],))
            c.commit()
        response = client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
        assert response.status_code == 409, response.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM learning_input_proposal').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['paused', 'after_readback', 'after_scope_confirmed'])
def test_scope_confirmation_never_reuses_changed_or_unsettled_old_execution(schedule, tmp_path, monkeypatch, change):
    (db, sid, _), _, clock = schedule
    from home_study_agent import repository
    monkeypatch.setattr(repository, 'utc_now', lambda: clock[0].isoformat())
    with db.connect() as c:
        c.execute('UPDATE daily_session SET started_at=? WHERE session_id=?', (clock[0].isoformat(), sid))
        c.commit()
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway())) as client:
        post(client, '/v1/plan-items/old/start', 'old-start')
        post(client, '/v1/plan-items/old/pause', 'old-pause')
        say(client, sid, WORDS, 'revision-proposal')
        say(client, sid, '确认，就这样改', 'revision-confirm')
        if change != 'paused':
            post(client, '/v1/plan-items/old/complete', 'old-history-complete', actual_minutes=1)
        say(client, sid, '没排完的作业有哪些', 'list-remaining')
        say(client, sid, '第一项', 'select-remaining')
        say(client, sid, '还剩第十一页没做', 'scope-content')
        say(client, sid, '还要十分钟', 'scope-estimate')
        if change == 'after_scope_confirmed':
            say(client, sid, '确认', 'scope-confirm')
        if change != 'paused':
            with db.connect() as c:
                c.execute("UPDATE plan_item SET actual_minutes=2 WHERE plan_item_id='old'")
                c.commit()
        turn = post(client, '/v1/sessions/' + sid + '/agent-turn', 'cannot-consume',
                    transcript='今天继续' if change == 'after_scope_confirmed' else '确认')
        if turn.get('runtime_capability'):
            result = client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
            assert result.status_code in (200, 409), result.text
        else:
            assert turn['action']['type'] in {'none', 'clarify'}, turn
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM learning_input_carryover_event WHERE operation='confirm_scope'").fetchone()[0] == int(change == 'after_scope_confirmed')
            assert c.execute("SELECT COUNT(*) FROM learning_input_carryover_event WHERE operation IN ('continue_today','allocate')").fetchone()[0] == 0
            assert c.execute("SELECT status FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == 'pending'


@pytest.mark.parametrize('case', ['changed_target', 'two_paused', 'negation', 'unknown', 'other_subject'])
def test_semantic_deferral_is_bound_to_one_paused_task_and_never_retargets(schedule, tmp_path, case):
    (db, sid, _), _, _ = schedule
    gateway = SemanticGateway(role='unknown' if case == 'unknown' else 'control')
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=gateway)) as client:
        post(client, '/v1/plan-items/old/start', 'old-start')
        post(client, '/v1/plan-items/old/pause', 'old-pause')
        if case == 'two_paused':
            with db.connect() as c:
                c.execute("UPDATE plan_item SET status='paused' WHERE plan_item_id='protected'")
                c.commit()
        words = '不要把这项延期' if case == 'negation' else '语文延期' if case == 'other_subject' else '这项延期'
        turn = post(client, '/v1/sessions/' + sid + '/agent-turn', 'deferral', transcript=words)
        if case == 'changed_target':
            post(client, '/v1/plan-items/old/complete', 'concurrent-original-complete', actual_minutes=1)
        if turn.get('runtime_capability'):
            result = client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
            assert result.status_code in (200, 409), result.text
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='rescheduled'").fetchone()[0] == 0
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='protected'").fetchone()[0] == ('paused' if case == 'two_paused' else 'pending')
