from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.catalog_bootstrap import CatalogGraphBootstrapper
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.mistake_graph import MistakeGraphMatcher, _source_overlap_supports_candidate, _score, _chunks
from home_study_agent.repository import StudyRepository
from test_atomic_companion_context import _fixture_app, _fixture_build


def _source(question, subject='数学', title='错题', original_answer=''):
    return dict(title=title, question=question, subject=subject, original_answer=original_answer)


def test_shared_topic_does_not_confirm_a_different_requested_skill():
    def score(question, title):
        return _score({'title': title, 'review_status': 'unreviewed'},
                      _chunks(question), _chunks(question), (), ())
    assert score('图上距离实际距离单位怎么统一', '说明图上距离：实际距离=比例尺') < .83
    assert score('氧化反应为什么需要氧气', '理解氧化反应') < .83
    assert score('判断两个比能否组成比例的前提是什么', '判断两个比能否组成比例') < .83
    assert score('圆柱侧面积怎么算', '计算圆柱侧面积') >= .83


def _fixture(tmp_path):
    app, build = tmp_path / 'app.db', tmp_path / 'atomic.db'
    _fixture_app(app)
    _fixture_build(build)
    return app, build


def _manifest(build: Path):
    checksum = hashlib.sha256(build.read_bytes()).hexdigest()
    build.with_name('atomic-knowledge-manifest.json').write_text(json.dumps({
        'schema': 'hsc.atomic-knowledge.package.v1', 'source_label': 'fixture-v6.db',
        'packaged_name': build.name, 'read_only_runtime': True, 'bytes': build.stat().st_size, 'sha256': checksum,
    }), encoding='utf-8')
    return checksum


def test_source_bound_match_and_directional_review_state(tmp_path):
    app, build = _fixture(tmp_path)
    matcher = MistakeGraphMatcher(Database(app), build)
    source = _source('请解释分数与小数的关系。', title='分数与小数的关系', original_answer='无关作答')
    matched = matcher.match('child-private', source)
    assert matched['status'] == 'matched'
    assert matched['primary']['key'] == 'fraction'
    assert matched['primary']['review_status'] == 'unreviewed'
    assert matched['prerequisites'] == [{
        'key': 'decimal', 'title': '理解小数的意义', 'review_status': 'approved', 'relation_review_status': 'unreviewed',
    }]
    assert matched['graph_version'] == matcher.cache_version()
    assert 'local:mindmap' not in json.dumps(matched)
    assert matcher.match('child-private', source | {'subject': '物理'})['status'] == 'no_match'
    assert matcher.match('missing-child', source)['status'] == 'no_match'

    with closing(sqlite3.connect(app)) as connection:
        connection.execute("INSERT INTO kg_atomic_review_decision VALUES('relation','r1','approved','parent','','{}','approve-r1','now')")
        connection.commit()
    approved = matcher.match('child-private', source)
    assert approved['graph_version'] != matched['graph_version']
    assert approved['prerequisites'][0]['relation_review_status'] == 'approved'
    with closing(sqlite3.connect(app)) as connection:
        connection.execute("UPDATE child_profile SET grade='初中三年级' WHERE child_id='child-private'")
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%mastery%' OR name LIKE '%companion_graph%'").fetchone()[0] == 0
    assert matcher.match('child-private', source)['status'] == 'no_match'


def test_model_only_match_and_multiple_original_concepts_are_ambiguous(tmp_path):
    app, build = _fixture(tmp_path)
    matcher = MistakeGraphMatcher(Database(app), build)
    result = matcher.match('child-private', _source('求值。', original_answer='小数的意义'), hints=[{'label': '小数的意义'}])
    assert result['status'] == 'ambiguous'
    assert result['primary'] is None
    assert result['alternatives'][0]['key'] == 'decimal'
    assert result['prerequisites'] == []
    result = matcher.match('child-private', _source('小数的意义；分数与小数的关系。'))
    assert result['status'] == 'ambiguous'
    assert {item['key'] for item in result['alternatives']} == {'decimal', 'fraction'}
    assert matcher.match('child-private', _source('求值。', original_answer='小数的意义'))['status'] == 'no_match'


