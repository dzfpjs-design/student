"""Finalizing an original deletion does not acquire unrelated restore authority."""
from contextlib import contextmanager
from pathlib import Path
import inspect,sys,threading
import pytest
from test_privacy_operation_recovery_http import (
    test_completed_deletion_finish_failure_is_visible_and_retry_only_finalizes_original_job as _original_delete_case,
    auth as _auth,
)
from test_parent_owned_portable_recovery import exported_source as _source, request as _restore_request
from test_parent_owned_portable_restore import parent_only as _parent_only
from test_app import client_for
from home_study_agent.database import Database
from home_study_agent.pet_speaking_candidate_jobs import SpeakingCandidateJobs
from home_study_agent import restore_publication as pub,parent_restore_workflow as workflow,parent_restore_parent as parent


def test_original_delete_finish_and_idempotent_retry_with_real_background_shared_reader(tmp_path,monkeypatch):
    connect=Database.connect;claim=SpeakingCandidateJobs._claim
    code=connect.__wrapped__.__code__;target_code=_original_delete_case.__code__
    lines,start=inspect.getsourcelines(connect.__wrapped__)
    yield_line=start+next(i for i,s in enumerate(lines)if s.strip()=='yield connection')
    lines,start=inspect.getsourcelines(_original_delete_case)
    request_line=start+next(i for i,s in enumerate(lines)if "retried = restarted.post('/v1/parent/privacy-operations/"in s)
    armed=threading.Event();entered=threading.Event();release=threading.Event();closed=threading.Event();held=[None];facts={}
    def trace(frame,kind,arg):
        if frame.f_code is target_code:
            if kind=='line'and frame.f_lineno==request_line:
                armed.set();assert entered.wait(15),'original background claim did not reach its original shared reader'
            if kind=='line'and frame.f_lineno==request_line+1:
                facts['status']=frame.f_locals['retried'].status_code;release.set();assert closed.wait(15)
            return trace
        if frame.f_code is code:
            if kind=='line'and frame.f_lineno==yield_line and armed.is_set()and held[0]is None and frame.f_locals['self'].path==tmp_path/'test.db':
                caller=frame.f_back
                while caller is not None and caller.f_code is not claim.__code__:caller=caller.f_back
                if caller is not None:
                    held[0]=frame;entered.set();assert release.wait(45)
            if kind=='return'and arg is None and frame is held[0]:closed.set()
            return trace
        return None
    old=sys.gettrace();old_thread=getattr(threading,'gettrace',lambda:None)()
    threading.settrace(trace);sys.settrace(trace)
    try:_original_delete_case(tmp_path,monkeypatch)
    finally:
        release.set();sys.settrace(old);threading.settrace(old_thread)
    assert entered.is_set()and closed.is_set()and facts=={'status':200}
    assert Database.connect is connect and SpeakingCandidateJobs._claim is claim


def _pending_parent(tmp_path,monkeypatch,point):
    # This is the original real source/restore fault setup, with no alternate SQL.
    external,items=_source(tmp_path);root=tmp_path/'target';root.mkdir(mode=0o700)
    return external,root


@pytest.mark.parametrize('point',('before_prepare_journal','after_backup'))
def test_stale_false_dispatch_cannot_bypass_original_active_publication_reader_gate(tmp_path,monkeypatch,point):
    import asyncio
    external,root=_pending_parent(tmp_path,monkeypatch,point);seen=[]
    with _parent_only(root)as client:
        headers=_auth(client,True)
        async def quiesce():
            tasks=[t for t in asyncio.all_tasks()if t.get_coro().__qualname__=='VisualCaptureProducer.maintain']
            assert len(tasks)==1;tasks[0].cancel()
            try:await tasks[0]
            except asyncio.CancelledError:pass
        client.portal.call(quiesce)
        def fault(where):
            if where==point and not seen:seen.append(where);raise pub.RestorePublicationError('PUBLIC_PARENT_FAULT')
        with monkeypatch.context()as patch:
            patch.setattr(pub,'_fault',fault)
            operation,result=_restore_request(client,headers,external)
            assert result.status_code==409 and seen==[point],result.text
            patch.setattr(pub,'_fault',lambda where:None)
            database=client.app.state.privacy_lifecycle.database
            assert workflow.has_active_publication(database)
            calls=[]
            def stale_false(db):
                assert db is database;calls.append(True);return False
            def forbidden(*args,**kwargs):raise AssertionError('a rejected classification reader must not fall back to owner recovery')
            patch.setattr(workflow,'has_active_publication',stale_false)
            patch.setattr(parent,'finalize_original',forbidden)
            failed=client.post('/v1/parent/privacy-operations/'+operation['request_id']+'/finalize',headers=headers)
            assert failed.status_code==409 and failed.json()['code']=='RESTORE_PUBLICATION_RECOVERY_REQUIRED',failed.text
            assert len(calls)==2 # middleware advisory then classification advisory; ordinary reader is authoritative


def test_classification_preserves_original_parent_authority_schema_rejection(tmp_path):
    with client_for(tmp_path)as client:
        headers=_auth(client,True)
        with Database(tmp_path/'test.db').connect()as c:
            c.execute('ALTER TABLE restore_parent_authority ADD COLUMN synthetic_untrusted_shape TEXT');c.commit()
        response=client.post('/v1/parent/privacy-operations/unknown-original/finalize',headers=headers)
        assert response.status_code==409 and response.json()['code']=='RESTORE_PUBLICATION_RECOVERY_REQUIRED',response.text
