"""Restricted control never consumes an answer or repeats an evaluation."""
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_learning_outcome_evaluator import evaluation_fixture
from test_learning_outcome_mastery_gate import gate, flow


@contextmanager
def host(tmp_path, *, role='control', control='evaluation.status', hook=None, completed=False, original=None):
    if original:
        db,sid,evaluator = original
    else:
        db, sid, evaluator, _, _ = evaluation_fixture(tmp_path)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
        model_provider='qwen', model_api_key='synthetic-no-network', model_name='isolated', voice_intent_model_name='isolated')
    requests = []
    def transport(request):
        body = json.loads(request.content)
        data = json.loads(body['messages'][-1]['content'])
        assert set(data) == {'transcript', 'state'}
        assert all(type(v) is bool for v in data['state'].values())
        assert '正弦' not in json.dumps(body, ensure_ascii=False)
        assert 'eval-action' not in request.content.decode()
        requests.append(data)
        if hook:
            hook(db)
        return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps({
            'role': role, 'control': control, 'quote': data['transcript'], 'confidence': .99})}}]})
    with TestClient(create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport))),
                    raise_server_exceptions=True) as client:
        if original:
            pass
        elif completed:
            asyncio.run(evaluator.evaluate('eval'))
        else:
            evaluator._claim(evaluator._load('eval'))
        yield db, sid, client, requests


def ask(client, sid, text='帮我看看刚才评价怎么样了？', *, key='restricted-control', reference='eval'):
    return client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': text, 'client_action_id':key,
        **({'pending_evaluation_turn_id':reference} if reference is not None else {})})


def facts(db):
    with db.connect() as c:
        return {name: [tuple(row) for row in c.execute('SELECT * FROM ' + name).fetchall()]
                for name in ('diagnostic_attempt','learning_evidence','rubric_assessment','learning_evaluation_execution')}


@pytest.mark.parametrize('control,action', [('evaluation.status','none'), ('device.status','query_device_status'),
    ('planning.describe','describe_plan'), ('parent.request','find_parent')])
def test_real_http_natural_queries_use_exact_pending_receipt_and_no_scoring(tmp_path, control, action):
    with host(tmp_path, control=control) as (db, sid, client, requests):
        before = facts(db)
        result = ask(client, sid)
        assert result.status_code == 200, result.text
        value = result.json()
        assert value['action']['type'] == action
        assert value['action']['pending_evaluation_control'] == {'version':'pending_evaluation_control_v1',
            'evaluation_turn_id':'eval','mode':'control_only'}
        assert value['after_execution'] == 'stop'
        if control == 'evaluation.status':
            assert value['action']['pending_evaluation_query'] == {'turn_id':'eval'}
            execution = client.post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
            assert execution.status_code == 200, execution.text
        assert ask(client, sid).json()['agent_turn_id'] == value['agent_turn_id']
        assert len(requests) == 1 and facts(db) == before


@pytest.mark.parametrize('role,control', [('answer',None),('mixed',None),('unknown',None),
    ('control','writing.confirm_original'),('control','diagnostic.replay')])
def test_noncontrols_and_scoring_adjacent_controls_cannot_escape_restricted_lane(tmp_path, role, control):
    with host(tmp_path, role=role, control=control) as (db, sid, client, requests):
        before = facts(db)
        result = ask(client, sid, '这句话只核对之前的请求')
        assert result.status_code == 200, result.text
        value = result.json()
        assert value['action']['type'] == 'none'
        assert value['runtime_capability'] == 'conversation.respond'
        assert value['after_execution'] == 'stop'
        assert 'pending_evaluation_query' not in value['action']
        assert facts(db) == before


@pytest.mark.parametrize('reference', ['absent','eval-action'])
def test_bad_reference_is_rejected_before_arrival_or_model(tmp_path, reference):
    with host(tmp_path) as (db, sid, client, requests):
        before = facts(db)
        result = ask(client, sid, reference=reference)
        assert result.status_code == 409, result.text
        assert requests == [] and facts(db) == before
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM learning_input_arrival').fetchone()[0] == 0