def test_fraction_operators_match_calculation_over_incidental_terms(tmp_path):
    app, build = _fixture(tmp_path)
    with closing(sqlite3.connect(app)) as connection:
        connection.execute("UPDATE child_profile SET grade='六年级'")
        connection.commit()
    with closing(sqlite3.connect(build)) as connection:
        nodes = [('division', '计算分数除法（转化为乘倒数）'), ('describe', '说明分数除法'), ('simplest', '说明最简分数')]
        connection.executemany('''INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,'小学5-6','数学','concept','{}','',
            '{}','[]','[]','[]',.8,'unreviewed')''', [(key, key, title) for key, title in nodes])
        connection.commit()
    matcher = MistakeGraphMatcher(Database(app), build)
    for text in ('计算 3/4 ÷ 2/5，并写成最简分数。', '计算¾除以⅖，再写成最简分数。', '四分之三除以五分之二等于几？'):
        result = matcher.match('child-private', _source(text))
        assert result['status'] == 'matched'
        assert result['primary']['key'] == 'division'


def test_frozen_wal_header_package_opens_without_sidecars(tmp_path):
    app, build = tmp_path / 'app.db', tmp_path / 'atomic-knowledge.db'
    _fixture_app(app)
    _fixture_build(build)
    with closing(sqlite3.connect(build)) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
    checksum = _manifest(build)
    matcher = MistakeGraphMatcher(Database(app), build)
    assert matcher.atomic.packaged_identity() == ('fixture-v6.db', checksum)
    assert matcher.match('child-private', _source('小数的意义'))['status'] == 'matched'
    assert not build.with_name(build.name + '-wal').exists()
    assert not build.with_name(build.name + '-shm').exists()
    assert hashlib.sha256(build.read_bytes()).hexdigest() == checksum


def test_package_manifest_cannot_hide_live_wal(tmp_path):
    app, build = tmp_path / 'app.db', tmp_path / 'atomic-knowledge.db'
    _fixture_app(app)
    _fixture_build(build)
    with closing(sqlite3.connect(build)) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
    _manifest(build)
    matcher = MistakeGraphMatcher(Database(app), build)
    frozen_version = matcher.cache_version()
    with closing(sqlite3.connect(build)) as writer:
        writer.execute("UPDATE kg_atomic_definition_draft SET title='说明十进制位值' WHERE canonical_signature='decimal'")
        writer.commit()
        assert matcher.atomic.packaged_identity() is None
        assert matcher.cache_version() != frozen_version
        assert matcher.match('child-private', _source('十进制位值'))['primary']['key'] == 'decimal'


def test_bootstrapped_curated_prerequisite_is_approved_without_bulk_import(tmp_path):
    database = Database(tmp_path / 'app.db')
    database.initialize()
    StudyRepository(database).seed_family()
    CatalogGraphBootstrapper(database).run(rebuild_local_candidates=False)
    matcher = MistakeGraphMatcher(database, tmp_path / 'not-installed.db')
    source = _source('用求根公式解方程。', title='一元二次方程求根公式')
    result = matcher.match('child-junior-3', source)
    assert result['status'] == 'matched'
    assert result['primary']['key'] == 'starter:J3.MATH.QUADRATIC.FORMULA'
    assert result['primary']['review_status'] == 'approved'
    assert result['prerequisites'] == [{
        'key': 'starter:J3.MATH.QUADRATIC.DISCRIMINANT', 'title': '根的判别式',
        'review_status': 'approved', 'relation_review_status': 'approved',
    }]
    wrong_grade = matcher.match('child-primary-4', source)
    assert wrong_grade['status'] == 'unavailable'
    assert wrong_grade['primary'] is None


