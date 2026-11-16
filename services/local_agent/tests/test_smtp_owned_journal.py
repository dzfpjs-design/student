"""Synthetic v98 migration, Windows-identity decisions and restore fence."""
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from home_study_agent.database import Database, ExternalEffectBusyError, LATEST_SCHEMA_VERSION
from home_study_agent import smtp_owned_journal as journal
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from local_restore_operation_fixture import restore_local
from test_combined_report_loopback_candidate import sink


ID='a'*32
IDENTITY={'pid':4321,'windows_creation_time_100ns':123456789}
MEMBERS=[{**IDENTITY,'role':'worker','image_path':'synthetic-worker.exe'}]
JOB_NAME='Global\\HSC_SMTP_'+'b'*32


def create(tmp_path):
    db=Database(tmp_path/'synthetic.db'); db.initialize(); return db


def insert_pending(db):
    with db.connect() as c:
        c.execute("INSERT INTO smtp_owned_worker_journal VALUES(?,?,?,'pending',NULL,'synthetic','synthetic')",(ID,IDENTITY['pid'],IDENTITY['windows_creation_time_100ns']))
        values,digest=journal._checked_members(IDENTITY,MEMBERS)
        c.execute("INSERT INTO smtp_owned_worker_binding VALUES(?,'standalone',NULL,NULL,NULL,NULL,NULL,NULL,?,?,?)",(ID,JOB_NAME,len(values),digest))
        c.executemany('INSERT INTO smtp_owned_worker_member VALUES(?,?,?,?,?)',[(ID,*item) for item in values])
        c.commit()


def settle_synthetic_wal(db):
    """Make this fixture's committed journal stable before its restart check.

    This controls only a freshly-created test database. Production preflight
    must still reject a WAL which disappears before it can be inspected.
    """
    connection = sqlite3.connect(db.path, timeout=5)
    try:
        busy, frames, checkpointed = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        assert busy == 0 and (frames == -1 or frames == checkpointed)
        # A closed SQLite connection may leave empty sidecars behind until a
        # later checkpoint. For this synthetic fixture, switch to rollback mode
        # only after all frames are confirmed, then verify sidecars are gone.
        assert connection.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() == 'delete'
    finally:
        connection.close()
    wal, shm = (Path(str(db.path) + suffix) for suffix in ('-wal', '-shm'))
    # SQLite can leave an orphan SHM after switching this private fixture to
    # rollback mode. It is safe to remove only after the successful checkpoint
    # and confirmed absence of WAL; never apply this cleanup to user data.
    if not wal.exists() and shm.exists():
        assert not shm.is_symlink() and shm.is_file()
        shm.unlink()
    end = time.monotonic() + 3
    sidecars = [wal, shm]
    while any(path.exists() for path in sidecars) and time.monotonic() < end:
        time.sleep(.02)
    assert not any(path.exists() for path in sidecars), 'synthetic WAL family did not settle'


def test_v97_upgrade_has_backup_and_installs_v98_guard(tmp_path):
    db=create(tmp_path)
    with db.connect() as c:
        c.execute('DROP TABLE smtp_owned_worker_member'); c.execute('DROP TABLE smtp_owned_worker_binding')
        c.execute('DROP TABLE smtp_owned_worker_journal')
        c.execute('DELETE FROM schema_migration WHERE version>=98'); c.commit()
    Database(db.path).initialize()
    status=db.latest_upgrade_status()
    assert status['source_schema_version']==97 and status['target_schema_version']==LATEST_SCHEMA_VERSION and status['snapshot_available']
    with db.connect() as c: assert journal.schema_valid_v99(c)


def test_v98_upgrade_without_pending_has_verified_backup(tmp_path):
    db=create(tmp_path)
    with db.connect() as c:
        c.execute('DROP TABLE smtp_owned_worker_member'); c.execute('DROP TABLE smtp_owned_worker_binding')
        c.execute('DELETE FROM schema_migration WHERE version>=99'); c.commit()
    Database(db.path).initialize()
    status=db.latest_upgrade_status()
    assert (status['source_schema_version'],status['target_schema_version'])==(98,99)
    assert status['snapshot_available']
    with db.connect() as c: assert journal.schema_valid_v99(c)


def test_v98_pending_without_full_members_cannot_be_migrated_away(tmp_path,monkeypatch):
    db=create(tmp_path); insert_pending(db)
    with db.connect() as c:
        c.execute('DROP TABLE smtp_owned_worker_member'); c.execute('DROP TABLE smtp_owned_worker_binding')
        c.execute('DELETE FROM schema_migration WHERE version>=99'); c.commit()
    monkeypatch.setattr(journal,'probe_windows_original',lambda *args:'dead')
    with pytest.raises(ExternalEffectBusyError,match='SMTP_LEGACY_MEMBER_SET_UNCONFIRMED'):
        Database(db.path).initialize()
    with sqlite3.connect(db.path) as c:
        assert c.execute('SELECT max(version) FROM schema_migration').fetchone()[0]==98
        assert c.execute('SELECT status FROM smtp_owned_worker_journal').fetchone()[0]=='pending'


