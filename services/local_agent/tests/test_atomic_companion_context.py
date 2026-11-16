from __future__ import annotations

import sqlite3
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "src/home_study_agent/knowledge_graph/atomic_companion.py"
SPEC = importlib.util.spec_from_file_location("atomic_companion_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
AtomicGraphCompanionService = MODULE.AtomicGraphCompanionService


def _fixture_build(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE kg_atomic_definition_draft (
              draft_id TEXT PRIMARY KEY, canonical_signature TEXT, title TEXT, grade_band TEXT,
              subject TEXT, atom_kind TEXT, boundary_json TEXT, observable_outcome TEXT,
              mastery_criteria_json TEXT, diagnostic_blueprint_json TEXT,
              misconception_hypotheses_json TEXT, standard_alignments_json TEXT,
              confidence REAL, review_status TEXT
            );
            CREATE TABLE kg_atomic_candidate (
              canonical_signature TEXT, unit_path TEXT, source_ref TEXT,
              evidence_text TEXT, confidence REAL
            );
            CREATE TABLE kg_atomic_relation_candidate (
              relation_id TEXT PRIMARY KEY, source_key TEXT, target_key TEXT,
              relation_type TEXT, rationale TEXT, confidence REAL, review_status TEXT
            );
            """
        )
        diagnostics = '[{"level":"recognition","task_family":"识别"},{"level":"independent","task_family":"独立作答"},{"level":"transfer","task_family":"迁移"}]'
        rows = [
            ("d1", "decimal", "理解小数的意义", "小学3-4", "数学", "concept_identification", "{}", "能解释小数的意义", '{}', diagnostics, '[]', '[]', .91, "unreviewed"),
            ("d2", "fraction", "理解分数与小数的关系", "小学3-4", "数学", "concept_discrimination", "{}", "能比较分数和小数", '{}', diagnostics, '[]', '[]', .83, "unreviewed"),
            ("d3", "noise", "小数错误噪声", "小学3-4", "数学", "concept_identification", "{}", "噪声", '{}', diagnostics, '[]', '[]', .99, "unreviewed"),
            ("d4", "senior", "高中小数表示", "高中10-12", "数学", "concept_identification", "{}", "越级内容", '{}', diagnostics, '[]', '[]', .99, "unreviewed"),
            ("d5", "truncated", "理解小数的意", "小学3-4", "数学", "concept_identification", "{}", "截断内容", '{}', diagnostics, '[]', '[]', .92, "unreviewed"),
        ]
        connection.executemany("INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        connection.executemany(
            "INSERT INTO kg_atomic_candidate VALUES(?,?,?,?,?)",
            [
                ("decimal", "四年级数学/小数", "local:mindmap:decimal", "小数表示十分之几", .9),
                ("fraction", "四年级数学/分数", "local:mindmap:fraction", "分数可以化成小数", .8),
                ("noise", "四年级数学/噪声", "local:mindmap:noise", "OCR noise", .9),
                ("senior", "高中数学", "local:mindmap:senior", "不应返回", .9),
                ("truncated", "四年级数学/小数", "local:mindmap:truncated", "截断内容", .92),
            ],
        )
        connection.execute(
            "INSERT INTO kg_atomic_relation_candidate VALUES(?,?,?,?,?,?,?)",
            ("r1", "decimal", "fraction", "prerequisite_of", "先理解小数", .88, "unreviewed"),
        )


def _fixture_app(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE child_profile(child_id TEXT PRIMARY KEY,nickname TEXT,grade TEXT,
              curriculum_region TEXT,status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE kg_atomic_review_decision(entity_type TEXT,entity_key TEXT,action TEXT,
              reviewer TEXT,note TEXT,payload_json TEXT,client_action_id TEXT,updated_at TEXT,
              PRIMARY KEY(entity_type,entity_key));
            """
        )
        connection.execute(
            "INSERT INTO child_profile VALUES('child-private','同学','四年级','江苏','active','','')"
        )
        connection.executemany(
            "INSERT INTO kg_atomic_review_decision VALUES(?,?,?,?,?,?,?,?)",
            [
                ("draft", "decimal", "approved", "parent", "", "{}", "review-1", ""),
                ("draft", "noise", "rejected", "parent", "", "{}", "review-2", ""),
            ],
        )


def test_short_query_finds_particle_variant_without_matching_one_part(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title in [('cone-volume', '计算圆锥的体积'), ('cone-surface', '认识圆锥的表面')]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '数学', 'procedure_execution', '{}', title,
                 '{}', '[]', '[]', '[]', .9, 'unreviewed'),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='圆锥体积', subject='数学')
    assert [node['key'] for node in result['nodes']] == ['cone-volume']


