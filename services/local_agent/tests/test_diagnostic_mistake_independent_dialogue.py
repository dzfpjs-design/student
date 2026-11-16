"""Real local diagnostic receipts and HTTP; speech/model data are synthetic."""
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.agent_runtime import AgentRuntimeStore
from test_mistake_voice_journey import settings, focus
from test_pet_vocabulary_prompt_integration import Planner
from test_mistake_stage_independent_dialogue import ask, learning_facts
from test_pending_evaluation_independent_tutor import QUESTION, tutor_models, lesson_body, tutor_calls


@pytest.fixture(params=['diagnosis', 'remediation'])
def diagnostic_context(tmp_path, request):
    config = settings(tmp_path)
    gateway = Planner()
    gateway.configured = False
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        session = client.post('/v1/sessions', json={'child_id': 'child-primary-4',
            'client_action_id': 'diagnostic-original-session'})
        assert session.status_code == 201, session.text
        sid = session.json()['session_id']
        db = Database(config.database_path)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        receipt = LearningStateService(db).submit_diagnostic(sid,
            'diag-p4-arithmetic-closed-1', '54', 'diagnostic-original-wrong')
        assert receipt['score_status'] == 'verified_local'
        case = client.app.state.mistake_book.capture_from_attempt(receipt)
        dialogue = MistakeDialogueService(db)
        original = dialogue.open_review(sid, 'open-diagnostic-original', mistake_id=case['mistake_id'])
        if request.param == 'remediation':
            probed = dialogue.respond(sid, '我不知道为什么错了', 'diagnostic-original-reason')
            assert probed['phase'] == 'diagnosis_probe'
            original = dialogue.respond(sid, '答案是42', 'diagnostic-original-probe-answer')
        assert original['phase'] == request.param
        assert not original.get('answer_binding')
        yield {'settings': config, 'db': db, 'sid': sid, 'client': client,
               'gateway': gateway, 'original': original, 'receipt': receipt}


def test_diagnostic_origin_independent_tutor_keeps_original_and_restarts(diagnostic_context, monkeypatch):
    j = diagnostic_context
    original = focus(j['client'], j['sid'])
    assert original.get('dialogue_binding'), original
    j['original'] = original
    tutor_models(j, monkeypatch)
    before = learning_facts(j)
    planned = ask(j, QUESTION)
    assert planned.status_code == 200, planned.text
    body = lesson_body(planned.json())
    taught = j['client'].post(f"/v1/sessions/{j['sid']}/tutor", json=body)
    assert taught.status_code == 200, taught.text
    assert learning_facts(j) == before
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        replay = restarted.post(f"/v1/sessions/{j['sid']}/tutor", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['turn_id'] == taught.json()['turn_id']
        assert focus(restarted, j['sid'])['dialogue_binding'] == original['dialogue_binding']
    assert len(tutor_calls(j)) == 1
    assert learning_facts(j) == before
    with j['db'].connect() as c:
        assert AgentRuntimeStore.local_mistake_answer_target(c, j['sid'], original['child_id']) is None
    for sent in j['gateway'].requests:
        value = json.dumps({'messages': sent.messages, 'manifest': sent.context_manifest,
                            'refs': sent.evidence_refs}, ensure_ascii=False)
        for private in (j['sid'], original['child_id'], original['mistake_id'],
                        j['receipt']['evidence_id'], j['receipt']['attempt_id'], original['state']['question_text']):
            assert private not in value


@pytest.mark.parametrize('fault', ['item', 'evidence', 'answer', 'owner', 'legacy', 'withdrawal', 'inactive'])
@pytest.mark.parametrize('timing', ['before_dispatch', 'after_dispatch'])
def test_diagnostic_receipt_changes_reject_late_independent_tutor(diagnostic_context, monkeypatch, fault, timing):
    j = diagnostic_context
    j['original'] = focus(j['client'], j['sid'])
    assert j['original'].get('dialogue_binding')
    def invalidate():
        with j['db'].connect() as c:
            c.execute('BEGIN')
            if fault == 'item':
                c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?",
                          (j['receipt']['diagnostic_item_id'],))
            elif fault == 'evidence':
                c.execute('UPDATE learning_evidence SET confidence=.1 WHERE evidence_id=?', (j['receipt']['evidence_id'],))
            elif fault == 'answer':
                c.execute("UPDATE diagnostic_attempt SET submitted_answer='different' WHERE attempt_id=?", (j['receipt']['attempt_id'],))
            elif fault == 'owner':
                c.execute("UPDATE diagnostic_attempt SET child_id='child-junior-3' WHERE attempt_id=?", (j['receipt']['attempt_id'],))
            elif fault == 'withdrawal':
                from home_study_agent.learning_evidence_withdrawal import record_withdrawal
                record_withdrawal(c, child_id=j['original']['child_id'], session_id=j['sid'],
                    evidence_id=j['receipt']['evidence_id'], client_action_id='withdraw-diagnostic-origin',
                    request={'reason': 'synthetic original answer withdrawn'})
            elif fault == 'inactive':
                c.execute("UPDATE diagnostic_item SET status='archived' WHERE diagnostic_item_id=?", (j['receipt']['diagnostic_item_id'],))
            else:
                c.execute("UPDATE mistake_case SET source_type='manual' WHERE mistake_id=?", (j['original']['mistake_id'],))
            c.commit()
    tutor_models(j, monkeypatch, after_submission=invalidate if timing == 'after_dispatch' else None)
    planned = ask(j, QUESTION)
    assert planned.status_code == 200, planned.text
    body = lesson_body(planned.json())
    if timing == 'before_dispatch':
        invalidate()
    result = j['client'].post(f"/v1/sessions/{j['sid']}/tutor", json=body)
    assert result.status_code in {403, 409, 422}, result.text
    assert len(tutor_calls(j)) == int(timing == 'after_dispatch')
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 0


def test_diagnostic_reference_is_readonly_and_missing_receipt_cannot_publish(diagnostic_context):
    from home_study_agent.mistake_dialogue_reference import current_binding
    j = diagnostic_context
    with j['db'].connect() as c:
        c.execute('PRAGMA query_only=ON')
        changes = c.total_changes
        assert current_binding(c, j['sid'], j['original']['child_id'])
        assert c.total_changes == changes
    with j['db'].connect() as c:
        c.execute('DELETE FROM diagnostic_attempt WHERE attempt_id=?', (j['receipt']['attempt_id'],))
        c.commit()
        assert current_binding(c, j['sid'], j['original']['child_id']) is None
