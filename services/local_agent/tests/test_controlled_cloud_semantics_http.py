"""Declared synthetic semantic provider, actual local permission/turn/write APIs.

No network model or native resource is launched in this test file.
"""
import json
import sqlite3
import pytest
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelResult
from home_study_agent.voice_control_intent import VoiceControlIntent
from test_app import advance_to_study, client_for
from test_controlled_start_atomic_http import CHILD
from test_controlled_end_identity_http import identity_key, stored
from test_learning_runtime_http import NoCloud


class Semantic:
    configured = True

    def __init__(self, control, quote, query=None, hook=None, role='control'):
        self.control, self.quote, self.query, self.hook, self.role = control, quote, query, hook, role
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        assert request.request_type == 'voice_interaction'
        payload = json.loads(request.messages[-1]['content'])
        assert set(payload) == {'transcript', 'state'}
        assert payload['transcript'] == self.quote
        assert all(type(v) is bool for v in payload['state'].values())
        assert 'controlled_content.play' in request.messages[0]['content']
        assert 'controlled_game.play' in request.messages[0]['content']
        if self.hook:
            self.hook()
        value = {'role': self.role, 'control': self.control, 'quote': self.quote, 'confidence': .99}
        if self.query is not None:
            value['query'] = self.query
        return ModelResult(content=json.dumps(value, ensure_ascii=False), provider='synthetic', model='declared-semantic', latency_ms=1)


@pytest.mark.parametrize('control', ['controlled_content.play', 'controlled_game.play',
                                    'controlled_content.stop', 'controlled_game.stop'])
def test_typed_external_control_parser(control):
    quote = '我想看看Synthetic exercise这段视频' if control.endswith('.play') else '先把这个关掉'
    value = {'role': 'control', 'control': control, 'quote': quote, 'confidence': .99}
    if control.endswith('.play'):
        value['query'] = 'Synthetic exercise'
    parsed = VoiceControlIntent.parse(json.dumps(value), quote)
    assert parsed['status'] == 'control'
    assert parsed['control'] == control


def prepare(client, db, tmp_path, kind):
    sid, pid = advance_to_study(client, action_prefix='semantic-controlled')
    assert client.post(f'/v1/plan-items/{pid}/start', json={'client_action_id': 'original-task'}).status_code == 200
    token = client.post('/v1/parent/setup', json={'pin': '4826'}).json()['parent_token']
    headers = {'X-HSC-Parent': token}
    parent = f'/v1/parent/children/{CHILD}/controlled-{kind}'
    assert client.put(parent + '/rule', headers=headers, json={
        'enabled': True, 'max_daily_minutes': 20, 'max_item_minutes': 8}).status_code == 200
    if kind == 'games':
        asset = tmp_path / 'inert.exe'
        asset.write_bytes(b'synthetic protocol only; never launched')
        approval = {'title': '图形练习', 'executable_path': str(asset), 'max_minutes': 6}
    else:
        root = tmp_path / 'video'
        root.mkdir()
        (root / 'inert.mp4').write_bytes(b'synthetic protocol only; never played')
        # This fixture registers these roots as real backup assets. Provide
        # actual synthetic source files; missing registered assets stay fatal.
        transcripts = tmp_path / 'transcripts'
        transcripts.mkdir()
        (transcripts / '101.txt').write_text('Synthetic reference', encoding='utf-8')
        with sqlite3.connect(tmp_path / 'synthetic.db') as source:
            source.execute('CREATE TABLE resources(resource_id INTEGER PRIMARY KEY)')
            source.execute('INSERT INTO resources VALUES(101)')
            source.commit()
        with db.connect() as c:
            c.execute('''INSERT INTO curriculum_corpus(corpus_id,title,corpus_type,source_database_path,
                transcript_root_path,video_root_path,source_ref,license_status,review_status,status,
                resource_count,transcript_count,local_video_count,imported_at,updated_at)
                VALUES('semantic-media','Synthetic media','seyk',?,?,?,'local-test','public_personal_use',
                'approved_baseline','ready',1,1,1,datetime('now'),datetime('now'))''',
                (str(tmp_path/'synthetic.db'),str(tmp_path/'transcripts'),str(root)))
            rid = c.execute('''INSERT INTO curriculum_resource(corpus_id,external_resource_id,title,
                description,stage_title,grade_title,subject_title,semester,unit_path,transcript_text,
                video_relative_path,video_duration,local_video_available,import_job_id)
                VALUES('semantic-media','101','图形练习','Synthetic only','初中','初三','数学',
                '上册','Synthetic unit','Synthetic reference','inert.mp4',600,1,'synthetic-job')''').lastrowid
            c.commit()
        approval = {'curriculum_row_id': rid, 'max_minutes': 6}
    saved = client.post(parent + '/approvals', headers=headers, json=approval)
    assert saved.status_code == 200, saved.text
    path = f'/v1/sessions/{sid}/controlled-{kind}'
    original = client.post(path + '/start', json={'query': '图形练习', 'client_action_id': 'original-activity'})
    assert original.status_code == 200, original.text
    ended = client.post(path + '/end', json={identity_key(kind): original.json()[identity_key(kind)],
        'reason': 'synthetic_protocol_history', 'actual_seconds': 0})
    assert ended.status_code == 200, ended.text
    token = client.post('/v1/parent/unlock', json={'pin': '4826'}).json()['parent_token']
    headers = {'X-HSC-Parent': token}
    permission = client.put('/v1/parent/privacy', headers=headers, json={'cloud_model_text_allowed': True})
    assert permission.status_code == 200, permission.text
    return sid, pid, path, headers


