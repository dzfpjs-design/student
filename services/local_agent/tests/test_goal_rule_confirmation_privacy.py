import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_goal_rule_confirmation_http import host, goal_db, PATH, CHILD, authorize, request_body


@pytest.mark.parametrize('scope',['learning','all_history'])
def test_confirmed_rule_export_delete_and_oldkey_cannot_restore_authority(host, scope):
    db,service,settings=host
    with TestClient(create_app(settings)) as client:
        authorize(client)
        body=request_body(client.get(PATH).json())
        result=client.post(PATH,json=body)
        assert result.status_code==200,result.text
        revision=result.json()['receipt']['revision_id']
        export=client.post(f'/v1/parent/children/{CHILD}/export')
        assert export.status_code==200,export.text
        data=json.loads(Path(export.json()['path']).read_text())
        assert revision in {r['revision_id'] for r in data['goal_program_revisions']}
        other=client.post('/v1/parent/children/child-junior-3/export').json()
        assert not any(r['revision_id']==revision for r in json.loads(Path(other['path']).read_text())['goal_program_revisions'])
        deleted=client.post(f'/v1/parent/children/{CHILD}/delete',json={
            'scope':scope,'confirmation':'确认删除','client_action_id':'delete-rule-'+scope})
        assert deleted.status_code==200,deleted.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM goal_program_revision WHERE revision_id=?',(revision,)).fetchone()[0]==0
            assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
    with TestClient(create_app(settings)) as client:
        authorize(client,False)
        retry=client.post(PATH,json=body)
        assert retry.status_code in (404,409),retry.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM goal_program_revision WHERE revision_id=?',(revision,)).fetchone()[0]==0
