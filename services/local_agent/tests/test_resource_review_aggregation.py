from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.quality import GraphQualityService
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor


def test_resource_review_aggregates_segments_and_persists_undo(tmp_path: Path) -> None:
    db=Database(tmp_path/'study.db');db.initialize();root=tmp_path/'t';root.mkdir()
    (root/'lesson.txt').write_text(('求根公式用于一元二次方程。'*120),encoding='utf-8')
    spec=CorpusSpec('aggregate','聚合','local',root,None,'test','user_local');ingest=LocalCorpusIngestor(db);ingest.run(spec,ingest.start(spec,idempotency_key='aggregate-v1'))
    graph=KnowledgeGraphService(db);node=graph.upsert_node(canonical_key='formula',node_type='knowledge_point',title='求根公式',scope={'grade':'初三','subject':'数学'},source_ref='test',license_boundary='test',review_status='approved')
    with db.connect() as c:
        resource=c.execute('SELECT resource_id FROM kg_resource').fetchone()[0]; segments=c.execute('SELECT segment_id FROM kg_segment').fetchall()
    assert len(segments)>=2
    for index,row in enumerate(segments): graph.map_segment(resource,row[0],node,'covers',confidence=.7,source_ref='aggregate-test' if index % 2 else 'file:///Users/example/private/lesson.txt')
    with db.connect() as c:
        c.execute("UPDATE kg_resource SET source_ref=? WHERE resource_id=?",('/Users/example/private/lesson.txt',resource)); c.commit()
    quality=GraphQualityService(db);item=quality.resource_review_queue()[0]
    assert item['evidence_segment_count']==len(segments) and 'text' not in item
    assert item['mapping_sources']==['aggregate-test','本机来源（路径已隐藏）']
    assert item['mapping_source_count']==2
    assert item['resource_source_ref']=='本机来源（路径已隐藏）'
    assert quality.resource_review_summary()=={"pending_resource_review_items":1,"pending_resources":1,
                                               "pending_evidence_segments":len(segments),"review_items_per_resource_p95":1}
    evidence=quality.resource_review_evidence(resource_id=resource,node_id=node,relation_type='covers')
    assert 1 <= len(evidence) <= 3 and {'sequence','source','preview'} <= evidence[0].keys()
    assert 'transcript_relative_path' not in evidence[0] and len(evidence[0]['preview']) <= 181
    assert all('/Users/' not in str(row) and 'file://' not in str(row) for row in evidence)
    redacted=quality._redacted_preview('URL https://example.test/a 邮箱 kid@example.test 手机 19900000001 学号 202612345 数学 3.14 和 12')
    assert 'https://' not in redacted and 'example.test' not in redacted and '19900000001' not in redacted and '202612345' not in redacted
    assert '3.14' in redacted and '12' in redacted
    approved=quality.record_resource_review(resource_id=resource,node_id=node,relation_type='covers',decision='approved',reviewer='parent',client_action_id='aggregate-approve-001')
    assert approved['affected_segment_count']==len(segments) and approved['statuses']=={'approved':len(segments)}
    reopened=GraphQualityService(Database(tmp_path/'study.db'));assert not reopened.resource_review_queue()
    history=reopened.resource_review_history('parent'); assert history[0]['decision']=='approved' and history[0]['affected_segment_count']==len(segments)
    # A response-loss replay is a no-op, not an additional audit decision.
    replay=reopened.record_resource_review(resource_id=resource,node_id=node,relation_type='covers',decision='approved',reviewer='parent',client_action_id='aggregate-approve-001')
    assert replay['statuses']=={'approved':len(segments)}
    with db.connect() as c: assert c.execute("SELECT COUNT(*) FROM kg_mapping_review_event WHERE action='decision'").fetchone()[0]==len(segments)
    undone=reopened.undo_resource_review(resource_id=resource,node_id=node,relation_type='covers',reviewer='parent',client_action_id='aggregate-undo-001')
    assert undone['statuses']=={'unreviewed':len(segments)} and len(reopened.resource_review_queue())==1
    split=reopened.record_resource_review(resource_id=resource,node_id=node,relation_type='covers',decision='needs_split',reviewer='parent',client_action_id='aggregate-split-001')
    assert split['statuses']=={'unreviewed':len(segments)} and not reopened.resource_review_queue()
    assert reopened.resource_review_summary()['pending_resource_review_items']==0
    reopened.undo_resource_review(resource_id=resource,node_id=node,relation_type='covers',reviewer='parent',client_action_id='aggregate-split-undo-001')
    assert len(reopened.resource_review_queue())==1
    reopened.record_resource_review(resource_id=resource,node_id=node,relation_type='covers',decision='approved',reviewer='other-parent',client_action_id='aggregate-approve-other')
    assert all(row['reviewer']=='parent' for row in reopened.resource_review_history('parent'))
    # Pre-resource-level databases can contain divergent per-segment decisions.
    # They are one explicit destructive undo target, not two misleading buttons.
    reopened.record_review(resource_id=resource,segment_id=segments[0][0],node_id=node,relation_type='covers',decision='approved',reviewer='parent',client_action_id='legacy-segment-approve-001')
    reopened.record_review(resource_id=resource,segment_id=segments[1][0],node_id=node,relation_type='covers',decision='rejected',reviewer='parent',client_action_id='legacy-segment-reject-001')
    mixed=reopened.resource_review_history('parent')
    assert len(mixed)==1 and mixed[0]['decision']=='mixed_legacy' and mixed[0]['decision_values']==['approved','rejected']
    reloaded=GraphQualityService(Database(tmp_path/'study.db'))
    assert reloaded.resource_review_history('parent')[0]['decision']=='mixed_legacy'
    reloaded.undo_resource_review(resource_id=resource,node_id=node,relation_type='covers',reviewer='parent',client_action_id='legacy-resource-undo-001')
    assert not GraphQualityService(Database(tmp_path/'study.db')).resource_review_history('parent')
