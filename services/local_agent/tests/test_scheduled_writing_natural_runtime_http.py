"""Production orchestrator and real Gateway transport boundary; synthetic data only."""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import json

import httpx
import pytest

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.writing_response_origin import WritingOriginalService
from test_cloud_first_agent import Planner
from test_pet_task_execution import CHILD
from test_writing_source_publication import prepared, publish
from home_study_agent.repository import StudyRepository
from home_study_agent.learning_state import LearningStateService
from test_school_review_start_integrity_http import _http_client
from home_study_agent.app import create_app
from test_writing_question_authorization import LICENSE

TEXT = ('Dear Ben, our school concert was in the hall on Friday. I enjoyed the violin performance '
        'because the music was cheerful. Would you like to come with me to our next concert in June? Best wishes, Mia.')


def post(client, path, body):
    response = client.post(path,json=body)
    assert response.status_code == 200, (path,response.status_code,response.text)
    return response.json()


class ControlledTransport:
    def __init__(self, objective_type='execution'):
        self.quote = '开始今天的写作任务'
        self.payload = Planner('task.start', {'start_selector': {
            'subject': None,
            'task_kind': 'writing',
            'source_type': None,
            'target_quote': self.quote,
        }}, self.quote, objective_type=objective_type).payload
        if objective_type == 'learning':
            self.payload['objective']['phase'] = 'diagnose'
        self.calls = []
        self.control = None

    async def __call__(self, request):
        assert request.url.host == 'controlled.invalid'
        body = json.loads(request.content)
        self.calls.append(body)
        data = json.loads(body['messages'][-1]['content'])
        if set(data) == {'transcript','state'}:
            value = dict(role='control',control=self.control,quote=data['transcript'],confidence=.95)
            if self.control == 'task.resume':
                value['selector'] = dict(subject=None,task_kind=None,relative_order=None)
        elif set(data) == {'question','response','rubric'}:
            value = dict(understanding='partial',confidence=.5,evidence_summary='Synthetic formative evidence',
                spoken_feedback='请把见面的时间写得更清楚一些。',next_check='核对原稿中的见面时间。',
                rubric_dimensions=[dict(dimension_id=d['dimension_id'],score=2,response_quotes=[data['response']],
                    rationale='Synthetic bounded response') for d in data['rubric']['dimensions']],
                criterion_evidence=[dict(criterion=p,response_quote=data['response']) for p in data['rubric']['task_points']])
            from formative_response_fixture import criterion_reply
            value=criterion_reply(data,value)
        else:
            value = self.payload
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(value,ensure_ascii=False)}}]})


