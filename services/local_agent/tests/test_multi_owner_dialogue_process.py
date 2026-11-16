"""Real TCP + SIGKILL; synthetic provider and disposable evidence only."""
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.exam_simulation import ExamSimulationService
from home_study_agent.pet_vocabulary import PetVocabularyService
from test_exam_bound_runtime_http import environment as exam_environment, start as start_exam
from test_pet_vocabulary_prompt import flow
from test_pet_vocabulary_prompt_integration import environment as vocabulary_environment, next_prompt, binding
from test_document_voice_capture import begin, complete


@pytest.fixture
def process_environment(request):
    settings,db,sid=request.getfixturevalue(request.getfixturevalue('owner')+'_environment')
    with ExitStack() as stack:
        setup=stack.enter_context(TestClient(create_app(settings)))
        yield settings,db,sid,setup,stack.close


@pytest.mark.parametrize('owner',['exam','vocabulary'])
def test_owned_independent_reply_survives_real_process_crash(process_environment,tmp_path,owner):
    settings,db,sid,setup,close_original_setup=process_environment
    if owner=='exam':
        service=ExamSimulationService(db);start_exam(service,sid);reference=service.arrival_binding(sid)
    else:
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,));c.commit()
        reference=binding(next_prompt(setup,sid))
    j={'settings':settings,'db':db,'sid':sid,'client':setup}
    quote='我们先不看这张纸，你有PET学习资料可以给我介绍吗？'
    assert begin(j,reference={'kind':owner,'binding':reference}).status_code==200
    voice=complete(j,text=quote)
    body={'transcript':quote,'voice_input_id':voice['voice_input_id'],
          'client_action_id':'process-'+owner+'-independent','cloud_text_allowed':True}
    close_original_setup()
    processes=[];source=Path(__file__).parents[1]/'src'
    fixture=Path(__file__).parent/'fixtures/document_dialogue_process.py'
    def launch():
        log=(tmp_path/f'{owner}-process-{len(processes)}.log').open('w')
        p=subprocess.Popen([sys.executable,str(fixture),'--data-root',str(settings.data_dir),
                            '--database-name',db.path.name],stdout=subprocess.PIPE,stderr=log,text=True,
                            env={**os.environ,'PYTHONPATH':str(source),'PYTHONDONTWRITEBYTECODE':'1'})
        processes.append((p,log))
        assert select.select([p.stdout],[],[],15)[0],'startup deadline'
        info=json.loads(p.stdout.readline());assert info['synthetic_models']
        assert set(info) == {'port', 'synthetic_models', 'pid'}
        assert info['pid'] == p.pid and p.poll() is None
        client=httpx.Client(base_url=f"http://127.0.0.1:{info['port']}",timeout=10,trust_env=False)
        until=time.monotonic()+10
        while time.monotonic()<until:
            try:
                if client.get('/openapi.json').status_code==200:return p,client
            except httpx.TransportError:
                pass
            time.sleep(.05)
        client.close();raise AssertionError('HTTP startup deadline')
    try:
        p,client=launch()
        with closing(client):
            if owner=='exam':
                # Production restart recovery intentionally pauses assessments.
                # Resume explicitly; neither a resource query nor a replay may
                # implicitly resume or reset the original clock.
                resumed=client.post(f"/v1/exam-simulations/{reference['run_id']}/control",json={'command':'resume'})
                assert resumed.status_code==200,resumed.text
            planned=client.post(f'/v1/sessions/{sid}/agent-turn',json=body)
            assert planned.status_code==200,planned.text
            turn=planned.json();assert turn['action']['type']=='inspect_learning_system',turn
            execution=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert execution.status_code==200,execution.text
            saved=execution.json()['execution']
        p.kill();p.wait(timeout=5);assert p.returncode==-signal.SIGKILL
        p,client=launch()
        with closing(client):
            same=client.post(f'/v1/sessions/{sid}/agent-turn',json=body)
            assert same.status_code==200 and same.json()['agent_turn_id']==turn['agent_turn_id']
            recovered=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert recovered.status_code==200 and recovered.json()['execution']==saved
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM pet_vocabulary_attempt').fetchone()[0]==0
            assert c.execute("SELECT count(*) FROM model_call WHERE session_id=? AND purpose IN ('voice_interaction','agent_plan')",(sid,)).fetchone()[0]==2
            assert c.execute('SELECT count(*) FROM agent_turn WHERE client_action_id=?',(body['client_action_id'],)).fetchone()[0]==1
        if owner=='exam':
            with db.connect() as c:
                run=c.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(reference['run_id'],)).fetchone()
                assert run['status']=='paused' and run['current_position']==reference['position']
                assert c.execute('SELECT source_version FROM specialized_exam_item WHERE run_id=? AND position=?',
                                 (reference['run_id'],reference['position'])).fetchone()[0]==reference['source_version']
        else:
            assert binding(PetVocabularyService(db).active_prompt(sid))==reference
        p.terminate();p.wait(timeout=5);assert p.returncode in (0,-signal.SIGTERM)
    finally:
        for p,log in processes:
            if p.poll() is None:p.kill();p.wait(timeout=5)
            p.stdout.close();log.close()
