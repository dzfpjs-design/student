"""Catalog startup batching must retain schema recovery and review semantics."""
from contextlib import closing, contextmanager
import asyncio
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.catalog_bootstrap import CatalogGraphBootstrapper
from home_study_agent.knowledge_graph.official_catalog import OfficialCurriculumCatalogProjector
from home_study_agent.knowledge_graph import service as graph_module
from home_study_agent.knowledge_graph.schema import KG_SCHEMA_VERSION


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'graph.db')
    database.initialize()
    return database


def test_catalog_initializes_schema_per_owned_projection_not_per_node(db, monkeypatch):
    calls = []
    original = graph_module.ensure_schema
    def counted(*args):
        calls.append(True)
        return original(*args)
    monkeypatch.setattr(graph_module, 'ensure_schema', counted)
    result = CatalogGraphBootstrapper(db).run(rebuild_local_candidates=False)
    assert result['nodes'] > 0 and result['exercise_nodes'] > 0
    assert result['candidate_segment_mappings']['skipped'] == 'startup_does_not_rescan_local_corpus'
    assert result['official_catalog']['exact_title_candidates']['skipped'] == 'startup_does_not_rescan_local_corpus'
    assert len(calls) <= 3, ('Repeated schema migration per node/edge', len(calls))


def test_failed_catalog_batch_does_not_commit_partial_nodes(db):
    with db.connect() as c:
        c.execute("""CREATE TRIGGER stop_catalog BEFORE INSERT ON kg_edge
            BEGIN SELECT RAISE(ABORT, 'isolated catalog failure'); END""")
        before = c.execute('SELECT COUNT(*) FROM kg_node').fetchone()[0]
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='isolated catalog failure'):
        CatalogGraphBootstrapper(db).run(rebuild_local_candidates=False)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM kg_node').fetchone()[0] == before


def test_same_projector_rechecks_schema_after_restore(db, tmp_path):
    backup = tmp_path / 'before.db'
    with db.connect() as c, closing(sqlite3.connect(backup)) as saved:
        c.backup(saved)
    projector = OfficialCurriculumCatalogProjector(db)
    first = projector.run(rebuild_local_candidates=False)
    with closing(sqlite3.connect(backup)) as saved, db.connect() as c:
        saved.backup(c)
    with db.connect() as c:
        c.execute('DELETE FROM kg_schema_migration WHERE version=?', (KG_SCHEMA_VERSION,))
        c.execute('DROP TABLE kg_atomic_merge_candidate')
        c.commit()
    restored = projector.run(rebuild_local_candidates=False)
    assert first == restored
    with db.connect() as c:
        assert c.execute('SELECT 1 FROM kg_schema_migration WHERE version=?', (KG_SCHEMA_VERSION,)).fetchone()
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_atomic_merge_candidate'").fetchone()


def test_official_projection_preserves_approved_node_and_does_not_resume_atomic_jobs(db):
    projector = OfficialCurriculumCatalogProjector(db)
    projector.run(rebuild_local_candidates=False)
    with db.connect() as c:
        node = c.execute("SELECT node_id FROM kg_node WHERE canonical_key LIKE 'catalog-atom:%' LIMIT 1").fetchone()[0]
        c.execute("UPDATE kg_node SET review_status='approved',title='Reviewed title',source_ref='parent-reviewed',confidence=1 WHERE node_id=?", (node,))
        c.execute("""INSERT INTO kg_atomic_extraction_job(job_id,idempotency_key,status,scope_json,
            prompt_version,created_at,updated_at) VALUES('stopped','stopped','interrupted','{}','isolated','2026-09-01','2026-09-01')""")
        c.execute("""INSERT INTO kg_atomic_extraction_batch(batch_id,job_id,batch_index,resource_ids_json,
            input_json,input_sha256,status,attempt_count,created_at,updated_at)
            VALUES('stopped-batch','stopped',0,'[]','{}','isolated','interrupted',2,'2026-09-01','2026-09-01')""")
        before = [tuple(r) for r in c.execute('SELECT * FROM kg_atomic_extraction_batch')]
        jobs = [tuple(r) for r in c.execute('SELECT * FROM kg_atomic_extraction_job')]
        c.commit()
    projector.run(rebuild_local_candidates=False)
    with db.connect() as c:
        row = c.execute('SELECT title,source_ref,review_status,confidence FROM kg_node WHERE node_id=?', (node,)).fetchone()
        assert tuple(row) == ('Reviewed title', 'parent-reviewed', 'approved', 1.0)
        assert [tuple(r) for r in c.execute('SELECT * FROM kg_atomic_extraction_batch')] == before
        assert [tuple(r) for r in c.execute('SELECT * FROM kg_atomic_extraction_job')] == jobs


