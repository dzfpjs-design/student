"""Paper facts under review are not a scored answer or a confirmed mistake."""
import pytest
from contextlib import ExitStack
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_mistake_voice_journey import settings, fixture, begin, say, focus
from test_pet_vocabulary_prompt_integration import Planner
from test_mistake_stage_independent_dialogue import ask, learning_facts
from test_pending_evaluation_independent_tutor import QUESTION, tutor_models, lesson_body, tutor_calls
from test_foreground_independent_dialogue import foreground_models
from home_study_agent.agent_runtime import AgentRuntimeStore
import json


def paper_facts(j):
    values = learning_facts(j)
    with j['db'].connect() as c:
        for table in ('mistake_capture_candidate', 'mistake_capture_candidate_event',
                      'capture_artifact', 'paper_scoring_request', 'paper_scoring_decision'):
            values[table] = [tuple(row) for row in c.execute('SELECT * FROM ' + table)]
    return values


def test_standalone_scoring_does_not_require_or_create_readonly_review_store(tmp_path):
    from test_answer_attempt_ledger import capture_visible_attempt, setup_case
    from home_study_agent.mistake_dialogue import MistakeDialogueService
    db, monitor = setup_case(tmp_path)
    with db.connect() as connection:
        for table in ('ocr_review_event', 'ocr_review_batch', 'ocr_review_item'):
            connection.execute('DROP TABLE ' + table)
        connection.commit()
    capture_visible_attempt(db, monitor)
    dialogue = MistakeDialogueService(db)
    opened = dialogue.begin('study', 'page', 'review-store-begin')
    assert opened.get('dialogue_binding') is None
    scored = dialogue.respond('study', '对', 'review-store-score')
    assert scored['phase'] == 'diagnosis' and scored.get('dialogue_binding') is None
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='ocr_review_item'").fetchone()
        assert c.execute('SELECT count(*) FROM paper_scoring_decision').fetchone()[0] == 1


@pytest.fixture(params=[('candidate', []), ('answer', ['对']), ('confirm', ['对', '我选B'])])
def paper_fact_context(tmp_path, request):
    phase, responses = request.param
    config = settings(tmp_path)
    gateway = Planner()
    gateway.configured = False
    with ExitStack() as original_client_lifecycle:
        client = original_client_lifecycle.enter_context(TestClient(create_app(config, model_gateway=gateway)))
        sid, db = fixture(client)
        begin(client, sid)
        for index, response in enumerate(responses):
            say(client, sid, response, 'prepare-fact-' + str(index))
        original = focus(client, sid)
        assert original['phase'] == phase
        assert original['mistake_id'] is None
        yield {'settings': config, 'gateway': gateway, 'client': client, 'db': db,
               'sid': sid, 'original': original, 'finish_client': original_client_lifecycle.close}


def test_fact_review_can_answer_a_separate_question_without_accepting_original(paper_fact_context, monkeypatch):
    j = paper_fact_context
    assert j['original'].get('dialogue_binding'), j['original']
    tutor_models(j, monkeypatch)
    before = paper_facts(j)
    planned = ask(j, QUESTION)
    assert planned.status_code == 200, planned.text
    taught = j['client'].post(f"/v1/sessions/{j['sid']}/tutor", json=lesson_body(planned.json()))
    assert taught.status_code == 200, taught.text
    assert paper_facts(j) == before
    j['finish_client']()
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        replay = restarted.post(f"/v1/sessions/{j['sid']}/tutor", json=lesson_body(planned.json()))
        assert replay.status_code == 200, replay.text
        assert replay.json()['turn_id'] == taught.json()['turn_id']
        assert focus(restarted, j['sid'])['dialogue_binding'] == j['original']['dialogue_binding']
    assert len(tutor_calls(j)) == 1
    assert paper_facts(j) == before
    with j['db'].connect() as c:
        assert AgentRuntimeStore.local_mistake_answer_target(c, j['sid'], j['original']['child_id']) is None
    for sent in j['gateway'].requests:
        value = json.dumps({'messages': sent.messages, 'manifest': sent.context_manifest,
                            'refs': sent.evidence_refs}, ensure_ascii=False)
        for private in (j['sid'], j['original']['child_id'], j['original']['candidate_id'],
                        j['original']['state']['artifact_id'], j['original']['state']['question_text']):
            assert private not in value