@contextmanager
def scheduled(tmp_path, objective_type='execution'):
    base,supply,spec,_ = prepared(tmp_path)
    published = publish(supply,spec)
    repo = StudyRepository(base)
    sid = repo.start_session(CHILD,'natural-session')['session_id']
    with base.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
        c.execute('''UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,
            task_segment_minutes=30,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?''',(CHILD,))
        c.execute('''UPDATE diagnostic_item SET status='retired' WHERE knowledge_node_id IN
            (SELECT knowledge_node_id FROM knowledge_node WHERE track='pet') AND diagnostic_item_id NOT IN (?,?)''',
            (published['diagnostic_item_id'],spec['seed_item_id']))
        c.commit()
    # Old reference-only seed history makes the real planner choose the newly
    # reviewed original; it is not this test's writing answer or a mastery proof.
    LearningStateService(base).submit_diagnostic(sid,spec['seed_item_id'],'I am not sure','seed-reference-history')
    with base.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?",(sid,))
        c.commit()
    workspace=repo.generate_plan(sid,'natural-schedule')
    slot=next(p for p in workspace['plan_items'] if p['source_ref']==published['diagnostic_item_id'])
    pid=slot['plan_item_id']
    item=published
    settings = Settings(data_dir=tmp_path,database_path=base.path,maintenance_enabled=False,
        data_encryption_key=b'r'*32,model_api_key='synthetic-only',model_base_url='https://controlled.invalid/v1',
        model_name='controlled-model',voice_intent_model_name='controlled-model')
    db = Database(base.path,media_root=tmp_path,data_encryption_key=b'r'*32)
    transport = ControlledTransport(objective_type)
    gateway = ModelGateway(settings,transport=httpx.MockTransport(transport))
    with ExitStack() as original_client_lifecycle:
        client = original_client_lifecycle.enter_context(_http_client(create_app(settings,model_gateway=gateway)))
        parent = post(client,'/v1/parent/setup',{'pin':'2468'})
        client.headers['X-HSC-Parent'] = parent['parent_token']
        assert client.put('/v1/parent/privacy',json={'cloud_model_text_allowed':True,'raw_media_storage_allowed':True}).status_code == 200
        # The synthetic daily schedule has already finished its other slots.
        with db.connect() as c:
            c.execute("UPDATE plan_item SET status='completed' WHERE plan_item_id!=?",(pid,))
            c.commit()
        post(client,f'/v1/sessions/{sid}/transition',dict(expected_state='plan_review',target_state='study',
            reason='Accept the synthetic daily schedule',client_action_id='accept-schedule'))
        path=f'/v1/parent/children/{CHILD}/writing-question-authorizations/{item["diagnostic_item_id"]}'
        grant = client.get(path)
        assert grant.status_code == 200, grant.text
        version = grant.json()
        post(client,path+'/grant',dict(**{k:version[k] for k in ('item_version','source_version','curriculum_version')},
            expected_revision=0,client_action_id='synthetic-source-grant',license_basis=LICENSE))
        planned = post(client,f'/v1/sessions/{sid}/agent-turn',dict(transcript=transport.quote,client_action_id='natural-task-start'))
        assert planned['action']['source'] == 'verified_general_plan_control', planned
        assert planned['runtime_capability'] == 'task.start', planned
        result = post(client,f'/v1/agent-turns/{planned["agent_turn_id"]}/execute',{})
        prompt = result['execution']['result']['diagnostic_prompt']['prompt']
        assert prompt['diagnostic_item_id'] == item['diagnostic_item_id']
        yield dict(db=db,sid=sid,pid=pid,settings=settings,client=client,transport=transport,
            gateway=gateway,planned=planned,result=result,prompt=prompt,finish_client=original_client_lifecycle.close,
            gateway_factory=lambda: ModelGateway(settings,transport=httpx.MockTransport(transport)))


def native_answer(case, key='natural'):
    p=case['prompt']
    original=WritingOriginalService(case['db']).capture_native_response(case['sid'],prompt_id=p['prompt_id'],
        item_version=p['item_version'],text=TEXT,client_action_id=key+'-native-manuscript')
    body=dict(prompt_id=p['prompt_id'],item_version=p['item_version'],answer=TEXT,
        writing_original_artifact_id=original['artifact_id'],client_action_id=key+'-original-answer')
    result=post(case['client'],f'/v1/sessions/{case["sid"]}/diagnostic-prompt/answer',body)
    return result,body


def test_natural_scheduled_writing_preserves_preanswer_goal_for_feedback(tmp_path):
    with scheduled(tmp_path) as case:
        answer,body = native_answer(case)
        follow=answer['local_learning_evaluation']
        assert follow['status'] == 'feedback_required', follow
        with case['db'].connect() as c:
            origin=c.execute('''SELECT p.plan_json,o.* FROM agent_plan p JOIN agent_objective o USING(objective_id)
                WHERE p.agent_turn_id=?''',(case['planned']['agent_turn_id'],)).fetchone()
            binding=json.loads(origin['plan_json'])['local_learning_binding']
            assert origin['objective_type']=='learning'
            assert origin['created_at'] <= answer['attempt']['created_at']
            assert binding['prompt_id']==case['prompt']['prompt_id']
            assert binding['item_version']==case['prompt']['item_version']
        evaluated=post(case['client'],f'/v1/agent-turns/{follow["feedback_turn_id"]}/execute',{})
        assert evaluated['execution']['result']['rubric_assessment']['source_evidence_id']==answer['attempt']['evidence_id']


