"""Real local HTTP/DB; declared planner and bridge receipts, no native execution."""
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelResult
from test_app import advance_to_study, client_for
from test_controlled_failed_launch_cancel_http import capture, cancel_turn
from test_controlled_cloud_semantics_http import CHILD, Semantic, provider
from test_learning_runtime_http import NoCloud


class Planner:
    configured = True

    def __init__(self, kind, query='图形', mode='new', objective_type='execution'):
        self.kind, self.query, self.mode, self.objective_type = kind, query, mode, objective_type
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        assert request.request_type == 'agent_plan'
        packet = json.loads(request.messages[-1]['content'])
        value = {'utterance_binding': {'role': 'task_request', 'quote': packet['child_utterance']},
            'objective': {'mode': self.mode, 'type': self.objective_type, 'summary': packet['child_utterance'],
                'success_criteria': ['核对许可并打开本次请求的受控资源'], 'phase': 'execution'},
            'next_step': {'capability': 'controlled_content.play' if self.kind == 'content' else 'controlled_game.play',
                'arguments': {'query': self.query}, 'reason': '本轮明确请求', 'expected_evidence': ['native_open_receipt']},
            'spoken_transition': '我先核对许可，再打开这项内容。', 'requires_child_reply': False,
            'after_execution': 'stop', 'continuation_scope': [], 'confidence': .99}
        return ModelResult(content=json.dumps(value, ensure_ascii=False), provider='declared-synthetic',
                           model='no-model-network', latency_ms=1)


def setup(client, db, folder, kind):
    sid, _ = advance_to_study(client, CHILD, 'objective-closure')
    token = client.post('/v1/parent/setup', json={'pin': '4826'}).json()['parent_token']
    parent = {'X-HSC-Parent': token}
    assert client.put('/v1/parent/privacy', headers=parent, json={'cloud_model_text_allowed': True}).status_code == 200
    base = f'/v1/parent/children/{CHILD}/controlled-{kind}'
    assert client.put(base+'/rule', headers=parent, json={
        'enabled': True, 'max_daily_minutes': 20, 'max_item_minutes': 8}).status_code == 200
    if kind == 'content':
        (folder/'video.mp4').write_bytes(b'synthetic input, HTTP does not prove decoding')
        with db.connect() as c:
            c.execute('''INSERT INTO curriculum_corpus(corpus_id,title,corpus_type,source_database_path,
                transcript_root_path,video_root_path,source_ref,license_status,review_status,status,
                resource_count,transcript_count,local_video_count,imported_at,updated_at)
                VALUES('closure','Synthetic','seyk',?,?,?,'synthetic','public_personal_use',
                'approved_baseline','ready',2,2,2,datetime('now'),datetime('now'))''',
                (str(folder/'source.db'), str(folder), str(folder)))
            ids = [c.execute('''INSERT INTO curriculum_resource(corpus_id,external_resource_id,title,description,
                stage_title,grade_title,subject_title,semester,unit_path,transcript_text,video_relative_path,
                video_duration,local_video_available,import_job_id)
                VALUES('closure',?,?,'Synthetic','初中','初三','数学','上册','Synthetic','Private fixture text',
                'video.mp4',60,1,'synthetic')''', (str(i), f'图形{i}')).lastrowid for i in (1, 2)]
            c.commit()
        bodies = [{'curriculum_row_id': i, 'max_minutes': 6} for i in ids]
    else:
        bodies = []
        for i in (1, 2):
            path = folder/f'inert{i}.exe'
            path.write_bytes(b'never launched')
            bodies.append({'title': f'图形{i}', 'executable_path': str(path), 'max_minutes': 6})
    for body in bodies:
        r = client.post(base+'/approvals', headers=parent, json=body)
        assert r.status_code == 200, r.text
    return sid, f'/v1/sessions/{sid}/controlled-{kind}'


