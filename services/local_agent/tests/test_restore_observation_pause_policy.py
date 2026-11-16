"""Real synthetic ASGI/SQLite restore; no Main edits, physical devices or external model."""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post, observe_original_publication
from pathlib import Path
from contextlib import ExitStack
from uuid import uuid4
import hashlib,json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
class NoCloud:
    async def complete(self,*args,**kwargs):
        raise AssertionError("No model call is authorized in this synthetic probe")
def client_for(path, *, native_launch_attestation_key=None, raise_server_exceptions=True):
    return TestClient(create_app(Settings(data_dir=path,database_path=path/"synthetic.db",data_encryption_key=b"0"*32,
        maintenance_enabled=False,agent_runtime_enabled=False,native_launch_attestation_key=native_launch_attestation_key),model_gateway=NoCloud()),raise_server_exceptions=raise_server_exceptions)
def workspace(client,sid):
    response=client.get(f"/v1/sessions/{sid}")
    assert response.status_code==200,response.status_code
    return response.json()["session"]
def checked_post(client,path,body,headers=None,expected=200,*,native_restore=False,proof_out=None):
    response=(signed_local_restore_post(client,json=body,headers=headers,proof_out=proof_out) if native_restore else
        client.post(path,json=body,headers=headers))
    assert response.status_code==expected,(path,response.status_code,response.text[:200])
    return response.json()
@pytest.mark.parametrize("portable",[False,True],ids=["local","portable"])
def test_latest_pause_is_not_restored_to_enabled_without_a_new_explicit_camera_intent(tmp_path,portable):
    with client_for(tmp_path,native_launch_attestation_key=KEY if not portable else None) as client:
        token=checked_post(client,"/v1/parent/setup",{"pin":"4286"})["parent_token"]
        headers={"X-HSC-Parent":token}
        sid=checked_post(client,"/v1/sessions",{"child_id":"child-primary-4","client_action_id":"synthetic-policy-session"},expected=201)["session_id"]
        saved=workspace(client,sid)
        assert saved["status"]=="active" and saved["observation_status"]=="enabled"
        backup=checked_post(client,"/v1/parent/backups"+("/portable" if portable else ""),
            {"passphrase":"synthetic policy passphrase"} if portable else None,headers)
        checked_post(client,f"/v1/sessions/{sid}/observation/pause",{"client_action_id":"synthetic-policy-latest-camera-pause"})
        assert workspace(client,sid)["observation_status"]=="paused"
        # End via actual allowed session transition APIs. No SQL fixture mutation,
        # altered clock, fabricated approval, request to resume, or live device.
        stages=["check_in","classroom_recall","homework_capture","plan_review","study","reflection","evolving","completed"]
        for index,(old,new) in enumerate(zip(stages,stages[1:])):
            if old=="classroom_recall":
                checked_post(client,f"/v1/sessions/{sid}/recalls/none",{"client_action_id":"synthetic-policy-no-recall"})
            checked_post(client,f"/v1/sessions/{sid}/transition",{"expected_state":old,"target_state":new,
                "reason":"synthetic isolated session closure","client_action_id":f"synthetic-policy-transition-{index}"})
        before=workspace(client,sid)
        assert before["status"]=="completed" and before["observation_status"]=="paused"
        generation=client.get("/v1/parent/privacy-operations/generation",headers=headers).json()["generation"]
        operation={"request_id":str(uuid4()),"expected_generation":generation,"cleanup_manifest_sha256":hashlib.sha256(b"synthetic exact cleanup keys").hexdigest()}
        body=({"path":backup["path"],"passphrase":"synthetic policy passphrase","confirmation":"确认迁移恢复"} if portable else
            {"backup_id":backup["backup_id"],"confirmation":"确认恢复"})
        checked_post(client,"/v1/parent/backups/"+("portable/restore" if portable else "restore"),dict(body,operation=operation),headers,native_restore=not portable)
        after=workspace(client,sid)
    with client_for(tmp_path) as restarted:
        again=workspace(restarted,sid)
    witness={"scope":"synthetic actual ASGI HTTP SQLite only","portable":portable,
        "saved":{"status":saved["status"],"observation_status":saved["observation_status"]},
        "before_restore":{"status":before["status"],"observation_status":before["observation_status"]},
        "after_restore":{"status":after["status"],"observation_status":after["observation_status"]},
        "after_application_restart":{"status":again["status"],"observation_status":again["observation_status"]},
        "generation_rotated":after["observation_generation"]!=saved["observation_generation"],
        "explicit_post_restore_camera_resume_calls":0,"direct_SQL_mutations":0,
        "family_data_read":False,"Windows_or_physical_camera_opened":False,
        "candidate_policy":"Data restore alone must not authorize camera restart; restored sessions remain paused until a new explicit camera resume intent"}
    (tmp_path/(("portable" if portable else "local")+"-witness.json")).write_text(json.dumps(witness,ensure_ascii=False,indent=2)+"\n")
    assert (after["observation_status"],again["observation_status"])==("paused","paused")


