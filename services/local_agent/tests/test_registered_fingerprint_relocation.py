import hashlib,sqlite3
from pathlib import Path
import pytest
from home_study_agent import registered_fingerprint_relocation as relocation
from home_study_agent.registered_asset_restore import RegisteredAssetRestoreError
from home_study_agent.registered_asset_snapshot import (canonical,digest,registered_fingerprint_witness,AssetSnapshotError)


def asset(fp,role='pdf'):
    return {'role':role,'bytes':7,'sha256':'a'*64,
      'version_sha256s':[digest(canonical(registered_fingerprint_witness(role,fp,'a'*64)))]}


def test_exact_source_authentication_precedes_target_precision():
    fp='size:7:mtime_ns:1700000000000000043'
    value=relocation.planned_fingerprint('pdf',fp,asset(fp),100)
    assert value['source_fingerprint']==fp
    assert value['target_fingerprint']=='size:7:mtime_ns:1700000000000000000'
    assert relocation.planned_fingerprint('pdf',fp,asset(fp),1)['target_fingerprint']==fp
    with pytest.raises(RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_FINGERPRINT_WITNESS_REQUIRED$'):
        relocation.planned_fingerprint('pdf','size:7:mtime_ns:1700000000000000044',asset(fp),100)
    with pytest.raises(AssetSnapshotError):relocation.planned_fingerprint('pdf',fp,asset(fp),1000)


def test_conflicting_source_claims_do_not_merge_after_quantization():
    a='size:7:mtime_ns:1700000000000000043';b='size:7:mtime_ns:1700000000000000044'
    first=relocation.planned_fingerprint('pdf',a,asset(a),100)
    second=relocation.planned_fingerprint('pdf',b,asset(b),100)
    assert first['target_fingerprint']==second['target_fingerprint']
    targets={};relocation.merge_target(targets,'kg-x/book.pdf',first)
    with pytest.raises(AssetSnapshotError):relocation.merge_target(targets,'kg-x/book.pdf',second)


def source_database(path):
    c=sqlite3.connect(path)
    c.executescript("""PRAGMA journal_mode=DELETE;
      CREATE TABLE textbook_book(book_id TEXT PRIMARY KEY,relative_pdf_path TEXT UNIQUE,fingerprint TEXT NOT NULL,title TEXT);
      CREATE TABLE textbook_page(page_id TEXT PRIMARY KEY,book_id TEXT REFERENCES textbook_book(book_id),status TEXT,confidence REAL,layout BLOB);
      CREATE INDEX original_page_index ON textbook_page(book_id);
      INSERT INTO textbook_book VALUES('one','one.pdf','size:7:mtime_ns:1700000000000000043','original title');
      INSERT INTO textbook_book VALUES('two','two.pdf','size:7:mtime_ns:1700000000000000000','other title');
      INSERT INTO textbook_page VALUES('page-one','one','complete',0.123,X'00ff01');
    """)
    c.commit();c.close()


def ro(path):
    c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF')
    return c


def changes():
    return [('one','size:7:mtime_ns:1700000000000000043','size:7:mtime_ns:1700000000000000000')]


def test_real_sqlite_derived_fp_persists_and_all_other_cells_are_exact(tmp_path):
    source=tmp_path/'source.db';target=tmp_path/'target.db';source_database(source)
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    original=ro(source);derived=sqlite3.connect(target)
    try:expected=relocation.rewrite_metadata_connection(original,derived,changes())
    finally:derived.close();original.close()
    reopened=ro(target)
    try:
        relocation.verify_metadata_connection(reopened,expected)
        assert reopened.execute('SELECT fingerprint FROM textbook_book WHERE book_id=?',('one',)).fetchone()[0]==changes()[0][2]
        assert reopened.execute('SELECT status,confidence,hex(layout) FROM textbook_page').fetchone()==('complete',0.123,'00FF01')
        assert reopened.execute('SELECT title FROM textbook_book ORDER BY book_id').fetchall()==[('original title',),('other title',)]
    finally:reopened.close()
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    assert not any(Path(str(source)+suffix).exists()or Path(str(target)+suffix).exists()for suffix in ('-wal','-shm','-journal'))
    with pytest.raises(sqlite3.ProgrammingError):derived.execute('SELECT 1')


def test_unknown_source_cas_refuses_before_copying(tmp_path):
    source=tmp_path/'source.db';target=tmp_path/'target.db';source_database(source)
    original=ro(source);derived=sqlite3.connect(target)
    wrong=[('one','size:7:mtime_ns:1700000000000000044',changes()[0][2])]
    try:
        with pytest.raises(AssetSnapshotError):relocation.rewrite_metadata_connection(original,derived,wrong)
        assert derived.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]==0
    finally:derived.close();original.close()


