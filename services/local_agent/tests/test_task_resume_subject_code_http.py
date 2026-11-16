"""Original paused_case HTTP source, real DB offers; only ASR/control inference is synthetic."""
import copy,json,os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.task_start_binding import TaskStartBinding
from home_study_agent.task_start_semantics import SUBJECTS
from home_study_agent.model_gateway import ModelResult
from test_diagnostic_prompt_http import environment,OTHER
from test_content_free_voice_control_http import ControlGateway
import test_task_resume_voice_http as original_resume
EXPECTED=Path(os.environ.get('HSC_RESUME_EXPECTED_RUNTIME',str(Path(__file__).resolve().parents[1]/'src'))).resolve()
assert Path(__import__('inspect').getsourcefile(TaskStartBinding)).resolve()==EXPECTED/'home_study_agent/task_start_binding.py'

class SubjectResumeGateway(ControlGateway):
    async def complete(self,request):
        result=await super().complete(request)
        if self.control!='task.resume':return result
        payload=json.loads(result.content);payload['selector']={'subject':'math','task_kind':None,'relative_order':None}
        return ModelResult(content=json.dumps(payload,ensure_ascii=False),provider='test',model='controlled-subject-resume',latency_ms=1)

def paused_math(client,settings,db,monkeypatch,gateway):
    original=original_resume.task_fixture
    def public_code_fixture(database,sid):
        repo=original(database,sid)
        with database.connect()as c:
            c.execute("UPDATE plan_item SET subject='math' WHERE plan_id='control-plan'");c.commit()
        return repo
    with monkeypatch.context()as fixture:
        fixture.setattr(original_resume,'task_fixture',public_code_fixture)
        return original_resume.paused_case(client,settings,db,monkeypatch,gateway)

def test_actual_paused_offer_matches_canonical_math_code(environment,monkeypatch):
    settings,db=environment;gateway=ControlGateway('')
    with TestClient(create_app(settings,model_gateway=gateway))as client:
        sid,repo,prompt=paused_math(client,settings,db,monkeypatch,gateway)
        with db.connect()as c:
            target=TaskStartBinding.resume_snapshot(c,sid,'child-primary-4')
            assert len(target['items'])==1 and target['items'][0]['subject']=='math'
            actual=TaskStartBinding.select_resume_target(target,{'subject':'math','task_kind':None,'relative_order':None})
            assert actual['status']=='ready'and actual['item']==target['items'][0],('CANONICAL_RESUME_SUBJECT_CODE_REJECTED',actual['status'])
            assert repo._get_plan_item_by_id('control-task-1')['status']=='paused'

def test_actual_http_restart_resume_named_math_preserves_original_task(environment,monkeypatch):
    settings,db=environment;gateway=SubjectResumeGateway('')
    with TestClient(create_app(settings,model_gateway=gateway))as client:
        sid,repo,prompt=paused_math(client,settings,db,monkeypatch,gateway)
        before=repo._get_plan_item_by_id('control-task-1')['active_seconds']
    with TestClient(create_app(settings,model_gateway=gateway))as client:
        resumed=original_resume.hear(client,settings,monkeypatch,gateway,sid,prompt,'把刚才暂停的数学任务接着做，计时和进度继续','resume-math','task.resume')
        assert resumed['action']['type']=='resume_paused_task',('CANONICAL_RESUME_SUBJECT_CODE_REJECTED',resumed['action'].get('source'))
        assert resumed['runtime_capability']=='task.resume';url='/v1/agent-turns/'+resumed['agent_turn_id']+'/execute'
        response=client.post(url);assert response.status_code==200,response.text
        assert repo._get_plan_item_by_id('control-task-1')['status']=='active'
        assert repo._get_plan_item_by_id('control-task-1')['active_seconds']==before
    with TestClient(create_app(settings,model_gateway=gateway))as client:
        before_replay=repo._get_plan_item_by_id('control-task-1')
        response=client.post(url);assert response.status_code==200,response.text
        after_replay=repo._get_plan_item_by_id('control-task-1')
        assert after_replay==before_replay
    with db.connect()as c:
        row=c.execute("SELECT subject,status,active_seconds FROM plan_item WHERE plan_item_id='control-task-1'").fetchone()
        assert row['subject']=='math'and row['status']==before_replay['status']and row['active_seconds']==before_replay['active_seconds']and row['active_seconds']>=before
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id='control-task-1' AND event_type='resumed'").fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0

def test_closed_catalog_equivalence_and_ambiguity_remain_strict():
    selector=lambda subject:{'subject':subject,'task_kind':None,'relative_order':None}
    for code,name in SUBJECTS.items():
        for stored in (code,name):
            item={'subject':stored,'task_type':'written','plan_item_id':'public'};target={'items':[item]}
            assert TaskStartBinding.select_resume_target(target,selector(code))['item']==item
    for stored in (None,'','mathematics-near-name','Math','physics','物理'):
        actual=TaskStartBinding.select_resume_target({'items':[{'subject':stored}]},selector('math'))
        assert actual['status']=='ambiguous'and actual['item']is None
    item={'subject':'math'};same={'subject':'数学'}
    duplicate=TaskStartBinding.select_resume_target({'items':[item,same]},selector('math'));assert duplicate['status']=='ambiguous'and duplicate['item']is None
    empty=TaskStartBinding.select_resume_target({'items':[]},selector('math'));assert empty['status']=='ambiguous'and empty['item']is None
    with pytest.raises(KeyError):TaskStartBinding.select_resume_target({'items':[item]},selector('mathematics-near-name'))

@pytest.mark.parametrize('changed',['raw_equivalent_subject','cross_child_arrival'])
def test_resume_write_rechecks_original_raw_binding(environment,monkeypatch,changed):
    settings,db=environment;gateway=SubjectResumeGateway('')
    with TestClient(create_app(settings,model_gateway=gateway))as client:
        sid,repo,prompt=paused_math(client,settings,db,monkeypatch,gateway)
        resumed=original_resume.hear(client,settings,monkeypatch,gateway,sid,prompt,'把刚才暂停的数学任务接着做','resume-before-source-change','task.resume')
        assert resumed['action']['type']=='resume_paused_task'
        with db.connect()as c:
            if changed=='raw_equivalent_subject':c.execute("UPDATE plan_item SET subject='数学' WHERE plan_item_id='control-task-1'")
            else:c.execute('UPDATE voice_control_intent_request SET child_id=? WHERE session_id=?',(OTHER,sid))
            c.commit()
        response=client.post('/v1/agent-turns/'+resumed['agent_turn_id']+'/execute');assert response.status_code==409,response.text
        assert repo._get_plan_item_by_id('control-task-1')['status']=='paused'
        with db.connect()as c:assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0]==0