def parent(client):
    return {"X-HSC-Parent":checked_post(client,"/v1/parent/setup",{"pin":"4286"})["parent_token"]}
def session(client,child="child-primary-4",action="boundary-session-start"):
    return checked_post(client,"/v1/sessions",{"child_id":child,"client_action_id":action},expected=201)["session_id"]
def close(client,sid):
    stages=["check_in","classroom_recall","homework_capture","plan_review","study","reflection","evolving","completed"]
    for index,(old,new) in enumerate(zip(stages,stages[1:])):
        if old=="classroom_recall":
            checked_post(client,f"/v1/sessions/{sid}/recalls/none",{"client_action_id":sid+"-no-recall"})
        checked_post(client,f"/v1/sessions/{sid}/transition",{"expected_state":old,"target_state":new,
            "reason":"synthetic boundary closure","client_action_id":sid+f"-transition-{index}"})
def backup(client,headers,portable):
    return checked_post(client,"/v1/parent/backups"+("/portable" if portable else ""),
        {"passphrase":"synthetic boundary passphrase"} if portable else None,headers)
def restore(client,headers,saved,portable,expected=200,*,proof_out=None,operation_out=None):
    generation=client.get("/v1/parent/privacy-operations/generation",headers=headers).json()["generation"]
    operation={"request_id":str(uuid4()),"expected_generation":generation,
        "cleanup_manifest_sha256":hashlib.sha256(b"synthetic bounded restore").hexdigest()}
    if operation_out is not None:operation_out.append(dict(operation))
    body=({"path":saved["path"],"passphrase":"synthetic boundary passphrase","confirmation":"确认迁移恢复"} if portable else
        {"backup_id":saved["backup_id"],"confirmation":"确认恢复"})
    return checked_post(client,"/v1/parent/backups/"+("portable/restore" if portable else "restore"),dict(body,operation=operation),headers,expected=expected,native_restore=not portable,proof_out=proof_out)

