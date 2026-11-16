from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.knowledge_graph.atomic_companion import (
    AtomicGraphCompanionService,
    AtomicGraphUnavailableError,
)
from home_study_agent.learning_rubric import evaluation_source_boundary
from home_study_agent.model_gateway import ModelResult


def _settings(tmp_path: Path, *, agent_runtime_enabled: bool = False) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "app.db",
        data_encryption_key=b"0" * 32,
        agent_runtime_enabled=agent_runtime_enabled,
        maintenance_enabled=False,
    )


def _atomic_build(path: Path) -> None:
    diagnostics = json.dumps([
        {"level": "recognition", "task_family": "识别"},
        {"level": "independent", "task_family": "独立作答"},
        {"level": "transfer", "task_family": "迁移"},
    ], ensure_ascii=False)
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
        connection.executemany(
            "INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("d1", "decimal-meaning", "理解小数的意义", "小学3-4", "数学", "concept", "{}",
                 "能用位值解释小数", "{}", diagnostics, "[]", "[]", .92, "unreviewed"),
                ("d2", "place-value", "十进制位值前置判断", "小学3-4", "数学", "prerequisite", "{}",
                 "能说明位值", "{}", diagnostics, "[]", "[]", .88, "unreviewed"),
            ],
        )
        connection.executemany(
            "INSERT INTO kg_atomic_candidate VALUES(?,?,?,?,?)",
            [
                ("decimal-meaning", "四年级数学/小数", "local:decimal", "DO_NOT_CLOUD_EVIDENCE_7391", .92),
                ("place-value", "四年级数学/位值", "local:place-value", "LOCAL_ONLY_SOURCE_5520", .88),
            ],
        )
        connection.execute(
            "INSERT INTO kg_atomic_relation_candidate VALUES(?,?,?,?,?,?,?)",
            ("LOCAL_RELATION_991", "place-value", "decimal-meaning", "prerequisite_of", "位值是前置", .9, "unreviewed"),
        )


def _install_fixture_companion(application, build: Path) -> None:
    application.state.agent_orchestrator.atomic_companion = AtomicGraphCompanionService(
        application.state.settings.database_path, build
    )


class PlannerGateway:
    configured = True

    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResult(
            content=json.dumps({
                "objective": {
                    "mode": "new", "type": "learning", "summary": "理解小数的意义",
                    "success_criteria": ["能用自己的话说明", "完成一道迁移检查"], "phase": "diagnose",
                },
                "next_step": {
                    "capability": "learning.explain",
                    "arguments": {"question": "数学里的小数为什么这样表示？", "subject": "数学", "support_level": 4},
                    "reason": "孩子正在询问学习问题", "expected_evidence": ["child_check_response"],
                },
                "spoken_transition": "我们先讲清小数的表示。",
                "requires_child_reply": True, "confidence": .96,
                "utterance_binding": {"role": "question", "quote": json.loads(request.messages[-1]["content"])["child_utterance"]},
            }, ensure_ascii=False),
            provider="planner-provider", model="planner-v1", latency_ms=8,
            prompt_tokens=40, completion_tokens=40,
        )


class UnavailableCompanion:
    def context_for_child(self, **_kwargs):
        raise AtomicGraphUnavailableError("构建库不可用")