def test_member_deletion_cannot_silently_clear_pending(tmp_path,monkeypatch):
    db=create(tmp_path); insert_pending(db)
    with db.connect() as c: c.execute('DELETE FROM smtp_owned_worker_member'); c.commit()
    monkeypatch.setattr(journal,'recover_named_job',lambda *args:True)
    with pytest.raises(ExternalEffectBusyError):
        with db.external_effect_guard(timeout=0): pass


def test_same_version_damaged_journal_is_not_silently_recreated(tmp_path):
    db=create(tmp_path)
    with db.connect() as c: c.execute('DROP TABLE smtp_owned_worker_member'); c.commit()
    with pytest.raises(ExternalEffectBusyError): Database(db.path).initialize()
    with sqlite3.connect(db.path) as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='smtp_owned_worker_member'").fetchone()


def test_corrupt_pending_status_cannot_silently_bypass_fence(tmp_path):
    db=create(tmp_path); insert_pending(db)
    with sqlite3.connect(db.path) as c:
        c.execute('PRAGMA ignore_check_constraints=ON')
        c.execute("UPDATE smtp_owned_worker_journal SET status='corrupt'")
        c.commit()
    with pytest.raises(ExternalEffectBusyError,match='SMTP_WORKER_JOURNAL_INVALID'):
        with db.external_effect_guard(timeout=0): pass


@pytest.mark.parametrize('pending', [False, True])
def test_recovery_check_closes_its_sqlite_connection_on_success_and_failure(tmp_path, monkeypatch, pending):
    db = create(tmp_path)
    if pending:
        insert_pending(db)
        monkeypatch.setattr(journal, 'recover_named_job', lambda *args: False)
    original_connect = sqlite3.connect
    connections = []

    class TrackedConnection:
        def __init__(self, inner):
            object.__setattr__(self, 'inner', inner)
            object.__setattr__(self, 'closed', False)
        def __getattr__(self, name):
            return getattr(self.inner, name)
        def __setattr__(self, name, value):
            if name in {'inner', 'closed'}:
                object.__setattr__(self, name, value)
            else:
                setattr(self.inner, name, value)
        def __enter__(self):
            self.inner.__enter__()
            return self
        def __exit__(self, *args):
            return self.inner.__exit__(*args)
        def close(self):
            self.closed = True
            self.inner.close()

    def tracked_connect(*args, **kwargs):
        result = TrackedConnection(original_connect(*args, **kwargs))
        connections.append(result)
        return result

    monkeypatch.setattr(journal.sqlite3, 'connect', tracked_connect)
    if pending:
        with pytest.raises(ValueError, match='SMTP_WORKER_RECOVERY_PENDING'):
            journal.check_and_reconcile(db.path)
    else:
        journal.check_and_reconcile(db.path)
    assert len(connections) == 1 and connections[0].closed


def test_pending_worker_restart_without_fixture_checkpoint_stays_blocked(tmp_path, monkeypatch):
    db = create(tmp_path)
    insert_pending(db)
    monkeypatch.setattr(journal, 'recover_named_job', lambda *args: False)
    with pytest.raises(ExternalEffectBusyError, match='SMTP_WORKER_RECOVERY_PENDING'):
        with db.external_effect_guard(timeout=0):
            pass
    # Deliberately no fixture checkpoint here: this covers the production
    # connection-close path rather than the stabilized test fixture below.
    with pytest.raises(ExternalEffectBusyError, match='SMTP_WORKER_RECOVERY_PENDING'):
        Database(db.path).initialize()


@pytest.mark.parametrize('probe',['alive','unknown'])
def test_live_or_unverifiable_original_blocks_every_effect_guard(tmp_path,monkeypatch,probe):
    db=create(tmp_path); insert_pending(db)
    monkeypatch.setattr(journal,'recover_named_job',lambda *args:False)
    with pytest.raises(ExternalEffectBusyError,match='SMTP_WORKER_RECOVERY_PENDING'):
        with db.external_effect_guard(timeout=0): pass
    settle_synthetic_wal(db)
    with pytest.raises(ExternalEffectBusyError): Database(db.path).initialize()
    with sqlite3.connect(db.path) as c:
        assert c.execute('SELECT status,result FROM smtp_owned_worker_journal').fetchone()==('pending',None)


def test_preflight_preserves_worker_busy_error_instead_of_masking_it(tmp_path):
    from home_study_agent.restore_publication import initialization_preflight
    db=create(tmp_path)
    with pytest.raises(ExternalEffectBusyError,match='synthetic worker busy'):
        with initialization_preflight(db):
            raise ExternalEffectBusyError('synthetic worker busy')


