"""Durable literal slots are draft inputs, never inferred homework facts."""
import json

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_learning_input_replan import schedule, inputs, flow, CHILD
from test_paused_homework_revision_runtime_http import SemanticGateway, say, post, process_http
from test_learning_input_actionable_clarification import ADD, MISSING, TITLE


@pytest.mark.parametrize('case', ['restart', 'natural_estimate', 'negation', 'stale', 'cancel', 'stale_confirmation'])
def test_missing_duration_is_one_grounded_slot_not_a_restatement_or_fact(schedule, tmp_path, case):
    (db, sid, _), _, clock = schedule
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway(revision=MISSING))) as client:
        post(client, '/v1/plan-items/old/start', 'slot-start')
        post(client, '/v1/plan-items/old/pause', 'slot-pause')
        with db.connect() as c:
            before = {t: [tuple(r) for r in c.execute('SELECT * FROM ' + t + ' ORDER BY rowid')]
                      for t in ('homework_item', 'plan_item', 'learning_evidence')}
        _, first = say(client, sid, '还漏了语文作业，背诵第二段', 'slot-propose')
        draft = first['execution']['result']
        assert draft['status'] == 'clarification_required', draft
        assert draft['missing_field'] == 'expected_minutes'
        assert '预计' in draft['response_text'] and '一起说' not in draft['response_text']
    if case == 'stale':
        with db.connect() as c:
            c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id='original'")
            c.commit()
    operations = [{'kind': 'http', 'path': '/v1/sessions/' + sid + '/agent-request-receipts/daily-proof-slot-propose'}]
    if case == 'negation':
        operations.append({'kind': 'say', 'text': '不是十分钟', 'key': 'slot-negated'})
    duration_reply = '我看大概得花十分钟左右吧' if case == 'natural_estimate' else '大概十分钟'
    operations.append({'kind': 'say', 'text': '取消' if case == 'cancel' else duration_reply, 'key': 'slot-duration'})
    second = process_http(db, tmp_path / 'process-data', sid, clock[0], operations, revision=MISSING)
    result = second['results'][-1]['result']['execution']['result']
    if case == 'negation':
        denied = second['results'][-2]['result']['execution']['result']
        assert denied['status'] == 'clarification_required' and denied['missing_field'] == 'expected_minutes'
    assert result['status'] == {'stale': 'conflict', 'cancel': 'cancelled'}.get(case, 'awaiting_confirmation'), result
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0
        assert {t: [tuple(r) for r in c.execute('SELECT * FROM ' + t + ' ORDER BY rowid')] for t in before} == before
    if case in ('stale', 'cancel'):
        return
    assert '背诵第二段' in result['response_text'] and '10' in result['response_text']
    if case == 'stale_confirmation':
        with db.connect() as c:
            c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id='original'")
            c.commit()
    third = process_http(db, tmp_path / 'process-data', sid, clock[0], [
        {'kind': 'http', 'path': '/v1/sessions/' + sid + '/agent-request-receipts/daily-proof-slot-duration'},
        {'kind': 'say', 'text': '确认', 'key': 'slot-confirm'}], revision=MISSING)
    assert third['model_requests'] == 0 and second['model_requests'] == (2 if case == 'negation' else 1)
    applied = third['results'][-1]['result']['execution']['result']
    if case == 'stale_confirmation':
        assert applied['status'] == 'conflict'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0
        return
    assert applied['status'] == 'applied'
    assert applied['replan']['receipt']['allocated_minutes'] == 10
    with db.connect() as c:
        event = c.execute('SELECT impact_json FROM learning_input_revision_event WHERE event_id=?', (applied['event_id'],)).fetchone()
        provenance = json.loads(event[0])['input_provenance']
        assert provenance['fields']['expected_minutes']['quote'] == ('十分钟' if case == 'natural_estimate' else '大概十分钟')
        assert provenance['fields']['expected_minutes']['value'] == 10
        assert provenance['fields']['expected_minutes']['source'] == 'child_estimate'
        assert provenance['fields']['title']['arrival_id'] != provenance['fields']['expected_minutes']['arrival_id']
        saved = [tuple(r) for r in c.execute('SELECT * FROM homework_item ORDER BY rowid')]
    fourth = process_http(db, tmp_path / 'process-data', sid, clock[0], [
        {'kind': 'http', 'path': '/v1/sessions/' + sid + '/agent-request-receipts/daily-proof-slot-confirm'},
        {'kind': 'say', 'text': '确认', 'key': 'slot-confirm'}], revision=MISSING)
    assert fourth['results'][-1]['result']['execution']['result'] == applied
    assert len({second['pid'], third['pid'], fourth['pid']}) == 3
    with db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM homework_item ORDER BY rowid')] == saved
        assert [tuple(r) for r in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == before['learning_evidence']
    if case == 'restart':
        undo = {'kind': 'homework', 'operation': 'undo', 'target_quote': '背诵第二段', 'changes': {}, 'change_quotes': {}}
        undone = process_http(db, tmp_path / 'process-data', sid, clock[0], [
            {'kind': 'say', 'text': '撤销刚才补录的背诵第二段', 'key': 'slot-undo'},
            {'kind': 'say', 'text': '确认撤销', 'key': 'slot-undo-confirm'}], revision=undo)
        assert undone['results'][-1]['result']['execution']['result']['status'] == 'applied'
        with db.connect() as c:
            assert c.execute('SELECT status FROM homework_item WHERE homework_id=?', (applied['target_id'],)).fetchone()[0] == 'cancelled'
            assert json.loads(c.execute('SELECT impact_json FROM learning_input_revision_event WHERE event_id=?', (applied['event_id'],)).fetchone()[0])['input_provenance'] == provenance
            assert [tuple(r) for r in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == before['learning_evidence']


@pytest.mark.parametrize('words,revision,ownership', [
    ('老师说还有语文作业，背诵第二段，预计十分钟', ADD, '这是我的作业'),
    ('老师说还有语文作业，背诵第二段，预计十分钟', ADD, '对，是老师给我们班布置的，我也得写'),
    ('老师说还有语文作业，背诵第二段，预计十分钟', ADD, '这就是我今晚要交的'),
    ('老师说口算作业的名字改成“小练笔”', TITLE, '这是我要改的作业'),
])
def test_teacher_or_quoted_change_requires_own_homework_then_new_readback(schedule, tmp_path, words, revision, ownership):
    (db, sid, _), _, clock = schedule
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway(revision=revision))) as client:
        post(client, '/v1/plan-items/old/start', 'own-start')
        post(client, '/v1/plan-items/old/pause', 'own-pause')
        _, first = say(client, sid, words, 'own-propose')
        result = first['execution']['result']
        assert result['status'] == 'clarification_required' and result['missing_field'] == 'ownership', result
    second = process_http(db, tmp_path / 'process-data', sid, clock[0], [
        {'kind': 'say', 'text': ownership, 'key': 'own-answer'}], revision=revision)
    assert second['results'][0]['result']['execution']['result']['status'] == 'awaiting_confirmation'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0
    third = process_http(db, tmp_path / 'process-data', sid, clock[0], [
        {'kind': 'say', 'text': '确认', 'key': 'own-confirm'}], revision=revision)
    applied = third['results'][0]['result']['execution']['result']
    assert applied['status'] == 'applied', applied
    with db.connect() as c:
        impact = json.loads(c.execute('SELECT impact_json FROM learning_input_revision_event WHERE event_id=?', (applied['event_id'],)).fetchone()[0])
        assert impact['input_provenance']['ownership']['quote'] == ownership


