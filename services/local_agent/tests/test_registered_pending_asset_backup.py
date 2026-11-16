"""A stopped import's registered source material is still family backup data."""
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor
from home_study_agent.registered_asset_snapshot import Policy, create_registered_asset_snapshot


@pytest.mark.parametrize('source_kind', ['seyk', 'bilibili'])
def test_registered_sources_survive_backup_before_any_import_runs(tmp_path, source_kind):
    # Only register the original source. Never start/resume ingestion, OCR or a model.
    text = tmp_path / 'selected-text'
    video = tmp_path / 'selected-video'
    (text / 'pending').mkdir(parents=True)
    (video / 'pending').mkdir(parents=True)
    transcript = text / 'pending' / 'not-imported.txt'
    movie = video / 'pending' / 'not-imported.mp4'
    transcript.write_bytes(b'synthetic registered but not yet processed transcript')
    movie.write_bytes(b'synthetic registered but not yet processed video')
    outside = tmp_path / 'unregistered.txt'
    outside.write_bytes(b'outside the selected corpus; never include')
    database = Database(tmp_path / 'study.db')
    database.initialize()
    LocalCorpusIngestor(database).register(CorpusSpec(
        'pending-source', 'Synthetic stopped source', source_kind, text, video,
        'parent-selected-local', 'family-owned-local',
    ))
    with database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM kg_resource').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM kg_ingest_job').fetchone()[0] == 0
    owned = tmp_path / 'owned'
    owned.mkdir(mode=0o700)
    total = max(64 * 1024 * 1024, database.path.stat().st_size * 4)
    stage = create_registered_asset_snapshot(
        database.path, owned, Policy(1000, 64, 1024, 1024 * 1024, total, total),
    )
    manifest = json.loads((stage / 'manifest.json').read_text())
    saved = {(item['role'], item['relative_path']): (stage / item['logical_name']).read_bytes()
             for item in manifest['assets']}
    assert saved == {
        ('transcript', 'pending/not-imported.txt'): transcript.read_bytes(),
        ('video', 'pending/not-imported.mp4'): movie.read_bytes(),
    }
    assert outside.read_bytes() == b'outside the selected corpus; never include'
    with database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM kg_resource').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM kg_ingest_job').fetchone()[0] == 0

# The controls below exercise the provider directly with synthetic fixed registries;
# the first two tests above retain the actual original register() reproduction.
import dataclasses
import errno
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from home_study_agent import registered_asset_snapshot as m


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def inventory(tmp_path):
    database = tmp_path / 'registry.db'
    with sqlite3.connect(database) as c:
        c.executescript('''
        CREATE TABLE kg_corpus(corpus_id TEXT PRIMARY KEY,source_kind TEXT,transcript_root TEXT,video_root TEXT,metadata_db_path TEXT,version_label TEXT,pdf_root TEXT,image_root TEXT);
        CREATE TABLE kg_resource(resource_id TEXT PRIMARY KEY,corpus_id TEXT,transcript_relative_path TEXT,video_relative_path TEXT,transcript_sha256 TEXT,video_sha256 TEXT);
        CREATE TABLE kg_document_page(page_id TEXT PRIMARY KEY,resource_id TEXT,image_relative_path TEXT,image_sha256 TEXT);
        CREATE TABLE curriculum_corpus(corpus_id TEXT PRIMARY KEY,source_database_path TEXT,transcript_root_path TEXT,video_root_path TEXT,source_hash TEXT);
        CREATE TABLE curriculum_resource(row_id INTEGER PRIMARY KEY,corpus_id TEXT,transcript_relative_path TEXT,video_relative_path TEXT,local_video_available INTEGER);
        CREATE TABLE voice_input(local_path TEXT);
        CREATE TABLE capture_artifact(local_path TEXT);
        CREATE TABLE kg_parent_textbook_ocr_artifact(raw_image_path TEXT);
        ''')
    owned = tmp_path / 'owned'
    owned.mkdir(mode=0o700)
    policy = Policy(100, 64, 1024, 1024*1024, 16*1024*1024, 8*1024*1024)

    def file(relative, data=b'synthetic pending'):
        p = tmp_path / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def kg(kind='seyk', image=False):
        text = tmp_path / 'text'
        video = tmp_path / 'video'
        images = tmp_path / 'images'
        for p in (text, video, images):
            p.mkdir(exist_ok=True)
        with sqlite3.connect(database) as c:
            c.execute('INSERT INTO kg_corpus VALUES (?,?,?,?,?,?,?,?)',
                      ('kg', kind, str(text), str(video), None, 'registered-v1', None, str(images) if image else None))
        return text, video, images

    def snapshot(policy=policy):
        stage = create_registered_asset_snapshot(database, owned, policy)
        manifest = json.loads((stage/'manifest.json').read_bytes())
        return stage, manifest

    def failure(code, policy=policy):
        with pytest.raises(m.AssetSnapshotError, match='^'+code+'$'):
            snapshot(policy)
        assert list(owned.iterdir()) == []

    return SimpleNamespace(base=tmp_path, db=database, owned=owned, policy=policy,
                           file=file, kg=kg, snapshot=snapshot, failure=failure)