def test_short_query_can_match_title_and_its_concept_tag(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        connection.execute("ALTER TABLE kg_atomic_definition_draft ADD COLUMN concept_tags_json TEXT DEFAULT '[]'")
        connection.execute(
            'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('travel', 'travel', '估算家庭旅游各项支出', '小学3-4', '数学', 'procedure_execution',
             '{}', '估算费用', '{}', '[]', '[]', '[]', .9, 'unreviewed', '["预算"]'),
        )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='旅游预算', subject='数学')
    assert [node['key'] for node in result['nodes']] == ['travel']


def test_question_suffix_does_not_hide_relevant_short_topic(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        connection.execute(
            'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('zero-divisor', 'zero-divisor', '一个数不能除以零', '小学3-4', '数学',
             'concept_discrimination', '{}', '识别零不能作除数', '{}', '[]', '[]', '[]', .9, 'unreviewed'),
        )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='除以零可以吗', subject='数学')
    assert [node['key'] for node in result['nodes']] == ['zero-divisor']


def test_english_query_matches_punctuation_in_concept_title(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        connection.execute(
            'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('comparison', 'comparison', '运用as...as进行同级比较', '小学3-4', '英语',
             'language_use', '{}', '使用同级比较', '{}', '[]', '[]', '[]', .9, 'unreviewed'),
        )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='as as和not as as怎么用', subject='英语')
    assert [node['key'] for node in result['nodes']] == ['comparison']


def test_short_query_matches_reproduction_terms_without_tag_only_topic_promotion(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        connection.execute("ALTER TABLE kg_atomic_definition_draft ADD COLUMN concept_tags_json TEXT DEFAULT '[]'")
        for key, title, tags, confidence in [
            ('fungi', '说出真菌主要通过产生孢子进行繁殖的特征', '[]', .9),
            ('actino', '说出放线菌的生殖方式', '["真菌", "生殖"]', .9),
            ('bacteria', '计算细菌分裂繁殖后的数量变化', '[]', .8),
            ('food', '说出食品变质与细菌生长和繁殖的关系', '[]', .99),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '生物', 'concept_identification', '{}', title,
                 '{}', '[]', '[]', '[]', confidence, 'unreviewed', tags),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='真菌如何生殖', subject='生物')
    assert result['nodes'][0]['key'] == 'fungi'
    result = service.context_for_child(child_id='child-private', query='细菌如何生殖', subject='生物')
    assert result['nodes'][0]['key'] == 'bacteria'


def test_query_topic_precedes_a_later_mention_in_an_unrelated_title(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('opposites', '计算互为相反数的两有理数之和'),
            ('difference', '识别平方差公式的结构特征：两个二项式中一项相同另一项互为相反数'),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '数学', 'procedure_execution', '{}', title,
                 '{}', '[]', '[]', '[]', .95, 'unreviewed'),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='互为相反数', subject='数学')
    matched = [node['key'] for node in result['nodes'] if node['graph_role'] == 'matched']
    assert matched.index('opposites') < matched.index('difference')


