"""Synthetic local registration only: no family DB, OCR, model or network calls."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys

import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.schema import ensure_schema
from home_study_agent.knowledge_ingestion.document_evidence import DocumentCorpusSpec, LocalDocumentEvidenceIngestor
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor
from home_study_agent.knowledge_ingestion.textbook_pdf import TextbookPdfOcrPipeline
from home_study_agent.registered_asset_snapshot import Policy, create_registered_asset_snapshot, verify_registered_asset_snapshot


def document(tmp_path):
    pdf_root=tmp_path/'pdf';pdf_root.mkdir();pdf=pdf_root/'四年级上册_数学_苏教版.pdf';pdf.write_bytes(b'%PDF synthetic original')
    text=tmp_path/'ocr';metadata=tmp_path/'metadata.db'
    pipeline=TextbookPdfOcrPipeline(pdf_root,text,metadata,page_counter=lambda _:1)
    assert pipeline.prepare()['books']==1  # Preparation only; never run OCR.
    with sqlite3.connect(metadata) as con:
        page_id=con.execute('SELECT page_id FROM textbook_page').fetchone()[0]
        relative='pages/'+page_id+'.txt'
        con.execute("UPDATE textbook_page SET status='completed',ocr_text_relative_path=?,ocr_engine='synthetic',ocr_confidence=.9",(relative,))
    (text/'pages').mkdir(exist_ok=True);(text/relative).write_text('原教材可追溯的合成学习内容。',encoding='utf-8')
    images=tmp_path/'images';images.mkdir();(images/(page_id+'.png')).write_bytes(b'synthetic original scan')
    database=Database(tmp_path/'study.db');database.initialize()
    spec=DocumentCorpusSpec('book','合成教材','textbook',text,images,'parent-selected-local','family_owned_local','synthetic',metadata_db_path=metadata,pdf_root=pdf_root)
    return database,spec,pdf


def corpus_row(database):
    with database.connect() as con:return dict(con.execute('SELECT * FROM kg_corpus WHERE corpus_id=?',('book',)).fetchone())


def test_real_old_kg_corpus_migration_twice_preserves_every_old_column(tmp_path):
    path=tmp_path/'old.db'
    with sqlite3.connect(path) as con:
        con.execute('''CREATE TABLE kg_corpus(corpus_id TEXT PRIMARY KEY,title TEXT NOT NULL,source_kind TEXT NOT NULL,
            transcript_root TEXT NOT NULL,video_root TEXT,metadata_db_path TEXT,source_ref TEXT NOT NULL,
            license_boundary TEXT NOT NULL,review_status TEXT NOT NULL,version_label TEXT NOT NULL,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(source_kind,transcript_root))''')
        old=('old','旧教材','seyk','/registered/original',None,None,'original','local','approved','v-original','first','last')
        con.execute('INSERT INTO kg_corpus VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',old);con.commit()
        ensure_schema(con,'migration-one');con.commit();ensure_schema(con,'migration-two');con.commit()
        assert con.execute('SELECT * FROM kg_corpus').fetchone()==old+(None,None)
        columns={r[1]:r for r in con.execute('PRAGMA table_info(kg_corpus)')}
        assert columns['pdf_root'][3]==columns['image_root'][3]==0
        assert con.execute('SELECT COUNT(*) FROM kg_schema_migration WHERE version=36').fetchone()[0]==1


def test_original_register_and_positional_spec_store_explicit_roots(tmp_path):
    database,spec,_=document(tmp_path)
    source=spec.corpus_spec()
    # All nine original positional arguments retain their meaning.
    original=CorpusSpec(source.corpus_id,source.title,source.source_kind,source.transcript_root,source.video_root,source.source_ref,source.license_boundary,source.metadata_db_path,source.version_label)
    LocalCorpusIngestor(database).register(replace(original,pdf_root=spec.pdf_root,image_root=spec.image_root))
    row=corpus_row(database)
    assert row['pdf_root']==str(spec.pdf_root) and row['image_root']==str(spec.image_root)
    with database.connect() as con:assert con.execute('SELECT COUNT(*) FROM kg_ingest_job').fetchone()[0]==0


def test_document_import_restart_and_actual_snapshot_find_pdf_image_and_ocr(tmp_path):
    database,spec,pdf=document(tmp_path);ingestor=LocalDocumentEvidenceIngestor(database)
    job=ingestor.start(spec,idempotency_key='parent-explicit-one')
    assert ingestor.run(spec,job,hash_images=True,throttle_ms=0)['status']=='completed'
    reopened=Database(database.path)
    row=corpus_row(reopened);assert row['pdf_root']==str(spec.pdf_root) and row['image_root']==str(spec.image_root)
    parent=tmp_path/'private-stage';parent.mkdir(mode=0o700)
    # Caller supplies measured finite budgets; the real application schema is
    # larger than the tiny registry used by the module's standalone tests.
    source_sizes=sum(p.stat().st_size for base in (reopened.path,spec.metadata_db_path)
                     for p in (base,Path(str(base)+'-wal'),Path(str(base)+'-shm')) if p.exists())
    file_budget=source_sizes+65536
    policy=Policy(100000,64,1024,64*1024*1024,file_budget*4,file_budget)
    stage=create_registered_asset_snapshot(reopened.path,parent,policy)
    verify_registered_asset_snapshot(stage,policy)
    manifest=json.loads((stage/'manifest.json').read_bytes())
    roles={item['role'] for item in manifest['assets']}
    assert {'pdf','image','transcript','metadata'}<=roles
    pdf_asset=next(item for item in manifest['assets'] if item['role']=='pdf')
    assert (stage/pdf_asset['logical_name']).read_bytes()==pdf.read_bytes()
    assert str(tmp_path) not in (stage/'manifest.json').read_text()


def test_legacy_root_supplement_preserves_all_resources_reviews_jobs_and_versions(tmp_path):
    database,spec,_=document(tmp_path);ingestor=LocalDocumentEvidenceIngestor(database)
    job=ingestor.start(spec,idempotency_key='old-job');ingestor.run(spec,job,hash_images=True,throttle_ms=0)
    with database.connect() as con:
        con.execute("UPDATE kg_corpus SET pdf_root=NULL,image_root=NULL,review_status='approved' WHERE corpus_id='book'")
        con.execute("UPDATE kg_resource SET review_status='approved'");con.commit()
        tables=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('kg_corpus','kg_schema_migration')")]
        before={t:con.execute('SELECT * FROM "'+t+'"').fetchall() for t in tables}
    LocalCorpusIngestor(database).register(spec.corpus_spec())
    row=corpus_row(database);assert row['version_label']==spec.version_label and row['review_status']=='approved'
    with database.connect() as con:
        assert {t:con.execute('SELECT * FROM "'+t+'"').fetchall() for t in tables}==before
    # An original caller omitting the appended optional arguments cannot erase roots.
    LocalCorpusIngestor(database).register(replace(spec.corpus_spec(),pdf_root=None,image_root=None))
    assert corpus_row(database)['pdf_root']==str(spec.pdf_root)


def test_pdf_reference_without_explicit_registered_root_requires_association(tmp_path):
    database,spec,_=document(tmp_path)
    with pytest.raises(ValueError,match='^MISSING_REGISTERED_PDF_ROOT$'):
        LocalDocumentEvidenceIngestor(database).start(replace(spec,pdf_root=None),idempotency_key='missing')
    with database.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM kg_corpus').fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM kg_ingest_job').fetchone()[0]==0


@pytest.mark.parametrize('change',['missing','different','no-metadata','caller-dict','symlink'])
def test_unproven_or_unsafe_root_does_not_register(tmp_path,change):
    database,spec,pdf=document(tmp_path)
    if change=='missing':bad=tmp_path/'absent';expected='REGISTERED_ASSET_ROOT_REQUIRED'
    elif change=='different':
        bad=tmp_path/'wrong';bad.mkdir();(bad/pdf.name).write_bytes(b'different source bytes');expected='REGISTERED_PDF_SOURCE_MISMATCH'
    elif change=='no-metadata':bad=spec.pdf_root;spec=replace(spec,metadata_db_path=None);expected='REGISTERED_PDF_ASSOCIATION_REQUIRED'
    elif change=='caller-dict':bad={'root':str(spec.pdf_root)};expected='REGISTERED_ASSET_ROOT_REQUIRED'
    else:bad=tmp_path/'alias';bad.symlink_to(spec.pdf_root,target_is_directory=True);expected='REGISTERED_ASSET_ROOT_REQUIRED'
    with pytest.raises(ValueError,match='^'+expected+'$'):LocalCorpusIngestor(database).register(replace(spec.corpus_spec(),pdf_root=bad))
    with database.connect() as con:assert con.execute('SELECT COUNT(*) FROM kg_corpus').fetchone()[0]==0


def test_existing_binding_conflicts_and_version_changes_are_rejected_without_reset(tmp_path):
    database,spec,_=document(tmp_path);register=LocalCorpusIngestor(database).register;register(spec.corpus_spec());before=corpus_row(database)
    other=tmp_path/'other';shutil.copytree(spec.pdf_root,other,copy_function=shutil.copy2)
    with pytest.raises(ValueError,match='^REGISTERED_ASSET_ROOT_CONFLICT$'):register(replace(spec.corpus_spec(),pdf_root=other))
    with pytest.raises(ValueError,match='^REGISTERED_ASSET_SOURCE_CONFLICT$'):register(replace(spec.corpus_spec(),version_label='different-version'))
    assert corpus_row(database)==before


@pytest.mark.parametrize('change',['missing','different','no-evidence'])
def test_legacy_image_root_supplement_must_match_original_page_evidence(tmp_path,change):
    database,spec,_=document(tmp_path);ingestor=LocalDocumentEvidenceIngestor(database)
    job=ingestor.start(spec,idempotency_key='image-evidence');ingestor.run(spec,job,hash_images=True,throttle_ms=0)
    with database.connect() as con:
        con.execute("UPDATE kg_corpus SET image_root=NULL WHERE corpus_id='book'")
        if change=='no-evidence':con.execute('UPDATE kg_document_page SET image_sha256=NULL,image_fingerprint=NULL')
        con.commit()
    before=corpus_row(database);image=next(spec.image_root.iterdir())
    if change=='missing':image.unlink();expected='REGISTERED_ASSET_ROOT_REQUIRED'
    elif change=='different':image.write_bytes(b'changed unrelated scan');expected='REGISTERED_IMAGE_SOURCE_MISMATCH'
    else:expected='REGISTERED_IMAGE_EVIDENCE_REQUIRED'
    with pytest.raises(ValueError,match='^'+expected+'$'):LocalCorpusIngestor(database).register(spec.corpus_spec())
    assert corpus_row(database)==before


def test_legacy_missing_root_still_rejects_snapshot_without_inventing_association(tmp_path):
    database,spec,_=document(tmp_path);ingestor=LocalDocumentEvidenceIngestor(database)
    job=ingestor.start(spec,idempotency_key='legacy');ingestor.run(spec,job,hash_images=True,throttle_ms=0)
    with database.connect() as con:con.execute('UPDATE kg_corpus SET pdf_root=NULL,image_root=NULL');con.commit()
    parent=tmp_path/'private-stage';parent.mkdir(mode=0o700);size=database.path.stat().st_size+65536
    with pytest.raises(ValueError,match='^MISSING_REGISTERED_IMAGE_ROOT$'):
        create_registered_asset_snapshot(database.path,parent,Policy(100000,64,1024,64*1024*1024,size*4,size))
    assert list(parent.iterdir())==[]


def test_registered_metadata_relative_escape_cannot_authorize_another_file(tmp_path):
    database,spec,_=document(tmp_path)
    with sqlite3.connect(spec.metadata_db_path) as con:con.execute("UPDATE textbook_book SET relative_pdf_path='../outside.pdf'")
    with pytest.raises(ValueError,match='^REGISTERED_ASSET_ROOT_REQUIRED$'):LocalCorpusIngestor(database).register(spec.corpus_spec())


def test_plain_original_corpus_import_keeps_both_optional_roots_null(tmp_path):
    database=Database(tmp_path/'study.db');database.initialize();text=tmp_path/'text';text.mkdir();(text/'one.txt').write_text('普通合成教学内容。',encoding='utf-8')
    spec=CorpusSpec('plain','普通','seyk',text,None,'original','local')
    ingestor=LocalCorpusIngestor(database);job=ingestor.start(spec,idempotency_key='plain');assert ingestor.run(spec,job)['status']=='completed'
    with database.connect() as con:assert tuple(con.execute('SELECT pdf_root,image_root FROM kg_corpus').fetchone())==(None,None)


def test_nested_transcript_and_video_locators_keep_their_registered_root(tmp_path):
    database=Database(tmp_path/'study.db');database.initialize()
    text=tmp_path/'text';video=tmp_path/'video';(text/'nested').mkdir(parents=True);(video/'unit').mkdir(parents=True)
    (text/'nested/one.txt').write_text('嵌套路径中的原教学内容。',encoding='utf-8');(video/'unit/one.mp4').write_bytes(b'synthetic local video')
    spec=CorpusSpec('nested','嵌套','seyk',text,video,'original','local')
    ingestor=LocalCorpusIngestor(database);job=ingestor.start(spec,idempotency_key='nested');assert ingestor.run(spec,job)['status']=='completed'
    with Database(database.path).connect() as con:
        assert tuple(con.execute('SELECT transcript_relative_path,video_relative_path FROM kg_resource').fetchone())==('nested/one.txt','unit/one.mp4')


def test_original_parent_inline_import_does_not_acquire_external_roots(tmp_path):
    from home_study_agent.knowledge_graph.textbook_page_service import TextbookOcrPageInput, TextbookOcrPageService
    database=Database(tmp_path/'study.db');database.initialize()
    TextbookOcrPageService(database).import_page(TextbookOcrPageInput('原家长教材页','公开合成教材页内容。'))
    with database.connect() as con:
        assert tuple(con.execute('SELECT pdf_root,image_root FROM kg_corpus').fetchone())==(None,None)


def test_existing_textbook_cli_passes_only_explicit_roots(tmp_path,monkeypatch,capsys):
    database,spec,_=document(tmp_path)
    script=Path(__file__).parents[1]/'scripts/ingest_textbook_pages.py';module_spec=importlib.util.spec_from_file_location('registered_root_cli',script);cli=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(cli)
    monkeypatch.setattr(sys,'argv',[str(script),'--app-db',str(database.path),'--metadata-db',str(spec.metadata_db_path),'--ocr-root',str(spec.ocr_text_root),'--pdf-root',str(spec.pdf_root),'--image-root',str(spec.image_root),'--corpus-id','book','--idempotency-key','cli-explicit'])
    cli.main();assert corpus_row(Database(database.path))['pdf_root']==str(spec.pdf_root);assert 'completed' in capsys.readouterr().out