def test_curated_prerequisites_can_cross_grades_without_expanding_targets(tmp_path):
    database = Database(tmp_path / 'app.db')
    database.initialize()
    StudyRepository(database).seed_family()
    graph = KnowledgeGraphService(database)

    def node(key, title, grade, subject='数学', *, approved=True):
        scope = {'track': 'school', 'subject': subject}
        if grade is not None:
            scope['grade'] = grade
        return graph.upsert_node(canonical_key=key, node_type='knowledge_point', title=title,
                                 scope=scope, source_ref='fixture', license_boundary='project_curated_local',
                                 review_status='approved' if approved else 'unreviewed')

    target = node('target', '计算分数除法', '四年级')
    earlier = node('earlier', '理解分数乘法', '小学三年级')
    same_grade = node('same', '识别倒数含义', '四年级')
    later = node('later', '理解正比例关系', '五年级')
    wrong_subject = node('physics', '认识物理前置', '三年级', subject='物理')
    unknown_grade = node('unknown', '认识未知年级前置', None)
    unreviewed = node('unreviewed', '认识未审核前置', '三年级', approved=False)
    wrong_direction = node('reverse', '理解反向关系', '三年级')
    unapproved_edge = node('edge', '理解待审关系', '三年级')
    junior_target = node('junior-target', '掌握一元二次方程求根', '初中三年级')
    graph.link(target, junior_target, 'prerequisite_of', rationale='fixture', source_ref='fixture', review_status='approved')
    for source in (earlier, same_grade, later, wrong_subject, unknown_grade, unreviewed):
        graph.link(source, target, 'prerequisite_of', rationale='fixture', source_ref='fixture', review_status='approved')
    graph.link(target, wrong_direction, 'prerequisite_of', rationale='fixture', source_ref='fixture', review_status='approved')
    graph.link(unapproved_edge, target, 'prerequisite_of', rationale='fixture', source_ref='fixture')

    with database.connect() as connection:
        before_children = [tuple(row) for row in connection.execute('SELECT * FROM child_profile ORDER BY child_id')]
        before_nodes = [tuple(row) for row in connection.execute('SELECT * FROM kg_node ORDER BY node_id')]
        before_edges = [tuple(row) for row in connection.execute('SELECT * FROM kg_edge ORDER BY edge_id')]
    matcher = MistakeGraphMatcher(database, tmp_path / 'not-installed.db')
    result = matcher.match('child-primary-4', _source('计算分数除法'))
    assert result['status'] == 'matched'
    assert result['primary']['key'] == 'target'
    assert {item['key'] for item in result['prerequisites']} == {'earlier', 'same'}
    assert matcher.match('child-primary-4', _source('理解分数乘法'))['primary'] is None
    assert matcher.match('child-primary-4', _source('理解正比例关系'))['primary'] is None
    cross_stage = matcher.match('child-junior-3', _source('掌握一元二次方程求根'))
    assert cross_stage['primary']['key'] == 'junior-target'
    assert [item['key'] for item in cross_stage['prerequisites']] == ['target']
    with database.connect() as connection:
        assert [tuple(row) for row in connection.execute('SELECT * FROM child_profile ORDER BY child_id')] == before_children
        assert [tuple(row) for row in connection.execute('SELECT * FROM kg_node ORDER BY node_id')] == before_nodes
        assert [tuple(row) for row in connection.execute('SELECT * FROM kg_edge ORDER BY edge_id')] == before_edges