def test_trigger_on_fingerprint_target_is_rejected_before_clone(tmp_path):
    source=tmp_path/'source.db';target=tmp_path/'target.db';source_database(source)
    c=sqlite3.connect(source);c.execute("CREATE TRIGGER unknown_effect AFTER UPDATE ON textbook_book BEGIN UPDATE textbook_page SET status='lost'; END");c.commit();c.close()
    original=ro(source);derived=sqlite3.connect(target)
    try:
        with pytest.raises(AssetSnapshotError):relocation.rewrite_metadata_connection(original,derived,changes())
        assert derived.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]==0
    finally:derived.close();original.close()


def test_destination_must_be_originally_empty(tmp_path):
    source=tmp_path/'source.db';target=tmp_path/'target.db';source_database(source);source_database(target)
    original=ro(source);derived=sqlite3.connect(target)
    try:
        with pytest.raises(AssetSnapshotError):relocation.rewrite_metadata_connection(original,derived,changes())
    finally:derived.close();original.close()


def test_new_readonly_projection_rejects_other_cell_corruption(tmp_path):
    source=tmp_path/'source.db';target=tmp_path/'target.db';source_database(source)
    original=ro(source);derived=sqlite3.connect(target)
    try:expected=relocation.rewrite_metadata_connection(original,derived,changes())
    finally:derived.close();original.close()
    c=sqlite3.connect(target);c.execute("UPDATE textbook_page SET status='lost'");c.commit();c.close()
    reopened=ro(target)
    try:
        with pytest.raises(AssetSnapshotError):relocation.verify_metadata_connection(reopened,expected)
    finally:reopened.close()


def test_posix_and_already_aligned_sources_do_not_need_derivation():
    fp='size:7:mtime_ns:1700000000000000000'
    assert relocation.planned_fingerprint('pdf',fp,asset(fp),100)['target_fingerprint']==fp
    with pytest.raises(AssetSnapshotError):relocation.metadata_changes([('one',fp,fp)])


def test_arbitrary_target_timestamp_is_not_accepted_as_relocation():
    with pytest.raises(AssetSnapshotError):
        relocation.metadata_changes([('one','size:7:mtime_ns:1700000000000000043','size:7:mtime_ns:1700000000000000100')])


def test_metadata_order_uses_original_identity_when_changed_column_is_first(tmp_path):
    source=tmp_path/'source.db';target=tmp_path/'target.db'
    c=sqlite3.connect(source);c.execute('CREATE TABLE textbook_book(fingerprint TEXT NOT NULL,book_id TEXT PRIMARY KEY)')
    c.executemany('INSERT INTO textbook_book VALUES(?,?)',[
      ('size:7:mtime_ns:1700000000000000043','z'),('size:7:mtime_ns:1700000000000000044','a')]);c.commit();c.close()
    updates=[('z','size:7:mtime_ns:1700000000000000043','size:7:mtime_ns:1700000000000000000'),
             ('a','size:7:mtime_ns:1700000000000000044','size:7:mtime_ns:1700000000000000000')]
    original=ro(source);derived=sqlite3.connect(target)
    try:expected=relocation.rewrite_metadata_connection(original,derived,updates)
    finally:derived.close();original.close()
    reopened=ro(target)
    try:relocation.verify_metadata_connection(reopened,expected)
    finally:reopened.close()


def test_unrelated_metadata_can_have_more_rows_than_asset_count(tmp_path):
    source=tmp_path/'source.db';target=tmp_path/'target.db';source_database(source)
    c=sqlite3.connect(source);c.execute('CREATE TABLE unrelated_metadata(n INTEGER)')
    c.executemany('INSERT INTO unrelated_metadata VALUES(?)',((i,)for i in range(100001)));c.commit();c.close()
    assert source.stat().st_size<relocation.MAX_METADATA_BYTES
    original=ro(source);derived=sqlite3.connect(target)
    try:expected=relocation.rewrite_metadata_connection(original,derived,changes())
    finally:derived.close();original.close()
    reopened=ro(target)
    try:
        relocation.verify_metadata_connection(reopened,expected)
        assert reopened.execute('SELECT count(*) FROM unrelated_metadata').fetchone()[0]==100001
    finally:reopened.close()