def test_learning_question_persists_local_atomic_audit_and_never_updates_mastery(tmp_path: Path) -> None:
    build = tmp_path / "atomic.db"
    _atomic_build(build)
    settings = _settings(tmp_path)
    application = create_app(settings)
    payload = {
        "transcript": "数学里的小数为什么这样表示？",
        "client_action_id": "atomic-agent-turn-001",
    }
    with TestClient(application) as client:
        _install_fixture_companion(application, build)
        session = client.post(
            "/v1/sessions",
            json={"child_id": "child-primary-4", "client_action_id": "atomic-session-001"},
        ).json()
        with sqlite3.connect(settings.database_path) as connection:
            mastery_before = connection.execute(
                "SELECT * FROM kg_mastery_state ORDER BY child_id,node_id"
            ).fetchall()
        first = client.post(f"/v1/sessions/{session['session_id']}/agent-turn", json=payload)

    assert first.status_code == 200
    action = first.json()["action"]
    local_context = action["local_learning_context"]
    assert action["type"] == "ask_tutor"
    assert local_context["status"] == "matched"
    assert local_context["cloud_transmission_allowed"] is False
    assert local_context["mastery_update_allowed"] is False
    assert local_context["approved_node_count"] == 0
    assert "十进制位值前置判断" in local_context["diagnostic_prompt"]
    assert not ({"nodes", "relations", "evidence", "source_ref"} & set(local_context))

    with sqlite3.connect(settings.database_path) as connection:
        mastery_after = connection.execute(
            "SELECT * FROM kg_mastery_state ORDER BY child_id,node_id"
        ).fetchall()
        event = connection.execute(
            "SELECT event_id FROM kg_companion_graph_context_event"
        ).fetchone()
    assert mastery_after == mastery_before
    assert event[0] == local_context["audit_event_id"]

    restarted = create_app(settings)
    with TestClient(restarted) as client:
        replay = client.post(f"/v1/sessions/{session['session_id']}/agent-turn", json=payload)
    assert replay.json()["agent_turn_id"] == first.json()["agent_turn_id"]
    assert replay.json()["action"]["local_learning_context"]["audit_event_id"] == event[0]
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM kg_companion_graph_context_event").fetchone()[0] == 1


def test_cloud_question_planning_keeps_atomic_context_local_and_never_transmitted(tmp_path: Path) -> None:
    build = tmp_path / "atomic.db"
    _atomic_build(build)
    gateway = PlannerGateway()
    settings = _settings(tmp_path, agent_runtime_enabled=True)
    application = create_app(settings, model_gateway=gateway)
    with TestClient(application) as client:
        _install_fixture_companion(application, build)
        session = client.post(
            "/v1/sessions",
            json={"child_id": "child-primary-4", "client_action_id": "atomic-cloud-session-001"},
        ).json()
        turn = client.post(
            f"/v1/sessions/{session['session_id']}/agent-turn",
            json={"transcript": "数学里的小数为什么这样表示？", "client_action_id": "atomic-cloud-turn-001"},
        )

    assert turn.status_code == 200
    assert turn.json()["action"]["local_learning_context"]["audit_event_id"]
    assert turn.json()["intent"] == "learning.explain"
    assert turn.json()["action"]["capability"] == "learning.explain"
    assert turn.json()["action"]["source"] == "agent_runtime_planner"
    assert turn.json()["action"]["subject"] == "数学"
    assert len(gateway.requests) == 1
    transmitted = json.dumps([
        {"messages": request.messages, "manifest": request.context_manifest}
        for request in gateway.requests
    ], ensure_ascii=False)
    for forbidden in (
        "DO_NOT_CLOUD_EVIDENCE_7391", "LOCAL_ONLY_SOURCE_5520", "LOCAL_RELATION_991",
        "十进制位值前置判断", "atomic-context-",
    ):
        assert forbidden not in transmitted
    with application.state.repository.database.connect() as connection:
        objective_id = connection.execute(
            "SELECT objective_id FROM agent_plan WHERE agent_turn_id=?",
            (turn.json()["agent_turn_id"],),
        ).fetchone()[0]
        boundary = evaluation_source_boundary(
            connection, "child-primary-4", objective_id=objective_id
        )
        assert not boundary["cloud_evaluator_blocked"], boundary

        action = json.loads(connection.execute(
            "SELECT action_json FROM agent_turn WHERE agent_turn_id=?",
            (turn.json()["agent_turn_id"],),
        ).fetchone()[0])
        action["local_document_context"] = {
            "local_only": True, "cloud_transmission_allowed": False,
            "text": "PRIVATE_DOCUMENT_TEXT",
        }
        connection.execute(
            "UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?",
            (json.dumps(action, ensure_ascii=False), turn.json()["agent_turn_id"]),
        )
        blocked = evaluation_source_boundary(
            connection, "child-primary-4", objective_id=objective_id
        )
        assert blocked["cloud_evaluator_blocked"]
        assert "objective_contains_local_only_action" in blocked["reasons"]


