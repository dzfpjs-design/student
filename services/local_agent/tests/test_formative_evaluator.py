"""Actual evaluator request/persistence with a no-network model double."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator,LearningEvaluationUnknownError,InvalidLearningEvaluationError
from home_study_agent.model_gateway import ModelResult
from home_study_agent.repository import StudyRepository,utc_now
from home_study_agent.writing_teaching import WritingTeachingService
from test_formative_evaluation_projection import projection,writing
from formative_response_fixture import criterion_reply


@pytest.fixture
def evaluation(projection):
    writing,scope=projection
    db,sid,_,p,_=writing
    turn,plan,step=[uuid4().hex for _ in range(3)]
    now=utc_now()
    with db.connect() as c:
        attempt=c.execute('SELECT * FROM diagnostic_attempt').fetchone()
        item=c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',(p['diagnostic_item_id'],)).fetchone()
        args=dict(evidence_id=attempt['evidence_id'],response=attempt['submitted_answer'],question_asked=item['prompt'],subject='英语')
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
            orchestrator_version,created_at,client_action_id) VALUES(?,?,?,'','local_evaluation','',?,'fixture',?,?)""",
            (turn,scope['child_id'],sid,json.dumps({'source':'local_evaluation','local_only':True,'cloud_transmission_allowed':False}),now,turn))
        c.execute("INSERT INTO agent_plan VALUES(?,?,?,?,?,'fixture','{\"execution_method\":\"formative_evaluation\"}','{}','planned',?,?)",(plan,scope['objective_id'],scope['child_id'],sid,turn,now,now))
        c.execute("INSERT INTO agent_step VALUES(?,?,0,'learning.evaluate_response','1','local_model','medium',?,'[]','planned',?,NULL,NULL,?,?)",
            (step,plan,json.dumps(args),step,now,now))
        c.commit()
    calls=[]

    class Gateway:
        before_return=None
        fault=None

        async def complete(self,request):
            calls.append(request)
            payload=json.loads(request.messages[1]['content'])
            assert set(payload)=={'question','response','rubric'}
            result=dict(understanding='independent',confidence=.99,evidence_summary='Synthetic formative evidence',
                spoken_feedback='Make the meeting time easier to find.',next_check='Revise the meeting time sentence.',
                rubric_dimensions=[dict(dimension_id=d['dimension_id'],score=2,response_quotes=[payload['response']],
                    rationale='Grounded in the original response') for d in payload['rubric']['dimensions']],
                criterion_evidence=[dict(criterion=point,response_quote=payload['response']) for point in payload['rubric']['task_points']])
            from home_study_agent.learning_outcome_evaluator import FORMATIVE_SYSTEM_PROMPT
            if request.messages[0]['content']==FORMATIVE_SYSTEM_PROMPT:
                result=criterion_reply(payload,result)
            if self.fault=='quote':result['rubric_dimensions'][0]['response_quotes']=['invented words']
            elif self.fault=='joined_quote':
                text=payload['response']
                result['rubric_dimensions'][0]['response_quotes']=[text[:5]+text[-5:]]
            elif self.fault=='rubric_as_source':
                result['rubric_dimensions'][0]['source_quotes']=[payload['rubric']['dimensions'][0]['criterion']]
            elif self.fault=='missing_dimension':result['rubric_dimensions'].pop()
            elif self.fault=='criterion':result['criterion_checks'][0]['criterion_index']=999
            elif self.fault=='nonfinite':result['rubric_dimensions'][0]['score']=float('nan')
            if self.before_return:self.before_return()
            return ModelResult(content=json.dumps(result),provider='fixture',model='fixture',latency_ms=1,
                prompt_tokens=950,completion_tokens=900)

    gateway=Gateway()
    service=LearningOutcomeEvaluator(db,StudyRepository(db),gateway,SimpleNamespace(model_provider='fixture',model_name='fixture'))
    return writing,scope,service,gateway,calls,turn