def test_spoken_questions_rank_specific_topic_above_partial_or_generic_matches(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title, confidence in [
            ('circle', '计算圆的面积（基于半径或直径）', .95),
            ('sector', '计算扇形或部分圆的面积（基于圆心角比例）', .95),
            ('pi-generic', '说明圆周率', .32),
            ('pi-definition', '说出圆周率是圆的周长与直径的比值', .95),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '数学', 'procedure_execution', '{}', title,
                 '{}', '[]', '[]', '[]', confidence, 'unreviewed'),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    circle = service.context_for_child(child_id='child-private', query='圆的面积怎么计算', subject='数学')
    pi = service.context_for_child(child_id='child-private', query='圆周率是什么', subject='数学')
    assert circle['nodes'][0]['key'] == 'circle'
    assert pi['nodes'][0]['key'] == 'pi-definition'


def test_topic_subject_is_ranked_above_a_shared_generic_suffix(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('bacteria', '区分多数细菌的寄生与腐生营养来源'),
            ('fungi', '解释真菌因缺乏叶绿体而必须利用现成有机物的异养营养方式'),
            ('food', '说明营养方式'),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '生物', 'concept_identification', '{}', title,
                 '{}', '[]', '[]', '[]', .9, 'unreviewed'),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    for query in ('细菌营养方式', '解释细菌营养方式'):
        result = service.context_for_child(child_id='child-private', query=query, subject='生物')
        assert result['nodes'][0]['key'] == 'bacteria'


def test_question_intent_beats_broad_labels_and_prioritizes_written_formulas(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title, confidence in [
            ('rust', '说明铁锈', .99),
            ('rust-products', '说出铁锈生成产物的组成', .8),
            ('lever', '说明杠杆', .99),
            ('lever-balance', '识别杠杆的平衡条件', .8),
            ('speed-compare', '判断速度公式中各个量是否一致', .99),
            ('speed-formula', '写出速度公式 v=s/t', .8),
            ('density-formula', '写出密度公式 ρ=m/v', .99),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '物理', 'concept_identification', '{}', title,
                 '{}', '[]', '[]', '[]', confidence, 'unreviewed'),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    for query, expected in [
        ('铁锈产生什么', 'rust-products'),
        ('杠杆有什么平衡条件', 'lever-balance'),
        ('速度公式', 'speed-formula'),
    ]:
        result = service.context_for_child(child_id='child-private', query=query, subject='物理')
        assert result['nodes'][0]['key'] == expected


def test_known_topic_without_requested_aspect_does_not_fall_back_to_another_subject(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('virus', '说出病毒可以引起人类传染病'),
            ('insects', '说出昆虫的受精方式和生殖方式'),
            ('birds', '说出鸟类的受精方式和生殖方式'),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '生物', 'concept_identification', '{}', title,
                 '{}', '[]', '[]', '[]', .9, 'unreviewed'),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    missing = service.context_for_child(child_id='child-private', query='病毒的繁殖方式', subject='生物')
    assert missing['nodes'] == []
    assert missing['recovery']['state'] == 'no_matching_atomic_context'
    unknown = service.context_for_child(child_id='child-private', query='月球的繁殖方式', subject='生物')
    assert any(node['key'] in {'insects', 'birds'} for node in unknown['nodes'])


def test_curated_draft_evidence_fallback_is_scoped_and_preserves_candidate_evidence(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        connection.executescript('''
            CREATE TABLE kg_resource(resource_id TEXT PRIMARY KEY,source_ref TEXT,review_status TEXT);
            CREATE TABLE kg_atomic_draft_evidence(evidence_id TEXT PRIMARY KEY,draft_id TEXT,
              resource_id TEXT,exact_quote TEXT,confidence REAL);
        ''')
        connection.execute("UPDATE kg_atomic_definition_draft SET title='理解分数的意义' WHERE draft_id='d2'")
        connection.execute("UPDATE kg_atomic_candidate SET source_ref=NULL,evidence_text=NULL WHERE canonical_signature='fraction'")
        connection.executemany('INSERT INTO kg_resource VALUES(?,?,?)', [
            ('good', 'textbook:fractions', 'restricted'),
            ('rejected', 'textbook:rejected', 'rejected'),
            ('other', 'textbook:other', 'approved'),
        ])
        connection.executemany('INSERT INTO kg_atomic_draft_evidence VALUES(?,?,?,?,?)', [
            ('f1', 'd2', 'good', '分数的真实教材证据', .8),
            ('f2', 'd2', 'rejected', '已驳回来源', .99),
            ('d1-fallback', 'd1', 'other', '候选已有来源时不能混入', 1.0),
            ('other-draft', 'd3', 'other', '其他节点证据', 1.0),
        ])
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='小数', subject='数学', limit=3)
    nodes = {node['key']: node for node in result['nodes']}
    assert nodes['fraction']['graph_role'] == 'one_hop_neighbor'
    assert nodes['fraction']['evidence'] == {'quote': '分数的真实教材证据', 'source_ref': 'textbook:fractions'}
    assert nodes['decimal']['evidence']['source_ref'] == 'local:mindmap:decimal'
    assert nodes['decimal']['evidence']['quote'] == '小数表示十分之几'


def test_incomplete_legacy_evidence_tables_leave_empty_evidence(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    app = tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        connection.execute('CREATE TABLE kg_resource(resource_id TEXT PRIMARY KEY)')
        connection.execute('CREATE TABLE kg_atomic_draft_evidence(draft_id TEXT,resource_id TEXT)')
        connection.execute("UPDATE kg_atomic_candidate SET source_ref=NULL,evidence_text=NULL WHERE canonical_signature='decimal'")
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='小数', subject='数学')
    decimal = next(node for node in result['nodes'] if node['key'] == 'decimal')
    assert decimal['evidence'] == {'quote': '', 'source_ref': None}


def test_companion_context_is_scoped_directed_truthful_and_persistent(tmp_path: Path) -> None:
    build = tmp_path / "atomic.db"
    app = tmp_path / "application.db"
    _fixture_build(build)
    _fixture_app(app)
    before = build.read_bytes()
    raw_query = "孩子不理解小数和分数"

    service = AtomicGraphCompanionService(app, build)
    context = service.context_for_child(
        child_id="child-private", query=raw_query, subject="数学", limit=3
    )

    assert {node["key"] for node in context["nodes"]} == {"decimal", "fraction"}
    assert all(node["grade_band"] == "小学3-4" for node in context["nodes"])
    decimal = next(node for node in context["nodes"] if node["key"] == "decimal")
    fraction = next(node for node in context["nodes"] if node["key"] == "fraction")
    assert decimal["usage_policy"]["mode"] == "reviewed_context"
    assert fraction["usage_policy"]["mode"] == "hypothesis_only"
    assert context["relations"][0]["source_key"] == "decimal"
    assert context["relations"][0]["target_key"] == "fraction"
    assert context["relations"][0]["direction"] == "source_to_target"
    assert context["weakness_hypotheses"][0]["status"] == "question_required"
    assert context["truthfulness"]["mastery_was_updated"] is False
    assert context["truthfulness"]["cloud_transmission_allowed"] is False
    assert build.read_bytes() == before

    reopened = AtomicGraphCompanionService(app, build)
    reopened.context_for_child(child_id="child-private", query="小数", subject="数学", limit=2)
    with sqlite3.connect(app) as connection:
        events = connection.execute(
            "SELECT child_fingerprint,query_fingerprint,selected_keys_json FROM kg_companion_graph_context_event"
        ).fetchall()
    assert len(events) == 2
    persisted = " ".join(str(value) for row in events for value in row)
    assert "child-private" not in persisted
    assert raw_query not in persisted
    assert service.usage_summary()["calls"] == 2


def test_companion_empty_match_has_recovery_truth_and_audit(tmp_path: Path) -> None:
    build = tmp_path / "atomic.db"
    app = tmp_path / "application.db"
    _fixture_build(build)
    _fixture_app(app)

    context = AtomicGraphCompanionService(app, build).context_for_child(
        child_id="child-private", query="光合作用", subject="数学", limit=2
    )
    assert context["nodes"] == []
    assert context["relations"] == []
    assert context["recovery"]["state"] == "no_matching_atomic_context"
    assert context["truthfulness"]["mastery_was_updated"] is False
    assert context["audit_event_id"]


def test_one_character_subject_with_connector_requires_both_participants(tmp_path: Path) -> None:
    build, app = tmp_path / 'atomic.db', tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('target', '识别铁在氧气中燃烧生成四氧化三铁'),
            ('rust', '说明铁在潮湿空气中与氧气反应'),
            ('acid', '识别铁和稀硫酸反应的产物'),
            ('other', '说明镁和氧气反应的产物'),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '化学', 'concept_identification', '{}', title,
                 '{}', '[]', '[]', '[]', .95, 'unreviewed'),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    for query in ('铁和氧气反应产物', '铁与氧气反应产物'):
        result = service.context_for_child(child_id='child-private', query=query, subject='化学')
        assert result['nodes'][0]['key'] == 'target'


def test_mixed_language_query_prioritizes_exact_english_words_over_generic_grammar(tmp_path: Path) -> None:
    build, app = tmp_path / 'atomic.db', tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('relative', '识别定语从句关系代词which指代事物'),
            ('relative-comparison', '区分as与which引导非限制性定语从句区别'),
            ('time', '运用since引导的时间状语从句中主句使用现在完成时的规则'),
            ('condition', '运用if引导条件状语从句'),
            ('partial-word', '辨析different引导的条件状语从句'),
            ('that-relative', '识别限定性定语从句关系代词that可指人或事物'),
            ('that-object', '识别that引导的宾语从句的结构'),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '英语', 'concept_identification', '{}', title,
                 '{}', '[]', '[]', '[]', .95, 'unreviewed'),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    assert service.context_for_child(child_id='child-private', query='which引导的定语从句', subject='英语')['nodes'][0]['key'] == 'relative'
    assert service.context_for_child(child_id='child-private', query='as与which引导的定语从句区别', subject='英语')['nodes'][0]['key'] == 'relative-comparison'
    assert service.context_for_child(child_id='child-private', query='if引导的条件状语从句', subject='英语')['nodes'][0]['key'] == 'condition'
    assert service.context_for_child(child_id='child-private', query='that引导的定语从句', subject='英语')['nodes'][0]['key'] == 'that-relative'