@pytest.mark.parametrize('reference', [None,'another-original'])
def test_same_key_cannot_change_restricted_mode_or_original_evaluation(tmp_path, reference):
    with host(tmp_path) as (db, sid, client, requests):
        assert ask(client, sid).status_code == 200
        result = ask(client, sid, reference=reference)
        assert result.status_code == 409, result.text
        assert len(requests) == 1


def test_delayed_completed_receipt_remains_queryable_after_restart_without_regrading(tmp_path):
    from home_study_agent.pending_evaluation_control import validate_pending_evaluation_reference
    from home_study_agent.database import Database
    with host(tmp_path, completed=True) as (db, sid, client, requests):
        before = facts(db)
        first = ask(client, sid)
        assert first.status_code == 200, first.text
        with db.connect() as c:
            c.execute('BEGIN')
            changed = c.total_changes
            original = validate_pending_evaluation_reference(c, sid, 'child-runtime', 'eval')
            assert c.in_transaction and c.total_changes == changed
        with Database(db.path).connect() as c:
            assert validate_pending_evaluation_reference(c, sid, 'child-runtime', 'eval') == original
        assert ask(client, sid).json()['agent_turn_id'] == first.json()['agent_turn_id']
        assert facts(db) == before and len(requests) == 1


@pytest.mark.parametrize('fault', ['cancel','source_deleted','response_changed','consent_regranted'])
def test_real_execute_revalidates_original_reference_and_never_uses_new_authority(tmp_path, fault):
    with host(tmp_path) as (db, sid, client, requests):
        control = ask(client, sid)
        assert control.status_code == 200, control.text
        if fault == 'consent_regranted':
            privacy = PrivacyLifecycleService(db,tmp_path)
            privacy.update_preferences({'cloud_model_text_allowed': False})
            privacy.update_preferences({'cloud_model_text_allowed': True})
        else:
            with db.connect() as c:
                sql = {'cancel':"UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id='eval'",
                    'source_deleted':"UPDATE agent_step SET error_code='SOURCE_DELETED' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id='eval')",
                    'response_changed':"UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.response','changed original') WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id='eval')"}[fault]
                c.execute(sql)
                c.commit()
        before = facts(db)
        result = client.post(f"/v1/agent-turns/{control.json()['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
        assert result.json()['code'].startswith('PENDING_EVALUATION_CONTROL_')
        replay = ask(client, sid)
        assert replay.status_code == 409, replay.text
        assert facts(db) == before and len(requests) == 1


def test_cancelled_during_semantic_request_cannot_persist_control_or_score(tmp_path):
    def cancel(db):
        with db.connect() as c:
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id='eval'")
            c.commit()
    with host(tmp_path, hook=cancel) as (db, sid, client, requests):
        before = facts(db)
        result = ask(client, sid)
        assert result.status_code == 409, result.text
        assert len(requests) == 1 and facts(db) == before
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM agent_turn WHERE client_action_id='restricted-control'").fetchone()[0] == 0


def test_other_child_original_and_uncaptured_question_are_not_query_aliases(tmp_path):
    from home_study_agent.pending_evaluation_control import validate_pending_evaluation_reference
    from home_study_agent.agent_runtime import InvalidAgentPlanError
    from home_study_agent.repository import StudyRepository
    with host(tmp_path) as (db, sid, client, requests):
        with db.connect() as c:
            with pytest.raises(InvalidAgentPlanError,match='QUESTION_MISMATCH'):
                validate_pending_evaluation_reference(c,sid,'child-runtime','eval',question_reference={
                    'prompt_id':'not-the-evaluated-question','item_version':'version'})
        other = StudyRepository(db).switch_active_session_child(sid,'child-primary-4','other-child-session')['session_id']
        result = ask(client,other)
        assert result.status_code == 409, result.text
        assert requests == []


@pytest.mark.parametrize('text,risk', [('我现在有危险，请帮我找大人。','R3'), ('有人欺负我。','R2')])
def test_restricted_safety_is_immediate_local_with_cloud_disabled(tmp_path, text, risk):
    from test_voice_device_capability_contract import receipt
    with host(tmp_path) as (db, sid, client, requests):
        PrivacyLifecycleService(db,tmp_path).update_preferences({'cloud_model_text_allowed':False})
        before = facts(db)
        result = ask(client,sid,text)
        assert result.status_code == 200, result.text
        value = result.json()
        assert value['action'].get('risk_level') == risk
        assert value['action']['pending_evaluation_control']['evaluation_turn_id'] == 'eval'
        assert value['requires_control_admission']
        admission = client.post(f"/v1/sessions/{sid}/agent-turns/{value['agent_turn_id']}/control-admission")
        assert admission.status_code==200 and admission.json()['status']=='admitted',admission.text
        acknowledgement = receipt('safety.find_parent',{'parent_contact_prompted':True})
        acknowledgement['execution_receipt']['action_type'] = value['action']['type']
        false_claim = client.put(f"/v1/agent-turns/{value['agent_turn_id']}/outcome",json={
            'final_response':'已经联系到家长。','tool_status':'completed','result':acknowledgement})
        assert false_claim.status_code==409,false_claim.text
        with db.connect() as c:
            row=c.execute('SELECT response_text,tool_status FROM agent_turn WHERE agent_turn_id=?',
                          (value['agent_turn_id'],)).fetchone()
            assert tuple(row)==(value['response_text'],'pending')
        completed = client.put(f"/v1/agent-turns/{value['agent_turn_id']}/outcome",json={
            'final_response':value['response_text'],'tool_status':'completed','result':acknowledgement})
        assert completed.status_code == 200,completed.text
        assert ask(client,sid,text).json()['agent_turn_id'] == value['agent_turn_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM child_safety_event').fetchone()[0] == 1
        assert not requests and facts(db) == before


@pytest.mark.parametrize('explicit_playback', [True,False])
def test_restricted_overlap_never_classifies_assistant_words_as_new_control(tmp_path, explicit_playback):
    text = '请帮我看看刚才那一次评价的状态现在怎么样了。'
    with host(tmp_path) as (db,sid,client,requests):
        if explicit_playback:
            registration = client.post(f'/v1/sessions/{sid}/voice-playback',json={
                'playback_id':'pending-output','text':text,'event':'started'})
            assert registration.status_code == 200, registration.text
        else:
            with db.connect() as c:
                c.execute("UPDATE agent_turn SET response_text=?,created_at=? WHERE agent_turn_id='eval'",
                          (text,__import__('home_study_agent.repository',fromlist=['utc_now']).utc_now()))
                c.commit()
        before = facts(db)
        result = client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'transcript':text,'client_action_id':'overlap-original','pending_evaluation_turn_id':'eval',
            **({'playback_overlap_id':'pending-output'} if explicit_playback else {})})
        assert result.status_code == 200, result.text
        value = result.json()
        assert value['action'].get('source') == 'playback_overlap_clarification'
        assert value['action']['echo_source_verified'] is False
        assert value['action']['pending_evaluation_control']['evaluation_turn_id']=='eval'
        assert value['action']['type']=='none' and not requests and facts(db)==before


