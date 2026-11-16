from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.atomic_knowledge import (
    AtomicObjectiveExtractor,
    AtomicCandidateStore,
    validate_prerequisite_acyclic,
)
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor


def _seyk_metadata(path: Path) -> None:
    connection=sqlite3.connect(path)
    connection.executescript("""
      CREATE TABLE resources(resource_id TEXT PRIMARY KEY,title TEXT,description TEXT,teacher TEXT);
      CREATE TABLE resource_tree(resource_id TEXT,stage_title TEXT,grade_title TEXT,subject_title TEXT,
        version_title TEXT,volumn_title TEXT,unit_path TEXT,chapter_title TEXT);
      INSERT INTO resources VALUES('106616','二次函数的图像和性质 第三课时','', '张老师');
      INSERT INTO resource_tree VALUES('106616','初中','初三','初中数学','苏科版','上册',
        '上册/第5章 二次函数/5.2 二次函数的图像和性质','5.2 二次函数的图像和性质');
    """)
    connection.commit();connection.close()


def test_seyk_metadata_preserves_curriculum_structure(tmp_path: Path) -> None:
    metadata=tmp_path/'seyk.db';_seyk_metadata(metadata)
    transcripts=tmp_path/'transcripts';transcripts.mkdir()
    (transcripts/'106616.txt').write_text('学习目标：能根据解析式确定抛物线的开口方向。',encoding='utf-8')
    database=Database(tmp_path/'study.db');database.initialize()
    spec=CorpusSpec('seyk','苏e优课','seyk',transcripts,None,'local','local_only',metadata)
    ingestor=LocalCorpusIngestor(database);job=ingestor.start(spec,idempotency_key='seyk-meta-v1')
    assert ingestor.run(spec,job)['status']=='completed'
    with database.connect() as connection:
        row=connection.execute('SELECT title,grade_scope,subject_scope,semester_scope,metadata_json FROM kg_resource').fetchone()
    assert (row['title'],row['grade_scope'],row['subject_scope'],row['semester_scope']) == (
        '二次函数的图像和性质 第三课时','初三','数学','上学期')
    assert '苏科版' in row['metadata_json'] and '第5章 二次函数' in row['metadata_json']


def test_bilibili_metadata_covers_legacy_lists_and_prefers_v2_scope(tmp_path: Path) -> None:
    metadata=tmp_path/'bili.db';connection=sqlite3.connect(metadata)
    connection.executescript("""
      CREATE TABLE selected(bvid TEXT PRIMARY KEY,grade TEXT,subject TEXT,rank INT,score INT,status TEXT,transcript TEXT);
      CREATE TABLE selected_aoshu(bvid TEXT PRIMARY KEY,grade TEXT,subject TEXT,semester TEXT,score INT,status TEXT,transcript TEXT);
      CREATE TABLE selected_v2(bvid TEXT PRIMARY KEY,grade TEXT,subject TEXT,semester TEXT,status TEXT,transcript TEXT);
      CREATE TABLE details(bvid TEXT PRIMARY KEY,title TEXT,author TEXT,desc TEXT);
      INSERT INTO selected VALUES('BVlegacy','初三','政治',1,90,'done','');
      INSERT INTO selected VALUES('BVprefer','初二','数学',1,90,'done','');
      INSERT INTO selected_v2 VALUES('BVprefer','初三','物理','上学期','done','');
      INSERT INTO selected_aoshu VALUES('BVaoshu','四年级','奥数','全册',90,'done','');
      INSERT INTO details VALUES('BVlegacy','道德与法治专题','老师','说明');
      INSERT INTO details VALUES('BVprefer','欧姆定律','老师','说明');
      INSERT INTO details VALUES('BVaoshu','四年级奥数','老师','说明');
    """);connection.commit();connection.close()
    rows=LocalCorpusIngestor._metadata(metadata,'bilibili')
    assert rows['BVlegacy']['subject']=='道德与法治' and rows['BVlegacy']['grade']=='初三'
    assert rows['BVprefer']['subject']=='物理' and rows['BVprefer']['grade']=='初三'
    assert rows['BVaoshu']['subject']=='奥数' and rows['BVaoshu']['semester']=='全册'


def test_bilibili_video_inventory_ignores_hidden_download_fragments(tmp_path: Path) -> None:
    transcripts=tmp_path/'transcripts';transcripts.mkdir()
    videos=tmp_path/'videos';videos.mkdir();(videos/'BVcourse.mp4').write_bytes(b'video')
    fragments=videos/'.tmp_BVcourse';fragments.mkdir();(fragments/'000.mp4').write_bytes(b'fragment')
    spec=CorpusSpec('bili','B站','bilibili',transcripts,videos,'local','local_only')
    assert LocalCorpusIngestor._video_files(spec)=={'BVcourse':videos/'BVcourse.mp4'}


