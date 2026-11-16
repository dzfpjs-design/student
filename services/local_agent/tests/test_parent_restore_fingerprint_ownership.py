"""Actual synthetic native claim/restore owner and local filesystem; no device credit."""
from contextlib import contextmanager,closing
import copy,hashlib,os,socket,sqlite3,threading,zipfile
from uuid import uuid4
import pytest
from home_study_agent.database import Database
from home_study_agent import native_privacy as native,restore_publication as pub,parent_restore_ownership as own
from home_study_agent.privacy_operation_receipt import PrivacyOperationReceipts,initialize


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:(_ for _ in()).throw(AssertionError('NETWORK_FORBIDDEN')))


@contextmanager
def original_work(tmp_path):
    db=Database(tmp_path/'original.db');db.initialize();receipts=PrivacyOperationReceipts(db)
    with db.connect()as c:initialize(c);native.ensure_schema(c);c.commit()
    operation={'request_id':'public-'+uuid4().hex,'expected_generation':receipts.current()['generation'],'cleanup_manifest_sha256':'c'*64}
    route='/v1/parent/backups/restore';body={'operation':operation,'backup_id':'public','confirmation':'public'}
    metadata={'nonce':uuid4().hex,'request_id':operation['request_id'],'expected_generation':operation['expected_generation'],
      'kind':'restore','child_id':None,'scope':None,'command_sha256':native.command_hash(route,body),'cleanup_manifest_sha256':'c'*64}
    with db.connect()as c:
        c.execute('BEGIN IMMEDIATE');native.claim(c,metadata,route,body,'restore');c.commit()
    receipts.reserve(operation,'restore','public',source_sha256='a'*64)
    with pub.publication_owner(db,timeout=0)as owner:
        work=own.begin(owner,operation['request_id'])
        try:yield work
        finally:
            if not owner.gate.poisoned:
                receipts.failed(operation['request_id'])
                with db.connect()as c:native.finish(c,metadata,'failed');c.commit()
                own.cleanup(owner,metadata);own.finish(owner,metadata)
                assert not work.work.exists()and not work.prepared.exists()
                assert own.current(owner)is None


def test_original_32_ordinary_slots_plus_exact_three_typed_and_real_cleanup(tmp_path):
    with original_work(tmp_path)as work:
        directory=work.work/'upgrade-backups';work.directory(directory)
        count=len(work._value()['members']['entries'])
        for i in range(own.MAX_MEMBERS-count):
            work.add_file(directory/f'pre-upgrade-v1-to-v2-20261023T000000000000Z-{i:08x}.db',family=True)
        assert len(work._value()['members']['entries'])==32
        for kind in('source','target','derived'):work.fingerprint_core(kind)
        value=work._value();assert len(value['members']['entries'])==35
        assert own._decode(copy.deepcopy(value))==value
        with pytest.raises(pub.RestorePublicationError):
            work.add_file(directory/'pre-upgrade-v1-to-v2-20261023T000000000000Z-ffffffff.db',family=True)
        # A basename in another directory never receives the typed allowance.
        with pytest.raises(pub.RestorePublicationError):work.add_file(directory/'fingerprint-source.db',family=True)
        assert not (directory/'fingerprint-source.db').exists()
        for entry in value['members']['entries']:
            if entry['relative'].endswith('/fingerprint-source.db'):
                entry['relative']=value['binding']['work_name']+'/upgrade-backups/fingerprint-source.db';break
        with pytest.raises(pub.RestorePublicationError):own._decode(value)


def test_exact_db_family_and_create_new_derived_only(tmp_path):
    with original_work(tmp_path)as work:
        for kind,bad in(('source',False),('target',False),('derived',True)):
            path=work.work/own.FINGERPRINT_CORES[kind]
            with pytest.raises(pub.RestorePublicationError):work.add_file(path,family=bad)
            assert not path.exists()
        source=work.fingerprint_core('source');assert work.fingerprint_core('source')==source
        derived=work.fingerprint_core('derived');assert derived.exists()
        with pytest.raises(pub.RestorePublicationError):work.fingerprint_core('derived')
        with pytest.raises(pub.RestorePublicationError):work.reset_fingerprint_core('derived')
        value=work._value()
        next(e for e in value['members']['entries']if e['relative'].endswith('/fingerprint-source.db'))['family']=False
        with pytest.raises(pub.RestorePublicationError):own._decode(value)


