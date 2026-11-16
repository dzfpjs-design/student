"""Real app, local-only observation preparation, with synthetic records."""
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.repository import utc_now


def test_preparation_auth_durability_refresh_and_unknown(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'app.db', maintenance_enabled=False, session_token='local-secret')
    with TestClient(create_app(settings), client=('127.0.0.1', 4200)) as client:
        child = 'child-primary-4'
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        path = f'/api/parent/children/{child}/daily-visual-preview/{day}/delivery-preparation'
        assert client.get(path).status_code == 401
        local = {'X-HSC-Session':'local-secret'}
        assert client.get(path, headers=local).status_code == 401
        parent = client.post('/v1/parent/setup', json={'pin':'2468'}, headers=local).json()['parent_token']
        headers = {**local, 'X-HSC-Parent':parent}
        assert client.get(path, headers={'X-HSC-Parent':parent}).status_code == 401
        db = client.app.state.repository.database
        now = utc_now()
        with db.connect() as c:
            c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('other','另一个','初三','江苏','active',?,?)", (now,now))
            c.execute("INSERT INTO report_delivery_recipient(recipient_id,child_id,channel,address,label,consent_status,consented_at,created_at) VALUES('r',?,'email','parent@example.test','家长','consented',?,?)", (child,now,now))
            c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('s',?,'active','study',?,?,'action')", (child,now,now))
            c.commit()
        result = client.get(path, headers=headers)
        assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
        assert result.json()['delivery_enabled'] is False
        assert result.json()['recipients'][0]['status'] == 'not_prepared'
        assert client.get(path.replace(child,'other'), headers=headers).json()['recipients'] == []
        assert client.post(path.replace(child,'other'), headers=headers, json={'recipient_id':'r'}).status_code == 409
        assert client.post(path, headers=headers, json={'recipient_id':'r','extra':True}).status_code == 422
        result = client.post(path, headers=headers, json={'recipient_id':'r'})
        assert result.status_code == 200 and result.json()['external_delivery_permitted'] is False
        source = result.json()['source_id']
        assert client.get(path, headers=headers).json()['recipients'][0]['status'] == 'prepared'
        with db.connect() as c:
            c.execute("INSERT INTO child_state_event(state_event_id,child_id,session_id,state,confidence,reason,duration_ms,model_version,shadow,observed_at) VALUES('event',?,'s','writing_like',.9,'synthetic',0,'synthetic',0,?)", (child,now))
            c.commit()
        assert client.get(path, headers=headers).json()['recipients'][0]['status'] == 'source_changed'
        assert client.post(path, headers=headers, json={'recipient_id':'r'}).status_code == 409
        assert client.post(path, headers=headers, json={'recipient_id':'r','expected_source_id':'wrong'}).status_code == 409
        refreshed = client.post(path, headers=headers, json={'recipient_id':'r','expected_source_id':source})
        assert refreshed.status_code == 200 and refreshed.json()['source_id'] != source
        with db.connect() as c:
            c.execute("UPDATE report_delivery_recipient SET consent_status='revoked',revoked_at=? WHERE recipient_id='r'", (now,))
            c.commit()
        assert client.get(path, headers=headers).json()['recipients'][0]['status'] == 'needs_authorization'
        assert client.post(path, headers=headers, json={'recipient_id':'r'}).status_code == 409
        with db.connect() as c:
            c.execute("UPDATE report_delivery_recipient SET consent_status='consented',revoked_at=NULL WHERE recipient_id='r'")
            c.execute("INSERT INTO daily_report(report_id,session_id,child_id,report_date,source,evidence_json,summary_json,parent_coaching_json,created_at) VALUES('report','s',?,?,'synthetic','{}','{}','{}',?)", (child,day,now))
            c.execute("""INSERT INTO parent_report_snapshot(snapshot_id,source_kind,source_id,report_id,child_id,report_type,source_version,template_version,evidence_version,facts_json,inferences_json,content_json,confidence_json,status,created_at) VALUES('snap','daily_report','report','report',?,'daily','1','1','1','{}','{}','{}','{}','approved',?)""", (child,now))
            c.execute("""INSERT INTO report_delivery_outbox(delivery_id,snapshot_id,recipient_id,channel,idempotency_key,status,scheduled_at,created_at,updated_at) VALUES('old','snap','r','email','old','unknown',?,?,?)""", (now,now,now))
            c.commit()
        assert client.get(path, headers=headers).json()['recipients'][0]['status'] == 'unknown'
        assert client.post(path, headers=headers, json={'recipient_id':'r'}).status_code == 409
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM report_delivery_outbox").fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM report_delivery_attempt").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM daily_reflection").fetchone()[0] == 0


def test_parent_session_loss_before_commit_rolls_back(tmp_path, monkeypatch):
    from home_study_agent.parent_auth import ParentSessionRequiredError
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'app.db', maintenance_enabled=False, session_token='local-secret')
    with TestClient(create_app(settings), client=('127.0.0.1', 4200)) as client:
        headers = {'X-HSC-Session':'local-secret'}
        headers['X-HSC-Parent'] = client.post('/v1/parent/setup', json={'pin':'2468'}, headers=headers).json()['parent_token']
        now = utc_now()
        db = client.app.state.repository.database
        with db.connect() as c:
            c.execute("INSERT INTO report_delivery_recipient(recipient_id,child_id,channel,address,label,consent_status,consented_at,created_at) VALUES('r','child-primary-4','email','parent@example.test','家长','consented',?,?)", (now,now))
            c.commit()
        auth = client.app.state.parent_auth
        original = auth.require
        calls = []
        def expires(token):
            calls.append(token)
            if len(calls) > 1:
                raise ParentSessionRequiredError()
            original(token)
        monkeypatch.setattr(auth, 'require', expires)
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        path = f'/api/parent/children/child-primary-4/daily-visual-preview/{day}/delivery-preparation'
        assert client.post(path, headers=headers, json={'recipient_id':'r'}).status_code == 401
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM daily_observation_report_source').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM daily_observation_report_outbox').fetchone()[0] == 0
