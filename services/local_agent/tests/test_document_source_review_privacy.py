"""Synthetic other-child records must survive source-review cleanup."""
import json

import pytest

from test_diagnostic_prompt_http import environment, CHILD
from test_document_source_review import unmatched, proposal


@pytest.mark.parametrize('scope',['learning','media','tutor','knowledge'])
def test_source_mapping_cleanup_keeps_other_child_and_unrelated_reminders(environment,monkeypatch,scope):
    with unmatched(environment,monkeypatch) as j:
        response=j['parent'].post(j['path'],json=proposal(j['parent'].get(j['path']).json()))
        assert response.status_code==200,response.text
        sentinel=json.dumps({'independent_mapping':{'note':'another child synthetic note'},'practice':{'independent_request':{'turn_id':'sentinel'}}})
        with j['db'].connect() as c:
            c.execute('''INSERT INTO agent_commitment(commitment_id,child_id,commitment_type,due_at,status,payload_json,created_at,client_action_id)
                VALUES ('other-review','child-junior-3','reminder','2026-10-01','pending',?,'2026-09-13','other-review-action')''',(sentinel,))
            c.execute('''INSERT INTO agent_commitment(commitment_id,child_id,commitment_type,due_at,status,payload_json,created_at,client_action_id)
                VALUES ('own-unrelated',?,'reminder','2026-10-01','pending','{"unrelated":true}','2026-09-13','own-unrelated-action')''',(CHILD,))
            c.commit()
        result=j['client'].app.state.privacy_lifecycle.delete_child_scope(CHILD,scope,'确认删除','source-owned-'+scope)
        assert result['status']=='completed'
        with j['db'].connect() as c:
            assert c.execute("SELECT payload_json FROM agent_commitment WHERE commitment_id='other-review'").fetchone()[0]==sentinel
            if scope!='media':
                assert c.execute("SELECT payload_json FROM agent_commitment WHERE commitment_id='own-unrelated'").fetchone()[0]=='{"unrelated":true}'
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
            own=c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()
            assert not own or not json.loads(own[0]).get('independent_mapping')