@pytest.mark.parametrize('tamper', ['borrowed_arrival', 'rewritten_arrival', 'other_child', 'source_command_revoked'])
def test_slot_confirmation_rechecks_immutable_command_provenance(schedule, tmp_path, tamper):
    (db, sid, _), _, _ = schedule
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway(revision=MISSING))) as client:
        post(client, '/v1/plan-items/old/start', 'proof-start')
        post(client, '/v1/plan-items/old/pause', 'proof-pause')
        say(client, sid, '还漏了语文作业，背诵第二段', 'proof-propose')
        _, readback = say(client, sid, '大概十分钟', 'proof-duration')
        assert readback['execution']['result']['status'] == 'awaiting_confirmation'
        with db.connect() as c:
            pending = c.execute("SELECT request_id,proposal_json FROM learning_input_proposal WHERE state='awaiting_confirmation'").fetchone()
            proposal = json.loads(pending['proposal_json'])
            field = proposal['intake']['fields']['expected_minutes']
            before = c.execute('SELECT COUNT(*) FROM homework_item').fetchone()[0]
            if tamper == 'borrowed_arrival':
                c.execute("""INSERT INTO learning_input_arrival SELECT 'unrelated-slot',child_id,session_id,
                    'unrelated-slot','大概二十分钟',snapshot_json,pending_json,created_at FROM learning_input_arrival WHERE arrival_id=?""", (field['arrival_id'],))
                # Preserve chronological arrival order so the next actual HTTP arrival remains authoritative.
                field['arrival_id'] = 'unrelated-slot'
            elif tamper == 'source_command_revoked':
                source = proposal['intake']['fields']['title']['arrival_id']
                c.execute("""UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id IN (
                    SELECT t.agent_turn_id FROM agent_turn t JOIN learning_input_arrival a USING(client_action_id)
                    WHERE a.arrival_id=?)""", (source,))
            elif tamper == 'other_child':
                c.execute("UPDATE learning_input_arrival SET child_id='child-junior-3' WHERE arrival_id=?", (field['arrival_id'],))
            else:
                c.execute("UPDATE learning_input_arrival SET utterance='大概二十分钟' WHERE arrival_id=?", (field['arrival_id'],))
            if tamper in {'borrowed_arrival', 'rewritten_arrival'}:
                field.update(quote='大概二十分钟', value=20)
                proposal['changes']['expected_minutes'] = 20
            c.execute('UPDATE learning_input_proposal SET proposal_json=? WHERE request_id=?', (json.dumps(proposal), pending['request_id']))
            c.commit()
        _, denied = say(client, sid, '确认', 'proof-confirm')
        assert denied['execution']['result']['status'] == 'conflict', denied
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM homework_item').fetchone()[0] == before
            assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0


