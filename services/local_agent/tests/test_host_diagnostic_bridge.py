from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.host_diagnostic_bridge import HostDiagnosticKnowledgeGraphBridge
from home_study_agent.knowledge_graph.application_service import KnowledgeGraphApplicationService
from home_study_agent.knowledge_graph.quality import GraphQualityService
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.repository import utc_now


@pytest.fixture
def host_flow(tmp_path, monkeypatch):
    from home_study_agent import learning_state, mistake_book, repository
    from home_study_agent.knowledge_graph import service
    from home_study_agent.mastery import review_planning

    clock = [(datetime.now(timezone.utc) - timedelta(days=4)).isoformat()]
    for module in (learning_state, mistake_book, repository, service, review_planning):
        monkeypatch.setattr(module, 'utc_now', lambda: clock[0])
    db = Database(tmp_path / 'host-replay.db')
    db.initialize()
    with db.connect() as connection:
        for child in ('child', 'other'):
            connection.execute("""INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
                VALUES(?,?,'四年级','江苏南京','active',?,?)""", (child, child, clock[0], clock[0]))
            connection.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
                VALUES(?,?,?,'study',?,?,?)""", (child, child, 'active' if child == 'child' else 'completed', clock[0], clock[0], child))
        connection.commit()
    learning = LearningStateService(db)
    learning.seed_catalog()
    graph = KnowledgeGraphService(db)
    root = tmp_path / 'materials'
    root.mkdir()
    (root / 'arithmetic.txt').write_text('整数运算：先括号，再乘除。', encoding='utf-8')
    spec = CorpusSpec('host-replay', '运算讲解', 'local', root, None, 'local', 'parent_owned')
    ingestor = LocalCorpusIngestor(db)
    ingestor.run(spec, ingestor.start(spec, idempotency_key='host-replay-material'))
    with db.connect() as connection:
        resource = connection.execute('SELECT resource_id FROM kg_resource').fetchone()[0]
        segment = connection.execute('SELECT segment_id FROM kg_segment').fetchone()[0]
    nodes = []
    for name in ('target', 'prerequisite', 'earlier-prerequisite', 'unrelated'):
        node = graph.upsert_node(canonical_key=name, node_type='knowledge_point', title=name,
            scope={'grade': '四年级'}, source_ref='product_evidence:host-test', review_status='approved')
        graph.map_segment(resource, segment, node, 'explains', confidence=1,
                          source_ref='parent-approved', review_status='approved')
        nodes.append(node)
    graph.link(nodes[1], nodes[0], 'prerequisite_of', rationale='first prerequisite',
               source_ref='product_evidence:host-test', review_status='approved')
    graph.link(nodes[2], nodes[1], 'prerequisite_of', rationale='second prerequisite',
               source_ref='product_evidence:host-test', review_status='approved')
    exercises = {}
    for item in ('diag-p4-arithmetic-closed-1', 'diag-p4-arithmetic-closed-2'):
        exercise = graph.upsert_node(canonical_key=f'diagnostic:{item}', node_type='exercise', title=item,
            source_ref='product_evidence:host-test', review_status='approved')
        graph.link(nodes[0], exercise, 'assessed_by', rationale='exact scored exercise',
                   source_ref='product_evidence:host-test', review_status='approved')
        exercises[item] = exercise
    return SimpleNamespace(db=db, graph=graph, learning=learning, clock=clock, nodes=nodes,
                           exercises=exercises, bridge=HostDiagnosticKnowledgeGraphBridge(db))


def host_answer(flow, *, item='diag-p4-arithmetic-closed-1', answer='12', child='child', review=None):
    with flow.db.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed' WHERE child_id!=?", (child,))
        connection.execute("UPDATE daily_session SET status='active' WHERE session_id=?", (child,))
        connection.commit()
    return flow.learning.submit_diagnostic(child, item, answer, str(uuid4()), review)


def withdraw_host(flow, attempt, child='child'):
    mistakes = MistakeBookService(flow.db)
    case = mistakes.capture(child_id=child, knowledge_node_id='node-p4-math-arithmetic', subject='math',
        title='整数运算', prompt_summary='核对运算顺序', submitted_answer='12', source_type='diagnostic',
        source_ref=attempt['attempt_id'], confidence=1, client_action_id=str(uuid4()),
        diagnostic_item_id=attempt['diagnostic_item_id'], source_evidence_id=attempt['evidence_id'])
    mistakes.confirm(case['mistake_id'], child, 'exclude', str(uuid4()))


def generic_event(flow, node=None, *, child='child', score=1., independent=True, confidence=1., key=None):
    return flow.graph.record_evidence(child_id=child, node_id=node or flow.nodes[0], correctness=score,
        independent=independent, confidence=confidence, origin_type='teacher_observation', idempotency_key=key or str(uuid4()))


def legacy_host_event(flow, attempt, *, prefix='exercise:host-diagnostic:', mistake=None, confidence=1.):
    # Model a pre-upgrade event, retaining the real writer's historical propagation graph.
    key = str(uuid4())
    flow.graph.record_evidence(child_id='child', node_id=flow.nodes[0], correctness=attempt['correctness'],
        independent=True, confidence=confidence, origin_type='mistake' if mistake else 'exercise',
        origin_id=mistake or flow.exercises[attempt['diagnostic_item_id']], idempotency_key=key)
    with flow.db.connect() as connection:
        connection.execute('UPDATE kg_mastery_evidence SET idempotency_key=? WHERE idempotency_key=?',
                           (prefix + attempt['attempt_id'], key))
        connection.commit()


def test_keyword_objective_never_enters_host_kg_and_legacy_pollution_is_hidden(host_flow):
    f = host_flow
    with f.db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET expected_answer_json=? WHERE diagnostic_item_id=?",
                           (json.dumps({'keywords': ['90']}), 'diag-p4-arithmetic-closed-1'))
        connection.commit()
    attempt = host_answer(f, answer='90')
    assert f.bridge.record_attempt(attempt['attempt_id'])['status'] == 'manual_confirmation_required'
    legacy_host_event(f, attempt, confidence=.35)
    assert f.graph.mastery('child', f.nodes[0])['evidence_count'] == 0
    assert f.graph.mastery('child', f.nodes[0])['mastery_probability'] == .5
    with f.db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM kg_mastery_evidence').fetchone()[0] == 1


def test_host_duplicate_exposure_is_rejected_at_write_and_read(host_flow):
    f = host_flow
    first = host_answer(f, answer='90')
    assert f.bridge.record_attempt(first['attempt_id'])['status'] == 'recorded'
    repeated = host_answer(f, answer='90')
    assert f.bridge.record_attempt(repeated['attempt_id'])['status'] == 'manual_confirmation_required'
    legacy_host_event(f, repeated)
    assert f.graph.mastery('child', f.nodes[0])['evidence_count'] == 1


def test_mixed_history_replay_removes_only_host_and_restores_two_level_propagation(host_flow):
    f = host_flow
    for node in f.nodes[:3]:
        generic_event(f, node)
    other = generic_event(f, child='other', score=.3, confidence=.6)
    attempt = host_answer(f)
    f.bridge.record_attempt(attempt['attempt_id'])
    assert [f.graph.mastery('child', n)['evidence_count'] for n in f.nodes[:3]] == [2, 2, 2]
    # Old propagation survives graph edits, but must not spread to a new prerequisite.
    with f.db.connect() as connection:
        connection.execute("DELETE FROM kg_edge WHERE relation_type='prerequisite_of'")
        connection.commit()
    f.graph.link(f.nodes[3], f.nodes[0], 'prerequisite_of', rationale='new graph',
                 source_ref='product_evidence:changed', review_status='approved')
    assert f.graph.mastery('child', f.nodes[1])['evidence_count'] == 2
    assert f.graph.mastery('child', f.nodes[3])['evidence_count'] == 0
    generic_event(f, independent=False, confidence=.8)
    withdraw_host(f, attempt)
    graph = KnowledgeGraphService(Database(f.db.path))
    root = graph.mastery('child', f.nodes[0])
    assert root['evidence_count'] == 2
    assert root['mastery_probability'] == pytest.approx(.61 + .096 * (1 - .61))
    assert root['weakness_signal'] == 0
    for node in f.nodes[1:3]:
        assert graph.mastery('child', node)['mastery_probability'] == pytest.approx(.61)
        assert graph.mastery('child', node)['evidence_count'] == 1
    assert graph.mastery('other', f.nodes[0]) == other
    with f.db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM kg_mastery_evidence').fetchone()[0] == 6
        assert connection.execute("SELECT count(*) FROM kg_edge WHERE relation_type='generated_from'").fetchone()[0] >= 8


@pytest.mark.parametrize('legacy', [False, True])
def test_withdrawal_invalidates_latest_and_old_action_without_erasing_plan(host_flow, legacy):
    f = host_flow
    attempt = host_answer(f)
    f.bridge.record_attempt(attempt['attempt_id'])
    facade = KnowledgeGraphApplicationService(f.db)
    plan = facade.review_plan(child_id='child', action_id='old-plan')
    assert plan['source_evidence_count'] == 1
    if legacy:
        plan.pop('host_evidence_fingerprint')
        with f.db.connect() as connection:
            connection.execute('UPDATE kg_review_plan SET plan_json=?', (json.dumps(plan),))
            connection.commit()
    withdraw_host(f, attempt)
    reopened = KnowledgeGraphApplicationService(Database(f.db.path))
    assert reopened.latest_review_plan('child') is None
    with pytest.raises(ValueError, match='regenerate'):
        reopened.review_plan(child_id='child', action_id='old-plan')
    fresh = reopened.review_plan(child_id='child', action_id='fresh-plan')
    assert fresh['source_evidence_count'] == 0 and fresh['items'] == []
    with f.db.connect() as connection:
        saved = connection.execute("SELECT status,plan_json FROM kg_review_plan WHERE idempotency_key='old-plan'").fetchone()
        assert saved['status'] == 'superseded'
        assert json.loads(saved['plan_json']) == plan
        assert connection.execute('SELECT count(*) FROM kg_mastery_evidence').fetchone()[0] == 1


def test_both_historical_host_keys_are_one_observation_and_mistake_replay_is_compatible(host_flow):
    f = host_flow
    attempt = host_answer(f)
    mistake = MistakeBookService(f.db).capture_from_attempt(attempt)
    legacy_host_event(f, attempt)
    legacy_host_event(f, attempt, prefix='host-diagnostic:', mistake=mistake['mistake_id'])
    assert f.graph.mastery('child', f.nodes[0])['evidence_count'] == 1
    for kwargs in ({}, {'mistake_id': mistake['mistake_id']}):
        assert f.bridge.record_attempt(attempt['attempt_id'], **kwargs)['mastery']['evidence_count'] == 1
    withdraw_host(f, attempt)
    assert f.graph.mastery('child', f.nodes[0])['evidence_count'] == 0
    with f.db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM kg_mastery_evidence').fetchone()[0] == 2


@pytest.mark.parametrize('mutation', ['missing', 'other_child', 'source_changed'])
def test_host_source_revalidated_after_restart(host_flow, mutation):
    f = host_flow
    attempt = host_answer(f)
    f.bridge.record_attempt(attempt['attempt_id'])
    with f.db.connect() as connection:
        if mutation == 'missing':
            connection.execute('DELETE FROM diagnostic_attempt WHERE attempt_id=?', (attempt['attempt_id'],))
        elif mutation == 'other_child':
            connection.execute("UPDATE diagnostic_attempt SET child_id='other' WHERE attempt_id=?", (attempt['attempt_id'],))
        else:
            connection.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (attempt['diagnostic_item_id'],))
        connection.commit()
    reopened = KnowledgeGraphService(Database(f.db.path))
    assert all(reopened.mastery('child', n)['evidence_count'] == 0 for n in f.nodes[:3])


def test_source_withdrawal_removes_delayed_descendant_but_not_other_valid_receipt(host_flow):
    f = host_flow
    source = host_answer(f)
    f.bridge.record_attempt(source['attempt_id'])
    with f.db.connect() as connection:
        review = connection.execute('SELECT * FROM review_task WHERE source_evidence_id=? ORDER BY due_at LIMIT 1', (source['evidence_id'],)).fetchone()
    f.clock[0] = (datetime.fromisoformat(f.clock[0]) + timedelta(days=3)).isoformat()
    answers = {'diag-p4-arithmetic-closed-1': '90', 'diag-p4-arithmetic-closed-2': '42'}
    descendant = host_answer(f, item=review['diagnostic_item_id'], answer=answers[review['diagnostic_item_id']], review=review['review_task_id'])
    assert f.bridge.record_attempt(descendant['attempt_id'])['status'] == 'recorded'
    independent_item = next(item for item in answers if item != review['diagnostic_item_id'])
    independent = host_answer(f, item=independent_item, answer=answers[independent_item])
    assert f.bridge.record_attempt(independent['attempt_id'])['status'] == 'recorded'
    withdraw_host(f, source)
    assert f.graph.mastery('child', f.nodes[0])['evidence_count'] == 1
    assert f.bridge.record_attempt(descendant['attempt_id'])['status'] == 'manual_confirmation_required'


def test_concurrent_host_write_and_plan_replay_are_idempotent(host_flow):
    f = host_flow
    attempt = host_answer(f)
    with ThreadPoolExecutor(2) as pool:
        states = list(pool.map(lambda _: HostDiagnosticKnowledgeGraphBridge(Database(f.db.path)).record_attempt(attempt['attempt_id']), range(2)))
    assert all(row['mastery']['evidence_count'] == 1 for row in states)
    with ThreadPoolExecutor(2) as pool:
        plans = list(pool.map(lambda _: KnowledgeGraphApplicationService(Database(f.db.path)).review_plan(child_id='child', action_id='concurrent'), range(2)))
    assert plans[0] == plans[1]
    with f.db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM kg_mastery_evidence').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM kg_review_plan').fetchone()[0] == 1


def test_non_host_api_and_plan_replay_keep_existing_semantics(host_flow):
    f = host_flow
    state = generic_event(f, score=.35, independent=False, confidence=.35, key='generic')
    assert state['evidence_count'] == 1
    assert state['mastery_probability'] == pytest.approx(.5 + .12 * .35 * (.35 - .5))
    assert generic_event(f, score=.35, independent=False, confidence=.35, key='generic') == state
    facade = KnowledgeGraphApplicationService(f.db)
    plan = facade.review_plan(child_id='child', action_id='generic-plan')
    assert 'host_evidence_fingerprint' not in plan
    generic_event(f)
    assert facade.review_plan(child_id='child', action_id='generic-plan') == plan


def test_other_child_host_withdrawal_does_not_invalidate_plan(host_flow):
    f = host_flow
    first = host_answer(f)
    other = host_answer(f, child='other')
    f.bridge.record_attempt(first['attempt_id'])
    f.bridge.record_attempt(other['attempt_id'])
    facade = KnowledgeGraphApplicationService(f.db)
    plan = facade.review_plan(child_id='child', action_id='owned-plan')
    withdraw_host(f, other, 'other')
    assert facade.latest_review_plan('child') == plan
    assert f.graph.mastery('other', f.nodes[0])['evidence_count'] == 0


def test_bridge_rechecks_withdrawal_inside_kg_write_transaction(host_flow, monkeypatch):
    f = host_flow
    attempt = host_answer(f)
    original = f.bridge.facade.record_exercise_result

    def withdraw_before_write(**kwargs):
        withdraw_host(f, attempt)
        return original(**kwargs)

    monkeypatch.setattr(f.bridge.facade, 'record_exercise_result', withdraw_before_write)
    assert f.bridge.record_attempt(attempt['attempt_id'])['status'] == 'manual_confirmation_required'
    with f.db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM kg_mastery_evidence').fetchone()[0] == 0


def test_plan_generation_detects_withdrawal_between_selection_and_commit(host_flow, monkeypatch):
    f = host_flow
    attempt = host_answer(f)
    f.bridge.record_attempt(attempt['attempt_id'])
    plans = KnowledgeGraphApplicationService(f.db).review_plans
    original = plans._item
    changed = []

    def withdraw_during_plan(*args):
        if not changed:
            withdraw_host(f, attempt)
            changed.append(True)
        return original(*args)

    monkeypatch.setattr(plans, '_item', withdraw_during_plan)
    with pytest.raises(ValueError, match='changed during planning'):
        plans.generate(child_id='child', idempotency_key='racing-plan')
    with f.db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM kg_review_plan').fetchone()[0] == 0


def test_new_process_replays_withdrawal_and_refuses_old_plan(host_flow):
    from home_study_agent.knowledge_graph import service

    f = host_flow
    attempt = host_answer(f)
    f.bridge.record_attempt(attempt['attempt_id'])
    KnowledgeGraphApplicationService(f.db).review_plan(child_id='child', action_id='before-restart')
    withdraw_host(f, attempt)
    script = """
