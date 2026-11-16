from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import sleep

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.coverage_mapping import AtomicCoverageMapper
from home_study_agent.knowledge_graph.quality import GraphQualityService
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor


def test_coverage_mapper_checkpoints_and_preserves_rejected_mapping(tmp_path: Path) -> None:
    database=Database(tmp_path/'study.db');database.initialize(); root=tmp_path/'txt';root.mkdir()
    (root/'one.txt').write_text('欧姆定律说明电流和电阻的关系。',encoding='utf-8')
    (root/'two.txt').write_text('欧姆定律应用练习。',encoding='utf-8')
    spec=CorpusSpec('coverage','覆盖','local',root,None,'local','user_local')
    ingest=LocalCorpusIngestor(database);ingest.run(spec,ingest.start(spec,idempotency_key='coverage-ingest'))
    graph=KnowledgeGraphService(database);node=graph.upsert_node(canonical_key='ohm',node_type='knowledge_point',title='欧姆定律',scope={'subject':'物理','grade':'初三'},source_ref='standard',license_boundary='test')
    with database.connect() as con: con.execute("UPDATE kg_resource SET subject_scope='物理',grade_scope='初三'");con.commit()
    mapper=AtomicCoverageMapper(database);job=mapper.start(idempotency_key='coverage-v1')
    first=mapper.run(job,max_resources=1);assert first['status']=='processing'
    done=mapper.run(job,max_resources=10);assert done['status']=='completed' and done['mapped_rows']>=2
    with database.connect() as con:
        row=con.execute("SELECT resource_id,segment_id FROM kg_resource_node WHERE node_id=? ORDER BY resource_id LIMIT 1",(node,)).fetchone()
        con.execute("UPDATE kg_resource_node SET review_status='rejected' WHERE resource_id=? AND segment_id=? AND node_id=?",(row['resource_id'],row['segment_id'],node));con.commit()
    # A completed idempotency key remains stable; a later fresh run must not overwrite rejection.
    fresh=mapper.start(idempotency_key='coverage-v2');mapper.run(fresh,max_resources=10)
    with database.connect() as con: status=con.execute("SELECT review_status FROM kg_resource_node WHERE resource_id=? AND segment_id=? AND node_id=?",(row['resource_id'],row['segment_id'],node)).fetchone()[0]
    assert status=='rejected'


def test_coverage_mapper_maps_deduplicated_resource_occurrence(tmp_path: Path) -> None:
    database=Database(tmp_path/'study.db');database.initialize();root=tmp_path/'txt';root.mkdir()
    text='万位决定这个大数的读法。'; (root/'one.txt').write_text(text,encoding='utf-8'); (root/'two.txt').write_text(text,encoding='utf-8')
    spec=CorpusSpec('dedupe-coverage','覆盖','local',root,None,'local','user_local')
    ingest=LocalCorpusIngestor(database);ingest.run(spec,ingest.start(spec,idempotency_key='dedupe-coverage-ingest'))
    graph=KnowledgeGraphService(database);node=graph.upsert_node(canonical_key='place-ten-thousand',node_type='knowledge_point',title='万位',scope={'subject':'数学','grade':'四年级'},source_ref='standard',license_boundary='test')
    with database.connect() as con:
        con.execute("UPDATE kg_resource SET subject_scope='数学',grade_scope='四年级'");con.commit()
    mapper=AtomicCoverageMapper(database);job=mapper.start(idempotency_key='dedupe-coverage-v1');assert mapper.run(job,max_resources=10)['status']=='completed'
    with database.connect() as con:
        mapped=con.execute("SELECT COUNT(DISTINCT resource_id) FROM kg_resource_node WHERE node_id=? AND source_ref=?",(node,mapper.source_ref)).fetchone()[0]
    assert mapped==2


def test_coverage_mapper_matches_english_atom_without_case_sensitivity(tmp_path: Path) -> None:
    database=Database(tmp_path/'study.db');database.initialize();root=tmp_path/'txt';root.mkdir()
    (root/'lesson.txt').write_text('Apple is a common English word.',encoding='utf-8')
    spec=CorpusSpec('english-case','覆盖','local',root,None,'local','user_local')
    ingest=LocalCorpusIngestor(database);ingest.run(spec,ingest.start(spec,idempotency_key='english-case-ingest'))
    graph=KnowledgeGraphService(database);node=graph.upsert_node(canonical_key='apple',node_type='knowledge_point',title='apple',scope={'subject':'英语','grade':'四年级'},source_ref='standard',license_boundary='test')
    with database.connect() as con:
        con.execute("UPDATE kg_resource SET subject_scope='英语',grade_scope='四年级'");con.commit()
    mapper=AtomicCoverageMapper(database);job=mapper.start(idempotency_key='english-case-map');assert mapper.run(job,max_resources=10)['status']=='completed'
    with database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM kg_resource_node WHERE node_id=?",(node,)).fetchone()[0]==1


