"""Actual parent backup APIs; isolated synthetic registry and encrypted payloads only."""
from contextlib import closing
import hashlib,json,os,sqlite3,struct,zipfile
from pathlib import Path
import pytest
from home_study_agent.database import Database
from home_study_agent.knowledge_ingestion.document_evidence import DocumentCorpusSpec,LocalDocumentEvidenceIngestor
from home_study_agent.curriculum_standards import CurriculumStandardService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService,InvalidBackupError
from home_study_agent.registered_asset_snapshot import Policy,create_registered_asset_snapshot,verify_registered_asset_snapshot
H=lambda b:hashlib.sha256(b).hexdigest()
def fixture(base):
    source_dir=base/'source-data';source_dir.mkdir();external=base/'old-machine-registered';external.mkdir()
    db=Database(source_dir/'agent.db');db.initialize()
    def file(relative,data):
        p=external/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return p
    pdf=file('pdf/original.pdf',b'%PDF public synthetic original')
    transcript=file('ocr/pages/one.txt',b'Public synthetic page: 6 divided by 2 equals 3.')
    image=file('images/one.png',b'PUBLIC_SYNTHETIC_SCAN_BYTES')
    meta=external/'document-metadata.db'
    fingerprint=f'size:{pdf.stat().st_size}:mtime_ns:{pdf.stat().st_mtime_ns}'
    with closing(sqlite3.connect(meta)) as c:
        c.executescript('''CREATE TABLE textbook_book(book_id TEXT PRIMARY KEY,relative_pdf_path TEXT,title TEXT,stage TEXT,grade TEXT,subject TEXT,semester TEXT,volume TEXT,edition TEXT,fingerprint TEXT);
            CREATE TABLE textbook_page(page_id TEXT PRIMARY KEY,book_id TEXT,page_number INTEGER,status TEXT,ocr_confidence REAL,manual_review_required INTEGER,formula_review_required INTEGER,ocr_text_relative_path TEXT);''')
        c.execute('INSERT INTO textbook_book VALUES (?,?,?,?,?,?,?,?,?,?)',('book','original.pdf','Public textbook','primary','4','math','first','1','public',fingerprint))
        c.execute('INSERT INTO textbook_page VALUES (?,?,?,?,?,?,?,?)',('one','book',1,'completed',.9,0,0,'pages/one.txt'));c.commit()
    spec=DocumentCorpusSpec('book','Public textbook','textbook',transcript.parent.parent,image.parent,'parent-explicit-local','family_owned_local','synthetic',metadata_db_path=meta,pdf_root=pdf.parent)
    ingestor=LocalDocumentEvidenceIngestor(db);job=ingestor.start(spec,idempotency_key='public-one');ingestor.run(spec,job,hash_images=True,throttle_ms=0)
    legacy_db=external/'legacy/source.db';legacy_db.parent.mkdir()
    with closing(sqlite3.connect(legacy_db)) as c:c.execute('CREATE TABLE public_metadata(value TEXT)');c.execute("INSERT INTO public_metadata VALUES ('committed synthetic metadata')");c.commit()
    text=file('legacy/text/unit/original.txt',b'PUBLIC_SYNTHETIC_TRANSCRIPT')
    video=file('legacy/video/unit/original.mp4',b'PUBLIC_SYNTHETIC_VIDEO_BYTES')
    with db.connect() as c:
        c.execute('''INSERT INTO curriculum_corpus(corpus_id,title,corpus_type,source_database_path,transcript_root_path,video_root_path,source_ref,license_status,review_status,status,source_hash,resource_count,transcript_count,local_video_count,imported_at,updated_at)
            VALUES ('legacy','Public corpus','seyk',?,?,?,'parent-selected','public_personal_use','approved_baseline','ready',?,1,1,1,'original','original')''',(str(legacy_db),str(text.parent.parent),str(video.parent.parent),H(legacy_db.read_bytes())))
        c.execute('''INSERT INTO curriculum_resource(corpus_id,external_resource_id,title,description,stage_title,grade_title,subject_title,unit_path,transcript_text,transcript_relative_path,video_relative_path,local_video_available,import_job_id)
            VALUES ('legacy','original','Public resource','public','primary','4','math','unit','public','unit/original.txt','unit/original.mp4',1,'original-job')''');c.commit()
    std_pdf=file('standard/official.pdf',b'%PDF public standard');content=file('standard/content.txt',b'PUBLIC_STANDARD_TEXT');ocr=file('standard/ocr.txt',content.read_bytes());pages=file('standard/pages.jsonl',b'{"page":1,"text":"PUBLIC_STANDARD_TEXT"}\n')
    manifest={'format':'hsc_official_curriculum_sources_v1','sources':[{'source_key':'public-standard','jurisdiction':'CN','source_type':'national_standard','subject':'math','title':'Public standard','version':'public-v1','official_page_url':'https://www.moe.gov.cn/public-synthetic','page_count':1,'pdf_filename':std_pdf.name,'pdf_sha256':H(std_pdf.read_bytes()),'content_filename':content.name,'content_sha256':H(content.read_bytes()),'ocr_status':'completed','ocr_pages_filename':pages.name,'ocr_text_filename':ocr.name}]}
    file('standard/manifest.json',json.dumps(manifest,separators=(',',':')).encode());CurriculumStandardService(db).import_root(str(std_pdf.parent))
    raw={p.relative_to(external).as_posix():H(p.read_bytes()) for p in external.rglob('*') if p.is_file()}
    private=base/'oracle-private-stage';private.mkdir(mode=0o700);size=db.path.stat().st_size+sum(p.stat().st_size for p in external.rglob('*') if p.is_file())+65536
    policy=Policy(100000,64,1024,64*1024*1024,size*4,size);stage=create_registered_asset_snapshot(db.path,private,policy);verify_registered_asset_snapshot(stage,policy)
    oracle=json.loads((stage/'manifest.json').read_bytes());assert len(oracle['assets'])>=12 and {'pdf','image','metadata','transcript','video','manifest','ocr_pages','content','ocr_text'}<={r['role'] for r in oracle['assets']}
    service=PrivacyLifecycleService(db,source_dir,b'o'*32)
    return db,service,external,raw,oracle

