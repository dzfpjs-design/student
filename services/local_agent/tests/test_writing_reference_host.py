"""Durability/privacy host tests; request admission uses a synthetic fixture."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_companion_activity_host import NoCloud
from test_writing_response_origin import writing,CHILD
from test_writing_reference_revision import reference_revision,prepare_revision


@pytest.mark.parametrize('scope',['learning','media','all_history'])
def test_reference_revision_export_read_and_erasure_are_durable(reference_revision,scope):
    source,_,_=reference_revision
    db,sid,_,parent,_=source
    revised=prepare_revision(reference_revision)['prompt']
    settings=Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False,
        session_token='reference-host-isolated')
    headers={'X-HSC-Session':settings.session_token}
    with TestClient(create_app(settings,model_gateway=NoCloud()),raise_server_exceptions=False) as client:
        token=client.post('/v1/parent/setup',headers=headers,json={'pin':'2468'}).json()['parent_token']
        result=client.get(f'/v1/sessions/{sid}/writing-teaching',headers=headers,
            params={'prompt_id':parent['prompt_id'],'item_version':parent['item_version']})
        assert result.status_code==200,result.text
        assert result.json()['followups'][0]['prompt_id']==revised['prompt_id']
        assert result.json()['followups'][0]['reference_only'] is True
        assert result.json()['assessment_id'] is None
        exported=client.app.state.privacy_lifecycle.export_child(CHILD)
        data=json.loads(Path(exported['path']).read_text())
        rows=data['writing_reference_revisions']
        assert len(rows)==1 and rows[0]['prompt_id']==revised['prompt_id']
        assert 'assessment_id' not in rows[0]
        deleted=client.post(f'/v1/parent/children/{CHILD}/delete',headers={**headers,'X-HSC-Parent':token},
            json={'scope':scope,'confirmation':'确认删除','client_action_id':'erase-reference'})
        assert deleted.status_code==200 and deleted.json()['status']=='completed',deleted.text
        assert deleted.json()['deleted_counts']['writing_reference_revision']==1
    with TestClient(create_app(settings,model_gateway=NoCloud())):
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM writing_reference_revision').fetchone()[0]==0
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