def test_one_character_chinese_atom_is_not_auto_mapped_from_generic_corpus(tmp_path: Path) -> None:
    database=Database(tmp_path/'study.db');database.initialize();root=tmp_path/'txt';root.mkdir()
    (root/'lesson.txt').write_text('万位是大数读写的基础。',encoding='utf-8')
    spec=CorpusSpec('short-chinese','覆盖','local',root,None,'local','user_local')
    ingest=LocalCorpusIngestor(database);ingest.run(spec,ingest.start(spec,idempotency_key='short-chinese-ingest'))
    graph=KnowledgeGraphService(database);node=graph.upsert_node(canonical_key='wan',node_type='knowledge_point',title='万',scope={'subject':'数学','grade':'四年级'},source_ref='standard',license_boundary='test')
    with database.connect() as con:
        con.execute("UPDATE kg_resource SET subject_scope='数学',grade_scope='四年级'");con.commit()
    mapper=AtomicCoverageMapper(database);job=mapper.start(idempotency_key='short-chinese-map');assert mapper.run(job,max_resources=10)['status']=='completed'
    with database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM kg_resource_node WHERE node_id=?",(node,)).fetchone()[0] == 0


def test_approved_search_uses_deduplicated_occurrence_scope_and_approval(tmp_path: Path) -> None:
    database=Database(tmp_path/'study.db');database.initialize();root=tmp_path/'txt';root.mkdir()
    text='apple 是一个常见英语单词。'; (root/'one.txt').write_text(text,encoding='utf-8'); (root/'two.txt').write_text(text,encoding='utf-8')
    spec=CorpusSpec('dedupe-search','覆盖','local',root,None,'local','user_local')
    ingestor=LocalCorpusIngestor(database);ingestor.run(spec,ingestor.start(spec,idempotency_key='dedupe-search-ingest'))
    graph=KnowledgeGraphService(database);node=graph.upsert_node(canonical_key='apple-p4',node_type='knowledge_point',title='四年级 apple',scope={'subject':'英语','grade':'四年级'},source_ref='standard',license_boundary='test',review_status='approved')
    j3_node=graph.upsert_node(canonical_key='apple-j3',node_type='knowledge_point',title='初三 apple',scope={'subject':'英语','grade':'初三'},source_ref='standard',license_boundary='test',review_status='approved')
    with database.connect() as con:
        first=con.execute("SELECT resource_id FROM kg_resource WHERE external_id='one'").fetchone()[0]
        second=con.execute("SELECT resource_id FROM kg_resource WHERE external_id='two'").fetchone()[0]
        segment=con.execute("SELECT segment_id FROM kg_segment_occurrence WHERE resource_id=?",(second,)).fetchone()[0]
        con.execute("UPDATE kg_resource SET subject_scope='英语',grade_scope='初三' WHERE resource_id=?",(first,))
        con.execute("UPDATE kg_resource SET subject_scope='英语',grade_scope='四年级' WHERE resource_id=?",(second,));con.commit()
    graph.map_segment(second,segment,node,'covers',confidence=.9,source_ref='test-dedupe',review_status='unreviewed')
    graph.map_segment(first,segment,j3_node,'covers',confidence=.9,source_ref='test-dedupe',review_status='unreviewed')
    GraphQualityService(database).record_review(resource_id=second,segment_id=segment,node_id=node,relation_type='covers',decision='approved',reviewer='tester',client_action_id='approve-dedupe-search')
    GraphQualityService(database).record_review(resource_id=first,segment_id=segment,node_id=j3_node,relation_type='covers',decision='approved',reviewer='tester',client_action_id='approve-dedupe-search-j3')
    fourth=graph.search_explanations_with_policy('apple',grade='四年级',subject='英语',approved_only=True)
    third=graph.search_explanations_with_policy('apple',grade='初三',subject='英语',approved_only=True)
    assert fourth and fourth[0]['resource_id']==second
    assert [item['resource_id'] for item in fourth[0]['source_occurrences']]==[second]
    assert {item['knowledge_point'] for item in fourth[0]['approvals']}=={'四年级 apple'}
    assert third and {item['knowledge_point'] for item in third[0]['approvals']}=={'初三 apple'}