def test_sign_conditions_do_not_confuse_positive_negative_zero_or_unknown(tmp_path: Path) -> None:
    build, app = tmp_path / 'atomic.db', tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('positive', '判断不等式两边乘除正数时不等号方向不变'),
            ('negative', '判断不等式两边乘除负数时不等号方向改变'),
            ('zero', '说明不等式两边乘以零结果相等'),
            ('unknown', '判断不等式乘数符号未知时需要分类讨论'),
            ('both', '比较不等式两边乘正数和负数时的不等号方向'),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '数学', 'concept_identification', '{}', title,
                 '{}', '[]', '[]', '[]', .95, 'unreviewed'),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    cases = [
        ('不等式两边乘正数方向变吗', 'positive'),
        ('不等式两边乘负数方向变吗', 'negative'),
        ('不等式两边乘零会怎样', 'zero'),
        ('不等式乘数符号未知怎么办', 'unknown'),
        ('不等式乘正数和负数方向有何不同', 'both'),
    ]
    for query, expected in cases:
        result = service.context_for_child(child_id='child-private', query=query, subject='数学')
        assert result['nodes'][0]['key'] == expected, query
    assert service._sign_conditions('负担太重，正好可以休息') == frozenset()
    assert service._sign_conditions('物体以0.5米每秒运动') == frozenset()
    assert service._sign_conditions('分母不为0') == frozenset()
    assert service._sign_conditions('分母!=0') == frozenset()
    assert service._sign_conditions('乘以零') == frozenset({'zero'})