def test_atomic_context_failure_does_not_block_the_tutor_journey(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    application = create_app(settings)
    with TestClient(application) as client:
        application.state.agent_orchestrator.atomic_companion = UnavailableCompanion()
        session = client.post(
            "/v1/sessions",
            json={"child_id": "child-primary-4", "client_action_id": "atomic-recovery-session-001"},
        ).json()
        turn = client.post(
            f"/v1/sessions/{session['session_id']}/agent-turn",
            json={"transcript": "数学里的小数是什么意思？", "client_action_id": "atomic-recovery-turn-001"},
        )

    assert turn.status_code == 200
    assert turn.json()["action"]["type"] == "ask_tutor"
    context = turn.json()["action"]["local_learning_context"]
    assert context.pop("course_context")["status"] == "no_match"
    assert context == {
        "status": "unavailable", "cloud_transmission_allowed": False,
        "mastery_update_allowed": False, "audit_event_id": None,
        "matched_node_count": 0, "approved_node_count": 0,
        "hypothesis_count": 0, "diagnostic_prompt": None,
    }


def test_explicit_physics_question_cannot_be_relabelled_by_cloud_planner(tmp_path: Path) -> None:
    gateway = PlannerGateway()
    settings = _settings(tmp_path, agent_runtime_enabled=True)
    application = create_app(settings, model_gateway=gateway)
    with TestClient(application) as client:
        application.state.agent_orchestrator.atomic_companion = UnavailableCompanion()
        session = client.post(
            "/v1/sessions",
            json={"child_id": "child-junior-3", "client_action_id": "physics-lock-session-001"},
        ).json()
        turn = client.post(
            f"/v1/sessions/{session['session_id']}/agent-turn",
            json={
                "transcript": "物理里浮力和排开液体体积是什么关系，为什么？",
                "client_action_id": "physics-lock-turn-001",
            },
        )

    assert turn.status_code == 200
    assert turn.json()["intent"] == "cloud_understanding_unavailable"
    assert turn.json()["action"]["type"] == "none"
    assert turn.json()["action"]["source"] == "cloud_planner_unavailable"
    assert len(gateway.requests) == 1


def test_long_procedural_atomic_hypothesis_is_not_spoken_to_child() -> None:
    prompt = (
        "先检查前置知识“说明常数项移到右边；方程两边都加上一次项系数的一半的平方”是否掌握。"
    )
    result = AgentOrchestrator._deterministic_atomic_follow_up([
        {"status": "question_required", "prompt": prompt},
    ])
    assert result is None


def test_long_atomic_title_is_shortened_to_a_child_speakable_concept() -> None:
    result = AgentOrchestrator._deterministic_atomic_follow_up([
        {
            "status": "question_required",
            "prompt": "先检查前置知识“选择改变物体浮沉的方法：改变浮力或改变重力”是否掌握。",
        },
    ])
    assert result == "讲完后我再确认一个小点：你能用自己的话说说“选择改变物体浮沉的方法”吗？"


def test_restricted_fallback_excerpt_stays_local_in_real_agent_turn(tmp_path: Path) -> None:
    build = tmp_path / 'atomic.db'
    _atomic_build(build)
    with sqlite3.connect(build) as connection:
        connection.executescript('''
            CREATE TABLE kg_resource(resource_id TEXT PRIMARY KEY,source_ref TEXT,review_status TEXT);
            CREATE TABLE kg_atomic_draft_evidence(evidence_id TEXT PRIMARY KEY,draft_id TEXT,
              resource_id TEXT,exact_quote TEXT,confidence REAL);
        ''')
        connection.execute("UPDATE kg_atomic_candidate SET source_ref=NULL,evidence_text=NULL WHERE canonical_signature='decimal-meaning'")
        connection.execute("INSERT INTO kg_resource VALUES('r1','local:restricted-source-714','restricted')")
        connection.execute("INSERT INTO kg_atomic_draft_evidence VALUES('e1','d1','r1','LOCAL_ONLY_FALLBACK_EVIDENCE_714',.9)")
    gateway = PlannerGateway()
    settings = _settings(tmp_path, agent_runtime_enabled=True)
    application = create_app(settings, model_gateway=gateway)
    with TestClient(application) as client:
        _install_fixture_companion(application, build)
        session = client.post('/v1/sessions', json={
            'child_id': 'child-primary-4', 'client_action_id': 'fallback-sentinel-session',
        }).json()
        local = application.state.agent_orchestrator.atomic_companion.context_for_child(
            child_id='child-primary-4', query='小数', subject='数学', limit=6,
        )
        assert any(node['evidence']['quote'] == 'LOCAL_ONLY_FALLBACK_EVIDENCE_714' for node in local['nodes'])
        response = client.post(f"/v1/sessions/{session['session_id']}/agent-turn", json={
            'transcript': '数学里的小数为什么这样表示？', 'client_action_id': 'fallback-sentinel-turn',
        })
    assert response.status_code == 200
    assert len(gateway.requests) == 1
    transmitted = json.dumps([
        {'messages': request.messages, 'manifest': request.context_manifest} for request in gateway.requests
    ], ensure_ascii=False)
    assert 'LOCAL_ONLY_FALLBACK_EVIDENCE_714' not in transmitted
    assert 'local:restricted-source-714' not in transmitted


def test_spoken_english_grammar_without_subject_reaches_real_graph(tmp_path: Path) -> None:
    build = tmp_path / 'spoken-grammar.db'
    _atomic_build(build)
    with sqlite3.connect(build) as connection:
        for key, title in [
            ('which-relative', '识别定语从句关系代词which指代事物'),
            ('since-time', '运用since引导的时间状语从句中主句使用现在完成时的规则'),
        ]:
            connection.execute(
                'INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, title, '初中7-9', '英语', 'concept', '{}', title,
                 '{}', '[]', '[]', '[]', .95, 'unreviewed'),
            )

    class SpokenPlanner(PlannerGateway):
        async def complete(self, request):
            self.requests.append(request)
            utterance = json.loads(request.messages[-1]['content'])['child_utterance']
            return ModelResult(content=json.dumps({
                'objective': {'mode': 'new', 'type': 'learning', 'summary': '语法问题',
                              'success_criteria': ['理解并应用'], 'phase': 'diagnose'},
                'next_step': {'capability': 'learning.explain',
                              'arguments': {'question': utterance, 'subject': '其他', 'support_level': 4},
                              'reason': '孩子请求讲解', 'expected_evidence': ['child_check_response']},
                'spoken_transition': '我们来看看。', 'requires_child_reply': True, 'confidence': .96,
                'utterance_binding': {'role': 'question', 'quote': utterance},
            }, ensure_ascii=False), provider='synthetic-planner', model='synthetic', latency_ms=1,
               prompt_tokens=1, completion_tokens=1)

    class RecordingCompanion(AtomicGraphCompanionService):
        calls = []
        def context_for_child(self, **kwargs):
            result = super().context_for_child(**kwargs)
            self.calls.append((kwargs, result))
            return result

    gateway = SpokenPlanner()
    settings = _settings(tmp_path, agent_runtime_enabled=True)
    application = create_app(settings, model_gateway=gateway)
    with TestClient(application) as client:
        companion = RecordingCompanion(settings.database_path, build)
        application.state.agent_orchestrator.atomic_companion = companion
        session = client.post('/v1/sessions', json={
            'child_id': 'child-junior-3', 'client_action_id': 'english-spoken-session-1',
        }).json()
        response = client.post(f"/v1/sessions/{session['session_id']}/agent-turn", json={
            'transcript': '请给我讲讲which引导的定语从句', 'client_action_id': 'english-spoken-turn-1',
        })
    assert response.status_code == 200
    assert response.json()['action']['type'] == 'ask_tutor'
    assert response.json()['action']['subject'] == '英语'
    assert companion.calls
    args, graph = companion.calls[-1]
    assert args['subject'] == '英语'
    assert graph['nodes'][0]['key'] == 'which-relative'


def test_language_subject_detection_preserves_explicit_and_math_context() -> None:
    subject = AgentOrchestrator._subject_from_text
    assert subject('请讲讲when引导的时间状语从句') == '英语'
    assert subject('the短语怎么用') == '英语'
    assert subject('数学里sin函数怎么用') == '数学'
    assert subject('语文里which作为代词') == '语文'
    assert subject('这个字母ABC是什么意思') == '其他'