def test_coverage_mapper_run_lease_prevents_concurrent_checkpoint_advancement(tmp_path: Path) -> None:
    database=Database(tmp_path/'study.db');database.initialize();root=tmp_path/'txt';root.mkdir()
    (root/'one.txt').write_text('欧姆定律说明电流和电阻的关系。',encoding='utf-8')
    (root/'two.txt').write_text('欧姆定律应用练习。',encoding='utf-8')
    spec=CorpusSpec('coverage-lease','覆盖','local',root,None,'local','user_local')
    ingestor=LocalCorpusIngestor(database);ingestor.run(spec,ingestor.start(spec,idempotency_key='coverage-lease-ingest'))
    graph=KnowledgeGraphService(database);graph.upsert_node(canonical_key='ohm-lease',node_type='knowledge_point',title='欧姆定律',scope={'subject':'物理','grade':'初三'},source_ref='standard',license_boundary='test')
    with database.connect() as con: con.execute("UPDATE kg_resource SET subject_scope='物理',grade_scope='初三'");con.commit()
    mapper=AtomicCoverageMapper(database);job=mapper.start(idempotency_key='coverage-lease-v1')
    mapper.run_lease_seconds=.5;mapper.heartbeat_interval_seconds=.05
    entered=Event();release=Event();original=mapper._map_one
    def paused(*args):
        entered.set();assert release.wait(timeout=5);return original(*args)
    mapper._map_one=paused  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(mapper.run,job,max_resources=10)
        assert entered.wait(timeout=5)
        sleep(.75)  # Longer than the lease: a second worker still cannot take it.
        second=mapper.run(job,max_resources=10)
        release.set();final=first.result(timeout=5)
    assert second['status'] in {'processing','completed'} and final['status']=='completed'
    with database.connect() as con:
        row=con.execute("SELECT processed_resources,run_token,run_lease_expires_at FROM kg_candidate_mapping_job WHERE mapping_job_id=?",(job,)).fetchone()
    assert dict(row)=={'processed_resources':2,'run_token':None,'run_lease_expires_at':None}


def test_coverage_mapper_renews_lease_inside_slow_write_transaction_before_commit(tmp_path: Path) -> None:
    database=Database(tmp_path/'study.db');database.initialize();root=tmp_path/'txt';root.mkdir()
    (root/'one.txt').write_text('欧姆定律说明电流和电阻的关系。',encoding='utf-8')
    spec=CorpusSpec('map-write-lease','覆盖','local',root,None,'local','user_local')
    ingestor=LocalCorpusIngestor(database);ingestor.run(spec,ingestor.start(spec,idempotency_key='map-write-lease-ingest'))
    graph=KnowledgeGraphService(database);node=graph.upsert_node(canonical_key='ohm-write-lease',node_type='knowledge_point',title='欧姆定律',scope={'subject':'物理','grade':'初三'},source_ref='standard',license_boundary='test')
    with database.connect() as con: con.execute("UPDATE kg_resource SET subject_scope='物理',grade_scope='初三'");con.commit()
    mapper=AtomicCoverageMapper(database);job=mapper.start(idempotency_key='map-write-lease-v1')
    mapper.run_lease_seconds=.5;mapper.heartbeat_interval_seconds=.05;entered=Event()
    def slow_commit() -> None:
        entered.set();sleep(.12)  # Runs after BEGIN IMMEDIATE and candidate writes.
    mapper._before_resource_commit=slow_commit  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(mapper.run,job,max_resources=10)
        assert entered.wait(timeout=5)
        second=mapper.run(job,max_resources=10)
        final=first.result(timeout=5)
    assert second['status'] in {'processing','completed'} and final['status']=='completed'
    with database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM kg_resource_node WHERE node_id=?",(node,)).fetchone()[0]==1
        assert con.execute("SELECT processed_resources FROM kg_candidate_mapping_job WHERE mapping_job_id=?",(job,)).fetchone()[0]==1
