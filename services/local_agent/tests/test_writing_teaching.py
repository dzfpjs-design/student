"""Real SQLite follow-up links, with no model request or score on GET."""
import pytest

from home_study_agent.writing_teaching import WritingTeachingService, bind_writing_followup
from formative_response_fixture import criterion_reply
from test_writing_response_origin import writing, capture, answer, bind, TEXT, CHILD
from test_writing_rubric import prepared


def ready(writing):
    rubric, snapshot, body = prepared(writing)
    rubric.review(CHILD, snapshot['assessment_id'], **body)
    service = WritingTeachingService(writing[0])
    service.initialize()
    return service, rubric, snapshot


def read(service, writing):
    return service.read(writing[1], prompt_id=writing[3]['prompt_id'], item_version=writing[3]['item_version'])


def resolve(service, writing, kind='revision', item=None):
    return service.resolve_next_action(writing[1], prompt_id=writing[3]['prompt_id'], item_version=writing[3]['item_version'],
                                       kind=kind, diagnostic_item_id=item)


def test_teaching_read_does_not_invent_feedback_grade_or_tasks(writing):
    service = WritingTeachingService(writing[0])
    service.initialize()
    assert read(service, writing)['status'] == 'waiting_answer'
    original = capture(writing)
    answer(writing)
    with writing[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind(c, writing, original)
        c.commit()
    for _ in range(2):
        assert read(service, writing)['status'] == 'feedback_required'
        assert resolve(service, writing)['status'] == 'blocked'
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 0


def test_exact_revision_and_distinct_independent_source_contract(writing):
    service, _, _ = ready(writing)
    result = read(service, writing)
    assert result['status'] == 'feedback_ready' and not result['automatic_certification']
    revised = resolve(service, writing)
    assert revised['action']['required_hint_level'] == 1
    assert revised['action']['diagnostic_item_id'] == writing[3]['diagnostic_item_id']
    assert resolve(service, writing, 'revision', 'diag-pet-writing-2')['status'] == 'blocked'
    assert resolve(service, writing, 'independent_check')['reason'] == 'writing_exact_new_source_required'
    assert resolve(service, writing, 'independent_check', writing[3]['diagnostic_item_id'])['status'] == 'blocked'
    independent = resolve(service, writing, 'independent_check', 'diag-pet-writing-2')
    assert independent['status'] == 'ready' and independent['action']['required_hint_level'] == 0
    assert not independent['action']['mastery_claim_allowed']


def test_followup_persists_cas_idempotency_and_current_source_revalidation(writing):
    service, rubric, snapshot = ready(writing)
    action = resolve(service, writing)
    db, sid, prompts, parent, _ = writing
    target = prompts.prepare(sid, track='pet', preferred_item_id=parent['diagnostic_item_id'], client_action_id='revision')['prompt']
    kwargs = dict(session_id=sid, parent_prompt_id=parent['prompt_id'], parent_item_version=parent['item_version'],
        prompt_id=target['prompt_id'], item_version=target['item_version'], kind='revision',
        binding_version=action['binding_version'], client_action_id='link')
    with db.connect() as c:
        with pytest.raises(ValueError, match='TRANSACTION_REQUIRED'): bind_writing_followup(c, **kwargs)
        c.execute('BEGIN IMMEDIATE')
        receipt = bind_writing_followup(c, **kwargs)
        assert bind_writing_followup(c, **kwargs) == receipt
        with pytest.raises(ValueError, match='ACTION_CONFLICT'):
            bind_writing_followup(c, **{**kwargs, 'kind':'independent_check'})
        c.commit()
    assert read(WritingTeachingService(db), writing) == read(service, writing)
    rubric.review(CHILD, snapshot['assessment_id'], expected_revision=1, client_action_id='retract', reviewer_id='parent',
                  reviewer_role='parent', operation='retract', reason='Changed assessment')
    assert resolve(service, writing)['reason'] == 'writing_feedback_review_withdrawn'
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError, match='FOLLOWUP_BINDING_CHANGED'):
            bind_writing_followup(c, **{**kwargs, 'client_action_id':'new-key'})
        c.rollback()
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 1


def test_missing_source_cross_session_and_terminal_cannot_prepare_new_writing(writing):
    service, _, _ = ready(writing)
    with pytest.raises(ValueError):
        service.read('other-session', prompt_id=writing[3]['prompt_id'], item_version=writing[3]['item_version'])
    with writing[0].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id='diag-pet-writing-2'")
        c.commit()
    assert resolve(service, writing, 'independent_check', 'diag-pet-writing-2')['status'] == 'blocked'
    with writing[0].connect() as c:
        c.execute("UPDATE daily_session SET state='completed'")
        c.commit()
    assert resolve(service, writing)['reason'] == 'writing_session_not_active'