def start(client, db, sid, kind, path, mode):
    query = '图形' if mode == 'cancel' else '图形1'
    gateway = Planner(kind, query)
    provider(client, gateway)
    quote = '现在我想看看图形视频' if kind == 'content' else '现在我想玩玩图形游戏'
    r = client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': quote, 'client_action_id': str(uuid4()), 'cloud_text_allowed': True})
    assert r.status_code == 200, r.text
    turn = r.json()
    tid = turn['agent_turn_id']
    with db.connect() as c:
        p = c.execute('SELECT objective_id,plan_json FROM agent_plan WHERE agent_turn_id=?', (tid,)).fetchone()
        assert json.loads(p['plan_json'])['objective']['mode'] == 'new', turn
        oid = p['objective_id']
    launched = client.post(path+'/start', json={'query': query, 'client_action_id': tid})
    assert launched.status_code == (409 if mode == 'cancel' else 200), launched.text
    if mode == 'cancel':
        outcome = {'tool_status': 'failed', 'tool_error_code': 'CONTROLLED_START_NATIVE_UNCONFIRMED',
                   'final_response': '原启动是否执行仍待核对。'}
    else:
        identity = 'content_session_id' if kind == 'content' else 'game_session_id'
        outcome = body(turn, {identity: launched.json()[identity], 'status': 'active',
                             'launch_performed': True, 'replayed': False})
    saved = client.put(f'/v1/agent-turns/{tid}/outcome', json=outcome)
    assert saved.status_code == 200, saved.text
    return tid, oid, launched.json()


def body(turn, output):
    receipt = {'capability': turn['runtime_capability'], 'execution_location': 'client_device_bridge',
        'action_type': turn['action']['type'], 'status': 'completed',
        'completed_at': datetime.now(timezone.utc).isoformat(), 'output': output}
    return {'tool_status': 'completed', 'final_response': '原活动结束已核对。',
            'result': {'execution_receipt': receipt, 'client_bridge_receipt': receipt}}


def ending(client, folder, monkeypatch, sid, kind, path, source, launched, mode, persist=True):
    voice = capture(client, folder, monkeypatch, sid, 'stop-asr', '停止原活动')
    turn = cancel_turn(client, sid, voice, kind)
    tid = turn['agent_turn_id']
    admitted = client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/control-admission')
    assert admitted.status_code == 200, admitted.text
    if mode == 'cancel':
        intent = str(uuid4())
        if persist:
            r = client.post(f'/v1/sessions/{sid}/controlled-launch-intents/{intent}/cancel-unstarted',
                json={'kind': 'video' if kind == 'content' else 'game', 'request_id': source})
            assert r.status_code == 200, r.text
        output = {'request_id': source, 'native_intent_id': intent, 'status': 'cancelled_before_start',
                  'device_closed': True, 'no_activity': True, 'task_resumed': False}
    else:
        identity = 'content_session_id' if kind == 'content' else 'game_session_id'
        if persist:
            r = client.post(path+'/end', json={identity: launched[identity], 'reason': 'child_requested', 'actual_seconds': 1})
            assert r.status_code == 200, r.text
        output = {'child_id': CHILD, 'session_id': sid, identity: launched[identity], 'status': 'stopped',
                  'device_closed': True, 'task_resumed': False, 'replayed': False}
    return turn, body(turn, output)


def objective(db, oid):
    with db.connect() as c:
        return dict(c.execute('SELECT * FROM agent_objective WHERE objective_id=?', (oid,)).fetchone())


@pytest.mark.parametrize('kind', ['content', 'games'])
@pytest.mark.parametrize('mode', ['cancel', 'stop'])
def test_actual_original_objective_terminal_and_restart_exact_replay(tmp_path, monkeypatch, kind, mode):
    db = Database(tmp_path/'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, path = setup(client, db, tmp_path, kind)
        source, oid, launched = start(client, db, sid, kind, path, mode)
        turn, payload = ending(client, tmp_path, monkeypatch, sid, kind, path, source, launched, mode)
        assert objective(db, oid)['status'] == 'active'
        r = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json=payload)
        assert r.status_code == 200, r.text
        assert objective(db, oid)['status'] == 'cancelled'
        frozen = objective(db, oid)
        with db.connect() as c:
            assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?', (source,)).fetchone()[0] == ('failed' if mode == 'cancel' else 'completed')
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        context = client.app.state.agent_orchestrator.agent_runtime.assemble_context(sid, ())
        assert context['active_objective'] is None
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        r = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json=payload)
        assert r.status_code == 200, r.text
        assert objective(db, oid) == frozen


