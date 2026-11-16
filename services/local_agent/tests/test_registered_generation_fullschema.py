"""Main full-schema generation regression with synthetic registered assets.

The success path creates and authenticates a real encrypted capsule, disconnects
old roots and uses unchanged Main read-only consumers. It does not publish the
parent live database or prove a real family restore, DPAPI or crash recovery.
No ingest, OCR, model, device or artifact/candidate module is loaded.
"""
from contextlib import closing
import ast
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from home_study_agent.database import Database
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec,LocalCorpusIngestor
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent import registered_asset_backup as backup
from home_study_agent import registered_asset_snapshot as snapshot
from home_study_agent import registered_asset_restore as restore

WORKSPACE=Path(__file__).resolve().parents[3]
sha=snapshot.digest
fp=lambda p:f'size:{p.stat().st_size}:mtime_ns:{p.stat().st_mtime_ns}'


@pytest.fixture
def registered_generation(tmp_path):
    original=tmp_path/'old-machine';original.mkdir()
    source_data=tmp_path/'source-data';source_data.mkdir()
    target=tmp_path/'target';target.mkdir(mode=0o700)
    (target/'live.db').write_bytes(b'unchanged live DB')
    (target/'media').mkdir();(target/'media'/'old').write_bytes(b'unchanged managed media')
    (target/'registered-generation-old').mkdir();(target/'registered-generation-old'/'old').write_bytes(b'keep old generation')
    db=Database(source_data/'agent.db');db.initialize()
    def file(name,data):
        p=original/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return p
    pdf=file('pdf/original.pdf',b'%PDF public synthetic verified book')
    text=file('text/pages/one.txt',b'synthetic projected page text')
    image=file('images/one.png',b'synthetic projected image')
    video=file('video/one.mp4',b'synthetic projected video')
    meta=original/'metadata.db'
    tree=ast.parse((WORKSPACE/'services/local_agent/src/home_study_agent/knowledge_ingestion/textbook_pdf.py').read_text())
    ddl=next(n.value for n in ast.walk(tree)if isinstance(n,ast.Constant)and isinstance(n.value,str)and 'CREATE TABLE IF NOT EXISTS textbook_book('in n.value)
    with closing(sqlite3.connect(meta))as c:
        c.executescript(ddl)
        c.execute('''INSERT INTO textbook_book(book_id,relative_pdf_path,title,stage,grade,subject,semester,volume,edition,fingerprint,page_count,status,created_at,updated_at)
          VALUES ('book','original.pdf','Public','primary','4','math','first','1','synthetic',?,1,'completed','synthetic','synthetic')''',(fp(pdf),))
        c.execute('''INSERT INTO textbook_page(page_id,book_id,page_number,status,ocr_text_relative_path,created_at,updated_at)
          VALUES ('one','book',1,'completed','pages/one.txt','synthetic','synthetic')''');c.commit()
    spec=CorpusSpec('book','Public','local_scanned_document',text.parent.parent,video.parent,
                   'parent-selected','family-owned',meta,'source-v1',pdf.parent,image.parent)
    LocalCorpusIngestor(db).register(spec)  # Registration only; no start/run/OCR.
    with db.connect()as c:
        c.execute('''INSERT INTO kg_resource(resource_id,corpus_id,external_id,title,transcript_relative_path,video_relative_path,transcript_sha256,video_sha256,video_fingerprint,source_ref,license_boundary,confidence,review_status,version_label,ingested_at,updated_at)
          VALUES ('r','book','one','Public','pages/one.txt','one.mp4',?,?,?,'parent-selected','family-owned',1,'restricted','source-v1','synthetic','synthetic')''',(sha(text.read_bytes()),sha(video.read_bytes()),fp(video)))
        c.execute('''INSERT INTO kg_document_page(page_id,resource_id,document_kind,page_number,image_relative_path,image_sha256,image_fingerprint,ocr_engine,source_ref,license_boundary,version_label,created_at,updated_at)
          VALUES ('one','r','textbook',1,'one.png',?,?,'synthetic','parent-selected','family-owned','source-v1','synthetic','synthetic')''',(sha(image.read_bytes()),fp(image)))
        c.execute("INSERT INTO kg_segment(segment_id,resource_id,ordinal,start_char,end_char,text,text_sha256,source_ref,confidence,review_status,created_at) VALUES ('s','r',0,0,4,'text','synthetic-hash','parent-selected',1,'restricted','synthetic')")
        c.execute("INSERT INTO kg_segment_occurrence VALUES ('r','s',0,0,4)")
        c.commit()
    # Optional curriculum pending files come only from original source IDs.
    course=original/'curriculum/source.db';course.parent.mkdir()
    with closing(sqlite3.connect(course))as c:
        c.executescript("CREATE TABLE resources(resource_id);INSERT INTO resources VALUES ('one'),('inline');");c.commit()
    ct=file('curriculum/text/one.txt',b'course public text');cv=file('curriculum/video/one.mp4',b'course public movie')
    std=file('standard/book.pdf',b'%PDF public standard');content=file('standard/content.txt',b'public standard content')
    pages=file('standard/pages.jsonl',b'{"text":"public"}\n')
    manifest={'format':'hsc_official_curriculum_sources_v1','sources':[{'source_key':'std','pdf_filename':'book.pdf','pages_filename':'pages.jsonl','content_filename':'content.txt'}]}
    file('standard/manifest.json',snapshot.canonical(manifest))
    with db.connect()as c:
        c.execute('''INSERT INTO curriculum_corpus(corpus_id,title,corpus_type,source_database_path,transcript_root_path,video_root_path,source_ref,license_status,review_status,status,source_hash,imported_at,updated_at)
          VALUES ('course','Public','seyk',?,?,?,'parent-selected','public_personal_use','approved_baseline','ready','v1','synthetic','synthetic')''',(str(course),str(ct.parent),str(cv.parent)))
        c.execute('''INSERT INTO curriculum_resource(corpus_id,external_resource_id,title,description,stage_title,grade_title,subject_title,unit_path,transcript_text,transcript_relative_path,video_relative_path,local_video_available,import_job_id)
          VALUES ('course','one','Public','Public','primary','4','math','one','text','one.txt','one.mp4',1,'synthetic')''')
        c.execute('''INSERT INTO curriculum_standard_source(source_id,source_key,jurisdiction,source_type,subject_title,title,version_label,official_url,local_root_path,pdf_filename,pages_filename,pdf_sha256,text_sha256,status,imported_at,updated_at)
          VALUES ('std','std','CN','national_standard','math','Public','v1','public',?,'book.pdf','pages.jsonl',?,?,'structured','synthetic','synthetic')''',(str(std.parent),sha(std.read_bytes()),sha(content.read_bytes())))
        c.commit()
    # The admitted cold DB intentionally lives under target: its source-held
    # ancestor must be closed before a target-only GENERIC_WRITE dir flush.
    (target/'backups').mkdir();cold=target/'backups/cold.db'
    def cold_copy():
        if cold.exists():cold.unlink()
        with db.connect()as source,closing(sqlite3.connect(cold))as dest:
            source.backup(dest);assert dest.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete';dest.commit()
        backup.normalize_owned_snapshot(cold)
    cold_copy()
    owned=tmp_path/'verified-parent';owned.mkdir(mode=0o700)
    key=b'S'*32
    def capsule():
        cold_copy();contract=backup.create_capsule(cold,key,PrivacyLifecycleService._encrypt_file)
        stage=backup.decrypt_capsule(backup.capsule_path(cold),key,PrivacyLifecycleService._decrypt_file,owned,sha(cold.read_bytes()))
        policy=snapshot.Policy(**json.loads((stage/'manifest.json').read_bytes())['policy'])
        return stage,contract,policy
    def prepare(stage,contract,policy):
        return restore.prepare_registered_asset_restore(cold,stage,target,expected_database_sha256=sha(cold.read_bytes()),registered_contract=contract,policy=policy)
    def sentinels():
        assert (target/'live.db').read_bytes()==b'unchanged live DB'
        assert (target/'media/old').read_bytes()==b'unchanged managed media'
        assert (target/'registered-generation-old/old').read_bytes()==b'keep old generation'
    return SimpleNamespace(db=db,original=original,target=target,cold=cold,owned=owned,key=key,pdf=pdf,text=text,image=image,video=video,meta=meta,spec=spec,
                           capsule=capsule,prepare=prepare,sentinels=sentinels)


