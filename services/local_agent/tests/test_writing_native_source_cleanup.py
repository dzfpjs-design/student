"""Question erasure is distinct from erasure of the child's original writing."""
import json
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.privacy_native_sources import purge_native_sources
from home_study_agent.repository import StudyRepository, utc_now
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.writing_ocr_authorization import WritingOcrAuthorizationService
from home_study_agent.writing_response_origin import WritingOriginalService
from test_writing_response_origin import CHILD, TEXT
from test_writing_source_publication import prepared, publish
from test_writing_ocr_authorization import ocr, readback

SOURCE = 'isolated-fixture:authored-original'


@pytest.fixture
def writing(tmp_path):
    db,service,spec,_ = prepared(tmp_path)
    published = publish(service,spec)
    sid = StudyRepository(db).start_session(CHILD,'writing-native')['session_id']
    prompts = DiagnosticPromptService(db)
    p = prompts.prepare(sid,track='pet',preferred_item_id=published['diagnostic_item_id'],client_action_id='native-source-question')['prompt']
    return db,sid,prompts,p,WritingOriginalService(db)


def test_readback_and_retract_source_guards_survive_native_erasure_restart(ocr):
    writing,service,packet = ocr
    db,sid,_,_,_ = writing
    binding = {k:packet[k] for k in ('confirmation_id','binding_version')}
    assert service.read(sid,**binding)['response_text'] == TEXT
    readback(ocr)
    readback(ocr,'ended')
    with db.connect() as c:
        before = [tuple(r) for r in c.execute('SELECT * FROM writing_ocr_event')]
        question = c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?',(writing[3]['diagnostic_item_id'],)).fetchone()[0]
        assert question in packet['readback_text']
        assert all(question not in str(tuple(r)) and TEXT not in str(tuple(r)) for table in
                   ('writing_ocr_confirmation','writing_ocr_event') for r in c.execute('SELECT * FROM '+table))
        c.execute('BEGIN IMMEDIATE')
        purge_native_sources(c,CHILD,{SOURCE},affected_scopes={})
        assert [tuple(r) for r in c.execute('SELECT * FROM writing_ocr_event')] == before
        assert c.execute("SELECT extracted_text FROM capture_artifact WHERE artifact_id='page'").fetchone()[0] == TEXT
        c.commit()
    restarted = WritingOcrAuthorizationService(Database(db.path))
    with pytest.raises(ValueError):
        restarted.read(sid,**binding)
    with pytest.raises(ValueError):
        readback(ocr,'ended',key='late-readback')
    # Withdrawal stays explicit; source erasure does not fabricate it.
    assert restarted.retract(sid,**binding,client_action_id='withdraw-after-source')['status'] == 'withdrawn'
    with pytest.raises(ValueError):
        restarted.read(sid,**binding)


def test_legacy_readback_result_linked_only_by_confirmation_is_erased(ocr):
    writing,_,packet = ocr
    db,sid,_,_,_ = writing
    readback(ocr)
    readback(ocr,'ended')
    turn,plan,step,objective = [uuid4().hex for _ in range(4)]
    now = utc_now()
    with db.connect() as c:
        # Actual readback, legacy envelope with only a server confirmation ID.
        action = {'type':'read_writing_original','writing_ocr_confirmation':
                  {k:packet[k] for k in ('confirmation_id','binding_version')}}
        c.execute("INSERT INTO agent_objective VALUES(?,?,?,'learning','Read original','[]','diagnose','active',?,?,NULL,NULL)",
                  (objective,CHILD,sid,now,now))
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,'Read my writing','read_writing_original',?,?,'legacy-local',?,?)''',
                  (turn,CHILD,sid,packet['readback_text'],json.dumps(action),now,turn))
        c.execute("INSERT INTO agent_plan VALUES(?,?,?,?,?,'legacy-local','{}','{}','completed',?,?)",(plan,objective,CHILD,sid,turn,now,now))
        c.execute("INSERT INTO agent_step VALUES(?,?,0,'writing.read_original','1','client_device_bridge','low','{}','[]','completed',?,?,NULL,?,?)",
                  (step,plan,step,json.dumps({'readback_text':packet['readback_text']}),now,now))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        counts = purge_native_sources(c,CHILD,{SOURCE},affected_scopes={})
        saved = c.execute('SELECT transcript,response_text,action_json FROM agent_turn WHERE agent_turn_id=?',(turn,)).fetchone()
        assert packet['readback_text'] not in saved['response_text']
        assert saved['transcript'] == 'Read my writing' and saved['action_json'] == '{}'
        assert c.execute('SELECT result_json FROM agent_step WHERE step_id=?',(step,)).fetchone()[0] is None
        assert counts['agent_turn_redacted'] == 1
        c.commit()


def test_actual_knowledge_delete_erases_published_writing_prompt_snapshot(ocr):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    writing,_,packet = ocr
    db,sid,_,p,_ = writing
    settings = Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False,session_token='writing-native',
                        data_encryption_key=b'T' * 32)
    local = {'X-HSC-Session':settings.session_token}
    binding = {k:packet[k] for k in ('confirmation_id','binding_version')}
    for restarted in (False,True):
        with TestClient(create_app(settings,model_gateway=NoCloud()),raise_server_exceptions=False) as client:
            token = client.post('/v1/parent/'+('unlock' if restarted else 'setup'),headers=local,json={'pin':'2468'}).json()['parent_token']
            if not restarted:
                read = client.get(f'/v1/sessions/{sid}/writing-ocr',headers=local,params=binding)
                assert read.status_code == 200, read.text
                assert read.json()['readback_text'] == packet['readback_text']
            deleted = client.post(f'/v1/parent/children/{CHILD}/delete',headers={**local,'X-HSC-Parent':token},
                json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'writing-native-delete'})
            assert deleted.status_code == 200 and deleted.json()['status']=='completed', deleted.text
            read = client.get(f'/v1/sessions/{sid}/writing-ocr',headers=local,params=binding)
            with db.connect() as c:
                row = c.execute('SELECT status,item_json FROM diagnostic_prompt WHERE prompt_id=?',(p['prompt_id'],)).fetchone()
                assert tuple(row) == ('invalidated','{}')
                assert c.execute("SELECT extracted_text FROM capture_artifact WHERE artifact_id='page'").fetchone()[0] == TEXT
            assert read.status_code == 409 and read.headers['cache-control'] == 'no-store', read.text
