"""Two real writing attempts and evaluator persistence, with no-network models."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator, LearningEvaluationUnknownError
from home_study_agent.repository import StudyRepository
from home_study_agent.writing_teaching import WritingTeachingService
from home_study_agent.formative_evaluation_projection import build_formative_projection
from home_study_agent.writing_followup_evidence import validate_formal_followup_evaluation
from test_agent_continuation import orchestrator_for
from test_formative_evaluator import evaluation, projection, writing


@pytest.fixture(params=['revision', 'independent_check'])
def second_draft(evaluation, request):
    case, scope, evaluator, gateway, calls, first_turn = evaluation
    db, sid, prompts, parent, originals = case
    first = asyncio.run(evaluator.evaluate(first_turn))
    teaching = WritingTeachingService(db)
    teaching.initialize()
    agent, controls = orchestrator_for(db, db.path.parent, {})
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    agent.complete_turn(first_turn, first['response_text'], 'completed', None, first)
    kind = request.param
    quote, cap = ('我想按刚才的反馈自己改一遍', 'writing.revise_original') if kind == 'revision' else (
        '再来一道不一样的，我自己写', 'writing.independent_check')
    controls.output = {'role': 'control', 'control': cap, 'quote': quote, 'confidence': .99}
    heard = asyncio.run(agent.interpret(sid, quote, 'formal-followup', True))
    assert heard.get('runtime_capability') == cap, heard
    executed = AgentCapabilityExecutor(db, StudyRepository(db)).execute(heard['agent_turn_id'])
    target = executed['result']['prompt']
    agent.complete_turn(heard['agent_turn_id'], executed['response_text'], 'completed', None, executed)
    text = ('Dear Alex, meet me at the library at ten this Saturday. We will study English and practise reading. '
            'Please bring your notebook. I look forward to studying with you. Best wishes, Mia.') if kind == 'revision' else (
            'Dear Mia, thank you for the wonderful book. I liked its funny characters and surprising ending. '
            'I will return it to you at school on Monday morning. Best wishes, Alex.')
    original = originals.capture_native_response(sid, prompt_id=target['prompt_id'], item_version=target['item_version'],
        text=text, client_action_id='second-original')
    saved = prompts.answer(sid, prompt_id=target['prompt_id'], item_version=target['item_version'],
        answer=text, client_action_id='second-answer', writing_original_artifact_id=original['artifact_id'])
    return dict(case=case, scope=scope, evaluator=evaluator, gateway=gateway, calls=calls, first=first,
        agent=agent, heard=heard, target=target, original=original, saved=saved, text=text, kind=kind)


def next_evaluation(f):
    result = f['agent'].agent_runtime.finalize_diagnostic_evaluation(f['case'][1], f['saved']['attempt']['evidence_id'])
    assert result['status'] == 'feedback_required', result
    return result['feedback_turn_id']


def test_formal_followup_second_manuscript_is_evaluated_and_persisted(second_draft):
    f = second_draft
    turn = next_evaluation(f)
    result = asyncio.run(f['evaluator'].evaluate(turn))
    assert result['evaluation']['formative_only'] and result['evaluation']['certification_status'] == 'candidate'
    assert len(f['calls']) == 2
    assert json.loads(f['calls'][-1].messages[1]['content'])['response'] == f['text']
    view = WritingTeachingService(f['case'][0]).read(f['case'][1], prompt_id=f['target']['prompt_id'],
        item_version=f['target']['item_version'])
    assert view['status'] == 'feedback_ready', view
    assert view['assessment_id'] != f['first']['evaluation']['rubric_assessment']['assessment_id']
    assert view['feedback']['authority'] == 'uncalibrated_model_formative'
    with f['case'][0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='writing_original'").fetchone()[0] == 2


def scope_for(f):
    return {**f['scope'], 'prompt_id':f['target']['prompt_id'], 'item_version':f['target']['item_version']}


@pytest.mark.parametrize('withdraw', [False, True])
def test_second_model_response_ready_restart_never_repeats_or_accepts_retracted_work(second_draft, monkeypatch, withdraw):
    f = second_draft
    turn = next_evaluation(f)
    def crash(*args):
        raise RuntimeError('synthetic crash after second model response')
    monkeypatch.setattr(f['evaluator'], '_persist', crash)
    with pytest.raises(LearningEvaluationUnknownError):
        asyncio.run(f['evaluator'].evaluate(turn))
    db = f['case'][0]
    with db.connect() as c:
        assert c.execute("SELECT state FROM learning_evaluation_execution WHERE state='response_ready'").fetchone()[0] == 'response_ready'
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 1
        if withdraw:
            c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id=?", (f['original']['artifact_id'],))
            c.commit()
    reopened = LearningOutcomeEvaluator(db, StudyRepository(db), f['gateway'], f['evaluator'].settings)
    if withdraw:
        with pytest.raises(LearningEvaluationUnknownError): reopened.recover_ready(turn)
    else:
        result = reopened.recover_ready(turn)
        assert result['evaluation']['formative_only']
        assert asyncio.run(reopened.evaluate(turn)) == result
    assert len(f['calls']) == 2
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == (1 if withdraw else 2)
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 1


def change_source(f, fault):
    db = f['case'][0]
    if fault == 'feedback_revision':
        from home_study_agent.learning_rubric import RubricEvidenceService
        from test_pet_media_evidence import review_rubric
        service = RubricEvidenceService(db)
        snapshot = service.get(f['scope']['child_id'], f['first']['evaluation']['rubric_assessment']['assessment_id'])
        review_rubric(service, snapshot)
        return
    with db.connect() as c:
        if fault == 'privacy': c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
        elif fault == 'source': c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (f['target']['diagnostic_item_id'],))
        elif fault == 'original': c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id=?", (f['original']['artifact_id'],))
        elif fault == 'parent_original': c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_type='writing_original' AND artifact_id!=?", (f['original']['artifact_id'],))
        elif fault == 'command': c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (f['heard']['agent_turn_id'],))
        elif fault == 'semantic':
            key = f['heard']['action']['writing_followup_semantic_receipt']['request_key']
            c.execute("UPDATE voice_control_intent_request SET status='cancelled' WHERE request_key=?", (key,))
        elif fault in {'answer_erased','parent_answer_erased'}:
            from home_study_agent.privacy_source_withdrawal import ensure_schema, record
            ensure_schema(c)
            c.execute('BEGIN')
            prompt = f['target'] if fault == 'answer_erased' else f['case'][3]
            eid = c.execute('''SELECT a.evidence_id FROM diagnostic_prompt p JOIN diagnostic_attempt a
                ON a.attempt_id=p.attempt_id WHERE p.prompt_id=?''', (prompt['prompt_id'],)).fetchone()[0]
            record(c, f['scope']['child_id'], {'synthetic-erased-source'}, {eid})
        c.commit()


@pytest.mark.parametrize('fault', ['privacy','source','original','parent_original','command','semantic',
    'feedback_revision','answer_erased','parent_answer_erased'])
@pytest.mark.parametrize('late', [False, True])
def test_second_evaluator_rechecks_original_authority_before_dispatch_and_persistence(second_draft, fault, late):
    from home_study_agent.learning_outcome_evaluator import InvalidLearningEvaluationError
    f = second_draft
    turn = next_evaluation(f)
    if late: f['gateway'].before_return = lambda: change_source(f, fault)
    else: change_source(f, fault)
    with pytest.raises((LearningEvaluationUnknownError, InvalidLearningEvaluationError)):
        asyncio.run(f['evaluator'].evaluate(turn))
    assert len(f['calls']) == (2 if late else 1)
    with f['case'][0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['child','session','attempt','prepare_key','parent','request','reference',
    'arrival','manifest','packet','voice','source_deleted','hint'])
def test_historical_formal_proof_never_borrows_other_targets_or_execution(second_draft, fault):
    f = second_draft
    scope = dict(child_id=f['scope']['child_id'],session_id=f['case'][1],prompt_id=f['target']['prompt_id'],
        item_version=f['target']['item_version'],attempt_id=f['saved']['attempt']['attempt_id'])
    with f['case'][0].connect() as c:
        c.execute('BEGIN')
        before = c.total_changes
        assert validate_formal_followup_evaluation(c,**scope)['packet'] == f['heard']['action']['writing_followup_binding']
        assert c.total_changes == before
        if fault in {'child','session','attempt'}: scope[fault+'_id'] = 'other-owner'
        elif fault == 'prepare_key': c.execute("UPDATE diagnostic_prompt SET prepare_action_id='other-key' WHERE prompt_id=?", (scope['prompt_id'],))
        elif fault == 'parent': c.execute("UPDATE writing_followup SET parent_prompt_id=? WHERE prompt_id=?", (scope['prompt_id'],scope['prompt_id']))
        elif fault == 'request': c.execute("UPDATE agent_turn SET transcript='another request' WHERE agent_turn_id=?", (f['heard']['agent_turn_id'],))
        elif fault == 'reference': c.execute("UPDATE diagnostic_prompt SET request_json=json_set(request_json,'$.writing_reference_revision',json('{}')) WHERE prompt_id=?", (scope['prompt_id'],))
        elif fault == 'arrival': c.execute("UPDATE learning_input_arrival SET utterance='another arrival' WHERE client_action_id='formal-followup'")
        elif fault == 'manifest': c.execute("UPDATE agent_plan SET context_manifest_json='{}' WHERE agent_turn_id=?", (f['heard']['agent_turn_id'],))
        elif fault == 'packet': c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.writing_followup_binding.action.feedback_version','changed') WHERE agent_turn_id=?", (f['heard']['agent_turn_id'],))
        elif fault == 'voice': c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.voice_input_id','another-voice') WHERE agent_turn_id=?", (f['heard']['agent_turn_id'],))
        elif fault == 'source_deleted': c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id=?", (f['heard']['agent_turn_id'],))
        elif fault == 'hint': c.execute('UPDATE learning_evidence SET hint_level=? WHERE evidence_id=?',
            (0 if f['kind']=='revision' else 1, f['saved']['attempt']['evidence_id']))
        with pytest.raises(ValueError): validate_formal_followup_evaluation(c, **scope)


@pytest.mark.parametrize('second_draft', ['independent_check'], indirect=True)
@pytest.mark.parametrize('exposure', ['answer','tutor'])
@pytest.mark.parametrize('after_answer', [False, True])
def test_independent_followup_uses_original_admission_time_not_rolling_exposure_window(second_draft, exposure, after_answer):
    f = second_draft
    scope = dict(child_id=f['scope']['child_id'],session_id=f['case'][1],prompt_id=f['target']['prompt_id'],
        item_version=f['target']['item_version'],attempt_id=f['saved']['attempt']['attempt_id'])
    with f['case'][0].connect() as c:
        c.execute('BEGIN')
        attempt = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(scope['attempt_id'],)).fetchone())
        at = (datetime.fromisoformat(attempt['created_at']) + timedelta(days=1 if after_answer else -30)).isoformat()
        if exposure == 'answer':
            evidence = dict(c.execute('SELECT * FROM learning_evidence WHERE evidence_id=?',(attempt['evidence_id'],)).fetchone())
            evidence.update(evidence_id='other-answer-evidence',client_action_id='other-answer-evidence',occurred_at=at)
            attempt.update(attempt_id='other-answer',evidence_id=evidence['evidence_id'],client_action_id='other-answer',created_at=at)
            for table,row in [('learning_evidence',evidence),('diagnostic_attempt',attempt)]:
                c.execute('INSERT INTO '+table+' ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        else:
            question = c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?',
                (attempt['diagnostic_item_id'],)).fetchone()[0]
            c.execute('''INSERT INTO tutor_turn(turn_id,session_id,child_id,subject,question,answer,status,created_at,client_action_id)
                VALUES('other-hint',?,?,'英语',?,'Synthetic exact-question hint','completed',?,'other-hint')''',
                (scope['session_id'],scope['child_id'],question,at))
        if after_answer:
            assert validate_formal_followup_evaluation(c,**scope)['packet']==f['heard']['action']['writing_followup_binding']
        else:
            with pytest.raises(ValueError,match='FOLLOWUP_PRIOR_'+('ANSWER' if exposure=='answer' else 'TUTOR')+'_EXPOSURE'):
                validate_formal_followup_evaluation(c,**scope)
