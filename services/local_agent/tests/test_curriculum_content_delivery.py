from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from home_study_agent.knowledge_graph.content_bundle import export_content, import_content, FIELDS
from home_study_agent.knowledge_graph.local_course_context import LocalCourseContextService
from home_study_agent.knowledge_graph.schema import ensure_schema


class FixtureDatabase:
    def __init__(self, path): self.path = path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            with db: yield db


def initialize(path):
    with closing(sqlite3.connect(path)) as db:
        ensure_schema(db, '2026-09-05')
        db.execute("CREATE TABLE child_profile(child_id TEXT PRIMARY KEY,grade TEXT,status TEXT)")
        db.executemany("INSERT INTO child_profile VALUES(?,?,'active')", [('a', '四年级'), ('b', '初三')])
        db.commit()


def seed(path):
    initialize(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO kg_corpus(corpus_id,title,source_kind,transcript_root,video_root,metadata_db_path,source_ref,license_boundary,review_status,version_label,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ('seyk-local-v1','课程','seyk','/private/transcripts','/private/videos','/private/family.db','local:seyk','local_only_rights_not_verified','restricted','1','now','now'))
        for number, grade, title in [(1,'四年级','三角形的内角和'), (2,'初三','一元二次方程')]:
            rid = f'r{number}'
            text = title + '。请先说明这道题所使用的条件，再写出计算过程。'
            db.execute("INSERT INTO kg_resource VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (rid,'seyk-local-v1',rid,title,grade,'数学','上学期',rid+'.txt','private.mp4','hash',None,None,json.dumps({'title':title,'child_name':'MUST_NOT_EXPORT','private_path':'/family'}),'local:seyk:'+rid,'local_only_rights_not_verified',1,'restricted','1','now','now'))
            db.execute("INSERT INTO kg_segment VALUES(?,?,?,?,?,?,?,?,?,?,?)", ('s'+rid,rid,0,0,len(text),text,hashlib.sha256(text.encode()).hexdigest(),'local:seyk:'+rid,1,'restricted','now'))
        db.commit()


@pytest.fixture
def bundle(tmp_path):
    source, target, package = tmp_path/'source.db', tmp_path/'app.db', tmp_path/'content.zip'
    seed(source)
    initialize(target)
    manifest = export_content(source, package)
    return source, target, package, manifest


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def test_delivery_is_text_only_idempotent_and_preserves_family(bundle):
    source, target, package, manifest = bundle
    source_hash = digest(source)
    with zipfile.ZipFile(package) as archive:
        assert set(archive.namelist()) == {table+'.jsonl' for table in FIELDS} | {'manifest.json'}
        assert b'MUST_NOT_EXPORT' not in archive.read('kg_resource.jsonl')
        assert b'/private/' not in archive.read('kg_corpus.jsonl')
    result = import_content(package, target, expected_sha256=digest(package))
    assert result['tables']['kg_segment']['inserted'] == 2
    with closing(sqlite3.connect(target)) as db:
        assert db.execute('SELECT count(*) FROM child_profile').fetchone()[0] == 2
        db.execute("UPDATE kg_resource SET review_status='rejected' WHERE resource_id='r1'")
        db.commit()
    assert import_content(package, target, expected_sha256=digest(package))['already_imported']
    with closing(sqlite3.connect(target)) as db:
        assert db.execute("SELECT review_status FROM kg_resource WHERE resource_id='r1'").fetchone()[0] == 'rejected'
    assert digest(source) == source_hash


def test_local_course_lookup_is_child_bound_and_durable(bundle):
    _, target, package, _ = bundle
    import_content(package, target, expected_sha256=digest(package))
    db = FixtureDatabase(target)
    service = LocalCourseContextService(db)
    context = service.context_for_child(child_id='a', query='三角形内角和为什么是这样', subject='数学')
    assert context['status'] == 'matched'
    assert context['evidence'][0]['resource_id'] == 'r1'
    assert context['cloud_transmission_allowed'] is False
    assert context['mastery_update_allowed'] is False
    assert service.context_for_child(child_id='b', query='三角形内角和', subject='数学')['status'] == 'no_match'
    with pytest.raises(ValueError): service.context_for_child(child_id='unknown', query='三角形内角和')
    with db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM kg_local_course_context_event').fetchone()[0] == 2
        assert connection.execute('SELECT count(*) FROM kg_mastery_evidence').fetchone()[0] == 0
    assert LocalCourseContextService(db).context_for_child(child_id='b', query='一元二次方程', subject='数学')['status'] == 'matched'


def test_bad_hash_does_not_mutate_target(bundle):
    _, target, package, _ = bundle
    before = digest(target)
    with pytest.raises(ValueError, match='checksum'):
        import_content(package, target, expected_sha256='bad')
    assert digest(target) == before


def test_existing_canonical_identity_is_reused_without_overwriting_review(bundle, tmp_path):
    source, target, _, _ = bundle
    for path, identity, review in [(source,'source-node','unreviewed'), (target,'existing-node','rejected')]:
        with closing(sqlite3.connect(path)) as db:
            db.execute('INSERT INTO kg_node VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identity,'knowledge_point','same-concept','三角形','内角和','{}','local:curriculum',None,'1','local',.7,review,'active','now','now'))
            if path == source:
                db.execute('INSERT INTO kg_resource_node VALUES(?,?,?,?,?,?,?,?)',('r1','sr1',identity,'covers',.5,'unreviewed','local:source','now'))
            db.commit()
    package = tmp_path/'remap.zip'
    export_content(source, package)
    import_content(package, target, expected_sha256=digest(package))
    with closing(sqlite3.connect(target)) as db:
        assert db.execute('SELECT node_id,review_status FROM kg_node').fetchone() == ('existing-node','rejected')
        assert db.execute('SELECT node_id,review_status FROM kg_resource_node').fetchone() == ('existing-node','unreviewed')


def test_corrupt_record_rolls_back_entire_import(bundle, tmp_path):
    _, target, package, _ = bundle
    corrupt = tmp_path/'corrupt.zip'
    with zipfile.ZipFile(package) as original, zipfile.ZipFile(corrupt,'w') as modified:
        for name in original.namelist():
            data = original.read(name)
            if name == 'kg_segment.jsonl': data = data.replace('计算'.encode(), '错误'.encode())
            modified.writestr(name, data)
    with pytest.raises(ValueError, match='hash mismatch'):
        import_content(corrupt, target, expected_sha256=digest(corrupt))
    with closing(sqlite3.connect(target)) as db:
        assert db.execute('SELECT count(*) FROM kg_resource').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM kg_content_delivery').fetchone()[0] == 0


def test_archive_paths_are_not_extracted(bundle, tmp_path):
    _, target, package, _ = bundle
    with zipfile.ZipFile(package,'a') as archive: archive.writestr('../payload', 'bad')
    with pytest.raises(ValueError, match='archive members'):
        import_content(package, target, expected_sha256=digest(package))
    assert not (tmp_path/'payload').exists()


def test_actual_agent_receives_local_evidence_after_import_and_restart(bundle, tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    _, _, package, _ = bundle
    settings = Settings(data_dir=tmp_path/'runtime', database_path=tmp_path/'runtime/app.db',
                        data_encryption_key=b'0'*32, maintenance_enabled=False)
    application = create_app(settings)
    import_content(package, settings.database_path, expected_sha256=digest(package))
    with TestClient(application) as client:
        session = client.post('/v1/sessions', json={'child_id':'child-primary-4','client_action_id':'course-import-session'}).json()
        response = client.post(f"/v1/sessions/{session['session_id']}/agent-turn", json={
            'transcript':'数学里的三角形内角和为什么是这样？','client_action_id':'course-import-question'})
        assert response.status_code == 200
        turn = response.json()
        context = turn['action']['local_learning_context']['course_context']
        assert context['status'] == 'matched'
        assert context['evidence'][0]['resource_id'] == 'r1'
        assert context['cloud_transmission_allowed'] is False
    with TestClient(create_app(settings)) as restarted:
        response = restarted.get('/v1/children/child-primary-4/knowledge-graph/course-context', params={'query':'三角形内角和','subject':'数学'})
        assert response.status_code == 200
        assert response.json()['evidence'][0]['resource_id'] == 'r1'
        other = restarted.get('/v1/children/child-junior-3/knowledge-graph/course-context', params={'query':'三角形内角和','subject':'数学'})
        assert other.json()['evidence'] == []
    with closing(sqlite3.connect(settings.database_path)) as db:
        assert db.execute('SELECT count(*) FROM kg_local_course_context_event').fetchone()[0] == 3