@pytest.mark.parametrize('mode', ['cancel', 'stop'])
@pytest.mark.parametrize('case', ['no_server_fact', 'unconfirmed', 'wrong_receipt', 'failed'])
def test_unknown_failed_and_forged_bridge_cannot_close_goal(tmp_path, monkeypatch, mode, case):
    db = Database(tmp_path/'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, path = setup(client, db, tmp_path, 'content')
        source, oid, launched = start(client, db, sid, 'content', path, mode)
        turn, payload = ending(client, tmp_path, monkeypatch, sid, 'content', path, source, launched, mode,
                               persist=case != 'no_server_fact')
        before = objective(db, oid)
        if case == 'failed':
            payload = {'tool_status': 'failed', 'tool_error_code': 'CLIENT_TOOL_ACTION_FAILED',
                       'final_response': '关闭尚未核对。'}
        elif case == 'unconfirmed':
            payload['result']['execution_receipt']['output']['device_closed'] = False
        elif case == 'wrong_receipt':
            payload['result']['execution_receipt']['output'][
                'request_id' if mode == 'cancel' else 'content_session_id'] = str(uuid4())
        r = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json=payload)
        assert r.status_code == 200, r.text  # Existing generic outcome compatibility; not proof of closure.
        assert objective(db, oid) == before
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM audit_event WHERE action='controlled_execution_objective_cancelled'").fetchone()


@pytest.mark.parametrize('case', ['learning', 'other_child', 'revised', 'shared', 'legacy', 'continuation'])
def test_adversarial_goal_ownership_is_retained_after_real_end(tmp_path, monkeypatch, case):
    db = Database(tmp_path/'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, path = setup(client, db, tmp_path, 'content')
        source, oid, launched = start(client, db, sid, 'content', path, 'stop')
        turn, payload = ending(client, tmp_path, monkeypatch, sid, 'content', path, source, launched, 'stop')
        # Deliberate legacy/corrupt/shared ownership fixtures, never positive authority.
        with db.connect() as c:
            if case == 'learning':
                c.execute("UPDATE agent_objective SET objective_type='learning' WHERE objective_id=?", (oid,))
            elif case == 'other_child':
                c.execute("UPDATE agent_objective SET child_id='child-primary-4' WHERE objective_id=?", (oid,))
            elif case == 'revised':
                c.execute("UPDATE agent_objective SET summary='新的目标' WHERE objective_id=?", (oid,))
            elif case == 'legacy':
                c.execute("UPDATE agent_plan SET plan_json=json_set(plan_json,'$.objective.mode','none') WHERE agent_turn_id=?", (source,))
            elif case == 'continuation':
                c.execute("UPDATE agent_plan SET plan_json=json_set(plan_json,'$.after_execution','replan',"
                          "'$.continuation_scope',json(?)) WHERE agent_turn_id=?",
                          (json.dumps([{'capability': 'conversation.respond', 'arguments': {},
                                        'request_quote': '然后讨论我的作业'}]), source))
            else:
                prior = client.post(f'/v1/sessions/{sid}/agent-turn', json={
                    'transcript': '你好', 'client_action_id': 'shared-work', 'cloud_text_allowed': False})
                assert prior.status_code == 200, prior.text
                linked = c.execute('UPDATE agent_plan SET objective_id=? WHERE agent_turn_id=?',
                    (oid, prior.json()['agent_turn_id']))
                assert linked.rowcount == 1
            c.commit()
        before = objective(db, oid)
        r = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json=payload)
        assert r.status_code == (409 if case == 'shared' else 200), r.text
        if case == 'shared':
            assert r.json()['code'] == 'ANSWER_CONTROL_SCOPE_CHANGED'
            from home_study_agent.controlled_objective_closure import inspect
            with db.connect() as c:
                assert inspect(c, turn['agent_turn_id'], payload['result'])['reason'] == 'objective_shared_with_other_work'
        assert objective(db, oid) == before