def test_projected_evaluator_real_save_restart_and_revision_without_parent_review(evaluation):
    writing,scope,service,gateway,calls,turn=evaluation
    result=asyncio.run(service.evaluate(turn))
    assert result['evaluation']['formative_only'] and not result['evaluation']['automatic_certification']
    assert result['evaluation']['certification_status']=='candidate'
    assert result['evaluation']['outcome_level'] not in {'independent','transferred','retained'}
    assert len(calls)==1
    serialized=json.dumps(calls[0].messages,ensure_ascii=False)
    assert all(secret not in serialized for secret in ['PRIVATE',scope['child_id'],scope['prompt_id'],scope['objective_id'],'local_audit'])
    assert set(calls[0].context_manifest)=={'purpose','raw_audio_or_video','fields','redaction_count'}
    restarted=LearningOutcomeEvaluator(writing[0],StudyRepository(writing[0]),gateway,service.settings)
    assert asyncio.run(restarted.evaluate(turn))==result
    assert len(calls)==1
    teaching=WritingTeachingService(writing[0])
    teaching.initialize()
    view=teaching.read(writing[1],prompt_id=scope['prompt_id'],item_version=scope['item_version'])
    assert view['status']=='feedback_ready',view
    assert view['feedback']['authority']=='uncalibrated_model_formative'
    assert teaching.resolve_next_action(writing[1],prompt_id=scope['prompt_id'],item_version=scope['item_version'],kind='revision')['status']=='ready'
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
        saved=json.loads(c.execute('SELECT record_json FROM learning_evaluation_execution').fetchone()[0])
        assert saved['formative_projection']['local_audit']['prompt_id']==scope['prompt_id']


@pytest.mark.parametrize('fault',['privacy','source','original','arguments'])
def test_revocation_during_cloud_work_rejects_late_result_no_candidate(evaluation,fault):
    writing,_,service,gateway,calls,turn=evaluation
    def change():
        with writing[0].connect() as c:
            if fault=='privacy':c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
            elif fault=='source':c.execute("UPDATE diagnostic_item SET prompt='changed' WHERE diagnostic_item_id='diag-pet-writing-1'")
            elif fault=='original':c.execute("UPDATE capture_artifact SET status='withdrawn'")
            else:c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.response','late replacement') WHERE capability_name='learning.evaluate_response'")
            c.commit()
    gateway.before_return=change
    with pytest.raises(LearningEvaluationUnknownError):asyncio.run(service.evaluate(turn))
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0]==0
    with pytest.raises(LearningEvaluationUnknownError):asyncio.run(service.evaluate(turn))
    assert len(calls)==1


@pytest.mark.parametrize('fault',['quote','joined_quote','rubric_as_source','missing_dimension','criterion','nonfinite'])
def test_model_result_needs_exact_rubric_and_original_quotes(evaluation,fault):
    writing,_,service,gateway,calls,turn=evaluation
    gateway.fault=fault
    with pytest.raises(LearningEvaluationUnknownError):asyncio.run(service.evaluate(turn))
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0
        assert tuple(c.execute('SELECT status,prompt_tokens,completion_tokens FROM model_call').fetchone())==('failed',950,900)
        assert c.execute("SELECT status FROM model_call").fetchone()[0]=='failed'


def test_source_or_consent_block_before_call_is_not_feedback(evaluation):
    writing,_,service,_,calls,turn=evaluation
    with writing[0].connect() as c:
        c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
        c.commit()
    with pytest.raises(InvalidLearningEvaluationError,match='LOCAL_REQUIRED'):
        asyncio.run(service.evaluate(turn))
    assert not calls
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM learning_evaluation_execution').fetchone()[0]==0


def test_truncated_model_answer_is_consumed_not_scored_or_repeated(evaluation):
    from home_study_agent.model_gateway import ModelResponseError
    writing,_,service,gateway,calls,turn=evaluation
    async def truncated(request):
        calls.append(request)
        raise ModelResponseError('MODEL_OUTPUT_TRUNCATED',response_received=True,
            prompt_tokens=950,completion_tokens=2400,latency_ms=1200)
    gateway.complete=truncated
    with pytest.raises(LearningEvaluationUnknownError):asyncio.run(service.evaluate(turn))
    with writing[0].connect() as c:
        row=c.execute('SELECT status,error_code,prompt_tokens,completion_tokens,latency_ms FROM model_call').fetchone()
        assert tuple(row)==('failed','MODEL_OUTPUT_TRUNCATED',950,2400,1200)
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
    with pytest.raises(LearningEvaluationUnknownError):asyncio.run(service.evaluate(turn))
    assert len(calls)==1


def test_concurrent_evaluation_has_one_audio_receipt_and_no_persisted_play_permission(evaluation):
    writing,_,service,gateway,calls,turn=evaluation
    complete=gateway.complete
    async def delayed(request):
        await asyncio.sleep(.05)
        return await complete(request)
    gateway.complete=delayed
    async def concurrent():
        return await asyncio.gather(*(service.evaluate(turn,include_delivery=True) for _ in range(2)))
    results=asyncio.run(concurrent())
    assert sorted(r['replay_audio_allowed'] for r in results)==[False,True]
    assert len(calls)==1
    with writing[0].connect() as c:
        saved=json.loads(c.execute('SELECT result_json FROM learning_evaluation_execution').fetchone()[0])
        assert 'replay_audio_allowed' not in saved
    restarted=LearningOutcomeEvaluator(writing[0],StudyRepository(writing[0]),gateway,service.settings)
    assert asyncio.run(restarted.evaluate(turn,include_delivery=True))['replay_audio_allowed'] is False
    assert len(calls)==1