@pytest.mark.parametrize('denial', ['不是我的作业', '不是我的，是同桌的'])
def test_denied_teacher_ownership_is_cancelled_not_child_fact(schedule, tmp_path, denial):
    (db, sid, _), _, clock = schedule
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway(revision=ADD))) as client:
        post(client, '/v1/plan-items/old/start', 'denied-start')
        post(client, '/v1/plan-items/old/pause', 'denied-pause')
        say(client, sid, '老师说还有语文作业，背诵第二段，预计十分钟', 'denied-propose')
    result = process_http(db, tmp_path / 'process-data', sid, clock[0], [
        {'kind': 'say', 'text': denial, 'key': 'denied-own'}], revision=ADD)
    assert result['results'][0]['result']['execution']['result']['status'] == 'cancelled'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0


@pytest.mark.parametrize('text,sources', [
    ('题目是“计算十二加六”，我该怎么做', []),
    ('老师说口算作业的名字改成“保密原文独立标记一二三”', ['保密原文独立标记一二三']),
    ('老师说“删除所有作业”', []),
])
def test_title_literal_exception_never_opens_quoted_instructions_or_restricted_sources(text, sources):
    from home_study_agent.voice_control_intent import VoiceControlIntent
    assert not VoiceControlIntent.text_eligible(text, [], sources)