def assert_original_consumer(result):
    database=Database(result.database_path)
    with database.connect()as c:
        row=c.execute("SELECT * FROM kg_corpus WHERE corpus_id='book'").fetchone()
        spec=CorpusSpec(row['corpus_id'],row['title'],row['source_kind'],Path(row['transcript_root']),Path(row['video_root']),
            row['source_ref'],row['license_boundary'],Path(row['metadata_db_path']),row['version_label'],Path(row['pdf_root']),Path(row['image_root']))
        # Original read-only consumer, including the first-bind image branch.
        roots=LocalCorpusIngestor(database)._registered_document_roots(spec,c,None)
        assert roots==(row['pdf_root'],row['image_root'])
        for table,column in [('kg_resource','video_fingerprint'),('kg_document_page','image_fingerprint')]:
            stored=c.execute('SELECT '+column+' FROM '+table).fetchone()[0]
            root=Path(row['video_root']if table=='kg_resource'else row['image_root'])
            assert fp(root/('one.mp4'if table=='kg_resource'else'one.png'))==stored
        assert LocalCorpusIngestor._metadata(Path(row['metadata_db_path']),'local_scanned_document')
        assert (Path(row['transcript_root'])/'pages/one.txt').read_bytes()==b'synthetic projected page text'
        return row