def test_natural_preanswer_pause_resume_keeps_original_goal(tmp_path,monkeypatch):
    from test_voice_runtime_binding import capture,interpret
    with scheduled(tmp_path) as case:
        with case['db'].connect() as c:
            before=dict(c.execute('SELECT * FROM agent_objective WHERE objective_type=\'learning\' AND status=\'active\'').fetchone())
        for control,quote in [('task.pause','先暂停这项写作，保留题目'),('task.resume','刚才停下的作文我接着写，计时也接上')]:
            case['transport'].control=control
            voice=capture(case['client'],case['settings'],monkeypatch,case['sid'],quote,case['prompt'],control+'-voice')
            turn=interpret(case['client'],case['sid'],quote,control+'-turn',voice)
            post(case['client'],f'/v1/agent-turns/{turn["agent_turn_id"]}/execute',{})
        answer,_=native_answer(case)
        assert answer['local_learning_evaluation']['status']=='feedback_required',answer['local_learning_evaluation']
        with case['db'].connect() as c:
            after=c.execute('SELECT * FROM agent_objective WHERE objective_id=?',(before['objective_id'],)).fetchone()
            assert after['created_at']==before['created_at']
            assert after['status']=='active'
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0]==1


def test_scheduled_goal_current_authority_guards_and_http_dispatch(tmp_path):
    from home_study_agent.formative_evaluation_projection import build_formative_projection
    with scheduled(tmp_path) as case:
        answer,_=native_answer(case)
        with case['db'].connect() as c:
            plan=c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=?',(case['planned']['agent_turn_id'],)).fetchone()
            args=dict(child_id=CHILD,session_id=case['sid'],prompt_id=case['prompt']['prompt_id'],
                item_version=case['prompt']['item_version'],objective_id=plan['objective_id'])
            faults={
                'cancelled_command':("UPDATE agent_plan SET status='cancelled' WHERE plan_id=?",(plan['plan_id'],)),
                'unbound':("UPDATE agent_plan SET context_manifest_json=json_remove(context_manifest_json,'$.scheduled_writing_goal') WHERE plan_id=?",(plan['plan_id'],)),
                'wrong_owner':("UPDATE agent_plan SET child_id='child-junior-3' WHERE plan_id=?",(plan['plan_id'],)),
                'wrong_session':("UPDATE agent_plan SET plan_json=json_set(plan_json,'$.local_learning_binding.session_id','other') WHERE plan_id=?",(plan['plan_id'],)),
                'item_changed':("UPDATE diagnostic_item SET prompt=prompt || ' changed' WHERE diagnostic_item_id=?",(case['prompt']['diagnostic_item_id'],)),
                'source_retired':("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(case['prompt']['diagnostic_item_id'],)),
                'task_rebound':("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,'$.goal_prompt_binding.plan_item_id','other') WHERE plan_id=?",(plan['plan_id'],)),
                'unstarted':("UPDATE agent_step SET status='planned' WHERE plan_id=?",(plan['plan_id'],)),
                'late_objective':("UPDATE agent_objective SET created_at='2099-01-01T00:00:00+00:00' WHERE objective_id=?",(plan['objective_id'],)),
                'withdrawn_original':("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id=?",(answer['attempt'].get('writing_original_artifact_id',''),)),
            }
            original=c.execute("SELECT artifact_id FROM capture_artifact WHERE artifact_type='writing_original'").fetchone()
            faults['withdrawn_original']=("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id=?",(original[0],))
            for name,(sql,params) in faults.items():
                c.execute('SAVEPOINT fault')
                c.execute(sql,params)
                value=build_formative_projection(c,**args)
                assert value['status']=='local_required',(name,value)
                c.execute('ROLLBACK TO fault')
                c.execute('RELEASE fault')
            assert build_formative_projection(c,**args)['status']=='ready_for_runtime_checks'
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(case['prompt']['diagnostic_item_id'],))
            c.commit()
        count=len(case['transport'].calls)
        rejected=case['client'].post(f'/v1/agent-turns/{answer["local_learning_evaluation"]["feedback_turn_id"]}/execute',json={})
        assert rejected.status_code in (200,409),rejected.text
        assert len(case['transport'].calls)==count
        with case['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0]==0
