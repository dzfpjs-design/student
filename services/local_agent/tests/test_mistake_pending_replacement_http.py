"""Replace an unprocessed saved answer in one child utterance."""
import copy
import json
from dataclasses import replace as replace_settings

import pytest
from fastapi.testclient import TestClient

from home_study_agent.agent_runtime import InvalidAgentPlanError
from home_study_agent.app import create_app
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_cloud_first_agent import Planner
from test_mistake_pending_answer_http import execute, hear
from test_mistake_pending_recovery_control_http import checkpoint
from test_mistake_voice_journey import focus, ok, settings
from test_mistake_dialogue_diagnosis import opened, speak
from test_mistake_gate_reliability import CHILD, flow


def replace_answer(client, sid, text='刚才的答案改成A', *, cloud=False,
                   key='replace-pending-answer'):
    return ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': text, 'client_action_id': key,
        'cloud_text_allowed': cloud,
    }))


@pytest.mark.parametrize('natural', [False, True])
def test_replace_pending_answer_scores_only_replacement_and_survives_restart(
        tmp_path, monkeypatch, natural):
    configured, sid, db, old = checkpoint(tmp_path, monkeypatch)
    stale = None
    text = ('刚才没处理完的答案不对，请替换成A'
            if natural else '刚才的答案改成A')
    planner = Planner('mistake.dialogue', {
        'utterance': text,
        'pending_answer_control': 'replace',
        'replacement_answer': 'A',
    }, text, role='correction')
    with TestClient(create_app(configured, model_gateway=planner if natural else None)) as client:
        stale = copy.deepcopy(focus(client, sid))
        request = replace_answer(client, sid, text, cloud=natural)
        assert request['action']['type'] == 'continue_mistake_dialogue'
        result = execute(client, request)
        assert execute(client, request) == result
        assert 'pending_operation' not in focus(client, sid)['state']
    with TestClient(create_app(configured)) as client:
        assert 'pending_operation' not in focus(client, sid)['state']
        with pytest.raises(InvalidAgentPlanError, match='WITHDRAWN'):
            MistakeDialogueService(db)._score_operation(stale, old)
    with db.connect() as c:
        answers = [row[0] for row in c.execute('SELECT submitted_answer FROM mistake_attempt')]
        assert answers == ['A']
        old_receipt = c.execute(
            'SELECT result_json FROM mistake_dialogue_event WHERE client_action_id=?',
            (old['action_id'],)).fetchone()
        audit = json.loads(old_receipt[0])
        assert audit['withdrawn_pending_answer']['operation'] == old
        assert audit['pending_answer_replacement']['replacement_answer'] == 'A'
        assert audit['pending_answer_replacement']['new_operation']['answer'] == 'A'
    if natural:
        assert len(planner.requests) == 1
        payload = json.dumps(planner.requests[0].messages, ensure_ascii=False)
        assert old['utterance'] not in payload
        assert old['action_id'] not in payload
        assert old['authorized_turn_id'] not in payload


def test_replacement_failure_keeps_new_answer_for_restart_retry(tmp_path, monkeypatch):
    configured, sid, db, old = checkpoint(tmp_path, monkeypatch)
    original_score = MistakeDialogueService._score_operation
    with TestClient(create_app(configured)) as client:
        request = replace_answer(client, sid)

        def fail(*args, **kwargs):
            raise RuntimeError('synthetic replacement score failure')

        with monkeypatch.context() as patch:
            patch.setattr(MistakeDialogueService, '_score_operation', fail)
            execute(client, request)
        pending = focus(client, sid)['state']['pending_operation']
        assert pending['answer'] == 'A'
        assert pending['utterance'] == '刚才的答案改成A'
        assert pending['action_id'] != old['action_id']
    monkeypatch.setattr(MistakeDialogueService, '_score_operation', original_score)
    with TestClient(create_app(configured)) as client:
        execute(client, hear(client, sid, '重试', 'retry-replacement-after-restart'))
        assert 'pending_operation' not in focus(client, sid)['state']
    with db.connect() as c:
        assert [row[0] for row in c.execute('SELECT submitted_answer FROM mistake_attempt')] == ['A']


