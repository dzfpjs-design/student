"""Published material packets must remain readable through the real parent API."""
import pytest
import json
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_open_response_paper_http import published_pair, CHILD
from test_companion_activity_host import NoCloud


@pytest.mark.parametrize('with_material',[True,False])
def test_parent_audit_reads_two_published_questions_without_cursor_reset(tmp_path,with_material):
    db,_,first,second=published_pair(tmp_path,'物理',with_material)
    settings=Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False)
    with TestClient(create_app(settings,model_gateway=NoCloud()),client=('127.0.0.1',42000),raise_server_exceptions=False) as client:
        parent=client.post('/v1/parent/setup',json={'pin':'8264'})
        assert parent.status_code==200,parent.text
        headers={'X-HSC-Parent':parent.json()['parent_token']}
        reply=client.get(f'/api/parent/children/{CHILD}/curriculum-supply/audit',headers=headers)
        assert reply.status_code==200,reply.text
        result=reply.json()
        assert result['eligible_curriculum_items']==2,result
        assert {p['candidate_id'] for p in result['packages']}=={first['candidate_id'],second['candidate_id']}
        with db.connect() as c:
            snapshot=json.loads(c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?',(first['candidate_id'],)).fetchone()[0])
        options=client.get(f"/api/parent/children/{CHILD}/curriculum-supply/sources/{snapshot['source']['source_id']}/options",headers=headers)
        assert options.status_code==200,options.text
        assert options.json()['seeds'] and options.json()['excerpts']