def test_dead_original_clears_only_resource_fence_and_preserves_unknown(tmp_path,monkeypatch):
    db=create(tmp_path); insert_pending(db)
    seen=[]
    monkeypatch.setattr(journal,'recover_named_job',lambda name,members,*args:seen.append((name,members)) or True)
    with db.external_effect_guard(timeout=0): pass
    with db.connect() as c:
        assert tuple(c.execute('SELECT status,result FROM smtp_owned_worker_journal').fetchone())==('stopped','unknown')
    assert seen[0][0]==JOB_NAME and seen[0][1][0][:2]==[IDENTITY['pid'],IDENTITY['windows_creation_time_100ns']]


def test_reserve_and_finish_require_guard_and_confirmed_original_exit(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from home_study_agent import smtp_owned_process as owned
    db=create(tmp_path)
    job=SimpleNamespace(name=JOB_NAME,handle=None); process=object()
    monkeypatch.setattr(owned,'_job_members',lambda *args:MEMBERS)
    with pytest.raises(ValueError,match='GUARD_REQUIRED'): journal.reserve(db,ID,IDENTITY,MEMBERS,{'kind':'standalone'},job=job,process=process)
    monkeypatch.setattr(journal,'probe_windows_original',lambda *args:'alive')
    with db.external_effect_guard():
        journal.reserve(db,ID,IDENTITY,MEMBERS,{'kind':'standalone'},job=job,process=process)
        with pytest.raises(ValueError,match='EXIT_UNCONFIRMED'): journal.finish(db,ID,'accepted',job=job)
        monkeypatch.setattr(journal,'probe_windows_original',lambda *args:'dead')
        monkeypatch.setattr(journal,'recover_named_job',lambda *args:True)
        journal.finish(db,ID,'accepted',job=job)
    with db.connect() as c:
        assert tuple(c.execute('SELECT status,result FROM smtp_owned_worker_journal').fetchone())==('stopped','accepted')


def test_old_backup_cannot_replace_pending_live_worker_journal(tmp_path,monkeypatch):
    db=create(tmp_path); privacy=PrivacyLifecycleService(db,tmp_path); privacy.initialize()
    backup=privacy.create_backup()
    insert_pending(db)
    monkeypatch.setattr(journal,'recover_named_job',lambda *args:False)
    with pytest.raises(ExternalEffectBusyError): restore_local(privacy,backup['backup_id'],'确认恢复')
    with sqlite3.connect(db.path) as c:
        assert c.execute('SELECT status,result FROM smtp_owned_worker_journal').fetchone()==('pending',None)


@pytest.mark.parametrize('lose_reply,expected',[(False,'accepted'),(True,'unknown')])
def test_loopback_process_commits_identity_and_result_under_effect_guard(tmp_path,monkeypatch,lose_reply,expected):
    from home_study_agent import smtp_owned_process as owned
    db=create(tmp_path)
    monkeypatch.setattr(owned,'_process_identity',lambda process: {'pid':process.pid,'windows_creation_time_100ns':123456789})
    monkeypatch.setattr(owned,'_job_members',lambda job,process:[{'pid':process.pid,'windows_creation_time_100ns':123456789,'role':'worker','image_path':'synthetic-worker.exe'}])
    monkeypatch.setattr(journal,'probe_windows_original',lambda *args:'dead')
    monkeypatch.setattr(journal,'recover_named_job',lambda *args:True)
    with sink(lose_reply=lose_reply) as smtp:
        process=owned.OwnedSmtpProcess(lambda:db.external_effect_guard(),lambda:True,
            lambda identity,members,job,child:journal.reserve(db,ID,identity,members,{'kind':'standalone'},job=job,process=child),
            lambda result,job:journal.finish(db,ID,result,job=job))
        result=process.submit(host='127.0.0.1',port=smtp.server_address[1],username='',password='',
            sender='sender@example.test',recipient='parent@example.test',
            mime_bytes=b'Subject: synthetic\r\n\r\nmessage\r\n',timeout_seconds=4,use_ssl=False,starttls=False)
        assert result['status']==expected and result['exit_confirmed']
        assert len(smtp.messages)==1
    with db.connect() as c:
        assert tuple(c.execute('SELECT status,result FROM smtp_owned_worker_journal').fetchone())==('stopped',expected)


@pytest.mark.skipif(sys.platform!='win32',reason='Windows process creation identity required')
def test_native_windows_probe_distinguishes_original_alive_dead_and_reused_epoch():
    from home_study_agent.smtp_owned_process import _process_identity
    process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        identity=_process_identity(process); pid=identity['pid']; created=identity['windows_creation_time_100ns']
        assert created>0 and journal.probe_windows_original(pid,created)=='alive'
        assert journal.probe_windows_original(pid,created+1)=='dead'
        process.kill(); process.wait(timeout=10)
        assert journal.probe_windows_original(pid,created)=='dead'
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=10)