@pytest.mark.parametrize('mode', ['cancel', 'stop'])
def test_closure_fault_rolls_back_goal_turn_step_and_audit_then_same_key_recovers(tmp_path, monkeypatch, mode):
    db = Database(tmp_path/'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, path = setup(client, db, tmp_path, 'content')
        source, oid, launched = start(client, db, sid, 'content', path, mode)
        turn, payload = ending(client, tmp_path, monkeypatch, sid, 'content', path, source, launched, mode)
        tid = turn['agent_turn_id']
        with db.connect() as c:
            c.execute("""CREATE TRIGGER closure_fault BEFORE INSERT ON audit_event
                WHEN NEW.action='controlled_execution_objective_cancelled'
                BEGIN SELECT RAISE(ABORT, 'synthetic_closure_write_fault'); END""")
            c.commit()
        client.raise_server_exceptions = False
        client._transport.raise_server_exceptions = False
        r = client.put(f'/v1/agent-turns/{tid}/outcome', json=payload)
        assert r.status_code == 500, r.text
        assert objective(db, oid)['status'] == 'active'
        with db.connect() as c:
            assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?', (tid,)).fetchone()[0] == 'pending'
            assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?', (tid,)).fetchone()[0] == 'running'
            c.execute('DROP TRIGGER closure_fault')
            c.commit()
        r = client.put(f'/v1/agent-turns/{tid}/outcome', json=payload)
        assert r.status_code == 200, r.text
        assert objective(db, oid)['status'] == 'cancelled'


def test_late_original_cancel_cannot_close_new_execution_objective(tmp_path, monkeypatch):
    db = Database(tmp_path/'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, path = setup(client, db, tmp_path, 'content')
        source, oid, launched = start(client, db, sid, 'content', path, 'cancel')
        turn, payload = ending(client, tmp_path, monkeypatch, sid, 'content', path, source, launched, 'cancel')
        # A real new question request creates a separate goal while the old device outcome is delayed.
        class NewGoal(Planner):
            async def complete(self, request):
                self.requests.append(request)
                assert request.request_type == 'agent_plan'
                packet = json.loads(request.messages[-1]['content'])
                value = {'utterance_binding': {'role': 'question', 'quote': packet['child_utterance']},
                    'objective': {'mode': 'new', 'type': 'learning', 'summary': '理解三角函数',
                                  'success_criteria': ['自己解释'], 'phase': 'explain'},
                    'next_step': {'capability': 'learning.explain',
                        'arguments': {'question': packet['child_utterance'], 'subject': '数学', 'support_level': 6},
                        'reason': '明确的新问题', 'expected_evidence': ['child_check_response']},
                    'spoken_transition': '我们来讨论这个问题。', 'requires_child_reply': True,
                    'after_execution': 'stop', 'confidence': .99}
                return ModelResult(content=json.dumps(value, ensure_ascii=False), provider='declared-synthetic', model='no-network', latency_ms=1)
        provider(client, NewGoal('content'))
        r = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '请给我讲讲正弦和余弦有什么区别', 'client_action_id': 'new-learning-request', 'cloud_text_allowed': True})
        assert r.status_code == 200, r.text
        with db.connect() as c:
            new = c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?', (r.json()['agent_turn_id'],)).fetchone()[0]
        assert new != oid and objective(db, new)['objective_type'] == 'learning'
        frozen = objective(db, new)
        r = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json=payload)
        assert r.status_code == 409 and r.json()['code'] == 'ANSWER_CONTROL_SCOPE_CHANGED', r.text
        assert objective(db, oid)['status'] == 'paused'
        assert objective(db, new) == frozen
        # Fresh speech may confirm the original request; it cannot use the new learning goal as its own.
        voice = capture(client, tmp_path, monkeypatch, sid, 'fresh-stop-asr', '停止原活动')
        fresh = cancel_turn(client, sid, voice, 'content', key='fresh-stop')
        assert fresh['action']['controlled_launch_cancel_binding']['request_id'] == source
        admitted = client.post(f"/v1/sessions/{sid}/agent-turns/{fresh['agent_turn_id']}/control-admission")
        assert admitted.status_code == 200, admitted.text
        r = client.put(f"/v1/agent-turns/{fresh['agent_turn_id']}/outcome",
            json=body(fresh, payload['result']['execution_receipt']['output']))
        assert r.status_code == 200, r.text
        assert objective(db, oid)['status'] == 'cancelled'
        assert objective(db, new) == frozen
