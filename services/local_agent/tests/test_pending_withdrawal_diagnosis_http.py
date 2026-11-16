"""Withdrawing an interrupted probe must not be treated as a diagnostic answer."""
from dataclasses import replace
import json

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_mistake_dialogue_diagnosis import opened, speak
from test_mistake_gate_reliability import flow, CHILD
from test_mistake_pending_answer_http import hear, execute
from test_mistake_voice_journey import focus, settings


def test_withdraw_probe_then_new_answer_attaches_only_new_evidence(flow, tmp_path, monkeypatch):
    db, _, _, _ = flow
    configured = replace(settings(tmp_path), database_path=db.path)
    with TestClient(create_app(configured)) as client:
        dialogue, book, case, sid = opened(flow)
        speak(dialogue, sid, '不是粗心，我不知道为什么错', 'probe-reason')
        before = focus(client, sid)
        assert before['phase'] == 'diagnosis_probe'
        request = hear(client, sid, '答案是41', 'unprocessed-probe-answer')
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic diagnostic scoring interruption')
        with monkeypatch.context() as patch:
            patch.setattr(MistakeDialogueService, '_score_operation', fail)
            execute(client, request)
        pending = focus(client, sid)['state']['pending_operation']
        assert pending['kind'] == 'diagnosis_answer'
        execute(client, hear(client, sid, '撤回刚才的回答', 'withdraw-probe-answer'))
        current = focus(client, sid)
        assert current['phase'] == 'diagnosis_probe'
        assert current['state']['diagnosis_probe_id'] == before['state']['diagnosis_probe_id']
        assert not book.diagnosis_state(case, CHILD)['probes'][0]['answer']
    with TestClient(create_app(configured)) as client:
        execute(client, hear(client, sid, '答案是42', 'new-probe-answer'))
        assert focus(client, sid)['phase'] == 'remediation'
    with db.connect() as c:
        answers = [r[0] for r in c.execute('SELECT submitted_answer FROM diagnostic_attempt')]
        assert answers == ['答案是42']
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
        receipt = c.execute('SELECT result_json FROM mistake_dialogue_event WHERE client_action_id=?',
                             (pending['action_id'],)).fetchone()
        assert json.loads(receipt[0])['withdrawn_pending_answer']['operation'] == pending
    assert book.diagnosis_state(case, CHILD)['assessment']['causal_status'] == 'unconfirmed'