@pytest.mark.parametrize("portable",[False,True],ids=["local","portable"])
def test_first_live_copy_paused_backup_immutable_old_intent_rejected_and_new_explicit_intent_works(tmp_path,monkeypatch,portable):
    with client_for(tmp_path,native_launch_attestation_key=KEY if not portable else None) as client:
        headers=parent(client);sid=session(client)
        old=workspace(client,sid)
        saved=backup(client,headers,portable)
        before_hash=hashlib.sha256(Path(saved["path"]).read_bytes()).hexdigest()
        checked_post(client,f"/v1/sessions/{sid}/observation/pause",{"client_action_id":"boundary-newer-pause"})
        close(client,sid)
        first_publication=[]
        def snapshot(c):return c.execute("SELECT observation_status FROM daily_session WHERE session_id=?",(sid,)).fetchone()[0]
        with observe_original_publication(monkeypatch,client.app.state.repository.database,snapshot,first_publication):
            restore(client,headers,saved,portable)
        fresh=workspace(client,sid)
        assert first_publication==["paused"]
        assert fresh["status"]=="active" and fresh["observation_status"]=="paused"
        assert fresh["observation_revision"]==old["observation_revision"]
        assert fresh["observation_generation"]!=old["observation_generation"]
        assert hashlib.sha256(Path(saved["path"]).read_bytes()).hexdigest()==before_hash
        stale=client.post(f"/v1/sessions/{sid}/observation/resume",json={"client_action_id":"boundary-old-intent",
            "expected_revision":old["observation_revision"],"expected_generation":old["observation_generation"]})
        assert stale.status_code==409 and stale.json()["code"]=="OBSERVATION_CONTROL_GENERATION_CHANGED"
        legacy=client.post(f"/v1/sessions/{sid}/observation/resume",json={"client_action_id":"boundary-legacy-no-version"})
        assert legacy.status_code==409 and legacy.json()["code"]=="OBSERVATION_CONTROL_REVISION_REQUIRED"
        resumed=checked_post(client,f"/v1/sessions/{sid}/observation/resume",{"client_action_id":"boundary-new-explicit-intent",
            "expected_revision":fresh["observation_revision"],"expected_generation":fresh["observation_generation"]})
        assert resumed["observation_status"]=="enabled" and resumed["observation_revision"]==fresh["observation_revision"]+2
        assert resumed["observation_control"]["fresh"] is True

def test_portable_into_fresh_target_pauses_all_restored_children_without_existing_target_sessions(tmp_path):
    source=tmp_path/"source";source.mkdir()
    target=tmp_path/"target";target.mkdir()
    with client_for(source) as client:
        headers=parent(client)
        primary=session(client);close(client,primary)
        junior=session(client,"child-junior-3","boundary-other-child-start")
        saved=backup(client,headers,True)
        before_hash=hashlib.sha256(Path(saved["path"]).read_bytes()).hexdigest()
    with client_for(target) as client:
        headers=parent(client)
        with client.app.state.repository.database.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM daily_session").fetchone()[0]==0
        restore(client,headers,saved,True)
        first=workspace(client,primary);second=workspace(client,junior)
        assert (first["child_id"],second["child_id"])==("child-primary-4","child-junior-3")
        assert first["status"]=="completed" and second["status"]=="active"
        assert first["observation_status"]==second["observation_status"]=="paused"
        assert hashlib.sha256(Path(saved["path"]).read_bytes()).hexdigest()==before_hash
    with client_for(target) as restarted:
        assert workspace(restarted,primary)["observation_status"]==workspace(restarted,junior)["observation_status"]=="paused"