def test_unknown_sign_does_not_return_single_sign_as_established_fact(tmp_path: Path) -> None:
    build, app = tmp_path / 'atomic.db', tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('positive', '判断不等式乘正数时符号不变'),
            ('negative', '判断不等式乘负数时符号改变'),
            ('unrelated-zero', '说明分母为0而分母不等于0'),
            ('unrelated-sign', '说明立方根的正数负数与不等于零'),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '数学', 'concept_identification', '{}', title,
                 '{}', '[]', '[]', '[]', .95, 'unreviewed'),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='不等式乘数符号未知', subject='数学')
    assert result['nodes'] == []
    zero = service.context_for_child(child_id='child-private', query='不等式两边乘零会怎样', subject='数学')
    assert zero['nodes'] == []


def test_non_math_chinese_words_do_not_trigger_sign_filter(tmp_path: Path) -> None:
    build, app = tmp_path / 'atomic.db', tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        connection.execute(
            'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('language', 'language', '说明负担和正好这两个词语的意思', '小学3-4', '语文', 'concept',
             '{}', '', '{}', '[]', '[]', '[]', .95, 'unreviewed'),
        )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='负担和正好是什么意思', subject='语文')
    assert result['nodes'][0]['key'] == 'language'


def test_composite_second_subject_is_not_reduced_to_shared_prefix(tmp_path: Path) -> None:
    build, app = tmp_path / 'atomic.db', tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('copper-sulfate', '识别铁与硫酸铜溶液发生置换反应'),
            ('acid', '识别铁与稀硫酸反应的产物'),
            ('oxygen', '识别铁在氧气中反应的产物'),
            ('hydrogen', '判断铁在氢气中的反应条件'),
            ('rectangle', '比较甲和长方形的面积关系'),
            ('cuboid', '比较甲和长方体的面积关系'),
            ('unrelated-list', '说明硫酸锌、氯化钠、铁的性质和反应'),
            ('different-first-object', '说明氢气与氧化钙的反应'),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '化学', 'concept', '{}', '',
                 '{}', '[]', '[]', '[]', .95, 'unreviewed'),
            )
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    for query, expected in [
        ('铁和硫酸铜反应生成什么', 'copper-sulfate'),
        ('铁和氧气反应生成什么', 'oxygen'),
        ('铁和氢气反应条件', 'hydrogen'),
        ('甲和长方形的面积关系', 'rectangle'),
    ]:
        result = service.context_for_child(child_id='child-private', query=query, subject='化学')
        assert result['nodes'][0]['key'] == expected, query
    missing = service.context_for_child(child_id='child-private', query='铁和硫酸锌反应生成什么', subject='化学')
    assert missing['nodes'] == []
    missing_first = service.context_for_child(child_id='child-private', query='钙和氢气反应生成什么', subject='化学')
    assert missing_first['nodes'] == []