def test_real_evaluator_candidate_can_teach_without_parent_review_but_not_certify(writing):
    import asyncio
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from uuid import uuid4
    from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
    from home_study_agent.model_gateway import ModelResult
    from home_study_agent.repository import StudyRepository
    from home_study_agent.learning_rubric import RubricEvidenceService
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from test_formative_evaluation_projection import objective as bound_objective
    db, sid, _, p, _ = writing
    original = capture(writing)
    scored = answer(writing)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind(c, writing, original)
        c.commit()
    rubric = RubricEvidenceService(db)
    rubric.initialize()
    PrivacyLifecycleService(db,db.path.parent).initialize()
    scope=bound_objective(db,sid,p,scored['attempt'])
    candidate = rubric.diagnostic_candidate_input(CHILD, scored['attempt']['evidence_id'])
    calls = []

    class NoNetworkGateway:
        async def complete(self, request):
            calls.append(request)
            ctx = json.loads(request.messages[1]['content'])
            result = {'understanding':'independent', 'confidence':.99,
                'evidence_summary':'Controlled model fixture', 'spoken_feedback':'Check that the reader can find the meeting time.',
                'next_check':'An unreviewed model question must not be dispatched',
                'rubric_dimensions':[dict(dimension_id=d['dimension_id'], score=2,
                    response_quotes=[ctx['response']], rationale='Check the task detail against this original sentence')
                    for d in ctx['rubric']['dimensions']]}
            return ModelResult(content=json.dumps(criterion_reply(ctx, result)),
                provider='fixture', model='fixture', latency_ms=1)

    evaluator = LearningOutcomeEvaluator(db, StudyRepository(db), NoNetworkGateway(),
        SimpleNamespace(model_provider='fixture', model_name='fixture'))
    objective=scope['objective_id']
    turn, plan, step = [uuid4().hex for _ in range(3)]
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id)
            VALUES(?,?,?,?,'learning.evaluate_response','','{}','fixture',?,?)""", (turn,CHILD,sid,TEXT,now,turn))
        c.execute("INSERT INTO agent_plan VALUES(?,?,?,?,?,'fixture','{}','{}','planned',?,?)", (plan,objective,CHILD,sid,turn,now,now))
        c.execute("""INSERT INTO agent_step VALUES(?,?,0,'learning.evaluate_response','1','local_model','medium',?,
            '[]','planned',?,NULL,NULL,?,?)""", (step,plan,json.dumps(candidate['arguments']),step,now,now))
        c.commit()
    evaluation = asyncio.run(evaluator.evaluate(turn))['evaluation']
    assert evaluation['outcome_level'] not in {'independent','transferred','retained'}
    service = WritingTeachingService(db)
    service.initialize()
    view = read(service, writing)
    assert view['status'] == 'feedback_ready', view
    assert view['feedback']['authority'] == 'uncalibrated_model_formative'
    assert not view['automatic_certification']
    assert resolve(service, writing)['status'] == 'ready'
    assert resolve(service, writing, 'independent_check')['status'] == 'blocked'
    assert read(WritingTeachingService(db), writing) == view
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 1
    assert len(calls) == 1


@pytest.mark.parametrize('hint', [0,1])
def test_actual_revised_answer_cannot_erase_recorded_support(writing, hint):
    from home_study_agent.writing_response_origin import bind_written_attempt
    db,sid,prompts,parent,originals = writing
    service,_,_ = ready(writing)
    resolved = resolve(service,writing)
    target = prompts.prepare(sid,track='pet',preferred_item_id=parent['diagnostic_item_id'],client_action_id='revision')['prompt']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind_writing_followup(c,session_id=sid,parent_prompt_id=parent['prompt_id'],parent_item_version=parent['item_version'],
            prompt_id=target['prompt_id'],item_version=target['item_version'],kind='revision',binding_version=resolved['binding_version'],client_action_id='bind-revision')
        c.commit()
    original = originals.capture_native_response(sid,prompt_id=target['prompt_id'],item_version=target['item_version'],text=TEXT,client_action_id='revised-original')
    answer = prompts.answer(sid,prompt_id=target['prompt_id'],item_version=target['item_version'],answer=TEXT,client_action_id='revised-answer',hint_level=hint)
    args = dict(child_id=CHILD,session_id=sid,prompt_id=target['prompt_id'],item_version=target['item_version'],
        attempt_id=answer['attempt']['attempt_id'],original_artifact_id=original['artifact_id'])
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        # The actual answer ingress now records revision support even when a
        # caller asks for zero. Verify that fact, then retain the consumer veto.
        saved=c.execute('SELECT hint_level FROM learning_evidence WHERE evidence_id=?',
            (answer['attempt']['evidence_id'],)).fetchone()[0]
        assert saved >= 1
        assert bind_written_attempt(c,**args)['kind'] == 'native_editor'
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        c.execute('UPDATE learning_evidence SET hint_level=0 WHERE evidence_id=?',(answer['attempt']['evidence_id'],))
        with pytest.raises(ValueError,match='REVISION_ASSISTANCE_MUST_BE_RECORDED'):bind_written_attempt(c,**args)
        c.rollback()
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 2
    recovered = read(WritingTeachingService(db),writing)
    assert recovered['followups'] == [dict(prompt_id=target['prompt_id'],item_version=target['item_version'],kind='revision',
        status='answered',assessment_id=recovered['assessment_id'])]