@pytest.mark.parametrize('text', [
    '不要把刚才的答案改成A',
    '如果把刚才的答案改成A会怎样',
    '老师说把刚才的答案改成A',
    '刚才的答案是不是要改成A？',
    '我不想把刚才的答案改成A',
    '我没打算把刚才的答案换成A',
    '刚才答案不应该是A，先别改成B',
])
def test_model_cannot_replace_from_negated_reported_or_hypothetical_text(
        tmp_path, monkeypatch, text):
    configured, sid, db, old = checkpoint(tmp_path, monkeypatch)
    planner = Planner('mistake.dialogue', {
        'utterance': text,
        'pending_answer_control': 'replace',
        'replacement_answer': 'A',
    }, text, role='correction')
    with TestClient(create_app(configured, model_gateway=planner)) as client:
        request = replace_answer(client, sid, text, cloud=True, key='unsafe-replacement')
        if request['action']['type'] == 'continue_mistake_dialogue':
            client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert focus(client, sid)['state']['pending_operation'] == old
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('change', [
    'operation', 'source', 'model_receipt', 'model_status', 'model_owner', 'answer_argument',
])
def test_natural_replacement_requires_current_owned_exact_receipt(
        tmp_path, monkeypatch, change):
    configured, sid, db, old = checkpoint(tmp_path, monkeypatch)
    text = '刚才没处理完的答案不对，请替换成A'
    planner = Planner('mistake.dialogue', {
        'utterance': text,
        'pending_answer_control': 'replace',
        'replacement_answer': 'A',
    }, text, role='correction')
    with TestClient(create_app(configured, model_gateway=planner)) as client:
        request = replace_answer(client, sid, text, cloud=True, key='replace-before-change')
        with db.connect() as c:
            if change == 'operation':
                c.execute("UPDATE mistake_dialogue SET state_json=json_set(state_json,'$.pending_operation.answer','C') WHERE session_id=?", (sid,))
            elif change == 'source':
                item_id = focus(client, sid)['state']['next_activity']['item']['diagnostic_item_id']
                c.execute("UPDATE diagnostic_item SET prompt=prompt||' amended' WHERE diagnostic_item_id=?", (item_id,))
            elif change == 'model_receipt':
                c.execute("UPDATE model_call SET context_manifest_json=json_remove(context_manifest_json,'$.mistake_pending_replace') WHERE purpose='agent_plan'")
            elif change == 'model_status':
                c.execute("UPDATE model_call SET verification_status='failed' WHERE purpose='agent_plan'")
            elif change == 'model_owner':
                c.execute("UPDATE model_call SET child_id='child-junior-3' WHERE purpose='agent_plan'")
            else:
                c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.replacement_answer','C') WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (request['agent_turn_id'],))
            c.commit()
        response = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert response.status_code in {200, 409, 422}, response.text
        current = focus(client, sid)['state']['pending_operation']
        assert current['action_id'] == old['action_id']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0


def test_scored_pending_answer_is_not_rewritten_by_replacement(tmp_path, monkeypatch):
    configured, sid, db, old = checkpoint(tmp_path, monkeypatch)
    with TestClient(create_app(configured)) as client:
        service = MistakeDialogueService(db)
        service._score_operation(focus(client, sid), old)
        with db.connect() as c:
            before = [tuple(row) for row in c.execute('SELECT * FROM mistake_attempt')]
        request = replace_answer(client, sid)
        response = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['execution']['result']['status'] == 'blocked'
        assert result['execution']['result']['reason'] == 'answer_already_recorded'
        assert '还没有替换' in result['turn']['response_text']
        assert focus(client, sid)['state']['pending_operation'] == old
    with db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM mistake_attempt')] == before


def test_replacement_audit_failure_rolls_back_old_and_new_operations(tmp_path, monkeypatch):
    configured, sid, db, old = checkpoint(tmp_path, monkeypatch)
    with db.connect() as c:
        c.execute('''CREATE TRIGGER fail_replacement_audit BEFORE INSERT ON mistake_dialogue_event
            WHEN json_extract(NEW.result_json,'$.pending_answer_replacement.version')=1
            BEGIN SELECT RAISE(ABORT,'synthetic replacement audit failure'); END''')
        c.commit()
    with TestClient(create_app(configured), raise_server_exceptions=False) as client:
        request = replace_answer(client, sid)
        response = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert response.status_code >= 400
        assert focus(client, sid)['state']['pending_operation'] == old
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM mistake_dialogue_event WHERE client_action_id=?',
                             (old['action_id'],)).fetchone()
        c.execute('DROP TRIGGER fail_replacement_audit')
        c.commit()
    with TestClient(create_app(configured)) as client:
        execute(client, replace_answer(client, sid, key='replacement-after-audit-recovery'))
    with db.connect() as c:
        assert [row[0] for row in c.execute('SELECT submitted_answer FROM mistake_attempt')] == ['A']


def test_replace_pending_diagnosis_answer_attaches_only_new_evidence(
        flow, tmp_path, monkeypatch):
    db, _, _, _ = flow
    configured = replace_settings(settings(tmp_path), database_path=db.path)
    with TestClient(create_app(configured)) as client:
        dialogue, book, case, sid = opened(flow)
        speak(dialogue, sid, '不是粗心，我不知道为什么错', 'replacement-probe-reason')
        request = hear(client, sid, '答案是41', 'unprocessed-replacement-probe')

        def fail(*args, **kwargs):
            raise RuntimeError('synthetic diagnostic score interruption')

        with monkeypatch.context() as patch:
            patch.setattr(MistakeDialogueService, '_score_operation', fail)
            execute(client, request)
        old = focus(client, sid)['state']['pending_operation']
        assert old['kind'] == 'diagnosis_answer'
        execute(client, replace_answer(client, sid, '刚才的答案改成42',
                                       key='replace-pending-probe-answer'))
        assert focus(client, sid)['phase'] == 'remediation'
        probe = book.diagnosis_state(case, CHILD)['probes'][0]
        assert probe['answer']['answer'] == '42'
    with db.connect() as c:
        assert [row[0] for row in c.execute('SELECT submitted_answer FROM diagnostic_attempt')] == ['42']
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
        audit = json.loads(c.execute(
            'SELECT result_json FROM mistake_dialogue_event WHERE client_action_id=?',
            (old['action_id'],)).fetchone()[0])
        assert audit['pending_answer_replacement']['replacement_answer'] == '42'