def test_english_sentence_keeps_separate_words_for_retrieval(tmp_path: Path) -> None:
    build, app = tmp_path / 'atomic.db', tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('since-time', '识别since引导的时间状语与现在完成时的关系'),
            ('before-time', '识别before引导时间状语从句'),
        ]:
            connection.execute('INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '小学3-4', '英语', 'concept', '{}', '', '{}', '[]', '[]', '[]', .9, 'unreviewed'))
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    assert 'since' in service._query_fragments('I have lived here since 2020')
    result = service.context_for_child(child_id='child-private', query='I have lived here since 2020', subject='英语')
    assert result['nodes'][0]['key'] == 'since-time'


def test_reviewed_evidence_replaces_polluted_source_and_never_crosses_nodes(tmp_path: Path) -> None:
    import hashlib
    build, app = tmp_path / 'atomic.db', tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    quote = '核实的该节点教材依据'
    with sqlite3.connect(build) as connection:
        connection.executescript('''
            CREATE TABLE kg_resource(resource_id TEXT PRIMARY KEY,source_ref TEXT,review_status TEXT);
            CREATE TABLE kg_segment(segment_id TEXT PRIMARY KEY,resource_id TEXT,text TEXT);
            CREATE TABLE kg_atomic_draft_evidence(evidence_id TEXT PRIMARY KEY,draft_id TEXT,resource_id TEXT,
                segment_id TEXT,exact_quote TEXT,quote_sha256 TEXT,confidence REAL);
            CREATE TABLE kg_atomic_evidence_review(draft_id TEXT,evidence_kind TEXT,evidence_id TEXT,action TEXT,
                reviewer TEXT,note TEXT,updated_at TEXT,PRIMARY KEY(draft_id,evidence_kind,evidence_id));
        ''')
        connection.executemany('INSERT INTO kg_resource VALUES(?,?,?)', [
            ('book', 'local:textbook:page', 'restricted'), ('bad', 'local:video', 'unreviewed')])
        connection.executemany('INSERT INTO kg_segment VALUES(?,?,?)', [
            ('good-seg', 'book', '前文'+quote+'后文'), ('bad-seg','bad','点赞订阅广告')])
        connection.executemany('INSERT INTO kg_atomic_draft_evidence VALUES(?,?,?,?,?,?,?)', [
            ('good', 'd1', 'book', 'good-seg', quote, hashlib.sha256(quote.encode()).hexdigest(), .8),
            ('bad-evi', 'd1', 'bad', 'bad-seg', '点赞订阅广告', hashlib.sha256('点赞订阅广告'.encode()).hexdigest(), .99),
        ])
        connection.executemany('INSERT INTO kg_atomic_evidence_review VALUES(?,?,?,?,?,?,?)', [
            ('d1','draft_evidence','good','approved','reviewer','教材核实','now'),
            ('d1','draft_evidence','bad-evi','rejected','reviewer','广告','now'),
            ('d2','draft_evidence','good','approved','reviewer','错误引用另一个节点','now'),
        ])
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='分数与小数的关系', subject='数学')
    nodes = {node['key']:node for node in result['nodes']}
    assert nodes['decimal']['evidence'] == {'quote':quote,'source_ref':'local:textbook:page'}
    assert nodes['decimal']['evidence_review_status'] == 'source_checked'
    assert nodes['fraction']['evidence'] == {'quote':'','source_ref':None}
    assert nodes['fraction']['evidence_review_status'] == 'unavailable'
    with sqlite3.connect(build) as connection:
        connection.execute("UPDATE kg_resource SET review_status='rejected' WHERE resource_id='book'")
    invalid = service.context_for_child(child_id='child-private', query='小数的意义', subject='数学')['nodes'][0]
    assert invalid['evidence'] == {'quote':'','source_ref':None}
    assert invalid['evidence_review_status'] == 'unavailable'
    with sqlite3.connect(build) as connection:
        connection.execute("UPDATE kg_resource SET review_status='restricted' WHERE resource_id='book'")
        connection.execute("UPDATE kg_atomic_draft_evidence SET quote_sha256='incorrect' WHERE evidence_id='good'")
    changed = service.context_for_child(child_id='child-private', query='小数的意义', subject='数学')['nodes'][0]
    assert changed['evidence'] == {'quote':'','source_ref':None}
    assert changed['evidence_review_status'] == 'unavailable'