def provider(client, value):
    client.app.state.agent_orchestrator.model_gateway = value
    client.app.state.agent_orchestrator.voice_control_intent.gateway = value


@pytest.mark.parametrize('kind', ['content', 'games'])
def test_cloud_allowed_natural_start_in_retained_outcome_context(tmp_path, kind):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _, path, _ = prepare(client, db, tmp_path, kind)
        control = 'controlled_content.play' if kind == 'content' else 'controlled_game.play'
        quote = '我想看看图形练习这段视频' if kind == 'content' else '我想玩玩图形练习这个游戏'
        gateway = Semantic(control, quote, '图形练习')
        provider(client, gateway)
        heard = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': quote, 'client_action_id': 'natural-start', 'cloud_text_allowed': True})
        assert heard.status_code == 200, heard.text
        turn = heard.json()
        with db.connect() as c:
            decisions = [dict(r) for r in c.execute('SELECT decision_json FROM voice_control_intent_request')]
        assert turn['action']['type'] == ('start_controlled_content' if kind == 'content' else 'start_controlled_game'), (decisions, turn.get('response'), len(gateway.requests))
        assert turn['requires_control_admission'] is True
        assert turn['action']['query'] == '图形练习'
        assert turn['action']['selection_binding']['source_version']
        admission = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
        assert admission.status_code == 200, admission.text
        started = client.post(path + '/start', json={'query': turn['action']['query'],
            'client_action_id': turn['agent_turn_id'], 'selection_binding': turn['action']['selection_binding']})
        assert started.status_code == 200, started.text
        assert started.json()['launch_allowed'] is True
        again = client.post(path + '/start', json={'query': turn['action']['query'],
            'client_action_id': turn['agent_turn_id'], 'selection_binding': turn['action']['selection_binding']})
        assert again.status_code == 200 and again.json()['launch_allowed'] is False
        assert again.json()[identity_key(kind)] == started.json()[identity_key(kind)]
        assert len(gateway.requests) == 1
        serialized = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
        for secret in (str(tmp_path), sid, CHILD, 'Synthetic reference', 'Synthetic unit'):
            assert secret not in serialized


@pytest.mark.parametrize('quote,role,query', [
    ('不要打开图形练习', 'control', '图形练习'),
    ('老师说打开图形练习', 'control', '图形练习'),
    ('如果打开图形练习会怎样', 'control', '图形练习'),
    ('打开图形练习然后交卷', 'control', '图形练习'),
    ('打开图形练习答案是三', 'mixed', '图形练习'),
    ('打开图形练习', 'control', '模型编出的别题'),
])
def test_external_launch_rejects_non_executable_or_invented_query(quote, role, query):
    payload = {'role': role, 'control': 'controlled_content.play', 'quote': quote, 'confidence': .99, 'query': query}
    assert VoiceControlIntent.parse(json.dumps(payload), quote)['status'] != 'control'