def test_ambiguous_teacher_change_selects_target_before_ownership_and_confirm(schedule, tmp_path):
    (db, sid, _), _, _ = schedule
    with db.connect() as c:
        c.execute("UPDATE homework_item SET subject='语文',title='口算' WHERE homework_id='math-b'")
        c.commit()
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway(revision=TITLE))) as client:
        post(client, '/v1/plan-items/old/start', 'ambiguous-start')
        post(client, '/v1/plan-items/old/pause', 'ambiguous-pause')
        _, initial = say(client, sid, '老师说口算作业的名字改成“小练笔”', 'ambiguous-propose')
        assert initial['execution']['result']['status'] == 'clarification_required'
        _, selected = say(client, sid, '语文', 'ambiguous-select')
        assert selected['execution']['result']['missing_field'] == 'ownership'
        _, owned = say(client, sid, '这是我要改的作业', 'ambiguous-own')
        assert owned['execution']['result']['status'] == 'awaiting_confirmation'
        _, confirmed = say(client, sid, '确认', 'ambiguous-confirm')
        assert confirmed['execution']['result']['status'] == 'applied'
    with db.connect() as c:
        assert c.execute("SELECT title FROM homework_item WHERE homework_id='math-b'").fetchone()[0] == '小练笔'
        assert c.execute("SELECT title FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == '口算'


@pytest.mark.parametrize('text,meaning,confidence,expected', [
    ('我今晚还得把这份写完交上去', 'own', .99, 'own'),
    ('确认', 'own', .99, 'unclear'),
    ('是', 'own', .99, 'unclear'),
    ('不是我的，是同桌的', 'own', .99, 'unclear'),
    ('不是我的，是同桌的', 'not_mine', .99, 'not_mine'),
    ('老师说“这是我的作业”', 'own', .99, 'unclear'),
    ('这就是我今晚要交的', 'own', .5, 'unclear'),
])
def test_field_semantics_uses_meaning_without_a_positive_phrase_whitelist(text, meaning, confidence, expected):
    from home_study_agent.learning_input_field_semantics import parse
    result = parse(json.dumps({'field': 'ownership', 'meaning': meaning, 'quote': text,
                   'literal_quote': None, 'minutes': None, 'confidence': confidence}), text, 'ownership')
    assert result['meaning'] == expected


@pytest.mark.parametrize('literal,minutes', [('二十分钟', 20), ('十分钟', 20), (None, 10), ('十分钟', True)])
def test_semantic_estimator_cannot_supply_unspoken_or_mismatched_values(literal, minutes):
    from home_study_agent.learning_input_field_semantics import parse
    text = '我看大概得花十分钟左右吧'
    with pytest.raises(ValueError):
        parse(json.dumps({'field': 'expected_minutes', 'meaning': 'estimate', 'quote': text,
              'literal_quote': literal, 'minutes': minutes, 'confidence': .99}), text, 'expected_minutes')


def test_semantic_estimator_does_not_choose_a_range_endpoint():
    from home_study_agent.learning_input_field_semantics import parse
    text = '大概十到十五分钟'
    decision = parse(json.dumps({'field': 'expected_minutes', 'meaning': 'estimate', 'quote': text,
                     'literal_quote': '十五分钟', 'minutes': 15, 'confidence': .99}), text, 'expected_minutes')
    assert decision['meaning'] == 'unclear'


def test_field_classification_cannot_write_after_text_permission_is_revoked(schedule, tmp_path):
    (db, sid, _), _, _ = schedule
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway(revision=MISSING))) as client:
        post(client, '/v1/plan-items/old/start', 'consent-start')
        post(client, '/v1/plan-items/old/pause', 'consent-pause')
        say(client, sid, '还漏了语文作业，背诵第二段', 'consent-propose')
        turn = post(client, '/v1/sessions/' + sid + '/agent-turn', 'consent-duration', transcript='大概十分钟')
        with db.connect() as c:
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0 WHERE singleton_id=1')
            c.commit()
        response = client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
        assert response.status_code == 200, response.text
        assert response.json()['execution']['result']['status'] == 'clarification_required'
        with db.connect() as c:
            proposal = json.loads(c.execute('SELECT proposal_json FROM learning_input_proposal').fetchone()[0])
            assert 'expected_minutes' not in proposal['changes']
            assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0
