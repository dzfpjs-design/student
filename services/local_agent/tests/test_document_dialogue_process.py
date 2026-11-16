"""Two actual service processes preserve one input, original page and receipt."""
import json
from contextlib import closing
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import httpx

from test_document_voice_capture import page_context, begin, complete
from test_diagnostic_prompt_http import environment


def test_tcp_process_restart_keeps_one_original_and_one_execution(page_context, tmp_path):
    j = page_context
    quote = '我们先不看这张纸，你有PET学习资料可以给我介绍吗？'
    assert begin(j).status_code == 200
    voice = complete(j,text=quote)
    body = {'transcript':quote,'voice_input_id':voice['voice_input_id'],
            'client_action_id':'process-original-question','cloud_text_allowed':True}
    fixture = Path(__file__).parent/'fixtures/document_dialogue_process.py'
    source = Path(__file__).parents[1]/'src'
    processes = []
    def start():
        log = (tmp_path/f'process-{len(processes)}.log').open('w')
        process = subprocess.Popen([sys.executable,str(fixture),'--data-root',str(j['settings'].data_dir)],
            stdout=subprocess.PIPE,stderr=log,text=True,
            env={**os.environ,'PYTHONPATH':str(source),'PYTHONDONTWRITEBYTECODE':'1'})
        processes.append((process,log))
        assert select.select([process.stdout],[],[],15)[0], 'fixture startup deadline'
        info = json.loads(process.stdout.readline())
        assert set(info) == {'port', 'synthetic_models', 'pid'}, info
        assert info['pid'] == process.pid and process.poll() is None, info
        assert info['synthetic_models']
        client = httpx.Client(base_url=f"http://127.0.0.1:{info['port']}",timeout=10,trust_env=False)
        deadline = time.monotonic()+10
        last = 'not connected'
        while time.monotonic()<deadline:
            try:
                response = client.get('/openapi.json')
                last = f'HTTP {response.status_code}'
                if response.status_code == 200:
                    return process,client
            except httpx.TransportError as error:
                last = type(error).__name__
            time.sleep(.05)
        client.close()
        raise AssertionError(f'HTTP startup deadline: {last}, exit={process.poll()}')
    def stop(process, crash=False):
        if crash:
            process.kill()
        else:
            process.terminate()
        process.wait(timeout=5)
        if crash:
            assert process.returncode == -signal.SIGKILL
        else:
            # Uvicorn re-raises SIGTERM after its shutdown handlers run.
            assert process.returncode in (0, -signal.SIGTERM)
    parent_closed = j['close_parent_app']()
    assert parent_closed == {'lifespan_consumed': True, 'portal_joined': True,
                             'money_owner_closed': True, 'visual_task_closed': True,
                             'background_tasks_closed': True, 'database_connections_closed': True}
    try:
        process,client = start()
        with closing(client):
            response = client.post(f"/v1/sessions/{j['sid']}/agent-turn",json=body)
            assert response.status_code == 200,response.text
            turn = response.json()
            assert turn['action']['voice_input_binding']['question_reference'] == j['reference']
            assert turn['intent'] == 'system.inspect_learning'
            receipt = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert receipt.status_code == 200,receipt.text
            original = receipt.json()['execution']['result']
        stop(process, crash=True)
        process,client = start()
        with closing(client):
            repeated = client.post(f"/v1/sessions/{j['sid']}/agent-turn",json=body)
            assert repeated.status_code == 200,repeated.text
            assert repeated.json()['agent_turn_id'] == turn['agent_turn_id']
            receipt = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert receipt.status_code == 200,receipt.text
            assert receipt.json()['execution']['result'] == original
        stop(process)
        with j['db'].connect() as c:
            assert c.execute("SELECT count(*) FROM model_call WHERE session_id=? AND purpose IN ('voice_interaction','agent_plan')",
                             (j['sid'],)).fetchone()[0] == 2
            assert c.execute("SELECT count(*) FROM agent_turn WHERE client_action_id=?",(body['client_action_id'],)).fetchone()[0] == 1
    finally:
        for process,log in processes:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
            process.stdout.close(); log.close()