def test_reset_reuses_original_id_and_source_stream_is_exact_bounded(tmp_path):
    with original_work(tmp_path)as work:
        source=work.fingerprint_core('source');identity=own._identity(work.owner,source)
        source.write_bytes(b'old bytes')
        assert work.reset_fingerprint_core('source')==source and source.read_bytes()==b''
        assert own._identity(work.owner,source)==identity
        path,facts=work.stream_fingerprint_source(iter([b'abc',b'def']),6)
        assert facts==(hashlib.sha256(b'abcdef').hexdigest(),6)and path.read_bytes()==b'abcdef'
        assert own._identity(work.owner,source)==identity
        with pytest.raises(pub.RestorePublicationError):work.stream_fingerprint_source([b'abcdefg'],6)
        with pytest.raises(pub.RestorePublicationError):work.stream_fingerprint_source([b'a'],2)
        with pytest.raises(pub.RestorePublicationError):work.stream_fingerprint_source([],64*1024*1024+1)
        assert work.reset_fingerprint_core('source').stat().st_size==0


@pytest.mark.parametrize('suffix',['-wal','-shm','-journal'])
def test_reset_refuses_sidecar_before_truncation(tmp_path,suffix):
    with original_work(tmp_path)as work:
        source=work.fingerprint_core('source');source.write_bytes(b'original untouched')
        sidecar=source.with_name(source.name+suffix);sidecar.write_bytes(b'synthetic auxiliary')
        with pytest.raises(pub.RestorePublicationError):work.reset_fingerprint_core('source')
        assert source.read_bytes()==b'original untouched'
        sidecar.unlink();assert work.reset_fingerprint_core('source').stat().st_size==0


def test_reset_refuses_replaced_identity_without_changing_replacement(tmp_path):
    with original_work(tmp_path)as work:
        source=work.fingerprint_core('source');source.write_bytes(b'original')
        saved=source.with_name('original-temporarily-held');source.rename(saved);source.write_bytes(b'replacement')
        try:
            with pytest.raises(pub.RestorePublicationError):work.reset_fingerprint_core('source')
            assert source.read_bytes()==b'replacement'
        finally:source.unlink();saved.rename(source)


def test_actual_sqlite_wal_and_live_sql_refuse_reset_then_clean_core_reopens(tmp_path):
    with original_work(tmp_path)as work:
        target=work.fingerprint_core('target')
        with pub._sql(target,owner=work.owner,timeout=0)as c:
            c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE public_value(value)');c.execute('INSERT INTO public_value VALUES(7)');c.commit()
            with pytest.raises(pub.RestorePublicationError):work.reset_fingerprint_core('target')
            assert c.execute('SELECT value FROM public_value').fetchone()[0]==7
        # Closing SQL alone does not authorize deleting a still-present family.
        if any(target.with_name(target.name+s).exists()for s in('-wal','-shm','-journal')):
            with pytest.raises(pub.RestorePublicationError):work.reset_fingerprint_core('target')
        source=work.fingerprint_core('source');identity=own._identity(work.owner,source)
        with pub._sql(source,owner=work.owner,timeout=0)as c:
            c.execute('CREATE TABLE public_value(value)');c.execute('INSERT INTO public_value VALUES(9)');c.commit()
            with pytest.raises(pub.RestorePublicationError):work.reset_fingerprint_core('source')
            assert c.execute('SELECT value FROM public_value').fetchone()[0]==9
        assert work.reset_fingerprint_core('source').stat().st_size==0
        assert own._identity(work.owner,source)==identity
        with pub._sql(source,owner=work.owner,timeout=0)as c:
            c.execute('CREATE TABLE public_value(value)');c.execute('INSERT INTO public_value VALUES(11)');c.commit()
        with pub._sql(source,owner=work.owner,readonly=True,timeout=0)as c:
            assert c.execute('SELECT value FROM public_value').fetchone()[0]==11


def test_spool_real_seekable_zip_store_limits_and_single_creation(tmp_path):
    with original_work(tmp_path)as work:
        with work.open_fingerprint_spool_writer(512)as out:
            with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_STORED)as archive:
                archive.writestr('metadata-0001.db',b'public synthetic metadata')
            position=out.tell()
            with pytest.raises(pub.RestorePublicationError):out.seek(513)
            assert out.tell()==position
            out.seek(510)
            with pytest.raises(pub.RestorePublicationError):out.write(b'xxx')
            assert out.tell()==510
            out.seek(position);out.flush()
        assert out.closed
        with pytest.raises(pub.RestorePublicationError):out.write(b'late')
        path=work.work/own.FINGERPRINT_CORES['derived']
        with zipfile.ZipFile(path)as archive:
            assert archive.read('metadata-0001.db')==b'public synthetic metadata'
            assert archive.getinfo('metadata-0001.db').compress_type==zipfile.ZIP_STORED
        with pytest.raises(pub.RestorePublicationError):
            with work.open_fingerprint_spool_writer(512):pytest.fail('second writer admitted')