def test_shared_plan_authenticates_all_three_readers_and_preserves_semantic_version(tmp_path):
    from contextlib import contextmanager
    from home_study_agent.registered_asset_snapshot import Policy,record_key
    source=tmp_path/'metadata.db';source_database(source)
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.executescript("""
      CREATE TABLE kg_corpus(corpus_id TEXT,transcript_root TEXT,video_root TEXT,metadata_db_path TEXT,pdf_root TEXT,image_root TEXT,version_label TEXT);
      CREATE TABLE kg_resource(resource_id TEXT,corpus_id TEXT,video_relative_path TEXT,video_fingerprint TEXT);
      CREATE TABLE kg_document_page(page_id TEXT,resource_id TEXT,image_relative_path TEXT,image_fingerprint TEXT);
      INSERT INTO kg_corpus VALUES('corpus','/origin/transcript','/origin/video','/origin/metadata.db','/origin/pdf','/origin/image','semantic-original-v1');
      INSERT INTO kg_resource VALUES('resource','corpus','lesson.mp4','size:7:mtime_ns:1700000000000000043');
      INSERT INTO kg_document_page VALUES('page','resource','page.png','size:7:mtime_ns:1700000000000000085');
      INSERT INTO kg_resource VALUES('unused','corpus',NULL,'size:7:mtime_ns:1700000000000000043');
      INSERT INTO kg_document_page VALUES('unused-page','unused',NULL,'size:7:mtime_ns:1700000000000000085');
    """)
    key=record_key('kg','corpus');entries=[]
    for role,relative,fp in [('video','lesson.mp4','size:7:mtime_ns:1700000000000000043'),('image','page.png','size:7:mtime_ns:1700000000000000085'),('pdf','one.pdf','size:7:mtime_ns:1700000000000000043'),('pdf','two.pdf','size:7:mtime_ns:1700000000000000000')]:
        value=asset(fp,role);value.update({'kind':'kg','record_sha256':key,'relative_path':relative,'logical_name':'registered/kg/'+key+'/'+role+'/'+relative,'storage':'regular'});entries.append(value)
    name='registered/kg/'+key+'/metadata/metadata.db'
    entries.append({'role':'metadata','kind':'kg','record_sha256':key,'relative_path':'metadata.db','logical_name':name,'storage':'sqlite_backup','sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'bytes':source.stat().st_size,'version_sha256s':[digest(canonical('semantic-original-v1'))]})
    @contextmanager
    def metadata_reader(logical,item):
        assert logical==name and hashlib.sha256(source.read_bytes()).hexdigest()==item['sha256']
        reader=ro(source);reader.row_factory=sqlite3.Row
        try:yield reader
        finally:reader.close()
    policy=Policy(100000,64,1024,64*1024*1024,2**53-1,2**53-1)
    try:
        plan=relocation.collect_plan(c,{'assets':entries},policy,100,metadata_reader)
        assert len(plan['references'])==4
        assert [r['table']for r in plan['main_changes']]==['kg_resource','kg_document_page']
        assert plan['metadata_changes'][name]['changes']==changes()
        assert c.execute('SELECT version_label FROM kg_corpus').fetchone()[0]=='semantic-original-v1'
        unchanged=relocation.collect_plan(c,{'assets':entries},policy,1,metadata_reader)
        assert unchanged['main_changes']==[]and unchanged['metadata_changes']=={}
    finally:c.close()


def test_real_original_owner_spool_enrolls_exact_derived_bytes_and_crc_before_use(tmp_path):
    from contextlib import contextmanager
    from test_parent_restore_fingerprint_ownership import original_work
    from home_study_agent.registered_asset_snapshot import Policy,HeldFile,_open_readonly,record_key
    source=tmp_path/'source.db';source_database(source)
    source_hash=hashlib.sha256(source.read_bytes()).hexdigest();key=record_key('kg','corpus')
    logical='registered/kg/'+key+'/metadata/metadata.db'
    item={'logical_name':logical,'bytes':source.stat().st_size,'sha256':source_hash}
    source_fp=changes()[0][1];target_fp=changes()[0][2]
    reference={'logical_name':'registered/kg/'+key+'/pdf/one.pdf','target_relative':'kg-pdf/one.pdf','corpus_id':'corpus','role':'pdf',
      'source_fingerprint':source_fp,'target_fingerprint':target_fp,'source_sha256':'a'*64,'bytes':7}
    plan={'references':[reference],'metadata_changes':{logical:{'source_bytes':item['bytes'],'source_sha256':source_hash,
      'target_relative':'kg-metadata/metadata.db','changes':changes(),'corpus_id':'corpus'}},'corpus_groups':{'corpus':'kg-transcript'}}
    policy=Policy(100000,64,1024,64*1024*1024,2**30,64*1024*1024)
    with original_work(tmp_path)as work:
        @contextmanager
        def metadata_source(name,record):
            assert name==logical and record==item
            with HeldFile(source.parent,source.name,policy)as held:
                path,actual=work.stream_fingerprint_source(held.chunks(),item['bytes'])
                assert actual==(source_hash,item['bytes'])
            with _open_readonly(path)as c:yield c
        with relocation.derivative_spool(work,plan,{'assets':[item]},metadata_source,{},policy)as spool:
            contract=relocation.derived_contract(spool)
            assert set(contract['source_inputs'])=={logical}
            assert len(contract['records'])==2
            record=next(r for r in contract['records']if r['relative']=='kg-metadata/metadata.db')
            name=relocation.derived_member_for_relative(spool,record['relative'])
            body=b''.join(relocation.derived_blocks(spool,name))
            assert hashlib.sha256(body).hexdigest()==record['sha256']
            assert len(body)==record['bytes']and body!=source.read_bytes()
            extracted=tmp_path/'derived-readonly-check.db';extracted.write_bytes(body)
            c=ro(extracted)
            try:
                assert c.execute('SELECT fingerprint FROM textbook_book WHERE book_id=?',('one',)).fetchone()[0]==target_fp
                assert c.execute('SELECT status FROM textbook_page').fetchone()[0]=='complete'
            finally:c.close()
            provenance=next(r for r in contract['records']if r['relative'].endswith(relocation.NAME))
            raw=b''.join(relocation.derived_blocks(spool,relocation.derived_member_for_relative(spool,provenance['relative'])))
            decoded=relocation.decode_provenance(raw)
            assert decoded['fingerprints'][0]['source_fingerprint']==source_fp
            assert decoded['fingerprints'][0]['target_fingerprint']==target_fp
            assert decoded['metadata_derivations'][0]['source_sha256']==source_hash
            assert decoded['metadata_derivations'][0]['target_sha256']==record['sha256']
            assert relocation.encode_provenance([raw,raw],[],[])==raw
            with pytest.raises(AssetSnapshotError):relocation.derived_member_for_relative(spool,'unlisted.db')
        with pytest.raises(AssetSnapshotError):relocation.derived_contract(spool)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash


def _seed_real_registered_document(database,root):
    import os
    from home_study_agent.knowledge_ingestion.textbook_pdf import TextbookPdfOcrPipeline
    from home_study_agent.knowledge_ingestion.document_evidence import DocumentCorpusSpec,LocalDocumentEvidenceIngestor
    root.mkdir(mode=0o700);pdfs=root/'pdf';pdfs.mkdir();pdf=pdfs/'四年级上册_数学_苏教版.pdf';pdf.write_bytes(b'%PDF synthetic original source')
    original_ns=1700000000000000043;os.utime(pdf,ns=(original_ns,original_ns))
    text=root/'text';metadata=root/'metadata.db'
    pipeline=TextbookPdfOcrPipeline(pdfs,text,metadata,page_counter=lambda _:1)
    assert pipeline.prepare()['books']==1
    with sqlite3.connect(metadata)as c:
        page=c.execute('SELECT page_id FROM textbook_page').fetchone()[0];relative='pages/'+page+'.txt'
        c.execute("UPDATE textbook_page SET status='completed',ocr_text_relative_path=?,ocr_engine='synthetic',ocr_confidence=.9",(relative,))
    (text/'pages').mkdir(exist_ok=True);(text/relative).write_text('公开合成教材中的一道数学题。',encoding='utf-8')
    images=root/'images';images.mkdir();image=images/(page+'.png');image.write_bytes(b'synthetic original page')
    os.utime(image,ns=(original_ns+42,original_ns+42))
    spec=DocumentCorpusSpec('book','合成教材','textbook',text,images,'parent-selected-local','family_owned_local','synthetic',metadata_db_path=metadata,pdf_root=pdfs)
    ingestor=LocalDocumentEvidenceIngestor(database);job=ingestor.start(spec,idempotency_key='public-original-document')
    ingestor.run(spec,job,hash_images=True,throttle_ms=0)
    return spec,pdf,page


def _actual_restored_readers(database):
    from home_study_agent.knowledge_ingestion.local_corpora import LocalCorpusIngestor,CorpusSpec
    with database.connect()as c:
        row=dict(c.execute("SELECT *FROM kg_corpus WHERE corpus_id='book'").fetchone())
        image=dict(c.execute('SELECT image_fingerprint,image_relative_path FROM kg_document_page').fetchone())
    metadata=Path(row['metadata_db_path']);before=hashlib.sha256(metadata.read_bytes()).hexdigest()
    found=LocalCorpusIngestor._metadata(metadata,row['source_kind']);assert len(found)==1
    spec=CorpusSpec(row['corpus_id'],row['title'],row['source_kind'],Path(row['transcript_root']),
        Path(row['video_root'])if row['video_root']else None,row['source_ref'],row['license_boundary'],
        metadata,row['version_label'],pdf_root=Path(row['pdf_root']),image_root=Path(row['image_root']))
    LocalCorpusIngestor(database).register(spec)
    assert hashlib.sha256(metadata.read_bytes()).hexdigest()==before
    assert not any(Path(str(metadata)+suffix).exists()for suffix in ('-wal','-shm','-journal'))
    with ro(metadata)as c:pdf_relative,fp=c.execute('SELECT relative_pdf_path,fingerprint FROM textbook_book').fetchone()
    pdf=Path(row['pdf_root'])/pdf_relative
    assert fp==f'size:{pdf.stat().st_size}:mtime_ns:{pdf.stat().st_mtime_ns}'
    assert pdf.stat().st_mtime_ns%100==0
    image_path=Path(row['image_root'])/image['image_relative_path']
    assert image['image_fingerprint']==f'size:{image_path.stat().st_size}:mtime_ns:{image_path.stat().st_mtime_ns}'
    assert image_path.stat().st_mtime_ns%100==0
    return row,metadata,before


def test_actual_native_restore_readers_backup_restore_is_durable_and_idempotent(tmp_path,monkeypatch):
    import socket
    from parent_owned_media_fixture import client,auth,backup,restore
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:(_ for _ in()).throw(AssertionError('NETWORK_FORBIDDEN')))
    monkeypatch.setattr(relocation,'platform_precision_ns',lambda:100)
    root=tmp_path/'actual-data';root.mkdir(mode=0o700)
    with client(root,root/'agent.db')as c:
        headers=auth(c,True);database=c.app.state.privacy_lifecycle.database
        _seed_real_registered_document(database,tmp_path/'original-source')
        first=backup(c,headers)
        import sys
        observed=[];plans=[]
        watched={'registered_fingerprint_relocation.py','parent_restore_assets.py','registered_asset_restore.py','parent_restore_ownership.py','privacy_lifecycle.py','restore_publication.py','parent_restore_workflow.py'}
        def trace(frame,event,arg):
            if event=='return'and frame.f_code.co_name=='collect_plan'and Path(frame.f_code.co_filename).name=='registered_fingerprint_relocation.py'and type(arg)is dict:
                plans.append((arg['precision_ns'],len(arg['main_changes']),len(arg['metadata_changes'])))
            if event=='return'and frame.f_code.co_name=='authorize'and Path(frame.f_code.co_filename).name=='registered_asset_restore.py'and arg==sqlite3.SQLITE_DENY:
                print('FIXED_SQL_AUTHORIZATION_DENIED',[(k,frame.f_locals[k])for k in ('action','first','second','database','source')])
            if event=='exception'and Path(frame.f_code.co_filename).name in watched:
                value=arg[1];code=value.args[0]if len(value.args)==1 and isinstance(value.args[0],str)and value.args[0].replace('_','').isupper()else None
                observed.append((Path(frame.f_code.co_filename).name,frame.f_code.co_name,frame.f_lineno,type(value).__name__,code))
                if len(observed)>24:del observed[0]
            return trace
        c.portal.call(lambda:sys.settrace(trace))
        try:restore(c,headers,first)
        finally:
            c.portal.call(lambda:sys.settrace(None))
            print('FINITE_PRODUCTION_FAILURE_EVENTS',observed)

        row,metadata,first_metadata_sha=_actual_restored_readers(database)
        first_bytes=metadata.read_bytes()
        with ro(metadata)as projection_reader:first_projection=relocation.metadata_projection(projection_reader)
        assert plans==[(100,1,1)]
        proof=Path(row['transcript_root'])/relocation.NAME;first_proof=proof.read_bytes();decoded=relocation.decode_provenance(first_proof)
        assert len(decoded['fingerprints'])==2 and len(decoded['metadata_derivations'])==1
        with database.connect()as d:
            assert d.execute("SELECT count(*)FROM restore_asset_generation WHERE state='ADOPTED'").fetchone()[0]==1
            assert d.execute("SELECT authority_kind FROM restore_asset_generation WHERE state='ADOPTED'").fetchone()[0]=='NATIVE_PERMIT'
        second=backup(c,headers);observed.clear();c.portal.call(lambda:sys.settrace(trace))
        try:restore(c,headers,second)
        finally:
            c.portal.call(lambda:sys.settrace(None))
            print('FINITE_SECOND_RESTORE_EVENTS',observed)

        row,metadata,second_metadata_sha=_actual_restored_readers(database)
        assert plans==[(100,1,1),(100,0,0)]
        with ro(metadata)as projection_reader:assert relocation.metadata_projection(projection_reader)==first_projection
        second_bytes=metadata.read_bytes();offsets=[i for i,(before,after)in enumerate(zip(first_bytes,second_bytes))if before!=after]
        # The original backup SQLite API produces a fresh authenticated backup
        # file even without this feature; it can advance SQLite header counters.
        # No target derivation runs on the aligned second restore.
        assert len(first_bytes)==len(second_bytes)and set(offsets)<=set(range(24,28))|set(range(40,44))|set(range(92,96))
        print('ORIGINAL_SQLITE_BACKUP_HEADER_COUNTER_OFFSETS',offsets,'PLAN_DELTAS',plans)
        assert(Path(row['transcript_root'])/relocation.NAME).read_bytes()==first_proof
    with client(root,root/'agent.db')as fresh:
        database=fresh.app.state.privacy_lifecycle.database
        row,metadata,sha=_actual_restored_readers(database)
        assert sha==second_metadata_sha and(Path(row['transcript_root'])/relocation.NAME).read_bytes()==first_proof


def _main_catalog_fixture():
    from home_study_agent.paper_scoring_source_match import ensure_schema
    c=sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE kg_resource(resource_id TEXT PRIMARY KEY,corpus_id TEXT,video_fingerprint TEXT);
      CREATE TABLE kg_document_page(page_id TEXT PRIMARY KEY,resource_id TEXT,image_fingerprint TEXT);''')
    ensure_schema(c)
    old='size:7:mtime_ns:1700000000000000043';new='size:7:mtime_ns:1700000000000000000'
    c.execute('INSERT INTO kg_resource VALUES(?,?,?)',('r','c',old));c.execute('INSERT INTO kg_document_page VALUES(?,?,?)',('p','r',old));c.commit()
    rows=[]
    for table,key,column,ident,role in [('kg_resource','resource_id','video_fingerprint','r','video'),('kg_document_page','page_id','image_fingerprint','p','image')]:
        rows.append({'table':table,'id_column':key,'id':ident,'column':column,'resource_id':'r','corpus_id':'c',
            'source_fingerprint':old,'target_fingerprint':new,'logical_name':'registered/kg/'+'a'*64+'/'+role+'/source.bin'})
    return c,rows


def test_exact_original_catalog_triggers_increment_only_by_successful_cas():
    from home_study_agent.registered_asset_restore import _rewrite_fixed_paths,_verify_rewritten_projection
    c,rows=_main_catalog_fixture();before=c.execute('SELECT revision FROM '+relocation.CATALOG_TABLE).fetchone()[0]
    expected=_rewrite_fixed_paths(c,[],rows)
    c.set_authorizer(lambda *args:sqlite3.SQLITE_OK)  # Test-only fresh verification stand-in; production closes writer and opens RO.
    try:
        _verify_rewritten_projection(c,expected,[],rows)
        assert c.execute('SELECT revision FROM '+relocation.CATALOG_TABLE).fetchone()[0]==before+2
    finally:c.close()


@pytest.mark.parametrize('fault',['unknown-trigger','modified-original-trigger','cache-trigger','overflow'])
def test_catalog_effect_rejects_unknown_sql_or_overflow_before_any_write(fault):
    from home_study_agent.registered_asset_restore import _rewrite_fixed_paths,_projection_digest
    c,rows=_main_catalog_fixture()
    if fault=='unknown-trigger':c.execute('CREATE TRIGGER unknown AFTER UPDATE ON kg_resource BEGIN SELECT 1;END')
    elif fault=='modified-original-trigger':
        c.execute('DROP TRIGGER paper_scoring_catalog_kg_resource_update')
        c.execute('CREATE TRIGGER paper_scoring_catalog_kg_resource_update AFTER UPDATE ON kg_resource BEGIN UPDATE paper_scoring_source_catalog_revision SET revision=revision+2;END')
    elif fault=='cache-trigger':c.execute('CREATE TRIGGER extra_cache AFTER UPDATE ON paper_scoring_source_catalog_revision BEGIN SELECT 1;END')
    else:c.execute('UPDATE '+relocation.CATALOG_TABLE+' SET revision=?',(2**63-1,))
    c.commit();before=_projection_digest(c)
    try:
        with pytest.raises(AssetSnapshotError):_rewrite_fixed_paths(c,[],rows)
        assert not c.in_transaction and _projection_digest(c)==before
    finally:c.close()


def test_second_cas_failure_rolls_back_first_target_and_original_catalog_trigger():
    from home_study_agent.registered_asset_restore import _rewrite_fixed_paths,_projection_digest
    c,rows=_main_catalog_fixture();before=_projection_digest(c)
    rows[1]['id']='not-present'
    try:
        with pytest.raises(AssetSnapshotError):_rewrite_fixed_paths(c,[],rows)
        c.set_authorizer(lambda *args:sqlite3.SQLITE_OK)
        assert not c.in_transaction and _projection_digest(c)==before
    finally:c.close()


def test_old_same_basename_member_keeps_original_asset_projection_digest():
    import json
    from home_study_agent.parent_restore_assets import _asset_projection
    root=Path('/original/media/old-generation');c=sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE kg_corpus(corpus_id TEXT,transcript_root TEXT,video_root TEXT,metadata_db_path TEXT,pdf_root TEXT,image_root TEXT);
      CREATE TABLE restore_asset_generation(generation_id TEXT,root_name TEXT);
      CREATE TABLE restore_asset_member(generation_id TEXT,relative_name TEXT,purpose TEXT,reference_json TEXT);''')
    c.execute('INSERT INTO kg_corpus VALUES(?,?,NULL,?,NULL,NULL)',('c',str(root/'kg-text'),str(root/'kg-meta'/relocation.NAME)))
    c.execute('INSERT INTO restore_asset_generation VALUES(?,?)',('g',root.name))
    reference={'table':'kg_corpus','id_column':'corpus_id','id':'c','column':'metadata_db_path','child_id':None}
    c.execute('INSERT INTO restore_asset_member VALUES(?,?,?,?)',('g','kg-meta/'+relocation.NAME,'metadata',json.dumps([reference])))
    try:assert _asset_projection(c,root=root)==_asset_projection(c,root=root,fingerprint_scope=False)
    finally:c.close()


