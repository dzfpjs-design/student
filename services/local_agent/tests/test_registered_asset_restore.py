"""Fixed-registry restore preparation with synthetic disconnected source roots.

The fixture provides the caller-authenticated contract; these tests do not claim
AEAD integration, Main DB publication, Windows durability or user restore.
"""
import copy
import dataclasses
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from unittest.mock import patch

import pytest
from home_study_agent import registered_asset_snapshot as snapshot
from home_study_agent import registered_asset_restore as restore


@pytest.fixture
def family(tmp_path):
    src = tmp_path/'registered-sources'; src.mkdir()
    target = tmp_path/'target'; target.mkdir(mode=0o700)
    (target/'main.db').write_bytes(b'original live DB sentinel')
    (target/'media').mkdir(); (target/'media'/'original').write_bytes(b'original media sentinel')
    (target/'registered-generation-old').mkdir(); (target/'registered-generation-old'/'original').write_bytes(b'old generation')
    database = tmp_path/'cold.db'
    with sqlite3.connect(database) as c:
        c.executescript('''
        CREATE TABLE curriculum_corpus(corpus_id TEXT PRIMARY KEY,source_database_path TEXT,transcript_root_path TEXT,video_root_path TEXT,source_hash TEXT);
        CREATE TABLE curriculum_resource(row_id INTEGER PRIMARY KEY,corpus_id TEXT,transcript_relative_path TEXT,video_relative_path TEXT,local_video_available INTEGER);
        CREATE TABLE kg_corpus(corpus_id TEXT PRIMARY KEY,source_kind TEXT,transcript_root TEXT,video_root TEXT,metadata_db_path TEXT,version_label TEXT,pdf_root TEXT,image_root TEXT);
        CREATE TABLE kg_resource(resource_id TEXT PRIMARY KEY,corpus_id TEXT,transcript_relative_path TEXT,video_relative_path TEXT,transcript_sha256 TEXT,video_sha256 TEXT);
        CREATE TABLE kg_document_page(page_id TEXT PRIMARY KEY,resource_id TEXT,image_relative_path TEXT,image_sha256 TEXT);
        CREATE TABLE curriculum_standard_source(source_id TEXT PRIMARY KEY,source_key TEXT,local_root_path TEXT,pdf_filename TEXT,pages_filename TEXT,pdf_sha256 TEXT,text_sha256 TEXT);
        CREATE TABLE voice_input(local_path TEXT);CREATE TABLE capture_artifact(local_path TEXT);CREATE TABLE kg_parent_textbook_ocr_artifact(raw_image_path TEXT);
        ''')
    policy = snapshot.Policy(1000,64,1024,1024*1024,64*1024*1024,32*1024*1024)
    owned = tmp_path/'backup-owned'; owned.mkdir(mode=0o700)

    def file(relative, data=b'synthetic local source'):
        p = src/relative; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(data); return p

    def categories():
        text = file('curriculum/text/projected.txt',b'course text')
        file('curriculum/video/projected.mp4',b'course movie')
        file('curriculum/text/pending.txt',b'not imported course text')
        db = src/'curriculum/source.db'
        with sqlite3.connect(db) as c:
            c.executescript("CREATE TABLE resources(resource_id);INSERT INTO resources VALUES ('projected'),('pending'),('inline-only');")
        kgtext = file('kg/text/projected.txt',b'KG text')
        kgvideo = file('kg/video/projected.mp4',b'KG movie')
        image = file('kg/images/page.png',b'original image')
        file('kg/images/pending.HEIC',b'pending image')
        file('kg/text/pending.txt',b'pending KG text')
        pdf = file('kg/pdf/book.pdf',b'%PDF original book')
        metadata = src/'kg/metadata.db'
        with sqlite3.connect(metadata) as c:
            c.executescript("CREATE TABLE textbook_book(book_id,relative_pdf_path);INSERT INTO textbook_book VALUES ('book','book.pdf');CREATE TABLE textbook_page(page_id,ocr_text_relative_path);INSERT INTO textbook_page VALUES ('p','projected.txt');")
        with sqlite3.connect(metadata) as c:
            c.execute('ALTER TABLE textbook_book ADD COLUMN fingerprint TEXT')
            c.execute('UPDATE textbook_book SET fingerprint=?', (f'size:{pdf.stat().st_size}:mtime_ns:{pdf.stat().st_mtime_ns}',))
        std = file('standard/book.pdf',b'%PDF standard same-role data')
        file('standard/pages.jsonl',b'{"text":"synthetic"}\n')
        file('standard/ocr.txt',b'standard text')
        file('standard/manifest.json',snapshot.canonical({'format':'hsc_official_curriculum_sources_v1','sources':[
            {'source_key':'math','pdf_filename':'book.pdf','pages_filename':'pages.jsonl',
             'content_filename':'book.pdf','ocr_text_filename':'ocr.txt','ocr_pages_filename':'pages.jsonl'}]}))
        with sqlite3.connect(database) as c:
            c.execute('INSERT INTO curriculum_corpus VALUES (?,?,?,?,?)',('course',str(db),str(text.parent),str(src/'curriculum/video'),'source-v1'))
            c.execute('INSERT INTO curriculum_resource VALUES (1,?,?,?,1)',('course','projected.txt','projected.mp4'))
            c.execute('INSERT INTO kg_corpus VALUES (?,?,?,?,?,?,?,?)',('kg','local_scanned_document',str(kgtext.parent),str(kgvideo.parent),str(metadata),'kg-v1',str(pdf.parent),str(image.parent)))
            c.execute('INSERT INTO kg_resource VALUES (?,?,?,?,?,?)',('r','kg','projected.txt','projected.mp4',snapshot.digest(kgtext.read_bytes()),snapshot.digest(kgvideo.read_bytes())))
            c.execute('INSERT INTO kg_document_page VALUES (?,?,?,?)',('page','r','page.png',snapshot.digest(image.read_bytes())))
            c.execute("INSERT INTO kg_corpus VALUES ('inline','inline','local://family-textbook-ocr-pages',NULL,NULL,'v1',NULL,NULL)")
            c.execute('INSERT INTO curriculum_standard_source VALUES (?,?,?,?,?,?,?)',('std','math',str(std.parent),'book.pdf','pages.jsonl',snapshot.digest(std.read_bytes()),snapshot.digest(b'standard text')))

    def backup():
        stage = snapshot.create_registered_asset_snapshot(database,owned,policy)
        manifest = json.loads((stage/'manifest.json').read_bytes())
        summary = {'coverage':'encrypted_registered_v1' if manifest['assets'] else 'zero_registered_payload',
                   'asset_count':len(manifest['assets']),'total_bytes':sum(a['bytes']for a in manifest['assets']),
                   'manifest_sha256':snapshot.digest((stage/'manifest.json').read_bytes()),
                   'coverage_sha256':snapshot.digest(snapshot.canonical(manifest['coverage'])),
                   'capsule':{'sha256':'0'*64,'size_bytes':1} if manifest['assets'] else None}
        return stage, manifest, summary

    def prepare(stage,contract):
        return restore.prepare_registered_asset_restore(database,stage,target,
            expected_database_sha256=snapshot.digest(database.read_bytes()),registered_contract=contract,policy=policy)

    def unchanged():
        assert (target/'main.db').read_bytes() == b'original live DB sentinel'
        assert (target/'media'/'original').read_bytes() == b'original media sentinel'
        assert (target/'registered-generation-old'/'original').read_bytes() == b'old generation'

    class Family: pass
    f=Family();f.src=src;f.target=target;f.db=database;f.policy=policy;f.owned=owned
    f.file=file;f.categories=categories;f.backup=backup;f.prepare=prepare;f.unchanged=unchanged
    return f