def test_metadata_refresh_removes_only_stale_resources_without_segment_evidence(tmp_path: Path) -> None:
    metadata=tmp_path/'bili.db';connection=sqlite3.connect(metadata)
    connection.executescript("""
      CREATE TABLE selected(bvid TEXT PRIMARY KEY,grade TEXT,subject TEXT,rank INT,score INT,status TEXT,transcript TEXT);
      CREATE TABLE selected_aoshu(bvid TEXT PRIMARY KEY,grade TEXT,subject TEXT,semester TEXT,score INT,status TEXT,transcript TEXT);
      CREATE TABLE selected_v2(bvid TEXT PRIMARY KEY,grade TEXT,subject TEXT,semester TEXT,status TEXT,transcript TEXT);
      CREATE TABLE details(bvid TEXT PRIMARY KEY,title TEXT,author TEXT,desc TEXT);
      INSERT INTO selected_v2 VALUES('BVcourse','四年级','数学','上学期','done','');
      INSERT INTO details VALUES('BVcourse','四年级数学','老师','说明');
    """);connection.commit();connection.close()
    transcripts=tmp_path/'transcripts';transcripts.mkdir();(transcripts/'BVcourse.txt').write_text('学习目标：计算三位数除以两位数。',encoding='utf-8')
    videos=tmp_path/'videos';videos.mkdir();(videos/'BVcourse.mp4').write_bytes(b'video')
    database=Database(tmp_path/'study.db');database.initialize()
    spec=CorpusSpec('bili','B站','bilibili',transcripts,videos,'local','local_only',metadata)
    ingestor=LocalCorpusIngestor(database);job=ingestor.start(spec,idempotency_key='bili-import');ingestor.run(spec,job)
    with database.connect() as connection:
        connection.execute("""INSERT INTO kg_resource(resource_id,corpus_id,external_id,title,transcript_relative_path,
          transcript_sha256,metadata_json,source_ref,license_boundary,confidence,review_status,version_label,ingested_at,updated_at)
          VALUES('stale','bili','000','fragment','',?,'{}','local','local_only',.1,'restricted','v1','now','now')""",('0'*64,))
        connection.commit()
    result=ingestor.refresh_metadata(spec)
    assert result['stale_video_fragments_removed']==1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM kg_resource WHERE corpus_id='bili'").fetchone()[0]==1


def test_explicit_objectives_become_observable_atoms_not_topic_labels() -> None:
    extractor=AtomicObjectiveExtractor()
    candidates=extractor.extract(
        title='二次函数的图像和性质 第三课时',
        transcript='学习目标：一、理解二次函数图像开口方向与系数a符号的关系。二、能根据解析式确定对称轴。教学重点如下。',
        scope={'grade':'初三','subject':'数学'},
        unit_path='上册/第5章 二次函数',
    )
    assert {item.action_verb for item in candidates} == {'解释','确定'}
    assert all(item.object_text not in {'二次函数','二次函数图像和性质'} for item in candidates)
    assert all(item.mastery_criterion and item.canonical_signature for item in candidates)
    assert all(item.atomicity['is_observable'] and item.atomicity['single_action'] for item in candidates)


def test_one_objective_with_two_objects_is_split_into_separately_assessable_atoms() -> None:
    candidates=AtomicObjectiveExtractor().extract(
        title='耐久跑',
        transcript='教学目标：知道耐久跑的呼吸方式以及跑动节奏的练习方法。能够把呼吸节奏与跑动节奏结合。教学重难点如下。',
        scope={'grade':'初一','subject':'体育与健康'},
        unit_path='耐久跑',
    )
    objects={item.object_text for item in candidates}
    assert any('呼吸方式' in item for item in objects)
    assert any('跑动节奏' in item for item in objects)
    assert any(item.action_verb == '运用' and '呼吸节奏' in item.object_text for item in candidates)


def test_topic_only_title_is_not_falsely_promoted_to_an_atom() -> None:
    assert AtomicObjectiveExtractor().extract(
        title='二次函数', transcript='今天我们学习二次函数。',
        scope={'grade':'初三','subject':'数学'}, unit_path='第5章 二次函数',
    ) == []


def test_teacher_assessment_prompt_adds_evidence_without_becoming_approved() -> None:
    candidates=AtomicObjectiveExtractor().extract(
        title='圆周角',
        transcript='今天学习圆周角。请同学们判断同弧所对的两个圆周角是否相等，并说明理由。',
        scope={'grade':'初三','subject':'数学'},unit_path='第2章 圆',
    )
    assert any(item.source_kind=='assessment_prompt' and item.action_verb=='判断' for item in candidates)
    assert all(item.confidence<.8 for item in candidates)


def test_generic_task_and_comparative_degree_title_do_not_masquerade_as_atoms() -> None:
    extractor=AtomicObjectiveExtractor()
    assert extractor.extract(title='形容词（包括比较级和最高级）',transcript='今天学习形容词。',
        scope={'grade':'初三','subject':'英语'},unit_path='语法') == []
    candidates=extractor.extract(title='练习',transcript='请同学们完成下列任务。',
        scope={'grade':'初三','subject':'语文'},unit_path='复习')
    assert candidates == []


def test_candidate_store_deduplicates_signature_but_keeps_multiple_evidence(tmp_path: Path) -> None:
    database=Database(tmp_path/'study.db');database.initialize();root=tmp_path/'transcripts';root.mkdir()
    (root/'a.txt').write_text('学习目标：能根据解析式确定抛物线的开口方向。',encoding='utf-8')
    (root/'b.txt').write_text('教学目标：能够根据解析式确定抛物线开口方向。',encoding='utf-8')
    spec=CorpusSpec('sample','样本','seyk',root,None,'local','local_only')
    ingestor=LocalCorpusIngestor(database);job=ingestor.start(spec,idempotency_key='sample-v1');ingestor.run(spec,job)
    store=AtomicCandidateStore(database);result=store.extract_pending()
    assert result['resources_processed']==2
    with database.connect() as connection:
        rows=connection.execute('SELECT canonical_signature,status,atomicity_json FROM kg_atomic_candidate').fetchall()
    assert len(rows)==2 and len({row['canonical_signature'] for row in rows})==1
    assert {row['status'] for row in rows}=={'pending'}


def test_prerequisite_validation_rejects_cycles_and_self_edges() -> None:
    with pytest.raises(ValueError,match='self'):
        validate_prerequisite_acyclic([('a','a')])
    with pytest.raises(ValueError,match='cycle'):
        validate_prerequisite_acyclic([('a','b'),('b','c'),('c','a')])
    assert validate_prerequisite_acyclic([('a','b'),('a','c'),('b','d'),('c','d')]) is True