def test_legacy_saved_response_recovers_without_upgrade_or_second_call(evaluation,monkeypatch):
    writing,_,service,gateway,calls,turn=evaluation
    claim=service._claim
    def pre_upgrade_claim(record):
        value=claim(record)
        assert value['acquired']
        stored=json.loads(value['record_json'])
        stored.pop('formative_protocol_version')
        value['record_json']=json.dumps(stored)
        with writing[0].connect() as c:
            c.execute('UPDATE learning_evaluation_execution SET record_json=?',(value['record_json'],))
            c.commit()
        return value
    monkeypatch.setattr(service,'_claim',pre_upgrade_claim)
    def interrupted(*args):raise RuntimeError('synthetic pre-upgrade interruption after receipt')
    monkeypatch.setattr(service,'_persist',interrupted)
    with pytest.raises(LearningEvaluationUnknownError):asyncio.run(service.evaluate(turn))
    restarted=LearningOutcomeEvaluator(writing[0],StudyRepository(writing[0]),gateway,service.settings)
    result=restarted.recover_ready(turn)
    assert result['evaluation']['formative_only'] and 'criterion_review' not in result['evaluation']
    assert len(calls)==1
    with writing[0].connect() as c:
        record=json.loads(c.execute('SELECT record_json FROM learning_evaluation_execution').fetchone()[0])
        assert 'formative_protocol_version' not in record and 'criterion_review_receipt' not in record
    assert asyncio.run(restarted.evaluate(turn))['evaluation']['formative_only']
    assert len(calls)==1


def test_new_request_rejects_legacy_reply_instead_of_downgrading_contract(evaluation):
    writing,_,service,gateway,calls,turn=evaluation
    async def legacy(request):
        calls.append(request)
        return ModelResult(content=json.dumps(dict(understanding='independent',confidence=.9,
            spoken_feedback='Correct.',evidence_summary='Old protocol.',next_check='Try again.')),
            provider='fixture',model='fixture',latency_ms=1,prompt_tokens=50,completion_tokens=40)
    gateway.complete=legacy
    with pytest.raises(LearningEvaluationUnknownError) as rejected:asyncio.run(service.evaluate(turn))
    assert 'FORMATIVE_CHECK_FIELDS' in str(rejected.value.__cause__)
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0
        assert tuple(c.execute('SELECT prompt_tokens,completion_tokens FROM model_call').fetchone())==(50,40)
    assert len(calls)==1


@pytest.mark.parametrize('field',['criterion_review','criterion_evidence','spoken_feedback'])
def test_changed_saved_criterion_receipt_is_not_replayed_as_feedback(evaluation,field):
    writing,_,service,gateway,calls,turn=evaluation
    asyncio.run(service.evaluate(turn))
    with writing[0].connect() as c:
        result=json.loads(c.execute('SELECT result_json FROM learning_evaluation_execution').fetchone()[0])
        if field=='criterion_review':result['evaluation'][field]['checks'][0]['status']='contradicted'
        elif field=='criterion_evidence':result['evaluation'][field]=[]
        else:result['evaluation'][field]='You have mastered every school subject.'
        c.execute('UPDATE learning_evaluation_execution SET result_json=?',(json.dumps(result),))
        c.commit()
    restarted=LearningOutcomeEvaluator(writing[0],StudyRepository(writing[0]),gateway,service.settings)
    recovered=asyncio.run(restarted.evaluate(turn))
    assert recovered['evaluation']['feedback_status']=='blocked'
    assert recovered['evaluation']['feedback_reason']=='feedback_criterion_review_changed'
    assert recovered['evaluation']['spoken_feedback']==''
    assert len(calls)==1


def test_saved_model_result_recovers_without_new_call_and_rechecks_in_write_transaction(evaluation,monkeypatch):
    writing,_,service,gateway,calls,turn=evaluation
    def crash(*args):raise RuntimeError('synthetic crash after saved model reply')
    monkeypatch.setattr(service,'_persist',crash)
    with pytest.raises(LearningEvaluationUnknownError):asyncio.run(service.evaluate(turn))
    with writing[0].connect() as c:
        assert c.execute('SELECT state FROM learning_evaluation_execution').fetchone()[0]=='response_ready'
    restarted=LearningOutcomeEvaluator(writing[0],StudyRepository(writing[0]),gateway,service.settings)
    assert restarted.recover_ready(turn)['evaluation']['formative_only']
    assert len(calls)==1