def test_precise_curated_skill_outweighs_generic_atomic_labels_but_not_distinct_skills(tmp_path):
    database = Database(tmp_path / 'app.db')
    database.initialize()
    StudyRepository(database).seed_family()
    CatalogGraphBootstrapper(database).run(rebuild_local_candidates=False)
    build = tmp_path / 'atomic.db'
    _fixture_build(build)
    with closing(sqlite3.connect(build)) as connection:
        nodes = [('solve', '说明解方程'), ('formula', '运用公式法'), ('equation', '说明一元二次方程')]
        connection.executemany('''INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,'初中7-9','数学','concept','{}','',
            '{}','[]','[]','[]',.8,'unreviewed')''', [(key, key, title) for key, title in nodes])
        connection.commit()
    matcher = MistakeGraphMatcher(database, build)
    for question in ('用一元二次方程求根公式在实数范围内解方程 x²+2x+3=0，并解释为什么。',
                     '用公式法求根，在实数范围内解 x²−6x+9=0，并说明不同实数根的个数。'):
        for stored_subject in ('数学', '', '其他'):
            result = matcher.match('child-junior-3', _source(question, stored_subject, '手动录入错题'))
            assert result['status'] == 'matched'
            assert result['subject'] == '数学'
            assert result['primary']['key'] == 'starter:J3.MATH.QUADRATIC.FORMULA'
            assert result['prerequisites'][0]['key'] == 'starter:J3.MATH.QUADRATIC.DISCRIMINANT'
    mixed = matcher.match('child-junior-3', _source('一元二次方程求根公式；根的判别式。'))
    assert mixed['status'] == 'ambiguous'
    assert mixed['primary'] is None


def test_blank_subject_uses_strong_original_source_signals_only(tmp_path):
    app, build = _fixture(tmp_path)
    matcher = MistakeGraphMatcher(Database(app), build)
    for subject, question in (
        ('数学', '求 x²=9 的所有解。'), ('数学', '算一算 3/4÷2/5。'), ('物理', '用欧姆定律说明。'),
        ('化学', '写出反应的化学方程式。'), ('生物', '为什么植物进行光合作用需要光照？'),
        ('地理', '解释地球的自转和昼夜更替。'), ('历史', '辛亥革命的影响是什么？'),
        ('英语', '【英语】完成第三题'), ('道德与法治', '科目：道法，完成第三题'),
    ):
        result = matcher.match('child-private', _source(question, ''))
        assert result['subject'] == subject
        assert result['status'] == 'no_match'
    for question in ('解释光合作用与地球的自转。', '【语文】用欧姆定律解答。', '解释第三题。'):
        result = matcher.match('child-private', _source(question, '', original_answer='欧姆定律'), hints=['光合作用'])
        assert result['status'] == 'no_match'
        assert 'subject' not in result
    assert matcher.match('child-private', _source('解释第三题。', '道法'))['subject'] == '道德与法治'


def test_version_can_observe_uncommitted_local_review_within_same_connection(tmp_path):
    app, build = _fixture(tmp_path)
    database = Database(app)
    matcher = MistakeGraphMatcher(database, build)
    first = matcher.cache_version()
    with database.connect() as connection:
        assert matcher.cache_version(connection=connection) == first
        connection.execute('BEGIN IMMEDIATE')
        connection.execute("UPDATE kg_atomic_review_decision SET action='rejected' WHERE entity_key='decimal'")
        assert matcher.cache_version(connection=connection) != first
        connection.rollback()
    assert matcher.cache_version() == first


def test_short_yes_no_question_surfaces_exact_topic_without_claiming_equivalence(tmp_path):
    app, build = _fixture(tmp_path)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('zero-divisor', '说明一个数不能除以零'),
            ('zero-context', '比较除以零和乘以零的区别'),
            ('zero-property', '说明零的性质'),
        ]:
            connection.execute('''INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,'小学3-4','数学','concept','{}','',
                '{}','[]','[]','[]',.9,'unreviewed')''', (key, key, title))
    matcher = MistakeGraphMatcher(Database(app), build)
    result = matcher.match('child-private', _source('除以零可以吗', title='除以零可以吗'))
    assert result['status'] == 'ambiguous'
    assert result['primary'] is None
    assert 'zero-divisor' in {item['key'] for item in result['alternatives']}
    assert matcher.match('child-private', _source('除以零可以吗', title='除以零可以吗'),
                         hints=['零的性质'])['status'] != 'matched'
    exact = matcher.match('child-private', _source('一个数不能除以零'))
    assert exact['status'] == 'matched'
    assert exact['primary']['key'] == 'zero-divisor'