def test_legacy_evidence_is_explicitly_unverified(tmp_path: Path) -> None:
    build, app = tmp_path / 'atomic.db', tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    node = service.context_for_child(child_id='child-private', query='小数的意义', subject='数学')['nodes'][0]
    assert node['evidence']['quote'] == '小数表示十分之几'
    assert node['evidence_review_status'] == 'unverified'
    with sqlite3.connect(build) as connection:
        connection.execute('''CREATE TABLE kg_atomic_evidence_review(draft_id TEXT,evidence_kind TEXT,
            evidence_id TEXT,action TEXT,reviewer TEXT,note TEXT,updated_at TEXT)''')
    node = service.context_for_child(child_id='child-private', query='小数的意义', subject='数学')['nodes'][0]
    assert node['evidence']['quote'] == '小数表示十分之几'
    assert node['evidence_review_status'] == 'unverified'


def test_reviewed_candidate_evidence_requires_its_own_node_and_resource(tmp_path: Path) -> None:
    build, app = tmp_path / 'atomic.db', tmp_path / 'application.db'
    _fixture_build(build)
    _fixture_app(app)
    with sqlite3.connect(build) as connection:
        connection.executescript('''
            ALTER TABLE kg_atomic_candidate ADD COLUMN candidate_id TEXT;
            ALTER TABLE kg_atomic_candidate ADD COLUMN resource_id TEXT;
            ALTER TABLE kg_atomic_candidate ADD COLUMN segment_id TEXT;
            CREATE TABLE kg_resource(resource_id TEXT PRIMARY KEY,source_ref TEXT,review_status TEXT);
            CREATE TABLE kg_segment(segment_id TEXT PRIMARY KEY,resource_id TEXT,text TEXT);
            CREATE TABLE kg_atomic_evidence_review(draft_id TEXT,evidence_kind TEXT,evidence_id TEXT,action TEXT,
                reviewer TEXT,note TEXT,updated_at TEXT,PRIMARY KEY(draft_id,evidence_kind,evidence_id));
            INSERT INTO kg_resource VALUES('book','local:book','restricted');
            INSERT INTO kg_segment VALUES('segment','book','教材小数表示十分之几。');
            UPDATE kg_atomic_candidate SET candidate_id='approved-candidate',resource_id='book',segment_id='segment',source_ref='local:book:page'
                WHERE canonical_signature='decimal';
            INSERT INTO kg_atomic_evidence_review VALUES('d1','candidate','approved-candidate','approved','reviewer','核对','now');
            INSERT INTO kg_atomic_evidence_review VALUES('d2','candidate','approved-candidate','approved','reviewer','错误串用','now');
        ''')
    service = AtomicGraphCompanionService(app, build, audit_usage=False)
    result = service.context_for_child(child_id='child-private', query='分数与小数的关系', subject='数学')
    nodes = {node['key']:node for node in result['nodes']}
    assert nodes['decimal']['evidence']['quote'] == '小数表示十分之几'
    assert nodes['decimal']['evidence_review_status'] == 'source_checked'
    assert nodes['fraction']['evidence'] == {'quote':'','source_ref':None}
    assert nodes['fraction']['evidence_review_status'] == 'unavailable'