def test_requested_projection_missing_prompt_never_uses_legacy_free_text_route(evaluation):
    writing,_,service,_,calls,turn=evaluation
    with writing[0].connect() as c:
        c.execute('DELETE FROM diagnostic_prompt')
        c.commit()
    with pytest.raises(InvalidLearningEvaluationError,match='LOCAL_REQUIRED'):
        asyncio.run(service.evaluate(turn))
    assert not calls


@pytest.mark.parametrize('fault',['withdrawal','saved_quote'])
def test_recovery_revalidates_sources_and_saved_model_receipt_in_final_transaction(evaluation,monkeypatch,fault):
    writing,_,service,gateway,calls,turn=evaluation
    def crash(*args):raise RuntimeError('synthetic crash before persist')
    monkeypatch.setattr(service,'_persist',crash)
    with pytest.raises(LearningEvaluationUnknownError):asyncio.run(service.evaluate(turn))
    with writing[0].connect() as c:
        if fault=='withdrawal':c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
        else:c.execute("UPDATE learning_evaluation_execution SET evaluation_json=json_set(evaluation_json,'$.rubric_dimensions[0].response_quotes[0]','invented after crash')")
        c.commit()
    restarted=LearningOutcomeEvaluator(writing[0],StudyRepository(writing[0]),gateway,service.settings)
    with pytest.raises(LearningEvaluationUnknownError) as rejected:restarted.recover_ready(turn)
    assert isinstance(rejected.value.__cause__,InvalidLearningEvaluationError)
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0]==0
    assert len(calls)==1


def test_new_task_without_original_runtime_command_cannot_authorize_cloud_evaluation(evaluation):
    from home_study_agent.formative_evaluation_projection import build_formative_projection
    writing,scope,service,_,calls,turn=evaluation
    asyncio.run(service.evaluate(turn))
    db,sid,prompts,parent,originals=writing
    teaching=WritingTeachingService(db)
    teaching.initialize()
    next_action=teaching.resolve_next_action(sid,prompt_id=parent['prompt_id'],item_version=parent['item_version'],
        kind='independent_check',diagnostic_item_id='diag-pet-writing-2')
    assert next_action['status']=='ready',next_action
    target=prompts.prepare(sid,track='pet',preferred_item_id='diag-pet-writing-2',client_action_id='independent-source',
        writing_followup=dict(parent_prompt_id=parent['prompt_id'],parent_item_version=parent['item_version'],
            kind='independent_check',binding_version=next_action['binding_version']))['prompt']
    text='This is a separate synthetic manuscript for the newly published task, not the previous email.'
    original=originals.capture_native_response(sid,prompt_id=target['prompt_id'],item_version=target['item_version'],text=text,client_action_id='new-original')
    prompts.answer(sid,prompt_id=target['prompt_id'],item_version=target['item_version'],answer=text,
        writing_original_artifact_id=original['artifact_id'],client_action_id='new-answer')
    tid,pid,step=[uuid4().hex for _ in range(3)]
    now=utc_now()
    with db.connect() as c:
        goal=c.execute('SELECT * FROM agent_objective WHERE objective_id=?',(scope['objective_id'],)).fetchone()
        binding=dict(child_id=scope['child_id'],session_id=sid,objective_id=scope['objective_id'],prompt_id=target['prompt_id'],
            item_version=target['item_version'],phase=goal['phase'],success_criteria=json.loads(goal['success_criteria_json']),
            local_only=True,cloud_transmission_allowed=False)
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
            orchestrator_version,created_at,client_action_id) VALUES(?,?,?,'','diagnostic.start','',?,'fixture',?,?)""",
            (tid,scope['child_id'],sid,json.dumps({'local_only':True,'cloud_transmission_allowed':False}),now,tid))
        c.execute("INSERT INTO agent_plan VALUES(?,?,?,?,?,'fixture','{}',?,'completed',?,?)",(pid,scope['objective_id'],scope['child_id'],sid,tid,
            json.dumps({'local_learning_binding':binding}),now,now))
        c.execute("INSERT INTO agent_step VALUES(?,?,0,'diagnostic.start','1','server','low','{}','[]','completed',?,NULL,NULL,?,?)",(step,pid,step,now,now))
        c.commit()
        projected=build_formative_projection(c,**{**scope,'prompt_id':target['prompt_id'],'item_version':target['item_version']})
        assert projected['status']=='local_required',projected
        assert projected['reason']=='WRITING_FOLLOWUP_ORIGINAL_COMMAND_REQUIRED'
        assert projected['payload'] is None
        assert not projected['automatic_certification']
    assert len(calls)==1