def assets(manifest):
    return {(a['role'], a['relative_path']): a for a in manifest['assets']}


@pytest.mark.parametrize('kind,hidden_video', [('seyk', True), ('bilibili', False)])
def test_original_consumer_types_and_hidden_parts(inventory, kind, hidden_video):
    e = inventory
    e.kg(kind)
    for rel in ['text/a.txt', 'text/.hidden/b.txt', 'text/.dot.txt',
                'video/a.mp4', 'video/.hidden/b.mp4', 'video/.dot.mp4',
                'text/ignored.pdf', 'video/ignored.mov', 'outside/private.txt']:
        e.file(rel)
    stage, manifest = e.snapshot()
    names = set(assets(manifest))
    expected = {('transcript', 'a.txt'), ('transcript', '.hidden/b.txt'),
                ('transcript', '.dot.txt'), ('video', 'a.mp4')}
    if hidden_video:
        expected |= {('video', '.hidden/b.mp4'), ('video', '.dot.mp4')}
    assert names == expected
    assert all(a['version_sha256s'] == [sha(m.canonical('registered-v1'))] for a in manifest['assets'])
    assert b'outside/private' not in (stage/'manifest.json').read_bytes()


def test_existing_projection_not_duplicated_or_given_pending_version(inventory):
    e = inventory
    e.kg()
    text = e.file('text/a.txt')
    video = e.file('video/a.mp4')
    th, vh = sha(text.read_bytes()), sha(video.read_bytes())
    with sqlite3.connect(e.db) as c:
        c.execute('INSERT INTO kg_resource VALUES (?,?,?,?,?,?)', ('r', 'kg', 'a.txt', 'a.mp4', th, vh))
    _, manifest = e.snapshot()
    saved = assets(manifest)
    assert len(saved) == 2
    assert all(a['version_sha256s'] == [sha(m.canonical(['registered-v1', th, vh]))]
               for a in saved.values())
    assert saved['transcript', 'a.txt']['sha256'] == th


@pytest.mark.parametrize('suffix', ['.jpg', '.JPEG', '.png', '.WebP', '.heic'])
def test_pending_images_explicitly_registered_without_ingest(tmp_path, suffix):
    text, images = tmp_path/'text', tmp_path/'images'
    text.mkdir()
    (images/'pending').mkdir(parents=True)
    image = images/'pending'/('scan'+suffix)
    image.write_bytes(b'synthetic pending scan')
    (images/'not-authorized.pdf').write_bytes(b'not a selected image')
    db = Database(tmp_path/'study.db')
    db.initialize()
    LocalCorpusIngestor(db).register(CorpusSpec('scan', 'Synthetic pending scan', 'local_scanned_document',
        text, None, 'parent-selected-local', 'family-owned-local', image_root=images))
    owned = tmp_path/'owned'
    owned.mkdir(mode=0o700)
    stage = create_registered_asset_snapshot(db.path, owned, Policy(100, 64, 1024, 1024*1024, 64*1024*1024, 64*1024*1024))
    saved = assets(json.loads((stage/'manifest.json').read_bytes()))
    assert set(saved) == {('image', 'pending/scan'+suffix)}
    assert (stage/saved['image', 'pending/scan'+suffix]['logical_name']).read_bytes() == image.read_bytes()
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM kg_resource').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM kg_ingest_job').fetchone()[0] == 0


@pytest.mark.parametrize('table', ['voice_input', 'capture_artifact', 'kg_parent_textbook_ocr_artifact'])
def test_pending_does_not_duplicate_original_managed_media(inventory, table):
    e = inventory
    e.kg(image=True)
    image = e.file('images/managed.png')
    column = 'raw_image_path' if table == 'kg_parent_textbook_ocr_artifact' else 'local_path'
    with sqlite3.connect(e.db) as c:
        c.execute('INSERT INTO '+table+'('+column+') VALUES (?)', (str(image),))
    _, manifest = e.snapshot()
    assert manifest['assets'] == []