def test_legacy_prepared_generation_uses_same_target_contract_without_native_authority(tmp_path,monkeypatch):
    import json
    from home_study_agent import registered_asset_restore as restore_module
    original_io = restore_module.io_path
    path_calls = []
    rejected_capability_receivers = []
    def path_receivers_only(value):
        if isinstance(value, restore_module._OwnedGeneration):
            rejected_capability_receivers.append(True)
            raise TypeError('OWNED_GENERATION_IS_CAPABILITY_NOT_PATH')
        path_calls.append(type(value).__name__)
        return original_io(value)
    monkeypatch.setattr(restore_module, 'io_path', path_receivers_only)
    from home_study_agent.database import Database
    from home_study_agent.registered_asset_snapshot import Policy,create_registered_asset_snapshot,_sqlite_snapshot
    from home_study_agent.registered_asset_restore import prepare_registered_asset_restore
    monkeypatch.setattr(relocation,'platform_precision_ns',lambda:100)
    database=Database(tmp_path/'original.db');database.initialize()
    _seed_real_registered_document(database,tmp_path/'source')
    before=hashlib.sha256(database.path.read_bytes()).hexdigest()
    owned=tmp_path/'owned';owned.mkdir(mode=0o700);target=tmp_path/'target';target.mkdir(mode=0o700)
    policy=Policy(100000,64,1024,64*1024*1024,2**30,64*1024*1024)
    cold=owned/'authenticated-cold.db';_sqlite_snapshot(database.path,cold,owned,policy)
    cold_sha=hashlib.sha256(cold.read_bytes()).hexdigest()
    stage=create_registered_asset_snapshot(cold,owned,policy)
    manifest=json.loads((stage/'manifest.json').read_bytes())
    contract={'coverage':'encrypted_registered_v1','asset_count':len(manifest['assets']),
      'total_bytes':sum(item['bytes']for item in manifest['assets']),
      'manifest_sha256':hashlib.sha256((stage/'manifest.json').read_bytes()).hexdigest(),
      'coverage_sha256':digest(canonical(manifest['coverage'])),'capsule':{'sha256':'0'*64,'size_bytes':1}}
    try:
        prepared=prepare_registered_asset_restore(cold,stage,target,expected_database_sha256=cold_sha,registered_contract=contract,policy=policy)
    finally:
        assert not rejected_capability_receivers, 'OWNED_GENERATION_WAS_ROUTED_TO_PATH_ADAPTER'
    assert prepared.status=='REGISTERED_ASSETS_PUBLISHED_PREPARED_DB_ONLY'
    assert path_calls  # Real legacy I/O ran, without routing the capability through io_path.
    restored=Database(prepared.database_path);row,metadata,sha=_actual_restored_readers(restored)
    assert relocation.decode_provenance((Path(row['transcript_root'])/relocation.NAME).read_bytes())['metadata_derivations'][0]['target_sha256']==sha
    assert hashlib.sha256(database.path.read_bytes()).hexdigest()==before
    assert hashlib.sha256(cold.read_bytes()).hexdigest()==cold_sha
    with restored.connect()as c:
        # A Prepared result has no adopted native publication authority.
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='restore_asset_generation'").fetchone()
        assert not exists or c.execute('SELECT count(*)FROM restore_asset_generation').fetchone()[0]==0


