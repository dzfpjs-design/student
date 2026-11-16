"""Current school observations steer practice without becoming mastery samples."""
import json
import sqlite3
from datetime import datetime, timezone
import pytest

from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.learning_feedback import build_learning_feedback
from home_study_agent.strategy_evolution import StrategyEvolutionService, STUCK_VERSION
from test_school_open_reviews import school_case, answer_and_review, next_day, CHILD


def feedback(db, child=CHILD):
    with db.connect() as c:
        return build_learning_feedback(c, child, datetime.now(timezone.utc))


def model(client, headers):
    response = client.get(f'/v1/parent/children/{CHILD}/longitudinal-model', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()['payload']


def test_low_original_api_strategy_next_day_retraction_and_reopen(school_case, monkeypatch, tmp_path):
    db, client, headers = school_case[:3]
    _, saved, assessment, path, body = answer_and_review(school_case, score=0)
    original = saved['attempt']['evidence_id']
    learning = model(client, headers)['learning']
    node = learning['reliable_feedback']['nodes'][0]
    assert node['status'] == 'recheck_needed'
    assert node['evidence_refs'] == [original] and node['qualified_original_count'] == 1
    assert node['attempt_count'] == node['independent_correct'] == node['retention_success_count'] == 0
    assert learning['evidenced_node_count'] == 0 and learning['qualified_original_count'] == 1
    priority = ChildModelService(db).compact_for_agent(CHILD)['learning_priorities'][0]
    assert priority['next_action'] == 'clarify_then_recheck' and priority['evidence_refs'] == [original]
    service = StrategyEvolutionService(db)
    candidate = service.maybe_propose(CHILD)
    assert candidate['candidate_version'] == STUCK_VERSION and candidate['status'] == 'draft'
    with db.connect() as c:
        basis = json.loads(c.execute('SELECT candidate_config_json FROM strategy_experiment WHERE experiment_id=?',
            (candidate['experiment_id'],)).fetchone()[0])['selection']['basis']
        reviews_before = [tuple(r) for r in c.execute('SELECT review_task_id,status,diagnostic_item_id FROM review_task')]
        assert basis['version'] == 'selection_basis_v2' and not basis['learning']
        assert len(basis['original_observations']) == 1
    assert client.post(path, headers=headers, json=body).status_code == 200
    reopened = client.school_reopen()
    unlocked = reopened.post('/v1/parent/unlock', json={'pin': '8264'})
    headers = {'X-HSC-Parent': unlocked.json()['parent_token']}
    assert model(reopened, headers)['learning']['qualified_original_count'] == 1
    assert StrategyEvolutionService(Database(db.path)).maybe_propose(CHILD)['experiment_id'] == candidate['experiment_id']
    with db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT review_task_id,status,diagnostic_item_id FROM review_task')] == reviews_before
    case = (db, reopened, headers, *school_case[3:])
    case, slot, _, _, clock = next_day(case, monkeypatch, key='observation-next-day', execute=False)
    assert slot['source_ref'] == reviews_before[0][0]
    assert '本份原稿' in slot['completion_standard']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 1
        assert [tuple(r) for r in c.execute('SELECT review_task_id,status,diagnostic_item_id FROM review_task')] == reviews_before
    # Source observation survives day rollover but disappears immediately on withdrawal.
    response = case[1].post(path, headers=case[2], json={'operation': 'retract', 'expected_revision': 1,
        'client_action_id': 'observation-withdraw', 'reason': 'Withdraw the original human score'})
    assert response.status_code == 200, response.text
    assert model(case[1], case[2])['learning']['reliable_feedback']['nodes'] == []
    assert service.get(candidate['experiment_id'])['status'] == 'rolled_back'
    with Database(db.path).connect() as c:
        assert c.execute('SELECT status FROM review_task').fetchone()[0] == 'cancelled'
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert build_learning_feedback(c, CHILD, clock.now(timezone.utc))['nodes'] == []
    (tmp_path / 'lifecycle-result.json').write_text(json.dumps({
        'evidence_id': original, 'assessment_id': assessment['assessment_id'],
        'strategy_id': candidate['experiment_id'], 'review_id': reviews_before[0][0],
        'review_target_id': reviews_before[0][2],
        'before_exact_samples': 0, 'before_observations': 1, 'review_count_after_reopen_and_plan': 1,
        'after_withdraw_nodes': [], 'strategy_after_withdraw': 'rolled_back',
        'boundary': 'ASGI app lifespan restart, synthetic sources/review/planner; no external model'}))


def test_reference_foreign_child_changed_version_and_rollback_fail_closed(school_case):
    db, client, headers = school_case[:3]
    prompt, saved, _, path, body = answer_and_review(school_case, score=0, submit=False)
    assert feedback(db)['nodes'] == []
    assert client.post(path, headers=headers, json=body).status_code == 200
    baseline = feedback(db)
    assert baseline['nodes'][0]['qualified_original_count'] == 1
    assert feedback(db, 'child-primary-4')['nodes'] == []
    mutations = [
        ('UPDATE diagnostic_item SET prompt=prompt || ? WHERE diagnostic_item_id=?', (' changed', prompt['diagnostic_item_id'])),
        ('UPDATE diagnostic_attempt SET child_id=? WHERE evidence_id=?', ('child-primary-4', saved['attempt']['evidence_id'])),
        ('UPDATE learning_evidence SET evidence_type=? WHERE evidence_id=?', ('self_report', saved['attempt']['evidence_id'])),
    ]
    for sql, params in mutations:
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute(sql, params)
            assert build_learning_feedback(c, CHILD, datetime.now(timezone.utc))['nodes'] == []
            c.rollback()
        assert feedback(db)['evidence_fingerprint'] == baseline['evidence_fingerprint']
    with db.connect() as c:
        with pytest.raises(sqlite3.IntegrityError, match='RUBRIC_HISTORY_IMMUTABLE'):
            c.execute('UPDATE rubric_assessment SET payload_json=? WHERE child_id=?', ('{}', CHILD))
        c.rollback()
        c.execute('PRAGMA query_only=ON')
        assert build_learning_feedback(c, CHILD, datetime.now(timezone.utc)) == baseline


def test_high_original_never_becomes_independent_mastery_or_effectiveness(school_case):
    db, client, headers = school_case[:3]
    answer_and_review(school_case, score=3)
    node = feedback(db)['nodes'][0]
    assert node['status'] == 'insufficient_evidence'
    assert node['next_action'] == 'schedule_independent_check'
    assert node['attempt_count'] == node['independent_attempt_count'] == node['independent_correct'] == 0
    assert node['retention_success_count'] == node['independent_distinct_items'] == 0
    assert node['mastery_claim_allowed'] is False
    assert StrategyEvolutionService(db).maybe_propose(CHILD) is None
    assert model(client, headers)['learning']['evidenced_node_count'] == 0


def test_high_observation_cannot_clear_prior_recheck_or_add_exact_samples():
    from home_study_agent.learning_feedback import _merge_school_observations
    node = dict(knowledge_node_id='node', status='recheck_needed', next_action='clarify_then_recheck',
        reason='Independent check still required', latest_evidence_at='2026-09-10T00:00:00+00:00',
        evidence_refs=['exact'], attempt_count=1, independent_correct=0)
    fact = dict(knowledge_node_id='node', evidence_id='original', passed=True,
        occurred_at='2026-09-11T00:00:00+00:00')
    nodes = [node]
    _merge_school_observations(nodes, [fact], {'node': {'effective_evidence': [{'evidence_id': 'exact'}]}})
    assert node['status'] == 'recheck_needed' and node['attempt_count'] == 1
    assert node['independent_correct'] == 0 and node['qualified_original_count'] == 1
    # Even a duplicate representation of an exact original is not another observation.
    _merge_school_observations(nodes, [{**fact, 'evidence_id': 'exact'}],
        {'node': {'effective_evidence': [{'evidence_id': 'exact'}]}})
    assert node['qualified_original_count'] == 1