def curriculum(e, source_sql):
    source = e.base/'source.db'
    with sqlite3.connect(source) as c:
        c.executescript(source_sql)
    for name in ('text', 'video'):
        (e.base/name).mkdir()
    with sqlite3.connect(e.db) as c:
        c.execute('INSERT INTO curriculum_corpus VALUES (?,?,?,?,?)',
                  ('c', str(source), str(e.base/'text'), str(e.base/'video'), 'source-version'))
    return source


def test_curriculum_uses_all_source_ids_but_only_exact_optional_files(inventory):
    e = inventory
    source = curriculum(e, "CREATE TABLE resources(resource_id);INSERT INTO resources VALUES ('pending'),('inline-only'),('nested/allowed');")
    e.file('text/pending.txt')
    e.file('video/pending.mp4')
    e.file('text/nested/allowed.txt')
    e.file('text/unregistered.txt')
    e.file('video/unregistered.mp4')
    before = source.read_bytes()
    stage, manifest = e.snapshot()
    saved = assets(manifest)
    assert set(saved) == {('metadata', 'source.db'), ('transcript', 'pending.txt'),
                          ('video', 'pending.mp4'), ('transcript', 'nested/allowed.txt')}
    assert source.read_bytes() == before
    with sqlite3.connect(stage/saved['metadata', 'source.db']['logical_name']) as c:
        assert c.execute('SELECT COUNT(*) FROM resources').fetchone()[0] == 3


def test_curriculum_legacy_source_without_resources_keeps_original_projection(inventory):
    e = inventory
    curriculum(e, 'CREATE TABLE old_source(value);')
    e.file('text/projected.txt')
    e.file('text/unrelated.txt')
    with sqlite3.connect(e.db) as c:
        c.execute('INSERT INTO curriculum_resource VALUES (1,?,?,NULL,0)', ('c', 'projected.txt'))
    _, manifest = e.snapshot()
    assert set(assets(manifest)) == {('metadata', 'source.db'), ('transcript', 'projected.txt')}


@pytest.mark.parametrize('sql', ['CREATE TABLE resources(other);', 'CREATE VIEW resources AS SELECT 1 AS resource_id;'])
def test_curriculum_present_but_unknown_id_schema_is_rejected(inventory, sql):
    e = inventory
    curriculum(e, sql)
    e.failure('REGISTRY_SCHEMA_REQUIRED')


@pytest.mark.parametrize('kind', ['directory', 'file', 'hardlink'])
def test_tree_alias_rejected_and_stage_cleaned(inventory, kind):
    e = inventory
    text, _, _ = e.kg()
    outside = e.file('outside/a.txt')
    if kind == 'directory':
        (text/'alias').symlink_to(outside.parent, target_is_directory=True)
    elif kind == 'file':
        (text/'a.txt').symlink_to(outside)
    else:
        os.link(outside, text/'a.txt')
    e.failure('ASSET_FILE_BOUND_OR_ALIAS' if kind == 'hardlink' else 'SOURCE_ALIAS_OR_REPLACED')
    assert outside.read_bytes() == b'synthetic pending'


def test_directory_replacement_while_held_is_rejected(inventory):
    e = inventory
    text, _, _ = e.kg()
    e.file('text/nested/a.txt')
    original = m.HeldFile.copy
    replaced = False
    def replace(file, target):
        nonlocal replaced
        value = original(file, target)
        if file.root == text and not replaced:
            replaced = True
            (text/'nested').rename(text/'retired')
            (text/'nested').mkdir()
        return value
    with patch.object(m.HeldFile, 'copy', replace):
        e.failure('SOURCE_ALIAS_OR_REPLACED')
    assert replaced


def test_directory_mutation_while_held_is_rejected(inventory):
    e = inventory
    text, _, _ = e.kg()
    e.file('text/a.txt')
    original = m.HeldFile.copy
    def mutate(file, target):
        value = original(file, target)
        if file.root == text:
            (text/'new.txt').write_bytes(b'changed enumeration')
        return value
    with patch.object(m.HeldFile, 'copy', mutate):
        e.failure('SOURCE_DIRECTORY_CHANGED')


