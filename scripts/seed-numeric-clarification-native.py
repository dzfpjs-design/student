"""Isolated reviewed synthetic scalar; no family DB, media, or model requests."""
import json
import os
from pathlib import Path
import sys

database_path = Path(sys.argv[1]).resolve()
receipt_path = Path(sys.argv[2]).resolve()
os.environ.update(HSC_DATA_DIR=str(database_path.parent), HSC_DATABASE_PATH=str(database_path), HSC_MAINTENANCE_ENABLED='0')
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root/'services/local_agent/src'), str(root/'services/local_agent/tests')]


def local_only(event,args):
    if event == 'socket.connect' and isinstance(args[1],tuple) and args[1][0] not in ('127.0.0.1','::1','localhost'):
        raise RuntimeError('External networking forbidden')


sys.addaudithook(local_only)
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from test_specialized_closed_http import generated_http
from test_spoken_numeric import numeric_generated
import test_specialized_closed_contract as closed

closed.generated = numeric_generated
settings = Settings(data_dir=database_path.parent,database_path=database_path,maintenance_enabled=False)
db=Database(database_path)
with TestClient(create_app(settings,model_gateway=closed.ClosedGateway())) as client:
    def post(path,body):
        response=client.post(path,json=body)
        assert response.status_code in (200,201), (path,response.status_code)
        return response.json()
    headers,rows=generated_http(client,db)
    row=next(r for r in rows if r['stage']=='parallel')
    response=client.post('/v1/parent/specialized-supply/candidates/'+row['candidate_id']+'/review',headers=headers,
        json={'decision':'approve','note':'Synthetic scalar native check','client_action_id':'native-numeric-review'})
    assert response.status_code==200
    item=response.json()['diagnostic_item_id']
    session=post('/v1/sessions',{'child_id':'child-primary-4','client_action_id':'native-numeric-session'})['session_id']
    def transition(before,after):
        return post('/v1/sessions/'+session+'/transition',{'expected_state':before,'target_state':after,
            'reason':'synthetic native check','client_action_id':'native-transition-'+after})
    transition('check_in','classroom_recall')
    post('/v1/sessions/'+session+'/recalls/none',{'client_action_id':'native-no-recall'})
    transition('classroom_recall','homework_capture')
    post('/v1/sessions/'+session+'/homework',{'subject':'数学','title':'合成澄清练习','expected_minutes':10,'client_action_id':'native-homework'})
    post('/v1/sessions/'+session+'/plan/generate',{'client_action_id':'native-plan'})
    transition('plan_review','study')
    prompt=post('/v1/sessions/'+session+'/diagnostic-prompt/prepare',{'track':'competition_math',
        'preferred_item_id':item,'client_action_id':'native-numeric-prompt'})['prompt']
receipt_path.write_text(json.dumps({'session_id':session,'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],
    'diagnostic_item_id':item,'synthetic':True},ensure_ascii=False),encoding='utf-8')