import json, sys
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.application_service import KnowledgeGraphApplicationService
facade = KnowledgeGraphApplicationService(Database(Path(sys.argv[1])))
counts = [facade.graph.mastery('child', node)['evidence_count'] for node in sys.argv[2:]]
latest = facade.latest_review_plan('child')
try:
    facade.review_plan(child_id='child', action_id='before-restart')
except ValueError:
    refused = True
else:
    refused = False
print(json.dumps(dict(counts=counts, latest=latest, refused=refused)))
"""
    env = dict(os.environ, PYTHONPATH=str(Path(service.__file__).resolve().parents[2]))
    result = subprocess.check_output([sys.executable, '-B', '-c', script, str(f.db.path), *f.nodes[:3]],
                                     env=env, text=True, timeout=20)
    assert json.loads(result) == {'counts': [0, 0, 0], 'latest': None, 'refused': True}


def test_host_scored_attempt_is_the_only_path_that_updates_kg_mastery(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child','学生','四年级','江苏南京','active',?,?)", (utc_now(), utc_now()))
        connection.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('study','child','active','study',?,?,?)", (utc_now(), utc_now(), 'host-study-session'))
        connection.commit()
    learning = LearningStateService(database); learning.seed_catalog()
    root = tmp_path / "approved"; root.mkdir(); (root / "lesson.txt").write_text("判别式讲解", encoding="utf-8")
    spec = CorpusSpec("approved", "获批讲解", "local", root, None, "local", "parent_owned")
    LocalCorpusIngestor(database).run(spec, LocalCorpusIngestor(database).start(spec, idempotency_key="approved-source"))
    graph = KnowledgeGraphService(database)
    node = graph.upsert_node(canonical_key="bridge-discriminant", node_type="knowledge_point", title="根的判别式", scope={"grade":"四年级","subject":"数学"}, source_ref="product_evidence:bridge", review_status="approved")
    exercise = graph.upsert_node(canonical_key="diagnostic:diag-p4-arithmetic-closed-1", node_type="exercise", title="真实宿主诊断题", source_ref="product_evidence:bridge", review_status="approved")
    graph.link(node, exercise, "assessed_by", rationale="宿主判分题", source_ref="product_evidence:bridge", review_status="approved")
    with database.connect() as connection:
        resource = connection.execute("SELECT resource_id FROM kg_resource").fetchone()[0]
        segment = connection.execute("SELECT segment_id FROM kg_segment").fetchone()[0]
    graph.map_segment(resource, segment, node, "explains", confidence=.9, source_ref="parent-approved", review_status="approved")
    attempt = learning.submit_diagnostic("study", "diag-p4-arithmetic-closed-1", "90", "host-scored-answer-001")
    recorded = HostDiagnosticKnowledgeGraphBridge(database).record_attempt(attempt["attempt_id"])
    assert recorded["status"] == "recorded" and recorded["mastery"]["evidence_count"] == 1
    # Response-loss/restart replay is keyed to the immutable host attempt.
    reopened = HostDiagnosticKnowledgeGraphBridge(Database(tmp_path / "study.db")).record_attempt(attempt["attempt_id"])
    assert reopened["mastery"]["evidence_count"] == 1
    # An incorrect host attempt plus its host-created mistake case remains one
    # observation: the mistake is the durable origin, not a second penalty.
    second_exercise = graph.upsert_node(canonical_key="diagnostic:diag-p4-arithmetic-closed-2", node_type="exercise", title="另一道精确题", source_ref="product_evidence:bridge", review_status="approved")
    graph.link(node, second_exercise, "assessed_by", rationale="宿主精确题", source_ref="product_evidence:bridge", review_status="approved")
    wrong = learning.submit_diagnostic("study", "diag-p4-arithmetic-closed-2", "12", "host-scored-answer-002")
    host_mistake = MistakeBookService(database).capture_from_attempt(wrong)
    failed = HostDiagnosticKnowledgeGraphBridge(database).record_attempt(wrong["attempt_id"], mistake_id=host_mistake["mistake_id"])
    assert failed["status"] == "recorded" and failed["mistake_recorded"] is True and failed["mastery"]["evidence_count"] == 2
    replay_failed = HostDiagnosticKnowledgeGraphBridge(Database(tmp_path / "study.db")).record_attempt(wrong["attempt_id"], mistake_id=host_mistake["mistake_id"])
    assert replay_failed["mastery"]["evidence_count"] == 2
    with database.connect() as connection:
        evidence = connection.execute("SELECT origin_type,origin_id FROM kg_mastery_evidence WHERE idempotency_key=?", (f"host-diagnostic:{wrong['attempt_id']}",)).fetchone()
    assert tuple(evidence) == ('mistake', host_mistake['mistake_id'])
    # One locally scored exercise must resolve to exactly one target atom.
    ambiguous = graph.upsert_node(canonical_key="bridge-ambiguous", node_type="knowledge_point", title="错误关联点", scope={"grade":"四年级"}, source_ref="product_evidence:bridge", review_status="approved")
    graph.link(ambiguous, exercise, "assessed_by", rationale="错误多重关联", source_ref="product_evidence:bridge-2", review_status="approved")
    assert HostDiagnosticKnowledgeGraphBridge(database).record_attempt(attempt["attempt_id"])["status"] == "manual_confirmation_required"
    # PET reference rubric is explicitly not calibrated, even if its host
    # service produces a numerical heuristic; it cannot become mastery evidence.
    with database.connect() as connection:
        connection.execute("DELETE FROM kg_edge WHERE source_node_id=? AND target_node_id=?", (ambiguous, exercise))
        connection.execute("UPDATE diagnostic_item SET assessment_mode='pet_reference_rubric' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-1'")
        connection.commit()
    assert HostDiagnosticKnowledgeGraphBridge(database).record_attempt(attempt["attempt_id"])["status"] == "manual_confirmation_required"


def test_host_mistake_only_exposes_parent_approved_resource_guidance_across_restart_and_undo(tmp_path: Path) -> None:
    """A representative local mistake cannot turn corpus candidates into child material.

    The three non-approved paths intentionally cover the states a parent can see in
    the resource queue: pending, rejected and needs-split.  None is a fallback for
    either host mastery evidence or a child-facing explanation/retest.
    """
    database = Database(tmp_path / "study.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child','学生','四年级','江苏南京','active',?,?)", (utc_now(), utc_now()))
        connection.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('study','child','active','study',?,?,?)", (utc_now(), utc_now(), 'resource-review-study'))
        connection.commit()
    learning = LearningStateService(database); learning.seed_catalog()
    root = tmp_path / "representative-local"; root.mkdir()
    for name, text in {
        'pending.txt': '判别式候选讲解，仍等待家长审核。',
        'rejected.txt': '判别式错误候选，家长应拒绝。',
        'split.txt': '判别式混合候选，需要拆分后再审核。',
        'approved.txt': '判别式讲解：先计算 b²-4ac，再判断根的情况。',
    }.items():
        (root / name).write_text(text, encoding='utf-8')
    spec = CorpusSpec('review-boundary', '本机代表性错题讲解', 'local', root, None, 'local', 'parent_owned')
    ingestor = LocalCorpusIngestor(database); ingestor.run(spec, ingestor.start(spec, idempotency_key='review-boundary-v1'))
    graph = KnowledgeGraphService(database)
    node = graph.upsert_node(canonical_key='review-boundary-discriminant', node_type='knowledge_point', title='根的判别式', scope={'grade':'四年级','subject':'数学'}, source_ref='product_evidence:catalog', review_status='approved')
    exercise = graph.upsert_node(canonical_key='diagnostic:diag-p4-arithmetic-closed-1', node_type='exercise', title='判别式独立复测', source_ref='product_evidence:catalog', review_status='approved')
    graph.link(node, exercise, 'assessed_by', rationale='真实宿主判分题', source_ref='product_evidence:catalog', review_status='approved')
    with database.connect() as connection:
        rows = connection.execute("""SELECT r.resource_id,r.title,s.segment_id FROM kg_resource r
            JOIN kg_segment s ON s.resource_id=r.resource_id ORDER BY r.title""").fetchall()
    by_name = {next(name for name in ('pending', 'rejected', 'split', 'approved') if name in row['title']): row for row in rows}
    for row in by_name.values():
        graph.map_segment(row['resource_id'], row['segment_id'], node, 'explains', confidence=.91, source_ref='local-lexical-candidate')
    quality = GraphQualityService(database)
    quality.record_resource_review(resource_id=by_name['rejected']['resource_id'], node_id=node, relation_type='explains', decision='rejected', reviewer='家长', client_action_id='reject-boundary')
    quality.record_resource_review(resource_id=by_name['split']['resource_id'], node_id=node, relation_type='explains', decision='needs_split', reviewer='家长', client_action_id='split-boundary')
    attempt = learning.submit_diagnostic('study', 'diag-p4-arithmetic-closed-1', '12', 'review-boundary-wrong-answer')
    mistake = MistakeBookService(database).capture_from_attempt(attempt)
    # Pending/rejected/needs-split mappings do not permit a KG diagnosis or materials.
    assert HostDiagnosticKnowledgeGraphBridge(database).record_attempt(attempt['attempt_id'], mistake_id=mistake['mistake_id'])['status'] == 'manual_confirmation_required'
    try:
        KnowledgeGraphApplicationService(database).learning_path('child', node)
        raise AssertionError('unapproved material leaked into a child learning path')
    except ValueError:
        pass
    assert KnowledgeGraphApplicationService(database).search_materials_for_child('child', '判别式', subject='数学') == []

    # The parent accepts exactly one resource-level mapping.  After a fresh
    # database connection, both the host bridge and child material lookup use
    # that durable approved mapping, not its siblings.
    quality.record_resource_review(resource_id=by_name['approved']['resource_id'], node_id=node, relation_type='explains', decision='approved', reviewer='家长', client_action_id='approve-boundary')
    reopened = Database(tmp_path / 'study.db')
    recorded = HostDiagnosticKnowledgeGraphBridge(reopened).record_attempt(attempt['attempt_id'], mistake_id=mistake['mistake_id'])
    assert recorded['status'] == 'recorded' and recorded['mistake_recorded'] is True and recorded['mastery']['evidence_count'] == 1
    path = KnowledgeGraphApplicationService(reopened).learning_path('child', node)
    assert len(path['materials']) == 1 and path['materials'][0]['title'].endswith(' · approved')
    assert 'text' not in path['materials'][0] and '判别式讲解' in path['materials'][0]['preview']
    assert [item['title'] for item in path['diagnosis']['retest_items']] == ['判别式独立复测']
    assert path['truthfulness']['unreviewed_candidates_hidden'] is True
    plan = KnowledgeGraphApplicationService(reopened).review_plan(child_id='child', action_id='review-boundary-plan', max_items=3)
    assert [item['knowledge_node_id'] for item in plan['items']] == [node]
    # A non-undo correction through the same parent API cannot retain a plan
    # that names the now-rejected local segment.
    GraphQualityService(reopened).record_resource_review(resource_id=by_name['approved']['resource_id'], node_id=node, relation_type='explains', decision='rejected', reviewer='家长', client_action_id='reject-after-approval')
    assert KnowledgeGraphApplicationService(reopened).latest_review_plan('child') is None
    GraphQualityService(reopened).record_resource_review(resource_id=by_name['approved']['resource_id'], node_id=node, relation_type='explains', decision='approved', reviewer='家长', client_action_id='reapprove-boundary')
    plan = KnowledgeGraphApplicationService(reopened).review_plan(child_id='child', action_id='review-boundary-plan-2', max_items=3)
    assert [item['knowledge_node_id'] for item in plan['items']] == [node]

    # Undo survives restart and removes child access immediately; prior evidence
    # remains auditable but cannot be used to leak withdrawn source material.
    GraphQualityService(reopened).undo_resource_review(resource_id=by_name['approved']['resource_id'], node_id=node, relation_type='explains', reviewer='家长', client_action_id='undo-boundary')
    final = Database(tmp_path / 'study.db')
    try:
        KnowledgeGraphApplicationService(final).learning_path('child', node)
        raise AssertionError('withdrawn resource still leaked into a child learning path')
    except ValueError:
        pass
    assert KnowledgeGraphApplicationService(final).search_materials_for_child('child', '判别式', subject='数学') == []
    assert KnowledgeGraphApplicationService(final).latest_review_plan('child') is None
    with final.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM kg_mastery_evidence WHERE child_id='child'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM kg_review_plan WHERE status='superseded'").fetchone()[0] == 2