@pytest.mark.parametrize('fault', ['phase', 'candidate', 'artifact', 'owner', 'answer'])
@pytest.mark.parametrize('timing', ['before_dispatch', 'after_dispatch'])
def test_fact_source_changes_reject_late_tutor(paper_fact_context, monkeypatch, fault, timing):
    j = paper_fact_context
    def invalidate():
        with j['db'].connect() as c:
            if fault == 'phase':
                c.execute("UPDATE mistake_dialogue SET phase='paused' WHERE session_id=?", (j['sid'],))
            elif fault == 'candidate':
                c.execute("UPDATE mistake_capture_candidate SET status='skipped' WHERE candidate_id=?",
                          (j['original']['candidate_id'],))
            elif fault == 'artifact':
                c.execute("UPDATE capture_artifact SET extracted_text='different page' WHERE artifact_id=?",
                          (j['original']['state']['artifact_id'],))
            elif fault == 'owner':
                c.execute("UPDATE mistake_capture_candidate SET child_id='child-junior-3' WHERE candidate_id=?",
                          (j['original']['candidate_id'],))
            else:
                state = {**j['original']['state'], 'submitted_answer': 'changed-original-answer'}
                c.execute('UPDATE mistake_dialogue SET state_json=? WHERE session_id=?',
                          (json.dumps(state), j['sid']))
            c.commit()
    tutor_models(j, monkeypatch, after_submission=invalidate if timing == 'after_dispatch' else None)
    planned = ask(j, QUESTION)
    assert planned.status_code == 200, planned.text
    if timing == 'before_dispatch':
        invalidate()
    before = paper_facts(j)
    result = j['client'].post(f"/v1/sessions/{j['sid']}/tutor", json=lesson_body(planned.json()))
    if timing == 'before_dispatch':
        assert result.status_code == 422, result.text
        assert result.json()['code'] == 'AGENT_PLAN_INVALID'
        assert not tutor_calls(j)
        assert paper_facts(j) == before
    else:
        assert result.status_code == 409, result.text
        assert len(tutor_calls(j)) == 1
        with j['db'].connect() as c:
            assert not c.execute('SELECT 1 FROM tutor_turn').fetchone()


@pytest.mark.parametrize('fault', ['missing_candidate', 'missing_artifact', 'answer_phase',
    'bad_phase_type', 'blank_candidate', 'answer_item', 'extra'])
def test_fact_reference_cannot_become_an_answer_capability(fault):
    from home_study_agent.voice_capture_binding import normalize_question_reference, VoiceCaptureBindingError
    binding = {'session_id': 'synthetic-session', 'child_id': 'synthetic-child', 'mistake_id': None,
               'candidate_id': 'candidate', 'artifact_id': 'artifact', 'phase': 'candidate',
               'item_id': 'source-dialogue', 'source_version': 'version'}
    if fault.startswith('missing_'):
        binding.pop(fault.removeprefix('missing_') + '_id')
    elif fault == 'answer_phase':
        binding['phase'] = 'attempt'
    elif fault == 'bad_phase_type':
        binding['phase'] = []
    elif fault == 'blank_candidate':
        binding['candidate_id'] = ''
    elif fault == 'answer_item':
        binding['item_id'] = 'scorable-item'
    else:
        binding['extra'] = 'unexpected'
    with pytest.raises(VoiceCaptureBindingError):
        normalize_question_reference({'kind': 'mistake', 'binding': binding})


@pytest.mark.parametrize('role,scope', [('question', 'current'), ('answer', None),
                                      ('mixed', None), ('unknown', None)])
def test_original_or_mixed_speech_does_not_gain_independent_tutoring(paper_fact_context, monkeypatch, role, scope):
    j = paper_fact_context
    quote = '这道题给我一个提示。' if role == 'question' else '我选B，不过还想问个问题'
    foreground_models(j, monkeypatch, quote, role=role, scope=scope)
    planned = ask(j, quote)
    assert planned.status_code == 200, planned.text
    assert not planned.json()['action'].get('tutor_request_action_id')
    assert not any(request.request_type == 'agent_plan' for request in j['gateway'].requests)
    assert not tutor_calls(j)
    with j['db'].connect() as c:
        assert not c.execute('SELECT 1 FROM tutor_turn').fetchone()
