"""Actual parent write transaction failures, immutable revisions and ownership."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.local_supply import validate_item, LocalSupplyError
from test_pet_oral_supply_http import setup, authored, post, BASE, CHILD, OTHER
from test_pet_reviewed_source_actual_http import NoCloud


@pytest.mark.parametrize('failure', ['publication_abort', 'source_withdrawal'])
def test_actual_oral_publication_cannot_commit_without_current_source_and_item(tmp_path, failure):
    db, settings = setup(tmp_path)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41904),
            headers={'X-HSC-Session': settings.session_token}, raise_server_exceptions=False) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        original, source, draft = authored(client, db, 'read_aloud')
        cid = draft['candidate_id']
        assert client.post(BASE + f'/{cid}/validate', json=dict(action_id='v', expected_version=1,
            actor='another-independent-reviewer')).status_code == 422
        checked = post(client, BASE + f'/{cid}/validate', dict(action_id='v', expected_version=1))
        assert checked['state'] == 'validated'
        assert client.post(BASE + f'/{cid}/review', json=dict(action_id='r', expected_version=1,
            decision='approve', note='wrong version')).status_code == 409
        assert client.post(BASE.replace(CHILD, OTHER) + f'/{cid}/review', json=dict(action_id='r', expected_version=2,
            decision='approve', note='foreign child')).status_code == 404
        reviewed = post(client, BASE + f'/{cid}/review', dict(action_id='r', expected_version=2,
            decision='approve', note='Real parent review of the synthetic complete source'))
        assert reviewed['state'] == 'reviewed'
        with db.connect() as c:
            frozen = c.execute('SELECT payload_json,source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?', (cid,)).fetchone()[:]
            if failure == 'publication_abort':
                c.execute("""CREATE TRIGGER abort_oral_publication BEFORE INSERT ON diagnostic_item
                    WHEN NEW.source_ref LIKE 'parent_approved_specialized:%'
                    BEGIN SELECT RAISE(ABORT,'synthetic publication storage failure'); END""")
            else:
                c.execute("UPDATE knowledge_source SET review_status='pending' WHERE source_id=?", (source['source_id'],))
            c.commit()
        body = dict(action_id='p', expected_version=3)
        failed = client.post(BASE + f'/{cid}/publish', json=body)
        if failure == 'publication_abort':
            assert failed.status_code == 500, failed.text
            with db.connect() as c:
                assert c.execute('SELECT state,version FROM curriculum_supply_package WHERE candidate_id=?', (cid,)).fetchone()[:] == ('reviewed', 3)
                assert not c.execute("SELECT 1 FROM curriculum_supply_event WHERE candidate_id=? AND kind='publish'", (cid,)).fetchone()
                assert not c.execute("SELECT 1 FROM diagnostic_item WHERE source_ref=?", ('parent_approved_specialized:' + cid,)).fetchone()
                c.execute('DROP TRIGGER abort_oral_publication')
                c.commit()
            recovered = post(client, BASE + f'/{cid}/publish', body)
            assert recovered['eligible'], recovered['blockers']
            assert post(client, BASE + f'/{cid}/publish', body)['diagnostic_item_id'] == recovered['diagnostic_item_id']
            with db.connect() as c:
                assert c.execute("SELECT count(*) FROM curriculum_supply_event WHERE candidate_id=? AND kind='publish'", (cid,)).fetchone()[0] == 1
                assert validate_item(c, CHILD, recovered['diagnostic_item_id'])['stimulus_text'] == original['stimulus_text']
        else:
            assert failed.status_code == 200 and failed.json()['state'] == 'blocked', failed.text
            assert not failed.json()['eligible']
            with db.connect() as c:
                assert not c.execute('SELECT 1 FROM diagnostic_item WHERE source_ref=?', ('parent_approved_specialized:' + cid,)).fetchone()
        with db.connect() as c:
            assert c.execute('SELECT payload_json,source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?', (cid,)).fetchone()[:] == frozen
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM model_call').fetchone()[0] == 0