@pytest.mark.parametrize("portable",[False,True],ids=["local","portable"])
@pytest.mark.parametrize("paused",[False,True],ids=["prior-enabled","prior-paused"])
@pytest.mark.parametrize("failure",["migration","audit"])
def test_failed_postcopy_restore_rolls_back_original_observation_status_without_changing_backup(tmp_path,monkeypatch,portable,paused,failure):
    with ExitStack()as lifecycle:
        client=lifecycle.enter_context(client_for(tmp_path,native_launch_attestation_key=KEY if not portable else None,raise_server_exceptions=False))
        headers=parent(client);sid=session(client)
        saved=backup(client,headers,portable)
        source_hash=hashlib.sha256(Path(saved["path"]).read_bytes()).hexdigest()
        if paused:
            checked_post(client,f"/v1/sessions/{sid}/observation/pause",{"client_action_id":"boundary-before-failed-pause"})
        close(client,sid)
        before=workspace(client,sid)
        if failure=="migration":
            original=Database.initialize;calls=[]
            def fail_first_live(db,*args,**kwargs):
                if (getattr(db,"_restore_work_capability",None)is not None and db.path==db._restore_work_capability.work/"database.db" and db._restore_work_capability.owner.path==tmp_path/"synthetic.db"):
                    calls.append(True)
                    if len(calls)==1:raise RuntimeError("synthetic first live initialize failure")
                return original(db,*args,**kwargs)
            monkeypatch.setattr(Database,"initialize",fail_first_live)
        else:
            original=PrivacyLifecycleService._audit;faults=[]
            def fail_restore_audit(connection,actor,action,*args,**kwargs):
                if action in {"backup_restored","portable_backup_restored"}:
                    faults.append(action)
                    raise RuntimeError("synthetic restore audit failure")
                return original(connection,actor,action,*args,**kwargs)
            monkeypatch.setattr(PrivacyLifecycleService,"_audit",staticmethod(fail_restore_audit))
        proofs=[];original_operations=[]
        failed=restore(client,headers,saved,portable,expected=500,proof_out=proofs,operation_out=original_operations)
        assert failed["code"]=="RESTORE_OPERATION_INTERRUPTED"
        if failure=="migration":assert calls==[True]
        else:assert faults==["portable_backup_restored"if portable else"backup_restored"]
        if not portable:
            assert failed['retryable']is False and failed['recovery_action']=='review_original_operation'
            assert len(proofs)==1
            state=client.get('/v1/parent/recovery-status',headers=headers)
            assert state.status_code==200 and state.json()['state']=='UNAPPLIED',state.text
            result=client.post('/v1/parent/native-privacy-permits/fence',headers=headers,json=proofs[0])
            assert result.status_code==200 and result.json()['status']=='failed',result.text
            lifecycle.close()
            client=lifecycle.enter_context(client_for(tmp_path,native_launch_attestation_key=KEY))
        else:
            # The original portable request has Parent-session authority, not a
            # native nonce. Close the old App and recover the same durable ID
            # through the existing authenticated Parent finalize endpoint.
            assert failed['retryable']is False and failed['recovery_action']=='review_original_operation'
            assert len(original_operations)==1 and proofs==[]
            original_operation=original_operations[0]
            lifecycle.close()
            client=lifecycle.enter_context(client_for(tmp_path))
            headers={"X-HSC-Parent":checked_post(client,"/v1/parent/unlock",{"pin":"4286"})['parent_token']}
            state=client.get('/v1/parent/recovery-status',headers=headers)
            assert state.status_code==200 and state.json()['state']=='UNAPPLIED',state.text
            assert state.json()['request_id']==original_operation['request_id'] and state.json()['kind']=='portable_restore'
            result=client.post('/v1/parent/privacy-operations/'+original_operation['request_id']+'/finalize',headers=headers)
            assert result.status_code==200,result.text
            receipt=result.json()
            assert receipt['request_id']==original_operation['request_id'] and receipt['kind']=='portable_restore'
            assert receipt['status']=='failed' and receipt['cleanup_allowed']is False
            assert receipt['origin_generation']==receipt['current_generation']==original_operation['expected_generation'] and receipt['result_generation']is None
            lifecycle.close()
            client=lifecycle.enter_context(client_for(tmp_path))
        after=workspace(client,sid)
        assert after["status"]==before["status"]=="completed"
        assert after["observation_status"]==before["observation_status"]==("paused" if paused else "enabled")
        assert after["observation_revision"]==before["observation_revision"]
        assert hashlib.sha256(Path(saved["path"]).read_bytes()).hexdigest()==source_hash
        with client.app.state.repository.database.connect() as c:
            assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
            assert not c.execute("PRAGMA foreign_key_check").fetchall()

@pytest.mark.parametrize("portable",[False,True],ids=["local","portable"])
def test_live_session_restore_rejected_before_any_pause_or_generation_mutation(tmp_path,portable):
    with client_for(tmp_path,native_launch_attestation_key=KEY if not portable else None) as client:
        headers=parent(client);sid=session(client)
        saved=backup(client,headers,portable);before=workspace(client,sid)
        failed=restore(client,headers,saved,portable,expected=409)
        assert failed["code"]=="RESTORE_SESSION_ACTIVE"
        after=workspace(client,sid)
        assert after["observation_status"]==before["observation_status"]=="enabled"
        assert after["observation_revision"]==before["observation_revision"]
        assert after["observation_generation"]==before["observation_generation"]