@pytest.mark.parametrize('control', ['controlled_content.play','controlled_game.play','controlled_content.stop','controlled_game.stop'])
def test_pending_evaluation_catalog_cannot_acquire_external_controls(control):
    payload = {'role': 'control', 'control': control, 'quote': '打开图形练习', 'confidence': .99}
    if control.endswith('.play'):
        payload['query'] = '图形练习'
    assert VoiceControlIntent.parse(json.dumps(payload), payload['quote'], restricted=True)['status'] != 'control'


@pytest.mark.parametrize('quote', ['结束播放', '不看了', '不玩电脑了'])
def test_ambiguous_stop_is_not_hardcoded_to_external_media(quote):
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    assert AgentOrchestrator._direct_controlled_stop(quote) is None


@pytest.mark.parametrize('quote,accepted', [('播放“光影”', True), ('他说“播放光影”', False),
    ('“播放光影”', False), ('不要播放“光影”', False), ('如果播放“光影”会怎样', False)])
def test_media_literal_quotes_are_not_quoted_commands(quote, accepted):
    payload = {'role':'control','control':'controlled_content.play','quote':quote,'confidence':.99,'query':'光影'}
    parsed = VoiceControlIntent.parse(json.dumps(payload), quote)
    assert (parsed['status'] == 'control') is accepted
    assert VoiceControlIntent.text_eligible('播放“光影”', [], [])
    assert not VoiceControlIntent.text_eligible('他说“播放光影”', [], [])


@pytest.mark.parametrize('quote,query', [('帮我看看“光影”这个标题怎么读','光影'),
    ('我看到了“播放光影”这句话','播放光影')])
def test_named_literal_exception_never_promotes_text_discussion(quote,query):
    payload={'role':'control','control':'controlled_content.play','quote':quote,'confidence':.99,'query':query}
    assert VoiceControlIntent.parse(json.dumps(payload),quote)['status']!='control'
    assert not VoiceControlIntent.text_eligible(quote,[],[])


def request_start(client, sid, key='new-start'):
    response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': '我想看看图形练习这段视频', 'client_action_id': key, 'cloud_text_allowed': True})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('phase', ['model', 'admission', 'start'])
@pytest.mark.parametrize('change', ['permission', 'consent_cycle', 'source'])
def test_exact_authority_changes_block_effect_without_retarget(tmp_path, phase, change):
    db = Database(tmp_path/'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid, path, headers = prepare(client, db, tmp_path, 'content')
        def mutate():
            if change == 'consent_cycle':
                for allowed in (False, True):
                    assert client.put('/v1/parent/privacy', headers=headers,
                        json={'cloud_model_text_allowed': allowed}).status_code == 200
            elif change == 'permission':
                assert client.put(f'/v1/parent/children/{CHILD}/controlled-content/rule',headers=headers,
                    json={'enabled': False,'max_daily_minutes':20,'max_item_minutes':8}).status_code == 200
            else:
                # A real file replacement, not a forged source-version field.
                (tmp_path/'video'/'inert.mp4').write_bytes(b'revised synthetic source; not the authorized original')
        gateway = Semantic('controlled_content.play', '我想看看图形练习这段视频', '图形练习',
                           hook=mutate if phase == 'model' else None)
        provider(client, gateway)
        turn = request_start(client, sid)
        if phase == 'model':
            assert turn['action']['type'] == 'none'
        else:
            assert turn['action']['type'] == 'start_controlled_content', turn
            if phase == 'admission':
                mutate()
            admission = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
            if phase == 'admission':
                assert admission.status_code == 409, admission.text
            else:
                assert admission.status_code == 200, admission.text
                mutate()
                failed = client.post(path+'/start',json={'query':'图形练习','client_action_id':turn['agent_turn_id'],
                    'selection_binding':turn['action']['selection_binding']})
                assert failed.status_code == 409, failed.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM controlled_content_session').fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'",(pid,)).fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM companion_activity_outcome').fetchone()[0] == 1