def test_fullschema_real_capsule_disconnected_original_readonly_consumer(registered_generation):
    f=registered_generation;stage,contract,policy=f.capsule();cold=f.cold.read_bytes()
    m=json.loads((stage/'manifest.json').read_bytes())
    witnessed=[a for a in m['assets']if a['kind']=='kg'and a['role']in{'pdf','image','video'}]
    assert len(witnessed)==3 and all(len(a['version_sha256s'])==2 for a in witnessed)
    f.original.rename(f.original.with_name('offline-source'))
    result=f.prepare(stage,contract,policy)
    row=assert_original_consumer(result)
    with closing(sqlite3.connect(row['metadata_db_path']))as c:
        expected=c.execute('SELECT fingerprint FROM textbook_book').fetchone()[0]
        assert fp(Path(row['pdf_root'])/'original.pdf')==expected
    assert f.cold.read_bytes()==cold
    f.sentinels()


@pytest.mark.parametrize('role',['pdf','image','video'])
def test_changed_source_before_actual_backup_cannot_get_verified_witness(registered_generation,role):
    f=registered_generation;path=getattr(f,role);path.write_bytes(path.read_bytes()+b' changed')
    with pytest.raises(snapshot.AssetSnapshotError,match='FINGERPRINT_MISMATCH|SOURCE_HASH_MISMATCH'):f.capsule()
    assert not backup.capsule_path(f.cold).exists()
    assert path.read_bytes().endswith(b' changed')
    f.sentinels()


def test_trigger_cannot_change_nonroot_projection(registered_generation):
    f=registered_generation
    with f.db.connect()as c:
        c.execute("CREATE TRIGGER malicious_root AFTER UPDATE OF transcript_root ON kg_corpus BEGIN UPDATE kg_resource SET transcript_relative_path='missing-after-rewrite.txt'; END");c.commit()
    stage,contract,policy=f.capsule();cold=f.cold.read_bytes()
    with pytest.raises(restore.RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_SQL_AUTHORIZATION_DENIED$'):f.prepare(stage,contract,policy)
    assert f.cold.read_bytes()==cold
    with sqlite3.connect(f.cold)as c:
        assert c.execute('SELECT transcript_relative_path FROM kg_resource').fetchone()[0]=='pages/one.txt'
        assert c.execute("SELECT COUNT(*)FROM sqlite_master WHERE type='trigger'AND name='malicious_root'").fetchone()[0]==1
    assert not any(p.name.startswith('.registered-restore-')or(p.name.startswith('registered-generation-')and p.name!='registered-generation-old')for p in f.target.iterdir())
    f.sentinels()