@pytest.mark.parametrize('prior', ['approved', 'unreviewed'])
@pytest.mark.parametrize('next_review', ['approved', 'unreviewed', 'rejected'])
def test_batch_node_and_edge_conflicts_equal_public_service(db, prior, next_review):
    graph = graph_module.KnowledgeGraphService(db)
    def write(writer, prefix, review, title):
        first = writer.upsert_node(canonical_key=prefix+'-a', node_type='knowledge_point',
            title=title, source_ref=title, review_status=review, confidence=.7)
        second = writer.upsert_node(canonical_key=prefix+'-b', node_type='exercise',
            title=title, source_ref=title, review_status=review)
        writer.link(first, second, 'assessed_by', rationale=title, source_ref=title,
            confidence=.6, review_status=review)
    write(graph, 'public', prior, 'before')
    write(graph, 'public', next_review, 'after')
    with graph.catalog_batch() as batch:
        write(batch, 'batch', prior, 'before')
        write(batch, 'batch', next_review, 'after')
    with db.connect() as c:
        def records(prefix):
            nodes = c.execute('''SELECT title,description,scope_json,source_ref,source_time,
                version_label,license_boundary,confidence,review_status,status FROM kg_node
                WHERE canonical_key LIKE ? ORDER BY canonical_key''', (prefix+'-%',)).fetchall()
            edges = c.execute('''SELECT e.relation_type,e.rationale,e.source_ref,e.version_label,
                e.confidence,e.review_status FROM kg_edge e JOIN kg_node n ON n.node_id=e.source_node_id
                WHERE n.canonical_key=?''', (prefix+'-a',)).fetchall()
            return [tuple(r) for r in nodes], [tuple(r) for r in edges]
        assert records('public') == records('batch')


def test_cancelled_batch_rolls_back_and_escaped_writer_cannot_write(db):
    graph = graph_module.KnowledgeGraphService(db)
    with pytest.raises(asyncio.CancelledError):
        with graph.catalog_batch() as batch:
            batch.upsert_node(canonical_key='cancelled', node_type='knowledge_point',
                title='cancelled', source_ref='isolated')
            raise asyncio.CancelledError()
    with pytest.raises(RuntimeError, match='CATALOG_BATCH_CLOSED'):
        batch.upsert_node(canonical_key='escaped', node_type='knowledge_point', title='escaped', source_ref='isolated')
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM kg_node WHERE canonical_key IN ('cancelled','escaped')").fetchone()[0] == 0


def test_batch_serializes_writes_and_new_batch_rechecks_schema(db):
    graph = graph_module.KnowledgeGraphService(db)
    with graph.catalog_batch():
        with closing(sqlite3.connect(db.path, timeout=0)) as other:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
    with db.connect() as c:
        c.execute('DROP TABLE kg_atomic_merge_candidate')
        c.commit()
    with graph.catalog_batch() as batch:
        batch.upsert_node(canonical_key='next', node_type='knowledge_point', title='next', source_ref='isolated')
    with db.connect() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='kg_atomic_merge_candidate'").fetchone()


def test_unknown_database_retains_original_operation_ownership(db, monkeypatch):
    class Adapter:
        @contextmanager
        def connect(self):
            with db.connect() as c:
                yield c
    graph = graph_module.KnowledgeGraphService(Adapter())
    calls = []
    original = graph_module.ensure_schema
    def counted(*args):
        calls.append(True)
        return original(*args)
    monkeypatch.setattr(graph_module, 'ensure_schema', counted)
    with graph.catalog_batch() as batch:
        assert batch is graph
        for key in ('one', 'two'):
            batch.upsert_node(canonical_key=key, node_type='knowledge_point', title=key, source_ref='isolated')
    assert len(calls) == 2