@pytest.mark.parametrize('quote', ['我说的是“关闭相机”这个词。','不要关闭相机。','假如我要求暂停会怎样？'])
def test_restricted_parse_does_not_turn_quoted_negated_or_hypothetical_requests_into_control(quote):
    from home_study_agent.voice_control_intent import VoiceControlIntent
    value = {'role':'control','control':'observation.pause','quote':quote,'confidence':.99}
    assert VoiceControlIntent.parse(json.dumps(value), quote, restricted=True)['status'] != 'control'


def test_concurrent_initial_entries_cannot_rebind_the_semantic_journal(tmp_path, monkeypatch):
    from home_study_agent.voice_control_intent import VoiceControlIntent
    from test_learning_outcome_evaluator import persist_evaluation_turn
    from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
    entered, first_entered, release = [], Event(), Event()
    original = VoiceControlIntent.resolve
    async def simultaneous(self, **kwargs):
        entered.append(kwargs['binding']['pending_evaluation']['evaluation_turn_id'])
        first_entered.set()
        assert await asyncio.to_thread(release.wait,10)
        return await original(self,**kwargs)
    with host(tmp_path) as (db,sid,client,requests):
        persist_evaluation_turn(db,sid,'eval-second','second-eval-action','continue','diagnose')
        evaluator = LearningOutcomeEvaluator(db,None,None,None,initialize=False)
        evaluator._claim(evaluator._load('eval-second'))
        monkeypatch.setattr(VoiceControlIntent,'resolve',simultaneous)
        before = facts(db)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(ask,client,sid,reference='eval')
            try:
                assert first_entered.wait(10)
                second = pool.submit(ask,client,sid,reference='eval-second').result(timeout=10)
            finally:
                release.set()
            results = [first.result(timeout=15), second]
        assert sorted(r.status_code for r in results)==[200,409], [r.text for r in results]
        rejected = next(r for r in results if r.status_code==409)
        assert rejected.json()['code']=='AGENT_HANDOFF_SOURCE_CONFLICT'
        winner = next(r for r in results if r.status_code==200).json()
        origin = winner['action']['pending_evaluation_control']['evaluation_turn_id']
        assert ask(client,sid,reference=origin).json()['agent_turn_id']==winner['agent_turn_id']
        assert entered==['eval'] and len(requests)==1 and facts(db)==before