def registered_rows(db):
    with db.connect() as c:
        return {t:[list(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in ('kg_corpus','kg_resource','kg_document_page','kg_segment','kg_segment_occurrence','curriculum_corpus','curriculum_resource','curriculum_standard_source','curriculum_standard_page')}

def assert_original_assets_unchanged(external,before):
    assert {p.relative_to(external).as_posix():H(p.read_bytes()) for p in external.rglob('*') if p.is_file()}==before

def test_actual_local_backup_covers_twelve_registered_assets_in_encrypted_capsule(tmp_path):
    db,service,external,raw,oracle=fixture(tmp_path);before=registered_rows(db)
    result=service.create_backup(prune=False);path=Path(result['path']);metadata=json.loads(service._backup_metadata_path(path).read_bytes())
    assert metadata['format']=='hsc_backup_full_v2'
    assert metadata['registered']['asset_count']==len(oracle['assets'])==12
    assert metadata['registered']['coverage']=='encrypted_registered_v1'
    capsule=path.with_suffix('.registered.enc');assert capsule.is_file()
    assert b'PUBLIC_SYNTHETIC_VIDEO_BYTES'not in capsule.read_bytes()
    assert b'%PDF public synthetic original'not in capsule.read_bytes()
    assert str(external).encode()not in service._backup_metadata_path(path).read_bytes()
    assert registered_rows(db)==before;assert_original_assets_unchanged(external,raw)
    assert not any(x.name.startswith('.registered-working-')for x in path.parent.iterdir())


def test_registered_new_local_restore_holds_before_any_live_publication(tmp_path):
    db,service,external,raw,_=fixture(tmp_path);result=service.create_backup(prune=False);before=registered_rows(db)
    from home_study_agent.restore_publication import RestorePublicationError
    with pytest.raises(RestorePublicationError,match='^RESTORE_PUBLICATION_INTENT_REQUIRED$'):
        service.restore_backup(result['backup_id'],'确认恢复')
    assert registered_rows(db)==before;assert_original_assets_unchanged(external,raw)


def open_portable(service, path, passphrase, parent):
    import base64
    header,offset=service._read_portable_header(Path(path));archive=parent/'portable.zip'
    key=service._derive_portable_key(passphrase,base64.b64decode(header['salt']))
    service._decrypt_file(Path(path),archive,key,base64.b64decode(header['nonce']),base64.b64decode(header['tag']),offset)
    extracted=parent/'extracted';manifest=service._extract_and_verify_portable_archive(archive,extracted)
    return archive,extracted,manifest


def test_actual_portable_outer_contract_carries_twelve_assets_and_source_key_for_new_device(tmp_path):
    import base64
    from home_study_agent import registered_asset_backup as backup
    db,source,external,raw,oracle=fixture(tmp_path);before=registered_rows(db)
    result=source.create_portable_backup('public synthetic passphrase only')
    target_dir=tmp_path/'target';target_dir.mkdir();target_db=Database(target_dir/'agent.db');target_db.initialize()
    target=PrivacyLifecycleService(target_db,target_dir,b'n'*32);private=tmp_path/'private';private.mkdir(mode=0o700)
    archive,extracted,manifest=open_portable(target,result['path'],'public synthetic passphrase only',private)
    old_key=base64.b64decode(manifest['source_data_key']);assert old_key==b'o'*32 and old_key!=target.data_encryption_key
    assert {x['archive_path']for x in manifest['files']}=={'database.db','database.media.json','database.registered.enc'}
    contract=json.loads((extracted/'database.media.json').read_bytes())
    stage=backup.decrypt_capsule(extracted/'database.registered.enc',old_key,target._decrypt_file,private,contract['database']['sha256'])
    payload=json.loads((stage/'manifest.json').read_bytes());assert len(payload['assets'])==len(oracle['assets'])==12
    for item in payload['assets']:assert H((stage/item['logical_name']).read_bytes())==item['sha256']
    assert {'pdf','image','video','metadata'}<={x['role']for x in payload['assets']}
    assert b'PUBLIC_SYNTHETIC_VIDEO_BYTES'not in Path(result['path']).read_bytes()
    before_target=registered_rows(target_db)
    with pytest.raises(InvalidBackupError,match='REGISTERED_ASSET_RESTORE_MIGRATION_REQUIRED'):
        target.restore_portable_backup(result['path'],'public synthetic passphrase only','确认迁移恢复')
    assert registered_rows(target_db)==before_target;assert registered_rows(db)==before;assert_original_assets_unchanged(external,raw)


@pytest.mark.parametrize('kind',['wrong-key','tag'])
def test_capsule_authentication_failure_never_returns_plaintext_stage(tmp_path,kind):
    from home_study_agent import registered_asset_backup as backup
    db,service,external,raw,_=fixture(tmp_path);result=service.create_backup(prune=False);snapshot=Path(result['path'])
    capsule=backup.capsule_path(snapshot);private=tmp_path/'private';private.mkdir(mode=0o700)
    key=b'x'*32 if kind=='wrong-key'else service.data_encryption_key
    if kind=='tag':
        damaged=private/'damaged.enc';payload=bytearray(capsule.read_bytes());payload[-1]^=1;damaged.write_bytes(payload);capsule=damaged
    with pytest.raises(Exception):backup.decrypt_capsule(capsule,key,service._decrypt_file,private,backup.record(snapshot)['sha256'])
    assert not any(x.is_dir()for x in private.iterdir());assert_original_assets_unchanged(external,raw)


@pytest.mark.parametrize('kind',['missing-capsule','changed-capsule'])
def test_actual_local_restore_rejects_missing_or_changed_bundle_before_live_database(tmp_path,kind):
    from home_study_agent import registered_asset_backup as backup
    db,service,external,raw,_=fixture(tmp_path);result=service.create_backup(prune=False);before=registered_rows(db);capsule=backup.capsule_path(Path(result['path']))
    if kind=='missing-capsule':capsule.unlink()
    else:
        data=bytearray(capsule.read_bytes());data[-1]^=1;capsule.write_bytes(data)
    with pytest.raises(InvalidBackupError):service.restore_backup(result['backup_id'],'确认恢复')
    assert registered_rows(db)==before;assert_original_assets_unchanged(external,raw)


@pytest.mark.parametrize('phase',['preflight','cipher-ENOSPC'])
def test_actual_create_failure_has_no_success_row_or_raw_stage_and_preserves_source(tmp_path,monkeypatch,phase):
    import errno
    from home_study_agent import registered_asset_backup as backup
    db,service,external,raw,_=fixture(tmp_path);before=registered_rows(db)
    if phase=='preflight':monkeypatch.setattr(backup,'stage_policy',lambda _:(_ for _ in ()).throw(ValueError('REGISTERED_BACKUP_DISK_REQUIRED')))
    else:monkeypatch.setattr(service,'_encrypt_file',lambda *a,**k:(_ for _ in ()).throw(OSError(errno.ENOSPC,'synthetic disk full')))
    with pytest.raises(InvalidBackupError):service.create_backup(prune=False)
    with db.connect()as c:assert c.execute('SELECT COUNT(*) FROM backup_manifest').fetchone()[0]==0
    assert list((service.data_dir/'backups').iterdir())==[];assert registered_rows(db)==before;assert_original_assets_unchanged(external,raw)


def test_actual_create_rejects_original_source_change_during_held_read(tmp_path,monkeypatch):
    from home_study_agent import registered_asset_snapshot as snapshot
    db,service,external,raw,_=fixture(tmp_path);before=registered_rows(db);original=snapshot.HeldFile.chunks;changed=[]
    def mutate(held):
        for chunk in original(held):
            yield chunk
            if held.root==external/'pdf' and held.parts==['original.pdf']and not changed:
                changed.append(True);(external/'pdf/original.pdf').write_bytes(b'%PDF changed by synthetic concurrent writer')
    monkeypatch.setattr(snapshot.HeldFile,'chunks',mutate)
    with pytest.raises(InvalidBackupError):service.create_backup(prune=False)
    assert changed and registered_rows(db)==before
    with db.connect()as c:assert c.execute('SELECT COUNT(*) FROM backup_manifest').fetchone()[0]==0
    assert list((service.data_dir/'backups').iterdir())==[]


def test_payload_without_data_key_refuses_but_true_db_only_no_key_is_explicit_zero(tmp_path):
    db,service,external,raw,_=fixture(tmp_path);service.data_encryption_key=None
    with pytest.raises(InvalidBackupError,match='REGISTERED_BACKUP_KEY_REQUIRED'):service.create_backup(prune=False)
    assert list((service.data_dir/'backups').iterdir())==[];assert_original_assets_unchanged(external,raw)
    root=tmp_path/'empty';root.mkdir();empty=Database(root/'agent.db');empty.initialize();local=PrivacyLifecycleService(empty,root)
    result=local.create_backup(prune=False);assert result['registered_coverage']=='zero_registered_payload'and result['registered_asset_count']==0
    assert not Path(result['path']).with_suffix('.registered.enc').exists()


def test_owned_bundle_cleanup_removes_its_capsule_without_traversing_external_roots(tmp_path):
    db,service,external,raw,_=fixture(tmp_path);result=service.create_backup(prune=False)
    service._remove_backup_bundle(Path(result['path']))
    assert list((service.data_dir/'backups').iterdir())==[];assert_original_assets_unchanged(external,raw)


@pytest.mark.parametrize('with_assets',[False,True])
def test_small_real_backup_fits_reported_256MiB_without_theoretical_maximum_tax(tmp_path,monkeypatch,with_assets):
    from home_study_agent import registered_asset_backup as backup
    if with_assets:
        db,service,external,raw,_=fixture(tmp_path)
    else:
        root=tmp_path/'data';root.mkdir();db=Database(root/'agent.db');db.initialize();service=PrivacyLifecycleService(db,root)
    usage=backup.shutil.disk_usage(tmp_path)
    monkeypatch.setattr(backup.shutil,'disk_usage',lambda _:type(usage)(usage.total,usage.total-256*1024*1024,256*1024*1024))
    result=service.create_backup(prune=False)
    assert result['integrity_status']=='ok'and result['registered_asset_count']==(12 if with_assets else 0)
    with db.connect()as c:assert c.execute('SELECT COUNT(*) FROM backup_manifest').fetchone()[0]==1
    assert not any(x.name.startswith('.registered-working-')for x in (service.data_dir/'backups').iterdir())


def test_reported_actual_insufficient_disk_fails_before_success_row_and_cleans(tmp_path,monkeypatch):
    from home_study_agent import registered_asset_backup as backup
    db,service,external,raw,_=fixture(tmp_path);before=registered_rows(db);usage=backup.shutil.disk_usage(tmp_path)
    monkeypatch.setattr(backup.shutil,'disk_usage',lambda _:type(usage)(usage.total,usage.total-131072,131072))
    with pytest.raises(InvalidBackupError,match='REGISTERED_BACKUP_DISK_REQUIRED'):service.create_backup(prune=False)
    assert registered_rows(db)==before;assert_original_assets_unchanged(external,raw)
    with db.connect()as c:assert c.execute('SELECT COUNT(*) FROM backup_manifest').fetchone()[0]==0
    assert list((service.data_dir/'backups').iterdir())==[]


def altered_authenticated_portable(service,source,parent,kind):
    """Real passphrase AEAD over a malformed finite inner archive."""
    import base64,shutil,stat
    from home_study_agent.privacy_lifecycle import PORTABLE_BACKUP_MAGIC
    password='public synthetic passphrase only'
    archive,extracted,manifest=open_portable(service,source,password,parent)
    rewritten=parent/'malformed.zip';files=[dict(x)for x in manifest['files']]
    with zipfile.ZipFile(archive)as incoming,zipfile.ZipFile(rewritten,'x',compression=zipfile.ZIP_STORED)as out:
        for info in incoming.infolist():
            if info.filename=='portable-manifest.json':continue
            if kind=='missing'and info.filename=='database.registered.enc':
                files=[x for x in files if x['archive_path']!=info.filename];continue
            target=zipfile.ZipInfo(info.filename)
            target.external_attr=((stat.S_IFLNK|0o777)if kind=='symlink'and info.filename=='database.db'else(stat.S_IFREG|0o600))<<16
            with incoming.open(info)as src,out.open(target,'w')as dst:shutil.copyfileobj(src,dst,65536)
        if kind=='duplicate':
            with incoming.open('database.db')as src,out.open('database.db','w')as dst:shutil.copyfileobj(src,dst,65536)
        if kind in {'extra','traversal','ADS','casecollision'}:
            name={'extra':'unexpected.bin','traversal':'database.media/../escape.bin','ADS':'database.media/file:stream','casecollision':'DATABASE.DB'}[kind]
            out.writestr(name,b'public-extra');files.append({'archive_path':name,'size_bytes':12,'sha256':H(b'public-extra')})
        manifest['files']=files;out.writestr('portable-manifest.json',json.dumps(manifest,sort_keys=True,separators=(',',':')))
    header,_=service._read_portable_header(Path(source));nonce=os.urandom(12);enc=parent/'malformed.enc'
    key=service._derive_portable_key(password,base64.b64decode(header['salt']))
    tag=service._encrypt_file(rewritten,enc,key,nonce)
    header.update(nonce=base64.b64encode(nonce).decode(),tag=base64.b64encode(tag).decode(),ciphertext_size=enc.stat().st_size)
    payload=json.dumps(header,sort_keys=True,separators=(',',':')).encode();dest=parent/'malformed.hscbackup'
    with dest.open('xb')as dst,enc.open('rb')as src:
        dst.write(PORTABLE_BACKUP_MAGIC);dst.write(struct.pack('>I',len(payload)));dst.write(payload);shutil.copyfileobj(src,dst,65536)
    return dest


@pytest.mark.parametrize('kind',['missing','duplicate','extra','traversal','ADS','casecollision','symlink'])
def test_real_portable_restore_rejects_authenticated_nonexact_namespace_without_live_changes(tmp_path,kind):
    db,service,external,raw,_=fixture(tmp_path);portable=service.create_portable_backup('public synthetic passphrase only')
    private=tmp_path/'private';private.mkdir(mode=0o700)
    malformed=altered_authenticated_portable(service,portable['path'],private,kind)
    target_dir=tmp_path/'target';target_dir.mkdir();target_db=Database(target_dir/'agent.db');target_db.initialize();target=PrivacyLifecycleService(target_db,target_dir,b'n'*32)
    before=registered_rows(target_db)
    with pytest.raises(InvalidBackupError):target.restore_portable_backup(str(malformed),'public synthetic passphrase only','确认迁移恢复')
    assert registered_rows(target_db)==before;assert_original_assets_unchanged(external,raw)
    assert not any(x.name.startswith('hsc-portable-restore-')or x.name.startswith('.registered-working-')for x in target_dir.iterdir())


def test_outer_cipher_ENOSPC_cleans_raw_stage_but_keeps_only_valid_local_point(tmp_path,monkeypatch):
    import errno
    db,service,external,raw,_=fixture(tmp_path);original=service._encrypt_file
    def encrypt(src,dest,*args,**kwargs):
        if Path(src).name=='payload.zip':
            Path(dest).write_bytes(b'partial synthetic cipher');raise OSError(errno.ENOSPC,'synthetic disk full')
        return original(src,dest,*args,**kwargs)
    monkeypatch.setattr(service,'_encrypt_file',encrypt)
    with pytest.raises(InvalidBackupError,match='PORTABLE_BACKUP_CREATE_FAILED'):service.create_portable_backup('public synthetic passphrase only')
    assert list((service.data_dir/'portable-backups').iterdir())==[];assert_original_assets_unchanged(external,raw)
    with db.connect()as c:
        assert c.execute('SELECT COUNT(*) FROM backup_manifest').fetchone()[0]==1
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='portable_backup_created'").fetchone()[0]==0


def test_prune_removes_only_owned_old_capsule_and_retains_foreign_historical_row(tmp_path):
    db,service,external,raw,_=fixture(tmp_path);result=service.create_backup(prune=False);old=Path(result['path'])
    foreign=external/'home-study-foreign.db';foreign.write_bytes(b'PUBLIC_UNOWNED_HISTORY');foreign_before=foreign.read_bytes()
    with db.connect()as c:
        c.execute("UPDATE backup_manifest SET created_at='2001-01-01T00:00:00+00:00'")
        for i in range(1,8):
            c.execute('''INSERT INTO backup_manifest SELECT ?,?,database_version,size_bytes,sha256,integrity_status,reason,? FROM backup_manifest WHERE backup_id=?''',
                ('keep-'+str(i),str(old.parent/('home-study-keep-'+str(i)+'.db')),f'2001-01-01T0{i}:00:00+00:00',result['backup_id']))
        c.execute('''INSERT INTO backup_manifest SELECT 'foreign',?,database_version,size_bytes,sha256,integrity_status,reason,'2001-01-01T00:00:01+00:00' FROM backup_manifest WHERE backup_id=?''',(str(foreign),result['backup_id']));c.commit()
    service._prune_backups()
    assert not old.exists()and not old.with_suffix('.registered.enc').exists()
    assert foreign.read_bytes()==foreign_before
    with db.connect()as c:
        assert c.execute('SELECT 1 FROM backup_manifest WHERE backup_id=?',(result['backup_id'],)).fetchone()is None
        assert c.execute("SELECT 1 FROM backup_manifest WHERE backup_id='foreign'").fetchone()is not None
    foreign.unlink();assert_original_assets_unchanged(external,raw)


def test_portable_postwrite_audit_failure_removes_only_its_new_output(tmp_path,monkeypatch):
    db,service,external,raw,_=fixture(tmp_path);original=service._audit
    def audit(connection,actor,action,*args):
        if action=='portable_backup_created':raise OSError('synthetic audit failure')
        return original(connection,actor,action,*args)
    monkeypatch.setattr(service,'_audit',audit)
    with pytest.raises(InvalidBackupError,match='PORTABLE_BACKUP_CREATE_FAILED'):service.create_portable_backup('public synthetic passphrase only')
    assert list((service.data_dir/'portable-backups').iterdir())==[];assert_original_assets_unchanged(external,raw)


def test_finite_archive_rejects_directory_count_before_zipfile_allocates_entries(tmp_path,monkeypatch):
    from home_study_agent import registered_asset_backup as backup
    archive=tmp_path/'two.zip'
    with zipfile.ZipFile(archive,'x')as z:z.writestr('one',b'1');z.writestr('two',b'2')
    def forbidden(*args,**kwargs):raise AssertionError('eager ZipFile allocation reached')
    monkeypatch.setattr(backup.zipfile,'ZipFile',forbidden)
    with pytest.raises(ValueError,match='BACKUP_CONTAINER_DIRECTORY'):
        with backup.finite_archive(archive,1):pass


@pytest.mark.parametrize('source_kind',['seyk','bilibili'])
def test_both_real_parent_apis_encrypt_registered_pending_sources_without_starting_import(tmp_path,source_kind):
    import base64
    from home_study_agent import registered_asset_backup as backup
    from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec,LocalCorpusIngestor
    root=tmp_path/'data';root.mkdir();text=tmp_path/'selected-text';video=tmp_path/'selected-video'
    (text/'pending').mkdir(parents=True);(video/'pending').mkdir(parents=True)
    (text/'pending/not-imported.txt').write_bytes(b'public registered pending transcript')
    (video/'pending/not-imported.mp4').write_bytes(b'public registered pending video')
    outside=tmp_path/'not-selected.txt';outside.write_bytes(b'PUBLIC_UNREGISTERED_MUST_STAY_OUT')
    db=Database(root/'agent.db');db.initialize()
    LocalCorpusIngestor(db).register(CorpusSpec('public-stopped','Synthetic registered stopped source',source_kind,text,video,'parent-selected-local','family-owned-local'))
    service=PrivacyLifecycleService(db,root,b'o'*32)
    def counts():
        with db.connect()as c:return tuple(c.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]for name in ['kg_resource','kg_ingest_job','kg_segment'])
    assert counts()==(0,0,0)
    local=service.create_backup(prune=False);assert local['registered_asset_count']==2
    portable=service.create_portable_backup('public synthetic passphrase only')
    private=tmp_path/'private';private.mkdir(mode=0o700)
    _,extracted,manifest=open_portable(service,portable['path'],'public synthetic passphrase only',private)
    contract=json.loads((extracted/'database.media.json').read_bytes());assert contract['registered']['asset_count']==2
    stage=backup.decrypt_capsule(extracted/'database.registered.enc',base64.b64decode(manifest['source_data_key']),service._decrypt_file,private,contract['database']['sha256'])
    payload=json.loads((stage/'manifest.json').read_bytes())
    saved={(x['role'],x['relative_path']):(stage/x['logical_name']).read_bytes()for x in payload['assets']}
    assert saved=={('transcript','pending/not-imported.txt'):b'public registered pending transcript',('video','pending/not-imported.mp4'):b'public registered pending video'}
    assert outside.read_bytes()==b'PUBLIC_UNREGISTERED_MUST_STAY_OUT'and counts()==(0,0,0)


@pytest.mark.parametrize('api',['local','portable'])
def test_actual_parent_refuses_registered_ancestor_of_its_private_stage_and_cleans(tmp_path,api):
    from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec,LocalCorpusIngestor
    root=tmp_path/'data';root.mkdir();marker=root/'selected-transcript.txt';marker.write_bytes(b'public selected original; never mutate')
    db=Database(root/'agent.db');db.initialize()
    LocalCorpusIngestor(db).register(CorpusSpec('overlap','Synthetic explicit ancestor source','seyk',root,None,'parent-selected-local','family-owned-local'))
    service=PrivacyLifecycleService(db,root,b'o'*32)
    with pytest.raises(InvalidBackupError,match='REGISTERED_ROOT_STAGE_OVERLAP'):
        if api=='local':service.create_backup(prune=False)
        else:service.create_portable_backup('public synthetic passphrase only')
    assert marker.read_bytes()==b'public selected original; never mutate'
    assert list((root/'backups').iterdir())==[]
    if (root/'portable-backups').exists():assert list((root/'portable-backups').iterdir())==[]
    with db.connect()as c:
        assert c.execute('SELECT COUNT(*) FROM backup_manifest').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM kg_resource').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM kg_ingest_job').fetchone()[0]==0
