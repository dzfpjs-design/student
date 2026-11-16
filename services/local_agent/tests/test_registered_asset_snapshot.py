"""Real temporary files/SQLite; no App, network, provider or family content."""
import dataclasses
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'services/local_agent/src'))
from home_study_agent import registered_asset_snapshot as m
sha=lambda b:hashlib.sha256(b).hexdigest()


def policy(**values):
    return dataclasses.replace(m.Policy(100000,64,1024,64*1024*1024,8*1024*1024,2*1024*1024),**values)


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.base=Path(self.temporary.name).resolve()
        self.parent=self.base/'owned';self.parent.mkdir(mode=0o700)
        self.db=self.base/'registry.db';self.c=sqlite3.connect(self.db)
        self.c.executescript('''
        CREATE TABLE curriculum_corpus(corpus_id TEXT PRIMARY KEY,source_database_path TEXT,transcript_root_path TEXT,video_root_path TEXT,source_hash TEXT);
        CREATE TABLE curriculum_resource(row_id INTEGER PRIMARY KEY,corpus_id TEXT,transcript_relative_path TEXT,video_relative_path TEXT,local_video_available INTEGER);
        CREATE TABLE kg_corpus(corpus_id TEXT PRIMARY KEY,source_kind TEXT,transcript_root TEXT,video_root TEXT,metadata_db_path TEXT,version_label TEXT,pdf_root TEXT,image_root TEXT);
        CREATE TABLE kg_resource(resource_id TEXT PRIMARY KEY,corpus_id TEXT,transcript_relative_path TEXT,video_relative_path TEXT,transcript_sha256 TEXT,video_sha256 TEXT);
        CREATE TABLE kg_document_page(page_id TEXT PRIMARY KEY,resource_id TEXT,image_relative_path TEXT,image_sha256 TEXT);
        CREATE TABLE curriculum_standard_source(source_id TEXT PRIMARY KEY,source_key TEXT,local_root_path TEXT,pdf_filename TEXT,pages_filename TEXT,pdf_sha256 TEXT,text_sha256 TEXT);
        CREATE TABLE voice_input(local_path TEXT); CREATE TABLE capture_artifact(local_path TEXT);
        CREATE TABLE kg_parent_textbook_ocr_artifact(raw_image_path TEXT);
        ''');self.c.commit();self.extra=[]

    def tearDown(self):
        for c in self.extra:c.close()
        self.c.close();self.temporary.cleanup()

    def file(self,relative,data=b'public synthetic'):
        p=self.base/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return p

    def sqlite(self,relative,wal=False):
        p=self.base/relative;p.parent.mkdir(parents=True,exist_ok=True)
        c=sqlite3.connect(p)
        if wal:c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA wal_autocheckpoint=0')
        c.execute('CREATE TABLE public_rows(value TEXT)');c.execute("INSERT INTO public_rows VALUES ('original')");c.commit()
        if wal:self.extra.append(c)
        else:c.close()
        return p,c

    def kg(self,key,root,rel='public.txt',**kw):
        p=self.file(str(root.relative_to(self.base)/rel),kw.pop('content',b'public text'))
        self.c.execute('INSERT INTO kg_corpus VALUES (?,?,?,?,?,?,?,?)',(key,'local_scanned_document',str(root),kw.get('video_root'),kw.get('metadata'), 'unchanged-version',kw.get('pdf_root'),kw.get('image_root')))
        self.c.execute('INSERT INTO kg_resource VALUES (?,?,?,?,?,?)',(key+'-resource',key,rel,kw.get('video'),sha(p.read_bytes()),kw.get('video_sha')));self.c.commit();return p

    def all_categories(self,wal=False):
        db,writer=self.sqlite('curriculum/source.db',wal)
        self.file('curriculum/text/public.txt',b'legacy text');self.file('curriculum/video/public.mp4',b'public video')
        self.c.execute('INSERT INTO curriculum_corpus VALUES (?,?,?,?,NULL)',('legacy',str(db),str(db.parent/'text'),str(db.parent/'video')))
        self.c.execute('INSERT INTO curriculum_resource VALUES (1,?,?,?,1)',('legacy','public.txt','public.mp4'))
        metadata,_=self.sqlite('textbook/metadata.db')
        with sqlite3.connect(metadata) as c:
            c.executescript("CREATE TABLE textbook_book(book_id TEXT,relative_pdf_path TEXT);CREATE TABLE textbook_page(page_id TEXT,ocr_text_relative_path TEXT);INSERT INTO textbook_book VALUES('b','math.pdf');INSERT INTO textbook_page VALUES('p','pages/public.txt');")
        pdf=self.file('textbook/pdf/math.pdf',b'%PDF public textbook');image=self.file('textbook/images/page.png',b'public image')
        with sqlite3.connect(metadata) as c:
            c.execute('ALTER TABLE textbook_book ADD COLUMN fingerprint TEXT')
            c.execute('UPDATE textbook_book SET fingerprint=?', (f'size:{pdf.stat().st_size}:mtime_ns:{pdf.stat().st_mtime_ns}',))
        video=self.file('textbook/video/same.mp4',b'public kg video')
        self.kg('book',self.base/'textbook/ocr','pages/public.txt',metadata=str(metadata),pdf_root=str(self.base/'textbook/pdf'),image_root=str(self.base/'textbook/images'),video_root=str(video.parent),video=video.name,video_sha=sha(video.read_bytes()))
        self.c.execute('INSERT INTO kg_document_page VALUES (?,?,?,?)',('page','book-resource',image.name,sha(image.read_bytes())))
        self.kg('different-root',self.base/'another-root','public.txt',content=b'independent same basename')
        self.c.execute("INSERT INTO kg_corpus VALUES ('inline','inline','local://family-textbook-ocr-pages',NULL,NULL,'v1',NULL,NULL)")
        std=self.file('standard/math.pdf',b'%PDF official public');self.file('standard/pages.jsonl',b'{"page":1,"text":"public"}\n')
        self.file('standard/content.txt',b'public official content');self.file('standard/ocr.txt',b'public OCR')
        self.file('standard/manifest.json',m.canonical({'format':'hsc_official_curriculum_sources_v1','sources':[{'source_key':'math','pdf_filename':'math.pdf','pages_filename':'pages.jsonl','content_filename':'content.txt','ocr_text_filename':'ocr.txt'}]}))
        self.c.execute('INSERT INTO curriculum_standard_source VALUES (?,?,?,?,?,?,?)',('s','math',str(std.parent),std.name,'pages.jsonl',sha(std.read_bytes()),sha(b'public OCR')))
        # The existing HSC1 collectors remain solely responsible for these.
        for t,col in [('voice_input','local_path'),('capture_artifact','local_path'),('kg_parent_textbook_ocr_artifact','raw_image_path')]:
            self.c.execute('INSERT INTO '+t+'('+col+') VALUES (?)',(str(self.base/'deliberately-unavailable-hsc1'),))
        self.c.commit();return writer

    def snapshot(self,p=None):return m.create_registered_asset_snapshot(self.db,self.parent,p or policy())

    def failure(self,code,p=None):
        with self.assertRaisesRegex(m.AssetSnapshotError,'^'+code+'$'):self.snapshot(p)
        self.assertEqual(list(self.parent.iterdir()),[])

    def test_all_registered_categories_roundtrip_and_no_absolute_path_manifest(self):
        self.all_categories();before=self.db.read_bytes();stage=self.snapshot();raw=(stage/'manifest.json').read_bytes();manifest=json.loads(raw)
        self.assertNotIn(str(self.base).encode(),raw);self.assertEqual(self.db.read_bytes(),before)
        self.assertEqual(m.verify_registered_asset_snapshot(stage,policy())['files'],14)
        self.assertEqual({x['kind'] for x in manifest['assets']},{'curriculum','kg','standard'})
        self.assertEqual({x['role'] for x in manifest['assets']},{'metadata','transcript','video','pdf','image','manifest','ocr_pages','content','ocr_text'})
        self.assertEqual(len(manifest['coverage']),5)
        self.assertFalse((stage/'.working').exists())

    def test_same_basename_different_registered_roots_are_not_deduplicated(self):
        self.kg('a',self.base/'A');self.kg('b',self.base/'B',content=b'other root')
        stage=self.snapshot();assets=json.loads((stage/'manifest.json').read_bytes())['assets']
        self.assertEqual(len(assets),2);self.assertNotEqual(assets[0]['logical_name'],assets[1]['logical_name']);self.assertNotEqual(assets[0]['sha256'],assets[1]['sha256'])

    def test_real_wal_rows_survive_readonly_backup_and_no_raw_sidecars_published(self):
        writer=self.all_categories(wal=True);writer.execute("INSERT INTO public_rows VALUES ('committed-in-wal')");writer.commit()
        stage=self.snapshot();assets=json.loads((stage/'manifest.json').read_bytes())['assets'];item=next(x for x in assets if x['kind']=='curriculum' and x['role']=='metadata')
        with sqlite3.connect(stage/item['logical_name']) as c:self.assertEqual(c.execute('SELECT value FROM public_rows').fetchall(),[('original',),('committed-in-wal',)])
        self.assertFalse(any(p.name.endswith(('-wal','-shm','-journal')) for p in stage.rglob('*')))
        self.assertEqual(writer.execute('SELECT COUNT(*) FROM public_rows').fetchone()[0],2)

    def test_required_file_missing_has_no_completion_or_stage(self):
        p=self.kg('a',self.base/'text');p.unlink();self.failure('MISSING_REGISTERED_FILE')

    def test_changed_source_during_held_read_has_no_completed_stage(self):
        p=self.kg('a',self.base/'text');original=m.HeldFile.chunks;changed=False
        def mutate(file):
            nonlocal changed
            for block in original(file):
                yield block
                if file.root==p.parent and not changed:p.write_bytes(b'changed source');changed=True
        with patch.object(m.HeldFile,'chunks',mutate):self.failure('ASSET_CHANGED')
        self.assertTrue(changed)

    def test_registered_hash_mismatch_does_not_invent_new_evidence_version(self):
        p=self.kg('a',self.base/'text');p.write_bytes(b'replaced before snapshot');self.failure('REGISTERED_SOURCE_HASH_MISMATCH')

    @unittest.skipIf(os.name=='nt','Real POSIX alias controls; Windows needs separate native target')
    def test_file_symlink_hardlink_and_directory_symlink_rejected(self):
        for kind in ['file','hardlink','directory']:
            with self.subTest(kind=kind):
                self.c.execute('DELETE FROM kg_resource');self.c.execute('DELETE FROM kg_corpus');self.c.commit()
                root=self.base/kind;p=self.kg(kind,root)
                if kind=='file':p.unlink();p.symlink_to(self.file(kind+'-other.txt'))
                elif kind=='hardlink':os.link(p,self.base/'hardlink-alias.txt')
                else:root.rename(self.base/'real-root');root.symlink_to(self.base/'real-root',target_is_directory=True)
                self.failure('ASSET_FILE_BOUND_OR_ALIAS' if kind=='hardlink' else 'SOURCE_ALIAS_OR_REPLACED')

    def test_case_collision_is_rejected(self):
        # Case-insensitive host volumes alias these names, so retain the same
        # content hash and let the manifest namespace collision be decisive.
        self.kg('a',self.base/'text','Page.txt');p=self.file('text/page.txt',b'public text')
        self.c.execute('INSERT INTO kg_resource VALUES (?,?,?,?,?,NULL)',('second','a',p.name,None,sha(p.read_bytes())));self.c.commit();self.failure('ASSET_CASE_COLLISION')

    def test_unsafe_relative_ADS_UNC_device_escape_names(self):
        self.kg('a',self.base/'text')
        for value in ['../outside','/absolute','a:stream','a\\b','//server/share','NUL.txt','a/./b','a//b','a/space.']:
            with self.subTest(value=value):
                self.c.execute('UPDATE kg_resource SET transcript_relative_path=?',(value,));self.c.commit();self.failure('UNSAFE_ASSET_PATH')

    def test_unregistered_pdf_and_image_roots_are_not_inferred(self):
        self.all_categories();self.c.execute('UPDATE kg_corpus SET image_root=NULL');self.c.commit();self.failure('MISSING_REGISTERED_IMAGE_ROOT')
        self.c.execute('DELETE FROM kg_document_page');self.c.execute('UPDATE kg_corpus SET pdf_root=NULL');self.c.commit();self.failure('MISSING_REGISTERED_PDF_ROOT')

    def test_old_schema_without_nullable_future_columns_still_fails_closed(self):
        self.all_categories();self.c.execute('ALTER TABLE kg_corpus DROP COLUMN image_root');self.c.execute('ALTER TABLE kg_corpus DROP COLUMN pdf_root');self.c.commit();self.failure('MISSING_REGISTERED_IMAGE_ROOT')

    def test_original_virtual_bundle_is_explicit_but_conflict_rejected(self):
        self.c.execute("INSERT INTO kg_corpus VALUES ('v','bundle','bundle:v',NULL,NULL,'v1',NULL,NULL)");self.c.commit();stage=self.snapshot()
        self.assertEqual(m.verify_registered_asset_snapshot(stage,policy())['files'],0);shutil.rmtree(stage)
        self.c.execute("UPDATE kg_corpus SET metadata_db_path='/unregistered.db'");self.c.commit();self.failure('VIRTUAL_REGISTRY_CONFLICT')

    def test_count_total_file_and_manifest_limits_do_not_truncate(self):
        self.all_categories();self.failure('ASSET_COUNT_BOUND',policy(max_files=1))
        self.failure('ASSET_MANIFEST_BOUND',policy(max_manifest_bytes=64))
        p=self.base/'textbook/video/same.mp4';p.write_bytes(b'x'*120000)
        self.c.execute('UPDATE kg_resource SET video_sha256=? WHERE corpus_id=?',(sha(p.read_bytes()),'book'));self.c.commit()
        self.failure('ASSET_FILE_BOUND_OR_ALIAS',policy(max_file_bytes=100000,max_total_bytes=500000))
        self.failure('ASSET_TOTAL_BOUND',policy(max_file_bytes=125000,max_total_bytes=125000))

    def test_invalid_policy_types_and_unbounded_values_rejected_before_writes(self):
        for changes in [{'max_file_bytes':0},{'max_total_bytes':float('inf')},{'max_file_bytes':True},{'max_file_bytes':9000000},{'max_files':100001},{'max_depth':65},{'max_path_utf8':1025},{'max_manifest_bytes':64*1024*1024+1}]:
            with self.subTest(changes=changes):self.failure('INVALID_POLICY',policy(**changes))

    def test_standard_manifest_scope_change_rejected(self):
        self.all_categories();p=self.base/'standard/manifest.json';d=json.loads(p.read_bytes());d['sources'][0]['pdf_filename']='foreign.pdf';p.write_bytes(m.canonical(d));self.failure('STANDARD_MANIFEST_SCOPE_REQUIRED')

    def test_roundtrip_tamper_extra_file_and_path_redirection_rejected(self):
        self.kg('a',self.base/'text');stage=self.snapshot();manifest=stage/'manifest.json';raw=manifest.read_bytes();d=json.loads(raw);asset=stage/d['assets'][0]['logical_name'];original=asset.read_bytes()
        asset.write_bytes(b'broken');self.assertRaises(m.AssetSnapshotError,m.verify_registered_asset_snapshot,stage,policy());asset.write_bytes(original)
        (stage/'extra').write_bytes(b'unregistered');self.assertRaises(m.AssetSnapshotError,m.verify_registered_asset_snapshot,stage,policy());(stage/'extra').unlink()
        d['assets'][0]['logical_name']='../escape';manifest.write_bytes(m.canonical(d));self.assertRaises(m.AssetSnapshotError,m.verify_registered_asset_snapshot,stage,policy())

    def test_real_current_database_schema_and_original_corpus_registration(self):
        from home_study_agent.database import Database
        from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec,LocalCorpusIngestor
        path=self.base/'actual-current-schema.db';database=Database(path);database.initialize()
        text=self.file('original-registration/public.txt',b'original registered public source')
        spec=CorpusSpec('original-id','public source','seyk',text.parent,None,'synthetic-source','user_local')
        LocalCorpusIngestor(database).register(spec)
        with database.connect() as c:
            c.execute('''INSERT INTO kg_resource(resource_id,corpus_id,external_id,title,
              transcript_relative_path,transcript_sha256,source_ref,license_boundary,
              confidence,review_status,version_label,ingested_at,updated_at)
              VALUES ('original-resource','original-id','public','public',?,?,'synthetic-source',
              'user_local',.8,'restricted','local-2026-08','fixed-time','fixed-time')''',(text.name,sha(text.read_bytes())))
            c.commit()
        size=max(path.stat().st_size*2,2*1024*1024);p=policy(max_file_bytes=size,max_total_bytes=size*4)
        stage=m.create_registered_asset_snapshot(path,self.parent,p)
        self.assertEqual(m.verify_registered_asset_snapshot(stage,p)['files'],1)
        with database.connect() as c:
            self.assertEqual(c.execute('SELECT transcript_root FROM kg_corpus').fetchone()[0],str(text.parent))
            self.assertEqual(c.execute('SELECT resource_id,version_label FROM kg_resource').fetchone()[:],('original-resource','local-2026-08'))

    def test_duplicate_file_bindings_retain_all_original_version_digests(self):
        self.all_categories();stage=self.snapshot();assets=json.loads((stage/'manifest.json').read_bytes())['assets']
        item=next(x for x in assets if x['kind']=='kg' and x['role']=='transcript' and x['relative_path']=='pages/public.txt')
        self.assertEqual(len(item['version_sha256s']),2)

    def test_db_or_wal_changed_after_stable_copy_fails_before_stage_completion(self):
        writer=self.all_categories(wal=True);original=m.HeldFile.copy;changed=False
        def mutate(file,target):
            nonlocal changed
            result=original(file,target)
            if file.parts[-1]=='source.db-wal' and not changed:
                writer.execute("INSERT INTO public_rows VALUES ('concurrent update')");writer.commit();changed=True
            return result
        with patch.object(m.HeldFile,'copy',mutate):self.failure('ASSET_CHANGED')
        self.assertTrue(changed)

    def test_empty_registered_local_root_is_explicit_and_missing_root_rejected(self):
        root=self.base/'empty-original';root.mkdir()
        self.c.execute('INSERT INTO kg_corpus VALUES (?,?,?,?,NULL,?,NULL,NULL)',('empty','seyk',str(root),None,'original-version'));self.c.commit()
        stage=self.snapshot();d=json.loads((stage/'manifest.json').read_bytes());self.assertEqual(d['coverage'][0]['registry_columns'],['transcript_root']);shutil.rmtree(stage)
        root.rmdir();self.failure('MISSING_REGISTERED_FILE')

    def test_no_connection_or_held_file_descriptor_accumulation(self):
        if not Path('/dev/fd').is_dir():self.skipTest('POSIX finite descriptor accounting only')
        self.kg('a',self.base/'text');before=len(list(Path('/dev/fd').iterdir()))
        for _ in range(8):shutil.rmtree(self.snapshot())
        self.assertEqual(len(list(Path('/dev/fd').iterdir())),before)

    def test_owned_stage_parent_alias_fails_before_any_write(self):
        alias=self.base/'alias';alias.symlink_to(self.parent,target_is_directory=True)
        with self.assertRaises(m.AssetSnapshotError):m.create_registered_asset_snapshot(self.db,alias,policy())
        self.assertEqual(list(self.parent.iterdir()),[])

    def test_original_hsc1_registration_is_not_copied_again_through_another_registry(self):
        p=self.kg('a',self.base/'text');self.c.execute('INSERT INTO voice_input VALUES (?)',(str(p),));self.c.commit()
        self.failure('MANAGED_MEDIA_OWNED_BY_LIFECYCLE')

    def test_failed_staging_cleanup_retains_explicit_original_ownership(self):
        p=self.kg('a',self.base/'text');p.unlink()
        original=shutil.rmtree
        def fail_stage(path,*args,**kwargs):
            if Path(path).name.startswith('registered-assets-'):raise OSError('synthetic cleanup failure')
            return original(path,*args,**kwargs)
        with patch.object(shutil,'rmtree',fail_stage):
            with self.assertRaisesRegex(m.AssetSnapshotError,'^STAGE_CLEANUP_UNCONFIRMED$') as error:self.snapshot()
        self.assertEqual(error.exception.owned_stage.parent,self.parent)
        self.assertTrue(error.exception.owned_stage.is_dir());self.assertFalse((error.exception.owned_stage/'manifest.json').exists())
        original(error.exception.owned_stage)

    def test_payload_precheck_never_reads_or_copies_second_registered_file(self):
        first=self.kg('a',self.base/'first',content=b'A'*60000)
        second=self.kg('b',self.base/'second',content=b'B'*60000)
        self.assertLess(self.db.stat().st_size,100000)
        copies=[];reads=[];original_copy=m.HeldFile.copy;original_chunks=m.HeldFile.chunks
        def copy(file,target):
            if file.root in {first.parent,second.parent}:copies.append(file.root)
            return original_copy(file,target)
        def chunks(file):
            if file.root==second.parent:reads.append(file.parts[-1])
            yield from original_chunks(file)
        with patch.object(m.HeldFile,'copy',copy),patch.object(m.HeldFile,'chunks',chunks):
            self.failure('ASSET_TOTAL_BOUND',policy(max_total_bytes=100000,max_file_bytes=80000))
        self.assertEqual(copies,[first.parent]);self.assertEqual(reads,[])

    def test_sqlite_payload_remaining_rejected_before_target_creation(self):
        self.kg('a',self.base/'first',content=b'A'*60000)
        db,_=self.sqlite('second-metadata/source.db')
        with sqlite3.connect(db) as c:c.execute('INSERT INTO public_rows VALUES (zeroblob(50000))');c.commit()
        self.assertGreater(db.stat().st_size,40000);self.assertLess(db.stat().st_size,80000)
        self.kg('b',self.base/'second',metadata=str(db))
        destinations=[];original=sqlite3.connect
        def connect(path,*args,**kwargs):
            if '/registered/' in str(path) and not str(path).startswith('file:'):destinations.append(str(path))
            return original(path,*args,**kwargs)
        with patch.object(sqlite3,'connect',connect):
            self.failure('ASSET_TOTAL_BOUND',policy(max_total_bytes=100000,max_file_bytes=80000))
        self.assertEqual(destinations,[])

    def test_wal_merged_page_bound_rejected_before_target_backup_creation(self):
        db=self.base/'wal-merge.db';writer=sqlite3.connect(db);self.extra.append(writer)
        writer.execute('CREATE TABLE committed(value BLOB)');writer.execute('INSERT INTO committed VALUES (zeroblob(110000))');writer.commit()
        writer.execute('PRAGMA journal_mode=WAL');writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('INSERT INTO committed VALUES (zeroblob(110000))');writer.commit()
        self.assertLess(db.stat().st_size,200000);self.assertLess(Path(str(db)+'-wal').stat().st_size,200000)
        pages,size=writer.execute('PRAGMA page_count').fetchone()[0],writer.execute('PRAGMA page_size').fetchone()[0]
        self.assertGreater(pages*size,200000)
        self.kg('a',self.base/'text',metadata=str(db))
        destinations=[];original=sqlite3.connect
        def connect(path,*args,**kwargs):
            if '/registered/' in str(path) and not str(path).startswith('file:'):destinations.append(str(path))
            return original(path,*args,**kwargs)
        with patch.object(sqlite3,'connect',connect):
            self.failure('ASSET_FILE_BOUND_OR_ALIAS',policy(max_total_bytes=1000000,max_file_bytes=200000))
        self.assertEqual(destinations,[])

    def test_enospc_after_partial_stage_write_preserves_original_db_and_source(self):
        source=self.kg('a',self.base/'text',content=b'unchanged original bytes')
        before_db=self.db.read_bytes();before_source=source.read_bytes();original=Path.open;triggered=[]
        class FullDisk:
            def __init__(self,raw):self.raw=raw
            def __enter__(self):return self
            def __exit__(self,*args):self.raw.close()
            def write(self,value):
                self.raw.write(value[:8]);triggered.append(True);raise OSError(errno.ENOSPC,'synthetic stage disk full')
        def opened(path,*args,**kwargs):
            raw=original(path,*args,**kwargs)
            if args and args[0]=='xb' and '/registered/' in str(path):return FullDisk(raw)
            return raw
        with patch.object(Path,'open',opened):self.failure('REGISTERED_ASSET_SNAPSHOT_FAILED')
        self.assertEqual(triggered,[True]);self.assertEqual(self.db.read_bytes(),before_db);self.assertEqual(source.read_bytes(),before_source)

    def test_initial_mkdtemp_enospc_is_finite_and_never_deletes_parent_content(self):
        sentinel=self.parent/'original-owned-parent-file';sentinel.write_bytes(b'preserve original parent')
        before_db=self.db.read_bytes();removed=[]
        def unexpected_remove(*args,**kwargs):removed.append(args[0]);raise AssertionError('non-owned cleanup')
        with patch.object(tempfile,'mkdtemp',side_effect=OSError(errno.ENOSPC,'synthetic mkdtemp full')),patch.object(shutil,'rmtree',unexpected_remove):
            with self.assertRaisesRegex(m.AssetSnapshotError,'^REGISTERED_ASSET_SNAPSHOT_FAILED$') as error:self.snapshot()
        self.assertFalse(hasattr(error.exception,'owned_stage'));self.assertEqual(removed,[])
        self.assertEqual(list(self.parent.iterdir()),[sentinel]);self.assertEqual(sentinel.read_bytes(),b'preserve original parent');self.assertEqual(self.db.read_bytes(),before_db)

    def test_initial_scratch_mkdir_enospc_cleans_only_created_owned_stage(self):
        sentinel=self.parent/'original-owned-parent-file';sentinel.write_bytes(b'preserve original parent')
        before_db=self.db.read_bytes();created=[];original=Path.mkdir
        def mkdir(path,*args,**kwargs):
            if path.name=='.working' and path.parent.parent==self.parent:
                created.append(path.parent);self.assertTrue(path.parent.is_dir());raise OSError(errno.ENOSPC,'synthetic scratch full')
            return original(path,*args,**kwargs)
        with patch.object(Path,'mkdir',mkdir):
            with self.assertRaisesRegex(m.AssetSnapshotError,'^REGISTERED_ASSET_SNAPSHOT_FAILED$') as error:self.snapshot()
        self.assertEqual(len(created),1);self.assertFalse(created[0].exists());self.assertFalse(hasattr(error.exception,'owned_stage'))
        self.assertEqual(list(self.parent.iterdir()),[sentinel]);self.assertEqual(sentinel.read_bytes(),b'preserve original parent');self.assertEqual(self.db.read_bytes(),before_db)

    def test_initial_scratch_failure_cleanup_unconfirmed_retains_owned_responsibility(self):
        sentinel=self.parent/'original-owned-parent-file';sentinel.write_bytes(b'preserve original parent')
        before_db=self.db.read_bytes();created=[];attempted=[];original_mkdir=Path.mkdir;original_remove=shutil.rmtree
        def mkdir(path,*args,**kwargs):
            if path.name=='.working' and path.parent.parent==self.parent:
                created.append(path.parent);raise OSError(errno.ENOSPC,'synthetic scratch full')
            return original_mkdir(path,*args,**kwargs)
        def remove(path,*args,**kwargs):
            attempted.append(Path(path));raise OSError(errno.EACCES,'synthetic cleanup denied')
        with patch.object(Path,'mkdir',mkdir),patch.object(shutil,'rmtree',remove):
            with self.assertRaisesRegex(m.AssetSnapshotError,'^STAGE_CLEANUP_UNCONFIRMED$') as error:self.snapshot()
        self.assertEqual(len(created),1);self.assertEqual(attempted,created);self.assertEqual(error.exception.owned_stage,created[0])
        self.assertTrue(created[0].is_dir());self.assertEqual(list(created[0].iterdir()),[]);self.assertFalse((created[0]/'manifest.json').exists())
        self.assertEqual(set(self.parent.iterdir()),{sentinel,created[0]});self.assertEqual(sentinel.read_bytes(),b'preserve original parent');self.assertEqual(self.db.read_bytes(),before_db)
        original_remove(error.exception.owned_stage)


    @unittest.skipUnless(sys.platform == 'win32', 'Windows SQLite destination spelling; real Windows control is recorded separately')
    def test_windows_long_owned_snapshot_registry_and_metadata_destinations(self):
        metadata,_=self.sqlite('public-metadata/original.db')
        self.kg('long-path-book',self.base/'public-transcript',metadata=str(metadata))
        self.c.close()
        before_main=sha(self.db.read_bytes());before_metadata=sha(metadata.read_bytes())
        owned=self.base/('owned-'+('a'*75))/('stage-'+('b'*75))
        m.io_path(owned).mkdir(parents=True)
        try:
            stage=m.create_registered_asset_snapshot(self.db,owned,policy())
            receipt=m.verify_registered_asset_snapshot(stage,policy())
            manifest=json.loads(m.io_path(stage/'manifest.json').read_bytes())
            members=[row for row in manifest['assets'] if row['role']=='metadata']
            self.assertEqual(len(members),1)
            target=stage/members[0]['logical_name']
            self.assertGreater(len(str(target)),260)
            self.assertEqual(members[0]['storage'],'sqlite_backup')
            with m._open_readonly(target) as copied:
                self.assertEqual(copied.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(copied.execute('SELECT value FROM public_rows').fetchone()[0],'original')
            self.assertEqual(receipt['files'],2)
            self.assertEqual(sha(self.db.read_bytes()),before_main)
            self.assertEqual(sha(metadata.read_bytes()),before_metadata)
            with m.HeldFile(target.parent,target.name,policy()) as held:
                self.assertGreater(held.verified_length(),0)
        finally:
            shutil.rmtree(m.io_path(owned))


if __name__=='__main__':unittest.main(verbosity=2)
