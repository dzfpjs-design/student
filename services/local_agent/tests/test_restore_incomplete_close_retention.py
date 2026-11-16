"""Real App/probe flow, original close seams, no Windows/native closure claim."""
from pathlib import Path
import sys
import pytest
from home_study_agent import restore_publication as pub,registered_asset_snapshot as snap
from home_study_agent import restore_incomplete_protection as protection
from test_restore_incomplete_protection import seed,request,no_network
from test_manual_standard_scope import client,auth
from synthetic_native_restore_request import KEY


def frames(error):
    trace=error.__traceback__
    while trace is not None:
        yield trace.tb_frame
        trace=trace.tb_next


@pytest.mark.parametrize('fault',['output_close','win_style_close','unknown_io'])
def test_real_probe_unknown_resources_retain_original_traceback_once_and_deny_abort(tmp_path,monkeypatch,fault):
    db,life,pdf=seed(tmp_path)
    cm=client(tmp_path,db,data_encryption_key=b's'*32,native_launch_attestation_key=KEY)
    c=cm.__enter__();errors=[];resources=[];closes=[];shutdown=[]
    try:
        auth(c);backup=life.create_backup(prune=False);pdf.unlink()
        original_open=Path.open
        original_enter=snap.HeldFile.__enter__
        if fault=='output_close':
            class OriginalOutput:
                def __init__(self,original):self.original=original
                def __enter__(self):return self
                def write(self,raw):return self.original.write(raw)
                def flush(self):return self.original.flush()
                def fileno(self):return self.original.fileno()
                def __exit__(self,*args):
                    closes.append(self)
                    self.original.close()  # Real close executes, but its ACK is lost.
                    error=OSError('PRIVATE_OUTPUT_CLOSE_ACK_UNKNOWN')
                    errors.append(error);raise error
            def open_original(path,*args,**kwargs):
                original=original_open(path,*args,**kwargs)
                mode=args[0]if args else kwargs.get('mode','r')
                if mode=='xb'and path.name=='standard.pdf'and any(p.startswith('.registered-working-')for p in path.parts):
                    output=OriginalOutput(original);resources.append(output);return output
                return original
            monkeypatch.setattr(Path,'open',open_original)
        elif fault=='win_style_close':
            class DLL:
                def CloseHandle(self,handle):closes.append(handle);return 0
            kernel=object.__new__(snap._CheckedWin32);kernel.dll=DLL();handle=67890
            resources.extend([kernel,handle])
            def enter_original(held):
                result=original_enter(held)
                frame=sys._getframe(1);in_probe=False
                try:
                    while frame is not None:
                        if frame.f_code is protection._RepairProbe.ordinary.__code__:in_probe=True;break
                        frame=frame.f_back
                finally:del frame
                if in_probe and held.root==tmp_path/'standards'and held.parts==['manifest.json']:
                    def close_same(h):
                        try:kernel.close(h)  # Exact original Win-style API/callback.
                        except BaseException as error:errors.append(error);raise
                    held.stack.callback(close_same,handle)
                return result
            monkeypatch.setattr(snap.HeldFile,'__enter__',enter_original)
        else:
            def inaccessible(parent,name):
                error=PermissionError('PRIVATE_PROBE_IO_UNKNOWN');errors.append(error);raise error
            monkeypatch.setattr(protection,'_absent',inaccessible)
        result=request(c,backup)
        assert result.status_code==409,result.text
        assert result.json()['code']in{'RESTORE_CURRENT_PROTECTION_UNCONFIRMED','RESTORE_PUBLICATION_RECOVERY_REQUIRED'}
        assert 'PRIVATE_'not in result.text
        assert len(errors)==1
        error=errors[0];gate=pub._gate(db.path)
        assert gate.poisoned,'ORIGINAL_RESOURCE_UNKNOWN_WAS_NOT_QUARANTINED'
        records=[r for r in gate.retained if r[0]=='incomplete_current_io'and r[2]is error]
        assert len(records)==1,'SAME_ORIGINAL_ERROR_AND_PROTECTION_NOT_RETAINED'
        retained=records[0][1]
        assert retained.owner.gate is gate and retained.work.request_id
        if fault=='output_close':
            assert len(resources)==1 and closes==resources
            assert any(frame.f_locals.get('out')is resources[0]for frame in frames(error))
            assert any(frame.f_locals.get('self')is resources[0]for frame in frames(error))
        elif fault=='win_style_close':
            assert closes==[handle]
            assert type(error)is snap.AssetSnapshotError and error.args==('HANDLE_CLOSE_UNCONFIRMED',)
            assert any(frame.f_locals.get('self')is kernel and frame.f_locals.get('handle')==handle for frame in frames(error))
        with pytest.raises(pub.RestorePublicationError):
            with pub.publication_owner(db):pytest.fail('POISONED_OWNER_REOPENED_FOR_ABORT')
        assert gate.poisoned and len(errors)==1
        assert not list((tmp_path/'backups').glob('.registered-working-*'))
    finally:
        # These tests intentionally retain unknown responsibility. App shutdown
        # may reject SQL admission; neither process exit nor fd.closed is ACK.
        try:cm.__exit__(None,None,None)
        except pub.RestorePublicationError as error:shutdown.append(str(error))
    assert len(errors)==1
    if fault=='output_close':assert closes==resources
    if fault=='win_style_close':assert closes==[handle]
    assert pub._gate(db.path).poisoned