def test_comparison_does_not_match_only_one_side_as_the_whole_topic(tmp_path):
    app, build = _fixture(tmp_path)
    matcher = MistakeGraphMatcher(Database(app), build)
    result = matcher.match('child-private', _source('比较小数的意义和十进制位值的不同'))
    assert result['status'] != 'matched'
    assert result['primary'] is None
    titled = matcher.match('child-private', _source('比较小数的意义和十进制位值的不同', title='小数的意义'))
    assert titled['status'] != 'matched'
    assert titled['primary'] is None


def test_source_qualifiers_keep_broad_geometry_node_from_false_match(tmp_path):
    app, build = _fixture(tmp_path)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('triangle-basic', '说明三角形的面积'),
            ('triangle-ratio', '推断等高三角形面积比与底边比的关系'),
        ]:
            connection.execute('''INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,'小学3-4','数学','concept','{}','',
                '{}','[]','[]','[]',.9,'unreviewed')''', (key, key, title))
    matcher = MistakeGraphMatcher(Database(app), build)
    basic = matcher.match('child-private', _source('三角形的面积怎么求'))
    assert basic['status'] == 'matched' and basic['primary']['key'] == 'triangle-basic'
    given = matcher.match('child-private', _source('已知三角形底是3高是4，面积是多少'))
    assert given['status'] == 'matched' and given['primary']['key'] == 'triangle-basic'
    query = '等高两个三角形的面积比是多少'
    qualified = matcher.match('child-private', _source(query, title=query))
    assert qualified['status'] == 'ambiguous' and qualified['primary'] is None
    assert qualified['alternatives'][0]['key'] == 'triangle-ratio'
    assert 'triangle-basic' in {x['key'] for x in qualified['alternatives']}
    split = matcher.match('child-private', _source('等高的两个三角形，面积比是多少', title='三角形面积'))
    assert split['status'] != 'matched' and split['primary'] is None


def test_source_grounded_atomic_candidates_are_visible_but_not_proven(tmp_path):
    app, build = _fixture(tmp_path)
    with sqlite3.connect(build) as connection:
        for key, subject, title in [
            ('math-sign', '数学', '判断不等式两边乘除负数时不等号方向改变'),
            ('physics-friction', '物理', '说明滑动摩擦力方向与接触面之间的相对滑动方向相反'),
            ('chem-iron', '化学', '写出铁与硫酸铜溶液反应的化学方程式Fe+CuSO₄=FeSO₄+Cu'),
            ('english-relative', '英语', '识别定语从句关系代词which指代事物'),
        ]:
            connection.execute('INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', subject, 'concept', '{}', '', '{}', '[]', '[]', '[]', .9, 'unreviewed'))
    matcher = MistakeGraphMatcher(Database(app), build)
    for subject, query, key in [
        ('数学', '不等式两边乘负数方向变吗', 'math-sign'),
        ('物理', '滑动摩擦力方向与相对运动有什么关系', 'physics-friction'),
        ('化学', '铁和硫酸铜反应生成什么', 'chem-iron'),
        ('英语', 'which引导的定语从句', 'english-relative'),
    ]:
        result = matcher.match('child-private', _source(query, subject))
        assert result['status'] == 'ambiguous', (subject, result)
        assert result['primary'] is None
        assert key in {node['key'] for node in result['alternatives']}
    # Model-only labels and a single broad shared word cannot establish a match.
    assert matcher.match('child-private', _source('完全无关的问题', '化学'), hints=['铁和硫酸铜'])['primary'] is None