def test_tree_depth_and_count_bounds_prevent_unbounded_discovery(inventory):
    e = inventory
    text, _, _ = e.kg()
    # Depth of the source path remains valid; the nested discovery alone exceeds
    # the complete logical-asset depth. Nonselected paths count too.
    depth = len(e.base.parts)+6
    e.file('text/'+'/'.join('d'+str(i) for i in range(depth))+'/ignored.bin')
    e.failure('ASSET_PATH_BOUND', dataclasses.replace(e.policy, max_depth=depth))


def test_tree_visited_bound_is_streaming_including_unselected_files(inventory):
    e = inventory
    e.kg()
    p = dataclasses.replace(e.policy, max_files=1, max_depth=64)
    for i in range(66):
        e.file('text/ignored-'+str(i)+'.bin')
    e.failure('ASSET_TREE_BOUND', p)


def test_selected_pending_count_bound_cleans_stage(inventory):
    e = inventory
    e.kg()
    e.file('text/a.txt')
    e.file('text/b.txt')
    e.failure('ASSET_COUNT_BOUND', dataclasses.replace(e.policy, max_files=1))


def test_enumeration_resource_failure_has_no_publication_and_closes_fds(inventory):
    e = inventory
    e.kg()
    original = m.os.scandir
    fd_path = '/dev/fd' if Path('/dev/fd').exists() else '/proc/self/fd'
    before = len(os.listdir(fd_path))
    calls = 0
    def fail(path):
        nonlocal calls
        if isinstance(path, int):
            calls += 1
            if calls == 1:
                raise OSError(errno.EMFILE, 'synthetic no handles')
        return original(path)
    with patch.object(m.os, 'scandir', fail), patch.object(m.os, 'supports_fd', m.os.supports_fd | {fail}):
        e.failure('REGISTERED_ASSET_SNAPSHOT_FAILED')
    assert calls >= 1  # Cleanup uses the same original scandir and must still run.
    assert len(os.listdir(fd_path)) == before


def test_pending_budget_preflight_before_copy_keeps_source(inventory):
    e = inventory
    text, _, _ = e.kg()
    payload = e.file('text/pending.txt', b'x'*(4*1024*1024))
    # Enough for the source SQLite snapshot, insufficient for this pending file.
    p = dataclasses.replace(e.policy, max_total_bytes=2*1024*1024, max_file_bytes=2*1024*1024)
    original = m.HeldFile.copy
    def no_payload_read(file, target):
        assert file.root != text
        return original(file, target)
    with patch.object(m.HeldFile, 'copy', no_payload_read):
        e.failure('ASSET_FILE_BOUND_OR_ALIAS', p)
    assert payload.stat().st_size == 4*1024*1024


def test_pending_total_budget_rejects_before_payload_read(inventory):
    e = inventory
    text, _, _ = e.kg()
    first = e.file('text/projected.txt', b'A'*60000)
    pending = e.file('text/pending.txt', b'B'*60000)
    with sqlite3.connect(e.db) as c:
        c.execute('INSERT INTO kg_resource VALUES (?,?,?,NULL,?,NULL)',
                  ('r', 'kg', first.name, sha(first.read_bytes())))
    reads = []
    original = m.HeldFile.chunks
    def chunks(file):
        if file.root == text and file.parts[-1] == pending.name:
            reads.append(True)
        yield from original(file)
    p = dataclasses.replace(e.policy, max_total_bytes=100000, max_file_bytes=80000)
    with patch.object(m.HeldFile, 'chunks', chunks):
        e.failure('ASSET_TOTAL_BOUND', p)
    assert reads == []
    assert first.read_bytes() == b'A'*60000 and pending.read_bytes() == b'B'*60000


def test_curriculum_resources_are_read_from_stabilized_snapshot_only(inventory):
    e = inventory
    source = curriculum(e, "CREATE TABLE resources(resource_id);INSERT INTO resources VALUES ('pending');")
    e.file('text/pending.txt')
    seen = []
    original = m._rows
    def rows(connection, table, columns):
        if table == 'resources':
            actual = Path(connection.execute('PRAGMA database_list').fetchone()[2])
            seen.append(actual)
            assert actual != source and e.owned in actual.parents
            assert actual.name == source.name and 'registered' in actual.parts
        return original(connection, table, columns)
    with patch.object(m, '_rows', rows):
        _, manifest = e.snapshot()
    assert len(seen) == 1
    assert ('transcript', 'pending.txt') in assets(manifest)
