"""Real HTTP playback registration, post-commit clearing, and child isolation."""
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.repository import StudyRepository
from test_companion_activity_host import NoCloud
from test_native_source_privacy_cleanup import native, CHILD, PROMPT


def test_actual_http_registered_playback_clears_only_after_commit_and_keeps_other_child(native, monkeypatch):
    db,_,_,_,sid = native
    settings = Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False,session_token='playback-cleanup', data_encryption_key=b'0'*32)
    local = {'X-HSC-Session':settings.session_token}
    with TestClient(create_app(settings,model_gateway=NoCloud()),raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup',headers=local,json={'pin':'2468'}).json()['parent_token']
        before = client.post(f'/v1/sessions/{sid}/voice-playback',headers=local,
            json={'playback_id':'source-readback','text':PROMPT,'event':'started'})
        assert before.status_code == 200 and before.json()['registered'], before.text
        DocumentMonitoringService(db)
        other = StudyRepository(db).switch_active_session_child(sid,'child-junior-3','playback-other')['session_id']
        another = client.post(f'/v1/sessions/{other}/voice-playback',headers=local,
            json={'playback_id':'other-readback','text':'Other child retained playback','event':'started'})
        assert another.status_code == 200 and another.json()['registered'], another.text
        guard = client.app.state.agent_orchestrator.voice_playback
        assert guard.entries[(sid,'source-readback')]['child_id'] == CHILD
        assert guard.entries[(other,'other-readback')]['child_id'] == 'child-junior-3'
        saved = {key:dict(value) for key,value in guard.entries.items()}
        def fail(*args, **kwargs):
            assert kwargs['connection'].in_transaction
            raise ValueError('synthetic rollback before ephemeral clear')
        request = {'scope':'knowledge','confirmation':'确认删除','client_action_id':'delete-playback-source'}
        with monkeypatch.context() as scoped:
            scoped.setattr(client.app.state.privacy_lifecycle,'reconcile_source_deletion',fail)
            failed = client.post(f'/v1/parent/children/{CHILD}/delete',headers={**local,'X-HSC-Parent':token},json=request)
        assert failed.json()['code'] == 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE', failed.text
        assert failed.json()['retryable']
        assert guard.entries == saved
        with db.connect() as c:
            job = c.execute("SELECT deletion_id,status FROM deletion_job WHERE client_action_id='delete-playback-source'").fetchone()
            assert job['status'] == 'failed'
        completed = client.post(f'/v1/parent/children/{CHILD}/delete',headers={**local,'X-HSC-Parent':token},json=request)
        assert completed.status_code == 200 and completed.json()['status'] == 'completed', completed.text
        assert completed.json()['deletion_id'] == job['deletion_id']
        assert (sid,'source-readback') not in guard.entries
        assert guard.entries[(other,'other-readback')] == saved[(other,'other-readback')]
        # A delayed transport callback must not recreate the erased echo cache.
        for event, text in [('started', PROMPT), ('started', 'Changed late playback text'), ('ended', PROMPT)]:
            replayed = client.post(f'/v1/sessions/{sid}/voice-playback',headers=local,
                json={'playback_id':'source-readback','text':text,'event':event})
            assert replayed.status_code == 200, replayed.text
            assert replayed.json() == {'playback_id':'source-readback','registered':False}
            assert (sid,'source-readback') not in guard.entries
            assert not guard.matches(sid,'source-readback',PROMPT)
            assert guard.entries[(other,'other-readback')] == saved[(other,'other-readback')]
        assert (sid,'source-readback') in guard.revoked_playbacks