def test_english_partial_word_and_truncated_title_are_not_trusted(tmp_path):
    app, build = _fixture(tmp_path)
    with sqlite3.connect(build) as connection:
        for key, title in [('ever-list', '说明ever, never, every day, now and then'),
                           ('unfinished', '说明when引导时间状语从句，若')]:
            connection.execute('INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '英语', 'concept', '{}', '', '{}', '[]', '[]', '[]', .9, 'unreviewed'))
    matcher = MistakeGraphMatcher(Database(app), build)
    for query in ('He goes to school every day', 'when引导的时间状语从句'):
        result = matcher.match('child-private', _source(query, '英语'))
        assert result['status'] != 'matched', (query, result)
        assert result['primary'] is None
    assert not _source_overlap_supports_candidate(
        'I have lived here since 2020', 'She said that she had made progress since last year')


def test_semantic_export_requires_verified_manual_label_and_local_evidence(tmp_path):
    app, build = _fixture(tmp_path)
    with sqlite3.connect(build) as connection:
        connection.execute("ALTER TABLE kg_atomic_definition_draft ADD COLUMN source_method TEXT")
        connection.execute("ALTER TABLE kg_atomic_definition_draft ADD COLUMN prompt_version TEXT")
        connection.execute('CREATE TABLE kg_resource(resource_id TEXT PRIMARY KEY, review_status TEXT)')
        connection.execute('CREATE TABLE kg_atomic_draft_evidence(draft_id TEXT,resource_id TEXT,exact_quote TEXT)')
        connection.execute('CREATE TABLE kg_atomic_label_cloud_right(draft_id TEXT PRIMARY KEY,title_sha256 TEXT,origin TEXT,status TEXT,basis TEXT)')
        connection.execute("INSERT INTO kg_resource VALUES('local-source','restricted')")
        for key, title, source_method, quote in [
            ('safe', '判断不等式两边乘除负数时不等号方向改变', 'curated', '教材给出了不等式性质和一个练习'),
            ('copy', '说明不等式两边都乘以同一个负数方向改变', 'curated', '不等式两边都乘以同一个负数方向改变'),
            ('unknown', '判断不等式与方向的关系', 'model', '相关来源未给出')]:
            connection.execute('INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key,key,title,'小学3-4','数学','concept','{}','','{}','[]','[]','[]',.9,'unreviewed',source_method,'manual-test'))
            connection.execute('INSERT INTO kg_atomic_draft_evidence VALUES(?,?,?)', (key, 'local-source', quote))
            if key != 'unknown':
                connection.execute('INSERT INTO kg_atomic_label_cloud_right VALUES(?,?,?,?,?)',
                    (key,hashlib.sha256(title.encode()).hexdigest(),'project_authored','active','synthetic-authored'))
    matcher = MistakeGraphMatcher(Database(app), build)
    def graph(keys):
        with sqlite3.connect(build) as connection:
            return {'status':'ambiguous', 'candidates_truncated':False, 'alternatives':[{'key':k,'title':connection.execute('select title from kg_atomic_definition_draft where draft_id=?',(k,)).fetchone()[0]} for k in keys]}
    options, mapping = matcher.semantic_options(graph(['safe']))
    assert options == [{'ref':'C1','label':'判断不等式两边乘除负数时不等号方向改变'}] and mapping == {'C1':'safe'}
    for keys in (['copy'], ['unknown'], ['safe','copy']):
        assert matcher.semantic_options(graph(keys)) == ([], {})
    assert matcher.semantic_options(graph(['safe']) | {'candidates_truncated': True}) == ([], {})
    with sqlite3.connect(build) as connection:
        connection.execute("UPDATE kg_atomic_label_cloud_right SET title_sha256='stale' WHERE draft_id='safe'")
    assert matcher.semantic_options(graph(['safe'])) == ([], {})
