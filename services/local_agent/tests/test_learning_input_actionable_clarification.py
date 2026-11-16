"""Original v4 scenarios, now with bounded slots instead of forced restatement."""
import json

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_paused_homework_revision_runtime_http import SemanticGateway, say, post, process_http
from test_learning_input_replan import schedule, inputs, flow, CHILD

ADD = {'kind': 'homework', 'operation': 'add', 'target_quote': '背诵第二段',
       'changes': {'subject': '语文', 'title': '背诵第二段', 'expected_minutes': 10},
       'change_quotes': {'subject': '语文', 'title': '背诵第二段', 'expected_minutes': '十分钟'}}
TITLE = {'kind': 'homework', 'operation': 'update', 'target_quote': '口算',
         'changes': {'title': '小练笔'}, 'change_quotes': {'title': '小练笔'}}
COMPLETE_ADD = '我要补录语文作业，背诵第二段，预计十分钟'
MISSING = {**ADD, 'changes': {'subject': '语文', 'title': '背诵第二段'},
           'change_quotes': {'subject': '语文', 'title': '背诵第二段'}}


@pytest.mark.parametrize('first,initial,role,marker,repeated,revision', [
    ('老师说还有语文作业，背诵第二段，预计十分钟', ADD, 'control', '自己的话', COMPLETE_ADD, ADD),
    ('老师说还有语文作业，背诵第二段，预计十分钟', ADD, 'unknown', '自己的话', COMPLETE_ADD, ADD),
    ('口算作业的名字改成“小练笔”', TITLE, 'control', '自己的话', '我要把口算作业的名字改成小练笔', TITLE),
    ('还漏了语文作业，背诵第二段', MISSING, 'control', '预计', COMPLETE_ADD, ADD),
])
def test_clarification_does_not_adopt_attribution_and_full_restatement_recovers_after_restart(
        schedule, tmp_path, first, initial, role, marker, repeated, revision):
    (db, sid, _), _, clock = schedule
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway(role=role, revision=initial))) as client:
        post(client, '/v1/plan-items/old/start', 'clarify-start')
        post(client, '/v1/plan-items/old/pause', 'clarify-pause')
        with db.connect() as c:
            before = {table: [tuple(row) for row in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                      for table in ('homework_item', 'plan_item', 'learning_evidence')}
        turn, response = say(client, sid, first, 'needs-clarification')
        spoken = response['execution']['response_text']
        retained = role == 'control'
        assert ('预计' if initial == MISSING else '自己') in spoken, spoken
        assert '服务' not in spoken, 'Local input rejection must not claim a cloud outage'
        with db.connect() as c:
            plan = json.loads(c.execute('SELECT plan_json FROM agent_plan WHERE agent_turn_id=?',
                                       (turn['agent_turn_id'],)).fetchone()[0])
            if retained:
                assert response['execution']['result']['requires_child_reply'] is True
            else:
                assert plan['requires_child_reply'] is True
            assert c.execute('SELECT COUNT(*) FROM learning_input_proposal').fetchone()[0] == int(retained)
            assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0
            assert {table: [tuple(row) for row in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                    for table in before} == before
    restarted = process_http(db, tmp_path / 'process-data', sid, clock[0], [
        {'kind': 'http', 'path': '/v1/sessions/' + sid + '/agent-request-receipts/daily-proof-needs-clarification'},
        {'kind': 'say', 'text': '确认', 'key': 'not-a-full-request'},
        {'kind': 'say', 'text': ('大概十分钟' if initial == MISSING else '这是我要改的作业') if retained else repeated,
         'key': 'own-explicit-request'}], revision=revision)
    assert restarted['results'][1]['result']['execution']['result']['status'] == ('clarification_required' if retained else 'responded')
    proposal = restarted['results'][2]['result']['execution']['result']
    assert proposal['status'] == 'awaiting_confirmation', proposal
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0
        assert {table: [tuple(row) for row in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in before} == before
    confirmed = process_http(db, tmp_path / 'process-data', sid, clock[0], [
        {'kind': 'say', 'text': '确认', 'key': 'confirm-own-request'}], revision=revision)
    assert confirmed['pid'] != restarted['pid']
    result = confirmed['results'][0]['result']['execution']['result']
    assert result['status'] == 'applied', result
    with db.connect() as c:
        event = c.execute('SELECT request_id FROM learning_input_revision_event WHERE event_id=?', (result['event_id'],)).fetchone()
        source = c.execute('SELECT utterance FROM learning_input_arrival WHERE arrival_id=?', (event[0],)).fetchone()[0]
        assert source == (first if retained else repeated)
        impact = json.loads(c.execute('SELECT impact_json FROM learning_input_revision_event WHERE event_id=?', (result['event_id'],)).fetchone()[0])
        if retained:
            proof = impact['input_provenance']
            if initial == MISSING:
                assert proof['fields']['expected_minutes']['quote'] == '大概十分钟'
            else:
                assert proof['ownership']['quote'] == '这是我要改的作业'
        assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == before['learning_evidence']
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='old'").fetchone()[0] == 'paused'
        assert c.execute('SELECT COUNT(*) FROM homework_item').fetchone()[0] == len(before['homework_item']) + int(revision['operation'] == 'add')


@pytest.mark.parametrize('reason', ['CONTROL_MODEL_FAILED', 'MODEL_UNAVAILABLE', 'CLOUD_TEXT_DISABLED', 'CONTROL_SCOPE_CHANGED'])
def test_actionable_input_clarification_never_hides_real_service_or_scope_failure(reason):
    from home_study_agent.learning_input_voice import clarification_response
    assert clarification_response({'status': 'unavailable', 'reason': reason},
                                  '老师说还有语文作业，背诵第二段') is None