def test_all_three_families_disconnected_originals_and_standard_roles_merge(family):
    f=family;f.categories();stage,manifest,contract=f.backup();original=f.db.read_bytes()
    f.src.rename(f.src.with_name('offline-old-roots'))
    result=f.prepare(stage,contract)
    assert result.status=='REGISTERED_ASSETS_PUBLISHED_PREPARED_DB_ONLY'
    assert result.source_database_sha256==snapshot.digest(original)
    assert f.db.read_bytes()==original
    f.unchanged()
    with sqlite3.connect(result.database_path) as c:
        c.row_factory=sqlite3.Row
        course=c.execute('SELECT * FROM curriculum_corpus').fetchone()
        assert (Path(course['transcript_root_path'])/'pending.txt').read_bytes()==b'not imported course text'
        assert (Path(course['video_root_path'])/'projected.mp4').read_bytes()==b'course movie'
        with sqlite3.connect(course['source_database_path']) as source:
            assert source.execute('SELECT COUNT(*) FROM resources').fetchone()[0]==3
        kg=c.execute("SELECT * FROM kg_corpus WHERE corpus_id='kg'").fetchone()
        assert (Path(kg['image_root'])/'pending.HEIC').read_bytes()==b'pending image'
        assert (Path(kg['pdf_root'])/'book.pdf').read_bytes()==b'%PDF original book'
        with sqlite3.connect(kg['metadata_db_path']) as metadata:
            assert metadata.execute('SELECT COUNT(*) FROM textbook_book').fetchone()[0]==1
        assert c.execute("SELECT transcript_root FROM kg_corpus WHERE corpus_id='inline'").fetchone()[0]=='local://family-textbook-ocr-pages'
        std=c.execute('SELECT * FROM curriculum_standard_source').fetchone()
        root=Path(std['local_root_path'])
        assert (root/'book.pdf').read_bytes()==b'%PDF standard same-role data'
        selected=json.loads((root/'manifest.json').read_bytes())['sources'][0]
        assert (root/selected['content_filename']).read_bytes()==(root/selected['pdf_filename']).read_bytes()
        assert sorted(p.name for p in root.iterdir())==['book.pdf','manifest.json','ocr.txt','pages.jsonl']
    assert not any(p.name.startswith('.registered-restore-')for p in f.target.iterdir())
    assert result.asset_count==len(manifest['assets'])
    # No two redundant logical roles become two physical copies of one path.
    physical=[p for p in result.generation.rglob('*')if p.is_file()]
    assert len(physical)<len(manifest['assets'])+1