def test_writer_exception_still_closes_original_fd_and_cleanup(tmp_path):
    with original_work(tmp_path)as work:
        with pytest.raises(RuntimeError,match='FIXED_PUBLIC_STOP'):
            with work.open_fingerprint_spool_writer(128)as out:
                out.write(b'public');raise RuntimeError('FIXED_PUBLIC_STOP')
        assert out.closed
        with pytest.raises(OSError):os.fstat(out._fd)


def test_symlink_alias_is_rejected_and_claim_is_poisoned(tmp_path):
    if os.name=='nt':pytest.skip('POSIX actual alias control; native Win tested separately')
    with pytest.raises(pub.RestorePublicationError,match='CLOSE_UNCONFIRMED'):
        with original_work(tmp_path)as work:
            source=work.fingerprint_core('source');source.unlink();source.symlink_to('database.db')
            with pytest.raises((pub.RestorePublicationError,OSError)):work.reset_fingerprint_core('source')
            assert work.owner.gate.poisoned is True


@pytest.mark.skipif(os.name!='nt',reason='Requires actual Windows native share0 and real SQLite handle')
def test_windows_open_delete_mode_sqlite_handle_blocks_reset_without_sidecars(tmp_path):
    with original_work(tmp_path)as work:
        source=work.fingerprint_core('source')
        with closing(sqlite3.connect(source,timeout=0))as c:
            c.execute('PRAGMA journal_mode=DELETE');c.execute('CREATE TABLE public_value(value)');c.commit()
            assert not any(source.with_name(source.name+s).exists()for s in('-wal','-shm','-journal'))
            with pytest.raises(pub.RestorePublicationError):work.reset_fingerprint_core('source')
            assert c.execute('SELECT count(*)FROM public_value').fetchone()[0]==0
        work.reset_fingerprint_core('source')


def test_spool_final_size_is_verified_and_original_fd_closes_on_rejection(tmp_path):
    with original_work(tmp_path)as work:
        with pytest.raises(pub.RestorePublicationError):
            with work.open_fingerprint_spool_writer(16)as out:
                out.write(b'public');os.ftruncate(out._fd,17)
        assert out.closed
        with pytest.raises(OSError):os.fstat(out._fd)


def test_unconfirmed_original_fd_close_poison_retains_claim_without_double_close(tmp_path,monkeypatch):
    original_close=os.close;closed=[];target=None
    def failed_close(fd):
        if fd==target:
            closed.append(fd);original_close(fd);raise OSError('FIXED_PUBLIC_CLOSE_UNCONFIRMED')
        return original_close(fd)
    with pytest.raises(pub.RestorePublicationError,match='CLOSE_UNCONFIRMED'):
        with original_work(tmp_path)as work:
            with work.open_fingerprint_spool_writer(128)as out:
                out.write(b'public');target=out._fd;monkeypatch.setattr(os,'close',failed_close)
    assert closed==[target]and out.closed and work.owner.gate.poisoned
    assert any(item[0]=='restore_work_fd'for item in work.owner.gate.retained if isinstance(item,tuple)and item)
    assert work.work.exists()  # A failure never asserts cleanup or releases the claim.


def test_foreign_actor_cannot_mutate_original_writer_but_original_owner_continues(tmp_path):
    with original_work(tmp_path)as work:
        failures=[]
        with work.open_fingerprint_spool_writer(128)as out:
            out.write(b'public')
            def foreign():
                for operation in(lambda:out.write(b'foreign'),lambda:out.seek(0)):
                    try:operation()
                    except pub.RestorePublicationError as error:failures.append(str(error))
            thread=threading.Thread(target=foreign);thread.start();thread.join(2)
            assert not thread.is_alive()and failures==['RESTORE_PUBLICATION_OWNER_REQUIRED']*2
            assert out.tell()==6;out.write(b'-owner')
        assert (work.work/own.FINGERPRINT_CORES['derived']).read_bytes()==b'public-owner'