@pytest.mark.parametrize('role',['pdf','video','image'])
@pytest.mark.parametrize('mismatch',['size','missing_witness'])
def test_source_witness_rejection_keeps_original_restore_error(role,mismatch):
    fp='size:7:mtime_ns:1700000000000000043'
    item=asset(fp,role)
    if mismatch=='size':item['bytes']=8
    else:item['version_sha256s']=[]
    with pytest.raises(RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_FINGERPRINT_WITNESS_REQUIRED$'):
        relocation.planned_fingerprint(role,fp,item,100)


def test_malformed_source_and_new_relocation_guards_keep_original_classes():
    fp='size:7:mtime_ns:1700000000000000043';item=asset(fp)
    with pytest.raises(AssetSnapshotError,match='^REGISTERED_SOURCE_FINGERPRINT_REQUIRED$'):
        relocation.planned_fingerprint('pdf','invalid',item,100)
    item['version_sha256s']=None
    with pytest.raises(AssetSnapshotError,match='^REGISTERED_RESTORE_FINGERPRINT_RELOCATION_REQUIRED$'):
        relocation.planned_fingerprint('pdf',fp,item,100)


@pytest.mark.parametrize('columns,error,code',[
    ('book_id TEXT PRIMARY KEY,relative_pdf_path TEXT',RegisteredAssetRestoreError,'REGISTERED_RESTORE_FINGERPRINT_WITNESS_REQUIRED'),
    ('fingerprint TEXT,relative_pdf_path TEXT',AssetSnapshotError,'REGISTERED_RESTORE_FINGERPRINT_RELOCATION_REQUIRED')])
def test_missing_metadata_witness_preserves_error_without_weakening_schema(columns,error,code):
    from contextlib import contextmanager
    from home_study_agent.registered_asset_snapshot import Policy,record_key
    main=sqlite3.connect(':memory:');main.row_factory=sqlite3.Row
    metadata=sqlite3.connect(':memory:')
    try:
        main.execute('CREATE TABLE kg_corpus(corpus_id TEXT,transcript_root TEXT,video_root TEXT,metadata_db_path TEXT)')
        main.execute("INSERT INTO kg_corpus VALUES('book','/synthetic/text',NULL,'/synthetic/source.db')")
        main.commit();main.execute('PRAGMA query_only=ON')
        metadata.execute('CREATE TABLE textbook_book('+columns+')');metadata.commit()
        metadata.execute('PRAGMA query_only=ON')
        logical='registered/kg/'+record_key('kg','book')+'/metadata/source.db'
        item={'logical_name':logical,'kind':'kg','record_sha256':record_key('kg','book'),
          'role':'metadata','storage':'sqlite_backup','relative_path':'source.db','sha256':'a'*64,'bytes':1}
        calls=[]
        @contextmanager
        def admitted_metadata(name,asset):
            assert name==logical and asset is item;calls.append(name);yield metadata
        # Planning only: the caller supplies this original admitted read handle.
        # No file/capsule authentication or publication authority is awarded here.
        with pytest.raises(error,match='^'+code+'$'):
            relocation.collect_plan(main,{'assets':[item]},Policy(1000,64,1024,1024*1024,64*1024*1024,32*1024*1024),100,admitted_metadata)
        assert calls==[logical] and main.total_changes==1 and metadata.total_changes==0
    finally:metadata.close();main.close()