def test_zero_payload_keeps_empty_roots_and_requires_bound_coverage(family):
    f=family
    text=f.src/'empty-text';video=f.src/'empty-video';images=f.src/'empty-images'
    for p in (text,video,images):p.mkdir()
    with sqlite3.connect(f.db)as c:
        c.execute('INSERT INTO kg_corpus VALUES (?,?,?,?,NULL,?,NULL,?)',('empty','local_scanned_document',str(text),str(video),'v1',str(images)))
    stage,manifest,contract=f.backup();assert manifest['assets']==[]
    shutil.rmtree(f.src)
    result=f.prepare(None,contract)
    with sqlite3.connect(result.database_path)as c:
        values=c.execute('SELECT transcript_root,video_root,image_root FROM kg_corpus').fetchone()
    assert all(Path(p).is_dir()and not any(Path(p).iterdir())for p in values)
    assert all(result.generation in Path(p).parents for p in values)
    f.unchanged()


def mutated(f,stage,manifest,contract,change):
    m=copy.deepcopy(manifest);data={a['logical_name']:(stage/a['logical_name']).read_bytes()for a in m['assets']}
    change(m,data)
    new=f.owned/('mutated-'+str(len(list(f.owned.iterdir()))));new.mkdir()
    for a in m['assets']:
        p=new/a['logical_name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data[a['logical_name']])
    raw=snapshot.canonical(m);(new/'manifest.json').write_bytes(raw)
    bound={**contract,'asset_count':len(m['assets']),'total_bytes':sum(a['bytes']for a in m['assets']),
           'manifest_sha256':snapshot.digest(raw),'coverage_sha256':snapshot.digest(snapshot.canonical(m['coverage']))}
    return new,bound


@pytest.mark.parametrize('mutation,code',[
    ('missing','REGISTERED_RESTORE_MISSING_ASSET'),('extra','REGISTERED_RESTORE_EXTRA_ASSET'),
    ('version','REGISTERED_RESTORE_VERSION_MISMATCH'),('coverage','REGISTERED_RESTORE_COVERAGE_MISMATCH'),
    ('conflicting-standard-role','REGISTERED_RESTORE_PATH_COLLISION')])
def test_authenticated_shape_cannot_replace_original_registry_authority(family,mutation,code):
    f=family;f.categories();stage,manifest,contract=f.backup()
    def change(m,data):
        if mutation=='missing':
            a=next(a for a in m['assets']if a['kind']=='curriculum'and a['relative_path']=='projected.txt')
            m['assets'].remove(a)
        elif mutation=='extra':
            a=copy.deepcopy(next(a for a in m['assets']if a['kind']=='kg'and a['role']=='transcript'))
            a['role']='unknown';a['logical_name']=a['logical_name'].replace('/transcript/','/unknown/')
            m['assets'].append(a);data[a['logical_name']]=b'forged';a['bytes']=6;a['sha256']=snapshot.digest(b'forged')
        elif mutation=='version':m['assets'][0]['version_sha256s']=['f'*64]
        elif mutation=='coverage':
            row=next(a for a in m['coverage']if a['kind']=='kg'and a['storage']=='registered_local')
            row['registry_columns']=['transcript_root']
        else:
            a=next(a for a in m['assets']if a['kind']=='standard'and a['role']=='content')
            data[a['logical_name']]=b'conflicting role';a['bytes']=16;a['sha256']=snapshot.digest(b'conflicting role')
    changed,bound=mutated(f,stage,manifest,contract,change)
    with pytest.raises(restore.RegisteredAssetRestoreError,match='^'+code+'$'):f.prepare(changed,bound)
    f.unchanged()
    assert sorted(p.name for p in f.target.iterdir())==['main.db','media','registered-generation-old']


@pytest.mark.parametrize('field',['database','manifest','coverage'])
def test_original_binding_tamper_rejected(family,field):
    f=family;f.categories();stage,manifest,contract=f.backup()
    if field=='database':
        expected='f'*64
    else:
        expected=snapshot.digest(f.db.read_bytes());contract[{'manifest':'manifest_sha256','coverage':'coverage_sha256'}[field]]='f'*64
    with pytest.raises(restore.RegisteredAssetRestoreError):
        restore.prepare_registered_asset_restore(f.db,stage,f.target,expected_database_sha256=expected,registered_contract=contract,policy=f.policy)
    f.unchanged()
    assert not any(p.name.startswith('.registered-restore-')or(p.name.startswith('registered-generation-')and p.name!='registered-generation-old')for p in f.target.iterdir())


def test_symlink_input_or_target_rejected_without_following(family):
    f=family;f.categories();stage,manifest,contract=f.backup()
    alias=f.target.with_name('target-alias');alias.symlink_to(f.target,target_is_directory=True)
    with pytest.raises(restore.RegisteredAssetRestoreError):
        restore.prepare_registered_asset_restore(f.db,stage,alias,expected_database_sha256=snapshot.digest(f.db.read_bytes()),registered_contract=contract,policy=f.policy)
    item=manifest['assets'][0];path=stage/item['logical_name'];original=path.read_bytes();path.unlink()
    outside=f.owned/'outside';outside.write_bytes(original);path.symlink_to(outside)
    with pytest.raises((restore.RegisteredAssetRestoreError,snapshot.AssetSnapshotError)):f.prepare(stage,contract)
    f.unchanged();assert outside.read_bytes()==original


@pytest.mark.parametrize('fault',['space','write','flush','rename'])
def test_failure_never_publishes_db_or_erases_prior_generation(family,fault):
    f=family;f.categories();stage,manifest,contract=f.backup()
    if fault=='space':
        hook=patch.object(restore._OwnedGeneration,'space',side_effect=restore.RegisteredAssetRestoreError('REGISTERED_RESTORE_DISK_REQUIRED'))
    elif fault=='write':
        original=restore._OwnedGeneration.copy
        def partial(g,held,relative,expected):
            original(g,held,relative,expected)
            raise OSError(errno.ENOSPC,'synthetic partial phase failure')
        hook=patch.object(restore._OwnedGeneration,'copy',partial)
    elif fault=='flush':
        hook=patch.object(restore._OwnedGeneration,'flush',side_effect=OSError(errno.EIO,'synthetic directory fsync failure'))
    else:hook=patch.object(restore.os,'rename',side_effect=OSError(errno.EXDEV,'synthetic wrong volume'))
    with hook,pytest.raises(restore.RegisteredAssetRestoreError):f.prepare(stage,contract)
    f.unchanged();assert sorted(p.name for p in f.target.iterdir())==['main.db','media','registered-generation-old']


def test_cleanup_failure_retains_private_responsibility(family):
    f=family;f.categories();stage,manifest,contract=f.backup()
    original=restore.shutil.rmtree
    with patch.object(restore._OwnedGeneration,'space',side_effect=OSError(errno.ENOSPC,'synthetic')), \
         patch.object(restore,'_remove_owned_tree',side_effect=OSError(errno.EACCES,'synthetic cleanup debt')):
        with pytest.raises(restore.RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_CLEANUP_UNCONFIRMED$')as e:f.prepare(stage,contract)
    assert e.value.owned_generation.parent==f.target and e.value.owned_generation.is_dir()
    f.unchanged();original(e.value.owned_generation)


def test_parent_fsync_failure_after_rename_retains_orphan_not_success(family):
    f=family;f.categories();stage,manifest,contract=f.backup()
    original=restore.os.fsync;renamed=False;rename=restore.os.rename
    def move(*a,**k):
        nonlocal renamed
        result=rename(*a,**k)
        if str(a[0]).startswith('.registered-restore-'):renamed=True
        return result
    def sync(fd):
        if renamed:raise OSError(errno.EIO,'synthetic post-rename fsync failure')
        return original(fd)
    with patch.object(restore.os,'rename',move),patch.object(restore.os,'fsync',sync):
        with pytest.raises(restore.RegisteredAssetRestoreError)as e:f.prepare(stage,contract)
    assert e.value.owned_generation.is_dir()
    assert e.value.owned_generation.name.startswith('registered-generation-')
    f.unchanged()


def test_zero_payload_cannot_bypass_registry_coverage(family):
    f=family;text=f.src/'empty';text.mkdir()
    with sqlite3.connect(f.db)as c:
        c.execute('INSERT INTO kg_corpus VALUES (?,?,?,NULL,NULL,?,NULL,NULL)',('empty','seyk',str(text),'v1'))
    _,_,contract=f.backup();contract['coverage_sha256']='f'*64
    with pytest.raises(restore.RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_COVERAGE_MISMATCH$'):f.prepare(None,contract)
    f.unchanged();assert sorted(p.name for p in f.target.iterdir())==['main.db','media','registered-generation-old']


def test_many_directories_use_bounded_live_handles_and_close_them(family):
    f=family;root=f.src/'many';root.mkdir()
    with sqlite3.connect(f.db)as c:
        c.execute('INSERT INTO kg_corpus VALUES (?,?,?,NULL,NULL,?,NULL,NULL)',('many','seyk',str(root),'v1'))
    for i in range(180):f.file('many/d'+str(i)+'/pending.txt',b'synthetic one file')
    stage,manifest,contract=f.backup();baseline=len(os.listdir('/dev/fd'));peaks=[]
    original=restore._OwnedGeneration.copy
    def copy_one(g,held,relative,expected):
        original(g,held,relative,expected)
        peaks.append(len(os.listdir('/dev/fd')))
        assert len(g.dirs)==1  # Keep only the work root between operations.
    with patch.object(restore._OwnedGeneration,'copy',copy_one):result=f.prepare(stage,contract)
    ceiling=baseline+len(f.db.parts)+len(f.target.parts)+max(len((stage/a['logical_name']).parts)for a in manifest['assets'])+8
    assert len(peaks)==181 and max(peaks)<=ceiling
    assert len(set(peaks[1:]))==1  # Directory count never accumulates handles.
    assert len(os.listdir('/dev/fd'))==baseline
    assert len([p for p in result.generation.rglob('*.txt')])==180
    f.unchanged()


def test_target_root_replacement_rejects_without_writing_outside(family):
    f=family;f.categories();stage,_,contract=f.backup()
    original=restore._OwnedGeneration.copy;changed=False
    outside=f.target.with_name('outside-target');outside.mkdir();marker=outside/'marker';marker.write_bytes(b'do not touch')
    moved=f.target.with_name('retired-owned-target')
    def copy_then_replace(g,held,relative,expected):
        nonlocal changed
        original(g,held,relative,expected)
        if not changed:
            changed=True;f.target.rename(moved);f.target.symlink_to(outside,target_is_directory=True)
    with patch.object(restore._OwnedGeneration,'copy',copy_then_replace):
        with pytest.raises(restore.RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_TARGET_ALIAS$'):f.prepare(stage,contract)
    assert marker.read_bytes()==b'do not touch'and sorted(p.name for p in outside.iterdir())==['marker']
    assert sorted(p.name for p in moved.iterdir())==['main.db','media','registered-generation-old']


def test_duplicate_manifest_fields_and_invalid_policy_are_finite(family):
    f=family;f.categories();stage,_,contract=f.backup()
    raw=(stage/'manifest.json').read_bytes();raw=b'{"schema":1,'+raw[1:]
    (stage/'manifest.json').write_bytes(raw);contract['manifest_sha256']=snapshot.digest(raw)
    with pytest.raises(restore.RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_DUPLICATE_FIELD$'):f.prepare(stage,contract)
    with pytest.raises(restore.RegisteredAssetRestoreError)as e:
        restore.prepare_registered_asset_restore(f.db,stage,f.target,expected_database_sha256=snapshot.digest(f.db.read_bytes()),
            registered_contract=contract,policy=dataclasses.replace(f.policy,max_files=0))
    assert str(e.value)=='REGISTERED_RESTORE_PREPARATION_FAILED'
    f.unchanged()


def test_actual_partial_write_enospc_closes_fd_and_preserves_inputs(family):
    f=family;f.categories();stage,_,contract=f.backup();before=f.db.read_bytes()
    baseline=len(os.listdir('/dev/fd'));original=restore.os.fdopen;triggered=[]
    class FullDisk:
        def __init__(self,raw):self.raw=raw
        def __enter__(self):return self
        def __exit__(self,*args):self.raw.close()
        def write(self,value):
            self.raw.write(value[:8]);triggered.append(True)
            raise OSError(errno.ENOSPC,'synthetic partial target write')
    def opened(fd,*args,**kwargs):
        raw=original(fd,*args,**kwargs)
        return FullDisk(raw)if args and args[0]=='wb'else raw
    with patch.object(restore.os,'fdopen',opened):
        with pytest.raises(restore.RegisteredAssetRestoreError,match='^REGISTERED_RESTORE_PREPARATION_FAILED$'):f.prepare(stage,contract)
    assert triggered==[True]and f.db.read_bytes()==before
    assert len(os.listdir('/dev/fd'))==baseline
    f.unchanged();assert sorted(p.name for p in f.target.iterdir())==['main.db','media','registered-generation-old']