@pytest.mark.parametrize('fault', ['source','withdrawal'])
def test_real_local_legacy_attempt_without_prompt_has_exact_readonly_evaluation_scope(gate,tmp_path,fault):
    result,evidence,turn = gate.run(local=True)
    assert result['evaluation']['rubric_assessment']['source_kind']=='local_criteria_mapping'
    with host(tmp_path,original=(gate.db,gate.sid,gate.evaluator)) as (db,sid,client,requests):
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE attempt_id=?',(evidence['attempt_id'],)).fetchone()[0]==0
        before = facts(db)
        first = ask(client,sid,reference=turn)
        assert first.status_code==200,first.text
        assert client.post(f"/v1/agent-turns/{first.json()['agent_turn_id']}/execute").status_code==200
        assert facts(db)==before and len(requests)==1
        if fault=='source':
            with db.connect() as c:
                c.execute('UPDATE diagnostic_item SET prompt=prompt||? WHERE diagnostic_item_id=?',
                          (' revised',evidence['diagnostic_item_id']))
                c.commit()
        else:
            from home_study_agent.mistake_book import MistakeBookService
            book = MistakeBookService(db)
            with db.connect() as c:
                node = c.execute('SELECT knowledge_node_id FROM learning_evidence WHERE evidence_id=?',
                                 (evidence['evidence_id'],)).fetchone()[0]
            withdrawal = book.capture(child_id=evidence['child_id'],knowledge_node_id=node,
                subject='数学',title='Correction',prompt_summary='Withdraw this evidence',submitted_answer='wrong',
                source_type='manual',source_ref=None,confidence=1,client_action_id='withdraw-pending-source',
                source_evidence_id=evidence['evidence_id'])
            book.confirm(withdrawal['mistake_id'],evidence['child_id'],'exclude','withdraw-pending-confirm')
        stale = ask(client,sid,reference=turn,key='after-source-change')
        assert stale.status_code==409,stale.text
        assert len(requests)==1


@pytest.mark.parametrize('cancelled', [False,True])
def test_pending_document_stop_is_local_and_cannot_evaluate_its_followup_clause(tmp_path,cancelled):
    from test_document_stop_continuation_http import complete_stop
    from home_study_agent.document_monitoring import DocumentMonitoringService
    with host(tmp_path) as (db,sid,client,requests):
        with db.connect() as c:
            # An active study session is the monitor-start fixture prerequisite.
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
            if cancelled:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id='eval'")
            c.commit()
        started=client.post(f'/v1/sessions/{sid}/document-monitor/start',json={'client_action_id':'pending-monitor-start'})
        assert started.status_code==200,started.text
        PrivacyLifecycleService(db,tmp_path).update_preferences({'cloud_model_text_allowed':False})
        before=facts(db)
        stopped=ask(client,sid,'暂停高拍仪，我刚才写了什么？')
        assert stopped.status_code==200,stopped.text
        value=stopped.json()
        assert value['action']['type']=='control_document_monitor' and value['action']['command']=='pause'
        assert value['after_execution']=='stop'
        complete_stop(client,sid,value['agent_turn_id'])
        assert DocumentMonitoringService(db).status(sid)['status']=='paused'
        assert client.get(f'/v1/sessions/{sid}/agent-continuation').json()['status']!='pending'
        assert facts(db)==before and not requests
